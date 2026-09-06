#!/usr/bin/env python3
"""Run the canonical config-surface extractor against a real Codex checkout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from jsonschema import Draft202012Validator

# Allow direct execution from any working directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_wire_audit.diagnostics import DiagnosticCollector  # noqa: E402
from codex_wire_audit.extractors.config_effects import ConfigEffectsExtractor, SOURCE_IDS  # noqa: E402
from codex_wire_audit.legacy import load_legacy_modules  # noqa: E402
from codex_wire_audit.models import SourceFile, SourceRevision, SourceSnapshot  # noqa: E402
from codex_wire_audit.source_registry import from_legacy_maps  # noqa: E402


_REQUIRED_PATHS = {
    "features.context_management",
    "features.context_management.experimental_mode",
    "features.token_budget",
    "features.token_budget.use_history_notes_extension",
    "features.tool_registry.turn_metadata_includes_tool_info",
    "openai_base_url",
    "chatgpt_base_url",
    "model_providers.*.base_url",
    "model_providers.*.experimental_bearer_token",
    "responses_api_metadata.*",
}


def _git(root: Path, *args: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _snapshot(codex_root: Path) -> SourceSnapshot:
    legacy = load_legacy_modules()
    registry = from_legacy_maps(legacy.base.FILES, legacy.base.SURFACE_FILES, legacy.entrypoint.EXTRA)
    files: dict[str, SourceFile] = {}
    missing: dict[str, str] = {}
    for spec_id in ConfigEffectsExtractor.result_source_spec_ids:
        spec = registry.get(spec_id)
        selected: Path | None = None
        candidate_index = 0
        for index, candidate in enumerate(spec.path_candidates):
            path = codex_root / candidate
            if path.is_file():
                selected = path
                candidate_index = index
                break
        if selected is None:
            missing[spec_id] = "no candidate path exists"
            continue
        files[spec_id] = SourceFile.create(
            spec=spec,
            selected_path=selected.relative_to(codex_root).as_posix(),
            raw_bytes=selected.read_bytes(),
            path_candidate_index=candidate_index,
        )
    commit = _git(codex_root, "rev-parse", "HEAD")
    revision = SourceRevision(
        source_mode="local_git_worktree",
        repository="openai/codex",
        requested_ref=commit or "worktree",
        resolved_commit_sha=commit,
        source_set_sha256=SourceSnapshot.digest_files(files),
        dirty=bool(_git(codex_root, "status", "--porcelain")),
        commit_date=_git(codex_root, "show", "-s", "--format=%cI", "HEAD"),
        commit_message=_git(codex_root, "show", "-s", "--format=%s", "HEAD"),
    )
    return SourceSnapshot(revision=revision, files=files, unavailable_specs=missing)


def _schema_errors(data: dict[str, Any]) -> list[str]:
    schema_path = ROOT / "codex_wire_audit" / "proof_schema_templates" / "config-surface-semantics-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return [error.message for error in Draft202012Validator(schema).iter_errors(data)]


def evaluate(codex_root: Path) -> tuple[dict[str, Any], int]:
    snapshot = _snapshot(codex_root)
    diagnostics = DiagnosticCollector()
    result = ConfigEffectsExtractor().extract(snapshot, diagnostics)
    schema = result.data.get("config_schema") or {}
    summary = schema.get("summary") or {}
    paths = set((schema.get("paths") or {}).keys())
    crosswalk = result.data.get("feature_crosswalk") or {}
    graph = result.data.get("surface_graph") or {}
    failures: list[str] = []
    if not result.semantic_complete:
        failures.append("extractor semantic_complete=false")
    failures.extend(_schema_errors(result.data) if result.data else ["extractor returned no data"])
    if int(summary.get("root_property_count") or 0) < 50:
        failures.append("generated schema has fewer than 50 root properties")
    if int(summary.get("definition_count") or 0) < 25:
        failures.append("generated schema has fewer than 25 definitions")
    if int(summary.get("config_path_count") or 0) < 200:
        failures.append("normalized config catalog has fewer than 200 paths")
    missing_paths = sorted(_REQUIRED_PATHS - paths)
    if missing_paths:
        failures.append("required config paths missing: " + ", ".join(missing_paths))
    if int((crosswalk.get("counts") or {}).get("registered") or 0) < 50:
        failures.append("fewer than 50 FeatureSpec entries were extracted")
    if crosswalk.get("registered_without_root_schema"):
        failures.append("registered features missing from generated schema")
    if (graph.get("coverage") or {}).get("unresolved_node_refs"):
        failures.append("surface graph contains unresolved node references")
    payload = {
        "format": "codex-wire-audit-config-surface-integration/v1",
        "codex_revision": snapshot.revision.to_dict(),
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "diagnostics": diagnostics.to_list(),
        "summary": {
            **summary,
            "feature_registry_count": (crosswalk.get("counts") or {}).get("registered", 0),
            "effect_link_count": len(result.data.get("effect_links") or []),
            "graph_node_count": (graph.get("coverage") or {}).get("node_count", 0),
            "graph_edge_count": (graph.get("coverage") or {}).get("edge_count", 0),
        },
        "config_schema": schema,
        "surface_graph": graph,
    }
    return payload, 0 if not failures else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--schema-output", type=Path)
    parser.add_argument("--graph-output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload, status = evaluate(args.codex_root.resolve())
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for path, value in (
        (args.schema_output, payload.get("config_schema")),
        (args.graph_output, payload.get("surface_graph")),
    ):
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
