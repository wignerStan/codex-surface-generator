# Codex Surface Generator

Schema and wire-contract report generator for the OpenAI Codex CLI. It
parses the CLI's Rust sources at a pinned upstream commit and emits
validated, machine-readable schema bundles covering the full product
surface: HTTP/WebSocket request-response endpoints, endpoint addressing,
header taxonomy, turn metadata, common settings and feature schemas,
model matrices, and coverage profiles — produced by a reproducible,
attested release pipeline.


- Active release line: **v18 / package 11.0.0**
- Reviewed protocol baseline: `openai/codex@6af345407d9c2a568da9d01b6c4b81a9e61495c0`
- Layout:
  - [`toolchain/`](toolchain/) — package source (v18 pipeline)
  - [`release/`](release/) — release artifacts: wheel, sdist, attestation,
    release spec, artifact inventory, validation log, checksums
  - [`DESKTOP_ARCHITECTURE.md`](DESKTOP_ARCHITECTURE.md) — ChatGPT Desktop /
    Codex Desktop process topology, `codex_app` MCP bridge, native-pipe wire
    protocol, tool ownership, and public/shipped/private boundaries
  - [`CODE_MODE_TOOL_ARCHITECTURE.md`](CODE_MODE_TOOL_ARCHITECTURE.md) — model
    `ToolMode`, per-tool `ToolExposure`, Code Mode `exec`, deferred discovery,
    `ALL_TOOLS`, and the distinction from `cua_repl` / `node_repl`
  - [`CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md`](CHATGPT_HOSTED_SERVICES_ARCHITECTURE.md) —
    `chatgpt_base_url`, `RemotePlugin`, `Apps`, `codex_apps`, generic remote MCP,
    and the distinction from Desktop `codex_app`
  - [`NOTES.md`](NOTES.md) — maintenance notes

## Install

```bash
python -m pip install release/codex_wire_audit-11.0.0-py3-none-any.whl
```

## Usage

```bash
codex-wire-audit --json \
  --repo-root /path/to/codex \
  --output report.json
```

Or against GitHub directly (token via `GITHUB_TOKEN`):

```bash
codex-wire-audit --json --ref main --output report.json
```

## Release pipeline

Build, validate, assemble, and publish one closed release:

```bash
python -m pip install --constraint toolchain/ci/constraints.txt \
  -e toolchain/
python toolchain/tools/release_pipeline.py release --output-dir release_v18
```

`verify` re-installs the released wheel, re-runs its capability probe,
extracts and tests the sdist, and compares rebuilt bytes:

```bash
python toolchain/tools/release_pipeline.py verify --release-dir release_v18
```

See [`toolchain/README_CODEX_WIRE_AUDIT_V18.md`](toolchain/README_CODEX_WIRE_AUDIT_V18.md)
for the full release contract.
