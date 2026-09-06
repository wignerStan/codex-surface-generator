"""Canonical evolution contract and compatibility-report overlay."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping, MutableMapping

from . import EVOLUTION_CONTRACT_VERSION, GENERATOR_VERSION
from .canonical import canonical_json_bytes
from .diagnostics import DiagnosticCollector
from .extractors import ExtractorResult, create_extractors
from .models import SourceSnapshot
from .source_registry import SourceRegistry


def _description_for_emission(field: Mapping[str, Any]) -> str:
    kind = field.get("kind")
    predicate = field.get("predicate")
    if kind == "conditional_option_passthrough":
        return f"emitted conditionally under {predicate}; nested Option is flattened"
    if kind == "conditional_value":
        return f"emitted conditionally under {predicate}"
    if kind == "option_passthrough":
        return "independent optional passthrough; emitted when the runtime Option is Some"
    if kind == "value_passthrough":
        return "direct runtime field passthrough"
    if kind == "non_empty_projection":
        return "emitted only when the source collection is non-empty"
    if kind == "flattened_extra":
        return "flattened extra metadata at the root of the nested turn object"
    if kind == "derived_local":
        return "derived from a local value in turn_metadata_payload"
    return "unclassified source expression; inspect the structured diagnostic"


def _gate_descriptions(turn_data: Mapping[str, Any]) -> dict[str, str]:
    descriptions: dict[str, str] = {}
    for name, gate in (turn_data.get("gates") or {}).items():
        expression = gate.get("source_expression")
        if name == "has_thread_identity":
            descriptions[name] = (
                "thread-scoped identity is emitted when request_kind is absent or the request kind "
                f"accepts thread identity; source expression: {expression}"
            )
        elif name == "has_turn_identity":
            descriptions[name] = (
                "legacy combined identity gate; source expression: " + str(expression)
            )
        elif name == "has_request_identity":
            descriptions[name] = (
                "request-scoped identity is emitted only when request_kind is present and its kind "
                f"accepts thread identity; source expression: {expression}"
            )
        else:
            descriptions[name] = f"source-derived gate; expression: {expression}"
    return descriptions


def apply_turn_metadata_overlay(
    report: MutableMapping[str, Any],
    turn_result: ExtractorResult,
) -> None:
    """Replace stale v10 gate assumptions with source-derived semantics.

    Existing field names remain available, but the authoritative machine form is
    attached under ``semantic_contract`` and all human descriptions are rendered
    from it.
    """
    data = turn_result.data
    if not data:
        return
    construction = report.setdefault("turn_metadata_construction", {})
    if not isinstance(construction, MutableMapping):
        construction = {}
        report["turn_metadata_construction"] = construction
    construction["schema_version"] = 2
    construction["construction_fn"] = data.get("construction_function")
    construction["present"] = bool(data.get("fields"))
    construction["identity_gates"] = _gate_descriptions(data)
    construction["identity_gate_contract"] = copy.deepcopy(data.get("gates") or {})
    construction["fields"] = [
        {
            "name": field_name,
            "construction_rule": _description_for_emission(field),
            "expression": field.get("source_expression"),
            "emission": copy.deepcopy(field),
            "source": {"path": data.get("source_path"), "symbol": field.get("source_ref")},
        }
        for field_name, field in (data.get("fields") or {}).items()
    ]
    construction["semantic_complete"] = turn_result.semantic_complete
    construction["semantic_digest"] = data.get("semantic_digest")

    schema = report.get("turn_metadata_schema")
    if isinstance(schema, MutableMapping):
        notes = [
            note
            for note in (schema.get("notes") or [])
            if "has_turn_identity" not in str(note)
        ]
        notes.append(
            "Identity emission is source-derived per field: request identity, thread identity, "
            "turn identity, and lineage are distinct domains."
        )
        schema["notes"] = notes
        schema["identity_model"] = {
            "field_groups": copy.deepcopy(data.get("field_groups") or {}),
            "gate_refs": sorted((data.get("gates") or {}).keys()),
            "semantic_digest": data.get("semantic_digest"),
        }

    metadata_protocol = report.get("metadata_protocol")
    if isinstance(metadata_protocol, MutableMapping):
        nested = metadata_protocol.get("nested_turn_metadata")
        if isinstance(nested, MutableMapping):
            nested["identity_gates"] = _gate_descriptions(data)
            nested["identity_gate_contract"] = copy.deepcopy(data.get("gates") or {})
            groups = nested.setdefault("field_groups", {})
            if isinstance(groups, MutableMapping):
                groups["request_identity"] = list(
                    (data.get("field_groups") or {}).get("request_identity", [])
                )
                groups["thread_identity"] = list(
                    (data.get("field_groups") or {}).get("thread_identity", [])
                )
                groups["turn_identity"] = list(
                    (data.get("field_groups") or {}).get("turn_identity", [])
                )
                groups["legacy_combined_turn_identity"] = {
                    "deprecated": True,
                    "fields": sorted(
                        set(groups.get("thread_identity", []))
                        | set(groups.get("turn_identity", []))
                    ),
                    "replacement": ["thread_identity", "turn_identity"],
                }

    mcp_protocol = report.get("mcp_protocol")
    if isinstance(mcp_protocol, MutableMapping):
        projection = mcp_protocol.get("turn_metadata_projection")
        if isinstance(projection, MutableMapping):
            identity = projection.setdefault("identity_effect", {})
            if isinstance(identity, MutableMapping):
                identity.pop("has_turn_identity", None)
                identity["has_thread_identity"] = True
                identity["turn_identity_independent"] = True
                identity["has_request_identity"] = False
                identity["thread_identity_fields"] = list(
                    (data.get("field_groups") or {}).get("thread_identity", [])
                )
                identity["turn_identity_fields"] = list(
                    (data.get("field_groups") or {}).get("turn_identity", [])
                )
                identity["legacy_has_turn_identity"] = {
                    "deprecated": True,
                    "replacement": ["has_thread_identity", "turn_identity_independent"],
                }

    relation_map = report.get("relation_map")
    if isinstance(relation_map, MutableMapping):
        relation_types = relation_map.setdefault("relation_types", {})
        if isinstance(relation_types, MutableMapping):
            relation_types["same_value_when_thread_identity"] = (
                "same value when thread identity is emitted"
            )
            relation_types["same_value_when_turn_identity"] = (
                "same value when the independent turn identity value exists"
            )
        for group in relation_map.get("groups", []) if isinstance(relation_map.get("groups"), list) else []:
            if not isinstance(group, MutableMapping):
                continue
            concept = group.get("concept")
            if concept not in {"session_id", "thread_id"}:
                continue
            for node in group.get("nodes", []) if isinstance(group.get("nodes"), list) else []:
                if isinstance(node, MutableMapping) and node.get("relation") == "same_value_when_turn_identity":
                    node["relation"] = "same_value_when_thread_identity"

    report["turn_metadata_semantics"] = {
        "authoritative": True,
        "extractor_id": turn_result.extractor_id,
        "schema_version": turn_result.schema_version,
        **copy.deepcopy(data),
    }


def _dimension_state(condition: bool, *, false_state: str = "partial") -> str:
    return "complete" if condition else false_state


def build_evolution_contract(
    snapshot: SourceSnapshot,
    registry: SourceRegistry,
    diagnostics: DiagnosticCollector,
    *,
    coverage_profile: str = "codex_wire_full",
) -> tuple[dict[str, Any], dict[str, ExtractorResult]]:
    results: dict[str, ExtractorResult] = {}
    for extractor in create_extractors():
        # Extractors are profile-scoped.  If none of their declared sources are
        # present in this snapshot, omit them rather than turning unrelated
        # narrow/legacy fixtures incomplete.  A selected proof profile still
        # fails visibly when it requires the omitted extractor.
        if extractor.source_spec_ids and not any(
            source_id in snapshot.files for source_id in extractor.source_spec_ids
        ):
            continue
        result = extractor.extract(snapshot, diagnostics)
        results[result.extractor_id] = result

    required_ids = {spec.id for spec in registry.specs if spec.required}
    required_available = required_ids <= set(snapshot.files)
    syntax_complete = all(result.data for result in results.values())
    semantics_complete = all(result.semantic_complete for result in results.values())
    overall = required_available and syntax_complete and semantics_complete and not any(
        item.severity == "error" for item in diagnostics.values()
    )
    contract: dict[str, Any] = {
        "$schema": "codex_wire_audit_v11_schemas/evolution-contract.schema.json",
        "schema_version": EVOLUTION_CONTRACT_VERSION,
        "generator_version": GENERATOR_VERSION,
        "coverage_profile": coverage_profile,
        "source_revision": snapshot.revision.to_dict(),
        "source_registry": registry.to_dict(),
        "source_snapshot": snapshot.manifest(),
        "extractors": {
            extractor_id: {
                "extractor_id": result.extractor_id,
                "schema_version": result.schema_version,
                "source_spec_ids": list(result.source_spec_ids),
                "semantic_complete": result.semantic_complete,
                "data": copy.deepcopy(result.data),
            }
            for extractor_id, result in sorted(results.items())
        },
        "migration": {
            "stage": "hybrid_canonical_ir",
            "canonical_ir_extractors": sorted(results),
            "legacy_adapter": "frozen v10 extraction/report renderer",
            "legacy_machine_reconstruction_remaining": True,
            "extension_contract": (
                "new source-aware semantic extractors register independently and emit canonical IR fragments; "
                "the compatibility adapter is not edited for new semantics"
            ),
        },
        "status": {
            "source_inventory": _dimension_state(required_available),
            "syntax_extraction": _dimension_state(syntax_complete),
            "semantic_classification": _dimension_state(semantics_complete),
            "schema_resolution": "pending_report_upgrade",
            "runtime_observation": "not_run",
            "current_main_conformance": "not_compared",
            "overall": "complete" if overall else "partial",
        },
        "diagnostic_refs": [item.id for item in diagnostics.values()],
        "diagnostic_summary": diagnostics.summary(),
    }
    digest_value = copy.deepcopy(contract)
    digest_value.pop("integrity", None)
    contract["integrity"] = {
        "algorithm": "sha256",
        "canonicalization": "codex-wire-audit-canonical-json-v2",
        "canonical_ir_sha256": hashlib.sha256(canonical_json_bytes(digest_value)).hexdigest(),
    }
    return contract, results


def finalize_evolution_contract(
    contract: MutableMapping[str, Any],
    diagnostics: DiagnosticCollector,
    *,
    schema_resolution_complete: bool,
) -> None:
    status = contract.setdefault("status", {})
    if isinstance(status, MutableMapping):
        status["schema_resolution"] = "complete" if schema_resolution_complete else "failed"
        if not schema_resolution_complete or diagnostics.summary().get("error", 0):
            status["overall"] = "partial"
    contract["diagnostic_refs"] = [item.id for item in diagnostics.values()]
    contract["diagnostic_summary"] = diagnostics.summary()
    digest_value = copy.deepcopy(dict(contract))
    digest_value.pop("integrity", None)
    contract["integrity"] = {
        "algorithm": "sha256",
        "canonicalization": "codex-wire-audit-canonical-json-v2",
        "canonical_ir_sha256": hashlib.sha256(canonical_json_bytes(digest_value)).hexdigest(),
    }
