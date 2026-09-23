"""The parsing a press turns on, which now decides what gets clicked on a device.

This helper may press any control the plugin draws, including the ones that
write, so the two pure decisions in front of that press are worth holding: which
row a `--press` word names, and which of several pages' refusals is the one the
operator is shown.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import target_panel_read as panel_read  # noqa: E402


def test_a_bare_name_is_looked_for_everywhere():
    assert panel_read.press_target("Manage") == (None, "Manage")
    assert panel_read.press_target("Back to the list") == (None, "Back to the list")
    assert panel_read.press_target("Advanced…") == (None, "Advanced…")


def test_a_test_id_in_front_names_the_one_row_to_look_in():
    assert panel_read.press_target("table-row:Search") == ("table-row", "Search")
    assert panel_read.press_target("imported-table-33a60e6b:Use") == ("imported-table-33a60e6b", "Use")
    # The row the reader lists the buttons of no row under takes a scope too,
    # which it did not while it was spelled `(untagged)`.
    assert panel_read.press_target("untagged-controls:Load table & start CE") == (
        "untagged-controls", "Load table & start CE",
    )
    assert "untagged-controls" in panel_read._READ and "untagged-controls" in panel_read._PRESS


def test_a_control_whose_own_wording_carries_a_colon_keeps_it():
    """A scope is one hyphenated token, which a sentence is not.

    Read as a scope, `Confirm: delete` would look for `delete` inside a row
    called `Confirm` - which is to say nowhere - and the press would report a
    control that is on screen as missing.
    """
    assert panel_read.press_target("Confirm: delete") == (None, "Confirm: delete")
    assert panel_read.press_target("Ready: 9727076d") == (None, "Ready: 9727076d")
    # And a scoped press whose name carries one keeps both halves.
    assert panel_read.press_target("manage-list:Confirm: delete") == ("manage-list", "Confirm: delete")


def test_the_refusal_reported_is_the_one_that_saw_the_controls():
    """Most pages draw nothing of this plugin's, and answer that way.

    A page that saw four rows called `Use` is the one with the screen on it,
    and its refusal is the one that tells the reader what to write instead.
    """
    nothing = {"pressed": False, "reason": "no page answered"}
    absent = {"pressed": False, "reason": "no control with that name is on screen"}
    disabled = {"pressed": False, "where": "manage-summary", "reason": "that control is disabled right now"}
    ambiguous = {"pressed": False, "matches": ["imported-table-a", "imported-table-b"], "reason": "that name is on 2"}
    ranks = [panel_read._refusal_rank(answer) for answer in (nothing, absent, disabled, ambiguous)]
    assert ranks == sorted(ranks) and len(set(ranks)) == 4
