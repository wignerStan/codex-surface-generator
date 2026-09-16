from __future__ import annotations

import json
from pathlib import Path

ROOT = Path.cwd()
PKG = ROOT / "toolchain" / "codex_wire_audit"
TESTS = ROOT / "toolchain" / "tests"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one occurrence, found {count}")
    return text.replace(old, new, 1)


helper = PKG / "extractors" / "app_server_history_mutation.py"
text = helper.read_text()
text = replace_once(
    text,
    'APP_SERVER_THREAD_PROCESSOR = "source_spec.extra.app_server_thread_processor"\n',
    'APP_SERVER_THREAD_PROCESSOR = "source_spec.extra.app_server_thread_processor"\nTHREAD_MANAGER = "source_spec.extra.app_server_thread_manager"\n',
    "thread manager constant",
)
text = replace_once(
    text,
    '    processor: SourceFile,\n    tui_session: SourceFile,',
    '    processor: SourceFile,\n    thread_manager: SourceFile,\n    tui_session: SourceFile,',
    "validator thread manager arg",
)
text = replace_once(
    text,
    '            ("APP_SERVER_FORK_NEW_THREAD_ROW_MISSING", \'"thread/fork"\'),\n',
    '            ("APP_SERVER_FORK_STARTED_NOTIFICATION_MISSING", "ServerNotification::ThreadStarted(notif)"),\n',
    "fork notification marker",
)
text = replace_once(
    text,
    '            ("APP_SERVER_REVERT_SUBSCRIPTION_PRESERVE_MISSING", "Keep thread state and subscriptions across the internal reload"),\n',
    '            ("APP_SERVER_REVERT_SUBSCRIPTION_PRESERVE_MISSING", "Keep thread state and subscriptions across the internal reload"),\n            ("APP_SERVER_REVERT_NOTIFICATION_MISSING", "ServerNotification::ThreadReverted("),\n',
    "revert notification marker",
)
anchor = '''    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=tui_session,
'''
block = '''    complete &= _require(
        diagnostics,
        extractor_id=extractor_id,
        source=thread_manager,
        entity="app_server_rpc.history_mutation.fork_identity",
        tokens=(
            ("APP_SERVER_FORK_FRESH_ID_MISSING", "The new thread will have"),
            ("APP_SERVER_FORK_FRESH_ID_SUFFIX_MISSING", "a fresh id."),
            ("APP_SERVER_PREPARED_FORK_ENTRY_MISSING", "pub async fn fork_prepared_thread"),
            ("APP_SERVER_PREPARED_FORK_SOURCE_ID_MISSING", "conversation_id: prepared.source_thread_id"),
            ("APP_SERVER_FORK_RELATION_MISSING", "request.forked_from_thread_id = source_thread_id;"),
        ),
    )
'''
text = replace_once(text, anchor, block + anchor, "thread manager validation")
text = replace_once(
    text,
    '                "replacement_filename_semantics": "A_R: A is stable thread_id and R is replacement rollout_id",\n',
    '                "replacement_identity": "stable logical thread_id plus new rollout_id",\n                "physical_layout_owner": "extractor.local_storage",\n',
    "storage ownership boundary",
)
text = replace_once(
    text,
    '            "revert_signature": "same thread.id; replacement A_R rollout path; thread/reverted; existing thread row changes rollout_path",\n',
    '            "revert_signature": "same thread.id; thread/reverted; storage-selected replacement whose parsed logical thread_id remains A while rollout_id changes",\n',
    "desktop revert classifier",
)
helper.write_text(text)

rpc = PKG / "extractors" / "app_server_rpc.py"
text = rpc.read_text()
text = replace_once(
    text,
    '    STORAGE_REVERT,\n    TUI_APP_SERVER_SESSION,',
    '    STORAGE_REVERT,\n    THREAD_MANAGER,\n    TUI_APP_SERVER_SESSION,',
    "rpc import thread manager",
)
text = replace_once(
    text,
    '    "processor": APP_SERVER_THREAD_PROCESSOR,\n    "tui_session": TUI_APP_SERVER_SESSION,',
    '    "processor": APP_SERVER_THREAD_PROCESSOR,\n    "thread_manager": THREAD_MANAGER,\n    "tui_session": TUI_APP_SERVER_SESSION,',
    "rpc source ids thread manager",
)
text = replace_once(
    text,
    '    processor: SourceFile,\n    tui_session: SourceFile,',
    '    processor: SourceFile,\n    thread_manager: SourceFile,\n    tui_session: SourceFile,',
    "body thread manager arg",
)
text = replace_once(
    text,
    '            "processor": _evidence(processor, "thread_fork_inner/thread_revert_response/reload_paginated_thread"),\n',
    '            "processor": _evidence(processor, "thread_fork_inner/thread_revert_response/reload_paginated_thread"),\n            "thread_manager": _evidence(thread_manager, "fork_prepared_thread/fork_thread_with_initial_history"),\n',
    "thread manager evidence",
)
text = replace_once(
    text,
    '        processor = sources["processor"]\n        tui_session = sources["tui_session"]\n',
    '        processor = sources["processor"]\n        thread_manager = sources["thread_manager"]\n        tui_session = sources["tui_session"]\n',
    "source binding thread manager",
)
text = replace_once(
    text,
    '        assert common and thread and thread_data and turn and item and rpc and processor and tui_session and tui_backtrack and storage_fork and storage_revert\n',
    '        assert common and thread and thread_data and turn and item and rpc and processor and thread_manager and tui_session and tui_backtrack and storage_fork and storage_revert\n',
    "assert thread manager",
)
text = replace_once(
    text,
    '                ("APP_SERVER_THREAD_REVERT_PARAMS_MISSING", "pub struct ThreadRevertParams"),\n',
    '                ("APP_SERVER_THREAD_REVERT_PARAMS_MISSING", "pub struct ThreadRevertParams"),\n                ("APP_SERVER_THREAD_REVERT_FILES_BOUNDARY_MISSING", "This only changes persisted conversation history. It does not revert local file changes."),\n',
    "protocol revert file boundary",
)
text = replace_once(
    text,
    '            processor=processor,\n            tui_session=tui_session,\n',
    '            processor=processor,\n            thread_manager=thread_manager,\n            tui_session=tui_session,\n',
    "validator thread manager call",
)
text = replace_once(
    text,
    '            processor=processor,\n            tui_session=tui_session,\n            tui_backtrack=tui_backtrack,\n            storage_fork=storage_fork,\n            storage_revert=storage_revert,\n            history_mutation=history_mutation,\n',
    '            processor=processor,\n            thread_manager=thread_manager,\n            tui_session=tui_session,\n            tui_backtrack=tui_backtrack,\n            storage_fork=storage_fork,\n            storage_revert=storage_revert,\n            history_mutation=history_mutation,\n',
    "body builder thread manager call",
)
rpc.write_text(text)

registry = PKG / "source_registry.py"
text = registry.read_text()
text = replace_once(
    text,
    '    ("source_spec.extra.app_server_thread_processor", "app_server_thread_processor", "codex-rs/app-server/src/request_processors/thread_processor.rs", ("thread_fork_inner", "thread_revert_response", "reload_paginated_thread", "prepare_fork", "revert_thread")),\n',
    '    ("source_spec.extra.app_server_thread_processor", "app_server_thread_processor", "codex-rs/app-server/src/request_processors/thread_processor.rs", ("thread_fork_inner", "thread_revert_response", "reload_paginated_thread", "prepare_fork", "revert_thread", "ServerNotification::ThreadStarted", "ServerNotification::ThreadReverted")),\n    ("source_spec.extra.app_server_thread_manager", "app_server_thread_manager", "codex-rs/core/src/thread_manager.rs", ("fork_prepared_thread", "fork_thread_with_initial_history", "a fresh id", "forked_from_thread_id")),\n',
    "registry thread manager source",
)
registry.write_text(text)

schema_path = PKG / "proof_schema_templates" / "app-server-rpc-lifecycle-v2.schema.json"
schema = json.loads(schema_path.read_text())
schema["properties"]["evidence"]["minProperties"] = 12
bridge = schema["properties"]["history_mutation"]["properties"]["revert"]["properties"]["storage_bridge"]
bridge.clear()
bridge.update({
    "type": "object",
    "additionalProperties": False,
    "required": [
        "replacement_rollout_id",
        "logical_thread_id",
        "replacement_identity",
        "physical_layout_owner",
        "state_cutover",
        "new_thread_row",
    ],
    "properties": {
        "replacement_rollout_id": {"const": "new UUID"},
        "logical_thread_id": {"const": "preserved"},
        "replacement_identity": {"const": "stable logical thread_id plus new rollout_id"},
        "physical_layout_owner": {"const": "extractor.local_storage"},
        "state_cutover": {"type": "string", "minLength": 1},
        "new_thread_row": {"const": False},
    },
})
schema_path.write_text(json.dumps(schema, indent=2) + "\n")

test_path = TESTS / "test_app_server_rpc_extractor.py"
text = test_path.read_text()
text = replace_once(
    text,
    'pub struct ThreadRevertParams { pub thread_id: String, pub before_turn_id: String }\n',
    '// This only changes persisted conversation history. It does not revert local file changes.\npub struct ThreadRevertParams { pub thread_id: String, pub before_turn_id: String }\n',
    "test protocol file boundary",
)
text = replace_once(
    text,
    '    // Keep thread state and subscriptions across the internal reload.\n    self.thread_store.revert_thread',
    '    // Keep thread state and subscriptions across the internal reload.\n    ServerNotification::ThreadStarted(notif);\n    ServerNotification::ThreadReverted(ThreadRevertedNotification { thread_id });\n    self.thread_store.revert_thread',
    "test notification markers",
)
thread_manager_fixture = '''    thread_manager = """
/// Fork an existing thread by snapshotting rollout history. The new thread will have
/// a fresh id.
pub async fn fork_prepared_thread() {
    let history = InitialHistory::Resumed(ResumedHistory {
        conversation_id: prepared.source_thread_id,
    });
}
async fn fork_thread_with_initial_history() {
    request.forked_from_thread_id = source_thread_id;
}
"""
'''
text = replace_once(text, '    tui_session = """\n', thread_manager_fixture + '    tui_session = """\n', "test thread manager fixture")
text = replace_once(
    text,
    '        "processor": _file("source_spec.extra.app_server_thread_processor", "app_server_thread_processor", "codex-rs/app-server/src/request_processors/thread_processor.rs", processor),\n',
    '        "processor": _file("source_spec.extra.app_server_thread_processor", "app_server_thread_processor", "codex-rs/app-server/src/request_processors/thread_processor.rs", processor),\n        "thread_manager": _file("source_spec.extra.app_server_thread_manager", "app_server_thread_manager", "codex-rs/core/src/thread_manager.rs", thread_manager),\n',
    "test thread manager source row",
)
text = replace_once(
    text,
    '        "source_spec.extra.app_server_thread_processor",\n        "source_spec.extra.app_server_tui_session",\n',
    '        "source_spec.extra.app_server_thread_processor",\n        "source_spec.extra.app_server_thread_manager",\n        "source_spec.extra.app_server_tui_session",\n',
    "test expected source set",
)
text = replace_once(
    text,
    '    assert mutation["revert"]["storage_bridge"]["new_thread_row"] is False\n',
    '    assert mutation["revert"]["storage_bridge"]["new_thread_row"] is False\n    assert mutation["revert"]["storage_bridge"]["physical_layout_owner"] == "extractor.local_storage"\n    assert mutation["revert"]["storage_bridge"]["replacement_identity"] == "stable logical thread_id plus new rollout_id"\n',
    "test storage ownership assertions",
)
text = replace_once(
    text,
    '    assert registry.get("source_spec.extra.app_server_tui_session").primary_path == "codex-rs/tui/src/app_server_session.rs"\n',
    '    assert registry.get("source_spec.extra.app_server_tui_session").primary_path == "codex-rs/tui/src/app_server_session.rs"\n    assert registry.get("source_spec.extra.app_server_thread_manager").primary_path == "codex-rs/core/src/thread_manager.rs"\n',
    "test thread manager identity",
)
test_path.write_text(text)

print("history mutation proof hardened")
