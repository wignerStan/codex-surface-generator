"""Deterministic archive construction and fail-closed archive inspection."""
from __future__ import annotations

import base64
import copy
import csv
import datetime
import gzip
import hashlib
import io
from email import policy
from email.parser import Parser
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
from typing import Any, Iterable, Mapping, Sequence
import zipfile

from codex_wire_audit.release_spec_validation import collision_key, safe_relative_path
from tools.release_integrity import sha256_bytes, sha256_path
from tools.release_types import ReleaseError, SourceRecord

ZIP_MIN_EPOCH = 315532800
DEFAULT_MAX_ARCHIVE_MEMBERS = 100_000
DEFAULT_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_MEMBER_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_COMPRESSION_RATIO = 2_000


def _validate_archive_names(names: Sequence[str], *, label: str) -> None:
    if len(names) != len(set(names)):
        raise ReleaseError(f"{label} contains duplicate member names")
    normalized: dict[str, str] = {}
    for name in names:
        if not safe_relative_path(name):
            raise ReleaseError(f"{label} contains unsafe member name: {name!r}")
        key = collision_key(name)
        previous = normalized.get(key)
        if previous is not None:
            raise ReleaseError(f"{label} contains a normalized path collision: {previous!r}, {name!r}")
        normalized[key] = name


def validate_zip_archive(
    path: Path,
    *,
    expected_names: set[str] | None = None,
    allow_directories: bool = False,
    max_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> dict[str, zipfile.ZipInfo]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise ReleaseError(f"cannot open ZIP archive {path}: {error}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > max_members:
            raise ReleaseError(f"ZIP archive has too many members: {path.name}")
        names = [info.filename for info in infos]
        _validate_archive_names(names, label=f"ZIP {path.name}")
        total = 0
        by_name: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            if info.flag_bits & 0x1:
                raise ReleaseError(f"encrypted ZIP member is not allowed: {info.filename}")
            if info.is_dir():
                if not allow_directories:
                    raise ReleaseError(f"directory ZIP member is not allowed: {info.filename}")
            else:
                total += info.file_size
                if info.file_size > DEFAULT_MAX_MEMBER_BYTES:
                    raise ReleaseError(f"ZIP member exceeds size limit: {info.filename}")
                if info.compress_size == 0 and info.file_size:
                    raise ReleaseError(f"ZIP member has invalid compressed size: {info.filename}")
                if info.compress_size and info.file_size / info.compress_size > DEFAULT_MAX_COMPRESSION_RATIO:
                    raise ReleaseError(f"ZIP member compression ratio is suspicious: {info.filename}")
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG}:
                    raise ReleaseError(f"ZIP member is not a regular file: {info.filename}")
                permissions = stat.S_IMODE(mode)
                if permissions not in {0, 0o644, 0o755}:
                    raise ReleaseError(
                        f"ZIP member has non-canonical permissions: {info.filename} ({permissions:04o})"
                    )
            by_name[info.filename] = info
        if total > max_uncompressed_bytes:
            raise ReleaseError(f"ZIP archive exceeds uncompressed size limit: {path.name}")
        if expected_names is not None and set(names) != expected_names:
            missing = sorted(expected_names - set(names))
            extra = sorted(set(names) - expected_names)
            raise ReleaseError(f"ZIP member set mismatch for {path.name}: missing={missing}, extra={extra}")
        bad = archive.testzip()
        if bad is not None:
            raise ReleaseError(f"ZIP CRC failure: {bad}")
        return by_name


def zip_member_bytes(path: Path, *, expected_names: set[str] | None = None) -> dict[str, bytes]:
    infos = validate_zip_archive(path, expected_names=expected_names)
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in infos}


def zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    moment = datetime.datetime.fromtimestamp(max(epoch, ZIP_MIN_EPOCH), tz=datetime.timezone.utc)
    second = moment.second - (moment.second % 2)
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, second)


def deterministic_zip_bytes(
    destination: Path,
    entries: Iterable[tuple[str, bytes, int]],
    *,
    epoch: int,
) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    seen: set[str] = set()
    collision_names: dict[str, str] = {}
    timestamp = zip_timestamp(epoch)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data, mode in sorted(entries, key=lambda item: item[0]):
                if mode not in {0o644, 0o755}:
                    raise ReleaseError(f"non-canonical ZIP member mode for {name}: {mode:04o}")
                if name in seen or not safe_relative_path(name):
                    raise ReleaseError(f"unsafe or duplicate ZIP member: {name}")
                key = collision_key(name)
                if key in collision_names:
                    raise ReleaseError(
                        f"normalized ZIP member collision: {collision_names[key]!r}, {name!r}"
                    )
                if len(data) > DEFAULT_MAX_MEMBER_BYTES:
                    raise ReleaseError(f"ZIP member exceeds size limit: {name}")
                seen.add(name)
                collision_names[key] = name
                info = zipfile.ZipInfo(name, timestamp)
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                info.flag_bits |= 0x800
                archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    infos = validate_zip_archive(destination, expected_names=seen)
    return {
        "name": destination.name,
        "size": destination.stat().st_size,
        "sha256": sha256_path(destination),
        "member_count": len(infos),
        "uncompressed_bytes": sum(info.file_size for info in infos.values()),
    }


def _wheel_record_digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")


def _read_email_header(text: str, key: str) -> str | None:
    prefix = f"{key}: "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def inspect_wheel(
    wheel: Path,
    spec: Mapping[str, Any],
    *,
    required_resources: set[str],
) -> dict[str, Any]:
    if wheel.name != spec["artifacts"]["wheel"]:
        raise ReleaseError(f"unexpected wheel filename: {wheel.name}")
    infos = validate_zip_archive(wheel)
    names = list(infos)
    with zipfile.ZipFile(wheel) as archive:
        if any(name.endswith(".pyc") or "/__pycache__/" in name for name in names):
            raise ReleaseError("wheel must not contain interpreter-specific bytecode")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        record_names = [name for name in names if name.endswith(".dist-info/RECORD")]
        wheel_names = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(metadata_names) != 1 or len(record_names) != 1 or len(wheel_names) != 1:
            raise ReleaseError("wheel METADATA/WHEEL/RECORD members are unresolved")
        metadata_text = archive.read(metadata_names[0]).decode("utf-8", errors="strict")
        metadata_message = Parser(policy=policy.default).parsestr(metadata_text)
        if _read_email_header(metadata_text, "Name") != spec["release"]["package_name"]:
            raise ReleaseError("wheel package name mismatch")
        if _read_email_header(metadata_text, "Version") != spec["release"]["package_version"]:
            raise ReleaseError("wheel package version mismatch")
        from packaging.requirements import Requirement
        from tools.release_spec import canonical_requirements

        raw_requires = metadata_message.get_all("Requires-Dist", [])
        runtime_requires = [
            value
            for value in raw_requires
            if Requirement(value).marker is None
            or "extra" not in str(Requirement(value).marker)
        ]
        observed_dependencies = canonical_requirements(runtime_requires)
        expected_dependencies = canonical_requirements(spec["release"]["runtime_dependencies"])
        if observed_dependencies != expected_dependencies:
            raise ReleaseError(
                "wheel runtime dependencies differ from the release specification: "
                f"expected {list(expected_dependencies)}, got {list(observed_dependencies)}"
            )
        wheel_text = archive.read(wheel_names[0]).decode("utf-8", errors="strict")
        if "Root-Is-Purelib: true" not in wheel_text or "Tag: py3-none-any" not in wheel_text:
            raise ReleaseError("wheel compatibility metadata is not py3-none-any pure Python")

        prefix = f"{spec['release']['package_import']}/"
        required = {prefix + path for path in required_resources}
        missing = sorted(required - set(names))
        if missing:
            raise ReleaseError(f"wheel missing active package resources: {missing}")
        packaged_spec = json_loads_object(archive.read(prefix + "release_spec.v1.json"), "wheel release spec")
        from tools.release_spec import release_spec_sha256
        if release_spec_sha256(packaged_spec) != release_spec_sha256(spec):
            raise ReleaseError("wheel carries a different release specification")

        record_rows = list(csv.reader(io.StringIO(archive.read(record_names[0]).decode("utf-8"))))
        declared: dict[str, tuple[str, str]] = {}
        for row in record_rows:
            if len(row) != 3 or row[0] in declared or not safe_relative_path(row[0]):
                raise ReleaseError("wheel RECORD is malformed, unsafe, or duplicated")
            declared[row[0]] = (row[1], row[2])
        if set(declared) != set(names):
            raise ReleaseError("wheel RECORD does not close over archive members")
        for name in names:
            hash_field, size_field = declared[name]
            if name == record_names[0]:
                if hash_field or size_field:
                    raise ReleaseError("wheel RECORD must not hash itself")
                continue
            data = archive.read(name)
            if hash_field != f"sha256={_wheel_record_digest(data)}":
                raise ReleaseError(f"wheel RECORD hash mismatch: {name}")
            if size_field != str(len(data)):
                raise ReleaseError(f"wheel RECORD size mismatch: {name}")
    return {
        "name": wheel.name,
        "size": wheel.stat().st_size,
        "sha256": sha256_path(wheel),
        "member_count": len(names),
        "record_verified": True,
        "required_resource_count": len(required),
        "runtime_dependencies": list(observed_dependencies),
    }


def json_loads_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = __import__("json").loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ReleaseError(f"invalid JSON in {label}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"expected JSON object in {label}")
    return value


def _validate_tar_members(
    members: Sequence[tarfile.TarInfo],
    *,
    label: str,
    expected_epoch: int | None = None,
) -> None:
    if len(members) > DEFAULT_MAX_ARCHIVE_MEMBERS:
        raise ReleaseError(f"{label} has too many members")
    names = [member.name for member in members]
    _validate_archive_names(names, label=label)
    total = 0
    for member in members:
        if not (member.isdir() or member.isfile()):
            raise ReleaseError(f"{label} contains a special or linked member: {member.name}")
        permissions = stat.S_IMODE(member.mode)
        allowed_permissions = {0o755} if member.isdir() else {0o644, 0o755}
        if permissions not in allowed_permissions:
            raise ReleaseError(
                f"{label} member has non-canonical permissions: {member.name} ({permissions:04o})"
            )
        if member.isfile():
            total += member.size
            if member.size > DEFAULT_MAX_MEMBER_BYTES:
                raise ReleaseError(f"{label} member exceeds size limit: {member.name}")
        if expected_epoch is not None:
            if int(member.mtime) != expected_epoch or member.uid != 0 or member.gid != 0:
                raise ReleaseError(f"{label} member metadata is not normalized: {member.name}")
            if member.uname or member.gname or member.pax_headers:
                raise ReleaseError(f"{label} member ownership metadata is not normalized: {member.name}")
    if total > DEFAULT_MAX_UNCOMPRESSED_BYTES:
        raise ReleaseError(f"{label} exceeds uncompressed size limit")


def _gzip_mtime(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            header = handle.read(10)
    except OSError as error:
        raise ReleaseError(f"cannot read sdist gzip header: {path.name}: {error}") from error
    if len(header) < 10 or header[:2] != b"\x1f\x8b":
        raise ReleaseError(f"sdist is not a gzip stream: {path.name}")
    return int.from_bytes(header[4:8], "little")


def inspect_sdist(
    sdist: Path,
    spec: Mapping[str, Any],
    *,
    required_resources: set[str],
) -> dict[str, Any]:
    if sdist.name != spec["artifacts"]["sdist"]:
        raise ReleaseError(f"unexpected sdist filename: {sdist.name}")
    epoch = int(spec["release"]["source_date_epoch"])
    if _gzip_mtime(sdist) != epoch:
        raise ReleaseError("sdist gzip timestamp is not normalized")
    try:
        archive = tarfile.open(sdist, "r:gz")
    except (OSError, tarfile.TarError) as error:
        raise ReleaseError(f"cannot open sdist {sdist}: {error}") from error
    with archive:
        members = archive.getmembers()
        _validate_tar_members(members, label=f"sdist {sdist.name}", expected_epoch=epoch)
        names = [member.name for member in members]
        roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
        if len(roots) != 1:
            raise ReleaseError("sdist does not have one root directory")
        root_name = next(iter(roots))
        expected_root = f"{spec['release']['package_name'].replace('-', '_')}-{spec['release']['package_version']}"
        if root_name != expected_root:
            raise ReleaseError(f"sdist root mismatch: expected {expected_root}, got {root_name}")
        required = {
            f"{root_name}/pyproject.toml",
            f"{root_name}/codex_wire_audit/release_spec.v1.json",
            f"{root_name}/codex_wire_audit/release_spec.schema.json",
            f"{root_name}/codex_wire_audit/release_contract.py",
            f"{root_name}/tools/release_pipeline.py",
        }
        required.update(f"{root_name}/codex_wire_audit/{path}" for path in required_resources)
        missing = sorted(required - set(names))
        if missing:
            raise ReleaseError(f"sdist missing release-closure sources: {missing}")
        spec_member = archive.extractfile(f"{root_name}/codex_wire_audit/release_spec.v1.json")
        if spec_member is None:
            raise ReleaseError("sdist release specification is unreadable")
        packaged_spec = json_loads_object(spec_member.read(), "sdist release spec")
        from tools.release_spec import release_spec_sha256
        if release_spec_sha256(packaged_spec) != release_spec_sha256(spec):
            raise ReleaseError("sdist carries a different release specification")
    return {
        "name": sdist.name,
        "size": sdist.stat().st_size,
        "sha256": sha256_path(sdist),
        "member_count": len(names),
        "root": root_name,
        "required_source_count": len(required),
        "normalized_metadata": True,
    }


def extract_sdist(sdist: Path, destination: Path) -> Path:
    if destination.exists():
        raise ReleaseError(f"sdist extraction destination already exists: {destination}")
    destination.mkdir(parents=True)
    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        _validate_tar_members(members, label=f"sdist {sdist.name}")
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseError(f"cannot read sdist member: {member.name}")
            target.write_bytes(source.read())
            target.chmod(stat.S_IMODE(member.mode))
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ReleaseError("extracted sdist root is unresolved")
    return roots[0]


def extract_zip_tree(path: Path, destination: Path, *, strip_prefix: str) -> Path:
    if destination.exists():
        raise ReleaseError(f"ZIP extraction destination already exists: {destination}")
    infos = validate_zip_archive(path)
    prefix = strip_prefix.rstrip("/") + "/"
    expected = {name for name in infos if name.startswith(prefix)}
    if expected != set(infos):
        raise ReleaseError(f"ZIP contains members outside expected prefix {prefix}")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(path) as archive:
        for name, info in infos.items():
            relative = name[len(prefix):]
            if not relative or not safe_relative_path(relative):
                raise ReleaseError(f"invalid stripped ZIP member: {name}")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            mode = (info.external_attr >> 16) & 0o7777
            target.chmod(mode or 0o644)
    return destination


def source_zip_entries(
    source_root: Path,
    records: Sequence[SourceRecord],
    *,
    prefix: str,
) -> list[tuple[str, bytes, int]]:
    return [
        (f"{prefix}/{record.path}", (source_root / record.path).read_bytes(), record.mode)
        for record in records
    ]


def wheel_resource_entries(
    wheel: Path,
    *,
    resource_prefix: str,
) -> list[tuple[str, bytes, int]]:
    infos = validate_zip_archive(wheel)
    archive_prefix = resource_prefix.rstrip("/") + "/"
    entries: list[tuple[str, bytes, int]] = []
    with zipfile.ZipFile(wheel) as archive:
        for name, info in infos.items():
            if not name.startswith(archive_prefix):
                continue
            relative = name[len(archive_prefix):]
            if not relative or not safe_relative_path(relative):
                raise ReleaseError(f"unsafe package resource path: {name}")
            mode = (info.external_attr >> 16) & 0o7777
            entries.append((relative, archive.read(name), mode or 0o644))
    if not entries:
        raise ReleaseError(f"wheel has no resources under {resource_prefix}")
    _validate_archive_names([name for name, _, _ in entries], label="wheel resource subtree")
    return sorted(entries, key=lambda item: item[0])
