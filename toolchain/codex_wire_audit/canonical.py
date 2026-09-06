"""Canonical JSON helpers with collision-safe Unicode normalization."""

from __future__ import annotations

import copy
import json
import unicodedata
from typing import Any, Mapping


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented without information loss."""


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def normalize_json(value: Any, *, pointer: str = "") -> Any:
    if isinstance(value, str):
        return nfc(value)
    if isinstance(value, list):
        return [normalize_json(item, pointer=f"{pointer}/{index}") for index, item in enumerate(value)]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        originals: dict[str, str] = {}
        for key, child in value.items():
            original = str(key)
            normalized_key = nfc(original)
            if normalized_key in normalized and originals[normalized_key] != original:
                location = pointer or "/"
                raise CanonicalizationError(
                    "UNICODE_NORMALIZED_KEY_COLLISION at "
                    f"{location}: {originals[normalized_key]!r} and {original!r} normalize to "
                    f"{normalized_key!r}"
                )
            originals[normalized_key] = original
            normalized[normalized_key] = normalize_json(
                child,
                pointer=f"{pointer}/{normalized_key.replace('~', '~0').replace('/', '~1')}",
            )
        return normalized
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        raise CanonicalizationError("non-finite numbers are not canonical JSON")
    return value


def canonical_json_bytes(value: Any) -> bytes:
    normalized = normalize_json(value)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise CanonicalizationError(f"value cannot be canonically serialized: {error}") from error
    return (encoded + "\n").encode("utf-8")


def canonical_report_payload(report: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(report))
    payload.pop("integrity", None)
    payload.pop("generated_at", None)
    status = payload.get("status")
    if isinstance(status, dict):
        status.pop("validated_at", None)
    return normalize_json(payload)
