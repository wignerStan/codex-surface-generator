"""Compose config, feature, and protocol facts into one navigable surface graph."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping

from .canonical import canonical_json_bytes


def _node(node_id: str, kind: str, label: str, **attributes: Any) -> dict[str, Any]:
    value = {"id": node_id, "kind": kind, "label": label}
    value.update({key: item for key, item in attributes.items() if item is not None})
    return value


def _edge(edge_id: str, source: str, target: str, relation: str, **attributes: Any) -> dict[str, Any]:
    value = {"id": edge_id, "source": source, "target": target, "relation": relation}
    value.update({key: item for key, item in attributes.items() if item is not None})
    return value


def _canonical_surface_nodes(extractors: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    context = extractors.get("extractor.context_management")
    context_data = context.data if context is not None else {}
    if context_data:
        nodes["surface.context_management.activation"] = _node(
            "surface.context_management.activation", "activation", "Experimental context activation",
            extractor_id="extractor.context_management",
        )
        nodes["surface.context_management.rollover"] = _node(
            "surface.context_management.rollover", "context_rollover", "No-summary fresh context window",
            extractor_id="extractor.context_management",
        )
        nodes["surface.context_management.history_notes"] = _node(
            "surface.context_management.history_notes", "remote_state", "History and Notes durable state",
            extractor_id="extractor.context_management",
        )
        routing = context_data.get("routing") or {}
        if routing:
            nodes["surface.routing.model_provider"] = _node(
                "surface.routing.model_provider", "endpoint_plane", "Model-provider transport plane",
                extractor_id="extractor.context_management",
            )
            nodes["surface.routing.chatgpt_auxiliary"] = _node(
                "surface.routing.chatgpt_auxiliary", "endpoint_plane", "ChatGPT auxiliary backend plane",
                extractor_id="extractor.context_management",
            )
        history = (context_data.get("history_notes") or {}).get("routes") or []
        for route in history:
            route_id = f"surface.route.{route}"
            nodes[route_id] = _node(route_id, "http_route", route, method="POST", extractor_id="extractor.context_management")
    turn = extractors.get("extractor.turn_metadata")
    turn_data = turn.data if turn is not None else {}
    if turn_data:
        nodes["surface.turn_metadata.extra"] = _node(
            "surface.turn_metadata.extra", "metadata", "Flattened extra turn metadata",
            extractor_id="extractor.turn_metadata",
        )
        fields = turn_data.get("fields") or {}
        if "tool_namespaces_info" in fields:
            nodes["surface.turn_metadata.tool_namespaces_info"] = _node(
                "surface.turn_metadata.tool_namespaces_info", "metadata_field", "tool_namespaces_info",
                extractor_id="extractor.turn_metadata",
            )
    return nodes


def _declared_target_nodes(effect_links: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for link in effect_links:
        target = str(link["target"])
        if target in nodes:
            continue
        prefix = target.split(".", 2)[1] if target.startswith("surface.") and "." in target else "surface"
        nodes[target] = _node(target, prefix, target.removeprefix("surface.").replace(".", " / "), declaration="config_effect_spec")
    return nodes


def _legacy_effects(report: Mapping[str, Any] | None) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    if not isinstance(report, Mapping):
        return nodes, edges
    settings = ((report.get("config_protocol") or {}).get("wire_affecting_settings") or [])
    for setting in settings:
        if not isinstance(setting, Mapping) or not isinstance(setting.get("setting"), str):
            continue
        config_path = str(setting["setting"])
        source_id = f"config.{config_path}"
        for index, effect in enumerate(setting.get("wire_effects") or []):
            if not isinstance(effect, Mapping):
                continue
            layer = str(effect.get("layer") or "uncategorized")
            path = str(effect.get("path") or "unknown")
            target_id = f"legacy_surface.{layer}.{hashlib.sha256(path.encode()).hexdigest()[:12]}"
            nodes[target_id] = _node(
                target_id, "legacy_compatibility_surface", path,
                layer=layer, behavior=effect.get("behavior"), proof_tier="legacy_compatibility",
            )
            edges.append(_edge(
                f"legacy_effect.{hashlib.sha256(f'{config_path}:{index}:{layer}:{path}'.encode()).hexdigest()[:16]}",
                source_id, target_id, "legacy_projects_to",
                behavior=effect.get("behavior"), condition=effect.get("condition"),
                proof_tier="legacy_compatibility",
            ))
    return nodes, edges


def compose_surface_graph(
    config_data: Mapping[str, Any],
    extractors: Mapping[str, Any],
    legacy_report: Mapping[str, Any] | None,
) -> dict[str, Any]:
    config_paths = ((config_data.get("config_schema") or {}).get("paths") or {})
    feature_rows = ((config_data.get("feature_crosswalk") or {}).get("features") or [])
    effect_links = list(config_data.get("effect_links") or [])
    nodes: dict[str, dict[str, Any]] = {
        f"config.{path}": _node(
            f"config.{path}", "config_path", path,
            required=fact.get("required"), declared_types=fact.get("declared_types"),
            schema_digest=fact.get("semantic_digest"),
        )
        for path, fact in config_paths.items()
    }
    for row in feature_rows:
        key = str(row["config_key"])
        nodes[f"feature.{key}"] = _node(
            f"feature.{key}", "feature", key,
            feature_id=row.get("feature_id"), stage=row.get("stage"),
            default_enabled=row.get("default_enabled"),
            schema_representation=row.get("schema_representation"),
        )
        policy_id = f"schema_policy.feature.{key}"
        nodes[policy_id] = _node(
            policy_id,
            "config_schema_policy",
            f"schema policy / {key}",
            action=row.get("schema_policy_action"),
            representation=row.get("schema_representation"),
            reason=row.get("schema_policy_reason"),
            source=row.get("schema_policy_source"),
        )
    nodes.update(_declared_target_nodes(effect_links))
    nodes.update(_canonical_surface_nodes(extractors))
    legacy_nodes, legacy_edges = _legacy_effects(legacy_report)
    nodes.update(legacy_nodes)

    edges: list[dict[str, Any]] = []
    for row in feature_rows:
        root_path = row.get("root_path")
        profile_path = row.get("profile_path")
        target = f"feature.{row['config_key']}"
        policy_id = f"schema_policy.feature.{row['config_key']}"
        edges.append(_edge(
            f"feature_schema_policy.{hashlib.sha256(f'{target}:{policy_id}'.encode()).hexdigest()[:16]}",
            target,
            policy_id,
            "has_schema_projection_policy",
            proof_tier="rust_schema_generator",
        ))
        for config_path in (root_path, profile_path):
            node_id = f"config.{config_path}"
            if config_path and node_id in nodes:
                edges.append(_edge(
                    f"feature_binding.{hashlib.sha256(f'{node_id}:{target}'.encode()).hexdigest()[:16]}",
                    node_id, target, "configures_feature", proof_tier="generated_schema_plus_rust_registry",
                ))
                edges.append(_edge(
                    f"schema_materialization.{hashlib.sha256(f'{policy_id}:{node_id}'.encode()).hexdigest()[:16]}",
                    policy_id, node_id, "materializes_as_config_path",
                    proof_tier="generated_schema_plus_rust_schema_generator",
                ))
    for link in effect_links:
        for config_path in link.get("config_paths") or []:
            source = f"config.{config_path}"
            if source not in nodes:
                continue
            edges.append(_edge(
                f"{link['id']}.{hashlib.sha256(config_path.encode()).hexdigest()[:12]}",
                source, str(link["target"]), str(link["relation"]),
                behavior=link.get("behavior"), condition=link.get("condition"),
                proof_tier=link.get("proof_tier"),
            ))
    edges.extend(edge for edge in legacy_edges if edge["source"] in nodes)
    edges.sort(key=lambda item: item["id"])

    referenced = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    unresolved = sorted(referenced - set(nodes))
    by_config: dict[str, list[str]] = {}
    by_surface: dict[str, list[str]] = {}
    by_feature: dict[str, list[str]] = {}
    for edge in edges:
        by_config.setdefault(edge["source"], []).append(edge["id"])
        by_surface.setdefault(edge["target"], []).append(edge["id"])
        if edge["source"].startswith("feature."):
            by_feature.setdefault(edge["source"], []).append(edge["id"])
        if edge["target"].startswith("feature."):
            by_feature.setdefault(edge["target"], []).append(edge["id"])
    graph: dict[str, Any] = {
        "format": "codex-wire-audit-surface-graph/v1",
        "nodes": {key: nodes[key] for key in sorted(nodes)},
        "edges": edges,
        "views": {
            "by_config": {key: sorted(value) for key, value in sorted(by_config.items())},
            "by_surface": {key: sorted(value) for key, value in sorted(by_surface.items())},
            "by_feature": {key: sorted(value) for key, value in sorted(by_feature.items())},
            "context_management": sorted(
                key for key in nodes if "context_management" in key or key.startswith("surface.route.alpha/")
            ),
        },
        "coverage": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "canonical_effect_count": sum(edge.get("proof_tier") != "legacy_compatibility" for edge in edges),
            "legacy_effect_count": sum(edge.get("proof_tier") == "legacy_compatibility" for edge in edges),
            "schema_policy_count": sum(node.get("kind") == "config_schema_policy" for node in nodes.values()),
            "unresolved_node_refs": unresolved,
        },
    }
    graph["semantic_digest"] = hashlib.sha256(canonical_json_bytes(graph)).hexdigest()
    return graph
