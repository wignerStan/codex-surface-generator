#!/usr/bin/env python3
"""Hermetic-ish validation for the v16 Context Management release.

The validator deliberately checks the installable artifact, not only the source tree:
- full tests and byte compilation;
- all installed Draft 2020-12 schemas;
- closed Context Management reference container;
- executable v16 coverage profiles and extractor registration;
- two clean-copy distribution builds must be byte-identical;
- the wheel must contain the active extractor, schema, profile and reference data;
- the sdist must rerun the complete test suite;
- a clean venv must install the wheel and verify the packaged reference container.
"""
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
import zipfile

ROOT = Path(__file__).resolve().parent.parent
RELEASE_VERSION = "9.0.0"
SOURCE_DATE_EPOCH = "315532800"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    merged = os.environ.copy()
    merged["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    if env:
        merged.update(env)
    result = subprocess.run(command, cwd=cwd, env=merged, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout + result.stderr


def copy_source(destination: Path) -> None:
    excluded = {
        ".git", ".pytest_cache", "__pycache__", "build", "dist", "release_v13",
        "release_v16", "ci-out", "htmlcov", ".coverage",
    }
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in excluded or name.endswith(".egg-info")}
        if Path(directory) == ROOT:
            ignored.update(name for name in names if name.startswith("codex_wire_audit_v16_validation"))
        return ignored
    shutil.copytree(ROOT, destination, ignore=ignore)


def build_from_clean_copy(base: Path, name: str) -> tuple[Path, dict[str, str]]:
    source = base / name
    copy_source(source)
    dist = source / "dist"
    run([sys.executable, "tools/build_distribution.py", "--output-dir", str(dist)], cwd=source)
    artifacts = {path.name: sha256(path) for path in sorted(dist.iterdir()) if path.is_file()}
    return dist, artifacts


def schema_check() -> int:
    sys.path.insert(0, str(ROOT))
    from jsonschema import Draft202012Validator
    from codex_wire_audit.proof_schema_validation import load_schemas
    by_id, by_path = load_schemas()
    if not by_path:
        raise RuntimeError("no JSON Schema documents found")
    for schema in by_path.values():
        Draft202012Validator.check_schema(schema)
    return len(by_path)


def package_checks() -> dict[str, object]:
    sys.path.insert(0, str(ROOT))
    import codex_wire_audit
    from codex_wire_audit.context_management_reference import verify_context_management_container
    from codex_wire_audit.extractors import create_extractors
    from codex_wire_audit.proof_profiles import resolve_profile

    if codex_wire_audit.GENERATOR_VERSION != RELEASE_VERSION:
        raise RuntimeError("generator version does not match v16 release version")
    extractor_ids = {item.extractor_id for item in create_extractors()}
    if "extractor.context_management" not in extractor_ids:
        raise RuntimeError("context management extractor is not active")
    profile = resolve_profile("hybrid_v16")
    if "extractor.context_management" not in profile.required_extractors:
        raise RuntimeError("hybrid_v16 does not require context management extractor")
    full = resolve_profile("codex_wire_full")
    for scenario in ("context_management_activation", "context_window_rollover", "history_notes_retrieval"):
        if scenario not in full.required_runtime_scenarios:
            raise RuntimeError(f"codex_wire_full missing runtime scenario: {scenario}")
    return {
        "generator_version": codex_wire_audit.GENERATOR_VERSION,
        "extractor_count": len(extractor_ids),
        "context_reference": verify_context_management_container(),
    }


def wheel_check(wheel: Path) -> dict[str, object]:
    required = {
        "codex_wire_audit/extractors/context_management.py",
        "codex_wire_audit/context_management_reference.py",
        "codex_wire_audit/coverage_profiles.v3.json",
        "codex_wire_audit/proof_schema_templates/context-management-semantics-v1.schema.json",
        "codex_wire_audit/context_management_container_data/index.json",
        "codex_wire_audit/context_management_container_data/current_main.json",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = required - names
        if missing:
            raise RuntimeError(f"wheel missing v16 package data: {sorted(missing)}")
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise RuntimeError("wheel METADATA member unresolved")
        text = archive.read(metadata[0]).decode("utf-8", errors="replace")
        if f"Version: {RELEASE_VERSION}" not in text:
            raise RuntimeError("wheel metadata version mismatch")
        if archive.testzip() is not None:
            raise RuntimeError("wheel CRC verification failed")
    return {"members": len(names), "sha256": sha256(wheel)}


def sdist_test(sdist: Path, base: Path) -> int:
    extract = base / "sdist-extracted"
    extract.mkdir()
    with tarfile.open(sdist, "r:gz") as archive:
        archive.extractall(extract, filter="data")
    roots = [p for p in extract.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError("unexpected sdist root layout")
    output = run([sys.executable, "-m", "pytest", "-q"], cwd=roots[0])
    for line in reversed(output.splitlines()):
        if "passed" in line:
            parts = line.split()
            try:
                return int(parts[0])
            except (ValueError, IndexError):
                break
    raise RuntimeError("could not recover pytest pass count from sdist rerun")


def wheel_install_test(wheel: Path, base: Path) -> None:
    target = base / "wheel-venv"
    builder = venv.EnvBuilder(with_pip=True, system_site_packages=True, clear=True)
    builder.create(target)
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], cwd=base)
    code = (
        "import codex_wire_audit; "
        "from codex_wire_audit.context_management_reference import verify_context_management_container; "
        "assert codex_wire_audit.GENERATOR_VERSION == '9.0.0'; "
        "r=verify_context_management_container(); "
        "assert r['reviewed_commit']=='6af345407d9c2a568da9d01b6c4b81a9e61495c0'; "
        "print(r['member_count'])"
    )
    run([str(python), "-c", code], cwd=base)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", default=str(ROOT / "codex_wire_audit_v16_validation.json"))
    parser.add_argument("--log", default=str(ROOT / "codex_wire_audit_v16_validation.txt"))
    parser.add_argument("--dist-dir", default=str(ROOT / "dist"))
    args = parser.parse_args()
    output_json = Path(args.json).resolve()
    output_log = Path(args.log).resolve()
    log_lines: list[str] = []
    record: dict[str, object] = {
        "format": "codex-wire-audit-v16-validation/v1",
        "release_version": RELEASE_VERSION,
        "reviewed_codex_commit": "6af345407d9c2a568da9d01b6c4b81a9e61495c0",
        "status": "failed",
        "checks": {},
    }
    try:
        log_lines.append(run([sys.executable, "-m", "compileall", "-q", "codex_wire_audit", "tests", "tools", "scripts"]))
        pytest_output = run([sys.executable, "-m", "pytest", "-q"])
        log_lines.append(pytest_output)
        passed = None
        for line in reversed(pytest_output.splitlines()):
            if "passed" in line:
                try:
                    passed = int(line.split()[0])
                except (ValueError, IndexError):
                    pass
                break
        record["checks"] = {
            "source_pytest_passed": passed,
            "schema_documents": schema_check(),
            "package": package_checks(),
        }
        with tempfile.TemporaryDirectory(prefix="codex-wire-audit-v16-") as temporary:
            base = Path(temporary)
            dist_a, hashes_a = build_from_clean_copy(base, "build-a")
            dist_b, hashes_b = build_from_clean_copy(base, "build-b")
            if hashes_a != hashes_b:
                raise RuntimeError(f"clean-copy distributions are not reproducible: {hashes_a} != {hashes_b}")
            wheel = next(dist_a.glob("*.whl"))
            sdist = next(dist_a.glob("*.tar.gz"))
            checks = record["checks"]
            assert isinstance(checks, dict)
            checks["clean_copy_distribution_hashes"] = hashes_a
            checks["wheel"] = wheel_check(wheel)
            checks["sdist_pytest_passed"] = sdist_test(sdist, base)
            wheel_install_test(wheel, base)
            checks["wheel_install"] = "passed"
            # Publish one verified distribution set for the delivery builder.
            dist_target = Path(args.dist_dir).resolve()
            if dist_target.exists():
                shutil.rmtree(dist_target)
            shutil.copytree(dist_a, dist_target)
        record["status"] = "passed"
    except Exception as error:
        record["error"] = str(error)
        log_lines.append(f"ERROR: {error}\n")
    finally:
        encoded = json.dumps(record, indent=2, sort_keys=True) + "\n"
        output_json.write_text(encoded, encoding="utf-8")
        output_log.write_text("\n".join(log_lines), encoding="utf-8")
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
