#!/usr/bin/env python3
"""Emit one bounded read-only CE Decky state snapshot for target correlation.

The probe reads only plugin-owned config/profile/session/launch records plus an
allowlisted process marker used by the managed CE installer.  It never calls a
mutation RPC, never prints launch-option contents, and never emits installer
operation markers or environment values.  Take exact DECKY_USER_HOME and DECKY_PLUGIN_SETTINGS_DIR values from
``target_plugin_install.py authority``, which reads them from the
authenticated live backend. ``target_decky_env_probe.py`` reports the same
paths from procfs and is the fallback, not the first route: SteamOS may deny a
same-user sibling process the environment bytes it needs.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import os
import stat
import sys
import time

if __package__:
    from . import host_platform
else:
    import host_platform

ROOT = Path(__file__).resolve().parents[1]
MODULE_ROOT = ROOT / "py_modules"
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from ce_decky.ce_launch import CELaunchSupervisor, observe_running_app_ids  # noqa: E402
from ce_decky.config import ConfigStore  # noqa: E402
from ce_decky.game_run_holds import GameRunHolds  # noqa: E402
from ce_decky.profiles import GameProfile, ProfileStore  # noqa: E402
from ce_decky.session_protocol import SessionStore  # noqa: E402

# A device holds a handful of profiles, and this is a diagnostic rather than an
# export. The cap keeps one unusual state store from filling a report.
MAX_REPORTED_PROFILES = 32

# How long a cached provider listing page stays fresh. It mirrors
# `FEARLESS_INDEX_MAX_AGE_SECONDS` in `py_modules/ce_decky/catalog.py` and is
# repeated rather than imported because that module pulls in the HTML and XML
# parsers, which a probe running under the system interpreter has no reason to
# need. `tests/test_target_state_probe_providers.py` holds the two equal.
PROVIDER_PAGE_FRESHNESS_SECONDS = 24 * 60 * 60
# One cache file contributes this much of itself to the report: the index is
# hundreds of kilobytes and the question is its shape, never its contents.
MAX_PROVIDER_CACHE_BYTES = 64 * 1024 * 1024
# The Search marker's own file. It mirrors `FEARLESS_SEARCH_MARKER_NAME` in
# `py_modules/ce_decky/catalog.py`, and is repeated here for the same reason the
# freshness window above is; `tests/test_target_state_probe_providers.py` holds
# the two equal.
SEARCH_MARKER_NAME = "fearless-search.json"
# All the marker file is allowed to be before this refuses to read it. It holds
# one timestamp; anything larger is not the file this understands, and a probe
# reads bounded amounts or nothing.
MAX_SEARCH_MARKER_BYTES = 4096

MAX_ENVIRON_BYTES = 256 * 1024
MAX_SCANNED_PROCESSES = 32768
HEARTBEAT_TTL_MS = 3000
FUTURE_SKEW_MS = 5000


def _age_hours(epoch: float | None, now: float) -> float | None:
    if epoch is None:
        return None
    return round(max(0.0, now - epoch) / 3600, 1)


def _searched_moment(value: object, now: float) -> tuple[float | None, bool]:
    """One recorded Search moment, under the exact rule the plugin loads by.

    Returns the usable moment and whether one was there and was refused. The
    plugin refuses a marker that is not a finite number, is not positive, or is
    further into the future than a page stays fresh, and reads it as never
    searched; a probe that accepted what the plugin refuses would report the
    background pass armed while the live backend has it disarmed, which is the
    one moment this report exists for. `NaN` and `Infinity` are refused by the
    same comparison, and `json` parses both.
    """
    if value is None:
        return None, False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, True
    moment = float(value)
    if not 0 < moment <= now + PROVIDER_PAGE_FRESHNESS_SECONDS:
        return None, True
    return moment, False


def _search_marker_moment(cache_root: Path, now: float) -> tuple[float | None, bool]:
    """What the Search marker's own small file says, if it holds anything usable.

    The plugin records that Search was used in two places: inside the listing
    index, where an ordinary write carries it for free, and in this file, which
    a search writes as it happens and which the unload prologue can write when a
    crawl worker holds the index. The plugin reads the newer of the two, so a
    report that read only the index would answer a question about the background
    pass with a moment the plugin itself no longer believes.
    """
    path = cache_root / SEARCH_MARKER_NAME
    try:
        if path.stat().st_size > MAX_SEARCH_MARKER_BYTES:
            return None, False
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, False
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        return None, False
    return _searched_moment(raw.get("searched"), now)


def _fearless_index_summary(
    path: Path, now: float, searched_marker: tuple[float | None, bool] = (None, False),
) -> dict[str, object]:
    """What the FearLess listing index holds, without holding any of it.

    The numbers a provider question actually turns on: how much of the listing
    is indexed, how old the oldest and newest page of it are, how many are due a
    refresh, and whether Search has been used recently enough for the background
    pass to be armed at all. Reading them by hand meant a `python3 -` over this
    file every time, which is the shape the tracked-helper rule exists to
    remove - and a figure read that way is not a figure this project may record.

    `searched_marker` is the same moment out of the marker's own file, with
    whether that file held one the plugin refuses; the newer of the two usable
    moments is the one the plugin acts on.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("FearLess index cache is not an object")
    pages = raw.get("pages")
    fetched = raw.get("fetched")
    pages = pages if isinstance(pages, dict) else {}
    fetched = fetched if isinstance(fetched, dict) else {}
    stamps = [float(value) for value in fetched.values() if isinstance(value, (int, float)) and not isinstance(value, bool)]
    total_pages = raw.get("total_pages")
    total_pages = total_pages if isinstance(total_pages, int) and not isinstance(total_pages, bool) else None
    searched, index_rejected = _searched_moment(raw.get("searched"), now)
    marker, marker_rejected = searched_marker
    if marker is not None:
        searched = marker if searched is None else max(searched, marker)
    # Due means the same thing it means to the crawl: never read, or read longer
    # ago than a page stays fresh. A page with no timestamp is due by definition.
    cutoff = now - PROVIDER_PAGE_FRESHNESS_SECONDS
    dated = sum(1 for value in stamps if value > cutoff)
    indexed = len(pages)
    return {
        "indexed_pages": indexed,
        "total_pages": total_pages,
        "topics": raw.get("total_topics") if isinstance(raw.get("total_topics"), int) else None,
        "page_step": raw.get("page_step") if isinstance(raw.get("page_step"), int) else None,
        "oldest_page_age_h": _age_hours(min(stamps), now) if stamps else None,
        "newest_page_age_h": _age_hours(max(stamps), now) if stamps else None,
        # Everything the crawl still owes: the pages it has never read plus the
        # ones that have aged out of it.
        "pages_due": None if total_pages is None else max(0, total_pages - dated),
        "freshness_window_s": PROVIDER_PAGE_FRESHNESS_SECONDS,
        "searched_age_h": _age_hours(searched, now),
        # A moment the plugin refuses is not the same answer as no moment at
        # all: it is a cache this device cannot act on, and after a clock
        # correction it is the thing a report is being read to find.
        "searched_rejected": index_rejected or marker_rejected,
        # The background pass is armed by Search and goes quiet on its own; a
        # scheduler that is running and one that is running and armed look
        # identical from outside the plugin.
        "background_pass_armed": (
            searched is not None and now - searched <= PROVIDER_PAGE_FRESHNESS_SECONDS
        ),
    }


def _provider_caches(cache_root: Path, now: float) -> dict[str, object]:
    """Every provider cache this device holds, by shape rather than by content.

    The rest are one document each - a sitemap, a game list, a repository index
    - so size and age is the whole of what a report needs from them. The
    FearLess listing is the one with structure worth reporting, because the
    background crawl's whole behaviour is a function of it.
    """
    report: dict[str, object] = {"cache_root": str(cache_root), "files": {}}
    files: dict[str, object] = report["files"]  # type: ignore[assignment]
    for name in (
        "fearless-index.json",
        SEARCH_MARKER_NAME,
        "playground-sitemap.json",
        "thecheatscript-sitemap.json",
        "vgtimes-games.json",
        "github-repository-index.json",
        "provider-results.json",
        "providers.json",
    ):
        path = cache_root / name
        try:
            stats = path.stat()
        except OSError:
            files[name] = {"present": False}
            continue
        entry: dict[str, object] = {
            "present": True,
            "bytes": stats.st_size,
            "age_h": _age_hours(stats.st_mtime, now),
        }
        if name == "fearless-index.json" and stats.st_size <= MAX_PROVIDER_CACHE_BYTES:
            try:
                entry["listing"] = _fearless_index_summary(path, now, _search_marker_moment(cache_root, now))
            except (OSError, ValueError, TypeError) as exc:
                entry["listing_error"] = str(exc)[:256]
        files[name] = entry
    return report


def _directory(raw: Path, label: str) -> Path:
    raw = raw.expanduser()
    if raw.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    path = raw.resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"{label} must be a directory")
    return path


def _profile_summary(profile: GameProfile | None) -> dict[str, object] | None:
    if profile is None:
        return None
    return {
        "app_id": profile.app_id,
        "name": profile.name,
        "is_shortcut": profile.is_shortcut,
        "table_sha256": profile.table_sha256,
        "previous_table_sha256": profile.previous_table_sha256,
        "target_process": profile.target_process,
        "execution_consent_sha256": profile.execution_consent_sha256,
        "autoload_enabled": profile.autoload_enabled,
        "table_library_count": len(profile.table_library),
        "table_history_count": len(profile.table_history),
        "startup_count": len(profile.startup),
        "remembered_count": len(profile.remembered),
        "pinned_count": len(profile.pinned),
    }


def _read_environ(path: Path) -> bytes | None:
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENVIRON_BYTES:
        return None
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    return payload if len(payload) <= MAX_ENVIRON_BYTES else None


def _managed_installer_processes(proc_root: Path) -> list[dict[str, int]]:
    marker = b"CE_DECKY_MANAGED_INSTALLER="
    rows: list[dict[str, int]] = []
    try:
        entries = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError as exc:
        raise ValueError(f"cannot enumerate process table: {exc}") from exc
    if len(entries) > MAX_SCANNED_PROCESSES:
        raise ValueError("process table exceeds target probe scan bound")
    for entry in sorted(entries, key=lambda item: int(item.name)):
        payload = _read_environ(entry / "environ")
        if not payload or not any(part.startswith(marker) for part in payload.split(b"\0")):
            continue
        pid = int(entry.name)
        pgid = 0
        try:
            pgid = os.getpgid(pid) if proc_root == Path("/proc") else pid
        except OSError:
            pgid = 0
        rows.append({"pid": pid, "pgid": pgid})
    return rows


def _runtime_snapshot(
    store: SessionStore,
    profile: GameProfile | None,
    config_sha: str | None,
    app_id: int,
    launch_observation: dict[str, object] | None = None,
) -> dict[str, object]:
    try:
        prepared = store.load_current(app_id)
    except ValueError as exc:
        return {"state": "invalid", "error": str(exc)[:512]}
    if prepared is None:
        return {"state": "absent", "prepared": None, "status": None, "identity_current": False, "connected": False}
    try:
        descriptor = store.validated_descriptor(prepared)
        status, status_mtime_ns = store.read_status_observation(prepared)
        next_generation = store.next_generation(prepared)
    except ValueError as exc:
        return {
            "state": "invalid",
            "prepared": prepared.as_dict(),
            "error": str(exc)[:512],
            "identity_current": False,
            "connected": False,
        }

    identity_current = bool(
        profile
        and profile.table_sha256 == prepared.table_sha256
        and profile.execution_consent_sha256 == prepared.table_sha256
        and profile.target_process == descriptor.target_process
        and config_sha == prepared.ce_sha256
        and prepared.is_shortcut is not None
        and profile.is_shortcut == prepared.is_shortcut
    )
    age_ms: int | None = None
    fresh = False
    clock_skew = False
    if status is not None and status_mtime_ns is not None:
        delta_ms = int((time.time() - status_mtime_ns / 1_000_000_000) * 1000)
        if delta_ms < -FUTURE_SKEW_MS:
            age_ms = 0
            clock_skew = True
        else:
            age_ms = max(0, delta_ms)
            fresh = age_ms <= HEARTBEAT_TTL_MS
    status_public = None
    if status is not None:
        status_public = {
            "session_id": status.session_id,
            "app_id": status.app_id,
            "ce_sha256": status.ce_sha256,
            "table_sha256": status.table_sha256,
            "descriptor_sha256": status.descriptor_sha256,
            "attached": status.attached,
            "target_process": status.target_process,
            "opened_process_id": status.opened_process_id,
            "address_list_count": status.address_list_count,
            "table_load_state": status.table_load_state,
            "table_load_error": status.table_load_error,
            "result_count": len(status.results),
            "processes": [[pid, name] for pid, name in status.processes],
        }
    launch_state = str((launch_observation or {}).get("state") or "unknown")
    launch_record = (launch_observation or {}).get("record")
    launch_session_id = launch_record.get("session_id") if isinstance(launch_record, dict) else None
    owned_launch_proven_gone = launch_state == "gone_or_reused" and launch_session_id == prepared.session_id
    terminal_reason = "owned_bridge_process_gone" if owned_launch_proven_gone else None
    return {
        "state": "current" if identity_current else "stale",
        "prepared": prepared.as_dict(),
        "status": status_public,
        "next_generation": next_generation,
        "status_age_ms": age_ms,
        "status_fresh": fresh,
        "status_clock_skew": clock_skew,
        "identity_current": identity_current,
        "owned_launch_state": launch_state,
        "terminal_reason": terminal_reason,
        "connected": bool(
            status is not None
            and fresh
            and not clock_skew
            and identity_current
            and not owned_launch_proven_gone
        ),
    }


def probe(home: Path, settings_dir: Path, app_id: int | None, proc_root: Path = Path("/proc")) -> dict[str, object]:
    home = _directory(home, "DECKY_USER_HOME")
    settings_dir = _directory(settings_dir, "DECKY_PLUGIN_SETTINGS_DIR")
    proc_root = _directory(proc_root, "proc root")
    managed_root = home / ".cheat-engine-decky"
    state_root = managed_root / "state"
    ce_root = managed_root / "ce"

    config_error: str | None = None
    try:
        config = ConfigStore(settings_dir / "config.json").load()
        config_public: dict[str, object] = {
            "configured": bool(config.imported_ce_executable),
            "executable": config.imported_ce_executable,
            "root": config.imported_ce_root,
            "sha256": config.imported_ce_sha256,
        }
    except (OSError, ValueError) as exc:
        config = None
        config_error = str(exc)[:512]
        config_public = {"configured": False, "executable": None, "root": None, "sha256": None}

    # This walks the process table, and `_managed_installer_processes` below
    # walks it again. Collapsing the two into one traversal was considered and
    # rejected, with the measurement rather than an impression: on this device,
    # 345 processes, the second sweep costs 3.9 ms against the first one's 13.2,
    # so one hand-run diagnostic saves about four milliseconds. What it would
    # cost is the point of the field. `observe_running_app_ids` is the
    # production observer, so this reports the AppIDs the plugin itself sees; a
    # merged traversal written here would report the ones this helper sees, and
    # a diagnostic that quietly answers a different question from the code it
    # describes is worth much more than four milliseconds. The skew between the
    # two sections is the same few milliseconds, and they are independent facts
    # in a read-only snapshot, so nothing a reader concludes turns on it.
    running_error: str | None = None
    try:
        observation = observe_running_app_ids(proc_root=proc_root)
        running_app_ids = list(observation.app_ids) if observation.available else []
        if not observation.available:
            running_error = observation.reason or "the process table could not be read"
    except (OSError, ValueError) as exc:
        running_app_ids = []
        running_error = str(exc)[:512]

    def _bounded_profiles(items: list[GameProfile], running: list[int]) -> list[dict[str, object]]:
        """The bound, with the running games inside it rather than sorted out of it.

        Profiles come back sorted by name, and the cap took the first of them.
        So on a device with more profiles than the cap this could report an
        AppID as running and omit that AppID's profile, purely because its name
        sorts late, which is precisely the second call this field exists to
        remove. The running games go first, the rest fill what is left in the
        ordinary order, and nothing is reported twice.
        """
        wanted = set(running)
        ordered = [item for item in items if item.app_id in wanted]
        ordered += [item for item in items if item.app_id not in wanted]
        return [_profile_summary(item) for item in ordered[:MAX_REPORTED_PROFILES]]

    profile_store = ProfileStore(state_root / "profiles.json")
    profile_error: str | None = None
    try:
        profiles = profile_store.list_profiles()
    except (OSError, ValueError) as exc:
        profiles = []
        profile_error = str(exc)[:512]

    # An unreadable profile store reports nothing rather than an empty list that
    # would read as a device with no profiles; `profile_state_error` says why.
    reported_profiles = _bounded_profiles(profiles, running_app_ids) if profile_error is None else []

    session_store = SessionStore(state_root, home)
    try:
        inventory = session_store.inventory()
        session_inventory_error: str | None = None
    except (OSError, ValueError) as exc:
        inventory = {"apps": [], "total_sessions": 0, "errors": []}
        session_inventory_error = str(exc)[:512]

    # Which games a stop holds, as the file has them. The backend's own view is
    # this plus any hold it could not save, which only its capability answer
    # carries; `run_holds_error` is a file the backend refuses every start on.
    try:
        run_holds = [
            {
                "app_id": held_app,
                "kind": kind,
                "since": hold.since,
                "age_minutes": round((time.time() - hold.since) / 60, 1),
                "unsettled": hold.unsettled,
                "targets": list(hold.targets) if hold.targets is not None else None,
                "processes": None if hold.identities is None else len(hold.identities),
            }
            for held_app, kinds in sorted(GameRunHolds(state_root / "game_run_holds.json").load().items())
            for kind, hold in sorted(kinds.items())
        ]
        run_holds_error: str | None = None
    except (OSError, ValueError) as exc:
        run_holds = []
        run_holds_error = str(exc)[:512]

    try:
        installer_processes = _managed_installer_processes(proc_root)
        installer_process_error: str | None = None
    except ValueError as exc:
        installer_processes = []
        installer_process_error = str(exc)[:512]

    try:
        provider_caches = _provider_caches(managed_root / "cache", time.time())
        provider_cache_error: str | None = None
    except OSError as exc:
        provider_caches = {"cache_root": str(managed_root / "cache"), "files": {}}
        provider_cache_error = str(exc)[:512]

    report: dict[str, object] = {
        "schema": 1,
        "mode": "read_only_target_state",
        "user_home": str(home),
        "settings_dir": str(settings_dir),
        "managed_root": str(managed_root),
        "config": config_public,
        "config_error": config_error,
        "profile_count": len(profiles),
        # What each profile actually selects, not just how many there are. A
        # count answers nothing a setup question asks: which table a game is
        # configured with, whether its execution consent is bound to that exact
        # table, and what process it attaches to. Bounded, and the same summary
        # `--appid` already produced for one of them.
        "profiles": reported_profiles,
        "profiles_reported": len(reported_profiles),
        "profile_state_error": profile_error,
        # Which AppIDs are running right now, so this report can be reached
        # without already knowing the answer to the question it is asked to
        # correlate. Without it the only route from "a game is running" to
        # "which profile it uses" was an ad-hoc snippet against the production
        # module, which is how the AppID for a measurement was found twice.
        "running_app_ids": running_app_ids,
        "running_app_ids_error": running_error,
        "session_inventory": inventory,
        "session_inventory_error": session_inventory_error,
        "run_holds": run_holds,
        "run_holds_error": run_holds_error,
        "managed_installer_processes": installer_processes,
        "managed_installer_process_error": installer_process_error,
        # What the provider caches hold, which is the other half of "why did
        # search answer that": how much of the FearLess listing is indexed, how
        # stale it is, how much it still owes, and whether its background pass
        # is armed at all.
        "provider_caches": provider_caches,
        "provider_cache_error": provider_cache_error,
        "privacy": "launch-option values and installer environment markers are never emitted",
    }

    if app_id is not None:
        if isinstance(app_id, bool) or app_id < 1 or app_id > 0xFFFFFFFF:
            raise ValueError("AppID must be between 1 and 4294967295")
        profile = next((item for item in profiles if item.app_id == app_id), None) if profile_error is None else None
        report["app_id"] = app_id
        report["profile"] = _profile_summary(profile)
        launch = CELaunchSupervisor(home, ce_root, state_root, logging.getLogger("target-state-probe"))
        launch_observation = launch.inspect_owned_launch_record(app_id, proc_root=proc_root)
        report["owned_ce_launch"] = launch_observation
        report["runtime"] = _runtime_snapshot(
            session_store,
            profile,
            config.imported_ce_sha256 if config is not None else None,
            app_id,
            launch_observation,
        )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True, help="exact DECKY_USER_HOME; `target_plugin_install.py authority` reports it as user_home")
    parser.add_argument("--settings-dir", type=Path, required=True, help="exact DECKY_PLUGIN_SETTINGS_DIR; `target_plugin_install.py authority` reports it as settings_dir")
    parser.add_argument("--appid", type=int, help="exact Steam/non-Steam AppID for profile/session/runtime correlation")
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        host_platform.require("The plugin state probe")
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    try:
        report = probe(args.home, args.settings_dir, args.appid, args.proc_root)
    except (OSError, ValueError) as exc:
        print(f"target state probe: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    # The helper is an observation tool: corrupt/unreadable state is useful gate
    # evidence and remains represented in JSON rather than making capture fail.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
