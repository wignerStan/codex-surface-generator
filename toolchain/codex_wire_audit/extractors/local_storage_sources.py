"""Source parsing and fail-visible evidence checks for local storage."""
from __future__ import annotations

import re
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot
from .local_storage_specs import EXTRACTOR_ID, SOURCE_IDS, _DB_SPECS, _REQUIRED_MARKERS

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
