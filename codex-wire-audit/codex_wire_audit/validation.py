"""Validation for v11 maintainability extensions."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Mapping

from . import EVOLUTION_CONTRACT_VERSION
from .canonical import CanonicalizationError, canonical_json_bytes


def validate_evolution_contract(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    contract = value.get("evolution_contract") if "evolution_contract" in value else value
    diagnostics: list[dict[str, Any]] = []

    def error(code: str, message: str, pointer: str) -> None:
        diagnostics.append(
            {
                "id": f"diag.validation.{code.lower()}.{len(diagnostics)}",
                "code": code,
                "severity": "error",
                "category": "evolution_validation",
                "message": message,
                "report_pointer": pointer,
            }
        )

    if not isinstance(contract, Mapping):
        error("EVOLUTION_CONTRACT_MISSING", "evolution_contract is missing or not an object", "/evolution_contract")
        return diagnostics
    if contract.get("schema_version") != EVOLUTION_CONTRACT_VERSION:
        error(
            "EVOLUTION_SCHEMA_VERSION_UNSUPPORTED",
            f"expected evolution schema {EVOLUTION_CONTRACT_VERSION}",
            "/evolution_contract/schema_version",
        )
    source_revision = contract.get("source_revision")
    if not isinstance(source_revision, Mapping):
        error("SOURCE_REVISION_MISSING", "source_revision is missing", "/evolution_contract/source_revision")
    else:
        digest = source_revision.get("source_set_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            error(
                "SOURCE_SET_DIGEST_INVALID",
                "source_set_sha256 must be a lowercase SHA-256 digest",
                "/evolution_contract/source_revision/source_set_sha256",
            )
        expected_id = f"sha256:{digest}" if isinstance(digest, str) else None
        if source_revision.get("source_revision_id") != expected_id:
            error(
                "SOURCE_REVISION_ID_MISMATCH",
                "source_revision_id does not match source_set_sha256",
                "/evolution_contract/source_revision/source_revision_id",
            )
    extractors = contract.get("extractors")
    if not isinstance(extractors, Mapping) or not extractors:
        error("EXTRACTOR_RESULTS_MISSING", "at least one extractor result is required", "/evolution_contract/extractors")
    integrity = contract.get("integrity")
    if not isinstance(integrity, Mapping):
        error("EVOLUTION_INTEGRITY_MISSING", "evolution integrity is missing", "/evolution_contract/integrity")
    else:
        expected = integrity.get("canonical_ir_sha256")
        body = copy.deepcopy(dict(contract))
        body.pop("integrity", None)
        try:
            actual = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        except CanonicalizationError as exc:
            error("EVOLUTION_CANONICALIZATION_FAILED", str(exc), "/evolution_contract")
        else:
            if expected != actual:
                error(
                    "EVOLUTION_INTEGRITY_MISMATCH",
                    "canonical_ir_sha256 does not match the contract payload",
                    "/evolution_contract/integrity/canonical_ir_sha256",
                )
    return diagnostics
