from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import urllib.request
import zipfile

REPOSITORY = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
BLOB_SHAS = [
    "203e1577574b8f38246baa79612206ef4d089907",
    "09d8d691254c33032dbf5a2080d5f2e3769d8330",
]
EXPECTED_FILES = 55
ROOT = Path.cwd()
TEMP = Path(os.environ["RUNNER_TEMP"])
PAYLOAD_ROOT = TEMP / "canonical-payload-v2"
EVIDENCE_ROOT = TEMP / "canonical-evidence-v2"


def run(*args: str) -> str:
    completed = subprocess.run(args, check=True, text=True, capture_output=True)
    return completed.stdout.strip()


def fetch_blob(sha: str) -> bytes:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/git/blobs/{sha}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "canonical-bootstrap-v2",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return base64.b64decode(payload["content"])


def variants(label: str, raw: bytes):
    seen: set[str] = set()

    def emit(kind: str, value: bytes):
        if not value:
            return None
        digest = hashlib.sha256(value).hexdigest()
        if digest in seen:
            return None
        seen.add(digest)
        return f"{label}:{kind}", value

    item = emit("raw", raw)
    if item:
        yield item
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return

    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 128 and len(compact) % 4 == 0:
        try:
            item = emit("whole-base64", base64.b64decode(compact, validate=True))
            if item:
                yield item
        except Exception:
            pass

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None

    def strings(value, path: str = "json"):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from strings(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from strings(child, f"{path}[{index}]")
        elif isinstance(value, str):
            yield path, value

    if parsed is not None:
        for path, value in strings(parsed):
            compact = re.sub(r"\s+", "", value)
            if len(compact) < 128 or len(compact) % 4:
                continue
            try:
                item = emit(path, base64.b64decode(compact, validate=True))
                if item:
                    yield item
            except Exception:
                pass

    for index, match in enumerate(re.finditer(r"[A-Za-z0-9+/=\r\n]{512,}", text)):
        compact = re.sub(r"\s+", "", match.group(0))
        if len(compact) % 4:
            continue
        try:
            item = emit(
                f"embedded-{index}", base64.b64decode(compact, validate=True)
            )
            if item:
                yield item
        except Exception:
            pass


def safe_path(name: str) -> PurePosixPath | None:
    value = name.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    if not value:
        return None
    if value.startswith("/"):
        raise RuntimeError(f"absolute archive path rejected: {name!r}")
    path = PurePosixPath(value)
    if not path.parts or ".." in path.parts or path.parts[0] == ".git":
        raise RuntimeError(f"unsafe archive path rejected: {name!r}")
    return path


def inspect_archive(data: bytes):
    stream = io.BytesIO(data)
    if zipfile.is_zipfile(stream):
        with zipfile.ZipFile(stream) as archive:
            files: list[tuple[str, int]] = []
            for info in archive.infolist():
                path = safe_path(info.filename)
                if path is None or info.is_dir():
                    continue
                kind = (info.external_attr >> 16) & 0o170000
                if kind == stat.S_IFLNK:
                    raise RuntimeError(f"symlink rejected: {info.filename}")
                files.append((str(path), (info.external_attr >> 16) & 0o777))
            return "zip", files
    try:
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:*") as archive:
            files = []
            for member in archive.getmembers():
                path = safe_path(member.name)
                if path is None or member.isdir():
                    continue
                if not member.isfile():
                    raise RuntimeError(f"non-regular member rejected: {member.name}")
                files.append((str(path), member.mode & 0o777))
            return "tar", files
    except tarfile.TarError:
        return None


def select_payload(raw_blobs: dict[str, bytes]):
    digest_hints: set[str] = set()
    for raw in raw_blobs.values():
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        digest_hints.update(
            value.lower()
            for value in re.findall(
                r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", text
            )
        )

    candidates = []
    for sha, raw in raw_blobs.items():
        for label, data in variants(sha, raw):
            inspected = inspect_archive(data)
            if inspected is None:
                continue
            kind, files = inspected
            if not files or len(files) > 250:
                continue
            digest = hashlib.sha256(data).hexdigest()
            toolchain = any(
                "toolchain" in PurePosixPath(name).parts for name, _mode in files
            )
            score = (
                (10000 if digest.lower() in digest_hints else 0)
                + (2000 if len(files) == EXPECTED_FILES else 0)
                + (500 if 40 <= len(files) <= 80 else 0)
                + (250 if toolchain else 0)
            )
            candidates.append((score, label, kind, data, files, digest))

    if not candidates:
        raise SystemExit("staged blobs contain no safe archive payload")
    candidates.sort(key=lambda row: (row[0], len(row[4]), len(row[3])), reverse=True)
    _score, label, kind, data, files, digest = candidates[0]
    if len(files) != EXPECTED_FILES:
        raise SystemExit(
            f"selected payload contains {len(files)} regular files; expected {EXPECTED_FILES}"
        )
    if not any(
        "toolchain" in PurePosixPath(name).parts for name, _mode in files
    ):
        raise SystemExit("selected payload contains no toolchain files")
    return label, kind, data, files, digest, digest.lower() in digest_hints


def extract_payload(kind: str, data: bytes, files: list[tuple[str, int]]) -> list[str]:
    paths = [PurePosixPath(name) for name, _mode in files]
    roots = {path.parts[0] for path in paths}
    prefix: str | None = None
    if len(roots) == 1 and all(len(path.parts) > 1 for path in paths):
        candidate = next(iter(roots))
        if candidate not in {".github", "toolchain", "release"}:
            prefix = candidate

    def destination(name: str) -> Path:
        path = PurePosixPath(name)
        if prefix and path.parts[0] == prefix:
            path = PurePosixPath(*path.parts[1:])
        if not path.parts or path.parts[0] == ".git":
            raise RuntimeError(f"unsafe extraction target: {name!r}")
        return PAYLOAD_ROOT.joinpath(*path.parts)

    PAYLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                path = safe_path(info.filename)
                if path is None or info.is_dir():
                    continue
                target = destination(str(path))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
                mode = (info.external_attr >> 16) & 0o777
                target.chmod(0o755 if mode & 0o111 else 0o644)
                written.append(str(target.relative_to(PAYLOAD_ROOT)))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                path = safe_path(member.name)
                if path is None:
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"unable to extract {member.name}")
                target = destination(str(path))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                written.append(str(target.relative_to(PAYLOAD_ROOT)))
    return sorted(written)


def copy_payload_without_workflows() -> None:
    for source in sorted(PAYLOAD_ROOT.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(PAYLOAD_ROOT)
        if relative.parts[:2] == (".github", "workflows"):
            continue
        target = ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(source.stat().st_mode & 0o777)


def apply_deletions() -> None:
    for manifest in [ROOT / ".canonical-payload.json", ROOT / "canonical-payload.json"]:
        if not manifest.is_file():
            continue
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        deletions = payload.get("delete", payload.get("deletions", []))
        if not isinstance(deletions, list):
            raise SystemExit(f"invalid deletion list in {manifest}")
        for raw in deletions:
            path = PurePosixPath(str(raw))
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] == ".git"
            ):
                raise SystemExit(f"unsafe deletion path: {raw!r}")
            if path.parts[:2] == (".github", "workflows"):
                continue
            target = ROOT.joinpath(*path.parts)
            if target.is_dir():
                raise SystemExit(f"payload deletion may not remove a directory: {raw!r}")
            target.unlink(missing_ok=True)
        manifest.unlink()


def remove_dotfile_residue() -> None:
    for correct_root in PAYLOAD_ROOT.iterdir():
        if not correct_root.name.startswith(".") or correct_root.name in {".", ".."}:
            continue
        residue_root = ROOT / correct_root.name[1:]
        if correct_root.is_file() and residue_root.is_file():
            if correct_root.read_bytes() == residue_root.read_bytes():
                residue_root.unlink()
        elif correct_root.is_dir() and residue_root.is_dir():
            for correct in sorted(correct_root.rglob("*"), reverse=True):
                if not correct.is_file():
                    continue
                relative = correct.relative_to(correct_root)
                if relative.parts and correct_root.name == ".github" and relative.parts[0] == "workflows":
                    continue
                residue = residue_root / relative
                if residue.is_file() and residue.read_bytes() == correct.read_bytes():
                    residue.unlink()
            for directory in sorted(
                (path for path in residue_root.rglob("*") if path.is_dir()), reverse=True
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                residue_root.rmdir()
            except OSError:
                pass


def write_output(key: str, value: str) -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def main() -> None:
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    raw_blobs = {sha: fetch_blob(sha) for sha in BLOB_SHAS}
    label, kind, data, files, digest, digest_matched = select_payload(raw_blobs)
    written = extract_payload(kind, data, files)
    (EVIDENCE_ROOT / "payload-recovery.json").write_text(
        json.dumps(
            {
                "source": label,
                "archive_kind": kind,
                "sha256": digest,
                "digest_hint_matched": digest_matched,
                "regular_files": len(written),
                "paths": written,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    base_sha = run("git", "rev-parse", "HEAD")
    copy_payload_without_workflows()
    apply_deletions()
    remove_dotfile_residue()
    (ROOT / ".github" / "canonical_bootstrap_v2.py").unlink(missing_ok=True)

    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "diff", "--cached", "--check"], check=True)
    quiet = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0
    if quiet:
        head_sha = base_sha
        changed = "false"
    else:
        run("git", "config", "user.name", "github-actions[bot]")
        run(
            "git",
            "config",
            "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        )
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                "feat: add canonical connected-system contract",
                "-m",
                (
                    "Unifies configuration, environment, resolution, lifecycle, and "
                    "local-storage evidence under codex-system-contract/v1 while "
                    "preserving compatibility views and enforcing reference, coverage, "
                    "digest, and secret-safety invariants."
                ),
            ],
            check=True,
        )
        head_sha = run("git", "rev-parse", "HEAD")
        changed = "true"

    write_output("changed", changed)
    write_output("base_sha", base_sha)
    write_output("head_sha", head_sha)
    print(json.dumps({"changed": changed, "base_sha": base_sha, "head_sha": head_sha}))


if __name__ == "__main__":
    main()
