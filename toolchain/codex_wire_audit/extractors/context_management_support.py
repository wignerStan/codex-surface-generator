"""Shared constants and source/evidence helpers for Context Management extraction."""
from __future__ import annotations

import re
from typing import Any, Iterable

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot

EXTRACTOR_ID = "extractor.context_management"
SCHEMA_VERSION = "1.0.0"

SOURCE_IDS = {
    "activation": "source_spec.extra.context_management_activation",
    "tools": "source_spec.extra.history_notes_tools",
    "backend": "source_spec.extra.history_notes_backend",
    "extension": "source_spec.extra.history_notes_extension",
    "new_context": "source_spec.extra.new_context_window",
    "new_context_spec": "source_spec.extra.new_context_window_spec",
    "compact": "source_spec.extra.compact_token_budget",
    "provider_info": "source_spec.base.provider_info",
    "config_toml": "source_spec.extra.config_toml",
    "feature_configs": "source_spec.extra.feature_configs",
    "feature_registry": "source_spec.extra.feature_registry",
    "model_protocol": "source_spec.extra.model_protocol",
    "provider_runtime": "source_spec.extra.provider_runtime",
    "activation_tests": "source_spec.extra.context_management_tests",
    "history_notes_tests": "source_spec.extra.history_notes_tests",
}

ESSENTIAL = (
    "activation",
    "tools",
    "backend",
    "extension",
    "new_context",
    "new_context_spec",
    "compact",
    "provider_info",
    "config_toml",
    "feature_configs",
    "feature_registry",
    "model_protocol",
)

EXPECTED_ROUTES = (
    "alpha/history/v2/list_windows",
    "alpha/history/v2/list_items",
    "alpha/history/v2/read_item",
    "alpha/history/v2/search_contents",
    "alpha/notes/v2/list_files_by_prefix",
    "alpha/notes/v2/read_file",
    "alpha/notes/v2/search_contents",
    "alpha/notes/v2/append_to_file",
    "alpha/notes/v2/write_file",
    "alpha/notes/v2/thread_hint",
)
EXPECTED_ENCRYPTED_ROUTES = (
    "alpha/history/v2/search_contents",
    "alpha/notes/v2/search_contents",
    "alpha/notes/v2/append_to_file",
    "alpha/notes/v2/write_file",
)


def source(snapshot: SourceSnapshot, key: str) -> SourceFile | None:
    return snapshot.files.get(SOURCE_IDS[key])


def text(snapshot: SourceSnapshot, key: str) -> str:
    item = source(snapshot, key)
    return item.text if item else ""


def evidence(item: SourceFile | None, symbol: str) -> dict[str, Any]:
    if item is None:
        return {"available": False, "symbol": symbol}
    return {
        "available": True,
        "source_spec_id": item.spec_id,
        "source_path": item.selected_path,
        "source_sha256": item.content_sha256,
        "symbol": symbol,
    }


def emit_missing(
    diagnostics: DiagnosticCollector,
    *,
    code: str,
    message: str,
    source_file: SourceFile | None,
    entity: str,
    details: dict[str, Any] | None = None,
) -> None:
    diagnostics.emit(
        code=code,
        severity="error",
        category="context_management",
        message=message,
        extractor_id=EXTRACTOR_ID,
        entity_id=entity,
        source_refs=(source_file.selected_path,) if source_file else (),
        details=details or {},
        recoverable=False,
        strict_failure=True,
    )


def require_tokens(
    diagnostics: DiagnosticCollector,
    source_file: SourceFile | None,
    tokens: Iterable[tuple[str, str]],
    *,
    entity_prefix: str,
) -> bool:
    if source_file is None:
        return False
    ok = True
    for code, token in tokens:
        if token not in source_file.text:
            ok = False
            emit_missing(
                diagnostics,
                code=code,
                message=f"Required context-management source token is missing: {token}",
                source_file=source_file,
                entity=f"{entity_prefix}.{token}",
                details={"token": token},
            )
    return ok


def extract_routes(tools: str, extension: str, backend: str) -> tuple[str, ...]:
    values = set(
        re.findall(
            r'"(alpha/(?:history|notes)/v2/[a-z_]+)"',
            tools + "\n" + extension + "\n" + backend,
        )
    )
    return tuple(sorted(values))
