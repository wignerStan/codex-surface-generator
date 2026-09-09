"""Legacy configuration/storage views projected from canonical extractor results."""
from __future__ import annotations

import copy
from typing import Any, MutableMapping

from .extractors.registry import ExtractorResult


def apply_config_surface_overlay(report: MutableMapping[str, Any], config_result: ExtractorResult) -> None:
    """Attach configuration views derived from the canonical extractor fragment."""
    data = config_result.data
    if not data:
        return
    report["config_schema"] = {
        "authoritative": False,
        "extractor_id": config_result.extractor_id,
        "schema_version": config_result.schema_version,
        **copy.deepcopy(data.get("config_schema") or {}),
    }
    report["config_surface_graph"] = copy.deepcopy(data.get("surface_graph") or {})
    protocol = report.setdefault("config_protocol", {})
    if isinstance(protocol, MutableMapping):
        protocol["canonical_schema"] = {
            "extractor_id": config_result.extractor_id,
            "semantic_digest": (data.get("config_schema") or {}).get("semantic_digest"),
            "summary": copy.deepcopy((data.get("config_schema") or {}).get("summary") or {}),
        }
        protocol["canonical_feature_registry"] = copy.deepcopy(data.get("feature_crosswalk") or {})
        protocol["canonical_effect_links"] = copy.deepcopy(data.get("effect_links") or [])
        protocol["surface_graph"] = copy.deepcopy(data.get("surface_graph") or {})
        protocol["legacy_effect_catalog_role"] = (
            "compatibility evidence connected into the graph with proof_tier=legacy_compatibility"
        )


def apply_local_storage_overlay(report: MutableMapping[str, Any], result: ExtractorResult) -> None:
    """Expose local-storage semantics as an explicitly non-authoritative view."""
    if result.data:
        report["local_storage_schema"] = {
            "authoritative": False,
            "extractor_id": result.extractor_id,
            "schema_version": result.schema_version,
            **copy.deepcopy(result.data),
        }
