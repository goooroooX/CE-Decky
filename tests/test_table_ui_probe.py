from __future__ import annotations

import pytest

from scripts.table_ui_probe import CONTROL_PAGE_SIZE, inspect_path, summarize_inspection
from ce_decky.ct_inspector import TableControl, TableInspection
from ce_decky.table_store import MAX_CT_BYTES


def _control(record_id: int | None, kind: str, path: tuple[str, ...], dropdown=()):
    return TableControl(
        id=record_id,
        description=path[-1],
        path=path,
        variable_type="4 Bytes",
        kind=kind,
        group_header=kind == "group",
        has_assembler_script=kind == "script",
        dropdown_values=dropdown,
        dropdown_read_only=kind == "dropdown",
    )


def test_probe_models_complete_paged_controller_reachability():
    controls = [_control(1, "group", ("Root",))]
    controls.extend(_control(index + 2, "value", ("Root", f"Value {index}")) for index in range(CONTROL_PAGE_SIZE * 3 + 5))
    inspection = TableInspection(
        sha256="1" * 64,
        table_version="45",
        total_entries=len(controls),
        has_lua=False,
        has_auto_assembler=False,
        embedded_files=0,
        process_candidates=("game.exe",),
        controls=tuple(controls),
    )
    result = summarize_inspection("large-real-shape.CT", inspection)
    assert result["navigation_complete"] is True
    assert result["safe_actionable_controls"] == CONTROL_PAGE_SIZE * 3 + 5
    assert result["pages"] == 4
    assert result["max_path_depth"] == 2


def test_probe_excludes_ambiguous_and_unsupported_records_and_preserves_dropdown_cardinality():
    inspection = TableInspection(
        sha256="2" * 64,
        table_version="26",
        total_entries=5,
        has_lua=True,
        has_auto_assembler=True,
        embedded_files=0,
        process_candidates=(),
        controls=(
            _control(1, "script", ("Script",)),
            _control(2, "dropdown", ("Script", "Mode"), (("0", "Off"), ("1", "On"), ("2", "Hard"))),
            _control(7, "value", ("Duplicate A",)),
            _control(7, "value", ("Duplicate B",)),
            _control(None, "value", ("Unsupported",)),
        ),
        ambiguous_record_ids=(7,),
        unsupported_record_id_count=1,
    )
    result = summarize_inspection("dropdown.CT", inspection)
    assert result["safe_actionable_controls"] == 2
    assert result["dropdown_values"] == 3
    assert result["max_dropdown_values"] == 3
    assert result["ambiguous_record_ids"] == [7]
    assert result["navigation_complete"] is True


def test_probe_rejects_oversized_table_before_unbounded_read(tmp_path):
    path = tmp_path / "oversized.CT"
    with path.open("wb") as handle:
        handle.truncate(MAX_CT_BYTES + 1)
    with pytest.raises(ValueError, match="size"):
        inspect_path(path)


def test_probe_page_size_tracks_the_frontend_constant():
    """The probe measures controller reachability, so a drifted page size lies."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src" / "uiModel.ts").read_text(encoding="utf-8")
    match = re.search(r"export const CONTROL_PAGE_SIZE = (\d+);", source)
    assert match is not None
    assert int(match.group(1)) == CONTROL_PAGE_SIZE
