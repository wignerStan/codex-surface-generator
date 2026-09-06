#!/usr/bin/env python3
"""Machine-contract helpers for Codex wire audit v10.

This module is standard-library only. It converts the detailed human-oriented
report into a stable entity/edge model, emits JSON Schema documents, validates
report invariants, supports canonical serialization, and provides offline
source-loading helpers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unicodedata
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence

REPORT_FORMAT_VERSION = "10.0.0"
GENERATOR_VERSION = "4.0.0"
MACHINE_CONTRACT_VERSION = "1.0.0"
REPORT_SCHEMA_ID = "urn:codex-wire-audit:schema:report:10.0.0"
DIAGNOSTIC_SCHEMA_ID = "urn:codex-wire-audit:schema:diagnostic:1.0.0"
WIRE_FIELD_SCHEMA_ID = "urn:codex-wire-audit:schema:wire-field:1.0.0"
RULE_SCHEMA_ID = "urn:codex-wire-audit:schema:rule:1.0.0"
PREDICATE_SCHEMA_ID = "urn:codex-wire-audit:schema:predicate:1.0.0"
EDGE_SCHEMA_ID = "urn:codex-wire-audit:schema:edge:1.0.0"
SOURCE_SCHEMA_ID = "urn:codex-wire-audit:schema:source:1.0.0"
CANONICALIZATION_ID = "codex-wire-audit-canonical-json-v1"


class ContractError(RuntimeError):
    """Raised when the v10 machine contract cannot be produced or validated."""


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def stable_slug(value: Any, *, max_length: int = 80) -> str:
    text = nfc(str(value)).strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    if not text:
        text = "unnamed"
    if len(text) <= max_length:
        return text
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    return f"{text[:max_length - 13]}_{digest}"


def stable_component(value: Any, *, max_length: int = 80) -> str:
    original = nfc(str(value)).strip().lower()
    slug = stable_slug(original, max_length=max_length)
    # Characters collapsed by slugification can otherwise make distinct wire
    # names collide (for example x-a-b versus x_a_b). Preserve readable IDs for
    # already-safe components and add a deterministic suffix for transformed ones.
    if re.fullmatch(r"[a-z0-9_]+", original) and len(original) <= max_length:
        return slug
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    room = max(1, max_length - len(digest) - 1)
    return f"{slug[:room]}_{digest}"


def _stable_id_parts(value: Any) -> list[str]:
    """Return readable ID segments while preserving transformed-name collision safety."""
    original = nfc(str(value)).strip().lower()
    if re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*", original):
        return original.split(".")
    return [stable_component(original)]


def stable_id(kind: str, *parts: Any) -> str:
    segments = _stable_id_parts(kind)
    for part in parts:
        segments.extend(_stable_id_parts(part))
    return ".".join(segments)


def pointer_escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer_join(pointer: str, token: str | int) -> str:
    return f"{pointer}/{pointer_escape(str(token))}" if pointer else f"/{pointer_escape(str(token))}"


def walk_json(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    yield pointer or "/", value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_json(child, pointer_join(pointer, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_json(child, pointer_join(pointer, index))


def json_pointer_get(value: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return value
    if not pointer.startswith("/"):
        # Friendly section shorthand.
        pointer = "/" + pointer
    current = value
    for encoded in pointer[1:].split("/"):
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as error:
                raise ContractError(f"invalid JSON pointer list token {token!r}: {pointer}") from error
        elif isinstance(current, dict):
            if token not in current:
                raise ContractError(f"JSON pointer not found: {pointer}")
            current = current[token]
        else:
            raise ContractError(f"JSON pointer traverses a scalar: {pointer}")
    return current


def _normalize_json_strings(value: Any) -> Any:
    if isinstance(value, str):
        return nfc(value)
    if isinstance(value, list):
        return [_normalize_json_strings(item) for item in value]
    if isinstance(value, dict):
        return {
            nfc(str(key)): _normalize_json_strings(child)
            for key, child in value.items()
        }
    return value


def canonical_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(report))
    payload.pop("integrity", None)
    payload.pop("generated_at", None)
    status = payload.get("status")
    if isinstance(status, dict):
        status.pop("validated_at", None)
    return _normalize_json_strings(payload)


def canonical_report_view(report: Mapping[str, Any]) -> dict[str, Any]:
    view = copy.deepcopy(dict(report))
    view.pop("generated_at", None)
    status = view.get("status")
    if isinstance(status, dict):
        status.pop("validated_at", None)
    return view


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _normalize_json_strings(value)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ContractError(f"value cannot be canonically serialized: {error}") from error
    return (encoded + "\n").encode("utf-8")


def pretty_json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def attach_integrity(report: MutableMapping[str, Any]) -> dict[str, Any]:
    payload = canonical_payload(report)
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    integrity = {
        "algorithm": "sha256",
        "canonicalization": CANONICALIZATION_ID,
        "payload_excludes": ["generated_at", "integrity", "status.validated_at"],
        "payload_sha256": digest,
    }
    report["integrity"] = integrity
    return integrity


def verify_integrity(report: Mapping[str, Any]) -> bool:
    integrity = report.get("integrity")
    if not isinstance(integrity, dict):
        return False
    expected = integrity.get("payload_sha256")
    if not isinstance(expected, str):
        return False
    actual = hashlib.sha256(canonical_json_bytes(canonical_payload(report))).hexdigest()
    return actual == expected


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    parts: list[str] = []
    start = 0
    depths = {"<": 0, "(": 0, "[": 0, "{": 0}
    pairs = {">": "<", ")": "(", "]": "[", "}": "{"}
    quote: str | None = None
    escaped = False
    for index, char in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char in depths:
            depths[char] += 1
            continue
        if char in pairs:
            opener = pairs[char]
            if depths[opener]:
                depths[opener] -= 1
            continue
        if char == delimiter and not any(depths.values()):
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _strip_rust_reference(rust_type: str) -> str:
    value = " ".join(rust_type.strip().split())
    while value.startswith("&"):
        value = value[1:].lstrip()
        value = re.sub(r"^'[A-Za-z_][A-Za-z0-9_]*\s*", "", value)
        value = re.sub(r"^mut\s+", "", value)
    return value


def _outer_generic(rust_type: str) -> tuple[str, list[str]] | None:
    text = rust_type.strip()
    first = text.find("<")
    if first < 1 or not text.endswith(">"):
        return None
    depth = 0
    for index in range(first, len(text)):
        char = text[index]
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return None
    if depth != 0:
        return None
    return text[:first].strip(), _split_top_level(text[first + 1 : -1])


def unwrap_rust_option(rust_type: str) -> tuple[str, bool]:
    text = _strip_rust_reference(rust_type)
    generic = _outer_generic(text)
    if generic and generic[0].split("::")[-1] == "Option" and generic[1]:
        return generic[1][0], True
    return text, False


def rust_type_to_wire_schema(rust_type: str) -> dict[str, Any]:
    """Return a JSON-Schema-like fragment plus extraction certainty metadata."""
    raw = " ".join(str(rust_type or "").split())
    text, _optional = unwrap_rust_option(raw)
    text = _strip_rust_reference(text)

    # Wrapper types that do not change the serialized value shape.
    generic = _outer_generic(text)
    if generic and generic[0].split("::")[-1] in {"Box", "Arc", "Rc", "Cow", "Pin"}:
        inner = generic[1][-1] if generic[1] else "serde_json::Value"
        return rust_type_to_wire_schema(inner)

    generic = _outer_generic(text)
    if generic:
        outer = generic[0].split("::")[-1]
        args = generic[1]
        if outer in {"Vec", "VecDeque", "HashSet", "BTreeSet", "SmallVec"}:
            item = rust_type_to_wire_schema(args[-1] if args else "serde_json::Value")
            return {"type": "array", "items": item, "x-rust-type": raw, "x-certainty": "source_derived"}
        if outer in {"HashMap", "BTreeMap", "IndexMap"}:
            value_type = args[-1] if args else "serde_json::Value"
            return {
                "type": "object",
                "additionalProperties": rust_type_to_wire_schema(value_type),
                "x-rust-type": raw,
                "x-certainty": "source_derived",
            }
        if outer in {"Result", "ControlFlow"}:
            return {
                "x-rust-type": raw,
                "x-wire-kind": "non_wire_wrapper",
                "x-certainty": "inferred",
            }
        if outer in {"NonZeroU8", "NonZeroU16", "NonZeroU32", "NonZeroU64", "NonZeroUsize"}:
            return {"type": "integer", "minimum": 1, "x-rust-type": raw, "x-certainty": "source_derived"}

    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1]
        if ";" in body:
            item_type, length = body.rsplit(";", 1)
            schema = rust_type_to_wire_schema(item_type)
            result: dict[str, Any] = {"type": "array", "items": schema, "x-rust-type": raw}
            try:
                size = int(length.strip())
            except ValueError:
                result["x-array-length-expression"] = length.strip()
            else:
                result["minItems"] = size
                result["maxItems"] = size
            return result
        return {"type": "array", "items": rust_type_to_wire_schema(body), "x-rust-type": raw}

    if text.startswith("(") and text.endswith(")"):
        items = [rust_type_to_wire_schema(item) for item in _split_top_level(text[1:-1])]
        return {
            "type": "array",
            "prefixItems": items,
            "minItems": len(items),
            "maxItems": len(items),
            "x-rust-type": raw,
        }

    leaf = text.split("::")[-1]
    integer_ranges = {
        "u8": (0, 255),
        "u16": (0, 65535),
        "u32": (0, 4294967295),
        "i8": (-128, 127),
        "i16": (-32768, 32767),
        "i32": (-2147483648, 2147483647),
    }
    if leaf in integer_ranges:
        minimum, maximum = integer_ranges[leaf]
        return {"type": "integer", "minimum": minimum, "maximum": maximum, "x-rust-type": raw}
    if leaf in {"u64", "usize", "u128"}:
        return {"type": "integer", "minimum": 0, "x-rust-type": raw}
    if leaf in {"i64", "isize", "i128"}:
        return {"type": "integer", "x-rust-type": raw}
    if leaf in {"f32", "f64", "Number"}:
        return {"type": "number", "x-rust-type": raw}
    if leaf == "bool":
        return {"type": "boolean", "x-rust-type": raw}
    if leaf in {"String", "str", "OsString"}:
        return {"type": "string", "x-rust-type": raw}
    nonzero_integer_ranges = {
        "NonZeroU8": (1, 255),
        "NonZeroU16": (1, 65535),
        "NonZeroU32": (1, 4294967295),
    }
    if leaf in nonzero_integer_ranges:
        minimum, maximum = nonzero_integer_ranges[leaf]
        return {
            "type": "integer",
            "minimum": minimum,
            "maximum": maximum,
            "x-rust-type": raw,
        }
    if leaf in {"NonZeroU64", "NonZeroUsize"}:
        return {"type": "integer", "minimum": 1, "x-rust-type": raw}
    if leaf in {"Uuid", "ThreadId", "SessionId"}:
        return {"type": "string", "format": "uuid", "x-rust-type": raw}
    if leaf == "ResponseItemId":
        return {
            "type": "string",
            "x-wire-kind": "responses_prefixed_id",
            "x-rust-type": raw,
            "x-certainty": "source_derived",
        }
    if leaf in {"Path", "PathBuf", "AbsolutePathBuf", "PathUri", "LegacyAppPathString"}:
        return {"type": "string", "format": "path-or-uri", "x-rust-type": raw}
    if leaf in {"Duration"}:
        return {"type": "number", "minimum": 0, "x-unit": "seconds", "x-rust-type": raw, "x-certainty": "inferred"}
    if leaf in {"Value", "RawValue", "JsonValue"} or "serde_json::Value" in text:
        return {"x-json-type": "any", "x-rust-type": raw, "x-certainty": "source_derived"}
    if leaf in {"HeaderMap", "HeaderValue"}:
        return {"type": "string", "x-wire-kind": "http_header_value", "x-rust-type": raw, "x-certainty": "inferred"}
    if not raw:
        return {"x-json-type": "unknown", "x-certainty": "unknown"}
    return {
        "x-entity-ref": stable_id("schema", "rust", leaf),
        "x-rust-type": raw,
        "x-wire-kind": "named_type",
        "x-certainty": "inferred",
    }


def omitted_when_from_skip(skip: str | None) -> dict[str, Any] | None:
    if not skip:
        return None
    normalized = skip.strip()
    if normalized.endswith("Option::is_none") or normalized == "Option::is_none":
        return {"op": "is_null", "path": "$"}
    if normalized.endswith("::is_empty"):
        return {"op": "is_empty", "path": "$"}
    if normalized.endswith("Not::not") or normalized == "std::ops::Not::not":
        return {"op": "eq", "path": "$", "value": False}
    return {"op": "custom_predicate", "path": "$", "function": normalized, "machine_evaluable": False}


def classify_lifetime(name: str) -> str:
    lower = name.lower()
    if "installation" in lower:
        return "installation"
    if "session" in lower:
        return "session"
    if "thread" in lower:
        return "thread"
    if "turn" in lower:
        return "turn"
    if "request" in lower:
        return "request"
    if "window" in lower:
        return "context_window"
    return "message_or_configuration"


def infer_field_authority(schema_id: str, wire_name: str) -> str:
    lower_schema = schema_id.lower()
    lower_name = wire_name.lower()
    if "turn_metadata" in lower_schema or lower_name in {
        "session_id", "thread_id", "turn_id", "request_kind", "tool_namespaces_info"
    }:
        return "codex_core"
    if "response" in lower_schema and "request" not in lower_schema:
        return "server_or_provider"
    if "config" in lower_schema or "mcpserverconfig" in lower_schema:
        return "configuration"
    return "client_request_builder"


def normalize_field(field: Mapping[str, Any], schema_id: str, pointer: str) -> dict[str, Any]:
    rust_type = str(field.get("type") or "")
    inner_type, rust_optional = unwrap_rust_option(rust_type)
    serialized = bool(field.get("serialized", not field.get("skipped", False)))
    flattened = bool(field.get("flattened"))
    skip_if = field.get("skip_serializing_if")
    omitted_when = omitted_when_from_skip(str(skip_if)) if skip_if else None
    if not serialized:
        mode = "never"
        required = False
        nullable = False
    elif flattened:
        mode = "flattened"
        required = False
        nullable = False
    elif skip_if:
        mode = "conditional"
        required = False
        nullable = False
    elif rust_optional:
        # Serde's default Option serialization writes an explicit null when None.
        mode = "present_nullable"
        required = True
        nullable = True
    else:
        mode = "present"
        required = True
        nullable = False

    base_schema = rust_type_to_wire_schema(inner_type)
    wire_schema: dict[str, Any]
    if nullable:
        wire_schema = {"anyOf": [base_schema, {"type": "null"}]}
    else:
        wire_schema = base_schema

    wire_name = str(field.get("wire_name") or field.get("name") or "unnamed")
    field_id = stable_id(
        "field",
        schema_id.removeprefix("schema."),
        wire_name,
    )
    source = field.get("source") if isinstance(field.get("source"), dict) else None
    source_refs = []
    if source and source.get("location_ref"):
        source_refs.append(source["location_ref"])
    sensitivity, logging_policy = classify_sensitivity(wire_name)
    return {
        "id": field_id,
        "schema_id": schema_id,
        "name": field.get("name"),
        "wire_name": wire_name,
        "rust_type": rust_type,
        "wire_schema": wire_schema,
        "presence": {
            "mode": mode,
            "required": required,
            "nullable": nullable,
            "omitted_when": omitted_when,
            "wire_wrapper": None if flattened else "field",
        },
        "deserialization": {
            "accepts_absent": bool(rust_optional or field.get("serde_default") or flattened),
            "serde_default": bool(field.get("serde_default")),
        },
        "flattened": flattened,
        "serialized": serialized,
        "source_refs": source_refs,
        "occurrences": [pointer],
        "certainty": "source_derived",
        "authority": infer_field_authority(schema_id, wire_name),
        "lifetime": classify_lifetime(wire_name),
        "sensitivity": sensitivity,
        "logging_policy": logging_policy,
    }


def condition_to_predicate(condition: Any) -> dict[str, Any]:
    if condition is None:
        return {"op": "unspecified", "machine_evaluable": False}
    if isinstance(condition, bool):
        return {"op": "const", "value": condition}
    text = str(condition).strip()
    lower = text.lower()
    if lower in {"always", "required", "unconditional"} or lower.startswith("always "):
        return {"op": "const", "value": True}
    if "responses lite only" in lower or lower == "responses lite":
        return {"op": "eq", "path": "model.use_responses_lite", "value": True}
    if "request_kind" in lower and ("present" in lower or "exists" in lower):
        return {"op": "exists", "path": "turn_metadata.request_kind"}
    match = re.match(r"when\s+([a-zA-Z0-9_.-]+)\s+exists$", text, re.I)
    if match:
        return {"op": "exists", "path": match.group(1)}
    match = re.match(r"when\s+([A-Z0-9_]+)\s+is set$", text, re.I)
    if match:
        return {"op": "env_exists", "name": match.group(1)}
    if "fedramp only" in lower:
        return {"op": "eq", "path": "auth.fedramp", "value": True}
    if "when account" in lower and "exists" in lower:
        return {"op": "exists", "path": "auth.account_id"}
    if "after server" in lower and "turn state" in lower:
        return {"op": "exists", "path": "continuation.x_codex_turn_state"}
    if "when parent_thread_id exists" in lower or "when parent thread exists" in lower:
        return {"op": "exists", "path": "turn.parent_thread_id"}
    if "when turn_id exists" in lower:
        return {"op": "exists", "path": "turn.turn_id"}
    return {
        "op": "source_condition",
        "expression": text,
        "machine_evaluable": False,
    }


def classify_sensitivity(name: str, category: str | None = None) -> tuple[str, str]:
    lower = name.lower()
    cat = (category or "").lower()
    if lower == "authorization" or "bearer" in lower or "credential" in lower:
        return "credential", "redact"
    if "ticket" in lower or "attestation" in lower:
        return "opaque_secret", "redact"
    if any(token in lower for token in ("session", "thread", "request-id", "window-id", "turn-id", "account-id")):
        return "identifier", "hash_or_redact"
    if "metadata" in lower or "workspace" in lower or cat == "metadata":
        return "operational", "review_before_logging"
    if "path" in lower or "cwd" in lower:
        return "filesystem_path", "redact_or_relativize"
    if cat in {"auth", "attestation", "security"}:
        return "security_decision", "redact"
    return "public_protocol", "allow"


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def build_source_manifest(
    commit: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    *,
    source_mode: str,
    repository: str,
    requested_ref: str,
    dirty: bool | None = None,
) -> dict[str, Any]:
    files: dict[str, Any] = {}
    path_index: dict[str, str] = {}
    for path in sorted(sources):
        descriptor = sources[path]
        available = bool(
            descriptor.get("available", descriptor.get("text") is not None)
        )
        raw_text = descriptor.get("text")
        text = str(raw_text) if available and raw_text is not None else None
        data = text.encode("utf-8") if text is not None else None
        source_id = stable_id(
            "source",
            Path(path).name,
            hashlib.sha256(path.encode("utf-8")).hexdigest()[:16],
        )
        path_index[path] = source_id
        roles = descriptor.get("roles") or []
        files[source_id] = {
            "id": source_id,
            "path": path,
            "roles": sorted({str(role) for role in roles}),
            "encoding": "utf-8" if data is not None else None,
            "byte_length": len(data) if data is not None else None,
            "line_count": (
                text.count("\n") + (0 if text.endswith("\n") else 1)
                if text is not None
                else None
            ),
            "sha256": hashlib.sha256(data).hexdigest() if data is not None else None,
            "git_blob_sha1": _git_blob_sha(data) if data is not None else None,
            "available": available,
        }
    available_count = sum(1 for source in files.values() if source["available"])
    return {
        "schema_version": "1.0.0",
        "source_mode": source_mode,
        "repository": repository,
        "requested_ref": requested_ref,
        "commit": dict(commit),
        "dirty": dirty,
        "files": files,
        "path_index": path_index,
        "counts": {
            "expected": len(files),
            "available": available_count,
            "unavailable": len(files) - available_count,
        },
    }


def annotate_source_references(report: Any, manifest: MutableMapping[str, Any]) -> dict[str, Any]:
    path_index = manifest.setdefault("path_index", {})
    files = manifest.setdefault("files", {})
    locations: dict[str, Any] = manifest.setdefault("locations", {})

    def ensure_source(path: str) -> str:
        source_id = path_index.get(path)
        if source_id:
            return str(source_id)
        source_id = stable_id(
            "source",
            Path(path).name,
            hashlib.sha256(path.encode("utf-8")).hexdigest()[:16],
        )
        path_index[path] = source_id
        files[source_id] = {
            "id": source_id,
            "path": path,
            "roles": ["referenced_but_not_loaded"],
            "available": False,
            "encoding": None,
            "byte_length": None,
            "line_count": None,
            "sha256": None,
            "git_blob_sha1": None,
        }
        return source_id

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if (
                isinstance(value.get("path"), str)
                and isinstance(value.get("line"), int)
                and "symbol" in value
            ):
                path = value["path"]
                source_id = ensure_source(path)
                symbol = str(value.get("symbol") or "")
                line = int(value["line"])
                location_id = stable_id(
                    "location",
                    source_id,
                    line,
                    hashlib.sha256(symbol.encode("utf-8")).hexdigest()[:10],
                )
                value.setdefault("source_ref", source_id)
                value.setdefault("location_ref", location_id)
                value.setdefault("start_line", line)
                value.setdefault("end_line", line)
                value.setdefault("derivation", "source_anchor")
                value.setdefault("confidence", "source_derived")
                locations.setdefault(location_id, {
                    "id": location_id,
                    "source_ref": source_id,
                    "start_line": line,
                    "end_line": line,
                    "symbol": symbol,
                    "anchor": value.get("anchor"),
                    "derivation": value.get("derivation"),
                    "confidence": value.get("confidence"),
                })
            for child in list(value.values()):
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(report)
    return locations


def _warning_category(pointer: str, message: str) -> str:
    lower = (pointer + " " + message).lower()
    if "source" in lower or "unavailable" in lower or "not found" in lower:
        return "source_coverage"
    if "event" in lower:
        return "event_coverage"
    if "header" in lower:
        return "header_coverage"
    if "config" in lower:
        return "config_coverage"
    if "mcp" in lower:
        return "mcp_coverage"
    if "response" in lower:
        return "responses_coverage"
    return "coverage"



def diagnostic_code(category: str, message: str) -> str:
    lower = message.lower()
    if "optional" in lower and ("unavailable" in lower or "missing" in lower):
        return "OPTIONAL_SOURCE_UNAVAILABLE"
    if "required" in lower and ("unavailable" in lower or "missing" in lower):
        return "REQUIRED_SOURCE_UNAVAILABLE"
    if "unclassified" in lower and "event" in lower:
        return "RESPONSE_EVENT_UNCLASSIFIED"
    if "unclassified" in lower and "header" in lower:
        return "HEADER_CANDIDATE_UNCLASSIFIED"
    if "anchor changed" in lower or "anchor" in lower and "failed" in lower:
        if category == "mcp_coverage":
            return "MCP_SOURCE_ANCHOR_CHANGED"
        if category == "config_coverage":
            return "CONFIG_SOURCE_ANCHOR_CHANGED"
        if category == "responses_coverage":
            return "RESPONSES_SOURCE_ANCHOR_CHANGED"
        return "SOURCE_ANCHOR_CHANGED"
    if "not found" in lower or "absent upstream" in lower:
        return "SOURCE_SYMBOL_NOT_FOUND"
    if category == "event_coverage":
        return "RESPONSE_EVENT_COVERAGE_WARNING"
    if category == "header_coverage":
        return "HEADER_COVERAGE_WARNING"
    if category == "config_coverage":
        return "CONFIG_COVERAGE_WARNING"
    if category == "mcp_coverage":
        return "MCP_COVERAGE_WARNING"
    if category == "responses_coverage":
        return "RESPONSES_COVERAGE_WARNING"
    if category == "source_coverage":
        return "SOURCE_COVERAGE_WARNING"
    return "COVERAGE_WARNING"


def collect_structured_diagnostics(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_message: dict[str, dict[str, Any]] = {}
    for pointer, value in walk_json(report):
        if not isinstance(value, dict):
            continue
        for key in ("warnings", "inventory_warnings"):
            warnings = value.get(key)
            if not isinstance(warnings, list):
                continue
            for warning in warnings:
                if not isinstance(warning, str) or not warning.strip():
                    continue
                message = warning.strip()
                record = by_message.get(message)
                if record is None:
                    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:14]
                    category = _warning_category(pointer, message)
                    record = {
                        "id": stable_id("diagnostic", category, digest),
                        "code": diagnostic_code(category, message),
                        "severity": "warning",
                        "category": category,
                        "message": message,
                        "report_pointers": [],
                        "source_refs": [],
                        "strict_failure": True,
                        "recoverable": True,
                    }
                    by_message[message] = record
                warning_pointer = pointer_join("" if pointer == "/" else pointer, key)
                if warning_pointer not in record["report_pointers"]:
                    record["report_pointers"].append(warning_pointer)
                source = value.get("source")
                if isinstance(source, dict):
                    for candidate in source.values() if not source.get("location_ref") else [source]:
                        if isinstance(candidate, dict) and candidate.get("location_ref"):
                            if candidate["location_ref"] not in record["source_refs"]:
                                record["source_refs"].append(candidate["location_ref"])
    manifest = report.get("source_manifest") if isinstance(report.get("source_manifest"), dict) else {}
    manifest_files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    for source_id, source in manifest_files.items():
        if not isinstance(source, dict):
            continue
        path = str(source.get("path") or "")
        if path:
            for record in by_message.values():
                if path in record["message"] and source_id not in record["source_refs"]:
                    record["source_refs"].append(source_id)
        if source.get("available", True):
            continue
        matching = next(
            (record for record in by_message.values() if path and path in record["message"]),
            None,
        )
        if matching is None:
            message = f"expected source unavailable: {path}"
            required = any(str(role).startswith("base:") for role in source.get("roles", []))
            matching = {
                "id": stable_id("diagnostic", "source_coverage", source_id),
                "code": "REQUIRED_SOURCE_UNAVAILABLE" if required else "OPTIONAL_SOURCE_UNAVAILABLE",
                "severity": "error" if required else "warning",
                "category": "source_coverage",
                "message": message,
                "report_pointers": [
                    f"/source_manifest/files/{pointer_escape(str(source_id))}"
                ],
                "source_refs": [source_id],
                "strict_failure": True,
                "recoverable": not required,
            }
            by_message[message] = matching

    diagnostics = sorted(by_message.values(), key=lambda item: item["id"])
    for record in diagnostics:
        record["report_pointers"] = sorted(set(record["report_pointers"]))
        record["source_refs"] = sorted(set(record["source_refs"]))
    return diagnostics


def _field_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and ("name" in item or "wire_name" in item)
        for item in value
    )


def _schema_name_from_object(obj: Mapping[str, Any], pointer: str) -> str | None:
    for key in ("struct", "name", "body_schema", "response_body_schema"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if pointer.endswith("/turn_metadata_schema"):
        return "CodexTurnMetadataPayload"
    return None


def _schema_id(name: str, pointer: str) -> str:
    if "turn_metadata_schema" in pointer and name == "CodexTurnMetadataPayload":
        return "schema.codex.turn_metadata.full"
    simple = re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:<'?[^>]+>)?$", name)
    if simple:
        return stable_id("schema", "rust", name.split("<", 1)[0])
    return stable_id("schema", "view", hashlib.sha256((name + "\0" + pointer).encode("utf-8")).hexdigest()[:14])


def collect_schema_entities(report: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    schemas: dict[str, Any] = {}
    fields: dict[str, Any] = {}

    def add_schema(name: str, field_rows: list[dict[str, Any]], pointer: str, kind: str = "rust_struct") -> str:
        schema_id = _schema_id(name, pointer)
        schema = schemas.setdefault(schema_id, {
            "id": schema_id,
            "name": name,
            "kind": kind,
            "field_ids": [],
            "occurrences": [],
            "source_refs": [],
            "certainty": "source_derived",
        })
        if pointer not in schema["occurrences"]:
            schema["occurrences"].append(pointer)
        for index, field in enumerate(field_rows):
            field_pointer = pointer_join(pointer if pointer != "/" else "", index)
            normalized = normalize_field(field, schema_id, field_pointer)
            field_id = normalized["id"]
            existing = fields.get(field_id)
            if existing is None:
                fields[field_id] = normalized
            else:
                for occurrence in normalized["occurrences"]:
                    if occurrence not in existing["occurrences"]:
                        existing["occurrences"].append(occurrence)
                for source_ref in normalized["source_refs"]:
                    if source_ref not in existing["source_refs"]:
                        existing["source_refs"].append(source_ref)
                if existing["wire_schema"] != normalized["wire_schema"]:
                    existing.setdefault("conflicts", []).append({
                        "pointer": field_pointer,
                        "wire_schema": normalized["wire_schema"],
                        "rust_type": normalized["rust_type"],
                    })
            if field_id not in schema["field_ids"]:
                schema["field_ids"].append(field_id)
            source = field.get("source")
            if isinstance(source, dict) and source.get("location_ref"):
                if source["location_ref"] not in schema["source_refs"]:
                    schema["source_refs"].append(source["location_ref"])
        return schema_id

    # Surface schemas are always addressable, even when they are unions or aliases.
    catalog = report.get("wire_surface_catalog")
    if isinstance(catalog, dict):
        for surface_index, surface in enumerate(catalog.get("surfaces", [])):
            if not isinstance(surface, dict) or not surface.get("id"):
                continue
            for direction in ("request", "response"):
                section = surface.get(direction)
                if not isinstance(section, dict):
                    continue
                field_rows = section.get("body_fields")
                name = section.get("body_schema")
                if isinstance(name, str) and name.strip():
                    pointer = f"/wire_surface_catalog/surfaces/{surface_index}/{direction}/body_fields"
                    schema_id = stable_id("schema", "surface", surface["id"], direction)
                    has_fields = _field_list(field_rows)
                    schema = schemas.setdefault(schema_id, {
                        "id": schema_id,
                        "name": name,
                        "kind": "surface_body",
                        "field_ids": [],
                        "occurrences": [pointer],
                        "source_refs": [],
                        "certainty": "source_derived" if has_fields else "catalog_declared",
                        "opaque": not has_fields,
                    })
                    if has_fields:
                        for index, field in enumerate(field_rows):
                            normalized = normalize_field(field, schema_id, pointer_join(pointer, index))
                            fields.setdefault(normalized["id"], normalized)
                            if normalized["id"] not in schema["field_ids"]:
                                schema["field_ids"].append(normalized["id"])

    for pointer, obj in walk_json(report):
        if not isinstance(obj, dict):
            continue
        if "fields" in obj and _field_list(obj.get("fields")):
            name = _schema_name_from_object(obj, pointer)
            if name:
                add_schema(name, obj["fields"], pointer_join("" if pointer == "/" else pointer, "fields"))
        if "schema" in obj and _field_list(obj.get("schema")):
            name = _schema_name_from_object(obj, pointer) or pointer.rsplit("/", 1)[-1]
            add_schema(name, obj["schema"], pointer_join("" if pointer == "/" else pointer, "schema"), "schema_view")
        if "fixed_fields" in obj and _field_list(obj.get("fixed_fields")):
            add_schema(
                "CodexTurnMetadataPayload",
                obj["fixed_fields"],
                pointer_join("" if pointer == "/" else pointer, "fixed_fields"),
                "canonical_turn_metadata",
            )
        if isinstance(obj.get("variants"), list) and isinstance(obj.get("name"), str):
            schema_id = _schema_id(obj["name"], pointer)
            schema = schemas.setdefault(schema_id, {
                "id": schema_id,
                "name": obj["name"],
                "kind": "rust_enum",
                "field_ids": [],
                "variants": [],
                "occurrences": [],
                "source_refs": [],
                "certainty": "source_derived",
            })
            if pointer not in schema["occurrences"]:
                schema["occurrences"].append(pointer)
            for variant in obj.get("variants", []):
                if not isinstance(variant, dict):
                    continue
                schema["variants"].append({
                    "name": variant.get("name"),
                    "wire_name": variant.get("wire_name"),
                    "shape": variant.get("shape"),
                    "aliases": variant.get("aliases") or [],
                })
                source = variant.get("source")
                if isinstance(source, dict) and source.get("location_ref"):
                    if source["location_ref"] not in schema["source_refs"]:
                        schema["source_refs"].append(source["location_ref"])

    # Preserve every named-type reference as an addressable schema entity, even
    # when the source only exposes an alias, tuple wrapper, macro-generated type,
    # or otherwise opaque payload. This prevents machine consumers from seeing
    # dangling x-entity-ref values.
    referenced_by: dict[str, list[str]] = {}

    def collect_entity_refs(value: Any, field_id: str) -> None:
        if isinstance(value, dict):
            entity_ref = value.get("x-entity-ref")
            if isinstance(entity_ref, str) and entity_ref:
                referenced_by.setdefault(entity_ref, []).append(field_id)
            for child in value.values():
                collect_entity_refs(child, field_id)
        elif isinstance(value, list):
            for child in value:
                collect_entity_refs(child, field_id)

    for field_id, field in fields.items():
        collect_entity_refs(field.get("wire_schema"), field_id)
    for entity_ref, field_ids in referenced_by.items():
        schemas.setdefault(entity_ref, {
            "id": entity_ref,
            "name": entity_ref.rsplit(".", 1)[-1],
            "kind": "named_type_reference",
            "field_ids": [],
            "occurrences": [],
            "source_refs": [],
            "certainty": "inferred",
            "opaque": True,
            "referenced_by_field_ids": sorted(set(field_ids)),
        })

    for schema in schemas.values():
        schema["field_ids"].sort()
        schema["occurrences"].sort()
        schema["source_refs"].sort()
        if "variants" in schema:
            unique = {(str(v.get("wire_name")), str(v.get("name")), str(v.get("shape"))): v for v in schema["variants"]}
            schema["variants"] = [unique[key] for key in sorted(unique)]
    for field in fields.values():
        field["occurrences"].sort()
        field["source_refs"].sort()
    return dict(sorted(schemas.items())), dict(sorted(fields.items()))


def collect_surface_entities(report: Mapping[str, Any]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    catalog = report.get("wire_surface_catalog")
    if not isinstance(catalog, dict):
        return entities
    for index, surface in enumerate(catalog.get("surfaces", [])):
        if not isinstance(surface, dict) or not surface.get("id"):
            continue
        entity_id = stable_id("surface", surface["id"])
        addressing = surface.get("addressing") if isinstance(surface.get("addressing"), dict) else {}
        entities[entity_id] = {
            "id": entity_id,
            "legacy_id": surface["id"],
            "plane": surface.get("plane"),
            "service_family": surface.get("service_family"),
            "category": surface.get("category"),
            "addressing": addressing,
            "method": surface.get("method"),
            "transport": surface.get("transport"),
            "protocol": surface.get("protocol"),
            "streaming": surface.get("streaming"),
            "usage_status": surface.get("usage_status"),
            "request_schema_id": (
                stable_id("schema", "surface", surface["id"], "request")
                if isinstance(surface.get("request"), dict) and surface["request"].get("body_schema")
                else None
            ),
            "response_schema_id": (
                stable_id("schema", "surface", surface["id"], "response")
                if isinstance(surface.get("response"), dict) and surface["response"].get("body_schema")
                else None
            ),
            "profile_refs": surface.get("profiles") or {},
            "source_refs": [surface["source"]["location_ref"]]
            if isinstance(surface.get("source"), dict) and surface["source"].get("location_ref")
            else [],
            "report_pointer": f"/wire_surface_catalog/surfaces/{index}",
        }
    return dict(sorted(entities.items()))


def collect_header_entities(report: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    headers: dict[str, Any] = {}
    templates: dict[str, Any] = {}
    catalog = report.get("wire_surface_catalog")
    if not isinstance(catalog, dict):
        return headers, templates
    for surface_index, surface in enumerate(catalog.get("surfaces", [])):
        if not isinstance(surface, dict):
            continue
        surface_id = stable_id("surface", surface.get("id", surface_index))
        header_block = surface.get("headers") if isinstance(surface.get("headers"), dict) else {}
        for direction, key in (("request", "request_by_category"), ("response", "response_by_category")):
            by_category = header_block.get(key)
            if not isinstance(by_category, dict):
                continue
            for category, rows in by_category.items():
                if not isinstance(rows, list):
                    continue
                for row_index, row in enumerate(rows):
                    if not isinstance(row, dict) or not row.get("name"):
                        continue
                    name = str(row["name"])
                    entity_id = stable_id("header", direction, name.lower())
                    sensitivity, logging_policy = classify_sensitivity(name, str(category))
                    entity = headers.setdefault(entity_id, {
                        "id": entity_id,
                        "canonical_name": name.lower(),
                        "display_names": [],
                        "direction": direction,
                        "case_sensitive": False,
                        "categories": [],
                        "origins": [],
                        "value_shapes": [],
                        "conditions": [],
                        "continuation": False,
                        "authority": "server_or_provider" if direction == "response" else "client_or_codex_core",
                        "lifetime": classify_lifetime(name),
                        "sensitivity": sensitivity,
                        "logging_policy": logging_policy,
                        "occurrences": [],
                        "source_refs": [],
                    })
                    for list_name, value in (
                        ("display_names", name),
                        ("categories", category),
                        ("origins", row.get("origin")),
                        ("value_shapes", row.get("value_shape")),
                    ):
                        if value is not None and value not in entity[list_name]:
                            entity[list_name].append(value)
                    condition = {
                        "text": row.get("condition"),
                        "predicate": condition_to_predicate(row.get("condition")),
                    }
                    if condition not in entity["conditions"]:
                        entity["conditions"].append(condition)
                    entity["continuation"] = entity["continuation"] or bool(row.get("continuation"))
                    pointer = f"/wire_surface_catalog/surfaces/{surface_index}/headers/{key}/{pointer_escape(str(category))}/{row_index}"
                    entity["occurrences"].append({"surface_id": surface_id, "report_pointer": pointer})
                    source = row.get("source")
                    if isinstance(source, dict) and source.get("location_ref"):
                        if source["location_ref"] not in entity["source_refs"]:
                            entity["source_refs"].append(source["location_ref"])
        for direction, key in (("request", "request_templates"), ("response", "response_templates")):
            rows = header_block.get(key)
            if not isinstance(rows, list):
                continue
            for row_index, row in enumerate(rows):
                if not isinstance(row, dict) or not row.get("name_template"):
                    continue
                name = str(row["name_template"])
                entity_id = stable_id("header_template", direction, name)
                templates[entity_id] = {
                    "id": entity_id,
                    "name_template": name,
                    "direction": direction,
                    "parameters": row.get("parameters") or {},
                    "value_shape": row.get("value_shape"),
                    "condition": {
                        "text": row.get("condition"),
                        "predicate": condition_to_predicate(row.get("condition")),
                    },
                    "category": row.get("category"),
                    "origin": row.get("origin"),
                    "surface_id": surface_id,
                    "report_pointer": f"/wire_surface_catalog/surfaces/{surface_index}/headers/{key}/{row_index}",
                    "source_refs": [row["source"]["location_ref"]]
                    if isinstance(row.get("source"), dict) and row["source"].get("location_ref")
                    else [],
                }

    # Transport-generated headers do not belong to one logical endpoint surface.
    # Keep them in the same normalized header registry while recording their
    # redirect/profile occurrence separately from ordinary surface occurrences.
    generated_profiles = catalog.get("transport_generated_header_profiles")
    if isinstance(generated_profiles, dict):
        for profile_name, profile_ref in generated_profiles.items():
            profile = None
            if isinstance(profile_ref, dict):
                ref = profile_ref.get("$ref")
                if isinstance(ref, str) and ref.startswith("#/"):
                    try:
                        candidate = json_pointer_get(report, ref[1:])
                    except (KeyError, IndexError, TypeError, ValueError):
                        candidate = None
                    if isinstance(candidate, dict):
                        profile = candidate
            if not isinstance(profile, dict):
                continue
            header = profile.get("header") if isinstance(profile.get("header"), dict) else {}
            if not header.get("name"):
                continue
            name = str(header["name"])
            direction = str(header.get("direction") or "request")
            category = str(header.get("category") or "transport_generated")
            entity_id = stable_id("header", direction, name.lower())
            sensitivity = str(header.get("sensitivity") or classify_sensitivity(name, category)[0])
            logging_policy = str(
                header.get("logging_policy") or classify_sensitivity(name, category)[1]
            )
            entity = headers.setdefault(entity_id, {
                "id": entity_id,
                "canonical_name": name.lower(),
                "display_names": [],
                "direction": direction,
                "case_sensitive": bool(header.get("case_sensitive", False)),
                "categories": [],
                "origins": [],
                "value_shapes": [],
                "conditions": [],
                "continuation": False,
                "authority": "client_transport",
                "lifetime": "redirect_hop",
                "sensitivity": sensitivity,
                "logging_policy": logging_policy,
                "occurrences": [],
                "source_refs": [],
            })
            for list_name, value in (
                ("display_names", name),
                ("categories", category),
                ("origins", header.get("origin")),
                ("value_shapes", header.get("value_shape")),
            ):
                if value is not None and value not in entity[list_name]:
                    entity[list_name].append(value)
            entity["transport_generated"] = True
            entity["first_request_generated"] = bool(header.get("first_request_generated"))
            entity["redirect_request_generated"] = bool(header.get("redirect_request_generated"))
            entity.setdefault("transport_profile_ids", [])
            for subprofile_index, subprofile in enumerate(profile.get("profiles", [])):
                if not isinstance(subprofile, dict):
                    continue
                subprofile_id = stable_id(
                    "transport_header_profile",
                    subprofile.get("id") or profile_name,
                )
                if subprofile_id not in entity["transport_profile_ids"]:
                    entity["transport_profile_ids"].append(subprofile_id)
                occurrence = {
                    "transport_profile_id": subprofile_id,
                    "report_pointer": f"/http_redirect_protocol/profiles/{subprofile_index}",
                }
                if occurrence not in entity["occurrences"]:
                    entity["occurrences"].append(occurrence)
                source = subprofile.get("source")
                if isinstance(source, dict) and source.get("location_ref"):
                    if source["location_ref"] not in entity["source_refs"]:
                        entity["source_refs"].append(source["location_ref"])
            condition = {
                "text": "generated on accepted HTTP redirect hops; not generated by this profile on the first request",
                "predicate": {
                    "op": "source_condition",
                    "expression": "transport.event == http_redirect_follow && request.hop_index > 0",
                    "machine_evaluable": False,
                },
            }
            if condition not in entity["conditions"]:
                entity["conditions"].append(condition)

    for entity in headers.values():
        for key in ("display_names", "categories", "origins", "value_shapes", "source_refs"):
            entity[key] = sorted(entity[key], key=str)
        if "transport_profile_ids" in entity:
            entity["transport_profile_ids"] = sorted(entity["transport_profile_ids"], key=str)
        entity["occurrences"].sort(
            key=lambda item: (
                str(item.get("surface_id") or item.get("transport_profile_id") or ""),
                str(item.get("report_pointer") or ""),
            )
        )
    return dict(sorted(headers.items())), dict(sorted(templates.items()))


def collect_transport_header_profile_entities(report: Mapping[str, Any]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    protocol = report.get("http_redirect_protocol")
    if not isinstance(protocol, dict):
        return entities
    header = protocol.get("header") if isinstance(protocol.get("header"), dict) else {}
    header_id = stable_id(
        "header",
        str(header.get("direction") or "request"),
        str(header.get("canonical_name") or header.get("name") or "").lower(),
    )
    for index, profile in enumerate(protocol.get("profiles", [])):
        if not isinstance(profile, dict):
            continue
        entity_id = stable_id(
            "transport_header_profile",
            profile.get("id") or index,
        )
        source_refs = []
        source = profile.get("source")
        if isinstance(source, dict) and source.get("location_ref"):
            source_refs.append(source["location_ref"])
        entities[entity_id] = {
            "id": entity_id,
            "header_id": header_id,
            "transport_family": profile.get("transport_family"),
            "applies_when": {
                "text": profile.get("applies_when"),
                "predicate": condition_to_predicate(profile.get("applies_when")),
            },
            "redirect_statuses": profile.get("redirect_statuses") or [],
            "first_request_generated": bool(profile.get("first_request_generated")),
            "redirect_request_generated": bool(profile.get("redirect_request_generated")),
            "stale_header_policy": profile.get("stale_header_policy"),
            "same_origin_value": profile.get("same_origin_value"),
            "cross_origin_value": profile.get("cross_origin_value"),
            "https_to_http_downgrade": profile.get("https_to_http_downgrade"),
            "redirect_origin_policy": profile.get("redirect_origin_policy"),
            "plaintext_redirect_policy": profile.get("plaintext_redirect_policy"),
            "source_checks": profile.get("source_checks") or {},
            "source_refs": source_refs,
            "report_pointer": f"/http_redirect_protocol/profiles/{index}",
        }
    return dict(sorted(entities.items()))


def collect_setting_entities(report: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    settings: dict[str, Any] = {}
    edges: list[dict[str, Any]] = []

    def normalize_effects(
        setting_id: str,
        raw_effects: Any,
        *,
        relation: str = "affects",
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, effect in enumerate(raw_effects if isinstance(raw_effects, list) else []):
            if not isinstance(effect, dict):
                continue
            target_path = str(effect.get("path") or effect.get("target") or "unknown")
            target_id = stable_id("wire_location", target_path)
            structured = {
                "layer": effect.get("layer"),
                "target": target_path,
                "target_id": target_id,
                "behavior": effect.get("behavior") or effect.get("effect"),
                "condition": {
                    "text": effect.get("condition"),
                    "predicate": condition_to_predicate(effect.get("condition")),
                },
            }
            normalized.append(structured)
            edges.append({
                "id": stable_id("edge", setting_id, relation, target_id, index),
                "from": setting_id,
                "to": target_id,
                "relation": relation,
                "rule": structured,
            })
        return normalized

    config = report.get("config_protocol")
    if isinstance(config, dict):
        for row in config.get("wire_affecting_settings", []):
            if not isinstance(row, dict) or not row.get("setting"):
                continue
            setting_name = str(row["setting"])
            setting_id = stable_id("setting", "config", setting_name)
            effects = normalize_effects(setting_id, row.get("wire_effects"))
            source_refs = []
            for key in ("source", "runtime_source"):
                source = row.get(key)
                if isinstance(source, dict) and source.get("location_ref"):
                    source_refs.append(source["location_ref"])
            settings[setting_id] = {
                "id": setting_id,
                "name": setting_name,
                "syntax": row.get("syntax"),
                "owner": row.get("owner"),
                "category": row.get("category"),
                "configured_type": row.get("configured_type"),
                "effective_runtime": row.get("effective_runtime"),
                "effective_type": row.get("effective_type"),
                "wire_effects": effects,
                "notes": row.get("notes") or [],
                "source_refs": sorted(set(source_refs)),
            }
        for row in config.get("wire_affecting_features", []):
            if not isinstance(row, dict) or not row.get("feature_id"):
                continue
            key = str(row.get("config_key") or row["feature_id"])
            setting_id = stable_id("setting", "feature", key)
            source_refs = []
            source = row.get("source")
            if isinstance(source, dict) and source.get("location_ref"):
                source_refs.append(source["location_ref"])
            settings[setting_id] = {
                "id": setting_id,
                "name": key,
                "feature_id": row.get("feature_id"),
                "category": "feature",
                "stage": row.get("stage"),
                "default_enabled": row.get("default_enabled"),
                "default_enabled_expression": row.get("default_enabled_expression"),
                "wire_effects": normalize_effects(setting_id, row.get("wire_effects")),
                "source_refs": source_refs,
            }

    # MCP settings are first-class machine entities rather than being left only
    # inside the human-oriented protocol section.
    mcp = report.get("mcp_protocol")
    configuration = mcp.get("configuration") if isinstance(mcp, dict) and isinstance(mcp.get("configuration"), dict) else {}
    target_by_name = {
        "command": "mcp.transport.stdio.command",
        "args": "mcp.transport.stdio.arguments",
        "env": "mcp.transport.stdio.environment",
        "env_vars": "mcp.transport.stdio.environment_sources",
        "cwd": "mcp.transport.stdio.cwd",
        "url": "mcp.transport.streamable_http.url",
        "bearer_token_env_var": "mcp.transport.streamable_http.authorization",
        "http_headers": "mcp.transport.streamable_http.headers",
        "env_http_headers": "mcp.transport.streamable_http.environment_headers",
        "http_headers_helper": "mcp.transport.streamable_http.dynamic_headers",
        "auth": "mcp.authentication.mode",
        "environment_id": "mcp.runtime.environment",
        "enabled": "mcp.lifecycle.enabled",
        "required": "mcp.lifecycle.required",
        "supports_parallel_tool_calls": "responses.tools.parallel_capability",
        "omit_tools_from": "responses.tools.exposure_policy",
        "startup_timeout_sec": "mcp.lifecycle.startup_timeout",
        "startup_timeout_ms": "mcp.lifecycle.startup_timeout",
        "tool_timeout_sec": "mcp.lifecycle.tool_timeout",
        "default_tools_approval_mode": "mcp.approval.default",
        "enabled_tools": "responses.tools.mcp_allowlist",
        "disabled_tools": "responses.tools.mcp_denylist",
        "scopes": "mcp.authentication.oauth_scopes",
        "oauth": "mcp.authentication.oauth_client",
        "oauth_resource": "mcp.authentication.oauth_resource",
        "tools": "mcp.tool_overrides",
        "approval_mode": "mcp.tool.approval_mode",
        "output_token_limit": "mcp.tool.output_token_limit",
    }

    def mcp_source_refs(row: Mapping[str, Any]) -> list[str]:
        source = row.get("source")
        return [source["location_ref"]] if isinstance(source, dict) and source.get("location_ref") else []

    def add_mcp_setting(
        *,
        scope: str,
        path: str,
        name: str,
        row: Mapping[str, Any],
        category: str,
        meaning: str | None = None,
        input_status: str = "accepted",
    ) -> None:
        setting_id = stable_id("setting", "mcp", scope, name)
        target = target_by_name.get(name, f"mcp.configuration.{name}")
        effects = normalize_effects(
            setting_id,
            [{
                "layer": target.split(".", 1)[0],
                "path": target,
                "behavior": meaning or row.get("meaning") or "configures MCP behavior",
                "condition": None,
            }],
            relation="configures",
        )
        settings[setting_id] = {
            "id": setting_id,
            "name": path,
            "wire_name": row.get("wire_name") or name,
            "field_name": row.get("name") or name,
            "category": category,
            "configured_type": row.get("type"),
            "optional": row.get("optional"),
            "serialized": row.get("serialized", True),
            "input_status": input_status,
            "meaning": meaning or row.get("meaning"),
            "wire_effects": effects,
            "source_refs": mcp_source_refs(row),
        }

    server = configuration.get("server") if isinstance(configuration.get("server"), dict) else {}
    for row in server.get("settings", []):
        if not isinstance(row, dict) or not row.get("name"):
            continue
        name = str(row["name"])
        add_mcp_setting(
            scope="server",
            path=str(row.get("setting") or f"mcp_servers.<server>.{row.get('wire_name') or name}"),
            name=name,
            row=row,
            category="mcp_server",
        )

    raw = configuration.get("raw_toml") if isinstance(configuration.get("raw_toml"), dict) else {}
    existing_paths = {str(setting.get("name")) for setting in settings.values()}
    for row in raw.get("fields", []):
        if not isinstance(row, dict) or not (row.get("wire_name") or row.get("name")):
            continue
        name = str(row.get("name") or row.get("wire_name"))
        wire_name = str(row.get("wire_name") or name)
        path = f"mcp_servers.<server>.{wire_name}"
        if path in existing_paths:
            continue
        input_status = {
            "bearer_token": "rejected_validation_only",
            "_name": "accepted_legacy_ignored",
            "startup_timeout_ms": "accepted_legacy_alias",
        }.get(name, "accepted")
        category = (
            "mcp_transport"
            if name in {
                "command", "args", "env", "env_vars", "cwd", "url",
                "bearer_token", "bearer_token_env_var", "http_headers",
                "env_http_headers", "http_headers_helper",
            }
            else "mcp_server"
        )
        add_mcp_setting(
            scope="raw",
            path=path,
            name=name,
            row=row,
            category=category,
            input_status=input_status,
        )
        existing_paths.add(path)

    per_tool = configuration.get("per_tool") if isinstance(configuration.get("per_tool"), dict) else {}
    for row in per_tool.get("fields", []):
        if not isinstance(row, dict) or not (row.get("wire_name") or row.get("name")):
            continue
        name = str(row.get("name") or row.get("wire_name"))
        wire_name = str(row.get("wire_name") or name)
        add_mcp_setting(
            scope="tool",
            path=f"mcp_servers.<server>.tools.<tool>.{wire_name}",
            name=name,
            row=row,
            category="mcp_tool",
        )

    oauth = configuration.get("oauth") if isinstance(configuration.get("oauth"), dict) else {}
    for row in oauth.get("fields", []):
        if not isinstance(row, dict) or not (row.get("wire_name") or row.get("name")):
            continue
        name = str(row.get("name") or row.get("wire_name"))
        wire_name = str(row.get("wire_name") or name)
        add_mcp_setting(
            scope="oauth",
            path=f"mcp_servers.<server>.oauth.{wire_name}",
            name=name,
            row=row,
            category="mcp_oauth",
        )

    return dict(sorted(settings.items())), sorted(edges, key=lambda edge: edge["id"])

def collect_event_entities(report: Mapping[str, Any]) -> dict[str, Any]:
    protocol = report.get("responses_protocol")
    if not isinstance(protocol, dict):
        return {}
    stream = protocol.get("response_stream")
    if not isinstance(stream, dict):
        return {}
    events: dict[str, Any] = {}
    dispatch_by_event = {
        str(row.get("event")): row
        for row in stream.get("dispatch", [])
        if isinstance(row, dict) and row.get("event")
    }
    names = set(str(name) for name in stream.get("event_kinds_discovered", []) if name)
    names.update(dispatch_by_event)
    for name in sorted(names):
        row = dispatch_by_event.get(name, {})
        event_id = stable_id("event", "responses", name)
        source_refs = []
        source = row.get("source") if isinstance(row, dict) else None
        if isinstance(source, dict) and source.get("location_ref"):
            source_refs.append(source["location_ref"])
        events[event_id] = {
            "id": event_id,
            "name": name,
            "protocol": "Responses",
            "disposition": row.get("disposition", "discovered_not_classified"),
            "emitted_event": row.get("emitted_event"),
            "mapped_errors": row.get("mapped_errors") or [],
            "source_refs": source_refs,
        }
    return events


def collect_wire_locations(report: Mapping[str, Any]) -> dict[str, Any]:
    locations: dict[str, Any] = {}
    relation = report.get("relation_map")
    if isinstance(relation, dict):
        for group in relation.get("groups", []):
            if not isinstance(group, dict):
                continue
            canonical = group.get("canonical")
            for path in [canonical] + [node.get("path") for node in group.get("nodes", []) if isinstance(node, dict)]:
                if not path:
                    continue
                location_id = stable_id("wire_location", path)
                locations.setdefault(location_id, {
                    "id": location_id,
                    "path": path,
                    "concepts": [],
                })
                concept = group.get("concept")
                if concept and concept not in locations[location_id]["concepts"]:
                    locations[location_id]["concepts"].append(concept)
    config = report.get("config_protocol")
    if isinstance(config, dict):
        for row in config.get("wire_affecting_settings", []):
            if not isinstance(row, dict):
                continue
            for effect in row.get("wire_effects", []):
                if isinstance(effect, dict) and effect.get("path"):
                    path = str(effect["path"])
                    location_id = stable_id("wire_location", path)
                    locations.setdefault(location_id, {"id": location_id, "path": path, "concepts": []})
    for item in locations.values():
        item["concepts"].sort()
    return dict(sorted(locations.items()))


def collect_rules(report: Mapping[str, Any]) -> dict[str, Any]:
    rules: dict[str, Any] = {}

    def add(rule_id: str, rule: dict[str, Any]) -> None:
        rule = dict(rule)
        rule["id"] = rule_id
        rules[rule_id] = rule

    lite = report.get("responses_lite_protocol")
    if isinstance(lite, dict):
        for index, row in enumerate(lite.get("request_rewrite", [])):
            if not isinstance(row, dict):
                continue
            concept = str(row.get("concept") or index)
            rule_id = stable_id("rule", "responses_lite", concept)
            add(rule_id, {
                "kind": "transformation",
                "when": {"op": "eq", "path": "model.use_responses_lite", "value": True},
                "target": concept,
                "before": {"mode": "full_responses", "value": row.get("full_responses")},
                "after": {"mode": "responses_lite", "value": row.get("responses_lite")},
                "description": f"Responses Lite rewrite for {concept}",
                "source_refs": [],
            })
        tool_gate = lite.get("tool_namespace_metadata")
        if isinstance(tool_gate, dict):
            add("rule.responses_lite.tool_namespace_metadata", {
                "kind": "emission_gate",
                "when": {
                    "op": "all",
                    "args": [
                        {"op": "eq", "path": "config.tool_registry.turn_metadata_includes_tool_info", "value": True},
                        {"op": "eq", "path": "model.use_responses_lite", "value": True},
                    ],
                },
                "target": tool_gate.get("wire_path"),
                "action": "emit",
                "description": tool_gate.get("contents"),
                "source_refs": [tool_gate["source"]["location_ref"]]
                if isinstance(tool_gate.get("source"), dict) and tool_gate["source"].get("location_ref")
                else [],
            })

    metadata = report.get("metadata_protocol")
    if isinstance(metadata, dict):
        flattened = metadata.get("flattened_extra")
        if isinstance(flattened, dict):
            add("rule.metadata.flattened_extra", {
                "kind": "flatten",
                "when": {"op": "accepted_by_extra_metadata_filter"},
                "source": "CodexResponsesMetadata.extra",
                "target": flattened.get("wire_path"),
                "action": "merge_at_object_root",
                "precedence": flattened.get("precedence") or flattened.get("collision_precedence"),
                "wire_wrapper": flattened.get("wire_wrapper"),
                "constraints": flattened.get("validation") or {},
                "accepted_inputs": flattened.get("accepted_inputs") or [],
                "merge_algorithm": [
                    {"order": 1, "action": "filter_reserved_keys", "input": "turn/start responsesapiClientMetadata"},
                    {"order": 2, "action": "filter_reserved_keys_and_validate_bounds", "input": "config.responses_api_metadata"},
                    {"order": 3, "action": "remove_client_keys_shadowed_by_config"},
                    {"order": 4, "action": "append_config_values"},
                    {"order": 5, "action": "serde_flatten_into_turn_object_root"},
                ],
                "description": "Accepted extra metadata is flattened into the root of the nested turn metadata object.",
                "source_refs": [],
            })
        mcp = metadata.get("mcp_projection")
        if isinstance(mcp, dict):
            identity_effect = mcp.get("identity_effect") if isinstance(mcp.get("identity_effect"), dict) else {}
            removed_fields = list(dict.fromkeys(
                list(identity_effect.get("therefore_omitted") or [])
                + list(mcp.get("explicitly_removed") or [])
            ))
            added_rows = (
                mcp.get("explicitly_added_or_overridden")
                or mcp.get("added_or_overridden")
                or []
            )
            source_refs = []
            for source_key in ("source", "attachment_source", "hook_attachment_source"):
                source = mcp.get(source_key)
                if isinstance(source, dict) and source.get("location_ref"):
                    source_refs.append(source["location_ref"])
            add("rule.metadata.mcp_projection", {
                "kind": "projection",
                "when": {"op": "eq", "path": "transport.protocol", "value": "mcp"},
                "source": 'client_metadata["x-codex-turn-metadata"]',
                "target": '_meta["x-codex-turn-metadata"]',
                "wire_type": mcp.get("wire_type"),
                "removed_fields": removed_fields,
                "added_or_overridden_fields": added_rows,
                "identity_effect": identity_effect,
                "extra_behavior": mcp.get("extra_behavior") or {},
                "description": "MCP uses an object-valued projection with deliberate identity, lineage, tool-inventory, and runtime-field changes.",
                "source_refs": sorted(set(source_refs)),
            })

    redirect = report.get("http_redirect_protocol")
    if isinstance(redirect, dict):
        profiles = [row for row in redirect.get("profiles", []) if isinstance(row, dict)]
        all_refs: list[str] = []
        for profile in profiles:
            source = profile.get("source")
            if isinstance(source, dict) and source.get("location_ref"):
                all_refs.append(source["location_ref"])
        add("rule.http_redirect.referer_emit", {
            "kind": "emission_gate",
            "when": {
                "op": "eq",
                "path": "transport.event",
                "value": "http_redirect_follow",
            },
            "target": "request.header.referer",
            "action": "remove_stale_then_recompute",
            "first_request_generated": False,
            "description": "Codex-generated Referer is a redirect-hop transport header, not a normal first-hop Responses header.",
            "source_refs": sorted(set(all_refs)),
        })
        general = next(
            (row for row in profiles if row.get("id") == "route_aware_http_redirect_referer"),
            None,
        )
        if isinstance(general, dict):
            source_refs = []
            source = general.get("source")
            if isinstance(source, dict) and source.get("location_ref"):
                source_refs.append(source["location_ref"])
            add("rule.http_redirect.referer_general_value", {
                "kind": "transformation",
                "when": {"op": "eq", "path": "transport.redirect_profile", "value": "route_aware_http"},
                "source": "previous_request.url",
                "target": "request.header.referer",
                "same_origin": general.get("same_origin_value"),
                "cross_origin": general.get("cross_origin_value"),
                "https_to_http_downgrade": general.get("https_to_http_downgrade"),
                "description": "General route-aware redirects derive Referer from the previous URL with strict-origin reduction for cross-origin hops and downgrade suppression.",
                "source_refs": source_refs,
            })
        mcp_redirect = next(
            (row for row in profiles if row.get("id") == "mcp_same_origin_redirect_referer"),
            None,
        )
        if isinstance(mcp_redirect, dict):
            source_refs = []
            source = mcp_redirect.get("source")
            if isinstance(source, dict) and source.get("location_ref"):
                source_refs.append(source["location_ref"])
            add("rule.http_redirect.referer_mcp_value", {
                "kind": "transformation",
                "when": {"op": "eq", "path": "transport.redirect_profile", "value": "mcp_streamable_http"},
                "source": "previous_request.url",
                "target": "request.header.referer",
                "same_origin": mcp_redirect.get("same_origin_value"),
                "cross_origin": mcp_redirect.get("cross_origin_value"),
                "redirect_origin_policy": mcp_redirect.get("redirect_origin_policy"),
                "description": "MCP Streamable HTTP only follows same-origin redirects and derives Referer from the previous accepted hop.",
                "source_refs": source_refs,
            })

    relation = report.get("relation_map")
    if isinstance(relation, dict):
        for group in relation.get("groups", []):
            if not isinstance(group, dict):
                continue
            concept = str(group.get("concept") or "unknown")
            for index, node in enumerate(group.get("nodes", [])):
                if not isinstance(node, dict):
                    continue
                rule_id = stable_id("rule", "relation", concept, index)
                add(rule_id, {
                    "kind": "relationship",
                    "relation": node.get("relation"),
                    "from": group.get("canonical"),
                    "to": node.get("path"),
                    "surface": node.get("surface"),
                    "optional": node.get("optional"),
                    "description": node.get("note"),
                    "source_refs": [],
                })

    return dict(sorted(rules.items()))


def build_edges(
    surfaces: Mapping[str, Any],
    schemas: Mapping[str, Any],
    fields: Mapping[str, Any],
    rules: Mapping[str, Any],
    relation_map: Mapping[str, Any] | None,
    setting_edges: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    edges: dict[str, dict[str, Any]] = {}

    def add(edge: Mapping[str, Any]) -> None:
        edge_id = str(edge["id"])
        edges[edge_id] = dict(edge)

    for surface_id, surface in surfaces.items():
        for direction, key in (("request", "request_schema_id"), ("response", "response_schema_id")):
            schema_id = surface.get(key)
            if schema_id and schema_id in schemas:
                add({
                    "id": stable_id("edge", surface_id, f"uses_{direction}_schema", schema_id),
                    "from": surface_id,
                    "to": schema_id,
                    "relation": f"uses_{direction}_schema",
                })
    for schema_id, schema in schemas.items():
        for field_id in schema.get("field_ids", []):
            if field_id in fields:
                add({
                    "id": stable_id("edge", schema_id, "contains", field_id),
                    "from": schema_id,
                    "to": field_id,
                    "relation": "contains",
                })
    for rule_id, rule in rules.items():
        for key, orientation in (
            ("from", "inbound"),
            ("source", "inbound"),
            ("to", "outbound"),
            ("target", "outbound"),
        ):
            value = rule.get(key)
            if not isinstance(value, str) or not value:
                continue
            target_id = (
                value
                if value in surfaces or value in schemas or value in fields
                else stable_id("wire_location", value)
            )
            outbound = orientation == "outbound"
            add({
                "id": stable_id("edge", rule_id, key, target_id),
                "from": rule_id if outbound else target_id,
                "to": target_id if outbound else rule_id,
                "relation": f"rule_{key}",
            })
    for edge in setting_edges:
        add(edge)
    if isinstance(relation_map, dict):
        for group in relation_map.get("groups", []):
            if not isinstance(group, dict) or not group.get("canonical"):
                continue
            source_id = stable_id("wire_location", group["canonical"])
            for index, node in enumerate(group.get("nodes", [])):
                if not isinstance(node, dict) or not node.get("path"):
                    continue
                target_id = stable_id("wire_location", node["path"])
                add({
                    "id": stable_id("edge", "relation", group.get("concept"), index),
                    "from": source_id,
                    "to": target_id,
                    "relation": node.get("relation") or "related_to",
                    "surface": node.get("surface"),
                    "optional": bool(node.get("optional")),
                    "description": node.get("note"),
                })
    return [edges[key] for key in sorted(edges)]


def build_machine_contract(report: Mapping[str, Any]) -> dict[str, Any]:
    schemas, fields = collect_schema_entities(report)
    surfaces = collect_surface_entities(report)
    headers, header_templates = collect_header_entities(report)
    transport_header_profiles = collect_transport_header_profile_entities(report)
    settings, setting_edges = collect_setting_entities(report)
    events = collect_event_entities(report)
    wire_locations = collect_wire_locations(report)
    rules = collect_rules(report)
    for rule in rules.values():
        for key in ("from", "to", "source", "target"):
            value = rule.get(key)
            if not isinstance(value, str) or not value:
                continue
            location_id = stable_id("wire_location", value)
            wire_locations.setdefault(
                location_id,
                {"id": location_id, "path": value, "concepts": []},
            )
    edges = build_edges(
        surfaces,
        schemas,
        fields,
        rules,
        report.get("relation_map") if isinstance(report.get("relation_map"), dict) else None,
        setting_edges,
    )
    for profile_id, profile in transport_header_profiles.items():
        header_id = profile.get("header_id")
        if isinstance(header_id, str) and header_id in headers:
            edges.append({
                "id": stable_id("edge", profile_id, "emits", header_id),
                "from": profile_id,
                "to": header_id,
                "relation": "emits_transport_header",
            })
    edges = sorted({str(edge["id"]): edge for edge in edges}.values(), key=lambda edge: str(edge["id"]))

    # Ensure rule and setting targets have addressable wire-location entities.
    for edge in edges:
        for endpoint in (edge.get("from"), edge.get("to")):
            if isinstance(endpoint, str) and endpoint.startswith("wire_location."):
                wire_locations.setdefault(endpoint, {"id": endpoint, "path": None, "concepts": []})

    entities = {
        "surfaces": surfaces,
        "schemas": schemas,
        "fields": fields,
        "headers": headers,
        "header_templates": header_templates,
        "transport_header_profiles": transport_header_profiles,
        "settings": settings,
        "events": events,
        "rules": rules,
        "wire_locations": dict(sorted(wire_locations.items())),
    }
    return {
        "schema_version": MACHINE_CONTRACT_VERSION,
        "entities": entities,
        "edges": edges,
        "views": {
            "responses_full": {
                "protocol_ref": "#/responses_protocol",
                "surface_ids": [entity_id for entity_id, surface in surfaces.items() if surface.get("category") == "inference.responses"],
            },
            "responses_lite": {
                "protocol_ref": "#/responses_lite_protocol",
                "rule_ids": [rule_id for rule_id in rules if rule_id.startswith("rule.responses_lite")],
            },
            "mcp": {
                "protocol_ref": "#/mcp_protocol",
                "surface_ids": [entity_id for entity_id, surface in surfaces.items() if "mcp" in str(surface.get("category", ""))],
            },
            "http_redirects": {
                "protocol_ref": "#/http_redirect_protocol",
                "header_ids": [stable_id("header", "request", "referer")]
                if stable_id("header", "request", "referer") in headers
                else [],
                "transport_header_profile_ids": sorted(transport_header_profiles),
            },
            "client_metadata": {
                "protocol_ref": "#/metadata_protocol",
                "canonical_wire_location": stable_id("wire_location", 'client_metadata["x-codex-turn-metadata"]'),
            },
            "configuration": {
                "protocol_ref": "#/config_protocol",
                "setting_ids": sorted(settings),
            },
        },
        "counts": {
            category: len(items)
            for category, items in entities.items()
        } | {"edges": len(edges)},
    }


def diagnostic_summary(diagnostics: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    summary = {"error": 0, "warning": 0, "info": 0}
    for diagnostic in diagnostics:
        severity = str(diagnostic.get("severity") or "warning")
        summary[severity] = summary.get(severity, 0) + 1
    return summary


def upgrade_report(
    report: MutableMapping[str, Any],
    *,
    commit: Mapping[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    source_mode: str,
    repository: str,
    requested_ref: str,
    dirty: bool | None = None,
    parser_backend: str = "regex",
) -> MutableMapping[str, Any]:
    report["$schema"] = REPORT_SCHEMA_ID
    report["$id"] = f"urn:codex-wire-audit:{stable_slug(repository)}:{commit.get('sha', 'unknown')}"
    report["format_version"] = REPORT_FORMAT_VERSION
    report["versions"] = {
        "report_format": REPORT_FORMAT_VERSION,
        "generator": GENERATOR_VERSION,
        "wire_surface_catalog": str(report.get("catalog_schema_version") or "10"),
        "model_transport_matrix": str(
            (report.get("wire_surface_catalog") or {}).get("model_facing_transport_matrix", {}).get("schema_version", "7")
            if isinstance(report.get("wire_surface_catalog"), dict)
            else "7"
        ),
        "config_protocol": str((report.get("config_protocol") or {}).get("schema_version", "1")),
        "mcp_protocol": str((report.get("mcp_protocol") or {}).get("schema_version", "1")),
        "responses_protocol": str((report.get("responses_protocol") or {}).get("schema_version", "1")),
        "responses_lite_protocol": str((report.get("responses_lite_protocol") or {}).get("schema_version", "1")),
        "metadata_protocol": str((report.get("metadata_protocol") or {}).get("schema_version", "1")),
    }
    report["reader_compatibility"] = {
        "minimum_report_format": "10.0.0",
        "breaking_change_policy": "semantic-versioning",
        "legacy_sections_preserved": True,
    }
    manifest = build_source_manifest(
        commit,
        sources,
        source_mode=source_mode,
        repository=repository,
        requested_ref=requested_ref,
        dirty=dirty,
    )
    report["source_manifest"] = manifest
    annotate_source_references(report, manifest)
    report["parser"] = {
        "requested_backend": parser_backend,
        "effective_backend": "regex",
        "fallback_used": parser_backend not in {"regex", "auto"},
        "note": "v10 exposes the parser contract; this distribution ships the hardened standard-library Rust tokenizer/regex extractor.",
    }
    machine = build_machine_contract(report)
    report["entities"] = machine["entities"]
    report["edges"] = machine["edges"]
    report["views"] = machine["views"]
    report["machine_contract"] = {
        "schema_version": machine["schema_version"],
        "entities_ref": "#/entities",
        "edges_ref": "#/edges",
        "views_ref": "#/views",
        "counts": machine["counts"],
    }
    diagnostics = collect_structured_diagnostics(report)
    report["diagnostics"] = diagnostics
    report["diagnostic_summary"] = diagnostic_summary(diagnostics)
    coverage_diagnostics = [
        diagnostic
        for diagnostic in diagnostics
        if str(diagnostic.get("category", "")).endswith("coverage")
        or diagnostic.get("category") == "source_coverage"
    ]
    manifest_counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    source_complete = (
        not any(
            diagnostic.get("category") == "source_coverage"
            for diagnostic in diagnostics
        )
        and manifest_counts.get("unavailable", 0) == 0
    )
    coverage_complete = not coverage_diagnostics
    complete = source_complete and coverage_complete
    severity_rank = {"info": 1, "warning": 2, "error": 3}
    highest_severity = max(
        (str(diagnostic.get("severity") or "info") for diagnostic in diagnostics),
        key=lambda severity: severity_rank.get(severity, 0),
        default="none",
    )
    report["status"] = {
        "state": "complete" if complete else "partial_with_diagnostics",
        "complete": complete,
        "core_report_built": True,
        "source_complete": source_complete,
        "coverage_complete": coverage_complete,
        "highest_severity": highest_severity,
        "diagnostic_counts": report["diagnostic_summary"],
        "diagnostic_refs": [diagnostic["id"] for diagnostic in diagnostics],
        "source_mode": source_mode,
        "dirty_source": dirty,
        "source_counts": manifest_counts,
    }
    report["schema_documents"] = {
        "report": REPORT_SCHEMA_ID,
        "diagnostic": DIAGNOSTIC_SCHEMA_ID,
        "wire_field": WIRE_FIELD_SCHEMA_ID,
        "rule": RULE_SCHEMA_ID,
        "predicate": PREDICATE_SCHEMA_ID,
        "edge": EDGE_SCHEMA_ID,
        "source": SOURCE_SCHEMA_ID,
    }
    attach_integrity(report)
    return report


def validate_report(report: Mapping[str, Any], *, verify_digest: bool = True) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []

    def error(code: str, message: str, pointer: str = "/") -> None:
        diagnostics.append({
            "id": stable_id("diagnostic", "validation", code, pointer),
            "code": code,
            "severity": "error",
            "category": "report_validation",
            "message": message,
            "report_pointers": [pointer],
            "source_refs": [],
            "strict_failure": True,
            "recoverable": False,
        })

    required = {
        "$schema": str,
        "$id": str,
        "format_version": str,
        "versions": dict,
        "reader_compatibility": dict,
        "source_manifest": dict,
        "parser": dict,
        "entities": dict,
        "edges": list,
        "views": dict,
        "machine_contract": dict,
        "coverage": dict,
        "diagnostics": list,
        "diagnostic_summary": dict,
        "status": dict,
        "schema_documents": dict,
        "integrity": dict,
        "wire_surface_catalog": dict,
    }
    for key, expected_type in required.items():
        if key not in report:
            error("REPORT_REQUIRED_PROPERTY_MISSING", f"required report property is missing: {key}", f"/{pointer_escape(key)}")
        elif not isinstance(report[key], expected_type):
            error("REPORT_PROPERTY_TYPE_INVALID", f"{key} must be {expected_type.__name__}", f"/{pointer_escape(key)}")

    if report.get("$schema") != REPORT_SCHEMA_ID:
        error("REPORT_SCHEMA_ID_UNSUPPORTED", f"expected $schema {REPORT_SCHEMA_ID!r}", "/$schema")
    if report.get("format_version") != REPORT_FORMAT_VERSION:
        error("REPORT_FORMAT_VERSION_UNSUPPORTED", f"expected format_version {REPORT_FORMAT_VERSION!r}", "/format_version")

    entities = report.get("entities") if isinstance(report.get("entities"), dict) else {}
    entity_ids: set[str] = set()
    for category, category_entities in entities.items():
        if not isinstance(category_entities, dict):
            error("ENTITY_CATEGORY_TYPE_INVALID", f"entities.{category} must be an object", f"/entities/{pointer_escape(str(category))}")
            continue
        for key, entity in category_entities.items():
            if not isinstance(entity, dict):
                error("ENTITY_TYPE_INVALID", f"entity {key} must be an object", f"/entities/{pointer_escape(str(category))}/{pointer_escape(str(key))}")
                continue
            entity_id = entity.get("id")
            if entity_id != key:
                error("ENTITY_ID_KEY_MISMATCH", f"entity key {key!r} does not match id {entity_id!r}", f"/entities/{pointer_escape(str(category))}/{pointer_escape(str(key))}/id")
            if key in entity_ids:
                error("ENTITY_ID_DUPLICATE", f"duplicate entity id: {key}", f"/entities/{pointer_escape(str(category))}/{pointer_escape(str(key))}")
            entity_ids.add(str(key))

    rule_ids = set((entities.get("rules") or {}).keys()) if isinstance(entities.get("rules"), dict) else set()
    known_ids = entity_ids | rule_ids
    schemas = entities.get("schemas") if isinstance(entities.get("schemas"), dict) else {}
    fields = entities.get("fields") if isinstance(entities.get("fields"), dict) else {}
    surfaces = entities.get("surfaces") if isinstance(entities.get("surfaces"), dict) else {}

    for schema_id, schema in schemas.items():
        if not isinstance(schema, dict):
            continue
        for field_id in schema.get("field_ids", []):
            if field_id not in fields:
                error(
                    "SCHEMA_FIELD_REFERENCE_UNKNOWN",
                    f"schema {schema_id} references unknown field: {field_id}",
                    f"/entities/schemas/{pointer_escape(str(schema_id))}/field_ids",
                )

    def validate_wire_schema_refs(value: Any, pointer: str) -> None:
        if isinstance(value, dict):
            entity_ref = value.get("x-entity-ref")
            if isinstance(entity_ref, str) and entity_ref not in schemas:
                error(
                    "WIRE_SCHEMA_ENTITY_REFERENCE_UNKNOWN",
                    f"wire schema references unknown schema entity: {entity_ref}",
                    pointer_join(pointer, "x-entity-ref"),
                )
            for key, child in value.items():
                validate_wire_schema_refs(child, pointer_join(pointer, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                validate_wire_schema_refs(child, pointer_join(pointer, index))

    for field_id, field in fields.items():
        if not isinstance(field, dict):
            continue
        schema_id = field.get("schema_id")
        if not isinstance(schema_id, str) or schema_id not in schemas:
            error(
                "FIELD_SCHEMA_REFERENCE_UNKNOWN",
                f"field {field_id} references unknown schema: {schema_id}",
                f"/entities/fields/{pointer_escape(str(field_id))}/schema_id",
            )
        validate_wire_schema_refs(
            field.get("wire_schema"),
            f"/entities/fields/{pointer_escape(str(field_id))}/wire_schema",
        )

    for surface_id, surface in surfaces.items():
        if not isinstance(surface, dict):
            continue
        for key in ("request_schema_id", "response_schema_id"):
            schema_id = surface.get(key)
            if schema_id is not None and schema_id not in schemas:
                error(
                    "SURFACE_SCHEMA_REFERENCE_UNKNOWN",
                    f"surface {surface_id} references unknown schema: {schema_id}",
                    f"/entities/surfaces/{pointer_escape(str(surface_id))}/{key}",
                )

    edge_ids: set[str] = set()
    for index, edge in enumerate(report.get("edges", [])) if isinstance(report.get("edges"), list) else []:
        pointer = f"/edges/{index}"
        if not isinstance(edge, dict):
            error("EDGE_TYPE_INVALID", "edge must be an object", pointer)
            continue
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id:
            error("EDGE_ID_MISSING", "edge id is required", pointer_join(pointer, "id"))
        elif edge_id in edge_ids:
            error("EDGE_ID_DUPLICATE", f"duplicate edge id: {edge_id}", pointer_join(pointer, "id"))
        else:
            edge_ids.add(edge_id)
        for endpoint_key in ("from", "to"):
            endpoint = edge.get(endpoint_key)
            if not isinstance(endpoint, str) or not endpoint:
                error("EDGE_ENDPOINT_MISSING", f"edge {endpoint_key} is required", pointer_join(pointer, endpoint_key))
            elif endpoint not in known_ids:
                error("EDGE_ENDPOINT_UNKNOWN", f"edge {endpoint_key} references unknown entity: {endpoint}", pointer_join(pointer, endpoint_key))

    manifest = report.get("source_manifest") if isinstance(report.get("source_manifest"), dict) else {}
    source_files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    locations = manifest.get("locations") if isinstance(manifest.get("locations"), dict) else {}
    source_ref_ids = set(source_files) | set(locations)
    path_index = manifest.get("path_index") if isinstance(manifest.get("path_index"), dict) else {}
    for path, source_id in path_index.items():
        if source_id not in source_files:
            error(
                "SOURCE_PATH_INDEX_REFERENCE_UNKNOWN",
                f"source path index references unknown source: {source_id}",
                f"/source_manifest/path_index/{pointer_escape(str(path))}",
            )
    for location_id, location in locations.items():
        if not isinstance(location, dict):
            continue
        source_id = location.get("source_ref")
        if source_id not in source_files:
            error(
                "SOURCE_LOCATION_REFERENCE_UNKNOWN",
                f"source location {location_id} references unknown source: {source_id}",
                f"/source_manifest/locations/{pointer_escape(str(location_id))}/source_ref",
            )

    for category, category_entities in entities.items():
        if not isinstance(category_entities, dict):
            continue
        for entity_id, entity in category_entities.items():
            if not isinstance(entity, dict):
                continue
            for source_ref in entity.get("source_refs", []):
                if source_ref not in source_ref_ids:
                    error(
                        "ENTITY_SOURCE_REFERENCE_UNKNOWN",
                        f"entity {entity_id} references unknown source/location: {source_ref}",
                        f"/entities/{pointer_escape(str(category))}/{pointer_escape(str(entity_id))}/source_refs",
                    )

    diagnostics_value = report.get("diagnostics")
    diagnostic_ids: set[str] = set()
    if isinstance(diagnostics_value, list):
        for index, diagnostic in enumerate(diagnostics_value):
            pointer = f"/diagnostics/{index}"
            if not isinstance(diagnostic, dict):
                error("DIAGNOSTIC_TYPE_INVALID", "diagnostic must be an object", pointer)
                continue
            diagnostic_id = diagnostic.get("id")
            if not isinstance(diagnostic_id, str) or not diagnostic_id:
                error("DIAGNOSTIC_ID_MISSING", "diagnostic id is required", pointer_join(pointer, "id"))
            elif diagnostic_id in diagnostic_ids:
                error("DIAGNOSTIC_ID_DUPLICATE", f"duplicate diagnostic id: {diagnostic_id}", pointer_join(pointer, "id"))
            else:
                diagnostic_ids.add(diagnostic_id)
            if diagnostic.get("severity") not in {"error", "warning", "info"}:
                error("DIAGNOSTIC_SEVERITY_INVALID", "diagnostic severity must be error, warning, or info", pointer_join(pointer, "severity"))
            for source_ref in diagnostic.get("source_refs", []):
                if source_ref not in source_ref_ids:
                    error(
                        "DIAGNOSTIC_SOURCE_REFERENCE_UNKNOWN",
                        f"diagnostic references unknown source/location: {source_ref}",
                        pointer_join(pointer, "source_refs"),
                    )

    status = report.get("status") if isinstance(report.get("status"), dict) else {}
    for diagnostic_id in status.get("diagnostic_refs", []):
        if diagnostic_id not in diagnostic_ids:
            error(
                "STATUS_DIAGNOSTIC_REFERENCE_UNKNOWN",
                f"status references unknown diagnostic: {diagnostic_id}",
                "/status/diagnostic_refs",
            )

    expected_summary = diagnostic_summary(
        diagnostics_value if isinstance(diagnostics_value, list) else []
    )
    if report.get("diagnostic_summary") != expected_summary:
        error(
            "DIAGNOSTIC_SUMMARY_MISMATCH",
            "diagnostic_summary does not match diagnostics",
            "/diagnostic_summary",
        )

    machine = report.get("machine_contract") if isinstance(report.get("machine_contract"), dict) else {}
    expected_counts = {
        category: len(values)
        for category, values in entities.items()
        if isinstance(values, dict)
    } | {"edges": len(report.get("edges", [])) if isinstance(report.get("edges"), list) else 0}
    if machine.get("counts") != expected_counts:
        error(
            "MACHINE_CONTRACT_COUNT_MISMATCH",
            "machine_contract.counts does not match entities and edges",
            "/machine_contract/counts",
        )

    if verify_digest and isinstance(report.get("integrity"), dict) and not verify_integrity(report):
        error("REPORT_INTEGRITY_MISMATCH", "canonical report digest does not match", "/integrity/payload_sha256")
    return diagnostics


def schema_documents() -> dict[str, dict[str, Any]]:
    draft = "https://json-schema.org/draft/2020-12/schema"
    source_schema = {
        "$schema": draft,
        "$id": SOURCE_SCHEMA_ID,
        "title": "Codex wire audit source record",
        "type": "object",
        "required": ["id", "path", "available"],
        "properties": {
            "id": {"type": "string", "pattern": "^source\\."},
            "path": {"type": "string", "minLength": 1},
            "roles": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "available": {"type": "boolean"},
            "encoding": {"type": ["string", "null"]},
            "byte_length": {"type": ["integer", "null"], "minimum": 0},
            "line_count": {"type": ["integer", "null"], "minimum": 0},
            "sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
            "git_blob_sha1": {"type": ["string", "null"], "pattern": "^[0-9a-f]{40}$"},
        },
        "additionalProperties": True,
    }
    diagnostic_schema = {
        "$schema": draft,
        "$id": DIAGNOSTIC_SCHEMA_ID,
        "title": "Codex wire audit diagnostic",
        "type": "object",
        "required": ["id", "code", "severity", "category", "message", "report_pointers"],
        "properties": {
            "id": {"type": "string", "pattern": "^diagnostic\\."},
            "code": {"type": "string", "pattern": "^[A-Z0-9_]+$"},
            "severity": {"enum": ["error", "warning", "info"]},
            "category": {"type": "string"},
            "message": {"type": "string"},
            "report_pointers": {"type": "array", "items": {"type": "string", "pattern": "^/"}},
            "source_refs": {"type": "array", "items": {"type": "string"}},
            "strict_failure": {"type": "boolean"},
            "recoverable": {"type": "boolean"},
        },
        "additionalProperties": True,
    }
    wire_field_schema = {
        "$schema": draft,
        "$id": WIRE_FIELD_SCHEMA_ID,
        "title": "Normalized wire field",
        "type": "object",
        "required": ["id", "schema_id", "wire_name", "rust_type", "wire_schema", "presence"],
        "properties": {
            "id": {"type": "string", "pattern": "^field\\."},
            "schema_id": {"type": "string", "pattern": "^schema\\."},
            "name": {"type": ["string", "null"]},
            "wire_name": {"type": "string"},
            "rust_type": {"type": "string"},
            "wire_schema": {"type": "object"},
            "authority": {"type": "string"},
            "lifetime": {"type": "string"},
            "sensitivity": {"type": "string"},
            "logging_policy": {"type": "string"},
            "presence": {
                "type": "object",
                "required": ["mode", "required", "nullable"],
                "properties": {
                    "mode": {"enum": ["never", "flattened", "conditional", "present_nullable", "present"]},
                    "required": {"type": "boolean"},
                    "nullable": {"type": "boolean"},
                    "omitted_when": {
                        "oneOf": [
                            {"$ref": PREDICATE_SCHEMA_ID},
                            {"type": "null"},
                        ]
                    },
                },
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    }
    predicate_schema = {
        "$schema": draft,
        "$id": PREDICATE_SCHEMA_ID,
        "title": "Codex wire audit predicate",
        "type": "object",
        "required": ["op"],
        "properties": {
            "op": {
                "enum": [
                    "const", "all", "any", "not", "eq", "ne", "exists",
                    "env_exists", "is_null", "is_empty", "custom_predicate",
                    "source_condition", "accepted_by_extra_metadata_filter",
                    "unspecified"
                ]
            },
            "path": {"type": "string"},
            "name": {"type": "string"},
            "value": {},
            "args": {
                "type": "array",
                "items": {"$ref": PREDICATE_SCHEMA_ID},
            },
            "arg": {"$ref": PREDICATE_SCHEMA_ID},
            "expression": {"type": "string"},
            "function": {"type": "string"},
            "machine_evaluable": {"type": "boolean"},
        },
        "allOf": [
            {"if": {"properties": {"op": {"enum": ["all", "any"]}}}, "then": {"required": ["args"]}},
            {"if": {"properties": {"op": {"const": "not"}}}, "then": {"required": ["arg"]}},
            {"if": {"properties": {"op": {"enum": ["eq", "ne"]}}}, "then": {"required": ["path", "value"]}},
            {"if": {"properties": {"op": {"enum": ["exists", "is_null", "is_empty"]}}}, "then": {"required": ["path"]}},
            {"if": {"properties": {"op": {"const": "env_exists"}}}, "then": {"required": ["name"]}},
            {"if": {"properties": {"op": {"const": "source_condition"}}}, "then": {"required": ["expression"]}},
            {"if": {"properties": {"op": {"const": "custom_predicate"}}}, "then": {"required": ["function"]}},
        ],
        "additionalProperties": True,
    }
    edge_schema = {
        "$schema": draft,
        "$id": EDGE_SCHEMA_ID,
        "title": "Codex wire audit graph edge",
        "type": "object",
        "required": ["id", "from", "to", "relation"],
        "properties": {
            "id": {"type": "string", "pattern": "^edge\\."},
            "from": {"type": "string", "minLength": 1},
            "to": {"type": "string", "minLength": 1},
            "relation": {"type": "string", "minLength": 1},
            "optional": {"type": "boolean"},
            "surface": {"type": ["string", "null"]},
            "description": {"type": ["string", "null"]},
            "rule": {"type": "object"},
        },
        "additionalProperties": True,
    }
    rule_schema = {
        "$schema": draft,
        "$id": RULE_SCHEMA_ID,
        "title": "Codex wire audit rule",
        "type": "object",
        "required": ["id", "kind"],
        "properties": {
            "id": {"type": "string", "pattern": "^rule\\."},
            "kind": {"enum": ["transformation", "emission_gate", "flatten", "projection", "relationship"]},
            "when": {"$ref": PREDICATE_SCHEMA_ID},
            "from": {},
            "to": {},
            "source": {},
            "target": {},
            "description": {"type": ["string", "null"]},
            "source_refs": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    }
    report_schema = {
        "$schema": draft,
        "$id": REPORT_SCHEMA_ID,
        "title": "Codex wire audit report v10",
        "type": "object",
        "required": [
            "$schema", "$id", "format_version", "versions", "reader_compatibility",
            "source_manifest", "parser", "entities", "edges", "views", "machine_contract",
            "coverage", "diagnostics", "diagnostic_summary", "status", "schema_documents",
            "integrity", "wire_surface_catalog",
        ],
        "properties": {
            "$schema": {"const": REPORT_SCHEMA_ID},
            "$id": {"type": "string", "pattern": "^urn:codex-wire-audit:"},
            "format_version": {"const": REPORT_FORMAT_VERSION},
            "versions": {"type": "object", "required": ["report_format", "generator"]},
            "reader_compatibility": {"type": "object"},
            "parser": {"type": "object", "required": ["requested_backend", "effective_backend"]},
            "machine_contract": {"type": "object", "required": ["schema_version", "counts"]},
            "diagnostic_summary": {
                "type": "object",
                "required": ["error", "warning", "info"],
                "properties": {
                    "error": {"type": "integer", "minimum": 0},
                    "warning": {"type": "integer", "minimum": 0},
                    "info": {"type": "integer", "minimum": 0},
                },
                "additionalProperties": {"type": "integer", "minimum": 0},
            },
            "status": {"type": "object", "required": ["state", "complete", "diagnostic_refs"]},
            "schema_documents": {"type": "object"},
            "source_manifest": {
                "type": "object",
                "required": ["source_mode", "repository", "commit", "files", "path_index"],
                "properties": {
                    "source_mode": {"enum": ["github", "repo_root", "source_archive", "cache_only", "fixture"]},
                    "repository": {"type": "string"},
                    "commit": {"type": "object"},
                    "files": {
                        "type": "object",
                        "additionalProperties": {"$ref": SOURCE_SCHEMA_ID},
                    },
                    "path_index": {"type": "object", "additionalProperties": {"type": "string"}},
                    "locations": {"type": "object"},
                },
                "additionalProperties": True,
            },
            "entities": {
                "type": "object",
                "required": [
                    "surfaces", "schemas", "fields", "headers", "header_templates",
                    "settings", "events", "rules", "wire_locations"
                ],
                "properties": {
                    "fields": {"type": "object", "additionalProperties": {"$ref": WIRE_FIELD_SCHEMA_ID}},
                    "rules": {"type": "object", "additionalProperties": {"$ref": RULE_SCHEMA_ID}},
                },
                "additionalProperties": {"type": "object"},
            },
            "edges": {
                "type": "array",
                "items": {"$ref": EDGE_SCHEMA_ID},
            },
            "views": {"type": "object"},
            "coverage": {"type": "object"},
            "diagnostics": {"type": "array", "items": {"$ref": DIAGNOSTIC_SCHEMA_ID}},
            "integrity": {
                "type": "object",
                "required": ["algorithm", "canonicalization", "payload_sha256"],
                "properties": {
                    "algorithm": {"const": "sha256"},
                    "canonicalization": {"const": CANONICALIZATION_ID},
                    "payload_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                "additionalProperties": True,
            },
            "wire_surface_catalog": {"type": "object"},
        },
        "additionalProperties": True,
    }
    return {
        "codex-wire-audit-report.schema.json": report_schema,
        "codex-wire-audit-diagnostic.schema.json": diagnostic_schema,
        "codex-wire-audit-wire-field.schema.json": wire_field_schema,
        "codex-wire-audit-rule.schema.json": rule_schema,
        "codex-wire-audit-predicate.schema.json": predicate_schema,
        "codex-wire-audit-edge.schema.json": edge_schema,
        "codex-wire-audit-source.schema.json": source_schema,
    }



def _json_schema_for_entity(report: Mapping[str, Any], schema_id: str) -> dict[str, Any] | None:
    entities = report.get("entities")
    if not isinstance(entities, dict):
        return None
    schemas = entities.get("schemas")
    fields = entities.get("fields")
    if not isinstance(schemas, dict) or not isinstance(fields, dict):
        return None
    schema = schemas.get(schema_id)
    if not isinstance(schema, dict):
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field_id in schema.get("field_ids", []):
        field = fields.get(field_id)
        if not isinstance(field, dict) or not field.get("wire_name"):
            continue
        if not field.get("serialized", True) or field.get("flattened"):
            continue
        name = str(field["wire_name"])
        properties[name] = copy.deepcopy(field.get("wire_schema") or {})
        presence = field.get("presence") if isinstance(field.get("presence"), dict) else {}
        if presence.get("required"):
            required.append(name)
    result: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": schema.get("name") or schema_id,
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
        "x-codex-wire-audit-entity": schema_id,
        "x-codex-wire-audit-certainty": schema.get("certainty", "source_derived"),
    }
    if required:
        result["required"] = sorted(set(required))
    return result


def _find_schema_entity(report: Mapping[str, Any], *names: str) -> str | None:
    schemas = ((report.get("entities") or {}).get("schemas") or {}) if isinstance(report.get("entities"), dict) else {}
    normalized = {name.lower() for name in names}
    for schema_id, schema in schemas.items() if isinstance(schemas, dict) else []:
        if not isinstance(schema, dict):
            continue
        if str(schema.get("name") or "").lower() in normalized:
            return str(schema_id)
    for name in names:
        candidate = stable_id("schema", "rust", name)
        if candidate in schemas:
            return candidate
    return None


def _schema_without_null(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Remove JSON null from a field schema for config-input validation."""
    result = copy.deepcopy(dict(schema))
    if isinstance(result.get("type"), list):
        types = [value for value in result["type"] if value != "null"]
        if len(types) == 1:
            result["type"] = types[0]
        else:
            result["type"] = types
    for keyword in ("anyOf", "oneOf"):
        branches = result.get(keyword)
        if isinstance(branches, list):
            kept = [
                branch
                for branch in branches
                if not (isinstance(branch, dict) and branch.get("type") == "null")
            ]
            if len(kept) == 1:
                result.pop(keyword, None)
                merged = copy.deepcopy(kept[0])
                for key, value in result.items():
                    merged.setdefault(key, value)
                result = merged
            else:
                result[keyword] = kept
    return result


def _json_schema_for_config_input(
    report: Mapping[str, Any], schema_id: str
) -> dict[str, Any] | None:
    entities = report.get("entities")
    if not isinstance(entities, dict):
        return None
    schemas = entities.get("schemas")
    fields = entities.get("fields")
    if not isinstance(schemas, dict) or not isinstance(fields, dict):
        return None
    schema = schemas.get(schema_id)
    if not isinstance(schema, dict):
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field_id in schema.get("field_ids", []):
        field = fields.get(field_id)
        if not isinstance(field, dict) or not field.get("wire_name"):
            continue
        name = str(field["wire_name"])
        properties[name] = _schema_without_null(field.get("wire_schema") or {})
        deserialization = (
            field.get("deserialization")
            if isinstance(field.get("deserialization"), dict)
            else {}
        )
        if not deserialization.get("accepts_absent", False):
            required.append(name)
    result: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": schema.get("name") or schema_id,
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
        "x-codex-wire-audit-entity": schema_id,
        "x-codex-wire-audit-certainty": schema.get("certainty", "source_derived"),
        "x-codex-wire-audit-view": "deserialization_input",
    }
    if required:
        result["required"] = sorted(set(required))
    return result


def _collect_extension_entity_refs(value: Any) -> list[str]:
    refs: set[str] = set()

    def visit(current: Any) -> None:
        if isinstance(current, dict):
            entity_ref = current.get("x-entity-ref")
            if isinstance(entity_ref, str) and entity_ref:
                refs.add(entity_ref)
            for child in current.values():
                visit(child)
        elif isinstance(current, list):
            for child in current:
                visit(child)

    visit(value)
    return sorted(refs)


def protocol_schema_documents(report: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Generate per-source-revision JSON Schemas for the principal wire variants."""
    draft = "https://json-schema.org/draft/2020-12/schema"
    documents: dict[str, dict[str, Any]] = {}
    source_manifest = report.get("source_manifest") if isinstance(report.get("source_manifest"), dict) else {}
    source_commit = (
        source_manifest.get("commit", {}).get("sha")
        if isinstance(source_manifest.get("commit"), dict)
        else None
    )
    scope_seed = str(source_commit or report.get("$id") or "unknown")
    scope = stable_component(scope_seed, max_length=96)

    def protocol_id(name: str) -> str:
        return f"urn:codex-wire-audit:protocol:{scope}:{stable_slug(name)}:1.0.0"

    ids = {
        name: protocol_id(name.removesuffix(".schema.json"))
        for name in (
            "responses-request-full.schema.json",
            "responses-request-lite.schema.json",
            "responses-websocket-response-create-full.schema.json",
            "responses-websocket-response-create-lite.schema.json",
            "responses-websocket-prewarm.schema.json",
            "responses-event.schema.json",
            "responses-item.schema.json",
            "client-metadata.schema.json",
            "client-metadata-websocket.schema.json",
            "client-metadata-websocket-lite.schema.json",
            "codex-turn-metadata-full.schema.json",
            "codex-turn-metadata-compatibility.schema.json",
            "codex-turn-metadata-mcp.schema.json",
            "codex-turn-metadata-extra-input.schema.json",
            "mcp-request-meta.schema.json",
            "mcp-server-config.schema.json",
            "entity-schemas.schema.json",
        )
    }

    metadata_protocol = report.get("metadata_protocol") if isinstance(report.get("metadata_protocol"), dict) else {}

    # Canonical nested turn metadata and the three transport/projection variants.
    turn_schema_id = "schema.codex.turn_metadata.full"
    turn_schema = _json_schema_for_entity(report, turn_schema_id)
    if turn_schema is None:
        turn_schema = {
            "$schema": draft,
            "title": "Codex nested turn metadata",
            "type": "object",
            "properties": {},
            "additionalProperties": {"type": "string"},
            "x-codex-wire-audit-certainty": "unavailable",
        }
    turn_schema["$id"] = ids["codex-turn-metadata-full.schema.json"]
    turn_schema["description"] = (
        "Parsed JSON value stored as a string under client_metadata['x-codex-turn-metadata']. "
        "Unknown accepted properties are flattened string-valued extras."
    )
    turn_schema["additionalProperties"] = {"type": "string"}
    flattened = metadata_protocol.get("flattened_extra") if isinstance(metadata_protocol.get("flattened_extra"), dict) else {}
    validation = flattened.get("validation") if isinstance(flattened.get("validation"), dict) else {}
    key_pattern = r"^[A-Za-z][A-Za-z0-9_-]*$"
    turn_schema["propertyNames"] = {"pattern": key_pattern}
    turn_schema["x-codex-wire-audit-extra-key-pattern"] = key_pattern
    turn_schema["x-codex-wire-audit-max-extra-properties"] = validation.get("max_entries")
    turn_schema["x-codex-wire-audit-extra-key-max-bytes"] = validation.get("max_key_bytes")
    turn_schema["x-codex-wire-audit-extra-value-max-bytes"] = validation.get("max_value_bytes")
    turn_schema["x-codex-wire-audit-extra-reserved-keys"] = validation.get("reserved_keys") or []
    documents["codex-turn-metadata-full.schema.json"] = turn_schema

    compatibility = copy.deepcopy(turn_schema)
    compatibility["$id"] = ids["codex-turn-metadata-compatibility.schema.json"]
    compatibility["title"] = "Codex turn metadata compatibility-header projection"
    compatibility["description"] = (
        "Bounded direct-header projection of turn metadata; tool_namespaces_info is deliberately omitted."
    )
    compatibility.get("properties", {}).pop("tool_namespaces_info", None)
    compatibility["not"] = {"required": ["tool_namespaces_info"]}
    documents["codex-turn-metadata-compatibility.schema.json"] = compatibility

    mcp_projection = metadata_protocol.get("mcp_projection") if isinstance(metadata_protocol.get("mcp_projection"), dict) else {}
    identity_effect = mcp_projection.get("identity_effect") if isinstance(mcp_projection.get("identity_effect"), dict) else {}
    removed = set(identity_effect.get("therefore_omitted") or [])
    removed.update(mcp_projection.get("explicitly_removed") or [])
    removed.add("tool_namespaces_info")
    mcp = copy.deepcopy(turn_schema)
    mcp["$id"] = ids["codex-turn-metadata-mcp.schema.json"]
    mcp["title"] = "MCP x-codex-turn-metadata object"
    mcp["description"] = (
        "Object-valued MCP request _meta projection. It is not the string-valued Responses client_metadata representation."
    )
    for name in removed:
        mcp.get("properties", {}).pop(name, None)
    if removed:
        mcp["not"] = {"anyOf": [{"required": [name]} for name in sorted(removed)]}
    mcp.setdefault("properties", {}).update({
        "model": {"type": "string", "minLength": 1},
        "codex_version": {"type": "string", "minLength": 1},
        "reasoning_effort": {"type": "string"},
        "user_input_requested_during_turn": {"type": "boolean"},
        "node_repl_disabled": {"type": "boolean"},
    })
    mcp["required"] = sorted(
        (set(mcp.get("required", [])) - removed)
        | {"model", "codex_version", "node_repl_disabled"}
    )
    mcp["x-codex-wire-audit-identity-effect"] = identity_effect
    mcp["x-codex-wire-audit-extra-behavior"] = mcp_projection.get("extra_behavior") or {}
    documents["codex-turn-metadata-mcp.schema.json"] = mcp

    reserved_keys = sorted({str(value) for value in validation.get("reserved_keys") or []})
    extra_input: dict[str, Any] = {
        "$schema": draft,
        "$id": ids["codex-turn-metadata-extra-input.schema.json"],
        "title": "Accepted flattened turn-metadata extra entries",
        "description": (
            "Effective caller/config extra metadata after applying the key, count, value, and reserved-name contract. "
            "Values are strings and serialize as root siblings of fixed turn metadata fields."
        ),
        "type": "object",
        "propertyNames": {"pattern": key_pattern},
        "additionalProperties": {"type": "string"},
        "x-max-key-bytes": validation.get("max_key_bytes"),
        "x-max-value-bytes": validation.get("max_value_bytes"),
        "x-byte-limit-note": "JSON Schema maxLength counts Unicode code points, so byte limits remain explicit extension keywords.",
        "x-reserved-keys": reserved_keys,
    }
    if isinstance(validation.get("max_entries"), int):
        extra_input["maxProperties"] = validation["max_entries"]
    if reserved_keys:
        extra_input["not"] = {"anyOf": [{"required": [name]} for name in reserved_keys]}
    documents["codex-turn-metadata-extra-input.schema.json"] = extra_input

    # Top-level client_metadata maps. All values remain strings; the nested turn
    # object is encoded as JSON text only on Responses transports.
    base_keys: list[str] = []
    required_client_keys: list[str] = []
    top = metadata_protocol.get("top_level_client_metadata") if isinstance(metadata_protocol.get("top_level_client_metadata"), dict) else {}
    for row in top.get("base_and_conditional_keys", []):
        if isinstance(row, dict) and row.get("name"):
            key = str(row["name"])
            base_keys.append(key)
            if row.get("optional") is False:
                required_client_keys.append(key)
    client_properties = {key: {"type": "string"} for key in sorted(set(base_keys))}
    client_properties["x-codex-turn-metadata"] = {
        "type": "string",
        "contentMediaType": "application/json",
        "contentSchema": {"$ref": ids["codex-turn-metadata-full.schema.json"]},
    }
    client_metadata_schema = {
        "$schema": draft,
        "$id": ids["client-metadata.schema.json"],
        "title": "Responses client_metadata",
        "description": "Every top-level value is a string; x-codex-turn-metadata contains JSON text.",
        "type": "object",
        "properties": client_properties,
        "required": sorted(set(required_client_keys)),
        "additionalProperties": {"type": "string"},
    }
    documents["client-metadata.schema.json"] = client_metadata_schema

    ws_client = copy.deepcopy(client_metadata_schema)
    ws_client["$id"] = ids["client-metadata-websocket.schema.json"]
    ws_client["title"] = "Responses WebSocket response.create client_metadata"
    ws_client["description"] = (
        "Base Codex client metadata plus per-response.create trace, timing, continuation, and transport markers."
    )
    ws_required = set(required_client_keys)
    for row in top.get("websocket_response_create_additions", []):
        if not isinstance(row, dict) or not row.get("name"):
            continue
        key = str(row["name"])
        ws_client.setdefault("properties", {})[key] = {"type": "string"}
        if row.get("optional") is False:
            ws_required.add(key)
    ws_client["required"] = sorted(ws_required)
    documents["client-metadata-websocket.schema.json"] = ws_client

    ws_lite_client = copy.deepcopy(ws_client)
    ws_lite_client["$id"] = ids["client-metadata-websocket-lite.schema.json"]
    ws_lite_client["title"] = "Responses Lite WebSocket response.create client_metadata"
    lite_marker = "ws_request_header_x_openai_internal_codex_responses_lite"
    ws_lite_client.setdefault("properties", {})[lite_marker] = {"const": "true"}
    ws_lite_client["required"] = sorted(set(ws_lite_client.get("required", [])) | {lite_marker})
    documents["client-metadata-websocket-lite.schema.json"] = ws_lite_client

    # ResponseItem is referenced by both HTTP and WebSocket request input arrays.
    raw_response_item = None
    responses_protocol = report.get("responses_protocol")
    if isinstance(responses_protocol, dict):
        raw_response_item = (
            ((responses_protocol.get("input_and_output_items") or {}).get("enums") or {}).get("ResponseItem")
            if isinstance(responses_protocol.get("input_and_output_items"), dict)
            else None
        )
    response_item_id = _find_schema_entity(report, "ResponseItem")
    item_variants: list[dict[str, Any]] = []
    if isinstance(raw_response_item, dict):
        for variant in raw_response_item.get("variants", []):
            if not isinstance(variant, dict) or not variant.get("wire_name"):
                continue
            properties: dict[str, Any] = {"type": {"const": variant["wire_name"]}}
            required = ["type"]
            for field in variant.get("fields", []):
                if not isinstance(field, dict) or not field.get("wire_name"):
                    continue
                normalized = normalize_field(
                    field,
                    response_item_id or "schema.rust.responseitem",
                    "/protocol/response_item",
                )
                if not normalized.get("serialized", True) or normalized.get("flattened"):
                    continue
                wire_name = str(normalized["wire_name"])
                properties[wire_name] = normalized["wire_schema"]
                if normalized["presence"]["required"]:
                    required.append(wire_name)
            item_variants.append({
                "title": str(variant.get("name") or variant["wire_name"]),
                "type": "object",
                "required": sorted(set(required)),
                "properties": properties,
                "additionalProperties": True,
            })
    item_wire_names = {
        variant.get("properties", {}).get("type", {}).get("const")
        for variant in item_variants
        if isinstance(variant, dict)
    }
    if "additional_tools" not in item_wire_names:
        item_variants.append({
            "title": "AdditionalTools",
            "type": "object",
            "required": ["type", "role", "tools"],
            "properties": {
                "type": {"const": "additional_tools"},
                "id": {"type": "string"},
                "role": {"type": "string"},
                "tools": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": True,
            "x-codex-wire-audit-origin": "Responses Lite request-only control item",
        })
    documents["responses-item.schema.json"] = {
        "$schema": draft,
        "$id": ids["responses-item.schema.json"],
        "title": "Responses item tagged union",
        "oneOf": item_variants if item_variants else [
            {
                "type": "object",
                "required": ["type"],
                "properties": {"type": {"type": "string"}},
                "additionalProperties": True,
            }
        ],
        "x-codex-wire-audit-entity": response_item_id,
    }

    # HTTP full and Lite requests.
    request_schema_id = _find_schema_entity(report, "ResponsesApiRequest")
    request_schema = _json_schema_for_entity(report, request_schema_id) if request_schema_id else None
    if request_schema is None:
        request_schema = {
            "$schema": draft,
            "title": "ResponsesApiRequest",
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "x-codex-wire-audit-certainty": "unavailable",
        }
    request_schema["$id"] = ids["responses-request-full.schema.json"]
    request_schema["title"] = "Codex full Responses HTTP request"
    request_schema["description"] = "Full Responses request after Codex request construction and Serde omission rules."
    request_properties = request_schema.setdefault("properties", {})
    request_properties.update({
        "model": {"type": "string", "minLength": 1},
        "instructions": {"type": "string"},
        "input": {"type": "array", "items": {"$ref": ids["responses-item.schema.json"]}},
        "tools": {"type": "array", "items": {"type": "object"}},
        "tool_choice": {"const": "auto"},
        "parallel_tool_calls": {"type": "boolean"},
        "reasoning": {"type": "object"},
        "store": {"const": False},
        "stream": {"const": True},
        "include": {
            "type": "array",
            "items": {"type": "string"},
            "contains": {"const": "reasoning.encrypted_content"},
            "minContains": 1,
        },
        "stream_options": {"type": "object"},
        "service_tier": {"type": "string"},
        "prompt_cache_key": {"type": "string"},
        "text": {"type": "object"},
        "client_metadata": {"$ref": ids["client-metadata.schema.json"]},
        "access_programs": {"type": "object"},
    })
    request_schema["required"] = sorted(
        set(request_schema.get("required", []))
        | {
            "model", "input", "tools", "tool_choice", "parallel_tool_calls",
            "reasoning", "store", "stream", "include", "client_metadata",
        }
    )
    if request_schema.get("x-codex-wire-audit-certainty") != "unavailable":
        request_schema["additionalProperties"] = False
    documents["responses-request-full.schema.json"] = request_schema

    lite_schema = copy.deepcopy(request_schema)
    lite_schema["$id"] = ids["responses-request-lite.schema.json"]
    lite_schema["title"] = "Codex Responses Lite HTTP request"
    lite_schema["description"] = (
        "Responses Lite uses the same Rust request type after moving instructions and tools into input items. "
        "Top-level instructions/tools are omitted, parallel tool calls are false, and reasoning context is all_turns."
    )
    properties = lite_schema.setdefault("properties", {})
    properties["instructions"] = False
    properties["tools"] = False
    properties["parallel_tool_calls"] = {"const": False}
    properties["input"] = {
        "type": "array",
        "items": {"$ref": ids["responses-item.schema.json"]},
        "contains": {
            "type": "object",
            "required": ["type", "role", "tools"],
            "properties": {
                "type": {"const": "additional_tools"},
                "role": {"const": "developer"},
                "tools": {"type": "array"},
            },
            "additionalProperties": True,
        },
        "minContains": 1,
    }
    properties["reasoning"] = {
        "type": "object",
        "required": ["context"],
        "properties": {"context": {"const": "all_turns"}},
        "additionalProperties": True,
    }
    lite_schema["required"] = sorted(
        (set(lite_schema.get("required", [])) - {"instructions", "tools"})
        | {"parallel_tool_calls", "input", "reasoning"}
    )
    lite_schema["x-codex-wire-audit-transformation-rules"] = [
        rule_id
        for rule_id in sorted(((report.get("entities") or {}).get("rules") or {}))
        if str(rule_id).startswith("rule.responses_lite")
    ]
    documents["responses-request-lite.schema.json"] = lite_schema

    # Responses WebSocket response.create messages, including explicit prewarm.
    ws_request_id = _find_schema_entity(report, "ResponseCreateWsRequest")
    ws_request = _json_schema_for_entity(report, ws_request_id) if ws_request_id else None
    if ws_request is None:
        ws_request = {
            "$schema": draft,
            "title": "ResponseCreateWsRequest",
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "x-codex-wire-audit-certainty": "unavailable",
        }
    ws_request["$id"] = ids["responses-websocket-response-create-full.schema.json"]
    ws_request["title"] = "Codex full Responses WebSocket response.create message"
    ws_properties = ws_request.setdefault("properties", {})
    ws_properties.update({
        "type": {"const": "response.create"},
        "model": {"type": "string", "minLength": 1},
        "instructions": {"type": "string"},
        "input": {"type": "array", "items": {"$ref": ids["responses-item.schema.json"]}},
        "tools": {"type": "array", "items": {"type": "object"}},
        "tool_choice": {"const": "auto"},
        "parallel_tool_calls": {"type": "boolean"},
        "reasoning": {"type": "object"},
        "store": {"const": False},
        "stream": {"const": True},
        "include": {
            "type": "array",
            "items": {"type": "string"},
            "contains": {"const": "reasoning.encrypted_content"},
            "minContains": 1,
        },
        "stream_options": {"type": "object"},
        "service_tier": {"type": "string"},
        "prompt_cache_key": {"type": "string"},
        "text": {"type": "object"},
        "client_metadata": {"$ref": ids["client-metadata-websocket.schema.json"]},
        "access_programs": {"type": "object"},
        "previous_response_id": {"type": "string"},
        "generate": {"type": "boolean"},
    })
    ws_request["required"] = sorted(
        set(ws_request.get("required", []))
        | {
            "type", "model", "input", "tools", "tool_choice",
            "parallel_tool_calls", "reasoning", "store", "stream", "include",
            "client_metadata",
        }
    )
    ws_request["not"] = {
        "required": ["generate"],
        "properties": {"generate": {"const": False}},
    }
    if ws_request.get("x-codex-wire-audit-certainty") != "unavailable":
        ws_request["additionalProperties"] = False
    documents["responses-websocket-response-create-full.schema.json"] = ws_request

    ws_lite = copy.deepcopy(ws_request)
    ws_lite["$id"] = ids["responses-websocket-response-create-lite.schema.json"]
    ws_lite["title"] = "Codex Responses Lite WebSocket response.create message"
    ws_lite_properties = ws_lite.setdefault("properties", {})
    ws_lite_properties["instructions"] = False
    ws_lite_properties["tools"] = False
    ws_lite_properties["parallel_tool_calls"] = {"const": False}
    ws_lite_properties["client_metadata"] = {"$ref": ids["client-metadata-websocket-lite.schema.json"]}
    ws_lite_properties["input"] = copy.deepcopy(properties["input"])
    ws_lite_properties["reasoning"] = copy.deepcopy(properties["reasoning"])
    ws_lite["required"] = sorted(
        (set(ws_lite.get("required", [])) - {"instructions", "tools"})
        | {"parallel_tool_calls", "input", "reasoning", "client_metadata"}
    )
    documents["responses-websocket-response-create-lite.schema.json"] = ws_lite

    prewarm = copy.deepcopy(ws_request)
    prewarm["$id"] = ids["responses-websocket-prewarm.schema.json"]
    prewarm["title"] = "Codex Responses WebSocket prewarm response.create message"
    prewarm["description"] = "WebSocket v2 prewarm request; generate=false suppresses model generation while establishing reusable state."
    prewarm.pop("not", None)
    prewarm.setdefault("properties", {})["generate"] = {"const": False}
    prewarm["required"] = sorted(set(prewarm.get("required", [])) | {"generate"})
    # Prewarm construction can omit normal generation-only payload fields.
    prewarm["required"] = [
        name
        for name in prewarm["required"]
        if name in {"type", "model", "store", "stream", "generate", "client_metadata"}
    ]
    prewarm["properties"]["tools"] = {"type": ["array", "null"], "items": {"type": "object"}}
    prewarm["properties"]["reasoning"] = {"type": ["object", "null"]}
    documents["responses-websocket-prewarm.schema.json"] = prewarm

    # MCP configuration and request metadata.
    raw_mcp_schema_id = _find_schema_entity(report, "RawMcpServerConfig")
    mcp_schema_id = raw_mcp_schema_id or _find_schema_entity(report, "McpServerConfig")
    mcp_server = _json_schema_for_config_input(report, mcp_schema_id) if mcp_schema_id else None
    if mcp_server is None:
        mcp_server = {
            "$schema": draft,
            "title": "RawMcpServerConfig",
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "x-codex-wire-audit-certainty": "unavailable",
        }
    mcp_server["$id"] = ids["mcp-server-config.schema.json"]
    mcp_server["title"] = "Codex MCP server configuration input"
    mcp_server["description"] = (
        "MCP server configuration with an exclusive stdio-versus-Streamable-HTTP transport. "
        "Shared lifecycle, tool-exposure, approval, and OAuth settings are constrained by the selected branch."
    )
    mcp_properties = mcp_server.setdefault("properties", {})
    mcp_properties.pop("bearer_token", None)
    if "http_headers_helper" in mcp_properties:
        mcp_properties["http_headers_helper"] = {
            **_schema_without_null(mcp_properties["http_headers_helper"]),
            "minLength": 1,
        }
    mcp_server.setdefault("allOf", []).extend([
        {
            "oneOf": [
                {
                    "title": "stdio MCP transport",
                    "required": ["command"],
                    "not": {
                        "anyOf": [
                            {"required": [name]}
                            for name in (
                                "url", "bearer_token_env_var", "http_headers_helper",
                                "http_headers", "env_http_headers", "oauth",
                                "oauth_resource", "auth",
                            )
                        ]
                    },
                },
                {
                    "title": "Streamable HTTP MCP transport",
                    "required": ["url"],
                    "not": {
                        "anyOf": [
                            {"required": [name]}
                            for name in ("command", "args", "env", "env_vars", "cwd")
                        ]
                    },
                },
            ]
        },
        {"not": {"required": ["bearer_token"]}},
        {
            "not": {
                "required": ["http_headers_helper", "environment_id"],
                "properties": {"environment_id": {"not": {"const": "local"}}},
            }
        },
    ])
    documents["mcp-server-config.schema.json"] = mcp_server
    documents["mcp-request-meta.schema.json"] = {
        "$schema": draft,
        "$id": ids["mcp-request-meta.schema.json"],
        "title": "MCP request _meta carrying Codex turn metadata",
        "type": "object",
        "properties": {
            "x-codex-turn-metadata": {
                "$ref": ids["codex-turn-metadata-mcp.schema.json"]
            }
        },
        "required": ["x-codex-turn-metadata"],
        "additionalProperties": True,
    }

    # Responses stream events with event-specific required payloads.
    event_names = sorted(
        event.get("name")
        for event in (((report.get("entities") or {}).get("events") or {}).values())
        if isinstance(event, dict) and event.get("name")
    )
    event_requirements: dict[str, dict[str, Any]] = {
        "response.created": {
            "required": ["response"],
            "properties": {"response": {"type": "object"}},
        },
        "response.completed": {
            "required": ["response"],
            "properties": {
                "response": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "usage": {"type": ["object", "null"]},
                        "usage_metadata": {"type": ["object", "null"]},
                        "end_turn": {"type": ["boolean", "null"]},
                    },
                    "additionalProperties": True,
                }
            },
        },
        "response.failed": {
            "required": ["response"],
            "properties": {"response": {"type": "object"}},
        },
        "response.incomplete": {
            "required": ["response"],
            "properties": {"response": {"type": "object"}},
        },
        "response.output_item.added": {
            "required": ["item"],
            "properties": {"item": {"$ref": ids["responses-item.schema.json"]}},
        },
        "response.output_item.done": {
            "required": ["item"],
            "properties": {"item": {"$ref": ids["responses-item.schema.json"]}},
        },
        "response.output_text.delta": {
            "required": ["delta"],
            "properties": {"delta": {"type": "string"}},
        },
        "response.custom_tool_call_input.delta": {
            "required": ["delta"],
            "properties": {
                "delta": {"type": "string"},
                "item_id": {"type": ["string", "null"]},
                "call_id": {"type": ["string", "null"]},
            },
            "anyOf": [{"required": ["item_id"]}, {"required": ["call_id"]}],
        },
        "response.reasoning_summary_text.delta": {
            "required": ["delta", "summary_index"],
            "properties": {"delta": {"type": "string"}, "summary_index": {"type": "integer"}},
        },
        "response.reasoning_summary_text.done": {
            "required": ["item_id", "text", "summary_index"],
            "properties": {
                "item_id": {"type": "string"},
                "text": {"type": "string"},
                "summary_index": {"type": "integer"},
            },
        },
        "response.reasoning_text.delta": {
            "required": ["delta", "content_index"],
            "properties": {"delta": {"type": "string"}, "content_index": {"type": "integer"}},
        },
        "response.reasoning_summary_part.added": {
            "required": ["summary_index"],
            "properties": {"summary_index": {"type": "integer"}},
        },
        "response.metadata": {
            "properties": {
                "headers": {"type": ["object", "null"]},
                "metadata": {"type": ["object", "null"]},
            }
        },
        "codex.rate_limits": {
            "properties": {
                "rate_limits": {"type": ["object", "array", "null"]},
            }
        },
    }
    event_variants: list[dict[str, Any]] = []
    for name in event_names:
        details = copy.deepcopy(event_requirements.get(name, {}))
        event_properties = {"type": {"const": name}}
        event_properties.update(details.pop("properties", {}))
        variant: dict[str, Any] = {
            "type": "object",
            "required": ["type"] + list(details.pop("required", [])),
            "properties": event_properties,
            "additionalProperties": True,
        }
        variant.update(details)
        event_variants.append(variant)
    documents["responses-event.schema.json"] = {
        "$schema": draft,
        "$id": ids["responses-event.schema.json"],
        "title": "Responses stream event envelope",
        "oneOf": event_variants if event_variants else [
            {
                "type": "object",
                "required": ["type"],
                "properties": {"type": {"type": "string"}},
                "additionalProperties": True,
            }
        ],
        "x-codex-wire-audit-note": (
            "Known consumed fields are constrained per event. Additional server fields remain accepted for forward compatibility."
        ),
    }

    # Addressable JSON Schema fragments for all normalized source-derived types.
    all_schemas = ((report.get("entities") or {}).get("schemas") or {}) if isinstance(report.get("entities"), dict) else {}
    entity_defs: dict[str, Any] = {}
    entity_id_map: dict[str, str] = {}
    for entity_id in sorted(all_schemas) if isinstance(all_schemas, dict) else []:
        fragment = _json_schema_for_entity(report, entity_id)
        if fragment is None:
            continue
        def_name = stable_component(entity_id, max_length=120)
        fragment.pop("$schema", None)
        entity_defs[def_name] = fragment
        entity_id_map[entity_id] = f"#/$defs/{pointer_escape(def_name)}"
    documents["entity-schemas.schema.json"] = {
        "$schema": draft,
        "$id": ids["entity-schemas.schema.json"],
        "title": "Normalized Codex wire schema entity bundle",
        "$defs": entity_defs,
        "x-codex-wire-audit-entity-id-map": entity_id_map,
    }

    # Make partial named-type coverage explicit instead of silently pretending
    # extension references are standard JSON Schema references.
    for name, document in documents.items():
        refs = _collect_extension_entity_refs(document)
        document.setdefault("x-codex-wire-audit-report-id", report.get("$id"))
        document.setdefault("x-codex-wire-audit-source-commit", source_commit)
        document.setdefault("x-codex-wire-audit-entity-refs", refs)
        document.setdefault(
            "x-codex-wire-audit-validation-coverage",
            "structural_with_report_entity_refs" if refs else "structural",
        )

    documents["index.json"] = {
        "$schema": draft,
        "$id": protocol_id("protocol-index"),
        "report_id": report.get("$id"),
        "source_commit": source_commit,
        "schemas": {
            name: document.get("$id")
            for name, document in sorted(documents.items())
            if name != "index.json"
        },
        "validation_scope": {
            name: document.get("x-codex-wire-audit-validation-coverage")
            for name, document in sorted(documents.items())
            if name != "index.json"
        },
    }
    return documents

def write_protocol_schema_documents(
    directory: str | os.PathLike[str], report: Mapping[str, Any]
) -> list[Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, document in protocol_schema_documents(report).items():
        path = root / name
        path.write_text(pretty_json_text(document), encoding="utf-8")
        paths.append(path)
    return paths


def write_schema_documents(directory: str | os.PathLike[str]) -> list[Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, document in schema_documents().items():
        path = root / name
        path.write_text(pretty_json_text(document), encoding="utf-8")
        paths.append(path)
    index = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schemas": {name: document["$id"] for name, document in schema_documents().items()},
    }
    index_path = root / "index.json"
    index_path.write_text(pretty_json_text(index), encoding="utf-8")
    paths.append(index_path)
    return paths


def fixture_documents() -> dict[str, Any]:
    """Return positive and negative conformance fixtures for emitted schemas."""
    base_turn_metadata = {
        "session_id": "00000000-0000-0000-0000-000000000001",
        "thread_id": "00000000-0000-0000-0000-000000000002",
        "request_kind": "turn",
        "team": "infra",
    }
    base_client_metadata = {
        "session_id": "00000000-0000-0000-0000-000000000001",
        "thread_id": "00000000-0000-0000-0000-000000000002",
        "x-codex-installation-id": "install-example",
        "x-codex-window-id": "window-example",
        "x-codex-turn-metadata": json.dumps(
            base_turn_metadata,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    full_request = {
        "model": "gpt-example",
        "instructions": "Follow the project instructions.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Hello"}],
            }
        ],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
        "reasoning": {"effort": "medium", "summary": "auto"},
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "client_metadata": copy.deepcopy(base_client_metadata),
    }
    lite_request = copy.deepcopy(full_request)
    lite_request.pop("instructions", None)
    lite_request.pop("tools", None)
    lite_request["parallel_tool_calls"] = False
    lite_request["reasoning"]["context"] = "all_turns"
    lite_request["input"] = [
        {
            "id": "at_fixture",
            "type": "additional_tools",
            "role": "developer",
            "tools": [],
        },
        {
            "id": "msg_fixture",
            "type": "message",
            "role": "developer",
            "content": [
                {"type": "input_text", "text": "Follow the project instructions."}
            ],
        },
        *lite_request["input"],
    ]

    ws_client_metadata = copy.deepcopy(base_client_metadata)
    ws_client_metadata["x-codex-ws-stream-request-start-ms"] = "1725494400000"
    ws_full = copy.deepcopy(full_request)
    ws_full["type"] = "response.create"
    ws_full["client_metadata"] = ws_client_metadata

    ws_lite = copy.deepcopy(lite_request)
    ws_lite["type"] = "response.create"
    ws_lite_metadata = copy.deepcopy(ws_client_metadata)
    ws_lite_metadata[
        "ws_request_header_x_openai_internal_codex_responses_lite"
    ] = "true"
    ws_lite["client_metadata"] = ws_lite_metadata

    ws_prewarm = {
        "type": "response.create",
        "model": "gpt-example",
        "store": False,
        "stream": True,
        "generate": False,
        "client_metadata": copy.deepcopy(ws_client_metadata),
    }

    mcp_meta = {
        "x-codex-turn-metadata": {
            "session_id": "00000000-0000-0000-0000-000000000001",
            "thread_id": "00000000-0000-0000-0000-000000000002",
            "turn_id": "turn-example",
            "model": "gpt-example",
            "codex_version": "0.0.0-fixture",
            "reasoning_effort": "medium",
            "node_repl_disabled": False,
        }
    }
    mcp_stdio = {
        "command": "example-mcp-server",
        "args": ["--stdio"],
        "environment_id": "local",
        "enabled": True,
        "required": False,
    }
    mcp_http = {
        "url": "https://example.invalid/mcp",
        "bearer_token_env_var": "EXAMPLE_MCP_TOKEN",
        "environment_id": "local",
        "auth": "oauth",
        "enabled": True,
    }
    mcp_mixed = {
        "command": "example-mcp-server",
        "url": "https://example.invalid/mcp",
    }
    invalid_reserved = {
        "session_id": "client-forged-session",
        "custom": "accepted only after reserved-key filtering",
    }
    manifest = {
        "format_version": "1.0.0",
        "fixtures": [
            {
                "file": "responses-full-http.valid.json",
                "expected": "valid",
                "schema_file": "responses-request-full.schema.json",
            },
            {
                "file": "responses-lite-http.valid.json",
                "expected": "valid",
                "schema_file": "responses-request-lite.schema.json",
            },
            {
                "file": "responses-full-websocket.valid.json",
                "expected": "valid",
                "schema_file": "responses-websocket-response-create-full.schema.json",
            },
            {
                "file": "responses-lite-websocket.valid.json",
                "expected": "valid",
                "schema_file": "responses-websocket-response-create-lite.schema.json",
            },
            {
                "file": "responses-websocket-prewarm.valid.json",
                "expected": "valid",
                "schema_file": "responses-websocket-prewarm.schema.json",
            },
            {
                "file": "mcp-request-meta.valid.json",
                "expected": "valid",
                "schema_file": "mcp-request-meta.schema.json",
            },
            {
                "file": "mcp-server-stdio.valid.json",
                "expected": "valid",
                "schema_file": "mcp-server-config.schema.json",
            },
            {
                "file": "mcp-server-http.valid.json",
                "expected": "valid",
                "schema_file": "mcp-server-config.schema.json",
            },
            {
                "file": "turn-meta-reserved-key.invalid.json",
                "expected": "invalid",
                "schema_file": "codex-turn-metadata-extra-input.schema.json",
                "diagnostic_code": "EXTRA_METADATA_RESERVED_KEY",
            },
            {
                "file": "mcp-mixed-transport.invalid.json",
                "expected": "invalid",
                "schema_file": "mcp-server-config.schema.json",
                "diagnostic_code": "MCP_TRANSPORT_FIELDS_MIXED",
            },
            {
                "file": "mcp-mixed-transport.invalid.toml",
                "expected": "invalid",
                "schema_file": "mcp-server-config.schema.json",
                "toml_path": "mcp_servers.bad",
                "diagnostic_code": "MCP_TRANSPORT_FIELDS_MIXED",
            },
        ],
    }
    return {
        "responses-full-http.valid.json": full_request,
        "responses-lite-http.valid.json": lite_request,
        "responses-full-websocket.valid.json": ws_full,
        "responses-lite-websocket.valid.json": ws_lite,
        "responses-websocket-prewarm.valid.json": ws_prewarm,
        "mcp-request-meta.valid.json": mcp_meta,
        # Compatibility fixture name retained for consumers of early v10 drafts.
        "mcp-turn-meta.valid.json": mcp_meta,
        "mcp-server-stdio.valid.json": mcp_stdio,
        "mcp-server-http.valid.json": mcp_http,
        "turn-meta-reserved-key.invalid.json": invalid_reserved,
        "mcp-mixed-transport.invalid.json": mcp_mixed,
        "mcp-mixed-transport.invalid.toml": (
            '[mcp_servers.bad]\n'
            'command = "example-mcp-server"\n'
            'url = "https://example.invalid/mcp"\n'
        ),
        "manifest.json": manifest,
    }


def write_fixture_documents(directory: str | os.PathLike[str]) -> list[Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for name, document in fixture_documents().items():
        path = root / name
        if name.endswith(".json"):
            path.write_text(pretty_json_text(document), encoding="utf-8")
        else:
            path.write_text(str(document), encoding="utf-8")
        paths.append(path)
    return paths


def _safe_archive_member(destination: Path, member_name: str) -> Path:
    member = destination / member_name
    resolved_destination = destination.resolve()
    resolved_member = member.resolve()
    try:
        resolved_member.relative_to(resolved_destination)
    except ValueError as error:
        raise ContractError(f"archive member escapes destination: {member_name}") from error
    return member


def extract_source_archive(archive_path: str | os.PathLike[str], destination: str | os.PathLike[str]) -> Path:
    archive = Path(archive_path)
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            for member in handle.infolist():
                _safe_archive_member(root, member.filename)
                unix_mode = (member.external_attr >> 16) & 0o170000
                if unix_mode == 0o120000:
                    raise ContractError(f"archive links are not accepted: {member.filename}")
            handle.extractall(root)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as handle:
            members = handle.getmembers()
            for member in members:
                _safe_archive_member(root, member.name)
                if member.issym() or member.islnk():
                    raise ContractError(f"archive links are not accepted: {member.name}")
                if not (member.isfile() or member.isdir()):
                    raise ContractError(f"archive special files are not accepted: {member.name}")
            handle.extractall(root, members=members)
    else:
        raise ContractError(f"unsupported source archive: {archive}")
    return find_repo_root(root)


def find_repo_root(root: str | os.PathLike[str]) -> Path:
    path = Path(root).resolve()
    if (path / "codex-rs").is_dir():
        return path
    candidates = [candidate.parent for candidate in path.rglob("codex-rs") if candidate.is_dir()]
    candidates = sorted(set(candidates), key=lambda candidate: (len(candidate.parts), str(candidate)))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ContractError(f"could not locate codex-rs under {path}")
    raise ContractError(f"multiple possible repository roots under {path}: {candidates}")


def _run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def resolve_local_commit(
    root: str | os.PathLike[str],
    *,
    source_commit: str | None = None,
    allow_dirty: bool = False,
) -> tuple[dict[str, Any], bool | None]:
    repo_root = find_repo_root(root)
    git_sha = _run_git(repo_root, "rev-parse", "HEAD")
    dirty_text = _run_git(repo_root, "status", "--porcelain", "--untracked-files=no")
    dirty = bool(dirty_text) if dirty_text is not None else None
    if dirty and not allow_dirty:
        raise ContractError("repository has tracked modifications; pass --allow-dirty-source to audit it")
    if source_commit and git_sha and source_commit != git_sha:
        raise ContractError(
            f"--source-commit {source_commit} does not match repository HEAD {git_sha}"
        )
    sha = source_commit or git_sha
    if not sha:
        # Archive without .git: derive a stable pseudo-commit from source paths and bytes.
        digest = hashlib.sha256()
        for file in sorted((repo_root / "codex-rs").rglob("*.rs")):
            digest.update(str(file.relative_to(repo_root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(file.read_bytes())
            digest.update(b"\0")
        sha = "archive-" + digest.hexdigest()
    date = _run_git(repo_root, "show", "-s", "--format=%cI", "HEAD") if git_sha else None
    message = _run_git(repo_root, "show", "-s", "--format=%s", "HEAD") if git_sha else "offline source archive"
    return {
        "sha": sha,
        "date": date,
        "message": message or "offline source",
        "html_url": None,
    }, dirty


def load_source_sets_from_root(
    root: str | os.PathLike[str],
    base_files: Mapping[str, str],
    surface_files: Mapping[str, str],
    extra_files: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], list[str], list[str]]:
    repo_root = find_repo_root(root)

    def read_required(mapping: Mapping[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        missing: list[str] = []
        for key, relative in mapping.items():
            path = repo_root / relative
            try:
                result[key] = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                missing.append(relative)
            except UnicodeDecodeError as error:
                raise ContractError(f"source is not UTF-8: {relative}: {error}") from error
        if missing:
            raise ContractError("required source files missing: " + ", ".join(sorted(missing)))
        return result

    def read_optional(mapping: Mapping[str, str], label: str) -> tuple[dict[str, str], list[str]]:
        result: dict[str, str] = {}
        warnings: list[str] = []
        for key, relative in mapping.items():
            path = repo_root / relative
            try:
                result[key] = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                warnings.append(f"optional {label} source unavailable: {relative}: file not found")
            except UnicodeDecodeError:
                warnings.append(f"optional {label} source unavailable: {relative}: not UTF-8")
        return result, warnings

    base = read_required(base_files)
    surfaces, surface_warnings = read_optional(surface_files, "wire surface")
    extra, extra_warnings = read_optional(extra_files, "schema")
    return base, surfaces, extra, surface_warnings, extra_warnings


def load_source_sets_from_cache(
    repo: str,
    sha: str,
    base_files: Mapping[str, str],
    surface_files: Mapping[str, str],
    extra_files: Mapping[str, str],
    cache_reader: Any,
) -> tuple[dict[str, str], dict[str, str], dict[str, str], list[str], list[str]]:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise ContractError("--cache-only requires --ref to be a 40-character commit SHA")

    def required(mapping: Mapping[str, str]) -> dict[str, str]:
        result: dict[str, str] = {}
        missing: list[str] = []
        for key, path in mapping.items():
            value = cache_reader(repo, sha, path)
            if value is None:
                missing.append(path)
            else:
                result[key] = value
        if missing:
            raise ContractError("required cached sources missing: " + ", ".join(sorted(missing)))
        return result

    def optional(mapping: Mapping[str, str], label: str) -> tuple[dict[str, str], list[str]]:
        result: dict[str, str] = {}
        warnings: list[str] = []
        for key, path in mapping.items():
            value = cache_reader(repo, sha, path)
            if value is None:
                warnings.append(f"optional {label} source unavailable in cache: {path}")
            else:
                result[key] = value
        return result, warnings

    base = required(base_files)
    surfaces, surface_warnings = optional(surface_files, "wire surface")
    extra, extra_warnings = optional(extra_files, "schema")
    return base, surfaces, extra, surface_warnings, extra_warnings


def combine_sources(
    base_sources: Mapping[str, str],
    base_files: Mapping[str, str],
    surface_sources: Mapping[str, str],
    surface_files: Mapping[str, str],
    extra_sources: Mapping[str, str],
    extra_files: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    combined: dict[str, dict[str, Any]] = {}
    for role, values, paths in (
        ("base", base_sources, base_files),
        ("surface", surface_sources, surface_files),
        ("extra", extra_sources, extra_files),
    ):
        for key, path in paths.items():
            text = values.get(key)
            entry = combined.setdefault(
                path,
                {"text": text, "roles": [], "available": text is not None},
            )
            if text is not None:
                if entry.get("text") is not None and entry["text"] != text:
                    raise ContractError(f"same source path loaded with different content: {path}")
                entry["text"] = text
                entry["available"] = True
            entry["roles"].append(f"{role}:{key}")
    return combined


def render_jsonl(report: Mapping[str, Any]) -> str:
    rows: list[dict[str, Any]] = []
    entities = report.get("entities")
    if isinstance(entities, dict):
        for category, values in sorted(entities.items()):
            if not isinstance(values, dict):
                continue
            for entity_id, entity in sorted(values.items()):
                rows.append({"record_type": "entity", "entity_category": category, "id": entity_id, "value": entity})
    for edge in report.get("edges", []) if isinstance(report.get("edges"), list) else []:
        rows.append({"record_type": "edge", "id": edge.get("id"), "value": edge})
    for diagnostic in report.get("diagnostics", []) if isinstance(report.get("diagnostics"), list) else []:
        rows.append({"record_type": "diagnostic", "id": diagnostic.get("id"), "value": diagnostic})
    rows.append({"record_type": "report_metadata", "id": report.get("$id"), "value": {
        "$schema": report.get("$schema"),
        "$id": report.get("$id"),
        "format_version": report.get("format_version"),
        "versions": report.get("versions"),
        "source_manifest": report.get("source_manifest"),
        "coverage": report.get("coverage"),
        "status": report.get("status"),
        "integrity": report.get("integrity"),
    }})
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)


def write_output_directory(
    directory: str | os.PathLike[str],
    report: Mapping[str, Any],
    *,
    include_fixtures: bool = False,
) -> list[Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    pretty_path = root / "codex-wire-audit.report.json"
    pretty_path.write_text(pretty_json_text(report), encoding="utf-8")
    written.append(pretty_path)
    canonical_path = root / "codex-wire-audit.report.canonical.json"
    canonical_path.write_bytes(canonical_json_bytes(canonical_report_view(report)))
    written.append(canonical_path)
    jsonl_path = root / "codex-wire-audit.records.jsonl"
    jsonl_path.write_text(render_jsonl(report), encoding="utf-8")
    written.append(jsonl_path)
    diagnostics_path = root / "diagnostics.jsonl"
    diagnostics_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for item in report.get("diagnostics", [])),
        encoding="utf-8",
    )
    written.append(diagnostics_path)
    entities_root = root / "entities"
    entities_root.mkdir(exist_ok=True)
    for category, entities in sorted((report.get("entities") or {}).items()):
        if not isinstance(entities, dict):
            continue
        path = entities_root / f"{stable_slug(category)}.jsonl"
        path.write_text(
            "".join(json.dumps(entity, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for _, entity in sorted(entities.items())),
            encoding="utf-8",
        )
        written.append(path)
    written.extend(write_schema_documents(root / "schemas" / "report"))
    written.extend(write_protocol_schema_documents(root / "schemas" / "protocol", report))
    if include_fixtures:
        written.extend(write_fixture_documents(root / "fixtures"))
    manifest = {
        "files": [
            {
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
            for path in sorted(written)
        ]
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(pretty_json_text(manifest), encoding="utf-8")
    written.append(manifest_path)
    return written


def temporary_archive_root(archive_path: str | os.PathLike[str]) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temp = tempfile.TemporaryDirectory(prefix="codex-wire-audit-source-")
    root = extract_source_archive(archive_path, temp.name)
    return temp, root
