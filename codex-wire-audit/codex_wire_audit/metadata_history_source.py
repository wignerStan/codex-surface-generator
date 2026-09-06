"""Exact source loading and structured observations for metadata-key history.

This module owns the Git/worktree byte boundary and the small Rust/JSON probes
used by :mod:`codex_wire_audit.metadata_history_probe`.  Keeping these mechanics
separate makes the policy layer easier to review and lets future parser backends
replace observation helpers without changing proof orchestration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping

from .metadata_history import MetadataHistoryError
from .rust_syntax import (
    RustSyntaxError,
    find_function_body,
    find_matching_delimiter,
    find_struct_literal,
    split_struct_entries,
)


@dataclass(frozen=True, slots=True)
class LoadedHistorySource:
    """One exact source file used by a metadata-history probe."""

    path: str
    basis: str
    byte_length: int
    sha256: str
    git_blob_sha: str | None
    text: str

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("text", None)
        return value


def run_git(repo: Path, *args: str, check: bool = True) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and process.returncode:
        message = process.stderr.decode("utf-8", "replace").strip()
        raise MetadataHistoryError(message or f"git {' '.join(args)} failed")
    return process.stdout


def resolve_commit(root: Path, requested_ref: str) -> str | None:
    try:
        return run_git(root, "rev-parse", f"{requested_ref}^{{commit}}").decode().strip()
    except MetadataHistoryError:
        return None


def _decode_utf8(path: str, data: bytes) -> str:
    try:
        return data.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise MetadataHistoryError(f"history source is not UTF-8: {path}: {error}") from error


def _read_git_source(root: Path, commit_sha: str, path: str) -> LoadedHistorySource | None:
    process = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit_sha}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode:
        return None
    data = process.stdout
    blob = run_git(root, "rev-parse", f"{commit_sha}:{path}", check=False).decode().strip() or None
    return LoadedHistorySource(
        path=path,
        basis="git_object_database",
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        git_blob_sha=blob,
        text=_decode_utf8(path, data),
    )


def _read_worktree_source(
    root: Path,
    head_sha: str | None,
    path: str,
) -> LoadedHistorySource | None:
    candidate = root / path
    if not candidate.is_file():
        return None
    data = candidate.read_bytes()
    blob = None
    if head_sha is not None:
        blob = run_git(root, "rev-parse", f"{head_sha}:{path}", check=False).decode().strip() or None
    return LoadedHistorySource(
        path=path,
        basis="worktree",
        byte_length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        git_blob_sha=blob,
        text=_decode_utf8(path, data),
    )


def load_history_sources(
    root: Path,
    paths: Iterable[str],
    *,
    source_mode: str,
    commit_sha: str | None,
) -> tuple[dict[str, LoadedHistorySource], tuple[str, ...]]:
    loaded: dict[str, LoadedHistorySource] = {}
    missing: list[str] = []
    for path in sorted(set(paths)):
        item = (
            _read_git_source(root, commit_sha, path)
            if source_mode == "commit" and commit_sha is not None
            else _read_worktree_source(root, commit_sha, path)
        )
        if item is None:
            missing.append(path)
        else:
            loaded[path] = item
    return loaded, tuple(missing)


def _source_text(sources: Mapping[str, LoadedHistorySource], suffix: str) -> str | None:
    matches = [item.text for path, item in sources.items() if path.endswith(suffix)]
    if not matches:
        return None
    # Candidate registries may deliberately list relocated paths. Multiple
    # matches are concatenated deterministically so duplicate definitions remain
    # visible to the assertion layer instead of being hidden by path ordering.
    return "\n".join(matches)


def _constant_map(source: str | None) -> dict[str, str]:
    if source is None:
        return {}
    return {
        match.group(1): match.group(2)
        for match in re.finditer(
            r'\b(?:pub(?:\([^)]*\))?\s+)?const\s+([A-Z][A-Z0-9_]*)\s*:\s*&(?:\'static\s+)?str\s*=\s*"([^"]+)"',
            source,
        )
    }


def _named_array_tokens(source: str | None, name: str) -> tuple[str, ...] | None:
    if source is None:
        return None
    match = re.search(
        rf"\b(?:pub(?:\([^)]*\))?\s+)?const\s+{re.escape(name)}\b[^=]*=\s*&\s*\[",
        source,
    )
    if not match:
        return ()
    open_index = source.rfind("[", match.start(), match.end())
    try:
        close_index = find_matching_delimiter(source, open_index)
    except RustSyntaxError:
        return None
    body = source[open_index + 1 : close_index]
    tokens: list[str] = []
    for literal, identifier in re.findall(
        r'"([^"\\]*(?:\\.[^"\\]*)*)"|\b([A-Z][A-Z0-9_]*)\b',
        body,
    ):
        tokens.append(json.loads(f'"{literal}"') if literal else identifier)
    return tuple(tokens)


def _resolved_array_values(
    source: str | None,
    name: str,
    constants: Mapping[str, str],
) -> tuple[set[str], set[str]] | None:
    tokens = _named_array_tokens(source, name)
    if tokens is None:
        return None
    values: set[str] = set()
    identifiers: set[str] = set()
    for token in tokens:
        if token in constants:
            values.add(constants[token])
            identifiers.add(token)
        elif re.fullmatch(r"[A-Z][A-Z0-9_]*", token):
            identifiers.add(token)
        else:
            values.add(token)
    return values, identifiers


def _struct_body(source: str | None, name: str) -> str | None:
    if source is None:
        return None
    match = re.search(rf"\bstruct\s+{re.escape(name)}(?:\s*<[^{{;]+>)?\s*\{{", source)
    if not match:
        return None
    open_index = source.find("{", match.start(), match.end())
    try:
        close_index = find_matching_delimiter(source, open_index)
    except RustSyntaxError:
        return None
    return source[open_index + 1 : close_index]


def _struct_has_field(source: str | None, struct_name: str, field_name: str) -> bool | None:
    body = _struct_body(source, struct_name)
    if body is None:
        return None
    return bool(
        re.search(
            rf"\b(?:pub(?:\([^)]*\))?\s+)?{re.escape(field_name)}\s*:",
            body,
        )
    )


def _payload_entries(source: str | None) -> dict[str, str] | None:
    if source is None:
        return None
    try:
        function = find_function_body(source, "turn_metadata_payload")
        literal = find_struct_literal(
            source,
            "CodexTurnMetadataPayload",
            start=function.start,
            end=function.end,
        )
    except RustSyntaxError:
        return None
    return dict(split_struct_entries(literal.text(source)))


def _function_text(source: str | None, name: str) -> str | None:
    if source is None:
        return None
    try:
        return find_function_body(source, name).text(source)
    except RustSyntaxError:
        return None


def _explicit_none_projection(function: str | None, field_name: str) -> bool | None:
    if function is None:
        return None
    try:
        literal = find_struct_literal(function, "CodexTurnMetadataPayload")
    except RustSyntaxError:
        return False
    entries = dict(split_struct_entries(literal.text(function)))
    value = entries.get(field_name)
    return value is not None and re.fullmatch(r"None", value.strip()) is not None


def _direct_client_metadata_state(
    source: str | None,
    wire_name: str,
    constant_names: Iterable[str],
) -> str | None:
    body = _function_text(source, "client_metadata")
    if body is None:
        return None
    names = list(constant_names)
    patterns = [
        rf"client_metadata\s*\.\s*insert\s*\(\s*{re.escape(name)}\b"
        for name in names
    ]
    patterns.append(rf'client_metadata\s*\.\s*insert\s*\(\s*"{re.escape(wire_name)}"')
    # Base-map entries are also direct flat client metadata.
    patterns.extend(rf"\(\s*{re.escape(name)}\.to_string\s*\(\)" for name in names)
    return "emitted_conditionally" if any(re.search(pattern, body) for pattern in patterns) else "absent"


def _compatibility_state(
    source: str | None,
    field_name: str,
    payload_present: bool | None,
) -> str | None:
    if payload_present is False:
        return "absent"
    body = _function_text(source, "compatibility_headers")
    if body is None or payload_present is None:
        return None
    projected_out = _explicit_none_projection(body, field_name)
    if projected_out is None:
        return None
    return "omitted" if projected_out else "emitted_conditionally"


def _mcp_state(
    source: str | None,
    field_name: str,
    wire_name: str,
    constant_names: Iterable[str],
    payload_present: bool | None,
    payload_expression: str | None = None,
) -> str | None:
    if payload_present is False:
        return "absent"
    body = _function_text(source, "current_meta_value_for_mcp_request")
    if body is None or payload_present is None:
        return None
    if payload_expression and "has_request_identity" in payload_expression:
        return "absent"
    if re.search(rf"responses_metadata\s*\.\s*{re.escape(field_name)}\s*=\s*None", body):
        return "omitted"
    for name in constant_names:
        if re.search(rf"metadata\s*\.\s*remove\s*\(\s*{re.escape(name)}\s*\)", body):
            return "omitted"
    if re.search(rf'metadata\s*\.\s*remove\s*\(\s*"{re.escape(wire_name)}"\s*\)', body):
        return "omitted"
    return "emitted_conditionally"


def _has_flattened_extra(source: str | None) -> bool | None:
    body = _struct_body(source, "CodexTurnMetadataPayload")
    if body is None:
        return None
    return bool(
        re.search(
            r"#\s*\[\s*serde\s*\(\s*flatten\s*\)\s*\][\s\S]{0,180}\bextra\s*:",
            body,
        )
    )


def _payload_state(
    responses_source: str | None,
    field_name: str,
    wire_name: str,
    reserved: bool | None,
) -> str | None:
    present = _struct_has_field(responses_source, "CodexTurnMetadataPayload", field_name)
    if present is None:
        return None
    if present:
        entries = _payload_entries(responses_source)
        if entries is None:
            return None
        return "emitted_conditionally" if field_name in entries else "declared_not_assigned"
    flattened = _has_flattened_extra(responses_source)
    if flattened is None:
        return None
    if flattened and reserved is False and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", wire_name):
        return "flattened_extra_string"
    return "absent"


def _json_contains_property(source: str | None, property_name: str) -> bool | None:
    if source is None:
        return None
    try:
        value = json.loads(source)
    except json.JSONDecodeError:
        return bool(re.search(rf'"{re.escape(property_name)}"\s*:', source))

    def visit(item: Any) -> bool:
        if isinstance(item, dict):
            return property_name in item or any(visit(child) for child in item.values())
        if isinstance(item, list):
            return any(visit(child) for child in item)
        return False

    return visit(value)


def observe_config_key(
    wire_name: str,
    sources: Mapping[str, LoadedHistorySource],
) -> dict[str, Any]:
    rust_sources = [item.text for path, item in sources.items() if path.endswith(".rs")]
    schema_sources = [
        item.text for path, item in sources.items() if path.endswith("config.schema.json")
    ]
    if not rust_sources:
        definition: bool | None = None
        runtime: bool | None = None
    else:
        declaration = re.compile(
            rf"(?m)^\s*(?:#\[[^\n]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?{re.escape(wire_name)}\s*:"
        )
        definition_count = sum(len(declaration.findall(source)) for source in rust_sources)
        total_count = sum(
            len(re.findall(rf"\b{re.escape(wire_name)}\b", source))
            for source in rust_sources
        )
        definition = definition_count > 0
        runtime = total_count > definition_count
    schema = (
        None
        if not schema_sources
        else any(_json_contains_property(source, wire_name) is True for source in schema_sources)
    )
    return {
        "definition_present": definition,
        "runtime_use_present": runtime,
        "schema_property_present": schema,
    }


def observe_turn_metadata_key(
    wire_name: str,
    sources: Mapping[str, LoadedHistorySource],
) -> dict[str, Any]:
    responses = _source_text(sources, "responses_metadata.rs")
    turn = _source_text(sources, "turn_metadata.rs")
    constants = _constant_map(responses)
    constant_names = sorted(name for name, value in constants.items() if value == wire_name)
    reserved = _resolved_array_values(responses, "RESERVED_METADATA_KEYS", constants)
    backward = _resolved_array_values(
        responses,
        "BACKWARD_COMPATIBLE_RESERVED_METADATA_KEYS",
        constants,
    )
    reserved_values = reserved[0] if reserved is not None else None
    backward_values = backward[0] if backward is not None else None
    field_name = wire_name.replace("-", "_")
    payload_present = _struct_has_field(responses, "CodexTurnMetadataPayload", field_name)
    entries = _payload_entries(responses)
    payload_expression = entries.get(field_name) if isinstance(entries, dict) else None

    all_text = "\n".join(item.text for item in sources.values())
    non_metadata_text = "\n".join(
        item.text
        for path, item in sources.items()
        if not path.endswith("responses_metadata.rs")
    )
    replacement_field = _struct_has_field(responses, "TurnToolFunctionInfo", "code_mode_name")
    mapping_source = _source_text(sources, "tool_namespaces_info.rs")
    if mapping_source is None:
        successor_mapping: bool | None = False if responses is not None else None
    else:
        successor_mapping = bool(
            re.search(r"\bcode_mode_tool_names\b", mapping_source)
            and re.search(r"\bcode_mode_name\b", mapping_source)
            and (
                re.search(r"code_mode_names_by_tool", mapping_source)
                or re.search(
                    r"code_mode_tool_names[\s\S]{0,3000}code_mode_name",
                    mapping_source,
                )
            )
        )

    return {
        "wire_literal_present": (
            None
            if responses is None
            else wire_name in constants.values() or f'"{wire_name}"' in responses
        ),
        "constant_names": constant_names,
        "reserved_extra_key": (
            None if reserved_values is None else wire_name in reserved_values
        ),
        "backward_compatible_reserved": (
            None if backward_values is None else wire_name in backward_values
        ),
        "payload_container": _payload_state(
            responses,
            field_name,
            wire_name,
            None if reserved_values is None else wire_name in reserved_values,
        ),
        "top_level_client_metadata": _direct_client_metadata_state(
            responses,
            wire_name,
            constant_names,
        ),
        "compatibility_header_container": _compatibility_state(
            responses,
            field_name,
            payload_present,
        ),
        "mcp_container": _mcp_state(
            turn,
            field_name,
            wire_name,
            constant_names,
            payload_present,
            payload_expression,
        ),
        "internal_identifier_present": (
            None
            if not all_text
            else bool(re.search(rf"\b{re.escape(field_name)}\b", non_metadata_text))
        ),
        "replacement_code_mode_name_field_present": replacement_field,
        "successor_mapping_present": successor_mapping,
    }
