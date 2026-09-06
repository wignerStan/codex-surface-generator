#!/usr/bin/env python3
"""Enforce ratcheted line-and-branch coverage for proof-critical v13 modules."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

DEFAULT_COVERAGE_JSON = Path("coverage-v13.json")
MODULE_THRESHOLDS = {
    "codex_wire_audit/history_cli.py": 60.0,
    "codex_wire_audit/metadata_history.py": 72.0,
    "codex_wire_audit/metadata_history_container.py": 78.0,
    "codex_wire_audit/metadata_history_probe.py": 80.0,
    "codex_wire_audit/metadata_history_source.py": 70.0,
    "codex_wire_audit/proof_gate.py": 67.0,
    "codex_wire_audit/proof_metadata_history.py": 80.0,
}
AGGREGATE_THRESHOLD = 72.0


def _combined_percent(summary: dict[str, Any]) -> float:
    statements = int(summary.get("num_statements", 0))
    branches = int(summary.get("num_branches", 0))
    covered_lines = int(summary.get("covered_lines", 0))
    covered_branches = int(summary.get("covered_branches", 0))
    opportunities = statements + branches
    if opportunities == 0:
        return 100.0
    return 100.0 * (covered_lines + covered_branches) / opportunities


def check(path: Path) -> tuple[dict[str, Any], list[str]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    files = document.get("files")
    if not isinstance(files, dict):
        raise ValueError("coverage JSON has no files map")
    failures: list[str] = []
    modules: dict[str, Any] = {}
    totals = {
        "num_statements": 0,
        "covered_lines": 0,
        "num_branches": 0,
        "covered_branches": 0,
    }
    for module, threshold in MODULE_THRESHOLDS.items():
        item = files.get(module)
        if not isinstance(item, dict) or not isinstance(item.get("summary"), dict):
            failures.append(f"missing coverage record: {module}")
            continue
        summary = item["summary"]
        percent = _combined_percent(summary)
        modules[module] = {
            "combined_percent": round(percent, 2),
            "threshold": threshold,
            "passes": percent + 1e-9 >= threshold,
            "statements": summary.get("num_statements"),
            "covered_lines": summary.get("covered_lines"),
            "branches": summary.get("num_branches"),
            "covered_branches": summary.get("covered_branches"),
        }
        for key in totals:
            totals[key] += int(summary.get(key, 0))
        if percent + 1e-9 < threshold:
            failures.append(f"{module}: {percent:.2f}% < {threshold:.2f}%")
    aggregate = _combined_percent(totals)
    if aggregate + 1e-9 < AGGREGATE_THRESHOLD:
        failures.append(
            f"proof-critical aggregate: {aggregate:.2f}% < {AGGREGATE_THRESHOLD:.2f}%"
        )
    result = {
        "format": "codex-wire-audit-coverage-ratchet/v1",
        "coverage_json": str(path),
        "modules": modules,
        "aggregate": {
            "combined_percent": round(aggregate, 2),
            "threshold": AGGREGATE_THRESHOLD,
            "passes": aggregate + 1e-9 >= AGGREGATE_THRESHOLD,
            **totals,
        },
        "status": "passed" if not failures else "failed",
    }
    return result, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage_json", nargs="?", type=Path, default=DEFAULT_COVERAGE_JSON)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result, failures = check(args.coverage_json)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"coverage ratchet failed: {error}", file=sys.stderr)
        return 2
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if failures:
        for failure in failures:
            print(f"coverage ratchet: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
