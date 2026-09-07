# Maintenance notes

## Purpose

The generator answers one question repeatedly and reproducibly: *what surface
does the selected Codex source snapshot expose* — which endpoints exist, which
headers each transport carries, which metadata/settings/feature schemas the
payloads use, and which surfaces changed since the last generation. Everything
in this repository is a schema-generation and verification toolchain; it
performs no traffic of its own beyond reading public repository sources.

“Current” is intentionally not used as an unqualified synonym for “latest
upstream main.” A report is current for the exact source identity recorded in
that report. A separately scheduled canary detects newer upstream drift.

## Ownership

- This repository is intentionally scoped to the audit toolchain, its release
  pipeline, release artifacts, and architecture notes tied to explicit evidence
  snapshots.
- Consumers of the generated reports (schema-conformance tests, fixture
  regeneration) live in their own repositories and pin the upstream commit
  they were generated against.
- Human architecture notes own design explanation and evidence boundaries.
  Generated JSON owns exact setting names, field shapes, defaults, constraints,
  source hashes, and graph relationships.
- A public-source note, a shipped Desktop-bundle observation, and a private-host
  inference are different evidence classes and must remain labeled separately.

## Reproducibility contract

1. Every release derives from one frozen source snapshot (path/mode/byte).
2. Two independent clean copies must produce byte-identical wheel + sdist.
3. The installed wheel is probed for the capabilities and resources it
   actually contains; the probe backend is reported explicitly.
4. `verify --fast` covers integrity/topology/provenance/dependency/archive/
   byte-closure without re-installation; deep `verify` repeats installs and
   rebuilds.
5. Architecture documentation records its own source snapshot instead of
   inheriting the generator baseline implicitly.
6. Checked-in release artifacts are described by their actual version; an
   advanced source tree does not retroactively upgrade an older release bundle.

## Documentation review

Use [`ARCHITECTURE_DOCS_REVIEW.md`](ARCHITECTURE_DOCS_REVIEW.md) as the index of
active architecture documents, evidence classes, source pins, and review
caveats. The slide-format lifecycle and topology notes are deliberately
conceptual; machine-readable schemas remain authoritative for exact RPC fields.

## Upgrading the audited baseline

1. Pick the new upstream commit and run the CLI with `--repo-root` pointing
   at a clean checkout (avoids GitHub-API rate limits on optional files).
2. Resolve any report validation diagnostics — they indicate the audit's
   file set no longer matches the upstream layout (moved/renamed files).
3. Regenerate fixtures in consumer repositories and update their pinned
   `schema_version` / surface expectations in the same change.
4. Review architecture documents whose source anchors changed. Do not update a
   snapshot label merely because upstream `main` advanced.
5. Build and verify a new release directory before describing its artifacts as
   published.
