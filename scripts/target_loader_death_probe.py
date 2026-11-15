#!/usr/bin/env python3
"""Does this exact CE Decky package leave when Decky's loader dies under it?

`docs/FIELD_NOTES.md` records why the question exists: Decky's unit runs with
`KillMode=process`, so a loader that dies leaves every plugin backend running,
and a backend whose loader has gone reads its socket at EOF in a loop that never
yields and grows until the machine kills it. `py_modules/ce_decky/loader_watch.py`
is the answer on this side, and this probe is how it is seen working against
Decky's real plugin host rather than a model of one.

The live loader is never touched. It runs as root, and killing it would orphan
every other plugin on the device too, which is the incident itself. Instead this
starts a private one: the device's own `PluginLoader` executable, as the
operator's user, through the PyInstaller resource-tracker route
`decky_compat_probe.py` uses, holding exactly this package through Decky's own
`PluginWrapper` (`target_loader_death_harness.py`). The process shape is the
service's: a PyInstaller bootloader, under it the loader, under that the
`multiprocessing` forkserver, under that the plugin. Everything the plugin
writes goes into the run's own directory under `build/loader-death/`.

Two things are then done to it, each to a process this probe started and whose
identity (pid, name and parent) is read again immediately before the signal:

1. The forkserver alone is killed. The loader still lives, so the plugin must
   stay: leaving then would close its socket under a loader still reading it.
2. The loader is killed. The plugin must leave, and the probe watches it for
   `--wait` seconds, sampling its resident size, and reads its log for
   `backend.loader_lost`.

Whatever is still running afterwards is killed with the whole session. A package
older than 0.9.36 has no watch, and is the control: it stays, and the line says
how much it grew. Exit 0 when the package stayed for (1) and left after (2),
1 when it did not, 2 when the probe could not run, 3 on a host that is not the
target. The plugin is started with an address-space limit of 4 GiB, so a control
that never leaves is bounded even if this probe is.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import resource
import shutil
import signal
import subprocess
import sys
import time
import zipfile

if __package__:
    from . import host_platform
    from .target_package_probe import inspect_package
else:
    import host_platform
    from target_package_probe import inspect_package

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
DEFAULT_OUTPUT_ROOT = REPO / "build" / "loader-death"
HARNESS_MODULE = "target_loader_death_harness"
HOME_VARIABLE = "LOADER_DEATH_PROBE_USER_HOME"
ADDRESS_SPACE_LIMIT = 4 << 30
START_TIMEOUT_S = 60.0
FORKSERVER_GRACE_S = 4.0
# The watch polls once a second from the moment it is armed, and everything up
# to the loader's kill is timed the same way on every run, so without this the
# kill lands at one point of that cycle every time and the time to leave is the
# probe's own phase rather than anything about the watch. Up to one interval
# more, drawn per run, makes repeated runs sample the cycle.
MAX_PHASE_JITTER_S = 1.0
DEFAULT_WAIT_S = 10.0
SAMPLE_S = 0.1


def read_stat(pid: int, proc_root: Path = Path("/proc")) -> dict[str, object] | None:
    """State, parent and processor ticks of one process, or None when it is gone."""
    try:
        text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = text.rfind(")")
    rest = text[end + 2:].split() if end >= 0 else []
    if len(rest) < 13:
        return None
    return {"state": rest[0], "ppid": int(rest[1]), "ticks": int(rest[11]) + int(rest[12])}


def _comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _rss_mib(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _alive(pid: int) -> bool:
    stat = read_stat(pid)
    return stat is not None and stat["state"] not in ("Z", "X")


def _log_lines(root: Path) -> list[str]:
    lines: list[str] = []
    for path in sorted((root / "homebrew" / "logs").rglob("*.log")):
        lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return lines


def _events(lines: list[str], needle: str) -> list[str]:
    return [line.split("event=", 1)[-1][:200] for line in lines if f"event={needle}" in line]


def _kill_checked(pid: int, *, comm: str, parent: int) -> None:
    """Signal one process only if it is still the one this probe identified."""
    stat = read_stat(pid)
    if stat is None or _comm(pid) != comm or stat["ppid"] != parent:
        raise RuntimeError(f"process {pid} is no longer the {comm} under {parent}; nothing was signalled")
    os.kill(pid, signal.SIGKILL)


def verdict(report: dict[str, object]) -> tuple[bool, str]:
    """Whether the package behaved as required, and the one line that says so."""
    phase_a = report.get("forkserver_killed") or {}
    phase_b = report.get("loader_killed") or {}
    stayed = bool(phase_a.get("plugin_alive")) and not phase_a.get("lost_logged")
    left_after = phase_b.get("plugin_exited_after_s")
    parts = [f"CE Decky {report.get('package_version')}", f"Decky runtime {str(report.get('runtime_sha256'))[:12]}"]
    started = report.get("started") or {}
    parts.append("watch armed" if started.get("watch_armed") else "no watch")
    parts.append("forkserver alone: stayed" if stayed else "forkserver alone: did not stay")
    first, last = phase_b.get("rss_first_mib"), phase_b.get("rss_last_mib")
    grown = f", RSS {first}→{last} MiB" if first is not None and last is not None else ""
    if left_after is not None:
        logged = " with backend.loader_lost" if phase_b.get("lost_logged") else ", backend.loader_lost not logged"
        parts.append(f"loader killed: left after {left_after}s{logged}{grown}")
    else:
        busy = phase_b.get("cpu_share")
        cpu = f", {busy:.0%} of a core" if isinstance(busy, (int, float)) else ""
        parts.append(f"loader killed: still running after {phase_b.get('waited_s')}s{grown}{cpu}, killed by the probe")
    ok = stayed and left_after is not None and bool(phase_b.get("lost_logged"))
    return ok, "target loader death probe: " + " · ".join(parts)


def _runtime_from_authority() -> Path:
    if __package__:
        from .target_plugin_install import resolve_install_authority
    else:
        from target_plugin_install import resolve_install_authority
    authority = resolve_install_authority()
    plugin_root = Path(str(authority["plugin_root"]))
    return plugin_root.parent.parent / "services" / "PluginLoader"


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_LIMIT, ADDRESS_SPACE_LIMIT))


def _digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(package: Path, expected_sha: str, runtime: Path, output_root: Path, wait_s: float) -> dict[str, object]:
    # The package's own version, not the repository's: an older package is the
    # control this probe is run against.
    with zipfile.ZipFile(package) as archive:
        metadata = archive.getinfo("CE-Decky/package.json")
        if metadata.file_size > 256 * 1024:
            raise ValueError("package metadata exceeds its limit")
        version = json.loads(archive.read(metadata))["version"]
    inspected = inspect_package(package, expect_version=version)
    if not inspected["ok"]:
        raise RuntimeError(f"package probe failed: {inspected['errors']!r}")
    if inspected["sha256"] != expected_sha:
        raise RuntimeError(f"package SHA mismatch: expected {expected_sha}, observed {inspected['sha256']}")
    if not runtime.is_file():
        raise RuntimeError(f"Decky runtime is not a file: {runtime}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = output_root / f"{stamp}-{inspected['version']}"
    (root / "home").mkdir(parents=True)
    (root / "homebrew" / "plugins").mkdir(parents=True)
    with zipfile.ZipFile(package) as archive:
        archive.extractall(root / "homebrew" / "plugins")
    pids_file = root / "pids.json"
    report: dict[str, object] = {
        "schema": 1, "package": str(package), "package_sha256": expected_sha,
        "package_version": inspected["version"], "runtime": str(runtime), "runtime_sha256": _digest(runtime),
        "directory": str(root),
    }
    code = (
        "from multiprocessing.resource_tracker import main; import sys; "
        f"sys.path.insert(0, {str(SCRIPTS)!r}); import {HARNESS_MODULE} as harness; "
        f"harness.run({str(pids_file)!r})"
    )
    environment = {
        "PATH": os.defpath, "HOME": str(root / "home"), "LANG": "C.UTF-8",
        "UNPRIVILEGED_PATH": str(root / "homebrew"), "UNPRIVILEGED_USER": os.environ.get("USER") or "deck",
        "CHOWN_PLUGIN_PATH": "0", "LIVE_RELOAD": "0", HOME_VARIABLE: str(root / "home"),
    }
    with (root / "loader.out").open("wb") as output:
        process = subprocess.Popen(
            [str(runtime), "-B", "-S", "-I", "-c", code], cwd=root, env=environment,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=_limits,
        )
    plugin: int | None = None
    try:
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if pids_file.is_file() and _events(_log_lines(root), "backend.initialized"):
                break
            if process.poll() is not None:
                raise RuntimeError(f"Decky's runtime exited with {process.returncode}; see {root / 'loader.out'}")
            time.sleep(0.2)
        else:
            raise RuntimeError(f"the plugin did not finish loading within {START_TIMEOUT_S:.0f}s; see {root}")
        pids = json.loads(pids_file.read_text(encoding="utf-8"))
        loader, plugin = int(pids["loader"]), int(pids["plugin"])
        forkserver = int((read_stat(plugin) or {}).get("ppid", 0))
        watch = _events(_log_lines(root), "backend.loader_watch_armed")
        report["started"] = {
            "bootloader": process.pid, "loader": loader, "forkserver": forkserver, "plugin": plugin,
            "loader_parent": (read_stat(loader) or {}).get("ppid"),
            "forkserver_parent": (read_stat(forkserver) or {}).get("ppid"),
            "watch_armed": bool(watch), "watch": watch[-1] if watch else None, "rss_mib": _rss_mib(plugin),
        }
        _kill_checked(forkserver, comm="PluginLoader", parent=loader)
        time.sleep(FORKSERVER_GRACE_S)
        report["forkserver_killed"] = {
            "plugin_alive": _alive(plugin), "plugin_parent": (read_stat(plugin) or {}).get("ppid"),
            "loader_alive": _alive(loader), "lost_logged": bool(_events(_log_lines(root), "backend.loader_lost")),
            "rss_mib": _rss_mib(plugin),
        }
        jitter = random.uniform(0.0, MAX_PHASE_JITTER_S)
        time.sleep(jitter)
        report["forkserver_killed"]["jitter_s"] = round(jitter, 2)
        before = read_stat(plugin)
        _kill_checked(loader, comm="PluginLoader", parent=process.pid)
        killed = time.monotonic()
        exited_after: float | None = None
        samples: list[int] = []
        last = before
        while time.monotonic() - killed < wait_s:
            if not _alive(plugin):
                exited_after = round(time.monotonic() - killed, 2)
                break
            size = _rss_mib(plugin)
            if size is not None:
                samples.append(size)
            last = read_stat(plugin) or last
            time.sleep(SAMPLE_S)
        waited = round(time.monotonic() - killed, 2)
        ticks = None if not (before and last) else int(last["ticks"]) - int(before["ticks"])
        hertz = os.sysconf("SC_CLK_TCK")
        report["loader_killed"] = {
            "plugin_exited_after_s": exited_after, "waited_s": waited,
            "rss_first_mib": samples[0] if samples else None, "rss_last_mib": samples[-1] if samples else None,
            "cpu_share": None if ticks is None or exited_after is not None else round(ticks / hertz / waited, 2),
            "lost_logged": bool(_events(_log_lines(root), "backend.loader_lost")),
            "log": _events(_log_lines(root), "backend.loader_lost"),
        }
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        # The orphan left the bootloader's tree but not its session's process
        # group, so the group kill above is what ends a control; this is only
        # for one that somehow left the group as well.
        if plugin is not None and _alive(plugin) and _comm(plugin) == "CE Decky":
            os.kill(plugin, signal.SIGKILL)
            report["orphan_killed_separately"] = True
    ok, line = verdict(report)
    report["ok"], report["summary"] = ok, line
    (root / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package", type=Path, help="the exact CE Decky ZIP")
    parser.add_argument("--sha256", required=True, help="the package's exact SHA-256")
    parser.add_argument("--runtime", type=Path,
                        help="Decky's PluginLoader; by default the one beside the installed plugin's homebrew root")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT_S,
                        help="seconds to watch the plugin after the loader is killed (1-60)")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--json", action="store_true", help="print the whole report")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The loader death probe")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if not 1 <= args.wait <= 60:
        parser.error("--wait must be between 1 and 60 seconds")
    try:
        runtime = args.runtime or _runtime_from_authority()
        report = probe(args.package.resolve(), args.sha256, runtime.resolve(), args.output_root, args.wait)
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"target loader death probe: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=1, sort_keys=True) if args.json else report["summary"])
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
