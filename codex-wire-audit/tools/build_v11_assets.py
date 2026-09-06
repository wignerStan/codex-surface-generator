#!/usr/bin/env python3
"""Regenerate deterministic v11 schemas, examples, and the source registry.

This script intentionally consumes only package APIs.  It is both a release
helper and an example of how downstream automation can build the canonical
maintainability contract without parsing the presentation report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_wire_audit.diagnostics import DiagnosticCollector
from codex_wire_audit.evolution import build_evolution_contract
from codex_wire_audit.legacy import load_legacy_modules
from codex_wire_audit.models import SourceFile, SourceGroup, SourceRevision, SourceSnapshot, SourceSpec
from codex_wire_audit.orchestrator import build_registry
from codex_wire_audit.schemas import write_schema_documents
from codex_wire_audit.semantic_diff import compare
from codex_wire_audit.source_registry import SourceRegistry

FIXTURE_DIR = ROOT / "codex_wire_audit_v11_fixtures"
EXAMPLE_DIR = ROOT / "codex_wire_audit_v11_examples"
SCHEMA_DIR = ROOT / "codex_wire_audit_v11_schemas"
SOURCE_PATH = "codex-rs/core/src/responses_metadata.rs"
SPEC_ID = "source_spec.base.metadata"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _snapshot(fixture_name: str) -> tuple[SourceRegistry, SourceSnapshot]:
    spec = SourceSpec(
        id=SPEC_ID,
        legacy_key="metadata",
        group=SourceGroup.BASE,
        path_candidates=(SOURCE_PATH,),
        required=True,
        roles=("responses_metadata", "turn_metadata_semantics"),
        expected_symbols=("CodexResponsesMetadata", "CodexTurnMetadataPayload", "turn_metadata_payload"),
        extractor_ids=("extractor.turn_metadata",),
    )
    raw = (FIXTURE_DIR / fixture_name).read_bytes()
    source_file = SourceFile.create(spec=spec, selected_path=SOURCE_PATH, raw_bytes=raw)
    files = {spec.id: source_file}
    digest = SourceSnapshot.digest_files(files)
    snapshot = SourceSnapshot(
        revision=SourceRevision(
            source_mode="fixture",
            repository="openai/codex",
            requested_ref=fixture_name,
            resolved_commit_sha=None,
            source_set_sha256=digest,
            dirty=None,
            commit_message="deterministic maintainability fixture",
        ),
        files=files,
    )
    return SourceRegistry([spec]), snapshot


def _contract(fixture_name: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    registry, snapshot = _snapshot(fixture_name)
    diagnostics = DiagnosticCollector()
    contract, _ = build_evolution_contract(
        snapshot,
        registry,
        diagnostics,
        coverage_profile="fixture",
    )
    return contract, diagnostics.to_list()


def _manifest(root: Path, *, excluded: set[Path] | None = None) -> dict[str, Any]:
    excluded = excluded or set()
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(path for path in root.rglob("*") if path.is_file() and path not in excluded):
        raw = path.read_bytes()
        files[str(path.relative_to(root))] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return {"schema_version": "1.0.0", "files": files, "count": len(files)}


def build_assets(*, schema_dir: Path, example_dir: Path) -> None:
    if schema_dir.exists():
        shutil.rmtree(schema_dir)
    if example_dir.exists():
        shutil.rmtree(example_dir)
    schema_dir.mkdir(parents=True)
    example_dir.mkdir(parents=True)

    write_schema_documents(schema_dir)

    current, current_diagnostics = _contract("responses_metadata_identity_split.rs")
    legacy, legacy_diagnostics = _contract("responses_metadata_combined_identity.rs")
    unclassified, unclassified_diagnostics = _contract("responses_metadata_unclassified.rs")
    semantic_diff = compare(legacy, current)

    _write_json(example_dir / "evolution-contract.identity-split.json", current)
    _write_json(example_dir / "evolution-contract.legacy-combined-identity.json", legacy)
    _write_json(example_dir / "semantic-diff.identity-split.json", semantic_diff)
    _write_json(
        example_dir / "evolution-contract.unclassified-expression.json",
        {"evolution_contract": unclassified, "diagnostics": unclassified_diagnostics},
    )
    _write_json(
        example_dir / "fixture-diagnostics.json",
        {
            "identity_split": current_diagnostics,
            "legacy_combined_identity": legacy_diagnostics,
            "unclassified_expression": unclassified_diagnostics,
        },
    )

    legacy_modules = load_legacy_modules()
    registry = build_registry(legacy_modules)
    _write_json(example_dir / "source-registry.default.json", registry.to_dict())
    _write_json(
        example_dir / "source-registry.overlay.example.json",
        {
            "schema_version": "1.0.0",
            "sources": {
                SPEC_ID: {
                    "path_candidates": [
                        "codex-rs/core/src/responses/metadata.rs",
                        SOURCE_PATH,
                    ]
                }
            },
        },
    )

    (example_dir / "README.md").write_text(
        "# v11 generated examples\n\n"
        "These files are generated by `python tools/build_v11_assets.py`.\n\n"
        "- `evolution-contract.identity-split.json` shows independent thread and turn identity.\n"
        "- `evolution-contract.legacy-combined-identity.json` captures the older combined gate.\n"
        "- `semantic-diff.identity-split.json` demonstrates the change a current-main canary sees.\n"
        "- `evolution-contract.unclassified-expression.json` demonstrates fail-visible semantics.\n"
        "- `source-registry.default.json` is the generated registry behind all source providers.\n"
        "- `source-registry.overlay.example.json` shows a file-move override without code edits.\n",
        encoding="utf-8",
    )

    _write_json(schema_dir / "manifest.json", _manifest(schema_dir, excluded={schema_dir / "manifest.json"}))
    _write_json(example_dir / "manifest.json", _manifest(example_dir, excluded={example_dir / "manifest.json"}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT,
        help="Root under which generated v11 schema/example directories are written.",
    )
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    build_assets(
        schema_dir=output_root / "codex_wire_audit_v11_schemas",
        example_dir=output_root / "codex_wire_audit_v11_examples",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
