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
        # A script whose script holds nothing, and a script holding a value
        # that holds values: the first holds a script, the second does not.
        + _entry(9, "Hooks", "script", _entry(10, "Godmode", "script"))
        + _entry(11, "Stats", "script", _entry(12, "Player", "value", _entry(13, "HP", "value")))
    )
    path = tmp_path / "shapes.CT"
    path.write_text(f'<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>{entries}</CheatEntries></CheatTable>')
    return path


def test_scripts_sorts_every_enclosing_record_into_one_shape(tmp_path: Path):
    answer = survey.scripts_one(_table(tmp_path))

    names = {shape: [row["name"] for row in rows] for shape, rows in answer["shapes"].items()}
    assert names == {
        "outermost_values_only": ["Teleporter", "Stats"],
        "outermost_holds_scripts": ["Activate", "Hooks"],
        "nested_values_only": ["Activate > Infinite Health", "Activate > Section > Sub", "Stats > Player"],
        "nested_holds_scripts": ["Activate > Section"],
    }
    assert answer["enclosing"] == 8


def test_scripts_corpus_counts_what_each_rule_would_hide(tmp_path: Path):
    _table(tmp_path)
    (tmp_path / "notes.txt").write_text("not a table")

    answer = survey.scripts_corpus(tmp_path)

    assert answer["tables"] == 1
    assert answer["hidden_by_rule"] == {"everything": 8, "outermost": 4, "scripts_holding_scripts": 3}
    assert answer["enclosing_shown_by_rule"] == {"everything": 0, "outermost": 4, "scripts_holding_scripts": 5}
    assert answer["kinds"]["nested_values_only"] == {"script": 2, "value": 1}


def _stored(store: Path, content: bytes, digest: str | None = None) -> str:
    from hashlib import sha256

    digest = digest or sha256(content).hexdigest()
    blob = store / "sha256" / digest[:2] / digest / "table.CT"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(content)
    return digest


def test_a_stored_table_is_found_by_the_start_of_its_digest_and_verified(tmp_path: Path):
    store = tmp_path / "tables"
    digest = _stored(store, _table(tmp_path).read_bytes())

    found = survey.table_by_sha(digest[:10].upper(), store)

    assert found == store / "sha256" / digest[:2] / digest / "table.CT"
    assert survey.scripts_one(found)["enclosing"] == 8


def test_a_prefix_that_names_no_table_or_several_is_refused(tmp_path: Path):
    import pytest

    store = tmp_path / "tables"
    _stored(store, b"one", "ab" * 32)
    _stored(store, b"two", "ab" * 31 + "cd")
    with pytest.raises(ValueError, match="begins 2 stored tables"):
        survey.table_by_sha("abababab", store)
    with pytest.raises(ValueError, match="no stored table"):
        survey.table_by_sha("cdcdcdcd", store)
    with pytest.raises(ValueError, match="at least eight"):
        survey.table_by_sha("abab", store)
    # Bytes that are not the digest they are filed under are refused, not read.
    with pytest.raises(ValueError, match="SHA-256 verification"):
        survey.table_by_sha("ab" * 32, store)
