#!/usr/bin/env python3
"""Fetch openai/codex source and audit Codex request/response wire metadata.

The report separates:
- outbound HTTP request headers,
- HTTP response/debug headers,
- flat Responses client_metadata,
- WebSocket handshake headers,
- WebSocket response.create-only metadata,
- /responses/compact, which has a different wire shape,
- account usage, rate-limit/reset-credit, thread usage, and turn-cost endpoints,
- model discovery and the three-stage hosted-file upload flow,
- static and templated response/debug/rate-limit/safety metadata,
- server-owned continuation values such as response.id / x-codex-turn-state,
- reusable header/client_metadata/request-body/event profiles and normalized HTTP↔WSS deltas,
- endpoint-module and selected response-header drift coverage with explicit exclusions.

One GitHub ref is resolved to an immutable SHA before files are fetched, so a
report never mixes revisions while main is moving. Standard-library only.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPO = "openai/codex"
REF = "main"
API = "https://api.github.com"

GENERATOR_VERSION = "4.0.0"
REPORT_SCHEMA_VERSION = 10
CATALOG_SCHEMA_VERSION = 10
MODEL_MATRIX_SCHEMA_VERSION = 7
GITHUB_MAX_ATTEMPTS = 3
DEFAULT_CACHE_DIR = os.getenv("CODEX_WIRE_AUDIT_CACHE_DIR") or str(
    Path.home() / ".cache" / "codex-wire-audit"
)
_CACHE_DIR: Path | None = Path(DEFAULT_CACHE_DIR).expanduser()

FILES = {
    "account": "codex-rs/backend-client/src/client.rs",
    "default_client": "codex-rs/login/src/auth/default_client.rs",
    "auth": "codex-rs/model-provider/src/bearer_auth_provider.rs",
    "provider_info": "codex-rs/model-provider-info/src/lib.rs",
    "core": "codex-rs/core/src/client.rs",
    "metadata": "codex-rs/core/src/responses_metadata.rs",
    "common": "codex-rs/codex-api/src/common.rs",
    "headers": "codex-rs/codex-api/src/requests/headers.rs",
    "http": "codex-rs/codex-api/src/endpoint/responses.rs",
    "compact": "codex-rs/codex-api/src/endpoint/compact.rs",
    "response_sse": "codex-rs/codex-api/src/sse/responses.rs",
    "ws": "codex-rs/codex-api/src/endpoint/responses_websocket.rs",
    "provider": "codex-rs/codex-api/src/provider.rs",
    "request": "codex-rs/http-client/src/request.rs",
    "attestation": "codex-rs/core/src/attestation.rs",
    "inference": "codex-rs/rollout-trace/src/inference.rs",
}

# Optional sources broaden the audit beyond the core Responses path.  They are
# intentionally fetched best-effort so an upstream file move does not make the
# stable Responses/metadata audit unusable.
SURFACE_FILES = {
    "images_endpoint": "codex-rs/codex-api/src/endpoint/images.rs",
    "images_types": "codex-rs/codex-api/src/images.rs",
    "search_endpoint": "codex-rs/codex-api/src/endpoint/search.rs",
    "search_types": "codex-rs/codex-api/src/search.rs",
    "memories_endpoint": "codex-rs/codex-api/src/endpoint/memories.rs",
    "realtime_call": "codex-rs/codex-api/src/endpoint/realtime_call.rs",
    "realtime_ws": "codex-rs/codex-api/src/endpoint/realtime_websocket/methods.rs",
    "realtime_protocol": "codex-rs/codex-api/src/endpoint/realtime_websocket/protocol.rs",
    "guardian_sampler": "codex-rs/ext/guardian-v2/src/async_scorer/sampler.rs",
    "guardian_feature_config": "codex-rs/features/src/feature_configs.rs",
    "rate_limits": "codex-rs/backend-client/src/client/rate_limit_resets.rs",
    "backend_types": "codex-rs/backend-client/src/types.rs",
    "thread_usage": "codex-rs/backend-client/src/client/thread_usage.rs",
    # v10: provider discovery, hosted-file transfer, response diagnostics, and cost APIs.
    "endpoint_mod": "codex-rs/codex-api/src/endpoint/mod.rs",
    "models_endpoint": "codex-rs/codex-api/src/endpoint/models.rs",
    "models_protocol": "codex-rs/protocol/src/openai_models.rs",
    "model_provider_models": "codex-rs/model-provider/src/models_endpoint.rs",
    "files_api": "codex-rs/codex-api/src/files.rs",
    "mcp_openai_file": "codex-rs/core/src/mcp_openai_file.rs",
    "response_rate_limits": "codex-rs/codex-api/src/rate_limits.rs",
    "safety_buffering": "codex-rs/codex-api/src/safety_buffering.rs",
    "api_bridge": "codex-rs/codex-api/src/api_bridge.rs",
    "response_debug_context": "codex-rs/response-debug-context/src/lib.rs",
    "guardian_ticket": "codex-rs/codex-api/src/guardian_ticket.rs",
    "chatgpt_turn_cost": "codex-rs/backend-client/src/client/chatgpt_turn_cost.rs",
    "api_key_turn_cost": "codex-rs/backend-client/src/client/turn_usage.rs",
}

STANDARD_CONSTS = {
    "USER_AGENT": "User-Agent",
    "AUTHORIZATION": "Authorization",
    "CONTENT_TYPE": "Content-Type",
    "CONTENT_ENCODING": "Content-Encoding",
    "ACCEPT": "Accept",
}

# Backend account/control requests use a deliberately smaller header profile than
# model inference. Keep this explicit so /usage and reset-credit calls cannot
# accidentally inherit Responses turn/routing metadata from another report path.
BACKEND_ACCOUNT_HEADER_NAMES = {
    "user-agent",
    "authorization",
    "chatgpt-account-id",
    "x-openai-fedramp",
}

INFERENCE_ONLY_HEADER_NAMES = {
    "session-id",
    "thread-id",
    "x-client-request-id",
    "x-codex-installation-id",
    "x-codex-window-id",
    "x-codex-turn-metadata",
    "x-codex-parent-thread-id",
    "x-codex-turn-state",
    "x-codex-routing-hint",
    "x-codex-beta-features",
    "x-openai-subagent",
    "x-openai-internal-codex-responses-lite",
    "x-oai-attestation",
    "x-openai-memgen-request",
    "x-responsesapi-include-timing-metrics",
}

CORE_BASE_CLIENT_METADATA = {
    "x-codex-installation-id",
    "session_id",
    "thread_id",
    "x-codex-window-id",
}

KNOWN_CONDITIONAL_CLIENT_METADATA = {
    "turn_id",
    "x-openai-subagent",
    "x-codex-parent-thread-id",
    "parent_turn_id",
    "root_turn_id",
    "x-codex-turn-metadata",
    # Guardian ticket lifecycle: attached at the transport boundary
    # (core client.rs set_guardian_ticket_request; codex-api
    # guardian_ticket::attach) — outside the builder block, so listed here
    # as dynamic known-conditional keys (see DYNAMIC_CLIENT_METADATA).
    "guardian_ticket_requested",
    "guardian_ticket",
}

CLIENT_METADATA_CONDITIONS = {
    "x-codex-installation-id": "always in CodexResponsesMetadata::client_metadata",
    "session_id": "always in CodexResponsesMetadata::client_metadata",
    "thread_id": "always in CodexResponsesMetadata::client_metadata",
    "x-codex-window-id": "always in CodexResponsesMetadata::client_metadata",
    "turn_id": "when turn_id exists",
    "x-openai-subagent": "when subagent_header exists",
    "x-codex-parent-thread-id": "when parent_thread_id exists",
    "parent_turn_id": "when parent_turn_id exists",
    "root_turn_id": "when root_turn_id exists",
    "x-codex-turn-metadata": "when request_kind exists and metadata serializes",
}

WS_METADATA_CONDITIONS = {
    "ws_request_header_traceparent": "when W3C traceparent exists",
    "ws_request_header_tracestate": "when W3C tracestate exists",
    "ws_request_header_x_openai_internal_codex_responses_lite": "Responses Lite only",
    "x-codex-turn-state": "after server supplied turn state for this turn",
    "x-codex-ws-stream-request-start-ms": "always on each response.create just before send",
}


class AuditError(RuntimeError):
    pass


def configure_cache_dir(path: str | os.PathLike[str] | None) -> None:
    """Configure the immutable-SHA source cache; None disables it."""
    global _CACHE_DIR
    _CACHE_DIR = Path(path).expanduser() if path is not None else None


def _source_cache_path(repo: str, sha: str, path: str) -> Path | None:
    if _CACHE_DIR is None:
        return None
    digest = hashlib.sha256(f"{repo}\0{sha}\0{path}".encode("utf-8")).hexdigest()
    return _CACHE_DIR / "source-v1" / digest[:2] / f"{digest}.rs"


def _read_source_cache(repo: str, sha: str, path: str) -> str | None:
    cache_path = _source_cache_path(repo, sha, path)
    if cache_path is None:
        return None
    try:
        return cache_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _write_source_cache(repo: str, sha: str, path: str, content: str) -> None:
    cache_path = _source_cache_path(repo, sha, path)
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(cache_path)
    except OSError:
        # Caching is an optimization; read-only homes and sandbox policies must
        # not make the audit fail.
        return


def _github_headers(token: str | None, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": f"codex-wire-audit/{GENERATOR_VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    retry_after = error.headers.get("Retry-After") if error.headers else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 30.0)
        except ValueError:
            pass
    reset_at = error.headers.get("X-RateLimit-Reset") if error.headers else None
    if reset_at:
        try:
            return min(max(float(reset_at) - time.time(), 0.0), 30.0)
        except ValueError:
            pass
    return min(0.5 * (2 ** attempt), 4.0)


def _github_open(url: str, token: str | None, *, accept: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(GITHUB_MAX_ATTEMPTS):
        req = urllib.request.Request(url, headers=_github_headers(token, accept=accept))
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            last_error = error
            rate_limited = (
                error.code == 403
                and error.headers is not None
                and (
                    error.headers.get("Retry-After") is not None
                    or error.headers.get("X-RateLimit-Remaining") == "0"
                )
            )
            retryable = error.code == 429 or rate_limited or 500 <= error.code < 600
            if retryable and attempt + 1 < GITHUB_MAX_ATTEMPTS:
                time.sleep(_retry_delay(error, attempt))
                continue
            detail = error.read().decode("utf-8", "replace")[:1000]
            raise AuditError(f"GitHub HTTP {error.code}: {url}: {detail}") from error
        except urllib.error.URLError as error:
            last_error = error
            if attempt + 1 < GITHUB_MAX_ATTEMPTS:
                time.sleep(min(0.5 * (2 ** attempt), 4.0))
                continue
            raise AuditError(f"GitHub request failed: {url}: {error}") from error
    raise AuditError(f"GitHub request failed after retries: {url}: {last_error}")


def gh_json(url: str, token: str | None) -> Any:
    raw = _github_open(url, token, accept="application/vnd.github+json")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AuditError(f"invalid GitHub JSON response: {url}: {error}") from error


def gh_text(url: str, token: str | None) -> str:
    raw = _github_open(url, token, accept="application/vnd.github.raw+json")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AuditError(f"GitHub file is not UTF-8 text: {url}: {error}") from error


def resolve_commit(repo: str, ref: str, api: str, token: str | None) -> dict[str, Any]:
    url = f"{api.rstrip('/')}/repos/{repo}/commits/{urllib.parse.quote(ref, safe='')}"
    d = gh_json(url, token)
    commit = d.get("commit") or {}
    return {
        "sha": d["sha"],
        "date": (commit.get("committer") or {}).get("date"),
        "message": (commit.get("message") or "").splitlines()[0],
        "html_url": d.get("html_url"),
    }


def fetch_file(repo: str, sha: str, path: str, api: str, token: str | None) -> str:
    cached = _read_source_cache(repo, sha, path)
    if cached is not None:
        return cached

    def decoded(payload: dict[str, Any], source_kind: str) -> str | None:
        if payload.get("encoding") != "base64" or "content" not in payload:
            return None
        try:
            return base64.b64decode(payload["content"]).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise AuditError(f"failed to decode GitHub {source_kind} for {path}: {error}") from error

    quoted_path = urllib.parse.quote(path, safe="/")
    url = (
        f"{api.rstrip('/')}/repos/{repo}/contents/{quoted_path}"
        f"?ref={urllib.parse.quote(sha, safe='')}"
    )
    payload = gh_json(url, token)
    content = decoded(payload, "contents")
    if content is None:
        # The Contents API can omit inline content for larger files. Prefer the
        # immutable blob URL, then the raw download URL.
        blob_sha = payload.get("sha")
        if blob_sha:
            blob_url = f"{api.rstrip('/')}/repos/{repo}/git/blobs/{blob_sha}"
            content = decoded(gh_json(blob_url, token), "blob")
        if content is None and payload.get("download_url"):
            content = gh_text(str(payload["download_url"]), token)
    if content is None:
        raise AuditError(f"unexpected contents response for {path}")
    _write_source_cache(repo, sha, path, content)
    return content


def fetch_optional_sources(
    repo: str,
    sha: str,
    api: str,
    token: str | None,
    files: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Fetch non-core wire surfaces without making the base audit brittle.

    Optional sources are used for the endpoint/protocol catalog (images, search,
    memories, realtime, Guardian classifier, account usage, and reset credits).  Missing or
    moved files become warnings instead of aborting the core Responses audit.
    """
    out: dict[str, str] = {}
    warnings: list[str] = []
    for key, path in (files or SURFACE_FILES).items():
        try:
            out[key] = fetch_file(repo, sha, path, api, token)
        except AuditError as exc:
            warnings.append(f"optional wire surface unavailable: {path}: {exc}")
    return out, warnings


def line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, max(offset, 0)) + 1


def src(
    text: str,
    path: str,
    needle: str,
    symbol: str | None = None,
    start: int = 0,
) -> dict[str, Any]:
    i = text.find(needle, start)
    if i < 0:
        raise AuditError(f"expected source anchor missing: {path}: {needle}")
    return {"path": path, "line": line_no(text, i), "symbol": symbol or needle}


def consts(sources: dict[str, str]) -> dict[str, str]:
    out = dict(STANDARD_CONSTS)
    rx = re.compile(
        r'(?ms)^\s*(?:pub(?:\([^)]*\))?\s+)?const\s+([A-Z][A-Z0-9_]*)\s*:\s*&str\s*=\s*"([^"]+)"\s*;'
    )
    for text in sources.values():
        out.update(rx.findall(text))
    return out


def const_definitions(sources: dict[str, str]) -> list[tuple[str, str]]:
    rx = re.compile(
        r'(?ms)^\s*(?:pub(?:\([^)]*\))?\s+)?const\s+([A-Z][A-Z0-9_]*)\s*:\s*&str\s*=\s*"([^"]+)"\s*;'
    )
    out: list[tuple[str, str]] = []
    for text in sources.values():
        out.extend(rx.findall(text))
    return out


def find_rust_matching_brace(text: str, open_index: int) -> int:
    """Find a Rust block's closing brace while ignoring comments and literals."""
    if open_index < 0 or open_index >= len(text) or text[open_index] != "{":
        raise AuditError("invalid Rust block opening brace")
    depth = 1
    index = open_index + 1
    line_comment = False
    block_comment_depth = 0
    string_quote: str | None = None
    raw_terminator: str | None = None
    escaped = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment_depth:
            if char == "/" and nxt == "*":
                block_comment_depth += 1
                index += 2
            elif char == "*" and nxt == "/":
                block_comment_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if raw_terminator is not None:
            if text.startswith(raw_terminator, index):
                index += len(raw_terminator)
                raw_terminator = None
            else:
                index += 1
            continue
        if string_quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == string_quote:
                string_quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment_depth = 1
            index += 2
            continue

        raw = re.match(r'(?:br|r)(?P<hashes>#{0,16})"', text[index:])
        if raw:
            hashes = raw.group("hashes")
            raw_terminator = '"' + hashes
            index += raw.end()
            continue
        if char == "b" and nxt == '"':
            string_quote = '"'
            index += 2
            continue
        if char == '"':
            string_quote = '"'
            index += 1
            continue
        if char == "'":
            # Do not confuse Rust lifetimes with character literals.
            probe = index + 1
            probe_escaped = False
            while probe < len(text) and text[probe] != "\n" and probe - index <= 16:
                if probe_escaped:
                    probe_escaped = False
                elif text[probe] == "\\":
                    probe_escaped = True
                elif text[probe] == "'":
                    string_quote = "'"
                    break
                probe += 1
            index += 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise AuditError("Rust block closing brace not found")


def struct_body(text: str, name: str) -> tuple[str, int]:
    m = re.search(
        rf"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+{re.escape(name)}\b[^{{]*\{{",
        text,
    )
    if not m:
        raise AuditError(f"struct not found: {name}")
    start = m.end()
    close = find_rust_matching_brace(text, start - 1)
    return text[start:close], start


def serde_field_properties(field_type: str, attrs: list[str], name: str) -> dict[str, Any]:
    serde = " ".join(attrs)
    rename = re.search(r'\brename\s*=\s*"([^"]+)"', serde)
    skip_if = re.search(r'\bskip_serializing_if\s*=\s*"([^"]+)"', serde)
    skipped = bool(re.search(r'\bskip(?:\s*[,\)])|\bskip_serializing(?:\s*[,\)])', serde))
    return {
        "wire_name": rename.group(1) if rename else name,
        "optional": bool(re.search(r"\bOption\s*<", field_type)),
        "serde_default": bool(re.search(r"(?:\(|,)\s*default(?:\s*[,\)])", serde)),
        "skip_serializing_if": skip_if.group(1) if skip_if else None,
        "skipped": skipped,
        "serialized": not skipped,
    }


def _split_rust_top_level_commas(text: str) -> list[tuple[str, int]]:
    """Split a Rust declaration body on commas outside nested type/attribute syntax."""
    items: list[tuple[str, int]] = []
    start = 0
    index = 0
    paren = bracket = brace = angle = 0
    line_comment = False
    block_comment_depth = 0
    quote: str | None = None
    raw_terminator: str | None = None
    escaped = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment_depth:
            if char == "/" and nxt == "*":
                block_comment_depth += 1
                index += 2
            elif char == "*" and nxt == "/":
                block_comment_depth -= 1
                index += 2
            else:
                index += 1
            continue
        if raw_terminator is not None:
            if text.startswith(raw_terminator, index):
                index += len(raw_terminator)
                raw_terminator = None
            else:
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment_depth = 1
            index += 2
            continue
        raw = re.match(r'(?:br|r)(?P<hashes>#{0,16})"', text[index:])
        if raw:
            raw_terminator = '"' + raw.group("hashes")
            index += raw.end()
            continue
        if char == "b" and nxt == '"':
            quote = '"'
            index += 2
            continue
        if char == '"':
            quote = '"'
            index += 1
            continue
        if char == "'":
            probe = index + 1
            probe_escaped = False
            while probe < len(text) and text[probe] != "\n" and probe - index <= 16:
                if probe_escaped:
                    probe_escaped = False
                elif text[probe] == "\\":
                    probe_escaped = True
                elif text[probe] == "'":
                    quote = "'"
                    break
                probe += 1
            index += 1
            continue
        if char == "(":
            paren += 1
        elif char == ")" and paren:
            paren -= 1
        elif char == "[":
            bracket += 1
        elif char == "]" and bracket:
            bracket -= 1
        elif char == "{":
            brace += 1
        elif char == "}" and brace:
            brace -= 1
        elif char == "<":
            angle += 1
        elif char == ">" and angle:
            angle -= 1
        elif char == "," and not any((paren, bracket, brace, angle)):
            items.append((text[start:index], start))
            start = index + 1
        index += 1
    if text[start:].strip():
        items.append((text[start:], start))
    return items


def _strip_leading_rust_comments(raw: str) -> tuple[str, int]:
    """Remove leading doc/comments while preserving the offset of the declaration."""
    consumed = 0
    while True:
        match = re.match(r"\s+", raw[consumed:])
        if match:
            consumed += match.end()
        if raw.startswith("//", consumed):
            newline = raw.find("\n", consumed)
            if newline < 0:
                return "", len(raw)
            consumed = newline + 1
            continue
        if raw.startswith("/*", consumed):
            close = raw.find("*/", consumed + 2)
            if close < 0:
                return "", len(raw)
            consumed = close + 2
            continue
        return raw[consumed:], consumed


def struct_fields(text: str, path: str, name: str) -> list[dict[str, Any]]:
    """Extract named Rust struct fields, including multiline attributes and types."""
    body, offset = struct_body(text, name)
    fields: list[dict[str, Any]] = []
    for raw_item, item_offset in _split_rust_top_level_commas(body):
        declaration, leading_offset = _strip_leading_rust_comments(raw_item)
        attrs: list[str] = []
        attr_consumed = 0
        while True:
            match = re.match(r"\s*(#\[[\s\S]*?\])", declaration[attr_consumed:])
            if not match:
                break
            attrs.append(match.group(1).strip())
            attr_consumed += match.end()
        declaration = declaration[attr_consumed:].strip()
        if not declaration:
            continue
        field_match = re.match(
            r"(?s)^(?:pub(?:\([^)]*\))?\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.+?)\s*$",
            declaration,
        )
        if not field_match:
            continue
        field_name = field_match.group(1)
        field_type = " ".join(field_match.group(2).split())
        declaration_start = raw_item.find(declaration)
        if declaration_start < 0:
            declaration_start = leading_offset + attr_consumed
        item = {
            "name": field_name,
            "type": field_type,
            "serde": list(attrs),
            "flattened": any("flatten" in attribute for attribute in attrs),
            "source": {
                "path": path,
                "line": line_no(text, offset + item_offset + declaration_start),
                "symbol": name,
            },
        }
        item.update(serde_field_properties(field_type, attrs, field_name))
        fields.append(item)
    return fields


def key_tokens(block: str, cmap: dict[str, str]) -> set[str]:
    names: set[str] = set()
    for token in re.findall(r"\b([A-Z][A-Z0-9_]*)\.to_string\(\)", block):
        if token in cmap:
            names.add(cmap[token])
    for literal in re.findall(r'"([A-Za-z0-9_.-]+)"\.to_string\(\)', block):
        if literal != "true":
            names.add(literal)
    return names


def client_metadata_keys(
    metadata: str,
    core: str,
    common: str,
    *,
    strict: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    cmap = consts({"metadata": metadata, "core": core, "common": common})
    a = metadata.find("fn client_metadata")
    b = metadata.find("fn compatibility_headers", a)
    if a < 0 or b < 0:
        raise AuditError("CodexResponsesMetadata client_metadata builder not found")
    block = metadata[a:b]

    initial_match = re.search(r"HashMap::from\(\[(.*?)\]\s*\)", block, re.S)
    initial = key_tokens(initial_match.group(1), cmap) if initial_match else set()
    names = key_tokens(block, cmap)

    missing_base = sorted(CORE_BASE_CLIENT_METADATA - names)
    if missing_base:
        raise AuditError(
            "required base client_metadata keys missing upstream: " + ", ".join(missing_base)
        )

    warnings: list[str] = []
    missing_conditional = sorted(KNOWN_CONDITIONAL_CLIENT_METADATA - names)
    if missing_conditional:
        warnings.append(
            "known conditional client_metadata keys absent upstream: "
            + ", ".join(missing_conditional)
        )
    if strict and warnings:
        raise AuditError("; ".join(warnings))

    reverse = {value: token for token, value in cmap.items()}

    def metadata_key_source(key: str) -> dict[str, Any]:
        token = reverse.get(key)
        local = block.find(token) if token else -1
        if local < 0:
            local = block.find(f'"{key}"')
        if local < 0:
            raise AuditError(f"no source anchor for client_metadata key: {key}")
        return {
            "path": FILES["metadata"],
            "line": line_no(metadata, a + local),
            "symbol": "CodexResponsesMetadata::client_metadata",
        }

    base = []
    for key in sorted(names):
        required = key in initial or key in CORE_BASE_CLIENT_METADATA
        base.append(
            {
                "name": key,
                "optional": not required,
                "condition": CLIENT_METADATA_CONDITIONS.get(
                    key, "always" if required else "conditional insertion in client_metadata()"
                ),
                "direction": "request",
                "origin": "client",
                "continuation": False,
                "source": metadata_key_source(key),
            }
        )

    combined = core + "\n" + common
    definitions = const_definitions({"core": core, "common": common})
    ws_tokens = {
        token: value
        for token, value in definitions
        if token.endswith("CLIENT_METADATA_KEY")
        and len(re.findall(rf"\b{re.escape(token)}\b", combined)) >= 2
    }
    ws: list[dict[str, Any]] = []
    for token, key in sorted(ws_tokens.items(), key=lambda item: item[1]):
        text, path = (core, FILES["core"]) if token in core else (common, FILES["common"])
        ws.append(
            {
                "name": key,
                "optional": key != "x-codex-ws-stream-request-start-ms",
                "condition": WS_METADATA_CONDITIONS.get(key, "WS response.create metadata"),
                "direction": "request",
                "origin": "client_trace" if "trace" in key else "client",
                "continuation": False,
                "source": src(text, path, token, "WS response.create client_metadata"),
            }
        )
    if "client_metadata.insert(X_CODEX_TURN_STATE_HEADER" in core:
        ws.append(
            {
                "name": "x-codex-turn-state",
                "optional": True,
                "condition": WS_METADATA_CONDITIONS["x-codex-turn-state"],
                "direction": "request",
                "origin": "server_derived",
                "continuation": True,
                "source": src(
                    core,
                    FILES["core"],
                    "client_metadata.insert(X_CODEX_TURN_STATE_HEADER",
                    "WS response.create client_metadata",
                ),
            }
        )
    ws.sort(key=lambda x: x["name"])
    return base, ws, warnings


def header(
    name: str,
    condition: str,
    text: str,
    path: str,
    anchor: str,
    value: str | None = None,
    layer: str = "codex",
    *,
    optional: bool = True,
    category: str = "protocol",
    direction: str = "request",
    origin: str = "client",
    continuation: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "condition": condition,
        "optional": optional,
        "value_shape": value,
        "layer": layer,
        "category": category,
        "direction": direction,
        "origin": origin,
        "continuation": continuation,
        "source": src(text, path, anchor, name),
    }


def response_header(
    name: str,
    condition: str,
    text: str,
    path: str,
    anchor: str,
    *,
    value: str | None = None,
    category: str = "response",
    continuation: bool = False,
    origin: str = "server",
) -> dict[str, Any]:
    return header(
        name,
        condition,
        text,
        path,
        anchor,
        value,
        "response",
        optional=True,
        category=category,
        direction="response",
        origin=origin,
        continuation=continuation,
    )


def builtin_openai_provider_headers(provider_info: str) -> list[dict[str, Any]]:
    return [
        header(
            "version",
            "built-in OpenAI provider",
            provider_info,
            FILES["provider_info"],
            '"version".to_string()',
            "<Codex CARGO_PKG_VERSION>",
            "provider",
            optional=False,
            category="provider",
            origin="provider_config",
        ),
        header(
            "OpenAI-Organization",
            "when OPENAI_ORGANIZATION is set",
            provider_info,
            FILES["provider_info"],
            '"OpenAI-Organization".to_string()',
            "$OPENAI_ORGANIZATION",
            "provider",
            category="provider",
            origin="provider_config",
        ),
        header(
            "OpenAI-Project",
            "when OPENAI_PROJECT is set",
            provider_info,
            FILES["provider_info"],
            '"OpenAI-Project".to_string()',
            "$OPENAI_PROJECT",
            "provider",
            category="provider",
            origin="provider_config",
        ),
    ]


def discover_header_candidates(sources: dict[str, str]) -> list[str]:
    """Discover request headers without treating generic map inserts as headers.

    This is deliberately source-scoped, not a claim that every possible provider,
    auth implementation, proxy, or runtime-generated transport header is enumerated.
    """
    cmap = consts(sources)
    found: set[str] = set()
    literal_patterns = [
        r'insert_header\([^,]+,\s*"([A-Za-z0-9_-]+)"',
        r'\.(?:header)\(\s*"([A-Za-z0-9_-]+)"',
        r'\b(?:headers|extra_headers|default_headers|provider_headers)\.insert\(\s*"([A-Za-z0-9_-]+)"',
    ]
    token_patterns = [
        r'insert_header\([^,]+,\s*(?:http::header::)?([A-Z][A-Z0-9_]*)',
        r'\b(?:headers|extra_headers|default_headers|provider_headers)\.insert\(\s*(?:http::header::)?([A-Z][A-Z0-9_]*)',
        r'HeaderName::from_static\(\s*([A-Z][A-Z0-9_]*)',
    ]
    for text in sources.values():
        for pattern in literal_patterns:
            found.update(re.findall(pattern, text))
        for pattern in token_patterns:
            for token in re.findall(pattern, text):
                if token in cmap:
                    found.add(cmap[token])
    return sorted(found, key=str.lower)



def discover_response_header_candidates(sources: dict[str, str]) -> list[str]:
    """Discover static response-header names read by selected response consumers."""
    cmap = consts(sources)
    found: set[str] = set()
    literal_patterns = [
        r'\b(?:headers|response_headers)\.(?:get|contains_key)\(\s*"([A-Za-z0-9_-]+)"',
        r'\b(?:parse_header_str|extract_header|upload_response_header)\([^\n,]+,\s*"([A-Za-z0-9_-]+)"',
        r'\.headers\(\)\.get\(\s*"([A-Za-z0-9_-]+)"',
    ]
    token_patterns = [
        r'\b(?:headers|response_headers)\.(?:get|contains_key)\(\s*(?:http::header::)?([A-Z][A-Z0-9_]*)',
        r'\b(?:parse_header_str|extract_header)\([^\n,]+,\s*(?:http::header::)?([A-Z][A-Z0-9_]*)',
        r'\.headers\(\)\.get\(\s*(?:http::header::)?([A-Z][A-Z0-9_]*)',
    ]
    for text in sources.values():
        for pattern in literal_patterns:
            found.update(re.findall(pattern, text))
        for pattern in token_patterns:
            for token in re.findall(pattern, text):
                if token in cmap:
                    found.add(cmap[token])
    return sorted(found, key=str.lower)


def discover_response_event_kinds(text: str) -> list[str]:
    """Return application event kinds explicitly matched or compared by the parser."""
    kinds = set(re.findall(r'"((?:response|codex)\.[A-Za-z0-9_.-]+)"', text))
    return sorted(kinds)


def _fn_body(text: str, anchor: str) -> str:
    """Brace-matched body of the function whose signature contains `anchor`."""
    start = text.find(anchor)
    if start < 0:
        return ""
    open_idx = text.index("{", start)
    try:
        close_idx = find_rust_matching_brace(text, open_idx)
    except AuditError:
        return ""
    return text[open_idx : close_idx + 1]


def _split_struct_entries(body: str) -> list[tuple[str, str]]:
    """Split `Field: expr,` entries of a struct literal on top-level commas.

    Comment- and string-aware: `//` line comments are skipped (a trailing
    comment comma must not terminate an entry) and double-quoted string
    literals are honored so `//` inside a string is not mistaken for a comment.
    """
    open_idx = body.index("{")
    entries: list[tuple[str, str]] = []
    depth = 0
    cur: list[str] = []
    name = None
    in_string = False
    in_line_comment = False
    i = open_idx + 1
    end = len(body) - 1
    while i < end:
        ch = body[i]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_string:
            cur.append(ch)
            if ch == "\\" and i + 1 < end:
                cur.append(body[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == "/" and i + 1 < end and body[i + 1] == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == '"':
            in_string = True
            cur.append(ch)
            i += 1
            continue
        if depth == 0 and ch == ":" and name is None:
            # The pre-colon text may carry preceding comments inside the
            # literal; the field name is its trailing identifier.
            m = re.search(r"([A-Za-z_]\w*)\s*$", "".join(cur))
            name = m.group(1) if m else "".join(cur).strip()
            cur = []
            i += 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if depth == 0 and ch == ",":
            # Shorthand entries (`compaction,` — no colon) name themselves.
            trailing = re.search(r"([A-Za-z_]\w*)\s*$", "".join(cur))
            resolved = name or (trailing.group(1) if trailing else None)
            if resolved is not None:
                entries.append((resolved, "".join(cur).strip()))
            name, cur = None, []
            i += 1
            continue
        cur.append(ch)
        i += 1
    if name is not None:
        entries.append((name, "".join(cur).strip()))
    return entries


def turn_metadata_construction(metadata: str) -> dict[str, Any]:
    """How CodexResponsesMetadata composes the turn-metadata payload.

    Derived from `CodexResponsesMetadata::turn_metadata_payload` — the
    CONSTRUCTION function, not just the struct shape: per-field emission
    gates, the request-kind derivation of request_kind/compaction, the
    non-empty workspaces rule, the flattened-extra validation limits, and
    the SessionSource -> subagent header mapping.
    """
    body = _fn_body(metadata, "fn turn_metadata_payload(&self)")
    literal = _fn_body(body, "CodexTurnMetadataPayload {") if body else ""

    gate_rules = {
        "has_turn_identity": (
            "emitted when has_turn_identity — request_kind is None (composer/"
            "user turns) OR the kind carries turn identity"
        ),
        "has_request_identity": (
            "emitted only when has_request_identity — a request_kind exists AND "
            "it carries turn identity (Memory-kind frames suppress these)"
        ),
        "non_empty": "emitted only when the workspaces map is non-empty",
        "request_kind_derived": (
            "derived from CodexResponsesRequestKind::metadata() — compaction is "
            "Some only for the Compact kind"
        ),
        "flattened_extra": (
            "flattened extra metadata map (responses_api_metadata): validated + "
            "reserved-key filtered before reaching this struct"
        ),
    }

    warnings: list[str] = []
    fields: list[dict[str, Any]] = []
    if literal:
        for name, expr in _split_struct_entries(literal):
            if name == "extra" or "self.extra" in expr:
                rule = gate_rules["flattened_extra"]
            elif "non_empty_workspaces" in expr:
                rule = gate_rules["non_empty"]
            elif name == "request_kind" or name == "compaction":
                rule = gate_rules["request_kind_derived"]
            elif "has_request_identity" in expr:
                rule = gate_rules["has_request_identity"]
            elif "has_turn_identity" in expr:
                rule = gate_rules["has_turn_identity"]
            else:
                rule = "pass-through of the runtime field (Option none-suppressed)"
            fields.append(
                {
                    "name": name,
                    "construction_rule": rule,
                    "expression": " ".join(expr.split()),
                    "source": src(
                        metadata,
                        FILES["metadata"],
                        f"{name}: " if f"{name}: " in metadata else f"{name},",
                        name,
                    ),
                }
            )
    else:
        warnings.append(
            "turn_metadata_payload construction body not found in fetched "
            "responses_metadata.rs — construction section is empty for this revision"
        )

    # Flattened-extra validation contract (validate_extra_metadata + consts).
    limits: dict[str, Any] = {}
    for const in (
        "MAX_EXTRA_METADATA_ENTRIES",
        "MAX_EXTRA_METADATA_KEY_BYTES",
        "MAX_EXTRA_METADATA_VALUE_BYTES",
    ):
        m = re.search(rf"const {const}: usize = (\d+);", metadata)
        if m:
            limits[const] = int(m.group(1))
        else:
            warnings.append(f"extra-metadata limit {const} not found")

    def _const_list(name: str) -> list[str]:
        m = re.search(rf"{name}[^=]*=\s*&?\[(.*?)\]", metadata, re.S)
        if not m:
            return []
        # List elements may be string literals OR identifiers of string consts
        # (e.g. INSTALLATION_ID_KEY) — resolve the identifiers in-file.
        out: list[str] = []
        for tok in re.findall(r'"([^"]+)"|([A-Za-z_]\w*)', m.group(1)):
            literal, ident = tok
            if literal:
                out.append(literal)
            else:
                dm = re.search(rf'const {ident}: &str = "([^"]*)"', metadata)
                out.append(dm.group(1) if dm else ident)
        return out

    reserved = _const_list("RESERVED_METADATA_KEYS")
    if not reserved:
        warnings.append("RESERVED_METADATA_KEYS not found")

    return {
        "construction_fn": "CodexResponsesMetadata::turn_metadata_payload",
        "present": bool(literal),
        "identity_gates": {
            "has_turn_identity": gate_rules["has_turn_identity"],
            "has_request_identity": gate_rules["has_request_identity"],
        },
        "fields": fields,
        "workspaces_rule": gate_rules["non_empty"],
        "subagent_header_value_mapping": {
            "SubAgentSource::Review": "review",
            "SubAgentSource::Compact": "compact",
            "SubAgentSource::MemoryConsolidation": "memory_consolidation",
            "SubAgentSource::ThreadSpawn": "collab_spawn",
            "SubAgentSource::Other(label)": "label verbatim",
            "SessionSource::Internal(source)": "source.to_string()",
            "Cli/VSCode/Exec/Mcp/Custom/Unknown": None,
        },
        "extra_metadata_limits": limits,
        "extra_metadata_reserved_keys": reserved,
        "extra_metadata_key_charset": (
            "ASCII identifier: leading alpha, then alphanumeric/_/./-"
        ),
        "warnings": warnings,
    }



# WS "virtual frame" (response.create client_metadata) -> HTTP header map.
# Derived from CodexResponsesMetadata::client_metadata() + the endpoint
# construction (responses_metadata.rs:303-340, common.rs:360-378,
# client.rs responses/compact/ws paths). None = no HTTP counterpart.
VIRTUAL_FRAME_HTTP_COUNTERPARTS = {
    "x-codex-installation-id": (
        "x-codex-installation-id",
        "HTTP header projection only on /responses/compact; on /responses the key rides the body client_metadata",
    ),
    "session_id": ("session-id", "build_session_headers — identity headers are NON-x- on the HTTP transport"),
    "thread_id": ("thread-id", "build_session_headers"),
    "x-codex-window-id": ("x-codex-window-id", "compatibility projection"),
    "turn_id": (None, "turn identity lives in the turn-metadata payload; the HTTP transport sends no turn-id header"),
    "x-openai-subagent": ("x-openai-subagent", "compatibility projection"),
    "x-codex-parent-thread-id": ("x-codex-parent-thread-id", "compatibility projection"),
    "parent_turn_id": (None, "turn-level coordination field; no HTTP header projection"),
    "root_turn_id": (None, "turn-level coordination field; no HTTP header projection"),
    "x-codex-turn-metadata": (
        "x-codex-turn-metadata",
        "compatibility header projection (omits tool_namespaces_info)",
    ),
    "x-codex-turn-state": (
        "x-codex-turn-state",
        "replayed as an HTTP request header; the WS handshake never carries it (response.create metadata only)",
    ),
    "ws_request_header_x_openai_internal_codex_responses_lite": (
        "x-openai-internal-codex-responses-lite",
        "transport shape flip: literal HTTP header vs WS response.create metadata key",
    ),
    "ws_request_header_traceparent": (None, "W3C trace context rides response.create metadata only"),
    "ws_request_header_tracestate": (None, "W3C trace context rides response.create metadata only"),
    "x-codex-ws-stream-request-start-ms": (None, "REQUIRED on the WS surface: stamped unconditionally on every response.create just before send (client.rs stamp_ws_stream_request_start_ms); no HTTP per-frame counterpart"),
}

VIRTUAL_FRAME_TRANSPORT_HEADERS = {
    "originator",
    "User-Agent",
    "version",
    "OpenAI-Organization",
    "OpenAI-Project",
    "Authorization",
    "ChatGPT-Account-ID",
    "X-OpenAI-Fedramp",
    "x-openai-internal-codex-residency",
}


def ws_virtual_frame_mapping(base_cm, ws_cm, http_headers, ws_headers, compact_headers):
    """Map the WS virtual frame (response.create client_metadata) to the HTTP
    transport's per-frame headers, marking required/optional per surface from
    the source construction. Transport-level headers (auth/UA/originator) are
    reported separately: they ride both transports outside any frame."""
    http_by_name = {h["name"]: h for h in http_headers + ws_headers + compact_headers}
    frames = base_cm + ws_cm
    rows = []
    for entry in frames:
        key = entry["name"]
        http_name, http_note = VIRTUAL_FRAME_HTTP_COUNTERPARTS.get(
            key, (None, "no HTTP counterpart recorded for this key")
        )
        http_entry = http_by_name.get(http_name) if http_name else None
        rows.append(
            {
                "virtual_frame_key": key,
                "ws_required": not entry["optional"],
                "ws_condition": entry["condition"],
                "http_header": http_name,
                "http_required": not http_entry["optional"] if http_entry else None,
                "http_condition": (http_entry["condition"] if http_entry else http_note),
                "delta": "paired" if http_entry else "ws_only",
                "source": entry["source"],
            }
        )
    mapped_http = {r["http_header"] for r in rows if r["http_header"]}
    mapped_keys = {r["virtual_frame_key"] for r in rows}
    http_only = [
        {
            "http_header": h["name"],
            "http_condition": h["condition"],
            "http_required": not h["optional"],
        }
        for h in http_headers
        if h["name"] not in mapped_http
        and h["name"] not in VIRTUAL_FRAME_TRANSPORT_HEADERS
    ]
    transport_level = [
        {"http_header": h["name"], "note": "rides both transports outside any frame"}
        for h in http_headers
        if h["name"] in VIRTUAL_FRAME_TRANSPORT_HEADERS
    ]
    return {
        "rows": rows,
        "http_only_headers": http_only,
        "transport_level_headers": transport_level,
        "notes": [
            "On the WS handshake codex ALSO sends session-id/thread-id headers "
            "(build_session_headers) in addition to the response.create "
            "client_metadata keys — the handshake pairs exist on both surfaces.",
            "delta=ws_only means the field exists only in the virtual frame; "
            "delta=paired means both surfaces carry it (check the http_condition "
            "text for surface-specific gating, e.g. compact-only installation-id).",
        ],
    }


def safe_src(
    text: str | None,
    path: str,
    needle: str,
    symbol: str | None = None,
) -> dict[str, Any] | None:
    if not text:
        return None
    i = text.find(needle)
    if i < 0:
        return None
    return {"path": path, "line": line_no(text, i), "symbol": symbol or needle}


def try_struct_fields(text: str | None, path: str, name: str) -> list[dict[str, Any]]:
    if not text:
        return []
    try:
        return struct_fields(text, path, name)
    except AuditError:
        return []


def group_headers_by_category(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Normalize header lists for endpoint-by-endpoint comparison."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item.get("category") or "uncategorized", []).append(
            {
                "name": item["name"],
                "optional": bool(item.get("optional", True)),
                "condition": item.get("condition"),
                "value_shape": item.get("value_shape"),
                "value_encoding": item.get("value_encoding"),
                "sensitive": bool(item.get("sensitive", False)),
                "direction": item.get("direction", "request"),
                "origin": item.get("origin", "client"),
                "continuation": bool(item.get("continuation")),
                "layer": item.get("layer"),
                "source": item.get("source"),
            }
        )
    return dict(sorted(grouped.items()))


def _header_names(items: list[dict[str, Any]]) -> list[str]:
    return [h["name"] for h in items]


def catalog_header(
    name: str,
    *,
    condition: str,
    value_shape: str | None = None,
    value_encoding: str | None = None,
    category: str = "http",
    direction: str = "request",
    origin: str = "client_transport",
    optional: bool = True,
    continuation: bool = False,
    sensitive: bool = False,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "condition": condition,
        "optional": optional,
        "value_shape": value_shape,
        "value_encoding": value_encoding,
        "sensitive": sensitive,
        "layer": "catalog",
        "category": category,
        "direction": direction,
        "origin": origin,
        "continuation": continuation,
        "source": source,
    }


def catalog_field(
    name: str,
    field_type: str,
    *,
    optional: bool = False,
    source: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "wire_name": name,
        "type": field_type,
        "optional": optional,
        "serialized": True,
        "source": source,
        "note": note,
    }


def catalog_header_template(
    name_template: str,
    *,
    condition: str,
    parameters: dict[str, Any],
    value_shape: str,
    category: str,
    source: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "name_template": name_template,
        "condition": condition,
        "parameters": parameters,
        "value_shape": value_shape,
        "direction": "response",
        "origin": "server",
        "category": category,
        "source": source,
    }


def build_response_metadata_inventory(
    response_sse: str,
    surface_sources: dict[str, str],
) -> dict[str, Any]:
    """Additional response headers/templates/events consumed outside the core SSE list."""
    rate = surface_sources.get("response_rate_limits")
    safety = surface_sources.get("safety_buffering")
    bridge = surface_sources.get("api_bridge")
    debug = surface_sources.get("response_debug_context")

    def response(
        name: str,
        *,
        condition: str,
        category: str,
        text: str | None,
        path_key: str,
        anchor: str,
        value_shape: str | None = None,
        value_encoding: str | None = None,
        sensitive: bool = False,
        origin: str = "server",
    ) -> dict[str, Any]:
        return catalog_header(
            name,
            condition=condition,
            value_shape=value_shape,
            value_encoding=value_encoding,
            category=category,
            direction="response",
            origin=origin,
            sensitive=sensitive,
            source=safe_src(text, SURFACE_FILES[path_key], anchor, name),
        )

    headers = [
        response(
            "x-oai-request-id",
            condition="fallback provider request id on HTTP errors when x-request-id is absent",
            category="observability",
            text=debug,
            path_key="response_debug_context",
            anchor="OAI_REQUEST_ID_HEADER",
            origin="server_or_infrastructure",
        ),
        response(
            "cf-ray",
            condition="Cloudflare request/ray identifier when supplied",
            category="infrastructure_debug",
            text=debug,
            path_key="response_debug_context",
            anchor="CF_RAY_HEADER",
            origin="server_or_infrastructure",
        ),
        response(
            "x-openai-authorization-error",
            condition="identity authorization diagnostic on failed requests",
            category="identity_debug",
            text=debug,
            path_key="response_debug_context",
            anchor="AUTH_ERROR_HEADER",
            sensitive=True,
        ),
        response(
            "x-error-json",
            condition="base64-encoded structured identity/error diagnostic on failed requests",
            category="identity_debug",
            text=debug,
            path_key="response_debug_context",
            anchor="X_ERROR_JSON_HEADER",
            value_shape="JSON object",
            value_encoding="base64",
            sensitive=True,
        ),
        response(
            "x-codex-active-limit",
            condition="selects the metered limit family associated with a usage-limit error",
            category="rate_limit",
            text=bridge,
            path_key="api_bridge",
            anchor="ACTIVE_LIMIT_HEADER",
        ),
        response(
            "x-codex-promo-message",
            condition="optional usage-limit promotional message",
            category="rate_limit",
            text=rate,
            path_key="response_rate_limits",
            anchor='"x-codex-promo-message"',
        ),
        response(
            "x-codex-rate-limit-reached-type",
            condition="optional structured reason for a reached rate limit",
            category="rate_limit",
            text=rate,
            path_key="response_rate_limits",
            anchor='"x-codex-rate-limit-reached-type"',
        ),
        response(
            "x-codex-credits-has-credits",
            condition="credit availability snapshot accompanies rate-limit headers",
            category="rate_limit_credit",
            text=rate,
            path_key="response_rate_limits",
            anchor='"x-codex-credits-has-credits"',
            value_shape="boolean: true/false or 1/0",
        ),
        response(
            "x-codex-credits-unlimited",
            condition="credit snapshot indicates unlimited use",
            category="rate_limit_credit",
            text=rate,
            path_key="response_rate_limits",
            anchor='"x-codex-credits-unlimited"',
            value_shape="boolean: true/false or 1/0",
        ),
        response(
            "x-codex-credits-balance",
            condition="credit snapshot includes a balance",
            category="rate_limit_credit",
            text=rate,
            path_key="response_rate_limits",
            anchor='"x-codex-credits-balance"',
        ),
        response(
            "x-codex-safety-buffering-enabled",
            condition="server supplies safety-buffering treatment state",
            category="safety_treatment",
            text=safety,
            path_key="safety_buffering",
            anchor="X_CODEX_SAFETY_BUFFERING_ENABLED_HEADER",
            value_shape="boolean-like string",
        ),
        response(
            "x-codex-safety-buffering-faster-model",
            condition="server supplies the treatment's faster-model fallback",
            category="safety_treatment",
            text=safety,
            path_key="safety_buffering",
            anchor="X_CODEX_SAFETY_BUFFERING_FASTER_MODEL_HEADER",
        ),
    ]
    templates = [
        catalog_header_template(
            "x-{limit-id}-{window}-{metric}",
            condition="server reports one or more dynamically named metered-limit windows",
            parameters={
                "limit-id": "normalized server-provided metered limit id; hyphenated on the wire",
                "window": ["primary", "secondary"],
                "metric": ["used-percent", "window-minutes", "reset-at"],
            },
            value_shape="number encoded as an HTTP header string",
            category="rate_limit",
            source=safe_src(rate, SURFACE_FILES["response_rate_limits"], 'let prefix = format!("x-{normalized_limit}")', "parse_rate_limit_for_limit"),
        ),
        catalog_header_template(
            "x-{limit-id}-limit-name",
            condition="server supplies the display/model name for a dynamically named metered limit",
            parameters={"limit-id": "normalized server-provided metered limit id; hyphenated on the wire"},
            value_shape="non-empty string",
            category="rate_limit",
            source=safe_src(rate, SURFACE_FILES["response_rate_limits"], 'let limit_name_header = format!("{prefix}-limit-name")', "parse_rate_limit_for_limit"),
        ),
    ]
    event_metadata = [
        {
            "event": "codex.rate_limits",
            "location": "event JSON payload",
            "condition": "rate-limit update arrives as an application event rather than HTTP headers",
            "fields": ["plan_type", "rate_limits.primary", "rate_limits.secondary", "credits", "metered_limit_name", "limit_name"],
            "source": safe_src(rate, SURFACE_FILES["response_rate_limits"], 'event.kind != "codex.rate_limits"', "parse_rate_limit_event"),
        },
        {
            "event": "response.metadata",
            "location": "metadata.openai_verification_recommendation",
            "condition": "server provides model verification recommendations",
            "source": safe_src(response_sse, FILES["response_sse"], "openai_verification_recommendation", "ResponsesStreamEvent::model_verifications"),
        },
        {
            "event": "response.metadata",
            "location": "metadata.openai_chatgpt_moderation_metadata",
            "condition": "server provides turn moderation metadata",
            "source": safe_src(response_sse, FILES["response_sse"], "openai_chatgpt_moderation_metadata", "ResponsesStreamEvent::turn_moderation_metadata"),
        },
        {
            "event": "response.metadata or event top level",
            "location": "safety_buffering",
            "condition": "server provides safety-buffering treatment data",
            "source": safe_src(response_sse, FILES["response_sse"], "fn safety_buffering", "ResponsesStreamEvent::safety_buffering"),
        },
        {
            "event": "response.created",
            "location": "response.headers.<guardian-ticket-header>",
            "condition": "server returns the opaque Guardian receipt for this response",
            "source": safe_src(response_sse, FILES["response_sse"], "GUARDIAN_TICKET_HEADER", "response.created Guardian ticket"),
        },
    ]
    return {
        "headers": [item for item in headers if item.get("source")],
        "header_templates": [item for item in templates if item.get("source")],
        "event_metadata": [item for item in event_metadata if item.get("source")],
        "event_kinds": discover_response_event_kinds(response_sse),
    }


ENDPOINT_MODULE_SURFACE_MAP = {
    "compact": ["responses_compact_legacy"],
    "images": ["images_unary_json"],
    "memories": ["memories_trace_summarize"],
    "models": ["models_list"],
    "realtime_call": ["realtime_call_webrtc_negotiation"],
    "realtime_websocket": ["realtime_websocket"],
    "responses": ["responses_http_sse"],
    "responses_websocket": ["responses_websocket"],
    "search": ["search_unary_json"],
}

ENDPOINT_MODULE_EXCLUSIONS = {
    "session": "internal shared request/session implementation; not an independently addressable wire surface",
}


def endpoint_module_inventory(text: str | None) -> dict[str, Any]:
    text = text or ""
    modules = list(dict.fromkeys(re.findall(
        r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*;",
        text,
    )))
    exported_clients = list(dict.fromkeys(re.findall(r"(?m)^\s*pub\s+use\s+[a-zA-Z_][a-zA-Z0-9_]*::([A-Z][A-Za-z0-9_]*Client)\s*;", text)))
    return {"modules": modules, "exported_clients": exported_clients}


def build_coverage_contract(
    surfaces: list[dict[str, Any]],
    surface_sources: dict[str, str],
    core_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    inventory = endpoint_module_inventory(surface_sources.get("endpoint_mod"))
    surface_ids = {str(surface.get("id")) for surface in surfaces}
    module_rows = []
    unclassified = []
    for module in inventory["modules"]:
        expected = ENDPOINT_MODULE_SURFACE_MAP.get(module, [])
        exclusion = ENDPOINT_MODULE_EXCLUSIONS.get(module)
        classified = bool(expected) and all(surface_id in surface_ids for surface_id in expected)
        if not classified and not exclusion:
            unclassified.append(module)
        module_rows.append({
            "module": module,
            "expected_surface_ids": expected,
            "classified": classified,
            "exclusion": exclusion,
        })
    response_source_keys = {
        "response_rate_limits",
        "safety_buffering",
        "api_bridge",
        "response_debug_context",
        "models_endpoint",
        "files_api",
        "images_endpoint",
    }
    response_sources = {
        key: value for key, value in surface_sources.items() if key in response_source_keys
    }
    for key in ("response_sse", "ws", "compact"):
        value = (core_sources or {}).get(key)
        if value:
            response_sources[f"core_{key}"] = value
    response_candidates = discover_response_header_candidates(response_sources)
    declared_response_headers = {
        str(name).lower()
        for surface in surfaces
        for name in (surface.get("headers", {}).get("response_names") or [])
    }
    declared_response_headers.update(
        str(item.get("name")).lower()
        for surface in surfaces
        for item in (surface.get("response", {}).get("event_metadata") or [])
        if item.get("name")
    )
    unclassified_response_headers = [
        name for name in response_candidates if name.lower() not in declared_response_headers
    ]

    return {
        "scope_contract": (
            "Exhaustive for addressable modules declared by codex-api/src/endpoint/mod.rs; "
            "also catalogs selected production backend-control, hosted-file, cost, Guardian, "
            "realtime, and app-server surfaces named by this generator. It is not a repository-wide "
            "inventory of every internal service client or IPC protocol."
        ),
        "endpoint_module_inventory": inventory,
        "endpoint_module_classification": module_rows,
        "unclassified_endpoint_modules": unclassified,
        "selected_response_header_candidates": response_candidates,
        "declared_response_header_names": sorted(declared_response_headers),
        "unclassified_response_header_candidates": unclassified_response_headers,
        "explicit_repository_exclusions": [
            "internal exec-server JSON-RPC and environment filesystem/process protocols",
            "cloud-task and environment-discovery product APIs",
            "agent-identity bootstrap/JWKS traffic",
            "workspace-message and other product backend routes not used by the audited account/cost flows",
            "arbitrary custom-provider, proxy-added, and runtime-generated transport headers",
        ],
        "fetched_optional_sources": sorted(surface_sources),
        "catalog_surface_ids": sorted(surface_ids),
    }


def _surface(
    *,
    surface_id: str,
    category: str,
    endpoints: list[str],
    method: str,
    transport: str,
    protocol: str,
    streaming: bool,
    request_content_type: str | None,
    request_accept: str | None,
    request_framing: str,
    response_media_type: str | None,
    response_framing: str,
    body_schema: str | None,
    body_fields: list[dict[str, Any]] | None = None,
    response_body_schema: str | None = None,
    response_body_fields: list[dict[str, Any]] | None = None,
    request_headers: list[dict[str, Any]] | None = None,
    response_headers: list[dict[str, Any]] | None = None,
    response_header_templates: list[dict[str, Any]] | None = None,
    response_event_metadata: list[dict[str, Any]] | None = None,
    variants: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
    source: dict[str, Any] | None = None,
    responses_compatible: bool = False,
    plane: str = "provider_data",
    service_family: str | None = None,
    address_kind: str = "http_endpoint",
    header_profile: str | None = None,
    inherits_inference_headers: bool = False,
    excluded_header_names: list[str] | None = None,
    application_metadata: dict[str, Any] | None = None,
    transport_generated_headers: list[str] | None = None,
    usage_status: str = "active",
    profile_refs: dict[str, str] | None = None,
    trust_domain: str | None = None,
    connection_lifetime: str | None = None,
) -> dict[str, Any]:
    req_headers = request_headers or []
    resp_headers = response_headers or []
    return {
        "id": surface_id,
        "plane": plane,
        "service_family": service_family or category,
        "category": category,
        "addressing": {"kind": address_kind, "values": endpoints},
        # Compatibility key. RPC/control surfaces should use addressing instead.
        "endpoints": endpoints if address_kind in {"http_endpoint", "websocket_endpoint"} else [],
        "method": method,
        "transport": transport,
        "protocol": protocol,
        "streaming": streaming,
        "responses_compatible": responses_compatible,
        "usage_status": usage_status,
        "trust_domain": trust_domain,
        "lifecycle": {
            "connection_lifetime": connection_lifetime or ("persistent" if "WSS" in transport else "request-scoped"),
            "payload_streaming": streaming,
        },
        "request": {
            "content_type": request_content_type,
            "accept": request_accept,
            "framing": request_framing,
            "body_schema": body_schema,
            "body_fields": body_fields or [],
        },
        "response": {
            "media_type": response_media_type,
            "framing": response_framing,
            "body_schema": response_body_schema,
            "body_fields": response_body_fields or [],
            "event_metadata": response_event_metadata or [],
        },
        "headers": {
            "profile": header_profile,
            "inherits_inference_headers": inherits_inference_headers,
            "excluded_names": sorted(excluded_header_names or [], key=str.lower),
            "request_names": _header_names(req_headers),
            "request_by_category": group_headers_by_category(req_headers),
            "response_names": _header_names(resp_headers),
            "response_by_category": group_headers_by_category(resp_headers),
            "response_templates": response_header_templates or [],
        },
        "application_metadata": application_metadata or {},
        "profiles": profile_refs or {},
        "transport_generated_headers": transport_generated_headers or [],
        "variants": variants or [],
        "notes": notes or [],
        "source": source,
    }


def backend_account_headers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only headers owned by BackendClient::headers().

    Usage/reset traffic is backend account/control traffic, not Responses
    inference. Filtering here prevents turn/routing headers from leaking into
    this profile if another report section changes later.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name", ""))
        if name.lower() not in BACKEND_ACCOUNT_HEADER_NAMES:
            continue
        cloned = dict(item)
        lower = name.lower()
        if lower == "user-agent":
            cloned["category"] = "client"
        elif lower == "chatgpt-account-id":
            cloned["category"] = "account_scope"
        elif lower == "x-openai-fedramp":
            cloned["category"] = "account_routing"
        else:
            cloned["category"] = "auth"
        out.append(cloned)
    return out


def backend_json_content_type(
    text: str | None, path: str, anchor_text: str, symbol: str
) -> dict[str, Any]:
    return catalog_header(
        "Content-Type",
        condition="JSON request body",
        value_shape="application/json",
        category="http",
        optional=False,
        source=safe_src(text, path, anchor_text, symbol),
    )



def _normalized_header_profile(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Canonical header profile payload used by multiple wire surfaces."""
    return {
        "names": sorted({str(h.get("name", "")) for h in items if h.get("name")}, key=str.lower),
        "by_category": group_headers_by_category(items),
    }


def _field_map(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(x.get("wire_name") or x.get("name")): x for x in items}


def compare_struct_field_sets(
    left_name: str,
    left: list[dict[str, Any]],
    right_name: str,
    right: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute a wire-name based schema delta between two serialized structs."""
    lmap, rmap = _field_map(left), _field_map(right)
    shared = sorted(set(lmap) & set(rmap))
    return {
        "left": left_name,
        "right": right_name,
        "shared_fields": [
            {
                "wire_name": name,
                "left_type": lmap[name].get("type"),
                "right_type": rmap[name].get("type"),
                "same_type": lmap[name].get("type") == rmap[name].get("type"),
                "left_optional": bool(lmap[name].get("optional")),
                "right_optional": bool(rmap[name].get("optional")),
            }
            for name in shared
        ],
        "shared_wire_field_names": shared,
        "left_only": [lmap[name] for name in sorted(set(lmap) - set(rmap))],
        "left_only_wire_field_names": sorted(set(lmap) - set(rmap)),
        "right_only": [rmap[name] for name in sorted(set(rmap) - set(lmap))],
        "right_only_wire_field_names": sorted(set(rmap) - set(lmap)),
        "wire_note": "Rust owned-vs-borrowed field types can differ while serializing to the same JSON wire shape; compare wire_name/serde behavior first",
    }


def guardian_classifier_client_metadata_profile(text: str | None) -> dict[str, Any]:
    """Extract the Luna classifier's independently-built response.create metadata.

    This intentionally does not inherit CodexResponsesMetadata::client_metadata().
    The classifier creates a fresh per-connection thread id and its own metadata
    map, then inserts the serialized Guardian classifier turn snapshot.
    """
    if not text:
        return {
            "id": "guardian_classifier_response_create",
            "construction": "independent LunaSampler map",
            "keys": [],
            "nested_turn_metadata_keys": [],
            "warnings": ["guardian sampler source unavailable"],
        }
    cmap = consts({"guardian_sampler": text})
    start = text.find("let mut client_metadata = HashMap::from([")
    end = text.find("request.client_metadata = Some(client_metadata)", start)
    if start < 0 or end < 0:
        return {
            "id": "guardian_classifier_response_create",
            "construction": "independent LunaSampler map",
            "keys": [],
            "nested_turn_metadata_keys": [],
            "warnings": ["classifier client_metadata construction anchor changed upstream"],
        }
    block = text[start:end]
    names: list[str] = []
    def add(name: str) -> None:
        if name and name not in names:
            names.append(name)
    for lit in re.findall(r'\(\s*"([^"]+)"\.to_owned\(\)', block):
        add(lit)
    for token in re.findall(r'\(\s*([A-Z][A-Z0-9_]*)\.to_owned\(\)', block):
        add(cmap.get(token, token))
    for lit in re.findall(r'client_metadata\.insert\(\s*"([^"]+)"\.to_owned\(\)', block):
        add(lit)
    for token in re.findall(r'client_metadata\.insert\(\s*([A-Z][A-Z0-9_]*)\.to_owned\(\)', block):
        add(cmap.get(token, token))

    entries = []
    for name in names:
        token = next((t for t, v in cmap.items() if v == name), None)
        anchor = token if token and token in block else f'"{name}"'
        entries.append({
            "name": name,
            "optional": name == "root_turn_id",
            "condition": "when root_turn_id exists" if name == "root_turn_id" else "classifier response.create metadata",
            "source": safe_src(text, SURFACE_FILES["guardian_sampler"], anchor, "LunaSampler classifier client_metadata"),
        })

    turn_start = text.rfind("let mut turn_metadata = json!({", 0, start)
    turn_end = text.find("});", turn_start) if turn_start >= 0 else -1
    turn_block = text[turn_start:turn_end] if turn_start >= 0 and turn_end >= 0 else ""
    nested = list(dict.fromkeys(re.findall(r'"([A-Za-z0-9_.-]+)"\s*:', turn_block)))
    if "root_turn_id" in block and "root_turn_id" not in nested:
        nested.append("root_turn_id")
    return {
        "id": "guardian_classifier_response_create",
        "construction": "independent LunaSampler map; does not extend CodexResponsesMetadata::client_metadata()",
        "keys": entries,
        "key_names": names,
        "nested_turn_metadata_keys": nested,
        "not_inherited_from_normal_responses": sorted(
            CORE_BASE_CLIENT_METADATA - set(names)
        ),
        "identity_note": (
            "thread_id is the classifier connection's generated thread id; the owning source thread is retained separately inside nested turn metadata"
        ),
        "warnings": [],
        "source": safe_src(
            text,
            SURFACE_FILES["guardian_sampler"],
            "let mut client_metadata = HashMap::from([",
            "LunaSampler classifier client_metadata",
        ),
    }


def build_shared_wire_profiles(
    report: dict[str, Any],
    core_sources: dict[str, str],
    surface_sources: dict[str, str] | None = None,
    guardian_ws_request_headers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build canonical reusable profiles and deltas instead of duplicating wire data."""
    surface_sources = surface_sources or {}
    http_headers = report["responses_http"].get("request_headers", []) + report["responses_http"].get("tracking_request_headers", [])
    ws_headers = report["responses_websocket"].get("handshake_request_headers", [])
    compact_headers = report["responses_compact"].get("request_headers", [])
    account_headers = backend_account_headers((report.get("account_status_check") or {}).get("request_headers", []))

    h_http = {h["name"].lower(): h for h in http_headers}
    h_ws = {h["name"].lower(): h for h in ws_headers}
    shared_header_names = sorted(set(h_http) & set(h_ws))

    common = core_sources.get("common", "")
    http_body = try_struct_fields(common, FILES["common"], "ResponsesApiRequest")
    ws_body = try_struct_fields(common, FILES["common"], "ResponseCreateWsRequest")
    body_delta = compare_struct_field_sets(
        "ResponsesApiRequest", http_body, "ResponseCreateWsRequest", ws_body
    )

    base_cm = report["responses_websocket"].get("client_metadata_base", [])
    ws_cm = report["responses_websocket"].get("client_metadata_ws_additions", [])
    classifier_cm = guardian_classifier_client_metadata_profile(surface_sources.get("guardian_sampler"))

    guardian_headers = list(guardian_ws_request_headers or [])
    if not guardian_headers and surface_sources.get("guardian_sampler"):
        guardian_header_names = {
            "session-id", "thread-id", "x-client-request-id", "x-codex-window-id",
            "x-openai-subagent", "openai-beta", "authorization", "chatgpt-account-id",
            "x-openai-fedramp", "user-agent", "originator", "version",
            "x-openai-internal-codex-residency", "openai-organization", "openai-project",
        }
        guardian_headers = [h for h in ws_headers if h.get("name", "").lower() in guardian_header_names]
        guardian_headers.append(
            catalog_header(
                "x-openai-internal-codex-responses-lite",
                condition="always on current Luna classifier WebSocket handshake",
                value_shape="true",
                category="feature",
                optional=False,
                source=safe_src(
                    surface_sources.get("guardian_sampler"),
                    SURFACE_FILES["guardian_sampler"],
                    "x-openai-internal-codex-responses-lite",
                    "LunaSampler::open_connection",
                ),
            )
        )
    header_profiles = {
        "responses_common_request_headers": {
            "kind": "intersection",
            "names": [h_http[n]["name"] for n in shared_header_names],
            "http_cadence": "every HTTP inference request",
            "websocket_cadence": "WebSocket handshake only",
        },
        "responses_http_inference": {
            "extends": ["responses_common_request_headers"],
            **_normalized_header_profile(http_headers),
            "adds_relative_to_ws": [h_http[n]["name"] for n in sorted(set(h_http) - set(h_ws))],
        },
        "responses_ws_handshake": {
            "extends": ["responses_common_request_headers"],
            **_normalized_header_profile(ws_headers),
            "adds_relative_to_http": [h_ws[n]["name"] for n in sorted(set(h_ws) - set(h_http))],
        },
        "legacy_compact_inference": _normalized_header_profile(compact_headers),
        "backend_account_common": {
            **_normalized_header_profile(account_headers),
            "explicitly_excludes_inference_headers": sorted(INFERENCE_ONLY_HEADER_NAMES),
        },
        "guardian_classifier_lite_ws": {
            **_normalized_header_profile(guardian_headers),
            "construction": "dedicated LunaSampler handshake; not the normal Responses handshake builder",
            "layers": {
                "direct_sampler_headers": [
                    "session-id", "thread-id", "x-openai-subagent", "x-codex-window-id",
                    "OpenAI-Beta", "x-openai-internal-codex-responses-lite",
                    "originator (when configured)", "x-client-request-id",
                ],
                "inherited_via_responses_websocket_client": [
                    "provider headers (for example version/OpenAI-Organization/OpenAI-Project)",
                    "auth headers (Authorization/ChatGPT-Account-ID/X-OpenAI-Fedramp as applicable)",
                    "default client headers (User-Agent/residency as applicable)",
                ],
            },
        },
    }

    cm_profiles = {
        "responses_base": {
            "construction": "CodexResponsesMetadata::client_metadata()",
            "keys": base_cm,
            "key_names": [x.get("name") for x in base_cm],
        },
        "responses_ws_response_create": {
            "extends": ["responses_base"],
            "base_keys": base_cm,
            "additions": ws_cm,
            "key_names": [x.get("name") for x in base_cm + ws_cm],
        },
        "guardian_classifier_response_create": classifier_cm,
    }

    body_profiles = {
        "responses_http_request": {
            "struct": "ResponsesApiRequest",
            "fields": http_body,
        },
        "responses_ws_response_create": {
            "struct": "ResponseCreateWsRequest",
            "envelope": "ResponsesWsRequest::ResponseCreate / type=response.create",
            "fields": ws_body,
        },
        "responses_http_vs_ws_delta": body_delta,
        "guardian_classifier_response_create": {
            "extends": ["responses_ws_response_create"],
            "specialized_values": {
                "tools": None,
                "tool_choice": "none",
                "parallel_tool_calls": False,
                "stream": True,
                "service_tier": None,
                "prompt_cache_key": "guardian-v2:<owning-thread-id>",
            },
        },
    }

    event_profiles = {
        "responses_events": {
            "logical_application_protocol": "Responses event stream",
            "shared_semantics": {
                "response.completed.response.id": "server response identity",
                "response.completed.response.usage": "token usage/accounting payload when present",
            },
            "transport_encodings": {
                "http_sse": "SSE record whose data field contains JSON",
                "websocket": "JSON WebSocket text frame",
            },
            "metadata_inventory": {
                "http_response_headers": report.get("responses_http", {}).get("response_headers", []),
                "http_response_header_templates": report.get("responses_http", {}).get("response_header_templates", []),
                "logical_event_metadata": report.get("responses_http", {}).get("response_event_metadata", []),
                "websocket_metadata_headers": report.get("responses_websocket", {}).get("response_event_metadata", []),
            },
            "note": "framing differs; logical response.* event semantics are shared",
            "source": {
                "http_sse_parser": safe_src(core_sources.get("response_sse"), FILES["response_sse"], "ResponsesStreamEvent", "Responses SSE event parser"),
                "websocket_envelope": safe_src(core_sources.get("common"), FILES["common"], "pub enum ResponsesWsRequest", "ResponsesWsRequest"),
            },
        }
    }

    application_protocol_profiles = {
        "responses_events": event_profiles["responses_events"],
        "unary_json": {"framing": "single HTTP response body decoded as JSON"},
        "websocket_json": {"framing": "persistent JSON text frames after HTTP 101"},
    }

    return {
        "header_profiles": header_profiles,
        "client_metadata_profiles": cm_profiles,
        "request_body_profiles": body_profiles,
        "response_event_profiles": event_profiles,
        "application_protocol_profiles": application_protocol_profiles,
    }


def build_model_transport_delta(
    report: dict[str, Any], shared_profiles: dict[str, Any]
) -> dict[str, Any]:
    """Normalized HTTP/SSE ↔ Responses-WSS delta across header/body/event/lifecycle layers."""
    hp = shared_profiles["header_profiles"]
    cp = shared_profiles["client_metadata_profiles"]
    bp = shared_profiles["request_body_profiles"]
    http_names = set(hp["responses_http_inference"]["names"])
    ws_names = set(hp["responses_ws_handshake"]["names"])
    relation_types = {
        "same_name_same_value": "same header/key/value concept on both surfaces",
        "same_value_renamed": "same runtime value but wire name changes",
        "same_concept_different_representation": "same logical concept represented in a different wire layer/shape",
        "header_to_client_metadata": "HTTP header projection becomes per-response.create client_metadata",
        "per_request_to_handshake": "HTTP request header becomes WebSocket handshake-scoped",
        "http_only": "present only on HTTP transport",
        "ws_only": "present only on Responses WebSocket transport",
        "transport_generated": "generated by HTTP/WebSocket stack rather than Codex application framing",
        "compatibility_projection": "bounded/legacy projection of a canonical metadata payload",
        "server_derived_replay": "server value replayed by client on later same-turn requests",
        "route_suppressed": "normally supported field/header deliberately omitted on a route",
    }
    return {
        "relation_types": relation_types,
        "headers": {
            "shared_names": sorted(http_names & ws_names, key=str.lower),
            "http_only": sorted(http_names - ws_names, key=str.lower),
            "websocket_handshake_only": sorted(ws_names - http_names, key=str.lower),
            "cadence_delta": {
                "http": "headers are sent per HTTP request",
                "websocket": "HTTP/Codex headers are sent at handshake; per-request metadata moves into response.create",
            },
        },
        "client_metadata": {
            "normal_responses_shared_profile": "responses_base",
            "websocket_extension_profile": "responses_ws_response_create",
            "websocket_only_additions": [x.get("name") for x in cp["responses_ws_response_create"].get("additions", [])],
            "guardian_classifier_profile": "guardian_classifier_response_create",
            "guardian_classifier_construction": cp["guardian_classifier_response_create"].get("construction"),
            "guardian_classifier_not_inherited": sorted(
                set(cp["responses_base"].get("key_names", []))
                - set(cp["guardian_classifier_response_create"].get("key_names", []))
            ),
            "guardian_classifier_only": sorted(
                set(cp["guardian_classifier_response_create"].get("key_names", []))
                - set(cp["responses_base"].get("key_names", []))
            ),
        },
        "request_body": bp["responses_http_vs_ws_delta"],
        "response": {
            "token_usage": {
                "http_sse": "response.completed JSON payload inside SSE data",
                "websocket": "response.completed JSON text frame",
                "relation": "same_concept_different_representation",
            },
            "response_id": {
                "http_sse": "response.completed.response.id",
                "websocket": "response.completed.response.id; may be replayed as previous_response_id",
                "relation": "server_derived_replay",
            },
            "effective_model": {
                "http_sse": "openai-model HTTP response header",
                "websocket": "HTTP 101 header and/or response metadata event",
                "relation": "same_concept_different_representation",
            },
            "model_etag": {
                "http_sse": "X-Models-Etag HTTP response header",
                "websocket": "x-models-etag response metadata event",
                "relation": "same_value_renamed",
            },
            "upstream_request_id": {
                "http_sse": "x-request-id may populate ResponseStream.upstream_request_id",
                "websocket": "ResponseStream.upstream_request_id=None",
                "relation": "http_only",
            },
        },
        "framing": {
            "logical_application_protocol": "Responses response.* events",
            "http_sse": "HTTP POST + SSE records; data contains JSON",
            "websocket": "HTTP Upgrade + persistent JSON text frames",
        },
        "connection_lifecycle": {
            "http_sse": {
                "connection_scope": "request/response attempt",
                "continuation": "full request resent; no previous_response_id transport optimization",
                "compression": "optional Content-Encoding: zstd",
            },
            "websocket": {
                "connection_scope": "persistent reusable connection, cached per turn/session path",
                "prewarm": "response.create generate=false is supported for v2 prewarm",
                "continuation": "previous_response_id may reuse prior completed response",
                "compression": "permessage-deflate WebSocket extension",
            },
        },
        "field_movement": [
            {"concept": "routing_hint", "http": "request.header.x-codex-routing-hint", "websocket": "handshake.header.x-codex-routing-hint", "relation": "per_request_to_handshake"},
            {"concept": "service_tier", "http": "ResponsesApiRequest.service_tier", "websocket": "response.create.service_tier", "relation": "same_concept_different_representation"},
            {"concept": "responses_lite", "http": "request.header.x-openai-internal-codex-responses-lite", "websocket": "response.create.client_metadata.ws_request_header_x_openai_internal_codex_responses_lite", "relation": "header_to_client_metadata"},
            {"concept": "turn_state", "http": "response/request.header.x-codex-turn-state", "websocket": "response.create.client_metadata.x-codex-turn-state", "relation": "header_to_client_metadata"},
            {"concept": "turn_metadata", "http": "body.client_metadata + compatibility header", "websocket": "response.create.client_metadata + handshake compatibility projection", "relation": "compatibility_projection"},
        ],
    }


def build_model_facing_transport_matrix(
    report: dict[str, Any],
    core_sources: dict[str, str],
    surface_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Route-by-route model-facing transport and field-placement matrix.

    The endpoint catalog is surface-oriented. This matrix answers the inverse
    question: for one model-facing route, how does the wire change when Codex
    uses HTTP/SSE versus Responses WebSocket? It also separates *client
    capability* from the transport currently used by a specialized call path.
    """
    surface_sources = surface_sources or {}
    shared_profiles = build_shared_wire_profiles(report, core_sources, surface_sources)
    transport_delta = build_model_transport_delta(report, shared_profiles)
    http_headers = {h["name"].lower(): h for h in report["responses_http"].get("request_headers", [])}
    ws_headers = {h["name"].lower(): h for h in report["responses_websocket"].get("handshake_request_headers", [])}
    compact_headers = {h["name"].lower(): h for h in report["responses_compact"].get("request_headers", [])}
    base_cm = [x["name"] for x in report["responses_websocket"].get("client_metadata_base", [])]
    ws_cm = [x["name"] for x in report["responses_websocket"].get("client_metadata_ws_additions", [])]

    def has(mapping: dict[str, Any], name: str) -> bool:
        return name.lower() in mapping

    common_http = {
        "method": "POST",
        "transport": "HTTPS",
        "request_body": "ResponsesApiRequest JSON",
        "request_body_profile": "responses_http_request",
        "request_content_type": "application/json",
        "accept": "text/event-stream",
        "response_framing": "SSE records containing JSON event payloads",
        "header_cadence": "request headers are rebuilt/sent on each HTTP inference request",
        "client_metadata_profile": "responses_base",
        "identity": {
            "headers": [n for n in ("session-id", "thread-id", "x-client-request-id") if has(http_headers, n)],
            "body_client_metadata": [n for n in ("session_id", "thread_id", "x-codex-installation-id", "x-codex-window-id") if n in base_cm],
        },
        "turn_metadata": {
            "canonical": 'client_metadata["x-codex-turn-metadata"]',
            "compatibility": "x-codex-turn-metadata HTTP header on applicable requests",
        },
        "turn_state": "server response header x-codex-turn-state -> request header x-codex-turn-state on later requests in the same turn",
        "lite_marker": "x-openai-internal-codex-responses-lite: true HTTP request header",
        "service_tier_location": "ResponsesApiRequest.service_tier",
        "response_id_continuation": "no previous_response_id transport optimization on HTTP; the full request is sent",
        "compression": "optional HTTP Content-Encoding: zstd on supported first-party request paths",
        "token_usage": "response.completed event payload (resp.usage -> token_usage), not an HTTP usage header",
        "effective_model": "openai-model may arrive as an HTTP response header",
        "model_etag": "X-Models-Etag may arrive as an HTTP response header",
        "reasoning_included": "x-reasoning-included may arrive as an HTTP response header",
        "upstream_request_id": "x-request-id response header may populate ResponseStream.upstream_request_id",
    }
    common_ws = {
        "method": "HTTP GET Upgrade, then persistent WSS",
        "transport": "WSS",
        "request_body": "response.create JSON text frame (ResponseCreateWsRequest)",
        "request_body_profile": "responses_ws_response_create",
        "request_content_type": None,
        "accept": None,
        "response_framing": "persistent WebSocket JSON text frames",
        "header_cadence": "HTTP/Codex headers are sent at the WebSocket handshake, not per response.create",
        "runtime_upgrade_headers": ["Host", "Connection: Upgrade", "Upgrade: websocket", "Sec-WebSocket-Key", "Sec-WebSocket-Version"],
        "client_metadata_profile": "responses_ws_response_create",
        "identity": {
            "handshake_headers": [n for n in ("session-id", "thread-id", "x-client-request-id") if has(ws_headers, n)],
            "response_create_client_metadata": [n for n in ("session_id", "thread_id", "x-codex-installation-id", "x-codex-window-id") if n in base_cm],
        },
        "turn_metadata": {
            "canonical": 'response.create.client_metadata["x-codex-turn-metadata"]',
            "compatibility": "x-codex-turn-metadata may be projected on the handshake; it is not a per-frame HTTP header",
        },
        "turn_state": 'response.create.client_metadata["x-codex-turn-state"] after server supplies it',
        "lite_marker": 'response.create.client_metadata["ws_request_header_x_openai_internal_codex_responses_lite"]="true"',
        "service_tier_location": "response.create request field service_tier",
        "response_id_continuation": "previous_response_id may reference the previous completed response.id for incremental reuse",
        "per_message_metadata_additions": ws_cm,
        "prewarm": "Responses WebSocket v2 can send response.create with generate=false before the first real turn request",
        "compression": "WebSocket extension negotiates permessage-deflate; no Content-Encoding header per frame",
        "token_usage": "response.completed JSON event/frame payload, not a handshake usage header",
        "effective_model": "openai-model may arrive on the 101 handshake and/or response metadata events",
        "model_etag": "x-models-etag is surfaced from response metadata events",
        "reasoning_included": "x-reasoning-included may arrive on the 101 handshake",
        "upstream_request_id": "ResponseStream.upstream_request_id is None for Responses WebSocket",
    }

    routes = [
        {
            "route": "/responses",
            "role": "normal user-owned Responses inference and Guardian fallback route",
            "guardian_routing_role": "normal/fallback route when free_guardian is disabled or eligibility checks fail",
            "transport_policy": "normal Codex transport selection can use HTTP/SSE or Responses WSS",
            "profiles": {
                "http": {"headers": "responses_http_inference", "client_metadata": "responses_base", "request_body": "responses_http_request", "response_events": "responses_events"},
                "websocket": {"headers": "responses_ws_handshake", "client_metadata": "responses_ws_response_create", "request_body": "responses_ws_response_create", "response_events": "responses_events"},
            },
            "http_sse": dict(common_http, current_path=True, routing_hint="x-codex-routing-hint may be sent per HTTP request on first-party Codex backend"),
            "websocket": dict(common_ws, current_path=True, routing_hint="x-codex-routing-hint is a handshake header, not a response.create field"),
            "service_tier": "may be present when supported/non-default; body/frame field, with priority mapping handled separately",
            "responses_lite": "optional; transport-specific marker locations shown above",
            "compaction_v2": "uses this same route/transport with a compaction_trigger input item; no separate endpoint",
        },
        {
            "route": "/guardian",
            "role": "full Guardian approval-review inference",
            "guardian_routing_role": "unmetered full-review route selected by free_guardian when eligibility checks pass",
            "selected_when": "free_guardian=true AND current Codex backend/provider/auth/session/model eligibility checks pass",
            "fallback_route": "/responses",
            "transport_policy": "shares normal Responses transport machinery; can follow HTTP/SSE or WSS selection",
            "profiles": {
                "http": {"headers": "responses_http_inference", "client_metadata": "responses_base", "request_body": "responses_http_request", "response_events": "responses_events"},
                "websocket": {"headers": "responses_ws_handshake", "client_metadata": "responses_ws_response_create", "request_body": "responses_ws_response_create", "response_events": "responses_events"},
            },
            "route_overrides": {
                "x-codex-routing-hint": "suppressed/not added by the normal /responses routing-hint branch",
                "service_tier": "forced to None/omitted",
            },
            "http_sse": dict(common_http, current_path=True, routing_hint="normal /responses routing-hint branch is not used"),
            "websocket": dict(common_ws, current_path=True, routing_hint="normal /responses routing-hint branch is not used"),
            "service_tier": "forced to None/omitted before send",
            "responses_lite": "depends on the selected model; body schema remains ResponsesApiRequest",
        },
        {
            "route": "/guardian-classifier",
            "role": "lightweight asynchronous Guardian risk classification",
            "guardian_routing_role": "unmetered classifier route selected by free_guardian when eligibility checks pass",
            "selected_when": "free_guardian=true AND current Codex backend/provider/auth eligibility checks pass",
            "fallback_route": "/responses",
            "transport_policy": "Responses-compatible route; current Luna async scorer uses dedicated pooled Responses WSS",
            "profiles": {
                "http_capability": {"headers": "responses_http_inference", "client_metadata": "responses_base", "request_body": "responses_http_request", "response_events": "responses_events"},
                "current_websocket": {"headers": "guardian_classifier_lite_ws", "client_metadata": "guardian_classifier_response_create", "request_body": "guardian_classifier_response_create", "response_events": "responses_events"},
            },
            "route_overrides": {
                "transport": "current async scorer is dedicated pooled WSS",
                "responses_lite": "always true on current classifier path",
                "service_tier": "None",
                "tools": "None; tool_choice=none; parallel_tool_calls=false",
                "client_metadata": "independent LunaSampler construction, not normal CodexResponsesMetadata::client_metadata()",
            },
            "http_sse": dict(common_http, current_path=False, capability=True, note="shared ResponsesClient can address this route, but the current async classifier path is WebSocket-based"),
            "websocket": dict(
                common_ws,
                current_path=True,
                dedicated_pool=True,
                client_metadata_profile="guardian_classifier_response_create",
                request_body_profile="guardian_classifier_response_create",
                note="current classifier explicitly enables Responses Lite, builds client_metadata independently in LunaSampler, and uses tool-less classifier-specialized values",
            ),
            "service_tier": "current classifier request uses None",
            "responses_lite": "explicitly true; classifier also puts the Lite marker on its dedicated WS handshake",
        },
        {
            "route": "/responses/compact",
            "role": "legacy remote compaction fallback",
            "transport_policy": "HTTP unary only; no Responses WebSocket variant",
            "profiles": {"http": {"headers": "legacy_compact_inference", "application_protocol": "unary_json"}},
            "http_unary": {
                "current_path": True,
                "method": "POST",
                "transport": "HTTPS",
                "request_body": "CompactionInput JSON",
                "request_content_type": "application/json",
                "accept": None,
                "response_framing": "one completed JSON body",
                "headers": sorted(compact_headers),
                "installation_identity": "x-codex-installation-id is a direct HTTP header",
                "client_metadata": None,
                "turn_state": "response header -> request header replay within same turn",
            },
            "websocket": {"current_path": False, "capability": False},
            "replacement": "/responses + compaction_trigger (v2) when enabled",
        },
    ]
    guardian_feature = {
        "name": "free_guardian",
        "kind": "routing_feature_flag",
        "is_endpoint": False,
        "meaning": "route eligible Guardian review/classification traffic through unmetered Codex Guardian routes",
        "normal_route_when_disabled_or_ineligible": "/responses",
        "workloads": {
            "full_review": {
                "normal_route": "/responses",
                "unmetered_route": "/guardian",
                "selected_route_when_enabled_and_eligible": "/guardian",
            },
            "classifier": {
                "normal_route": "/responses",
                "unmetered_route": "/guardian-classifier",
                "selected_route_when_enabled_and_eligible": "/guardian-classifier",
                "current_transport": "dedicated pooled Responses WSS + Responses Lite",
            },
        },
        "eligibility_note": (
            "The flag alone is not sufficient: current source also gates on Codex-backend/auth/provider/session/model conditions; "
            "if those checks fail, the workload remains on /responses."
        ),
        "source": safe_src(
            surface_sources.get("guardian_feature_config"),
            SURFACE_FILES["guardian_feature_config"],
            "pub free_guardian: Option<bool>",
            "GuardianV2Config.free_guardian",
        ) if surface_sources.get("guardian_feature_config") else None,
    }

    return {
        "schema_version": MODEL_MATRIX_SCHEMA_VERSION,
        "scope": "model-facing inference routes only; realtime media and backend/account/provider auxiliary APIs are cataloged separately",
        "guardian_routing_feature": guardian_feature,
        "shared_profiles": shared_profiles,
        "transport_delta": transport_delta,
        "routes": routes,
        "field_movement": {
            "routing_hint": {"http": "HTTP request header", "websocket": "handshake header"},
            "service_tier": {"http": "JSON body", "websocket": "response.create field"},
            "responses_lite": {"http": "HTTP request header", "websocket": "response.create client_metadata; dedicated classifier additionally uses a handshake marker"},
            "turn_state": {"http": "response/request HTTP header", "websocket": "response.create client_metadata after server supplies it"},
            "turn_metadata": {"http": "canonical body client_metadata + bounded compatibility header", "websocket": "canonical response.create client_metadata + handshake compatibility projection"},
            "response_id": {"http": "response.completed event identity only", "websocket": "response.completed identity may be replayed as previous_response_id for incremental continuation"},
            "token_usage": {"http": "response.completed SSE event payload", "websocket": "response.completed JSON frame/event payload", "header": "not a normal usage header"},
            "effective_model": {"http": "openai-model response header", "websocket": "101 handshake header and response metadata event when present"},
            "model_etag": {"http": "X-Models-Etag response header", "websocket": "x-models-etag response metadata event"},
            "reasoning_included": {"http": "x-reasoning-included response header", "websocket": "x-reasoning-included 101 handshake header"},
            "upstream_request_id": {"http": "x-request-id may populate ResponseStream.upstream_request_id", "websocket": "ResponseStream.upstream_request_id=None"},
            "compression": {"http": "optional Content-Encoding: zstd", "websocket": "permessage-deflate WebSocket extension"},
        },
        "route_specific_deltas": {
            "/responses": {"profile": "baseline Responses behavior"},
            "/guardian": {"inherits": "/responses transport/body/client-metadata profiles", "suppresses": ["x-codex-routing-hint", "service_tier"]},
            "/guardian-classifier": {"current_transport": "WSS", "header_profile": "guardian_classifier_lite_ws", "client_metadata_profile": "guardian_classifier_response_create", "request_body_profile": "guardian_classifier_response_create", "always_lite": True},
            "/responses/compact": {"protocol": "legacy unary JSON", "no_websocket": True, "no_normal_client_metadata_body": True},
        },
        "source": {
            "http": safe_src(core_sources.get("http"), FILES["http"], "pub enum ResponsesEndpoint", "ResponsesEndpoint"),
            "websocket": safe_src(core_sources.get("ws"), FILES["ws"], "ResponsesWebsocketConnection", "ResponsesWebsocketConnection"),
            "core": safe_src(core_sources.get("core"), FILES["core"], "WebSocket prewarm", "ModelClientSession WebSocket prewarm"),
            "guardian_classifier": safe_src(surface_sources.get("guardian_sampler"), SURFACE_FILES["guardian_sampler"], "x-openai-internal-codex-responses-lite", "LunaSampler") if surface_sources.get("guardian_sampler") else None,
            "guardian_classifier_route_selector": safe_src(surface_sources.get("guardian_sampler"), SURFACE_FILES["guardian_sampler"], "if self.config.free_guardian", "LunaSampler::responses_endpoint") if surface_sources.get("guardian_sampler") else None,
            "guardian_review_route_selector": safe_src(core_sources.get("core"), FILES["core"], "if self.free_guardian_enabled", "ModelClient::responses_endpoint"),
            "guardian_feature_config": guardian_feature.get("source"),
        },
    }


def build_endpoint_protocol_catalog(
    report: dict[str, Any],
    core_sources: dict[str, str],
    surface_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Cross-endpoint transport/media/header taxonomy.

    This deliberately separates three concepts that are easy to conflate:
    1) HTTP request body media type (Content-Type),
    2) desired/returned response media type (Accept / response Content-Type), and
    3) application framing (unary JSON, SSE, WebSocket frames, SDP/WebRTC).
    """
    surface_sources = surface_sources or {}
    common = core_sources["common"]
    http = core_sources["http"]
    compact = core_sources["compact"]
    core = core_sources["core"]

    responses_body = try_struct_fields(common, FILES["common"], "ResponsesApiRequest")
    responses_ws_body = try_struct_fields(common, FILES["common"], "ResponseCreateWsRequest")
    compact_body = report["responses_compact"].get("request_body_schema") or []
    http_req = report["responses_http"]["request_headers"] + report["responses_http"].get("tracking_request_headers", [])
    http_resp = report["responses_http"].get("response_headers", [])
    http_resp_templates = report["responses_http"].get("response_header_templates", [])
    response_event_metadata = report["responses_http"].get("response_event_metadata", [])
    ws_req = report["responses_websocket"].get("handshake_request_headers", [])
    ws_resp = report["responses_websocket"].get("handshake_response_headers", [])
    compact_req = report["responses_compact"].get("request_headers", [])
    compact_resp = report["responses_compact"].get("response_headers", [])
    account_req = backend_account_headers(report["account_status_check"].get("request_headers", []))
    json_content_type = [h for h in report["responses_http"]["request_headers"] if h["name"].lower() == "content-type"]
    guardian_ws_headers = [
        h for h in ws_req
        if h["name"].lower() in {
            "session-id", "thread-id", "x-client-request-id", "x-codex-window-id",
            "x-openai-subagent", "openai-beta", "authorization", "chatgpt-account-id",
            "x-openai-fedramp", "user-agent", "originator", "version",
            "x-openai-internal-codex-residency", "openai-organization", "openai-project",
        }
    ]

    image_ep = surface_sources.get("images_endpoint")
    image_types = surface_sources.get("images_types")
    search_ep = surface_sources.get("search_endpoint")
    search_types = surface_sources.get("search_types")
    memories_ep = surface_sources.get("memories_endpoint")
    realtime_call = surface_sources.get("realtime_call")
    realtime_ws = surface_sources.get("realtime_ws")
    realtime_protocol = surface_sources.get("realtime_protocol")
    guardian = surface_sources.get("guardian_sampler")
    guardian_ticket = surface_sources.get("guardian_ticket")
    rate_limits = surface_sources.get("rate_limits")
    backend_types = surface_sources.get("backend_types")
    thread_usage = surface_sources.get("thread_usage")
    models_ep = surface_sources.get("models_endpoint")
    models_protocol = surface_sources.get("models_protocol")
    model_provider_models = surface_sources.get("model_provider_models")
    files_api = surface_sources.get("files_api")
    mcp_openai_file = surface_sources.get("mcp_openai_file")
    chatgpt_turn_cost = surface_sources.get("chatgpt_turn_cost")
    api_key_turn_cost = surface_sources.get("api_key_turn_cost")

    file_auth_headers = [
        dict(header)
        for header in account_req
        if str(header.get("name", "")).lower()
        in {"authorization", "chatgpt-account-id", "x-openai-fedramp"}
    ]
    api_key_cost_headers = [
        dict(header)
        for header in http_req
        if str(header.get("name", "")).lower()
        in {"authorization", "openai-organization", "openai-project"}
    ]

    guardian_classifier_headers = list(guardian_ws_headers)
    if guardian:
        guardian_classifier_headers.append(
            catalog_header(
                "x-openai-internal-codex-responses-lite",
                condition="always on current Luna classifier WebSocket handshake",
                value_shape="true",
                category="feature",
                optional=False,
                source=safe_src(guardian, SURFACE_FILES["guardian_sampler"], "x-openai-internal-codex-responses-lite", "LunaSampler::open_connection"),
            )
        )
    shared_profiles = build_shared_wire_profiles(
        report, core_sources, surface_sources, guardian_classifier_headers
    )

    surfaces: list[dict[str, Any]] = []
    surfaces.append(_surface(
        surface_id="responses_http_sse",
        category="inference.responses",
        endpoints=["/responses", "/guardian"],
        method="POST",
        transport="HTTPS",
        protocol="Responses API over HTTP + SSE",
        streaming=True,
        request_content_type="application/json",
        request_accept="text/event-stream",
        request_framing="one JSON HTTP request body",
        response_media_type="text/event-stream",
        response_framing="SSE records; each data payload is JSON",
        body_schema="ResponsesApiRequest",
        body_fields=responses_body,
        request_headers=http_req,
        response_headers=http_resp,
        response_header_templates=http_resp_templates,
        response_event_metadata=response_event_metadata,
        responses_compatible=True,
        variants=[
            {
                "name": "normal Responses",
                "endpoint": "/responses",
                "routing_hint": "x-codex-routing-hint may be present on first-party Codex backend",
                "service_tier": "may be serialized when supported and non-default",
                "responses_lite": "optional; HTTP marker x-openai-internal-codex-responses-lite: true",
            },
            {
                "name": "full Guardian route",
                "endpoint": "/guardian",
                "routing_hint": "not added by the normal Responses routing-hint branch",
                "service_tier": "forced to null/omitted before send",
                "responses_lite": "depends on selected model; same Responses request machinery",
            },
            {
                "name": "Guardian classifier HTTP capability (not current async classifier path)",
                "endpoint": "/guardian-classifier",
                "current_http_path": False,
                "client_capability": True,
                "routing_hint": "not a normal /responses routing-hint path",
                "service_tier": "current Luna classifier request uses none",
                "responses_lite": "current async classifier explicitly uses Lite over Responses WebSocket",
            },
        ],
        notes=[
            "Content-Type describes the JSON request body; Accept describes the desired SSE response.",
            "The same ResponsesClient selects /responses, /guardian, or /guardian-classifier by endpoint enum.",
            "Internal route-selection eligibility/config flag names are intentionally omitted from the emitted report.",
        ],
        source=safe_src(http, FILES["http"], "pub enum ResponsesEndpoint", "ResponsesEndpoint"),
        plane="model_inference",
        service_family="responses_compatible_inference",
        header_profile="responses_http_inference",
        inherits_inference_headers=True,
        application_metadata={
            "request_body_client_metadata": report["responses_http"].get("client_metadata", []),
            "canonical_turn_metadata": 'client_metadata["x-codex-turn-metadata"]',
            "header_scope": "per HTTP request",
            "response_event_usage": "token usage is carried by response.completed event payload, not a usage HTTP header",
            "response_event_identity": "response.completed carries response.id",
        },
        transport_generated_headers=["Host", "Content-Length or Transfer-Encoding", "Accept-Encoding"],
        profile_refs={
            "headers": "responses_http_inference",
            "client_metadata": "responses_base",
            "request_body": "responses_http_request",
            "response_events": "responses_events",
            "application_protocol": "responses_events",
        },
    ))

    surfaces.append(_surface(
        surface_id="responses_websocket",
        category="inference.responses",
        endpoints=["/responses", "/guardian", "/guardian-classifier"],
        method="GET upgrade / persistent frames",
        transport="WSS",
        protocol="Responses API over WebSocket",
        streaming=True,
        request_content_type=None,
        request_accept=None,
        request_framing="HTTP Upgrade handshake, then JSON WebSocket text frames (response.create)",
        response_media_type=None,
        response_framing="JSON WebSocket text frames; no HTTP media type per frame",
        body_schema="ResponseCreateWsRequest",
        body_fields=responses_ws_body,
        request_headers=ws_req,
        response_headers=ws_resp,
        response_header_templates=[],
        response_event_metadata=response_event_metadata,
        responses_compatible=True,
        variants=[
            {
                "name": "Responses Lite marker",
                "http": "x-openai-internal-codex-responses-lite: true",
                "websocket": "response.create.client_metadata[ws_request_header_x_openai_internal_codex_responses_lite]=true",
            },
            {
                "name": "continuation",
                "websocket": "previous_response_id replays completed response.id when incremental reuse is valid",
                "turn_state": "x-codex-turn-state rides response.create client_metadata after server supplies it",
            },
        ],
        notes=[
            "Accept and Content-Type are handshake-level HTTP concepts; they are not repeated on each WebSocket frame.",
            "session-id/thread-id are handshake headers while session_id/thread_id also appear in each response.create client_metadata.",
        ],
        source=safe_src(core_sources["ws"], FILES["ws"], "pub struct ResponsesWebsocketClient", "ResponsesWebsocketClient"),
        plane="model_inference",
        service_family="responses_compatible_inference",
        address_kind="websocket_endpoint",
        header_profile="responses_ws_handshake",
        inherits_inference_headers=True,
        application_metadata={
            "request_envelope": "response.create",
            "base_client_metadata": report["responses_websocket"].get("client_metadata_base", []),
            "ws_client_metadata_additions": report["responses_websocket"].get("client_metadata_ws_additions", []),
            "continuation_fields": report["responses_websocket"].get("request_body_continuation", []),
            "response_event_metadata": report["responses_websocket"].get("response_event_metadata", []),
            "header_scope": "handshake only; response.create carries per-request metadata",
            "response_event_usage": "token usage is carried by response.completed JSON event/frame, not a usage handshake header",
        },
        transport_generated_headers=["Host", "Connection: Upgrade", "Upgrade: websocket", "Sec-WebSocket-Key", "Sec-WebSocket-Version"],
        profile_refs={
            "headers": "responses_ws_handshake",
            "client_metadata": "responses_ws_response_create",
            "request_body": "responses_ws_response_create",
            "response_events": "responses_events",
            "application_protocol": "responses_events",
        },
    ))

    if guardian:
        surfaces.append(_surface(
            surface_id="guardian_classifier_lite_ws",
            category="inference.guardian_classifier",
            endpoints=["/guardian-classifier", "/responses (fallback when Guardian classifier routing is not selected)"],
            method="WebSocket upgrade + response.create",
            transport="WSS",
            protocol="Responses Lite over dedicated pooled Responses WebSockets",
            streaming=True,
            request_content_type=None,
            request_accept=None,
            request_framing="JSON text frames; tool-less classifier request",
            response_media_type=None,
            response_framing="Responses WebSocket events",
            body_schema="ResponseCreateWsRequest (from classifier-specialized ResponsesApiRequest)",
            body_fields=responses_ws_body,
            request_headers=guardian_classifier_headers,
            response_headers=[],
            responses_compatible=True,
            variants=[{
                "model": "gpt-5.6-luna (current source constant when present)",
                "tools": None,
                "tool_choice": "none",
                "parallel_tool_calls": False,
                "stream": True,
                "service_tier": None,
                "responses_lite": True,
                "handshake_marker": "x-openai-internal-codex-responses-lite: true",
                "subagent": "x-openai-subagent: guardian",
            }],
            notes=[
                "This is not a separate body schema: it instantiates ResponsesApiRequest with classifier-specific values.",
                "The current classifier pool uses WebSockets even though /guardian-classifier is a Responses-compatible route.",
            ],
            source=safe_src(guardian, SURFACE_FILES["guardian_sampler"], "x-openai-internal-codex-responses-lite", "LunaSampler::open_connection"),
            plane="model_inference",
            service_family="guardian_classifier",
            address_kind="websocket_endpoint",
            header_profile="guardian_classifier_lite_ws",
            inherits_inference_headers=True,
            application_metadata={
                "request_envelope": "response.create",
                "lite": True,
                "tools": None,
                "tool_choice": "none",
                "client_metadata_profile": shared_profiles["client_metadata_profiles"]["guardian_classifier_response_create"],
                "per_request_metadata": "classifier response.create client_metadata is independently constructed in LunaSampler",
            },
            transport_generated_headers=["Host", "Connection: Upgrade", "Upgrade: websocket", "Sec-WebSocket-Key", "Sec-WebSocket-Version"],
            profile_refs={
                "headers": "guardian_classifier_lite_ws",
                "client_metadata": "guardian_classifier_response_create",
                "request_body": "guardian_classifier_response_create",
                "response_events": "responses_events",
                "application_protocol": "responses_events",
            },
        ))

    surfaces.append(_surface(
        surface_id="responses_compact_legacy",
        category="inference.compaction.legacy",
        endpoints=["/responses/compact"],
        method="POST",
        transport="HTTPS",
        protocol="unary HTTP JSON",
        streaming=False,
        request_content_type="application/json",
        request_accept=None,
        request_framing="one JSON body",
        response_media_type="application/json (parsed as one completed body)",
        response_framing="unary JSON",
        body_schema="CompactionInput",
        body_fields=compact_body,
        request_headers=compact_req,
        response_headers=compact_resp,
        variants=[{
            "replacement": "/responses + compaction_trigger is the preferred v2 path when enabled",
            "installation_identity": "x-codex-installation-id is a direct legacy compact header",
        }],
        notes=[
            "No SSE stream: the timeout covers the complete unary response.",
            "Legacy compact has no normal Responses client_metadata body field.",
        ],
        source=safe_src(compact, FILES["compact"], '"responses/compact"', "CompactClient::path"),
        plane="model_inference",
        service_family="responses_compaction_legacy",
        header_profile="legacy_compact_inference",
        inherits_inference_headers=True,
        profile_refs={
            "headers": "legacy_compact_inference",
            "application_protocol": "unary_json",
        },
    ))

    if image_ep:
        surfaces.append(_surface(
            surface_id="images_unary_json",
            category="media.image",
            endpoints=["/images/generations", "/images/edits"],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one typed JSON request body",
            response_media_type="application/json (decoded as one completed body)",
            response_framing="unary JSON",
            body_schema="ImageGenerationRequest | ImageEditRequest",
            body_fields=(
                try_struct_fields(image_types, SURFACE_FILES["images_types"], "ImageGenerationRequest")
                + try_struct_fields(image_types, SURFACE_FILES["images_types"], "ImageEditRequest")
            ),
            request_headers=json_content_type,
            response_headers=[
                catalog_header(
                    "x-codex-imagegen-request-id",
                    condition="read from image response when present",
                    category="observability",
                    direction="response",
                    origin="server_or_infrastructure",
                    source=safe_src(image_ep, SURFACE_FILES["images_endpoint"], "X_CODEX_IMAGEGEN_REQUEST_ID_HEADER", "ImagesClient"),
                )
            ],
            variants=[{
                "response_header": "x-codex-imagegen-request-id is read when present",
                "accept": "no endpoint-specific Accept: application/json insertion in ImagesClient",
            }],
            notes=[
                "JSON media type does not make this a Responses request; image endpoints use their own typed schema.",
                "EndpointSession/auth/provider/caller extra headers are outside the image-specific module.",
            ],
            source=safe_src(image_ep, SURFACE_FILES["images_endpoint"], '"images/generations"', "ImagesClient"),
            plane="provider_auxiliary",
            service_family="image_api",
            header_profile="endpoint_session_json + caller/provider/auth headers",
            inherits_inference_headers=False,
        ))

    if search_ep:
        surfaces.append(_surface(
            surface_id="search_unary_json",
            category="search",
            endpoints=["/alpha/search"],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one SearchRequest JSON body",
            response_media_type="application/json",
            response_framing="unary JSON",
            body_schema="SearchRequest",
            body_fields=try_struct_fields(search_types, SURFACE_FILES["search_types"], "SearchRequest"),
            request_headers=json_content_type,
            variants=[],
            notes=["No SSE or Responses client_metadata framing in SearchClient."],
            source=safe_src(search_ep, SURFACE_FILES["search_endpoint"], '"alpha/search"', "SearchClient::path"),
            plane="provider_auxiliary",
            service_family="search_api",
            header_profile="endpoint_session_json + caller/provider/auth headers",
            inherits_inference_headers=False,
        ))

    if memories_ep:
        surfaces.append(_surface(
            surface_id="memories_trace_summarize",
            category="memory",
            endpoints=["/memories/trace_summarize"],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one memory summarization JSON body",
            response_media_type="application/json",
            response_framing="unary JSON",
            body_schema="MemorySummarizeInput",
            body_fields=try_struct_fields(common, FILES["common"], "MemorySummarizeInput"),
            request_headers=json_content_type,
            variants=[],
            notes=["Separate memory service endpoint, not the normal /responses stream."],
            source=safe_src(memories_ep, SURFACE_FILES["memories_endpoint"], '"memories/trace_summarize"', "MemoriesClient::path"),
            plane="provider_auxiliary",
            service_family="memory_service",
            header_profile="endpoint_session_json + caller/provider/auth headers",
            inherits_inference_headers=False,
        ))

    if models_ep:
        surfaces.append(_surface(
            surface_id="models_list",
            category="provider.models",
            endpoints=["/models?client_version=<client-version>"],
            method="GET",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type=None,
            request_accept=None,
            request_framing="no request body; client_version is appended as a query parameter",
            response_media_type="application/json",
            response_framing="one completed JSON body",
            body_schema=None,
            response_body_schema="ModelsResponse containing ModelInfo[]",
            response_body_fields=(
                try_struct_fields(models_protocol, SURFACE_FILES["models_protocol"], "ModelsResponse")
                + try_struct_fields(models_protocol, SURFACE_FILES["models_protocol"], "ModelInfo")
            ),
            response_headers=[
                catalog_header(
                    "ETag",
                    condition="standard HTTP model-catalog validator returned by the models endpoint",
                    category="cache_validator",
                    direction="response",
                    origin="server",
                    source=safe_src(models_ep, SURFACE_FILES["models_endpoint"], "http::header::ETAG", "ModelsClient::list_models"),
                )
            ],
            variants=[{
                "query_parameter": "client_version",
                "value": "the running Codex client version",
                "note": "standard ETag here is distinct from X-Models-Etag on Responses traffic",
            }],
            notes=[
                "Provider/auth/default headers are composed through EndpointSession and the route-aware model-provider client.",
                "This is model discovery, not a model inference request and not a Responses event stream.",
            ],
            source=safe_src(models_ep, SURFACE_FILES["models_endpoint"], '"models"', "ModelsClient::path"),
            plane="provider_auxiliary",
            service_family="model_catalog",
            header_profile="endpoint_session_get + caller/provider/auth headers",
            inherits_inference_headers=False,
            trust_domain="configured model provider or Codex backend",
            profile_refs={"application_protocol": "unary_json"},
            application_metadata={
                "query": {
                    "client_version": {
                        "required": True,
                        "source": safe_src(models_ep, SURFACE_FILES["models_endpoint"], "append_client_version_query", "ModelsClient::request_url"),
                    }
                },
                "refresh_timeout": {
                    "value": "5 seconds in current model-provider client",
                    "source": safe_src(model_provider_models, SURFACE_FILES["model_provider_models"], "MODELS_REFRESH_TIMEOUT", "OpenAiModelsEndpoint"),
                },
                "managed_residency": {
                    "enforced": True,
                    "source": safe_src(model_provider_models, SURFACE_FILES["model_provider_models"], "enforce_managed_residency", "OpenAiModelsEndpoint::list_models"),
                },
            },
        ))

    if files_api:
        create_fields = [
            catalog_field("file_name", "String", source=safe_src(files_api, SURFACE_FILES["files_api"], '"file_name"', "upload_openai_file create request")),
            catalog_field("file_size", "u64", source=safe_src(files_api, SURFACE_FILES["files_api"], '"file_size"', "upload_openai_file create request")),
            catalog_field("use_case", "String", source=safe_src(files_api, SURFACE_FILES["files_api"], '"use_case"', "upload_openai_file create request")),
            catalog_field("codex_connector_id", "String", optional=True, source=safe_src(files_api, SURFACE_FILES["files_api"], '"codex_connector_id"', "hosted upload context")),
            catalog_field("codex_action_name", "String", optional=True, source=safe_src(files_api, SURFACE_FILES["files_api"], '"codex_action_name"', "hosted upload context")),
            catalog_field("codex_model", "String", optional=True, source=safe_src(files_api, SURFACE_FILES["files_api"], '"codex_model"', "hosted upload context")),
        ]
        surfaces.append(_surface(
            surface_id="hosted_file_create",
            category="backend.file_upload",
            endpoints=["{chatgpt_base_url}/files"],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one file-registration JSON body",
            response_media_type="application/json",
            response_framing="one completed JSON body",
            body_schema="CreateFileRequest (constructed JSON)",
            body_fields=create_fields,
            response_body_schema="CreateFileResponse",
            response_body_fields=try_struct_fields(files_api, SURFACE_FILES["files_api"], "CreateFileResponse"),
            request_headers=file_auth_headers + [
                catalog_header(
                    "Content-Type",
                    condition="HTTP client JSON serializer for the file-registration body",
                    value_shape="application/json",
                    category="http",
                    optional=False,
                    source=safe_src(files_api, SURFACE_FILES["files_api"], ".json(&create_request)", "upload_openai_file create request"),
                )
            ],
            variants=[{
                "hosted_upload_context": "adds codex_connector_id, codex_action_name, and codex_model",
                "ordinary_upload": "omits hosted context fields",
            }],
            notes=[
                "The response supplies an upload_url that crosses into a separately authorized object-storage trust domain.",
                "The create request is authenticated; the subsequent signed blob PUT does not reuse those auth headers.",
            ],
            source=safe_src(files_api, SURFACE_FILES["files_api"], 'format!("{}/files"', "upload_openai_file create"),
            plane="backend_control",
            service_family="hosted_file_upload",
            header_profile="file_backend_auth + json_body",
            inherits_inference_headers=False,
            trust_domain="Codex/ChatGPT backend",
            excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
            application_metadata={
                "next_surface": "hosted_file_blob_upload",
                "authority_transition": "server-issued upload_url",
                "consumer": safe_src(mcp_openai_file, SURFACE_FILES["mcp_openai_file"], "upload_openai_file(", "MCP Apps file argument bridge"),
            },
        ))

        blob_request_headers = [
            catalog_header(
                "Content-Length",
                condition="declared file size for the streamed blob body",
                value_shape="u64 bytes",
                category="http",
                optional=False,
                source=safe_src(files_api, SURFACE_FILES["files_api"], ".header(CONTENT_LENGTH", "blob upload"),
            ),
            catalog_header(
                "x-ms-blob-type",
                condition="Azure-compatible object storage block blob upload",
                value_shape="BlockBlob",
                category="object_storage",
                optional=False,
                source=safe_src(files_api, SURFACE_FILES["files_api"], '"x-ms-blob-type"', "blob upload"),
            ),
            catalog_header(
                "x-ms-client-request-id",
                condition="one UUID generated for object-storage upload diagnostics",
                value_shape="UUID v4",
                category="observability",
                optional=False,
                source=safe_src(files_api, SURFACE_FILES["files_api"], '"x-ms-client-request-id"', "blob upload"),
            ),
        ]
        blob_response_headers = [
            catalog_header(
                "cf-ray",
                condition="captured for failed object-storage responses when present",
                category="infrastructure_debug",
                direction="response",
                origin="server_or_infrastructure",
                source=safe_src(files_api, SURFACE_FILES["files_api"], '"cf-ray"', "blob upload response diagnostics"),
            ),
            catalog_header(
                "x-ms-request-id",
                condition="object-storage request id captured on failed status",
                category="observability",
                direction="response",
                origin="server",
                source=safe_src(files_api, SURFACE_FILES["files_api"], '"x-ms-request-id"', "blob upload response diagnostics"),
            ),
            catalog_header(
                "x-ms-error-code",
                condition="object-storage error code captured on failed status",
                category="object_storage_error",
                direction="response",
                origin="server",
                source=safe_src(files_api, SURFACE_FILES["files_api"], '"x-ms-error-code"', "blob upload response diagnostics"),
            ),
        ]
        surfaces.append(_surface(
            surface_id="hosted_file_blob_upload",
            category="object_storage.file_upload",
            endpoints=["<server-issued upload_url>"],
            method="PUT",
            transport="HTTPS",
            protocol="streamed binary HTTP upload",
            streaming=True,
            request_content_type=None,
            request_accept=None,
            request_framing="streamed file bytes with a declared Content-Length",
            response_media_type=None,
            response_framing="status and diagnostic headers; response body is not part of the success contract",
            body_schema="binary file byte stream",
            request_headers=blob_request_headers,
            response_headers=blob_response_headers,
            notes=[
                "The URL is an opaque server-issued capability; Codex backend Authorization/ChatGPT-Account-ID headers are deliberately not attached.",
                "Request streaming here is body streaming, not an SSE or WebSocket application protocol.",
            ],
            source=safe_src(files_api, SURFACE_FILES["files_api"], ".body_stream(contents)", "upload_openai_file blob PUT"),
            plane="external_storage",
            service_family="hosted_file_upload",
            header_profile="signed_blob_upload",
            inherits_inference_headers=False,
            trust_domain="server-selected object-storage host",
            connection_lifetime="single upload request",
            application_metadata={
                "previous_surface": "hosted_file_create",
                "next_surface": "hosted_file_finalize",
                "credential": "signed/opaque upload_url rather than Codex auth headers",
            },
        ))

        surfaces.append(_surface(
            surface_id="hosted_file_finalize",
            category="backend.file_upload",
            endpoints=["{chatgpt_base_url}/files/{file_id}/uploaded"],
            method="POST (polled while status=retry)",
            transport="HTTPS",
            protocol="unary HTTP JSON with bounded polling",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="{} or {pdf_c2pa_create_request: <create request>} JSON",
            response_media_type="application/json",
            response_framing="one status JSON body per polling attempt",
            body_schema="FinalizeFileRequest (constructed JSON)",
            body_fields=[
                catalog_field(
                    "pdf_c2pa_create_request",
                    "CreateFileRequest",
                    optional=True,
                    source=safe_src(files_api, SURFACE_FILES["files_api"], '"pdf_c2pa_create_request"', "upload finalization"),
                )
            ],
            response_body_schema="DownloadLinkResponse",
            response_body_fields=try_struct_fields(files_api, SURFACE_FILES["files_api"], "DownloadLinkResponse"),
            request_headers=file_auth_headers + [
                catalog_header(
                    "Content-Type",
                    condition="HTTP client JSON serializer for each finalization poll",
                    value_shape="application/json",
                    category="http",
                    optional=False,
                    source=safe_src(files_api, SURFACE_FILES["files_api"], ".json(&finalize_request)", "upload finalization"),
                )
            ],
            variants=[
                {"status": "success", "result": "download_url, file_name, mime_type, file_size_bytes"},
                {"status": "retry", "action": "sleep then repeat until the finalization timeout"},
                {"status": "other", "result": "UploadFailed using error_message or fallback text"},
            ],
            notes=[
                "The returned canonical local reference is sediment://{file_id}; the downstream Apps payload receives file_id and download_url.",
                "Polling is bounded by OPENAI_FILE_FINALIZE_TIMEOUT.",
            ],
            source=safe_src(files_api, SURFACE_FILES["files_api"], '"/files/{}/uploaded"', "upload_openai_file finalization"),
            plane="backend_control",
            service_family="hosted_file_upload",
            header_profile="file_backend_auth + json_body",
            inherits_inference_headers=False,
            trust_domain="Codex/ChatGPT backend",
            connection_lifetime="bounded sequence of unary polling requests",
            excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
            application_metadata={
                "previous_surface": "hosted_file_blob_upload",
                "canonical_uri_prefix": "sediment://",
            },
        ))

    if rate_limits:
        surfaces.append(_surface(
            surface_id="account_rate_limits",
            category="backend.account.usage",
            endpoints=["/api/codex/usage", "/backend-api/wham/usage"],
            method="GET",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type=None,
            request_accept=None,
            request_framing="no request body",
            response_media_type="application/json (decoded from completed body)",
            response_framing="unary JSON",
            body_schema=None,
            response_body_schema="RateLimitStatusWithResetCredits",
            response_body_fields=try_struct_fields(backend_types, SURFACE_FILES["backend_types"], "RateLimitStatusWithResetCredits"),
            request_headers=account_req + [
                catalog_header(
                    "x-openai-codex-luna-reserve",
                    condition="only when caller opts into Reserve-aware usage",
                    value_shape="1",
                    category="feature",
                    optional=True,
                    source=safe_src(rate_limits, SURFACE_FILES["rate_limits"], "x-openai-codex-luna-reserve", "Client::get_rate_limit_status"),
                )
            ],
            variants=[{
                "optional_request_header": "x-openai-codex-luna-reserve: 1 when caller opts into Reserve-aware usage",
                "slash_command_relation": "/status and /usage may refresh this through app-server account/rateLimits/read",
                "reset_credit_summary": "usage response can carry reset-credit summary; app-server may separately fetch detailed reset-credit rows",
            }],
            notes=[
                "ChatGPT/Codex backend account-control HTTP, not model-provider inference.",
                "The backend client does not explicitly set Accept: application/json on this GET.",
                "No Responses turn/session/routing metadata is inherited by this header profile.",
            ],
            source=safe_src(rate_limits, SURFACE_FILES["rate_limits"], "fn rate_limit_status_url", "Client::rate_limit_status_url"),
            plane="backend_control",
            service_family="chatgpt_backend.account_usage",
            header_profile="backend_account_common + optional luna-reserve",
            inherits_inference_headers=False,
            profile_refs={"headers": "backend_account_common", "application_protocol": "unary_json"},
            excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
        ))

    if rate_limits:
        surfaces.append(_surface(
            surface_id="account_rate_limit_reset_credits_list",
            category="backend.account.reset_credit",
            endpoints=["/api/codex/rate-limit-reset-credits", "/backend-api/wham/rate-limit-reset-credits"],
            method="GET",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type=None,
            request_accept=None,
            request_framing="no request body",
            response_media_type="application/json",
            response_framing="unary JSON",
            body_schema=None,
            response_body_schema="RateLimitResetCreditsDetails",
            response_body_fields=try_struct_fields(backend_types, SURFACE_FILES["backend_types"], "RateLimitResetCreditsDetails"),
            request_headers=account_req,
            variants=[{
                "app_server_relation": "account/rateLimits/read may issue this best-effort detail fetch after /usage",
                "fallback": "if this detail request fails/times out, app-server falls back to reset-credit data from the usage response",
            }],
            notes=[
                "ChatGPT/Codex backend account-control HTTP, not model-provider inference.",
                "This is a separate reset-credit detail route; it is not /responses and it is not SSE.",
                "No explicit Accept: application/json is added by the backend client.",
                "No x-codex-* inference metadata is added by BackendClient::headers().",
            ],
            source=safe_src(rate_limits, SURFACE_FILES["rate_limits"], "fn rate_limit_reset_credits_url", "Client::rate_limit_reset_credits_url"),
            plane="backend_control",
            service_family="chatgpt_backend.account_reset_credit",
            header_profile="backend_account_common",
            inherits_inference_headers=False,
            profile_refs={"headers": "backend_account_common", "application_protocol": "unary_json"},
            excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
        ))

        surfaces.append(_surface(
            surface_id="account_rate_limit_reset_credit_consume",
            category="backend.account.reset_credit",
            endpoints=["/api/codex/rate-limit-reset-credits/consume", "/backend-api/wham/rate-limit-reset-credits/consume"],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one JSON body containing redeem_request_id and optional credit_id",
            response_media_type="application/json",
            response_framing="unary JSON",
            body_schema="ConsumeRateLimitResetCreditRequest",
            body_fields=try_struct_fields(rate_limits, SURFACE_FILES["rate_limits"], "ConsumeRateLimitResetCreditRequest"),
            response_body_schema="ConsumeRateLimitResetCreditResponse",
            response_body_fields=try_struct_fields(backend_types, SURFACE_FILES["backend_types"], "ConsumeRateLimitResetCreditResponse"),
            request_headers=account_req + [
                backend_json_content_type(
                    rate_limits,
                    SURFACE_FILES["rate_limits"],
                    ".header(CONTENT_TYPE",
                    "Client::consume_rate_limit_reset_credit_request",
                )
            ],
            variants=[{
                "app_server_method": "account/rateLimitResetCredit/consume",
                "app_server_params": "idempotencyKey + optional creditId",
                "backend_body_translation": "idempotencyKey -> redeem_request_id; creditId -> credit_id",
                "recommended_follow_up": "refetch account/rateLimits/read after consuming a reset",
            }],
            notes=[
                "ChatGPT/Codex backend account-control HTTP, not model-provider inference.",
                "The consume request explicitly sets Content-Type: application/json.",
                "The backend client still does not explicitly set Accept: application/json.",
                "No Responses turn/session/routing metadata is inherited by this request.",
            ],
            source=safe_src(rate_limits, SURFACE_FILES["rate_limits"], "fn consume_rate_limit_reset_credit_url", "Client::consume_rate_limit_reset_credit_url"),
            plane="backend_control",
            service_family="chatgpt_backend.account_reset_credit",
            header_profile="backend_account_common + json_body",
            inherits_inference_headers=False,
            excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
        ))

    surfaces.append(_surface(
        surface_id="account_token_activity",
        category="backend.account.token_activity",
        endpoints=["/api/codex/profiles/me", "/backend-api/wham/profiles/me"],
        method="GET",
        transport="HTTPS",
        protocol="unary HTTP JSON",
        streaming=False,
        request_content_type=None,
        request_accept=None,
        request_framing="no request body",
        response_media_type="application/json (decoded from completed body)",
        response_framing="unary JSON",
        body_schema=None,
        response_body_schema="TokenUsageProfile",
        response_body_fields=try_struct_fields(backend_types, SURFACE_FILES["backend_types"], "TokenUsageProfile"),
        request_headers=account_req,
        variants=[{"slash_command_relation": "/usage -> app-server account/usage/read -> token usage profile GET"}],
        notes=[
            "ChatGPT/Codex backend account-control HTTP, not model-provider inference.",
            "No explicit Accept: application/json insertion in BackendClient::get_token_usage_profile.",
            "No x-codex-* Responses metadata is added by this backend client path.",
        ],
        source=safe_src(core_sources["account"], FILES["account"], "fn token_usage_profile_url", "Client::token_usage_profile_url"),
        plane="backend_control",
        service_family="chatgpt_backend.account_token_activity",
        header_profile="backend_account_common",
        inherits_inference_headers=False,
        profile_refs={"headers": "backend_account_common", "application_protocol": "unary_json"},
        excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
    ))

    if thread_usage:
        surfaces.append(_surface(
            surface_id="thread_usage_query",
            category="backend.account.thread_usage",
            endpoints=["/api/codex/usage/thread_usage/query", "/backend-api/wham/usage/thread_usage/query"],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one JSON body containing thread_ids",
            response_media_type="application/json",
            response_framing="unary JSON",
            body_schema="ThreadUsageQueryRequest",
            body_fields=try_struct_fields(thread_usage, SURFACE_FILES["thread_usage"], "ThreadUsageQueryRequest"),
            response_body_schema="ThreadUsageQueryResponse / ThreadUsage",
            response_body_fields=(
                try_struct_fields(thread_usage, SURFACE_FILES["thread_usage"], "ThreadUsageQueryResponse")
                + try_struct_fields(thread_usage, SURFACE_FILES["thread_usage"], "ThreadUsage")
            ),
            request_headers=account_req + [
                backend_json_content_type(
                    thread_usage,
                    SURFACE_FILES["thread_usage"],
                    ".header(CONTENT_TYPE",
                    "Client::get_thread_usage",
                )
            ],
            variants=[],
            notes=[
                "ChatGPT/Codex backend account-control HTTP, not model-provider inference.",
                "Used for authoritative estimated per-thread credit/token totals on supported status surfaces.",
                "No Codex Responses turn/routing headers are inherited.",
            ],
            source=safe_src(thread_usage, SURFACE_FILES["thread_usage"], "fn thread_usage_url", "Client::thread_usage_url"),
            plane="backend_control",
            service_family="chatgpt_backend.thread_usage",
            header_profile="backend_account_common + json_body",
            inherits_inference_headers=False,
            excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
        ))

    if chatgpt_turn_cost:
        surfaces.append(_surface(
            surface_id="chatgpt_turn_cost_estimates",
            category="backend.account.turn_cost",
            endpoints=[
                "/api/codex/usage/thread-estimates/query",
                "/backend-api/wham/usage/thread-estimates/query",
            ],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one JSON body grouping turn_ids by thread_id",
            response_media_type="application/json",
            response_framing="one completed JSON body",
            body_schema="TurnCostsRequest / ThreadTurnIds",
            body_fields=(
                try_struct_fields(chatgpt_turn_cost, SURFACE_FILES["chatgpt_turn_cost"], "TurnCostsRequest")
                + try_struct_fields(chatgpt_turn_cost, SURFACE_FILES["chatgpt_turn_cost"], "ThreadTurnIds")
            ),
            response_body_schema="TurnCostsResponse / ChatgptThreadTurnCosts / ChatgptTurnCost",
            response_body_fields=(
                try_struct_fields(chatgpt_turn_cost, SURFACE_FILES["chatgpt_turn_cost"], "TurnCostsResponse")
                + try_struct_fields(chatgpt_turn_cost, SURFACE_FILES["chatgpt_turn_cost"], "ChatgptThreadTurnCosts")
                + try_struct_fields(chatgpt_turn_cost, SURFACE_FILES["chatgpt_turn_cost"], "ChatgptTurnCost")
            ),
            request_headers=account_req + [
                catalog_header(
                    "Content-Type",
                    condition="HTTP client JSON serializer for turn-estimate query",
                    value_shape="application/json",
                    category="http",
                    optional=False,
                    source=safe_src(chatgpt_turn_cost, SURFACE_FILES["chatgpt_turn_cost"], ".json(&TurnCostsRequest", "Client::query_chatgpt_turn_costs"),
                )
            ],
            variants=[{
                "include_settled_response_ids": True,
                "estimated_amount": "estimated_usage_usd_micros (optional)",
                "association": "thread_id -> turn_id[]",
            }],
            notes=[
                "Workspace-visible ChatGPT estimate path; this is distinct from token/credit thread_usage query.",
                "No Responses turn/routing metadata is inherited.",
            ],
            source=safe_src(chatgpt_turn_cost, SURFACE_FILES["chatgpt_turn_cost"], "thread-estimates/query", "Client::query_chatgpt_turn_costs"),
            plane="backend_control",
            service_family="chatgpt_backend.turn_cost_estimates",
            header_profile="backend_account_common + json_body",
            inherits_inference_headers=False,
            excluded_header_names=list(INFERENCE_ONLY_HEADER_NAMES),
            trust_domain="Codex/ChatGPT backend",
            profile_refs={"headers": "backend_account_common", "application_protocol": "unary_json"},
        ))

    if api_key_turn_cost:
        surfaces.append(_surface(
            surface_id="api_key_turn_costs",
            category="analytics.turn_cost",
            endpoints=["https://api.chatgpt.com/v1/analytics/codex/turn-costs"],
            method="POST",
            transport="HTTPS",
            protocol="unary HTTP JSON",
            streaming=False,
            request_content_type="application/json",
            request_accept=None,
            request_framing="one JSON body containing turn_ids",
            response_media_type="application/json",
            response_framing="one completed JSON body",
            body_schema="ApiKeyTurnCostsRequest",
            body_fields=try_struct_fields(api_key_turn_cost, SURFACE_FILES["api_key_turn_cost"], "ApiKeyTurnCostsRequest"),
            response_body_schema="ApiKeyTurnCostsResponse / ApiKeyTurnCost / ApiKeyResponseCost",
            response_body_fields=(
                try_struct_fields(api_key_turn_cost, SURFACE_FILES["api_key_turn_cost"], "ApiKeyTurnCostsResponse")
                + try_struct_fields(api_key_turn_cost, SURFACE_FILES["api_key_turn_cost"], "ApiKeyTurnCost")
                + try_struct_fields(api_key_turn_cost, SURFACE_FILES["api_key_turn_cost"], "ApiKeyResponseCost")
            ),
            request_headers=api_key_cost_headers + [
                catalog_header(
                    "Content-Type",
                    condition="explicit JSON request header",
                    value_shape="application/json",
                    category="http",
                    optional=False,
                    source=safe_src(api_key_turn_cost, SURFACE_FILES["api_key_turn_cost"], ".header(CONTENT_TYPE", "Client::query_api_key_turn_costs_at"),
                )
            ],
            variants=[{
                "host_rewrite": "chatgpt.com/chat.openai.com -> api.chatgpt.com; staging equivalent also rewritten",
                "provider_scope_allowlist": ["OpenAI-Organization", "OpenAI-Project"],
                "auth_precedence": "client auth headers are applied after allowed provider-scope headers",
            }],
            notes=[
                "This analytics API is an API-key pricing/cost service, not the signed-in workspace estimate endpoint.",
                "Only organization/project provider headers are forwarded; arbitrary provider headers are filtered out.",
            ],
            source=safe_src(api_key_turn_cost, SURFACE_FILES["api_key_turn_cost"], 'url.set_path("/v1/analytics/codex/turn-costs")', "Client::query_api_key_turn_costs"),
            plane="backend_control",
            service_family="api_key_turn_cost_analytics",
            header_profile="api_key_auth + provider_scope_allowlist + json_body",
            inherits_inference_headers=False,
            trust_domain="ChatGPT analytics API",
            application_metadata={
                "provider_header_allowlist": ["OpenAI-Organization", "OpenAI-Project"],
            },
            profile_refs={"application_protocol": "unary_json"},
        ))

    if realtime_call:
        surfaces.append(_surface(
            surface_id="realtime_call_webrtc_negotiation",
            category="realtime.webrtc",
            endpoints=["/realtime/calls", "/live (Frameless API path variant)"],
            method="POST",
            transport="HTTPS followed by WebRTC media; optional sideband WSS",
            protocol="WebRTC call creation / SDP negotiation",
            streaming=False,
            request_content_type="application/sdp OR multipart/form-data OR application/json (backend shape)",
            request_accept=None,
            request_framing="raw SDP, multipart SDP+session JSON, or backend JSON {sdp,session}",
            response_media_type="application/sdp-like raw SDP body",
            response_framing="unary SDP answer + Location header carrying call id",
            body_schema="raw SDP | multipart(sdp, session) | BackendRealtimeCallRequest",
            body_fields=try_struct_fields(realtime_call, SURFACE_FILES["realtime_call"], "BackendRealtimeCallRequest"),
            request_headers=[
                catalog_header(
                    "Content-Type",
                    condition="depends on realtime call request shape",
                    value_shape="application/sdp | multipart/form-data; boundary=codex-realtime-call-boundary | application/json",
                    optional=False,
                    source=safe_src(realtime_call, SURFACE_FILES["realtime_call"], "CONTENT_TYPE", "RealtimeCallClient"),
                )
            ],
            response_headers=[
                catalog_header(
                    "Location",
                    condition="required to recover realtime call id from call creation response",
                    category="continuation",
                    direction="response",
                    origin="server",
                    optional=False,
                    source=safe_src(realtime_call, SURFACE_FILES["realtime_call"], "LOCATION", "decode_call_id_from_location"),
                )
            ],
            variants=[
                {"name": "raw SDP", "content_type": "application/sdp", "path": "/realtime/calls"},
                {"name": "API session creation", "content_type": "multipart/form-data; boundary=codex-realtime-call-boundary", "parts": ["sdp: application/sdp", "session: application/json"]},
                {"name": "backend-api shape", "content_type": "application/json via EndpointSession", "body": "{sdp, session}"},
            ],
            notes=["This protocol is separate from both normal Responses WebSocket and realtime event WebSocket."],
            source=safe_src(realtime_call, SURFACE_FILES["realtime_call"], '"realtime/calls"', "RealtimeCallClient::path"),
            plane="realtime_media",
            service_family="realtime_call_signaling",
            header_profile="realtime_call_signaling",
            inherits_inference_headers=False,
        ))

    if realtime_ws:
        surfaces.append(_surface(
            surface_id="realtime_websocket",
            category="realtime.events",
            endpoints=["dedicated realtime WSS URL derived from provider base", "sideband WSS for an existing/WebRTC call"],
            method="GET upgrade / persistent frames",
            transport="WSS",
            protocol="Realtime event protocol over WebSocket",
            streaming=True,
            request_content_type=None,
            request_accept=None,
            request_framing="HTTP Upgrade, then realtime JSON text frames (audio is encoded in protocol messages)",
            response_media_type=None,
            response_framing="realtime WebSocket text frames/events",
            body_schema="RealtimeOutboundMessage / RealtimeSessionConfig",
            body_fields=try_struct_fields(realtime_protocol, SURFACE_FILES["realtime_protocol"], "RealtimeSessionConfig"),
            variants=[
                {"parser": "V1 / RealtimeV2", "message_family": "input_audio_buffer.append / conversation/session events"},
                {"parser": "FramelessBidi", "message_family": "frameless live events"},
            ],
            notes=[
                "No HTTP Content-Type or Accept applies to each WebSocket frame after the 101 upgrade.",
                "This is a dedicated RealtimeWebsocketClient, not reuse of the normal Responses WebSocket connection.",
            ],
            source=safe_src(realtime_ws, SURFACE_FILES["realtime_ws"], "websocket_url_from_api_url", "RealtimeWebsocketClient"),
            plane="realtime_media",
            service_family="realtime_events",
            address_kind="websocket_endpoint",
            header_profile="realtime_ws_handshake",
            inherits_inference_headers=False,
        ))

    protocol_differences = [
        {
            "dimension": "request media type",
            "http_responses": "Content-Type: application/json",
            "unary_json": "Content-Type: application/json when a JSON body exists",
            "websocket": "no per-frame HTTP Content-Type after upgrade",
            "realtime_call": "application/sdp, multipart/form-data, or backend JSON depending call shape",
        },
        {
            "dimension": "desired/returned media type",
            "http_responses": "Accept: text/event-stream; server streams SSE",
            "unary_json": "Codex often does not explicitly send Accept: application/json; it decodes one completed JSON body",
            "websocket": "HTTP Accept is not a per-frame negotiation mechanism",
            "realtime_call": "raw SDP answer plus Location header",
        },
        {
            "dimension": "stream framing",
            "http_responses": "SSE records containing JSON event payloads",
            "unary_json": "single completed HTTP response body",
            "websocket": "persistent JSON text frames",
            "realtime_call": "HTTP negotiation then WebRTC media / optional sideband WSS",
        },
        {
            "dimension": "identity cadence",
            "http_responses": "session-id/thread-id/x-client-request-id on each HTTP inference request; client_metadata in each JSON body",
            "websocket": "session/thread headers on handshake; identity repeated in each response.create client_metadata",
            "legacy_compact": "direct x-codex-installation-id header plus compatibility/session headers; no normal client_metadata body field",
        },
        {
            "dimension": "connection and header cadence",
            "http_responses": "one HTTP POST per inference attempt/request; Codex/request headers are sent on each request",
            "websocket": "one HTTP Upgrade establishes a reusable WSS connection; HTTP/Codex headers are handshake-scoped while response.create carries per-request fields/metadata",
            "prewarm": "Responses WS v2 can prewarm with response.create generate=false and then continue using previous_response_id",
        },
        {
            "dimension": "compression",
            "http_responses": "optional request Content-Encoding: zstd",
            "websocket": "permessage-deflate extension; no per-frame HTTP Content-Encoding",
        },
        {
            "dimension": "usage and response metadata",
            "http_responses": "token usage comes from response.completed SSE payload; openai-model/x-reasoning-included/X-Models-Etag/x-request-id can come from HTTP response headers",
            "websocket": "token usage comes from response.completed frame; model/etag metadata can be surfaced via handshake/response metadata events; upstream_request_id is None",
        },
        {
            "dimension": "account backend control plane",
            "classification": "ChatGPT/Codex backend account-control HTTP; NOT model-provider inference",
            "common_headers": "User-Agent + Authorization + optional ChatGPT-Account-ID + optional X-OpenAI-Fedramp",
            "inference_headers": "session/thread/x-codex routing/turn metadata are not inherited",
            "rate_limits": "GET /wham/usage or /api/codex/usage; optional x-openai-codex-luna-reserve: 1",
            "reset_details": "GET /wham/rate-limit-reset-credits or /api/codex/rate-limit-reset-credits",
            "reset_consume": "POST .../rate-limit-reset-credits/consume with Content-Type: application/json",
            "token_activity": "GET /wham/profiles/me or /api/codex/profiles/me",
            "thread_usage": "POST /wham/usage/thread_usage/query or /api/codex/usage/thread_usage/query",
        },
        {
            "dimension": "Responses Lite",
            "http_responses": "literal x-openai-internal-codex-responses-lite: true request header",
            "websocket": "Lite marker is also copied into response.create client_metadata under ws_request_header_x_openai_internal_codex_responses_lite",
            "guardian_classifier": "explicitly Lite in current Luna classifier",
        },
        {
            "dimension": "Guardian routing / free_guardian",
            "feature": "free_guardian is a routing flag, not an endpoint",
            "disabled_or_ineligible": "both Guardian full review and classifier remain on /responses",
            "full_review_unmetered": "/guardian",
            "classifier_unmetered": "/guardian-classifier",
            "guardian": "/guardian shares Responses transport/body but service_tier is cleared and normal routing hint is not added",
            "guardian_classifier": "/guardian-classifier is Responses-compatible; current async scorer uses dedicated pooled WSS + Lite",
        },
        {
            "dimension": "model catalog validation",
            "models_endpoint": "GET /models?client_version=... returns standard HTTP ETag",
            "responses_http": "X-Models-Etag can accompany inference responses",
            "responses_websocket": "x-models-etag can arrive in response metadata events",
            "warning": "these are related cache/version signals but not the same wire header",
        },
        {
            "dimension": "hosted-file transfer",
            "create": "authenticated backend JSON registration",
            "blob": "streamed PUT to a server-issued object-storage URL without backend auth headers",
            "finalize": "authenticated backend JSON polling until success/failure/timeout",
        },
        {
            "dimension": "response diagnostics and dynamic rate limits",
            "static": "request ids, Cloudflare/identity diagnostics, safety treatment, credit state",
            "templated": "x-{limit-id}-{primary|secondary}-{used-percent|window-minutes|reset-at}",
            "events": "codex.rate_limits and response.metadata payloads supplement HTTP headers",
        },
        {
            "dimension": "turn cost APIs",
            "workspace": "thread-estimates query uses signed-in backend account scope",
            "api_key": "/v1/analytics/codex/turn-costs forwards only organization/project provider scope plus client auth",
            "thread_usage": "separate token/credit usage query; not a pricing response",
        },
    ]

    categories: dict[str, list[str]] = {}
    for surface in surfaces:
        categories.setdefault(surface["category"], []).append(surface["id"])

    model_matrix = build_model_facing_transport_matrix(report, core_sources, surface_sources)

    coverage = build_coverage_contract(surfaces, surface_sources, core_sources)

    catalog = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "coverage": coverage,
        "planes": {
            "model_inference": "Codex Core ↔ model inference service; Responses/Guardian and inference metadata",
            "backend_control": "Codex Core ↔ ChatGPT/Codex backend account/control APIs; no Responses turn/routing header inheritance",
            "provider_auxiliary": "Specialized non-Responses provider/service APIs such as models, images, search, and memory",
            "realtime_media": "Realtime signaling/events/media transport family",
            "external_storage": "Server-selected object-storage authority used by the hosted-file upload flow",
            "client_control": "Client ↔ Codex app-server RPC; enriched by the v10 wrapper when those sources are available",
        },
        "categories": dict(sorted(categories.items())),
        "surfaces": surfaces,
        "shared_profiles": shared_profiles,
        "transport_delta": model_matrix.get("transport_delta"),
        "protocol_differences": protocol_differences,
        "model_facing_transport_matrix": model_matrix,
        "guardian_routing_feature": model_matrix.get("guardian_routing_feature"),
        "guardian_ticket_lifecycle": {
            "request_marker": "guardian_ticket_requested may be placed on a normal /responses request",
            "receipt_source": "response.created response headers",
            "replay": "guardian_ticket is attached only to /guardian or /guardian-classifier metadata at the transport boundary",
            "canonical_turn_metadata": "deliberately excluded",
            "source": {
                "attachment": safe_src(guardian_ticket, SURFACE_FILES["guardian_ticket"], "pub(crate) fn attach", "guardian_ticket::attach"),
                "response": safe_src(core_sources.get("response_sse"), FILES["response_sse"], "GUARDIAN_TICKET_HEADER", "response.created Guardian ticket"),
            },
        },
        "response_metadata_inventory": {
            "headers": http_resp,
            "header_templates": http_resp_templates,
            "event_metadata": response_event_metadata,
            "event_kinds": report.get("responses_http", {}).get("response_event_kinds", []),
        },
        "hosted_file_flow": {
            "sequence": ["hosted_file_create", "hosted_file_blob_upload", "hosted_file_finalize"],
            "trust_boundary": "authenticated Codex/ChatGPT backend -> opaque server-issued object-storage URL -> authenticated finalize polling",
            "canonical_result": "sediment://{file_id} plus download_url/file metadata",
        },
        "turn_cost_flows": {
            "chatgpt_workspace_estimate": "chatgpt_turn_cost_estimates",
            "api_key_pricing": "api_key_turn_costs",
            "thread_credit_token_usage": "thread_usage_query",
        },
        "account_usage_reset_flows": {
            "status": {
                "app_server": "account/rateLimits/read",
                "backend": ["GET /wham/usage", "best-effort GET /wham/rate-limit-reset-credits for detailed credits"],
            },
            "usage": {
                "app_server": ["account/usage/read", "account/rateLimits/read"],
                "backend": ["GET /wham/profiles/me", "GET /wham/usage", "optional reset-credit detail GET"],
            },
            "consume_reset": {
                "app_server": "account/rateLimitResetCredit/consume",
                "backend": "POST /wham/rate-limit-reset-credits/consume",
                "follow_up": "refetch account/rateLimits/read",
            },
            "path_style_note": "CodexApi path style uses /api/codex/... equivalents instead of /backend-api/wham/...",
        },
        "media_type_semantics": {
            "Content-Type": "format of the HTTP request body being sent",
            "Accept": "response media type the client asks the HTTP server to return",
            "WebSocket": "after HTTP 101, application messages are WebSocket frames and do not carry per-frame HTTP media headers",
        },
        "notes": [
            "application/json is a media type, not a transport protocol; unary JSON and SSE/WS can all carry JSON at different framing layers.",
            "Account usage/reset routes are backend-control APIs, not model-provider inference endpoints.",
            "Endpoint-specific modules may accept caller-supplied extra_headers/provider headers; the catalog distinguishes explicit endpoint headers from higher-layer dynamic headers.",
            "coverage.scope_contract defines the exhaustiveness boundary; explicit_repository_exclusions are intentional rather than silently omitted.",
            "ETag on /models is a standard HTTP cache validator and is not the same wire header as X-Models-Etag on Responses traffic.",
        ],
    }
    return catalog


def build_report(
    repo: str,
    ref: str,
    commit: dict[str, Any],
    s: dict[str, str],
    *,
    strict: bool = False,
    surface_sources: dict[str, str] | None = None,
    surface_warnings: list[str] | None = None,
    deterministic: bool = False,
) -> dict[str, Any]:
    account, default, auth = s["account"], s["default_client"], s["auth"]
    provider_info = s["provider_info"]
    core, metadata, common = s["core"], s["metadata"], s["common"]
    hdrs, http, compact = s["headers"], s["http"], s["compact"]
    response_sse, ws = s["response_sse"], s["ws"]
    provider, request = s["provider"], s["request"]
    att, inference = s["attestation"], s["inference"]
    surface_sources = surface_sources or {}

    account_headers = [
        header(
            "User-Agent",
            "always",
            account,
            FILES["account"],
            "USER_AGENT",
            "get_codex_user_agent() or codex-cli",
            optional=False,
        ),
        header(
            "Authorization",
            "bearer/auth provider when credentials exist",
            auth,
            FILES["auth"],
            "AUTHORIZATION",
            "Bearer <token>",
            "auth",
            category="auth",
            origin="auth",
        ),
        header(
            "ChatGPT-Account-ID",
            "when account/workspace id exists",
            account,
            FILES["account"],
            "ChatGPT-Account-Id",
            "<account-id>",
            "auth/account",
            category="auth",
            origin="auth",
        ),
        header(
            "X-OpenAI-Fedramp",
            "FedRAMP only",
            account,
            FILES["account"],
            "X-OpenAI-Fedramp",
            "true",
            "auth/account",
            category="auth",
            origin="auth",
        ),
    ]

    provider_headers = builtin_openai_provider_headers(provider_info)

    common_responses_headers = provider_headers + [
        header(
            "originator",
            "default Codex client or thread override",
            default,
            FILES["default_client"],
            'headers.insert("originator"',
            "codex_cli_rs or host originator",
            optional=False,
            category="client",
        ),
        header(
            "User-Agent",
            "default Codex client",
            default,
            FILES["default_client"],
            "USER_AGENT",
            "<originator>/<version> (<OS> <version>; <arch>) <terminal>",
            optional=False,
            category="client",
        ),
        header(
            "x-openai-internal-codex-residency",
            "configured residency only",
            default,
            FILES["default_client"],
            "RESIDENCY_HEADER_NAME",
            "us",
            category="provider",
        ),
        header(
            "Authorization",
            "active auth provider attaches credentials",
            auth,
            FILES["auth"],
            "AUTHORIZATION",
            "Bearer <token> or provider-specific authorization",
            "auth",
            category="auth",
            origin="auth",
        ),
        header(
            "ChatGPT-Account-ID",
            "when auth carries account id",
            auth,
            FILES["auth"],
            "ChatGPT-Account-ID",
            None,
            "auth",
            category="auth",
            origin="auth",
        ),
        header(
            "X-OpenAI-Fedramp",
            "FedRAMP auth only",
            auth,
            FILES["auth"],
            "X-OpenAI-Fedramp",
            "true",
            "auth",
            category="auth",
            origin="auth",
        ),
        header(
            "session-id",
            "normal Codex Responses/compact request",
            hdrs,
            FILES["headers"],
            '"session-id"',
            optional=False,
            category="identity",
        ),
        header(
            "thread-id",
            "normal Codex Responses/compact request",
            hdrs,
            FILES["headers"],
            '"thread-id"',
            optional=False,
            category="identity",
        ),
        header(
            "x-codex-window-id",
            "window compatibility projection",
            metadata,
            FILES["metadata"],
            "X_CODEX_WINDOW_ID_HEADER",
            optional=False,
            category="identity",
        ),
        header(
            "x-codex-turn-metadata",
            "request_kind present; compatibility projection omits tool_namespaces_info",
            metadata,
            FILES["metadata"],
            "X_CODEX_TURN_METADATA_HEADER",
            "ASCII JSON",
            category="metadata",
        ),
        header(
            "x-codex-parent-thread-id",
            "when parent thread exists",
            metadata,
            FILES["metadata"],
            "X_CODEX_PARENT_THREAD_ID_HEADER",
            category="identity",
        ),
        header(
            "x-openai-subagent",
            "subagent/internal source when header value exists",
            metadata,
            FILES["metadata"],
            "X_OPENAI_SUBAGENT_HEADER",
            category="metadata",
        ),
        header(
            "x-codex-beta-features",
            "enabled beta feature keys",
            core,
            FILES["core"],
            '"x-codex-beta-features"',
            "comma-separated",
            category="feature",
        ),
        header(
            "x-codex-routing-hint",
            "Codex backend routing hint for Responses/compact/WS Responses endpoint; absent on Guardian/non-Codex backend paths",
            core,
            FILES["core"],
            "X_CODEX_ROUTING_HINT_HEADER",
            "model=<model>[;tier=<tier>]",
            category="routing",
        ),
        header(
            "x-oai-attestation",
            "when attestation is enabled and provider returns a value",
            att,
            FILES["attestation"],
            "X_OAI_ATTESTATION_HEADER",
            None,
            "attestation",
            category="attestation",
            origin="client_attestation",
        ),
        header(
            "x-openai-memgen-request",
            "memory-consolidation session only",
            core,
            FILES["core"],
            "X_OPENAI_MEMGEN_REQUEST_HEADER",
            "true",
            category="feature",
        ),
    ]

    http_headers = common_responses_headers + [
        header(
            "x-client-request-id",
            "set from thread_id by ResponsesClient::stream_request",
            http,
            FILES["http"],
            '"x-client-request-id"',
            optional=False,
            category="identity",
        ),
        header(
            "x-openai-internal-codex-responses-lite",
            "Responses Lite only; literal HTTP request header",
            core,
            FILES["core"],
            "X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER",
            "true",
            category="feature",
        ),
        header(
            "x-codex-turn-state",
            "replayed unchanged after server supplies sticky routing state within this turn",
            core,
            FILES["core"],
            "X_CODEX_TURN_STATE_HEADER",
            category="continuation",
            origin="server_derived",
            continuation=True,
        ),
        header(
            "Accept",
            "HTTP streaming Responses",
            http,
            FILES["http"],
            "http::header::ACCEPT",
            "text/event-stream",
            "http",
            optional=False,
            category="http",
            origin="client_transport",
        ),
        header(
            "Content-Type",
            "JSON body",
            request,
            FILES["request"],
            "CONTENT_TYPE",
            "application/json",
            "http",
            optional=False,
            category="http",
            origin="client_transport",
        ),
        header(
            "Content-Encoding",
            "request compression enabled for first-party Codex backend",
            request,
            FILES["request"],
            "CONTENT_ENCODING",
            "zstd",
            "http",
            category="http",
            origin="client_transport",
        ),
    ]

    http_tracking_headers = [
        header(
            "x-codex-inference-call-id",
            "rollout inference tracing enabled; one UUID per concrete HTTP inference attempt",
            inference,
            FILES["inference"],
            "INFERENCE_CALL_ID_HEADER",
            "UUID v4",
            "tracking",
            category="tracking",
            origin="client_trace",
        )
    ]

    http_response_headers = [
        response_header(
            "x-request-id",
            "optional provider/HTTP infrastructure request id; observed only if present",
            response_sse,
            FILES["response_sse"],
            "REQUEST_ID_HEADER",
            category="observability",
            continuation=False,
            origin="server_or_infrastructure",
        ),
        response_header(
            "x-codex-turn-state",
            "server sticky-routing token captured once and replayed within the same turn",
            response_sse,
            FILES["response_sse"],
            "X_CODEX_TURN_STATE_HEADER",
            category="continuation",
            continuation=True,
        ),
        response_header(
            "openai-model",
            "server-selected effective model when present",
            response_sse,
            FILES["response_sse"],
            "OPENAI_MODEL_HEADER",
            category="response_metadata",
        ),
        response_header(
            "x-reasoning-included",
            "server indicates reasoning accounting is already included",
            response_sse,
            FILES["response_sse"],
            "X_REASONING_INCLUDED_HEADER",
            category="response_metadata",
        ),
        response_header(
            "X-Models-Etag",
            "model catalog etag when present",
            response_sse,
            FILES["response_sse"],
            '"X-Models-Etag"',
            category="response_metadata",
        ),
    ]
    response_metadata_inventory = build_response_metadata_inventory(
        response_sse, surface_sources
    )
    http_response_headers.extend(response_metadata_inventory["headers"])
    http_response_header_templates = response_metadata_inventory["header_templates"]
    response_event_metadata = response_metadata_inventory["event_metadata"]
    response_event_kinds = response_metadata_inventory["event_kinds"]

    ws_headers = common_responses_headers + [
        header(
            "x-client-request-id",
            "set from thread_id during WebSocket handshake",
            core,
            FILES["core"],
            'headers.insert("x-client-request-id"',
            optional=False,
            category="identity",
        ),
        header(
            "OpenAI-Beta",
            "Responses WebSocket v2 upgrade",
            core,
            FILES["core"],
            "OPENAI_BETA_HEADER",
            "responses_websockets=2026-02-06",
            "ws-handshake",
            optional=False,
            category="feature",
        ),
        header(
            "x-responsesapi-include-timing-metrics",
            "timing metrics enabled",
            core,
            FILES["core"],
            "X_RESPONSESAPI_INCLUDE_TIMING_METRICS_HEADER",
            "true",
            "ws-handshake",
            category="telemetry",
        ),
    ]

    ws_handshake_response_headers = [
        response_header(
            "openai-model",
            "server-selected model on HTTP 101 upgrade when present",
            ws,
            FILES["ws"],
            "OPENAI_MODEL_HEADER",
            category="response_metadata",
        ),
        response_header(
            "x-reasoning-included",
            "server reasoning capability marker on HTTP 101 upgrade when present",
            ws,
            FILES["ws"],
            "X_REASONING_INCLUDED_HEADER",
            category="response_metadata",
        ),
    ]

    ws_event_metadata = [
        {
            "name": "x-codex-turn-state",
            "optional": True,
            "condition": "response metadata supplies sticky routing state",
            "direction": "response",
            "origin": "server",
            "continuation": True,
            "category": "continuation",
            "source": src(ws, FILES["ws"], "event.turn_state()", "WebSocket response metadata"),
        },
        {
            "name": "x-models-etag",
            "optional": True,
            "condition": "codex.response.metadata includes model etag",
            "direction": "response",
            "origin": "server",
            "continuation": False,
            "category": "response_metadata",
            "source": src(ws, FILES["ws"], "X_MODELS_ETAG_HEADER", "WebSocket response metadata"),
        },
        {
            "name": "openai-model",
            "optional": True,
            "condition": "response event headers report the effective model",
            "direction": "response",
            "origin": "server",
            "continuation": False,
            "category": "response_metadata",
            "source": src(ws, FILES["ws"], "event.response_model()", "WebSocket response metadata"),
        },
    ]

    base_cm, ws_cm, warnings = client_metadata_keys(metadata, core, common, strict=strict)

    # Guardian ticket lifecycle: dynamic transport-boundary client_metadata
    # carriers (core client.rs set_guardian_ticket_request; codex-api
    # guardian_ticket::attach). Enumerate them alongside the parsed keys so
    # consumers see the complete flat contract; deliberately NOT projected
    # into the nested turn snapshot.
    guardian_sources = {
        "guardian_ticket_requested": (
            "core",
            '"guardian_ticket_requested".to_owned()',
            "set_guardian_ticket_request",
        ),
        "guardian_ticket": (
            "guardian_ticket",
            'metadata.remove(GUARDIAN_TICKET_METADATA_KEY)',
            "guardian_ticket::attach",
        ),
    }
    for key, (src_key, needle, symbol) in guardian_sources.items():
        text = core if src_key == "core" else surface_sources.get(src_key) or ""
        path = (
            FILES["core"]
            if src_key == "core"
            else SURFACE_FILES[src_key]
        )
        if needle not in text:
            warnings.append(f"guardian client_metadata source anchor missing: {key}")
            continue
        base_cm.append(
            {
                "name": key,
                "optional": True,
                "condition": (
                    "transport-boundary guardian ticket carrier — attached and "
                    "removed outside CodexResponsesMetadata::client_metadata"
                ),
                "direction": "request",
                "origin": "client",
                "continuation": False,
                "source": {
                    "path": path,
                    "line": text[: text.find(needle)].count("\n") + 1,
                    "symbol": symbol,
                },
            }
        )
        base_cm.sort(key=lambda item: item["name"])

    turn = struct_fields(metadata, FILES["metadata"], "CodexTurnMetadataPayload")
    extra = next((f for f in turn if f["name"] == "extra"), None)
    fixed = [f for f in turn if f["name"] != "extra"]

    kinds = re.findall(
        r'CodexResponsesRequestKind::\w+(?:\([^)]*\))?\s*=>\s*\("([^"]+)"',
        metadata,
    )
    kinds = list(dict.fromkeys(kinds))

    compact_headers = common_responses_headers + [
        header(
            "x-codex-installation-id",
            "direct compact request identity header",
            core,
            FILES["core"],
            "extra_headers.insert(X_CODEX_INSTALLATION_ID_HEADER",
            category="identity",
            optional=False,
        ),
        header(
            "x-codex-turn-state",
            "replayed unchanged when server already supplied sticky routing state for this turn",
            core,
            FILES["core"],
            "build_responses_headers(",
            category="continuation",
            origin="server_derived",
            continuation=True,
        ),
        header(
            "x-openai-internal-codex-responses-lite",
            "Responses Lite model only",
            core,
            FILES["core"],
            "add_responses_lite_header(&mut extra_headers",
            "true",
            category="feature",
        ),
        header(
            "Content-Type",
            "compact JSON body",
            request,
            FILES["request"],
            "CONTENT_TYPE",
            "application/json",
            "http",
            optional=False,
            category="http",
            origin="client_transport",
        ),
    ]

    compact_response_headers = [
        response_header(
            "x-codex-turn-state",
            "compact endpoint may establish sticky turn routing state",
            compact,
            FILES["compact"],
            "X_CODEX_TURN_STATE_HEADER",
            category="continuation",
            continuation=True,
        )
    ]

    endpoints = []
    for path in ("/api/codex/accounts/check", "/wham/accounts/check"):
        if path not in account:
            warnings.append(f"account status endpoint absent upstream: {path}")
        else:
            endpoints.append(path)
    if strict and warnings:
        raise AuditError("; ".join(warnings))

    declared = {
        h["name"]
        for h in (
            account_headers
            + http_headers
            + http_tracking_headers
            + ws_headers
            + compact_headers
        )
    }
    request_candidate_sources = {
        k: v
        for k, v in s.items()
        if k not in {"ws", "response_sse", "compact"}
    }
    candidates = discover_header_candidates(request_candidate_sources)
    unclassified = [name for name in candidates if name not in declared]

    report = {
        "generated_at": (commit.get("date") if deterministic else dt.datetime.now(dt.timezone.utc).isoformat()),
        "deterministic": deterministic,
        "generator_version": GENERATOR_VERSION,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "source": {
            "repo": repo,
            "ref": ref,
            "commit": commit,
            "files": list(FILES.values()),
        },
        "scope": {
            "request_header_candidate_scope": (
                "Selected Codex request-construction source files. Endpoint-module coverage "
                "is checked separately; arbitrary custom-provider/auth/proxy/runtime headers "
                "remain outside the contract."
            ),
            "selected_source_request_header_candidates": candidates,
            "all_discovered_header_candidates": candidates,
            "unclassified_header_candidates": unclassified,
            "runtime_transport_headers": [
                "Host",
                "Content-Length/Transfer-Encoding",
                "Accept-Encoding",
                "Connection/Upgrade",
                "Sec-WebSocket-*",
                "proxy-added headers",
                "runtime Cloudflare Cookie values",
            ],
            "warnings": warnings,
            "diagnostics": {
                "semantic_warnings": list(warnings),
                "optional_source_warnings": list(surface_warnings or []),
                "coverage_warnings": [],
            },
        },
        "account_status_check": {
            "request": "GET accounts/check",
            "endpoints": endpoints,
            "request_headers": account_headers,
            "http_headers": account_headers,
            "client_metadata": [],
            "turn_metadata": None,
        },
        "responses_http": {
            "request": "POST /responses (and Responses-compatible Guardian routes)",
            "request_headers": http_headers,
            "http_headers": http_headers,
            "tracking_request_headers": http_tracking_headers,
            "tracking_headers": http_tracking_headers,
            "response_headers": http_response_headers,
            "response_header_templates": http_response_header_templates,
            "response_event_metadata": response_event_metadata,
            "response_event_kinds": response_event_kinds,
            "dynamic_header_sources": [
                {
                    "name": "provider.headers",
                    "optional": True,
                    "scope": "custom providers may supply additional headers",
                    "source": src(
                        provider,
                        FILES["provider"],
                        "pub headers: HeaderMap",
                        "Provider.headers",
                    ),
                }
            ],
            "client_metadata_wire_type": "Option<HashMap<String, String>>",
            "client_metadata": base_cm,
            "turn_metadata": {
                "canonical": 'client_metadata["x-codex-turn-metadata"] JSON string',
                "optional": True,
                "compatibility_header": (
                    "x-codex-turn-metadata; tool_namespaces_info removed from bounded "
                    "compatibility header projection"
                ),
            },
            "continuation": {
                "x-codex-turn-state": "server response header -> replayed request header within same turn",
                "x-request-id": "observability only; never used as next-request continuation identity",
                "response.id": "server response body identity; primarily used by WebSocket incremental continuation",
            },
        },
        "responses_websocket": {
            "request": "HTTP Upgrade + response.create",
            "handshake_request_headers": ws_headers,
            "handshake_http_headers": ws_headers,
            "handshake_response_headers": ws_handshake_response_headers,
            "client_metadata_wire_type": "Option<HashMap<String, String>> in response.create",
            "client_metadata_base": base_cm,
            "client_metadata_ws_additions": ws_cm,
            "response_event_metadata": ws_event_metadata,
            "response_payload_metadata": response_event_metadata,
            "response_event_kinds": response_event_kinds,
            "request_body_continuation": [
                {
                    "name": "previous_response_id",
                    "optional": True,
                    "direction": "request",
                    "origin": "server_derived",
                    "continuation": True,
                    "condition": "incremental WS request reuses a completed server response.id",
                    "source": src(
                        core,
                        FILES["core"],
                        "Some((last_response.response_id, incremental_items))",
                        "ModelClientSession::prepare_websocket_request",
                    ),
                }
            ],
            "turn_metadata": {
                "canonical": 'response.create.client_metadata["x-codex-turn-metadata"] JSON string',
                "optional": True,
                "compatibility_handshake_header": (
                    "x-codex-turn-metadata can be in upgrade compatibility headers; "
                    "tool_namespaces_info omitted there"
                ),
            },
            "notes": [
                "Responses Lite is an HTTP request header on HTTP transport but a response.create client_metadata key on WebSocket.",
                "x-codex-turn-state is response.create client_metadata for WS continuation, not a mutable handshake header.",
                "WebSocket ResponseStream sets upstream_request_id=None; x-request-id is not a WS continuation field.",
                "Connection/Upgrade/Sec-WebSocket-* are generated by the WebSocket stack.",
            ],
            "virtual_frame_mapping": ws_virtual_frame_mapping(
                base_cm, ws_cm, http_headers, ws_headers, compact_headers
            ),
        },
        "responses_compact": {
            "request": "POST /responses/compact",
            "request_headers": compact_headers,
            "response_headers": compact_response_headers,
            "client_metadata": [],
            "request_body_schema": struct_fields(common, FILES["common"], "CompactionInput"),
            "notes": [
                "Compact is unary JSON, not SSE; it does not use the normal Responses client_metadata body field.",
                "x-codex-installation-id is a direct compact HTTP request header.",
                "There is no x-client-request-id insertion in the compact call path.",
            ],
        },
        "server_owned_continuation": [
            {
                "name": "x-codex-turn-state",
                "origin": "server",
                "received_via": "HTTP response header or WebSocket response metadata",
                "replayed_via": "HTTP request header or WS response.create client_metadata",
                "scope": "same Codex turn only",
            },
            {
                "name": "response.id",
                "origin": "server",
                "received_via": "response.completed body event",
                "replayed_via": "previous_response_id",
                "scope": "incremental WebSocket continuation when request properties/input allow reuse",
                "source": src(
                    response_sse,
                    FILES["response_sse"],
                    "response_id: resp.id",
                    "response.completed",
                ),
            },
        ],
        "turn_metadata_schema": {
            "fixed_fields": fixed,
            "flattened_extra": extra,
            "request_kind_values": kinds,
            "nested": {
                "workspace": struct_fields(metadata, FILES["metadata"], "TurnMetadataWorkspace"),
                "tool_namespace": struct_fields(metadata, FILES["metadata"], "TurnToolNamespaceInfo"),
                "tool_function": struct_fields(metadata, FILES["metadata"], "TurnToolFunctionInfo"),
                "compaction": struct_fields(metadata, FILES["metadata"], "CompactionTurnMetadata"),
            },
            "notes": [
                "optional is derived from Option<T>; skip_serializing_if/default/rename are derived from serde attributes.",
                "serde(flatten) extra fields serialize at the same JSON object level; there is no wire-level extra wrapper.",
                "Memory request_kind suppresses normal turn/request identity according to has_turn_identity().",
            ],
        },
        "turn_metadata_construction": turn_metadata_construction(metadata),
    }
    if surface_warnings:
        report["scope"].setdefault("warnings", []).extend(surface_warnings)
        report["scope"].setdefault("diagnostics", {}).setdefault(
            "optional_source_warnings", []
        ).extend(surface_warnings)
    catalog = build_endpoint_protocol_catalog(report, s, surface_sources)
    coverage = catalog.get("coverage") or {}
    report["coverage"] = coverage
    coverage_warnings: list[str] = []
    if coverage.get("unclassified_endpoint_modules"):
        coverage_warnings.append(
            "unclassified codex-api endpoint modules: "
            + ", ".join(coverage["unclassified_endpoint_modules"])
        )
    if coverage.get("unclassified_response_header_candidates"):
        coverage_warnings.append(
            "unclassified selected-source response headers: "
            + ", ".join(coverage["unclassified_response_header_candidates"])
        )
    report["scope"].setdefault("warnings", []).extend(coverage_warnings)
    report["scope"].setdefault("diagnostics", {})["coverage_warnings"] = coverage_warnings
    report["wire_surface_catalog"] = catalog
    # Preserve discoverability of the old key without serializing the full catalog twice.
    report["endpoint_protocol_catalog"] = {
        "deprecated": True,
        "$ref": "#/wire_surface_catalog",
        "replacement": "wire_surface_catalog",
    }
    report["schema_version"] = REPORT_SCHEMA_VERSION
    if surface_sources:
        report["source"]["files"] = list(dict.fromkeys(
            report["source"]["files"]
            + [SURFACE_FILES[k] for k in SURFACE_FILES if k in surface_sources]
        ))
    if strict and report["scope"].get("warnings"):
        raise AuditError("; ".join(report["scope"]["warnings"]))
    return report


def fmt_source(s: dict[str, Any]) -> str:
    return f"{s['path']}:{s['line']} ({s['symbol']})"


def print_headers(items: list[dict[str, Any]]) -> None:
    for h in items:
        value = f"; value={h['value_shape']}" if h.get("value_shape") else ""
        optional = "optional" if h.get("optional") else "required/current-normal-path"
        flow = f"{h.get('direction', 'request')}/{h.get('origin', 'client')}"
        cont = "; continuation" if h.get("continuation") else ""
        print(f"  - {h['name']}: {optional}; {flow}{cont}; {h['condition']}{value}")
        print(f"      source: {fmt_source(h['source'])}")


def print_cm(items: list[dict[str, Any]]) -> None:
    for x in items:
        optional = "optional" if x.get("optional") else "required/current-normal-path"
        cont = "; continuation" if x.get("continuation") else ""
        print(
            f"  - {x['name']}: {optional}; {x.get('direction', 'request')}/"
            f"{x.get('origin', 'client')}{cont}; {x.get('condition', '')}"
        )
        print(f"      source: {fmt_source(x['source'])}")


def print_schema_fields(items: list[dict[str, Any]]) -> None:
    for f in items:
        flags: list[str] = []
        if f.get("optional"):
            flags.append("optional")
        if f.get("skip_serializing_if"):
            flags.append(f"skip_if={f['skip_serializing_if']}")
        if f.get("serde_default"):
            flags.append("serde_default")
        if f.get("skipped"):
            flags.append("serde_skip")
        if f.get("wire_name") != f.get("name"):
            flags.append(f"wire={f['wire_name']}")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        print(f"  - {f['name']}: {f['type']}{suffix}")


def print_endpoint_protocol_catalog(r: dict[str, Any]) -> None:
    cat = r.get("wire_surface_catalog") or r.get("endpoint_protocol_catalog") or {}
    if not cat:
        return
    print("\n=== 6. WIRE SURFACE / HEADER / PROTOCOL CATALOG ===")
    for surface in cat.get("surfaces", []):
        addressing = surface.get("addressing") or {"kind": "http_endpoint", "values": surface.get("endpoints") or []}
        addresses = ", ".join(addressing.get("values") or [])
        print(f"[{surface['id']}] {surface['category']}")
        print(f"  plane/service: {surface.get('plane', '-')} / {surface.get('service_family', '-')}")
        print(f"  {addressing.get('kind', 'address')}: {addresses or '-'}")
        print(f"  method/transport: {surface['method']} / {surface['transport']}")
        print(f"  protocol: {surface['protocol']}; streaming={surface['streaming']}; usage={surface.get('usage_status', 'active')}")
        if surface.get("trust_domain"):
            print(f"  trust domain: {surface['trust_domain']}")
        lifecycle = surface.get("lifecycle") or {}
        if lifecycle:
            print("  lifecycle: " + json.dumps(lifecycle, ensure_ascii=False, sort_keys=True))
        req = surface["request"]
        resp = surface["response"]
        print(f"  request: Content-Type={req.get('content_type') or '-'}; Accept={req.get('accept') or '-'}; framing={req.get('framing')}")
        print(f"  response: media={resp.get('media_type') or '-'}; framing={resp.get('framing')}")
        if req.get("body_schema"):
            print(f"  request body schema: {req['body_schema']}")
        if resp.get("body_schema"):
            print(f"  response body schema: {resp['body_schema']}")
        header_meta = surface.get("headers") or {}
        if header_meta.get("profile"):
            print(f"  header profile: {header_meta['profile']}; inherits inference headers={header_meta.get('inherits_inference_headers')}")
        excluded = header_meta.get("excluded_names") or []
        if excluded:
            print("  explicitly not inherited: " + ", ".join(excluded))
        by_cat = header_meta.get("request_by_category") or {}
        if by_cat:
            print("  request header categories:")
            for category, items in by_cat.items():
                names = ", ".join(x["name"] for x in items)
                print(f"    - {category}: {names}")
        response_by_cat = header_meta.get("response_by_category") or {}
        if response_by_cat:
            print("  response header categories:")
            for category, items in response_by_cat.items():
                names = ", ".join(x["name"] for x in items)
                print(f"    - {category}: {names}")
        templates = header_meta.get("response_templates") or []
        if templates:
            print("  response header templates:")
            for template in templates:
                print(f"    - {template['name_template']}: {template['condition']}")
        event_metadata = resp.get("event_metadata") or []
        if event_metadata:
            print(f"  response event metadata entries: {len(event_metadata)}")
        app_meta = surface.get("application_metadata") or {}
        if app_meta:
            print("  application metadata: " + json.dumps(app_meta, ensure_ascii=False, sort_keys=True))
        runtime_headers = surface.get("transport_generated_headers") or []
        if runtime_headers:
            print("  transport-generated headers: " + ", ".join(runtime_headers))
        if surface.get("variants"):
            print("  variants:")
            for variant in surface["variants"]:
                print("    - " + json.dumps(variant, ensure_ascii=False, sort_keys=True))
        for note in surface.get("notes") or []:
            print(f"  note: {note}")
        print()

    coverage = cat.get("coverage") or {}
    if coverage:
        print("Coverage contract:")
        print("  " + str(coverage.get("scope_contract")))
        print("  endpoint modules: " + str(len((coverage.get("endpoint_module_inventory") or {}).get("modules") or [])))
        endpoint_gaps = coverage.get("unclassified_endpoint_modules") or []
        response_gaps = coverage.get("unclassified_response_header_candidates") or []
        print("  unclassified endpoint modules: " + (", ".join(endpoint_gaps) if endpoint_gaps else "none"))
        print("  unclassified response headers: " + (", ".join(response_gaps) if response_gaps else "none"))

    shared = cat.get("shared_profiles") or {}
    if shared:
        print("Shared wire profiles:")
        for kind in ("header_profiles", "client_metadata_profiles", "request_body_profiles", "response_event_profiles"):
            profiles = shared.get(kind) or {}
            if profiles:
                print(f"  {kind}:")
                for name, profile in profiles.items():
                    if kind == "header_profiles":
                        detail = {"names": profile.get("names", []), "extends": profile.get("extends", [])}
                    elif kind == "client_metadata_profiles":
                        detail = {"key_names": profile.get("key_names", []), "extends": profile.get("extends", []), "construction": profile.get("construction")}
                    elif kind == "request_body_profiles":
                        detail = {"struct": profile.get("struct"), "extends": profile.get("extends", [])}
                    else:
                        detail = {"logical_application_protocol": profile.get("logical_application_protocol")}
                    print(f"    - {name}: " + json.dumps(detail, ensure_ascii=False, sort_keys=True))
    delta = cat.get("transport_delta") or {}
    if delta:
        print("Normalized model transport delta:")
        for dimension in ("headers", "client_metadata", "request_body", "response", "framing", "connection_lifecycle", "field_movement"):
            if dimension in delta:
                print(f"  - {dimension}: " + json.dumps(delta[dimension], ensure_ascii=False, sort_keys=True))

    if cat.get("protocol_differences"):
        print("Protocol differences:")
        for row in cat["protocol_differences"]:
            dim = row.get("dimension")
            details = "; ".join(f"{k}={v}" for k, v in row.items() if k != "dimension")
            print(f"  - {dim}: {details}")
    matrix = cat.get("model_facing_transport_matrix") or {}
    if matrix:
        feature = matrix.get("guardian_routing_feature") or {}
        if feature:
            print("Guardian routing feature:")
            print("  " + json.dumps(feature, ensure_ascii=False, sort_keys=True))
        print("Model-facing route transport matrix:")
        for route in matrix.get("routes") or []:
            print(f"  - {route['route']}: {route['role']}")
            print(f"      transport policy: {route['transport_policy']}")
            if route.get("guardian_routing_role"):
                print(f"      guardian routing role: {route['guardian_routing_role']}")
            if route.get("selected_when"):
                print(f"      selected when: {route['selected_when']}")
            if route.get("fallback_route"):
                print(f"      fallback route: {route['fallback_route']}")
            for key in ("http_sse", "websocket", "http_unary"):
                if key in route:
                    print(f"      {key}: " + json.dumps(route[key], ensure_ascii=False, sort_keys=True))
            for key in ("service_tier", "responses_lite", "compaction_v2", "replacement"):
                if route.get(key) is not None:
                    print(f"      {key}: {route[key]}")
        print("  field movement: " + json.dumps(matrix.get("field_movement") or {}, ensure_ascii=False, sort_keys=True))
    flows = cat.get("account_usage_reset_flows") or {}
    if flows:
        print("Account usage/reset flows:")
        for name, flow in flows.items():
            if isinstance(flow, dict):
                print(f"  - {name}: " + json.dumps(flow, ensure_ascii=False, sort_keys=True))
            else:
                print(f"  - {name}: {flow}")


def print_text(r: dict[str, Any]) -> None:
    c = r["source"]["commit"]
    print(f"Codex wire audit: {r['source']['repo']}@{c['sha']}")
    print(f"ref: {r['source']['ref']}")
    print(f"commit date: {c.get('date')}")
    print(f"commit: {c.get('message')}\n")
    print("=== 1. ACCOUNT / STATUS CHECK ===")
    print_headers(r["account_status_check"]["request_headers"])

    print("\n=== 2. RESPONSES / HTTP REQUEST ===")
    print_headers(r["responses_http"]["request_headers"])
    if r["responses_http"]["tracking_request_headers"]:
        print("Tracking request headers:")
        print_headers(r["responses_http"]["tracking_request_headers"])
    print("client_metadata:")
    print_cm(r["responses_http"]["client_metadata"])
    print("HTTP response/debug headers:")
    print_headers(r["responses_http"]["response_headers"])
    templates = r["responses_http"].get("response_header_templates") or []
    if templates:
        print("HTTP response-header templates:")
        for template in templates:
            print(f"  - {template['name_template']}: {template['condition']}")
    events = r["responses_http"].get("response_event_kinds") or []
    if events:
        print("Consumed response event kinds: " + ", ".join(events))

    print("\n=== 3. RESPONSES / WEBSOCKET ===")
    print("Handshake request headers:")
    print_headers(r["responses_websocket"]["handshake_request_headers"])
    print("Handshake response headers:")
    print_headers(r["responses_websocket"]["handshake_response_headers"])
    print("Base response.create client_metadata:")
    print_cm(r["responses_websocket"]["client_metadata_base"])
    print("WS-only/additional response.create client_metadata:")
    print_cm(r["responses_websocket"]["client_metadata_ws_additions"])

    vfm = r["responses_websocket"].get("virtual_frame_mapping") or {}
    if vfm.get("rows"):
        print("Virtual frame -> HTTP header mapping (required/optional per surface):")
        for row in vfm["rows"]:
            req_ws = "required" if row["ws_required"] else "optional"
            req_http = (row["http_required"] and "required") or (row["http_header"] and "optional") or "-"
            delta = row["delta"]
            print(f"  - {row['virtual_frame_key']} [{req_ws}, {row['ws_condition']}]")
            print(f"      -> {row['http_header'] or '(no HTTP counterpart)'} [{req_http}] delta={delta}")
        if vfm.get("http_only_headers"):
            print("HTTP-only per-frame headers:")
            for x in vfm["http_only_headers"]:
                print(f"  - {x['http_header']} [{'required' if x['http_required'] else 'optional'}] {x['http_condition']}")

    print("\n=== 4. RESPONSES / COMPACT ===")
    print_headers(r["responses_compact"]["request_headers"])
    print("Response headers:")
    print_headers(r["responses_compact"]["response_headers"])

    print("\n=== 5. FULL x-codex-turn-metadata SCHEMA ===")
    print_schema_fields(r["turn_metadata_schema"]["fixed_fields"])
    if r["turn_metadata_schema"]["flattened_extra"]:
        f = r["turn_metadata_schema"]["flattened_extra"]
        print(f"  - [flattened extra]: {f['type']}")
    print("request_kind: " + ", ".join(r["turn_metadata_schema"]["request_kind_values"]))
    for name, fields in r["turn_metadata_schema"]["nested"].items():
        print(f"Nested {name}:")
        print_schema_fields(fields)

    tc = r.get("turn_metadata_construction") or {}
    if tc.get("fields"):
        print("\n=== 5b. TURN-METADATA CONSTRUCTION (per-field emission gates) ===")
        print(f"construction fn: {tc['construction_fn']}")
        for gate, rule in tc["identity_gates"].items():
            print(f"gate {gate}: {rule}")
        for f in tc["fields"]:
            print(f"  - {f['name']}: {f['construction_rule']}")
        print(f"workspaces rule: {tc.get('workspaces_rule')}")
        lim = tc.get("extra_metadata_limits") or {}
        if lim:
            print("extra metadata limits: " + ", ".join(f"{k}={v}" for k, v in lim.items()))

    print_endpoint_protocol_catalog(r)

    if r["scope"]["unclassified_header_candidates"]:
        print("\nUnclassified selected-source request-header candidates:")
        for x in r["scope"]["unclassified_header_candidates"]:
            print(f"  - {x}")
    if r["scope"]["warnings"]:
        print("\nWarnings:")
        for x in r["scope"]["warnings"]:
            print(f"  - {x}")


def self_test() -> None:
    demo = """
struct Demo<'a> {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    a: Option<&'a str>,
    #[serde(rename = "wire_b", skip_serializing_if = "str::is_empty")]
    b: &'a str,
    #[serde(skip)]
    hidden: String,
    #[serde(flatten)]
    extra: &'a BTreeMap<String, String>,
}
"""
    f = struct_fields(demo, "demo.rs", "Demo")
    assert [x["name"] for x in f] == ["a", "b", "hidden", "extra"]
    assert f[0]["optional"] and f[0]["serde_default"]
    assert f[0]["skip_serializing_if"] == "Option::is_none"
    assert f[1]["wire_name"] == "wire_b"
    assert f[2]["skipped"] and not f[2]["serialized"]
    assert f[3]["flattened"]

    fake_sources = {
        "x": '''
const X_REAL_HEADER: &str = "x-real-header";
const REQUEST_ID_HEADER: &str = "x-request-id";
const WS_REQUEST_HEADER_THING_CLIENT_METADATA_KEY: &str = "ws_request_header_thing";
fn f(headers: &mut HeaderMap, client_metadata: &mut HashMap<String, String>, response: &Response) {
    headers.insert(X_REAL_HEADER, v);
    headers.insert("x-literal-header", v);
    insert_header(headers, "session-id", v);
    client_metadata.insert("turn_id".to_string(), v.to_string());
    client_metadata.insert(WS_REQUEST_HEADER_THING_CLIENT_METADATA_KEY.to_string(), v.to_string());
    let _ = response.headers().get(REQUEST_ID_HEADER);
}
'''
    }
    candidates = discover_header_candidates(fake_sources)
    assert "x-real-header" in candidates
    assert "x-literal-header" in candidates
    assert "session-id" in candidates
    assert "turn_id" not in candidates
    assert "ws_request_header_thing" not in candidates
    assert "x-request-id" not in candidates

    metadata = '''
const X_CODEX_INSTALLATION_ID_HEADER: &str = "x-codex-installation-id";
const X_CODEX_WINDOW_ID_HEADER: &str = "x-codex-window-id";
const X_CODEX_TURN_METADATA_HEADER: &str = "x-codex-turn-metadata";
const X_OPENAI_SUBAGENT_HEADER: &str = "x-openai-subagent";
const X_CODEX_PARENT_THREAD_ID_HEADER: &str = "x-codex-parent-thread-id";
const SESSION_ID_KEY: &str = "session_id";
const THREAD_ID_KEY: &str = "thread_id";
const TURN_ID_KEY: &str = "turn_id";
const PARENT_TURN_ID_KEY: &str = "parent_turn_id";
const ROOT_TURN_ID_KEY: &str = "root_turn_id";
fn client_metadata(&self) -> HashMap<String, String> {
    let mut client_metadata = HashMap::from([
        (X_CODEX_INSTALLATION_ID_HEADER.to_string(), self.installation_id.clone()),
        (SESSION_ID_KEY.to_string(), self.session_id.clone()),
        (THREAD_ID_KEY.to_string(), self.thread_id.clone()),
        (X_CODEX_WINDOW_ID_HEADER.to_string(), self.window_id.clone()),
    ]);
    if let Some(turn_id) = &self.turn_id {
        client_metadata.insert(TURN_ID_KEY.to_string(), turn_id.clone());
    }
    if let Some(subagent) = &self.subagent {
        client_metadata.insert(X_OPENAI_SUBAGENT_HEADER.to_string(), subagent.clone());
    }
    if let Some(parent) = &self.parent {
        client_metadata.insert(X_CODEX_PARENT_THREAD_ID_HEADER.to_string(), parent.clone());
    }
    if let Some(parent_turn) = &self.parent_turn {
        client_metadata.insert(PARENT_TURN_ID_KEY.to_string(), parent_turn.clone());
    }
    if let Some(root_turn) = &self.root_turn {
        client_metadata.insert(ROOT_TURN_ID_KEY.to_string(), root_turn.clone());
    }
    client_metadata.insert(X_CODEX_TURN_METADATA_HEADER.to_string(), "{}".to_string());
    client_metadata
}
fn compatibility_headers(&self) {}
'''
    cm, _, warnings = client_metadata_keys(metadata, "", "")
    by_name = {x["name"]: x for x in cm}
    assert not by_name["session_id"]["optional"]
    assert by_name["turn_id"]["optional"]
    assert not warnings

    response = response_header(
        "x-request-id",
        "optional debug",
        'const REQUEST_ID_HEADER: &str = "x-request-id";',
        "response.rs",
        "REQUEST_ID_HEADER",
        category="observability",
        origin="server_or_infrastructure",
    )
    assert response["direction"] == "response"
    assert response["origin"] == "server_or_infrastructure"
    assert not response["continuation"]
    grouped = group_headers_by_category([response])
    assert "observability" in grouped and grouped["observability"][0]["name"] == "x-request-id"

    common_demo = """
struct ResponsesApiRequest {
    pub model: String,
    pub input: Vec<String>,
    pub service_tier: Option<String>,
    pub client_metadata: Option<HashMap<String, String>>,
}
struct ResponseCreateWsRequest<'a> {
    pub model: &'a str,
    pub previous_response_id: Option<String>,
    pub input: &'a [String],
    pub service_tier: Option<&'a str>,
    pub generate: Option<bool>,
    pub client_metadata: Option<HashMap<String, String>>,
}
"""
    http_fields = struct_fields(common_demo, "common.rs", "ResponsesApiRequest")
    ws_fields = struct_fields(common_demo, "common.rs", "ResponseCreateWsRequest")
    body_delta = compare_struct_field_sets("ResponsesApiRequest", http_fields, "ResponseCreateWsRequest", ws_fields)
    assert body_delta["right_only_wire_field_names"] == ["generate", "previous_response_id"]

    guardian_demo = """
const RESPONSES_LITE_METADATA_KEY: &str = "ws_request_header_x_openai_internal_codex_responses_lite";
const TURN_METADATA_KEY: &str = "x-codex-turn-metadata";
fn sample() {
    let mut turn_metadata = json!({
        "session_id": session_id,
        "thread_id": thread_id,
        "guardian_classifier_source_thread_id": source_thread_id,
        "turn_id": turn_id,
        "parent_turn_id": parent_turn_id,
        "thread_source": "guardian_classifier",
    });
    let mut client_metadata = HashMap::from([
        ("session_id".to_owned(), session_id),
        ("thread_id".to_owned(), thread_id),
        ("turn_id".to_owned(), turn_id),
        ("parent_turn_id".to_owned(), parent_turn_id),
        ("x-openai-subagent".to_owned(), "guardian".to_owned()),
        ("x-codex-window-id".to_owned(), window),
        (RESPONSES_LITE_METADATA_KEY.to_owned(), "true".to_owned()),
    ]);
    if let Some(root_turn_id) = root {
        client_metadata.insert("root_turn_id".to_owned(), root_turn_id.clone());
        turn_metadata["root_turn_id"] = json!(root_turn_id);
    }
    client_metadata.insert(TURN_METADATA_KEY.to_owned(), turn_metadata.to_string());
    request.client_metadata = Some(client_metadata);
}
"""
    classifier = guardian_classifier_client_metadata_profile(guardian_demo)
    assert "x-codex-installation-id" not in classifier["key_names"]
    assert "ws_request_header_x_openai_internal_codex_responses_lite" in classifier["key_names"]
    assert "x-codex-turn-metadata" in classifier["key_names"]
    assert "guardian_classifier_source_thread_id" in classifier["nested_turn_metadata_keys"]
    print("self-test: ok")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", default=REPO)
    p.add_argument("--ref", default=REF)
    p.add_argument("--api-base", default=API)
    p.add_argument("--token", default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--output")
    p.add_argument(
        "--strict",
        action="store_true",
        help="fail on missing known conditional fields/endpoints",
    )
    cache = p.add_mutually_exclusive_group()
    cache.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help="immutable-SHA source cache directory (default: %(default)s)",
    )
    cache.add_argument(
        "--no-cache",
        action="store_true",
        help="disable the local immutable-SHA source cache",
    )
    p.add_argument(
        "--deterministic",
        action="store_true",
        help="use the resolved commit timestamp as generated_at for stable golden output",
    )
    p.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    a = p.parse_args()
    configure_cache_dir(None if a.no_cache else a.cache_dir)

    if a.self_test:
        self_test()
        return 0

    commit = resolve_commit(a.repo, a.ref, a.api_base, a.token)
    sources = {
        k: fetch_file(a.repo, commit["sha"], path, a.api_base, a.token)
        for k, path in FILES.items()
    }
    surface_sources, surface_warnings = fetch_optional_sources(
        a.repo, commit["sha"], a.api_base, a.token
    )
    report = build_report(
        a.repo,
        a.ref,
        commit,
        sources,
        strict=a.strict,
        surface_sources=surface_sources,
        surface_warnings=surface_warnings,
        deterministic=a.deterministic,
    )

    if a.json:
        out = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    else:
        import io

        old, buf = sys.stdout, io.StringIO()
        try:
            sys.stdout = buf
            print_text(report)
        finally:
            sys.stdout = old
        out = buf.getvalue()

    if a.output:
        with open(a.output, "w", encoding="utf-8") as fh:
            fh.write(out)
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
