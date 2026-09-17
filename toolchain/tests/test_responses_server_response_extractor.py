from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from codex_wire_audit.diagnostics import DiagnosticCollector
from codex_wire_audit.extractors import create_extractors
from codex_wire_audit.extractors.responses_server_response import ResponsesServerResponseExtractor
from codex_wire_audit.models import SourceFile, SourceGroup, SourceRevision, SourceSnapshot, SourceSpec


def _file(spec_id: str, key: str, path: str, text: str, group: SourceGroup = SourceGroup.EXTRA) -> SourceFile:
    spec = SourceSpec(
        id=spec_id,
        legacy_key=key,
        group=group,
        path_candidates=(path,),
        required=True,
        roles=("fixture",),
        expected_symbols=(),
        extractor_ids=(),
    )
    return SourceFile.create(spec=spec, selected_path=path, raw_bytes=text.encode())


def _snapshot(
    *,
    response_model_payload_authority: bool = False,
    break_ws_handshake: bool = False,
    break_models_refresh: bool = False,
    break_models_endpoint_etag: bool = False,
) -> SourceSnapshot:
    common = '''
pub enum ResponseEvent {
    /// Emitted when the server includes `OpenAI-Model` on the stream response.
    ServerModel(String),
    ModelsEtag(String),
}
'''
    response_model_lookup = 'let legacy = response.get("model");' if response_model_payload_authority else ''
    sse = f'''
const X_REASONING_INCLUDED_HEADER: &str = "x-reasoning-included";
const X_CODEX_TURN_STATE_HEADER: &str = "x-codex-turn-state";
const OPENAI_MODEL_HEADER: &str = "openai-model";
const REQUEST_ID_HEADER: &str = "x-request-id";
fn spawn_response_stream(stream_response: StreamResponse) {{
    let models_etag = stream_response.headers.get("X-Models-Etag");
    let server_model = stream_response
        .headers
        .get(OPENAI_MODEL_HEADER);
    let reasoning_included = stream_response.headers.get(X_REASONING_INCLUDED_HEADER);
    let upstream_request_id = stream_response.headers.get(REQUEST_ID_HEADER);
    let turn_state = stream_response.headers.get(X_CODEX_TURN_STATE_HEADER);
    if let Some(etag) = models_etag {{
        tx_event.send(Ok(ResponseEvent::ModelsEtag(etag))).await;
    }}
}}
pub struct ResponsesStreamEvent {{ response: Option<Value>, headers: Option<Value> }}
impl ResponsesStreamEvent {{
    pub fn response_model(&self) -> Option<String> {{
        {response_model_lookup}
        let response_headers_model = self
            .response
            .as_ref()
            .and_then(|response| response.get("headers"))
            .and_then(header_openai_model_value_from_json);
        match response_headers_model {{
            Some(model) => Some(model),
            None => self
                .headers
                .as_ref()
                .and_then(header_openai_model_value_from_json),
        }}
    }}
}}
fn header_openai_model_value_from_json(value: &Value) -> Option<String> {{
    value.as_object()?.iter().find_map(|(name, value)| {{
        if name.eq_ignore_ascii_case("openai-model") || name.eq_ignore_ascii_case("x-openai-model") {{
            json_value_as_string(value)
        }} else {{ None }}
    }})
}}
async fn process() {{
    if let Some(model) = event.response_model()
        && last_server_model.as_deref() != Some(model.as_str())
    {{
        tx_event.send(Ok(ResponseEvent::ServerModel(model.clone()))).await;
    }}
}}
'''
    ws_header_read = '''let server_model = response
        .headers()
        .get(OPENAI_MODEL_HEADER);''' if not break_ws_handshake else 'let server_model = None;'
    ws = f'''
const X_MODELS_ETAG_HEADER: &str = "x-models-etag";
pub struct ResponsesWebsocketConnection {{ server_model: Option<String> }}
fn connect_websocket() {{
    let reasoning_included = response.headers().contains_key(X_REASONING_INCLUDED_HEADER);
    {ws_header_read}
    let turn_state = response.headers().get(X_CODEX_TURN_STATE_HEADER);
}}
async fn run_websocket_response_stream() {{
    if event.kind() == "codex.response.metadata" {{
        headers.get(X_MODELS_ETAG_HEADER);
        tx_event.send(Ok(ResponseEvent::ModelsEtag(etag))).await;
    }}
    event.turn_state();
    if let Some(model) = event.response_model()
        && last_server_model.as_deref() != Some(model.as_str())
    {{
        tx_event.send(Ok(ResponseEvent::ServerModel(model.clone()))).await;
    }}
}}
'''
    turn = '''
match event {
    ResponseEvent::ModelsEtag(etag) => {
        sess.services
            .models_manager
            .refresh_if_new_etag(etag, turn_context.config.http_client_factory())
            .await;
    }
}
'''
    online_refresh = (
        '.refresh_available_models(RefreshStrategy::Offline, &http_client_factory)'
        if break_models_refresh
        else '.refresh_available_models(RefreshStrategy::Online, &http_client_factory)'
    )
    manager = f'''
/// Uses `Online` strategy to fetch latest models when ETags differ.
async fn refresh_if_new_etag(&self, etag: String, http_client_factory: HttpClientFactory) {{
    let (identity, current_etag) = (entry.identity.clone(), entry.etag.clone());
    if let Some(identity) = identity
        && Some(&identity) == self.endpoint_client.identity().as_ref()
        && current_etag.as_deref() == Some(etag.as_str())
    {{
        cache.refresh_ttl(&crate::client_version_to_whole(), &identity, &etag).await;
        return;
    }}
    self{online_refresh}.await;
}}
async fn refresh_available_models(&self, refresh_strategy: RefreshStrategy, http_client_factory: &HttpClientFactory) {{
    match refresh_strategy {{
        RefreshStrategy::Online => self.fetch_and_update_models(http_client_factory).await,
        _ => Ok(()),
    }}
}}
async fn fetch_and_update_models(&self, http_client_factory: &HttpClientFactory) {{
    let client_version = crate::client_version_to_whole();
    let ModelsEndpointResponse {{ models, etag, identity }} = self
        .endpoint_client
        .list_models(&client_version, http_client_factory.clone())
        .await?;
    let entry = ModelsCacheEntry {{
        fetched_at: Utc::now(),
        etag,
        client_version: Some(client_version),
        identity: Some(identity),
        models,
    }};
    cache.store(&entry).await;
    self.apply_remote_models(entry).await;
}}
'''
    provider_models = '''
const MODELS_ENDPOINT: &str = "/models";
async fn list_models(&self, client_version: &str) {
    let request_url =
        ModelsClient::<ReqwestTransport>::request_url(&api_provider, client_version);
    let (models, etag) = client
        .list_models(request_url, HeaderMap::new())
        .await?;
    Ok(ModelsEndpointResponse {
        models,
        etag,
        identity,
    })
}
'''
    etag_read = '.get(OTHER_HEADER)' if break_models_endpoint_etag else '.get(ETAG)'
    models_endpoint = f'''
fn path() -> &'static str {{
        "models"
}}
fn append_client_version_query(req: &mut Request, client_version: &str) {{
    req.url = format!("{{}}?client_version={{client_version}}", req.url);
}}
pub fn request_url(provider: &Provider, client_version: &str) -> String {{
    let mut request = provider.build_request(Method::GET, Self::path());
    Self::append_client_version_query(&mut request, client_version);
    request.url
}}
pub async fn list_models(&self) {{
    let header_etag = resp
        .headers
        {etag_read};
    let ModelsResponse {{ models }} = serde_json::from_slice::<ModelsResponse>(&resp.body)?;
    Ok((models, header_etag))
}}
'''
    rows = {
        "source_spec.base.common": _file("source_spec.base.common", "common", "codex-rs/codex-api/src/common.rs", common, SourceGroup.BASE),
        "source_spec.base.response_sse": _file("source_spec.base.response_sse", "response_sse", "codex-rs/codex-api/src/sse/responses.rs", sse, SourceGroup.BASE),
        "source_spec.base.ws": _file("source_spec.base.ws", "ws", "codex-rs/codex-api/src/endpoint/responses_websocket.rs", ws, SourceGroup.BASE),
        "source_spec.extra.prompt_turn": _file("source_spec.extra.prompt_turn", "prompt_turn", "codex-rs/core/src/session/turn.rs", turn),
        "source_spec.extra.models_manager": _file("source_spec.extra.models_manager", "models_manager", "codex-rs/models-manager/src/manager.rs", manager),
        "source_spec.surface.models_endpoint": _file("source_spec.surface.models_endpoint", "models_endpoint", "codex-rs/codex-api/src/endpoint/models.rs", models_endpoint, SourceGroup.SURFACE),
        "source_spec.surface.model_provider_models": _file("source_spec.surface.model_provider_models", "model_provider_models", "codex-rs/model-provider/src/models_endpoint.rs", provider_models, SourceGroup.SURFACE),
    }
    revision = SourceRevision("fixture", "openai/codex", "fixture", "1" * 40, SourceSnapshot.digest_files(rows), False)
    return SourceSnapshot(revision, rows)


def test_server_response_extractor_is_registered() -> None:
    assert "extractor.responses_server_response" in {extractor.extractor_id for extractor in create_extractors()}


def test_effective_model_locations_and_transport_distinction_are_derived() -> None:
    diagnostics = DiagnosticCollector()
    result = ResponsesServerResponseExtractor().extract(_snapshot(), diagnostics)
    assert result.semantic_complete
    assert diagnostics.summary()["error"] == 0

    model = result.data["effective_model"]
    assert result.data["direction"] == "server_to_client"
    assert model["wire_header"] == "openai-model"
    assert model["event_json_header_aliases"] == ["openai-model", "x-openai-model"]
    assert model["http_sse"]["outer_http_response_header"] == "openai-model"
    assert model["http_sse"]["event_json_precedence"] == ["response.headers", "headers"]
    assert model["websocket"]["upgrade_response_header"] == "openai-model"
    assert model["websocket"]["upgrade_scope"].startswith("HTTP 101")
    assert model["response_model_payload_field_consulted"] is False
    assert model["not_client_metadata"] is True
    assert model["not_turn_metadata"] is True


def test_models_etag_is_invalidation_signal_then_separate_catalog_fetch() -> None:
    result = ResponsesServerResponseExtractor().extract(_snapshot(), DiagnosticCollector())
    contract = result.data["models_catalog_invalidation"]

    assert contract["signal"]["http_sse"]["header"] == "X-Models-Etag"
    assert contract["signal"]["websocket"]["header"] == "x-models-etag"
    assert contract["signal"]["catalog_body_embedded"] is False
    assert contract["signal"]["normalized_event"] == "ResponseEvent::ModelsEtag(String)"
    assert "refresh_if_new_etag" in contract["dispatch"]
    assert "do not refetch /models" in contract["comparison"]["on_match"]
    assert "RefreshStrategy::Online" in contract["comparison"]["on_mismatch_or_identity_change"]
    assert contract["catalog_fetch"]["separate_request"] is True
    assert contract["catalog_fetch"]["method"] == "GET"
    assert contract["catalog_fetch"]["path"] == "/models"
    assert "client_version" in contract["catalog_fetch"]["query"]
    assert contract["catalog_fetch"]["response_version_header"] == "ETag"
    assert "ModelsResponse" in contract["catalog_fetch"]["response_body"]
    assert "ModelsCacheEntry" in contract["catalog_update"]["cache_entry"]
    assert contract["wire_version_relation"]["models_response_version"] == "ETag"


def test_response_model_payload_becoming_authoritative_fails_closed() -> None:
    diagnostics = DiagnosticCollector()
    result = ResponsesServerResponseExtractor().extract(
        _snapshot(response_model_payload_authority=True), diagnostics
    )
    assert result.semantic_complete is False
    assert result.data["effective_model"]["response_model_payload_field_consulted"] is True
    assert "RESPONSES_EFFECTIVE_MODEL_AUTHORITY_CHANGED" in {item.code for item in diagnostics.values()}


def test_websocket_handshake_model_header_drift_fails_closed() -> None:
    diagnostics = DiagnosticCollector()
    result = ResponsesServerResponseExtractor().extract(_snapshot(break_ws_handshake=True), diagnostics)
    assert result.semantic_complete is False
    assert "RESPONSES_WS_UPGRADE_MODEL_HEADER_MISSING" in {item.code for item in diagnostics.values()}


def test_models_etag_refresh_policy_drift_fails_closed() -> None:
    diagnostics = DiagnosticCollector()
    result = ResponsesServerResponseExtractor().extract(_snapshot(break_models_refresh=True), diagnostics)
    assert result.semantic_complete is False
    assert "RESPONSES_MODELS_ETAG_ONLINE_REFRESH_MISSING" in {item.code for item in diagnostics.values()}


def test_models_endpoint_etag_drift_fails_closed() -> None:
    diagnostics = DiagnosticCollector()
    result = ResponsesServerResponseExtractor().extract(
        _snapshot(break_models_endpoint_etag=True), diagnostics
    )
    assert result.semantic_complete is False
    assert "RESPONSES_MODELS_HTTP_ETAG_MISSING" in {item.code for item in diagnostics.values()}


def test_server_response_schema_validates_extractor_output() -> None:
    result = ResponsesServerResponseExtractor().extract(_snapshot(), DiagnosticCollector())
    schema_path = (
        Path(__file__).parents[1]
        / "codex_wire_audit"
        / "proof_schema_templates"
        / "responses-server-response-semantics-v1.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert list(Draft202012Validator(schema).iter_errors(result.data)) == []
