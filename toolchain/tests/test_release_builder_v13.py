from __future__ import annotations

import hashlib
from pathlib import Path
import zipfile

import pytest

from tools.build_v13_release import (
    _build_review_diff,
    _file_mode,
    _machine_release_asset,
    _recursive_release_asset,
    _safe_member_name,
    _verify_source_manifest,
    _verify_zip,
    _write_source_manifest,
    _zip_tree,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_release_zip_is_deterministic_and_permission_normalized(tmp_path: Path) -> None:
    first_tree = tmp_path / "first"
    second_tree = tmp_path / "second"
    first_tree.mkdir()
    second_tree.mkdir()
    for root in (first_tree, second_tree):
        (root / "data.json").write_text('{"ok":true}\n', encoding="utf-8")
        (root / "script.py").write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    (first_tree / "data.json").chmod(0o600)
    (second_tree / "data.json").chmod(0o777)
    (first_tree / "script.py").chmod(0o600)
    (second_tree / "script.py").chmod(0o777)

    first_zip = tmp_path / "first.zip"
    second_zip = tmp_path / "second.zip"
    _zip_tree(first_tree, first_zip, prefix="bundle")
    _zip_tree(second_tree, second_zip, prefix="bundle")

    assert first_zip.read_bytes() == second_zip.read_bytes()
    assert _verify_zip(first_zip)["members"] == 2
    with zipfile.ZipFile(first_zip) as archive:
        modes = {item.filename: (item.external_attr >> 16) & 0o777 for item in archive.infolist()}
    assert modes["bundle/data.json"] == 0o644
    assert modes["bundle/script.py"] == 0o755
    assert _file_mode(first_tree / "script.py") == 0o755


def test_bundle_manifest_is_closed_and_detects_tampering(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/b.json").write_text("{}\n", encoding="utf-8")
    manifest = _write_source_manifest(tmp_path)
    result = _verify_source_manifest(tmp_path)
    assert manifest["file_count"] == 2
    assert result == {"manifest_files": 2, "checksum_files": 3}

    (tmp_path / "a.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bundle manifest mismatch"):
        _verify_source_manifest(tmp_path)


def test_release_asset_recursion_filters_are_explicit() -> None:
    assert not _recursive_release_asset("codex_wire_audit_v13_ci_history_bundle.zip")
    assert not _recursive_release_asset("codex_wire_audit_v13_machine_history_assets.zip.sha256")
    assert _recursive_release_asset("codex_wire_audit-7.0.0.tar.gz")
    assert not _machine_release_asset("codex_wire_audit-7.0.0.tar.gz")
    assert not _machine_release_asset("codex_wire_audit_v12_to_v13_metadata_history.diff")
    assert _machine_release_asset("codex_wire_audit_v13_release.json")


def test_safe_member_name_rejects_traversal_and_windows_aliases() -> None:
    assert _safe_member_name("bundle/a.json")
    assert not _safe_member_name("../a.json")
    assert not _safe_member_name("bundle/../a.json")
    assert not _safe_member_name("C:/a.json")
    assert not _safe_member_name("bundle\\a.json")


def test_review_diff_is_path_stable(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "a.txt").write_text("old\n", encoding="utf-8")
    (after / "a.txt").write_text("new\n", encoding="utf-8")
    output = tmp_path / "review.diff"
    _build_review_diff(before, after, output)
    text = output.read_text(encoding="utf-8")
    assert "--- v12/a.txt" in text
    assert "+++ v13/a.txt" in text
    assert str(tmp_path) not in text
    assert _sha256(output) == _sha256(output)
