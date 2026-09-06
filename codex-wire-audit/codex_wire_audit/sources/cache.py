"""Compatibility reader for the v10 immutable source cache."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile
from ..source_registry import SourceRegistry
from .common import SnapshotLoadResult, SourceLoadError, finalize_snapshot, validate_symbols


def _legacy_cache_path(cache_dir: Path, repository: str, sha: str, path: str) -> Path:
    digest = hashlib.sha256(f"{repository}\0{sha}\0{path}".encode("utf-8")).hexdigest()
    return cache_dir / "source-v1" / digest[:2] / f"{digest}.rs"


def load_cache_snapshot(
    registry: SourceRegistry,
    *,
    cache_dir: str | os.PathLike[str],
    repository: str,
    commit_sha: str,
) -> SnapshotLoadResult:
    if len(commit_sha) != 40 or any(char not in "0123456789abcdefABCDEF" for char in commit_sha):
        raise SourceLoadError("cache mode requires a 40-character commit SHA")
    diagnostics = DiagnosticCollector()
    root = Path(cache_dir).expanduser()
    files: dict[str, SourceFile] = {}
    unavailable: dict[str, str] = {}
    for spec in registry.specs:
        selected: SourceFile | None = None
        for index, relative in enumerate(spec.path_candidates):
            path = _legacy_cache_path(root, repository, commit_sha, relative)
            try:
                raw = path.read_bytes()
            except (FileNotFoundError, OSError):
                continue
            try:
                selected = SourceFile.create(
                    spec=spec,
                    selected_path=relative,
                    raw_bytes=raw,
                    path_candidate_index=index,
                )
            except ValueError:
                continue
            break
        if selected is None:
            unavailable[spec.id] = "not present in legacy immutable source cache"
            if not spec.required:
                diagnostics.emit(
                    code="SOURCE_OPTIONAL_UNAVAILABLE",
                    severity="warning",
                    category="source_coverage",
                    message=f"Optional source is unavailable in cache: {spec.id}",
                    extractor_id="source_provider.cache",
                    entity_id=spec.id,
                )
            continue
        files[spec.id] = selected
        validate_symbols(selected, spec, diagnostics)
    return finalize_snapshot(
        registry=registry,
        files=files,
        unavailable=unavailable,
        source_mode="cache_only",
        repository=repository,
        requested_ref=commit_sha,
        resolved_commit_sha=commit_sha,
        dirty=False,
        commit_date=None,
        commit_message="cached immutable source set",
        diagnostics=diagnostics,
    )
