"""Canonical extraction for experimental context-window management.

This extractor treats activation, transport routing, rollover, History, Notes,
and thread-hint behavior as separate semantic domains.  That separation matters:
account eligibility is not the same thing as request authentication, and the
ChatGPT auxiliary base URL is not the model-provider base URL used by the
History/Notes extension.
"""

from __future__ import annotations

import re
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot, semantic_fingerprint
from .registry import ExtractorResult, register_extractor

from .context_management_support import (
    ESSENTIAL,
    EXPECTED_ENCRYPTED_ROUTES,
    EXPECTED_ROUTES,
    EXTRACTOR_ID,
    SCHEMA_VERSION,
    SOURCE_IDS,
    emit_missing as _emit_missing,
    evidence as _evidence,
    extract_routes as _extract_routes,
    require_tokens as _require_tokens,
    source as _source,
    text as _text,
)

def _activation(snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> tuple[dict[str, Any], bool]:
    source = _source(snapshot, "activation")
    if source is None:
        _emit_missing(
            diagnostics,
            code="CONTEXT_MANAGEMENT_ACTIVATION_SOURCE_UNAVAILABLE",
            message="Experimental context activation source is unavailable.",
            source_file=None,
            entity="context_management.activation",
        )
        return {}, False

    checks = (
        ("CONTEXT_MANAGEMENT_FEATURE_GATE_MISSING", "Feature::ContextManagement"),
        ("CONTEXT_MANAGEMENT_MODEL_CAPABILITY_GATE_MISSING", "starting_model.supports_experimental_context"),
        ("CONTEXT_MANAGEMENT_CODEX_ROUTE_GATE_MISSING", "provider.supports_codex_backend_routes()"),
        ("CONTEXT_MANAGEMENT_OPENAI_AUTH_GATE_MISSING", "provider.requires_openai_auth"),
        ("CONTEXT_MANAGEMENT_ENV_KEY_REJECTION_MISSING", "provider.env_key.is_some()"),
        ("CONTEXT_MANAGEMENT_BEARER_REJECTION_MISSING", "provider.experimental_bearer_token.is_some()"),
        ("CONTEXT_MANAGEMENT_CUSTOM_AUTH_REJECTION_MISSING", "provider.auth.is_some()"),
        ("CONTEXT_MANAGEMENT_AWS_REJECTION_MISSING", "provider.aws.is_some()"),
        ("CONTEXT_MANAGEMENT_TOKEN_BUDGET_GATE_MISSING", "Feature::TokenBudget"),
        ("CONTEXT_MANAGEMENT_HISTORY_NOTES_ENABLE_MISSING", ".use_history_notes_extension = true"),
    )
    complete = _require_tokens(diagnostics, source, checks, entity_prefix="context_management.activation")

    plan_match = re.search(
        r"Some\(PlanType::Plus\s*\|\s*PlanType::Pro\s*\|\s*PlanType::ProLite\)",
        source.text,
    )
    if not plan_match:
        complete = False
        _emit_missing(
            diagnostics,
            code="CONTEXT_MANAGEMENT_PLAN_ELIGIBILITY_DRIFT",
            message="Expected Plus/Pro/ProLite ChatGPT eligibility expression was not found.",
            source_file=source,
            entity="context_management.activation.plan_types",
        )

    auth_chatgpt = "auth_mode == AuthMode::Chatgpt" in source.text
    if not auth_chatgpt:
        complete = False
        _emit_missing(
            diagnostics,
            code="CONTEXT_MANAGEMENT_AUTH_MODE_DRIFT",
            message="Experimental context is no longer visibly restricted to AuthMode::Chatgpt.",
            source_file=source,
            entity="context_management.activation.auth_mode",
        )

    return {
        "feature": "context_management",
        "config_key": "features.context_management.experimental_mode",
        "account": {
            "auth_mode": "chatgpt",
            "plan_types": ["plus", "pro", "prolite"],
        },
        "model": {
            "capability_field": "supports_experimental_context",
            "required": True,
            "default_false": True,
        },
        "provider": {
            "supports_codex_backend_routes": True,
            "requires_openai_auth": True,
            "provider_credentials_must_be_absent": [
                "env_key",
                "experimental_bearer_token",
                "auth",
                "aws",
            ],
        },
        "effects": {
            "token_budget_enabled": True,
            "history_notes_extension_enabled": True,
            "new_context_tool_available_via_token_budget": True,
        },
        "evidence": _evidence(source, "apply_experimental_context"),
    }, complete


def _history_notes(snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> tuple[dict[str, Any], bool]:
    tools_source = _source(snapshot, "tools")
    backend_source = _source(snapshot, "backend")
    extension_source = _source(snapshot, "extension")
    if tools_source is None or backend_source is None or extension_source is None:
        for key, source in (("tools", tools_source), ("backend", backend_source), ("extension", extension_source)):
            if source is None:
                _emit_missing(
                    diagnostics,
                    code="HISTORY_NOTES_SOURCE_UNAVAILABLE",
                    message=f"History/Notes {key} source is unavailable.",
                    source_file=None,
                    entity=f"context_management.history_notes.{key}",
                    details={"source_spec_id": SOURCE_IDS[key]},
                )
        return {}, False

    observed_routes = _extract_routes(tools_source.text, extension_source.text, backend_source.text)
    expected = set(EXPECTED_ROUTES)
    observed = set(observed_routes)
    complete = True
    for route in sorted(expected - observed):
        complete = False
        _emit_missing(
            diagnostics,
            code="CONTEXT_MANAGEMENT_ROUTE_MISSING",
            message=f"Expected History/Notes route is missing: {route}",
            source_file=tools_source,
            entity=f"context_management.route.{route}",
            details={"route": route},
        )
    for route in sorted(observed - expected):
        complete = False
        _emit_missing(
            diagnostics,
            code="ALPHA_ROUTE_UNCLASSIFIED",
            message=f"A new History/Notes alpha route requires classification: {route}",
            source_file=tools_source,
            entity=f"context_management.route.{route}",
            details={"route": route},
        )

    backend_checks = (
        ("HISTORY_NOTES_POST_METHOD_MISSING", "build_request(Method::POST, path)"),
        ("HISTORY_NOTES_CONTEXT_SESSION_ID_MISSING", '"session_id": session_id'),
        ("HISTORY_NOTES_CONTEXT_AGENT_NAME_MISSING", '"current_agent_name": current_agent_name'),
        ("HISTORY_NOTES_TRUNCATION_HEADER_MISSING", "x-openai-tool-output-truncation-policy"),
        ("HISTORY_NOTES_ENCRYPTED_HEADER_MISSING", "x-openai-encrypted-tool-arguments"),
        ("HISTORY_NOTES_API_PROVIDER_MISSING", ".api_provider()"),
        ("HISTORY_NOTES_API_AUTH_MISSING", ".api_auth()"),
    )
    complete &= _require_tokens(
        diagnostics,
        backend_source,
        backend_checks,
        entity_prefix="context_management.history_notes.backend",
    )

    timeout_match = re.search(r"HISTORY_NOTES_BACKEND_TIMEOUT[^\n]*Duration::from_secs\((\d+)\)", backend_source.text)
    timeout_seconds = int(timeout_match.group(1)) if timeout_match else None
    if timeout_seconds != 35:
        complete = False
        _emit_missing(
            diagnostics,
            code="HISTORY_NOTES_TIMEOUT_DRIFT",
            message="History/Notes backend timeout is not the expected 35 seconds.",
            source_file=backend_source,
            entity="context_management.history_notes.timeout",
            details={"observed": timeout_seconds},
        )

    encrypted_observed = {
        route for route in EXPECTED_ENCRYPTED_ROUTES if f'"{route}"' in backend_source.text
    }
    if encrypted_observed != set(EXPECTED_ENCRYPTED_ROUTES):
        complete = False
        _emit_missing(
            diagnostics,
            code="ENCRYPTION_POLICY_DRIFT",
            message="Encrypted History/Notes argument route set changed.",
            source_file=backend_source,
            entity="context_management.history_notes.encrypted_routes",
            details={"observed": sorted(encrypted_observed), "expected": list(EXPECTED_ENCRYPTED_ROUTES)},
        )

    extension_checks = (
        ("HISTORY_NOTES_EXTENSION_FLAG_MISSING", "use_history_notes_extension"),
        ("HISTORY_NOTES_OPENAI_PROVIDER_GATE_MISSING", "config.model_provider.is_openai()"),
        ("HISTORY_NOTES_CODEX_AUTH_GATE_MISSING", "current_auth_uses_codex_backend()"),
        ("HISTORY_NOTES_PROVIDER_CLONE_MISSING", "config.model_provider.clone()"),
        ("THREAD_HINT_ROUTE_MISSING", '"alpha/notes/v2/thread_hint"'),
    )
    complete &= _require_tokens(
        diagnostics,
        extension_source,
        extension_checks,
        entity_prefix="context_management.history_notes.extension",
    )
    hint_limit_match = re.search(r"MAX_THREAD_HINT_BYTES:\s*usize\s*=\s*([0-9_]+)", extension_source.text)
    hint_max_bytes = int(hint_limit_match.group(1).replace("_", "")) if hint_limit_match else None

    notes_description = tools_source.text
    history_read_only = "History is read-only and eventually consistent" in notes_description
    notes_cross_agent = "may access other agents' notes" in notes_description
    notes_survive = "survive context-window transitions" in notes_description
    notes_eventual = "listings and searches are eventually consistent" in notes_description
    notes_read_after_write = "Note reads reflect successful writes immediately" in notes_description
    max_file_match = re.search(r"at or below ([0-9,]+) UTF-8 bytes", notes_description)
    max_file_bytes = int(max_file_match.group(1).replace(",", "")) if max_file_match else None

    test_source = _source(snapshot, "history_notes_tests")
    hint_fixture = None
    if test_source:
        fixture_match = re.search(
            r'Recent notes \(up to (\d+), most-recent first\):\\n- ([^"\\]+)',
            test_source.text,
        )
        if fixture_match:
            hint_fixture = {
                "kind": "recent_note_metadata_fixture",
                "max_recent_notes": int(fixture_match.group(1)),
                "example_entry": fixture_match.group(2),
                "semantic_scope": "client test fixture; server-side generation algorithm is not open-source",
                "evidence": _evidence(test_source, "THREAD_HINT"),
            }

    return {
        "routes": list(observed_routes),
        "tool_route_count": 9,
        "hidden_routes": ["alpha/notes/v2/thread_hint"],
        "request": {
            "method": "POST",
            "timeout_seconds": timeout_seconds,
            "context_fields": ["session_id", "current_agent_name"],
            "headers": [
                "x-openai-tool-output-truncation-policy",
                "x-openai-encrypted-tool-arguments",
            ],
            "encrypted_argument_routes": list(EXPECTED_ENCRYPTED_ROUTES),
            "auth_source": "current model provider api_auth",
            "provider_source": "config.model_provider cloned into HistoryNotesBackend",
        },
        "history": {
            "role": "normalized remote conversation recovery",
            "read_only": history_read_only,
            "eventual_consistency": history_read_only,
            "cross_agent_addressing": True,
        },
        "notes": {
            "role": "remote model-owned durable working memory",
            "virtual_paths_not_filesystem_paths": "Paths are virtual, not filesystem paths" in notes_description,
            "survives_context_window_transitions": notes_survive,
            "cross_agent_access": notes_cross_agent,
            "read_after_write_immediate": notes_read_after_write,
            "listing_search_eventual_consistency": notes_eventual,
            "max_file_bytes": max_file_bytes,
        },
        "thread_hint": {
            "route": "alpha/notes/v2/thread_hint",
            "max_bytes": hint_max_bytes,
            "injection_role": "bounded bootstrap hint for a fresh context window",
            "fixture_contract": hint_fixture,
            "not_proven_as_remote_compaction_summary": True,
        },
        "evidence": {
            "tools": _evidence(tools_source, "HistoryNotesAction"),
            "backend": _evidence(backend_source, "HistoryNotesBackend::call"),
            "extension": _evidence(extension_source, "HistoryNotesExtension"),
        },
    }, complete


def _routing(snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> tuple[dict[str, Any], bool]:
    provider_info = _source(snapshot, "provider_info")
    config_toml = _source(snapshot, "config_toml")
    extension = _source(snapshot, "extension")
    backend = _source(snapshot, "backend")
    if None in (provider_info, config_toml, extension, backend):
        _emit_missing(
            diagnostics,
            code="CONTEXT_MANAGEMENT_ROUTING_SOURCE_UNAVAILABLE",
            message="One or more routing sources are unavailable.",
            source_file=None,
            entity="context_management.routing",
        )
        return {}, False
    assert provider_info and config_toml and extension and backend

    complete = True
    checks = (
        (provider_info, "CODEX_ROUTE_SUFFIX_GATE_MISSING", "supports_codex_backend_routes"),
        (provider_info, "CODEX_ROUTE_SUFFIX_LITERAL_MISSING", "/backend-api/codex"),
        (config_toml, "OPENAI_BASE_URL_CONFIG_MISSING", "pub openai_base_url: Option<String>"),
        (config_toml, "CHATGPT_BASE_URL_CONFIG_MISSING", "pub chatgpt_base_url: Option<String>"),
        (extension, "HISTORY_NOTES_PROVIDER_CLONE_MISSING", "config.model_provider.clone()"),
        (backend, "HISTORY_NOTES_API_PROVIDER_MISSING", ".api_provider()"),
        (backend, "HISTORY_NOTES_API_AUTH_MISSING", ".api_auth()"),
    )
    for source, code, token in checks:
        if token not in source.text:
            complete = False
            _emit_missing(
                diagnostics,
                code=code,
                message=f"Context-management routing token is missing: {token}",
                source_file=source,
                entity=f"context_management.routing.{token}",
            )

    supports_suffix = "ends_with" in provider_info.text and "/backend-api/codex" in provider_info.text
    return {
        "model_provider_plane": {
            "top_level_override": "openai_base_url",
            "built_in_provider": "openai",
            "context_management_backend_suffix": "/backend-api/codex",
            "supports_custom_origin_when_suffix_matches": supports_suffix,
            "history_notes_follow_this_plane": True,
        },
        "chatgpt_auxiliary_plane": {
            "top_level_override": "chatgpt_base_url",
            "default_semantic_role": "ChatGPT-side backend requests as opposed to model-provider Responses transport",
            "history_notes_follow_this_plane": False,
        },
        "separation": {
            "activation_auth_identity": "AuthManager ChatGPT account",
            "history_notes_request_provider": "config.model_provider",
            "history_notes_request_auth": "model provider api_auth",
            "provider_credentials_rejected_by_official_activation": True,
        },
        "evidence": {
            "provider_info": _evidence(provider_info, "supports_codex_backend_routes"),
            "config_toml": _evidence(config_toml, "openai_base_url/chatgpt_base_url"),
        },
    }, complete


def _rollover(snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> tuple[dict[str, Any], bool]:
    handler = _source(snapshot, "new_context")
    spec = _source(snapshot, "new_context_spec")
    compact = _source(snapshot, "compact")
    if handler is None or spec is None or compact is None:
        _emit_missing(
            diagnostics,
            code="CONTEXT_ROLLOVER_SOURCE_UNAVAILABLE",
            message="New-context rollover source is unavailable.",
            source_file=None,
            entity="context_management.rollover",
        )
        return {}, False

    checks = (
        (spec, "NEW_CONTEXT_TOOL_NAME_MISSING", 'NEW_CONTEXT_WINDOW_TOOL_NAME: &str = "new_context"'),
        (spec, "NEW_CONTEXT_ENVIRONMENT_PRESERVATION_MISSING", "Does not clear, reset, or otherwise affect environment state"),
        (handler, "NEW_CONTEXT_REQUEST_MISSING", "request_new_context_window().await"),
        (handler, "NO_SUMMARY_ROLLOVER_MESSAGE_MISSING", "without summarizing conversation history"),
        (compact, "TOKEN_BUDGET_NO_SUMMARY_MISSING", "skips model/server summarization"),
        (compact, "TOKEN_BUDGET_FRESH_WINDOW_MISSING", "start_new_context_window"),
    )
    complete = True
    for source, code, token in checks:
        if token not in source.text:
            complete = False
            _emit_missing(
                diagnostics,
                code=code,
                message=f"Context rollover invariant is missing: {token}",
                source_file=source,
                entity=f"context_management.rollover.{token}",
            )

    return {
        "tool_name": "new_context",
        "model_can_request_rollover": True,
        "summarization": "none",
        "environment_state": "preserved",
        "manual_compaction_under_token_budget": "fresh_context_window_without_model_server_summary",
        "automatic_compaction_under_token_budget": "fresh_context_window_without_model_server_summary",
        "architectural_interpretation": {
            "classification": "derived_inference",
            "context_window": "disposable working set",
            "history_and_notes": "durable state outside the active model context",
            "retrieval": "demand-loaded through history/notes tools",
        },
        "evidence": {
            "tool_spec": _evidence(spec, "create_new_context_window_tool"),
            "handler": _evidence(handler, "NewContextWindowHandler"),
            "token_budget_compaction": _evidence(compact, "run_compact_task_inner"),
        },
    }, complete


def _feature_config(snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> tuple[dict[str, Any], bool]:
    feature_config = _source(snapshot, "feature_configs")
    feature_registry = _source(snapshot, "feature_registry")
    model_protocol = _source(snapshot, "model_protocol")
    if feature_config is None or feature_registry is None or model_protocol is None:
        _emit_missing(
            diagnostics,
            code="CONTEXT_MANAGEMENT_FEATURE_SOURCE_UNAVAILABLE",
            message="Feature configuration/model capability source is unavailable.",
            source_file=None,
            entity="context_management.feature",
        )
        return {}, False

    checks = (
        (feature_config, "CONTEXT_MANAGEMENT_TOML_FIELD_MISSING", "pub experimental_mode: Option<bool>"),
        (feature_registry, "CONTEXT_MANAGEMENT_FEATURE_REGISTRY_MISSING", "ContextManagement"),
        (feature_registry, "CONTEXT_MANAGEMENT_FEATURE_KEY_MISSING", 'key: "context_management"'),
        (model_protocol, "EXPERIMENTAL_CONTEXT_MODEL_CAPABILITY_MISSING", "pub supports_experimental_context: bool"),
    )
    complete = True
    for source, code, token in checks:
        if token not in source.text:
            complete = False
            _emit_missing(
                diagnostics,
                code=code,
                message=f"Feature/model capability contract token is missing: {token}",
                source_file=source,
                entity=f"context_management.feature.{token}",
            )

    default_false = "#[serde(default)]" in model_protocol.text and "pub supports_experimental_context: bool" in model_protocol.text
    under_development = bool(re.search(
        r'id:\s*Feature::ContextManagement,\s*key:\s*"context_management",\s*stage:\s*Stage::UnderDevelopment,\s*default_enabled:\s*false',
        feature_registry.text,
        re.S,
    ))
    if not under_development:
        complete = False
        _emit_missing(
            diagnostics,
            code="CONTEXT_MANAGEMENT_FEATURE_STAGE_DRIFT",
            message="ContextManagement is no longer observed as under-development/default-off.",
            source_file=feature_registry,
            entity="context_management.feature.stage",
        )

    return {
        "toml": {
            "path": "features.context_management.experimental_mode",
            "type": "optional_boolean",
        },
        "feature_registry": {
            "key": "context_management",
            "stage": "under_development",
            "default_enabled": False,
        },
        "model_capability": {
            "field": "supports_experimental_context",
            "serde_default": default_false,
            "default_value": False,
            "activation_scope": "starting model at session startup",
        },
        "evidence": {
            "feature_config": _evidence(feature_config, "ContextManagementConfigToml"),
            "feature_registry": _evidence(feature_registry, "Feature::ContextManagement"),
            "model_protocol": _evidence(model_protocol, "ModelInfo.supports_experimental_context"),
        },
    }, complete


class ContextManagementExtractor:
    extractor_id = EXTRACTOR_ID
    source_spec_ids = tuple(SOURCE_IDS.values())

    def extract(self, snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> ExtractorResult:
        missing_essential = [key for key in ESSENTIAL if _source(snapshot, key) is None]
        for key in missing_essential:
            _emit_missing(
                diagnostics,
                code="CONTEXT_MANAGEMENT_SOURCE_UNAVAILABLE",
                message=f"Required context-management source is unavailable: {SOURCE_IDS[key]}",
                source_file=None,
                entity=SOURCE_IDS[key],
                details={"source_spec_id": SOURCE_IDS[key]},
            )

        feature, feature_ok = _feature_config(snapshot, diagnostics)
        activation, activation_ok = _activation(snapshot, diagnostics)
        history_notes, history_ok = _history_notes(snapshot, diagnostics)
        routing, routing_ok = _routing(snapshot, diagnostics)
        rollover, rollover_ok = _rollover(snapshot, diagnostics)

        semantic_complete = not missing_essential and all(
            (feature_ok, activation_ok, history_ok, routing_ok, rollover_ok)
        )
        data: dict[str, Any] = {
            "$schema": "https://schemas.codex-wire-audit.invalid/v16/context-management-semantics-v1.schema.json",
            "source_revision": snapshot.revision.to_dict(),
            "feature": feature,
            "activation": activation,
            "history_notes": history_notes,
            "routing": routing,
            "rollover": rollover,
            "semantic_complete": semantic_complete,
        }
        data["semantic_digest"] = semantic_fingerprint({
            "feature": feature,
            "activation": activation,
            "history_notes": history_notes,
            "routing": routing,
            "rollover": rollover,
        })
        return ExtractorResult(
            extractor_id=self.extractor_id,
            schema_version=SCHEMA_VERSION,
            data=data,
            semantic_complete=semantic_complete,
            source_spec_ids=self.source_spec_ids,
        )


@register_extractor(EXTRACTOR_ID)
def _factory() -> ContextManagementExtractor:
    return ContextManagementExtractor()
