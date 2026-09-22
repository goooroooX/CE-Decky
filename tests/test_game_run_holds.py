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
        asyncio.run(service.launch_ce_for_game(10))
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
            await service.launch_ce_for_game(10)
        except ValueError as exc:
            refusals.append(str(exc))
        release.set()
        await stopping
        try:
            await service.launch_ce_for_game(10)
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
        asyncio.run(service.launch_ce_for_game(10))


def test_a_hold_that_could_not_be_taken_at_all_is_still_taken(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("session record went away")
    monkeypatch.setattr(service, "_hold_run", broken)
    _stopping(service, confirmed=False, unsettled=["6"])
    asyncio.run(service.stop_ce_for_game(10))
    assert service._public_run_holds(10)["dirty"] == {"since": service._current_run_holds(10)["dirty"].since, "unsettled": 1}
    with pytest.raises(ValueError, match="Restart the game"):
        asyncio.run(service.launch_ce_for_game(10))


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
        asyncio.run(service.launch_ce_for_game(20))
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
