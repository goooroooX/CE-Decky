"""Which of Steam's library entries this device actually holds.

Steam's library is an account's, not a machine's. The quick access panel built
its game list from what Steam's own client reports and offered every one of
them, and on a second device most of those are titles whose files are somewhere
else: a Steam Deck beside a Steam Machine listed 39 non-Steam shortcuts while
its own shortcut store held 6, because Steam syncs the library rather than the
installs. Choosing one of those is a game the plugin can do nothing with.

So this answers, from the device's own files and nothing else, which entries are
here:

- a Steam app is here when it has an `appmanifest` under one of this device's
  libraries and that manifest says the app is fully installed. A manifest exists
  from the moment a download is queued, so the state flag is what separates a
  game on the disk from one that is merely on its way;
- a non-Steam shortcut is here when this device's own `shortcuts.vdf` holds it,
  the program it points at is on this disk, and Steam did not make it out of an
  installed application's own `.desktop` entry. The file is per device, which is
  exactly the fact the library does not carry, and the target settles the case
  the store cannot: an entry that was copied here, or whose game has since been
  deleted, names a path that is not there. The third test is what keeps a
  desktop off the list: a Steam Deck's own store held an emoji picker, a web
  browser and a streaming client beside its games, and Steam's own record says
  which of the two ways each was added;
- and an installed app Steam declares no way to start is reported separately.
  Proton and the Steam Linux Runtimes are exactly that, eleven of the twenty-two
  installed apps on that Deck, in a list a user scrolls with a thumbstick. What
  Steam calls an app is not the test and would have been the wrong one: it types
  Half-Life 2's three episodes `Tool` as well, and those are games people cheat
  in.

Nothing here decides what the panel does with the answer. It says what is on the
device; refusing, hiding or merely ordering by it is the panel's choice, and a
read that fails says so rather than reporting an empty device, because an empty
answer would hide the user's whole library.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .atomic import read_regular_bytes
from .managed_ce import MAX_STEAM_VDF_BYTES, discover_steam_library_roots
from .shortcuts_vdf import parse_binary
from .steam_launch import apps_with_no_game_to_start

# How many manifests one library may hold before this stops reading them. A
# library of a few hundred games is ordinary; this is a bound, not an
# expectation. A library that goes past it is reported as read in part, because
# the ones beyond the bound are installs this never looked at and the panel must
# not take their absence for proof. The bound is on what is collected rather
# than applied to a listing already in memory, so the reading itself stays
# bounded too.
MAX_MANIFESTS = 4096
# One shortcut store. Steam writes a few kilobytes per shortcut.
MAX_SHORTCUTS_BYTES = 8 * 1024 * 1024
# `StateFlags` bit 2 (value 4) is Steam's "fully installed". The flags observed
# on a healthy device are 4, 6 (installed, update queued) and 68 (installed,
# plus a shared depot), and every one of them carries it.
STATE_FULLY_INSTALLED = 4

_APPID_RE = re.compile(r'"appid"\s*"(\d{1,20})"')
_STATE_RE = re.compile(r'"StateFlags"\s*"(\d{1,20})"', re.IGNORECASE)
_MANIFEST_NAME_RE = re.compile(r"^appmanifest_(\d{1,20})\.acf$")


def local_library(user_home: Path) -> dict[str, object]:
    """What this device holds of the account's library, read only and bounded."""
    try:
        steam_roots, libraries = discover_steam_library_roots(user_home)
    except (OSError, RuntimeError, ValueError):
        return {
            "schema": 1,
            "steam_app_ids": [],
            "unstartable_app_ids": [],
            "shortcut_app_ids": [],
            "reason": "this device's Steam libraries could not be read",
            "shortcuts_reason": "this device's Steam libraries could not be read",
        }
    installed, listed_every_library = _installed_app_ids(libraries)
    unstartable = _unstartable_app_ids(steam_roots, installed)
    shortcuts, shortcuts_reason = _shortcut_app_ids(steam_roots)
    if not libraries:
        reason = "this device declares no Steam library"
    elif not listed_every_library:
        # A library this could not list, or could not read to the end of, says
        # nothing about the games it holds beyond the ones that came back, so
        # what came back is part of an answer rather than an answer. The panel
        # reads a reason as "keep everything", which is the right way to be
        # wrong here.
        reason = "a Steam library on this device could not be read whole"
    else:
        # An empty list of installed apps is a real answer on a device that has
        # none, and only a read that failed is a reason.
        reason = None
    return {
        "schema": 1,
        "steam_app_ids": sorted(installed),
        "unstartable_app_ids": sorted(unstartable),
        "shortcut_app_ids": sorted(shortcuts),
        "reason": reason,
        "shortcuts_reason": shortcuts_reason,
    }


def _installed_app_ids(libraries: list[Path]) -> tuple[set[int], bool]:
    """Every AppID a library's own manifest says is fully installed here.

    With whether every library was read whole, because a folder that could not
    be listed, or that holds more manifests than this reads, holds an unknown
    number of installed games, and the caller has to say so rather than report
    the ones it did reach as all there are. A single manifest that could not be
    read is answered here instead, in the same terms as one whose state is
    unreadable: it is counted as installed.
    """
    found: set[int] = set()
    listed_every_library = True
    for library in libraries:
        steamapps = library / "steamapps"
        # Scanned rather than globbed: `Path.glob` answers a folder it is not
        # allowed to read with no matches, so a library behind a permission this
        # process does not have came back as a library holding no games at all,
        # and every game in it left the picker. One more than the bound is
        # collected, because the only way to know the bound was reached is to
        # meet it.
        manifests: list[tuple[str, int]] = []
        try:
            with os.scandir(steamapps) as entries:
                for entry in entries:
                    named = _MANIFEST_NAME_RE.match(entry.name)
                    if named is None:
                        continue
                    manifests.append((entry.name, int(named.group(1))))
                    if len(manifests) > MAX_MANIFESTS:
                        break
        except OSError:
            listed_every_library = False
            continue
        if len(manifests) > MAX_MANIFESTS:
            listed_every_library = False
            del manifests[MAX_MANIFESTS:]
        for name, app_id in sorted(manifests):
            manifest = steamapps / name
            if app_id <= 0 or app_id > 0xFFFFFFFF:
                continue
            # The file is named for the app it is about, so a manifest that
            # cannot be read or decoded still says which game is installed here;
            # what it does not say is whether the install finished. Counted as
            # installed for the same reason an unreadable state is, below.
            try:
                data = read_regular_bytes(manifest, max_bytes=MAX_STEAM_VDF_BYTES, allow_missing=True)
            except (OSError, ValueError):
                found.add(app_id)
                continue
            if data is None:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                found.add(app_id)
                continue
            # The same refusal `game_files.py` makes: a file named for one app
            # that declares another is corruption rather than an install.
            declared = _APPID_RE.search(text)
            if declared is None or int(declared.group(1)) != app_id:
                continue
            state = _STATE_RE.search(text)
            # A manifest with no readable state is counted as installed: hiding
            # a game the user has is worse than listing one they do not, and
            # this is the read that cannot prove either way. The same applies to
            # one whose bytes never arrived, above.
            if state is not None and not int(state.group(1)) & STATE_FULLY_INSTALLED:
                continue
            found.add(app_id)
    return found, listed_every_library


def _unstartable_app_ids(steam_roots: list[Path], installed: set[int]) -> set[int]:
    """Of those, the ones Steam itself has nothing to start."""
    if not installed:
        return set()
    for root in sorted(steam_roots):
        try:
            found = apps_with_no_game_to_start(root, installed)
        except (OSError, ValueError, RuntimeError):
            continue
        if found:
            return found
    return set()


def _is_a_desktop_application(shortcut_path: object) -> bool:
    """Whether Steam made this shortcut out of an installed application's own entry.

    Steam's "Add a Non-Steam Game" offers two routes, and it records which was
    taken. Browsing to a program writes no `ShortcutPath` at all; picking one of
    the applications the system advertises writes the `.desktop` file it came
    from. A device is full of the second kind and none of them are games.

    Read off a Steam Deck's own store, which is where this was decided: Emoji
    Selector carries `/usr/share/applications/org.kde.plasma.emojier.desktop`,
    Google Chrome and Moonlight carry their flatpak exports under
    `/var/lib/flatpak/`, and every game on the same device - an emulator's ROM,
    a launcher's title, a Windows game under Proton - carries an empty one.

    This is Steam's own record of how the entry was made, not a guess from the
    name or the path, which is why it is the test. What it cannot separate is a
    game a user added from its own `.desktop` entry, and a flatpak game is
    exactly that: this hides it. That is the known cost, and it is stated in
    `docs/FIELD_NOTES.md` rather than hidden behind a heuristic that looks
    surer than it is.
    """
    if not isinstance(shortcut_path, str):
        return False
    return shortcut_path.strip().casefold().endswith(".desktop")


def _target_is_here(exe: object) -> bool:
    """Whether this shortcut's program is on this device, where that is provable.

    Only an absolute path that is not there is treated as absent. Every other
    shape is kept, because the field is a command line rather than a path and
    the ways it can be written are exactly where a filter like this hides a game
    somebody has. The Steam Deck this was measured on holds all of them: an
    executable with its arguments beside it and the whole thing quoted,
    `"/home/deck/Emulation/roms/xbox360/xenia_canary.exe" "/home/deck/..."`; a
    bare command with no path at all, `ibus-ui-emojier-plasma`; and a flatpak
    wrapper, `/usr/bin/flatpak`, whose own existence says nothing about the
    application it runs.
    """
    if not isinstance(exe, str):
        return True
    text = exe.strip()
    if not text:
        return True
    if text.startswith('"'):
        closing = text.find('"', 1)
        target = text[1:closing] if closing > 1 else text[1:]
    else:
        target = text.split()[0]
    if not target.startswith("/"):
        return True
    try:
        return Path(target).exists()
    except OSError:
        return True


def shortcut_program(user_home: Path, app_id: int) -> str | None:
    """The program Steam itself recorded for one non-Steam shortcut.

    A shortcut has no manifest and no install folder, so the listing that
    answers for a Steam library entry answers nothing for one. What it has
    instead is Steam's own record of what it starts, which is the same store
    this module already reads to know the shortcut exists at all.

    The field is a command line rather than a path: it can be quoted, it can
    carry arguments, and it can be a wrapper that says nothing about what it
    runs. Only an absolute path is returned, and only when it is there; every
    other shape is no answer rather than a guess at one.
    """
    try:
        roots, _ = discover_steam_library_roots(user_home)
    except (OSError, ValueError, RuntimeError):
        return None
    signed = app_id - (1 << 32) if app_id >= (1 << 31) else app_id
    for root in sorted(roots if isinstance(roots, list) else [roots]):
        userdata = Path(root) / "userdata"
        try:
            users = sorted(os.listdir(userdata))
        except OSError:
            continue
        for user in users:
            try:
                data = read_regular_bytes(userdata / user / "config/shortcuts.vdf", max_bytes=MAX_SHORTCUTS_BYTES, allow_missing=True)
            except (OSError, ValueError):
                continue
            if data is None:
                continue
            try:
                nodes = parse_binary(data)
            except (ValueError, RuntimeError):
                continue
            for top in nodes:
                if top.key.casefold() != "shortcuts" or not isinstance(top.value, list):
                    continue
                for entry in top.value:
                    if not isinstance(entry.value, list):
                        continue
                    fields = {
                        getattr(node, "key", "").casefold(): node.value
                        for node in entry.value if hasattr(node, "key")
                    }
                    stored = fields.get("appid")
                    if not isinstance(stored, int) or isinstance(stored, bool) or stored not in (app_id, signed):
                        continue
                    target = _absolute_target(fields.get("exe"))
                    if target is not None:
                        return target
    return None


def _absolute_target(exe: object) -> str | None:
    """The absolute program one shortcut's command line names, where it names one."""
    if not isinstance(exe, str):
        return None
    text = exe.strip()
    if not text:
        return None
    if text.startswith('"'):
        closing = text.find('"', 1)
        target = text[1:closing] if closing > 1 else text[1:]
    else:
        target = text.split()[0]
    if not target.startswith("/"):
        return None
    try:
        return target if Path(target).is_file() else None
    except OSError:
        return None


def _shortcut_app_ids(steam_roots: list[Path]) -> tuple[set[int], str | None]:
    """Every non-Steam shortcut this device's own store holds.

    Across every Steam user on the device, because a shortcut belongs to the
    account that made it and the panel is used by whoever is logged in. The
    union can only make the list longer, which is the direction that never hides
    something the user has.

    With the reason the panel keeps every shortcut when there is one, which is
    only ever a read that failed. A store that is there and cannot be read or
    parsed is that; a Steam user folder that holds no `shortcuts.vdf` is not,
    it is a user who has added nothing, and saying otherwise put the whole
    account's shortcuts back on the picker of the one device most certain not
    to hold them - a clean one. What is left for the reason is the case where
    nothing was looked at: a Steam root with no `userdata` at all, where no user
    has ever signed in here and there is nothing yet to be the answer.
    """
    found: set[int] = set()
    listed_a_user_folder = False
    unreadable = False
    for root in sorted(steam_roots):
        userdata = root / "userdata"
        # Listed rather than globbed, for the same reason the manifests are: a
        # folder this may not read answers a glob with no matches, which is
        # indistinguishable from a device whose users have added nothing.
        try:
            users = sorted(os.listdir(userdata))
        except FileNotFoundError:
            continue
        except OSError:
            unreadable = True
            continue
        listed_a_user_folder = True
        for user in users:
            store = userdata / user / "config/shortcuts.vdf"
            try:
                data = read_regular_bytes(store, max_bytes=MAX_SHORTCUTS_BYTES, allow_missing=True)
            except (OSError, ValueError):
                unreadable = True
                continue
            # A store that is not there is a user who has added nothing, which
            # is an answer.
            if data is None:
                continue
            try:
                nodes = parse_binary(data)
            except (ValueError, RuntimeError):
                unreadable = True
                continue
            for top in nodes:
                if top.key.casefold() != "shortcuts" or not isinstance(top.value, list):
                    continue
                for entry in top.value:
                    if not isinstance(entry.value, list):
                        continue
                    # Steam writes `appid` lower case and `AppName` and `Exe`
                    # capitalised in the same record, so nothing here is
                    # matched by case.
                    fields = {
                        getattr(node, "key", "").casefold(): node.value
                        for node in entry.value if hasattr(node, "key")
                    }
                    app_id = fields.get("appid")
                    if not isinstance(app_id, int) or isinstance(app_id, bool):
                        continue
                    if not _target_is_here(fields.get("exe")):
                        continue
                    if _is_a_desktop_application(fields.get("shortcutpath")):
                        continue
                    # Stored as a signed 32 bit integer and used everywhere
                    # else as the unsigned one.
                    found.add(app_id + (1 << 32) if app_id < 0 else app_id)
    if unreadable:
        return found, "a Steam shortcut store on this device could not be read"
    if not listed_a_user_folder:
        return found, "this device has no Steam shortcut store to read"
    return found, None
