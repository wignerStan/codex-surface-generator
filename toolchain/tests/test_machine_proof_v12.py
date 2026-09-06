from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from codex_wire_audit.proof_diagnostics import DiagnosticBag, ProofDiagnostic
from codex_wire_audit.proof_gate import _self_test, main
from codex_wire_audit.proof_profiles import evaluate_profile
from codex_wire_audit.proof_provenance import verify_git_source
from codex_wire_audit.rust_semantics import analyze_turn_metadata_source


def test_self_test() -> None:
    assert _self_test() == 0


def test_profile_names_are_executable_requirements() -> None:
    report = {"extractors": {"extractor.turn_metadata": {"id": "extractor.turn_metadata"}}}
    fixture, _ = evaluate_profile(report, "fixture")
    full, diagnostics = evaluate_profile(report, "codex_wire_full")
    assert fixture["complete"]
    assert not full["complete"]
    assert "extractor.responses_request" in full["missing_extractors"]
    assert any(item.code == "REQUIRED_EXTRACTOR_MISSING" for item in diagnostics)


def test_spread_and_macro_never_silently_complete() -> None:
    spread = analyze_turn_metadata_source("fn x(){ CodexTurnMetadataPayload { turn_id: self.turn_id, ..self.base() } }")
    macro = analyze_turn_metadata_source("fn x(){ CodexTurnMetadataPayload { payload_fields!() } }")
    assert not spread.complete
    assert not macro.complete
    assert {item.code for item in spread.diagnostics} >= {"STRUCT_SPREAD_UNRESOLVED"}
    assert {item.code for item in macro.diagnostics} >= {"STRUCT_MACRO_UNRESOLVED"}


def test_helper_body_changes_semantic_fingerprint() -> None:
    first = analyze_turn_metadata_source("impl K { fn pred(self)->bool{true} fn x(){ CodexTurnMetadataPayload { a: Some(K::pred) } } }")
    second = analyze_turn_metadata_source("impl K { fn pred(self)->bool{false} fn x(){ CodexTurnMetadataPayload { a: Some(K::pred) } } }")
    assert first.semantic_sha256 != second.semantic_sha256


def test_diagnostic_merge_is_monotonic_and_order_independent() -> None:
    error = ProofDiagnostic(code="X", message="error", severity="error", strict_failure=True, recoverable=False)
    warning = ProofDiagnostic(code="X", message="warning", severity="warning", strict_failure=False, recoverable=True)
    left = DiagnosticBag([error, warning]).values()[0]
    right = DiagnosticBag([warning, error]).values()[0]
    assert left == right
    assert left.severity == "error"
    assert left.strict_failure
    assert not left.recoverable


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_worktree_provenance_rejects_non_head_and_untracked(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "proof@example.invalid")
    _git(tmp_path, "config", "user.name", "Proof")
    tracked = tmp_path / "tracked.rs"
    tracked.write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "tracked.rs")
    _git(tmp_path, "commit", "-m", "one")
    old = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    tracked.write_text("two", encoding="utf-8")
    _git(tmp_path, "commit", "-am", "two")
    untracked = tmp_path / "untracked.rs"
    untracked.write_text("new", encoding="utf-8")
    result = verify_git_source(tmp_path, old, ["tracked.rs", "untracked.rs"], mode="worktree")
    codes = {item.code for item in result.diagnostics}
    assert "WORKTREE_REF_NOT_HEAD" in codes
    assert "LOADED_SOURCE_UNTRACKED" in codes
    assert not result.commit_binding_verified
    assert not result.complete


def test_gate_writes_attestation_before_incomplete_exit(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"extractors": {"extractor.turn_metadata": {"id": "extractor.turn_metadata"}}}), encoding="utf-8")
    output = tmp_path / "proof.json"
    code = main(["--report", str(report), "--attestation", str(output), "--profile", "codex_wire_full"])
    assert code == 3
    assert output.is_file()
    value = json.loads(output.read_text(encoding="utf-8"))
    assert value["status"]["overall"] == "incomplete"


def test_console_errors_are_stable_without_traceback(capsys) -> None:
    code = main(["--report", "/definitely/missing/report.json"])
    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.err



def test_strict_schema_validator_catches_missing_required_fields(tmp_path: Path) -> None:
    from codex_wire_audit.proof_schema_validation import validate_report
    schema_id = "https://example.invalid/test-contract.schema.json"
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "type": "object",
        "required": ["migration", "status", "diagnostic_summary"],
        "properties": {
            "schema_id": {"const": schema_id},
            "migration": {"type": "object"},
            "status": {"type": "object"},
            "diagnostic_summary": {"type": "object"},
        },
        "additionalProperties": False,
    }
    (tmp_path / "contract.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    diagnostics = validate_report({"schema_id": schema_id}, tmp_path)
    messages = " ".join(item.message for item in diagnostics)
    assert "migration" in messages
    assert "status" in messages
    assert "diagnostic_summary" in messages


def test_runtime_evidence_must_be_report_bound() -> None:
    from codex_wire_audit.proof_diagnostics import DiagnosticBag
    from codex_wire_audit.proof_gate import _runtime_result
    report = {"extractors": {}}
    evidence = {"status": "complete", "report_sha256": "0" * 64, "scenarios": []}
    bag = DiagnosticBag()
    result = _runtime_result(report, "codex_wire_full", evidence, bag)
    assert not result["complete"]
    codes = {item.code for item in bag.values()}
    assert "RUNTIME_EVIDENCE_REPORT_MISMATCH" in codes
    assert "RUNTIME_SCENARIO_MISSING" in codes



def test_generic_semantic_diff_has_stable_change_ids(monkeypatch) -> None:
    import codex_wire_audit.proof_diff as module
    monkeypatch.setattr(module, "validate_report", lambda report: [])
    baseline = {"coverage_profile": "fixture", "entities": {"field.a": {"id": "field.a", "value": 1}}}
    candidate = {"coverage_profile": "fixture", "entities": {"field.a": {"id": "field.a", "value": 2}, "field.b": {"id": "field.b", "value": 3}}}
    first = module.semantic_diff(baseline, candidate)
    second = module.semantic_diff(baseline, candidate)
    assert first["changes"] == second["changes"]
    assert {item["classification"] for item in first["changes"]} >= {"field_semantics_changed", "entity_added"}
    assert len({item["id"] for item in first["changes"]}) == len(first["changes"])
