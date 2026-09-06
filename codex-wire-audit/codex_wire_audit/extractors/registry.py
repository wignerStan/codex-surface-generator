"""Typed registry for canonical protocol extractors.

The registry is separate from package initialization so extractor modules can
import its types without creating an import cycle. Built-ins are registered by
``codex_wire_audit.extractors`` after these definitions are available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from ..diagnostics import DiagnosticCollector
from ..models import SourceSnapshot


@dataclass(frozen=True, slots=True)
class ExtractorResult:
    extractor_id: str
    schema_version: str
    data: dict[str, Any]
    semantic_complete: bool
    source_spec_ids: tuple[str, ...]


class Extractor(Protocol):
    extractor_id: str
    source_spec_ids: tuple[str, ...]

    def extract(
        self,
        snapshot: SourceSnapshot,
        diagnostics: DiagnosticCollector,
    ) -> ExtractorResult: ...


_FACTORIES: dict[str, Callable[[], Extractor]] = {}


def register_extractor(
    extractor_id: str,
) -> Callable[[Callable[[], Extractor]], Callable[[], Extractor]]:
    def decorator(factory: Callable[[], Extractor]) -> Callable[[], Extractor]:
        if extractor_id in _FACTORIES:
            raise RuntimeError(f"duplicate extractor id: {extractor_id}")
        _FACTORIES[extractor_id] = factory
        return factory

    return decorator


def create_extractors() -> tuple[Extractor, ...]:
    return tuple(_FACTORIES[key]() for key in sorted(_FACTORIES))
