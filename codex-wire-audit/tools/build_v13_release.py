#!/usr/bin/env python3
"""Build and verify the deterministic Codex wire-audit v13 release set."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from typing import Any
import uuid
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCE_DATE_EPOCH = 315532800  # 1980-01-01T00:00:00Z, valid for ZIP.
RELEASE_VERSION = "7.0.0"
RELEASE_NAME = "codex-wire-audit-v13"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

_EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "ci-out",
    "dist",
    "htmlcov",
    "release_v13",
}
_EXCLUDED_NAMES = {
    ".coverage",
    "coverage-v13.json",
    "bundle-manifest.json",
    "dist-manifest.json",
    "SHA256SUMS.txt",
    "SHA256SUMS.v13",
    "codex_wire_audit_v13_validation.json",
    "codex_wire_audit_v13_validation.txt",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=True, sort_keys=True) + "\n").encode(
        "ascii"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    temporary.replace(path)


def _file_mode(path: Path) -> int:
    try:
        executable = path.read_bytes()[:2] == b"#!"
    except OSError:
        executable = False
    return 0o755 if executable else 0o644


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(_file_mode(destination))


def _is_excluded(relative: Path) -> bool:
    if relative.name in _EXCLUDED_NAMES:
        return True
    if any(part in _EXCLUDED_PARTS for part in relative.parts):
        return True
    return any(part.endswith(".egg-info") for part in relative.parts)


def _source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if _is_excluded(relative):
            continue
        files.append(relative)
    return sorted(files, key=lambda item: item.as_posix())


_RECURSIVE_RELEASE_EXCLUSIONS = {
    "codex_wire_audit_v13_ci_history_bundle.zip",
    "codex_wire_audit_v13_ci_history_bundle.zip.sha256",
    "codex_wire_audit_v13_machine_history_assets.zip",
    "codex_wire_audit_v13_machine_history_assets.zip.sha256",
    "codex_wire_audit_v13_artifact_inventory.json",
    "SHA256SUMS_codex_wire_audit_v13.txt",
    "codex_wire_audit_v13_release_build.json",
}


def _recursive_release_asset(name: str) -> bool:
    return name not in _RECURSIVE_RELEASE_EXCLUSIONS


def _machine_release_asset(name: str) -> bool:
    if not _recursive_release_asset(name):
        return False
    if name.endswith((".whl", ".tar.gz")):
        return False
    if name.endswith((".whl.sha256", ".tar.gz.sha256")):
        return False
    if name.endswith("_metadata_history.diff"):
        return False
    if name == "codex_wire_audit_v13_validation.txt":
        return False
    return True


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not name.startswith(("/", "\\"))
        and "\\" not in name
        and not any(part in {"", ".", ".."} for part in path.parts)
        and not (path.parts and ":" in path.parts[0])
    )


def _zip_tree(source: Path, destination: Path, *, prefix: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        seen: set[str] = set()
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source).as_posix()
            name = f"{prefix.rstrip('/')}/{relative}"
            if not _safe_member_name(name) or name in seen:
                raise ValueError(f"unsafe or duplicate ZIP member: {name}")
            seen.add(name)
            info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | _file_mode(path)) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    temporary.chmod(0o644)
    temporary.replace(destination)


def _verify_zip(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate ZIP member in {path.name}")
        for name in names:
            if not _safe_member_name(name):
                raise ValueError(f"unsafe ZIP member {name!r} in {path.name}")
        bad = archive.testzip()
        if bad is not None:
            raise ValueError(f"ZIP CRC failure in {path.name}: {bad}")
        uncompressed = sum(item.file_size for item in archive.infolist())
    return {"members": len(names), "uncompressed_bytes": uncompressed}


def _integrity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "algorithm": "sha256",
        "canonical_payload_sha256": _sha256_bytes(_canonical_json(value)),
    }


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _copy_source_tree(source_root: Path, destination: Path) -> list[Path]:
    copied: list[Path] = []
    for relative in _source_files(source_root):
        _copy_file(source_root / relative, destination / relative)
        copied.append(relative)
    return copied


def _file_record(path: Path, relative: str, role: str) -> dict[str, Any]:
    return {
        "path": relative,
        "role": role,
        "byte_length": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _source_role(relative: Path) -> str:
    suffix = relative.suffix.lower()
    parts = set(relative.parts)
    if relative.parts and relative.parts[0] in {"tests", "tools", "scripts"}:
        return "test_or_release_source"
    if suffix == ".py":
        return "implementation_source"
    if suffix == ".json" and "schema" in relative.as_posix():
        return "json_schema"
    if suffix == ".json":
        return "machine_contract_or_fixture"
    if suffix in {".md", ".txt"}:
        return "documentation_or_evidence"
    if suffix in {".yml", ".yaml"} or ".github" in parts:
        return "ci_configuration"
    return "source_asset"


def _write_source_manifest(bundle_root: Path) -> dict[str, Any]:
    records = [
        _file_record(path, path.relative_to(bundle_root).as_posix(), _source_role(path.relative_to(bundle_root)))
        for path in sorted(bundle_root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
        and path.name not in {"bundle-manifest.json", "SHA256SUMS.v13"}
    ]
    manifest: dict[str, Any] = {
        "format": "codex-wire-audit-bundle-manifest/v13",
        "release": RELEASE_NAME,
        "generator_version": RELEASE_VERSION,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "closed_over": "all regular files except this manifest and SHA256SUMS.v13",
        "file_count": len(records),
        "files": records,
    }
    manifest["integrity"] = _integrity(manifest)
    _atomic_write(bundle_root / "bundle-manifest.json", _pretty_json(manifest))

    checksum_paths = [
        path
        for path in sorted(bundle_root.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file() and path.name != "SHA256SUMS.v13"
    ]
    lines = [
        f"{_sha256(path)}  {path.relative_to(bundle_root).as_posix()}"
        for path in checksum_paths
    ]
    _atomic_write(bundle_root / "SHA256SUMS.v13", ("\n".join(lines) + "\n").encode("utf-8"))
    return manifest


def _verify_source_manifest(bundle_root: Path) -> dict[str, Any]:
    manifest = _read_json(bundle_root / "bundle-manifest.json")
    integrity = manifest.pop("integrity")
    expected = _integrity(manifest)
    manifest["integrity"] = integrity
    if integrity != expected:
        raise ValueError("bundle manifest integrity mismatch")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ValueError("bundle manifest files must be an array")
    declared = {record["path"]: record for record in records}
    actual = {
        path.relative_to(bundle_root).as_posix(): path
        for path in bundle_root.rglob("*")
        if path.is_file()
        and path.name not in {"bundle-manifest.json", "SHA256SUMS.v13"}
    }
    if set(declared) != set(actual):
        raise ValueError("bundle manifest member set mismatch")
    for relative, path in actual.items():
        record = declared[relative]
        if record["byte_length"] != path.stat().st_size or record["sha256"] != _sha256(path):
            raise ValueError(f"bundle manifest mismatch: {relative}")

    sums: dict[str, str] = {}
    for line in (bundle_root / "SHA256SUMS.v13").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        if relative in sums:
            raise ValueError(f"duplicate checksum path: {relative}")
        sums[relative] = digest
    expected_sum_paths = {
        path.relative_to(bundle_root).as_posix(): path
        for path in bundle_root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS.v13"
    }
    if set(sums) != set(expected_sum_paths):
        raise ValueError("SHA256SUMS.v13 member set mismatch")
    for relative, path in expected_sum_paths.items():
        if sums[relative] != _sha256(path):
            raise ValueError(f"checksum mismatch: {relative}")
    return {"manifest_files": len(records), "checksum_files": len(sums)}


def _history_summary(catalog: dict[str, Any]) -> dict[str, Any]:
    return {
        "catalog_sha256": catalog["integrity"]["canonical_payload_sha256"],
        "keys": len(catalog["keys"]),
        "states": len(catalog["states"]),
        "versions": len(catalog["versions"]),
        "transitions": len(catalog["transitions"]),
        "current_version_ref": catalog["current_version_ref"],
        "reviewed_through": catalog["reviewed_through"],
    }


def _build_release_manifest(
    validation: dict[str, Any],
    catalog: dict[str, Any],
    distributions: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "format": "codex-wire-audit-release/v13",
        "release": RELEASE_NAME,
        "generator_version": RELEASE_VERSION,
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "status": "passed" if validation.get("status") == "passed" else "failed",
        "validation_evidence": {
            "format": validation.get("format"),
            "integrity": validation.get("evidence_integrity"),
        },
        "metadata_history": _history_summary(catalog),
        "tests": validation.get("tests", {}),
        "coverage": validation.get("coverage", {}).get("aggregate", {}),
        "maintainability": validation.get("maintainability", {}).get("guardrails", {}),
        "distributions": distributions,
        "boundaries": validation.get("boundaries", {}),
        "artifacts": {
            "history_catalog": "codex_wire_audit_v13_metadata_key_history.json",
            "history_container": "codex_wire_audit_v13_metadata_history_container.zip",
            "machine_assets": "codex_wire_audit_v13_machine_history_assets.zip",
            "complete_bundle": "codex_wire_audit_v13_ci_history_bundle.zip",
            "inventory": "codex_wire_audit_v13_artifact_inventory.json",
            "checksums": "SHA256SUMS_codex_wire_audit_v13.txt",
        },
    }
    manifest["integrity"] = _integrity(manifest)
    return manifest


def _build_sbom(
    validation: dict[str, Any],
    catalog: dict[str, Any],
    distributions: list[dict[str, Any]],
) -> dict[str, Any]:
    catalog_digest = catalog["integrity"]["canonical_payload_sha256"]
    namespace = uuid.UUID("d5ea28d2-c1bd-5b0f-a5d8-e25ecfcb485a")
    serial = uuid.uuid5(namespace, f"{RELEASE_NAME}:{catalog_digest}")
    components: list[dict[str, Any]] = [
        {
            "type": "application",
            "bom-ref": "pkg:pypi/codex-wire-audit@7.0.0",
            "name": "codex-wire-audit",
            "version": RELEASE_VERSION,
            "purl": "pkg:pypi/codex-wire-audit@7.0.0",
            "properties": [
                {"name": "codex-wire-audit:metadata-history-sha256", "value": catalog_digest},
                {"name": "codex-wire-audit:reviewed-commit", "value": catalog["reviewed_through"]["commit_sha"]},
            ],
        },
        {
            "type": "library",
            "bom-ref": "pkg:pypi/jsonschema@range%3A%3E%3D4.23%2C%3C5",
            "name": "jsonschema",
            "version": ">=4.23,<5",
            "purl": "pkg:pypi/jsonschema",
        },
        {
            "type": "library",
            "bom-ref": "pkg:pypi/referencing@range%3A%3E%3D0.35%2C%3C1",
            "name": "referencing",
            "version": ">=0.35,<1",
            "purl": "pkg:pypi/referencing",
        },
    ]
    for artifact in distributions:
        components.append(
            {
                "type": "file",
                "bom-ref": f"artifact:{artifact['name']}",
                "name": artifact["name"],
                "hashes": [{"alg": "SHA-256", "content": artifact["sha256"]}],
                "properties": [
                    {"name": "codex-wire-audit:byte-length", "value": str(artifact["byte_length"])}
                ],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": components[0],
            "properties": [
                {"name": "codex-wire-audit:validation-sha256", "value": validation["evidence_integrity"]["canonical_payload_sha256"]},
                {"name": "codex-wire-audit:source-date-epoch", "value": str(SOURCE_DATE_EPOCH)},
            ],
        },
        "components": components[1:],
        "dependencies": [
            {
                "ref": "pkg:pypi/codex-wire-audit@7.0.0",
                "dependsOn": [
                    "pkg:pypi/jsonschema@range%3A%3E%3D4.23%2C%3C5",
                    "pkg:pypi/referencing@range%3A%3E%3D0.35%2C%3C1",
                ],
            }
        ],
    }


def _write_history_convenience(source_root: Path, output_dir: Path) -> list[Path]:
    from codex_wire_audit.metadata_history import (
        diff_versions,
        history_key_view,
        load_history_catalog,
    )
    from codex_wire_audit.metadata_history_container import (
        build_container_zip,
        verify_multi_json_container,
    )

    catalog = load_history_catalog(
        source_root / "codex_wire_audit/metadata_history_data/metadata-key-history.v1.json"
    )
    raw_catalog = output_dir / "codex_wire_audit_v13_metadata_key_history.json"
    _atomic_write(raw_catalog, _pretty_json(catalog))

    timeline = output_dir / "codex_wire_audit_v13_code_mode_tool_names.timeline.json"
    _atomic_write(
        timeline,
        _pretty_json(history_key_view(catalog, "key.turn_metadata.code_mode_tool_names")),
    )
    history_diff = output_dir / "codex_wire_audit_v13_code_mode_introduction_to_removal.diff.json"
    _atomic_write(
        history_diff,
        _pretty_json(
            diff_versions(
                catalog,
                "version.2026-07-25.code_mode_metadata_introduced",
                "version.2026-08-07.code_mode_metadata_removed",
            )
        ),
    )

    packaged_container = source_root / "codex_wire_audit/metadata_history_container_data"
    diagnostics = verify_multi_json_container(packaged_container)
    if diagnostics:
        raise ValueError(f"checked-in metadata-history container invalid: {diagnostics}")
    history_zip = output_dir / "codex_wire_audit_v13_metadata_history_container.zip"
    build_container_zip(packaged_container, history_zip, source_date_epoch=SOURCE_DATE_EPOCH)
    return [raw_catalog, timeline, history_diff, history_zip]


def _machine_asset_paths(source_root: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    fixed = [
        "codex_wire_audit/coverage_profiles.v1.json",
        "codex_wire_audit/coverage_profiles.v2.json",
        "codex_wire_audit_v13_maintainability_metrics.json",
        "codex_wire_audit_v13_coverage_ratchet.json",
        "CODEX_WIRE_AUDIT_V13_METRICS.md",
        "CODEX_WIRE_AUDIT_V13_RELEASE_NOTES.md",
        "README_CODEX_WIRE_AUDIT_V13.md",
        "docs/METADATA_HISTORY.md",
        ".github/workflows/machine-proof.yml",
        "ci/constraints.txt",
    ]
    for relative in fixed:
        pairs.append((source_root / relative, Path(relative)))
    for root_name in [
        "codex_wire_audit/proof_schema_templates",
        "codex_wire_audit/schema_templates",
        "codex_wire_audit/metadata_history_data",
        "codex_wire_audit/metadata_history_container_data",
    ]:
        root = source_root / root_name
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_file():
                pairs.append((path, path.relative_to(source_root)))
    return pairs


def _build_machine_assets(
    source_root: Path,
    release_files: Path,
    output_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    staging = output_dir / ".machine-assets-stage"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for source, relative in _machine_asset_paths(source_root):
        _copy_file(source, staging / relative)
    for path in sorted(release_files.iterdir(), key=lambda item: item.name):
        if path.is_file() and _machine_release_asset(path.name):
            _copy_file(path, staging / "release" / path.name)
    manifest = _write_source_manifest(staging)
    details = _verify_source_manifest(staging)
    archive = output_dir / "codex_wire_audit_v13_machine_history_assets.zip"
    _zip_tree(staging, archive, prefix="codex_wire_audit_v13_machine_history_assets")
    zip_details = _verify_zip(archive)
    shutil.rmtree(staging)
    return archive, {"manifest": manifest["integrity"], **details, **zip_details}


def _build_full_bundle(
    source_root: Path,
    release_files: Path,
    output_dir: Path,
) -> tuple[Path, dict[str, Any]]:
    staging = output_dir / ".full-bundle-stage"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    copied = _copy_source_tree(source_root, staging)
    for path in sorted(release_files.iterdir(), key=lambda item: item.name):
        if path.is_file() and _recursive_release_asset(path.name):
            _copy_file(path, staging / "release_v13" / path.name)
    manifest = _write_source_manifest(staging)
    details = _verify_source_manifest(staging)
    archive = output_dir / "codex_wire_audit_v13_ci_history_bundle.zip"
    _zip_tree(staging, archive, prefix="codex_wire_audit_v13")
    zip_details = _verify_zip(archive)
    shutil.rmtree(staging)
    return archive, {
        "source_files_copied": len(copied),
        "manifest": manifest["integrity"],
        **details,
        **zip_details,
    }


def _text_files(root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for relative in _source_files(root):
        path = root / relative
        try:
            result[relative.as_posix()] = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            continue
    return result


def _build_review_diff(baseline_root: Path, source_root: Path, destination: Path) -> None:
    before = _text_files(baseline_root)
    after = _text_files(source_root)
    chunks: list[str] = []
    for relative in sorted(set(before) | set(after)):
        old = before.get(relative, [])
        new = after.get(relative, [])
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"v12/{relative}",
                tofile=f"v13/{relative}",
                lineterm="\n",
            )
        )
    _atomic_write(destination, "".join(chunks).encode("utf-8"))


def _artifact_role(name: str) -> str:
    if name.endswith(".whl") or name.endswith(".tar.gz"):
        return "installable_distribution"
    if name.endswith(".zip"):
        return "bundle_or_container"
    if name.endswith(".schema.json"):
        return "json_schema"
    if name.endswith(".json"):
        return "machine_evidence"
    if name.endswith(".md") or name.endswith(".txt"):
        return "documentation_or_validation"
    if name.endswith(".diff"):
        return "review_diff"
    if name.endswith(".sha256"):
        return "hash_sidecar"
    return "release_asset"


def _write_sidecar(path: Path) -> Path:
    sidecar = path.with_name(path.name + ".sha256")
    _atomic_write(sidecar, f"{_sha256(path)}  {path.name}\n".encode("ascii"))
    return sidecar


def _verify_inventory(output_dir: Path, inventory: dict[str, Any]) -> None:
    seen: set[str] = set()
    for record in inventory["artifacts"]:
        name = record["name"]
        if name in seen or not _safe_member_name(name):
            raise ValueError(f"duplicate or unsafe artifact name: {name}")
        seen.add(name)
        path = output_dir / name
        if not path.is_file():
            raise ValueError(f"missing release artifact: {name}")
        if path.stat().st_size != record["byte_length"] or _sha256(path) != record["sha256"]:
            raise ValueError(f"release artifact mismatch: {name}")


def build_release(
    *,
    source_root: Path,
    dist_dir: Path,
    validation_json: Path,
    validation_log: Path,
    output_dir: Path,
    baseline_root: Path | None,
) -> dict[str, Any]:
    validation = _read_json(validation_json)
    if validation.get("status") != "passed":
        raise ValueError("release requires a passed validation report")
    expected_validation = validation.get("evidence_integrity", {}).get("canonical_payload_sha256")
    without_integrity = dict(validation)
    without_integrity.pop("evidence_integrity", None)
    if expected_validation != _sha256_bytes(_canonical_json(without_integrity)):
        raise ValueError("validation report integrity mismatch")

    catalog_path = source_root / "codex_wire_audit/metadata_history_data/metadata-key-history.v1.json"
    catalog = _read_json(catalog_path)
    distributions: list[dict[str, Any]] = []
    expected_dist = {
        item["name"]: item
        for item in validation.get("distributions", {}).get("artifacts", [])
    }
    for name in [
        "codex_wire_audit-7.0.0-py3-none-any.whl",
        "codex_wire_audit-7.0.0.tar.gz",
    ]:
        path = dist_dir / name
        if not path.is_file():
            raise ValueError(f"missing validated distribution: {path}")
        record = {
            "name": name,
            "byte_length": path.stat().st_size,
            "sha256": _sha256(path),
        }
        if expected_dist.get(name) != record:
            raise ValueError(f"distribution does not match validation evidence: {name}")
        distributions.append(record)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    release_files = output_dir / ".release-files"
    release_files.mkdir()

    for source, name in [
        (validation_json, "codex_wire_audit_v13_validation.json"),
        (validation_log, "codex_wire_audit_v13_validation.txt"),
    ]:
        _copy_file(source, release_files / name)
    for name in [
        "codex_wire_audit_v13_maintainability_metrics.json",
        "codex_wire_audit_v13_coverage_ratchet.json",
        "CODEX_WIRE_AUDIT_V13_METRICS.md",
        "CODEX_WIRE_AUDIT_V13_RELEASE_NOTES.md",
    ]:
        _copy_file(source_root / name, release_files / name)
    for path in dist_dir.iterdir():
        if path.name in expected_dist:
            _copy_file(path, release_files / path.name)

    history_files = _write_history_convenience(source_root, release_files)
    release_manifest = _build_release_manifest(validation, catalog, distributions)
    _atomic_write(
        release_files / "codex_wire_audit_v13_release.json",
        _pretty_json(release_manifest),
    )
    sbom = _build_sbom(validation, catalog, distributions)
    _atomic_write(release_files / "codex_wire_audit_v13_sbom.cdx.json", _pretty_json(sbom))

    dist_manifest = {
        "distribution_manifest_version": "2.0.0",
        "source_date_epoch": str(SOURCE_DATE_EPOCH),
        "sdist_metadata_normalized": True,
        "artifacts": distributions,
    }
    dist_manifest["integrity"] = _integrity(dist_manifest)
    _atomic_write(release_files / "dist-manifest.json", _pretty_json(dist_manifest))

    if baseline_root is not None:
        review_diff = release_files / "codex_wire_audit_v12_to_v13_metadata_history.diff"
        _build_review_diff(baseline_root, source_root, review_diff)

    for path in sorted(release_files.iterdir(), key=lambda item: item.name):
        if path.is_file() and (
            path.suffix in {".zip", ".whl"} or path.name.endswith(".tar.gz")
        ):
            _write_sidecar(path)

    machine_archive, machine_details = _build_machine_assets(
        source_root, release_files, output_dir
    )
    _copy_file(machine_archive, release_files / machine_archive.name)
    _write_sidecar(release_files / machine_archive.name)

    full_archive, full_details = _build_full_bundle(source_root, release_files, output_dir)
    _copy_file(full_archive, release_files / full_archive.name)
    _write_sidecar(release_files / full_archive.name)

    # Publish standalone files only after all recursively built archives are final.
    for path in sorted(release_files.iterdir(), key=lambda item: item.name):
        if path.is_file():
            _copy_file(path, output_dir / path.name)

    inventory_candidates = [
        path
        for path in sorted(output_dir.iterdir(), key=lambda item: item.name)
        if path.is_file()
        and path.name not in {
            "codex_wire_audit_v13_artifact_inventory.json",
            "SHA256SUMS_codex_wire_audit_v13.txt",
            "codex_wire_audit_v13_release_build.json",
        }
    ]
    inventory: dict[str, Any] = {
        "format": "codex-wire-audit-artifact-inventory/v13",
        "release": RELEASE_NAME,
        "generator_version": RELEASE_VERSION,
        "inventory_excludes": [
            "codex_wire_audit_v13_artifact_inventory.json",
            "SHA256SUMS_codex_wire_audit_v13.txt",
            "codex_wire_audit_v13_release_build.json",
        ],
        "artifacts": [
            {
                "name": path.name,
                "role": _artifact_role(path.name),
                "byte_length": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in inventory_candidates
        ],
    }
    inventory["artifact_count"] = len(inventory["artifacts"])
    inventory["integrity"] = _integrity(inventory)
    inventory_path = output_dir / "codex_wire_audit_v13_artifact_inventory.json"
    _atomic_write(inventory_path, _pretty_json(inventory))
    _verify_inventory(output_dir, inventory)

    checksum_candidates = [
        path
        for path in sorted(output_dir.iterdir(), key=lambda item: item.name)
        if path.is_file()
        and path.name not in {
            "SHA256SUMS_codex_wire_audit_v13.txt",
            "codex_wire_audit_v13_release_build.json",
        }
    ]
    checksum_path = output_dir / "SHA256SUMS_codex_wire_audit_v13.txt"
    _atomic_write(
        checksum_path,
        ("\n".join(f"{_sha256(path)}  {path.name}" for path in checksum_candidates) + "\n").encode(
            "utf-8"
        ),
    )

    # Verify every sidecar and archive after publication.
    for sidecar in output_dir.glob("*.sha256"):
        digest, name = sidecar.read_text(encoding="ascii").strip().split("  ", 1)
        if _sha256(output_dir / name) != digest:
            raise ValueError(f"invalid hash sidecar: {sidecar.name}")
    zip_details = {
        path.name: _verify_zip(path)
        for path in output_dir.glob("*.zip")
    }

    build_summary: dict[str, Any] = {
        "format": "codex-wire-audit-release-build/v13",
        "release": RELEASE_NAME,
        "status": "passed",
        "source_date_epoch": SOURCE_DATE_EPOCH,
        "validation_integrity": validation["evidence_integrity"],
        "metadata_history": _history_summary(catalog),
        "machine_assets": machine_details,
        "complete_bundle": full_details,
        "zip_verification": zip_details,
        "artifact_inventory_sha256": _sha256(inventory_path),
        "checksum_manifest_sha256": _sha256(checksum_path),
        "output_files": len([path for path in output_dir.iterdir() if path.is_file()]) + 1,
    }
    build_summary["integrity"] = _integrity(build_summary)
    build_summary_path = output_dir / "codex_wire_audit_v13_release_build.json"
    _atomic_write(build_summary_path, _pretty_json(build_summary))

    shutil.rmtree(release_files)
    return build_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--validation-json",
        type=Path,
        default=ROOT / "codex_wire_audit_v13_validation.json",
    )
    parser.add_argument(
        "--validation-log",
        type=Path,
        default=ROOT / "codex_wire_audit_v13_validation.txt",
    )
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release_v13")
    parser.add_argument("--baseline-root", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = build_release(
            source_root=args.source_root.resolve(),
            dist_dir=args.dist_dir.resolve(),
            validation_json=args.validation_json.resolve(),
            validation_log=args.validation_log.resolve(),
            output_dir=args.output_dir.resolve(),
            baseline_root=args.baseline_root.resolve() if args.baseline_root else None,
        )
    except Exception as error:
        print(f"codex-wire-audit-v13 release build failed: {type(error).__name__}: {error}")
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
