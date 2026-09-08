"""Canonical Codex local thread-storage and rollout-layout extraction.

This extractor keeps the logical identity model separate from physical files:
``session_id`` groups a root thread and its subagents, ``thread_id`` names one
logical thread, and ``rollout_id`` identifies an immutable rollout revision.
"""

from __future__ import annotations

import re
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot, semantic_fingerprint
from .registry import ExtractorResult, register_extractor


EXTRACTOR_ID = "extractor.local_storage"
SCHEMA_VERSION = "1.0.0"
SOURCE_IDS = {
    "home_dir": "source_spec.extra.local_storage_home_dir",
    "thread_types": "source_spec.extra.local_storage_thread_types",
    "rollout_lib": "source_spec.extra.local_storage_rollout_lib",
    "rollout_filename": "source_spec.extra.local_storage_rollout_filename",
    "rollout_recorder": "source_spec.extra.local_storage_rollout_recorder",
    "rollout_compression": "source_spec.extra.local_storage_rollout_compression",
    "session_index": "source_spec.extra.local_storage_session_index",
    "revert_thread": "source_spec.extra.local_storage_revert_thread",
    "paginated_fork": "source_spec.extra.local_storage_paginated_fork",
    "archive_thread": "source_spec.extra.local_storage_archive_thread",
    "unarchive_thread": "source_spec.extra.local_storage_unarchive_thread",
    "delete_thread": "source_spec.extra.local_storage_delete_thread",
    "writer_lock": "source_spec.extra.local_storage_writer_lock",
    "state_sqlite": "source_spec.extra.local_storage_state_sqlite",
    "state_threads": "source_spec.extra.local_storage_state_threads",
    "threads_migration": "source_spec.extra.local_storage_threads_migration",
    "history_materialization": "source_spec.extra.local_storage_history_materialization",
    "shell_snapshot": "source_spec.extra.local_storage_shell_snapshot",
    "visualization": "source_spec.extra.local_storage_visualization",
    "config_toml": "source_spec.extra.config_toml",
}

_DB_SPECS = (
    ("state", "STATE_DB_FILENAME", "thread metadata, lookup, lifecycle state, and current rollout pointer"),
    ("logs", "LOGS_DB_FILENAME", "structured runtime logs"),
    ("goals", "GOALS_DB_FILENAME", "goal state"),
    ("memories", "MEMORIES_DB_FILENAME", "memory state"),
    ("queue", "QUEUE_DB_FILENAME", "durable user-message queue"),
    (
        "thread_history",
        "THREAD_HISTORY_DB_FILENAME",
        "derived paginated thread-history projection and byte/ordinal positions",
    ),
)

_REQUIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "home_dir": (
        'std::env::var("CODEX_HOME")',
        'p.push(".codex")',
    ),
    "thread_types": (
        "Session id shared by the root thread and all of its subagents.",
        "pub session_id: SessionId",
        "pub thread_id: ThreadId",
        "Stable logical thread to revert.",
    ),
    "rollout_lib": (
        'pub const SESSIONS_SUBDIR: &str = "sessions";',
        'pub const ARCHIVED_SESSIONS_SUBDIR: &str = "archived_sessions";',
    ),
    "rollout_filename": (
        "ids.split_once('_').unwrap_or((ids, ids))",
        "self.thread_id == self.rollout_id",
        'format!("rollout-{timestamp}-{}_{}.jsonl"',
    ),
    "rollout_recorder": (
        "rollout_id_override: Option<RolloutId>",
        "pub fn with_session_id",
        "pub fn with_rollout_id",
        "rollout_id_override.unwrap_or(thread_id)",
        "OffsetDateTime::now_local()",
        "dir.push(SESSIONS_SUBDIR);",
        "let session_meta = SessionMeta {",
        "session_id,",
        "id: conversation_id,",
        "Self::Resume { path }",
    ),
    "rollout_compression": (
        'const COMPRESSED_SUFFIX: &str = ".zst";',
        "plain_file_name",
    ),
    "session_index": (
        'const SESSION_INDEX_FILE: &str = "session_index.jsonl";',
        "Name updates are append-only",
        "pub id: ThreadId",
    ),
    "revert_thread": (
        "creating a new immutable rollout file",
        "Old rollouts stay intact.",
        "source_meta.history_mode != ThreadHistoryMode::Paginated",
        "let rollout_id = ThreadId::new();",
        ".with_session_id(source_meta.session_id)",
        ".with_rollout_id(rollout_id)",
        "source_meta.forked_from_id",
        ".replace_rollout_path_if_current(",
    ),
    "paginated_fork": (
        "resolve_rollout_lineage_for_reference",
        "materialize_to_sqlite",
        "HistoryPosition {",
        "end_ordinal_exclusive",
        "end_byte_offset",
    ),
    "archive_thread": (
        "ARCHIVED_SESSIONS_SUBDIR",
        "archive_folder.join(&file_name)",
        "std::fs::rename(source, destination)",
        ".mark_archived(",
    ),
    "unarchive_thread": (
        "rollout_date_parts(&file_name)",
        "SESSIONS_SUBDIR",
        "std::fs::rename(source, destination)",
        ".mark_unarchived(",
    ),
    "delete_thread": (
        "RolloutReferenceIndex::scan",
        "forked history still references it",
        "remove_thread_name_entries",
    ),
    "writer_lock": (
        'const WRITER_LOCK_DIR: &str = "thread-writer-locks";',
        'const COORDINATION_LOCK_FILE: &str = ".coordination.lock";',
        'format!("{thread_id}.lock")',
        "file.try_lock()",
    ),
    "state_sqlite": (
        "RUNTIME_DBS: [RuntimeDbSpec; 6]",
        ".journal_mode(SqliteJournalMode::Wal)",
        ".synchronous(SqliteSynchronous::Normal)",
        ".auto_vacuum(SqliteAutoVacuum::Incremental)",
        ".busy_timeout(Duration::from_secs(5))",
        ".max_connections(5)",
    ),
    "state_threads": (
        "attached to the stable thread id",
        'UPDATE threads SET rollout_path = ? WHERE id = ? AND rollout_path = ?',
    ),
    "threads_migration": (
        "CREATE TABLE threads",
        "id TEXT PRIMARY KEY",
        "rollout_path TEXT NOT NULL",
    ),
    "history_materialization": (
        "pub(super) async fn materialize_to_sqlite",
        "let initial_ordinal = session_meta",
        ".history_base",
        "apply_projection(",
    ),
    "shell_snapshot": (
        'const SNAPSHOT_DIR: &str = "shell_snapshots";',
        "Duration::from_secs(60 * 60 * 24 * 3)",
        'format!("{session_id}.{nonce}.{extension}")',
        "thread_id = %config.session_id",
    ),
    "visualization": (
        'codex_home.join("visualizations")',
        'created_at.format("%Y/%m/%d")',
        '.join("visualization-viewers")',
        ".join(artifact_thread_id)",
    ),
    "config_toml": (
        "Directory where Codex stores the SQLite state DB.",
        "Defaults to `$CODEX_SQLITE_HOME` when set. Otherwise uses `$CODEX_HOME`.",
        "pub sqlite_home: Option<AbsolutePathBuf>",
    ),
}


def _source(snapshot: SourceSnapshot, key: str) -> SourceFile | None:
    return snapshot.files.get(SOURCE_IDS[key])


def _source_ref(source: SourceFile) -> str:
    return f"{source.spec_id}:{source.selected_path}"


def _emit(
    diagnostics: DiagnosticCollector,
    *,
    code: str,
    message: str,
    entity: str,
    source: SourceFile | None = None,
    details: dict[str, Any] | None = None,
    severity: str = "error",
) -> None:
    diagnostics.emit(
        code=code,
        severity=severity,
        category="local_storage",
        message=message,
        extractor_id=EXTRACTOR_ID,
        entity_id=entity,
        source_refs=(_source_ref(source),) if source else (),
        details=details or {},
        recoverable=False,
        strict_failure=severity == "error",
    )


def _string_const(source: SourceFile | None, name: str) -> str | None:
    if source is None:
        return None
    match = re.search(
        rf"\b(?:pub\s+)?const\s+{re.escape(name)}\s*:\s*&str\s*=\s*\"([^\"]+)\"\s*;",
        source.text,
    )
    return match.group(1) if match else None


def _integer_call(source: SourceFile | None, call: str) -> int | None:
    if source is None:
        return None
    match = re.search(rf"{re.escape(call)}\((\d+)\)", source.text)
    return int(match.group(1)) if match else None


def _duration_product(source: SourceFile | None, constant: str) -> int | None:
    if source is None:
        return None
    match = re.search(
        rf"\b{re.escape(constant)}\s*:\s*Duration\s*=\s*Duration::from_secs\(([^)]+)\)",
        source.text,
    )
    if not match:
        return None
    factors = [part.strip() for part in match.group(1).split("*")]
    if not factors or not all(part.isdigit() for part in factors):
        return None
    result = 1
    for factor in factors:
        result *= int(factor)
    return result


def _check_sources(
    snapshot: SourceSnapshot,
    diagnostics: DiagnosticCollector,
) -> tuple[bool, list[dict[str, Any]]]:
    complete = True
    checks: list[dict[str, Any]] = []
    for key, markers in _REQUIRED_MARKERS.items():
        source = _source(snapshot, key)
        if source is None:
            complete = False
            checks.append(
                {
                    "source_key": key,
                    "source_spec_id": SOURCE_IDS[key],
                    "available": False,
                    "missing_markers": list(markers),
                }
            )
            _emit(
                diagnostics,
                code="LOCAL_STORAGE_SOURCE_UNAVAILABLE",
                message=f"Required local-storage source is unavailable: {key}.",
                entity=f"local_storage.source.{key}",
                details={"source_spec_id": SOURCE_IDS[key]},
            )
            continue
        missing = [marker for marker in markers if marker not in source.text]
        checks.append(
            {
                "source_key": key,
                "source_spec_id": source.spec_id,
                "available": True,
                "selected_path": source.selected_path,
                "sha256": source.content_sha256,
                "missing_markers": missing,
            }
        )
        if missing:
            complete = False
            _emit(
                diagnostics,
                code="LOCAL_STORAGE_SEMANTIC_MARKER_MISSING",
                message=f"Local-storage semantics changed or could not be classified in {key}.",
                entity=f"local_storage.source.{key}",
                source=source,
                details={"missing_markers": missing},
            )
    return complete, checks


def _database_catalog(
    source: SourceFile | None,
    diagnostics: DiagnosticCollector,
) -> tuple[list[dict[str, Any]], bool]:
    catalog: list[dict[str, Any]] = []
    complete = True
    for database_id, constant, purpose in _DB_SPECS:
        filename = _string_const(source, constant)
        if filename is None:
            complete = False
            _emit(
                diagnostics,
                code="LOCAL_STORAGE_DATABASE_FILENAME_UNRESOLVED",
                message=f"Could not resolve {constant} from Codex's SQLite catalog.",
                entity=f"local_storage.database.{database_id}",
                source=source,
                details={"constant": constant},
            )
            filename = f"<unresolved:{constant}>"
        catalog.append(
            {
                "id": database_id,
                "constant": constant,
                "filename": filename,
                "path_template": f"$SQLITE_HOME/{filename}",
                "purpose": purpose,
            }
        )
    return catalog, complete


def _const_or_expected(
    source: SourceFile | None,
    name: str,
    expected: str,
) -> tuple[str, bool]:
    value = _string_const(source, name)
    return (value if value is not None else expected, value is not None)


class LocalStorageExtractor:
    extractor_id = EXTRACTOR_ID
    # The rollout filename implementation is the activation source. Once it is
    # present, the extractor consumes the complete local-storage source family.
    source_spec_ids = (SOURCE_IDS["rollout_filename"],)
    result_source_spec_ids = tuple(dict.fromkeys(SOURCE_IDS.values()))

    def extract(
        self,
        snapshot: SourceSnapshot,
        diagnostics: DiagnosticCollector,
    ) -> ExtractorResult:
        complete, marker_checks = _check_sources(snapshot, diagnostics)

        rollout_lib = _source(snapshot, "rollout_lib")
        session_index_source = _source(snapshot, "session_index")
        writer_lock_source = _source(snapshot, "writer_lock")
        shell_source = _source(snapshot, "shell_snapshot")
        sqlite_source = _source(snapshot, "state_sqlite")
        migration_source = _source(snapshot, "threads_migration")

        sessions_dir, sessions_resolved = _const_or_expected(
            rollout_lib, "SESSIONS_SUBDIR", "sessions"
        )
        archive_dir, archive_resolved = _const_or_expected(
            rollout_lib, "ARCHIVED_SESSIONS_SUBDIR", "archived_sessions"
        )
        session_index_file, session_index_resolved = _const_or_expected(
            session_index_source, "SESSION_INDEX_FILE", "session_index.jsonl"
        )
        writer_lock_dir, writer_lock_resolved = _const_or_expected(
            writer_lock_source, "WRITER_LOCK_DIR", "thread-writer-locks"
        )
        coordination_lock, coordination_resolved = _const_or_expected(
            writer_lock_source, "COORDINATION_LOCK_FILE", ".coordination.lock"
        )
        snapshot_dir, snapshot_dir_resolved = _const_or_expected(
            shell_source, "SNAPSHOT_DIR", "shell_snapshots"
        )
        snapshot_retention_seconds = _duration_product(shell_source, "SNAPSHOT_RETENTION")
        if snapshot_retention_seconds is None:
            snapshot_retention_seconds = 3 * 24 * 60 * 60
            complete = False
            _emit(
                diagnostics,
                code="LOCAL_STORAGE_SNAPSHOT_RETENTION_UNRESOLVED",
                message="Could not resolve shell snapshot retention from source.",
                entity="local_storage.sidecar.shell_snapshot.retention",
                source=shell_source,
            )

        database_catalog, database_complete = _database_catalog(sqlite_source, diagnostics)
        complete = complete and database_complete
        busy_timeout_seconds = _integer_call(sqlite_source, "Duration::from_secs")
        max_connections = _integer_call(sqlite_source, ".max_connections")
        if busy_timeout_seconds is None:
            busy_timeout_seconds = 5
            complete = False
        if max_connections is None:
            max_connections = 5
            complete = False

        migration_text = migration_source.text if migration_source else ""
        threads_has_session_id_column = bool(
            re.search(r"(?m)^\s*session_id\s+", migration_text)
        )
        if threads_has_session_id_column:
            complete = False
            _emit(
                diagnostics,
                code="LOCAL_STORAGE_SQLITE_IDENTITY_SHAPE_CHANGED",
                message=(
                    "The initial threads table now appears to contain session_id; "
                    "the identity authority model requires review."
                ),
                entity="local_storage.database.state.threads.session_id",
                source=migration_source,
            )

        resolution_complete = all(
            (
                sessions_resolved,
                archive_resolved,
                session_index_resolved,
                writer_lock_resolved,
                coordination_resolved,
                snapshot_dir_resolved,
            )
        )
        complete = complete and resolution_complete

        semantic_payload: dict[str, Any] = {
            "scope": {
                "id": "codex_thread_rollout_local_storage",
                "authority": "pinned_public_codex_source",
                "includes": [
                    "thread/session/rollout identity",
                    "active and archived rollout files",
                    "state and projection SQLite databases",
                    "thread lifecycle cutovers",
                    "thread-keyed local sidecars and indexes",
                ],
                "excludes": [
                    "remote or cloud thread stores",
                    "Desktop-private databases",
                    "unrelated auth, configuration, cache, and marketplace artifacts",
                ],
            },
            "roots": {
                "codex_home": {
                    "symbol": "CODEX_HOME",
                    "environment_variable": "CODEX_HOME",
                    "default": "~/.codex",
                    "resolution": "non-empty environment override; otherwise HOME/.codex",
                    "contains": [
                        sessions_dir,
                        archive_dir,
                        session_index_file,
                        writer_lock_dir,
                        snapshot_dir,
                        "visualizations",
                        "visualization-viewers",
                    ],
                },
                "sqlite_home": {
                    "symbol": "SQLITE_HOME",
                    "config_key": "sqlite_home",
                    "environment_variable": "CODEX_SQLITE_HOME",
                    "fallback": "$CODEX_HOME",
                    "contains": [item["filename"] for item in database_catalog],
                    "may_equal_codex_home": True,
                },
            },
            "identity_domains": {
                "session_id": {
                    "rust_type": "SessionId",
                    "meaning": "session/tree identity shared by the root thread and its subagents",
                    "rollout_metadata_field": "session_meta.payload.session_id",
                    "filename_component": False,
                    "sqlite_threads_primary_key": False,
                },
                "thread_id": {
                    "rust_type": "ThreadId",
                    "meaning": "stable logical thread identity",
                    "rollout_metadata_field": "session_meta.payload.id",
                    "filename_component": "first UUID",
                    "sqlite_threads_primary_key": "threads.id",
                    "sidecar_key": True,
                },
                "rollout_id": {
                    "rust_type": "RolloutId",
                    "meaning": "immutable physical rollout revision identity",
                    "rollout_metadata_field": None,
                    "filename_component": "first UUID for ordinary files; suffix UUID after underscore for replacements",
                    "ordinary_equals_thread_id": True,
                    "sqlite_threads_primary_key": False,
                },
            },
            "rollouts": {
                "canonical_role": "durable replay history",
                "active": {
                    "root": f"$CODEX_HOME/{sessions_dir}",
                    "path_template": (
                        f"$CODEX_HOME/{sessions_dir}/YYYY/MM/DD/<rollout_filename>"
                    ),
                    "partition_clock": "local time at rollout creation",
                },
                "archived": {
                    "root": f"$CODEX_HOME/{archive_dir}",
                    "path_template": f"$CODEX_HOME/{archive_dir}/<rollout_filename>",
                    "partition": "flat",
                    "preserves_basename": True,
                },
                "filename_grammar": {
                    "ordinary": (
                        "rollout-<YYYY-MM-DDTHH-MM-SS>-<thread_id>.jsonl[.zst]"
                    ),
                    "replacement": (
                        "rollout-<YYYY-MM-DDTHH-MM-SS>-<thread_id>_<rollout_id>.jsonl[.zst]"
                    ),
                    "parse_rule": (
                        "split the ID suffix once at underscore; without an underscore, "
                        "rollout_id defaults to thread_id"
                    ),
                    "ordinary_identity_relation": "thread_id == rollout_id",
                    "replacement_identity_relation": "thread_id != rollout_id",
                    "underscore_meaning": "distinct rollout revision, not a fork edge",
                },
                "physical_representations": {
                    "plain": ".jsonl",
                    "compressed": ".jsonl.zst",
                    "canonical_identity_name": "plain .jsonl filename",
                    "readers_are_representation_transparent": True,
                },
                "session_meta": {
                    "record_position": "first rollout record",
                    "thread_id_field": "payload.id",
                    "session_id_field": "payload.session_id",
                    "history_base_field": "payload.history_base",
                    "forked_from_id_field": "payload.forked_from_id",
                    "parent_thread_id_field": "payload.parent_thread_id",
                },
                "name_index": {
                    "path_template": f"$CODEX_HOME/{session_index_file}",
                    "format": "append-only JSONL",
                    "key": "thread_id",
                    "conflict_rule": "most recent entry wins",
                    "canonical_history": False,
                },
                "paginated_lineage": {
                    "history_base": {
                        "rollout_id_field": "HistoryPosition.thread_id",
                        "end_ordinal_exclusive_field": (
                            "HistoryPosition.end_ordinal_exclusive"
                        ),
                        "end_byte_offset_field": "HistoryPosition.end_byte_offset",
                    },
                    "old_rollouts_may_be_referenced": True,
                    "thread_history_projection": "derived from newline-terminated rollout records",
                },
            },
            "databases": {
                "root": "$SQLITE_HOME",
                "files": database_catalog,
                "connection_policy": {
                    "journal_mode": "WAL",
                    "synchronous": "NORMAL",
                    "auto_vacuum": "INCREMENTAL",
                    "busy_timeout_seconds": busy_timeout_seconds,
                    "max_write_connections": max_connections,
                },
                "state_thread_pointer": {
                    "database_id": "state",
                    "table": "threads",
                    "logical_key_column": "id",
                    "logical_key_domain": "thread_id",
                    "current_rollout_path_column": "rollout_path",
                    "session_id_column_present_in_initial_table": (
                        threads_has_session_id_column
                    ),
                    "session_id_authority": "rollout session_meta payload",
                    "rollout_id_row_created_for_replacement": False,
                    "compare_and_swap_sql": (
                        "UPDATE threads SET rollout_path = ? "
                        "WHERE id = ? AND rollout_path = ?"
                    ),
                    "mutation_scope_on_revert": "physical rollout_path only",
                },
                "authority_model": {
                    "rollout_jsonl": "canonical durable replay history",
                    "state_database": "mutable metadata index and current-rollout pointer",
                    "thread_history_database": "derived paginated projection",
                    "session_index_jsonl": "append-only display-name index",
                },
            },
            "transitions": {
                "create": {
                    "logical_thread": "new thread_id",
                    "session_id": (
                        "defaults to thread_id; may be overridden to the root session_id for subagents"
                    ),
                    "rollout_id": "thread_id",
                    "filename_form": "ordinary",
                },
                "resume": {
                    "logical_thread": "same thread_id",
                    "session_id": "preserved",
                    "rollout_id": "same current rollout revision",
                    "storage_action": "open the resolved existing rollout path for append",
                    "creates_replacement_rollout": False,
                },
                "revert": {
                    "precondition": "paginated thread with state database",
                    "logical_thread": "same thread_id",
                    "session_id": "preserved verbatim from source_meta.session_id",
                    "rollout_id": "new UUID",
                    "filename_form": "replacement",
                    "forked_from_id": "preserved",
                    "parent_thread_id": "preserved",
                    "old_rollouts": "retained and may back the history_base reference",
                    "cutover": "compare-and-swap threads.rollout_path",
                    "creates_thread_row_for_rollout_id": False,
                    "on_compare_and_swap_conflict": "delete unpublished replacement and fail",
                },
                "fork": {
                    "logical_thread": "new thread_id",
                    "session_id": "operation-dependent; agent descendants share the root session_id",
                    "forked_from_id": "source thread_id",
                    "rollout_id": "new thread_id for the ordinary first rollout",
                    "paginated_storage": "may reference an immutable source prefix through history_base",
                    "equivalent_to_revert": False,
                },
                "archive": {
                    "thread_id": "preserved",
                    "rollout_filenames": "preserved",
                    "file_action": (
                        f"move owned rollouts from {sessions_dir}/... to flat {archive_dir}/"
                    ),
                    "state_action": "mark archived and update current rollout path",
                },
                "unarchive": {
                    "thread_id": "preserved",
                    "rollout_filenames": "preserved",
                    "file_action": (
                        f"restore rollouts from {archive_dir}/ to {sessions_dir}/YYYY/MM/DD/"
                    ),
                    "state_action": "mark unarchived and update current rollout path",
                },
                "delete": {
                    "reference_safety": (
                        "reject deletion while rollouts outside the deletion set reference its history"
                    ),
                    "file_action": "remove active/archived owned rollout representations",
                    "index_action": "remove thread-name index entries",
                    "projection_action": "remove rollout-id projection rows",
                },
            },
            "sidecars": {
                "writer_lock": {
                    "path_template": (
                        f"$CODEX_HOME/{writer_lock_dir}/<thread_id>.lock"
                    ),
                    "coordination_path": (
                        f"$CODEX_HOME/{writer_lock_dir}/{coordination_lock}"
                    ),
                    "key_domain": "thread_id",
                    "purpose": "cross-process single-writer exclusion and lifecycle coordination",
                    "durability_role": "coordination only",
                },
                "shell_snapshot": {
                    "path_template": (
                        f"$CODEX_HOME/{snapshot_dir}/<thread_id>.<nonce>.<sh|ps1>"
                    ),
                    "key_domain": "thread_id",
                    "source_parameter_name": "session_id",
                    "source_parameter_type": "ThreadId",
                    "retention_seconds": snapshot_retention_seconds,
                    "purpose": "local shell environment restoration",
                    "durability_role": "ephemeral sidecar",
                },
                "visualization": {
                    "path_template": (
                        "$CODEX_HOME/visualizations/YYYY/MM/DD/<thread_id>/<artifact>.html"
                    ),
                    "key_domain": "thread_id",
                    "date_source": "UUIDv7 thread timestamp",
                    "purpose": "assistant-authored inline visualization artifacts",
                },
                "visualization_viewer": {
                    "path_template": (
                        "$CODEX_HOME/visualization-viewers/<thread_id>/<artifact_thread_id>/..."
                    ),
                    "key_domain": "thread_id",
                    "purpose": "sandboxed materialized visualization viewer cache",
                },
            },
            "invariants": [
                {
                    "id": "local_storage.identity_domains_are_distinct",
                    "statement": "session_id, thread_id, and rollout_id are independent identity domains",
                },
                {
                    "id": "local_storage.underscore_is_rollout_revision",
                    "statement": (
                        "the underscore suffix identifies rollout_id and does not encode forked_from_id"
                    ),
                },
                {
                    "id": "local_storage.revert_preserves_logical_identity",
                    "statement": (
                        "revert preserves session_id and thread_id while rotating rollout_id"
                    ),
                },
                {
                    "id": "local_storage.resume_does_not_rotate_rollout",
                    "statement": "ordinary resume reopens the current path and does not create A_B",
                },
                {
                    "id": "local_storage.state_row_is_thread_keyed",
                    "statement": (
                        "threads.id is thread_id; a replacement rollout_id is not a second thread row"
                    ),
                },
                {
                    "id": "local_storage.pagination_is_orthogonal_to_fork",
                    "statement": (
                        "paginated persistence can be used by both a stable-thread revert and a new-thread fork"
                    ),
                },
                {
                    "id": "local_storage.sidecars_follow_thread_identity",
                    "statement": (
                        "writer locks, shell snapshots, and visualization roots are keyed by thread_id, not rollout_id"
                    ),
                },
            ],
        }

        available_sources = [
            key for key in SOURCE_IDS if _source(snapshot, key) is not None
        ]
        missing_sources = [key for key in SOURCE_IDS if _source(snapshot, key) is None]
        evidence_sources = [
            {
                "source_key": key,
                "source_spec_id": source.spec_id,
                "path": source.selected_path,
                "sha256": source.content_sha256,
                "git_blob_sha": source.git_blob_sha,
            }
            for key in SOURCE_IDS
            if (source := _source(snapshot, key)) is not None
        ]

        data: dict[str, Any] = {
            "$schema": (
                "https://schemas.codex-wire-audit.invalid/v19/"
                "local-storage-semantics-v1.schema.json"
            ),
            "source_revision": snapshot.revision.to_dict(),
            **semantic_payload,
            "coverage": {
                "required_source_specs": list(self.result_source_spec_ids),
                "available_source_keys": available_sources,
                "missing_source_keys": missing_sources,
                "marker_checks": marker_checks,
                "database_count": len(database_catalog),
                "sidecar_count": len(semantic_payload["sidecars"]),
                "transition_count": len(semantic_payload["transitions"]),
            },
            "evidence": {
                "sources": evidence_sources,
                "claim_source_keys": {
                    "roots": ["home_dir", "config_toml", "state_sqlite"],
                    "identity_domains": [
                        "thread_types",
                        "rollout_filename",
                        "rollout_recorder",
                    ],
                    "rollouts": [
                        "rollout_lib",
                        "rollout_filename",
                        "rollout_recorder",
                        "rollout_compression",
                        "session_index",
                        "paginated_fork",
                    ],
                    "database_pointer": [
                        "state_sqlite",
                        "state_threads",
                        "threads_migration",
                        "history_materialization",
                    ],
                    "lifecycle": [
                        "revert_thread",
                        "paginated_fork",
                        "archive_thread",
                        "unarchive_thread",
                        "delete_thread",
                    ],
                    "sidecars": [
                        "writer_lock",
                        "shell_snapshot",
                        "visualization",
                    ],
                },
            },
            "semantic_complete": complete,
        }
        data["semantic_digest"] = semantic_fingerprint(semantic_payload)
        return ExtractorResult(
            extractor_id=EXTRACTOR_ID,
            schema_version=SCHEMA_VERSION,
            data=data,
            semantic_complete=complete,
            source_spec_ids=self.result_source_spec_ids,
        )


@register_extractor(EXTRACTOR_ID)
def _factory() -> LocalStorageExtractor:
    return LocalStorageExtractor()
