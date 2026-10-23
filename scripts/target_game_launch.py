#!/usr/bin/env python3
"""Start a game on this device through Steam, or end it, and say when it has.

A device check that needs a running game used to start it by hand: a gameid
worked out in the shell for a non-Steam shortcut, a `steam://rungameid` URI and
a sleep long enough to guess that it had come up. Ending it was worse: a
`pkill -f` on the executable's name matched the shell that ran it as well as
the game, ended the check midway and signalled whatever else carried that text.

`launch` hands Steam its own URI for a game that is not running, and only when
no game Steam launched is running at all: `rungameid` on a running game is
refused and moves the input context off it, which is what `target_screenshot.py`
records, and whether to replace one running game with another is a question for
the person at the device rather than for a helper. It then waits, bounded,
for Steam to declare the AppID in a process environment, which is how the
plugin itself observes a running game.

`stop` ends that game's processes by PID and never by pattern: every process
whose environment declares this exact `SteamAppId`, never one of CE Decky's own
and never this helper or a process above it. It refuses while CE Decky's own
Cheat Engine is still attached to that game, because ending a game under a live
session is the plugin's stop to make, from the panel. It sends TERM, waits for
the AppID to leave the process table, and sends KILL only to what is still
there after that, saying so.

Whether an AppID is a non-Steam shortcut comes from the plugin's profile for
it, which took it from the library, or from `--shortcut`/`--steam-app` when no
profile names it. It is never inferred from the number.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

if __package__:
    from . import host_platform
else:
    import host_platform

ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "py_modules"
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from ce_decky.atomic import read_proc_bytes  # noqa: E402
from ce_decky.ce_launch import (  # noqa: E402
    MAX_ENVIRON_BYTES,
    is_ce_decky_owned_environment,
    observe_any_running_steam_game,
    observe_running_app_ids,
    parse_environ,
)
from ce_decky.profiles import ProfileStore  # noqa: E402

# Steam's own gameid for a non-Steam shortcut: the shortcut's AppID in the high
# word and the shortcut type in the low one. A Steam app's gameid is its AppID.
SHORTCUT_GAMEID_TYPE = 0x02000000
MAX_APP_ID = 0xFFFFFFFF
MAX_SCANNED = 32768
EXIT_REFUSED = 2


def game_id(app_id: int, *, shortcut: bool) -> int:
    """The gameid `steam://rungameid` takes for this AppID."""
    if app_id < 1 or app_id > MAX_APP_ID:
        raise ValueError("AppID must be between 1 and 4294967295")
    return (app_id << 32) | SHORTCUT_GAMEID_TYPE if shortcut else app_id


def _ancestors(pid: int, proc_root: Path) -> set[int]:
    """This process and every process above it, which a stop may never signal."""
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            status = (proc_root / str(pid) / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            break
        parent = next((line.split()[1] for line in status.splitlines() if line.startswith("PPid:")), "0")
        pid = int(parent) if parent.isdigit() else 0
    return seen


def game_processes(app_id: int, proc_root: Path, protected: set[int]) -> tuple[list[int], list[int]]:
    """The PIDs declaring this AppID, and those of them CE Decky owns.

    Read the same way the plugin reads a running game, from `SteamAppId` in the
    environment Steam gave it, so the set is the launch Steam made rather than
    whatever happens to mention the executable's name.
    """
    game: list[int] = []
    owned: list[int] = []
    try:
        entries = sorted((entry for entry in os.scandir(proc_root) if entry.name.isdigit()), key=lambda entry: int(entry.name))
    except OSError as exc:
        raise ValueError(f"the process table is unreadable: {exc}") from exc
    if len(entries) > MAX_SCANNED:
        raise ValueError("the process table exceeds the bounded scan")
    wanted = str(app_id)
    for entry in entries:
        pid = int(entry.name)
        if pid in protected:
            continue
        try:
            raw = read_proc_bytes(Path(entry.path) / "environ", max_bytes=MAX_ENVIRON_BYTES)
        except (OSError, ValueError):
            continue
        if not raw:
            continue
        environ = parse_environ(raw)
        if environ.get("SteamAppId") != wanted:
            continue
        (owned if is_ce_decky_owned_environment(environ) else game).append(pid)
    return game, owned


def _is_shortcut(app_id: int, flag: bool | None, profiles: Path | None) -> bool:
    if flag is not None:
        return flag
    if profiles is not None and profiles.is_file():
        profile = ProfileStore(profiles).get(app_id)
        if profile is not None:
            return profile.is_shortcut
    raise ValueError("no CE Decky profile names this AppID; say which it is with --shortcut or --steam-app")


def _profiles_path() -> Path | None:
    if __package__:
        from .target_plugin_install import resolve_install_authority
    else:
        from target_plugin_install import resolve_install_authority
    try:
        home = resolve_install_authority().get("user_home")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            f"CE Decky's profiles could not be found ({exc}); say which it is with --shortcut or --steam-app"
        ) from exc
    return Path(str(home)) / ".cheat-engine-decky" / "state" / "profiles.json" if home else None


def launch(
    app_id: int,
    *,
    shortcut: bool,
    wait_seconds: float,
    proc_root: Path = Path("/proc"),
    run: Callable[[list[str]], object] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    observed = observe_any_running_steam_game(proc_root=proc_root)
    if not observed.available:
        raise ValueError(f"cannot tell whether a game is running: {observed.reason}")
    if observed.running:
        running = observe_running_app_ids(proc_root=proc_root)
        raise ValueError(
            "a game Steam launched is already running"
            + (f" (AppID {', '.join(map(str, running.app_ids))})" if running.app_ids else "")
            + "; end that one first, because replacing a running game is not this helper's to decide"
        )
    gameid = game_id(app_id, shortcut=shortcut)
    url = f"steam://rungameid/{gameid}"
    # Detached: the `steam` client hands the URI to the one already running and
    # may stay up while it does, and what answers whether the game started is
    # the process table rather than that command's exit.
    runner = run or (lambda command: subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    ))
    started = clock()
    runner(["steam", url])
    while True:
        running = observe_running_app_ids(proc_root=proc_root)
        if running.available and app_id in running.app_ids:
            return {"app_id": app_id, "gameid": gameid, "url": url, "running": True,
                    "seconds": round(clock() - started, 1)}
        if clock() - started >= wait_seconds:
            return {"app_id": app_id, "gameid": gameid, "url": url, "running": False,
                    "seconds": round(clock() - started, 1)}
        sleep(2.0)


def stop(
    app_id: int,
    *,
    wait_seconds: float,
    proc_root: Path = Path("/proc"),
    kill: Callable[[int, int], None] = os.kill,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    own_pid: int | None = None,
) -> dict[str, object]:
    protected = _ancestors(own_pid if own_pid is not None else os.getpid(), proc_root)
    game, owned = game_processes(app_id, proc_root, protected)
    if owned:
        raise ValueError(
            "CE Decky's own Cheat Engine is still attached to this game; stop it from the panel first"
        )
    if not game:
        return {"app_id": app_id, "running": False, "terminated": [], "killed": [], "seconds": 0.0}

    def signal_all(pids: list[int], number: int) -> list[int]:
        sent: list[int] = []
        for pid in pids:
            try:
                kill(pid, number)
                sent.append(pid)
            except ProcessLookupError:
                continue
        return sent

    started = clock()
    terminated = signal_all(game, signal.SIGTERM)
    while True:
        left, owned = game_processes(app_id, proc_root, protected)
        if not left or clock() - started >= wait_seconds:
            break
        sleep(1.0)
    killed = signal_all(left, signal.SIGKILL) if left else []
    return {"app_id": app_id, "running": False, "terminated": terminated, "killed": killed,
            "seconds": round(clock() - started, 1)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (("launch", "start a game that is not running and wait until Steam declares it"),
                       ("stop", "end a running game's own processes by PID")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--appid", type=int, required=True, help="the exact AppID")
        sub.add_argument("--wait-seconds", type=float, default=90.0 if name == "launch" else 20.0,
                         help="how long to wait, at most 300")
        if name == "launch":
            kind = sub.add_mutually_exclusive_group()
            kind.add_argument("--shortcut", dest="shortcut", action="store_true", default=None,
                              help="the AppID is a non-Steam shortcut; needed only when no CE Decky profile names it")
            kind.add_argument("--steam-app", dest="shortcut", action="store_false",
                              help="the AppID is a Steam app; needed only when no CE Decky profile names it")
        sub.add_argument("--json", action="store_true", help="print the whole answer as JSON")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The game launcher")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if not 1 <= args.wait_seconds <= 300:
        parser.error("--wait-seconds must be between 1 and 300")
    try:
        if args.command == "launch":
            shortcut = _is_shortcut(args.appid, args.shortcut, None if args.shortcut is not None else _profiles_path())
            answer = launch(args.appid, shortcut=shortcut, wait_seconds=args.wait_seconds)
            line = (f"game launch: AppID {args.appid} is running after {answer['seconds']}s"
                    if answer["running"] else
                    f"game launch: Steam was asked ({answer['url']}) and AppID {args.appid} was not running after {answer['seconds']}s")
        else:
            answer = stop(args.appid, wait_seconds=args.wait_seconds)
            if not answer["terminated"]:
                line = f"game stop: AppID {args.appid} was not running"
            else:
                line = (f"game stop: AppID {args.appid} ended; TERM to {len(answer['terminated'])} processes"
                        + (f", KILL to {len(answer['killed'])} still there after {args.wait_seconds:g}s" if answer["killed"] else "")
                        + f", {answer['seconds']}s")
    except (OSError, ValueError) as exc:
        print(f"game {args.command}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(json.dumps(answer, indent=2, sort_keys=True) if args.json else line)
    if args.command == "launch" and not answer["running"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
