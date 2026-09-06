# v11 maintainability architecture

## Design objective

The dangerous failure mode for a source audit is not a crash. It is a successful report whose semantics silently became stale. v11 therefore optimizes for four invariants:

1. an unknown source construct must become a diagnostic;
2. source identity must describe the exact loaded bytes;
3. protocol changes must produce stable semantic deltas;
4. future extractors must be independently testable and register without extending a monolithic function.

## Package boundaries

### `source_registry.py`

The registry is the single inventory used by every source provider. Each `SourceSpec` has a stable ID, legacy compatibility key, candidate paths, requiredness, roles, expected symbols, and owning extractor IDs. A JSON overlay can change candidate paths or requiredness without modifying generator code.

### `sources/`

Provider implementations are split by responsibility (`git.py`, `github.py`, `archive.py`, `cache.py`, and shared exact-byte finalization in `common.py`). The old `source_providers.py` name remains only as a deprecation shim. All source modes produce the same `SourceSnapshot` abstraction:

- immutable local Git tree;
- explicit worktree;
- bounded source archive;
- GitHub source;
- legacy immutable cache.

The authoritative revision identity is `source_set_sha256`, computed over sorted path + NUL + exact bytes + NUL for every loaded source. A Git commit remains useful provenance but does not replace content identity.

Archive extraction rejects traversal, links, special files, duplicate paths, case-folded collisions, Unicode-normalized collisions, excessive member count, oversized files, excessive total size, and suspicious compression ratios.

### `extractors/`

`extractors/registry.py` owns the protocol, factory registry, and result type. Package initialization activates built-ins only after the registry is initialized, avoiding circular imports. Extractors consume a `SourceSnapshot`, emit canonical facts, and write structured diagnostics directly at discovery time. `extractor.turn_metadata` is the first migration.

Its output records both:

- `emission_fingerprint`: the field’s own expression, gate reference, value expression, and identity domain;
- `semantic_fingerprint`: the effective semantics after resolving the referenced gate predicate.

This distinction prevents one gate edit from generating duplicate field changes for every unchanged dependent field.

### `diagnostics.py`

Diagnostic IDs derive from machine identity (`code`, extractor, entity, source refs, and details), not human wording. Messages can improve without invalidating suppressions or CI baselines.

### `semantic_diff.py`

Diff records have stable content-derived IDs. Current change kinds include gate add/remove/predicate change, field add/remove/gate change/emission-kind change/value-expression change/domain change, and source registry changes.

### `schema_identity.py`

Rust schemas are qualified with crate/module/source context. When source resolution is unavailable, the ID includes a path/pointer digest. Conflicting shapes are errors rather than informational annotations.

### `canonical.py`

Canonical JSON uses sorted keys, compact separators, UTF-8, NFC normalization, finite numbers, and a terminal newline. It rejects Unicode-normalized key collisions instead of overwriting a value.

### `legacy.py`

This is the only supported import boundary to v10. It verifies the frozen v10 release hashes before dynamic loading, so accidental compatibility edits fail early. New extractors should never import `_codex_wire_audit_base_v10.py`, `codex_wire_audit_v10.py`, or `_codex_wire_contract_v10.py` directly.

## Status is multidimensional

The evolution contract does not use one overloaded `complete` Boolean. It records:

```json
{
  "source_inventory": "complete",
  "syntax_extraction": "complete",
  "semantic_classification": "complete",
  "schema_resolution": "complete",
  "runtime_observation": "not_run",
  "current_main_conformance": "not_compared",
  "overall": "complete"
}
```

`overall` is relative to a named coverage profile. Runtime observation and current-main comparison remain independent dimensions.

## Migration path beyond v11

Move one protocol family at a time:

1. add source specs and a canonical extractor;
2. add source mutation and semantic-diff fixtures;
3. render the existing v10 view from canonical facts;
4. prove compatibility with a golden report;
5. delete that family’s legacy reconstruction;
6. only then begin the next family.

Recommended next extractor order:

1. Responses request construction and Lite rewrites;
2. MCP turn metadata projection;
3. response event dispatch and errors;
4. redirect/runtime-generated headers;
5. config-to-wire effects;
6. app-server macro inventories.

When all families are migrated, remove the compatibility adapter and bump the report format as a deliberate breaking release.

## Structural budgets

`tools/maintainability_metrics.py` computes deterministic AST and import-graph metrics. The release currently enforces no active module above 600 lines, no active function above 200 lines, and no internal package import cycles. Generated results are stored in `CODEX_WIRE_AUDIT_V11_METRICS.md` and `codex_wire_audit_v11_maintainability_metrics.json`.
