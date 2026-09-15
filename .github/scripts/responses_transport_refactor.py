from __future__ import annotations

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one occurrence, found {count}")
    return text.replace(old, new, 1)


# Fix the generated regression fixture to use the fixture factory instead of a
# nonexistent SourceFile.spec attribute.
tests = Path("toolchain/tests/test_responses_protocol_extractor.py")
text = tests.read_text(encoding="utf-8")
text = replace_once(
    text,
    'files[core.spec_id] = SourceFile.create(spec=core.spec, selected_path=core.selected_path, raw_bytes=broken.encode())',
    'files[core.spec_id] = _file(core.spec_id, "core", core.selected_path, broken)',
    "Responses drift fixture",
)
tests.write_text(text, encoding="utf-8")

# Keep one extractor owner, but move deterministic transport classification and
# value construction out of the already-large Responses protocol module.
protocol = Path("toolchain/codex_wire_audit/extractors/responses_protocol.py")
text = protocol.read_text(encoding="utf-8")
import_anchor = "from .registry import ExtractorResult, register_extractor\n"
transport_import = '''from .responses_transport import (
    PROVIDER,
    RETRY,
    SESSION,
    STARTUP,
    build_transport_lifecycle,
    validate_transport_sources,
)
'''
if transport_import not in text:
    text = replace_once(text, import_anchor, import_anchor + transport_import, "transport import")

for line in (
    'STARTUP = "source_spec.extra.responses_transport_startup"\n',
    'SESSION = "source_spec.extra.responses_transport_session"\n',
    'RETRY = "source_spec.extra.responses_transport_retry"\n',
    'PROVIDER = "source_spec.extra.responses_transport_provider"\n',
):
    text = replace_once(text, line, "", f"local source constant {line.strip()}")

start_marker = '''        complete &= _require(
            diagnostics,
            extractor_id=REQUEST_ID,
            category="responses_request",
            source=startup,
'''
start = text.index(start_marker)
end = text.index('        http_fields = _struct_fields(common.text, "ResponsesApiRequest")', start)
compact_validation = '''        transport_complete, prewarm_before_history_restore = validate_transport_sources(
            diagnostics=diagnostics,
            extractor_id=REQUEST_ID,
            startup=startup,
            session=session,
            retry=retry,
            provider=provider,
        )
        complete &= transport_complete

'''
text = text[:start] + compact_validation + text[end:]

lifecycle_start = text.index('            "transport_lifecycle": {')
lifecycle_end = text.index('            "evidence": {', lifecycle_start)
lifecycle_projection = '''            "transport_lifecycle": build_transport_lifecycle(
                prewarm_before_history_restore=prewarm_before_history_restore
            ),
'''
text = text[:lifecycle_start] + lifecycle_projection + text[lifecycle_end:]
protocol.write_text(text, encoding="utf-8")

helper = Path("toolchain/codex_wire_audit/extractors/responses_transport.py")
helper.write_text(
    '''"""Deterministic Responses WebSocket/HTTP transport lifecycle classification."""
from __future__ import annotations

from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile

STARTUP = "source_spec.extra.responses_transport_startup"
SESSION = "source_spec.extra.responses_transport_session"
RETRY = "source_spec.extra.responses_transport_retry"
PROVIDER = "source_spec.extra.responses_transport_provider"


def _require(
    diagnostics: DiagnosticCollector,
    *,
    extractor_id: str,
    source: SourceFile,
    tokens: tuple[tuple[str, str], ...],
    entity: str,
) -> bool:
    complete = True
    for code, token in tokens:
        if token in source.text:
            continue
        complete = False
        diagnostics.emit(
            code=code,
            severity="error",
            category="responses_request",
            message=f"Required Responses transport source token is missing: {token}",
            extractor_id=extractor_id,
            entity_id=entity,
            source_refs=[source.spec_id],
            details={"path": source.selected_path, "token": token},
            recoverable=False,
            strict_failure=True,
        )
    return complete


def validate_transport_sources(
    *,
    diagnostics: DiagnosticCollector,
    extractor_id: str,
    startup: SourceFile,
    session: SourceFile,
    retry: SourceFile,
    provider: SourceFile,
) -> tuple[bool, bool]:
    complete = _require(
        diagnostics,
        extractor_id=extractor_id,
        source=startup,
        entity="responses_request.transport.startup",
        tokens=(
            ("RESPONSES_STARTUP_PREWARM_SCHEDULE_MISSING", "schedule_startup_prewarm"),
            ("RESPONSES_STARTUP_PREWARM_KIND_MISSING", "CodexResponsesRequestKind::Prewarm"),
            ("RESPONSES_STARTUP_PREWARM_CALL_MISSING", ".prewarm_websocket("),
            ("RESPONSES_STARTUP_EMPTY_INPUT_MISSING", "let startup_prompt = build_prompt(\n        Vec::new(),"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=session,
        entity="responses_request.transport.session",
        tokens=(
            ("RESPONSES_SESSION_PREWARM_MISSING", ".schedule_startup_prewarm("),
            ("RESPONSES_SESSION_HISTORY_RESTORE_MISSING", "record_initial_history(initial_history)"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=retry,
        entity="responses_request.transport.retry",
        tokens=(
            ("RESPONSES_RETRY_HANDLER_MISSING", "handle_retryable_response_stream_error"),
            ("RESPONSES_RETRY_SWITCH_MISSING", "try_switch_fallback_transport"),
            ("RESPONSES_RETRY_HTTPS_WARNING_MISSING", "Falling back from WebSockets to HTTPS transport."),
            ("RESPONSES_UNBOUNDED_CONNECTION_RETRIES_MISSING", "Feature::UnboundedConnectionRetries"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=provider,
        entity="responses_request.transport.provider",
        tokens=(
            ("RESPONSES_PROVIDER_WS_URL_MISSING", "websocket_url_for_path"),
            ("RESPONSES_PROVIDER_HTTP_WS_MAPPING_MISSING", '\"http\" => \"ws\"'),
            ("RESPONSES_PROVIDER_HTTPS_WSS_MAPPING_MISSING", '\"https\" => \"wss\"'),
        ),
    )
    schedule_index = session.text.find(".schedule_startup_prewarm(")
    history_index = session.text.find("record_initial_history(initial_history)")
    ordered = 0 <= schedule_index < history_index
    if not ordered:
        complete = False
        diagnostics.emit(
            code="RESPONSES_PREWARM_HISTORY_ORDER_DRIFT",
            severity="error",
            category="responses_request",
            message="Responses startup prewarm no longer precedes initial history restoration.",
            extractor_id=extractor_id,
            entity_id="responses_request.transport.startup",
            source_refs=[session.spec_id],
            details={"path": session.selected_path},
            recoverable=False,
            strict_failure=True,
        )
    return complete, ordered


def build_transport_lifecycle(*, prewarm_before_history_restore: bool) -> dict[str, Any]:
    return {
        "selection": {
            "websocket_preferred_when": "provider supports_websockets and session fallback is not active",
            "http_fallback": "Responses HTTP transport; user warning names HTTPS, while the concrete scheme follows provider.base_url",
            "path": "/responses",
            "scheme_pairing": {
                "http_base": {"websocket": "ws", "fallback_http": "http"},
                "https_base": {"websocket": "wss", "fallback_http": "https"},
                "ws_base": {"websocket": "ws"},
                "wss_base": {"websocket": "wss"},
            },
        },
        "startup_prewarm": {
            "scheduled_during_session_initialization": True,
            "occurs_before_initial_history_restore": prewarm_before_history_restore,
            "logical_conversation_input": "empty",
            "uses_normal_base_instructions_and_tool_snapshot": True,
            "wire_type": "response.create",
            "generate": False,
            "waits_for": "response.completed",
            "continuation_baseline": "completed response id",
        },
        "continuation": {
            "eligibility": "non-input request properties match and current input extends the previous request plus server-returned response items",
            "eligible_wire": "send previous_response_id and only the incremental input suffix while serializing the other response.create fields",
            "ineligible_wire": "omit previous_response_id and send the full current input",
        },
        "state_scope": {
            "turn_state": "x-codex-turn-state is scoped to one ModelClientSession/turn",
            "websocket_connection": "a healthy connection may be cached by the session-scoped ModelClient for reuse",
            "fallback_state": "session-scoped disable_websockets state",
            "fallback_sticky_across_turns": True,
        },
        "fallback": {
            "upgrade_required_426": "switch immediately from WebSocket to Responses HTTP without ordinary WebSocket stream retries",
            "retryable_failure_before_budget": "discard failed socket, back off, and retry using a new/reopened WebSocket",
            "retry_budget_exhausted": "activate session HTTP fallback, reset the retry counter, and replay the Responses request over HTTP",
            "warning_prefix": "Falling back from WebSockets to HTTPS transport.",
            "unbounded_connection_retry_exception": "when UnboundedConnectionRetries applies to an eligible sampling connection failure, connection retries occur before the normal retry-budget fallback branch",
        },
        "terminal_socket": {
            "on_stream_error": "remove the current WsStream from connection state and drop it",
            "graceful_close_handshake": False,
            "drop_behavior": "WsStream::drop aborts the pump task",
            "special_retryable_codes": [
                "websocket_connection_limit_reached",
                "previous_response_not_found",
            ],
        },
    }
''',
    encoding="utf-8",
)

release = Path("toolchain/codex_wire_audit/release_spec.v1.json")
text = release.read_text(encoding="utf-8")
anchor = '        "extractors/responses_protocol.py",\n'
resource = '        "extractors/responses_transport.py",\n'
if resource not in text:
    text = replace_once(text, anchor, anchor + resource, "responses release resource")
release.write_text(text, encoding="utf-8")
