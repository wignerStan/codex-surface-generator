#!/usr/bin/env python3
"""Build the deterministic v16 Context Management delivery."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile

ROOT = Path(__file__).resolve().parent.parent
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
RELEASE = "codex-wire-audit-v16-context-management"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_name(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(name) and not name.startswith(("/", "\\")) and "\\" not in name and not any(part in {"", ".", ".."} for part in p.parts)


def zip_files(destination: Path, entries: list[tuple[str, Path]]) -> dict[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    seen: set[str] = set()
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, source in sorted(entries):
            if name in seen or not safe_name(name):
                raise ValueError(f"unsafe/duplicate zip name: {name}")
            seen.add(name)
            info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
            info.create_system = 3
            mode = 0o755 if source.read_bytes()[:2] == b"#!" else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    temporary.replace(destination)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError(f"CRC failure: {destination}")
        return {"members": len(archive.infolist()), "uncompressed_bytes": sum(i.file_size for i in archive.infolist())}


def source_entries() -> list[tuple[str, Path]]:
    excluded_parts = {".git", ".pytest_cache", "__pycache__", "build", "dist", "release_v13", "release_v16", "ci-out", "htmlcov"}
    entries: list[tuple[str, Path]] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(ROOT)
        if any(part in excluded_parts or part.endswith(".egg-info") for part in relative.parts):
            continue
        if relative.name in {".coverage", "codex_wire_audit_v16_validation.json", "codex_wire_audit_v16_validation.txt"}:
            continue
        entries.append((f"codex_wire_audit_v16_source/{relative.as_posix()}", path))
    return entries


def copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o644)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", required=True)
    parser.add_argument("--validation-json", required=True)
    parser.add_argument("--validation-log", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--v15-delivery-dir")
    parser.add_argument("--review-diff")
    args = parser.parse_args()

    validation = json.loads(Path(args.validation_json).read_text(encoding="utf-8"))
    if validation.get("status") != "passed":
        raise SystemExit("refusing to build v16 delivery from failed validation")
    output = Path(args.output_dir).resolve()
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    # Primary source/installable artifacts.
    for artifact in sorted(Path(args.dist_dir).resolve().iterdir()):
        if artifact.is_file() and artifact.suffix in {".whl", ".gz"}:
            copy(artifact, output / artifact.name)
    copy(ROOT / "README_CODEX_WIRE_AUDIT_V16.md", output / "README_CODEX_WIRE_AUDIT_V16.md")
    copy(ROOT / "CODEX_WIRE_AUDIT_V16_RELEASE_NOTES.md", output / "CODEX_WIRE_AUDIT_V16_RELEASE_NOTES.md")
    copy(Path(args.validation_json), output / "codex_wire_audit_v16_validation.json")
    copy(Path(args.validation_log), output / "codex_wire_audit_v16_validation.txt")
    if args.review_diff and Path(args.review_diff).is_file():
        copy(Path(args.review_diff), output / "codex_wire_audit_v15_to_v16_context_management.diff")

    # Deterministic source zip.
    source_zip = output / "codex_wire_audit_v16_source.zip"
    source_zip_info = zip_files(source_zip, source_entries())

    # Closed Context Management machine container from package data.
    context_root = ROOT / "codex_wire_audit" / "context_management_container_data"
    context_zip = output / "codex_wire_audit_v16_context_management_container.zip"
    context_zip_info = zip_files(context_zip, [(p.name, p) for p in context_root.glob("*.json")])

    # Preserve supplied v15 companion evidence without pretending it is active package code.
    carried: list[str] = []
    if args.v15_delivery_dir:
        v15 = Path(args.v15_delivery_dir).resolve()
        for name in ("codex_wire_audit_v15_guardian_history_container.zip", "codex_wire_audit_v15_alpha_history_notes_container.zip"):
            source = v15 / name
            if source.is_file():
                destination = output / f"legacy_{name}"
                copy(source, destination)
                carried.append(destination.name)

    boundaries = {
        "format": "codex-wire-audit-v16-boundaries/v1",
        "reviewed_codex_commit": validation.get("reviewed_codex_commit"),
        "active_package_integration": {
            "context_management": True,
            "guardian_v15_companion_promoted_to_active_package": False,
            "auth_v14_claims_recovered_from_supplied_package": False,
        },
        "carried_v15_companion_artifacts": carried,
        "notes": [
            "The supplied v15 delivery contained companion Guardian/alpha JSON artifacts, while its installable wheel/sdist still identified package 7.0.0 and lacked those active extractors.",
            "v16 repairs that split for Context Management only; it does not fabricate missing v14/v15 active implementation that was absent from the supplied source package.",
        ],
    }
    write_json(output / "codex_wire_audit_v16_boundaries.json", boundaries)

    # Inventory before building aggregate archives.
    inventory = {
        "format": "codex-wire-audit-v16-artifact-inventory/v1",
        "release": RELEASE,
        "reviewed_codex_commit": validation.get("reviewed_codex_commit"),
        "source_zip": source_zip_info,
        "context_container_zip": context_zip_info,
        "artifacts": [],
    }
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name not in {"codex_wire_audit_v16_artifact_inventory.json", "SHA256SUMS_codex_wire_audit_v16.txt", "codex_wire_audit_v16_machine_assets.zip", "codex_wire_audit_v16_context_management_bundle.zip"}:
            inventory["artifacts"].append({"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)})
    write_json(output / "codex_wire_audit_v16_artifact_inventory.json", inventory)

    # Compact machine assets.
    machine_names = {
        "codex_wire_audit_v16_context_management_container.zip",
        "codex_wire_audit_v16_validation.json",
        "codex_wire_audit_v16_boundaries.json",
        "codex_wire_audit_v16_artifact_inventory.json",
        "CODEX_WIRE_AUDIT_V16_RELEASE_NOTES.md",
        "README_CODEX_WIRE_AUDIT_V16.md",
    }
    machine_zip = output / "codex_wire_audit_v16_machine_assets.zip"
    zip_files(machine_zip, [(p.name, p) for p in output.iterdir() if p.name in machine_names])

    # Checksums cover all standalone deliverables except the final aggregate ZIP and checksum file itself.
    checksum_candidates = [p for p in sorted(output.iterdir()) if p.is_file() and p.name not in {"SHA256SUMS_codex_wire_audit_v16.txt", "codex_wire_audit_v16_context_management_bundle.zip"}]
    checksum_text = "".join(f"{sha256(path)}  {path.name}\n" for path in checksum_candidates)
    (output / "SHA256SUMS_codex_wire_audit_v16.txt").write_text(checksum_text, encoding="utf-8")

    # Final aggregate bundle contains every standalone artifact including checksums, but not itself.
    bundle = output / "codex_wire_audit_v16_context_management_bundle.zip"
    bundle_info = zip_files(bundle, [(p.name, p) for p in output.iterdir() if p.is_file() and p != bundle])
    (output / "codex_wire_audit_v16_context_management_bundle.zip.sha256").write_text(f"{sha256(bundle)}  {bundle.name}\n", encoding="utf-8")

    summary = {
        "release": RELEASE,
        "status": "passed",
        "reviewed_codex_commit": validation.get("reviewed_codex_commit"),
        "bundle": {"name": bundle.name, "sha256": sha256(bundle), **bundle_info},
        "machine_assets": {"name": machine_zip.name, "sha256": sha256(machine_zip)},
        "context_container": {"name": context_zip.name, "sha256": sha256(context_zip)},
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
