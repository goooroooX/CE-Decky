"""Which Windows executable a Steam game starts, read only, for one AppID.

A cheat table very often names no process, and a Steam library entry's launch
executable is only observable in the running game's own process table. Together
those mean the exact case a user hits first: a table downloaded for a game that
is not running has nothing to attach to, the Review screen has no candidate to
offer, and **Use this table** cannot be pressed at all until the game has been
started once. Half-Life 2 and its own table are exactly that.

Steam's own record answers it. The client has to know what to start, and
`steam_launch.py` reads what it holds: for Half-Life 2 that is `hl2.exe`, for
Counter-Strike 2 `game/bin/win64/cs2.exe`, each of them the exact program Steam
would run for the branch this device has installed. What that record names is
resolved under the game's own install directory and reported only where the file
is actually there.

Walking the install folder is what is left when it does not answer. That walk is
a list of what is there rather than an answer to the question, and Half-Life 2
is why it is second: its root holds `hl2.exe` and its `bin/` holds twenty-seven
SDK compilers, all of them plausible to anything reading names, and the review
screen offered the lot.

Either way this is evidence and not a decision. Nothing here claims which
executable owns a game's memory: the frontend ranks what it is given against
everything else it knows, and the user confirms the choice with the press that
uses the table. A refusal is a named reason rather than an empty list, because
an empty list reads as a game with no executables in it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .atomic import read_regular_bytes
from .managed_ce import MAX_STEAM_VDF_BYTES, discover_steam_library_roots
from .steam_launch import windows_launch_entries

# How deep under the install directory a game's own executable is looked for.
#
# A game binary is at the root or one or two directories down: `hl2.exe` is at
# the root, and an Unreal title's shipping binary is at
# `<Game>/Binaries/Win64/<Game>-Win64-Shipping.exe`, which is two. One further
# level is allowed for a game that nests its own subproject; deeper than that is
# a toolchain, a redistributable tree or a bundled runtime, and walking it costs
# a user's time for candidates none of which is the answer.
#
# This is a bound on what is worth offering and not on what exists. Where the
# search comes back empty the question changes - "is there a Windows program in
# this install at all" - and that one is answered past this depth, by
# `_holds_an_executable`, because an install with its program one level further
# down is not a native Linux build and must not be refused as one.
MAX_DEPTH = 3
# How many directory entries the whole walk may look at.
#
# Half-Life 2 alone holds 30-odd SDK tools under `bin/`, and a modern title's
# tree runs to tens of thousands of files. This bounds the work rather than the
# answer: the walk stops and says it stopped.
MAX_ENTRIES = 20000
# How many executables are reported. More than this is not a list anybody reads
# on a controller, and the ranking below has already put the likely ones first.
MAX_EXECUTABLES = 64
# A basename this will report. Deliberately the same shape the frontend and the
# profile store validate a target process with: anything else cannot be used as
# a target anyway, so offering it would be offering a choice that fails later.
_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+()\[\]-]{0,127}\.exe$", re.IGNORECASE)
_INSTALLDIR_RE = re.compile(r'"installdir"\s*"((?:[^"\\]|\\.)*)"')
_APPID_RE = re.compile(r'"appid"\s*"(\d{1,20})"')
# The branch a game is installed from, which decides which of an app's launch
# entries is about the files on this disk. Absent for the public branch.
_BETAKEY_RE = re.compile(r'"betakey"\s*"([^"]{0,120})"', re.IGNORECASE)


# Why an install directory could not be resolved, in one word each, keyed by the
# sentence the resolution returns. A screen chooses what to offer from these,
# and a sentence is a thing somebody improves without knowing who reads it.
_INSTALL_CAUSES = {
    "this device's Steam libraries could not be read": "libraries_unreadable",
    "Steam has no record of this game being installed on this device": "not_installed",
    "this game is installed in more than one Steam library, so its files are ambiguous": "ambiguous_library",
}


@dataclass(frozen=True)
class _Installed:
    """Where one game's files are, and which branch of the game they are."""

    directory: Path
    branch: str | None


@dataclass(frozen=True)
class GameExecutable:
    """One Windows executable a game holds, and how it came to be reported."""

    name: str
    # Where it sits under the install directory, `""` for the root itself. A
    # reader choosing between two candidates wants this: `hl2.exe` at the root
    # and `bin/vrad.exe` are not the same kind of thing, and the path is what
    # says so without this module having to claim which is the game.
    directory: str
    depth: int
    size_bytes: int
    # Whether Steam itself names this as something it starts for this game, as
    # against this being one of the executables a walk of the folder found. The
    # screen that offers it says which, because they are not the same claim.
    declared: bool = False
    # What Steam calls this launch option, where it calls it anything. One game
    # can declare several and the description is how a reader tells them apart.
    description: str | None = None

    def public(self) -> dict[str, object]:
        return {
            "name": self.name,
            "directory": self.directory,
            "depth": self.depth,
            "size_bytes": self.size_bytes,
            "declared": self.declared,
            "description": self.description,
        }


def game_executables(user_home: Path, app_id: int) -> dict[str, object]:
    """What this installed Steam game starts, and what its folder holds otherwise.

    Read only, bounded, and never a guess: a manifest that cannot be read, an
    install directory that is missing, and a directory that lies outside the
    library it was named in are each a stated reason rather than an empty list.
    """
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
        raise ValueError("app_id must be a positive integer")
    resolved = _install_directory(user_home, app_id)
    if isinstance(resolved, str):
        return {
            "schema": 3,
            "app_id": app_id,
            "install_dir": None,
            "executables": [],
            "truncated": False,
            "source": None,
            "declared_reason": None,
            "reason": resolved,
            "cause": _INSTALL_CAUSES[resolved],
        }
    declared, declared_reason = _declared_executables(user_home, app_id, resolved)
    if declared:
        # Steam's answer is the whole answer. The walk is not run beside it and
        # its results are not added to it: what it would contribute is every
        # other program shipped with the game, offered next to the one the
        # client itself starts, which is the list this replaced.
        return {
            "schema": 3,
            "app_id": app_id,
            "install_dir": str(resolved.directory),
            "executables": [item.public() for item in declared],
            "truncated": False,
            "source": "steam",
            "declared_reason": None,
            "reason": None,
            "cause": None,
        }
    found, truncated, read_everything, deeper = _walk(resolved.directory)
    return {
        "schema": 3,
        "app_id": app_id,
        "install_dir": str(resolved.directory),
        "executables": [item.public() for item in found],
        "truncated": truncated,
        "source": "files",
        # Why the stronger answer was not available, kept whether or not the
        # walk found anything: a screen offering a folder full of candidates is
        # answerable from a support archive only if it says why it is doing
        # that rather than naming the one program Steam starts.
        "declared_reason": declared_reason,
        # An install directory that holds no Windows executable is a real
        # answer and not a failure: a native Linux game is one, and so is a
        # title whose payload has not finished downloading. A walk that stopped
        # at its own bound before finding one is not that answer and must not be
        # reported as it: an asset directory of 25000 files ahead of
        # `Binaries/Win64` would otherwise have this tell the user a game has no
        # executable when it has one.
        "reason": (
            None if found
            else "this game's files were too many to search through"
            if truncated
            else "some of this game's files could not be read"
            if not read_everything
            else "this game's Windows programs are deeper than a game's own executable is looked for"
            if deeper
            else "this game's files hold no Windows executable"
        ),
        # The same answer in one word, because a screen decides what to offer
        # from it and matching on a sentence is how that decision goes wrong
        # the first time the sentence is improved. `no_windows_executable` is
        # the one that means something rather than nothing: the walk completed
        # and there is no Windows program here, which is what a game installed
        # as a native Linux build looks like, and Cheat Engine has nothing to
        # attach to in one. It is said only when nothing else could have
        # produced the empty answer: not a directory that could not be opened,
        # not a bound that stopped the search, and not a Windows program sitting
        # deeper than a candidate is looked for. That last one is its own cause,
        # because it is a game whose target is unidentified rather than a game
        # with nothing to attach to, and the screen that refuses a press must
        # refuse it only for the second.
        "cause": (
            None if found
            else "too_many_files" if truncated
            else "unreadable_files" if not read_everything
            else "executable_too_deep" if deeper
            else "no_windows_executable"
        ),
    }


def _declared_executables(
    user_home: Path, app_id: int, installed: _Installed,
) -> tuple[list[GameExecutable], str | None]:
    """What Steam says it starts for this game, where the file is actually there.

    A record that names a program this device does not hold is not an answer
    about this device: a branch switched under a cache Steam has not rewritten
    yet, or a depot that has not finished downloading, both look exactly like
    that. So each name is resolved under the install directory and reported only
    once it has been found there, and a record whose every name is missing falls
    back to the walk rather than offering a program nobody can attach to.
    """
    try:
        steam_roots, _ = discover_steam_library_roots(user_home)
    except (OSError, RuntimeError, ValueError):
        return [], "this device's Steam libraries could not be read"
    reason: str | None = "this device has no Steam application cache to read"
    for root in sorted(steam_roots):
        try:
            entries, why = windows_launch_entries(root, app_id, installed.branch)
        except (OSError, ValueError, RuntimeError) as exc:
            reason = str(exc)[:200]
            continue
        if not entries:
            reason = why
            continue
        found: list[GameExecutable] = []
        for entry in entries:
            resolved = _resolve_under(installed.directory, entry.path)
            if resolved is None:
                continue
            try:
                size = int(resolved.stat().st_size)
            except OSError:
                continue
            found.append(GameExecutable(
                name=entry.name,
                directory=entry.directory,
                depth=len(entry.directory.split("/")) if entry.directory else 0,
                size_bytes=size,
                declared=True,
                description=entry.description,
            ))
        if found:
            return found, None
        reason = "the program Steam starts for this game is not in its installed files"
    return [], reason


def _resolve_under(root: Path, relative: str) -> Path | None:
    """One install-relative path as it actually exists, or nothing.

    Component by component, because a depot packed on Windows and unpacked on a
    case sensitive filesystem is the ordinary way a name Steam wrote does not
    open: `Binaries/Win64` against `binaries/win64` is the same path to the
    client and two different paths here. The exact spelling is tried first and
    a directory is read only when it does not open, so the usual case costs one
    `stat` and the awkward one costs a listing per component.
    """
    current = root
    for part in relative.split("/"):
        candidate = current / part
        if candidate.exists():
            current = candidate
            continue
        wanted = part.casefold()
        match: Path | None = None
        try:
            with os.scandir(current) as items:
                for seen, item in enumerate(items):
                    if seen >= MAX_ENTRIES:
                        return None
                    if item.name.casefold() == wanted:
                        match = Path(item.path)
                        break
        except OSError:
            return None
        if match is None:
            return None
        current = match
    try:
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    # The same boundary the walk holds: a link out of a user writable game
    # folder is not a path this reports.
    if not resolved.is_file() or not _within(root.resolve(), resolved):
        return None
    return resolved


def _install_directory(user_home: Path, app_id: int) -> _Installed | str:
    """The one library directory this game is installed in, or why there is none.

    One library, not several. The same AppID appearing under two libraries is a
    state this cannot resolve and must not choose between, because the wrong one
    names a different build of the game.
    """
    try:
        _, libraries = discover_steam_library_roots(user_home)
    except (OSError, RuntimeError, ValueError):
        return "this device's Steam libraries could not be read"
    found: list[_Installed] = []
    for library in libraries:
        installed = _installed_under(library, app_id)
        if installed is not None and all(installed.directory != seen.directory for seen in found):
            found.append(installed)
    if not found:
        return "Steam has no record of this game being installed on this device"
    if len(found) > 1:
        return "this game is installed in more than one Steam library, so its files are ambiguous"
    return found[0]


def _installed_under(library: Path, app_id: int) -> _Installed | None:
    """This library's install directory and branch for the AppID, where it holds one."""
    try:
        data = read_regular_bytes(
            library / f"steamapps/appmanifest_{app_id}.acf",
            max_bytes=MAX_STEAM_VDF_BYTES,
            allow_missing=True,
        )
    except (OSError, ValueError):
        return None
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return None
    # The manifest has to be about this AppID. A file named for one app that
    # declares another is corruption, and acting on it would open a different
    # game's files.
    declared = _APPID_RE.search(text)
    if declared is None or int(declared.group(1)) != app_id:
        return None
    match = _INSTALLDIR_RE.search(text)
    if match is None:
        return None
    name = match.group(1).replace(r"\\", "\\").replace(r"\"", '"').strip()
    # One path component, which is what Steam writes here. Anything with a
    # separator in it, or `..`, is escaping the library root and is refused
    # rather than normalised into something that happens to exist.
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        return None
    try:
        root = library.resolve(strict=True)
        directory = (root / "steamapps/common" / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not directory.is_dir() or not _within(root, directory):
        return None
    return _Installed(directory=directory, branch=_branch(text))


def _branch(manifest: str) -> str | None:
    """The beta branch this game is installed from, where it is on one.

    Steam writes nothing here for the public branch, and a launch entry about
    the public branch either names no branch or calls it `default`, so an absent
    value is an answer rather than a gap. It is spelled `betakey` in the manifest
    and `BetaKey` in the application cache, which is why neither is read with
    regard to case.

    The mounted branch wins over the wanted one where the manifest carries both.
    A user who has just asked for a branch that has not finished downloading has
    the old branch's files on the disk, and the question here is which files
    these are rather than which ones Steam is fetching.
    """
    mounted = manifest.partition('"MountedConfig"')[2]
    for section in (mounted, manifest):
        match = _BETAKEY_RE.search(section)
        if match is not None and match.group(1).strip():
            return match.group(1).strip()
    return None


def _within(root: Path, candidate: Path) -> bool:
    """Whether a resolved path is the root or below it."""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def program_in_tree(root: Path, basename: str) -> Path | None:
    """One named Windows program under a directory, where that directory holds it.

    For the game a shortcut points at. A shortcut has no manifest and no install
    folder, so the listing above answers nothing for one; what Steam recorded
    instead is the command it starts, and the directory that command lives in is
    the game's own. That directory is what this is rooted at.

    It invents no name: the basename is the program the screen is about to
    propose attaching to, and this only says where under that directory it is.
    Bounded exactly as the listing is - the same depth, the same entry budget -
    and it never leaves the directory it was given, so a link pointing out of
    the game's folder resolves to no answer rather than to somebody else's file.

    The first one found, which is what proposing a program to attach to wants: a
    game ships one program under that name and the user confirms what this
    proposes. A caller deciding something a user cannot see wants
    `programs_in_tree` instead, which says when there is more than one and when
    it did not get to look everywhere.
    """
    found, _complete = programs_in_tree(root, basename, limit=1)
    return found[0] if found else None


def programs_in_tree(root: Path, basename: str, *, limit: int = 2) -> tuple[list[Path], bool]:
    """Files of that name under the directory, and whether the walk finished.

    Up to `limit` of them, because two is enough to answer the question a caller
    has when it matters: whether the name picks out one file or several. A game
    that ships two copies of a library under one name - one per architecture,
    one per plugin directory - has two answers and no way here to say which one
    it loads, and a caller about to decide something from the contents of a file
    it chose by guessing has to know that.

    The second value is what makes one match usable as an identity. This walk is
    bounded in depth and in entries, so `[one file]` can mean the only one there
    is, or the only one it got to before the bound; a caller that removes code
    on the strength of it needs those told apart. It is true when the walk ran
    out of directories to look in, and when the limit was reached, because more
    than one is more than one however the rest of the tree looks.

    Deeper directories left unvisited, or the entry budget spent, is the whole
    of what makes it false. The directory this searches is the one it was given:
    a file the game loads from outside that tree is not something this can see
    at all, which is the boundary the caller states rather than this.
    """
    if not basename or "/" in basename or "\\" in basename:
        return [], True
    try:
        anchor = root.resolve()
    except OSError:
        return [], False
    wanted = basename.casefold()
    found: list[Path] = []
    seen = 0
    level = [anchor]
    for _ in range(MAX_DEPTH + 1):
        following: list[Path] = []
        for directory in level:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > MAX_ENTRIES:
                            return [], False
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                following.append(Path(entry.path))
                                continue
                            if not entry.is_file(follow_symlinks=False) or entry.name.casefold() != wanted:
                                continue
                        except OSError:
                            continue
                        candidate = Path(entry.path)
                        try:
                            if not candidate.resolve().is_relative_to(anchor):
                                continue
                        except (OSError, ValueError):
                            continue
                        found.append(candidate)
                        if len(found) >= limit:
                            return found, True
            except OSError:
                continue
        if not following:
            return found, True
        level = following
    # Directories left to look in when the depth bound ran out: whatever was
    # found is what this level of the tree holds, not what the tree holds.
    return found, False


def _walk(root: Path) -> tuple[list[GameExecutable], bool, bool]:
    """Bounded breadth-first listing of the `.exe` files under one directory.

    Answers four things: what was found, whether a bound stopped the search,
    whether everything it meant to read it actually read, and - only where it
    found nothing - whether the install holds a Windows program at all, deeper
    than this offers candidates from.

    The last two are separate from the first two on purpose, because two
    different questions are being asked of one tree. `MAX_DEPTH` answers "is
    there a candidate worth offering", and three levels is where a game's own
    executable is; it cannot answer "is there an executable here at all", and
    reading it as though it could is how an install with its program one level
    further down was called a native Linux build. So when the candidate search
    comes back empty the same queue carries on past that depth, collecting
    nothing and ranking nothing, and stops at the first `.exe` it sees. A
    directory this may not open is an accident and is reported as one; a bound
    is a decision, and only a search that met neither turns an empty answer into
    a claim about the game.

    Breadth first because depth is the whole of the ranking: the executable that
    owns a game's memory is at the root far more often than not, and Half-Life 2
    is the case that shows why it matters - its root holds exactly `hl2.exe`
    while `bin/` holds thirty-odd SDK tools that are all equally plausible to
    anything reading names alone.

    Symlinks are not followed. A game directory is user-writable and a link out
    of it is a route to listing a part of the filesystem this has no business
    reading.
    """
    found: list[GameExecutable] = []
    seen = 0
    truncated = False
    # Every directory and every entry this meant to read, it read. A depth this
    # does not descend into is not counted here: that bound is where a game's
    # own executable is, stated at `MAX_DEPTH`, so the scope of the answer
    # rather than a hole in it.
    read_everything = True
    # Directories the candidate search stopped at rather than read. Where it
    # finds something they are dropped, and where it does not they are where the
    # existence question is answered from.
    deferred: list[Path] = []
    level = [(root, "", 0)]
    while level and not truncated:
        next_level: list[tuple[Path, str, int]] = []
        for directory, relative, depth in level:
            # Read the directory through the iterator, counting against the
            # budget as it goes, and sort only what survived. Sorting the whole
            # of `os.scandir` first materialised every entry before a single cap
            # check could run, so the bound held between directories and not
            # inside one: a game with 400000 files in an asset folder built and
            # sorted 400000 objects on the executor before stopping. The
            # context manager is how every other walk in this plugin reads a
            # directory, and without it the iterator is left for the collector
            # to close along with the directory's own descriptor.
            kept: list[tuple[str, str, bool, int]] = []
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > MAX_ENTRIES:
                            truncated = True
                            break
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir():
                                kept.append((entry.name, entry.path, True, 0))
                                continue
                            if not entry.is_file() or not _BASENAME_RE.match(entry.name):
                                continue
                            kept.append((entry.name, entry.path, False, int(entry.stat().st_size)))
                        except OSError:
                            # One entry this could not even classify. It may
                            # have been the executable, so the answer is no
                            # longer whole.
                            read_everything = False
                            continue
            except OSError:
                read_everything = False
                continue
            kept.sort(key=lambda item: item[0].casefold())
            for name, path, is_directory, size in kept:
                if is_directory:
                    if depth < MAX_DEPTH:
                        child = f"{relative}/{name}" if relative else name
                        next_level.append((Path(path), child, depth + 1))
                    else:
                        deferred.append(Path(path))
                    continue
                if len(found) >= MAX_EXECUTABLES:
                    truncated = True
                    break
                found.append(GameExecutable(name, relative, depth, size))
            if truncated:
                break
        level = next_level
    # Asked only where the answer could still become a claim. A search that
    # found something makes none, and one a bound or an unreadable folder
    # stopped already says so, so walking the rest of the install would cost a
    # whole tree to produce a word nothing reads.
    if found or truncated or not read_everything:
        return found, truncated, read_everything, False
    deeper, truncated, read_everything = _holds_an_executable(deferred, seen, read_everything)
    return found, truncated, read_everything, deeper


def _holds_an_executable(queue: list[Path], seen: int, read_everything: bool) -> tuple[bool, bool, bool]:
    """Whether a Windows program exists below the depth candidates come from.

    Existence only: nothing is collected, nothing is ranked, and the first one
    ends the search. What it is for is refusing a claim rather than making one -
    a game whose only `.exe` is deeper than a candidate is looked for is a game
    whose target is unidentified, which is a different thing from a game that
    has no Windows program in it, and only the second may refuse a press.

    Bounded by the same entry budget the candidate search spends from, which is
    the whole fuse: the depth is not bounded here because the question is about
    the install and not about which program to offer.
    """
    truncated = False
    while queue and not truncated:
        next_queue: list[Path] = []
        for directory in queue:
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        seen += 1
                        if seen > MAX_ENTRIES:
                            truncated = True
                            break
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir():
                                next_queue.append(Path(entry.path))
                            elif entry.is_file() and _BASENAME_RE.match(entry.name):
                                return True, truncated, read_everything
                        except OSError:
                            read_everything = False
            except OSError:
                read_everything = False
            if truncated:
                break
        queue = next_queue
    return False, truncated, read_everything
