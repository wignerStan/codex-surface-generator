# Fork, pagination, and root/subagent topology

Slide-format technical notes.

Public-source snapshot: **`openai/codex@455318c2020d75ae7d66d6ccf19defda97edec34`**

Scope: public Codex thread manager, agent control, thread store, and app-server
protocol. Exact fields and enum values remain owned by the generated
machine-readable app-server schemas for this source revision.

---

## Slide 1 — There are two graphs

A Codex thread can participate in two independent relationships:

```text
control / spawn graph
    parentThreadId

history lineage graph
    forkedFromId + fork boundary
```

They answer different questions:

```text
Who owns or spawned this child?
Which thread's history seeded this thread?
```

A forked subagent often has both edges to the same parent. That common shape
does not make the fields interchangeable.

---

## Slide 2 — Root tree identity

```text
sessionId = root thread ID

root thread T0
├── child thread T1
│   └── grandchild thread T3
└── child thread T2
```

All descendant agent runtimes created through the root's shared `AgentControl`
use the same `sessionId`. Every thread still has its own `thread.id`.

The protocol does not need a boolean `isMain`. “Main thread” is best modeled as
the root of this control tree:

```text
thread.id == sessionId
```

for the root, while descendants have different thread IDs.

---

## Slide 3 — A user fork can start a new tree

A normal app-server/CLI fork:

```text
source thread A
      │ history snapshot
      ▼
new thread B
```

creates a fresh thread ID. When B is a new root runtime, its `sessionId` is B's
own thread ID, while:

```text
B.forkedFromId = A
B.parentThreadId = null
```

History lineage can therefore cross session-tree boundaries.

This differs from an agent child fork, which remains in the parent's session
tree because it reuses the shared `AgentControl`.

---

## Slide 4 — Fresh child versus forked child

### Fresh subagent

```text
parentThreadId = parent
forkedFromId   = null
```

The child can inherit bounded runtime authority, environment selection, service
tier, and role configuration without inheriting the parent's transcript.

### Forked subagent

```text
parentThreadId = parent
forkedFromId   = parent   [usual direct-parent case]
```

The child receives a filtered model-context snapshot and starts its own turn
with new input.

Control inheritance and history inheritance remain separate decisions.

---

## Slide 5 — Example topology

```text
session S / root T0
│
├── T1 researcher
│   parent = T0
│   forkedFrom = null
│
├── T2 implementer
│   parent = T0
│   forkedFrom = T0
│
│   └── T3 verifier
│       parent = T2
│       forkedFrom = T2
│
└── T4 guardian review
    parent = T0
    threadSource = GuardianReview
```

Outside this tree:

```text
T5 user-created fork
sessionId = T5
forkedFrom = T0
parent = null
```

This is a conceptual example, not a promise that every product surface exposes
all nodes simultaneously.

---

## Slide 6 — AgentControl is tree-scoped

Public source describes `AgentControl` as the multi-agent control-plane handle:

```text
one root AgentControl
    shared with descendants
    registry scoped to that root tree
```

It owns or coordinates:

- tree `sessionId`;
- agent metadata and status;
- thread ID allocation;
- spawn capacity and execution limits;
- inter-agent input/communication;
- V2 residency;
- rollout budget and root service tier.

It is not a global registry for every thread in the process.

---

## Slide 7 — Three classification axes

Do not collapse these fields:

| Axis | Example meaning |
|---|---|
| `SessionSource` | CLI, VS Code, exec, app-server, internal, or subagent runtime origin |
| `ThreadSource` | user, subagent, guardian review, feature-owned, or memory consolidation |
| agent role / nickname / path | functional identity and location inside the agent tree |

A thread can be a subagent in `ThreadSource`, have a role such as reviewer, and
still carry a separate runtime/client origin in `SessionSource`.

---

## Slide 8 — Fork cut controls

App-server fork supports source and boundary selection:

```text
source:
  threadId                      preferred
  path                          unstable; non-empty path takes precedence

boundary:
  lastTurnId                    include this completed turn
  beforeTurnId                  exclude this turn and everything after it
```

`lastTurnId` and `beforeTurnId` cannot be combined. A referenced boundary cannot
be an in-progress turn.

At the lower core layer, snapshot strategies can also represent truncation
before a user-message boundary or an “interrupted now” view of persisted
history.

---

## Slide 9 — Forks do not blindly clone every rollout item

Agent forks retain model-relevant context and intentionally exclude or rewrite
some parent-local state.

Examples include:

```text
keep:
  system/developer/user context
  final assistant answers
  selected configuration and session metadata
  applicable compaction/context checkpoints

drop or reset:
  parent cumulative token usage
  transient tool calls and most outputs
  inter-agent communications
  parent-local review authorization
  stale role/usage hints
```

The exact filter is implementation- and feature-dependent. Treat this slide as
architecture, not a substitute for source or generated evidence.

---

## Slide 10 — Copied and referenced persistence

Internally, a fork can persist inherited history in two ways:

```text
Copied
    child stores a copied inherited prefix

Referenced
    child stores a history_base reference
    ancestor records remain behind that boundary
    child stores local settings and later items
```

Both produce a child thread with its own identity. The distinction is storage
topology, not a different public “kind of thread.”

For paginated copied subagents, inherited context is already persisted when the
live child is created, so startup avoids appending the same inherited prefix a
second time.

---

## Slide 11 — Pagination has three levels

```text
thread/list
    pages thread summaries

thread/turns/list
    pages turns inside one thread

thread/items/list
    pages items, optionally scoped to a turn
```

Each cursor is opaque and belongs to its endpoint/order. Do not reuse a thread
cursor as a turn cursor or derive one cursor by editing another.

Turn pages can choose an item projection:

```text
NotLoaded
Summary
Full
```

so pagination volume and item detail are separate controls.

---

## Slide 12 — Forward and backward hydration

A turn or item page can return:

```text
nextCursor
backwardsCursor
```

The forward cursor continues the selected page order. Backward cursors support
loading earlier history and can deliberately overlap a boundary entity so a
client can reconcile updates.

Clients should deduplicate by stable identity when pages overlap.

---

## Slide 13 — Resume and fork can avoid full hydration

For paginated threads:

```text
thread/resume excludeTurns=true
thread/fork   excludeTurns=true
```

return live thread metadata without embedding the full turn list.

A resume request may ask for an initial turn page. Resume responses can also
include backward cursors for turns and items.

This design separates:

```text
activate runtime
from
hydrate historical UI
```

---

## Slide 14 — Durable graph versus resident runtimes

Multi-agent V2 can restore persisted agent metadata without reopening every
child runtime.

```text
durable agent graph
    may contain open descendants

resident runtime set
    bounded subset currently loaded
```

Idle descendants can be evicted and later reloaded. Some V2 reload paths require
the immediate parent to be loaded so parent-owned configuration, permissions,
environments, and execution policy can be revalidated.

A topology view must therefore distinguish:

```text
known child
loaded child
running child
completed/closed child
```

---

## Slide 15 — Input and completion flow

```text
root turn
   │ spawn request
   ▼
child thread created
   │ initial input / inter-agent communication
   ▼
child turn starts
   │ events and status
   ▼
completion watcher or V2 control path
   │
   └── result/activity returned to parent context
```

A child thread can receive later input through the shared control plane. Sending
input may start a new child turn or steer an active one, depending on the
thread's current state and the selected API.

---

## Slide 16 — Modeling checklist

Record these independently:

```text
sessionId
threadId
parentThreadId
forkedFromId
threadSource
sessionSource
agent role / nickname / path
historyMode
fork boundary
persistence mode
loaded/resident/running status
turnId and turn status
turn/item pagination cursors
itemsView
```

If a diagram has only “main → subagent,” it is hiding information required to
explain resume, fork, pagination, and residency correctly.

---

## Slide 17 — Source map

```text
codex-rs/core/src/agent/control.rs
    tree-scoped AgentControl and shared sessionId

codex-rs/core/src/agent/control/spawn.rs
    fresh/forked child creation, inheritance, role metadata, V2 reload

codex-rs/core/src/thread_manager.rs
    start/resume/fork, copied/reference persistence, loaded thread map

codex-rs/core/src/session/session.rs
codex-rs/core/src/session/mod.rs
    session/thread identity and initial-history persistence

codex-rs/app-server-protocol/src/protocol/v2/thread.rs
    fork boundaries, excludeTurns, turn/item pagination cursors

codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs
    Thread fields, history modes, source classifications, turn item views
```
