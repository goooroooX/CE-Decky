#!/usr/bin/env python3
"""Prove that Cheat Engine actually starts, using the production launch path.

This is the agent-verifiable half of proving an attached launch. It runs the same production code
the QAM button runs — exact installed Proton identity, private Cheat Engine
    runtime copy, Valve's ``proton run`` verb, synthetic self-test session,
resident bridge heartbeat — without Decky, without Steam mutation, without a
game and without a controller. A human is only needed afterwards to confirm the
same thing happens from the QAM.

Read-only with respect to Steam. It writes only inside the plugin-owned managed
root (private runtime copy, isolated self-test prefix, self-test session files).

    python scripts/target_ce_launch_probe.py --home <exact DECKY_USER_HOME> --list-tools
    python scripts/target_ce_launch_probe.py --home <exact DECKY_USER_HOME> \
        --settings-dir "$DECKY_PLUGIN_SETTINGS_DIR" --tool-id <exact tool ID>
    python scripts/target_ce_launch_probe.py --home <exact DECKY_USER_HOME> \
        --ce-executable <exact .exe> --tool-id <exact tool ID>

``target_plugin_install.py authority`` reports that home as ``user_home`` and
the settings directory as ``settings_dir``.

A launch always needs ``--tool-id``, because the probe never guesses which
installed Proton to run Cheat Engine under. Only ``--list-tools`` runs without
one, and a launch that omits it is refused with the installed identities in the
same report, so the refusal is the listing.

The Cheat Engine identity is never guessed: pass either the plugin's Decky
settings directory, so the exact registered executable is read from the plugin's
own config, or the exact executable path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys

if __package__:
    from . import host_platform
else:
    import host_platform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

from ce_decky.ce_import import inspect_ce_selection  # noqa: E402
from ce_decky.config import ConfigStore  # noqa: E402
from ce_decky.atomic import read_regular_bytes  # noqa: E402
from ce_decky.ce_launch import (  # noqa: E402
    MAX_ENVIRON_BYTES,
    MAX_SCANNED_PROCESSES,
    CELaunchSupervisor,
    parse_environ,
)
from ce_decky.ce_runtime import materialize_private_runtime  # noqa: E402
from ce_decky.managed_ce import discover_proton_tools  # noqa: E402

POLL_SECONDS = 0.25
TERMINAL_STATES = frozenset({"stopped", "failed", "cancelled"})
MAX_PREFIX_OWNERSHIP_ENTRIES = 100_000


def _managed_root(home: Path, override: str | None) -> Path:
    return Path(override).expanduser().resolve() if override else home / ".cheat-engine-decky"


def _registered_executable(settings_dir: str) -> Path:
    """Read the exact Cheat Engine executable the plugin registered."""
    config = ConfigStore(Path(settings_dir).expanduser().resolve() / "config.json").load()
    if not config.imported_ce_executable:
        raise ValueError("the plugin has no registered Cheat Engine executable; install or import one first")
    return Path(config.imported_ce_executable)


def _matching_owned_pids(descriptor: str, digest: str, proc_root: Path = Path("/proc")) -> list[int]:
    """Find surviving processes that inherited this exact owned descriptor."""
    matches: list[int] = []
    try:
        entries = sorted(
            (entry for entry in os.scandir(proc_root) if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return matches
    for entry in entries[:MAX_SCANNED_PROCESSES]:
        try:
            raw = read_regular_bytes(
                Path(entry.path) / "environ",
                max_bytes=MAX_ENVIRON_BYTES,
                allow_missing=True,
                allow_empty=True,
            )
        except (OSError, ValueError):
            continue
        if not raw:
            continue
        environ = parse_environ(raw)
        if (
            environ.get("CE_DECKY_DESCRIPTOR") == descriptor
            and environ.get("CE_DECKY_DESCRIPTOR_SHA256") == digest
        ):
            matches.append(int(entry.name))
    return matches


def _prefix_ownership(prefix: Path, expected_uid: int) -> dict[str, object]:
    try:
        root_info = prefix.stat(follow_symlinks=False)
    except OSError as exc:
        return {"ok": False, "scanned": 0, "truncated": False, "foreign": [], "error": str(exc)[:512]}
    scanned = 1
    foreign: list[dict[str, object]] = []
    if root_info.st_uid != expected_uid:
        foreign.append({"path": str(prefix), "uid": root_info.st_uid})
    pending = [prefix]
    while pending and scanned < MAX_PREFIX_OWNERSHIP_ENTRIES:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            return {"ok": False, "scanned": scanned, "truncated": False, "foreign": foreign, "error": str(exc)[:512]}
        for entry in entries:
            if scanned >= MAX_PREFIX_OWNERSHIP_ENTRIES:
                break
            scanned += 1
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                return {"ok": False, "scanned": scanned, "truncated": False, "foreign": foreign, "error": str(exc)[:512]}
            if info.st_uid != expected_uid and len(foreign) < 32:
                foreign.append({"path": entry.path, "uid": info.st_uid})
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
    truncated = bool(pending)
    return {
        "ok": not foreign and not truncated,
        "scanned": scanned,
        "truncated": truncated,
        "foreign": foreign,
        "error": None,
    }


async def _probe(home: Path, managed_root: Path, executable: Path, tool_id: str | None) -> dict[str, object]:
    tools = discover_proton_tools(home)
    if not tools:
        return {"ok": False, "state": "blocked", "error": "no installed Proton tool was discovered", "tools": []}
    if not tool_id:
        return {
            "ok": False,
            "state": "blocked",
            "error": "pass --tool-id with one of the identities listed in this report; the launch probe never guesses an installed tool",
            "tools": [item.public() for item in tools],
        }
    tool = next((item for item in tools if item.tool_id == tool_id), None)
    if tool is None:
        return {"ok": False, "state": "blocked", "error": "the requested Proton tool ID is not installed",
                "tools": [item.public() for item in tools]}

    imported = inspect_ce_selection(str(executable))
    runtime = materialize_private_runtime(
        source_root=Path(imported.root),
        source_executable=Path(imported.executable),
        source_executable_sha256=imported.sha256,
        managed_ce_root=managed_root / "ce",
        bridge_source=ROOT / "py_modules" / "ce_decky" / "ce_decky_bridge.lua",
    )
    supervisor = CELaunchSupervisor(
        home, managed_root / "ce", managed_root / "state", logging.getLogger("ce-launch-probe")
    )
    try:
        started = await supervisor.start_self_test(
            tool, Path(runtime.executable), runtime.source_executable_sha256
        )
        operation_id = str(started["operation_id"])
        while True:
            current = supervisor.status(operation_id)
            if current["state"] in TERMINAL_STATES:
                break
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await supervisor.close()
    effective_uid = os.geteuid()
    plan = current["plan"]
    survivors = _matching_owned_pids(
        str(plan["descriptor_windows_path"]), str(plan["descriptor_sha256"])
    )
    prefix_ownership = _prefix_ownership(Path(str(plan["compat_data_path"])), effective_uid)
    return {
        "ok": (
            current["state"] == "stopped"
            and current["bridge"] is not None
            and not survivors
            and not supervisor._processes
            and bool(prefix_ownership["ok"])
        ),
        "state": current["state"],
        "error": current["error"],
        "message": current["message"],
        "proton_tool": tool.public(),
        "ce": {
            "executable": imported.executable,
            "executable_sha256": imported.sha256,
            "private_runtime": runtime.as_dict(),
        },
        "effective_uid": effective_uid,
        "launched_pid": current["pid"],
        "launched_pgid": current["pgid"],
        "plan": plan,
        "bridge": current["bridge"],
        "exit_code": current["exit_code"],
        "log_tail": current["log_tail"],
        "owned_process_pids_after_close": survivors,
        "supervisor_processes_after_close": len(supervisor._processes),
        "self_test_prefix_ownership": prefix_ownership,
        "tools": [item.public() for item in tools],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", required=True, help="exact DECKY_USER_HOME; `target_plugin_install.py authority` reports it as user_home")
    parser.add_argument("--ce-executable", help="exact imported Cheat Engine executable under the user home")
    parser.add_argument("--settings-dir", help="plugin DECKY_PLUGIN_SETTINGS_DIR; reads the registered executable")
    parser.add_argument("--managed-root", help="plugin-owned managed root (default <home>/.cheat-engine-decky)")
    parser.add_argument("--tool-id", help="exact Proton tool ID from --list-tools; required for a launch")
    parser.add_argument("--list-tools", action="store_true", help="only enumerate installed Proton identities")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        host_platform.require("The Cheat Engine launch probe")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    home = Path(arguments.home).expanduser().resolve()
    managed_root = _managed_root(home, arguments.managed_root)
    try:
        if arguments.list_tools:
            result: dict[str, object] = {
                "ok": True,
                "tools": [tool.public() for tool in discover_proton_tools(home)],
            }
        elif arguments.ce_executable:
            result = asyncio.run(
                _probe(home, managed_root, Path(arguments.ce_executable).expanduser(), arguments.tool_id)
            )
        elif arguments.settings_dir:
            result = asyncio.run(
                _probe(home, managed_root, _registered_executable(arguments.settings_dir), arguments.tool_id)
            )
        else:
            raise ValueError("pass --settings-dir or --ce-executable unless --list-tools is used")
    except (OSError, RuntimeError, ValueError) as exc:
        result = {"ok": False, "state": "failed", "error": str(exc)[:1000]}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
