"""Stable semantic diffs for repository evolution canaries."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from . import SEMANTIC_DIFF_VERSION


class SemanticDiffError(ValueError):
    pass


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _contract(value: Mapping[str, Any]) -> Mapping[str, Any]:
    if "evolution_contract" in value and isinstance(value["evolution_contract"], Mapping):
        return value["evolution_contract"]
    if "extractors" in value and "source_revision" in value:
        return value
    raise SemanticDiffError("value does not contain an evolution_contract")


def _turn_metadata(contract: Mapping[str, Any]) -> Mapping[str, Any]:
    extractors = contract.get("extractors")
    if not isinstance(extractors, Mapping):
        return {}
    result = extractors.get("extractor.turn_metadata")
    if not isinstance(result, Mapping):
        return {}
    data = result.get("data")
    return data if isinstance(data, Mapping) else {}


def _change(
    kind: str,
    entity_id: str,
    before: Any,
    after: Any,
    *,
    severity: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity = {
        "kind": kind,
        "entity_id": entity_id,
        "before": before,
        "after": after,
        "details": dict(details or {}),
    }
    return {
        "id": f"semantic_change.{kind}.{_digest(identity)}",
        "kind": kind,
        "entity_id": entity_id,
        "severity": severity,
        "before": before,
        "after": after,
        "details": dict(details or {}),
    }


def compare(old_value: Mapping[str, Any], new_value: Mapping[str, Any]) -> dict[str, Any]:
    old_contract = _contract(old_value)
    new_contract = _contract(new_value)
    old_tm = _turn_metadata(old_contract)
    new_tm = _turn_metadata(new_contract)
    changes: list[dict[str, Any]] = []

    old_gates = old_tm.get("gates") if isinstance(old_tm.get("gates"), Mapping) else {}
    new_gates = new_tm.get("gates") if isinstance(new_tm.get("gates"), Mapping) else {}
    for name in sorted(set(old_gates) | set(new_gates)):
        before = old_gates.get(name)
        after = new_gates.get(name)
        entity_id = f"turn_metadata.gate.{name}"
        if before is None:
            changes.append(_change("gate_added", entity_id, None, after, severity="attention"))
        elif after is None:
            changes.append(_change("gate_removed", entity_id, before, None, severity="attention"))
        elif before.get("semantic_fingerprint") != after.get("semantic_fingerprint"):
            changes.append(
                _change(
                    "gate_predicate_changed",
                    entity_id,
                    before.get("predicate"),
                    after.get("predicate"),
                    severity="breaking",
                    details={
                        "before_expression": before.get("source_expression"),
                        "after_expression": after.get("source_expression"),
                    },
                )
            )

    old_fields = old_tm.get("fields") if isinstance(old_tm.get("fields"), Mapping) else {}
    new_fields = new_tm.get("fields") if isinstance(new_tm.get("fields"), Mapping) else {}
    for name in sorted(set(old_fields) | set(new_fields)):
        before = old_fields.get(name)
        after = new_fields.get(name)
        entity_id = f"turn_metadata.field.{name}"
        if before is None:
            changes.append(_change("field_added", entity_id, None, after, severity="attention"))
            continue
        if after is None:
            changes.append(_change("field_removed", entity_id, before, None, severity="breaking"))
            continue
        if before.get("semantic_fingerprint") == after.get("semantic_fingerprint"):
            continue
        # A referenced gate predicate can evolve while the field's own emission
        # expression remains unchanged. The gate delta is authoritative and
        # already lists the semantic change; do not duplicate it for every
        # dependent field. Older contracts without emission_fingerprint retain
        # the conservative v1 comparison behavior.
        before_emission = before.get("emission_fingerprint")
        after_emission = after.get("emission_fingerprint")
        if before_emission is not None and before_emission == after_emission:
            continue
        if before.get("gate_refs") != after.get("gate_refs"):
            changes.append(
                _change(
                    "field_gate_changed",
                    entity_id,
                    before.get("gate_refs"),
                    after.get("gate_refs"),
                    severity="breaking",
                    details={
                        "before_predicate": before.get("predicate"),
                        "after_predicate": after.get("predicate"),
                    },
                )
            )
        if before.get("kind") != after.get("kind"):
            changes.append(
                _change(
                    "field_emission_kind_changed",
                    entity_id,
                    before.get("kind"),
                    after.get("kind"),
                    severity="breaking",
                )
            )
        if before.get("value_expression") != after.get("value_expression"):
            changes.append(
                _change(
                    "field_value_expression_changed",
                    entity_id,
                    before.get("value_expression"),
                    after.get("value_expression"),
                    severity="attention",
                )
            )
        if before.get("identity_domain") != after.get("identity_domain"):
            changes.append(
                _change(
                    "field_identity_domain_changed",
                    entity_id,
                    before.get("identity_domain"),
                    after.get("identity_domain"),
                    severity="breaking",
                )
            )
        if not any(change["entity_id"] == entity_id for change in changes):
            changes.append(
                _change(
                    "field_semantics_changed",
                    entity_id,
                    before,
                    after,
                    severity="attention",
                )
            )

    old_registry = old_contract.get("source_registry")
    new_registry = new_contract.get("source_registry")
    old_sources = old_registry.get("sources", {}) if isinstance(old_registry, Mapping) else {}
    new_sources = new_registry.get("sources", {}) if isinstance(new_registry, Mapping) else {}
    for spec_id in sorted(set(old_sources) | set(new_sources)):
        before = old_sources.get(spec_id)
        after = new_sources.get(spec_id)
        if before is None:
            changes.append(_change("source_spec_added", spec_id, None, after, severity="info"))
        elif after is None:
            changes.append(_change("source_spec_removed", spec_id, before, None, severity="attention"))
        elif before.get("path_candidates") != after.get("path_candidates"):
            changes.append(
                _change(
                    "source_path_candidates_changed",
                    spec_id,
                    before.get("path_candidates"),
                    after.get("path_candidates"),
                    severity="info",
                )
            )

    changes.sort(key=lambda item: (item["severity"], item["kind"], item["entity_id"], item["id"]))
    counts = {"breaking": 0, "attention": 0, "info": 0}
    for item in changes:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return {
        "schema_version": SEMANTIC_DIFF_VERSION,
        "before_source_revision_id": (old_contract.get("source_revision") or {}).get("source_revision_id"),
        "after_source_revision_id": (new_contract.get("source_revision") or {}).get("source_revision_id"),
        "changes": changes,
        "summary": {
            "total": len(changes),
            **counts,
            "breaking": counts["breaking"],
            "has_breaking_changes": counts["breaking"] > 0,
        },
    }
