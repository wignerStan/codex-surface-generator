"""Safe destination checks, process locking, failure evidence, and publication."""
from __future__ import annotations

from contextlib import AbstractContextManager
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import tempfile
import time
from typing import Any, Mapping

from tools.release_integrity import (
    add_integrity,
    atomic_write,
    directory_digest,
    pretty_json,
)
from tools.release_runtime import redact_text
from tools.release_spec import release_spec_sha256, source_path_is_excluded
from tools.release_types import ReleaseError


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_symlink_components(path: Path) -> None:
    current = path
    chain: list[Path] = []
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    for item in reversed(chain):
        if item.exists() and item.is_symlink():
            raise ReleaseError(f"release path traverses a symlink: {item}")


def validate_output_destination(root: Path, output: Path, spec: Mapping[str, Any]) -> Path:
    root = root.resolve(strict=True)
    output = output.expanduser().absolute()
    _reject_symlink_components(output)
    resolved = output.resolve(strict=False)
    if resolved == root or _is_relative_to(root, resolved):
        raise ReleaseError("release output may not be the source root or one of its ancestors")
    if _is_relative_to(resolved, root):
        relative = resolved.relative_to(root)
        if len(relative.parts) < 1 or not source_path_is_excluded(relative / "__probe__", spec):
            raise ReleaseError(
                "release output inside the source tree must be excluded from the source snapshot"
            )
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise ReleaseError("release output exists but is not a regular directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    return resolved


class OutputLock(AbstractContextManager["OutputLock"]):
    """Cross-platform advisory lock that is released automatically on process death.

    The sibling lock file is intentionally persistent.  Its file contents are only
    diagnostic; ownership is the operating-system lock held on the open descriptor.
    This avoids permanent lockout after SIGKILL, host restart, or runner timeout.
    """

    def __init__(self, destination: Path) -> None:
        self.path = destination.parent / f".{destination.name}.release.lock"
        self._descriptor: int | None = None

    @staticmethod
    def _lock(descriptor: int) -> None:
        if os.name == "nt":  # pragma: no cover - exercised in Windows CI
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise BlockingIOError from error
            return

        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(descriptor: int) -> None:
        if os.name == "nt":  # pragma: no cover - exercised in Windows CI
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)

    def __enter__(self) -> "OutputLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as error:
            raise ReleaseError(f"cannot inspect release lock path: {self.path}: {error}") from error
        if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
            raise ReleaseError(f"release lock path is not a regular file: {self.path}")

        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise ReleaseError(f"cannot open release lock path: {self.path}: {error}") from error
        descriptor_info = os.fstat(descriptor)
        if not stat.S_ISREG(descriptor_info.st_mode) or descriptor_info.st_nlink != 1:
            os.close(descriptor)
            raise ReleaseError(f"release lock path is not a private regular file: {self.path}")
        if hasattr(os, "getuid") and descriptor_info.st_uid != os.getuid():
            os.close(descriptor)
            raise ReleaseError(f"release lock file is owned by another user: {self.path}")
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):  # pragma: no cover - platform dependent
            pass
        try:
            self._lock(descriptor)
        except (BlockingIOError, OSError) as error:
            os.close(descriptor)
            owner = "unreadable"
            try:
                owner = self.path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise ReleaseError(f"release output is locked: {self.path} ({owner})") from error

        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "created_unix": int(time.time()),
        }
        encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            self._unlock(descriptor)
        finally:
            os.close(descriptor)


def publish_directory(source: Path, destination: Path, *, replace: bool) -> None:
    source_manifest = directory_digest(source)
    if destination.exists() and not replace:
        raise ReleaseError(
            f"release destination already exists: {destination}; pass --replace to replace it"
        )
    staging_parent = destination.parent
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.publish.", dir=staging_parent))
    backup: Path | None = None
    published = False
    try:
        staging.rmdir()
        shutil.copytree(source, staging, symlinks=False, copy_function=shutil.copy2)
        if directory_digest(staging) != source_manifest:
            raise ReleaseError("staged publication differs from assembled release")
        if destination.exists():
            backup = Path(tempfile.mkdtemp(prefix=f".{destination.name}.backup.", dir=staging_parent))
            backup.rmdir()
            os.replace(destination, backup)
        os.replace(staging, destination)
        published = True
        if directory_digest(destination) != source_manifest:
            raise ReleaseError("published release differs from assembled release")
        if backup is not None:
            shutil.rmtree(backup)
            backup = None
    except Exception:
        if published and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        if backup is not None and backup.exists():
            os.replace(backup, destination)
            backup = None
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def failure_paths(output: Path, *, operation: str) -> tuple[Path, Path]:
    suffix = "release" if operation == "release" else "verify"
    return (
        output.parent / f"{output.name}.{suffix}.failure.json",
        output.parent / f"{output.name}.{suffix}.failure.log",
    )


def clear_failure_evidence(output: Path, *, operation: str) -> None:
    for path in failure_paths(output, operation=operation):
        path.unlink(missing_ok=True)


def write_failure_evidence(
    output: Path,
    *,
    operation: str,
    error: Exception,
    spec: Mapping[str, Any] | None,
    phase: str,
    log: list[str],
) -> tuple[Path, Path] | None:
    record_path, log_path = failure_paths(output, operation=operation)
    record: dict[str, Any] = {
        "format": "codex-wire-audit-release-failure/v2",
        "status": "failed",
        "operation": operation,
        "phase": phase,
        "error_type": type(error).__name__,
        "message": redact_text(str(error))[:16_384],
        "log_file": log_path.name,
    }
    if spec is not None:
        record["release_id"] = spec.get("release", {}).get("id")
        record["release_spec_sha256"] = release_spec_sha256(spec)
    record = add_integrity(record)
    try:
        atomic_write(log_path, (redact_text("\n".join(log)).rstrip() + "\n").encode("utf-8"))
        atomic_write(record_path, pretty_json(record))
    except Exception:
        return None
    return record_path, log_path
