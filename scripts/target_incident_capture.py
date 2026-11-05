#!/usr/bin/env python3
"""Capture everything an incident on the device is diagnosed from, in one command.

`docs/DEVELOPMENT.md` lists what an incident directory holds, and assembling it
by hand was a dozen calls made one at a time while the symptom was live: the
frame, the freeze probe, the webhelper threads, three logs, the CEF log and,
above all, the support bundle, which was reachable only through a button in a
UI that a wedged incident has taken away. This runs the same tracked helpers in
the order the procedure gives, each one bounded, and records what each answered
and how long it took. A step that fails is written down and the capture goes
on, because the one that failed is rarely the one the diagnosis needs.

The support bundle is the backend's own `create_support_bundle`, the call the
Advanced button makes, so it carries exactly what a user's bug report would. The
panel's in-memory record cannot be handed over from here; the backend adds the
copy the panel last flushed, which is what survives a dead renderer anyway. Like
the button, building a bundle removes the oldest bundles beyond the kept few.

Everything else is read only: nothing is restarted, reloaded or killed. Capture
first, recover second. The directory is under ignored `build/incidents/`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import host_platform
except ImportError:  # executed as a script
    import host_platform

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
CEF_TAIL_LINES = 3000


def _slug(symptom: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", symptom.casefold()).strip("-")
    if not slug:
        raise ValueError("the symptom needs at least one letter or digit")
    return slug[:60]


class Capture:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.steps: list[dict[str, object]] = []

    def run(self, name: str, argv: list[str], *, out: str | None, timeout: float) -> subprocess.CompletedProcess[str] | None:
        """Run one tracked helper, keep its stdout in `out`, and record the outcome."""
        started = time.monotonic()
        at = datetime.now().astimezone().strftime("%H:%M:%S")
        print(f"[{at}] {name}", flush=True)
        try:
            done = subprocess.run(
                [sys.executable, *argv], cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            self.steps.append({"step": name, "at": at, "outcome": f"timed out after {timeout:.0f} s"})
            return None
        if out:
            (self.directory / out).write_text(done.stdout + (f"\n--- stderr ---\n{done.stderr}" if done.stderr.strip() else ""), encoding="utf-8")
        self.steps.append({
            "step": name, "at": at, "outcome": f"exit {done.returncode}",
            "seconds": round(time.monotonic() - started, 1), "file": out,
        })
        return done

    def note(self, name: str, outcome: str) -> None:
        self.steps.append({"step": name, "at": datetime.now().astimezone().strftime("%H:%M:%S"), "outcome": outcome})


def _spinning(threads_file: Path) -> list[str]:
    try:
        data = json.loads(threads_file.read_text(encoding="utf-8").split("\n--- stderr ---")[0])
    except (OSError, ValueError):
        return []
    found = []
    for process in data.get("processes", []):
        for value in process.values():
            if not isinstance(value, list):
                continue
            for thread in value:
                if isinstance(thread, dict) and thread.get("verdict") == "spinning":
                    found.append(f"pid {process.get('pid')} {process.get('role')}: {thread.get('comm')} at {thread.get('cpu_share')} of a CPU")
    return found


# What the panel calls on its own while it is up: `counted` every few seconds,
# and the other two when it is shown and when it flushes its record. The last
# of them is when the panel's JavaScript was last seen alive.
PANEL_CALLS = ("name=counted", "name=note_panel_opened", "name=record_panel_log")
# Where the backend says the game it attached to went away.
TARGET_EVENTS = ("event=ce_launch.target_changed", "event=ce_launch.recovered_stop_decided")


def backend_landmarks(text: str) -> dict[str, object]:
    """The two moments a wedge is dated by, read out of the backend log.

    When the panel last called the backend, which bounds when its JavaScript
    stopped, and the lines where the attached game went away, which tell a game
    that died during the incident from one a recovery took down.
    """
    last_panel = None
    targets: list[str] = []
    for line in text.splitlines():
        stamp = line[1:24] if line.startswith("[") else None
        if stamp and any(marker in line for marker in PANEL_CALLS):
            last_panel = stamp
        if any(marker in line for marker in TARGET_EVENTS):
            targets.append(line[:300])
    return {"last_panel_call": last_panel, "target_events": targets[-6:]}


def capture(symptom: str, root: Path, *, thread_gap: float, bundle: bool) -> Path:
    directory = root / f"{_slug(symptom)}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')}"
    directory.mkdir(parents=True, exist_ok=True)
    cap = Capture(directory)
    prefix = _slug(symptom)

    # The frame first: it is what the user saw, and the cheapest thing to lose.
    # The whole screen, which already holds the panel wherever it is.
    done = cap.run("screenshot", [str(SCRIPTS / "target_screenshot.py"), "--region", "full", "--prefix", prefix,
                                  "--output-dir", str(directory)], out=None, timeout=40)
    if done is not None and done.returncode != 0:
        cap.note("screenshot said", (done.stderr or done.stdout).strip()[-300:])
    cap.run("ui freeze probe", [str(SCRIPTS / "target_ui_freeze_probe.py")], out="freeze-probe.txt", timeout=60)
    # Two samples apart: a thread spinning in both is a loop, not a long task.
    cap.run("webhelper threads 1", [str(SCRIPTS / "target_webhelper_threads.py"), "--json"], out="threads-1.json", timeout=60)
    time.sleep(thread_gap)
    cap.run("webhelper threads 2", [str(SCRIPTS / "target_webhelper_threads.py"), "--json"], out="threads-2.json", timeout=60)

    authority = cap.run("plugin authority", [str(SCRIPTS / "target_plugin_install.py"), "authority"], out="authority.json", timeout=60)
    user_home: Path | None = None
    if authority is not None and authority.returncode == 0:
        try:
            user_home = Path(json.loads(authority.stdout)["user_home"])
        except (ValueError, KeyError, TypeError):
            user_home = None

    cap.run("backend log", [str(SCRIPTS / "target_plugin_log.py"), "--lines", "5000"], out="backend.log", timeout=60)
    cap.run("journal", [str(SCRIPTS / "target_plugin_log.py"), "--journal", "--since=-2h", "--json"], out="journal.json", timeout=60)
    cap.run("panel log", [str(SCRIPTS / "target_plugin_log.py"), "--frontend", "--json", "--lines", "5000"], out="frontend.json", timeout=60)
    cap.run("updater log", [str(SCRIPTS / "target_plugin_log.py"), "--updater", "--json"], out="updater.json", timeout=60)
    cap.run("plugin state", [str(SCRIPTS / "target_state_probe.py")], out="state.json", timeout=90)

    # Steam's own browser log, from the Steam user's home the backend reports;
    # without that answer it is not guessed.
    if user_home is None:
        cap.note("cef log", "skipped: the live plugin did not report user_home")
    else:
        cef = user_home / ".steam/steam/logs/cef_log.txt"
        try:
            lines = cef.read_text(encoding="utf-8", errors="replace").splitlines()[-CEF_TAIL_LINES:]
            (directory / "cef-log-tail.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
            cap.note("cef log", f"last {len(lines)} lines of {cef}")
        except OSError as exc:
            cap.note("cef log", f"unreadable: {exc}")

    if bundle:
        done = cap.run("support bundle", [str(SCRIPTS / "target_plugin_rpc.py"), "create_support_bundle", "[[], 0]"],
                       out="support-bundle.json", timeout=120)
        try:
            archive = Path(json.loads(done.stdout)["path"]) if done is not None and done.returncode == 0 else None
        except (ValueError, KeyError, TypeError):
            archive = None
        if archive is not None and archive.is_file():
            shutil.copy2(archive, directory / archive.name)
            cap.note("support bundle copied", archive.name)
        else:
            cap.note("support bundle copied", "no archive: the backend did not build one, see support-bundle.json")
    else:
        cap.note("support bundle", "skipped by --no-bundle")

    spinning = sorted(set(_spinning(directory / "threads-1.json")) & set(_spinning(directory / "threads-2.json")))
    try:
        landmarks = backend_landmarks((directory / "backend.log").read_text(encoding="utf-8"))
    except OSError:
        landmarks = {"last_panel_call": None, "target_events": []}
    events = "\n".join(f"    {line}" for line in landmarks["target_events"]) or "    none in the backend log read"
    rows = "\n".join(f"| {step['at']} | {step['step']} | {step['outcome']} | {step.get('file') or ''} |" for step in cap.steps)
    (directory / "notes.md").write_text(
        f"# {symptom}\n\nCaptured by `scripts/target_incident_capture.py` at "
        f"{datetime.now().astimezone().isoformat(timespec='seconds')}, before any recovery.\n\n"
        "Spinning in both thread samples: " + ("; ".join(spinning) if spinning else "nothing") + "\n\n"
        f"Last call the panel made to the backend: {landmarks['last_panel_call'] or 'none in the backend log read'}\n\n"
        f"Where the attached game went away, newest last:\n\n{events}\n\n"
        "| Local time | Step | Outcome | File |\n|---|---|---|---|\n" + rows + "\n\n"
        "## What was done, and in what order\n\nWrite here what led to the symptom and every recovery step taken after this capture.\n",
        encoding="utf-8",
    )
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("symptom", help="what the user saw, in their terms, such as 'panel froze on collapse'")
    parser.add_argument("--root", type=Path, default=ROOT / "build" / "incidents", help="where incident directories go")
    parser.add_argument("--thread-gap", type=float, default=5.0, help="seconds between the two webhelper thread samples")
    parser.add_argument("--no-bundle", action="store_true", help="do not ask the backend for a support bundle")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The incident capture")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    try:
        directory = capture(args.symptom, args.root, thread_gap=args.thread_gap, bundle=not args.no_bundle)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"incident capture: {directory.relative_to(ROOT) if directory.is_relative_to(ROOT) else directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
