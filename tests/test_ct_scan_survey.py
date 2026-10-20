from pathlib import Path

from scripts import ct_scan_survey as survey


def _entry(record_id: int, name: str, kind: str, children: str = "") -> str:
    body = (
        "<VariableType>Auto Assembler Script</VariableType><AssemblerScript>[ENABLE]\n[DISABLE]\n</AssemblerScript>"
        if kind == "script" else "<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address>"
    )
    nested = f"<CheatEntries>{children}</CheatEntries>" if children else ""
    return f'<CheatEntry><ID>{record_id}</ID><Description>"{name}"</Description>{body}{nested}</CheatEntry>'


def _table(tmp_path: Path) -> Path:
    # One of each shape: machinery holding scripts, a cheat with its setting
    # under it, a section script holding a script, and a top-level cheat with
    # its own setting, which is what hiding only the top level would hide.
    entries = (
        _entry(1, "Activate", "script",
               _entry(2, "Infinite Health", "script", _entry(3, "Health %", "value"))
               + _entry(4, "Section", "script", _entry(5, "Sub", "script", _entry(6, "Amount", "value"))))
        + _entry(7, "Teleporter", "script", _entry(8, "X", "value"))
    )
    path = tmp_path / "shapes.CT"
    path.write_text(f'<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>{entries}</CheatEntries></CheatTable>')
    return path


def test_scripts_sorts_every_enclosing_record_into_one_shape(tmp_path: Path):
    answer = survey.scripts_one(_table(tmp_path))

    names = {shape: [row["name"] for row in rows] for shape, rows in answer["shapes"].items()}
    assert names == {
        "outermost_values_only": ["Teleporter"],
        "outermost_holds_scripts": ["Activate"],
        "nested_values_only": ["Activate > Infinite Health", "Activate > Section > Sub"],
        "nested_holds_scripts": ["Activate > Section"],
    }
    assert answer["enclosing"] == 5


def test_scripts_corpus_counts_what_each_rule_would_hide(tmp_path: Path):
    _table(tmp_path)
    (tmp_path / "notes.txt").write_text("not a table")

    answer = survey.scripts_corpus(tmp_path)

    assert answer["tables"] == 1
    assert answer["hidden_by_rule"] == {"everything": 5, "outermost": 2, "scripts_holding_scripts": 2}
    assert answer["enclosing_shown_by_rule"] == {"everything": 0, "outermost": 3, "scripts_holding_scripts": 3}
    assert answer["kinds"]["nested_values_only"] == {"script": 2}
