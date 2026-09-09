"""Versioned authority for migrated semantics, with deterministic legacy views.

The model is the existing source-derived evolution IR, not a second taxonomy.
Non-migrated legacy report fields are deliberately outside this authority.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping, MutableMapping

from .canonical import canonical_json_bytes
from .compatibility_views import apply_config_surface_overlay, apply_local_storage_overlay
from .extractors.registry import ExtractorResult

FORMAT = "codex-system-contract/v1"
SCHEMA_ID = "https://schemas.codex-wire-audit.invalid/system/v1/system-contract.schema.json"
CANONICALIZATION = "codex-wire-audit-canonical-json-v2"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def compatibility_views(model: Mapping[str, Any]) -> dict[str, Any]:
    """Project actual payloads, not descriptions of purported compatibility.

    New extractors enter the model automatically. Add a projector only when a
    consumer needs a new compatibility path; never reconstruct source facts.
    """
    views = {"/evolution_contract": copy.deepcopy(dict(model))}
    overlays = (
        ("extractor.config_effects", apply_config_surface_overlay),
        ("extractor.local_storage", apply_local_storage_overlay),
    )
    for identifier, project in overlays:
        entry = model["extractors"].get(identifier)
        if entry is None:
            continue
        result = ExtractorResult(
            extractor_id=identifier,
            schema_version=entry["schema_version"],
            data=entry["data"],
            semantic_complete=entry["semantic_complete"],
            source_spec_ids=tuple(entry["source_spec_ids"]),
        )
        projected: dict[str, Any] = {}
        project(projected, result)
        for key, value in projected.items():
            if key == "config_protocol":
                views.update({f"/{key}/{name}": child for name, child in value.items()})
            else:
                views[f"/{key}"] = value
    return views


def build_system_contract(model: Mapping[str, Any]) -> dict[str, Any]:
    """Seal the finalized IR. Integrity proves consistency, not source truth."""
    contract = {
        "$schema": SCHEMA_ID,
        "schema": FORMAT,
        "authority": "migrated_semantic_extractors",
        "model": copy.deepcopy(dict(model)),
        "compatibility_views": {
            pointer: digest(payload)
            for pointer, payload in sorted(compatibility_views(model).items())
        },
    }
    contract["integrity"] = {
        "algorithm": "sha256",
        "canonicalization": CANONICALIZATION,
        "canonical_sha256": digest(contract),
    }
    return contract


def attach_system_contract(report: MutableMapping[str, Any]) -> None:
    """Publish legacy mirrors only by projecting the sealed canonical model."""
    contract = build_system_contract(report["evolution_contract"])
    for pointer, payload in compatibility_views(contract["model"]).items():
        parts = pointer.lstrip("/").split("/")
        target = report
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = payload
    report["system_contract"] = contract


def view_at(report: Mapping[str, Any], pointer: str) -> Any:
    value: Any = report
    for part in pointer.lstrip("/").split("/"):
        if not isinstance(value, Mapping) or part not in value:
            raise ValueError(f"missing canonical compatibility view: {pointer}")
        value = value[part]
    return value
