"""Canonical extractor registry and built-in extractor activation."""

from .registry import Extractor, ExtractorResult, create_extractors, register_extractor

# Register built-ins only after the independent registry module is initialized.
from . import turn_metadata as _turn_metadata  # noqa: E402,F401
from . import context_management as _context_management  # noqa: E402,F401
from . import config_effects as _config_effects  # noqa: E402,F401

__all__ = [
    "Extractor",
    "ExtractorResult",
    "create_extractors",
    "register_extractor",
]
