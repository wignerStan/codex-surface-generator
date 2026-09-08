"""Source-derived Codex local thread/session storage layout."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping, MutableMapping

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot
from .local_storage_catalog import build_local_storage_contract
from .registry import ExtractorResult, register_extractor

EXTRACTOR_ID = "extractor.local_storage"
SCHEMA_VERSION = "1.0.0"

SOURCE_IDS = {
    "thread_types": "source_spec.extra.thread_store_types",
    "rollout_file_name": "source_spec.extra.rollout_file_name",
    "rollout_recorder": "source_spec.extra.rollout_recorder",
    "rollout_constants": "source_spec.extra.rollout_layout_constants",
    "rollout_compression": "source_spec.extra.rollout_compression",
    "thread_revert": "source_spec.extra.thread_revert",
    "thread_archive": "source_spec.extra.thread_archive",
    "writer_lock": "source_spec.extra.thread_writer_lock",
    "state_sqlite": "source_spec.extra.state_sqlite",
    "state_threads": "source_spec.extra.state_threads",
    "shell_snapshot": "source_spec.extra.shell_snapshot",
    "inline_visualization": "source_spec.extra.inline_visualization",
}

# A tuple entry is either a required token or alternatives where one must match.
MARKERS: Mapping[str, tuple[str | tuple[str, ...], ...]] = {
    "thread_types": (
        "pub session_id: SessionId",
        "pub thread_id: ThreadId",
        "pub forked_from_id: Option<ThreadId>",
    ),
    "rollout_file_name": (
        "self.thread_id == self.rollout_id",
        "split_once('_').unwrap_or((ids, ids))",
        ("rollout-{timestamp}-{}_{}.jsonl", "distinct rollout ID after the stable"),
    ),
    "rollout_recorder": (
        "OffsetDateTime::now_local()",
        "dir.push(SESSIONS_SUBDIR)",
        "rollout_id_override.unwrap_or(thread_id)",
        "RolloutFileName::new(timestamp, thread_id, rollout_id)",
        "with_rollout_id",
    ),
    "rollout_constants": ("SESSIONS_SUBDIR", "ARCHIVED_SESSIONS_SUBDIR"),
    "rollout_compression": ("COMPRESSED_SUFFIX", "plain `.jsonl` and `.jsonl.zst`"),
    "thread_revert": (
        ("let rollout_id = ThreadId::new();", "let rollout_id = RolloutId::new();"),
        "with_session_id(source_meta.session_id)",
        "with_rollout_id(rollout_id)",
        "replace_rollout_path_if_current",
        "Old rollouts stay intact",
    ),
    "thread_archive": (
        "ARCHIVED_SESSIONS_SUBDIR",
        "archive_folder.join(&file_name)",
        "mark_archived",
    ),
    "writer_lock": (
        "WRITER_LOCK_DIR",
        'format!("{thread_id}.lock")',
        "COORDINATION_LOCK_FILE",
    ),
    "state_sqlite": (
        "STATE_DB_FILENAME",
        "THREAD_HISTORY_DB_FILENAME",
        "sqlite_home",
    ),
    "state_threads": (
        "find_rollout_path_by_id",
        "replace_rollout_path_if_current",
        "UPDATE threads",
    ),
    "shell_snapshot": (
        "SNAPSHOT_DIR",
        'format!("{session_id}.{nonce}.{extension}")',
        'format!("{session_id}.tmp-{nonce}")',
    ),
    "inline_visualization": (
        'join("visualizations")',
        'format("%Y/%m/%d")',
        "join(&thread_id)",
        'join("visualization-viewers")',
    ),
}


def _evidence(source: SourceFile, markers: tuple[str | tuple[str, ...], ...]) -> dict[str, Any]:
    return {
        "available": True,
        "source_spec_id": source.spec_id,
        "source_path": source.selected_path,
        "source_sha256": source.content_sha256,
        "symbols": [
            " | ".join(marker) if isinstance(marker, tuple) else marker
            for marker in markers
        ],
    }


def _error(
    diagnostics: DiagnosticCollector,
    code: str,
    message: str,
    entity_id: str,
    source: SourceFile | None = None,
    **details: Any,
) -> None:
    diagnostics.emit(
        code=code,
        severity="error",
        category="local_storage",
        message=message,
        extractor_id=EXTRACTOR_ID,
        entity_id=entity_id,
        source_refs=(source.selected_path,) if source else (),
        details=details,
        recoverable=False,
        strict_failure=True,
    )


def _missing_markers(
    source: SourceFile,
    markers: tuple[str | tuple[str, ...], ...],
) -> list[list[str]]:
    missing: list[list[str]] = []
    for marker in markers:
        alternatives = marker if isinstance(marker, tuple) else (marker,)
        if not any(value in source.text for value in alternatives):
            missing.append(list(alternatives))
    return missing


def _constant(source: SourceFile, name: str) -> str | None:
    pattern = rf'(?:pub(?:\([^)]*\))?\s+)?const\s+{name}\s*:\s*&str\s*=\s*"([^"]+)"'
    match = re.search(pattern, source.text)
    return match.group(1) if match else None


def _sqlite_filenames(source: SourceFile) -> dict[str, str]:
    pairs = re.findall(
        r'const\s+([A-Z0-9_]+_DB_FILENAME)\s*:\s*&str\s*=\s*"([^"]+)"',
        source.text,
    )
    return {
        name.lower().removesuffix("_db_filename"): value
        for name, value in pairs
    }


class LocalStorageExtractor:
    extractor_id = EXTRACTOR_ID
    source_spec_ids = tuple(SOURCE_IDS.values())

    def extract(
        self,
        snapshot: SourceSnapshot,
        diagnostics: DiagnosticCollector,
    ) -> ExtractorResult:
        sources: dict[str, SourceFile] = {}
        complete = True
        for key, source_id in SOURCE_IDS.items():
            source = snapshot.files.get(source_id)
            if source is None:
                complete = False
                _error(
                    diagnostics,
                    "LOCAL_STORAGE_SOURCE_UNAVAILABLE",
                    f"Local-storage source is unavailable: {source_id}",
                    f"local_storage.source.{key}",
                    source_spec_id=source_id,
                )
                continue
            sources[key] = source
            missing = _missing_markers(source, MARKERS[key])
            if missing:
                complete = False
                _error(
                    diagnostics,
                    "LOCAL_STORAGE_SEMANTIC_MARKER_MISSING",
                    f"Local-storage semantics changed in {source.selected_path}.",
                    f"local_storage.source.{key}",
                    source,
                    missing_markers=missing,
                )

        if len(sources) != len(SOURCE_IDS):
            return ExtractorResult(
                EXTRACTOR_ID,
                SCHEMA_VERSION,
                {},
                False,
                tuple(SOURCE_IDS.values()),
            )

        constants = {
            "sessions": _constant(sources["rollout_constants"], "SESSIONS_SUBDIR"),
            "archived_sessions": _constant(
                sources["rollout_constants"], "ARCHIVED_SESSIONS_SUBDIR"
            ),
            "compressed_suffix": _constant(
                sources["rollout_compression"], "COMPRESSED_SUFFIX"
            ),
            "snapshots": _constant(sources["shell_snapshot"], "SNAPSHOT_DIR"),
            "locks": _constant(sources["writer_lock"], "WRITER_LOCK_DIR"),
            "coordination_lock": _constant(
                sources["writer_lock"], "COORDINATION_LOCK_FILE"
            ),
        }
        unresolved = sorted(key for key, value in constants.items() if value is None)
        if unresolved:
            _error(
                diagnostics,
                "LOCAL_STORAGE_CONSTANT_UNRESOLVED",
                "One or more local-storage path constants could not be resolved.",
                "local_storage.constants",
                unresolved=unresolved,
            )
            return ExtractorResult(
                EXTRACTOR_ID,
                SCHEMA_VERSION,
                {},
                False,
                tuple(SOURCE_IDS.values()),
            )

        databases = _sqlite_filenames(sources["state_sqlite"])
        expected_databases = {
            "logs",
            "goals",
            "memories",
            "queue",
            "state",
            "thread_history",
        }
        missing_databases = sorted(expected_databases - set(databases))
        if missing_databases:
            complete = False
            _error(
                diagnostics,
                "LOCAL_STORAGE_DATABASE_FILENAME_UNRESOLVED",
                "One or more runtime SQLite filenames could not be resolved.",
                "local_storage.sqlite_databases",
                sources["state_sqlite"],
                missing=missing_databases,
            )

        resolved_constants = {
            key: str(value) for key, value in constants.items() if value is not None
        }
        evidence = {
            key: _evidence(source, MARKERS[key]) for key, source in sources.items()
        }
        data = build_local_storage_contract(
            source_revision=snapshot.revision.to_dict(),
            constants=resolved_constants,
            databases=databases,
            evidence=evidence,
            semantic_complete=complete,
            missing_database_constants=missing_databases,
        )
        return ExtractorResult(
            EXTRACTOR_ID,
            SCHEMA_VERSION,
            data,
            complete,
            tuple(SOURCE_IDS.values()),
        )


@register_extractor(EXTRACTOR_ID)
def _factory() -> LocalStorageExtractor:
    return LocalStorageExtractor()


def apply_local_storage_overlay(
    report: MutableMapping[str, Any],
    result: ExtractorResult,
) -> None:
    """Attach local persistence semantics to the report and full schema."""
    if not result.data:
        return
    report["local_storage_layout"] = {
        "authoritative": True,
        "extractor_id": result.extractor_id,
        "schema_version": result.schema_version,
        **copy.deepcopy(result.data),
    }
    full_schema = report.get("full_wire_schema")
    if isinstance(full_schema, MutableMapping):
        sections = full_schema.setdefault("sections", {})
        if isinstance(sections, MutableMapping):
            sections["local_storage_layout"] = {
                "$ref": "#/local_storage_layout",
                "scope": "local thread/session persistence and adjacent artifacts",
            }
        full_schema["local_storage_semantic_digest"] = result.data.get(
            "semantic_digest"
        )
    coverage = report.get("coverage")
    if isinstance(coverage, MutableMapping):
        coverage["local_storage_layout"] = copy.deepcopy(
            result.data.get("coverage") or {}
        )
