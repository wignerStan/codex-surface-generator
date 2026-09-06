"""Compatibility facade for release helpers.

New code should import the ownership-specific modules directly.  This module is
kept so older tests and downstream scripts do not break while the release
pipeline remains internally acyclic.
"""
from __future__ import annotations

from typing import Any, Mapping
from pathlib import Path

from tools.release_archives import (
    deterministic_zip_bytes,
    extract_sdist,
    source_zip_entries,
    validate_zip_archive,
    wheel_resource_entries,
    zip_timestamp,
)
from tools.release_integrity import (
    add_integrity,
    atomic_write,
    canonical_json,
    copy_exact,
    directory_digest,
    file_record,
    load_json_object,
    pretty_json,
    sha256_bytes,
    sha256_path,
    verify_integrity,
)
from tools.release_runtime import (
    build_distributions,
    compare_distribution_sets,
    installed_wheel_probe,
    parse_pytest_pass_count,
    run,
    source_package_probe,
    validate_sdist_roundtrip,
)
from tools.release_spec import (
    copy_source_snapshot,
    load_release_spec,
    release_spec_sha256,
    required_package_resources,
    source_archive_prefix,
    source_snapshot,
    validate_source_alignment,
    verify_source_snapshot,
)
from tools.release_types import DistributionRecord, ReleaseError, SourceRecord


def inspect_wheel(path: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    from tools.release_archives import inspect_wheel as implementation

    return implementation(path, spec, required_resources=required_package_resources(spec))


def inspect_sdist(path: Path, spec: Mapping[str, Any]) -> dict[str, Any]:
    from tools.release_archives import inspect_sdist as implementation

    return implementation(path, spec, required_resources=required_package_resources(spec))


__all__ = [
    "DistributionRecord", "ReleaseError", "SourceRecord", "add_integrity", "atomic_write",
    "build_distributions", "canonical_json", "compare_distribution_sets", "copy_exact",
    "copy_source_snapshot", "deterministic_zip_bytes", "directory_digest", "extract_sdist",
    "file_record", "inspect_sdist", "inspect_wheel", "installed_wheel_probe",
    "load_json_object", "load_release_spec", "parse_pytest_pass_count", "pretty_json",
    "release_spec_sha256", "required_package_resources", "run", "sha256_bytes", "sha256_path",
    "source_archive_prefix", "source_package_probe", "source_snapshot", "source_zip_entries",
    "validate_sdist_roundtrip", "validate_source_alignment", "validate_zip_archive",
    "verify_integrity", "verify_source_snapshot", "wheel_resource_entries", "zip_timestamp",
]
