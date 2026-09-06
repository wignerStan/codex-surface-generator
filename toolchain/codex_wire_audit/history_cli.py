"""Command-line interface for versioned Codex metadata-key history."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .metadata_history import (
    MetadataHistoryError,
    catalog_payload_sha256,
    diff_versions,
    history_key_view,
    history_version_view,
    load_history_catalog,
    select_version,
    validate_history_catalog,
)
from .metadata_history_container import (
    build_container_zip,
    verify_multi_json_container,
    write_multi_json_container,
)
from .metadata_history_probe import probe_metadata_history

QUERY_FORMAT = "codex-wire-audit-metadata-history-query/v1"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", dir=path.parent, delete=False
    ) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _summary(catalog: dict[str, Any]) -> dict[str, Any]:
    keys = catalog.get("keys", {})
    versions = catalog.get("versions", {})
    current = versions.get(catalog.get("current_version_ref"), {})
    return {
        "format": QUERY_FORMAT,
        "catalog_format": catalog.get("format"),
        "catalog_version": catalog.get("catalog_version"),
        "catalog_sha256": catalog_payload_sha256(catalog),
        "repository": catalog.get("repository"),
        "reviewed_through": catalog.get("reviewed_through"),
        "current_version_ref": catalog.get("current_version_ref"),
        "current_commit_sha": current.get("commit_sha"),
        "key_count": len(keys),
        "version_count": len(versions),
        "keys": [
            {
                "id": key_id,
                "wire_name": item.get("wire_name"),
                "kind": item.get("kind"),
                "current_state_ref": item.get("current_state_ref"),
            }
            for key_id, item in sorted(keys.items())
            if isinstance(item, dict)
        ],
    }


def _self_test() -> int:
    catalog = load_history_catalog()
    diagnostics = validate_history_catalog(catalog)
    assert not diagnostics, [item.to_dict() for item in diagnostics]
    assert len(catalog["versions"]) >= 10
    assert len(catalog["keys"]) >= 7
    key = history_key_view(catalog, "code_mode_tool_names")
    assert key["key"]["current_state_ref"] == "state.code_mode.removed_reserved"
    introduced, _ = select_version(
        catalog,
        commit_sha="25b6fc9bbc49bbec12e8d38ceee550fc07cbc60d",
    )
    removed, _ = select_version(
        catalog,
        commit_sha="8e4b10446eed7bafb39d8a469f9be25a41f4864f",
    )
    diff = diff_versions(catalog, introduced["id"], removed["id"])
    assert any(
        item["key_ref"] == "key.turn_metadata.code_mode_tool_names"
        for item in diff["changes"]
    )
    packaged_container = Path(__file__).resolve().parent / "metadata_history_container_data"
    assert packaged_container.is_dir()
    assert not verify_multi_json_container(packaged_container)
    with tempfile.TemporaryDirectory() as directory:
        first = Path(directory) / "one"
        second = Path(directory) / "two"
        write_multi_json_container(catalog, first)
        write_multi_json_container(catalog, second)
        assert not verify_multi_json_container(first)
        assert not verify_multi_json_container(second)
        left = {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()}
        right = {p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()}
        assert left == right
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Query, verify, split, and source-probe the versioned Codex metadata-key history catalog."
        )
    )
    parser.add_argument("--catalog", type=Path, help="alternate history catalog JSON")
    parser.add_argument("--schema", type=Path, help="alternate history schema JSON")
    parser.add_argument("--validate", action="store_true", help="validate the catalog and selected outputs")
    parser.add_argument(
        "--verify-container",
        type=Path,
        help="verify an existing deterministic multi-JSON container",
    )
    parser.add_argument("--list-keys", action="store_true", help="emit a compact key/version index")
    parser.add_argument("--key", help="emit one key timeline by stable ID or unambiguous wire name")
    parser.add_argument("--version", help="select an exact history version ID")
    parser.add_argument("--commit", help="select an exact history commit; unknown commits use the current-state canary")
    parser.add_argument("--diff", nargs=2, metavar=("BASE_VERSION", "CANDIDATE_VERSION"))
    parser.add_argument("--source-root", type=Path, help="Codex checkout to verify against the selected history version")
    parser.add_argument("--source-mode", choices=("worktree", "commit"), default="worktree")
    parser.add_argument("--ref", default="HEAD", help="Git ref used to resolve source bytes")
    parser.add_argument("--output", type=Path, help="write the primary JSON result atomically")
    parser.add_argument("--output-dir", type=Path, help="write the deterministic multi-JSON container")
    parser.add_argument("--zip", dest="zip_path", type=Path, help="write a deterministic ZIP of the multi-JSON container")
    parser.add_argument("--source-date-epoch", type=int, default=315532800)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--debug", action="store_true")
    return parser


def _main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.self_test:
        return _self_test()
    if args.verify_container is not None:
        diagnostics = verify_multi_json_container(
            args.verify_container, schema_path=args.schema
        )
        result = {
            "format": QUERY_FORMAT,
            "operation": "verify_container",
            "container_root": str(args.verify_container.resolve()),
            "status": "complete" if not diagnostics else "invalid",
            "complete": not diagnostics,
            "diagnostics": [item.to_dict() for item in diagnostics],
        }
        if args.output is not None:
            _atomic_json(args.output, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
        return 0 if not diagnostics else 3

    catalog = load_history_catalog(args.catalog)
    diagnostics = validate_history_catalog(catalog, args.schema)
    if diagnostics:
        result = {
            "format": QUERY_FORMAT,
            "status": "invalid",
            "complete": False,
            "diagnostics": [item.to_dict() for item in diagnostics],
        }
        if args.output is not None:
            _atomic_json(args.output, result)
        else:
            print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
        return 3

    if args.diff:
        result: dict[str, Any] = diff_versions(catalog, args.diff[0], args.diff[1])
    elif args.key:
        result = history_key_view(catalog, args.key)
    elif args.version or args.commit:
        version, selection_basis = select_version(
            catalog, version_id=args.version, commit_sha=args.commit
        )
        result = history_version_view(catalog, version)
        result["selection_basis"] = selection_basis
    else:
        result = _summary(catalog) if args.list_keys else dict(catalog)

    if args.source_root is not None:
        probe = probe_metadata_history(
            catalog,
            args.source_root,
            version_id=args.version,
            commit_sha=args.commit,
            requested_ref=args.ref,
            source_mode=args.source_mode,
        ).to_dict()
        result = {
            "format": QUERY_FORMAT,
            "catalog_sha256": catalog_payload_sha256(catalog),
            "query": result,
            "probe": probe,
            "status": "complete" if probe["complete"] else "incomplete",
            "complete": probe["complete"],
        }

    container_dir = args.output_dir
    temporary_container: tempfile.TemporaryDirectory[str] | None = None
    try:
        if args.zip_path is not None and container_dir is None:
            temporary_container = tempfile.TemporaryDirectory(prefix="codex-wire-history-")
            container_dir = Path(temporary_container.name) / "metadata-history"
        if container_dir is not None:
            index = write_multi_json_container(catalog, container_dir)
            container_diagnostics = verify_multi_json_container(
                container_dir, schema_path=args.schema
            )
            if container_diagnostics:
                raise MetadataHistoryError(
                    "generated multi-JSON container failed verification: "
                    + "; ".join(item.code for item in container_diagnostics)
                )
            if args.zip_path is not None:
                build_container_zip(
                    container_dir,
                    args.zip_path,
                    source_date_epoch=args.source_date_epoch,
                )
                result = dict(result)
                result["container"] = {
                    "index": index,
                    "zip_path": str(args.zip_path.resolve()),
                    "zip_sha256": hashlib.sha256(args.zip_path.read_bytes()).hexdigest(),
                }
    finally:
        if temporary_container is not None:
            temporary_container.cleanup()

    if args.output is not None:
        _atomic_json(args.output, result)
    else:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))

    if isinstance(result.get("complete"), bool) and not result["complete"]:
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    try:
        return _main(arguments)
    except (OSError, ValueError, MetadataHistoryError, json.JSONDecodeError) as error:
        if "--debug" in arguments:
            raise
        print(f"codex-wire-audit-history: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
