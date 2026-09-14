#!/usr/bin/env python3
"""Record one bounded read-only snapshot of the machine a claim came from.

A measurement without the machine that produced it is a rumor, and the host
facts a report needs are spread across `platform`, half a dozen command
versions, Decky's own environment, the directories this plugin and Steam own,
and the TLS trust store. This collects them once, as JSON, so a number quoted
in an issue or in `docs/FIELD_NOTES.md` can name the host it came from without
anybody re-deriving it by hand.

It is non-mutating and reads nothing privileged: no secrets, no tokens, and no
environment value outside the Decky allowlist. It is a description of a host,
never installation authority and never evidence about a session; the state,
launch and cost probes beside it answer those.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

from ce_decky.network_tls import build_verified_ssl_context, candidate_ca_bundles  # noqa: E402


DECKY_ENV = (
    "DECKY_VERSION",
    "DECKY_USER",
    "DECKY_USER_HOME",
    "DECKY_HOME",
    "DECKY_PLUGIN_SETTINGS_DIR",
    "DECKY_PLUGIN_RUNTIME_DIR",
    "DECKY_PLUGIN_LOG_DIR",
    "DECKY_PLUGIN_DIR",
    "DECKY_PLUGIN_NAME",
    "DECKY_PLUGIN_VERSION",
)
COMMANDS = ("node", "npm", "pnpm", "7z", "7za", "7zr", "steam")


def _read_os_release() -> dict[str, str]:
    result: dict[str, str] = {}
    path = Path("/etc/os-release")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            result[key] = value.strip().strip('"')
    except OSError:
        pass
    return result


def _command_version(name: str) -> dict[str, object]:
    path = shutil.which(name)
    result: dict[str, object] = {"path": str(Path(path).resolve()) if path else None, "version": None}
    if not path:
        return result
    probes = ([path, "--version"], [path, "-version"], [path, "i"])
    for command in probes:
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=4,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = "\n".join(completed.stdout.strip().splitlines()[:4])
        if text:
            result["version"] = text
            break
    return result


def _dir_state(path: Path) -> dict[str, object]:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path.expanduser().absolute()
    return {
        "path": str(resolved),
        "exists": resolved.exists(),
        "is_dir": resolved.is_dir(),
    }


def _compatibility_tools(home: Path) -> list[dict[str, object]]:
    roots = [
        home / ".local/share/Steam/steamapps/common",
        home / ".steam/steam/steamapps/common",
        home / ".local/share/Steam/compatibilitytools.d",
        home / ".steam/steam/compatibilitytools.d",
    ]
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(root.iterdir(), key=lambda p: p.name.casefold())
        except OSError:
            continue
        for child in children:
            name = child.name
            if not (
                name.casefold().startswith("proton")
                or name.casefold().startswith("steamlinuxruntime")
                or root.name == "compatibilitytools.d"
            ):
                continue
            try:
                resolved = child.resolve()
            except OSError:
                resolved = child.absolute()
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            version_candidates = [resolved / "version", resolved / "proton", resolved / "toolmanifest.vdf"]
            version_hint = None
            for candidate in version_candidates:
                if not candidate.is_file():
                    continue
                try:
                    if candidate.name == "version":
                        version_hint = candidate.read_text(encoding="utf-8", errors="replace")[:512].strip()
                    else:
                        stat = candidate.stat()
                        version_hint = f"{candidate.name}: size={stat.st_size}, mtime_ns={stat.st_mtime_ns}"
                    break
                except OSError:
                    pass
            items.append({"name": name, "path": key, "version_hint": version_hint})
    return items


def main() -> None:
    # A helper whose `--help` runs the capture instead of describing it cannot
    # be surveyed, and surveying `scripts/` is how an agent finds the tool that
    # already answers its question.
    argparse.ArgumentParser(description=__doc__.splitlines()[0], epilog="\n".join(__doc__.splitlines()[2:]),
                            formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    home = Path.home()
    context, explicit_ca = build_verified_ssl_context()
    payload = {
        "schema": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version,
            "python_executable": sys.executable,
            "uname": list(platform.uname()),
            "os_release": _read_os_release(),
        },
        "home": str(home.resolve()),
        "commands": {name: _command_version(name) for name in COMMANDS},
        "decky_environment": {name: os.environ.get(name) for name in DECKY_ENV},
        "paths": {
            "steam_local_share": _dir_state(home / ".local/share/Steam"),
            "steam_dot_steam": _dir_state(home / ".steam/steam"),
            "decky_homebrew": _dir_state(home / "homebrew"),
            "managed_root": _dir_state(home / ".cheat-engine-decky"),
        },
        "tls": {
            "x509_ca_roots": int(context.cert_store_stats().get("x509_ca", 0)),
            "explicit_ca_file": explicit_ca,
            "candidate_ca_bundles": [str(path) for path in candidate_ca_bundles()],
            "verification_enabled": context.verify_mode != 0 and bool(context.check_hostname),
        },
        "compatibility_tools": _compatibility_tools(home),
        "notes": [
            "This snapshot is non-mutating and intentionally excludes secrets/tokens.",
            "Decky environment values are normally populated only inside the plugin process; null shell values are expected.",
            "Exact Proton identity still requires the target validation record and, where needed, file/version hashes.",
        ],
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
