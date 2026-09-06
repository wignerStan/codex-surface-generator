#!/usr/bin/env python3
"""Run the complete offline v13 release validation pipeline.

The validator always emits JSON and text evidence, including on failure. It
builds distributions from two independent clean source copies, verifies the
installed wheel and packaged metadata-history container, and records explicit
not-run boundaries for external current-main and runtime-wire integration.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata as importlib_metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import traceback
from typing import Any, Iterable
import venv
import zipfile

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SOURCE_DATE_EPOCH = 315532800
DEFAULT_TIMEOUT = 360


class ValidationFailure(RuntimeError):
    """Raised when a release validation assertion fails."""


class Recorder:
    def __init__(self) -> None:
        self.lines = [
            "Codex wire audit v13 offline release validation",
            f"python={sys.version.split()[0]}",
            f"source_date_epoch={SOURCE_DATE_EPOCH}",
            "",
        ]
        self.checks: list[dict[str, Any]] = []

    def command(
        self,
        label: str,
        command: list[str],
        *,
        cwd: Path = ROOT,
        env: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        started = datetime.now(timezone.utc)
        self.lines.append(f"$ (cd {cwd}) {' '.join(command)}")
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=merged,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            output = error.stdout or ""
            self.lines.append(str(output).rstrip())
            self.checks.append(
                {
                    "id": label,
                    "status": "failed",
                    "reason": "timeout",
                    "duration_seconds": round(
                        (datetime.now(timezone.utc) - started).total_seconds(), 3
                    ),
                }
            )
            raise ValidationFailure(f"{label} timed out after {timeout} seconds") from error
        self.lines.append(result.stdout.rstrip())
        check = {
            "id": label,
            "status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "duration_seconds": round(
                (datetime.now(timezone.utc) - started).total_seconds(), 3
            ),
        }
        self.checks.append(check)
        if result.returncode:
            raise ValidationFailure(
                f"{label} failed with exit status {result.returncode}"
            )
        self.lines.append(f"PASS: {label}")
        return result

    def assertion(self, label: str, condition: bool, detail: str) -> None:
        self.checks.append(
            {
                "id": label,
                "status": "passed" if condition else "failed",
                "detail": detail,
            }
        )
        self.lines.append(f"{'PASS' if condition else 'FAIL'}: {label}: {detail}")
        if not condition:
            raise ValidationFailure(f"{label}: {detail}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _source_copy(source: Path, destination: Path) -> None:
    ignored_names = {
        ".git",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
        ".coverage",
        "coverage-v13.json",
        "ci-out",
    }

    def ignore(directory: str, names: list[str]) -> set[str]:
        omitted: set[str] = set()
        for name in names:
            if name in ignored_names or name.endswith((".pyc", ".pyo", ".egg-info")):
                omitted.add(name)
            if name.startswith("codex_wire_audit_v13_validation"):
                omitted.add(name)
        return omitted

    shutil.copytree(source, destination, ignore=ignore)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _compare_file_sets(
    recorder: Recorder,
    label: str,
    first: Path,
    second: Path,
) -> dict[str, str]:
    left = _tree_bytes(first)
    right = _tree_bytes(second)
    recorder.assertion(label, left == right, f"{len(left)} files compared byte-for-byte")
    return {name: hashlib.sha256(data).hexdigest() for name, data in left.items()}


def _parse_pytest_count(output: str) -> int:
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:[,\s]|$)", output)
    if not matches:
        raise ValidationFailure("could not parse pytest pass count")
    return int(matches[-1])


def _parse_fixture_count(output: str) -> tuple[int, int]:
    match = re.search(r"fixtures:\s*(\d+)/(\d+)\s+passed", output)
    if not match:
        raise ValidationFailure("could not parse protocol fixture count")
    return int(match.group(1)), int(match.group(2))


def _tool_versions() -> dict[str, str]:
    names = [
        "pip",
        "setuptools",
        "wheel",
        "pytest",
        "coverage",
        "jsonschema",
        "referencing",
    ]
    result: dict[str, str] = {"python": sys.version.split()[0]}
    for name in names:
        try:
            result[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            result[name] = "not-installed"
    return result


def _workflow_assertions(recorder: Recorder) -> dict[str, Any]:
    path = ROOT / ".github/workflows/machine-proof.yml"
    text = path.read_text(encoding="utf-8")
    uses = re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s]+)", text)
    unpinned = [value for value in uses if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", value)]
    recorder.assertion("workflow actions pinned", not unpinned, f"{len(uses)} uses entries")
    recorder.assertion(
        "workflow read-only contents",
        bool(re.search(r"permissions:\s*\n\s+contents:\s+read", text)),
        "permissions.contents=read",
    )
    recorder.assertion(
        "workflow durable uploads",
        "if: always()" in text and "actions/upload-artifact@" in text,
        "failure-path artifact upload configured",
    )
    recorder.assertion(
        "workflow python matrix",
        all(version in text for version in ('"3.10"', '"3.11"', '"3.12"', '"3.13"')),
        "Python 3.10 through 3.13",
    )
    recorder.assertion(
        "workflow reproducible release assembly",
        text.count("tools/build_v13_release.py") >= 2
        and 'diff -ru "${RUNNER_TEMP}/release-a" "${RUNNER_TEMP}/release-b"' in text,
        "two complete release builds compared byte-for-byte",
    )
    return {"path": str(path.relative_to(ROOT)), "uses": uses, "unpinned": unpinned}


def _inspect_wheel(recorder: Recorder, wheel: Path) -> dict[str, Any]:
    required_members = {
        "codex_wire_audit/metadata_history_data/metadata-key-history.v1.json",
        "codex_wire_audit/metadata_history_container_data/index.json",
        "codex_wire_audit/metadata_history_container_data/keys/key.turn_metadata.code_mode_tool_names.json",
        "codex_wire_audit/proof_schema_templates/metadata-key-history-v1.schema.json",
        "codex_wire_audit/proof_schema_templates/codex-wire-audit-proof-attestation-v2.schema.json",
        "codex_wire_audit/coverage_profiles.v2.json",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = sorted(required_members - names)
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        metadata_text = archive.read(metadata_name).decode("utf-8")
        entry_points = archive.read(entry_points_name).decode("utf-8")
    recorder.assertion("wheel package data", not missing, f"missing={missing}")
    required_commands = {
        "codex-wire-audit",
        "codex-wire-audit-proof",
        "codex-wire-audit-proof-diff",
        "codex-wire-audit-history",
    }
    missing_commands = sorted(
        command for command in required_commands if f"{command} =" not in entry_points
    )
    recorder.assertion("wheel console commands", not missing_commands, f"missing={missing_commands}")
    dependency_lines = [
        line for line in metadata_text.splitlines() if line.startswith("Requires-Dist:")
    ]
    recorder.assertion(
        "wheel runtime dependencies",
        any("jsonschema" in line for line in dependency_lines)
        and any("referencing" in line for line in dependency_lines),
        "; ".join(dependency_lines),
    )
    return {
        "member_count": len(names),
        "required_members": sorted(required_members),
        "missing_members": missing,
        "entry_points": entry_points.splitlines(),
        "requires_dist": dependency_lines,
    }


def _extract_sdist(sdist: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(sdist, "r:gz") as archive:
        archive.extractall(destination, filter="data")
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValidationFailure(f"sdist must contain one root directory, found {roots}")
    return roots[0]


def _distribution_build(
    recorder: Recorder,
    source_copy: Path,
    output_dir: Path,
    home: Path,
    label: str,
) -> tuple[Path, Path]:
    env = {
        "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    recorder.command(
        label,
        [
            sys.executable,
            "tools/build_distribution.py",
            "--output-dir",
            str(output_dir),
            "--manifest",
            str(output_dir.parent / f"{label}.json"),
        ],
        cwd=source_copy,
        env=env,
        timeout=600,
    )
    wheels = list(output_dir.glob("*.whl"))
    sdists = list(output_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValidationFailure(
            f"{label} produced unexpected distributions: wheels={wheels}, sdists={sdists}"
        )
    return wheels[0], sdists[0]


def _validate_installed_wheel(
    recorder: Recorder,
    wheel: Path,
    temporary: Path,
) -> dict[str, Any]:
    environment = temporary / "installed-venv"
    venv.EnvBuilder(with_pip=True, clear=True, system_site_packages=False).create(environment)
    bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = bin_dir / ("python.exe" if os.name == "nt" else "python")
    suffix = ".exe" if os.name == "nt" else ""
    dependency_path = sysconfig.get_paths()["purelib"]
    dependency_env = {
        "PYTHONPATH": dependency_path,
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    recorder.command(
        "installed wheel",
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        cwd=temporary,
        env={"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    for command in (
        "codex-wire-audit",
        "codex-wire-audit-proof",
        "codex-wire-audit-history",
    ):
        recorder.command(
            f"installed {command} self-test",
            [str(bin_dir / f"{command}{suffix}"), "--self-test"],
            cwd=temporary,
            env=dependency_env,
        )
    generated = temporary / "installed-history-container"
    history = bin_dir / f"codex-wire-audit-history{suffix}"
    recorder.command(
        "installed history container generation",
        [str(history), "--output-dir", str(generated), "--output", str(temporary / "history-build.json")],
        cwd=temporary,
        env=dependency_env,
    )
    recorder.command(
        "installed history container verification",
        [str(history), "--verify-container", str(generated), "--output", str(temporary / "history-verify.json")],
        cwd=temporary,
        env=dependency_env,
    )
    recorder.command(
        "installed package data and version",
        [
            str(python),
            "-c",
            (
                "from codex_wire_audit import GENERATOR_VERSION; "
                "from codex_wire_audit.metadata_history import load_history_catalog, validate_history_catalog; "
                "from codex_wire_audit.metadata_history_container import verify_multi_json_container; "
                "from pathlib import Path; import codex_wire_audit; "
                "assert GENERATOR_VERSION == '7.0.0'; "
                "assert not validate_history_catalog(load_history_catalog()); "
                "root=Path(codex_wire_audit.__file__).parent/'metadata_history_container_data'; "
                "assert not verify_multi_json_container(root); print('installed package data: ok')"
            ),
        ],
        cwd=temporary,
        env=dependency_env,
    )
    recorder.command(
        "installed declared dependency check",
        [
            str(python),
            "-c",
            (
                "from importlib.metadata import distribution, version; "
                "from packaging.requirements import Requirement; "
                "requirements=[]; "
                "[requirements.append(Requirement(raw)) for raw in (distribution('codex-wire-audit').requires or [])]; "
                "active=[r for r in requirements if not r.marker or r.marker.evaluate({'extra':''})]; "
                "bad=[f'{r.name} {version(r.name)} not in {r.specifier}' for r in active if version(r.name) not in r.specifier]; "
                "assert not bad, bad; print('declared dependencies: ok')"
            ),
        ],
        cwd=temporary,
        env=dependency_env,
    )
    return {
        "venv_python": str(python),
        "generated_container_members": len(_tree_bytes(generated)),
    }


def _validate_source_tree(
    recorder: Recorder,
    temporary: Path,
) -> dict[str, Any]:
    python = sys.executable
    recorder.command(
        "byte compilation",
        [python, "-m", "compileall", "-q", "codex_wire_audit", "tests", "tools", "scripts"],
    )
    pytest_result = recorder.command(
        "unit and regression tests",
        [python, "-m", "pytest", "-q"],
        timeout=600,
    )
    test_count = _parse_pytest_count(pytest_result.stdout)
    fixtures = recorder.command(
        "protocol fixtures",
        [python, "validate_codex_wire_fixtures_v10.py"],
    )
    fixture_passed, fixture_total = _parse_fixture_count(fixtures.stdout)
    recorder.assertion(
        "all protocol fixtures passed",
        fixture_passed == fixture_total,
        f"{fixture_passed}/{fixture_total}",
    )
    recorder.command(
        "proof gate self-test",
        [python, "-m", "codex_wire_audit.proof_gate", "--self-test"],
    )
    recorder.command(
        "history CLI self-test",
        [python, "-m", "codex_wire_audit.history_cli", "--self-test"],
    )
    recorder.command("strict schemas and history catalog", ["make", "schemas"])
    recorder.command(
        "maintainability budgets",
        [python, "tools/maintainability_metrics.py", "--check"],
    )
    recorder.command(
        "frozen v10 integrity boundary",
        [
            python,
            "-c",
            (
                "from codex_wire_audit.legacy import verify_frozen_v10_integrity as v; "
                "assert v(include_tests=True) == (); print('frozen v10 hashes: ok')"
            ),
        ],
    )

    first = temporary / "history-container-a"
    second = temporary / "history-container-b"
    first_zip = temporary / "history-a.zip"
    second_zip = temporary / "history-b.zip"
    for label, directory, archive in (
        ("first history container", first, first_zip),
        ("second history container", second, second_zip),
    ):
        recorder.command(
            label,
            [
                python,
                "-m",
                "codex_wire_audit.history_cli",
                "--output-dir",
                str(directory),
                "--zip",
                str(archive),
                "--output",
                str(temporary / f"{label.replace(' ', '-')}.json"),
            ],
        )
        recorder.command(
            f"verify {label}",
            [
                python,
                "-m",
                "codex_wire_audit.history_cli",
                "--verify-container",
                str(directory),
                "--output",
                str(temporary / f"verify-{label.replace(' ', '-')}.json"),
            ],
        )
    _compare_file_sets(recorder, "history container reproducibility", first, second)
    recorder.assertion(
        "history ZIP reproducibility",
        first_zip.read_bytes() == second_zip.read_bytes(),
        f"sha256={_sha256(first_zip)}",
    )
    packaged = ROOT / "codex_wire_audit/metadata_history_container_data"
    _compare_file_sets(recorder, "checked-in history container is current", first, packaged)

    coverage_data = temporary / ".coverage"
    coverage_json = temporary / "coverage-v13.json"
    coverage_ratchet = temporary / "coverage-ratchet.json"
    coverage_env = {"COVERAGE_FILE": str(coverage_data)}
    recorder.command(
        "proof-critical coverage execution",
        [python, "-m", "coverage", "run", "--branch", "-m", "pytest", "-q"],
        env=coverage_env,
        timeout=600,
    )
    recorder.command(
        "coverage JSON",
        [python, "-m", "coverage", "json", "-o", str(coverage_json)],
        env=coverage_env,
    )
    recorder.command(
        "coverage ratchet",
        [
            python,
            "tools/check_v13_coverage.py",
            str(coverage_json),
            "--output",
            str(coverage_ratchet),
        ],
    )
    coverage_summary = json.loads(coverage_ratchet.read_text(encoding="utf-8"))
    return {
        "pytest_passed": test_count,
        "protocol_fixtures_passed": fixture_passed,
        "protocol_fixtures_total": fixture_total,
        "history_container_members": len(_tree_bytes(first)),
        "history_container_zip_sha256": _sha256(first_zip),
        "coverage": coverage_summary,
    }


def _validate_distributions(
    recorder: Recorder,
    temporary: Path,
    output_dir: Path,
) -> dict[str, Any]:
    copy_a = temporary / "source-a"
    copy_b = temporary / "source-b"
    _source_copy(ROOT, copy_a)
    _source_copy(ROOT, copy_b)
    wheel_a, sdist_a = _distribution_build(
        recorder,
        copy_a,
        temporary / "dist-a",
        temporary / "home-a",
        "distribution build a",
    )
    wheel_b, sdist_b = _distribution_build(
        recorder,
        copy_b,
        temporary / "dist-b",
        temporary / "home-b",
        "distribution build b",
    )
    recorder.assertion(
        "wheel reproducibility",
        wheel_a.read_bytes() == wheel_b.read_bytes(),
        f"sha256={_sha256(wheel_a)}",
    )
    recorder.assertion(
        "sdist reproducibility",
        sdist_a.read_bytes() == sdist_b.read_bytes(),
        f"sha256={_sha256(sdist_a)}",
    )
    wheel_inspection = _inspect_wheel(recorder, wheel_a)

    extracted = _extract_sdist(sdist_a, temporary / "sdist-source")
    roundtrip = temporary / "roundtrip-wheel"
    roundtrip.mkdir()
    recorder.command(
        "sdist to wheel round trip",
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(extracted),
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(roundtrip),
        ],
        cwd=temporary,
        env={
            "SOURCE_DATE_EPOCH": str(SOURCE_DATE_EPOCH),
            "PYTHONHASHSEED": "0",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        },
        timeout=600,
    )
    rebuilt = next(roundtrip.glob("*.whl"))
    recorder.assertion(
        "sdist round-trip wheel reproducibility",
        rebuilt.read_bytes() == wheel_a.read_bytes(),
        f"sha256={_sha256(rebuilt)}",
    )
    installed = _validate_installed_wheel(recorder, wheel_a, temporary)

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[dict[str, Any]] = []
    for source in (wheel_a, sdist_a):
        destination = output_dir / source.name
        shutil.copy2(source, destination)
        outputs.append(
            {
                "name": destination.name,
                "byte_length": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )
    return {
        "artifacts": outputs,
        "wheel_inspection": wheel_inspection,
        "installed_wheel": installed,
    }


def validate(output_dir: Path, recorder: Recorder) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-wire-audit-v13-validation-") as name:
        temporary = Path(name)
        source_tree = _validate_source_tree(recorder, temporary)
        workflow = _workflow_assertions(recorder)
        distributions = _validate_distributions(recorder, temporary, output_dir)

    from codex_wire_audit.metadata_history import (
        catalog_payload_sha256,
        load_history_catalog,
    )

    catalog = load_history_catalog()
    metrics = json.loads(
        (ROOT / "codex_wire_audit_v13_maintainability_metrics.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "format": "codex-wire-audit-v13-validation/v1",
        "status": "passed",
        "generator_version": "7.0.0",
        "tool_versions": _tool_versions(),
        "checks": recorder.checks,
        "tests": {
            "pytest_passed": source_tree["pytest_passed"],
            "protocol_fixtures_passed": source_tree["protocol_fixtures_passed"],
            "protocol_fixtures_total": source_tree["protocol_fixtures_total"],
        },
        "metadata_history": {
            "catalog_sha256": catalog_payload_sha256(catalog),
            "keys": len(catalog["keys"]),
            "states": len(catalog["states"]),
            "versions": len(catalog["versions"]),
            "transitions": len(catalog["transitions"]),
            "current_version_ref": catalog["current_version_ref"],
            "reviewed_through": catalog["reviewed_through"],
            "container_members": source_tree["history_container_members"],
            "container_zip_sha256": source_tree["history_container_zip_sha256"],
        },
        "coverage": source_tree["coverage"],
        "maintainability": {
            "metrics_sha256": hashlib.sha256(_canonical_json(metrics)).hexdigest(),
            "guardrails": metrics["guardrails"],
            "active_layer": metrics["v13_active_layer"],
        },
        "workflow": workflow,
        "distributions": distributions,
        "boundaries": {
            "connected_github_source_history_review": "completed_outside_local_validator",
            "full_current_main_checkout_probe": "not_run",
            "all_historical_checkout_probes": "not_run",
            "runtime_wire_observation": "not_run",
            "cryptographically_signed_build_provenance": "not_run",
        },
    }


def _write_evidence(
    json_path: Path,
    log_path: Path,
    summary: dict[str, Any],
    recorder: Recorder,
) -> None:
    summary = dict(summary)
    summary["evidence_integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": hashlib.sha256(_canonical_json(summary)).hexdigest(),
    }
    json_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    recorder.lines.extend(["", json.dumps(summary, indent=2, sort_keys=True)])
    log_path.write_text("\n".join(recorder.lines).rstrip() + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=ROOT / "codex_wire_audit_v13_validation.json")
    parser.add_argument("--log", type=Path, default=ROOT / "codex_wire_audit_v13_validation.txt")
    parser.add_argument("--dist-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    recorder = Recorder()
    summary: dict[str, Any] = {
        "format": "codex-wire-audit-v13-validation/v1",
        "status": "failed",
        "generator_version": "7.0.0",
        "tool_versions": _tool_versions(),
        "checks": recorder.checks,
    }
    exit_code = 1
    try:
        summary = validate(args.dist_dir.resolve(), recorder)
        exit_code = 0
    except Exception as error:  # Evidence must survive every operational failure.
        summary.update(
            {
                "status": "failed",
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
                "checks": recorder.checks,
                "boundaries": {
                    "connected_github_source_history_review": "completed_outside_local_validator",
                    "full_current_main_checkout_probe": "not_run",
                    "all_historical_checkout_probes": "not_run",
                    "runtime_wire_observation": "not_run",
                    "cryptographically_signed_build_provenance": "not_run",
                },
            }
        )
        recorder.lines.append(f"VALIDATION FAILED: {type(error).__name__}: {error}")
        if args.debug:
            recorder.lines.append(traceback.format_exc())
    finally:
        _write_evidence(args.json.resolve(), args.log.resolve(), summary, recorder)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
