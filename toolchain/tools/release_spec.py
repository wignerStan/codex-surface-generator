"""Release-spec loading, source identity, and snapshot ownership."""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
import stat
from typing import Any, Iterable, Mapping, Sequence

try:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
except ModuleNotFoundError:  # stable operational error is raised when used
    InvalidRequirement = ValueError  # type: ignore[assignment]
    Requirement = None  # type: ignore[assignment]
    canonicalize_name = None  # type: ignore[assignment]

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib  # type: ignore[no-redef]

from codex_wire_audit.release_spec_validation import (
    SCHEMA_RESOURCE,
    SPEC_RESOURCE,
    collision_key,
    safe_relative_path,
    validate_release_spec,
)
from tools.release_integrity import canonical_json, load_json_object, sha256_bytes
from tools.release_types import ReleaseError, SourceRecord

SPEC_RELATIVE = Path("codex_wire_audit") / SPEC_RESOURCE



def canonical_requirement(value: str) -> str:
    """Return a stable PEP 508 representation and reject URL dependencies."""
    if Requirement is None or canonicalize_name is None:
        raise ReleaseError(
            "release tooling dependency 'packaging' is missing; install the project test extra"
        )
    try:
        requirement = Requirement(value)
    except InvalidRequirement as error:
        raise ReleaseError(f"invalid dependency requirement {value!r}: {error}") from error
    if requirement.url is not None:
        raise ReleaseError(f"direct-URL dependencies are not allowed in a closed release: {value!r}")
    name = canonicalize_name(requirement.name)
    extras = f"[{','.join(sorted(requirement.extras))}]" if requirement.extras else ""
    specifier = str(requirement.specifier)
    marker = f"; {requirement.marker}" if requirement.marker is not None else ""
    return f"{name}{extras}{specifier}{marker}"


def canonical_requirements(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(canonical_requirement(value) for value in values))
    if len(normalized) != len(set(normalized)):
        raise ReleaseError("dependency list contains duplicate normalized requirements")
    return normalized

def load_release_spec(root: Path) -> dict[str, Any]:
    spec = load_json_object(root / SPEC_RELATIVE)
    try:
        validate_release_spec(spec)
    except ValueError as error:
        raise ReleaseError(str(error), phase="load_release_spec") from error
    return spec


def release_spec_sha256(spec: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(spec))


def source_archive_prefix(spec: Mapping[str, Any]) -> str:
    return f"codex_wire_audit_{spec['release']['series']}_source"


def default_output_directory(root: Path, spec: Mapping[str, Any]) -> Path:
    return root / f"release_{spec['release']['series']}"


def required_package_resources(spec: Mapping[str, Any]) -> set[str]:
    result = {
        SPEC_RESOURCE,
        SCHEMA_RESOURCE,
        "release_spec_validation.py",
        "release_contract.py",
        "__init__.py",
    }
    for claim in spec["capability_claims"]:
        result.update(claim.get("required_resources", []))
    return result


def _load_pyproject(root: Path) -> Mapping[str, Any]:
    path = root / "pyproject.toml"
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseError(f"cannot parse pyproject.toml: {error}") from error
    if not isinstance(value, Mapping):
        raise ReleaseError("pyproject.toml is not a table")
    return value


def _generator_version(init_path: Path) -> str | None:
    import ast

    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    except (OSError, UnicodeDecodeError, SyntaxError) as error:
        raise ReleaseError(f"cannot parse package __init__.py: {error}") from error
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "GENERATOR_VERSION"
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    return None


def validate_source_alignment(root: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    release = spec["release"]
    pyproject = _load_pyproject(root)
    project = pyproject.get("project")
    if not isinstance(project, Mapping):
        raise ReleaseError("pyproject.toml has no [project] table")
    generator_version = _generator_version(root / "codex_wire_audit/__init__.py")
    expected = {
        "project_name": release["package_name"],
        "project_version": release["package_version"],
        "generator_version": release["generator_version"],
        "project_readme": spec["artifacts"]["readme"],
    }
    project_dependencies = project.get("dependencies", [])
    if not isinstance(project_dependencies, list) or any(
        not isinstance(item, str) for item in project_dependencies
    ):
        raise ReleaseError("pyproject runtime dependencies are malformed")
    expected_dependencies = canonical_requirements(release["runtime_dependencies"])
    observed_dependencies = canonical_requirements(project_dependencies)
    expected["runtime_dependencies"] = list(expected_dependencies)
    observed = {
        "project_name": project.get("name"),
        "project_version": project.get("version"),
        "generator_version": generator_version,
        "project_readme": project.get("readme"),
        "runtime_dependencies": list(observed_dependencies),
    }
    if observed != expected:
        raise ReleaseError(f"source release identity mismatch: expected {expected}, got {observed}")

    normalized_name = release["package_name"].replace("-", "_")
    expected_wheel = f"{normalized_name}-{release['package_version']}-py3-none-any.whl"
    expected_sdist = f"{normalized_name}-{release['package_version']}.tar.gz"
    if spec["artifacts"]["wheel"] != expected_wheel:
        raise ReleaseError("release specification wheel name does not match package identity")
    if spec["artifacts"]["sdist"] != expected_sdist:
        raise ReleaseError("release specification sdist name does not match package identity")

    for document_key in ("readme", "release_notes"):
        path = root / spec["artifacts"][document_key]
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"release document is missing or not regular: {path.name}")

    setuptools = pyproject.get("tool", {}).get("setuptools", {}) if isinstance(pyproject.get("tool"), Mapping) else {}
    package_data = setuptools.get("package-data", {}) if isinstance(setuptools, Mapping) else {}
    declared = package_data.get(release["package_import"], []) if isinstance(package_data, Mapping) else []
    if not isinstance(declared, list):
        raise ReleaseError("package-data declaration is malformed")
    for required in (SPEC_RESOURCE, SCHEMA_RESOURCE):
        if required not in declared:
            raise ReleaseError(f"{required} is not declared as package data")
    return observed


def source_path_is_excluded(relative: Path, spec: Mapping[str, Any]) -> bool:
    snapshot = spec.get("source_snapshot", {})
    excluded_names = set(snapshot.get("exclude_directory_names", []))
    excluded_suffixes = tuple(snapshot.get("exclude_directory_suffixes", []))
    root_directory_globs = tuple(snapshot.get("exclude_root_directory_globs", []))
    parts = relative.parts
    for index, part in enumerate(parts[:-1]):
        if part in excluded_names or any(part.endswith(suffix) for suffix in excluded_suffixes):
            return True
        if index == 0 and any(fnmatch.fnmatch(part, pattern) for pattern in root_directory_globs):
            return True
    if len(parts) == 1:
        for pattern in snapshot.get("exclude_root_globs", []):
            if fnmatch.fnmatch(relative.name, pattern):
                return True
    return False


def _walk_source(root: Path, spec: Mapping[str, Any]) -> list[Path]:
    files: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root)
        kept_dirs: list[str] = []
        for name in sorted(dirnames):
            path = directory_path / name
            relative = path.relative_to(root)
            if source_path_is_excluded(relative / "__probe__", spec):
                continue
            if path.is_symlink():
                raise ReleaseError(f"source snapshot contains a directory symlink: {relative.as_posix()}")
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root)
            if source_path_is_excluded(relative, spec):
                continue
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ReleaseError(f"source snapshot contains a non-regular file: {relative.as_posix()}")
            if not safe_relative_path(relative.as_posix()):
                raise ReleaseError(f"source snapshot contains an unsafe path: {relative.as_posix()}")
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def source_snapshot(root: Path, spec: Mapping[str, Any]) -> tuple[list[SourceRecord], str]:
    records: list[SourceRecord] = []
    digest = hashlib.sha256()
    normalized_paths: dict[str, str] = {}
    for path in _walk_source(root, spec):
        relative = path.relative_to(root).as_posix()
        key = collision_key(relative)
        if key in normalized_paths:
            raise ReleaseError(
                f"source snapshot normalized path collision: {normalized_paths[key]!r}, {relative!r}"
            )
        normalized_paths[key] = relative
        data = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode not in {0o644, 0o755}:
            raise ReleaseError(f"source file mode must be 0644 or 0755: {relative} ({mode:04o})")
        record = SourceRecord(relative, len(data), sha256_bytes(data), mode)
        records.append(record)
        digest.update(record.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(f"{record.mode:04o}".encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    if not records:
        raise ReleaseError("source snapshot is empty")
    return records, digest.hexdigest()


def verify_source_snapshot(
    root: Path,
    records: Sequence[SourceRecord],
    expected_digest: str,
    spec: Mapping[str, Any] | None = None,
) -> None:
    effective_spec = spec or load_release_spec(root)
    current, digest = source_snapshot(root, effective_spec)
    if digest != expected_digest or [item.as_dict() for item in current] != [item.as_dict() for item in records]:
        raise ReleaseError("source tree changed after validation snapshot")


def copy_source_snapshot(source: Path, destination: Path, records: Sequence[SourceRecord]) -> None:
    if destination.exists():
        raise ReleaseError(f"source staging destination already exists: {destination}")
    destination.mkdir(parents=True)
    for record in records:
        src = source / record.path
        if src.is_symlink() or not src.is_file():
            raise ReleaseError(f"source changed type while staging: {record.path}")
        data = src.read_bytes()
        if len(data) != record.size or sha256_bytes(data) != record.sha256:
            raise ReleaseError(f"source changed while staging: {record.path}")
        target = destination / record.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(record.mode)
