from __future__ import annotations

import json
from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one occurrence, found {count}")
    return text.replace(old, new, 1)


ROOT = Path(__file__).resolve().parents[2]
TOOLCHAIN = ROOT / "toolchain"
PKG = TOOLCHAIN / "codex_wire_audit"

helper = '''"""Deterministic app-server history mutation semantics."""
from __future__ import annotations

from typing import Any

from ..diagnostics import DiagnosticCollector
from ..models import SourceFile

APP_SERVER_THREAD_PROCESSOR = "source_spec.extra.app_server_thread_processor"
TUI_APP_SERVER_SESSION = "source_spec.extra.app_server_tui_session"
TUI_BACKTRACK = "source_spec.extra.local_storage_tui_backtrack"
STORAGE_PAGINATED_FORK = "source_spec.extra.local_storage_paginated_fork"
STORAGE_REVERT = "source_spec.extra.local_storage_revert_thread"


def _require(
    diagnostics: DiagnosticCollector,
    *,
    extractor_id: str,
    source: SourceFile,
    entity: str,
    tokens: tuple[tuple[str, str], ...],
) -> bool:
    complete = True
    for code, token in tokens:
        if token in source.text:
            continue
        complete = False
        diagnostics.emit(
            code=code,
            severity="error",
            category="app_server_rpc",
            message=f"Required history-mutation evidence is missing: {token}",
            extractor_id=extractor_id,
            entity_id=entity,
            source_refs=[source.spec_id],
            details={"path": source.selected_path, "token": token},
            recoverable=False,
            strict_failure=True,
        )
    return complete


def validate_history_mutation_sources(
    *,
    diagnostics: DiagnosticCollector,
    extractor_id: str,
    processor: SourceFile,
    tui_session: SourceFile,
    tui_backtrack: SourceFile,
    storage_fork: SourceFile,
    storage_revert: SourceFile,
) -> bool:
    complete = _require(
        diagnostics,
        extractor_id=extractor_id,
        source=processor,
        entity="app_server_rpc.history_mutation.orchestration",
        tokens=(
            ("APP_SERVER_FORK_HANDLER_MISSING", "async fn thread_fork_inner"),
            ("APP_SERVER_FORK_PAGINATED_GATE_MISSING", "let paginated_source = matches!(source_thread.history_mode, ThreadHistoryMode::Paginated)"),
            ("APP_SERVER_FORK_PREPARE_MISSING", ".prepare_fork(codex_thread_store::PrepareForkParams"),
            ("APP_SERVER_FORK_PREPARED_START_MISSING", ".fork_prepared_thread("),
            ("APP_SERVER_FORK_NEW_THREAD_ROW_MISSING", '"thread/fork"'),
            ("APP_SERVER_REVERT_HANDLER_MISSING", "async fn thread_revert_response"),
            ("APP_SERVER_REVERT_PAGINATED_ONLY_MISSING", '"thread/revert only supports paginated threads"'),
            ("APP_SERVER_REVERT_SHUTDOWN_MISSING", "wait_for_thread_shutdown(&thread).await"),
            ("APP_SERVER_REVERT_REMOVE_LOADED_MISSING", ".remove_thread(&thread_id)"),
            ("APP_SERVER_REVERT_STORE_CALL_MISSING", ".revert_thread(codex_thread_store::RevertThreadParams"),
            ("APP_SERVER_REVERT_RELOAD_MISSING", "async fn reload_paginated_thread"),
            ("APP_SERVER_REVERT_IDENTITY_GUARD_MISSING", "if resumed_thread_id != thread_id"),
            ("APP_SERVER_REVERT_SUBSCRIPTION_PRESERVE_MISSING", "Keep thread state and subscriptions across the internal reload"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=tui_session,
        entity="app_server_rpc.history_mutation.tui",
        tokens=(
            ("APP_SERVER_TUI_FORK_AT_MISSING", "pub(crate) async fn fork_thread_at"),
            ("APP_SERVER_TUI_FORK_PARAMS_MISSING", "let mut params = ThreadForkParams"),
            ("APP_SERVER_TUI_FORK_RPC_MISSING", "ClientRequest::ThreadFork"),
            ("APP_SERVER_TUI_FORK_BOUNDARY_MISSING", "before_turn_id,"),
            ("APP_SERVER_TUI_PAGINATED_EXCLUDE_TURNS_MISSING", "ThreadHistorySupport::Paginated"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=tui_backtrack,
        entity="app_server_rpc.history_mutation.tui",
        tokens=(
            ("APP_SERVER_TUI_SOURCE_PRESERVING_BRANCH_MISSING", "source-preserving branch"),
            ("APP_SERVER_TUI_PROMPT_EDIT_FORK_MISSING", "ForkSessionForPromptEdit"),
            ("APP_SERVER_TUI_STEER_BOUNDARY_MISSING", "app-server cannot fork in the middle of a turn"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=storage_fork,
        entity="app_server_rpc.history_mutation.storage_bridge",
        tokens=(
            ("APP_SERVER_PAGINATED_FORK_BOUNDARY_LATEST_MISSING", "ForkBoundary::Latest"),
            ("APP_SERVER_PAGINATED_FORK_BOUNDARY_THROUGH_MISSING", "ForkBoundary::ThroughTurn"),
            ("APP_SERVER_PAGINATED_FORK_BOUNDARY_BEFORE_MISSING", "ForkBoundary::BeforeTurn"),
            ("APP_SERVER_PAGINATED_FORK_HISTORY_POSITION_MISSING", "HistoryPosition"),
            ("APP_SERVER_PAGINATED_FORK_HISTORY_BASE_MISSING", "history_base_at_boundary"),
        ),
    )
    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=storage_revert,
        entity="app_server_rpc.history_mutation.storage_bridge",
        tokens=(
            ("APP_SERVER_REVERT_NEW_ROLLOUT_ID_MISSING", "let rollout_id = ThreadId::new();"),
            ("APP_SERVER_REVERT_ROLLOUT_OVERRIDE_MISSING", ".with_rollout_id(rollout_id)"),
            ("APP_SERVER_REVERT_STABLE_THREAD_ID_MISSING", "RolloutRecorderParams::new(\n        source_meta.id,"),
            ("APP_SERVER_REVERT_POINTER_CAS_MISSING", ".replace_rollout_path_if_current("),
        ),
    )
    return complete


def build_history_mutation() -> dict[str, Any]:
    return {
        "fork": {
            "rpc": "thread/fork",
            "logical_identity": "new thread_id",
            "source_thread_preserved": True,
            "source_relation": "new thread forked_from_id references source thread_id",
            "boundaries": {
                "latest": "ForkBoundary::Latest",
                "lastTurnId": "ForkBoundary::ThroughTurn; referenced completed turn is included",
                "beforeTurnId": "ForkBoundary::BeforeTurn; referenced turn and all later turns are excluded",
                "last_and_before_mutually_exclusive": True,
            },
            "paginated_source": {
                "preparation": "thread_store.prepare_fork computes immutable source HistoryPosition/history_base",
                "model_context": "loaded from source lineage through the prepared history_base",
                "new_runtime": "thread_manager.fork_prepared_thread starts the new logical thread",
                "full_history_hydration": "optional compatibility response path; excludeTurns avoids eager hydration",
            },
            "legacy_source": "forks from reconstructed/truncated source history rather than PreparedFork",
            "notification": "thread/started",
        },
        "revert": {
            "rpc": "thread/revert",
            "logical_identity": "preserved thread_id",
            "supported_history_mode": "Paginated",
            "boundary": "beforeTurnId excludes the referenced turn and every later turn",
            "local_file_changes_reverted": False,
            "loaded_thread_orchestration": [
                "ensure listener and register shutdown drain waiter",
                "shut down loaded runtime and drain shutdown events",
                "remove runtime from ThreadManager",
                "preserve app-server thread state/subscriptions while cancelling pending requests",
                "delegate durable replacement to thread_store.revert_thread",
                "reload the same logical thread from the replacement rollout",
                "restore runtime settings and restart the listener",
            ],
            "storage_bridge": {
                "replacement_rollout_id": "new UUID",
                "logical_thread_id": "preserved",
                "replacement_filename_semantics": "A_R: A is stable thread_id and R is replacement rollout_id",
                "state_cutover": "compare-and-swap the existing thread row's rollout_path",
                "new_thread_row": False,
            },
            "notification": "thread/reverted",
        },
        "tui_prompt_edit_policy": {
            "operation": "thread/fork",
            "boundary": "beforeTurnId for the selected initial prompt's turn",
            "source_preserving": True,
            "steer_is_independent_branch_boundary": False,
            "uses_thread_revert": False,
        },
        "desktop_observation_classifier": {
            "public_source_claim": "Desktop host choice is not determined by public Codex source; classify an observation by public fork/revert signatures instead of assuming the host route",
            "revert_signature": "same thread.id; replacement A_R rollout path; thread/reverted; existing thread row changes rollout_path",
            "fork_signature": "new thread.id B; forked_from_id=A; thread/started; source A remains independently addressable",
        },
    }
'''
(PKG / "extractors" / "app_server_history_mutation.py").write_text(helper)

p = PKG / "extractors" / "app_server_rpc.py"
text = p.read_text()
text = replace_once(
    text,
    "from .registry import ExtractorResult, register_extractor\n",
    "from .registry import ExtractorResult, register_extractor\nfrom .app_server_history_mutation import (\n    APP_SERVER_THREAD_PROCESSOR,\n    STORAGE_PAGINATED_FORK,\n    STORAGE_REVERT,\n    TUI_APP_SERVER_SESSION,\n    TUI_BACKTRACK,\n    build_history_mutation,\n    validate_history_mutation_sources,\n)\n",
    "history mutation import",
)
text = replace_once(text, 'SCHEMA_VERSION = "1.0.0"\nSCHEMA_ID = "https://schemas.codex-system-contract.invalid/app-server/rpc-lifecycle-v1.schema.json"', 'SCHEMA_VERSION = "2.0.0"\nSCHEMA_ID = "https://schemas.codex-system-contract.invalid/app-server/rpc-lifecycle-v2.schema.json"', "schema version")
text = replace_once(
    text,
    '    "rpc": "source_spec.extra.app_server_rpc",\n}',
    '    "rpc": "source_spec.extra.app_server_rpc",\n    "processor": APP_SERVER_THREAD_PROCESSOR,\n    "tui_session": TUI_APP_SERVER_SESSION,\n    "tui_backtrack": TUI_BACKTRACK,\n    "storage_fork": STORAGE_PAGINATED_FORK,\n    "storage_revert": STORAGE_REVERT,\n}',
    "source ids",
)
text = replace_once(text, '    "thread/items/list",\n    "turn/start",', '    "thread/items/list",\n    "thread/fork",\n    "thread/revert",\n    "turn/start",', "core methods")
text = replace_once(text, '    "thread/status/changed",\n    "turn/started",', '    "thread/status/changed",\n    "thread/reverted",\n    "turn/started",', "core notifications")
text = replace_once(
    text,
    '    rpc: SourceFile,\n    requests: list[dict[str, str]],',
    '    rpc: SourceFile,\n    processor: SourceFile,\n    tui_session: SourceFile,\n    tui_backtrack: SourceFile,\n    storage_fork: SourceFile,\n    storage_revert: SourceFile,\n    history_mutation: dict[str, Any],\n    requests: list[dict[str, str]],',
    "builder args",
)
text = replace_once(
    text,
    '                "thread/turn/item pagination request shape",\n',
    '                "thread/turn/item pagination request shape",\n                "fork/revert history mutation orchestration and client-visible identity semantics",\n',
    "ownership",
)
text = replace_once(text, '        "pagination": {', '        "history_mutation": history_mutation,\n        "pagination": {', "history mutation body")
text = replace_once(
    text,
    '            "rpc": _evidence(rpc, "JSONRPCMessage/JSONRPCRequest/JSONRPCNotification"),\n',
    '            "rpc": _evidence(rpc, "JSONRPCMessage/JSONRPCRequest/JSONRPCNotification"),\n            "processor": _evidence(processor, "thread_fork_inner/thread_revert_response/reload_paginated_thread"),\n            "tui_session": _evidence(tui_session, "fork_thread_at/ClientRequest::ThreadFork"),\n            "tui_backtrack": _evidence(tui_backtrack, "ForkSessionForPromptEdit"),\n            "storage_fork": _evidence(storage_fork, "prepare/history_base_at_boundary"),\n            "storage_revert": _evidence(storage_revert, "revert/create_replacement_recorder"),\n',
    "evidence",
)
text = replace_once(
    text,
    '        rpc = sources["rpc"]\n        assert common and thread and thread_data and turn and item and rpc',
    '        rpc = sources["rpc"]\n        processor = sources["processor"]\n        tui_session = sources["tui_session"]\n        tui_backtrack = sources["tui_backtrack"]\n        storage_fork = sources["storage_fork"]\n        storage_revert = sources["storage_revert"]\n        assert common and thread and thread_data and turn and item and rpc and processor and tui_session and tui_backtrack and storage_fork and storage_revert',
    "source bindings",
)
text = replace_once(
    text,
    '                ("APP_SERVER_THREAD_ITEMS_LIST_MISSING", "pub struct ThreadItemsListParams"),\n                ("APP_SERVER_THREAD_STATUS_MISSING", "pub enum ThreadStatus"),',
    '                ("APP_SERVER_THREAD_ITEMS_LIST_MISSING", "pub struct ThreadItemsListParams"),\n                ("APP_SERVER_THREAD_FORK_PARAMS_MISSING", "pub struct ThreadForkParams"),\n                ("APP_SERVER_THREAD_REVERT_PARAMS_MISSING", "pub struct ThreadRevertParams"),\n                ("APP_SERVER_THREAD_STATUS_MISSING", "pub enum ThreadStatus"),',
    "protocol history params",
)
needle = '''        complete &= _require(
            diagnostics,
            item,
            (("APP_SERVER_THREAD_ITEM_MISSING", "pub enum ThreadItem"),),
            entity="app_server_rpc.item",
        )

        requests = _request_dispatch(common.text)
'''
replacement = '''        complete &= _require(
            diagnostics,
            item,
            (("APP_SERVER_THREAD_ITEM_MISSING", "pub enum ThreadItem"),),
            entity="app_server_rpc.item",
        )
        complete &= validate_history_mutation_sources(
            diagnostics=diagnostics,
            extractor_id=EXTRACTOR_ID,
            processor=processor,
            tui_session=tui_session,
            tui_backtrack=tui_backtrack,
            storage_fork=storage_fork,
            storage_revert=storage_revert,
        )
        history_mutation = build_history_mutation()

        requests = _request_dispatch(common.text)
'''
text = replace_once(text, needle, replacement, "history validation")
text = replace_once(
    text,
    '            rpc=rpc,\n            requests=requests,',
    '            rpc=rpc,\n            processor=processor,\n            tui_session=tui_session,\n            tui_backtrack=tui_backtrack,\n            storage_fork=storage_fork,\n            storage_revert=storage_revert,\n            history_mutation=history_mutation,\n            requests=requests,',
    "builder call",
)
p.write_text(text)

registry = PKG / "source_registry.py"
text = registry.read_text()
anchor = '''    ("source_spec.extra.app_server_rpc", "app_server_rpc", "codex-rs/app-server-protocol/src/rpc.rs", ("JSONRPCMessage", "JSONRPCRequest", "JSONRPCNotification", "JSONRPCResponse", "JSONRPCError")),
)'''
rows = '''    ("source_spec.extra.app_server_rpc", "app_server_rpc", "codex-rs/app-server-protocol/src/rpc.rs", ("JSONRPCMessage", "JSONRPCRequest", "JSONRPCNotification", "JSONRPCResponse", "JSONRPCError")),
    ("source_spec.extra.app_server_thread_processor", "app_server_thread_processor", "codex-rs/app-server/src/request_processors/thread_processor.rs", ("thread_fork_inner", "thread_revert_response", "reload_paginated_thread", "prepare_fork", "revert_thread")),
    ("source_spec.extra.app_server_tui_session", "app_server_tui_session", "codex-rs/tui/src/app_server_session.rs", ("fork_thread_at", "ThreadForkParams", "ClientRequest::ThreadFork", "ThreadHistorySupport::Paginated")),
    ("source_spec.extra.local_storage_tui_backtrack", "local_storage_tui_backtrack", "codex-rs/tui/src/app_backtrack.rs", ("backtrack_fork_before_turn_id", "ForkSessionForPromptEdit", "source-preserving branch", "cannot fork in the middle of a turn")),
    ("source_spec.extra.local_storage_paginated_fork", "local_storage_paginated_fork", "codex-rs/thread-store/src/local/paginated_fork.rs", ("HistoryPosition", "ForkBoundary::Latest", "ForkBoundary::ThroughTurn", "ForkBoundary::BeforeTurn", "history_base_at_boundary")),
    ("source_spec.extra.local_storage_revert_thread", "local_storage_revert_thread", "codex-rs/thread-store/src/local/revert_thread.rs", ("let rollout_id = ThreadId::new();", "with_rollout_id", "replace_rollout_path_if_current")),
)'''
text = replace_once(text, anchor, rows, "app-server source specs")
registry.write_text(text)

v1 = PKG / "proof_schema_templates" / "app-server-rpc-lifecycle-v1.schema.json"
schema = json.loads(v1.read_text())
schema_id = "https://schemas.codex-system-contract.invalid/app-server/rpc-lifecycle-v2.schema.json"
schema["$id"] = schema_id
schema["title"] = "Codex app-server RPC lifecycle and history mutation semantics"
schema["required"].append("history_mutation")
schema["properties"]["$schema"] = {"const": schema_id}
schema["properties"]["schema_version"] = {"const": "2.0.0"}
schema["properties"]["request_dispatch"]["minItems"] = 11
schema["properties"]["notification_dispatch"]["minItems"] = 7
schema["properties"]["evidence"]["minProperties"] = 11
schema["properties"]["history_mutation"] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fork", "revert", "tui_prompt_edit_policy", "desktop_observation_classifier"],
    "properties": {
        "fork": {"type": "object", "additionalProperties": True, "required": ["rpc", "logical_identity", "source_thread_preserved", "source_relation", "boundaries", "paginated_source", "legacy_source", "notification"], "properties": {"rpc": {"const": "thread/fork"}, "logical_identity": {"const": "new thread_id"}, "source_thread_preserved": {"const": True}, "source_relation": {"type": "string"}, "boundaries": {"type": "object"}, "paginated_source": {"type": "object"}, "legacy_source": {"type": "string"}, "notification": {"const": "thread/started"}}},
        "revert": {"type": "object", "additionalProperties": True, "required": ["rpc", "logical_identity", "supported_history_mode", "boundary", "local_file_changes_reverted", "loaded_thread_orchestration", "storage_bridge", "notification"], "properties": {"rpc": {"const": "thread/revert"}, "logical_identity": {"const": "preserved thread_id"}, "supported_history_mode": {"const": "Paginated"}, "boundary": {"type": "string"}, "local_file_changes_reverted": {"const": False}, "loaded_thread_orchestration": {"type": "array", "minItems": 7, "items": {"type": "string"}}, "storage_bridge": {"type": "object"}, "notification": {"const": "thread/reverted"}}},
        "tui_prompt_edit_policy": {"type": "object", "additionalProperties": False, "required": ["operation", "boundary", "source_preserving", "steer_is_independent_branch_boundary", "uses_thread_revert"], "properties": {"operation": {"const": "thread/fork"}, "boundary": {"type": "string"}, "source_preserving": {"const": True}, "steer_is_independent_branch_boundary": {"const": False}, "uses_thread_revert": {"const": False}}},
        "desktop_observation_classifier": {"type": "object", "additionalProperties": False, "required": ["public_source_claim", "revert_signature", "fork_signature"], "properties": {"public_source_claim": {"type": "string"}, "revert_signature": {"type": "string"}, "fork_signature": {"type": "string"}}},
    },
}
(PKG / "proof_schema_templates" / "app-server-rpc-lifecycle-v2.schema.json").write_text(json.dumps(schema, indent=2) + "\n")

release = PKG / "release_spec.v1.json"
text = release.read_text()
text = replace_once(
    text,
    '        "extractors/app_server_rpc.py",\n        "proof_schema_templates/app-server-rpc-lifecycle-v1.schema.json",',
    '        "extractors/app_server_rpc.py",\n        "extractors/app_server_history_mutation.py",\n        "proof_schema_templates/app-server-rpc-lifecycle-v2.schema.json",',
    "release resources",
)
release.write_text(text)

tests = TOOLCHAIN / "tests" / "test_app_server_rpc_extractor.py"
text = tests.read_text()
text = text.replace('    ThreadItemsList => "thread/items/list" {\n        params: v2::ThreadItemsListParams,\n        serialization: thread_id(params.thread_id),\n        response: v2::ThreadItemsListResponse,\n    },\n', '    ThreadItemsList => "thread/items/list" {\n        params: v2::ThreadItemsListParams,\n        serialization: thread_id(params.thread_id),\n        response: v2::ThreadItemsListResponse,\n    },\n    ThreadFork => "thread/fork" {\n        params: v2::ThreadForkParams,\n        serialization: thread_id(params.thread_id),\n        response: v2::ThreadForkResponse,\n    },\n    ThreadRevert => "thread/revert" {\n        params: v2::ThreadRevertParams,\n        serialization: thread_id(params.thread_id),\n        response: v2::ThreadRevertResponse,\n    },\n')
text = text.replace('    ThreadStatusChanged => "thread/status/changed" (v2::ThreadStatusChangedNotification),\n', '    ThreadStatusChanged => "thread/status/changed" (v2::ThreadStatusChangedNotification),\n    ThreadReverted => "thread/reverted" (v2::ThreadRevertedNotification),\n')
text = text.replace('pub struct ThreadItemsListParams { pub thread_id: String, pub turn_id: Option<String>, pub cursor: Option<String>, pub limit: Option<usize> }\npub enum ThreadStatus', 'pub struct ThreadItemsListParams { pub thread_id: String, pub turn_id: Option<String>, pub cursor: Option<String>, pub limit: Option<usize> }\npub struct ThreadForkParams { pub thread_id: String, pub last_turn_id: Option<String>, pub before_turn_id: Option<String>, pub exclude_turns: bool }\npub struct ThreadRevertParams { pub thread_id: String, pub before_turn_id: String }\npub enum ThreadStatus')
insert = '''    processor = ''' + "'''" + '''
async fn thread_fork_inner() {
    let paginated_source = matches!(source_thread.history_mode, ThreadHistoryMode::Paginated);
    self.thread_store.prepare_fork(codex_thread_store::PrepareForkParams { thread_id: source_thread_id, boundary }).await;
    self.thread_manager.fork_prepared_thread(config, prepared_fork, thread_source, parent_trace, client_mcp_extensions, reserved_thread_id).await;
    stage_pending_thread_metadata(self.thread_manager.as_ref(), self.thread_store.as_ref(), patch, "thread/fork").await;
}
async fn thread_revert_response() {
    "thread/revert only supports paginated threads";
    wait_for_thread_shutdown(&thread).await;
    self.thread_manager.remove_thread(&thread_id).await;
    // Keep thread state and subscriptions across the internal reload.
    self.thread_store.revert_thread(codex_thread_store::RevertThreadParams { thread_id, before_turn_id, multi_agent_version }).await;
}
async fn reload_paginated_thread() { if resumed_thread_id != thread_id { panic!(); } }
''' + "'''" + '''
    tui_session = ''' + "'''" + '''
pub(crate) async fn fork_thread_at() {
    ThreadHistorySupport::Paginated;
    let mut params = ThreadForkParams { before_turn_id, exclude_turns, ..Default::default() };
    ClientRequest::ThreadFork { request_id, params };
}
''' + "'''" + '''
    tui_backtrack = ''' + "'''" + '''
// source-preserving branch
// app-server cannot fork in the middle of a turn.
AppEvent::ForkSessionForPromptEdit { thread_id, nth_user_message, prompt };
''' + "'''" + '''
    storage_fork = ''' + "'''" + '''
HistoryPosition;
ForkBoundary::Latest;
ForkBoundary::ThroughTurn(turn_id);
ForkBoundary::BeforeTurn(turn_id);
fn history_base_at_boundary() {}
''' + "'''" + '''
    storage_revert = ''' + "'''" + '''
let rollout_id = ThreadId::new();
let params = RolloutRecorderParams::new(
        source_meta.id,
        source_meta.forked_from_id,
);
params.with_rollout_id(rollout_id);
state_db.replace_rollout_path_if_current(thread_id, expected, replacement).await;
''' + "'''" + '''
'''
text = replace_once(text, '    rpc = ''' + "'''" + '''\n//! We do not do true JSON-RPC 2.0, as we neither send nor expect the "jsonrpc": "2.0" field.\n''', insert + '    rpc = ''' + "'''" + '''\n//! We do not do true JSON-RPC 2.0, as we neither send nor expect the "jsonrpc": "2.0" field.\n''', "test fixtures")
text = replace_once(
    text,
    '        "rpc": _file("source_spec.extra.app_server_rpc", "app_server_rpc", "codex-rs/app-server-protocol/src/rpc.rs", rpc),\n',
    '        "rpc": _file("source_spec.extra.app_server_rpc", "app_server_rpc", "codex-rs/app-server-protocol/src/rpc.rs", rpc),\n        "processor": _file("source_spec.extra.app_server_thread_processor", "app_server_thread_processor", "codex-rs/app-server/src/request_processors/thread_processor.rs", processor),\n        "tui_session": _file("source_spec.extra.app_server_tui_session", "app_server_tui_session", "codex-rs/tui/src/app_server_session.rs", tui_session),\n        "tui_backtrack": _file("source_spec.extra.local_storage_tui_backtrack", "local_storage_tui_backtrack", "codex-rs/tui/src/app_backtrack.rs", tui_backtrack),\n        "storage_fork": _file("source_spec.extra.local_storage_paginated_fork", "local_storage_paginated_fork", "codex-rs/thread-store/src/local/paginated_fork.rs", storage_fork),\n        "storage_revert": _file("source_spec.extra.local_storage_revert_thread", "local_storage_revert_thread", "codex-rs/thread-store/src/local/revert_thread.rs", storage_revert),\n',
    "test source rows",
)
text = replace_once(
    text,
    '        "source_spec.extra.app_server_rpc",\n    }',
    '        "source_spec.extra.app_server_rpc",\n        "source_spec.extra.app_server_thread_processor",\n        "source_spec.extra.app_server_tui_session",\n        "source_spec.extra.local_storage_tui_backtrack",\n        "source_spec.extra.local_storage_paginated_fork",\n        "source_spec.extra.local_storage_revert_thread",\n    }',
    "test registry expected",
)
append = '''\n\ndef test_history_mutation_distinguishes_fork_and_revert_identity() -> None:\n    result = AppServerRpcExtractor().extract(_snapshot(), DiagnosticCollector())\n    mutation = result.data["history_mutation"]\n    assert mutation["fork"]["logical_identity"] == "new thread_id"\n    assert mutation["fork"]["paginated_source"]["preparation"].startswith("thread_store.prepare_fork")\n    assert mutation["revert"]["logical_identity"] == "preserved thread_id"\n    assert mutation["revert"]["storage_bridge"]["new_thread_row"] is False\n    assert mutation["revert"]["notification"] == "thread/reverted"\n\n\ndef test_tui_prompt_edit_is_source_preserving_fork_not_revert() -> None:\n    result = AppServerRpcExtractor().extract(_snapshot(), DiagnosticCollector())\n    policy = result.data["history_mutation"]["tui_prompt_edit_policy"]\n    assert policy == {\n        "operation": "thread/fork",\n        "boundary": "beforeTurnId for the selected initial prompt's turn",\n        "source_preserving": True,\n        "steer_is_independent_branch_boundary": False,\n        "uses_thread_revert": False,\n    }\n\n\ndef test_history_mutation_source_drift_fails_closed() -> None:\n    snapshot = _snapshot()\n    source = snapshot.files["source_spec.extra.app_server_thread_processor"]\n    broken = source.text.replace("wait_for_thread_shutdown(&thread).await", "skip_shutdown")\n    files = dict(snapshot.files)\n    files[source.spec_id] = _file(source.spec_id, "app_server_thread_processor", source.selected_path, broken)\n    drifted = SourceSnapshot(snapshot.revision, files)\n    diagnostics = DiagnosticCollector()\n    result = AppServerRpcExtractor().extract(drifted, diagnostics)\n    assert result.semantic_complete is False\n    assert "APP_SERVER_REVERT_SHUTDOWN_MISSING" in {item.code for item in diagnostics.values()}\n\n\ndef test_history_mutation_source_identity_is_exact() -> None:\n    registry = build_registry(load_legacy_modules())\n    assert registry.get("source_spec.extra.app_server_thread_processor").primary_path == "codex-rs/app-server/src/request_processors/thread_processor.rs"\n    assert registry.get("source_spec.extra.app_server_tui_session").primary_path == "codex-rs/tui/src/app_server_session.rs"\n'''
text += append
tests.write_text(text)

print("history mutation contract source edits prepared")
