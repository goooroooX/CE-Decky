"""What machine a helper is running on, and how it refuses the wrong one.

The expensive failure is not a refusal. It is a target helper that starts on a
Windows desktop, reaches a POSIX-only call, and leaves an agent reading a
traceback about `os.sysconf` when the real answer is that the work belongs on
another machine. Every case here is one part of that answer arriving early and
in words.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import host_platform


def _linux_host(tmp_path: Path, *, vendor: str = "Valve", product: str = "Fremont") -> Path:
    dmi = tmp_path / "dmi"
    dmi.mkdir()
    (dmi / "sys_vendor").write_text(f"{vendor}\n", encoding="utf-8")
    (dmi / "product_name").write_text(f"{product}\n", encoding="utf-8")
    (tmp_path / "os-release").write_text(
        'NAME="SteamOS"\nID=steamos\nVARIANT_ID=steamdeck\nVERSION_ID=3.8.16\nBUILD_ID=20260716.1\n',
        encoding="utf-8",
    )
    proc = tmp_path / "proc" / "self"
    proc.mkdir(parents=True)
    (proc / "stat").write_text("1 (init) S 0 1 1\n", encoding="utf-8")
    return dmi


@pytest.mark.parametrize(
    ("product", "device", "steam_machine", "steam_deck"),
    [
        ("Fremont", "steam_machine", True, False),
        ("Jupiter", "steam_deck_lcd", False, True),
        ("Galileo", "steam_deck_oled", False, True),
        ("Something Else", "valve_other", False, False),
    ],
)
def test_the_valve_model_comes_from_dmi_because_steamos_calls_them_all_steamdeck(
    tmp_path: Path, monkeypatch, product: str, device: str, steam_machine: bool, steam_deck: bool,
):
    monkeypatch.setattr(host_platform.platform, "system", lambda: "Linux")
    dmi = _linux_host(tmp_path, product=product)

    host = host_platform.describe(
        os_release=tmp_path / "os-release", dmi_root=dmi, proc_root=tmp_path / "proc",
    )

    # The operating system says "steamdeck" on every one of them, so it can
    # identify SteamOS and never the device.
    assert host.os_variant_id == "steamdeck"
    assert host.device == device
    assert host.steam_machine is steam_machine
    assert host.steam_deck is steam_deck
    assert host.valve_hardware is True
    assert host.steamos is True


def test_hardware_that_is_not_valves_is_left_unnamed_rather_than_guessed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(host_platform.platform, "system", lambda: "Linux")
    dmi = _linux_host(tmp_path, vendor="Some Vendor", product="Jupiter")

    host = host_platform.describe(
        os_release=tmp_path / "os-release", dmi_root=dmi, proc_root=tmp_path / "proc",
    )

    assert host.device is None
    assert host.valve_hardware is False
    assert "Some Vendor Jupiter" in host.describe()


def test_a_windows_host_is_described_without_reading_anything_posix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(host_platform.platform, "system", lambda: "Windows")

    host = host_platform.describe(
        os_release=tmp_path / "absent", dmi_root=tmp_path / "absent", proc_root=tmp_path / "absent",
    )

    assert host.family == "windows"
    assert (host.os_id, host.vendor, host.product, host.device) == (None, None, None, None)
    assert host.procfs is False


def test_a_target_helper_refuses_windows_by_name_and_says_where_the_work_belongs(monkeypatch):
    host = host_platform.Host(
        system="Windows", family="windows", machine="AMD64", python_version="3.13.5",
        os_id=None, os_variant_id=None, os_version_id=None, os_build_id=None,
        steamos=False, vendor=None, product=None, device=None, procfs=False,
    )

    with pytest.raises(host_platform.UnsupportedHost) as refusal:
        host_platform.require("The session cost probe", host=host)

    message = str(refusal.value)
    assert "The session cost probe" in message
    assert "Windows" in message
    assert "scripts/qa.py" in message


def test_a_linux_host_without_procfs_is_refused_for_the_reason_it_actually_fails():
    host = host_platform.Host(
        system="Linux", family="linux", machine="x86_64", python_version="3.13.5",
        os_id="arch", os_variant_id=None, os_version_id=None, os_build_id=None,
        steamos=False, vendor=None, product=None, device=None, procfs=False,
    )

    with pytest.raises(host_platform.UnsupportedHost, match="process table"):
        host_platform.require("The state probe", host=host)
    # The same host passes when the helper does not read the process table.
    assert host_platform.require("The prefix probe", needs_procfs=False, host=host) is host


def test_steamos_and_valve_hardware_are_separate_requirements():
    desktop = host_platform.Host(
        system="Linux", family="linux", machine="x86_64", python_version="3.13.5",
        os_id="arch", os_variant_id=None, os_version_id=None, os_build_id=None,
        steamos=False, vendor="ASUS", product="Desktop", device=None, procfs=True,
    )

    with pytest.raises(host_platform.UnsupportedHost, match="SteamOS"):
        host_platform.require("The screenshot helper", needs_steamos=True, host=desktop)
    with pytest.raises(host_platform.UnsupportedHost, match="Valve device"):
        host_platform.require("The sensor probe", needs_valve_hardware=True, host=desktop)


def test_the_refusal_exit_code_is_distinct_from_an_ordinary_failure(capsys):
    code = host_platform.refuse(host_platform.UnsupportedHost("this host is a toaster"))

    assert code == host_platform.EXIT_UNSUPPORTED_HOST
    assert code not in (0, 1, 2)
    assert "unsupported host: this host is a toaster" in capsys.readouterr().err


def test_a_process_table_that_cannot_be_read_is_not_one(tmp_path: Path, monkeypatch):
    """A mounted directory proves nothing about what is inside it.

    A container can present an empty `/proc`, and a helper that took `is_dir()`
    for an answer would run and report every process as absent.
    """
    monkeypatch.setattr(host_platform.platform, "system", lambda: "Linux")
    empty = tmp_path / "proc"
    empty.mkdir()

    host = host_platform.describe(
        os_release=tmp_path / "absent", dmi_root=tmp_path / "absent", proc_root=empty,
    )

    assert host.procfs is False
    with pytest.raises(host_platform.UnsupportedHost, match="not readable"):
        host_platform.require("The state probe", host=host)


def test_an_ordinary_linux_desktop_can_host_a_helper_and_is_still_not_the_device(tmp_path: Path, monkeypatch):
    """The one thing a desktop must never be mistaken for.

    The plugin is a Decky Loader plugin for SteamOS, and the ordinary
    arrangement is a desktop developing against a device that is elsewhere.
    Equating Linux with the target would let claims about Steam, Proton and
    Cheat Engine be made from a machine that cannot make them.
    """
    monkeypatch.setattr(host_platform.platform, "system", lambda: "Linux")
    dmi = tmp_path / "dmi"
    dmi.mkdir()
    (dmi / "sys_vendor").write_text("ASUS\n", encoding="utf-8")
    (dmi / "product_name").write_text("Desktop\n", encoding="utf-8")
    (tmp_path / "os-release").write_text('ID=arch\nNAME="Arch Linux"\n', encoding="utf-8")
    proc = tmp_path / "proc" / "self"
    proc.mkdir(parents=True)
    (proc / "stat").write_text("1 (init) S 0 1 1\n", encoding="utf-8")

    host = host_platform.describe(
        os_release=tmp_path / "os-release", dmi_root=dmi, proc_root=tmp_path / "proc",
    )

    assert host.procfs is True
    assert host.steamos is False
    assert host.target_device is False
    assert host.as_dict()["target_device"] is False
    # A Linux-generic helper still runs: it is the device claim that is refused.
    assert host_platform.require("The session cost probe", host=host) is host
    with pytest.raises(host_platform.UnsupportedHost, match="SteamOS"):
        host_platform.require("A device-only helper", needs_steamos=True, host=host)
