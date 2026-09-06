"""Qualified schema identities and conflict enforcement."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

from .diagnostics import DiagnosticCollector


def _pointer_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _pointer_join(pointer: str, token: str) -> str:
    return f"{pointer}/{_pointer_escape(token)}" if pointer else f"/{_pointer_escape(token)}"


def _walk(value: Any, pointer: str = ""):
    yield pointer or "/", value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk(child, _pointer_join(pointer, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, _pointer_join(pointer, str(index)))


def _field_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and ("name" in item or "wire_name" in item)
        for item in value
    )


def _source_path_from_rows(rows: Any) -> str | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        if isinstance(source, dict) and isinstance(source.get("path"), str):
            return source["path"]
    return None


def _schema_name(obj: Mapping[str, Any], pointer: str) -> str | None:
    for key in ("struct", "name", "body_schema", "response_body_schema"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if pointer.endswith("/turn_metadata_schema"):
        return "CodexTurnMetadataPayload"
    return None


def _safe_component(value: str) -> str:
    component = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")
    if not component:
        component = "unknown"
    if component[0].isdigit():
        component = "n_" + component
    return component


def qualified_rust_name(source_path: str | None, name: str) -> str:
    leaf_name = name.split("<", 1)[0]
    if source_path:
        match = re.match(r"codex-rs/([^/]+)/src/(.+)\.rs$", source_path)
        if match:
            crate = _safe_component(match.group(1))
            module_path = match.group(2)
            modules = [part for part in module_path.split("/") if part not in {"lib", "mod", "main"}]
            return "::".join([crate, *(_safe_component(part) for part in modules), leaf_name])
        digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:12]
        return f"path_{digest}::{leaf_name}"
    return f"unknown_source::{leaf_name}"


def schema_id_for_qualified_name(qualified_name: str) -> str:
    return "schema.rust." + ".".join(_safe_component(part) for part in qualified_name.split("::"))


class SchemaIdentityResolver:
    """Maps the pointer arguments used by v10 to source-qualified IDs."""

    def __init__(self, pointer_ids: Mapping[tuple[str, str], str], metadata: Mapping[str, Any]) -> None:
        self._pointer_ids = dict(pointer_ids)
        self.metadata = dict(metadata)

    @classmethod
    def from_report(cls, report: Mapping[str, Any]) -> "SchemaIdentityResolver":
        pointer_ids: dict[tuple[str, str], str] = {}
        metadata: dict[str, Any] = {}

        def record(name: str, pointer: str, rows: Any) -> None:
            if name == "CodexTurnMetadataPayload" and "turn_metadata_schema" in pointer:
                schema_id = "schema.codex.turn_metadata.full"
                qualified = "codex_core::responses_metadata::CodexTurnMetadataPayload"
                source_path = _source_path_from_rows(rows)
            else:
                source_path = _source_path_from_rows(rows)
                qualified = qualified_rust_name(source_path, name)
                schema_id = schema_id_for_qualified_name(qualified)
            pointer_ids[(name, pointer)] = schema_id
            metadata[schema_id] = {
                "id": schema_id,
                "rust_name": name,
                "qualified_rust_name": qualified,
                "source_path": source_path,
                "pointer": pointer,
            }

        for pointer, obj in _walk(report):
            if not isinstance(obj, dict):
                continue
            base_pointer = "" if pointer == "/" else pointer
            if _field_list(obj.get("fields")):
                name = _schema_name(obj, pointer)
                if name:
                    record(name, _pointer_join(base_pointer, "fields"), obj["fields"])
            if _field_list(obj.get("schema")):
                name = _schema_name(obj, pointer) or pointer.rsplit("/", 1)[-1]
                record(name, _pointer_join(base_pointer, "schema"), obj["schema"])
            if _field_list(obj.get("fixed_fields")):
                record(
                    "CodexTurnMetadataPayload",
                    _pointer_join(base_pointer, "fixed_fields"),
                    obj["fixed_fields"],
                )
            if isinstance(obj.get("variants"), list) and isinstance(obj.get("name"), str):
                source_path = _source_path_from_rows(obj.get("variants"))
                qualified = qualified_rust_name(source_path, obj["name"])
                schema_id = schema_id_for_qualified_name(qualified)
                pointer_ids[(obj["name"], pointer)] = schema_id
                metadata[schema_id] = {
                    "id": schema_id,
                    "rust_name": obj["name"],
                    "qualified_rust_name": qualified,
                    "source_path": source_path,
                    "pointer": pointer,
                }
        return cls(pointer_ids, metadata)

    def schema_id(self, name: str, pointer: str) -> str:
        exact = self._pointer_ids.get((name, pointer))
        if exact:
            return exact
        if "turn_metadata_schema" in pointer and name == "CodexTurnMetadataPayload":
            return "schema.codex.turn_metadata.full"
        # Unknown pointers remain collision-safe. The pointer suffix is only a
        # fallback when no source path was exposed by the extraction record.
        digest = hashlib.sha256(pointer.encode("utf-8")).hexdigest()[:12]
        qualified = f"pointer_{digest}::{name.split('<', 1)[0]}"
        schema_id = schema_id_for_qualified_name(qualified)
        self.metadata.setdefault(
            schema_id,
            {
                "id": schema_id,
                "rust_name": name,
                "qualified_rust_name": qualified,
                "source_path": None,
                "pointer": pointer,
                "fallback": True,
            },
        )
        return schema_id


def emit_schema_conflict_diagnostics(
    report: Mapping[str, Any], diagnostics: DiagnosticCollector
) -> None:
    entities = report.get("entities")
    if not isinstance(entities, Mapping):
        return
    fields = entities.get("fields")
    if not isinstance(fields, Mapping):
        return
    for field_id, field in fields.items():
        if not isinstance(field, Mapping):
            continue
        conflicts = field.get("conflicts")
        if not conflicts:
            continue
        diagnostics.emit(
            code="SCHEMA_FIELD_SHAPE_CONFLICT",
            severity="error",
            category="schema_resolution",
            message=f"A qualified field entity has conflicting extracted wire shapes: {field_id}",
            extractor_id="schema_identity",
            entity_id=str(field_id),
            report_pointer=f"/entities/fields/{_pointer_escape(str(field_id))}",
            details={"conflicts": conflicts},
            recoverable=False,
            strict_failure=True,
        )
