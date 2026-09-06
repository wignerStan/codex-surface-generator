"""Deprecated import shim for the v11 source-provider package.

Use :mod:`codex_wire_audit.sources`.  The shim keeps early v11 integrations
working while provider implementations remain independently maintainable.
"""

from .sources import *  # noqa: F401,F403
