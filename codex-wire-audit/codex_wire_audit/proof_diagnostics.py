from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

_SEVERITY = {"info": 0, "warning": 1, "error": 2, "critical": 3}


@dataclass(frozen=True, slots=True)
class ProofDiagnostic:
    code: str
    message: str
    severity: str = "error"
    category: str = "machine_proof"
    source_id: str | None = None
    field_id: str | None = None
    location: str | None = None
    strict_failure: bool = True
    recoverable: bool = False
    details: dict[str, Any] | None = None

    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.category,
            self.code,
            self.source_id or "",
            self.field_id or "",
            self.location or "",
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return {key: item for key, item in value.items() if item is not None}


def merge_diagnostics(existing: ProofDiagnostic, incoming: ProofDiagnostic) -> ProofDiagnostic:
    """Merge duplicate evidence without allowing a later observation to weaken it."""
    if existing.identity() != incoming.identity():
        raise ValueError("cannot merge diagnostics with different identities")
    severity = max((existing.severity, incoming.severity), key=lambda item: _SEVERITY.get(item, -1))
    messages = sorted({existing.message, incoming.message})
    details: dict[str, Any] = {}
    for candidate in (existing.details or {}, incoming.details or {}):
        for key, value in candidate.items():
            if key not in details:
                details[key] = value
            elif details[key] != value:
                current = details[key] if isinstance(details[key], list) else [details[key]]
                if value not in current:
                    current.append(value)
                details[key] = current
    return replace(
        existing,
        message=" | ".join(messages),
        severity=severity,
        strict_failure=existing.strict_failure or incoming.strict_failure,
        recoverable=existing.recoverable and incoming.recoverable,
        details=details or None,
    )


class DiagnosticBag:
    def __init__(self, diagnostics: Iterable[ProofDiagnostic] = ()) -> None:
        self._items: dict[tuple[str, str, str, str, str], ProofDiagnostic] = {}
        for diagnostic in diagnostics:
            self.add(diagnostic)

    def add(self, diagnostic: ProofDiagnostic) -> None:
        identity = diagnostic.identity()
        previous = self._items.get(identity)
        self._items[identity] = diagnostic if previous is None else merge_diagnostics(previous, diagnostic)

    def extend(self, diagnostics: Iterable[ProofDiagnostic]) -> None:
        for diagnostic in diagnostics:
            self.add(diagnostic)

    def values(self) -> list[ProofDiagnostic]:
        return [self._items[key] for key in sorted(self._items)]

    def to_list(self) -> list[dict[str, Any]]:
        return [diagnostic.to_dict() for diagnostic in self.values()]

    def has_strict_failure(self) -> bool:
        return any(item.strict_failure for item in self._items.values())
