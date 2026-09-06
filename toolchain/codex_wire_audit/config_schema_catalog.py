"""Normalize Codex's generated config JSON Schema into path-addressable facts."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

from .canonical import canonical_json_bytes


_SCHEMA_KEYS = {
    "type", "enum", "const", "default", "description", "format", "minimum",
    "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
    "pattern", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
    "deprecated", "readOnly", "writeOnly",
}
_CONSTRAINT_KEYS = _SCHEMA_KEYS - {"type", "enum", "const", "default", "description", "format"}


class ConfigSchemaError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SchemaLocation:
    pointer: str
    ref_chain: tuple[str, ...] = ()


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _json_pointer(document: Any, ref: str) -> Any:
    if not ref.startswith("#/"):
        raise ConfigSchemaError(f"unsupported external JSON Schema reference: {ref}")
    value = document
    for raw in ref[2:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, Mapping) or key not in value:
            raise ConfigSchemaError(f"unresolved JSON Schema reference: {ref}")
        value = value[key]
    return value


def _bounded_value(value: Any, *, max_bytes: int = 4096) -> Any:
    encoded = canonical_json_bytes(value)
    if len(encoded) <= max_bytes:
        return copy.deepcopy(value)
    return {
        "omitted": True,
        "byte_length": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "preview": encoded[:256].decode("utf-8", errors="replace"),
    }


def _declared_types(schema: Any) -> list[str]:
    if schema is True:
        return ["any"]
    if schema is False:
        return ["never"]
    if not isinstance(schema, Mapping):
        return []
    value = schema.get("type")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return sorted(str(item) for item in value)
    return []


def _composition(schema: Mapping[str, Any]) -> dict[str, int]:
    return {
        key: len(schema.get(key) or [])
        for key in ("allOf", "anyOf", "oneOf")
        if isinstance(schema.get(key), list)
    }


def _node_fact(
    path: str,
    schema: Any,
    location: SchemaLocation,
    *,
    parent_path: str | None,
    required: bool,
    kind: str,
) -> dict[str, Any]:
    if isinstance(schema, bool):
        fact: dict[str, Any] = {
            "id": f"config.{path or '<root>'}",
            "path": path,
            "parent_path": parent_path,
            "kind": kind,
            "required": required,
            "boolean_schema": schema,
            "declared_types": _declared_types(schema),
            "schema_locations": [location.pointer],
            "ref_chain": list(location.ref_chain),
        }
    elif isinstance(schema, Mapping):
        fact = {
            "id": f"config.{path or '<root>'}",
            "path": path,
            "parent_path": parent_path,
            "kind": kind,
            "required": required,
            "declared_types": _declared_types(schema),
            "schema_locations": [location.pointer],
            "ref_chain": list(location.ref_chain),
            "composition": _composition(schema),
        }
        for key in ("description", "format", "deprecated", "readOnly", "writeOnly"):
            if key in schema:
                fact[key] = copy.deepcopy(schema[key])
        if "enum" in schema:
            fact["enum"] = _bounded_value(schema["enum"])
        if "const" in schema:
            fact["const"] = _bounded_value(schema["const"])
        if "default" in schema:
            fact["default"] = _bounded_value(schema["default"])
        constraints = {key: copy.deepcopy(schema[key]) for key in sorted(_CONSTRAINT_KEYS) if key in schema}
        if constraints:
            fact["constraints"] = constraints
        if isinstance(schema.get("$ref"), str):
            fact["direct_ref"] = schema["$ref"]
    else:
        raise ConfigSchemaError(f"schema at {location.pointer} must be object or boolean")
    digest_input = {key: value for key, value in fact.items() if key not in {"id", "schema_locations"}}
    fact["semantic_digest"] = hashlib.sha256(canonical_json_bytes(digest_input)).hexdigest()
    return fact


def _merge_node(nodes: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    path = candidate["path"]
    existing = nodes.get(path)
    if existing is None:
        nodes[path] = candidate
        return
    locations = sorted(set(existing.get("schema_locations", [])) | set(candidate.get("schema_locations", [])))
    if existing["semantic_digest"] != candidate["semantic_digest"]:
        variants = existing.setdefault("schema_variants", [])
        compact = {key: value for key, value in candidate.items() if key != "schema_locations"}
        if compact not in variants:
            variants.append(compact)
        existing["variant_count"] = 1 + len(variants)
    existing["schema_locations"] = locations
    existing["required"] = bool(existing.get("required")) or bool(candidate.get("required"))


def _walk(
    document: Mapping[str, Any],
    schema: Any,
    *,
    path: str,
    pointer: str,
    parent_path: str | None,
    required: bool,
    kind: str,
    nodes: dict[str, dict[str, Any]],
    refs: dict[str, dict[str, Any]],
    unresolved: list[str],
    ref_stack: tuple[str, ...],
) -> None:
    location = SchemaLocation(pointer=pointer, ref_chain=ref_stack)
    _merge_node(nodes, _node_fact(path, schema, location, parent_path=parent_path, required=required, kind=kind))
    if isinstance(schema, bool):
        return
    if not isinstance(schema, Mapping):
        return

    direct_ref = schema.get("$ref")
    if isinstance(direct_ref, str):
        ref_entry = refs.setdefault(direct_ref, {"ref": direct_ref, "locations": [], "resolved": True})
        ref_entry["locations"] = sorted(set(ref_entry["locations"]) | {pointer})
        if direct_ref in ref_stack:
            ref_entry["cycle"] = True
        else:
            try:
                target = _json_pointer(document, direct_ref)
            except ConfigSchemaError:
                ref_entry["resolved"] = False
                unresolved.append(direct_ref)
            else:
                _walk(
                    document,
                    target,
                    path=path,
                    pointer=direct_ref[1:],
                    parent_path=parent_path,
                    required=required,
                    kind=kind,
                    nodes=nodes,
                    refs=refs,
                    unresolved=unresolved,
                    ref_stack=(*ref_stack, direct_ref),
                )

    for combinator in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(combinator)
        if not isinstance(branches, list):
            continue
        for index, branch in enumerate(branches):
            _walk(
                document,
                branch,
                path=path,
                pointer=f"{pointer}/{combinator}/{index}",
                parent_path=parent_path,
                required=required,
                kind=f"{kind}_{combinator}_branch",
                nodes=nodes,
                refs=refs,
                unresolved=unresolved,
                ref_stack=ref_stack,
            )

    properties = schema.get("properties")
    required_names = set(schema.get("required") or [])
    if isinstance(properties, Mapping):
        for name, child in sorted(properties.items()):
            child_path = f"{path}.{name}" if path else str(name)
            _walk(
                document,
                child,
                path=child_path,
                pointer=f"{pointer}/properties/{_escape_pointer(str(name))}",
                parent_path=path or None,
                required=name in required_names,
                kind="property",
                nodes=nodes,
                refs=refs,
                unresolved=unresolved,
                ref_stack=ref_stack,
            )

    additional = schema.get("additionalProperties")
    if isinstance(additional, (Mapping, bool)) and additional is not False:
        child_path = f"{path}.*" if path else "*"
        _walk(
            document,
            additional,
            path=child_path,
            pointer=f"{pointer}/additionalProperties",
            parent_path=path or None,
            required=False,
            kind="map_value",
            nodes=nodes,
            refs=refs,
            unresolved=unresolved,
            ref_stack=ref_stack,
        )

    items = schema.get("items")
    if isinstance(items, (Mapping, bool)):
        child_path = f"{path}[]" if path else "[]"
        _walk(
            document,
            items,
            path=child_path,
            pointer=f"{pointer}/items",
            parent_path=path or None,
            required=False,
            kind="array_item",
            nodes=nodes,
            refs=refs,
            unresolved=unresolved,
            ref_stack=ref_stack,
        )


def build_config_schema_catalog(text: str, *, source_path: str, source_sha256: str) -> dict[str, Any]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigSchemaError(f"generated config schema is invalid JSON: {error}") from error
    if not isinstance(document, Mapping):
        raise ConfigSchemaError("generated config schema root must be an object")
    nodes: dict[str, dict[str, Any]] = {}
    refs: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    _walk(
        document,
        document,
        path="",
        pointer="#",
        parent_path=None,
        required=True,
        kind="root",
        nodes=nodes,
        refs=refs,
        unresolved=unresolved,
        ref_stack=(),
    )
    definitions = document.get("$defs") or document.get("definitions") or {}
    root_properties = document.get("properties") or {}
    paths = sorted(path for path in nodes if path)
    feature_paths = sorted(path for path in paths if path.startswith("features."))
    profile_feature_paths = sorted(path for path in paths if path.startswith("profiles.*.features."))
    summary = {
        "draft": document.get("$schema"),
        "title": document.get("title"),
        "root_property_count": len(root_properties) if isinstance(root_properties, Mapping) else 0,
        "definition_count": len(definitions) if isinstance(definitions, Mapping) else 0,
        "config_path_count": len(paths),
        "feature_path_count": len(feature_paths),
        "profile_feature_path_count": len(profile_feature_paths),
        "reference_count": len(refs),
        "unresolved_reference_count": len(set(unresolved)),
    }
    catalog: dict[str, Any] = {
        "source": {"path": source_path, "sha256": source_sha256},
        "summary": summary,
        "paths": {path: nodes[path] for path in paths},
        "root_properties": sorted(root_properties) if isinstance(root_properties, Mapping) else [],
        "definitions": sorted(definitions) if isinstance(definitions, Mapping) else [],
        "references": {ref: refs[ref] for ref in sorted(refs)},
        "unresolved_references": sorted(set(unresolved)),
    }
    catalog["semantic_digest"] = hashlib.sha256(canonical_json_bytes(catalog)).hexdigest()
    return catalog
