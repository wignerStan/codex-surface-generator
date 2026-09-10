"""Codex local thread-storage extraction with explicit, independently testable modules."""
from __future__ import annotations

import re
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceSnapshot, semantic_fingerprint
from .registry import ExtractorResult, register_extractor
from .local_storage_specs import EXTRACTOR_ID, SCHEMA_VERSION, SOURCE_IDS
from .local_storage_sources import (
    _source, _emit, _check_sources, _const_or_expected,
    _duration_product, _database_catalog, _integer_call,
)
from .local_storage_layout import (
    build_scope, build_roots, build_identity_domains, build_rollouts, build_databases,
)
from .local_storage_lifecycle import build_transitions, build_sidecars, build_invariants


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
            "scope": build_scope(),
            "roots": build_roots(
                archive_dir=archive_dir,
                database_catalog=database_catalog,
                session_index_file=session_index_file,
                sessions_dir=sessions_dir,
                snapshot_dir=snapshot_dir,
                writer_lock_dir=writer_lock_dir,
            ),
            "identity_domains": build_identity_domains(),
            "rollouts": build_rollouts(
                archive_dir=archive_dir,
                session_index_file=session_index_file,
                sessions_dir=sessions_dir,
            ),
            "databases": build_databases(
                busy_timeout_seconds=busy_timeout_seconds,
                database_catalog=database_catalog,
                max_connections=max_connections,
                threads_has_session_id_column=threads_has_session_id_column,
            ),
            "transitions": build_transitions(archive_dir=archive_dir, sessions_dir=sessions_dir),
            "sidecars": build_sidecars(
                coordination_lock=coordination_lock,
                snapshot_dir=snapshot_dir,
                snapshot_retention_seconds=snapshot_retention_seconds,
                writer_lock_dir=writer_lock_dir,
            ),
            "invariants": build_invariants(),
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
