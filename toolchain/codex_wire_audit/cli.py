"""Command-line interface for the maintainability-focused generator."""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

from . import GENERATOR_VERSION, REPORT_FORMAT_VERSION
from .canonical import CanonicalizationError, canonical_json_bytes
from .legacy import load_legacy_modules
from .orchestrator import (
    GenerationResult,
    SourceLoadError,
    build_registry,
    generate_report,
    load_baseline,
)
from .schemas import write_schema_documents as write_v11_schema_documents
from .proof_profiles import PROFILES
from .validation import validate_evolution_contract


def _selected_output(report: Mapping[str, Any], sections: list[str], contract: Any) -> Any:
    if not sections:
        return report
    if len(sections) == 1:
        return contract.json_pointer_get(report, sections[0])
    return {section: contract.json_pointer_get(report, section) for section in sections}


def _canonical_output_view(value: Any, *, full_report: bool) -> Any:
    if not full_report or not isinstance(value, Mapping):
        return value
    view = copy.deepcopy(dict(value))
    view.pop("generated_at", None)
    status = view.get("status")
    if isinstance(status, dict):
        status.pop("validated_at", None)
    return view


def _render_output(
    value: Any,
    output_format: str,
    report: Mapping[str, Any],
    legacy: Any,
) -> tuple[str | None, bytes | None]:
    full = value is report
    if output_format == "canonical-json":
        return None, canonical_json_bytes(_canonical_output_view(value, full_report=full))
    if output_format == "pretty-json":
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n", None
    if output_format == "jsonl":
        if not full:
            return (
                json.dumps(
                    {"record_type": "selection", "value": value},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                None,
            )
        text = legacy.contract.render_jsonl(report)
        text += json.dumps(
            {
                "record_type": "evolution_contract",
                "id": (report.get("evolution_contract") or {}).get("source_revision", {}).get("source_revision_id"),
                "value": report.get("evolution_contract"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        if report.get("semantic_diff"):
            text += json.dumps(
                {
                    "record_type": "semantic_diff",
                    "value": report["semantic_diff"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
        return text, None
    if not full:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", None
    old_stdout, buffer = sys.stdout, io.StringIO()
    try:
        sys.stdout = buffer
        legacy.base.print_text(report)
        legacy.entrypoint.print_v10(report)
        evolution = report.get("evolution_contract") or {}
        status = evolution.get("status") or {}
        source = evolution.get("source_revision") or {}
        print("\n=== 17. MAINTAINABILITY / EVOLUTION CONTRACT ===")
        print("generator: " + GENERATOR_VERSION)
        print("source revision: " + str(source.get("source_revision_id")))
        print("source mode: " + str(source.get("source_mode")))
        print("migration stage: " + str((evolution.get("migration") or {}).get("stage")))
        print("semantic classification: " + str(status.get("semantic_classification")))
        print("schema resolution: " + str(status.get("schema_resolution")))
        print("runtime observation: " + str(status.get("runtime_observation")))
        print("diagnostics: " + json.dumps(report.get("diagnostic_summary") or {}, sort_keys=True))
        if report.get("semantic_diff"):
            print("semantic diff: " + json.dumps(report["semantic_diff"].get("summary") or {}, sort_keys=True))
    finally:
        sys.stdout = old_stdout
    return buffer.getvalue(), None


def _write_output(path: str | None, text: str | None, binary: bytes | None) -> None:
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        if binary is not None:
            output.write_bytes(binary)
        else:
            output.write_text(text or "", encoding="utf-8")
    elif binary is not None:
        sys.stdout.buffer.write(binary)
    else:
        sys.stdout.write(text or "")


def _write_v11_output_directory(
    directory: str,
    result: GenerationResult,
    legacy: Any,
    *,
    include_examples: bool,
) -> None:
    root = Path(directory)
    legacy.contract.write_output_directory(root, result.report, include_fixtures=include_examples)
    (root / "evolution-contract.json").write_text(
        json.dumps(result.report["evolution_contract"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (root / "source-registry.json").write_text(
        json.dumps(result.registry.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if result.semantic_diff is not None:
        (root / "semantic-diff.json").write_text(
            json.dumps(result.semantic_diff, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    config_data = (result.report.get("evolution_contract") or {}).get("extractors", {}).get("extractor.config_effects", {}).get("data")
    if isinstance(config_data, Mapping):
        (root / "config-schema.json").write_text(
            json.dumps(config_data.get("config_schema") or {}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (root / "config-surface-graph.json").write_text(
            json.dumps(config_data.get("surface_graph") or {}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    write_v11_schema_documents(root / "schemas" / "maintainability")
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "report_format_version": REPORT_FORMAT_VERSION,
        "source_revision_id": result.snapshot.revision.to_dict()["source_revision_id"],
        "artifacts": sorted(
            str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
        ),
    }
    (root / "v11-output-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fail_policy(report: Mapping[str, Any], policy: str, strict: bool) -> None:
    diagnostics = report.get("diagnostics") or []
    severities = [str(item.get("severity")) for item in diagnostics if isinstance(item, Mapping)]
    if strict and any(severity in {"warning", "error"} for severity in severities):
        raise SourceLoadError("strict mode rejects warning/error diagnostics")
    if policy == "never":
        return
    if policy == "warning" and any(severity in {"warning", "error"} for severity in severities):
        raise SourceLoadError("report contains warning/error diagnostics")
    if policy == "error" and "error" in severities:
        raise SourceLoadError("report contains error diagnostics")
    if policy == "incomplete" and not (report.get("status") or {}).get("complete", False):
        raise SourceLoadError("report status is incomplete")


def _fail_semantic_diff(report: Mapping[str, Any], policy: str) -> None:
    if policy == "never" or not report.get("semantic_diff"):
        return
    summary = report["semantic_diff"].get("summary") or {}
    if policy == "breaking" and summary.get("breaking", 0):
        raise SourceLoadError("semantic diff contains breaking changes")
    if policy == "any" and summary.get("total", 0):
        raise SourceLoadError("semantic diff contains changes")


def _validate_existing_report(path: str, legacy: Any) -> list[dict[str, Any]]:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceLoadError(f"cannot read report {path}: {error}") from error
    if not isinstance(report, Mapping):
        raise SourceLoadError("report root must be an object")
    diagnostics = list(legacy.contract.validate_report(report))
    diagnostics.extend(validate_evolution_contract(report))
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in diagnostics:
        unique[(str(item.get("code")), str(item.get("report_pointer") or item.get("pointer")))] = item
    return list(unique.values())


def _self_test(legacy: Any) -> None:
    legacy.base.self_test()
    legacy.entrypoint.self_test()
    legacy.entrypoint.contract_self_test()
    from .canonical import CanonicalizationError, canonical_json_bytes
    try:
        canonical_json_bytes({"e\u0301": 1, "é": 2})
    except CanonicalizationError:
        pass
    else:
        raise AssertionError("Unicode-normalized key collision was not detected")
    registry = build_registry(legacy)
    assert registry.get("source_spec.base.metadata").extractor_ids == ("extractor.turn_metadata",)
    print("v11 maintainability self-test: ok")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Codex wire audit v11 maintainability layer: v10-compatible reports plus a "
            "source registry, exact-byte revision identity, canonical semantic extractors, "
            "qualified schema IDs, and commit-to-commit semantic diffs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --repo-root ../codex --format canonical-json --output report.json\n"
            "  %(prog)s --repo-root ../codex --baseline-report previous.json --semantic-diff-output diff.json\n"
            "  %(prog)s --repo-root ../codex --worktree --allow-dirty-source --output-dir out\n"
            "  %(prog)s --validate-report report.json --format pretty-json"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {GENERATOR_VERSION} (compatible report format {REPORT_FORMAT_VERSION})",
    )
    parser.add_argument("--repo", default="openai/codex")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--api-base", default="https://api.github.com")
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))
    parser.add_argument("--json", action="store_true", help="compatibility alias for --format pretty-json")
    parser.add_argument(
        "--format",
        choices=("text", "pretty-json", "canonical-json", "jsonl"),
        default="text",
    )
    parser.add_argument("--output")
    parser.add_argument("--output-dir")
    parser.add_argument("--section", action="append", default=[])
    parser.add_argument("--emit-schema", metavar="DIRECTORY")
    parser.add_argument("--emit-ir", metavar="FILE", help="write the canonical evolution contract")
    parser.add_argument("--emit-config-schema", metavar="FILE", help="write the normalized generated config schema catalog")
    parser.add_argument("--emit-surface-graph", metavar="FILE", help="write the config-to-protocol surface graph")
    parser.add_argument("--write-source-registry", metavar="FILE")
    parser.add_argument("--include-examples", action="store_true")
    parser.add_argument("--validate-report", metavar="REPORT_JSON")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument(
        "--fail-on",
        choices=("never", "warning", "error", "incomplete"),
        default="error",
    )
    parser.add_argument("--parser", choices=("auto", "regex"), default="auto")
    parser.add_argument(
        "--coverage-profile",
        choices=tuple(sorted(PROFILES)),
        default="codex_wire_full",
    )
    parser.add_argument("--source-registry", help="JSON overlay for source path candidates and requirements")
    parser.add_argument("--baseline-report")
    parser.add_argument("--semantic-diff-output")
    parser.add_argument(
        "--fail-on-semantic-change",
        choices=("never", "breaking", "any"),
        default="breaking",
    )
    parser.add_argument("--fetch-workers", type=int, default=8)
    parser.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "codex-wire-audit"))
    parser.add_argument("--no-cache", action="store_true")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--repo-root")
    source.add_argument("--source-archive")
    source.add_argument("--cache-only", action="store_true")
    parser.add_argument("--source-commit")
    parser.add_argument("--allow-dirty-source", action="store_true")
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="read exact working-tree bytes instead of git show <commit>:<path>",
    )
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--self-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.json:
        if args.format != "text":
            parser.error("--json cannot be combined with an explicit --format")
        args.format = "pretty-json"
    if args.source_commit and not (args.repo_root or args.source_archive):
        parser.error("--source-commit requires --repo-root or --source-archive")
    if args.worktree and not args.repo_root:
        parser.error("--worktree requires --repo-root")
    if args.allow_dirty_source and not args.repo_root:
        parser.error("--allow-dirty-source requires --repo-root")
    if args.fetch_workers < 1 or args.fetch_workers > 32:
        parser.error("--fetch-workers must be between 1 and 32")

    legacy = load_legacy_modules()
    legacy.base.configure_cache_dir(None if args.no_cache else args.cache_dir)
    if args.self_test:
        _self_test(legacy)
        return 0

    if args.validate_report:
        diagnostics = _validate_existing_report(args.validate_report, legacy)
        payload = {"valid": not diagnostics, "diagnostics": diagnostics}
        if args.format == "canonical-json":
            text, binary = None, canonical_json_bytes(payload)
        elif args.format in {"pretty-json", "jsonl"}:
            if args.format == "jsonl":
                text = "".join(
                    json.dumps({"record_type": "diagnostic", "value": item}, sort_keys=True, separators=(",", ":")) + "\n"
                    for item in diagnostics
                )
                text += json.dumps(
                    {"record_type": "validation_summary", "valid": not diagnostics, "diagnostic_count": len(diagnostics)},
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n"
            else:
                text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            binary = None
        else:
            text = (
                "\n".join(
                    f"{item.get('severity')}: {item.get('code')}: {item.get('message')}"
                    for item in diagnostics
                )
                + ("\n" if diagnostics else "report validation: ok\n")
            )
            binary = None
        _write_output(args.output, text, binary)
        return 2 if diagnostics else 0

    baseline = load_baseline(args.baseline_report)
    result = generate_report(args, legacy, baseline_report=baseline)
    report = result.report
    _fail_policy(report, args.fail_on, args.strict)
    _fail_semantic_diff(report, args.fail_on_semantic_change)

    if args.emit_schema:
        root = Path(args.emit_schema)
        legacy.contract.write_schema_documents(root / "report")
        legacy.contract.write_protocol_schema_documents(root / "protocol", report)
        write_v11_schema_documents(root / "maintainability")
    if args.output_dir:
        _write_v11_output_directory(
            args.output_dir,
            result,
            legacy,
            include_examples=args.include_examples,
        )
    if args.emit_ir:
        Path(args.emit_ir).write_text(
            json.dumps(report["evolution_contract"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    config_data = (report.get("evolution_contract") or {}).get("extractors", {}).get("extractor.config_effects", {}).get("data")
    if args.emit_config_schema:
        if not isinstance(config_data, Mapping):
            raise SourceLoadError("config schema was not extracted for the selected source/profile")
        output = Path(args.emit_config_schema)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(config_data.get("config_schema") or {}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.emit_surface_graph:
        if not isinstance(config_data, Mapping):
            raise SourceLoadError("config surface graph was not extracted for the selected source/profile")
        output = Path(args.emit_surface_graph)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(config_data.get("surface_graph") or {}, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.write_source_registry:
        Path(args.write_source_registry).write_text(
            json.dumps(result.registry.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.semantic_diff_output and result.semantic_diff is not None:
        Path(args.semantic_diff_output).write_text(
            json.dumps(result.semantic_diff, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    value = _selected_output(report, args.section, legacy.contract)
    text, binary = _render_output(value, args.format, report, legacy)
    if args.output or not args.output_dir or args.format != "text":
        _write_output(args.output, text, binary)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SourceLoadError, CanonicalizationError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
