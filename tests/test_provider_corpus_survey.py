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
