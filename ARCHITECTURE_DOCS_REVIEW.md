# Architecture documentation review

Review date: **2026-09-06**

This file records what was checked, which source identity supports each class of
claim, and which boundaries remain outside public-source proof. It is a review
ledger, not a replacement for the machine-readable contracts.

## Review identities

```text
Repository head before this review
  wignerStan/codex-surface-generator@eac015ed0036b626444f839584b0744d9fb58fc8

Verified implementation ancestor
  5c18e40ebe475fee547f8bc422fe9b56e3caa039
  feat: connect config schema to the canonical surface graph

Verified documentation/workflow ancestor
  09d5ab54da9120894605771ae7417bdb27f58f2e
  docs: explain config surface architecture and install proof workflow

Generator integration baseline
  openai/codex@6af345407d9c2a568da9d01b6c4b81a9e61495c0

Existing public-source architecture snapshot
  openai/codex@52e12e0cb506e7bb2c9e406fc84d922e274a0e40

Current upstream main checked during this review
  openai/codex@455318c2020d75ae7d66d6ccf19defda97edec34
```

The two upstream commits between the existing architecture snapshot and the
current-main review point changed Bazel/build metadata and app-server daemon
shutdown transport. They did not modify the prompt assembly, feature registry,
tool exposure, hosted Apps/plugin, session/thread/turn protocol, or agent-control
source anchors used by the active architecture notes.

That comparison supports retaining the existing `52e12e...` snapshot labels.
It does not justify silently relabeling those notes as generated from
`455318c...`.

## Evidence classes

| Class | What it can support | What it cannot support by itself |
|---|---|---|
| Generated machine evidence | Exact parsed fields, paths, defaults, constraints, hashes, diagnostics, and typed graph links for its pinned source | Behavior not represented by the selected extractors or runtime profile |
| Public Codex source | Public implementation, protocol types, source-owned defaults, and control flow at an exact commit | Desktop-private host behavior or unpublished backend implementation |
| Shipped Desktop bundle | Code and configuration distributed in a reviewed application bundle | Server-side behavior not present in the bundle |
| Direct observation | A captured process, request, or response under recorded conditions | A universal contract across versions, platforms, accounts, or rollout cohorts |
| Inference | A clearly marked conclusion connecting multiple supported facts | A substitute for missing source or machine evidence |

## Active document status

| Document | Primary evidence | Review status and boundary |
|---|---|---|
| [`toolchain/CONFIG_SURFACE_ARCHITECTURE.md`](toolchain/CONFIG_SURFACE_ARCHITECTURE.md) | Generator baseline plus v19 implementation | Design and ownership model reviewed. Exact setting inventory remains in generated JSON. |
| [`toolchain/README_CODEX_WIRE_AUDIT_V19.md`](toolchain/README_CODEX_WIRE_AUDIT_V19.md) | v19 package and pinned integration workflow | Operational/proof boundary reviewed. It does not claim that the checked-in `release/` directory is already v19. |
| [`PROMPT_ASSEMBLY_AND_CONFIG.md`](PROMPT_ASSEMBLY_AND_CONFIG.md) | Public source at `52e12e...`, with labeled Desktop observations | Relevant public-source anchors were unchanged through `455318c...`. Desktop-specific statements remain observation-scoped. |
| [`CODE_MODE_TOOL_ARCHITECTURE.md`](CODE_MODE_TOOL_ARCHITECTURE.md) | Public source at `52e12e...` | Relevant tool-mode and exposure anchors were unchanged through `455318c...`. |
| [`CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md`](CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md) | Public source at `52e12e...` | Relevant hosted service/plugin/Apps anchors were unchanged through `455318c...`. |
| [`DESKTOP_ARCHITECTURE.md`](DESKTOP_ARCHITECTURE.md) | Mixed public source, shipped-bundle evidence, and direct observation | Evidence classes are explicitly separated. Private Desktop host internals are not promoted to public-source facts. |
| [`SESSION_THREAD_TURN_LIFECYCLE_SLIDES.md`](SESSION_THREAD_TURN_LIFECYCLE_SLIDES.md) | Public source at `455318c...` | Conceptual lifecycle model; exact RPC fields belong to generated app-server schemas. |
| [`FORK_PAGINATION_AND_AGENT_TOPOLOGY_SLIDES.md`](FORK_PAGINATION_AND_AGENT_TOPOLOGY_SLIDES.md) | Public source at `455318c...` | Conceptual topology/persistence model; implementation details are snapshot-bound. |

## Findings corrected in this review

### Source line is not the published release bundle

The package source is v19 / 12.0.0, but the checked-in `release/` directory
contains v18 / 11.0.0 artifacts. The root README now states both facts
explicitly. A source migration and passing CI do not by themselves publish a
new release bundle.

### A session, a thread, and a turn are different identities

In current public core source, a `Session` is the loaded runtime for one
concrete thread. Its `session_id` is the identity shared by the root thread and
its descendant agent threads. A turn is one execution interval inside one
thread. The new lifecycle slides keep those identities separate.

### Parentage and history lineage are different graphs

`parentThreadId` records the control/spawn parent for a subagent.
`forkedFromId` records the immediate history source. A forked subagent commonly
sets both to the same thread, while a fresh child can have a parent without
copying parent history. The new topology slides model both edges.

### Pagination is a hydration contract

`ThreadHistoryMode::Paginated` does not mean “all turns are already embedded in
the `Thread` object.” Paginated clients should use thread, turn, and item list
APIs with opaque cursors and an explicit item-detail view. Resume/fork options
can deliberately exclude turns and return bootstrap/backward cursors.

## What “reviewed” means

The review checked repository commit ancestry, active documentation links,
source pins, the published release directory, the generated package version,
and the public-source anchors behind the new lifecycle/topology notes. It also
compared the existing architecture snapshot with current upstream `main`.

It does **not** claim that prose can never drift. The maintenance rule remains:

```text
architecture prose explains design and boundaries
machine-readable artifacts define exact current fields for their source pin
CI proves reproducibility and detects selected classes of drift
```

## Public-source anchors for the new slide notes

```text
codex-rs/protocol/src/session_id.rs
codex-rs/core/src/session/session.rs
codex-rs/core/src/session/mod.rs
codex-rs/core/src/thread_manager.rs
codex-rs/core/src/agent/control.rs
codex-rs/core/src/agent/control/spawn.rs
codex-rs/app-server-protocol/src/protocol/v2/thread.rs
codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs
codex-rs/app-server-protocol/src/protocol/v2/turn.rs
codex-rs/app-server/src/request_processors/thread_processor.rs
```
