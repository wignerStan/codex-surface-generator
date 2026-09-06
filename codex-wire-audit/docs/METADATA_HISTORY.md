# Adding or changing metadata-key history

The history catalog is an executable contract, not a changelog. Every key, state, version, transition, source assertion, and evidence reference must validate before the catalog can be packaged.

## Add a key

1. Add a stable `key.*` record to `codex_wire_audit/metadata_history_data/metadata-key-history.v1.json`.
2. List all source candidates needed to observe its state.
3. Add one or more `state.*` records with structured assertions.
4. Add the selected state to every exact version snapshot.
5. Add typed transitions where the state changes.
6. Update `current_state_ref` and replacement links.
7. Recompute `integrity.canonical_payload_sha256` using `catalog_payload_sha256`.
8. Add adversarial source-probe tests for silent reappearance, disappearance, reservation changes, or successor changes.
9. Regenerate and verify the multi-JSON container.

## State design

Do not collapse these into one status:

```text
absent
active
active_bounded
active_with_successor
removed_reserved
renamed
backward_compatible_reserved
```

Container presence is modeled separately for flat `client_metadata`, nested turn metadata, the direct compatibility-header JSON, MCP `_meta`, configuration input, and internal runtime state.

## Assertion design

Assertions must be machine-observable. Human descriptions are explanatory only. Add an observation helper when a new fact cannot be represented by the existing source probe; never infer the assertion from prose.

## Version design

A version is an exact repository commit plus a complete key-to-state map. Keep ordinals monotonic. The current version is an exact reviewed snapshot, while later unknown commits are tested against it as forward canaries.

## Compatibility policy

A removed wire key may remain relevant because:

- clients could try to reintroduce it through extra metadata;
- old configuration files may still contain it;
- compatibility projections may intentionally differ from canonical payloads;
- its internal concept may feed a richer replacement structure.

Model these states explicitly rather than deleting the key from the catalog.
## Release container verification

`make release` runs validation first and then packages the canonical catalog, every split member, strict schemas, profile definitions, evidence, distributions, and SBOM. The complete and machine-assets release ZIPs use normalized paths, timestamps, and modes. Their external hash sidecars and artifact inventory are generated only after the archives pass duplicate-name, traversal-name, and CRC checks.

