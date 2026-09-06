"""Verification helpers for the packaged context-management reference container."""
from __future__ import annotations

import hashlib
from importlib import resources
import json
from pathlib import PurePosixPath
from typing import Any, Mapping

from .release_spec_validation import safe_relative_path

FORMAT = "codex-wire-audit-context-management-container-index/v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _walk(node: Any, prefix: str = "") -> dict[str, Any]:
    observed: dict[str, Any] = {}
    for item in node.iterdir():
        relative = f"{prefix}{item.name}"
        if item.is_dir():
            observed.update(_walk(item, f"{relative}/"))
        elif item.is_file():
            observed[relative] = item
    return observed


def _index() -> tuple[Any, dict[str, Any]]:
    root = resources.files(__package__).joinpath("context_management_container_data")
    index = json.loads(root.joinpath("index.json").read_text(encoding="utf-8"))
    if not isinstance(index, dict) or index.get("format") != FORMAT:
        raise ValueError("unsupported context-management container index format")
    integrity = index.get("integrity")
    payload = dict(index)
    payload.pop("integrity", None)
    claimed = integrity.get("canonical_payload_sha256") if isinstance(integrity, Mapping) else None
    if claimed != hashlib.sha256(_canonical(payload)).hexdigest():
        raise ValueError("context-management index integrity mismatch")
    return root, index


def load_context_management_reference() -> dict[str, Any]:
    root, _ = _index()
    value = json.loads(root.joinpath("catalog.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("context-management catalog is not an object")
    return value


def verify_context_management_container() -> dict[str, Any]:
    root, index = _index()
    rows = index.get("members")
    if not isinstance(rows, list):
        raise ValueError("context-management index members must be an array")
    declared: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("path"), str):
            raise ValueError("context-management index contains an invalid member")
        name = row["path"]
        if name in declared or not safe_relative_path(name) or PurePosixPath(name).suffix != ".json":
            raise ValueError(f"context-management member is duplicate or unsafe: {name}")
        declared[name] = row
    observed = _walk(root)
    observed.pop("index.json", None)
    if set(declared) != set(observed):
        raise ValueError("context-management container member set mismatch")
    for name, row in sorted(declared.items()):
        data = observed[name].read_bytes()
        if len(data) != row.get("size"):
            raise ValueError(f"context-management container size mismatch: {name}")
        if hashlib.sha256(data).hexdigest() != row.get("sha256"):
            raise ValueError(f"context-management container hash mismatch: {name}")
        try:
            json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"context-management member is not valid JSON: {name}") from error
    return {
        "format": index["format"],
        "reviewed_commit": index.get("reviewed_commit"),
        "member_count": len(declared),
        "members": sorted(declared),
        "index_integrity": index["integrity"],
    }
