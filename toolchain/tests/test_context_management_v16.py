from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from codex_wire_audit.context_management_reference import verify_context_management_container
from codex_wire_audit.diagnostics import DiagnosticCollector
from codex_wire_audit.extractors import create_extractors
from codex_wire_audit.extractors.context_management import ContextManagementExtractor, SOURCE_IDS
from codex_wire_audit.legacy import load_legacy_modules
from codex_wire_audit.models import SourceFile, SourceRevision, SourceSnapshot
from codex_wire_audit.proof_profiles import resolve_profile
from codex_wire_audit.source_registry import SourceRegistry, from_legacy_maps


SOURCES = {
    "feature_configs": """
pub struct ContextManagementConfigToml {
    pub experimental_mode: Option<bool>,
}
""",
    "feature_registry": """
FeatureSpec {
    id: Feature::ContextManagement,
    key: "context_management",
    stage: Stage::UnderDevelopment,
    default_enabled: false,
},
""",
    "model_protocol": """
pub struct ModelInfo {
    #[serde(default)]
    pub supports_experimental_context: bool,
}
""",
    "activation": """
fn experimental_context_is_eligible(auth_mode: AuthMode, plan_type: Option<PlanType>) -> bool {
    auth_mode == AuthMode::Chatgpt && matches!(plan_type, Some(PlanType::Plus | PlanType::Pro | PlanType::ProLite))
}
pub fn apply_experimental_context(config: &mut Config, auth: Option<&CodexAuth>, starting_model: &ModelInfo) {
    let provider = &config.model_provider;
    if !config.features.enabled(Feature::ContextManagement)
        || !starting_model.supports_experimental_context
        || !provider.supports_codex_backend_routes()
        || !provider.requires_openai_auth
        || provider.env_key.is_some()
        || provider.experimental_bearer_token.is_some()
        || provider.auth.is_some()
        || provider.aws.is_some()
        || config.features.enable(Feature::TokenBudget).is_err()
        || !config.features.enabled(Feature::TokenBudget) { return; }
    config.token_budget.get_or_insert_default().use_history_notes_extension = true;
}
""",
    "tools": """
const HISTORY_DESCRIPTION: &str = "History is read-only and eventually consistent";
const NOTES_DESCRIPTION: &str = "Read and maintain private notes that survive context-window transitions within this rollout. Paths are virtual, not filesystem paths. Reads, listings, searches, and writes may access other agents' notes. Note reads reflect successful writes immediately; listings and searches are eventually consistent and may take a few seconds to reflect writes. Every file must remain at or below 1,000,000 UTF-8 bytes";
fn endpoint(self) -> &'static str { match self {
A => "alpha/history/v2/list_windows",
B => "alpha/history/v2/list_items",
C => "alpha/history/v2/read_item",
D => "alpha/history/v2/search_contents",
E => "alpha/notes/v2/list_files_by_prefix",
F => "alpha/notes/v2/read_file",
G => "alpha/notes/v2/search_contents",
H => "alpha/notes/v2/append_to_file",
I => "alpha/notes/v2/write_file",
}}
""",
    "backend": """
const HISTORY_NOTES_BACKEND_TIMEOUT: Duration = Duration::from_secs(35);
const ENCRYPTED_TOOL_ARGUMENTS_HEADER: &str = "x-openai-encrypted-tool-arguments";
const TOOL_OUTPUT_TRUNCATION_POLICY_HEADER: &str = "x-openai-tool-output-truncation-policy";
fn call(path: &str, session_id: &str, current_agent_name: &str) {
 let provider = self.provider.api_provider();
 let auth = self.provider.api_auth();
 let mut request = provider.build_request(Method::POST, path);
 let context = json!({"session_id": session_id, "current_agent_name": current_agent_name});
 if matches!(path,
 "alpha/history/v2/search_contents" |
 "alpha/notes/v2/search_contents" |
 "alpha/notes/v2/append_to_file" |
 "alpha/notes/v2/write_file") { let _ = ENCRYPTED_TOOL_ARGUMENTS_HEADER; }
 let _ = TOOL_OUTPUT_TRUNCATION_POLICY_HEADER;
}
""",
    "extension": """
const MAX_THREAD_HINT_BYTES: usize = 4_000;
fn update_config(config: &Config) {
 if config.token_budget.as_ref().is_some_and(|x| x.use_history_notes_extension)
   && config.model_provider.is_openai()
   && self.auth_manager.current_auth_uses_codex_backend() {
   HistoryNotesBackend::new(create_model_provider(config.model_provider.clone(), Some(self.auth_manager.clone())));
 }
}
fn hint() { backend.call("alpha/notes/v2/thread_hint"); }
""",
    "new_context": """
pub const NEW_CONTEXT_WINDOW_MESSAGE: &str = "A new context window will start without summarizing conversation history.";
fn handle(invocation: ToolInvocation) { invocation.session.request_new_context_window().await; }
""",
    "new_context_spec": """
pub const NEW_CONTEXT_WINDOW_TOOL_NAME: &str = "new_context";
fn create_new_context_window_tool() { description: "Start a new context window. Does not clear, reset, or otherwise affect environment state." }
""",
    "compact": """
/// Token-budget compaction skips model/server summarization and installs a fresh context window instead.
fn run_compact_task_inner() { sess.start_new_context_window(step_context, world_state).await; }
""",
    "provider_info": """
const CHATGPT_CODEX_BASE_URL: &str = "https://chatgpt.com/backend-api/codex";
fn supports_codex_backend_routes(&self) -> bool {
 self.is_openai() && self.base_url.as_deref().is_none_or(|base_url| base_url.trim_end_matches('/').ends_with("/backend-api/codex"))
}
""",
    "config_toml": """
/// Base URL for requests to ChatGPT (as opposed to the OpenAI API).
pub chatgpt_base_url: Option<String>,
/// Base URL override for the built-in `openai` model provider.
pub openai_base_url: Option<String>,
""",
    "provider_runtime": "fn api_auth() {}",
    "activation_tests": "model.supports_experimental_context = true;",
    "history_notes_tests": 'const THREAD_HINT: &str = "Recent notes (up to 5, most-recent first):\\n- /root/notes/latest.md (2 lines, 14 UTF-8 bytes)";',
}


def _registry() -> SourceRegistry:
    legacy = load_legacy_modules()
    return from_legacy_maps(legacy.base.FILES, legacy.base.SURFACE_FILES, legacy.entrypoint.EXTRA)


def _snapshot(overrides: dict[str, str] | None = None) -> SourceSnapshot:
    values = dict(SOURCES)
    values.update(overrides or {})
    registry = _registry()
    files: dict[str, SourceFile] = {}
    for key, text in values.items():
        spec_id = SOURCE_IDS[key]
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
        resolved_commit_sha="6af345407d9c2a568da9d01b6c4b81a9e61495c0",
        source_set_sha256=SourceSnapshot.digest_files(files),
        dirty=False,
    )
    return SourceSnapshot(revision=revision, files=files)


def test_context_management_extractor_models_activation_routing_and_state() -> None:
    diagnostics = DiagnosticCollector()
    result = ContextManagementExtractor().extract(_snapshot(), diagnostics)
    assert result.semantic_complete
    assert diagnostics.summary()["error"] == 0
    assert result.data["activation"]["model"]["capability_field"] == "supports_experimental_context"
    assert result.data["routing"]["model_provider_plane"]["top_level_override"] == "openai_base_url"
    assert result.data["routing"]["chatgpt_auxiliary_plane"]["top_level_override"] == "chatgpt_base_url"
    assert result.data["history_notes"]["tool_route_count"] == 9
    assert len(result.data["history_notes"]["routes"]) == 10
    assert result.data["history_notes"]["notes"]["cross_agent_access"] is True
    assert result.data["history_notes"]["thread_hint"]["fixture_contract"]["max_recent_notes"] == 5
    assert result.data["history_notes"]["thread_hint"]["not_proven_as_remote_compaction_summary"] is True
    assert result.data["rollover"]["summarization"] == "none"


def test_model_capability_gate_mutation_fails_visible() -> None:
    mutated = SOURCES["activation"].replace("|| !starting_model.supports_experimental_context\n", "")
    diagnostics = DiagnosticCollector()
    result = ContextManagementExtractor().extract(_snapshot({"activation": mutated}), diagnostics)
    assert not result.semantic_complete
    assert "CONTEXT_MANAGEMENT_MODEL_CAPABILITY_GATE_MISSING" in {item.code for item in diagnostics.values()}


def test_unknown_alpha_route_is_strict_diagnostic() -> None:
    mutated = SOURCES["tools"] + '\nconst X: &str = "alpha/notes/v2/delete_file";\n'
    diagnostics = DiagnosticCollector()
    result = ContextManagementExtractor().extract(_snapshot({"tools": mutated}), diagnostics)
    assert not result.semantic_complete
    assert "ALPHA_ROUTE_UNCLASSIFIED" in {item.code for item in diagnostics.values()}


def test_context_schema_validates_extracted_contract() -> None:
    diagnostics = DiagnosticCollector()
    result = ContextManagementExtractor().extract(_snapshot(), diagnostics)
    schema_path = Path(__file__).parents[1] / "codex_wire_audit" / "proof_schema_templates" / "context-management-semantics-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(result.data)) == []


def test_context_management_profiles_are_executable() -> None:
    profile = resolve_profile("context_management_only")
    assert profile.required_extractors == ("extractor.context_management",)
    full = resolve_profile("codex_wire_full")
    assert "extractor.context_management" in full.required_extractors
    assert "context_management_activation" in full.required_runtime_scenarios
    hybrid = resolve_profile("hybrid_v16")
    assert set(hybrid.required_extractors) == {"extractor.turn_metadata", "extractor.context_management"}


def test_context_management_extractor_is_registered() -> None:
    ids = {extractor.extractor_id for extractor in create_extractors()}
    assert "extractor.context_management" in ids


def test_source_registry_overlay_can_add_new_source_id() -> None:
    registry = _registry()
    updated = registry.with_overlay({
        "schema_version": "1.0.0",
        "sources": {
            "source_spec.extra.future_context_source": {
                "legacy_key": "future_context_source",
                "group": "extra",
                "path_candidates": ["codex-rs/core/src/future_context.rs"],
                "required": False,
                "roles": ["context_management"],
                "expected_symbols": ["FutureContext"],
                "extractor_ids": ["extractor.context_management"]
            }
        }
    })
    assert updated.get("source_spec.extra.future_context_source").primary_path.endswith("future_context.rs")


def test_context_management_reference_container_is_closed_and_hashed() -> None:
    result = verify_context_management_container()
    assert result["reviewed_commit"] == "6af345407d9c2a568da9d01b6c4b81a9e61495c0"
    assert result["member_count"] == 7
    assert "state_model.json" in result["members"]
