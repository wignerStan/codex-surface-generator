from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
from typing import Any

ROOT = Path.cwd()
PACKAGE = ROOT / "toolchain" / "codex_wire_audit"
TOOLS = ROOT / "toolchain" / "tools"
TESTS = ROOT / "toolchain" / "tests"
FORMAT = "codex-system-contract/v1"
SCHEMA_ID = "https://schemas.codex-wire-audit.invalid/system/v1/system-contract.schema.json"
CANONICALIZATION = "codex-wire-audit-canonical-json-v2"
REMOVED_MODULE = "compatibility" + "_views"
REMOVED_CALLS = {"apply_config_surface_overlay", "apply_local_storage_overlay"}
REPORT_NAMES = {"report", "payload", "output", "result", "generated", "first", "second"}
OLD_REPORT_KEYS = {"config_schema", "config_surface_graph", "local_storage_schema"}


def write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


SYSTEM_CONTRACT = '''\
"""The single source-derived machine representation of the audited system.

The finalized extractor model is sealed once under ``system_contract``. This
module provides no aliases, projections, mirrors, or fallback read paths. A
consumer either understands this versioned contract or rejects it.
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
    """Seal one finalized source-derived model without a secondary shape."""
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
    """Consume the construction model and expose only the versioned contract."""
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


VALIDATION = '''\
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
                fields = ("path", "sha256", "git_blob_sha")
                if not source or any(evidence[name] != source[name] for name in fields):
                    raise ValueError("local-storage evidence/manifest disagreement")
        graph = data.get("surface_graph")
        if graph is not None:
            _graph_references(graph)

    if model["status"]["overall"] == "complete":
        required = {key for key, spec in specs.items() if spec["required"]}
        missing = sorted(required - set(files))
        unavailable_required = sorted(required & set(unavailable))
        if not required:
            raise ValueError("complete status requires a declared required-source inventory")
        if missing or unavailable_required:
            raise ValueError(
                "complete status has missing required sources: "
                f"missing={missing}, unavailable={unavailable_required}"
            )
        if not extractors or not all(entry["semantic_complete"] for entry in extractors.values()):
            raise ValueError("complete status contradicts extractor coverage")


def validate_system_contract(
    value: Mapping[str, Any],
    *,
    report: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Raise ``ValueError`` when a direct-contract invariant is false."""
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


GITHUB_CONTENT = '''\
"""Strict decoding for line-wrapped GitHub Contents API payloads."""

from __future__ import annotations

import base64


def decode_github_content(encoded: str) -> bytes:
    """Decode standard GitHub base64 after removing transport whitespace."""
    if not isinstance(encoded, str):
        raise TypeError("GitHub content must be a string")
    return base64.b64decode("".join(encoded.split()), validate=True)
'''


DIRECT_TEST = '''\
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from codex_wire_audit.system_contract import attach_system_contract, build_system_contract, digest
from codex_wire_audit.system_contract_validation import validate_system_contract

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "contracts" / "codex-system-contract" / "v1" / "example.fixture.json"
PARALLEL = {"config_schema", "config_surface_graph", "evolution_contract", "local_storage_schema"}


def model() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["model"]


def reseal(contract: dict) -> None:
    body = {key: value for key, value in contract.items() if key != "integrity"}
    contract["integrity"]["canonical_sha256"] = digest(body)


def test_contract_has_one_public_machine_shape() -> None:
    contract = build_system_contract(model())
    assert set(contract) == {"$schema", "schema", "authority", "model", "integrity"}
    validate_system_contract(contract)


def test_attach_consumes_the_private_construction_model() -> None:
    report = {"evolution_contract": model(), "diagnostics": []}
    attach_system_contract(report)
    assert set(report) == {"system_contract", "diagnostics"}
    assert not (PARALLEL & set(report))
    assert validate_system_contract(report["system_contract"], report=report)["direct_report_checked"]


@pytest.mark.parametrize("key", sorted(PARALLEL - {"evolution_contract"}))
def test_parallel_machine_paths_are_rejected(key: str) -> None:
    with pytest.raises(ValueError, match="parallel machine representations"):
        attach_system_contract({"evolution_contract": model(), key: {}})


def test_removed_adapter_module_is_not_importable() -> None:
    removed = "codex_wire_audit." + "compatibility" + "_views"
    assert importlib.util.find_spec(removed) is None


def test_complete_status_requires_all_required_sources() -> None:
    contract = build_system_contract(model())
    candidate = contract["model"]
    candidate["status"]["overall"] = "complete"
    required = [key for key, spec in candidate["source_registry"]["sources"].items() if spec["required"]]
    assert required
    victim = required[0]
    candidate["source_snapshot"]["files"].pop(victim, None)
    unavailable = candidate["source_snapshot"]["unavailable_specs"]
    if victim not in unavailable:
        unavailable.append(victim)
    candidate["source_snapshot"]["counts"] = {
        "available": len(candidate["source_snapshot"]["files"]),
        "unavailable": len(unavailable),
    }
    reseal(contract)
    with pytest.raises(ValueError, match="missing required sources"):
        validate_system_contract(contract)
'''


GITHUB_TEST = '''\
from __future__ import annotations

import base64

import pytest

from codex_wire_audit.github_content import decode_github_content


def test_line_wrapped_github_content_decodes_strictly() -> None:
    raw = b"canonical source bytes" * 20
    encoded = base64.b64encode(raw).decode("ascii")
    wrapped = "\n".join(encoded[index:index + 60] for index in range(0, len(encoded), 60))
    assert decode_github_content(wrapped) == raw


def test_invalid_github_content_is_rejected() -> None:
    with pytest.raises(ValueError):
        decode_github_content("not base64 !!!")
'''


class ScrubRemovedCode(ast.NodeTransformer):
    @staticmethod
    def call_name(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if node.module and node.module.endswith(REMOVED_MODULE):
            return None
        return self.generic_visit(node)

    def visit_Import(self, node: ast.Import):
        node.names = [alias for alias in node.names if not alias.name.endswith(REMOVED_MODULE)]
        return node if node.names else None

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if "compatib" in node.name.lower():
            return None
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        if "compatib" in node.name.lower():
            return None
        return self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr):
        if self.call_name(node.value) in REMOVED_CALLS:
            return None
        return self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign):
        if self.call_name(node.value) in REMOVED_CALLS:
            return None
        if any(isinstance(target, ast.Name) and "compatib" in target.id.lower() for target in node.targets):
            return None
        return self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if isinstance(node.target, ast.Name) and "compatib" in node.target.id.lower():
            return None
        return self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict):
        node = self.generic_visit(node)
        keys: list[ast.expr | None] = []
        values: list[ast.expr] = []
        for key, value in zip(node.keys, node.values):
            key_text = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else ""
            value_text = value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else ""
            if REMOVED_MODULE in key_text or REMOVED_MODULE in value_text:
                continue
            if any(name in key_text or name in value_text for name in REMOVED_CALLS):
                continue
            keys.append(key)
            values.append(value)
        node.keys = keys
        node.values = values
        return node

    def _sequence(self, node: ast.List | ast.Tuple | ast.Set):
        node = self.generic_visit(node)
        node.elts = [
            item for item in node.elts
            if not (
                isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and (REMOVED_MODULE in item.value or any(name in item.value for name in REMOVED_CALLS))
            )
        ]
        return node

    visit_List = _sequence
    visit_Tuple = _sequence
    visit_Set = _sequence


class GitHubDecodeTransformer(ast.NodeTransformer):
    def __init__(self) -> None:
        self.changed = False

    def visit_Call(self, node: ast.Call):
        node = self.generic_visit(node)
        if not (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "b64decode"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "base64"
            and node.args
        ):
            return node
        strict = any(
            keyword.arg == "validate"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in node.keywords
        )
        if not strict:
            return node
        self.changed = True
        return ast.copy_location(
            ast.Call(func=ast.Name(id="decode_github_content", ctx=ast.Load()), args=[node.args[0]], keywords=[]),
            node,
        )


def ensure_bodies(tree: ast.AST) -> None:
    owners = (
        ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For,
        ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
    )
    for node in ast.walk(tree):
        if isinstance(node, owners) and hasattr(node, "body") and not node.body:
            node.body = [ast.Pass()]


def unparse(path: Path, tree: ast.AST) -> None:
    ensure_bodies(tree)
    ast.fix_missing_locations(tree)
    compile(tree, str(path), "exec")
    write(path, ast.unparse(tree))


def scrub_active_python() -> None:
    for root in (PACKAGE, TOOLS):
        for path in root.rglob("*.py"):
            if path.name in {"system_contract.py", "system_contract_validation.py"}:
                continue
            source = path.read_text(encoding="utf-8")
            if not (
                REMOVED_MODULE in source
                or any(name in source for name in REMOVED_CALLS)
                or re.search(r"\bcompatib\w*\s*=", source)
            ):
                continue
            tree = ScrubRemovedCode().visit(ast.parse(source, filename=str(path)))
            unparse(path, tree)


def root_name(value: ast.AST) -> str | None:
    while isinstance(value, (ast.Subscript, ast.Attribute)):
        value = value.value
    return value.id if isinstance(value, ast.Name) else None


def obsolete_test_node(source: str, node: ast.AST) -> bool:
    segment = ast.get_source_segment(source, node) or ""
    if REMOVED_MODULE in segment or any(name in segment for name in REMOVED_CALLS):
        return True
    patterns = [
        r"\b(?:report|payload|output|result|generated)\s*\[\s*['\"](?:config_schema|config_surface_graph|local_storage_schema)['\"]",
        r"\b(?:report|payload|output|result|generated)\.get\(\s*['\"](?:config_schema|config_surface_graph|local_storage_schema)['\"]",
    ]
    return any(re.search(pattern, segment) for pattern in patterns)


class RetargetReportModelReads(ast.NodeTransformer):
    def visit_Subscript(self, node: ast.Subscript):
        node = self.generic_visit(node)
        if (
            isinstance(node.slice, ast.Constant)
            and node.slice.value == "evolution_contract"
            and root_name(node.value) in REPORT_NAMES
        ):
            contract = ast.Subscript(value=node.value, slice=ast.Constant("system_contract"), ctx=ast.Load())
            return ast.copy_location(
                ast.Subscript(value=contract, slice=ast.Constant("model"), ctx=node.ctx), node
            )
        return node

    def visit_Call(self, node: ast.Call):
        node = self.generic_visit(node)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and root_name(node.func.value) in REPORT_NAMES
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "evolution_contract"
        ):
            node.args[0] = ast.Constant("system_contract")
            return ast.copy_location(
                ast.Subscript(value=node, slice=ast.Constant("model"), ctx=ast.Load()), node
            )
        return node


def update_tests() -> None:
    for path in TESTS.glob("test_*.py"):
        if path.name in {"test_direct_system_contract.py", "test_github_content.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        body: list[ast.stmt] = []
        changed = False
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and obsolete_test_node(source, node):
                changed = True
                continue
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith(REMOVED_MODULE):
                changed = True
                continue
            body.append(node)
        tree.body = body
        transformed = RetargetReportModelReads().visit(tree)
        if changed or "evolution_contract" in source:
            unparse(path, transformed)
    write(TESTS / "test_direct_system_contract.py", DIRECT_TEST)
    write(TESTS / "test_github_content.py", GITHUB_TEST)


def prune_removed(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            label = str(key)
            if REMOVED_MODULE in label or any(name in label for name in REMOVED_CALLS):
                continue
            encoded = json.dumps(child, sort_keys=True) if isinstance(child, (dict, list)) else str(child)
            if REMOVED_MODULE in encoded or any(name in encoded for name in REMOVED_CALLS):
                continue
            result[key] = prune_removed(child)
        required = result.get("required")
        if isinstance(required, list):
            result["required"] = [item for item in required if item != REMOVED_MODULE]
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            encoded = json.dumps(child, sort_keys=True) if isinstance(child, (dict, list)) else str(child)
            if REMOVED_MODULE in encoded or any(name in encoded for name in REMOVED_CALLS):
                continue
            result.append(prune_removed(child))
        return result
    return value


def update_json_assets() -> None:
    for path in ROOT.rglob("*.json"):
        if ".git" in path.parts or "release" in path.parts:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        cleaned = prune_removed(value)
        changed = cleaned != value
        if isinstance(cleaned, dict) and cleaned.get("schema") == FORMAT:
            body = {key: child for key, child in cleaned.items() if key != "integrity"}
            cleaned["integrity"] = {
                "algorithm": "sha256",
                "canonicalization": CANONICALIZATION,
                "canonical_sha256": digest(body),
            }
            changed = True
        if changed:
            write(path, json.dumps(cleaned, indent=2, sort_keys=True))


def install_github_decoder() -> None:
    write(PACKAGE / "github_content.py", GITHUB_CONTENT)
    changed_paths: list[Path] = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "github_content.py":
            continue
        source = path.read_text(encoding="utf-8")
        if "base64.b64decode" not in source or "github" not in source.lower():
            continue
        tree = ast.parse(source, filename=str(path))
        transformer = GitHubDecodeTransformer()
        tree = transformer.visit(tree)
        if not transformer.changed:
            continue
        depth = len(path.relative_to(PACKAGE).parts) - 1
        import_node = ast.ImportFrom(
            module="github_content",
            names=[ast.alias(name="decode_github_content")],
            level=depth + 1,
        )
        insert_at = 1 if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant) else 0
        while insert_at < len(tree.body) and isinstance(tree.body[insert_at], ast.ImportFrom) and tree.body[insert_at].module == "__future__":
            insert_at += 1
        tree.body.insert(insert_at, import_node)
        unparse(path, tree)
        changed_paths.append(path)
    if not changed_paths:
        raise SystemExit("GitHub line-wrapped base64 decoder call was not found")


def update_docs() -> None:
    write(
        ROOT / "contracts" / "codex-system-contract" / "v1" / "README.md",
        '''# `codex-system-contract/v1`

The report emits exactly one machine representation for migrated Codex audit
semantics: `system_contract`. Its `model` owns source identity, source
inventory, extractor facts, completeness state, and graph relationships.

No configuration or local-storage aliases are emitted. Consumers use this
versioned contract directly and reject versions they do not understand. Schema
validation is supplemented by canonical-content integrity, source/reference
checks, required-source completeness, graph checks, secret safety, and optional
revalidation of every recorded source byte against a checkout.
''',
    )
    write(
        ROOT / "CANONICAL_REVIEW.md",
        '''# Canonical representation review

The reviewed `main` tree did not contain the claimed complete canonical
publication. This change establishes a bounded, source-derived
`codex-system-contract/v1` as the only machine representation emitted for the
migrated surfaces.

The report contains `system_contract` and no alternate configuration,
local-storage, graph, or construction-model paths. The API is deliberately
breaking: the package installs no adapter, alias, fallback reader, or duplicate
projection. New semantics enter through source specifications, extractor facts,
schema fragments, reference checks, and tests. A breaking semantic change
requires a contract-version change rather than a second representation.

Permanent CI is read-only and validates the exact committed tree across the
supported Python matrix, deterministic assets, maintainability, proof-critical
coverage, the pinned Codex checkout and source bytes, and an independently
rebuilt wheel/sdist closure.
''',
    )
    readme = ROOT / "README.md"
    text = readme.read_text(encoding="utf-8")
    text = re.sub(
        r"- `report\.json` contains extractor facts, diagnostics, coverage, and source provenance\.\n"
        r"- `report\.local_storage_schema`.*?"
        r"(?=- upstream app-server protocol JSON schemas)",
        "- `report.json` contains diagnostics plus one canonical `system_contract`.\n"
        "- Local-storage facts live at `report.system_contract.model.extractors[\"extractor.local_storage\"].data`.\n"
        "- Configuration facts and their graph live at `report.system_contract.model.extractors[\"extractor.config_effects\"].data`.\n"
        "- Standalone config/schema files are explicit CLI exports, not alternate report paths.\n",
        text,
        flags=re.S,
    )
    text = re.sub(
        r"## Canonical machine-readable contract\n.*?(?=\n## |\Z)",
        "## Canonical machine-readable contract\n\n"
        "[`codex-system-contract/v1`](contracts/codex-system-contract/v1/README.md) "
        "is the only machine representation emitted for migrated surfaces. The report "
        "contains no alternate configuration or local-storage paths.\n",
        text,
        flags=re.S,
    )
    write(readme, text)


def static_validation() -> None:
    removed = PACKAGE / f"{REMOVED_MODULE}.py"
    if removed.exists():
        raise SystemExit("removed adapter module still exists")
    for root in (PACKAGE, TOOLS):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json"}:
                continue
            source = path.read_text(encoding="utf-8")
            if REMOVED_MODULE in source or any(name in source for name in REMOVED_CALLS):
                raise SystemExit(f"removed adapter reference remains in {path}")
    schema = json.loads(
        (ROOT / "toolchain" / "codex_wire_audit_v11_schemas" / "system-contract.schema.json")
        .read_text(encoding="utf-8")
    )
    if REMOVED_MODULE in json.dumps(schema, sort_keys=True):
        raise SystemExit("contract schema admits a secondary representation")
    fixture = json.loads(
        (ROOT / "contracts" / "codex-system-contract" / "v1" / "example.fixture.json")
        .read_text(encoding="utf-8")
    )
    if set(fixture) != {"$schema", "schema", "authority", "model", "integrity"}:
        raise SystemExit(f"unexpected contract keys: {sorted(fixture)}")


def main() -> None:
    write(PACKAGE / "system_contract.py", SYSTEM_CONTRACT)
    write(PACKAGE / "system_contract_validation.py", VALIDATION)
    scrub_active_python()
    (PACKAGE / f"{REMOVED_MODULE}.py").unlink(missing_ok=True)
    update_tests()
    update_json_assets()
    install_github_decoder()
    update_docs()
    static_validation()
    print(json.dumps({"status": "direct-contract-v3-materialized"}, sort_keys=True))


if __name__ == "__main__":
    main()
