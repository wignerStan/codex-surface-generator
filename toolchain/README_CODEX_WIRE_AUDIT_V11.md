# Codex wire audit v11 — maintainability transition

This bundle preserves the complete **v10** implementation and report contract, while adding a packaged **v11 maintainability layer** around it. The goal is to make future Codex source evolution fail visibly and produce reviewable semantic changes instead of silently degrading a successful report.

## What v11 changes

v11 introduces one active package boundary:

```text
SourceRegistry
    ↓
SourceProvider (Git tree, worktree, archive, GitHub, cache)
    ↓
exact-byte SourceSnapshot
    ↓
registered semantic extractors
    ↓
canonical evolution contract + diagnostics
    ↓
v10 compatibility renderer / machine report
    ↓
semantic diff, schemas, JSON, JSONL, text
```

The first migrated canonical extractor covers `CodexResponsesMetadata::turn_metadata_payload`. It distinguishes:

- request identity;
- thread identity;
- independent turn identity;
- lineage;
- execution context;
- tool inventory;
- timing/history;
- compaction;
- flattened extra metadata.

Unknown field expressions no longer default to a guessed pass-through semantic. They produce `FIELD_EMISSION_EXPRESSION_UNCLASSIFIED`, set semantic completeness to false, and can fail CI.

## Compatibility and scope

The original v10 Python files, schemas, examples, and fixtures are included unchanged. Their release SHA-256 values are enforced when the compatibility adapter loads, so accidental edits fail before report generation. v11 reports retain the v10 report format and add an `evolution_contract` section.

This release is deliberately labeled `hybrid_canonical_ir`: turn-metadata semantics use the new canonical extractor path, while non-migrated protocol sections still use the frozen v10 adapter. That boundary is explicit in the report and in `codex_wire_audit/legacy.py`.

## Install or run in place

```bash
python codex_wire_audit_v11.py --self-test
```

```bash
python -m pip install .
codex-wire-audit --version
```

Audit an immutable local Git revision:

```bash
codex-wire-audit \
  --repo-root ../codex \
  --ref HEAD \
  --format canonical-json \
  --output codex-wire-report.json \
  --emit-ir codex-wire-evolution.json \
  --emit-schema generated-schemas
```

By default, local Git mode uses `git show <commit>:<path>`. Tracked worktree modifications do not alter the audit. To audit the worktree itself, opt in explicitly:

```bash
codex-wire-audit \
  --repo-root ../codex \
  --worktree \
  --allow-dirty-source \
  --format pretty-json
```

Compare against an earlier report:

```bash
codex-wire-audit \
  --repo-root ../codex \
  --baseline-report previous-report.json \
  --semantic-diff-output semantic-diff.json \
  --fail-on-semantic-change breaking \
  --output current-report.json \
  --format canonical-json
```

Handle a source move without editing Python:

```bash
codex-wire-audit \
  --repo-root ../codex \
  --source-registry codex_wire_audit_v11_examples/source-registry.overlay.example.json
```

## Important machine artifacts

- `codex_wire_audit_v11_schemas/`: referenced Draft 2020-12 schemas for the source registry, source snapshot, predicates, diagnostics, turn-metadata semantics, evolution contract, and semantic diff.
- `codex_wire_audit_v11_examples/`: deterministic contracts, a before/after identity split, an unclassified-expression example, and the default 76-source registry.
- `codex_wire_audit_v11_fixtures/`: source-level evolution fixtures.
- `test_codex_wire_audit_v11.py`: maintainability and mutation-oriented regression tests.
- `tools/build_v11_assets.py`: deterministic schema/example regeneration.
- `CODEX_WIRE_AUDIT_V11_METRICS.md` and `codex_wire_audit_v11_maintainability_metrics.json`: generated size, dependency-cycle, source-registry, schema, and test guardrails.
- `tools/maintainability_metrics.py`: reproducible metrics generation and CI budget enforcement.
- `tools/build_distribution.py`: offline wheel/sdist construction with fixed timestamps and normalized source-archive metadata.
- `tools/validate_release.py`: one-command offline compilation, test, fixture, asset, budget, reproducibility, sdist-round-trip, wheel-install, and package-data validation.

## Development

```bash
make test
make check-assets
make check-metrics
make validate
make dist
```

A new semantic area should be added as a registered extractor, not as another branch in the v10 monolith. Extractor types and factories live in `extractors/registry.py`, keeping package activation free of import cycles. See `docs/ADDING_AN_EXTRACTOR.md` and `CODEX_WIRE_AUDIT_V11_ARCHITECTURE.md`.

## Validation status

The release validation runs:

- Python byte compilation;
- 62 unchanged v10 tests;
- 39 v11 maintainability tests;
- schema meta-validation and fixture validation;
- deterministic asset regeneration;
- reproducible wheel and source-distribution builds, clean-environment install, package-data check, and installed CLI self-test;
- byte-for-byte v10 preservation checks;
- ZIP integrity and checksum verification.

A complete live audit against every file in current Codex was not executed inside this restricted runtime. The source-evolution regression is grounded in the observed thread-identity/turn-identity split, and the bundle clearly marks runtime observation and current-main conformance as `not_run`/`not_compared` until such jobs are run externally.
