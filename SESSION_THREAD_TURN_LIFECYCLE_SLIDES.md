# Session, thread, turn, and item lifecycle

Slide-format technical notes.

Public-source snapshot: **`openai/codex@455318c2020d75ae7d66d6ccf19defda97edec34`**

Scope: public Codex core and app-server protocol. These slides explain identity,
ownership, and lifecycle. For exact request/response fields, use the generated
app-server JSON schemas and SDK types from the same source revision.

---

## Slide 1 — The hierarchy

```text
session tree S
│
├── thread T0  (root)
│   ├── turn U0
│   │   ├── item I0
│   │   └── item I1
│   └── turn U1
│       └── item I2
│
└── thread T1  (descendant agent)
    └── turn U2
        ├── item I3
        └── item I4
```

The identities are nested for reasoning purposes, but they are not aliases:

```text
session_id != thread_id != turn_id != item_id
```

The root thread is a member of the session tree, not a separate object above
the tree.

---

## Slide 2 — Why the core type name can confuse readers

Public core source calls the loaded runtime object `Session`.

That object:

- owns one concrete `thread_id`;
- has at most one running task at a time;
- owns thread state, active-turn state, tools, services, and event delivery;
- exposes a `session_id` shared with descendant agent threads.

So use this translation when reading source:

```text
core Session
    = live runtime for one thread

Session.session_id
    = root/descendant tree identity
```

For a root runtime, `session_id` is the root thread's ID. Descendant agent
runtimes receive the same tree identity through their shared `AgentControl`.

---

## Slide 3 — Identity ledger

| Identity | Scope | Stability and role |
|---|---|---|
| `sessionId` | Root thread plus descendant agent threads | Tree-level identity; equal to the root thread ID |
| `thread.id` | One durable or ephemeral thread | Concrete thread identity; Codex-generated IDs are UUIDv7 |
| `turn.id` | One execution interval in a thread | Codex-generated IDs are UUIDv7 |
| item identity | One message/tool/reasoning/activity item | Item-type-specific identity used for reducer/event correlation |
| `parentThreadId` | Subagent control topology | Immediate spawn/control parent |
| `forkedFromId` | History topology | Immediate thread whose history seeded the new thread |

Never infer parentage from equal IDs or infer copied history from
`parentThreadId` alone.

---

## Slide 4 — Live runtime versus stored thread

```text
ThreadManager
├── loaded map: thread_id -> CodexThread -> core Session
└── ThreadStore
    └── durable metadata, history, settings, and indexes
```

A thread can be durable without being loaded. A loaded runtime can be removed
or evicted while its persisted thread remains resumable.

An ephemeral thread is different: it is intentionally not materialized as a
normal durable rollout.

Client rule:

```text
"not loaded" does not mean "does not exist"
```

---

## Slide 5 — Start, resume, read, and list are different operations

```text
thread/start
    create a new live thread and its initial settings

thread/resume
    rejoin a running thread or reconstruct a stored thread into a live runtime

thread/read
    read a stored/live thread representation, optionally including turns

thread/list
    page through thread summaries and filters
```

A `Thread` payload normally does not carry the entire conversation. The
`turns` array is populated only on selected read/resume/rollback/fork responses
when turns are requested; other responses and notifications return it empty.

---

## Slide 6 — Thread settings and turn settings

Thread-owned settings describe defaults inherited by later turns.

```text
thread/start
thread/settings/update
        ↓
future turn defaults
```

Many `turn/start` overrides update both the new turn and subsequent thread
defaults. In contrast, `turn/settings/update` publishes changes for later
captures inside the currently running turn:

```text
already captured step        unchanged
later capture in active turn may observe update
future thread settings       not automatically rewritten by that RPC
```

Keep “turn-local publication” separate from “thread-default mutation.”

---

## Slide 7 — Turn lifecycle

```text
turn/start
    ↓
InProgress
    ├── Completed
    ├── Interrupted
    └── Failed
```

A turn belongs to exactly one thread. Its payload can include:

- input and output messages;
- reasoning and plan items;
- tool calls and outputs;
- file/command activity;
- subagent activity;
- timing and error state.

`turn/interrupt` addresses both `threadId` and `turnId`.

---

## Slide 8 — Steering is an active-turn operation

`turn/steer` is not a new thread and not necessarily a new turn.

The request carries an `expectedTurnId` precondition:

```text
client's expected active turn
          ==
server's current active turn
```

If the identities do not match, the request fails rather than steering an
unexpected execution. Event consumers should therefore key turn state by
`threadId + turnId`, not by “most recent message” alone.

---

## Slide 9 — Item detail is explicit

A turn has an `itemsView` describing how much of its item list is present:

```text
NotLoaded  -> items intentionally empty
Summary    -> display-oriented subset
Full       -> all persisted app-server items available for that turn
```

Turn status and item completeness are independent dimensions:

```text
Completed + Summary    is valid
Completed + NotLoaded  is valid
InProgress + partial event stream is valid
```

Do not interpret an empty `items` array as an empty turn without checking
`itemsView`.

---

## Slide 10 — Legacy and paginated history modes

```text
ThreadHistoryMode::Legacy
ThreadHistoryMode::Paginated
```

This is a persistence/hydration contract, not an app-server protocol version
and not a model capability.

Legacy paths may hydrate a broad history object. Paginated paths expect clients
to page turns and items separately, which keeps startup and navigation bounded.

---

## Slide 11 — Paginated hydration

Recommended paginated bootstrap:

```text
thread/resume
  excludeTurns = true
  optional initialTurnsPage
        ↓
thread/turns/list
  cursor + limit + sortDirection + itemsView
        ↓
thread/items/list
  cursor + limit + optional turn filter
```

Responses use opaque cursors. Treat them as server-owned continuation tokens,
not offsets that clients may construct or edit.

Resume can return:

```text
initialTurnsPage
turnsBackwardsCursor
itemsBackwardsCursor
```

Those cursors allow a client to hydrate older history while continuing to
receive live updates.

---

## Slide 12 — Event-reducer model

A robust client reducer tracks independent keys:

```text
threadId
  └── turnId
      └── itemId / callId / activity identity
```

Useful state dimensions include:

```text
thread loaded/stored status
turn lifecycle status
turn item-detail view
live item start/delta/completion
pagination cursor state
subscription state
```

Appending every notification to one flat transcript loses lifecycle and
replacement semantics.

---

## Slide 13 — Common modeling errors

Avoid these equations:

```text
core Session == entire user account session
Thread == fully hydrated transcript
Completed turn == full items loaded
resume == fork
parentThreadId == forkedFromId
paginated == remote-only
empty items == no activity
```

The safer model is:

```text
tree identity
+ concrete thread identity
+ turn execution identity
+ item identity
+ explicit hydration state
```

---

## Slide 14 — Source map

```text
codex-rs/protocol/src/session_id.rs
    SessionId representation and ThreadId conversion

codex-rs/core/src/session/session.rs
    live Session ownership, thread/session identity, persistence initialization

codex-rs/core/src/thread_manager.rs
    loaded-thread registry, start/resume/fork lifecycle

codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs
    Thread, Turn, ThreadHistoryMode, ThreadSource, TurnItemsView

codex-rs/app-server-protocol/src/protocol/v2/thread.rs
    start/resume/fork/list and pagination contracts

codex-rs/app-server-protocol/src/protocol/v2/turn.rs
    turn start, steer, settings update, interrupt, and status
```
