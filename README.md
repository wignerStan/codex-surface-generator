# API Surface Audit — Codex CLI wire-contract toolchain

Schema-capture and contract-drift toolchain for the OpenAI **Codex CLI**
HTTP / WebSocket API surface. It parses the CLI's Rust sources at a pinned
upstream commit and emits a machine-readable, schema-validated report of the
wire contract: request/response surfaces, endpoint addressing, header
taxonomy, metadata schemas, and protocol coverage — plus a reproducible,
attested release pipeline.

- Active release line: **v18 / package 11.0.0**
- Reviewed protocol baseline: `openai/codex@6af345407d9c2a568da9d01b6c4b81a9e61495c0`
- Layout:
  - [`api-surface-audit/`](api-surface-audit/) — package source (v18 pipeline)
  - [`release/`](release/) — release artifacts: wheel, sdist, attestation,
    release spec, artifact inventory, validation log, checksums
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
python -m pip install --constraint api-surface-audit/ci/constraints.txt \
  -e api-surface-audit/
python api-surface-audit/tools/release_pipeline.py release --output-dir release_v18
```

`verify` re-installs the released wheel, re-runs its capability probe,
extracts and tests the sdist, and compares rebuilt bytes:

```bash
python api-surface-audit/tools/release_pipeline.py verify --release-dir release_v18
```

See [`api-surface-audit/README_CODEX_WIRE_AUDIT_V18.md`](api-surface-audit/README_CODEX_WIRE_AUDIT_V18.md)
for the full release contract.
