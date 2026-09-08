"""Declarative source registry shared by all source providers and extractors."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from . import SOURCE_REGISTRY_VERSION
from .models import SourceGroup, SourceSpec


class SourceRegistryError(ValueError):
    pass


class SourceRegistry:
    def __init__(self, specs: Iterable[SourceSpec]) -> None:
        self._specs: dict[str, SourceSpec] = {}
        legacy_slots: set[tuple[SourceGroup, str]] = set()
        for spec in specs:
            if spec.id in self._specs:
                raise SourceRegistryError(f"duplicate source spec id: {spec.id}")
            slot = (spec.group, spec.legacy_key)
            if slot in legacy_slots:
                raise SourceRegistryError(
                    f"duplicate legacy source slot: {spec.group.value}:{spec.legacy_key}"
                )
            if not spec.path_candidates:
                raise SourceRegistryError(f"source spec has no path candidates: {spec.id}")
            for path in spec.path_candidates:
                if path.startswith("/") or ".." in Path(path).parts:
                    raise SourceRegistryError(f"unsafe source path candidate: {path}")
            self._specs[spec.id] = spec
            legacy_slots.add(slot)

    @property
    def specs(self) -> tuple[SourceSpec, ...]:
        return tuple(sorted(self._specs.values(), key=lambda item: item.id))

    def get(self, spec_id: str) -> SourceSpec:
        try:
            return self._specs[spec_id]
        except KeyError as error:
            raise SourceRegistryError(f"unknown source spec: {spec_id}") from error

    def for_group(self, group: SourceGroup) -> tuple[SourceSpec, ...]:
        return tuple(spec for spec in self.specs if spec.group == group)

    def with_overlay(self, overlay: Mapping[str, Any]) -> "SourceRegistry":
        if set(overlay) - {"schema_version", "sources"}:
            raise SourceRegistryError(
                "source registry overlay contains unsupported top-level keys: "
                + ", ".join(sorted(set(overlay) - {"schema_version", "sources"}))
            )
        rows = overlay.get("sources")
        if not isinstance(rows, Mapping):
            raise SourceRegistryError("source registry overlay must contain an object named 'sources'")
        updated = dict(self._specs)
        for spec_id, patch in rows.items():
            if not isinstance(patch, Mapping):
                raise SourceRegistryError(f"source overlay must be an object: {spec_id}")
            creating = spec_id not in updated
            allowed = {
                "legacy_key",
                "group",
                "path_candidates",
                "required",
                "roles",
                "expected_symbols",
                "extractor_ids",
                "exclusion_reason",
            }
            unknown = set(patch) - allowed
            if unknown:
                raise SourceRegistryError(
                    f"unsupported overlay fields for {spec_id}: {', '.join(sorted(unknown))}"
                )
            changes: dict[str, Any] = {}
            for key in allowed:
                if key not in patch:
                    continue
                value = patch[key]
                if key in {"path_candidates", "roles", "expected_symbols", "extractor_ids"}:
                    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                        raise SourceRegistryError(f"{spec_id}.{key} must be an array of strings")
                    changes[key] = tuple(value)
                elif key == "required":
                    if not isinstance(value, bool):
                        raise SourceRegistryError(f"{spec_id}.required must be boolean")
                    changes[key] = value
                elif key == "group":
                    try:
                        changes[key] = SourceGroup(str(value))
                    except (TypeError, ValueError) as error:
                        raise SourceRegistryError(f"{spec_id}.group must be base, surface, or extra") from error
                else:
                    if value is not None and not isinstance(value, str):
                        raise SourceRegistryError(f"{spec_id}.{key} must be string or null")
                    changes[key] = value
            if creating:
                required_fields = {"legacy_key", "group", "path_candidates", "required"}
                missing = sorted(required_fields - set(changes))
                if missing:
                    raise SourceRegistryError(
                        f"new source spec {spec_id} is missing required fields: {', '.join(missing)}"
                    )
                updated[spec_id] = SourceSpec(
                    id=spec_id,
                    legacy_key=changes["legacy_key"],
                    group=changes["group"],
                    path_candidates=changes["path_candidates"],
                    required=changes["required"],
                    roles=changes.get("roles", ()),
                    expected_symbols=changes.get("expected_symbols", ()),
                    extractor_ids=changes.get("extractor_ids", ()),
                    exclusion_reason=changes.get("exclusion_reason"),
                )
            else:
                if "legacy_key" in changes or "group" in changes:
                    raise SourceRegistryError(
                        f"existing source spec identity cannot be changed by overlay: {spec_id}"
                    )
                updated[spec_id] = replace(updated[spec_id], **changes)
        return SourceRegistry(updated.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_REGISTRY_VERSION,
            "sources": {spec.id: spec.to_dict() for spec in self.specs},
            "counts": {
                "total": len(self._specs),
                "required": sum(spec.required for spec in self._specs.values()),
                "optional": sum(not spec.required for spec in self._specs.values()),
                "base": len(self.for_group(SourceGroup.BASE)),
                "surface": len(self.for_group(SourceGroup.SURFACE)),
                "extra": len(self.for_group(SourceGroup.EXTRA)),
            },
        }

    @classmethod
    def load_overlay(cls, path: str | Path) -> Mapping[str, Any]:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SourceRegistryError(f"cannot read source registry overlay {path}: {error}") from error
        if not isinstance(value, Mapping):
            raise SourceRegistryError("source registry overlay root must be an object")
        return value


def _spec_id(group: SourceGroup, key: str) -> str:
    safe = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return f"source_spec.{group.value}.{safe}"


def from_legacy_maps(
    base_files: Mapping[str, str],
    surface_files: Mapping[str, str],
    extra_files: Mapping[str, str],
) -> SourceRegistry:
    specs: list[SourceSpec] = []
    for group, mapping, required in (
        (SourceGroup.BASE, base_files, True),
        (SourceGroup.SURFACE, surface_files, False),
        (SourceGroup.EXTRA, extra_files, False),
    ):
        for key, path in mapping.items():
            roles = [f"legacy_{group.value}"]
            expected_symbols: tuple[str, ...] = ()
            extractors: tuple[str, ...] = ()
            candidates = (path,)
            if group == SourceGroup.BASE and key == "metadata":
                roles.extend(("responses_metadata", "turn_metadata_semantics"))
                expected_symbols = (
                    "CodexResponsesMetadata",
                    "CodexTurnMetadataPayload",
                    "turn_metadata_payload",
                )
                extractors = ("extractor.turn_metadata",)
                candidates = (
                    path,
                    "codex-rs/core/src/responses/metadata.rs",
                )
            elif group == SourceGroup.SURFACE and key == "endpoint_mod":
                roles.append("endpoint_discovery")
                expected_symbols = ("pub mod",)
            elif group == SourceGroup.EXTRA and key in {
                "feature_configs", "feature_registry", "config_toml", "model_protocol", "provider_runtime"
            }:
                roles.append("context_management_support")
                extractors = tuple(sorted(set((*extractors, "extractor.context_management"))))
            elif group == SourceGroup.BASE and key == "provider_info":
                roles.append("context_management_routing")
                extractors = tuple(sorted(set((*extractors, "extractor.context_management"))))
            if (group, key) in {
                (SourceGroup.BASE, "provider_info"),
                (SourceGroup.EXTRA, "config_toml"),
                (SourceGroup.EXTRA, "core_config"),
                (SourceGroup.EXTRA, "feature_configs"),
                (SourceGroup.EXTRA, "feature_registry"),
                (SourceGroup.EXTRA, "provider_runtime"),
                (SourceGroup.EXTRA, "turn_metadata"),
            }:
                roles.append("config_surface_support")
                extractors = tuple(sorted(set((*extractors, "extractor.config_effects"))))
            specs.append(
                SourceSpec(
                    id=_spec_id(group, key),
                    legacy_key=key,
                    group=group,
                    path_candidates=candidates,
                    required=required,
                    roles=tuple(roles),
                    expected_symbols=expected_symbols,
                    extractor_ids=extractors,
                )
            )
    context_specs = (
        ("source_spec.extra.context_management_activation", "context_management_activation", "codex-rs/core/src/session/token_budget.rs", ("apply_experimental_context", "supports_experimental_context")),
        ("source_spec.extra.history_notes_tools", "history_notes_tools", "codex-rs/ext/history-notes/src/tools.rs", ("HistoryNotesAction", "alpha/history/v2/list_windows", "alpha/notes/v2/write_file")),
        ("source_spec.extra.history_notes_backend", "history_notes_backend", "codex-rs/ext/history-notes/src/backend.rs", ("HistoryNotesBackend", "x-openai-tool-output-truncation-policy")),
        ("source_spec.extra.history_notes_extension", "history_notes_extension", "codex-rs/ext/history-notes/src/extension.rs", ("alpha/notes/v2/thread_hint", "use_history_notes_extension")),
        ("source_spec.extra.new_context_window", "new_context_window", "codex-rs/core/src/tools/handlers/new_context_window.rs", ("NEW_CONTEXT_WINDOW_MESSAGE", "request_new_context_window")),
        ("source_spec.extra.new_context_window_spec", "new_context_window_spec", "codex-rs/core/src/tools/handlers/new_context_window_spec.rs", ("NEW_CONTEXT_WINDOW_TOOL_NAME", "Start a new context window")),
        ("source_spec.extra.compact_token_budget", "compact_token_budget", "codex-rs/core/src/compact_token_budget.rs", ("skips model/server summarization", "start_new_context_window")),
        ("source_spec.extra.context_management_tests", "context_management_tests", "codex-rs/core/tests/suite/token_budget.rs", ("experimental_context_requires", "supports_experimental_context")),
        ("source_spec.extra.history_notes_tests", "history_notes_tests", "codex-rs/ext/history-notes/tests/history_notes_extension.rs", ("Recent notes (up to 5, most-recent first)", "thread_hint")),
    )
    local_storage_specs = (
        (
            "source_spec.extra.thread_store_types",
            "thread_store_types",
            "codex-rs/thread-store/src/types.rs",
            ("CreateThreadParams", "session_id", "thread_id", "forked_from_id"),
        ),
        (
            "source_spec.extra.rollout_file_name",
            "rollout_file_name",
            "codex-rs/rollout/src/rollout_file_name.rs",
            ("RolloutFileName", "split_once('_')", "rollout-{timestamp}-{}_{}.jsonl"),
        ),
        (
            "source_spec.extra.rollout_recorder",
            "rollout_recorder",
            "codex-rs/rollout/src/recorder.rs",
            ("precompute_new_rollout_path", "with_rollout_id", "SESSIONS_SUBDIR"),
        ),
        (
            "source_spec.extra.rollout_layout_constants",
            "rollout_layout_constants",
            "codex-rs/rollout/src/lib.rs",
            ("SESSIONS_SUBDIR", "ARCHIVED_SESSIONS_SUBDIR"),
        ),
        (
            "source_spec.extra.rollout_compression",
            "rollout_compression",
            "codex-rs/rollout/src/compression.rs",
            ("COMPRESSED_SUFFIX", "open_rollout_line_reader", "RolloutFile"),
        ),
        (
            "source_spec.extra.thread_revert",
            "thread_revert",
            "codex-rs/thread-store/src/local/revert_thread.rs",
            ("revert", "create_replacement_recorder", "replace_rollout_path_if_current"),
        ),
        (
            "source_spec.extra.thread_archive",
            "thread_archive",
            "codex-rs/thread-store/src/local/archive_thread.rs",
            ("archive_thread_with_paths", "ARCHIVED_SESSIONS_SUBDIR", "mark_archived"),
        ),
        (
            "source_spec.extra.thread_writer_lock",
            "thread_writer_lock",
            "codex-rs/thread-store/src/local/writer_lock.rs",
            ("WriterLockCoordinator", "WRITER_LOCK_DIR", "COORDINATION_LOCK_FILE"),
        ),
        (
            "source_spec.extra.state_sqlite",
            "state_sqlite",
            "codex-rs/state/src/sqlite.rs",
            ("STATE_DB_FILENAME", "THREAD_HISTORY_DB_FILENAME", "SqliteConfig"),
        ),
        (
            "source_spec.extra.state_threads",
            "state_threads",
            "codex-rs/state/src/runtime/threads.rs",
            ("find_rollout_path_by_id", "replace_rollout_path_if_current", "UPDATE threads"),
        ),
        (
            "source_spec.extra.shell_snapshot",
            "shell_snapshot",
            "codex-rs/core/src/shell_snapshot.rs",
            ("ShellSnapshot", "SNAPSHOT_DIR", "SNAPSHOT_RETENTION"),
        ),
        (
            "source_spec.extra.inline_visualization",
            "inline_visualization",
            "codex-rs/tui/src/inline_visualization.rs",
            ("InlineVisualizationContext", "visualizations", "visualization-viewers"),
        ),
    )
    specs.append(SourceSpec(
        id="source_spec.extra.generated_config_schema",
        legacy_key="generated_config_schema",
        group=SourceGroup.EXTRA,
        path_candidates=("codex-rs/core/config.schema.json",),
        required=False,
        roles=("generated_config_schema", "config_surface"),
        expected_symbols=("\"title\": \"ConfigToml\"", "\"features\"", "\"model_providers\""),
        extractor_ids=("extractor.config_effects",),
    ))
    specs.append(SourceSpec(
        id="source_spec.extra.config_schema_generator",
        legacy_key="config_schema_generator",
        group=SourceGroup.EXTRA,
        path_candidates=("codex-rs/config/src/schema.rs",),
        required=False,
        roles=("config_schema_generation_policy", "config_surface"),
        expected_symbols=("features_schema", "Feature::Artifact", "Feature::GuardianThreadContext"),
        extractor_ids=("extractor.config_effects",),
    ))
    existing_ids = {spec.id for spec in specs}
    for spec_id, legacy_key, path, symbols in context_specs:
        if spec_id in existing_ids:
            continue
        specs.append(SourceSpec(
            id=spec_id,
            legacy_key=legacy_key,
            group=SourceGroup.EXTRA,
            path_candidates=(path,),
            required=False,
            roles=("context_management",),
            expected_symbols=tuple(symbols),
            extractor_ids=("extractor.context_management",),
        ))
    existing_ids = {spec.id for spec in specs}
    for spec_id, legacy_key, path, symbols in local_storage_specs:
        if spec_id in existing_ids:
            continue
        specs.append(SourceSpec(
            id=spec_id,
            legacy_key=legacy_key,
            group=SourceGroup.EXTRA,
            path_candidates=(path,),
            required=False,
            roles=("local_storage_layout", "thread_persistence"),
            expected_symbols=tuple(symbols),
            extractor_ids=("extractor.local_storage",),
        ))
    return SourceRegistry(specs)
