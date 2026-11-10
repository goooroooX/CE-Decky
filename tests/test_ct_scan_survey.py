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


_PARAMETER_TABLE = """<CheatTable CheatEngineTableVersion="45"><CheatEntries>
  <CheatEntry><ID>22</ID><Description>Activate</Description><VariableType>Auto Assembler Script</VariableType>
    <AssemblerScript>[ENABLE]
label(bJump)
label(fJump)
bJump:
db 0
fJump:
dd (float)2.0
hook:
cmp byte ptr [bJump],1
jne @f
mulss xmm0,[fJump]
@@:
jmp return
[DISABLE]
</AssemblerScript>
    <CheatEntries>
      <CheatEntry><ID>60</ID><Description>Jump Height Multiplier</Description><VariableType>Auto Assembler Script</VariableType>
        <AssemblerScript>[ENABLE]
bJump:
db 1
[DISABLE]
bJump:
db 0
</AssemblerScript>
        <CheatEntries><CheatEntry><ID>61</ID><Description>Multiplier</Description><VariableType>Float</VariableType><Address>fJump</Address></CheatEntry></CheatEntries>
      </CheatEntry>
      <CheatEntry><ID>62</ID><Description>Independent value</Description><VariableType>4 Bytes</VariableType><Address>game.exe+100</Address></CheatEntry>
    </CheatEntries>
  </CheatEntry>
</CheatEntries></CheatTable>
"""


def _parameters(tmp_path: Path) -> Path:
    path = tmp_path / "parameters.CT"
    path.write_text(_PARAMETER_TABLE)
    return path


def test_parameter_survey_reports_legacy_startup_without_classifying_controls(tmp_path: Path):
    answer = survey.parameters_one(_parameters(tmp_path))
    row, = answer["candidates"]
    assert row["direct_reads"] == ["mulss xmm0,[fjump]"]
    assert row["startup"]["record_off"]["configured_value_applied"] is True
    assert [61, "active", "1"] not in row["startup"]["record_off"]["actions"]
    assert [61, "active", "1"] in row["startup"]["record_on"]["actions"]


def test_parameter_survey_does_not_treat_a_pointer_base_as_the_scalar(tmp_path: Path):
    path = _parameters(tmp_path)
    path.write_text(path.read_text().replace("<Address>fJump</Address>", "<Address>fJump</Address><Offsets><Offset>4</Offset></Offsets>"))
    row, = survey.parameters_one(path)["candidates"]
    assert row["pointer_offsets"] is True
    assert row["direct_reads"] == [] and "startup" not in row


def test_parameter_corpus_uses_the_first_repeated_field_like_the_inspector(tmp_path: Path):
    path = _parameters(tmp_path)
    path.write_text(path.read_text().replace("<ID>61</ID>", "<ID>61</ID><ID>999</ID>")
                    .replace("<Address>fJump</Address>", "<Address>fJump</Address><Address>other</Address>"))
    answer = survey.parameters_corpus([tmp_path])
    assert answer["tables"] == 1 and answer["unreadable"] == []
    row, = answer["results"][0]["candidates"]
    assert row["record_id"] == 61 and row["address"] == "fjump"
    assert row["direct_reads"] == ["mulss xmm0,[fjump]"]
    assert row["startup"]["record_off"]["configured_value_applied"] is True


def test_parameter_corpus_deduplicates_bytes_and_reports_parse_failures(tmp_path: Path):
    path = _parameters(tmp_path)
    (tmp_path / "copy.ct").write_bytes(path.read_bytes())
    (tmp_path / "broken.CT").write_text("<CheatTable>")
    answer = survey.parameters_corpus([tmp_path])
    assert answer["tables"] == 1 and answer["duplicate_copies"] == 1
    assert len(answer["unreadable"]) == 1
    assert answer["script_owned_numeric_candidates"] == answer["with_direct_reads"] == 1


def test_parameter_cli_uses_the_install_authoritys_home(tmp_path: Path, monkeypatch, capsys):
    import json
    import sys
    from types import SimpleNamespace

    home = tmp_path / "steam-user"
    blobs = home / ".cheat-engine-decky" / "tables" / "sha256"
    blobs.mkdir(parents=True)
    _parameters(blobs)
    monkeypatch.setitem(sys.modules, "target_plugin_install", SimpleNamespace(resolve_install_authority=lambda: {"user_home": str(home)}))
    monkeypatch.setattr(sys, "argv", ["ct_scan_survey.py", "--parameters", "--include-installed", "--json"])
    assert survey.main() == 0
    report = json.loads(capsys.readouterr().out)["parameters_corpus"]
    assert report["roots"] == [str(blobs)] and report["tables"] == 1


def test_startup_plan_survey_asks_the_planner_about_every_shape(tmp_path: Path):
    path = _parameters(tmp_path)
    plans = survey.startup_plans_one(path)["plans"]
    assert set(plans) == set(survey.STARTUP_SHAPES)
    # A read-back with no switch state is not replayed; the typed value is.
    assert [61, "value", "1"] not in plans["read_back"]
    assert [61, "value", "2"] in plans["read_back_typed"]
    assert [61, "value", "1"] not in plans["read_back_typed"]
    assert [61, "active", "1"] in plans["all_on"]
    corpus = survey.startup_plans_corpus([tmp_path])
    assert corpus["tables"] == 1 and set(corpus["shapes"]) == set(survey.STARTUP_SHAPES)


def _plans(plans: dict) -> dict:
    return {"sha256": "ab" * 32, "controls": 3, "plans": {shape: plans.get(shape, []) for shape in survey.STARTUP_SHAPES}}


def test_two_revisions_plans_are_compared_shape_by_shape():
    before = {"results": [_plans({"read_back": [[61, "value", "1"], [22, "active", "1"]]}),
                          {**_plans({}), "sha256": "cd" * 32}]}
    after = {"results": [_plans({"read_back": [[22, "active", "1"]], "all_on": {"error": "past the limit"}}),
                         {**_plans({}), "sha256": "ef" * 32}]}
    diff = survey.compare_startup_plans(before, after)
    assert diff["tables"] == 1
    assert diff["only_baseline"] == ["cdcdcdcd"] and diff["only_candidate"] == ["efefefef"]
    assert diff["shapes"]["read_back"]["removed"] == {"value": 1} and diff["shapes"]["read_back"]["added"] == {}
    assert diff["shapes"]["all_on"]["refused"] == [0, 1] and diff["shapes"]["all_on"]["moved"] == 1
    assert diff["shapes"]["remembered_off"] == {"same": 1, "moved": 0, "removed": {}, "added": {}, "refused": [0, 0], "examples": []}


def test_a_corpus_pass_says_how_far_it_is_on_stderr_only(tmp_path: Path, capsys):
    _parameters(tmp_path)
    answer = survey.startup_plans_corpus([tmp_path], progress=True)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "1 read" in captured.err and answer["tables"] == 1


def test_a_baseline_compares_a_corpus_and_never_one_table(monkeypatch, capsys):
    import sys

    import pytest

    monkeypatch.setattr(sys, "argv", ["ct_scan_survey.py", "--table", "one.CT", "--flags", "--baseline", "HEAD"])
    with pytest.raises(SystemExit):
        survey.main()
    assert "--baseline compares a corpus" in capsys.readouterr().err


def test_two_revisions_answers_are_compared_by_what_they_say_about_the_corpus():
    row = {"table": "a.CT", "sha256": "ab" * 32, "controls": 3, "lists": 2, "read_only": 0}
    before = {"lists": 2, "read_only": 0, "utc": "then", "host": "here", "source_sha256": {"x": "1"},
              "results": [row, {**row, "sha256": "cd" * 32}]}
    after = {"lists": 2, "read_only": 1, "utc": "now", "host": "there", "source_sha256": {"x": "2"},
             "results": [{**row, "table": "/elsewhere/a.CT", "read_only": 1}, {**row, "sha256": "ef" * 32}]}
    diff = survey.compare_answers(before, after)
    # When and where each ran, and the path a row was found at, are not findings.
    assert diff["changed"] == {"read_only": {"baseline": 0, "candidate": 1}}
    assert diff["tables"] == {"same": 0, "moved": 1, "examples": ["abababab"],
                              "only_baseline": ["cdcdcdcd"], "only_candidate": ["efefefef"]}
    assert diff["same"] is False
    assert survey.compare_answers({"tables": 3, "utc": "a"}, {"tables": 3, "utc": "b"}) == {"same": True, "changed": {}}


def test_a_baseline_that_does_not_exist_is_a_sentence(tmp_path: Path):
    import pytest

    with pytest.raises(RuntimeError, match="cannot check out no-such-revision"):
        survey.startup_plans_against("no-such-revision", [tmp_path], tables=tmp_path, include_installed=False)


def test_dropdowns_counts_what_configure_cheats_draws_for_each_list(tmp_path: Path):
    def listed(record_id: int, entries: str, options: str = "") -> str:
        return (f'<CheatEntry><ID>{record_id}</ID><Description>"L{record_id}"</Description>'
                f'<VariableType>Byte</VariableType><Address>game.exe+{record_id}</Address>'
                f'<DropDownList{options}>{entries}</DropDownList></CheatEntry>')
    marked = ' ReadOnly="1" DescriptionOnly="1" DisplayValueAsItem="1"'
    entries = (
        listed(1, "0:Disabled\n1:Enabled\n*:--", marked)
        + listed(2, "-1:Default\n0:Unlocked\n1:Locked", marked)
        + listed(3, "0:Pistol\n1:Rifle")
        + listed(4, "0:Off\n1:On")
    )
    (tmp_path / "lists.CT").write_text(
        f'<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries>{entries}</CheatEntries></CheatTable>'
    )
    (tmp_path / "copy.ct").write_bytes((tmp_path / "lists.CT").read_bytes())
    answer = survey.dropdowns_corpus([tmp_path])
    assert answer["tables"] == 1 and answer["duplicate_copies"] == 1
    assert (answer["lists"], answer["read_only"], answer["switches"], answer["read_only_switches"]) == (4, 2, 2, 1)
    assert answer["accept_a_typed_value"] == 1 and answer["tables_with_a_read_only_list"] == 1
