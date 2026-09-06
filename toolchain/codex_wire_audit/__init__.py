"""Source-derived Codex surface generation and machine-proof package.

Generator 12 keeps the frozen compatibility renderer while adding a canonical
generated-config catalog and a graph that connects config paths and Rust feature
registry entries to existing wire, tool, route, metadata, and context-management
surfaces.
"""

from __future__ import annotations

GENERATOR_VERSION = "12.0.0"
REPORT_FORMAT_VERSION = "10.0.0"
EVOLUTION_CONTRACT_VERSION = "1.0.0"
SOURCE_REGISTRY_VERSION = "1.0.0"
SEMANTIC_DIFF_VERSION = "1.0.0"
PROOF_ATTESTATION_VERSION = "2.0.0"
METADATA_HISTORY_VERSION = "1.0.0"

__all__ = [
    "GENERATOR_VERSION",
    "REPORT_FORMAT_VERSION",
    "EVOLUTION_CONTRACT_VERSION",
    "SOURCE_REGISTRY_VERSION",
    "SEMANTIC_DIFF_VERSION",
    "PROOF_ATTESTATION_VERSION",
    "METADATA_HISTORY_VERSION",
]
