#!/usr/bin/env python3
"""Record what Decky and every plugin backend are holding, over time.

A Decky Loader that has grown to gigabytes takes the whole device with it: the
machine swaps, the websocket stops answering, and every plugin looks hung,
including this one. What that leaves behind is a symptom and no curve. Whether
the growth is continuous or tied to something, and whether the process holding
it is the loader or a plugin backend, is the whole question, and neither is
answerable after a reboot.

So this samples it. One line per interval: the loader's resident size, each
plugin backend's, and the machine's free memory and swap, written where the
whole run can be read afterwards. It reads `/proc` and nothing else: no plugin
is called, no Steam state is touched, and nothing about Decky is changed.

Leave it running across the session that is suspected, then read the file. A
process whose resident size climbs while nothing is being asked of it is a leak
with a rate; one that climbs only while a screen is open, a game is running or a
plugin is reloaded is a leak with a trigger, and the trigger is what a report
upstream needs. Attribution is by process, because that is what the numbers can
actually support: the loader relays for every plugin, so a loader that grows is
not evidence about any one of them.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

if __package__:
    from . import host_platform
else:
    import host_platform

SCHEMA = 1
#: The loader's own worker, which is the process that held 13 GB when this was
#: written. Its parent supervisor is tiny and is reported with it.
LOADER_PATTERNS = ("Decky Loader", "PluginLoader")
#: A plugin backend, as Decky names it in its own process title.
PLUGIN_PATTERN = re.compile(r"^(?P<name>.+?) \((?P<path>/.*/plugins/(?P<plugin>[^/]+)/.*)\)$")
DEFAULT_INTERVAL = 60.0
DEFAULT_SECONDS = 3600.0


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rss_kb(pid: str) -> int | None:
    status = _read(Path(f"/proc/{pid}/status"))
    match = re.search(r"^VmRSS:\s+(\d+) kB", status, re.MULTILINE)
    return int(match.group(1)) if match else None


def processes() -> list[dict[str, object]]:
    """Every Decky process this sample can see, by what it calls itself."""
    found: list[dict[str, object]] = []
    for entry in sorted(Path("/proc").iterdir(), key=lambda item: item.name):
        if not entry.name.isdigit():
            continue
        title = _read(entry / "cmdline").replace("\x00", " ").strip()
        if not title:
            continue
        plugin = PLUGIN_PATTERN.match(title)
        if plugin and "/plugins/" in title:
            kind, label = "plugin", plugin.group("plugin")
        elif any(pattern in title for pattern in LOADER_PATTERNS):
            kind, label = "loader", "decky-loader"
        else:
            continue
        rss = _rss_kb(entry.name)
        if rss is None:
            continue
        found.append({"pid": int(entry.name), "kind": kind, "label": label, "rss_kb": rss})
    return found


def memory() -> dict[str, int]:
    """What the machine has left, which is what a climb eventually costs."""
    values = {}
    for line in _read(Path("/proc/meminfo")).splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            values[key] = int(rest.strip().split()[0])
    return values


def sample() -> dict[str, object]:
    return {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "memory_kb": memory(), "processes": processes()}


def summarize(reading: dict[str, object]) -> str:
    rows = reading["processes"]  # type: ignore[index]
    loader = sum(row["rss_kb"] for row in rows if row["kind"] == "loader")  # type: ignore[index]
    plugins = [row for row in rows if row["kind"] == "plugin"]  # type: ignore[index]
    biggest = max(plugins, key=lambda row: row["rss_kb"], default=None)
    free = reading["memory_kb"].get("MemAvailable", 0) // 1024  # type: ignore[union-attr]
    tail = "" if biggest is None else f" biggest_plugin={biggest['label']}:{biggest['rss_kb'] // 1024}M"
    return f"{reading['at']} loader={loader // 1024}M plugins={len(plugins)} available={free}M{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="how long to watch")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="seconds between samples")
    parser.add_argument("--out", type=Path, help="append one JSON sample per line here as well")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The Decky memory watch")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if not 1 <= args.interval <= 3600:
        parser.error("--interval must be between 1 and 3600 seconds")
    if not args.interval <= args.seconds <= 86400:
        parser.error("--seconds must be at least one interval and at most a day")

    deadline = time.monotonic() + args.seconds
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        reading = sample()
        print(summarize(reading), flush=True)
        if args.out:
            with args.out.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"schema": SCHEMA, **reading}, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        if time.monotonic() >= deadline - args.interval:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
