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
            if group == SourceGroup.EXTRA and key == "config_toml":
                roles.append("local_storage_configuration")
                extractors = tuple(sorted(set((*extractors, "extractor.local_storage"))))
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
        ("source_spec.extra.local_storage_home_dir", "local_storage_home_dir", "codex-rs/utils/home-dir/src/lib.rs", ("find_codex_home", "CODEX_HOME", ".codex")),
        ("source_spec.extra.local_storage_thread_types", "local_storage_thread_types", "codex-rs/thread-store/src/types.rs", ("CreateThreadParams", "session_id", "thread_id", "RevertThreadParams")),
        ("source_spec.extra.local_storage_rollout_lib", "local_storage_rollout_lib", "codex-rs/rollout/src/lib.rs", ("SESSIONS_SUBDIR", "ARCHIVED_SESSIONS_SUBDIR")),
        ("source_spec.extra.local_storage_rollout_filename", "local_storage_rollout_filename", "codex-rs/rollout/src/rollout_file_name.rs", ("RolloutFileName", "rollout_id", "split_once('_')")),
        ("source_spec.extra.local_storage_rollout_recorder", "local_storage_rollout_recorder", "codex-rs/rollout/src/recorder.rs", ("RolloutRecorderParams", "with_session_id", "with_rollout_id")),
        ("source_spec.extra.local_storage_rollout_compression", "local_storage_rollout_compression", "codex-rs/rollout/src/compression.rs", ("COMPRESSED_SUFFIX", "open_rollout_line_reader", "plain_rollout_path")),
        ("source_spec.extra.local_storage_session_index", "local_storage_session_index", "codex-rs/rollout/src/session_index.rs", ("SESSION_INDEX_FILE", "SessionIndexEntry", "append_thread_name")),
        ("source_spec.extra.local_storage_revert_thread", "local_storage_revert_thread", "codex-rs/thread-store/src/local/revert_thread.rs", ("replace_rollout_path_if_current", "with_rollout_id", "history_base")),
        ("source_spec.extra.local_storage_paginated_fork", "local_storage_paginated_fork", "codex-rs/thread-store/src/local/paginated_fork.rs", ("HistoryPosition", "end_ordinal_exclusive", "end_byte_offset")),
        ("source_spec.extra.local_storage_archive_thread", "local_storage_archive_thread", "codex-rs/thread-store/src/local/archive_thread.rs", ("ARCHIVED_SESSIONS_SUBDIR", "mark_archived", "rename")),
        ("source_spec.extra.local_storage_unarchive_thread", "local_storage_unarchive_thread", "codex-rs/thread-store/src/local/unarchive_thread.rs", ("rollout_date_parts", "mark_unarchived", "SESSIONS_SUBDIR")),
        ("source_spec.extra.local_storage_delete_thread", "local_storage_delete_thread", "codex-rs/thread-store/src/local/delete_thread.rs", ("RolloutReferenceIndex", "remove_thread_name_entries", "delete_rollout_file")),
        ("source_spec.extra.local_storage_writer_lock", "local_storage_writer_lock", "codex-rs/thread-store/src/local/writer_lock.rs", ("WRITER_LOCK_DIR", "COORDINATION_LOCK_FILE", "try_lock")),
        ("source_spec.extra.local_storage_state_sqlite", "local_storage_state_sqlite", "codex-rs/state/src/sqlite.rs", ("RUNTIME_DBS", "STATE_DB_FILENAME", "THREAD_HISTORY_DB_FILENAME")),
        ("source_spec.extra.local_storage_state_threads", "local_storage_state_threads", "codex-rs/state/src/runtime/threads.rs", ("replace_rollout_path_if_current", "UPDATE threads SET rollout_path")),
        ("source_spec.extra.local_storage_threads_migration", "local_storage_threads_migration", "codex-rs/state/migrations/0001_threads.sql", ("CREATE TABLE threads", "id TEXT PRIMARY KEY", "rollout_path TEXT NOT NULL")),
        ("source_spec.extra.local_storage_history_materialization", "local_storage_history_materialization", "codex-rs/thread-store/src/local/thread_history_materialization.rs", ("materialize_to_sqlite", "next_byte_offset", "next_ordinal")),
        ("source_spec.extra.local_storage_shell_snapshot", "local_storage_shell_snapshot", "codex-rs/core/src/shell_snapshot.rs", ("SNAPSHOT_DIR", "SNAPSHOT_RETENTION", "session_id")),
        ("source_spec.extra.local_storage_visualization", "local_storage_visualization", "codex-rs/tui/src/inline_visualization.rs", ("visualizations", "visualization-viewers", "thread_id")),
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
            roles=("local_storage",),
            expected_symbols=tuple(symbols),
            extractor_ids=("extractor.local_storage",),
        ))
    return SourceRegistry(specs)
