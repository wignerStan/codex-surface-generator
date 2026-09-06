"""Probe the capabilities and resources of the package that is actually imported."""
from __future__ import annotations

import argparse
import hashlib
import importlib
from importlib import metadata, resources
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from .release_spec_validation import (
    SCHEMA_RESOURCE,
    SPEC_RESOURCE,
    canonical_json,
    load_packaged_release_spec,
    release_spec_validation_backend,
    validate_release_spec,
)


def release_spec_digest(spec: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(spec)).hexdigest()


def load_release_spec() -> dict[str, Any]:
    return load_packaged_release_spec()


def _resolve_callable(reference: str) -> Callable[[], Any]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"invalid callable reference: {reference}")
    module = importlib.import_module(module_name)
    value = getattr(module, attribute)
    if not callable(value):
        raise TypeError(f"release verifier is not callable: {reference}")
    return value


def _distribution_version(package_name: str) -> str | None:
    try:
        return metadata.version(package_name)
    except metadata.PackageNotFoundError:
        return None


def _resource_record(relative: str) -> dict[str, Any]:
    item = resources.files(__package__).joinpath(relative)
    if not item.is_file():
        return {"path": relative, "present": False}
    data = item.read_bytes()
    return {
        "path": relative,
        "present": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _container_probe(reference: str) -> tuple[Any, str]:
    value = _resolve_callable(reference)()
    if not isinstance(value, Mapping):
        raise TypeError(f"container verifier returned {type(value).__name__}, expected object")
    return value, hashlib.sha256(canonical_json(value)).hexdigest()


def probe_active_package(*, require_distribution_metadata: bool = False) -> dict[str, Any]:
    """Return deterministic proof of what the imported package actually activates."""
    spec = load_release_spec()
    release = spec["release"]
    errors: list[str] = []

    package = importlib.import_module(release["package_import"])
    generator_version = getattr(package, "GENERATOR_VERSION", None)
    distribution_version = _distribution_version(release["package_name"])
    if generator_version != release["generator_version"]:
        errors.append(
            f"generator version mismatch: expected {release['generator_version']}, got {generator_version}"
        )
    if require_distribution_metadata and distribution_version is None:
        errors.append("installed distribution metadata is unavailable")
    if require_distribution_metadata and distribution_version != release["package_version"]:
        errors.append(
            f"distribution version mismatch: expected {release['package_version']}, got {distribution_version}"
        )

    from .extractors import create_extractors
    from .proof_profiles import resolve_profile

    active_extractors = sorted({extractor.extractor_id for extractor in create_extractors()})
    claim_results: list[dict[str, Any]] = []
    required_resource_names: set[str] = {SPEC_RESOURCE, SCHEMA_RESOURCE}

    for claim in spec["capability_claims"]:
        claim_errors: list[str] = []
        required_extractors = sorted(set(claim.get("extractor_ids", [])))
        missing_extractors = sorted(set(required_extractors) - set(active_extractors))
        if missing_extractors:
            claim_errors.append(f"inactive extractors: {', '.join(missing_extractors)}")

        profile_results: dict[str, Any] = {}
        for profile_id, assertion in sorted(claim.get("profile_assertions", {}).items()):
            try:
                profile = resolve_profile(profile_id)
            except Exception as error:  # pragma: no cover - defensive boundary
                claim_errors.append(f"profile {profile_id} unavailable: {error}")
                continue
            observed = {
                "required_extractors": sorted(profile.required_extractors),
                "required_dimensions": sorted(profile.required_dimensions),
                "required_runtime_scenarios": sorted(profile.required_runtime_scenarios),
            }
            profile_results[profile_id] = observed
            for assertion_key, actual_key in (
                ("required_extractors_contains", "required_extractors"),
                ("required_dimensions_contains", "required_dimensions"),
                ("required_runtime_scenarios_contains", "required_runtime_scenarios"),
            ):
                expected = set(assertion.get(assertion_key, []))
                missing = sorted(expected - set(observed[actual_key]))
                if missing:
                    claim_errors.append(
                        f"profile {profile_id} missing {actual_key}: {', '.join(missing)}"
                    )

        resources_for_claim = sorted(set(claim.get("required_resources", [])))
        required_resource_names.update(resources_for_claim)
        resource_records = [_resource_record(relative) for relative in resources_for_claim]
        missing_resources = [record["path"] for record in resource_records if not record["present"]]
        if missing_resources:
            claim_errors.append(f"missing package resources: {', '.join(missing_resources)}")

        container_result: Any = None
        container_sha256: str | None = None
        verifier_reference = claim.get("container_verifier")
        if verifier_reference:
            try:
                container_result, container_sha256 = _container_probe(verifier_reference)
            except Exception as error:
                claim_errors.append(f"container verifier failed: {type(error).__name__}: {error}")

        claim_results.append(
            {
                "id": claim["id"],
                "status": "passed" if not claim_errors else "failed",
                "extractor_ids": required_extractors,
                "missing_extractors": missing_extractors,
                "profiles": profile_results,
                "resources": resource_records,
                "container": container_result,
                "container_result_sha256": container_sha256,
                "errors": claim_errors,
            }
        )
        errors.extend(f"{claim['id']}: {message}" for message in claim_errors)

    all_resource_records = [_resource_record(relative) for relative in sorted(required_resource_names)]
    result: dict[str, Any] = {
        "format": "codex-wire-audit-installed-capability-probe/v2",
        "status": "passed" if not errors else "failed",
        "release_id": release["id"],
        "release_spec_sha256": release_spec_digest(spec),
        "package_name": release["package_name"],
        "package_version": distribution_version,
        "generator_version": generator_version,
        "reviewed_codex_commit": release["reviewed_codex_commit"],
        "release_spec_validation_backend": release_spec_validation_backend(),
        "active_extractors": active_extractors,
        "capability_claims": claim_results,
        "required_resources": all_resource_records,
        "errors": errors,
    }
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": hashlib.sha256(canonical_json(result)).hexdigest(),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="-")
    parser.add_argument("--require-distribution-metadata", action="store_true")
    args = parser.parse_args(argv)
    result = probe_active_package(require_distribution_metadata=args.require_distribution_metadata)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(encoded)
    else:
        Path(args.output).write_text(encoded, encoding="utf-8")
    return 0 if result["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
