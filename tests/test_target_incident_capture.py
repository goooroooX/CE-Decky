"""The incident capture must name its directory and its finding the way it says.

It runs while a symptom is live, so a directory name that throws, or a summary
that calls one sample's busy thread a loop, costs the one capture there is.
"""
from __future__ import annotations

import json

import pytest

from scripts import target_incident_capture as capture


def test_the_directory_is_named_for_the_symptom_in_plain_words():
    assert capture._slug("Panel froze on collapse!") == "panel-froze-on-collapse"
    with pytest.raises(ValueError):
        capture._slug("  !!  ")


def _sample(tmp_path, name, spinning_pids):
    processes = [
        {"pid": pid, "role": "renderer", "threads": [
            {"comm": "steamwebhelper", "cpu_share": 1.0, "verdict": "spinning" if pid in spinning_pids else "parked"},
        ]}
        for pid in (1, 2)
    ]
    path = tmp_path / name
    path.write_text(json.dumps({"processes": processes}) + "\n--- stderr ---\nnoise\n")
    return path


def test_only_a_thread_spinning_in_both_samples_is_reported(tmp_path):
    first = capture._spinning(_sample(tmp_path, "a.json", {1, 2}))
    second = capture._spinning(_sample(tmp_path, "b.json", {2}))
    assert sorted(set(first) & set(second)) == ["pid 2 renderer: steamwebhelper at 1.0 of a CPU"]


def test_an_unreadable_sample_reports_nothing_rather_than_failing(tmp_path):
    (tmp_path / "bad.json").write_text("not json")
    assert capture._spinning(tmp_path / "bad.json") == []
    assert capture._spinning(tmp_path / "missing.json") == []


def test_the_backend_log_dates_the_panel_and_the_game():
    log = "\n".join([
        "[2026-11-05 23:21:18,903][INFO]: ce-decky activity event=operation.completed duration_ms=5 name=counted operation=a",
        "[2026-11-05 23:21:26,787][INFO]: ce-decky activity event=operation.started name=note_panel_opened operation=b",
        "[2026-11-05 23:22:19,390][INFO]: ce-decky activity event=fearless.index_completed indexed=338",
        "[2026-11-05 23:32:21,079][INFO]: ce-decky activity event=ce_launch.target_changed app_id=1 target_state=absent",
    ])
    found = capture.backend_landmarks(log)
    assert found["last_panel_call"] == "2026-11-05 23:21:26,787"
    assert len(found["target_events"]) == 1 and "target_state=absent" in found["target_events"][0]
