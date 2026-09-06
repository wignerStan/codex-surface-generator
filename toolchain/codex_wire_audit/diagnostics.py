"""Structured diagnostics emitted at the point of discovery.

Unlike the legacy report adapter, this module never infers diagnostic codes or
severity by reparsing prose.  Human-readable messages are presentation data;
``code`` and ``details`` are the machine contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

_SEVERITY_ORDER = {"info": 1, "warning": 2, "error": 3}


def _slug(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return value or "unknown"


def _stable_suffix(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    category: str
    extractor_id: str | None = None
    entity_id: str | None = None
    report_pointer: str | None = None
    source_refs: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)
    recoverable: bool = True
    strict_failure: bool = False

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_ORDER:
            raise ValueError(f"unsupported diagnostic severity: {self.severity}")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", self.code):
            raise ValueError(f"diagnostic code must be upper snake case: {self.code}")

    @property
    def id(self) -> str:
        identity = {
            "code": self.code,
            "extractor_id": self.extractor_id,
            "entity_id": self.entity_id,
            "report_pointer": self.report_pointer,
            "source_refs": list(self.source_refs),
            "details": dict(self.details),
        }
        return f"diag.{_slug(self.code)}.{_stable_suffix(identity)}"

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "code": self.code,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "recoverable": self.recoverable,
            "strict_failure": self.strict_failure,
            "details": dict(self.details),
        }
        if self.extractor_id:
            value["extractor_id"] = self.extractor_id
        if self.entity_id:
            value["entity_id"] = self.entity_id
        if self.report_pointer:
            value["report_pointer"] = self.report_pointer
        if self.source_refs:
            value["source_refs"] = list(self.source_refs)
        return value


class DiagnosticCollector:
    """Deduplicating collector with deterministic output order."""

    def __init__(self) -> None:
        self._items: dict[str, Diagnostic] = {}

    def emit(self, diagnostic: Diagnostic | None = None, **kwargs: Any) -> Diagnostic:
        item = diagnostic or Diagnostic(**kwargs)
        self._items[item.id] = item
        return item

    def extend(self, diagnostics: Iterable[Diagnostic]) -> None:
        for diagnostic in diagnostics:
            self.emit(diagnostic)

    def values(self) -> tuple[Diagnostic, ...]:
        return tuple(
            sorted(
                self._items.values(),
                key=lambda item: (
                    -_SEVERITY_ORDER[item.severity],
                    item.code,
                    item.entity_id or "",
                    item.id,
                ),
            )
        )

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.values()]

    def summary(self) -> dict[str, int]:
        result = {"error": 0, "warning": 0, "info": 0}
        for item in self._items.values():
            result[item.severity] += 1
        return result

    def highest_severity(self) -> str:
        if not self._items:
            return "none"
        return max(self._items.values(), key=lambda item: _SEVERITY_ORDER[item.severity]).severity

    def __bool__(self) -> bool:
        return bool(self._items)
