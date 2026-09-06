PYTHON ?= python
HISTORY_CONTAINER ?= codex_wire_audit/metadata_history_container_data

.PHONY: compile test test-v10 test-v11 test-v13 test-v16 test-v18 self-test schemas \
	history-assets check-history-assets legacy-assets check-legacy-assets \
	assets check-assets metrics check-metrics coverage dist validate release verify-release clean

compile:
	$(PYTHON) -m compileall -q codex_wire_audit tests tools scripts
	$(PYTHON) -m py_compile codex_wire_audit_v11.py test_codex_wire_audit_v10.py test_codex_wire_audit_v11.py validate_codex_wire_fixtures_v10.py

test-v10:
	$(PYTHON) -m unittest -v test_codex_wire_audit_v10.py

test-v11:
	$(PYTHON) -m unittest -v test_codex_wire_audit_v11.py

test-v13:
	$(PYTHON) -m pytest -q tests/test_metadata_history_v13.py tests/test_release_builder_v13.py

test-v16:
	$(PYTHON) -m pytest -q tests/test_context_management_v16.py

test-v18:
	$(PYTHON) -m pytest -q tests/test_release_pipeline_v18.py

test: compile
	$(PYTHON) -m pytest -q

self-test:
	$(PYTHON) -m codex_wire_audit.proof_gate --self-test
	$(PYTHON) -m codex_wire_audit.history_cli --self-test
	$(PYTHON) -m codex_wire_audit.release_contract --output /dev/null

schemas:
	$(PYTHON) -c "from jsonschema import Draft202012Validator; from codex_wire_audit.metadata_history import load_history_catalog, validate_history_catalog; from codex_wire_audit.proof_schema_validation import load_schemas; _, docs = load_schemas(); assert docs and not validate_history_catalog(load_history_catalog()); [Draft202012Validator.check_schema(s) for s in docs.values()]; print(f'schemas: {len(docs)} documents; history catalog: valid')"

history-assets:
	rm -rf $(HISTORY_CONTAINER)
	$(PYTHON) -m codex_wire_audit.history_cli --output-dir $(HISTORY_CONTAINER) --output /dev/null
	$(PYTHON) -m codex_wire_audit.history_cli --verify-container $(HISTORY_CONTAINER) --output /dev/null

check-history-assets:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m codex_wire_audit.history_cli --output-dir $$tmp/container --output $$tmp/build.json >/dev/null; \
	$(PYTHON) -m codex_wire_audit.history_cli --verify-container $$tmp/container --output $$tmp/verify.json >/dev/null; \
	diff -ru $(HISTORY_CONTAINER) $$tmp/container; \
	rm -rf $$tmp

legacy-assets:
	$(PYTHON) tools/build_v11_assets.py

check-legacy-assets:
	@tmp=$$(mktemp -d); \
	$(PYTHON) tools/build_v11_assets.py --output-root $$tmp >/dev/null; \
	diff -ru codex_wire_audit_v11_schemas $$tmp/codex_wire_audit_v11_schemas; \
	diff -ru codex_wire_audit_v11_examples $$tmp/codex_wire_audit_v11_examples; \
	rm -rf $$tmp

metrics:
	$(PYTHON) tools/maintainability_metrics.py --write

check-metrics:
	$(PYTHON) tools/maintainability_metrics.py --check

assets: legacy-assets history-assets metrics

check-assets: check-legacy-assets check-history-assets check-metrics schemas

coverage:
	coverage erase
	coverage run --branch -m pytest -q
	coverage json -o coverage-v18.json
	$(PYTHON) tools/check_v13_coverage.py coverage-v18.json --output codex_wire_audit_v18_coverage_ratchet.json

# Convenience-only distribution build. The release command never trusts this directory.
dist:
	$(PYTHON) tools/build_distribution.py --output-dir dist --manifest dist-manifest.json

# Lightweight source/package contract validation. Full validation is inseparable from release assembly.
validate: test schemas
	$(PYTHON) -m codex_wire_audit.release_contract --output codex_wire_audit_v18_source_capabilities.json

release:
	$(PYTHON) tools/release_pipeline.py release --output-dir release_v18

verify-release:
	$(PYTHON) tools/release_pipeline.py verify --release-dir release_v18

clean:
	rm -rf build dist release_v13 release_v16 release_v18 *.egg-info .pytest_cache .coverage coverage-v13.json coverage-v18.json ci-out
	rm -f codex_wire_audit_v*_validation.json codex_wire_audit_v*_validation.txt codex_wire_audit_v18_source_capabilities.json dist-manifest.json
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
