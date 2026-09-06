# Codex Wire Audit v19 — generated config schema and full surface graph

v19 turns Codex configuration into a first-class canonical input surface.

The generator reads Codex's generated `codex-rs/core/config.schema.json`, resolves its local JSON Schema references, and emits a path-addressable catalog containing root settings, profile settings, dynamic provider/server maps, defaults, enums, constraints, descriptions, composition branches, and source identity.

It then reads the Rust `FeatureSpec` registry and cross-checks every active feature key against both:

- `features.<key>`
- `profiles.*.features.<key>`

Finally it connects those inputs to previously modeled surfaces:

```text
configuration path
  → feature/provider/runtime effect
  → Responses body/header/metadata/tool/route/context surface
```

The graph contains two proof tiers:

- **canonical** links backed by the generated schema plus current extractors;
- **legacy_compatibility** links imported from the frozen v10 config-effect catalog, kept visibly separate until each family receives a native extractor.

## Focused integration check

```bash
python tools/check_config_surface.py \
  --codex-root ../codex \
  --output config-surface-result.json \
  --schema-output config-schema.json \
  --graph-output config-surface-graph.json
```

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

## Current boundary

The generated schema inventory is repository-wide, but some behavioral edges still come from the explicitly labeled frozen compatibility catalog. `codex_wire_full` therefore remains incomplete until all declared protocol families and runtime scenarios are canonical and observed.
