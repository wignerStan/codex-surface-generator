"""Source-derived server -> client Responses metadata and catalog-refresh surface."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile, SourceSnapshot
from .registry import ExtractorResult, register_extractor

EXTRACTOR_ID = "extractor.responses_server_response"
SCHEMA_VERSION = "1.1.0"
SCHEMA = "https://schemas.codex-system-contract.invalid/responses/responses-server-response-semantics-v1.schema.json"

COMMON = "source_spec.base.common"
SSE = "source_spec.base.response_sse"
WS = "source_spec.base.ws"
TURN = "source_spec.extra.prompt_turn"
MODELS_MANAGER = "source_spec.extra.models_manager"
MODELS_ENDPOINT = "source_spec.surface.models_endpoint"
MODEL_PROVIDER_MODELS = "source_spec.surface.model_provider_models"
SOURCE_SPECS = (
    COMMON,
    SSE,
    WS,
    TURN,
    MODELS_MANAGER,
    MODELS_ENDPOINT,
    MODEL_PROVIDER_MODELS,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _evidence(source: SourceFile, symbol: str) -> dict[str, Any]:
    return {
        "source_spec_id": source.spec_id,
        "path": source.selected_path,
        "sha256": source.content_sha256,
        "git_blob_sha": source.git_blob_sha,
        "symbol": symbol,
    }


def _missing(
    diagnostics: DiagnosticCollector,
    *,
    code: str,
    token: str,
    source: SourceFile | None,
    entity: str,
) -> None:
    diagnostics.emit(
        code=code,
        severity="error",
        category="responses_server_response",
        message=f"Required server-response source token is missing: {token}",
        extractor_id=EXTRACTOR_ID,
        entity_id=entity,
        source_refs=[source.spec_id] if source else [],
        details={"path": source.selected_path, "token": token} if source else {"token": token},
        recoverable=False,
        strict_failure=True,
    )


def _require(
    diagnostics: DiagnosticCollector,
    source: SourceFile,
    entity: str,
    tokens: tuple[tuple[str, str], ...],
) -> bool:
    complete = True
    for code, token in tokens:
        if token not in source.text:
            complete = False
            _missing(diagnostics, code=code, token=token, source=source, entity=entity)
    return complete


def _brace_body(text: str, declaration: str) -> str | None:
    start = text.find(declaration)
    if start < 0:
        return None
    opening = text.find("{", start + len(declaration))
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index]
    return None


def _result(body: dict[str, Any], complete: bool) -> ExtractorResult:
    body["semantic_complete"] = complete
    body["semantic_digest"] = hashlib.sha256(_canonical(body)).hexdigest()
    return ExtractorResult(
        extractor_id=EXTRACTOR_ID,
        schema_version=SCHEMA_VERSION,
        data=body,
        semantic_complete=complete,
        source_spec_ids=SOURCE_SPECS,
    )


def _load_sources(
    snapshot: SourceSnapshot, diagnostics: DiagnosticCollector
) -> dict[str, SourceFile] | None:
    missing = [spec_id for spec_id in SOURCE_SPECS if snapshot.files.get(spec_id) is None]
    for spec_id in missing:
        _missing(
            diagnostics,
            code="RESPONSES_SERVER_RESPONSE_SOURCE_UNAVAILABLE",
            token=spec_id,
            source=None,
            entity=f"responses_server_response.source.{spec_id}",
        )
    if missing:
        return None
    return {spec_id: snapshot.files[spec_id] for spec_id in SOURCE_SPECS}


def _validate_wire_sources(
    common: SourceFile,
    sse: SourceFile,
    ws: SourceFile,
    diagnostics: DiagnosticCollector,
) -> bool:
    complete = _require(
        diagnostics,
        common,
        "responses_server_response.normalized_event",
        (
            ("RESPONSES_SERVER_MODEL_EVENT_MISSING", "ServerModel(String)"),
            ("RESPONSES_SERVER_MODEL_DOC_MISSING", "server includes `OpenAI-Model`"),
            ("RESPONSES_MODELS_ETAG_EVENT_MISSING", "ModelsEtag(String)"),
        ),
    )
    complete &= _require(
        diagnostics,
        sse,
        "responses_server_response.http_sse",
        (
            ("RESPONSES_OPENAI_MODEL_HEADER_CONST_MISSING", 'const OPENAI_MODEL_HEADER: &str = "openai-model"'),
            ("RESPONSES_HTTP_SERVER_MODEL_HEADER_READ_MISSING", ".get(OPENAI_MODEL_HEADER)"),
            ("RESPONSES_EVENT_MODEL_RESOLVER_MISSING", "pub fn response_model(&self) -> Option<String>"),
            ("RESPONSES_EVENT_RESPONSE_HEADERS_MISSING", 'response.get("headers")'),
            ("RESPONSES_EVENT_TOP_LEVEL_HEADERS_MISSING", ".headers\n                .as_ref()"),
            ("RESPONSES_EVENT_OPENAI_MODEL_HEADER_ALIAS_MISSING", 'eq_ignore_ascii_case("openai-model")'),
            ("RESPONSES_EVENT_X_OPENAI_MODEL_ALIAS_MISSING", 'eq_ignore_ascii_case("x-openai-model")'),
            ("RESPONSES_EVENT_SERVER_MODEL_EMIT_MISSING", "ResponseEvent::ServerModel(model.clone())"),
            ("RESPONSES_EVENT_SERVER_MODEL_DEDUPE_MISSING", "last_server_model.as_deref()"),
            ("RESPONSES_HTTP_MODELS_ETAG_MISSING", '.get("X-Models-Etag")'),
            ("RESPONSES_HTTP_MODELS_ETAG_EVENT_MISSING", "ResponseEvent::ModelsEtag(etag)"),
            ("RESPONSES_HTTP_REASONING_INCLUDED_MISSING", "X_REASONING_INCLUDED_HEADER"),
            ("RESPONSES_HTTP_REQUEST_ID_MISSING", "REQUEST_ID_HEADER"),
            ("RESPONSES_HTTP_TURN_STATE_MISSING", "X_CODEX_TURN_STATE_HEADER"),
        ),
    )
    complete &= _require(
        diagnostics,
        ws,
        "responses_server_response.websocket",
        (
            ("RESPONSES_WS_UPGRADE_MODEL_HEADER_MISSING", "response\n        .headers()\n        .get(OPENAI_MODEL_HEADER)"),
            ("RESPONSES_WS_SERVER_MODEL_STATE_MISSING", "server_model: Option<String>"),
            ("RESPONSES_WS_SERVER_MODEL_EVENT_MISSING", "event.response_model()"),
            ("RESPONSES_WS_SERVER_MODEL_EMIT_MISSING", "ResponseEvent::ServerModel(model.clone())"),
            ("RESPONSES_WS_SERVER_MODEL_DEDUPE_MISSING", "last_server_model.as_deref()"),
            ("RESPONSES_WS_MODELS_ETAG_MISSING", 'const X_MODELS_ETAG_HEADER: &str = "x-models-etag"'),
            ("RESPONSES_WS_MODELS_ETAG_METADATA_KIND_MISSING", 'event.kind() == "codex.response.metadata"'),
            ("RESPONSES_WS_MODELS_ETAG_EVENT_MISSING", "ResponseEvent::ModelsEtag(etag)"),
            ("RESPONSES_WS_UPGRADE_TURN_STATE_HEADER_MISSING", ".get(X_CODEX_TURN_STATE_HEADER)"),
            ("RESPONSES_WS_TURN_STATE_MISSING", "event.turn_state()"),
            ("RESPONSES_WS_REASONING_INCLUDED_MISSING", "response.headers().contains_key(X_REASONING_INCLUDED_HEADER)"),
        ),
    )
    return complete


def _validate_catalog_sources(
    turn: SourceFile,
    manager: SourceFile,
    endpoint: SourceFile,
    provider_models: SourceFile,
    diagnostics: DiagnosticCollector,
) -> bool:
    complete = _require(
        diagnostics,
        turn,
        "responses_server_response.models_catalog.dispatch",
        (
            ("RESPONSES_MODELS_ETAG_DISPATCH_MISSING", "ResponseEvent::ModelsEtag(etag)"),
            ("RESPONSES_MODELS_ETAG_MANAGER_CALL_MISSING", ".refresh_if_new_etag(etag, turn_context.config.http_client_factory())"),
        ),
    )
    complete &= _require(
        diagnostics,
        manager,
        "responses_server_response.models_catalog.manager",
        (
            ("RESPONSES_MODELS_ETAG_MANAGER_IMPL_MISSING", "async fn refresh_if_new_etag"),
            ("RESPONSES_MODELS_ETAG_COMPARE_MISSING", "current_etag.as_deref() == Some(etag.as_str())"),
            ("RESPONSES_MODELS_ETAG_IDENTITY_COMPARE_MISSING", "Some(&identity) == self.endpoint_client.identity().as_ref()"),
            ("RESPONSES_MODELS_ETAG_TTL_RENEW_MISSING", ".refresh_ttl(&crate::client_version_to_whole(), &identity, &etag)"),
            ("RESPONSES_MODELS_ETAG_ONLINE_REFRESH_MISSING", ".refresh_available_models(RefreshStrategy::Online, &http_client_factory)"),
            ("RESPONSES_MODELS_ETAG_ONLINE_FETCH_MISSING", "RefreshStrategy::Online => self.fetch_and_update_models(http_client_factory).await"),
            ("RESPONSES_MODELS_CATALOG_FETCH_MISSING", "async fn fetch_and_update_models"),
            ("RESPONSES_MODELS_CATALOG_ENDPOINT_CALL_MISSING", ".list_models(&client_version, http_client_factory.clone())"),
            ("RESPONSES_MODELS_CACHE_ENTRY_MISSING", "let entry = ModelsCacheEntry {"),
            ("RESPONSES_MODELS_CACHE_STORE_MISSING", "cache.store(&entry)"),
            ("RESPONSES_MODELS_IN_MEMORY_APPLY_MISSING", "self.apply_remote_models(entry).await"),
        ),
    )
    complete &= _require(
        diagnostics,
        provider_models,
        "responses_server_response.models_catalog.provider_bridge",
        (
            ("RESPONSES_MODELS_PROVIDER_ENDPOINT_MISSING", 'const MODELS_ENDPOINT: &str = "/models"'),
            ("RESPONSES_MODELS_PROVIDER_REQUEST_URL_MISSING", "ModelsClient::<ReqwestTransport>::request_url(&api_provider, client_version)"),
            ("RESPONSES_MODELS_PROVIDER_LIST_CALL_MISSING", ".list_models(request_url, HeaderMap::new())"),
            ("RESPONSES_MODELS_PROVIDER_RESULT_MISSING", "Ok(ModelsEndpointResponse {"),
        ),
    )
    complete &= _require(
        diagnostics,
        endpoint,
        "responses_server_response.models_catalog.http",
        (
            ("RESPONSES_MODELS_HTTP_PATH_MISSING", 'fn path() -> &\'static str {\n        "models"'),
            ("RESPONSES_MODELS_HTTP_GET_MISSING", "provider.build_request(Method::GET, Self::path())"),
            ("RESPONSES_MODELS_CLIENT_VERSION_QUERY_MISSING", "client_version={client_version}"),
            ("RESPONSES_MODELS_HTTP_ETAG_MISSING", ".get(ETAG)"),
            ("RESPONSES_MODELS_BODY_DECODE_MISSING", "let ModelsResponse { models } = serde_json::from_slice::<ModelsResponse>(&resp.body)"),
            ("RESPONSES_MODELS_HTTP_RESULT_MISSING", "Ok((models, header_etag))"),
        ),
    )
    return complete


def _check_response_model_authority(
    sse: SourceFile, diagnostics: DiagnosticCollector
) -> tuple[bool, bool]:
    body = _brace_body(sse.text, "pub fn response_model(&self) -> Option<String>")
    if body is None:
        _missing(
            diagnostics,
            code="RESPONSES_EVENT_MODEL_RESOLVER_BODY_MISSING",
            token="ResponsesStreamEvent::response_model body",
            source=sse,
            entity="responses_server_response.effective_model",
        )
        return False, False
    uses_payload_model = 'response.get("model")' in body or 'response["model"]' in body
    if uses_payload_model:
        diagnostics.emit(
            code="RESPONSES_EFFECTIVE_MODEL_AUTHORITY_CHANGED",
            severity="error",
            category="responses_server_response",
            message="ResponsesStreamEvent::response_model now consults response.model; effective-model authority changed.",
            extractor_id=EXTRACTOR_ID,
            entity_id="responses_server_response.effective_model",
            source_refs=[sse.spec_id],
            details={"path": sse.selected_path},
            recoverable=False,
            strict_failure=True,
        )
    return not uses_payload_model, uses_payload_model


def _check_manager_body(manager: SourceFile, diagnostics: DiagnosticCollector) -> bool:
    if _brace_body(manager.text, "async fn refresh_if_new_etag") is not None:
        return True
    _missing(
        diagnostics,
        code="RESPONSES_MODELS_ETAG_MANAGER_BODY_MISSING",
        token="OpenAiModelsManager::refresh_if_new_etag body",
        source=manager,
        entity="responses_server_response.models_catalog.manager",
    )
    return False


def _effective_model_contract(payload_model_consulted: bool) -> dict[str, Any]:
    return {
        "meaning": "server-reported effective model; it may differ from the requested model when backend routing changes the serving model",
        "wire_header": "openai-model",
        "event_json_header_aliases": ["openai-model", "x-openai-model"],
        "normalized_event": "ResponseEvent::ServerModel(String)",
        "http_sse": {
            "outer_http_response_header": "openai-model",
            "body_framing": "SSE data JSON decoded as ResponsesStreamEvent",
            "event_json_locations": ["response.headers", "headers"],
            "event_json_precedence": ["response.headers", "headers"],
            "event_change_detection": "emit ServerModel only when the event-reported value differs from last_server_model",
        },
        "websocket": {
            "upgrade_response_header": "openai-model",
            "upgrade_scope": "HTTP 101 WebSocket handshake response / connection establishment",
            "frame_framing": "WebSocket JSON text frame decoded as ResponsesStreamEvent",
            "event_json_locations": ["response.headers", "headers"],
            "event_json_precedence": ["response.headers", "headers"],
            "event_change_detection": "emit ServerModel when an event-reported value differs from last_server_model",
        },
        "response_model_payload_field_consulted": payload_model_consulted,
        "not_client_metadata": True,
        "not_turn_metadata": True,
    }


def _models_catalog_contract() -> dict[str, Any]:
    return {
        "signal": {
            "meaning": "opaque catalog-generation/invalidation signal; it is not a model identifier and does not carry the catalog body",
            "normalized_event": "ResponseEvent::ModelsEtag(String)",
            "http_sse": {"location": "outer HTTP response header", "header": "X-Models-Etag"},
            "websocket": {"location": "codex.response.metadata top-level headers", "header": "x-models-etag"},
            "catalog_body_embedded": False,
        },
        "dispatch": "core turn handling forwards ModelsEtag(etag) to models_manager.refresh_if_new_etag(etag, http_client_factory)",
        "comparison": {
            "state": "current in-memory ModelsCacheEntry identity + etag",
            "match_condition": "same endpoint identity and current_etag == received etag",
            "on_match": "do not refetch /models; renew persistent cache TTL when a cache is present, then return",
            "on_mismatch_or_identity_change": "refresh_available_models with RefreshStrategy::Online",
        },
        "catalog_fetch": {
            "separate_request": True,
            "method": "GET",
            "path": "/models",
            "query": "client_version=<current Codex client version>",
            "provider_bridge": "OpenAiModelsEndpoint builds ModelsClient request_url and calls ModelsClient.list_models",
            "response_body": "ModelsResponse JSON; Codex extracts models[]",
            "response_version_header": "ETag",
            "returns_to_manager": "ModelsEndpointResponse { models, etag, identity }",
        },
        "catalog_update": {
            "cache_entry": "ModelsCacheEntry { fetched_at, etag, client_version, identity, models }",
            "persistent_cache": "store entry when cache is configured",
            "in_memory_catalog": "apply_remote_models(entry) after identity validation",
        },
        "wire_version_relation": {
            "responses_signal": "X-Models-Etag / x-models-etag",
            "models_response_version": "ETag",
            "relationship": "the Responses signal is compared against the cached etag that originated from the /models response",
        },
    }


def _server_channels_contract() -> dict[str, Any]:
    return {
        "http_sse": {
            "outer_http_response_headers": {
                "effective_model": "openai-model",
                "models_etag": "X-Models-Etag",
                "reasoning_included": "x-reasoning-included",
                "upstream_request_id": "x-request-id",
                "turn_state": "x-codex-turn-state",
            },
            "stream_body": "text/event-stream; each SSE data record carries Responses JSON",
            "event_embedded_headers": "ResponsesStreamEvent may expose response.headers and top-level headers",
        },
        "websocket": {
            "upgrade_response_headers": {
                "effective_model": "openai-model",
                "reasoning_included": "x-reasoning-included",
                "turn_state": "x-codex-turn-state",
            },
            "frames": "server JSON text frames decoded as ResponsesStreamEvent",
            "event_embedded_headers": "response.headers has precedence for effective model; top-level headers is the fallback metadata-event location",
            "metadata_event_fields": {
                "models_etag": "x-models-etag from codex.response.metadata top-level headers",
                "turn_state": "x-codex-turn-state from response.metadata top-level headers",
            },
        },
    }


def _build_body(sources: dict[str, SourceFile], payload_model_consulted: bool) -> dict[str, Any]:
    return {
        "$schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "ownership": {
            "owns": [
                "server-to-client Responses response metadata placement",
                "effective/resolved model observation and normalization",
                "HTTP/SSE versus WebSocket response-channel distinctions",
                "X-Models-Etag invalidation and model-catalog refresh semantics",
            ],
            "does_not_own": [
                "client_metadata construction",
                "x-codex-turn-metadata construction",
                "request model selection",
                "backend routing policy that chooses the effective model",
            ],
        },
        "direction": "server_to_client",
        "effective_model": _effective_model_contract(payload_model_consulted),
        "models_catalog_invalidation": _models_catalog_contract(),
        "server_response_channels": _server_channels_contract(),
        "request_response_boundary": {
            "requested_model": "client request body model",
            "effective_model": "server response OpenAI-Model signal normalized to ServerModel",
            "client_metadata_direction": "client_to_server",
            "turn_metadata_direction": "client_to_server compatibility/canonical request metadata",
        },
        "evidence": {
            "common": _evidence(sources[COMMON], "ResponseEvent::ServerModel/ModelsEtag"),
            "sse": _evidence(sources[SSE], "spawn_response_stream/ResponsesStreamEvent::response_model"),
            "websocket": _evidence(sources[WS], "connect_websocket/run_websocket_response_stream"),
            "turn_dispatch": _evidence(sources[TURN], "ResponseEvent::ModelsEtag -> refresh_if_new_etag"),
            "models_manager": _evidence(sources[MODELS_MANAGER], "refresh_if_new_etag/fetch_and_update_models"),
            "models_endpoint": _evidence(sources[MODELS_ENDPOINT], "ModelsClient::request_url/list_models"),
            "model_provider_models": _evidence(sources[MODEL_PROVIDER_MODELS], "OpenAiModelsEndpoint::list_models"),
        },
    }


class ResponsesServerResponseExtractor:
    extractor_id = EXTRACTOR_ID
    source_spec_ids = SOURCE_SPECS

    def extract(self, snapshot: SourceSnapshot, diagnostics: DiagnosticCollector) -> ExtractorResult:
        sources = _load_sources(snapshot, diagnostics)
        if sources is None:
            return ExtractorResult(EXTRACTOR_ID, SCHEMA_VERSION, {}, False, SOURCE_SPECS)
        complete = _validate_wire_sources(
            sources[COMMON], sources[SSE], sources[WS], diagnostics
        )
        complete &= _validate_catalog_sources(
            sources[TURN],
            sources[MODELS_MANAGER],
            sources[MODELS_ENDPOINT],
            sources[MODEL_PROVIDER_MODELS],
            diagnostics,
        )
        authority_ok, payload_model_consulted = _check_response_model_authority(
            sources[SSE], diagnostics
        )
        complete &= authority_ok
        complete &= _check_manager_body(sources[MODELS_MANAGER], diagnostics)
        return _result(_build_body(sources, payload_model_consulted), complete)


@register_extractor(EXTRACTOR_ID)
def _factory() -> ResponsesServerResponseExtractor:
    return ResponsesServerResponseExtractor()
