"""Concurrent GitHub source provider isolated behind the snapshot interface."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSpec
from ..source_registry import SourceRegistry
from .common import SnapshotLoadResult, finalize_snapshot, validate_symbols


def load_github_snapshot(
    registry: SourceRegistry,
    *,
    repository: str,
    requested_ref: str,
    api_base: str,
    token: str | None,
    legacy_base: Any,
    max_workers: int = 8,
) -> SnapshotLoadResult:
    diagnostics = DiagnosticCollector()
    commit = legacy_base.resolve_commit(repository, requested_ref, api_base, token)
    resolved = str(commit["sha"])

    def fetch_spec(spec: SourceSpec) -> tuple[SourceSpec, SourceFile | None, str | None]:
        errors: list[str] = []
        for index, path in enumerate(spec.path_candidates):
            try:
                text = legacy_base.fetch_file(repository, resolved, path, api_base, token)
            except Exception as error:  # Legacy adapter owns the concrete HTTP exception type.
                errors.append(f"{path}: {error}")
                continue
            try:
                return (
                    spec,
                    SourceFile.create(
                        spec=spec,
                        selected_path=path,
                        raw_bytes=text.encode("utf-8"),
                        path_candidate_index=index,
                    ),
                    None,
                )
            except ValueError as error:
                errors.append(str(error))
        return spec, None, "; ".join(errors) or "fetch failed"

    files: dict[str, SourceFile] = {}
    unavailable: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 32))) as executor:
        futures = {executor.submit(fetch_spec, spec): spec for spec in registry.specs}
        for future in as_completed(futures):
            spec, source_file, error = future.result()
            if source_file is None:
                unavailable[spec.id] = error or "fetch failed"
                if not spec.required:
                    diagnostics.emit(
                        code="SOURCE_OPTIONAL_UNAVAILABLE",
                        severity="warning",
                        category="source_coverage",
                        message=f"Optional source is unavailable: {spec.id}",
                        extractor_id="source_provider.github",
                        entity_id=spec.id,
                        details={"path_candidates": list(spec.path_candidates), "reason": error},
                    )
                continue
            files[spec.id] = source_file
            validate_symbols(source_file, spec, diagnostics)
            if source_file.path_candidate_index:
                diagnostics.emit(
                    code="SOURCE_PATH_FALLBACK_SELECTED",
                    severity="info",
                    category="source_evolution",
                    message=f"A fallback path candidate was selected for {spec.id}.",
                    extractor_id="source_registry",
                    entity_id=spec.id,
                    source_refs=(source_file.selected_path,),
                    details={
                        "selected_path": source_file.selected_path,
                        "candidate_index": source_file.path_candidate_index,
                        "primary_path": spec.primary_path,
                    },
                )
    return finalize_snapshot(
        registry=registry,
        files=files,
        unavailable=unavailable,
        source_mode="github",
        repository=repository,
        requested_ref=requested_ref,
        resolved_commit_sha=resolved,
        dirty=False,
        commit_date=commit.get("date"),
        commit_message=commit.get("message"),
        diagnostics=diagnostics,
    )
