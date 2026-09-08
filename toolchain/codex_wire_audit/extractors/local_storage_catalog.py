"""Canonical local-storage contract renderer used by the source extractor."""

from __future__ import annotations

from typing import Any, Mapping

from ..models import semantic_fingerprint

SCHEMA_ID = (
    "https://schemas.codex-wire-audit.invalid/"
    "local-storage-layout-v1.schema.json"
)


def _artifact(
    root: str,
    relative_pattern: str,
    identity_key: str,
    kind: str,
    mutability: str,
    purpose: str,
    evidence: Mapping[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "root": root,
        "relative_pattern": relative_pattern,
        "identity_key": identity_key,
        "kind": kind,
        "mutability": mutability,
        "purpose": purpose,
        "evidence": dict(evidence),
        **extra,
    }


def build_local_storage_contract(
    *,
    source_revision: Mapping[str, Any],
    constants: Mapping[str, str],
    databases: Mapping[str, str],
    evidence: Mapping[str, Mapping[str, Any]],
    semantic_complete: bool,
    missing_database_constants: list[str],
) -> dict[str, Any]:
    sessions = constants["sessions"]
    archived = constants["archived_sessions"]
    compressed_suffix = constants["compressed_suffix"]
    snapshots = constants["snapshots"]
    locks = constants["locks"]
    coordination_lock = constants["coordination_lock"]
    dated = {"pattern": "YYYY/MM/DD", "source": "rollout creation local timestamp"}
    uuid_dated = {
        "pattern": "YYYY/MM/DD",
        "source": "timestamp embedded in the thread UUID",
    }

    artifacts = {
        "active_rollout": _artifact(
            "codex_home",
            f"{sessions}/{{yyyy}}/{{mm}}/{{dd}}/"
            "rollout-{timestamp}-{thread_id}[_{rollout_id}].jsonl",
            "thread_id with optional distinct rollout_id",
            "append-only JSONL rollout",
            "active files append; replacements are new immutable files",
            "canonical persisted thread history",
            evidence["rollout_recorder"],
            date_partition=dated,
            notes=[
                "The underscore suffix exists only when rollout_id differs from thread_id.",
                "A compressed .jsonl.zst sibling can represent the same logical rollout.",
            ],
        ),
        "archived_rollout_root": _artifact(
            "codex_home",
            f"{archived}/{{rollout_filename}}",
            "thread_id encoded in rollout_filename",
            "archived rollout",
            "moved by the archive lifecycle",
            "discoverable archived thread history",
            evidence["thread_archive"],
        ),
        "shell_snapshot": _artifact(
            "codex_home",
            f"{snapshots}/{{thread_id}}.{{nonce}}.{{sh|ps1}}",
            "thread_id passed as session_id to ShellSnapshot",
            "shell environment script",
            "atomic rename; stale files expire",
            "restore shell state for command execution",
            evidence["shell_snapshot"],
        ),
        "shell_snapshot_temp": _artifact(
            "codex_home",
            f"{snapshots}/{{thread_id}}.tmp-{{nonce}}",
            "thread_id",
            "temporary shell snapshot",
            "renamed atomically or removed on failure",
            "safe snapshot construction",
            evidence["shell_snapshot"],
        ),
        "thread_writer_lock": _artifact(
            "codex_home",
            f"{locks}/{{thread_id}}.lock",
            "thread_id",
            "filesystem advisory lock",
            "created for one active writer and removed on guard drop",
            "enforce one persisted writer per logical thread",
            evidence["writer_lock"],
        ),
        "writer_coordination_lock": _artifact(
            "codex_home",
            f"{locks}/{coordination_lock}",
            "global local thread store",
            "filesystem advisory lock",
            "persistent coordination file",
            "serialize writer-lock cleanup and removal",
            evidence["writer_lock"],
        ),
        "visualization_thread_dir": _artifact(
            "codex_home",
            "visualizations/{yyyy}/{mm}/{dd}/{thread_id}/{file}.html",
            "thread_id",
            "thread-scoped inline visualization artifact",
            "assistant-authored direct-child HTML file",
            "safe inline visualization fragments",
            evidence["inline_visualization"],
            date_partition=uuid_dated,
        ),
        "visualization_viewer_cache": _artifact(
            "codex_home",
            "visualization-viewers/{thread_id}/{artifact_thread_id}/...",
            "thread_id and artifact_thread_id",
            "materialized viewer cache",
            "generated cache",
            "sandboxed visualization viewer documents",
            evidence["inline_visualization"],
        ),
    }
    purposes = {
        "state": "thread metadata and current rollout-path pointer",
        "thread_history": "materialized/queryable thread history state",
        "logs": "runtime log records",
        "goals": "goal state",
        "memories": "memory state",
        "queue": "queued work state",
    }
    for database_id in sorted(purposes.keys() & databases.keys()):
        artifacts[f"sqlite_{database_id}"] = _artifact(
            "sqlite_home",
            databases[database_id],
            "threads.id" if database_id == "state" else "database-specific",
            "SQLite database",
            "transactional",
            purposes[database_id],
            evidence["state_sqlite"],
        )

    identities = {
        "session_id": {
            "type": "SessionId",
            "role": "shared by a root thread and its subagents",
            "filename_component": False,
            "revert_behavior": "preserved verbatim from source_meta.session_id",
            "evidence": evidence["thread_types"],
        },
        "thread_id": {
            "type": "ThreadId",
            "role": "stable logical thread identity",
            "filename_component": True,
            "revert_behavior": "preserved as source_meta.id",
            "adjacent_artifact_key": True,
            "evidence": evidence["rollout_file_name"],
        },
        "rollout_id": {
            "type": "RolloutId",
            "role": "physical rollout revision identity",
            "filename_component": (
                "omitted when equal to thread_id; appended after underscore when distinct"
            ),
            "ordinary_behavior": "equals thread_id",
            "revert_behavior": "new identity for the replacement rollout",
            "evidence": evidence["rollout_file_name"],
        },
        "forked_from_id": {
            "type": "Option<ThreadId>",
            "role": "logical fork lineage",
            "filename_component": False,
            "revert_behavior": "preserved; underscore is not a fork marker",
            "evidence": evidence["thread_types"],
        },
    }
    timestamp = "YYYY-MM-DDTHH-MM-SS"
    filename_grammar = {
        "timestamp": {
            "pattern": timestamp,
            "source_time": "local time when a new rollout path is precomputed",
            "parser_interpretation": "parsed as UTC because the filename has no offset",
        },
        "ordinary_rollout": {
            "pattern": f"rollout-{{{timestamp}}}-{{thread_id}}.jsonl",
            "identity_equation": "rollout_id == thread_id",
        },
        "replacement_rollout": {
            "pattern": f"rollout-{{{timestamp}}}-{{thread_id}}_{{rollout_id}}.jsonl",
            "identity_equation": "rollout_id != thread_id",
            "underscore_semantics": (
                "separator between stable thread identity and distinct rollout revision; "
                "not fork lineage"
            ),
        },
        "compressed_representation": {
            "suffix": compressed_suffix,
            "ordinary_pattern": (
                f"rollout-{{{timestamp}}}-{{thread_id}}.jsonl{compressed_suffix}"
            ),
            "replacement_pattern": (
                f"rollout-{{{timestamp}}}-{{thread_id}}_{{rollout_id}}"
                f".jsonl{compressed_suffix}"
            ),
            "reader_semantics": "plain and compressed forms are one logical rollout",
        },
        "evidence": evidence["rollout_file_name"],
    }
    pointer_model = {
        "database": databases.get("state"),
        "logical_key": "threads.id = thread_id",
        "physical_pointer": "threads.rollout_path",
        "lookup": "thread_id -> current rollout_path",
        "replacement_cutover": {
            "operation": "compare-and-swap",
            "predicate": (
                "threads.id == thread_id AND "
                "threads.rollout_path == expected_rollout_path"
            ),
            "update": "threads.rollout_path = replacement_path",
            "logical_metadata": "remains attached to the stable thread row",
        },
        "evidence": evidence["state_threads"],
    }
    operations = {
        "create": {
            "thread_id": "provided before persistence opens",
            "rollout_id": "defaults to thread_id",
            "path": f"{sessions}/YYYY/MM/DD/ordinary rollout filename",
            "write_model": "append JSONL through the active recorder",
        },
        "resume": {
            "thread_id": "preserved",
            "session_id": "preserved",
            "rollout_id": "decoded from the resolved current rollout path",
            "path_behavior": "reopen the current rollout without a replacement ID",
        },
        "revert_paginated_thread": {
            "thread_id": "preserved",
            "session_id": "preserved verbatim",
            "rollout_id": "new distinct identity",
            "forked_from_id": "preserved",
            "old_rollouts": "retained intact",
            "replacement": "new immutable rollout referencing retained history",
            "write_order": [
                "persist replacement rollout",
                "shutdown replacement recorder",
                "compare-and-swap threads.rollout_path",
            ],
            "compare_and_swap_failure": "delete replacement and return conflict",
            "evidence": evidence["thread_revert"],
        },
    }
    invariant_statements = {
        "identity.session_thread_rollout_distinct": (
            "session_id, thread_id, and rollout_id are distinct identity domains"
        ),
        "filename.ordinary_ids_equal": (
            "ordinary rollout filenames encode thread_id == rollout_id"
        ),
        "filename.underscore_is_revision": (
            "thread_id_rollout_id denotes a replacement revision, not fork lineage"
        ),
        "revert.logical_identity_stable": (
            "revert preserves thread_id, session_id, and forked_from_id"
        ),
        "revert.old_rollout_retained": "old rollouts remain intact",
        "revert.pointer_cutover_atomic": (
            "current rollout_path changes through compare-and-swap"
        ),
        "adjacent_artifacts.thread_keyed": (
            "snapshots, writer locks, and visualizations remain thread-keyed"
        ),
        "roots.sqlite_separate": (
            "sqlite_home is configurable and must not be assumed equal to codex_home"
        ),
    }
    invariants = [
        {"id": f"local_storage.{key}", "statement": value}
        for key, value in invariant_statements.items()
    ]
    data: dict[str, Any] = {
        "$schema": SCHEMA_ID,
        "source_revision": dict(source_revision),
        "scope": {
            "name": "Codex local thread/session persistence layout",
            "codex_home_default_display": "~/.codex",
            "roots": {
                "codex_home": "rollouts and adjacent thread artifacts",
                "sqlite_home": "runtime SQLite databases; independently configurable",
            },
            "included": [
                "active and archived rollout layout",
                "rollout filename and compression grammar",
                "SQLite logical-to-physical pointer model",
                "paginated revert replacement semantics",
                "thread-keyed snapshots, writer locks, and visualizations",
                "runtime SQLite filenames",
            ],
            "excluded": [
                "credentials and configuration",
                "unrelated caches and telemetry",
                "complete SQLite table schemas",
            ],
        },
        "identities": identities,
        "filename_grammar": filename_grammar,
        "artifacts": artifacts,
        "pointer_model": pointer_model,
        "operations": operations,
        "invariants": invariants,
        "evidence": {key: dict(value) for key, value in evidence.items()},
        "coverage": {
            "source_count": len(evidence),
            "artifact_count": len(artifacts),
            "sqlite_database_count": len(purposes.keys() & databases.keys()),
            "identity_count": len(identities),
            "invariant_count": len(invariants),
            "missing_sources": [],
            "missing_database_constants": missing_database_constants,
            "scope_complete": semantic_complete,
        },
        "semantic_complete": semantic_complete,
    }
    data["semantic_digest"] = semantic_fingerprint(
        {key: value for key, value in data.items() if key != "semantic_digest"}
    )
    return data
