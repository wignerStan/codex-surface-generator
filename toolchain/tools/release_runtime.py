"""Hermetic command execution, package builds, and installed-package probes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import venv
from typing import Any, Mapping, Sequence

from tools.release_archives import extract_sdist
from tools.release_integrity import load_json_object, sha256_path, verify_integrity
from tools.release_spec import release_spec_sha256
from tools.release_types import DistributionRecord, ReleaseError

MAX_CAPTURED_OUTPUT_BYTES = 2 * 1024 * 1024
_SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "TMP", "TEMP", "TMPDIR", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
}
_PACKAGING_WARNING_MARKERS = (
    "_warning:",
    "warning:",
    "setuptoolsdeprecationwarning",
    "package would be ignored",
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\]}]+"),
    re.compile(
        r'''(?ix)(["']?(?:access_token|refresh_token|id_token|api_key|personal_access_token|secret_access_key|session_token|authorization_code|code_verifier|user_code)["']?\s*[:=]\s*)["']?[^\s,"'\]}]+'''
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
)


def redact_text(text: str) -> str:
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "<redacted>", result)
    return result


def _bounded_output(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_CAPTURED_OUTPUT_BYTES:
        return text
    half = MAX_CAPTURED_OUTPUT_BYTES // 2
    head = encoded[:half].decode("utf-8", errors="replace")
    tail = encoded[-half:].decode("utf-8", errors="replace")
    return f"{head}\n... <output truncated: {len(encoded)} bytes total> ...\n{tail}"


def _command_environment(source_date_epoch: int, home: Path, extra_env: Mapping[str, str] | None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    env.update(
        {
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "PIP_CACHE_DIR": str(home / ".cache" / "pip"),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "PIP_NO_INDEX": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    if extra_env:
        forbidden = [key for key in extra_env if re.search(r"(?i)(TOKEN|SECRET|PASSWORD|API_KEY|AUTHORIZATION)", key)]
        if forbidden:
            raise ReleaseError(f"refusing sensitive extra environment keys: {sorted(forbidden)}")
        env.update(extra_env)
    return env


def run(
    command: Sequence[str],
    *,
    cwd: Path,
    source_date_epoch: int,
    log: list[str] | None = None,
    timeout: int = 600,
    extra_env: Mapping[str, str] | None = None,
) -> str:
    if not cwd.is_dir():
        raise ReleaseError(f"command working directory does not exist: {cwd}")
    printable = shlex.join(str(item) for item in command)
    if log is not None:
        log.append(f"$ {printable}")
    with tempfile.TemporaryDirectory(prefix="codex-wire-audit-command-home-") as home_name:
        home = Path(home_name)
        env = _command_environment(source_date_epoch, home, extra_env)
        try:
            result = subprocess.run(
                [str(item) for item in command],
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            partial = error.stdout if isinstance(error.stdout, str) else ""
            output = _bounded_output(redact_text(partial))
            if log is not None and output:
                log.append(output.rstrip())
            raise ReleaseError(f"command timed out after {timeout}s: {printable}") from error
    output = _bounded_output(redact_text(result.stdout))
    if log is not None and output:
        log.append(output.rstrip())
    if result.returncode != 0:
        raise ReleaseError(f"command failed ({result.returncode}): {printable}\n{output}")
    return output



def deterministic_log_text(
    entries: Sequence[str],
    *,
    path_replacements: Mapping[str, str] | None = None,
) -> str:
    """Return a redacted, path-stable transcript suitable for reproducible releases."""
    text = redact_text("\n".join(entries)).rstrip() + "\n"
    replacements = path_replacements or {}
    for source, replacement in sorted(
        ((str(source), target) for source, target in replacements.items()),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if source:
            text = text.replace(source, replacement)
            text = text.replace(source.replace("\\", "/"), replacement)
    text = re.sub(r"(?<=/)\.tmp-[A-Za-z0-9_-]+", ".tmp-<id>", text)
    text = re.sub(
        r"codex-wire-audit-(?:command-home|deep-verify|release-closure)-[A-Za-z0-9_-]+",
        lambda match: match.group(0).rsplit("-", 1)[0] + "-<id>",
        text,
    )
    text = re.sub(r"\bin [0-9]+(?:\.[0-9]+)?s\b", "in <elapsed>s", text)
    return text

def validate_build_output(output: str) -> None:
    """Reject successful builds that emitted packaging warnings."""
    lowered = output.casefold()
    marker = next((value for value in _PACKAGING_WARNING_MARKERS if value in lowered), None)
    if marker is not None:
        raise ReleaseError(f"distribution build emitted a packaging warning ({marker})")


def build_distributions(
    source_root: Path,
    output_dir: Path,
    spec: Mapping[str, Any],
    *,
    log: list[str],
) -> dict[str, DistributionRecord]:
    if output_dir.exists():
        raise ReleaseError(f"distribution output already exists: {output_dir}")
    build_output = run(
        [sys.executable, "tools/build_distribution.py", "--output-dir", str(output_dir)],
        cwd=source_root,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    validate_build_output(build_output)
    expected = {spec["artifacts"]["wheel"], spec["artifacts"]["sdist"]}
    entries = list(output_dir.iterdir())
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ReleaseError("distribution directory contains a non-regular entry")
    observed = {path.name for path in entries}
    if observed != expected:
        raise ReleaseError(f"distribution set mismatch: expected {sorted(expected)}, got {sorted(observed)}")
    return {
        name: DistributionRecord(name, (output_dir / name).stat().st_size, sha256_path(output_dir / name))
        for name in sorted(expected)
    }


def compare_distribution_sets(
    first: Mapping[str, DistributionRecord],
    second: Mapping[str, DistributionRecord],
) -> None:
    if {key: value.as_dict() for key, value in first.items()} != {
        key: value.as_dict() for key, value in second.items()
    }:
        raise ReleaseError("clean source copies produced different distributions")


def parse_pytest_pass_count(output: str) -> int:
    for line in reversed(output.splitlines()):
        match = re.search(r"(?:^|\s)(\d+)\s+passed\b", line)
        if match:
            return int(match.group(1))
    raise ReleaseError("pytest pass count was not found")


def _parse_probe(output: str, spec: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    try:
        probe = json.loads(output)
    except json.JSONDecodeError as error:
        raise ReleaseError(f"{label} returned invalid JSON: {error}") from error
    if not isinstance(probe, dict):
        raise ReleaseError(f"{label} did not return a JSON object")
    verify_integrity(probe, label=label)
    if probe.get("status") != "passed":
        raise ReleaseError(f"{label} failed: {probe.get('errors')}")
    if probe.get("release_spec_sha256") != release_spec_sha256(spec):
        raise ReleaseError(f"{label} belongs to a different release specification")
    return probe


def source_package_probe(source_root: Path, spec: Mapping[str, Any], *, log: list[str]) -> dict[str, Any]:
    output = run(
        [sys.executable, "-m", "codex_wire_audit.release_contract", "--output", "-"],
        cwd=source_root,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    return _parse_probe(output, spec, label="source capability probe")


def installed_wheel_probe(
    wheel: Path,
    spec: Mapping[str, Any],
    base: Path,
    *,
    log: list[str],
) -> dict[str, Any]:
    base.mkdir(parents=True, exist_ok=True)
    environment = base / "installed-wheel"
    if environment.exists():
        raise ReleaseError(f"wheel probe environment already exists: {environment}")
    venv.EnvBuilder(with_pip=True, clear=False, system_site_packages=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run(
        [str(python), "-m", "pip", "install", "--no-deps", "--no-index", "--no-input", str(wheel)],
        cwd=base,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    output = run(
        [
            str(python),
            "-m",
            "codex_wire_audit.release_contract",
            "--require-distribution-metadata",
            "--output",
            "-",
        ],
        cwd=base,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    return _parse_probe(output, spec, label="installed capability probe")


def validate_sdist_roundtrip(
    sdist: Path,
    direct_wheel: Path,
    spec: Mapping[str, Any],
    base: Path,
    *,
    log: list[str],
) -> dict[str, Any]:
    base.mkdir(parents=True, exist_ok=True)
    source = extract_sdist(sdist, base / "sdist-source")
    test_output = run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=source,
        source_date_epoch=spec["release"]["source_date_epoch"],
        log=log,
    )
    roundtrip_dist = base / "sdist-dist"
    records = build_distributions(source, roundtrip_dist, spec, log=log)
    rebuilt = roundtrip_dist / spec["artifacts"]["wheel"]
    if rebuilt.read_bytes() != direct_wheel.read_bytes():
        raise ReleaseError("wheel rebuilt from sdist is not byte-identical to direct wheel")
    return {
        "pytest_passed": parse_pytest_pass_count(test_output),
        "rebuilt_wheel": records[spec["artifacts"]["wheel"]].as_dict(),
        "byte_identical_to_direct_wheel": True,
    }
