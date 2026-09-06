"""Strict validation for the package-owned release specification."""
from __future__ import annotations

from importlib import resources
import json
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any, Mapping

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # installed-wheel probe deliberately runs without dependencies
    Draft202012Validator = None  # type: ignore[assignment]

SPEC_FORMAT = "codex-wire-audit-release-spec/v1"
SPEC_RESOURCE = "release_spec.v1.json"
SCHEMA_RESOURCE = "release_spec.schema.json"
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def safe_relative_path(value: str, *, top_level: bool = False) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return False
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    path = PurePosixPath(value)
    if top_level and len(path.parts) != 1:
        return False
    for part in path.parts:
        if part in {"", ".", ".."} or part != part.rstrip(" ."):
            return False
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED:
            return False
        if any(ord(character) < 32 for character in part):
            return False
    return True


def collision_key(value: str) -> str:
    return "/".join(
        unicodedata.normalize("NFC", part).rstrip(" .").casefold()
        for part in PurePosixPath(value).parts
    )


def _schema() -> dict[str, Any]:
    raw = resources.files(__package__).joinpath(SCHEMA_RESOURCE).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("release specification schema is not an object")
    if Draft202012Validator is not None:
        Draft202012Validator.check_schema(value)
    return value



def release_spec_validation_backend() -> str:
    """Describe whether full Draft 2020-12 or the dependency-free fallback is active."""
    return "jsonschema-draft-2020-12" if Draft202012Validator is not None else "stdlib-closed-shape"


def _closed_object(
    value: Any,
    *,
    label: str,
    required: set[str],
    optional: set[str] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"release specification {label} must be an object")
    optional = optional or set()
    keys = set(value)
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise ValueError(f"release specification {label} is missing: {missing}")
    if unknown:
        raise ValueError(f"release specification {label} has unknown fields: {unknown}")
    return value


def _string_array(value: Any, *, label: str, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"release specification {label} must be an array of nonempty strings")
    if nonempty and not value:
        raise ValueError(f"release specification {label} must not be empty")
    if len(value) != len(set(value)):
        raise ValueError(f"release specification {label} contains duplicates")
    return value


def _manual_schema_shape(spec: Mapping[str, Any]) -> None:
    """Dependency-free closed-shape validation for the isolated wheel probe.

    Full JSON Schema validation is always run while building the release.  This
    fallback keeps the installed-wheel capability probe independent from runtime
    dependencies while still rejecting missing, unknown, and mistyped fields.
    """
    root = _closed_object(
        spec,
        label="root",
        required={
            "format", "release", "artifacts", "artifact_groups",
            "capability_claims", "legacy_evidence", "source_snapshot",
        },
    )
    if root["format"] != SPEC_FORMAT:
        raise ValueError("unsupported or malformed release specification")

    release = _closed_object(
        root["release"],
        label="release",
        required={
            "id", "series", "package_name", "package_import", "package_version",
            "generator_version", "runtime_dependencies", "reviewed_codex_commit",
            "source_date_epoch",
        },
    )
    string_patterns = {
        "id": r".{1,160}",
        "series": r"v[1-9][0-9]*",
        "package_name": r"[A-Za-z0-9][A-Za-z0-9._-]*",
        "package_import": r"[A-Za-z_][A-Za-z0-9_.]*",
        "package_version": r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._+-]*)?",
        "generator_version": r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9._+-]*)?",
        "reviewed_codex_commit": r"[0-9a-f]{40}",
    }
    for key, pattern in string_patterns.items():
        candidate = release[key]
        if not isinstance(candidate, str) or re.fullmatch(pattern, candidate) is None:
            raise ValueError(f"release specification release.{key} is invalid")
    _string_array(
        release["runtime_dependencies"],
        label="release.runtime_dependencies",
    )
    epoch = release["source_date_epoch"]
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 315532800:
        raise ValueError("release specification release.source_date_epoch is invalid")

    artifact_keys = {
        "artifact_inventory", "bundle", "bundle_sidecar", "checksums",
        "context_container", "machine_assets", "readme", "release_attestation",
        "release_notes", "release_spec_copy", "sdist", "source_zip",
        "validation_json", "validation_log", "wheel",
    }
    artifacts = _closed_object(root["artifacts"], label="artifacts", required=artifact_keys)
    if any(not isinstance(value, str) or not value for value in artifacts.values()):
        raise ValueError("release specification artifact names must be nonempty strings")

    groups = _closed_object(
        root["artifact_groups"], label="artifact_groups", required={"machine_assets"}
    )
    _string_array(groups["machine_assets"], label="artifact_groups.machine_assets", nonempty=True)

    claims = root["capability_claims"]
    if not isinstance(claims, list) or not claims:
        raise ValueError("release specification capability_claims must be a nonempty array")
    assertion_keys = {
        "required_extractors_contains", "required_dimensions_contains",
        "required_runtime_scenarios_contains",
    }
    for index, raw_claim in enumerate(claims):
        label = f"capability_claims[{index}]"
        claim = _closed_object(
            raw_claim,
            label=label,
            required={"id", "status", "extractor_ids", "profile_assertions", "required_resources"},
            optional={"container_verifier"},
        )
        if not isinstance(claim["id"], str) or re.fullmatch(r"[a-z][a-z0-9_.-]*", claim["id"]) is None:
            raise ValueError(f"release specification {label}.id is invalid")
        if claim["status"] != "active_package":
            raise ValueError(f"release specification {label}.status is invalid")
        _string_array(claim["extractor_ids"], label=f"{label}.extractor_ids")
        _string_array(
            claim["required_resources"], label=f"{label}.required_resources", nonempty=True
        )
        assertions = claim["profile_assertions"]
        if not isinstance(assertions, Mapping) or not assertions:
            raise ValueError(f"release specification {label}.profile_assertions must be nonempty")
        for profile_id, raw_assertion in assertions.items():
            if not isinstance(profile_id, str) or not profile_id:
                raise ValueError(f"release specification {label} has an invalid profile id")
            assertion = _closed_object(
                raw_assertion,
                label=f"{label}.profile_assertions.{profile_id}",
                required=set(),
                optional=assertion_keys,
            )
            for key, value in assertion.items():
                _string_array(value, label=f"{label}.profile_assertions.{profile_id}.{key}")
        verifier = claim.get("container_verifier")
        if verifier is not None and (
            not isinstance(verifier, str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*", verifier) is None
        ):
            raise ValueError(f"release specification {label}.container_verifier is invalid")

    legacy = _closed_object(
        root["legacy_evidence"],
        label="legacy_evidence",
        required={
            "allowed_source_names", "may_satisfy_active_capability_claims",
            "output_prefix", "role",
        },
    )
    _string_array(legacy["allowed_source_names"], label="legacy_evidence.allowed_source_names")
    if legacy["may_satisfy_active_capability_claims"] is not False:
        raise ValueError("legacy evidence may not satisfy active capability claims")
    if legacy["role"] != "legacy_evidence_only":
        raise ValueError("release specification legacy evidence role is invalid")
    if not isinstance(legacy["output_prefix"], str) or re.fullmatch(r"[A-Za-z0-9._-]{0,80}", legacy["output_prefix"]) is None:
        raise ValueError("release specification legacy evidence output prefix is invalid")

    snapshot = _closed_object(
        root["source_snapshot"],
        label="source_snapshot",
        required={
            "exclude_directory_names", "exclude_directory_suffixes",
            "exclude_root_globs", "exclude_root_directory_globs",
        },
    )
    for key, value in snapshot.items():
        _string_array(value, label=f"source_snapshot.{key}")

def _reject_collisions(values: list[str], *, label: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        key = collision_key(value)
        previous = seen.get(key)
        if previous is not None:
            # Reusing the exact same package resource across capability claims is valid;
            # only distinct spellings that normalize to the same destination are ambiguous.
            if previous == value:
                continue
            raise ValueError(f"{label} collision: {previous!r} and {value!r}")
        seen[key] = value


def validate_release_spec(spec: Mapping[str, Any]) -> None:
    """Validate schema shape and cross-field release invariants."""
    if not isinstance(spec, Mapping) or spec.get("format") != SPEC_FORMAT:
        raise ValueError("unsupported or malformed release specification")
    if Draft202012Validator is not None:
        errors = sorted(
            Draft202012Validator(_schema()).iter_errors(spec),
            key=lambda item: (list(item.absolute_path), item.message),
        )
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            raise ValueError(f"release specification schema error at {location}: {error.message}")
    else:
        _manual_schema_shape(spec)

    release = spec["release"]
    artifacts = spec["artifacts"]
    claims = spec["capability_claims"]
    if release["package_version"] != release["generator_version"]:
        raise ValueError("package_version and generator_version must match")

    artifact_names = list(artifacts.values())
    if any(not safe_relative_path(name, top_level=True) for name in artifact_names):
        raise ValueError("artifact names must be safe top-level filenames")
    if len(artifact_names) != len(set(artifact_names)):
        raise ValueError("artifact names must be unique")
    _reject_collisions(artifact_names, label="artifact name")

    machine_keys = spec["artifact_groups"]["machine_assets"]
    unknown_machine_keys = sorted(set(machine_keys) - set(artifacts))
    if unknown_machine_keys:
        raise ValueError(f"machine_assets references unknown artifact keys: {unknown_machine_keys}")
    forbidden_machine_keys = {"machine_assets", "bundle", "bundle_sidecar", "checksums"}
    if forbidden_machine_keys.intersection(machine_keys):
        raise ValueError("machine_assets group contains recursive or post-assembly artifacts")

    claim_ids: set[str] = set()
    resource_paths: list[str] = [SPEC_RESOURCE, SCHEMA_RESOURCE]
    for claim in claims:
        claim_id = claim["id"]
        if claim_id in claim_ids:
            raise ValueError(f"duplicate capability claim: {claim_id}")
        claim_ids.add(claim_id)
        if len(claim["extractor_ids"]) != len(set(claim["extractor_ids"])):
            raise ValueError(f"duplicate extractor id in capability claim {claim_id}")
        for relative in claim["required_resources"]:
            if not safe_relative_path(relative):
                raise ValueError(f"unsafe resource path in capability claim {claim_id}: {relative}")
            resource_paths.append(relative)
        for profile_id, assertion in claim["profile_assertions"].items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", profile_id):
                raise ValueError(f"invalid profile id in capability claim {claim_id}: {profile_id}")
            if not assertion:
                raise ValueError(f"empty profile assertion in capability claim {claim_id}: {profile_id}")

    _reject_collisions(resource_paths, label="package resource")

    legacy = spec["legacy_evidence"]
    if not safe_relative_path(legacy["output_prefix"] + "x", top_level=True):
        raise ValueError("legacy evidence output prefix is unsafe")
    projected_legacy = [legacy["output_prefix"] + name for name in legacy["allowed_source_names"]]
    if any(not safe_relative_path(name, top_level=True) for name in projected_legacy):
        raise ValueError("legacy evidence output name is unsafe")
    _reject_collisions(projected_legacy, label="legacy evidence output")
    collisions = set(map(collision_key, artifact_names)).intersection(map(collision_key, projected_legacy))
    if collisions:
        raise ValueError("legacy evidence output collides with a release artifact")


def load_packaged_release_spec() -> dict[str, Any]:
    raw = resources.files(__package__).joinpath(SPEC_RESOURCE).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("release specification is not an object")
    validate_release_spec(value)
    return value
