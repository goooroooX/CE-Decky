"""The table switch helper runs the production RPCs in the one order that works."""
from __future__ import annotations

import pytest

from scripts import target_table_switch as switcher

APP = 10
OLD = "a" * 64
NEW = "b" * 64
PROFILE = {"app_id": APP, "name": "A game", "is_shortcut": True, "target_process": "game.exe"}


class FakeBackend:
    def __init__(self, *, running=True, consented=False, quiesce=None, loads_after=1):
        self.calls: list[tuple[str, list]] = []
        self.running = running
        self.consented = consented
        self.quiesce = quiesce if quiesce is not None else {"asked": True, "cleanup_confirmed": True, "records_unsettled": []}
        self.loads_after = loads_after
        self.launched = False
        self.polls = 0

    def __call__(self, method, args):
        self.calls.append((method, args))
        if method == "list_profiles":
            return [PROFILE, {**PROFILE, "app_id": 99}]
        if method == "get_runtime_status":
            if not self.launched:
                return {"connected": self.running, "owned_launch_state": "matched" if self.running else None,
                        "prepared": {"table_sha256": OLD} if self.running else None}
            self.polls += 1
            loaded = self.polls >= self.loads_after
            return {"connected": loaded, "status": {"attached": loaded, "table_load_state": "loaded" if loaded else "loading",
                                                    "table_sha256": NEW, "session_id": "s-new", "opened_process_id": 7}}
        if method == "stop_ce_for_game":
            return {"operation": {"state": "stopped"}, "quiesce": self.quiesce}
        if method == "save_profile":
            return {"table_sha256": args[3], "execution_consent_sha256": NEW if self.consented else None}
        if method == "set_execution_consent":
            return {"table_sha256": NEW, "execution_consent_sha256": NEW}
        if method == "get_ce_launch_capability":
            return {"observed_proton_tool": {"name": "Proton X", "tool_id": "t" * 64}}
        if method == "launch_ce_for_game":
            self.launched = True
            return {"state": "starting", "session_id": "s-new", "error": None}
        raise AssertionError(f"unexpected RPC {method}")


def test_a_switch_stops_selects_consents_launches_and_waits_in_that_order():
    backend = FakeBackend(loads_after=3)
    report = switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)

    assert report["ok"] is True and report["launched"] is True and report["session_id"] == "s-new"
    methods = [method for method, _ in backend.calls]
    assert methods[:7] == ["list_profiles", "get_runtime_status", "stop_ce_for_game", "save_profile",
                           "set_execution_consent", "get_ce_launch_capability", "launch_ce_for_game"]
    # The stop names the table the running session holds, and the selection
    # keeps the game's own name, shortcut flag and process.
    assert backend.calls[2][1] == [APP, OLD, False]
    assert backend.calls[3][1] == [APP, "A game", True, NEW, "game.exe"]
    assert backend.calls[6][1] == [APP, "t" * 64, False]
    assert backend.polls == 3


def test_without_consent_the_table_is_selected_and_nothing_is_launched():
    backend = FakeBackend(running=False)
    report = switcher.switch(APP, NEW, backend, sleep=lambda _s: None)

    assert report["launched"] is False
    methods = [method for method, _ in backend.calls]
    assert "stop_ce_for_game" not in methods and "set_execution_consent" not in methods
    assert "launch_ce_for_game" not in methods


def test_a_stop_that_left_a_record_up_ends_the_switch_before_anything_is_selected():
    backend = FakeBackend(quiesce={"asked": True, "cleanup_confirmed": False, "records_unsettled": [5]})
    with pytest.raises(switcher.SwitchRefused, match="put down"):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)
    assert "save_profile" not in [method for method, _ in backend.calls]


def test_a_bridge_that_never_reports_the_table_is_not_a_success():
    backend = FakeBackend(loads_after=10**6)
    with pytest.raises(switcher.SwitchRefused, match="may still be starting"):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)


def test_identity_is_exact():
    with pytest.raises(switcher.SwitchRefused):
        switcher.switch(APP, "B" * 64, FakeBackend(), sleep=lambda _s: None)
    with pytest.raises(switcher.SwitchRefused):
        switcher.switch(0, NEW, FakeBackend(), sleep=lambda _s: None)
