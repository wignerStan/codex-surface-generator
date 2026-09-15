import json
from pathlib import Path


def sub(path: str, old: str, new: str, n: int = 1) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) < n:
        raise SystemExit(f"{path}: missing patch anchor: {old[:120]!r}")
    p.write_text(text.replace(old, new, n))


# Extend the existing Responses owner instead of introducing an overlapping WebSocket extractor.
rp = "toolchain/codex_wire_audit/extractors/responses_protocol.py"
sub(
    rp,
    'SCHEMA_VERSION = "1.0.0"\nREQUEST_SCHEMA = "https://schemas.codex-system-contract.invalid/responses/responses-request-semantics-v1.schema.json"',
    'SCHEMA_VERSION = "1.0.0"\nREQUEST_SCHEMA_VERSION = "2.0.0"\nREQUEST_SCHEMA = "https://schemas.codex-system-contract.invalid/responses/responses-request-semantics-v2.schema.json"',
)
sub(
    rp,
    'WS = "source_spec.base.ws"\n',
    '''WS = "source_spec.base.ws"\nSTARTUP_PREWARM = "source_spec.extra.responses_websocket_startup_prewarm"\nSESSION_LIFECYCLE = "source_spec.extra.responses_websocket_session_lifecycle"\nRETRY_FALLBACK = "source_spec.extra.responses_websocket_retry_fallback"\nPROVIDER_URL = "source_spec.extra.responses_provider_url"\n''',
)
sub(
    rp,
    '    complete: bool,\n) -> ExtractorResult:\n',
    '    complete: bool,\n    *,\n    schema_version: str = SCHEMA_VERSION,\n) -> ExtractorResult:\n',
)
sub(
    rp,
    '        schema_version=SCHEMA_VERSION,\n',
    '        schema_version=schema_version,\n',
)
sub(
    rp,
    '    source_spec_ids = (COMMON, CORE, HTTP, WS)\n',
    '    source_spec_ids = (COMMON, CORE, HTTP, WS, STARTUP_PREWARM, SESSION_LIFECYCLE, RETRY_FALLBACK, PROVIDER_URL)\n',
)
sub(
    rp,
    '        ws = sources[WS]\n        assert common and core and http and ws\n',
    '        ws = sources[WS]\n        startup_prewarm = sources[STARTUP_PREWARM]\n        session_lifecycle = sources[SESSION_LIFECYCLE]\n        retry_fallback = sources[RETRY_FALLBACK]\n        provider_url = sources[PROVIDER_URL]\n        assert common and core and http and ws and startup_prewarm and session_lifecycle and retry_fallback and provider_url\n',
)
sub(
    rp,
    '                ("RESPONSES_WS_METADATA_MISSING", "build_ws_client_metadata"),\n',
    '''                ("RESPONSES_WS_METADATA_MISSING", "build_ws_client_metadata"),\n                ("RESPONSES_WS_PREWARM_MISSING", "pub async fn prewarm_websocket"),\n                ("RESPONSES_WS_PREWARM_GENERATE_FALSE_MISSING", "generate: if warmup { Some(false) } else { None }"),\n                ("RESPONSES_WS_426_FALLBACK_MISSING", "StatusCode::UPGRADE_REQUIRED"),\n                ("RESPONSES_WS_FALLBACK_OUTCOME_MISSING", "WebsocketStreamOutcome::FallbackToHttp"),\n                ("RESPONSES_WS_STICKY_FALLBACK_MISSING", "try_switch_fallback_transport"),\n''',
)
sub(
    rp,
    '                ("RESPONSES_WS_STREAM_MISSING", "pub async fn stream_request"),\n',
    '''                ("RESPONSES_WS_STREAM_MISSING", "pub async fn stream_request"),\n                ("RESPONSES_WS_TERMINAL_DISCARD_MISSING", "let failed_stream = guard.take();"),\n                ("RESPONSES_WS_PUMP_ABORT_MISSING", "self.pump_task.abort();"),\n                ("RESPONSES_WS_CONNECTION_LIMIT_RETRY_MISSING", "websocket_connection_limit_reached"),\n                ("RESPONSES_WS_PREVIOUS_RESPONSE_RETRY_MISSING", "previous_response_not_found"),\n''',
)
anchor = '''        complete &= _require(\n            diagnostics,\n            extractor_id=REQUEST_ID,\n            category="responses_request",\n            source=ws,\n            entity="responses_request.websocket",\n            tokens=(\n                ("RESPONSES_WS_CONNECTION_MISSING", "pub struct ResponsesWebsocketConnection"),\n                ("RESPONSES_WS_SERIALIZE_MISSING", "serialize_websocket_request"),\n                ("RESPONSES_WS_ENDPOINT_PATH_MISSING", "websocket_url_for_path(self.endpoint.path())"),\n                ("RESPONSES_WS_STREAM_MISSING", "pub async fn stream_request"),\n                ("RESPONSES_WS_TERMINAL_DISCARD_MISSING", "let failed_stream = guard.take();"),\n                ("RESPONSES_WS_PUMP_ABORT_MISSING", "self.pump_task.abort();"),\n                ("RESPONSES_WS_CONNECTION_LIMIT_RETRY_MISSING", "websocket_connection_limit_reached"),\n                ("RESPONSES_WS_PREVIOUS_RESPONSE_RETRY_MISSING", "previous_response_not_found"),\n            ),\n        )\n'''
if Path(rp).read_text().count(anchor) != 1:
    raise SystemExit("responses websocket require block drifted")
extra_require = anchor + '''        complete &= _require(\n            diagnostics,\n            extractor_id=REQUEST_ID,\n            category="responses_request",\n            source=startup_prewarm,\n            entity="responses_request.startup_prewarm",\n            tokens=(\n                ("RESPONSES_STARTUP_PREWARM_SCHEDULER_MISSING", "schedule_startup_prewarm"),\n                ("RESPONSES_STARTUP_PREWARM_GATE_MISSING", "responses_websocket_enabled()"),\n                ("RESPONSES_STARTUP_PREWARM_HANDLE_MISSING", "SessionStartupPrewarmHandle"),\n            ),\n        )\n        complete &= _require(\n            diagnostics,\n            extractor_id=REQUEST_ID,\n            category="responses_request",\n            source=session_lifecycle,\n            entity="responses_request.session_lifecycle",\n            tokens=(\n                ("RESPONSES_SESSION_PREWARM_ORDER_MISSING", "sess.schedule_startup_prewarm"),\n                ("RESPONSES_SESSION_PREWARM_BASE_INSTRUCTIONS_MISSING", "sess.get_prompt_base_instructions"),\n                ("RESPONSES_SESSION_INITIAL_HISTORY_MISSING", "initial_history"),\n            ),\n        )\n        complete &= _require(\n            diagnostics,\n            extractor_id=REQUEST_ID,\n            category="responses_request",\n            source=retry_fallback,\n            entity="responses_request.retry_fallback",\n            tokens=(\n                ("RESPONSES_WS_RETRY_FALLBACK_SWITCH_MISSING", "try_switch_fallback_transport"),\n                ("RESPONSES_WS_FALLBACK_WARNING_MISSING", "Falling back from WebSockets to HTTPS transport."),\n                ("RESPONSES_WS_UNBOUNDED_CONNECTION_RETRY_MISSING", "Feature::UnboundedConnectionRetries"),\n                ("RESPONSES_WS_INITIAL_CONNECTION_DELAY_MISSING", "Duration::from_secs(5)"),\n                ("RESPONSES_WS_MAX_CONNECTION_DELAY_MISSING", "Duration::from_secs(60)"),\n            ),\n        )\n        complete &= _require(\n            diagnostics,\n            extractor_id=REQUEST_ID,\n            category="responses_request",\n            source=provider_url,\n            entity="responses_request.provider_url",\n            tokens=(\n                ("RESPONSES_WS_URL_BUILDER_MISSING", "websocket_url_for_path"),\n                ("RESPONSES_WS_HTTP_SCHEME_MAP_MISSING", '"http" => "ws"'),\n                ("RESPONSES_WS_HTTPS_SCHEME_MAP_MISSING", '"https" => "wss"'),\n                ("RESPONSES_WS_PRESERVE_SCHEME_MISSING", '"ws" | "wss" => return Ok(url)'),\n            ),\n        )\n'''
Path(rp).write_text(Path(rp).read_text().replace(anchor, extra_require, 1))

sub(
    rp,
    '            "$schema": REQUEST_SCHEMA,\n            "schema_version": SCHEMA_VERSION,\n',
    '            "$schema": REQUEST_SCHEMA,\n            "schema_version": REQUEST_SCHEMA_VERSION,\n',
)
sub(
    rp,
    '                    "HTTP/WS request transport semantics and continuation shape",\n',
    '''                    "HTTP/WS request transport semantics and continuation shape",\n                    "Responses WebSocket startup prewarm, terminal socket lifecycle, retry, and HTTP fallback",\n''',
)
sub(
    rp,
    '            "websocket_transport": {\n                "message_type": "response.create",\n                "body": "ResponseCreateWsRequest serialized as one WebSocket request frame",\n                "connection": "provider websocket URL uses the same selected ResponsesEndpoint path",\n                "continuation_fields": ["previous_response_id", "generate", "client_metadata"],\n            },\n',
    '''            "websocket_transport": {\n                "message_type": "response.create",\n                "body": "ResponseCreateWsRequest serialized as one WebSocket request frame",\n                "connection": "provider websocket URL uses the same selected ResponsesEndpoint path",\n                "continuation_fields": ["previous_response_id", "generate", "client_metadata"],\n                "beta_header": "OpenAI-Beta: responses_websockets=2026-02-06",\n            },\n            "endpoint_mapping": {\n                "responses_path": "/responses",\n                "http": {"request": "POST /responses", "response_stream": "text/event-stream (SSE)"},\n                "websocket": {"request": "WebSocket HTTP Upgrade followed by response.create frames", "same_responses_path": True},\n                "provider_scheme_map": {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss", "other": "unchanged"},\n            },\n            "startup_prewarm": {\n                "activation": "only when responses_websocket_enabled()",\n                "scheduling": "session startup before the first user submit",\n                "base_instructions": "current prompt base instructions",\n                "logical_conversation_input": "empty before first user input",\n                "request_type": "response.create",\n                "generate": False,\n                "waits_for": "response.completed",\n                "purpose": "prime a reusable WebSocket connection and previous_response_id continuation baseline",\n                "standard_mode": "static top-level instructions and model-visible tools remain on the prewarm request",\n                "responses_lite_mode": "tools and base instructions are developer-visible input prefix items; no user item is present",\n                "resumed_session": "startup prewarm is scheduled before initial history handling; the first real delta may therefore include restored history plus the new user item",\n            },\n            "continuation": {\n                "requires_compatible_non_input_properties": True,\n                "compatibility_function": "responses_request_properties_match",\n                "previous_response_id": "reuses the last completed response only when the current logical input extends the prior request plus server-returned items",\n                "input_on_reuse": "incremental suffix only",\n                "terminally_failed_socket_reused": False,\n            },\n            "fallback_policy": {\n                "preferred_transport": "Responses WebSocket when enabled and session fallback is inactive",\n                "fallback_transport": "HTTP Responses transport (HTTPS for the normal https:// OpenAI provider; plain HTTP remains possible for custom http:// providers)",\n                "upgrade_required_426": "immediate WebSocket-to-HTTP fallback before normal retry-budget exhaustion",\n                "retryable_websocket_error": "discard failed socket, then retry on a newly connected WebSocket while retry budget remains",\n                "after_retry_budget_exhaustion": "activate session-scoped HTTP fallback and retry the same logical request over HTTP",\n                "session_sticky": True,\n                "normal_retry_counter_reset_after_fallback": True,\n                "user_warning_literal": "Falling back from WebSockets to HTTPS transport. {err:#}",\n                "warning_scope": "literal user-facing wording; machine transport semantics remain HTTP so custom http:// providers are represented accurately",\n                "unbounded_connection_retry": {\n                    "feature": "UnboundedConnectionRetries",\n                    "conditions": ["sampling request", "ConnectionFailed", "non-internal session", "provider is not Amazon Bedrock"],\n                    "delay": "5 seconds exponential backoff capped at 60 seconds",\n                    "fallback_preempted": True,\n                },\n                "nonretryable_error": "surface the error; terminal socket teardown alone does not imply transport fallback",\n            },\n            "terminal_socket_policy": {\n                "terminal_stream_error_discards_connection": True,\n                "graceful_close_wait": False,\n                "drop_action": "WsStream::drop aborts the pump task",\n                "retry_uses_new_websocket": True,\n                "recognized_retryable_wrapped_codes": ["websocket_connection_limit_reached", "previous_response_not_found"],\n                "ping": "reply Pong and continue",\n                "pong": "ignore and continue",\n            },\n''',
)
sub(
    rp,
    '                "websocket": _evidence(ws, "ResponsesWebsocketConnection::stream_request"),\n',
    '''                "websocket": _evidence(ws, "ResponsesWebsocketConnection::stream_request"),\n                "startup_prewarm": _evidence(startup_prewarm, "Session::schedule_startup_prewarm"),\n                "session_lifecycle": _evidence(session_lifecycle, "Session startup prewarm before initial_history handling"),\n                "retry_fallback": _evidence(retry_fallback, "handle_retryable_response_stream_error"),\n                "provider_url": _evidence(provider_url, "Provider::websocket_url_for_path"),\n''',
)
sub(
    rp,
    '        return _result(REQUEST_ID, self.source_spec_ids, body, complete)\n',
    '        return _result(REQUEST_ID, self.source_spec_ids, body, complete, schema_version=REQUEST_SCHEMA_VERSION)\n',
)

# Register the extra source authority under the existing Responses request extractor.
sr = "toolchain/codex_wire_audit/source_registry.py"
sub(
    sr,
    '    ("source_spec.base.ws", "ws", "codex-rs/codex-api/src/endpoint/responses_websocket.rs", ("ResponsesWebsocketConnection", "ResponsesWebsocketClient", "stream_request")),\n)',
    '''    ("source_spec.base.ws", "ws", "codex-rs/codex-api/src/endpoint/responses_websocket.rs", ("ResponsesWebsocketConnection", "ResponsesWebsocketClient", "stream_request")),\n    ("source_spec.extra.responses_websocket_startup_prewarm", "responses_websocket_startup_prewarm", "codex-rs/core/src/session_startup_prewarm.rs", ("schedule_startup_prewarm", "responses_websocket_enabled", "SessionStartupPrewarmHandle")),\n    ("source_spec.extra.responses_websocket_session_lifecycle", "responses_websocket_session_lifecycle", "codex-rs/core/src/session/session.rs", ("sess.schedule_startup_prewarm", "sess.get_prompt_base_instructions", "initial_history")),\n    ("source_spec.extra.responses_websocket_retry_fallback", "responses_websocket_retry_fallback", "codex-rs/core/src/responses_retry.rs", ("handle_retryable_response_stream_error", "try_switch_fallback_transport", "UnboundedConnectionRetries")),\n    ("source_spec.extra.responses_provider_url", "responses_provider_url", "codex-rs/codex-api/src/provider.rs", ("websocket_url_for_path", '"http" => "ws"', '"https" => "wss"')),\n)''',
)

# Add strict v2 schema while retaining v1 for old consumers.
v1 = Path("toolchain/codex_wire_audit/proof_schema_templates/responses-request-semantics-v1.schema.json")
schema = json.loads(v1.read_text())
schema["$id"] = "https://schemas.codex-system-contract.invalid/responses/responses-request-semantics-v2.schema.json"
schema["title"] = "Codex Responses request and transport lifecycle semantics"
schema["properties"]["$schema"] = {"const": schema["$id"]}
schema["properties"]["schema_version"] = {"const": "2.0.0"}
for key in ("endpoint_mapping", "startup_prewarm", "continuation", "fallback_policy", "terminal_socket_policy"):
    if key not in schema["required"]:
        schema["required"].append(key)
    schema["properties"][key] = {"$ref": "#/$defs/semanticSection"}
schema["properties"]["evidence"]["minProperties"] = 8
Path("toolchain/codex_wire_audit/proof_schema_templates/responses-request-semantics-v2.schema.json").write_text(json.dumps(schema, indent=2) + "\n")

# Release capability closure points to the v2 request schema.
release = Path("toolchain/codex_wire_audit/release_spec.v1.json")
release_text = release.read_text()
if release_text.count("proof_schema_templates/responses-request-semantics-v1.schema.json") != 1:
    raise SystemExit("responses request release resource drift")
release.write_text(release_text.replace("proof_schema_templates/responses-request-semantics-v1.schema.json", "proof_schema_templates/responses-request-semantics-v2.schema.json", 1))

# Update tests with exact source and behavior coverage.
tp = Path("toolchain/tests/test_responses_protocol_extractor.py")
ts = tp.read_text()
ts = ts.replace("from __future__ import annotations\n", "from __future__ import annotations\n\nimport json\nfrom pathlib import Path\n\nfrom jsonschema import Draft202012Validator\n", 1)
sub_anchor = 'def _snapshot(*, omit: str | None = None, break_events: bool = False) -> SourceSnapshot:\n'
if sub_anchor not in ts:
    raise SystemExit("responses test snapshot signature drift")
ts = ts.replace(sub_anchor, 'def _snapshot(*, omit: str | None = None, break_events: bool = False, break_fallback: bool = False) -> SourceSnapshot:\n', 1)
core_anchor = '''    core = \'\'\'\nfn responses_request_properties_match() { previous_response_id; }\nfn build_ws_client_metadata() {}\nfn build_responses_request() {\n'''
if core_anchor not in ts:
    raise SystemExit("responses core fixture drift")
ts = ts.replace(core_anchor, '''    core = \'\'\'\nfn responses_request_properties_match() { previous_response_id; }\nfn build_ws_client_metadata() {}\npub async fn prewarm_websocket() { generate: if warmup { Some(false) } else { None }; }\nenum WebsocketStreamOutcome { Stream, FallbackToHttp }\nfn websocket_fallback() { StatusCode::UPGRADE_REQUIRED; try_switch_fallback_transport; }\nfn build_responses_request() {\n''', 1)
ws_anchor = '''    ws = \'\'\'\npub struct ResponsesWebsocketConnection;\npub async fn stream_request() { serialize_websocket_request; process_responses_event; parse_rate_limit_event; }\nfn connect() { websocket_url_for_path(self.endpoint.path()); }\nfn run_websocket_response_stream() { previous_response_not_found; websocket_connection_limit_reached; ResponsesStreamEvent; }\n\'\'\'\n'''
if ws_anchor not in ts:
    raise SystemExit("responses ws fixture drift")
ws_new = ws_anchor + '''    startup_prewarm = \'\'\'\nstruct SessionStartupPrewarmHandle;\nfn schedule_startup_prewarm() { responses_websocket_enabled(); }\n\'\'\'\n    session_lifecycle = \'\'\'\nfn start() { sess.schedule_startup_prewarm(sess.get_prompt_base_instructions()); let initial_history = (); }\n\'\'\'\n    retry_fallback = \'\'\'\nfn handle_retryable_response_stream_error() {\n    Feature::UnboundedConnectionRetries;\n    Duration::from_secs(5);\n    Duration::from_secs(60);\n    try_switch_fallback_transport;\n    format!("Falling back from WebSockets to HTTPS transport. {err:#}");\n}\n\'\'\'\n    if break_fallback:\n        retry_fallback = retry_fallback.replace("Falling back from WebSockets to HTTPS transport.", "transport fallback changed")\n    provider_url = \'\'\'\nfn websocket_url_for_path() { match scheme { "http" => "ws", "https" => "wss", "ws" | "wss" => return Ok(url), _ => return Ok(url) }; }\n\'\'\'\n'''
ts = ts.replace(ws_anchor, ws_new, 1)
rows_anchor = '''        "ws": _file("source_spec.base.ws", "ws", "codex-rs/codex-api/src/endpoint/responses_websocket.rs", ws),\n    }\n'''
if rows_anchor not in ts:
    raise SystemExit("responses rows fixture drift")
ts = ts.replace(rows_anchor, '''        "ws": _file("source_spec.base.ws", "ws", "codex-rs/codex-api/src/endpoint/responses_websocket.rs", ws),\n        "startup_prewarm": _file("source_spec.extra.responses_websocket_startup_prewarm", "responses_websocket_startup_prewarm", "codex-rs/core/src/session_startup_prewarm.rs", startup_prewarm),\n        "session_lifecycle": _file("source_spec.extra.responses_websocket_session_lifecycle", "responses_websocket_session_lifecycle", "codex-rs/core/src/session/session.rs", session_lifecycle),\n        "retry_fallback": _file("source_spec.extra.responses_websocket_retry_fallback", "responses_websocket_retry_fallback", "codex-rs/core/src/responses_retry.rs", retry_fallback),\n        "provider_url": _file("source_spec.extra.responses_provider_url", "responses_provider_url", "codex-rs/codex-api/src/provider.rs", provider_url),\n    }\n''', 1)
old_set = '''    assert by_extractor["extractor.responses_request"] == {\n        "source_spec.base.common", "source_spec.base.core", "source_spec.base.http", "source_spec.base.ws"\n    }\n'''
if old_set not in ts:
    raise SystemExit("responses registry expectation drift")
ts = ts.replace(old_set, '''    assert by_extractor["extractor.responses_request"] == {\n        "source_spec.base.common",\n        "source_spec.base.core",\n        "source_spec.base.http",\n        "source_spec.base.ws",\n        "source_spec.extra.responses_websocket_startup_prewarm",\n        "source_spec.extra.responses_websocket_session_lifecycle",\n        "source_spec.extra.responses_websocket_retry_fallback",\n        "source_spec.extra.responses_provider_url",\n    }\n''', 1)
request_assert_anchor = '    assert result.data["request_shape"]["websocket_envelope_type"] == "response.create"\n'
if request_assert_anchor not in ts:
    raise SystemExit("responses request assertion anchor drift")
ts = ts.replace(request_assert_anchor, request_assert_anchor + '''    assert result.schema_version == "2.0.0"\n    assert result.data["startup_prewarm"]["generate"] is False\n    assert result.data["endpoint_mapping"]["provider_scheme_map"] == {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss", "other": "unchanged"}\n    assert result.data["fallback_policy"]["upgrade_required_426"].startswith("immediate")\n    assert result.data["fallback_policy"]["session_sticky"] is True\n    assert result.data["fallback_policy"]["user_warning_literal"] == "Falling back from WebSockets to HTTPS transport. {err:#}"\n    assert result.data["terminal_socket_policy"]["retry_uses_new_websocket"] is True\n''', 1)
ts += '''\n\ndef test_request_schema_v2_validates_exact_transport_contract() -> None:\n    diagnostics = DiagnosticCollector()\n    result = ResponsesRequestExtractor().extract(_snapshot(), diagnostics)\n    schema_path = Path(__file__).parents[1] / "codex_wire_audit" / "proof_schema_templates" / "responses-request-semantics-v2.schema.json"\n    schema = json.loads(schema_path.read_text())\n    Draft202012Validator.check_schema(schema)\n    assert list(Draft202012Validator(schema).iter_errors(result.data)) == []\n\n\ndef test_websocket_fallback_contract_drift_fails_closed() -> None:\n    diagnostics = DiagnosticCollector()\n    result = ResponsesRequestExtractor().extract(_snapshot(break_fallback=True), diagnostics)\n    assert result.semantic_complete is False\n    assert "RESPONSES_WS_FALLBACK_WARNING_MISSING" in {item.code for item in diagnostics.values()}\n\n\ndef test_responses_transport_source_identity_is_exact() -> None:\n    registry = build_registry(load_legacy_modules())\n    assert registry.get("source_spec.extra.responses_websocket_startup_prewarm").primary_path == "codex-rs/core/src/session_startup_prewarm.rs"\n    assert registry.get("source_spec.extra.responses_websocket_session_lifecycle").primary_path == "codex-rs/core/src/session/session.rs"\n    assert registry.get("source_spec.extra.responses_websocket_retry_fallback").primary_path == "codex-rs/core/src/responses_retry.rs"\n    assert registry.get("source_spec.extra.responses_provider_url").primary_path == "codex-rs/codex-api/src/provider.rs"\n'''
tp.write_text(ts)

# Port the useful prose from the uploaded patch and add the current exact fallback policy.
doc = Path("PROMPT_ASSEMBLY_AND_CONFIG.md")
ds = doc.read_text()
anchor = "The internal metadata field is subject to the `content_item_kinds_enabled` gate.\n"
if anchor not in ds:
    raise SystemExit("prompt doc transport anchor drift")
if "### Responses WebSocket startup prewarm and input deltas" not in ds:
    section = r'''

### Responses WebSocket startup prewarm and input deltas

For Responses WebSocket sessions, Codex schedules a startup prewarm while the session is being initialized; it does not wait for the first user submit. The startup prompt uses the normal base instructions and model-visible tool snapshot, but its logical conversation input is empty. The wire request is an ordinary `response.create` with `generate: false`, and Codex waits for `response.completed` so the returned `response.id` can become the continuation baseline.

In Standard Responses mode the prewarm retains the ordinary static top-level instructions and model-visible tools. In Responses Lite, those tools and base instructions have already been rewritten into developer-visible input prefix items, so the prewarm contains that prefix but no user item.

After completion, a later WebSocket request can reuse that response id only when the non-input request properties still match and the current logical input extends the previous request plus server-returned response items. Codex then sends `previous_response_id` and only the incremental input suffix. For a resumed session, startup prewarm is scheduled before initial-history handling, so the first real delta can include restored history plus the new user item.

#### WebSocket versus HTTP(S) transport and fallback

Both transports address the selected Responses endpoint path, normally `/responses`. HTTP uses `POST` with a JSON `ResponsesApiRequest` and receives `text/event-stream` SSE. WebSocket converts the provider URL scheme while keeping the same endpoint path: `http -> ws`, `https -> wss`, and existing `ws`/`wss` schemes are preserved. The upgraded connection carries `response.create` frames and may reuse `previous_response_id` across compatible requests.

WebSocket is preferred only while it is enabled and the session has not activated transport fallback. HTTP status 426 (`Upgrade Required`) during WebSocket setup triggers immediate fallback to the HTTP Responses transport. Retryable WebSocket stream failures first discard the terminal socket and retry on a newly connected socket while the ordinary retry budget remains. Once that budget is exhausted, `try_switch_fallback_transport` activates a session-scoped fallback; the same logical request is retried over HTTP, the normal retry counter is reset, and subsequent turns in that session stay on HTTP.

The current user-facing warning is exactly `Falling back from WebSockets to HTTPS transport. {err:#}`. That string describes the normal OpenAI `https://` provider. The machine contract records the fallback generically as HTTP Responses transport because a custom `http://` provider maps to `ws://` and falls back to plain HTTP.

There is one important exception: with `UnboundedConnectionRetries`, a sampling `ConnectionFailed` on a non-internal, non-Bedrock session retries indefinitely with exponential delay from 5 seconds up to a 60-second cap; that branch runs before normal retry-budget fallback.

Terminality and retryability remain separate. On terminal WebSocket stream failure, Codex removes the current `WsStream`; dropping it aborts the pump task, and the code intentionally does not wait for a graceful close handshake. Special wrapped codes such as `websocket_connection_limit_reached` and `previous_response_not_found` are retryable, but they still poison the current socket first. Ping is answered with Pong; Pong is ignored.
'''
    ds = ds.replace(anchor, anchor + section, 1)
doc.write_text(ds)
