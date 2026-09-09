from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path
import subprocess

import pytest

from codex_wire_audit.extractors import create_extractors

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("target", ["check-legacy-assets", "check-history-assets"])
def test_asset_failure_cannot_be_hidden_by_cleanup(tmp_path, target):
    result = subprocess.run(
        ["make", target, "PYTHON=false"], cwd=ROOT,
        env={**os.environ, "TMPDIR": str(tmp_path)}, capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert not list(tmp_path.iterdir()), "temporary directories must still be cleaned"


def test_storage_helpers_are_normal_modules_without_dynamic_execution():
    before = [item.extractor_id for item in create_extractors()]
    for name in ("specs", "sources", "layout", "lifecycle"):
        module = importlib.import_module(f"codex_wire_audit.extractors.local_storage_{name}")
        source = Path(module.__file__).read_text()
        assert len(source.splitlines()) <= 250
        calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
        assert not any(isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval", "compile"} for node in calls)
    assert before == [item.extractor_id for item in create_extractors()]
