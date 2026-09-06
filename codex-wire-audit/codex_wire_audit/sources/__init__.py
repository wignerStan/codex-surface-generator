"""Stable source-provider API.

Provider implementations live in separate modules so adding a new source mode
does not enlarge one central loader.
"""

from .archive import ArchiveLimits, load_archive_snapshot, safe_extract_archive
from .cache import load_cache_snapshot
from .common import SnapshotLoadResult, SourceLoadError, find_repo_root
from .git import load_repo_snapshot
from .github import load_github_snapshot

__all__ = [
    "ArchiveLimits",
    "SnapshotLoadResult",
    "SourceLoadError",
    "find_repo_root",
    "load_archive_snapshot",
    "load_cache_snapshot",
    "load_github_snapshot",
    "load_repo_snapshot",
    "safe_extract_archive",
]
