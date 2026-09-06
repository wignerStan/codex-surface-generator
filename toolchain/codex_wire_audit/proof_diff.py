from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import os
from typing import Any, Iterable

from .proof_diagnostics import DiagnosticBag, ProofDiagnostic
from .proof_profiles import find_profile_id
from .proof_schema_validation import report_sha256, validate_report

DIFF_FORMAT = "codex-wire-audit-proof-diff/v1"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False) as handle:
        handle.write(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True).encode("ascii"))
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _entity_id(value: dict[str, Any]) -> str | None:
    for key in ("id", "field_id", "gate_id", "source_id", "extractor_id", "endpoint_id", "event_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _entities(value: Any) -> tuple[dict[str, Any], list[ProofDiagnostic]]:
    result: dict[str, Any] = {}
    diagnostics: list[ProofDiagnostic] = []

    def visit(item: Any, path: tuple[object, ...]) -> None:
        if isinstance(item, dict):
            identifier = _entity_id(item)
            if identifier is not None:
                previous = result.get(identifier)
                if previous is not None and _canonical(previous) != _canonical(item):
                    diagnostics.append(ProofDiagnostic(
                        code="DUPLICATE_ENTITY_ID_CONFLICT",
                        message=f"entity id has conflicting definitions: {identifier}",
                        source_id=identifier,
                        location="$" + "".join(f".{part}" for part in path),
                    ))
                else:
                    result[identifier] = item
            for key, child in item.items():
                visit(child, path + (key,))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, path + (index,))

    visit(value, ())
    return result, diagnostics


def _leaf_changes(before: Any, after: Any, path: tuple[object, ...] = ()) -> Iterable[tuple[tuple[object, ...], Any, Any]]:
    if type(before) is not type(after):
        yield path, before, after
        return
    if isinstance(before, dict):
        keys = sorted(set(before) | set(after))
        for key in keys:
            if key not in before:
                yield path + (key,), None, after[key]
            elif key not in after:
                yield path + (key,), before[key], None
            else:
                yield from _leaf_changes(before[key], after[key], path + (key,))
    elif isinstance(before, list):
        if _canonical(before) != _canonical(after):
            yield path, before, after
    elif before != after:
        yield path, before, after


def _classification(entity_id: str, path: tuple[object, ...], before: Any, after: Any) -> str:
    joined = ".".join(map(str, path)).lower()
    if before is None:
        return "entity_value_added"
    if after is None:
        return "entity_value_removed"
    if "gate" in entity_id.lower() or "gate" in joined or "predicate" in joined:
        return "gate_semantics_changed"
    if "source" in entity_id.lower() and any(token in joined for token in ("sha", "digest", "blob", "path")):
        return "source_identity_changed"
    if "schema" in joined:
        return "schema_changed"
    if "status" in joined or "complete" in joined:
        return "status_changed"
    if "field" in entity_id.lower() or "field" in joined:
        return "field_semantics_changed"
    return "entity_semantics_changed"


def _change_id(classification: str, entity_id: str, path: tuple[object, ...], before: Any, after: Any) -> str:
    payload = {
        "classification": classification,
        "entity_id": entity_id,
        "path": list(path),
        "before": before,
        "after": after,
    }
    return "change." + hashlib.sha256(_canonical(payload)).hexdigest()[:24]


def semantic_diff(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    bag = DiagnosticBag()
    for side, report in (("baseline", baseline), ("candidate", candidate)):
        for diagnostic in validate_report(report):
            bag.add(ProofDiagnostic(
                code=f"{side.upper()}_{diagnostic.code}",
                message=diagnostic.message,
                severity=diagnostic.severity,
                category="semantic_diff_validation",
                source_id=diagnostic.source_id,
                field_id=diagnostic.field_id,
                location=diagnostic.location,
                strict_failure=diagnostic.strict_failure,
                recoverable=diagnostic.recoverable,
                details=diagnostic.details,
            ))

    baseline_profile = find_profile_id(baseline)
    candidate_profile = find_profile_id(candidate)
    if baseline_profile != candidate_profile:
        bag.add(ProofDiagnostic(
            code="COVERAGE_PROFILE_CHANGED",
            message=f"coverage profile changed from {baseline_profile!r} to {candidate_profile!r}",
            severity="warning",
            strict_failure=False,
            recoverable=True,
        ))

    before_entities, before_diagnostics = _entities(baseline)
    after_entities, after_diagnostics = _entities(candidate)
    bag.extend(before_diagnostics)
    bag.extend(after_diagnostics)

    changes: list[dict[str, Any]] = []
    for entity_id in sorted(set(before_entities) - set(after_entities)):
        classification = "entity_removed"
        before = before_entities[entity_id]
        changes.append({
            "id": _change_id(classification, entity_id, (), before, None),
            "classification": classification,
            "entity_id": entity_id,
            "path": [],
            "before": before,
            "after": None,
        })
    for entity_id in sorted(set(after_entities) - set(before_entities)):
        classification = "entity_added"
        after = after_entities[entity_id]
        changes.append({
            "id": _change_id(classification, entity_id, (), None, after),
            "classification": classification,
            "entity_id": entity_id,
            "path": [],
            "before": None,
            "after": after,
        })
    for entity_id in sorted(set(before_entities) & set(after_entities)):
        before = before_entities[entity_id]
        after = after_entities[entity_id]
        for path, old, new in _leaf_changes(before, after):
            classification = _classification(entity_id, path, old, new)
            changes.append({
                "id": _change_id(classification, entity_id, path, old, new),
                "classification": classification,
                "entity_id": entity_id,
                "path": list(path),
                "before": old,
                "after": new,
            })

    changes.sort(key=lambda item: item["id"])
    complete = not bag.has_strict_failure()
    result: dict[str, Any] = {
        "format": DIFF_FORMAT,
        "baseline_sha256": report_sha256(baseline),
        "candidate_sha256": report_sha256(candidate),
        "baseline_profile": baseline_profile,
        "candidate_profile": candidate_profile,
        "status": {
            "overall": "complete" if complete else "incomplete",
            "complete": complete,
            "has_changes": bool(changes),
        },
        "changes": changes,
        "diagnostics": bag.to_list(),
        "summary": {
            "change_count": len(changes),
            "diagnostic_count": len(bag.values()),
            "classifications": {
                name: sum(1 for item in changes if item["classification"] == name)
                for name in sorted({item["classification"] for item in changes})
            },
        },
    }
    result["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": hashlib.sha256(_canonical(result)).hexdigest(),
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and semantically compare two audit reports.")
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fail-on", choices=("validation", "changes", "never"), default="validation")
    parser.add_argument("--debug", action="store_true")
    return parser


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    if not isinstance(baseline, dict) or not isinstance(candidate, dict):
        raise ValueError("both report roots must be JSON objects")
    result = semantic_diff(baseline, candidate)
    # Write evidence before applying validation/change policy.
    _atomic_json(args.output, result)
    if args.fail_on == "validation" and not result["status"]["complete"]:
        return 3
    if args.fail_on == "changes" and result["status"]["has_changes"]:
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        return _main(arguments)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        if "--debug" in arguments:
            raise
        print(f"codex-wire-audit-proof-diff: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
