from __future__ import annotations

import base64

import pytest

from codex_wire_audit.github_content import decode_github_content


def test_line_wrapped_github_content_decodes_strictly() -> None:
    raw = b"canonical source bytes" * 20
    encoded = base64.b64encode(raw).decode("ascii")
    wrapped = "
".join(encoded[index:index + 60] for index in range(0, len(encoded), 60))
    assert decode_github_content(wrapped) == raw


def test_invalid_github_content_is_rejected() -> None:
    with pytest.raises(ValueError):
        decode_github_content("not base64 !!!")
