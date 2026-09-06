# Codex wire audit v13 maintainability metrics

These metrics compare the three frozen v10 generator modules with the active v13 package. Frozen compatibility generators and thin legacy entrypoints are intentionally excluded from the active-package totals.

| Metric | v10 structural baseline | v13 active package |
| --- | ---: | ---: |
| Python modules | 3 | 42 |
| Physical Python lines | 12,671 | 8,941 |
| Largest module | 5,016 | 581 |
| Largest function | 1,254 | 199 |
| Highest cyclomatic complexity | not measured | 41 |
| Maximum module fan-out | not measured | 10 |
| Modules over 600 lines | 3 | 0 |
| Functions over 200 lines | 16 | 0 |
| Internal import cycles | not measured | 0 |

The largest active module is **`codex_wire_audit/metadata_history.py`** at 581 lines. The largest active function is **`build_attestation`** at 199 lines. The most complex active function is **`verify_git_source`** at complexity 41. Relative to v10, the largest module is reduced by **88.4%** and the largest function by **84.1%**.

## Generated maintenance assets

- Source registry entries: **85** (16 required, 69 optional).
- Strict JSON Schema templates: **13** (7 evolution, 6 proof/history).
- Versioned metadata history: **7 keys**, **19 states**, **10 versions**, and **9 transitions**.
- Regression test methods: **146**.
- Active package source digest: `713ef88d2b14cd51adad1b677bb90ba155c069b7d50af2d4538f3d6990e65407`.

## Enforced budgets

- No active module above 600 lines: **true**.
- No active function above 200 lines: **true**.
- No active function above cyclomatic complexity 45: **true**.
- No active module fan-out above 10: **true**.
- No internal package import cycles: **true**.

Run `python tools/maintainability_metrics.py --check` in CI to enforce the generated metrics and budgets.
