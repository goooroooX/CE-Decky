"""The table switch helper runs the production RPCs in the one order that works."""
from __future__ import annotations

import pytest

from scripts import target_table_switch as switcher

APP = 10
OLD = "a" * 64
NEW = "b" * 64
TOOL = "t" * 64
PROFILE = {"app_id": APP, "name": "A game", "is_shortcut": True, "target_process": "game.exe", "table_sha256": OLD}
CLEAN = {"asked": True, "cleanup_confirmed": True, "records_unsettled": []}


class FakeBackend:
    def __init__(self, *, running=True, connected=None, recovered=False, consented=False, quiesce=CLEAN,
                 stopped=True, loads_after=1, owner_state="matched", ownership_error=None, tool=TOOL,
                 inspect_error=None, blocked=()):
        self.calls: list[tuple[str, list]] = []
        self.running = running
        self.connected = running if connected is None else connected
        self.recovered = recovered
        self.consented = consented
        self.quiesce = quiesce
        self.stopped = stopped
        self.loads_after = loads_after
        self.owner_state = owner_state
        self.ownership_error = ownership_error
        self.tool = tool
        self.inspect_error = inspect_error
        self.blocked = list(blocked)
        self.launched = False
        self.polls = 0

    def methods(self):
        return [method for method, _ in self.calls]

    def __call__(self, method, args):
        self.calls.append((method, args))
        if method == "list_profiles":
            return [PROFILE, {**PROFILE, "app_id": 99}]
        if method == "inspect_table_sha":
            if self.inspect_error:
                raise RuntimeError(f"Decky RPC failed: {self.inspect_error}")
            return {"controls": []}
        if method == "list_blocked_tables":
            return {"schema": 1, "tables": self.blocked}
        if method == "get_runtime_status":
            if not self.launched:
                # No `owned_launch_state` here: the backend's runtime status
                # never carried one, which is what the helper used to read.
                return {"connected": self.connected, "prepared": {"table_sha256": OLD} if self.running else None}
            self.polls += 1
            loaded = self.polls >= self.loads_after
            return {"connected": loaded, "status": {"attached": loaded, "table_load_state": "loaded" if loaded else "loading",
                                                    "table_sha256": NEW, "session_id": "s-new", "opened_process_id": 7}}
        if method == "get_ce_launch_capability":
            owners = ([{"app_id": APP, "state": self.owner_state, "recovered": self.recovered}]
                      if self.running else [])
            return {"observed_proton_tool": {"name": "Proton X", "tool_id": self.tool} if self.tool else None,
                    "owned_launch_owners": owners, "ownership_state_error": self.ownership_error}
        if method == "stop_ce_for_game":
            if self.recovered:
                return {"stopped": self.stopped, "operation": None, "recovered": True, "quiesce": self.quiesce}
            return {"stopped": self.stopped, "operation": {"state": "stopped"}, "recovered": False, "quiesce": self.quiesce}
        if method == "save_profile":
            return {"table_sha256": args[3], "execution_consent_sha256": NEW if self.consented else None}
        if method == "set_execution_consent":
            return {"table_sha256": NEW, "execution_consent_sha256": NEW}
        if method == "launch_ce_for_game":
            self.launched = True
            return {"state": "starting", "session_id": "s-new", "error": None}
        raise AssertionError(f"unexpected RPC {method}")


def test_a_switch_proves_the_destination_stops_selects_consents_launches_and_waits_in_that_order():
    backend = FakeBackend(loads_after=3)
    report = switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)

    assert report["ok"] is True and report["launched"] is True and report["session_id"] == "s-new"
    assert backend.methods()[:8] == ["list_profiles", "inspect_table_sha", "list_blocked_tables", "get_runtime_status",
                                     "get_ce_launch_capability", "stop_ce_for_game", "save_profile",
                                     "set_execution_consent"]
    assert backend.methods()[8] == "launch_ce_for_game"
    assert backend.calls[1][1] == [NEW, APP]
    # The stop names the table the running session holds and holds Auto-load,
    # so an open panel cannot start the old table again before the selection;
    # the launch is a press, which is what lifts that hold.
    assert backend.calls[5][1] == [APP, OLD, True]
    assert backend.calls[6][1] == [APP, "A game", True, NEW, "game.exe"]
    assert backend.calls[8][1] == [APP, TOOL, False]
    assert backend.methods().count("launch_ce_for_game") == 1
    assert backend.polls == 3


@pytest.mark.parametrize("connected", [True, False])
def test_a_recovered_cheat_engine_is_stopped_and_switched_whether_or_not_its_bridge_answers(connected):
    backend = FakeBackend(recovered=True, connected=connected)
    report = switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)

    assert report["ok"] is True and report["launched"] is True
    assert backend.methods().count("stop_ce_for_game") == 1
    stop = next(step for step in report["steps"] if step["step"] == "stop")
    assert stop["stopped"] is True and stop["recovered"] is True and stop["state"] is None


@pytest.mark.parametrize("recovered", [True, False])
def test_a_stop_that_did_not_prove_cheat_engine_gone_changes_nothing(recovered):
    backend = FakeBackend(recovered=recovered, stopped=False)
    with pytest.raises(switcher.SwitchRefused, match="not proven stopped"):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)
    assert "save_profile" not in backend.methods() and "set_execution_consent" not in backend.methods()


@pytest.mark.parametrize("quiesce", [
    {"asked": True, "cleanup_confirmed": False, "records_unsettled": [5]},
    {"asked": False, "reason": "bridge raised", "cleanup_confirmed": False},
    {"asked": True, "answered": False, "cleanup_confirmed": False, "records_unsettled": []},
    {"asked": True, "cleanup_confirmed": True, "records_unsettled": [5]},
    {"asked": False, "cleanup_confirmed": None},
    # No verdict at all, which is what a backend with no bridge to ask
    # answers: not proof that nothing was up.
    None,
])
def test_a_stop_without_a_confirmed_clean_game_ends_the_switch_before_anything_is_selected(quiesce):
    backend = FakeBackend(quiesce=quiesce)
    with pytest.raises(switcher.SwitchRefused, match="put down"):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)
    assert "save_profile" not in backend.methods() and "set_execution_consent" not in backend.methods()


def test_a_stop_of_a_cheat_engine_that_never_started_counts_as_clean():
    backend = FakeBackend(quiesce={"asked": False, "answered": False, "reason": "Cheat Engine had not started",
                                   "cleanup_confirmed": True, "records_unsettled": []})
    assert switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)["launched"] is True


@pytest.mark.parametrize("backend", [
    FakeBackend(ownership_error="record directory unreadable"),
    FakeBackend(owner_state="unreadable"),
    FakeBackend(owner_state="invalid", connected=False),
], ids=["inventory", "unreadable", "invalid"])
def test_ownership_that_cannot_be_read_stops_nothing(backend):
    with pytest.raises(switcher.SwitchRefused, match="ownership"):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)
    assert "stop_ce_for_game" not in backend.methods() and "save_profile" not in backend.methods()


@pytest.mark.parametrize(("backend", "match"), [
    (FakeBackend(inspect_error="table blob is unavailable"), "destination table cannot be used"),
    (FakeBackend(blocked=[{"sha256": NEW, "cause": "refused"}]), "marked as not working"),
    (FakeBackend(tool=None), "no Proton build"),
], ids=["missing-or-damaged", "not-working", "no-proton"])
def test_a_destination_that_cannot_be_used_leaves_the_running_cheat_engine_alone(backend, match):
    with pytest.raises(switcher.SwitchRefused, match=match):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)
    assert "stop_ce_for_game" not in backend.methods() and "save_profile" not in backend.methods()


def test_a_lost_answer_is_not_reported_as_a_destination_the_backend_refused():
    class Closed(FakeBackend):
        def __call__(self, method, args):
            if method == "inspect_table_sha":
                self.calls.append((method, args))
                raise switcher.target_plugin_rpc.DeckyWebSocketClosed("socket closed")
            return super().__call__(method, args)

    backend = Closed()
    with pytest.raises(switcher.target_plugin_rpc.DeckyWebSocketClosed):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)
    assert "stop_ce_for_game" not in backend.methods()


def test_a_mark_that_is_not_a_compatibility_failure_refuses_nothing():
    backend = FakeBackend(blocked=[{"sha256": NEW, "cause": "gone"},{"sha256": OLD, "cause": "refused"}])
    assert switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)["ok"] is True


def test_without_consent_the_table_is_selected_nothing_is_launched_and_no_proton_is_needed():
    backend = FakeBackend(running=False, tool=None)
    report = switcher.switch(APP, NEW, backend, sleep=lambda _s: None)

    assert report["launched"] is False
    assert "stop_ce_for_game" not in backend.methods() and "set_execution_consent" not in backend.methods()
    assert "launch_ce_for_game" not in backend.methods()


def test_without_consent_a_running_cheat_engine_is_stopped_with_auto_load_still_held():
    backend = FakeBackend()
    report = switcher.switch(APP, NEW, backend, sleep=lambda _s: None)

    assert report["launched"] is False
    assert next(args for method, args in backend.calls if method == "stop_ce_for_game") == [APP, OLD, True]
    assert "launch_ce_for_game" not in backend.methods()


def test_a_bridge_that_never_reports_the_table_is_not_a_success():
    backend = FakeBackend(loads_after=10**6)
    with pytest.raises(switcher.SwitchRefused, match="may still be starting"):
        switcher.switch(APP, NEW, backend, consent=True, sleep=lambda _s: None)


def test_identity_is_exact():
    with pytest.raises(switcher.SwitchRefused):
        switcher.switch(APP, "B" * 64, FakeBackend(), sleep=lambda _s: None)
    with pytest.raises(switcher.SwitchRefused):
        switcher.switch(0, NEW, FakeBackend(), sleep=lambda _s: None)
