"""Extract and cross-check Rust feature definitions and config-schema policy."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .canonical import canonical_json_bytes
from .rust_syntax import (
    RustSyntaxError,
    find_function_body,
    find_matching_delimiter,
    line_number,
    normalize_expression,
    split_top_level,
)


_FEATURES_DECL = re.compile(
    r"\b(?:pub\s+)?const\s+FEATURES\s*:\s*&\s*\[\s*FeatureSpec\s*\]\s*=\s*&\s*\["
)
_FEATURE_START = re.compile(r"\bFeatureSpec\s*\{")
_SCHEMA_BRANCH = re.compile(
    r"\bif\s+feature\.id\s*==\s*codex_features::Feature::([A-Za-z0-9_]+)\s*\{"
)


def _stage(block: str) -> str | None:
    match = re.search(r"\bstage\s*:\s*Stage::([A-Za-z0-9_]+)", block)
    return match.group(1) if match else None


def _feature_array_span(text: str) -> tuple[int, int]:
    match = _FEATURES_DECL.search(text)
    if match is None:
        raise RustSyntaxError("FEATURES FeatureSpec array not found")
    open_index = text.rfind("[", match.start(), match.end())
    if open_index < 0:
        raise RustSyntaxError("FEATURES array opening bracket not found")
    close_index = find_matching_delimiter(text, open_index)
    return open_index + 1, close_index


def extract_feature_registry(text: str, *, source_path: str) -> dict[str, Any]:
    features: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        array_start, array_end = _feature_array_span(text)
    except RustSyntaxError as error:
        return {
            "source_path": source_path,
            "features": [],
            "by_id": {},
            "by_key": {},
            "duplicate_ids": [],
            "duplicate_keys": [],
            "parse_errors": [{
                "code": "FEATURE_ARRAY_UNAVAILABLE",
                "line": 1,
                "message": str(error),
            }],
            "counts": {"total": 0, "by_stage": {}},
        }

    array_text = text[array_start:array_end]
    for entry in split_top_level(array_text):
        match = _FEATURE_START.search(entry)
        if match is None:
            # Comments are retained with the following entry by split_top_level.
            # A non-comment expression in the registry must stay fail-visible.
            stripped = re.sub(r"(?m)^\s*//.*$", "", entry).strip()
            if stripped:
                errors.append({
                    "code": "FEATURE_ARRAY_ENTRY_UNCLASSIFIED",
                    "line": line_number(text, array_start + array_text.find(entry)),
                    "message": "FEATURES contains a non-FeatureSpec entry",
                })
            continue
        absolute = array_start + array_text.find(entry) + match.start()
        open_index = text.find("{", absolute, absolute + len(match.group(0)) + 1)
        try:
            close_index = find_matching_delimiter(text, open_index)
        except RustSyntaxError as error:
            errors.append({
                "code": "FEATURE_BLOCK_UNCLOSED",
                "line": line_number(text, absolute),
                "message": str(error),
            })
            continue
        block = text[open_index + 1 : close_index]
        fields: dict[str, str] = {}
        for field_entry in split_top_level(block):
            if ":" not in field_entry:
                continue
            name, expression = field_entry.split(":", 1)
            fields[name.strip()] = normalize_expression(expression)
        id_match = re.fullmatch(r"Feature::([A-Za-z0-9_]+)", fields.get("id", ""))
        key_match = re.fullmatch(r'"([^"]+)"', fields.get("key", ""))
        default_expression = fields.get("default_enabled")
        if not id_match or not key_match or default_expression is None:
            errors.append({
                "code": "FEATURE_BLOCK_UNCLASSIFIED",
                "line": line_number(text, absolute),
                "message": "FeatureSpec is missing id, key, or default_enabled",
            })
            continue
        default_enabled = default_expression == "true" if default_expression in {"true", "false"} else None
        row = {
            "id": id_match.group(1),
            "key": key_match.group(1),
            "stage": _stage(block),
            "default_enabled": default_enabled,
            "default_enabled_expression": default_expression,
            "source": {"path": source_path, "line": line_number(text, absolute)},
        }
        row["semantic_digest"] = hashlib.sha256(canonical_json_bytes(row)).hexdigest()
        features.append(row)

    features.sort(key=lambda item: item["key"])
    id_counts = {item["id"]: sum(row["id"] == item["id"] for row in features) for item in features}
    key_counts = {item["key"]: sum(row["key"] == item["key"] for row in features) for item in features}
    duplicate_ids = sorted(key for key, count in id_counts.items() if count > 1)
    duplicate_keys = sorted(key for key, count in key_counts.items() if count > 1)
    return {
        "source_path": source_path,
        "features": features,
        "by_id": {item["id"]: item for item in features},
        "by_key": {item["key"]: item for item in features},
        "duplicate_ids": duplicate_ids,
        "duplicate_keys": duplicate_keys,
        "parse_errors": errors,
        "counts": {
            "total": len(features),
            "by_stage": {
                stage: sum(item.get("stage") == stage for item in features)
                for stage in sorted({str(item.get("stage")) for item in features})
            },
        },
    }


def _schema_branch_action(branch: str) -> tuple[str, str | None]:
    schema_match = re.search(
        r"FeatureToml\s*<\s*codex_features::([A-Za-z0-9_]+)", branch, re.S
    )
    if schema_match:
        return "structured_schema", schema_match.group(1)
    if "removed_apps_mcp_path_override_schema" in branch:
        return "custom_schema", "removed_apps_mcp_path_override_schema"
    without_comments = re.sub(r"(?m)^\s*//.*$", "", branch).strip()
    if without_comments == "continue;":
        return "skip_direct_property", None
    return "unclassified", None


def extract_feature_schema_policy(text: str, *, source_path: str) -> dict[str, Any]:
    """Extract explicit feature-to-generated-schema branches from features_schema()."""
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        function = find_function_body(text, "features_schema")
    except RustSyntaxError as error:
        return {
            "source_path": source_path,
            "features": [],
            "by_id": {},
            "fallback": None,
            "parse_errors": [{
                "code": "FEATURE_SCHEMA_FUNCTION_UNAVAILABLE",
                "line": 1,
                "message": str(error),
            }],
        }

    body = function.text(text)
    for match in _SCHEMA_BRANCH.finditer(body):
        feature_id = match.group(1)
        absolute = function.start + match.start()
        open_index = text.find("{", absolute, function.end)
        try:
            close_index = find_matching_delimiter(text, open_index)
        except RustSyntaxError as error:
            errors.append({
                "code": "FEATURE_SCHEMA_BRANCH_UNCLOSED",
                "line": line_number(text, absolute),
                "message": str(error),
            })
            continue
        branch = text[open_index + 1 : close_index]
        action, schema_type = _schema_branch_action(branch)
        comments = [value.strip() for value in re.findall(r"(?m)^\s*//\s?(.*)$", branch) if value.strip()]
        row: dict[str, Any] = {
            "feature_id": feature_id,
            "action": action,
            "schema_type": schema_type,
            "reason": " ".join(comments) or None,
            "source": {"path": source_path, "line": line_number(text, absolute)},
        }
        row["semantic_digest"] = hashlib.sha256(canonical_json_bytes(row)).hexdigest()
        rows.append(row)
        if action == "unclassified":
            errors.append({
                "code": "FEATURE_SCHEMA_BRANCH_UNCLASSIFIED",
                "line": line_number(text, absolute),
                "message": f"Unable to classify generated-schema branch for Feature::{feature_id}",
            })

    rows.sort(key=lambda item: item["feature_id"])
    duplicate_ids = sorted({
        row["feature_id"] for row in rows
        if sum(other["feature_id"] == row["feature_id"] for other in rows) > 1
    })
    for feature_id in duplicate_ids:
        errors.append({
            "code": "FEATURE_SCHEMA_BRANCH_DUPLICATE",
            "line": 1,
            "message": f"Feature::{feature_id} has more than one generated-schema branch",
        })
    fallback = {
        "action": "boolean_schema" if "schema_gen.subschema_for::<bool>()" in body else "unresolved",
        "source": {"path": source_path, "line": line_number(text, function.start)},
    }
    fallback["semantic_digest"] = hashlib.sha256(canonical_json_bytes(fallback)).hexdigest()
    if fallback["action"] == "unresolved":
        errors.append({
            "code": "FEATURE_SCHEMA_FALLBACK_UNRESOLVED",
            "line": line_number(text, function.start),
            "message": "features_schema() boolean fallback was not found",
        })
    return {
        "source_path": source_path,
        "features": rows,
        "by_id": {row["feature_id"]: row for row in rows},
        "fallback": fallback,
        "parse_errors": errors,
        "counts": {
            "explicit_branches": len(rows),
            "skip_direct_property": sum(row["action"] == "skip_direct_property" for row in rows),
            "structured_schema": sum(row["action"] == "structured_schema" for row in rows),
            "custom_schema": sum(row["action"] == "custom_schema" for row in rows),
        },
    }


def crosswalk_features(
    registry: dict[str, Any],
    config_paths: dict[str, Any],
    schema_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    available_paths = set(config_paths)
    root_schema_keys = {
        path.removeprefix("features.")
        for path in available_paths
        if path.startswith("features.") and "." not in path.removeprefix("features.")
    }
    profile_schema_keys = {
        path.removeprefix("profiles.*.features.")
        for path in available_paths
        if path.startswith("profiles.*.features.")
        and "." not in path.removeprefix("profiles.*.features.")
    }
    policy_by_id = (schema_policy or {}).get("by_id") or {}
    rows: list[dict[str, Any]] = []
    registered_keys: set[str] = set()
    missing_root: list[str] = []
    missing_profile: list[str] = []
    intentionally_omitted: list[str] = []
    embedded_keys: list[str] = []
    for feature in registry.get("features", []):
        key = str(feature["key"])
        feature_id = str(feature["id"])
        registered_keys.add(key)
        root_path = f"features.{key}"
        profile_path = f"profiles.*.features.{key}"
        root_present = root_path in available_paths
        profile_present = profile_path in available_paths
        policy = policy_by_id.get(feature_id) or {}
        policy_action = str(policy.get("action") or "boolean_schema_fallback")
        skip_direct = policy_action == "skip_direct_property"
        if skip_direct and root_present:
            representation = "embedded_in_structured_feature"
            embedded_keys.append(key)
        elif skip_direct:
            representation = "intentionally_not_user_configurable"
            intentionally_omitted.append(key)
        elif root_present:
            representation = "direct_feature_property"
        else:
            representation = "missing"
            missing_root.append(key)
        if not profile_present and not skip_direct:
            missing_profile.append(key)
        rows.append({
            "feature_id": feature_id,
            "config_key": key,
            "stage": feature.get("stage"),
            "default_enabled": feature.get("default_enabled"),
            "root_path": root_path,
            "root_schema_present": root_present,
            "profile_path": profile_path,
            "profile_schema_present": profile_present,
            "schema_node_ref": f"config.{root_path}",
            "feature_node_ref": f"feature.{key}",
            "schema_policy_action": policy_action,
            "schema_representation": representation,
            "schema_policy_source": policy.get("source"),
            "schema_policy_reason": policy.get("reason"),
            "source": feature.get("source"),
        })
    return {
        "features": rows,
        "registered_without_root_schema": sorted(missing_root),
        "registered_without_profile_schema": sorted(missing_profile),
        "intentionally_omitted_keys": sorted(intentionally_omitted),
        "embedded_feature_keys": sorted(embedded_keys),
        "schema_only_root_keys": sorted(root_schema_keys - registered_keys),
        "schema_only_profile_keys": sorted(profile_schema_keys - registered_keys),
        "counts": {
            "registered": len(registered_keys),
            "root_schema": len(root_schema_keys),
            "profile_schema": len(profile_schema_keys),
            "fully_crosswalked": sum(row["root_schema_present"] and row["profile_schema_present"] for row in rows),
            "fully_accounted": sum(
                (row["root_schema_present"] and row["profile_schema_present"])
                or row["schema_representation"] == "intentionally_not_user_configurable"
                for row in rows
            ),
            "intentionally_omitted": len(intentionally_omitted),
            "embedded": len(embedded_keys),
        },
    }
