from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from codex_wire_audit.system_contract import attach_system_contract, build_system_contract
from codex_wire_audit.system_contract_validation import validate_system_contract

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "contracts" / "codex-system-contract" / "v1" / "example.fixture.json"
FORBIDDEN = {"config_schema", "config_surface_graph", "evolution_contract", "local_storage_schema"}


def model() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["model"]


def test_contract_has_one_machine_representation() -> None:
    contract = build_system_contract(model())
    assert set(contract) == {"$schema", "schema", "authority", "model", "integrity"}
    assert "compatibility_views" not in contract
    result = validate_system_contract(contract)
    assert result["direct_report_checked"] is False


def test_attach_consumes_construction_model() -> None:
    report = {"evolution_contract": model(), "diagnostics": []}
    attach_system_contract(report)
    assert report.keys() == {"system_contract", "diagnostics"}
    assert not (FORBIDDEN & set(report))
    result = validate_system_contract(report["system_contract"], report=report)
    assert result["direct_report_checked"] is True


@pytest.mark.parametrize("key", sorted(FORBIDDEN - {"evolution_contract"}))
def test_parallel_report_representation_is_rejected(key: str) -> None:
    report = {"evolution_contract": model(), key: {}}
    with pytest.raises(ValueError, match="parallel machine representations"):
        attach_system_contract(report)


def test_removed_projection_module_is_not_importable() -> None:
    assert importlib.util.find_spec("codex_wire_audit.compatibility_views") is None


def test_complete_status_requires_every_required_source() -> None:
    candidate = build_system_contract(model())
    candidate["model"]["status"]["overall"] = "complete"
    required = [
        key
        for key, spec in candidate["model"]["source_registry"]["sources"].items()
        if spec["required"]
    ]
    assert required
    victim = required[0]
    candidate["model"]["source_snapshot"]["files"].pop(victim, None)
    if victim not in candidate["model"]["source_snapshot"]["unavailable_specs"]:
        candidate["model"]["source_snapshot"]["unavailable_specs"].append(victim)
    files = candidate["model"]["source_snapshot"]["files"]
    unavailable = candidate["model"]["source_snapshot"]["unavailable_specs"]
    candidate["model"]["source_snapshot"]["counts"] = {
        "available": len(files), "unavailable": len(unavailable)
    }
    body = {key: value for key, value in candidate.items() if key != "integrity"}
    from codex_wire_audit.system_contract import digest
    candidate["integrity"]["canonical_sha256"] = digest(body)
    with pytest.raises(ValueError, match="missing required sources"):
        validate_system_contract(candidate)
