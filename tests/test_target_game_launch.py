"""Starting and ending a game for a device check, by Steam's own route and by PID."""
from __future__ import annotations

import signal
from pathlib import Path

import pytest

from scripts import target_game_launch as launcher


def _process(proc: Path, pid: int, parent: int, environ: dict[str, str]) -> None:
    root = proc / str(pid)
    root.mkdir(parents=True)
    (root / "environ").write_bytes(b"\0".join(f"{key}={value}".encode() for key, value in environ.items()) + b"\0")
    (root / "status").write_text(f"Name:\tx\nPPid:\t{parent}\n", encoding="utf-8")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_a_shortcuts_gameid_is_steams_own_and_a_steam_apps_is_its_appid():
    # The number a shortcut launch needs, which a shell computed signed.
    assert launcher.game_id(3000000123, shortcut=True) == (3000000123 << 32) | 0x02000000
    assert launcher.game_id(3000000123, shortcut=True) > 0
    assert launcher.game_id(900001, shortcut=False) == 900001
    with pytest.raises(ValueError):
        launcher.game_id(0, shortcut=False)


def test_launch_asks_steam_and_waits_until_the_appid_is_declared(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    clock = Clock()
    asked: list[list[str]] = []

    def run(command: list[str]) -> None:
        asked.append(command)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now >= 4 and not (proc / "900").exists():
            _process(proc, 900, 1, {"SteamAppId": "900001", "SteamGameId": "900001"})

    answer = launcher.launch(900001, shortcut=False, wait_seconds=30, proc_root=proc, run=run, clock=clock, sleep=sleep)

    assert asked == [["steam", "steam://rungameid/900001"]]
    assert answer["running"] is True and answer["seconds"] == 4.0


def test_launch_is_refused_while_another_game_runs(tmp_path: Path):
    # A URI for a running game moves the input context off it, and replacing
    # one game with another is not the helper's to decide.
    proc = tmp_path / "proc"
    _process(proc, 900, 1, {"SteamAppId": "900002", "SteamGameId": "900002"})
    asked: list[list[str]] = []

    with pytest.raises(ValueError, match="already running"):
        launcher.launch(900001, shortcut=False, wait_seconds=5, proc_root=proc, run=asked.append)
    assert asked == []


def test_launch_says_when_the_game_never_came_up(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    clock = Clock()

    answer = launcher.launch(900001, shortcut=False, wait_seconds=10, proc_root=proc, run=lambda _: None, clock=clock, sleep=clock.sleep)

    assert answer["running"] is False and answer["seconds"] >= 10


def test_stop_signals_the_games_processes_by_pid_and_nothing_that_merely_names_it(tmp_path: Path):
    # A pattern on the executable's name matched the shell that ran it. The set
    # here is what Steam declared for this AppID, minus this helper's own line.
    proc = tmp_path / "proc"
    _process(proc, 100, 1, {"HOME": "/home/user"})                          # the shell
    _process(proc, 101, 100, {"SteamAppId": "900001"})                        # this helper, launched from a game
    _process(proc, 200, 1, {"SteamAppId": "900001"})                          # reaper
    _process(proc, 201, 200, {"SteamAppId": "900001", "SteamGameId": "900001"})  # the game
    _process(proc, 300, 1, {"SteamAppId": "900002"})                          # another game
    sent: list[tuple[int, int]] = []

    def kill(pid: int, number: int) -> None:
        sent.append((pid, number))
        for child in (proc / str(pid)).iterdir():
            child.unlink()
        (proc / str(pid)).rmdir()

    answer = launcher.stop(900001, wait_seconds=10, proc_root=proc, kill=kill, own_pid=101)

    assert sent == [(200, signal.SIGTERM), (201, signal.SIGTERM)]
    assert answer["terminated"] == [200, 201] and answer["killed"] == []


def test_stop_kills_only_what_outlived_the_wait(tmp_path: Path):
    proc = tmp_path / "proc"
    _process(proc, 200, 1, {"SteamAppId": "900001"})
    clock = Clock()
    sent: list[tuple[int, int]] = []

    answer = launcher.stop(900001, wait_seconds=5, proc_root=proc, kill=lambda pid, number: sent.append((pid, number)),
                           clock=clock, sleep=clock.sleep, own_pid=999)

    assert sent == [(200, signal.SIGTERM), (200, signal.SIGKILL)]
    assert answer["killed"] == [200]


def test_stop_is_refused_while_ce_deckys_own_cheat_engine_is_attached(tmp_path: Path):
    # Ending a game under a live session is the plugin's stop to make.
    proc = tmp_path / "proc"
    _process(proc, 200, 1, {"SteamAppId": "900001"})
    _process(proc, 250, 1, {"SteamAppId": "900001", "CE_DECKY_DESCRIPTOR": "x", "CE_DECKY_DESCRIPTOR_SHA256": "y"})
    sent: list[tuple[int, int]] = []

    with pytest.raises(ValueError, match="stop it from the panel first"):
        launcher.stop(900001, wait_seconds=5, proc_root=proc, kill=lambda pid, number: sent.append((pid, number)), own_pid=999)
    assert sent == []


def test_a_shortcut_is_taken_from_the_profile_or_from_the_caller_never_from_the_number(tmp_path: Path):
    from ce_decky.profiles import ProfileStore

    profiles = tmp_path / "profiles.json"
    ProfileStore(profiles).upsert(app_id=3000000123, name="Safe Game", is_shortcut=True, table_sha256=None, target_process=None)

    assert launcher._is_shortcut(3000000123, None, profiles) is True
    assert launcher._is_shortcut(3000000123, False, profiles) is False
    with pytest.raises(ValueError, match="--shortcut or --steam-app"):
        launcher._is_shortcut(3000000999, None, profiles)


def test_stop_finds_a_shortcut_by_its_gameid_and_nothing_else_by_one(tmp_path: Path):
    # A shortcut's processes may carry only the 64-bit gameid. Matching only
    # SteamAppId reported such a game as not running and left it up.
    proc = tmp_path / "proc"
    shortcut = 3000000123
    gameid = str(launcher.game_id(shortcut, shortcut=True))
    _process(proc, 200, 1, {"SteamGameId": gameid})                                   # gameid only
    _process(proc, 201, 1, {"SteamAppId": str(shortcut), "SteamGameId": gameid})       # both
    _process(proc, 202, 1, {"SteamGameId": str(launcher.game_id(3000000999, shortcut=True))})  # another shortcut
    _process(proc, 203, 1, {"SteamGameId": "900001"})                                 # a Steam game
    sent: list[int] = []

    def kill(pid: int, number: int) -> None:
        sent.append(pid)
        for child in (proc / str(pid)).iterdir():
            child.unlink()
        (proc / str(pid)).rmdir()

    answer = launcher.stop(shortcut, wait_seconds=5, proc_root=proc, kill=kill, own_pid=999)

    assert sorted(sent) == [200, 201]
    assert answer["running"] is False and answer["survivors"] == []


def test_stopping_a_store_app_leaves_a_shortcut_that_calls_itself_that_app(tmp_path: Path):
    # A GOG build that ships `steam_appid.txt` runs as a shortcut whose
    # processes carry the store app's SteamAppId; its prefix says what it is.
    proc = tmp_path / "proc"
    shortcut = 3000000123
    compat = f"/lib/steamapps/compatdata/{shortcut}"
    gameid = str(launcher.game_id(shortcut, shortcut=True))
    _process(proc, 300, 1, {"SteamAppId": "424242", "SteamGameId": gameid, "STEAM_COMPAT_DATA_PATH": compat})
    _process(proc, 301, 1, {"SteamAppId": "424242", "STEAM_COMPAT_DATA_PATH": "/lib/steamapps/compatdata/424242"})

    assert launcher.game_processes(424242, proc, set()) == ([301], [])
    assert launcher.game_processes(shortcut, proc, set()) == ([300], [])


def test_stop_does_not_call_the_game_stopped_while_a_process_outlives_kill(tmp_path: Path):
    proc = tmp_path / "proc"
    _process(proc, 200, 1, {"SteamAppId": "900001"})
    clock = Clock()

    answer = launcher.stop(900001, wait_seconds=5, proc_root=proc, kill=lambda pid, number: None,
                           clock=clock, sleep=clock.sleep, own_pid=999)

    assert answer["running"] is True and answer["survivors"] == [200] and answer["killed"] == [200]


def _library(monkeypatch, *, steam=(), unstartable=(), shortcuts=(), programs=None, reason=None):
    monkeypatch.setattr(launcher, "local_library", lambda home: {
        "steam_app_ids": list(steam), "unstartable_app_ids": list(unstartable),
        "shortcut_app_ids": list(shortcuts), "reason": reason, "shortcuts_reason": reason,
    })
    monkeypatch.setattr(launcher, "shortcut_program", lambda home, app_id: (programs or {}).get(app_id))


def test_a_game_this_device_does_not_hold_is_not_started(monkeypatch, tmp_path: Path):
    """A profile outlives its game, and Steam's URI for one that is not here offers to download it."""
    _library(monkeypatch, steam=[620, 228980], unstartable=[228980], shortcuts=[3000000123],
             programs={3000000123: "/games/x/Game.EXE"})
    reason = launcher.not_here_reason
    assert reason(620, shortcut=False, user_home=tmp_path, through_launcher=False) is None
    assert "fully installed" in reason(621, shortcut=False, user_home=tmp_path, through_launcher=False)
    assert "no way to start" in reason(228980, shortcut=False, user_home=tmp_path, through_launcher=False)
    assert reason(3000000123, shortcut=True, user_home=tmp_path, through_launcher=False, target_process="game.exe") is None
    assert "shortcuts.vdf" in reason(3000000124, shortcut=True, user_home=tmp_path, through_launcher=False)
    # A read in part vouches for what it listed and for nothing it did not.
    _library(monkeypatch, steam=[620], shortcuts=[3000000123], programs={3000000123: "/games/x/Game.exe"},
             reason="a Steam library on this device could not be read whole")
    assert reason(620, shortcut=False, user_home=tmp_path, through_launcher=False) is None
    assert reason(3000000123, shortcut=True, user_home=tmp_path, through_launcher=False, target_process="Game.exe") is None
    assert "unknown" in reason(621, shortcut=False, user_home=tmp_path, through_launcher=False)
    assert "unknown" in reason(3000000124, shortcut=True, user_home=tmp_path, through_launcher=False)


def test_a_shortcut_that_does_not_start_the_game_itself_waits_for_through_launcher(monkeypatch, tmp_path: Path):
    """A store front's launcher is on the disk whether or not the game behind it is, and may install it.

    Its name and whether it is a Windows program are no test of one, so a
    launcher `.exe`, a native one, a command and a game with no target process
    are all refused alike.
    """
    programs = {3000000123: "/plugins/Store/bin/store-launcher", 3000000124: "/games/x/Launcher.exe",
                3000000125: "/games/x/Game.exe"}
    _library(monkeypatch, shortcuts=[3000000123, 3000000124, 3000000125, 3000000126], programs=programs)
    for app_id, target in ((3000000123, "Game.exe"), (3000000124, "Game.exe"), (3000000125, None), (3000000126, "Game.exe")):
        refusal = launcher.not_here_reason(app_id, shortcut=True, user_home=tmp_path, through_launcher=False,
                                           target_process=target)
        assert refusal and "--through-launcher" in refusal
        assert launcher.not_here_reason(app_id, shortcut=True, user_home=tmp_path, through_launcher=True,
                                        target_process=target) is None


def test_launch_refuses_before_asking_steam(monkeypatch, tmp_path: Path, capsys):
    _library(monkeypatch)
    monkeypatch.setattr(launcher.host_platform, "require", lambda name: None)
    monkeypatch.setattr(launcher, "_user_home", lambda: tmp_path)
    monkeypatch.setattr(launcher, "launch", lambda *args, **kwargs: pytest.fail("Steam was asked"))
    assert launcher.main(["launch", "--appid", "621", "--steam-app"]) == launcher.EXIT_REFUSED
    assert "AppID 621 is not started" in capsys.readouterr().err
