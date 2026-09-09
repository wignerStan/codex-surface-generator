"""Source identities and reviewed markers for local-storage extraction."""

from __future__ import annotations

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
