"""Bounded and collision-safe source archive provider."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
import unicodedata
import zipfile

from ..models import SourceRevision
from ..source_registry import SourceRegistry
from .common import SnapshotLoadResult, SourceLoadError, find_repo_root
from .git import load_repo_snapshot


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_members: int = 50_000
    max_total_uncompressed_bytes: int = 1_073_741_824
    max_file_bytes: int = 67_108_864
    max_compression_ratio: float = 200.0
    max_depth: int = 40
    max_name_length: int = 512


def _validated_archive_name(
    name: str,
    *,
    limits: ArchiveLimits,
    seen: set[str],
    casefolded: dict[str, str],
    normalized: dict[str, str],
) -> PurePosixPath:
    if not name or len(name) > limits.max_name_length:
        raise SourceLoadError(f"archive member name is empty or exceeds limit: {name[:80]!r}")
    posix = PurePosixPath(name)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise SourceLoadError(f"unsafe archive member path: {name}")
    if len(posix.parts) > limits.max_depth:
        raise SourceLoadError(f"archive member depth exceeds limit: {name}")
    canonical = posix.as_posix().rstrip("/")
    if not canonical:
        raise SourceLoadError(f"unsafe archive member path: {name}")
    if canonical in seen:
        raise SourceLoadError(f"duplicate archive member path: {canonical}")
    folded = canonical.casefold()
    if folded in casefolded and casefolded[folded] != canonical:
        raise SourceLoadError(
            f"case-folded archive path collision: {casefolded[folded]!r} and {canonical!r}"
        )
    nfc_name = unicodedata.normalize("NFC", canonical)
    if nfc_name in normalized and normalized[nfc_name] != canonical:
        raise SourceLoadError(
            f"Unicode-normalized archive path collision: {normalized[nfc_name]!r} and {canonical!r}"
        )
    seen.add(canonical)
    casefolded[folded] = canonical
    normalized[nfc_name] = canonical
    return posix


def safe_extract_archive(
    archive_path: str | os.PathLike[str],
    destination: str | os.PathLike[str],
    *,
    limits: ArchiveLimits | None = None,
) -> Path:
    limits = limits or ArchiveLimits()
    archive = Path(archive_path)
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    casefolded: dict[str, str] = {}
    normalized: dict[str, str] = {}
    total = 0

    def target_for(name: str) -> Path:
        posix = _validated_archive_name(
            name,
            limits=limits,
            seen=seen,
            casefolded=casefolded,
            normalized=normalized,
        )
        target = root.joinpath(*posix.parts)
        resolved = target.resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError as error:
            raise SourceLoadError(f"archive member escapes destination: {name}") from error
        return target

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            members = handle.infolist()
            if len(members) > limits.max_members:
                raise SourceLoadError("archive member count exceeds configured limit")
            for member in members:
                target = target_for(member.filename)
                unix_type = (member.external_attr >> 16) & 0o170000
                if unix_type == 0o120000:
                    raise SourceLoadError(f"archive links are not accepted: {member.filename}")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.file_size > limits.max_file_bytes:
                    raise SourceLoadError(f"archive member exceeds file-size limit: {member.filename}")
                total += member.file_size
                if total > limits.max_total_uncompressed_bytes:
                    raise SourceLoadError("archive total uncompressed size exceeds configured limit")
                ratio = member.file_size / max(member.compress_size, 1)
                if ratio > limits.max_compression_ratio:
                    raise SourceLoadError(f"archive compression ratio exceeds limit: {member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as handle:
            members = handle.getmembers()
            if len(members) > limits.max_members:
                raise SourceLoadError("archive member count exceeds configured limit")
            for member in members:
                target = target_for(member.name)
                if member.issym() or member.islnk():
                    raise SourceLoadError(f"archive links are not accepted: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise SourceLoadError(f"archive special files are not accepted: {member.name}")
                if member.size > limits.max_file_bytes:
                    raise SourceLoadError(f"archive member exceeds file-size limit: {member.name}")
                total += member.size
                if total > limits.max_total_uncompressed_bytes:
                    raise SourceLoadError("archive total uncompressed size exceeds configured limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = handle.extractfile(member)
                if source is None:
                    raise SourceLoadError(f"cannot read archive member: {member.name}")
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    else:
        raise SourceLoadError(f"unsupported source archive: {archive}")
    return find_repo_root(root)


def load_archive_snapshot(
    registry: SourceRegistry,
    archive_path: str | os.PathLike[str],
    *,
    repository: str,
    requested_ref: str,
    source_commit: str | None = None,
    limits: ArchiveLimits | None = None,
) -> SnapshotLoadResult:
    with tempfile.TemporaryDirectory(prefix="codex-wire-audit-source-") as temporary:
        repo_root = safe_extract_archive(archive_path, temporary, limits=limits)
        result = load_repo_snapshot(
            registry,
            repo_root,
            repository=repository,
            requested_ref=requested_ref,
            source_commit=None,
            allow_dirty=True,
            worktree=True,
        )
        revision = result.snapshot.revision
        result.snapshot.revision = SourceRevision(
            source_mode="source_archive",
            repository=repository,
            requested_ref=source_commit or requested_ref,
            resolved_commit_sha=source_commit or revision.resolved_commit_sha,
            source_set_sha256=revision.source_set_sha256,
            dirty=None,
            commit_date=revision.commit_date,
            commit_message=revision.commit_message or "offline source archive",
        )
        result.commit["sha"] = source_commit or revision.resolved_commit_sha or (
            "source-set-" + revision.source_set_sha256
        )
        result.commit["source_set_sha256"] = revision.source_set_sha256
        return result
