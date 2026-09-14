"""What Steam's own application cache says one game starts, and what it refuses.

The fixtures here encode Valve's binary VDF rather than asserting against a real
`appinfo.vdf`: the file on a device is whatever that device's Steam has cached,
and a regression that depends on one cannot be run anywhere else. The shapes
encoded are the ones a real cache was read to establish, including Half-Life 2's
one entry per branch and Counter-Strike 2's workshop tools behind a DLC it does
not own.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from ce_decky.steam_launch import (
    MAX_APP_RECORD_BYTES,
    LaunchEntry,
    windows_launch_entries,
)

_MAP = b"\x00"
_STRING = b"\x01"
_INT32 = b"\x02"
_WIDESTRING = b"\x05"
_UINT64 = b"\x07"
_MAP_END = b"\x08"


class Wide(str):
    """A value Steam writes as UTF-16, which this reader only has to skip past."""


def _encode_map(node: dict, keys: list[str] | None) -> bytes:
    """One binary VDF map, keyed inline or by index into a string table."""
    out = bytearray()
    for key, value in node.items():
        if keys is None:
            encoded_key = key.encode("utf-8") + b"\0"
        else:
            if key not in keys:
                keys.append(key)
            encoded_key = struct.pack("<I", keys.index(key))
        if isinstance(value, dict):
            out += _MAP + encoded_key + _encode_map(value, keys)
        elif isinstance(value, Wide):
            out += _WIDESTRING + encoded_key + str(value).encode("utf-16-le") + b"\0\0"
        elif isinstance(value, int) and not isinstance(value, bool):
            out += _INT32 + encoded_key + struct.pack("<i", value)
        else:
            out += _STRING + encoded_key + str(value).encode("utf-8") + b"\0"
    return bytes(out) + _MAP_END


def write_appinfo(root: Path, apps: dict[int, dict], *, magic: int = 0x07564429) -> Path:
    """A cache holding these apps, in the container version named."""
    keys: list[str] | None = [] if magic == 0x07564429 else None
    bodies: dict[int, bytes] = {}
    for app_id, node in apps.items():
        body = _MAP
        body += (b"appinfo\0" if keys is None else struct.pack("<I", _index(keys, "appinfo")))
        body += _encode_map(node, keys)
        metadata = struct.pack("<IIQ", 1, 0, 0) + b"\0" * 20 + struct.pack("<I", 0)
        if magic >= 0x07564428:
            metadata += b"\0" * 20
        bodies[app_id] = metadata + body

    out = bytearray(struct.pack("<II", magic, 1))
    if keys is not None:
        out += struct.pack("<q", 0)  # replaced once the table's offset is known
    for app_id, body in bodies.items():
        out += struct.pack("<II", app_id, len(body)) + body
    out += struct.pack("<I", 0)
    if keys is not None:
        offset = len(out)
        table = struct.pack("<I", len(keys)) + b"".join(key.encode("utf-8") + b"\0" for key in keys)
        struct.pack_into("<q", out, 8, offset)
        out += table
    path = root / "appcache" / "appinfo.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def _index(keys: list[str], key: str) -> int:
    if key not in keys:
        keys.append(key)
    return keys.index(key)


def _half_life_2() -> dict:
    """The real shape: one Windows entry per branch, plus the native ones."""
    return {
        "common": {"name": "Half-Life 2", "type": "Game"},
        "config": {
            "installdir": "Half-Life 2",
            "launch": {
                "0": {"executable": "hl2.exe", "arguments": "-steam", "workingdir": "bin",
                      "config": {"oslist": "windows", "BetaKey": "default"}},
                "1": {"executable": "hl2.sh", "config": {"oslist": "linux"}},
                "3": {"executable": "hl2.exe", "config": {"oslist": "windows", "BetaKey": "steam_legacy"}},
            },
        },
    }


def test_the_program_steam_starts_is_the_one_answer(tmp_path: Path):
    """The case this replaced a folder walk for.

    Half-Life 2's folder holds twenty-eight Windows executables and its record
    here holds one line for the branch this device is on.
    """
    write_appinfo(tmp_path, {220: _half_life_2()})
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert reason is None
    assert [(item.name, item.directory, item.branch) for item in found] == [("hl2.exe", "", "default")]


def test_a_wide_value_before_the_launch_lines_does_not_move_them(tmp_path: Path):
    # A UTF-16 value is two bytes per character and ends in two zero bytes, so
    # any Latin letter carries a zero byte of its own: `E` is `45 00`, and with
    # the terminator behind it the file holds `45 00 00 00`. Searching for the
    # first zero pair finds one a byte early, which leaves half a terminator in
    # the stream and reads every field after it from the wrong place - here,
    # the whole `config/launch` block this game is about.
    record = _half_life_2()
    record["common"]["gamepad_ui_name"] = Wide("E")
    write_appinfo(tmp_path, {220: record})
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert reason is None
    assert [(item.name, item.branch) for item in found] == [("hl2.exe", "default")]


def test_a_wide_value_that_never_ends_is_still_refused(tmp_path: Path):
    record = _half_life_2()
    record["common"]["gamepad_ui_name"] = Wide("E")
    path = write_appinfo(tmp_path, {220: record})
    data = path.read_bytes()
    # The terminator, and nothing after it that could stand in for one.
    path.write_bytes(data[:data.index(b"E\x00\x00\x00") + 2].rstrip(b"\0"))
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert found == []
    assert reason is not None


def test_a_branch_chooses_the_entry_written_for_it(tmp_path: Path):
    """The files on this disk are one branch's, and so is the entry about them."""
    write_appinfo(tmp_path, {220: _half_life_2()})
    legacy, reason = windows_launch_entries(tmp_path, 220, "steam_legacy")
    assert reason is None
    assert [item.index for item in legacy] == [3]
    # A branch nothing is written for keeps the entries that name no branch and
    # drops the rest, rather than falling back to another branch's.
    other, reason = windows_launch_entries(tmp_path, 220, "some_playtest")
    assert other == []
    assert reason == "Steam's own record names no Windows program for this game"


def test_what_is_not_the_game_stays_out(tmp_path: Path):
    """Every exclusion the real cache made necessary, in one record."""
    write_appinfo(tmp_path, {730: {
        "common": {"name": "Counter-Strike 2", "type": "Game"},
        "config": {"launch": {
            "0": {"executable": "game\\bin\\win64\\cs2.exe", "config": {"oslist": "windows"}},
            "1": {"executable": "cs2.sh", "config": {"oslist": "linux"}},
            # Steam writes the AppID a launch option is conditioned on as a
            # number as often as as a string, and reading only the string
            # spelling let the workshop tools through as though they were the
            # game.
            "2": {"executable": "game\\bin\\win64\\csgocfg.exe", "config": {"oslist": "windows", "ownsdlc": 2279721}},
            "3": {"executable": "configure.exe", "type": "config", "config": {"oslist": "windows"}},
            "4": {"executable": "srcds.exe", "type": "server", "config": {"oslist": "windows"}},
            "5": {"executable": "hammer.exe", "type": "editor", "config": {"oslist": "windows"}},
            # A path that climbs out of the install directory is refused rather
            # than resolved into whatever it lands on.
            "6": {"executable": "..\\..\\elsewhere.exe", "config": {"oslist": "windows"}},
            "7": {"executable": "readme.txt", "config": {"oslist": "windows"}},
        }},
    }})
    found, reason = windows_launch_entries(tmp_path, 730, None)
    assert reason is None
    assert [(item.name, item.directory) for item in found] == [("cs2.exe", "game/bin/win64")]


def test_a_windows_only_game_needs_no_platform_written_on_it(tmp_path: Path):
    """Anachronox and DuckTales Remastered are both written this way."""
    write_appinfo(tmp_path, {242940: {
        "common": {"name": "Anachronox", "type": "Game"},
        "config": {"launch": {"0": {"executable": "anox.exe", "description": "Launch"}}},
    }})
    found, reason = windows_launch_entries(tmp_path, 242940, None)
    assert reason is None
    assert found == [LaunchEntry(index=0, path="anox.exe", name="anox.exe", directory="", description="Launch", branch=None)]


@pytest.mark.parametrize("magic", [0x07564427, 0x07564428, 0x07564429])
def test_every_container_version_steam_has_written_is_read(tmp_path: Path, magic: int):
    """The key table arrived in `0x07564429`; the two before it spell keys inline."""
    write_appinfo(tmp_path, {220: _half_life_2()}, magic=magic)
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert reason is None
    assert [item.name for item in found] == ["hl2.exe"]


def test_an_app_the_cache_does_not_hold_is_a_stated_reason(tmp_path: Path):
    write_appinfo(tmp_path, {220: _half_life_2()})
    found, reason = windows_launch_entries(tmp_path, 999999, None)
    assert found == []
    assert reason == "Steam's own record holds nothing about this game"


def test_a_game_with_no_windows_entry_says_so(tmp_path: Path):
    write_appinfo(tmp_path, {1070560: {
        "common": {"name": "Steam Linux Runtime", "type": "Tool"},
        "config": {"launch": {"0": {"executable": "run", "config": {"oslist": "linux"}}}},
    }})
    found, reason = windows_launch_entries(tmp_path, 1070560, None)
    assert found == []
    assert reason == "Steam's own record names no Windows program for this game"


def test_no_cache_at_all_is_a_stated_reason(tmp_path: Path):
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert found == []
    assert reason == "Steam's own application cache is not on this device"


def test_a_cache_that_is_not_one_is_refused_rather_than_read(tmp_path: Path):
    cache = tmp_path / "appcache" / "appinfo.vdf"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"not a cache, but long enough to be one" * 4)
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert found == []
    assert reason == "Steam's own application cache is in a format this does not read"


def test_a_record_longer_than_the_bound_is_refused(tmp_path: Path):
    """A length this cannot trust is where a parser starts inventing answers."""
    write_appinfo(tmp_path, {220: _half_life_2()})
    cache = tmp_path / "appcache" / "appinfo.vdf"
    data = bytearray(cache.read_bytes())
    struct.pack_into("<I", data, 16 + 4, MAX_APP_RECORD_BYTES + 1)
    cache.write_bytes(bytes(data))
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert found == []
    assert reason == "Steam's own application cache holds a record this cannot read"


def test_a_record_that_ends_early_is_refused_rather_than_guessed(tmp_path: Path):
    write_appinfo(tmp_path, {220: _half_life_2()})
    cache = tmp_path / "appcache" / "appinfo.vdf"
    data = cache.read_bytes()
    cache.write_bytes(data[: len(data) // 2])
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert found == []
    assert reason is not None


def test_an_app_id_that_is_not_one_is_a_programming_error(tmp_path: Path):
    for value in (0, -1, True, "220", 1.0):
        with pytest.raises(ValueError):
            windows_launch_entries(tmp_path, value, None)  # type: ignore[arg-type]


def test_a_symlinked_cache_is_not_followed(tmp_path: Path):
    """A game folder and a Steam root are user writable; a link out of one is not read."""
    real = tmp_path / "elsewhere"
    write_appinfo(real, {220: _half_life_2()})
    (tmp_path / "appcache").mkdir()
    (tmp_path / "appcache" / "appinfo.vdf").symlink_to(real / "appcache" / "appinfo.vdf")
    found, reason = windows_launch_entries(tmp_path, 220, None)
    assert found == []
    assert reason == "Steam's own application cache is not a regular file"
