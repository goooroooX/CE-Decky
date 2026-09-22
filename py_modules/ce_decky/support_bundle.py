"""Collect one archive that answers a bug report without the device.

A CE Decky problem is almost never visible in one place. The plugin log says
what the backend did, the launch log says what Proton and Cheat Engine printed,
the session directory says which exact table and target a Cheat Engine was given,
the profile store says what the user had chosen, the provider diagnostics say why
a search returned nothing, and the panel's own ring buffer says what was on
screen. Asking a user to find and attach seven of those from Game Mode is asking
for a report that never arrives.

So this walks all of them once, into one bounded ZIP in the Steam user's home,
and every collection failure becomes a line in the manifest rather than a reason
for the archive not to exist: a bundle that is missing one file is still worth
reading, and the thing that could not be read is usually the defect.

Nothing here mutates plugin state. Every read is bounded, refuses symlinks, and
truncates rather than refusing an oversized file, because the tail of a log is
what matters and the head of a state file is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import platform
import stat
import tempfile
import time
import zipfile

from .activity_log import is_sensitive_field, safe_log_text
from . import frontend_journal, journal_records
from .paths import PluginPaths


SCHEMA = 1
BUNDLE_PREFIX = "ce-decky-support"

# One log file contributes at most this much, taken from its end.
MAX_LOG_TAIL_BYTES = 1024 * 1024
# One state or diagnostics file contributes at most this much, from its start.
MAX_STATE_BYTES = 2 * 1024 * 1024
# One cheat table contributes at most this much. Tables are XML and compress
# hard, and a table larger than this is not the one a parsing report is about.
MAX_TABLE_BYTES = 4 * 1024 * 1024
# Everything collected, before compression. A bundle has to be attachable to an
# issue, so this is a budget and not just a safety net.
MAX_TOTAL_BYTES = 32 * 1024 * 1024
# Newest plugin log files kept. Decky starts a new one on every plugin load, so
# a day of reload-heavy use is dozens of small files and the oldest are noise.
MAX_PLUGIN_LOG_FILES = 25
# Per-game Proton/Cheat Engine launch logs kept.
MAX_LAUNCH_LOG_FILES = 20
# Session directories walked, newest first.
MAX_SESSION_DIRECTORIES = 12
# Cheat tables copied in whole.
MAX_BUNDLED_TABLES = 6
# Panel log entries accepted from the frontend.
MAX_FRONTEND_ENTRIES = 2000
# The panel's flushed journal contributes at most this much, from its end. It is
# the only record of a panel that stopped before anyone could ask it for one, so
# it is collected even when the live ring above arrived full.
MAX_FRONTEND_JOURNAL_BYTES = 512 * 1024
# This plugin's own system-journal records contribute at most this much. Named
# apart from the panel's journal above, because the two are different records
# and a shared word between adjacent budgets is how the wrong one gets used.
#
# Sized so `journal_records.MAX_LINES` is the bound that actually applies. That
# line cap is the deliberate one and it is reported; this is a backstop against
# a pathological line length. At 512 KiB it was not a backstop: one ordinary
# collection here kept 4000 lines and wrote 3386 of them, so the byte cap silently
# removed 614 records from the one log that survives a plugin reload, while the
# whole-bundle budget it is spending had 31 of its 32 MiB left. Four thousand
# lines measured about 620 KB on that device, so this holds them with room for
# lines several times longer.
MAX_SYSTEM_JOURNAL_BYTES = 4 * 1024 * 1024
# Support bundles left in the home directory before the oldest are removed.
MAX_RETAINED_BUNDLES = 5

# Files inside a session or self-test directory that describe the run. The
# session's own `table.ct` snapshot is deliberately absent here: the active
# tables are collected once, by digest, instead of once per session.
SESSION_MEMBERS = ("session.json", "descriptor.txt", "control.txt", "status.txt")

# Environment variables recorded verbatim. Everything else is omitted rather
# than redacted: an allowlist cannot leak a variable nobody thought about.
ENVIRONMENT_ALLOWLIST = (
    "DECKY_VERSION",
    "DECKY_USER",
    "DECKY_USER_HOME",
    "DECKY_HOME",
    "DECKY_PLUGIN_NAME",
    "DECKY_PLUGIN_VERSION",
    "DECKY_PLUGIN_AUTHOR",
    "DECKY_PLUGIN_DIR",
    "DECKY_PLUGIN_LOG",
    "DECKY_PLUGIN_LOG_DIR",
    "DECKY_PLUGIN_SETTINGS_DIR",
    "DECKY_PLUGIN_RUNTIME_DIR",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SteamAppId",
    "SteamDeck",
    "SteamOS",
    "XDG_RUNTIME_DIR",
)


@dataclass
class _Collector:
    """Accumulates archive members and the reasons some are missing."""

    members: list[tuple[str, bytes]]
    notes: list[dict[str, object]]
    total_bytes: int = 0

    def note(self, name: str, reason: str, *, kind: str = "omitted", **fields: object) -> None:
        """Record why a member is missing, or why an included one is partial.

        Three different facts, and the panel counts one of them:

        - `omitted` is something that exists and is not in the archive. That is
          the one worth a reader's attention;
        - `truncated` and `partial` are in the archive and readable, and
          reporting them as items that could not be collected made a healthy
          bundle look damaged;
        - `absent` is nothing to collect. A device that has never launched
          Cheat Engine has no launch logs, and a plugin that has just been
          reloaded has no log file yet; both were counted as failures, so a
          healthy Steam Deck reported two items it could not collect. The note
          stays, because a reader looking for those files should find out why
          they are not there rather than wonder.
        """
        self.notes.append({
            "member": name,
            "kind": kind,
            "reason": safe_log_text(reason, 320),
            **{key: _plain(value) for key, value in fields.items()},
        })

    def add_bytes(self, name: str, data: bytes, *, truncated: bool = False) -> bool:
        """Add one member unless the whole-bundle budget is already spent."""
        if self.total_bytes + len(data) > MAX_TOTAL_BYTES:
            self.note(name, "the bundle size budget was already spent", bytes=len(data))
            return False
        self.members.append((name, data))
        self.total_bytes += len(data)
        if truncated:
            self.note(name, "included, truncated to its size limit", kind="truncated", bytes=len(data))
        return True

    def add_json(self, name: str, value: object) -> bool:
        try:
            data = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self.note(name, f"could not be serialized: {exc}")
            return False
        return self.add_bytes(name, data)

    def add_file(self, name: str, path: Path, *, max_bytes: int, tail: bool) -> bool:
        try:
            data, truncated = read_bounded(path, max_bytes=max_bytes, tail=tail)
        except (OSError, ValueError) as exc:
            self.note(name, f"{type(exc).__name__}: {exc}", path=str(path))
            return False
        if data is None:
            return False
        return self.add_bytes(name, data, truncated=truncated)


def _plain(value: object) -> object:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return safe_log_text(value, 320)


def read_bounded(path: Path, *, max_bytes: int, tail: bool) -> tuple[bytes | None, bool]:
    """Read at most `max_bytes` of a regular file, from its end when `tail`.

    Returns `(None, False)` for a file that is not there. A file larger than the
    bound is truncated rather than refused: for a log the last megabyte is the
    interesting one, and for a state file the first is.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if path.is_symlink():
        raise ValueError("path is a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None, False
    except OSError as exc:
        raise ValueError(f"could not be opened: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("is not a regular file")
        truncated = info.st_size > max_bytes
        if truncated and tail:
            os.lseek(descriptor, info.st_size - max_bytes, os.SEEK_SET)
        chunks = bytearray()
        while len(chunks) < max_bytes:
            chunk = os.read(descriptor, min(512 * 1024, max_bytes - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks), truncated
    finally:
        os.close(descriptor)


def managed_child(root: Path, *parts: str) -> Path:
    """Resolve a path under a managed root with every component checked.

    `read_bounded()` opens the final component with `O_NOFOLLOW`, which says
    nothing about the directories above it: a symlinked shard or digest
    directory would redirect the read out of the managed tree entirely. Normal
    operation cannot produce one, so meeting one is either damage or someone
    else's doing, and the diagnostic read refuses either.
    """
    current = root
    if current.is_symlink() or not current.is_dir():
        raise ValueError("managed root is not a directory")
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"managed directory is a symlink: {part}")
        if not current.is_dir():
            raise ValueError(f"managed directory is missing: {part}")
    return current / parts[-1]


def _newest_files(directory: Path, limit: int, *, suffix: str | None = None) -> list[Path]:
    """Regular files in one directory, newest first, bounded."""
    if directory.is_symlink() or not directory.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                # A directory with an implausible number of entries is a defect
                # of its own; read a bounded prefix rather than walking it all.
                if index > 4096:
                    break
                if entry.is_symlink() or not entry.is_file():
                    continue
                if suffix is not None and not entry.name.endswith(suffix):
                    continue
                try:
                    found.append((entry.stat().st_mtime, Path(entry.path)))
                except OSError:
                    continue
    except OSError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:limit]]


def _newest_directories(directory: Path, limit: int) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index > 4096:
                    break
                if entry.is_symlink() or not entry.is_dir():
                    continue
                try:
                    found.append((entry.stat().st_mtime, Path(entry.path)))
                except OSError:
                    continue
    except OSError:
        return []
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:limit]]


def normalize_frontend_log(raw: object) -> list[dict[str, object]]:
    """Accept the panel's ring buffer, dropping anything that is not its shape.

    The panel is CE Decky's own code, but this still crosses an RPC boundary and
    ends up written to a file the user attaches in public. One malformed entry
    is dropped alone; a malformed argument produces an empty log and a manifest
    note, never a failed bundle.

    The panel redacts secret-shaped field names before it records them, and this
    applies the same rule again rather than trusting that: an RPC argument is a
    trust boundary whatever is expected to be on the other side of it, and the
    cost of being wrong here is a credential inside a file attached to a public
    issue.
    """
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, object]] = []
    for item in raw[:MAX_FRONTEND_ENTRIES]:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields")
        recorded: dict[str, str] = {}
        if isinstance(fields, dict):
            for key in sorted(fields)[:48]:
                name = safe_log_text(key, 64)
                recorded[name] = "<redacted>" if is_sensitive_field(name) else safe_log_text(fields[key], 320)
        level = item.get("level")
        entries.append({
            "at": safe_log_text(item.get("at"), 40),
            "level": level if level in {"info", "warning", "error"} else "info",
            "event": safe_log_text(item.get("event"), 96),
            "fields": recorded,
        })
    return entries


def _environment() -> dict[str, object]:
    values = {name: os.environ.get(name) for name in ENVIRONMENT_ALLOWLIST if os.environ.get(name) is not None}
    return {
        "variables": values,
        "omitted_variable_count": max(0, len(os.environ) - len(values)),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "cwd": str(Path.cwd()),
    }


def _disk_usage(paths: PluginPaths) -> dict[str, object]:
    usage: dict[str, object] = {}
    for name, path in (
        ("user_home", paths.user_home),
        ("managed_root", paths.managed_root),
        ("log_dir", paths.log_dir),
        ("settings_dir", paths.settings_dir),
    ):
        try:
            stats = os.statvfs(path)
            usage[name] = {
                "path": str(path),
                "free_bytes": stats.f_bavail * stats.f_frsize,
                "total_bytes": stats.f_blocks * stats.f_frsize,
            }
        except OSError as exc:
            usage[name] = {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return usage


def _readme(archive_name: str) -> bytes:
    """The first thing a maintainer opens, and the first thing a user reads."""
    return (
        f"{archive_name}\n"
        "\n"
        "This archive was produced by CE Decky's Advanced screen for a bug report.\n"
        "Attach it whole to a GitHub issue and describe what you were doing.\n"
        "\n"
        "What is in it\n"
        "  manifest.json          what was collected, and why anything is missing\n"
        "  summary.txt            the short version: versions, identities, current state\n"
        "  logs/plugin/           CE Decky's own backend log, newest files first\n"
        "  logs/journal.txt       the same backend records, kept by the system journal.\n"
        "                         CE Decky's own records only: Decky's loader writes\n"
        "                         its start and stop lines as root, and this plugin\n"
        "                         cannot read those\n"
        "  logs/ce-launch/        what Proton and Cheat Engine printed, per game\n"
        "  logs/frontend.json     what the Quick Access panel did in this session\n"
        "  logs/frontend-journal.jsonl  the same, written as it happened, so it\n"
        "                         survives a panel that stopped\n"
        "  diagnostics/           the backend's own state snapshots and self-test\n"
        "  state/                 your settings, game profiles, and session records\n"
        "  tables/                the cheat tables currently selected for your games\n"
        "\n"
        "What is NOT in it\n"
        "  No password you typed for an archive, and no credential of any kind.\n"
        "  No Cheat Engine binary, no downloaded installer, and no game file.\n"
        "\n"
        "What you may want to remove before attaching it\n"
        "  It contains the names and AppIDs of the games you configured, the paths\n"
        "  these files live under, this device's host name on every journal line,\n"
        "  and under tables/ the cheat tables themselves.\n"
        "  Delete anything you would rather not publish; the rest still reads.\n"
    ).encode("utf-8")


def _summary(payload: dict[str, object]) -> bytes:
    """A plain-text digest, so triage does not begin by parsing JSON."""
    status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    self_test = payload.get("self_test") if isinstance(payload.get("self_test"), dict) else {}
    ce = status.get("ce") if isinstance(status.get("ce"), dict) else {}
    profiles = status.get("profiles") if isinstance(status.get("profiles"), list) else []
    tables = status.get("tables") if isinstance(status.get("tables"), list) else []

    lines = [
        f"CE Decky {status.get('version', 'unknown')} support bundle",
        f"collected  {payload.get('created_at', 'unknown')}",
        f"uptime     {diagnostics.get('uptime_s', 'unknown')}s",
        f"platform   {platform.platform()}",
        "",
        "Cheat Engine",
        f"  registered {bool(ce.get('configured'))}   valid {bool(ce.get('valid'))}",
        f"  version    {ce.get('version') or 'unknown'}",
        f"  executable {ce.get('executable') or 'none'}",
        f"  sha256     {ce.get('sha256') or 'none'}",
        f"  reason     {ce.get('reason') or 'none'}",
        "",
        f"Self-test  {'PASS' if self_test.get('ok') else 'FAIL'}",
    ]
    checks = self_test.get("checks") if isinstance(self_test.get("checks"), list) else []
    for check in checks:
        if isinstance(check, dict) and not check.get("ok"):
            lines.append(f"  FAILED {check.get('name')}: {check.get('detail')}")
    lines += [
        "",
        f"Tables imported  {len(tables)}",
        f"Game profiles    {len(profiles)}",
    ]
    for profile in profiles[:24]:
        if not isinstance(profile, dict):
            continue
        digest = profile.get("table_sha256")
        lines.append(
            f"  {profile.get('app_id')} {profile.get('name')!r}"
            f" table={str(digest)[:12] if digest else 'none'}"
            f" target={profile.get('target_process') or 'none'}"
            f" authorized={bool(profile.get('execution_consent_sha256'))}"
            f" autoload={bool(profile.get('autoload_enabled'))}"
        )
    for label, key in (
        ("config", "config_state_reason"),
        ("tables", "table_state_reason"),
        ("profiles", "profile_state_reason"),
    ):
        reason = status.get(key)
        if reason:
            lines.append(f"UNREADABLE {label}: {reason}")
    # What the backend's repeating paths ran and cost. A report of a hot device
    # or a session that drained a battery cannot be answered from a total for
    # the process, because several loops share it: this says which one was
    # awake, at what rate, and what it spent on a core.
    counters = diagnostics.get("poll_counters") if isinstance(diagnostics.get("poll_counters"), dict) else {}
    paths = counters.get("paths") if isinstance(counters.get("paths"), dict) else {}
    uptime = counters.get("uptime_seconds")
    if paths:
        lines += ["", f"Backend polling  over {uptime if isinstance(uptime, (int, float)) else '?'}s"]
        for name in sorted(paths):
            row = paths[name]
            if not isinstance(row, dict):
                continue
            calls = row.get("calls")
            per_minute = "?"
            if isinstance(calls, int) and isinstance(uptime, (int, float)) and uptime > 0:
                per_minute = f"{calls / uptime * 60:.1f}"
            lines.append(
                f"  {name} calls={calls} per_min={per_minute}"
                f" cpu={row.get('cpu_seconds')}s wall={row.get('wall_seconds')}s"
            )

    # Which sources were on, and what each one last did. A report of "search
    # finds nothing" is answered here or not at all: a source that was switched
    # off and a source that answered with nothing are the same empty answer
    # everywhere else in this archive.
    sources = payload.get("provider_sources") if isinstance(payload.get("provider_sources"), dict) else {}
    rows = sources.get("sources") if isinstance(sources.get("sources"), list) else []
    if rows:
        lines += ["", f"Table sources    {sources.get('enabled_count', '?')}/{sources.get('total', '?')} on"]
        for row in rows:
            if not isinstance(row, dict):
                continue
            counters = row.get("counters")
            switch = f"  {'on ' if row.get('enabled') else 'OFF'} {row.get('provider')}"
            if not isinstance(counters, dict):
                # A source with nothing recorded, or a counter record that could
                # not be read at all. Printing zeroes for either states as fact
                # something nobody measured, and the reader has no way back to
                # the difference from a row of zeroes.
                lines.append(f"{switch} counters=none recorded")
                continue
            lines.append(
                f"{switch}"
                f" state={row.get('state') or 'never asked'}"
                f" searches={counters.get('searches', 0)}"
                f" results={counters.get('results', 0)}"
                f" linked_reads={counters.get('linked_reads', 0)}"
                f" downloads={counters.get('downloads_succeeded', 0)}/{counters.get('downloads_failed', 0)}"
                f" errors={counters.get('errors', 0)}"
                f" unread_pages={counters.get('parse_failed', 0)}"
                f" cooldown={row.get('cooldown_seconds', 0)}s"
            )
            if row.get("last_error"):
                lines.append(f"      last error: {str(row.get('last_error'))[:200]}")
    for label, key in (
        ("source selection", "selection_reason"),
        ("provider diagnostics", "diagnostics_reason"),
    ):
        reason = sources.get(key)
        if reason:
            lines.append(f"UNREADABLE {label}: {reason}")
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), list) else []
    if runtime:
        lines += ["", "Runtime"]
        for item in runtime:
            if not isinstance(item, dict):
                continue
            envelope = item.get("envelope") if isinstance(item.get("envelope"), dict) else {}
            bridge = envelope.get("status") if isinstance(envelope.get("status"), dict) else {}
            lines.append(
                f"  app {item.get('app_id')} connected={bool(envelope.get('connected'))}"
                f" attached={bool(bridge.get('attached'))}"
                f" pid={bridge.get('opened_process_id')}"
                f" target={bridge.get('target_process')}"
                f" table_load={bridge.get('table_load_state')}"
                f" stale={envelope.get('session_stale_reason') or 'none'}"
            )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _claim_bundle_path(home: Path, archive_name: str) -> Path:
    """Take a bundle path that nothing else can then take.

    Asking whether a name is free and using it afterwards is two steps, and two
    presses inside the same second run them interleaved: both see the name free,
    both write, and the second replaces the first. `O_CREAT | O_EXCL` makes the
    question and the answer one step that only one caller can win.

    The empty file left by the claim is what the finished archive replaces. It
    also refuses a symlink for free, because `O_EXCL` fails on anything that
    already exists, a link included.
    """
    stem, suffix = archive_name[: -len(".zip")], ".zip"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for attempt in range(64):
        candidate = home / (f"{stem}{suffix}" if attempt == 0 else f"{stem}-{attempt + 1}{suffix}")
        try:
            descriptor = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise ValueError(f"the support bundle path could not be claimed: {exc}") from exc
        os.close(descriptor)
        return candidate
    raise ValueError("too many support bundles already exist with this name")


def _prune_old_bundles(home: Path, keep: int) -> list[str]:
    """Remove all but the newest `keep` bundles this plugin wrote.

    A user who presses the button five times while reproducing a problem should
    not slowly fill their home directory with archives, and only files matching
    this plugin's own name pattern are ever considered.
    """
    removed: list[str] = []
    existing = _newest_files(home, 512, suffix=".zip")
    ours = [path for path in existing if path.name.startswith(f"{BUNDLE_PREFIX}-")]
    for path in ours[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            continue
    return removed


def create_support_bundle(
    paths: PluginPaths,
    *,
    status: dict[str, object] | None,
    diagnostics: dict[str, object] | None,
    self_test: dict[str, object] | None,
    session_inventory: dict[str, object] | None,
    runtime: list[dict[str, object]],
    launch_capabilities: list[dict[str, object]],
    provider_diagnostics: dict[str, object] | None,
    provider_sources: dict[str, object] | None,
    blocked_tables: dict[str, object] | None,
    frontend_log: list[dict[str, object]],
    frontend_dropped: int,
    version: str,
    now: float | None = None,
    journal: dict[str, object] | None = None,
) -> dict[str, object]:
    """Write one support archive into the Steam user's home and describe it.

    Every backend snapshot is passed in already read rather than read here, so
    this module owns file collection and archive shape and the service keeps
    owning what a snapshot means. `journal` follows that rule: reading it runs a
    subprocess against the system journal, which is the service's to attempt and
    to absorb the failure of, and which a test collecting an archive must not be
    made to do. Omitted, it is recorded as not read rather than as empty.
    """
    moment = time.time() if now is None else now
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(moment))
    # The name carries a whole second, so two presses inside one second would
    # otherwise agree on it. Claim a free one before anything is written, so the
    # manifest and the README name the file this actually becomes.
    archive_path = _claim_bundle_path(paths.user_home, f"{BUNDLE_PREFIX}-{version}-{stamp}.zip")
    archive_name = archive_path.name
    # Held from here until the finished archive replaces it. Anything that
    # fails in between has to take the empty claim back with it, or a name is
    # burned and the next bundle silently becomes `-2`.
    try:
        return _build_support_bundle(
            paths,
            archive_path=archive_path,
            status=status, diagnostics=diagnostics, self_test=self_test,
            session_inventory=session_inventory, runtime=runtime,
            launch_capabilities=launch_capabilities,
            provider_diagnostics=provider_diagnostics, provider_sources=provider_sources,
            blocked_tables=blocked_tables,
            frontend_log=frontend_log, frontend_dropped=frontend_dropped,
            version=version, moment=moment, journal=journal,
        )
    finally:
        if not _claim_was_consumed(archive_path):
            archive_path.unlink(missing_ok=True)


def _claim_was_consumed(archive_path: Path) -> bool:
    """Whether the claim now holds a real archive rather than the empty file."""
    try:
        return archive_path.stat().st_size > 0
    except OSError:
        return False


def _build_support_bundle(
    paths: PluginPaths,
    *,
    archive_path: Path,
    status: dict[str, object] | None,
    diagnostics: dict[str, object] | None,
    self_test: dict[str, object] | None,
    session_inventory: dict[str, object] | None,
    runtime: list[dict[str, object]],
    launch_capabilities: list[dict[str, object]],
    provider_diagnostics: dict[str, object] | None,
    provider_sources: dict[str, object] | None,
    blocked_tables: dict[str, object] | None,
    frontend_log: list[dict[str, object]],
    frontend_dropped: int,
    version: str,
    moment: float,
    journal: dict[str, object] | None,
) -> dict[str, object]:
    """Collect into the path already claimed for this bundle."""
    archive_name = archive_path.name
    collector = _Collector(members=[], notes=[])

    payload = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(moment)),
        "status": status,
        "diagnostics": diagnostics,
        "self_test": self_test,
        "runtime": runtime,
        "provider_sources": provider_sources,
    }

    collector.add_json("diagnostics/status.json", status)
    collector.add_json("diagnostics/diagnostics.json", diagnostics)
    collector.add_json("diagnostics/self_test.json", self_test)
    collector.add_json("diagnostics/sessions.json", session_inventory)
    collector.add_json("diagnostics/runtime.json", runtime)
    collector.add_json("diagnostics/ce_launch.json", launch_capabilities)
    collector.add_json("diagnostics/providers.json", provider_diagnostics)
    collector.add_json("diagnostics/provider_sources.json", provider_sources)
    collector.add_json("diagnostics/blocked_tables.json", blocked_tables)
    collector.add_json("diagnostics/environment.json", _environment())
    collector.add_json("diagnostics/disk.json", _disk_usage(paths))
    collector.add_json("logs/frontend.json", {
        "dropped": frontend_dropped,
        "entries": frontend_log,
    })

    # The same record, written as the panel went rather than asked for at the
    # end. For a panel that wedged, this is the only one of the two that exists:
    # recovering a wedged Quick Access panel restarts Steam's webhelper, which
    # destroys the renderer the live ring above lives in, so the bundle taken
    # afterwards carries an empty one. The last entry here is what says whether
    # the panel was closed or stopped.
    journal_file = frontend_journal.journal_path(paths.state_root)
    journal_summary = frontend_journal.summarize(journal_file)
    collector.add_json("diagnostics/frontend_journal.json", journal_summary)
    if journal_summary.get("present"):
        data, front_truncated = frontend_journal.read_tail(
            journal_file, max_bytes=MAX_FRONTEND_JOURNAL_BYTES,
        )
        collector.add_bytes("logs/frontend-journal.jsonl", data, truncated=front_truncated)
    else:
        # Nothing to collect rather than something that could not be: the panel
        # flushes on its own schedule, so a bundle taken shortly after it loads
        # legitimately finds no file. Counted as omitted, it told the reader one
        # item had failed on an archive where everything had worked.
        collector.note(
            "logs/frontend-journal.jsonl",
            "the panel has not flushed anything to disk yet",
            kind="absent",
            path=str(journal_file),
        )

    # Current state before historical log tails. Both are bounded against one
    # whole-bundle budget, and the logs can ask for far more of it than
    # everything else put together, so collecting them first let a reload-heavy
    # day of log tails crowd out the configuration, the session records and the
    # tables in use - which is exactly the evidence a noisy failure needs and
    # the only evidence that cannot be reconstructed from anywhere else.
    for name, path in (
        ("state/config.json", paths.config_path),
        # The user's own on/off choices. Their own file, so that this one being
        # unreadable is never what a registered Cheat Engine is lost to.
        ("state/preferences.json", paths.settings_dir / "preferences.json"),
        ("state/profiles.json", paths.state_root / "profiles.json"),
        ("state/artifact_resolutions.json", paths.state_root / "artifact_resolutions.json"),
        ("state/table_compatibility.json", paths.state_root / "table_compatibility.json"),
        ("state/blocked_tables.json", paths.state_root / "blocked_tables.json"),
        ("state/provider_sources.json", paths.state_root / "provider_sources.json"),
        ("state/providers.json", paths.cache_root / "providers.json"),
        ("state/github-repository-index.json", paths.cache_root / "github-repository-index.json"),
        # A few dozen bytes, and the only durable record of when Search was last
        # used that a bundle can carry: the listing index it also rides in is a
        # third of a megabyte and is deliberately left out. Whether the
        # background listing refresh was armed at all is a question about this
        # file, and a report with no answer to it is read as a crawl that was
        # broken rather than one that was never owed a pass.
        ("state/fearless-search.json", paths.cache_root / "fearless-search.json"),
        # Which games no table may be started in until they are restarted, and
        # on what evidence each will be lifted. A report that a game "will not
        # start any table" is this file or a guess.
        ("state/game_run_holds.json", paths.state_root / "game_run_holds.json"),
        # What the last update check found, and what the last update did. The
        # install happens while this plugin is being replaced, so the backend
        # that could describe it no longer exists by the time it finishes:
        # these two files are the whole of what a report about a failed or
        # half-finished self-update can be written from.
        ("state/plugin-update.json", paths.state_root / "plugin-update.json"),
        ("state/plugin-update-result.json", paths.state_root / "plugin-update-result.json"),
    ):
        collector.add_file(name, path, max_bytes=MAX_STATE_BYTES, tail=False)

    # The exact descriptor a Cheat Engine was given, the commands it was sent
    # and the heartbeat it last wrote. A session's own table snapshot is left
    # out: the same bytes are collected once under tables/.
    for app_directory in _newest_directories(paths.state_root / "sessions", MAX_SESSION_DIRECTORIES):
        collector.add_file(
            f"state/sessions/{app_directory.name}/current.json",
            app_directory / "current.json",
            max_bytes=MAX_STATE_BYTES,
            tail=False,
        )
        for session in _newest_directories(app_directory, 3):
            for member in SESSION_MEMBERS:
                collector.add_file(
                    f"state/sessions/{app_directory.name}/{session.name}/{member}",
                    session / member,
                    max_bytes=MAX_STATE_BYTES,
                    tail=False,
                )

    # The self-test runs Cheat Engine with no game at all, so when an attached
    # launch fails these say whether Cheat Engine itself ever worked here.
    for run in _newest_directories(paths.state_root / "ce-launch-self-test", 3):
        for member in SESSION_MEMBERS:
            collector.add_file(
                f"state/ce-launch-self-test/{run.name}/{member}",
                run / member,
                max_bytes=MAX_STATE_BYTES,
                tail=False,
            )

    bundled_tables = _collect_active_tables(collector, paths, status)

    # Decky starts a new plugin log on every load, so the file holding the
    # failure is very often not the current one.
    plugin_logs = _newest_files(paths.log_dir, MAX_PLUGIN_LOG_FILES, suffix=".log")
    if not plugin_logs:
        collector.note("logs/plugin/", "no plugin log files were found", kind="absent", path=str(paths.log_dir))
    for path in plugin_logs:
        collector.add_file(f"logs/plugin/{path.name}", path, max_bytes=MAX_LOG_TAIL_BYTES, tail=True)

    # The detached updater's own record. It is written by a process that
    # outlives the plugin, so none of it is in the logs above; without it, an
    # update that failed between Decky's install and the interface restart has
    # no account of itself anywhere.
    collector.add_file(
        "logs/plugin-update-runner.jsonl",
        paths.log_dir / "plugin-update-runner.jsonl",
        max_bytes=MAX_LOG_TAIL_BYTES,
        tail=True,
    )

    # The same backend records out of the journal, which keeps them across the
    # plugin reload, the webhelper restart and the reboot. The files above are
    # the fragile copy: Decky starts a new one on every load and keeps only the
    # last few, so an install after a failure deletes the file holding it, which
    # is why one reported refusal in `docs/FIELD_NOTES.md` could not be
    # diagnosed at all. Only this plugin's own lines are taken; see the module.
    if not isinstance(journal, dict):
        # Also the shape a failed probe arrives in, since the service absorbs
        # the exception and hands back its default rather than raising here.
        collector.note("logs/journal.txt", "the journal was not read for this bundle")
    else:
        summary = {key: value for key, value in journal.items() if key != "lines"}
        if journal.get("ok"):
            lines = [str(line) for line in (journal.get("lines") or [])]
            blob = ("\n".join(lines) + "\n").encode("utf-8", "replace") if lines else b""
            if blob:
                truncated = len(blob) > MAX_SYSTEM_JOURNAL_BYTES
                data = blob[-MAX_SYSTEM_JOURNAL_BYTES:]
                if truncated:
                    # On a record boundary: a reader opens this at the top, and
                    # half a line there reads as a corrupt file rather than as a
                    # deliberately bounded one.
                    newline = data.find(b"\n")
                    data = data[newline + 1:] if newline >= 0 else b""
                collector.add_bytes("logs/journal.txt", data, truncated=truncated)
                # Read back out of the bytes that were written, because this is
                # the second truncation: the collector has already dropped
                # everything past its own line budget, and the tail above drops
                # whatever the byte budget cannot hold on top of that. A count
                # taken before either one describes a file that was never
                # written, and the one thing it would get wrong is the case the
                # note below exists for.
                written = data.decode("utf-8", "replace").splitlines()
                from_decky = sum(1 for line in written if journal_records.is_from_decky(line))
                summary["lines_written"] = len(written)
                summary["from_decky_written"] = from_decky
                # Only where this file lost records it was actually given. That
                # it never holds Decky's own loader lifecycle lines is not a
                # fault in this collection and never was: the backend is spawned
                # with `setuid` to the host user and no supplementary groups, so
                # journald shows it only what that user wrote and the loader's
                # records are root's. Noting it made every healthy bundle on
                # every device carry a member the panel counted as a problem,
                # which is a default that cannot succeed. It is stated where it
                # belongs instead: `from_decky_written` beside the counts it can
                # be read against, and one line of the archive's own README.
                if journal.get("from_decky") and not from_decky:
                    collector.note(
                        "logs/journal.txt",
                        _journal_partial_reason(journal, written=len(data)),
                        kind="partial",
                    )
                # The other way this file loses records, and the one the byte
                # budget no longer causes. `journal_records` counts what it
                # hands over *after* applying its own line cap, so when that cap
                # drops the head of the window `from_decky` is already zero and
                # the note above cannot fire. This says what is missing in terms
                # of our own lines, which is a fact rather than a claim about
                # records this backend may never have been able to read at all.
                over_limit = journal.get("dropped_over_limit")
                if isinstance(over_limit, int) and over_limit > 0:
                    collector.note(
                        "logs/journal.txt",
                        f"the newest {len(written)} of this plugin's lines in the window; "
                        f"{over_limit} older one(s) were past the line budget and are not here",
                        kind="partial",
                    )
            else:
                # A read that worked and found nothing of ours, which is what a
                # healthy device that has not run this plugin lately looks like.
                # Counted as omitted, it told the reader one item could not be
                # collected when everything had been.
                collector.note(
                    "logs/journal.txt",
                    "the journal window held none of this plugin's records",
                    kind="absent",
                )
        else:
            collector.note("logs/journal.txt", str(journal.get("reason", "the journal could not be read")))
        collector.add_json("diagnostics/journal.json", summary)

    # Whatever Proton and Cheat Engine printed. For a launch that never reached
    # the bridge, this is the only place the reason exists at all.
    launch_root = paths.state_root / "ce-launch"
    launch_logs = _newest_files(launch_root, MAX_LAUNCH_LOG_FILES, suffix=".log")
    if not launch_logs:
        collector.note("logs/ce-launch/", "no launch logs were found", kind="absent", path=str(launch_root))
    for path in launch_logs:
        collector.add_file(f"logs/ce-launch/{path.name}", path, max_bytes=MAX_LOG_TAIL_BYTES, tail=True)

    manifest = {
        "schema": SCHEMA,
        "version": version,
        "created_at": payload["created_at"],
        "created_epoch": int(moment),
        "archive": archive_name,
        "user_home": str(paths.user_home),
        "managed_root": str(paths.managed_root),
        "log_dir": str(paths.log_dir),
        "frontend_entries": len(frontend_log),
        # How many the panel had already written down before it was asked. A
        # bundle whose live ring is empty and whose journal is not was taken
        # after the renderer holding the ring was replaced.
        "frontend_journal_entries": journal_summary.get("entries", 0),
        "frontend_journal_last_event": journal_summary.get("last_event"),
        "frontend_dropped": frontend_dropped,
        "bundled_tables": bundled_tables,
        "limits": {
            "log_tail_bytes": MAX_LOG_TAIL_BYTES,
            "state_bytes": MAX_STATE_BYTES,
            "table_bytes": MAX_TABLE_BYTES,
            "total_bytes": MAX_TOTAL_BYTES,
        },
        "notes": collector.notes,
    }
    # The manifest names every note, including notes made while adding members,
    # so it is serialized last and added without going through the collector's
    # own budget check: an archive that loses its manifest cannot be read.
    members = [
        ("README.txt", _readme(archive_name)),
        ("summary.txt", _summary(payload)),
        ("manifest.json", json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")),
        *collector.members,
    ]

    # Built in a temporary file only this writer knows about: a shared staging
    # name let one writer truncate and unlink the archive another was writing.
    handle, temp_name = tempfile.mkstemp(prefix=f".{BUNDLE_PREFIX}-", suffix=".partial", dir=paths.user_home)
    os.close(handle)
    temporary = Path(temp_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, data in members:
                info = zipfile.ZipInfo(name, date_time=time.localtime(moment)[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, data)
        os.chmod(temporary, 0o600)
        os.replace(temporary, archive_path)
    finally:
        temporary.unlink(missing_ok=True)

    pruned = _prune_old_bundles(paths.user_home, MAX_RETAINED_BUNDLES)
    return {
        "schema": SCHEMA,
        "path": str(archive_path),
        "filename": archive_name,
        "size_bytes": archive_path.stat().st_size,
        "member_count": len(members),
        "uncompressed_bytes": collector.total_bytes,
        "notes": collector.notes,
        "removed_older_bundles": pruned,
    }


def _journal_partial_reason(journal: dict[str, object], *, written: int) -> str:
    """Why `logs/journal.txt` lost the loader records this collection was given.

    Said only for that case.  A bundle collected from the panel ordinarily never
    sees such a record at all, because the backend is spawned with `setuid` to
    the host user and no supplementary groups, so journald shows it only what
    that user wrote while the loader's records are root's.  That is a standing
    property of the device rather than something this collection did, and noting
    it on every bundle made every healthy archive look damaged; the README says
    it once and `diagnostics/journal.json` carries the counts.

    What this is for is the case where the collector did hand some over and the
    byte tail then dropped them, which is a record this archive was offered and
    does not carry.
    """
    collected = journal.get("from_decky")
    collected = collected if isinstance(collected, int) else 0
    return (
        f"this file is the last {written} bytes of the window and does not reach back to the "
        f"{collected} record(s) Decky's own loader wrote in it"
        ". Take those from the device with `scripts/target_plugin_log.py --journal`"
    )


def _collect_active_tables(collector: _Collector, paths: PluginPaths, status: object) -> list[dict[str, object]]:
    """Copy in the cheat table each configured game is actually using.

    A report about a table failing to parse, load or enable is unanswerable
    without the table, and the digest alone does not identify a community file
    that has been renamed and reposted. Only tables a profile currently selects
    are taken, never the whole library.
    """
    if not isinstance(status, dict):
        return []
    profiles = status.get("profiles")
    catalog = status.get("tables")
    if not isinstance(profiles, list) or not isinstance(catalog, list):
        return []
    by_digest = {
        entry.get("sha256"): entry
        for entry in catalog
        if isinstance(entry, dict) and isinstance(entry.get("sha256"), str)
    }
    wanted: list[str] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        digest = profile.get("table_sha256")
        if isinstance(digest, str) and digest in by_digest and digest not in wanted:
            wanted.append(digest)
    collected: list[dict[str, object]] = []
    for digest in wanted[:MAX_BUNDLED_TABLES]:
        entry = by_digest[digest]
        name = f"tables/{digest[:16]}.ct"
        record: dict[str, object] = {
            "sha256": digest,
            "filename": entry.get("filename"),
            "size": entry.get("size"),
            "included": False,
        }
        try:
            # Content-addressed layout owned by TableStore:
            # sha256/<shard>/<digest>/table.CT
            blob = managed_child(paths.tables_root, "sha256", digest[:2], digest, "table.CT")
        except ValueError as exc:
            collector.note(name, f"the managed table tree could not be walked safely: {exc}")
            collected.append(record)
            continue
        if not blob.is_file():
            collector.note(name, "the stored table blob was not found where the catalog says it is", path=str(blob))
        elif collector.add_file(name, blob, max_bytes=MAX_TABLE_BYTES, tail=False):
            record["included"] = True
            record["member"] = name
        collected.append(record)
    if len(wanted) > MAX_BUNDLED_TABLES:
        collector.note("tables/", "more active tables than the bundle limit", limit=MAX_BUNDLED_TABLES, active=len(wanted))
    return collected
