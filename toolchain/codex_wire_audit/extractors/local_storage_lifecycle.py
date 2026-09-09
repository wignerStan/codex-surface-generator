"""Pure local-storage lifecycle projections from resolved source values."""

from __future__ import annotations

from typing import Any


def build_transitions(*, archive_dir: Any, sessions_dir: Any) -> dict[str, Any]:
    return {
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
    }


def build_sidecars(*, coordination_lock: Any, snapshot_dir: Any, snapshot_retention_seconds: Any, writer_lock_dir: Any) -> dict[str, Any]:
    return {
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
    }


def build_invariants() -> list[dict[str, Any]]:
    return [
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
    ]
