"""What a device holds of an account's library, and what it refuses to claim.

The case this exists for was measured on a Steam Deck standing beside a Steam
Machine: Steam offered 39 non-Steam shortcuts while the Deck's own store held 6,
and 11 of its 22 installed apps were Proton builds and Steam Linux Runtimes.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from ce_decky import steam_library
from ce_decky.steam_library import local_library
from tests.test_steam_launch import write_appinfo


def _library(home: Path) -> Path:
    library = home / ".local/share/Steam"
    (library / "steamapps/common").mkdir(parents=True, exist_ok=True)
    return library


def _manifest(library: Path, app_id: int, *, state: int = 4, name: str = "A Game") -> None:
    (library / f"steamapps/appmanifest_{app_id}.acf").write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{app_id}"\n'
        f'\t"name"\t\t"{name}"\n'
        f'\t"StateFlags"\t\t"{state}"\n'
        f'\t"installdir"\t\t"{name}"\n'
        "}\n",
        encoding="utf-8",
    )


def _shortcuts(
    library: Path,
    app_ids: list[int],
    # Not an account id: Steam names this folder after the signed-in user, and
    # a fixture has no business carrying a real one.
    user: str = "0",
    exe: str | None = None,
    shortcut_path: str | None = None,
) -> None:
    """One `shortcuts.vdf` holding these AppIDs, as Steam writes them.

    Signed 32 bit, and with the key case Steam actually uses: `appid` lower
    case beside `AppName` and `Exe` capitalised, which is what a reader matching
    on case misses.
    """
    body = bytearray(b"\x00shortcuts\x00")
    for index, app_id in enumerate(app_ids):
        signed = app_id - (1 << 32) if app_id >= 1 << 31 else app_id
        body += b"\x00" + str(index).encode() + b"\x00"
        body += b"\x02appid\x00" + struct.pack("<i", signed)
        body += b"\x01AppName\x00" + f"Game {index}".encode() + b"\x00"
        body += b"\x01Exe\x00" + (exe or '"/games/game.exe"').encode() + b"\x00"
        body += b"\x01ShortcutPath\x00" + (shortcut_path or "").encode() + b"\x00"
        body += b"\x08"
    body += b"\x08\x08"
    path = library / f"userdata/{user}/config/shortcuts.vdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(body))


def test_only_what_this_device_installed_is_reported(tmp_path: Path):
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 220, name="Half-Life 2")
    _manifest(library, 362890, name="Black Mesa")
    # Queued rather than installed: a manifest exists from the moment a download
    # starts, and there are no files behind this one.
    _manifest(library, 1151340, state=1026, name="Fallout 76")

    found = local_library(home)
    assert found["steam_app_ids"] == [220, 362890]
    assert found["reason"] is None


def test_an_app_with_nothing_to_start_is_named_and_a_mistyped_game_is_not(tmp_path: Path):
    """The discriminator is what Steam can start, not what Steam calls it.

    Measured on a Steam Deck: every Proton build and every Steam Linux Runtime
    carries no launch entry at all, while Half-Life 2's three episodes carry
    three each and Steam types all three of them `Tool`. Filtering on the type
    hid Episode One, which is a game people cheat in.
    """
    home = tmp_path / "home"
    library = _library(home)
    for app_id in (220, 380, 1628350, 3658110):
        _manifest(library, app_id)
    write_appinfo(library, {
        220: {"common": {"name": "Half-Life 2", "type": "Game"},
              "config": {"launch": {"0": {"executable": "hl2.exe", "config": {"oslist": "windows"}}}}},
        380: {"common": {"name": "Half-Life 2: Episode One", "type": "Tool"},
              "config": {"launch": {"0": {"executable": "hl2.exe", "config": {"oslist": "windows"}}}}},
        1628350: {"common": {"name": "Steam Linux Runtime 3.0", "type": "Tool"},
                  "config": {"installdir": "SteamLinuxRuntime_sniper"}},
        3658110: {"common": {"name": "Proton 10.0", "type": "Tool"},
                  "config": {"installdir": "Proton 10.0"}},
    })

    found = local_library(home)
    assert found["steam_app_ids"] == [220, 380, 1628350, 3658110]
    assert found["unstartable_app_ids"] == [1628350, 3658110]


def test_a_tool_with_one_entry_and_no_windows_program_is_not_a_game(tmp_path: Path):
    """Steamworks Common Redistributables, which is installed on most devices.

    It is typed `Tool` and carries one launch entry, so "declares nothing to
    start" left it in the list, under whatever name the user's Steam is in - it
    reached a Steam Deck's picker as a Russian sentence. Its entry declares no
    platform and names no `.exe`, and Half-Life 2's episodes, typed `Tool` on
    the same device, name both.
    """
    home = tmp_path / "home"
    library = _library(home)
    for app_id in (220, 380, 228980):
        _manifest(library, app_id)
    write_appinfo(library, {
        220: {"common": {"name": "Half-Life 2", "type": "Game"},
              "config": {"launch": {"0": {"executable": "hl2.exe", "config": {"oslist": "windows"}}}}},
        380: {"common": {"name": "Half-Life 2: Episode One", "type": "Tool"},
              "config": {"launch": {"0": {"executable": "hl2.exe", "config": {"oslist": "windows"}}}}},
        228980: {"common": {"name": "Steamworks Common Redistributables", "type": "Tool"},
                 "config": {"launch": {"0": {"executable": "steamworks.sh", "type": "default"}}}},
    })

    found = local_library(home)
    assert found["unstartable_app_ids"] == [228980]


def test_a_tool_whose_windows_program_lives_on_a_named_branch_is_still_a_game(tmp_path: Path):
    # Asking for no branch is not asking for every branch: it means the branch
    # nobody named, which keeps the public entries and drops the rest. A user
    # actually running a beta whose only Windows line is written for it lost
    # that game out of the picker.
    home = tmp_path / "home"
    library = _library(home)
    for app_id in (380, 228980):
        _manifest(library, app_id)
    write_appinfo(library, {
        380: {"common": {"name": "Half-Life 2: Episode One", "type": "Tool"},
              "config": {"launch": {"0": {"executable": "hl2.exe",
                                          "config": {"oslist": "windows", "BetaKey": "beta"}}}}},
        228980: {"common": {"name": "Steamworks Common Redistributables", "type": "Tool"},
                 "config": {"launch": {"0": {"executable": "steamworks.sh", "type": "default"}}}},
    })

    # And the tool that really has nothing to start is still named.
    assert local_library(home)["unstartable_app_ids"] == [228980]


def test_a_game_that_declares_nothing_at_all_is_still_the_user_s_to_choose(tmp_path: Path):
    # `Game` answers on its own, and it has to be asked first: an app that
    # declares nothing to start is a tool, except when Steam has already said it
    # is a game, and then it is a game with an unusual record. Hiding one is the
    # mistake this whole filter is written not to make.
    home = tmp_path / "home"
    library = _library(home)
    for app_id in (220, 1628350):
        _manifest(library, app_id)
    write_appinfo(library, {
        220: {"common": {"name": "A Game", "type": "Game"}, "config": {"installdir": "A Game"}},
        1628350: {"common": {"name": "Steam Linux Runtime 3.0", "type": "Tool"},
                  "config": {"installdir": "SteamLinuxRuntime_sniper"}},
    })

    # The tool beside it declares exactly as little and is still named.
    assert local_library(home)["unstartable_app_ids"] == [1628350]


def test_a_game_that_only_ships_for_linux_is_still_the_user_s_to_choose(tmp_path: Path):
    """The Windows test applies to tools only, and this is why.

    An app Steam types `Game` is left alone whatever it declares. This plugin
    cannot attach to a native build today, and saying so is a refusal the user
    can read; taking the game off the list is a decision made for them.
    """
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 570)
    write_appinfo(library, {
        570: {"common": {"name": "A Linux Game", "type": "Game"},
              "config": {"launch": {"0": {"executable": "game", "config": {"oslist": "linux"}}}}},
    })

    assert local_library(home)["unstartable_app_ids"] == []


def test_a_shortcut_belongs_to_the_device_whose_store_holds_it(tmp_path: Path):
    """The one fact Steam's synced library does not carry."""
    home = tmp_path / "home"
    library = _library(home)
    target = home / "Games/game.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"MZ")
    _shortcuts(library, [3407131886, 4254818636], exe=f'"{target}"')

    found = local_library(home)
    assert found["shortcut_app_ids"] == [3407131886, 4254818636]
    assert found["shortcuts_reason"] is None


def test_a_shortcut_whose_program_is_not_on_this_disk_is_not_this_device_s(tmp_path: Path):
    """A store entry can be copied here, and a game can be deleted under one."""
    home = tmp_path / "home"
    library = _library(home)
    here = home / "Games/game.exe"
    here.parent.mkdir(parents=True)
    here.write_bytes(b"MZ")
    _shortcuts(library, [3407131886], exe=f'"{here}"')
    _shortcuts(library, [4093505389], user="99", exe='"/somewhere/else/other.exe"')

    assert local_library(home)["shortcut_app_ids"] == [3407131886]


def test_every_shape_this_field_actually_takes_is_kept(tmp_path: Path):
    """Read off a Steam Deck's own store, because this is where a filter hides a game.

    An executable with its arguments beside it, a bare command with no path at
    all, and a flatpak wrapper whose own existence says nothing about what it
    runs. Only an absolute path that is not there is treated as absent.
    """
    home = tmp_path / "home"
    library = _library(home)
    xenia = home / "Emulation/roms/xbox360/xenia_canary.exe"
    xenia.parent.mkdir(parents=True)
    xenia.write_bytes(b"MZ")
    _shortcuts(library, [2573626021], exe=f'"{xenia}" "/home/deck/Emulation/roms/xbox360/Lost Odyssey"')
    _shortcuts(library, [2812140668], user="2", exe="ibus-ui-emojier-plasma")
    _shortcuts(library, [2589385289], user="3", exe='"/usr/bin/flatpak"' if Path("/usr/bin/flatpak").exists() else "flatpak")

    assert local_library(home)["shortcut_app_ids"] == [2573626021, 2589385289, 2812140668]


def test_a_device_with_no_shortcut_store_says_so_rather_than_none(tmp_path: Path):
    # The difference decides whether a panel hides every shortcut or keeps them,
    # and "there are none" and "nobody could look" are not the same answer.
    home = tmp_path / "home"
    _library(home)
    found = local_library(home)
    assert found["shortcut_app_ids"] == []
    assert found["shortcuts_reason"] == "this device has no Steam shortcut store to read"


def test_a_user_who_has_added_nothing_is_an_answer_rather_than_a_failed_read(tmp_path: Path):
    # The device most certain to hold no shortcuts is a clean one, and it was
    # the device this put the whole account's shortcuts back on: a Steam user
    # folder with no `shortcuts.vdf` in it was reported the same way a store
    # nobody could read is, and the panel reads that as "keep everything".
    home = tmp_path / "home"
    library = _library(home)
    (library / "userdata/0/config").mkdir(parents=True)

    found = local_library(home)
    assert found["shortcut_app_ids"] == []
    assert found["shortcuts_reason"] is None


def test_a_manifest_that_names_another_app_is_not_an_install(tmp_path: Path):
    home = tmp_path / "home"
    library = _library(home)
    (library / "steamapps/appmanifest_11.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"12"\n\t"StateFlags"\t\t"4"\n}\n', encoding="utf-8",
    )
    assert local_library(home)["steam_app_ids"] == []


def test_a_manifest_with_no_readable_state_is_kept(tmp_path: Path):
    """Hiding a game the user has is the worse mistake of the two."""
    home = tmp_path / "home"
    library = _library(home)
    (library / "steamapps/appmanifest_220.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"220"\n\t"installdir"\t\t"Half-Life 2"\n}\n', encoding="utf-8",
    )
    assert local_library(home)["steam_app_ids"] == [220]


def test_a_manifest_whose_bytes_cannot_be_read_still_names_an_installed_game(tmp_path: Path):
    # The file is named for the app, so the one thing it still proves when its
    # contents are gone is which game is here. Dropping it hid a game the user
    # has, on a read that never said the game was missing.
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 220, name="Half-Life 2")
    unreadable = library / "steamapps/appmanifest_400.acf"
    unreadable.write_bytes(b"\xff\xfe not text at all")

    found = local_library(home)
    assert found["steam_app_ids"] == [220, 400]
    # One file, not the device: the rest of the library is still an answer.
    assert found["reason"] is None


def test_a_library_folder_that_cannot_be_listed_is_a_reason_rather_than_its_contents(tmp_path: Path):
    # Nothing here knows how many manifests that folder holds, so what came back
    # is part of an answer. The panel reads a reason as "keep everything", which
    # is the only way to be wrong that does not hide a game.
    home = tmp_path / "home"
    library = _library(home)
    _manifest(library, 220, name="Half-Life 2")
    second = tmp_path / "library2"
    (second / "steamapps").mkdir(parents=True)
    (library / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"1"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % second, encoding="utf-8",
    )
    (second / "steamapps").chmod(0o000)
    try:
        found = local_library(home)
    finally:
        (second / "steamapps").chmod(0o755)
    assert found["reason"] == "a Steam library on this device could not be read whole"
    # And what was read is still reported, because the panel shows a library it
    # was given a reason about rather than nothing at all.
    assert found["steam_app_ids"] == [220]


def test_a_library_holding_more_manifests_than_this_reads_says_so(monkeypatch, tmp_path: Path):
    # The bound is a bound on reading, not a claim about the library: the games
    # past it are installs nothing here looked at, and reporting the slice as
    # the whole answer hid every one of them from the picker.
    monkeypatch.setattr(steam_library, "MAX_MANIFESTS", 2)
    home = tmp_path / "home"
    library = _library(home)
    for app_id in (220, 380, 420):
        _manifest(library, app_id)

    found = local_library(home)
    assert found["reason"] == "a Steam library on this device could not be read whole"
    # What was read is still reported; the reason is what keeps the rest.
    assert len(found["steam_app_ids"]) == 2


def test_a_shortcut_store_that_cannot_be_parsed_keeps_every_shortcut(tmp_path: Path):
    # "There are none" and "nobody could read it" are different answers, and a
    # store that is there and corrupt is the second one: the set that comes back
    # is missing entries it has no way to count.
    home = tmp_path / "home"
    library = _library(home)
    store = library / "userdata/0/config/shortcuts.vdf"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"\x00shortcuts\x00\x02appid\x00\x01\x02")

    found = local_library(home)
    assert found["shortcuts_reason"] == "a Steam shortcut store on this device could not be read"


def test_libraries_that_cannot_be_read_are_a_reason_rather_than_an_empty_device(tmp_path: Path):
    missing = tmp_path / "nowhere"
    found = local_library(missing)
    assert found["steam_app_ids"] == []
    assert found["reason"] is not None
    assert found["shortcuts_reason"] is not None


def test_an_application_steam_was_told_about_is_not_a_game(tmp_path: Path):
    """The one thing Steam records about how a shortcut was made.

    Read off a Steam Deck's own store: an emoji picker, a web browser and a
    streaming client sat beside the games, all three carrying the `.desktop`
    file Steam made them from, and every game on the same device carrying an
    empty one. Browsing to a program writes no path; picking one of the
    applications the system advertises writes where it came from.
    """
    home = tmp_path / "home"
    library = _library(home)
    game = home / "Emulation/roms/game.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"MZ")
    _shortcuts(library, [2573626021], exe=f'"{game}"')
    _shortcuts(library, [2812140668], user="2", exe='"ibus-ui-emojier-plasma"',
               shortcut_path="/usr/share/applications/org.kde.plasma.emojier.desktop")
    _shortcuts(library, [2589385289], user="3", exe='"/usr/bin/flatpak"',
               shortcut_path="/var/lib/flatpak/exports/share/applications/com.moonlight_stream.Moonlight.desktop")

    assert local_library(home)["shortcut_app_ids"] == [2573626021]


def test_a_shortcut_somebody_pointed_steam_at_stays_whatever_it_runs(tmp_path: Path):
    """The flatpak wrapper is not the test; how the entry was made is.

    A game launched through `/usr/bin/flatpak` is still a game if somebody
    browsed to it, and the same path under an application's own `.desktop`
    entry is not. Reading the program would have got both of those wrong.
    """
    home = tmp_path / "home"
    library = _library(home)
    _shortcuts(library, [4093505389], exe='"/usr/bin/flatpak"', shortcut_path="")

    assert local_library(home)["shortcut_app_ids"] == [4093505389]
