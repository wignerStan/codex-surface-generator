from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unicodedata
import zipfile

from codex_wire_audit import EVOLUTION_CONTRACT_VERSION, GENERATOR_VERSION
from codex_wire_audit.canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    normalize_json,
)
from codex_wire_audit.diagnostics import Diagnostic, DiagnosticCollector
from codex_wire_audit.evolution import apply_turn_metadata_overlay, build_evolution_contract
from codex_wire_audit.legacy import load_legacy_modules, verify_frozen_v10_integrity
from codex_wire_audit.extractors.turn_metadata import TurnMetadataExtractor
from codex_wire_audit.models import (
    SourceFile,
    SourceGroup,
    SourceRevision,
    SourceSnapshot,
    SourceSpec,
)
from codex_wire_audit.schema_identity import (
    SchemaIdentityResolver,
    emit_schema_conflict_diagnostics,
    qualified_rust_name,
    schema_id_for_qualified_name,
)
from codex_wire_audit.schemas import schema_documents
from codex_wire_audit.semantic_diff import compare as semantic_compare
from codex_wire_audit.sources import (
    ArchiveLimits,
    SourceLoadError,
    load_archive_snapshot,
    load_repo_snapshot,
    safe_extract_archive,
)
from codex_wire_audit.source_registry import (
    SourceRegistry,
    SourceRegistryError,
    from_legacy_maps,
)
from codex_wire_audit.validation import validate_evolution_contract

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "codex_wire_audit_v11_fixtures"
SOURCE_PATH = "codex-rs/core/src/responses_metadata.rs"
SOURCE_SPEC_ID = "source_spec.base.metadata"


def metadata_spec(*, candidates: tuple[str, ...] = (SOURCE_PATH,), required: bool = True) -> SourceSpec:
    return SourceSpec(
        id=SOURCE_SPEC_ID,
        legacy_key="metadata",
        group=SourceGroup.BASE,
        path_candidates=candidates,
        required=required,
        roles=("responses_metadata", "turn_metadata_semantics"),
        expected_symbols=("CodexResponsesMetadata", "CodexTurnMetadataPayload", "turn_metadata_payload"),
        extractor_ids=("extractor.turn_metadata",),
    )


def make_snapshot(
    source: str,
    *,
    selected_path: str = SOURCE_PATH,
    extra_files: list[tuple[SourceSpec, str, bytes]] | None = None,
    source_mode: str = "fixture",
) -> tuple[SourceRegistry, SourceSnapshot]:
    spec = metadata_spec(candidates=(selected_path,))
    files = {
        spec.id: SourceFile.create(spec=spec, selected_path=selected_path, raw_bytes=source.encode("utf-8"))
    }
    specs = [spec]
    for extra_spec, path, raw in extra_files or []:
        specs.append(extra_spec)
        files[extra_spec.id] = SourceFile.create(
            spec=extra_spec,
            selected_path=path,
            raw_bytes=raw,
        )
    digest = SourceSnapshot.digest_files(files)
    snapshot = SourceSnapshot(
        revision=SourceRevision(
            source_mode=source_mode,
            repository="openai/codex",
            requested_ref="fixture",
            resolved_commit_sha=None,
            source_set_sha256=digest,
            dirty=None,
        ),
        files=files,
    )
    return SourceRegistry(specs), snapshot


def extract_fixture(name: str):
    source = (FIXTURES / name).read_text(encoding="utf-8")
    registry, snapshot = make_snapshot(source)
    diagnostics = DiagnosticCollector()
    result = TurnMetadataExtractor().extract(snapshot, diagnostics)
    return registry, snapshot, result, diagnostics


def contract_for_fixture(name: str) -> dict:
    registry, snapshot, _, _ = extract_fixture(name)
    diagnostics = DiagnosticCollector()
    contract, _ = build_evolution_contract(snapshot, registry, diagnostics, coverage_profile="fixture")
    return contract


class CanonicalizationTests(unittest.TestCase):
    def test_unicode_normalized_key_collision_is_rejected(self) -> None:
        with self.assertRaisesRegex(CanonicalizationError, "UNICODE_NORMALIZED_KEY_COLLISION"):
            canonical_json_bytes({"e\u0301": 1, "é": 2})

    def test_strings_and_keys_are_normalized_to_nfc(self) -> None:
        value = normalize_json({"e\u0301": "A\u030a"})
        self.assertEqual(value, {"é": "Å"})

    def test_canonical_output_is_stable(self) -> None:
        left = canonical_json_bytes({"b": 2, "a": [3, 1]})
        right = canonical_json_bytes({"a": [3, 1], "b": 2})
        self.assertEqual(left, right)
        self.assertTrue(left.endswith(b"\n"))

    def test_non_finite_numbers_are_rejected(self) -> None:
        with self.assertRaises(CanonicalizationError):
            canonical_json_bytes({"value": float("nan")})


class DiagnosticTests(unittest.TestCase):
    def test_diagnostic_id_is_semantic_and_stable(self) -> None:
        first = Diagnostic(
            code="FIELD_CHANGED",
            severity="warning",
            category="test",
            message="wording one",
            entity_id="field.a",
            details={"x": 1},
        )
        second = Diagnostic(
            code="FIELD_CHANGED",
            severity="warning",
            category="test",
            message="wording two",
            entity_id="field.a",
            details={"x": 1},
        )
        self.assertEqual(first.id, second.id)

    def test_collector_deduplicates_by_diagnostic_identity(self) -> None:
        collector = DiagnosticCollector()
        for wording in ("first", "second"):
            collector.emit(
                code="SOURCE_MOVED",
                severity="info",
                category="test",
                message=wording,
                entity_id="source.a",
                details={"path": "a.rs"},
            )
        self.assertEqual(len(collector.values()), 1)
        self.assertEqual(collector.summary(), {"error": 0, "warning": 0, "info": 1})


class TurnMetadataExtractionTests(unittest.TestCase):
    def test_current_identity_split_is_machine_evaluable(self) -> None:
        _, _, result, diagnostics = extract_fixture("responses_metadata_identity_split.rs")
        self.assertTrue(result.semantic_complete)
        self.assertEqual(diagnostics.summary(), {"error": 0, "warning": 0, "info": 0})
        gates = result.data["gates"]
        self.assertEqual(set(gates), {"has_request_identity", "has_thread_identity"})
        fields = result.data["fields"]
        self.assertEqual(fields["session_id"]["kind"], "conditional_value")
        self.assertEqual(fields["session_id"]["gate_refs"], ["turn_metadata.gate.has_thread_identity"])
        self.assertEqual(fields["turn_id"]["kind"], "option_passthrough")
        self.assertEqual(fields["turn_id"]["gate_refs"], [])
        self.assertEqual(fields["session_id"]["identity_domain"], "thread_identity")
        self.assertEqual(fields["turn_id"]["identity_domain"], "turn_identity")
        self.assertEqual(fields["agent_name"]["kind"], "conditional_option_passthrough")

    def test_legacy_combined_gate_remains_explicit(self) -> None:
        _, _, result, diagnostics = extract_fixture("responses_metadata_combined_identity.rs")
        self.assertTrue(result.semantic_complete)
        self.assertIn("has_turn_identity", result.data["gates"])
        self.assertEqual(result.data["fields"]["turn_id"]["kind"], "conditional_option_passthrough")
        self.assertIn(
            "LEGACY_COMBINED_IDENTITY_GATE_OBSERVED",
            {item.code for item in diagnostics.values()},
        )

    def test_unknown_expression_is_never_silently_passed_through(self) -> None:
        _, _, result, diagnostics = extract_fixture("responses_metadata_unclassified.rs")
        self.assertFalse(result.semantic_complete)
        self.assertEqual(result.data["fields"]["turn_id"]["kind"], "unclassified")
        self.assertIn(
            "FIELD_EMISSION_EXPRESSION_UNCLASSIFIED",
            {item.code for item in diagnostics.values()},
        )

    def test_missing_gate_definition_is_an_error(self) -> None:
        source = """
        impl CodexResponsesMetadata {
            fn turn_metadata_payload(&self) -> CodexTurnMetadataPayload<'_> {
                CodexTurnMetadataPayload {
                    session_id: missing_gate.then_some(self.session_id.as_str()),
                    extra: &self.extra,
                }
            }
        }
        """
        _, snapshot = make_snapshot(source)
        diagnostics = DiagnosticCollector()
        result = TurnMetadataExtractor().extract(snapshot, diagnostics)
        self.assertFalse(result.semantic_complete)
        self.assertIn("FIELD_GATE_DEFINITION_MISSING", {item.code for item in diagnostics.values()})

    def test_formatting_only_change_preserves_semantic_digest(self) -> None:
        source = (FIXTURES / "responses_metadata_identity_split.rs").read_text(encoding="utf-8")
        formatted = source.replace(
            "turn_id: self.turn_id.as_deref(),",
            "// formatting-only comment\n            turn_id :\n                self.turn_id\n                    .as_deref ( ) ,",
        )
        _, snapshot_a = make_snapshot(source)
        _, snapshot_b = make_snapshot(formatted)
        result_a = TurnMetadataExtractor().extract(snapshot_a, DiagnosticCollector())
        result_b = TurnMetadataExtractor().extract(snapshot_b, DiagnosticCollector())
        self.assertEqual(result_a.data["semantic_digest"], result_b.data["semantic_digest"])

    def test_field_order_is_preserved_for_wire_review(self) -> None:
        _, _, result, _ = extract_fixture("responses_metadata_identity_split.rs")
        self.assertEqual(result.data["field_order"][:5], [
            "installation_id", "session_id", "thread_id", "agent_name", "turn_id"
        ])


class EvolutionContractTests(unittest.TestCase):
    def test_contract_has_valid_integrity_and_multidimensional_status(self) -> None:
        contract = contract_for_fixture("responses_metadata_identity_split.rs")
        self.assertEqual(contract["schema_version"], EVOLUTION_CONTRACT_VERSION)
        self.assertEqual(contract["generator_version"], GENERATOR_VERSION)
        self.assertEqual(contract["migration"]["stage"], "hybrid_canonical_ir")
        self.assertEqual(contract["status"]["semantic_classification"], "complete")
        self.assertEqual(validate_evolution_contract(contract), [])

    def test_integrity_tampering_is_detected(self) -> None:
        contract = contract_for_fixture("responses_metadata_identity_split.rs")
        contract["extractors"]["extractor.turn_metadata"]["data"]["fields"]["turn_id"]["kind"] = "tampered"
        errors = validate_evolution_contract(contract)
        self.assertIn("EVOLUTION_INTEGRITY_MISMATCH", {item["code"] for item in errors})

    def test_overlay_replaces_stale_identity_assumptions(self) -> None:
        _, _, result, _ = extract_fixture("responses_metadata_identity_split.rs")
        report = {
            "turn_metadata_construction": {"identity_gates": {"has_turn_identity": "stale"}},
            "turn_metadata_schema": {"notes": ["has_turn_identity stale note"]},
            "metadata_protocol": {
                "nested_turn_metadata": {
                    "field_groups": {"turn_identity": ["session_id", "thread_id", "turn_id"]}
                }
            },
            "mcp_protocol": {
                "turn_metadata_projection": {"identity_effect": {"has_turn_identity": True}}
            },
            "relation_map": {
                "relation_types": {},
                "groups": [
                    {
                        "concept": "session_id",
                        "nodes": [{"relation": "same_value_when_turn_identity"}],
                    }
                ],
            },
        }
        apply_turn_metadata_overlay(report, result)
        identity = report["mcp_protocol"]["turn_metadata_projection"]["identity_effect"]
        self.assertNotIn("has_turn_identity", identity)
        self.assertTrue(identity["has_thread_identity"])
        self.assertTrue(identity["turn_identity_independent"])
        groups = report["metadata_protocol"]["nested_turn_metadata"]["field_groups"]
        self.assertEqual(groups["thread_identity"], ["agent_name", "session_id", "thread_id"])
        self.assertEqual(groups["turn_identity"], ["turn_id"])
        relation = report["relation_map"]["groups"][0]["nodes"][0]["relation"]
        self.assertEqual(relation, "same_value_when_thread_identity")
        self.assertTrue(report["turn_metadata_semantics"]["authoritative"])


class SemanticDiffTests(unittest.TestCase):
    def test_identity_split_produces_gate_and_turn_id_deltas(self) -> None:
        old = contract_for_fixture("responses_metadata_combined_identity.rs")
        new = contract_for_fixture("responses_metadata_identity_split.rs")
        diff = semantic_compare(old, new)
        kinds = {(item["kind"], item["entity_id"]) for item in diff["changes"]}
        self.assertIn(("gate_removed", "turn_metadata.gate.has_turn_identity"), kinds)
        self.assertIn(("gate_added", "turn_metadata.gate.has_thread_identity"), kinds)
        self.assertIn(("field_gate_changed", "turn_metadata.field.turn_id"), kinds)
        self.assertIn(("field_emission_kind_changed", "turn_metadata.field.turn_id"), kinds)
        self.assertTrue(diff["summary"]["has_breaking_changes"])

    def test_semantic_change_ids_do_not_depend_on_input_order(self) -> None:
        old = contract_for_fixture("responses_metadata_combined_identity.rs")
        new = contract_for_fixture("responses_metadata_identity_split.rs")
        first = semantic_compare(old, new)
        old_reordered = json.loads(json.dumps(old, sort_keys=True))
        new_reordered = json.loads(json.dumps(new, sort_keys=True))
        second = semantic_compare(old_reordered, new_reordered)
        self.assertEqual(
            [item["id"] for item in first["changes"]],
            [item["id"] for item in second["changes"]],
        )

    def test_identical_contract_has_no_changes(self) -> None:
        contract = contract_for_fixture("responses_metadata_identity_split.rs")
        diff = semantic_compare(contract, copy.deepcopy(contract))
        self.assertEqual(diff["summary"]["total"], 0)


class SourceRegistryTests(unittest.TestCase):
    def test_legacy_registry_adds_metadata_path_fallback_and_extractor(self) -> None:
        registry = from_legacy_maps(
            {"metadata": SOURCE_PATH, "client": "codex-rs/core/src/client.rs"},
            {"endpoint_mod": "codex-rs/codex-api/src/endpoint/mod.rs"},
            {"config": "codex-rs/core/src/config/mod.rs"},
        )
        spec = registry.get(SOURCE_SPEC_ID)
        self.assertEqual(spec.path_candidates[1], "codex-rs/core/src/responses/metadata.rs")
        self.assertEqual(spec.extractor_ids, ("extractor.turn_metadata",))
        self.assertTrue(spec.required)

    def test_registry_overlay_can_add_path_candidate_without_code_change(self) -> None:
        registry = SourceRegistry([metadata_spec()])
        updated = registry.with_overlay(
            {
                "schema_version": "1.0.0",
                "sources": {
                    SOURCE_SPEC_ID: {
                        "path_candidates": [
                            "codex-rs/core/src/new/responses_metadata.rs",
                            SOURCE_PATH,
                        ]
                    }
                },
            }
        )
        self.assertEqual(updated.get(SOURCE_SPEC_ID).path_candidates[0], "codex-rs/core/src/new/responses_metadata.rs")

    def test_registry_rejects_unsafe_paths(self) -> None:
        with self.assertRaises(SourceRegistryError):
            SourceRegistry([metadata_spec(candidates=("../escape.rs",))])

    def test_registry_rejects_duplicate_legacy_slots(self) -> None:
        duplicate = SourceSpec(
            id="source_spec.base.other",
            legacy_key="metadata",
            group=SourceGroup.BASE,
            path_candidates=("other.rs",),
            required=True,
        )
        with self.assertRaises(SourceRegistryError):
            SourceRegistry([metadata_spec(), duplicate])


class SourceProviderTests(unittest.TestCase):
    def _git_repo(self, source: bytes) -> tuple[tempfile.TemporaryDirectory[str], Path, SourceRegistry]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        target = root / SOURCE_PATH
        target.parent.mkdir(parents=True)
        target.write_bytes(source)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
        return temporary, root, SourceRegistry([metadata_spec()])

    def test_git_tree_reads_committed_bytes_and_ignores_dirty_worktree(self) -> None:
        original = (FIXTURES / "responses_metadata_identity_split.rs").read_bytes()
        temporary, root, registry = self._git_repo(original)
        self.addCleanup(temporary.cleanup)
        (root / SOURCE_PATH).write_bytes(b"dirty worktree\n")
        result = load_repo_snapshot(
            registry,
            root,
            repository="openai/codex",
            requested_ref="HEAD",
            worktree=False,
        )
        self.assertEqual(result.snapshot.files[SOURCE_SPEC_ID].raw_bytes, original)
        self.assertEqual(result.snapshot.revision.source_mode, "git_tree")
        self.assertIn("WORKTREE_MODIFICATIONS_IGNORED", {item.code for item in result.diagnostics.values()})

    def test_worktree_requires_explicit_dirty_opt_in(self) -> None:
        original = (FIXTURES / "responses_metadata_identity_split.rs").read_bytes()
        temporary, root, registry = self._git_repo(original)
        self.addCleanup(temporary.cleanup)
        (root / SOURCE_PATH).write_bytes(b"dirty worktree\n")
        with self.assertRaisesRegex(SourceLoadError, "allow-dirty-source"):
            load_repo_snapshot(
                registry,
                root,
                repository="openai/codex",
                requested_ref="HEAD",
                worktree=True,
                allow_dirty=False,
            )

    def test_fallback_path_is_selected_and_reported(self) -> None:
        source = (FIXTURES / "responses_metadata_identity_split.rs").read_bytes()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fallback = "codex-rs/core/src/responses/metadata.rs"
        target = root / fallback
        target.parent.mkdir(parents=True)
        target.write_bytes(source)
        registry = SourceRegistry([metadata_spec(candidates=(SOURCE_PATH, fallback))])
        result = load_repo_snapshot(
            registry,
            root,
            repository="openai/codex",
            requested_ref="fixture",
            worktree=True,
            allow_dirty=True,
        )
        selected = result.snapshot.files[SOURCE_SPEC_ID]
        self.assertEqual(selected.selected_path, fallback)
        self.assertEqual(selected.path_candidate_index, 1)
        self.assertIn("SOURCE_PATH_FALLBACK_SELECTED", {item.code for item in result.diagnostics.values()})

    def test_source_set_digest_includes_non_rust_inputs(self) -> None:
        source = (FIXTURES / "responses_metadata_identity_split.rs").read_text(encoding="utf-8")
        config_spec = SourceSpec(
            id="source_spec.extra.config_json",
            legacy_key="config_json",
            group=SourceGroup.EXTRA,
            path_candidates=("config/example.json",),
            required=False,
        )
        _, first = make_snapshot(source, extra_files=[(config_spec, "config/example.json", b'{"a":1}\n')])
        _, second = make_snapshot(source, extra_files=[(config_spec, "config/example.json", b'{"a":2}\n')])
        self.assertNotEqual(first.revision.source_set_sha256, second.revision.source_set_sha256)

    def test_zip_archive_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../escape", "x")
            with self.assertRaisesRegex(SourceLoadError, "unsafe archive member"):
                safe_extract_archive(archive, Path(directory) / "out")

    def test_zip_archive_rejects_casefold_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("A/file.rs", "x")
                handle.writestr("a/file.rs", "y")
            with self.assertRaisesRegex(SourceLoadError, "case-folded"):
                safe_extract_archive(archive, Path(directory) / "out")

    def test_zip_archive_rejects_unicode_normalized_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.zip"
            first = "codex-rs/e\u0301.rs"
            second = "codex-rs/é.rs"
            self.assertEqual(unicodedata.normalize("NFC", first), unicodedata.normalize("NFC", second))
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(first, "x")
                handle.writestr(second, "y")
            with self.assertRaisesRegex(SourceLoadError, "Unicode-normalized"):
                safe_extract_archive(archive, Path(directory) / "out")

    def test_archive_file_size_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "large.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
                handle.writestr("codex-rs/file.rs", b"x" * 10)
            with self.assertRaisesRegex(SourceLoadError, "file-size limit"):
                safe_extract_archive(
                    archive,
                    Path(directory) / "out",
                    limits=ArchiveLimits(max_file_bytes=5),
                )

    def test_archive_snapshot_uses_source_set_identity(self) -> None:
        source = (FIXTURES / "responses_metadata_identity_split.rs").read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr(f"codex-main/{SOURCE_PATH}", source)
            result = load_archive_snapshot(
                SourceRegistry([metadata_spec()]),
                archive,
                repository="openai/codex",
                requested_ref="main",
                source_commit="a" * 40,
            )
            revision = result.snapshot.revision
            self.assertEqual(revision.source_mode, "source_archive")
            self.assertEqual(revision.resolved_commit_sha, "a" * 40)
            self.assertEqual(
                revision.to_dict()["source_revision_id"],
                f"sha256:{revision.source_set_sha256}",
            )


class SchemaIdentityTests(unittest.TestCase):
    def test_module_local_types_receive_distinct_schema_ids(self) -> None:
        report = {
            "a": {
                "struct": "Config",
                "fields": [{"name": "x", "source": {"path": "codex-rs/a/src/config.rs"}}],
            },
            "b": {
                "struct": "Config",
                "fields": [{"name": "x", "source": {"path": "codex-rs/b/src/config.rs"}}],
            },
        }
        resolver = SchemaIdentityResolver.from_report(report)
        ids = sorted(resolver.metadata)
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(ids[0], ids[1])
        self.assertIn("schema.rust.a.config.config", ids)
        self.assertIn("schema.rust.b.config.config", ids)

    def test_path_hash_fallback_is_collision_safe(self) -> None:
        first = qualified_rust_name("vendor/a.rs", "Config")
        second = qualified_rust_name("vendor/b.rs", "Config")
        self.assertNotEqual(first, second)
        self.assertNotEqual(schema_id_for_qualified_name(first), schema_id_for_qualified_name(second))

    def test_schema_conflict_becomes_error_diagnostic(self) -> None:
        report = {
            "entities": {
                "fields": {
                    "field.test": {
                        "conflicts": [{"wire_schema": {"type": "string"}}, {"wire_schema": {"type": "boolean"}}]
                    }
                }
            }
        }
        diagnostics = DiagnosticCollector()
        emit_schema_conflict_diagnostics(report, diagnostics)
        item = diagnostics.values()[0]
        self.assertEqual(item.code, "SCHEMA_FIELD_SHAPE_CONFLICT")
        self.assertEqual(item.severity, "error")
        self.assertTrue(item.strict_failure)


class SchemaDocumentTests(unittest.TestCase):
    def test_fixture_contract_validates_against_emitted_schema_bundle(self) -> None:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource

        documents = schema_documents()
        registry = Registry()
        for document in documents.values():
            registry = registry.with_resource(document["$id"], Resource.from_contents(document))
        validator = Draft202012Validator(
            documents["evolution-contract.schema.json"],
            registry=registry,
        )
        errors = list(validator.iter_errors(contract_for_fixture("responses_metadata_identity_split.rs")))
        self.assertEqual(errors, [])

    def test_maintainability_schemas_are_strict_and_serializable(self) -> None:
        documents = schema_documents()
        self.assertEqual(
            set(documents),
            {
                "diagnostic.schema.json",
                "source-registry.schema.json",
                "source-snapshot.schema.json",
                "predicate.schema.json",
                "turn-metadata-semantics.schema.json",
                "evolution-contract.schema.json",
                "semantic-diff.schema.json",
            },
        )
        from jsonschema import Draft202012Validator
        for document in documents.values():
            encoded = json.dumps(document, sort_keys=True)
            self.assertIn("$schema", encoded)
            self.assertFalse(document.get("additionalProperties") is True)
            Draft202012Validator.check_schema(document)


class CompatibilityTests(unittest.TestCase):
    def test_v11_cli_self_test(self) -> None:
        result = subprocess.run(
            [sys.executable, "codex_wire_audit_v11.py", "--self-test"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("v11 maintainability self-test: ok", result.stdout)

    def test_v10_sources_match_the_frozen_release_hashes(self) -> None:
        self.assertEqual(verify_frozen_v10_integrity(ROOT, include_tests=True), ())

    def test_mutated_v10_boundary_is_rejected_before_loading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = (
                "_codex_wire_audit_base_v10.py",
                "_codex_wire_contract_v10.py",
                "codex_wire_audit_v10.py",
            )
            for name in names:
                (root / name).write_bytes((ROOT / name).read_bytes())
            with (root / "codex_wire_audit_v10.py").open("ab") as stream:
                stream.write(b"\n# accidental compatibility edit\n")
            with self.assertRaisesRegex(RuntimeError, "frozen v10 compatibility boundary changed"):
                load_legacy_modules(root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
