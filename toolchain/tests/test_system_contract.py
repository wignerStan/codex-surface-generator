from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema import Draft202012Validator

from codex_wire_audit.canonical import CanonicalizationError, canonical_json_bytes
from codex_wire_audit.diagnostics import DiagnosticCollector
from codex_wire_audit.evolution import build_evolution_contract, finalize_system_report
from codex_wire_audit.models import SourceRevision, SourceSnapshot
from codex_wire_audit.source_registry import SourceRegistry, from_legacy_maps
from codex_wire_audit.legacy import load_legacy_modules
from codex_wire_audit.system_contract import build_system_contract, compatibility_views, digest
from codex_wire_audit.system_contract_evidence import load_document, relative_source_path
from codex_wire_audit.system_contract_validation import validate_system_contract
from test_codex_wire_audit_v11 import contract_for_fixture, make_snapshot, FIXTURES
from test_local_storage_v19 import _snapshot as storage_snapshot
from test_config_surface_v19 import _snapshot as config_snapshot
from tools.check_canonical_contract import expected_assets

ROOT = Path(__file__).resolve().parents[1]


def fixture_contract():
    return build_system_contract(contract_for_fixture("responses_metadata_identity_split.rs"))


def reseal_model(model):
    model["integrity"]["canonical_ir_sha256"] = digest({k: v for k, v in model.items() if k != "integrity"})
    return build_system_contract(model)


def combined_model():
    legacy = load_legacy_modules()
    registry = from_legacy_maps(legacy.base.FILES, legacy.base.SURFACE_FILES, legacy.entrypoint.EXTRA)
    _, metadata = make_snapshot((FIXTURES / "responses_metadata_identity_split.rs").read_text())
    files = {**config_snapshot().files, **storage_snapshot().files, **metadata.files}
    registry = SourceRegistry([
        replace(spec, path_candidates=(files[spec.id].selected_path,)) if spec.id in files else spec
        for spec in registry.specs
    ])
    revision = SourceRevision("fixture", "openai/codex", "fixture", None, SourceSnapshot.digest_files(files), None)
    snapshot = SourceSnapshot(revision, files, {spec.id: "not in fixture" for spec in registry.specs if spec.id not in files})
    model, _ = build_evolution_contract(snapshot, registry, DiagnosticCollector(), coverage_profile="hybrid_v19")
    return model, snapshot


def test_canonical_contract_is_real_source_ir_not_a_narrative_catalog():
    contract = fixture_contract()
    result = validate_system_contract(contract)
    assert result["authority"] == "migrated_semantic_extractors"
    assert result["legacy_machine_reconstruction_remaining"] is True
    assert result["source_bytes_status"] == "not_checked"
    assert contract["model"]["extractors"]["extractor.turn_metadata"]["data"]["fields"]
    assert contract["model"]["source_revision"]["source_mode"] == "fixture"
    assert contract["model"]["status"]["runtime_observation"] == "not_run"


def test_projection_payloads_are_deterministic_and_not_aliases():
    contract = fixture_contract()
    assert canonical_json_bytes(contract) == canonical_json_bytes(dict(reversed(list(contract.items()))))
    views = compatibility_views(contract["model"])
    assert digest(views["/evolution_contract"]) == contract["compatibility_views"]["/evolution_contract"]
    views["/evolution_contract"]["generator_version"] = "changed"
    assert contract["model"]["generator_version"] != "changed"


def test_combined_config_storage_projection_and_references():
    model, _ = combined_model()
    report = {"evolution_contract": model, "config_protocol": {"legacy": "retained"}}
    finalize_system_report(report)
    result = validate_system_contract(report["system_contract"], report=report)
    assert result["compatibility_views_checked"] >= 4
    assert report["local_storage_schema"]["authoritative"] is False
    assert report["config_schema"]["authoritative"] is False
    assert report["config_protocol"]["legacy"] == "retained"
    report["local_storage_schema"]["roots"]["codex_home"]["default"] = "/wrong"
    with pytest.raises(ValueError, match="compatibility view differs"):
        validate_system_contract(report["system_contract"], report=report)


@pytest.mark.parametrize("mutation,match", [
    (lambda c: c.update(schema="codex-system-contract/v2"), "schema violation"),
    (lambda c: c.update(extra="unexpected"), "schema violation"),
    (lambda c: c["integrity"].update(canonical_sha256="0" * 64), "content digest mismatch"),
    (lambda c: c["compatibility_views"].clear(), "schema violation"),
    (lambda c: c["compatibility_views"].update({"/unexpected": "0" * 64}), "projection digest mismatch"),
])
def test_tampering_is_rejected(mutation, match):
    contract = fixture_contract()
    mutation(contract)
    with pytest.raises(ValueError, match=match):
        validate_system_contract(contract)


@pytest.mark.parametrize("mutation,match", [
    (lambda m: m["migration"]["canonical_ir_extractors"].clear(), "coverage mismatch"),
    (lambda m: m["extractors"]["extractor.turn_metadata"].update(extractor_id="extractor.other"), "schema violation|key/id mismatch"),
    (lambda m: m["extractors"]["extractor.turn_metadata"]["source_spec_ids"].append("source_spec.unknown"), "undeclared source"),
    (lambda m: m["source_snapshot"]["counts"].update(available=99), "count mismatch"),
    (lambda m: m["source_snapshot"]["revision"].update(requested_ref="different"), "revision disagreement"),
])
def test_rehashing_cannot_hide_reference_or_coverage_errors(mutation, match):
    model = fixture_contract()["model"]
    mutation(model)
    with pytest.raises(ValueError, match=match):
        validate_system_contract(reseal_model(model))


def test_new_extractor_is_not_dropped_by_a_hard_coded_surface_catalog():
    model = fixture_contract()["model"]
    model["extractors"]["extractor.future"] = {
        "extractor_id": "extractor.future", "schema_version": "1.0.0",
        "source_spec_ids": ["source_spec.base.metadata"], "semantic_complete": True,
        "data": {"new_fact": "preserved"},
    }
    model["migration"]["canonical_ir_extractors"].append("extractor.future")
    contract = reseal_model(model)
    assert "extractor.future" in validate_system_contract(contract)["extractors"]


def test_empty_extraction_does_not_pass_vacuously():
    registry, snapshot = make_snapshot("")
    snapshot.files.clear()
    model, _ = build_evolution_contract(snapshot, registry, DiagnosticCollector(), coverage_profile="fixture")
    assert model["status"]["overall"] == "partial"
    assert model["status"]["syntax_extraction"] == "partial"


def test_source_bytes_revalidated_and_changed_bytes_rejected(tmp_path):
    source = (FIXTURES / "responses_metadata_identity_split.rs").read_text()
    registry, snapshot = make_snapshot(source)
    model, _ = build_evolution_contract(snapshot, registry, DiagnosticCollector(), coverage_profile="fixture")
    contract = build_system_contract(model)
    record = next(iter(snapshot.files.values()))
    path = tmp_path / record.selected_path
    path.parent.mkdir(parents=True)
    path.write_bytes(record.raw_bytes)
    assert validate_system_contract(contract, source_root=tmp_path)["source_files_revalidated"] == 1
    path.write_bytes(record.raw_bytes + b"\n")
    with pytest.raises(ValueError, match="byte mismatch"):
        validate_system_contract(contract, source_root=tmp_path)


@pytest.mark.parametrize("path", ["/tmp/evidence", "../secret", ".git/config", "a/../b", "a/.git/config", "C:/secret", "a\\b", "./a", ".", "a\x00b"])
def test_evidence_paths_are_confined(path):
    with pytest.raises(ValueError, match="repository-relative"):
        relative_source_path(path)


def test_symlink_evidence_is_rejected(tmp_path):
    contract = fixture_contract()
    record = next(iter(contract["model"]["source_snapshot"]["files"].values()))
    path = tmp_path / record["path"]
    path.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("secret")
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        validate_system_contract(contract, source_root=tmp_path)


@pytest.mark.parametrize("place", ["key", "value"])
def test_secret_rejection_includes_keys_without_echoing_tokens(place):
    contract = fixture_contract()
    token = "ghp_" + "a" * 36
    contract[token if place == "key" else "note"] = "redacted" if place == "key" else token
    with pytest.raises(ValueError, match="secret-like") as error:
        validate_system_contract(contract)
    assert token not in str(error.value)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"é":1,"e\\u0301":2}', '{"x":NaN}'])
def test_ambiguous_json_is_rejected(tmp_path, raw):
    path = tmp_path / "contract.json"
    path.write_text(raw)
    with pytest.raises(ValueError):
        load_document(path)


@pytest.mark.parametrize("value", [{1: "lost", "1": "kept"}, {None: 3}, "\ud800", ("e\u0301",)])
def test_canonicalization_rejects_information_loss(value):
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(value)


def test_standalone_schema_bundle_validates_offline_without_package_registry():
    assets = expected_assets()
    schema = json.loads(assets["schema.json"])
    contract = json.loads(assets["example.fixture.json"])
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract)


def test_cli_exposes_canonical_export_and_checker_rejects_bad_contract(tmp_path):
    from codex_wire_audit.cli import build_parser
    assert build_parser().parse_args(["--emit-system-contract", "out.json"]).emit_system_contract == "out.json"
    path = tmp_path / "contract.json"
    path.write_text("{}")
    result = subprocess.run([sys.executable, "tools/check_canonical_contract.py", "--contract", str(path)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2
    assert '"status": "failed"' in result.stdout


def test_installed_capability_contract_includes_canonical_runtime():
    from codex_wire_audit.release_contract import probe_active_package
    result = probe_active_package()
    claim = next(c for c in result["capability_claims"] if c["id"] == "canonical_system_contract")
    assert claim["status"] == "passed"
    assert all(resource["present"] for resource in claim["resources"])
    assert "schema_templates/system-contract.schema.json" in {r["path"] for r in claim["resources"]}
