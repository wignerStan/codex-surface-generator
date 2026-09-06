# Adding a canonical extractor

1. Add or update a `SourceSpec` in the registry builder. Give it a stable `source_spec.*` ID, candidate paths, expected symbols, and the extractor ID.
2. Add `codex_wire_audit/extractors/<family>.py`.
3. Import `ExtractorResult` and `register_extractor` from `codex_wire_audit.extractors.registry`, then implement an object with `extractor_id`, `source_spec_ids`, and `extract(snapshot, diagnostics)`.
4. Register a factory with `@register_extractor("extractor.<family>")`.
5. Emit normalized data and semantic fingerprints. Never use human descriptions as machine predicates.
6. Emit an explicit diagnostic for every unclassified expression or unresolved source construct.
7. Add three fixtures: current shape, an earlier/alternate shape, and an unclassified mutation.
8. Extend `semantic_diff.py` with stable change kinds when the family needs more than generic add/remove behavior.
9. Add a strict schema for the extractor data and reference it from `evolution-contract.schema.json`.
10. Add an overlay renderer only when compatibility output must be corrected. The canonical extractor remains authoritative.
11. Run `make check-metrics`; split helpers before an active function exceeds 200 lines or a module exceeds 600 lines. Do not introduce an internal import cycle.

An extractor is ready when formatting-only mutations preserve its semantic digest and every semantic mutation produces either the correct delta or a structured diagnostic.
