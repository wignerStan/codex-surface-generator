#!/usr/bin/env python3
"""Focused regression tests for the Codex wire-audit v10 generators and machine contract."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load_module("codex_wire_audit_base_v10_tests", ROOT / "_codex_wire_audit_base_v10.py")
W = load_module("codex_wire_audit_v10_tests", ROOT / "codex_wire_audit_v10.py")
C = load_module("codex_wire_contract_v10_tests", ROOT / "_codex_wire_contract_v10.py")


class CoverageTests(unittest.TestCase):
    def test_endpoint_module_coverage_flags_only_unknown_addressable_modules(self) -> None:
        endpoint_mod = """
        pub(crate) mod models;
        pub(crate) mod responses;
        mod session;
        pub mod future_transport;
        pub use models::ModelsClient;
        """
        surfaces = [
            {"id": "models_list", "headers": {"response_names": []}, "response": {}},
            {"id": "responses_http_sse", "headers": {"response_names": []}, "response": {}},
        ]
        coverage = B.build_coverage_contract(
            surfaces,
            {"endpoint_mod": endpoint_mod},
            {},
        )
        self.assertEqual(coverage["unclassified_endpoint_modules"], ["future_transport"])
        by_module = {
            item["module"]: item for item in coverage["endpoint_module_classification"]
        }
        self.assertTrue(by_module["models"]["classified"])
        self.assertIsNotNone(by_module["session"]["exclusion"])

    def test_response_header_discovery_handles_literals_and_constants(self) -> None:
        source = r'''
        const REQUEST_ID_HEADER: &str = "x-request-id";
        const SAFETY_HEADER: &str = "x-codex-safety-buffering-enabled";
        fn read(headers: &HeaderMap, response: &Response) {
            let _ = headers.get(REQUEST_ID_HEADER);
            let _ = headers.contains_key(SAFETY_HEADER);
            let _ = parse_header_str(headers, "x-codex-promo-message");
            let _ = response.headers().get("cf-ray");
        }
        '''
        self.assertEqual(
            B.discover_response_header_candidates({"source": source}),
            [
                "cf-ray",
                "x-codex-promo-message",
                "x-codex-safety-buffering-enabled",
                "x-request-id",
            ],
        )

    def test_response_event_discovery(self) -> None:
        source = '''
        match kind {
            "response.created" => {},
            "response.completed" => {},
            "codex.rate_limits" => {},
            "not.an.event" => {},
        }
        '''
        self.assertEqual(
            B.discover_response_event_kinds(source),
            ["codex.rate_limits", "response.completed", "response.created"],
        )


class RustParsingTests(unittest.TestCase):
    def test_matching_brace_ignores_literals_comments_and_lifetimes(self) -> None:
        source = r'''
        fn demo<'a>(value: &'a str) {
            let normal = "}";
            let raw = r###"{ still text }"###;
            let ch = '}';
            // } ignored
            /* { nested /* } */ ignored } */
            if true { println!("ok"); }
        }
        '''
        open_index = source.index("{")
        close_index = B.find_rust_matching_brace(source, open_index)
        self.assertEqual(source[close_index], "}")
        self.assertEqual(source[close_index + 1:].strip(), "")

    def test_struct_fields_support_multiline_attributes_and_types(self) -> None:
        source = """
        struct Demo<'a> {
            #[serde(
                default,
                rename = "wire_value",
                skip_serializing_if = "Option::is_none"
            )]
            pub value: Option<
                BTreeMap<String, Vec<&'a str>>
            >,
            #[serde(flatten)]
            extra: &'a BTreeMap<String, String>,
        }
        """
        fields = B.struct_fields(source, "demo.rs", "Demo")
        self.assertEqual([field["name"] for field in fields], ["value", "extra"])
        self.assertEqual(fields[0]["wire_name"], "wire_value")
        self.assertTrue(fields[0]["optional"] and fields[0]["serde_default"])
        self.assertTrue(fields[1]["flattened"])


class CatalogShapeTests(unittest.TestCase):
    def test_surface_keeps_templates_events_trust_and_lifecycle(self) -> None:
        surface = B._surface(
            surface_id="demo",
            category="test",
            endpoints=["/demo"],
            method="GET",
            transport="HTTPS",
            protocol="unary",
            streaming=False,
            request_content_type=None,
            request_accept=None,
            request_framing="none",
            response_media_type="application/json",
            response_framing="one body",
            body_schema=None,
            response_header_templates=[
                {
                    "name_template": "x-{id}-used",
                    "condition": "when present",
                    "parameters": {"id": "limit"},
                    "value_shape": "number",
                    "direction": "response",
                    "origin": "server",
                    "category": "rate_limit",
                    "source": None,
                }
            ],
            response_event_metadata=[{"event": "demo.event"}],
            trust_domain="demo service",
        )
        self.assertEqual(surface["trust_domain"], "demo service")
        self.assertEqual(surface["lifecycle"]["connection_lifetime"], "request-scoped")
        self.assertEqual(
            surface["headers"]["response_templates"][0]["name_template"],
            "x-{id}-used",
        )
        self.assertEqual(surface["response"]["event_metadata"][0]["event"], "demo.event")

    def test_response_metadata_inventory_marks_encoded_diagnostics_sensitive(self) -> None:
        rate = '''
        let prefix = format!("x-{normalized_limit}");
        let limit_name_header = format!("{prefix}-limit-name");
        parse_header_str(headers, "x-codex-promo-message");
        parse_header_str(headers, "x-codex-rate-limit-reached-type");
        parse_header_bool(headers, "x-codex-credits-has-credits");
        parse_header_bool(headers, "x-codex-credits-unlimited");
        parse_header_str(headers, "x-codex-credits-balance");
        if event.kind != "codex.rate_limits" {}
        '''
        inventory = B.build_response_metadata_inventory(
            '''
            openai_verification_recommendation
            openai_chatgpt_moderation_metadata
            fn safety_buffering() {}
            GUARDIAN_TICKET_HEADER
            "response.created" "response.completed"
            ''',
            {
                "response_rate_limits": rate,
                "safety_buffering": "X_CODEX_SAFETY_BUFFERING_ENABLED_HEADER X_CODEX_SAFETY_BUFFERING_FASTER_MODEL_HEADER",
                "api_bridge": "ACTIVE_LIMIT_HEADER",
                "response_debug_context": "OAI_REQUEST_ID_HEADER CF_RAY_HEADER AUTH_ERROR_HEADER X_ERROR_JSON_HEADER",
            },
        )
        by_name = {item["name"]: item for item in inventory["headers"]}
        self.assertTrue(by_name["x-error-json"]["sensitive"])
        self.assertEqual(by_name["x-error-json"]["value_encoding"], "base64")
        self.assertEqual(len(inventory["header_templates"]), 2)
        self.assertIn("response.completed", inventory["event_kinds"])


class ReportAssemblyTests(unittest.TestCase):
    def test_base_report_uses_v10_schema_determinism_and_lightweight_alias(self) -> None:
        saved = {
            "src": B.src,
            "struct_fields": B.struct_fields,
            "client_metadata_keys": B.client_metadata_keys,
            "discover_header_candidates": B.discover_header_candidates,
            "turn_metadata_construction": B.turn_metadata_construction,
        }
        try:
            B.src = lambda _text, path, _needle, symbol=None, start=0: {
                "path": path, "line": 1, "symbol": symbol or "fixture"
            }
            B.struct_fields = lambda _text, _path, _name: []
            B.client_metadata_keys = lambda *_args, **_kwargs: (
                [
                    {"name": "x-codex-installation-id", "optional": False, "condition": "always", "source": {}},
                    {"name": "session_id", "optional": False, "condition": "always", "source": {}},
                    {"name": "thread_id", "optional": False, "condition": "always", "source": {}},
                    {"name": "x-codex-window-id", "optional": False, "condition": "always", "source": {}},
                ],
                [],
                [],
            )
            B.discover_header_candidates = lambda _sources: []
            B.turn_metadata_construction = lambda _metadata: {"fields": [], "warnings": []}
            sources = {key: "fixture" for key in B.FILES}
            sources["account"] = "/api/codex/accounts/check /wham/accounts/check"
            sources["provider"] = "pub headers: HeaderMap"
            sources["response_sse"] = "response_id: resp.id"
            report = B.build_report(
                "openai/codex",
                "fixture",
                {"sha": "abc", "date": "2026-01-02T03:04:05Z", "message": "fixture"},
                sources,
                surface_sources={},
                deterministic=True,
            )
        finally:
            for name, value in saved.items():
                setattr(B, name, value)
        self.assertEqual(report["schema_version"], 10)
        self.assertEqual(report["generated_at"], "2026-01-02T03:04:05Z")
        self.assertIn("wire_surface_catalog", report)
        self.assertEqual(report["endpoint_protocol_catalog"]["$ref"], "#/wire_surface_catalog")
        self.assertNotIn("surfaces", report["endpoint_protocol_catalog"])


class WrapperInventoryTests(unittest.TestCase):
    def test_bidirectional_app_server_macro_inventory(self) -> None:
        source = r'''
        client_request_definitions! {
            Initialize => "initialize" {
                params: v1::InitializeParams,
                serialization: None,
                response: v1::InitializeResponse,
            },
        }
        server_request_definitions! {
            LegacyApproval {
                params: v1::LegacyApprovalParams,
                response: v1::LegacyApprovalResponse,
            },
            Approval => "item/requestApproval" {
                params: v2::ApprovalParams,
                response: v2::ApprovalResponse,
            },
        }
        server_notification_definitions! {
            Error => "error" (v2::ErrorNotification),
            ThreadStarted(v2::ThreadStartedNotification),
        }
        client_notification_definitions! {
            Initialized,
            Ping(v2::PingNotification),
        }
        '''
        inventory = W.app_server_method_inventory(source)
        self.assertTrue(inventory["exhaustive_for_source_macros"])
        self.assertEqual(
            [item["method"] for item in inventory["server_requests"]],
            ["legacyApproval", "item/requestApproval"],
        )
        self.assertEqual(
            [item["method"] for item in inventory["server_notifications"]],
            ["error", "threadStarted"],
        )
        self.assertEqual(inventory["counts"]["client_notifications"], 2)

    def test_inventory_warns_when_a_present_macro_cannot_be_extracted(self) -> None:
        inventory = W.app_server_method_inventory(
            "client_notification_definitions! { /* malformed fixture */ }"
        )
        self.assertFalse(inventory["exhaustive_for_source_macros"])
        self.assertIn(
            "client_notification_definitions! inventory was not extracted",
            inventory["warnings"],
        )

    def test_relation_map_contains_guardian_receipt_and_model_validator(self) -> None:
        concepts = {item["concept"] for item in W.relation_map()["groups"]}
        self.assertIn("guardian_ticket_lifecycle", concepts)
        self.assertIn("model_catalog_validator", concepts)


class V10ProtocolExpansionTests(unittest.TestCase):
    def test_referer_is_redirect_derived_not_first_hop(self) -> None:
        route = r'''
        use http::header::REFERER;
        pub(super) fn insert_referer(headers: &mut HeaderMap, previous: &Url, next: &Url) {
            headers.remove(REFERER);
            if next.scheme() == "http" && previous.scheme() == "https" { return; }
            let mut referer = previous.clone();
            let _ = referer.set_username("");
            let _ = referer.set_password(None);
            referer.set_fragment(None);
            if !same_origin(previous, next) { referer.set_path("/"); referer.set_query(None); }
            if let Ok(value) = referer.as_str().parse() { headers.insert(REFERER, value); }
        }
        '''
        mcp = r'''
        pub(crate) struct SameOriginRedirectHttpClient;
        fn f(params: &mut Params, current_url: &Url, next_url: &Url, original_origin: Origin) {
            if next_url.origin() != original_origin { return; }
            let _ = "MCP HTTP redirects for non-loopback hostnames require HTTPS";
            params.headers.retain(|header| !header.name.eq_ignore_ascii_case("referer"));
            let mut referer = current_url.clone();
            let _ = referer.set_username("");
            let _ = referer.set_password(None);
            referer.set_fragment(None);
            params.headers.push(HttpHeader { name: "referer".to_string(), value: referer.to_string(), value_env_var: None });
        }
        '''
        helper = r'''
        if matches!(name.as_str(), "referer") {
            return Err(anyhow!("MCP HTTP headers helper returned a reserved header"));
        }
        '''
        protocol = W.build_redirect_header_protocol({
            "route_aware_redirect": route,
            "mcp_http_redirect": mcp,
            "mcp_http_headers": helper,
        })
        self.assertFalse(protocol["header"]["first_request_generated"])
        self.assertTrue(protocol["header"]["redirect_request_generated"])
        self.assertEqual(len(protocol["profiles"]), 2)
        general = protocol["profiles"][0]
        self.assertEqual(general["https_to_http_downgrade"], "omit Referer")
        self.assertIn("origin root", general["cross_origin_value"])
        mcp_profile = protocol["profiles"][1]
        self.assertIn("reject", mcp_profile["redirect_origin_policy"])
        self.assertFalse(protocol["mcp_helper_policy"]["http_headers_helper_may_emit_referer"])

    def test_enum_summary_handles_multiline_variants_and_serde_names(self) -> None:
        source = r'''
        #[derive(Serialize)]
        #[serde(tag = "type", content = "payload", rename_all = "snake_case")]
        enum Demo {
            PlainValue,
            #[serde(rename = "renamed", alias = "old_name")]
            TupleValue(
                Option<Vec<String>>,
            ),
            StructValue {
                foo_bar: Option<String>,
                count: u64,
            },
        }
        '''
        schema = W.enum_summary(source, "demo.rs", "Demo")
        self.assertEqual(schema["tag"], "type")
        self.assertEqual(schema["content"], "payload")
        by_name = {row["name"]: row for row in schema["variants"]}
        self.assertEqual(by_name["PlainValue"]["wire_name"], "plain_value")
        self.assertEqual(by_name["TupleValue"]["wire_name"], "renamed")
        self.assertEqual(by_name["TupleValue"]["aliases"], ["old_name"])
        self.assertEqual(by_name["StructValue"]["shape"], "struct")
        self.assertEqual(
            [field["name"] for field in by_name["StructValue"]["fields"]],
            ["foo_bar", "count"],
        )

    def test_feature_registry_extracts_stage_default_and_structured_fields(self) -> None:
        source = r'''
        enum Feature { Mcp20260728, EnableRequestCompression }
        struct FeatureSpec { id: Feature, key: &'static str, stage: Stage, default_enabled: bool }
        pub const FEATURES: &[FeatureSpec] = &[
            FeatureSpec {
                id: Feature::Mcp20260728,
                key: "mcp_2026_07_28",
                stage: Stage::UnderDevelopment,
                default_enabled: false,
            },
            FeatureSpec {
                id: Feature::EnableRequestCompression,
                key: "enable_request_compression",
                stage: Stage::Stable,
                default_enabled: true,
            },
        ];
        struct FeaturesToml {
            #[serde(default)]
            apps: Option<bool>,
            #[serde(flatten)]
            legacy: BTreeMap<String, bool>,
        }
        '''
        result = W.feature_registry_summary(source)
        self.assertEqual(result["counts"]["total"], 2)
        self.assertEqual(result["by_id"]["Mcp20260728"]["key"], "mcp_2026_07_28")
        self.assertFalse(result["by_id"]["Mcp20260728"]["default_enabled"])
        self.assertEqual(result["by_id"]["EnableRequestCompression"]["stage"], "Stable")
        self.assertEqual([f["name"] for f in result["features_toml_fields"]], ["apps", "legacy"])

    def test_match_arm_inventory_supports_block_arms_without_commas(self) -> None:
        source = r'''
        pub fn process_responses_event(event: Event) -> Result<Option<ResponseEvent>, Error> {
            match event.kind.as_str() {
                "response.created" => {
                    return Ok(Some(ResponseEvent::Created));
                }
                "response.failed" | "response.incomplete" => {
                    return Err(ResponsesEventError::Api(ApiError::Retryable));
                }
                "response.metadata" => {}
                _ => {}
            }
        }
        '''
        rows = W._match_arm_inventory(source, "responses.rs", "pub fn process_responses_event")
        by_event = {row["event"]: row for row in rows}
        self.assertEqual(by_event["response.created"]["disposition"], "emitted")
        self.assertEqual(by_event["response.failed"]["disposition"], "terminal_error")
        self.assertEqual(by_event["response.incomplete"]["disposition"], "terminal_error")
        self.assertEqual(by_event["response.metadata"]["disposition"], "observed_not_emitted")

    def test_mcp_protocol_distinguishes_config_transport_tool_and_metadata_planes(self) -> None:
        extra = {
            "mcp_types": r'''
                struct McpServerConfig {
                    #[serde(flatten)] transport: McpServerTransportConfig,
                    enabled: bool,
                    required: bool,
                    omit_tools_from: Option<Vec<String>>,
                    tools: HashMap<String, McpServerToolConfig>,
                }
                struct RawMcpServerConfig { command: Option<String>, url: Option<String> }
                struct McpServerToolConfig { approval_mode: Option<String>, output_token_limit: Option<u64> }
                struct McpServerOAuthConfig { client_id: Option<String> }
                #[serde(untagged)]
                enum McpServerTransportConfig {
                    Stdio { command: String },
                    StreamableHttp { url: String },
                }
                enum McpServerAuth { OAuth, ChatGpt }
                enum AppToolApproval { Auto, Prompt }
                impl TryFrom<RawMcpServerConfig> for McpServerConfig {}
            ''',
            "mcp_runtime": r'''
                struct McpConfig { apps_enabled: bool, mcp_server_catalog: Catalog }
                const H: &str = "X-OpenAI-Product-Sku";
                fn apps() { let p = "/ps/mcp"; }
            ''',
            "rmcp_protocol_mode": r'''
                enum McpProtocolMode { Legacy, V20260728 }
                fn preferred_protocol_version() {}
            ''',
            "rmcp_client": r'''
                enum PendingTransport { InProcess, Stdio, StreamableHttp }
                impl RmcpClient {
                    pub async fn new_stdio_client() {}
                    pub async fn list_tools() {}
                    pub async fn call_tool() {}
                }
                struct StreamableHttpClientTransport;
            ''',
            "mcp_handler": "fn create_tool_spec() {}",
            "tool_spec_plan": "fn apply_mcp_tool_exposure_policy() {} fn finalize_tool_router() {}",
            "tool_responses_api": r'''
                struct ResponsesApiTool { name: String }
                struct ResponsesApiNamespace { name: String }
                enum ResponsesApiNamespaceTool { Function(ResponsesApiTool) }
            ''',
            "turn_metadata": r'''
                fn current_meta_value_for_mcp_request() {}
                fn mcp_metadata_template() {}
            ''',
            "mcp_binding": "struct McpBinding;",
            "mcp_catalog": "struct McpServerCatalog;",
            "feature_registry": "enum Feature { Mcp20260728 }",
        }
        protocol = W.build_mcp_protocol(extra, {})
        self.assertEqual(protocol["configuration"]["server"]["struct"], "McpServerConfig")
        self.assertEqual([row["name"] for row in protocol["transports"]], ["in_process", "stdio", "streamable_http"])
        self.assertIn("call_tool", protocol["operation_names"])
        projection = protocol["turn_metadata_projection"]
        self.assertIn("tool_namespaces_info", projection["explicitly_removed"])
        self.assertIn("does not get appended", projection["extra_behavior"]["config_responses_api_metadata"])
        self.assertEqual(protocol["tool_projection"]["codex_runtime_spec"], "ToolSpec::Namespace(ResponsesApiNamespace)")

    def test_responses_lite_is_reported_as_request_and_tool_rewrite(self) -> None:
        base = {
            "core": "fn build_responses_request() { if model_info.use_responses_lite {} } fn add_responses_lite_header() {} fn build_ws_client_metadata() {}",
        }
        extra = {
            "tool_spec": "fn create_tools_json_for_responses_lite() {}",
            "tool_spec_plan": "// Responses Lite accepts schemas for client-executed tools, not hosted Responses tools. fn x() { turn_metadata_includes_tool_info; }",
            "provider_runtime": "pub namespace_tools: bool",
            "model_protocol": "pub use_responses_lite: bool",
        }
        protocol = W.build_responses_lite_protocol(base, extra, {"config_protocol": {"feature_registry": {"features": []}}})
        rewrite = {row["concept"]: row for row in protocol["request_rewrite"]}
        self.assertEqual(rewrite["top-level tools"]["responses_lite"], None)
        self.assertEqual(rewrite["parallel_tool_calls"]["responses_lite"], False)
        self.assertEqual(rewrite["reasoning.context"]["responses_lite"], "all_turns")
        self.assertIn("default `functions` namespace", " ".join(protocol["tool_rewrite"]["lite"]))

    def test_metadata_protocol_reports_string_nesting_flat_extra_and_mcp_object(self) -> None:
        report = {
            "turn_metadata_schema": {
                "canonical_wire_path": 'client_metadata["x-codex-turn-metadata"]',
                "canonical_wire_type": "ASCII JSON string",
                "fixed_fields": [{"name": "session_id", "wire_name": "session_id"}],
                "flattened_extra": {"name": "extra", "flattened": True},
                "request_kind_values": ["turn"],
                "nested": {},
                "compatibility_header_difference": "tool_namespaces_info=None",
            },
            "turn_metadata_construction": {
                "fields": [{"name": "session_id", "construction_rule": "turn identity", "expression": "self.session_id"}],
                "identity_gates": {},
                "extra_metadata_limits": {"MAX_EXTRA_METADATA_ENTRIES": 16},
                "extra_metadata_reserved_keys": ["session_id"],
                "extra_metadata_key_charset": "ASCII",
            },
            "responses_http": {
                "client_metadata": [{"name": "session_id", "optional": False}],
            },
            "responses_websocket": {"client_metadata_ws_additions": []},
            "protocol_metadata_inputs": {"inputs": []},
        }
        base = {"metadata": "fn client_metadata() {} fn turn_metadata_payload() {} fn compatibility_headers() {} struct CodexTurnMetadataPayload {}"}
        extra = {"turn_metadata": "fn responses_metadata_template() {} fn mcp_metadata_template() {}"}
        mcp = {"turn_metadata_projection": {
            "wire_type": "JSON object",
            "extra_behavior": {"config_responses_api_metadata": "shadow only"},
        }}
        protocol = W.build_metadata_protocol(report, base, extra, mcp)
        self.assertEqual(protocol["top_level_client_metadata"]["value_type"], "every top-level value is a string")
        self.assertIsNone(protocol["flattened_extra"]["wire_wrapper"])
        self.assertEqual(protocol["mcp_projection"]["wire_type"], "JSON object")
        self.assertIn("outer client_metadata value is a string", protocol["top_level_client_metadata"]["nested_encoding"]["double_layer_note"])
        self.assertEqual(protocol["representation_matrix"]["mcp_request"]["turn_metadata_representation"], "JSON object")
        self.assertIn("parse that ASCII JSON string", protocol["decode_recipe"][3])
        self.assertEqual(len(protocol["flattened_extra"]["provenance_matrix"]), 3)


    def test_full_responses_builder_assignments_are_source_derived(self) -> None:
        common = r'''
            struct ResponsesApiRequest {
                model: String,
                instructions: String,
                input: Vec<String>,
                tools: Option<Vec<String>>,
                tool_choice: String,
                parallel_tool_calls: bool,
                reasoning: Option<String>,
                store: bool,
                stream: bool,
                stream_options: Option<String>,
                include: Vec<String>,
                service_tier: Option<String>,
                prompt_cache_key: Option<String>,
                text: String,
                client_metadata: Option<HashMap<String, String>>,
                access_programs: Option<String>,
            }
            struct ResponseCreateWsRequest<'a> { model: &'a str }
        '''
        core = r'''
            fn build_responses_request() -> Result<ResponsesApiRequest> {
                let request = ResponsesApiRequest {
                    model: model_info.slug.clone(),
                    instructions,
                    input,
                    tools,
                    tool_choice: "auto".to_string(),
                    parallel_tool_calls: prompt.parallel_tool_calls && !model_info.use_responses_lite,
                    reasoning: Some(reasoning),
                    store: false,
                    stream: true,
                    stream_options,
                    include,
                    service_tier,
                    prompt_cache_key,
                    text,
                    client_metadata: Some(responses_metadata.client_metadata()),
                    access_programs: None,
                };
                Ok(request)
            }
        '''
        sse = r'''
            pub struct ResponsesStreamEvent { kind: String }
            pub fn process_responses_event(event: ResponsesStreamEvent) {
                match event.kind.as_str() {
                    "response.created" => { let _ = ResponseEvent::Created; }
                    "response.completed" => { let _ = ResponseEvent::Completed; }
                    "response.failed" => { let _ = ApiError::Retryable; return Err(()); }
                    _ => {}
                }
            }
        '''
        protocol = W.build_responses_protocol(
            {"common": common, "core": core, "response_sse": sse, "ws": ""},
            {},
            {"wire_surface_catalog": {}},
        )
        checks = protocol["request"]["builder_checks"]
        self.assertTrue(checks["literal_extracted"])
        self.assertTrue(checks["all_schema_fields_assigned"])
        self.assertTrue(checks["store_false"])
        self.assertTrue(checks["stream_true"])
        self.assertTrue(checks["parallel_calls_are_lite_gated"])
        self.assertEqual(protocol["request"]["builder_missing_schema_fields"], [])
        semantics = {row["field"]: row for row in protocol["request"]["field_semantics"]}
        self.assertIn("string map", semantics["client_metadata"]["wire_semantics"])

    def test_config_protocol_builds_category_and_wire_effect_indexes(self) -> None:
        config_toml = r'''
            pub struct ConfigToml {
                pub model: Option<String>,
                pub responses_api_metadata: Option<BTreeMap<String, String>>,
                pub mcp_servers: HashMap<String, McpServerConfig>,
            }
        '''
        effective = r'''
            pub struct Config {
                pub model: String,
                pub responses_api_metadata: BTreeMap<String, String>,
                pub mcp_servers: Catalog,
            }
        '''
        protocol = W.build_config_protocol(
            {"config_toml": config_toml, "core_config": effective},
            {"core": "fn build_responses_request() {}"},
        )
        self.assertIn("metadata", protocol["settings_by_category"])
        self.assertIn("mcp", protocol["settings_by_category"])
        self.assertIn("client_metadata", protocol["wire_effect_index"])
        self.assertEqual(
            protocol["settings_by_category"]["metadata"][0]["setting"],
            "responses_api_metadata",
        )

    def test_lite_field_matrix_preserves_metadata_but_rewrites_tools(self) -> None:
        request_schema = [
            {"name": "instructions", "wire_name": "instructions", "type": "String"},
            {"name": "tools", "wire_name": "tools", "type": "Option<Vec<Value>>"},
            {"name": "client_metadata", "wire_name": "client_metadata", "type": "Option<HashMap<String, String>>"},
        ]
        report = {
            "config_protocol": {"feature_registry": {"features": []}},
            "responses_protocol": {
                "request": {
                    "http": {"schema": request_schema},
                    "field_semantics": [
                        {"field": "instructions", "wire_semantics": "top-level instructions"},
                        {"field": "tools", "wire_semantics": "top-level tools"},
                        {"field": "client_metadata", "wire_semantics": "string map"},
                    ],
                },
                "input_and_output_items": {"enums": {"ResponseItem": {"variants": []}}},
            },
        }
        protocol = W.build_responses_lite_protocol(
            {"core": "fn build_responses_request() { if model_info.use_responses_lite {} } fn add_responses_lite_header() {} fn build_ws_client_metadata() {}"},
            {
                "tool_spec": "fn create_tools_json_for_responses_lite() {}",
                "tool_spec_plan": "// Responses Lite accepts schemas for client-executed tools, not hosted Responses tools. fn x() { turn_metadata_includes_tool_info; }",
                "provider_runtime": "pub namespace_tools: bool",
                "model_protocol": "pub use_responses_lite: bool",
            },
            report,
        )
        matrix = {row["field"]: row for row in protocol["request_field_matrix"]}
        self.assertIn("empty string", matrix["instructions"]["responses_lite"])
        self.assertIn("AdditionalTools", matrix["tools"]["responses_lite"])
        self.assertIn("same base", matrix["client_metadata"]["responses_lite"])
        self.assertIn("same logical response", protocol["metadata_invariance"]["response_protocol"])

    def test_append_mcp_surfaces_adds_three_distinct_surfaces(self) -> None:
        report = {"wire_surface_catalog": {"surfaces": [], "categories": {}, "planes": {}}}
        mcp = {
            "transports": [
                {"name": "in_process", "source": None},
                {"name": "stdio", "source": None},
                {"name": "streamable_http", "source": None},
            ],
            "hosted_apps_mcp": {"path": "/ps/mcp", "source": None},
        }
        W.append_mcp_surfaces(report, mcp)
        ids = {surface["id"] for surface in report["wire_surface_catalog"]["surfaces"]}
        self.assertEqual(ids, {"mcp_stdio", "mcp_streamable_http", "mcp_codex_apps"})
        self.assertEqual(report["wire_surface_catalog"]["settings_catalog"]["mcp"]["$ref"], "#/mcp_protocol")

    def test_enhance_integrates_config_mcp_responses_lite_and_metadata_sections(self) -> None:
        report = {
            "source": {"files": []},
            "scope": {"warnings": [], "diagnostics": {}},
            "turn_metadata_schema": {
                "fixed_fields": [],
                "flattened_extra": None,
                "request_kind_values": [],
                "nested": {},
            },
            "turn_metadata_construction": {
                "fields": [],
                "identity_gates": {},
                "warnings": [],
                "extra_metadata_limits": {},
                "extra_metadata_reserved_keys": [],
            },
            "responses_http": {
                "client_metadata": [],
                "request_headers": [],
                "tracking_request_headers": [],
                "response_headers": [],
            },
            "responses_websocket": {
                "client_metadata_ws_additions": [],
                "client_metadata_base": [],
                "handshake_request_headers": [],
                "handshake_response_headers": [],
                "request_body_continuation": [],
            },
            "responses_compact": {},
            "wire_surface_catalog": {
                "surfaces": [],
                "categories": {},
                "planes": {},
                "coverage": {},
            },
        }
        base = {
            "common": r'''
                struct ResponsesApiRequest { model: String }
                struct ResponseCreateWsRequest<'a> { model: &'a str }
                enum ResponseEvent { Completed }
            ''',
            "metadata": r'''
                enum TurnToolSource { Harness, Mcp { server_name: String } }
                struct CodexTurnMetadataPayload { #[serde(flatten)] extra: BTreeMap<String, String> }
                fn client_metadata() {}
                fn turn_metadata_payload() {}
                fn compatibility_headers() {}
                // compatibility projections of this snapshot
            ''',
            "core": "fn build_responses_request() {}",
            "response_sse": "",
            "ws": "",
        }
        extra = {
            "mcp_types": "struct McpServerConfig { enabled: bool } enum McpServerTransportConfig { Stdio { command: String } }",
            "mcp_runtime": "struct McpConfig { apps_enabled: bool }",
        }
        enhanced = W.enhance(report, base, extra, [], False)
        for key in (
            "config_protocol",
            "mcp_protocol",
            "responses_protocol",
            "responses_lite_protocol",
            "metadata_protocol",
        ):
            self.assertIn(key, enhanced)
        catalog = enhanced["wire_surface_catalog"]
        self.assertEqual(catalog["protocol_profiles"]["responses_lite"]["$ref"], "#/responses_lite_protocol")
        self.assertEqual(catalog["metadata_profiles"]["codex_client_metadata"]["$ref"], "#/metadata_protocol")
        self.assertIn("mcp_stdio", {surface["id"] for surface in catalog["surfaces"]})
        self.assertIn("config_protocol", enhanced["full_wire_schema"]["sections"])



def minimal_v10_legacy_report() -> dict[str, Any]:
    source = {"path": "fixture.rs", "line": 1, "symbol": "Fixture"}
    request_fields = [
        {
            "name": "model",
            "wire_name": "model",
            "type": "String",
            "optional": False,
            "serialized": True,
            "source": copy.deepcopy(source),
        },
        {
            "name": "tools",
            "wire_name": "tools",
            "type": "Option<Vec<String>>",
            "optional": True,
            "serialized": True,
            "skip_serializing_if": "Option::is_none",
            "source": copy.deepcopy(source),
        },
    ]
    turn_fields = [
        {
            "name": "session_id",
            "wire_name": "session_id",
            "type": "Option<&str>",
            "optional": True,
            "serialized": True,
            "skip_serializing_if": "Option::is_none",
            "source": copy.deepcopy(source),
        },
        {
            "name": "tool_namespaces_info",
            "wire_name": "tool_namespaces_info",
            "type": "Option<BTreeMap<String, String>>",
            "optional": True,
            "serialized": True,
            "skip_serializing_if": "Option::is_none",
            "source": copy.deepcopy(source),
        },
    ]
    return {
        "schema_version": 10,
        "catalog_schema_version": 10,
        "coverage": {},
        "scope": {"warnings": ["optional fixture source unavailable"], "diagnostics": {}},
        "wire_surface_catalog": {
            "surfaces": [
                {
                    "id": "responses_http_sse",
                    "plane": "model_inference",
                    "service_family": "responses",
                    "category": "inference.responses",
                    "addressing": {"kind": "http_endpoint", "values": ["/responses"]},
                    "method": "POST",
                    "transport": "HTTPS",
                    "protocol": "Responses API",
                    "streaming": True,
                    "usage_status": "active",
                    "request": {
                        "body_schema": "ResponsesApiRequest",
                        "body_fields": copy.deepcopy(request_fields),
                    },
                    "response": {"body_schema": None, "body_fields": []},
                    "headers": {
                        "request_by_category": {
                            "identity": [
                                {
                                    "name": "session-id",
                                    "direction": "request",
                                    "category": "identity",
                                    "condition": "always",
                                    "optional": False,
                                    "origin": "client",
                                    "source": copy.deepcopy(source),
                                }
                            ]
                        },
                        "response_by_category": {},
                        "request_templates": [],
                        "response_templates": [],
                    },
                    "profiles": {},
                    "source": copy.deepcopy(source),
                }
            ],
            "model_facing_transport_matrix": {"schema_version": 7},
        },
        "responses_protocol": {
            "schema_version": 1,
            "request": {
                "http": {
                    "struct": "ResponsesApiRequest",
                    "schema": copy.deepcopy(request_fields),
                }
            },
            "response_stream": {
                "event_kinds_discovered": ["response.completed"],
                "dispatch": [
                    {
                        "event": "response.completed",
                        "disposition": "emitted",
                        "source": copy.deepcopy(source),
                    }
                ],
            },
            "input_and_output_items": {
                "enums": {
                    "ResponseItem": {
                        "name": "ResponseItem",
                        "variants": [
                            {
                                "name": "Message",
                                "wire_name": "message",
                                "shape": "struct",
                                "source": copy.deepcopy(source),
                            }
                        ],
                    }
                }
            },
        },
        "responses_lite_protocol": {
            "schema_version": 1,
            "request_rewrite": [
                {
                    "concept": "top-level tools",
                    "full_responses": "array",
                    "responses_lite": None,
                }
            ],
            "tool_namespace_metadata": {
                "wire_path": 'client_metadata["x-codex-turn-metadata"].tool_namespaces_info',
                "contents": "effective tool ownership",
                "source": copy.deepcopy(source),
            },
        },
        "metadata_protocol": {
            "schema_version": 1,
            "flattened_extra": {
                "wire_path": 'client_metadata["x-codex-turn-metadata"].<extra-key>',
                "wire_wrapper": None,
                "precedence": "config wins",
            },
            "mcp_projection": {
                "wire_type": "JSON object",
                "identity_effect": {
                    "therefore_omitted": ["installation_id", "window_id"]
                },
                "explicitly_removed": ["tool_namespaces_info"],
                "explicitly_added_or_overridden": [
                    {"name": "model", "condition": "always"}
                ],
                "extra_behavior": {"config_values": "shadow without copying"},
                "source": copy.deepcopy(source),
            },
            "top_level_client_metadata": {
                "base_and_conditional_keys": [
                    {"name": "session_id"},
                    {"name": "x-codex-turn-metadata"},
                ]
            },
        },
        "config_protocol": {
            "schema_version": 1,
            "wire_affecting_settings": [
                {
                    "setting": "model",
                    "syntax": "model",
                    "owner": "ConfigToml",
                    "category": "model_selection",
                    "configured_type": "Option<String>",
                    "effective_runtime": "Config.model",
                    "effective_type": "String",
                    "wire_effects": [
                        {
                            "layer": "request_body",
                            "path": "ResponsesApiRequest.model",
                            "behavior": "selects model",
                            "condition": None,
                        }
                    ],
                    "source": copy.deepcopy(source),
                    "runtime_source": copy.deepcopy(source),
                }
            ],
            "wire_affecting_features": [],
        },
        "relation_map": {
            "groups": [
                {
                    "concept": "session_id",
                    "canonical": "CodexResponsesMetadata.session_id",
                    "nodes": [
                        {
                            "path": 'client_metadata["session_id"]',
                            "surface": "http",
                            "relation": "same_value",
                            "optional": False,
                            "note": None,
                        }
                    ],
                }
            ]
        },
        "turn_metadata_schema": {
            "fixed_fields": turn_fields,
            "flattened_extra": {
                "name": "extra",
                "wire_name": "extra",
                "type": "BTreeMap<String, String>",
                "flattened": True,
                "serialized": True,
                "source": copy.deepcopy(source),
            },
        },
    }


class MachineContractTests(unittest.TestCase):
    def upgrade(self) -> dict[str, Any]:
        report = minimal_v10_legacy_report()
        return C.upgrade_report(
            report,
            commit={"sha": "abc", "date": "2026-01-01T00:00:00Z", "message": "fixture"},
            sources={
                "fixture.rs": {
                    "text": "struct Fixture {}\n",
                    "roles": ["base:fixture"],
                }
            },
            source_mode="fixture",
            repository="openai/codex",
            requested_ref="abc",
        )

    def test_stable_ids_are_deterministic_and_ascii(self) -> None:
        first = C.stable_id("Field", "Réponse metadata", "x-codex-turn-state")
        second = C.stable_id("Field", "Re\u0301ponse metadata", "x-codex-turn-state")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[a-z0-9_.]+$")

    def test_referer_becomes_transport_header_entity_and_rules(self) -> None:
        report = minimal_v10_legacy_report()
        source = {"path": "fixture.rs", "line": 1, "symbol": "redirect"}
        report["http_redirect_protocol"] = {
            "schema_version": 1,
            "header": {
                "name": "Referer",
                "canonical_name": "referer",
                "direction": "request",
                "origin": "client_transport_derived",
                "category": "redirect_context",
                "case_sensitive": False,
                "first_request_generated": False,
                "redirect_request_generated": True,
                "value_shape": "absolute URI derived from the previous request URL",
                "sensitivity": "request_url",
                "logging_policy": "redact URL path/query",
            },
            "profiles": [
                {
                    "id": "route_aware_http_redirect_referer",
                    "transport_family": "route_aware_http",
                    "applies_when": "manual redirect follow",
                    "redirect_statuses": [301, 302, 303, 307, 308],
                    "first_request_generated": False,
                    "redirect_request_generated": True,
                    "stale_header_policy": "remove before recomputing",
                    "same_origin_value": "previous URL",
                    "cross_origin_value": "previous origin root",
                    "https_to_http_downgrade": "omit Referer",
                    "source_checks": {},
                    "source": copy.deepcopy(source),
                }
            ],
            "warnings": [],
        }
        report["wire_surface_catalog"]["transport_generated_header_profiles"] = {
            "referer": {"$ref": "#/http_redirect_protocol"}
        }
        upgraded = C.upgrade_report(
            report,
            commit={"sha": "abc", "date": "2026-01-01T00:00:00Z", "message": "fixture"},
            sources={"fixture.rs": {"text": "redirect\n", "roles": ["extra:redirect"]}},
            source_mode="fixture",
            repository="openai/codex",
            requested_ref="abc",
        )
        header_id = C.stable_id("header", "request", "referer")
        header = upgraded["entities"]["headers"][header_id]
        self.assertTrue(header["transport_generated"])
        self.assertFalse(header["first_request_generated"])
        self.assertTrue(header["redirect_request_generated"])
        self.assertIn("transport_header_profiles", upgraded["entities"])
        self.assertIn("rule.http_redirect.referer_emit", upgraded["entities"]["rules"])
        self.assertFalse(C.validate_report(upgraded))

    def test_rust_wire_type_inference(self) -> None:
        vector = C.rust_type_to_wire_schema("Option<Vec<String>>")
        mapping = C.rust_type_to_wire_schema("BTreeMap<String, bool>")
        uuid = C.rust_type_to_wire_schema("ThreadId")
        self.assertEqual(vector["type"], "array")
        self.assertEqual(vector["items"]["type"], "string")
        self.assertEqual(mapping["additionalProperties"]["type"], "boolean")
        self.assertEqual(uuid["format"], "uuid")

    def test_presence_distinguishes_null_from_omission(self) -> None:
        omitted = C.normalize_field(
            {
                "name": "value",
                "wire_name": "value",
                "type": "Option<String>",
                "skip_serializing_if": "Option::is_none",
                "serialized": True,
            },
            "schema.fixture",
            "/fields/0",
        )
        nullable = C.normalize_field(
            {
                "name": "value",
                "wire_name": "value",
                "type": "Option<String>",
                "serialized": True,
            },
            "schema.fixture_nullable",
            "/fields/0",
        )
        self.assertEqual(omitted["presence"]["mode"], "conditional")
        self.assertFalse(omitted["presence"]["nullable"])
        self.assertEqual(nullable["presence"]["mode"], "present_nullable")
        self.assertTrue(nullable["presence"]["required"])

    def test_flattened_field_has_no_wire_wrapper(self) -> None:
        field = C.normalize_field(
            {
                "name": "extra",
                "wire_name": "extra",
                "type": "BTreeMap<String, String>",
                "flattened": True,
                "serialized": True,
            },
            "schema.turn",
            "/extra",
        )
        self.assertEqual(field["presence"]["mode"], "flattened")
        self.assertIsNone(field["presence"]["wire_wrapper"])

    def test_conditions_are_structured_without_dropping_text(self) -> None:
        lite = C.condition_to_predicate("Responses Lite only")
        unknown = C.condition_to_predicate("custom source-specific branch")
        self.assertEqual(lite, {"op": "eq", "path": "model.use_responses_lite", "value": True})
        self.assertEqual(unknown["op"], "source_condition")
        self.assertFalse(unknown["machine_evaluable"])

    def test_source_manifest_has_content_and_git_blob_hashes(self) -> None:
        report = self.upgrade()
        source_id = report["source_manifest"]["path_index"]["fixture.rs"]
        source = report["source_manifest"]["files"][source_id]
        data = b"struct Fixture {}\n"
        expected_blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        self.assertEqual(source["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(source["git_blob_sha1"], expected_blob)
        self.assertTrue(report["source_manifest"]["locations"])

    def test_structured_diagnostics_are_deduplicated(self) -> None:
        report = minimal_v10_legacy_report()
        report["config_protocol"]["warnings"] = ["optional fixture source unavailable"]
        upgraded = C.upgrade_report(
            report,
            commit={"sha": "abc"},
            sources={"fixture.rs": {"text": "x\n", "roles": ["base"]}},
            source_mode="fixture",
            repository="openai/codex",
            requested_ref="abc",
        )
        matches = [d for d in upgraded["diagnostics"] if d["message"] == "optional fixture source unavailable"]
        self.assertEqual(len(matches), 1)
        self.assertGreaterEqual(len(matches[0]["report_pointers"]), 2)

    def test_machine_contract_has_stable_entities_and_edges(self) -> None:
        report = self.upgrade()
        counts = report["machine_contract"]["counts"]
        self.assertGreater(counts["schemas"], 0)
        self.assertGreater(counts["fields"], 0)
        self.assertGreater(counts["edges"], 0)
        self.assertIn("surface.responses_http_sse", report["entities"]["surfaces"])
        self.assertIn("setting.config.model", report["entities"]["settings"])
        self.assertTrue(any(edge["relation"] == "contains" for edge in report["edges"]))

    def test_upgraded_report_validates(self) -> None:
        report = self.upgrade()
        self.assertEqual(report["$schema"], C.REPORT_SCHEMA_ID)
        self.assertEqual(report["format_version"], C.REPORT_FORMAT_VERSION)
        self.assertEqual(C.validate_report(report), [])

    def test_integrity_detects_mutation_but_ignores_generated_at(self) -> None:
        report = self.upgrade()
        original = report["integrity"]["payload_sha256"]
        report["generated_at"] = "changed"
        self.assertTrue(C.verify_integrity(report))
        report["views"]["configuration"]["setting_ids"].append("setting.fake")
        self.assertFalse(C.verify_integrity(report))
        self.assertEqual(original, report["integrity"]["payload_sha256"])

    def test_json_pointer_supports_escaping(self) -> None:
        value = {"a/b": {"~key": ["zero", "one"]}}
        self.assertEqual(C.json_pointer_get(value, "/a~1b/~0key/1"), "one")
        with self.assertRaises(C.ContractError):
            C.json_pointer_get(value, "/missing")

    def test_report_and_protocol_schema_documents_are_emitted(self) -> None:
        report = self.upgrade()
        report_docs = C.schema_documents()
        protocol_docs = C.protocol_schema_documents(report)
        self.assertEqual(report_docs["codex-wire-audit-report.schema.json"]["$id"], C.REPORT_SCHEMA_ID)
        self.assertIn("responses-request-full.schema.json", protocol_docs)
        self.assertIn("responses-request-lite.schema.json", protocol_docs)
        self.assertFalse(protocol_docs["responses-request-lite.schema.json"]["properties"]["tools"])
        self.assertEqual(
            protocol_docs["codex-turn-metadata-mcp.schema.json"]["type"],
            "object",
        )

    def test_write_output_directory_creates_split_machine_files(self) -> None:
        report = self.upgrade()
        with tempfile.TemporaryDirectory() as directory:
            paths = C.write_output_directory(directory, report, include_fixtures=True)
            root = Path(directory)
            self.assertTrue((root / "codex-wire-audit.report.canonical.json").exists())
            self.assertTrue((root / "entities" / "fields.jsonl").exists())
            self.assertTrue((root / "schemas" / "protocol" / "responses-request-lite.schema.json").exists())
            self.assertTrue((root / "fixtures" / "manifest.json").exists())
            self.assertTrue((root / "manifest.json").exists())
            self.assertGreater(len(paths), 10)

    def test_jsonl_output_has_entities_edges_diagnostics_and_metadata(self) -> None:
        report = self.upgrade()
        rows = [json.loads(line) for line in C.render_jsonl(report).splitlines()]
        record_types = {row["record_type"] for row in rows}
        self.assertTrue({"entity", "edge", "diagnostic", "report_metadata"}.issubset(record_types))

    def test_validator_rejects_unknown_edge_endpoint(self) -> None:
        report = self.upgrade()
        report["edges"].append({
            "id": "edge.invalid",
            "from": "surface.missing",
            "to": "schema.missing",
            "relation": "uses_request_schema",
        })
        C.attach_integrity(report)
        codes = {item["code"] for item in C.validate_report(report)}
        self.assertIn("EDGE_ENDPOINT_UNKNOWN", codes)

    def test_offline_repo_root_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex-rs").mkdir()
            mappings = {"a": "codex-rs/a.rs"}
            (root / "codex-rs" / "a.rs").write_text("struct A;\n", encoding="utf-8")
            base, surface, extra, surface_warnings, extra_warnings = C.load_source_sets_from_root(
                root, mappings, {"optional": "codex-rs/missing.rs"}, {}
            )
            self.assertEqual(base["a"], "struct A;\n")
            self.assertEqual(surface, {})
            self.assertEqual(extra, {})
            self.assertEqual(len(surface_warnings), 1)
            self.assertEqual(extra_warnings, [])

    def test_zip_archive_loader_finds_single_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("codex-main/codex-rs/a.rs", "struct A;\n")
            extracted = Path(directory) / "out"
            root = C.extract_source_archive(archive, extracted)
            self.assertEqual(root.name, "codex-main")
            self.assertTrue((root / "codex-rs" / "a.rs").exists())

    def test_archive_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../escape.txt", "bad")
            with self.assertRaises(C.ContractError):
                C.extract_source_archive(archive, Path(directory) / "out")

    def test_cache_only_loader_requires_complete_mandatory_set(self) -> None:
        values = {("repo", "a" * 40, "a.rs"): "A\n"}
        reader = lambda repo, sha, path: values.get((repo, sha, path))
        base, surface, extra, surface_warnings, extra_warnings = C.load_source_sets_from_cache(
            "repo", "a" * 40, {"a": "a.rs"}, {"b": "b.rs"}, {}, reader
        )
        self.assertEqual(base["a"], "A\n")
        self.assertEqual(surface, {})
        self.assertEqual(len(surface_warnings), 1)
        self.assertEqual(extra_warnings, [])
        with self.assertRaises(C.ContractError):
            C.load_source_sets_from_cache("repo", "main", {"a": "a.rs"}, {}, {}, reader)

    def test_fixture_lite_request_omits_top_level_tools_and_instructions(self) -> None:
        fixtures = C.fixture_documents()
        lite = fixtures["responses-lite-http.valid.json"]
        self.assertNotIn("tools", lite)
        self.assertNotIn("instructions", lite)
        self.assertFalse(lite["parallel_tool_calls"])
        self.assertEqual(lite["input"][0]["type"], "additional_tools")

    def test_canonical_bytes_normalize_unicode_and_key_order(self) -> None:
        left = {"b": "Re\u0301ponse", "a": 1}
        right = {"a": 1, "b": "Réponse"}
        self.assertEqual(C.canonical_json_bytes(left), C.canonical_json_bytes(right))

    def test_transformed_id_components_include_collision_suffix(self) -> None:
        self.assertNotEqual(
            C.stable_id("header", "x-a-b"),
            C.stable_id("header", "x_a_b"),
        )

    def test_event_schema_has_event_specific_requirements(self) -> None:
        report = self.upgrade()
        schema = C.protocol_schema_documents(report)["responses-event.schema.json"]
        completed = next(
            variant
            for variant in schema["oneOf"]
            if variant["properties"]["type"].get("const") == "response.completed"
        )
        self.assertIn("response", completed["required"])
        self.assertIn("id", completed["properties"]["response"]["required"])

    def test_mcp_schema_encodes_exclusive_transport(self) -> None:
        report = self.upgrade()
        schema = C.protocol_schema_documents(report)["mcp-server-config.schema.json"]
        transport_constraint = next(
            constraint for constraint in schema["allOf"] if "oneOf" in constraint
        )
        branches = transport_constraint["oneOf"]
        self.assertEqual(branches[0]["required"], ["command"])
        self.assertEqual(branches[1]["required"], ["url"])



    def test_field_ids_remain_readable_and_schema_qualified(self) -> None:
        report = self.upgrade()
        self.assertIn(
            "field.codex.turn_metadata.full.session_id",
            report["entities"]["fields"],
        )

    def test_mcp_projection_rule_uses_current_added_field_key(self) -> None:
        report = self.upgrade()
        rule = report["entities"]["rules"]["rule.metadata.mcp_projection"]
        self.assertEqual(
            [row["name"] for row in rule["added_or_overridden_fields"]],
            ["model"],
        )
        self.assertEqual(
            rule["removed_fields"],
            ["installation_id", "window_id", "tool_namespaces_info"],
        )
        self.assertEqual(rule["extra_behavior"]["config_values"], "shadow without copying")
        self.assertTrue(rule["source_refs"])

    def test_source_manifest_includes_expected_unavailable_sources(self) -> None:
        combined = C.combine_sources(
            {"required": "struct A;\n"},
            {"required": "a.rs"},
            {},
            {"optional": "optional.rs"},
            {},
            {},
        )
        manifest = C.build_source_manifest(
            {"sha": "abc"},
            combined,
            source_mode="fixture",
            repository="openai/codex",
            requested_ref="abc",
        )
        optional_id = manifest["path_index"]["optional.rs"]
        self.assertFalse(manifest["files"][optional_id]["available"])
        self.assertEqual(manifest["counts"], {"expected": 2, "available": 1, "unavailable": 1})

    def test_surface_body_schema_is_addressable_when_fields_are_opaque(self) -> None:
        legacy = minimal_v10_legacy_report()
        legacy["wire_surface_catalog"]["surfaces"].append({
            "id": "opaque_surface",
            "plane": "provider_auxiliary",
            "service_family": "opaque",
            "category": "opaque",
            "addressing": {"kind": "http_endpoint", "values": ["/opaque"]},
            "method": "POST",
            "transport": "HTTPS",
            "protocol": "unary JSON",
            "streaming": False,
            "request": {"body_schema": "OpaquePayload", "body_fields": []},
            "response": {"body_schema": "OpaqueResult", "body_fields": []},
            "headers": {},
        })
        report = C.upgrade_report(
            legacy,
            commit={"sha": "abc"},
            sources={"fixture.rs": {"text": "x\n", "roles": ["base:fixture"]}},
            source_mode="fixture",
            repository="openai/codex",
            requested_ref="abc",
        )
        surface = report["entities"]["surfaces"]["surface.opaque_surface"]
        request_schema = report["entities"]["schemas"][surface["request_schema_id"]]
        response_schema = report["entities"]["schemas"][surface["response_schema_id"]]
        self.assertTrue(request_schema["opaque"])
        self.assertTrue(response_schema["opaque"])
        self.assertEqual(C.validate_report(report), [])

    def test_protocol_schema_ids_are_source_revision_scoped(self) -> None:
        first = self.upgrade()
        second_legacy = minimal_v10_legacy_report()
        second = C.upgrade_report(
            second_legacy,
            commit={"sha": "def"},
            sources={"fixture.rs": {"text": "x\n", "roles": ["base:fixture"]}},
            source_mode="fixture",
            repository="openai/codex",
            requested_ref="def",
        )
        first_id = C.protocol_schema_documents(first)["responses-request-full.schema.json"]["$id"]
        second_id = C.protocol_schema_documents(second)["responses-request-full.schema.json"]["$id"]
        self.assertNotEqual(first_id, second_id)

    def test_protocol_bundle_includes_websocket_and_extra_input_schemas(self) -> None:
        documents = C.protocol_schema_documents(self.upgrade())
        self.assertIn("responses-websocket-response-create-full.schema.json", documents)
        self.assertIn("responses-websocket-response-create-lite.schema.json", documents)
        self.assertIn("responses-websocket-prewarm.schema.json", documents)
        self.assertIn("client-metadata-websocket-lite.schema.json", documents)
        self.assertIn("codex-turn-metadata-extra-input.schema.json", documents)
        self.assertIn("entity-schemas.schema.json", documents)

    def test_report_schema_bundle_includes_predicate_and_edge_contracts(self) -> None:
        documents = C.schema_documents()
        self.assertIn("codex-wire-audit-predicate.schema.json", documents)
        self.assertIn("codex-wire-audit-edge.schema.json", documents)
        self.assertEqual(
            documents["codex-wire-audit-rule.schema.json"]["properties"]["when"]["$ref"],
            C.PREDICATE_SCHEMA_ID,
        )

    def test_text_section_output_is_machine_readable_json(self) -> None:
        report = self.upgrade()
        selection = report["diagnostic_summary"]
        text, binary = W._render_output(selection, "text", report)
        self.assertIsNone(binary)
        self.assertEqual(json.loads(text), selection)

    def test_local_serde_omission_predicates_have_current_value_path(self) -> None:
        self.assertEqual(
            C.omitted_when_from_skip("std::ops::Not::not"),
            {"op": "eq", "path": "$", "value": False},
        )
        self.assertEqual(
            C.omitted_when_from_skip("Option::is_none"),
            {"op": "is_null", "path": "$"},
        )

    def test_nonzero_integer_and_response_item_id_wire_types(self) -> None:
        self.assertEqual(
            C.rust_type_to_wire_schema("NonZeroU16"),
            {
                "type": "integer",
                "minimum": 1,
                "maximum": 65535,
                "x-rust-type": "NonZeroU16",
            },
        )
        item_id = C.rust_type_to_wire_schema("ResponseItemId")
        self.assertEqual(item_id["type"], "string")
        self.assertNotIn("format", item_id)
        self.assertEqual(item_id["x-wire-kind"], "responses_prefixed_id")

    def test_mcp_configuration_fields_become_first_class_settings(self) -> None:
        legacy = minimal_v10_legacy_report()
        source = {"path": "fixture.rs", "line": 1, "symbol": "Fixture"}
        legacy["mcp_protocol"] = {
            "configuration": {
                "server": {
                    "settings": [
                        {
                            "setting": "mcp_servers.<server>.enabled",
                            "name": "enabled",
                            "wire_name": "enabled",
                            "type": "bool",
                            "optional": False,
                            "source": copy.deepcopy(source),
                        }
                    ]
                },
                "raw_toml": {
                    "fields": [
                        {
                            "name": "url",
                            "wire_name": "url",
                            "type": "Option<String>",
                            "optional": True,
                            "source": copy.deepcopy(source),
                        }
                    ]
                },
                "per_tool": {
                    "fields": [
                        {
                            "name": "output_token_limit",
                            "wire_name": "output_token_limit",
                            "type": "Option<NonZeroUsize>",
                            "optional": True,
                            "source": copy.deepcopy(source),
                        }
                    ]
                },
                "oauth": {
                    "fields": [
                        {
                            "name": "client_id",
                            "wire_name": "client_id",
                            "type": "Option<String>",
                            "optional": True,
                            "source": copy.deepcopy(source),
                        }
                    ]
                },
            }
        }
        report = C.upgrade_report(
            legacy,
            commit={"sha": "abc"},
            sources={"fixture.rs": {"text": "x\n", "roles": ["base:fixture"]}},
            source_mode="fixture",
            repository="openai/codex",
            requested_ref="abc",
        )
        settings = report["entities"]["settings"]
        self.assertIn("setting.mcp.server.enabled", settings)
        self.assertIn("setting.mcp.raw.url", settings)
        self.assertIn("setting.mcp.tool.output_token_limit", settings)
        self.assertIn("setting.mcp.oauth.client_id", settings)
        mcp_edges = [
            edge for edge in report["edges"]
            if edge["from"].startswith("setting.mcp.")
        ]
        self.assertTrue(mcp_edges)
        self.assertTrue(all(edge["relation"] == "configures" for edge in mcp_edges))

    def test_rule_edges_point_from_rule_to_target_and_source_to_rule(self) -> None:
        report = self.upgrade()
        rule_id = "rule.metadata.flattened_extra"
        target_id = C.stable_id(
            "wire_location",
            'client_metadata["x-codex-turn-metadata"].<extra-key>',
        )
        source_id = C.stable_id("wire_location", "CodexResponsesMetadata.extra")
        edges = report["edges"]
        self.assertTrue(any(
            edge["from"] == rule_id
            and edge["to"] == target_id
            and edge["relation"] == "rule_target"
            for edge in edges
        ))
        self.assertTrue(any(
            edge["from"] == source_id
            and edge["to"] == rule_id
            and edge["relation"] == "rule_source"
            for edge in edges
        ))

    def test_fixture_manifest_is_schema_addressable(self) -> None:
        fixtures = C.fixture_documents()
        manifest = fixtures["manifest.json"]
        self.assertEqual(manifest["format_version"], "1.0.0")
        self.assertEqual(len(manifest["fixtures"]), 11)
        self.assertTrue(all(row.get("schema_file") for row in manifest["fixtures"]))
        self.assertIn("responses-full-websocket.valid.json", fixtures)
        self.assertIn("responses-lite-websocket.valid.json", fixtures)
        self.assertIn("responses-websocket-prewarm.valid.json", fixtures)
        self.assertIn("mcp-server-stdio.valid.json", fixtures)
        self.assertIn("mcp-server-http.valid.json", fixtures)

    def test_websocket_fixture_markers_and_prewarm_shape(self) -> None:
        fixtures = C.fixture_documents()
        lite = fixtures["responses-lite-websocket.valid.json"]
        self.assertEqual(lite["type"], "response.create")
        self.assertEqual(
            lite["client_metadata"][
                "ws_request_header_x_openai_internal_codex_responses_lite"
            ],
            "true",
        )
        self.assertIn(
            "x-codex-ws-stream-request-start-ms",
            lite["client_metadata"],
        )
        prewarm = fixtures["responses-websocket-prewarm.valid.json"]
        self.assertFalse(prewarm["generate"])
        self.assertNotIn("input", prewarm)
        self.assertNotIn("tools", prewarm)


if __name__ == "__main__":
    unittest.main(verbosity=2)
