"""Local Git-tree and explicit worktree source provider."""

from __future__ import annotations

import os
from pathlib import Path

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile
from ..source_registry import SourceRegistry
from .common import (
    SnapshotLoadResult,
    SourceLoadError,
    finalize_snapshot,
    find_repo_root,
    run_git_bytes,
    run_git_text,
    validate_symbols,
)


def load_repo_snapshot(
    registry: SourceRegistry,
    root: str | os.PathLike[str],
    *,
    repository: str,
    requested_ref: str = "HEAD",
    source_commit: str | None = None,
    allow_dirty: bool = False,
    worktree: bool = False,
) -> SnapshotLoadResult:
    repo_root = find_repo_root(root)
    diagnostics = DiagnosticCollector()
    head = run_git_text(repo_root, "rev-parse", "HEAD", check=False)
    treeish = source_commit or (requested_ref if requested_ref not in {"", "main", "HEAD"} else "HEAD")
    resolved = run_git_text(repo_root, "rev-parse", f"{treeish}^{{commit}}", check=False)
    if resolved is None:
        if source_commit:
            raise SourceLoadError(f"cannot resolve local source commit: {source_commit}")
        if not worktree:
            diagnostics.emit(
                code="GIT_METADATA_UNAVAILABLE_FALLING_BACK_TO_WORKTREE",
                severity="warning",
                category="source_identity",
                message="Git metadata was unavailable; source files will be read from the worktree.",
                extractor_id="source_provider.git",
            )
            worktree = True

    dirty_text = run_git_text(
        repo_root,
        "status",
        "--porcelain",
        "--untracked-files=no",
        check=False,
    )
    dirty = bool(dirty_text) if dirty_text is not None else None
    if dirty and worktree and not allow_dirty:
        raise SourceLoadError(
            "repository has tracked modifications; pass --allow-dirty-source with --worktree to audit them"
        )
    if dirty and not worktree:
        diagnostics.emit(
            code="WORKTREE_MODIFICATIONS_IGNORED",
            severity="info",
            category="source_identity",
            message=(
                "Tracked worktree modifications are ignored because source bytes are read "
                "from the immutable Git tree."
            ),
            extractor_id="source_provider.git",
            details={"resolved_commit_sha": resolved},
        )
    if worktree and source_commit and head and resolved and resolved != head:
        raise SourceLoadError("--worktree cannot be combined with a non-HEAD --source-commit")

    files: dict[str, SourceFile] = {}
    unavailable: dict[str, str] = {}
    for spec in registry.specs:
        selected: SourceFile | None = None
        errors: list[str] = []
        for index, relative in enumerate(spec.path_candidates):
            raw: bytes | None
            blob_sha: str | None = None
            if worktree:
                path = repo_root / relative
                try:
                    raw = path.read_bytes()
                except FileNotFoundError:
                    raw = None
                except OSError as error:
                    raw = None
                    errors.append(f"{relative}: {error}")
            else:
                assert resolved is not None
                raw = run_git_bytes(repo_root, "show", f"{resolved}:{relative}", check=False)
                blob_sha = run_git_text(repo_root, "rev-parse", f"{resolved}:{relative}", check=False)
            if raw is None:
                continue
            try:
                selected = SourceFile.create(
                    spec=spec,
                    selected_path=relative,
                    raw_bytes=raw,
                    repository_blob_sha=blob_sha,
                    path_candidate_index=index,
                )
            except ValueError as error:
                errors.append(str(error))
                continue
            break
        if selected is None:
            reason = "; ".join(errors) or "none of the path candidates exist"
            unavailable[spec.id] = reason
            if not spec.required:
                diagnostics.emit(
                    code="SOURCE_OPTIONAL_UNAVAILABLE",
                    severity="warning",
                    category="source_coverage",
                    message=f"Optional source is unavailable: {spec.id}",
                    extractor_id="source_registry",
                    entity_id=spec.id,
                    details={"path_candidates": list(spec.path_candidates), "reason": reason},
                )
            continue
        files[spec.id] = selected
        validate_symbols(selected, spec, diagnostics)
        if selected.path_candidate_index:
            diagnostics.emit(
                code="SOURCE_PATH_FALLBACK_SELECTED",
                severity="info",
                category="source_evolution",
                message=f"A fallback path candidate was selected for {spec.id}.",
                extractor_id="source_registry",
                entity_id=spec.id,
                source_refs=(selected.selected_path,),
                details={
                    "selected_path": selected.selected_path,
                    "candidate_index": selected.path_candidate_index,
                    "primary_path": spec.primary_path,
                },
            )

    commit_date = (
        run_git_text(repo_root, "show", "-s", "--format=%cI", resolved, check=False)
        if resolved
        else None
    )
    commit_message = (
        run_git_text(repo_root, "show", "-s", "--format=%s", resolved, check=False)
        if resolved
        else "working tree source snapshot"
    )
    return finalize_snapshot(
        registry=registry,
        files=files,
        unavailable=unavailable,
        source_mode="worktree" if worktree else "git_tree",
        repository=repository,
        requested_ref=source_commit or requested_ref,
        resolved_commit_sha=resolved,
        dirty=dirty,
        commit_date=commit_date,
        commit_message=commit_message,
        diagnostics=diagnostics,
    )
