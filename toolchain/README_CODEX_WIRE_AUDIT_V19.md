# Codex Wire Audit v19 — generated config schema and full surface graph

v19 turns Codex configuration into a first-class canonical input surface and connects it to the request, metadata, tool, route, authentication, and context contracts already modeled by the generator.

For the design and ownership model, read [`CONFIG_SURFACE_ARCHITECTURE.md`](CONFIG_SURFACE_ARCHITECTURE.md). This document covers operation and proof boundaries. Neither document is a current setting reference; generated JSON owns exact keys, types, defaults, constraints, and relationships.

## Canonical inputs

The generator consumes three distinct upstream facts:

1. `codex-rs/core/config.schema.json` — accepted configuration shape;
2. the Rust `FEATURES` constant — feature identity, stage, and default state;
3. `features_schema()` — how a feature becomes a direct property, structured table, embedded field, or intentionally omitted user setting.

Those facts are cross-checked rather than collapsed. The generated schema is normalized into a path-addressable catalog, while the registry and projection policy explain why a feature is present at a particular path—or deliberately absent.

The resulting relationship is:

```text
configuration path
  → feature and schema-projection policy
  → provider/runtime effect
  → Responses body/header/metadata/tool/route/context surface
```

The graph contains two proof tiers:

- **canonical** links backed by generated schema, Rust sources, and active extractors;
- **legacy_compatibility** links imported from the frozen v10 config-effect catalog and kept visibly separate until migrated.

## Focused integration check

```bash
python tools/check_config_surface.py \
  --codex-root ../codex \
  --output config-surface-result.json \
  --schema-output config-schema.json \
  --graph-output config-surface-graph.json
```

The command exits nonzero when schema references, registry parsing, schema-projection policy, required anchors, effect paths, or graph references cannot be proved.

## Main CLI outputs

```bash
codex-wire-audit \
  --repo-root ../codex \
  --coverage-profile hybrid_v19 \
  --output report.json \
  --emit-config-schema config-schema.json \
  --emit-surface-graph config-surface-graph.json
```

Using `--output-dir` also writes `config-schema.json` and `config-surface-graph.json` automatically.

## Machine-readable authority

Use these outputs rather than prose for current details:

- `config-schema.json` for normalized paths and schema facts;
- `config-surface-graph.json` for typed config/feature/policy/surface links;
- `extractor.config_effects` in the report for crosswalks, diagnostics, and coverage;
- CI integration records for source revision and pass/fail status.

## Current boundary

The generated schema inventory and feature/schema-policy crosswalk are repository-wide. Selected behavioral edges are canonical; some remaining behavioral edges still come from the explicitly labeled frozen compatibility catalog. `codex_wire_full` therefore remains incomplete until every declared protocol family and required runtime scenario is canonical and observed.
