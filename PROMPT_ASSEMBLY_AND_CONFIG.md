# Codex / ChatGPT Desktop: prompt assembly and configuration semantics

Research snapshot: **2026-09-06**.

Public-source validation target: `openai/codex@52e12e0cb506e7bb2c9e406fc84d922e274a0e40`.

This note describes how Codex constructs model input, how world-state fragments are composed, how Responses and Responses Lite differ, and how MCP configuration layers interact with the ChatGPT Desktop-injected `codex_app` server.

Keep two evidence classes separate:

- **Public Codex source** — behavior directly supported by `openai/codex`.
- **Desktop observation** — process arguments, bundled `server.mjs`, or captured requests from ChatGPT Desktop. Desktop observations are useful, but they are not automatically general Codex configuration semantics.

The earlier draft cited `rust-v0.152.0` and described it as wire-identical to a bundled `0.151.0-alpha.7.2`. The public `rust-v0.152.0` tag exists, but the "wire-identical" statement cannot be established from the public repository alone, so this revision does not treat it as a verified public-source fact.

For adjacent topics see:

- [`DESKTOP_ARCHITECTURE.md`](DESKTOP_ARCHITECTURE.md)
- [`CODE_MODE_TOOL_ARCHITECTURE.md`](CODE_MODE_TOOL_ARCHITECTURE.md)
- [`CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md`](CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md)

---

## 1. Base instructions, history, and world state are different layers

Codex does not reduce all model-visible context to one concatenated prompt string. Internally a sampling request is assembled from a `Prompt` containing at least:

```text
Prompt
├── base_instructions
├── input: Vec<ResponseItem>
├── tools
├── parallel_tool_calls
└── optional output schema
```

`build_prompt()` takes already-assembled history/input and attaches the current model-visible tool specs from the step's `ToolRouter`:

```text
BaseInstructions
      │
      ├── Standard Responses -> request.instructions
      │
      └── Responses Lite -> dedicated developer message
                            content kind: model.base_instructions

Conversation / contextual input
      │
      └── typed ResponseItem history
          ├── developer fragments
          ├── contextual user fragments
          ├── world-state full/delta items
          ├── selected skill/plugin injections
          ├── user input
          └── prior assistant/tool history

ToolRouter
      │
      └── model_visible_specs() -> Prompt.tools
```

Fragments are annotated internally with `ContentItemKind` values. Compatible fragments of the same role may be coalesced into one message with multiple content parts. Fragments that require isolation remain separate messages.

`content_item_kinds` is not unconditional wire metadata: Codex can clear those annotations when `content_item_kinds_enabled` is false, and internal metadata is stripped for non-OpenAI providers. Treat content kinds as a Codex/OpenAI metadata channel, not a universal provider requirement.

---

## 2. Base-instruction resolution

Session base instructions have their own resolution path. Current source documents this priority:

```text
1. config.base_instructions override
2. persisted conversation-history / session_meta.base_instructions
3. rendered instructions_template for the current model
```

`model_instructions_file` is loaded into the config-side base-instruction override path, so it can replace the model's built-in instructions. The model catalog can provide `model_messages.instructions_template`, rendered by `ModelInfo::get_model_instructions()`.

Production sampling uses `get_prompt_base_instructions()`, which makes the request copy without mutating instructions persisted or inherited by the session. For example, request-time logic can remove `update_plan` guidance when that tool is unavailable.

Model changes are handled separately by world state: `ModelInstructionsState` can emit model-switch instructions when the current model differs from the previous one. That fragment is not the same object as the session's `BaseInstructions`.

---

## 3. Prompt assembly / composition call graph

The clean current source map is:

```text
Session / turn setup
    │
    ├─ resolve session BaseInstructions
    │
    ├─ capture_step_context(...)
    │    └─ captures ToolRouter / MCP / environment / step settings
    │
    ├─ record_context_updates_and_set_reference_context_item(...)
    │    ├─ build_world_state_for_step(...)
    │    ├─ first context window:
    │    │    build_initial_context_with_world_state(...)
    │    │    + world_state.render_full()
    │    └─ later context:
    │         history.update_world_state(...)
    │         + contextual-fragment merge
    │
    ├─ turn-specific input/injections
    │    ├─ hooks / user input
    │    └─ selected skills / plugins where applicable
    │
    ├─ clone_history().for_prompt(...)
    │
    ├─ get_prompt_base_instructions()
    │
    └─ build_prompt(input, step_context, base_instructions)
         ├─ input = assembled ResponseItem history
         └─ tools = step_context.tool_router.model_visible_specs()
```

Production `run_turn()` repeatedly captures a step view, records live world-state changes, builds `sampling_request_input` from `clone_history().for_prompt(...)`, then calls the sampling path using that exact step context.

There is also a public-source debug helper in `core/src/prompt_debug.rs`:

```text
build_prompt_input(...)
  -> build_prompt_input_from_session(...)
      -> capture_step_context(...)
      -> record_context_updates_and_set_reference_context_item(...)
      -> record supplied user input
      -> clone_history().for_prompt(...)
      -> build_prompt(...)
```

That helper is useful for inspecting the model-visible `input` list without entering the full production loop. Production-only hooks, reminders, compaction, retries, plugin activation, and other turn behavior still live in `session/turn.rs`.

### Source-search map

Useful searches when auditing prompt composition changes:

```text
build_world_state_for_step
build_initial_context_with_world_state
record_context_updates_and_set_reference_context_item
record_step_world_state_if_changed
build_skills_and_plugins
run_hooks_and_record_inputs
clone_history().for_prompt
get_prompt_base_instructions
build_prompt
build_prompt_input_from_session
BaseInstructionsFragment
build_rendered_message
content_item_kinds
```

---

## 4. World-state construction order is not final wire-message order

This distinction is essential.

`build_world_state_for_step()` currently adds core sections in approximately this order:

```text
1.  model identity / model-switch instructions
2.  personality
3.  token-budget context
4.  context-window guidance
5.  realtime state
6.  AGENTS.md state
7.  permissions (or compact permissions)
8.  collaboration mode
9.  persistent mode
10. environment snapshot/context
11. environment instructions
12. apps instructions
13. plugins instructions
14. deferred-tool world state
15. extension-provided world-state sections
16. multi-agent usage hint
17. multi-agent mode
18. managed developer instructions
```

Memory guidance and the available-skills catalog are not core `WorldState` sections in the current architecture. They are extension context contributions.

### Final initial-context composition

`build_initial_context_with_world_state()` then **re-buckets** fragments before writing them into conversation history:

```text
developer_sections
    ordinary compatible developer fragments
    -> coalesced into one developer message

separate_developer_sections
    fragments that must stay isolated
    -> one developer message each

contextual_user_sections
    compatible contextual-user fragments
    -> coalesced into one user message
```

Current special ordering includes:

- model-switch instructions are inserted at the front of the aggregated developer bundle;
- token-budget full-context metadata is kept as a separate developer message;
- multi-agent role instructions are kept separate;
- active multi-agent mode is emitted after usage/role guidance so it can override it;
- managed developer instructions are emitted separately at the end of initial context;
- Guardian developer policy can be isolated as its own message;
- recommended-plugin context can enter the contextual-user bucket.

Therefore a single request capture is useful as an observation, but it is not a universal numbered message sequence. Feature flags, session source, extensions, selected skills/plugins, hooks, reminders, and model mode can all change the concrete item list.

---

## 5. Conditional fragment inclusion

| Fragment | Role / channel | Current condition, approximately |
|---|---|---|
| Session base instructions | special / developer in Lite | Always resolved; source chosen by base-instruction precedence |
| `developer_instructions` | developer | Configured and non-empty; Guardian can isolate it |
| Memory guidance | developer policy | `Feature::MemoryTool` + `memories.use_memories`, and memory instructions can be built |
| Available-skills catalog | developer | Skills prompt contributor has catalog content to inject |
| Selected skill full body | user | Skill is actually selected/invoked |
| AGENTS.md | user-context world state | Loaded AGENTS.md is present |
| Permissions | developer world state | `include_permissions_instructions`; otherwise compact permission state may be used |
| Collaboration mode | developer world state | `include_collaboration_mode_instructions` |
| Multi-agent usage / mode | developer, with isolation rules | Effective multi-agent configuration produces them |
| Environment context | user-context world state | `include_environment_context` |
| Environment instructions | developer world state | environment context enabled + `DeferredExecutor` |
| Apps instructions | developer world state | app instructions enabled + Apps available + model flag |
| Plugin instructions | developer world state | plugins available + model flag |
| Deferred-tool state | world state | `DeferredToolWorldState` feature |
| Managed developer policy | developer, separate | managed additional developer instructions exist |

### Skills

Skills have two separate prompt forms:

```text
skills.catalog
    role: developer
    bounded available-skills catalog / usage guidance

skills.selected_skill_instructions
    role: user
    selected skill body and metadata
```

Do not infer that every installed `SKILL.md` is copied into every request.

### Memories

The current memories extension distinguishes:

```text
generate_memories
    creation/storage behavior

use_memories
    enables read-path prompt context when MemoryTool is enabled

dedicated_tools
    exposes dedicated memory tools when memory use is enabled
```

Memory developer guidance reads the Codex memories area and includes a budget-truncated `memory_summary.md` when available. Its content kind is `memories.instructions`.

---

## 6. Base instructions on the wire

### Standard Responses

Base instructions stay in the request's `instructions` field:

```json
{
  "instructions": "You are Codex…",
  "input": ["…conversation/context items…"],
  "tools": ["…model-visible tool specs…"]
}
```

### Responses Lite

Responses Lite rebuilds prompt-only prefix items for each request. The prefix includes an `additional_tools` developer item and, when non-empty, a dedicated base-instructions developer message:

```json
{
  "input": [
    {
      "type": "additional_tools",
      "role": "developer",
      "tools": ["…"]
    },
    {
      "type": "message",
      "role": "developer",
      "content": [
        {"type": "input_text", "text": "You are Codex…"}
      ],
      "internal_chat_message_metadata_passthrough": {
        "content_item_kinds": ["model.base_instructions"]
      }
    },
    "…conversation/context items…"
  ]
}
```

The internal metadata field is subject to the `content_item_kinds_enabled` gate.

---

## 7. World-state deltas

On the first context window, Codex builds the current world state, renders full context, and stores a `WorldStateSnapshot` baseline.

On later turns:

```text
current WorldState
      +
previous WorldStateSnapshot
      ↓
history.update_world_state(...)
      ↓
changed fragments only
```

Unchanged fragments remain in conversation history. New context windows and compaction can re-establish a full baseline and re-inject the full current context.

This is separate from tool publication: the tool router computes the current model-visible tool projection for the sampling request rather than using world-state delta semantics.

---

## 8. `include_environment_context` is a prompt-context gate

Default configuration currently enables it:

```toml
include_environment_context = true
```

Setting it false removes the model-visible environment snapshot/context. Developer-side environment instructions are also disabled because their world-state condition is:

```text
include_environment_context && Feature::DeferredExecutor
```

It does **not** by itself disable environment creation/selection, cwd handling, shell tools, permission enforcement, or sandboxing.

---

## 9. MCP configuration layering and filtering

### 9.1 Layer precedence

Codex uses `ConfigLayerStack` and recursively merges TOML tables. Current documented precedence, highest first, is:

```text
Legacy managed MDM
Legacy managed file
SessionFlags / CLI overrides
Project config
User profile
User config
Enterprise-managed cloud config
System config
```

The stack is stored internally low-to-high and folded so later/higher layers win.

For the normal Desktop relationship:

```text
user/project config
      ↓ lower precedence
SessionFlags (`-c ...`)
      ↓ higher precedence
legacy managed layers may still outrank SessionFlags
```

`merge_toml_values(base, overlay)` recursively merges tables. A higher layer replaces a key it explicitly supplies; sibling keys omitted by the higher layer survive from the lower table.

A CLI override such as:

```text
-c mcp_servers.codex_app={command=...,enabled=true,...}
```

is first turned into a nested TOML SessionFlags layer and then participates in the same recursive merge.

### 9.2 MCP raw config shape

`config.toml` exposes `mcp_servers` using the raw MCP input shape with custom deserialization, even though the effective value is a `HashMap<String, McpServerConfig>`.

Relevant effective fields include:

```rust
pub struct McpServerConfig {
    #[serde(flatten)]
    pub transport: McpServerTransportConfig,
    pub enabled: bool,
    pub enabled_tools: Option<Vec<String>>,
    pub disabled_tools: Option<Vec<String>>,
    pub omit_tools_from: Option<Vec<ToolExposureSurface>>,
    // ...
}
```

`enabled_tools` and `disabled_tools` are exact **raw MCP tool-name** filters:

```text
allowed iff
    enabled_tools is absent OR raw tool name is in enabled_tools
AND raw tool name is not in disabled_tools
```

Consequences:

```text
enabled_tools = []
    -> empty allow set -> no tool passes

disabled_tools = ["*"]
    -> no wildcard semantics
    -> only a literal tool named "*" is denied
```

---

## 10. Desktop `codex_app`: raw host catalog vs Codex-side filtering

ChatGPT Desktop has been observed launching bundled `codex app-server` with a SessionFlags entry for `mcp_servers.codex_app`. Bundled `server.mjs` gets its raw tool catalog from the Desktop host over the private native pipe:

```text
Codex app-server
     │ MCP tools/list
     ▼
server.mjs
     │ native tools/list
     ▼
Desktop host
     │
     └─ dynamic raw tool catalog
```

That host-owned raw catalog and Codex's effective MCP filtering are different layers.

### Important correction

It is **not correct as a general public-Codex rule** to say that a lower `[mcp_servers.codex_app] enabled_tools = []` cannot affect the effective server merely because Desktop supplies transport fields in a higher SessionFlags layer.

Public merge semantics are recursive. If a lower layer contributes only:

```toml
[mcp_servers.codex_app]
enabled_tools = []
```

and the higher SessionFlags table supplies `command`, `args`, `enabled`, etc. but does not supply `enabled_tools`, the public merge algorithm preserves `enabled_tools = []` in the effective table. `ToolFilter` can then filter the dynamically returned host catalog.

Always distinguish:

```text
Desktop host tools/list
    raw catalog, host-owned

Codex effective MCP tool catalog
    raw catalog after enabled_tools / disabled_tools filtering

model-visible tool list
    filtered catalog after exposure + ToolMode projection
```

If a Desktop capture still shows every raw `codex_app` tool after configuring `enabled_tools = []`, first determine **which layer was captured**. Seeing all tools in the host/native `tools/list` response does not prove that Codex's effective registration or model-visible projection ignored the filter.

A claim that a particular Desktop build ignores the lower-layer filter needs a capture of the **effective merged config and/or model-visible request**, not only the host's raw catalog.

`enabled = false` is different: Desktop's higher SessionFlags layer has been observed explicitly setting `enabled = true`, so the higher boolean wins for that key unless an even-higher managed layer changes it.

---

## 11. Tool exposure: three independent surfaces

Codex models model-facing tool exposure using three independent bits:

```text
DIRECT     0b001   initial direct model surface
DEFERRED   0b010   deferred/search surface
CODE_MODE  0b100   nested Code Mode surface
```

The higher-level `ToolExposure` enum represents combinations:

| `ToolExposure` | Direct | Deferred | Code Mode |
|---|---:|---:|---:|
| `Direct` | yes | no | yes |
| `Deferred` | no | yes | yes |
| `DeferredModelOnly` | no | yes | no |
| `DirectModelOnly` | yes | no | no |
| `CodeModeOnly` | no | no | yes |
| `Hidden` | no | no | no |

Do not confuse:

```text
model_info.tool_mode = code_mode_only
```

with:

```text
ToolExposure::CodeModeOnly
```

The first is model/turn execution policy; the second is one tool's exposure combination.

### `omit_tools_from`

`omit_tools_from` subtracts surfaces before Codex maps the remaining bit set to a `ToolExposure` value.

For a default MCP tool:

```text
DIRECT + DEFERRED + CODE_MODE
```

then:

```toml
omit_tools_from = ["deferred"]
```

leaves:

```text
DIRECT + CODE_MODE
```

This does **not** universally imply an independent top-level tool schema.

- In a Direct/ordinary CodeMode turn, that combination can remain directly visible.
- In a true model-level `ToolMode::CodeModeOnly` turn, ordinary Code-Mode-capable Direct tools are hidden from the independent top-level business-tool list and become eager nested Code Mode tools.

Approval mode is orthogonal to prompt size/exposure: `approval_mode = "prompt"` controls authorization after a tool is selected; it does not by itself remove the tool schema or nested definition from model context.

---

## 12. Deferred discovery under model-level `CodeModeOnly`

For MCP tools, exposure policy only keeps a deferred tool under an effective `ToolMode::CodeModeOnly` when that tool also has the `CODE_MODE` surface. This commonly produces:

```text
DEFERRED + CODE_MODE
    -> ToolExposure::Deferred
```

`register_code_mode_executors()` retains Code-Mode-capable deferred tool runtimes in the nested dispatch set, while keeping their detailed declarations out of the ordinary eager `enabled_tools` description.

The `exec` prompt explicitly tells the model that deferred nested tools can be omitted from the detailed description but are still:

```text
- present on the global tools object
- listed in ALL_TOOLS as name/description metadata
```

So the important lazy-discovery loop for a model-advertised CodeModeOnly turn is:

```text
exec JavaScript
    ↓
filter ALL_TOOLS
    ↓
select normalized tool name
    ↓
await tools.<name>(...)
```

### What about `tool_search`?

Codex may still register a `ToolSearchHandler` internally when deferred searchable tools exist. But under the current default handler exposure in a true effective `ToolMode::CodeModeOnly` turn:

- `build_model_visible_specs()` filters the ordinary Direct `tool_search` spec out of the model-visible top-level list;
- `ToolSpec::ToolSearch` is explicitly skipped when building Code Mode nested definitions.

Therefore do not model ordinary `tool_search` as the required bootstrap for a model-advertised CodeModeOnly turn. The Code Mode discovery surface is `ALL_TOOLS` metadata plus the global `tools` object exposed by `exec`.

---

## 13. Responses Lite tool wire format

`ToolSpec` has five relevant wire shapes:

```text
function
namespace
tool_search
web_search
custom
```

For Responses Lite, `create_tools_json_for_responses_lite()` coalesces plain function and freeform/custom tools into a synthetic `functions` namespace. Namespace tools remain namespaces, and special tool types remain special tool types.

Conceptually:

```text
additional_tools.tools[]
├── namespace: functions
│    ├── ordinary functions
│    └── freeform/custom tools
├── other direct namespaces
├── tool_search        [only when model-visible in that mode]
└── other special tools
```

Code Mode is not a `ToolSpec` wire type. Its public `exec` is a freeform/custom tool with a Lark grammar; `wait` is a function. In Responses Lite those ordinary function/custom specs are carried through the synthetic `functions` namespace.

No `{ "type": "code_mode" }` tool exists on this wire path.

---

## 14. `tool_namespaces_info` is metadata, not the full tool source

The tool router produces separate projections:

```text
ToolRegistry
   │
   └─ finalize_tool_router()
       ├─ model_visible_specs()
       │    -> Prompt.tools
       │    -> Responses tools / Lite additional_tools
       │
       └─ collect_tool_namespaces_info(...)
            -> turn metadata
```

`tool_namespaces_info` is compact topology/exposure metadata. It is not the complete function/namespace schema and should not be treated as the executable source of tools. It can describe tool topology not independently visible as a top-level tool in the current model mode.

---

## 15. Turn-metadata workspace serialization

`x-codex-turn-metadata.workspaces` is Git-workspace metadata keyed by repository root. The implementation uses omission rather than forcing absent values to `null`.

Typical states include:

| Situation | Shape |
|---|---|
| cwd is not a Git repository / no usable enrichment | `workspaces` absent |
| Git repo with commit | workspace may include `latest_git_commit_hash` |
| dirty/clean status known | workspace includes `has_changes: true/false` |
| associated remotes known | workspace can include `associated_remote_urls` |

Git enrichment is asynchronous, so an early request can temporarily contain less workspace metadata than a later request after enrichment completes.

Avoid treating an exact capture table as a permanent protocol guarantee; the current struct already carries more than only commit hash and `has_changes`.

---

## 16. Validation summary of the earlier draft

The earlier draft was **substantially correct in architecture**, but several statements needed tightening or correction.

| Area | Status | Revision |
|---|---|---|
| Base instructions separate from context | correct | Added actual resolution precedence and model-switch distinction |
| Typed/coalesced fragments | correct with caveat | `content_item_kinds` can be stripped by client/provider policy |
| `build_world_state_for_step()` order | broadly correct | Separated construction order from final message order |
| Fixed numbered initial message sequence | too strong | Reclassified as capture-specific; current composer re-buckets fragments |
| Memory/skills behavior | mostly correct | Clarified they are extension contributions in current architecture |
| Responses vs Responses Lite base instructions | correct | Added current Lite prefix behavior |
| World-state deltas | correct | Kept |
| `include_environment_context` | correct | Kept with explicit `DeferredExecutor` condition |
| recursive config merge | correct | Added actual layer-precedence nuance |
| `enabled_tools` exact allow-list | correct | Kept |
| Desktop user `enabled_tools=[]` can never affect `codex_app` | **incorrect as public-Codex rule** | Recursive merge preserves lower sibling keys unless a higher layer overrides them |
| `omit_tools_from=["deferred"]` always leaves top-level visible schemas | **incorrect for model-level CodeModeOnly** | Visibility depends on effective ToolMode; CodeModeOnly makes ordinary tools eager nested tools |
| three exposure bits | correct | Kept with model ToolMode distinction |
| Responses Lite tool shapes | correct with mode caveat | Added CodeModeOnly/top-level visibility nuance |
| workspace omission semantics | directionally correct | Avoided freezing an exact captured shape |

---

## 17. Source anchors

Primary public-source anchors for this revision:

```text
codex-rs/core/src/session/mod.rs
    base-instruction resolution
    build_initial_context_with_world_state
    full-vs-delta context persistence

codex-rs/core/src/session/world_state.rs
    build_world_state_for_step
    ordered world-state section construction

codex-rs/core/src/session/turn.rs
    run_turn
    build_prompt
    production sampling path
    skill/plugin and input recording

codex-rs/core/src/prompt_debug.rs
    build_prompt_input / build_prompt_input_from_session
    standalone model-visible input inspection path

codex-rs/core/src/context_manager/updates.rs
codex-rs/context-fragments/src/fragment.rs
    fragment coalescing and content_item_kinds annotations

codex-rs/core/src/client.rs
    Standard Responses vs Responses Lite request assembly
    content-item-kind stripping

codex-rs/ext/memories/src/extension.rs
codex-rs/ext/memories/src/prompts.rs
    memory prompt contribution

codex-rs/ext/skills/src/fragments.rs
    skills.catalog
    skills.selected_skill_instructions

codex-rs/config/src/loader/README.md
codex-rs/config/src/merge.rs
codex-rs/config/src/overrides.rs
    layer precedence
    recursive TOML merge
    CLI SessionFlags construction

codex-rs/config/src/config_toml.rs
codex-rs/config/src/mcp_types.rs
codex-rs/codex-mcp/src/tools.rs
    MCP config shape and enabled/disabled tool filters

codex-rs/core/src/tools/spec_plan.rs
codex-rs/tools/src/tool_executor.rs
    ToolMode-aware exposure projection
    Direct / Deferred / CodeMode surfaces

codex-rs/code-mode-protocol/src/description.rs
    exec global tools object
    ALL_TOOLS
    deferred nested-tool guidance

codex-rs/core/src/turn_metadata.rs
    workspace / turn metadata
```

When debugging a captured request, record these dimensions separately:

```text
base-instruction source
world-state snapshot and delta baseline
fragment role/content kind
final message grouping/order
selected skills/plugins
ToolMode
per-tool ToolExposure
raw MCP catalog vs filtered MCP catalog
model-visible tool projection
Standard Responses vs Responses Lite
Desktop host observation vs public Codex behavior
```
