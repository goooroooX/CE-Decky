from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import asyncio
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from ce_decky.ce_launch import (
    MODE_ATTACHED,
    MODE_SELF_TEST,
    SELF_TEST_TABLE,
    CELaunchSupervisor,
    CompatDataResolution,
    GameContainerObservation,
    _describe_unfinished_bridge,
    _read_bridge_status,
    game_process_state,
    game_target_state,
    game_target_states,
    target_exit_is_proved,
    launch_environment,
    match_observed_proton,
    observe_game_container,
    observe_running_app_ids,
    owned_descriptor_pids,
    owned_descriptor_state,
    owned_any_descriptor_pids,
    owned_process_matches,
    owned_process_state,
    parse_environ,
    plan_ce_launch,
    prepare_self_test_session,
    resolve_game_mode_display,
    resolve_compat_data,
)
from ce_decky.managed_ce import ProtonTool, discover_proton_tools
from ce_decky.paths import PluginPaths
from ce_decky.session_protocol import RuntimeStatus, parse_descriptor, render_status


# How long a tick of recovered supervision lasts while these tests drive it.
#
# The loop polls once a second on the device, and every test here is about what
# it decides on a tick rather than about how long one lasts: at the real
# interval this family spent thirty seconds of the backend gate asleep, because
# a scenario that has to reach the third tick has to wait three seconds for it.
# Each window below is counted in ticks rather than in wall clock, and the ones
# whose result is that nothing happened count the ticks they gave the loop and
# refuse a window that did not give it any: a negative result from a loop that
# never ran is the one thing a shorter tick could quietly turn this family into.
SUPERVISION_TICK = 0.01


@pytest.fixture(autouse=True)
def _fast_recovered_supervision(monkeypatch):
    """Shorten that interval for this file, and put it back afterwards."""
    import ce_decky.ce_launch as launch_module

    monkeypatch.setattr(launch_module, "RECOVERED_SUPERVISION_POLL_SECONDS", SUPERVISION_TICK)


def _tool(root: Path, name: str = "Proton 10") -> ProtonTool:
    return ProtonTool(
        tool_id="a" * 64,
        name=name,
        path=str(root),
        proton_sha256="b" * 64,
        source="steam-library",
    )


def _proc(root: Path, pid: int, environ: dict[str, str], *, argv: list[str] | None = None) -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True)
    payload = b"".join(f"{key}={value}".encode("utf-8") + b"\0" for key, value in environ.items())
    (directory / "environ").write_bytes(payload)
    if argv is not None:
        (directory / "cmdline").write_bytes(b"".join(argument.encode("utf-8") + b"\0" for argument in argv))


def _steam_home(root: Path, *, app_id: int, libraries: list[Path] | None = None) -> Path:
    home = root / "home"
    steam = home / ".local/share/Steam"
    (steam / "steamapps/compatdata" / str(app_id) / "pfx").mkdir(parents=True)
    if libraries:
        rendered = "".join(f'\t\t\t"path"\t\t"{library.as_posix()}"\n' for library in libraries)
        (steam / "steamapps/libraryfolders.vdf").write_text(
            '"libraryfolders"\n{\n\t"0"\n\t{\n' + rendered + "\t}\n}\n", encoding="utf-8"
        )
    return home


def test_parse_environ_keeps_only_well_formed_bounded_entries():
    raw = b"A=1\x00SteamAppId=440\x00no-separator\x00=empty-name\x00B=" + b"x" * 10 + b"\x00"
    assert parse_environ(raw) == {"A": "1", "SteamAppId": "440", "B": "x" * 10}


def test_parse_environ_rejects_invalid_utf8_and_control_bearing_values():
    assert parse_environ(b"A=\xff\xfe\x00B=ok\x00") == {"B": "ok"}
    assert "C" not in parse_environ(b"C=bad\x1bvalue\x00")


def _gamescope_fixture(tmp_path: Path, display: str = ":7") -> tuple[Path, Path, Path, socket.socket]:
    home = tmp_path / "home"
    home.mkdir()
    runtime_root = tmp_path / "run-user"
    runtime = runtime_root / str(home.stat().st_uid)
    runtime.mkdir(parents=True, mode=0o700)
    (runtime / "gamescope-environment").write_text(
        f"XDG_CURRENT_DESKTOP=gamescope\nDISPLAY={display}\n",
        encoding="utf-8",
    )
    x11_root = tmp_path / ".X11-unix"
    x11_root.mkdir()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(x11_root / f"X{int(display[1:])}"))
    return home, runtime_root, x11_root, listener


@pytest.mark.skipif(os.name != "posix", reason="Gamescope display uses a POSIX Unix socket")
def test_resolve_game_mode_display_requires_the_exact_owned_gamescope_socket(tmp_path: Path):
    home, runtime_root, x11_root, listener = _gamescope_fixture(tmp_path)
    try:
        assert resolve_game_mode_display(home, runtime_root=runtime_root, x11_root=x11_root) == ":7"
    finally:
        listener.close()


@pytest.mark.skipif(os.name != "posix", reason="Gamescope display uses a POSIX Unix socket")
def test_resolve_game_mode_display_rejects_ambiguous_or_unbacked_values(tmp_path: Path):
    home, runtime_root, x11_root, listener = _gamescope_fixture(tmp_path)
    environment = runtime_root / str(home.stat().st_uid) / "gamescope-environment"
    try:
        environment.write_text("DISPLAY=:7\nDISPLAY=:8\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing or ambiguous"):
            resolve_game_mode_display(home, runtime_root=runtime_root, x11_root=x11_root)
        environment.write_text("DISPLAY=:8\n", encoding="utf-8")
        with pytest.raises(ValueError, match="socket is unavailable"):
            resolve_game_mode_display(home, runtime_root=runtime_root, x11_root=x11_root)
    finally:
        listener.close()


def test_observe_game_container_reports_the_running_game_environment(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 10, {"SteamAppId": "1", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"})
    _proc(
        proc,
        20,
        {
            "SteamGameId": "220",
            "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220",
            "STEAM_COMPAT_CLIENT_INSTALL_PATH": "/home/deck/.steam/steam",
            "STEAM_COMPAT_TOOL_PATHS": "/lib/common/Proton 10:/lib/common/SteamLinuxRuntime",
        },
    )
    _proc(proc, 30, {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"})

    observation = observe_game_container(220, proc_root=proc)
    assert observation.running is True
    assert observation.pids == (20, 30)
    assert observation.compat_data_path == "/lib/compatdata/220"
    assert observation.steam_client_install_path == "/home/deck/.steam/steam"
    assert observation.compat_tool_paths == ("/lib/common/Proton 10", "/lib/common/SteamLinuxRuntime")
    assert observation.reason is None


def test_observe_game_container_refuses_a_truncated_matching_pid_set(tmp_path: Path, monkeypatch):
    import ce_decky.ce_launch as launch_module

    proc = tmp_path / "proc"
    for pid in (10, 20, 30):
        _proc(proc, pid, {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"})
    monkeypatch.setattr(launch_module, "MAX_OBSERVED_PIDS", 2)

    observation = observe_game_container(220, proc_root=proc)
    assert observation.running is False
    assert observation.pids == ()
    assert observation.reason == "the running game's matching process set exceeds the bounded PID limit"


def test_baseline_game_pid_liveness_keeps_unreadable_identity_ambiguous(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 10, {"SteamGameId": "220"})
    (proc / "20").mkdir(parents=True)
    _proc(proc, 30, {"SteamAppId": "999"})

    assert game_process_state(10, 220, proc_root=proc) == "matched"
    assert game_process_state(20, 220, proc_root=proc) == "unreadable"
    assert game_process_state(30, 220, proc_root=proc) == "gone_or_reused"
    assert game_process_state(40, 220, proc_root=proc) == "gone_or_reused"


def test_observe_game_container_lists_the_running_windows_executables(tmp_path: Path):
    # A table usually names no process, and the library entry points at the
    # launcher rather than the executable that owns the game's memory, so the
    # user has to be offered what the game is really running.
    proc = tmp_path / "proc"
    environ = {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"}
    _proc(proc, 10, environ, argv=["/lib/Steam/reaper", "SteamLaunch", "AppId=220"])
    _proc(proc, 20, environ, argv=["Z:\\home\\deck\\Games\\Demo\\Launcher_Steam.exe"])
    _proc(proc, 30, environ, argv=["Z:\\home\\deck\\Games\\Demo\\Bin\\Game-Win64-Shipping.exe", "Demo"])
    # Wine reports the same client more than once; a duplicate must not become a
    # second identical controller choice.
    _proc(proc, 40, environ, argv=["z:\\home\\deck\\games\\demo\\bin\\Game-Win64-Shipping.exe"])
    # Everything Proton installs into the prefix runs from its Windows directory,
    # so the choice stays clean without knowing each program's name.
    _proc(proc, 50, environ, argv=["c:\\windows\\system32\\services.exe"])
    _proc(proc, 55, environ, argv=["C:\\windows\\syswow64\\rpcss.exe"])
    _proc(proc, 56, environ, argv=["c:\\windows\\system32\\steam.exe", "/home/deck/Games/Demo/Launcher_Steam.exe"])
    # A game directory that merely contains "windows" is not the prefix's own.
    _proc(proc, 57, environ, argv=["Z:\\home\\deck\\Games\\windows-demo\\Extra.exe"])
    _proc(proc, 60, {"SteamAppId": "9", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/9"}, argv=["Z:\\other\\Other.exe"])

    observation = observe_game_container(220, proc_root=proc)
    assert observation.windows_executables == ("Extra.exe", "Game-Win64-Shipping.exe", "Launcher_Steam.exe")


def test_observe_game_container_reads_the_proton_launch_target(tmp_path: Path):
    """Steam launches a game as `<tool>/proton <verb> <exe>`, for library entries too.

    The argument vector is the exact shape observed on the target for the
    reviewed non-Steam entry; a Steam library entry is launched the same way,
    which is the only place its launch executable is observable at all.
    """
    proc = tmp_path / "proc"
    environ = {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"}
    _proc(proc, 10, environ, argv=[
        "/lib/Steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=220", "--",
        "/lib/Steam/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point",
        "--verb=waitforexitandrun", "--",
        "/lib/Steam/compatibilitytools.d/GE-Proton11-2/proton", "waitforexitandrun",
        "/home/deck/Games/Demo/Launcher_Steam.exe",
    ])
    _proc(proc, 20, environ, argv=["Z:\\home\\deck\\Games\\Demo\\Bin\\Game-Win64-Shipping.exe"])

    observation = observe_game_container(220, proc_root=proc)
    assert observation.launch_executable == "Launcher_Steam.exe"
    # The launcher itself has already exited here, so it is a ranking hint only
    # and never becomes a candidate the user could pick.
    assert observation.windows_executables == ("Game-Win64-Shipping.exe",)


def test_observe_game_container_reports_no_launch_target_without_a_proton_verb(tmp_path: Path):
    proc = tmp_path / "proc"
    environ = {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"}
    # A path that merely ends in proton, and a verb that launches nothing.
    _proc(proc, 10, environ, argv=["/opt/proton", "getcompatpath", "/home/deck/Games/Demo/Game.exe"])
    _proc(proc, 20, environ, argv=["/lib/tools/proton"])

    observation = observe_game_container(220, proc_root=proc)
    assert observation.launch_executable is None


def test_observe_game_container_reports_no_windows_executable_without_cmdline(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 10, {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"})

    observation = observe_game_container(220, proc_root=proc)
    assert observation.running is True
    assert observation.windows_executables == ()


def test_observe_game_container_refuses_conflicting_compatibility_paths(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 11, {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib-a/compatdata/220"})
    _proc(proc, 12, {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib-b/compatdata/220"})

    observation = observe_game_container(220, proc_root=proc)
    assert observation.running is True
    assert observation.compat_data_path is None
    assert len(observation.conflicting_compat_data_paths) == 2
    assert observation.reason is not None


def test_observe_game_container_reports_a_missing_game_without_raising(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 9, {"SteamAppId": "7"})
    observation = observe_game_container(220, proc_root=proc)
    assert observation.running is False
    assert observation.compat_data_path is None
    assert observation.reason == "no running process reports this Steam AppID"


def test_observe_game_container_fails_closed_when_the_bounded_scan_truncates(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 1, {"SteamAppId": "7"})
    _proc(proc, 2, {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/compat/220"})
    observation = observe_game_container(220, proc_root=proc, max_processes=1)
    assert observation.running is False
    assert observation.reason == "process table exceeds the bounded scan limit"


def test_resolve_compat_data_returns_one_exact_prefix(tmp_path: Path):
    home = _steam_home(tmp_path, app_id=220)
    resolution = resolve_compat_data(220, home)
    assert resolution.state == "resolved"
    assert resolution.compat_data_path is not None
    assert resolution.compat_data_path.endswith(os.path.join("compatdata", "220"))


def test_resolve_compat_data_refuses_an_ambiguous_second_library(tmp_path: Path):
    second = tmp_path / "second-library"
    (second / "steamapps/compatdata/220/pfx").mkdir(parents=True)
    home = _steam_home(tmp_path, app_id=220, libraries=[second])
    resolution = resolve_compat_data(220, home)
    assert resolution.state == "ambiguous"
    assert resolution.compat_data_path is None
    assert len(resolution.candidates) == 2


def test_resolve_compat_data_reports_a_missing_prefix(tmp_path: Path):
    home = _steam_home(tmp_path, app_id=220)
    assert resolve_compat_data(4711, home).state == "missing"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink semantics")
def test_resolve_compat_data_rejects_a_symlinked_prefix_component(tmp_path: Path):
    home = _steam_home(tmp_path, app_id=220)
    app_root = home / ".local/share/Steam/steamapps/compatdata/4711"
    app_root.mkdir(parents=True)
    (app_root / "pfx").symlink_to(tmp_path)
    resolution = resolve_compat_data(4711, home)
    assert resolution.state == "unsafe"
    assert resolution.compat_data_path is None


@pytest.mark.skipif(os.name != "posix", reason="Wine Z: mapping requires POSIX absolute paths")
def test_prepare_self_test_session_writes_a_parsable_bounded_descriptor(tmp_path: Path):
    root = tmp_path / "self-test"
    session = prepare_self_test_session(
        root, ce_sha256="c" * 64, target_process="cheatengine-x86_64.exe", boundary=tmp_path
    )
    descriptor = parse_descriptor(Path(session.descriptor_path).read_bytes())
    assert descriptor.session_id == session.session_id
    assert descriptor.target_process == "cheatengine-x86_64.exe"
    assert descriptor.startup == ()
    assert descriptor.table_path.startswith("Z:\\")
    assert Path(session.table_path).read_bytes() == SELF_TEST_TABLE
    assert Path(session.root).is_dir()


def test_prepare_self_test_session_rejects_a_non_executable_target(tmp_path: Path):
    with pytest.raises(ValueError, match="basename"):
        prepare_self_test_session(tmp_path, ce_sha256="c" * 64, target_process="ce", boundary=tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="Wine Z: mapping requires POSIX absolute paths")
def test_prepare_self_test_session_carries_an_exact_table_and_its_own_digest(tmp_path: Path):
    """An exact table replaces the synthetic one, descriptor digest and all.

    The development probe puts a real `.CT` in front of Cheat Engine to ask
    whether it opens at all. That only means anything if the bytes reach the
    session unchanged and the descriptor names their digest rather than the
    synthetic table's, because the bridge refuses a descriptor whose table
    digest does not match what it was handed.
    """
    blob = b'<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>'
    session = prepare_self_test_session(
        tmp_path / "self-test", ce_sha256="c" * 64, target_process="cheatengine-x86_64.exe",
        boundary=tmp_path, table_bytes=blob,
    )
    assert Path(session.table_path).read_bytes() == blob
    assert session.table_sha256 == hashlib.sha256(blob).hexdigest()
    assert parse_descriptor(Path(session.descriptor_path).read_bytes()).table_sha256 == session.table_sha256


def test_prepare_self_test_session_refuses_an_empty_table(tmp_path: Path):
    with pytest.raises(ValueError, match="must not be empty"):
        prepare_self_test_session(
            tmp_path, ce_sha256="c" * 64, target_process="cheatengine-x86_64.exe",
            boundary=tmp_path, table_bytes=b"",
        )


def _plan(tmp_path: Path, mode: str = MODE_SELF_TEST, app_id: int | None = None):
    return plan_ce_launch(
        mode=mode,
        tool=_tool(tmp_path / "Proton 10"),
        executable=tmp_path / "ce" / "cheatengine-x86_64.exe",
        ce_sha256="c" * 64,
        compat_data_path=tmp_path / "prefix",
        steam_client_install_path=tmp_path / "steam",
        descriptor_path=tmp_path / "session" / "descriptor.txt",
        descriptor_sha256="d" * 64,
        descriptor_md5="e" * 32,
        descriptor_windows_path="Z:\\session\\descriptor.txt",
        table_windows_path="Z:\\session\\table.ct",
        table_sha256="b" * 64,
        session_id="123e4567-e89b-42d3-a456-426614174000",
        app_id=app_id,
        display=":0",
    )


def test_plan_ce_launch_uses_the_documented_proton_verb_for_each_mode(tmp_path: Path):
    plan = _plan(tmp_path)
    assert plan.argv[0].endswith("proton")
    # The self-test owns its prefix, so it may use the full-setup verb.
    assert plan.argv[1] == "run"
    assert plan.verb == "run"
    assert plan.argv[2].endswith("cheatengine-x86_64.exe")
    # The table is deliberately not on the command line: Cheat Engine opens one
    # from there only once its main window is shown, and CE Decky never lets
    # that window map. The bridge loads the exact table from the descriptor.
    assert len(plan.argv) == 3
    assert plan.table_windows_path == "Z:\\session\\table.ct"

    # An attached launch must never rewrite the running game's prefix files.
    attached = _plan(tmp_path, MODE_ATTACHED, 220)
    assert attached.argv[1] == "runinprefix"
    assert attached.verb == "runinprefix"


def test_plan_ce_launch_publishes_exact_owned_environment_slots(tmp_path: Path):
    overrides = dict(_plan(tmp_path, MODE_ATTACHED, 220).env_overrides)
    assert overrides["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "prefix")
    assert overrides["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(tmp_path / "steam")
    assert overrides["SteamAppId"] == "220"
    assert overrides["SteamGameId"] == "220"
    assert overrides["DISPLAY"] == ":0"
    assert overrides["CE_DECKY_DESCRIPTOR"] == "Z:\\session\\descriptor.txt"
    assert overrides["CE_DECKY_DESCRIPTOR_SHA256"] == "d" * 64
    assert overrides["CE_DECKY_DESCRIPTOR_MD5"] == "e" * 32


def test_plan_ce_launch_keeps_the_self_test_free_of_a_library_identity(tmp_path: Path):
    overrides = dict(_plan(tmp_path).env_overrides)
    assert overrides["SteamAppId"] == "0"
    assert overrides["SteamGameId"] == "0"
    with pytest.raises(ValueError, match="must not carry a library AppID"):
        _plan(tmp_path, MODE_SELF_TEST, 220)
    with pytest.raises(ValueError, match="requires an exact AppID"):
        _plan(tmp_path, MODE_ATTACHED, None)


def test_plan_ce_launch_rejects_paths_that_are_not_absolute_or_not_wine_z(tmp_path: Path):
    with pytest.raises(ValueError, match="absolute"):
        plan_ce_launch(
            mode=MODE_SELF_TEST,
            tool=_tool(Path("relative-proton")),
            executable=tmp_path / "ce.exe",
            ce_sha256="c" * 64,
            compat_data_path=tmp_path / "prefix",
            steam_client_install_path=tmp_path / "steam",
            descriptor_path=tmp_path / "descriptor.txt",
            descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32,
            descriptor_windows_path="Z:\\descriptor.txt",
            table_windows_path="Z:\\table.ct",
            table_sha256="b" * 64,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            app_id=None,
            display=":0",
        )
    with pytest.raises(ValueError, match="Wine Z: paths"):
        plan_ce_launch(
            mode=MODE_SELF_TEST,
            tool=_tool(tmp_path / "Proton 10"),
            executable=tmp_path / "ce.exe",
            ce_sha256="c" * 64,
            compat_data_path=tmp_path / "prefix",
            steam_client_install_path=tmp_path / "steam",
            descriptor_path=tmp_path / "descriptor.txt",
            descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32,
            descriptor_windows_path="/session/descriptor.txt",
            table_windows_path="Z:\\table.ct",
            table_sha256="b" * 64,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            app_id=None,
            display=":0",
        )


def test_launch_environment_drops_inherited_proton_wine_and_steam_variables(tmp_path: Path):
    plan = _plan(tmp_path, MODE_ATTACHED, 220)
    env = launch_environment(
        plan,
        {
            "HOME": "/home/deck",
            "WINEPREFIX": "/somewhere/else",
            "WINEDLLOVERRIDES": "a=b",
            "PROTON_LOG": "1",
            "STEAM_COMPAT_DATA_PATH": "/wrong",
            "SteamAppId": "999",
            "CE_DECKY_DESCRIPTOR": "Z:\\stale\\descriptor.txt",
        },
    )
    assert env["HOME"] == "/home/deck"
    assert "WINEDLLOVERRIDES" not in env
    assert env["WINEPREFIX"] if False else "WINEPREFIX" not in env
    assert env["STEAM_COMPAT_DATA_PATH"] == str(tmp_path / "prefix")
    assert env["SteamAppId"] == "220"
    assert env["CE_DECKY_DESCRIPTOR"] == "Z:\\session\\descriptor.txt"
    assert env["PROTON_LOG"] == "0"


def test_match_observed_proton_requires_exactly_one_installed_match(tmp_path: Path):
    proc = tmp_path / "proc"
    first = "/lib/common/Proton 10"
    second = "/lib/common/Proton 9"
    _proc(
        proc,
        50,
        {
            "SteamAppId": "220",
            "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220",
            "STEAM_COMPAT_TOOL_PATHS": f"{first}:/lib/common/SteamLinuxRuntime",
        },
    )
    observation = observe_game_container(220, proc_root=proc)
    tools = [_tool(Path(first), "Proton 10"), ProtonTool("f" * 64, "Proton 9", second, "0" * 64, "steam-library")]

    matched, reason = match_observed_proton(observation, tools)
    assert reason is None
    assert matched is not None and matched.name == "Proton 10"

    unmatched, unmatched_reason = match_observed_proton(observation, [tools[1]])
    assert unmatched is None
    assert unmatched_reason is not None


def test_match_observed_proton_reports_a_game_without_tool_paths(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 60, {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"})
    observation = observe_game_container(220, proc_root=proc)
    matched, reason = match_observed_proton(observation, [_tool(tmp_path / "Proton 10")])
    assert matched is None
    assert reason == "the running game does not expose its compatibility tool paths"


def test_a_native_linux_game_is_named_as_one_instead_of_a_missing_proton(tmp_path: Path):
    """The Steam Linux Runtime is a compatibility tool, and it is not Proton.

    A native build lands in the same branch as a Proton install CE Decky cannot
    resolve, and "no installed Proton tool matches" sends the user looking for a
    broken Proton. There is no Windows process here at all, and no Proton would
    create one; only Steam installing the game's Windows build will.
    """
    proc = tmp_path / "proc"
    _proc(
        proc,
        70,
        {
            "SteamAppId": "220",
            "STEAM_COMPAT_TOOL_PATHS": "/lib/common/SteamLinuxRuntime_soldier",
        },
    )
    observation = observe_game_container(220, proc_root=proc)
    matched, reason = match_observed_proton(observation, [_tool(tmp_path / "Proton 10")])

    assert matched is None
    assert reason is not None
    assert "native Linux build" in reason
    assert "Force a Proton compatibility tool" in reason


def test_an_unresolvable_proton_is_still_reported_as_one(tmp_path: Path):
    # The same branch, with a prefix and a Windows process: this really is a
    # Proton game whose tool CE Decky cannot account for, and telling that user
    # to install a Windows build would be nonsense.
    proc = tmp_path / "proc"
    _proc(
        proc,
        71,
        {
            "SteamAppId": "220",
            "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220",
            "WINEPREFIX": "/lib/compatdata/220/pfx",
            "STEAM_COMPAT_TOOL_PATHS": "/lib/common/Proton Unregistered",
        },
        argv=["Z:\\games\\game.exe"],
    )
    observation = observe_game_container(220, proc_root=proc)
    matched, reason = match_observed_proton(observation, [_tool(tmp_path / "Proton 10")])

    assert matched is None
    assert reason == "no installed Proton tool matches the running game's compatibility tool paths"


def test_attached_launch_cannot_use_a_selected_tool_when_the_game_tool_is_unobservable(tmp_path: Path, monkeypatch):
    supervisor, executable = _supervisor(tmp_path)
    tool = _tool(tmp_path / "Proton 10")
    monkeypatch.setattr(
        "ce_decky.ce_launch.observe_game_container",
        lambda app_id: GameContainerObservation(
            app_id, True, (42,), "/steam/compatdata/220", "/steam", None, ":1", None, (), (), (), (), (), (), 1,
            "the running game does not expose its compatibility tool paths",
        ),
    )

    async def exercise():
        await supervisor.start_attached(
            [tool], executable, app_id=220, tool_id=tool.tool_id,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            descriptor_path=tmp_path / "descriptor.txt", descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32, descriptor_windows_path="Z:\\descriptor.txt",
            table_windows_path="Z:\\table.ct", table_sha256="b" * 64,
            ce_sha256="c" * 64, status_path=tmp_path / "status.txt",
        )

    with pytest.raises(ValueError, match="does not expose its compatibility tool paths"):
        asyncio.run(exercise())


def test_attached_launch_fails_closed_when_the_game_wine_prefix_is_unobservable(tmp_path: Path, monkeypatch):
    supervisor, executable = _supervisor(tmp_path)
    steam_root = supervisor.user_home / ".local/share/Steam"
    tool_root = steam_root / "steamapps/common/Proton 10"
    compat = steam_root / "steamapps/compatdata/220"
    tool_root.mkdir(parents=True, exist_ok=True)
    compat.mkdir(parents=True, exist_ok=True)
    tool = _tool(tool_root)
    monkeypatch.setattr(
        "ce_decky.ce_launch.observe_game_container",
        lambda app_id: GameContainerObservation(
            app_id=app_id,
            running=True,
            pids=(42,),
            compat_data_path=str(compat),
            steam_client_install_path=str(steam_root),
            wine_prefix=None,
            display=":1",
            launch_executable="game.exe",
            compat_tool_paths=(str(tool_root),),
            windows_executables=("game.exe",),
            conflicting_compat_data_paths=(),
            conflicting_steam_client_install_paths=(),
            conflicting_wine_prefixes=(),
            conflicting_displays=(),
            scanned=1,
            reason=None,
        ),
    )
    monkeypatch.setattr(
        "ce_decky.ce_launch.resolve_compat_data",
        lambda app_id, user_home: CompatDataResolution(app_id, "resolved", str(compat), (str(compat),), ()),
    )

    async def exercise():
        await supervisor.start_attached(
            [tool], executable, app_id=220, tool_id=tool.tool_id,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            descriptor_path=tmp_path / "descriptor.txt", descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32, descriptor_windows_path="Z:\\descriptor.txt",
            table_windows_path="Z:\\table.ct", table_sha256="b" * 64,
            ce_sha256="c" * 64, status_path=tmp_path / "status.txt",
        )

    with pytest.raises(ValueError, match="does not expose its Wine prefix"):
        asyncio.run(exercise())


# -- owned process behaviour ------------------------------------------------

_FAKE_PROTON = '''#!{python}
"""Stand in for an installed Proton script.

It accepts only the reviewed `runinprefix` verb, then answers as the resident
bridge would by writing one exact status file for the descriptor it was given.
"""
import os
import sys
import time

sys.path.insert(0, {py_modules!r})

from ce_decky.session_protocol import RuntimeStatus, parse_descriptor, render_status

assert sys.argv[1] in ("run", "runinprefix"), sys.argv
assert os.environ["STEAM_COMPAT_DATA_PATH"]
os.makedirs(os.path.join(os.environ["STEAM_COMPAT_DATA_PATH"], "pfx"), exist_ok=True)

def host(value):
    return value[2:].replace("\\\\", "/")

descriptor = parse_descriptor(open(host(os.environ["CE_DECKY_DESCRIPTOR"]), "rb").read())
if {answer!r}:
    status = RuntimeStatus(
        descriptor.session_id, descriptor.app_id, descriptor.ce_sha256, descriptor.table_sha256,
        os.environ["CE_DECKY_DESCRIPTOR_SHA256"], 1234, True, descriptor.target_process, 4321, (), ((7, "game.exe"),),
    )
    open(host(descriptor.status_path), "wb").write(render_status(status))
time.sleep({sleep})
'''


def _install_fake_proton(root: Path, *, answers: bool = True, sleep: float = 30.0) -> ProtonTool:
    root.mkdir(parents=True, exist_ok=True)
    script = root / "proton"
    script.write_text(
        _FAKE_PROTON.format(
            python=sys.executable,
            py_modules=str(Path(__file__).resolve().parents[1] / "py_modules"),
            answer=answers,
            sleep=sleep,
        ),
        encoding="utf-8",
    )
    os.chmod(script, 0o755)
    wineserver = root / "files/bin/wineserver"
    wineserver.parent.mkdir(parents=True)
    wineserver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(wineserver, 0o755)
    resolved = root.resolve(strict=True)
    script_sha = sha256(script.read_bytes()).hexdigest()
    tool_id = sha256((str(resolved) + "\0" + script_sha).encode("utf-8")).hexdigest()
    return ProtonTool(tool_id, root.name, str(resolved), script_sha, "test")


def test_launch_plan_rejects_a_windows_path_longer_than_legacy_createprocess_support(tmp_path: Path):
    tool = _tool(tmp_path / "Proton 10")
    executable = tmp_path / ("x" * 220) / "cheatengine-x86_64.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"MZ")
    with pytest.raises(ValueError, match="executable path exceeds"):
        plan_ce_launch(
            mode=MODE_SELF_TEST,
            tool=tool,
            executable=executable,
            ce_sha256="c" * 64,
            compat_data_path=tmp_path / "prefix",
            steam_client_install_path=tmp_path / "steam",
            descriptor_path=tmp_path / "descriptor.txt",
            descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32,
            descriptor_windows_path="Z:\\descriptor.txt",
            table_windows_path="Z:\\table.ct",
            table_sha256="b" * 64,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            app_id=None,
            display=":0",
        )


def _supervisor(tmp_path: Path) -> tuple[CELaunchSupervisor, Path]:
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    (paths.user_home / ".local/share/Steam").mkdir(parents=True, exist_ok=True)
    executable = paths.ce_root / "runtime" / "cheatengine-x86_64.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"MZ")
    supervisor = CELaunchSupervisor(
        paths.user_home,
        paths.ce_root,
        paths.state_root,
        logging.getLogger("ce-launch"),
        display_resolver=lambda _home: ":0",
    )
    return supervisor, executable


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group launch")
def test_self_test_launch_requires_the_resident_bridge_and_then_stops_the_process(tmp_path: Path):
    supervisor, executable = _supervisor(tmp_path)
    tool = _install_fake_proton(tmp_path / "Proton 10")

    async def exercise():
        started = await supervisor.start_self_test(tool, executable, "c" * 64)
        operation_id = str(started["operation_id"])
        for _ in range(200):
            current = supervisor.status(operation_id)
            if current["state"] in {"stopped", "failed", "cancelled"}:
                return current
            await asyncio.sleep(0.05)
        raise AssertionError("self-test did not finish")

    result = asyncio.run(exercise())
    assert result["state"] == "stopped", result
    assert result["bridge"] is not None
    assert result["bridge"]["attached"] is True
    assert result["bridge"]["process_count"] == 1
    assert result["plan"]["argv"][1] == "run"


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group launch")
def test_self_test_launch_fails_when_the_bridge_never_answers(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("ce_decky.ce_launch.SELF_TEST_TIMEOUT_SECONDS", 1.0)
    supervisor, executable = _supervisor(tmp_path)
    tool = _install_fake_proton(tmp_path / "Proton 10", answers=False, sleep=0.1)

    async def exercise():
        started = await supervisor.start_self_test(tool, executable, "c" * 64)
        operation_id = str(started["operation_id"])
        for _ in range(200):
            current = supervisor.status(operation_id)
            if current["state"] in {"stopped", "failed", "cancelled"}:
                return current
            await asyncio.sleep(0.05)
        raise AssertionError("self-test did not finish")

    result = asyncio.run(exercise())
    assert result["state"] == "failed"
    assert result["bridge"] is None
    assert "bridge" in str(result["error"]).lower()


def test_closing_the_supervisor_retires_self_tests_even_when_close_is_cancelled(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    stopped: list[str] = []

    async def exercise():
        supervisor._operations["op-self-test"] = {"mode": MODE_SELF_TEST}
        running = asyncio.Event()

        async def supervising():
            running.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # A real supervising task needs a moment to unwind its launch.
                await asyncio.sleep(0.05)
                raise

        supervisor._tasks["op-self-test"] = asyncio.create_task(supervising())
        await running.wait()

        async def stop_process(operation_id: str) -> None:
            stopped.append(operation_id)

        monkeypatch.setattr(supervisor, "_stop_process", stop_process)
        closing = asyncio.create_task(supervisor.close())
        await asyncio.sleep(0)
        # A self-test is a transaction: however often unload is cancelled, the
        # Cheat Engine process CE Decky started must still be retired.
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        with pytest.raises(asyncio.CancelledError):
            await closing

    asyncio.run(exercise())
    assert stopped == ["op-self-test"]
    assert supervisor._tasks == {}


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group launch")
def test_closing_the_supervisor_retires_every_owned_process(tmp_path: Path):
    supervisor, executable = _supervisor(tmp_path)
    tool = _install_fake_proton(tmp_path / "Proton 10")

    async def exercise():
        started = await supervisor.start_self_test(tool, executable, "c" * 64)
        await asyncio.sleep(0.5)
        await supervisor.close()
        return supervisor.status(str(started["operation_id"]))

    result = asyncio.run(exercise())
    assert result["state"] in {"stopped", "cancelled", "failed"}
    assert supervisor._processes == {}


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group launch")
def test_attached_launch_refuses_a_game_that_is_not_running(tmp_path: Path, monkeypatch):
    supervisor, executable = _supervisor(tmp_path)
    tool = _install_fake_proton(tmp_path / "Proton 10")
    # State the premise instead of reading whatever this machine is running: on
    # a development device AppID 220 may genuinely be playing, and then the
    # refusal under test never happens.
    monkeypatch.setattr(
        "ce_decky.ce_launch.observe_game_container",
        lambda app_id: GameContainerObservation(
            app_id, False, (), None, None, None, None, None, (), (), (), (), (), (), 0,
            "no running process reports this Steam AppID",
        ),
    )

    async def exercise():
        await supervisor.start_attached(
            [tool],
            executable,
            app_id=220,
            tool_id=tool.tool_id,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            descriptor_path=tmp_path / "descriptor.txt",
            descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32,
            descriptor_windows_path="Z:\\descriptor.txt",
            table_windows_path="Z:\\table.ct",
            table_sha256="b" * 64,
            ce_sha256="c" * 64,
            status_path=tmp_path / "status.txt",
        )

    with pytest.raises(ValueError, match="not running"):
        asyncio.run(exercise())


def test_discover_proton_tools_still_reads_declared_extra_libraries(tmp_path: Path):
    second = tmp_path / "second-library"
    (second / "steamapps/common/Proton 10").mkdir(parents=True)
    script = second / "steamapps/common/Proton 10/proton"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    os.chmod(script, 0o755)
    home = _steam_home(tmp_path, app_id=220, libraries=[second])
    tools = discover_proton_tools(home)
    assert [tool.name for tool in tools] == ["Proton 10"]


def test_module_documents_the_reused_proton_verb():
    import ce_decky.ce_launch as module

    documentation = textwrap.dedent(module.__doc__ or "")
    assert "runinprefix" in documentation
    assert "PROTON_REMOTE_DEBUG_CMD" in documentation


# -- owned launch recovery across a plugin reload ---------------------------


def _record(supervisor: CELaunchSupervisor, app_id: int, pid: int, descriptor: str, digest: str) -> None:
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    (supervisor.launch_record_root / f"{app_id}.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "app_id": app_id,
                "session_id": "123e4567-e89b-42d3-a456-426614174000",
                "pid": pid,
                "pgid": pid,
                "tool_id": "a" * 64,
                "executable": str((supervisor.ce_root / "runtime/cheatengine-x86_64.exe").absolute()),
                "descriptor_windows_path": descriptor,
                "descriptor_sha256": digest,
                "started_at": 1.0,
            }
        ),
        encoding="utf-8",
    )


def test_owned_process_matches_requires_the_exact_descriptor_identity(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 4242, {"CE_DECKY_DESCRIPTOR": "Z:\\s\\descriptor.txt", "CE_DECKY_DESCRIPTOR_SHA256": "d" * 64})
    assert owned_process_matches(4242, "Z:\\s\\descriptor.txt", "D" * 64, proc_root=proc) is True
    assert owned_process_matches(4242, "Z:\\other\\descriptor.txt", "d" * 64, proc_root=proc) is False
    assert owned_process_matches(4242, "Z:\\s\\descriptor.txt", "e" * 64, proc_root=proc) is False
    assert owned_process_matches(9999, "Z:\\s\\descriptor.txt", "d" * 64, proc_root=proc) is False
    assert owned_process_matches(1, "Z:\\s\\descriptor.txt", "d" * 64, proc_root=proc) is False


def test_owned_process_state_keeps_an_existing_but_unreadable_pid_ambiguous(tmp_path: Path):
    proc = tmp_path / "proc"
    (proc / "4242").mkdir(parents=True)
    assert owned_process_state(4242, "Z:\\s\\descriptor.txt", "d" * 64, proc_root=proc) == "unreadable"
    assert owned_process_state(9999, "Z:\\s\\descriptor.txt", "d" * 64, proc_root=proc) == "gone_or_reused"


def test_inspecting_a_proven_gone_launch_is_read_only(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "gone_or_reused")
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)

    observed = supervisor.inspect_owned_launch_record(220)

    assert observed["state"] == "gone_or_reused"
    assert observed["record"]["session_id"] == "123e4567-e89b-42d3-a456-426614174000"
    assert (supervisor.launch_record_root / "220.json").is_file()
    assert supervisor.confirmed_exit_epoch(220, "123e4567-e89b-42d3-a456-426614174000") is None


def test_recovering_a_launch_whose_process_is_gone_records_a_confirmed_exit(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "gone_or_reused")
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)

    assert supervisor.recover_owned_launch(220) is None
    assert supervisor.confirmed_exit_epoch(220, "123e4567-e89b-42d3-a456-426614174000") is not None
    assert not (supervisor.launch_record_root / "220.json").exists()


def test_launcher_scope_liveness_covers_every_owned_launch_record(tmp_path: Path, monkeypatch):
    """The private runtime tree is shared, so rebuilding it asks at launcher scope."""
    supervisor, _ = _supervisor(tmp_path)
    assert supervisor.has_live_owned_launch() is False

    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "gone_or_reused")
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)
    assert supervisor.has_live_owned_launch() is False

    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "matched")
    assert supervisor.has_live_owned_launch() is True


class _ExitedProcess:
    """A launcher this supervisor started that has already exited."""

    returncode = 0


class _RunningProcess:
    returncode = None


def test_a_refusal_to_start_says_which_press_clears_it(tmp_path: Path, monkeypatch):
    # These refusals reach the user as a notification and nothing else. "Stop it
    # before starting another Cheat Engine" describes the rule rather than the
    # move, and the move is not obvious from a panel where nothing looks to be
    # running: on the device it ended a test session.
    supervisor, _ = _supervisor(tmp_path)
    supervisor._processes["operation"] = _ExitedProcess()  # type: ignore[assignment]
    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_any_descriptor_pids", lambda **kwargs: ("unreadable", ())
    )

    with pytest.raises(ValueError, match="Press Stop CE"):
        supervisor._assert_launcher_is_free(220)

    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_any_descriptor_pids", lambda **kwargs: ("gone", ())
    )
    monkeypatch.setattr(type(supervisor), "ownership_state_error", lambda self: "records unreadable")
    with pytest.raises(ValueError, match="Advanced offers a repair"):
        supervisor._assert_launcher_is_free(220)


def test_a_stop_that_could_not_prove_absence_stops_owning_once_absence_is_proven(tmp_path: Path, monkeypatch):
    # A stop whose cleanup could not prove the identity gone keeps its process
    # entry. That is right at that moment and wrong forever after: the doubt
    # belonged to one instant, and holding it refused every later start with
    # "stop it before starting another Cheat Engine" for a Cheat Engine that
    # had already exited. Only a plugin reload cleared it.
    supervisor, _ = _supervisor(tmp_path)
    supervisor._processes["operation"] = _ExitedProcess()  # type: ignore[assignment]

    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_any_descriptor_pids", lambda **kwargs: ("matched", (4242,))
    )
    assert supervisor.has_live_owned_launch() is True
    assert "operation" in supervisor._processes

    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_any_descriptor_pids", lambda **kwargs: ("gone", ())
    )
    assert supervisor.has_live_owned_launch() is False
    # Released, so the next question does not have to prove it all over again.
    assert supervisor._processes == {}


def test_ownership_is_kept_while_absence_cannot_be_proven_or_the_launcher_still_runs(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    supervisor._processes["operation"] = _ExitedProcess()  # type: ignore[assignment]
    # An unreadable scan is not an absence proof.
    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_any_descriptor_pids", lambda **kwargs: ("unreadable", ())
    )
    assert supervisor.has_live_owned_launch() is True

    # A launcher that has not exited owns on its own authority, and nothing is
    # scanned for or released on its behalf.
    supervisor._processes["operation"] = _RunningProcess()  # type: ignore[assignment]

    def refuse(**kwargs):
        raise AssertionError("a running launcher must not be second-guessed by a scan")

    monkeypatch.setattr("ce_decky.ce_launch.owned_any_descriptor_pids", refuse)
    assert supervisor.has_live_owned_launch() is True
    assert "operation" in supervisor._processes


def test_launcher_scope_liveness_fails_closed_when_record_directory_is_unreadable(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)

    def unreadable(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", unreadable)
    assert supervisor.has_live_owned_launch() is True

    # An unreadable or malformed record must not read as "nothing is running".
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "unreadable")
    assert supervisor.has_live_owned_launch() is True
    (supervisor.launch_record_root / "220.json").write_text("{", encoding="utf-8")
    assert supervisor.has_live_owned_launch() is True


def test_recovering_a_live_launch_keeps_it_controllable_after_a_reload(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "matched")
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)

    recovered = supervisor.recover_owned_launch(220)
    assert recovered is not None
    assert recovered["pid"] == 4242
    assert recovered["session_id"] == "123e4567-e89b-42d3-a456-426614174000"
    assert supervisor.capability(220)["recovered"] == recovered


def test_unreadable_live_process_identity_retains_the_recovery_record(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "unreadable")
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)

    with pytest.raises(ValueError, match="cannot be re-proved"):
        supervisor.recover_owned_launch(220)
    assert (supervisor.launch_record_root / "220.json").is_file()
    assert supervisor.confirmed_exit_epoch(220, "123e4567-e89b-42d3-a456-426614174000") is None
    assert supervisor.capability(220)["recovery_error"] is not None


def test_a_malformed_or_foreign_launch_record_blocks_new_process_ownership(tmp_path: Path):
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    (supervisor.launch_record_root / "220.json").write_text(json.dumps({"schema": 1, "pid": 5}), encoding="utf-8")
    with pytest.raises(ValueError, match="present but invalid"):
        supervisor.recover_owned_launch(220)
    assert supervisor.capability(220)["recovery_error"] is not None

    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)
    payload = json.loads((supervisor.launch_record_root / "220.json").read_text(encoding="utf-8"))
    payload["app_id"] = 221
    (supervisor.launch_record_root / "220.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="present but invalid"):
        supervisor.recover_owned_launch(220)


def test_stopping_a_game_without_any_owned_launch_is_a_benign_no_op(tmp_path: Path):
    supervisor, _ = _supervisor(tmp_path)
    result = asyncio.run(supervisor.stop_for_app(220))
    # A stop asks the session to switch its own cheats off first; with no owned
    # launch there is no session to ask, and the answer says so.
    assert result == {"stopped": False, "operation": None, "recovered": False, "quiesce": None}


def test_failed_recovered_process_stop_retains_ownership_and_does_not_confirm_exit(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "matched")
    monkeypatch.setattr(supervisor, "_terminate_recovered", lambda record: False)
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)

    result = asyncio.run(supervisor.stop_for_app(220))

    assert result == {"stopped": False, "operation": None, "recovered": True, "quiesce": None}
    assert (supervisor.launch_record_root / "220.json").is_file()
    assert supervisor.confirmed_exit_epoch(220, "123e4567-e89b-42d3-a456-426614174000") is None


def test_attached_launch_record_persistence_failure_is_not_suppressed(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.atomic_write_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        supervisor._write_record(_plan(tmp_path, MODE_ATTACHED, 220), 4242)


def test_launch_heartbeat_must_match_every_exact_execution_identity(tmp_path: Path):
    status_path = tmp_path / "status.txt"
    status_path.write_bytes(render_status(RuntimeStatus(
        "123e4567-e89b-42d3-a456-426614174000", 220, "c" * 64, "b" * 64,
        "d" * 64, 1, True, "game.exe", 42, (), (),
    )))
    assert _read_bridge_status(
        status_path, "123e4567-e89b-42d3-a456-426614174000",
        "c" * 64, "b" * 64, "d" * 64,
    ) is not None
    assert _read_bridge_status(
        status_path, "123e4567-e89b-42d3-a456-426614174000",
        "c" * 64, "b" * 64, "e" * 64,
    ) is None


def test_a_bridge_that_is_only_starting_is_not_a_connected_session(tmp_path: Path, monkeypatch):
    # The bridge now publishes a heartbeat before it opens the table, so that a
    # Cheat Engine stopped inside one of its own modal forms is a reported state
    # instead of five minutes of silence. That heartbeat is liveness and not
    # readiness: nothing is attached and no table is open, so returning it would
    # report an unusable session as connected.
    supervisor, _ = _supervisor(tmp_path)
    plan = _plan(tmp_path, MODE_ATTACHED, 220)
    monkeypatch.setattr("ce_decky.ce_launch.SELF_TEST_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr("ce_decky.ce_launch.HEARTBEAT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        "ce_decky.ce_launch._read_bridge_status",
        lambda *args, **kwargs: {
            "bridge_phase": "starting", "table_load_state": "pending",
            "table_load_route": "prompted", "attached": False,
        },
    )

    class _Running:
        returncode = None

    observed, starting = asyncio.run(
        supervisor._await_bridge(_Running(), tmp_path / "status.txt", plan, 0)
    )

    assert observed is None
    assert starting is not None and starting["bridge_phase"] == "starting"
    # And the failure says where it stopped, rather than that nothing was heard.
    described = _describe_unfinished_bridge(starting)
    assert "never became ready" in described
    assert "still opening the session table" in described


def test_the_unfinished_bridge_message_stays_a_sentence():
    # `table_load_error` is deliberately allowed to carry a multi-line Lua
    # traceback of up to 4 KB, and this message becomes the launch failure the
    # user is shown in a Steam toast and in Review's failure row. The whole
    # traceback is in the status file and the support bundle, which is where it
    # is read from.
    described = _describe_unfinished_bridge({
        "bridge_phase": "starting",
        "table_load_state": "failed",
        "table_load_route": "approved",
        "table_load_error": "loadTable failed\nstack traceback:\n  [C]: in ?\n" + ("x" * 4000),
        "last_dialog": "Confirmation\nover two lines",
    })
    assert "\n" not in described
    assert len(described) < 600
    assert "stack traceback" in described
    assert "…" in described


def test_a_bridge_that_finishes_starting_is_the_connected_session(tmp_path: Path, monkeypatch):
    supervisor, _ = _supervisor(tmp_path)
    plan = _plan(tmp_path, MODE_ATTACHED, 220)
    monkeypatch.setattr("ce_decky.ce_launch.SELF_TEST_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr("ce_decky.ce_launch.HEARTBEAT_POLL_SECONDS", 0.01)
    answers = [
        {"bridge_phase": "starting", "table_load_state": "pending"},
        {"bridge_phase": "ready", "table_load_state": "loaded", "attached": True},
    ]
    monkeypatch.setattr(
        "ce_decky.ce_launch._read_bridge_status",
        lambda *args, **kwargs: answers.pop(0) if answers else {"bridge_phase": "ready"},
    )

    class _Running:
        returncode = None

    observed, starting = asyncio.run(
        supervisor._await_bridge(_Running(), tmp_path / "status.txt", plan, 0)
    )

    assert observed is not None and observed["bridge_phase"] == "ready"
    assert starting is not None and starting["bridge_phase"] == "starting"


def test_a_bridge_from_before_the_phase_contract_is_still_a_connected_session(tmp_path: Path, monkeypatch):
    # Every earlier bridge published nothing at all until it was ready, so a
    # heartbeat with no phase in it must not be waited on forever.
    supervisor, _ = _supervisor(tmp_path)
    plan = _plan(tmp_path, MODE_ATTACHED, 220)
    monkeypatch.setattr("ce_decky.ce_launch.SELF_TEST_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr("ce_decky.ce_launch.HEARTBEAT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        "ce_decky.ce_launch._read_bridge_status",
        lambda *args, **kwargs: {"attached": True, "table_load_state": "loaded"},
    )

    class _Running:
        returncode = None

    observed, starting = asyncio.run(
        supervisor._await_bridge(_Running(), tmp_path / "status.txt", plan, 0)
    )

    assert observed is not None
    assert starting is None


def test_the_bridge_phase_and_table_route_reach_the_launch_observation(tmp_path: Path):
    status_path = tmp_path / "status.txt"
    status_path.write_bytes(render_status(RuntimeStatus(
        "123e4567-e89b-42d3-a456-426614174000", 220, "c" * 64, "b" * 64,
        "d" * 64, 1, True, "game.exe", 42, (), (), 3,
        table_load_state="loaded", table_load_route="approved", bridge_phase="ready",
    )))
    observed = _read_bridge_status(
        status_path, "123e4567-e89b-42d3-a456-426614174000",
        "c" * 64, "b" * 64, "d" * 64,
    )
    assert observed is not None
    assert observed["bridge_phase"] == "ready"
    assert observed["table_load_route"] == "approved"


def test_launch_heartbeat_must_be_newer_than_its_process_lifetime(tmp_path: Path):
    status_path = tmp_path / "status.txt"
    status_path.write_bytes(render_status(RuntimeStatus(
        "123e4567-e89b-42d3-a456-426614174000", 220, "c" * 64, "b" * 64,
        "d" * 64, 1, False, "game.exe", 0, (), (),
    )))
    observed_mtime = status_path.stat().st_mtime_ns
    assert _read_bridge_status(
        status_path, "123e4567-e89b-42d3-a456-426614174000",
        "c" * 64, "b" * 64, "d" * 64, observed_mtime,
    ) is None


def test_attached_launch_refuses_to_start_a_second_cheat_engine_for_one_game(tmp_path: Path, monkeypatch):
    supervisor, executable = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "matched")
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)
    tool = _tool(Path("/lib/common/Proton 10"))

    async def exercise():
        await supervisor.start_attached(
            [tool],
            executable,
            app_id=220,
            tool_id=tool.tool_id,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            descriptor_path=tmp_path / "descriptor.txt",
            descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32,
            descriptor_windows_path="Z:\\s\\descriptor.txt",
            table_windows_path="Z:\\table.ct",
            table_sha256="b" * 64,
            ce_sha256="c" * 64,
            status_path=tmp_path / "status.txt",
        )

    with pytest.raises(ValueError, match="already running"):
        asyncio.run(exercise())


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="requires a real Linux procfs")
def test_observe_game_container_reads_real_procfs_records() -> None:
    """The observation must work against real procfs, not only fixtures.

    ``/proc/<pid>/environ`` reports a synthetic zero size while still yielding
    data. Reading it with the regular-file reader rejects every record, so this
    observation silently reported that no game was running on the real target.
    """
    app_id = 424242
    compat = "/tmp/ce-decky-procfs-observation-test"
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "SteamAppId": str(app_id),
        "SteamGameId": str(app_id),
        "STEAM_COMPAT_DATA_PATH": compat,
    }
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        observation = observe_game_container(app_id)
        while not observation.running and time.monotonic() < deadline:
            time.sleep(0.1)
            observation = observe_game_container(app_id)
        assert observation.running, observation.reason
        assert child.pid in observation.pids
        assert observation.compat_data_path == compat
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_running_app_observation_lists_only_steam_declared_app_ids(tmp_path: Path):
    proc = tmp_path / "proc"
    _proc(proc, 10, {"SteamAppId": "220"})
    _proc(proc, 20, {"SteamAppId": "220"})
    _proc(proc, 30, {"SteamAppId": "3086660526"})
    # A non-Steam shortcut carries a 64-bit gameid here, which is not an AppID.
    _proc(proc, 40, {"SteamGameId": "13257106013057712128"})
    _proc(proc, 50, {"HOME": "/home/deck"})

    observation = observe_running_app_ids(proc_root=proc)
    assert observation.available is True
    assert observation.app_ids == (220, 3086660526)
    assert observation.reason is None


def test_running_app_observation_refuses_a_truncated_process_table(tmp_path: Path):
    """A short list must never be mistaken for "that game is not running"."""
    proc = tmp_path / "proc"
    _proc(proc, 10, {"SteamAppId": "220"})
    _proc(proc, 20, {"SteamAppId": "440"})

    observation = observe_running_app_ids(proc_root=proc, max_processes=1)
    assert observation.available is False
    assert observation.app_ids == ()
    assert "bounded scan limit" in (observation.reason or "")


DESCRIPTOR = "Z:\\home\\deck\\.cheat-engine-decky\\state\\sessions\\10\\descriptor.txt"
DIGEST = "c" * 64


def test_owned_cheat_engine_is_found_after_it_leaves_the_recorded_process_group(tmp_path: Path):
    """Process-group scope cannot prove an owned Cheat Engine is gone.

    Proton starts the launcher in its own POSIX session, but Wine clients can
    leave that group, and one was observed doing exactly that on the target. A
    stop that only signalled the recorded group could report success while a
    matching Cheat Engine was still attached, after which switching table would
    start a second one beside it.
    """
    proc = tmp_path / "proc"
    proc.mkdir()
    # The launcher's group is empty; the escapee kept the exact descriptor.
    _proc(proc, 901, {"CE_DECKY_DESCRIPTOR": DESCRIPTOR, "CE_DECKY_DESCRIPTOR_SHA256": DIGEST})
    _proc(proc, 902, {"SteamAppId": "220"})

    state, pids = owned_descriptor_pids(DESCRIPTOR, DIGEST, proc_root=proc)
    assert state == "matched"
    assert pids == (901,)


def test_descriptor_sweep_reports_gone_only_from_a_complete_scan(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 901, {"SteamAppId": "220"})
    assert owned_descriptor_state(DESCRIPTOR, DIGEST, proc_root=proc) == "gone"

    # A different descriptor identity is not this session.
    _proc(proc, 902, {"CE_DECKY_DESCRIPTOR": DESCRIPTOR, "CE_DECKY_DESCRIPTOR_SHA256": "d" * 64})
    assert owned_descriptor_state(DESCRIPTOR, DIGEST, proc_root=proc) == "gone"

    # A truncated scan is ambiguous and must retain ownership.
    assert owned_descriptor_pids(DESCRIPTOR, DIGEST, proc_root=proc, max_processes=1)[0] == "unreadable"
    assert owned_descriptor_state(DESCRIPTOR, DIGEST, proc_root=tmp_path / "missing") == "unreadable"


def test_descriptor_sweep_retains_same_user_process_when_environ_is_denied(tmp_path: Path, monkeypatch):
    proc = tmp_path / "proc"
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    executable = runtime / "cheatengine-x86_64.exe"
    executable.write_bytes(b"ce")
    _proc(proc, 901, {}, argv=[str(executable)])
    (proc / "901" / "exe").symlink_to(executable)
    denied_path = proc / "901" / "environ"
    real_open = os.open

    def deny_environ(path, flags, *args, **kwargs):
        if Path(path) == denied_path:
            raise PermissionError(13, "denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_environ)
    assert owned_descriptor_pids(
        DESCRIPTOR, DIGEST, proc_root=proc, expected_executable=str(executable)
    ) == ("unreadable", (901,))


def test_descriptor_sweep_skips_only_a_positively_foreign_denied_process(tmp_path: Path, monkeypatch):
    proc = tmp_path / "proc"
    _proc(proc, 901, {}, argv=["/usr/bin/chromium", "--type=renderer"])
    denied_path = proc / "901" / "environ"
    real_open = os.open

    def deny_environ(path, flags, *args, **kwargs):
        if Path(path) == denied_path:
            raise PermissionError(13, "denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_environ)
    assert owned_descriptor_pids(
        DESCRIPTOR, DIGEST, proc_root=proc, expected_executable="/private/runtime/ce.exe"
    ) == ("gone", ())


def test_global_descriptor_sweep_classifies_denied_same_user_metadata(tmp_path: Path, monkeypatch):
    proc = tmp_path / "proc"
    _proc(proc, 901, {}, argv=["/usr/lib/systemd/systemd", "--user"])
    _proc(proc, 902, {}, argv=["/opt/Proton/proton", "runinprefix", "Z:\\ce.exe"])
    (proc / "901" / "comm").write_text("systemd\n", encoding="utf-8")
    (proc / "902" / "comm").write_text("python3\n", encoding="utf-8")
    denied = {proc / "901" / "environ", proc / "902" / "environ"}
    real_open = os.open

    def deny_environ(path, flags, *args, **kwargs):
        if Path(path) in denied:
            raise PermissionError(13, "denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_environ)
    # The obvious user-systemd PID is positively foreign, but the Proton-shaped
    # denied PID keeps the global corrupt-record repair fail-closed.
    assert owned_any_descriptor_pids(proc_root=proc) == ("unreadable", (902,))
    (proc / "902").rename(proc / "gone-902")
    assert owned_any_descriptor_pids(proc_root=proc) == ("gone", ())


def test_ce_decky_owned_process_is_not_evidence_that_the_game_is_running(tmp_path: Path):
    """An attached Cheat Engine inherits the game's SteamAppId on purpose.

    That is what puts it inside the game's prefix, and it makes CE Decky's own
    helper answer "is this game running?" the same way the game does. Without an
    exclusion, a game that exited keeps looking alive because the tool CE Decky
    launched for it is still there.
    """
    proc = tmp_path / "proc"
    proc.mkdir()
    _proc(proc, 901, {
        "SteamAppId": "220", "SteamGameId": "220",
        "CE_DECKY_DESCRIPTOR": DESCRIPTOR, "CE_DECKY_DESCRIPTOR_SHA256": DIGEST,
        "STEAM_COMPAT_DATA_PATH": "/home/deck/compatdata/220",
    })

    observation = observe_running_app_ids(proc_root=proc)
    assert observation.available is True
    assert observation.app_ids == ()

    container = observe_game_container(220, proc_root=proc)
    assert container.pids == ()

    # Ownership evidence is a different question and still finds it.
    assert owned_descriptor_state(DESCRIPTOR, DIGEST, proc_root=proc) == "matched"


def test_the_game_leaving_is_visible_while_cheat_engine_still_holds_its_wine_session(tmp_path: Path):
    """The baseline can never empty while the owned Cheat Engine runs.

    Cheat Engine has to run inside the game's own compatibility prefix to read
    that game's memory, which makes it a client of the same `wineserver`. So
    when the game exits, the Wine session and the whole Proton chain above it
    stay up for exactly as long as Cheat Engine does - and every one of those
    processes reports the game's own AppID. Waiting for that set to disappear
    was a wait on something Cheat Engine itself prevented, and Steam waits on
    the same chain, so the console sat on "shutting down" until the user gave
    up. This is the exact process tree that produced it.
    """
    proc = tmp_path / "proc"
    environ = {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"}
    _proc(proc, 10, environ, argv=["/lib/Steam/reaper", "SteamLaunch", "AppId=220"])
    _proc(proc, 11, environ, argv=["/lib/pressure-vessel/pv-adverb"])
    _proc(proc, 12, environ, argv=["python3", "/tools/GE-Proton/proton", "waitforexitandrun", "/games/Demo.exe"])
    _proc(proc, 13, environ, argv=["/tools/GE-Proton/files/bin/wineserver"])
    _proc(proc, 14, environ, argv=["c:\\windows\\system32\\winedevice.exe"])
    # The owned Cheat Engine inherits the game's AppID on purpose; it is the
    # reason all of the above is still alive and must never be game evidence.
    _proc(proc, 15, {**environ, "CE_DECKY_DESCRIPTOR": DESCRIPTOR, "CE_DECKY_DESCRIPTOR_SHA256": DIGEST},
          argv=["Z:\\ce\\cheatengine-x86_64.exe"])
    baseline = (10, 11, 12, 13, 14)

    # The old rule cannot fire here: every baseline PID still reports the game.
    assert all(game_process_state(pid, 220, proc_root=proc) == "matched" for pid in baseline)
    # The game's own program is what actually left.
    assert game_target_state(220, "Game-Win64-Shipping.exe", proc_root=proc) == "absent"

    _proc(proc, 20, environ, argv=["Z:\\home\\deck\\Games\\Demo\\Bin\\Game-Win64-Shipping.exe"])
    assert game_target_state(220, "Game-Win64-Shipping.exe", proc_root=proc) == "present"
    # Wine's own system processes never stand in for the game.
    assert game_target_state(220, "winedevice.exe", proc_root=proc) == "absent"


def test_several_targets_are_answered_for_from_one_walk(tmp_path: Path):
    """A session pointed at two executables asks about both at once.

    Two walks taken a moment apart can disagree, and a caller that needs every
    name gone at the same time would be reading a coincidence rather than an
    answer. Each name is still answered for itself.
    """
    proc = tmp_path / "proc"
    environ = {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"}
    _proc(proc, 10, environ, argv=["Z:\\games\\Demo\\Game-Win64-Shipping.exe"])

    states = game_target_states(220, ("launcher.exe", "Game-Win64-Shipping.exe"), proc_root=proc)
    assert states == {"launcher.exe": "absent", "Game-Win64-Shipping.exe": "present"}
    assert game_target_states(220, ()) == {}
    # A name that is not an executable is unknown, and says nothing about the
    # one beside it.
    assert game_target_states(220, ("launcher.exe", "not-an-executable"), proc_root=proc) == {
        "launcher.exe": "absent", "not-an-executable": "unknown",
    }
    assert game_target_states(220, ("not-an-executable",), proc_root=proc) == {"not-an-executable": "unknown"}


def test_game_target_state_never_reports_absent_from_a_scan_that_could_not_answer(tmp_path: Path):
    """Only a complete scan may end a launch; everything else stays ambiguous."""
    proc = tmp_path / "proc"
    environ = {"SteamAppId": "220", "STEAM_COMPAT_DATA_PATH": "/lib/compatdata/220"}
    _proc(proc, 10, environ, argv=["Z:\\games\\Demo\\Game-Win64-Shipping.exe"])
    _proc(proc, 11, environ, argv=["Z:\\games\\Demo\\Other.exe"])

    assert game_target_state(220, "Game-Win64-Shipping.exe", proc_root=proc, max_processes=1) == "unknown"
    assert game_target_state(220, "not-an-executable", proc_root=proc) == "unknown"
    assert game_target_state(220, "Game-Win64-Shipping.exe", proc_root=tmp_path / "missing") == "unknown"
    # No process reports this AppID at all: the game really is gone.
    assert game_target_state(221, "Game-Win64-Shipping.exe", proc_root=proc) == "absent"


def test_launch_record_carries_the_target_process_for_recovered_supervision(tmp_path: Path):
    """Supervision regained after a reload needs the same identity to watch."""
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record_path = supervisor.launch_record_root / "10.json"
    base = {
        "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST,
        "baseline_game_pids": [351], "bridge_sha256": "e" * 64, "started_at": 1.0,
    }

    record_path.write_text(json.dumps({**base, "schema": 4, "target_process": "Game-Win64-Shipping.exe"}), encoding="utf-8")
    assert supervisor._read_record(10)["target_process"] == "Game-Win64-Shipping.exe"

    # A record from before this build names no target and keeps the older
    # baseline-only supervision rather than being refused.
    record_path.write_text(json.dumps({**base, "schema": 3}), encoding="utf-8")
    assert supervisor._read_record(10)["target_process"] == ""

    record_path.write_text(json.dumps({**base, "schema": 4, "target_process": "../game.exe"}), encoding="utf-8")
    assert supervisor._read_record(10) is None


def test_launch_record_carries_the_game_liveness_baseline_and_reads_both_schemas(tmp_path: Path):
    """Recovered ownership must be able to regain supervision.

    An attached launch stops itself when the game it was started for exits, and
    only PIDs observed before Cheat Engine spawned count as game liveness. The
    durable record now carries them, and a record written by the previous build
    still loads so a live Cheat Engine is never orphaned by an update.
    """
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record_path = supervisor.launch_record_root / "10.json"

    legacy = {
        "schema": 1, "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST, "started_at": 1.0,
    }
    record_path.write_text(json.dumps(legacy), encoding="utf-8")
    loaded = supervisor._read_record(10)
    assert loaded is not None
    assert loaded["baseline_game_pids"] == []

    current = {**legacy, "schema": 2, "baseline_game_pids": [351, 356]}
    record_path.write_text(json.dumps(current), encoding="utf-8")
    loaded = supervisor._read_record(10)
    assert loaded is not None
    assert loaded["baseline_game_pids"] == [351, 356]

    # An unsupported schema is still refused rather than guessed at.
    record_path.write_text(json.dumps({**current, "schema": 3}), encoding="utf-8")
    assert supervisor._read_record(10) is None


def test_recovered_supervision_stops_owned_ce_the_game_left_behind_in_its_own_prefix(tmp_path: Path):
    """The reload path had the same deadlock, so it needs the same rule.

    Every baseline PID keeps reporting the game, because the recovered Cheat
    Engine is what keeps that Wine session alive. Only the target process the
    session was prepared for can prove the game itself is gone.
    """
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 4, "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST,
        "baseline_game_pids": [351], "bridge_sha256": "e" * 64,
        "target_process": "Game-Win64-Shipping.exe", "started_at": 1.0,
    }
    (supervisor.launch_record_root / "10.json").write_text(json.dumps(record), encoding="utf-8")

    terminated: list[dict] = []
    supervisor.recover_owned_launch = lambda app_id: {**record, "app_id": app_id}  # type: ignore[method-assign]
    supervisor._terminate_recovered = lambda item: (terminated.append(item), True)[1]  # type: ignore[method-assign]

    async def scenario() -> None:
        import ce_decky.ce_launch as module
        original_baseline = module.game_process_state
        # The supervisors ask `target_liveness`, which answers with the state
        # and the identity a later tick can confirm it by. Driving that is
        # driving what they actually consult.
        original_target = module.target_liveness
        seen = iter(["present", "present", "absent"])
        # The baseline never empties, exactly as on the real machine.
        module.game_process_state = lambda pid, app_id, **kwargs: "matched"
        module.target_liveness = lambda app_id, target, **kwargs: (next(seen, "absent"), None, False)
        try:
            assert await supervisor.resume_recovered_supervision(10) is True
            for _ in range(200):
                if terminated:
                    break
                await asyncio.sleep(SUPERVISION_TICK)
        finally:
            module.game_process_state = original_baseline
            module.target_liveness = original_target
        await supervisor.close()

    asyncio.run(scenario())
    assert [item["session_id"] for item in terminated] == ["b0a3f2c1-1111-4222-8333-444455556666"]
    assert not (supervisor.launch_record_root / "10.json").exists()


def _recovered_baseline_scenario(
    tmp_path: Path, target_states: list[str], *, container_gone: bool = False,
) -> list[dict]:
    """Run recovered supervision with a baseline that has completely vanished.

    `container_gone` is the walk's own answer to whether anything on the machine
    still reports this AppID, which is the only thing that can prove the game
    left. It defaults to false because the scenario this helper sets up, every
    launch-time PID retired, is an ordinary handoff with the game still there.
    """
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 5, "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST,
        "baseline_game_pids": [351], "bridge_sha256": "e" * 64,
        "target_process": "Game-Win64-Shipping.exe", "started_at": 1.0,
    }
    (supervisor.launch_record_root / "10.json").write_text(json.dumps(record), encoding="utf-8")

    terminated: list[dict] = []
    supervisor.recover_owned_launch = lambda app_id: {**record, "app_id": app_id}  # type: ignore[method-assign]
    supervisor._terminate_recovered = lambda item: (terminated.append(item), True)[1]  # type: ignore[method-assign]

    async def scenario() -> None:
        import ce_decky.ce_launch as module
        original_baseline = module.game_process_state
        # The supervisors ask `target_liveness`, which answers with the state
        # and the identity a later tick can confirm it by. Driving that is
        # driving what they actually consult.
        original_target = module.target_liveness
        states = iter(target_states)
        # How many ticks this window actually gave the loop. A scenario that
        # ends with nothing terminated has to have run for that to mean
        # anything, and a window counted in wall clock does not say whether it
        # did: asserting the floor is what keeps the faster tick from turning a
        # negative result into a loop that never got going.
        ticks = [0]

        def target(app_id, target_process, **kwargs):
            ticks[0] += 1
            return (next(states, target_states[-1]), None, container_gone)

        # Every PID observed when the launch started is gone: the launcher
        # handed off and exited, which is an ordinary way for a game to start.
        module.game_process_state = lambda pid, app_id, **kwargs: "gone_or_reused"
        module.target_liveness = target
        try:
            assert await supervisor.resume_recovered_supervision(10) is True
            for _ in range(60):
                if terminated:
                    break
                await asyncio.sleep(SUPERVISION_TICK)
            assert ticks[0] >= min(len(target_states), 3), ticks[0]
        finally:
            module.game_process_state = original_baseline
            module.target_liveness = original_target
        await supervisor.close()

    asyncio.run(scenario())
    return terminated


def test_only_a_seen_target_or_a_gone_game_proves_a_session_is_over():
    """The one rule both supervisors decide by, including the case they got wrong.

    `scan_is_due` calls a disappeared launch-time PID set with a never-seen
    target an ordinary handoff and keeps scanning through it. Both supervisors
    used to call the same state complete proof and stop Cheat Engine over it,
    which is the contradiction this function exists to make impossible to
    reintroduce in one of the two places.
    """
    # The handoff: the game has not started its exact executable yet, and the
    # walk can still see the game.
    assert target_exit_is_proved(state="absent", target_seen=False, container_gone=False) is False
    # The two real exits.
    assert target_exit_is_proved(state="absent", target_seen=True, container_gone=False) is True
    assert target_exit_is_proved(state="absent", target_seen=False, container_gone=True) is True
    # Nothing but a complete scan reaching absent proves anything at all.
    for state in ("present", "unknown"):
        for seen in (True, False):
            for gone in (True, False):
                assert target_exit_is_proved(state=state, target_seen=seen, container_gone=gone) is False


def test_recovered_supervision_keeps_ce_while_the_exact_target_is_alive(tmp_path: Path):
    """A launcher exiting is not the game exiting.

    Wine and Proton baseline PIDs cannot be the game-liveness authority, which
    is why the exact target process is watched at all - but the baseline was
    tested first and its disappearance stopped Cheat Engine on its own. A game
    started through a launcher or a bootstrap process replaces that whole PID
    set during an ordinary handoff, so an attached Cheat Engine and its table
    could be killed over a game that was still running.
    """
    assert _recovered_baseline_scenario(tmp_path, ["present"]) == []


def test_recovered_supervision_sits_through_a_handoff_its_target_never_arrived_in(tmp_path: Path):
    """A gone baseline and a never-seen target are the handoff, not the exit.

    Every PID observed when the launch started has retired and the game's own
    executable is not there yet, which is exactly what Steam's handoff looks
    like from here: the launcher and the bootstrap exit, and the game arrives
    after them under PIDs no frozen set can contain. Reading that as the game
    leaving stopped Cheat Engine in the middle of the startup this loop exists
    to wait out, and it did it to every session whose target had not appeared
    before the launch was prepared.
    """
    assert _recovered_baseline_scenario(tmp_path, ["absent"]) == []


def test_recovered_supervision_keeps_ce_until_a_late_target_arrives(tmp_path: Path):
    """The same handoff, held across several ticks, ending in the game."""
    assert _recovered_baseline_scenario(tmp_path, ["absent", "absent", "absent", "present"]) == []


def test_recovered_supervision_stops_ce_once_the_game_itself_is_gone(tmp_path: Path):
    """The other half, and the evidence that actually proves it.

    Not the launch-time PID set, which proves only that the launcher retired,
    but a complete walk of the process table finding nothing at all that reports
    this AppID. That ends a launch even for a target this supervisor never
    managed to see for itself, because there is no longer a game for it to
    appear in.
    """
    terminated = _recovered_baseline_scenario(tmp_path, ["absent"], container_gone=True)
    assert [item["session_id"] for item in terminated] == ["b0a3f2c1-1111-4222-8333-444455556666"]


def test_recovered_supervision_waits_out_a_scan_that_cannot_answer(tmp_path: Path):
    """An ambiguous scan is not absence, whatever the baseline says."""
    assert _recovered_baseline_scenario(tmp_path, ["unknown"]) == []


def test_recovered_supervision_never_stops_a_target_it_has_not_seen_alive(tmp_path: Path):
    """A launch prepared before the game settles must not stop itself at once."""
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 4, "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST,
        "baseline_game_pids": [351], "bridge_sha256": "e" * 64,
        "target_process": "Game-Win64-Shipping.exe", "started_at": 1.0,
    }
    (supervisor.launch_record_root / "10.json").write_text(json.dumps(record), encoding="utf-8")

    terminated: list[dict] = []
    supervisor.recover_owned_launch = lambda app_id: {**record, "app_id": app_id}  # type: ignore[method-assign]
    supervisor._terminate_recovered = lambda item: (terminated.append(item), True)[1]  # type: ignore[method-assign]

    async def scenario() -> None:
        import ce_decky.ce_launch as module
        original_baseline = module.game_process_state
        # The supervisors ask `target_liveness`, which answers with the state
        # and the identity a later tick can confirm it by. Driving that is
        # driving what they actually consult.
        original_target = module.target_liveness
        module.game_process_state = lambda pid, app_id, **kwargs: "matched"
        ticks = [0]

        def target(app_id, target_process, **kwargs):
            # Never seen alive, and the game itself is still running: the exact
            # executable simply is not the one the container is running yet.
            ticks[0] += 1
            return ("absent", None, False)

        module.target_liveness = target
        try:
            assert await supervisor.resume_recovered_supervision(10) is True
            for _ in range(30):
                await asyncio.sleep(SUPERVISION_TICK)
            # The loop ran and decided not to stop, rather than not running.
            assert ticks[0] >= 3, ticks[0]
        finally:
            module.game_process_state = original_baseline
            module.target_liveness = original_target
        await supervisor.close()

    asyncio.run(scenario())
    assert terminated == []


def test_recovered_supervision_stops_owned_ce_once_the_original_game_is_gone(tmp_path: Path):
    """A reload must not downgrade owned-and-supervised into owned-and-unsupervised."""
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    (supervisor.launch_record_root / "10.json").write_text(json.dumps({
        "schema": 2, "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST,
        "baseline_game_pids": [351], "started_at": 1.0,
    }), encoding="utf-8")

    terminated: list[dict] = []

    def _recovered(app_id: int) -> dict[str, object]:
        return {
            "app_id": app_id, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
            "pid": 4242, "pgid": 4242, "descriptor_windows_path": DESCRIPTOR,
            "descriptor_sha256": DIGEST, "baseline_game_pids": [351],
        }

    supervisor.recover_owned_launch = _recovered  # type: ignore[method-assign]
    supervisor._terminate_recovered = lambda record: (terminated.append(record), True)[1]  # type: ignore[method-assign]

    async def scenario() -> None:
        import ce_decky.ce_launch as module
        states = iter(["matched", "gone_or_reused", "gone_or_reused"])
        original = module.game_process_state
        module.game_process_state = lambda pid, app_id, **kwargs: next(states, "gone_or_reused")
        try:
            assert await supervisor.resume_recovered_supervision(10) is True
            # Resuming twice must not create a second monitor.
            assert await supervisor.resume_recovered_supervision(10) is True
            for _ in range(80):
                if terminated:
                    break
                await asyncio.sleep(SUPERVISION_TICK)
        finally:
            module.game_process_state = original
        await supervisor.close()

    asyncio.run(scenario())
    assert len(terminated) == 1
    assert not (supervisor.launch_record_root / "10.json").exists()


def test_only_one_attached_cheat_engine_may_execute_from_the_shared_runtime(tmp_path: Path):
    """The private runtime is one directory for every game and the self-test.

    It is keyed by CE/tree/bridge identity, not by game, and Cheat Engine writes
    into its own installation while it runs. Whether a second game reused that
    tree or was refused used to depend on whether the first process had dirtied
    it yet, so the behaviour flipped on timing.
    """
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    (supervisor.launch_record_root / "10.json").write_text(json.dumps({
        "schema": 3, "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST,
        "baseline_game_pids": [351], "bridge_sha256": "e" * 64, "started_at": 1.0,
    }), encoding="utf-8")
    supervisor.inspect_owned_launch_record = lambda app_id, **kwargs: {  # type: ignore[method-assign]
        "state": "matched", "record": {"app_id": app_id},
    }

    owners = supervisor.owned_launch_owners()
    assert [owner["app_id"] for owner in owners] == [10]
    assert owners[0]["recovered"] is True


def test_launch_record_carries_the_resident_bridge_identity(tmp_path: Path):
    """An attached Cheat Engine survives a plugin update on purpose.

    The runtime identity that binds the bridge lives in the runtime directory
    name, so nothing in the live session could tell a new backend that the
    process it recovered is running the previous resident protocol.
    """
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    base = {
        "app_id": 10, "session_id": "b0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST, "started_at": 1.0,
    }
    record_path = supervisor.launch_record_root / "10.json"

    record_path.write_text(json.dumps({**base, "schema": 3, "baseline_game_pids": [], "bridge_sha256": "e" * 64}), encoding="utf-8")
    assert supervisor._read_record(10)["bridge_sha256"] == "e" * 64

    # A record from before this build cannot identify its bridge, and says so
    # with an empty value rather than pretending to match.
    record_path.write_text(json.dumps({**base, "schema": 2, "baseline_game_pids": []}), encoding="utf-8")
    assert supervisor._read_record(10)["bridge_sha256"] == ""

    record_path.write_text(json.dumps({**base, "schema": 3, "baseline_game_pids": [], "bridge_sha256": "nope"}), encoding="utf-8")
    assert supervisor._read_record(10) is None


def test_recovery_retains_a_survivor_that_escaped_the_recorded_process_group(tmp_path: Path, monkeypatch):
    """Durable recovery must use the same authority Stop uses.

    The recorded group is where Proton put the launcher, not where its Wine
    clients stay, and one was observed leaving it on the target. Classifying the
    durable record by process group alone meant a reload cleared ownership of a
    Cheat Engine still attached to the game - after which a table switch starts
    a second one beside it, which is the failure the descriptor sweep exists to
    prevent.
    """
    supervisor, _ = _supervisor(tmp_path)
    monkeypatch.setattr("ce_decky.ce_launch.owned_process_group_state", lambda *args, **kwargs: "gone_or_reused")
    _record(supervisor, 220, 4242, "Z:\\s\\descriptor.txt", "d" * 64)

    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_descriptor_state",
        lambda descriptor, digest, **kwargs: "matched",
    )
    observed = supervisor.inspect_owned_launch_record(220)
    assert observed["state"] == "matched"
    recovered = supervisor.recover_owned_launch(220)
    assert recovered is not None and recovered["app_id"] == 220
    assert (supervisor.launch_record_root / "220.json").is_file()
    assert supervisor.has_live_owned_launch() is True
    assert [owner["app_id"] for owner in supervisor.owned_launch_owners()] == [220]

    # An unreadable scan is ambiguous and must also retain ownership.
    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_descriptor_state",
        lambda descriptor, digest, **kwargs: "unreadable",
    )
    assert supervisor.inspect_owned_launch_record(220)["state"] == "unreadable"
    assert (supervisor.launch_record_root / "220.json").is_file()

    # Only a complete scan that found nothing clears the record.
    monkeypatch.setattr(
        "ce_decky.ce_launch.owned_descriptor_state",
        lambda descriptor, digest, **kwargs: "gone",
    )
    assert supervisor.recover_owned_launch(220) is None
    assert not (supervisor.launch_record_root / "220.json").exists()


def test_descriptor_scan_can_prove_absence_on_a_real_process_table(tmp_path: Path):
    """Absence has to be reachable, or Stop can never confirm.

    An ordinary /proc pass always meets processes whose environment the kernel
    refuses - another user's, a setuid one, one that dropped dumpability.
    Treating every refusal as ambiguous made `gone` unreachable on any real
    system, so Stop and recovery would have blocked forever.
    """
    if os.name != "posix" or not Path("/proc").is_dir():
        pytest.skip("procfs is required")
    state, pids = owned_descriptor_pids(
        "Z:\\definitely\\not-running.txt",
        "f" * 64,
        expected_executable="/home/deck/.cheat-engine-decky/ce/runtime/not-running/cheatengine-x86_64.exe",
    )
    assert state == "gone"
    assert pids == ()


def test_corrupt_ownership_record_is_reported_to_the_panel_as_an_owner(tmp_path: Path):
    """`has_live_owned_launch()` blocks on a malformed record, so the UI must agree."""
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    (supervisor.launch_record_root / "220.json").write_text("{ not json", encoding="utf-8")

    assert supervisor.inspect_owned_launch_record(220)["state"] == "invalid"
    assert supervisor.has_live_owned_launch() is True
    assert [owner["app_id"] for owner in supervisor.owned_launch_owners()] == [220]


def test_corrupt_ownership_record_has_a_guarded_controller_repair(tmp_path: Path):
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = supervisor.launch_record_root / "220.json"
    record.write_text("{ not json", encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()

    outcome = supervisor.repair_invalid_owned_launch_record(220, proc_root=proc)
    assert outcome["discarded"] is True
    assert not record.exists()
    assert (supervisor.launch_record_root / str(outcome["quarantined"])).is_file()
    assert supervisor.inspect_owned_launch_record(220)["state"] == "absent"


def test_corrupt_ownership_repair_releases_an_exited_supervised_process(tmp_path: Path):
    """The compound failure the repair action exists for must not block itself.

    A stop whose cleanup could not prove the identity gone keeps its process
    entry, and ordinary liveness self-heals that from the global descriptor
    scan. Repair refused on the entry alone, before running the same scan, so
    the one controller action offered for malformed ownership could not act and
    a plugin reload was the only way out.
    """
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = supervisor.launch_record_root / "220.json"
    record.write_text("{ not json", encoding="utf-8")
    proc = tmp_path / "proc"
    proc.mkdir()

    class _Exited:
        returncode = 3

    class _Running:
        returncode = None

    supervisor._processes["stale"] = _Exited()  # type: ignore[assignment]
    outcome = supervisor.repair_invalid_owned_launch_record(220, proc_root=proc)
    assert outcome["discarded"] is True
    assert "stale" not in supervisor._processes

    # A supervisor that is still running keeps ownership on its own authority.
    record.write_text("{ not json", encoding="utf-8")
    supervisor._processes["live"] = _Running()  # type: ignore[assignment]
    with pytest.raises(ValueError, match="still supervised"):
        supervisor.repair_invalid_owned_launch_record(220, proc_root=proc)
    assert record.is_file()


def test_corrupt_ownership_repair_refuses_any_live_descriptor_process(tmp_path: Path):
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = supervisor.launch_record_root / "220.json"
    record.write_text("{ not json", encoding="utf-8")
    proc = tmp_path / "proc"
    _proc(proc, 901, {"CE_DECKY_DESCRIPTOR": DESCRIPTOR, "CE_DECKY_DESCRIPTOR_SHA256": DIGEST})
    assert owned_any_descriptor_pids(proc_root=proc) == ("matched", (901,))

    with pytest.raises(ValueError, match="descriptor-bearing process"):
        supervisor.repair_invalid_owned_launch_record(220, proc_root=proc)
    assert record.is_file()


def test_an_unreadable_ownership_inventory_is_published_as_uncertainty(tmp_path: Path, monkeypatch):
    """The panel must not read "clear" from a list the backend calls incomplete.

    `owned_launch_owners()` can only report the records it managed to read, and
    `has_live_owned_launch()` deliberately treats the same failure as owned. The
    capability now carries that disagreement so the panel gates on it instead of
    offering identity mutations the backend is certain to reject.
    """
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    assert supervisor.ownership_state_error() is None
    assert supervisor.capability(None)["ownership_state_error"] is None

    # A malformed record is already visible in the inventory as its own state,
    # so it needs no extra uncertainty signal.
    (supervisor.launch_record_root / "220.json").write_text("{", encoding="utf-8")
    assert [owner["state"] for owner in supervisor.owned_launch_owners()] == ["invalid"]
    assert supervisor.has_live_owned_launch() is True

    real_iterdir = Path.iterdir

    def unreadable(self):
        if self == supervisor.launch_record_root:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", unreadable)
    # This is the exact shape that used to publish an empty owner list while the
    # mutation guard kept reporting the machine as owned.
    assert supervisor.owned_launch_owners() == []
    assert supervisor.has_live_owned_launch() is True
    assert supervisor.capability(None)["ownership_state_error"] is not None


def test_known_anti_cheat_matches_only_an_exact_basename():
    from ce_decky.ce_launch import observed_anti_cheat

    assert observed_anti_cheat(["Game-Win64-Shipping.exe", "BELauncher.exe"]) == "belauncher.exe"
    assert observed_anti_cheat(["C:\\Games\\Ark\\belauncher.exe"]) == "belauncher.exe"
    # A directory that merely contains the name is not the process.
    assert observed_anti_cheat(["C:\\Games\\belauncher\\Game.exe"]) is None
    assert observed_anti_cheat(["Game-Win64-Shipping.exe", "crashpad_handler.exe"]) is None
    assert observed_anti_cheat([]) is None


@pytest.mark.asyncio
async def test_attached_launch_refuses_a_game_running_a_known_anti_cheat(tmp_path: Path, monkeypatch):
    """The panel refuses this, but the panel is not the boundary that spawns.

    A stale overlapping frontend or a direct RPC reaches the supervisor without
    the UI guard, so the documented offline/single-player refusal is enforced
    where the process is actually started. Nothing here disables, hides from or
    interferes with the anti-cheat.
    """
    supervisor, executable = _supervisor(tmp_path)
    observation = GameContainerObservation(
        app_id=220, running=True, pids=(4242,), compat_data_path=str(tmp_path / "compat"),
        steam_client_install_path=None, wine_prefix=str(tmp_path / "compat" / "pfx"),
        compat_tool_paths=(), conflicting_compat_data_paths=(), conflicting_steam_client_install_paths=(),
        conflicting_wine_prefixes=(), scanned=2, reason=None,
        windows_executables=("Game-Win64-Shipping.exe", "BELauncher.exe"),
        display=":1", launch_executable="Game-Win64-Shipping.exe", conflicting_displays=(),
    )
    monkeypatch.setattr("ce_decky.ce_launch.observe_game_container", lambda _app_id: observation)

    with pytest.raises(ValueError, match="known anti-cheat"):
        await supervisor.start_attached(
            [], executable, app_id=220, tool_id=None,
            session_id="123e4567-e89b-42d3-a456-426614174000",
            descriptor_path=tmp_path / "descriptor.txt", descriptor_sha256="d" * 64,
            descriptor_md5="e" * 32, descriptor_windows_path="Z:\\descriptor.txt",
            table_windows_path="Z:\\table.ct", table_sha256="b" * 64, ce_sha256="a" * 64,
            status_path=tmp_path / "status.txt", target_process="Game-Win64-Shipping.exe",
        )


@pytest.mark.asyncio
async def test_a_cold_proton_prefix_is_nothing_to_retire_rather_than_a_failure(tmp_path: Path):
    """Cancelling a self-test before Proton created `pfx` is not a cleanup error.

    Proton builds that directory asynchronously while it initializes a cold
    prefix, so an early cancellation or a plugin unload legitimately reaches
    cleanup before it exists. Treating it as missing turned a successful process
    stop back into "owned self-test Proton prefix did not stop".
    """
    from ce_decky.managed_ce import ProtonTool, _retire_owned_proton_prefix

    ce_root = tmp_path / "ce"
    prefix = ce_root / "self-test-prefix" / "tool"
    prefix.mkdir(parents=True)
    tool_root = tmp_path / "proton"
    (tool_root / "files" / "bin").mkdir(parents=True)
    wineserver = tool_root / "files" / "bin" / "wineserver"
    wineserver.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wineserver.chmod(0o755)
    # The helper re-verifies the exact Proton identity before it does anything,
    # so the fixture has to be a tool that identity actually describes.
    script = tool_root / "proton"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    resolved = str(tool_root.resolve(strict=True))
    script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    tool_id = hashlib.sha256((resolved + "\0" + script_sha).encode("utf-8")).hexdigest()
    tool = ProtonTool(tool_id, "Proton", resolved, script_sha, "owned-self-test-stop")

    # No `pfx` yet: nothing to stop, and nothing to complain about.
    await _retire_owned_proton_prefix(tool, prefix, ce_root)

    # Anything that does exist there still has to be a plain directory.
    (prefix / "pfx").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError):
        await _retire_owned_proton_prefix(tool, prefix, ce_root)

    (prefix / "pfx").unlink()
    (prefix / "pfx").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        await _retire_owned_proton_prefix(tool, prefix, ce_root)


def test_the_spawn_boundary_refuses_when_ownership_cannot_be_read(tmp_path: Path, monkeypatch):
    """The list built for the panel is not the authority the spawn needs.

    `owned_launch_owners()` reports the records it could read; the runtime and
    identity mutation guards already treat the same unreadable state as owned.
    Enforcing the one-Cheat-Engine-at-a-time invariant from the inventory alone
    let this boundary put a second process into the shared private runtime on
    exactly that evidence.
    """
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    supervisor._assert_launcher_is_free(220)

    real_iterdir = Path.iterdir

    def unreadable(self):
        if self == supervisor.launch_record_root:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", unreadable)
    assert supervisor.owned_launch_owners() == []
    assert supervisor.has_live_owned_launch() is True
    with pytest.raises(ValueError, match="ownership cannot be read"):
        supervisor._assert_launcher_is_free(220)
    # The self-test shares the same private runtime and refuses on the same
    # evidence.
    with pytest.raises(ValueError, match="ownership cannot be read"):
        supervisor._assert_launcher_is_free(None)


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group stop")
def test_the_self_test_prefix_is_retired_after_the_process_group_is_proven_gone(tmp_path: Path, monkeypatch):
    """Proton is still running while a pre-pass decides whether `pfx` exists.

    A cold prefix is created asynchronously, so `pfx` and its wineserver can
    appear after the pre-pass answered "nothing to retire" and before the group
    is retired. The pass that actually settles it has to run once nothing is
    left that could still be creating it.
    """
    import ce_decky.ce_launch as ce_launch_module

    supervisor, _ = _supervisor(tmp_path)
    order: list[str] = []

    class _Process:
        pid = 4242
        returncode = 0

        async def wait(self):
            return 0

    supervisor._processes["op-self-test"] = _Process()  # type: ignore[assignment]
    supervisor._operations["op-self-test"] = {
        "mode": MODE_SELF_TEST, "app_id": None, "session_id": "s", "plan": {"tool_id": "t"},
    }

    async def retire(_plan):
        order.append("prefix")

    monkeypatch.setattr(supervisor, "_retire_self_test_prefix", retire)
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda *_args: order.append("killpg"))
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda _pid: False)

    asyncio.run(supervisor._stop_process("op-self-test"))

    # The pre-pass still runs, so Wine gets its chance to exit cleanly, but the
    # decision that publishes the confirmed exit is the one after the group.
    assert order == ["prefix", "killpg", "prefix"]
    assert supervisor._operations["op-self-test"]["exit_code"] == 0


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group stop")
def test_a_prefix_that_only_the_pre_pass_could_not_retire_is_not_a_stop_failure(tmp_path: Path, monkeypatch):
    """The authoritative pass is the one after the group is gone."""
    import ce_decky.ce_launch as ce_launch_module

    supervisor, _ = _supervisor(tmp_path)
    attempts: list[int] = []

    class _Process:
        pid = 4242
        returncode = 0

        async def wait(self):
            return 0

    supervisor._processes["op-self-test"] = _Process()  # type: ignore[assignment]
    supervisor._operations["op-self-test"] = {
        "mode": MODE_SELF_TEST, "app_id": None, "session_id": "s", "plan": {"tool_id": "t"},
    }

    async def retire(_plan):
        attempts.append(len(attempts))
        # Proton was still shutting down when the pre-pass ran.
        if len(attempts) == 1:
            raise RuntimeError("owned Proton prefix did not stop")

    monkeypatch.setattr(supervisor, "_retire_self_test_prefix", retire)
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda _pid: False)

    asyncio.run(supervisor._stop_process("op-self-test"))
    assert len(attempts) == 2
    assert supervisor._operations["op-self-test"]["exit_code"] == 0


@pytest.mark.skipif(os.name != "posix", reason="owned POSIX process-group stop")
def test_a_prefix_the_post_group_pass_cannot_retire_still_fails_the_stop(tmp_path: Path, monkeypatch):
    """Malformed or unstoppable owned state stays fail-closed."""
    import ce_decky.ce_launch as ce_launch_module

    supervisor, _ = _supervisor(tmp_path)

    class _Process:
        pid = 4242
        returncode = 0

        async def wait(self):
            return 0

    supervisor._processes["op-self-test"] = _Process()  # type: ignore[assignment]
    supervisor._operations["op-self-test"] = {
        "mode": MODE_SELF_TEST, "app_id": None, "session_id": "s", "plan": {"tool_id": "t"},
    }

    async def retire(_plan):
        raise ValueError("managed CE installer path contains a symlink")

    monkeypatch.setattr(supervisor, "_retire_self_test_prefix", retire)
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda _pid: False)

    with pytest.raises(RuntimeError, match="owned self-test Proton prefix did not stop"):
        asyncio.run(supervisor._stop_process("op-self-test"))
    # No confirmed exit is published for a stop that could not be proven.
    assert "exit_code" not in supervisor._operations["op-self-test"]
    assert "op-self-test" in supervisor._processes


from ce_decky import ce_launch as _ce_launch  # noqa: E402


def _baseline_proc(root, pid: int, app_id: int | None) -> None:
    """One process directory whose environment either carries the AppID or does not."""
    directory = root / str(pid)
    directory.mkdir(parents=True)
    if app_id is None:
        (directory / "environ").write_bytes(b"PATH=/usr/bin\x00")
    else:
        (directory / "environ").write_bytes(f"SteamAppId={app_id}\x00PATH=/usr/bin\x00".encode())


def test_one_live_baseline_pid_settles_it_without_reading_the_rest(tmp_path):
    """The answer cannot change after the first live PID, so nothing else is read.

    This is what the one-second check used to spend on: a game under Proton
    leaves about twenty baseline PIDs and every tick read all of them to answer
    a question the first read usually answers.
    """
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    for pid in (101, 102, 103, 104, 105):
        _baseline_proc(proc, pid, 620)
    poll_counters.reset()
    try:
        assert _ce_launch.baseline_is_gone([101, 102, 103, 104, 105], 620, proc_root=proc) is False
        assert poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"] == 1
    finally:
        poll_counters.reset()


def test_every_pid_is_read_when_none_of_them_is_alive(tmp_path):
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    for pid in (201, 202, 203):
        _baseline_proc(proc, pid, 999)
    poll_counters.reset()
    try:
        # Proving absence is the expensive case and stays exhaustive, because
        # only the whole set can prove it.
        assert _ce_launch.baseline_is_gone([201, 202, 203], 620, proc_root=proc) is True
        assert poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"] == 3
    finally:
        poll_counters.reset()


def test_an_unreadable_pid_still_counts_as_alive_and_stops_the_scan(tmp_path):
    """procfs denies a same-user non-dumpable process; reading that as gone
    would misclassify a live game, which is a rule this must not quietly lose."""
    proc = tmp_path / "proc"
    directory = proc / "301"
    directory.mkdir(parents=True)
    (directory / "environ").write_bytes(b"")
    _baseline_proc(proc, 302, 999)

    assert _ce_launch.baseline_is_gone([301, 302], 620, proc_root=proc) is False


def test_the_short_circuit_agrees_with_reading_every_pid(tmp_path):
    """The predicate it replaced, checked against it over every arrangement."""
    import itertools

    proc = tmp_path / "proc"
    pids = [401, 402, 403]
    for index, pid in enumerate(pids):
        _baseline_proc(proc, pid, 620 if index == 0 else 999)

    for order in itertools.permutations(pids):
        states = tuple(_ce_launch.game_process_state(pid, 620, proc_root=proc) for pid in order)
        expected = "matched" not in states and "unreadable" not in states
        assert _ce_launch.baseline_is_gone(order, 620, proc_root=proc) is expected


def test_a_production_shaped_baseline_reads_everything_before_the_survivor_once(tmp_path):
    """The shape the short-circuit alone does not help with.

    Steam's handoff retires the launcher and bootstrap processes first, and the
    survivor can sit anywhere in the order, so a set or a PID-sorted tuple can
    put twenty dead PIDs in front of it. Without a hint the tick pays for all of
    them every second to reach a PID that answered the same way a second ago.
    """
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    dead = list(range(500, 520))
    for pid in dead:
        _baseline_proc(proc, pid, 999)
    _baseline_proc(proc, 520, 620)
    baseline = dead + [520]

    poll_counters.reset()
    try:
        gone, prover = _ce_launch.baseline_liveness(baseline, 620, proc_root=proc)
        assert gone is False and prover == 520
        first_pass = poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"]
        assert first_pass == 21

        # The next tick starts where the last one finished.
        gone, prover = _ce_launch.baseline_liveness(baseline, 620, prefer=prover, proc_root=proc)
        assert gone is False and prover == 520
        assert poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"] == first_pass + 1
    finally:
        poll_counters.reset()


def test_a_hint_that_stopped_proving_anything_falls_through_to_the_whole_baseline(tmp_path):
    proc = tmp_path / "proc"
    _baseline_proc(proc, 601, 999)
    _baseline_proc(proc, 602, 620)

    # 601 is offered but is not alive, so the traversal continues past it.
    gone, prover = _ce_launch.baseline_liveness([601, 602], 620, prefer=601, proc_root=proc)

    assert gone is False
    assert prover == 602


def test_a_hint_that_is_not_one_of_this_sessions_pids_is_never_read(tmp_path):
    """A PID outside the baseline is not evidence about this session's game."""
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    _baseline_proc(proc, 701, 620)
    _baseline_proc(proc, 999_001, 620)

    poll_counters.reset()
    try:
        gone, prover = _ce_launch.baseline_liveness([701], 620, prefer=999_001, proc_root=proc)
        assert gone is False and prover == 701
        assert poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"] == 1
    finally:
        poll_counters.reset()


def test_a_hint_cannot_make_a_gone_baseline_look_alive(tmp_path):
    proc = tmp_path / "proc"
    for pid in (801, 802, 803):
        _baseline_proc(proc, pid, 999)

    # Even offered the PID that used to prove liveness, absence is still proved
    # by reading the whole set.
    gone, prover = _ce_launch.baseline_liveness([801, 802, 803], 620, prefer=802, proc_root=proc)

    assert gone is True
    assert prover is None


def test_the_hint_never_changes_the_answer_whatever_it_is(tmp_path):
    proc = tmp_path / "proc"
    _baseline_proc(proc, 901, 999)
    _baseline_proc(proc, 902, 620)
    _baseline_proc(proc, 903, 999)
    baseline = [901, 902, 903]

    plain = _ce_launch.baseline_is_gone(baseline, 620, proc_root=proc)
    for hint in (None, 901, 902, 903, 12345):
        assert _ce_launch.baseline_liveness(baseline, 620, prefer=hint, proc_root=proc)[0] is plain


def test_a_one_shot_iterator_cannot_be_mistaken_for_a_gone_baseline(tmp_path):
    """Membership and traversal both read the baseline, so it must survive both.

    An earlier shape built a set from it and then iterated it again. Given a
    generator the first read exhausted it, the second saw an empty baseline, and
    an empty baseline reads as the game being gone. Nothing in production passes
    a generator; the contract now says so and the behaviour matches it.
    """
    proc = tmp_path / "proc"
    _baseline_proc(proc, 1101, 999)
    _baseline_proc(proc, 1102, 620)

    for hint in (None, 1101, 1102):
        assert _ce_launch.baseline_liveness((1101, 1102), 620, prefer=hint, proc_root=proc)[0] is False
        assert _ce_launch.baseline_liveness(frozenset({1101, 1102}), 620, prefer=hint, proc_root=proc)[0] is False


def test_a_dead_hint_is_not_read_twice(tmp_path):
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    _baseline_proc(proc, 1201, 999)
    _baseline_proc(proc, 1202, 620)

    poll_counters.reset()
    try:
        gone, prover = _ce_launch.baseline_liveness([1201, 1202], 620, prefer=1201, proc_root=proc)
        assert gone is False and prover == 1202
        # 1201 was tried as the hint and skipped in the traversal, not read again.
        assert poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"] == 2
    finally:
        poll_counters.reset()


def test_a_baseline_that_has_just_gone_is_scanned_immediately():
    """The property the old gate existed for, kept."""
    assert _ce_launch.scan_is_due(tick=1, baseline_gone=True, baseline_was_gone=False, target_seen=True) is True


def test_a_session_still_converging_keeps_scanning_every_tick():
    """While the target has never been seen, a gone baseline stays urgent.

    This is the launcher handoff: the PIDs recorded at launch are going and the
    game is only just arriving, and the session has to find it.
    """
    for tick in (1, 2, 4, 5):
        assert _ce_launch.scan_is_due(
            tick=tick, baseline_gone=True, baseline_was_gone=True, target_seen=False,
        ) is True


def test_a_baseline_that_is_merely_still_gone_returns_to_the_ordinary_interval():
    """The defect: a permanently gone baseline tripled the dominant scan.

    A launch prepared while the game was bootstrapping records the launcher's
    PIDs, Steam retires all of them, and `baseline_gone` then stays true for the
    rest of an ordinary session. The scan is the most expensive thing this
    backend does, and it ran every second instead of every three for all of it.
    """
    due = [
        _ce_launch.scan_is_due(tick=tick, baseline_gone=True, baseline_was_gone=True, target_seen=True)
        for tick in range(1, 10)
    ]

    assert due == [False, False, True, False, False, True, False, False, True]


def test_an_ordinary_live_baseline_scans_on_the_interval_as_before():
    due = [
        _ce_launch.scan_is_due(tick=tick, baseline_gone=False, baseline_was_gone=False, target_seen=True)
        for tick in range(1, 7)
    ]

    assert due == [False, False, True, False, False, True]


def test_the_gate_only_decides_when_a_scan_happens():
    """It can never end a launch: only a complete scan returning absent may."""
    import inspect

    source = inspect.getsource(_ce_launch.scan_is_due)

    assert "absent" not in source.replace("`absent`", "")
    assert "_stop_process" not in source


# --- candidate 4: cheap positive liveness for the target ---------------------

def _target_proc(root: Path, pid: int, *, app_id: int, executable: str, start_time: int = 4242) -> None:
    """One process of the game, complete enough for both liveness routes."""
    _proc(
        root, pid,
        {"SteamAppId": str(app_id), "STEAM_COMPAT_DATA_PATH": "/compat/data", "WINEPREFIX": "/compat/data/pfx"},
        argv=["z:\\games\\" + executable],
    )
    (root / str(pid) / "stat").write_bytes(
        f"{pid} (wine) S 1 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 {start_time} 0 0\n".encode()
    )


def _counts() -> dict[str, int]:
    from ce_decky import poll_counters

    return {name: row["calls"] for name, row in poll_counters.snapshot()["paths"].items()}


def test_a_confirmed_target_is_present_without_walking_the_table(tmp_path):
    """The whole point: three reads of one process instead of the whole table.

    The walk reads an environment for every process on the machine to find one,
    and it is 85% to 88% of what this backend spends while a session is live.
    """
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe")

    poll_counters.reset()
    try:
        state, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)
        assert state == "present"
        assert identity is not None and identity.pid == 4321 and identity.app_id == 620
        first = _counts()
        assert first[poll_counters.SUPERVISOR_TARGET_SCAN] == 1

        state, identity, _ = _ce_launch.target_liveness(620, "game.exe", known=identity, proc_root=proc)
        assert state == "present"
        after = _counts()
        # No second scan, and no second walk of the process table.
        assert after[poll_counters.SUPERVISOR_TARGET_SCAN] == 1
        assert after[poll_counters.PRIMITIVE_GAME_CONTAINER] == first[poll_counters.PRIMITIVE_GAME_CONTAINER]
        # One confirmation: the first call had nothing to confirm against.
        assert after[poll_counters.SUPERVISOR_TARGET_IDENTITY] == 1
    finally:
        poll_counters.reset()


def test_the_cheap_check_can_never_answer_absent(tmp_path):
    """Only the complete table can prove a process is not in it.

    Every way the cheap check fails means the complete scan runs, so it can make
    a session cheaper and cannot make it wrong.
    """
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe")
    _, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)

    import shutil

    shutil.rmtree(proc / "4321")

    from ce_decky import poll_counters

    poll_counters.reset()
    try:
        state, dropped, _ = _ce_launch.target_liveness(620, "game.exe", known=identity, proc_root=proc)
        assert state == "absent"
        assert dropped is None, "a stale identity is never offered back"
        # It reached that answer through the complete scan, not the cheap check.
        assert _counts()[poll_counters.SUPERVISOR_TARGET_SCAN] == 1
    finally:
        poll_counters.reset()


def test_a_reused_pid_is_refused_by_its_start_time(tmp_path):
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=999, windows_executable="game.exe", app_id=620)

    assert _ce_launch.confirm_target_identity(identity, proc_root=proc) is False


def test_a_process_that_no_longer_declares_this_app_is_refused(tmp_path):
    """Start time and executable prove only that something of that name is there.

    Two titles running at once can share an executable name and a Proton chain,
    so the declared AppID is what ties the process back to this session's game.
    """
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=999, executable="game.exe", start_time=1000)
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=1000, windows_executable="game.exe", app_id=620)

    assert _ce_launch.confirm_target_identity(identity, proc_root=proc) is False


def test_a_process_running_a_different_executable_is_refused(tmp_path):
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="other.exe", start_time=1000)
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=1000, windows_executable="game.exe", app_id=620)

    assert _ce_launch.confirm_target_identity(identity, proc_root=proc) is False


def test_an_unreadable_environment_is_refused_rather_than_assumed(tmp_path):
    """procfs denies a same-user non-dumpable process, and this must not guess.

    Refusing costs a complete scan. Assuming would call a process the session's
    game on the strength of a name.
    """
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    (proc / "4321" / "environ").write_bytes(b"")
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=1000, windows_executable="game.exe", app_id=620)

    assert _ce_launch.confirm_target_identity(identity, proc_root=proc) is False


def test_the_executable_match_is_case_insensitive_like_the_scan(tmp_path):
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="Game.EXE", start_time=1000)
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=1000, windows_executable="game.exe", app_id=620)

    assert _ce_launch.confirm_target_identity(identity, proc_root=proc) is True


def test_an_unknown_target_state_drops_the_identity_too(tmp_path):
    """`unknown` is not proof of anything, so nothing may be carried through it."""
    proc = tmp_path / "proc"
    proc.mkdir()

    state, identity, _ = _ce_launch.target_liveness(620, "", proc_root=proc)

    assert state == "unknown"
    assert identity is None


def test_an_unreadable_target_is_never_reported_absent(tmp_path):
    """The scan is blind in exactly the way the confirmation was.

    A target that still exists at the same PID, with the same start time and the
    same executable, whose environment procfs will not hand over, is invisible
    to the complete scan for the same reason it cannot be confirmed cheaply. The
    scan's silence is that missing evidence, not a second opinion about it, and
    the documented invariant is that an unreadable same-user process is
    ambiguous rather than gone. Reading it as absent stops owned Cheat Engine
    over a game that is still running.
    """
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    state, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)
    assert state == "present" and identity is not None

    # The process is untouched; only its environment stops being readable.
    (proc / "4321" / "environ").write_bytes(b"")

    assert _ce_launch.inspect_target_identity(identity, proc_root=proc) == _ce_launch.TARGET_UNREADABLE
    state, kept, _ = _ce_launch.target_liveness(620, "game.exe", known=identity, proc_root=proc)

    assert state == "unknown", "an unreadable target is ambiguous, never gone"
    # The identity is kept, so the moment procfs answers again the cheap check
    # confirms it rather than starting from another full scan.
    assert kept == identity
    (proc / "4321" / "environ").write_bytes(b"SteamAppId=620\x00")
    assert _ce_launch.target_liveness(620, "game.exe", known=kept, proc_root=proc) == ("present", identity, False)


def test_a_target_that_really_went_away_is_still_absent(tmp_path):
    """The protection above must not swallow a real exit."""
    import shutil

    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    _, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)
    shutil.rmtree(proc / "4321")

    assert _ce_launch.inspect_target_identity(identity, proc_root=proc) == _ce_launch.TARGET_CHANGED
    assert _ce_launch.target_liveness(620, "game.exe", known=identity, proc_root=proc) == ("absent", None, True)


def test_a_replaced_process_at_the_same_pid_is_still_absent(tmp_path):
    """Unreadable is the only ambiguity; a different process is a real answer."""
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    _, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)

    import shutil

    shutil.rmtree(proc / "4321")
    _target_proc(proc, 4321, app_id=999, executable="other.exe", start_time=5000)

    assert _ce_launch.inspect_target_identity(identity, proc_root=proc) == _ce_launch.TARGET_CHANGED
    assert _ce_launch.target_liveness(620, "game.exe", known=identity, proc_root=proc)[0] == "absent"


def test_acquiring_the_identity_walks_the_table_exactly_once(tmp_path):
    """The acquisition used to pay the most expensive path here twice.

    Only the first walk was counted as a scan, so the duplicate was invisible in
    exactly the attribution the identity exists to improve.
    """
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe")

    poll_counters.reset()
    try:
        state, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)
        assert state == "present" and identity is not None
        counts = _counts()
        assert counts[poll_counters.PRIMITIVE_GAME_CONTAINER] == 1
        assert counts[poll_counters.SUPERVISOR_TARGET_SCAN] == 1
    finally:
        poll_counters.reset()


def test_a_failed_confirmation_that_resolves_to_present_walks_once_too(tmp_path):
    from ce_decky import poll_counters

    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    _, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)

    # The game restarted: same executable and AppID, a new process.
    import shutil

    shutil.rmtree(proc / "4321")
    _target_proc(proc, 5555, app_id=620, executable="game.exe", start_time=7000)

    poll_counters.reset()
    try:
        state, refreshed, _ = _ce_launch.target_liveness(620, "game.exe", known=identity, proc_root=proc)
        assert state == "present"
        assert refreshed is not None and refreshed.pid == 5555
        counts = _counts()
        assert counts[poll_counters.PRIMITIVE_GAME_CONTAINER] == 1
        assert counts[poll_counters.SUPERVISOR_TARGET_SCAN] == 1
    finally:
        poll_counters.reset()


def test_supervision_logs_a_change_and_not_a_tick(tmp_path, caplog):
    """A line per tick would add work at the rate being measured.

    The supervision loop asks these questions every second. What a later report
    needs is the moment an answer changed, not ninety copies of the answer.
    """
    supervisor = CELaunchSupervisor(
        tmp_path / "home", tmp_path / "ce", tmp_path / "state", logging.getLogger("supervision-log-test"),
    )
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=1, windows_executable="game.exe", app_id=620)

    with caplog.at_level(logging.INFO, logger="supervision-log-test"):
        logged = None
        for _ in range(5):
            logged = supervisor._log_target_change(620, "session-id-here", "present", identity, True, logged)
        assert len([r for r in caplog.records if "target_changed" in r.getMessage()]) == 1

        supervisor._log_target_change(620, "session-id-here", "absent", None, True, logged)
        assert len([r for r in caplog.records if "target_changed" in r.getMessage()]) == 2

        gone = None
        for _ in range(4):
            gone = supervisor._log_baseline_change(620, True, gone)
        assert len([r for r in caplog.records if "baseline_changed" in r.getMessage()]) == 1


def test_the_ambiguous_target_is_named_in_the_log_as_such(tmp_path, caplog):
    """Cheat Engine outliving a game the user quit is answered here or nowhere.

    An unreadable target reads as `unknown` while keeping its identity, and that
    combination is what a report needs to distinguish from an ordinary
    unknown.
    """
    supervisor = CELaunchSupervisor(
        tmp_path / "home", tmp_path / "ce", tmp_path / "state", logging.getLogger("ambiguity-log-test"),
    )
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=1, windows_executable="game.exe", app_id=620)

    with caplog.at_level(logging.INFO, logger="ambiguity-log-test"):
        supervisor._log_target_change(620, "abcdefghijkl", "unknown", identity, True, None)
        message = "\n".join(record.getMessage() for record in caplog.records)

    assert "target_unreadable=true" in message
    assert "target_pid=4321" in message
    # The session id is truncated, like every other identifier this logs.
    assert "abcdefghijkl" not in message and "abcdefgh" in message


def test_only_the_ambiguity_keeps_an_identity_on_a_non_present_state(tmp_path):
    """The signature the log relies on, held by a test so it cannot drift.

    `_log_target_change` reports `target_unreadable` from the fact that a
    non-present state arrived with an identity. That is only true because every
    other non-present answer drops it.
    """
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    _, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)

    import shutil

    shutil.rmtree(proc / "4321")
    assert _ce_launch.target_liveness(620, "game.exe", known=identity, proc_root=proc) == ("absent", None, True)
    assert _ce_launch.target_liveness(620, "", known=identity, proc_root=proc) == ("unknown", None, False)


def test_an_unreadable_target_is_not_absent_even_with_nothing_cached(tmp_path):
    """The scan itself must not prove an absence by declining to look.

    Two ordinary windows have no cached identity: a fresh session before its
    first successful acquisition, and recovered supervision after a reload,
    which restores the durable `target_seen` and starts with no identity at all.
    In both, an absence reported here is acted on, and the recovered case can
    stop Cheat Engine immediately. The walk decides what a process belongs to by
    reading its environment, and procfs refuses that for a same-user
    non-dumpable process, so a live target in that state was skipped and its
    absence proved by not having looked at it.
    """
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    # The process is untouched and still running the exact target; only its
    # environment stops being readable.
    (proc / "4321" / "environ").write_bytes(b"")

    state, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)

    assert state == "unknown", "an absence must not be provable by skipping the evidence"
    assert identity is None
    assert _ce_launch.game_target_state(620, "game.exe", proc_root=proc) == "unknown"


def test_a_machine_full_of_unreadable_processes_can_still_prove_an_absence(tmp_path):
    """The protection must be about the target, not about procfs in general.

    Most of an ordinary machine's process table is unreadable to a normal user.
    Treating that as ambiguity would mean a session could never notice a game
    exiting at all, which is the opposite failure and a worse one.
    """
    proc = tmp_path / "proc"
    for pid in range(500, 530):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "environ").write_bytes(b"")
        (directory / "cmdline").write_bytes(b"/usr/bin/somethingelse\x00")

    assert _ce_launch.game_target_state(620, "game.exe", proc_root=proc) == "absent"


def test_an_unreadable_process_running_another_executable_does_not_hold_a_session_open(tmp_path):
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="other.exe", start_time=1000)
    (proc / "4321" / "environ").write_bytes(b"")

    assert _ce_launch.game_target_state(620, "game.exe", proc_root=proc) == "absent"


def test_the_observation_reports_how_many_processes_it_could_not_read(tmp_path):
    """A count is the diagnostic; the list of PIDs is not, and stays internal."""
    proc = tmp_path / "proc"
    for pid in (601, 602):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "environ").write_bytes(b"")

    observation = _ce_launch.observe_game_container(620, proc_root=proc)
    published = observation.public()

    assert observation.unreadable_pids == (601, 602)
    assert published["unreadable_processes"] == 2
    assert "unreadable_pids" not in published


def test_a_pid_handed_to_another_process_between_reads_is_not_confirmed(tmp_path, monkeypatch):
    """A PID is a name, and it can change owner between any two reads of it.

    The confirmation reads a start time, then a command line, then an
    environment. Without closing that transaction, an old start time could be
    combined with a replacement process's executable and app and read as the
    same process.
    """
    proc = tmp_path / "proc"
    _target_proc(proc, 4321, app_id=620, executable="game.exe", start_time=1000)
    identity = _ce_launch.TargetIdentity(pid=4321, start_time=1000, windows_executable="game.exe", app_id=620)
    assert _ce_launch.inspect_target_identity(identity, proc_root=proc) == _ce_launch.TARGET_CONFIRMED

    # The process is replaced after the first read and before the last.
    real = _ce_launch._process_start_time
    calls = {"n": 0}

    def racing(pid: int, root):
        calls["n"] += 1
        return 1000 if calls["n"] == 1 else 7777

    monkeypatch.setattr(_ce_launch, "_process_start_time", racing)

    assert _ce_launch.inspect_target_identity(identity, proc_root=proc) == _ce_launch.TARGET_CHANGED
    monkeypatch.setattr(_ce_launch, "_process_start_time", real)


def test_resolving_an_identity_revalidates_the_app_the_process_declares(tmp_path):
    """The PID came from an observation, and a moment is enough for reuse."""
    proc = tmp_path / "proc"
    # Running the target executable, but declaring a different app.
    _target_proc(proc, 4321, app_id=999, executable="game.exe", start_time=1000)

    assert _ce_launch.resolve_target_identity(620, "game.exe", [4321], proc_root=proc) is None


def test_a_target_past_the_thousandth_unreadable_process_is_still_not_absent(tmp_path):
    """The set of skipped processes had a bound of its own, and that was a hole.

    The walk is already bounded and refuses to report an absence once it reaches
    that bound, so a second, tighter cap on the skipped set bought nothing and
    reintroduced the failure this protection exists to close: a live target
    sitting past the cap is skipped again, and reported gone again, in exactly
    the windows where no cached identity is there to catch it.
    """
    proc = tmp_path / "proc"
    for pid in range(1000, 2100):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "environ").write_bytes(b"")
        (directory / "cmdline").write_bytes(b"/usr/bin/somethingelse\x00")
    # Well past the 1024 the old cap retained, and ordered after them.
    _target_proc(proc, 9000, app_id=620, executable="game.exe", start_time=1000)
    (proc / "9000" / "environ").write_bytes(b"")

    state, identity, _ = _ce_launch.target_liveness(620, "game.exe", proc_root=proc)

    assert state == "unknown"
    assert identity is None

    observation = _ce_launch.observe_game_container(620, proc_root=proc)
    # And the published count is the real one, not the size of a cap.
    assert observation.public()["unreadable_processes"] == 1101
    assert 9000 in observation.unreadable_pids


def test_a_walk_that_hit_its_own_bound_never_reports_an_absence(tmp_path):
    """Which is what makes the skipped set complete whenever it is consulted."""
    proc = tmp_path / "proc"
    for pid in range(1000, 1010):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "environ").write_bytes(b"")

    assert _ce_launch.game_target_state(620, "game.exe", proc_root=proc, max_processes=4) == "unknown"


def test_a_truncated_scan_still_reports_what_it_had_already_skipped(tmp_path):
    """The early returns built a fresh observation and dropped the count.

    It cannot bring the liveness defect back, because a truncated scan reports
    `unknown` and never `absent`. It made the published number wrong, which is
    its own problem: a report reading zero unattributable processes for a scan
    that had already skipped a dozen of them is being told the opposite of what
    happened.
    """
    proc = tmp_path / "proc"
    for pid in range(1000, 1012):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "environ").write_bytes(b"")

    observation = _ce_launch.observe_game_container(620, proc_root=proc, max_processes=6)

    assert observation.reason == "process table exceeds the bounded scan limit"
    assert observation.public()["unreadable_processes"] == 6
    assert _ce_launch.game_target_state(620, "game.exe", proc_root=proc, max_processes=6) == "unknown"


def test_a_pid_limited_observation_reports_them_too(tmp_path):
    proc = tmp_path / "proc"
    for pid in range(1000, 1005):
        directory = proc / str(pid)
        directory.mkdir(parents=True)
        (directory / "environ").write_bytes(b"")
    for index in range(_ce_launch.MAX_OBSERVED_PIDS + 1):
        _target_proc(proc, 2000 + index, app_id=620, executable="game.exe")

    observation = _ce_launch.observe_game_container(620, proc_root=proc)

    assert "bounded PID limit" in (observation.reason or "")
    assert observation.public()["unreadable_processes"] == 5


@pytest.mark.parametrize("repair", [True, False])
def test_ownership_removal_preserves_durability_unknown_type(tmp_path, monkeypatch, repair):
    from ce_decky import atomic
    supervisor, _ = _supervisor(tmp_path)
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = supervisor.launch_record_root / "220.json"
    record.write_text("{ not json")
    proc = tmp_path / "proc"
    proc.mkdir()
    def refuse(path):
        raise OSError("sync refused")
    monkeypatch.setattr(atomic, "fsync_directory", refuse)
    with pytest.raises(atomic.DurabilityUnknownError):
        if repair:
            supervisor.repair_invalid_owned_launch_record(220, proc_root=proc)
        else:
            supervisor._clear_record(220)
    assert not record.exists()


@pytest.mark.parametrize("recovered", [False, True])
def test_exact_revoke_stop_refuses_a_replacement_session(tmp_path, monkeypatch, recovered):
    from unittest.mock import AsyncMock, Mock
    supervisor, _ = _supervisor(tmp_path)
    replacement = {"operation_id": "replacement", "session_id": "replacement-session"}
    monkeypatch.setattr(supervisor, "current_for_app", lambda app_id: None if recovered else replacement)
    monkeypatch.setattr(supervisor, "recover_owned_launch", lambda app_id: replacement)
    stop, terminate = AsyncMock(), Mock()
    monkeypatch.setattr(supervisor, "stop", stop)
    monkeypatch.setattr(supervisor, "_terminate_recovered", terminate)
    with pytest.raises(ValueError, match="session changed"):
        asyncio.run(supervisor.stop_for_app(220, expected_session_id="confirmed-session"))
    stop.assert_not_called()
    terminate.assert_not_called()


def test_batch_executable_observation_scans_process_table_once(tmp_path, monkeypatch):
    from ce_decky.ce_launch import observe_game_executable_paths
    from unittest.mock import Mock
    proc = tmp_path / "proc"
    proc.mkdir()
    exe = tmp_path / "game.exe"
    exe.write_bytes(b"game")
    for app_id in (10, 20):
        process = proc / str(app_id)
        process.mkdir()
        (process / "environ").write_bytes(f"SteamAppId={app_id}\0".encode())
        (process / "cmdline").write_bytes(("Z:" + str(exe) + "\0").encode())
    scandir = Mock(wraps=os.scandir)
    monkeypatch.setattr("ce_decky.ce_launch.os.scandir", scandir)
    assert observe_game_executable_paths({10: "game.exe", 20: "game.exe"}, proc_root=proc) == {10: str(exe), 20: str(exe)}
    assert scandir.call_count == 1


def test_recovered_supervision_outlives_the_call_that_started_it(tmp_path: Path):
    """`_main` returning is not the end of the work `_main` started.

    Decky gives a plugin process one event loop and runs it forever, so `_main`
    is a task on that loop and the monitors it creates keep running after it
    returns; `docs/FIELD_NOTES.md` carries the upstream source and the device
    reading for that. A static review read the opposite claim, which this
    repository used to make in a test name, and reported that an owned Cheat
    Engine which outlived a plugin update would be left unsupervised. This is
    the case that says otherwise, in the host's own shape: the resuming call
    returns completely, and only then does the game go away.
    """
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 4, "app_id": 10, "session_id": "c0a3f2c1-1111-4222-8333-444455556666",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64, "executable": "/ce/cheatengine-x86_64.exe",
        "descriptor_windows_path": DESCRIPTOR, "descriptor_sha256": DIGEST,
        "baseline_game_pids": [351], "bridge_sha256": "e" * 64,
        "target_process": "Game-Win64-Shipping.exe", "started_at": 1.0,
    }
    (supervisor.launch_record_root / "10.json").write_text(json.dumps(record), encoding="utf-8")

    terminated: list[dict] = []
    supervisor.recover_owned_launch = lambda app_id: {**record, "app_id": app_id}  # type: ignore[method-assign]
    supervisor._terminate_recovered = lambda item: (terminated.append(item), True)[1]  # type: ignore[method-assign]

    async def host() -> None:
        import ce_decky.ce_launch as module
        original_baseline = module.game_process_state
        original_target = module.target_liveness
        # The game is there for the first complete scan and gone by the next,
        # which is the ordinary exit. The baseline never empties, exactly as on
        # the real machine, because the recovered Cheat Engine holds the session.
        seen = iter(["present", "absent"])
        module.game_process_state = lambda pid, app_id, **kwargs: "matched"
        module.target_liveness = lambda app_id, target, **kwargs: (next(seen, "absent"), None, False)
        try:
            # What the plugin's load does, as its own task, which is how the
            # host runs it. Awaited to completion here, exactly as `_main`
            # awaits it before returning.
            assert await asyncio.create_task(supervisor.resume_all_recovered_supervision()) == [10]
            assert not terminated

            # The load is over and nothing of it is left running. On the device
            # the loop it ran on is the process's own and goes on running, so
            # the monitor it left behind is still there when the game exits.
            for _ in range(200):
                if terminated:
                    break
                await asyncio.sleep(SUPERVISION_TICK)
        finally:
            module.game_process_state = original_baseline
            module.target_liveness = original_target
        await supervisor.close()

    asyncio.run(host())
    assert [item["session_id"] for item in terminated] == ["c0a3f2c1-1111-4222-8333-444455556666"]
    assert not (supervisor.launch_record_root / "10.json").exists()


def test_a_self_test_is_signalled_before_the_unload_can_suspend(tmp_path: Path, monkeypatch):
    """A self-test is a transaction, and it must not outlive this plugin.

    `close()` retires it properly, prefix and durable record included, but every
    part of that happens after an `await`, and `docs/FIELD_NOTES.md` records why
    that is not where something which must happen belongs: Decky's stop starves
    this process's loop and kills it five seconds later. So the signal itself is
    sent in the calling thread, which is what this proves by sending it with no
    loop running at all. An attached launch is deliberately left alone; that
    Cheat Engine is the user's and survives a plugin update on purpose.
    """
    import signal

    import ce_decky.ce_launch as ce_launch_module

    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: signalled.append((pid, sig)))

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    supervisor._processes["self-test"] = _Process(4242)  # type: ignore[assignment]
    supervisor._processes["attached"] = _Process(4343)  # type: ignore[assignment]
    supervisor._operations["self-test"] = {"mode": ce_launch_module.MODE_SELF_TEST}
    supervisor._operations["attached"] = {"mode": ce_launch_module.MODE_ATTACHED}

    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: False)

    supervisor.begin_close()

    assert signalled == [(4242, signal.SIGTERM)]
    assert supervisor._closing is True


def test_a_self_test_that_ignores_the_signal_is_killed_before_the_plugin_dies(tmp_path: Path, monkeypatch):
    """The signal is not the stop, and the prologue is the only chance to finish it.

    This project's own supervisor records why: Wine children outlive the Proton
    leader, which is what the orderly path's escalation exists for. That path
    runs after an `await` and on this host never runs at all, and a self-test
    writes no durable record, so a group that ignored the signal would be left
    running with nothing naming it.
    """
    import signal

    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.05)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_KILL_SECONDS", 0.05)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    sent: list[int] = []
    # A group that sits through SIGTERM and goes when it is killed, which is the
    # case the escalation is for.
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(
        ce_launch_module, "_process_group_exists", lambda pgid: signal.SIGKILL not in sent,
    )

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    supervisor._processes["self-test"] = _Process(4242)  # type: ignore[assignment]
    supervisor._operations["self-test"] = {"mode": ce_launch_module.MODE_SELF_TEST}

    supervisor.begin_close()

    assert sent == [signal.SIGTERM, signal.SIGKILL]


def test_the_prologue_sweeps_the_prefix_again_after_the_group_is_gone(tmp_path: Path, monkeypatch, caplog):
    """The sweep before the signal is courtesy; the one after it is the proof.

    Measured on the device: under Proton, Cheat Engine ends up in a process
    group of its own with `init` as its parent, so the group the launch started
    goes away, the stop reports success, and Cheat Engine is still running. The
    prefix sweep is what reaches it.

    Which sweep, though, is the whole of this case. This file already says the
    pass before the signal cannot be the answer, because Proton is still running
    while it decides whether `pfx` exists and a cold prefix can appear
    immediately afterwards. So the terminal sequence is sweep, signal, sweep,
    and only the last of those decides.
    """
    import signal

    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    order: list[str] = []
    monkeypatch.setattr(
        ce_launch_module, "retire_owned_proton_prefix_now",
        lambda tool, prefix, ce_root, budget: (order.append("prefix"), "retired")[1],
    )
    monkeypatch.setattr(
        ce_launch_module.os, "killpg", lambda pid, sig: order.append(f"signal:{sig}"),
    )
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: False)

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    supervisor._processes["self-test"] = _Process(4242)  # type: ignore[assignment]
    supervisor._operations["self-test"] = {
        "mode": ce_launch_module.MODE_SELF_TEST,
        "plan": {
            "tool_id": "a" * 64, "tool_name": "Proton", "tool_path": str(tmp_path / "proton"),
            "proton_sha256": "b" * 64, "compat_data_path": str(tmp_path / "prefix"),
        },
    }

    with caplog.at_level(logging.INFO, logger="test"):
        supervisor.begin_close()

    assert order == ["prefix", f"signal:{signal.SIGTERM}", "prefix"]
    stopped = [record.getMessage() for record in caplog.records if "self_test_stopped" in record.getMessage()]
    assert stopped and "prefix=retired" in stopped[0]


def test_only_the_sweep_after_the_group_decides_the_prefix(tmp_path: Path, monkeypatch, caplog):
    """A prefix that was absent before the signal can exist a moment later.

    Proton is still alive while the first sweep runs, so `absent` from it means
    only that `pfx` did not exist yet. Taking that as the verdict reports a stop
    while a Wine client created immediately afterwards is still running, which
    is the race the orderly path's second pass exists for.
    """
    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    answers = iter(["absent", "retired"])
    monkeypatch.setattr(
        ce_launch_module, "retire_owned_proton_prefix_now",
        lambda tool, prefix, ce_root, budget: next(answers, "retired"),
    )
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: False)

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    supervisor._processes["self-test"] = _Process(4242)  # type: ignore[assignment]
    supervisor._operations["self-test"] = {"mode": ce_launch_module.MODE_SELF_TEST, "plan": _PLAN(tmp_path)}

    with caplog.at_level(logging.INFO, logger="test"):
        supervisor.begin_close()

    stopped = [record.getMessage() for record in caplog.records if "self_test_stopped" in record.getMessage()]
    # The second answer is the one recorded, and the first is not kept at all.
    assert stopped and "prefix=retired" in stopped[0]
    assert "outcome=stopped" in stopped[0]


def test_a_post_group_sweep_that_cannot_answer_is_not_a_stop(tmp_path: Path, monkeypatch, caplog):
    """And when the sweep that decides cannot run, the stop is unresolved."""
    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    answers = iter(["retired", "timeout"])
    monkeypatch.setattr(
        ce_launch_module, "retire_owned_proton_prefix_now",
        lambda tool, prefix, ce_root, budget: next(answers, "timeout"),
    )
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: False)

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    supervisor._processes["self-test"] = _Process(4242)  # type: ignore[assignment]
    supervisor._operations["self-test"] = {"mode": ce_launch_module.MODE_SELF_TEST, "plan": _PLAN(tmp_path)}

    with caplog.at_level(logging.INFO, logger="test"):
        supervisor.begin_close()

    stopped = [record.getMessage() for record in caplog.records if "self_test_stopped" in record.getMessage()]
    # A courteous first sweep that did retire something cannot stand in for it.
    assert stopped and "prefix=timeout" in stopped[0]
    assert "outcome=group_gone_prefix_unproven" in stopped[0]


def _PLAN(tmp_path: Path) -> dict:
    return {
        "tool_id": "a" * 64, "tool_name": "Proton", "tool_path": str(tmp_path / "proton"),
        "proton_sha256": "b" * 64, "compat_data_path": str(tmp_path / "prefix"),
    }


def test_a_stop_whose_prefix_is_unsettled_is_not_reported_as_a_stop(tmp_path: Path, monkeypatch, caplog):
    """An empty process group is not proof that Cheat Engine is gone.

    That is this supervisor's own rule, and the prologue has to keep it: a Wine
    client reaches a process group of its own under Proton, so the pass that
    answers for those is the prefix one. When that pass cannot run, because the
    tool or the prefix identity will not resolve, the stop is unresolved and
    says so rather than borrowing the group's answer.
    """
    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: False)
    monkeypatch.setattr(
        ce_launch_module, "retire_owned_proton_prefix_now",
        lambda tool, prefix, ce_root, budget: "unavailable",
    )

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    supervisor._processes["self-test"] = _Process(4242)  # type: ignore[assignment]
    supervisor._operations["self-test"] = {
        "mode": ce_launch_module.MODE_SELF_TEST,
        "plan": {
            "tool_id": "a" * 64, "tool_name": "Proton", "tool_path": str(tmp_path / "proton"),
            "proton_sha256": "b" * 64, "compat_data_path": str(tmp_path / "prefix"),
        },
    }

    with caplog.at_level(logging.INFO, logger="test"):
        supervisor.begin_close()

    stopped = [record.getMessage() for record in caplog.records if "self_test_stopped" in record.getMessage()]
    assert stopped
    assert "outcome=group_gone_prefix_unproven" in stopped[0]
    assert "group=stopped" in stopped[0] and "prefix=unavailable" in stopped[0]


def test_a_spent_prologue_budget_still_signals_every_owned_group(tmp_path: Path, monkeypatch, caplog):
    """The expensive half is what a spent budget drops, never the signal.

    Decky allows five seconds and everything else on the unload path waits
    behind this, so the prologue is bounded as a whole rather than per launch.
    A group left running is worse than one stopped without its prefix proven,
    so the syscall always goes and the verdict says the prefix was not settled.
    """
    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TOTAL_SECONDS", 0.0)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    signalled: list[int] = []
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: signalled.append(pid))
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: False)
    retired: list[object] = []
    monkeypatch.setattr(
        ce_launch_module, "retire_owned_proton_prefix_now",
        lambda tool, prefix, ce_root, budget: (retired.append(budget), "retired")[1],
    )

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    plan = {
        "tool_id": "a" * 64, "tool_name": "Proton", "tool_path": str(tmp_path / "proton"),
        "proton_sha256": "b" * 64, "compat_data_path": str(tmp_path / "prefix"),
    }
    supervisor._processes["one"] = _Process(4242)  # type: ignore[assignment]
    supervisor._processes["two"] = _Process(4343)  # type: ignore[assignment]
    supervisor._operations["one"] = {"mode": ce_launch_module.MODE_SELF_TEST, "plan": plan}
    supervisor._operations["two"] = {"mode": ce_launch_module.MODE_SELF_TEST, "plan": plan}

    with caplog.at_level(logging.INFO, logger="test"):
        supervisor.begin_close()

    assert signalled == [4242, 4343]
    assert retired == []
    stopped = [record.getMessage() for record in caplog.records if "self_test_stopped" in record.getMessage()]
    assert len(stopped) == 2
    assert all("prefix=budget_spent" in message for message in stopped)
    assert all("outcome=group_gone_prefix_unproven" in message for message in stopped)


def test_the_stop_verdict_reads_both_halves(tmp_path: Path):
    """The whole rule in one place, including what counts as settled."""
    import ce_decky.ce_launch as ce_launch_module

    verdict = ce_launch_module._self_test_stop_verdict
    # A prefix Proton never created holds nothing to retire.
    assert verdict("stopped", "absent") == "stopped"
    assert verdict("killed", "retired") == "stopped"
    assert verdict("already_gone", "retired") == "stopped"
    # Neither half may be borrowed from the other.
    assert verdict("stopped", "timeout") == "group_gone_prefix_unproven"
    assert verdict("already_gone", "no_plan") == "group_gone_prefix_unproven"
    assert verdict("still_running", "retired") == "still_running"
    assert verdict("signal_refused", "retired") == "signal_refused"


def test_a_self_test_with_no_readable_plan_still_has_its_group_stopped(tmp_path: Path, monkeypatch):
    """A plan that cannot be read costs the prefix pass, never the signal."""
    import signal

    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    sent: list[int] = []
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: sent.append(sig))
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: False)

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    supervisor._processes["self-test"] = _Process(4242)  # type: ignore[assignment]
    supervisor._operations["self-test"] = {"mode": ce_launch_module.MODE_SELF_TEST, "plan": None}

    supervisor.begin_close()

    assert sent == [signal.SIGTERM]


def test_a_group_that_survives_even_the_kill_is_reported_rather_than_claimed(tmp_path: Path, monkeypatch):
    """An unreadable or surviving group is never reported as stopped."""
    import signal

    import ce_decky.ce_launch as ce_launch_module

    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_TERM_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_KILL_SECONDS", 0.02)
    monkeypatch.setattr(ce_launch_module, "BEGIN_CLOSE_POLL_SECONDS", 0.01)
    paths = PluginPaths.for_tests(tmp_path)
    supervisor = CELaunchSupervisor(paths.user_home, paths.ce_root, paths.state_root, logging.getLogger("test"))
    monkeypatch.setattr(ce_launch_module.os, "killpg", lambda pid, sig: None)
    monkeypatch.setattr(ce_launch_module, "_process_group_exists", lambda pgid: True)

    assert supervisor._stop_group_now("self-test", 4242) == "still_running"


def test_a_stop_proceeds_when_the_quiesce_cannot_be_asked_or_answered(tmp_path: Path):
    """The stop must never become less reliable than the stop that does none of this.

    Putting the session's records down first is what keeps a game from being
    left patched, but a bridge that is not answering, a game that has exited
    and a quiesce that raises are all the stop's business to ignore: it goes
    ahead and reports what the attempt said.
    """
    supervisor, _ = _supervisor(tmp_path)
    asked: list[int] = []

    def refuses(app_id: int) -> dict[str, object]:
        asked.append(app_id)
        raise ValueError("the resident bridge is not attached")

    supervisor._quiesce = refuses
    result = asyncio.run(supervisor.stop_for_app(220))

    assert asked == []  # nothing owned, so there was nothing to ask about
    assert result["stopped"] is False

    # And with something to stop, a refusal is recorded rather than raised.
    supervisor._quiesce = refuses
    record = {
        "session_id": "11111111-2222-4333-8444-555555555555",
        "pgid": 999999, "descriptor_windows_path": "Z:\\descriptor.txt",
        "descriptor_sha256": "c" * 64,
    }
    supervisor.recover_owned_launch = lambda app_id: record
    supervisor._terminate_recovered = lambda held: True
    result = asyncio.run(supervisor.stop_for_app(220))

    assert asked == [220]
    assert result["stopped"] is True
    # And nothing was established about the game, which the answer says rather
    # than leaving the panel to read a verdict-free outcome as a clean stop.
    assert result["quiesce"] == {
        "asked": False, "reason": "the resident bridge is not attached", "cleanup_confirmed": False,
    }
