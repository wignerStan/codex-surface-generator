# Codex Wire Audit v19 release notes

## Canonical configuration surface

- Added `extractor.config_effects`.
- Added exact-source ingestion of `codex-rs/core/config.schema.json`.
- Added recursive Draft-07/2020-12-compatible schema normalization.
- Bounded feature extraction to the Rust `FEATURES` constant so type declarations and function signatures cannot masquerade as registry entries.
- Added source-derived `features_schema()` projection policy, including direct, structured, embedded, and intentionally omitted feature representations.
- Added exact root/profile cross-checks, including dotted nested feature keys.
- Added selected canonical configuration-to-surface links.
- Added a bidirectional graph over configuration, feature, schema policy, provider, Responses, metadata, MCP, and context-management surfaces.
- Preserved older config-effect evidence only as `legacy_compatibility` graph edges.

## Fail-visible drift

The extractor becomes incomplete for invalid generated JSON, unresolved references, an unbounded or malformed feature registry, unclassified schema projection branches, duplicate feature identities, unexplained missing feature paths, missing full-picture anchors, missing selected effect paths, and unresolved graph references.

An internal feature deliberately excluded by `features_schema()` is recorded as such rather than reported as a false schema omission. A registry key embedded in a structured table is connected to its exact nested path.

## Documentation model

Added `CONFIG_SURFACE_ARCHITECTURE.md`. It explains architecture, proof tiers, ownership, and graph composition without duplicating the setting catalog. Generated JSON remains authoritative for current names, shapes, defaults, constraints, and effect links.

## CLI and CI

- Added `--emit-config-schema` and `--emit-surface-graph`.
- Output directories include both artifacts.
- Added `config_surface_only` and `hybrid_v19` profiles.
- Added a real-checkout pinned integration command and root GitHub Actions workflow.
- `codex_wire_full` requires the `config_schema_surface_graph` runtime scenario.

## Reviewed baseline

The required pinned integration baseline is:

```text
openai/codex@6af345407d9c2a568da9d01b6c4b81a9e61495c0
```

Current-main drift remains a separate canary dimension rather than being silently substituted for the reviewed baseline.
