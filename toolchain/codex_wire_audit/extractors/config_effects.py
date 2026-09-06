"""Canonical generated-config schema and config-to-surface extraction."""

from __future__ import annotations

import copy
from typing import Any

from ..config_effect_specs import effect_specs, expanded_config_paths
from ..config_feature_registry import (
    crosswalk_features,
    extract_feature_registry,
    extract_feature_schema_policy,
)
from ..config_schema_catalog import ConfigSchemaError, build_config_schema_catalog
from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot, semantic_fingerprint
from ..surface_graph import compose_surface_graph
from .registry import ExtractorResult, register_extractor


EXTRACTOR_ID = "extractor.config_effects"
SCHEMA_VERSION = "1.0.0"
SOURCE_IDS = {
    "generated_schema": "source_spec.extra.generated_config_schema",
    "feature_registry": "source_spec.extra.feature_registry",
    "schema_generator": "source_spec.extra.config_schema_generator",
    "config_toml": "source_spec.extra.config_toml",
    "core_config": "source_spec.extra.core_config",
    "provider_info": "source_spec.base.provider_info",
    "turn_metadata": "source_spec.base.metadata",
    "activation": "source_spec.extra.context_management_activation",
}


def _source(snapshot: SourceSnapshot, key: str) -> SourceFile | None:
    return snapshot.files.get(SOURCE_IDS[key])


def _emit(
    diagnostics: DiagnosticCollector,
    *,
    code: str,
    message: str,
    entity: str,
    source: SourceFile | None = None,
    details: dict[str, Any] | None = None,
    severity: str = "error",
) -> None:
    diagnostics.emit(
        code=code,
        severity=severity,
        category="config_surface",
        message=message,
        extractor_id=EXTRACTOR_ID,
        entity_id=entity,
        source_refs=(f"{source.spec_id}:{source.selected_path}",) if source else (),
        details=details or {},
        recoverable=False,
        strict_failure=severity == "error",
    )


def _effect_links(catalog: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    paths = set((catalog.get("paths") or {}).keys())
    links: list[dict[str, Any]] = []
    missing: list[str] = []
    for spec in effect_specs():
        expanded = expanded_config_paths(spec, paths)
        if not expanded:
            missing.extend(spec.config_paths)
            continue
        links.append({
            "id": spec.id,
            "config_paths": list(expanded),
            "target": spec.target_id,
            "relation": spec.relation,
            "behavior": spec.behavior,
            "condition": spec.condition,
            "proof_tier": spec.proof_tier,
        })
    return links, sorted(set(missing))


class ConfigEffectsExtractor:
    extractor_id = EXTRACTOR_ID
    # The generated schema is the activation source. Ancillary Rust files are
    # consumed once it is present, but narrow legacy fixtures must not trigger
    # this extractor merely because they contain generic provider/metadata files.
    source_spec_ids = (SOURCE_IDS["generated_schema"],)
    result_source_spec_ids = tuple(SOURCE_IDS.values())

    def extract(self, snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> ExtractorResult:
        schema_source = _source(snapshot, "generated_schema")
        registry_source = _source(snapshot, "feature_registry")
        schema_generator_source = _source(snapshot, "schema_generator")
        if schema_source is None:
            _emit(
                diagnostics,
                code="GENERATED_CONFIG_SCHEMA_UNAVAILABLE",
                message="Codex's generated config.schema.json is unavailable.",
                entity="config_schema",
                details={"source_spec_id": SOURCE_IDS["generated_schema"]},
            )
        if registry_source is None:
            _emit(
                diagnostics,
                code="FEATURE_REGISTRY_SOURCE_UNAVAILABLE",
                message="The Rust FeatureSpec registry source is unavailable.",
                entity="feature_registry",
                details={"source_spec_id": SOURCE_IDS["feature_registry"]},
            )
        if schema_generator_source is None:
            _emit(
                diagnostics,
                code="CONFIG_SCHEMA_GENERATOR_SOURCE_UNAVAILABLE",
                message="The Rust features_schema() generator source is unavailable.",
                entity="feature_schema_policy",
                details={"source_spec_id": SOURCE_IDS["schema_generator"]},
            )
        if schema_source is None or registry_source is None or schema_generator_source is None:
            return ExtractorResult(
                extractor_id=EXTRACTOR_ID,
                schema_version=SCHEMA_VERSION,
                data={},
                semantic_complete=False,
                source_spec_ids=self.result_source_spec_ids,
            )

        try:
            catalog = build_config_schema_catalog(
                schema_source.text,
                source_path=schema_source.selected_path,
                source_sha256=schema_source.content_sha256,
            )
        except ConfigSchemaError as error:
            _emit(
                diagnostics,
                code="GENERATED_CONFIG_SCHEMA_INVALID",
                message=str(error),
                entity="config_schema",
                source=schema_source,
            )
            catalog = {"paths": {}, "summary": {}, "unresolved_references": [str(error)]}

        registry = extract_feature_registry(
            registry_source.text,
            source_path=registry_source.selected_path,
        )
        schema_policy = extract_feature_schema_policy(
            schema_generator_source.text,
            source_path=schema_generator_source.selected_path,
        )
        registry["schema_policy"] = schema_policy
        crosswalk = crosswalk_features(
            registry,
            catalog.get("paths") or {},
            schema_policy,
        )
        effect_links, missing_effect_paths = _effect_links(catalog)
        complete = True
        for ref in catalog.get("unresolved_references") or []:
            complete = False
            _emit(
                diagnostics,
                code="CONFIG_SCHEMA_REFERENCE_UNRESOLVED",
                message=f"Generated config schema contains an unresolved reference: {ref}",
                entity=f"config_schema.ref.{ref}",
                source=schema_source,
                details={"ref": ref},
            )
        for error in registry.get("parse_errors") or []:
            complete = False
            _emit(
                diagnostics,
                code=str(error.get("code") or "FEATURE_REGISTRY_PARSE_ERROR"),
                message=str(error.get("message") or "Feature registry parse failure"),
                entity="feature_registry",
                source=registry_source,
                details=copy.deepcopy(error),
            )
        for error in schema_policy.get("parse_errors") or []:
            complete = False
            _emit(
                diagnostics,
                code=str(error.get("code") or "FEATURE_SCHEMA_POLICY_PARSE_ERROR"),
                message=str(error.get("message") or "Feature schema policy parse failure"),
                entity="feature_schema_policy",
                source=schema_generator_source,
                details=copy.deepcopy(error),
            )
        for kind, values in (
            ("FEATURE_REGISTRY_DUPLICATE_ID", registry.get("duplicate_ids") or []),
            ("FEATURE_REGISTRY_DUPLICATE_KEY", registry.get("duplicate_keys") or []),
        ):
            if values:
                complete = False
                _emit(
                    diagnostics,
                    code=kind,
                    message=f"Feature registry contains duplicates: {', '.join(values)}",
                    entity="feature_registry",
                    source=registry_source,
                    details={"values": values},
                )
        for key in crosswalk.get("registered_without_root_schema") or []:
            complete = False
            _emit(
                diagnostics,
                code="REGISTERED_FEATURE_MISSING_FROM_CONFIG_SCHEMA",
                message=f"Registered feature is absent from root generated config schema: {key}",
                entity=f"feature.{key}",
                source=registry_source,
                details={"config_path": f"features.{key}"},
            )
        if missing_effect_paths:
            complete = False
            _emit(
                diagnostics,
                code="CONFIG_EFFECT_PATH_MISSING",
                message="One or more required config-to-surface paths are absent from the generated schema.",
                entity="config_effects",
                source=schema_source,
                details={"paths": missing_effect_paths},
            )

        required_paths = {
            "features.context_management",
            "features.context_management.experimental_mode",
            "features.token_budget.use_history_notes_extension",
            "features.tool_registry.turn_metadata_includes_tool_info",
            "openai_base_url",
            "chatgpt_base_url",
            "model_providers.*.experimental_bearer_token",
            "responses_api_metadata.*",
        }
        absent_required = sorted(required_paths - set(catalog.get("paths") or {}))
        if absent_required:
            complete = False
            _emit(
                diagnostics,
                code="CONFIG_FULL_PICTURE_ANCHOR_MISSING",
                message="Generated schema is missing config anchors required to connect existing protocol surfaces.",
                entity="config_surface.full_picture",
                source=schema_source,
                details={"paths": absent_required},
            )

        data: dict[str, Any] = {
            "$schema": "https://schemas.codex-wire-audit.invalid/v19/config-surface-semantics-v1.schema.json",
            "source_revision": snapshot.revision.to_dict(),
            "config_schema": catalog,
            "feature_registry": registry,
            "feature_crosswalk": crosswalk,
            "effect_links": effect_links,
            "surface_graph": {},
            "coverage": {
                "schema_path_count": (catalog.get("summary") or {}).get("config_path_count", 0),
                "feature_registry_count": (registry.get("counts") or {}).get("total", 0),
                "feature_schema_policy_count": (schema_policy.get("counts") or {}).get("explicit_branches", 0),
                "intentionally_omitted_feature_count": (crosswalk.get("counts") or {}).get("intentionally_omitted", 0),
                "embedded_feature_count": (crosswalk.get("counts") or {}).get("embedded", 0),
                "effect_link_count": len(effect_links),
                "missing_effect_paths": missing_effect_paths,
                "schema_only_feature_keys": crosswalk.get("schema_only_root_keys") or [],
                "effect_semantics": "canonical selected links; remaining legacy effects are connected during report composition",
            },
            "semantic_complete": complete,
        }
        data["surface_graph"] = compose_surface_graph(data, {}, None)
        data["semantic_digest"] = semantic_fingerprint({
            "config_schema": catalog,
            "feature_registry": registry,
            "feature_crosswalk": crosswalk,
            "effect_links": effect_links,
            "surface_graph": data["surface_graph"],
        })
        return ExtractorResult(
            extractor_id=EXTRACTOR_ID,
            schema_version=SCHEMA_VERSION,
            data=data,
            semantic_complete=complete,
            source_spec_ids=self.result_source_spec_ids,
        )


@register_extractor(EXTRACTOR_ID)
def _factory() -> ConfigEffectsExtractor:
    return ConfigEffectsExtractor()
