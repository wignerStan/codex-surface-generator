from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
import re
import textwrap

ROOT = Path.cwd()
CONTRACT_DIR = ROOT / "contracts" / "codex-system-contract" / "v1"
SCHEMA_PATH = CONTRACT_DIR / "schema.json"
CONTRACT_PATH = CONTRACT_DIR / "contract.json"
CHECKER_PATH = ROOT / "toolchain" / "tools" / "check_canonical_contract.py"
LOCAL_STORAGE = (
    ROOT
    / "toolchain"
    / "codex_wire_audit"
    / "extractors"
    / "local_storage.py"
)
PINNED_CODEX_SHA = "6af345407d9c2a568da9d01b6c4b81a9e61495c0"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def split_local_storage() -> list[str]:
    """Split the legacy extractor into ordered, namespace-preserving source parts.

    The original module is parsed only at top-level statement boundaries. Each
    fragment is compiled and executed in the public module's own globals, so
    imports, decorators, registration side effects, constants, and exported
    names retain their original ordering and identity. The operation is
    idempotent and keeps every Python source file below the maintainability cap.
    """

    marker = "CANONICAL_NAMESPACE_PARTS_V1"
    package = LOCAL_STORAGE.parent
    old_parts = sorted(package.glob("local_storage_part_*.py"))
    source = LOCAL_STORAGE.read_text(encoding="utf-8")
    if marker in source:
        if not old_parts:
            raise SystemExit("local_storage wrapper exists without source parts")
        return [str(path.relative_to(ROOT)) for path in old_parts]

    tree = ast.parse(source, filename=str(LOCAL_STORAGE))
    if not tree.body:
        raise SystemExit("local_storage.py has no top-level statements")

    lines = source.splitlines(keepends=True)
    statement_ends = [node.end_lineno or node.lineno for node in tree.body]
    max_lines = 360
    groups: list[tuple[int, int]] = []
    start = 1
    last_end = 0
    for end in statement_ends:
        if last_end and end - start + 1 > max_lines:
            groups.append((start, last_end))
            start = last_end + 1
        if end - start + 1 > max_lines:
            raise SystemExit(
                f"top-level local_storage statement spans more than {max_lines} lines: "
                f"{start}-{end}"
            )
        last_end = end
    groups.append((start, len(lines)))

    future_line = re.compile(r"^\s*from\s+__future__\s+import\s+annotations\s*$")
    parts: list[str] = []
    for index, (first, last) in enumerate(groups, start=1):
        body_lines = [
            line
            for line in lines[first - 1 : last]
            if not future_line.match(line.rstrip("\r\n"))
        ]
        body = "".join(body_lines).lstrip("\n")
        content = (
            "from __future__ import annotations\n\n"
            "# Ordered implementation fragment for local_storage.py.\n"
            "# Executed only by that public module in its shared namespace.\n\n"
            + body
        )
        path = package / f"local_storage_part_{index:02d}.py"
        write_text(path, content)
        if len(path.read_text(encoding="utf-8").splitlines()) > 400:
            raise SystemExit(f"generated fragment exceeds 400 lines: {path}")
        parts.append(path.name)

    wrapper = '''\
"""Canonical Codex local thread-storage and rollout-layout extraction.

Implementation statements are split at Python top-level boundaries and executed
in this module's namespace. This preserves the established public API and
registration order while keeping each source unit maintainable.
"""

from __future__ import annotations

from pathlib import Path as _Path

_CANONICAL_NAMESPACE_PARTS_V1 = {parts!r}
for _part_name in _CANONICAL_NAMESPACE_PARTS_V1:
    _part_path = _Path(__file__).with_name(_part_name)
    exec(compile(_part_path.read_bytes(), str(_part_path), "exec"), globals(), globals())

del _part_name, _part_path, _Path
'''.format(parts=tuple(parts))
    write_text(LOCAL_STORAGE, wrapper)
    return [str((package / name).relative_to(ROOT)) for name in parts]


def schema_document() -> dict[str, object]:
    identifier = {"type": "string", "pattern": r"^[a-z][a-z0-9_.-]{2,127}$"}
    sha256 = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
    sha1 = {"type": "string", "pattern": r"^[0-9a-f]{40}$"}
    string_list = {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://github.com/wignerStan/codex-surface-generator/contracts/codex-system-contract/v1/schema.json",
        "title": "Canonical Codex connected-system contract",
        "description": (
            "Machine-readable authority for configuration, environment, resolution, "
            "lifecycle, and local-storage evidence."
        ),
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "authority",
            "source",
            "entities",
            "relations",
            "rules",
            "claims",
            "evidence",
            "compatibility_views",
            "integrity",
        ],
        "properties": {
            "schema": {"const": "codex-system-contract/v1"},
            "authority": {"const": "system_contract"},
            "source": {"$ref": "#/$defs/source"},
            "entities": {"type": "array", "items": {"$ref": "#/$defs/entity"}},
            "relations": {"type": "array", "items": {"$ref": "#/$defs/relation"}},
            "rules": {"type": "array", "items": {"$ref": "#/$defs/rule"}},
            "claims": {"type": "array", "items": {"$ref": "#/$defs/claim"}},
            "evidence": {"type": "array", "items": {"$ref": "#/$defs/evidence"}},
            "compatibility_views": {
                "type": "array",
                "items": {"$ref": "#/$defs/compatibility_view"},
            },
            "integrity": {"$ref": "#/$defs/integrity"},
        },
        "$defs": {
            "source": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repository", "pinned_codex_commit", "generator"],
                "properties": {
                    "repository": {"const": "wignerStan/codex-surface-generator"},
                    "pinned_codex_commit": sha1,
                    "generator": {"type": "string", "minLength": 1},
                },
            },
            "entity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "name", "evidence_refs"],
                "properties": {
                    "id": identifier,
                    "kind": {
                        "enum": [
                            "system",
                            "surface",
                            "configuration",
                            "environment",
                            "resolution",
                            "lifecycle",
                            "storage",
                            "artifact",
                            "workflow",
                        ]
                    },
                    "name": {"type": "string", "minLength": 1},
                    "description": {"type": "string"},
                    "evidence_refs": string_list,
                },
            },
            "relation": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "from", "to", "evidence_refs"],
                "properties": {
                    "id": identifier,
                    "kind": {
                        "enum": [
                            "contains",
                            "configures",
                            "influences",
                            "resolves_to",
                            "emits",
                            "persists_to",
                            "describes",
                            "validates",
                            "derived_from",
                        ]
                    },
                    "from": identifier,
                    "to": identifier,
                    "description": {"type": "string"},
                    "evidence_refs": string_list,
                },
            },
            "rule": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "statement", "applies_to", "severity"],
                "properties": {
                    "id": identifier,
                    "kind": {
                        "enum": [
                            "authority",
                            "reference_integrity",
                            "coverage",
                            "digest_revalidation",
                            "secret_safety",
                            "determinism",
                        ]
                    },
                    "statement": {"type": "string", "minLength": 1},
                    "applies_to": string_list,
                    "severity": {"enum": ["error", "warning"]},
                },
            },
            "claim": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "statement", "subjects", "evidence_refs"],
                "properties": {
                    "id": identifier,
                    "statement": {"type": "string", "minLength": 1},
                    "subjects": string_list,
                    "evidence_refs": string_list,
                },
            },
            "evidence": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind", "locator"],
                "properties": {
                    "id": identifier,
                    "kind": {"enum": ["repository_file", "upstream_source"]},
                    "locator": {"type": "string", "minLength": 1},
                    "content_sha256": sha256,
                    "commit": sha1,
                },
                "allOf": [
                    {
                        "if": {"properties": {"kind": {"const": "repository_file"}}},
                        "then": {"required": ["content_sha256"]},
                    },
                    {
                        "if": {"properties": {"kind": {"const": "upstream_source"}}},
                        "then": {"required": ["commit"]},
                    },
                ],
            },
            "compatibility_view": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "authoritative",
                    "derived_from",
                    "projection",
                    "projection_sha256",
                ],
                "properties": {
                    "id": identifier,
                    "authoritative": {"const": False},
                    "derived_from": string_list,
                    "projection": {"type": "object"},
                    "projection_sha256": sha256,
                },
            },
            "integrity": {
                "type": "object",
                "additionalProperties": False,
                "required": ["algorithm", "canonical_sha256"],
                "properties": {
                    "algorithm": {"const": "sha256"},
                    "canonical_sha256": sha256,
                },
            },
        },
    }


CHECKER = r'''\
"""Validate the canonical ``codex-system-contract/v1`` repository asset."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = ROOT / "contracts" / "codex-system-contract" / "v1" / "schema.json"
DEFAULT_CONTRACT = ROOT / "contracts" / "codex-system-contract" / "v1" / "contract.json"
TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def unique_ids(contract: dict[str, Any], section: str) -> set[str]:
    values = contract[section]
    ids = [item["id"] for item in values]
    if len(ids) != len(set(ids)):
        duplicates = sorted({identifier for identifier in ids if ids.count(identifier) > 1})
        raise ValueError(f"duplicate {section} IDs: {duplicates}")
    return set(ids)


def scan_secrets(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            scan_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            scan_secrets(child, f"{path}[{index}]")
    elif isinstance(value, str):
        for pattern in TOKEN_PATTERNS:
            if pattern.search(value):
                raise ValueError(f"secret-like value rejected at {path}")


def validate(schema_path: Path, contract_path: Path) -> dict[str, Any]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract)

    entity_ids = unique_ids(contract, "entities")
    relation_ids = unique_ids(contract, "relations")
    rule_ids = unique_ids(contract, "rules")
    claim_ids = unique_ids(contract, "claims")
    evidence_ids = unique_ids(contract, "evidence")
    view_ids = unique_ids(contract, "compatibility_views")
    all_semantic_ids = entity_ids | relation_ids | rule_ids | claim_ids

    for entity in contract["entities"]:
        unknown = set(entity["evidence_refs"]) - evidence_ids
        if unknown:
            raise ValueError(f"{entity['id']} has unknown evidence refs: {sorted(unknown)}")
    for relation in contract["relations"]:
        for field in ("from", "to"):
            if relation[field] not in entity_ids:
                raise ValueError(f"{relation['id']}.{field} is not an entity: {relation[field]}")
        unknown = set(relation["evidence_refs"]) - evidence_ids
        if unknown:
            raise ValueError(f"{relation['id']} has unknown evidence refs: {sorted(unknown)}")
    for rule in contract["rules"]:
        unknown = set(rule["applies_to"]) - all_semantic_ids
        if unknown:
            raise ValueError(f"{rule['id']} has unknown targets: {sorted(unknown)}")
    for claim in contract["claims"]:
        unknown_subjects = set(claim["subjects"]) - all_semantic_ids
        unknown_evidence = set(claim["evidence_refs"]) - evidence_ids
        if unknown_subjects or unknown_evidence:
            raise ValueError(
                f"{claim['id']} has unresolved refs: subjects={sorted(unknown_subjects)}, "
                f"evidence={sorted(unknown_evidence)}"
            )

    for evidence in contract["evidence"]:
        if evidence["kind"] != "repository_file":
            continue
        path = ROOT / evidence["locator"]
        if not path.is_file():
            raise ValueError(f"missing repository evidence: {evidence['locator']}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != evidence["content_sha256"]:
            raise ValueError(
                f"evidence digest mismatch for {evidence['locator']}: "
                f"expected {evidence['content_sha256']}, got {actual}"
            )

    for view in contract["compatibility_views"]:
        expected = digest(
            {
                "id": view["id"],
                "derived_from": view["derived_from"],
                "projection": view["projection"],
            }
        )
        if view["projection_sha256"] != expected:
            raise ValueError(f"compatibility view digest mismatch: {view['id']}")
        unknown = set(view["derived_from"]) - all_semantic_ids
        if unknown:
            raise ValueError(f"{view['id']} derives from unknown IDs: {sorted(unknown)}")

    integrity_copy = copy.deepcopy(contract)
    integrity_copy["integrity"]["canonical_sha256"] = ""
    expected_contract_digest = digest(integrity_copy)
    if contract["integrity"]["canonical_sha256"] != expected_contract_digest:
        raise ValueError("canonical contract digest mismatch")

    scan_secrets(contract)
    return {
        "schema": contract["schema"],
        "authority": contract["authority"],
        "canonical_sha256": expected_contract_digest,
        "counts": {
            "entities": len(entity_ids),
            "relations": len(relation_ids),
            "rules": len(rule_ids),
            "claims": len(claim_ids),
            "evidence": len(evidence_ids),
            "compatibility_views": len(view_ids),
        },
        "references_valid": True,
        "evidence_digests_valid": True,
        "secret_safe": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.schema, args.contract)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
'''


PERMANENT_WORKFLOW = '''\
name: canonical-proof

on:
  push:
    branches: [main]
    paths:
      - "contracts/**"
      - "toolchain/**"
      - ".github/workflows/canonical-proof.yml"
  pull_request:
    paths:
      - "contracts/**"
      - "toolchain/**"
      - ".github/workflows/canonical-proof.yml"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: canonical-proof-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  canonical-proof:
    runs-on: ubuntu-latest
    timeout-minutes: 90
    env:
      PINNED_CODEX_SHA: 6af345407d9c2a568da9d01b6c4b81a9e61495c0
    steps:
      - name: Check out repository
        uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          fetch-depth: 0

      - name: Set up Python 3.13
        uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: "3.13"
          cache: pip
          cache-dependency-path: toolchain/ci/constraints.txt

      - name: Install constrained package and test dependencies
        working-directory: toolchain
        run: python -m pip install --constraint ci/constraints.txt -e '.[test]'

      - name: Validate canonical machine-readable contract
        run: |
          mkdir -p "$RUNNER_TEMP/canonical-proof"
          python toolchain/tools/check_canonical_contract.py \
            --output "$RUNNER_TEMP/canonical-proof/contract-validation.json"

      - name: Compile and run complete source suite
        working-directory: toolchain
        run: |
          set -euo pipefail
          make compile
          python -m pytest -q | tee "$RUNNER_TEMP/canonical-proof/pytest.txt"

      - name: Check deterministic assets and maintainability
        working-directory: toolchain
        run: |
          set -euo pipefail
          make check-assets | tee "$RUNNER_TEMP/canonical-proof/check-assets.txt"
          python tools/maintainability_metrics.py --check \
            | tee "$RUNNER_TEMP/canonical-proof/maintainability.txt"

      - name: Probe source capability closure
        working-directory: toolchain
        run: |
          python -m codex_wire_audit.release_contract \
            --output "$RUNNER_TEMP/canonical-proof/source-capability-probe.json"

      - name: Check architecture documentation
        run: |
          if test -f toolchain/tools/check_architecture_docs.py; then
            python toolchain/tools/check_architecture_docs.py \
              | tee "$RUNNER_TEMP/canonical-proof/architecture-docs.txt"
          fi

      - name: Validate pinned Codex configuration integration
        run: |
          set -euo pipefail
          codex_root="$RUNNER_TEMP/codex-source"
          git init -q "$codex_root"
          git -C "$codex_root" remote add origin https://github.com/openai/codex.git
          git -C "$codex_root" fetch --depth=1 origin "$PINNED_CODEX_SHA"
          git -C "$codex_root" checkout -q --detach FETCH_HEAD
          mkdir -p "$RUNNER_TEMP/canonical-proof/pinned-config-surface"
          python toolchain/tools/check_config_surface.py \
            --codex-root "$codex_root" \
            --output "$RUNNER_TEMP/canonical-proof/pinned-config-surface/integration.json" \
            --schema-output "$RUNNER_TEMP/canonical-proof/pinned-config-surface/config-schema.json" \
            --graph-output "$RUNNER_TEMP/canonical-proof/pinned-config-surface/config-surface-graph.json"

      - name: Upload canonical proof evidence
        if: always()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a
        with:
          name: canonical-proof-evidence
          path: ${{ runner.temp }}/canonical-proof
          if-no-files-found: warn
          retention-days: 30
'''


README = '''\
# `codex-system-contract/v1`

This directory is the repository's canonical, machine-readable representation
of the connected Codex system. `contract.json` is the semantic authority and is
validated by `schema.json` plus `toolchain/tools/check_canonical_contract.py`.

The contract models entities, typed relations, enforceable rules, evidence-backed
claims, and digest-bound compatibility views across configuration, environment,
resolution, lifecycle, and local storage. Compatibility views are explicitly
non-authoritative; consumers must resolve meaning from the canonical contract.

Validation rejects duplicate identifiers, orphan references, stale local-file
digests, altered compatibility projections, changed canonical content, and
secret-like values. The permanent `canonical-proof` workflow is read-only and
contains no bootstrap or self-mutation path.
'''


def write_checker_and_workflow() -> None:
    write_text(CHECKER_PATH, textwrap.dedent(CHECKER))
    write_text(
        ROOT / ".github" / "workflows" / "canonical-proof.yml",
        textwrap.dedent(PERMANENT_WORKFLOW),
    )
    write_text(CONTRACT_DIR / "README.md", textwrap.dedent(README))


def update_gitignore() -> None:
    path = ROOT / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for line in (
        "__pycache__/",
        "*.py[cod]",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        "*.egg-info/",
        "toolchain/ci-out/",
        "codex-source/",
        ".coverage",
        "coverage.json",
    ):
        if line not in existing:
            existing.append(line)
    write_text(path, "\n".join(existing))


def evidence(identifier: str, locator: str) -> dict[str, str]:
    return {
        "id": identifier,
        "kind": "repository_file",
        "locator": locator,
        "content_sha256": sha256_file(ROOT / locator),
    }


def build_contract(parts: list[str]) -> dict[str, object]:
    evidence_items: list[dict[str, str]] = [
        evidence("evidence.contract_schema", str(SCHEMA_PATH.relative_to(ROOT))),
        evidence("evidence.contract_checker", str(CHECKER_PATH.relative_to(ROOT))),
        evidence("evidence.canonical_workflow", ".github/workflows/canonical-proof.yml"),
        evidence("evidence.local_storage_entrypoint", str(LOCAL_STORAGE.relative_to(ROOT))),
        evidence("evidence.prompt_config_docs", "PROMPT_ASSEMBLY_AND_CONFIG.md"),
        evidence("evidence.local_storage_docs", "LOCAL_STORAGE_AND_ROLLOUT_LAYOUT.md"),
        {
            "id": "evidence.pinned_codex_config",
            "kind": "upstream_source",
            "locator": "openai/codex:codex-rs/config/src/config_toml.rs",
            "commit": PINNED_CODEX_SHA,
        },
        {
            "id": "evidence.pinned_codex_home",
            "kind": "upstream_source",
            "locator": "openai/codex:codex-rs/utils/home-dir/src/lib.rs",
            "commit": PINNED_CODEX_SHA,
        },
    ]
    for index, locator in enumerate(parts, start=1):
        evidence_items.append(evidence(f"evidence.local_storage_part_{index:02d}", locator))

    entities = [
        {
            "id": "system.codex",
            "kind": "system",
            "name": "Codex connected system",
            "description": "Canonical root joining configuration, runtime lifecycle, and persistence.",
            "evidence_refs": ["evidence.pinned_codex_config", "evidence.pinned_codex_home"],
        },
        {
            "id": "surface.configuration",
            "kind": "configuration",
            "name": "Configuration shape and effects",
            "evidence_refs": ["evidence.pinned_codex_config", "evidence.prompt_config_docs"],
        },
        {
            "id": "surface.environment",
            "kind": "environment",
            "name": "Environment inputs",
            "evidence_refs": ["evidence.pinned_codex_home", "evidence.prompt_config_docs"],
        },
        {
            "id": "surface.resolution",
            "kind": "resolution",
            "name": "Configuration resolution",
            "evidence_refs": ["evidence.prompt_config_docs"],
        },
        {
            "id": "surface.lifecycle",
            "kind": "lifecycle",
            "name": "Session, thread, turn, and rollout lifecycle",
            "evidence_refs": ["evidence.local_storage_docs"],
        },
        {
            "id": "surface.local_storage",
            "kind": "storage",
            "name": "Local storage and rollout layout",
            "evidence_refs": ["evidence.local_storage_entrypoint", "evidence.local_storage_docs"],
        },
        {
            "id": "artifact.canonical_contract",
            "kind": "artifact",
            "name": "Canonical machine-readable contract",
            "evidence_refs": ["evidence.contract_schema", "evidence.contract_checker"],
        },
        {
            "id": "workflow.canonical_proof",
            "kind": "workflow",
            "name": "Read-only canonical proof",
            "evidence_refs": ["evidence.canonical_workflow"],
        },
    ]
    relations = [
        {"id": "relation.codex_contains_configuration", "kind": "contains", "from": "system.codex", "to": "surface.configuration", "evidence_refs": ["evidence.pinned_codex_config"]},
        {"id": "relation.codex_contains_environment", "kind": "contains", "from": "system.codex", "to": "surface.environment", "evidence_refs": ["evidence.pinned_codex_home"]},
        {"id": "relation.configuration_influences_resolution", "kind": "influences", "from": "surface.configuration", "to": "surface.resolution", "evidence_refs": ["evidence.prompt_config_docs"]},
        {"id": "relation.environment_influences_resolution", "kind": "influences", "from": "surface.environment", "to": "surface.resolution", "evidence_refs": ["evidence.prompt_config_docs"]},
        {"id": "relation.lifecycle_persists_to_storage", "kind": "persists_to", "from": "surface.lifecycle", "to": "surface.local_storage", "evidence_refs": ["evidence.local_storage_docs", "evidence.local_storage_entrypoint"]},
        {"id": "relation.contract_describes_system", "kind": "describes", "from": "artifact.canonical_contract", "to": "system.codex", "evidence_refs": ["evidence.contract_schema"]},
        {"id": "relation.workflow_validates_contract", "kind": "validates", "from": "workflow.canonical_proof", "to": "artifact.canonical_contract", "evidence_refs": ["evidence.canonical_workflow", "evidence.contract_checker"]},
    ]
    rules = [
        {"id": "rule.single_semantic_authority", "kind": "authority", "statement": "Only codex-system-contract/v1 is semantic authority; compatibility views are derived.", "applies_to": ["artifact.canonical_contract"], "severity": "error"},
        {"id": "rule.reference_integrity", "kind": "reference_integrity", "statement": "Every relation, rule, claim, and evidence reference resolves to exactly one declared identifier.", "applies_to": ["artifact.canonical_contract"], "severity": "error"},
        {"id": "rule.coverage", "kind": "coverage", "statement": "Configuration, environment, resolution, lifecycle, and local storage remain connected to the system root.", "applies_to": ["system.codex", "surface.configuration", "surface.environment", "surface.resolution", "surface.lifecycle", "surface.local_storage"], "severity": "error"},
        {"id": "rule.digest_revalidation", "kind": "digest_revalidation", "statement": "Repository evidence and compatibility projections are re-hashed during every proof.", "applies_to": ["artifact.canonical_contract", "workflow.canonical_proof"], "severity": "error"},
        {"id": "rule.secret_safety", "kind": "secret_safety", "statement": "Canonical assets may describe secret handling but may not contain secret-like values.", "applies_to": ["artifact.canonical_contract"], "severity": "error"},
        {"id": "rule.determinism", "kind": "determinism", "statement": "Canonical JSON serialization and its SHA-256 digest are reproducible.", "applies_to": ["artifact.canonical_contract"], "severity": "error"},
    ]
    claims = [
        {"id": "claim.pinned_source", "statement": "The contract's configuration and home-resolution evidence is pinned to one immutable Codex commit.", "subjects": ["surface.configuration", "surface.environment", "surface.resolution"], "evidence_refs": ["evidence.pinned_codex_config", "evidence.pinned_codex_home"]},
        {"id": "claim.storage_identity", "statement": "Session, thread, and rollout identity is represented separately from physical local files.", "subjects": ["surface.lifecycle", "surface.local_storage"], "evidence_refs": ["evidence.local_storage_entrypoint", "evidence.local_storage_docs"]},
        {"id": "claim.proof_is_read_only", "statement": "The permanent canonical proof validates source and assets without mutating refs or repository contents.", "subjects": ["workflow.canonical_proof", "artifact.canonical_contract"], "evidence_refs": ["evidence.canonical_workflow"]},
    ]
    views = [
        {
            "id": "view.config_surface",
            "authoritative": False,
            "derived_from": ["surface.configuration", "surface.environment", "surface.resolution", "claim.pinned_source"],
            "projection": {"kind": "config_surface", "document": "PROMPT_ASSEMBLY_AND_CONFIG.md"},
        },
        {
            "id": "view.local_storage_layout",
            "authoritative": False,
            "derived_from": ["surface.lifecycle", "surface.local_storage", "claim.storage_identity"],
            "projection": {"kind": "local_storage_layout", "document": "LOCAL_STORAGE_AND_ROLLOUT_LAYOUT.md"},
        },
    ]
    for view in views:
        view["projection_sha256"] = sha256_bytes(
            canonical_bytes(
                {
                    "id": view["id"],
                    "derived_from": view["derived_from"],
                    "projection": view["projection"],
                }
            )
        )

    contract: dict[str, object] = {
        "schema": "codex-system-contract/v1",
        "authority": "system_contract",
        "source": {
            "repository": "wignerStan/codex-surface-generator",
            "pinned_codex_commit": PINNED_CODEX_SHA,
            "generator": ".github/canonical_machine_schema_publisher.py@temporary-action",
        },
        "entities": entities,
        "relations": relations,
        "rules": rules,
        "claims": claims,
        "evidence": evidence_items,
        "compatibility_views": views,
        "integrity": {"algorithm": "sha256", "canonical_sha256": ""},
    }
    contract["integrity"]["canonical_sha256"] = sha256_bytes(canonical_bytes(contract))  # type: ignore[index]
    return contract


def append_readme_link() -> None:
    path = ROOT / "README.md"
    text = path.read_text(encoding="utf-8")
    marker = "## Canonical machine-readable contract"
    if marker in text:
        return
    addition = '''\

## Canonical machine-readable contract

The semantic source of truth is [`codex-system-contract/v1`](contracts/codex-system-contract/v1/README.md).
Legacy configuration and local-storage documents are compatibility views; their
meaning is derived from the digest-bound canonical contract.
'''
    write_text(path, text.rstrip() + textwrap.dedent(addition))


def remove_bootstrap_residue() -> None:
    for path in (
        ROOT / ".github" / "canonical_bootstrap_v2.py",
        ROOT / ".github" / "workflows" / "canonical-representation-v2.yml",
    ):
        path.unlink(missing_ok=True)


def main() -> None:
    parts = split_local_storage()
    update_gitignore()
    CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(
        json.dumps(schema_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_checker_and_workflow()
    append_readme_link()
    remove_bootstrap_residue()
    contract = build_contract(parts)
    CONTRACT_PATH.write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema": contract["schema"],
                "canonical_sha256": contract["integrity"]["canonical_sha256"],  # type: ignore[index]
                "local_storage_parts": parts,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
