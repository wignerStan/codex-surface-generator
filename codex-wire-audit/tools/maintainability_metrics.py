#!/usr/bin/env python3
"""Generate deterministic architecture metrics for active package and release tooling."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
V10_CORE = (
    "_codex_wire_audit_base_v10.py",
    "codex_wire_audit_v10.py",
    "_codex_wire_contract_v10.py",
)


@dataclass(frozen=True, slots=True)
class FunctionMetric:
    file: str
    qualified_name: str
    start_line: int
    line_count: int
    cyclomatic_complexity: int

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "qualified_name": self.qualified_name,
            "start_line": self.start_line,
            "line_count": self.line_count,
            "cyclomatic_complexity": self.cyclomatic_complexity,
        }


class ComplexityVisitor(ast.NodeVisitor):
    """Small deterministic McCabe-style complexity counter."""

    def __init__(self) -> None:
        self.value = 1

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        self.value += 1
        self.generic_visit(node)

    def visit_For(self, node: ast.For | ast.AsyncFor) -> None:  # noqa: N802
        self.value += 1
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        self.value += 1
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        self.value += len(node.handlers)
        self.value += int(bool(node.orelse)) + int(bool(node.finalbody))
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:  # noqa: N802
        self.value += max(0, len(node.values) - 1)
        self.generic_visit(node)

    def visit_IfExp(self, node: ast.IfExp) -> None:  # noqa: N802
        self.value += 1
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.value += 1 + len(node.ifs)
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:  # noqa: N802
        self.value += max(0, len(node.cases) - 1)
        self.generic_visit(node)


def _cyclomatic_complexity(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    visitor = ComplexityVisitor()
    for statement in node.body:
        visitor.visit(statement)
    return visitor.value


class FunctionVisitor(ast.NodeVisitor):
    def __init__(self, file: str) -> None:
        self.file = file
        self.stack: list[str] = []
        self.functions: list[FunctionMetric] = []

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        end = node.end_lineno or node.lineno
        name = ".".join((*self.stack, node.name))
        complexity = _cyclomatic_complexity(node)
        self.functions.append(
            FunctionMetric(
                self.file,
                name,
                node.lineno,
                end - node.lineno + 1,
                complexity,
            )
        )
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()


def _source_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(ROOT).as_posix()):
        relative = path.relative_to(ROOT).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _module_name(path: Path) -> tuple[str, bool]:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _resolve_import_from(
    current_module: str,
    is_package: bool,
    level: int,
    imported_module: str | None,
) -> str:
    if level == 0:
        return imported_module or ""
    package = current_module.split(".") if is_package else current_module.split(".")[:-1]
    trim = max(0, level - 1)
    base = package[: len(package) - trim] if trim else package
    if imported_module:
        base.extend(imported_module.split("."))
    return ".".join(base)


def _import_graph(paths: list[Path]) -> dict[str, set[str]]:
    module_meta = {_module_name(path)[0]: (path, _module_name(path)[1]) for path in paths}
    modules = set(module_meta)
    graph: dict[str, set[str]] = {module: set() for module in modules}
    for module, (path, is_package) in module_meta.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.name
                    while target and target not in modules and "." in target:
                        target = target.rsplit(".", 1)[0]
                    if target in modules and target != module:
                        graph[module].add(target)
            elif isinstance(node, ast.ImportFrom):
                base = _resolve_import_from(module, is_package, node.level, node.module)
                candidates = [base]
                candidates.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
                for candidate in candidates:
                    target = candidate
                    while target and target not in modules and "." in target:
                        target = target.rsplit(".", 1)[0]
                    if target in modules and target != module:
                        graph[module].add(target)
    return graph


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])
        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(result)


def _metrics(paths: list[Path], *, include_import_graph: bool) -> dict[str, object]:
    files: list[dict[str, object]] = []
    functions: list[FunctionMetric] = []
    for path in sorted(paths):
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=relative)
        visitor = FunctionVisitor(relative)
        visitor.visit(tree)
        functions.extend(visitor.functions)
        files.append({"file": relative, "line_count": len(text.splitlines())})
    largest_files = sorted(files, key=lambda item: (-int(item["line_count"]), str(item["file"])))
    largest_functions = sorted(
        functions,
        key=lambda item: (-item.line_count, item.file, item.qualified_name, item.start_line),
    )
    most_complex = sorted(
        functions,
        key=lambda item: (
            -item.cyclomatic_complexity,
            item.file,
            item.qualified_name,
            item.start_line,
        ),
    )
    graph = _import_graph(paths) if include_import_graph else {}
    cycles = _strongly_connected_components(graph) if graph else []
    return {
        "source_sha256": _source_digest(paths),
        "module_count": len(paths),
        "physical_line_count": sum(int(item["line_count"]) for item in files),
        "function_count": len(functions),
        "largest_module": largest_files[0] if largest_files else None,
        "largest_function": largest_functions[0].to_dict() if largest_functions else None,
        "modules_over_600_lines": [item for item in largest_files if int(item["line_count"]) > 600],
        "functions_over_200_lines": [item.to_dict() for item in largest_functions if item.line_count > 200],
        "largest_modules": largest_files[:10],
        "largest_functions": [item.to_dict() for item in largest_functions[:10]],
        "most_complex_function": most_complex[0].to_dict() if most_complex else None,
        "functions_over_complexity_45": [
            item.to_dict() for item in most_complex if item.cyclomatic_complexity > 45
        ],
        "largest_functions_by_complexity": [item.to_dict() for item in most_complex[:10]],
        "internal_import_edge_count": sum(len(targets) for targets in graph.values()),
        "maximum_module_fan_out": max((len(targets) for targets in graph.values()), default=0),
        "modules_over_fan_out_10": sorted(
            module for module, targets in graph.items() if len(targets) > 10
        ),
        "internal_import_cycles": cycles,
    }


def _test_count(paths: Iterable[Path]) -> int:
    count = 0
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
        )
    return count




def _release_identity() -> tuple[str, str]:
    spec = json.loads((ROOT / "codex_wire_audit/release_spec.v1.json").read_text(encoding="utf-8"))
    return str(spec["release"]["series"]), str(spec["release"]["package_version"])


def _output_paths() -> tuple[Path, Path]:
    series, _ = _release_identity()
    upper = series.upper()
    return (
        ROOT / f"codex_wire_audit_{series}_maintainability_metrics.json",
        ROOT / f"CODEX_WIRE_AUDIT_{upper}_METRICS.md",
    )


def build_metrics() -> dict[str, object]:
    series, package_version = _release_identity()
    v10_paths = [ROOT / name for name in V10_CORE]
    package_paths = sorted((ROOT / "codex_wire_audit").rglob("*.py"))
    release_tool_paths = sorted(
        {
            *list((ROOT / "tools").glob("release_*.py")),
            ROOT / "tools/build_distribution.py",
            ROOT / "tools/maintainability_metrics.py",
        }
    )
    active_paths = sorted({*package_paths, *release_tool_paths})
    v10 = _metrics(v10_paths, include_import_graph=False)
    package = _metrics(package_paths, include_import_graph=True)
    release_tools = _metrics(release_tool_paths, include_import_graph=True)
    active = _metrics(active_paths, include_import_graph=True)

    v10_largest_module = int(v10["largest_module"]["line_count"])  # type: ignore[index]
    active_largest_module = int(active["largest_module"]["line_count"])  # type: ignore[index]
    v10_largest_function = int(v10["largest_function"]["line_count"])  # type: ignore[index]
    active_largest_function = int(active["largest_function"]["line_count"])  # type: ignore[index]
    source_registry = json.loads(
        (ROOT / "codex_wire_audit_v11_examples/source-registry.default.json").read_text(encoding="utf-8")
    )
    registry_source_map = source_registry.get("sources", {})
    registry_sources = (
        list(registry_source_map.values())
        if isinstance(registry_source_map, dict)
        else list(registry_source_map)
    )
    schema_templates = sorted((ROOT / "codex_wire_audit/schema_templates").glob("*.schema.json"))
    proof_schema_templates = sorted(
        (ROOT / "codex_wire_audit/proof_schema_templates").glob("*.schema.json")
    )
    standalone_schemas = [ROOT / "codex_wire_audit/release_spec.schema.json"]
    history_catalog = json.loads(
        (ROOT / "codex_wire_audit/metadata_history_data/metadata-key-history.v1.json")
        .read_text(encoding="utf-8")
    )
    test_paths = [
        ROOT / "test_codex_wire_audit_v10.py",
        ROOT / "test_codex_wire_audit_v11.py",
        *sorted((ROOT / "tests").glob("test_*.py")),
    ]
    passes = {
        "module_size": not active["modules_over_600_lines"],
        "function_size": not active["functions_over_200_lines"],
        "cyclomatic_complexity": not active["functions_over_complexity_45"],
        "module_fan_out": not active["modules_over_fan_out_10"],
        "import_cycles": not active["internal_import_cycles"],
    }
    return {
        "metrics_schema_version": "3.0.0",
        "release": {"series": series, "package_version": package_version},
        "scope": {
            "v10": "three frozen generator modules used as structural baseline",
            "package": "active installable codex_wire_audit package",
            "release_tools": "active release_*.py tooling plus distribution builder and this metrics gate",
            "active": "package and release tooling together; this is the enforced ownership boundary",
        },
        "v10_structural_baseline": v10,
        "active_package": package,
        "active_release_tools": release_tools,
        "active_implementation": active,
        "deltas": {
            "largest_module_line_reduction_percent": round(
                (1 - active_largest_module / v10_largest_module) * 100, 1
            ),
            "largest_function_line_reduction_percent": round(
                (1 - active_largest_function / v10_largest_function) * 100, 1
            ),
            "v10_modules_over_600": len(v10["modules_over_600_lines"]),
            "active_modules_over_600": len(active["modules_over_600_lines"]),
            "v10_functions_over_200": len(v10["functions_over_200_lines"]),
            "active_functions_over_200": len(active["functions_over_200_lines"]),
        },
        "maintainability_assets": {
            "registered_source_specs": len(registry_sources),
            "registered_required_sources": sum(bool(item.get("required")) for item in registry_sources),
            "registered_optional_sources": sum(not bool(item.get("required")) for item in registry_sources),
            "strict_schema_templates": len(schema_templates)
            + len(proof_schema_templates)
            + len(standalone_schemas),
            "evolution_schema_templates": len(schema_templates),
            "proof_schema_templates": len(proof_schema_templates),
            "standalone_release_schemas": len(standalone_schemas),
            "metadata_history_keys": len(history_catalog.get("keys", {})),
            "metadata_history_states": len(history_catalog.get("states", {})),
            "metadata_history_versions": len(history_catalog.get("versions", {})),
            "metadata_history_transitions": len(history_catalog.get("transitions", [])),
            "regression_test_methods": _test_count(test_paths),
        },
        "guardrails": {
            "max_active_module_lines": 600,
            "max_active_function_lines": 200,
            "max_cyclomatic_complexity": 45,
            "max_module_fan_out": 10,
            "allow_internal_import_cycles": False,
            "passes": passes,
        },
    }


def render_markdown(metrics: dict[str, object]) -> str:
    release = metrics["release"]
    v10 = metrics["v10_structural_baseline"]
    package = metrics["active_package"]
    tools = metrics["active_release_tools"]
    active = metrics["active_implementation"]
    assets = metrics["maintainability_assets"]
    guardrails = metrics["guardrails"]
    assert all(isinstance(value, dict) for value in (release, v10, package, tools, active, assets, guardrails))
    passes = guardrails["passes"]
    return f"""# Codex wire audit {release['series']} maintainability metrics

The enforced scope now includes both the installable package and the release scripts. This closes the former blind spot where an oversized or highly coupled publisher could pass while package-only metrics remained green.

| Metric | Frozen v10 baseline | Active package | Active release tools | Combined enforced scope |
| --- | ---: | ---: | ---: | ---: |
| Python modules | {v10['module_count']} | {package['module_count']} | {tools['module_count']} | {active['module_count']} |
| Physical Python lines | {v10['physical_line_count']:,} | {package['physical_line_count']:,} | {tools['physical_line_count']:,} | {active['physical_line_count']:,} |
| Largest module | {v10['largest_module']['line_count']:,} | {package['largest_module']['line_count']:,} | {tools['largest_module']['line_count']:,} | {active['largest_module']['line_count']:,} |
| Largest function | {v10['largest_function']['line_count']:,} | {package['largest_function']['line_count']:,} | {tools['largest_function']['line_count']:,} | {active['largest_function']['line_count']:,} |
| Highest complexity | not measured | {package['most_complex_function']['cyclomatic_complexity']} | {tools['most_complex_function']['cyclomatic_complexity']} | {active['most_complex_function']['cyclomatic_complexity']} |
| Maximum module fan-out | not measured | {package['maximum_module_fan_out']} | {tools['maximum_module_fan_out']} | {active['maximum_module_fan_out']} |
| Internal import cycles | not measured | {len(package['internal_import_cycles'])} | {len(tools['internal_import_cycles'])} | {len(active['internal_import_cycles'])} |

Largest combined module: **`{active['largest_module']['file']}`** ({active['largest_module']['line_count']} lines). Largest combined function: **`{active['largest_function']['qualified_name']}`** ({active['largest_function']['line_count']} lines).

## Generated maintenance assets

- Source registry entries: **{assets['registered_source_specs']}** ({assets['registered_required_sources']} required, {assets['registered_optional_sources']} optional).
- Strict schemas: **{assets['strict_schema_templates']}**, including **{assets['standalone_release_schemas']}** release-spec schema.
- Metadata history: **{assets['metadata_history_keys']} keys**, **{assets['metadata_history_states']} states**, **{assets['metadata_history_versions']} versions**, **{assets['metadata_history_transitions']} transitions**.
- Regression test methods: **{assets['regression_test_methods']}**.
- Combined source digest: `{active['source_sha256']}`.

## Enforced budgets

- Module ≤ {guardrails['max_active_module_lines']} lines: **{str(passes['module_size']).lower()}**.
- Function ≤ {guardrails['max_active_function_lines']} lines: **{str(passes['function_size']).lower()}**.
- Complexity ≤ {guardrails['max_cyclomatic_complexity']}: **{str(passes['cyclomatic_complexity']).lower()}**.
- Module fan-out ≤ {guardrails['max_module_fan_out']}: **{str(passes['module_fan_out']).lower()}**.
- Internal import cycles forbidden: **{str(passes['import_cycles']).lower()}**.
"""


def _encoded(metrics: dict[str, object]) -> str:
    return json.dumps(metrics, ensure_ascii=True, indent=2, sort_keys=True) + "\n"


def write_outputs(metrics: dict[str, object]) -> None:
    json_output, markdown_output = _output_paths()
    json_output.write_text(_encoded(metrics), encoding="utf-8")
    markdown_output.write_text(render_markdown(metrics), encoding="utf-8")


def check_outputs(metrics: dict[str, object]) -> int:
    json_output, markdown_output = _output_paths()
    expected_json = _encoded(metrics)
    expected_markdown = render_markdown(metrics)
    problems: list[str] = []
    if not json_output.is_file() or json_output.read_text(encoding="utf-8") != expected_json:
        problems.append(str(json_output.relative_to(ROOT)))
    if not markdown_output.is_file() or markdown_output.read_text(encoding="utf-8") != expected_markdown:
        problems.append(str(markdown_output.relative_to(ROOT)))
    passes = metrics["guardrails"]["passes"]  # type: ignore[index]
    failed_budgets = [name for name, value in passes.items() if not value]
    if problems:
        print("stale generated metrics: " + ", ".join(problems))
    if failed_budgets:
        print("maintainability budget failures: " + ", ".join(failed_budgets))
    return 1 if problems or failed_budgets else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args()
    metrics = build_metrics()
    if args.check:
        return check_outputs(metrics)
    write_outputs(metrics)
    for output in _output_paths():
        print(output.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
