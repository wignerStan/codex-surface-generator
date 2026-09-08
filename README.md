# Codex Surface Generator

Schema and wire-contract generator for the OpenAI Codex CLI. It parses Codex
Rust sources at a pinned upstream revision and emits validated,
machine-readable contracts for HTTP/WebSocket traffic, endpoint addressing,
headers, turn metadata, tools, authentication, context management,
configuration shape, local thread storage, rollout identity/layout, and the
relationships between those surfaces.

- Active source/tooling line: **v19 / package 12.0.0**
- Reviewed generator baseline: `openai/codex@6af345407d9c2a568da9d01b6c4b81a9e61495c0`
- Checked-in published release snapshot: **v18 / package 11.0.0**
- Layout:
  - [`toolchain/`](toolchain/) — generator, schemas, profiles, tests, and release pipeline
  - [`release/`](release/) — currently published v18 wheel, sdist, checksums, validation, and attestations
  - [`ARCHITECTURE_DOCS_REVIEW.md`](ARCHITECTURE_DOCS_REVIEW.md) — evidence classes, source pins, review status, and known boundaries for the documentation set
  - [`toolchain/CONFIG_SURFACE_ARCHITECTURE.md`](toolchain/CONFIG_SURFACE_ARCHITECTURE.md) — how generated config shape, feature identity, schema-projection policy, and runtime effects form one graph
  - [`SESSION_THREAD_TURN_LIFECYCLE_SLIDES.md`](SESSION_THREAD_TURN_LIFECYCLE_SLIDES.md) — slide-format model of session-tree identity, live threads, turns, items, settings, and persistence
  - [`FORK_PAGINATION_AND_AGENT_TOPOLOGY_SLIDES.md`](FORK_PAGINATION_AND_AGENT_TOPOLOGY_SLIDES.md) — slide-format model of fork lineage, pagination, root/subagent control, and residency
  - [`LOCAL_STORAGE_AND_ROLLOUT_LAYOUT.md`](LOCAL_STORAGE_AND_ROLLOUT_LAYOUT.md) — concrete `CODEX_HOME`/SQLite layout, `session_id`/`thread_id`/`rollout_id` identity, lifecycle cutovers, and sidecars
  - [`DESKTOP_ARCHITECTURE.md`](DESKTOP_ARCHITECTURE.md) — Desktop process topology and bridge ownership
  - [`CODE_MODE_TOOL_ARCHITECTURE.md`](CODE_MODE_TOOL_ARCHITECTURE.md) — tool exposure and Code Mode ownership
  - [`CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md`](CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md) — ChatGPT-hosted service planes and their boundaries
  - [`PROMPT_ASSEMBLY_AND_CONFIG.md`](PROMPT_ASSEMBLY_AND_CONFIG.md) — prompt assembly/composition call graph, world-state grouping and deltas, Responses/Lite request shape, config-layer merge semantics, and Desktop MCP filtering boundaries
  - [`NOTES.md`](NOTES.md) — maintenance notes

The architecture notes explain design, evidence ownership, and boundaries. They
intentionally do not copy the current setting or API-field catalog. Use generated
JSON for exact keys, types, defaults, constraints, source identity, and effect
links.

The repository uses more than one deliberate source pin:

- the generator's reviewed integration baseline is the commit shown above;
- public-source architecture notes identify their own reviewed Codex commit;
- Desktop notes can also contain explicitly labeled shipped-bundle or observed
  behavior that public Codex source cannot prove.

Do not silently substitute one of those evidence classes for another.

## Install for development

```bash
python -m pip install --constraint toolchain/ci/constraints.txt \
  -e './toolchain[test]'
```

## Generate the full report

```bash
codex-wire-audit --json \
  --repo-root /path/to/codex \
  --coverage-profile hybrid_v19 \
  --output report.json \
  --emit-config-schema config-schema.json \
  --emit-surface-graph config-surface-graph.json
```

Run the focused config/schema integration against a checkout with:

```bash
python toolchain/tools/check_config_surface.py \
  --codex-root /path/to/codex \
  --output config-surface-result.json \
  --schema-output config-schema.json \
  --graph-output config-surface-graph.json
```

## Machine-readable outputs

- `report.json` contains extractor facts, diagnostics, coverage, and source provenance.
- `report.local_storage_schema` is the authoritative local storage/layout view;
  the same data remains available at
  `report.evolution_contract.extractors["extractor.local_storage"].data`.
- `config-schema.json` is the normalized, path-addressable configuration catalog.
- `config-surface-graph.json` connects config paths and feature policy to previously modeled protocol surfaces.
- upstream app-server protocol JSON schemas remain the field-level authority for
  session, thread, turn, item, fork, and pagination RPC shapes.

Human documentation may lag upstream changes. A successful pinned generation
must not: CI validates the package, generated schema, graph references, local
storage semantics, and the reviewed Codex revision, while a scheduled
current-main run remains a separate drift canary.

## Published artifacts versus source

The source tree and package metadata are v19 / 12.0.0. The checked-in
[`release/`](release/) directory has not yet been promoted to v19 and still
contains the verified v18 / 11.0.0 artifacts. Do not treat that directory as a
12.0.0 installation source until a v19 release build is generated, verified,
and committed.

## Release pipeline

```bash
python toolchain/tools/release_pipeline.py release --output-dir release_v19
python toolchain/tools/release_pipeline.py verify --release-dir release_v19
```

See [`toolchain/README_CODEX_WIRE_AUDIT_V19.md`](toolchain/README_CODEX_WIRE_AUDIT_V19.md)
for the operational and proof boundaries.
