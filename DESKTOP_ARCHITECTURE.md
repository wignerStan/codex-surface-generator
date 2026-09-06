# ChatGPT Desktop / Codex Desktop architecture

Research snapshot: **2026-09-06**.

This note records the architecture of the **unified ChatGPT Desktop app's Codex surface**. It is intentionally narrower than a general Codex runtime architecture document.

Keep three evidence classes separate:

- **Public Codex source** — implementation visible in `openai/codex`.
- **Shipped ChatGPT.app bundle** — code/config distributed inside the Desktop app, but not necessarily published as open source.
- **Desktop host/private implementation** — behavior behind the Desktop native bridge; only claim details supported by shipped bundle behavior or direct observations.

The most important distinction is:

```text
ChatGPT Desktop shell
    contains a ChatGPT side
    and a Codex side

but:

ChatGPT conversations != Codex threads
ChatGPT backend       != Codex app-server
codex_app             != codex_apps
```

## 1. Top-level Desktop topology

The unified Desktop app hosts both the ordinary ChatGPT product surface and the Codex product surface.

```text
                         ChatGPT Desktop

        +---------------- ChatGPT side ----------------+
        |                                               |
        | ordinary ChatGPT conversations                |
        | account/session/backend services              |
        | Desktop UI / Work / other host capabilities   |
        |                                               |
        +-----------------------+-----------------------+
                                |
                         Desktop host/router
                                |
                   native app-tools pipe server
                                |
                                v
        +---------------- Codex side -------------------+
        |                                               |
        | codex-app-tools `server.mjs`                  |
        |             ^                                 |
        |             | MCP over stdio                  |
        |             v                                 |
        | bundled `codex app-server`                    |
        |             ^                                 |
        |             | AppServerClient                 |
        |             v                                 |
        | Desktop Codex UI                              |
        |                                               |
        +-----------------------------------------------+
```

The Codex side is built around the public/open-source Codex app-server, while Desktop-only capabilities are injected through an app-owned MCP server named `codex_app`.

## 2. How ChatGPT Desktop launches Codex

A current macOS Desktop process has the shape:

```text
/Applications/ChatGPT.app/Contents/Resources/codex
    -c features.code_mode_host=true
    app-server
    --analytics-default-enabled
    -c mcp_servers.codex_app={...}
```

The important point is that Desktop launches the normal Codex `app-server` binary and supplies a **request/process-level MCP override** for `mcp_servers.codex_app`.

So from Codex's perspective:

```text
codex_app == an externally configured MCP server
```

It is not a hidden tool implementation compiled directly into app-server.

## 3. Shipped `desktop-mcp.json`

The bundled `codex-app-tools` plugin ships a manifest equivalent to:

```text
mcpServers.codex_app
    command = ./scripts/launch_codex_app_tools_mcp
    args    = [./server.mjs]
    cwd     = .
    enabled = true

    default_tools_approval_mode = approve

    prompt approval overrides:
        automation_update
        create_thread
        send_message_to_thread
        fork_thread
        handoff_thread

    environment includes:
        CODEX_APP_TOOLS_PIPE_PATH
        CODEX_MCP_NODE_PATH
        CODEX_BROWSER_USE_NODE_PATH
        CODEX_ELECTRON_RESOURCES_PATH
        CODEX_CLI_PATH
        ...
```

The raw shipped manifest does **not** contain `omit_tools_from=["deferred"]`, even though current Desktop process arguments may contain that setting. Therefore that exposure setting is added/merged by another Desktop/plugin configuration layer before app-server starts.

## 4. The launcher script is only a Node resolver

`launch_codex_app_tools_mcp` does not implement Desktop tools. Its job is to find a Node runtime and `exec` the requested JavaScript entrypoint.

Its effective flow is:

```text
launch_codex_app_tools_mcp ./server.mjs
        |
        +-- prefer CODEX_MCP_NODE_PATH
        +-- then browser/electron bundled Node paths
        +-- then cached Codex runtime Node
        +-- finally PATH `node`
        |
        v
exec node ./server.mjs
```

Because it uses `exec`, the shell process is replaced by Node rather than remaining as another long-lived proxy layer.

## 5. What `server.mjs` actually is

The shipped `server.mjs` is a bundle containing the MCP SDK, Zod/AJV, and a relatively small OpenAI-owned adapter whose source path comments identify roughly:

```text
../protocol/src/dynamic-app-tools.ts
../bundled-plugins/codex-app-tools/src/server.ts
```

Its role is:

```text
MCP stdio adapter
    + metadata normalization
    + native-pipe JSON-RPC client
    + cancellation
    + result conversion
```

It is **not** where `read_thread`, `create_thread`, `handoff_thread`, etc. are implemented.

Exact searches in the shipped bundle find no hard-coded definitions for those tool names. Instead the tool catalog is fetched dynamically from the Desktop host.

## 6. Tool discovery is host-owned

The MCP `tools/list` handler in `server.mjs` does this conceptually:

```text
MCP client
   |
   | tools/list
   v
server.mjs
   |
   | native RPC tools/list
   v
Desktop host
   |
   | returns [{name, description, inputSchema, namespace, ...}]
   v
server.mjs
   |
   | caches by name
   | strips host-private `namespace`
   v
MCP tools/list result
```

The native request includes:

```text
threadStartKind = "all" | "default"
```

This explains why different Desktop task contexts can receive different `codex_app` tool catalogs: **the catalog is selected by the Desktop host, not statically declared in `server.mjs`.**

## 7. Tool execution is also host-owned

For MCP `tools/call`, `server.mjs`:

1. resolves the tool from the host-provided catalog;
2. extracts thread/turn/call identifiers from MCP metadata;
3. restores the host-private namespace;
4. forwards the call to the native host;
5. converts host result items into MCP content.

The native request is approximately:

```json
{
  "method": "tools/call",
  "params": {
    "arguments": {},
    "callId": "...",
    "namespace": "...",
    "threadId": "...",
    "tool": "read_thread",
    "turnId": "..."
  }
}
```

There is no local implementation such as:

```text
if tool == read_thread -> perform read here
```

inside `server.mjs`.

Therefore:

```text
server.mjs = adapter/proxy
Desktop host = owner/router of tool-specific semantics
```

## 8. Desktop native-pipe wire protocol

`server.mjs` opens `CODEX_APP_TOOLS_PIPE_PATH` using Node `net.createConnection()`.

The wire format is:

```text
4-byte unsigned little-endian payload length
+ UTF-8 JSON-RPC 2.0 payload
```

Maximum frame size in the observed bundle:

```text
8 MiB
```

Native methods used by the proxy include at least:

```text
tools/list
tools/call
tools/cancel
```

Request example:

```json
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "tools/list",
  "params": {"threadStartKind": "all"}
}
```

Responses use normal JSON-RPC `{result}` / `{error}` envelopes with the same length-prefix framing.

This pipe is **not MCP**. MCP exists on the app-server <-> `server.mjs` side; the native pipe is a separate Desktop-private JSON-RPC transport.

## 9. Cancellation and request identity

`server.mjs` keeps pending native requests by numeric ID.

When the outer MCP tool call is aborted it sends:

```text
native method: tools/cancel
same request id
```

It also normalizes several possible metadata keys to recover:

```text
threadId
turnId
callId
```

including `x-codex-turn-metadata` and several OpenAI/Codex key spellings.

This adapter behavior is one reason `server.mjs` is more than a raw byte proxy, even though it does not implement the actual Desktop business operations.

## 10. Where `read_thread` actually belongs

There are two distinct implementations that should not be conflated.

### Public TUI implementation

Public Codex contains complete Rust durable-task tooling under namespace:

```text
codex_tui
```

in:

```text
codex-rs/tui/src/dynamic_tools.rs
codex-rs/tui/src/dynamic_tools_mcp.rs
```

That implementation directly maps task operations to app-server APIs such as `thread/read`, `thread/turns/list`, `thread/start`, `thread/resume`, and `turn/start`.

### Desktop implementation

Desktop exposes similarly named tools through:

```text
codex_app
```

but the shipped `server.mjs` only forwards them to the Desktop host.

Therefore identical tool names do not imply identical implementations:

```text
codex_tui.read_thread
    -> public Rust -> Codex app-server

codex_app.read_thread
    -> MCP proxy -> Desktop host/router -> source-specific backend
```

## 11. Codex threads vs ordinary ChatGPT conversations

This is the major reason Desktop needs its own host-side router.

A public app-server `thread/read` operates on Codex threads. An ordinary ChatGPT conversation is a different resource type.

Desktop can expose ChatGPT conversation references to the Codex surface. Conceptually the Desktop-host route is:

```text
codex_app.read_thread(resource)
        |
        v
Desktop host resource/source router
      /                         \
 Codex resource             ChatGPT resource
      |                         |
 app-server thread APIs     ChatGPT-side service
```

The exact private implementation behind the right-hand branch is not present in public `openai/codex` or in `server.mjs`; only the host-facing routing boundary is confirmed by the proxy architecture and observed Desktop behavior.

Do not claim:

```text
public app-server thread/read == generic ChatGPT conversation reader
```

## 12. Desktop `codex_app` vs public `codex_apps`

These are separate systems:

```text
codex_app
    singular
    Desktop-injected app-tools MCP
    stdio MCP -> server.mjs -> Desktop native pipe

codex_apps
    plural
    Codex Apps / Plugin Service path
    implemented/configured in public Codex source
```

Configuration behavior of `codex_apps` must not be projected onto Desktop `codex_app`. For example, public Codex use of `chatgpt_base_url` in `codex_apps` is not evidence that Desktop-host `codex_app.read_thread` uses that same base URL for ordinary ChatGPT conversations.

## 13. Tool exposure: why Desktop adds `omit_tools_from=["deferred"]`

Public Codex distinguishes model-facing tool surfaces:

```text
Direct    = included in the initial model-visible tool list
Deferred  = discovered later through tool_search
CodeMode  = available as nested Code Mode tools
```

When Desktop launches `codex_app` with:

```text
omit_tools_from = ["deferred"]
```

those tools are explicitly removed from the deferred/tool-search surface. If Direct remains enabled, the intent is eager model exposure rather than lazy discovery through `tool_search`.

Because this field is absent from the raw shipped `desktop-mcp.json`, it is a Desktop runtime/configuration merge rather than part of the plugin's static manifest.

## 14. Desktop app-server ownership and writer conflicts

Desktop currently may run its own private app-server process rather than sharing another local daemon.

Two app-server processes can coexist on different transports/sockets, but if they share the same Codex store and both attempt to own the same thread writer, public Codex cross-process writer locking prevents dual write ownership.

This explains Desktop errors such as "open in another app" without requiring a socket-address conflict:

```text
Desktop private app-server ------+
                                 +--> same persisted Codex thread
other app-server/daemon ---------+

second writer -> rejected by thread writer lock
```

The writer lock itself belongs to public Codex thread-store implementation; it is separate from the Desktop native app-tools pipe.

## 15. Public / shipped / private ownership matrix

| Component | Public `openai/codex` | Shipped ChatGPT.app bundle | Desktop host/private |
|---|---:|---:|---:|
| Codex core | yes | bundled binary | — |
| `codex app-server` | yes | bundled binary | launched/managed by Desktop |
| app-server protocol | yes | used | — |
| generic MCP client/config | yes | used | — |
| public `codex_tui` task tools | yes | — | — |
| `mcp_servers.codex_app` injection | generic support only | manifest/argv visible | Desktop assembles it |
| `launch_codex_app_tools_mcp` | no public source identified | yes | — |
| `server.mjs` | no public source identified | yes | — |
| MCP <-> native-pipe adapter | no public source identified | yes | — |
| `CODEX_APP_TOOLS_PIPE_PATH` client | no public source identified | yes | pipe server is host-owned |
| `codex_app` dynamic tool catalog | no | fetched at runtime | yes |
| `codex_app` tool-specific semantics | no | not in `server.mjs` | yes |
| ChatGPT conversation source routing | no | not in proxy | yes / ChatGPT service side |

## 16. Canonical Desktop model

```text
                           ChatGPT Desktop

 +------------------------------------------------------------------+
 |                                                                  |
 |  ChatGPT side                                                    |
 |      ordinary conversations / account / hosted services          |
 |                         ^                                        |
 |                         |                                        |
 |                  Desktop host/router                             |
 |                         ^                                        |
 |                         | tools/list, tools/call, tools/cancel    |
 |                         | uint32LE + JSON-RPC                     |
 |                         |                                        |
 |                  native app-tools pipe                           |
 |                         ^                                        |
 |                         |                                        |
 |              codex-app-tools `server.mjs`                        |
 |                         ^                                        |
 |                         | MCP stdio                               |
 |                         |                                        |
 |                  Codex app-server                                |
 |                         ^                                        |
 |                         | AppServerClient                         |
 |                         |                                        |
 |                   Desktop Codex UI                               |
 |                                                                  |
 +------------------------------------------------------------------+
```

When documenting Desktop behavior, always record these dimensions separately:

```text
resource kind
namespace/tool owner
ID format
transport
storage/backend
process owner/lifetime
public vs shipped vs private evidence
```

That prevents similarly named tools or thread IDs from being mistaken for the same architecture.