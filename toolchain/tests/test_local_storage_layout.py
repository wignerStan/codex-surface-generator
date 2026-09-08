from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from codex_wire_audit.diagnostics import DiagnosticCollector
from codex_wire_audit.evolution import build_evolution_contract
from codex_wire_audit.extractors import create_extractors
from codex_wire_audit.extractors.local_storage import (
    SOURCE_IDS,
    LocalStorageExtractor,
)
from codex_wire_audit.legacy import load_legacy_modules
from codex_wire_audit.models import SourceFile, SourceRevision, SourceSnapshot
from codex_wire_audit.source_registry import SourceRegistry, from_legacy_maps


SOURCES = {
    "thread_types": """
/// Session id shared by the root thread and all of its subagents.
pub session_id: SessionId,
pub thread_id: ThreadId,
pub forked_from_id: Option<ThreadId>,
""",
    "rollout_file_name": """
if self.thread_id == self.rollout_id {}
let (thread_id, rollout_id) = ids.split_once('_').unwrap_or((ids, ids));
format!("rollout-{timestamp}-{}_{}.jsonl", self.thread_id, self.rollout_id)
""",
    "rollout_recorder": """
let timestamp = OffsetDateTime::now_local()?;
dir.push(SESSIONS_SUBDIR);
let rollout_id = rollout_id_override.unwrap_or(thread_id);
let filename = RolloutFileName::new(timestamp, thread_id, rollout_id).render()?;
/// This is for creating a new immutable rollout file for an existing thread.
pub fn with_rollout_id(self, rollout_id: RolloutId) -> Self { self }
""",
    "rollout_constants": """
pub const SESSIONS_SUBDIR: &str = "sessions";
pub const ARCHIVED_SESSIONS_SUBDIR: &str = "archived_sessions";
""",
    "rollout_compression": """
const COMPRESSED_SUFFIX: &str = ".zst";
/// Transparently handles plain `.jsonl` and `.jsonl.zst` files.
pub async fn open_rollout_line_reader() {}
struct RolloutFile;
""",
    "thread_revert": """
/// Old rollouts stay intact.
let rollout_id = ThreadId::new();
let params = params
    .with_session_id(source_meta.session_id)
    .with_rollout_id(rollout_id);
state_db.replace_rollout_path_if_current(thread_id, expected, replacement).await?;
""",
    "thread_archive": """
let archive_folder = codex_home.join(ARCHIVED_SESSIONS_SUBDIR);
let destination = archive_folder.join(&file_name);
state_db.mark_archived(thread_id, destination, now).await?;
""",
    "writer_lock": """
const WRITER_LOCK_DIR: &str = "thread-writer-locks";
const COORDINATION_LOCK_FILE: &str = ".coordination.lock";
let path = self.directory.join(format!("{thread_id}.lock"));
""",
    "state_sqlite": """
const LOGS_DB_FILENAME: &str = "logs_2.sqlite";
const GOALS_DB_FILENAME: &str = "goals_1.sqlite";
const MEMORIES_DB_FILENAME: &str = "memories_1.sqlite";
const QUEUE_DB_FILENAME: &str = "queue_1.sqlite";
const STATE_DB_FILENAME: &str = "state_5.sqlite";
const THREAD_HISTORY_DB_FILENAME: &str = "thread_history_1.sqlite";
struct SqliteConfig { sqlite_home: AbsolutePathBuf }
""",
    "state_threads": """
pub async fn find_rollout_path_by_id() {}
pub async fn replace_rollout_path_if_current() {
    sqlx::query("UPDATE threads SET rollout_path = ? WHERE id = ? AND rollout_path = ?");
}
""",
    "shell_snapshot": """
const SNAPSHOT_DIR: &str = "shell_snapshots";
const SNAPSHOT_RETENTION: Duration = Duration::from_secs(60 * 60 * 24 * 3);
let snapshot_path = codex_home.join(format!("{session_id}.{nonce}.{extension}"));
let temp_path = codex_home.join(format!("{session_id}.tmp-{nonce}"));
""",
    "inline_visualization": """
let visualizations_dir = codex_home.join("visualizations");
let thread_dir = visualizations_dir
    .join(created_at.format("%Y/%m/%d").to_string())
    .join(&thread_id);
let viewer_dir = codex_home.join("visualization-viewers");
""",
}


def _registry() -> SourceRegistry:
    legacy = load_legacy_modules()
    return from_legacy_maps(
        legacy.base.FILES,
        legacy.base.SURFACE_FILES,
        legacy.entrypoint.EXTRA,
    )


def _snapshot(
    overrides: dict[str, str] | None = None,
    *,
    omit: set[str] | None = None,
) -> SourceSnapshot:
    values = dict(SOURCES)
    values.update(overrides or {})
    registry = _registry()
    files: dict[str, SourceFile] = {}
    for key, text in values.items():
        if key in (omit or set()):
            continue
        spec_id = SOURCE_IDS[key]
        spec = registry.get(spec_id)
        files[spec_id] = SourceFile.create(
            spec=spec,
            selected_path=spec.primary_path,
            raw_bytes=text.encode("utf-8"),
        )
    revision = SourceRevision(
        source_mode="fixture",
        repository="openai/codex",
        requested_ref="fixture",
        resolved_commit_sha="6af345407d9c2a568da9d01b6c4b81a9e61495c0",
        source_set_sha256=SourceSnapshot.digest_files(files),
        dirty=False,
    )
    return SourceSnapshot(revision=revision, files=files)


def test_local_storage_extractor_models_layout_and_identity_cutover() -> None:
    diagnostics = DiagnosticCollector()
    result = LocalStorageExtractor().extract(_snapshot(), diagnostics)

    assert result.semantic_complete
    assert diagnostics.summary()["error"] == 0
    assert result.data["coverage"]["source_count"] == 12
    assert result.data["coverage"]["artifact_count"] == 14
    assert result.data["filename_grammar"]["ordinary_rollout"]["identity_equation"] == (
        "rollout_id == thread_id"
    )
    replacement = result.data["filename_grammar"]["replacement_rollout"]
    assert replacement["pattern"] == (
        "rollout-{YYYY-MM-DDTHH-MM-SS}-{thread_id}_{rollout_id}.jsonl"
    )
    assert "not fork lineage" in replacement["underscore_semantics"]
    assert result.data["artifacts"]["thread_writer_lock"]["relative_pattern"] == (
        "thread-writer-locks/{thread_id}.lock"
    )
    assert result.data["artifacts"]["shell_snapshot"]["relative_pattern"] == (
        "shell_snapshots/{thread_id}.{nonce}.{sh|ps1}"
    )
    assert result.data["artifacts"]["sqlite_state"]["relative_pattern"] == (
        "state_5.sqlite"
    )
    assert result.data["pointer_model"]["replacement_cutover"]["operation"] == (
        "compare-and-swap"
    )
    revert = result.data["operations"]["revert_paginated_thread"]
    assert revert["thread_id"] == "preserved"
    assert revert["session_id"] == "preserved verbatim"
    assert revert["forked_from_id"] == "preserved"
    assert revert["rollout_id"] == "new distinct identity"


def test_local_storage_schema_validates_extracted_contract() -> None:
    diagnostics = DiagnosticCollector()
    result = LocalStorageExtractor().extract(_snapshot(), diagnostics)
    schema_path = (
        Path(__file__).parents[1]
        / "codex_wire_audit"
        / "proof_schema_templates"
        / "local-storage-layout-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(result.data)) == []


def test_local_storage_is_registered_and_linked_from_full_schema() -> None:
    registry = _registry()
    local_registry = SourceRegistry(
        registry.get(source_id) for source_id in SOURCE_IDS.values()
    )
    report = {"full_wire_schema": {"sections": {}}, "coverage": {}}
    diagnostics = DiagnosticCollector()

    contract, results = build_evolution_contract(
        _snapshot(),
        local_registry,
        diagnostics,
        coverage_profile="fixture",
        legacy_report=report,
    )

    assert "extractor.local_storage" in {
        extractor.extractor_id for extractor in create_extractors()
    }
    assert "extractor.local_storage" in results
    assert "extractor.local_storage" in contract["extractors"]
    assert report["local_storage_layout"]["authoritative"] is True
    schema_path = (
        Path(__file__).parents[1]
        / "codex_wire_audit"
        / "proof_schema_templates"
        / "local-storage-layout-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(
        Draft202012Validator(schema).iter_errors(report["local_storage_layout"])
    ) == []
    assert report["full_wire_schema"]["sections"]["local_storage_layout"] == {
        "$ref": "#/local_storage_layout",
        "scope": "local thread/session persistence and adjacent artifacts",
    }
    assert report["coverage"]["local_storage_layout"]["scope_complete"] is True


def test_local_storage_semantic_mutation_fails_visible() -> None:
    mutated = SOURCES["rollout_file_name"].replace(
        "let (thread_id, rollout_id) = ids.split_once('_').unwrap_or((ids, ids));\n",
        "",
    )
    diagnostics = DiagnosticCollector()
    result = LocalStorageExtractor().extract(
        _snapshot({"rollout_file_name": mutated}), diagnostics
    )

    assert not result.semantic_complete
    assert "LOCAL_STORAGE_SEMANTIC_MARKER_MISSING" in {
        item.code for item in diagnostics.values()
    }


def test_local_storage_missing_source_fails_without_guessing_paths() -> None:
    diagnostics = DiagnosticCollector()
    result = LocalStorageExtractor().extract(
        _snapshot(omit={"state_sqlite"}), diagnostics
    )

    assert not result.semantic_complete
    assert result.data == {}
    assert "LOCAL_STORAGE_SOURCE_UNAVAILABLE" in {
        item.code for item in diagnostics.values()
    }


def test_source_registry_declares_local_storage_sources() -> None:
    registry = _registry()
    for source_id in SOURCE_IDS.values():
        spec = registry.get(source_id)
        assert "extractor.local_storage" in spec.extractor_ids
        assert "local_storage_layout" in spec.roles
