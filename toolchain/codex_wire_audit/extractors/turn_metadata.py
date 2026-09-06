"""Source-derived identity gates and field emission semantics."""

from __future__ import annotations

import re
from typing import Any, Mapping

from ..diagnostics import DiagnosticCollector
from ..models import (
    FieldEmissionFact,
    GateFact,
    SourceFile,
    SourceSnapshot,
    semantic_fingerprint,
)
from ..rust_syntax import (
    RustSpan,
    RustSyntaxError,
    extract_top_level_let_bindings,
    find_function_body,
    find_matching_delimiter,
    find_struct_literal,
    line_number,
    normalize_expression,
    split_struct_entries,
)
from .registry import ExtractorResult, register_extractor


EXTRACTOR_ID = "extractor.turn_metadata"
SOURCE_SPEC_ID = "source_spec.base.metadata"

_REQUEST_IDENTITY = {
    "installation_id",
    "window_id",
    "window_number",
    "context_window_id",
    "request_kind",
}
_THREAD_IDENTITY = {"session_id", "thread_id", "agent_name"}
_TURN_IDENTITY = {"turn_id"}
_LINEAGE = {
    "forked_from_thread_id",
    "forked_from_ordinal_exclusive",
    "parent_thread_id",
    "parent_turn_id",
    "root_turn_id",
    "subagent_kind",
    "thread_source",
    "turn_trigger",
}
_EXECUTION = {
    "sandbox",
    "sandbox_mode",
    "auto_review_enabled",
    "node_repl_auto_review_required",
    "node_repl_disabled",
    "workspaces",
}


def _identity_domain(field_name: str) -> str:
    if field_name in _REQUEST_IDENTITY:
        return "request_identity"
    if field_name in _THREAD_IDENTITY:
        return "thread_identity"
    if field_name in _TURN_IDENTITY:
        return "turn_identity"
    if field_name in _LINEAGE:
        return "lineage"
    if field_name in _EXECUTION:
        return "execution_context"
    if field_name == "tool_namespaces_info":
        return "tool_inventory"
    if field_name in {"turn_started_at_unix_ms", "history_ingest_requested"}:
        return "timing_and_history"
    if field_name == "compaction":
        return "compaction"
    if field_name == "extra":
        return "flattened_extra"
    return "other"


def _gate_predicate(expression: str) -> tuple[dict[str, Any], bool]:
    normalized = normalize_expression(expression)
    match = re.fullmatch(
        r"(?P<subject>[A-Za-z_][A-Za-z0-9_]*)\.is_none_or\((?P<call>.+)\)",
        normalized,
    )
    if match:
        return (
            {
                "op": "any",
                "args": [
                    {"op": "not", "arg": {"op": "exists", "path": match.group("subject")}},
                    {
                        "op": "call_predicate",
                        "function": match.group("call"),
                        "argument": match.group("subject"),
                    },
                ],
            },
            True,
        )
    match = re.fullmatch(
        r"(?P<subject>[A-Za-z_][A-Za-z0-9_]*)\.is_some_and\((?P<call>.+)\)",
        normalized,
    )
    if match:
        return (
            {
                "op": "all",
                "args": [
                    {"op": "exists", "path": match.group("subject")},
                    {
                        "op": "call_predicate",
                        "function": match.group("call"),
                        "argument": match.group("subject"),
                    },
                ],
            },
            True,
        )
    if normalized in {"true", "false"}:
        return {"op": "const", "value": normalized == "true"}, True
    return {
        "op": "rust_expression",
        "expression": normalized,
        "machine_evaluable": False,
    }, False


def _resolved_predicate(
    predicate: Mapping[str, Any],
    gates: Mapping[str, GateFact],
) -> Mapping[str, Any]:
    if predicate.get("op") == "var":
        name = predicate.get("name")
        gate = gates.get(str(name))
        if gate:
            return gate.predicate
    return predicate


def _classify_field(
    field_name: str,
    expression: str,
    *,
    gates: Mapping[str, GateFact],
    local_bindings: Mapping[str, str],
) -> tuple[str, str | None, dict[str, Any], tuple[str, ...], bool]:
    value = normalize_expression(expression)
    conditional_prefix = re.match(
        r"(?P<gate>[A-Za-z_][A-Za-z0-9_]*)\.then_some\(", value
    )
    if conditional_prefix:
        gate = conditional_prefix.group("gate")
        open_index = value.find("(", conditional_prefix.start())
        try:
            close_index = find_matching_delimiter(value, open_index)
        except RustSyntaxError:
            close_index = -1
        if close_index >= 0:
            suffix = value[close_index + 1 :]
            if suffix in {"", ".flatten()"}:
                gate_id = f"turn_metadata.gate.{gate}"
                return (
                    "conditional_option_passthrough" if suffix else "conditional_value",
                    normalize_expression(value[open_index + 1 : close_index]),
                    {"op": "var", "name": gate},
                    (gate_id,),
                    gate in gates,
                )
    if value in {"&self.extra", "self.extra", "self.extra.as_ref()"}:
        return "flattened_extra", value, {"op": "const", "value": True}, (), True
    if re.fullmatch(r"non_empty_[A-Za-z0-9_]+\(&self\.[A-Za-z0-9_]+\)", value):
        return (
            "non_empty_projection",
            value,
            {"op": "not", "arg": {"op": "is_empty", "path": value[value.find("&self.") + 1 : -1]}},
            (),
            True,
        )
    direct_option = re.fullmatch(
        r"self\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)\.(?:as_deref|as_ref)\(\)",
        value,
    )
    if direct_option:
        path = f"self.{direct_option.group('field')}"
        return "option_passthrough", value, {"op": "exists", "path": path}, (), True
    direct = re.fullmatch(r"(?:&)?self\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)", value)
    if direct:
        return "value_passthrough", value, {"op": "const", "value": True}, (), True
    if value in local_bindings:
        return "derived_local", value, {"op": "const", "value": True}, (), True
    if value in {"request_kind_value", "compaction"}:
        return "derived_local", value, {"op": "const", "value": True}, (), True
    # Positive classifications can be extended without changing the extractor
    # interface. Unknown helpers never silently become pass-through.
    return (
        "unclassified",
        value,
        {"op": "rust_expression", "expression": value, "machine_evaluable": False},
        (),
        False,
    )


def _empty_result() -> ExtractorResult:
    return ExtractorResult(
        extractor_id=EXTRACTOR_ID,
        schema_version="1.0.0",
        data={"gates": {}, "fields": {}, "field_groups": {}},
        semantic_complete=False,
        source_spec_ids=(SOURCE_SPEC_ID,),
    )


def _source_ref(source: str, source_file: SourceFile, index: int) -> str:
    if index < 0:
        return source_file.selected_path
    return f"{source_file.selected_path}:{line_number(source, index)}"


def _locate_payload(source: str) -> tuple[RustSpan, RustSpan]:
    function_span = find_function_body(source, "turn_metadata_payload")
    literal_span = find_struct_literal(
        source,
        "CodexTurnMetadataPayload",
        start=function_span.start,
        end=function_span.end,
    )
    return function_span, literal_span


def _extract_gates(
    *,
    source: str,
    source_file: SourceFile,
    function_span: RustSpan,
    literal_span: RustSpan,
    local_bindings: Mapping[str, str],
    field_rows: list[tuple[str, str]],
    diagnostics: DiagnosticCollector,
) -> dict[str, GateFact]:
    gate_names = {
        match.group(1)
        for _, expression in field_rows
        for match in [re.match(r"([A-Za-z_][A-Za-z0-9_]*)\.then_some\(", expression)]
        if match
    }
    gates: dict[str, GateFact] = {}
    for name in sorted(gate_names):
        expression = local_bindings.get(name)
        if expression is None:
            diagnostics.emit(
                code="FIELD_GATE_DEFINITION_MISSING",
                severity="error",
                category="semantic_classification",
                message=f"Field emission references an undefined local gate: {name}",
                extractor_id=EXTRACTOR_ID,
                entity_id=f"turn_metadata.gate.{name}",
                source_refs=(source_file.selected_path,),
                details={"gate": name},
                recoverable=False,
                strict_failure=True,
            )
            continue
        predicate, machine_evaluable = _gate_predicate(expression)
        source_index = source.find(f"let {name}", function_span.start, literal_span.start)
        source_location = _source_ref(source, source_file, source_index)
        gate = GateFact(
            id=f"turn_metadata.gate.{name}",
            name=name,
            source_expression=expression,
            predicate=predicate,
            semantic_fingerprint=semantic_fingerprint(
                {"predicate": predicate, "machine_evaluable": machine_evaluable}
            ),
            source_ref=source_location,
        )
        gates[name] = gate
        if not machine_evaluable:
            diagnostics.emit(
                code="IDENTITY_GATE_EXPRESSION_UNCLASSIFIED",
                severity="warning",
                category="semantic_classification",
                message=f"The identity gate expression for {name} is not machine-evaluable.",
                extractor_id=EXTRACTOR_ID,
                entity_id=gate.id,
                source_refs=(source_location,),
                details={"gate": name, "expression": expression},
                strict_failure=True,
            )
    return gates


def _extract_fields(
    *,
    source: str,
    source_file: SourceFile,
    literal_span: RustSpan,
    local_bindings: Mapping[str, str],
    field_rows: list[tuple[str, str]],
    gates: Mapping[str, GateFact],
    diagnostics: DiagnosticCollector,
) -> dict[str, FieldEmissionFact]:
    fields: dict[str, FieldEmissionFact] = {}
    search_offset = literal_span.start
    for field_name, expression in field_rows:
        kind, value_expression, predicate, gate_refs, machine_evaluable = _classify_field(
            field_name,
            expression,
            gates=gates,
            local_bindings=local_bindings,
        )
        source_index = source.find(field_name, search_offset, literal_span.end)
        if source_index >= 0:
            search_offset = source_index + len(field_name)
        source_location = _source_ref(source, source_file, source_index)
        emission_fingerprint = semantic_fingerprint(
            {
                "field_name": field_name,
                "kind": kind,
                "value_expression": value_expression,
                "predicate": predicate,
                "gate_refs": list(gate_refs),
                "identity_domain": _identity_domain(field_name),
            }
        )
        fingerprint = semantic_fingerprint(
            {
                "emission_fingerprint": emission_fingerprint,
                "resolved_predicate": _resolved_predicate(predicate, gates),
            }
        )
        fact = FieldEmissionFact(
            id=f"turn_metadata.field.{field_name}",
            field_name=field_name,
            kind=kind,
            source_expression=expression,
            value_expression=value_expression,
            predicate=predicate,
            gate_refs=gate_refs,
            identity_domain=_identity_domain(field_name),
            emission_fingerprint=emission_fingerprint,
            semantic_fingerprint=fingerprint,
            source_ref=source_location,
            machine_evaluable=machine_evaluable,
        )
        fields[field_name] = fact
        if not machine_evaluable:
            diagnostics.emit(
                code="FIELD_EMISSION_EXPRESSION_UNCLASSIFIED",
                severity="warning",
                category="semantic_classification",
                message=f"The emission expression for turn-metadata field {field_name} is unclassified.",
                extractor_id=EXTRACTOR_ID,
                entity_id=fact.id,
                source_refs=(source_location,),
                details={"field": field_name, "expression": expression},
                strict_failure=True,
            )
    return fields


def _field_groups(fields: Mapping[str, FieldEmissionFact]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for fact in fields.values():
        groups.setdefault(fact.identity_domain, []).append(fact.field_name)
    return {key: sorted(value) for key, value in sorted(groups.items())}


def _emit_transition_notices(
    gates: Mapping[str, GateFact], diagnostics: DiagnosticCollector
) -> None:
    if "has_turn_identity" in gates and "has_thread_identity" not in gates:
        diagnostics.emit(
            code="LEGACY_COMBINED_IDENTITY_GATE_OBSERVED",
            severity="info",
            category="source_evolution",
            message="The source still uses the older combined has_turn_identity gate name.",
            extractor_id=EXTRACTOR_ID,
            entity_id="turn_metadata.gate.has_turn_identity",
            source_refs=(gates["has_turn_identity"].source_ref,),
        )


def _is_semantically_complete(
    fields: Mapping[str, FieldEmissionFact], gates: Mapping[str, GateFact]
) -> bool:
    return all(fact.machine_evaluable for fact in fields.values()) and all(
        gate.predicate.get("machine_evaluable", True) is not False for gate in gates.values()
    )


def _result_data(
    *,
    source_file: SourceFile,
    local_bindings: Mapping[str, str],
    field_rows: list[tuple[str, str]],
    gates: Mapping[str, GateFact],
    fields: Mapping[str, FieldEmissionFact],
    semantic_complete: bool,
) -> dict[str, Any]:
    field_order = [name for name, _ in field_rows]
    return {
        "construction_function": "CodexResponsesMetadata::turn_metadata_payload",
        "source_path": source_file.selected_path,
        "source_sha256": source_file.content_sha256,
        "gates": {name: gate.to_dict() for name, gate in sorted(gates.items())},
        "fields": {name: fact.to_dict() for name, fact in sorted(fields.items())},
        "field_order": field_order,
        "field_groups": _field_groups(fields),
        "local_bindings": dict(sorted(local_bindings.items())),
        "semantic_complete": semantic_complete,
        "semantic_digest": semantic_fingerprint(
            {
                "gates": {
                    name: gate.semantic_fingerprint for name, gate in sorted(gates.items())
                },
                "fields": {
                    name: fact.semantic_fingerprint for name, fact in sorted(fields.items())
                },
                "field_order": field_order,
            }
        ),
    }


class TurnMetadataExtractor:
    extractor_id = EXTRACTOR_ID
    source_spec_ids = (SOURCE_SPEC_ID,)

    def extract(
        self,
        snapshot: SourceSnapshot,
        diagnostics: DiagnosticCollector,
    ) -> ExtractorResult:
        source_file = snapshot.files.get(SOURCE_SPEC_ID)
        if source_file is None:
            diagnostics.emit(
                code="TURN_METADATA_SOURCE_UNAVAILABLE",
                severity="error",
                category="semantic_extraction",
                message="The Responses metadata source required for turn-metadata extraction is unavailable.",
                extractor_id=self.extractor_id,
                entity_id=SOURCE_SPEC_ID,
                recoverable=False,
                strict_failure=True,
            )
            return _empty_result()

        source = source_file.text
        try:
            function_span, literal_span = _locate_payload(source)
        except RustSyntaxError as error:
            diagnostics.emit(
                code="TURN_METADATA_CONSTRUCTION_NOT_FOUND",
                severity="error",
                category="semantic_extraction",
                message="Could not locate the turn-metadata construction function and payload literal.",
                extractor_id=self.extractor_id,
                entity_id="turn_metadata.construction",
                source_refs=(source_file.selected_path,),
                details={"error": str(error)},
                recoverable=False,
                strict_failure=True,
            )
            return _empty_result()

        prefix = source[function_span.start : literal_span.start]
        local_bindings = extract_top_level_let_bindings(prefix)
        field_rows = split_struct_entries(source[literal_span.start : literal_span.end])
        gates = _extract_gates(
            source=source,
            source_file=source_file,
            function_span=function_span,
            literal_span=literal_span,
            local_bindings=local_bindings,
            field_rows=field_rows,
            diagnostics=diagnostics,
        )
        fields = _extract_fields(
            source=source,
            source_file=source_file,
            literal_span=literal_span,
            local_bindings=local_bindings,
            field_rows=field_rows,
            gates=gates,
            diagnostics=diagnostics,
        )
        _emit_transition_notices(gates, diagnostics)
        semantic_complete = _is_semantically_complete(fields, gates)
        return ExtractorResult(
            extractor_id=self.extractor_id,
            schema_version="1.0.0",
            data=_result_data(
                source_file=source_file,
                local_bindings=local_bindings,
                field_rows=field_rows,
                gates=gates,
                fields=fields,
                semantic_complete=semantic_complete,
            ),
            semantic_complete=semantic_complete,
            source_spec_ids=self.source_spec_ids,
        )


@register_extractor(EXTRACTOR_ID)
def _factory() -> TurnMetadataExtractor:
    return TurnMetadataExtractor()
