from __future__ import annotations

import ast
import base64
import importlib.util
import os
from pathlib import Path
import textwrap
import zlib

base_path = Path(os.environ.get("BASE_PUBLISHER", "/tmp/publisher.py"))
spec = importlib.util.spec_from_file_location("canonical_machine_schema_publisher_base", base_path)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load base publisher: {base_path}")
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


def split_local_storage_v2() -> list[str]:
    marker = "CANONICAL_GUARDED_NAMESPACE_PARTS_V2"
    old_marker = "CANONICAL_NAMESPACE_PARTS_V1"
    path: Path = base.LOCAL_STORAGE
    package = path.parent
    source = path.read_text(encoding="utf-8")
    if marker in source:
        parts = sorted(package.glob("_local_storage_part_*.py"))
        if not parts:
            raise SystemExit("v2 local-storage wrapper exists without source parts")
        return [str(item.relative_to(base.ROOT)) for item in parts]
    if old_marker in source:
        parts = sorted(package.glob("local_storage_part_*.py"))
        if not parts:
            raise SystemExit("v1 local-storage wrapper exists without source parts")
        return [str(item.relative_to(base.ROOT)) for item in parts]

    tree = ast.parse(source, filename=str(path))
    if not tree.body:
        raise SystemExit("local_storage.py has no top-level statements")
    lines = source.splitlines(keepends=True)
    future_lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            future_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    max_plain_lines = 300
    spans: list[tuple[int, int, bool]] = []
    start = 1
    last_end = 0
    for node in tree.body:
        end = node.end_lineno or node.lineno
        if last_end and end - start + 1 > max_plain_lines:
            spans.append((start, last_end, False))
            start = last_end + 1
        if end - start + 1 > max_plain_lines:
            spans.append((start, end, True))
            start = end + 1
        last_end = end
    if start <= len(lines):
        spans.append((start, len(lines), len(lines) - start + 1 > max_plain_lines))

    for old in list(package.glob("local_storage_part_*.py")) + list(package.glob("_local_storage_part_*.py")):
        old.unlink()

    flag = "_CANONICAL_LOCAL_STORAGE_EXECUTION_V2"
    names: list[str] = []
    for index, (first, last, compressed) in enumerate(spans, start=1):
        raw = "".join(
            line for line_number, line in enumerate(lines[first - 1 : last], start=first)
            if line_number not in future_lines
        ).lstrip("\n")
        name = f"_local_storage_part_{index:02d}.py"
        part_path = package / name
        if compressed:
            encoded = base64.b85encode(zlib.compress(raw.encode("utf-8"), level=9)).decode("ascii")
            chunks = "\n".join(f"        b{chunk!r}" for chunk in textwrap.wrap(encoded, 180))
            content = f'''\
from __future__ import annotations

# Lossless execution capsule for one oversized top-level source statement.
# Imported directly, this module is intentionally inert.
if globals().get({flag!r}):
    import base64 as _canonical_b64
    import zlib as _canonical_zlib
    _canonical_source = _canonical_zlib.decompress(_canonical_b64.b85decode(
{chunks}
    ))
    exec(compile(_canonical_source, {str(part_path)!r}, "exec"), globals(), globals())
    del _canonical_source, _canonical_b64, _canonical_zlib
'''
        else:
            guarded = textwrap.indent(raw, "    ") if raw else "    pass\n"
            content = f'''\
from __future__ import annotations

# Ordered implementation fragment for local_storage.py.
# Imported directly, this module is intentionally inert.
if globals().get({flag!r}):
{guarded}
'''
        base.write_text(part_path, content)
        line_count = len(part_path.read_text(encoding="utf-8").splitlines())
        if line_count > 390:
            raise SystemExit(f"generated source part exceeds 390 lines: {part_path} ({line_count})")
        names.append(name)

    wrapper = f'''\
"""Canonical Codex local thread-storage and rollout-layout extraction.

Implementation statements are split at Python top-level boundaries and executed
in this module's namespace. Guarded source parts are inert when imported on
their own, preserving extractor discovery behavior and the established API.
"""

from __future__ import annotations

from pathlib import Path as _CanonicalPath

{flag} = True
_CANONICAL_GUARDED_NAMESPACE_PARTS_V2 = {tuple(names)!r}
for _canonical_part_name in _CANONICAL_GUARDED_NAMESPACE_PARTS_V2:
    _canonical_part_path = _CanonicalPath(__file__).with_name(_canonical_part_name)
    exec(
        compile(_canonical_part_path.read_bytes(), str(_canonical_part_path), "exec"),
        globals(),
        globals(),
    )

del {flag}, _canonical_part_name, _canonical_part_path, _CanonicalPath
'''
    base.write_text(path, wrapper)
    return [str((package / name).relative_to(base.ROOT)) for name in names]


base.split_local_storage = split_local_storage_v2
base.main()
