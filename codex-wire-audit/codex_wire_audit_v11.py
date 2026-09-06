#!/usr/bin/env python3
"""Compatibility entrypoint for the packaged v11 maintainability layer."""

from codex_wire_audit.cli import main
from codex_wire_audit.canonical import CanonicalizationError
from codex_wire_audit.sources import SourceLoadError


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SourceLoadError, CanonicalizationError, ValueError) as error:
        import sys

        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)
