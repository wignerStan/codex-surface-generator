"""Strict stdlib verification of the packaged metadata-history container."""
from __future__ import annotations

import hashlib
from importlib import resources
import json
from pathlib import PurePosixPath
from typing import Any, Mapping

from .release_spec_validation import safe_relative_path

FORMAT = "codex-wire-audit-metadata-history-container/v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _walk(node: Any, prefix: str = "") -> dict[str, Any]:
    observed: dict[str, Any] = {}
    for item in node.iterdir():
        relative = f"{prefix}{item.name}"
        if item.is_dir():
            observed.update(_walk(item, f"{relative}/"))
        elif item.is_file():
            observed[relative] = item
    return observed


def verify_metadata_history_container() -> dict[str, Any]:
    root = resources.files(__package__).joinpath("metadata_history_container_data")
    index = json.loads(root.joinpath("index.json").read_text(encoding="utf-8"))
    if not isinstance(index, dict) or index.get("format") != FORMAT:
        raise ValueError("unsupported metadata-history container format")
    integrity = index.get("integrity")
    payload = dict(index)
    payload.pop("integrity", None)
    claimed = integrity.get("canonical_payload_sha256") if isinstance(integrity, Mapping) else None
    if claimed != hashlib.sha256(_canonical(payload)).hexdigest():
        raise ValueError("metadata-history index integrity mismatch")
    rows = index.get("members")
    if not isinstance(rows, list):
        raise ValueError("metadata-history index members must be an array")
    declared: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
            raise ValueError("metadata-history index contains an invalid member")
        relative = row["path"]
        if (
            relative in declared
            or not safe_relative_path(relative)
            or PurePosixPath(relative).suffix != ".json"
        ):
            raise ValueError(f"metadata-history member is duplicate or unsafe: {relative}")
        declared[relative] = row
    observed = _walk(root)
    observed.pop("index.json", None)
    if set(declared) != set(observed):
        raise ValueError("metadata-history container member set mismatch")
    for relative, row in sorted(declared.items()):
        data = observed[relative].read_bytes()
        if len(data) != row.get("byte_length"):
            raise ValueError(f"metadata-history member size mismatch: {relative}")
        if hashlib.sha256(data).hexdigest() != row.get("sha256"):
            raise ValueError(f"metadata-history member hash mismatch: {relative}")
        try:
            json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"metadata-history member is not valid JSON: {relative}") from error
    return {
        "format": index["format"],
        "catalog_sha256": index.get("catalog_sha256"),
        "member_count": len(declared),
        "key_count": index.get("key_count"),
        "version_count": index.get("version_count"),
        "index_integrity": integrity,
    }
