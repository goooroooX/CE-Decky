"""A game a stop could not prove clean stays held until that run of it is over.

The backend owns the hold and keeps it on disk, one per game, because the panel
is remounted, the plugin is reloaded and more than one game can be running, and
none of those may lift a hold on a game still running what it was about. It is
lifted only on proof: the run's own processes gone, or where those could not be
listed, its programs proven absent. Not knowing keeps it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import zipfile

import pytest

from ce_decky import service as service_module
from ce_decky.ce_launch import TargetIdentity, run_identities_gone
from ce_decky.game_run_holds import GameRunHolds, new_hold
from ce_decky.paths import PluginPaths
from ce_decky.service import DIRTY_RUN_REFUSAL, PluginService


def _stat(start_time: int) -> str:
    # Fields after the command name, the state first: the start time is the
    # twentieth of them, which is field 22 of the whole line.
    return "1 (game.exe) S " + " ".join(["0"] * 18) + f" {start_time} 0 0\n"


def _proc(tmp_path: Path) -> Path:
    root = tmp_path / "proc"
    (root / str(os.getpid())).mkdir(parents=True)
    (root / str(os.getpid()) / "stat").write_text(_stat(1))
    return root


def _identity(pid: int = 4242, start_time: int = 500) -> TargetIdentity:
    return TargetIdentity(pid=pid, start_time=start_time, windows_executable="game.exe", app_id=10)


def test_a_run_is_over_only_where_every_process_of_it_is_proven_gone(tmp_path: Path):
    root = _proc(tmp_path)
    identity = _identity()
    # No entry for it in a `/proc` that answers for this process: it ended.
    assert run_identities_gone([identity], proc_root=root) is True
    # The same PID started at another time is another process.
    (root / "4242").mkdir()
    (root / "4242" / "stat").write_text(_stat(900))
    assert run_identities_gone([identity], proc_root=root) is True
    # The same process is still running.
    (root / "4242" / "stat").write_text(_stat(500))
    assert run_identities_gone([identity], proc_root=root) is False
    # A read refused for any other reason proves nothing.
    (root / "4242" / "stat").unlink()
    (root / "4242" / "stat").mkdir()
    assert run_identities_gone([identity], proc_root=root) is False
    # Nor does a `/proc` that does not answer for this process itself, which is
    # how an unmounted one answers "no such file" for every PID.
    assert run_identities_gone([identity], proc_root=tmp_path / "nowhere") is False
    # And nothing proves nothing.
    assert run_identities_gone([], proc_root=root) is False


def test_holds_are_kept_per_game_and_survive_a_new_reader(tmp_path: Path):
    store = GameRunHolds(tmp_path / "holds.json")
    store.save({
        10: {"dirty": new_hold(10, "dirty", ("game.exe",), (_identity(),), 2)},
        20: {"dirty": new_hold(20, "dirty", None, None), "stopped": new_hold(20, "stopped", ("b.exe",), None)},
    })
    loaded = GameRunHolds(tmp_path / "holds.json").load()
    assert set(loaded) == {10, 20}
    assert loaded[10]["dirty"].identities == (_identity(),)
    assert loaded[10]["dirty"].unsettled == 2
    assert loaded[20]["dirty"].targets is None
    assert set(loaded[20]) == {"dirty", "stopped"}
    (tmp_path / "holds.json").write_text("{\"schema\": 9}")
    with pytest.raises(ValueError):
        GameRunHolds(tmp_path / "holds.json").load()


def _service(tmp_path: Path) -> PluginService:
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("run-holds-test"))
    service.initialize()
    return service


def _stopping(service: PluginService, *, confirmed: bool, unsettled: list[str] | None = None) -> None:
    async def stop_for_app(app_id: int, **_kwargs):
        return {
            "stopped": True, "operation": None, "recovered": False,
            "quiesce": {"asked": True, "answered": True, "reason": None,
                        "cleanup_confirmed": confirmed, "records_unsettled": unsettled or []},
        }
    service.ce_launch.stop_for_app = stop_for_app  # type: ignore[method-assign]


def test_an_unconfirmed_stop_holds_that_game_and_no_other(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(pid=app_id * 100),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())

    _stopping(service, confirmed=False, unsettled=["6"])
    asyncio.run(service.stop_ce_for_game(10))
    asyncio.run(service.stop_ce_for_game(20))
    # One game's hold never replaces another's.
    assert service._public_run_holds(10)["dirty"]["unsettled"] == 1
    assert service._public_run_holds(10)["autoload_held"] is False
    assert service._public_run_holds(20)["dirty"] is not None
    # A reload of the plugin is a new reader of the same file.
    reloaded = _service(tmp_path)
    assert reloaded._public_run_holds(10)["dirty"] is not None

    # Every start in that game is refused by the backend itself.
    with pytest.raises(ValueError, match="Restart the game"):
        asyncio.run(service.launch_ce_for_game(10, None, False))
    assert DIRTY_RUN_REFUSAL.endswith("Restart the game before starting a table in it.")

    # Proof that one game's run is over lifts that game's hold only.
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: identities[0].pid == 1000)
    assert service._public_run_holds(10)["dirty"] is None
    assert service._public_run_holds(20)["dirty"] is not None


def test_not_knowing_whether_the_run_is_over_keeps_the_hold(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    # The processes could not all be listed, so only the programs' absence can
    # lift it, and a walk that could not answer is not absence.
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: None)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    monkeypatch.setattr(service_module, "game_target_states", lambda app_id, names: {name: "unknown" for name in names})
    _stopping(service, confirmed=False)
    asyncio.run(service.stop_ce_for_game(10))
    assert service._public_run_holds(10)["dirty"] is not None
    monkeypatch.setattr(service_module, "game_target_states", lambda app_id, names: {name: "absent" for name in names})
    assert service._public_run_holds(10)["dirty"] is None


def test_a_clean_stop_holds_nothing_and_a_users_stop_holds_auto_load(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    _stopping(service, confirmed=True)
    asyncio.run(service.stop_ce_for_game(10))
    assert service._public_run_holds(10) == {"dirty": None, "autoload_held": False, "error": None, "unreadable": False}
    asyncio.run(service.stop_ce_for_game(10, None, True))
    assert service._public_run_holds(10) == {"dirty": None, "autoload_held": True, "error": None, "unreadable": False}
    # The user may clear what holds a game, having been told what it is.
    assert service.clear_game_run_holds(10)["cleared"] == ["stopped"]
    assert service._public_run_holds(10) == {"dirty": None, "autoload_held": False, "error": None, "unreadable": False}


def test_a_game_none_of_whose_programs_is_running_is_not_held(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: ())
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    _stopping(service, confirmed=False)
    asyncio.run(service.stop_ce_for_game(10))
    assert service._public_run_holds(10)["dirty"] is None


def test_the_bundle_carries_which_games_are_held(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    _stopping(service, confirmed=False, unsettled=["6", "7"])
    asyncio.run(service.stop_ce_for_game(10))
    result = service.create_support_bundle([], 0)
    with zipfile.ZipFile(Path(str(result["path"]))) as archive:
        held = json.loads(archive.read("state/game_run_holds.json"))
    assert held["apps"]["10"]["dirty"]["unsettled"] == 2
    assert held["apps"]["10"]["dirty"]["targets"] == ["game.exe"]


def test_an_auto_load_hold_can_still_be_lifted_by_a_restart_when_the_session_is_unreadable(tmp_path: Path, monkeypatch):
    """Nothing is at stake in it but Auto-load, so it falls back to the profile's program."""
    service = _service(tmp_path)
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: seen.append(tuple(names)) or None)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: None)
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    service.profile_store.upsert(app_id=10, name="Game", is_shortcut=False, table_sha256=None, target_process="game.exe")
    monkeypatch.setattr(service_module, "game_target_states", lambda app_id, names: {name: "present" for name in names})
    _stopping(service, confirmed=False)
    asyncio.run(service.stop_ce_for_game(10, None, True))
    holds = service._current_run_holds(10)
    # The dirty hold may not guess which programs the session was pointed at.
    assert holds["dirty"].targets is None
    assert holds["stopped"].targets == ("game.exe",)
    assert seen == [("game.exe",)]



def test_no_start_is_admitted_while_a_stop_is_still_deciding_what_it_left(tmp_path: Path, monkeypatch):
    """The old Cheat Engine is gone before the hold saying what it left exists.

    A start admitted in that gap would find neither a running Cheat Engine to
    refuse it nor a hold, and go into a game the stop is about to call dirty.
    """
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())

    async def scenario(confirmed: bool) -> list[str]:
        ended = asyncio.Event()
        release = asyncio.Event()

        async def stop_for_app(app_id: int, **_kwargs):
            ended.set()  # the old Cheat Engine and its ownership are gone here
            await release.wait()
            return {"stopped": True, "operation": None, "recovered": False,
                    "quiesce": {"asked": True, "answered": True, "reason": None, "cleanup_confirmed": confirmed}}
        service.ce_launch.stop_for_app = stop_for_app  # type: ignore[method-assign]
        stopping = asyncio.create_task(service.stop_ce_for_game(10))
        await ended.wait()
        refusals: list[str] = []
        try:
            await service.launch_ce_for_game(10, None, False)
        except ValueError as exc:
            refusals.append(str(exc))
        release.set()
        await stopping
        try:
            await service.launch_ce_for_game(10, None, False)
        except Exception as exc:  # noqa: BLE001 - past admission, this launch fails on its fakes
            refusals.append(str(exc))
        return refusals

    during, after = asyncio.run(scenario(False))
    assert "still being stopped" in during
    assert after == DIRTY_RUN_REFUSAL
    # A clean stop holds nothing once its verdict is in: what refuses the start
    # afterwards is whatever else the launch needs, never a hold.
    service.clear_game_run_holds(10)
    during, after = asyncio.run(scenario(True))
    assert "still being stopped" in during
    assert "Restart the game" not in after and "still being stopped" not in after


def test_a_hold_that_could_not_be_saved_is_still_in_force(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())

    def full_disk(_holds):
        raise OSError("No space left on device")
    monkeypatch.setattr(service.game_run_holds, "save", full_disk)
    _stopping(service, confirmed=False)
    asyncio.run(service.stop_ce_for_game(10))
    public = service._public_run_holds(10)
    assert public["dirty"] is not None
    assert "could not be saved" in public["error"]
    with pytest.raises(ValueError, match="Restart the game"):
        asyncio.run(service.launch_ce_for_game(10, None, False))


def test_a_hold_that_could_not_be_taken_at_all_is_still_taken(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("session record went away")
    monkeypatch.setattr(service, "_hold_run", broken)
    _stopping(service, confirmed=False, unsettled=["6"])
    asyncio.run(service.stop_ce_for_game(10))
    assert service._public_run_holds(10)["dirty"] == {"since": service._current_run_holds(10)["dirty"].since, "unsettled": 1}
    with pytest.raises(ValueError, match="Restart the game"):
        asyncio.run(service.launch_ce_for_game(10, None, False))


def test_an_unreadable_record_holds_every_game_until_it_is_cleared(tmp_path: Path):
    """Not reading it is not reading nothing: it may hold a game still running dirty."""
    paths = PluginPaths.for_tests(tmp_path)
    paths.state_root.mkdir(parents=True, exist_ok=True)
    (paths.state_root / "game_run_holds.json").write_text("{not json")
    service = PluginService(paths, logging.getLogger("run-holds-test"))
    service.initialize()
    public = service._public_run_holds(20)
    assert "could not be read" in public["error"]
    assert public["unreadable"] is True
    with pytest.raises(ValueError, match="could not read which games"):
        asyncio.run(service.launch_ce_for_game(20, None, False))
    # Nothing here writes over it while it is unreadable.
    assert (paths.state_root / "game_run_holds.json").read_text() == "{not json"
    # Clearing it starts it over, and says so.
    assert service.clear_game_run_holds(20)["cleared"] == ["unreadable"]
    assert service._public_run_holds(20)["error"] is None
    assert json.loads((paths.state_root / "game_run_holds.json").read_text())["apps"] == {}


def test_no_record_at_all_is_a_device_that_never_held_a_game(tmp_path: Path):
    service = _service(tmp_path)
    assert service._public_run_holds(10) == {"dirty": None, "autoload_held": False, "error": None, "unreadable": False}


def test_a_launch_stopped_by_its_operation_holds_the_game_it_was_in(tmp_path: Path, monkeypatch):
    """That route asks nothing of the bridge first, so it never confirms anything was switched off."""
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    operations = {"attached": {"mode": "attached", "app_id": 10}, "self": {"mode": "self_test", "app_id": None}}
    monkeypatch.setattr(service.ce_launch, "status", lambda operation_id: operations[operation_id])

    async def stop(operation_id: str):
        return {**operations[operation_id], "state": "stopped"}
    monkeypatch.setattr(service.ce_launch, "stop", stop)
    asyncio.run(service.stop_ce_launch("self"))
    assert service._public_run_holds(10)["dirty"] is None
    asyncio.run(service.stop_ce_launch("attached"))
    assert service._public_run_holds(10)["dirty"] is not None


def test_a_stop_holds_the_game_on_what_its_session_was_pointed_at_whatever_changes_meanwhile(tmp_path: Path, monkeypatch):
    """What a stop holds a game on is read before it ends anything.

    A session retried onto `other.exe` may have patched both programs. Read
    again after Cheat Engine is gone, a revoked table or retired session leaves
    only the profile's `game.exe`, which may be gone while `other.exe` still
    runs what was left in it, and no hold would be taken at all.
    """
    service = _service(tmp_path)
    service.profile_store.upsert(app_id=10, name="Game", is_shortcut=False, table_sha256=None, target_process="game.exe")
    session = {"current": object()}
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: session["current"])
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe", "other.exe") if prepared else ())
    asked: list[tuple[str, ...]] = []

    def capture(app_id, names):
        asked.append(tuple(names))
        return (_identity(pid=77),) if "other.exe" in names else ()
    monkeypatch.setattr(service_module, "capture_run_identities", capture)
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)

    async def scenario() -> list[str]:
        ended = asyncio.Event()
        release = asyncio.Event()

        async def stop_for_app(app_id: int, **_kwargs):
            ended.set()
            await release.wait()
            return {"stopped": True, "operation": None, "recovered": False,
                    "quiesce": {"asked": True, "answered": False, "reason": "no answer", "cleanup_confirmed": False}}
        service.ce_launch.stop_for_app = stop_for_app  # type: ignore[method-assign]
        stopping = asyncio.create_task(service.stop_ce_for_game(10))
        await ended.wait()
        refusals: list[str] = []
        for change in (
            lambda: service.save_profile(10, "Game", False, None, "new.exe"),
            lambda: service.revoke_table(10, "a" * 64),
            lambda: service.retire_session(10, "session"),
        ):
            try:
                change()
            except ValueError as exc:
                refusals.append(str(exc))
        # Whatever else happened to the record while the stop ran.
        session["current"] = None
        release.set()
        await stopping
        return refusals

    refusals = asyncio.run(scenario())
    assert len(refusals) == 3 and all("still being stopped" in reason for reason in refusals)
    assert service.profile_store.get(10).target_process == "game.exe"
    hold = service._current_run_holds(10)["dirty"]
    assert hold.targets == ("game.exe", "other.exe")
    assert asked == [("game.exe", "other.exe")]
    with pytest.raises(ValueError, match="Restart the game"):
        asyncio.run(service.launch_ce_for_game(10, None, False))


def _paused_stop(service: PluginService, monkeypatch, *, confirmed: bool):
    """Both stop routes end Cheat Engine and then wait on `release` before their verdict."""
    ended = asyncio.Event()
    release = asyncio.Event()

    async def stop_for_app(app_id: int, **_kwargs):
        ended.set()
        await release.wait()
        return {"stopped": True, "operation": None, "recovered": False,
                "quiesce": {"asked": True, "answered": True, "reason": None, "cleanup_confirmed": confirmed}}

    async def stop(operation_id: str):
        ended.set()
        await release.wait()
        return {"mode": "attached", "app_id": 10, "state": "stopped"}
    service.ce_launch.stop_for_app = stop_for_app  # type: ignore[method-assign]
    monkeypatch.setattr(service.ce_launch, "stop", stop)
    monkeypatch.setattr(service.ce_launch, "status", lambda operation_id: {"mode": "attached", "app_id": 10})
    return ended, release


@pytest.mark.parametrize("first", ["game", "operation"])
def test_a_second_stop_is_refused_and_cannot_end_the_first_ones_transition(tmp_path: Path, monkeypatch, first: str):
    """Two panels can both press Stop, and only the first one owns the stop.

    The second finds nothing left to end. Were it let in, it would clear the
    game's transition on its way out while the first is still deciding what to
    hold, and a start admitted then would go into a game about to be held dirty.
    """
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    routes = {"game": lambda: service.stop_ce_for_game(10), "operation": lambda: service.stop_ce_launch("op")}

    async def scenario() -> tuple[list[str], str]:
        ended, release = _paused_stop(service, monkeypatch, confirmed=False)
        stopping = asyncio.create_task(routes[first]())
        await ended.wait()
        refusals: list[str] = []
        for second in ("game", "operation"):
            # Admitted, it would wait on the same release as the first.
            try:
                await asyncio.wait_for(routes[second](), 1)
            except ValueError as exc:
                refusals.append(str(exc))
            except asyncio.TimeoutError:
                refusals.append("admitted")
        try:
            await service.launch_ce_for_game(10, None, False)
        except ValueError as exc:
            refusals.append(str(exc))
        release.set()
        await stopping
        try:
            await service.launch_ce_for_game(10, None, False)
        except ValueError as exc:
            return refusals, str(exc)
        return refusals, ""

    refusals, after = asyncio.run(scenario())
    assert [reason for reason in refusals[:2] if "already being stopped" in reason] == refusals[:2]
    assert "still being stopped" in refusals[2]
    assert service._run_transitions == {}
    assert after == DIRTY_RUN_REFUSAL


def test_nothing_is_switched_in_a_game_while_it_is_being_stopped(tmp_path: Path, monkeypatch):
    """What a stop holds the game on is read before it ends anything.

    A retried attach accepted after that moves the bridge onto a program the
    stop knows nothing about and starts the table there, and a switched cheat
    changes the game it is about to call clean or dirty. Neither reaches the
    control file; a read still may.
    """
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    # No session record for this fake, so the stop names the profile's program.
    service.profile_store.upsert(app_id=10, name="Game", is_shortcut=False, table_sha256=None, target_process="game.exe")
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: None)
    written: list[object] = []
    monkeypatch.setattr(service.session_store, "write_commands", lambda prepared, commands: written.append(commands))

    async def scenario() -> list[str]:
        ended, release = _paused_stop(service, monkeypatch, confirmed=False)
        stopping = asyncio.create_task(service.stop_ce_for_game(10))
        await ended.wait()
        refusals: list[str] = []
        for command in (
            {"generation": 5, "kind": "retry_attach", "value": "other.exe", "target_pid": 4343},
            {"generation": 5, "kind": "set_active", "record_id": 1, "value": "1"},
            {"generation": 5, "kind": "set_value", "record_id": 1, "value": "99"},
            {"generation": 5, "kind": "query", "record_id": 1},
            {"generation": 5, "kind": "list_processes"},
        ):
            try:
                service.write_runtime_commands(10, [command])
            except ValueError as exc:
                refusals.append(str(exc))
        release.set()
        await stopping
        return refusals

    refusals = asyncio.run(scenario())
    assert all("no cheat is changed and no program attached" in reason for reason in refusals[:3])
    # The reads are past the stop's refusal: here they fail on the session this
    # fake does not have, which is a later check than the one under test.
    assert refusals[3:] == ["no prepared session exists for AppID"] * 2
    assert written == []
    assert service._current_run_holds(10)["dirty"].targets == ("game.exe",)


def test_a_stop_that_could_not_read_what_it_is_about_leaves_the_game_unmarked(tmp_path: Path, monkeypatch):
    """The game is marked before the stop reads its session, and the read can fail.

    A mark left behind by a stop that never ran would refuse every start and
    every stop in that game until the plugin was reloaded.
    """
    service = _service(tmp_path)
    _stopping(service, confirmed=True)
    failures = iter([OSError("record went away")])

    def evidence(app_id: int):
        raise next(failures)
    monkeypatch.setattr(service, "_stop_evidence", evidence)
    with pytest.raises(OSError):
        asyncio.run(service.stop_ce_for_game(10))
    assert service._run_transitions == {}
    monkeypatch.setattr(service, "_stop_evidence", lambda app_id: {"dirty": (), "stopped": ()})
    assert asyncio.run(service.stop_ce_for_game(10))["stopped"] is True


def test_no_update_replaces_the_backend_while_a_stop_is_deciding_what_it_left(tmp_path: Path, monkeypatch):
    """The transition and the verdict it waits for live in this process until the hold is on disk.

    An install replaces the process. Crossing into one while a stop has ended
    Cheat Engine and not yet held the game would leave neither, and the next
    backend would admit a start over what the stop left.
    """
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    committed: list[str] = []
    admit = service.plugin_updates._admit_install

    async def scenario() -> tuple[str | None, object]:
        ended, release = _paused_stop(service, monkeypatch, confirmed=False)
        stopping = asyncio.create_task(service.stop_ce_for_game(10))
        await ended.wait()
        during = admit(lambda: committed.append("installing"))
        release.set()
        await stopping
        return during, service._current_run_holds(10).get("dirty")

    during, hold = asyncio.run(scenario())
    assert during == "Cheat Engine is still being stopped in a game" and committed == []
    assert hold is not None
    # With the verdict held, the install goes ahead.
    assert admit(lambda: committed.append("installing")) is None
    assert committed == ["installing"]


def test_no_stop_begins_once_an_install_has_been_handed_on(tmp_path: Path, monkeypatch):
    """Past that point this backend can be replaced at any moment, so no transition may start in it.

    The next backend recovers the Cheat Engine this one owned and stops it with
    a transaction of its own.
    """
    service = _service(tmp_path)
    reached: list[str] = []

    async def stop_for_app(app_id: int, **_kwargs):
        reached.append("stop_for_app")
        return {"stopped": True, "operation": None, "recovered": False, "quiesce": None}

    async def stop(operation_id: str):
        reached.append("stop")
        return {"mode": "attached", "app_id": 10, "state": "stopped"}
    service.ce_launch.stop_for_app = stop_for_app  # type: ignore[method-assign]
    monkeypatch.setattr(service.ce_launch, "stop", stop)
    monkeypatch.setattr(service.ce_launch, "status", lambda operation_id: {"mode": "attached", "app_id": 10})
    monkeypatch.setattr(service.plugin_updates, "replacement_committed", lambda: True)
    with pytest.raises(ValueError, match="installing an update"):
        asyncio.run(service.stop_ce_for_game(10))
    with pytest.raises(ValueError, match="installing an update"):
        asyncio.run(service.stop_ce_launch("op"))
    assert reached == [] and service._run_transitions == {}
    # Nothing installing: a stop is what it always was.
    monkeypatch.setattr(service.plugin_updates, "replacement_committed", lambda: False)
    assert asyncio.run(service.stop_ce_for_game(10))["stopped"] is True


@pytest.mark.parametrize("hold_autoload, confirmed", [(False, False), (True, True)])
def test_no_update_replaces_the_backend_while_a_hold_is_held_only_in_memory(
    tmp_path: Path, monkeypatch, hold_autoload: bool, confirmed: bool,
):
    """A hold whose write failed is in force here and nowhere else.

    The next backend reads the file, which does not have it: a dirty game would
    admit a start over what the stop left, and a game the user stopped would
    have Auto-load start it straight back. The install waits for the same holds
    to be written, and goes ahead once they are.
    """
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    saved = service.game_run_holds.save

    def full_disk(_holds):
        raise OSError("no space left on device")
    monkeypatch.setattr(service.game_run_holds, "save", full_disk)
    _stopping(service, confirmed=confirmed)
    asyncio.run(service.stop_ce_for_game(10, None, hold_autoload))
    kind = "stopped" if hold_autoload else "dirty"
    assert kind in service._current_run_holds(10)
    assert service._run_holds_unsaved is not None and service._run_transitions == {}
    committed: list[str] = []
    reason = service.plugin_updates._admit_install(lambda: committed.append("installing"))
    assert reason is not None and "could not save which games" in reason and committed == []

    monkeypatch.setattr(service.game_run_holds, "save", saved)
    assert service.plugin_updates._admit_install(lambda: committed.append("installing")) is None
    assert committed == ["installing"] and service._run_holds_unsaved is None
    # What the next backend reads is the hold this one was keeping.
    assert kind in _service(tmp_path)._current_run_holds(10)


def test_a_hold_already_on_disk_does_not_hold_an_update(tmp_path: Path, monkeypatch):
    """The next backend reads the same hold and goes on refusing, so nothing is lost."""
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    _stopping(service, confirmed=False)
    asyncio.run(service.stop_ce_for_game(10))
    assert "dirty" in service._current_run_holds(10)
    assert service.plugin_updates._admit_install(lambda: None) is None



def _armed(service: PluginService, app_id: int = 10) -> None:
    """A profile whose Auto-load is on for its consented table, as Auto-load needs."""
    sha = "f" * 64
    service.profile_store.upsert(app_id=app_id, name="Game", is_shortcut=False, table_sha256=sha, target_process="game.exe")
    service.profile_store.set_execution_consent(app_id=app_id, table_sha256=sha, consent=True)
    service.profile_store.set_autoload(app_id=app_id, table_sha256=sha, enabled=True)


def test_auto_load_is_refused_by_the_users_stop_in_the_backend_itself(tmp_path: Path, monkeypatch):
    """The panel checks the hold first, but it is not the authority.

    A panel left over from a reload, or an Auto-load that read no hold just
    before another panel's Stop, reaches the same launch a press does. Were it
    admitted it would start Cheat Engine behind the Stop and, being a start,
    lift the hold that says not to.
    """
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    _armed(service)
    _stopping(service, confirmed=True)
    asyncio.run(service.stop_ce_for_game(10, None, True))
    assert service._public_run_holds(10)["autoload_held"] is True

    with pytest.raises(ValueError, match="because you stopped it"):
        asyncio.run(service.launch_ce_for_game(10, None, True))
    assert service._public_run_holds(10)["autoload_held"] is True

    # A start by hand is admitted past it, and is what answers it.
    started = {"operation_id": "op", "app_id": 10, "state": "connected"}
    monkeypatch.setattr(service, "_attached_launch_inputs", lambda app_id, automatic=False: (
        _prepared(), Path("cheatengine.exe"), "b" * 64, "game.exe"))
    monkeypatch.setattr(service, "_revalidate_launch_reservation", lambda prepared: None)
    monkeypatch.setattr(service_module, "discover_proton_tools", lambda home: ())

    async def start_attached(*_args, **_kwargs):
        return started
    monkeypatch.setattr(service.ce_launch, "start_attached", start_attached)
    assert asyncio.run(service.launch_ce_for_game(10, None, False)) == started
    assert service._public_run_holds(10)["autoload_held"] is False


def test_a_start_by_hand_lifts_only_the_stop_hold_it_answered(tmp_path: Path, monkeypatch):
    """A Stop taken while a start is still under way is about what that start runs, and stays."""
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    _stopping(service, confirmed=True)
    monkeypatch.setattr(service, "_attached_launch_inputs", lambda app_id, automatic=False: (
        _prepared(), Path("cheatengine.exe"), "b" * 64, "game.exe"))
    monkeypatch.setattr(service, "_revalidate_launch_reservation", lambda prepared: None)
    monkeypatch.setattr(service_module, "discover_proton_tools", lambda home: ())

    async def start_attached(*_args, **_kwargs):
        # The user's Stop, from another panel, while this start is under way.
        service._hold_run(10, "stopped", 0, ("game.exe",))
        return {"operation_id": "op", "app_id": 10, "state": "connected"}
    monkeypatch.setattr(service.ce_launch, "start_attached", start_attached)
    asyncio.run(service.launch_ce_for_game(10, None, False))
    assert service._public_run_holds(10)["autoload_held"] is True


def _prepared():
    from types import SimpleNamespace
    return SimpleNamespace(
        app_id=10, session_id="session", descriptor_path="/tmp/d/descriptor.json", descriptor_sha256="a" * 64,
        descriptor_md5="c" * 32, descriptor_windows_path="Z:\\tmp\\d\\descriptor.json", table_sha256="d" * 64,
        ce_sha256="e" * 64, status_path="/tmp/d/status.json",
    )



def test_a_start_that_does_not_say_it_was_pressed_is_not_let_past_a_stop(tmp_path: Path, monkeypatch):
    """A panel from before the question sends two arguments for Auto-load and Start alike.

    Read as a press, its Auto-load would start Cheat Engine straight back after
    the user's Stop and lift the hold. Only a start that says it was pressed is
    let past; one that says nothing is refused and leaves the hold standing.
    """
    import importlib
    import sys
    import types
    from ce_decky.service import UNSTATED_START_REFUSAL
    # The RPC surface, loaded the way the contract test loads it: Decky's own
    # module exists only inside the loader.
    fake_decky = types.ModuleType("decky")
    fake_decky.logger = logging.getLogger("fake-decky-run-holds")
    monkeypatch.setitem(sys.modules, "decky", fake_decky)
    monkeypatch.delitem(sys.modules, "ce_decky.plugin", raising=False)
    Plugin = importlib.import_module("ce_decky.plugin").Plugin
    service = _service(tmp_path)
    monkeypatch.setattr(service_module, "capture_run_identities", lambda app_id, names: (_identity(),))
    monkeypatch.setattr(service_module, "run_identities_gone", lambda identities: False)
    monkeypatch.setattr(service, "_session_targets", lambda prepared: ("game.exe",))
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: object())
    _armed(service)
    _stopping(service, confirmed=True)
    asyncio.run(service.stop_ce_for_game(10, None, True))

    plugin = Plugin.__new__(Plugin)
    plugin._svc = lambda: service  # type: ignore[method-assign]
    plugin.operations = type("Inline", (), {"create": staticmethod(lambda awaitable, label="": awaitable)})()
    with pytest.raises(ValueError) as refused:
        asyncio.run(plugin.launch_ce_for_game(10, None))
    assert str(refused.value) == UNSTATED_START_REFUSAL
    with pytest.raises(ValueError, match="because you stopped it"):
        asyncio.run(plugin.launch_ce_for_game(10, None, True))
    assert service._public_run_holds(10)["autoload_held"] is True

    monkeypatch.setattr(service, "_attached_launch_inputs", lambda app_id, automatic=None: (
        _prepared(), Path("cheatengine.exe"), "b" * 64, "game.exe"))
    monkeypatch.setattr(service, "_revalidate_launch_reservation", lambda prepared: None)
    monkeypatch.setattr(service_module, "discover_proton_tools", lambda home: ())

    async def start_attached(*_args, **_kwargs):
        return {"operation_id": "op", "app_id": 10, "state": "connected"}
    monkeypatch.setattr(service.ce_launch, "start_attached", start_attached)
    asyncio.run(plugin.launch_ce_for_game(10, None, False))
    assert service._public_run_holds(10)["autoload_held"] is False



def test_auto_load_is_refused_where_the_backend_has_it_switched_off(tmp_path: Path, monkeypatch):
    """A panel that has not seen Auto-load switched off still believes it on.

    Seen on the device: Auto-load was switched off from outside the panel, a
    stop that held nothing followed, and the panel's own Auto-load started
    Cheat Engine straight back because the backend took its word for it.
    """
    from ce_decky.service import AUTOLOAD_OFF_REFUSAL
    service = _service(tmp_path)
    with pytest.raises(ValueError) as refused:
        asyncio.run(service.launch_ce_for_game(10, None, True))
    assert str(refused.value) == AUTOLOAD_OFF_REFUSAL
    _armed(service)
    service.profile_store.set_autoload(app_id=10, table_sha256="f" * 64, enabled=False)
    with pytest.raises(ValueError) as refused:
        asyncio.run(service.launch_ce_for_game(10, None, True))
    assert str(refused.value) == AUTOLOAD_OFF_REFUSAL
    # Armed, but the consent is for no table any more.
    service.profile_store.set_autoload(app_id=10, table_sha256="f" * 64, enabled=True)
    service.profile_store.set_execution_consent(app_id=10, table_sha256="f" * 64, consent=False)
    with pytest.raises(ValueError) as refused:
        asyncio.run(service.launch_ce_for_game(10, None, True))
    assert str(refused.value) == AUTOLOAD_OFF_REFUSAL
    service.profile_store.set_execution_consent(app_id=10, table_sha256="f" * 64, consent=True)
    # Switched on, it is past this check and meets the next one, which is this
    # fake's missing table.
    service.profile_store.set_autoload(app_id=10, table_sha256="f" * 64, enabled=True)
    with pytest.raises(ValueError, match="table store directory is missing"):
        asyncio.run(service.launch_ce_for_game(10, None, True))


def _launchable(service: PluginService, monkeypatch) -> None:
    """Past everything before the spawn, so what a test decides is the admission."""
    monkeypatch.setattr(service, "_attached_launch_inputs", lambda app_id, automatic=None: (
        _prepared(), Path("cheatengine.exe"), "b" * 64, "game.exe"))
    monkeypatch.setattr(service, "_revalidate_launch_reservation", lambda prepared: None)
    monkeypatch.setattr(service_module, "discover_proton_tools", lambda home: ())
    # The session this start prepared is still the game's when it reaches the fork.
    monkeypatch.setattr(service.session_store, "load_current", lambda app_id: _prepared())


def test_a_start_that_does_not_say_it_was_pressed_is_not_made_where_auto_load_is_off(tmp_path: Path, monkeypatch):
    """A panel from before the question sends its Auto-load the way it sends a press.

    Where the backend has Auto-load off, that request may be the Auto-load of
    a panel that has not seen it switched off. Only a start that says it was
    pressed is made.
    """
    from ce_decky.service import AUTOLOAD_OFF_REFUSAL, UNSTATED_AUTOLOAD_OFF_REFUSAL
    service = _service(tmp_path)
    _armed(service)
    service.profile_store.set_autoload(app_id=10, table_sha256="f" * 64, enabled=False)
    _launchable(service, monkeypatch)
    spawned: list[bool | None] = []

    async def start_attached(*_args, spawn_gate=None, **_kwargs):
        # The supervisor's own shape: the gate is read under its lock, around the fork.
        with spawn_gate.lock:
            spawn_gate.check()
            spawned.append(True)
        return {"operation_id": "op", "app_id": 10, "state": "connected"}
    monkeypatch.setattr(service.ce_launch, "start_attached", start_attached)

    with pytest.raises(ValueError) as refused:
        asyncio.run(service.launch_ce_for_game(10, None))
    assert str(refused.value) == UNSTATED_AUTOLOAD_OFF_REFUSAL
    with pytest.raises(ValueError) as refused:
        asyncio.run(service.launch_ce_for_game(10, None, True))
    assert str(refused.value) == AUTOLOAD_OFF_REFUSAL
    assert spawned == []
    asyncio.run(service.launch_ce_for_game(10, None, False))
    assert spawned == [True]


@pytest.mark.parametrize("withdrawn_by", ["auto_load_off", "stop"])
def test_an_auto_load_admitted_before_its_authority_went_is_not_spawned(tmp_path: Path, monkeypatch, withdrawn_by: str):
    """Admission comes before the session, the Proton tool and the launch, and a start is not made on it alone.

    Switching Auto-load off, or a Stop beginning, while an Auto-load is still
    on its way to the fork is the user withdrawing exactly that start. The
    service's gate is read again, under the same lock those take, around the
    fork itself; `tests/test_ce_launch.py` holds the supervisor to reading it
    there.
    """
    service = _service(tmp_path)
    _armed(service)
    _launchable(service, monkeypatch)
    spawned: list[bool] = []

    async def scenario() -> str:
        reached = asyncio.Event()
        release = asyncio.Event()

        async def start_attached(*_args, spawn_gate=None, **_kwargs):
            # Published, and on its way to the fork.
            reached.set()
            await release.wait()
            with spawn_gate.lock:
                spawn_gate.check()
                spawned.append(True)
            return {"operation_id": "op", "app_id": 10, "state": "connected"}
        monkeypatch.setattr(service.ce_launch, "start_attached", start_attached)
        launching = asyncio.create_task(service.launch_ce_for_game(10, None, True))
        await reached.wait()
        transition = None
        if withdrawn_by == "auto_load_off":
            service.profile_store.set_autoload(app_id=10, table_sha256="f" * 64, enabled=False)
        else:
            transition, _evidence = service._begin_run_transition(10)
        release.set()
        try:
            await launching
        except ValueError as exc:
            return str(exc)
        finally:
            if transition is not None:
                service._end_run_transition(10, transition)
        return "spawned"

    outcome = asyncio.run(scenario())
    assert spawned == []
    assert ("Auto-load is switched off" in outcome) if withdrawn_by == "auto_load_off" else ("still being stopped" in outcome)
