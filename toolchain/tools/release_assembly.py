"""Assemble the closed release artifact graph from validated inputs."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from codex_wire_audit.release_spec_validation import safe_relative_path
from tools.release_archives import deterministic_zip_bytes, source_zip_entries, wheel_resource_entries
from tools.release_integrity import (
    add_integrity, atomic_write, copy_exact, file_record, pretty_json, sha256_path,
    verify_integrity,
)
from tools.release_spec import release_spec_sha256, source_archive_prefix
from tools.release_types import DistributionRecord, ReleaseError, SourceRecord



def validated_distributions(
    validation: Mapping[str, Any],
    dist_dir: Path,
    spec: Mapping[str, Any],
    *,
    require_exact_directory: bool = True,
) -> dict[str, DistributionRecord]:
    verify_integrity(validation, label="validation record")
    if validation.get("status") != "passed":
        raise ReleaseError("refusing to assemble from failed validation")
    if validation.get("release_spec_sha256") != release_spec_sha256(spec):
        raise ReleaseError("validation record belongs to a different release specification")
    checks = validation.get("checks")
    raw = checks.get("clean_copy_distributions") if isinstance(checks, Mapping) else None
    if not isinstance(raw, Mapping):
        raise ReleaseError("validation record has no closed distribution set")
    expected_names = {spec["artifacts"]["wheel"], spec["artifacts"]["sdist"]}
    if set(raw) != expected_names:
        raise ReleaseError("validation distribution names do not match release specification")
    entries = list(dist_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ReleaseError("distribution directory contains a non-regular entry")
    observed_names = {path.name for path in entries}
    if require_exact_directory and observed_names != expected_names:
        raise ReleaseError("distribution directory contains an unexpected or missing artifact")
    if not expected_names.issubset(observed_names):
        raise ReleaseError("distribution directory is missing a validated artifact")
    result: dict[str, DistributionRecord] = {}
    for name in sorted(expected_names):
        item = raw[name]
        if not isinstance(item, Mapping):
            raise ReleaseError(f"invalid distribution record: {name}")
        record = DistributionRecord.from_mapping(item)
        if record.name != name:
            raise ReleaseError(f"distribution record name mismatch: {name}")
        path = dist_dir / name
        if path.stat().st_size != record.size or sha256_path(path) != record.sha256:
            raise ReleaseError(f"distribution was replaced after validation: {name}")
        result[name] = record
    return result


def _copy_legacy_evidence(
    legacy_dir: Path | None,
    output: Path,
    spec: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if legacy_dir is None:
        return []
    if legacy_dir.is_symlink() or not legacy_dir.is_dir():
        raise ReleaseError("legacy evidence input is not a regular directory")
    policy = spec["legacy_evidence"]
    records: list[dict[str, Any]] = []
    for source_name in policy.get("allowed_source_names", []):
        source = legacy_dir / source_name
        if not source.exists():
            continue
        destination = output / f"{policy['output_prefix']}{source_name}"
        copy_exact(source, destination)
        records.append(
            file_record(destination, role=policy["role"], provenance="external_legacy_input")
        )
    return records


def _copy_review_diff(review_diff: Path | None, output: Path) -> dict[str, Any] | None:
    if review_diff is None:
        return None
    if not safe_relative_path(review_diff.name, top_level=True):
        raise ReleaseError("review diff has an unsafe filename")
    destination = output / review_diff.name
    copy_exact(review_diff, destination)
    return file_record(destination, role="review_diff", provenance="caller_supplied")


def assemble_release(
    source_root: Path,
    records: Sequence[SourceRecord],
    source_digest: str,
    dist_dir: Path,
    validation: Mapping[str, Any],
    validation_log: str,
    output: Path,
    spec: Mapping[str, Any],
    *,
    legacy_dir: Path | None,
    review_diff: Path | None,
) -> dict[str, Any]:
    if output.exists():
        raise ReleaseError(f"assembly output already exists: {output}")
    output.mkdir(parents=True)
    distributions = validated_distributions(validation, dist_dir, spec)
    artifacts = spec["artifacts"]
    epoch = spec["release"]["source_date_epoch"]

    wheel_record = distributions[artifacts["wheel"]]
    sdist_record = distributions[artifacts["sdist"]]
    wheel = output / wheel_record.name
    sdist = output / sdist_record.name
    copy_exact(dist_dir / wheel_record.name, wheel, wheel_record)
    copy_exact(dist_dir / sdist_record.name, sdist, sdist_record)

    for key in ("readme", "release_notes"):
        copy_exact(source_root / artifacts[key], output / artifacts[key])
    atomic_write(output / artifacts["validation_json"], pretty_json(validation))
    atomic_write(output / artifacts["validation_log"], validation_log.encode("utf-8"))
    atomic_write(output / artifacts["release_spec_copy"], pretty_json(spec))
    review_record = _copy_review_diff(review_diff, output)

    source_zip = output / artifacts["source_zip"]
    source_zip_info = deterministic_zip_bytes(
        source_zip,
        source_zip_entries(source_root, records, prefix=source_archive_prefix(spec)),
        epoch=epoch,
    )
    context_zip = output / artifacts["context_container"]
    context_zip_info = deterministic_zip_bytes(
        context_zip,
        wheel_resource_entries(
            wheel,
            resource_prefix="codex_wire_audit/context_management_container_data",
        ),
        epoch=epoch,
    )
    legacy_records = _copy_legacy_evidence(legacy_dir, output, spec)

    base_records = [
        file_record(wheel, role="installable_distribution", provenance="validated_source_snapshot"),
        file_record(sdist, role="source_distribution", provenance="validated_source_snapshot"),
        file_record(source_zip, role="source_snapshot", provenance="validated_source_snapshot"),
        file_record(
            context_zip,
            role="active_package_container",
            provenance="extracted_from_validated_wheel",
        ),
        file_record(
            output / artifacts["validation_json"],
            role="validation_evidence",
            provenance="release_pipeline",
        ),
        file_record(
            output / artifacts["validation_log"],
            role="validation_log",
            provenance="release_pipeline",
        ),
        file_record(
            output / artifacts["release_spec_copy"],
            role="release_specification",
            provenance="validated_source_snapshot",
        ),
        file_record(
            output / artifacts["readme"],
            role="documentation",
            provenance="validated_source_snapshot",
        ),
        file_record(
            output / artifacts["release_notes"],
            role="documentation",
            provenance="validated_source_snapshot",
        ),
        *legacy_records,
    ]
    if review_record is not None:
        base_records.append(review_record)

    release_attestation = add_integrity(
        {
            "format": "codex-wire-audit-release-attestation/v2",
            "status": "passed",
            "release": spec["release"],
            "release_spec_sha256": release_spec_sha256(spec),
            "source_snapshot": {"digest": source_digest, "file_count": len(records)},
            "validation_integrity": validation["integrity"],
            "distributions": {
                wheel_record.name: wheel_record.as_dict(),
                sdist_record.name: sdist_record.as_dict(),
            },
            "installed_capability_probe": validation["checks"]["installed_wheel_capability_probe"],
            "claim_binding": {
                "active_capabilities_must_be_package_owned": True,
                "active_context_container_provenance": "extracted_from_validated_wheel",
                "legacy_evidence_may_satisfy_active_claims": spec["legacy_evidence"][
                    "may_satisfy_active_capability_claims"
                ],
            },
            "artifacts": sorted(base_records, key=lambda item: item["name"]),
            "source_zip": source_zip_info,
            "context_container": context_zip_info,
        }
    )
    atomic_write(output / artifacts["release_attestation"], pretty_json(release_attestation))

    inventory_scope = [
        file_record(path, role="standalone_release_artifact", provenance="release_pipeline")
        for path in sorted(output.iterdir(), key=lambda item: item.name)
        if path.is_file() and path.name != artifacts["artifact_inventory"]
    ]
    inventory = add_integrity(
        {
            "format": "codex-wire-audit-artifact-inventory/v1",
            "release_id": spec["release"]["id"],
            "scope": "standalone base artifacts before machine-assets, checksums, and aggregate bundle",
            "artifacts": inventory_scope,
        }
    )
    atomic_write(output / artifacts["artifact_inventory"], pretty_json(inventory))

    machine_names = {
        artifacts[key] for key in spec["artifact_groups"]["machine_assets"]
    }
    missing_machine = sorted(name for name in machine_names if not (output / name).is_file())
    if missing_machine:
        raise ReleaseError(f"machine-assets inputs are missing: {missing_machine}")
    machine_zip = output / artifacts["machine_assets"]
    machine_info = deterministic_zip_bytes(
        machine_zip,
        [(name, (output / name).read_bytes(), 0o644) for name in sorted(machine_names)],
        epoch=epoch,
    )

    checksum_candidates = [
        path
        for path in sorted(output.iterdir(), key=lambda item: item.name)
        if path.is_file()
        and path.name
        not in {artifacts["checksums"], artifacts["bundle"], artifacts["bundle_sidecar"]}
    ]
    checksum_text = "".join(f"{sha256_path(path)}  {path.name}\n" for path in checksum_candidates)
    atomic_write(output / artifacts["checksums"], checksum_text.encode("utf-8"))

    bundle = output / artifacts["bundle"]
    bundle_info = deterministic_zip_bytes(
        bundle,
        [
            (path.name, path.read_bytes(), 0o644)
            for path in sorted(output.iterdir(), key=lambda item: item.name)
            if path.is_file() and path.name not in {artifacts["bundle"], artifacts["bundle_sidecar"]}
        ],
        epoch=epoch,
    )
    atomic_write(
        output / artifacts["bundle_sidecar"],
        f"{sha256_path(bundle)}  {bundle.name}\n".encode("utf-8"),
    )
    return {
        "release_attestation": release_attestation,
        "machine_assets": machine_info,
        "bundle": bundle_info,
    }
