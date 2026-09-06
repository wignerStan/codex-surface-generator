"""Load and emit the declarative v11 JSON Schema bundle.

Schema documents are package data, not a 600-line Python builder.  This keeps
schema review as ordinary JSON diffs and allows non-Python tooling to consume
the exact same source files used by the CLI.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import EVOLUTION_CONTRACT_VERSION, SEMANTIC_DIFF_VERSION, SOURCE_REGISTRY_VERSION

_TEMPLATE_DIR = Path(__file__).resolve().parent / "schema_templates"
_VERSION_EXPECTATIONS = {
    "source-registry.schema.json": ("schema_version", SOURCE_REGISTRY_VERSION),
    "evolution-contract.schema.json": ("schema_version", EVOLUTION_CONTRACT_VERSION),
    "semantic-diff.schema.json": ("schema_version", SEMANTIC_DIFF_VERSION),
}


class SchemaBundleError(ValueError):
    pass


def _load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SchemaBundleError(f"cannot load schema template {path}: {error}") from error
    if not isinstance(value, dict):
        raise SchemaBundleError(f"schema template root must be an object: {path}")
    if value.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise SchemaBundleError(f"schema template is not Draft 2020-12: {path}")
    if not isinstance(value.get("$id"), str):
        raise SchemaBundleError(f"schema template has no $id: {path}")
    expectation = _VERSION_EXPECTATIONS.get(path.name)
    if expectation:
        property_name, expected = expectation
        actual = ((value.get("properties") or {}).get(property_name) or {}).get("const")
        if actual != expected:
            raise SchemaBundleError(
                f"schema/package version drift in {path.name}: expected {expected}, found {actual}"
            )
    return value


def schema_documents() -> dict[str, dict[str, Any]]:
    paths = sorted(_TEMPLATE_DIR.glob("*.schema.json"))
    if not paths:
        raise SchemaBundleError(f"no schema templates found under {_TEMPLATE_DIR}")
    return {path.name: _load_document(path) for path in paths}


def write_schema_documents(directory: str | Path) -> list[Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    documents = schema_documents()
    paths: list[Path] = []
    for name, document in documents.items():
        path = root / name
        path.write_text(
            json.dumps(copy.deepcopy(document), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        paths.append(path)
    first_id = next(iter(documents.values())).get("$id", "")
    base_id = str(first_id).rsplit("/", 1)[0] + "/" if "/" in str(first_id) else None
    index = root / "index.json"
    index.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "base_id": base_id,
                "documents": sorted(documents),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    paths.append(index)
    return paths
