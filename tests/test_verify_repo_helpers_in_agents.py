"""A helper documented where nobody looks first is a helper nobody finds.

`docs/DEVELOPMENT.md` is the authority for what a helper guarantees, and this
project proved that being documented there is not the same as being findable:
eleven helpers were in that document, the rule enforcing it was green, and none
of them had a row in the table `AGENTS.md` sends an agent to before it writes a
command of its own. This is the rule that would have caught it, and these are
the ways it has to fail.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


def _tree(root: Path, *, helpers: dict[str, str], agents: str) -> None:
    for name, body in helpers.items():
        directory = root / ("tools" if name.startswith("tool_") else "scripts")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(body, encoding="utf-8")
    (root / "AGENTS.md").write_text(agents, encoding="utf-8")


# A faithful miniature of the two surfaces the rule reads: the tracked-helper
# table and the route table. Both have to be here, because a contract missing
# either is a failure of its own and the rule says so before it says anything
# about a helper.
_AGENTS = """# Contract

## Validation

| Surface | Minimum route |
|---|---|
| Ordinary change | `python scripts/qa.py` |

Prose after the table.

## Tracked helpers

| Question | Helper |
|---|---|
| What is on the device | `scripts/target_thing_probe.py` |

## Environment and Git

Nothing here.
"""


def test_the_repository_as_it_stands_is_findable_from_its_own_contract():
    verify_repo._verify_helpers_are_in_the_agent_table()


def test_a_helper_with_no_row_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _tree(tmp_path, helpers={"target_new_probe.py": '"""A probe."""\n'}, agents=_AGENTS)
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "HELPERS_OUTSIDE_THE_AGENT_TABLE", {})
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_helpers_are_in_the_agent_table()

    assert "target_new_probe.py" in str(refused.value)


def test_a_helper_under_tools_is_asked_the_same_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`tools/` had no enforcement at all, which is how its three went unlisted."""
    _tree(tmp_path, helpers={"tool_reader.py": '"""A reader."""\n'}, agents=_AGENTS)
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "HELPERS_OUTSIDE_THE_AGENT_TABLE", {})
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_helpers_are_in_the_agent_table()

    assert "tools/tool_reader.py" in str(refused.value)


def test_prose_elsewhere_in_the_contract_does_not_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The whole failure being fixed is a helper that is present and invisible."""
    agents = _AGENTS.replace(
        "Nothing here.", "Run `scripts/target_new_probe.py` when you feel like it."
    )
    _tree(tmp_path, helpers={"target_new_probe.py": '"""A probe."""\n'}, agents=agents)
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "HELPERS_OUTSIDE_THE_AGENT_TABLE", {})
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})

    with pytest.raises(SystemExit):
        verify_repo._verify_helpers_are_in_the_agent_table()


def test_a_row_in_the_table_satisfies_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    agents = _AGENTS.replace(
        "| What is on the device | `scripts/target_thing_probe.py` |",
        "| What is on the device | `scripts/target_new_probe.py` |",
    )
    _tree(tmp_path, helpers={"target_new_probe.py": '"""A probe."""\n'}, agents=agents)
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "HELPERS_OUTSIDE_THE_AGENT_TABLE", {})
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})

    verify_repo._verify_helpers_are_in_the_agent_table()


def test_a_recorded_reason_satisfies_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _tree(tmp_path, helpers={"target_new_probe.py": '"""A probe."""\n'}, agents=_AGENTS)
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})
    monkeypatch.setattr(
        verify_repo, "HELPERS_OUTSIDE_THE_AGENT_TABLE",
        {"target_new_probe.py": "reached through another documented helper"},
    )

    verify_repo._verify_helpers_are_in_the_agent_table()


def test_a_reason_left_behind_by_a_deleted_helper_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _tree(tmp_path, helpers={"target_thing_probe.py": '"""A probe."""\n'}, agents=_AGENTS)
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})
    monkeypatch.setattr(
        verify_repo, "HELPERS_OUTSIDE_THE_AGENT_TABLE", {"target_gone.py": "it was excused once"},
    )

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_helpers_are_in_the_agent_table()

    assert "target_gone.py" in str(refused.value)


def test_a_missing_table_is_refused_by_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A renamed section must fail loudly, not quietly excuse every helper."""
    _tree(tmp_path, helpers={"target_new_probe.py": '"""A probe."""\n'}, agents="# Contract\n\nNothing.\n")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "HELPERS_OUTSIDE_THE_AGENT_TABLE", {})
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_helpers_are_in_the_agent_table()

    assert "Tracked helpers" in str(refused.value)
