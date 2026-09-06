#!/usr/bin/env python3
"""Run the complete offline v11 release validation pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv

ROOT = Path(__file__).resolve().parent.parent
EPOCH = "315532800"


class ValidationFailure(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(
    label: str,
    command: list[str],
    log: list[str],
    *,
    cwd: Path = ROOT,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    log.append(f"$ {' '.join(command)}")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=merged_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
    )
    log.append(result.stdout.rstrip())
    if result.returncode != 0:
        raise ValidationFailure(f"{label} failed with exit status {result.returncode}")
    log.append(f"PASS: {label}")
    return result


def _compare_artifacts(first: Path, second: Path, log: list[str]) -> dict[str, str]:
    names = sorted(path.name for path in first.iterdir() if path.is_file())
    second_names = sorted(path.name for path in second.iterdir() if path.is_file())
    if names != second_names:
        raise ValidationFailure(f"distribution file sets differ: {names!r} != {second_names!r}")
    hashes: dict[str, str] = {}
    for name in names:
        left = first / name
        right = second / name
        if left.read_bytes() != right.read_bytes():
            raise ValidationFailure(f"distribution is not reproducible: {name}")
        hashes[name] = _sha256(left)
        log.append(f"reproducible: {name} sha256={hashes[name]}")
    return hashes


def validate(log_path: Path | None) -> dict[str, object]:
    log: list[str] = [
        "Codex wire audit v11 offline release validation",
        f"python={sys.version.split()[0]}",
        f"source_date_epoch={EPOCH}",
        "",
    ]
    python = sys.executable
    _run("byte compilation", [python, "-m", "py_compile", *[
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "codex_wire_audit").rglob("*.py"))
    ], "codex_wire_audit_v11.py", "test_codex_wire_audit_v11.py", "tools/build_v11_assets.py", "tools/maintainability_metrics.py", "tools/build_distribution.py", "tools/validate_release.py"], log)
    tests = _run(
        "101 unit and regression tests",
        [python, "-m", "unittest", "-q", "test_codex_wire_audit_v10.py", "test_codex_wire_audit_v11.py"],
        log,
    )
    if "Ran 101 tests" not in tests.stdout:
        raise ValidationFailure("unexpected unit-test count")
    fixtures = _run(
        "11 protocol fixtures",
        [python, "validate_codex_wire_fixtures_v10.py"],
        log,
    )
    if "fixtures: 11/11 passed" not in fixtures.stdout:
        raise ValidationFailure("unexpected fixture-validation count")
    _run("deterministic generated assets", ["make", "check-assets"], log)
    _run(
        "maintainability size and import-cycle budgets",
        [python, "tools/maintainability_metrics.py", "--check"],
        log,
    )
    _run(
        "frozen v10 SHA-256 boundary",
        [
            python,
            "-c",
            "from codex_wire_audit.legacy import verify_frozen_v10_integrity as v; assert v(include_tests=True) == (); print('frozen v10 hashes: ok')",
        ],
        log,
    )

    with tempfile.TemporaryDirectory(prefix="codex-wire-audit-v11-validation-") as directory:
        temporary = Path(directory)
        first = temporary / "dist-a"
        second = temporary / "dist-b"
        _run(
            "first reproducible distribution build",
            [python, "tools/build_distribution.py", "--output-dir", str(first)],
            log,
            env={"SOURCE_DATE_EPOCH": EPOCH},
        )
        _run(
            "second reproducible distribution build",
            [python, "tools/build_distribution.py", "--output-dir", str(second)],
            log,
            env={"SOURCE_DATE_EPOCH": EPOCH},
        )
        hashes = _compare_artifacts(first, second, log)
        wheel = next(first.glob("*.whl"))
        sdist = next(first.glob("*.tar.gz"))

        source_root = temporary / "source"
        source_root.mkdir()
        with tarfile.open(sdist, "r:gz") as archive:
            archive.extractall(source_root, filter="data")
        extracted = next(path for path in source_root.iterdir() if path.is_dir())
        wheelhouse = temporary / "roundtrip-wheel"
        wheelhouse.mkdir()
        _run(
            "source-distribution wheel round trip",
            [
                python,
                "-m",
                "pip",
                "wheel",
                str(extracted),
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheelhouse),
            ],
            log,
            env={"SOURCE_DATE_EPOCH": EPOCH, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        roundtrip_wheel = next(wheelhouse.glob("*.whl"))
        if wheel.read_bytes() != roundtrip_wheel.read_bytes():
            raise ValidationFailure("wheel rebuilt from sdist differs from direct wheel")
        log.append("PASS: source-distribution wheel is byte-identical to direct wheel")

        environment = temporary / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        bin_dir = environment / ("Scripts" if os.name == "nt" else "bin")
        venv_python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        cli = bin_dir / ("codex-wire-audit.exe" if os.name == "nt" else "codex-wire-audit")
        _run(
            "clean wheel install",
            [str(venv_python), "-m", "pip", "install", "--no-index", "--no-deps", str(wheel)],
            log,
            cwd=temporary,
            env={"PIP_DISABLE_PIP_VERSION_CHECK": "1"},
        )
        _run("installed CLI self-test", [str(cli), "--self-test"], log, cwd=temporary)
        _run(
            "installed package schema and compatibility data",
            [
                str(venv_python),
                "-c",
                "from codex_wire_audit.schemas import schema_documents; from codex_wire_audit.legacy import verify_frozen_v10_integrity as v; assert len(schema_documents()) == 7; assert v() == (); print('package data: ok')",
            ],
            log,
            cwd=temporary,
        )
        _run("installed dependency check", [str(venv_python), "-m", "pip", "check"], log, cwd=temporary)

    metrics = json.loads((ROOT / "codex_wire_audit_v11_maintainability_metrics.json").read_text())
    summary = {
        "validation_format_version": "1.0.0",
        "status": "passed",
        "unit_tests": 101,
        "v10_tests": 62,
        "v11_tests": 39,
        "protocol_fixtures": 11,
        "distribution_sha256": hashes,
        "maintainability_guardrails": metrics["guardrails"],
        "current_main_live_audit": "not_run",
        "runtime_wire_observation": "not_run",
    }
    log.append("")
    log.append(json.dumps(summary, indent=2, sort_keys=True))
    encoded = "\n".join(log).rstrip() + "\n"
    if log_path:
        log_path.write_text(encoded, encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log")
    args = parser.parse_args()
    try:
        validate(Path(args.log) if args.log else None)
    except (OSError, subprocess.SubprocessError, ValidationFailure, AssertionError) as error:
        print(f"validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
