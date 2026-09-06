# Codex wire audit v18 release notes

v18 / package `11.0.0` hardens the release scripts rather than introducing another Codex endpoint catalog. The protocol baseline remains Codex commit `6af345407d9c2a568da9d01b6c4b81a9e61495c0` and retains the Context Management model: model-gated activation, fresh context-window rollover, `alpha/history/v2/*`, `alpha/notes/v2/*`, and the bounded `thread_hint` bootstrap.

## Fixed release-closure defects

Real end-to-end release attempts exposed defects that ordinary pytest execution had hidden:

- the documented direct script invocation failed outside pytest because the repository root was missing from `sys.path`;
- the installed capability probe imported `jsonschema` despite deliberately installing the wheel without dependencies;
- an exclusive-create lock file could remain permanently after SIGKILL or runner timeout;
- pytest summaries containing warnings were not recognized;
- deep verification existed only in console output rather than durable release evidence;
- validation logs contained transient paths and elapsed times, preventing independent-run reproducibility;
- top-level deterministic ZIPs inherited private temporary-file modes;
- wheel runtime dependencies were declared but not bound to the release specification;
- archive verification accepted non-canonical permission bits;
- the wheel backend emitted a group-writable generated `RECORD` member even when source modes and process umask were canonical;
- setuptools emitted importable-package warnings for JSON-only resource directories;
- release-tool complexity was absent from the maintainability budget.

Each issue is now covered by regression tests.

## New release architecture

The active implementation is split into ownership-specific modules:

```text
tools/release_pipeline.py       orchestration and policy
tools/release_spec.py           identity, dependency, and source binding
tools/release_runtime.py        sanitized commands, builds, and probes
tools/release_archives.py       archive construction and validation
tools/release_assembly.py       closed artifact-graph assembly
tools/release_verification.py   independent fast/deep verification
tools/release_publish.py        destination safety, locking, rollback
tools/release_integrity.py      hashing and atomic byte operations
tools/release_types.py          shared immutable records and errors
```

The former `release_common.py` remains a thin compatibility facade. Maintainability metrics now enforce package and active release-tool code together.

## Durable deep evidence

The pipeline first assembles a preliminary candidate and deeply verifies its wheel, sdist, and source ZIP. The deterministic result is then inserted into the validation record, whose integrity digest is bound by the final release attestation. Final release assembly happens twice from that updated record.

A later deep `verify` recomputes the result and requires canonical equality with the recorded evidence. Fast verification still requires the durable evidence to exist and validates the attestation binding.

## Safer publication

The output lock now uses the operating system rather than the existence of a file as ownership. The diagnostic lock file can survive process death without blocking the next valid run. Symlink, special-file, cross-user, and multi-link lock targets are rejected before truncation.

Release publication requires `--replace` when the destination exists, stages a complete copy, compares its recursive byte/mode manifest, performs an atomic swap, and restores the prior directory on failure.

## Reproducibility and dependency closure

The release transcript is redacted and normalized before packaging. Two separate pipeline processes using the same source and toolchain must produce byte-identical complete release directories.

`release.runtime_dependencies` now binds `pyproject.toml` to wheel `Requires-Dist` metadata using normalized PEP 508 requirements. Direct-URL dependencies and duplicate normalized requirements are rejected.

Completed wheels are deterministically rewritten before inspection so every member has a canonical timestamp, order, compression form, and `0644`/`0755` mode. The release verifier then revalidates every `RECORD` row and rejects any packaging warning. JSON-only resource directories are discovered as namespace packages, eliminating setuptools ambiguity instead of filtering the warnings from logs.

## Validation scope

The source, sdist, and source-ZIP suites currently contain 166 tests. The release pipeline additionally verifies strict schemas, active extractor registration, package resources, wheel `RECORD`, source path/mode/byte identity, deterministic distribution rebuilding, machine-container closure, complete checksums, aggregate bundle bytes, and post-publication identity.

No real authentication tokens or private History/Notes contents are captured. Cryptographic signing and live sanitized backend conformance remain separate future proof dimensions.
