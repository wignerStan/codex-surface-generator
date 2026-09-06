# ChatGPT-hosted services, remote plugins, and Apps MCP

Research snapshot: **2026-09-06**.

This note is intentionally separate from [`DESKTOP_ARCHITECTURE.md`](DESKTOP_ARCHITECTURE.md) and [`CODE_MODE_TOOL_ARCHITECTURE.md`](CODE_MODE_TOOL_ARCHITECTURE.md). It documents the public `openai/codex` architecture around `chatgpt_base_url`, `Feature::RemotePlugin`, `Feature::Apps`, the reserved `codex_apps` MCP server, and generic remote MCP servers.

Source snapshot used for this note: `openai/codex@52e12e0cb506e7bb2c9e406fc84d922e274a0e40`.

## 1. The shortest distinction

```text
Feature::Plugins
    = plugin framework

Feature::RemotePlugin
    = ChatGPT-hosted remote plugin catalog / marketplace control plane

Feature::Apps
    = ChatGPT-hosted Apps / connector runtime gate
        -> reserved MCP server: codex_apps
        -> <chatgpt_base_url>/ps/mcp

generic remote MCP
    = arbitrary configured HTTP MCP endpoint
    = not necessarily related to chatgpt_base_url

Desktop codex_app
    = singular, Desktop-private MCP bridge
    = not codex_apps
```

The most important rule is:

```text
RemotePlugin != codex_apps
```

They are related layers in the same product flow, but they are not the same feature and not the same transport.

## 2. `chatgpt_base_url` is a ChatGPT backend root

Production code uses a ChatGPT backend base in the shape:

```text
https://chatgpt.com/backend-api
```

Public Codex passes `chatgpt_base_url` into multiple ChatGPT-hosted subsystems, including plugin-service, hosted Apps MCP, ChatGPT client helpers, connector/plugin caches, memories, cloud configuration, analytics, and authentication-related services.

Do not treat it as synonymous with the model inference endpoint. Model inference has its own provider/base URL configuration and, for ChatGPT-backed Codex inference, commonly uses the Codex-specific backend base:

```text
https://chatgpt.com/backend-api/codex
```

So keep these concepts separate:

```text
config.chatgpt_base_url
    = root for ChatGPT-hosted product/backend services

config.model_provider.base_url
    = model provider / inference base
```

## 3. `Feature::RemotePlugin`: discovery and marketplace control plane

Current public source defines:

```text
Feature::RemotePlugin
key = "remote_plugin"
stage = Stable
default_enabled = true
```

`RemotePlugin` is passed into plugin configuration separately from `Feature::Plugins` and from `chatgpt_base_url`.

Its role is the remote plugin catalog / marketplace side of the system. Public code builds endpoints from `chatgpt_base_url` such as:

```text
/ps/plugins/search
/ps/plugins/suggested/codex
/ps/plugins/installed
/ps/plugins/{id}
/ps/plugins/{id}/install
/ps/plugins/{id}/uninstall
/ps/plugins/{id}/shares
```

Conceptually:

```text
Feature::RemotePlugin
        |
        v
<chatgpt_base_url>/ps/plugins/*
        |
        +-- discover plugins
        +-- search catalog
        +-- install/uninstall
        +-- installed-plugin sync
        +-- plugin metadata / connector IDs
        +-- sharing / marketplace state
```

This is primarily a **control/discovery plane**.

## 4. `Feature::Apps`: direct gate for `codex_apps`

The reserved hosted Apps MCP server is:

```text
codex_apps
```

Plural.

Public `ext/mcp` code contributes this server only when:

```text
Feature::Apps == enabled
```

If `Feature::Apps` is disabled, the contributor explicitly removes the reserved `codex_apps` registration.

When enabled, Codex builds the hosted Apps MCP server using:

```text
hosted_plugin_runtime_mcp_server_config(
    config.chatgpt_base_url,
    ...
)
```

Therefore the direct relationship is:

```text
Feature::Apps
     |
     v
codex_apps
     |
     v
<chatgpt_base_url>/ps/mcp
```

`Feature::Apps`, not `Feature::RemotePlugin`, is the direct feature gate for the `codex_apps` MCP runtime.

Current public source also defines `Feature::Apps` as stable and default-enabled.

## 5. `codex_apps` is the hosted execution plane

Public source describes the server builder as the:

```text
ChatGPT-hosted plugin runtime served by plugin-service
```

The URL derivation is explicitly tested:

```text
chatgpt_base_url:
    https://chatgpt.com/backend-api

codex_apps MCP URL:
    https://chatgpt.com/backend-api/ps/mcp
```

So `codex_apps` is not a local plugin process and not Desktop's native bridge. It is a ChatGPT-hosted Streamable HTTP MCP endpoint used for Apps / connector tools.

Conceptually:

```text
Codex MCP client
      |
      | Streamable HTTP MCP
      v
<chatgpt_base_url>/ps/mcp
      |
      v
ChatGPT Plugin Service / hosted Apps runtime
```

## 6. Why `RemotePlugin` and `codex_apps` look like the same feature

They are connected in the product flow:

```text
                    ChatGPT Plugin Service

                /ps/plugins/*
                     ^
                     |
               RemotePlugin
          discovery/control plane
                     |
                     v
               plugin metadata
               connector IDs
                     |
                     v
                   Codex
                     |
                     | Feature::Apps
                     v
                 codex_apps
                     |
                     | MCP
                     v
                  /ps/mcp
               execution plane
```

A remote plugin discovered through `/ps/plugins/*` can identify Apps/connectors that are then exercised through the `codex_apps` MCP runtime.

This is why the architecture feels like one subsystem even though the feature gates are separate.

## 7. Concrete evidence that the planes meet

`request_plugin_install` handles discoverable plugins and records plugin metadata such as:

```text
plugin_id
remote_plugin_id
plugin_name
connector_ids
```

For the install/approval interaction it sends MCP elicitation to:

```text
CODEX_APPS_MCP_SERVER_NAME
```

That is the reserved `codex_apps` server.

So a simplified install path is:

```text
RemotePlugin catalog
      |
      | discover/select plugin
      v
request_plugin_install
      |
      | MCP elicitation
      v
codex_apps
      |
      v
ChatGPT /ps/mcp
```

This is the strongest reason to describe the two pieces as **control plane + execution plane**, not as unrelated features.

## 8. Feature matrix

| Concept | Direct feature gate | Uses `chatgpt_base_url` | Primary role | Transport / endpoint |
|---|---|---:|---|---|
| Plugin framework | `Feature::Plugins` | indirectly | local/shared plugin framework | varies |
| Remote plugin catalog | `Feature::RemotePlugin` | yes | discovery, marketplace, install/sync metadata | HTTP `/ps/plugins/*` |
| Hosted Apps runtime | `Feature::Apps` | yes | enable ChatGPT-hosted app/connector tools | MCP via `/ps/mcp` |
| `codex_apps` | `Feature::Apps` | yes | reserved hosted Apps MCP server | Streamable HTTP MCP |
| Generic remote MCP | MCP server config | no requirement | arbitrary third-party/local-hosted MCP | configured URL |
| Desktop `codex_app` | Desktop injection | no public guarantee | Desktop-native app-control bridge | stdio MCP -> native pipe |

## 9. Important counterexample

This configuration is meaningful:

```text
Apps = true
RemotePlugin = false
```

In that case, `codex_apps` can still be contributed because its direct gate is `Feature::Apps`.

What is disabled is the remote plugin marketplace/catalog layer controlled by `Feature::RemotePlugin`.

Therefore this statement is incorrect:

```text
codex_apps == RemotePlugin
```

The better statement is:

```text
RemotePlugin
    = remote plugin discovery/install plane

codex_apps
    = hosted Apps execution plane

Feature::Apps
    = direct gate for codex_apps
```

## 10. Generic remote MCP is a separate axis

A user/plugin can configure an ordinary remote MCP server such as:

```text
mcp_servers.example
    transport = StreamableHttp
    url = https://third-party.example/mcp
```

That server is remote because of its MCP transport/location, not because it is part of ChatGPT Plugin Service.

Its URL does not have to derive from `chatgpt_base_url`.

So split "remote MCP" into two categories:

```text
remote MCP
   |
   +-- generic remote MCP
   |      arbitrary configured URL
   |      not inherently ChatGPT-hosted
   |
   `-- reserved ChatGPT-hosted Apps MCP
          codex_apps
          <chatgpt_base_url>/ps/mcp
```

## 11. `codex_app` singular is not part of this public hosted path

Desktop's injected MCP server is:

```text
codex_app
```

Singular.

Its path is:

```text
codex app-server
      |
      | MCP stdio
      v
codex-app-tools/server.mjs
      |
      | Desktop native-pipe JSON-RPC
      v
ChatGPT Desktop host/router
```

This is completely different from:

```text
codex_apps
      |
      | Streamable HTTP MCP
      v
<chatgpt_base_url>/ps/mcp
```

Never infer Desktop `codex_app` behavior from public `codex_apps` behavior merely because the names are similar.

In particular, public source proves `codex_apps` respects `chatgpt_base_url`; it does **not** prove that Desktop host-side `codex_app.read_thread` or ordinary ChatGPT conversation routing uses the Codex `chatgpt_base_url` setting.

## 12. Canonical mental model

```text
                         Codex

           +-------------+-------------+
           |                           |
           |                           |
     Plugin control plane         Apps execution plane
           |                           |
    Feature::RemotePlugin         Feature::Apps
           |                           |
           v                           v
 <chatgpt_base_url>             codex_apps
   /ps/plugins/*                     |
           |                         | MCP
           |                         v
           +----------------> <chatgpt_base_url>/ps/mcp


Generic remote MCP:
    arbitrary configured URL
    separate from the diagram above

Desktop codex_app:
    Desktop native bridge
    separate from the diagram above
```

When documenting any plugin/MCP behavior, record these dimensions separately:

```text
feature gate
server name
control-plane source
execution-plane endpoint
transport
whether URL derives from chatgpt_base_url
whether implementation is public Codex or Desktop-private
```

That prevents `RemotePlugin`, `Apps`, `codex_apps`, generic remote MCP, and Desktop `codex_app` from collapsing into one ambiguous "remote plugin/MCP" concept.
