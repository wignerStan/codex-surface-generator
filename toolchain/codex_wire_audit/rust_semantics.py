from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Iterable

from .proof_diagnostics import ProofDiagnostic


@dataclass(frozen=True, slots=True)
class StructEntry:
    ordinal: int
    kind: str
    source: str
    field_name: str | None = None


@dataclass(frozen=True, slots=True)
class HelperDependency:
    reference: str
    name: str
    found: bool
    normalized_body_sha256: str | None


@dataclass(frozen=True, slots=True)
class RustSemanticAnalysis:
    payload_found: bool
    entries: tuple[StructEntry, ...]
    helpers: tuple[HelperDependency, ...]
    semantic_sha256: str
    complete: bool
    diagnostics: tuple[ProofDiagnostic, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "payload_found": self.payload_found,
            "entries": [asdict(item) for item in self.entries],
            "helpers": [asdict(item) for item in self.helpers],
            "semantic_sha256": self.semantic_sha256,
            "complete": self.complete,
            "diagnostics": [item.to_dict() for item in self.diagnostics],
        }


def _find_matching(text: str, start: int, opening: str = "{", closing: str = "}") -> int | None:
    depth = 0
    state = "code"
    index = start
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == '"':
                state = "string"
            elif char == "'":
                # Rust lifetimes are not strings. Treat apostrophe followed by identifier as code.
                if not (nxt.isalpha() or nxt == "_"):
                    state = "char"
            elif char == "/" and nxt == "/":
                state = "line_comment"; index += 1
            elif char == "/" and nxt == "*":
                state = "block_comment"; index += 1
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return index
        elif state == "string":
            if char == "\\":
                index += 1
            elif char == '"':
                state = "code"
        elif state == "char":
            if char == "\\":
                index += 1
            elif char == "'":
                state = "code"
        elif state == "line_comment":
            if char == "\n":
                state = "code"
        elif state == "block_comment":
            if char == "*" and nxt == "/":
                state = "code"; index += 1
        index += 1
    return None


def _split_top_level(text: str) -> list[str]:
    entries: list[str] = []
    start = 0
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    state = "code"
    index = 0
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if state == "code":
            if char == '"': state = "string"
            elif char == "'" and not (nxt.isalpha() or nxt == "_"): state = "char"
            elif char == "/" and nxt == "/": state = "line_comment"; index += 1
            elif char == "/" and nxt == "*": state = "block_comment"; index += 1
            elif char in depths: depths[char] += 1
            elif char in pairs and depths[pairs[char]] > 0: depths[pairs[char]] -= 1
            elif char == "," and not any(depths.values()):
                candidate = text[start:index].strip()
                if candidate: entries.append(candidate)
                start = index + 1
        elif state == "string":
            if char == "\\": index += 1
            elif char == '"': state = "code"
        elif state == "char":
            if char == "\\": index += 1
            elif char == "'": state = "code"
        elif state == "line_comment":
            if char == "\n": state = "code"
        elif state == "block_comment":
            if char == "*" and nxt == "/": state = "code"; index += 1
        index += 1
    candidate = text[start:].strip()
    if candidate: entries.append(candidate)
    return entries


def split_struct_entries_lossless(text: str) -> list[str]:
    """Return every non-empty top-level entry; never discard syntax by category."""
    return _split_top_level(text)


def _top_level_colon(entry: str) -> int | None:
    pieces = _split_top_level(entry.replace(":", ":,"))
    # The replacement trick is unsuitable inside paths; use a small direct scanner instead.
    depths = {"(": 0, "[": 0, "{": 0, "<": 0}
    pairs = {")": "(", "]": "[", "}": "{", ">": "<"}
    state = "code"
    index = 0
    while index < len(entry):
        char = entry[index]
        nxt = entry[index + 1] if index + 1 < len(entry) else ""
        if state == "code":
            if char == '"': state = "string"
            elif char == "'" and not (nxt.isalpha() or nxt == "_"): state = "char"
            elif char == "/" and nxt == "/": state = "line_comment"; index += 1
            elif char == "/" and nxt == "*": state = "block_comment"; index += 1
            elif char in depths: depths[char] += 1
            elif char in pairs and depths[pairs[char]] > 0: depths[pairs[char]] -= 1
            elif char == ":" and not any(depths.values()):
                if nxt == ":" or (index > 0 and entry[index - 1] == ":"):
                    pass
                else:
                    return index
        elif state == "string":
            if char == "\\": index += 1
            elif char == '"': state = "code"
        elif state == "char":
            if char == "\\": index += 1
            elif char == "'": state = "code"
        elif state == "line_comment":
            if char == "\n": state = "code"
        elif state == "block_comment":
            if char == "*" and nxt == "/": state = "code"; index += 1
        index += 1
    return None


def classify_struct_entry(source: str, ordinal: int) -> StructEntry:
    stripped = source.strip()
    if stripped.startswith(".."):
        return StructEntry(ordinal, "struct_spread", stripped)
    if stripped.startswith("#["):
        return StructEntry(ordinal, "attribute", stripped)
    colon = _top_level_colon(stripped)
    if colon is not None:
        name = stripped[:colon].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            return StructEntry(ordinal, "field", stripped, name)
        return StructEntry(ordinal, "unclassified", stripped)
    if re.match(r"(?:[A-Za-z_][A-Za-z0-9_]*::)*[A-Za-z_][A-Za-z0-9_]*!\s*[\(\{\[]", stripped):
        return StructEntry(ordinal, "macro_invocation", stripped)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", stripped):
        return StructEntry(ordinal, "shorthand_field", stripped, stripped)
    return StructEntry(ordinal, "unclassified", stripped)


def _payload_body(source: str, type_name: str) -> str | None:
    pattern = re.compile(r"\b" + re.escape(type_name) + r"\s*\{")
    matches = list(pattern.finditer(source))
    if not matches:
        return None
    # Prefer a constructor in a function body rather than the struct declaration.
    for match in reversed(matches):
        brace = source.find("{", match.start())
        end = _find_matching(source, brace)
        if end is not None:
            body = source[brace + 1:end]
            if ":" in body or ".." in body or "!" in body:
                return body
    return None


def _normalize_rust(text: str) -> str:
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"\s+", "", text)


def _extract_function_body(source: str, name: str) -> str | None:
    pattern = re.compile(r"\bfn\s+" + re.escape(name) + r"\s*(?:<[^>{}]*>)?\s*\(")
    for match in pattern.finditer(source):
        brace = source.find("{", match.end())
        semicolon = source.find(";", match.end())
        if brace < 0 or (semicolon >= 0 and semicolon < brace):
            continue
        end = _find_matching(source, brace)
        if end is not None:
            return source[brace + 1:end]
    return None


def _helper_references(payload: str, source: str) -> tuple[str, ...]:
    references = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+", payload))
    # Follow local gate bindings used by the payload.
    for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:", payload):
        binding = re.search(r"\blet\s+" + re.escape(name) + r"\s*=\s*(.*?);", source, flags=re.S)
        if binding:
            references.update(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)+", binding.group(1)))
    ignored = {"Option::is_none", "Option::is_some", "Option::is_none_or", "Option::is_some_and"}
    return tuple(sorted(ref for ref in references if ref not in ignored))


def analyze_turn_metadata_source(source: str, type_name: str = "CodexTurnMetadataPayload") -> RustSemanticAnalysis:
    diagnostics: list[ProofDiagnostic] = []
    body = _payload_body(source, type_name)
    if body is None:
        diagnostics.append(ProofDiagnostic(
            code="TURN_METADATA_PAYLOAD_NOT_FOUND",
            message=f"could not locate a {type_name} constructor",
            source_id=type_name,
        ))
        payload_entries: tuple[StructEntry, ...] = ()
        helpers: tuple[HelperDependency, ...] = ()
    else:
        raw_entries = split_struct_entries_lossless(body)
        payload_entries = tuple(classify_struct_entry(item, ordinal) for ordinal, item in enumerate(raw_entries))
        if body.strip() and not payload_entries:
            diagnostics.append(ProofDiagnostic(
                code="NONEMPTY_PAYLOAD_WITH_ZERO_ENTRIES",
                message="non-empty payload constructor yielded zero entries",
                source_id=type_name,
            ))
        for entry in payload_entries:
            if entry.kind == "struct_spread":
                diagnostics.append(ProofDiagnostic(
                    code="STRUCT_SPREAD_UNRESOLVED",
                    message="struct update syntax must be resolved before semantic completeness",
                    source_id=type_name,
                    location=f"entry:{entry.ordinal}",
                    details={"source": entry.source},
                ))
            elif entry.kind == "macro_invocation":
                diagnostics.append(ProofDiagnostic(
                    code="STRUCT_MACRO_UNRESOLVED",
                    message="macro-generated payload entries must be expanded or diagnosed",
                    source_id=type_name,
                    location=f"entry:{entry.ordinal}",
                    details={"source": entry.source},
                ))
            elif entry.kind in {"attribute", "unclassified"}:
                diagnostics.append(ProofDiagnostic(
                    code="STRUCT_ENTRY_UNCLASSIFIED",
                    message=f"payload entry has unsupported kind {entry.kind}",
                    source_id=type_name,
                    location=f"entry:{entry.ordinal}",
                    details={"source": entry.source},
                ))

        helper_items: list[HelperDependency] = []
        for reference in _helper_references(body, source):
            name = reference.rsplit("::", 1)[-1]
            helper_body = _extract_function_body(source, name)
            digest = hashlib.sha256(_normalize_rust(helper_body).encode("utf-8")).hexdigest() if helper_body is not None else None
            helper_items.append(HelperDependency(reference, name, helper_body is not None, digest))
            if helper_body is None:
                diagnostics.append(ProofDiagnostic(
                    code="HELPER_DEFINITION_UNRESOLVED",
                    message=f"semantic helper definition was not found: {reference}",
                    source_id=type_name,
                    field_id=reference,
                ))
        helpers = tuple(helper_items)

    semantic_payload = {
        "entries": [asdict(item) for item in payload_entries],
        "helpers": [asdict(item) for item in helpers],
    }
    semantic_sha256 = hashlib.sha256(
        json.dumps(semantic_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RustSemanticAnalysis(
        payload_found=body is not None,
        entries=payload_entries,
        helpers=helpers,
        semantic_sha256=semantic_sha256,
        complete=not diagnostics,
        diagnostics=tuple(diagnostics),
    )
