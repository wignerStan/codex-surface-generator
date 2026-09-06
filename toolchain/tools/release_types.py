"""Small immutable records and stable errors used by release tooling."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class ReleaseError(RuntimeError):
    """Stable operational failure raised by the release pipeline."""

    def __init__(self, message: str, *, phase: str | None = None) -> None:
        super().__init__(message)
        self.phase = phase


@dataclass(frozen=True, slots=True)
class SourceRecord:
    path: str
    size: int
    sha256: str
    mode: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "mode": f"{self.mode:04o}",
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceRecord":
        try:
            mode = int(str(value["mode"]), 8)
            return cls(
                path=str(value["path"]),
                size=int(value["size"]),
                sha256=str(value["sha256"]),
                mode=mode,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ReleaseError(f"invalid source record: {value!r}") from error


@dataclass(frozen=True, slots=True)
class DistributionRecord:
    name: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "sha256": self.sha256}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DistributionRecord":
        try:
            return cls(
                name=str(value["name"]),
                size=int(value["size"]),
                sha256=str(value["sha256"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ReleaseError(f"invalid distribution record: {value!r}") from error
