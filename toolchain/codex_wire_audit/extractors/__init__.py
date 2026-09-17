"""Canonical extractor registry and deterministic built-in activation."""

from importlib import import_module

from .registry import Extractor, ExtractorResult, create_extractors, register_extractor

_BUILTIN_MODULES = (
    "turn_metadata",
    "context_management",
    "config_effects",
    "local_storage",
    "prompt_context",
    "execution_policy",
    "plugin_runtime",
    "execution_environment",
    "routing_transport",
    "redirect_headers",
    "mcp_projection",
    "responses_protocol",
    "responses_server_response",
    "app_server_rpc",
    "runtime_behavior",
)
for _module_name in _BUILTIN_MODULES:
    import_module(f"{__name__}.{_module_name}")
del _module_name

__all__ = [
    "Extractor",
    "ExtractorResult",
    "create_extractors",
    "register_extractor",
]
