"""Strict offline structure, reference, digest and compatibility validation."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .proof_schema_validation import load_schemas
from .system_contract import SCHEMA_ID, compatibility_views, digest, view_at
from .system_contract_evidence import revalidate_source_bytes, relative_source_path, scan_secrets
from .validation import validate_evolution_contract


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


def _references(model: Mapping[str, Any]) -> None:
    specs = model["source_registry"]["sources"]
    snapshot = model["source_snapshot"]
    files = snapshot["files"]
    unavailable = snapshot["unavailable_specs"]
    if snapshot["revision"] != model["source_revision"]:
        raise ValueError("source snapshot/revision disagreement")
    if set(files) & set(unavailable) or (set(files) | set(unavailable)) - set(specs):
        raise ValueError("unresolved or contradictory source inventory")
    if snapshot["counts"] != {"available": len(files), "unavailable": len(unavailable)}:
        raise ValueError("source inventory count mismatch")
    for key, spec in specs.items():
        if key != spec["id"]:
            raise ValueError("source registry key/id mismatch")
    paths = set()
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
    if sorted(extractors) != sorted(model["migration"]["canonical_ir_extractors"]):
        raise ValueError("canonical extractor coverage mismatch")
    for key, entry in extractors.items():
        if key != entry["extractor_id"]:
            raise ValueError("extractor key/id mismatch")
        if set(entry["source_spec_ids"]) - set(specs):
            raise ValueError("extractor references undeclared source specs")
        data = entry["data"]
        if data.get("$schema"):
            errors = list(_validator(data["$schema"]).iter_errors(data))
            # Partial extraction may omit facts, but cannot mis-type or invent
            # fields that it does provide. Complete fragments require all facts.
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
                if not source or any(evidence[name] != source[name] for name in ("path", "sha256", "git_blob_sha")):
                    raise ValueError("local-storage evidence/manifest disagreement")
        graph = data.get("surface_graph")
        if graph is not None:
            _graph_references(graph)
    if model["status"]["overall"] == "complete":
        required = {key for key, spec in specs.items() if spec["required"]}
        if not extractors or not required <= set(files) or not all(e["semantic_complete"] for e in extractors.values()):
            raise ValueError("complete status contradicts extraction coverage")


def _graph_references(graph: Mapping[str, Any]) -> None:
    nodes = graph["nodes"]
    if any(key != node["id"] for key, node in nodes.items()):
        raise ValueError("surface graph node key/id mismatch")
    edge_ids = set()
    for edge in graph["edges"]:
        if edge["id"] in edge_ids:
            raise ValueError("duplicate surface graph edge id")
        edge_ids.add(edge["id"])
        if edge["source"] not in nodes or edge["target"] not in nodes:
            raise ValueError("unresolved surface graph edge")


def validate_system_contract(
    value: Mapping[str, Any], *, report: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Raise ValueError on a bad contract. Source-byte checks are explicit."""
    scan_secrets(value)
    errors = list(_validator().iter_errors(value))
    if errors:
        # Never echo untrusted values (possibly credentials) in diagnostics.
        raise ValueError("canonical schema violation at " + "/".join(map(str, errors[0].absolute_path)))
    model = value["model"]
    failures = validate_evolution_contract(model)
    if failures:
        raise ValueError(failures[0]["code"])
    _references(model)
    expected_views = {path: digest(data) for path, data in compatibility_views(model).items()}
    if value["compatibility_views"] != expected_views:
        raise ValueError("canonical compatibility projection digest mismatch")
    body = {key: child for key, child in value.items() if key != "integrity"}
    if value["integrity"]["canonical_sha256"] != digest(body):
        raise ValueError("canonical content digest mismatch")
    if report is not None:
        for pointer, expected in expected_views.items():
            if digest(view_at(report, pointer)) != expected:
                raise ValueError(f"compatibility view differs from canonical model: {pointer}")
    checked = revalidate_source_bytes(model, source_root) if source_root is not None else None
    return {
        "schema": value["schema"], "authority": value["authority"],
        "canonical_sha256": value["integrity"]["canonical_sha256"],
        "extractors": sorted(model["extractors"]),
        "legacy_machine_reconstruction_remaining": model["migration"]["legacy_machine_reconstruction_remaining"],
        "compatibility_views_checked": len(expected_views) if report is not None else 0,
        "source_files_revalidated": checked,
        "source_bytes_status": "verified" if checked is not None else "not_checked",
    }
