# `codex-system-contract/v1`

This is a canonical **migrated semantic IR**, not a claim that every Codex
subsystem has been migrated. `model` contains the existing source-derived
evolution contract: extractor fragments, source registry and exact-byte source
manifest, revision, diagnostics, coverage, and the explicit legacy boundary.
The model retains `legacy_machine_reconstruction_remaining: true`; runtime
observation and current-main conformance are not inferred from static extraction.

## Authority and projections

New reports have one owner for migrated facts: `system_contract.model`.
`evolution_contract`, configuration/storage root views, and registered
`config_protocol` subviews are reconstructed from it. Their digests cover actual
payloads, not prose descriptions or filenames. Configuration and storage views
carry `authoritative: false`. Non-migrated legacy fields and hand-written
architecture documents are **not** claimed to be derived from this contract.

`schema.json` is a generated, self-contained Draft 2020-12 schema bundle. Its
editable source is `toolchain/codex_wire_audit/schema_templates`. The `.invalid`
identifiers are stable offline identifiers, not network endpoints.
`example.fixture.json` is synthetic fixture output, not an attestation about the
pinned or current Codex repository.

## Generate, validate, and evolve

From `toolchain`, after installing `.[test]`:

```sh
python -m codex_wire_audit --repo-root /path/to/codex \
  --coverage-profile hybrid_v19 --json --output report.json \
  --emit-system-contract system-contract.json
python tools/check_canonical_contract.py --report report.json \
  --source-root /path/to/codex
python tools/check_canonical_contract.py --contract system-contract.json
python tools/check_canonical_contract.py --write-assets
python tools/check_canonical_contract.py
make check-assets
```

The installed APIs are `build_system_contract` in `codex_wire_audit.system_contract`
and `validate_system_contract` in `codex_wire_audit.system_contract_validation`.
The package capability probe requires these modules and schema resources.

Validation checks versioned structure, source/extractor identities, inventory
counts, declared paths, coverage declarations, graph endpoints, fragment shape,
integrity, and real compatibility projections. Partial fragments may omit facts,
but supplied facts must satisfy their types and allowed fields. Complete
fragments must satisfy their full schema. Missing extraction is not success.

Without `--source-root`, success explicitly says `source_bytes_status: not_checked`.
With it, every manifest file and the entire source set are rehashed. Paths must
be normalized regular files beneath that root; symlinks and `.git` are rejected.
Credential-pattern screening includes keys and values but remains heuristic.
Digests prove consistency, not authenticity or the truth of a hand-edited and
re-signed semantic claim.

Evolve extractors and schemas/tests in the normal package. New extractor IDs are
preserved automatically rather than filtered by a frozen surface catalog. Source
path changes belong in the registry. Add a projector only when a consumer needs
a new compatibility path. Breaking envelope changes require a new format/schema
version; do not reinterpret v1 in place. Regenerate derived assets and keep
source, coverage, installed-package, and pinned-source checks green. The old
report is a compatibility boundary, not a second place to author migrated facts.
