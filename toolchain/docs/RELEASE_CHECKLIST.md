# Release checklist

Run `make validate` first, then review the generated validation log.

```text
[ ] Frozen v10 release SHA-256 checks pass.
[ ] All Python files compile.
[ ] All v10 and v11 tests pass.
[ ] tools/build_v11_assets.py produces no diff on a second run.
[ ] tools/maintainability_metrics.py --check passes all size and import-cycle budgets.
[ ] Every schema passes Draft 2020-12 meta-validation.
[ ] Generated fixture contracts validate against the emitted schema bundle.
[ ] A pinned full Codex source revision runs end to end outside the restricted sandbox.
[ ] Current-main canary output is reviewed.
[ ] Unknown expressions are absent or explicitly accepted with diagnostics.
[ ] Semantic diff is reviewed and baseline updated intentionally.
[ ] Wheel and source distribution reproduce byte-for-byte with the fixed source epoch.
[ ] Wheel installs in a clean environment, package schemas load, and CLI self-test passes.
[ ] Release manifest, checksums, and ZIP integrity are verified.
```

## v13 closed release

- Run `python tools/validate_v13_release.py --json codex_wire_audit_v13_validation.json --log codex_wire_audit_v13_validation.txt --dist-dir dist`.
- Run `python tools/build_v13_release.py --validation-json codex_wire_audit_v13_validation.json --validation-log codex_wire_audit_v13_validation.txt --dist-dir dist --output-dir release_v13`.
- Repeat the release build in a second empty directory and require `diff -ru` to produce no output.
- Verify the full bundle's `bundle-manifest.json` and `SHA256SUMS.v13`, plus every external `.sha256` sidecar.

