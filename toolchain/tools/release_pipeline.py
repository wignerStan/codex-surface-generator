#!/usr/bin/env python3
"""Build, publish, and deeply verify a source/wheel/sdist/capability closure."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
import traceback
from typing import Any, Mapping, Sequence

# ``python tools/release_pipeline.py`` puts ``tools/`` rather than the repository
# root on sys.path. Bootstrap the root before importing sibling modules so the
# documented direct-script entrypoint behaves exactly like module/test execution.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.release_archives import inspect_sdist, inspect_wheel
from tools.release_assembly import assemble_release, validated_distributions
from tools.release_verification import verify_release_directory
from tools.release_integrity import add_integrity, directory_digest
from tools.release_publish import (
    OutputLock,
    clear_failure_evidence,
    publish_directory,
    validate_output_destination,
    write_failure_evidence,
)
from tools.release_runtime import (
    build_distributions,
    compare_distribution_sets,
    deterministic_log_text,
    installed_wheel_probe,
    parse_pytest_pass_count,
    run,
    source_package_probe,
    validate_sdist_roundtrip,
)
from tools.release_spec import (
    copy_source_snapshot,
    default_output_directory,
    load_release_spec,
    release_spec_sha256,
    required_package_resources,
    source_snapshot,
    validate_source_alignment,
    verify_source_snapshot,
)
from tools.release_types import DistributionRecord, ReleaseError, SourceRecord

ROOT = Path(__file__).resolve().parent.parent
# Compatibility alias for historical tests and downstream callers.
_validated_distributions = validated_distributions


def _schema_check(source_root: Path, spec: Mapping[str, Any], log: list[str]) -> int:
    code = (
        "import json; "
        "from importlib.resources import files; "
        "from jsonschema import Draft202012Validator; "
        "from codex_wire_audit.proof_schema_validation import load_schemas; "
        "_, docs=load_schemas(); "
        "release_schema=json.loads(files('codex_wire_audit').joinpath('release_spec.schema.json').read_text()); "
        "docs=dict(docs); docs['release_spec']=release_schema; "
        "assert docs; [Draft202012Validator.check_schema(s) for s in docs.values()]; print(len(docs))"
    )
    output = run(
        [sys.executable, "-c", code],
        cwd=source_root,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    return int(output.strip().splitlines()[-1])


def _toolchain_versions() -> dict[str, str | None]:
    names = ["setuptools", "wheel", "pytest", "jsonschema", "referencing"]
    result: dict[str, str | None] = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _validate_snapshot(
    original_root: Path,
    records: Sequence[SourceRecord],
    source_digest: str,
    spec: Mapping[str, Any],
    workspace: Path,
    *,
    log: list[str],
    phase: dict[str, str],
) -> tuple[dict[str, Any], Path, Path]:
    stage_a = workspace / "source-a"
    stage_b = workspace / "source-b"
    phase["value"] = "stage_source_snapshot"
    copy_source_snapshot(original_root, stage_a, records)
    copy_source_snapshot(original_root, stage_b, records)

    phase["value"] = "validate_source_contract"
    identity = validate_source_alignment(stage_a, spec)
    source_probe = source_package_probe(stage_a, spec, log=log)
    run(
        [sys.executable, "-m", "compileall", "-q", "codex_wire_audit", "tests", "tools", "scripts"],
        cwd=stage_a,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    pytest_output = run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=stage_a,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    schema_documents = _schema_check(stage_a, spec, log)

    phase["value"] = "build_reproducible_distributions"
    dist_a = workspace / "dist-a"
    dist_b = workspace / "dist-b"
    distributions_a = build_distributions(stage_a, dist_a, spec, log=log)
    distributions_b = build_distributions(stage_b, dist_b, spec, log=log)
    compare_distribution_sets(distributions_a, distributions_b)

    wheel = dist_a / spec["artifacts"]["wheel"]
    sdist = dist_a / spec["artifacts"]["sdist"]
    resources = required_package_resources(spec)
    phase["value"] = "inspect_and_probe_distributions"
    wheel_inspection = inspect_wheel(wheel, spec, required_resources=resources)
    sdist_inspection = inspect_sdist(sdist, spec, required_resources=resources)
    installed_probe = installed_wheel_probe(wheel, spec, workspace / "probe", log=log)
    phase["value"] = "validate_sdist_roundtrip"
    roundtrip = validate_sdist_roundtrip(
        sdist,
        wheel,
        spec,
        workspace / "roundtrip",
        log=log,
    )

    validation = add_integrity(
        {
            "format": "codex-wire-audit-release-validation/v4",
            "status": "passed",
            "release_id": spec["release"]["id"],
            "release_spec_sha256": release_spec_sha256(spec),
            "reviewed_codex_commit": spec["release"]["reviewed_codex_commit"],
            "source_snapshot": {
                "algorithm": "sha256-path-mode-bytes-v1",
                "digest": source_digest,
                "file_count": len(records),
                "files": [record.as_dict() for record in records],
            },
            "source_identity": identity,
            "checks": {
                "source_pytest_passed": parse_pytest_pass_count(pytest_output),
                "schema_documents": schema_documents,
                "source_capability_probe": source_probe,
                "clean_copy_distributions": {
                    name: record.as_dict() for name, record in sorted(distributions_a.items())
                },
                "clean_copy_reproducible": True,
                "wheel": wheel_inspection,
                "sdist": sdist_inspection,
                "installed_wheel_capability_probe": installed_probe,
                "sdist_roundtrip": roundtrip,
            },
            "environment": {
                "python": sys.version.split()[0],
                "implementation": platform.python_implementation(),
                "platform": platform.system(),
                "source_date_epoch": spec["release"]["source_date_epoch"],
                "toolchain": _toolchain_versions(),
                "commands_use_sanitized_environment": True,
            },
        }
    )
    return validation, stage_a, dist_a


def release(
    *,
    output: Path,
    legacy_dir: Path | None,
    review_diff: Path | None,
    replace: bool,
    log: list[str],
    phase: dict[str, str],
) -> dict[str, Any]:
    spec = load_release_spec(ROOT)
    output = validate_output_destination(ROOT, output, spec)
    if legacy_dir is not None and output == legacy_dir.resolve(strict=False):
        raise ReleaseError("legacy evidence directory may not equal the release output")
    if review_diff is not None and output in review_diff.resolve(strict=False).parents:
        raise ReleaseError("review diff may not be stored inside the release output")

    with OutputLock(output):
        clear_failure_evidence(output, operation="release")
        phase["value"] = "freeze_source_snapshot"
        validate_source_alignment(ROOT, spec)
        records, source_digest = source_snapshot(ROOT, spec)
        with tempfile.TemporaryDirectory(prefix="codex-wire-audit-release-closure-") as temporary_name:
            workspace = Path(temporary_name)
            validation, stage, dist = _validate_snapshot(
                ROOT,
                records,
                source_digest,
                spec,
                workspace / "validation",
                log=log,
                phase=phase,
            )
            log_replacements = {
                str(workspace): "<RELEASE_WORKSPACE>",
                str(ROOT): "<SOURCE_ROOT>",
            }

            # Deep verification must become durable evidence before the final
            # attestation and aggregate bundle are assembled.  A preliminary
            # candidate is verified, then only its deterministic deep result is
            # folded into the integrity-bound validation record.
            phase["value"] = "assemble_deep_verification_candidate"
            candidate = workspace / "deep-candidate"
            assemble_release(
                stage,
                records,
                source_digest,
                dist,
                validation,
                deterministic_log_text(log, path_replacements=log_replacements),
                candidate,
                spec,
                legacy_dir=legacy_dir,
                review_diff=review_diff,
            )
            phase["value"] = "deep_verify_candidate"
            candidate_verification = verify_release_directory(
                candidate,
                spec,
                deep=True,
                log=log,
                require_recorded_deep=False,
            )
            deep_evidence = candidate_verification.get("deep_verification")
            if not isinstance(deep_evidence, Mapping):
                raise ReleaseError("candidate deep verification produced no evidence")
            validation_payload = dict(validation)
            validation_payload.pop("integrity", None)
            validation_checks = dict(validation_payload["checks"])
            validation_checks["deep_closure_verification"] = deep_evidence
            validation_payload["checks"] = validation_checks
            validation = add_integrity(validation_payload)
            validation_log = deterministic_log_text(
                log, path_replacements=log_replacements
            )

            phase["value"] = "assemble_final_release_twice"
            release_a = workspace / "release-a"
            release_b = workspace / "release-b"
            for target in (release_a, release_b):
                assemble_release(
                    stage,
                    records,
                    source_digest,
                    dist,
                    validation,
                    validation_log,
                    target,
                    spec,
                    legacy_dir=legacy_dir,
                    review_diff=review_diff,
                )
            if directory_digest(release_a) != directory_digest(release_b):
                raise ReleaseError("complete release assembly is not reproducible")

            phase["value"] = "verify_final_assembled_release"
            assembled_verification = verify_release_directory(
                release_a, spec, deep=False, log=log
            )
            phase["value"] = "verify_source_unchanged"
            verify_source_snapshot(ROOT, records, source_digest, spec)
            phase["value"] = "publish_release"
            publish_directory(release_a, output, replace=replace)
            phase["value"] = "verify_published_release"
            published_verification = verify_release_directory(
                output, spec, deep=False, log=log
            )
        clear_failure_evidence(output, operation="release")
    return {
        "status": "passed",
        "release_id": spec["release"]["id"],
        "durable_deep_verification": deep_evidence,
        "assembled_verification": assembled_verification,
        "published_verification": published_verification,
    }


def verify(
    *,
    release_dir: Path,
    deep: bool,
    log: list[str],
    phase: dict[str, str],
) -> dict[str, Any]:
    spec = load_release_spec(ROOT)
    release_dir = validate_output_destination(ROOT, release_dir, spec)
    if not release_dir.is_dir():
        raise ReleaseError(f"release directory does not exist: {release_dir}")
    with OutputLock(release_dir):
        clear_failure_evidence(release_dir, operation="verify")
        phase["value"] = "verify_release_directory"
        result = verify_release_directory(release_dir, spec, deep=deep, log=log)
        clear_failure_evidence(release_dir, operation="verify")
        return result


def _failure_record(error: Exception, spec: Mapping[str, Any] | None, *, phase: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "format": "codex-wire-audit-release-failure/v2",
        "status": "failed",
        "phase": phase,
        "error_type": type(error).__name__,
        "message": str(error),
    }
    if spec is not None:
        record["release_id"] = spec.get("release", {}).get("id")
        record["release_spec_sha256"] = release_spec_sha256(spec)
    return add_integrity(record)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true", help="show a traceback after stable failure evidence")
    subparsers = parser.add_subparsers(dest="command", required=True)
    release_parser = subparsers.add_parser("release", help="validate, assemble, and publish atomically")
    release_parser.add_argument("--output-dir")
    release_parser.add_argument("--legacy-dir")
    release_parser.add_argument("--review-diff")
    release_parser.add_argument("--replace", action="store_true")
    verify_parser = subparsers.add_parser("verify", help="verify an assembled release directory")
    verify_parser.add_argument("--release-dir", required=True)
    verify_parser.add_argument("--fast", action="store_true", help="skip rebuild and reinstall checks")
    args = parser.parse_args(argv)

    spec: dict[str, Any] | None = None
    log: list[str] = []
    phase = {"value": "startup"}
    target: Path | None = None
    try:
        spec = load_release_spec(ROOT)
        if args.command == "release":
            target = (
                Path(args.output_dir).resolve()
                if args.output_dir
                else default_output_directory(ROOT, spec).resolve()
            )
            result = release(
                output=target,
                legacy_dir=Path(args.legacy_dir).resolve() if args.legacy_dir else None,
                review_diff=Path(args.review_diff).resolve() if args.review_diff else None,
                replace=args.replace,
                log=log,
                phase=phase,
            )
        else:
            target = Path(args.release_dir).resolve()
            result = verify(
                release_dir=target,
                deep=not args.fast,
                log=log,
                phase=phase,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as error:
        if target is not None:
            write_failure_evidence(
                target,
                operation=args.command,
                error=error,
                spec=spec,
                phase=phase["value"],
                log=log,
            )
        failure = _failure_record(error, spec, phase=phase["value"])
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        if args.debug:
            traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
