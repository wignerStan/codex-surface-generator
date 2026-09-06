"""Byte identity, atomic file I/O, and bounded JSON primitives."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any, Mapping

from tools.release_types import DistributionRecord, ReleaseError

DEFAULT_MAX_JSON_BYTES = 32 * 1024 * 1024


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def pretty_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _regular_stat(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseError(f"cannot stat file {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReleaseError(f"expected a regular non-symlink file: {path}")
    return info


def sha256_path(path: Path) -> str:
    _regular_stat(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseError(f"cannot hash file {path}: {error}") from error
    return digest.hexdigest()


def add_integrity(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("integrity", None)
    result = dict(payload)
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": sha256_bytes(canonical_json(payload)),
    }
    return result


def verify_integrity(value: Mapping[str, Any], *, label: str) -> None:
    integrity = value.get("integrity")
    if not isinstance(integrity, Mapping) or integrity.get("algorithm") != "sha256":
        raise ReleaseError(f"{label} integrity metadata is missing or unsupported")
    claimed = integrity.get("canonical_payload_sha256")
    payload = dict(value)
    payload.pop("integrity", None)
    actual = sha256_bytes(canonical_json(payload))
    if claimed != actual:
        raise ReleaseError(f"{label} integrity mismatch")


def load_json_object(path: Path, *, max_bytes: int = DEFAULT_MAX_JSON_BYTES) -> dict[str, Any]:
    info = _regular_stat(path)
    if info.st_size > max_bytes:
        raise ReleaseError(f"JSON object exceeds {max_bytes} bytes: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"expected JSON object: {path}")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def copy_exact(
    source: Path,
    destination: Path,
    expected: DistributionRecord | None = None,
    *,
    mode: int = 0o644,
) -> None:
    source_info = _regular_stat(source)
    source_hash = sha256_path(source)
    if expected is not None and (
        source.name != expected.name
        or source_info.st_size != expected.size
        or source_hash != expected.sha256
    ):
        raise ReleaseError(f"artifact changed after validation: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        temporary.chmod(mode)
        if temporary.stat().st_size != source_info.st_size or sha256_path(temporary) != source_hash:
            raise ReleaseError(f"copied artifact differs from source: {source.name}")
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def file_record(path: Path, *, role: str, provenance: str) -> dict[str, Any]:
    info = _regular_stat(path)
    return {
        "name": path.name,
        "role": role,
        "provenance": provenance,
        "size": info.st_size,
        "sha256": sha256_path(path),
    }


def directory_digest(root: Path) -> dict[str, dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError(f"expected release directory: {root}")
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReleaseError(f"release tree contains a non-regular entry: {relative}")
        records[relative] = {
            "size": info.st_size,
            "mode": f"{stat.S_IMODE(info.st_mode):04o}",
            "sha256": sha256_path(path),
        }
    return records
