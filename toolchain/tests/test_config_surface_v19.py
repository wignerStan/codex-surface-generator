from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from codex_wire_audit.config_feature_registry import (
    crosswalk_features,
    extract_feature_registry,
    extract_feature_schema_policy,
)
from codex_wire_audit.config_schema_catalog import ConfigSchemaError, build_config_schema_catalog
from codex_wire_audit.diagnostics import DiagnosticCollector
from codex_wire_audit.extractors import create_extractors
from codex_wire_audit.extractors.config_effects import ConfigEffectsExtractor, SOURCE_IDS
from codex_wire_audit.legacy import load_legacy_modules
from codex_wire_audit.models import SourceFile, SourceRevision, SourceSnapshot
from codex_wire_audit.proof_profiles import resolve_profile
from codex_wire_audit.source_registry import SourceRegistry, from_legacy_maps
from codex_wire_audit.surface_graph import compose_surface_graph


FEATURE_KEYS = ("context_management", "token_budget", "tool_registry")


def _feature_properties() -> dict[str, object]:
    return {
        "context_management": {"$ref": "#/definitions/FeatureToml_for_ContextManagementConfigToml"},
        "token_budget": {"$ref": "#/definitions/FeatureToml_for_TokenBudgetConfigToml"},
        "tool_registry": {"$ref": "#/definitions/ToolRegistryConfigToml"},
    }


def _schema() -> dict[str, object]:
    profile_properties = {
        "features": {
            "type": "object",
            "additionalProperties": False,
            "properties": _feature_properties(),
        },
        "model": {"type": "string"},
        "model_provider": {"type": "string"},
        "model_reasoning_effort": {"type": "string"},
        "model_reasoning_summary": {"type": "string"},
        "model_verbosity": {"type": "string"},
        "service_tier": {"type": "string"},
        "web_search": {"type": "string"},
    }
    properties: dict[str, object] = dict(profile_properties)
    properties.update({
        "chatgpt_base_url": {"type": "string", "description": "ChatGPT auxiliary base"},
        "openai_base_url": {"type": "string", "description": "OpenAI provider override"},
        "responses_api_metadata": {"type": "object", "additionalProperties": {"type": "string"}},
        "mcp_servers": {"type": "object", "additionalProperties": {"type": "object"}},
        "model_providers": {
            "type": "object",
            "additionalProperties": {"$ref": "#/definitions/ModelProviderInfo"},
        },
        "profiles": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": profile_properties,
            },
        },
    })
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "ConfigToml",
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "definitions": {
            "ContextManagementConfigToml": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"experimental_mode": {"type": "boolean"}},
            },
            "FeatureToml_for_ContextManagementConfigToml": {
                "anyOf": [
                    {"type": "boolean"},
                    {"$ref": "#/definitions/ContextManagementConfigToml"},
                ]
            },
            "TokenBudgetConfigToml": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "enabled": {"type": "boolean"},
                    "use_history_notes_extension": {"type": "boolean"},
                },
            },
            "FeatureToml_for_TokenBudgetConfigToml": {
                "anyOf": [
                    {"type": "boolean"},
                    {"$ref": "#/definitions/TokenBudgetConfigToml"},
                ]
            },
            "ToolRegistryConfigToml": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"turn_metadata_includes_tool_info": {"type": "boolean"}},
            },
            "ModelProviderInfo": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "base_url": {"type": "string"},
                    "env_key": {"type": "string"},
                    "experimental_bearer_token": {"type": "string"},
                    "auth": {"type": "object"},
                    "aws": {"type": "object"},
                    "requires_openai_auth": {"type": "boolean", "default": False},
                },
            },
        },
    }


FEATURE_SOURCE = """
pub struct FeatureSpec {
    pub id: Feature,
}

fn info(self) -> &'static FeatureSpec {
    todo!()
}

pub const FEATURES: &[FeatureSpec] = &[
    FeatureSpec { id: Feature::ContextManagement, key: "context_management", stage: Stage::UnderDevelopment, default_enabled: false },
    FeatureSpec { id: Feature::TokenBudget, key: "token_budget", stage: Stage::UnderDevelopment, default_enabled: false },
    FeatureSpec { id: Feature::ToolRegistry, key: "tool_registry", stage: Stage::Stable, default_enabled: true },
];
"""

FEATURE_SCHEMA_SOURCE = r"""
pub fn features_schema(schema_gen: &mut SchemaGenerator) -> Schema {
    for feature in FEATURES {
        if feature.id == codex_features::Feature::Artifact {
            continue;
        }
        if feature.id == codex_features::Feature::GuardianThreadContext {
            // This setting is already part of the guardianv2 feature table.
            continue;
        }
        if feature.id == codex_features::Feature::ContextManagement {
            validation.properties.insert(
                feature.key.to_string(),
                schema_gen.subschema_for::<codex_features::FeatureToml<
                    codex_features::ContextManagementConfigToml,
                >>(),
            );
            continue;
        }
        validation
            .properties
            .insert(feature.key.to_string(), schema_gen.subschema_for::<bool>());
    }
    Schema::Object(object)
}
"""


def _registry() -> SourceRegistry:
    legacy = load_legacy_modules()
    return from_legacy_maps(legacy.base.FILES, legacy.base.SURFACE_FILES, legacy.entrypoint.EXTRA)


def _snapshot(schema: dict[str, object] | None = None, feature_source: str = FEATURE_SOURCE) -> SourceSnapshot:
    registry = _registry()
    values = {
        SOURCE_IDS["generated_schema"]: json.dumps(schema or _schema(), sort_keys=True),
        SOURCE_IDS["feature_registry"]: feature_source,
        SOURCE_IDS["schema_generator"]: FEATURE_SCHEMA_SOURCE,
        SOURCE_IDS["config_toml"]: "pub struct ConfigToml {}",
        SOURCE_IDS["core_config"]: "pub struct Config {}",
        SOURCE_IDS["provider_info"]: "pub struct ModelProviderInfo {}",
        SOURCE_IDS["turn_metadata"]: "pub struct CodexResponsesMetadata {}",
        SOURCE_IDS["activation"]: "fn apply_experimental_context() {}",
    }
    files: dict[str, SourceFile] = {}
    for spec_id, text in values.items():
        spec = registry.get(spec_id)
        files[spec_id] = SourceFile.create(
            spec=spec,
            selected_path=spec.primary_path,
            raw_bytes=text.encode("utf-8"),
        )
    revision = SourceRevision(
        source_mode="fixture",
        repository="openai/codex",
        requested_ref="fixture",
        resolved_commit_sha="fixture",
        source_set_sha256=SourceSnapshot.digest_files(files),
        dirty=False,
    )
    return SourceSnapshot(revision=revision, files=files)


def test_generated_schema_catalog_resolves_refs_maps_and_profiles() -> None:
    text = json.dumps(_schema(), sort_keys=True)
    catalog = build_config_schema_catalog(text, source_path="config.schema.json", source_sha256="a" * 64)
    paths = catalog["paths"]
    assert "features.context_management.experimental_mode" in paths
    assert "profiles.*.features.context_management.experimental_mode" in paths
    assert "model_providers.*.experimental_bearer_token" in paths
    assert "responses_api_metadata.*" in paths
    assert catalog["summary"]["root_property_count"] >= 10
    assert catalog["summary"]["unresolved_reference_count"] == 0


def test_generated_schema_catalog_rejects_unresolved_ref() -> None:
    schema = _schema()
    schema["properties"]["broken"] = {"$ref": "#/definitions/Missing"}  # type: ignore[index]
    catalog = build_config_schema_catalog(json.dumps(schema), source_path="config.schema.json", source_sha256="b" * 64)
    assert catalog["unresolved_references"] == ["#/definitions/Missing"]


def test_feature_registry_crosswalks_root_and_profile_features() -> None:
    registry = extract_feature_registry(FEATURE_SOURCE, source_path="features/src/lib.rs")
    catalog = build_config_schema_catalog(json.dumps(_schema()), source_path="config.schema.json", source_sha256="c" * 64)
    crosswalk = crosswalk_features(registry, catalog["paths"])
    assert crosswalk["registered_without_root_schema"] == []
    assert crosswalk["registered_without_profile_schema"] == []
    assert crosswalk["counts"]["fully_crosswalked"] == len(FEATURE_KEYS)


def test_feature_registry_ignores_type_and_function_braces_outside_registry() -> None:
    registry = extract_feature_registry(FEATURE_SOURCE, source_path="features/src/lib.rs")
    assert registry["parse_errors"] == []
    assert [feature["id"] for feature in registry["features"]] == [
        "ContextManagement",
        "TokenBudget",
        "ToolRegistry",
    ]


def test_feature_schema_policy_accounts_for_omitted_and_embedded_features() -> None:
    schema = _schema()
    features = schema["properties"]["features"]["properties"]  # type: ignore[index]
    profiles = schema["properties"]["profiles"]["additionalProperties"]["properties"]["features"]["properties"]  # type: ignore[index]
    features["guardianv2"] = {
        "type": "object",
        "properties": {"thread_context": {"type": "boolean"}},
    }
    profiles["guardianv2"] = features["guardianv2"]
    source = FEATURE_SOURCE.replace(
        'FeatureSpec { id: Feature::ToolRegistry, key: "tool_registry", stage: Stage::Stable, default_enabled: true },',
        'FeatureSpec { id: Feature::ToolRegistry, key: "tool_registry", stage: Stage::Stable, default_enabled: true },\n'
        '    FeatureSpec { id: Feature::Artifact, key: "artifact", stage: Stage::UnderDevelopment, default_enabled: false },\n'
        '    FeatureSpec { id: Feature::GuardianThreadContext, key: "guardianv2.thread_context", stage: Stage::UnderDevelopment, default_enabled: false },',
    )
    registry = extract_feature_registry(source, source_path="features/src/lib.rs")
    policy = extract_feature_schema_policy(FEATURE_SCHEMA_SOURCE, source_path="config/src/schema.rs")
    catalog = build_config_schema_catalog(
        json.dumps(schema), source_path="config.schema.json", source_sha256="d" * 64
    )
    crosswalk = crosswalk_features(registry, catalog["paths"], policy)
    assert crosswalk["registered_without_root_schema"] == []
    assert crosswalk["registered_without_profile_schema"] == []
    assert crosswalk["intentionally_omitted_keys"] == ["artifact"]
    assert crosswalk["embedded_feature_keys"] == ["guardianv2.thread_context"]
    by_key = {row["config_key"]: row for row in crosswalk["features"]}
    assert by_key["artifact"]["schema_representation"] == "intentionally_not_user_configurable"
    assert by_key["guardianv2.thread_context"]["schema_representation"] == "embedded_in_structured_feature"


def test_config_effects_extractor_builds_schema_and_selected_links() -> None:
    diagnostics = DiagnosticCollector()
    result = ConfigEffectsExtractor().extract(_snapshot(), diagnostics)
    assert result.semantic_complete
    assert diagnostics.summary()["error"] == 0
    assert result.data["config_schema"]["summary"]["config_path_count"] > 20
    assert result.data["feature_crosswalk"]["counts"]["registered"] == 3
    assert result.data["feature_registry"]["schema_policy"]["fallback"]["action"] == "boolean_schema"
    assert result.data["surface_graph"]["coverage"]["schema_policy_count"] == 3
    targets = {row["target"] for row in result.data["effect_links"]}
    assert "surface.context_management.activation" in targets
    assert "surface.turn_metadata.tool_namespaces_info" in targets
    assert result.data["surface_graph"]["coverage"]["unresolved_node_refs"] == []


def test_config_effects_fails_visible_when_full_picture_anchor_disappears() -> None:
    schema = _schema()
    del schema["properties"]["openai_base_url"]  # type: ignore[index]
    diagnostics = DiagnosticCollector()
    result = ConfigEffectsExtractor().extract(_snapshot(schema), diagnostics)
    assert not result.semantic_complete
    codes = {item.code for item in diagnostics.values()}
    assert "CONFIG_FULL_PICTURE_ANCHOR_MISSING" in codes
    assert "CONFIG_EFFECT_PATH_MISSING" in codes


def test_surface_graph_connects_canonical_and_legacy_effects() -> None:
    diagnostics = DiagnosticCollector()
    result = ConfigEffectsExtractor().extract(_snapshot(), diagnostics)
    legacy = {
        "config_protocol": {
            "wire_affecting_settings": [
                {
                    "setting": "model",
                    "wire_effects": [
                        {"layer": "request_body", "path": "ResponsesApiRequest.model", "behavior": "select model"}
                    ],
                }
            ]
        }
    }
    graph = compose_surface_graph(result.data, {}, legacy)
    assert graph["coverage"]["legacy_effect_count"] == 1
    assert graph["views"]["by_config"]["config.model"]
    assert graph["views"]["by_surface"]["surface.context_management.activation"]
    assert graph["views"]["by_feature"]["feature.context_management"]
    assert graph["coverage"]["schema_policy_count"] == len(FEATURE_KEYS)
    assert graph["coverage"]["unresolved_node_refs"] == []


def test_config_surface_schema_validates_extracted_contract() -> None:
    diagnostics = DiagnosticCollector()
    result = ConfigEffectsExtractor().extract(_snapshot(), diagnostics)
    schema_path = Path(__file__).parents[1] / "codex_wire_audit" / "proof_schema_templates" / "config-surface-semantics-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = list(Draft202012Validator(schema).iter_errors(result.data))
    assert errors == []


def test_config_effects_extractor_source_and_profile_registration() -> None:
    ids = {extractor.extractor_id for extractor in create_extractors()}
    assert "extractor.config_effects" in ids
    registry = _registry()
    schema_spec = registry.get("source_spec.extra.generated_config_schema")
    assert schema_spec.primary_path == "codex-rs/core/config.schema.json"
    assert "extractor.config_effects" in schema_spec.extractor_ids
    generator_spec = registry.get("source_spec.extra.config_schema_generator")
    assert generator_spec.primary_path == "codex-rs/config/src/schema.rs"
    assert "extractor.config_effects" in generator_spec.extractor_ids
    profile = resolve_profile("config_surface_only")
    assert profile.required_extractors == ("extractor.config_effects",)
    full = resolve_profile("codex_wire_full")
    assert "extractor.config_effects" in full.required_extractors
    assert "config_schema_surface_graph" in full.required_runtime_scenarios
