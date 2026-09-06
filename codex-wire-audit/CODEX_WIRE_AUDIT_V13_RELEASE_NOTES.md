# Codex wire audit v13 release notes

## Release focus

v13 makes metadata evolution a first-class proof artifact. It does not answer only whether a key exists in current Codex; it records what the key meant at exact repository revisions, where it appeared on the wire, how compatibility projections differed, which legacy inputs remain accepted or filtered, and which successor replaced it.

## Versioned metadata history

The built-in catalog contains seven keys, nineteen semantic states, ten exact Codex revision snapshots, and nine typed transitions. The primary legacy path is:

```text
code_mode_tool_names
  absent
  → nested Responses Lite metadata
  → omitted from bounded compatibility headers
  → coexists with tool_namespaces_info
  → removed from the payload but kept reserved
```

Its successor is modeled as `tool_namespaces_info`, including the `code_mode_name` mapping. The catalog also preserves the three historical tool-registry configuration names and the later core-ownership transition for `window_number` and `forked_from_ordinal_exclusive`.

## Multi-JSON distribution

The same contract is distributed as:

- one canonical catalog;
- one document per key;
- one document per exact version;
- a transition document;
- a closed index with exact sizes and SHA-256 hashes;
- a deterministic ZIP container.

The installed wheel contains the split container as package data. The history CLI can regenerate it byte-for-byte and verify existing directory containers.

## Proof integration

Proof attestation format v2 adds `metadata_history` as an independent dimension. History-aware profiles declare required key IDs. Source probes compare structured assertions against worktree or exact Git-object bytes and fail on removed-key reappearance, lost reservation, missing successor mapping, stale configuration names, or other state drift.

Unknown newer commits use the current catalog state only as an explicitly labeled forward canary. They are not represented as exact historical snapshots.

## CI and packaging corrections

- Removed the duplicate v12 test-module tree that broke plain `pytest` collection.
- Added deterministic two-copy history-container checks.
- Added a standalone container-verification CLI operation.
- Added proof-critical line/branch coverage ratchets.
- Added module, function, complexity, fan-out, and import-cycle budgets.
- Pinned the build backend and direct CI toolchain.
- Added an active workflow that retains durable evidence with `if: always()`.
- Added two-clean-copy distribution reproducibility and installed-wheel checks.

## Compatibility

- Frozen v10 report compatibility remains included.
- The v11 canonical evolution layer remains the active protocol-semantic base.
- The v12 `hybrid_v12` coverage profile remains available.
- New history-aware consumers should use `hybrid_v13`, a protocol-specific v2 profile, or `legacy_metadata_history`.

## Closed release assembly

The new release builder consumes only passed validation evidence and matching validated distributions. It writes deterministic full-bundle and machine-assets ZIPs, a CycloneDX SBOM, a complete external artifact inventory, per-artifact hash sidecars, and a source-tree manifest checked against every bundled file. The active workflow performs the assembly twice and rejects any byte difference.

The inherited unversioned v10 bundle manifest is not reused as v13 evidence; the full v13 ZIP receives a newly generated closed manifest and `SHA256SUMS.v13`.

## Deliberate limits

`codex_wire_full` still cannot pass until every canonical protocol family and required runtime scenario exists. The v13 history work prevents legacy-key drift from being hidden; it does not claim that the remaining full-protocol migrations or runtime observations have been completed.

The local release validator exercises synthetic source probes, strict schemas, containers, tests, package installation, and reproducible builds. A full local checkout probe of every historical Codex revision and live sanitized runtime-wire comparison remain separate integration dimensions.
