# Codex wire audit v16 release notes

## Why this release exists

The supplied v15 delivery contained useful Guardian and alpha History/Notes
companion JSON containers, but the installable source/wheel inside that delivery
still identified itself as package `7.0.0` and did not contain those claimed active
extractors.  v16 stops extending that split-brain pattern for context management:
the new proof is part of the installable package, registered in the canonical
extractor registry, required by dedicated coverage profiles, packaged with strict
schema/reference data, and mutation-tested.

## New canonical domain: `extractor.context_management`

The extractor separates:

- `feature`: `features.context_management.experimental_mode`, feature stage/default,
  and `ModelInfo.supports_experimental_context`;
- `activation`: ChatGPT OAuth mode, Plus/Pro/ProLite eligibility, provider gates,
  TokenBudget activation, and History/Notes extension activation;
- `history_notes`: 10 `alpha/...` routes, 9 model tools, hidden `thread_hint`, POST,
  timeout, context injection, truncation header, encrypted-argument policy, History
  and Notes storage semantics;
- `routing`: `openai_base_url`/model-provider plane versus the separate
  `chatgpt_base_url` auxiliary plane;
- `rollover`: `new_context`, environment preservation, and no-summary manual/auto
  token-budget compaction.

## Upstream drift caught during the release

Current Codex `main` moved again to
`6af345407d9c2a568da9d01b6c4b81a9e61495c0` and added a model capability gate:
experimental context activation now requires the *starting model* to advertise
`supports_experimental_context`.  The field is serde-defaulted to false.

This is now a strict machine invariant.  Removing the gate from a fixture produces
`CONTEXT_MANAGEMENT_MODEL_CAPABILITY_GATE_MISSING`.

## Thread hint correction

v16 does not classify `thread_hint` as a remote compaction summary.  Public tests
show a small recent-note metadata fixture such as:

`Recent notes (up to 5, most-recent first): ...`

The client caps injected hint bytes, but the backend algorithm that selects or
constructs the hint is not present in the public client source.  v16 therefore
records the fixture as evidence and marks the server algorithm opaque.

## Remote Notes interpretation

Source-proven facts are kept distinct from inference.  Notes are remote model-owned
working memory with virtual agent paths, cross-agent access, persistence across
context-window transitions, immediate direct reads after successful writes, and
an eventually-consistent list/search surface.  The conclusion that this forms a
"durable state / disposable working-set" architecture is stored as a derived
architectural interpretation, not as a quoted source fact.

## Maintainability changes

- Source-registry overlays can now add validated new `SourceSpec` IDs instead of
  only editing known IDs.
- 9 new context-management source specs are present; existing config/provider/model
  sources are also tagged for the context extractor.
- A closed, hash-verified 7-member context-management reference container ships as
  package data.
- `coverage_profiles.v3.json` adds `hybrid_v16` and `context_management_only` and
  expands `codex_wire_full`.
- The context extractor is profile-scoped: unrelated narrow fixtures do not become
  incomplete merely because they do not load context-management sources.

## Validation target

The release is expected to pass the complete inherited test suite plus v16 context
management tests, build deterministic wheel/sdist artifacts, and verify the closed
reference container.  Final delivery records the exact observed results rather
than hard-coding them here.
