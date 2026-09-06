# Codex wire audit v18 — release-pipeline hardening

v18 preserves the active Context Management and metadata-history proof introduced in the preceding releases, but changes the ownership boundary of the release itself. A release is no longer accepted because a source tree, a wheel, and several companion JSON files happen to exist together. The active pipeline must prove that they all derive from one frozen source snapshot and one package-owned release specification.

The reviewed Codex protocol baseline remains commit `6af345407d9c2a568da9d01b6c4b81a9e61495c0`. The package version is `11.0.0`.

## Authoritative commands

Install the constrained release/test toolchain first:

```bash
python -m pip install --constraint ci/constraints.txt -e '.[test]'
```

Build, validate, assemble, and publish one closed release:

```bash
python tools/release_pipeline.py release \
  --output-dir release_v18
```

Existing output is never replaced implicitly:

```bash
python tools/release_pipeline.py release \
  --output-dir release_v18 \
  --replace
```

Independently reinstall, retest, and rebuild every source-bearing artifact:

```bash
python tools/release_pipeline.py verify \
  --release-dir release_v18
```

`verify` is deep by default. `--fast` performs integrity, topology, provenance, dependency, archive, and byte-closure checks without repeating the installations and rebuilds.

## What the pipeline proves

The package-owned `release_spec.v1.json`, validated against a strict Draft 2020-12 schema, binds:

- release and package identity;
- exact runtime dependency requirements;
- expected wheel and sdist names;
- active capability claims and extractor registration;
- required package resources and coverage profiles;
- the machine-assets member set;
- legacy-evidence policy;
- source-snapshot exclusions.

A release is constructed from a frozen path/mode/byte snapshot. Two independent clean source copies must produce byte-identical wheels and source distributions. The wheel is then installed without dependencies in an isolated environment and asked to report the capabilities and resources it actually contains. Full JSON Schema validation runs during the build; the dependency-free installed probe uses a closed-shape standard-library validator and reports that backend explicitly.

Deep verification additionally:

1. reinstalls the released wheel;
2. reruns its package capability probe;
3. extracts and tests the released sdist;
4. rebuilds the wheel from that sdist and compares exact bytes;
5. extracts and tests the released source ZIP;
6. rebuilds both distributions from the source ZIP and compares exact bytes;
7. compares the fresh result with deep evidence already stored in the integrity-bound validation record.

The standalone Context Management container is generated from the verified wheel's packaged resource bytes. It cannot be substituted by a separately generated companion file.

## Publication and failure behavior

The publisher rejects the source root, its ancestors, unsafe in-tree destinations, symlinked path components, unsafe lock files, and implicit replacement. It uses an operating-system advisory lock, so forced process termination does not permanently strand the release path. Publication is staged, compared by a recursive path/mode/size/hash manifest, atomically swapped, and rolled back when replacement fails.

Failures write bounded, secret-redacted JSON and text evidence beside the requested destination. Subprocesses run with an allowlisted environment, isolated HOME/cache paths, disabled network package lookup, deterministic hash/time settings, and no inherited token or proxy credentials.

## Determinism and archive policy

Validation logs normalize transient workspace paths, build temporary names, and elapsed test durations. Independent release invocations from the same source and toolchain therefore produce the same complete artifact directory, not merely matching distributions inside one process.

ZIP and tar verification rejects duplicate names, Unicode/case-fold aliases, traversal and platform-ambiguous paths, links and special files, encrypted members, excessive sizes or compression ratios, and non-canonical permission bits. Wheels must be pure `py3-none-any`, contain no bytecode, close over every member in `RECORD`, carry the exact release specification, and declare the runtime dependencies bound by that specification.

## Maintained boundaries

This is still a hybrid protocol audit: Context Management and turn metadata are active canonical extractors, while several older protocol families remain behind the preserved compatibility boundary. The release does not claim live credential-bearing calls, cryptographically signed provenance, or successful execution on operating systems not represented by the local run. CI contains the broader platform and current-main checks.
