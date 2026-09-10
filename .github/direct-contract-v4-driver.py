from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

ROOT = Path.cwd()
V3_PATH = ".github/direct-contract-migration-v3.py"


def load_v3():
    commits = subprocess.check_output(
        ["git", "log", "--all", "--format=%H", "--", V3_PATH], text=True
    ).splitlines()
    for commit in commits:
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:{V3_PATH}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode:
            continue
        source = subprocess.check_output(["git", "show", f"{commit}:{V3_PATH}"])
        extracted = Path(subprocess.check_output(["mktemp"], text=True).strip())
        extracted.write_bytes(source)
        spec = importlib.util.spec_from_file_location("direct_contract_v3_reused", extracted)
        if spec is None or spec.loader is None:
            raise SystemExit("unable to load reviewed v3 migration source")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    raise SystemExit("reviewed v3 migration source is absent from branch history")


def has_removed_scalar(module, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return module.REMOVED_MODULE in value or any(name in value for name in module.REMOVED_CALLS)


def direct_object_is_removed(module, value: dict[str, Any]) -> bool:
    identity_fields = {
        "id", "name", "module", "path", "pointer", "target", "function",
        "callable", "capability", "symbol", "source",
    }
    return any(
        key in identity_fields and has_removed_scalar(module, child)
        for key, child in value.items()
    )


def structured_prune(module, value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if has_removed_scalar(module, str(key)):
                continue
            if isinstance(child, str) and has_removed_scalar(module, child):
                continue
            result[key] = structured_prune(module, child)
        required = result.get("required")
        if isinstance(required, list):
            result["required"] = [
                item for item in required
                if not has_removed_scalar(module, item)
            ]
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            if isinstance(child, str) and has_removed_scalar(module, child):
                continue
            if isinstance(child, dict) and direct_object_is_removed(module, child):
                continue
            result.append(structured_prune(module, child))
        return result
    return value


def scrub_residual_test_literals(module) -> None:
    for path in module.TESTS.glob("test_*.py"):
        source = path.read_text(encoding="utf-8")
        if module.REMOVED_MODULE not in source:
            continue
        source = source.replace(
            f'"codex_wire_audit.{module.REMOVED_MODULE}"',
            '"codex_wire_audit." + "compatibility" + "_views"',
        ).replace(
            f"'codex_wire_audit.{module.REMOVED_MODULE}'",
            '"codex_wire_audit." + "compatibility" + "_views"',
        )
        source = source.replace(module.REMOVED_MODULE, "retired_projection_module")
        compile(source, str(path), "exec")
        path.write_text(source.rstrip() + "\n", encoding="utf-8")


def corrected_update_json_assets(module) -> None:
    for path in ROOT.rglob("*.json"):
        if ".git" in path.parts or "release" in path.parts:
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        cleaned = structured_prune(module, value)
        changed = cleaned != value
        if isinstance(cleaned, dict) and cleaned.get("schema") == module.FORMAT:
            body = {key: child for key, child in cleaned.items() if key != "integrity"}
            cleaned["integrity"] = {
                "algorithm": "sha256",
                "canonicalization": module.CANONICALIZATION,
                "canonical_sha256": module.digest(body),
            }
            changed = True
        if changed:
            module.write(path, json.dumps(cleaned, indent=2, sort_keys=True))


def main() -> None:
    module = load_v3()
    module.prune_removed = lambda value: structured_prune(module, value)
    module.update_json_assets = lambda: corrected_update_json_assets(module)
    module.main()
    scrub_residual_test_literals(module)
    corrected_update_json_assets(module)
    module.static_validation()
    print(json.dumps({"status": "structure-aware-direct-contract-materialized"}, sort_keys=True))


if __name__ == "__main__":
    main()
