"""Declarative links from user configuration to canonical protocol surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class ConfigEffectSpec:
    id: str
    config_paths: tuple[str, ...]
    target_id: str
    relation: str
    behavior: str
    proof_tier: str = "canonical_source_link"
    condition: str | None = None


_SPECS = (
    ConfigEffectSpec("effect.model", ("model",), "surface.responses.body.model", "selects", "Selects the model slug serialized into Responses requests."),
    ConfigEffectSpec("effect.model_provider", ("model_provider",), "surface.provider.selection", "selects", "Selects the model provider and its endpoint/auth/capability plane."),
    ConfigEffectSpec("effect.reasoning_effort", ("model_reasoning_effort",), "surface.responses.body.reasoning.effort", "projects_to", "Controls the normalized reasoning effort request field."),
    ConfigEffectSpec("effect.reasoning_summary", ("model_reasoning_summary",), "surface.responses.body.reasoning.summary", "projects_to", "Controls reasoning-summary request policy."),
    ConfigEffectSpec("effect.verbosity", ("model_verbosity",), "surface.responses.body.text.verbosity", "projects_to", "Controls Responses text verbosity when supported by the model."),
    ConfigEffectSpec("effect.service_tier", ("service_tier",), "surface.responses.body.service_tier", "projects_to", "Selects the requested service tier after support/default filtering."),
    ConfigEffectSpec("effect.web_search", ("web_search",), "surface.tool.web_search", "controls_exposure", "Controls hosted web-search tool exposure."),
    ConfigEffectSpec("effect.responses_metadata", ("responses_api_metadata", "responses_api_metadata.*"), "surface.turn_metadata.extra", "merges_into", "Validated product metadata is flattened into canonical nested turn metadata."),
    ConfigEffectSpec("effect.mcp_servers", ("mcp_servers", "mcp_servers.*"), "surface.mcp.registry", "defines", "Defines MCP transports, authentication, lifecycle, and model-visible tools."),
    ConfigEffectSpec("effect.chatgpt_base", ("chatgpt_base_url",), "surface.routing.chatgpt_auxiliary", "routes", "Selects the ChatGPT auxiliary backend authority."),
    ConfigEffectSpec("effect.openai_base", ("openai_base_url",), "surface.routing.model_provider", "routes", "Overrides the built-in OpenAI provider authority, including Codex backend routes."),
    ConfigEffectSpec("effect.provider_base", ("model_providers.*.base_url",), "surface.routing.model_provider", "routes", "Defines a configured provider's API authority."),
    ConfigEffectSpec("effect.provider_auth", ("model_providers.*.env_key", "model_providers.*.experimental_bearer_token", "model_providers.*.auth", "model_providers.*.aws"), "surface.provider.credentials", "configures", "Configures provider-owned credentials and disables official experimental-context activation."),
    ConfigEffectSpec("effect.provider_openai_auth", ("model_providers.*.requires_openai_auth",), "surface.provider.openai_auth_requirement", "configures", "Controls whether the provider consumes Codex-managed OpenAI/ChatGPT authentication."),
    ConfigEffectSpec("effect.context_management", ("features.context_management", "features.context_management.experimental_mode"), "surface.context_management.activation", "requests_enablement", "Requests experimental no-summary context-window management; account, provider, and model gates still apply."),
    ConfigEffectSpec("effect.token_budget", ("features.token_budget", "features.token_budget.enabled"), "surface.context_management.rollover", "enables", "Enables token-budget context metadata and fresh-window rollover mechanics."),
    ConfigEffectSpec("effect.history_notes", ("features.token_budget.use_history_notes_extension",), "surface.context_management.history_notes", "enables", "Exposes remote History/Notes tools and the thread-hint contributor."),
    ConfigEffectSpec("effect.tool_metadata", ("features.tool_registry.turn_metadata_includes_tool_info",), "surface.turn_metadata.tool_namespaces_info", "enables", "Includes authoritative tool namespace/function information in per-turn metadata when applicable."),
)


def effect_specs() -> tuple[ConfigEffectSpec, ...]:
    return _SPECS


def expanded_config_paths(spec: ConfigEffectSpec, available_paths: Iterable[str]) -> tuple[str, ...]:
    available = set(available_paths)
    result: set[str] = set()
    for path in spec.config_paths:
        if path in available:
            result.add(path)
        profile = f"profiles.*.{path}"
        if profile in available:
            result.add(profile)
    return tuple(sorted(result))
