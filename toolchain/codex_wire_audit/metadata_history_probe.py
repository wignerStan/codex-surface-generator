"""Policy layer for source-backed metadata-key history verification.

The history catalog states what a key meant at an exact Codex commit. This
module selects a version, compares each structured catalog assertion with an
independent source observation, and emits stable proof diagnostics. Source and
parser mechanics live in :mod:`codex_wire_audit.metadata_history_source`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .metadata_history import (
    MetadataHistoryError,
    catalog_payload_sha256,
    select_version,
)
from .metadata_history_source import (
    LoadedHistorySource,
    load_history_sources,
    observe_config_key,
    observe_turn_metadata_key,
    resolve_commit,
)
from .proof_diagnostics import DiagnosticBag, ProofDiagnostic

PROBE_FORMAT = "codex-wire-audit-metadata-history-probe/v1"


@dataclass(frozen=True, slots=True)
class HistoryProbeResult:
    selected_version_ref: str
    selection_basis: str
    selected_commit_sha: str
    observed_commit_sha: str | None
    source_mode: str
    source_root: str
    forward_canary: bool
    complete: bool
    key_results: Mapping[str, Any]
    sources: tuple[LoadedHistorySource, ...]
    missing_source_paths: tuple[str, ...]
    diagnostics: tuple[ProofDiagnostic, ...]
    semantic_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": PROBE_FORMAT,
            "selected_version_ref": self.selected_version_ref,
            "selection_basis": self.selection_basis,
            "selected_commit_sha": self.selected_commit_sha,
            "observed_commit_sha": self.observed_commit_sha,
            "source_mode": self.source_mode,
            "source_root": self.source_root,
            "forward_canary": self.forward_canary,
            "complete": self.complete,
            "status": "complete" if self.complete else "incomplete",
            "key_results": dict(self.key_results),
            "sources": [item.public_dict() for item in self.sources],
            "missing_source_paths": list(self.missing_source_paths),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "semantic_sha256": self.semantic_sha256,
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _assertion_matches(expected: Any, observed: Any) -> bool:
    # Lists in the catalog are semantic sets (constant aliases), not source order.
    if isinstance(expected, list) and isinstance(observed, list):
        return sorted(expected) == sorted(observed)
    return expected == observed


def _key_source_paths(key: Mapping[str, Any]) -> tuple[str, ...]:
    values = key.get("source_candidates")
    if not isinstance(values, list):
        return ()
    return tuple(item for item in values if isinstance(item, str))


def _record_selection_diagnostics(
    bag: DiagnosticBag,
    *,
    root: Path,
    loaded: Mapping[str, LoadedHistorySource],
    forward_canary: bool,
    observed_commit: str | None,
    selected: Mapping[str, Any],
) -> None:
    if not loaded:
        bag.add(
            ProofDiagnostic(
                code="METADATA_HISTORY_SOURCE_SET_EMPTY",
                message="none of the metadata-history source candidates could be loaded",
                category="metadata_history",
                source_id=str(root),
            )
        )
    if forward_canary:
        bag.add(
            ProofDiagnostic(
                code="METADATA_HISTORY_FORWARD_CANARY",
                message=(
                    f"commit {observed_commit} is newer or unknown to the catalog; "
                    f"verifying it against current state {selected['id']}"
                ),
                severity="info",
                category="metadata_history",
                strict_failure=False,
                recoverable=True,
                details={
                    "observed_commit_sha": observed_commit,
                    "catalog_current_commit_sha": selected["commit_sha"],
                },
            )
        )


def _compare_assertions(
    bag: DiagnosticBag,
    *,
    key_id: str,
    wire_name: str,
    state_ref: str,
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    comparisons: dict[str, Any] = {}
    complete = True
    for assertion_name, expected_value in sorted(expected.items()):
        observed_value = observed.get(assertion_name)
        matched = _assertion_matches(expected_value, observed_value)
        comparisons[assertion_name] = {
            "expected": expected_value,
            "observed": observed_value,
            "match": matched,
        }
        if matched:
            continue
        complete = False
        if observed_value is None:
            bag.add(
                ProofDiagnostic(
                    code="METADATA_HISTORY_ASSERTION_UNOBSERVED",
                    message=f"could not observe {assertion_name} for {wire_name}",
                    category="metadata_history",
                    source_id=key_id,
                    location=f"$.states.{state_ref}.assertions.{assertion_name}",
                    details={"expected": expected_value},
                )
            )
        else:
            bag.add(
                ProofDiagnostic(
                    code="METADATA_HISTORY_ASSERTION_MISMATCH",
                    message=(
                        f"source does not match {wire_name} history assertion "
                        f"{assertion_name}"
                    ),
                    category="metadata_history",
                    source_id=key_id,
                    location=f"$.states.{state_ref}.assertions.{assertion_name}",
                    details={"expected": expected_value, "observed": observed_value},
                )
            )
    return comparisons, complete


def _probe_keys(
    keys: Mapping[str, Any],
    states: Mapping[str, Any],
    state_refs: Mapping[str, Any],
    loaded: Mapping[str, LoadedHistorySource],
    bag: DiagnosticBag,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for key_id in sorted(keys):
        key = keys[key_id]
        state_ref = state_refs.get(key_id)
        state = states.get(state_ref)
        if not isinstance(key, Mapping) or not isinstance(state, Mapping):
            bag.add(
                ProofDiagnostic(
                    code="METADATA_HISTORY_SELECTED_STATE_UNRESOLVED",
                    message=f"selected version cannot resolve state for {key_id}",
                    category="metadata_history",
                    source_id=key_id,
                )
            )
            continue
        wire_name = key.get("wire_name")
        if not isinstance(wire_name, str) or not isinstance(state_ref, str):
            continue
        observed = (
            observe_config_key(wire_name, loaded)
            if key.get("kind") == "configuration_key"
            else observe_turn_metadata_key(wire_name, loaded)
        )
        expected_value = state.get("assertions")
        expected = expected_value if isinstance(expected_value, Mapping) else {}
        comparisons, key_complete = _compare_assertions(
            bag,
            key_id=key_id,
            wire_name=wire_name,
            state_ref=state_ref,
            expected=expected,
            observed=observed,
        )
        source_paths = _key_source_paths(key)
        results[key_id] = {
            "wire_name": wire_name,
            "state_ref": state_ref,
            "state_status": state.get("status"),
            "complete": key_complete,
            "assertions": comparisons,
            "source_candidates": list(source_paths),
            "loaded_source_paths": [path for path in source_paths if path in loaded],
        }
    return results


def probe_metadata_history(
    catalog: Mapping[str, Any],
    source_root: str | Path,
    *,
    version_id: str | None = None,
    commit_sha: str | None = None,
    requested_ref: str = "HEAD",
    source_mode: str = "worktree",
) -> HistoryProbeResult:
    """Verify one catalog snapshot against source bytes.

    Unknown commits intentionally use the catalog's current state as a forward
    canary. The result records this fact; a mismatch then fails rather than
    silently treating the latest known schema as exact history.
    """

    if source_mode not in {"worktree", "commit"}:
        raise MetadataHistoryError(
            f"unsupported metadata-history source mode: {source_mode}"
        )
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise MetadataHistoryError(
            f"metadata-history source root is not a directory: {root}"
        )

    resolved_ref = resolve_commit(root, requested_ref)
    observed_commit = commit_sha or resolved_ref
    selected, basis = select_version(
        catalog,
        version_id=version_id,
        commit_sha=observed_commit,
    )
    selected_commit = selected["commit_sha"]
    forward_canary = basis == "current_forward_canary" and observed_commit != selected_commit
    read_commit = observed_commit if source_mode == "commit" else resolved_ref
    if source_mode == "commit" and read_commit is None:
        raise MetadataHistoryError(
            "commit-mode metadata-history probe requires a resolvable Git ref or commit"
        )

    keys = catalog.get("keys")
    states = catalog.get("states")
    if not isinstance(keys, Mapping) or not isinstance(states, Mapping):
        raise MetadataHistoryError("metadata history catalog key/state maps are missing")
    all_paths = [
        path
        for key in keys.values()
        if isinstance(key, Mapping)
        for path in _key_source_paths(key)
    ]
    loaded, missing = load_history_sources(
        root,
        all_paths,
        source_mode=source_mode,
        commit_sha=read_commit,
    )
    bag = DiagnosticBag()
    _record_selection_diagnostics(
        bag,
        root=root,
        loaded=loaded,
        forward_canary=forward_canary,
        observed_commit=observed_commit,
        selected=selected,
    )

    state_refs = selected.get("state_refs")
    if not isinstance(state_refs, Mapping):
        raise MetadataHistoryError(
            f"selected history version has no state map: {selected['id']}"
        )
    key_results = _probe_keys(keys, states, state_refs, loaded, bag)
    complete = not bag.has_strict_failure() and all(
        item.get("complete") for item in key_results.values()
    )
    semantic_payload = {
        "catalog_sha256": catalog_payload_sha256(catalog),
        "selected_version_ref": selected["id"],
        "selected_commit_sha": selected_commit,
        "observed_commit_sha": observed_commit,
        "key_results": key_results,
        "source_digests": {
            path: item.sha256 for path, item in sorted(loaded.items())
        },
    }
    return HistoryProbeResult(
        selected_version_ref=selected["id"],
        selection_basis=basis,
        selected_commit_sha=selected_commit,
        observed_commit_sha=observed_commit,
        source_mode=source_mode,
        source_root=str(root),
        forward_canary=forward_canary,
        complete=complete,
        key_results=key_results,
        sources=tuple(loaded[path] for path in sorted(loaded)),
        missing_source_paths=missing,
        diagnostics=tuple(bag.values()),
        semantic_sha256=hashlib.sha256(_canonical_bytes(semantic_payload)).hexdigest(),
    )
