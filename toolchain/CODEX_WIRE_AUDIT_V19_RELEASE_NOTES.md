# Codex Wire Audit v19 release notes

## Canonical configuration surface

- Added `extractor.config_effects`.
- Added exact-source ingestion of `codex-rs/core/config.schema.json`.
- Added recursive Draft-07/2020-12-compatible schema normalization.
- Added root/profile feature cross-checks against the Rust `FeatureSpec` registry.
- Added selected canonical configuration-to-surface links.
- Added a bidirectional graph over configuration, feature, provider, Responses, metadata, MCP, and context-management surfaces.
- Preserved older config-effect evidence only as `legacy_compatibility` graph edges.

## Fail-visible drift

The extractor becomes incomplete for invalid generated JSON, unresolved references, malformed/duplicate feature registry entries, registered features missing from the generated schema, missing full-picture anchor paths, missing selected effect paths, and unresolved graph references.

## CLI and CI

- Added `--emit-config-schema` and `--emit-surface-graph`.
- Output directories now include both artifacts.
- Added `config_surface_only` and `hybrid_v19` profiles.
- Added a real-checkout integration command and root GitHub Actions workflow.
- `codex_wire_full` now requires the `config_schema_surface_graph` runtime scenario.

## Reviewed baseline

The required pinned integration baseline remains:

```text
openai/codex@6af345407d9c2a568da9d01b6c4b81a9e61495c0
```

Current-main drift remains a separate canary dimension rather than being silently substituted for the reviewed baseline.
