#!/usr/bin/env python3
"""Build an offline wheel and source distribution with stable timestamps."""

from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import time
import zipfile

import setuptools.build_meta

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DATE_EPOCH = "315532800"  # 1980-01-01, valid for ZIP timestamps.


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    # ZIP timestamps cannot represent dates before 1980. SOURCE_DATE_EPOCH is
    # still authoritative; only the container representation is clamped.
    return time.gmtime(max(epoch, 315532800))[:6]


def normalize_wheel(path: Path, *, epoch: int) -> None:
    """Rewrite a wheel with deterministic entry ordering and canonical modes.

    Some wheel writers create the generated ``RECORD`` member using the host's
    group-writable default mode even when every source file is canonical. File
    modes and ZIP metadata are outside RECORD's content hashes, so normalize the
    completed archive and then let the release verifier re-check every RECORD
    row, hash, size, path and mode.
    """
    with zipfile.ZipFile(path, mode="r") as source:
        infos = source.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError(f"wheel contains duplicate members: {path}")
        entries = [(info, b"" if info.is_dir() else source.read(info)) for info in infos]

    def order_key(entry: tuple[zipfile.ZipInfo, bytes]) -> tuple[int, str]:
        name = entry[0].filename
        # Keeping RECORD last matches conventional wheel layout while remaining
        # independent of the backend's temporary-directory traversal order.
        return (1 if name.endswith(".dist-info/RECORD") else 0, name)

    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=False,
        ) as target:
            target.comment = b""
            for original, data in sorted(entries, key=order_key):
                normalized = zipfile.ZipInfo(original.filename, _zip_datetime(epoch))
                normalized.create_system = 3
                normalized.internal_attr = 0
                normalized.extra = b""
                normalized.comment = b""
                if original.is_dir():
                    normalized.compress_type = zipfile.ZIP_STORED
                    normalized.external_attr = ((0o040000 | 0o755) << 16) | 0x10
                    target.writestr(normalized, b"")
                else:
                    original_mode = (original.external_attr >> 16) & 0o7777
                    mode = 0o755 if original_mode & 0o111 else 0o644
                    normalized.compress_type = zipfile.ZIP_DEFLATED
                    normalized.external_attr = (0o100000 | mode) << 16
                    target.writestr(
                        normalized,
                        data,
                        compress_type=zipfile.ZIP_DEFLATED,
                        compresslevel=9,
                    )
        temporary_path.replace(path)
        path.chmod(0o644)
    finally:
        temporary_path.unlink(missing_ok=True)


def normalize_sdist(path: Path, *, epoch: int) -> None:
    """Rewrite an sdist with deterministic metadata and gzip framing."""
    with tarfile.open(path, mode="r:gz") as source:
        members = sorted(source.getmembers(), key=lambda item: item.name)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with temporary_path.open("wb") as raw_output:
                with gzip.GzipFile(
                    filename="",
                    mode="wb",
                    fileobj=raw_output,
                    compresslevel=9,
                    mtime=epoch,
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed,
                        mode="w",
                        format=tarfile.GNU_FORMAT,
                    ) as target:
                        for member in members:
                            normalized = copy.copy(member)
                            normalized.mtime = epoch
                            normalized.uid = 0
                            normalized.gid = 0
                            normalized.uname = ""
                            normalized.gname = ""
                            normalized.pax_headers = {}
                            data = source.extractfile(member) if member.isfile() else None
                            target.addfile(normalized, data)
            temporary_path.replace(path)
            path.chmod(0o644)
        finally:
            temporary_path.unlink(missing_ok=True)


def build(output_dir: Path, *, clean: bool) -> list[Path]:
    if clean and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("SOURCE_DATE_EPOCH", DEFAULT_SOURCE_DATE_EPOCH)
    previous = Path.cwd()
    os.chdir(ROOT)
    try:
        sdist_name = setuptools.build_meta.build_sdist(str(output_dir))
        normalize_sdist(
            output_dir / sdist_name,
            epoch=int(os.environ["SOURCE_DATE_EPOCH"]),
        )
        wheel_name = setuptools.build_meta.build_wheel(str(output_dir))
        normalize_wheel(
            output_dir / wheel_name,
            epoch=int(os.environ["SOURCE_DATE_EPOCH"]),
        )
    finally:
        os.chdir(previous)
    return [output_dir / sdist_name, output_dir / wheel_name]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=str(ROOT / "dist"))
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--manifest")
    args = parser.parse_args()
    artifacts = build(Path(args.output_dir).resolve(), clean=not args.no_clean)
    manifest = {
        "distribution_manifest_version": "1.0.0",
        "source_date_epoch": os.environ.get("SOURCE_DATE_EPOCH", DEFAULT_SOURCE_DATE_EPOCH),
        "sdist_metadata_normalized": True,
        "artifacts": [
            {
                "name": path.name,
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in sorted(artifacts)
        ],
    }
    encoded = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.manifest:
        Path(args.manifest).write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
