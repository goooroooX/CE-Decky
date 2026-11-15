#!/usr/bin/env python3
"""What happened to this device around Decky, boot by boot, from the system journal.

`target_plugin_log.py --journal` answers what this plugin's backend did, and
returns nothing else on purpose. The questions an incident starts with are
wider than that, and they are usually asked about a boot that is already over:
who did the out-of-memory killer take, and how large was it; what did Decky's
service leave running when its loader died; what was installed, from where, and
did the old backend have to be SIGKILLed; did Decky stop itself after Steam's
webhelper crashed three times in a minute; what crashed and dumped core; when
did the machine suspend, resume or reboot. Each of those lives in a different
part of the journal under a different identifier, and reading them by hand is a
`journalctl` call per source per boot and a grep per question.

This reads them once and puts them on one line each, in time order, under a
header per boot:

- `oom`: earlyoom's kills with the process name, its resident size and what
  the machine had left at that moment, whether the process then exited, and the
  kernel's own OOM kills;
- `memory`: earlyoom's periodic reading of available memory and swap;
- `unit`: the service units' starts, stops, exit status and result, the
  processes systemd left running after a stop (`KillMode=process`) or found
  left over at the next start, and the memory peak it reports, `plugin_loader`
  by default and any other with `--unit`;
- `decky`: Decky's own loader records - installs with their source and
  version, uninstalls, loads, a backend that had to be SIGKILLed, reloads it
  refused (collapsed into one line with a count), the webhelper crash count
  that makes it stop its own service, its own start and shutdown, and every
  warning or error it logged;
- `crash`: processes systemd-coredump caught, and kernel segfaults;
- `session`: power key, suspend, resume, reboot and power-off.

It reads; it changes nothing, and it calls no plugin and no Steam interface.
Command lines are never printed, only process names, because an earlyoom record
carries a game's path in its command line. A process name can still be a game's
own executable, so what is copied out of this into tracked text follows the
rule that names no game. A source the operator's user may not
read is reported as such rather than as a boot where nothing happened.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import json
import re
import subprocess
import sys
from typing import Any, Callable, Iterable

if __package__:
    from . import host_platform
else:
    import host_platform

SCHEMA = 1
KINDS = ("oom", "memory", "unit", "decky", "crash", "session")
DEFAULT_UNITS = ("plugin_loader.service",)
FIELDS = "MESSAGE,SYSLOG_IDENTIFIER,_PID,__REALTIME_TIMESTAMP,_BOOT_ID,PRIORITY"
# Per source and boot. A boot's kernel log is the largest of these and is still
# a few thousand records; the bound is what stops a runaway rather than a size.
MAX_ENTRIES = 200_000
# Repeats of one record inside this window are one line with a count.
COLLAPSE_WINDOW_S = 60.0
# How close before a kill earlyoom's memory reading has to be to belong to it.
READING_ATTACH_S = 2.0
JOURNALCTL_TIMEOUT_S = 60.0

# The loggers Decky's own code names its records with. A plugin's own output
# goes through the same unit, so a record is Decky's only under one of these.
DECKY_LOGGERS = frozenset({
    "loader", "plugin", "browser", "main", "updater", "utilities", "injector", "wsrouter", "base_events",
    "localplatformlinux", "localplatform", "localsocket", "settings", "sandboxed_plugin", "helpers",
})
_DECKY_RECORD = re.compile(r"^\[(?P<logger>[A-Za-z_]+)\]\[(?P<level>[A-Z]+)\]: (?P<message>.*)$", re.S)

Runner = Callable[[list[str]], tuple[int, str, str]]


@dataclass
class Event:
    at: float
    boot: int
    kind: str
    name: str
    text: str
    fields: dict[str, Any] = field(default_factory=dict)
    count: int = 1
    until: float | None = None
    key: str = ""

    def as_dict(self) -> dict[str, Any]:
        value = {
            "at": _iso(self.at), "boot": self.boot, "kind": self.kind, "event": self.name,
            "text": self.text, "fields": self.fields,
        }
        if self.count > 1:
            value["count"] = self.count
            value["until"] = _iso(self.until or self.at)
        return value


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")


def _seconds(entry: dict[str, Any]) -> float:
    return int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000


def _message(entry: dict[str, Any]) -> str:
    message = entry.get("MESSAGE", "")
    if isinstance(message, list):  # journald hands binary messages over as bytes
        message = bytes(message).decode("utf-8", "replace")
    return str(message)


# --- earlyoom ---------------------------------------------------------------

_READING = re.compile(
    r"mem avail:\s*(?P<avail>\d+) of\s*(?P<total>\d+) MiB.*?swap free:\s*(?P<swap>\d+) of\s*(?P<swap_total>\d+) MiB"
)
_KILL = re.compile(
    r'sending (?P<signal>SIG[A-Z]+) to process (?P<pid>\d+) uid (?P<uid>\d+) "(?P<name>[^"]*)":'
    r".*?VmRSS (?P<rss>\d+) MiB"
)
_EXITED = re.compile(r"process (?P<pid>\d+) exited after (?P<seconds>[\d.]+) seconds")
_ESCALATED = re.compile(r"escalating to SIGKILL")


def parse_earlyoom(entries: Iterable[dict[str, Any]], boot: int) -> list[Event]:
    events: list[Event] = []
    pending: tuple[float, dict[str, int]] | None = None
    kills: dict[int, Event] = {}
    last_kill: Event | None = None

    def flush_reading() -> None:
        nonlocal pending
        if pending is not None:
            moment, values = pending
            events.append(Event(
                moment, boot, "memory", "earlyoom.reading",
                f"{values['avail']} of {values['total']} MiB available, swap {values['swap']} of {values['swap_total']} MiB free",
                values,
            ))
            pending = None

    for entry in entries:
        if entry.get("SYSLOG_IDENTIFIER") != "earlyoom":
            continue
        moment, message = _seconds(entry), _message(entry)
        reading = _READING.search(message)
        if reading:
            flush_reading()
            pending = (moment, {key: int(value) for key, value in reading.groupdict().items()})
            continue
        kill = _KILL.search(message)
        if kill:
            values = {
                "signal": kill["signal"], "pid": int(kill["pid"]), "uid": int(kill["uid"]),
                "name": kill["name"], "rss_mib": int(kill["rss"]),
            }
            if pending is not None and moment - pending[0] <= READING_ATTACH_S:
                values.update(available_mib=pending[1]["avail"], swap_free_mib=pending[1]["swap"])
                pending = None
            else:
                flush_reading()
            # A second signal to the same process is the same kill, escalated.
            earlier = kills.get(values["pid"])
            if earlier is not None and moment - earlier.at <= COLLAPSE_WINDOW_S:
                _escalate(earlier, values["signal"])
                continue
            # Keyed by name, so a process Steam keeps restarting into the same
            # shortage is one line with a count rather than a screen of them.
            kills[values["pid"]] = Event(moment, boot, "oom", "earlyoom.kill", "", values,
                                         key=f"earlyoom.kill:{values['name']}")
            last_kill = kills[values["pid"]]
            events.append(last_kill)
            continue
        if _ESCALATED.search(message) and last_kill is not None:
            _escalate(last_kill, "SIGKILL")
            continue
        exited = _EXITED.search(message)
        if exited and int(exited["pid"]) in kills:
            kills[int(exited["pid"])].fields["exited_after_s"] = float(exited["seconds"])
    flush_reading()
    for event in kills.values():
        values = event.fields
        event.text = (
            f"earlyoom {values['signal']} {values['name']} (pid {values['pid']}, uid {values['uid']})"
            f" at {values['rss_mib']} MiB"
            + (f" · {values['available_mib']} MiB available, swap {values['swap_free_mib']} MiB free"
               if "available_mib" in values else "")
            + (f" · exited after {values['exited_after_s']:.1f}s" if "exited_after_s" in values else "")
        )
    return events


def _escalate(event: Event, signal: str) -> None:
    if not str(event.fields["signal"]).endswith(signal):
        event.fields["signal"] = f"{event.fields['signal']}→{signal}"


# --- service units ----------------------------------------------------------

_UNIT_PREFIX = re.compile(r"^(?P<unit>[\w@.\-]+\.service): (?P<rest>.*)$")
_MAIN_EXITED = re.compile(r"Main process exited, code=(?P<code>\w+), status=(?P<status>\S+)")
_RESULT = re.compile(r"Failed with result '(?P<result>[^']+)'")
_REMAINS = re.compile(r"Unit process (?P<pid>\d+) \((?P<name>[^)]*)\) remains running after unit stopped")
_LEFT_OVER = re.compile(r"Found left-over process (?P<pid>\d+) \((?P<name>[^)]*)\) in control group")
_CONSUMED = re.compile(r"Consumed (?P<cpu>.+?) CPU time(?: over (?P<wall>.+?) wall clock time)?(?P<rest>.*)\.$")
_PEAK = re.compile(r"(?P<value>[\d.]+[KMGT]?B?) memory peak")
_SWAP_PEAK = re.compile(r"(?P<value>[\d.]+[KMGT]?B?) memory swap peak")
_RESTART = re.compile(r"Scheduled restart job, restart counter is at (?P<n>\d+)")
_STARTED = re.compile(r"^(?P<verb>Started|Stopped|Stopping) (?P<description>.+?)\.{0,3}$")


def parse_unit(entries: Iterable[dict[str, Any]], boot: int, unit: str) -> list[Event]:
    """systemd's own records about one unit, one event per stop or start.

    systemd writes a stop as several records within a second: how the main
    process exited, the result, every process left running, and what the unit
    consumed. They are one thing that happened and are put on one line.
    """
    events: list[Event] = []
    current: Event | None = None
    short = unit.removesuffix(".service")

    def group(moment: float, name: str) -> Event:
        nonlocal current
        if current is None or current.name != name or moment - (current.until or current.at) > 5:
            current = Event(moment, boot, "unit", name, "", {"unit": short})
            events.append(current)
        current.until = moment
        return current

    for entry in entries:
        if entry.get("SYSLOG_IDENTIFIER") != "systemd":
            continue
        moment, message = _seconds(entry), _message(entry)
        prefixed = _UNIT_PREFIX.match(message)
        rest = prefixed["rest"] if prefixed and prefixed["unit"] == unit else None
        if rest is not None:
            if match := _MAIN_EXITED.search(rest):
                group(moment, "unit.stopped").fields.update(exit_code=match["code"], exit_status=match["status"])
            elif match := _RESULT.search(rest):
                group(moment, "unit.stopped").fields["result"] = match["result"]
            elif match := _REMAINS.search(rest):
                group(moment, "unit.stopped").fields.setdefault("left_running", []).append(
                    {"pid": int(match["pid"]), "name": match["name"]})
            elif match := _CONSUMED.search(rest):
                values = group(moment, "unit.stopped").fields
                if peak := _PEAK.search(match["rest"]):
                    values["memory_peak"] = peak["value"]
                if swap := _SWAP_PEAK.search(match["rest"]):
                    values["swap_peak"] = swap["value"]
                if match["wall"]:
                    values["ran_for"] = match["wall"]
            elif match := _LEFT_OVER.search(rest):
                group(moment, "unit.started").fields.setdefault("left_over", []).append(
                    {"pid": int(match["pid"]), "name": match["name"]})
            elif match := _RESTART.search(rest):
                events.append(Event(moment, boot, "unit", "unit.restart_scheduled",
                                    f"{short}: restart scheduled, counter {match['n']}",
                                    {"unit": short, "counter": int(match["n"])}))
            continue
        if match := _STARTED.match(message):
            # The description, not the unit name, is what these carry; they
            # arrive in this unit's own query, so they are this unit's.
            name = {"Started": "unit.started", "Stopped": "unit.stopped", "Stopping": "unit.stopping"}[match["verb"]]
            if name == "unit.stopping":
                events.append(Event(moment, boot, "unit", name, f"{short}: stopping", {"unit": short}))
            else:
                group(moment, name)
    for event in events:
        if event.name in {"unit.stopped", "unit.started"} and not event.text:
            event.text = _describe_unit(event)
    return events


def _names(processes: list[dict[str, Any]]) -> str:
    return ", ".join(f"{process['name']} {process['pid']}" for process in processes)


def _describe_unit(event: Event) -> str:
    values = event.fields
    if event.name == "unit.started":
        text = f"{values['unit']} started"
        if values.get("left_over"):
            text += f"; found left over from before: {_names(values['left_over'])}"
        return text
    text = f"{values['unit']} stopped"
    if "exit_code" in values:
        text += f" (main process {values['exit_code']}, status {values['exit_status']})"
    if "result" in values:
        text += f", result {values['result']}"
    if values.get("left_running"):
        text += f"; left running: {_names(values['left_running'])}"
    if "memory_peak" in values:
        text += f" · memory peak {values['memory_peak']}"
        if "swap_peak" in values:
            text += f", swap peak {values['swap_peak']}"
    if "ran_for" in values:
        text += f" · ran {values['ran_for']}"
    return text


# --- Decky's own records ----------------------------------------------------

_DECKY_PATTERNS: tuple[tuple[str, str, re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    ("browser", "decky.install_started",
     re.compile(r"^Installing (?P<plugin>.+?) from (?P<source>URL|local ZIP file) \(Version: (?P<version>[^)]*)\)"),
     lambda m: f"installing {m['plugin']} {m['version']} from {'the store' if m['source'] == 'URL' else 'a local ZIP'}"),
    ("browser", "decky.installed", re.compile(r"^Installed (?P<plugin>.+?) \(Version: (?P<version>[^)]*)\)"),
     lambda m: f"installed {m['plugin']} {m['version']}"),
    ("browser", "decky.uninstalling", re.compile(r"^uninstalling (?P<plugin>.+)$"),
     lambda m: f"uninstalling {m['plugin']} (the old version, as part of a replacement or a removal)"),
    ("loader", "decky.loaded", re.compile(r"^Loaded (?P<plugin>.+?)(?: \(v(?P<version>[^)]*)\))?$"),
     lambda m: f"loaded {m['plugin']}" + (f" {m['version']}" if m["version"] else "")),
    ("loader", "decky.reload_refused",
     re.compile(r"^Plugin (?P<plugin>.+?) is already loaded and has requested to not be re-loaded"),
     lambda m: f"refused to reload {m['plugin']}, already loaded"),
    ("loader", "decky.method_failed", re.compile(r"^Method (?P<method>\S+) of plugin (?P<plugin>.+?) failed"),
     lambda m: f"{m['plugin']}: method {m['method']} failed"),
    ("plugin", "decky.plugin_stopping", re.compile(r"^Shutting down (?P<plugin>.+)$"),
     lambda m: f"stopping {m['plugin']}"),
    ("plugin", "decky.plugin_sigkill",
     re.compile(r"^Plugin (?P<plugin>.+?) still alive (?P<seconds>\d+) seconds after stop request"),
     lambda m: f"{m['plugin']} still alive {m['seconds']}s after its stop request: SIGKILL"),
    ("plugin", "decky.plugin_stopped", re.compile(r"^Plugin (?P<plugin>.+?) has been stopped in (?P<seconds>[\d.]+)s"),
     lambda m: f"{m['plugin']} stopped in {m['seconds']}s"),
    ("main", "decky.started", re.compile(r"^Starting Decky version (?P<version>\S+)"),
     lambda m: f"Decky {m['version']} starting"),
    ("main", "decky.shutting_down", re.compile(r"^Shutting down\.\.\.$"), lambda m: "Decky shutting down"),
    ("main", "decky.webhelper_crash_count",
     re.compile(r"webhelper crashed within a minute from last crash! crash count: (?P<count>\d+)"),
     lambda m: f"Steam's webhelper crashed again within a minute: count {m['count']} (Decky stops itself at 3)"),
    ("main", "decky.frontend_loading", re.compile(r"^Loading Decky frontend!$"), lambda m: "frontend loading"),
    ("main", "decky.cef_disconnected", re.compile(r"^CEF has disconnected"), lambda m: "CEF disconnected"),
    ("localplatformlinux", "decky.webhelper_restart", re.compile(r"^Restarting steamwebhelper$"),
     lambda m: "restarting Steam's webhelper"),
)


def parse_decky(entries: Iterable[dict[str, Any]], boot: int) -> list[Event]:
    events: list[Event] = []
    for entry in entries:
        if entry.get("SYSLOG_IDENTIFIER") == "systemd":
            continue
        record = _DECKY_RECORD.match(_message(entry))
        if record is None or record["logger"] not in DECKY_LOGGERS:
            continue
        logger, level, message = record["logger"], record["level"], record["message"].strip()
        moment = _seconds(entry)
        for expected, name, pattern, describe in _DECKY_PATTERNS:
            if logger != expected:
                continue
            match = pattern.search(message)
            if match:
                values = {key: value for key, value in match.groupdict().items() if value is not None}
                events.append(Event(moment, boot, "decky", name, describe(match), values,
                                    key=f"{name}:{values.get('plugin', '')}"))
                break
        else:
            if level in {"WARNING", "ERROR", "CRITICAL"}:
                text = message.splitlines()[0][:200] if message else ""
                events.append(Event(moment, boot, "decky", f"decky.{level.lower()}", f"[{logger}] {text}",
                                    {"logger": logger, "level": level}, key=f"{logger}:{level}:{text}"))
    return _join_loads(events)


def _join_loads(events: list[Event]) -> list[Event]:
    """A loader start loads every plugin within a few seconds: one line for all of them."""
    result: list[Event] = []
    for event in events:
        held = result[-1] if result else None
        if (event.name == "decky.loaded" and held is not None and held.name == "decky.loaded"
                and event.at - (held.until or held.at) <= 5):
            held.fields.setdefault("plugins", []).append(event.text.removeprefix("loaded "))
            held.until = event.at
            continue
        result.append(event)
    for event in result:
        if event.name == "decky.loaded" and "plugins" in event.fields:
            first = event.text.removeprefix("loaded ")
            names = [first, *event.fields["plugins"]]
            event.fields = {"plugins": names}
            event.text = f"loaded {len(names)} plugins: {', '.join(names)}"
    return result


# --- crashes, the kernel and the session ------------------------------------

_COREDUMP = re.compile(
    r"Process (?P<pid>\d+) \((?P<name>[^)]*)\) of user (?P<uid>\d+) "
    r"(?:terminated abnormally with signal (?P<signal>[^\s,]+)|dumped core)"
)
_KERNEL_OOM = re.compile(r"Out of memory: Killed process (?P<pid>\d+) \((?P<name>[^)]*)\).*?anon-rss:(?P<anon>\d+)kB")
_SEGFAULT = re.compile(r"(?P<name>[^\s\[]+)\[(?P<pid>\d+)\]: segfault at")
_SESSION = (
    (re.compile(r"Power key pressed (?P<how>\w+)"), "session.power_key", lambda m: f"power key pressed {m['how']}"),
    (re.compile(r"The system will suspend now"), "session.suspend", lambda m: "suspending"),
    (re.compile(r"Operation 'suspend' finished"), "session.resume", lambda m: "resumed from suspend"),
    (re.compile(r"System is rebooting"), "session.reboot", lambda m: "rebooting"),
    (re.compile(r"System is powering (?:down|off)"), "session.power_off", lambda m: "powering off"),
)


def parse_coredump(entries: Iterable[dict[str, Any]], boot: int) -> list[Event]:
    events: dict[int, Event] = {}
    for entry in entries:
        match = _COREDUMP.search(_message(entry))
        if not match:
            continue
        pid = int(match["pid"])
        event = events.get(pid)
        if event is None:
            event = Event(_seconds(entry), boot, "crash", "crash.coredump", "",
                          {"pid": pid, "name": match["name"], "uid": int(match["uid"])})
            events[pid] = event
        if match["signal"]:
            event.fields["signal"] = match["signal"]
        else:
            event.fields["dumped_core"] = True
    for event in events.values():
        values = event.fields
        event.text = (f"{values['name']} (pid {values['pid']}) ended by {values.get('signal', 'a signal')}"
                      + (", core dumped" if values.get("dumped_core") else ""))
    return list(events.values())


def parse_kernel(entries: Iterable[dict[str, Any]], boot: int) -> list[Event]:
    events: list[Event] = []
    for entry in entries:
        message = _message(entry)
        if match := _KERNEL_OOM.search(message):
            values = {"pid": int(match["pid"]), "name": match["name"], "anon_rss_mib": int(match["anon"]) // 1024}
            events.append(Event(_seconds(entry), boot, "oom", "kernel.oom_kill",
                                f"kernel OOM killer took {values['name']} (pid {values['pid']}) at {values['anon_rss_mib']} MiB anonymous",
                                values))
        elif match := _SEGFAULT.search(message):
            events.append(Event(_seconds(entry), boot, "crash", "kernel.segfault",
                                f"{match['name']} (pid {match['pid']}) segfaulted",
                                {"pid": int(match["pid"]), "name": match["name"]}))
    return events


def parse_logind(entries: Iterable[dict[str, Any]], boot: int) -> list[Event]:
    events: list[Event] = []
    for entry in entries:
        message = _message(entry)
        for pattern, name, describe in _SESSION:
            if match := pattern.search(message):
                events.append(Event(_seconds(entry), boot, "session", name, describe(match), {}))
                break
    return events


# --- assembly ---------------------------------------------------------------

def collapse(events: list[Event], window: float = COLLAPSE_WINDOW_S) -> list[Event]:
    """Repeats of one keyed record within the window become one line with a count."""
    result: list[Event] = []
    open_by_key: dict[str, Event] = {}
    for event in sorted(events, key=lambda item: item.at):
        if event.key:
            held = open_by_key.get(event.key)
            if held is not None and event.at - (held.until or held.at) <= window:
                held.count += 1
                held.until = event.at
                if "rss_mib" in event.fields and "rss_mib" in held.fields:
                    held.fields["rss_mib_max"] = max(
                        held.fields.get("rss_mib_max", held.fields["rss_mib"]), event.fields["rss_mib"])
                continue
            open_by_key[event.key] = event
        result.append(event)
    for event in result:
        largest = event.fields.get("rss_mib_max")
        if largest is not None and largest > event.fields.get("rss_mib", 0):
            event.text += f" \u00b7 the largest of them {largest} MiB"
    return result


@dataclass(frozen=True)
class Boot:
    index: int
    boot_id: str
    first: float
    last: float


def journal_runner(arguments: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(
        ["journalctl", "--no-pager", *arguments], capture_output=True, text=True,
        timeout=JOURNALCTL_TIMEOUT_S, check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def list_boots(runner: Runner = journal_runner) -> list[Boot]:
    code, out, err = runner(["--list-boots", "-o", "json"])
    if code != 0:
        raise RuntimeError(f"journalctl --list-boots failed: {err.strip()[:200]}")
    return [
        Boot(int(row["index"]), str(row["boot_id"]), int(row["first_entry"]) / 1e6, int(row["last_entry"]) / 1e6)
        for row in json.loads(out or "[]")
    ]


def read_source(selector: list[str], boot: Boot, window: list[str], runner: Runner) -> tuple[list[dict[str, Any]], str | None]:
    """One source's entries in one boot, and why it could not be read, if it could not."""
    code, out, err = runner([
        "-b", boot.boot_id, *window, *selector, "-o", "json", f"--output-fields={FIELDS}",
        "-n", str(MAX_ENTRIES),
    ])
    if code != 0:
        return [], (err.strip() or f"journalctl exited {code}")[:200]
    entries = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    problem = None
    if any(sign in err for sign in ("insufficient permissions", "No journal files were opened", "not seeing messages")):
        problem = "the journal could not be read in full by this user: " + err.strip().splitlines()[0][:160]
    elif len(entries) >= MAX_ENTRIES:
        problem = f"truncated at {MAX_ENTRIES} records"
    return entries, problem


def timeline(
    boots: list[Boot], *, units: Iterable[str] = DEFAULT_UNITS, kinds: Iterable[str] = KINDS,
    since: str | None = None, until: str | None = None, runner: Runner = journal_runner,
) -> dict[str, Any]:
    window = [*(["--since", since] if since else []), *(["--until", until] if until else [])]
    wanted = set(kinds)
    report: dict[str, Any] = {"schema": SCHEMA, "kinds": sorted(wanted), "boots": []}
    for boot in boots:
        events: list[Event] = []
        problems: dict[str, str] = {}

        def source(name: str, selector: list[str], parse: Callable[[list[dict[str, Any]]], list[Event]]) -> None:
            entries, problem = read_source(selector, boot, window, runner)
            if problem:
                problems[name] = problem
            events.extend(parse(entries))

        if wanted & {"oom", "memory"}:
            source("earlyoom", ["-u", "earlyoom.service"], lambda entries: parse_earlyoom(entries, boot.index))
        # The kernel's records hold both its OOM kills and its segfaults.
        if wanted & {"oom", "crash"}:
            source("kernel", ["-k"], lambda entries: parse_kernel(entries, boot.index))
        if wanted & {"unit", "decky"}:
            for unit in units:
                source(unit, ["-u", unit], lambda entries, unit=unit: [
                    *parse_unit(entries, boot.index, unit),
                    *(parse_decky(entries, boot.index) if unit == "plugin_loader.service" else []),
                ])
        if "crash" in wanted:
            source("coredump", ["-t", "systemd-coredump"], lambda entries: parse_coredump(entries, boot.index))
        if "session" in wanted:
            source("logind", ["-u", "systemd-logind.service"], lambda entries: parse_logind(entries, boot.index))
        kept = collapse([event for event in events if event.kind in wanted])
        counts: dict[str, int] = {}
        for event in kept:
            counts[event.kind] = counts.get(event.kind, 0) + 1
        report["boots"].append({
            "index": boot.index, "boot_id": boot.boot_id, "first": _iso(boot.first), "last": _iso(boot.last),
            "counts": counts, "unreadable": problems, "events": [event.as_dict() for event in kept],
        })
    return report


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for boot in report["boots"]:
        counts = ", ".join(f"{count} {kind}" for kind, count in sorted(boot["counts"].items())) or "nothing"
        # The end's date only where it is another day, which is most boots that
        # matter: a device is rarely rebooted daily.
        same_day = boot["last"][:10] == boot["first"][:10]
        last = boot["last"][11:19] if same_day else boot["last"][:19].replace("T", " ")
        lines.append(f"== boot {boot['index']} {boot['boot_id'][:8]} {boot['first'][:19].replace('T', ' ')}"
                     f" → {last} · {counts}")
        for name, problem in sorted(boot["unreadable"].items()):
            lines.append(f"   ! {name}: {problem}")
        for event in boot["events"]:
            moment = event["at"][11:19]
            repeat = f" ×{event['count']} until {event['until'][11:19]}" if event.get("count") else ""
            lines.append(f"{moment}  {event['kind']:<7} {event['text']}{repeat}")
    return "\n".join(lines)


def select_boots(boots: list[Boot], wanted: list[str]) -> list[Boot]:
    if not wanted:
        wanted = ["0"]
    if "all" in wanted:
        return boots
    chosen: list[Boot] = []
    for token in wanted:
        try:
            index = int(token)
        except ValueError:
            raise ValueError(f"--boot takes an index such as 0 or -2, or all: {token!r}") from None
        match = [boot for boot in boots if boot.index == index]
        if not match:
            raise ValueError(f"the journal holds no boot {index}; --list-boots says which it does")
        chosen.extend(match)
    return sorted(set(chosen), key=lambda boot: boot.index)


def main(argv: list[str] | None = None, runner: Runner = journal_runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list-boots", action="store_true", help="list the boots the journal holds and stop")
    parser.add_argument("--boot", action="append", default=[],
                        help="a boot index as journalctl numbers them (0 is this one, -1 the one before), or all; repeatable")
    parser.add_argument("--since", help="journalctl's own --since syntax, applied inside every boot read")
    parser.add_argument("--until", help="journalctl's own --until syntax")
    parser.add_argument("--only", help=f"comma-separated kinds out of {', '.join(KINDS)}")
    parser.add_argument("--unit", action="append", default=[],
                        help="another system unit whose starts, stops and leftovers to include; repeatable")
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The device timeline", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    kinds = KINDS
    if args.only:
        kinds = tuple(kind.strip() for kind in args.only.split(",") if kind.strip())
        unknown = sorted(set(kinds) - set(KINDS))
        if unknown:
            parser.error(f"unknown kind(s) {', '.join(unknown)}; choose from {', '.join(KINDS)}")
    units = [*DEFAULT_UNITS, *(unit if unit.endswith(".service") else f"{unit}.service" for unit in args.unit)]
    try:
        boots = list_boots(runner)
        if args.list_boots:
            for boot in boots:
                print(f"{boot.index:>4}  {boot.boot_id}  {_iso(boot.first)[:19]} → {_iso(boot.last)[:19]}")
            return 0
        report = timeline(select_boots(boots, args.boot), units=dict.fromkeys(units), kinds=kinds,
                          since=args.since, until=args.until, runner=runner)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"target device timeline: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
