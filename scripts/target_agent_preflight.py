#!/usr/bin/env python3
"""Check what this machine is, and whether it can perform the non-UI target-agent work.

The probe is intentionally bounded: it names the host, verifies the
Python/Git/workspace capabilities every host needs and the Linux/procfs ones
only the target has, and never reads credentials, edits Steam or Decky state, or
reaches the network unless ``--network`` is explicitly selected.

It answers three questions rather than one, because they have different answers
on a Windows or Linux desktop. ``repository_ok`` says whether ordinary
repository work runs here. ``target_host_ok`` says whether a helper that reads
the live process table can run here at all, which an ordinary Linux desktop
satisfies. ``target_ok`` says whether this machine is the device, which it does
not: the plugin is a Decky Loader plugin for SteamOS, and a desktop developing
against a Steam Deck elsewhere is the ordinary arrangement, so equating Linux
with the target would let target claims be made from a machine that cannot make
them. ``host`` says which machine this is, down to the Valve model, so an agent
does not have to infer it from a path or a user name. The exit code separates
them: 0 when everything holds, 3 when the only thing wrong is the machine, and 2
for an ordinary failure.

Whether a live Decky holding this plugin is reachable is a different question,
and ``scripts/target_plugin_install.py authority`` is what answers it.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__:
    from . import host_platform
else:
    import host_platform


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_COMMANDS = ("git", "python3", "codex")
# Checks that answer for the machine rather than for the checkout. A host that
# fails one of these is the wrong machine for target work, which is a different
# outcome from a checkout that is not ready.
TARGET_CHECK_NAMES = ("linux_host", "proc_available", "steamos_device")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _workspace_roundtrip(root: Path) -> Check:
    build = root / "build"
    if build.is_symlink():
        return Check("workspace_write", False, "build directory must not be a symlink")
    try:
        build.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".target-agent-preflight-", dir=build) as temporary:
            path = Path(temporary) / "write-check.txt"
            path.write_text("ok\n", encoding="ascii")
            if path.read_text(encoding="ascii") != "ok\n":
                return Check("workspace_write", False, "workspace write/read round trip changed bytes")
        return Check("workspace_write", True, "create/write/read/remove in build/ succeeded")
    except OSError as exc:
        return Check("workspace_write", False, f"{type(exc).__name__}: {exc}")


def _git_checkout(root: Path) -> Check:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except OSError as exc:
        return Check("git_checkout", False, f"{type(exc).__name__}: {exc}")
    ok = completed.returncode == 0 and completed.stdout.strip() == "true"
    detail = "Git worktree is readable" if ok else (completed.stderr or completed.stdout).strip()[:512]
    return Check("git_checkout", ok, detail or f"exit={completed.returncode}")


def _network_remote(root: Path) -> Check:
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--exit-code", "origin", "HEAD"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except OSError as exc:
        return Check("network_origin", False, f"{type(exc).__name__}: {exc}")
    ok = completed.returncode == 0
    return Check("network_origin", ok, "origin HEAD is reachable" if ok else ((completed.stderr or completed.stdout).strip()[:512] or f"exit={completed.returncode}"))


def probe(*, network: bool = False, root: Path = ROOT) -> dict[str, object]:
    host = host_platform.describe()
    commands = [
        Check(f"command_{name}", shutil.which(name) is not None, shutil.which(name) or "not found")
        for name in REQUIRED_COMMANDS
    ]
    checks = [
        Check("linux_host", host.family == "linux", host.describe()),
        Check("python_3_11", sys.version_info >= (3, 11), sys.version.split()[0]),
        *commands,
        _git_checkout(root),
        _workspace_roundtrip(root),
        Check(
            "proc_available",
            host.procfs,
            "readable" if host.procfs else f"{host_platform.PROC_ROOT} is not readable on this host",
        ),
        # Linux with a process table is what a helper needs in order to run.
        # Being the device is a second question, and a desktop answers no.
        Check(
            "steamos_device",
            host.target_device,
            host.describe() if host.target_device else f"not the target device: {host.describe()}",
        ),
    ]
    if network:
        checks.append(_network_remote(root))
    lua = next((shutil.which(name) for name in ("lua", "lua5.4", "lua54", "lua5.3", "lua53", "luajit") if shutil.which(name)), None)
    target = [check for check in checks if check.name in TARGET_CHECK_NAMES]
    repository = [check for check in checks if check.name not in TARGET_CHECK_NAMES]
    target_ok = all(check.ok for check in target)
    target_host_ok = all(check.ok for check in target if check.name != "steamos_device")
    notes = [
        "This does not inspect or modify Steam, Decky, CE, VDF, or user-managed state.",
        "Codex approval and sandbox policy are enforced outside a child process; a failed check is the authority for this session.",
        "Run again with --network only after the operator permits Git/provider network access.",
        "Optional checks never fail the preflight; a missing optional tool means a skipped local stage, never an OS change.",
        "Whether a live Decky holding this plugin is reachable is the other half of the question:"
        " scripts/target_plugin_install.py authority answers it.",
    ]
    if not target_host_ok:
        notes.insert(0, (
            f"This host is {host.describe()}, so nothing that reads the live process table, Decky, Steam,"
            " Proton or Cheat Engine may run here. Repository work runs here; target work belongs on the device."
        ))
    elif not target_ok:
        notes.insert(0, (
            f"This host is {host.describe()}, which is not the target device. Helpers that only need Linux and"
            " a process table run here, and nothing observed here is evidence about Steam, Proton or Cheat"
            " Engine on SteamOS. docs/REMOTE_TARGET.md is the arrangement for a device that is somewhere else."
        ))
    return {
        "schema": 2,
        "root": str(root),
        "host": host.as_dict(),
        "network_requested": network,
        "repository_ok": all(check.ok for check in repository),
        "target_host_ok": target_host_ok,
        "target_ok": target_ok,
        "checks": [asdict(check) for check in checks],
        "optional": [
            asdict(Check(
                "lua_interpreter",
                lua is not None,
                lua or "absent: the resident-bridge conformance test skips here and runs in CI instead; do not modify SteamOS to install it",
            )),
        ],
        "ok": all(check.ok for check in checks),
        "notes": notes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="read-only probe of origin HEAD; may require Codex network approval")
    args = parser.parse_args(argv)
    report = probe(network=args.network)
    import json
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["ok"]:
        return 0
    # A host that is simply the wrong machine exits distinctly, so a caller does
    # not have to read the checks to tell it from a checkout that is not ready.
    return 2 if report["repository_ok"] is False else host_platform.EXIT_UNSUPPORTED_HOST


if __name__ == "__main__":
    raise SystemExit(main())
