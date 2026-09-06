# Codex wire audit v17 — release-closure hardening

v17 keeps the v16 Context Management semantics and fixes the release process that
previously allowed newer companion evidence to sit beside an older installable
package. The authoritative release command now treats source, wheel, sdist,
installed capabilities, reference containers, validation evidence, and aggregate
archives as one byte-bound closure.

## Authoritative release command

```bash
python tools/release_pipeline.py release --output-dir release_v17
python tools/release_pipeline.py verify --release-dir release_v17
```

The command does not accept an arbitrary prebuilt `dist/` directory. It:

1. loads the package-owned `release_spec.v1.json`;
2. freezes an exact source snapshot including file modes;
3. builds wheel and sdist from two independent copies;
4. requires byte-identical distributions;
5. verifies wheel `METADATA` and every `RECORD` entry;
6. installs the wheel in an isolated no-dependency virtual environment;
7. asks that installed wheel to enumerate active extractors, profiles, resources,
   and indexed containers;
8. reruns tests from the sdist and rebuilds an identical wheel from it;
9. derives the standalone Context Management container from the verified wheel,
   rather than copying an unrelated companion directory;
10. assembles the complete release twice and requires byte-identical output;
11. emits self-hashed validation and release attestations;
12. verifies checksums, aggregate ZIP members, and claim provenance before publish.

## What this prevents

The release fails if any of these occur:

- an old wheel or sdist is substituted after validation;
- the source tree changes between validation and assembly;
- package version, generator version, release spec, or filenames disagree;
- a claimed extractor exists only in a companion JSON artifact;
- an extractor file is packaged but not registered at runtime;
- a profile omits a capability required by the release claim;
- required schemas or reference data are missing from the installed wheel;
- the sdist cannot reproduce the exact wheel;
- a standalone active container differs from the data inside the wheel;
- an extra or missing distribution appears in the validated set;
- CI invokes a retired v13 release builder.

Legacy v15 companion artifacts may still be carried for historical evidence, but
they receive the explicit role `legacy_evidence_only` and are forbidden from
satisfying any active package capability claim.

## Context Management scope retained from v16

The active package still models:

- account/provider/model-capability activation gates;
- the split between `openai_base_url` and `chatgpt_base_url`;
- `new_context` and no-summary rollover;
- all `alpha/history/v2/*` and `alpha/notes/v2/*` routes;
- the bounded `thread_hint` index contract;
- remote History/Notes as durable state with lazy retrieval.

The reviewed Codex source revision remains
`6af345407d9c2a568da9d01b6c4b81a9e61495c0` for these semantics.

## Script ownership boundaries

- `codex_wire_audit/release_spec.v1.json` owns release identity and claims.
- `codex_wire_audit/release_contract.py` owns installed-package introspection.
- `tools/release_common.py` owns byte/provenance/archive primitives.
- `tools/release_pipeline.py` owns orchestration and final policy.
- version-specific v13/v16 builders remain historical utilities and are not used
  by the active Makefile or CI release path.
