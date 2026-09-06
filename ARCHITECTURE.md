# Codex runtime / Desktop architecture notes

Research snapshot: **2026-09-06**.

This document records implementation-level architecture observed while auditing current Codex and the unified ChatGPT Desktop app. It is a research note, not a generated schema contract. Keep three evidence classes separate:

- **Public source** — implementation visible in `openai/codex`.
- **Shipped Desktop bundle** — code/config observable inside `ChatGPT.app`, but not necessarily published as open source.
- **Desktop host/private behavior** — behavior behind the native Desktop bridge; only claim details directly supported by bundle behavior or observed product behavior.

The most important rule is:

```text
native subagents / multi-agent
    != durable Codex threads/tasks
    != ordinary ChatGPT conversations
```

Likewise:

```text
codex_tui   != codex_app   != codex_apps
```

## 1. Core roles

`codex app-server` is the Codex backend/server used by rich clients. TUI and Desktop are clients of it.

```text
TUI / Desktop
     |
     | AppServerClient / JSON-RPC
     v
codex app-server
     |
     +-- thread/* APIs
     +-- turn/item/history APIs
     +-- execution / approvals / MCP plumbing
     +-- local Codex thread store
```

Public source anchors:

- `codex-rs/app-server/README.md`
- `codex-rs/app-server/src/...`
- `codex-rs/app-server-client/...`
- `codex-rs/tui/src/app_server_session.rs`

Do not describe app-server as the client. The client is the TUI/Desktop side.

## 2. App-server transports

### Embedded TUI

Normal TUI can fall back to an in-process app-server:

```text
TUI -> in-process app-server
```

No Unix socket and no `app-server proxy` are involved.

Current TUI deliberately disables its cross-thread task-tool transport when the app-server client is `InProcess`/embedded. See `codex-rs/tui/src/app_server_session.rs`.

### External/local app-server

TUI can connect directly to an external app-server using Unix-domain sockets:

```text
codex --remote unix://
codex --remote unix:///absolute/path.sock
```

or a WebSocket endpoint:

```text
codex --remote ws://host:port
```

For a Unix endpoint the TUI opens the UDS directly. `codex app-server proxy` is not inserted in this path.

### `codex app-server proxy`

`app-server proxy` is a transport bridge:

```text
stdio <-> running app-server control Unix socket
```

It is useful when a remote controller has stdio (for example through SSH) but cannot directly open the remote Unix socket:

```text
Desktop
   |
   | SSH stdin/stdout
   v
remote `codex app-server proxy`
   |
   | Unix socket
   v
remote app-server
```

It is not an HTTP/model/MCP proxy.

### Direct server vs managed daemon

These are separate launch/lifecycle modes:

```text
codex app-server --listen unix://...     # direct foreground server
codex remote-control                     # foreground/current executable

codex app-server daemon start            # managed daemon lifecycle
codex remote-control start               # managed daemon lifecycle
```

The managed daemon currently expects the standalone installer-managed Codex binary under `CODEX_HOME/packages/standalone/...`. A Homebrew/npm `codex` can still run a direct app-server; the standalone restriction belongs to daemon lifecycle/update management, not app-server itself.

## 3. Multiple app-servers and thread writer ownership

Different socket paths do not conflict by themselves:

```text
app-server A -> /tmp/a.sock
app-server B -> /tmp/b.sock
```

If both use the same `CODEX_HOME`, they still share the same Codex thread store. Writing the same thread concurrently is prevented by a cross-process writer lock.

Current public implementation:

- `codex-rs/thread-store/src/local/writer_lock.rs`
- lock directory: `thread-writer-locks`
- coordination file: `.coordination.lock`
- per-thread file: `<thread-id>.lock`

The `.lock` pathname is not merely a marker. Codex opens the file and uses an OS file lock (`try_lock`-style ownership) held by the live file handle/FD. If the process dies, the kernel releases the lock even if the pathname remains. A later process can acquire the OS lock and clean up stale lock files.

So:

```text
lock file exists != active writer
open handle + OS lock == active writer ownership
```

Within one process there is also process-local synchronization; the OS lock is the cross-process layer.

## 4. TUI durable task/thread tools: `codex_tui`

The public TUI implements durable cross-thread task management in:

- `codex-rs/tui/src/dynamic_tools.rs`
- `codex-rs/tui/src/dynamic_tools_mcp.rs`

Namespace:

```text
codex_tui
```

Current tools include:

```text
list_threads
list_archived_threads
read_thread
wait_threads
create_thread
fork_thread
send_message_to_thread
set_thread_title
set_thread_archived
```

This is full Rust business logic, not only schemas. For example:

```text
codex_tui.read_thread
    -> app-server thread/read
    -> thread/turns/list (or compatibility fallback)
    -> normalized / bounded model-facing response
```

and:

```text
codex_tui.create_thread
    -> inspect source thread
    -> inherit cwd/project/model/permissions/sandbox
    -> thread/start
    -> optional thread/name
    -> turn/start
```

These are durable Codex tasks, not native multi-agent subagents.

### TUI `@` task references

`MentionsV2` provides the unified `@` picker, including Codex task candidates. Task search is additionally gated by task-tool availability, so files may appear in `@` while previous tasks do not.

A selected task is represented as a live thread reference (`thread://<ThreadId>` internally) and the prompt tells the model to call `read_thread` before relying on the referenced task. The reference itself is not an inference-server-side history dereference.

The embedded app-server case is important: TUI task tools are disabled there. Connecting to an external/local app-server enables the separate task-tool transport path.

## 5. Native multi-agent is separate

Native subagents use the multi-agent namespaces/tools (for example `spawn_agent`, `send_input`, `wait_agent`, `resume_agent`, `close_agent`). Their lifetime/ownership is delegation inside an agent/thread hierarchy.

Durable peer Codex threads are a different abstraction. Do not use “multi-agent” as a synonym for TUI/Desktop thread orchestration.

Public source anchor:

- `codex-rs/core/src/tools/handlers/multi_agents_spec.rs`

## 6. Desktop `codex_app`: injected MCP bridge

The unified ChatGPT Desktop app launches the bundled Codex app-server with an injected MCP server named:

```text
mcp_servers.codex_app
```

Observed shipped manifest (`desktop-mcp.json`) configures approximately:

```text
command = ./scripts/launch_codex_app_tools_mcp
args    = [./server.mjs]
env     includes CODEX_APP_TOOLS_PIPE_PATH
```

with default tool approval `approve` and selected mutations such as `create_thread`, `send_message_to_thread`, `fork_thread`, `handoff_thread`, and `automation_update` overridden to `prompt`.

This means `codex_app` is not compiled-in special app-server business logic. From Codex's point of view it is an externally launched MCP server.

```text
Codex app-server/runtime
        |
        | MCP over stdio
        v
codex-app-tools `server.mjs`
        |
        | native pipe
        v
ChatGPT Desktop host
```

## 7. What shipped `server.mjs` actually does

The shipped bundle preserves source-path comments showing its OpenAI-owned portion originated from roughly:

```text
../protocol/src/dynamic-app-tools.ts
../bundled-plugins/codex-app-tools/src/server.ts
```

The 25k-line `server.mjs` is mostly bundled dependencies (MCP SDK, Zod, AJV, etc.). The application-specific code is near the end.

### Tool catalog is host-owned

`server.mjs` does not hard-code `read_thread`, `list_threads`, `create_thread`, `handoff_thread`, etc. Instead its MCP `tools/list` handler calls the Desktop native host:

```text
native RPC: tools/list
params: { threadStartKind: "all" | "default" }
```

It validates the returned tool objects, caches them by name, removes the private `namespace` property, and exposes the rest as normal MCP tools.

Therefore the Desktop host owns the dynamic tool catalog and each tool's schema/namespace.

### Tool calls are forwarded

For MCP `tools/call`, `server.mjs`:

1. resolves the tool from the cached host catalog;
2. extracts/normalizes thread/turn/call IDs from MCP metadata;
3. restores the host-private tool namespace;
4. sends native RPC `tools/call` with `{arguments, callId, namespace, threadId, tool, turnId}`;
5. converts the host's `inputText` / `inputImage` / `inputAudio` result into MCP content.

There is no `if (tool === "read_thread")` implementation in this bundle. The tool-specific semantics are behind the native host RPC.

### Native pipe wire format

`CODEX_APP_TOOLS_PIPE_PATH` points to a Unix socket / named-pipe endpoint opened with Node `net.createConnection()`.

Framing is:

```text
4-byte unsigned little-endian payload length
+ UTF-8 JSON-RPC 2.0 payload
```

Maximum frame size observed in the bundle: **8 MiB**.

Native methods used by this proxy include at least:

```text
tools/list
tools/call
tools/cancel
```

Cancellation sends `tools/cancel` over the same pipe when the outer MCP request is aborted.

This is not MCP on the native pipe; it is a small length-prefixed JSON-RPC protocol used between `server.mjs` and the Desktop host.

## 8. Desktop host boundary

The tool catalog and tool-specific execution are not implemented in the shipped `server.mjs`. They are provided by the Desktop host behind `CODEX_APP_TOOLS_PIPE_PATH`.

Confirmed from the bundle:

```text
server.mjs asks host for tools/list
server.mjs forwards tools/call
server.mjs forwards tools/cancel
```

Do not claim private host implementation details beyond evidence. Product behavior/issues indicate that the host can route resources across local Codex threads, configured remote/SSH Codex hosts, and ordinary ChatGPT conversations, but the exact host code is not present in the public `openai/codex` repository or in this MCP proxy bundle.

A useful conceptual router is:

```text
codex_app.read_thread(resource)
        |
        v
Desktop host source/router
      /                  \
 kind=codex           kind=chatgpt
    |                     |
app-server             ChatGPT-side
thread APIs             conversation service
```

Treat the right-hand implementation as Desktop-private unless independently confirmed.

## 9. Ordinary ChatGPT conversations are not app-server threads

Desktop can surface references to normal ChatGPT conversations (observed `chatgpt-conversation://...` style resources). These are not `thread://` Codex task references and must not be treated as local app-server thread IDs.

Public app-server `thread/read` reads Codex threads. A Desktop bridge may expose a higher-level `codex_app.read_thread` that source-routes ChatGPT resources elsewhere.

Thus:

```text
app-server thread/read != generic ChatGPT history reader
```

## 10. `codex_app` vs `codex_apps`

These names are easy to conflate:

```text
codex_app   # singular: Desktop-injected app-tools MCP bridge
codex_apps  # plural: Codex Apps / Plugin Service MCP path
```

The plural `codex_apps` path is present in public Codex source and uses Codex-owned ChatGPT service configuration. The singular Desktop `codex_app` uses the Desktop native pipe described above.

Do not infer that configuration behavior of one applies to the other. In particular, a Codex `chatgpt_base_url` override is not evidence that the Desktop-host implementation behind singular `codex_app` uses that base URL for ordinary ChatGPT conversation reads.

## 11. Tool exposure: direct vs deferred/tool-search

Public Codex models MCP tool exposure with surfaces including:

```text
Direct    # in initial model-visible tool list
Deferred  # discoverable later through tool_search
CodeMode  # nested tools available to Code Mode scripts
```

`omit_tools_from = ["deferred"]` means tools from that server are omitted from deferred/tool-search exposure. If direct exposure remains, they are presented eagerly instead of being discovered via `tool_search`.

Observed Desktop process arguments added `omit_tools_from=["deferred"]` to `mcp_servers.codex_app`, while the observed original `desktop-mcp.json` manifest did not contain that field. Therefore this setting is injected/merged by another Desktop/plugin configuration layer rather than originating in the raw manifest itself.

Public source anchors:

- `codex-rs/protocol/src/config_types.rs` (`ToolExposureSurface`)
- `codex-rs/config/src/mcp_types.rs` (`omit_tools_from`)
- `codex-rs/core/src/tools/spec_plan.rs` (exposure calculation)

## 12. Evidence / ownership matrix

| Surface | Public `openai/codex` | Shipped Desktop bundle | Desktop host/private |
|---|---:|---:|---:|
| `codex app-server` | yes | bundled binary | — |
| app-server JSON-RPC protocol | yes | consumed | — |
| generic MCP client/config | yes | consumed | — |
| TUI `codex_tui` task tools | yes, full Rust logic | — | — |
| TUI task-tools MCP wrapper | yes | — | — |
| native multi-agent/subagents | yes | — | — |
| `codex_app` manifest | no | yes | injected/managed by Desktop |
| `codex_app server.mjs` proxy | no | yes | connects to host |
| `CODEX_APP_TOOLS_PIPE_PATH` client | no | yes | pipe server is host-owned |
| Desktop `codex_app` tool catalog | no | fetched dynamically | yes |
| Desktop tool-specific semantics | no | not in proxy | yes |
| ordinary ChatGPT conversation backend | no | not implemented in proxy | yes / service-side |

## 13. Short canonical model

```text
                    Unified ChatGPT Desktop

     +---------------- ChatGPT-side services ----------------+
     |                                                        |
     |  ordinary conversations / account / hosted services    |
     |                                                        |
     +--------------------------^-----------------------------+
                                |
                         Desktop host/router
                                ^
                                |
             length-prefixed JSON-RPC native pipe
                                |
                         codex-app-tools
                           server.mjs
                                ^
                                | MCP stdio
                                |
     +--------------------------+-----------------------------+
     |                 Codex runtime side                    |
     |                                                       |
     |  Codex app-server <---- AppServerClient ---- Desktop  |
     |        ^                                              |
     |        | direct UDS / WS                              |
     |       TUI                                             |
     |        |                                              |
     |        +-- public `codex_tui` durable task tools      |
     |                                                       |
     +-------------------------------------------------------+
```

When auditing surfaces, always record **namespace, source kind, ID format, transport, storage backend, and ownership/lifetime**. Similar tool names (`read_thread`, `create_thread`) do not imply the same implementation.