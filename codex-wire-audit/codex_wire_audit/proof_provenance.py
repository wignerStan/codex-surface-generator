from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import subprocess
from typing import Iterable

from .proof_diagnostics import ProofDiagnostic


@dataclass(frozen=True, slots=True)
class FileProvenance:
    path: str
    exists: bool
    tracked: bool
    worktree_sha256: str | None
    committed_sha256: str | None
    bytes_equal_commit: bool
    status: str | None


@dataclass(frozen=True, slots=True)
class ProvenanceResult:
    mode: str
    repository: str
    requested_ref: str
    requested_sha: str | None
    head_sha: str | None
    bytes_basis: str
    commit_binding_verified: bool
    dirty: bool
    authoritative_source_sha256: str | None
    unique_blob_count: int
    files: tuple[FileProvenance, ...]
    complete: bool
    diagnostics: tuple[ProofDiagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["diagnostics"] = [item.to_dict() for item in self.diagnostics]
        return value


def _git(repo: Path, *args: str, check: bool = True) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and process.returncode:
        raise RuntimeError(process.stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} failed")
    return process.stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_paths(repo: Path, paths: Iterable[str | Path]) -> tuple[str, ...]:
    result: set[str] = set()
    root = repo.resolve()
    for raw in paths:
        path = Path(raw)
        absolute = path if path.is_absolute() else root / path
        try:
            relative = absolute.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        result.add(relative.as_posix())
    return tuple(sorted(result))


def verify_git_source(
    repository: str | Path,
    requested_ref: str = "HEAD",
    loaded_paths: Iterable[str | Path] = (),
    *,
    mode: str = "worktree",
    allow_dirty: bool = False,
) -> ProvenanceResult:
    repo = Path(repository).resolve()
    diagnostics: list[ProofDiagnostic] = []
    paths = _normalize_paths(repo, loaded_paths)
    try:
        head = _git(repo, "rev-parse", "HEAD").decode().strip()
        requested = _git(repo, "rev-parse", f"{requested_ref}^{{commit}}").decode().strip()
    except Exception as error:
        diagnostic = ProofDiagnostic(code="GIT_IDENTITY_UNRESOLVED", message=str(error), source_id=str(repo))
        return ProvenanceResult(mode, str(repo), requested_ref, None, None, mode, False, False, None, 0, (), False, (diagnostic,))

    if mode == "worktree" and requested != head:
        diagnostics.append(ProofDiagnostic(
            code="WORKTREE_REF_NOT_HEAD",
            message=f"worktree bytes cannot be attributed to requested commit {requested}; HEAD is {head}",
            source_id=str(repo),
            details={"requested_sha": requested, "head_sha": head},
        ))

    status_by_path: dict[str, str] = {}
    if paths:
        status_output = _git(repo, "status", "--porcelain=v1", "--untracked-files=all", "--", *paths).decode("utf-8", "replace")
        for line in status_output.splitlines():
            if len(line) < 4:
                continue
            status = line[:2]
            path = line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            status_by_path[path] = status

    if not paths:
        diagnostics.append(ProofDiagnostic(
            code="NO_SOURCE_PATHS_FOR_PROVENANCE",
            message="no loaded source paths were available for byte provenance",
            source_id=str(repo),
        ))

    file_results: list[FileProvenance] = []
    source_digest = hashlib.sha256()
    digested_paths = 0
    for path in paths:
        absolute = repo / path
        exists = absolute.is_file()
        worktree_bytes = absolute.read_bytes() if exists else None
        tracked = _git(repo, "ls-files", "--error-unmatch", "--", path, check=False) != b""
        committed = None
        if tracked:
            process = subprocess.run(
                ["git", "-C", str(repo), "show", f"{requested}:{path}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if process.returncode == 0:
                committed = process.stdout
        equal = committed is not None and (mode == "commit" or (worktree_bytes is not None and worktree_bytes == committed))
        status = status_by_path.get(path)
        authoritative_bytes = committed if mode == "commit" else worktree_bytes
        if authoritative_bytes is not None:
            source_digest.update(path.encode("utf-8"))
            source_digest.update(b"\0")
            source_digest.update(authoritative_bytes)
            source_digest.update(b"\0")
            digested_paths += 1
        file_results.append(FileProvenance(
            path=path,
            exists=exists,
            tracked=tracked,
            worktree_sha256=_sha(worktree_bytes) if worktree_bytes is not None else None,
            committed_sha256=_sha(committed) if committed is not None else None,
            bytes_equal_commit=equal,
            status=status,
        ))
        if not exists:
            diagnostics.append(ProofDiagnostic(code="LOADED_SOURCE_MISSING", message=f"source path is missing: {path}", source_id=path))
        if mode == "worktree" and not tracked:
            diagnostics.append(ProofDiagnostic(code="LOADED_SOURCE_UNTRACKED", message=f"loaded source is untracked: {path}", source_id=path))
        if mode == "worktree" and status:
            diagnostics.append(ProofDiagnostic(
                code="LOADED_SOURCE_DIRTY",
                message=f"loaded source has Git status {status!r}: {path}",
                source_id=path,
                strict_failure=not allow_dirty,
                recoverable=allow_dirty,
            ))
        if mode == "worktree" and tracked and not equal:
            diagnostics.append(ProofDiagnostic(code="WORKTREE_BYTES_NOT_COMMIT_BYTES", message=f"worktree bytes differ from {requested}: {path}", source_id=path))

    dirty = bool(status_by_path)
    commit_bound = requested == head if mode == "worktree" else True
    if mode == "worktree":
        commit_bound = commit_bound and all(item.tracked and item.bytes_equal_commit for item in file_results)
        if dirty and not allow_dirty:
            commit_bound = False
    complete = not any(item.strict_failure for item in diagnostics)
    if mode == "commit" and any(item.committed_sha256 is None for item in file_results):
        complete = False
    return ProvenanceResult(
        mode=mode,
        repository=str(repo),
        requested_ref=requested_ref,
        requested_sha=requested,
        head_sha=head,
        bytes_basis="worktree" if mode == "worktree" else "git_object_database",
        commit_binding_verified=commit_bound,
        dirty=dirty,
        authoritative_source_sha256=source_digest.hexdigest() if digested_paths else None,
        unique_blob_count=digested_paths,
        files=tuple(file_results),
        complete=complete,
        diagnostics=tuple(diagnostics),
    )
