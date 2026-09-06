"""Deterministic split-container writer and verifier for metadata history."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping
import zipfile

from .metadata_history import (
    CONTAINER_FORMAT,
    _canonical_bytes,
    _diagnostic,
    catalog_payload_sha256,
    history_key_view,
    history_version_view,
    validate_history_catalog,
)
from .proof_diagnostics import ProofDiagnostic


def _safe_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    return name or "item"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )


def _member_record(root: Path, path: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "byte_length": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def write_multi_json_container(
    catalog: Mapping[str, Any], output_dir: str | Path
) -> dict[str, Any]:
    """Write a deterministic split container and return its index document."""
    target = Path(output_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{target.name}.", dir=target.parent) as temporary:
        root = Path(temporary) / target.name
        root.mkdir()
        _write_json(root / "catalog.json", catalog)
        keys = catalog.get("keys") if isinstance(catalog.get("keys"), Mapping) else {}
        for key_id in sorted(keys):
            _write_json(
                root / "keys" / f"{_safe_name(key_id)}.json",
                history_key_view(catalog, key_id),
            )
        versions = catalog.get("versions") if isinstance(catalog.get("versions"), Mapping) else {}
        ordered = sorted(
            (item for item in versions.values() if isinstance(item, dict)),
            key=lambda item: item.get("ordinal", -1),
        )
        for version in ordered:
            filename = f"{version['ordinal']:03d}-{_safe_name(version['id'])}.json"
            _write_json(
                root / "versions" / filename,
                history_version_view(catalog, version),
            )
        _write_json(
            root / "transitions.json",
            {
                "format": "codex-wire-audit-metadata-history-transitions/v1",
                "catalog_sha256": catalog_payload_sha256(catalog),
                "transitions": catalog.get("transitions", []),
            },
        )
        members = [_member_record(root, path) for path in sorted(root.rglob("*.json"))]
        index: dict[str, Any] = {
            "format": CONTAINER_FORMAT,
            "catalog_format": catalog.get("format"),
            "catalog_version": catalog.get("catalog_version"),
            "catalog_sha256": catalog_payload_sha256(catalog),
            "current_version_ref": catalog.get("current_version_ref"),
            "key_count": len(keys),
            "version_count": len(versions),
            "members": members,
        }
        index["integrity"] = {
            "algorithm": "sha256",
            "canonical_payload_sha256": hashlib.sha256(_canonical_bytes(index)).hexdigest(),
        }
        _write_json(root / "index.json", index)
        if target.exists():
            shutil.rmtree(target) if target.is_dir() else target.unlink()
        os.replace(root, target)
    return index


def build_container_zip(
    container_dir: str | Path,
    output_path: str | Path,
    *,
    source_date_epoch: int = 315532800,
) -> Path:
    root = Path(container_dir).resolve()
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    moment = datetime.datetime.fromtimestamp(
        max(source_date_epoch, 315532800), tz=datetime.timezone.utc
    )
    timestamp = (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", dir=output.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                info = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                info.create_system = 3
                archive.writestr(
                    info,
                    path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _verify_member(root: Path, member: Any, position: int) -> tuple[str | None, list[ProofDiagnostic]]:
    diagnostics: list[ProofDiagnostic] = []
    if not isinstance(member, dict):
        return None, [_diagnostic("METADATA_HISTORY_CONTAINER_MEMBER_INVALID", f"member {position} is not an object")]
    relative = member.get("path")
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")):
        return None, [_diagnostic("METADATA_HISTORY_CONTAINER_PATH_INVALID", f"member {position} has an invalid path")]
    pure = Path(relative)
    if ".." in pure.parts or "\\" in relative:
        return None, [_diagnostic("METADATA_HISTORY_CONTAINER_PATH_INVALID", f"unsafe member path: {relative}", source_id=relative)]
    path = root / pure
    if not path.is_file():
        return relative, [_diagnostic("METADATA_HISTORY_CONTAINER_MEMBER_MISSING", f"container member is missing: {relative}", source_id=relative)]
    data = path.read_bytes()
    if member.get("byte_length") != len(data):
        diagnostics.append(_diagnostic("METADATA_HISTORY_CONTAINER_MEMBER_SIZE_MISMATCH", f"container member size differs: {relative}", source_id=relative, details={"claimed": member.get("byte_length"), "actual": len(data)}))
    digest = hashlib.sha256(data).hexdigest()
    if member.get("sha256") != digest:
        diagnostics.append(_diagnostic("METADATA_HISTORY_CONTAINER_MEMBER_HASH_MISMATCH", f"container member hash differs: {relative}", source_id=relative, details={"claimed": member.get("sha256"), "actual": digest}))
    return relative, diagnostics


def _verify_catalog(root: Path, index: Mapping[str, Any], schema_path: str | Path | None) -> list[ProofDiagnostic]:
    path = root / "catalog.json"
    if not path.is_file():
        return [_diagnostic("METADATA_HISTORY_CONTAINER_CATALOG_INVALID", "catalog.json is missing", source_id="catalog.json")]
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [_diagnostic("METADATA_HISTORY_CONTAINER_CATALOG_INVALID", str(error), source_id="catalog.json")]
    if not isinstance(catalog, dict):
        return [_diagnostic("METADATA_HISTORY_CONTAINER_CATALOG_INVALID", "catalog.json root must be an object", source_id="catalog.json")]
    diagnostics = validate_history_catalog(catalog, schema_path)
    digest = catalog_payload_sha256(catalog)
    if index.get("catalog_sha256") != digest:
        diagnostics.append(_diagnostic("METADATA_HISTORY_CONTAINER_CATALOG_DIGEST_MISMATCH", "index catalog digest differs from catalog.json", source_id="catalog.json", details={"claimed": index.get("catalog_sha256"), "actual": digest}))
    for count_name, collection in (("key_count", "keys"), ("version_count", "versions")):
        if index.get(count_name) != len(catalog.get(collection, {})):
            diagnostics.append(_diagnostic(f"METADATA_HISTORY_CONTAINER_{count_name.upper()}_MISMATCH", f"index {count_name} differs from catalog.json", source_id="catalog.json"))
    return diagnostics


def verify_multi_json_container(
    container_dir: str | Path,
    *,
    schema_path: str | Path | None = None,
) -> list[ProofDiagnostic]:
    root = Path(container_dir).resolve()
    index_path = root / "index.json"
    if not index_path.is_file():
        return [_diagnostic("METADATA_HISTORY_CONTAINER_INDEX_MISSING", f"metadata-history container has no index.json: {root}", source_id=str(root))]
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [_diagnostic("METADATA_HISTORY_CONTAINER_INDEX_INVALID", str(error), source_id=str(index_path))]
    if not isinstance(index, dict) or index.get("format") != CONTAINER_FORMAT:
        return [_diagnostic("METADATA_HISTORY_CONTAINER_FORMAT_INVALID", "metadata-history container index has an unsupported format", source_id=str(index_path))]
    diagnostics: list[ProofDiagnostic] = []
    integrity = index.get("integrity")
    claimed = integrity.get("canonical_payload_sha256") if isinstance(integrity, dict) else None
    payload = dict(index)
    payload.pop("integrity", None)
    actual = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    if claimed != actual:
        diagnostics.append(_diagnostic("METADATA_HISTORY_CONTAINER_INTEGRITY_MISMATCH", "metadata-history container index digest does not match", source_id=str(index_path), details={"claimed": claimed, "actual": actual}))
    members = index.get("members")
    if not isinstance(members, list):
        return diagnostics + [_diagnostic("METADATA_HISTORY_CONTAINER_MEMBERS_INVALID", "metadata-history container members must be an array", source_id=str(index_path))]
    expected: set[str] = set()
    for position, member in enumerate(members):
        relative, item_diagnostics = _verify_member(root, member, position)
        diagnostics.extend(item_diagnostics)
        if relative is None:
            continue
        if relative in expected:
            diagnostics.append(_diagnostic("METADATA_HISTORY_CONTAINER_DUPLICATE_MEMBER", f"duplicate container member: {relative}", source_id=relative))
        expected.add(relative)
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*.json") if path.is_file() and path.name != "index.json"}
    for extra in sorted(actual_paths - expected):
        diagnostics.append(_diagnostic("METADATA_HISTORY_CONTAINER_UNDECLARED_MEMBER", f"container contains an undeclared JSON member: {extra}", source_id=extra))
    diagnostics.extend(_verify_catalog(root, index, schema_path))
    return diagnostics
