# Codex wire audit v17 release notes

## Purpose

v17 is a packaging and release-integrity hardening release. It directly addresses
the split-brain failure mode found in the supplied v15 delivery: newer Guardian
and alpha-route companion artifacts were present, while the wheel and sdist still
identified and executed the older v13 package.

## Single package-owned release specification

`codex_wire_audit/release_spec.v1.json` is now the source of truth for package and
generator versions, reviewed Codex commit, active capability claims, required
extractors/profiles/resources, artifact names, source exclusions, and the limited
role of carried legacy evidence.

The source preflight rejects disagreement among the release specification,
`pyproject.toml`, `GENERATOR_VERSION`, the README selection, and expected wheel or
sdist filenames.

## Installed capability proof

`python -m codex_wire_audit.release_contract` reports what the imported package
actually activates. The isolated-wheel probe verifies:

- distribution and generator versions;
- registered extractor IDs;
- profile requirements and runtime-scenario requirements;
- required package resources and their hashes;
- the Context Management indexed container;
- the metadata-history indexed container.

This is stronger than checking that files merely exist in a ZIP.

## Atomic release pipeline

`tools/release_pipeline.py release` freezes one source snapshot and performs the
entire validation and assembly from that immutable copy. Exact wheel/sdist hashes
are recorded in a self-hashed validation record and rechecked during assembly.
The builder no longer globs and copies every `.whl` or `.gz` file from a directory.

The standalone Context Management container is extracted from the verified wheel,
so it cannot describe behavior absent from the installed package. Legacy v15
companions remain explicitly non-authoritative.

## Reproducibility and closure

The pipeline builds distributions from two clean copies, rebuilds the wheel from
the sdist, assembles the complete release twice, and requires byte identity at
each boundary. It also verifies wheel `RECORD`, safe archive paths, release
checksums, aggregate bundle membership, and byte equality between bundled and
standalone artifacts.

## CI correction

The active workflow now invokes the v17 release pipeline. The stale v13 validation
and release-builder calls are removed from the release job.

## Semantic scope

No new Codex protocol meaning is asserted beyond v16. Context Management remains
bound to the source revision
`6af345407d9c2a568da9d01b6c4b81a9e61495c0`; v17 strengthens how those claims are
packaged and proven.

## Validation gates

The release pipeline requires the complete source suite, schema validation,
source and installed capability probes, exact wheel `RECORD` verification,
two-copy distribution reproducibility, sdist test rerun, sdist-to-wheel byte
identity, two-copy complete release reproducibility, and independent final
release verification. The active package remains within the established
maintainability budgets; the Context Management extractor was split so the
largest active module remains below 600 lines rather than relaxing the limit.
