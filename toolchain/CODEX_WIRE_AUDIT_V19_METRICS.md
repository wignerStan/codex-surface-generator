# Codex wire audit v19 maintainability metrics

The enforced scope now includes both the installable package and the release scripts. This closes the former blind spot where an oversized or highly coupled publisher could pass while package-only metrics remained green.

| Metric | Frozen v10 baseline | Active package | Active release tools | Combined enforced scope |
| --- | ---: | ---: | ---: | ---: |
| Python modules | 3 | 72 | 12 | 84 |
| Physical Python lines | 12,671 | 17,696 | 3,347 | 21,043 |
| Largest module | 5,016 | 581 | 496 | 581 |
| Largest function | 1,254 | 200 | 174 | 200 |
| Highest complexity | not measured | 41 | 36 | 41 |
| Maximum module fan-out | not measured | 10 | 8 | 10 |
| Internal import cycles | not measured | 0 | 0 | 0 |

Largest combined module: **`codex_wire_audit/metadata_history.py`** (581 lines). Largest combined function: **`build_history_mutation`** (200 lines).

## Generated maintenance assets

- Source registry entries: **160** (16 required, 144 optional).
- Strict schemas: **33**, including **1** release-spec schema.
- Metadata history: **7 keys**, **19 states**, **10 versions**, **9 transitions**.
- Regression test methods: **309**.
- Combined source digest: `00ec4f5569b9eba01be2ec3cc8cb84c3d408035842163760f7b287660b0c4a76`.

## Enforced budgets

- Module ≤ 600 lines: **true**.
- Function ≤ 200 lines: **true**.
- Complexity ≤ 45: **true**.
- Module fan-out ≤ 10: **true**.
- Internal import cycles forbidden: **true**.
