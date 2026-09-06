#!/usr/bin/env python3
"""Codex wire audit v10: normalized wire profiles, deltas, and protocol taxonomy.

The stable extractor lives in ``_codex_wire_audit_base_v10.py``. This entrypoint
keeps the v1/v2 report keys and adds the converged metadata schema, relation
map, protocol/config inputs, generated bidirectional app-server method inventories,
and the v10 wire-surface catalog produced by the base extractor with repository
drift coverage and explicit scope exclusions.

The report deliberately distinguishes:
- HTTP request headers vs HTTP response headers,
- Content-Type (request body format) vs Accept (desired response format),
- unary JSON vs SSE vs Responses WebSocket vs Realtime WebSocket/WebRTC,
- free_guardian routing: normal /responses vs unmetered /guardian and /guardian-classifier,
- /responses vs /guardian vs /guardian-classifier vs legacy /responses/compact,
- model-inference wire vs ChatGPT/Codex backend account-control HTTP,
- app-server client↔Codex JSON-RPC traffic vs upstream HTTP/WSS traffic,
- account usage/rate-limit reads vs reset-credit detail/consume flows,
- shared header/client_metadata/body/event profiles vs transport-specific deltas,
- ResponsesApiRequest vs ResponseCreateWsRequest field-level wire differences,
- generated client requests, server requests, and bidirectional app-server notifications,
- model discovery, hosted-file trust-boundary transitions, and cost/analytics surfaces,
- wire-affecting config and feature settings with runtime/request projections,
- MCP server configuration, transport, tool exposure, and model-facing namespace projection,
- the complete Responses request/event/item protocol and the Responses Lite rewrite,
- top-level client_metadata, nested x-codex-turn-metadata, and flattened extra lineage,
- normalized wire presence/types, stable entity IDs, typed edges, and structured rules,
- formal report/protocol schemas, source hashes, canonical JSON, and diagnostics,
- live GitHub, local checkout, source-archive, and immutable cache-only source modes.

"""
from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

P = Path(__file__).with_name("_codex_wire_audit_base_v10.py")
S = importlib.util.spec_from_file_location("_codex_wire_audit_base", P)
if S is None or S.loader is None:
    raise RuntimeError(f"cannot load {P}")
B = importlib.util.module_from_spec(S)
S.loader.exec_module(B)
AuditError, REPO, REF, API = B.AuditError, B.REPO, B.REF, B.API
REPORT_SCHEMA_VERSION = B.REPORT_SCHEMA_VERSION
CATALOG_SCHEMA_VERSION = B.CATALOG_SCHEMA_VERSION
GENERATOR_VERSION = B.GENERATOR_VERSION

CONTRACT_PATH = Path(__file__).with_name("_codex_wire_contract_v10.py")
CONTRACT_SPEC = importlib.util.spec_from_file_location("_codex_wire_contract_v10", CONTRACT_PATH)
if CONTRACT_SPEC is None or CONTRACT_SPEC.loader is None:
    raise RuntimeError(f"cannot load {CONTRACT_PATH}")
C = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(C)

EXTRA = {
    "turn_metadata": "codex-rs/core/src/turn_metadata.rs",
    "protocol": "codex-rs/protocol/src/protocol.rs",
    "protocol_turn_input": "codex-rs/protocol/src/turn_input.rs",
    "app_server_turn": "codex-rs/app-server-protocol/src/protocol/v2/turn.rs",
    "config_toml": "codex-rs/config/src/config_toml.rs",
    "core_config": "codex-rs/core/src/config/mod.rs",
    "app_server_readme": "codex-rs/app-server/README.md",
    "app_server_realtime": "codex-rs/app-server-protocol/src/protocol/v2/realtime.rs",
    "app_server_common": "codex-rs/app-server-protocol/src/protocol/common.rs",
    "app_server_account": "codex-rs/app-server-protocol/src/protocol/v2/account.rs",
    "app_server_rate_limit_resets": "codex-rs/app-server/src/request_processors/account_processor/rate_limit_resets.rs",
    # v10: configuration, MCP, full Responses, Responses Lite, metadata lineage, and machine contract.
    "feature_registry": "codex-rs/features/src/lib.rs",
    "feature_configs": "codex-rs/features/src/feature_configs.rs",
    "mcp_types": "codex-rs/config/src/mcp_types.rs",
    "mcp_runtime": "codex-rs/codex-mcp/src/mcp/mod.rs",
    "mcp_binding": "codex-rs/codex-mcp/src/binding.rs",
    "mcp_catalog": "codex-rs/codex-mcp/src/catalog.rs",
    "rmcp_client": "codex-rs/rmcp-client/src/rmcp_client.rs",
    "rmcp_protocol_mode": "codex-rs/rmcp-client/src/protocol_mode.rs",
    "route_aware_redirect": "codex-rs/http-client/src/route_aware_redirect.rs",
    "mcp_http_redirect": "codex-rs/rmcp-client/src/http_client_redirect.rs",
    "mcp_http_headers": "codex-rs/rmcp-client/src/http_headers.rs",
    "mcp_handler": "codex-rs/core/src/tools/handlers/mcp.rs",
    "mcp_tool_call": "codex-rs/core/src/mcp_tool_call.rs",
    "hook_runtime": "codex-rs/core/src/hook_runtime.rs",
    "tool_spec_plan": "codex-rs/core/src/tools/spec_plan.rs",
    "tool_namespaces_info": "codex-rs/core/src/tools/tool_namespaces_info.rs",
    "tool_spec": "codex-rs/tools/src/tool_spec.rs",
    "tool_responses_api": "codex-rs/tools/src/responses_api.rs",
    "protocol_models": "codex-rs/protocol/src/models.rs",
    "protocol_config_types": "codex-rs/protocol/src/config_types.rs",
    "response_usage": "codex-rs/protocol/src/response_usage.rs",
    "model_protocol": "codex-rs/protocol/src/openai_models.rs",
    "provider_runtime": "codex-rs/model-provider/src/provider.rs",
}


def ln(s: str, i: int) -> int:
    return s.count("\n", 0, max(i, 0)) + 1

def anchor(s: str | None, path: str, needle: str, symbol: str) -> dict[str, Any] | None:
    if not s: return None
    i = s.find(needle)
    return None if i < 0 else {"path": path, "line": ln(s, i), "symbol": symbol}


def fetch_extra(repo: str, sha: str, api: str, token: str | None):
    out, warnings = {}, []
    for k, path in EXTRA.items():
        try: out[k] = B.fetch_file(repo, sha, path, api, token)
        except AuditError as e: warnings.append(f"optional schema source unavailable: {path}: {e}")
    return out, warnings


def build_redirect_header_protocol(extra: dict[str, str]) -> dict[str, Any]:
    """Describe Codex-generated Referer behavior on redirect hops.

    Referer is deliberately not classified as an ordinary first-hop Responses
    header.  Codex constructs it while manually following redirects in two
    transport layers: the route-aware HTTP client and the MCP same-origin
    redirect client.
    """
    route = extra.get("route_aware_redirect", "")
    mcp = extra.get("mcp_http_redirect", "")
    helper = extra.get("mcp_http_headers", "")
    warnings: list[str] = []
    profiles: list[dict[str, Any]] = []

    if route:
        checks = {
            "removes_stale_referer": "headers.remove(REFERER);" in route,
            "suppresses_https_to_http": 'next.scheme() == "http" && previous.scheme() == "https"' in route,
            "strips_userinfo": "referer.set_username" in route and "referer.set_password" in route,
            "strips_fragment": "referer.set_fragment(None)" in route,
            "cross_origin_reduces_to_origin": "if !same_origin(previous, next)" in route and "referer.set_path(\"/\")" in route and "referer.set_query(None)" in route,
            "inserts_referer": "headers.insert(REFERER, value)" in route,
        }
        if not all(checks.values()):
            warnings.append("route-aware Referer redirect anchors changed upstream")
        profiles.append({
            "id": "route_aware_http_redirect_referer",
            "transport_family": "route_aware_http",
            "applies_when": "RouteAwareClientPool manually follows an HTTP redirect after route/proxy selection",
            "redirect_statuses": [301, 302, 303, 307, 308],
            "first_request_generated": False,
            "redirect_request_generated": True,
            "stale_header_policy": "remove before recomputing",
            "same_origin_value": "previous absolute URL without userinfo or fragment; path and query retained",
            "cross_origin_value": "previous origin root only; path and query removed",
            "https_to_http_downgrade": "omit Referer",
            "source_checks": checks,
            "source": anchor(
                route,
                EXTRA["route_aware_redirect"],
                "pub(super) fn insert_referer(",
                "route_aware_redirect::insert_referer",
            ),
        })
    else:
        warnings.append("route-aware redirect source unavailable; Referer profile incomplete")

    if mcp:
        checks = {
            "same_origin_only": "next_url.origin() != original_origin" in mcp,
            "removes_stale_referer": '!header.name.eq_ignore_ascii_case("referer")' in mcp,
            "strips_userinfo": "referer.set_username" in mcp and "referer.set_password" in mcp,
            "strips_fragment": "referer.set_fragment(None)" in mcp,
            "inserts_referer": 'name: "referer".to_string()' in mcp,
            "non_loopback_http_redirect_requires_https": "MCP HTTP redirects for non-loopback hostnames require HTTPS" in mcp,
        }
        if not all(checks.values()):
            warnings.append("MCP Referer redirect anchors changed upstream")
        profiles.append({
            "id": "mcp_same_origin_redirect_referer",
            "transport_family": "mcp_streamable_http",
            "applies_when": "SameOriginRedirectHttpClient follows an accepted MCP HTTP redirect",
            "redirect_statuses": [301, 302, 303, 307, 308],
            "first_request_generated": False,
            "redirect_request_generated": True,
            "redirect_origin_policy": "reject any redirect whose origin differs from the configured original origin",
            "stale_header_policy": "remove before recomputing",
            "same_origin_value": "previous absolute URL without userinfo or fragment; path and query retained",
            "cross_origin_value": "not sent because cross-origin redirects are rejected",
            "plaintext_redirect_policy": "non-loopback hostname redirects require HTTPS",
            "source_checks": checks,
            "source": anchor(
                mcp,
                EXTRA["mcp_http_redirect"],
                "pub(crate) struct SameOriginRedirectHttpClient",
                "SameOriginRedirectHttpClient",
            ),
        })
    else:
        warnings.append("MCP redirect source unavailable; Referer profile incomplete")

    helper_policy = {
        "http_headers_helper_may_emit_referer": None,
        "note": "static/caller-provided custom headers are outside this generated-Referer profile",
        "source": None,
    }
    if helper:
        reserved = '"referer"' in helper and "MCP HTTP headers helper returned a reserved header" in helper
        helper_policy.update({
            "http_headers_helper_may_emit_referer": not reserved,
            "note": (
                "MCP http_headers_helper rejects Referer as reserved; redirect handling owns recomputation"
                if reserved
                else "Referer helper reservation anchor was not found"
            ),
            "source": anchor(
                helper,
                EXTRA["mcp_http_headers"],
                '| "referer"',
                "parse_helper_output reserved headers",
            ),
        })
        if not reserved:
            warnings.append("MCP helper Referer reservation anchor changed upstream")

    return {
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
            "logging_policy": "redact or minimize URL path/query when logging",
        },
        "profiles": profiles,
        "mcp_helper_policy": helper_policy,
        "scope_note": (
            "This profile covers Codex-generated Referer values on redirect hops. It does not claim "
            "that every initial request lacks a caller/provider-configured custom Referer."
        ),
        "warnings": warnings,
    }


def named_body(text: str, kind: str, name: str) -> tuple[str, int, int]:
    m = re.search(rf"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?{kind}\s+{re.escape(name)}\b[^{{]*\{{", text)
    if not m: raise AuditError(f"{kind} not found: {name}")
    start = m.end()
    close = B.find_rust_matching_brace(text, start - 1)
    return text[start:close], start, m.start()


def struct_field(text: str, path: str, struct: str, field: str) -> dict[str, Any] | None:
    """Return one named struct field using the multiline-aware base parser."""
    for item in struct_summary(text, path, struct):
        if item.get("name") == field:
            return item
    return None


def struct_summary(text: str, path: str, struct: str) -> list[dict[str, Any]]:
    """Return struct fields with struct-level serde rename_all applied."""
    _body, _off, decl = named_body(text, "struct", struct)
    fields = B.struct_fields(text, path, struct)
    prefix = text[max(0, decl-1200):decl]
    attrs = " ".join(re.findall(r"#\[serde\((.*?)\)\]", prefix, re.S)[-2:])
    rename_all = (re.search(r'rename_all\s*=\s*"([^"]+)"', attrs) or [None, None])[1]

    def rename(name: str) -> str:
        if rename_all == "camelCase":
            parts = name.split("_")
            return parts[0] + "".join(x[:1].upper() + x[1:] for x in parts[1:])
        if rename_all == "snake_case":
            return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        if rename_all == "lowercase":
            return name.lower()
        if rename_all == "UPPERCASE":
            return name.upper()
        return name

    out = []
    for field in fields:
        item = dict(field)
        if item.get("wire_name") == item.get("name"):
            item["wire_name"] = rename(item["name"])
        out.append(item)
    return out


def try_struct_summary(text: str, path: str, struct: str) -> list[dict[str, Any]]:
    if not text:
        return []
    try:
        return struct_summary(text, path, struct)
    except AuditError:
        return []


def _snake_case(name: str) -> str:
    """Apply serde-style snake_case closely enough for protocol inventory output."""
    first = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", first).replace("__", "_").lower()


def _rename_rust_ident(name: str, rename_all: str | None) -> str:
    if rename_all == "camelCase":
        snake = _snake_case(name)
        parts = snake.split("_")
        return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
    if rename_all == "PascalCase":
        snake = _snake_case(name)
        return "".join(part[:1].upper() + part[1:] for part in snake.split("_"))
    if rename_all == "snake_case":
        return _snake_case(name)
    if rename_all == "kebab-case":
        return _snake_case(name).replace("_", "-")
    if rename_all == "SCREAMING_SNAKE_CASE":
        return _snake_case(name).upper()
    if rename_all == "lowercase":
        return name.lower()
    if rename_all == "UPPERCASE":
        return name.upper()
    return name


def _consume_rust_attributes(text: str) -> tuple[list[str], str, int]:
    """Consume leading Rust attributes and return attrs, declaration, offset."""
    attrs: list[str] = []
    cursor = 0
    while True:
        whitespace = re.match(r"\s*", text[cursor:])
        if whitespace:
            cursor += whitespace.end()
        if not text.startswith("#[", cursor):
            break
        index = cursor + 2
        depth = 1
        quote: str | None = None
        escaped = False
        while index < len(text) and depth:
            char = text[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in {'"', "'"}:
                quote = char
            elif char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            index += 1
        if depth:
            break
        attrs.append(text[cursor:index].strip())
        cursor = index
    return attrs, text[cursor:].strip(), cursor


def enum_summary(text: str, path: str, enum: str) -> dict[str, Any]:
    """Return a source-linked enum wire schema, including multiline variants."""
    body, off, decl = named_body(text, "enum", enum)
    prefix = text[max(0, decl - 2000):decl]
    serde_attrs = re.findall(r"#\[serde\((.*?)\)\]", prefix, re.S)
    serde = " ".join(serde_attrs[-3:])
    custom = bool(re.search(r'\b(?:try_from|into)\s*=\s*"String"', serde))
    tag = (re.search(r'\btag\s*=\s*"([^"]+)"', serde) or [None, None])[1]
    content = (re.search(r'\bcontent\s*=\s*"([^"]+)"', serde) or [None, None])[1]
    rename_all = (re.search(r'\brename_all\s*=\s*"([^"]+)"', serde) or [None, None])[1]
    untagged = bool(re.search(r"(?:^|[,\s])untagged(?:[,\s]|$)", serde))
    variants: list[dict[str, Any]] = []
    for raw_item, item_offset in B._split_rust_top_level_commas(body):
        declaration, leading_offset = B._strip_leading_rust_comments(raw_item)
        attrs, declaration, attr_offset = _consume_rust_attributes(declaration)
        if not declaration:
            continue
        match = re.match(r"(?s)^([A-Z][A-Za-z0-9_]*)\b(.*)$", declaration)
        if not match:
            continue
        name = match.group(1)
        tail = match.group(2).strip()
        attr_text = " ".join(attrs)
        explicit = re.search(r'\brename\s*=\s*"([^"]+)"', attr_text)
        aliases = re.findall(r'\balias\s*=\s*"([^"]+)"', attr_text)
        if custom:
            wire = None
            wire_source = "custom String conversion"
        elif explicit:
            wire = explicit.group(1)
            wire_source = "serde(rename)"
        else:
            wire = _rename_rust_ident(name, rename_all)
            wire_source = f"serde(rename_all={rename_all})" if rename_all else "Rust variant name"

        shape = "unit"
        payload_type: str | None = None
        fields: list[dict[str, Any]] = []
        discriminant: str | None = None
        if tail.startswith("("):
            shape = "tuple"
            payload_type = " ".join(tail[1:tail.rfind(")")].split()) if ")" in tail else tail
        elif tail.startswith("{"):
            shape = "struct"
            close = tail.rfind("}")
            field_body = tail[1:close] if close >= 0 else tail[1:]
            synthetic = f"struct {enum}{name} {{\n{field_body}\n}}"
            try:
                fields = B.struct_fields(synthetic, path, f"{enum}{name}")
                for field in fields:
                    local = raw_item.find(str(field.get("name", "")))
                    if local >= 0:
                        field["source"] = {
                            "path": path,
                            "line": ln(text, off + item_offset + local),
                            "symbol": f"{enum}::{name}",
                        }
            except AuditError:
                fields = []
        elif tail.startswith("="):
            discriminant = " ".join(tail[1:].split())
        variants.append({
            "name": name,
            "wire_name": wire,
            "wire_name_source": wire_source,
            "aliases": aliases,
            "shape": shape,
            "payload_type": payload_type,
            "fields": fields,
            "discriminant": discriminant,
            "serde": attrs,
            "serde_other": any("serde(other" in attr.replace(" ", "") for attr in attrs),
            "source": {
                "path": path,
                "line": ln(text, off + item_offset + leading_offset + attr_offset),
                "symbol": enum,
            },
        })
    return {
        "name": enum,
        "serde": serde,
        "tag": tag,
        "content": content,
        "untagged": untagged,
        "rename_all": rename_all,
        "custom_string_conversion": custom,
        "wire_contract": "custom String conversion" if custom else "serde enum serialization",
        "variants": variants,
    }


def try_enum_summary(text: str | None, path: str, enum: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        return enum_summary(text, path, enum)
    except AuditError:
        return None


def _balanced_macro_body(text: str, macro_name: str) -> tuple[str, int] | None:
    """Return the invocation body for `macro_name! { ... }`, excluding braces."""
    match = re.search(rf"\b{re.escape(macro_name)}!\s*\{{", text)
    if not match:
        return None
    open_index = text.find("{", match.start())
    try:
        close_index = B.find_rust_matching_brace(text, open_index)
    except AuditError:
        return None
    return text[open_index + 1:close_index], open_index + 1


def _line_value(snippet: str, field: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(field)}\s*:\s*(.*?)\s*,\s*$", snippet)
    return " ".join(match.group(1).split()) if match else None


def _pascal_to_camel(name: str) -> str:
    return _rename_rust_ident(name, "camelCase")


def _explicit_wire_macro_entries(
    text: str,
    path: str,
    macro_name: str,
    direction: str,
) -> list[dict[str, Any]]:
    """Extract macro entries with either explicit wire names or serde camelCase defaults."""
    located = _balanced_macro_body(text, macro_name)
    if not located:
        return []
    body, body_offset = located
    pattern = re.compile(
        r"(?m)^(?P<prefix>(?:(?:\s*#\[[^\n]+\]|\s*///[^\n]*|\s*//[^\n]*)\n)*)"
        r"\s*(?P<variant>[A-Z][A-Za-z0-9_]*)"
        r"\s*(?:=>\s*\"(?P<wire>[^\"]+)\")?\s*(?P<shape>[{(])"
    )
    matches = list(pattern.finditer(body))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        snippet = body[match.start():end]
        prefix = match.group("prefix") or ""
        experimental = re.search(r"#\[experimental\((.*?)\)\]", prefix)
        paren_type = None
        if match.group("shape") == "(":
            payload = re.search(
                rf"{re.escape(match.group('variant'))}\s*(?:=>\s*\"[^\"]+\")?\s*\(([^\n)]*)\)",
                snippet,
            )
            paren_type = payload.group(1).strip() if payload else None
        wire = match.group("wire") or _pascal_to_camel(match.group("variant"))
        rows.append({
            "variant": match.group("variant"),
            "method": wire,
            "wire_name_source": "explicit" if match.group("wire") else "serde rename_all=camelCase",
            "direction": direction,
            "params_type": _line_value(snippet, "params") or paren_type,
            "response_type": _line_value(snippet, "response"),
            "serialization": _line_value(snippet, "serialization"),
            "experimental": bool(experimental),
            "experimental_reason": experimental.group(1).strip() if experimental else None,
            "source": {
                "path": path,
                "line": ln(text, body_offset + match.start()),
                "symbol": f"{macro_name}!::{match.group('variant')}",
            },
        })
    return rows


def _implicit_notification_entries(text: str, path: str) -> list[dict[str, Any]]:
    located = _balanced_macro_body(text, "client_notification_definitions")
    if not located:
        return []
    body, body_offset = located
    rows = []
    pattern = re.compile(
        r"(?m)^\s*([A-Z][A-Za-z0-9_]*)\s*(?:\(([^\n)]*)\))?\s*,?\s*$"
    )
    for match in pattern.finditer(body):
        variant = match.group(1)
        rows.append({
            "variant": variant,
            "method": _pascal_to_camel(variant),
            "wire_name_source": "serde rename_all=camelCase",
            "direction": "client_to_server_notification",
            "params_type": match.group(2).strip() if match.group(2) else None,
            "response_type": None,
            "serialization": None,
            "experimental": False,
            "experimental_reason": None,
            "source": {
                "path": path,
                "line": ln(text, body_offset + match.start()),
                "symbol": f"client_notification_definitions!::{variant}",
            },
        })
    return rows


def app_server_method_inventory(common: str) -> dict[str, Any]:
    path = EXTRA["app_server_common"]
    client_requests = _explicit_wire_macro_entries(
        common, path, "client_request_definitions", "client_to_server_request"
    )
    server_requests = _explicit_wire_macro_entries(
        common, path, "server_request_definitions", "server_to_client_request"
    )
    server_notifications = _explicit_wire_macro_entries(
        common, path, "server_notification_definitions", "server_to_client_notification"
    )
    client_notifications = _implicit_notification_entries(common, path)
    warnings = []
    expected_groups = (
        ("client_request_definitions", client_requests),
        ("server_request_definitions", server_requests),
        ("server_notification_definitions", server_notifications),
        ("client_notification_definitions", client_notifications),
    )
    for macro_name, entries in expected_groups:
        if common and f"{macro_name}!" in common and not entries:
            warnings.append(f"{macro_name}! inventory was not extracted")

    duplicate_methods: dict[str, list[str]] = {}
    for group_name, entries in (
        ("client_requests", client_requests),
        ("server_requests", server_requests),
        ("server_notifications", server_notifications),
        ("client_notifications", client_notifications),
    ):
        seen: set[str] = set()
        duplicates: list[str] = []
        for entry in entries:
            method = str(entry.get("method", ""))
            if method in seen and method not in duplicates:
                duplicates.append(method)
            seen.add(method)
        if duplicates:
            duplicate_methods[group_name] = duplicates
            warnings.append(
                f"duplicate {group_name} wire methods: {', '.join(duplicates)}"
            )
    return {
        "exhaustive_for_source_macros": not warnings,
        "client_requests": client_requests,
        "server_requests": server_requests,
        "server_notifications": server_notifications,
        "client_notifications": client_notifications,
        "counts": {
            "client_requests": len(client_requests),
            "server_requests": len(server_requests),
            "server_notifications": len(server_notifications),
            "client_notifications": len(client_notifications),
        },
        "duplicate_methods": duplicate_methods,
        "warnings": warnings,
        "source": anchor(common, path, "client_request_definitions!", "app-server generated method inventory"),
    }



def _field_index(fields: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(field.get("name")): field for field in fields}


def _source_for_field(fields: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return _field_index(fields).get(name, {}).get("source")


def _source_anchor_any(
    text: str | None,
    path: str,
    needles: list[str] | tuple[str, ...],
    symbol: str,
) -> dict[str, Any] | None:
    if not text:
        return None
    for needle in needles:
        found = anchor(text, path, needle, symbol)
        if found:
            return found
    return None


def _rust_struct_literal_fields(block: str) -> dict[str, str]:
    """Parse `name: expression` entries from an already brace-free Rust literal."""
    result: dict[str, str] = {}
    for raw, _ in B._split_rust_top_level_commas(block):
        declaration, _ = B._strip_leading_rust_comments(raw)
        _attrs, declaration, _ = _consume_rust_attributes(declaration)
        match = re.match(r"(?s)^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$", declaration)
        if match:
            result[match.group(1)] = " ".join(match.group(2).split())
            continue
        shorthand = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)", declaration.strip())
        if shorthand:
            result[shorthand.group(1)] = shorthand.group(1)
    return result




def _struct_literal_assignments(
    text: str | None,
    path: str,
    function_anchor: str,
    struct_name: str,
) -> dict[str, Any]:
    """Extract one named struct literal from a function body and validate its assignments."""
    if not text:
        return {
            "struct": struct_name,
            "assignments": [],
            "assignment_map": {},
            "warnings": [f"source unavailable for {function_anchor}"],
            "source": None,
        }
    function = B._fn_body(text, function_anchor)
    if not function:
        return {
            "struct": struct_name,
            "assignments": [],
            "assignment_map": {},
            "warnings": [f"function body not found: {function_anchor}"],
            "source": None,
        }
    marker = f"{struct_name} {{"
    marker_index = function.find(marker)
    if marker_index < 0:
        return {
            "struct": struct_name,
            "assignments": [],
            "assignment_map": {},
            "warnings": [f"struct literal not found in {function_anchor}: {struct_name}"],
            "source": None,
        }
    open_index = function.find("{", marker_index)
    try:
        close_index = B.find_rust_matching_brace(function, open_index)
    except AuditError as exc:
        return {
            "struct": struct_name,
            "assignments": [],
            "assignment_map": {},
            "warnings": [f"could not parse {struct_name} literal: {exc}"],
            "source": None,
        }
    literal = function[open_index + 1:close_index]
    assignment_map = _rust_struct_literal_fields(literal)
    signature_start = text.find(function_anchor)
    function_open = text.find("{", signature_start) if signature_start >= 0 else -1
    literal_start = (
        function_open + open_index + 1
        if function_open >= 0
        else 0
    )
    assignments = [
        {
            "field": name,
            "expression": expression,
            "source": {
                "path": path,
                "line": ln(text, literal_start + max(literal.find(name), 0)),
                "symbol": f"{function_anchor}::{struct_name}.{name}",
            },
        }
        for name, expression in assignment_map.items()
    ]
    return {
        "struct": struct_name,
        "assignments": assignments,
        "assignment_map": assignment_map,
        "warnings": [],
        "source": anchor(text, path, function_anchor, function_anchor),
    }


def _index_rows_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        grouped.setdefault(str(value), []).append(row)
    return dict(sorted(grouped.items()))

def feature_registry_summary(text: str | None) -> dict[str, Any]:
    """Extract the centralized FeatureSpec registry and structured feature TOML shape."""
    path = EXTRA["feature_registry"]
    if not text:
        return {
            "features": [],
            "by_id": {},
            "by_key": {},
            "features_toml_fields": [],
            "warnings": ["feature registry source unavailable"],
            "source": None,
        }
    start = text.find("pub const FEATURES")
    if start < 0:
        return {
            "features": [],
            "by_id": {},
            "by_key": {},
            "features_toml_fields": try_struct_summary(text, path, "FeaturesToml"),
            "warnings": ["pub const FEATURES registry anchor changed upstream"],
            "source": None,
        }
    cursor = start
    rows: list[dict[str, Any]] = []
    while True:
        match = re.search(r"\bFeatureSpec\s*\{", text[cursor:])
        if not match:
            break
        open_index = cursor + text[cursor:].find("{", match.start())
        try:
            close_index = B.find_rust_matching_brace(text, open_index)
        except AuditError:
            break
        literal = text[open_index + 1:close_index]
        values = _rust_struct_literal_fields(literal)
        feature_match = re.search(r"Feature::([A-Za-z0-9_]+)", values.get("id", ""))
        key_match = re.search(r'"([^"]+)"', values.get("key", ""))
        stage_match = re.search(r"Stage::([A-Za-z0-9_]+)", values.get("stage", ""))
        if feature_match and key_match:
            stage_expression = values.get("stage")
            rows.append({
                "id": feature_match.group(1),
                "key": key_match.group(1),
                "stage": stage_match.group(1) if stage_match else None,
                "stage_expression": stage_expression,
                "default_enabled_expression": values.get("default_enabled"),
                "default_enabled": (
                    True if values.get("default_enabled") == "true"
                    else False if values.get("default_enabled") == "false"
                    else None
                ),
                "source": {
                    "path": path,
                    "line": ln(text, cursor + match.start()),
                    "symbol": f"FeatureSpec::{feature_match.group(1)}",
                },
            })
        cursor = close_index + 1
    duplicate_ids = sorted({row["id"] for row in rows if sum(r["id"] == row["id"] for r in rows) > 1})
    duplicate_keys = sorted({row["key"] for row in rows if sum(r["key"] == row["key"] for r in rows) > 1})
    warnings = []
    if not rows:
        warnings.append("FeatureSpec registry was present but no entries were extracted")
    if duplicate_ids:
        warnings.append("duplicate feature ids: " + ", ".join(duplicate_ids))
    if duplicate_keys:
        warnings.append("duplicate feature keys: " + ", ".join(duplicate_keys))
    return {
        "features": rows,
        "by_id": {row["id"]: row for row in rows},
        "by_key": {row["key"]: row for row in rows},
        "feature_enum": try_enum_summary(text, path, "Feature"),
        "features_toml_fields": try_struct_summary(text, path, "FeaturesToml"),
        "counts": {
            "total": len(rows),
            "by_stage": {
                stage: sum(row.get("stage") == stage for row in rows)
                for stage in sorted({row.get("stage") for row in rows if row.get("stage")})
            },
        },
        "warnings": warnings,
        "source": anchor(text, path, "pub const FEATURES", "FeatureSpec registry"),
    }


def build_config_protocol(
    extra: dict[str, str],
    base_src: dict[str, str],
) -> dict[str, Any]:
    """Build a config-to-runtime-to-wire causal catalog."""
    config_toml = extra.get("config_toml", "")
    core_config = extra.get("core_config", "")
    feature_source = extra.get("feature_registry", "")
    feature_configs = extra.get("feature_configs", "")
    config_fields = try_struct_summary(config_toml, EXTRA["config_toml"], "ConfigToml")
    runtime_fields = try_struct_summary(core_config, EXTRA["core_config"], "Config")
    tool_registry_fields = (
        try_struct_summary(core_config, EXTRA["core_config"], "ToolRegistryConfig")
        or try_struct_summary(feature_configs, EXTRA["feature_configs"], "ToolRegistryConfig")
    )
    feature_registry = feature_registry_summary(feature_source)
    config_by_name = _field_index(config_fields)
    runtime_by_name = _field_index(runtime_fields)
    feature_by_id = feature_registry.get("by_id", {})
    warnings: list[str] = list(feature_registry.get("warnings", []))
    settings: list[dict[str, Any]] = []

    def add_config(
        setting: str,
        *,
        category: str,
        runtime: str,
        effects: list[dict[str, Any]],
        notes: list[str] | None = None,
        effective_field: str | None = None,
    ) -> None:
        field = config_by_name.get(setting)
        effective = runtime_by_name.get(effective_field or setting)
        settings.append({
            "setting": setting,
            "syntax": setting if setting != "mcp_servers" else "mcp_servers.<server-name>.*",
            "owner": "ConfigToml",
            "category": category,
            "configured_type": field.get("type") if field else None,
            "effective_runtime": runtime,
            "effective_type": effective.get("type") if effective else None,
            "present_in_config_toml": bool(field),
            "present_in_effective_config": bool(effective),
            "wire_effects": effects,
            "notes": notes or [],
            "source": field.get("source") if field else None,
            "runtime_source": effective.get("source") if effective else None,
        })
        if config_toml and not field:
            warnings.append(f"wire-affecting ConfigToml field not found: {setting}")

    def effect(layer: str, path: str, behavior: str, condition: str | None = None) -> dict[str, Any]:
        return {"layer": layer, "path": path, "behavior": behavior, "condition": condition}

    add_config(
        "model",
        category="model_selection",
        runtime="Config.model / per-turn ModelInfo",
        effects=[
            effect("request_body", "ResponsesApiRequest.model", "selects the model slug"),
            effect("request_header", "x-codex-routing-hint.model", "projects the model for first-party Codex routing"),
            effect("request_cache", "prompt_cache_key", "participates in request/cache identity indirectly"),
        ],
    )
    add_config(
        "model_provider",
        category="provider",
        runtime="Config.model_provider + SharedModelProvider",
        effects=[
            effect("endpoint", "provider base URL / WireApi", "selects authority, route family, and HTTP versus Responses-compatible behavior"),
            effect("headers", "provider/auth/default headers", "selects provider and authentication header layers"),
            effect("capability", "ProviderCapabilities", "bounds namespace tools, hosted web search, image generation, WebSockets, and remote compaction"),
        ],
    )
    add_config(
        "model_reasoning_effort",
        category="request_body",
        runtime="effective reasoning effort",
        effects=[effect("request_body", "reasoning.effort", "serialized after model-specific normalization")],
    )
    add_config(
        "model_reasoning_summary",
        category="request_body",
        runtime="effective reasoning-summary policy",
        effects=[
            effect("request_body", "reasoning.summary", "serialized only when supported and not None"),
            effect("request_body", "stream_options.reasoning_summary_delivery", "can request sequential cutoff delivery", "ConcurrentReasoningSummaries enabled"),
        ],
    )
    add_config(
        "model_verbosity",
        category="request_body",
        runtime="ModelClientState.model_verbosity",
        effects=[effect("request_body", "text.verbosity", "serialized only for models that support verbosity")],
    )
    add_config(
        "service_tier",
        category="routing",
        runtime="Config.service_tier / per-turn override",
        effects=[
            effect("request_body", "service_tier", "serialized after model support/default filtering"),
            effect("request_header", "x-codex-routing-hint.tier", "projects tier for first-party Codex routing"),
        ],
    )
    add_config(
        "web_search",
        category="tools",
        runtime="Config.web_search_mode / web_search_config",
        effects=[
            effect("tool_schema", "hosted web_search tool", "controls hosted search tool exposure in full Responses"),
            effect("responses_lite", "hosted model tools", "hosted Responses tools are omitted", "model uses Responses Lite"),
        ],
    )
    add_config(
        "responses_api_metadata",
        category="metadata",
        runtime="Config.responses_api_metadata → TurnMetadataState.responses_api_metadata",
        effects=[
            effect("client_metadata", 'client_metadata["x-codex-turn-metadata"].<key>', "validated and serde-flattened into the canonical turn JSON"),
            effect("precedence", "turn metadata extra merge", "overrides duplicate app-server responsesapiClientMetadata keys"),
        ],
        notes=["This does not create literal top-level Responses client_metadata keys."],
    )
    add_config(
        "mcp_servers",
        category="mcp",
        runtime="Constrained<McpServerCatalog>",
        effects=[
            effect("mcp_transport", "stdio / Streamable HTTP", "defines MCP server processes, URLs, auth, headers, and lifecycle"),
            effect("tool_schema", "Responses namespace tools", "controls which MCP tools become model-visible and how they are exposed"),
            effect("turn_metadata", "tool_namespaces_info", "can record effective namespace/function ownership", "Responses Lite plus turn_metadata_includes_tool_info"),
        ],
    )
    for name, runtime, behavior in (
        ("mcp_oauth_credentials_store", "MCP OAuth credential-store policy", "selects persistent credential storage"),
        ("mcp_oauth_callback_port", "global MCP OAuth callback port", "provides a callback fallback overridden by server OAuth config"),
        ("mcp_oauth_callback_url", "global MCP OAuth callback URL", "selects OAuth redirect URL"),
        ("mcp_optional_startup_grace_ms", "MCP optional-server startup grace", "bounds non-required startup waiting"),
        ("apps_mcp_product_sku", "Codex Apps MCP product SKU", "populates X-OpenAI-Product-Sku on the hosted Apps MCP connection"),
    ):
        add_config(
            name,
            category="mcp",
            runtime=runtime,
            effects=[effect("mcp_runtime", name, behavior)],
        )
    add_config(
        "chatgpt_base_url",
        category="endpoint",
        runtime="Config.chatgpt_base_url",
        effects=[
            effect("backend_endpoint", "ChatGPT/Codex backend routes", "selects account, file, and Apps MCP authority"),
            effect("mcp_endpoint", "/ps/mcp", "forms the hosted Codex Apps MCP URL"),
        ],
    )
    add_config(
        "openai_base_url",
        category="endpoint",
        runtime="provider base URL override",
        effects=[effect("provider_endpoint", "Responses/models/provider auxiliary routes", "overrides the OpenAI-compatible API authority")],
    )
    add_config(
        "orchestrator",
        category="tool_orchestration",
        runtime="orchestrator skill/MCP gates",
        effects=[effect("tool_registry", "skills and MCP contributions", "can enable or suppress orchestrator-provided tools")],
    )

    feature_impacts = {
        "ContentItemKinds": [effect("request_input", "internal message metadata.content_item_kinds", "preserves or clears per-content classifications")],
        "ExecutedToolCallMetadata": [effect("request_input", "internal message metadata.executed_tool_calls", "records attempted tool calls when enabled")],
        "EnableRequestCompression": [effect("http_request", "Content-Encoding: zstd", "compresses streaming request bodies on supported Codex-backend paths")],
        "ConcurrentReasoningSummaries": [effect("request_body", "stream_options.reasoning_summary_delivery", "requests sequential-cutoff reasoning summary delivery")],
        "RemoteCompactionV2": [effect("request_input", "compaction_trigger item over /responses", "uses full Responses protocol instead of legacy /responses/compact")],
        "Apps": [effect("mcp_runtime", "Codex Apps MCP server", "enables app/connector-backed MCP contributions")],
        "EnableMcpApps": [effect("mcp_runtime", "MCP Apps behavior", "enables MCP Apps support")],
        "Mcp20260728": [effect("mcp_protocol", "McpProtocolMode::V20260728", "allows MCP 2026-07-28 discovery/lifecycle with legacy fallback")],
        "McpOAuthRefreshCoordination": [effect("mcp_auth", "OAuth refresh mode", "lets RMCP coordinate OAuth refresh with the credential store")],
        "NonPrefixedMcpToolNames": [effect("tool_schema", "MCP callable namespaces", "allows selected MCP servers to omit the legacy mcp__ prefix")],
        "DeferredToolWorldState": [effect("model_context", "deferred namespace world state", "describes deferred tool namespaces to the model")],
        "CodeMode": [effect("tool_schema", "code-mode exec/wait and nested tool exposure", "projects eligible tools through code mode")],
        "CodeModeOnly": [effect("tool_schema", "direct tool visibility", "restricts model-visible tools to code-mode entry points")],
        "ToolSuggest": [effect("tool_registry", "discoverable app suggestions", "exposes tool suggestions when Apps and Plugins are also enabled")],
        "TokenBudget": [effect("model_context", "context-window metadata", "adds current context-window budget information to model-visible context")],
    }
    feature_rows: list[dict[str, Any]] = []
    for feature_id, effects in feature_impacts.items():
        registry_row = feature_by_id.get(feature_id)
        feature_rows.append({
            "feature_id": feature_id,
            "config_key": registry_row.get("key") if registry_row else None,
            "stage": registry_row.get("stage") if registry_row else None,
            "default_enabled": registry_row.get("default_enabled") if registry_row else None,
            "default_enabled_expression": registry_row.get("default_enabled_expression") if registry_row else None,
            "wire_effects": effects,
            "present_in_registry": bool(registry_row),
            "source": registry_row.get("source") if registry_row else None,
        })
        if feature_source and not registry_row:
            warnings.append(f"wire-affecting feature not found in registry: {feature_id}")

    nested_config_schemas: dict[str, Any] = {}
    for struct_name in (
        "ToolRegistryConfig", "CodeModeConfig", "OrchestratorConfig", "WebSearchConfig",
        "RealtimeConfig", "MultiAgentV2Config", "ToolsConfig", "FeaturesToml",
    ):
        fields = (
            try_struct_summary(core_config, EXTRA["core_config"], struct_name)
            or try_struct_summary(config_toml, EXTRA["config_toml"], struct_name)
            or try_struct_summary(feature_configs, EXTRA["feature_configs"], struct_name)
            or try_struct_summary(feature_source, EXTRA["feature_registry"], struct_name)
        )
        if fields:
            nested_config_schemas[struct_name] = fields

    tool_registry = {
        "schema": tool_registry_fields,
        "settings": [
            {
                "setting": "tool_registry.error_on_tool_collisions",
                "effect": "turn creation fails instead of silently accepting colliding model-visible tool names/namespaces",
                "source": _source_for_field(tool_registry_fields, "error_on_tool_collisions"),
            },
            {
                "setting": "tool_registry.turn_metadata_includes_tool_info",
                "effect": "enables nested tool_namespaces_info only when the selected model uses Responses Lite",
                "source": _source_for_field(tool_registry_fields, "turn_metadata_includes_tool_info"),
            },
        ],
    }
    settings_by_category = _index_rows_by_key(settings, "category")
    wire_effect_index: dict[str, list[dict[str, Any]]] = {}
    for setting in settings:
        for wire_effect in setting.get("wire_effects", []):
            layer = str(wire_effect.get("layer") or "uncategorized")
            wire_effect_index.setdefault(layer, []).append({
                "setting": setting.get("setting"),
                "path": wire_effect.get("path"),
                "behavior": wire_effect.get("behavior"),
                "condition": wire_effect.get("condition"),
            })
    wire_effect_index = dict(sorted(wire_effect_index.items()))

    return {
        "schema_version": 1,
        "role": "configuration and feature inputs that causally affect Codex protocol output",
        "config_toml": {
            "struct": "ConfigToml",
            "fields": config_fields,
            "field_names": [field["name"] for field in config_fields],
            "source": anchor(config_toml, EXTRA["config_toml"], "pub struct ConfigToml", "ConfigToml"),
        },
        "effective_config": {
            "struct": "Config",
            "fields": runtime_fields,
            "field_names": [field["name"] for field in runtime_fields],
            "source": anchor(core_config, EXTRA["core_config"], "pub struct Config", "Config"),
        },
        "wire_affecting_settings": settings,
        "settings_by_category": settings_by_category,
        "wire_effect_index": wire_effect_index,
        "feature_registry": feature_registry,
        "wire_affecting_features": feature_rows,
        "tool_registry": tool_registry,
        "nested_config_schemas": nested_config_schemas,
        "resolution_pipeline": [
            "load layered ConfigToml inputs and profile selections",
            "apply centrally managed requirements/constraints",
            "resolve effective Config fields and feature set",
            "apply per-turn or app-server overrides where supported",
            "intersect requested behavior with provider and model capabilities",
            "project the result into endpoint, header, body, tool, and metadata layers",
        ],
        "removed_or_compatibility_features": [
            row for row in feature_registry.get("features", [])
            if row.get("stage") in {"Removed", "Deprecated"}
        ],
        "notes": [
            "The full ConfigToml schema is reported, but wire_affecting_settings is the selected causal subset.",
            "Provider/model capability is an upper bound: configuration can disable supported behavior but should not enable behavior the provider rejects.",
            "Legacy feature keys may still parse even when their registry stage is Removed; stage and default are therefore reported explicitly.",
        ],
        "warnings": list(dict.fromkeys(warnings)),
        "source": {
            "config_toml": anchor(config_toml, EXTRA["config_toml"], "pub struct ConfigToml", "ConfigToml"),
            "effective_config": anchor(core_config, EXTRA["core_config"], "pub struct Config", "Config"),
            "feature_registry": feature_registry.get("source"),
            "responses_request": _source_anchor_any(base_src.get("core"), B.FILES["core"], ["fn build_responses_request"], "ModelClient::build_responses_request"),
        },
    }


def _async_method_inventory(text: str | None, path: str, owner: str) -> list[dict[str, Any]]:
    if not text:
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?m)^\s*pub(?:\([^)]*\))?\s+async\s+fn\s+([a-z_][A-Za-z0-9_]*)\s*\(", text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        if name.startswith("new_"):
            category = "transport_construction"
        elif name.startswith("list_"):
            category = "discovery"
        elif name.startswith("read_"):
            category = "resource_read"
        elif "call_tool" in name:
            category = "tool_invocation"
        elif "elicit" in name:
            category = "elicitation"
        elif "event" in name or "notification" in name:
            category = "events"
        elif name in {"initialize", "connect", "shutdown", "close", "cancel"}:
            category = "lifecycle"
        else:
            category = "operation"
        rows.append({
            "name": name,
            "category": category,
            "source": {"path": path, "line": ln(text, match.start()), "symbol": f"{owner}::{name}"},
        })
    return rows


def _mcp_server_setting_semantics(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    meanings = {
        "transport": "selects stdio or Streamable HTTP and supplies transport-specific fields",
        "auth": "selects OAuth or trusted first-party ChatGPT fallback after configured bearer/header auth",
        "environment_id": "selects the execution environment and namespaces managed OAuth credentials",
        "enabled": "when false, skips server initialization",
        "required": "makes initialization failure fatal for codex exec",
        "supports_parallel_tool_calls": "advertises every server tool as parallel-call capable",
        "omit_tools_from": "removes tools from selected direct/deferred/code-mode model-facing surfaces",
        "startup_timeout_sec": "bounds initialization and initial tool discovery",
        "tool_timeout_sec": "default timeout for calls through this server",
        "default_tools_approval_mode": "default tool approval policy before per-tool overrides",
        "enabled_tools": "allow-list applied before disabled_tools",
        "disabled_tools": "deny-list applied after enabled_tools",
        "scopes": "OAuth scopes requested during MCP login",
        "oauth": "per-server OAuth client/callback configuration",
        "oauth_resource": "RFC 8707 OAuth resource parameter",
        "tools": "per-tool approval and output-token-limit overrides",
        "disabled_reason": "runtime-only requirements result; not serialized",
    }
    return [
        {
            "setting": f"mcp_servers.<server>.{field['wire_name']}",
            "name": field["name"],
            "type": field.get("type"),
            "optional": field.get("optional"),
            "serialized": field.get("serialized", True),
            "meaning": meanings.get(field["name"], "MCP server runtime configuration"),
            "source": field.get("source"),
        }
        for field in fields
    ]


def build_mcp_protocol(extra: dict[str, str], report: dict[str, Any]) -> dict[str, Any]:
    """Describe MCP configuration, external protocol, tool projection, and request metadata."""
    mcp_types = extra.get("mcp_types", "")
    runtime = extra.get("mcp_runtime", "")
    binding = extra.get("mcp_binding", "")
    catalog = extra.get("mcp_catalog", "")
    rmcp_client = extra.get("rmcp_client", "")
    protocol_mode = extra.get("rmcp_protocol_mode", "")
    handler = extra.get("mcp_handler", "")
    spec_plan = extra.get("tool_spec_plan", "")
    tool_responses = extra.get("tool_responses_api", "")
    turn_metadata = extra.get("turn_metadata", "")
    feature_registry = extra.get("feature_registry", "")
    server_fields = try_struct_summary(mcp_types, EXTRA["mcp_types"], "McpServerConfig")
    raw_server_fields = try_struct_summary(mcp_types, EXTRA["mcp_types"], "RawMcpServerConfig")
    tool_fields = try_struct_summary(mcp_types, EXTRA["mcp_types"], "McpServerToolConfig")
    oauth_fields = try_struct_summary(mcp_types, EXTRA["mcp_types"], "McpServerOAuthConfig")
    runtime_fields = try_struct_summary(runtime, EXTRA["mcp_runtime"], "McpConfig")
    transport_schema = try_enum_summary(mcp_types, EXTRA["mcp_types"], "McpServerTransportConfig")
    auth_schema = try_enum_summary(mcp_types, EXTRA["mcp_types"], "McpServerAuth")
    approval_schema = try_enum_summary(mcp_types, EXTRA["mcp_types"], "AppToolApproval")
    mode_schema = try_enum_summary(protocol_mode, EXTRA["rmcp_protocol_mode"], "McpProtocolMode")
    operations = _async_method_inventory(rmcp_client, EXTRA["rmcp_client"], "RmcpClient")
    operation_categories = _index_rows_by_key(operations, "category")
    server_setting_rows = _mcp_server_setting_semantics(server_fields)
    warnings: list[str] = []
    for name, value in (
        ("McpServerConfig", server_fields),
        ("McpServerTransportConfig", transport_schema),
        ("McpConfig", runtime_fields),
    ):
        if mcp_types or runtime:
            if not value:
                warnings.append(f"MCP schema not extracted: {name}")

    if rmcp_client and not operations:
        warnings.append("RmcpClient public async operation inventory was not extracted")
    if turn_metadata and "current_meta_value_for_mcp_request" not in turn_metadata:
        warnings.append("MCP turn-metadata projection anchor changed upstream")
    if spec_plan and "apply_mcp_tool_exposure_policy" not in spec_plan:
        warnings.append("MCP tool exposure policy anchor changed upstream")

    metadata_projection = {
        "wire_path": 'MCP request params._meta["x-codex-turn-metadata"]',
        "wire_type": "JSON object (not the ASCII JSON string used by Responses client_metadata)",
        "base": "TurnMetadataState::mcp_metadata_template",
        "request_kind": None,
        "identity_effect": {
            "has_turn_identity": True,
            "has_request_identity": False,
            "therefore_omitted": [
                "installation_id", "window_id", "window_number", "context_window_id", "request_kind", "compaction"
            ],
        },
        "explicitly_removed": ["agent_name", "parent_turn_id", "root_turn_id", "tool_namespaces_info"],
        "explicitly_added_or_overridden": [
            {"name": "model", "condition": "always", "value": "issuing step model slug"},
            {"name": "codex_version", "condition": "always", "value": "Codex package version"},
            {"name": "reasoning_effort", "condition": "when the issuing step has an effective effort"},
            {"name": "user_input_requested_during_turn", "condition": "after user input was requested during the turn", "value": True},
            {"name": "node_repl_disabled", "condition": "always set from the issuing step"},
        ],
        "extra_behavior": {
            "app_server_responsesapi_client_metadata": "retained after reserved-key filtering, except keys shadowed by config metadata are removed",
            "config_responses_api_metadata": "does not get appended to the MCP projection; its keys only shadow same-named app-server extras",
            "flattening": "remaining app-server extras stay at the top level of the MCP turn-metadata object",
        },
        "source": _source_anchor_any(turn_metadata, EXTRA["turn_metadata"], ["current_meta_value_for_mcp_request"], "TurnMetadataState::current_meta_value_for_mcp_request"),
        "attachment_source": _source_anchor_any(
            extra.get("mcp_tool_call"),
            EXTRA["mcp_tool_call"],
            ["request_meta.insert", "X_CODEX_TURN_METADATA_HEADER"],
            "MCP request _meta attachment",
        ),
        "hook_attachment_source": _source_anchor_any(
            extra.get("hook_runtime"),
            EXTRA["hook_runtime"],
            ["X_CODEX_TURN_METADATA_HEADER"],
            "hook MCP metadata attachment",
        ),
    }

    return {
        "schema_version": 1,
        "role": "Codex as an MCP client plus the projection of MCP tools into model-facing Responses schemas",
        "configuration": {
            "server": {
                "struct": "McpServerConfig",
                "fields": server_fields,
                "settings": server_setting_rows,
                "settings_by_concern": {
                    "lifecycle": [row for row in server_setting_rows if row.get("name") in {"enabled", "required", "startup_timeout_sec", "tool_timeout_sec", "environment_id"}],
                    "transport_and_auth": [row for row in server_setting_rows if row.get("name") in {"transport", "auth", "scopes", "oauth", "oauth_resource"}],
                    "tool_visibility": [row for row in server_setting_rows if row.get("name") in {"enabled_tools", "disabled_tools", "omit_tools_from", "supports_parallel_tool_calls"}],
                    "approval_and_output": [row for row in server_setting_rows if row.get("name") in {"default_tools_approval_mode", "tools"}],
                },
            },
            "raw_toml": {"struct": "RawMcpServerConfig", "fields": raw_server_fields},
            "per_tool": {"struct": "McpServerToolConfig", "fields": tool_fields},
            "oauth": {"struct": "McpServerOAuthConfig", "fields": oauth_fields},
            "auth": auth_schema,
            "approval": approval_schema,
            "environment_variable": try_enum_summary(mcp_types, EXTRA["mcp_types"], "McpServerEnvVar"),
            "disabled_reason": try_enum_summary(mcp_types, EXTRA["mcp_types"], "McpServerDisabledReason"),
            "tool_exposure_surface": try_enum_summary(
                extra.get("protocol_config_types"),
                EXTRA["protocol_config_types"],
                "ToolExposureSurface",
            ),
            "transport": transport_schema,
            "runtime": {"struct": "McpConfig", "fields": runtime_fields},
            "validation": {
                "transport_choice": "command selects stdio; url selects Streamable HTTP; setting fields from the other transport is rejected",
                "stdio_modern_protocol_opt_in": "MCP 2026-07-28 requires both the feature and CODEX_MCP_PROTOCOL_VERSION=2026-07-28 for stdio",
                "http_headers_helper": "local Streamable HTTP only; an empty command is rejected",
                "source": _source_anchor_any(mcp_types, EXTRA["mcp_types"], ["impl TryFrom<RawMcpServerConfig>"], "RawMcpServerConfig validation"),
            },
        },
        "protocol_mode": {
            "schema": mode_schema,
            "legacy": {"preferred_version": "2025-06-18", "lifecycle": "explicit initialize"},
            "v20260728": {"preferred_version": "2026-07-28", "fallback_version": "2025-06-18", "lifecycle": "SDK auto lifecycle"},
            "feature_gate": "features.mcp_2026_07_28",
            "source": _source_anchor_any(protocol_mode, EXTRA["rmcp_protocol_mode"], ["preferred_protocol_version"], "McpProtocolMode"),
        },
        "transports": [
            {
                "name": "in_process",
                "external_wire": False,
                "framing": "in-memory duplex transport using MCP message semantics",
                "source": _source_anchor_any(rmcp_client, EXTRA["rmcp_client"], ["InProcess"], "TransportRecipe::InProcess"),
            },
            {
                "name": "stdio",
                "external_wire": True,
                "addressing": "configured command/args/cwd/environment",
                "framing": "MCP JSON-RPC messages over child-process stdio",
                "config_fields": ["command", "args", "env", "env_vars", "cwd"],
                "source": _source_anchor_any(rmcp_client, EXTRA["rmcp_client"], ["new_stdio_client_with_protocol_mode"], "RmcpClient stdio"),
            },
            {
                "name": "streamable_http",
                "external_wire": True,
                "addressing": "configured URL",
                "framing": "MCP Streamable HTTP managed by the rmcp SDK",
                "auth_precedence": [
                    "configured bearer token/environment and configured authorization headers",
                    "selected ChatGPT or stored OAuth flow",
                    "unauthenticated fallback when allowed",
                ],
                "config_fields": ["url", "bearer_token_env_var", "http_headers", "env_http_headers", "http_headers_helper", "auth", "oauth", "oauth_resource"],
                "source": _source_anchor_any(rmcp_client, EXTRA["rmcp_client"], ["StreamableHttpClientTransport"], "RmcpClient Streamable HTTP"),
            },
        ],
        "operations": operations,
        "operation_names": [row["name"] for row in operations],
        "operations_by_category": operation_categories,
        "operation_counts_by_category": {name: len(rows) for name, rows in operation_categories.items()},
        "tool_projection": {
            "source_tool": "rmcp::model::Tool",
            "codex_runtime_spec": "ToolSpec::Namespace(ResponsesApiNamespace)",
            "namespace": "ToolInfo.callable_namespace",
            "function": "ResponsesApiNamespaceTool::Function(ResponsesApiTool)",
            "schema_fields": {
                "responses_api_tool": try_struct_summary(tool_responses, EXTRA["tool_responses_api"], "ResponsesApiTool"),
                "namespace": try_struct_summary(tool_responses, EXTRA["tool_responses_api"], "ResponsesApiNamespace"),
                "namespace_tool": try_enum_summary(tool_responses, EXTRA["tool_responses_api"], "ResponsesApiNamespaceTool"),
            },
            "naming": {
                "canonical": "namespace + function name",
                "legacy_hook_name": "mcp__<namespace>__<tool>",
                "non_prefixed_feature": "selected servers can use non-prefixed callable namespaces",
            },
            "exposure_pipeline": [
                "server enabled/required and transport initialization",
                "enabled_tools allow-list",
                "disabled_tools deny-list",
                "omit_tools_from surface restrictions",
                "direct versus deferred tool-search decision",
                "code-mode exclusions/direct-only overrides",
                "provider namespace-tools capability",
                "namespace merge, sorting, and collision checks",
            ],
            "parallel_calls": "enabled when server opts in or a tool advertises read_only_hint=true",
            "output_budget": "per-tool output_token_limit overrides the normal model truncation policy",
            "sources": {
                "handler": _source_anchor_any(handler, EXTRA["mcp_handler"], ["fn create_tool_spec"], "McpHandler tool projection"),
                "exposure": _source_anchor_any(spec_plan, EXTRA["tool_spec_plan"], ["fn apply_mcp_tool_exposure_policy"], "MCP tool exposure policy"),
                "finalization": _source_anchor_any(spec_plan, EXTRA["tool_spec_plan"], ["fn finalize_tool_router"], "tool-router finalization"),
            },
        },
        "turn_metadata_projection": metadata_projection,
        "representation_boundary": {
            "responses": {
                "container": "ResponsesApiRequest.client_metadata or response.create.client_metadata",
                "key": "x-codex-turn-metadata",
                "value_type": "ASCII JSON string",
            },
            "mcp": {
                "container": "MCP request params._meta",
                "key": "x-codex-turn-metadata",
                "value_type": "JSON object",
            },
            "same_name_not_same_wire_type": True,
        },
        "hosted_apps_mcp": {
            "server_name": "codex_apps",
            "path": "/ps/mcp",
            "transport": "Streamable HTTP",
            "headers": ["X-OpenAI-Product-Sku", "originator when configured", "ChatGPT authorization/account headers"],
            "config_inputs": ["chatgpt_base_url", "apps_mcp_product_sku", "features.apps", "features.enable_mcp_apps"],
            "source": _source_anchor_any(runtime, EXTRA["mcp_runtime"], ["X-OpenAI-Product-Sku", "/ps/mcp"], "Codex Apps MCP configuration"),
        },
        "catalog_and_binding": {
            "catalog_source": anchor(catalog, EXTRA["mcp_catalog"], "McpServerCatalog", "McpServerCatalog"),
            "binding_source": anchor(binding, EXTRA["mcp_binding"], "McpBinding", "McpBinding"),
            "feature_source": anchor(feature_registry, EXTRA["feature_registry"], "Mcp20260728", "MCP feature gates"),
        },
        "warnings": warnings,
    }


def _match_arm_inventory(text: str | None, path: str, function_anchor: str) -> list[dict[str, Any]]:
    """Extract response/codex string match arms and emitted ResponseEvent variants."""
    if not text:
        return []
    fn_start = text.find(function_anchor)
    if fn_start < 0:
        return []
    fn_open = text.find("{", fn_start)
    if fn_open < 0:
        return []
    try:
        fn_close = B.find_rust_matching_brace(text, fn_open)
    except AuditError:
        return []
    function = text[fn_open + 1:fn_close]
    match_anchor = re.search(r"match\s+event\.kind(?:\.as_str\(\))?\s*\{", function)
    if not match_anchor:
        return []
    match_open = fn_open + 1 + function.find("{", match_anchor.start())
    try:
        match_close = B.find_rust_matching_brace(text, match_open)
    except AuditError:
        return []
    body = text[match_open + 1:match_close]
    # Match arms whose patterns are response.* or codex.* string literals. Rust
    # permits block arms without a trailing comma, so comma splitting is not enough.
    arm_pattern = re.compile(
        r'(?m)^[ \t]*(?P<patterns>"(?:response|codex)\.[^"]+"'
        r'(?:\s*\|\s*"(?:response|codex)\.[^"]+")*)\s*=>'
    )
    matches = list(arm_pattern.finditer(body))
    rows: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        arm_body = body[match.end():end]
        events = re.findall(r'"((?:response|codex)\.[^"]+)"', match.group("patterns"))
        emitted = list(dict.fromkeys(re.findall(r"ResponseEvent::([A-Za-z0-9_]+)", arm_body)))
        api_errors = list(dict.fromkeys(re.findall(r"ApiError::([A-Za-z0-9_]+)", arm_body)))
        if "Err(" in arm_body or any(event in {"response.failed", "response.incomplete"} for event in events):
            disposition = "terminal_error"
        elif emitted:
            disposition = "emitted"
        else:
            disposition = "observed_not_emitted"
        for event in events:
            rows.append({
                "event": event,
                "disposition": disposition,
                "emitted_response_events": emitted,
                "possible_api_errors": api_errors,
                "source": {
                    "path": path,
                    "line": ln(text, match_open + 1 + match.start()),
                    "symbol": f"process_responses_event::{event}",
                },
            })
    return rows


def build_responses_protocol(
    base_src: dict[str, str],
    extra: dict[str, str],
    report: dict[str, Any],
) -> dict[str, Any]:
    """Build the complete logical Responses request/item/event protocol inventory."""
    common = base_src.get("common", "")
    core = base_src.get("core", "")
    sse = base_src.get("response_sse", "")
    ws = base_src.get("ws", "")
    models = extra.get("protocol_models", "")
    tools = extra.get("tool_spec", "")
    tool_api = extra.get("tool_responses_api", "")
    request_structs = {
        name: try_struct_summary(common, B.FILES["common"], name)
        for name in (
            "AccessPrograms", "Reasoning", "StreamOptions", "TextControls", "TextFormat",
            "ResponsesApiRequest", "ResponseCreateWsRequest",
        )
    }
    request_enums = {
        name: try_enum_summary(common, B.FILES["common"], name)
        for name in ("ReasoningContext", "ReasoningSummaryDelivery", "ResponsesWsRequest")
    }
    builder = _struct_literal_assignments(
        core,
        B.FILES["core"],
        "fn build_responses_request",
        "ResponsesApiRequest",
    )
    request_schema_names = [
        str(field.get("name"))
        for field in request_structs.get("ResponsesApiRequest", [])
        if field.get("name")
    ]
    assigned_names = list(builder.get("assignment_map", {}))
    builder_missing_fields = sorted(set(request_schema_names) - set(assigned_names))
    builder_unknown_fields = sorted(set(assigned_names) - set(request_schema_names))
    builder_assignments = {
        row["field"]: row for row in builder.get("assignments", [])
    }
    request_field_semantics = [
        {
            "field": field,
            "builder_expression": (builder.get("assignment_map") or {}).get(field),
            "wire_semantics": semantics,
            "source": builder_assignments.get(field, {}).get("source"),
        }
        for field, semantics in (
            ("model", "selected ModelInfo slug"),
            ("instructions", "full Responses uses the top-level base-instructions string; Lite empties it and moves instructions into input"),
            ("input", "ordered ResponseItem array, including any Lite-only prefix items"),
            ("tools", "full Responses top-level serialized tools; Lite uses null after moving definitions into an AdditionalTools input item"),
            ("tool_choice", "automatic tool selection"),
            ("parallel_tool_calls", "prompt/runtime value in full Responses and false in Lite"),
            ("reasoning", "model-normalized effort/summary/context controls"),
            ("store", "false for the Codex request builder"),
            ("stream", "true; HTTP returns SSE and WebSocket returns event frames"),
            ("stream_options", "optional reasoning-summary delivery controls"),
            ("include", "requests reasoning.encrypted_content"),
            ("service_tier", "model-filtered tier such as priority/flex when supported"),
            ("prompt_cache_key", "turn/request-derived cache identity"),
            ("text", "verbosity and optional structured-output schema controls"),
            ("client_metadata", "string map produced by CodexResponsesMetadata::client_metadata"),
            ("access_programs", "starts empty and can be authorized/populated later in the request path"),
        )
    ]
    builder_checks = {
        "literal_extracted": bool(assigned_names),
        "all_schema_fields_assigned": not builder_missing_fields,
        "no_unknown_assignments": not builder_unknown_fields,
        "store_false": (builder.get("assignment_map") or {}).get("store") == "false",
        "stream_true": (builder.get("assignment_map") or {}).get("stream") == "true",
        "client_metadata_uses_responses_metadata": "responses_metadata.client_metadata" in ((builder.get("assignment_map") or {}).get("client_metadata") or ""),
        "parallel_calls_are_lite_gated": "use_responses_lite" in ((builder.get("assignment_map") or {}).get("parallel_tool_calls") or ""),
    }
    item_structs = {
        name: try_struct_summary(models, EXTRA["protocol_models"], name)
        for name in ("InternalChatMessageMetadataPassthrough",)
    }
    item_enums = {
        name: try_enum_summary(models, EXTRA["protocol_models"], name)
        for name in ("ResponseInputItem", "ContentItem", "ResponseItem")
    }
    tool_protocol = {
        "tool_spec": try_enum_summary(tools, EXTRA["tool_spec"], "ToolSpec"),
        "responses_api_tool": try_struct_summary(tool_api, EXTRA["tool_responses_api"], "ResponsesApiTool"),
        "freeform_tool": try_struct_summary(tool_api, EXTRA["tool_responses_api"], "FreeformTool"),
        "freeform_tool_format": try_struct_summary(tool_api, EXTRA["tool_responses_api"], "FreeformToolFormat"),
        "namespace": try_struct_summary(tool_api, EXTRA["tool_responses_api"], "ResponsesApiNamespace"),
        "namespace_tool": try_enum_summary(tool_api, EXTRA["tool_responses_api"], "ResponsesApiNamespaceTool"),
        "loadable_tool_spec": try_enum_summary(tool_api, EXTRA["tool_responses_api"], "LoadableToolSpec"),
    }
    raw_event_fields = try_struct_summary(sse, B.FILES["response_sse"], "ResponsesStreamEvent")
    dispatch = _match_arm_inventory(sse, B.FILES["response_sse"], "pub fn process_responses_event")
    all_event_kinds = B.discover_response_event_kinds(sse)
    dispatch_by_event = {row["event"]: row for row in dispatch}
    # Source-wide discovery also sees fixture/test payloads. Treat those as
    # additional literals, not protocol drift; drift is based on the actual
    # process_responses_event match inventory.
    source_wide_only = [event for event in all_event_kinds if event not in dispatch_by_event]
    required_dispatch_events = {"response.created", "response.completed", "response.failed"}
    missing_required = sorted(required_dispatch_events - set(dispatch_by_event)) if sse else []
    response_event = try_enum_summary(common, B.FILES["common"], "ResponseEvent")
    completion = {
        name: try_struct_summary(sse, B.FILES["response_sse"], name)
        for name in (
            "ResponseCompleted", "ResponseCompletedUsage", "ResponseCompletedInputTokensDetails",
            "ResponseCompletedOutputTokensDetails", "Error",
        )
    }
    completion["ResponseUsageMetadata"] = try_struct_summary(
        extra.get("response_usage", ""),
        EXTRA["response_usage"],
        "ResponseUsageMetadata",
    )
    response_support_structs = {
        name: try_struct_summary(common, B.FILES["common"], name)
        for name in ("ResponseStream", "SafetyBuffering", "SafetyBufferingTreatment")
    }
    transport = report.get("wire_surface_catalog", {}).get("model_facing_transport_matrix", {})
    return {
        "schema_version": 1,
        "role": "logical Responses protocol shared by HTTP/SSE and Responses WebSocket framing",
        "request": {
            "http": {
                "envelope": "ResponsesApiRequest JSON object",
                "schema": request_structs["ResponsesApiRequest"],
                "transport_ref": "#/responses_http",
            },
            "websocket": {
                "envelope": "ResponsesWsRequest::ResponseCreate / type=response.create",
                "schema": request_structs["ResponseCreateWsRequest"],
                "transport_ref": "#/responses_websocket",
                "websocket_only_fields": ["previous_response_id", "generate"],
            },
            "nested_structs": request_structs,
            "nested_enums": request_enums,
            "builder": builder,
            "builder_assignments": builder.get("assignments", []),
            "field_semantics": request_field_semantics,
            "builder_checks": builder_checks,
            "builder_missing_schema_fields": builder_missing_fields,
            "builder_unknown_fields": builder_unknown_fields,
            "normal_builder_invariants": [
                {"field": "tool_choice", "value": "auto"},
                {"field": "store", "value": False},
                {"field": "stream", "value": True},
                {"field": "include", "value": ["reasoning.encrypted_content"]},
                {"field": "prompt_cache_key", "value": "derived from request/turn metadata"},
            ],
            "source": _source_anchor_any(core, B.FILES["core"], ["fn build_responses_request"], "ModelClient::build_responses_request"),
        },
        "input_and_output_items": {
            "structs": item_structs,
            "enums": item_enums,
            "response_item_wire_variants": [
                variant.get("wire_name")
                for variant in (item_enums.get("ResponseItem") or {}).get("variants", [])
            ],
            "content_item_wire_variants": [
                variant.get("wire_name")
                for variant in (item_enums.get("ContentItem") or {}).get("variants", [])
            ],
        },
        "tools": tool_protocol,
        "response_stream": {
            "raw_event_envelope": {"struct": "ResponsesStreamEvent", "fields": raw_event_fields},
            "event_kinds_discovered": all_event_kinds,
            "dispatch": dispatch,
            "dispatch_by_event": dispatch_by_event,
            "unclassified_discovered_event_kinds": missing_required,
            "source_wide_additional_event_literals": source_wide_only,
            "client_response_event": response_event,
            "response_support_structs": response_support_structs,
            "completion_and_error_schemas": completion,
            "event_transport_encodings": {
                "http_sse": "SSE record whose data payload is JSON",
                "websocket": "JSON WebSocket text frame",
            },
            "terminal_identity": "response.completed.response.id",
            "usage": {
                "location": "response.completed.response.usage",
                "fields": completion["ResponseCompletedUsage"],
                "usage_metadata": "optional response.completed.response.usage_metadata",
            },
            "source": anchor(sse, B.FILES["response_sse"], "pub struct ResponsesStreamEvent", "ResponsesStreamEvent"),
        },
        "response_metadata": {
            "http_response_headers": report.get("responses_http", {}).get("response_headers", []),
            "http_response_header_templates": report.get("responses_http", {}).get("response_header_templates", []),
            "application_event_metadata": report.get("responses_http", {}).get("response_event_metadata", []),
            "websocket_handshake_response_headers": report.get("responses_websocket", {}).get("handshake_response_headers", []),
            "websocket_response_event_metadata": report.get("responses_websocket", {}).get("response_event_metadata", []),
            "distinction": "HTTP headers, handshake headers, and response-event embedded metadata are separate wire layers",
        },
        "continuation": {
            "turn_state": report.get("server_owned_continuation", [])[0] if report.get("server_owned_continuation") else None,
            "response_id": report.get("server_owned_continuation", [None, None])[1] if len(report.get("server_owned_continuation", [])) > 1 else None,
            "websocket_request_fields": report.get("responses_websocket", {}).get("request_body_continuation", []),
        },
        "transport_matrix": transport,
        "notes": [
            "HTTP/SSE and WSS differ in transport framing, not in the logical response.* event family.",
            "Embedded event headers and metadata are application-event fields; they are not additional HTTP headers after a WebSocket upgrade.",
            "ResponseItem includes Codex-specific history/control variants in addition to ordinary message and tool-call items.",
        ],
        "warnings": list(dict.fromkeys(
            (
                ["required Responses dispatch events were not extracted: " + ", ".join(missing_required)]
                if missing_required else []
            )
            + (builder.get("warnings") or [])
            + (["ResponsesApiRequest builder did not assign schema fields: " + ", ".join(builder_missing_fields)] if builder_missing_fields and core else [])
            + (["ResponsesApiRequest builder assigned unknown fields: " + ", ".join(builder_unknown_fields)] if builder_unknown_fields and core else [])
        )),
    }


def build_responses_lite_protocol(
    base_src: dict[str, str],
    extra: dict[str, str],
    report: dict[str, Any],
) -> dict[str, Any]:
    """Describe Responses Lite as a request rewrite, not merely a header marker."""
    core = base_src.get("core", "")
    tools = extra.get("tool_spec", "")
    spec_plan = extra.get("tool_spec_plan", "")
    provider = extra.get("provider_runtime", "")
    model_protocol = extra.get("model_protocol", "")
    config_protocol = report.get("config_protocol", {})
    feature_registry = config_protocol.get("feature_registry", {})
    websocket_features = [
        row for row in feature_registry.get("features", [])
        if row.get("id") in {"ResponsesWebsockets", "ResponsesWebsocketsV2"}
    ]
    response_item_schema = (
        report.get("responses_protocol", {})
        .get("input_and_output_items", {})
        .get("enums", {})
        .get("ResponseItem")
    ) or {}
    additional_tools_variant = next(
        (variant for variant in response_item_schema.get("variants", []) if variant.get("name") == "AdditionalTools"),
        None,
    )
    full_request_fields = (
        report.get("responses_protocol", {})
        .get("request", {})
        .get("http", {})
        .get("schema", [])
    )
    lite_field_behavior = {
        "model": "same selected model slug; the selected ModelInfo is what activates Lite",
        "instructions": "forced to an empty string; base instructions move into an input message",
        "input": "Lite-aware formatted input prefixed by AdditionalTools and optional base-instructions developer items",
        "tools": "null; tool definitions move into the AdditionalTools input item",
        "tool_choice": "auto",
        "parallel_tool_calls": "false",
        "reasoning": "same effort/summary normalization, with context forced to all_turns",
        "store": "false",
        "stream": "true",
        "stream_options": "same optional reasoning-summary delivery behavior",
        "include": "reasoning.encrypted_content",
        "service_tier": "same model-filtered service-tier behavior",
        "prompt_cache_key": "same turn/request-derived cache identity",
        "text": "same verbosity/structured-output controls when supported",
        "client_metadata": "same base CodexResponsesMetadata map; only the WebSocket transport adds a Lite marker key",
        "access_programs": "same authorization field behavior",
    }
    field_matrix = [
        {
            "field": field.get("name"),
            "wire_name": field.get("wire_name"),
            "type": field.get("type"),
            "full_responses": next(
                (row.get("wire_semantics") for row in report.get("responses_protocol", {}).get("request", {}).get("field_semantics", []) if row.get("field") == field.get("name")),
                "same ResponsesApiRequest field",
            ),
            "responses_lite": lite_field_behavior.get(str(field.get("name")), "same field unless modified by Lite-aware input formatting"),
        }
        for field in full_request_fields
    ]
    source_checks = {
        "model_activation": "use_responses_lite" in core,
        "request_rewrite": "if model_info.use_responses_lite" in core,
        "lite_tool_serializer": "create_tools_json_for_responses_lite" in tools,
        "hosted_tool_suppression": "Responses Lite accepts schemas for client-executed tools" in spec_plan,
        "tool_namespace_metadata_gate": "turn_metadata_includes_tool_info" in spec_plan,
    }
    warnings = [
        f"Responses Lite source check failed: {name}"
        for name, present in source_checks.items()
        if not present and any((core, tools, spec_plan))
    ]
    return {
        "schema_version": 1,
        "activation": {
            "source": "ModelInfo.use_responses_lite",
            "configurable_directly": False,
            "provider_namespace_capability": "ProviderCapabilities.namespace_tools",
            "model_source": _source_anchor_any(model_protocol, EXTRA["model_protocol"], ["use_responses_lite"], "ModelInfo.use_responses_lite"),
            "provider_source": _source_anchor_any(provider, EXTRA["provider_runtime"], ["pub namespace_tools: bool"], "ProviderCapabilities.namespace_tools"),
        },
        "request_field_matrix": field_matrix,
        "request_rewrite": [
            {"concept": "formatted input", "full_responses": "normal formatted ResponseItem input", "responses_lite": "prompt.get_formatted_input_for_request(true)"},
            {"concept": "base instructions", "full_responses": "top-level instructions string", "responses_lite": "developer contextual message inserted at the start of input with a stable thread-derived ID"},
            {"concept": "tool definitions", "full_responses": "top-level tools array", "responses_lite": "developer AdditionalTools input item with a stable thread-derived ID"},
            {"concept": "top-level instructions", "full_responses": "configured base instructions", "responses_lite": "empty string"},
            {"concept": "top-level tools", "full_responses": "serialized tool array", "responses_lite": None},
            {"concept": "parallel_tool_calls", "full_responses": "prompt/model value", "responses_lite": False},
            {"concept": "reasoning.context", "full_responses": "omitted so the service default applies", "responses_lite": "all_turns"},
            {"concept": "hosted Responses tools", "full_responses": "may include hosted web_search", "responses_lite": "omitted; Lite accepts client-executed tool schemas"},
            {"concept": "request struct", "full_responses": "ResponsesApiRequest", "responses_lite": "same ResponsesApiRequest struct after rewrite"},
        ],
        "additional_tools_input_item": {
            "variant": additional_tools_variant,
            "wire_role": "developer",
            "stable_id": "UUIDv5 derived from the thread id and the additional-tools purpose",
            "position": "inserted at the start of input before the base-instructions contextual item",
        },
        "tool_rewrite": {
            "input": "Vec<ToolSpec>",
            "normal": "each ToolSpec serializes as one top-level tool entry",
            "lite": [
                "ordinary function tools are accumulated under the default `functions` namespace",
                "ordinary custom/freeform tools are accumulated under the same default namespace",
                "an existing default namespace is merged into that accumulator",
                "non-default namespaces remain separate namespace entries",
                "tool_search and web_search entries remain separate when present",
            ],
            "provider_without_namespace_tools": "the normal tool serializer is used, but its result still rides the AdditionalTools input item and top-level tools remains omitted",
            "source": _source_anchor_any(tools, EXTRA["tool_spec"], ["create_tools_json_for_responses_lite"], "Responses Lite tool serialization"),
        },
        "metadata_invariance": {
            "top_level_client_metadata": "same normal CodexResponsesMetadata key set on HTTP; WebSocket adds only the transport-specific Lite marker alongside normal WS additions",
            "nested_turn_metadata": "same canonical nested turn schema; optional tool_namespaces_info is especially relevant to Lite",
            "flattened_extra": "same normal Responses merge, validation, reserved-key filtering, and config-over-app-server precedence",
            "response_protocol": "same logical response.* events and response parser as full Responses",
        },
        "transport_markers": {
            "http_and_legacy_compact": "x-openai-internal-codex-responses-lite: true request header",
            "responses_websocket": 'response.create.client_metadata["ws_request_header_x_openai_internal_codex_responses_lite"] = "true"',
            "guardian_classifier": "dedicated WSS path also sets the literal handshake marker and builds independent Lite client_metadata",
        },
        "tool_namespace_metadata": {
            "included_when": "tool_registry.turn_metadata_includes_tool_info && ModelInfo.use_responses_lite",
            "wire_path": 'client_metadata["x-codex-turn-metadata"].tool_namespaces_info',
            "contents": "effective namespace/function name, direct/deferred/code-mode exposure, and Harness versus MCP server ownership",
            "compatibility_header": "omitted from x-codex-turn-metadata compatibility headers to keep them bounded",
            "source": _source_anchor_any(spec_plan, EXTRA["tool_spec_plan"], ["turn_metadata_includes_tool_info"], "tool_namespaces_info inclusion gate"),
        },
        "websocket_configuration": {
            "current_gate": "provider supports_websockets plus session-scoped fallback state",
            "legacy_feature_flags": websocket_features,
            "legacy_feature_note": "responses_websockets and responses_websockets_v2 registry entries are compatibility/removed controls when present",
        },
        "source": {
            "request_rewrite": _source_anchor_any(core, B.FILES["core"], ["if model_info.use_responses_lite"], "Responses Lite request rewrite"),
            "http_marker": _source_anchor_any(core, B.FILES["core"], ["fn add_responses_lite_header"], "Responses Lite HTTP marker"),
            "ws_marker": _source_anchor_any(core, B.FILES["core"], ["ws_request_header_x_openai_internal_codex_responses_lite", "build_ws_client_metadata"], "Responses Lite WS marker"),
            "hosted_tool_suppression": _source_anchor_any(spec_plan, EXTRA["tool_spec_plan"], ["Responses Lite accepts schemas for client-executed tools"], "hosted tool suppression"),
        },
        "source_checks": source_checks,
        "warnings": warnings,
        "response_protocol_ref": "#/responses_protocol/response_stream",
        "metadata_protocol_ref": "#/metadata_protocol",
        "notes": [
            "The Lite marker identifies the mode, but the wire-semantic difference is the instruction/tool rewrite into input items.",
            "The canonical nested turn metadata and ordinary flat client_metadata still use the normal CodexResponsesMetadata builder.",
            "Lite changes request construction; it does not define a second response-event language.",
        ],
    }


def build_metadata_protocol(
    report: dict[str, Any],
    base_src: dict[str, str],
    extra: dict[str, str],
    mcp_protocol: dict[str, Any],
) -> dict[str, Any]:
    """Build a layered client_metadata, nested turn metadata, and extra lineage view."""
    schema = report.get("turn_metadata_schema", {})
    construction = report.get("turn_metadata_construction", {})
    base_entries = report.get("responses_http", {}).get("client_metadata", [])
    ws_additions = report.get("responses_websocket", {}).get("client_metadata_ws_additions", [])
    turn_metadata = extra.get("turn_metadata", "")
    responses_metadata = base_src.get("metadata", "")
    input_protocol = report.get("protocol_metadata_inputs", {})
    catalog_client_profiles = (
        report.get("wire_surface_catalog", {})
        .get("shared_profiles", {})
        .get("client_metadata_profiles", {})
    )
    fixed_fields = schema.get("fixed_fields", [])
    construction_by_name = {
        row.get("name"): row for row in construction.get("fields", [])
    }
    nested_fields = []
    for field in fixed_fields:
        item = dict(field)
        rule = construction_by_name.get(field.get("name"), {})
        item["construction_rule"] = rule.get("construction_rule")
        item["construction_expression"] = rule.get("expression")
        nested_fields.append(item)

    top_level = []
    for item in base_entries:
        name = item.get("name")
        nested_name = {
            "x-codex-installation-id": "installation_id",
            "session_id": "session_id",
            "thread_id": "thread_id",
            "x-codex-window-id": "window_id",
            "turn_id": "turn_id",
            "x-openai-subagent": "subagent_kind (related, not byte-identical)",
            "x-codex-parent-thread-id": "parent_thread_id",
            "parent_turn_id": "parent_turn_id",
            "root_turn_id": "root_turn_id",
            "x-codex-turn-metadata": "entire nested object encoded as a string",
        }.get(name)
        row = dict(item)
        row.update({
            "wire_container": "ResponsesApiRequest.client_metadata / response.create.client_metadata",
            "wire_value_type": "string",
            "nested_counterpart": nested_name,
            "canonical": name == "x-codex-turn-metadata",
        })
        top_level.append(row)

    extra_contract_data = extra_contract(report)
    extra_inputs = input_protocol.get("inputs", [])
    source_checks = {
        "responses_client_metadata_builder": "fn client_metadata" in responses_metadata,
        "nested_turn_metadata_builder": "fn turn_metadata_payload" in responses_metadata,
        "compatibility_header_builder": "fn compatibility_headers" in responses_metadata,
        "normal_extra_merge": "responses_metadata_template" in turn_metadata,
        "mcp_extra_projection": "mcp_metadata_template" in turn_metadata,
    }
    warnings = [
        f"metadata source check failed: {name}"
        for name, present in source_checks.items()
        if not present and any((responses_metadata, turn_metadata))
    ]
    return {
        "schema_version": 1,
        "canonical_statement": (
            'client_metadata["x-codex-turn-metadata"] is the canonical full turn snapshot; '
            "flat client_metadata and direct headers are compatibility projections"
        ),
        "decode_recipe": [
            "decode the outer HTTP request JSON or WebSocket response.create JSON object",
            "read the client_metadata object; every value in this outer map is a string",
            "read client_metadata[\"x-codex-turn-metadata\"] when present",
            "parse that ASCII JSON string into the nested turn-metadata object",
            "interpret known fixed keys using nested_turn_metadata.fixed_fields",
            "treat other permitted root string keys as flattened extra metadata; there is no nested extra wrapper",
        ],
        "representation_matrix": {
            "responses_http_full": {
                "outer_container": "ResponsesApiRequest.client_metadata",
                "turn_metadata_representation": "string containing JSON",
                "lite_marker": None,
            },
            "responses_http_lite": {
                "outer_container": "ResponsesApiRequest.client_metadata",
                "turn_metadata_representation": "string containing JSON",
                "lite_marker": "HTTP header x-openai-internal-codex-responses-lite",
            },
            "responses_websocket_full": {
                "outer_container": "response.create.client_metadata",
                "turn_metadata_representation": "string containing JSON",
                "lite_marker": None,
            },
            "responses_websocket_lite": {
                "outer_container": "response.create.client_metadata",
                "turn_metadata_representation": "string containing JSON",
                "lite_marker": "ws_request_header_x_openai_internal_codex_responses_lite string key",
            },
            "mcp_request": {
                "outer_container": "request params._meta",
                "turn_metadata_representation": "JSON object",
                "same_key_name": "x-codex-turn-metadata",
            },
        },
        "top_level_client_metadata": {
            "wire_type": "Option<HashMap<String, String>>",
            "value_type": "every top-level value is a string",
            "base_and_conditional_keys": top_level,
            "websocket_response_create_additions": ws_additions,
            "route_boundary_additions": [
                {"name": "guardian_ticket_requested", "route": "/responses", "condition": "eligible request asks for an opaque Guardian receipt"},
                {"name": "guardian_ticket", "route": "/guardian or /guardian-classifier", "condition": "an opaque server receipt is replayed at the transport boundary"},
            ],
            "profiles": {
                "responses_http_full": {
                    "keys": [row.get("name") for row in top_level],
                    "lite_marker_location": None,
                    "cadence": "one client_metadata map per HTTP request body",
                },
                "responses_http_lite": {
                    "keys": [row.get("name") for row in top_level],
                    "lite_marker_location": "HTTP request header, not client_metadata",
                    "cadence": "one client_metadata map per HTTP request body",
                },
                "responses_websocket_full": {
                    "keys": [row.get("name") for row in top_level + ws_additions if row.get("name") != "ws_request_header_x_openai_internal_codex_responses_lite"],
                    "cadence": "one client_metadata map per response.create frame",
                },
                "responses_websocket_lite": {
                    "keys": [row.get("name") for row in top_level + ws_additions],
                    "lite_marker_location": "ws_request_header_x_openai_internal_codex_responses_lite",
                    "cadence": "one client_metadata map per response.create frame",
                },
                "guardian_classifier_lite_websocket": catalog_client_profiles.get("guardian_classifier_response_create"),
            },
            "nested_encoding": {
                "key": "x-codex-turn-metadata",
                "value": "ASCII JSON string containing an object",
                "double_layer_note": "the outer client_metadata value is a string; consumers must parse that string as JSON to reach nested fields",
            },
        },
        "nested_turn_metadata": {
            "wire_path": schema.get("canonical_wire_path"),
            "wire_type": schema.get("canonical_wire_type"),
            "fixed_fields": nested_fields,
            "fixed_field_names": [field.get("wire_name") or field.get("name") for field in nested_fields],
            "unknown_root_key_rule": "a permitted unknown root string key is flattened extra metadata, not a nested child object",
            "reserved_key_protection": "caller-provided extra keys matching Core-owned fixed or compatibility names are filtered before serialization",
            "field_groups": {
                "request_identity": ["installation_id", "window_id", "window_number", "context_window_id", "request_kind"],
                "turn_identity": ["session_id", "thread_id", "agent_name", "turn_id"],
                "lineage": ["forked_from_thread_id", "forked_from_ordinal_exclusive", "parent_thread_id", "parent_turn_id", "root_turn_id", "thread_source", "turn_trigger", "subagent_kind"],
                "execution_context": ["sandbox", "sandbox_mode", "auto_review_enabled", "node_repl_auto_review_required", "node_repl_disabled", "workspaces"],
                "tool_inventory": ["tool_namespaces_info"],
                "timing_and_history": ["turn_started_at_unix_ms", "history_ingest_requested"],
                "compaction": ["compaction"],
                "flattened_custom": ["<extra-key>"],
            },
            "request_kind_values": schema.get("request_kind_values", []),
            "nested_types": schema.get("nested", {}),
            "identity_gates": construction.get("identity_gates", {}),
            "workspaces_rule": construction.get("workspaces_rule"),
            "compatibility_header_difference": schema.get("compatibility_header_difference"),
            "source": anchor(responses_metadata, B.FILES["metadata"], "struct CodexTurnMetadataPayload", "CodexTurnMetadataPayload"),
        },
        "flattened_extra": {
            "wire_path": extra_contract_data["wire_location"],
            "wire_wrapper": None,
            "wire_value_type": "string for each accepted extra entry",
            "decode_rule": "after parsing the nested turn JSON, keys not claimed by fixed fields can be extra metadata when they satisfy the source contract",
            "schema_field": schema.get("flattened_extra"),
            "accepted_inputs": extra_inputs,
            "normal_responses_merge": {
                "step_1": "filter reserved Core-owned keys from app-server responsesapiClientMetadata",
                "step_2": "filter reserved Core-owned keys from config responses_api_metadata",
                "step_3": "remove app-server keys that collide with config keys",
                "step_4": "extend with config values, so config wins",
                "step_5": "serde(flatten) the merged map into the root of the nested turn object",
            },
            "validation": extra_contract_data["config_validation"],
            "wire_shape_example": {
                "session_id": "<core-owned fixed field>",
                "example.custom-key": "<caller-supplied string value at the same object level>",
            },
            "app_server_validation_difference": (
                "app-server values are reserved-key filtered at the setter; the config size/count/key validation contract is not reapplied there"
            ),
            "mcp_difference": mcp_protocol.get("turn_metadata_projection", {}).get("extra_behavior"),
            "collision_precedence": "config.responses_api_metadata wins for normal Responses; in the MCP projection its keys shadow app-server extras but the config values themselves are not appended",
            "provenance_matrix": [
                {
                    "input": "config.toml responses_api_metadata",
                    "normal_responses": "validated, reserved-key filtered, appended, and wins collisions",
                    "mcp_request": "values omitted; key names shadow same-named app-server extras",
                },
                {
                    "input": "turn/start responsesapiClientMetadata",
                    "normal_responses": "reserved-key filtered, then overridden by duplicate config keys",
                    "mcp_request": "reserved-key filtered and retained unless shadowed by a config key",
                },
                {
                    "input": "Core-owned runtime metadata",
                    "normal_responses": "serialized as fixed nested fields",
                    "mcp_request": "projected with request-identity fields and selected lineage/tool fields removed or replaced",
                },
            ],
            "source": _source_anchor_any(turn_metadata, EXTRA["turn_metadata"], ["fn mcp_metadata_template", "fn responses_metadata_template"], "TurnMetadataState extra merge"),
        },
        "compatibility_projections": {
            "direct_headers": [
                "x-codex-window-id",
                "x-codex-turn-metadata",
                "x-codex-parent-thread-id",
                "x-openai-subagent",
            ],
            "turn_metadata_header": {
                "wire_type": "ASCII JSON header value",
                "difference": "tool_namespaces_info is forced to None; flattened extra metadata remains part of the projection",
            },
            "http_identity_headers": ["session-id", "thread-id", "x-client-request-id"],
            "websocket_cadence": "compatibility headers are handshake-scoped; canonical client_metadata is repeated per response.create",
        },
        "mcp_projection": mcp_protocol.get("turn_metadata_projection"),
        "source_checks": source_checks,
        "warnings": warnings,
        "sources": {
            "builder": anchor(responses_metadata, B.FILES["metadata"], "fn client_metadata", "CodexResponsesMetadata::client_metadata"),
            "nested_builder": anchor(responses_metadata, B.FILES["metadata"], "fn turn_metadata_payload", "CodexResponsesMetadata::turn_metadata_payload"),
            "compatibility_headers": anchor(responses_metadata, B.FILES["metadata"], "fn compatibility_headers", "CodexResponsesMetadata::compatibility_headers"),
            "merge": _source_anchor_any(turn_metadata, EXTRA["turn_metadata"], ["fn responses_metadata_template"], "TurnMetadataState::responses_metadata_template"),
        },
        "notes": [
            "There is no wire-level `extra` object: extra keys are siblings of fixed nested fields.",
            "Reserved-key filtering prevents callers from overwriting Core-owned fields even though extra uses serde(flatten).",
            "MCP reuses a related turn snapshot but sends it as a JSON object in request _meta, with deliberate field removals/additions.",
        ],
    }


def append_mcp_surfaces(report: dict[str, Any], mcp: dict[str, Any]) -> None:
    catalog = report.get("wire_surface_catalog")
    if not isinstance(catalog, dict):
        return
    catalog.setdefault("planes", {})["mcp_external"] = (
        "Codex ↔ MCP server protocol; distinct from model-facing Responses and app-server RPC"
    )
    surfaces = catalog.setdefault("surfaces", [])
    categories = catalog.setdefault("categories", {})

    def add(surface: dict[str, Any]) -> None:
        if not any(existing.get("id") == surface.get("id") for existing in surfaces):
            surfaces.append(surface)
        ids = categories.setdefault(surface["category"], [])
        if surface["id"] not in ids:
            ids.append(surface["id"])

    common_meta = {
        "operations_ref": "#/mcp_protocol/operations",
        "configuration_ref": "#/mcp_protocol/configuration",
        "turn_metadata_projection_ref": "#/mcp_protocol/turn_metadata_projection",
        "model_tool_projection_ref": "#/mcp_protocol/tool_projection",
    }
    add(B._surface(
        surface_id="mcp_stdio",
        category="mcp.transport",
        endpoints=["configured child-process command"],
        method="bidirectional MCP JSON-RPC messages",
        transport="stdio",
        protocol="Model Context Protocol",
        streaming=True,
        request_content_type=None,
        request_accept=None,
        request_framing="MCP messages over child-process stdin/stdout",
        response_media_type=None,
        response_framing="MCP responses, notifications, and server requests over stdout/stdin",
        body_schema="MCP request/notification types",
        variants=[{"protocol_mode": "2025-06-18 legacy or 2026-07-28 when both sides opt in"}],
        notes=["stderr is process diagnostics, not the MCP message stream."],
        source=(mcp.get("transports") or [{}])[1].get("source") if len(mcp.get("transports") or []) > 1 else None,
        plane="mcp_external",
        service_family="mcp_server",
        address_kind="process_command",
        header_profile="none_stdio",
        application_metadata=common_meta,
        trust_domain="configured local or executor-hosted MCP process",
        connection_lifetime="session/server-lifetime",
    ))
    add(B._surface(
        surface_id="mcp_streamable_http",
        category="mcp.transport",
        endpoints=["configured MCP server URL"],
        method="SDK-managed Streamable HTTP requests and streams",
        transport="HTTPS/HTTP",
        protocol="Model Context Protocol Streamable HTTP",
        streaming=True,
        request_content_type="application/json / SDK-managed MCP media negotiation",
        request_accept="SDK-managed MCP response/stream negotiation",
        request_framing="MCP JSON-RPC requests with optional configured/static/environment/dynamic headers",
        response_media_type="MCP JSON or event stream as negotiated by rmcp",
        response_framing="Streamable HTTP responses and notifications",
        body_schema="MCP request/notification types",
        variants=[{"auth": "configured bearer/headers, ChatGPT, OAuth, or unauthenticated fallback"}],
        notes=["Exact HTTP retry and session lifecycle is delegated to the rmcp StreamableHttpClientTransport."],
        source=(mcp.get("transports") or [{}, {}, {}])[2].get("source") if len(mcp.get("transports") or []) > 2 else None,
        plane="mcp_external",
        service_family="mcp_server",
        header_profile="mcp_streamable_http_configured",
        application_metadata=common_meta,
        transport_generated_headers=["Host", "Content-Length or Transfer-Encoding", "Accept-Encoding"],
        trust_domain="configured MCP HTTP origin",
        connection_lifetime="session/server-lifetime with recovery",
    ))
    apps = mcp.get("hosted_apps_mcp", {})
    add(B._surface(
        surface_id="mcp_codex_apps",
        category="mcp.apps",
        endpoints=[apps.get("path", "/ps/mcp")],
        method="SDK-managed Streamable HTTP",
        transport="HTTPS",
        protocol="MCP for Codex Apps/connectors",
        streaming=True,
        request_content_type="SDK-managed MCP request media",
        request_accept="SDK-managed MCP response/stream negotiation",
        request_framing="MCP messages with ChatGPT auth and product/origin headers",
        response_media_type="MCP JSON or event stream",
        response_framing="Streamable HTTP MCP",
        body_schema="MCP request/notification types",
        request_headers=[
            B.catalog_header("X-OpenAI-Product-Sku", condition="configured Apps MCP SKU", category="product", optional=True, source=apps.get("source")),
            B.catalog_header("originator", condition="configured host originator", category="client", optional=True, source=apps.get("source")),
            B.catalog_header("Authorization", condition="ChatGPT auth available", category="auth", optional=True, source=apps.get("source")),
            B.catalog_header("ChatGPT-Account-ID", condition="account/workspace scope available", category="auth", optional=True, source=apps.get("source")),
        ],
        variants=[],
        notes=["This is an MCP server surface, not a Responses endpoint and not the Codex app-server RPC protocol."],
        source=apps.get("source"),
        plane="mcp_external",
        service_family="codex_apps_mcp",
        header_profile="codex_apps_mcp",
        application_metadata=common_meta,
        trust_domain="first-party ChatGPT/Codex Apps MCP",
        connection_lifetime="session/server-lifetime",
    ))
    catalog.setdefault("settings_catalog", {})["mcp"] = {"$ref": "#/mcp_protocol"}


def extra_contract(report: dict[str, Any]) -> dict[str, Any]:
    c = report.get("turn_metadata_construction") or {}; lim = c.get("extra_metadata_limits") or {}
    return {"wire_location": 'client_metadata["x-codex-turn-metadata"].<extra-key>', "wire_wrapper": None,
            "config_validation": {"max_entries": lim.get("MAX_EXTRA_METADATA_ENTRIES"),
              "max_key_bytes": lim.get("MAX_EXTRA_METADATA_KEY_BYTES"),
              "max_value_bytes": lim.get("MAX_EXTRA_METADATA_VALUE_BYTES"),
              "key_charset": c.get("extra_metadata_key_charset"),
              "reserved_keys": c.get("extra_metadata_reserved_keys") or []}}


def protocol_inputs(extra: dict[str, str], report: dict[str, Any]) -> dict[str, Any]:
    rows = []; contract = extra_contract(report)
    specs = [
        ("app_server_turn", "TurnStartParams", "responsesapi_client_metadata", "app_server.turn/start",
         'flattened x-codex-turn-metadata extras', "reserved keys filtered; setter does not apply config size bounds"),
        ("protocol_turn_input", "TurnInputRequest", "responsesapi_client_metadata", "core.protocol",
         "TurnMetadataState.responsesapi_client_metadata", "internal transport field"),
        ("config_toml", "ConfigToml", "responses_api_metadata", "config.toml",
         'flattened x-codex-turn-metadata extras', "validate_extra_metadata bounded"),
    ]
    warnings: list[str] = []
    for key, st, fld, surface, dst, note in specs:
        if key not in extra:
            continue
        try:
            f = struct_field(extra[key], EXTRA[key], st, fld)
        except AuditError as exc:
            warnings.append(f"protocol input anchor changed: {EXTRA[key]}: {exc}")
            continue
        if f:
            rows.append({
                "surface": surface,
                "field": f,
                "optional": f["optional"],
                "destination": dst,
                "top_level_responses_client_metadata": False,
                "note": note,
                "validation": contract["config_validation"] if key == "config_toml" else None,
            })
    tm, cc = extra.get("turn_metadata", ""), extra.get("core_config", "")
    checks = {
      "client_reserved_keys_filtered": "filter_extra_metadata(responsesapi_client_metadata)" in tm,
      "config_reserved_keys_filtered": "filter_extra_metadata(responses_api_metadata)" in tm,
      "config_wins_collision": "extra.remove(key);" in tm and "metadata.extra.extend(" in tm,
      "config_validated": "validate_extra_metadata(responses_api_metadata.iter())" in cc,
    }
    return {"inputs": rows, "merge": {
        "canonical_destination": 'CodexResponsesMetadata.extra -> serde(flatten) in client_metadata["x-codex-turn-metadata"]',
        "precedence": "config.responses_api_metadata overrides duplicate responsesapiClientMetadata keys",
        "algorithm": ["filter reserved Core keys", "drop client keys shadowed by config", "extend with config", "serde(flatten) into turn JSON"],
        "source_checks": checks, "contract": contract,
        "source": anchor(tm, EXTRA["turn_metadata"], "fn mcp_metadata_template(&self)", "TurnMetadataState metadata merge")},
        "warnings": warnings + ([] if all(checks.values()) else ["protocol-extra merge anchors changed upstream; inspect source_checks"])}


def relation_map() -> dict[str, Any]:
    # path, surface, relation, optional, note
    G = {
      "installation_id": ("CodexResponsesMetadata.installation_id", [
        ('client_metadata["x-codex-installation-id"]','http/ws','same_value',False,None),
        ('client_metadata["x-codex-turn-metadata"].installation_id','turn_json','same_value_when_request_identity',True,None),
        ('header.x-codex-installation-id','compact','same_value',False,None)]),
      "session_id": ("CodexResponsesMetadata.session_id", [
        ('header.session-id','http/ws_handshake','same_value',False,'dash spelling'),
        ('client_metadata["session_id"]','http/ws','same_value',False,None),
        ('client_metadata["x-codex-turn-metadata"].session_id','turn_json','same_value_when_turn_identity',True,None)]),
      "thread_id": ("CodexResponsesMetadata.thread_id", [
        ('header.thread-id','http/ws_handshake','same_value',False,None),
        ('client_metadata["thread_id"]','http/ws','same_value',False,None),
        ('client_metadata["x-codex-turn-metadata"].thread_id','turn_json','same_value_when_turn_identity',True,None),
        ('header.x-client-request-id','http/ws_handshake','same_runtime_value_different_semantics',False,'derived from thread_id')]),
      "window_id": ("CodexResponsesMetadata.window_id", [
        ('header.x-codex-window-id','http/ws_handshake/compact','same_value',False,None),
        ('client_metadata["x-codex-window-id"]','http/ws','same_value',False,None),
        ('client_metadata["x-codex-turn-metadata"].window_id','turn_json','same_value_when_request_identity',True,None)]),
      "turn_id": ("CodexResponsesMetadata.turn_id", [
        ('client_metadata["turn_id"]','http/ws','same_value',True,None),
        ('client_metadata["x-codex-turn-metadata"].turn_id','turn_json','same_value_when_turn_identity',True,None)]),
      "parent_thread_id": ("CodexResponsesMetadata.parent_thread_id", [
        ('header.x-codex-parent-thread-id','http/ws_handshake/compact','same_value',True,None),
        ('client_metadata["x-codex-parent-thread-id"]','http/ws','same_value',True,None),
        ('client_metadata["x-codex-turn-metadata"].parent_thread_id','turn_json','same_value',True,None)]),
      "subagent": ("SessionSource -> subagent_header/subagent_kind", [
        ('header.x-openai-subagent','http/ws_handshake/compact','same_derived_header_value',True,None),
        ('client_metadata["x-openai-subagent"]','http/ws','same_derived_header_value',True,None),
        ('client_metadata["x-codex-turn-metadata"].subagent_kind','turn_json','related_not_equal',True,'normalized kind vs label/internal source')]),
      "turn_metadata_blob": ("CodexResponsesMetadata::turn_metadata_payload()", [
        ('client_metadata["x-codex-turn-metadata"]','http','canonical_full_json',True,'includes tool inventory + extras'),
        ('response.create.client_metadata["x-codex-turn-metadata"]','ws','canonical_full_json',True,'same builder'),
        ('header.x-codex-turn-metadata','http/ws_handshake/compact','bounded_projection',True,'tool_namespaces_info forced None')]),
      "flattened_extra_metadata": ("CodexResponsesMetadata.extra", [
        ('turn/start.responsesapiClientMetadata.*','app_server','input_to_flattened_extra',True,'reserved filtered'),
        ('config.responses_api_metadata.*','config','input_to_flattened_extra',True,'validated; wins collisions'),
        ('client_metadata["x-codex-turn-metadata"].<extra-key>','http/ws','canonical_output',True,'no extra wrapper')]),
      "turn_state": ("server-owned x-codex-turn-state", [
        ('response.header.x-codex-turn-state','http/compact','source',True,None),
        ('request.header.x-codex-turn-state','http/compact','replay_same_value',True,'same turn only'),
        ('response.create.client_metadata["x-codex-turn-state"]','ws','replay_same_value',True,'not handshake')]),
      "responses_lite_marker": ("Responses Lite marker", [
        ('header.x-openai-internal-codex-responses-lite','http/compact','same_marker_different_transport',True,None),
        ('response.create.client_metadata["ws_request_header_x_openai_internal_codex_responses_lite"]','ws','same_marker_different_name',True,None)]),
      "service_tier": ("turn/model service tier selection", [
        ('ResponsesApiRequest.service_tier','http/ws','wire_body_value',True,'priority/flex when supported; default omitted'),
        ('header.x-codex-routing-hint.tier','http/ws_handshake','derived_projection',True,'first-party /responses routing only'),
        ('/guardian service_tier','guardian','suppressed',True,'full Guardian clears service_tier before send')]),
      "response_continuation_id": ("server response.id", [
        ('response.completed.response.id','server','source',True,None),
        ('response.create.previous_response_id','ws','replay_same_value',True,'incremental continuation')]),
      "guardian_ticket_lifecycle": ("opaque Guardian receipt returned for one server response", [
        ('client_metadata["guardian_ticket_requested"]','/responses request','request_marker',True,'asks the normal Responses route to return a receipt when eligible'),
        ('response.created.response.headers.<guardian-ticket-header>','server event','source',True,'opaque runtime receipt'),
        ('client_metadata["guardian_ticket"]','/guardian or /guardian-classifier request','opaque_receipt_replay',True,'attached only at the transport boundary'),
        ('client_metadata["x-codex-turn-metadata"].guardian_ticket','canonical turn snapshot','deliberately_not_projected',True,'runtime receipt is excluded from normal metadata projections')]),
      "model_catalog_validator": ("model catalog cache/version signal", [
        ('GET /models response.header.ETag','models endpoint','source',True,'standard HTTP cache validator'),
        ('Responses response.header.X-Models-Etag','responses HTTP','related_not_equal',True,'separate wire header used during inference traffic'),
        ('response.metadata.headers.x-models-etag','responses websocket','related_not_equal',True,'event-carried inference metadata')]),
      "redirect_referer": ("previous HTTP request URL", [
        ('request.header.Referer','redirect hop','derived_projection',True,'Codex-generated only on followed redirect hops; stale values are recomputed'),
        ('route-aware cross-origin Referer','redirect hop','bounded_projection',True,'reduced to the previous origin root'),
        ('route-aware HTTPS-to-HTTP Referer','redirect hop','suppressed',True,'omitted on downgrade'),
        ('MCP cross-origin redirect','mcp','suppressed',True,'redirect is rejected instead of replaying Referer')]),
      "tool_namespace_inventory": ("effective model-visible tool registry", [
        ('tool_registry.turn_metadata_includes_tool_info','config','input_gate',True,'must be enabled'),
        ('ModelInfo.use_responses_lite','model catalog','input_gate',False,'must also be true'),
        ('client_metadata["x-codex-turn-metadata"].tool_namespaces_info','responses turn_json','canonical_output',True,'records direct/deferred/code-mode exposure and Harness/MCP ownership'),
        ('header.x-codex-turn-metadata.tool_namespaces_info','compatibility header','deliberately_not_projected',True,'removed to keep the header bounded'),
        ('MCP request _meta["x-codex-turn-metadata"].tool_namespaces_info','mcp request','deliberately_not_projected',True,'external MCP servers do not receive harness-owned tool inventory')]),
      "mcp_turn_metadata_projection": ("TurnMetadataState::current_meta_value_for_mcp_request", [
        ('Responses client_metadata["x-codex-turn-metadata"]','responses','related_not_equal',True,'ASCII JSON string; full Responses projection'),
        ('MCP request params._meta["x-codex-turn-metadata"]','mcp','derived_object_projection',True,'JSON object; removes selected fields and adds model/codex_version/reasoning state'),
        ('config.responses_api_metadata.*','mcp','shadow_only',True,'config keys suppress same-named app-server extras but config values are not appended')]),
      "extra_metadata_precedence": ("TurnMetadataState extra maps", [
        ('turn/start.responsesapiClientMetadata.*','app_server','source',True,'reserved keys filtered'),
        ('config.responses_api_metadata.*','config','source',True,'validated and reserved keys filtered'),
        ('normal Responses nested extra','responses','merge_override',True,'config values override duplicate app-server values'),
        ('MCP nested extra','mcp','shadow_only',True,'app-server extras survive except keys shadowed by config')]),
    }
    for x in ("parent_turn_id", "root_turn_id"):
        G[x] = (f"CodexResponsesMetadata.{x}", [(f'client_metadata["{x}"]','http/ws','same_value',True,None),
          (f'client_metadata["x-codex-turn-metadata"].{x}','turn_json','same_value',True,None)])
    return {
        "canonical_truth": 'client_metadata["x-codex-turn-metadata"] is canonical turn snapshot',
        "groups": [
            {
                "concept": k,
                "canonical": v[0],
                "nodes": [
                    {"path": a, "surface": b, "relation": c, "optional": d, "note": e}
                    for a, b, c, d, e in v[1]
                ],
            }
            for k, v in G.items()
        ],
        "relation_types": {
            "same_value": "same runtime value projected to another surface",
            "same_value_when_request_identity": "same value when request identity is emitted",
            "same_value_when_turn_identity": "same value when turn identity is emitted",
            "same_runtime_value_different_semantics": "same bytes reused for a different protocol purpose",
            "related_not_equal": "same concept, different representation",
            "bounded_projection": "same payload with intentional omission",
            "canonical_full_json": "canonical full JSON snapshot",
            "input_to_flattened_extra": "input merged into flattened turn metadata extras",
            "canonical_output": "canonical output location",
            "source": "server/source value",
            "replay_same_value": "server value replayed unchanged",
            "same_marker_different_transport": "same feature marker on another transport",
            "same_marker_different_name": "same marker represented under a transport-specific key",
            "wire_body_value": "serialized body field",
            "derived_projection": "derived header projection of a body/config concept",
            "suppressed": "deliberately omitted on this route",
            "request_marker": "client asks the server to produce a related runtime receipt",
            "opaque_receipt_replay": "opaque server receipt replayed unchanged on a later specialized route",
            "deliberately_not_projected": "runtime value intentionally excluded from canonical/compatibility metadata projections",
            "input_gate": "configuration/model predicate that must hold before a projection is emitted",
            "derived_object_projection": "related metadata is transformed and emitted as a JSON object on another protocol",
            "shadow_only": "a source key suppresses another value but is not itself copied to the destination",
            "merge_override": "later source wins duplicate keys during merge",
        },
    }


def app_server_protocol(extra: dict[str, str]) -> dict[str, Any]:
    readme = extra.get("app_server_readme", "")
    realtime = extra.get("app_server_realtime", "")
    common = extra.get("app_server_common", "")
    account = extra.get("app_server_account", "")
    reset_processor = extra.get("app_server_rate_limit_resets", "")
    method_inventory = app_server_method_inventory(common)

    rate_limit_params = try_struct_summary(
        account, EXTRA["app_server_account"], "GetAccountRateLimitsParams"
    )
    token_usage_params = try_struct_summary(
        account, EXTRA["app_server_account"], "GetAccountTokenUsageParams"
    )
    consume_params = try_struct_summary(
        account, EXTRA["app_server_account"], "ConsumeAccountRateLimitResetCreditParams"
    )
    try:
        consume_outcomes = enum_summary(
            account,
            EXTRA["app_server_account"],
            "ConsumeAccountRateLimitResetCreditOutcome",
        )
    except AuditError:
        consume_outcomes = None

    account_usage_reset = {
        "rate_limits_read": {
            "method": "account/rateLimits/read",
            "plane": "client_control -> backend_control",
            "params": rate_limit_params,
            "backend_http_calls": [
                "GET /backend-api/wham/usage (or /api/codex/usage)",
                "best-effort GET /backend-api/wham/rate-limit-reset-credits (or /api/codex/rate-limit-reset-credits) for detailed credit rows",
            ],
            "fallback": "if detailed reset-credit fetch fails or times out, keep reset-credit summary from the usage response",
        },
        "token_activity_read": {
            "method": "account/usage/read",
            "plane": "client_control -> backend_control",
            "params": token_usage_params,
            "backend_http_calls": [
                "GET /backend-api/wham/profiles/me (or /api/codex/profiles/me)"
            ],
        },
        "reset_credit_consume": {
            "method": "account/rateLimitResetCredit/consume",
            "plane": "client_control -> backend_control",
            "params": consume_params,
            "backend_http_calls": [
                "POST /backend-api/wham/rate-limit-reset-credits/consume (or /api/codex/rate-limit-reset-credits/consume)"
            ],
            "translation": {
                "idempotencyKey": "backend JSON redeem_request_id",
                "creditId": "backend JSON credit_id (optional)",
            },
            "backend_content_type": "application/json",
            "recommended_follow_up": "refetch account/rateLimits/read after consume",
            "outcomes": consume_outcomes,
        },
    }

    return {
        "role": "client ↔ Codex Core control protocol; separate from model-inference wire and backend account HTTP",
        "wire_format": "JSON-RPC 2.0-like messages; Codex app-server omits the literal jsonrpc field",
        "transports": [
            {"transport": "stdio", "framing": "JSONL: one JSON message per line"},
            {"transport": "WebSocket", "framing": "one app-server JSON message per WebSocket text frame"},
        ],
        "method_inventory": method_inventory,
        "request_methods": method_inventory["client_requests"],
        "server_requests": method_inventory["server_requests"],
        "server_notifications": method_inventory["server_notifications"],
        "client_notifications": method_inventory["client_notifications"],
        "representative_methods": [
            {"method": "turn/start", "purpose": "start a model turn; may carry responsesapiClientMetadata"},
            {"method": "account/rateLimits/read", "purpose": "read rate limits plus reset-credit availability/details"},
            {"method": "account/usage/read", "purpose": "read account token activity"},
            {"method": "account/rateLimitResetCredit/consume", "purpose": "consume one available rate-limit reset credit idempotently"},
            {"method": "thread/realtime/start", "purpose": "start realtime session using websocket/webrtc/existingCall transport"},
            {"method": "thread/realtime/appendAudio", "purpose": "append realtime audio"},
            {"method": "thread/realtime/appendText", "purpose": "append realtime text"},
            {"method": "thread/realtime/stop", "purpose": "stop realtime session"},
        ],
        "account_usage_reset": account_usage_reset,
        "inventory_warnings": method_inventory["warnings"],
        "upstream_boundaries": {
            "app_server": "client request/notification protocol",
            "model_inference": "Codex Core emits /responses, /guardian, /guardian-classifier, compact/realtime inference traffic",
            "backend_account_control": "Codex Core separately calls ChatGPT/Codex backend /usage, /profiles/me, reset-credit, and thread-usage routes",
            "important": "app-server method names are RPC methods, not HTTP paths; backend account routes are not model-provider inference endpoints",
        },
        "source": {
            "readme": anchor(readme, EXTRA["app_server_readme"], "JSON-RPC", "app-server wire protocol"),
            "turn_start": anchor(readme, EXTRA["app_server_readme"], "turn/start", "turn/start"),
            "realtime": anchor(realtime, EXTRA["app_server_realtime"], "ThreadRealtimeStartParams", "thread/realtime/start"),
            "rate_limits": anchor(common, EXTRA["app_server_common"], "account/rateLimits/read", "account/rateLimits/read"),
            "account_usage": anchor(common, EXTRA["app_server_common"], "account/usage/read", "account/usage/read"),
            "reset_consume": anchor(common, EXTRA["app_server_common"], "account/rateLimitResetCredit/consume", "account/rateLimitResetCredit/consume"),
            "reset_backend_bridge": anchor(reset_processor, EXTRA["app_server_rate_limit_resets"], "consume_account_rate_limit_reset_credit", "reset-credit backend bridge"),
        },
    }


def append_app_server_surface(report: dict[str, Any], app: dict[str, Any]) -> None:
    catalog = report.get("wire_surface_catalog") or report.get("endpoint_protocol_catalog")
    if not isinstance(catalog, dict):
        return
    catalog.setdefault("planes", {})["client_control"] = (
        "Client ↔ Codex app-server RPC; method names are not upstream HTTP endpoints"
    )
    surfaces = catalog.setdefault("surfaces", [])
    categories = catalog.setdefault("categories", {})

    def add_surface(surface: dict[str, Any]) -> None:
        if not any(existing.get("id") == surface["id"] for existing in surfaces):
            surfaces.append(surface)
        category = categories.setdefault(surface["category"], [])
        if surface["id"] not in category:
            category.append(surface["id"])

    inventory = app.get("method_inventory") or {}
    request_rows = app.get("request_methods") or []
    if request_rows:
        rpc_methods = [row["method"] for row in request_rows]
    else:
        rpc_methods = [row["method"] for row in app.get("representative_methods") or []]
    server_request_rows = app.get("server_requests") or []
    server_notification_rows = app.get("server_notifications") or []
    client_notification_rows = app.get("client_notifications") or []

    add_surface({
        "id": "app_server_rpc",
        "plane": "client_control",
        "service_family": "codex_app_server",
        "category": "control.app_server",
        "addressing": {"kind": "rpc_method", "values": rpc_methods},
        "endpoints": [],
        "rpc_methods": rpc_methods,
        "transports": ["stdio", "app-server WebSocket"],
        "method": "client-to-server JSON-RPC-style requests",
        "transport": "stdio or WebSocket",
        "protocol": "Codex app-server control protocol",
        "streaming": False,
        "responses_compatible": False,
        "usage_status": "active",
        "trust_domain": "local/host client ↔ Codex Core",
        "lifecycle": {
            "connection_lifetime": "persistent process/session channel",
            "payload_streaming": False,
            "interaction": "request/response plus asynchronous bidirectional notifications",
        },
        "request": {
            "content_type": None,
            "accept": None,
            "framing": "JSONL on stdio or one JSON message per WebSocket text frame",
            "body_schema": "generated app-server ClientRequest variants",
            "body_fields": [],
        },
        "response": {
            "media_type": None,
            "framing": "JSON-RPC-style response; server requests/notifications share the channel",
            "body_schema": "generated ClientResponse variants",
            "body_fields": [],
            "event_metadata": server_notification_rows,
        },
        "headers": {
            "profile": "app_server_message_layer",
            "inherits_inference_headers": False,
            "excluded_names": [],
            "request_names": [],
            "request_by_category": {},
            "response_names": [],
            "response_by_category": {},
            "response_templates": [],
        },
        "application_metadata": {
            "exhaustive_for_source_macros": inventory.get("exhaustive_for_source_macros", False),
            "method_inventory_counts": inventory.get("counts", {}),
            "request_methods": request_rows,
            "client_notifications": client_notification_rows,
        },
        "profiles": {},
        "transport_generated_headers": [],
        "variants": app["transports"],
        "notes": [
            "This is client↔Codex Core, not Codex↔model-provider traffic.",
            "The addressing values are extracted from client_request_definitions!, not a hand-maintained representative subset.",
            "No per-message x-codex HTTP header set exists at the app-server JSON message layer; transport handshakes are separate.",
        ],
        "source": inventory.get("source") or app.get("source", {}).get("readme"),
    })

    if server_request_rows:
        server_methods = [row["method"] for row in server_request_rows]
        add_surface({
            "id": "app_server_server_requests",
            "plane": "client_control",
            "service_family": "codex_app_server.server_requests",
            "category": "control.app_server.server_request",
            "addressing": {"kind": "rpc_method", "values": server_methods},
            "endpoints": [],
            "rpc_methods": server_methods,
            "method": "server-to-client JSON-RPC-style requests",
            "transport": "stdio or app-server WebSocket",
            "protocol": "Codex app-server bidirectional control protocol",
            "streaming": False,
            "responses_compatible": False,
            "usage_status": "active",
            "trust_domain": "Codex Core ↔ local/host client",
            "lifecycle": {"connection_lifetime": "persistent process/session channel", "payload_streaming": False},
            "request": {
                "content_type": None,
                "accept": None,
                "framing": "JSONL or one WebSocket text-frame JSON message",
                "body_schema": "generated ServerRequest variants",
                "body_fields": [],
            },
            "response": {
                "media_type": None,
                "framing": "client JSON-RPC-style response",
                "body_schema": "generated server-request response types",
                "body_fields": [],
                "event_metadata": [],
            },
            "headers": {
                "profile": "app_server_message_layer",
                "inherits_inference_headers": False,
                "excluded_names": [],
                "request_names": [],
                "request_by_category": {},
                "response_names": [],
                "response_by_category": {},
                "response_templates": [],
            },
            "application_metadata": {"methods": server_request_rows},
            "profiles": {},
            "transport_generated_headers": [],
            "variants": app["transports"],
            "notes": ["Approval, elicitation, and similar server-originated calls share the app-server connection."],
            "source": server_request_rows[0].get("source"),
        })

    notification_rows = server_notification_rows + client_notification_rows
    if notification_rows:
        notification_methods = list(dict.fromkeys(row["method"] for row in notification_rows))
        add_surface({
            "id": "app_server_notifications",
            "plane": "client_control",
            "service_family": "codex_app_server.notifications",
            "category": "control.app_server.notification",
            "addressing": {"kind": "notification_method", "values": notification_methods},
            "endpoints": [],
            "rpc_methods": notification_methods,
            "method": "bidirectional notifications (no response)",
            "transport": "stdio or app-server WebSocket",
            "protocol": "Codex app-server notification protocol",
            "streaming": False,
            "responses_compatible": False,
            "usage_status": "active",
            "trust_domain": "local/host client ↔ Codex Core",
            "lifecycle": {"connection_lifetime": "persistent process/session channel", "payload_streaming": False},
            "request": {
                "content_type": None,
                "accept": None,
                "framing": "JSONL or one WebSocket text-frame JSON message",
                "body_schema": "generated client/server notification variants",
                "body_fields": [],
            },
            "response": {
                "media_type": None,
                "framing": "none",
                "body_schema": None,
                "body_fields": [],
                "event_metadata": notification_rows,
            },
            "headers": {
                "profile": "app_server_message_layer",
                "inherits_inference_headers": False,
                "excluded_names": [],
                "request_names": [],
                "request_by_category": {},
                "response_names": [],
                "response_by_category": {},
                "response_templates": [],
            },
            "application_metadata": {
                "server_to_client": server_notification_rows,
                "client_to_server": client_notification_rows,
            },
            "profiles": {},
            "transport_generated_headers": [],
            "variants": app["transports"],
            "notes": ["Notifications are one-way messages and must not be mistaken for request endpoints."],
            "source": notification_rows[0].get("source"),
        })

    account_rpc_methods = [
        "account/rateLimits/read",
        "account/usage/read",
        "account/rateLimitResetCredit/consume",
    ]
    add_surface({
        "id": "app_server_account_usage_reset_rpc",
        "plane": "client_control",
        "service_family": "codex_app_server.account",
        "category": "control.app_server.account",
        "addressing": {"kind": "rpc_method", "values": account_rpc_methods},
        "endpoints": [],
        "rpc_methods": account_rpc_methods,
        "method": "JSON-RPC-style methods",
        "transport": "stdio or app-server WebSocket",
        "protocol": "Codex app-server account usage/reset control protocol",
        "streaming": False,
        "responses_compatible": False,
        "usage_status": "active",
        "trust_domain": "local/host client ↔ Codex Core",
        "lifecycle": {"connection_lifetime": "persistent process/session channel", "payload_streaming": False},
        "request": {
            "content_type": None,
            "accept": None,
            "framing": "JSONL or WebSocket text-frame JSON",
            "body_schema": "GetAccountRateLimitsParams | GetAccountTokenUsageParams | ConsumeAccountRateLimitResetCreditParams",
            "body_fields": [],
        },
        "response": {
            "media_type": None,
            "framing": "JSON-RPC-style result",
            "body_schema": "account usage/rate-limit/reset response DTOs",
            "body_fields": [],
            "event_metadata": [],
        },
        "headers": {
            "profile": "app_server_message_layer",
            "inherits_inference_headers": False,
            "excluded_names": [],
            "request_names": [],
            "request_by_category": {},
            "response_names": [],
            "response_by_category": {},
            "response_templates": [],
        },
        "application_metadata": {},
        "profiles": {},
        "transport_generated_headers": [],
        "variants": list((app.get("account_usage_reset") or {}).values()),
        "notes": [
            "These are app-server RPC methods, not HTTP endpoint paths.",
            "Codex Core translates them into ChatGPT/Codex backend-control HTTP routes.",
            "Those backend calls use the backend-account header profile, not Responses turn/routing headers.",
        ],
        "source": app.get("source", {}).get("rate_limits"),
    })

    coverage = catalog.setdefault("coverage", {})
    coverage["app_server_macro_inventory"] = {
        "exhaustive_for_source_macros": inventory.get("exhaustive_for_source_macros", False),
        "counts": inventory.get("counts", {}),
        "warnings": inventory.get("warnings", []),
    }


def enhance(
    report: dict[str, Any],
    base_src: dict[str, str],
    extra: dict[str, str],
    warnings: list[str],
    strict: bool,
) -> dict[str, Any]:
    report["schema_version"] = REPORT_SCHEMA_VERSION
    report["report_schema_version"] = REPORT_SCHEMA_VERSION
    report["catalog_schema_version"] = CATALOG_SCHEMA_VERSION
    report["generator_version"] = GENERATOR_VERSION
    report["source"]["files"] = list(dict.fromkeys(
        report["source"]["files"] + [EXTRA[key] for key in EXTRA if key in extra]
    ))
    report["scope"].setdefault("warnings", []).extend(warnings)
    diagnostics = report["scope"].setdefault("diagnostics", {})
    diagnostics["wrapper_optional_source_warnings"] = list(warnings)

    # Existing extractor derives Option<T>, serde skip/default/flatten and source
    # locations. The wrapper adds cross-protocol relationships and macro inventories.
    ts = report["turn_metadata_schema"]
    ts["canonical_wire_path"] = 'client_metadata["x-codex-turn-metadata"]'
    ts["canonical_wire_type"] = "ASCII JSON string"
    if ts.get("flattened_extra"):
        ts["flattened_extra"] = dict(
            ts["flattened_extra"],
            wire_path='client_metadata["x-codex-turn-metadata"].<extra-key>',
            wire_wrapper=None,
            contract=extra_contract(report),
        )
    nested = dict(ts.get("nested") or {})
    try:
        nested["tool_source"] = enum_summary(
            base_src["metadata"], B.FILES["metadata"], "TurnToolSource"
        )
    except AuditError:
        pass
    if "protocol" in extra:
        for key, name in (
            ("thread_source", "ThreadSource"),
            ("session_source", "SessionSource"),
            ("subagent_source", "SubAgentSource"),
        ):
            try:
                nested[key] = enum_summary(extra["protocol"], EXTRA["protocol"], name)
            except AuditError:
                pass
    ts["nested"] = nested
    ts["compatibility_header_difference"] = (
        "header projection uses the same payload with tool_namespaces_info=None"
    )

    protocol_input = protocol_inputs(extra, report)
    report["protocol_metadata_inputs"] = protocol_input
    report["scope"]["warnings"].extend(protocol_input["warnings"])
    diagnostics["protocol_schema_warnings"] = list(protocol_input["warnings"])

    config_protocol = build_config_protocol(extra, base_src)
    report["config_protocol"] = config_protocol
    report["scope"]["warnings"].extend(config_protocol.get("warnings") or [])
    diagnostics["config_protocol_warnings"] = list(config_protocol.get("warnings") or [])

    mcp_protocol = build_mcp_protocol(extra, report)
    report["mcp_protocol"] = mcp_protocol
    report["scope"]["warnings"].extend(mcp_protocol.get("warnings") or [])
    diagnostics["mcp_protocol_warnings"] = list(mcp_protocol.get("warnings") or [])

    redirect_protocol = build_redirect_header_protocol(extra)
    report["http_redirect_protocol"] = redirect_protocol
    report["scope"]["warnings"].extend(redirect_protocol.get("warnings") or [])
    diagnostics["http_redirect_protocol_warnings"] = list(
        redirect_protocol.get("warnings") or []
    )

    responses_protocol = build_responses_protocol(base_src, extra, report)
    report["responses_protocol"] = responses_protocol
    report["scope"]["warnings"].extend(responses_protocol.get("warnings") or [])
    diagnostics["responses_protocol_warnings"] = list(responses_protocol.get("warnings") or [])

    responses_lite_protocol = build_responses_lite_protocol(base_src, extra, report)
    report["responses_lite_protocol"] = responses_lite_protocol
    report["scope"]["warnings"].extend(responses_lite_protocol.get("warnings") or [])
    diagnostics["responses_lite_protocol_warnings"] = list(
        responses_lite_protocol.get("warnings") or []
    )

    report["metadata_protocol"] = build_metadata_protocol(
        report, base_src, extra, mcp_protocol
    )
    report["scope"]["warnings"].extend(
        report["metadata_protocol"].get("warnings") or []
    )
    diagnostics["metadata_protocol_warnings"] = list(
        report["metadata_protocol"].get("warnings") or []
    )
    report["relation_map"] = relation_map()

    app = app_server_protocol(extra)
    report["app_server_protocol"] = app
    report["scope"]["warnings"].extend(app.get("inventory_warnings") or [])
    diagnostics["app_server_inventory_warnings"] = list(app.get("inventory_warnings") or [])
    append_app_server_surface(report, app)
    append_mcp_surfaces(report, mcp_protocol)

    catalog = report.get("wire_surface_catalog") or {}
    if isinstance(catalog, dict):
        catalog.setdefault("settings_catalog", {})["config"] = {"$ref": "#/config_protocol"}
        catalog.setdefault("protocol_profiles", {})["responses_full"] = {"$ref": "#/responses_protocol"}
        catalog.setdefault("protocol_profiles", {})["responses_lite"] = {"$ref": "#/responses_lite_protocol"}
        catalog.setdefault("metadata_profiles", {})["codex_client_metadata"] = {"$ref": "#/metadata_protocol"}
        catalog.setdefault("transport_generated_header_profiles", {})["referer"] = {
            "$ref": "#/http_redirect_protocol"
        }
    coverage = catalog.setdefault("coverage", {}) if isinstance(catalog, dict) else {}
    if isinstance(catalog, dict):
        coverage["catalog_surface_ids"] = sorted(
            str(surface.get("id")) for surface in catalog.get("surfaces", [])
        )
        coverage["app_server_macro_inventory"] = {
            "exhaustive_for_source_macros": app.get("method_inventory", {}).get(
                "exhaustive_for_source_macros", False
            ),
            "counts": app.get("method_inventory", {}).get("counts", {}),
            "warnings": app.get("inventory_warnings", []),
        }
        coverage["config_protocol"] = {
            "config_toml_field_count": len(config_protocol.get("config_toml", {}).get("fields", [])),
            "effective_config_field_count": len(config_protocol.get("effective_config", {}).get("fields", [])),
            "selected_wire_setting_count": len(config_protocol.get("wire_affecting_settings", [])),
            "feature_registry_count": config_protocol.get("feature_registry", {}).get("counts", {}).get("total", 0),
            "warnings": config_protocol.get("warnings", []),
        }
        coverage["mcp_protocol"] = {
            "server_setting_count": len(mcp_protocol.get("configuration", {}).get("server", {}).get("settings", [])),
            "operation_count": len(mcp_protocol.get("operations", [])),
            "transport_count": len(mcp_protocol.get("transports", [])),
            "warnings": mcp_protocol.get("warnings", []),
        }
        coverage["http_redirect_protocol"] = {
            "referer_profile_count": len(redirect_protocol.get("profiles", [])),
            "first_request_generated": redirect_protocol.get("header", {}).get(
                "first_request_generated"
            ),
            "redirect_request_generated": redirect_protocol.get("header", {}).get(
                "redirect_request_generated"
            ),
            "warnings": redirect_protocol.get("warnings", []),
        }
        coverage["responses_protocol"] = {
            "event_kind_count": len(responses_protocol.get("response_stream", {}).get("event_kinds_discovered", [])),
            "dispatch_count": len(responses_protocol.get("response_stream", {}).get("dispatch", [])),
            "unclassified_event_kinds": responses_protocol.get("response_stream", {}).get("unclassified_discovered_event_kinds", []),
            "response_item_variant_count": len(
                (responses_protocol.get("input_and_output_items", {}).get("enums", {}).get("ResponseItem") or {}).get("variants", [])
            ),
        }
        coverage["metadata_protocol"] = {
            "top_level_client_metadata_key_count": len(
                report.get("metadata_protocol", {}).get("top_level_client_metadata", {}).get("base_and_conditional_keys", [])
            ),
            "nested_fixed_field_count": len(
                report.get("metadata_protocol", {}).get("nested_turn_metadata", {}).get("fixed_fields", [])
            ),
            "flattened_extra_documented": bool(report.get("metadata_protocol", {}).get("flattened_extra")),
        }
    report["coverage"] = coverage

    report["protocol_layer_map"] = {
        "layer_0_configuration": (
            "config.toml, requirements, feature registry, provider/model capabilities, and per-turn overrides"
        ),
        "layer_1_client_control": (
            "client ↔ Codex app-server generated requests, server requests, and notifications "
            "over stdio JSONL or WebSocket"
        ),
        "layer_2_codex_core": "turn/tool/session orchestration and metadata projection",
        "layer_3a_model_inference": (
            "Codex Core ↔ model inference services: Responses HTTP+SSE/WSS, Guardian, "
            "legacy compact, and inference-side realtime"
        ),
        "layer_3b_backend_account_control": (
            "Codex Core ↔ ChatGPT/Codex backend: account usage, reset credits, token activity, "
            "thread usage, turn estimates, and hosted-file registration/finalization"
        ),
        "layer_3c_provider_auxiliary": "model discovery, images, search, and memory services",
        "layer_3d_external_storage": "server-issued object-storage upload URL used for hosted files",
        "layer_3e_realtime_media": "Realtime signaling/events/media: dedicated WSS, WebRTC, and sideband",
        "layer_3f_cost_analytics": "API-key turn-cost analytics with provider-scope header allowlisting",
        "layer_3g_mcp_external": "Codex as MCP client over stdio, Streamable HTTP, or in-process transport; MCP tools are later projected into Responses schemas",
        "layer_4_transport_redirect": "HTTP redirect-hop transport behavior, including derived Referer and origin-sensitive credential stripping",
    }

    body_fields = B.struct_fields(base_src["common"], B.FILES["common"], "ResponsesApiRequest")
    try:
        ws_body_fields = B.struct_fields(
            base_src["common"], B.FILES["common"], "ResponseCreateWsRequest"
        )
    except AuditError:
        ws_body_fields = []

    # v7 serialized the catalog several times under aliases and inside
    # full_wire_schema. v10 keeps one canonical catalog and uses JSON-pointer-like
    # references in the convergence view.
    report["full_wire_schema"] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "canonical_truth": {
            "turn_snapshot": ts["canonical_wire_path"],
            "compatibility_projections": ["flat client_metadata", "direct HTTP/WS headers"],
        },
        "sections": {
            "responses_http": {
                "$ref": "#/responses_http",
                "request_body": {
                    "struct": "ResponsesApiRequest",
                    "fields": body_fields,
                    "client_metadata_ref": "#/responses_http/client_metadata",
                    "turn_metadata_schema_ref": "#/turn_metadata_schema",
                },
            },
            "responses_websocket": {
                "$ref": "#/responses_websocket",
                "response_create": {
                    "struct": "ResponseCreateWsRequest",
                    "fields": ws_body_fields,
                    "client_metadata_ref": "#/responses_websocket/client_metadata_base",
                    "ws_additions_ref": "#/responses_websocket/client_metadata_ws_additions",
                    "turn_metadata_schema_ref": "#/turn_metadata_schema",
                    "continuation_ref": "#/responses_websocket/request_body_continuation",
                },
            },
            "responses_compact": {"$ref": "#/responses_compact"},
            "protocol_metadata_inputs": {"$ref": "#/protocol_metadata_inputs"},
            "config_protocol": {"$ref": "#/config_protocol"},
            "mcp_protocol": {"$ref": "#/mcp_protocol"},
            "http_redirect_protocol": {"$ref": "#/http_redirect_protocol"},
            "responses_protocol": {"$ref": "#/responses_protocol"},
            "responses_lite_protocol": {"$ref": "#/responses_lite_protocol"},
            "metadata_protocol": {"$ref": "#/metadata_protocol"},
            "relation_map": {"$ref": "#/relation_map"},
            "app_server_protocol": {"$ref": "#/app_server_protocol"},
            "protocol_layer_map": {"$ref": "#/protocol_layer_map"},
        },
        "wire_surface_catalog_ref": "#/wire_surface_catalog",
        "shared_profiles_ref": "#/wire_surface_catalog/shared_profiles",
        "transport_delta_ref": "#/wire_surface_catalog/transport_delta",
        "model_facing_transport_matrix_ref": (
            "#/wire_surface_catalog/model_facing_transport_matrix"
        ),
        "coverage_ref": "#/coverage",
    }
    report["canonical_metadata_statement"] = {
        "canonical": ts["canonical_wire_path"],
        "source": anchor(
            base_src["metadata"],
            B.FILES["metadata"],
            "compatibility projections of this snapshot",
            "CodexResponsesMetadata source-of-truth comment",
        ),
    }

    # The old alias remains a lightweight reference; it no longer duplicates the
    # full catalog in serialized JSON.
    report["endpoint_protocol_catalog"] = {
        "deprecated": True,
        "$ref": "#/wire_surface_catalog",
        "replacement": "wire_surface_catalog",
    }

    if strict and report["scope"]["warnings"]:
        raise AuditError("; ".join(report["scope"]["warnings"]))
    return report


def print_v10(r: dict[str, Any]) -> None:
    print("\n=== 7. FULL CONVERGED METADATA SCHEMA ===")
    print('canonical: client_metadata["x-codex-turn-metadata"]')
    print("flat client_metadata:")
    for x in r["responses_http"]["client_metadata"]:
        state = "optional" if x["optional"] else "required/current-normal-path"
        print(f"  - {x['name']}: {state}")
    print("nested turn metadata:")
    for f in r["turn_metadata_schema"]["fixed_fields"]:
        suffix = " [optional]" if f["optional"] else ""
        print(f"  - {f['wire_name']}: {f['type']}{suffix}")
    if r["turn_metadata_schema"].get("flattened_extra"):
        print("  - <extra-key>: flattened string; optional; no extra wrapper")

    print("\n=== 8. PROTOCOL EXTRA INPUTS ===")
    for x in r["protocol_metadata_inputs"]["inputs"]:
        print(
            f"  - {x['surface']}.{x['field']['wire_name']}: "
            f"optional={x['optional']} -> {x['destination']}"
        )
    print("  precedence: " + r["protocol_metadata_inputs"]["merge"]["precedence"])

    print("\n=== 9. CROSS-SURFACE RELATION MAP ===")
    for g in r["relation_map"]["groups"]:
        print(f"  {g['concept']} <- {g['canonical']}")
        for n in g["nodes"]:
            optional = "; optional" if n["optional"] else ""
            note = "; " + n["note"] if n.get("note") else ""
            print(f"    - {n['path']} [{n['surface']}; {n['relation']}{optional}]{note}")

    print("\n=== 10. APP-SERVER RPC CONTROL PROTOCOL ===")
    app = r.get("app_server_protocol") or {}
    print("  role: " + str(app.get("role")))
    print("  wire: " + str(app.get("wire_format")))
    for t in app.get("transports") or []:
        print(f"  - {t['transport']}: {t['framing']}")
    inventory = app.get("method_inventory") or {}
    if inventory:
        print("  generated method inventory: " + json.dumps(inventory.get("counts") or {}, sort_keys=True))
        print(f"  exhaustive for source macros: {inventory.get('exhaustive_for_source_macros')}")
    print("  representative methods:")
    for m in app.get("representative_methods") or []:
        print(f"    - {m['method']}: {m['purpose']}")
    flows = app.get("account_usage_reset") or {}
    if flows:
        print("  account usage/reset bridge:")
        for name, flow in flows.items():
            calls = ", ".join(flow.get("backend_http_calls") or [])
            print(f"    - {name}: {flow.get('method')} -> {calls}")

    config = r.get("config_protocol") or {}
    print("\n=== 11. CONFIG → RUNTIME → WIRE SETTINGS ===")
    print(f"  ConfigToml fields: {len((config.get('config_toml') or {}).get('fields') or [])}")
    print(f"  selected wire-affecting settings: {len(config.get('wire_affecting_settings') or [])}")
    feature_counts = (config.get("feature_registry") or {}).get("counts") or {}
    print("  feature registry: " + json.dumps(feature_counts, ensure_ascii=False, sort_keys=True))
    for setting in config.get("wire_affecting_settings") or []:
        paths = ", ".join(effect.get("path", "") for effect in setting.get("wire_effects") or [])
        print(f"  - {setting['setting']}: {paths}")

    mcp = r.get("mcp_protocol") or {}
    print("\n=== 12. MCP CONFIG / TRANSPORT / MODEL PROJECTION ===")
    for transport in mcp.get("transports") or []:
        print(f"  - {transport['name']}: {transport.get('framing')}")
    print(f"  extracted MCP client operations: {len(mcp.get('operations') or [])}")
    projection = mcp.get("turn_metadata_projection") or {}
    print(f"  turn metadata: {projection.get('wire_path')} ({projection.get('wire_type')})")
    print("  explicitly removed: " + ", ".join(projection.get("explicitly_removed") or []))

    responses = r.get("responses_protocol") or {}
    print("\n=== 13. FULL RESPONSES LOGICAL PROTOCOL ===")
    stream = responses.get("response_stream") or {}
    print(f"  discovered event kinds: {len(stream.get('event_kinds_discovered') or [])}")
    print(f"  classified dispatch rows: {len(stream.get('dispatch') or [])}")
    item_enums = (responses.get("input_and_output_items") or {}).get("enums") or {}
    response_items = (item_enums.get("ResponseItem") or {}).get("variants") or []
    print(f"  ResponseItem variants: {len(response_items)}")

    lite = r.get("responses_lite_protocol") or {}
    print("\n=== 14. RESPONSES LITE REWRITE ===")
    for row in lite.get("request_rewrite") or []:
        print(f"  - {row['concept']}: full={row.get('full_responses')} | lite={row.get('responses_lite')}")
    print("  HTTP marker: " + str((lite.get("transport_markers") or {}).get("http_and_legacy_compact")))
    print("  WS marker: " + str((lite.get("transport_markers") or {}).get("responses_websocket")))

    metadata = r.get("metadata_protocol") or {}
    print("\n=== 15. CLIENT_METADATA / NESTED TURN METADATA / EXTRA ===")
    top = metadata.get("top_level_client_metadata") or {}
    print(f"  top-level key rows: {len(top.get('base_and_conditional_keys') or [])}")
    nested = metadata.get("nested_turn_metadata") or {}
    print(f"  nested fixed fields: {len(nested.get('fixed_fields') or [])}")
    extra_meta = metadata.get("flattened_extra") or {}
    print(f"  extra location: {extra_meta.get('wire_path')}; wrapper={extra_meta.get('wire_wrapper')}")
    print("  precedence: " + str(extra_meta.get("collision_precedence")))

    print("\n=== 16. PROTOCOL LAYER MAP ===")
    for key, value in (r.get("protocol_layer_map") or {}).items():
        print(f"  - {key}: {value}")


# Compatibility aliases for callers importing earlier helper names.
print_v9 = print_v10
print_v7 = print_v10
print_v5 = print_v10
print_v3 = print_v10

def self_test():
    s='''#[serde(rename_all = "camelCase")]\nstruct T { pub responsesapi_client_metadata: Option<HashMap<String, String>>, }'''
    f=struct_field(s,"x.rs","T","responsesapi_client_metadata"); assert f and f["wire_name"]=="responsesapiClientMetadata" and f["optional"]
    e='''#[serde(try_from = "String", into = "String")]\nenum ThreadSource { User, Feature(String), }'''
    q=enum_summary(e,"x.rs","ThreadSource"); assert q["custom_string_conversion"] and q["variants"][0]["wire_name"] is None
    lo='''#[serde(rename_all = "lowercase")]\nenum SessionSource {\n    Cli,\n    VSCode,\n    Exec,\n}'''
    z=enum_summary(lo,"x.rs","SessionSource"); assert [v["wire_name"] for v in z["variants"]]==["cli","vscode","exec"]
    app = app_server_protocol({})
    assert "client ↔ Codex Core" in app["role"]
    assert all("backend_http_calls" in flow for flow in app["account_usage_reset"].values())
    assert all("provider_calls" not in flow for flow in app["account_usage_reset"].values())
    account_demo = '''#[serde(rename_all = "camelCase")]
struct ConsumeAccountRateLimitResetCreditParams {
    pub idempotency_key: String,
    pub credit_id: Option<String>,
}
'''
    fields = struct_summary(account_demo, "account.rs", "ConsumeAccountRateLimitResetCreditParams")
    assert [x["wire_name"] for x in fields] == ["idempotencyKey", "creditId"]

    macro_demo = r'''
client_request_definitions! {
    Initialize => "initialize" {
        params: v1::InitializeParams,
        serialization: None,
        response: v1::InitializeResponse,
    },
    #[experimental("thread/example")]
    ThreadExample => "thread/example" {
        params: v2::ThreadExampleParams,
        serialization: thread_id(params.thread_id),
        response: v2::ThreadExampleResponse,
    },
}
server_request_definitions! {
    LegacyApproval {
        params: v1::LegacyApprovalParams,
        response: v1::LegacyApprovalResponse,
    },
    CommandApproval => "item/command/requestApproval" {
        params: v2::CommandApprovalParams,
        response: v2::CommandApprovalResponse,
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
    inventory = app_server_method_inventory(macro_demo)
    assert inventory["exhaustive_for_source_macros"]
    assert [x["method"] for x in inventory["client_requests"]] == ["initialize", "thread/example"]
    assert inventory["client_requests"][1]["experimental"]
    assert [x["method"] for x in inventory["server_requests"]] == ["legacyApproval", "item/command/requestApproval"]
    assert [x["method"] for x in inventory["server_notifications"]] == ["error", "threadStarted"]
    assert [x["method"] for x in inventory["client_notifications"]] == ["initialized", "ping"]

    demo_report = {
        "responses_http": {
            "request_headers": [
                {"name": "session-id"}, {"name": "thread-id"},
                {"name": "x-client-request-id"}, {"name": "x-codex-routing-hint"},
            ]
        },
        "responses_websocket": {
            "handshake_request_headers": [
                {"name": "session-id"}, {"name": "thread-id"},
                {"name": "x-client-request-id"}, {"name": "x-codex-routing-hint"},
            ],
            "client_metadata_base": [
                {"name": "session_id"}, {"name": "thread_id"},
                {"name": "x-codex-installation-id"}, {"name": "x-codex-window-id"},
            ],
            "client_metadata_ws_additions": [
                {"name": "ws_request_header_x_openai_internal_codex_responses_lite"},
                {"name": "x-codex-turn-state"},
            ],
        },
        "responses_compact": {
            "request_headers": [{"name": "x-codex-installation-id"}],
        },
    }
    matrix = B.build_model_facing_transport_matrix(demo_report, {"http": "", "ws": "", "core": ""}, {})
    routes = {row["route"]: row for row in matrix["routes"]}
    assert routes["/responses"]["http_sse"]["accept"] == "text/event-stream"
    assert routes["/responses"]["websocket"]["accept"] is None
    assert routes["/guardian"]["service_tier"].startswith("forced")
    assert routes["/guardian-classifier"]["http_sse"]["current_path"] is False
    assert routes["/responses/compact"]["websocket"]["capability"] is False
    feature = matrix["guardian_routing_feature"]
    assert feature["name"] == "free_guardian" and feature["is_endpoint"] is False
    assert feature["workloads"]["full_review"]["normal_route"] == "/responses"
    assert feature["workloads"]["full_review"]["unmetered_route"] == "/guardian"
    assert feature["workloads"]["classifier"]["normal_route"] == "/responses"
    assert feature["workloads"]["classifier"]["unmetered_route"] == "/guardian-classifier"
    assert routes["/guardian"]["fallback_route"] == "/responses"
    assert routes["/guardian-classifier"]["fallback_route"] == "/responses"
    assert matrix["schema_version"] >= 4
    assert "transport_delta" in matrix
    profiles = matrix["shared_profiles"]
    assert "responses_http_inference" in profiles["header_profiles"]
    assert "responses_ws_response_create" in profiles["client_metadata_profiles"]

    feature_demo = r'''
    enum Feature { Mcp20260728 }
    pub const FEATURES: &[FeatureSpec] = &[FeatureSpec {
        id: Feature::Mcp20260728,
        key: "mcp_2026_07_28",
        stage: Stage::UnderDevelopment,
        default_enabled: false,
    }];
    struct FeaturesToml { #[serde(flatten)] entries: BTreeMap<String, bool> }
    '''
    feature_inventory = feature_registry_summary(feature_demo)
    assert feature_inventory["by_id"]["Mcp20260728"]["key"] == "mcp_2026_07_28"

    event_demo = r'''
    pub fn process_responses_event(event: Event) -> Result<Option<ResponseEvent>, Error> {
        match event.kind.as_str() {
            "response.created" => { Ok(Some(ResponseEvent::Created)) }
            "response.failed" => { Err(ResponsesEventError::Api(ApiError::Retryable)) }
            "response.metadata" => { Ok(None) }
            _ => { Ok(None) }
        }
    }
    '''
    dispatch = {row["event"]: row for row in _match_arm_inventory(
        event_demo, "responses.rs", "pub fn process_responses_event"
    )}
    assert dispatch["response.created"]["disposition"] == "emitted"
    assert dispatch["response.failed"]["disposition"] == "terminal_error"

    lite = build_responses_lite_protocol(
        {"core": "if model_info.use_responses_lite {} fn add_responses_lite_header() {} ws_request_header_x_openai_internal_codex_responses_lite"},
        {
            "tool_spec": "fn create_tools_json_for_responses_lite() {}",
            "tool_spec_plan": "Responses Lite accepts schemas for client-executed tools; turn_metadata_includes_tool_info",
            "provider_runtime": "pub namespace_tools: bool",
            "model_protocol": "pub use_responses_lite: bool",
        },
        {"config_protocol": {"feature_registry": {"features": []}}, "responses_protocol": {}},
    )
    assert next(row for row in lite["request_rewrite"] if row["concept"] == "top-level tools")["responses_lite"] is None
    print("v10 protocol self-test: ok")



def contract_self_test() -> None:
    field = C.normalize_field(
        {
            "name": "value",
            "wire_name": "value",
            "type": "Option<Vec<String>>",
            "skip_serializing_if": "Option::is_none",
            "serialized": True,
            "source": None,
        },
        "schema.test",
        "/fixture/value",
    )
    assert field["presence"]["mode"] == "conditional"
    assert field["wire_schema"]["type"] == "array"
    assert C.condition_to_predicate("Responses Lite only")["machine_evaluable"] if "machine_evaluable" in C.condition_to_predicate("Responses Lite only") else True
    documents = C.schema_documents()
    assert "codex-wire-audit-report.schema.json" in documents
    sample = {
        "$schema": C.REPORT_SCHEMA_ID,
        "$id": "urn:codex-wire-audit:fixture:abc",
        "format_version": C.REPORT_FORMAT_VERSION,
        "versions": {"report_format": C.REPORT_FORMAT_VERSION, "generator": C.GENERATOR_VERSION},
        "reader_compatibility": {"minimum_report_format": C.REPORT_FORMAT_VERSION},
        "source_manifest": {
            "source_mode": "fixture",
            "repository": "fixture",
            "commit": {},
            "files": {},
            "path_index": {},
            "locations": {},
        },
        "parser": {"requested_backend": "regex", "effective_backend": "regex"},
        "entities": {
            "surfaces": {},
            "schemas": {},
            "fields": {},
            "headers": {},
            "header_templates": {},
            "settings": {},
            "events": {},
            "rules": {},
            "wire_locations": {},
        },
        "edges": [],
        "views": {},
        "machine_contract": {
            "schema_version": C.MACHINE_CONTRACT_VERSION,
            "counts": {
                "surfaces": 0,
                "schemas": 0,
                "fields": 0,
                "headers": 0,
                "header_templates": 0,
                "settings": 0,
                "events": 0,
                "rules": 0,
                "wire_locations": 0,
                "edges": 0,
            },
        },
        "coverage": {},
        "diagnostics": [],
        "diagnostic_summary": {"error": 0, "warning": 0, "info": 0},
        "status": {"state": "complete", "complete": True, "diagnostic_refs": []},
        "schema_documents": {"report": C.REPORT_SCHEMA_ID},
        "wire_surface_catalog": {},
    }
    C.attach_integrity(sample)
    assert not C.validate_report(sample)
    print("v10 contract self-test: ok")


def _load_generation_sources(args: argparse.Namespace):
    temporary_source = None
    source_mode = "github"
    dirty = None
    requested_ref = args.ref
    if args.repo_root:
        source_mode = "repo_root"
        repository_root = C.find_repo_root(args.repo_root)
        commit, dirty = C.resolve_local_commit(
            repository_root,
            source_commit=args.source_commit,
            allow_dirty=args.allow_dirty_source,
        )
        requested_ref = args.source_commit or str(commit["sha"])
        base_src, surface_src, extra, surface_warnings, warnings = C.load_source_sets_from_root(
            repository_root, B.FILES, B.SURFACE_FILES, EXTRA
        )
    elif args.source_archive:
        source_mode = "source_archive"
        temporary_source, repository_root = C.temporary_archive_root(args.source_archive)
        commit, dirty = C.resolve_local_commit(
            repository_root,
            source_commit=args.source_commit,
            allow_dirty=True,
        )
        requested_ref = args.source_commit or str(commit["sha"])
        base_src, surface_src, extra, surface_warnings, warnings = C.load_source_sets_from_root(
            repository_root, B.FILES, B.SURFACE_FILES, EXTRA
        )
    elif args.cache_only:
        source_mode = "cache_only"
        commit = {
            "sha": args.ref,
            "date": None,
            "message": "cached immutable source set",
            "html_url": None,
        }
        base_src, surface_src, extra, surface_warnings, warnings = C.load_source_sets_from_cache(
            args.repo,
            args.ref,
            B.FILES,
            B.SURFACE_FILES,
            EXTRA,
            B._read_source_cache,
        )
    else:
        commit = B.resolve_commit(args.repo, args.ref, args.api_base, args.token)
        base_src = {
            key: B.fetch_file(args.repo, commit["sha"], path, args.api_base, args.token)
            for key, path in B.FILES.items()
        }
        surface_src, surface_warnings = B.fetch_optional_sources(
            args.repo, commit["sha"], args.api_base, args.token
        )
        extra, warnings = fetch_extra(args.repo, commit["sha"], args.api_base, args.token)
    return (
        temporary_source,
        source_mode,
        dirty,
        requested_ref,
        commit,
        base_src,
        surface_src,
        extra,
        surface_warnings,
        warnings,
    )


def _selected_output(report: dict[str, Any], sections: list[str]) -> Any:
    if not sections:
        return report
    if len(sections) == 1:
        return C.json_pointer_get(report, sections[0])
    return {section: C.json_pointer_get(report, section) for section in sections}


def _render_output(value: Any, output_format: str, full_report: dict[str, Any]) -> tuple[str | None, bytes | None]:
    if output_format == "canonical-json":
        canonical_value = C.canonical_report_view(value) if value is full_report else value
        return None, C.canonical_json_bytes(canonical_value)
    if output_format == "pretty-json":
        return C.pretty_json_text(value), None
    if output_format == "jsonl":
        if value is full_report:
            return C.render_jsonl(full_report), None
        return json.dumps(
            {"record_type": "selection", "value": value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n", None
    if value is not full_report:
        return C.pretty_json_text(value), None
    old, buf = sys.stdout, io.StringIO()
    try:
        sys.stdout = buf
        B.print_text(full_report)
        print_v10(full_report)
        machine = full_report.get("machine_contract") or {}
        print("\n=== 16. V10 MACHINE CONTRACT ===")
        print("format: " + str(full_report.get("format_version")))
        print("schema: " + str(full_report.get("$schema")))
        print("counts: " + json.dumps(machine.get("counts") or {}, sort_keys=True))
        print("diagnostics: " + json.dumps(full_report.get("diagnostic_summary") or {}, sort_keys=True))
        print("integrity: " + str((full_report.get("integrity") or {}).get("payload_sha256")))
    finally:
        sys.stdout = old
    return buf.getvalue(), None


def _fail_on_policy(report: dict[str, Any], policy: str) -> None:
    diagnostics = report.get("diagnostics") or []
    if policy == "never":
        return
    if policy == "warning" and any(d.get("severity") in {"warning", "error"} for d in diagnostics):
        raise AuditError("report contains warning/error diagnostics")
    if policy == "error" and any(d.get("severity") == "error" for d in diagnostics):
        raise AuditError("report contains error diagnostics")
    if policy == "incomplete" and not (report.get("status") or {}).get("complete", False):
        raise AuditError("report status is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --repo-root ../codex --format canonical-json --output report.json\n"
            "  %(prog)s --repo-root ../codex --output-dir out --include-examples\n"
            "  %(prog)s --validate-report report.json --format pretty-json\n"
            "  %(prog)s --cache-only --ref <40-character-commit-sha> --format jsonl"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {C.GENERATOR_VERSION} (report format {C.REPORT_FORMAT_VERSION})",
    )
    parser.add_argument("--repo", default=REPO, help="GitHub repository in owner/name form (default: %(default)s)")
    parser.add_argument("--ref", default=REF, help="Git ref or immutable commit SHA (default: %(default)s)")
    parser.add_argument("--api-base", default=API, help="GitHub API base URL (default: %(default)s)")
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"), help="GitHub token; defaults to GITHUB_TOKEN or GH_TOKEN")
    parser.add_argument("--json", action="store_true", help="compatibility alias for --format pretty-json")
    parser.add_argument(
        "--format",
        choices=("text", "pretty-json", "canonical-json", "jsonl"),
        default="text",
        help="output representation (default: %(default)s)",
    )
    parser.add_argument("--output", help="write the selected representation to this file")
    parser.add_argument("--output-dir", help="write split report, schemas, JSONL entities, and manifest")
    parser.add_argument("--section", action="append", default=[], help="emit a JSON Pointer or top-level section; repeatable")
    parser.add_argument("--emit-schema", metavar="DIRECTORY", help="write report and source-revision-scoped protocol JSON Schemas")
    parser.add_argument("--include-examples", action="store_true", help="include conformance fixtures in --output-dir")
    parser.add_argument("--validate-report", metavar="REPORT_JSON", help="validate an existing v10 report and exit")
    parser.add_argument("--strict", action="store_true", help="fail immediately when legacy extraction records a warning")
    parser.add_argument(
        "--fail-on",
        choices=("never", "warning", "error", "incomplete"),
        default="error",
        help="generation exit policy after structured diagnostics (default: %(default)s)",
    )
    parser.add_argument("--parser", choices=("auto", "regex"), default="auto", help="parser contract requested in the report; this bundle ships the regex/tokenizer backend")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument(
        "--cache-dir",
        default=B.DEFAULT_CACHE_DIR,
        help="immutable-SHA source cache directory (default: %(default)s)",
    )
    cache.add_argument(
        "--no-cache",
        action="store_true",
        help="disable the local immutable-SHA source cache",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--repo-root", help="audit an existing local Codex checkout")
    source.add_argument("--source-archive", help="audit a zip/tar source archive containing codex-rs")
    source.add_argument("--cache-only", action="store_true", help="read an immutable commit entirely from the local source cache")
    parser.add_argument("--source-commit", help="commit SHA for an archive without .git; must match local HEAD when .git exists")
    parser.add_argument("--allow-dirty-source", action="store_true", help="allow tracked local checkout modifications")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="use the resolved commit timestamp as generated_at for stable golden output",
    )
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.json:
        if args.format != "text":
            parser.error("--json cannot be combined with an explicit --format")
        args.format = "pretty-json"
    if args.source_commit and not (args.repo_root or args.source_archive):
        parser.error("--source-commit requires --repo-root or --source-archive")
    if args.allow_dirty_source and not args.repo_root:
        parser.error("--allow-dirty-source requires --repo-root")
    B.configure_cache_dir(None if args.no_cache else args.cache_dir)

    if args.self_test:
        B.self_test()
        self_test()
        contract_self_test()
        return 0

    if args.validate_report:
        report = json.loads(Path(args.validate_report).read_text(encoding="utf-8"))
        diagnostics = C.validate_report(report)
        payload = {"valid": not diagnostics, "diagnostics": diagnostics}
        if args.format == "jsonl":
            rendered = "".join(
                json.dumps(
                    {"record_type": "diagnostic", "value": diagnostic},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
                for diagnostic in diagnostics
            )
            rendered += json.dumps(
                {
                    "record_type": "validation_summary",
                    "valid": not diagnostics,
                    "diagnostic_count": len(diagnostics),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            text, binary = rendered, None
        elif args.format in {"pretty-json", "canonical-json"}:
            text, binary = _render_output(payload, args.format, payload)
        else:
            lines = [
                f"{diagnostic['severity']}: {diagnostic['code']}: {diagnostic['message']}"
                for diagnostic in diagnostics
            ] or ["report validation: ok"]
            text, binary = "\n".join(lines) + "\n", None
        if args.output:
            output_path = Path(args.output)
            if binary is not None:
                output_path.write_bytes(binary)
            else:
                output_path.write_text(text or "", encoding="utf-8")
        elif binary is not None:
            sys.stdout.buffer.write(binary)
        else:
            sys.stdout.write(text or "")
        return 2 if diagnostics else 0

    temporary_source = None
    try:
        (
            temporary_source,
            source_mode,
            dirty,
            requested_ref,
            commit,
            base_src,
            surface_src,
            extra,
            surface_warnings,
            warnings,
        ) = _load_generation_sources(args)

        deterministic = args.deterministic or args.format == "canonical-json"
        report = B.build_report(
            args.repo,
            requested_ref,
            commit,
            base_src,
            strict=args.strict,
            surface_sources=surface_src,
            surface_warnings=surface_warnings,
            deterministic=deterministic,
        )
        report = enhance(report, base_src, extra, warnings, args.strict)
        combined_sources = C.combine_sources(
            base_src,
            B.FILES,
            surface_src,
            B.SURFACE_FILES,
            extra,
            EXTRA,
        )
        report = C.upgrade_report(
            report,
            commit=commit,
            sources=combined_sources,
            source_mode=source_mode,
            repository=args.repo,
            requested_ref=requested_ref,
            dirty=dirty,
            parser_backend=args.parser,
        )
        validation_errors = C.validate_report(report)
        if validation_errors:
            report.setdefault("validation_diagnostics", []).extend(validation_errors)
            raise AuditError(
                "generated v10 report failed validation: "
                + "; ".join(item["message"] for item in validation_errors[:5])
            )
        _fail_on_policy(report, args.fail_on)

        if args.emit_schema:
            schema_root = Path(args.emit_schema)
            C.write_schema_documents(schema_root / "report")
            C.write_protocol_schema_documents(schema_root / "protocol", report)
        if args.output_dir:
            C.write_output_directory(
                args.output_dir,
                report,
                include_fixtures=args.include_examples,
            )

        value = _selected_output(report, args.section)
        text, binary = _render_output(value, args.format, report)
        if args.output:
            output_path = Path(args.output)
            if binary is not None:
                output_path.write_bytes(binary)
            else:
                output_path.write_text(text or "", encoding="utf-8")
        elif not args.output_dir or args.format != "text":
            if binary is not None:
                sys.stdout.buffer.write(binary)
            else:
                sys.stdout.write(text or "")
        return 0
    finally:
        if temporary_source is not None:
            temporary_source.cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AuditError, C.ContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)
