"""What Steam itself starts for one AppID, read from its own application cache.

A cheat table very often names no process, and the executable that owns a game's
memory was previously looked for by walking the game's install folder. That walk
is a list of what is there rather than an answer: Half-Life 2 ships one
`hl2.exe` and twenty-seven SDK compilers under `bin/`, all of them plausible to
anything reading names, and the Review screen offered the lot.

Steam does not have to guess. `appcache/appinfo.vdf` carries, per app, the
launch entries the client itself uses: the executable relative to the install
directory, the platform it is for, the branch it belongs to and the DLC it is
conditioned on. For AppID 220 that is `hl2.exe`, for Counter-Strike 2 it is
`game/bin/win64/cs2.exe`. If Steam knows what to start, so do we.

This reads that file and nothing else. It is bounded at every step, follows no
symlink, and materialises one app's record rather than the file: a large Steam
account's cache runs to tens of megabytes and nearly all of it belongs to apps
this device has never installed, so entries for other AppIDs are skipped by the
length Steam wrote in front of them. Every failure is a stated reason, because
the caller has a weaker answer to fall back to and a silent empty list would be
indistinguishable from a game with nothing to start.

The format is Valve's binary VDF, two headers deep:

- the file header is a magic, a universe and, for `0x07564429`, the offset of a
  string table holding every key in the file, which its records then index;
- each app is `appid`, the byte length of its record, a fixed metadata block,
  and one binary VDF map. `appid` zero ends the file.

Nothing here executes, resolves or launches anything. What it produces is the
name of an executable, which the caller resolves under the install directory it
found for itself and the user confirms with the press that uses the table.
"""
from __future__ import annotations

import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

# The three container versions Steam has written. Older files carry no string
# table and spell their keys inline; `0x07564429` moved every key into one table
# at the end of the file and refers to them by index.
_MAGIC_V27 = 0x07564427
_MAGIC_V28 = 0x07564428
_MAGIC_V29 = 0x07564429
_MAGICS = {_MAGIC_V27, _MAGIC_V28, _MAGIC_V29}

# Binary VDF value types. Only these appear in an app record; anything else is
# refused rather than skipped, because a type this does not know is a length
# this cannot compute and every byte after it would be read at the wrong offset.
_MAP = 0x00
_STRING = 0x01
_INT32 = 0x02
_FLOAT32 = 0x03
_POINTER = 0x04
_WIDESTRING = 0x05
_COLOR = 0x06
_UINT64 = 0x07
_MAP_END = 0x08
_INT64 = 0x0A

# How much of the cache this will read. The file is skipped through rather than
# loaded, so these bound what is materialised and what is walked past.
MAX_CACHE_BYTES = 512 * 1024 * 1024
MAX_STRING_TABLE_BYTES = 16 * 1024 * 1024
MAX_STRING_TABLE_ENTRIES = 500_000
# One app's own record. Steam writes a few kilobytes per app; a megabyte is
# already far past anything observed and is a bound rather than an expectation.
MAX_APP_RECORD_BYTES = 4 * 1024 * 1024
# How many app records may be walked past looking for one. A cache holding more
# than this is not one this should spend a user's time on.
MAX_APPS_SCANNED = 200_000
# How deep a launch map may nest. The shape is two levels: the entry and its
# `config`. This is the refusal for a file that claims otherwise.
MAX_DEPTH = 8
# How many launch entries are reported. Steam writes a handful; a game with more
# than this is offering options, and the ranking has already put the plain ones
# first.
MAX_LAUNCH_ENTRIES = 12

# The same basename shape the frontend and the profile store validate a target
# process with: anything else cannot be used as a target anyway.
_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()\[\]-]{0,127}\.exe$", re.IGNORECASE)

# Launch types that are not the game. Steam's own client offers these as
# separate menu items: a configuration tool, a dedicated server, a level editor,
# the manual. `none` is not among them, because `none` is what an ordinary
# launch entry carries when its author set no type at all.
_NOT_THE_GAME = frozenset({"config", "server", "editor", "manual", "brochure"})

# What a branch is called when a game is installed from the public one. Steam
# writes no branch in the manifest for it, and a launch entry that is about it
# spells it one of these two ways.
_PUBLIC_BRANCHES = frozenset({"default", "public"})


@dataclass(frozen=True)
class LaunchEntry:
    """One Windows launch entry Steam holds for an app."""

    index: int
    # Relative to the install directory, forward slashed. Steam writes this with
    # Windows separators: `game\\bin\\win64\\cs2.exe`.
    path: str
    name: str
    directory: str
    description: str | None
    # The branch this entry is for, where it names one. `None` means it applies
    # whatever the game is installed from.
    branch: str | None


def windows_launch_entries(steam_root: Path, app_id: int, branch: str | None) -> tuple[list[LaunchEntry], str | None]:
    """The Windows executables Steam would start for this app, and why there are none.

    `branch` is the beta branch the game is actually installed from, as its own
    manifest records it, so an entry written for another branch is dropped
    rather than offered: Half-Life 2 carries one entry per branch and only one
    of them is about the files on this disk.
    """
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
        raise ValueError("app_id must be a positive integer")
    cache = steam_root / "appcache" / "appinfo.vdf"
    try:
        record, strings = _app_record(cache, app_id)
    except _CacheError as exc:
        return [], str(exc)
    if record is None:
        return [], "Steam's own record holds nothing about this game"
    try:
        node = _root_map(record, strings)
    except _CacheError as exc:
        return [], str(exc)
    config = node.get("config")
    launch = config.get("launch") if isinstance(config, dict) else None
    if not isinstance(launch, dict) or not launch:
        return [], "Steam's own record names nothing for this game to start"
    found = _windows_entries(launch, branch)
    if not found:
        return [], "Steam's own record names no Windows program for this game"
    return found, None


def _root_map(record: bytes, strings: list[str] | None) -> dict[str, object]:
    """An app record's one outer map, past the type byte and name that open it.

    The record is a map like any other and is written the same way: the type
    that says a map follows, the key it is filed under, and then its members.
    Reading it as though it began at its members is how a record that is
    perfectly well formed reports nothing at all.
    """
    if not record or record[0] != _MAP:
        raise _CacheError("Steam's own record for this game is not in a shape this reads")
    _name, offset = _read_key(record, 1, strings)
    node, _ = _read_map(record, offset, strings, 0)
    return node


def apps_with_no_game_to_start(steam_root: Path, app_ids: set[int]) -> set[int]:
    """Of these installed apps, the ones with no game in them for this plugin.

    Proton, the Steam Linux Runtimes and the compatibility tools beside them are
    installed apps that are not games, and a panel offering a game to cheat in
    should not list eleven of them. What separates them is not what Steam calls
    them: measured on a Steam Deck, every Proton build and every Steam Linux
    Runtime carries no launch entry at all, while Half-Life 2's three episodes
    carry three each and Steam's own record types all three of them `Tool`.
    Filtering on the type would have hidden the episodes, which are games people
    cheat in; filtering on whether Steam has anything to start does not.

    Two shapes, then, and the second is why the first was not enough. Steamworks
    Common Redistributables is installed on most devices, is typed `Tool`, and
    carries one launch entry - so it is not nothing to start, and it appeared in
    the list under whatever name the user's Steam is in. That one entry declares
    no `oslist` and names no `.exe`, while all seven of Half-Life 2's and all
    three of each episode's name an operating system and a Windows program.

    So an app is left out when Steam declares nothing to start, and also when
    Steam types it `Tool` and nothing it declares is a Windows program. The type
    alone would take the episodes; the Windows test alone would take a game that
    only ships for Linux, which this plugin cannot attach to today but which is
    the user's to choose. Together they take the tools and leave every `Game`
    exactly as it was - which is why the type is read first and answers on its
    own for a `Game`, rather than being consulted after the launch entries have
    already decided.

    An app this cannot read is not in the answer. The caller keeps what it is
    not told about, because this decides what a user is allowed to choose.
    """
    wanted = {
        app_id for app_id in app_ids
        if isinstance(app_id, int) and not isinstance(app_id, bool) and 0 < app_id <= 0xFFFFFFFF
    }
    if not wanted:
        return set()
    cache = steam_root / "appcache" / "appinfo.vdf"
    found: set[int] = set()
    try:
        with _open_regular(cache) as handle:
            for app_id, record, strings in _each_app_record(handle, wanted):
                try:
                    node = _root_map(record, strings)
                except _CacheError:
                    continue
                common = node.get("common")
                kind = common.get("type") if isinstance(common, dict) else None
                typed = kind.casefold() if isinstance(kind, str) else ""
                # Read before anything the record declares, because one answer
                # here settles the whole question: an app Steam types `Game` is
                # the user's to choose whatever it declares. Asking what it
                # declares first meant a game whose record carries no launch map
                # at all - which is a shape this has not seen but has no reason
                # to refuse - was dropped for declaring nothing to start, and a
                # game the user has is the one thing this must not hide.
                if typed == "game":
                    continue
                config = node.get("config")
                launch = config.get("launch") if isinstance(config, dict) else None
                if not isinstance(launch, dict) or not launch:
                    found.add(app_id)
                    continue
                if typed != "tool":
                    continue
                # The same reading the Review screen makes, rather than a second
                # opinion about the same entries: what is offered there is what
                # decides whether there is a game here at all. Asked of every
                # branch, because a tool with a Windows program on any of them is
                # one this has no business hiding - and asking for no branch is
                # not that: it means the branch nobody named, which keeps only
                # the public ones and dropped a user who is actually on a beta.
                if not _windows_entries(launch, None, any_branch=True):
                    found.add(app_id)
    except (FileNotFoundError, OSError, _CacheError):
        return found
    return found


def _windows_entries(launch: dict[str, object], branch: str | None, *, any_branch: bool = False) -> list[LaunchEntry]:
    """The entries that are about this branch, this platform and the game itself.

    `any_branch` drops the branch test rather than widening it, for the one
    caller that is asking whether this app has a Windows program at all.
    """
    wanted = (branch or "").strip().casefold()
    found: list[LaunchEntry] = []
    seen: set[str] = set()
    for key, entry in sorted(launch.items(), key=_launch_order):
        if len(found) >= MAX_LAUNCH_ENTRIES:
            break
        if not isinstance(entry, dict):
            continue
        options = entry.get("config")
        options = options if isinstance(options, dict) else {}
        # Conditioned on downloadable content this cannot prove is owned. It is
        # bonus material or a workshop tool either way, never the game. The test
        # is that the condition is there at all rather than what it says: Steam
        # writes the AppID it names as a number as often as as a string, and
        # reading only the string spelling let Counter-Strike 2's workshop tools
        # through as though they were the game.
        if "ownsdlc" in options:
            continue
        oslist = _text(options.get("oslist")).casefold()
        # An entry with no platform at all is a Windows-only title's single
        # launch line, which is how Anachronox and DuckTales Remastered are
        # written. The `.exe` check below is what actually keeps a native Linux
        # entry out.
        if oslist and "windows" not in {part.strip() for part in oslist.split(",")}:
            continue
        if _text(entry.get("type")).casefold() in _NOT_THE_GAME:
            continue
        entry_branch = _text(options.get("betakey")) or None
        if not any_branch and not _branch_applies(entry_branch, wanted):
            continue
        resolved = _relative_executable(_text(entry.get("executable")))
        if resolved is None:
            continue
        path, name, directory = resolved
        if path.casefold() in seen:
            continue
        seen.add(path.casefold())
        found.append(LaunchEntry(
            index=_launch_index(key),
            path=path,
            name=name,
            directory=directory,
            description=_text(entry.get("description")) or None,
            branch=entry_branch,
        ))
    return found


def _branch_applies(entry_branch: str | None, installed: str) -> bool:
    """Whether an entry written for a branch is about the installed one."""
    if entry_branch is None:
        return True
    named = entry_branch.casefold()
    if installed:
        return named == installed
    return named in _PUBLIC_BRANCHES


def _launch_order(item: tuple[str, object]) -> tuple[int, str]:
    """Steam's own order: the numeric keys it writes, ascending."""
    key = item[0]
    return (int(key), "") if key.isdigit() else (1 << 30, key)


def _launch_index(key: str) -> int:
    return int(key) if key.isdigit() and len(key) < 9 else -1


def _relative_executable(value: str) -> tuple[str, str, str] | None:
    """One install-relative `.exe` path, or nothing where it is not one.

    Steam writes these with Windows separators and they are always relative to
    the install directory. A path that is absolute, that climbs out with `..`,
    or whose final component is not a basename this plugin would accept as a
    target process is refused rather than repaired: the caller resolves it
    against a real directory and a repaired path is one nobody wrote.
    """
    text = value.strip().strip('"').replace("\\", "/").strip()
    if not text or text.startswith("/") or ":" in text or "\0" in text:
        return None
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    name = parts[-1]
    if not _BASENAME_RE.match(name):
        return None
    return "/".join(parts), name, "/".join(parts[:-1])


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


class _CacheError(ValueError):
    """A stated reason this cache could not answer, in the user's terms."""


def _app_record(cache: Path, app_id: int) -> tuple[bytes | None, list[str] | None]:
    """One app's binary VDF body, skipping every other app by its own length."""
    try:
        descriptor = _open_regular(cache)
    except FileNotFoundError:
        raise _CacheError("Steam's own application cache is not on this device") from None
    except OSError:
        raise _CacheError("Steam's own application cache could not be read") from None
    with descriptor as handle:
        for _found, body, table in _each_app_record(handle, {app_id}):
            return body, table
        # The walk ran to the end without this app in it, which is a cache that
        # holds nothing about it rather than one that could not be read. No key
        # table is returned with that, because there is no record to read with
        # it and the caller does not look.
        return None, None


def _each_app_record(handle, wanted: set[int]):
    """Walk the cache from the start, yielding the records that were asked for.

    One pass however many apps are wanted, because every record carries its own
    length and a record nobody asked about is seeked past rather than read. The
    walk stops as soon as the last one asked for has been yielded.
    """
    handle.seek(0)
    size = os.fstat(handle.fileno()).st_size
    if size <= 16 or size > MAX_CACHE_BYTES:
        raise _CacheError("Steam's own application cache is not a size this can read")
    magic, _universe = struct.unpack("<II", _exact(handle, 8))
    if magic not in _MAGICS:
        raise _CacheError("Steam's own application cache is in a format this does not read")
    strings = _string_table(handle, size) if magic == _MAGIC_V29 else None
    # The fixed metadata between an app's length and its record: info state,
    # last updated, access token, the record's SHA-1, the change number, and
    # from v28 the SHA-1 of the binary VDF itself.
    metadata = 4 + 4 + 8 + 20 + 4 + (20 if magic >= _MAGIC_V28 else 0)
    outstanding = set(wanted)
    if not outstanding:
        return
    scanned = 0
    while scanned < MAX_APPS_SCANNED:
        header = handle.read(8)
        if len(header) < 8:
            return
        found, length = struct.unpack("<II", header)
        if found == 0:
            return
        if length < metadata or length > MAX_APP_RECORD_BYTES:
            raise _CacheError("Steam's own application cache holds a record this cannot read")
        scanned += 1
        if found not in outstanding:
            handle.seek(length, os.SEEK_CUR)
            continue
        outstanding.discard(found)
        yield found, _exact(handle, length)[metadata:], strings
        if not outstanding:
            return
    raise _CacheError("Steam's own application cache holds more apps than this reads")


def _string_table(handle, size: int) -> list[str]:
    """Every key in the file, which its app records refer to by index."""
    (offset,) = struct.unpack("<q", _exact(handle, 8))
    if offset <= 0 or offset >= size or size - offset > MAX_STRING_TABLE_BYTES:
        raise _CacheError("Steam's own application cache has no readable key table")
    here = handle.tell()
    handle.seek(offset)
    (count,) = struct.unpack("<I", _exact(handle, 4))
    if count > MAX_STRING_TABLE_ENTRIES:
        raise _CacheError("Steam's own application cache has more keys than this reads")
    blob = _exact(handle, size - offset - 4)
    handle.seek(here)
    parts = blob.split(b"\0")
    # One trailing empty part is the terminator of the last key; anything else
    # means this is not the table it was pointed at.
    if len(parts) - 1 != count:
        raise _CacheError("Steam's own application cache has an unreadable key table")
    return [part.decode("utf-8", "replace") for part in parts[:count]]


def _open_regular(path: Path):
    """Open one existing regular file without following the final component."""
    if path.is_symlink():
        raise _CacheError("Steam's own application cache is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise _CacheError("Steam's own application cache is not a regular file")
    return os.fdopen(descriptor, "rb")


def _exact(handle, count: int) -> bytes:
    """Exactly this many bytes, or the file is not the file it claims to be."""
    if count < 0 or count > MAX_STRING_TABLE_BYTES:
        raise _CacheError("Steam's own application cache holds a record this cannot read")
    data = handle.read(count)
    if len(data) != count:
        raise _CacheError("Steam's own application cache ends in the middle of a record")
    return data


def _read_map(data: bytes, offset: int, strings: list[str] | None, depth: int) -> tuple[dict[str, object], int]:
    """One binary VDF map, and where it ended.

    Values this does not need are read past rather than kept, but they are still
    read: a length that is skipped by guesswork puts every byte after it at the
    wrong offset, which is how a parser reports a plausible executable that
    nobody wrote.
    """
    if depth > MAX_DEPTH:
        raise _CacheError("Steam's own record for this game is nested deeper than this reads")
    out: dict[str, object] = {}
    while True:
        if offset >= len(data):
            raise _CacheError("Steam's own record for this game ends in the middle of itself")
        kind = data[offset]
        offset += 1
        if kind == _MAP_END:
            return out, offset
        key, offset = _read_key(data, offset, strings)
        if kind == _MAP:
            value, offset = _read_map(data, offset, strings, depth + 1)
        elif kind == _STRING:
            value, offset = _read_string(data, offset)
        elif kind == _WIDESTRING:
            # Two bytes per character, terminated by two zero bytes. Never seen
            # in a launch entry; read past so the entries after it still line
            # up. Scanned a character at a time rather than searched for, since
            # the terminator is only a terminator where a character starts: an
            # ordinary Latin letter is a byte and a zero byte, so `E` followed
            # by the terminator holds a zero pair one byte early, and stopping
            # there leaves half a terminator behind and every field after it
            # read from the wrong place.
            end = offset
            while data[end:end + 2] != b"\0\0":
                if end + 2 > len(data):
                    raise _CacheError("Steam's own record for this game ends in the middle of a value")
                end += 2
            value, offset = "", end + 2
        elif kind in {_INT32, _FLOAT32, _POINTER, _COLOR}:
            value, offset = None, offset + 4
        elif kind in {_UINT64, _INT64}:
            value, offset = None, offset + 8
        else:
            raise _CacheError("Steam's own record for this game holds a value this does not read")
        if offset > len(data):
            raise _CacheError("Steam's own record for this game ends in the middle of a value")
        # Keys are lowercased because Steam writes them both ways: the branch a
        # launch entry is for is `BetaKey` in this file and `betakey` in the
        # game's own manifest.
        out[key.casefold()] = value


def _read_key(data: bytes, offset: int, strings: list[str] | None) -> tuple[str, int]:
    if strings is None:
        return _read_string(data, offset)
    if offset + 4 > len(data):
        raise _CacheError("Steam's own record for this game ends in the middle of a key")
    (index,) = struct.unpack_from("<I", data, offset)
    if index >= len(strings):
        raise _CacheError("Steam's own record for this game names a key the cache does not hold")
    return strings[index], offset + 4


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\0", offset)
    if end < 0:
        raise _CacheError("Steam's own record for this game ends in the middle of a value")
    return data[offset:end].decode("utf-8", "replace"), end + 1
