from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile

import pytest

from codex_wire_audit import GENERATOR_VERSION
from codex_wire_audit.release_contract import (
    load_release_spec as load_installed_release_spec,
    probe_active_package,
    release_spec_digest,
)
from tools.release_common import (
    DistributionRecord,
    ReleaseError,
    add_integrity,
    deterministic_zip_bytes,
    load_release_spec,
    release_spec_sha256,
    parse_pytest_pass_count,
    source_snapshot,
    validate_source_alignment,
    verify_integrity,
    wheel_resource_entries,
)
from tools.release_pipeline import _validated_distributions

from codex_wire_audit.release_spec_validation import validate_release_spec
from tools.maintainability_metrics import build_metrics
from tools.release_archives import extract_sdist, validate_zip_archive
from tools.release_runtime import deterministic_log_text
from tools.release_publish import (
    OutputLock, publish_directory, validate_output_destination, write_failure_evidence,
)
from tools.release_spec import canonical_requirements, source_archive_prefix

ROOT = Path(__file__).resolve().parents[1]


def test_release_spec_is_single_source_of_release_identity() -> None:
    spec = load_release_spec(ROOT)
    identity = validate_source_alignment(ROOT, spec)
    assert spec["release"]["package_version"] == "11.0.0"
    assert spec["release"]["generator_version"] == GENERATOR_VERSION
    assert identity["project_version"] == GENERATOR_VERSION
    assert spec["artifacts"]["wheel"] == "codex_wire_audit-11.0.0-py3-none-any.whl"
    assert spec["artifacts"]["sdist"] == "codex_wire_audit-11.0.0.tar.gz"



def test_runtime_dependency_contract_is_bound_to_pyproject() -> None:
    spec = load_release_spec(ROOT)
    identity = validate_source_alignment(ROOT, spec)
    assert identity["runtime_dependencies"] == [
        "jsonschema<5,>=4.23",
        "referencing<1,>=0.35",
        'tomli<3,>=2; python_version < "3.11"',
    ]
    assert canonical_requirements(["jsonschema>=4.23,<5"]) == (
        "jsonschema<5,>=4.23",
    )
    with pytest.raises(ReleaseError, match="direct-URL"):
        canonical_requirements(["pkg @ https://example.invalid/pkg.whl"])

def test_package_owned_spec_and_active_probe_agree() -> None:
    source_spec = load_release_spec(ROOT)
    installed_spec = load_installed_release_spec()
    assert release_spec_sha256(source_spec) == release_spec_digest(installed_spec)
    probe = probe_active_package()
    assert probe["status"] == "passed"
    assert probe["release_spec_sha256"] == release_spec_sha256(source_spec)
    assert {"extractor.turn_metadata", "extractor.context_management"}.issubset(
        probe["active_extractors"]
    )
    claims = {item["id"]: item for item in probe["capability_claims"]}
    assert claims["context_management"]["status"] == "passed"
    assert claims["context_management"]["container"]["member_count"] == 7


def _validation_for_dist(dist: Path, spec: dict[str, object]) -> dict[str, object]:
    wheel_name = spec["artifacts"]["wheel"]  # type: ignore[index]
    sdist_name = spec["artifacts"]["sdist"]  # type: ignore[index]
    records = {}
    for name in (wheel_name, sdist_name):
        path = dist / name
        records[name] = DistributionRecord(
            name=name,
            size=path.stat().st_size,
            sha256=__import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        ).as_dict()
    return add_integrity(
        {
            "format": "codex-wire-audit-release-validation/v2",
            "status": "passed",
            "release_spec_sha256": release_spec_sha256(spec),
            "checks": {"clean_copy_distributions": records},
        }
    )


def test_builder_rejects_distribution_replaced_after_validation(tmp_path: Path) -> None:
    spec = load_release_spec(ROOT)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / spec["artifacts"]["wheel"]).write_bytes(b"wheel-a")
    (dist / spec["artifacts"]["sdist"]).write_bytes(b"sdist-a")
    validation = _validation_for_dist(dist, spec)
    (dist / spec["artifacts"]["wheel"]).write_bytes(b"wheel-b")
    with pytest.raises(ReleaseError, match="replaced after validation"):
        _validated_distributions(validation, dist, spec)


def test_builder_rejects_extra_distribution_even_when_two_expected_exist(
    tmp_path: Path,
) -> None:
    spec = load_release_spec(ROOT)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / spec["artifacts"]["wheel"]).write_bytes(b"wheel")
    (dist / spec["artifacts"]["sdist"]).write_bytes(b"sdist")
    validation = _validation_for_dist(dist, spec)
    (dist / "codex_wire_audit-7.0.0-py3-none-any.whl").write_bytes(b"stale")
    with pytest.raises(ReleaseError, match="unexpected or missing"):
        _validated_distributions(validation, dist, spec)


def test_validation_integrity_is_monotonic_and_tamper_visible() -> None:
    value = add_integrity({"status": "passed", "checks": {"wheel": "ok"}})
    verify_integrity(value, label="fixture")
    value["checks"]["wheel"] = "swapped"  # type: ignore[index]
    with pytest.raises(ReleaseError, match="integrity mismatch"):
        verify_integrity(value, label="fixture")



def test_pytest_pass_count_accepts_warning_summary() -> None:
    assert parse_pytest_pass_count("158 passed, 1 warning in 9.34s\n") == 158
    assert parse_pytest_pass_count("all good: 4 passed in 0.12s\n") == 4

def test_source_snapshot_changes_when_any_bound_source_byte_changes(
    tmp_path: Path,
) -> None:
    spec = load_release_spec(ROOT)
    (tmp_path / "codex_wire_audit").mkdir()
    (tmp_path / "codex_wire_audit/release_spec.v1.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )
    (tmp_path / "payload.txt").write_text("first", encoding="utf-8")
    _, first = source_snapshot(tmp_path, spec)
    (tmp_path / "payload.txt").write_text("second", encoding="utf-8")
    _, second = source_snapshot(tmp_path, spec)
    assert first != second


def test_active_standalone_container_can_be_derived_from_wheel_bytes(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "fixture.whl"
    deterministic_zip_bytes(
        wheel,
        [
            (
                "codex_wire_audit/context_management_container_data/index.json",
                b"index",
                0o644,
            ),
            (
                "codex_wire_audit/context_management_container_data/routes.json",
                b"routes",
                0o644,
            ),
            ("unrelated.txt", b"ignore", 0o644),
        ],
        epoch=315532800,
    )
    assert [(name, data) for name, data, _ in wheel_resource_entries(
        wheel,
        resource_prefix="codex_wire_audit/context_management_container_data",
    )] == [("index.json", b"index"), ("routes.json", b"routes")]


def test_deterministic_archive_builder_is_byte_reproducible(tmp_path: Path) -> None:
    first = tmp_path / "a.zip"
    second = tmp_path / "b.zip"
    entries = [("b.txt", b"b", 0o644), ("a.sh", b"#!/bin/sh\n", 0o755)]
    deterministic_zip_bytes(first, entries, epoch=315532800)
    deterministic_zip_bytes(second, reversed(entries), epoch=315532800)
    assert first.read_bytes() == second.read_bytes()




def test_installed_contract_has_dependency_free_validation_fallback(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-m",
            "codex_wire_audit.release_contract",
            "--output",
            "-",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "passed"
    assert payload["release_spec_validation_backend"] == "stdlib-closed-shape"

def test_documented_direct_script_entrypoint_works_outside_repo(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/release_pipeline.py"), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "release,verify" in result.stdout


def test_release_log_normalization_is_reproducible_and_redacted() -> None:
    first = deterministic_log_text(
        [
            "$ /tmp/codex-wire-audit-release-closure-one/build/.tmp-first/tool",
            "159 passed in 8.19s",
            "Authorization: Bearer secret-value",
        ],
        path_replacements={
            "/tmp/codex-wire-audit-release-closure-one": "<RELEASE_WORKSPACE>"
        },
    )
    second = deterministic_log_text(
        [
            "$ /tmp/codex-wire-audit-release-closure-two/build/.tmp-second/tool",
            "159 passed in 12.72s",
            "Authorization: Bearer another-secret",
        ],
        path_replacements={
            "/tmp/codex-wire-audit-release-closure-two": "<RELEASE_WORKSPACE>"
        },
    )
    assert first == second
    assert "<RELEASE_WORKSPACE>/build/.tmp-<id>/tool" in first
    assert "in <elapsed>s" in first
    assert "secret" not in first


def test_deterministic_archive_has_canonical_top_level_mode(tmp_path: Path) -> None:
    archive = tmp_path / "artifact.zip"
    deterministic_zip_bytes(archive, [("payload.txt", b"value", 0o644)], epoch=315532800)
    if os.name != "nt":
        assert archive.stat().st_mode & 0o777 == 0o644

def test_ci_uses_release_closure_not_retired_v13_builder() -> None:
    workflow = (ROOT / ".github/workflows/machine-proof.yml").read_text(encoding="utf-8")
    assert "tools/release_pipeline.py release" in workflow
    assert "tools/release_pipeline.py verify" in workflow
    assert "validate_v13_release.py" not in workflow
    assert "build_v13_release.py" not in workflow


def test_legacy_companions_are_forbidden_from_active_claim_binding() -> None:
    spec = load_release_spec(ROOT)
    assert spec["legacy_evidence"]["role"] == "legacy_evidence_only"
    assert spec["legacy_evidence"]["may_satisfy_active_capability_claims"] is False
    assert all(
        claim["status"] == "active_package" and claim["required_resources"]
        for claim in spec["capability_claims"]
    )



def test_release_spec_rejects_unknown_fields_and_artifact_collisions() -> None:
    spec = load_release_spec(ROOT)
    unknown = copy.deepcopy(spec)
    unknown["unexpected"] = True
    with pytest.raises(ValueError, match="schema error"):
        validate_release_spec(unknown)

    collision = copy.deepcopy(spec)
    collision["artifacts"]["bundle_sidecar"] = collision["artifacts"]["bundle"]
    with pytest.raises(ValueError, match="unique"):
        validate_release_spec(collision)


def test_output_path_cannot_destroy_or_pollute_source_tree(tmp_path: Path) -> None:
    spec = load_release_spec(ROOT)
    with pytest.raises(ReleaseError, match="source root"):
        validate_output_destination(ROOT, ROOT, spec)
    with pytest.raises(ReleaseError, match="excluded"):
        validate_output_destination(ROOT, ROOT / "ordinary-output", spec)
    accepted = validate_output_destination(ROOT, ROOT / "release_v18-test", spec)
    assert accepted.name == "release_v18-test"


def test_output_lock_rejects_concurrent_writer(tmp_path: Path) -> None:
    destination = tmp_path / "release"
    with OutputLock(destination):
        with pytest.raises(ReleaseError, match="locked"):
            with OutputLock(destination):
                pass




def test_output_lock_rejects_symlink_lock_file(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink API unavailable")
    destination = tmp_path / "release"
    target = tmp_path / "target.txt"
    target.write_text("must survive", encoding="utf-8")
    lock_path = tmp_path / ".release.release.lock"
    try:
        os.symlink(target, lock_path)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ReleaseError, match="lock path"):
        with OutputLock(destination):
            pass
    assert target.read_text(encoding="utf-8") == "must survive"

def test_output_lock_recovers_from_stale_file(tmp_path: Path) -> None:
    destination = tmp_path / "release"
    lock_path = tmp_path / ".release.release.lock"
    lock_path.write_text('{"pid": 999999999, "host": "stale"}\n', encoding="utf-8")
    with OutputLock(destination):
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    # The diagnostic file may remain, but its OS lock is no longer held.
    with OutputLock(destination):
        pass

def test_publish_requires_explicit_replace_and_preserves_closed_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "artifact.txt").write_text("new", encoding="utf-8")
    destination = tmp_path / "release"
    destination.mkdir()
    (destination / "artifact.txt").write_text("old", encoding="utf-8")
    with pytest.raises(ReleaseError, match="--replace"):
        publish_directory(source, destination, replace=False)
    assert (destination / "artifact.txt").read_text(encoding="utf-8") == "old"
    publish_directory(source, destination, replace=True)
    assert (destination / "artifact.txt").read_text(encoding="utf-8") == "new"


def test_zip_verifier_rejects_duplicate_and_casefold_colliding_members(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("same.txt", b"a")
            archive.writestr("same.txt", b"b")
    with pytest.raises(ReleaseError, match="duplicate"):
        validate_zip_archive(duplicate)

    collision = tmp_path / "collision.zip"
    with zipfile.ZipFile(collision, "w") as archive:
        archive.writestr("A.txt", b"a")
        archive.writestr("a.txt", b"b")
    with pytest.raises(ReleaseError, match="normalized path collision"):
        validate_zip_archive(collision)



def test_archive_validation_rejects_noncanonical_permissions(tmp_path: Path) -> None:
    bad_zip = tmp_path / "bad-mode.zip"
    info = zipfile.ZipInfo("payload.txt")
    info.create_system = 3
    info.external_attr = (0o100000 | 0o4755) << 16
    with zipfile.ZipFile(bad_zip, "w") as archive:
        archive.writestr(info, b"payload")
    with pytest.raises(ReleaseError, match="non-canonical permissions"):
        validate_zip_archive(bad_zip)

    bad_sdist = tmp_path / "bad-mode.tar.gz"
    with tarfile.open(bad_sdist, "w:gz") as archive:
        directory = tarfile.TarInfo("fixture-1.0.0")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)
        member = tarfile.TarInfo("fixture-1.0.0/payload.sh")
        member.mode = 0o4755
        member.size = len(b"payload")
        archive.addfile(member, io.BytesIO(b"payload"))
    with pytest.raises(ReleaseError, match="non-canonical permissions"):
        extract_sdist(bad_sdist, tmp_path / "extract")

def test_source_snapshot_rejects_symlinks(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink API unavailable")
    spec = load_release_spec(ROOT)
    (tmp_path / "real.txt").write_text("value", encoding="utf-8")
    try:
        os.symlink(tmp_path / "real.txt", tmp_path / "alias.txt")
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ReleaseError, match="non-regular file"):
        source_snapshot(tmp_path, spec)


def test_failure_evidence_is_persisted_and_secret_redacted(tmp_path: Path) -> None:
    spec = load_release_spec(ROOT)
    output = tmp_path / "release"
    result = write_failure_evidence(
        output,
        operation="release",
        error=RuntimeError("Authorization: Bearer secret-token"),
        spec=spec,
        phase="test",
        log=["access_token=very-secret"],
    )
    assert result is not None
    record_path, log_path = result
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert "secret-token" not in json.dumps(record)
    assert "very-secret" not in log_path.read_text(encoding="utf-8")
    assert "<redacted>" in record["message"]


def test_release_pipeline_is_series_driven_and_tooling_is_budgeted() -> None:
    spec = load_release_spec(ROOT)
    assert source_archive_prefix(spec) == "codex_wire_audit_v18_source"
    pipeline = (ROOT / "tools/release_pipeline.py").read_text(encoding="utf-8")
    assert "codex_wire_audit_v17_source" not in pipeline
    assert "release_v17" not in pipeline
    metrics = build_metrics()
    assert metrics["active_release_tools"]["module_count"] >= 8
    assert not metrics["active_implementation"]["modules_over_600_lines"]
    assert all(metrics["guardrails"]["passes"].values())


def test_ci_runs_deep_verification_for_the_release_job() -> None:
    workflow = (ROOT / ".github/workflows/machine-proof.yml").read_text(encoding="utf-8")
    assert "machine-proof-v18" in workflow
    verify_section = workflow.split("Independently verify assembled release", 1)[1]
    assert "tools/release_pipeline.py verify" in verify_section
    assert "--fast" not in verify_section


def test_wheel_normalization_canonicalizes_generated_record_mode(tmp_path: Path) -> None:
    from tools.build_distribution import normalize_wheel

    wheel = tmp_path / "fixture-1.0.0-py3-none-any.whl"
    payload = zipfile.ZipInfo("fixture/__init__.py", (2026, 1, 2, 3, 4, 6))
    payload.create_system = 3
    payload.external_attr = (0o100000 | 0o644) << 16
    record = zipfile.ZipInfo("fixture-1.0.0.dist-info/RECORD", (2026, 1, 2, 3, 4, 6))
    record.create_system = 3
    record.external_attr = (0o100000 | 0o664) << 16
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(payload, b"VALUE = 1\n")
        archive.writestr(record, b"fixture/__init__.py,,\nfixture-1.0.0.dist-info/RECORD,,\n")

    normalize_wheel(wheel, epoch=315532800)

    with zipfile.ZipFile(wheel) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos][-1].endswith(".dist-info/RECORD")
        assert {info.date_time for info in infos} == {(1980, 1, 1, 0, 0, 0)}
        assert {
            info.filename: (info.external_attr >> 16) & 0o7777 for info in infos
        } == {
            "fixture/__init__.py": 0o644,
            "fixture-1.0.0.dist-info/RECORD": 0o644,
        }
        assert archive.read("fixture/__init__.py") == b"VALUE = 1\n"


def test_packaging_configuration_and_build_output_are_warning_free() -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    from tools.release_runtime import validate_build_output

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["setuptools"]["packages"]["find"]["namespaces"] is True
    validate_build_output("running sdist\nrunning bdist_wheel\n")
    with pytest.raises(ReleaseError, match="packaging warning"):
        validate_build_output("setuptools.command.build_py: _Warning: package would be ignored")
