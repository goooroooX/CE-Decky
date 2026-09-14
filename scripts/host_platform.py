#!/usr/bin/env python3
"""What machine a helper is running on, and whether it may do target work.

The same checkout is worked on from a Windows or Linux desktop, from CI, and
from the device itself, and nothing in the tree says which. A helper that reads
`/proc`, the Gamescope statistics pipe or Valve's own DMI fields cannot answer
for a host that has none of them, and the expensive failure is not the refusal:
it is a helper that begins running, crashes on the first POSIX-only call, and
leaves an agent reading a traceback about `os.sysconf` when the real answer is
that this work belongs on another machine.

So every helper that needs the target asks here first, and gets one of two
outcomes: the host facts, or an `UnsupportedHost` naming what was needed, what
was found and where the work belongs instead. `main` prints the facts as JSON,
which is the one-call answer to "is this the Steam Machine".

Two things about SteamOS that are not guessable and are why hardware is read
from DMI rather than from the operating system:

* `/etc/os-release` reports `VARIANT_ID=steamdeck` on a Steam Machine as well as
  on a Steam Deck, so it identifies SteamOS and never the device;
* the DMI product name does identify it: `Fremont` is the Steam Machine,
  `Jupiter` the Steam Deck LCD and `Galileo` the Steam Deck OLED, all under the
  `Valve` vendor.

Read-only, and it touches nothing a session owns.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, asdict
import json
from pathlib import Path
import platform
import sys

SCHEMA = 1

#: Exit code for a host that cannot run the helper at all. It is deliberately
#: distinct from the ordinary failure code, so a caller can tell "this machine
#: is the wrong one" from "this machine is right and the check failed".
EXIT_UNSUPPORTED_HOST = 3

OS_RELEASE = Path("/etc/os-release")
DMI_ROOT = Path("/sys/class/dmi/id")
PROC_ROOT = Path("/proc")

#: Valve's own DMI product names. Anything else Valve-made reads as
#: ``valve_other`` rather than being forced into one of these.
VALVE_PRODUCTS = {
    "fremont": "steam_machine",
    "jupiter": "steam_deck_lcd",
    "galileo": "steam_deck_oled",
}


class UnsupportedHost(RuntimeError):
    """This helper cannot run on this machine, and says so instead of failing later."""


@dataclass(frozen=True)
class Host:
    """What one machine is, as far as a helper needs to care."""

    system: str
    family: str
    machine: str
    python_version: str
    os_id: str | None
    os_variant_id: str | None
    os_version_id: str | None
    os_build_id: str | None
    steamos: bool
    vendor: str | None
    product: str | None
    device: str | None
    procfs: bool

    @property
    def steam_machine(self) -> bool:
        return self.device == "steam_machine"

    @property
    def steam_deck(self) -> bool:
        return self.device in {"steam_deck_lcd", "steam_deck_oled"}

    @property
    def valve_hardware(self) -> bool:
        return self.device is not None

    @property
    def target_device(self) -> bool:
        """Whether this machine is the device, rather than one that could host a helper.

        Linux with a readable process table is what the helpers need in order to
        run at all, and an ordinary Linux desktop has both. It is not the target:
        the plugin is a Decky Loader plugin for SteamOS, and the ordinary
        arrangement is a desktop developing against a device that is somewhere
        else. Equating the two would tell an agent on a desktop that every claim
        about Steam, Proton and Cheat Engine could be made here.
        """
        return self.steamos and self.procfs

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            **asdict(self),
            "steam_machine": self.steam_machine,
            "steam_deck": self.steam_deck,
            "valve_hardware": self.valve_hardware,
            "target_device": self.target_device,
        }

    def describe(self) -> str:
        """One line an agent can read without parsing anything."""
        if self.family != "linux":
            # Nothing below is readable off Linux, and inventing a placeholder
            # for each field would only make the line look like it knows more.
            return f"{self.system} on {self.machine}"
        if self.device is not None:
            hardware = self.device.replace("_", " ")
        elif self.vendor or self.product:
            hardware = f"{self.vendor or 'unknown vendor'} {self.product or 'unknown model'}"
        else:
            hardware = "unidentified hardware"
        return f"{self.system} ({self.os_id or self.family}) on {hardware}"


def _os_release(root: Path = OS_RELEASE) -> dict[str, str]:
    try:
        raw = root.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    fields: dict[str, str] = {}
    for line in raw.splitlines()[:128]:
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def _procfs_readable(proc_root: Path) -> bool:
    """Whether the live process table can actually be read, not merely mounted.

    A directory that exists proves nothing about what is inside it: a container
    can mount an empty one, and a helper that took `is_dir()` for an answer
    would go on to report every process as absent. `self/stat` is the cheapest
    thing that is there on a real procfs and readable by whoever is asking.
    """
    try:
        return bool((proc_root / "self" / "stat").read_bytes())
    except OSError:
        return False


def _dmi_field(name: str, root: Path = DMI_ROOT) -> str | None:
    try:
        value = (root / name).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


def describe(
    *, os_release: Path = OS_RELEASE, dmi_root: Path = DMI_ROOT, proc_root: Path = PROC_ROOT,
) -> Host:
    """Read the host's identity. Every field is optional and none is inferred."""
    system = platform.system()
    family = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(system, "other")
    fields = _os_release(os_release) if family == "linux" else {}
    vendor = _dmi_field("sys_vendor", dmi_root) if family == "linux" else None
    product = _dmi_field("product_name", dmi_root) if family == "linux" else None
    device = None
    if (vendor or "").strip().casefold() == "valve":
        device = VALVE_PRODUCTS.get((product or "").strip().casefold(), "valve_other")
    return Host(
        system=system,
        family=family,
        machine=platform.machine(),
        python_version=platform.python_version(),
        os_id=fields.get("ID"),
        os_variant_id=fields.get("VARIANT_ID"),
        os_version_id=fields.get("VERSION_ID"),
        os_build_id=fields.get("BUILD_ID"),
        steamos=fields.get("ID") == "steamos",
        vendor=vendor,
        product=product,
        device=device,
        procfs=_procfs_readable(proc_root),
    )


def require(
    tool: str,
    *,
    needs_procfs: bool = True,
    needs_steamos: bool = False,
    needs_valve_hardware: bool = False,
    host: Host | None = None,
) -> Host:
    """Return the host, or refuse this machine with a message that names the reason.

    `needs_procfs` is what most target helpers actually depend on, because the
    live process table is where a running game, an owned Cheat Engine and this
    plugin's own backend are observed. `needs_steamos` and `needs_valve_hardware`
    are for the narrower cases: Decky's own layout, and the sensors and
    compositor pipe a Valve device exposes.
    """
    found = describe() if host is None else host
    if found.family != "linux":
        raise UnsupportedHost(
            f"{tool} runs on the SteamOS/Linux target only, and this host is {found.describe()}."
            " Repository work (scripts/qa.py, packaging, the documents) runs here;"
            " run this helper on the device, or through the runner's WSL route."
        )
    if needs_procfs and not found.procfs:
        raise UnsupportedHost(
            f"{tool} reads the live process table and {PROC_ROOT} is not readable on this host"
            f" ({found.describe()}). Run it on the device itself, not in a container without procfs."
        )
    if needs_steamos and not found.steamos:
        raise UnsupportedHost(
            f"{tool} needs SteamOS, and this host reports {found.os_id or 'an unidentified distribution'}"
            f" ({found.describe()}). Run it on the device."
        )
    if needs_valve_hardware and not found.valve_hardware:
        raise UnsupportedHost(
            f"{tool} reads hardware only a Valve device exposes, and this host is {found.describe()}."
            " Run it on the Steam Machine or a Steam Deck."
        )
    return found


def refuse(exc: UnsupportedHost, *, stream=None) -> int:
    """Print one unsupported-host refusal and return the exit code for it."""
    print(f"unsupported host: {exc}", file=sys.stderr if stream is None else stream)
    return EXIT_UNSUPPORTED_HOST


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--line", action="store_true", help="print one plain line instead of JSON")
    args = parser.parse_args(argv)
    host = describe()
    if args.line:
        print(host.describe())
    else:
        json.dump(host.as_dict(), sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
