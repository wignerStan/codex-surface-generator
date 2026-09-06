from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .proof_diagnostics import ProofDiagnostic


def _schema_directories(explicit: str | Path | None = None) -> list[Path]:
    package = Path(__file__).resolve().parent
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    candidates.extend([
        package / "proof_schema_templates",
        package / "schema_templates",
        package / "schemas",
        package.parent / "schemas",
        package.parent / "machine_assets" / "schemas",
    ])
    result: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved.is_dir() and resolved not in result:
            result.append(resolved)
    return result


def load_schemas(explicit: str | Path | None = None) -> tuple[dict[str, dict[str, Any]], dict[Path, dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_path: dict[Path, dict[str, Any]] = {}
    for directory in _schema_directories(explicit):
        for path in sorted(directory.rglob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or "$schema" not in value:
                continue
            by_path[path] = value
            schema_id = value.get("$id")
            if isinstance(schema_id, str):
                by_id[schema_id] = value
    return by_id, by_path


def _candidate_identifiers(report: dict[str, Any]) -> Iterable[str]:
    for key in ("$schema", "schema_id", "contract_schema", "schema"):
        value = report.get(key)
        if isinstance(value, str):
            yield value
    integrity = report.get("integrity")
    if isinstance(integrity, dict):
        for key in ("schema_id", "contract_schema"):
            value = integrity.get(key)
            if isinstance(value, str):
                yield value


def select_root_schema(report: dict[str, Any], by_id: dict[str, dict[str, Any]], by_path: dict[Path, dict[str, Any]]) -> dict[str, Any] | None:
    for identifier in _candidate_identifiers(report):
        if identifier in by_id:
            return by_id[identifier]
        for path, schema in by_path.items():
            if path.name == Path(identifier).name:
                return schema
    scored: list[tuple[int, str, dict[str, Any]]] = []
    report_keys = set(report)
    for path, schema in by_path.items():
        required = set(schema.get("required", []))
        score = len(required & report_keys) - len(required - report_keys) * 3
        name = path.name.lower()
        if "evolution" in name or "contract" in name or "report" in name:
            score += 3
        scored.append((score, str(path), schema))
    return max(scored, default=(0, "", None))[2]


def _registry(by_id: dict[str, dict[str, Any]]) -> Registry:
    registry = Registry()
    for identifier, schema in sorted(by_id.items()):
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = registry.with_resource(identifier, resource)
    return registry


def _json_path(parts: Iterable[object]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def _schema_errors(report: dict[str, Any], explicit: str | Path | None) -> list[ProofDiagnostic]:
    by_id, by_path = load_schemas(explicit)
    if not by_path:
        return [ProofDiagnostic(code="SCHEMA_BUNDLE_MISSING", message="no installed JSON Schema documents were found")]
    schema = select_root_schema(report, by_id, by_path)
    if schema is None:
        return [ProofDiagnostic(code="SCHEMA_ROOT_UNRESOLVED", message="could not select a root schema for the report")]
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as error:  # jsonschema exposes several concrete schema errors
        return [ProofDiagnostic(code="SCHEMA_DOCUMENT_INVALID", message=str(error))]
    diagnostics: list[ProofDiagnostic] = []
    try:
        validator = Draft202012Validator(schema, registry=_registry(by_id))
        errors = list(validator.iter_errors(report))
    except Exception as error:
        return [ProofDiagnostic(
            code="SCHEMA_REFERENCE_UNRESOLVED",
            message=str(error),
        )]
    for error in sorted(errors, key=lambda item: (tuple(map(str, item.absolute_path)), item.message)):
        diagnostics.append(ProofDiagnostic(
            code="REPORT_SCHEMA_VIOLATION",
            message=error.message,
            location=_json_path(error.absolute_path),
            details={"validator": error.validator},
        ))
    return diagnostics


def _walk(value: Any, path: tuple[object, ...] = ()) -> Iterable[tuple[tuple[object, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, path + (index,))


def _semantic_invariants(report: dict[str, Any]) -> list[ProofDiagnostic]:
    diagnostics: list[ProofDiagnostic] = []
    for path, value in _walk(report):
        if not isinstance(value, dict):
            continue
        location = _json_path(path)
        # Maps of entities must agree with embedded IDs when an ID field is present.
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                continue
            embedded = None
            for id_key in ("id", "field_id", "gate_id", "source_id", "extractor_id"):
                if isinstance(item.get(id_key), str):
                    embedded = item[id_key]
                    break
            if embedded is not None and (key.startswith(("field.", "gate.", "source.", "extractor.")) or embedded.startswith(("field.", "gate.", "source.", "extractor."))):
                if key != embedded:
                    diagnostics.append(ProofDiagnostic(
                        code="ENTITY_MAP_KEY_ID_MISMATCH",
                        message=f"map key {key!r} does not match embedded id {embedded!r}",
                        location=f"{location}.{key}",
                    ))

        fields = value.get("fields")
        order = value.get("field_order")
        if isinstance(fields, dict) and isinstance(order, list) and all(isinstance(item, str) for item in order):
            if set(fields) != set(order) or len(order) != len(set(order)):
                diagnostics.append(ProofDiagnostic(
                    code="FIELD_ORDER_NOT_BIJECTIVE",
                    message="field_order must contain every field exactly once",
                    location=location,
                    details={"fields": sorted(fields), "field_order": order},
                ))

        for count_key, collection_key in (
            ("field_count", "fields"), ("gate_count", "gates"), ("source_count", "sources"),
            ("diagnostic_count", "diagnostics"), ("extractor_count", "extractors"),
        ):
            count = value.get(count_key)
            collection = value.get(collection_key)
            if isinstance(count, int) and isinstance(collection, (dict, list)) and count != len(collection):
                diagnostics.append(ProofDiagnostic(
                    code="DECLARED_COUNT_MISMATCH",
                    message=f"{count_key}={count} but {collection_key} contains {len(collection)} items",
                    location=location,
                ))

        selected = value.get("selected_path")
        candidates = value.get("candidate_paths") or value.get("candidates")
        if isinstance(selected, str) and isinstance(candidates, list) and candidates and selected not in candidates:
            diagnostics.append(ProofDiagnostic(
                code="SELECTED_PATH_NOT_DECLARED",
                message=f"selected path {selected!r} is not among declared candidates",
                location=location,
            ))
    return diagnostics


def validate_report(report: dict[str, Any], schema_dir: str | Path | None = None) -> list[ProofDiagnostic]:
    diagnostics = _schema_errors(report, schema_dir)
    diagnostics.extend(_semantic_invariants(report))
    return diagnostics


def report_sha256(report: dict[str, Any]) -> str:
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def verify_attestation(attestation: dict[str, Any], schema_dir: str | Path | None = None) -> list[ProofDiagnostic]:
    diagnostics = validate_report(attestation, schema_dir)
    integrity = attestation.get("integrity")
    if not isinstance(integrity, dict):
        diagnostics.append(ProofDiagnostic(code="ATTESTATION_INTEGRITY_MISSING", message="attestation integrity object is missing"))
        return diagnostics
    claimed = integrity.get("canonical_payload_sha256")
    payload = dict(attestation)
    payload.pop("integrity", None)
    actual = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    if claimed != actual:
        diagnostics.append(ProofDiagnostic(
            code="ATTESTATION_INTEGRITY_MISMATCH",
            message="attestation canonical payload digest does not match",
            details={"claimed": claimed, "actual": actual},
        ))
    return diagnostics
