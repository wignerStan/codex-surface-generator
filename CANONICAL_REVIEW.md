# Canonical representation review — 2026-09-09

## Reviewed baseline

Repository: `wignerStan/codex-surface-generator`.
Default-branch baseline: `3162092be72905b1bf1b29a8f0e5a88619493ef0`.
Toolchain tree: `0207dc2ff850cd5f2df9514c451a6620712931ae`.

The earlier assertion that a full `codex-system-contract/v1` implementation had
already been published on `main` was not supported by this tree. It contained a
recovery bootstrap and write-enabled proof/finalizer chain, not the claimed
canonical package/schema. Proposed publisher branches shared the unchanged
toolchain tree and had no published root `contracts` directory. Candidate-proof
run `34331477293` failed without starting a job; it proves no test or release
closure success.

The proposed publisher constructed a small hard-coded narrative graph instead
of real extractor data. Compatibility digests hashed descriptors rather than
generated payloads. Its second storage splitter compressed oversized statements
and executed them in shared globals, hiding complexity instead of separating
responsibilities. Those proposals are not used.

## Changes in this review

The source-derived aggregate owns migrated facts and deterministically projects
actual compatibility payloads. Its offline checker validates structure, references,
coverage declarations and integrity, with optional exact source-byte revalidation.
The existing hybrid boundary remains explicit rather than being relabeled complete.

The 818-line storage extractor is separated into ordinary modules for source
specs, parsing, layout, lifecycle and orchestration. Local fixture output was
byte-identical across the split. No compressed runtime code or `exec` is used.
Canonical serialization rejects lossy key coercion, unsupported JSON values,
duplicate keys, normalization collisions, non-finite numbers and invalid UTF-8.
Asset-check cleanup no longer masks a failing generator or diff. The stale
registry assets are regenerated, including local-storage source entries.

Read-only proof CI replaces recovery/self-publication and retains source tests,
deterministic assets, maintainability, coverage, pinned integration and wheel/sdist
release verification. Package capability checks include canonical resources and
new storage modules. Superseded main-tree recovery helpers are removed; unrelated
branches and history are preserved.

## Acceptance and remaining boundary

Local tests do not substitute for exact-candidate CI. Consult actual run and step
conclusions; a checked-in workflow is not evidence that it passed. The pinned
integration target remains `6af345407d9c2a568da9d01b6c4b81a9e61495c0`.
No force-push or automatic rewrite of `main` is part of this review.

The result is canonical for **migrated semantic extractors**, not all legacy wire
sections, all environment/configuration resolution behavior, runtime execution,
current upstream main, Desktop-private storage or architecture prose. Those
remain explicit migration or evidence work, not inferred successes.

See [the contract guide](contracts/codex-system-contract/v1/README.md) for editable
sources, regeneration commands, compatibility boundaries and evolution rules.
