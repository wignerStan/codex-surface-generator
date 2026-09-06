from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess
from typing import Any, Iterable

from .proof_diagnostics import DiagnosticBag, ProofDiagnostic
from .proof_metadata_history import build_metadata_history_result
from .proof_profiles import evaluate_profile, find_profile_id, resolve_profile
from .proof_provenance import verify_git_source
from .proof_schema_validation import report_sha256, validate_report, verify_attestation
from .rust_semantics import analyze_turn_metadata_source

PROOF_FORMAT = "codex-wire-audit-proof-attestation/v2"
PROOF_SCHEMA = "https://openai.example/codex-wire-audit/proof-attestation-v2.schema.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("ascii")
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False) as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _deterministic_time() -> str:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _source_paths_from_report(value: Any) -> tuple[str, ...]:
    paths: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"selected_path", "source_path", "path"} and isinstance(child, str):
                    if child.endswith((".rs", ".toml", ".json")):
                        paths.add(child)
                elif key not in {"diagnostics", "candidate_paths", "candidates"}:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return tuple(sorted(paths))


def _find_turn_metadata_source(root: Path, loaded_paths: Iterable[str]) -> Path | None:
    preferred = [
        path for path in loaded_paths
        if path.endswith("responses_metadata.rs") or path.endswith("turn_metadata.rs")
    ]
    for relative in preferred:
        candidate = root / relative
        if candidate.is_file():
            content = candidate.read_text(encoding="utf-8", errors="replace")
            if "CodexTurnMetadataPayload" in content:
                return candidate
    for relative in (
        "codex-rs/core/src/responses_metadata.rs",
        "core/src/responses_metadata.rs",
        "src/responses_metadata.rs",
    ):
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def _runtime_result(
    report: dict[str, Any],
    profile_id: str,
    evidence: dict[str, Any] | None,
    bag: DiagnosticBag,
) -> dict[str, Any]:
    profile = resolve_profile(profile_id)
    required = set(profile.required_runtime_scenarios)
    if evidence is None:
        return {
            "status": "not_run",
            "complete": not required,
            "report_bound": False,
            "required_scenarios": sorted(required),
            "passed_scenarios": [],
            "missing_scenarios": sorted(required),
        }

    scenarios = evidence.get("scenarios", [])
    passed = {
        item.get("id") for item in scenarios
        if isinstance(item, dict) and item.get("status") == "passed" and isinstance(item.get("id"), str)
    }
    bound = evidence.get("report_sha256") == report_sha256(report)
    missing = sorted(required - passed)
    if not bound:
        bag.add(ProofDiagnostic(
            code="RUNTIME_EVIDENCE_REPORT_MISMATCH",
            message="runtime evidence is not bound to this report digest",
        ))
    for scenario in missing:
        bag.add(ProofDiagnostic(
            code="RUNTIME_SCENARIO_MISSING",
            message=f"required runtime scenario did not pass: {scenario}",
            source_id=scenario,
        ))
    complete = bound and not missing and evidence.get("status") == "complete"
    return {
        "status": "complete" if complete else "incomplete",
        "complete": complete,
        "report_bound": bound,
        "required_scenarios": sorted(required),
        "passed_scenarios": sorted(passed),
        "missing_scenarios": missing,
        "evidence_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def build_attestation(
    report: dict[str, Any],
    *,
    profile_id: str | None,
    schema_dir: str | Path | None,
    source_root: str | Path | None,
    requested_ref: str,
    source_mode: str,
    allow_dirty_source: bool,
    runtime_evidence: dict[str, Any] | None = None,
    metadata_history_catalog: str | Path | None = None,
    metadata_history_version: str | None = None,
) -> dict[str, Any]:
    bag = DiagnosticBag()

    schema_diagnostics = validate_report(report, schema_dir)
    bag.extend(schema_diagnostics)

    selected_profile = profile_id or find_profile_id(report) or "hybrid_v13"
    profile_evaluation, profile_diagnostics = evaluate_profile(report, selected_profile)
    bag.extend(profile_diagnostics)
    try:
        profile = resolve_profile(selected_profile)
    except ValueError:
        profile = None
    required_dimensions = (
        profile.required_dimensions
        if profile is not None
        else ("schema", "profile", "rust_semantics", "provenance", "metadata_history")
    )

    metadata_history_result, history_diagnostics, history_source_paths = build_metadata_history_result(
        profile=profile,
        source_root=source_root,
        requested_ref=requested_ref,
        source_mode=source_mode,
        catalog_path=metadata_history_catalog,
        version_id=metadata_history_version,
    )
    if "metadata_history" in required_dimensions or metadata_history_catalog is not None or metadata_history_version is not None:
        bag.extend(history_diagnostics)

    loaded_paths = tuple(sorted(set(_source_paths_from_report(report)) | set(history_source_paths)))
    rust_required = "rust_semantics" in required_dimensions
    provenance_required = "provenance" in required_dimensions
    rust_result: dict[str, Any] = {
        "status": "not_run",
        "reason": "dimension_not_required" if not rust_required else "source_root_not_provided",
        "complete": not rust_required,
    }
    provenance_result: dict[str, Any] = {
        "status": "not_run",
        "reason": "dimension_not_required" if not provenance_required else "source_root_not_provided",
        "complete": not provenance_required,
    }

    if source_root is not None and (rust_required or provenance_required):
        root = Path(source_root).resolve()
        provenance = verify_git_source(
            root,
            requested_ref,
            loaded_paths,
            mode=source_mode,
            allow_dirty=allow_dirty_source,
        )
        provenance_result = provenance.to_dict()
        provenance_result["status"] = "complete" if provenance.complete else "incomplete"
        if provenance_required:
            bag.extend(provenance.diagnostics)

        if rust_required:
            turn_source = _find_turn_metadata_source(root, loaded_paths)
            if turn_source is None:
                diagnostic = ProofDiagnostic(
                    code="TURN_METADATA_SOURCE_UNRESOLVED",
                    message="could not locate responses_metadata.rs under the supplied source root",
                )
                bag.add(diagnostic)
                rust_result = {
                    "status": "incomplete",
                    "complete": False,
                    "diagnostics": [diagnostic.to_dict()],
                }
            else:
                # Commit mode must analyze the same object bytes proven above, not the worktree.
                if source_mode == "commit" and provenance.requested_sha:
                    relative = turn_source.relative_to(root).as_posix()
                    process = subprocess.run(
                        ["git", "-C", str(root), "show", f"{provenance.requested_sha}:{relative}"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        check=False,
                    )
                    source_text = process.stdout.decode("utf-8", "strict") if process.returncode == 0 else turn_source.read_text(encoding="utf-8", errors="strict")
                else:
                    source_text = turn_source.read_text(encoding="utf-8", errors="strict")
                analysis = analyze_turn_metadata_source(source_text)
                rust_result = analysis.to_dict()
                rust_result["status"] = "complete" if analysis.complete else "incomplete"
                rust_result["source_path"] = turn_source.relative_to(root).as_posix()
                bag.extend(analysis.diagnostics)

    runtime_required = "runtime_conformance" in required_dimensions
    if profile is None:
        runtime_result: dict[str, Any] = {
            "status": "incomplete",
            "complete": False,
            "required_scenarios": [],
            "passed_scenarios": [],
            "missing_scenarios": [],
        }
    elif runtime_required or runtime_evidence is not None:
        before = DiagnosticBag()
        runtime_result = _runtime_result(report, selected_profile, runtime_evidence, before)
        if runtime_required or runtime_evidence is not None:
            bag.extend(before.values())
    else:
        runtime_result = {
            "status": "not_run",
            "complete": True,
            "report_bound": False,
            "required_scenarios": [],
            "passed_scenarios": [],
            "missing_scenarios": [],
        }

    dimensions: dict[str, dict[str, Any]] = {
        "schema": {
            "status": "complete" if not schema_diagnostics else "incomplete",
            "complete": not schema_diagnostics,
        },
        "profile": {
            "status": "complete" if profile_evaluation.get("complete") else "incomplete",
            "complete": bool(profile_evaluation.get("complete")),
        },
        "rust_semantics": {
            "status": rust_result.get("status", "not_run"),
            "complete": bool(rust_result.get("complete")),
        },
        "provenance": {
            "status": provenance_result.get("status", "not_run"),
            "complete": bool(provenance_result.get("complete")),
        },
        "metadata_history": {
            "status": metadata_history_result["status"],
            "complete": metadata_history_result["complete"],
        },
        "runtime_conformance": {
            "status": runtime_result["status"],
            "complete": runtime_result["complete"],
        },
    }

    missing_dimensions = [
        name for name in required_dimensions
        if not dimensions.get(name, {}).get("complete")
    ]
    for name in missing_dimensions:
        bag.add(ProofDiagnostic(
            code="REQUIRED_PROOF_DIMENSION_INCOMPLETE",
            message=f"required proof dimension is incomplete: {name}",
            source_id=name,
        ))

    complete = not missing_dimensions and not bag.has_strict_failure()
    attestation: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "schema_id": PROOF_SCHEMA,
        "format": PROOF_FORMAT,
        "generated_at": _deterministic_time(),
        "report_sha256": report_sha256(report),
        "coverage_profile": profile_evaluation,
        "dimensions": dimensions,
        "rust_semantics": rust_result,
        "provenance": provenance_result,
        "metadata_history": metadata_history_result,
        "runtime_conformance": runtime_result,
        "status": {
            "overall": "complete" if complete else "incomplete",
            "complete": complete,
            "required_dimensions": list(required_dimensions),
            "incomplete_dimensions": missing_dimensions,
        },
        "diagnostics": bag.to_list(),
        "diagnostic_summary": {
            "total": len(bag.values()),
            "strict_failures": sum(1 for item in bag.values() if item.strict_failure),
            "errors": sum(1 for item in bag.values() if item.severity in {"error", "critical"}),
            "warnings": sum(1 for item in bag.values() if item.severity == "warning"),
        },
    }
    payload = json.dumps(
        attestation, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    attestation["integrity"] = {
        "algorithm": "sha256",
        "canonical_payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    return attestation


def _self_test() -> int:
    from .proof_diagnostics import merge_diagnostics
    from .proof_profiles import evaluate_profile

    strong = ProofDiagnostic(
        code="X", message="strong", severity="error",
        strict_failure=True, recoverable=False,
    )
    weak = ProofDiagnostic(
        code="X", message="weak", severity="warning",
        strict_failure=False, recoverable=True,
    )
    merged = merge_diagnostics(strong, weak)
    assert merged.severity == "error"
    assert merged.strict_failure
    assert not merged.recoverable

    report = {
        "extractors": {
            "extractor.turn_metadata": {"id": "extractor.turn_metadata"}
        },
        "legacy_machine_reconstruction_remaining": True,
    }
    evaluation, _ = evaluate_profile(report, "codex_wire_full")
    assert not evaluation["complete"]
    assert "extractor.responses_request" in evaluation["missing_extractors"]

    spread = analyze_turn_metadata_source(
        "fn x(){ CodexTurnMetadataPayload { turn_id: self.turn_id, ..self.base() } }"
    )
    assert not spread.complete
    assert any(item.code == "STRUCT_SPREAD_UNRESOLVED" for item in spread.diagnostics)

    macro = analyze_turn_metadata_source(
        "fn x(){ CodexTurnMetadataPayload { payload_fields!() } }"
    )
    assert not macro.complete
    assert any(item.code == "STRUCT_MACRO_UNRESOLVED" for item in macro.diagnostics)

    before = analyze_turn_metadata_source(
        "impl K { fn pred(self)->bool{true} fn x(){ CodexTurnMetadataPayload { a: Some(K::pred) } } }"
    )
    after = analyze_turn_metadata_source(
        "impl K { fn pred(self)->bool{false} fn x(){ CodexTurnMetadataPayload { a: Some(K::pred) } } }"
    )
    assert before.semantic_sha256 != after.semantic_sha256
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independently verify a Codex wire-audit report and emit a CI attestation."
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--profile")
    parser.add_argument("--schema-dir", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--source-mode", choices=("worktree", "commit"), default="worktree")
    parser.add_argument("--allow-dirty-source", action="store_true")
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--metadata-history-catalog", type=Path)
    parser.add_argument("--metadata-history-version")
    parser.add_argument("--fail-on", choices=("incomplete", "never"), default="incomplete")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.report is None:
        raise ValueError("--report is required unless --self-test is used")

    attestation_path = (
        args.attestation
        or args.report.with_suffix(args.report.suffix + ".proof.json")
    )
    report = json.loads(args.report.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("report root must be a JSON object")

    runtime_evidence = None
    if args.runtime_evidence is not None:
        runtime_evidence = json.loads(args.runtime_evidence.read_text(encoding="utf-8"))
        if not isinstance(runtime_evidence, dict):
            raise ValueError("runtime evidence root must be a JSON object")

    attestation = build_attestation(
        report,
        profile_id=args.profile,
        schema_dir=args.schema_dir,
        source_root=args.source_root,
        requested_ref=args.ref,
        source_mode=args.source_mode,
        allow_dirty_source=args.allow_dirty_source,
        runtime_evidence=runtime_evidence,
        metadata_history_catalog=args.metadata_history_catalog,
        metadata_history_version=args.metadata_history_version,
    )
    attestation_errors = verify_attestation(attestation)
    if attestation_errors:
        raise RuntimeError("generated proof attestation failed self-validation: " + "; ".join(item.code for item in attestation_errors))
    # Durable evidence comes before the policy decision.
    _atomic_json(attestation_path, attestation)
    if args.fail_on == "incomplete" and not attestation["status"]["complete"]:
        return 3
    return 0


def _failure_attestation_path(arguments: list[str]) -> Path | None:
    for index, item in enumerate(arguments):
        if item == "--attestation" and index + 1 < len(arguments):
            return Path(arguments[index + 1])
        if item.startswith("--attestation="):
            return Path(item.split("=", 1)[1])
    return None


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        return _main(arguments)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        if "--debug" in arguments:
            raise
        failure_path = _failure_attestation_path(arguments)
        if failure_path is not None:
            failure: dict[str, Any] = {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "schema_id": PROOF_SCHEMA,
                "format": PROOF_FORMAT,
                "generated_at": _deterministic_time(),
                "status": {"overall": "error", "complete": False},
                "diagnostics": [ProofDiagnostic(
                    code="PROOF_OPERATIONAL_FAILURE",
                    message=str(error),
                    severity="error",
                    strict_failure=True,
                    recoverable=False,
                ).to_dict()],
            }
            payload = json.dumps(
                failure, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
            failure["integrity"] = {
                "algorithm": "sha256",
                "canonical_payload_sha256": hashlib.sha256(payload).hexdigest(),
            }
            try:
                _atomic_json(failure_path, failure)
            except OSError:
                pass
        print(f"codex-wire-audit-proof: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
