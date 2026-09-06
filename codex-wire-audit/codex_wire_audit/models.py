"""Canonical data models for maintainability-oriented extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


class SourceGroup(str, Enum):
    BASE = "base"
    SURFACE = "surface"
    EXTRA = "extra"


@dataclass(frozen=True, slots=True)
class SourceSpec:
    id: str
    legacy_key: str
    group: SourceGroup
    path_candidates: tuple[str, ...]
    required: bool
    roles: tuple[str, ...] = ()
    expected_symbols: tuple[str, ...] = ()
    extractor_ids: tuple[str, ...] = ()
    exclusion_reason: str | None = None

    @property
    def primary_path(self) -> str:
        return self.path_candidates[0]

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "legacy_key": self.legacy_key,
            "group": self.group.value,
            "path_candidates": list(self.path_candidates),
            "required": self.required,
            "roles": list(self.roles),
            "expected_symbols": list(self.expected_symbols),
            "extractor_ids": list(self.extractor_ids),
        }
        if self.exclusion_reason is not None:
            value["exclusion_reason"] = self.exclusion_reason
        return value


@dataclass(frozen=True, slots=True)
class SourceFile:
    spec_id: str
    legacy_key: str
    group: SourceGroup
    selected_path: str
    raw_bytes: bytes
    text: str
    content_sha256: str
    git_blob_sha: str
    repository_blob_sha: str | None = None
    path_candidate_index: int = 0

    @classmethod
    def create(
        cls,
        *,
        spec: SourceSpec,
        selected_path: str,
        raw_bytes: bytes,
        repository_blob_sha: str | None = None,
        path_candidate_index: int = 0,
    ) -> "SourceFile":
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"source is not UTF-8: {selected_path}: {error}") from error
        git_blob = hashlib.sha1(
            b"blob " + str(len(raw_bytes)).encode("ascii") + b"\0" + raw_bytes
        ).hexdigest()
        return cls(
            spec_id=spec.id,
            legacy_key=spec.legacy_key,
            group=spec.group,
            selected_path=selected_path,
            raw_bytes=raw_bytes,
            text=text,
            content_sha256=hashlib.sha256(raw_bytes).hexdigest(),
            git_blob_sha=git_blob,
            repository_blob_sha=repository_blob_sha,
            path_candidate_index=path_candidate_index,
        )

    def to_manifest_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "legacy_key": self.legacy_key,
            "group": self.group.value,
            "path": self.selected_path,
            "byte_length": len(self.raw_bytes),
            "line_count": self.text.count("\n") + (0 if not self.text or self.text.endswith("\n") else 1),
            "sha256": self.content_sha256,
            "git_blob_sha": self.git_blob_sha,
            "repository_blob_sha": self.repository_blob_sha,
            "path_candidate_index": self.path_candidate_index,
            "byte_identity": "exact",
            "encoding": "utf-8",
        }


@dataclass(frozen=True, slots=True)
class SourceRevision:
    source_mode: str
    repository: str
    requested_ref: str
    resolved_commit_sha: str | None
    source_set_sha256: str
    dirty: bool | None
    commit_date: str | None = None
    commit_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_mode": self.source_mode,
            "repository": self.repository,
            "requested_ref": self.requested_ref,
            "resolved_commit_sha": self.resolved_commit_sha,
            "source_set_sha256": self.source_set_sha256,
            "source_revision_id": f"sha256:{self.source_set_sha256}",
            "dirty": self.dirty,
            "commit_date": self.commit_date,
            "commit_message": self.commit_message,
        }


@dataclass(slots=True)
class SourceSnapshot:
    revision: SourceRevision
    files: dict[str, SourceFile]
    unavailable_specs: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def digest_files(files: Mapping[str, SourceFile]) -> str:
        digest = hashlib.sha256()
        for source_file in sorted(files.values(), key=lambda item: item.selected_path):
            digest.update(source_file.selected_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(source_file.raw_bytes)
            digest.update(b"\0")
        return digest.hexdigest()

    def text_by_spec(self, spec_id: str) -> str | None:
        value = self.files.get(spec_id)
        return value.text if value else None

    def text_by_legacy_key(self, group: SourceGroup, key: str) -> str | None:
        for source_file in self.files.values():
            if source_file.group == group and source_file.legacy_key == key:
                return source_file.text
        return None

    def to_legacy_maps(self) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        groups: dict[SourceGroup, dict[str, str]] = {
            SourceGroup.BASE: {},
            SourceGroup.SURFACE: {},
            SourceGroup.EXTRA: {},
        }
        for source_file in self.files.values():
            groups[source_file.group][source_file.legacy_key] = source_file.text
        return groups[SourceGroup.BASE], groups[SourceGroup.SURFACE], groups[SourceGroup.EXTRA]

    def manifest(self) -> dict[str, Any]:
        return {
            "revision": self.revision.to_dict(),
            "files": {
                source_id: source_file.to_manifest_dict()
                for source_id, source_file in sorted(self.files.items())
            },
            "unavailable_specs": dict(sorted(self.unavailable_specs.items())),
            "counts": {
                "available": len(self.files),
                "unavailable": len(self.unavailable_specs),
            },
        }


@dataclass(frozen=True, slots=True)
class GateFact:
    id: str
    name: str
    source_expression: str
    predicate: Mapping[str, Any]
    semantic_fingerprint: str
    source_ref: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source_expression": self.source_expression,
            "predicate": dict(self.predicate),
            "semantic_fingerprint": self.semantic_fingerprint,
            "source_ref": self.source_ref,
        }


@dataclass(frozen=True, slots=True)
class FieldEmissionFact:
    id: str
    field_name: str
    kind: str
    source_expression: str
    value_expression: str | None
    predicate: Mapping[str, Any]
    gate_refs: tuple[str, ...]
    identity_domain: str
    emission_fingerprint: str
    semantic_fingerprint: str
    source_ref: str
    machine_evaluable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "field_name": self.field_name,
            "kind": self.kind,
            "source_expression": self.source_expression,
            "value_expression": self.value_expression,
            "predicate": dict(self.predicate),
            "gate_refs": list(self.gate_refs),
            "identity_domain": self.identity_domain,
            "emission_fingerprint": self.emission_fingerprint,
            "semantic_fingerprint": self.semantic_fingerprint,
            "source_ref": self.source_ref,
            "machine_evaluable": self.machine_evaluable,
        }


def semantic_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
