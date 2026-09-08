# Codex local storage and rollout layout

Source identity for this contract:

```text
openai/codex@6af345407d9c2a568da9d01b6c4b81a9e61495c0
```

This document explains the concrete local persistence model represented by the
machine-readable `extractor.local_storage` contract. It is intentionally scoped
to public Codex source. It does not describe Desktop-private databases, remote
thread stores, or every unrelated file under `CODEX_HOME`.

## Two independently configurable roots

Codex resolves a general storage root and a SQLite root:

```text
CODEX_HOME
  environment: $CODEX_HOME
  default:     ~/.codex

SQLITE_HOME
  config:      sqlite_home
  environment: $CODEX_SQLITE_HOME
  fallback:    $CODEX_HOME
```

The roots commonly resolve to the same directory, but consumers must not assume
that they are identical. Rollout files and thread sidecars live below
`CODEX_HOME`; the six runtime databases live below `SQLITE_HOME`.

## Concrete layout

```text
$CODEX_HOME/
├── sessions/
│   └── YYYY/MM/DD/
│       ├── rollout-<timestamp>-<thread_id>.jsonl[.zst]
│       └── rollout-<timestamp>-<thread_id>_<rollout_id>.jsonl[.zst]
├── archived_sessions/
│   └── rollout-<timestamp>-<thread_id>[_<rollout_id>].jsonl[.zst]
├── session_index.jsonl
├── thread-writer-locks/
│   ├── .coordination.lock
│   └── <thread_id>.lock
├── shell_snapshots/
│   └── <thread_id>.<nonce>.<sh|ps1>
├── visualizations/
│   └── YYYY/MM/DD/<thread_id>/<artifact>.html
└── visualization-viewers/
    └── <thread_id>/<artifact_thread_id>/...

$SQLITE_HOME/
├── state_5.sqlite
├── logs_2.sqlite
├── goals_1.sqlite
├── memories_1.sqlite
├── queue_1.sqlite
└── thread_history_1.sqlite
```

Active rollouts are date-partitioned using local time at rollout creation.
Archived rollouts are moved into a flat directory while preserving their
basenames. A rollout can be represented as plain JSONL or a compressed
`.jsonl.zst` sibling without changing its rollout identity.

## Identity domains

| Identity | Meaning | Persisted authority | Filename role |
|---|---|---|---|
| `session_id` | Session/tree identity shared by a root thread and its agent descendants | First rollout `session_meta.payload.session_id` | None |
| `thread_id` | Stable logical thread identity | `session_meta.payload.id`; `state_5.sqlite.threads.id` | First UUID |
| `rollout_id` | Immutable physical rollout revision identity | Filename parser/result and rollout-lineage references | First UUID for an ordinary rollout; suffix UUID after `_` for a replacement |

For an ordinary rollout, `rollout_id == thread_id` and the basename contains one
UUID. A replacement rollout preserves the first UUID and adds a distinct
rollout UUID after an underscore:

```text
rollout-2026-09-07T10-30-00-A.jsonl
                                 └─ thread_id A; rollout_id A

rollout-2026-09-07T10-45-00-A_B.jsonl
                                 ├─ thread_id A
                                 └─ rollout_id B
```

The underscore is a physical rollout-revision marker. It is not a
`forked_from_id` edge, not a session identifier, and not a second logical thread.

## Authority model

The local persistence layers have different responsibilities:

| Layer | Role |
|---|---|
| Rollout JSONL | Canonical durable replay history and first-record session metadata |
| `state_5.sqlite` | Mutable thread metadata, lookup data, archive state, and current rollout-path pointer |
| `thread_history_1.sqlite` | Derived paginated projection with rollout byte/ordinal positions |
| `session_index.jsonl` | Append-only display-name index; newest entry wins |
| Lock/snapshot/visualization paths | Thread-keyed coordination or ephemeral artifacts, not canonical history |

The initial `threads` table is keyed by `id`, which is the stable `thread_id`.
It stores `rollout_path`, but it does not contain a `session_id` column in the
pinned initial migration. The authoritative `session_id` is therefore read from
rollout session metadata, not inferred from SQLite.

## Lifecycle transitions

### Create

A new logical thread gets a new `thread_id`. Unless a caller supplies a shared
root session identity for an agent descendant, `session_id` defaults to the
thread identity. Its first ordinary rollout uses `rollout_id == thread_id`.

### Resume

Resume resolves and reopens the current rollout path. It preserves
`session_id`, `thread_id`, and the current `rollout_id`; it does not introduce an
underscore suffix merely because a thread was reopened.

### Revert

An unloaded paginated-thread revert is a stable-thread, new-rollout operation:

1. Resolve the current rollout and read its session metadata.
2. Preserve `session_id`, `thread_id`, `forked_from_id`, and
   `parent_thread_id`.
3. Generate a new `rollout_id` and write a replacement `A_B` rollout.
4. Keep old rollouts intact so a retained prefix can be referenced through
   `history_base`.
5. Compare-and-swap the `threads.rollout_path` pointer:

   ```sql
   UPDATE threads
   SET rollout_path = ?
   WHERE id = ? AND rollout_path = ?
   ```

6. If the pointer changed concurrently, delete the unpublished replacement and
   report a conflict.

No SQLite thread row is created for replacement rollout ID `B`.

### Fork

A fork creates a new logical `thread_id`; it is not equivalent to revert. A
paginated fork may reference an immutable source prefix through a
`HistoryPosition` containing a source `rollout_id`, an exclusive ordinal, and a
byte offset. Agent descendants can share the root `session_id` while retaining
independent thread identities.

### Archive and unarchive

Archive moves every owned rollout representation from date-partitioned
`sessions/` into flat `archived_sessions/`, preserving each basename, and then
updates state metadata. Unarchive derives the original `YYYY/MM/DD` partition
from the rollout filename timestamp, moves the files back, and updates the
current path and archived state.

### Delete

Delete first scans rollout references. It rejects a deletion when history
outside the deletion set still references one of the target rollouts. Once safe,
it removes owned active/archived representations, thread-history projections,
and thread-name index entries.

## Sidecar keying

Writer locks, shell snapshots, and visualization roots follow `thread_id`, not
`rollout_id`. A revert can rotate the current rollout without changing these
thread-scoped paths. The shell snapshot implementation names its parameter
`session_id`, but its type is `ThreadId` and it is used as the thread identifier
for lookup, telemetry, and filenames.

## Consumer invariants

Consumers of the full schema should enforce these rules:

1. Parse rollout basenames before deriving identity. Do not split on the final
   UUID alone.
2. Treat `threads.id` as `thread_id` and `threads.rollout_path` as a mutable
   pointer to the currently selected physical revision.
3. Read `session_id` from rollout session metadata; do not synthesize it from a
   rollout suffix or assume a SQLite column.
4. Treat plain and compressed rollout siblings as two representations of one
   physical revision.
5. Preserve referenced rollout ancestors until no `history_base` relationship
   depends on them.
6. Keep fork lineage (`forked_from_id`), spawn/control parentage
   (`parent_thread_id`), and rollout replacement identity separate.

## Machine-readable contract

Generated reports expose the contract in two places:

```text
report.local_storage_schema
report.evolution_contract.extractors["extractor.local_storage"].data
```

The dedicated validation schema is packaged at:

```text
toolchain/codex_wire_audit/proof_schema_templates/
  local-storage-semantics-v1.schema.json
```

The extractor is source-derived and fail-visible: missing source files, changed
filename parsing, renamed database constants, altered pointer cutovers, or
changed sidecar layouts emit structured diagnostics instead of silently
retaining this prose as truth.

## Public-source anchors

```text
codex-rs/utils/home-dir/src/lib.rs
codex-rs/config/src/config_toml.rs
codex-rs/thread-store/src/types.rs
codex-rs/rollout/src/lib.rs
codex-rs/rollout/src/rollout_file_name.rs
codex-rs/rollout/src/recorder.rs
codex-rs/rollout/src/compression.rs
codex-rs/rollout/src/session_index.rs
codex-rs/thread-store/src/local/revert_thread.rs
codex-rs/thread-store/src/local/paginated_fork.rs
codex-rs/thread-store/src/local/archive_thread.rs
codex-rs/thread-store/src/local/unarchive_thread.rs
codex-rs/thread-store/src/local/delete_thread.rs
codex-rs/thread-store/src/local/writer_lock.rs
codex-rs/thread-store/src/local/thread_history_materialization.rs
codex-rs/state/src/sqlite.rs
codex-rs/state/src/runtime/threads.rs
codex-rs/state/migrations/0001_threads.sql
codex-rs/core/src/shell_snapshot.rs
codex-rs/tui/src/inline_visualization.rs
```
