"""The helper that writes a changelog entry keeps the shape the contract wants.

`tests/test_changelog_shape.py` checks the file that is in the repository. This
checks the thing that writes into it, so the rules are met when the entry is
written rather than discovered at the next QA run.
"""
from __future__ import annotations

import pytest

from scripts.changelog import add_entry, set_date

CURRENT = [
    "# Changelog",
    "",
    "Versions are listed newest first.",
    "",
    "## 0.9.14 — 2026-09-04",
    "",
    "- [Fixed] The entry that was newest until now.",
    "- [New] Something older.",
    "",
    "## 0.9.13 — 2026-09-04",
    "",
    "- [Changed] Older still.",
]


def test_an_entry_goes_directly_above_the_one_it_replaces_at_the_top():
    # Directly above, with no blank line: a blank line between two entries makes
    # Markdown render the whole section as a loose list, which is invisible
    # until somebody reads the rendered page.
    updated = add_entry("[Security] The newest thing.", lines=list(CURRENT), version="0.9.14")

    assert updated[4:8] == [
        "## 0.9.14 — 2026-09-04",
        "",
        "- [Security] The newest thing.",
        "- [Fixed] The entry that was newest until now.",
    ]
    # The version below is untouched.
    assert updated[-3:] == ["## 0.9.13 — 2026-09-04", "", "- [Changed] Older still."]


def test_a_version_whose_entries_have_not_started_takes_the_first_one():
    lines = ["# Changelog", "", "## 0.9.14 — 2026-09-04", "", "## 0.9.13 — 2026-09-04", "", "- [New] Older."]

    updated = add_entry("[New] The first of this version.", lines=lines, version="0.9.14")

    # Including the blank line under it. A version heading carries its blank
    # line above the heading below it, so the first entry written into an empty
    # section lands directly on that next heading unless one is put back.
    assert updated[2:7] == [
        "## 0.9.14 — 2026-09-04",
        "",
        "- [New] The first of this version.",
        "",
        "## 0.9.13 — 2026-09-04",
    ]


def test_the_first_entry_of_the_oldest_version_needs_no_line_under_it():
    lines = ["# Changelog", "", "## 0.9.14 — 2026-09-04", ""]

    updated = add_entry("[New] The only one.", lines=lines, version="0.9.14")

    assert updated == ["# Changelog", "", "## 0.9.14 — 2026-09-04", "", "- [New] The only one."]


def test_a_line_pasted_with_its_own_bullet_is_taken_as_written():
    # The label is right there, so refusing it with "start with a label" would
    # be a lie about what is wrong with it.
    updated = add_entry("- [Fixed] Pasted whole.", lines=list(CURRENT), version="0.9.14")

    assert updated[6] == "- [Fixed] Pasted whole."


def test_whitespace_inside_an_entry_is_one_space():
    updated = add_entry("[Fixed]  Two  spaces\tand a tab.", lines=list(CURRENT), version="0.9.14")

    assert updated[6] == "- [Fixed] Two spaces and a tab."


def test_only_the_agreed_labels_are_accepted():
    for rejected in ("[Improved] no", "[fixed] no", "no label at all", "[Fixed]", "[Fixed] "):
        with pytest.raises(SystemExit):
            add_entry(rejected, lines=list(CURRENT), version="0.9.14")


def test_an_em_dash_is_refused_where_it_would_have_to_be_removed_later():
    with pytest.raises(SystemExit, match="em dash"):
        add_entry("[Fixed] A thing — and another.", lines=list(CURRENT), version="0.9.14")


def test_an_entry_cannot_land_under_a_heading_the_repository_has_moved_past():
    # The heading, `package.json` and `AGENTS.md` are kept in step, so writing
    # an entry against a stale one is the moment to say so rather than after.
    with pytest.raises(SystemExit, match="synchronize the version"):
        add_entry("[Fixed] A thing.", lines=list(CURRENT), version="0.9.15")


def test_a_paragraph_is_refused_because_an_entry_is_one_line():
    with pytest.raises(SystemExit, match="one line"):
        add_entry("[Fixed] A thing.\n\nAnd a second paragraph.", lines=list(CURRENT), version="0.9.14")


AGENTS_LINES = ["# AGENTS.md", "", "Current development version: **0.9.14 — 2026-09-04**", ""]


def test_a_date_move_changes_both_files_and_nothing_else():
    changelog, agents = set_date("2026-09-05", changelog=list(CURRENT), agents=list(AGENTS_LINES), version="0.9.14")

    assert changelog[4] == "## 0.9.14 — 2026-09-05"
    assert agents[2] == "Current development version: **0.9.14 — 2026-09-05**"
    # Only those two lines: the older version keeps its own date.
    assert [line for line, before in zip(changelog, CURRENT) if line != before] == ["## 0.9.14 — 2026-09-05"]
    assert changelog[9] == "## 0.9.13 — 2026-09-04"


@pytest.mark.parametrize("date", ["2026-9-5", "05.09.2026", "2026-02-30"])
def test_a_date_that_is_not_one_is_refused(date):
    with pytest.raises(SystemExit):
        set_date(date, changelog=list(CURRENT), agents=list(AGENTS_LINES), version="0.9.14")


def test_a_date_move_is_not_a_version_change():
    # The three disagreeing is a version that was never synchronized, and moving
    # the date would hide that behind a matching day.
    with pytest.raises(SystemExit, match="not synchronized"):
        set_date("2026-09-05", changelog=list(CURRENT), agents=list(AGENTS_LINES), version="0.9.15")
