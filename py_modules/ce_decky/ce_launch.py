"""Plugin-owned Cheat Engine launch.

CE Decky once started Cheat Engine only through Valve's
``PROTON_REMOTE_DEBUG_CMD`` sidecar, written into the game's Steam launch
options. That route is retired and its code was deleted in 0.9.0: it bound the
Cheat Engine process lifetime to one game launch, so a user could not verify
that Cheat Engine runs at all without mutating Steam state and starting a game,
and could not change the selected table without leaving the game.

This module is the only way Cheat Engine is started now, and it writes no Steam
state. It reuses Valve's own documented verbs. Proton (``proton_9.0``, script
``proton``) implements them as::

    g_session.init_session(sys.argv[1] != "runinprefix")
    ...
    elif sys.argv[1] == "runinprefix":
        rc = g_session.run_proc([g_proton.wine_bin] + sys.argv[2:])

which is exactly the invocation Proton uses for the sidecar
(``subprocess.Popen([g_proton.wine_bin] + self.remote_debug_cmd, env=self.env)``),
with prefix files explicitly *not* updated. The plugin-owned self-test prefix
uses the ordinary ``run`` verb in its own prefix, where full prefix setup is
wanted. Native managed extraction does not invoke Proton. Protontricks is the
accepted precedent for driving an installed Proton tool from outside Steam.

Two launch modes share one owned-process supervisor:

``self_test``
    Start the private Cheat Engine runtime in a plugin-owned isolated
    compatibility prefix with no game, no Steam mutation and a synthetic
    session, and require the resident bridge heartbeat. This is the agent- and
    controller-verifiable proof that the promoted Cheat Engine tree actually
    runs under a selected Proton identity.

``attached``
    Start the private Cheat Engine runtime in the *running* game's exact Steam
    compatibility prefix so it shares the game's wineserver. The prefix is
    resolved independently from Steam library metadata and must equal the value
    observed in the live game process environment; a mismatch fails closed.

Because the supervisor owns the process group, stopping Cheat Engine no longer
requires exiting the game, which is what makes controller-driven table
switching possible without an in-process table swap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import md5, sha256
from pathlib import Path, PurePosixPath
import asyncio
import os
import re
import signal
import stat
import threading
import time
import uuid
from typing import Collection, Iterable, Sequence

from . import poll_counters
from .activity_log import log_activity, log_failure
from .child_env import child_environment
from .atomic import (
    DurabilityUnknownError,
    durable_rename,
    durable_unlink,
    atomic_write_bytes,
    atomic_write_json,
    fsync_directory,
    load_json,
    read_proc_bytes,
    read_regular_bytes,
    read_regular_bytes_with_stat,
)
from .managed_ce import (
    ProtonTool,
    _ensure_managed_directory,
    _reset_owned_directory,
    _retire_owned_proton_prefix,
    _safe_status_text,
    retire_owned_proton_prefix_now,
    _steam_root_for_tool,
    _unsafe_text,
    _verify_proton_tool,
    discover_steam_library_roots,
)
from .operations import drain_through_cancellation, drained_to_thread
from .session_protocol import (
    CONTROL_HEADER,
    SessionDescriptor,
    parse_status,
    render_descriptor,
    wine_z_path,
)

MODE_SELF_TEST = "self_test"
MODE_ATTACHED = "attached"
LAUNCH_MODES = (MODE_SELF_TEST, MODE_ATTACHED)
# Proton verbs, chosen per mode and never mixed. `run` performs full prefix
# setup and is used only for the plugin-owned self-test prefix. Native managed
# extraction does not invoke Proton. `runinprefix` runs `wine <argv>` after
# `init_session(update_prefix_files=False)`, so an attached launch never
# rewrites the game's own prefix files.
LAUNCH_VERBS = {MODE_SELF_TEST: "run", MODE_ATTACHED: "runinprefix"}
LIVE_LAUNCH_STATES = frozenset({"starting", "running", "connected"})
# What one baseline PID has to answer for the original game to still be proved
# alive. `unreadable` is in here on purpose: procfs denies a same-user
# non-dumpable process, and reading that as gone would call a live game dead.
ALIVE_BASELINE_STATES = frozenset({"matched", "unreadable"})

# The self-test never represents a real library entry. Reserve the maximum
# 32-bit AppID for it so a synthetic descriptor can never be confused with a
# prepared session for an actual game.
SELF_TEST_APP_ID = 4294967295
SELF_TEST_TABLE = (
    b'<?xml version="1.0" encoding="utf-8"?>\n'
    b'<CheatTable CheatEngineTableVersion="45">\n'
    b"  <CheatEntries/>\n"
    b"</CheatTable>\n"
)

MAX_ENVIRON_BYTES = 512 * 1024
MAX_SCANNED_PROCESSES = 8192
MAX_OBSERVED_PIDS = 64
MAX_OBSERVED_WINDOWS_EXECUTABLES = 32
# The only observation reason that proves the app has no process left at all.
# Every other non-running reason means the scan itself could not answer.
NO_MATCHING_PROCESS_REASON = "no running process reports this Steam AppID"
# How many one-second supervision ticks pass between target-process scans. The
# baseline check reads a handful of known PIDs; this one walks /proc.
TARGET_SCAN_INTERVAL_TICKS = 3
MAX_OBSERVED_ARGV = 256
PROTON_LAUNCH_VERBS = frozenset({"run", "runinprefix", "waitforexitandrun"})
MAX_CMDLINE_BYTES = 64 * 1024
MAX_LAUNCH_LOG_BYTES = 256 * 1024
MAX_ENV_VALUE_BYTES = 4096
MAX_GAMESCOPE_ENV_BYTES = 64 * 1024
MAX_WINDOWS_LAUNCH_PATH_UNITS = 259
# A cold plugin-owned prefix has to be created by Proton on first use,
# which is far slower than an ordinary Cheat Engine start.
SELF_TEST_TIMEOUT_SECONDS = 300.0
HEARTBEAT_POLL_SECONDS = 0.5
STOP_GRACE_SECONDS = 10.0
# How often supervision of a recovered Cheat Engine looks at the game it was
# recovered beside.
#
# One second, which is what it has always been; it is named here because every
# other interval in this file is, and because a regression about what this loop
# decides then costs a second of wall clock per tick it has to reach. The loop
# itself is unchanged: this is a poll against a process table, and nothing about
# a plugin holding an owned Cheat Engine needs it tighter.
RECOVERED_SUPERVISION_POLL_SECONDS = 1.0
# What the unload prologue may spend proving an owned self-test group is gone.
#
# The orderly stop waits `STOP_GRACE_SECONDS` and can afford to: it has an event
# loop and nothing is racing it. The prologue has neither. Decky gives a plugin
# five seconds and then SIGKILLs it, and the loop is starved for all of it, so
# this window is the whole of what the process can spend, and spending it costs
# nothing because nothing else in this process can run while it does.
BEGIN_CLOSE_TERM_SECONDS = 0.6
BEGIN_CLOSE_KILL_SECONDS = 0.3
BEGIN_CLOSE_POLL_SECONDS = 0.02
# And what it may spend asking that prefix's own `wineserver` to stop the Wine
# clients the process group never held.
BEGIN_CLOSE_PREFIX_SECONDS = 1.5
# And what the whole prologue may spend, however many owned launches it finds.
# Decky allows five seconds before SIGKILL, and everything else on the unload
# path is waiting behind this, so it takes a little over half of them.
BEGIN_CLOSE_TOTAL_SECONDS = 3.0
# What a group verdict has to be for the prefix sweep after it to mean anything.
GROUP_GONE = frozenset({"stopped", "killed", "already_gone"})

# Proton derives the whole Wine environment from these. Anything already
# present in the backend environment is removed first so a Decky/Steam variable
# cannot silently redirect an owned launch.
_STRIPPED_ENV_PREFIXES = ("STEAM_COMPAT_", "PROTON_", "WINE", "CE_DECKY_")
_STRIPPED_ENV_NAMES = frozenset({"SteamAppId", "SteamGameId", "SteamClientLaunch", "STEAM_RUNTIME"})


# Executables that positively identify a known anti-cheat as part of a running
# game. Exact basenames only, never inferred from a title, and nothing here acts
# on the anti-cheat itself: the documented boundary is offline/single-player use
# with refusal rather than bypass automation, and this is the refusal.
KNOWN_ANTI_CHEAT_EXECUTABLES: frozenset[str] = frozenset({"belauncher.exe"})


def observed_anti_cheat(executables: Iterable[str]) -> str | None:
    """The known anti-cheat basename in this observation, if any."""
    for name in executables:
        basename = PurePosixPath(str(name).replace("\\", "/")).name.casefold()
        if basename in KNOWN_ANTI_CHEAT_EXECUTABLES:
            return basename
    return None


@dataclass(frozen=True)
class GameContainerObservation:
    """What the live process table says about one running Steam game."""

    app_id: int
    running: bool
    pids: tuple[int, ...]
    compat_data_path: str | None
    steam_client_install_path: str | None
    wine_prefix: str | None
    display: str | None
    launch_executable: str | None
    compat_tool_paths: tuple[str, ...]
    windows_executables: tuple[str, ...]
    conflicting_compat_data_paths: tuple[str, ...]
    conflicting_steam_client_install_paths: tuple[str, ...]
    conflicting_wine_prefixes: tuple[str, ...]
    conflicting_displays: tuple[str, ...]
    scanned: int
    reason: str | None
    # Processes the walk could not attribute to any app, because procfs refused
    # their environment. Most of a machine's process table reads this way for an
    # ordinary user, so this is never evidence on its own: it is the set that has
    # to be looked at again before an absence may be believed, and it is complete
    # for any observation that is allowed to prove one. Last and defaulted, so
    # that every existing construction of this observation, in the module and in
    # its tests, keeps meaning exactly what it meant.
    unreadable_pids: tuple[int, ...] = ()

    def public(self) -> dict[str, object]:
        data = asdict(self)
        # The count, not the list: a few hundred PIDs is noise in a payload and
        # the count is what a report can actually use. It is what this walk
        # actually encountered, including on the paths that stop early, and it
        # is not separately capped.
        data["unreadable_processes"] = len(data.pop("unreadable_pids"))
        return data


@dataclass(frozen=True)
class CompatDataResolution:
    """One exact ``steamapps/compatdata/<AppID>`` directory, or an explicit refusal."""

    app_id: int
    state: str  # resolved | missing | ambiguous | unsafe
    compat_data_path: str | None
    candidates: tuple[str, ...]
    unsafe_paths: tuple[str, ...]

    def public(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SelfTestSession:
    """Synthetic session files for a no-game Cheat Engine launch."""

    session_id: str
    root: str
    descriptor_path: str
    descriptor_sha256: str
    descriptor_md5: str
    descriptor_windows_path: str
    status_path: str
    table_path: str
    table_windows_path: str
    table_sha256: str
    target_process: str

    def public(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CELaunchPlan:
    """Deterministic argv/environment for one owned Cheat Engine launch."""

    mode: str
    app_id: int | None
    tool_id: str
    tool_name: str
    tool_path: str
    proton_sha256: str
    verb: str
    compat_data_path: str
    steam_client_install_path: str
    executable: str
    ce_sha256: str
    argv: tuple[str, ...]
    env_overrides: tuple[tuple[str, str], ...]
    descriptor_windows_path: str
    descriptor_sha256: str
    table_windows_path: str
    table_sha256: str
    session_id: str

    def public(self) -> dict[str, object]:
        value = asdict(self)
        value["argv"] = list(self.argv)
        value["env_overrides"] = [list(item) for item in self.env_overrides]
        return value


def parse_environ(raw: bytes) -> dict[str, str]:
    """Parse a NUL-separated ``/proc/<pid>/environ`` blob.

    Entries that are not valid UTF-8, carry no ``=``, use an empty or
    control-bearing name, or exceed the bounded value size are dropped rather
    than repaired; this is observation input, never a source of truth.
    """
    values: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry or len(entry) > MAX_ENV_VALUE_BYTES:
            continue
        separator = entry.find(b"=")
        if separator <= 0:
            continue
        try:
            name = entry[:separator].decode("utf-8")
            value = entry[separator + 1:].decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not name or any(character in name for character in "\0\n\r=") or _unsafe_text(value, MAX_ENV_VALUE_BYTES):
            continue
        values[name] = value
    return values


def resolve_game_mode_display(
    user_home: Path,
    *,
    runtime_root: Path = Path("/run/user"),
    x11_root: Path = Path("/tmp/.X11-unix"),
) -> str:
    """Resolve the exact active Gamescope X display without guessing.

    Decky backends do not necessarily inherit ``DISPLAY``. SteamOS publishes
    the active Gamescope session environment below the Steam user's protected
    runtime directory; require that exact user's regular file and corroborate
    its display number with an owned live X11 socket before launching Proton.
    """
    user_home = user_home.expanduser().absolute()
    if user_home.is_symlink() or not user_home.is_dir():
        raise ValueError("Steam user home is not a regular directory")
    user_info = user_home.stat()
    user_runtime = runtime_root / str(user_info.st_uid)
    if user_runtime.is_symlink() or not user_runtime.is_dir():
        raise ValueError("the Steam user's runtime directory is unavailable")
    runtime_info = user_runtime.stat()
    if runtime_info.st_uid != user_info.st_uid or stat.S_IMODE(runtime_info.st_mode) & 0o022:
        raise ValueError("the Steam user's runtime directory has unsafe ownership or permissions")

    raw, environment_info = read_regular_bytes_with_stat(
        user_runtime / "gamescope-environment",
        max_bytes=MAX_GAMESCOPE_ENV_BYTES,
    )
    if raw is None or environment_info is None:
        raise ValueError("the active Gamescope environment is unavailable")
    if environment_info.st_uid != user_info.st_uid or stat.S_IMODE(environment_info.st_mode) & 0o022:
        raise ValueError("the Gamescope environment has unsafe ownership or permissions")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("the Gamescope environment is not valid UTF-8") from exc
    if "\0" in text or "\r" in text:
        raise ValueError("the Gamescope environment contains forbidden control characters")
    displays = [line.removeprefix("DISPLAY=") for line in text.splitlines() if line.startswith("DISPLAY=")]
    if len(displays) != 1:
        raise ValueError("the active Gamescope display is missing or ambiguous")
    display = displays[0]
    if not display.startswith(":") or not display[1:].isdigit() or not (0 <= int(display[1:]) <= 65535):
        raise ValueError("the active Gamescope display is invalid")

    return _corroborated_display(display, owner_uid=user_info.st_uid, x11_root=x11_root)


def _corroborated_display(display: str, *, owner_uid: int, x11_root: Path) -> str:
    """Require an owned live X11 socket for the exact display number."""
    if not display.startswith(":") or not display[1:].isdigit() or not (0 <= int(display[1:]) <= 65535):
        raise ValueError("the active Gamescope display is invalid")
    try:
        socket_root_info = x11_root.lstat()
    except OSError as exc:
        raise ValueError("the X11 socket directory is unavailable") from exc
    if stat.S_ISLNK(socket_root_info.st_mode) or not stat.S_ISDIR(socket_root_info.st_mode):
        raise ValueError("the X11 socket directory is unsafe")
    socket_path = x11_root / f"X{int(display[1:])}"
    try:
        socket_info = socket_path.lstat()
    except OSError as exc:
        raise ValueError("the active Gamescope X11 socket is unavailable") from exc
    if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != owner_uid:
        raise ValueError("the active Gamescope X11 socket is unavailable or foreign")
    return display


def resolve_attached_display(
    observation: GameContainerObservation,
    user_home: Path,
    *,
    x11_root: Path = Path("/tmp/.X11-unix"),
) -> str:
    """Resolve the X display the running game itself reports.

    Gamescope runs more than one XWayland server, and Steam and the game do not
    share one: on this target the session publishes `:0` while the game's own
    processes run on `:1`. Cheat Engine joins that game's prefix and its
    wineserver, so it has to join the same display too; starting it on the
    session's display makes Wine's X11 driver operate on window identities from
    another server, and the Xlib default error handler ends the process.

    The display is therefore taken from the running game exactly like its Proton
    identity and prefix, and an absent or ambiguous value fails closed.
    """
    user_home = user_home.expanduser().absolute()
    if user_home.is_symlink() or not user_home.is_dir():
        raise ValueError("Steam user home is not a regular directory")
    if observation.conflicting_displays:
        raise ValueError("the running game's processes report more than one X display")
    if observation.display is None:
        raise ValueError("the running game does not report an X display")
    return _corroborated_display(observation.display, owner_uid=user_home.stat().st_uid, x11_root=x11_root)


MAX_OBSERVED_RUNNING_APPS = 32


@dataclass(frozen=True)
class RunningAppObservation:
    """Bounded list of Steam AppIDs the live process table reports as running."""

    available: bool
    app_ids: tuple[int, ...]
    scanned: int
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {"available": self.available, "app_ids": list(self.app_ids), "scanned": self.scanned, "reason": self.reason}


def is_ce_decky_owned_environment(environ: dict[str, str]) -> bool:
    """True when this process environment is a Cheat Engine CE Decky launched.

    An attached launch deliberately inherits the game's ``SteamAppId`` and
    compatibility environment, because that is what puts Cheat Engine inside the
    game's own prefix. The consequence is that CE Decky's own helper answers
    every "is this game running?" question the same way the game does. The
    attached supervisor already works around it by snapshotting the game's PIDs
    before spawning; the general observers must not repeat the mistake, or a
    game that has exited keeps looking alive because the tool CE Decky launched
    for it is still there.

    Ownership evidence and game evidence are deliberately different questions:
    this predicate removes a process from *game* observation only. Recovering
    and stopping that exact Cheat Engine still works through the durable launch
    record, which matches on the same descriptor identity.
    """
    return bool(environ.get("CE_DECKY_DESCRIPTOR") and environ.get("CE_DECKY_DESCRIPTOR_SHA256"))


def observe_running_app_ids(
    *, proc_root: Path = Path("/proc"), max_processes: int = MAX_SCANNED_PROCESSES
) -> RunningAppObservation:
    """List the Steam AppIDs currently declared by running processes.

    This exists only because some Steam builds expose no running-app query to
    the frontend at all. It offers the user a *candidate* game to select; it is
    never the source of a compatibility prefix, Proton identity or launch path,
    and the caller still resolves those independently. ``SteamAppId`` is Steam's
    own declaration in the process environment, not an inferred identity.

    An incomplete scan is reported as unavailable rather than as a short list,
    because a truncated observation must never look like "that game is not
    running".
    """
    if not proc_root.is_dir():
        return RunningAppObservation(False, (), 0, "process table is unavailable")
    try:
        entries = sorted(
            (entry for entry in os.scandir(proc_root) if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return RunningAppObservation(False, (), 0, "process table is unreadable")
    app_ids: list[int] = []
    scanned = 0
    for entry in entries:
        if scanned >= max_processes:
            return RunningAppObservation(False, (), scanned, "process table exceeds the bounded scan limit")
        scanned += 1
        try:
            raw = read_proc_bytes(Path(entry.path) / "environ", max_bytes=MAX_ENVIRON_BYTES)
        except (OSError, ValueError):
            continue
        if not raw:
            continue
        # SteamGameId carries a 64-bit gameid for non-Steam shortcuts, so only
        # SteamAppId can be trusted as the AppID the library is keyed by.
        environ = parse_environ(raw)
        if is_ce_decky_owned_environment(environ):
            continue
        candidate = environ.get("SteamAppId")
        if not candidate or not candidate.isdigit():
            continue
        try:
            app_id = _app_id(int(candidate))
        except ValueError:
            continue
        if app_id in app_ids:
            continue
        if len(app_ids) >= MAX_OBSERVED_RUNNING_APPS:
            return RunningAppObservation(False, (), scanned, "more running apps than the bounded observation supports")
        app_ids.append(app_id)
    return RunningAppObservation(True, tuple(sorted(app_ids)), scanned, None)


_PREFIX_SYSTEM_PATH_RE = re.compile(r"^[a-z]:\\windows\\", re.IGNORECASE)


def _is_prefix_system_executable(windows_path: str) -> bool:
    """True when this Windows path is inside the prefix's own Windows directory.

    A parent path is normally not identity evidence, because a real game can be
    installed below a store-owned directory. A Wine prefix's Windows directory is
    the exception: Proton installs its own services, helpers and tooling there
    and a game is never installed into it. Keying on that directory keeps every
    such process out of the user's choice without needing to know its name, so a
    newer Proton cannot reintroduce noise this build has never heard of.
    """
    normalized = windows_path.strip().replace("/", "\\")
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return bool(_PREFIX_SYSTEM_PATH_RE.match(normalized))


def _read_argv(cmdline_path: Path) -> tuple[str, ...]:
    """Read one process's bounded argument vector, or nothing readable."""
    try:
        raw = read_proc_bytes(cmdline_path, max_bytes=MAX_CMDLINE_BYTES)
    except (OSError, ValueError):
        return ()
    if not raw:
        return ()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    return tuple(argument for argument in text.split("\0") if argument)[:MAX_OBSERVED_ARGV]


def _windows_executable_basename(argv: tuple[str, ...]) -> str | None:
    """One running process's game-owned Windows executable basename.

    Wine reports its clients with a Windows path in `argv[0]`, so both `\\` and
    `/` separate components. Anything that is not a bounded, unambiguous `.exe`
    basename is dropped rather than repaired, and so is anything running out of
    the prefix's own Windows directory.
    """
    if not argv:
        return None
    text = argv[0]
    if _is_prefix_system_executable(text):
        return None
    basename = text.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return basename if _process_basename(basename) else None


def _proton_launch_target(argv: tuple[str, ...]) -> str | None:
    """The executable Steam asked Proton to run for this game, if this is it.

    Steam launches a game as `<tool>/proton <verb> <executable>`, so the game's
    own launch executable is observable for a Steam library entry exactly as it
    is for a non-Steam shortcut, where Steam records it in AppDetails instead.
    It is only ever a ranking hint for the target-process choice; nothing is
    launched or attached from it.
    """
    for index, argument in enumerate(argv[:-2]):
        if argument != "proton" and not argument.endswith("/proton"):
            continue
        if argv[index + 1] not in PROTON_LAUNCH_VERBS:
            continue
        candidate = argv[index + 2]
        if not candidate.casefold().endswith(".exe"):
            continue
        basename = candidate.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if _process_basename(basename):
            return basename
    return None


def observe_game_executable_path(
    app_id: int, basename: str, *, proc_root: Path = Path("/proc"),
    max_processes: int = MAX_SCANNED_PROCESSES,
) -> str | None:
    return observe_game_executable_paths({app_id: basename}, proc_root=proc_root, max_processes=max_processes).get(app_id)


def observe_game_executable_paths(
    targets: dict[int, str], *, proc_root: Path = Path("/proc"),
    max_processes: int = MAX_SCANNED_PROCESSES,
) -> dict[int, str]:
    """One bounded read-only scan for exact AppID/basename advisory identities.

    Only the game's own unambiguous Wine Z path supplies an executable path.
    Owned CE environments never count as a game; incomplete scans supply none.
    """
    targets = {_app_id(app_id): basename for app_id, basename in targets.items() if _process_basename(basename)}
    if not targets:
        return {}
    identities = {str(app_id): app_id for app_id in targets}
    if not proc_root.is_dir():
        return {}
    try:
        entries = sorted(
            (entry for entry in os.scandir(proc_root) if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return {}
    found: dict[int, str] = {}
    ambiguous: set[int] = set()
    scanned = 0
    for entry in entries:
        if scanned >= max_processes:
            return {}
        scanned += 1
        try:
            raw = read_proc_bytes(Path(entry.path) / "environ", max_bytes=MAX_ENVIRON_BYTES)
        except (OSError, ValueError):
            continue
        if not raw:
            continue
        environ = parse_environ(raw)
        if is_ce_decky_owned_environment(environ):
            continue
        app_ids = {identities[value] for key in ("SteamAppId", "SteamGameId")
                   if (value := environ.get(key)) in identities}
        if len(app_ids) != 1:
            continue
        app_id = next(iter(app_ids))
        target = targets[app_id].casefold()
        argv = _read_argv(Path(entry.path) / "cmdline")
        if not argv:
            continue
        windows_path = argv[0]
        if windows_path.replace("\\", "/").rsplit("/", 1)[-1].strip().casefold() != target:
            continue
        host_path = _host_path_from_wine_z(windows_path)
        if host_path is None:
            continue
        if app_id in found and found[app_id] != host_path:
            ambiguous.add(app_id)
        found[app_id] = host_path
    return {app_id: path for app_id, path in found.items() if app_id not in ambiguous}


def _host_path_from_wine_z(windows_path: str) -> str | None:
    """`Z:\\home\\deck\\game.exe` as the absolute host path it names."""
    text = windows_path.strip().strip('"')
    # Wine writes a long-path prefix for some clients; it names the same file.
    if text.startswith("\\\\?\\"):
        text = text[4:]
    if len(text) < 3 or text[0] not in "Zz" or text[1] != ":" or text[2] not in "\\/":
        return None
    posix = "/" + text[3:].replace("\\", "/").lstrip("/")
    candidate = Path(posix)
    if not candidate.is_absolute() or _unsafe_text(posix, 4096):
        return None
    try:
        if candidate.is_symlink() or not candidate.is_file():
            return None
    except OSError:
        return None
    return str(candidate)


def observe_game_container(
    app_id: int, *, proc_root: Path = Path("/proc"), max_processes: int = MAX_SCANNED_PROCESSES
) -> GameContainerObservation:
    """Find the running Steam game for ``app_id`` in the live process table.

    The observation is used to *corroborate* an independently resolved
    compatibility prefix and to prove the game is actually running. It is never
    used as the source of the prefix path itself.

    This is the process-table walk both repeating paths run, so it is counted
    here once for every caller. Its time is inside its callers' time rather than
    beside it; see `poll_counters`.
    """
    with poll_counters.timed(poll_counters.PRIMITIVE_GAME_CONTAINER):
        return _observe_game_container(app_id, proc_root=proc_root, max_processes=max_processes)


def _observe_game_container(
    app_id: int, *, proc_root: Path = Path("/proc"), max_processes: int = MAX_SCANNED_PROCESSES
) -> GameContainerObservation:
    app_id = _app_id(app_id)
    if not proc_root.is_dir():
        return GameContainerObservation(app_id, False, (), None, None, None, None, None, (), (), (), (), (), (), 0, "process table is unavailable")
    wanted = str(app_id)
    pids: list[int] = []
    compat_paths: list[str] = []
    client_paths: list[str] = []
    prefixes: list[str] = []
    displays: list[str] = []
    tool_paths: list[str] = []
    windows_executables: list[str] = []
    seen_executables: set[str] = set()
    unreadable: list[int] = []
    launch_targets: list[str] = []
    scanned = 0
    try:
        entries = sorted(
            (entry for entry in os.scandir(proc_root) if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return GameContainerObservation(app_id, False, (), None, None, None, None, None, (), (), (), (), (), (), 0, "process table is unreadable")
    for entry in entries:
        if scanned >= max_processes:
            # An incomplete process-table observation must never be used as
            # evidence that the requested AppID is absent (or unambiguous).
            return GameContainerObservation(
                app_id, False, (), None, None, None, None, None, (), (), (), (), (), (), scanned,
                "process table exceeds the bounded scan limit",
                unreadable_pids=tuple(unreadable),
            )
        scanned += 1
        try:
            # procfs records report a synthetic zero size, so the regular-file
            # reader's bytes-read/size equality check rejects every one of them
            # and would silently hide every running game.
            raw = read_proc_bytes(Path(entry.path) / "environ", max_bytes=MAX_ENVIRON_BYTES)
        except (OSError, ValueError):
            raw = None
        if not raw:
            # Refused, not absent. Which app this process belongs to is unknown
            # rather than known to be a different one, and an absence proved by
            # skipping it is proved by not looking.
            #
            # Every one of them is kept, with no second bound of its own. The
            # walk is already bounded, and it refuses to report an absence at
            # all once it hits that bound, so this set is complete for every
            # observation that is allowed to prove one. A tighter bound here
            # would buy nothing and would reintroduce exactly the hole this
            # exists to close: a target sitting past the cap, skipped again,
            # absent again.
            unreadable.append(int(entry.name))
            continue
        environ = parse_environ(raw)
        if is_ce_decky_owned_environment(environ):
            continue
        if environ.get("SteamAppId") != wanted and environ.get("SteamGameId") != wanted:
            continue
        if len(pids) >= MAX_OBSERVED_PIDS:
            return GameContainerObservation(
                app_id, False, (), None, None, None, None, None, (), (), (), (), (), (), scanned,
                "the running game's matching process set exceeds the bounded PID limit",
                unreadable_pids=tuple(unreadable),
            )
        pids.append(int(entry.name))
        for name, sink in (
            ("STEAM_COMPAT_DATA_PATH", compat_paths),
            ("STEAM_COMPAT_CLIENT_INSTALL_PATH", client_paths),
            ("WINEPREFIX", prefixes),
            ("DISPLAY", displays),
        ):
            value = environ.get(name)
            if value and value not in sink:
                sink.append(value)
        for candidate in (environ.get("STEAM_COMPAT_TOOL_PATHS") or "").split(":"):
            if candidate.startswith("/") and candidate not in tool_paths and len(tool_paths) < 16:
                tool_paths.append(candidate)
        # A table rarely names the process it belongs to, and the launcher the
        # library entry points at is often not the executable that owns the
        # game's memory. Offer what this game is actually running instead of
        # asking a controller user to type a Windows basename blind. This is a
        # display candidate only; attach still resolves the target itself.
        argv = _read_argv(Path(entry.path) / "cmdline")
        executable = _windows_executable_basename(argv)
        if executable is not None:
            key = executable.casefold()
            if key not in seen_executables and len(windows_executables) < MAX_OBSERVED_WINDOWS_EXECUTABLES:
                seen_executables.add(key)
                windows_executables.append(executable)
        launch_target = _proton_launch_target(argv)
        if launch_target is not None and launch_target.casefold() not in {
            candidate.casefold() for candidate in launch_targets
        }:
            launch_targets.append(launch_target)
    if not pids:
        return GameContainerObservation(
            app_id, False, (), None, None, None, None, None, (), (), (), (), (), (), scanned, NO_MATCHING_PROCESS_REASON,
            unreadable_pids=tuple(unreadable),
        )
    conflicting = tuple(compat_paths) if len(compat_paths) > 1 else ()
    conflicting_clients = tuple(client_paths) if len(client_paths) > 1 else ()
    conflicting_prefixes = tuple(prefixes) if len(prefixes) > 1 else ()
    conflicting_displays = tuple(displays) if len(displays) > 1 else ()
    reason = None
    if conflicting:
        reason = "running processes report more than one compatibility data path"
    elif conflicting_clients:
        reason = "running processes report more than one Steam client install path"
    elif conflicting_prefixes:
        reason = "running processes report more than one Wine prefix"
    elif conflicting_displays:
        reason = "running processes report more than one X display"
    elif not compat_paths:
        reason = "the running game does not report a Steam compatibility data path"
    return GameContainerObservation(
        app_id=app_id,
        running=True,
        pids=tuple(pids),
        compat_data_path=compat_paths[0] if len(compat_paths) == 1 else None,
        steam_client_install_path=client_paths[0] if len(client_paths) == 1 else None,
        wine_prefix=prefixes[0] if len(prefixes) == 1 else None,
        display=displays[0] if len(displays) == 1 else None,
        launch_executable=launch_targets[0] if len(launch_targets) == 1 else None,
        compat_tool_paths=tuple(tool_paths),
        windows_executables=tuple(sorted(windows_executables, key=str.casefold)),
        conflicting_compat_data_paths=conflicting,
        conflicting_steam_client_install_paths=conflicting_clients,
        conflicting_wine_prefixes=conflicting_prefixes,
        conflicting_displays=conflicting_displays,
        unreadable_pids=tuple(unreadable),
        scanned=scanned,
        reason=reason,
    )


def resolve_compat_data(app_id: int, user_home: Path) -> CompatDataResolution:
    """Resolve exactly one existing ``compatdata/<AppID>`` directory or refuse.

    An ambiguous result (the same AppID present in two libraries) or an unsafe
    exact component is never narrowed down to one candidate; guessing a prefix
    would guess the identity of the environment CE is injected into.
    """
    app_id = _app_id(app_id)
    _, libraries = discover_steam_library_roots(user_home)
    candidates: list[str] = []
    unsafe: list[str] = []
    seen: set[str] = set()
    for library in libraries:
        steamapps = library / "steamapps"
        compatdata = steamapps / "compatdata"
        app_root = compatdata / str(app_id)
        prefix = app_root / "pfx"
        unsafe_component = next(
            (
                component
                for component in (steamapps, compatdata, app_root, prefix)
                if _unsafe_exact_directory(component)
            ),
            None,
        )
        if unsafe_component is not None:
            unsafe.append(str(unsafe_component))
            continue
        if not prefix.is_dir():
            continue
        try:
            resolved = app_root.resolve(strict=True)
        except (OSError, RuntimeError):
            unsafe.append(str(app_root))
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            candidates.append(key)
    if unsafe:
        state = "unsafe"
    elif len(candidates) == 1:
        state = "resolved"
    elif not candidates:
        state = "missing"
    else:
        state = "ambiguous"
    return CompatDataResolution(
        app_id=app_id,
        state=state,
        compat_data_path=candidates[0] if state == "resolved" else None,
        candidates=tuple(candidates),
        unsafe_paths=tuple(unsafe),
    )


def match_observed_proton(
    observation: GameContainerObservation, tools: list[ProtonTool]
) -> tuple[ProtonTool | None, str | None]:
    """Identify the exact installed Proton tool the running game is using.

    Steam exports ``STEAM_COMPAT_TOOL_PATHS`` into the game environment, so the
    running tool can be *observed* instead of guessed. Anything other than one
    unambiguous match returns a reason and no tool.
    """
    if not observation.running:
        return None, observation.reason or "the selected game is not running"
    if not observation.compat_tool_paths:
        return None, "the running game does not expose its compatibility tool paths"
    observed = {_real_path(item) for item in observation.compat_tool_paths}
    matches = [tool for tool in tools if _real_path(tool.path) in observed]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        # A native Linux game runs under the Steam Linux Runtime, which is a
        # compatibility tool but not a Proton one, so it lands here looking like
        # a missing Proton install. It is not: there is no Windows process for
        # Cheat Engine to attach to, and no Proton would fix that. Say what the
        # user can actually do instead. The evidence for it is the absence of
        # both a reported Wine prefix and any Windows process in the game's own
        # process group; a Proton game whose tool CE Decky cannot resolve still
        # has those.
        if not observation.wine_prefix and not observation.windows_executables:
            return None, (
                "this game is running as a native Linux build rather than under Proton, so it has no "
                "Windows process to attach to. Force a Proton compatibility tool for it in Steam, which "
                "installs the game's Windows build, and start it again"
            )
        return None, "no installed Proton tool matches the running game's compatibility tool paths"
    return None, "more than one installed Proton tool matches the running game's compatibility tool paths"


def prepare_self_test_session(
    root: Path, *, ce_sha256: str, target_process: str, boundary: Path,
    table_bytes: bytes | None = None,
) -> SelfTestSession:
    """Write synthetic descriptor/control/table files for a no-game launch.

    `table_bytes` replaces the synthetic table with an exact one. The bridge
    loads the session table in its bootstrap, before the attach and without
    depending on a target process, so a self-test launch answers whether this
    exact Cheat Engine will open this exact `.CT` with no game, no profile and
    no consent involved. Nothing in the product passes it; it is how a
    development probe asks that question, and the default is the byte-identical
    behaviour every other caller already has.
    """
    ce_sha256 = _sha(ce_sha256)
    if not _process_basename(target_process):
        raise ValueError("self-test target process must be a bounded .exe basename")
    table = SELF_TEST_TABLE if table_bytes is None else bytes(table_bytes)
    if not table:
        raise ValueError("self-test table must not be empty")
    session_id = str(uuid.uuid4())
    session_root = root / session_id
    _reset_owned_directory(session_root, boundary)
    table_path = session_root / "table.ct"
    control_path = session_root / "control.txt"
    status_path = session_root / "status.txt"
    descriptor_path = session_root / "descriptor.txt"
    atomic_write_bytes(table_path, table, mode=0o400)
    atomic_write_bytes(control_path, (CONTROL_HEADER + "\n").encode("utf-8"))
    descriptor = SessionDescriptor(
        session_id=session_id,
        app_id=SELF_TEST_APP_ID,
        ce_sha256=ce_sha256,
        table_sha256=sha256(table).hexdigest(),
        table_path=wine_z_path(table_path),
        target_process=target_process,
        control_path=wine_z_path(control_path),
        status_path=wine_z_path(status_path),
        startup=(),
        is_shortcut=False,
    )
    descriptor_bytes = render_descriptor(descriptor)
    atomic_write_bytes(descriptor_path, descriptor_bytes)
    return SelfTestSession(
        session_id=session_id,
        root=str(session_root),
        descriptor_path=str(descriptor_path),
        descriptor_sha256=sha256(descriptor_bytes).hexdigest(),
        descriptor_md5=md5(descriptor_bytes).hexdigest(),
        descriptor_windows_path=wine_z_path(descriptor_path),
        status_path=str(status_path),
        table_path=str(table_path),
        table_windows_path=wine_z_path(table_path),
        table_sha256=descriptor.table_sha256,
        target_process=target_process,
    )


def owned_process_matches(
    pid: int,
    descriptor_windows_path: str,
    descriptor_sha256: str,
    *,
    proc_root: Path = Path("/proc"),
) -> bool:
    """Prove that one live PID is still the Cheat Engine launch CE Decky started.

    A recorded PID alone is not ownership: PIDs are reused. The launcher writes
    the exact descriptor identity into the process environment, so the same
    values must still be readable there before CE Decky signals anything.
    """
    return owned_process_state(
        pid, descriptor_windows_path, descriptor_sha256, proc_root=proc_root
    ) == "matched"


def game_process_state(pid: int, app_id: int, *, proc_root: Path = Path("/proc")) -> str:
    """Whether one baseline PID still proves the original Steam app is alive.

    Counted per call rather than per tick. The supervision loop runs once a
    second and calls this once for every PID recorded at launch, of which a game
    under Proton leaves dozens, so what this costs is a question about the
    number of PIDs and not about the cadence.
    """
    with poll_counters.timed(poll_counters.SUPERVISOR_BASELINE_CHECK):
        return _game_process_state(pid, app_id, proc_root=proc_root)


def _game_process_state(pid: int, app_id: int, *, proc_root: Path = Path("/proc")) -> str:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return "gone_or_reused"
    wanted = str(_app_id(app_id))
    process_root = proc_root / str(pid)
    try:
        raw = read_proc_bytes(process_root / "environ", max_bytes=MAX_ENVIRON_BYTES)
    except (OSError, ValueError):
        return "unreadable"
    if raw is None:
        try:
            return "unreadable" if process_root.exists() else "gone_or_reused"
        except OSError:
            return "unreadable"
    if not raw:
        return "unreadable"
    environ = parse_environ(raw)
    return "matched" if environ.get("SteamAppId") == wanted or environ.get("SteamGameId") == wanted else "gone_or_reused"


def baseline_is_gone(pids: Collection[int], app_id: int, *, proc_root: Path = Path("/proc")) -> bool:
    """Whether nothing recorded at launch still proves the original app is alive.

    The answer is the same as testing every PID and asking whether none of them
    came back `matched` or `unreadable`, and it is reached by stopping at the
    first one that does. That is not a shortcut around the semantics, it is the
    semantics: one live PID settles it, and reading the rest cannot change the
    result.

    The baseline is taken as a collection rather than an iterable, and is walked
    at most once: membership and traversal both read it, so a one-shot generator
    would be exhausted by the first and leave the second seeing an empty
    baseline, which reads as the game being gone. Nothing in production passes
    one, and this refuses to be the kind of function where that would matter.

    It was worth finding. The loop that calls this runs once a second and used
    to read every PID every time, and a game under Proton leaves about twenty of
    them, so the check cost roughly twenty process-environment reads a second to
    answer a question the first read usually answers. On the two machines
    measured on 2026-09-07 that was 2.0 points of one core on a Steam Machine
    and 5.6 on a Steam Deck, which was 61% of everything this plugin's backend
    spent while a session was live.

    An unreadable PID counts as alive on purpose, as it always did: procfs
    denies a same-user non-dumpable process, and reading that as gone would
    misclassify a live game.
    """
    return baseline_liveness(pids, app_id, proc_root=proc_root)[0]


def baseline_liveness(
    pids: Collection[int], app_id: int, *, prefer: int | None = None, proc_root: Path = Path("/proc")
) -> tuple[bool, int | None]:
    """As `baseline_is_gone`, and also which PID proved it, so the next tick can start there.

    Stopping at the first live PID only helps if a live one comes early, and
    nothing about the order says it will. The live supervisor holds its baseline
    as a set, so the order is arbitrary, and the recovered supervisor holds it
    sorted by PID, where the low numbers are the launcher and bootstrap
    processes that Steam's handoff retires first. Either way the survivor can
    sit at the end and the tick reads everything before it, every second, to
    reach a PID that answered the same way a second ago.

    So the caller keeps whichever PID last proved liveness and offers it back
    here. It is a hint about order and nothing else: a PID is only tried when it
    is still one of this session's own baseline PIDs, the predicate is
    order-independent, and a hint that no longer proves anything falls through
    to the ordinary traversal.
    """
    if prefer is not None and prefer in pids:
        if game_process_state(prefer, app_id, proc_root=proc_root) in ALIVE_BASELINE_STATES:
            return False, prefer
    for pid in pids:
        if pid == prefer:
            continue
        if game_process_state(pid, app_id, proc_root=proc_root) in ALIVE_BASELINE_STATES:
            return False, pid
    return True, None


def scan_is_due(
    *, tick: int, baseline_gone: bool, baseline_was_gone: bool, target_seen: bool,
    interval_ticks: int = TARGET_SCAN_INTERVAL_TICKS,
) -> bool:
    """Whether this tick runs the full target scan rather than waiting for the interval.

    A baseline that has gone is a reason to look now rather than at the next
    interval, and that is what this used to say. What it did not say is how long
    that stays true. A launch prepared while the game was still bootstrapping
    records the launcher's PIDs as its baseline, and Steam's handoff retires
    every one of them, so `baseline_gone` becomes true and stays true for the
    rest of a perfectly ordinary session, and the scan that is the most
    expensive thing this backend does ran every second instead of every three
    for all of it.

    So a gone baseline is urgent while it is news, and while the target has
    never been positively seen and the session is still converging on what is
    actually running. Once the target has been seen, a baseline that is merely
    still gone is not news, and the ordinary interval applies again.

    Nothing here can end a launch. Only a complete scan returning `absent` may
    do that, and this only decides when a scan happens.
    """
    if baseline_gone and not baseline_was_gone:
        return True
    if baseline_gone and not target_seen:
        return True
    return tick % interval_ticks == 0


def target_exit_is_proved(*, state: str, target_seen: bool, container_gone: bool) -> bool:
    """Whether a complete scan has proved this session's game is over.

    One rule, in one place, because there are two supervisors that have to
    agree with each other and with `scan_is_due`, and they did not: both read a
    disappeared launch-time PID set as proof, which is the state `scan_is_due`
    calls an ordinary handoff and keeps scanning through. So a session prepared
    while the launcher was still the only thing running had its Cheat Engine
    stopped the moment that launcher retired, before the game it was waiting
    for had ever started.

    Only a complete scan reaching `absent` can prove anything here, and then
    only in two situations. The target was seen alive earlier and is not there
    now, which is the game's process exiting. Or nothing on the machine reports
    this AppID at all, which is the game being gone whether or not this session
    ever saw its target: there is no longer anything for the target to appear
    in. Everything else, an ambiguous scan included, is a session still
    converging, and the answer is to keep waiting.
    """
    if state != "absent":
        return False
    return target_seen or container_gone


def game_target_state(
    app_id: int, target_process: str, *, proc_root: Path = Path("/proc"), max_processes: int = MAX_SCANNED_PROCESSES
) -> str:
    """Whether the game's own target process is still running.

    Baseline PIDs cannot answer this. Cheat Engine is started inside the game's
    own compatibility prefix - it has to be, or it could not read that game's
    memory - so it is a client of the same ``wineserver``. The Wine session and
    the Proton chain above it therefore survive the game's exit for exactly as
    long as Cheat Engine does, and every one of those processes inherits the
    game's ``SteamAppId``. Waiting for that whole set to disappear is a wait for
    something Cheat Engine itself is preventing, and Steam waits on the same
    chain, which is what left the console on "shutting down" indefinitely.

    The container observation already excludes CE Decky's own processes, so the
    executable it reports is the game's. ``unknown`` is returned for every scan
    that could not prove absence; only ``absent`` may end a launch.
    """
    with poll_counters.timed(poll_counters.SUPERVISOR_TARGET_SCAN):
        return _game_target_state(app_id, target_process, proc_root=proc_root, max_processes=max_processes)


def game_target_states(
    app_id: int,
    target_processes: Sequence[str],
    *,
    proc_root: Path = Path("/proc"),
    max_processes: int = MAX_SCANNED_PROCESSES,
) -> dict[str, str]:
    """The state of several executables, read from one walk.

    A session is not pointed at one executable for its whole life. A retried
    attach moves the bridge to another program, and whatever was patched before
    that move is still in the one it left, so a caller asking whether the game
    this session changed has exited has as many questions as there are names.

    One observation answers all of them. Taking it once per name would pay the
    most expensive thing this backend does as many times over, and two walks
    taken a moment apart can disagree with each other - which for a caller that
    needs every name absent at once is the difference between an answer and a
    coincidence.
    """
    wanted = tuple(dict.fromkeys(target_processes))
    if not wanted:
        return {}
    with poll_counters.timed(poll_counters.SUPERVISOR_TARGET_SCAN):
        app_id = _app_id(app_id)
        # Each name is answered for itself, the same as it would be alone: a
        # name that is not an executable is unknown, and says nothing about the
        # others. A caller that needs them all gone reads that as not knowing,
        # which is what it is.
        if not any(_process_basename(name) for name in wanted):
            return {name: "unknown" for name in wanted}
        observation = observe_game_container(app_id, proc_root=proc_root, max_processes=max_processes)
        return {
            name: (_target_state_of(observation, name, proc_root) if _process_basename(name) else "unknown")
            for name in wanted
        }


def _game_target_state(
    app_id: int, target_process: str, *, proc_root: Path = Path("/proc"), max_processes: int = MAX_SCANNED_PROCESSES
) -> str:
    app_id = _app_id(app_id)
    if not _process_basename(target_process):
        return "unknown"
    observation = observe_game_container(app_id, proc_root=proc_root, max_processes=max_processes)
    return _target_state_of(observation, target_process, proc_root)


def _unreadable_target_candidate(
    observation: GameContainerObservation, target_process: str, proc_root: Path
) -> bool:
    """Whether a process the walk could not read is running the exact target.

    Only asked when the walk is about to report an absence, so the cost is paid
    on the rare answer rather than on every pass. The walk decides what a
    process belongs to by reading its environment, and procfs refuses that for a
    non-dumpable process of the same user, so a target in that state is skipped
    and its absence is proved by not having looked. Its command line is still
    readable, and a process running the exact executable this session is
    watching is precisely the evidence the walk threw away.

    A match here is not proof the game is alive. It is proof that the scan
    cannot say it is gone, which is the whole difference between `absent` and
    `unknown`.
    """
    wanted = target_process.casefold()
    for pid in observation.unreadable_pids:
        declared = _declared_windows_executable(pid, proc_root)
        if declared is not None and declared.casefold() == wanted:
            return True
    return False


def _target_state_of(
    observation: GameContainerObservation, target_process: str, proc_root: Path = Path("/proc")
) -> str:
    """The target's state according to one observation, without taking another.

    Split out so a caller that also wants the target's identity can derive both
    from the same walk. Taking a second observation for the identity paid the
    most expensive thing this backend does twice, and only the first of them was
    counted as a scan, which quietly understated the very path the identity
    exists to avoid.
    """
    if observation.running:
        wanted = target_process.casefold()
        if any(name.casefold() == wanted for name in observation.windows_executables):
            return "present"
        # A truncated executable list cannot prove the target is not in it.
        if len(observation.windows_executables) >= MAX_OBSERVED_WINDOWS_EXECUTABLES:
            return "unknown"
        return _absent_unless_unreadable(observation, target_process, proc_root)
    if observation.reason != NO_MATCHING_PROCESS_REASON:
        return "unknown"
    return _absent_unless_unreadable(observation, target_process, proc_root)


def _absent_unless_unreadable(
    observation: GameContainerObservation, target_process: str, proc_root: Path
) -> str:
    """`absent`, unless the walk skipped a process that is running the target."""
    if _unreadable_target_candidate(observation, target_process, proc_root):
        return "unknown"
    return "absent"


@dataclass(frozen=True)
class TargetIdentity:
    """The four facts that let one process stand for the session's game.

    None of them is decoration. The PID alone is reused. Start time defeats
    reuse, but start time and executable together still prove only that some
    process of that name is alive at that PID, and two titles running at once
    can share an executable name and a whole Proton chain. The AppID the
    process itself declares is what ties it back to this session's game.
    """

    pid: int
    start_time: int
    windows_executable: str
    app_id: int


def _process_start_time(pid: int, proc_root: Path) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``, counted from the last ``)``.

    The command name is parenthesised and may itself contain spaces and
    parentheses, so every field is counted from the end of it rather than by
    splitting the whole line.
    """
    try:
        raw = (proc_root / str(pid) / "stat").read_bytes()[:4096].decode("utf-8", "replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1:].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _declared_windows_executable(pid: int, proc_root: Path) -> str | None:
    return _windows_executable_basename(_read_argv(proc_root / str(pid) / "cmdline"))


def _declared_app_id_state(pid: int, app_id: int, proc_root: Path) -> str:
    """Whether this exact process says it belongs to this exact Steam app.

    Three answers, not two. `unreadable` is the one that matters: procfs denies
    the environment of a same-user non-dumpable process, and that is missing
    evidence rather than evidence of a mismatch. Folding it into `mismatched`
    is how a live game gets called gone.
    """
    try:
        raw = read_proc_bytes(proc_root / str(pid) / "environ", max_bytes=MAX_ENVIRON_BYTES)
    except (OSError, ValueError):
        return "unreadable"
    if not raw:
        return "unreadable"
    environ = parse_environ(raw)
    wanted = str(app_id)
    if environ.get("SteamAppId") == wanted or environ.get("SteamGameId") == wanted:
        return "matched"
    return "mismatched"


#: `confirmed` is the only answer that may stand in for a scan. `changed` says
#: the process at that PID is not the one this session saw, and the scan decides
#: what that means. `unreadable` says the process is still there and still looks
#: like the target, but procfs would not say which app it belongs to, and that
#: is missing evidence which the scan inherits rather than resolves.
TARGET_CONFIRMED = "confirmed"
TARGET_CHANGED = "changed"
TARGET_UNREADABLE = "unreadable"


def inspect_target_identity(identity: TargetIdentity, *, proc_root: Path = Path("/proc")) -> str:
    """Whether the process this session already saw is still that same process.

    Three small reads of one process, against a walk of the whole table. Only
    `confirmed` may stand in for the scan, so this can make a session cheaper
    and cannot make it call something present that is not.

    It answers `unreadable` separately from `changed` because the two lead
    somewhere different. A process whose environment procfs refuses is invisible
    to the complete scan for exactly the same reason it is invisible here, so an
    absence reported after this answer is the same missing evidence rather than
    a second opinion about it.
    """
    with poll_counters.timed(poll_counters.SUPERVISOR_TARGET_IDENTITY):
        if _process_start_time(identity.pid, proc_root) != identity.start_time:
            return TARGET_CHANGED
        declared = _declared_windows_executable(identity.pid, proc_root)
        if declared is None or declared.casefold() != identity.windows_executable.casefold():
            return TARGET_CHANGED
        state = _declared_app_id_state(identity.pid, identity.app_id, proc_root)
        # Three separate reads of one PID, and a PID is a name that can be
        # handed to a different process between any two of them. Reading the
        # start time again closes the transaction: if the process that answered
        # the last two reads is not the one that answered the first, this says
        # so instead of combining an old start time with a new process's
        # executable and app. Equal here means one process answered all four.
        if _process_start_time(identity.pid, proc_root) != identity.start_time:
            return TARGET_CHANGED
        if state == "matched":
            return TARGET_CONFIRMED
        return TARGET_UNREADABLE if state == "unreadable" else TARGET_CHANGED


def confirm_target_identity(identity: TargetIdentity, *, proc_root: Path = Path("/proc")) -> bool:
    """Whether `inspect_target_identity` confirms this identity outright."""
    return inspect_target_identity(identity, proc_root=proc_root) == TARGET_CONFIRMED


def resolve_target_identity(
    app_id: int, target_process: str, pids: Iterable[int], *, proc_root: Path = Path("/proc")
) -> TargetIdentity | None:
    """Which of the game's own processes is the target, after a scan found it.

    Only the AppID cohort the scan already collected is read, not the table, and
    only when a session has no confirmed identity yet, so this is once per
    session rather than once per tick. A target whose identity cannot be pinned
    to one process is simply not pinned: the session keeps scanning, which is
    what it did before any of this existed.
    """
    wanted = target_process.casefold()
    app_id = _app_id(app_id)
    for pid in pids:
        start_time = _process_start_time(pid, proc_root)
        if start_time is None:
            continue
        declared = _declared_windows_executable(pid, proc_root)
        if declared is None or declared.casefold() != wanted:
            continue
        # The PID came from an observation taken a moment ago, and a moment is
        # long enough for it to be reused. Everything this identity will later
        # be confirmed against is therefore checked now, against this exact
        # process, rather than trusted from the observation: the app it declares
        # as well as its executable, and the start time again at the end to
        # prove one process answered all of it.
        if _declared_app_id_state(pid, app_id, proc_root) != "matched":
            continue
        if _process_start_time(pid, proc_root) != start_time:
            continue
        return TargetIdentity(pid=pid, start_time=start_time, windows_executable=declared, app_id=app_id)
    return None


def target_liveness(
    app_id: int,
    target_process: str,
    *,
    known: TargetIdentity | None = None,
    proc_root: Path = Path("/proc"),
    max_processes: int = MAX_SCANNED_PROCESSES,
) -> tuple[str, TargetIdentity | None, bool]:
    """The target's state, the identity to confirm it by, and whether the game is gone.

    The process-table walk is the most expensive thing this backend does, and
    almost all of what it costs is reading an environment for every process on
    the machine to find one. Once that walk has found the target, the session
    holds what it found, and every tick after it asks the far cheaper question:
    is that same process still there.

    The boundary that makes this safe is one sentence. The cheap check may only
    ever answer `present`. Nothing may be called `absent` without a complete
    scan, because only the complete table can prove a process is not in it.

    The third answer is the difference between two absences that look identical
    from outside and mean opposite things. A target can be absent because the
    game is running and has not started that executable yet, which is what an
    ordinary launcher handoff looks like for as long as it takes; or because
    nothing on the machine reports this AppID at all, which is the game itself
    being gone. Only a complete walk that found no process for the app may say
    the second, so a truncated table, an unreadable one and a bounded PID set
    each say nothing here, exactly as they say nothing about `absent`.
    """
    inspection = None
    if known is not None:
        inspection = inspect_target_identity(known, proc_root=proc_root)
        if inspection == TARGET_CONFIRMED:
            return "present", known, False

    app_id = _app_id(app_id)
    if not _process_basename(target_process):
        return "unknown", None, False
    # One walk, feeding both answers. Taking a second for the identity paid the
    # most expensive path here twice, and counted only the first of them.
    with poll_counters.timed(poll_counters.SUPERVISOR_TARGET_SCAN):
        observation = observe_game_container(app_id, proc_root=proc_root, max_processes=max_processes)
        state = _target_state_of(observation, target_process, proc_root)
        identity = (
            resolve_target_identity(app_id, target_process, observation.pids, proc_root=proc_root)
            if state == "present" else None
        )

    if state == "present":
        return state, identity, False
    if inspection == TARGET_UNREADABLE:
        # The confirmation could not read the target's environment, and the scan
        # is blind in exactly the same way: it skips a process it cannot
        # attribute to an app. So nothing here may be called absent, and the
        # identity is kept rather than dropped, because the moment procfs
        # answers again the cheap confirmation settles it without another walk.
        return ("unknown" if state == "absent" else state), known, False
    # Read off the same walk, and only where that walk was allowed to prove an
    # absence at all: `state` is `absent` only for a complete table with no
    # unreadable process running the target, and the reason separates "walked
    # the whole table and nothing reports this app" from every bound and refusal
    # the walk reports instead.
    container_gone = (
        state == "absent"
        and not observation.running
        and observation.reason == NO_MATCHING_PROCESS_REASON
    )
    # Otherwise an identity is dropped the moment the scan stops confirming it,
    # so a stale one can never be offered back and re-confirmed by accident.
    return state, None, container_gone


def owned_process_state(
    pid: int,
    descriptor_windows_path: str,
    descriptor_sha256: str,
    *,
    proc_root: Path = Path("/proc"),
) -> str:
    """Classify recovery evidence without calling an unreadable PID gone.

    ``gone_or_reused`` proves the recorded process identity is no longer live;
    ``unreadable`` is ambiguous and must retain ownership rather than allowing a
    second launch. The boolean compatibility wrapper above is intentionally
    suitable only for positive identity checks.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return "gone_or_reused"
    process_root = proc_root / str(pid)
    try:
        raw = read_proc_bytes(process_root / "environ", max_bytes=MAX_ENVIRON_BYTES)
    except (OSError, ValueError):
        return "unreadable"
    if raw is None:
        try:
            return "unreadable" if process_root.exists() else "gone_or_reused"
        except OSError:
            return "unreadable"
    if not raw:
        return "unreadable"
    environ = parse_environ(raw)
    matches = (
        environ.get("CE_DECKY_DESCRIPTOR") == descriptor_windows_path
        and environ.get("CE_DECKY_DESCRIPTOR_SHA256") == descriptor_sha256.lower()
    )
    return "matched" if matches else "gone_or_reused"


def owned_process_group_state(
    pgid: int,
    descriptor_windows_path: str,
    descriptor_sha256: str,
    *,
    proc_root: Path = Path("/proc"),
) -> str:
    """Prove an owned group is gone only after every member is gone.

    Proton's leader may exit before Wine/CE.  A durable record is therefore
    resolved at group scope; any unreadable member is deliberately ambiguous.
    """
    if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
        return "unreadable"
    leader_state = owned_process_state(pgid, descriptor_windows_path, descriptor_sha256, proc_root=proc_root)
    if leader_state != "gone_or_reused":
        return leader_state
    if os.name != "posix":
        return leader_state
    try:
        entries = sorted((entry for entry in os.scandir(proc_root) if entry.name.isdigit()), key=lambda entry: int(entry.name))
    except OSError:
        return "unreadable"
    saw_member = False
    if len(entries) > MAX_SCANNED_PROCESSES:
        # The leader can have exited while a later Wine child remains.  Do not
        # clear durable ownership based on a prefix of the process table.
        return "unreadable"
    for entry in entries:
        process_root = Path(entry.path)
        try:
            stat_raw = read_proc_bytes(process_root / "stat", max_bytes=4096)
        except (OSError, ValueError):
            return "unreadable"
        if stat_raw is None:
            continue
        try:
            text = stat_raw.decode("ascii")
            fields = text[text.rfind(")") + 1:].split()
            member_pgid = int(fields[2])
        except (UnicodeDecodeError, ValueError, IndexError):
            return "unreadable"
        if member_pgid != pgid:
            continue
        saw_member = True
        state = owned_process_state(int(entry.name), descriptor_windows_path, descriptor_sha256, proc_root=proc_root)
        if state == "matched":
            return "matched"
        if state == "unreadable":
            return "unreadable"
    return "gone_or_reused" if not saw_member else "unreadable"


def owned_descriptor_pids(
    descriptor_windows_path: str,
    descriptor_sha256: str,
    *,
    proc_root: Path = Path("/proc"),
    max_processes: int = MAX_SCANNED_PROCESSES,
    expected_executable: str | None = None,
) -> tuple[str, tuple[int, ...]]:
    """Every live process carrying this exact descriptor identity, by scan.

    Process-group scope is not enough to prove a Cheat Engine is gone. Proton
    starts the launcher in its own POSIX session, but Wine clients can and do
    leave that group, and target evidence already showed one escaping. A stop
    that only signalled the recorded PGID could therefore report success while a
    matching Cheat Engine was still attached to the game, and switching tables
    would then start a second one beside it.

    The descriptor path plus its SHA-256 is the exact identity CE Decky wrote
    into that process environment, so it finds those escapees wherever they
    ended up. Returns ``("matched", pids)``, ``("gone", ())`` when a complete
    scan found none, or ``("unreadable", ...)`` when the scan itself could not
    be trusted - which must retain ownership rather than claim the process is
    gone.
    """
    if not descriptor_windows_path or not descriptor_sha256:
        return ("unreadable", ())
    digest = descriptor_sha256.lower()
    if not proc_root.is_dir():
        return ("unreadable", ())
    try:
        entries = sorted(
            (entry for entry in os.scandir(proc_root) if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return ("unreadable", ())
    if len(entries) > max_processes:
        # A prefix of the process table can never prove absence.
        return ("unreadable", ())
    matched: list[int] = []
    for entry in entries:
        environ_path = Path(entry.path) / "environ"
        # Linux applies a ptrace access check to `environ`; a same-UID child that
        # drops dumpability is unreadable without CAP_SYS_PTRACE. Permission
        # denial therefore cannot itself prove that the process is foreign. Use
        # the exact runtime executable plus still-readable proc metadata to skip
        # a positively unrelated process, and retain ownership for a possible
        # CE/Proton/Wine process or for evidence that cannot be classified.
        try:
            os.close(os.open(environ_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)))
        except PermissionError:
            denied = _denied_process_state(Path(entry.path), expected_executable)
            if denied == "gone" or denied == "foreign":
                continue
            return ("unreadable", (int(entry.name),))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return ("unreadable", ())
        try:
            raw = read_proc_bytes(environ_path, max_bytes=MAX_ENVIRON_BYTES)
        except (OSError, ValueError):
            # Readable a moment ago and not now: ambiguous unless it exited.
            if Path(entry.path).exists():
                return ("unreadable", ())
            continue
        if raw is None:
            continue
        if not raw:
            # A zombie or kernel thread has an empty environment and cannot be a
            # live Cheat Engine carrying our descriptor.
            continue
        environ = parse_environ(raw)
        if (
            environ.get("CE_DECKY_DESCRIPTOR") == descriptor_windows_path
            and (environ.get("CE_DECKY_DESCRIPTOR_SHA256") or "").lower() == digest
        ):
            matched.append(int(entry.name))
    if matched:
        return ("matched", tuple(matched))
    return ("gone", ())


def owned_descriptor_state(
    descriptor_windows_path: str,
    descriptor_sha256: str,
    *,
    proc_root: Path = Path("/proc"),
    expected_executable: str | None = None,
) -> str:
    """`matched`, `gone` or `unreadable` for one exact descriptor identity."""
    return owned_descriptor_pids(
        descriptor_windows_path,
        descriptor_sha256,
        proc_root=proc_root,
        expected_executable=expected_executable,
    )[0]


def _denied_process_state(process_root: Path, expected_executable: str | None) -> str:
    """Classify an environ-denied PID without treating EACCES as absence."""
    try:
        info = process_root.stat()
    except (FileNotFoundError, ProcessLookupError):
        return "gone"
    except OSError:
        return "unreadable"
    if info.st_uid != os.geteuid():
        return "foreign"

    evidence: list[str] = []
    for name in ("exe", "cwd"):
        try:
            evidence.append(os.readlink(process_root / name))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            pass
    try:
        command = read_proc_bytes(process_root / "cmdline", max_bytes=MAX_CMDLINE_BYTES)
    except (OSError, ValueError):
        command = None
    if command:
        evidence.extend(part.decode("utf-8", "replace") for part in command.split(b"\0") if part)
    try:
        comm = read_proc_bytes(process_root / "comm", max_bytes=4096)
    except (OSError, ValueError):
        comm = None
    if comm:
        evidence.append(comm.decode("utf-8", "replace").strip())
    if not evidence:
        try:
            return "gone" if not process_root.exists() else "unreadable"
        except OSError:
            return "unreadable"

    if expected_executable is not None:
        expected = str(Path(expected_executable).absolute())
        expected_parent = str(Path(expected).parent)
        expected_wine = wine_z_path(Path(expected))
        folded = [item.casefold() for item in evidence]
        if any(item in {expected.casefold(), expected_parent.casefold(), expected_wine.casefold()} for item in folded):
            return "unreadable"

    # Wine clients do not all preserve the Windows command path (wineserver is
    # the important example), but their readable native identity remains in
    # this small process class. Such a denied same-user PID is ambiguous; an
    # ordinary Steam/browser/kernel process with none of these markers is
    # positively foreign and does not make exact absence unreachable.
    possible_runtime_names = {
        "proton", "wine", "wine64", "wineserver", "wineboot",
        "wine-preloader", "wine64-preloader",
    }
    basenames = [Path(item).name.casefold() for item in evidence]
    if any(name in possible_runtime_names or "cheatengine" in name for name in basenames):
        return "unreadable"
    if any(item.casefold().endswith(".exe") for item in evidence):
        return "unreadable"
    return "foreign"


def owned_any_descriptor_pids(
    *,
    proc_root: Path = Path("/proc"),
    max_processes: int = MAX_SCANNED_PROCESSES,
) -> tuple[str, tuple[int, ...]]:
    """Find any process carrying a syntactically valid CE Decky descriptor.

    This deliberately has no exact executable fallback: it exists for repairing
    a malformed ownership record whose executable identity has been lost, so a
    same-user permission denial must remain ambiguous.
    """
    if not proc_root.is_dir():
        return ("unreadable", ())
    try:
        entries = sorted(
            (entry for entry in os.scandir(proc_root) if entry.name.isdigit()),
            key=lambda entry: int(entry.name),
        )
    except OSError:
        return ("unreadable", ())
    if len(entries) > max_processes:
        return ("unreadable", ())
    matched: list[int] = []
    for entry in entries:
        process_root = Path(entry.path)
        environ_path = process_root / "environ"
        try:
            os.close(os.open(environ_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)))
        except PermissionError:
            denied = _denied_process_state(process_root, None)
            if denied in {"gone", "foreign"}:
                continue
            return ("unreadable", (int(entry.name),))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError:
            return ("unreadable", ())
        try:
            raw = read_proc_bytes(environ_path, max_bytes=MAX_ENVIRON_BYTES)
        except (OSError, ValueError):
            if process_root.exists():
                return ("unreadable", ())
            continue
        if not raw:
            continue
        environ = parse_environ(raw)
        descriptor = environ.get("CE_DECKY_DESCRIPTOR")
        digest = (environ.get("CE_DECKY_DESCRIPTOR_SHA256") or "").lower()
        if (
            isinstance(descriptor, str)
            and descriptor.startswith("Z:\\")
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        ):
            matched.append(int(entry.name))
    return ("matched", tuple(matched)) if matched else ("gone", ())


def plan_ce_launch(
    *,
    mode: str,
    tool: ProtonTool,
    executable: Path,
    ce_sha256: str,
    compat_data_path: Path,
    steam_client_install_path: Path,
    descriptor_path: Path,
    descriptor_sha256: str,
    descriptor_md5: str,
    descriptor_windows_path: str,
    table_windows_path: str,
    table_sha256: str,
    session_id: str,
    app_id: int | None,
    display: str,
) -> CELaunchPlan:
    """Build the exact argv/environment for one owned Cheat Engine launch.

    Pure and deterministic: no filesystem mutation and no process start, so the
    complete launch contract stays verifiable off target.
    """
    if mode not in LAUNCH_MODES:
        raise ValueError("unsupported Cheat Engine launch mode")
    if mode == MODE_ATTACHED:
        if app_id is None:
            raise ValueError("attached launch requires an exact AppID")
        app_id = _app_id(app_id)
    elif app_id is not None:
        raise ValueError("self-test launch must not carry a library AppID")
    proton = Path(tool.path) / "proton"
    for path in (executable, compat_data_path, steam_client_install_path, descriptor_path, proton):
        if not path.is_absolute():
            raise ValueError("Cheat Engine launch paths must be absolute")
        if _unsafe_text(str(path), 4096):
            raise ValueError("Cheat Engine launch paths must be bounded printable text")
    if not table_windows_path.startswith("Z:\\") or not descriptor_windows_path.startswith("Z:\\"):
        raise ValueError("Cheat Engine launch requires bounded Wine Z: paths")
    windows_executable_path = wine_z_path(executable)
    for label, value in (
        ("executable", windows_executable_path),
        ("descriptor", descriptor_windows_path),
        ("table", table_windows_path),
    ):
        if len(value.encode("utf-16-le")) // 2 > MAX_WINDOWS_LAUNCH_PATH_UNITS:
            raise ValueError(f"Cheat Engine {label} path exceeds the Windows launch limit")
    descriptor_sha256 = _sha(descriptor_sha256)
    descriptor_md5 = _md5(descriptor_md5)
    ce_sha256 = _sha(ce_sha256)
    table_sha256 = _sha(table_sha256)
    steam_app_id = str(app_id) if app_id is not None else "0"
    if not display.startswith(":") or not display[1:].isdigit() or not (0 <= int(display[1:]) <= 65535):
        raise ValueError("Cheat Engine launch requires an exact Gamescope display")
    overrides = {
        "DISPLAY": display,
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(steam_client_install_path),
        "STEAM_COMPAT_DATA_PATH": str(compat_data_path),
        "SteamAppId": steam_app_id,
        "SteamGameId": steam_app_id,
        "PROTON_LOG": "0",
        # The resident bridge reads these from the Windows process environment.
        # Wine propagates the launcher environment, exactly as it does for the
        # PROTON_REMOTE_DEBUG_CMD sidecar.
        "CE_DECKY_DESCRIPTOR": descriptor_windows_path,
        "CE_DECKY_DESCRIPTOR_SHA256": descriptor_sha256,
        "CE_DECKY_DESCRIPTOR_MD5": descriptor_md5,
    }
    return CELaunchPlan(
        mode=mode,
        app_id=app_id,
        tool_id=tool.tool_id,
        tool_name=tool.name,
        tool_path=tool.path,
        proton_sha256=tool.proton_sha256,
        verb=LAUNCH_VERBS[mode],
        compat_data_path=str(compat_data_path),
        steam_client_install_path=str(steam_client_install_path),
        executable=str(executable),
        ce_sha256=ce_sha256,
        # The table is not named on the command line. Cheat Engine only opens
        # one from there once its main window is actually shown, and CE Decky
        # never lets that window map, because a mapped Cheat Engine window
        # takes the running game's audio and controller input with it. The
        # resident bridge loads the exact table from the descriptor instead,
        # which also keeps anything else from loading a second copy and
        # raising Cheat Engine's "merge tables?" prompt over the game.
        argv=(str(proton), LAUNCH_VERBS[mode], str(executable)),
        env_overrides=tuple(sorted(overrides.items())),
        descriptor_windows_path=descriptor_windows_path,
        descriptor_sha256=descriptor_sha256,
        table_windows_path=table_windows_path,
        table_sha256=table_sha256,
        session_id=_uuid_text(session_id),
    )


def launch_environment(plan: CELaunchPlan, base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the exact environment for ``plan`` from a sanitized base."""
    # Starts from `child_environment` for the same reason 7-Zip does: Decky's
    # loader is a bundle that points `LD_LIBRARY_PATH` at its own libraries, and
    # nothing this plugin starts is that bundle.
    env = child_environment(base_env)
    for name in tuple(env):
        if name.startswith(_STRIPPED_ENV_PREFIXES) or name in _STRIPPED_ENV_NAMES:
            env.pop(name, None)
    env.update(dict(plan.env_overrides))
    return env


def _remaining(budget: float, deadline: float | None) -> float:
    """This step's own bound, never more than the whole prologue has left."""
    if deadline is None:
        return budget
    return max(0.0, min(budget, deadline - time.monotonic()))


def _self_test_stop_verdict(group: str, prefix: str) -> str:
    """One answer for a stop that has two halves, and neither may be assumed.

    An empty process group is not proof that Cheat Engine is gone: this
    supervisor's ordinary stop is built around that, because a Wine client
    reaches a process group of its own under Proton. The prefix pass is what
    answers for those, so a stop whose group went but whose prefix could not be
    settled is reported as exactly that rather than as a stop.

    `absent` is settled: a prefix Proton never created holds nothing to retire.
    """
    group_gone = group in GROUP_GONE
    prefix_settled = prefix in {"retired", "absent"}
    if not group_gone:
        return group
    if not prefix_settled:
        return "group_gone_prefix_unproven"
    return "stopped"


class CELaunchSupervisor:
    """Own at most one Cheat Engine process per launch key.

    ``self_test`` runs a bounded transaction and always stops the process it
    started. ``attached`` keeps the process for the lifetime of the runtime
    session so the controller can stop it explicitly — which is what removes
    the "exit the game to change table" requirement.
    """

    def __init__(
        self,
        user_home: Path,
        ce_root: Path,
        state_root: Path,
        logger,
        *,
        display_resolver=resolve_game_mode_display,
        quiesce=None,
        session_target=None,
    ) -> None:
        self.user_home = user_home
        self.ce_root = ce_root
        self.state_root = state_root
        self.self_test_root = state_root / "ce-launch-self-test"
        self.launch_record_root = state_root / "ce-launch"
        self.logger = logger
        self._display_resolver = display_resolver
        # Asked to put the session's own records down before this stops Cheat
        # Engine. Owned by the service, because the session's control and status
        # files are the session store's rather than this launcher's, and called
        # through here so every route to a stop gets it rather than the one that
        # happened to remember. Absent in a launcher built without one, and the
        # stop is then exactly the stop it always was.
        self._quiesce = quiesce
        # Asked which executable the session is pointed at now, for the same
        # reason and from the same owner. A launch is started for the program
        # the profile names and both supervisors watch that program to know the
        # game is still there, but a retried attach moves the session to another
        # one and deliberately leaves the profile alone. Watching the name the
        # launch started with then stops Cheat Engine when the program the user
        # moved away from exits, in the middle of the game they moved to. Absent
        # in a launcher built without one, and the launch name is then all there
        # is, exactly as before.
        self._session_target = session_target
        self._operations: dict[str, dict[str, object]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._exits: dict[tuple[int | None, str], float] = {}
        self._recovered_supervision: dict[int, asyncio.Task[None]] = {}
        # "One Cheat Engine at a time" is a launcher-scope invariant, but the
        # service's reservations are keyed per AppID, so nothing serialized the
        # check against another game's start. Two requests could both observe no
        # owner and both publish one. This covers the whole check -> begin
        # transition, for attached launches and the self-test alike, because they
        # execute from the same shared private runtime tree.
        self._launch_exclusion = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._closing = False

    # -- introspection ---------------------------------------------------

    def capability(self, app_id: int | None = None) -> dict[str, object]:
        observation: dict[str, object] | None = None
        resolution: dict[str, object] | None = None
        if app_id is not None:
            # Steam-owned and kernel-owned state a Game Mode user cannot repair
            # must degrade this capability, never fail the RPC that reports it.
            try:
                observation = observe_game_container(app_id).public()
            except (OSError, RuntimeError, ValueError):
                observation = None
            try:
                resolution = resolve_compat_data(app_id, self.user_home).public()
            except (OSError, RuntimeError, ValueError):
                resolution = None
        recovered: dict[str, object] | None = None
        recovery_error: str | None = None
        if app_id is not None:
            try:
                recovered = self.recover_owned_launch(app_id)
            except ValueError as exc:
                recovery_error = _safe_status_text(str(exc), 512)
        return {
            "schema": 2,
            "modes": list(LAUNCH_MODES),
            "self_test_prefix": str(self.ce_root / "self-test-prefix"),
            "operations": self._operation_snapshot(),
            "game": observation,
            "compat_data": resolution,
            "recovered": recovered,
            # Changing the registered Cheat Engine is global, so the panel needs
            # launcher scope to gate those actions rather than the selected game.
            "owned_launch_owners": self.owned_launch_owners(),
            # The inventory answers "who owns one"; this answers "is that list
            # complete". Without it an unreadable record directory looked
            # owner-free to the panel while every backend mutation guard kept
            # treating the same ambiguity as owned, so the offered actions could
            # only fail and nothing said which invariant blocked them.
            "ownership_state_error": self.ownership_state_error(),
            "recovery_error": recovery_error,
        }

    def status(self, operation_id: str) -> dict[str, object]:
        with self._state_lock:
            operation = self._operations.get(_uuid_text(operation_id))
            if operation is None:
                raise ValueError("Cheat Engine launch operation does not exist")
            return self._public(operation)

    def current_for_app(self, app_id: int) -> dict[str, object] | None:
        app_id = _app_id(app_id)
        with self._state_lock:
            for operation in self._operations.values():
                if operation.get("app_id") == app_id and operation.get("state") in LIVE_LAUNCH_STATES:
                    return self._public(operation)
        return None

    def _assert_launcher_is_free(self, for_app_id: int | None) -> None:
        """One Cheat Engine at a time, across every game and the self-test.

        The private runtime is keyed by CE/tree/bridge identity, so it is one
        shared directory for all of them, and Cheat Engine writes into its own
        installation while it runs. Two processes executing from that tree in
        different prefixes is not a private runtime at all - and whether that
        happened or was refused used to depend on whether the first had dirtied
        the tree yet, so the behaviour flipped on timing.

        Called under `_launch_exclusion`, including after every await that could
        let another start slip in.
        """
        for owner in self.owned_launch_owners():
            if owner["app_id"] != for_app_id:
                raise ValueError(
                    f"Cheat Engine is already running for AppID {owner['app_id']}. "
                    "Select that game in CE Decky and press Stop CE, then start this one again."
                )
        # The inventory is a best-effort list built for the panel: it reports
        # the records it managed to read and carries what it could not read
        # separately. Enforcing the spawn invariant from that list alone meant
        # this boundary could allow a second Cheat Engine into the shared
        # private runtime on exactly the evidence under which every runtime and
        # identity mutation already refuses. Ownership that cannot be read is
        # not proof that nothing is running.
        ownership_error = self.ownership_state_error()
        if ownership_error is not None:
            raise ValueError(
                f"Cheat Engine ownership cannot be read ({ownership_error}), so CE Decky will not "
                "start another one. Advanced offers a repair for this under Launch ownership."
            )
        if self.has_live_owned_launch():
            raise ValueError(
                "A Cheat Engine CE Decky started has not been proven to have stopped, so it will "
                "not start another one. Press Stop CE on the panel; if nothing is running at all, "
                "reloading CE Decky from Decky's plugin list clears this."
            )
        if self._live_self_test() is not None and for_app_id is not None:
            raise ValueError("a Cheat Engine self-test is running; wait for it to finish")

    def owned_launch_owners(self, *, proc_root: Path = Path("/proc")) -> list[dict[str, object]]:
        """Which games currently own a Cheat Engine, at launcher scope.

        Changing the registered Cheat Engine is a global operation and the
        backend refuses it while any AppID owns a live one. The panel could only
        see the selected game, so a user who left Cheat Engine running for one
        game and then selected another was offered install, import and forget
        actions that could only fail, with nothing naming the game that actually
        held it.
        """
        owners: list[dict[str, object]] = []
        with self._state_lock:
            for operation in self._operations.values():
                app_id = operation.get("app_id")
                if operation.get("state") not in LIVE_LAUNCH_STATES:
                    continue
                if isinstance(app_id, int) and not isinstance(app_id, bool):
                    owners.append({"app_id": app_id, "state": operation.get("state"), "recovered": False})
        try:
            entries = list(self.launch_record_root.iterdir())
        except OSError:
            entries = []
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.suffix != ".json" or not entry.stem.isdigit():
                continue
            app_id = int(entry.stem)
            if any(owner["app_id"] == app_id for owner in owners):
                continue
            try:
                observed = self.inspect_owned_launch_record(app_id, proc_root=proc_root)
            except (OSError, ValueError):
                continue
            if observed["state"] in {"matched", "unreadable", "invalid"}:
                # `has_live_owned_launch()` treats an unreadable or malformed
                # record as owned, so the panel must show the same owner rather
                # than reporting the machine clear while the backend refuses.
                owners.append({"app_id": app_id, "state": observed["state"], "recovered": True})
        return owners

    def ownership_state_error(self, *, proc_root: Path = Path("/proc")) -> str | None:
        """Why the durable ownership inventory is not a complete answer, if it is not.

        `owned_launch_owners()` can only report the records it could read, and
        `has_live_owned_launch()` deliberately treats the same unreadable state
        as owned. This reports that disagreement so one uncertainty reaches the
        panel instead of an inventory that looks clear.
        """
        try:
            entries = list(self.launch_record_root.iterdir())
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"Cheat Engine ownership records could not be listed: {_safe_status_text(str(exc), 256)}"
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.suffix != ".json" or not entry.stem.isdigit():
                continue
            try:
                self.inspect_owned_launch_record(int(entry.stem), proc_root=proc_root)
            except (OSError, ValueError) as exc:
                return (
                    f"Cheat Engine ownership record for AppID {entry.stem} could not be read: "
                    f"{_safe_status_text(str(exc), 256)}"
                )
        return None

    def has_live_owned_launch(self, *, proc_root: Path = Path("/proc")) -> bool:
        """True unless no Cheat Engine process CE Decky owns can be running.

        Rebuilding the shared private runtime tree is only safe while nothing is
        executing out of it, and that tree is not per-game, so this asks the
        question at launcher scope instead of for one AppID.  Anything the
        record layer cannot read is treated as live.
        """
        with self._state_lock:
            for operation in self._operations.values():
                if operation.get("state") in LIVE_LAUNCH_STATES:
                    return True
            # A launcher this supervisor is still running keeps ownership on its
            # own authority; one that has exited does not.
            running = [
                operation_id for operation_id, process in self._processes.items()
                if process.returncode is None
            ]
            exited = [
                operation_id for operation_id, process in self._processes.items()
                if process.returncode is not None
            ]
        if running:
            return True
        # A stop whose cleanup could not prove the identity gone keeps its
        # entry, which is right at that moment and wrong forever after: the
        # doubt was about one instant, and holding it latched the whole
        # launcher, so every later start was refused with "stop it before
        # starting another Cheat Engine" for a Cheat Engine that had already
        # exited, with a plugin reload as the only way out. Ask the same
        # authority Stop uses, now, and let a proven absence release it.
        if exited:
            scan_state, _ = owned_any_descriptor_pids(proc_root=proc_root)
            if scan_state != "gone":
                return True
            with self._state_lock:
                for operation_id in exited:
                    process = self._processes.get(operation_id)
                    if process is not None and process.returncode is not None:
                        self._processes.pop(operation_id, None)
        try:
            entries = list(self.launch_record_root.iterdir())
        except FileNotFoundError:
            return False
        except OSError:
            # An unreadable ownership directory is not proof that it contains no
            # live launch records.  Runtime rebuild and removal both rely on this
            # probe, so ambiguity must retain ownership rather than opening a
            # shared-tree mutation window.
            return True
        for entry in entries:
            if entry.suffix != ".json" or not entry.stem.isdigit():
                continue
            try:
                observed = self.inspect_owned_launch_record(int(entry.stem), proc_root=proc_root)
            except ValueError:
                return True
            if observed["state"] not in {"absent", "gone_or_reused"}:
                return True
        return False

    def inspect_owned_launch_record(
        self, app_id: int, *, proc_root: Path = Path("/proc")
    ) -> dict[str, object]:
        """Read one durable attached-launch record without mutating recovery state.

        Target diagnostics need to distinguish an absent record from malformed,
        unreadable, live, and proven-gone ownership.  ``recover_owned_launch``
        intentionally clears a proven-gone record, so it cannot be used by a
        read-only evidence probe.  Keep the validation and process-identity
        classification in this production owner instead of duplicating the
        launch-record schema in target tooling.
        """
        app_id = _app_id(app_id)
        path = self._record_path(app_id)
        record = self._read_record(app_id)
        if record is None:
            state = "invalid" if path.exists() or path.is_symlink() else "absent"
            return {"state": state, "record": None}
        descriptor = str(record["descriptor_windows_path"])
        digest = str(record["descriptor_sha256"])
        state = owned_process_group_state(int(record["pgid"]), descriptor, digest, proc_root=proc_root)
        if state == "gone_or_reused":
            # The recorded group is where the launcher was put, not where its
            # Wine clients necessarily stay, and one was observed leaving it on
            # the target. Stop already proves absence by scanning for the exact
            # descriptor identity; the durable record has to use the same
            # authority or a reload would clear ownership of a Cheat Engine that
            # is still attached - after which a table switch starts a second one
            # beside it, which is the exact failure the descriptor sweep exists
            # to prevent. PGID stays as the cheap first answer; it can no longer
            # be the last one.
            descriptor_state = owned_descriptor_state(
                descriptor,
                digest,
                proc_root=proc_root,
                expected_executable=str(record["executable"]),
            )
            if descriptor_state == "matched":
                state = "matched"
            elif descriptor_state == "unreadable":
                state = "unreadable"
        return {"state": state, "record": dict(record)}

    def recover_owned_launch(self, app_id: int) -> dict[str, object] | None:
        """Rediscover a Cheat Engine process CE Decky started before a reload.

        Decky unload/update must not kill a Cheat Engine the user is actually
        using, so an attached launch outlives the plugin. The durable record
        plus exact descriptor identity in the live process environment is what
        keeps that process controllable afterwards instead of orphaned.
        """
        app_id = _app_id(app_id)
        observed = self.inspect_owned_launch_record(app_id)
        state = observed["state"]
        if state == "absent":
            return None
        if state == "invalid":
            raise ValueError(
                "the owned Cheat Engine launch record is present but invalid; refusing to guess whether its process is still running"
            )
        record = observed["record"]
        assert isinstance(record, dict)
        if state == "unreadable":
            raise ValueError(
                "the owned Cheat Engine process still exists but its exact identity cannot be re-proved; ownership was retained"
            )
        if state == "gone_or_reused":
            # The owned process is gone. Nothing can still be writing that
            # session's status file, so record the confirmed exit now and drop
            # the record; a newer status write keeps failing closed.
            with self._state_lock:
                self._exits[(app_id, str(record["session_id"]))] = time.time()
            self._clear_record(app_id, str(record["session_id"]))
            return None
        return dict(record)

    def repair_invalid_owned_launch_record(
        self, app_id: int, *, proc_root: Path = Path("/proc")
    ) -> dict[str, object]:
        """Quarantine malformed durable ownership only after global absence proof."""
        app_id = _app_id(app_id)
        path = self._record_path(app_id)
        with self._state_lock:
            observed = self.inspect_owned_launch_record(app_id, proc_root=proc_root)
            if observed["state"] != "invalid":
                raise ValueError("the owned Cheat Engine launch record is not malformed")
            if any(operation.get("state") in LIVE_LAUNCH_STATES for operation in self._operations.values()):
                raise ValueError("a Cheat Engine launch is active; stop it before repairing ownership state")
            # A supervisor still running keeps ownership on its own authority.
            # An exited process object does not: a stop whose cleanup could not
            # prove the identity gone retains its entry, and that retained entry
            # used to refuse the one action offered for repairing the ownership
            # this exact compound failure produces, leaving a plugin reload as
            # the only way out. Ordinary liveness already self-heals it from the
            # global descriptor scan, so repair asks the same authority below.
            if any(process.returncode is None for process in self._processes.values()):
                raise ValueError("a Cheat Engine process is still supervised; stop it before repairing ownership state")
            descriptor_state, pids = owned_any_descriptor_pids(proc_root=proc_root)
            if descriptor_state != "gone":
                detail = f" (candidate PID {pids[0]})" if pids else ""
                raise ValueError(
                    "a CE Decky descriptor-bearing process may still be running"
                    f"{detail}; refusing to discard malformed ownership"
                )
            for operation_id in [
                operation_id for operation_id, process in self._processes.items()
                if process.returncode is not None
            ]:
                self._processes.pop(operation_id, None)
            quarantine = self.launch_record_root / f"{app_id}.json.invalid-{uuid.uuid4().hex}"
            try:
                durable_rename(path, quarantine)
            except DurabilityUnknownError:
                raise
            except FileNotFoundError as exc:
                raise ValueError("the malformed ownership record disappeared; refresh and try again") from exc
            except OSError as exc:
                raise ValueError(f"failed to quarantine malformed ownership state: {exc}") from exc
            return {"discarded": True, "app_id": app_id, "quarantined": quarantine.name}

    def confirmed_exit_epoch(self, app_id: int | None, session_id: str) -> float | None:
        """When the owned Cheat Engine process for one exact session exited.

        ``None`` means CE Decky never started that session or cannot prove the
        process is gone; callers must then keep the ordinary stale-heartbeat
        rule.
        """
        with self._state_lock:
            return self._exits.get((app_id, session_id))

    # -- lifecycle -------------------------------------------------------

    async def start_self_test(
        self, tool: ProtonTool, executable: Path, ce_sha256: str, *, table_bytes: bytes | None = None
    ) -> dict[str, object]:
        async with self._launch_exclusion:
            return await self._start_self_test_locked(tool, executable, ce_sha256, table_bytes=table_bytes)

    async def _start_self_test_locked(
        self, tool: ProtonTool, executable: Path, ce_sha256: str, *, table_bytes: bytes | None = None
    ) -> dict[str, object]:
        self._assert_launcher_is_free(None)
        session = await drained_to_thread(
            prepare_self_test_session,
            self.self_test_root,
            ce_sha256=ce_sha256,
            target_process=executable.name,
            boundary=self.state_root,
            table_bytes=table_bytes,
        )
        prefix = self.ce_root / "self-test-prefix" / tool.tool_id
        # Keep a known-good owned prefix instead of recreating it per run: the
        # self-test uses Proton's `run` verb, which performs its own prefix
        # setup/upgrade, and discarding a working prefix would make every
        # verification needlessly slow.
        await drained_to_thread(_ensure_managed_directory, prefix, self.ce_root, create=True)
        # Two callers can finish their worker-thread preparation in either
        # order. Re-check after the await so only one Proton process can ever
        # mutate the shared self-test prefix at a time.
        if self._live_self_test() is not None:
            raise ValueError("a Cheat Engine self-test is already running")
        # Preparation awaited; an attached launch could have started meanwhile.
        self._assert_launcher_is_free(None)
        plan = plan_ce_launch(
            mode=MODE_SELF_TEST,
            tool=tool,
            executable=executable,
            ce_sha256=ce_sha256,
            compat_data_path=prefix,
            steam_client_install_path=_steam_root_for_tool(Path(tool.path), self.user_home),
            descriptor_path=Path(session.descriptor_path),
            descriptor_sha256=session.descriptor_sha256,
            descriptor_md5=session.descriptor_md5,
            descriptor_windows_path=session.descriptor_windows_path,
            table_windows_path=session.table_windows_path,
            table_sha256=session.table_sha256,
            session_id=session.session_id,
            app_id=None,
            display=self._display_resolver(self.user_home),
        )
        return self._begin(plan, Path(session.status_path), auto_stop=True)

    async def start_attached(
        self,
        tools: list[ProtonTool],
        executable: Path,
        *,
        app_id: int,
        tool_id: str | None,
        session_id: str,
        descriptor_path: Path,
        descriptor_sha256: str,
        descriptor_md5: str,
        descriptor_windows_path: str,
        table_windows_path: str,
        table_sha256: str,
        ce_sha256: str,
        status_path: Path,
        bridge_sha256: str = "",
        target_process: str = "",
    ) -> dict[str, object]:
        app_id = _app_id(app_id)
        async with self._launch_exclusion:
            return await self._start_attached_locked(
                tools, executable, app_id=app_id, tool_id=tool_id, session_id=session_id,
                descriptor_path=descriptor_path, descriptor_sha256=descriptor_sha256,
                descriptor_md5=descriptor_md5, descriptor_windows_path=descriptor_windows_path,
                table_windows_path=table_windows_path, table_sha256=table_sha256,
                ce_sha256=ce_sha256, status_path=status_path, bridge_sha256=bridge_sha256,
                target_process=target_process,
            )

    async def _start_attached_locked(
        self,
        tools: list[ProtonTool],
        executable: Path,
        *,
        app_id: int,
        tool_id: str | None,
        session_id: str,
        descriptor_path: Path,
        descriptor_sha256: str,
        descriptor_md5: str,
        descriptor_windows_path: str,
        table_windows_path: str,
        table_sha256: str,
        ce_sha256: str,
        status_path: Path,
        bridge_sha256: str = "",
        target_process: str = "",
    ) -> dict[str, object]:
        if self.current_for_app(app_id) is not None or self.recover_owned_launch(app_id) is not None:
            # Also covers a launch that outlived a plugin reload: two Cheat
            # Engine processes in one prefix would fight over the same session.
            raise ValueError("Cheat Engine is already running for this game; stop it before launching again")
        self._assert_launcher_is_free(app_id)
        observation = observe_game_container(app_id)
        if not observation.running:
            raise ValueError("the selected game is not running; start the game first")
        # The panel refuses this too, but a stale overlapping frontend or a
        # direct RPC reaches here without that guard, and this is the boundary
        # that actually spawns the process.
        anti_cheat = observed_anti_cheat(observation.windows_executables)
        if anti_cheat is not None:
            raise ValueError(
                f"the running game is using {anti_cheat}, a known anti-cheat; "
                "CE Decky is for offline and single-player use and will not attach to it"
            )
        observed_tool, tool_reason = match_observed_proton(observation, tools)
        if observed_tool is None:
            raise ValueError(tool_reason or "the running game's Proton identity could not be observed")
        if tool_id is not None:
            selected = next((item for item in tools if item.tool_id == tool_id), None)
            if selected is None:
                raise ValueError("the selected Proton tool is no longer installed")
            if observed_tool.tool_id != selected.tool_id:
                raise ValueError(
                    "the selected Proton tool is not the one the running game is using"
                )
        tool = observed_tool
        resolution = resolve_compat_data(app_id, self.user_home)
        if resolution.state != "resolved" or resolution.compat_data_path is None:
            raise ValueError(f"exact Steam compatibility prefix is not resolvable ({resolution.state})")
        if observation.compat_data_path is None:
            raise ValueError(observation.reason or "the running game does not expose a compatibility data path")
        if _real_path(observation.compat_data_path) != _real_path(resolution.compat_data_path):
            raise ValueError(
                "the running game's compatibility data path does not match the resolved Steam library prefix"
            )
        steam_client_install_path = _steam_root_for_tool(Path(tool.path), self.user_home)
        if (
            observation.steam_client_install_path is not None
            and _real_path(observation.steam_client_install_path) != _real_path(str(steam_client_install_path))
        ):
            raise ValueError(
                "the running game's Steam client path does not match the independently resolved Steam root"
            )
        expected_prefix = Path(resolution.compat_data_path) / "pfx"
        if observation.wine_prefix is None:
            raise ValueError(
                observation.reason or "the running game does not expose its Wine prefix"
            )
        if _real_path(observation.wine_prefix) != _real_path(str(expected_prefix)):
            raise ValueError("the running game's Wine prefix does not match its exact Steam compatibility prefix")
        plan = plan_ce_launch(
            mode=MODE_ATTACHED,
            tool=tool,
            executable=executable,
            ce_sha256=ce_sha256,
            compat_data_path=Path(resolution.compat_data_path),
            steam_client_install_path=steam_client_install_path,
            descriptor_path=descriptor_path,
            descriptor_sha256=descriptor_sha256,
            descriptor_md5=descriptor_md5,
            descriptor_windows_path=descriptor_windows_path,
            table_windows_path=table_windows_path,
            table_sha256=table_sha256,
            session_id=session_id,
            app_id=app_id,
            display=resolve_attached_display(observation, self.user_home),
        )
        return self._begin(
            plan, status_path, auto_stop=False, observation=observation, bridge_sha256=bridge_sha256,
            target_process=target_process,
        )

    async def stop(self, operation_id: str) -> dict[str, object]:
        operation_id = _uuid_text(operation_id)
        with self._state_lock:
            operation = self._operations.get(operation_id)
            task = self._tasks.get(operation_id)
        if operation is None:
            raise ValueError("Cheat Engine launch operation does not exist")
        if task is not None and not task.done():
            operation["stop_requested"] = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_process(operation_id)
        app_id = operation.get("app_id")
        self._clear_record(app_id if isinstance(app_id, int) and not isinstance(app_id, bool) else None, str(operation["session_id"]))
        if operation.get("state") in LIVE_LAUNCH_STATES:
            self._set(operation_id, state="stopped", message="Cheat Engine was stopped by CE Decky")
        return self._public(operation)

    async def stop_for_app(self, app_id: int, *, expected_session_id: str | None = None) -> dict[str, object]:
        app_id = _app_id(app_id)
        current = self.current_for_app(app_id)
        if current is not None:
            if expected_session_id is not None and current["session_id"] != expected_session_id:
                raise ValueError("owned session changed; refresh before revoking it")
            quiesced = await self._put_records_down(app_id)
            result = await self.stop(str(current["operation_id"]))
            return {"stopped": True, "operation": result, "recovered": False, "quiesce": quiesced}
        recovered = self.recover_owned_launch(app_id)
        if recovered is None:
            return {"stopped": False, "operation": None, "recovered": False, "quiesce": None}
        if expected_session_id is not None and recovered["session_id"] != expected_session_id:
            raise ValueError("owned session changed; refresh before revoking it")
        quiesced = await self._put_records_down(app_id)
        stopped = await drained_to_thread(self._terminate_recovered, recovered)
        if stopped:
            with self._state_lock:
                self._exits[(app_id, str(recovered["session_id"]))] = time.time()
            self._clear_record(app_id, str(recovered["session_id"]))
        return {"stopped": stopped, "operation": None, "recovered": True, "quiesce": quiesced}

    async def _put_records_down(self, app_id: int) -> dict[str, object] | None:
        """Ask the session to switch its own cheats off, and stop either way.

        This is the one thing the stop cannot do for itself: killing the owned
        process group means the table's `[DISABLE]` never runs, so the session's
        patches and its allocation stay in a game that keeps running, and no
        later session can undo them - that block's restore reads symbols
        belonging to a Cheat Engine that no longer exists.

        Every failure here is the stop's business to ignore. A bridge that is
        not answering has nothing to ask, a game that has already exited has
        nothing to put down, and a record that will not settle is reported and
        left. The stop must never become less reliable than the stop that does
        none of this, which is why this returns what happened instead of
        raising it.

        The unload path does not come through here, and must not: `begin_close`
        signals its own process group in its own thread because Decky starves
        this process's event loop and kills it five seconds later. Waiting for a
        bridge to answer is exactly the thing that cannot be done there.
        """
        if self._quiesce is None:
            return None
        try:
            return await drained_to_thread(self._quiesce, app_id)
        except Exception as exc:  # noqa: BLE001 - a stop is never blocked by this
            log_failure(self.logger, "runtime.quiesce_failed", exc, expected=True, app_id=app_id)
            # Nothing was established about the game, which is the one thing
            # this has to say: a stop that fell through here is exactly as
            # unknown as one whose answer never came, and an outcome carrying no
            # verdict at all reads on the panel as a clean stop.
            return {"asked": False, "reason": str(exc)[:256], "cleanup_confirmed": False}

    def _terminate_recovered(self, record: dict[str, object]) -> bool:
        """Stop an owned Cheat Engine and prove by descriptor that it is gone.

        The recorded process group is where the launcher was put, not where its
        Wine clients necessarily stay. Signalling only that group could leave a
        matching Cheat Engine attached to the game while this reported success,
        after which switching table started a second one beside the first.

        So the group is signalled first, because that is the ordinary well-behaved
        case, and then every remaining process carrying the exact descriptor
        identity is signalled individually. Success requires a *negative*
        descriptor proof from a complete scan; an unreadable scan retains
        ownership rather than guessing.
        """
        pgid = int(record["pgid"])
        descriptor = str(record["descriptor_windows_path"])
        digest = str(record["descriptor_sha256"])
        if os.name != "posix":
            return False
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            state, pids = owned_descriptor_pids(
                descriptor, digest, expected_executable=str(record["executable"])
            )
            if state == "gone":
                return True
            if state == "unreadable":
                return False
            try:
                os.killpg(pgid, signal_number)
            except (ProcessLookupError, PermissionError, OSError):
                # The group leader is already gone. Any survivor left the group,
                # which is exactly the case this scan exists for.
                pass
            for pid in pids:
                try:
                    os.kill(pid, signal_number)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
            deadline = time.monotonic() + STOP_GRACE_SECONDS
            while time.monotonic() < deadline:
                state, _ = owned_descriptor_pids(
                    descriptor, digest, expected_executable=str(record["executable"])
                )
                if state == "gone":
                    return True
                if state == "unreadable":
                    return False
                time.sleep(0.2)
        return owned_descriptor_state(
            descriptor, digest, expected_executable=str(record["executable"])
        ) == "gone"

    async def resume_all_recovered_supervision(self) -> list[int]:
        """Re-supervise every Cheat Engine that outlived the previous plugin load."""
        try:
            entries = list(self.launch_record_root.iterdir())
        except OSError:
            return []
        resumed: list[int] = []
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.suffix != ".json" or not entry.stem.isdigit():
                continue
            try:
                if await self.resume_recovered_supervision(int(entry.stem)):
                    resumed.append(int(entry.stem))
            except (OSError, ValueError):
                continue
        return resumed

    async def resume_recovered_supervision(self, app_id: int) -> bool:
        """Re-establish game-exit supervision for a Cheat Engine that survived a reload.

        An attached launch is deliberately allowed to outlive Decky so a plugin
        update does not kill the tool the user is playing with. Recovery proved
        ownership but started no monitor, so the process that would normally
        stop itself when the game exits could stay running indefinitely - and
        Home hides its Stop action once the game is gone and the context clears.
        Ownership must never quietly become unsupervised.
        """
        app_id = _app_id(app_id)
        with self._state_lock:
            existing = self._recovered_supervision.get(app_id)
            if existing is not None and not existing.done():
                return True
            if self._closing:
                return False
        try:
            record = self.recover_owned_launch(app_id)
        except ValueError:
            return False
        if record is None:
            return False
        baseline = tuple(int(pid) for pid in record.get("baseline_game_pids") or ())
        target = record.get("target_process")
        target = target if isinstance(target, str) and _process_basename(target) else ""
        if not baseline:
            # A record written before this build carries no baseline. Ownership
            # and Stop still work; there is simply nothing exact to supervise
            # against, and inventing a baseline now would count Cheat Engine's
            # own inherited SteamAppId as the game being alive.
            return False
        task = asyncio.create_task(self._supervise_recovered(app_id, record, baseline, target))
        with self._state_lock:
            self._recovered_supervision[app_id] = task
        return True

    async def _supervise_recovered(
        self, app_id: int, record: dict[str, object], baseline: tuple[int, ...], target_process: str = ""
    ) -> None:
        try:
            # The recovered process is the same kind of process the attached
            # launch supervises, and it holds the same Wine session open, so it
            # needs the same target-process rule. A record written before that
            # existed carries no target and keeps the baseline-only behaviour.
            # A record that already proved this target alive keeps that proof
            # across the reload; without it, recovered supervision had to see
            # the game again before it was allowed to notice the game leaving,
            # so a game that exited during the reload was never noticed at all.
            target_seen = record.get("target_seen") is True
            tick = 0
            # Whichever baseline PID last proved the game alive. An ordering
            # hint for the next tick and nothing else; see `baseline_liveness`.
            prover: int | None = None
            baseline_was_gone = False
            # The target this session has already seen, once a complete scan has
            # named it. Confirming it is three reads of one process against a
            # walk of the whole table; see `target_liveness`.
            identity: TargetIdentity | None = None
            logged_baseline: bool | None = None
            logged_target: tuple[str, int | None, bool] | None = None
            while True:
                await asyncio.sleep(RECOVERED_SUPERVISION_POLL_SECONDS)
                tick += 1
                with self._state_lock:
                    if self._closing:
                        return
                baseline_gone, prover = await asyncio.to_thread(
                    baseline_liveness, baseline, app_id, prefer=prover,
                )
                logged_baseline = self._log_baseline_change(app_id, baseline_gone, logged_baseline)
                if target_process:
                    # Launcher and bootstrap PIDs go away during an ordinary
                    # handoff, and the game arrives after them. Their absence is
                    # not the game leaving, so where there is an exact target it
                    # is that scan which decides; a gone baseline only makes the
                    # scan happen now instead of at the next interval.
                    due = scan_is_due(
                        tick=tick, baseline_gone=baseline_gone,
                        baseline_was_gone=baseline_was_gone, target_seen=target_seen,
                    )
                    baseline_was_gone = baseline_gone
                    if not due:
                        continue
                    watched = await asyncio.to_thread(
                        self._supervised_target, app_id, str(record.get("session_id") or ""), target_process,
                    )
                    if watched != target_process:
                        # The same rule as the attached loop, and the reload is
                        # where it is most easily got wrong: the record names
                        # the program the launch started with, and a retried
                        # attach after that never touched it.
                        target_process, target_seen, identity, logged_target = watched, False, None, None
                        log_activity(
                            self.logger, "info", "ce_launch.supervised_target_changed",
                            app_id=app_id, session=str(record.get("session_id") or "")[:12],
                        )
                    state, identity, container_gone = await asyncio.to_thread(
                        target_liveness, app_id, target_process, known=identity,
                    )
                    if state == "present" and not target_seen:
                        target_seen = True
                        await asyncio.to_thread(
                            self._mark_target_seen, app_id, str(record["session_id"]),
                        )
                    # After the fact it reports has settled, so the first sight
                    # of a target is recorded as the moment it became seen.
                    logged_target = self._log_target_change(
                        app_id, str(record.get("session_id") or ""), state, identity, target_seen, logged_target,
                    )
                    if state == "present":
                        continue
                    # A target that was seen and is now absent is the ordinary
                    # exit. A target that was never seen may only be ended by
                    # the game itself being gone, which is the complete walk
                    # finding no process for this AppID at all: the launch-time
                    # PID set disappearing is the handoff this loop exists to
                    # sit through, not the game leaving.
                    if not target_exit_is_proved(
                        state=state, target_seen=target_seen, container_gone=container_gone,
                    ):
                        continue
                elif not baseline_gone:
                    # A record from before target identity existed has nothing
                    # else to supervise against.
                    continue
                # The decision itself, which had no record of its own: a
                # recovered session stopping owned Cheat Engine is exactly what
                # a report asks about, and until now only its effect was visible.
                log_activity(
                    self.logger, "info", "ce_launch.recovered_stop_decided",
                    app_id=app_id, baseline_gone=baseline_gone,
                    target_process=bool(target_process), target_seen=target_seen,
                )
                stopped = await drained_to_thread(self._terminate_recovered, record)
                if stopped:
                    with self._state_lock:
                        self._exits[(app_id, str(record["session_id"]))] = time.time()
                    self._clear_record(app_id, str(record["session_id"]))
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervision must not take the backend down
            log_failure(self.logger, "ce_launch.recovered_supervision_failed", exc, expected=False)
        finally:
            with self._state_lock:
                if self._recovered_supervision.get(app_id) is asyncio.current_task():
                    self._recovered_supervision.pop(app_id, None)

    def _record_path(self, app_id: int) -> Path:
        return self.launch_record_root / f"{app_id}.json"

    def _read_record(self, app_id: int) -> dict[str, object] | None:
        try:
            raw = load_json(self._record_path(app_id), None, max_bytes=16 * 1024)
        except (OSError, ValueError):
            return None
        required = {
            "schema", "app_id", "session_id", "pid", "pgid", "tool_id", "executable",
            "descriptor_windows_path", "descriptor_sha256", "started_at",
        }
        if not isinstance(raw, dict):
            return None
        # Schema 2 added the game-liveness baseline. A schema 1 record was
        # written by a build that ran before this update and its process may
        # still be the user's Cheat Engine, so it stays recoverable and simply
        # cannot have its supervision re-established.
        schema = raw.get("schema")
        if schema == 1:
            if set(raw) != required:
                return None
            raw = {**raw, "baseline_game_pids": [], "bridge_sha256": "", "target_process": "", "target_seen": False}
        elif schema == 2:
            if set(raw) != required | {"baseline_game_pids"}:
                return None
            raw = {**raw, "bridge_sha256": "", "target_process": "", "target_seen": False}
        elif schema == 3:
            if set(raw) != required | {"baseline_game_pids", "bridge_sha256"}:
                return None
            raw = {**raw, "target_process": "", "target_seen": False}
        elif schema == 4:
            if set(raw) != required | {"baseline_game_pids", "bridge_sha256", "target_process"}:
                return None
            # A record from before durable target liveness: the supervisor has
            # to observe the target itself before it may act on absence.
            raw = {**raw, "target_seen": False}
        elif schema == 5:
            if set(raw) != required | {"baseline_game_pids", "bridge_sha256", "target_process", "target_seen"}:
                return None
        else:
            return None
        if not isinstance(raw.get("target_seen"), bool):
            return None
        target = raw.get("target_process")
        # Empty means a record from before this build: ownership and Stop still
        # work and supervision falls back to the baseline alone.
        if not isinstance(target, str) or (target and not _process_basename(target)):
            return None
        bridge_sha = raw.get("bridge_sha256")
        if not isinstance(bridge_sha, str):
            return None
        if bridge_sha:
            try:
                _sha(bridge_sha)
            except ValueError:
                return None
        baseline = raw.get("baseline_game_pids")
        if not isinstance(baseline, list) or len(baseline) > MAX_OBSERVED_PIDS:
            return None
        if any(isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 for pid in baseline):
            return None
        try:
            if _app_id(raw["app_id"]) != app_id or _uuid_text(raw["session_id"]) != raw["session_id"]:
                return None
            _sha(raw["descriptor_sha256"])
            _sha(raw["tool_id"])
        except (ValueError, TypeError):
            return None
        if any(isinstance(raw[key], bool) or not isinstance(raw[key], int) or raw[key] <= 1 for key in ("pid", "pgid")):
            return None
        if raw["pgid"] != raw["pid"]:
            return None
        if (
            not isinstance(raw["executable"], str)
            or not Path(raw["executable"]).is_absolute()
            or _unsafe_text(raw["executable"], 4096)
            or not isinstance(raw["descriptor_windows_path"], str)
            or not raw["descriptor_windows_path"].startswith("Z:\\")
            or _unsafe_text(raw["descriptor_windows_path"], 16384)
        ):
            return None
        if (
            isinstance(raw["started_at"], bool)
            or not isinstance(raw["started_at"], (int, float))
            or raw["started_at"] <= 0
        ):
            return None
        return raw

    def _write_record(
        self, plan: CELaunchPlan, pid: int, baseline_pids: tuple[int, ...] = (), bridge_sha256: str = "",
        target_process: str = "", target_seen: bool = False,
    ) -> None:
        if plan.app_id is None:
            return
        _ensure_managed_directory(self.launch_record_root, self.state_root, create=True)
        # An attached launch is intentionally allowed to outlive Decky. Without
        # this durable identity it would become an unowned process after reload,
        # so persistence failure must abort and retire the process instead of
        # merely logging a warning.
        # `baseline_game_pids` is what makes recovered ownership supervisable.
        # An attached launch stops itself when the game it was started for goes
        # away, and those are the only PIDs that count as game liveness because
        # Cheat Engine's own environment inherits the game's SteamAppId. Without
        # them in the record, a Decky reload silently downgraded a supervised
        # process to an unsupervised one that outlived its game indefinitely.
        # `bridge_sha256` is the resident bridge this exact process is running.
        # An attached Cheat Engine deliberately survives a plugin update, and the
        # runtime identity that binds the bridge lives in the runtime directory
        # name, not in anything the live session carries. Without this, a new
        # backend could recover a Cheat Engine running the *previous* bridge and
        # report it fully connected, and the frontend would then issue current
        # commands to an old resident protocol.
        # `target_process` is the exact process this session was prepared for.
        # Cheat Engine holds the game's Wine session open, so the baseline can
        # never empty while CE runs; the target is the one identity that can.
        # `target_seen` is the durable answer to "was this exact target ever
        # observed alive". Only a target that was seen may later be proved gone,
        # and that fact used to live in one supervisor's local variable: a game
        # that exited between two three-second scans - or a plugin reload - reset
        # it to false, after which absence was ignored forever and an owned Cheat
        # Engine outlived the game it was started for.
        with self._state_lock:
            atomic_write_json(self._record_path(plan.app_id), {
                "schema": 5,
                "app_id": plan.app_id,
                "session_id": plan.session_id,
                "pid": pid,
                "pgid": pid,
                "tool_id": plan.tool_id,
                "executable": plan.executable,
                "descriptor_windows_path": plan.descriptor_windows_path,
                "descriptor_sha256": plan.descriptor_sha256,
                "baseline_game_pids": sorted(baseline_pids)[:MAX_OBSERVED_PIDS],
                "bridge_sha256": bridge_sha256,
                "target_process": target_process if _process_basename(target_process) else "",
                "target_seen": bool(target_seen),
                "started_at": time.time(),
            })

    def _log_baseline_change(self, app_id: int | None, gone: bool, logged: bool | None) -> bool:
        """One line when the launch-time process set appears or disappears.

        Not once a tick. The supervision loop asks this question every second,
        and a line per answer would add work at the rate the answer is being
        measured at and flush the ring buffer the bundle carries. What a later
        report needs is the moment it changed, which is this.
        """
        if gone == logged:
            return gone
        log_activity(
            self.logger, "info", "ce_launch.baseline_changed",
            app_id=app_id, baseline_gone=gone,
        )
        return gone

    def _log_target_change(
        self,
        app_id: int | None,
        session_id: str | None,
        state: str,
        identity: "TargetIdentity | None",
        target_seen: bool,
        logged: tuple[str, int | None, bool] | None,
    ) -> tuple[str, int | None, bool]:
        """One line whenever what this session believes about its target moves.

        Two questions from a bug report are answered here or nowhere. Cheat
        Engine outliving a game the user has quit is the `unknown` this reports,
        and Cheat Engine stopping mid-session is the `absent` that preceded it,
        and both turn on an identity that is otherwise invisible.

        `target_unreadable` is the one worth naming on its own. It means the
        target is still at its PID, with its start time and executable intact,
        and procfs would not say which app it belongs to, so neither the cheap
        confirmation nor the complete scan can see it and the session waits
        rather than concluding. That is the ambiguity a user experiences as
        Cheat Engine not stopping, and without this line nothing would say so.
        """
        # `target_seen` is part of the signature, not just a field on it. It is
        # the fact that decides whether an absence may end a launch at all, and
        # a key without it lets the line that first said `false` stand as the
        # record of a session where it became true a moment later.
        current = (state, identity.pid if identity is not None else None, target_seen)
        if current == logged:
            return current
        log_activity(
            self.logger, "info", "ce_launch.target_changed",
            app_id=app_id, session=(session_id or "")[:8] or None,
            target_state=state, target_pid=current[1], target_seen=target_seen,
            # The only state that keeps an identity is that ambiguity: every
            # other non-present answer drops it.
            target_unreadable=state != "present" and identity is not None,
        )
        return current

    def _supervised_target(self, app_id: int | None, session_id: str, watching: str) -> str:
        """The executable to watch this tick: where this session points now.

        Best effort, and never worse than what it replaces. A launcher with no
        session knowledge, a session that has been retired, one that belongs to
        another launch, state this could not read and an answer that is not a
        process name all keep the name already being watched, which is what
        both loops watched before this existed.
        """
        if app_id is None or self._session_target is None:
            return watching
        try:
            current = self._session_target(app_id, session_id)
        except Exception as exc:  # noqa: BLE001 - supervision is never ended by this
            log_failure(self.logger, "ce_launch.session_target_unreadable", exc, expected=True, app_id=app_id)
            return watching
        if isinstance(current, str) and _process_basename(current):
            return current
        return watching

    def _mark_target_seen(self, app_id: int | None, session_id: str) -> None:
        """Record durably that this exact session's target was observed alive.

        Best effort by design: failing to write it only costs the reload
        optimization, and it must never be able to end a healthy launch.
        """
        if app_id is None:
            return
        try:
            record = self._read_record(app_id)
            if record is None or str(record.get("session_id")) != session_id or record.get("target_seen") is True:
                return
            path = self._record_path(app_id)
            if path.is_symlink():
                return
            atomic_write_json(path, {**record, "schema": 5, "target_seen": True})
        except (OSError, ValueError) as exc:
            log_failure(self.logger, "ce_launch.target_seen_not_persisted", exc, expected=True, app_id=app_id)

    def _clear_record(self, app_id: int | None, expected_session_id: str | None = None) -> None:
        with self._state_lock:
            if app_id is None:
                return
            if expected_session_id is not None:
                record = self._read_record(app_id)
                if record is None or record["session_id"] != expected_session_id:
                    return
            path = self._record_path(app_id)
            if path.is_symlink():
                raise ValueError("owned Cheat Engine launch record is a symlink; refusing to clear corrupt ownership state")
            try:
                durable_unlink(path, missing_ok=True)
            except DurabilityUnknownError:
                raise
            except OSError as exc:
                raise ValueError(f"failed to clear owned Cheat Engine launch record: {exc}") from exc

    def begin_close(self) -> None:
        """Stop the owned Cheat Engine that must not outlive this plugin.

        A self-test is a transaction: the Cheat Engine it started is this
        plugin's and nobody else's, and it has to be stopped. `close()` does
        that properly, but everything in `close()` happens after an `await`, and
        `docs/FIELD_NOTES.md` records why that is not a place to put something
        which must happen: Decky's stop starves this process's event loop and
        SIGKILL arrives five seconds later, so nothing suspended runs again.

        So the whole stop happens here, in this thread, in the order the orderly
        one uses: a courtesy sweep of the owned prefix so Wine can exit cleanly,
        the process group with its own escalation, and then the sweep that
        actually decides. That last one is where the proof is, because Cheat
        Engine reaches a process group of its own under Proton and because a
        prefix asked about while Proton is still running answers for a moment
        that is already over. Doing either twice is free: signalling a group
        that is already gone finds nothing.

        Bounded as a whole rather than per operation, because the budget being
        spent is Decky's five seconds and everything after this waits on it. An
        attached launch is deliberately untouched: that Cheat Engine is the
        user's running tool and survives a plugin update on purpose.
        """
        with self._state_lock:
            self._closing = True
            owned = []
            for operation_id, process in self._processes.items():
                operation = self._operations.get(operation_id) or {}
                if operation.get("mode") != MODE_SELF_TEST:
                    continue
                plan = operation.get("plan")
                owned.append((operation_id, process.pid, plan if isinstance(plan, dict) else None))
        if not owned or os.name != "posix":
            return
        deadline = time.monotonic() + BEGIN_CLOSE_TOTAL_SECONDS
        for operation_id, pid, plan in owned:
            started = time.monotonic()
            # Before the signal, exactly as the orderly stop does it: shutting
            # the prefix down first lets Wine exit cleanly. Best effort and
            # nothing more, and this file already says why it cannot be the
            # answer: Proton is still running while it decides whether `pfx`
            # exists, so a cold prefix can appear immediately afterwards. Its
            # result is deliberately not kept.
            #
            # Half the remaining budget at most, so the pass that does decide
            # always has room left.
            if plan is not None:
                graceful = _remaining(BEGIN_CLOSE_PREFIX_SECONDS, deadline) / 2
                if graceful > 0:
                    self._retire_self_test_prefix_now(plan, graceful)
            group = self._stop_group_now(operation_id, pid, deadline)
            # The authoritative one, and the reason it is here rather than
            # above: only once the group is proven gone is there nothing left
            # that can create a Wine client behind this sweep's back. Cheat
            # Engine reaches a process group of its own under Proton, so this is
            # also what answers for the escapee the group stop cannot see.
            prefix = self._final_prefix_verdict(plan, group, deadline)
            log_activity(
                self.logger, "info", "ce_launch.self_test_stopped",
                operation=operation_id[:12], phase="begin_close",
                outcome=_self_test_stop_verdict(group, prefix), group=group, prefix=prefix,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

    def _final_prefix_verdict(self, plan: dict | None, group: str, deadline: float) -> str:
        """What the prefix says once the process group can no longer change it."""
        if plan is None:
            return "no_plan"
        if group not in GROUP_GONE:
            # The group is still there, so a sweep now proves nothing about what
            # it may still start. The group's own verdict is the answer.
            return "not_attempted"
        budget = _remaining(BEGIN_CLOSE_PREFIX_SECONDS, deadline)
        if budget <= 0:
            return "budget_spent"
        return self._retire_self_test_prefix_now(plan, budget)

    def _retire_self_test_prefix_now(self, plan: dict, budget: float = BEGIN_CLOSE_PREFIX_SECONDS) -> str:
        """Stop the Wine clients of one owned self-test prefix, without a loop."""
        try:
            tool = ProtonTool(
                str(plan["tool_id"]), str(plan["tool_name"]),
                str(plan["tool_path"]), str(plan["proton_sha256"]),
                "owned-self-test-stop",
            )
            compat_data = Path(str(plan["compat_data_path"]))
        except (KeyError, TypeError, ValueError):
            return "no_plan"
        try:
            return retire_owned_proton_prefix_now(tool, compat_data, self.ce_root, budget)
        except Exception as exc:  # noqa: BLE001 - a prologue never fails an unload
            log_failure(self.logger, "ce_launch.self_test_prefix_failed", exc, expected=True)
            return "failed"

    def _stop_group_now(self, operation_id: str, pid: int, deadline: float | None = None) -> str:
        """Stop one owned process group without an event loop, and say how it went.

        The signal alone is not the stop. This project's own supervisor records
        why: Wine children outlive the Proton leader, which is what the orderly
        path's escalation and its wait for the whole group to disappear exist
        for. A prologue that only sends `SIGTERM` and is then killed leaves
        whatever ignored it running, with no durable record naming it, because a
        self-test writes none.

        So the same escalation happens here, in this thread, out of syscalls:
        signal the group, wait a bounded moment for it to go, `SIGKILL` what is
        left, and confirm. `_process_group_exists` treats an unreadable answer
        as still there, so nothing here reports a stop it did not observe.

        The waiting blocks this thread on purpose. It is the unload's thread, on
        a host where nothing else in this process is going to run before the
        kill arrives, and the window it can spend is under a second of the five
        it has.
        """
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "already_gone"
        except (PermissionError, OSError) as exc:
            log_failure(
                self.logger, "ce_launch.self_test_signal_failed", exc,
                expected=True, operation=operation_id[:12],
            )
            return "signal_refused"
        if self._group_leaves(pid, _remaining(BEGIN_CLOSE_TERM_SECONDS, deadline)):
            return "stopped"
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            return "stopped"
        except (PermissionError, OSError) as exc:
            log_failure(
                self.logger, "ce_launch.self_test_kill_failed", exc,
                expected=True, operation=operation_id[:12],
            )
            return "kill_refused"
        return "killed" if self._group_leaves(pid, _remaining(BEGIN_CLOSE_KILL_SECONDS, deadline)) else "still_running"

    @staticmethod
    def _group_leaves(pid: int, budget: float) -> bool:
        """Whether the group is gone inside this window, asked by polling it."""
        deadline = time.monotonic() + budget
        while True:
            if not _process_group_exists(pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(BEGIN_CLOSE_POLL_SECONDS)

    async def close(self) -> None:
        """Retire bounded owned work without killing a Cheat Engine in use.

        A self-test is a transaction and is always stopped. An attached launch
        is the user's running tool: a Decky reload or plugin update must not
        end it mid-game, so its supervising task is released and the durable
        record keeps it controllable after the plugin loads again.
        """
        self._closing = True
        # What this owner holds, written before it waits on any of it. Decky
        # gives a plugin five seconds to stop and then sends SIGKILL, so a close
        # that does not return writes nothing at all and the record ends on
        # whatever came before it.
        log_activity(
            self.logger, "info", "ce_launch.close_started",
            supervision=len(self._recovered_supervision), operations=len(self._tasks),
            processes=len(self._processes),
        )
        # Recovered supervision is a monitor, not the process itself. Cancelling
        # it releases the watcher exactly the way an attached launch's own task
        # is released; the Cheat Engine it watches keeps running and the durable
        # record keeps it controllable after the plugin loads again.
        for supervision in list(self._recovered_supervision.values()):
            if not supervision.done():
                supervision.cancel()
        cancelled = False
        if self._recovered_supervision:
            watched = list(self._recovered_supervision.values())
            # Kept, like every other drain here. A plain `gather` used to raise
            # the cancellation straight out of this method; draining swallows it
            # by design, so it has to be carried to the end and re-raised there
            # or the caller is told this close was ordinary when it was not.
            cancelled = await drain_through_cancellation(
                asyncio.gather(*watched, return_exceptions=True),
                label="ce_launch.supervision", logger=self.logger, watching=watched,
            )
            self._recovered_supervision.clear()
        transactional = [
            operation_id
            for operation_id, operation in self._operations.items()
            if operation.get("mode") == MODE_SELF_TEST
        ]
        for operation_id in list(self._tasks):
            task = self._tasks.get(operation_id)
            if task is not None and not task.done():
                task.cancel()
        if self._tasks:
            watched = list(self._tasks.values())
            cancelled = await drain_through_cancellation(
                asyncio.gather(*watched, return_exceptions=True),
                label="ce_launch.operations", logger=self.logger, watching=watched,
            ) or cancelled
        failure: BaseException | None = None
        for operation_id in transactional:
            # A self-test is a transaction, so cancelling unload is not
            # permission to leave the Cheat Engine process CE Decky started
            # running with no durable record that can stop it later. Stopping
            # one owner must not skip the next either.
            stopping = asyncio.ensure_future(self._stop_process(operation_id))
            cancelled = await drain_through_cancellation(
                stopping, label="ce_launch.stop_self_test", logger=self.logger, watching=(stopping,),
            ) or cancelled
            if not stopping.cancelled() and stopping.exception() is not None and failure is None:
                failure = stopping.exception()
        self._processes.clear()
        self._tasks.clear()
        log_activity(
            self.logger, "info", "ce_launch.close_completed",
            self_tests_stopped=len(transactional), cancelled=cancelled, failed=failure is not None,
        )
        if cancelled:
            raise asyncio.CancelledError
        if failure is not None:
            raise failure

    # -- internals -------------------------------------------------------

    def _begin(
        self,
        plan: CELaunchPlan,
        status_path: Path,
        *,
        auto_stop: bool,
        observation: GameContainerObservation | None = None,
        bridge_sha256: str = "",
        target_process: str = "",
    ) -> dict[str, object]:
        if self._closing:
            raise ValueError("plugin is unloading")
        operation_id = str(uuid.uuid4())
        with self._state_lock:
            self._operations[operation_id] = {
            "operation_id": operation_id,
            "mode": plan.mode,
            "app_id": plan.app_id,
            "session_id": plan.session_id,
            "state": "starting",
            "message": "Starting Cheat Engine through the selected Proton tool",
            "error": None,
            "started_at": time.time(),
            "plan": plan.public(),
            "game": observation.public() if observation is not None else None,
            "bridge": None,
            "pid": None,
            "pgid": None,
            "exit_code": None,
            "log_tail": "",
            "stop_requested": False,
            }
            self._prune()
            self._tasks[operation_id] = asyncio.create_task(
                self._run(
                    operation_id, plan, status_path,
                    auto_stop=auto_stop, observation=observation, bridge_sha256=bridge_sha256,
                    target_process=target_process,
                )
            )
            log_activity(
                self.logger, "info", "ce_launch.started",
                operation=operation_id[:12], mode=plan.mode, app_id=plan.app_id,
                session=plan.session_id[:12], tool=plan.tool_id[:12],
                tool_name=plan.tool_name, proton_sha=plan.proton_sha256[:12],
                verb=plan.verb, compat_data=plan.compat_data_path,
                ce_sha=plan.ce_sha256[:12], table_sha=plan.table_sha256[:12],
                descriptor_sha=plan.descriptor_sha256[:12],
                target=target_process, auto_stop=auto_stop,
                display=dict(plan.env_overrides).get("DISPLAY"),
            )
            return self._public(self._operations[operation_id])

    async def _run(
        self,
        operation_id: str,
        plan: CELaunchPlan,
        status_path: Path,
        *,
        auto_stop: bool,
        observation: GameContainerObservation | None,
        bridge_sha256: str = "",
        target_process: str = "",
    ) -> None:
        retained = bytearray()
        log_path = None if auto_stop else self.launch_record_root / f"{plan.app_id}.log"
        try:
            # A reused prepared-session directory can retain an old status file.
            # Capture the boundary before spawning and accept only a heartbeat
            # written by this exact process lifetime.
            launch_epoch_ns = time.time_ns()
            # The game is normally already running when an attached launch
            # starts, so its exact target can be observed before the spawn.
            # Requiring the supervisor to see it for itself first - up to three
            # seconds later - is what let a game that exited in that window be
            # missed, after which absence was ignored for the rest of the
            # session and Cheat Engine outlived the game.
            target_seen_at_launch = bool(
                plan.app_id is not None
                and target_process
                and not auto_stop
                and await asyncio.to_thread(game_target_state, plan.app_id, target_process) == "present"
            )
            process = await self._spawn(plan, log_path)
            with self._state_lock:
                self._processes[operation_id] = process
            self._set(operation_id, pid=process.pid, pgid=process.pid)
            if not auto_stop:
                self.launch_record_root.mkdir(parents=True, exist_ok=True)
                self._write_record(
                    plan, process.pid,
                    frozenset(observation.pids) if observation is not None else frozenset(),
                    bridge_sha256, target_process, target_seen_at_launch,
                )
            self._set(operation_id, state="running", message="Cheat Engine process started")
            # Keep draining for the whole process lifetime. Proton and Wine
            # inherit this pipe, so an unread buffer would eventually block
            # Cheat Engine itself. An attached launch writes to an owned file
            # instead and has no pipe to drain.
            drain = (
                asyncio.create_task(_drain_output(process, retained))
                if process.stdout is not None
                else None
            )
            try:
                observed, starting = await self._await_bridge(process, status_path, plan, launch_epoch_ns)
                if observed is None:
                    if process.returncode is not None:
                        raise ValueError(
                            f"Cheat Engine exited with code {process.returncode} before the resident bridge connected"
                        )
                    if starting is not None:
                        raise ValueError(_describe_unfinished_bridge(starting))
                    raise ValueError("the resident Cheat Engine bridge did not report a heartbeat in time")
                self._set(
                    operation_id,
                    state="connected",
                    message="Cheat Engine is running and the resident bridge reported a heartbeat",
                    bridge=observed,
                )
                if auto_stop:
                    await self._stop_process(operation_id)
                    self._set(operation_id, state="stopped", message="Self-test finished and Cheat Engine was stopped")
                else:
                    # Attached launches stay owned until the controller stops
                    # them or the original game process set disappears.  CE's
                    # own environment inherits SteamAppId, so only PIDs seen
                    # before CE Decky spawned the owned group count as game
                    # liveness evidence.
                    baseline_pids = frozenset(observation.pids if observation is not None else ())
                    # The baseline alone cannot end an attached launch: Cheat
                    # Engine keeps the game's own Wine session alive, and every
                    # process in it reports the game's AppID. Watching the exact
                    # process the session was prepared for is what actually
                    # observes the game leaving.
                    target_seen = target_seen_at_launch
                    # A game normally starts through a launcher or a bootstrap
                    # process, and the PIDs observed at launch time are whatever
                    # was running at that instant. Every one of them can exit
                    # during an ordinary handoff while the game itself is only
                    # just arriving, so their disappearance is startup context,
                    # never proof the game left: it used to stop Cheat Engine
                    # over the game it was attached to. Where the session has an
                    # exact target to scan, only that scan may end the launch.
                    supervises_target = plan.app_id is not None and bool(target_process)
                    tick = 0
                    # Whichever baseline PID last proved the game alive. An
                    # ordering hint only; see `baseline_liveness`.
                    prover: int | None = None
                    baseline_was_gone = False
                    # See `target_liveness`: once a complete scan has named the
                    # target, later ticks confirm that one process instead.
                    identity: TargetIdentity | None = None
                    logged_baseline: bool | None = None
                    logged_target: tuple[str, int | None, bool] | None = None
                    while process.returncode is None:
                        await asyncio.sleep(1.0)
                        tick += 1
                        baseline_gone = False
                        if baseline_pids:
                            baseline_gone, prover = await asyncio.to_thread(
                                baseline_liveness, baseline_pids, plan.app_id or 0, prefer=prover,
                            )
                            logged_baseline = self._log_baseline_change(
                                plan.app_id, baseline_gone, logged_baseline,
                            )
                        if baseline_gone and not supervises_target:
                            # No target identity to fall back on: the baseline is
                            # all this session has, exactly as before.
                            self._set(operation_id, message="The original game process set exited; stopping owned Cheat Engine")
                            await self._stop_process(operation_id)
                            break
                        if not supervises_target:
                            continue
                        # A baseline that is gone is a reason to look now rather
                        # than a reason to stop, so it overrides the scan interval
                        # while that is news or while the target has never been
                        # seen; see `scan_is_due`.
                        due = scan_is_due(
                            tick=tick, baseline_gone=baseline_gone,
                            baseline_was_gone=baseline_was_gone, target_seen=target_seen,
                        )
                        baseline_was_gone = baseline_gone
                        if not due:
                            continue
                        watched = await asyncio.to_thread(
                            self._supervised_target, plan.app_id, plan.session_id, target_process,
                        )
                        if watched != target_process:
                            # The session was pointed at another program. What
                            # was learned about the one before it belongs to
                            # that one: the proof that a target was seen alive
                            # is what allows an absence to end the launch, so
                            # carrying it over would let the new name be proved
                            # gone before anything had looked for it once.
                            target_process, target_seen, identity, logged_target = watched, False, None, None
                            log_activity(
                                self.logger, "info", "ce_launch.supervised_target_changed",
                                app_id=plan.app_id, session=plan.session_id[:12],
                            )
                        state, identity, container_gone = await asyncio.to_thread(
                            target_liveness, plan.app_id, target_process, known=identity,
                        )
                        if state == "present" and not target_seen:
                            # Only a target that was actually seen may later be
                            # proved gone; a launch prepared before the game
                            # settled must not stop itself immediately. The fact
                            # is written down once, so a plugin reload does not
                            # have to observe the game a second time before it
                            # may act on the game being gone.
                            target_seen = True
                            await asyncio.to_thread(
                                self._mark_target_seen, plan.app_id, plan.session_id,
                            )
                        # After that has settled, so the first sight of a target
                        # is recorded as the moment it became seen.
                        logged_target = self._log_target_change(
                            plan.app_id, plan.session_id, state, identity, target_seen, logged_target,
                        )
                        if target_exit_is_proved(
                            state=state, target_seen=target_seen, container_gone=container_gone,
                        ):
                            # Absent alone ends a launch only for a target that
                            # was actually seen; a launch prepared before the
                            # game settled must not stop itself immediately.
                            #
                            # The other complete proof is the game being gone,
                            # and that is the walk finding no process reporting
                            # this AppID rather than the launch-time PID set
                            # having disappeared. Those PIDs are the launcher
                            # and the bootstrap, every one of them exits during
                            # an ordinary handoff, and the game arrives after
                            # them under PIDs no frozen set can contain: reading
                            # their absence as the game leaving stopped Cheat
                            # Engine in the middle of the startup this loop is
                            # supposed to wait out, on precisely the sessions
                            # whose target had not appeared yet.
                            self._set(operation_id, message=(
                                "The game's target process exited; stopping owned Cheat Engine"
                                if target_seen else
                                "The game is no longer running; stopping owned Cheat Engine"
                            ))
                            await self._stop_process(operation_id)
                            break
                    if process.returncode is None:
                        await process.wait()
                    await self._stop_process(operation_id)
                    self._clear_record(plan.app_id, plan.session_id)
                    self._set(operation_id, state="stopped", message="Cheat Engine exited")
            finally:
                if drain is not None:
                    drain.cancel()
                    await asyncio.gather(drain, return_exceptions=True)
        except asyncio.CancelledError:
            with self._state_lock:
                operation = self._operations.get(operation_id)
            if self._closing and plan.mode == MODE_ATTACHED:
                # Plugin unload or update. The user's Cheat Engine keeps
                # running; the durable record keeps it controllable after the
                # next load instead of leaving it orphaned.
                with self._state_lock:
                    self._processes.pop(operation_id, None)
                self._set(operation_id, message="Cheat Engine keeps running after CE Decky unloaded")
            else:
                try:
                    await self._stop_process(operation_id)
                except Exception as exc:
                    log_failure(
                        self.logger, "ce_launch.cancel_cleanup_failed", exc,
                        expected=isinstance(exc, (OSError, ValueError, RuntimeError)),
                        operation=operation_id[:12], mode=plan.mode, app_id=plan.app_id,
                    )
                    self._set(
                        operation_id,
                        state="failed",
                        message="Cheat Engine cancellation cleanup failed",
                        error=_safe_status_text(str(exc), 512),
                    )
                    return
                if operation is not None and not operation.get("stop_requested"):
                    self._set(operation_id, state="cancelled", message="Cheat Engine launch was cancelled")
            log_activity(
                self.logger, "info", "ce_launch.cancelled",
                operation=operation_id[:12], mode=plan.mode, app_id=plan.app_id,
                unloading=self._closing,
            )
            raise
        except Exception as exc:
            cleanup_error: str | None = None
            try:
                await self._stop_process(operation_id)
            except Exception as stop_exc:
                cleanup_error = _safe_status_text(str(stop_exc), 256)
                log_failure(
                    self.logger, "ce_launch.stop_cleanup_failed", stop_exc,
                    expected=isinstance(stop_exc, (OSError, ValueError, RuntimeError)),
                    operation=operation_id[:12], mode=plan.mode, app_id=plan.app_id,
                )
            with self._state_lock:
                process_retained = operation_id in self._processes
            if not process_retained:
                try:
                    self._clear_record(plan.app_id, plan.session_id)
                except Exception as record_exc:
                    record_error = _safe_status_text(str(record_exc), 256)
                    cleanup_error = f"{cleanup_error}; {record_error}" if cleanup_error else record_error
                    log_failure(
                        self.logger, "ce_launch.record_cleanup_failed", record_exc,
                        expected=isinstance(record_exc, (OSError, ValueError, RuntimeError)),
                        operation=operation_id[:12], mode=plan.mode, app_id=plan.app_id,
                    )
            detail = str(exc)
            if cleanup_error:
                detail = f"{detail}; cleanup remains blocked: {cleanup_error}"
            log_failure(
                self.logger, "ce_launch.failed", exc,
                expected=isinstance(exc, (OSError, ValueError, RuntimeError)),
                operation=operation_id[:12], mode=plan.mode, app_id=plan.app_id,
                cleanup_error=cleanup_error,
            )
            self._set(
                operation_id,
                state="failed",
                message="Cheat Engine could not be launched",
                error=_safe_status_text(detail, 512),
            )
        finally:
            tail = bytes(retained) if retained else _tail_file(log_path)
            if tail:
                self._set(operation_id, log_tail=_safe_status_text(tail.decode("utf-8", "replace"), 1000))
            with self._state_lock:
                self._tasks.pop(operation_id, None)

    async def _spawn(self, plan: CELaunchPlan, log_path: Path | None) -> asyncio.subprocess.Process:
        # Discovery is only a snapshot. Re-hash the exact Proton script at the
        # last boundary before execution so a changed installation cannot be
        # launched under the old identity.
        await asyncio.to_thread(
            _verify_proton_tool,
            ProtonTool(plan.tool_id, plan.tool_name, plan.tool_path, plan.proton_sha256, "owned-launch"),
        )
        options: dict[str, object] = {}
        if os.name == "posix":
            # Keep Proton, wineserver and Cheat Engine in one owned process
            # group so stopping a launch cannot strand a Windows child.
            options["start_new_session"] = True
        if log_path is None:
            # A bounded self-test may inherit a pipe: CE Decky outlives it.
            return await asyncio.create_subprocess_exec(
                *plan.argv,
                cwd=str(Path(plan.executable).parent),
                env=launch_environment(plan),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **options,
            )
        # An attached launch must survive plugin unload. Inheriting a pipe from
        # a process that can go away would eventually deliver EPIPE/SIGPIPE to
        # Proton and Cheat Engine, so give it an owned file instead.
        await drained_to_thread(_ensure_managed_directory, log_path.parent, self.state_root, create=True)
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(log_path, flags, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise ValueError("owned Cheat Engine launch log is not a regular file")
        with os.fdopen(descriptor, "wb") as handle:
            return await asyncio.create_subprocess_exec(
                *plan.argv,
                cwd=str(Path(plan.executable).parent),
                env=launch_environment(plan),
                stdout=handle,
                stderr=asyncio.subprocess.STDOUT,
                **options,
            )

    async def _await_bridge(
        self,
        process: asyncio.subprocess.Process,
        status_path: Path,
        plan: CELaunchPlan,
        launch_epoch_ns: int,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        """Wait for a bridge that has finished starting.

        Returns the ready heartbeat, and separately the last heartbeat seen
        while the bridge was still inside its own bootstrap. The bridge
        publishes liveness before it opens the table, so a Cheat Engine stopped
        by one of its own modal forms is a bridge that is alive and never
        becomes ready: waiting on the first heartbeat of any kind would report
        that as a connected session, and waiting on nothing at all is what used
        to make it five minutes of silence.
        """
        deadline = time.monotonic() + SELF_TEST_TIMEOUT_SECONDS
        starting: dict[str, object] | None = None
        while time.monotonic() < deadline:
            observed = await asyncio.to_thread(
                _read_bridge_status,
                status_path,
                plan.session_id,
                plan.ce_sha256,
                plan.table_sha256,
                plan.descriptor_sha256,
                launch_epoch_ns,
            )
            if observed is not None:
                # A bridge that predates this contract publishes no phase at all
                # and published nothing until it was ready, so absent is ready.
                if observed.get("bridge_phase") != "starting":
                    return observed, starting
                starting = observed
            if process.returncode is not None:
                return None, starting
            await asyncio.sleep(HEARTBEAT_POLL_SECONDS)
        return None, starting

    async def _retire_self_test_prefix(self, plan: dict) -> None:
        """Stop the Wine clients of the owned self-test prefix this plan names."""
        await _retire_owned_proton_prefix(
            ProtonTool(
                str(plan["tool_id"]), str(plan["tool_name"]),
                str(plan["tool_path"]), str(plan["proton_sha256"]),
                "owned-self-test-stop",
            ),
            Path(str(plan["compat_data_path"])),
            self.ce_root,
        )

    async def _stop_process(self, operation_id: str) -> None:
        with self._state_lock:
            process = self._processes.get(operation_id)
            operation = self._operations.get(operation_id)
        if process is None:
            return
        group_gone = os.name != "posix"
        prefix_cleanup_error: Exception | None = None
        self_test_plan: dict | None = None
        if operation is not None and operation.get("mode") == MODE_SELF_TEST:
            plan = operation.get("plan")
            self_test_plan = plan if isinstance(plan, dict) else None
            if self_test_plan is None:
                prefix_cleanup_error = RuntimeError("owned self-test launch plan is unavailable")
        try:
            if os.name == "posix":
                if self_test_plan is not None:
                    # First pass, best effort: shutting the prefix down before
                    # the group is signalled lets Wine exit cleanly. It cannot
                    # be the answer on its own, because Proton is still running
                    # while it decides whether `pfx` exists - a cold prefix
                    # being initialized can appear immediately afterwards. The
                    # pass after the group is proven gone is the authoritative
                    # one, so a failure here is not yet a failure.
                    try:
                        await self._retire_self_test_prefix(self_test_plan)
                    except (KeyError, OSError, RuntimeError, ValueError):
                        pass
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                process.terminate()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), timeout=STOP_GRACE_SECONDS)
                except (TimeoutError, asyncio.TimeoutError):
                    if os.name == "posix":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        process.kill()
                    await asyncio.shield(process.wait())
            if os.name == "posix":
                # Waiting for the Proton leader is not enough: Wine children can
                # outlive it. Do not publish a confirmed exit until the complete
                # owned POSIX process group has disappeared.
                deadline = time.monotonic() + STOP_GRACE_SECONDS
                while _process_group_exists(process.pid) and time.monotonic() < deadline:
                    await asyncio.sleep(0.1)
                if _process_group_exists(process.pid):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + STOP_GRACE_SECONDS
                    while _process_group_exists(process.pid) and time.monotonic() < deadline:
                        await asyncio.sleep(0.1)
                if _process_group_exists(process.pid):
                    raise RuntimeError("owned Cheat Engine process group did not stop")
                # The group is only where the launcher was put. A Wine client can
                # leave it, and target evidence shows one doing so, so an empty
                # group is not proof that this exact Cheat Engine is gone. Sweep
                # for the descriptor identity CE Decky wrote into its environment
                # and require a negative proof before publishing a confirmed exit;
                # otherwise a table switch would start a second Cheat Engine
                # beside the one still attached to the game.
                plan = operation.get("plan") if operation is not None else None
                if isinstance(plan, dict) and operation is not None and operation.get("mode") != MODE_SELF_TEST:
                    descriptor = str(plan.get("descriptor_windows_path") or "")
                    digest = str(plan.get("descriptor_sha256") or "")
                    if descriptor and digest:
                        await self._sweep_escaped_descriptor(
                            descriptor, digest, str(plan.get("executable") or "")
                        )
                if self_test_plan is not None:
                    # The self-test has no descriptor to sweep for; its owned
                    # prefix is the equivalent negative proof, and only now is
                    # nothing left that could still be creating `pfx`. Missing
                    # `pfx` is clean; malformed or symlinked state stays
                    # fail-closed.
                    try:
                        await self._retire_self_test_prefix(self_test_plan)
                        prefix_cleanup_error = None
                    except (KeyError, OSError, RuntimeError, ValueError) as exc:
                        prefix_cleanup_error = exc
                group_gone = True
                if prefix_cleanup_error is not None:
                    group_gone = False
                    raise RuntimeError(
                        f"owned self-test Proton prefix did not stop: {prefix_cleanup_error}"
                    ) from prefix_cleanup_error
        finally:
            with self._state_lock:
                operation = self._operations.get(operation_id)
                if process.returncode is not None and group_gone:
                    self._processes.pop(operation_id, None)
                if operation is not None and process.returncode is not None and group_gone:
                    operation["exit_code"] = process.returncode
                    key = (operation.get("app_id"), str(operation.get("session_id")))
                    self._exits[key] = time.time()
                    while len(self._exits) > 64:
                        self._exits.pop(next(iter(self._exits)))

    async def _sweep_escaped_descriptor(
        self,
        descriptor_windows_path: str,
        descriptor_sha256: str,
        expected_executable: str,
    ) -> None:
        """Stop any owned Cheat Engine that left the recorded process group.

        Raises rather than returning a flag: the caller is publishing a confirmed
        exit, and an ambiguous or surviving match must keep ownership instead.
        """
        if os.name != "posix":
            return
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            state, pids = await asyncio.to_thread(
                owned_descriptor_pids,
                descriptor_windows_path,
                descriptor_sha256,
                expected_executable=expected_executable,
            )
            if state == "gone":
                return
            if state == "unreadable":
                raise RuntimeError("owned Cheat Engine identity could not be proven gone")
            for pid in pids:
                try:
                    os.kill(pid, signal_number)
                except (ProcessLookupError, PermissionError, OSError):
                    continue
            deadline = time.monotonic() + STOP_GRACE_SECONDS
            while time.monotonic() < deadline:
                state, _ = await asyncio.to_thread(
                    owned_descriptor_pids,
                    descriptor_windows_path,
                    descriptor_sha256,
                    expected_executable=expected_executable,
                )
                if state == "gone":
                    return
                if state == "unreadable":
                    raise RuntimeError("owned Cheat Engine identity could not be proven gone")
                await asyncio.sleep(0.2)
        raise RuntimeError("an owned Cheat Engine carrying this exact descriptor is still running")

    def _set(self, operation_id: str, **values: object) -> None:
        next_state = values.get("state")
        context: dict[str, object] | None = None
        with self._state_lock:
            operation = self._operations.get(operation_id)
            if operation is not None:
                previous_state = operation.get("state")
                operation.update(values)
                if isinstance(next_state, str) and next_state != previous_state:
                    context = {
                        "operation": operation_id[:12],
                        "mode": operation.get("mode"),
                        "app_id": operation.get("app_id"),
                        "previous": previous_state,
                        "state": next_state,
                        "message": operation.get("message"),
                        "error": operation.get("error"),
                    }
        if context is not None:
            log_activity(self.logger, "info", "ce_launch.state_changed", **context)

    def _operation_snapshot(self) -> list[dict[str, object]]:
        with self._state_lock:
            return [self._public(operation) for operation in self._operations.values()]

    def _prune(self) -> None:
        while len(self._operations) > 16:
            for operation_id, operation in self._operations.items():
                if (
                    operation.get("state") not in LIVE_LAUNCH_STATES
                    and operation_id not in self._processes
                    and operation_id not in self._tasks
                ):
                    self._operations.pop(operation_id, None)
                    break
            else:
                return

    def _live_self_test(self) -> dict[str, object] | None:
        return next(
            (
                operation
                for operation in self._operations.values()
                if operation.get("mode") == MODE_SELF_TEST
                and (
                    operation.get("state") in LIVE_LAUNCH_STATES
                    or operation.get("operation_id") in self._processes
                )
            ),
            None,
        )

    @staticmethod
    def _public(operation: dict[str, object]) -> dict[str, object]:
        public = dict(operation)
        public.pop("stop_requested", None)
        return public


def _describe_unfinished_bridge(starting: dict[str, object]) -> str:
    """Say what a bridge that kept reporting `starting` was stopped by.

    Everything here comes from the last heartbeat it published, because a
    Cheat Engine held inside one of its own modal forms answers nothing else.
    """
    def one_line(value: object, limit: int = 200) -> str | None:
        """Cheat Engine's own text, fit for a sentence a user is shown.

        `table_load_error` is deliberately allowed to carry a multi-line Lua
        traceback of up to 4 KB, and this message ends up in a Steam toast and
        in Review's failure row. The whole traceback is already in the status
        file and the support bundle, which is where it is read from.
        """
        if not isinstance(value, str) or not value:
            return None
        collapsed = " ".join(value.split())
        if not collapsed:
            return None
        return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"

    parts = ["the resident Cheat Engine bridge stayed in its own startup and never became ready"]
    table_state = starting.get("table_load_state")
    route = starting.get("table_load_route")
    if table_state == "pending" and route == "approved":
        parts.append("it was still opening the session table")
    elif table_state == "pending" and route == "prompted":
        parts.append(
            "it was still opening the session table through the path form, which lets this "
            "Cheat Engine ask a question that cannot be answered over a running game"
        )
    elif table_state == "pending":
        parts.append("it had not opened the session table yet")
    elif isinstance(table_state, str):
        parts.append(f"the session table reported {table_state}")
    error = one_line(starting.get("table_load_error"))
    if error:
        parts.append(f"the last table-load error was {error}")
    dialog = one_line(starting.get("last_dialog"), 120)
    if dialog:
        parts.append(f"the last Cheat Engine window it answered was {dialog}")
    return "; ".join(parts)


def _read_bridge_status(
    status_path: Path,
    session_id: str,
    ce_sha256: str,
    table_sha256: str,
    descriptor_sha256: str,
    min_mtime_ns: int | None = None,
) -> dict[str, object] | None:
    try:
        raw, info = read_regular_bytes_with_stat(status_path, max_bytes=1024 * 1024, allow_missing=True)
    except (OSError, ValueError):
        return None
    if not raw or info is None or (min_mtime_ns is not None and info.st_mtime_ns <= min_mtime_ns):
        return None
    try:
        status = parse_status(raw)
    except ValueError:
        return None
    if (
        status.session_id != session_id
        or status.ce_sha256 != ce_sha256
        or status.table_sha256 != table_sha256
        or status.descriptor_sha256 != descriptor_sha256
    ):
        return None
    return {
        "session_id": status.session_id,
        "descriptor_sha256": status.descriptor_sha256,
        "table_sha256": status.table_sha256,
        "attached": status.attached,
        "target_process": status.target_process,
        "opened_process_id": status.opened_process_id,
        "address_list_count": status.address_list_count,
        "table_load_state": status.table_load_state,
        "table_load_error": status.table_load_error,
        # Whether the bridge has finished its own bootstrap, and how it opened
        # the table. A bridge stopped inside one of Cheat Engine's modal forms
        # is alive and starting forever, and without these two the only thing
        # left to report about it is that nothing was heard at all.
        "bridge_phase": status.bridge_phase,
        "table_load_route": status.table_load_route,
        "dialogs_dismissed": status.dialogs_dismissed,
        "last_dialog": status.last_dialog,
        "window_suppressions": status.window_suppressions,
        # Whether this exact Cheat Engine can be asked if a window is minimized,
        # which decides whether a game it pushes aside can ever be brought back.
        # The self-test is where that is answered without a game.
        "minimized_query": status.minimized_query,
        # Which half of that capability failed. The row above cannot say: it
        # reads `unavailable` whether the question cannot be answered or the
        # answer cannot be acted on, and those are different defects.
        "restore_capability": status.restore_capability,
        # And what that half actually said when it refused.
        "restore_error": status.restore_error,
        "process_count": len(status.processes),
        "heartbeat_ms": status.heartbeat_ms,
    }


def _tail_file(path: Path | None) -> bytes:
    if path is None:
        return b""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_LAUNCH_LOG_BYTES))
            return handle.read(MAX_LAUNCH_LOG_BYTES)
    except OSError:
        return b""


async def _drain_output(process: asyncio.subprocess.Process, retained: bytearray) -> None:
    stream = process.stdout
    if stream is None:
        return
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return
        retained.extend(chunk)
        if len(retained) > MAX_LAUNCH_LOG_BYTES:
            del retained[:-MAX_LAUNCH_LOG_BYTES]


def _unsafe_exact_directory(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return not stat.S_ISDIR(info.st_mode)


def _process_group_exists(pgid: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # Ambiguous observation is not proof that the group disappeared.
        return True


def _real_path(value: str) -> str:
    try:
        return str(Path(value).resolve(strict=True))
    except (OSError, RuntimeError):
        return str(Path(value))


def _process_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and 5 <= len(value.encode("utf-8")) <= 255
        and value.lower().endswith(".exe")
        and not any(character in value for character in '\\/:*?"<>|')
        and not _unsafe_text(value, 255)
    )


def _app_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 0xFFFFFFFF:
        raise ValueError("AppID must be an integer between 1 and 4294967295")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("SHA-256 digest is invalid")
    return value.lower()


def _md5(value: object) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(character not in "0123456789abcdefABCDEF" for character in value):
        raise ValueError("MD5 digest is invalid")
    return value.lower()


def _uuid_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("session identity must be a UUID string")
    return str(uuid.UUID(value))
