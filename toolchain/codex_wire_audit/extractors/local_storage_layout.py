"""Pure local-storage layout projections from resolved source values."""

from __future__ import annotations

from typing import Any


def build_scope() -> dict[str, Any]:
    return {
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
    }


def build_roots(*, archive_dir: Any, database_catalog: Any, session_index_file: Any, sessions_dir: Any, snapshot_dir: Any, writer_lock_dir: Any) -> dict[str, Any]:
    return {
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
    }


def build_identity_domains() -> dict[str, Any]:
    return {
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
    }


def build_rollouts(*, archive_dir: Any, session_index_file: Any, sessions_dir: Any) -> dict[str, Any]:
    return {
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
    }


def build_databases(*, busy_timeout_seconds: Any, database_catalog: Any, max_connections: Any, threads_has_session_id_column: Any) -> dict[str, Any]:
    return {
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
    }
