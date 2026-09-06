from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess

from jsonschema import Draft202012Validator

from codex_wire_audit.history_cli import main as history_main
from codex_wire_audit.metadata_history import (
    catalog_payload_sha256,
    diff_versions,
    history_key_view,
    load_history_catalog,
    validate_history_catalog,
)
from codex_wire_audit.metadata_history_container import (
    verify_multi_json_container,
    write_multi_json_container,
)
from codex_wire_audit.metadata_history_probe import probe_metadata_history
from codex_wire_audit.proof_gate import build_attestation
from codex_wire_audit.proof_schema_validation import load_schemas, verify_attestation


CURRENT_RESPONSES_METADATA = r'''
pub(crate) const LEGACY_CODE_MODE_TOOL_NAMES_KEY: &str = "code_mode_tool_names";
pub(crate) const TOOL_NAMESPACES_INFO_KEY: &str = "tool_namespaces_info";
pub(crate) const WINDOW_NUMBER_KEY: &str = "window_number";
pub(crate) const FORKED_FROM_ORDINAL_EXCLUSIVE_KEY: &str = "forked_from_ordinal_exclusive";
const RESERVED_METADATA_KEYS: &[&str] = &[
    LEGACY_CODE_MODE_TOOL_NAMES_KEY,
    TOOL_NAMESPACES_INFO_KEY,
    WINDOW_NUMBER_KEY,
    FORKED_FROM_ORDINAL_EXCLUSIVE_KEY,
];
const BACKWARD_COMPATIBLE_RESERVED_METADATA_KEYS: &[&str] = &[
    WINDOW_NUMBER_KEY,
    FORKED_FROM_ORDINAL_EXCLUSIVE_KEY,
];
pub(crate) struct TurnToolFunctionInfo {
    pub(crate) name: String,
    pub(crate) code_mode_name: Option<String>,
}
struct CodexTurnMetadataPayload<'a> {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    window_number: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    forked_from_ordinal_exclusive: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    tool_namespaces_info: Option<&'a BTreeMap<String, String>>,
    #[serde(flatten)]
    extra: &'a BTreeMap<String, String>,
}
impl CodexResponsesMetadata {
    fn client_metadata(&self) {
        let mut client_metadata = HashMap::new();
        client_metadata.insert(X_CODEX_TURN_METADATA_HEADER.to_string(), self.turn_metadata_json());
    }
    fn compatibility_headers(&self) {
        to_ascii_json_string(&CodexTurnMetadataPayload {
            tool_namespaces_info: None,
            ..self.turn_metadata_payload()
        });
    }
    fn turn_metadata_payload(&self) -> CodexTurnMetadataPayload<'_> {
        let has_request_identity = true;
        CodexTurnMetadataPayload {
            window_number: has_request_identity.then_some(self.window_number).flatten(),
            forked_from_ordinal_exclusive: self.forked_from_ordinal_exclusive,
            tool_namespaces_info: self.tool_namespaces_info.as_ref(),
            extra: &self.extra,
        }
    }
}
'''


def _git(repo: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process.stdout.strip()


def _write_current_source_repo(root: Path, responses: str = CURRENT_RESPONSES_METADATA) -> str:
    files = {
        "codex-rs/core/src/responses_metadata.rs": responses,
        "codex-rs/core/src/turn_metadata.rs": r'''
impl TurnMetadataState {
    fn current_meta_value_for_mcp_request(&self) {
        let mut responses_metadata = self.mcp_metadata_template();
        responses_metadata.tool_namespaces_info = None;
        let metadata = responses_metadata.turn_metadata_value();
    }
}
''',
        "codex-rs/core/src/tools/tool_namespaces_info.rs": r'''
fn collect_tool_namespaces_info(code_mode_tool_names: &BTreeMap<String, ToolName>) {
    let code_mode_names_by_tool = code_mode_tool_names.iter();
    TurnToolFunctionInfo {
        name: "lookup".to_string(),
        code_mode_name: Some("mcp__calendar__lookup".to_string()),
    };
}
''',
        "codex-rs/core/src/tools/spec_plan.rs": (
            "fn plan(config: &Config) { if config.tool_registry."
            "turn_metadata_includes_tool_info {} }"
        ),
        "codex-rs/core/src/session/mod.rs": (
            "fn session(config: &Config) { if config.tool_registry."
            "turn_metadata_includes_tool_info {} }"
        ),
        "codex-rs/core/src/tools/router.rs": (
            "struct ToolRouter { code_mode_tool_names: BTreeMap<String, ToolName> }"
        ),
        "codex-rs/features/src/feature_configs.rs": r'''
pub struct ToolRegistryConfig {
    pub turn_metadata_includes_tool_info: Option<bool>,
}
''',
        "codex-rs/core/src/config/mod.rs": r'''
pub struct ToolRegistryConfig {
    pub turn_metadata_includes_tool_info: bool,
}
fn load(cfg: &Features) -> ToolRegistryConfig {
    ToolRegistryConfig {
        turn_metadata_includes_tool_info:
            cfg.tool_registry.turn_metadata_includes_tool_info.unwrap_or_default(),
    }
}
''',
        "codex-rs/core/config.schema.json": json.dumps(
            {
                "properties": {
                    "turn_metadata_includes_tool_info": {"type": "boolean"}
                }
            }
        ),
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "proof@example.invalid")
    _git(root, "config", "user.name", "Proof")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "synthetic current metadata state")
    return _git(root, "rev-parse", "HEAD")


def test_catalog_is_strict_and_tracks_multiple_versions() -> None:
    catalog = load_history_catalog()
    assert validate_history_catalog(catalog) == []
    assert catalog["format"] == "codex-wire-audit-metadata-key-history/v1"
    assert len(catalog["versions"]) == 10
    assert len(catalog["keys"]) == 7
    assert catalog["integrity"]["canonical_payload_sha256"] == catalog_payload_sha256(catalog)


def test_code_mode_history_models_active_bounded_successor_and_removed_states() -> None:
    view = history_key_view(load_history_catalog(), "code_mode_tool_names")
    states = [item["state_ref"] for item in view["timeline"]]
    assert "state.code_mode.active_unbounded_header" in states
    assert "state.code_mode.active_bounded_header" in states
    assert "state.code_mode.active_with_successor" in states
    assert states[-1] == "state.code_mode.removed_reserved"
    assert view["key"]["replacement_key_refs"] == [
        "key.turn_metadata.tool_namespaces_info"
    ]


def test_configuration_aliases_are_preserved_as_three_distinct_keys() -> None:
    catalog = load_history_catalog()
    names = {
        catalog["keys"][key]["wire_name"]
        for key in catalog["keys"]
        if key.startswith("key.config.tool_registry.")
    }
    assert names == {
        "include_tool_namespaces_info",
        "include_tool_metadata",
        "turn_metadata_includes_tool_info",
    }


def test_history_diff_ids_are_stable_and_semantic() -> None:
    catalog = load_history_catalog()
    baseline = "version.2026-07-25.code_mode_metadata_introduced"
    candidate = "version.2026-08-07.code_mode_metadata_removed"
    first = diff_versions(catalog, baseline, candidate)
    second = diff_versions(catalog, baseline, candidate)
    assert first == second
    assert len({item["id"] for item in first["changes"]}) == first["change_count"]
    assert any(
        item["key_ref"] == "key.turn_metadata.code_mode_tool_names"
        for item in first["changes"]
    )


def test_multi_json_container_is_deterministic_and_closed(tmp_path: Path) -> None:
    catalog = load_history_catalog()
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_multi_json_container(catalog, first)
    write_multi_json_container(catalog, second)
    assert verify_multi_json_container(first) == []
    assert verify_multi_json_container(second) == []
    left = {
        path.relative_to(first).as_posix(): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    right = {
        path.relative_to(second).as_posix(): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert left == right
    assert len(list((first / "versions").glob("*.json"))) == 10
    assert len(list((first / "keys").glob("*.json"))) == 7

    victim = next((first / "keys").glob("*.json"))
    victim.write_text(victim.read_text(encoding="ascii") + " ", encoding="ascii")
    assert any(
        item.code == "METADATA_HISTORY_CONTAINER_MEMBER_HASH_MISMATCH"
        for item in verify_multi_json_container(first)
    )


def test_current_source_probe_matches_the_forward_canary(tmp_path: Path) -> None:
    commit = _write_current_source_repo(tmp_path)
    result = probe_metadata_history(load_history_catalog(), tmp_path)
    assert result.complete
    assert result.forward_canary
    assert result.observed_commit_sha == commit
    assert all(item["complete"] for item in result.key_results.values())
    assert {item.code for item in result.diagnostics} == {
        "METADATA_HISTORY_FORWARD_CANARY"
    }

    by_id, _ = load_schemas()
    schema = by_id[
        "https://openai.example/codex-wire-audit/metadata-history-probe-v1.schema.json"
    ]
    assert list(Draft202012Validator(schema).iter_errors(result.to_dict())) == []


def test_probe_fails_when_removed_key_is_no_longer_reserved(tmp_path: Path) -> None:
    changed = CURRENT_RESPONSES_METADATA.replace(
        "    LEGACY_CODE_MODE_TOOL_NAMES_KEY,\n", ""
    )
    _write_current_source_repo(tmp_path, changed)
    result = probe_metadata_history(load_history_catalog(), tmp_path)
    assert not result.complete
    assert any(
        item.code == "METADATA_HISTORY_ASSERTION_MISMATCH"
        and item.source_id == "key.turn_metadata.code_mode_tool_names"
        for item in result.diagnostics
    )


def test_probe_fails_when_removed_key_reappears_in_payload(tmp_path: Path) -> None:
    changed = CURRENT_RESPONSES_METADATA.replace(
        "struct CodexTurnMetadataPayload<'a> {",
        "struct CodexTurnMetadataPayload<'a> {\n"
        "    code_mode_tool_names: Option<&'a BTreeMap<String, String>>,",
    ).replace(
        "CodexTurnMetadataPayload {\n            window_number:",
        "CodexTurnMetadataPayload {\n            code_mode_tool_names: self.code_mode_tool_names.as_ref(),\n            window_number:",
    )
    _write_current_source_repo(tmp_path, changed)
    result = probe_metadata_history(load_history_catalog(), tmp_path)
    assert not result.complete
    comparison = result.key_results[
        "key.turn_metadata.code_mode_tool_names"
    ]["assertions"]["payload_container"]
    assert comparison["expected"] == "absent"
    assert comparison["observed"] == "emitted_conditionally"


def test_probe_fails_when_successor_mapping_disappears(tmp_path: Path) -> None:
    _write_current_source_repo(tmp_path)
    mapping = tmp_path / "codex-rs/core/src/tools/tool_namespaces_info.rs"
    mapping.write_text("fn collect_tool_namespaces_info() {}", encoding="utf-8")
    result = probe_metadata_history(load_history_catalog(), tmp_path)
    assert not result.complete
    assert any(
        item.source_id in {
            "key.turn_metadata.code_mode_tool_names",
            "key.turn_metadata.tool_namespaces_info",
        }
        and item.details
        and item.details.get("expected") is True
        and item.details.get("observed") is False
        for item in result.diagnostics
    )


def test_proof_attestation_v2_includes_history_dimension(
    tmp_path: Path, monkeypatch
) -> None:
    _write_current_source_repo(tmp_path)
    import codex_wire_audit.proof_gate as gate

    monkeypatch.setattr(gate, "validate_report", lambda report, schema_dir=None: [])
    report = {"extractors": {}}
    attestation = build_attestation(
        report,
        profile_id="legacy_metadata_history",
        schema_dir=None,
        source_root=tmp_path,
        requested_ref="HEAD",
        source_mode="worktree",
        allow_dirty_source=False,
    )
    assert attestation["format"] == "codex-wire-audit-proof-attestation/v2"
    assert attestation["metadata_history"]["complete"]
    assert attestation["dimensions"]["metadata_history"]["complete"]
    assert attestation["status"]["complete"]
    assert verify_attestation(attestation) == []


def test_history_cli_writes_container_and_stable_errors(tmp_path: Path, capsys) -> None:
    output = tmp_path / "summary.json"
    container = tmp_path / "container"
    archive = tmp_path / "container.zip"
    code = history_main(
        [
            "--list-keys",
            "--output",
            str(output),
            "--output-dir",
            str(container),
            "--zip",
            str(archive),
        ]
    )
    assert code == 0
    assert output.is_file() and archive.is_file()
    assert verify_multi_json_container(container) == []
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == json.loads(
        output.read_text(encoding="ascii")
    )["container"]["zip_sha256"]

    verification = tmp_path / "verification.json"
    assert history_main(
        ["--verify-container", str(container), "--output", str(verification)]
    ) == 0
    assert json.loads(verification.read_text(encoding="ascii"))["complete"] is True

    code = history_main(["--key", "definitely-not-a-key"])
    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.err
