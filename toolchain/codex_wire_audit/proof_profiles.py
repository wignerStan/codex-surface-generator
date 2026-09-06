from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from importlib.resources import files
import json
from typing import Any, Iterable

from .proof_diagnostics import ProofDiagnostic


@dataclass(frozen=True, slots=True)
class CoverageProfile:
    id: str
    version: str
    required_extractors: tuple[str, ...]
    required_dimensions: tuple[str, ...]
    required_runtime_scenarios: tuple[str, ...]
    required_history_keys: tuple[str, ...]
    allow_legacy_reconstruction: bool
    description: str

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _load_profiles() -> dict[str, CoverageProfile]:
    resource = files(__package__).joinpath("coverage_profiles.v3.json")
    document = json.loads(resource.read_text(encoding="utf-8"))
    if document.get("format") != "codex-wire-audit-coverage-profiles/v3":
        raise RuntimeError("unsupported coverage-profile manifest format")
    result: dict[str, CoverageProfile] = {}
    for raw in document.get("profiles", []):
        profile = CoverageProfile(
            id=raw["id"],
            version=raw["version"],
            required_extractors=tuple(raw["required_extractors"]),
            required_dimensions=tuple(raw["required_dimensions"]),
            required_runtime_scenarios=tuple(raw.get("required_runtime_scenarios", [])),
            required_history_keys=tuple(raw.get("required_history_keys", [])),
            allow_legacy_reconstruction=bool(raw["allow_legacy_reconstruction"]),
            description=raw["description"],
        )
        if profile.id in result:
            raise RuntimeError(f"duplicate coverage profile id: {profile.id}")
        result[profile.id] = profile
    if not result:
        raise RuntimeError("coverage-profile manifest is empty")
    return result


PROFILES = _load_profiles()

_SKIP_KEYS = {
    "required_extractors", "missing_extractors", "coverage_profile", "profile_evaluation",
    "proof_profile", "profile_manifest", "diagnostics", "description",
}


def resolve_profile(profile_id: str | None) -> CoverageProfile:
    candidate = profile_id or "hybrid_v18"
    try:
        return PROFILES[candidate]
    except KeyError as error:
        raise ValueError(f"unknown coverage profile: {candidate}") from error


def _walk_observed(value: Any, *, parent_key: str = "") -> Iterable[str]:
    if parent_key in _SKIP_KEYS:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _SKIP_KEYS:
                continue
            if isinstance(key, str) and key.startswith("extractor."):
                yield key
            if key in {"id", "extractor_id", "extractor"} and isinstance(item, str) and item.startswith("extractor."):
                yield item
            yield from _walk_observed(item, parent_key=key)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_observed(item, parent_key=parent_key)


def observed_extractors(report: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(set(_walk_observed(report))))


def _find_boolean(value: Any, key_name: str) -> bool | None:
    if isinstance(value, dict):
        if key_name in value and isinstance(value[key_name], bool):
            return value[key_name]
        for item in value.values():
            found = _find_boolean(item, key_name)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_boolean(item, key_name)
            if found is not None:
                return found
    return None


def evaluate_profile(report: dict[str, Any], profile_id: str | None) -> tuple[dict[str, Any], list[ProofDiagnostic]]:
    diagnostics: list[ProofDiagnostic] = []
    try:
        profile = resolve_profile(profile_id)
    except ValueError as error:
        diagnostics.append(ProofDiagnostic(code="COVERAGE_PROFILE_UNKNOWN", message=str(error)))
        return {"id": profile_id, "version": None, "digest": None, "required_extractors": [], "observed_extractors": list(observed_extractors(report)), "missing_extractors": [], "complete": False}, diagnostics
    observed = set(observed_extractors(report))
    missing = sorted(set(profile.required_extractors) - observed)
    for extractor_id in missing:
        diagnostics.append(ProofDiagnostic(code="REQUIRED_EXTRACTOR_MISSING", message=f"coverage profile {profile.id} requires {extractor_id}", source_id=extractor_id, details={"profile": profile.id}))
    legacy_remaining = _find_boolean(report, "legacy_machine_reconstruction_remaining") is True
    if legacy_remaining and not profile.allow_legacy_reconstruction:
        diagnostics.append(ProofDiagnostic(code="LEGACY_RECONSTRUCTION_NOT_ALLOWED", message=f"coverage profile {profile.id} cannot be satisfied by legacy machine reconstruction", details={"profile": profile.id}))
    evaluation = {
        "id": profile.id,
        "version": profile.version,
        "digest": profile.digest(),
        "description": profile.description,
        "required_extractors": list(profile.required_extractors),
        "observed_extractors": sorted(observed),
        "missing_extractors": missing,
        "allow_legacy_reconstruction": profile.allow_legacy_reconstruction,
        "legacy_reconstruction_observed": legacy_remaining,
        "required_dimensions": list(profile.required_dimensions),
        "required_runtime_scenarios": list(profile.required_runtime_scenarios),
        "required_history_keys": list(profile.required_history_keys),
        "complete": not missing and (profile.allow_legacy_reconstruction or not legacy_remaining),
    }
    return evaluation, diagnostics


def find_profile_id(report: dict[str, Any]) -> str | None:
    preferred = ("coverage_profile", "profile_id", "proof_profile")
    queue: list[Any] = [report]
    while queue:
        value = queue.pop(0)
        if isinstance(value, dict):
            for key in preferred:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate in PROFILES:
                    return candidate
            queue.extend(value.values())
        elif isinstance(value, list):
            queue.extend(value)
    return None


def mark_contract_incomplete(contract: dict[str, Any]) -> None:
    if isinstance(contract.get("status"), str):
        contract["status"] = "incomplete"
    elif isinstance(contract.get("status"), dict):
        status = contract["status"]
        if "overall" in status: status["overall"] = "incomplete"
        if "complete" in status: status["complete"] = False
        if "semantic_complete" in status: status["semantic_complete"] = False
    for key in ("complete", "semantic_complete"):
        if key in contract and isinstance(contract[key], bool): contract[key] = False
