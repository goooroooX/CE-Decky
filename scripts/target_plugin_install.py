#!/usr/bin/env python3
"""Install an exact CE Decky ZIP, or read the last verified install authority."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any
import zipfile

if __package__:
    from . import host_platform
    from .target_package_probe import PACKAGE_ROOT, inspect_package
else:
    import host_platform
    from target_package_probe import PACKAGE_ROOT, inspect_package


ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "py_modules"
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from ce_decky import frontend_journal  # noqa: E402
from ce_decky.ce_launch import observe_running_app_ids  # noqa: E402
# The loopback control socket and the two loader routes an install uses belong
# to the plugin now, because the plugin updates itself through the same two.
# This helper drives them for a development install and imports rather than
# repeats them; the names it re-exports are the ones its own regressions call.
from ce_decky.decky_control import (  # noqa: E402
    DEFAULT_DECKY_URL,
    MAX_WS_MESSAGE_BYTES,
    PLUGIN_NAME,
    DeckyWebSocket,
    DeckyWebSocketClosed,
    auth_token as _auth_token,
    await_reply as _await_reply,
    install_and_confirm,
    loader_plugin_matches as _loader_plugin_matches,
    receive_exact as _receive_exact,
    request_frontend_reload as _request_frontend_reload,
    rpc_error as _rpc_error,
)

# Where a mirror of this checkout sits on a device reached across the network.
# `docs/REMOTE_TARGET.md` describes the arrangement and leaves the path to the
# caller; this is the name it uses, so `--remote` takes one argument in the
# ordinary case. It is a directory name and never an address: which device is
# being worked on stays out of this repository.
DEFAULT_REMOTE_ROOT = "ce-decky-mirror"
# How long the two bounded SSH round trips either side of the install may take.
# The install's own bound is the caller's `--timeout`, which applies at the far
# end where the install happens.
REMOTE_STEP_TIMEOUT_SECONDS = 120
MAX_AUTHORITY_REPORTS = 256
# Install authority has to survive an ordinary repository clean. It is developer
# state, not plugin state and not a build artifact, so keep it in the user's XDG
# state directory rather than below the ignored and disposable `build/` tree.
def default_install_records(environ: dict[str, str] | None = None) -> Path:
    values = os.environ if environ is None else environ
    configured = values.get("XDG_STATE_HOME", "").strip()
    state_home = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    if not state_home.is_absolute():
        state_home = Path.home() / ".local" / "state"
    return state_home / "ce-decky-development" / "target-installs"


INSTALL_RECORDS = default_install_records()
MAX_RETAINED_INSTALL_RECORDS = 32
# How long this must leave between two Steam webhelper replacements.
#
# Every install ends in one, because that is how the exact frontend is imported.
# Decky counts a replacement that lands within a minute of the previous one as a
# webhelper crash, and on the third such count it stops its own service: the
# loader exits, nothing answers on its socket, and the device needs a
# `systemctl restart plugin_loader` that only root can give. That is not a
# hypothesis; `docs/FIELD_NOTES.md` carries the run where it happened, with
# Decky's own three warnings and the shutdown six seconds after the third.
#
# So an install owed less than this since the last one waits the rest out. A
# minute is Decky's window and this sits just past it, because the moment being
# measured from is this helper's previous reload request rather than the
# replacement Steam actually performed a moment later.
WEBHELPER_RESTART_SPACING_SECONDS = 65.0
# How long one process must have been this plugin's backend before the frontend
# reload may be requested.
#
# One install of an already installed plugin loads it twice, and that is Decky's
# own behaviour rather than this helper's. `browser.py` disables its hot reload
# watcher for the install, but the uninstall it performs first re-enables it in
# a `finally`, so the watcher is live again while the new tree is being written:
# it loads the plugin from those files, and the installer then stops that load
# and starts its own. Each load dispatches `loader/import_plugin` to the panel.
#
# The panel imports plugins on start without taking the reload lock its event
# handler takes, so an import event that lands during that bulk import races it:
# both remove the entry that is not there yet, both append their own, and the
# panel ends up with two CE Decky rows over one backend. Asking for the
# webhelper reload between the two loads is what puts the event exactly there,
# and on 2026-09-13 it did, twice.
#
# What says the second load has been and gone is the install's own reply, not a
# timer. `confirm_plugin_install` is awaited across the whole of Decky's
# `_install`, so the reply arrives after its final `import_plugin` has returned,
# and nothing else can start another load afterwards: the watcher's remaining
# queue entries all ask for a refresh of a plugin that is loaded again, which
# Decky refuses. A short window after that reply is about the process being up,
# not about which generation it is.
PLUGIN_BACKEND_SETTLE_SECONDS = 2.0
# And what it costs when that reply never arrives, which is the documented
# ambiguity this helper already handles: Decky can close the install socket
# after accepting the confirmation and before replying, and then the tree and
# the loader readback are all there is. Both of those are satisfied by the
# watcher's premature load, so a window here has to outlive that whole doomed
# generation rather than sample it: Decky's stop is SIGTERM, five seconds, then
# SIGKILL, its installer sleeps a second more, and the two replacements measured
# on this device took seven and six seconds from one `Loaded CE Decky` to the
# next. Four seconds sampled the middle of that and called it settled.
PLUGIN_BACKEND_REPLACEMENT_SECONDS = 10.0
# What either wait may cost before the install goes on without it. The doomed
# generation, the gap while nothing is running and a full replacement window fit
# inside this with margin; a settle that has not happened by then is not a slow
# one.
PLUGIN_BACKEND_SETTLE_TIMEOUT_SECONDS = 30.0
# What proves the reload did what an install asks it for, and the only thing
# that has ever caught it doing something else.
#
# The panel records `panel.mounted` from the factory Decky calls when it imports
# this plugin, and `panel.dismounted` from the teardown Decky calls when it
# drops one, both carrying `panel_instance`, which is one id per factory
# invocation and therefore one per row. Deliberately not the entry's `session`,
# which names the loaded module that wrote it: Decky imports the bundle as
# `index.js?t=${Date.now()}`, a browser returns one module instance per resolved
# URL, and two imports issued inside one millisecond therefore evaluate the
# module once and call its factory twice. Two rows, one session; pairing by
# session would collapse exactly the concurrency this exists to catch, and one
# of the two dismounting would then take the other's evidence with it.
#
# What the panel lists is the mounts with no dismount against them, and that is
# the number this reads: an import count is not it. Decky's own `importPlugin`
# removes the plugin's existing row before appending its replacement, and
# removing it dismounts it, so two imports in sequence are two mounts, one
# dismount and one row. The duplicate is two imports that overlapped, where each
# found no row to remove and both appended, and it is only that case that leaves
# two mounts with no dismount between them.
#
# The panel flushes its record on a five second timer and Steam takes a few
# seconds to bring the frontend up, so the wait for the first record is the sum
# of both with margin. The dismount of a replaced instance is flushed as it
# happens rather than on that timer, and a second overlapping import lands in
# the same tick as the first, so the window after the first record is margin
# rather than a measurement: the two seen on 2026-09-13 were 66 ms apart.
PANEL_IMPORT_TIMEOUT_SECONDS = 20.0
PANEL_IMPORT_SETTLE_SECONDS = 2.0
# How long after the reload was asked for a renderer must have started before it
# counts as one the reload created.
#
# A mount says when its renderer started and the helper knows when it asked for
# the reload, and both are fixed moments: the same record reads the same way on
# every pass over the file, which an age compared against a growing wait does
# not.
#
# Small, and measured rather than assumed. Steam starts the replacement
# renderer almost at once: on this device it started 1.3 s after the request,
# while Decky's own frontend load, which is the five seconds one might mistake
# for it, happens later inside that renderer. The renderer being replaced was
# created at the previous reload, minutes earlier, so the two are far apart and
# this only covers the skew between the panel's own estimate of when its
# document began and the clock this helper read. A margin sized for the
# frontend load instead swallowed the new renderer and reported a healthy
# install as unobserved.
PANEL_GENERATION_MARGIN_SECONDS = 0.5
MAX_PANEL_JOURNAL_BYTES = 256 * 1024
MAX_AUTHORITY_REPORT_BYTES = 256 * 1024
MAX_PLUGIN_METADATA_BYTES = 256 * 1024
MAX_INSTALLED_BUNDLE_BYTES = 32 * 1024 * 1024


class LiveAuthorityUnavailable(RuntimeError):
    """Decky's authenticated local API could not be reached."""


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_install_report(path: Path) -> dict[str, Any] | None:
    """One bounded, regular successful install report, or no report."""
    if path.is_symlink():
        raise RuntimeError(f"install authority record is a symlink: {path.name}")
    try:
        info = path.stat()
    except OSError as exc:
        raise RuntimeError(f"install authority record is unreadable: {path.name}: {exc}") from exc
    if not path.is_file() or info.st_size <= 0 or info.st_size > MAX_AUTHORITY_REPORT_BYTES:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("ok") is not True:
        return None
    installed = value.get("installed")
    plugin_root = installed.get("plugin_root") if isinstance(installed, dict) else None
    package_sha = value.get("package_sha256")
    version = value.get("version")
    if (
        not isinstance(plugin_root, str)
        or not isinstance(package_sha, str)
        or re.fullmatch(r"[0-9a-f]{64}", package_sha) is None
        or not isinstance(version, str)
        or not version
    ):
        return None
    return value


# Where the moment of the last reload request is left, for the next run of this.
#
# Deliberately not a `.json` in the same directory: install records are read
# back as install authority, and a file that is not one must not be found by
# that search. Deliberately not the install record either, because a record is
# only written for an install that succeeded, and a reload that was requested
# and then failed is exactly the one whose window still has to be waited out.
RELOAD_MARKER = "last-reload.stamp"


def _reload_stamp(moment: float | None = None) -> dict[str, object]:
    """When this webhelper replacement was asked for, for the next run of this.

    One wall-clock reading, because that is the clock Decky's own crash counter
    runs on: `handle_crash` compares `time()` with the moment of the last exit
    and counts anything inside a minute of it. A spacing measured on any other
    clock would be spacing against a window nobody keeps.
    """
    return {"requested_at": time.time() if moment is None else moment}


def _note_reload_requested(
    records_root: Path = INSTALL_RECORDS,
    moment: float | None = None,
    stamp: dict[str, object] | None = None,
) -> None:
    """Leave the moment of a reload request where the next install will find it."""
    try:
        records_root.mkdir(parents=True, exist_ok=True)
        marker = records_root / RELOAD_MARKER
        staging = marker.with_name(marker.name + ".tmp")
        # JSON in a file that is deliberately not named `.json`: the install
        # authority search reads `*.json` in this directory and must not find
        # this one.
        staging.write_text(
            json.dumps(stamp if stamp is not None else _reload_stamp(moment)) + "\n", encoding="utf-8",
        )
        staging.replace(marker)
    except OSError:
        # The window is then measured from the install record instead, or not at
        # all. Never a reason to fail an install that is otherwise fine.
        pass


def last_reload_request(records_root: Path = INSTALL_RECORDS) -> float | None:
    """When this helper last asked Steam to replace its webhelper, as a clock time.

    Two observations rather than a primary and a fallback, and the newest wins.
    The marker is written at the request itself and survives an install that
    failed afterwards; the newest install record is written only by one that
    succeeded. Either can be the fresher of the two: writing the marker is
    allowed to fail quietly, and a readable but stale marker standing in front
    of a newer record is how a window gets declared elapsed when it has not.
    """
    seen = [moment for moment in (_marker_moment(records_root), _record_moment(records_root)) if moment is not None]
    return max(seen) if seen else None


def _marker_moment(records_root: Path) -> float | None:
    try:
        raw = (records_root / RELOAD_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        try:
            # A marker from before this file carried an object.
            return float(raw)
        except ValueError:
            return None
    moment = value.get("requested_at") if isinstance(value, dict) else None
    return float(moment) if isinstance(moment, (int, float)) and not isinstance(moment, bool) else None


def _record_moment(records_root: Path) -> float | None:
    try:
        newest = max(
            (item for item in records_root.glob("*.json") if item.is_file() and not item.is_symlink()),
            key=lambda item: item.name,
            default=None,
        )
    except OSError:
        return None
    if newest is None:
        return None
    try:
        recorded = json.loads(newest.read_text(encoding="utf-8"))
        stamp = (recorded.get("frontend_reload") or {}).get("requested_at")
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            return float(stamp)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        # A record from before that field existed, or one that cannot be read:
        # the file's own age is the same moment to within a second.
        return newest.stat().st_mtime
    except OSError:
        return None


def _wait_out_reload_spacing(
    records_root: Path = INSTALL_RECORDS, spacing: float = WEBHELPER_RESTART_SPACING_SECONDS,
) -> float:
    """Leave Decky's window between two webhelper replacements. Returns the wait.

    The same clock Decky counts on, and deliberately no more machinery than
    that: its `handle_crash` compares `time()` against the moment of the last
    webhelper exit, so the window this is spacing against moves with the wall
    clock exactly as this arithmetic does. A correction forwards carries both
    sides with it and counts nothing; a correction backwards puts Decky's own
    recorded moment in its future, which makes it count every replacement as a
    crash until wall time catches up, and nothing this helper can wait out
    clears that. That limit is written down rather than modelled: it is a
    clock-correction window of a few minutes on a device where installs are
    minutes apart anyway.
    """
    previous = last_reload_request(records_root)
    if previous is None:
        return 0.0
    elapsed = time.time() - previous
    if elapsed < 0:
        # The recorded moment is in this clock's future, so the age of the last
        # replacement cannot be read off it and may be seconds rather than
        # hours. One bounded window rather than the distance back to it, which
        # for a corrupt stamp would be a sleep with no bound. It does not prove
        # Decky's own window has cleared, and does not claim to.
        owed = spacing
        reason = "the recorded moment is in this clock's future, so the window cannot be measured"
    else:
        owed = spacing - elapsed
        reason = None
    if owed <= 0:
        return 0.0
    print(
        f"target plugin install: waiting {owed:.0f}s so Steam's webhelper is not replaced twice inside"
        " Decky's own crash window; Decky stops its service on the third"
        + (f" ({reason})" if reason else ""),
        file=sys.stderr,
    )
    time.sleep(owed)
    return owed


def record_install(result: dict[str, Any], records_root: Path = INSTALL_RECORDS) -> Path:
    """Keep this install as the authority a later run can read back.

    The report used to exist only as this command's standard output, which meant
    something else had to capture it into a session directory for `authority` to
    find. The installer writes its own record instead, so the two halves need no
    third tool between them.
    """
    records_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = str(result.get("package_sha256", ""))[:12] or "unknown"
    path = records_root / f"{stamp}-{digest}.json"
    staging = path.with_name(path.name + ".tmp")
    staging.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    staging.replace(path)
    kept = sorted(
        item for item in records_root.glob("*.json")
        if item.is_file() and not item.is_symlink()
    )
    for stale in kept[:-MAX_RETAINED_INSTALL_RECORDS]:
        try:
            stale.unlink()
        except OSError:
            continue
    return path


def latest_install_authority(records_root: Path = INSTALL_RECORDS) -> dict[str, object]:
    """The newest successful install this installer recorded, verified again.

    This performs no discovery outside that directory and no mutation. The
    newest successful record supplies the answer, that exact root must still be
    a regular installation directory, and any other retained record whose root
    also still exists must name the same one, so two live installations are an
    ambiguity rather than a silent choice between them.
    """
    raw_root = records_root.expanduser().absolute()
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise RuntimeError("install authority records are missing or unsafe")
    root = raw_root.resolve(strict=True)
    paths = [item for item in root.glob("*.json")]
    if len(paths) > MAX_AUTHORITY_REPORTS:
        raise RuntimeError("too many install authority candidates")
    reports: list[tuple[int, str, Path, dict[str, Any]]] = []
    for path in paths:
        report = _read_install_report(path)
        if report is None:
            continue
        reports.append((path.stat().st_mtime_ns, path.name, path, report))
    if not reports:
        raise RuntimeError("no successful install has been recorded")

    live_roots = set()
    for _, _, _, report in reports:
        candidate = Path(str(report["installed"]["plugin_root"]))
        if _is_installed_plugin_root(candidate):
            live_roots.add(str(candidate))
    if len(live_roots) > 1:
        raise RuntimeError("successful install authorities disagree on plugin_root")
    _, _, record, report = max(reports, key=lambda item: (item[0], item[1]))
    plugin_root = Path(str(report["installed"]["plugin_root"]))
    if not _is_installed_plugin_root(plugin_root):
        raise RuntimeError("recorded plugin_root is missing or unsafe")
    return {
        "schema": 1,
        "ok": True,
        "source": "recorded_install",
        "record": str(record),
        "plugin_root": str(plugin_root),
        "version": report["version"],
        "package_sha256": report["package_sha256"],
        "bundle_sha256": _installed_bundle_sha256(plugin_root),
        **_recorded_decky_paths(report),
    }


def _recorded_decky_paths(report: dict[str, Any]) -> dict[str, object]:
    """The Decky directories an earlier install recorded, if they still hold.

    These used to be reported as nothing at all, which is the one moment they
    are wanted: the live backend answers them whenever it is running, so a
    consumer reaching the recorded authority has already failed to ask it, and
    that consumer is usually a probe about state on a device whose backend is
    the thing that stopped. The record carries both, because the install
    validated them against the live backend before writing them down.

    Recorded rather than live, so they are checked again here rather than
    trusted: the same shape the live answer is held to, and then the filesystem,
    because a home or a settings directory that has since gone is not an answer
    to give a helper that is about to open it. The `source` above already says
    which of the two this is, and a live reading never falls back to this one.
    """
    backend = (report.get("readback") or {}).get("backend")
    if not isinstance(backend, dict):
        return {"settings_dir": None, "user_home": None}
    settings_dir = _reported_settings_dir(backend)
    user_home = _reported_user_home(backend)
    return {
        "settings_dir": settings_dir if settings_dir and Path(settings_dir).is_dir() else None,
        "user_home": user_home if user_home and Path(user_home).is_dir() else None,
    }


def _installed_bundle_sha256(plugin_root: Path) -> str | None:
    """The digest of the frontend this device would actually load.

    What answers "is the device running the build in my tree?" in one command.
    The root and the version alone cannot: a development version is rebuilt and
    reinstalled many times without moving, so the only thing that separates two
    of them is the bundle itself, and comparing it otherwise means reaching into
    Decky's directory by hand for a digest, which is a step this helper exists to
    remove.

    Read from the device rather than remembered, so it answers for what is
    installed and not for what an install once reported. Never fatal: the
    authority being read here is the plugin root, and a bundle that cannot be
    read leaves that no less established, so this says nothing rather than
    refusing the answer around it.
    """
    bundle = plugin_root / "dist" / "index.js"
    try:
        if bundle.is_symlink() or not bundle.is_file():
            return None
        if bundle.stat().st_size > MAX_INSTALLED_BUNDLE_BYTES:
            return None
        digest = sha256()
        with bundle.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _installed_plugin_metadata(plugin_root: Path) -> dict[str, object]:
    values: dict[str, object] = {}
    for name in ("plugin.json", "package.json"):
        metadata = plugin_root / name
        if metadata.is_symlink() or not metadata.is_file():
            raise RuntimeError(f"live plugin root has no regular {name}")
        if metadata.stat().st_size <= 0 or metadata.stat().st_size > MAX_PLUGIN_METADATA_BYTES:
            raise RuntimeError(f"live {name} has an invalid size")
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"live {name} is unreadable: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"live {name} is not an object")
        values[name] = value
    return values


def _disagreement(headline: str, side: str, found: object, loader_version: str) -> str:
    """Say which two versions disagree, and name the ordinary reason they might.

    Immediately after an install the files on disk carry the new version while
    Decky's inventory still reports the old one, and that reads exactly like a
    live identity conflict: the same refusal, with no way to tell the two apart.
    One is a few seconds of the loader catching up and the other is a machine
    holding a plugin this run must not authorize, and the contract is strict
    about the second precisely because it must never be waved through. So the
    refusal stands either way, and it names both versions and the ordinary
    explanation rather than sending the reader to the alarming one first.

    It deliberately does not retry. Waiting quietly for the numbers to agree is
    how a real conflict becomes invisible.
    """
    return (
        f"{headline}: {side} reports {found!r} and Decky's inventory reports {loader_version!r}."
        " Immediately after an install these differ until the loader has caught up, so read the"
        " authority again in a few seconds. A difference that survives that is a real identity"
        " conflict and this refuses it."
    )


def _live_authority_from_answers(
    matches: object, status: object,
) -> dict[str, object]:
    """Validate the exact path reported by one authenticated live backend."""
    if not isinstance(matches, list):
        raise RuntimeError("Decky loader plugin inventory is not a list")
    selected = [item for item in matches if isinstance(item, dict) and item.get("name") == PLUGIN_NAME]
    if len(selected) != 1 or selected[0].get("disabled") is True:
        raise RuntimeError("Decky does not report one enabled CE Decky plugin")
    loader_version = selected[0].get("version")
    if not isinstance(loader_version, str) or not loader_version:
        raise RuntimeError("Decky reports CE Decky without a version")
    if not isinstance(status, dict):
        raise RuntimeError("CE Decky backend returned no status to authorize an installation")
    backend_version = status.get("version")
    # Validated before it is compared. Absent, null or non-string is identity
    # data that is missing or malformed, which is a different answer from two
    # valid versions disagreeing, and only the second can be the loader catching
    # up. Saying so sends the reader to look at the right thing.
    if not isinstance(backend_version, str) or not backend_version:
        raise RuntimeError(
            f"CE Decky backend reports no usable version ({backend_version!r}),"
            f" so it cannot be compared with Decky's inventory ({loader_version!r})"
        )
    if backend_version != loader_version:
        raise RuntimeError(_disagreement(
            "CE Decky backend identity disagrees with Decky inventory",
            "the running backend", backend_version, loader_version,
        ))

    source = "live_backend"
    raw_plugin_root = status.get("plugin_dir")
    if not isinstance(raw_plugin_root, str) or not raw_plugin_root.strip():
        # Versions installed before `plugin_dir` was part of status still expose
        # the exact Decky settings directory. Decky's own directory contract puts
        # settings and plugins under the same DECKY_HOME; use that relation once,
        # then validate the candidate as strictly as a direct self-report.
        settings_value = status.get("settings_dir")
        if not isinstance(settings_value, str) or not settings_value.strip():
            raise RuntimeError("legacy CE Decky status has no installation authority")
        settings_dir = Path(settings_value).expanduser()
        if (
            not settings_dir.is_absolute()
            or ".." in settings_dir.parts
            or settings_dir.name != PACKAGE_ROOT
            or settings_dir.parent.name != "settings"
        ):
            raise RuntimeError("legacy CE Decky settings path cannot authorize an installation")
        raw_plugin_root = str(settings_dir.parent.parent / "plugins" / PACKAGE_ROOT)
        source = "live_backend_legacy_layout"

    plugin_root = Path(raw_plugin_root).expanduser()
    if not _is_installed_plugin_root(plugin_root):
        raise RuntimeError("live CE Decky plugin root is missing or unsafe")
    metadata = _installed_plugin_metadata(plugin_root)
    plugin_metadata = metadata["plugin.json"]
    package_metadata = metadata["package.json"]
    if (
        not isinstance(plugin_metadata, dict)
        or plugin_metadata.get("name") != PLUGIN_NAME
        or not isinstance(package_metadata, dict)
    ):
        raise RuntimeError("live plugin metadata does not name this plugin")
    installed_version = package_metadata.get("version")
    if not isinstance(installed_version, str) or not installed_version:
        raise RuntimeError(
            f"the installed plugin package names no usable version ({installed_version!r}),"
            f" so it cannot be compared with Decky's inventory ({loader_version!r})"
        )
    if installed_version != loader_version:
        raise RuntimeError(_disagreement(
            "live plugin metadata disagrees with Decky inventory",
            "the installed tree", installed_version, loader_version,
        ))
    return {
        "schema": 2,
        "ok": True,
        "source": source,
        "plugin_root": str(plugin_root),
        "version": loader_version,
        "bundle_sha256": _installed_bundle_sha256(plugin_root),
        "settings_dir": _reported_settings_dir(status),
        "user_home": _reported_user_home(status),
    }


def _reported_settings_dir(status: dict[str, Any]) -> str | None:
    """The exact plugin settings directory the live backend reports for itself.

    Validated the same way the legacy layout path validates it, because a
    consumer fills a `--settings-dir` from this and an unchecked value would
    send a probe outside Decky's own directories. An answer that does not hold
    is reported as absent rather than guessed at from the plugin root.
    """
    value = status.get("settings_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if (
        not candidate.is_absolute()
        or ".." in candidate.parts
        or candidate.name != PACKAGE_ROOT
        or candidate.parent.name != "settings"
    ):
        return None
    return str(candidate)


def _reported_user_home(status: dict[str, Any]) -> str | None:
    """The Steam-user home the live backend received from Decky."""
    value = status.get("user_home")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        return None
    return str(candidate)


def live_install_authority(
    decky_url: str = DEFAULT_DECKY_URL, timeout: float = 5.0,
) -> dict[str, object]:
    """Read exact install authority through Decky's authenticated local API."""
    try:
        token = _auth_token(decky_url, timeout)
        with DeckyWebSocket.connect(decky_url, token, timeout) as ws:
            ws.send_json({"type": 0, "route": "loader/get_plugins", "args": [], "id": 10})
            matches = _await_reply(ws, 10)
            ws.send_json({
                "type": 0,
                "route": "loader/call_plugin_method",
                "args": [PLUGIN_NAME, "get_status"],
                "id": 11,
            })
            status = _await_reply(ws, 11)
    except (OSError, DeckyWebSocketClosed, TimeoutError, RuntimeError) as exc:
        raise LiveAuthorityUnavailable(f"live Decky authority is unavailable: {exc}") from exc
    return _live_authority_from_answers(matches, status)


def resolve_install_authority(
    records_root: Path = INSTALL_RECORDS,
    decky_url: str = DEFAULT_DECKY_URL,
    timeout: float = 5.0,
) -> dict[str, object]:
    """Prefer live self-report, with the durable install record as fallback."""
    try:
        return live_install_authority(decky_url, timeout)
    except LiveAuthorityUnavailable as live_error:
        try:
            return latest_install_authority(records_root)
        except RuntimeError as record_error:
            raise RuntimeError(f"{live_error}; recorded authority is unavailable: {record_error}") from record_error


def _is_installed_plugin_root(plugin_root: Path) -> bool:
    """An absolute, unescaped, non-symlinked Decky plugin directory that exists."""
    return (
        plugin_root.is_absolute()
        and ".." not in plugin_root.parts
        and plugin_root.name == PACKAGE_ROOT
        and plugin_root.parent.name == "plugins"
        and not plugin_root.is_symlink()
        and plugin_root.is_dir()
        and not plugin_root.parent.is_symlink()
    )


def verify_installed_tree(artifact: Path, plugin_root: Path) -> dict[str, object]:
    if plugin_root.is_symlink() or not plugin_root.is_dir():
        raise RuntimeError("Decky did not create the expected regular plugin directory")
    checked = 0
    with zipfile.ZipFile(artifact) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if member.is_dir():
                continue
            relative = Path(*path.parts[1:])
            installed = plugin_root / relative
            if installed.is_symlink() or not installed.is_file():
                raise RuntimeError(f"installed plugin member is missing or unsafe: {member.filename}")
            expected = sha256(archive.read(member)).hexdigest()
            if _sha256_file(installed) != expected:
                raise RuntimeError(f"installed plugin member differs from the exact ZIP: {member.filename}")
            checked += 1
    return {"checked_files": checked, "plugin_root": str(plugin_root)}


def _wait_for_installed_tree(artifact: Path, plugin_root: Path, timeout: float) -> dict[str, object]:
    """Wait for Decky's asynchronous replacement to expose one exact tree.

    The attempt count is reported so evidence distinguishes an immediately
    complete tree from one that only matched after Decky finished replacing it.
    """
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        attempts += 1
        try:
            verified = verify_installed_tree(artifact, plugin_root)
        except (OSError, RuntimeError) as exc:
            last_error = exc
            time.sleep(0.25)
            continue
        return {
            **verified,
            "attempts": attempts,
            "waited_seconds": round(time.monotonic() - started, 3),
        }
    raise RuntimeError(f"exact installed plugin tree did not stabilize: {last_error}")


def _steam_webhelper_pids(proc_root: Path = Path("/proc")) -> set[int]:
    result: set[int] = set()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() == "steamwebhelper":
                result.add(int(entry.name))
        except (OSError, UnicodeError):
            continue
    return result


def _plugin_backend_pids(plugin_root: Path, proc_root: Path = Path("/proc")) -> set[int]:
    """Every process running this plugin's backend, by exact argv.

    Decky names a plugin's process after the plugin and the exact `main.py` it
    was started from, so this matches the installed root rather than the plugin
    name alone: another copy of CE Decky under a different root is a different
    process and not one of these.
    """
    expected = f"{PLUGIN_NAME} ({plugin_root / 'main.py'})"
    found: set[int] = set()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            continue
        if argv.replace("\x00", "").strip() == expected:
            found.add(int(entry.name))
    return found


def _wait_for_settled_backend(
    plugin_root: Path, *, confirmed: bool, timeout: float = PLUGIN_BACKEND_SETTLE_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Wait until the process running this plugin is the one Decky will keep.

    `confirmed` is whether Decky's own install RPC replied. That reply is the
    generation boundary: it is sent after `_install` returns, so the loads that
    install made are all behind it and the window below only has to see the
    process. Without it there is no boundary to stand on, and the window has to
    be longer than the whole life of a generation Decky is about to replace,
    because the watcher's premature load satisfies every other check this helper
    makes and looks exactly like the final one.

    Never raises. A backend this cannot observe at all, on a host whose procfs
    answers differently, is reported as unobserved rather than failing an
    install whose files and readback are already proven; the same is true of one
    that is still being replaced when the bound runs out. Both are printed, and
    both leave a duplicate panel entry possible, which a webhelper reload of its
    own clears.
    """
    settle = PLUGIN_BACKEND_SETTLE_SECONDS if confirmed else PLUGIN_BACKEND_REPLACEMENT_SECONDS
    started = time.monotonic()
    deadline = started + timeout
    seen: set[int] = set()
    stable_since = started
    while time.monotonic() < deadline:
        current = _plugin_backend_pids(plugin_root)
        if current != seen:
            seen = current
            stable_since = time.monotonic()
        elif len(current) == 1 and time.monotonic() - stable_since >= settle:
            return {
                "settled": True,
                "confirmed_install": confirmed,
                "settle_s": settle,
                "pid": next(iter(current)),
                "waited_s": round(time.monotonic() - started, 2),
            }
        time.sleep(0.25)
    print(
        "target plugin install: Decky was still replacing the plugin backend after"
        f" {timeout:.0f}s; the panel may show a second CE Decky entry until the next reload",
        file=sys.stderr,
    )
    return {
        "settled": False,
        "confirmed_install": confirmed,
        "settle_s": settle,
        "observed_pids": sorted(seen),
        "waited_s": round(time.monotonic() - started, 2),
    }


def _panel_boundary(state_root: Path, requested_at: float) -> dict[str, object]:
    """Where the record ends now, and what tells this reload's rows from the last one's.

    Three things, because the position alone cannot do it. The position says
    where this reload's evidence starts in the file. The renderers already in
    the record are generations this reload replaces, since the file is the only
    thing that outlives them. And the moment the reload was asked for is what
    places a row whose renderer the record has never seen: every mount says when
    its renderer started, so one that started after the request was created by
    this reload and one that started before it was already there. Two fixed
    moments, so a row reads the same way on every pass over the file.

    That last one is the whole discriminator, and it is deliberately not a
    handshake with the outgoing frontend. Decky does not reliably ask that
    frontend to import a newly installed plugin: on this device's 19:39 install
    both backend loads dispatched and the outgoing frontend recorded nothing at
    all, so waiting for it to name itself made a healthy install unverifiable.
    """
    path = frontend_journal.journal_path(state_root)
    # The whole retained record rather than this file's own read bound: a
    # renderer this misses is one whose records have already been trimmed away,
    # and reading less than the file keeps would make that window wider for
    # nothing. The trim bounds it at half a megabyte.
    data, _truncated = frontend_journal.read_tail(path, max_bytes=frontend_journal.MAX_JOURNAL_BYTES)
    return {
        "position": frontend_journal.journal_position(path),
        "renderers": sorted(_renderers_in(data)),
        "requested_at": requested_at,
    }


def _renderers_in(data: bytes, events: tuple[str, ...] | None = None) -> set[str]:
    """Every frontend generation named in a stretch of the record.

    `events` narrows it to the records that mean a panel was actually there,
    which is what identifying the outgoing frontend turns on: any entry carries
    the renderer, but only a mount says that renderer holds a row.
    """
    renderers: set[str] = set()
    for line in data.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if events is not None and entry.get("event") not in events:
            continue
        fields = entry.get("fields")
        renderer = fields.get("panel_renderer") if isinstance(fields, dict) else None
        if isinstance(renderer, str) and renderer:
            renderers.add(renderer)
    return renderers


def _panel_instances_after(state_root: Path, boundary: dict[str, object]) -> dict[str, object]:
    """What the panel's own record says it is holding since a boundary.

    Read from that record rather than asked of the frontend, because the
    frontend is exactly the half that has just been replaced and a question put
    to it would be answered by whichever copy of the plugin got it.

    `boundary` is where the file ended before the reload was requested, not a
    moment: this record outlives the renderer, and the moments in it are the
    panel's own wall clock. A clock corrected backwards leaves the previous
    frontend's records stamped later than the clock this helper is holding, and
    filtering on that would take them as this reload's - one stale mount would
    certify the quiet case, two would report a duplicate. This helper already
    treats a persisted moment in its own future as a clock that moved rather
    than as a fact, and the same is true here. A boundary that no longer
    describes the file, because a trim replaced it, is reported rather than
    read around.

    `imports` is how many times the plugin's factory ran, and `live` is how many
    of those rows have not been dismounted, which is what the panel lists. The
    id is `fields.panel_instance`, one per factory invocation; the entry's
    `session` is the loaded module that wrote it and is not a row, because one
    module can be asked for two.

    A mount that carries no such id cannot be paired with anything, so it is
    counted as an import and reported as unpairable rather than guessed at in
    either direction. That is what a record written by a build older than this
    id looks like, and it is also why an unpairable mount never becomes a
    duplicate: what a wrong yes costs is a webhelper replacement nobody needed.
    """
    position = boundary.get("position")
    outgoing = set(boundary.get("renderers") or ())
    requested = boundary.get("requested_at")
    # The moment this reload was asked for, which every renderer start below is
    # compared with. Fixed on both sides, so the comparison does not change
    # while the helper waits.
    requested_at = (
        None if not isinstance(requested, (int, float)) or isinstance(requested, bool) else float(requested)
    )
    appended = frontend_journal.read_appended(
        frontend_journal.journal_path(state_root),
        position if isinstance(position, dict) else {},
        max_bytes=MAX_PANEL_JOURNAL_BYTES,
    )
    if not appended["valid"] or not appended["complete"]:
        return {"imports": 0, "live": 0, "unpairable": 0, "readable": False}
    imports = 0
    unpairable = 0
    live: set[str] = set()
    for line in bytes(appended["data"]).splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        event = entry.get("event")
        if event not in ("panel.mounted", "panel.dismounted"):
            continue
        fields = entry.get("fields")
        instance = fields.get("panel_instance") if isinstance(fields, dict) else None
        instance = instance if isinstance(instance, str) and instance else None
        renderer = fields.get("panel_renderer") if isinstance(fields, dict) else None
        renderer = renderer if isinstance(renderer, str) and renderer else None
        generation = _panel_generation(fields, renderer, outgoing, requested_at)
        if generation == "outgoing":
            # The frontend this reload replaced, writing down what it did before
            # it went. Its rows went with it.
            continue
        if generation == "unknown":
            # A record that cannot be placed on either side of the reload is
            # counted as an import and left unpairable, which is the answer that
            # never invents a row and never hides one.
            instance = None
        if event == "panel.mounted":
            imports += 1
            if instance is None:
                unpairable += 1
            else:
                live.add(instance)
        elif instance is not None:
            live.discard(instance)
    return {"imports": imports, "live": len(live), "unpairable": unpairable, "readable": True}


def _wait_for_panel_import(
    state_root: Path | None,
    boundary: dict[str, object],
    timeout: float = PANEL_IMPORT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """What the reloaded frontend ended up holding of this plugin.

    Never raises, and never fails the install: the files, the loader inventory
    and the backend are already proven by the time this runs, and what it adds
    is the state of the panel, which is the one thing an install has been able
    to get wrong without saying so.

    The answer is a cardinality rather than a pair of flags, because there are
    five states and only one of them is the quiet case:

    - `one`, exactly one row and nothing unpairable, which says nothing;
    - `duplicate`, more than one row;
    - `missing`, rows were imported and none of them is still there;
    - `unverified`, a mount that cannot be paired leaves the count unknowable,
      and it may itself be another row;
    - `unobserved`, nothing was recorded in time and the panel was not checked.

    A duplicate is about rows that were never dismounted, never about how many
    imports there were: an import that replaced the row before it is an ordinary
    reload, and calling that a duplicate would send the operator to replace
    Steam's webhelper again for nothing, inside the window where three of those
    stop Decky's service. The other three are not the quiet case either, and
    saying nothing about them is how an install that could not answer the
    question gets read as one that answered it.
    """
    if state_root is None:
        return {
            "observed": False,
            "cardinality": "unobserved",
            "reason": "the backend reported no user home to read the panel record from",
        }
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        seen = _panel_instances_after(state_root, boundary)
        if not seen["readable"]:
            return {
                "observed": False,
                "cardinality": "unverified",
                "reason": "the panel's record was replaced or outgrew one read after the reload",
            }
        if int(seen["imports"]):
            time.sleep(PANEL_IMPORT_SETTLE_SECONDS)
            counted = _panel_instances_after(state_root, boundary)
            if not counted["readable"]:
                return {
                    "observed": False,
                    "cardinality": "unverified",
                    "reason": "the panel's record was replaced or outgrew one read after the reload",
                }
            live = int(counted["live"])
            unpairable = int(counted["unpairable"])
            return {
                "observed": True,
                "imports": int(counted["imports"]),
                "live_instances": live,
                # A mount nothing can be paired with is not evidence of a second
                # row and never becomes one: what a wrong yes costs is a
                # webhelper replacement nobody needed. It is not evidence of one
                # row either, which is why it is its own answer below.
                "unpairable_imports": unpairable,
                "cardinality": _panel_cardinality(live, unpairable),
            }
        time.sleep(0.5)
    return {
        "observed": False,
        "imports": 0,
        "cardinality": "unobserved",
        "reason": f"the panel recorded no import within {timeout:.0f}s of the reload",
    }


def _panel_generation(
    fields: object, renderer: str | None, outgoing: set[str], requested_at: float | None,
) -> str:
    """Which side of the reload one panel record belongs to.

    A renderer the record already held before the reload is one the reload
    replaced. Otherwise the record says when its renderer started, and that
    answers it against the moment the reload was asked for: started after it,
    this reload created it; started before it, it was already there.

    Both sides are fixed, which is the property this turns on. An age recorded
    once and compared against how long the reader has been waiting classifies
    the same record differently on two passes, and the row from the frontend
    being replaced becomes the row that replaced it somewhere between them.

    A record that says nothing about its renderer's start is placed nowhere
    rather than guessed at.
    """
    if renderer is not None and renderer in outgoing:
        return "outgoing"
    started = fields.get("renderer_started_at_ms") if isinstance(fields, dict) else None
    if isinstance(started, str):
        try:
            started = float(started)
        except ValueError:
            started = None
    if not isinstance(started, (int, float)) or isinstance(started, bool) or requested_at is None:
        return "unknown"
    return "new" if float(started) / 1000.0 > requested_at + PANEL_GENERATION_MARGIN_SECONDS else "outgoing"


def _panel_cardinality(live: int, unpairable: int) -> str:
    """How many CE Decky rows the panel holds, or why that is not known."""
    if live > 1:
        return "duplicate"
    if unpairable:
        # Even beside one row that is known: the unpairable mount may be a
        # second one, and a count that might be two is not a count of one.
        return "unverified"
    return "one" if live == 1 else "missing"


def _panel_state_root(readback: dict[str, object] | None) -> Path | None:
    """Where the panel's own record lives, out of what the backend reported.

    The same relation `target_state_probe.py` uses, from the one authority for
    it: the Steam-user home the live backend was given by Decky. A backend that
    reports no usable home leaves the panel unread rather than guessed at.
    """
    backend = (readback or {}).get("backend")
    home = _reported_user_home(backend) if isinstance(backend, dict) else None
    return None if home is None else Path(home) / ".cheat-engine-decky" / "state"


def _validate_loader_readback(ws: Any, version: str, plugin_root: Path) -> dict[str, object]:
    matches = _loader_plugin_matches(ws, 3)
    if len(matches) != 1 or matches[0].get("version") != version or matches[0].get("disabled") is True:
        raise RuntimeError("Decky loader readback does not contain one enabled exact-version CE Decky plugin")
    ws.send_json({
        "type": 0,
        "route": "loader/call_plugin_method",
        "args": [PLUGIN_NAME, "get_status"],
        "id": 4,
    })
    status = _await_reply(ws, 4)
    if not isinstance(status, dict) or status.get("version") != version:
        raise RuntimeError("CE Decky backend status readback has the wrong version")
    if status.get("plugin_dir") != str(plugin_root):
        raise RuntimeError("CE Decky backend reports a different installed plugin root")
    return {
        "loader": matches[0],
        "backend": {
            "version": status.get("version"),
            "user_home": status.get("user_home"),
            "settings_dir": status.get("settings_dir"),
            "plugin_dir": status.get("plugin_dir"),
            "ce": status.get("ce"),
        },
    }


def _wait_for_loader_readback(
    decky_url: str, version: str, plugin_root: Path, timeout: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            token = _auth_token(decky_url, min(timeout, 5.0))
            with DeckyWebSocket.connect(decky_url, token, min(timeout, 5.0)) as ws:
                return _validate_loader_readback(ws, version, plugin_root)
        except (OSError, RuntimeError) as exc:
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"Decky loader/backend readback did not stabilize: {last_error}")


def _wait_for_webhelper_replacement(before: set[int], timeout: float) -> set[int]:
    deadline = time.monotonic() + timeout
    after: set[int] = set()
    while time.monotonic() < deadline:
        after = _steam_webhelper_pids()
        if after and after != before:
            return after
        time.sleep(0.25)
    return after


def _running_apps() -> dict[str, object]:
    """Which Steam AppIDs the live process table reports right now.

    Installing replaces the plugin and reloads Decky's frontend, which replaces
    Steam's webhelper - and that can take the Decky UI, and with it a playing
    game's controller input. Whether anything was running is therefore part of
    what this install did, not background context, and it is not something to
    reconstruct from memory afterwards. One bounded read-only scan of the same
    production observer the plugin uses; a truncated scan reports itself as
    unobserved rather than as an empty list.
    """
    return observe_running_app_ids().as_dict()


def _running_app_report(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    """Pair the two scans and name what stopped between them.

    `stopped` is only ever computed from two complete scans. A truncated one
    would otherwise turn "this install could not see the whole process table"
    into "this install closed your game", which is the opposite of the point.
    """
    observed = bool(before["available"]) and bool(after["available"])
    before_ids = [int(app_id) for app_id in before["app_ids"]]  # type: ignore[union-attr]
    after_ids = [int(app_id) for app_id in after["app_ids"]]  # type: ignore[union-attr]
    return {
        "observed": observed,
        "before": before,
        "after": after,
        "any_running": None if not observed else bool(before_ids),
        "stopped": None if not observed else [app_id for app_id in before_ids if app_id not in after_ids],
    }


def _describe_running_apps(report: dict[str, object]) -> str:
    if not report["observed"]:
        before, after = report["before"], report["after"]
        reason = before["reason"] or after["reason"] or "the process table could not be read"
        return f"whether a game was running could not be observed: {reason}"
    before_ids = [int(app_id) for app_id in report["before"]["app_ids"]]  # type: ignore[index,union-attr]
    stopped = [int(app_id) for app_id in report["stopped"]]  # type: ignore[union-attr]
    if not before_ids:
        return "no Steam app was running"
    running = ", ".join(f"AppID {app_id}" for app_id in before_ids)
    was = "was" if len(before_ids) == 1 else "were"
    if stopped:
        gone = ", ".join(f"AppID {app_id}" for app_id in stopped)
        is_gone = "is" if len(stopped) == 1 else "are"
        return f"{running} {was} running; {gone} {is_gone} no longer running after the frontend reload"
    return f"{running} {was} running and still is"


def install_package(
    artifact: Path,
    expected_sha: str,
    plugin_root: Path,
    decky_url: str,
    replace: bool,
    timeout: float,
) -> dict[str, object]:
    if not plugin_root.is_absolute() or plugin_root.name != PACKAGE_ROOT or plugin_root.parent.name != "plugins":
        raise RuntimeError("--plugin-root must be an absolute <homebrew>/plugins/CE-Decky path")
    # Before anything else, including the package read: what this waits for is
    # Decky's tolerance for webhelper replacements, and waiting it out with a
    # websocket already open would only add an idle connection to the problem.
    waited = _wait_out_reload_spacing()
    # Taken before the first mutation, so it describes the machine this install
    # arrived on rather than the one it left behind.
    running_before = _running_apps()
    report = inspect_package(artifact)
    artifact = Path(str(report["artifact"]))
    if not report["ok"]:
        raise RuntimeError(f"target package probe failed: {report['errors']!r}")
    if report["sha256"] != expected_sha:
        raise RuntimeError(f"package SHA mismatch: expected {expected_sha}, observed {report['sha256']}")
    existed = plugin_root.exists() or plugin_root.is_symlink()
    if existed and not replace:
        raise RuntimeError("plugin is already installed; pass --replace for an exact overwrite")
    if plugin_root.is_symlink():
        raise RuntimeError("refusing to replace a symlinked plugin root")
    if existed and not plugin_root.is_dir():
        raise RuntimeError("refusing to replace a non-directory plugin root")
    if not plugin_root.parent.is_dir() or plugin_root.parent.is_symlink():
        raise RuntimeError("exact Decky plugins parent is missing or symlinked")
    deploy_failure: Exception | None = None
    try:
        token = _auth_token(decky_url, timeout)
        with DeckyWebSocket.connect(decky_url, token, timeout) as ws:
            before_matches = _loader_plugin_matches(ws, 10)
            if existed and len(before_matches) != 1:
                raise RuntimeError("existing exact plugin root disagrees with Decky's CE Decky inventory")
            if not existed and before_matches:
                raise RuntimeError("Decky reports CE Decky from an unexpected plugin root")
            artifact_uri = artifact.as_uri()
            if artifact_uri[7:] != str(artifact):
                raise RuntimeError("Decky v3.2.6 local file install cannot safely represent this package path")
            install_and_confirm(ws, artifact_uri, str(report["version"]), expected_sha, existed)
    except Exception as exc:
        deploy_failure = exc
    # Whether Decky's own install RPC replied, taken before the readback below
    # is allowed to forgive a closed socket. The reply is the one thing that
    # says every load this install makes is already behind it, and the readback
    # cannot say that: the watcher's premature load answers it just as well.
    install_confirmed = deploy_failure is None

    # A plugin overwrite can restart its backend and close the install socket
    # after accepting confirm_plugin_install but before replying. Resolve only
    # that transport ambiguity through exact tree plus fresh loader/backend
    # readback. Explicit RPC/validation errors remain failures.
    installed: dict[str, object] | None = None
    readback: dict[str, object] | None = None
    try:
        installed = _wait_for_installed_tree(artifact, plugin_root, timeout)
        readback = _wait_for_loader_readback(decky_url, str(report["version"]), plugin_root, timeout)
    except Exception as readback_error:
        if deploy_failure is None:
            deploy_failure = readback_error
        else:
            deploy_failure = RuntimeError(f"{deploy_failure}; exact installed readback failed: {readback_error}")
    else:
        if isinstance(deploy_failure, DeckyWebSocketClosed):
            deploy_failure = None

    panel_state_root = _panel_state_root(readback)
    # Before the reload is even considered, because a reload requested between
    # Decky's two loads of one install is what puts a second CE Decky in the
    # panel. The constant above carries the mechanism.
    #
    # Skipped for an install that has already failed: there is no load to wait
    # out, the failure below is what the operator has to read, and spending the
    # whole bound to say the backend never settled buries it.
    settle = (
        {"settled": False, "reason": "the install failed; the backend was not waited for"}
        if deploy_failure is not None
        else _wait_for_settled_backend(plugin_root, confirmed=install_confirmed)
    )
    # Both snapshots immediately before this exact restart request, and for the
    # same reason: what happened during the plugin overwrite before it is not
    # evidence for this reload. The frontend being replaced is alive until the
    # webhelper goes, and Decky's second load of the plugin tells that frontend
    # to import it too, so a boundary taken any earlier would count the outgoing
    # panel's row as the incoming one's.
    before_webhelper = _steam_webhelper_pids()
    # What the next install measures its spacing from, taken here rather than
    # when this record is written: what Decky counts is the replacement, and
    # everything after this line can take a while. Three readings, because the
    # wall clock alone cannot measure "how long ago" on a host whose clock is
    # corrected, and the elapsed one is meaningless across a reboot.
    reload_stamp = _reload_stamp()
    reload_requested_at = float(reload_stamp["requested_at"])
    _note_reload_requested(stamp=reload_stamp)
    # Taken with the reload's own elapsed reading, and after the snapshot above,
    # so nothing the outgoing frontend writes between the two can be read as
    # this reload's.
    panel_boundary = (
        {} if panel_state_root is None else _panel_boundary(panel_state_root, reload_requested_at)
    )
    reload_request_failure: Exception | None = None
    if not before_webhelper:
        reload_request_failure = RuntimeError("Steam webhelper baseline is empty")
    else:
        try:
            token = _auth_token(decky_url, timeout)
            with DeckyWebSocket.connect(decky_url, token, timeout) as ws:
                _request_frontend_reload(ws)
        except Exception as exc:
            if not isinstance(exc, DeckyWebSocketClosed):
                reload_request_failure = exc

    after_webhelper = _wait_for_webhelper_replacement(before_webhelper, timeout)
    if not after_webhelper or after_webhelper == before_webhelper:
        details = []
        if deploy_failure is not None:
            details.append(f"deploy failure: {deploy_failure}")
        if reload_request_failure is not None:
            details.append(f"reload request failure: {reload_request_failure}")
        suffix = f" ({'; '.join(details)})" if details else ""
        raise RuntimeError(f"mandatory Steam webhelper reload was not observed{suffix}")
    if reload_request_failure is not None:
        raise RuntimeError(f"mandatory Steam webhelper reload RPC failed: {reload_request_failure}")
    if deploy_failure is not None:
        raise deploy_failure
    if installed is None or readback is None:
        raise RuntimeError("Decky deployment produced no verified installed identity")
    # The last thing, and the only one that reads the half this install exists
    # to replace. Everything above it proves what is on disk and what the
    # backend is; this proves what the panel did with it.
    panel = _wait_for_panel_import(panel_state_root, panel_boundary)
    if panel.get("cardinality") == "duplicate":
        print(
            f"target plugin install: the reloaded panel holds {panel.get('live_instances')} CE Decky"
            " instances that were never dismounted, so the Decky panel now lists it that many times"
            " over one backend; one more webhelper reload with no install beside it clears the extra rows",
            file=sys.stderr,
        )
    elif panel.get("cardinality") == "missing":
        print(
            "target plugin install: the reloaded panel imported CE Decky and holds none of it now,"
            " so the panel has no CE Decky row; the plugin is installed and its backend is running,"
            " and what is missing is the row, which one more webhelper reload rebuilds",
            file=sys.stderr,
        )
    return {
        "schema": 2,
        "ok": True,
        "running_apps": _running_app_report(running_before, _running_apps()),
        "artifact": str(artifact),
        "package_sha256": expected_sha,
        "version": report["version"],
        "replace": existed,
        "installed": installed,
        "readback": readback,
        "backend_settle": settle,
        "panel_import": panel,
        "frontend_reload": {
            "observed": True,
            "before_count": len(before_webhelper),
            "after_count": len(after_webhelper),
            **reload_stamp,
            "spacing_waited_s": round(waited, 1),
        },
    }


def _summary(result: dict[str, object]) -> str:
    """The whole install as one line.

    The full report is a couple of thousand bytes of readback that only matters
    when something went wrong, and this command is run several times a session.
    What a reader needs every time is: did it land, which build, and was it done
    over a running game. Everything else stays one `--json` away.
    """
    installed = result.get("installed") or {}
    reload_counts = result.get("frontend_reload") or {}
    parts = [
        f"CE Decky {result.get('version')} installed",
        f"package {str(result.get('package_sha256'))[:12]}",
        f"{installed.get('checked_files')} files verified",
    ]
    if reload_counts.get("observed"):
        parts.append(f"webhelper {reload_counts.get('before_count')}\u2192{reload_counts.get('after_count')}")
    parts.append(_describe_running_apps(result["running_apps"]))
    settle = result.get("backend_settle") or {}
    if settle and not settle.get("settled"):
        # Said on the summary line, because the panel is where it shows up and
        # nobody reads the JSON for an install that otherwise worked.
        parts.append("backend still settling; a duplicate panel entry is possible")
    # Every panel answer but the quiet one, because the four that are not it are
    # each a different thing to do next and none of them is nothing.
    panel = result.get("panel_import") or {}
    cardinality = panel.get("cardinality")
    if cardinality == "duplicate":
        parts.append(f"panel lists it {panel.get('live_instances')} times over one backend")
    elif cardinality == "missing":
        parts.append("panel holds no CE Decky row")
    elif cardinality == "unverified":
        parts.append(
            "panel cardinality unverified"
            + (f", {panel['unpairable_imports']} import(s) that belong to no known generation"
               if panel.get("unpairable_imports") else "")
        )
    elif cardinality != "one":
        parts.append("panel import unobserved")
    waited = reload_counts.get("spacing_waited_s") or 0
    if waited:
        parts.append(f"waited {waited}s for the webhelper window")
    if result.get("elapsed_s") is not None:
        parts.append(f"{result['elapsed_s']}s")
    return "target plugin install: " + " \u00b7 ".join(parts)


def _parser() -> argparse.ArgumentParser:
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    authority = commands.add_parser(
        "authority",
        help="read the latest verified install authority without changing the device",
    )
    authority.add_argument(
        "--records-root",
        default=str(INSTALL_RECORDS),
        help="directory of records this installer wrote",
    )
    authority.add_argument("--decky-url", default=DEFAULT_DECKY_URL)
    authority.add_argument("--timeout", type=float, default=5.0)
    install = commands.add_parser("install", help="install and verify one exact plugin package")
    install.add_argument(
        "package",
        nargs="?",
        default=str(ROOT / "artifacts" / f"CE-Decky-v{version}.zip"),
    )
    install.add_argument("--sha256", required=True, help="required exact package SHA-256")
    install.add_argument(
        "--plugin-root",
        help="exact Decky CE-Decky plugin directory; resolved from live/recorded authority when omitted",
    )
    install.add_argument("--decky-url", default=DEFAULT_DECKY_URL)
    install.add_argument("--replace", action="store_true", help="allow Decky to overwrite an existing exact plugin root")
    install.add_argument(
        "--remote", metavar="USER@HOST",
        help="install on a SteamOS device across the network, through the mirror of this checkout there",
    )
    install.add_argument(
        "--remote-root", default=DEFAULT_REMOTE_ROOT, metavar="PATH",
        help=f"where that mirror is, relative to the remote home (default: {DEFAULT_REMOTE_ROOT})",
    )
    install.add_argument("--timeout", type=float, default=30.0)
    install.add_argument(
        "--json", action="store_true",
        help="print the whole verified report; the default is one summary line",
    )
    return parser


# What the two names that also have to be paths may be spelled as. This is not
# what makes the command safe - every value interpolated into it is quoted for
# the remote shell - it is what keeps the mirror and the artifact plain names
# under the remote home: the artifact is `CE-Decky-v<version>.zip` and the
# mirror is a directory somebody chose, and neither may climb out of it.
#
# Quoting is the rule rather than this pattern because a path that has to be
# quoted is not a strange path: `--plugin-root` points at a Decky plugin
# directory, and a device whose home or library holds a space produces one. That
# argument used to be checked only for a leading `/` and written into the
# command as it stood, which broke such a path and left everything after it to
# the remote shell.
_REMOTE_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}$")


def install_through_mirror(
    package: Path, sha256_hex: str, *, remote: str, root: str, replace: bool,
    timeout: float, plugin_root: str | None = None, decky_url: str | None = None,
    as_json: bool = False,
) -> str:
    """Install this exact artifact on a device across the network, and say so.

    The install itself has to happen on the device: it talks to Decky's own
    socket there, resolves the plugin root from what that Decky reports, and
    waits on the backend generation that loader keeps. So this sends the
    artifact and runs the device's own copy of this helper against it, which is
    the arrangement `docs/REMOTE_TARGET.md` describes, done in one call instead
    of three commands with a 64 character digest pasted into the middle one.

    Two things it refuses rather than doing quietly. A mirror whose copy of this
    helper is not this file is a mirror that has not been synced, which is the
    failure that arrangement invites and which presents as a helper disagreeing
    with the source in front of you; the digest of this file is compared and a
    difference is named. And the artifact is sent every time rather than assumed
    to be there, because an install is about exact bytes and the copy at the far
    end is not evidence about the bytes this side just built.
    """
    ssh = shutil.which("ssh")
    if ssh is None:
        raise RuntimeError("ssh is not installed on this system")
    if not remote.strip():
        raise ValueError("--remote must name a user and host")
    if not _REMOTE_SAFE.match(root.strip()) or ".." in PurePosixPath(root).parts:
        raise ValueError("--remote-root must be a plain relative path under the remote home")
    if not _REMOTE_SAFE.match(package.name) or "/" in package.name:
        raise ValueError(f"this package's name cannot be sent as it is spelled: {package.name!r}")
    if plugin_root is not None and not plugin_root.startswith("/"):
        raise ValueError("--plugin-root must be an absolute path on the device")
    payload = package.read_bytes()
    if sha256(payload).hexdigest() != sha256_hex:
        raise ValueError("this package is not the SHA-256 that was named")

    here = sha256(Path(__file__).resolve().read_bytes()).hexdigest()
    read_digest = (
        "import hashlib,pathlib;"
        "print(hashlib.sha256(pathlib.Path('scripts/target_plugin_install.py').read_bytes()).hexdigest())"
    )
    there = _over_ssh(
        ssh, remote,
        f"cd {shlex.quote(root)} && python3 -c {shlex.quote(read_digest)}",
    )[0].strip()
    if there != here:
        raise RuntimeError(
            f"the mirror at {root} on {remote} is not this source: its copy of this helper is "
            f"{there[:12] or 'unreadable'} where this one is {here[:12]}. Sync it first, the way "
            "docs/REMOTE_TARGET.md describes"
        )

    name = package.name
    remote_package = f"{root}/artifacts/{name}"
    _over_ssh(
        ssh, remote,
        f"mkdir -p {shlex.quote(f'{root}/artifacts')} && cat > {shlex.quote(remote_package)}",
        payload,
    )
    # Every option this side accepts reaches the helper that does the work, as
    # argv rather than as text: `--plugin-root`, because the one install that
    # needs it is the first one on a device, which has no authority to resolve
    # and is exactly the install somebody is most likely to be driving from
    # another machine; and `--decky-url` and `--json`, which used to be accepted
    # here and dropped on the way, so a custom endpoint was silently ignored and
    # `--remote --json` answered a caller parsing JSON with a summary line.
    forwarded = [
        "install", f"artifacts/{name}", "--sha256", sha256_hex, "--timeout", str(timeout),
        *(["--plugin-root", plugin_root] if plugin_root else []),
        *(["--decky-url", decky_url] if decky_url else []),
        *(["--replace"] if replace else []),
        *(["--json"] if as_json else []),
    ]
    command = (
        f"cd {shlex.quote(root)} && timeout {int(timeout) + 90} "
        f"python3 scripts/target_plugin_install.py {shlex.join(forwarded)}"
    )
    out, warn = _over_ssh(ssh, remote, command, deadline=int(timeout) + 120)
    # What the far end wrote to stderr is something the caller has to read: that
    # helper reports an install it could not record there. It is relayed rather
    # than folded into the answer whenever the answer has a shape - a single
    # warning line turns a JSON document into something no parser accepts.
    if warn and as_json:
        print(warn, file=sys.stderr)
        return out.strip()
    return "\n".join(part for part in (out.strip(), warn) if part)


def _over_ssh(
    ssh: str, remote: str, command: str, payload: bytes = b"",
    deadline: int | None = None,
) -> tuple[str, str]:
    """One bounded SSH round trip, refusing a password prompt nothing is watching.

    What the far end wrote to each stream, kept apart, because a step whose
    answer is compared - the digest of the helper over there - must not have an
    SSH banner or a locale warning folded into it, and the step whose answer is
    handed to a parser must not either. The caller decides which of the two it
    is; what is on stderr is never simply dropped.
    """
    result = subprocess.run(
        [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", remote, command],
        input=payload, capture_output=True,
        timeout=deadline or REMOTE_STEP_TIMEOUT_SECONDS, check=False,
    )
    warn = (result.stderr or b"").decode("utf-8", "replace").strip()
    if result.returncode != 0:
        detail = warn.splitlines()
        raise RuntimeError(f"{remote}: {detail[-1] if detail else f'exit {result.returncode}'}")
    return (result.stdout or b"").decode("utf-8", "replace"), warn


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # A remote install runs on the device; this side only sends it there, so the
    # host requirement is the far end's and is enforced by the helper that runs
    # there. Asking for it here would refuse the ordinary arrangement this flag
    # exists for: a desktop driving a Steam Deck.
    if getattr(args, "remote", None) is None:
        try:
            host_platform.require("The plugin install helper")
        except host_platform.UnsupportedHost as exc:
            return host_platform.refuse(exc)
    try:
        if args.command == "authority":
            if args.timeout <= 0 or args.timeout > 30:
                raise ValueError("--timeout must be greater than zero and at most 30 seconds")
            result = resolve_install_authority(
                Path(args.records_root), args.decky_url.rstrip("/"), args.timeout,
            )
            json.dump(result, sys.stdout, indent=2, sort_keys=True)
            sys.stdout.write("\n")
            return 0
        if len(args.sha256) != 64 or any(character not in "0123456789abcdef" for character in args.sha256):
            raise ValueError("--sha256 must be 64 lowercase hexadecimal characters")
        if args.timeout <= 0 or args.timeout > 300:
            raise ValueError("--timeout must be greater than zero and at most 300 seconds")
        if args.remote is not None:
            print(install_through_mirror(
                Path(args.package).expanduser(), args.sha256,
                remote=args.remote, root=args.remote_root, replace=args.replace,
                timeout=args.timeout, plugin_root=args.plugin_root,
                decky_url=args.decky_url.rstrip("/"), as_json=args.json,
            ))
            return 0
        plugin_root = Path(args.plugin_root).expanduser() if args.plugin_root else Path(str(
            resolve_install_authority(INSTALL_RECORDS, args.decky_url.rstrip("/"), min(args.timeout, 5.0))["plugin_root"]
        ))
        started = time.monotonic()
        result = install_package(
            Path(args.package).expanduser(),
            args.sha256,
            plugin_root,
            args.decky_url.rstrip("/"),
            args.replace,
            args.timeout,
        )
        # What this cost, from the helper itself. An install is bounded by a
        # timeout sized from what one costs on the host running it, and until
        # this was printed that figure had to be taken from the loader's journal
        # by hand, which is not a source anybody can reproduce from a row in
        # `docs/DEVELOPMENT.md`.
        result["elapsed_s"] = round(time.monotonic() - started, 1)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        operation = "target install authority" if args.command == "authority" else "target plugin install"
        print(f"{operation}: {exc}", file=sys.stderr)
        return 2
    try:
        record_install(result)
    except OSError as exc:
        # The plugin is installed and verified by this point. Losing the record
        # costs a later `authority` its answer; it does not make this install
        # anything other than the success it already is, and reporting it as a
        # failure invites repeating a device mutation that already happened.
        print(f"target plugin install: install record not written: {exc}", file=sys.stderr)
    if args.json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
