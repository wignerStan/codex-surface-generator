"""Generation orchestration for the maintainability transition release."""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, MutableMapping

from . import GENERATOR_VERSION
from .canonical import canonical_json_bytes, canonical_report_payload
from .diagnostics import Diagnostic, DiagnosticCollector
from .evolution import (
    apply_turn_metadata_overlay,
    apply_config_surface_overlay,
    apply_local_storage_overlay,
    build_evolution_contract,
    finalize_evolution_contract,
    finalize_system_report,
)
from .legacy import LegacyModules
from .models import SourceGroup, SourceSnapshot
from .schema_identity import SchemaIdentityResolver, emit_schema_conflict_diagnostics
from .semantic_diff import SemanticDiffError, compare as semantic_compare
from .sources import (
    SnapshotLoadResult,
    SourceLoadError,
    load_archive_snapshot,
    load_cache_snapshot,
    load_github_snapshot,
    load_repo_snapshot,
)
from .source_registry import SourceRegistry, from_legacy_maps


@dataclass(slots=True)
class GenerationResult:
    report: dict[str, Any]
    snapshot: SourceSnapshot
    registry: SourceRegistry
    diagnostics: DiagnosticCollector
    semantic_diff: dict[str, Any] | None = None


def build_registry(legacy: LegacyModules, overlay_path: str | None = None) -> SourceRegistry:
    registry = from_legacy_maps(
        legacy.base.FILES,
        legacy.base.SURFACE_FILES,
        legacy.entrypoint.EXTRA,
    )
    if overlay_path:
        overlay = SourceRegistry.load_overlay(overlay_path)
        registry = registry.with_overlay(overlay)
    return registry


def load_snapshot(args: Any, legacy: LegacyModules, registry: SourceRegistry) -> SnapshotLoadResult:
    if getattr(args, "repo_root", None):
        return load_repo_snapshot(
            registry,
            args.repo_root,
            repository=args.repo,
            requested_ref=args.ref,
            source_commit=getattr(args, "source_commit", None),
            allow_dirty=getattr(args, "allow_dirty_source", False),
            worktree=getattr(args, "worktree", False),
        )
    if getattr(args, "source_archive", None):
        return load_archive_snapshot(
            registry,
            args.source_archive,
            repository=args.repo,
            requested_ref=args.ref,
            source_commit=getattr(args, "source_commit", None),
        )
    if getattr(args, "cache_only", False):
        return load_cache_snapshot(
            registry,
            cache_dir=args.cache_dir,
            repository=args.repo,
            commit_sha=args.ref,
        )
    return load_github_snapshot(
        registry,
        repository=args.repo,
        requested_ref=args.ref,
        api_base=args.api_base,
        token=args.token,
        legacy_base=legacy.base,
        max_workers=getattr(args, "fetch_workers", 8),
    )


def _legacy_warning_lists(
    snapshot: SourceSnapshot,
    registry: SourceRegistry,
) -> tuple[list[str], list[str]]:
    surface: list[str] = []
    extra: list[str] = []
    for spec_id, reason in sorted(snapshot.unavailable_specs.items()):
        spec = registry.get(spec_id)
        if spec.group == SourceGroup.SURFACE:
            surface.append(
                f"optional wire surface source unavailable: {spec.primary_path}: {reason}"
            )
        elif spec.group == SourceGroup.EXTRA:
            extra.append(f"optional schema source unavailable: {spec.primary_path}: {reason}")
    return surface, extra


def combined_source_descriptors(
    snapshot: SourceSnapshot,
    registry: SourceRegistry,
) -> dict[str, dict[str, Any]]:
    """Build descriptors for v10 while preserving selected-path provenance."""
    combined: dict[str, dict[str, Any]] = {}
    for spec in registry.specs:
        source_file = snapshot.files.get(spec.id)
        primary = spec.primary_path
        roles = [f"{spec.group.value}:{spec.legacy_key}", *spec.roles]
        if source_file is None:
            combined[primary] = {
                "text": None,
                "roles": roles,
                "available": False,
                "source_spec_id": spec.id,
                "path_candidates": list(spec.path_candidates),
            }
            continue
        # Legacy source anchors use the primary hardcoded path.  Retain a marked
        # logical alias while also exposing the actual selected path.
        combined[primary] = {
            "text": source_file.text,
            "roles": roles,
            "available": True,
            "source_spec_id": spec.id,
            "resolved_path": source_file.selected_path,
            "logical_path_alias": primary != source_file.selected_path,
        }
        if primary != source_file.selected_path:
            combined[source_file.selected_path] = {
                "text": source_file.text,
                "roles": [*roles, "resolved_path"],
                "available": True,
                "source_spec_id": spec.id,
                "resolved_path": source_file.selected_path,
                "logical_path_alias": False,
            }
    return combined


def _patch_exact_source_manifest(
    report: MutableMapping[str, Any],
    snapshot: SourceSnapshot,
    registry: SourceRegistry,
) -> None:
    manifest = report.get("source_manifest")
    if not isinstance(manifest, MutableMapping):
        return
    manifest["source_revision_id"] = snapshot.revision.to_dict()["source_revision_id"]
    manifest["source_set_sha256"] = snapshot.revision.source_set_sha256
    manifest["byte_identity"] = "exact_source_snapshot"
    manifest["exact_snapshot"] = snapshot.manifest()
    path_index = manifest.get("path_index")
    files = manifest.get("files")
    if not isinstance(path_index, Mapping) or not isinstance(files, MutableMapping):
        return
    for spec in registry.specs:
        source_file = snapshot.files.get(spec.id)
        if source_file is None:
            continue
        for path in {spec.primary_path, source_file.selected_path}:
            source_id = path_index.get(path)
            entry = files.get(source_id) if isinstance(source_id, str) else None
            if not isinstance(entry, MutableMapping):
                continue
            entry.update(
                {
                    "byte_length": len(source_file.raw_bytes),
                    "line_count": source_file.text.count("\n")
                    + (0 if not source_file.text or source_file.text.endswith("\n") else 1),
                    "sha256": source_file.content_sha256,
                    "git_blob_sha": source_file.git_blob_sha,
                    "repository_blob_sha": source_file.repository_blob_sha,
                    "byte_identity": "exact",
                    "source_spec_id": spec.id,
                    "resolved_path": source_file.selected_path,
                    "logical_path_alias": path != source_file.selected_path,
                    "path_candidate_index": source_file.path_candidate_index,
                }
            )


def _annotate_qualified_schemas(
    report: MutableMapping[str, Any],
    resolver: SchemaIdentityResolver,
    diagnostics: DiagnosticCollector,
) -> None:
    entities = report.get("entities")
    if not isinstance(entities, MutableMapping):
        return
    schemas = entities.get("schemas")
    fields = entities.get("fields")
    if not isinstance(schemas, MutableMapping):
        return
    for schema_id, metadata in resolver.metadata.items():
        schema = schemas.get(schema_id)
        if isinstance(schema, MutableMapping):
            schema.update(
                {
                    "rust_name": metadata.get("rust_name"),
                    "qualified_rust_name": metadata.get("qualified_rust_name"),
                    "source_path": metadata.get("source_path"),
                    "schema_identity_fallback": bool(metadata.get("fallback")),
                }
            )
            shape = {
                "field_ids": schema.get("field_ids") or [],
                "variants": schema.get("variants") or [],
                "kind": schema.get("kind"),
            }
            schema["shape_fingerprint"] = hashlib.sha256(
                canonical_json_bytes(shape)
            ).hexdigest()

    # Resolve simple named-type aliases when exactly one qualified schema exists.
    by_name: dict[str, list[str]] = {}
    for schema_id, schema in schemas.items():
        if not isinstance(schema, Mapping):
            continue
        if schema.get("kind") == "named_type_reference":
            continue
        name = schema.get("name")
        if isinstance(name, str):
            by_name.setdefault(name.split("<", 1)[0], []).append(str(schema_id))

    def rewrite_refs(value: Any, field_id: str) -> None:
        if isinstance(value, MutableMapping):
            ref = value.get("x-entity-ref")
            if isinstance(ref, str) and ref.startswith("schema.rust."):
                leaf = ref.rsplit(".", 1)[-1]
                candidates = by_name.get(leaf) or by_name.get(leaf[:1].upper() + leaf[1:]) or []
                if len(candidates) == 1:
                    value["x-entity-ref"] = candidates[0]
                    value["x-entity-ref-qualified"] = True
                elif len(candidates) > 1:
                    diagnostics.emit(
                        code="NAMED_TYPE_REFERENCE_AMBIGUOUS",
                        severity="warning",
                        category="schema_resolution",
                        message=f"A named Rust type reference is ambiguous: {leaf}",
                        extractor_id="schema_identity",
                        entity_id=field_id,
                        details={"name": leaf, "candidate_schema_ids": sorted(candidates)},
                        strict_failure=True,
                    )
            for child in value.values():
                rewrite_refs(child, field_id)
        elif isinstance(value, list):
            for child in value:
                rewrite_refs(child, field_id)

    if isinstance(fields, MutableMapping):
        for field_id, field in fields.items():
            if isinstance(field, MutableMapping):
                rewrite_refs(field.get("wire_schema"), str(field_id))
    report["schema_identity"] = {
        "strategy": "source_qualified_rust_name_with_path_hash_fallback",
        "schemas": copy.deepcopy(resolver.metadata),
    }


def _merge_structured_diagnostics(
    report: MutableMapping[str, Any], diagnostics: DiagnosticCollector
) -> None:
    existing = report.get("diagnostics")
    merged: dict[str, dict[str, Any]] = {}
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                merged[item["id"]] = item
    for item in diagnostics.to_list():
        merged[item["id"]] = item
    severity_order = {"error": 0, "warning": 1, "info": 2}
    values = sorted(
        merged.values(),
        key=lambda item: (
            severity_order.get(str(item.get("severity")), 3),
            str(item.get("code")),
            str(item.get("id")),
        ),
    )
    report["diagnostics"] = values
    summary = {"error": 0, "warning": 0, "info": 0}
    for item in values:
        severity = str(item.get("severity") or "warning")
        summary[severity] = summary.get(severity, 0) + 1
    report["diagnostic_summary"] = summary


def _attach_integrity(report: MutableMapping[str, Any]) -> None:
    payload = canonical_report_payload(report)
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    integrity = report.get("integrity")
    if not isinstance(integrity, MutableMapping):
        integrity = {}
        report["integrity"] = integrity
    integrity.update(
        {
            "algorithm": "sha256",
            "canonicalization": "codex-wire-audit-canonical-json-v2",
            "payload_excludes": ["generated_at", "integrity", "status.validated_at"],
            "payload_sha256": digest,
            "source_set_sha256": (
                (report.get("evolution_contract") or {}).get("source_revision") or {}
            ).get("source_set_sha256"),
            "canonical_ir_sha256": (
                (report.get("evolution_contract") or {}).get("integrity") or {}
            ).get("canonical_ir_sha256"),
        }
    )


def _update_report_status(
    report: MutableMapping[str, Any],
    evolution_contract: Mapping[str, Any],
) -> None:
    status = report.setdefault("status", {})
    if not isinstance(status, MutableMapping):
        return
    dimensions = evolution_contract.get("status") or {}
    status["dimensions"] = copy.deepcopy(dimensions)
    status["coverage_profile"] = evolution_contract.get("coverage_profile")
    has_error = (report.get("diagnostic_summary") or {}).get("error", 0) > 0
    evolution_complete = dimensions.get("overall") == "complete"
    legacy_complete = bool(status.get("complete", True))
    complete = legacy_complete and evolution_complete and not has_error
    status["complete"] = complete
    status["state"] = "complete" if complete else "partial_with_diagnostics"
    status["semantic_classification_complete"] = (
        dimensions.get("semantic_classification") == "complete"
    )
    status["source_revision_id"] = (
        evolution_contract.get("source_revision") or {}
    ).get("source_revision_id")
    status["diagnostic_refs"] = [
        item.get("id")
        for item in report.get("diagnostics", [])
        if isinstance(item, Mapping) and item.get("id")
    ]


def generate_report(
    args: Any,
    legacy: LegacyModules,
    *,
    baseline_report: Mapping[str, Any] | None = None,
) -> GenerationResult:
    registry = build_registry(legacy, getattr(args, "source_registry", None))
    loaded = load_snapshot(args, legacy, registry)
    snapshot = loaded.snapshot
    diagnostics = loaded.diagnostics
    base_src, surface_src, extra_src = snapshot.to_legacy_maps()
    surface_warnings, extra_warnings = _legacy_warning_lists(snapshot, registry)

    deterministic = bool(
        getattr(args, "deterministic", False)
        or getattr(args, "format", None) == "canonical-json"
    )
    report = legacy.base.build_report(
        args.repo,
        snapshot.revision.requested_ref,
        loaded.commit,
        base_src,
        strict=False,
        surface_sources=surface_src,
        surface_warnings=surface_warnings,
        deterministic=deterministic,
    )
    report = legacy.entrypoint.enhance(
        report,
        base_src,
        extra_src,
        extra_warnings,
        False,
    )

    evolution_contract, extractor_results = build_evolution_contract(
        snapshot,
        registry,
        diagnostics,
        coverage_profile=getattr(args, "coverage_profile", "codex_wire_full"),
        legacy_report=report,
    )
    turn_result = extractor_results.get("extractor.turn_metadata")
    if turn_result:
        apply_turn_metadata_overlay(report, turn_result)
    config_result = extractor_results.get("extractor.config_effects")
    if config_result:
        apply_config_surface_overlay(report, config_result)
    local_storage_result = extractor_results.get("extractor.local_storage")
    if local_storage_result:
        apply_local_storage_overlay(report, local_storage_result)

    resolver = SchemaIdentityResolver.from_report(report)
    contract_module = legacy.contract
    original_schema_id = contract_module._schema_id
    contract_module._schema_id = resolver.schema_id
    try:
        report = contract_module.upgrade_report(
            report,
            commit=loaded.commit,
            sources=combined_source_descriptors(snapshot, registry),
            source_mode=snapshot.revision.source_mode,
            repository=args.repo,
            requested_ref=snapshot.revision.requested_ref,
            dirty=snapshot.revision.dirty,
            parser_backend=getattr(args, "parser", "auto"),
        )
    finally:
        contract_module._schema_id = original_schema_id

    _patch_exact_source_manifest(report, snapshot, registry)
    _annotate_qualified_schemas(report, resolver, diagnostics)
    emit_schema_conflict_diagnostics(report, diagnostics)

    semantic_diff: dict[str, Any] | None = None
    if baseline_report is not None:
        try:
            semantic_diff = semantic_compare(baseline_report, {"evolution_contract": evolution_contract})
            report["semantic_diff"] = semantic_diff
        except SemanticDiffError as error:
            diagnostics.emit(
                code="BASELINE_EVOLUTION_CONTRACT_UNAVAILABLE",
                severity="error",
                category="semantic_diff",
                message="The baseline report cannot be used for semantic comparison.",
                extractor_id="semantic_diff",
                details={"error": str(error)},
                recoverable=False,
                strict_failure=True,
            )

    schema_resolution_complete = not any(
        item.severity == "error" and item.category == "schema_resolution"
        for item in diagnostics.values()
    )
    finalize_evolution_contract(
        evolution_contract,
        diagnostics,
        schema_resolution_complete=schema_resolution_complete,
    )
    report["evolution_contract"] = evolution_contract
    report.setdefault("versions", {})["generator"] = GENERATOR_VERSION
    report["versions"]["evolution_contract"] = evolution_contract["schema_version"]
    parser = report.setdefault("parser", {})
    if isinstance(parser, MutableMapping):
        parser["semantic_extractors"] = {
            "extractor.turn_metadata": {
                "backend": "rust_lexical_scanner_plus_expression_classifier",
                "machine_evaluable": turn_result.semantic_complete if turn_result else False,
            },
            "extractor.context_management": {
                "backend": "source_linked_context_management_classifier",
                "machine_evaluable": bool(extractor_results.get("extractor.context_management") and extractor_results["extractor.context_management"].semantic_complete),
            },
            "extractor.config_effects": {
                "backend": "generated_json_schema_plus_feature_registry_surface_graph",
                "machine_evaluable": config_result.semantic_complete if config_result else False,
            },
            "extractor.local_storage": {
                "backend": "source_linked_local_storage_layout_classifier",
                "machine_evaluable": (
                    local_storage_result.semantic_complete if local_storage_result else False
                ),
            },
        }
        parser["legacy_fallback_boundary"] = "all non-migrated v10 sections"

    try:
        finalize_system_report(report)
    except ValueError as error:
        raise SourceLoadError(f"canonical contract validation failed: {error}") from error

    _merge_structured_diagnostics(report, diagnostics)
    _update_report_status(report, evolution_contract)
    _attach_integrity(report)

    validation_errors = contract_module.validate_report(report)
    if validation_errors:
        for item in validation_errors:
            diagnostics.emit(
                Diagnostic(
                    code="REPORT_VALIDATION_FAILED",
                    severity="error",
                    category="report_validation",
                    message=str(item.get("message") or "Generated report failed validation."),
                    extractor_id="report_validator",
                    report_pointer=str(item.get("report_pointer") or item.get("pointer") or "/"),
                    details={"validator_diagnostic": item},
                    recoverable=False,
                    strict_failure=True,
                )
            )
        _merge_structured_diagnostics(report, diagnostics)
        _update_report_status(report, evolution_contract)
        _attach_integrity(report)
        raise SourceLoadError(
            "generated report failed validation: "
            + "; ".join(str(item.get("message")) for item in validation_errors[:5])
        )

    return GenerationResult(
        report=dict(report),
        snapshot=snapshot,
        registry=registry,
        diagnostics=diagnostics,
        semantic_diff=semantic_diff,
    )


def load_baseline(path: str | None) -> Mapping[str, Any] | None:
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceLoadError(f"cannot read baseline report {path}: {error}") from error
    if not isinstance(value, Mapping):
        raise SourceLoadError("baseline report root must be an object")
    return value
