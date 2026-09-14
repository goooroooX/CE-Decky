#!/usr/bin/env python3
"""Read only allowlisted Decky path/version variables from the live plugin process.

The probe scans readable `/proc/<pid>/environ` entries for CE Decky's plugin process
(or one explicit PID), never prints non-allowlisted environment variables, and does
not require privilege escalation. When DECKY_PLUGIN_NAME is absent, the exact
DECKY_PLUGIN_DIR/plugin.json name is the fallback identity; directory basename is
never treated as authority.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

if __package__:
    from . import host_platform
else:
    import host_platform

ALLOWLIST = (
    "DECKY_VERSION",
    "DECKY_USER",
    "DECKY_USER_HOME",
    "DECKY_HOME",
    "DECKY_PLUGIN_SETTINGS_DIR",
    "DECKY_PLUGIN_RUNTIME_DIR",
    "DECKY_PLUGIN_LOG_DIR",
    "DECKY_PLUGIN_LOG",
    "DECKY_PLUGIN_DIR",
    "DECKY_PLUGIN_NAME",
    "DECKY_PLUGIN_VERSION",
)
_PATH_KEYS = frozenset(
    {
        "DECKY_USER_HOME",
        "DECKY_HOME",
        "DECKY_PLUGIN_SETTINGS_DIR",
        "DECKY_PLUGIN_RUNTIME_DIR",
        "DECKY_PLUGIN_LOG_DIR",
        "DECKY_PLUGIN_LOG",
        "DECKY_PLUGIN_DIR",
    }
)
_PID = re.compile(r"^[1-9][0-9]*$")


def _read_env(path: Path) -> dict[str, str]:
    payload = path.read_bytes()
    result: dict[str, str] = {}
    for raw in payload.split(b"\0"):
        if not raw or b"=" not in raw:
            continue
        key_raw, value_raw = raw.split(b"=", 1)
        try:
            key = key_raw.decode("ascii")
        except UnicodeDecodeError:
            continue
        if key not in ALLOWLIST:
            continue
        result[key] = value_raw.decode("utf-8", errors="replace")
    return result


def _plugin_metadata_name(plugin_dir: str) -> str | None:
    raw = Path(plugin_dir).expanduser()
    if raw.is_symlink():
        return None
    try:
        root = raw.resolve(strict=True)
    except OSError:
        return None
    metadata = root / "plugin.json"
    if metadata.is_symlink() or not metadata.is_file():
        return None
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    name = payload.get("name") if isinstance(payload, dict) else None
    return name if isinstance(name, str) and name.strip() else None


def _candidate(pid: str, env: dict[str, str], plugin_name: str) -> dict[str, object] | None:
    plugin_dir = env.get("DECKY_PLUGIN_DIR", "").strip()
    user_home = env.get("DECKY_USER_HOME", "").strip()
    if not plugin_dir or not user_home:
        return None
    env_name = env.get("DECKY_PLUGIN_NAME", "").strip()
    identity_source = "DECKY_PLUGIN_NAME"
    if env_name:
        if env_name != plugin_name:
            return None
    else:
        metadata_name = _plugin_metadata_name(plugin_dir)
        if metadata_name != plugin_name:
            return None
        identity_source = "plugin.json"

    paths: dict[str, dict[str, object]] = {}
    for key in sorted(_PATH_KEYS):
        value = env.get(key, "").strip()
        if not value:
            continue
        raw = Path(value).expanduser()
        try:
            resolved = raw.resolve(strict=False)
        except OSError:
            resolved = raw.absolute()
        paths[key] = {
            "value": value,
            "resolved": str(resolved),
            "exists": resolved.exists(),
            "is_dir": resolved.is_dir(),
            "is_file": resolved.is_file(),
        }
    return {
        "pid": int(pid),
        "identity_source": identity_source,
        "environment": {key: env[key] for key in ALLOWLIST if key in env},
        "paths": paths,
    }


def probe(proc_root: Path, plugin_name: str, pid: int | None = None) -> dict[str, object]:
    raw_root = proc_root.expanduser()
    if raw_root.is_symlink():
        raise ValueError("proc root must not be a symlink")
    proc_root = raw_root.resolve(strict=True)
    if not proc_root.is_dir():
        raise ValueError("proc root must be a directory")
    pids = [str(pid)] if pid is not None else sorted(
        (entry.name for entry in proc_root.iterdir() if entry.is_dir() and _PID.fullmatch(entry.name)),
        key=int,
    )
    candidates: list[dict[str, object]] = []
    unreadable = 0
    for pid_text in pids:
        environ = proc_root / pid_text / "environ"
        try:
            env = _read_env(environ)
        except OSError:
            unreadable += 1
            continue
        item = _candidate(pid_text, env, plugin_name)
        if item is not None:
            candidates.append(item)
    state = "resolved" if len(candidates) == 1 else "missing" if not candidates else "ambiguous"
    return {
        "schema": 2,
        "plugin_name": plugin_name,
        "state": state,
        "ok": state == "resolved",
        "selected": candidates[0] if len(candidates) == 1 else None,
        "candidates": candidates,
        "unreadable_processes": unreadable,
        "privacy": "only allowlisted DECKY_* path/version variables are emitted",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-name", default="CE Decky")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        host_platform.require("The Decky environment probe")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if args.pid is not None and args.pid <= 0:
        print("target Decky env probe: PID must be positive", file=sys.stderr)
        return 2
    try:
        report = probe(args.proc_root, args.plugin_name, args.pid)
    except (OSError, ValueError) as exc:
        print(f"target Decky env probe: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
