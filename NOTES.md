# Maintenance notes

## Purpose

The toolchain answers one question repeatedly and reproducibly: *what is the
current wire contract of the Codex CLI* — which endpoints exist, which
headers each transport carries, which metadata schemas the payloads use, and
which surfaces changed since the last audit. Everything in this repository is
a documentation-and-verification toolchain; it performs no traffic of its own
beyond reading public repository sources.

## Ownership

- This repository is intentionally scoped to the audit toolchain, its release
  pipeline, and release artifacts.
- Consumers of the generated reports (schema-conformance tests, fixture
  regeneration) live in their own repositories and pin the upstream commit
  they were generated against.

## Reproducibility contract

1. Every release derives from one frozen source snapshot (path/mode/byte).
2. Two independent clean copies must produce byte-identical wheel + sdist.
3. The installed wheel is probed for the capabilities and resources it
   actually contains; the probe backend is reported explicitly.
4. `verify --fast` covers integrity/topology/provenance/dependency/archive/
   byte-closure without re-installation; deep `verify` repeats installs and
   rebuilds.

## Upgrading the audited baseline

1. Pick the new upstream commit and run the CLI with `--repo-root` pointing
   at a clean checkout (avoids GitHub-API rate limits on optional files).
2. Resolve any report validation diagnostics — they indicate the audit's
   file set no longer matches the upstream layout (moved/renamed files).
3. Regenerate fixtures in consumer repositories and update their pinned
   `schema_version` / surface expectations in the same change.
