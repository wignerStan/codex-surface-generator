"""Small Rust lexical helpers used by source-derived semantic extractors.

This is intentionally a syntax-fact layer, not a Rust type checker.  It handles
comments, strings, raw strings, character literals, lifetimes, and nested
brackets so formatting changes do not alter extracted field expressions.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterator


class RustSyntaxError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RustSpan:
    start: int
    end: int

    def text(self, source: str) -> str:
        return source[self.start : self.end]


def line_number(source: str, index: int) -> int:
    return source.count("\n", 0, max(0, index)) + 1


def normalize_expression(value: str) -> str:
    """Collapse insignificant whitespace while preserving token boundaries.

    Rust method chains are commonly formatted with the dot at the beginning of
    the continuation line.  Normalizing punctuation spacing makes those
    formatting-only changes semantically invisible.
    """
    text = " ".join(value.strip().split())
    text = re.sub(r"\s*::\s*", "::", text)
    text = re.sub(r"\s*\.\s*", ".", text)
    text = re.sub(r"\s*\(\s*", "(", text)
    text = re.sub(r"\s*\)\s*", ")", text)
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return text


def _raw_string_prefix(source: str, index: int) -> tuple[int, str] | None:
    # r"...", r#"..."#, br"...", br##"..."##
    start = index
    if source.startswith("br", index):
        index += 2
    elif source.startswith("r", index):
        index += 1
    else:
        return None
    hash_start = index
    while index < len(source) and source[index] == "#":
        index += 1
    if index >= len(source) or source[index] != '"':
        return None
    hashes = source[hash_start:index]
    return index + 1, '"' + hashes


def _looks_like_char_literal(source: str, index: int) -> bool:
    if source[index] != "'" or index + 2 >= len(source):
        return False
    # Lifetimes are followed by an identifier and have no closing quote nearby.
    if re.match(r"'[A-Za-z_][A-Za-z0-9_]*", source[index:]):
        match = re.match(r"'[A-Za-z_][A-Za-z0-9_]*", source[index:])
        assert match is not None
        after = index + len(match.group(0))
        if after >= len(source) or source[after] != "'":
            return False
    cursor = index + 1
    if source[cursor] == "\\":
        cursor += 2
        if cursor < len(source) and source[cursor] == "u" and cursor + 1 < len(source) and source[cursor + 1] == "{":
            close = source.find("}", cursor + 2)
            cursor = close + 1 if close >= 0 else len(source)
    else:
        cursor += 1
    return cursor < len(source) and source[cursor] == "'"


def find_matching_delimiter(source: str, open_index: int) -> int:
    pairs = {"{": "}", "(": ")", "[": "]"}
    opener = source[open_index] if 0 <= open_index < len(source) else ""
    if opener not in pairs:
        raise RustSyntaxError(f"index {open_index} is not an opening delimiter")
    closer = pairs[opener]
    stack = [opener]
    index = open_index + 1
    line_comment = False
    block_depth = 0
    string_quote: str | None = None
    raw_terminator: str | None = None
    escaped = False
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_depth:
            if char == "/" and nxt == "*":
                block_depth += 1
                index += 2
                continue
            if char == "*" and nxt == "/":
                block_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if raw_terminator is not None:
            if source.startswith(raw_terminator, index):
                index += len(raw_terminator)
                raw_terminator = None
            else:
                index += 1
            continue
        if string_quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == string_quote:
                string_quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_depth = 1
            index += 2
            continue
        raw = _raw_string_prefix(source, index)
        if raw is not None:
            content_start, terminator = raw
            raw_terminator = terminator
            index = content_start
            continue
        if char == '"':
            string_quote = '"'
            index += 1
            continue
        if char == "'" and _looks_like_char_literal(source, index):
            string_quote = "'"
            index += 1
            continue
        if char in pairs:
            stack.append(char)
            index += 1
            continue
        if char in pairs.values():
            if not stack or pairs[stack[-1]] != char:
                raise RustSyntaxError(f"mismatched delimiter at index {index}")
            stack.pop()
            if not stack:
                return index
            index += 1
            continue
        index += 1
    raise RustSyntaxError(f"unclosed delimiter at index {open_index}; expected {closer}")


def _find_open_brace_after(source: str, start: int) -> int:
    index = start
    while index < len(source):
        if source[index] == "{":
            return index
        index += 1
    raise RustSyntaxError("opening brace not found")


def find_function_body(source: str, function_name: str) -> RustSpan:
    match = re.search(rf"\bfn\s+{re.escape(function_name)}\b", source)
    if not match:
        raise RustSyntaxError(f"function not found: {function_name}")
    open_index = _find_open_brace_after(source, match.end())
    close_index = find_matching_delimiter(source, open_index)
    return RustSpan(open_index + 1, close_index)


def find_struct_literal(source: str, type_name: str, *, start: int = 0, end: int | None = None) -> RustSpan:
    limit = len(source) if end is None else end
    match = re.search(rf"\b{re.escape(type_name)}\s*\{{", source[start:limit])
    if not match:
        raise RustSyntaxError(f"struct literal not found: {type_name}")
    absolute = start + match.start()
    open_index = source.find("{", absolute, limit)
    if open_index < 0:
        raise RustSyntaxError(f"struct literal opening brace not found: {type_name}")
    close_index = find_matching_delimiter(source, open_index)
    if close_index > limit:
        raise RustSyntaxError(f"struct literal extends outside requested span: {type_name}")
    return RustSpan(open_index + 1, close_index)


def _top_level_slices(text: str, delimiter: str) -> Iterator[tuple[int, int]]:
    start = 0
    index = 0
    stack: list[str] = []
    pairs = {"{": "}", "(": ")", "[": "]", "<": ">"}
    line_comment = False
    block_depth = 0
    quote: str | None = None
    raw_terminator: str | None = None
    escaped = False
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_depth:
            if char == "/" and nxt == "*":
                block_depth += 1
                index += 2
                continue
            if char == "*" and nxt == "/":
                block_depth -= 1
                index += 2
                continue
            index += 1
            continue
        if raw_terminator is not None:
            if text.startswith(raw_terminator, index):
                index += len(raw_terminator)
                raw_terminator = None
            else:
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_depth = 1
            index += 2
            continue
        raw = _raw_string_prefix(text, index)
        if raw is not None:
            content_start, terminator = raw
            raw_terminator = terminator
            index = content_start
            continue
        if char == '"':
            quote = '"'
            index += 1
            continue
        if char == "'" and _looks_like_char_literal(text, index):
            quote = "'"
            index += 1
            continue
        if char in pairs:
            # '<' can also be a comparison operator.  In the source fragments
            # audited here it appears in types/turbofish; require a plausible
            # identifier/type token before treating it as a delimiter.
            if char != "<" or (index and (text[index - 1].isalnum() or text[index - 1] in "_:>")):
                stack.append(char)
                index += 1
                continue
        if char in pairs.values() and stack and pairs[stack[-1]] == char:
            stack.pop()
            index += 1
            continue
        if char == delimiter and not stack:
            yield start, index
            start = index + 1
        index += 1
    yield start, len(text)


def split_top_level(text: str, delimiter: str = ",") -> list[str]:
    return [text[start:end].strip() for start, end in _top_level_slices(text, delimiter) if text[start:end].strip()]


def _top_level_colon(entry: str) -> int | None:
    for start, end in _top_level_slices(entry, ":"):
        # _top_level_slices returns the final range as well. The first range ends
        # immediately before a top-level colon when one exists.
        if end < len(entry) and entry[end] == ":":
            return end
        break
    return None


def split_struct_entries(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for entry in split_top_level(text, ","):
        # Remove leading line comments before locating the field identifier.
        cleaned = re.sub(r"(?m)^\s*//.*$", "", entry).strip()
        if not cleaned or cleaned.startswith(".."):
            continue
        colon = _top_level_colon(cleaned)
        if colon is None:
            match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", cleaned)
            if match:
                rows.append((match.group(1), match.group(1)))
            continue
        left = cleaned[:colon]
        right = cleaned[colon + 1 :]
        match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", left)
        if not match:
            continue
        rows.append((match.group(1), normalize_expression(right)))
    return rows


def extract_top_level_let_bindings(text: str) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for statement in split_top_level(text, ";"):
        cleaned = re.sub(r"(?m)^\s*//.*$", "", statement).strip()
        match = re.match(
            r"let\s+(?:mut\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=\s*(.+)$",
            cleaned,
            re.S,
        )
        if match:
            bindings[match.group(1)] = normalize_expression(match.group(2))
    return bindings
