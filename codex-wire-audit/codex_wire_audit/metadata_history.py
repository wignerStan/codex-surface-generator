"""Versioned metadata-key history and deterministic multi-JSON export.

The history catalog is deliberately separate from the current-source report.  It
records exact transition commits and lets CI distinguish an active field, a
removed-but-reserved key, a compatibility alias, and a renamed configuration
setting without interpreting prose.
"""

from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from .proof_diagnostics import ProofDiagnostic

HISTORY_FORMAT = "codex-wire-audit-metadata-key-history/v1"
HISTORY_SCHEMA_ID = (
    "https://openai.example/codex-wire-audit/metadata-key-history-v1.schema.json"
)
CONTAINER_FORMAT = "codex-wire-audit-metadata-history-container/v1"
KEY_DOCUMENT_FORMAT = "codex-wire-audit-metadata-history-key/v1"
VERSION_DOCUMENT_FORMAT = "codex-wire-audit-metadata-history-version/v1"


class MetadataHistoryError(ValueError):
    """Raised when a history catalog cannot be loaded or selected."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def catalog_payload_sha256(catalog: Mapping[str, Any]) -> str:
    payload = dict(catalog)
    payload.pop("integrity", None)
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _resource_text(directory: str, name: str) -> str:
    resource = files(__package__).joinpath(directory, name)
    return resource.read_text(encoding="utf-8")


def load_history_catalog(path: str | Path | None = None) -> dict[str, Any]:
    try:
        text = (
            Path(path).read_text(encoding="utf-8")
            if path is not None
            else _resource_text(
                "metadata_history_data", "metadata-key-history.v1.json"
            )
        )
        value = json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        raise MetadataHistoryError(f"cannot load metadata history catalog: {error}") from error
    if not isinstance(value, dict):
        raise MetadataHistoryError("metadata history catalog root must be an object")
    return value


def _load_history_schema(path: str | Path | None = None) -> dict[str, Any]:
    try:
        text = (
            Path(path).read_text(encoding="utf-8")
            if path is not None
            else _resource_text(
                "proof_schema_templates", "metadata-key-history-v1.schema.json"
            )
        )
        value = json.loads(text)
    except (OSError, json.JSONDecodeError) as error:
        raise MetadataHistoryError(f"cannot load metadata history schema: {error}") from error
    if not isinstance(value, dict):
        raise MetadataHistoryError("metadata history schema root must be an object")
    return value


def _path(parts: Iterable[object]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def _diagnostic(
    code: str,
    message: str,
    *,
    location: str | None = None,
    source_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> ProofDiagnostic:
    return ProofDiagnostic(
        code=code,
        message=message,
        category="metadata_history",
        location=location,
        source_id=source_id,
        details=details,
    )


def _schema_diagnostics(
    catalog: dict[str, Any], schema_path: str | Path | None
) -> list[ProofDiagnostic]:
    try:
        schema = _load_history_schema(schema_path)
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(catalog),
            key=lambda item: (tuple(map(str, item.absolute_path)), item.message),
        )
    except Exception as error:  # jsonschema exposes several concrete error types.
        return [_diagnostic("METADATA_HISTORY_SCHEMA_INVALID", str(error))]
    return [
        _diagnostic(
            "METADATA_HISTORY_SCHEMA_VIOLATION",
            error.message,
            location=_path(error.absolute_path),
            details={"validator": error.validator},
        )
        for error in errors
    ]


def _key_invariants(
    keys: Mapping[str, Any], states: Mapping[str, Any]
) -> list[ProofDiagnostic]:
    diagnostics: list[ProofDiagnostic] = []
    for key_id, key in keys.items():
        if not isinstance(key, dict):
            continue
        if key.get("id") != key_id:
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_KEY_ID_MISMATCH",
                    f"key map entry {key_id!r} does not match embedded id {key.get('id')!r}",
                    location=f"$.keys.{key_id}",
                    source_id=key_id,
                )
            )
        declared = key.get("state_refs", [])
        declared_refs = declared if isinstance(declared, list) else []
        for state_ref in declared_refs:
            state = states.get(state_ref)
            if not isinstance(state, dict):
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_STATE_REF_UNRESOLVED",
                        f"key {key_id} references missing state {state_ref}",
                        location=f"$.keys.{key_id}.state_refs",
                        source_id=key_id,
                    )
                )
            elif state.get("key_ref") != key_id:
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_STATE_OWNER_MISMATCH",
                        f"state {state_ref} belongs to {state.get('key_ref')}, not {key_id}",
                        location=f"$.states.{state_ref}",
                        source_id=state_ref,
                    )
                )
        current = key.get("current_state_ref")
        if current not in declared_refs:
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_CURRENT_STATE_NOT_DECLARED",
                    f"current state {current!r} is not declared by key {key_id}",
                    location=f"$.keys.{key_id}.current_state_ref",
                    source_id=key_id,
                )
            )
        for replacement in key.get("replacement_key_refs", []):
            if replacement not in keys:
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_REPLACEMENT_UNRESOLVED",
                        f"replacement key does not exist: {replacement}",
                        location=f"$.keys.{key_id}.replacement_key_refs",
                        source_id=key_id,
                    )
                )
    return diagnostics


def _version_invariants(
    keys: Mapping[str, Any],
    states: Mapping[str, Any],
    versions: Mapping[str, Any],
) -> tuple[list[ProofDiagnostic], set[str], dict[int, str]]:
    diagnostics: list[ProofDiagnostic] = []
    evidence_ids: set[str] = set()
    ordinals: dict[int, str] = {}
    commits: dict[str, str] = {}
    for version_id, version in versions.items():
        if not isinstance(version, dict):
            continue
        if version.get("id") != version_id:
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_VERSION_ID_MISMATCH",
                    f"version map entry {version_id!r} does not match embedded id",
                    location=f"$.versions.{version_id}",
                    source_id=version_id,
                )
            )
        ordinal = version.get("ordinal")
        if isinstance(ordinal, int):
            previous = ordinals.get(ordinal)
            if previous is not None:
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_DUPLICATE_ORDINAL",
                        f"versions {previous} and {version_id} share ordinal {ordinal}",
                        source_id=version_id,
                    )
                )
            ordinals[ordinal] = version_id
        commit = version.get("commit_sha")
        if isinstance(commit, str):
            previous = commits.get(commit)
            if previous is not None:
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_DUPLICATE_COMMIT",
                        f"versions {previous} and {version_id} share commit {commit}",
                        source_id=version_id,
                    )
                )
            commits[commit] = version_id
        evidence = version.get("evidence")
        if isinstance(evidence, dict) and isinstance(evidence.get("id"), str):
            evidence_ids.add(evidence["id"])
            if evidence.get("commit_sha") != version.get("commit_sha"):
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_EVIDENCE_COMMIT_MISMATCH",
                        f"version {version_id} and its evidence name different commits",
                        source_id=version_id,
                    )
                )
        diagnostics.extend(_version_state_invariants(version_id, version, keys, states))
    return diagnostics, evidence_ids, ordinals


def _version_state_invariants(
    version_id: str,
    version: Mapping[str, Any],
    keys: Mapping[str, Any],
    states: Mapping[str, Any],
) -> list[ProofDiagnostic]:
    diagnostics: list[ProofDiagnostic] = []
    state_refs = version.get("state_refs")
    if not isinstance(state_refs, dict):
        return diagnostics
    if set(state_refs) != set(keys):
        diagnostics.append(
            _diagnostic(
                "METADATA_HISTORY_VERSION_KEY_SET_MISMATCH",
                f"version {version_id} must provide one state for every tracked key",
                source_id=version_id,
                details={
                    "missing": sorted(set(keys) - set(state_refs)),
                    "extra": sorted(set(state_refs) - set(keys)),
                },
            )
        )
    for key_id, state_ref in state_refs.items():
        state = states.get(state_ref)
        if not isinstance(state, dict):
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_VERSION_STATE_UNRESOLVED",
                    f"version {version_id} references missing state {state_ref}",
                    source_id=version_id,
                )
            )
        elif state.get("key_ref") != key_id:
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_VERSION_STATE_OWNER_MISMATCH",
                    f"version {version_id} maps {key_id} to a state owned by {state.get('key_ref')}",
                    source_id=version_id,
                )
            )
    return diagnostics


def _current_and_state_invariants(
    catalog: Mapping[str, Any],
    keys: Mapping[str, Any],
    states: Mapping[str, Any],
    versions: Mapping[str, Any],
    evidence_ids: set[str],
    ordinals: Mapping[int, str],
) -> list[ProofDiagnostic]:
    diagnostics: list[ProofDiagnostic] = []
    if ordinals and sorted(ordinals) != list(range(len(ordinals))):
        diagnostics.append(
            _diagnostic(
                "METADATA_HISTORY_ORDINALS_NOT_CONTIGUOUS",
                "version ordinals must be contiguous and zero-based",
                details={"ordinals": sorted(ordinals)},
            )
        )
    current_ref = catalog.get("current_version_ref")
    current = versions.get(current_ref)
    if not isinstance(current, dict):
        diagnostics.append(
            _diagnostic(
                "METADATA_HISTORY_CURRENT_VERSION_UNRESOLVED",
                f"current version does not exist: {current_ref}",
                location="$.current_version_ref",
            )
        )
    else:
        current_states = current.get("state_refs", {})
        for key_id, key in keys.items():
            if isinstance(key, dict) and current_states.get(key_id) != key.get("current_state_ref"):
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_CURRENT_STATE_MISMATCH",
                        f"key {key_id} current_state_ref disagrees with current version",
                        source_id=key_id,
                    )
                )
    for state_id, state in states.items():
        if not isinstance(state, dict):
            continue
        if state.get("id") != state_id:
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_STATE_ID_MISMATCH",
                    f"state map entry {state_id!r} does not match embedded id",
                    source_id=state_id,
                )
            )
        for evidence_ref in state.get("evidence_refs", []):
            if evidence_ref not in evidence_ids:
                diagnostics.append(
                    _diagnostic(
                        "METADATA_HISTORY_EVIDENCE_REF_UNRESOLVED",
                        f"state {state_id} references unknown evidence {evidence_ref}",
                        source_id=state_id,
                    )
                )
    return diagnostics


def _transition_invariants(
    transitions: Iterable[Any],
    keys: Mapping[str, Any],
    states: Mapping[str, Any],
    versions: Mapping[str, Any],
) -> list[ProofDiagnostic]:
    diagnostics: list[ProofDiagnostic] = []
    transition_ids: set[str] = set()
    for index, transition in enumerate(transitions):
        if not isinstance(transition, dict):
            continue
        transition_id = transition.get("id")
        if transition_id in transition_ids:
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_DUPLICATE_TRANSITION",
                    f"duplicate transition id: {transition_id}",
                    location=f"$.transitions[{index}]",
                )
            )
        if isinstance(transition_id, str):
            transition_ids.add(transition_id)
        key_ref = transition.get("key_ref")
        before = states.get(transition.get("from_state_ref"))
        after = states.get(transition.get("to_state_ref"))
        version = versions.get(transition.get("at_version_ref"))
        if key_ref not in keys or not isinstance(before, dict) or not isinstance(after, dict) or not isinstance(version, dict):
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_TRANSITION_REF_UNRESOLVED",
                    f"transition {transition_id} contains an unresolved reference",
                    location=f"$.transitions[{index}]",
                    source_id=transition_id if isinstance(transition_id, str) else None,
                )
            )
            continue
        if before.get("key_ref") != key_ref or after.get("key_ref") != key_ref:
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_TRANSITION_OWNER_MISMATCH",
                    f"transition {transition_id} crosses states owned by another key",
                    source_id=transition_id,
                )
            )
        if version.get("state_refs", {}).get(key_ref) != transition.get("to_state_ref"):
            diagnostics.append(
                _diagnostic(
                    "METADATA_HISTORY_TRANSITION_TARGET_MISMATCH",
                    f"transition {transition_id} target is not active at its declared version",
                    source_id=transition_id,
                )
            )
    return diagnostics


def _invariant_diagnostics(catalog: dict[str, Any]) -> list[ProofDiagnostic]:
    keys = catalog.get("keys") if isinstance(catalog.get("keys"), dict) else {}
    states = catalog.get("states") if isinstance(catalog.get("states"), dict) else {}
    versions = catalog.get("versions") if isinstance(catalog.get("versions"), dict) else {}
    transitions = catalog.get("transitions") if isinstance(catalog.get("transitions"), list) else []
    diagnostics = _key_invariants(keys, states)
    version_diagnostics, evidence_ids, ordinals = _version_invariants(keys, states, versions)
    diagnostics.extend(version_diagnostics)
    diagnostics.extend(
        _current_and_state_invariants(
            catalog, keys, states, versions, evidence_ids, ordinals
        )
    )
    diagnostics.extend(_transition_invariants(transitions, keys, states, versions))
    claimed = (catalog.get("integrity") or {}).get("canonical_payload_sha256")
    actual = catalog_payload_sha256(catalog)
    if claimed != actual:
        diagnostics.append(
            _diagnostic(
                "METADATA_HISTORY_INTEGRITY_MISMATCH",
                "metadata history canonical payload digest does not match",
                location="$.integrity.canonical_payload_sha256",
                details={"claimed": claimed, "actual": actual},
            )
        )
    return diagnostics


def validate_history_catalog(
    catalog: dict[str, Any], schema_path: str | Path | None = None
) -> list[ProofDiagnostic]:
    diagnostics = _schema_diagnostics(catalog, schema_path)
    diagnostics.extend(_invariant_diagnostics(catalog))
    return diagnostics


def select_version(
    catalog: Mapping[str, Any],
    *,
    version_id: str | None = None,
    commit_sha: str | None = None,
) -> tuple[dict[str, Any], str]:
    versions = catalog.get("versions")
    if not isinstance(versions, Mapping):
        raise MetadataHistoryError("catalog versions map is missing")
    if version_id is not None:
        value = versions.get(version_id)
        if not isinstance(value, dict):
            raise MetadataHistoryError(f"unknown metadata-history version: {version_id}")
        if commit_sha is not None and value.get("commit_sha") != commit_sha:
            raise MetadataHistoryError(
                f"metadata-history version {version_id} is bound to {value.get('commit_sha')}, not {commit_sha}"
            )
        return dict(value), "explicit_version"
    if commit_sha is not None:
        matches = [
            value
            for value in versions.values()
            if isinstance(value, dict) and value.get("commit_sha") == commit_sha
        ]
        if len(matches) == 1:
            return dict(matches[0]), "exact_commit"
        if len(matches) > 1:
            raise MetadataHistoryError(
                f"multiple metadata-history versions claim commit {commit_sha}"
            )
    current_ref = catalog.get("current_version_ref")
    current = versions.get(current_ref)
    if not isinstance(current, dict):
        raise MetadataHistoryError(f"current metadata-history version is unresolved: {current_ref}")
    return dict(current), "current_forward_canary"


def history_key_view(catalog: Mapping[str, Any], key_id: str) -> dict[str, Any]:
    keys = catalog.get("keys")
    states = catalog.get("states")
    transitions = catalog.get("transitions")
    if not isinstance(keys, Mapping) or not isinstance(states, Mapping):
        raise MetadataHistoryError("catalog key/state maps are missing")
    key = keys.get(key_id)
    if not isinstance(key, dict):
        # Accept an unambiguous wire name as a CLI convenience.
        matches = [item for item in keys.values() if isinstance(item, dict) and item.get("wire_name") == key_id]
        if len(matches) != 1:
            raise MetadataHistoryError(f"unknown or ambiguous metadata-history key: {key_id}")
        key = matches[0]
        key_id = key["id"]
    state_map = {
        ref: states[ref]
        for ref in key.get("state_refs", [])
        if ref in states
    }
    transition_rows = [
        item for item in transitions or []
        if isinstance(item, dict) and item.get("key_ref") == key_id
    ]
    versions = [
        version for version in (catalog.get("versions") or {}).values()
        if isinstance(version, dict) and key_id in version.get("state_refs", {})
    ]
    versions.sort(key=lambda item: item.get("ordinal", -1))
    return {
        "format": KEY_DOCUMENT_FORMAT,
        "catalog_version": catalog.get("catalog_version"),
        "catalog_sha256": catalog_payload_sha256(catalog),
        "key": key,
        "states": state_map,
        "timeline": [
            {
                "version_ref": item["id"],
                "ordinal": item["ordinal"],
                "commit_sha": item["commit_sha"],
                "committed_at": item["committed_at"],
                "state_ref": item["state_refs"][key_id],
            }
            for item in versions
        ],
        "transitions": transition_rows,
    }


def history_version_view(catalog: Mapping[str, Any], version: Mapping[str, Any]) -> dict[str, Any]:
    states = catalog.get("states") if isinstance(catalog.get("states"), Mapping) else {}
    state_refs = version.get("state_refs") if isinstance(version.get("state_refs"), Mapping) else {}
    return {
        "format": VERSION_DOCUMENT_FORMAT,
        "catalog_version": catalog.get("catalog_version"),
        "catalog_sha256": catalog_payload_sha256(catalog),
        "version": dict(version),
        "states": {
            key_id: states[state_ref]
            for key_id, state_ref in sorted(state_refs.items())
            if state_ref in states
        },
    }


def diff_versions(
    catalog: Mapping[str, Any], baseline_id: str, candidate_id: str
) -> dict[str, Any]:
    baseline, _ = select_version(catalog, version_id=baseline_id)
    candidate, _ = select_version(catalog, version_id=candidate_id)
    before = baseline.get("state_refs", {})
    after = candidate.get("state_refs", {})
    changes = []
    for key_id in sorted(set(before) | set(after)):
        if before.get(key_id) == after.get(key_id):
            continue
        identity = {
            "key_ref": key_id,
            "before_state_ref": before.get(key_id),
            "after_state_ref": after.get(key_id),
        }
        changes.append(
            {
                "id": "history-change."
                + hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:20],
                **identity,
            }
        )
    return {
        "format": "codex-wire-audit-metadata-history-diff/v1",
        "catalog_sha256": catalog_payload_sha256(catalog),
        "baseline_version_ref": baseline_id,
        "candidate_version_ref": candidate_id,
        "changes": changes,
        "change_count": len(changes),
    }
