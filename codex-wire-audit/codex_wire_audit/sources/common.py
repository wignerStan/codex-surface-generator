"""Shared source-provider primitives and snapshot finalization."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceRevision, SourceSnapshot, SourceSpec
from ..source_registry import SourceRegistry


class SourceLoadError(RuntimeError):
    pass


@dataclass(slots=True)
class SnapshotLoadResult:
    snapshot: SourceSnapshot
    commit: dict[str, Any]
    diagnostics: DiagnosticCollector


def find_repo_root(root: str | os.PathLike[str]) -> Path:
    path = Path(root).resolve()
    if (path / "codex-rs").is_dir():
        return path
    candidates = sorted(
        {candidate.parent for candidate in path.rglob("codex-rs") if candidate.is_dir()},
        key=lambda item: (len(item.parts), str(item)),
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SourceLoadError(f"could not locate codex-rs under {path}")
    raise SourceLoadError(f"multiple possible repository roots under {path}: {candidates}")


def run_git_bytes(root: Path, *args: str, check: bool = True) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if check:
            raise SourceLoadError(f"git invocation failed: {error}") from error
        return None
    if result.returncode != 0:
        if check:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            raise SourceLoadError(f"git {' '.join(args)} failed: {stderr}")
        return None
    return result.stdout


def run_git_text(root: Path, *args: str, check: bool = True) -> str | None:
    value = run_git_bytes(root, *args, check=check)
    return value.decode("utf-8", "replace").strip() if value is not None else None


def validate_symbols(
    source_file: SourceFile,
    spec: SourceSpec,
    diagnostics: DiagnosticCollector,
) -> None:
    missing = [symbol for symbol in spec.expected_symbols if symbol not in source_file.text]
    if missing:
        diagnostics.emit(
            code="SOURCE_EXPECTED_SYMBOL_MISSING",
            severity="warning",
            category="source_coverage",
            message=f"Expected symbols were not found in {source_file.selected_path}.",
            extractor_id="source_registry",
            entity_id=spec.id,
            source_refs=(source_file.selected_path,),
            details={"missing_symbols": missing, "selected_path": source_file.selected_path},
            strict_failure=True,
        )


def finalize_snapshot(
    *,
    registry: SourceRegistry,
    files: dict[str, SourceFile],
    unavailable: dict[str, str],
    source_mode: str,
    repository: str,
    requested_ref: str,
    resolved_commit_sha: str | None,
    dirty: bool | None,
    commit_date: str | None,
    commit_message: str | None,
    diagnostics: DiagnosticCollector,
) -> SnapshotLoadResult:
    for spec in registry.specs:
        if spec.required and spec.id not in files:
            diagnostics.emit(
                code="SOURCE_REQUIRED_UNAVAILABLE",
                severity="error",
                category="source_coverage",
                message=f"Required source is unavailable: {spec.id}",
                extractor_id="source_registry",
                entity_id=spec.id,
                details={
                    "path_candidates": list(spec.path_candidates),
                    "reason": unavailable.get(spec.id),
                },
                recoverable=False,
                strict_failure=True,
            )
    errors = [item.message for item in diagnostics.values() if item.severity == "error"]
    if errors:
        raise SourceLoadError("; ".join(errors))

    source_set_sha256 = SourceSnapshot.digest_files(files)
    revision = SourceRevision(
        source_mode=source_mode,
        repository=repository,
        requested_ref=requested_ref,
        resolved_commit_sha=resolved_commit_sha,
        source_set_sha256=source_set_sha256,
        dirty=dirty,
        commit_date=commit_date,
        commit_message=commit_message,
    )
    snapshot = SourceSnapshot(revision=revision, files=files, unavailable_specs=unavailable)
    commit = {
        "sha": resolved_commit_sha or f"source-set-{source_set_sha256}",
        "date": commit_date,
        "message": commit_message or f"{source_mode} source snapshot",
        "html_url": None,
        "source_set_sha256": source_set_sha256,
    }
    return SnapshotLoadResult(snapshot=snapshot, commit=commit, diagnostics=diagnostics)
