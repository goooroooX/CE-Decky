"""The corpus survey must sample and classify the way it says it does.

It is the only instrument this project has for "how often do real tables do
this", so a sampling bug would quietly answer a different question, and a
classifier that missed a section would answer the old one.
"""
from __future__ import annotations

from pathlib import Path

from scripts import provider_corpus_survey as survey


def _pages(count: int = 250) -> dict[int, list[tuple[str, str]]]:
    return {
        page * survey.TOPICS_PER_PAGE: [
            (f"{page}-{slot}", f"Game {page}-{slot}") for slot in range(survey.TOPICS_PER_PAGE)
        ]
        for page in range(count)
    }


def test_sampling_covers_every_stratum_and_repeats_for_one_seed():
    first = survey.sample_topics(_pages(), per_stratum=4, seed=7)
    again = survey.sample_topics(_pages(), per_stratum=4, seed=7)
    assert [item.topic_id for item in first] == [item.topic_id for item in again]
    assert {item.stratum for item in first} == {name for name, _, _ in survey.STRATA}
    assert len(first) == 4 * len(survey.STRATA)
    # A topic is never sampled twice, and each stratum draws from its own pages.
    assert len({item.topic_id for item in first}) == len(first)
    for item in first:
        page = int(item.topic_id.split("-")[0])
        low, high = next((low, high) for name, low, high in survey.STRATA if name == item.stratum)
        assert low <= page < high


def test_a_different_seed_asks_for_different_topics():
    assert (
        [item.topic_id for item in survey.sample_topics(_pages(), 6, 1)]
        != [item.topic_id for item in survey.sample_topics(_pages(), 6, 2)]
    )


def test_sampling_can_be_limited_to_named_strata():
    chosen = survey.sample_topics(_pages(), 5, 3, strata=("fresh", "recent"))
    assert {item.stratum for item in chosen} == {"fresh", "recent"} and len(chosen) == 10


def test_a_game_already_chosen_or_already_held_is_not_sampled_again():
    pages = {0: [("1", "Some Game"), ("2", "some  GAME"), ("3", "Held Game"), ("4", "Other Game")]}
    chosen = survey.sample_topics(pages, 10, 1, strata=("fresh",), known_titles=frozenset({"held game"}))
    assert sorted(item.title.casefold().split()[0] for item in chosen) == ["other", "some"]


def _table(tmp_path: Path, body: str, name: str = "t.CT") -> Path:
    from hashlib import sha256

    path = tmp_path / name
    data = f"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>
  </CheatEntries>
  {body}
</CheatTable>""".encode("utf-8")
    path.write_bytes(data)
    path.with_suffix(".sha").write_text(sha256(data).hexdigest())
    return path


def _digest(path: Path) -> str:
    return path.with_suffix(".sha").read_text()


def test_the_classifier_names_what_the_lua_actually_calls(tmp_path):
    path = _table(tmp_path, """<LuaScript>
    showMessage("hi")
    local f = createForm(true)
    openProcess("game.exe")
    sleep(100)
  </LuaScript>""")
    facts = survey.classify(path, _digest(path))
    assert facts["usable"] is True
    assert facts["has_lua"] is True
    assert set(facts["hazards"]) == {"asks", "window", "takes_session", "blocks"}


def test_the_classifier_reads_lua_that_auto_assembler_carries(tmp_path):
    # Auto Assembler can embed Lua, which is a second route to everything the
    # scan looks for and is invisible to one that only reads `<LuaScript>`.
    path = _table(tmp_path, """<CheatEntries><CheatEntry><ID>2</ID>
    <Description>"Script"</Description>
    <AssemblerScript>{$lua}
    showMessage("from AA")
    {$asm}
    </AssemblerScript></CheatEntry></CheatEntries>""")
    facts = survey.classify(path, _digest(path))
    assert facts["aa_embeds_lua"] is True
    assert "asks" in facts["hazards"]
    # And it is not reported as a `<LuaScript>`, because it is not one: Cheat
    # Engine runs it when a record is enabled, not when the table is opened.
    assert facts["has_lua"] is False


def test_the_classifier_reports_a_table_it_cannot_read_rather_than_raising(tmp_path):
    path = tmp_path / "broken.CT"
    path.write_bytes(b"<CheatTable><CheatEntries>")
    facts = survey.classify(path, "0" * 64)
    assert facts["usable"] is False
    assert "reason" in facts


def test_the_summary_reports_shares_and_reasons():
    text = survey.summarise([
        {"usable": True, "has_lua": True, "has_forms": False, "has_auto_assembler": True,
         "aa_embeds_lua": False, "embedded_files": 0, "hazards": ["asks"]},
        {"usable": True, "has_lua": False, "has_forms": True, "has_auto_assembler": False,
         "aa_embeds_lua": False, "embedded_files": 0, "hazards": []},
        {"skipped": "no attachment"},
    ])
    assert "2 usable table(s) of 3 fetched" in text
    assert "carries its own window" in text
    assert "no attachment" in text


def test_engine_marks_name_the_layout_a_table_reaches_its_targets_through():
    assert survey.engine_marks('local pool = getAddress("FNamePool")', "A game") == {
        "ue_fnamepool", "ue_object_graph", "unreal"}
    assert survey.engine_marks("[[[GWorld]+30]+38]", "A game") == {"ue_object_graph", "unreal"}
    assert survey.engine_marks("game.exe+10", "A game [Engine:Unreal 5.3]") == {"unreal"}
    assert survey.engine_marks('"Game-Win64-Shipping.exe"+10', "A game") == {"unreal"}
    assert survey.engine_marks("LaunchMonoDataCollector()", "A game") == {"unity_mono"}
    assert survey.engine_marks("GameAssembly.dll+10", "A game") == {"unity_il2cpp"}
    assert survey.engine_marks("game.exe+10", "A game [Engine:Unity]") == {"unity_title_only"}
    # A word that merely contains a marker is not one.
    assert survey.engine_marks("MyUObjectsList unrealistic", "A game") == set()


def test_the_engine_report_counts_each_distinct_stored_table_once_per_stratum(tmp_path):
    from ce_decky.table_store import TableStore

    store = TableStore(tmp_path / "store")
    held = [store.import_ct(str(_table(tmp_path, f"<LuaScript>{lua}</LuaScript>", f"{n}.CT")))
            for n, lua in enumerate(("GUObjectArray", "LaunchMonoDataCollector()", "print(1)"))]
    rows = [{"usable": True, "sha256": artifact.sha256, "stratum": stratum, "title": "A game"}
            for artifact, stratum in zip(held, ("fresh", "fresh", "recent"))]
    rows.append(dict(rows[0]))
    rows.append({"usable": True, "sha256": "f" * 64, "stratum": "recent", "title": "A game"})
    text = survey.summarise_engines(rows, store)
    assert "3 distinct usable table(s), 1 unreadable from the store" in text
    line = next(line for line in text.splitlines() if "names the Unreal object system" in line)
    assert "1/3" in line and "1/2" in line and "0/1" in line
    assert "upper bound" in text


def test_the_format_report_counts_declared_versions_and_signatures_per_stratum(tmp_path):
    from ce_decky.table_store import TableStore

    store = TableStore(tmp_path / "store")
    plain = store.import_ct(str(_table(tmp_path, "", "plain.CT")))
    signed = store.import_ct(str(_table(tmp_path, "<Signature>AAAA</Signature>", "signed.CT")))
    rows = [{"usable": True, "sha256": plain.sha256, "stratum": "fresh"},
            {"usable": True, "sha256": signed.sha256, "stratum": "recent"},
            {"usable": True, "sha256": plain.sha256, "stratum": "fresh"}]
    text = survey.summarise_formats(rows, store)
    assert "2 distinct usable table(s)" in text
    signed_line = next(line for line in text.splitlines() if line.strip().startswith("signed"))
    assert "1/2" in signed_line and "0/1" in signed_line and "1/1" in signed_line
    version_line = next(line for line in text.splitlines() if "CheatEngineTableVersion 45" in line)
    assert "2/2" in version_line


def test_a_retry_takes_only_topics_that_were_skipped_for_that_reason_alone():
    rows = [
        {"topic_id": "1", "stratum": "fresh", "title": "A", "skipped": "unsupported .rar"},
        {"topic_id": "2", "stratum": "recent", "title": "B", "skipped": "unsupported .rar"},
        {"topic_id": "2", "stratum": "recent", "title": "B", "usable": True, "sha256": "a" * 64},
        {"topic_id": "3", "stratum": "fresh", "title": "C", "skipped": "no attachment"},
    ]
    assert [(item.topic_id, item.stratum, item.title) for item in survey.retry_topics(rows, "unsupported .rar")] == [
        ("1", "fresh", "A")]


def test_a_retry_replaces_only_the_earlier_rows_of_the_topics_it_tried():
    import json

    lines = [json.dumps({"topic_id": topic, "n": n}) for n, topic in enumerate(("1", "2", "1"))] + ["not json"]
    lines.append(json.dumps({"topic_id": "1", "n": "new"}))
    kept = survey.drop_superseded(lines, held_lines=4, retried={"1"})
    assert [json.loads(line).get("n") if line != "not json" else line for line in kept] == [1, "not json", "new"]
