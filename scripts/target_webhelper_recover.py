#!/usr/bin/env python3
"""Bring a wedged Steam UI back by ending the exact webhelper processes that spin.

The ordinary recovery from a whole-UI wedge is a Steam restart by the
maintainer. That needs a controller, and a wedge can take the controller with
it: on the Steam Machine on 2026-11-05 the renderer's and the browser's main
threads spun for fifteen minutes, CEF answered nothing, and there was nobody who
could press anything. This is what was done by hand then, made exact.

It refuses unless all of this holds:

- an incident capture (`target_incident_capture.py`) made within the last half
  hour is named, because ending these processes destroys the panel's record;
- a webhelper main thread spins in both of two samples taken here, so a UI that
  is merely slow is never touched;
- Steam's Quick Access page does not answer the freeze probe with running
  JavaScript.

Then it ends, with SIGTERM and by PID, only processes whose start time is still
the one it sampled: first every renderer whose main thread spins, and only if
the UI is still not answering after that, the webhelper's browser process. Steam
starts a new webhelper by itself. It never matches by pattern, and never signals
the Steam client, Decky's loader, Cheat Engine or the game. What it did is
appended to the incident's `notes.md`, with the AppIDs running before and after.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from . import host_platform
except ImportError:  # executed as a script
    import host_platform

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
INCIDENTS = ROOT / "build" / "incidents"
CAPTURE_MAX_AGE_SECONDS = 30 * 60
SAMPLE_GAP_SECONDS = 3.0
RENDERER_SETTLE_SECONDS = 20.0
BROWSER_RESPAWN_SECONDS = 60.0


def _helper(argv: list[str], timeout: float) -> str:
    try:
        done = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return ""
    return done.stdout


def sample_threads() -> dict[str, object]:
    try:
        return json.loads(_helper([str(SCRIPTS / "target_webhelper_threads.py"), "--json"], 60))
    except ValueError:
        return {}


def spinning_mains(sample: dict[str, object]) -> dict[int, str]:
    """PID to role for every webhelper process whose own main thread spins."""
    found: dict[int, str] = {}
    for process in sample.get("processes", []) if isinstance(sample, dict) else []:
        if not isinstance(process, dict) or not isinstance(process.get("pid"), int):
            continue
        for value in process.values():
            if not isinstance(value, list):
                continue
            if any(isinstance(row, dict) and row.get("main") is True and row.get("verdict") == "spinning" for row in value):
                found[process["pid"]] = str(process.get("role") or "")
    return found


def wedged(first: dict[str, object], second: dict[str, object]) -> tuple[list[int], int | None]:
    """The renderers to end first and the browser to end after them, spinning in both samples."""
    a, b = spinning_mains(first), spinning_mains(second)
    both = {pid: role for pid, role in b.items() if pid in a}
    renderers = sorted(pid for pid, role in both.items() if role.startswith("renderer"))
    browsers = [pid for pid, role in both.items() if role == "browser"]
    return renderers, (browsers[0] if len(browsers) == 1 else None)


def quick_access_alive(probe_text: str) -> bool:
    return any(line.startswith("QuickAccess") and "javascript running" in line for line in probe_text.splitlines())


def start_time(pid: int, proc: Path = Path("/proc")) -> str | None:
    """The kernel's start time for `pid`, which a reused PID does not share."""
    try:
        stat = (proc / str(pid) / "stat").read_text()
        comm_end = stat.rindex(")")
        return stat[comm_end + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def is_webhelper(pid: int, proc: Path = Path("/proc")) -> bool:
    try:
        return (proc / str(pid) / "comm").read_text().strip() == "steamwebhelper"
    except OSError:
        return False


def _browser_pids() -> set[int]:
    sample = sample_threads()
    return {p["pid"] for p in sample.get("processes", []) if isinstance(p, dict) and p.get("role") == "browser"}


def _running_app_ids() -> str:
    text = _helper([str(SCRIPTS / "target_state_probe.py")], 90)
    try:
        return str(json.loads(text).get("running_app_ids"))
    except (ValueError, AttributeError):
        return "unread"


def _ui_alive() -> bool:
    return quick_access_alive(_helper([str(SCRIPTS / "target_ui_freeze_probe.py")], 60))


def latest_capture(root: Path = INCIDENTS) -> Path | None:
    candidates = [path.parent for path in root.glob("*/notes.md")]
    return max(candidates, key=lambda path: (path / "notes.md").stat().st_mtime, default=None)


def recover(incident: Path, *, dry_run: bool) -> int:
    log: list[str] = []

    def say(line: str) -> None:
        stamped = f"{datetime.now().astimezone().strftime('%H:%M:%S')} {line}"
        print(stamped, flush=True)
        log.append(stamped)

    def record() -> None:
        with (incident / "notes.md").open("a", encoding="utf-8") as notes:
            notes.write("\n## Recovery by `target_webhelper_recover.py`\n\n" + "\n".join(f"- {line}" for line in log) + "\n")

    first = sample_threads()
    time.sleep(SAMPLE_GAP_SECONDS)
    second = sample_threads()
    renderers, browser = wedged(first, second)
    if not renderers and browser is None:
        print("webhelper recover: no webhelper main thread spins in both samples; nothing is wedged here", file=sys.stderr)
        return 1
    if _ui_alive():
        print("webhelper recover: Quick Access is running JavaScript; the UI is not wedged", file=sys.stderr)
        return 1
    started = {pid: start_time(pid) for pid in [*renderers, *([browser] if browser else [])]}
    say(f"spinning in both samples: renderers {renderers or 'none'}, browser {browser or 'none'}")
    say(f"running AppIDs before: {_running_app_ids()}")
    if dry_run:
        say("dry run: nothing was signalled")
        record()
        return 0

    def end(pid: int, what: str) -> bool:
        if not is_webhelper(pid) or start_time(pid) != started.get(pid):
            say(f"{what} {pid} is no longer the process sampled; left alone")
            return False
        os.kill(pid, signal.SIGTERM)
        say(f"SIGTERM to {what} {pid}")
        return True

    for pid in renderers:
        end(pid, "renderer")
    if renderers:
        deadline = time.monotonic() + RENDERER_SETTLE_SECONDS
        while time.monotonic() < deadline:
            time.sleep(4)
            if _ui_alive():
                say("Quick Access runs JavaScript again after the renderer")
                say(f"running AppIDs after: {_running_app_ids()}")
                record()
                return 0
        say("the UI still does not answer after the renderer")
    if browser is None:
        say("no single spinning browser process to end; a Steam restart is the maintainer's")
        record()
        return 2
    if not end(browser, "browser"):
        record()
        return 2
    deadline = time.monotonic() + BROWSER_RESPAWN_SECONDS
    while time.monotonic() < deadline:
        time.sleep(5)
        fresh = _browser_pids() - {browser}
        if fresh and _ui_alive():
            say(f"Steam started webhelper browser {sorted(fresh)} and Quick Access runs JavaScript again")
            say(f"running AppIDs after: {_running_app_ids()}")
            record()
            return 0
    say("Steam did not bring a working webhelper back within the wait; a Steam restart is the maintainer's")
    say(f"running AppIDs after: {_running_app_ids()}")
    record()
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--incident", required=True,
                        help="the capture directory target_incident_capture.py wrote, or 'latest'")
    parser.add_argument("--dry-run", action="store_true", help="say what would be ended, and end nothing")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The webhelper recovery")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    incident = latest_capture() if args.incident == "latest" else Path(args.incident)
    notes = incident / "notes.md" if incident else None
    if notes is None or not notes.is_file():
        print("webhelper recover: name an incident capture; run target_incident_capture.py first", file=sys.stderr)
        return 1
    if time.time() - notes.stat().st_mtime > CAPTURE_MAX_AGE_SECONDS:
        print(f"webhelper recover: {incident} is older than half an hour; capture this incident first", file=sys.stderr)
        return 1
    return recover(incident, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
