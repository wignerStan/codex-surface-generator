"""Single compatibility boundary around the frozen v10 implementation.

Future extractors should not import v10 modules directly.  Keeping all dynamic
loading here makes the remaining migration surface explicit and searchable.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType


FROZEN_V10_SHA256 = {
    "_codex_wire_audit_base_v10.py": "56e4cdb13bf0470078f6040d345e98a48d88753c2ff03cb0ebdbfaf20b5f4156",
    "_codex_wire_contract_v10.py": "8caff1f00dc9f934440078c93e9f41feb745bd934337db26f37b908f4d0894b7",
    "codex_wire_audit_v10.py": "b7da74e31a902b4a2d8e73907857e95ff236c67fa08ab33ba70e49c1afbb6ab1",
    "test_codex_wire_audit_v10.py": "4fcf6307863e9ce9f0609d4471ecdd327e8d40f3a28b66dbb96c5f8965059c4f",
}


def verify_frozen_v10_integrity(
    bundle_root: str | Path | None = None, *, include_tests: bool = False
) -> tuple[str, ...]:
    """Return deterministic mismatch descriptions for the frozen v10 boundary."""
    root = Path(bundle_root).resolve() if bundle_root else Path(__file__).resolve().parent.parent
    names = tuple(FROZEN_V10_SHA256) if include_tests else tuple(FROZEN_V10_SHA256)[:3]
    mismatches: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file():
            mismatches.append(f"missing:{name}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = FROZEN_V10_SHA256[name]
        if actual != expected:
            mismatches.append(f"sha256:{name}:{expected}:{actual}")
    return tuple(mismatches)


@dataclass(frozen=True, slots=True)
class LegacyModules:
    base: ModuleType
    entrypoint: ModuleType
    contract: ModuleType


def _load(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load legacy module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CACHE: LegacyModules | None = None


def load_legacy_modules(bundle_root: str | Path | None = None) -> LegacyModules:
    global _CACHE
    if _CACHE is not None and bundle_root is None:
        return _CACHE
    root = Path(bundle_root).resolve() if bundle_root else Path(__file__).resolve().parent.parent
    mismatches = verify_frozen_v10_integrity(root)
    if mismatches:
        raise RuntimeError("frozen v10 compatibility boundary changed: " + "; ".join(mismatches))
    base = _load(root / "_codex_wire_audit_base_v10.py", "codex_wire_audit_legacy_base_v10")
    # The v10 entrypoint dynamically loads its own adjacent copies.  Load it only
    # after the files above have been proven present.
    entrypoint = _load(root / "codex_wire_audit_v10.py", "codex_wire_audit_legacy_entry_v10")
    contract = entrypoint.C
    result = LegacyModules(base=entrypoint.B, entrypoint=entrypoint, contract=contract)
    if bundle_root is None:
        _CACHE = result
    return result
