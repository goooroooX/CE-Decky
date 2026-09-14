#!/usr/bin/env python3
"""Static controller-UI reachability probe for real Cheat Engine tables.

The probe deliberately reuses CE Decky's production CT inspector and never executes
Lua, Auto Assembler, embedded files, or Cheat Engine itself. It models the normal
QAM control browser: group headers are presentation-only, malformed/ambiguous IDs
are excluded, every remaining control is paged, and dropdown values are not sliced.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

from ce_decky.atomic import read_regular_bytes  # noqa: E402
from ce_decky.ct_inspector import TableInspection, inspect_table  # noqa: E402
from ce_decky.table_store import MAX_CT_BYTES  # noqa: E402

# Keep in sync with `CONTROL_PAGE_SIZE` in src/uiModel.ts.
CONTROL_PAGE_SIZE = 6


def summarize_inspection(name: str, inspection: TableInspection) -> dict[str, object]:
    ambiguous = set(inspection.ambiguous_record_ids)
    safe = [
        control
        for control in inspection.controls
        if control.id is not None and control.kind != "group" and control.id not in ambiguous
    ]
    pages = [safe[offset : offset + CONTROL_PAGE_SIZE] for offset in range(0, len(safe), CONTROL_PAGE_SIZE)]
    reached = [control.id for page in pages for control in page]
    expected = [control.id for control in safe]
    kinds = {kind: sum(control.kind == kind for control in safe) for kind in ("script", "dropdown", "value")}
    dropdown_values = sum(len(control.dropdown_values) for control in safe if control.kind == "dropdown")
    max_dropdown_values = max((len(control.dropdown_values) for control in safe if control.kind == "dropdown"), default=0)
    max_depth = max((len(control.path) for control in inspection.controls), default=0)
    return {
        "name": name,
        "sha256": inspection.sha256,
        "table_version": inspection.table_version,
        "entries": inspection.total_entries,
        "inspected_controls": len(inspection.controls),
        "safe_actionable_controls": len(safe),
        "groups": sum(control.kind == "group" for control in inspection.controls),
        "scripts": kinds["script"],
        "dropdowns": kinds["dropdown"],
        "values": kinds["value"],
        "dropdown_values": dropdown_values,
        "max_dropdown_values": max_dropdown_values,
        "max_path_depth": max_depth,
        "process_candidates": list(inspection.process_candidates),
        "ambiguous_record_ids": list(inspection.ambiguous_record_ids),
        "unsupported_record_id_count": inspection.unsupported_record_id_count,
        "pages": max(1, len(pages)),
        "navigation_complete": reached == expected,
        "executable_content": {
            "lua": inspection.has_lua,
            "auto_assembler": inspection.has_auto_assembler,
            "embedded_files": inspection.embedded_files,
        },
    }


def inspect_path(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"table path must be a regular file: {path}")
    data = read_regular_bytes(path, max_bytes=MAX_CT_BYTES)
    assert data is not None
    digest = sha256(data).hexdigest()
    return summarize_inspection(path.name, inspect_table(path, digest))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tables", nargs="+", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output: list[dict[str, object]] = []
    failed = False
    for path in args.tables:
        try:
            output.append(inspect_path(path))
        except (OSError, UnicodeError, ValueError) as exc:
            failed = True
            output.append({"name": path.name, "error": str(exc), "navigation_complete": False})
    print(json.dumps({"schema": 1, "tables": output}, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
