"""Maintainability and CI-proof focused Codex wire-audit package.

Generator 10 keeps the frozen compatibility renderer and independent proof gate,
while adding first-class experimental context-management semantics: activation,
model capability, History/Notes transport, routing separation, and no-summary
context rollover.
"""

from __future__ import annotations

GENERATOR_VERSION = "11.0.0"
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
