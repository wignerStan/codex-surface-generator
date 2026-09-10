from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

ROOT = Path.cwd()
PACKAGE = ROOT / "toolchain" / "codex_wire_audit"
TESTS = ROOT / "toolchain" / "tests"
FORMAT = "codex-system-contract/v1"
SCHEMA_ID = "https://schemas.codex-wire-audit.invalid/system/v1/system-contract.schema.json"
CANONICALIZATION = "codex-wire-audit-canonical-json-v2"
FORBIDDEN_REPORT_KEYS = {
    "config_schema",
    "config_surface_graph",
    "evolution_contract",
    "local_storage_schema",
}
COMPATIBILITY_CALLS = {
    "apply_config_surface_overlay",
    "apply_local_storage_overlay",
}


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


SYSTEM_CONTRACT = '''\
"""The single source-derived machine representation of the audited system.

The finalized extractor model is sealed once under ``system_contract``.  This
module intentionally provides no aliases, projections, mirrors, or fallback
read paths.  A consumer either understands this versioned contract or rejects
it.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping, MutableMapping

from .canonical import canonical_json_bytes

FORMAT = "codex-system-contract/v1"
SCHEMA_ID = "https://schemas.codex-wire-audit.invalid/system/v1/system-contract.schema.json"
CANONICALIZATION = "codex-wire-audit-canonical-json-v2"
_FORBIDDEN_PARALLEL_KEYS = frozenset(
    {"config_schema", "config_surface_graph", "local_storage_schema"}
)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def build_system_contract(model: Mapping[str, Any]) -> dict[str, Any]:
    """Seal one finalized, source-derived model without secondary views."""
    body = {
        "$schema": SCHEMA_ID,
        "schema": FORMAT,
        "authority": "migrated_semantic_extractors",
        "model": copy.deepcopy(dict(model)),
    }
    return {
        **body,
        "integrity": {
            "algorithm": "sha256",
            "canonicalization": CANONICALIZATION,
            "canonical_sha256": digest(body),
        },
    }


def attach_system_contract(report: MutableMapping[str, Any]) -> None:
    """Replace the private construction model with the one public contract."""
    if "system_contract" in report:
        raise ValueError("system_contract is already attached")
    if "evolution_contract" not in report:
        raise ValueError("missing finalized extractor model")
    leaked = sorted(_FORBIDDEN_PARALLEL_KEYS & set(report))
    if leaked:
        raise ValueError(f"parallel machine representations are forbidden: {leaked}")
    model = report.pop("evolution_contract")
    if not isinstance(model, Mapping):
        raise ValueError("finalized extractor model must be an object")
    report["system_contract"] = build_system_contract(model)
'''


SYSTEM_CONTRACT_VALIDATION = '''\
"""Strict offline validation for the direct system contract."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .proof_schema_validation import load_schemas
from .system_contract import SCHEMA_ID, digest
from .system_contract_evidence import revalidate_source_bytes, relative_source_path, scan_secrets
from .validation import validate_evolution_contract

_FORBIDDEN_PARALLEL_KEYS = frozenset(
    {"config_schema", "config_surface_graph", "evolution_contract", "local_storage_schema"}
)


@lru_cache(maxsize=1)
def _schema_resources() -> tuple[dict[str, Any], Registry]:
    by_id, _ = load_schemas()
    registry = Registry().with_resources(
        (uri, Resource.from_contents(schema)) for uri, schema in by_id.items()
    )
    return by_id, registry


def _validator(identifier: str = SCHEMA_ID) -> Draft202012Validator:
    by_id, registry = _schema_resources()
    if identifier not in by_id:
        raise ValueError("fragment schema is not in the offline package bundle")
    return Draft202012Validator(by_id[identifier], registry=registry)


def _graph_references(graph: Mapping[str, Any]) -> None:
    nodes = graph["nodes"]
    if any(key != node["id"] for key, node in nodes.items()):
        raise ValueError("surface graph node key/id mismatch")
    edge_ids: set[str] = set()
    for edge in graph["edges"]:
        if edge["id"] in edge_ids:
            raise ValueError("duplicate surface graph edge id")
        edge_ids.add(edge["id"])
        if edge["source"] not in nodes or edge["target"] not in nodes:
            raise ValueError("unresolved surface graph edge")


def _references(model: Mapping[str, Any]) -> None:
    specs = model["source_registry"]["sources"]
    snapshot = model["source_snapshot"]
    files = snapshot["files"]
    unavailable = snapshot["unavailable_specs"]
    if snapshot["revision"] != model["source_revision"]:
        raise ValueError("source snapshot/revision disagreement")
    declared = set(specs)
    accounted = set(files) | set(unavailable)
    if set(files) & set(unavailable) or accounted - declared:
        raise ValueError("unresolved or contradictory source inventory")
    if snapshot["counts"] != {"available": len(files), "unavailable": len(unavailable)}:
        raise ValueError("source inventory count mismatch")
    for key, spec in specs.items():
        if key != spec["id"]:
            raise ValueError("source registry key/id mismatch")
    paths: set[str] = set()
    for key, record in files.items():
        path = record["path"]
        relative_source_path(path)
        if key != record["spec_id"] or path in paths:
            raise ValueError("source manifest identity/path collision")
        candidates = specs[key]["path_candidates"]
        index = record["path_candidate_index"]
        if index >= len(candidates) or candidates[index] != path:
            raise ValueError("source path is not the selected registry candidate")
        paths.add(path)

    extractors = model["extractors"]
    canonical_ids = model["migration"]["canonical_ir_extractors"]
    if sorted(extractors) != sorted(canonical_ids):
        raise ValueError("canonical extractor coverage mismatch")
    for key, entry in extractors.items():
        if key != entry["extractor_id"]:
            raise ValueError("extractor key/id mismatch")
        if set(entry["source_spec_ids"]) - declared:
            raise ValueError("extractor references undeclared source specs")
        data = entry["data"]
        if data.get("$schema"):
            errors = list(_validator(data["$schema"]).iter_errors(data))
            if not entry["semantic_complete"]:
                errors = [error for error in errors if error.validator != "required"]
            if errors:
                raise ValueError("extractor fragment schema violation: " + key)
        if "semantic_complete" in data and data["semantic_complete"] != entry["semantic_complete"]:
            raise ValueError("extractor completeness disagreement")
        if "source_revision" in data and data["source_revision"] != model["source_revision"]:
            raise ValueError("extractor source revision disagreement")
        if key == "extractor.local_storage":
            for evidence in data.get("evidence", {}).get("sources", []):
                source = files.get(evidence["source_spec_id"])
                names = ("path", "sha256", "git_blob_sha")
                if not source or any(evidence[name] != source[name] for name in names):
                    raise ValueError("local-storage evidence/manifest disagreement")
        graph = data.get("surface_graph")
        if graph is not None:
            _graph_references(graph)

    if model["status"]["overall"] == "complete":
        required = {key for key, spec in specs.items() if spec["required"]}
        missing = sorted(required - set(files))
        wrongly_unavailable = sorted(required & set(unavailable))
        if not required:
            raise ValueError("complete status requires a declared required-source inventory")
        if missing or wrongly_unavailable:
            raise ValueError(
                "complete status has missing required sources: "
                f"missing={missing}, unavailable={wrongly_unavailable}"
            )
        if not extractors or not all(entry["semantic_complete"] for entry in extractors.values()):
            raise ValueError("complete status contradicts extractor coverage")


def validate_system_contract(
    value: Mapping[str, Any],
    *,
    report: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Raise ``ValueError`` when any direct-contract invariant is false."""
    scan_secrets(value)
    errors = list(_validator().iter_errors(value))
    if errors:
        path = "/".join(map(str, errors[0].absolute_path))
        raise ValueError("canonical schema violation at " + path)
    model = value["model"]
    failures = validate_evolution_contract(model)
    if failures:
        raise ValueError(failures[0]["code"])
    _references(model)
    body = {key: child for key, child in value.items() if key != "integrity"}
    if value["integrity"]["canonical_sha256"] != digest(body):
        raise ValueError("canonical content digest mismatch")
    if report is not None:
        leaked = sorted(_FORBIDDEN_PARALLEL_KEYS & set(report))
        if leaked:
            raise ValueError(f"parallel machine representations are forbidden: {leaked}")
        if report.get("system_contract") != value:
            raise ValueError("report does not contain the validated system contract")
    checked = revalidate_source_bytes(model, source_root) if source_root is not None else None
    return {
        "schema": value["schema"],
        "authority": value["authority"],
        "canonical_sha256": value["integrity"]["canonical_sha256"],
        "extractors": sorted(model["extractors"]),
        "direct_report_checked": report is not None,
        "source_files_revalidated": checked,
        "source_bytes_status": "verified" if checked is not None else "not_checked",
    }
'''


DIRECT_TESTS = '''\
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
'''


CONTRACT_README = '''\
# `codex-system-contract/v1`

This directory defines the only machine-readable representation emitted for the
migrated Codex audit surface. The report contains exactly one
`system_contract`; its `model` owns source identity, source inventory,
extractor facts, semantic completeness, and graph relationships.

There are no aliases or duplicated report paths for configuration or local
storage. Consumers must use the versioned contract directly and reject a schema
version they do not understand. The implementation does not reconstruct an old
shape from the contract and does not reconstruct the contract from an old
shape.

`schema.json` is the JSON Schema. `example.fixture.json` is a deterministic
fixture. Runtime validation additionally checks canonical content integrity,
source-registry references, required-source completeness, graph references,
secret safety, and—when a source checkout is supplied—the recorded source bytes.
'''


REVIEW_DOC = '''\
# Canonical representation review

## Decision

The broad canonical-publication claim on the reviewed `main` tree was not
supported. This branch replaces the recovery-oriented proposal with one direct,
source-derived `codex-system-contract/v1` output.

## Authority boundary

The report publishes one `system_contract`. Its model is assembled from the
existing source registry, immutable source snapshot, and extractor results.
Configuration and local-storage facts live only in their extractor entries.
Top-level `config_schema`, `config_surface_graph`, `local_storage_schema`, and
`evolution_contract` outputs are forbidden.

The contract is intentionally breaking. No adapter, alias, fallback reader, or
secondary projection is installed. Consumers that need a different shape must
perform that transformation outside this package and own its validation.

## Evolution policy

A new surface enters the contract by adding source specifications, extractor
facts, a schema fragment, reference validation, and tests. Existing facts are
changed in place through a contract-version change when semantics break. The
package must never create a second authoritative representation to avoid a
version transition.

## Proof boundary

Permanent CI is read-only. It validates the committed tree on supported Python
versions, checks deterministic assets and maintainability, runs the complete
test suite, verifies source-byte evidence against the pinned Codex revision,
and independently builds and verifies release artifacts. Publication helpers
are temporary and are absent from the final candidate tree.
'''


class RemoveProjectionCode(ast.NodeTransformer):
    @staticmethod
    def _call_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                return node.func.id
            if isinstance(node.func, ast.Attribute):
                return node.func.attr
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module and node.module.endswith("compatibility_views"):
            return None
        return self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        kept = [alias for alias in node.names if not alias.name.endswith("compatibility_views")]
        if not kept:
            return None
        node.names = kept
        return node

    def visit_Expr(self, node: ast.Expr):
        if self._call_name(node.value) in COMPATIBILITY_CALLS:
            return None
        return self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        if self._call_name(node.value) in COMPATIBILITY_CALLS:
            return None
        return self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is not None and self._call_name(node.value) in COMPATIBILITY_CALLS:
            return None
        return self.generic_visit(node)


def ensure_nonempty_bodies(node: ast.AST) -> None:
    for child in ast.walk(node):
        for attribute in ("body", "orelse", "finalbody"):
            body = getattr(child, attribute, None)
            if attribute == "body" and isinstance(body, list) and not body and isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.AsyncFor,
                 ast.While, ast.With, ast.AsyncWith, ast.Try),
            ):
                body.append(ast.Pass())


def remove_projection_calls() -> None:
    for path in PACKAGE.rglob("*.py"):
        if path.name in {"system_contract.py", "system_contract_validation.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        if not any(token in source for token in ("compatibility_views", *COMPATIBILITY_CALLS)):
            continue
        tree = RemoveProjectionCode().visit(ast.parse(source, filename=str(path)))
        ensure_nonempty_bodies(tree)
        ast.fix_missing_locations(tree)
        compile(tree, str(path), "exec")
        write(path, ast.unparse(tree))


def remove_obsolete_projection_tests() -> None:
    for path in TESTS.glob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        if "compatib" not in source.lower():
            continue
        tree = ast.parse(source, filename=str(path))
        kept: list[ast.stmt] = []
        for node in tree.body:
            segment = ast.get_source_segment(source, node) or ""
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and (
                "compatib" in segment.lower()
            ):
                continue
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith(
                "compatibility_views"
            ):
                continue
            kept.append(node)
        tree.body = kept
        ensure_nonempty_bodies(tree)
        ast.fix_missing_locations(tree)
        compile(tree, str(path), "exec")
        write(path, ast.unparse(tree))


def scrub_schema(value: Any) -> None:
    if isinstance(value, dict):
        required = value.get("required")
        if isinstance(required, list):
            value["required"] = [item for item in required if item != "compatibility_views"]
        properties = value.get("properties")
        if isinstance(properties, dict):
            properties.pop("compatibility_views", None)
        definitions = value.get("$defs")
        if isinstance(definitions, dict):
            for key in list(definitions):
                if "compatib" in key.lower():
                    definitions.pop(key)
        for child in list(value.values()):
            scrub_schema(child)
    elif isinstance(value, list):
        for child in value:
            scrub_schema(child)


def update_contract_assets() -> None:
    for path in ROOT.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        changed = False
        if isinstance(value, dict) and (
            value.get("$id") == SCHEMA_ID
            or value.get("title", "").lower().startswith("codex system contract")
            or path.name == "system-contract.schema.json"
        ):
            before = json.dumps(value, sort_keys=True)
            scrub_schema(value)
            changed = json.dumps(value, sort_keys=True) != before
        if isinstance(value, dict) and value.get("schema") == FORMAT:
            if "compatibility_views" in value:
                value.pop("compatibility_views")
                changed = True
            body = {key: child for key, child in value.items() if key != "integrity"}
            value["integrity"] = {
                "algorithm": "sha256",
                "canonicalization": CANONICALIZATION,
                "canonical_sha256": digest(body),
            }
            changed = True
        if changed:
            write(path, json.dumps(value, indent=2, sort_keys=True))


def fix_github_base64() -> None:
    pattern = re.compile(
        r"base64\.b64decode\((?P<value>[^()\n]+?),\s*validate=True\)"
    )
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "base64.b64decode" not in source:
            continue
        if not any(marker in source for marker in ("api.github.com", "github", '["content"]', "['content']")):
            continue
        def replacement(match: re.Match[str]) -> str:
            value = match.group("value").strip()
            if ".split()" in value:
                return match.group(0)
            return f'base64.b64decode("".join(({value}).split()), validate=True)'
        updated = pattern.sub(replacement, source)
        if updated != source:
            write(path, updated)


def update_docs() -> None:
    write(ROOT / "contracts" / "codex-system-contract" / "v1" / "README.md", CONTRACT_README)
    write(ROOT / "CANONICAL_REVIEW.md", REVIEW_DOC)
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    old = '''- `report.json` contains extractor facts, diagnostics, coverage, and source provenance.
- `report.local_storage_schema` is the authoritative local storage/layout view;
  the same data remains available at
  `report.evolution_contract.extractors["extractor.local_storage"].data`.
- `config-schema.json` is the normalized, path-addressable configuration catalog.
- `config-surface-graph.json` connects config paths and feature policy to previously modeled protocol surfaces.
'''
    new = '''- `report.json` contains diagnostics plus one canonical `system_contract`.
- Local-storage facts live at
  `report.system_contract.model.extractors["extractor.local_storage"].data`.
- Configuration facts and their graph live at
  `report.system_contract.model.extractors["extractor.config_effects"].data`.
- `config-schema.json` and `config-surface-graph.json` are explicit standalone CLI
  exports; they are not duplicated inside the report under alternate paths.
'''
    if old in text:
        text = text.replace(old, new)
    text = text.replace(
        "The semantic source of truth is [`codex-system-contract/v1`](contracts/codex-system-contract/v1/README.md).\nLegacy configuration and local-storage documents are compatibility views; their\nmeaning is derived from the digest-bound canonical contract.",
        "The only machine source of truth for migrated surfaces is\n[`codex-system-contract/v1`](contracts/codex-system-contract/v1/README.md).\nThe report emits no alternate configuration or local-storage machine paths.",
    )
    write(readme, text)


def add_direct_tests() -> None:
    write(TESTS / "test_direct_system_contract.py", DIRECT_TESTS)


def static_guards() -> None:
    removed = PACKAGE / "compatibility_views.py"
    if removed.exists():
        raise SystemExit("projection module was not removed")
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "compatibility_views" in source or any(name in source for name in COMPATIBILITY_CALLS):
            raise SystemExit(f"projection implementation remains in {path}")
    schema = json.loads(
        (ROOT / "toolchain" / "codex_wire_audit_v11_schemas" / "system-contract.schema.json")
        .read_text(encoding="utf-8")
    )
    encoded = json.dumps(schema, sort_keys=True)
    if "compatibility_views" in encoded:
        raise SystemExit("system-contract schema still admits secondary views")
    fixture = json.loads(
        (ROOT / "contracts" / "codex-system-contract" / "v1" / "example.fixture.json")
        .read_text(encoding="utf-8")
    )
    if "compatibility_views" in fixture:
        raise SystemExit("contract fixture still contains secondary views")


def main() -> None:
    expected = {
        PACKAGE / "canonical.py": "aaea43ee41b43d433aa31583b5ad31917fbbc4e5",
        PACKAGE / "compatibility_views.py": "c770efa47329dc4055957df4f1b669b8ba621a1d",
        PACKAGE / "system_contract.py": "21dcf536fca4a7442cb860e25bb6b55e1966d081",
        PACKAGE / "system_contract_validation.py": "4e7ebeffa6561bd0b5193fc179c06fa7e4cc7a3c",
    }
    for path, blob in expected.items():
        actual = subprocess.check_output(["git", "hash-object", str(path)], text=True).strip()
        if actual != blob:
            raise SystemExit(f"unexpected reviewed base for {path}: {actual}")

    write(PACKAGE / "system_contract.py", SYSTEM_CONTRACT)
    write(PACKAGE / "system_contract_validation.py", SYSTEM_CONTRACT_VALIDATION)
    remove_projection_calls()
    (PACKAGE / "compatibility_views.py").unlink()
    remove_obsolete_projection_tests()
    fix_github_base64()
    update_contract_assets()
    update_docs()
    add_direct_tests()
    static_guards()

    print(json.dumps({
        "status": "direct-contract-migration-materialized",
        "removed_module": "codex_wire_audit.compatibility_views",
        "forbidden_report_keys": sorted(FORBIDDEN_REPORT_KEYS),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
