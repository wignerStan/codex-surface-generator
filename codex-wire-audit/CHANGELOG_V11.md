# v11 changes

## Added

- Installable `codex_wire_audit` package and one compatibility boundary to v10.
- Declarative source registry with JSON overlays and fallback paths.
- Exact-byte source snapshots and source-set revision IDs.
- Immutable Git-tree mode distinct from explicit dirty-worktree mode.
- Bounded and collision-safe archive loading.
- Registered canonical extractor architecture.
- Source-derived request/thread/turn/lineage identity semantics.
- Fail-visible unclassified field and gate expressions.
- Direct-emission and effective-semantic fingerprints.
- Stable semantic diff records suitable for current-main canaries.
- Qualified schema identities and hard conflict diagnostics.
- Collision-safe canonical JSON.
- Strict referenced Draft 2020-12 maintainability schemas.
- Deterministic generated examples, source registry, build helper, CI templates, and Make targets.
- Split Git, GitHub, archive, cache, and shared source logic into independent provider modules.
- Dedicated extractor registry module with no internal package import cycles.
- Frozen-v10 SHA-256 verification before compatibility loading.
- Deterministic maintainability metrics and CI budgets for module size, function size, and import cycles.
- Wheel package-data coverage for all strict schema templates.
- Offline reproducible wheel and normalized source-distribution builder.
- One-command offline release validator, including sdist-to-wheel byte-for-byte round trips.

## Preserved

- All v10 source files and v10 report compatibility.
- Existing v10 schemas, examples, fixtures, and tests.

## Explicitly not completed

- Full canonical-IR migration of Responses, Lite, MCP, config, events, redirects, and app-server sections.
- Runtime wire observation.
- A full current-main repository audit in this restricted execution environment.
