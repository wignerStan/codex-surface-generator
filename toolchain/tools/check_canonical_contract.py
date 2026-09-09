#!/usr/bin/env python3
"""Check canonical assets or a generated contract/report, without network access."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_wire_audit.canonical import canonical_json_bytes
from codex_wire_audit.schemas import schema_documents
from codex_wire_audit.system_contract import build_system_contract
from codex_wire_audit.system_contract_evidence import load_document
from codex_wire_audit.system_contract_validation import validate_system_contract

ASSETS = ROOT.parent / "contracts" / "codex-system-contract" / "v1"


def expected_assets() -> dict[str, bytes]:
    # Consume existing source fixtures, not a parallel narrative system model.
    from tools.build_v11_assets import _contract

    model, _ = _contract("responses_metadata_identity_split.rs")
    contract = build_system_contract(model)
    validate_system_contract(contract)
    documents = schema_documents()
    schema = copy.deepcopy(documents.pop("system-contract.schema.json"))
    schema["$defs"] = documents
    return {
        "schema.json": (json.dumps(schema, indent=2, sort_keys=True) + "\n").encode(),
        "example.fixture.json": canonical_json_bytes(contract),
    }


def check_assets(*, write: bool = False) -> dict[str, object]:
    for name, expected in expected_assets().items():
        path = ASSETS / name
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)
        elif not path.is_file() or path.read_bytes() != expected:
            raise ValueError(f"stale canonical asset: {path.relative_to(ROOT.parent)}")
    return {"canonical_assets": "written" if write else "verified", "example_source": "fixture"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--contract", type=Path)
    source.add_argument("--report", type=Path)
    parser.add_argument("--source-root", type=Path, help="revalidate exact local Codex source bytes")
    parser.add_argument("--write-assets", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.write_assets and (args.contract or args.report or args.source_root):
        parser.error("--write-assets cannot be combined with input/source validation")
    if args.source_root and not (args.contract or args.report):
        parser.error("--source-root requires --contract or --report")
    try:
        if args.contract or args.report:
            document = load_document(args.contract or args.report)
            report = document if args.report else None
            contract = document.get("system_contract") if report is not None else document
            result = validate_system_contract(contract, report=report, source_root=args.source_root)
        else:
            result = check_assets(write=args.write_assets)
        status = 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        result, status = {"status": "failed", "error": str(error)}, 2
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
