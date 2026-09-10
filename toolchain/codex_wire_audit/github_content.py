"""Strict decoding for line-wrapped GitHub Contents API payloads."""

from __future__ import annotations

import base64


def decode_github_content(encoded: str) -> bytes:
    """Decode standard GitHub base64 after removing transport whitespace."""
    if not isinstance(encoded, str):
        raise TypeError("GitHub content must be a string")
    return base64.b64decode("".join(encoded.split()), validate=True)
