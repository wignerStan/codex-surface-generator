from __future__ import annotations

from pathlib import Path
from typing import Any

from .metadata_history import (
    MetadataHistoryError,
    catalog_payload_sha256,
    load_history_catalog,
    validate_history_catalog,
)
from .metadata_history_probe import probe_metadata_history
from .proof_diagnostics import DiagnosticBag, ProofDiagnostic

def build_metadata_history_result(
    *,
    profile: Any | None,
    source_root: str | Path | None,
    requested_ref: str,
    source_mode: str,
    catalog_path: str | Path | None,
    version_id: str | None,
) -> tuple[dict[str, Any], list[ProofDiagnostic], tuple[str, ...]]:
    """Validate the history catalog and, when required, probe exact source bytes."""
    diagnostics: list[ProofDiagnostic] = []
    required = profile is not None and "metadata_history" in profile.required_dimensions
    required_keys = set(profile.required_history_keys if profile is not None else ())
    try:
        catalog = load_history_catalog(catalog_path)
    except MetadataHistoryError as error:
        diagnostic = ProofDiagnostic(
            code="METADATA_HISTORY_CATALOG_LOAD_FAILED",
            message=str(error),
            category="metadata_history",
        )
        diagnostics.append(diagnostic)
        return {
            "status": "incomplete" if required else "not_run",
            "complete": not required,
            "reason": "catalog_load_failed",
            "catalog_format": None,
            "catalog_version": None,
            "catalog_sha256": None,
            "reviewed_through": None,
            "selected_version_ref": None,
            "selection_basis": None,
            "selected_commit_sha": None,
            "observed_commit_sha": None,
            "forward_canary": False,
            "probe": None,
            "diagnostics": [diagnostic.to_dict()],
        }, diagnostics if required or catalog_path is not None else [], ()

    catalog_diagnostics = validate_history_catalog(catalog)
    diagnostics.extend(catalog_diagnostics)
    keys = catalog.get("keys") if isinstance(catalog.get("keys"), dict) else {}
    for key_id in sorted(required_keys - set(keys)):
        diagnostics.append(
            ProofDiagnostic(
                code="REQUIRED_METADATA_HISTORY_KEY_MISSING",
                message=f"coverage profile {profile.id} requires metadata history for {key_id}",
                category="metadata_history",
                source_id=key_id,
                details={"profile": profile.id},
            )
        )

    probe_value: dict[str, Any] | None = None
    source_paths: tuple[str, ...] = ()
    should_probe = required or source_root is not None or version_id is not None
    if should_probe and source_root is None:
        diagnostics.append(
            ProofDiagnostic(
                code="METADATA_HISTORY_SOURCE_NOT_PROVIDED",
                message="the selected proof profile requires a source root for metadata-history verification",
                category="metadata_history",
            )
        )
    elif should_probe and source_root is not None and not catalog_diagnostics:
        try:
            probe = probe_metadata_history(
                catalog,
                source_root,
                version_id=version_id,
                requested_ref=requested_ref,
                source_mode=source_mode,
            )
        except MetadataHistoryError as error:
            diagnostics.append(
                ProofDiagnostic(
                    code="METADATA_HISTORY_PROBE_FAILED",
                    message=str(error),
                    category="metadata_history",
                )
            )
        else:
            probe_value = probe.to_dict()
            source_paths = tuple(item.path for item in probe.sources)
            diagnostics.extend(probe.diagnostics)

    local_bag = DiagnosticBag(diagnostics)
    complete = not local_bag.has_strict_failure() and (
        probe_value is not None and bool(probe_value.get("complete"))
        if required
        else True
    )
    status = "complete" if complete and should_probe else "not_run"
    if required and not complete:
        status = "incomplete"
    current = catalog.get("versions", {}).get(catalog.get("current_version_ref"), {})
    result = {
        "status": status,
        "complete": complete,
        "catalog_format": catalog.get("format"),
        "catalog_version": catalog.get("catalog_version"),
        "catalog_sha256": catalog_payload_sha256(catalog),
        "reviewed_through": catalog.get("reviewed_through"),
        "selected_version_ref": (
            probe_value.get("selected_version_ref") if probe_value else catalog.get("current_version_ref")
        ),
        "selection_basis": probe_value.get("selection_basis") if probe_value else None,
        "selected_commit_sha": (
            probe_value.get("selected_commit_sha") if probe_value else current.get("commit_sha")
        ),
        "observed_commit_sha": probe_value.get("observed_commit_sha") if probe_value else None,
        "forward_canary": bool(probe_value and probe_value.get("forward_canary")),
        "probe": probe_value,
        "diagnostics": [item.to_dict() for item in local_bag.values()],
    }
    return result, local_bag.values(), source_paths

