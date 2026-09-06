# Codex wire audit v13

v13 adds **versioned metadata-key history** to the v12 independent proof gate. It is designed for keys whose meaning changes over time, especially keys that remain reserved after their wire representation is removed.

The current example is `code_mode_tool_names`. It was introduced as nested Responses Lite turn metadata, later excluded from the bounded compatibility header, briefly coexisted with `tool_namespaces_info`, and is now removed from the payload while remaining reserved against client/config extras. The catalog preserves every modeled state instead of replacing history with the latest answer.

## What is new

- Strict Draft 2020-12 metadata-history catalog and probe schemas.
- Ten exact Codex revision snapshots and nine typed transitions.
- Seven tracked current or legacy keys.
- Per-key, per-version, and transition JSON documents.
- Deterministic multi-JSON directory and ZIP containers.
- Member size/hash inventory and closed-container verification.
- Worktree or exact Git-object source probes.
- Forward-canary behavior for commits newer than the catalog.
- Metadata history as an executable proof-profile dimension.
- Stable history semantic diffs and diagnostic codes.
- CI coverage, complexity, size, fan-out, and import-cycle ratchets.

## Tracked keys

### Turn metadata

- `code_mode_tool_names`
- `tool_namespaces_info`
- `window_number`
- `forked_from_ordinal_exclusive`

### Configuration history

- `include_tool_namespaces_info`
- `include_tool_metadata`
- `turn_metadata_includes_tool_info`

The three configuration names are deliberately separate entities. A rename is represented as a transition, not as one timeless key with aliases hidden in prose.

## `code_mode_tool_names` timeline

| State | First modeled revision | Nested turn metadata | Direct compatibility header | External MCP metadata | Client/config override |
| --- | --- | --- | --- | --- | --- |
| absent | `63fe5a6b...` | absent | absent | absent | not reserved |
| active | `25b6fc9b...` | conditional, Responses Lite | conditional | omitted | reserved/filtered |
| active, bounded header | `5c36e869...` | conditional | omitted | omitted | reserved/filtered |
| active with successor | `2b1811e5...` | conditional | omitted | omitted | reserved/filtered |
| removed but reserved | `8e4b1044...` through current reviewed main | absent | absent | absent | reserved/filtered |

Its successor is `tool_namespaces_info`. The new structure records each visible namespace and function, including the function's optional `code_mode_name`, direct/deferred exposure, and harness or MCP ownership.

## Query the catalog

Validate and print the complete catalog:

```bash
codex-wire-audit-history --validate
```

List tracked keys and revision counts:

```bash
codex-wire-audit-history --list-keys
```

Inspect one legacy key:

```bash
codex-wire-audit-history --key code_mode_tool_names
```

Inspect an exact revision:

```bash
codex-wire-audit-history \
  --commit 5c36e869c1a7bdc5ec4c1325d02e60dfbadcc9b8
```

Diff two modeled versions:

```bash
codex-wire-audit-history \
  --diff \
  version.2026-07-25.code_mode_metadata_introduced \
  version.2026-08-07.code_mode_metadata_removed
```

## Multi-JSON container

Generate both a directory and deterministic ZIP:

```bash
codex-wire-audit-history \
  --output-dir codex_wire_audit_v13_metadata_history_container \
  --zip codex_wire_audit_v13_metadata_history_container.zip \
  --output metadata-history-build.json
```

The directory contains:

```text
catalog.json
index.json
transitions.json
keys/<stable-key-id>.json
versions/<ordinal>-<stable-version-id>.json
```

`index.json` records every declared member's exact byte length and SHA-256, the catalog digest, key/version counts, and the current version reference. Undeclared files, missing members, duplicate ZIP names, traversal names, size mismatches, hash mismatches, invalid catalog semantics, and digest mismatches are verification failures.

Verify an existing directory:

```bash
codex-wire-audit-history \
  --verify-container codex_wire_audit_v13_metadata_history_container \
  --output metadata-history-verification.json
```

## Source verification

Verify the selected history state against a checkout:

```bash
codex-wire-audit-history \
  --source-root /path/to/codex \
  --source-mode commit \
  --ref ddf04ad26789d040f9ef6a96736f76602e35a6cc \
  --commit ddf04ad26789d040f9ef6a96736f76602e35a6cc \
  --output metadata-history.probe.json
```

The probe reads exact Git object bytes in `commit` mode. It compares structured facts rather than descriptions, including:

- wire literal and constant aliases;
- reserved and backward-compatible-reserved sets;
- nested payload presence and assignment;
- direct flat `client_metadata` emission;
- compatibility-header inclusion or projection;
- external MCP projection;
- successor `code_mode_name` structure and mapping;
- configuration definition, generated schema property, and runtime use.

An unknown commit uses the latest catalog state as a **forward canary**. The attestation records that the selection is not exact. A reintroduced removed field, missing reservation, missing successor mapping, or renamed config property then fails rather than silently inheriting a latest-state label.

## Proof profiles

v13 retains `hybrid_v12` for compatibility and adds history-aware profiles:

- `hybrid_v13`: transition proof with all seven history keys.
- `turn_metadata_only`: canonical turn-metadata semantics plus turn-key history.
- `responses_only`: Responses extractors plus metadata/config history.
- `mcp_only`: MCP projection plus tool-metadata/config history.
- `legacy_metadata_history`: history-focused compatibility profile.
- `codex_wire_full`: all canonical protocol families, metadata history, and runtime conformance.

The full profile is still deliberately incomplete until every canonical protocol family and required runtime scenario exists. Versioned history strengthens the claim machinery; it does not relabel the hybrid generator as full repository proof.

## Stable exit codes

Both proof CLIs use:

```text
0  selected operation or proof completed
2  operational/input failure
3  catalog, container, source proof, or selected profile incomplete
```

Normal failures produce stable diagnostics and no traceback unless `--debug` is supplied.

## CI commands

```bash
python -m pytest -q
codex-wire-audit-proof --self-test
codex-wire-audit-history --self-test
python tools/maintainability_metrics.py --check
coverage run --branch -m pytest -q
coverage json -o coverage-v13.json
python tools/check_v13_coverage.py coverage-v13.json
```

The active workflow also generates the multi-JSON container twice and compares every byte before verifying both copies.

## Reproducible release assembly

Run the complete validation and release build with:

```bash
make release
```

`tools/build_v13_release.py` consumes the passed validation evidence and the exact validated wheel/sdist. It emits a closed source-bundle manifest, SHA-256 inventory, CycloneDX SBOM, deterministic full and machine-assets ZIPs, the split metadata-history container, per-key timeline and semantic-diff JSON, and hash sidecars. CI builds this complete release twice in independent output directories and requires a byte-for-byte match.

## Deliberate boundary

The catalog contains exact reviewed commits and source-assertion rules, but a full local probe of every historical Codex checkout is a separate integration job. Current-main and runtime-conformance results remain independent proof dimensions. A missing live checkout or missing runtime evidence is reported as `not_run` or `incomplete` according to the selected profile; it is never folded into a vague `complete` flag.
