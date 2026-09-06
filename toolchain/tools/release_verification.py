"""Independent verification of an assembled release directory."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence
import zipfile

from codex_wire_audit.release_spec_validation import safe_relative_path
from tools.release_archives import (
    extract_zip_tree, inspect_sdist, inspect_wheel, validate_zip_archive,
    wheel_resource_entries, zip_member_bytes,
)
from tools.release_assembly import validated_distributions
from tools.release_integrity import (
    canonical_json, load_json_object, sha256_bytes, sha256_path, verify_integrity,
)
from tools.release_runtime import (
    build_distributions, installed_wheel_probe, parse_pytest_pass_count, run,
    source_package_probe, validate_sdist_roundtrip,
)
from tools.release_spec import (
    release_spec_sha256, required_package_resources, source_archive_prefix,
    source_snapshot, validate_source_alignment,
)
from tools.release_types import ReleaseError, SourceRecord

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

def _read_checksums(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if (
            not separator
            or name in result
            or not _SHA256.fullmatch(digest)
            or not safe_relative_path(name, top_level=True)
        ):
            raise ReleaseError("checksum manifest is malformed, unsafe, or duplicated")
        result[name] = digest
    return result


def _verify_attested_artifacts(
    output: Path,
    attestation: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> set[str]:
    rows = attestation.get("artifacts")
    if not isinstance(rows, list):
        raise ReleaseError("release attestation artifacts must be an array")
    mandatory = {
        spec["artifacts"][key]
        for key in (
            "wheel", "sdist", "source_zip", "context_container", "validation_json",
            "validation_log", "release_spec_copy", "readme", "release_notes",
        )
    }
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            raise ReleaseError("release attestation contains an invalid artifact record")
        name = row["name"]
        if not safe_relative_path(name, top_level=True) or name in seen:
            raise ReleaseError(f"release attestation contains an unsafe or duplicate artifact: {name}")
        seen.add(name)
        path = output / name
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"attested artifact is missing or non-regular: {name}")
        if path.stat().st_size != row.get("size") or sha256_path(path) != row.get("sha256"):
            raise ReleaseError(f"attested artifact differs: {name}")
        role = row.get("role")
        provenance = row.get("provenance")
        if role == "active_package_container" and provenance != "extracted_from_validated_wheel":
            raise ReleaseError("active package container lacks wheel-derived provenance")
        if role == spec["legacy_evidence"]["role"]:
            if provenance != "external_legacy_input" or not name.startswith(
                spec["legacy_evidence"]["output_prefix"]
            ):
                raise ReleaseError(f"legacy evidence role/provenance mismatch: {name}")
        elif provenance == "external_legacy_input":
            raise ReleaseError(f"external legacy input has an active role: {name}")
        elif role == "review_diff" and provenance != "caller_supplied":
            raise ReleaseError("review diff provenance mismatch")
    if not mandatory.issubset(seen):
        raise ReleaseError(f"release attestation is missing mandatory artifacts: {sorted(mandatory - seen)}")
    return seen


def _verify_inventory(output: Path, spec: Mapping[str, Any]) -> set[str]:
    inventory = load_json_object(output / spec["artifacts"]["artifact_inventory"])
    verify_integrity(inventory, label="artifact inventory")
    rows = inventory.get("artifacts")
    if not isinstance(rows, list):
        raise ReleaseError("artifact inventory artifacts must be an array")
    declared: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            raise ReleaseError("artifact inventory contains an invalid record")
        name = row["name"]
        if not safe_relative_path(name, top_level=True) or name in declared:
            raise ReleaseError(f"artifact inventory contains an unsafe or duplicate name: {name}")
        declared.add(name)
        path = output / name
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"artifact inventory entry is missing or non-regular: {name}")
        if path.stat().st_size != row.get("size") or sha256_path(path) != row.get("sha256"):
            raise ReleaseError(f"artifact inventory mismatch: {name}")
    excluded = {
        spec["artifacts"]["artifact_inventory"],
        spec["artifacts"]["machine_assets"],
        spec["artifacts"]["checksums"],
        spec["artifacts"]["bundle"],
        spec["artifacts"]["bundle_sidecar"],
    }
    expected = {path.name for path in output.iterdir() if path.is_file() and path.name not in excluded}
    if declared != expected:
        raise ReleaseError("artifact inventory does not close over its declared scope")
    return declared


def _source_records(validation: Mapping[str, Any]) -> tuple[list[SourceRecord], str]:
    snapshot = validation.get("source_snapshot")
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("files"), list):
        raise ReleaseError("validation source snapshot is malformed")
    rows = snapshot["files"]
    records = [SourceRecord.from_mapping(row) for row in rows if isinstance(row, Mapping)]
    if len(records) != len(rows) or snapshot.get("file_count") != len(records):
        raise ReleaseError("validation source snapshot count mismatch")
    expected_digest = snapshot.get("digest")
    if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
        raise ReleaseError("validation source snapshot digest is malformed")
    return records, expected_digest


def _verify_source_zip(
    output: Path,
    validation: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> tuple[list[SourceRecord], str]:
    records, expected_digest = _source_records(validation)
    prefix = source_archive_prefix(spec)
    expected_names = {f"{prefix}/{record.path}" for record in records}
    source_zip = output / spec["artifacts"]["source_zip"]
    infos = validate_zip_archive(source_zip, expected_names=expected_names)
    digest = hashlib.sha256()
    with zipfile.ZipFile(source_zip) as archive:
        for record in records:
            name = f"{prefix}/{record.path}"
            data = archive.read(name)
            if len(data) != record.size or sha256_bytes(data) != record.sha256:
                raise ReleaseError(f"source ZIP byte mismatch: {record.path}")
            mode = (infos[name].external_attr >> 16) & 0o7777
            if mode != record.mode:
                raise ReleaseError(f"source ZIP mode mismatch: {record.path}")
            digest.update(record.path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(f"{mode:04o}".encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
    if digest.hexdigest() != expected_digest:
        raise ReleaseError("source ZIP authoritative digest mismatch")
    return records, expected_digest


def _verify_machine_assets(output: Path, spec: Mapping[str, Any]) -> None:
    artifacts = spec["artifacts"]
    expected = {artifacts[key] for key in spec["artifact_groups"]["machine_assets"]}
    members = zip_member_bytes(output / artifacts["machine_assets"], expected_names=expected)
    for name, data in members.items():
        if sha256_bytes(data) != sha256_path(output / name):
            raise ReleaseError(f"machine-assets member mismatch: {name}")


def _verify_release_topology(
    output: Path,
    spec: Mapping[str, Any],
    attested_names: set[str],
) -> None:
    if output.is_symlink() or not output.is_dir():
        raise ReleaseError("release directory is not a regular directory")
    entries = list(output.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ReleaseError("release directory contains a non-regular or nested entry")
    expected = set(attested_names)
    expected.update(
        spec["artifacts"][key]
        for key in (
            "release_attestation", "artifact_inventory", "machine_assets", "checksums",
            "bundle", "bundle_sidecar",
        )
    )
    observed = {path.name for path in entries}
    if observed != expected:
        raise ReleaseError(
            f"release directory topology mismatch: missing={sorted(expected-observed)}, extra={sorted(observed-expected)}"
        )


def _deep_verify(
    output: Path,
    validation: Mapping[str, Any],
    spec: Mapping[str, Any],
    records: Sequence[SourceRecord],
    source_digest: str,
    *,
    log: list[str],
) -> dict[str, Any]:
    artifacts = spec["artifacts"]
    with tempfile.TemporaryDirectory(prefix="codex-wire-audit-deep-verify-") as temporary_name:
        workspace = Path(temporary_name)
        probe = installed_wheel_probe(
            output / artifacts["wheel"], spec, workspace / "wheel-probe", log=log
        )
        recorded_probe = validation["checks"]["installed_wheel_capability_probe"]
        if canonical_json(probe) != canonical_json(recorded_probe):
            raise ReleaseError("deep installed-wheel probe differs from validation evidence")
        roundtrip = validate_sdist_roundtrip(
            output / artifacts["sdist"],
            output / artifacts["wheel"],
            spec,
            workspace / "sdist-roundtrip",
            log=log,
        )

        source = extract_zip_tree(
            output / artifacts["source_zip"],
            workspace / "source",
            strip_prefix=source_archive_prefix(spec),
        )
        validate_source_alignment(source, spec)
        observed_records, observed_digest = source_snapshot(source, spec)
        if observed_digest != source_digest or [row.as_dict() for row in observed_records] != [
            row.as_dict() for row in records
        ]:
            raise ReleaseError("deep source-ZIP snapshot differs from validation evidence")
        source_probe = source_package_probe(source, spec, log=log)
        if canonical_json(source_probe) != canonical_json(validation["checks"]["source_capability_probe"]):
            raise ReleaseError("deep source capability probe differs from validation evidence")
        test_output = run(
            [__import__("sys").executable, "-m", "pytest", "-q"],
            cwd=source,
            source_date_epoch=spec["release"]["source_date_epoch"],
            log=log,
        )
        rebuilt_dir = workspace / "source-dist"
        rebuilt = build_distributions(source, rebuilt_dir, spec, log=log)
        for key in ("wheel", "sdist"):
            name = artifacts[key]
            if (rebuilt_dir / name).read_bytes() != (output / name).read_bytes():
                raise ReleaseError(f"deep source-ZIP rebuild differs: {name}")
        return {
            "installed_probe_sha256": sha256_bytes(canonical_json(probe)),
            "source_probe_sha256": sha256_bytes(canonical_json(source_probe)),
            "source_pytest_passed": parse_pytest_pass_count(test_output),
            "sdist_roundtrip": roundtrip,
            "source_rebuilt_distributions": {
                name: record.as_dict() for name, record in sorted(rebuilt.items())
            },
        }


def verify_release_directory(
    output: Path,
    spec: Mapping[str, Any],
    *,
    deep: bool = False,
    log: list[str] | None = None,
    require_recorded_deep: bool = True,
) -> dict[str, Any]:
    artifacts = spec["artifacts"]
    validation = load_json_object(output / artifacts["validation_json"])
    verify_integrity(validation, label="validation record")
    if validation.get("status") != "passed" or validation.get("release_spec_sha256") != release_spec_sha256(spec):
        raise ReleaseError("validation record status or release specification binding is invalid")
    checks = validation.get("checks")
    if not isinstance(checks, Mapping):
        raise ReleaseError("validation record checks are malformed")
    recorded_deep = checks.get("deep_closure_verification")
    if require_recorded_deep and not isinstance(recorded_deep, Mapping):
        raise ReleaseError("validation record lacks durable deep-closure evidence")
    attestation = load_json_object(output / artifacts["release_attestation"])
    verify_integrity(attestation, label="release attestation")
    if attestation.get("release_spec_sha256") != release_spec_sha256(spec):
        raise ReleaseError("release attestation uses a different release specification")
    if attestation.get("validation_integrity") != validation.get("integrity"):
        raise ReleaseError("release attestation is not bound to the validation record")

    released_spec = load_json_object(output / artifacts["release_spec_copy"])
    if release_spec_sha256(released_spec) != release_spec_sha256(spec):
        raise ReleaseError("standalone release specification differs from package specification")
    probe = attestation.get("installed_capability_probe")
    if not isinstance(probe, Mapping):
        raise ReleaseError("release attestation lacks an installed capability probe")
    verify_integrity(probe, label="installed capability probe")
    if probe.get("status") != "passed" or probe.get("release_spec_sha256") != release_spec_sha256(spec):
        raise ReleaseError("installed capability probe status or specification binding is invalid")
    recorded_probe = validation.get("checks", {}).get("installed_wheel_capability_probe")
    if canonical_json(probe) != canonical_json(recorded_probe):
        raise ReleaseError("attested installed probe differs from validation evidence")

    attested_names = _verify_attested_artifacts(output, attestation, spec)
    _verify_inventory(output, spec)
    _verify_release_topology(output, spec, attested_names)
    records, source_digest = _verify_source_zip(output, validation, spec)
    _verify_machine_assets(output, spec)

    required_resources = required_package_resources(spec)
    distributions = validated_distributions(validation, output, spec, require_exact_directory=False)
    wheel_info = inspect_wheel(output / artifacts["wheel"], spec, required_resources=required_resources)
    sdist_info = inspect_sdist(output / artifacts["sdist"], spec, required_resources=required_resources)
    for name, record in distributions.items():
        if sha256_path(output / name) != record.sha256:
            raise ReleaseError(f"released distribution differs from validation: {name}")
    if attestation.get("distributions") != {name: record.as_dict() for name, record in distributions.items()}:
        raise ReleaseError("release attestation distribution records differ from validation")

    checksums = _read_checksums(output / artifacts["checksums"])
    expected_checksum_names = {
        path.name
        for path in output.iterdir()
        if path.is_file()
        and path.name not in {artifacts["checksums"], artifacts["bundle"], artifacts["bundle_sidecar"]}
    }
    if set(checksums) != expected_checksum_names:
        raise ReleaseError("checksum manifest does not close over standalone artifacts")
    for name, digest in checksums.items():
        if sha256_path(output / name) != digest:
            raise ReleaseError(f"checksum mismatch: {name}")

    bundle = output / artifacts["bundle"]
    sidecar = (output / artifacts["bundle_sidecar"]).read_text(encoding="utf-8").strip()
    if sidecar != f"{sha256_path(bundle)}  {bundle.name}":
        raise ReleaseError("aggregate bundle sidecar mismatch")
    expected_bundle = {
        path.name
        for path in output.iterdir()
        if path.is_file() and path.name not in {artifacts["bundle"], artifacts["bundle_sidecar"]}
    }
    bundle_members = zip_member_bytes(bundle, expected_names=expected_bundle)
    for name, data in bundle_members.items():
        if sha256_bytes(data) != sha256_path(output / name):
            raise ReleaseError(f"aggregate bundle member mismatch: {name}")

    context_expected = {
        name: sha256_bytes(data)
        for name, data, _ in wheel_resource_entries(
            output / artifacts["wheel"],
            resource_prefix="codex_wire_audit/context_management_container_data",
        )
    }
    context_members = zip_member_bytes(
        output / artifacts["context_container"], expected_names=set(context_expected)
    )
    context_observed = {name: sha256_bytes(data) for name, data in context_members.items()}
    if context_observed != context_expected:
        raise ReleaseError("standalone active container is not byte-derived from the wheel")

    deep_result: dict[str, Any] | None = None
    if deep:
        deep_log = log if log is not None else []
        deep_result = _deep_verify(
            output,
            validation,
            spec,
            records,
            source_digest,
            log=deep_log,
        )
        if require_recorded_deep and canonical_json(deep_result) != canonical_json(recorded_deep):
            raise ReleaseError("fresh deep verification differs from durable validation evidence")

    return {
        "status": "passed",
        "release_id": spec["release"]["id"],
        "release_spec_sha256": release_spec_sha256(spec),
        "bundle_sha256": sha256_path(bundle),
        "artifact_count": len(list(output.iterdir())),
        "wheel": wheel_info,
        "sdist": sdist_info,
        "deep_verification": deep_result,
    }
