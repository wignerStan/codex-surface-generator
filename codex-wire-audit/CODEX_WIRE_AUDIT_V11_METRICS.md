# Codex wire audit v11 maintainability metrics

These metrics compare the three frozen v10 generator modules with the active v11 maintainability layer. The frozen v10 modules remain in the bundle only as a compatibility renderer and are intentionally excluded from the v11 active-layer totals.

| Metric | v10 structural baseline | v11 active layer |
| --- | ---: | ---: |
| Python modules | 3 | 25 |
| Physical Python lines | 12,671 | 4,109 |
| Largest module | 5,016 | 513 |
| Largest function | 1,254 | 148 |
| Modules over 600 lines | 3 | 0 |
| Functions over 200 lines | 16 | 0 |
| Internal import cycles | not measured | 0 |

The largest active module is **`codex_wire_audit/extractors/turn_metadata.py`** at 513 lines. The largest active function is **`compare`** at 148 lines. Relative to v10, the largest module is reduced by **89.8%** and the largest function by **88.2%**.

## Generated maintenance assets

- Source registry entries: **76** (16 required, 60 optional).
- Strict JSON Schema templates: **7**.
- Regression test methods: **101**.
- Active package source digest: `84265a4d14bdff28d20d832ef2424c90b36e851ea1c96a85137ff740cbf3fffd`.

## Enforced budgets

- No active module above 600 lines: **true**.
- No active function above 200 lines: **true**.
- No internal package import cycles: **true**.

Run `python tools/maintainability_metrics.py --check` in CI to enforce these generated metrics and budgets.
