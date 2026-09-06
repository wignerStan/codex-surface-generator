#!/usr/bin/env python3
"""Validate Codex wire-audit v10 reports and protocol conformance fixtures.

The core generator is standard-library only. This optional validator uses
``jsonschema>=4.18`` and ``referencing`` to execute the emitted Draft 2020-12
schemas, including their URN references.
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError as error:  # pragma: no cover - environment-dependent
    print(
        "error: install jsonschema>=4.18 to run fixture/schema validation",
        file=sys.stderr,
    )
    raise SystemExit(2) from error


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read JSON {path}: {error}") from error


def load_schema_directory(path: Path) -> tuple[dict[str, dict[str, Any]], Registry]:
    documents: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource[Any]]] = []
    for schema_path in sorted(path.glob("*.schema.json")):
        document = read_json(schema_path)
        if not isinstance(document, dict):
            raise RuntimeError(f"schema is not a JSON object: {schema_path}")
        schema_id = document.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise RuntimeError(f"schema has no non-empty $id: {schema_path}")
        Draft202012Validator.check_schema(document)
        documents[schema_path.name] = document
        resources.append((schema_id, Resource.from_contents(document)))
    if not documents:
        raise RuntimeError(f"no *.schema.json files found under {path}")
    return documents, Registry().with_resources(resources)


def value_at_dotted_path(value: Any, path: str) -> Any:
    current = value
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise RuntimeError(f"TOML path not found: {path}")
        current = current[segment]
    return current


def read_fixture(path: Path, row: dict[str, Any]) -> Any:
    if path.suffix.lower() == ".toml":
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise RuntimeError(f"could not read TOML {path}: {error}") from error
        dotted = row.get("toml_path")
        return value_at_dotted_path(document, str(dotted)) if dotted else document
    return read_json(path)


def error_record(error: Any) -> dict[str, Any]:
    instance_path = "$" + "".join(
        f"[{segment}]" if isinstance(segment, int) else f".{segment}"
        for segment in error.absolute_path
    )
    schema_path = "#" + "".join(
        f"/{str(segment).replace('~', '~0').replace('/', '~1')}"
        for segment in error.absolute_schema_path
    )
    return {
        "instance_path": instance_path,
        "schema_path": schema_path,
        "message": error.message,
    }


def validate_fixtures(
    schema_dir: Path,
    fixture_dir: Path,
) -> dict[str, Any]:
    schemas, registry = load_schema_directory(schema_dir)
    manifest = read_json(fixture_dir / "manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("fixtures"), list):
        raise RuntimeError("fixture manifest must contain a fixtures array")

    results: list[dict[str, Any]] = []
    failures = 0
    for raw_row in manifest["fixtures"]:
        if not isinstance(raw_row, dict):
            raise RuntimeError("fixture manifest entries must be objects")
        row = dict(raw_row)
        fixture_name = str(row.get("file") or "")
        schema_name = str(row.get("schema_file") or "")
        expected = str(row.get("expected") or "")
        if not fixture_name or not schema_name or expected not in {"valid", "invalid"}:
            raise RuntimeError(f"invalid fixture manifest entry: {row!r}")
        if schema_name not in schemas:
            raise RuntimeError(f"fixture {fixture_name} references missing schema {schema_name}")
        fixture = read_fixture(fixture_dir / fixture_name, row)
        validator = Draft202012Validator(schemas[schema_name], registry=registry)
        errors = sorted(validator.iter_errors(fixture), key=lambda error: list(error.path))
        actual = "invalid" if errors else "valid"
        passed = actual == expected
        failures += not passed
        results.append(
            {
                "file": fixture_name,
                "schema_file": schema_name,
                "expected": expected,
                "actual": actual,
                "passed": passed,
                "diagnostic_code": row.get("diagnostic_code"),
                "errors": [error_record(error) for error in errors[:20]],
            }
        )
    return {
        "kind": "codex-wire-audit-v10-fixture-validation",
        "schema_directory": str(schema_dir),
        "fixture_directory": str(fixture_dir),
        "total": len(results),
        "passed": len(results) - failures,
        "failed": failures,
        "results": results,
    }


def validate_report(report_path: Path, schema_dir: Path) -> dict[str, Any]:
    schemas, registry = load_schema_directory(schema_dir)
    name = "codex-wire-audit-report.schema.json"
    if name not in schemas:
        raise RuntimeError(f"missing report schema: {schema_dir / name}")
    report = read_json(report_path)
    validator = Draft202012Validator(schemas[name], registry=registry)
    errors = sorted(validator.iter_errors(report), key=lambda error: list(error.path))
    return {
        "kind": "codex-wire-audit-v10-report-schema-validation",
        "report": str(report_path),
        "schema_directory": str(schema_dir),
        "valid": not errors,
        "errors": [error_record(error) for error in errors[:100]],
    }


def print_human(result: dict[str, Any]) -> None:
    if result["kind"].endswith("fixture-validation"):
        for row in result["results"]:
            mark = "PASS" if row["passed"] else "FAIL"
            print(
                f"{mark} {row['file']}: {row['actual']} "
                f"(expected {row['expected']}; {row['schema_file']})"
            )
            if not row["passed"]:
                for error in row["errors"][:5]:
                    print(f"  {error['instance_path']}: {error['message']}")
        print(
            f"fixtures: {result['passed']}/{result['total']} passed; "
            f"{result['failed']} failed"
        )
    else:
        print(f"report schema: {'valid' if result['valid'] else 'invalid'}")
        for error in result["errors"][:20]:
            print(f"  {error['instance_path']}: {error['message']}")


def main() -> int:
    script_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=False)
    mode.add_argument("--report", type=Path, help="validate one v10 report JSON document")
    mode.add_argument(
        "--fixtures",
        type=Path,
        default=script_root / "codex_wire_audit_v10_fixtures",
        help="fixture directory (default: beside this script)",
    )
    parser.add_argument(
        "--schemas",
        type=Path,
        help="schema directory; defaults to report/ or protocol-example/ beside this script",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable validation JSON")
    args = parser.parse_args()

    try:
        if args.report:
            schema_dir = args.schemas or script_root / "codex_wire_audit_v10_schemas" / "report"
            result = validate_report(args.report, schema_dir)
            failed = not result["valid"]
        else:
            schema_dir = args.schemas or script_root / "codex_wire_audit_v10_schemas" / "protocol-example"
            result = validate_fixtures(schema_dir, args.fixtures)
            failed = bool(result["failed"])
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_human(result)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
