#!/usr/bin/env python3
"""Measure what one CE Decky session costs on the machine it is running on.

The probe samples every process twice across one bounded window and reports the
CPU time each of them actually spent in it, grouped into the parts a session is
made of: Cheat Engine, the game's ``wineserver``, the plugin backend, Decky,
Gamescope, Steam and the game itself.  Over the same window it reads the frame
rate out of the live Gamescope statistics pipe, so the cost in processor time
and the cost in frames come from one window rather than from two runs that have
to be assumed comparable.

Nothing here is specific to a session: the probe is a stopwatch, and it is the
operator who runs it once with a session attached and once without.  Compare the
two reports; a single report is a reading, not a measurement.

It publishes no session total, deliberately.  Adding the groups a session
touches would count work that is there either way, most of it the game's own
``wineserver``, and produce a number larger than the cost of attaching.  What a
session costs is the difference between two readings, group by group, and only
the operator holds both.

Reading it honestly:

* CPU is reported as a percentage of one core, which is how the figures in
  ``docs/FIELD_NOTES.md`` are stated.
* ``wineserver`` is every process of that name on the host, not the game's
  alone: nothing here proves which prefix one belongs to.  The group's process
  count is how that shows, and more than one means the group is not
  attributable to the session under test.
* ``game`` is a cohort rather than one process.  Given ``--appid`` it is every
  process whose argv carries that AppID, which includes Steam's ``reaper``, and
  a game that does not carry it in argv needs ``--game-comm``.
* The resident bridge runs as Lua inside Cheat Engine and has no process of its
  own, so its cost is inside the ``cheat_engine`` group and can only be
  separated by comparing a session against one whose bridge is not resident.
* A process that started or exited inside the window is counted nowhere: only
  processes present at both samples carry a delta that the window explains.
  Both counts are reported so a run that raced a launch is visible as one.

Every run being compared takes the same identity flags.  A baseline read
without ``--appid`` and an attached run read with it group their processes
differently, and the difference between them is then partly the flags.

Beside the CPU groups, one window also carries what the groups cannot explain
on their own, because the scarce resource here is not the size of a report: it
is a person at the device holding one scene still for several minutes, and a
field not taken then cannot be taken afterwards.

* ``poll_counters`` asks the live backend, on each side of the window and never
  inside it, which of its repeating paths ran and what each of them spent.  Its
  interval therefore brackets the window rather than coinciding with it, and
  ``counter_window_offsets_seconds`` says by how much at each end, so the two
  are never compared as if they were the same seconds.  CPU
  for that process is one figure over several loops at once, so this is the only
  thing that says which of them produced it.  Its opening read happens before
  the window is anchored, so a slow or unreachable Decky costs the run its own
  time and never a second of the sampling interval, and a backend that did not
  stay up across the window reports that instead of a delta.
* ``process_detail`` carries wakeups, runqueue delay, resident memory and, where
  the kernel allows it, byte and syscall counters for the parts a session is
  made of.  ``/proc/<pid>/io`` is denied for this plugin's own backend even to
  the same user, so that refusal is recorded rather than dropped: an absent
  counter and a counter that did not move look identical otherwise.
* ``fps`` carries the compositor's own write cadence beside the rate, because
  the pipe does not write once a second: it was read writing every 5.3 seconds
  on one machine and every 7.5 on another, so a 90-second window holds a dozen
  readings rather than ninety.  A spread over a dozen readings is reported with
  a warning saying so rather than presented as if it were ninety.
* ``sensors`` samples package power, GPU load, clocks and temperatures across
  the window rather than at its ends, because those are gauges and not counters.
  Channels are found by reading hwmon labels, so a device that exposes a sensor
  set another one does not is read rather than assumed.
* ``pressure_cpu`` says whether anything was waiting for a core, which is the
  difference between work done and time spent queued on a machine held at a
  power limit.
* ``processes_sampled`` is the size of the process table, which the table walk's
  cost scales with: two machines with different numbers of processes are not
  comparable on the backend figure without it.

Read only.  It writes nothing, signals nothing, opens the statistics pipe for
reading only, and the one call it makes into the plugin increments none of the
counters it reads.

    python3 scripts/target_session_cost_probe.py --window 30 --label unattached --appid <appid>
    python3 scripts/target_session_cost_probe.py --window 30 --label attached --appid <appid>
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import statistics
import sys
import threading
import time

if __package__:
    from . import host_platform
else:
    import host_platform

DEFAULT_DECKY_URL = "http://127.0.0.1:1337"
# The counter snapshot shape this probe knows how to subtract. It is the
# backend's own schema, and an installed build that has moved past it is
# reported rather than read as if the fields still meant the same thing.
COUNTER_SCHEMA = 1
# The opening counter read happens before the window is anchored, so this
# bounds how long a run waits for an unresponsive Decky rather than how much of
# the window it can eat.
COUNTER_RPC_TIMEOUT_S = 5.0

MAX_SCANNED_PROCESSES = 8192
MAX_CMDLINE_BYTES = 16 * 1024
MAX_FPS_SAMPLES = 4096
# Gamescope does NOT write once a second. Read straight off the pipe with `cat`
# on 2026-09-07 it wrote a rate-and-focus pair every 5.3 seconds on a Steam Deck
# and about every 7.5 on a Steam Machine, and both cadences held steady. This
# code and the field notes both used to say once a second, which made every
# frame figure look like it rested on ninety readings when it rested on a dozen.
# Nothing here assumes a cadence any more: the observed interval is measured and
# reported beside the rate, so a spread is always read with the number of points
# under it.
OBSERVED_STATS_INTERVAL_S = 8.0
# Long enough to contain more than one write at the slowest cadence seen. A
# shorter bound would call a live pipe dead for the ordinary reason that it had
# not written yet.
STATS_OPEN_TIMEOUT_S = 3 * OBSERVED_STATS_INTERVAL_S
# Below this many usable readings a spread is not a spread. It is reported with
# the rate rather than suppressed, because the mean is still worth having.
MIN_USABLE_FPS_SAMPLES = 5
DEFAULT_WINDOW_S = 30.0
DEFAULT_TOP = 12


def clock_ticks_per_second() -> int:
    """The kernel tick every ``/proc/<pid>/stat`` CPU figure is counted in.

    Read on demand rather than at import. `os.sysconf` does not exist on
    Windows, and a module that cannot be imported there cannot report an
    unsupported host either: it fails on the import line, and what an agent
    reads is an AttributeError rather than the name of the machine this belongs
    on.
    """
    return os.sysconf("SC_CLK_TCK")


# Group order is the order the report lists them in: the parts a session adds
# first, then the parts that were already running.
GROUP_ORDER = (
    "cheat_engine",
    "wineserver",
    "plugin_backend",
    "game",
    "decky_loader",
    "gamescope",
    "steam",
    "other",
)


class ProcSample:
    """One process's accumulated CPU time, as of one instant."""

    __slots__ = ("pid", "comm", "cmdline", "ticks", "starttime")

    def __init__(self, pid: int, comm: str, cmdline: str, ticks: int, starttime: int) -> None:
        self.pid = pid
        self.comm = comm
        self.cmdline = cmdline
        self.ticks = ticks
        self.starttime = starttime


def _read_text(path: Path, limit: int) -> str | None:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def parse_stat(raw: str) -> tuple[str, int, int] | None:
    """Return (comm, utime+stime ticks, starttime) from one /proc/<pid>/stat.

    The comm field is parenthesised and may itself contain spaces and
    parentheses, so every field is counted from the last ')' rather than by
    splitting the whole line.
    """
    close = raw.rfind(")")
    open_paren = raw.find("(")
    if close < 0 or open_paren < 0 or close < open_paren:
        return None
    comm = raw[open_paren + 1 : close]
    fields = raw[close + 1 :].split()
    # After comm, field 3 is state; utime and stime are fields 14 and 15 of the
    # whole line, so index 11 and 12 here, and starttime is field 22, index 19.
    if len(fields) < 20:
        return None
    try:
        return comm, int(fields[11]) + int(fields[12]), int(fields[19])
    except ValueError:
        return None


def sample_processes(proc_root: Path) -> dict[int, ProcSample]:
    samples: dict[int, ProcSample] = {}
    scanned = 0
    try:
        entries = sorted(proc_root.iterdir())
    except OSError as exc:
        raise RuntimeError(f"cannot read {proc_root}: {exc}") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        scanned += 1
        if scanned > MAX_SCANNED_PROCESSES:
            break
        raw = _read_text(entry / "stat", 4096)
        if raw is None:
            continue
        parsed = parse_stat(raw)
        if parsed is None:
            continue
        comm, ticks, starttime = parsed
        cmdline_raw = _read_text(entry / "cmdline", MAX_CMDLINE_BYTES) or ""
        cmdline = cmdline_raw.replace("\0", " ").strip()
        samples[int(entry.name)] = ProcSample(int(entry.name), comm, cmdline, ticks, starttime)
    return samples


def total_cpu_ticks(proc_root: Path) -> int | None:
    raw = _read_text(proc_root / "stat", 4096)
    if raw is None:
        return None
    for line in raw.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()[1:]
        try:
            values = [int(item) for item in fields[:8]]
        except ValueError:
            return None
        # user+nice+system+irq+softirq+steal, leaving idle and iowait out.
        busy = values[0] + values[1] + values[2]
        busy += sum(values[5:8]) if len(values) >= 8 else 0
        return busy
    return None


def classify(sample: ProcSample, app_id: int | None, game_comm: str | None) -> str:
    comm = sample.comm.lower()
    cmdline = sample.cmdline.lower()
    if "cheatengine" in comm or "cheatengine" in cmdline:
        return "cheat_engine"
    if comm == "wineserver":
        return "wineserver"
    if "homebrew/plugins/ce-decky" in cmdline:
        return "plugin_backend"
    if game_comm is not None and comm == game_comm.lower():
        return "game"
    if app_id is not None and f"appid={app_id}" in cmdline.replace(" ", ""):
        return "game"
    if "homebrew/services/pluginloader" in cmdline or comm == "pluginloader":
        return "decky_loader"
    if comm.startswith("gamescope"):
        return "gamescope"
    if comm.startswith("steam") or "/steam/" in cmdline or comm == "reaper":
        return "steam"
    return "other"


def discover_stats_pipe(proc_root: Path) -> str | None:
    """Find the live Gamescope session's statistics pipe from its own argv.

    A previous boot leaves its ``/run/user/<uid>/gamescope.*`` directory behind,
    so the path is taken from the running compositor rather than from a glob
    that cannot tell this session's pipe from a dead one.
    """
    scanned = 0
    try:
        entries = sorted(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        scanned += 1
        if scanned > MAX_SCANNED_PROCESSES:
            break
        # SteamOS reports the compositor as "gamescope-wl" rather than
        # "gamescope", and the launcher script beside it is "start-gamescope",
        # so the prefix is what separates them.
        comm = (_read_text(entry / "comm", 128) or "").strip()
        if not comm.startswith("gamescope"):
            continue
        raw = _read_text(entry / "cmdline", MAX_CMDLINE_BYTES) or ""
        argv = [item for item in raw.split("\0") if item]
        for index, item in enumerate(argv):
            if item == "-T" and index + 1 < len(argv):
                return argv[index + 1]
    return None


class FpsReader:
    """Collect ``fps=`` and ``focus=`` lines from the statistics pipe."""

    def __init__(self, path: str, deadline: float) -> None:
        self.path = path
        self.deadline = deadline
        # Gamescope writes the rate first and the focus it belongs to second,
        # so each reading is a pair and the focus arrives after its own value.
        # Each entry is [rate, focus, arrival], and the arrival time is what
        # makes the pipe's real cadence measurable instead of assumed.
        self.samples: list[list[object]] = []
        self.focus: list[str] = []
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=STATS_OPEN_TIMEOUT_S)

    def _run(self) -> None:
        try:
            # Non-blocking open, so a pipe with no writer fails the read rather
            # than hanging the whole probe on open().
            fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            self.error = f"cannot open statistics pipe: {exc}"
            return
        try:
            with os.fdopen(fd, "r", buffering=1, errors="replace") as handle:
                pending = ""
                while time.monotonic() < self.deadline:
                    try:
                        chunk = handle.read(4096)
                    except OSError as exc:
                        self.error = f"statistics pipe read failed: {exc}"
                        return
                    if not chunk:
                        time.sleep(0.1)
                        continue
                    pending += chunk
                    lines = pending.split("\n")
                    pending = lines.pop()
                    for line in lines:
                        self._consume(line.strip())
                        if len(self.samples) >= MAX_FPS_SAMPLES:
                            return
        except OSError as exc:
            self.error = f"statistics pipe failed: {exc}"

    def _consume(self, line: str) -> None:
        if line.startswith("fps="):
            try:
                self.samples.append([float(line[4:]), None, time.monotonic()])
            except ValueError:
                return
        elif line.startswith("focus="):
            value = line[6:]
            if not value:
                return
            if self.samples and self.samples[-1][1] is None:
                self.samples[-1][1] = value
            if value not in self.focus:
                self.focus.append(value)

    def _usable(self) -> list[float]:
        """Every reading except the ones a window boundary made partial.

        The first reading covers however much of its interval the probe was
        there for, and the first after the focus changes covers two different
        things. Both are dropped by where they sit, never by how small they are:
        a genuine stall reads as a fraction of a frame, and that is exactly the
        observation this is for.

        A reading whose focus never arrived is dropped too. The compositor
        writes the rate first and the focus a couple of milliseconds later, so a
        window that ends between the two leaves the last reading unpaired, and an
        unpaired reading is one whose focus cannot be compared with the one
        before it. Counting it would let exactly the case this drops elsewhere,
        a focus change, through at the one boundary where nothing can catch it.
        If a compositor ever stopped writing focus at all, every reading would
        become unusable and the report would say so, with `samples` far above
        `usable_samples`, rather than quietly averaging readings it cannot place.
        """
        usable: list[float] = []
        previous_focus: object = None
        for index, (value, focus, _arrival) in enumerate(self.samples):
            boundary = index == 0 or focus is None
            if focus is not None and previous_focus is not None and focus != previous_focus:
                boundary = True
            if focus is not None:
                previous_focus = focus
            if not boundary:
                usable.append(float(value))
        return usable

    def _intervals(self) -> list[float]:
        """Seconds between consecutive writes, as this run actually saw them."""
        arrivals = [float(sample[2]) for sample in self.samples]
        return [later - earlier for earlier, later in zip(arrivals, arrivals[1:])]

    def summary(self) -> dict[str, object]:
        usable = self._usable()
        intervals = self._intervals()
        report: dict[str, object] = {
            "source": self.path,
            "samples": len(self.samples),
            "usable_samples": len(usable),
            "partial_samples": len(self.samples) - len(usable),
            "focus_seen": list(self.focus),
            "error": self.error,
        }
        if intervals:
            # The compositor's own cadence, measured rather than assumed. A rate
            # cannot be read without it: the same window is ninety readings at
            # one cadence and a dozen at another, and the spread means something
            # different in each case.
            report["write_interval_seconds"] = {
                "mean": round(statistics.fmean(intervals), 2),
                "min": round(min(intervals), 2),
                "max": round(max(intervals), 2),
            }
        if usable:
            report["mean"] = round(statistics.fmean(usable), 2)
            report["min"] = round(min(usable), 2)
            report["max"] = round(max(usable), 2)
            report["stdev"] = round(statistics.stdev(usable), 2) if len(usable) > 1 else 0.0
            if len(usable) < MIN_USABLE_FPS_SAMPLES:
                report["spread_warning"] = (
                    f"{len(usable)} usable readings: the mean stands, the spread does not."
                    " The compositor writes every few seconds, not every second, so a short"
                    " window holds few readings however long it looks."
                )
        return report


# The parts of a session whose own procfs counters are worth reading. Every
# other process is still in the CPU groups; this is the bounded set that also
# gets memory, wakeups, runqueue delay and, where the kernel allows it, bytes.
DETAILED_GROUPS = ("cheat_engine", "wineserver", "plugin_backend", "game")
MAX_DETAILED_PROCESSES = 24
# Package power, GPU load and clocks are instantaneous gauges rather than
# counters, so a reading at each end of the window says nothing about what
# happened inside it. Sample them across it instead.
SENSOR_INTERVAL_S = 1.0
MAX_SENSOR_SAMPLES = 4096
MAX_CPUFREQ_CORES = 32
HWMON_ROOT = Path("/sys/class/hwmon")
DRM_ROOT = Path("/sys/class/drm")
CPUFREQ_ROOT = Path("/sys/devices/system/cpu")
PRESSURE_CPU = Path("/proc/pressure/cpu")


def _read_int_file(path: Path) -> int | None:
    raw = _read_text(path, 64)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _status_fields(raw: str) -> dict[str, int]:
    wanted = {
        "VmRSS": "vm_rss_kb",
        "Threads": "threads",
        "voluntary_ctxt_switches": "voluntary_ctxt_switches",
        "nonvoluntary_ctxt_switches": "nonvoluntary_ctxt_switches",
    }
    found: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, value = line.partition(":")
        name = wanted.get(key.strip())
        if name is None:
            continue
        digits = value.strip().split()
        if digits:
            try:
                found[name] = int(digits[0])
            except ValueError:
                continue
    return found


def read_process_detail(proc_root: Path, pid: int) -> dict[str, object]:
    """Per-process counters that say what kind of work a CPU figure was.

    ``io`` is denied for this plugin's own backend even though the probe runs as
    the same user, which is the non-dumpable protection procfs already applies
    to that process's environment. It is readable for Cheat Engine and for the
    game, where ``rchar`` is a clue for repeated file hashing, so the field
    carries the refusal rather than being dropped: an absent counter and a
    counter that did not move look identical otherwise.
    """
    root = proc_root / str(pid)
    detail: dict[str, object] = {}
    status = _read_text(root / "status", 8192)
    if status is not None:
        detail.update(_status_fields(status))
    schedstat = _read_text(root / "schedstat", 256)
    if schedstat is not None:
        fields = schedstat.split()
        if len(fields) >= 2:
            try:
                detail["cpu_ns"] = int(fields[0])
                detail["runqueue_delay_ns"] = int(fields[1])
            except ValueError:
                pass
    try:
        raw_io = (root / "io").read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError as exc:
        detail["io_error"] = f"{type(exc).__name__}: {exc.strerror or exc}"
    else:
        for line in raw_io.splitlines():
            key, _, value = line.partition(":")
            if key.strip() in {"rchar", "wchar", "syscr", "syscw"}:
                try:
                    detail[key.strip()] = int(value.strip())
                except ValueError:
                    continue
    return detail


def read_pressure(path: Path = PRESSURE_CPU) -> dict[str, int] | None:
    """``some`` and ``full`` stall totals in microseconds, which are counters."""
    raw = _read_text(path, 1024)
    if raw is None:
        return None
    totals: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        if not fields:
            continue
        for field in fields[1:]:
            key, _, value = field.partition("=")
            if key == "total":
                try:
                    totals[f"{fields[0]}_total_us"] = int(value)
                except ValueError:
                    continue
    return totals or None


def discover_sensors(
    hwmon_root: Path = HWMON_ROOT, drm_root: Path = DRM_ROOT, cpufreq_root: Path = CPUFREQ_ROOT,
) -> dict[str, Path]:
    """Name every gauge this host actually exposes, once, before the window.

    The Deck exposes a ``steamdeck_hwmon`` the Steam Machine does not, and the
    package-power reading is the one hwmon whose ``power1_label`` says ``PPT``,
    so the channels are found by reading their labels rather than by assuming a
    path that happens to be right on one machine.
    """
    channels: dict[str, Path] = {}
    try:
        hwmons = sorted(hwmon_root.iterdir())
    except OSError:
        hwmons = []
    for hwmon in hwmons[:32]:
        name = (_read_text(hwmon / "name", 128) or "").strip() or hwmon.name
        power_label = (_read_text(hwmon / "power1_label", 128) or "").strip()
        if power_label and (hwmon / "power1_average").is_file():
            channels[f"{name}_{power_label.lower()}_power_uw"] = hwmon / "power1_average"
        for index in (1, 2):
            label = (_read_text(hwmon / f"freq{index}_label", 128) or "").strip()
            source = hwmon / f"freq{index}_input"
            if label and source.is_file():
                channels[f"{name}_{label.lower()}_hz"] = source
        if (hwmon / "temp1_input").is_file():
            channels[f"{name}_temp1_mc"] = hwmon / "temp1_input"
    try:
        cards = sorted(drm_root.glob("card[0-9]*"))
    except OSError:
        cards = []
    for card in cards[:8]:
        busy = card / "device" / "gpu_busy_percent"
        if busy.is_file():
            channels[f"{card.name}_gpu_busy_percent"] = busy
    for index in range(MAX_CPUFREQ_CORES):
        current = cpufreq_root / f"cpu{index}" / "cpufreq" / "scaling_cur_freq"
        if not current.is_file():
            break
        channels[f"cpu{index}_khz"] = current
    return channels


class SensorReader:
    """Sample every discovered gauge across the window, on its own thread."""

    def __init__(self, channels: dict[str, Path], deadline: float) -> None:
        self.channels = channels
        self.deadline = deadline
        self.readings: dict[str, list[int]] = {name: [] for name in channels}
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if self.channels:
            self._thread.start()

    def join(self) -> None:
        if self.channels:
            self._thread.join(timeout=SENSOR_INTERVAL_S * 2)

    def _run(self) -> None:
        taken = 0
        while time.monotonic() < self.deadline and taken < MAX_SENSOR_SAMPLES:
            for name, path in self.channels.items():
                value = _read_int_file(path)
                if value is not None:
                    self.readings[name].append(value)
            taken += 1
            time.sleep(SENSOR_INTERVAL_S)

    def summary(self) -> dict[str, object]:
        report: dict[str, object] = {}
        for name, values in self.readings.items():
            if not values:
                continue
            report[name] = {
                "samples": len(values),
                "mean": round(statistics.fmean(values), 2),
                "min": min(values),
                "max": max(values),
            }
        return report


def read_poll_counters(decky_url: str, timeout: float) -> dict[str, object]:
    """Ask the live backend which of its repeating paths has been running.

    Imported here rather than at module scope: the install helper reaches into
    the plugin's own modules, and this probe has to stay importable on a host
    that has none of them. The counters are read at each end of the window and
    nowhere inside it, and the call that carries them out is not itself one of
    the counted paths.
    """
    try:
        if __package__:
            from .target_plugin_install import DeckyWebSocket, PLUGIN_NAME, _auth_token, _await_reply
        else:
            from target_plugin_install import DeckyWebSocket, PLUGIN_NAME, _auth_token, _await_reply
    except ImportError as exc:
        return {"error": f"the Decky client is unavailable here: {exc}"}
    try:
        token = _auth_token(decky_url, timeout)
        with DeckyWebSocket.connect(decky_url, token, timeout) as ws:
            ws.send_json({
                "type": 0,
                "route": "loader/call_plugin_method",
                "args": [PLUGIN_NAME, "get_poll_counters"],
                "id": 20,
            })
            snapshot = _await_reply(ws, 20)
    except Exception as exc:  # noqa: BLE001 - an unreachable backend is a note, never the end of a window
        return {"error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(snapshot, dict):
        return {"error": "the backend returned no counter snapshot"}
    return snapshot


def poll_counter_deltas(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    """What each counted path did inside the window, from the backend's own clock.

    Cumulative counters may only be subtracted within one run of the backend,
    and a rising uptime does not prove one. A snapshot taken five seconds after
    load, a restart, and a second snapshot eighty seconds into the new process
    read exactly like an ordinary ninety-second window, and subtracting them
    mixes two incarnations into numbers that can look entirely plausible. The
    snapshot carries an incarnation token for that reason, and two snapshots
    that do not agree on it are a restart rather than a delta.
    """
    if "error" in before or "error" in after:
        return {"before": before, "after": after}
    for name, snapshot in (("before", before), ("after", after)):
        if snapshot.get("schema") != COUNTER_SCHEMA:
            return {
                "error": (
                    f"the {name} snapshot is counter schema {snapshot.get('schema')!r},"
                    f" and this probe reads {COUNTER_SCHEMA}"
                ),
                "before": before, "after": after,
            }
    incarnation = before.get("incarnation")
    if not isinstance(incarnation, str) or not incarnation:
        # An installed build older than the token cannot prove it stayed up, so
        # the reading is refused rather than reported as if it could.
        return {
            "error": "the backend does not report which run its counters belong to",
            "before": before, "after": after,
        }
    if after.get("incarnation") != incarnation:
        return {"error": "the backend restarted during the window", "before": before, "after": after}
    paths_before = before.get("paths") if isinstance(before.get("paths"), dict) else {}
    paths_after = after.get("paths") if isinstance(after.get("paths"), dict) else {}
    uptime_before = before.get("uptime_seconds")
    uptime_after = after.get("uptime_seconds")
    elapsed = None
    if isinstance(uptime_before, (int, float)) and isinstance(uptime_after, (int, float)):
        elapsed = uptime_after - uptime_before
    if elapsed is not None and elapsed < 0:
        # One incarnation whose clock ran backwards is not a window either.
        return {"error": "the backend clock ran backwards during the window", "before": before, "after": after}
    rows: dict[str, object] = {}
    for name, later in paths_after.items():
        earlier = paths_before.get(name)
        if not isinstance(later, dict) or not isinstance(earlier, dict):
            continue
        calls = int(later.get("calls", 0)) - int(earlier.get("calls", 0))
        cpu = float(later.get("cpu_seconds", 0.0)) - float(earlier.get("cpu_seconds", 0.0))
        wall = float(later.get("wall_seconds", 0.0)) - float(earlier.get("wall_seconds", 0.0))
        row: dict[str, object] = {
            "calls": calls,
            "cpu_seconds": round(cpu, 6),
            "wall_seconds": round(wall, 6),
        }
        if elapsed and elapsed > 0:
            row["calls_per_second"] = round(calls / elapsed, 4)
            row["cpu_percent_of_core"] = round(cpu / elapsed * 100.0, 3)
        rows[name] = row
    return {
        "incarnation": incarnation,
        "backend_window_seconds": None if elapsed is None else round(elapsed, 3),
        "paths": rows,
    }


def host_identity() -> dict[str, object]:
    model = None
    raw = _read_text(Path("/proc/cpuinfo"), 64 * 1024) or ""
    match = re.search(r"^model name\s*:\s*(.+)$", raw, re.MULTILINE)
    if match:
        model = match.group(1).strip()
    os_release = _read_text(Path("/etc/os-release"), 8192) or ""
    fields = {}
    for line in os_release.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value.strip().strip('"')
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "cpu_model": model,
        "cpu_count": os.cpu_count(),
        "os_version_id": fields.get("VERSION_ID"),
        "os_build_id": fields.get("BUILD_ID"),
        "clock_ticks_per_second": clock_ticks_per_second(),
    }


def _percent_of_core(ticks: int, elapsed: float) -> float:
    if elapsed <= 0:
        return 0.0
    return ticks / clock_ticks_per_second() / elapsed * 100.0


def probe(
    window: float,
    label: str,
    app_id: int | None,
    game_comm: str | None,
    stats_pipe: str | None,
    top: int,
    read_fps: bool,
    proc_root: Path,
    read_sensors: bool = True,
    read_counters: bool = True,
    decky_url: str = DEFAULT_DECKY_URL,
) -> dict[str, object]:
    if window <= 0:
        raise ValueError("window must be a positive number of seconds")
    resolved_pipe = stats_pipe
    if read_fps and resolved_pipe is None:
        resolved_pipe = discover_stats_pipe(proc_root)

    # Everything that can take an unknown amount of time happens before the
    # window is anchored. The opening counter read talks to Decky over a socket
    # with its own timeout, and anchoring first meant that a slow or unreachable
    # backend spent the requested sampling interval: the CPU window shrank by
    # however long the call took while the frame and sensor readers kept the
    # whole of it, and the report then claimed one window for readings that no
    # longer described the same seconds.
    counters_before: dict[str, object] = {"error": "not read"}
    counter_rpc_seconds = 0.0
    counters_before_at: float | None = None
    if read_counters:
        counter_rpc_started = time.monotonic()
        counters_before = read_poll_counters(decky_url, COUNTER_RPC_TIMEOUT_S)
        counters_before_at = time.monotonic()
        counter_rpc_seconds = counters_before_at - counter_rpc_started

    channels = discover_sensors() if read_sensors else {}

    reader: FpsReader | None = None
    started_at = time.monotonic()
    if read_fps and resolved_pipe is not None:
        reader = FpsReader(resolved_pipe, started_at + window)
        reader.start()

    sensors = SensorReader(channels, started_at + window)
    sensors.start()

    # The window is the interval between the two process samples, and the anchor
    # is taken at each of them rather than after the reads that follow. Measuring
    # it from after the detail reads put their duration into the denominator of
    # every CPU percentage, which understated all of them by however long a pass
    # over procfs took.
    first = sample_processes(proc_root)
    first_at = time.monotonic()
    first_total = total_cpu_ticks(proc_root)
    first_pressure = read_pressure()
    detailed = _detailed_pids(first, app_id, game_comm)
    first_detail = {pid: read_process_detail(proc_root, pid) for pid in detailed}
    time.sleep(max(0.0, window - (time.monotonic() - first_at)))
    second = sample_processes(proc_root)
    second_at = time.monotonic()
    elapsed = second_at - first_at
    # Every local reading of the closing side happens here, in the same order as
    # its opening counterpart, so each pair spans the same interval as the window
    # itself. Nothing slow may sit between the two halves of a pair: the closing
    # counter read talks to Decky over a socket with a five-second timeout, and
    # taking it here put that wait inside the system CPU numerator, the pressure
    # delta and the per-process deltas while the window it is divided by knew
    # nothing about it.
    second_total = total_cpu_ticks(proc_root)
    second_pressure = read_pressure()
    second_detail = {pid: read_process_detail(proc_root, pid) for pid in detailed if pid in second}
    # Last, because it is the one read that can take seconds. The counters
    # cannot be inside the window they describe, and how far outside they sit is
    # reported rather than described as the same window.
    counters_after_at: float | None = None
    if read_counters:
        counters_after = read_poll_counters(decky_url, COUNTER_RPC_TIMEOUT_S)
        counters_after_at = time.monotonic()
    else:
        counters_after = {"error": "not read"}

    if reader is not None:
        reader.join()
    sensors.join()

    rows: list[dict[str, object]] = []
    appeared = 0
    for pid, later in second.items():
        earlier = first.get(pid)
        if earlier is None or earlier.starttime != later.starttime:
            appeared += 1
            continue
        delta = later.ticks - earlier.ticks
        if delta <= 0:
            continue
        rows.append(
            {
                "pid": pid,
                "comm": later.comm,
                "group": classify(later, app_id, game_comm),
                "cpu_percent_of_core": round(_percent_of_core(delta, elapsed), 3),
            }
        )
    disappeared = sum(1 for pid in first if pid not in second)

    groups: dict[str, dict[str, object]] = {}
    for row in rows:
        name = str(row["group"])
        bucket = groups.setdefault(name, {"cpu_percent_of_core": 0.0, "processes": 0})
        bucket["cpu_percent_of_core"] = round(
            float(bucket["cpu_percent_of_core"]) + float(row["cpu_percent_of_core"]), 3
        )
        bucket["processes"] = int(bucket["processes"]) + 1
    ordered_groups = {name: groups[name] for name in GROUP_ORDER if name in groups}

    system_percent = None
    if first_total is not None and second_total is not None and elapsed > 0:
        system_percent = round(_percent_of_core(second_total - first_total, elapsed), 2)

    rows.sort(key=lambda item: float(item["cpu_percent_of_core"]), reverse=True)
    report: dict[str, object] = {
        "schema": 2,
        "mode": "read_only_session_cost",
        "label": label,
        "host": host_identity(),
        "window_seconds": round(elapsed, 3),
        # The frame and sensor readers run from the same anchor as the process
        # sample, so their spans differ from it only by what one pass of the
        # process table costs. The opening counter read is before the anchor and
        # is reported here rather than folded into the window it precedes.
        "counter_rpc_seconds": round(counter_rpc_seconds, 3),
        "sampling_offset_seconds": round(first_at - started_at, 3),
        # The counters cannot be read inside the window they describe, so their
        # own interval brackets it. This says by how much, at each end, rather
        # than leaving the two to be compared as if they were the same window.
        "counter_window_offsets_seconds": {
            "before_window": None if counters_before_at is None else round(first_at - counters_before_at, 3),
            "after_window": None if counters_after_at is None else round(counters_after_at - second_at, 3),
        },
        "app_id": app_id,
        "game_comm": game_comm,
        "groups": ordered_groups,
        "system_busy_percent_of_core": system_percent,
        # The walk's cost scales with the size of the table, so two machines
        # with different numbers of processes are not comparable on the backend
        # figure without it.
        "processes_sampled": len(second),
        "processes_appeared_in_window": appeared,
        "processes_left_in_window": disappeared,
        "top_processes": rows[: max(0, top)],
        "fps": reader.summary() if reader is not None else {"source": resolved_pipe, "error": "not read"},
        "process_detail": _detail_deltas(first, second, first_detail, second_detail, app_id, game_comm),
        "pressure_cpu": _pressure_delta(first_pressure, second_pressure),
        "sensors": sensors.summary() if read_sensors else {"error": "not read"},
        "poll_counters": poll_counter_deltas(counters_before, counters_after),
    }
    return report


def _detailed_pids(
    samples: dict[int, ProcSample], app_id: int | None, game_comm: str | None,
) -> list[int]:
    """The bounded set of processes whose own counters are read at both ends."""
    chosen = [
        pid for pid, sample in sorted(samples.items())
        if classify(sample, app_id, game_comm) in DETAILED_GROUPS
    ]
    return chosen[:MAX_DETAILED_PROCESSES]


def _detail_deltas(
    first: dict[int, ProcSample],
    second: dict[int, ProcSample],
    first_detail: dict[int, dict[str, object]],
    second_detail: dict[int, dict[str, object]],
    app_id: int | None,
    game_comm: str | None,
) -> list[dict[str, object]]:
    """Per-process counters as deltas, and the gauges as their final reading.

    A process that was replaced inside the window is dropped the same way the
    CPU rows drop one: a PID whose start time changed is a different process,
    and subtracting its predecessor's counters would invent a figure.
    """
    detail: list[dict[str, object]] = []
    for pid in sorted(second_detail):
        earlier = first_detail.get(pid)
        later = second_detail.get(pid)
        if not isinstance(earlier, dict) or not isinstance(later, dict):
            continue
        if pid not in first or pid not in second or first[pid].starttime != second[pid].starttime:
            continue
        row: dict[str, object] = {
            "pid": pid,
            "comm": second[pid].comm,
            "group": classify(second[pid], app_id, game_comm),
        }
        for name in ("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches", "runqueue_delay_ns",
                     "rchar", "wchar", "syscr", "syscw"):
            before = earlier.get(name)
            after = later.get(name)
            if isinstance(before, int) and isinstance(after, int):
                row[f"{name}_delta"] = after - before
        for name in ("vm_rss_kb", "threads"):
            if isinstance(later.get(name), int):
                row[name] = later[name]
        # Which counters the kernel refused, and for which process. The backend's
        # own io is denied even to the same user, and that is a fact about the
        # reading rather than a fault in it.
        refusal = later.get("io_error") or earlier.get("io_error")
        if refusal:
            row["io_error"] = refusal
        detail.append(row)
    return detail


def _pressure_delta(first: dict[str, int] | None, second: dict[str, int] | None) -> dict[str, object]:
    """Whether anything was waiting for a core, which CPU time alone cannot say."""
    if first is None or second is None:
        return {"error": f"{PRESSURE_CPU} is unavailable on this host"}
    return {f"{name}_delta": second[name] - value for name, value in first.items() if name in second}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", type=float, default=DEFAULT_WINDOW_S, help="sampling window in seconds")
    parser.add_argument("--label", default="unlabelled", help="what this run is, e.g. attached or unattached")
    parser.add_argument("--appid", type=int, help="exact AppID whose processes are the game")
    parser.add_argument("--game-comm", help="exact process name of the game, when its AppID is not in the argv")
    parser.add_argument("--stats-pipe", help="exact Gamescope statistics pipe; discovered from the live compositor when omitted")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="how many individual processes to list")
    parser.add_argument("--no-fps", action="store_true", help="skip the frame rate entirely")
    parser.add_argument("--no-sensors", action="store_true", help="skip package power, GPU load, clocks and temperature")
    parser.add_argument("--no-poll-counters", action="store_true", help="do not ask the live backend which of its repeating paths ran")
    parser.add_argument("--decky-url", default=DEFAULT_DECKY_URL, help="Decky's local API, which the counter snapshot is read through")
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        host_platform.require("The session cost probe")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if args.appid is not None and (args.appid < 1 or args.appid > 0xFFFFFFFF):
        print("target session cost probe: AppID must be between 1 and 4294967295", file=sys.stderr)
        return 2
    try:
        report = probe(
            args.window,
            args.label,
            args.appid,
            args.game_comm,
            args.stats_pipe,
            args.top,
            not args.no_fps,
            args.proc_root,
            read_sensors=not args.no_sensors,
            read_counters=not args.no_poll_counters,
            decky_url=args.decky_url,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"target session cost probe: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
