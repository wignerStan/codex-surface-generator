"""Offline, confined source-byte revalidation and bounded secret screening."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping

from .canonical import canonical_json_bytes

_TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def scan_secrets(value: Any) -> None:
    """Heuristic credential rejection, not a claim of exhaustive DLP."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            scan_secrets(key)
            scan_secrets(child)
    elif isinstance(value, list):
        for child in value:
            scan_secrets(child)
    elif isinstance(value, str) and any(p.search(value) for p in _TOKEN_PATTERNS):
        raise ValueError("secret-like material in canonical contract")


def relative_source_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value or not path.parts or "\x00" in value or "\\" in value
        or ":" in value or path.is_absolute() or str(path) != value
        or any(part in {".", ".."} or part.lower() == ".git" for part in path.parts)
    ):
        raise ValueError("source path must be a normalized, repository-relative file")
    return path


def load_document(path: str | Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    canonical_json_bytes(value)
    return value


def revalidate_source_bytes(model: Mapping[str, Any], source_root: Path) -> int:
    """Compare exact source bytes with the manifest; never fetch URLs or refs."""
    root = source_root.resolve(strict=True)
    records = model["source_snapshot"]["files"].values()
    source_digest = hashlib.sha256()
    count = 0
    for record in sorted(records, key=lambda row: row["path"]):
        relative = relative_source_path(record["path"])
        candidate = root
        for part in relative.parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ValueError("symlink source evidence is not allowed")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError("source evidence must be a regular file")
        raw = resolved.read_bytes()
        blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if (
            len(raw) != record["byte_length"]
            or hashlib.sha256(raw).hexdigest() != record["sha256"]
            or blob_sha != record["git_blob_sha"]
        ):
            raise ValueError(f"source evidence byte mismatch: {relative}")
        source_digest.update(str(relative).encode("utf-8") + b"\0" + raw + b"\0")
        count += 1
    if source_digest.hexdigest() != model["source_revision"]["source_set_sha256"]:
        raise ValueError("source-set digest mismatch")
    return count
