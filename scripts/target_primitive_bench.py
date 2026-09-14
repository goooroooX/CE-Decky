#!/usr/bin/env python3
"""Time the production primitives a live session repeats, on the machine it runs on.

What the backend costs while a session is attached is a figure for the whole
process. The repeating-path counters in the backend say which of its loops was
awake and what each one spent, and this says what one pass of the primitives
underneath them costs, so the two together turn a total into an attribution
instead of an argument.

Every function here is the production one, imported from `py_modules`. Nothing
is reimplemented for the measurement, because a reimplementation measures
itself: the process-table walk's cost is in how many files it opens per
candidate and how large the table is, and a simplified walk would agree with it
only by accident.

The Cheat Engine executable is taken from the live backend when it is not
named, because the backend is the authority on which one it registered and
reading its configuration file from here would be a second copy of that fact.
A backend that is not running leaves that one row out and says why, rather than
ending the run.

It measures a pass, not a cadence. Multiply a median by the interval its caller
actually runs at, and the report does that for the one-second and three-second
cadences beside each row so a reading can be compared with a measured backend
figure directly.

Read only. It opens procfs and the Steam library for reading, hashes files that
are already on disk, and writes nothing. Timings are per thread, so what is
reported is time on a core rather than time alive, which on a device held at a
power limit are different things.

The size of the process table is reported with every row. The walk's cost scales
with it, so a median without it cannot be compared against one from another
machine, or from the same machine on a quieter day.

    python3 scripts/target_primitive_bench.py --home "$DECKY_USER_HOME" \
        --appid <appid> --target-process <game.exe> --repeat 9
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import statistics
import sys
import time
from typing import Callable

if __package__:
    from . import host_platform
else:
    import host_platform

ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "py_modules"
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

DEFAULT_DECKY_URL = "http://127.0.0.1:1337"
DEFAULT_REPEAT = 7
MAX_REPEAT = 200
DEFAULT_WARMUP = 1
PROC_ROOT = Path("/proc")
# The cadences the plan is about: the supervisor's baseline tick, and both the
# target scan and the panel's own detector.
REPORTED_CADENCES_S = (1.0, 3.0)


@dataclass(frozen=True)
class Case:
    name: str
    call: Callable[[], object]
    detail: str


def process_table_size(proc_root: Path = PROC_ROOT) -> int:
    try:
        return sum(1 for entry in proc_root.iterdir() if entry.name.isdigit())
    except OSError:
        return -1


def _percentile(values: list[float], fraction: float) -> float:
    """The nearest-rank percentile, which needs no interpolation to defend.

    Seven repetitions cannot support an interpolated p95 in any case, and a rank
    is what a reader can check against the raw list.
    """
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(-(-len(ordered) * fraction // 1))))
    return ordered[rank - 1]


def time_case(case: Case, repeat: int, warmup: int, proc_root: Path = PROC_ROOT) -> dict[str, object]:
    """Run one primitive `repeat` times and report what a pass of it cost."""
    error: str | None = None
    cpu_ms: list[float] = []
    wall_ms: list[float] = []
    table_before = process_table_size(proc_root)
    try:
        for _ in range(warmup):
            case.call()
        for _ in range(repeat):
            started_cpu = time.thread_time()
            started_wall = time.perf_counter()
            case.call()
            cpu_ms.append((time.thread_time() - started_cpu) * 1000.0)
            wall_ms.append((time.perf_counter() - started_wall) * 1000.0)
    except Exception as exc:  # noqa: BLE001 - a primitive that refuses is a result
        error = f"{type(exc).__name__}: {exc}"
    row: dict[str, object] = {
        "name": case.name,
        "detail": case.detail,
        "repetitions": len(cpu_ms),
        "processes_before": table_before,
        "processes_after": process_table_size(proc_root),
    }
    if error is not None:
        row["error"] = error
    if not cpu_ms:
        return row
    row["cpu_ms"] = {
        "median": round(statistics.median(cpu_ms), 3),
        "p95": round(_percentile(cpu_ms, 0.95), 3),
        "min": round(min(cpu_ms), 3),
        "max": round(max(cpu_ms), 3),
    }
    row["wall_ms"] = {
        "median": round(statistics.median(wall_ms), 3),
        "p95": round(_percentile(wall_ms, 0.95), 3),
        "min": round(min(wall_ms), 3),
        "max": round(max(wall_ms), 3),
    }
    # What one pass at a given cadence would cost, so a row can be held against
    # a measured backend figure without arithmetic in the reader's head.
    row["implied_percent_of_core"] = {
        f"every_{cadence:g}s": round(statistics.median(cpu_ms) / 1000.0 / cadence * 100.0, 3)
        for cadence in REPORTED_CADENCES_S
    }
    return row


def registered_ce_executable(decky_url: str, timeout: float = 5.0) -> tuple[Path | None, str | None]:
    """The Cheat Engine this plugin has registered, from the backend that registered it.

    The path is in the plugin's own configuration, and reading that file from a
    developer helper is a second copy of a fact the live backend already
    reports, which is the arrangement the install authority exists to replace.
    Ask the backend instead, and validate the answer here rather than trusting
    it: an executable that is not an absolute path to a real file is refused,
    the same way `target_plugin_install.py` refuses a settings directory that is
    not shaped like one.

    Returns the path, or the reason there is none. A backend that is not running
    is a reason, never an exception: the rest of the run is still measurable.
    """
    try:
        if __package__:
            from .target_plugin_install import DeckyWebSocket, PLUGIN_NAME, _auth_token, _await_reply
        else:
            from target_plugin_install import DeckyWebSocket, PLUGIN_NAME, _auth_token, _await_reply
    except ImportError as exc:
        return None, f"the Decky client is unavailable here: {exc}"
    try:
        token = _auth_token(decky_url, timeout)
        with DeckyWebSocket.connect(decky_url, token, timeout) as ws:
            ws.send_json({
                "type": 0,
                "route": "loader/call_plugin_method",
                "args": [PLUGIN_NAME, "get_status"],
                "id": 30,
            })
            status = _await_reply(ws, 30)
    except Exception as exc:  # noqa: BLE001 - an unreachable backend is a reason, not the end of the run
        return None, f"{type(exc).__name__}: {exc}"
    ce = status.get("ce") if isinstance(status, dict) else None
    value = ce.get("executable") if isinstance(ce, dict) else None
    if not isinstance(value, str) or not value:
        return None, "the live backend has no Cheat Engine registered"
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None, "the live backend reported a Cheat Engine path that is not absolute"
    if not candidate.is_file():
        return None, f"the registered Cheat Engine executable is not a file: {candidate}"
    return candidate, None


def build_cases(
    home: Path,
    app_id: int | None,
    target_process: str | None,
    baseline_pid: int | None,
    ce_executable: Path | None,
) -> list[Case]:
    """Every primitive whose inputs this run was actually given.

    A primitive is left out rather than run against an invented input: timing
    `observe_game_container` for an AppID that is not running measures the
    cheapest state it has, and the plan already records that reading as a floor
    rather than as the cost.
    """
    from ce_decky.ce_import import inspect_ce_selection
    from ce_decky.ce_launch import game_process_state, game_target_state, observe_game_container
    from ce_decky.managed_ce import discover_proton_tools

    cases: list[Case] = [
        Case(
            "discover_proton_tools",
            lambda: discover_proton_tools(home),
            "enumerates installed Proton tools and hashes each proton script",
        ),
    ]
    if app_id is not None:
        cases.append(Case(
            "observe_game_container",
            lambda: observe_game_container(app_id),
            "walks the process table and reads per-process files for candidates",
        ))
    if app_id is not None and target_process:
        cases.append(Case(
            "game_target_state",
            lambda: game_target_state(app_id, target_process),
            "the supervisor's scan: one container observation plus the executable match",
        ))
    if app_id is not None and baseline_pid is not None:
        cases.append(Case(
            "game_process_state",
            lambda: game_process_state(baseline_pid, app_id),
            "the one-second baseline check: one process environment read",
        ))
    if ce_executable is not None:
        cases.append(Case(
            "inspect_ce_selection",
            lambda: inspect_ce_selection(str(ce_executable)),
            "hashes the Cheat Engine executable and reads its version",
        ))
    return cases


def bench(
    home: Path,
    app_id: int | None,
    target_process: str | None,
    baseline_pid: int | None,
    ce_executable: Path | None,
    repeat: int,
    warmup: int,
    decky_url: str = DEFAULT_DECKY_URL,
) -> dict[str, object]:
    host = host_platform.describe()
    ce_source = "argument"
    ce_reason: str | None = None
    if ce_executable is None:
        ce_executable, ce_reason = registered_ce_executable(decky_url)
        ce_source = "live_backend" if ce_executable is not None else "unresolved"
    cases = build_cases(home, app_id, target_process, baseline_pid, ce_executable)
    rows = [time_case(case, repeat, warmup) for case in cases]
    return {
        "schema": 2,
        "mode": "read_only_primitive_timing",
        "host": host.as_dict(),
        "user_home": str(home),
        "app_id": app_id,
        "target_process": target_process,
        "baseline_pid": baseline_pid,
        "ce_executable": None if ce_executable is None else str(ce_executable),
        "ce_executable_source": ce_source,
        "ce_executable_reason": ce_reason,
        "repetitions_requested": repeat,
        "warmup": warmup,
        "primitives": rows,
        "notes": [
            "CPU is per thread, so it is time on a core rather than time alive.",
            "A primitive whose inputs were not given is absent rather than run against an invented one.",
            "observe_game_container timed with no game running is its cheapest state, not its cost.",
            "The composite RPC paths are not here: the backend's own counters time those in production,"
            " and get_poll_counters reports them.",
            "Two of these primitives carry the backend's own counters, which cost a lock and two clock"
            " reads per pass. That is inside the production function under test and is reported as part"
            " of it, which is the honest accounting: the backend pays it too.",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, required=True,
                        help="the exact DECKY_USER_HOME; target_plugin_install.py authority reports it")
    parser.add_argument("--appid", type=int, help="AppID of the running game, without which the table walk is skipped")
    parser.add_argument("--target-process", help="the game's exact Windows executable basename")
    parser.add_argument("--baseline-pid", type=int, help="one PID recorded at launch, for the one-second baseline check")
    parser.add_argument("--ce-executable", type=Path,
                        help="the Cheat Engine executable to hash; taken from the live backend when omitted")
    parser.add_argument("--decky-url", default=DEFAULT_DECKY_URL,
                        help="Decky's local API, which the registered Cheat Engine is read through")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT, help="timed repetitions per primitive")
    parser.add_argument("--warmup", type=int, default=DEFAULT_WARMUP, help="untimed repetitions before each primitive")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        host_platform.require("The primitive timing bench")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if not 1 <= args.repeat <= MAX_REPEAT:
        print(f"target primitive bench: --repeat must be between 1 and {MAX_REPEAT}", file=sys.stderr)
        return 2
    if not 0 <= args.warmup <= MAX_REPEAT:
        print(f"target primitive bench: --warmup must be between 0 and {MAX_REPEAT}", file=sys.stderr)
        return 2
    if args.appid is not None and (args.appid < 1 or args.appid > 0xFFFFFFFF):
        print("target primitive bench: AppID must be between 1 and 4294967295", file=sys.stderr)
        return 2
    home = args.home.expanduser()
    if not home.is_dir():
        print(f"target primitive bench: the Steam user home is not a directory: {home}", file=sys.stderr)
        return 2
    report = bench(
        home, args.appid, args.target_process, args.baseline_pid,
        args.ce_executable, args.repeat, args.warmup, args.decky_url,
    )
    json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
