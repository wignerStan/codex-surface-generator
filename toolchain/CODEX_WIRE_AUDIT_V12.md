# Codex wire audit v12 hardening

v12 adds an independent CI proof gate instead of trusting a report's own completeness flag.
The gate emits its attestation atomically before applying failure policy.

## CI command

```bash
codex-wire-audit-proof \
  --report current.json \
  --attestation current.proof.json \
  --profile codex_wire_full \
  --source-root /path/to/codex \
  --ref "$PINNED_CODEX_SHA" \
  --source-mode commit
```

A `codex_wire_full` proof requires all canonical extractor families and the runtime-conformance
dimension. Until those migrations and scenarios exist, it intentionally exits 3 with a durable
`incomplete` attestation. Use `hybrid_v12` only for transition CI; it does not claim full canonical
coverage.

## Stable exit codes

- `0`: selected profile is complete;
- `2`: operational/input failure;
- `3`: machine proof is incomplete.

## Hardened failure cases

- coverage profile labels are executable requirements;
- struct spreads and macros cannot produce a complete Rust semantic proof;
- helper function bodies contribute to semantic fingerprints;
- worktree bytes cannot be attributed to a non-HEAD ref;
- loaded untracked files are dirty source evidence;
- strict Draft 2020-12 validation and graph invariants run independently;
- duplicate diagnostics merge monotonically;
- the installed proof command suppresses tracebacks unless debug mode is requested;
- proof dependencies are declared as runtime dependencies.

## Validated semantic diff

```bash
codex-wire-audit-proof-diff \
  --baseline pinned.json \
  --candidate current.json \
  --output semantic-diff.json \
  --fail-on validation
```

Both inputs are independently schema/invariant checked. Change IDs are derived from normalized
semantic content, so collection ordering does not renumber changes.

## Runtime evidence binding

Full proof accepts a runtime evidence document only when its `report_sha256` matches the exact
static report and every scenario required by the selected profile has status `passed`. Supply it
with `--runtime-evidence runtime.json`.
