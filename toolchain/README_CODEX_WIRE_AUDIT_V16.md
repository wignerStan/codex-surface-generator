# Codex wire audit v16 — experimental context-management proof

v16 adds a first-class canonical extractor for Codex's experimental context-window
management architecture.  It intentionally separates five concerns that are easy
to conflate:

1. feature/config activation;
2. ChatGPT account and plan eligibility;
3. starting-model capability (`supports_experimental_context`);
4. model-provider transport/routing for History and Notes;
5. fresh-window rollover semantics and durable state retrieval.

The extractor is `extractor.context_management`.  It covers the nine model-visible
History/Notes actions, the hidden `alpha/notes/v2/thread_hint` request, route-specific
encryption headers, provider/auth gating, `new_context`, and token-budget compaction
that starts a fresh window without model/server summarization.

## Important routing distinction

Routing, eligibility, and durable-state semantics for History/Notes are
defined in the upstream Codex sources (`codex-rs/ext/history-notes/`,
`codex-rs/codex-api/src/endpoint/`, `features/src/feature_configs.rs`) and are
not restated here — the generated report and coverage profiles are the
authoritative machine view.

## Durable state model

Direct source semantics:

- History is read-only normalized recovery state and is eventually consistent.
- Notes survive context-window transitions, use virtual agent-scoped paths, allow
  cross-agent access, provide immediate read-after-write, and eventually-consistent
  list/search indexes.
- `thread_hint` is bounded to 4,000 bytes by the client extension.
- Current tests use a small "Recent notes (up to 5, most-recent first)" metadata
  fixture; the server-side hint generation algorithm is not public client code.

The derived architectural interpretation is therefore recorded separately: the
active context window behaves like a disposable working set, while History/Notes
form durable state that can be demand-loaded.

## Profiles

- `context_management_only`: static context-management proof.
- `hybrid_v16`: canonical turn metadata + context management while preserving the
  explicit legacy-reconstruction boundary for other protocol families.
- `codex_wire_full`: now also requires `extractor.context_management` plus runtime
  scenarios for activation, rollover, and History/Notes retrieval.

## Current reviewed upstream revision

The bundled reference container is tied to Codex commit
`6af345407d9c2a568da9d01b6c4b81a9e61495c0`, which added the
`ModelInfo.supports_experimental_context` starting-model capability gate.
