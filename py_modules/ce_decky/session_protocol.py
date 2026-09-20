from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import md5, sha256
from pathlib import Path
import os
import re
import shutil
import stat
import time
import uuid
import unicodedata
from typing import Iterable

from .atomic import atomic_write_bytes, atomic_write_json, durable_unlink, load_json, read_regular_bytes, read_regular_bytes_with_stat
from .ct_inspector import TableInspection
from .profiles import GameProfile, StartupPreference, MAX_EFFECTIVE_STARTUP_ACTIONS, _effective_preferences
from .text import utf8_bytes, utf8_len

DESCRIPTOR_HEADER = "CEDECKY-DESCRIPTOR-1"
CONTROL_HEADER = "CEDECKY-CONTROL-1"
STATUS_HEADER = "CEDECKY-STATUS-1"
MAX_PROTOCOL_BYTES = 1024 * 1024
MAX_COMMANDS = 2048
MAX_STARTUP_ACTIONS = MAX_EFFECTIVE_STARTUP_ACTIONS
MAX_STATUS_RESULTS = 256
MAX_STATUS_PROCESSES = 1024
MAX_STATUS_TEXT_BYTES = 4096
MAX_STATUS_PROCESS_NAME_BYTES = 1024
MAX_RUNTIME_COMMAND_VALUE_BYTES = 4096

# What one runtime command may ask the bridge to do.
#
# `quiesce` is the one that is about the session rather than about a record:
# stopping Cheat Engine kills the process group, so a table's `[DISABLE]` never
# runs and the patches and allocations of that session outlive it inside the
# running game - a later session's restore then reads symbols belonging to a
# dead Cheat Engine and cannot undo them either. Asking the bridge to put its
# own records down first is what leaves the game as it was found.
RUNTIME_COMMAND_KINDS = frozenset({"query", "set_active", "set_value", "retry_attach", "list_processes", "quiesce"})
# The ones that act on the session rather than on one record, and therefore
# carry no MemoryRecord ID.
WHOLE_SESSION_COMMAND_KINDS = frozenset({"retry_attach", "list_processes", "quiesce"})
# Superseded sessions are evidence, not an archive: keep a short history so a
# failure can still be inspected, and collect the rest.
RETAINED_SESSION_HISTORY = 3
MAX_SESSION_TABLE_BYTES = 32 * 1024 * 1024
MAX_SESSION_INVENTORY_APPS = 1024
MAX_SESSION_INVENTORY_TOTAL = 8192
MAX_SESSION_INVENTORY_APP_ENTRIES = 16384
STATUS_REPLACE_RETRY_SECONDS = 0.05
# A literal hyphen is the protocol's nullable-field sentinel.  It therefore
# must not be emitted verbatim by the percent encoder.
_SAFE = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class StartupAction:
    record_id: int
    kind: str
    value: str
    path: tuple[str, ...]
    # True only for an activation this plan expects to switch a cheat on with
    # nothing of this plan's own below it. The bridge cannot work this out: it
    # never sees the record tree the paths come from, and a nested record does
    # not exist until the script above it has run. It is carried in the
    # descriptor so that whichever eligible record actually transitions can be
    # the one reported, rather than one chosen before startup ran.
    proof: bool = False


@dataclass(frozen=True)
class SessionDescriptor:
    session_id: str
    app_id: int
    ce_sha256: str
    table_sha256: str
    table_path: str
    target_process: str
    control_path: str
    status_path: str
    startup: tuple[StartupAction, ...]
    is_shortcut: bool | None = False
    # Whether this exact table carries its own Lua script, from the inspection
    # that produced the review the user consented to. Cheat Engine asks before
    # it runs one, and that question is a modal form raised from inside the
    # load: on the route that can raise it, the bridge refuses the table with a
    # reason instead of stopping inside a question nobody can reach. Absent in a
    # descriptor written before this contract, which reads as not known.
    table_has_lua: bool | None = False


@dataclass(frozen=True)
class RuntimeCommand:
    generation: int
    kind: str
    record_id: int | None = None
    value: str | None = None
    target_pid: int | None = None


@dataclass(frozen=True)
class RuntimeResult:
    generation: int
    record_id: int | None
    ok: bool
    active: bool | None
    value: str | None
    error: str | None
    error_code: str | None = None


@dataclass(frozen=True)
class RuntimeStatus:
    session_id: str
    app_id: int
    ce_sha256: str
    table_sha256: str
    descriptor_sha256: str
    heartbeat_ms: int
    attached: bool
    target_process: str
    opened_process_id: int
    results: tuple[RuntimeResult, ...]
    processes: tuple[tuple[int, str], ...]
    # Optional bridge diagnostics keep legacy status files readable while an
    # updated exact package distinguishes table-load failure from record lookup
    # failure on the target.
    address_list_count: int | None = None
    # `loaded`, `pending` or `failed`: whether the bridge has the session's
    # exact table in Cheat Engine's address list. Cheat Engine only opens a
    # table named on its command line once its main window is shown, and CE
    # Decky never lets that window map, so the bridge loads it itself and this
    # is how a failure to do so is told from a table with no usable records.
    table_load_state: str | None = None
    table_load_error: str | None = None
    # Which route opened that table. `approved` is Cheat Engine's own stream
    # overload, which takes the decision to run the table's Lua script as a
    # parameter and so never raises the question; `prompted` is the path form,
    # which an older Cheat Engine is the only reason to use and which can still
    # stop inside a modal question nothing on the device can answer. Absent in
    # status written by an earlier bridge and before a load has been attempted.
    table_load_route: str | None = None
    # `starting` while the bridge is still inside its own synchronous bootstrap,
    # `ready` once that has finished. A `starting` heartbeat proves the bridge is
    # alive and nothing more: no table is open, nothing is attached and startup
    # has not begun, so it is liveness rather than readiness. Absent in status
    # written by an earlier bridge, which published nothing until it was ready.
    bridge_phase: str | None = None
    # How many times a Cheat Engine window had to be hidden again after the
    # initial suppression. Anything above zero is CE mapping a window over the
    # running game, which is what takes its audio and controller input, and it
    # is otherwise impossible to attribute after the fact.
    window_suppressions: int | None = None
    # Sweeps that saw a Cheat Engine window and could not put it down. Cheat
    # Engine's own hide helper does not reach a form a table's Lua script
    # created, and a table's Lua script now runs, so this is the difference
    # between a sweep that worked and one that has been failing once a second
    # with a window sitting over the running game. It counts attempts, not
    # windows: the sweep runs on a timer, so one window nothing can hide raises
    # it once per tick. Absent while every sweep succeeded, and in status
    # written by an earlier bridge.
    unsuppressed_sweeps: int | None = None
    # Whether a Cheat Engine window is over the game right now. The count above
    # only ever climbs, so it cannot say that the screen came back: a window the
    # table closed itself left it claiming the game was still covered for the
    # rest of the session. Absent until a sweep has failed at least once, and in
    # status written by an earlier bridge.
    window_over_game: bool | None = None
    # A Cheat Engine window hiding could not reach, dismissed because it was on
    # screen over the game. A message dialog is not one of the forms the sweep
    # enumerates, so it used to sit there unseen and uncounted while the game
    # lost its picture. Closing it answers it on the user's behalf, so the count
    # and the last caption are published rather than swallowed. Absent in status
    # written by an earlier bridge, and while nothing has been dismissed.
    dialogs_dismissed: int | None = None
    last_dialog: str | None = None
    # How many times the attached game was asked to come back from minimized
    # after a Cheat Engine window was taken off it. A game that loses the
    # foreground minimizes itself and stays that way once the window is gone:
    # sound and controller keep working and nothing is drawn at all.
    game_restores: int | None = None
    game_activations: int | None = None
    focus_capability: str | None = None
    focus_error: str | None = None
    startup_active_ids: tuple[int, ...] = ()
    focus_discovery_attempts: int | None = None
    focus_candidates: int | None = None
    focus_attempts: int | None = None
    focus_successes: int | None = None
    focus_reason: str | None = None
    # Which call answers "is this window minimized" on this Cheat Engine, or
    # `unavailable`. Nothing is ever asked to restore without it, because for a
    # maximized window the same request means "back to windowed size", so a game
    # that stays dark is attributable to this line. Absent in status written by
    # an earlier bridge and before the question has been asked.
    minimized_query: str | None = None  # one of MINIMIZED_QUERY_VALUES
    # Where establishing the restore capability stopped, as one of
    # RESTORE_CAPABILITY_VALUES. `minimized_query` names a symbol and so can
    # only describe the half that resolves one: it reports `unavailable` both
    # for a Cheat Engine that cannot answer whether a window is minimized and
    # for one that answers perfectly well but cannot post the request back.
    # Absent in status written by an earlier bridge.
    restore_capability: str | None = None
    # What the refused call said, when one refused. The capability above names
    # where it stopped; this is the only thing that says why, and without it a
    # local call that answers nothing is indistinguishable from one that is not
    # there. Bounded free text from Cheat Engine's own error, absent on success.
    restore_error: str | None = None
    # `applied`, `pending` or `failed`. A fresh heartbeat proves the bridge is
    # alive; it says nothing about whether the remembered cheats were restored,
    # so auto-load needs this to tell success from "still waiting for a record
    # an enclosing script has to create" and from an outright failure. Absent in
    # status written by an earlier bridge.
    startup_state: str | None = None
    # Monotonic startup progress, independent of the bounded result window.
    startup_completed: int | None = None
    startup_total: int | None = None


@dataclass(frozen=True)
class PreparedSession:
    session_id: str
    app_id: int
    ce_sha256: str
    table_sha256: str
    descriptor_path: str
    descriptor_sha256: str
    control_path: str
    status_path: str
    descriptor_windows_path: str
    descriptor_md5: str
    is_shortcut: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class SessionStore:
    SCHEMA = 3
    LEGACY_SCHEMA = 2

    def __init__(self, state_root: Path, user_home: Path):
        self.root = state_root / "sessions"
        self.user_home = user_home.resolve()

    def prepare(
        self, profile: GameProfile, table_blob: Path, inspection: TableInspection, ce_sha256: str
    ) -> PreparedSession:
        ce_sha256 = _sha(ce_sha256)
        if profile.table_sha256 is None or profile.table_sha256 != inspection.sha256:
            raise ValueError("profile table identity does not match inspected table")
        if profile.execution_consent_sha256 != inspection.sha256:
            raise ValueError("exact-SHA table execution consent is required before session preparation")
        if not profile.target_process:
            raise ValueError("target process is not configured")
        if table_blob.is_symlink() or not table_blob.is_file():
            raise ValueError("verified table blob must be a regular file")

        session_id = str(uuid.uuid4())
        session_root = self.root / str(profile.app_id) / session_id
        descriptor_path = session_root / "descriptor.txt"
        control_path = session_root / "control.txt"
        status_path = session_root / "status.txt"
        table_snapshot_path = session_root / "table.ct"
        metadata_path = session_root / "session.json"
        self._reject_symlinked_session_root(session_root)

        try:
            table_bytes = read_regular_bytes(table_blob, max_bytes=MAX_SESSION_TABLE_BYTES)
        except ValueError as exc:
            raise ValueError(f"verified table blob is unreadable or unstable: {exc}") from exc
        assert table_bytes is not None
        if sha256(table_bytes).hexdigest() != inspection.sha256:
            raise ValueError("verified table blob changed before exact-SHA session snapshot")
        # Execute only a session-local snapshot. The shared content-addressed table
        # store may later be repaired/re-imported, but that must never change the
        # bytes covered by this session's execution consent.
        atomic_write_bytes(table_snapshot_path, table_bytes, mode=0o400)
        snapshot_bytes = read_regular_bytes(table_snapshot_path, max_bytes=MAX_SESSION_TABLE_BYTES)
        assert snapshot_bytes is not None
        if sha256(snapshot_bytes).hexdigest() != inspection.sha256:
            raise ValueError("session table snapshot failed exact SHA-256 verification")

        descriptor = SessionDescriptor(
            session_id=session_id,
            app_id=profile.app_id,
            ce_sha256=ce_sha256,
            table_sha256=inspection.sha256,
            table_path=wine_z_path(table_snapshot_path),
            target_process=profile.target_process,
            control_path=wine_z_path(control_path),
            status_path=wine_z_path(status_path),
            startup=tuple(_startup_actions(_effective_startup_preferences(profile), inspection)),
            is_shortcut=profile.is_shortcut,
            # From the same inspection the user reviewed and consented to, so
            # the bridge does not have to read the table again to know whether
            # Cheat Engine has a question to ask about it.
            table_has_lua=bool(inspection.has_lua),
        )
        descriptor_bytes = render_descriptor(descriptor)
        descriptor_sha = sha256(descriptor_bytes).hexdigest()
        # CE exposes md5file() publicly but no documented SHA-256 file helper.
        # SHA-256 remains the authoritative host identity; this MD5 is only a
        # bridge-side immediate tamper/race guard before descriptor parsing.
        descriptor_md5 = md5(descriptor_bytes).hexdigest()
        atomic_write_bytes(descriptor_path, descriptor_bytes)
        atomic_write_bytes(control_path, (CONTROL_HEADER + "\n").encode("utf-8"))
        prepared = PreparedSession(
            session_id=session_id,
            app_id=profile.app_id,
            ce_sha256=ce_sha256,
            table_sha256=inspection.sha256,
            descriptor_path=str(descriptor_path),
            descriptor_sha256=descriptor_sha,
            control_path=str(control_path),
            status_path=str(status_path),
            descriptor_windows_path=wine_z_path(descriptor_path),
            descriptor_md5=descriptor_md5,
            is_shortcut=profile.is_shortcut,
        )
        atomic_write_json(metadata_path, {"schema": self.SCHEMA, **prepared.as_dict()})
        self._write_current_pointer(profile.app_id, session_id)
        self.collect_retired_sessions(profile.app_id, keep=session_id)
        return prepared

    def collect_retired_sessions(
        self, app_id: int, *, keep: str | None = None, retain: int = RETAINED_SESSION_HISTORY
    ) -> list[str]:
        """Delete superseded session directories, newest kept as evidence.

        Retiring a session removed only the current pointer and deliberately
        left its directory, so nothing ever collected them. Every prepare writes
        a full exact-SHA `table.ct` snapshot of up to 32 MiB, and the launch,
        stop and table-switch loop prepares one each time, so ordinary play grew
        the managed root without bound long before the inventory limits that
        make diagnostics fail.

        A directory is removed only when its own metadata identifies it, so a
        malformed or foreign entry is left alone for the corrupt-state path to
        report rather than silently deleted. The current session and the most
        recent `retain` others are always kept; a caller that owns a live Cheat
        Engine passes its exact session as `keep`.
        """
        app_id = _positive_int(app_id, "AppID")
        app_root = self.root / str(app_id)
        if app_root.is_symlink() or not app_root.is_dir():
            return []
        current = keep
        if current is None:
            try:
                prepared = self.load_current(app_id)
            except ValueError:
                # Corrupt current state is repaired elsewhere. Never collect
                # against a pointer we could not read.
                return []
            current = prepared.session_id if prepared is not None else None
        candidates: list[tuple[float, str, Path]] = []
        try:
            entries = list(app_root.iterdir())
        except OSError:
            return []
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir() or entry.name == current:
                continue
            metadata_path = entry / "session.json"
            if metadata_path.is_symlink() or not metadata_path.is_file():
                continue
            try:
                metadata = load_json(metadata_path, None, max_bytes=64 * 1024)
                if not isinstance(metadata, dict) or metadata.get("session_id") != entry.name:
                    continue
                if _positive_int(metadata.get("app_id"), "AppID") != app_id:
                    continue
                modified = metadata_path.stat(follow_symlinks=False).st_mtime
            except (OSError, ValueError):
                continue
            candidates.append((modified, entry.name, entry))
        if len(candidates) <= retain:
            return []
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        removed: list[str] = []
        for _, session_id, path in candidates[retain:]:
            try:
                shutil.rmtree(path)
            except OSError:
                continue
            removed.append(session_id)
        return removed

    def load_current(self, app_id: int) -> PreparedSession | None:
        app_id = _positive_int(app_id, "AppID")
        current_path = self.root / str(app_id) / "current.json"
        # Check the link itself before exists(): a dangling symlink must be treated
        # as corrupt managed state, not as an absent current session.
        if current_path.is_symlink():
            raise ValueError("current session pointer is not a regular file")
        if not current_path.exists():
            return None
        if not current_path.is_file():
            raise ValueError("current session pointer is not a regular file")
        raw = load_json(current_path, None, max_bytes=64 * 1024)
        pointer_schema = raw.get("schema") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema", "session_id"}
            or isinstance(pointer_schema, bool)
            or not isinstance(pointer_schema, int)
            or pointer_schema not in {self.LEGACY_SCHEMA, self.SCHEMA}
        ):
            raise ValueError("current session pointer is corrupt")
        session_id = _uuid(raw.get("session_id"))
        metadata_path = self.root / str(app_id) / session_id / "session.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            raise ValueError("current session metadata is missing")
        metadata = load_json(metadata_path, None, max_bytes=64 * 1024)
        required = {
            "schema", "session_id", "app_id", "ce_sha256", "table_sha256", "descriptor_path", "descriptor_sha256",
            "control_path", "status_path", "descriptor_windows_path", "descriptor_md5",
        }
        if pointer_schema == self.SCHEMA:
            required.add("is_shortcut")
        metadata_schema = metadata.get("schema") if isinstance(metadata, dict) else None
        if (
            not isinstance(metadata, dict)
            or set(metadata) != required
            or isinstance(metadata_schema, bool)
            or not isinstance(metadata_schema, int)
            or metadata_schema != pointer_schema
        ):
            raise ValueError("session metadata is corrupt")
        prepared = PreparedSession(
            session_id=_uuid(metadata.get("session_id")),
            app_id=_positive_int(metadata.get("app_id"), "AppID"),
            ce_sha256=_sha(metadata.get("ce_sha256")),
            table_sha256=_sha(metadata.get("table_sha256")),
            descriptor_path=_required_string(metadata.get("descriptor_path"), "descriptor_path"),
            descriptor_sha256=_sha(metadata.get("descriptor_sha256")),
            control_path=_required_string(metadata.get("control_path"), "control_path"),
            status_path=_required_string(metadata.get("status_path"), "status_path"),
            descriptor_windows_path=_required_string(metadata.get("descriptor_windows_path"), "descriptor_windows_path"),
            descriptor_md5=_md5(metadata.get("descriptor_md5")),
            is_shortcut=_strict_bool(metadata.get("is_shortcut"), "is_shortcut") if pointer_schema == self.SCHEMA else None,
        )
        if prepared.app_id != app_id or prepared.session_id != session_id:
            raise ValueError("session metadata identity mismatch")
        self._validate_prepared(prepared)
        return prepared

    def inventory(self) -> dict[str, object]:
        """Return bounded, read-only diagnostics for prepared session state."""
        if self.root.is_symlink():
            raise ValueError("managed session root must not be a symlink")
        if not self.root.exists():
            return {"apps": [], "total_sessions": 0, "errors": []}
        if not self.root.is_dir():
            raise ValueError("managed session root is not a directory")

        apps: list[dict[str, object]] = []
        errors: list[dict[str, object]] = []
        total_sessions = 0
        entries = _bounded_directory_children(
            self.root, MAX_SESSION_INVENTORY_APPS, "session inventory exceeds app-directory limit"
        )
        entries.sort(key=lambda item: item.name)
        for app_path in entries:
            if app_path.is_symlink() or not app_path.is_dir():
                errors.append({"path": app_path.name, "error": "unexpected non-directory or symlink in session root"})
                continue
            try:
                app_id = _parse_int(app_path.name, "session AppID directory", allow_zero=False, max_value=0xFFFFFFFF)
            except ValueError as exc:
                errors.append({"path": app_path.name, "error": str(exc)})
                continue
            session_count = 0
            corrupt_entries = 0
            children = _bounded_directory_children(
                app_path,
                MAX_SESSION_INVENTORY_APP_ENTRIES,
                f"session inventory for AppID {app_id} exceeds per-app entry limit",
            )
            for child in children:
                if child.name == "current.json":
                    if child.is_symlink() or not child.is_file():
                        corrupt_entries += 1
                    continue
                if child.is_symlink() or not child.is_dir():
                    corrupt_entries += 1
                    continue
                try:
                    _uuid(child.name)
                except ValueError:
                    corrupt_entries += 1
                    continue
                session_count += 1
                total_sessions += 1
                if total_sessions > MAX_SESSION_INVENTORY_TOTAL:
                    raise ValueError("session inventory exceeds total-session limit")
            current_session_id: str | None = None
            current_error: str | None = None
            try:
                current = self.load_current(app_id)
                if current is not None:
                    current_session_id = current.session_id
            except ValueError as exc:
                current_error = str(exc)[:512]
            apps.append({
                "app_id": app_id,
                "session_count": session_count,
                "current_session_id": current_session_id,
                "current_error": current_error,
                "corrupt_entries": corrupt_entries,
            })
        return {"apps": apps, "total_sessions": total_sessions, "errors": errors}

    def write_commands(self, prepared: PreparedSession, commands: Iterable[RuntimeCommand]) -> int:
        """Merge new commands without dropping commands the resident bridge may not have consumed yet.

        The control file is a replayable generation log, not a single-message mailbox.
        A Decky/frontend reload can therefore recover the next generation from managed
        state instead of resetting to 1 and silently issuing commands the bridge ignores.
        """
        self._validate_prepared(prepared)
        incoming = tuple(commands)
        if not incoming:
            raise ValueError("at least one runtime command is required")
        control_path = Path(prepared.control_path)
        existing = self._read_control_file(control_path)
        acknowledged = self._acknowledged_generation(prepared, existing)
        pending = tuple(command for command in existing if command.generation > acknowledged)
        # A valueful retry_attach changes the bridge's active target process. Preserve
        # the latest acknowledged target-setting command as durable authorization so
        # later status validation can distinguish an explicit user switch from tamper.
        retained_target = max(
            (command for command in existing if command.generation <= acknowledged and command.kind == "retry_attach" and command.value is not None),
            key=lambda command: command.generation,
            default=None,
        )
        # Generation 0 is reserved for bridge-generated startup results.
        floor = max(0, acknowledged, max((command.generation for command in pending), default=-1))
        expected = floor + 1
        if incoming[0].generation < expected:
            raise ValueError(f"runtime command generation is stale; next generation is {expected}")
        if incoming[0].generation > expected:
            raise ValueError(f"runtime command generation skipped ahead; next generation is {expected}")
        previous = floor
        for command in incoming:
            if command.generation != previous + 1:
                raise ValueError("runtime command generations must be consecutive")
            previous = command.generation
        prefix = (retained_target,) if retained_target is not None else ()
        merged = prefix + pending + incoming
        data = render_control(merged)
        atomic_write_bytes(control_path, data)
        return previous + 1

    def next_generation(self, prepared: PreparedSession) -> int:
        self._validate_prepared(prepared)
        existing = self._read_control_file(Path(prepared.control_path))
        acknowledged = self._acknowledged_generation(prepared, existing)
        highest = max(0, acknowledged, max((command.generation for command in existing), default=-1))
        if highest >= 0x7FFFFFFF:
            raise ValueError("runtime command generation space is exhausted; prepare a new session")
        return highest + 1

    def _read_control_file(self, path: Path) -> tuple[RuntimeCommand, ...]:
        try:
            data = read_regular_bytes(path, max_bytes=MAX_PROTOCOL_BYTES, allow_missing=True)
        except ValueError as exc:
            raise ValueError(f"runtime control path is invalid: {exc}") from exc
        if data is None:
            return ()
        return parse_control(data)

    def _acknowledged_generation(
        self, prepared: PreparedSession, existing: tuple[RuntimeCommand, ...] | None = None
    ) -> int:
        status = self.read_status(prepared)
        if status is None:
            return -1
        acknowledged = max((result.generation for result in status.results), default=-1)
        if acknowledged > 0:
            issued = existing if existing is not None else self._read_control_file(Path(prepared.control_path))
            highest_issued = max((command.generation for command in issued), default=0)
            if acknowledged > highest_issued:
                raise ValueError("runtime status acknowledges a generation absent from the control log")
        return acknowledged

    def validated_descriptor(self, prepared: PreparedSession) -> SessionDescriptor:
        """Return the exact verified immutable descriptor for a prepared session."""
        return self._validate_prepared(prepared)

    def read_status(self, prepared: PreparedSession) -> RuntimeStatus | None:
        return self.read_status_observation(prepared)[0]

    def status_mtime_ns(self, prepared: PreparedSession) -> int | None:
        """Return the heartbeat's write time without parsing or trusting its bytes.

        Ownership proofs only need to know when the bridge last wrote, and a
        heartbeat whose content no longer parses must not be able to withdraw a
        confirmed-exit proof and wedge the session it belongs to.
        """
        self._validate_prepared(prepared)
        status_path = Path(prepared.status_path)
        try:
            info = status_path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"runtime status path is invalid: {exc}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("runtime status path is not a regular file")
        return info.st_mtime_ns

    def read_status_observation(self, prepared: PreparedSession) -> tuple[RuntimeStatus | None, int | None]:
        """Read status content and mtime from the same descriptor to avoid replace races."""
        descriptor = self._validate_prepared(prepared)
        status_path = Path(prepared.status_path)
        data, info = self._read_status_bytes(status_path)
        if data is None or info is None:
            return None, None
        status = parse_status(data)
        if status.session_id != prepared.session_id:
            raise ValueError("runtime status session identity mismatch")
        if status.app_id != prepared.app_id:
            raise ValueError("runtime status AppID mismatch")
        if status.ce_sha256 != prepared.ce_sha256:
            raise ValueError("runtime status CE SHA mismatch")
        if status.table_sha256 != prepared.table_sha256:
            raise ValueError("runtime status table SHA mismatch")
        if status.descriptor_sha256 != prepared.descriptor_sha256:
            raise ValueError("runtime status descriptor SHA mismatch")
        control = self._read_control_file(Path(prepared.control_path))
        issued_by_generation = {command.generation: command for command in control}
        highest_issued = max((command.generation for command in control), default=0)
        for result in status.results:
            if result.generation <= 0:
                continue
            if result.generation > highest_issued:
                raise ValueError("runtime status acknowledges a generation absent from the control log")
            issued = issued_by_generation.get(result.generation)
            if issued is not None and result.record_id != issued.record_id:
                raise ValueError("runtime status result MemoryRecord identity does not match the control log")
        acknowledged = max((result.generation for result in status.results), default=-1)
        authorized = [
            command for command in control
            if command.kind == "retry_attach" and command.value is not None and command.generation <= acknowledged
        ]
        expected_runtime_target = max(authorized, key=lambda command: command.generation).value if authorized else descriptor.target_process
        if status.target_process != expected_runtime_target:
            raise ValueError("runtime status target process mismatch")
        if status.attached != (status.opened_process_id > 0):
            raise ValueError("runtime status attach/PID state is inconsistent")
        return status, info.st_mtime_ns

    def _read_status_bytes(self, status_path: Path) -> tuple[bytes | None, os.stat_result | None]:
        """Read the status file, tolerating the bridge's non-atomic replace window.

        CE's Lua runs under Wine, where `rename` cannot overwrite an existing
        name, so the bridge must unlink `status.txt` before renaming its staged
        `.tmp` into place. Treating that sub-millisecond gap as "no status" would
        report a live attached bridge as disconnected and abort an in-flight
        runtime batch. The staged file's presence distinguishes that window from
        a session whose bridge has never written a heartbeat; only then is one
        bounded re-read attempted, and the staged bytes are never trusted.
        """
        try:
            data, info = read_regular_bytes_with_stat(status_path, max_bytes=MAX_PROTOCOL_BYTES, allow_missing=True)
        except ValueError as exc:
            raise ValueError(f"runtime status path is invalid: {exc}") from exc
        if data is not None and info is not None:
            return data, info
        staged = status_path.with_name(status_path.name + ".tmp")
        try:
            mid_replace = staged.is_file() and not staged.is_symlink()
        except OSError:
            mid_replace = False
        if not mid_replace:
            return None, None
        time.sleep(STATUS_REPLACE_RETRY_SECONDS)
        try:
            return read_regular_bytes_with_stat(status_path, max_bytes=MAX_PROTOCOL_BYTES, allow_missing=True)
        except ValueError as exc:
            raise ValueError(f"runtime status path is invalid: {exc}") from exc

    def _validate_prepared(self, prepared: PreparedSession) -> SessionDescriptor:
        self._validate_paths(prepared)
        descriptor = Path(prepared.descriptor_path)
        try:
            descriptor_bytes = read_regular_bytes(descriptor, max_bytes=MAX_PROTOCOL_BYTES)
        except ValueError as exc:
            raise ValueError(f"session descriptor is missing or invalid: {exc}") from exc
        assert descriptor_bytes is not None
        actual = sha256(descriptor_bytes).hexdigest()
        if actual != prepared.descriptor_sha256:
            raise ValueError("session descriptor failed exact SHA-256 verification")
        if md5(descriptor_bytes).hexdigest() != prepared.descriptor_md5:
            raise ValueError("session descriptor failed bridge MD5 integrity verification")
        parsed = parse_descriptor(descriptor_bytes)
        if (
            parsed.session_id != prepared.session_id
            or parsed.app_id != prepared.app_id
            or parsed.ce_sha256 != prepared.ce_sha256
            or parsed.table_sha256 != prepared.table_sha256
            or parsed.is_shortcut != prepared.is_shortcut
        ):
            raise ValueError("session descriptor identity mismatch")
        if parsed.control_path != wine_z_path(Path(prepared.control_path)):
            raise ValueError("session descriptor control path mismatch")
        if parsed.status_path != wine_z_path(Path(prepared.status_path)):
            raise ValueError("session descriptor status path mismatch")
        expected_table = Path(prepared.descriptor_path).parent / "table.ct"
        if parsed.table_path != wine_z_path(expected_table):
            raise ValueError("session descriptor table path mismatch")
        try:
            table_bytes = read_regular_bytes(expected_table, max_bytes=MAX_SESSION_TABLE_BYTES)
        except ValueError as exc:
            raise ValueError(f"session table snapshot is missing or invalid: {exc}") from exc
        assert table_bytes is not None
        if sha256(table_bytes).hexdigest() != prepared.table_sha256:
            raise ValueError("session table snapshot failed exact SHA-256 verification")
        return parsed

    def _validate_paths(self, prepared: PreparedSession) -> None:
        app_id = _positive_int(prepared.app_id, "AppID")
        session_id = _uuid(prepared.session_id)
        expected_root = self.root / str(app_id) / session_id
        self._reject_symlinked_session_root(expected_root)
        expected = {
            "descriptor_path": expected_root / "descriptor.txt",
            "control_path": expected_root / "control.txt",
            "status_path": expected_root / "status.txt",
        }
        for field, path in expected.items():
            actual = Path(getattr(prepared, field)).expanduser().absolute()
            if actual != path.expanduser().absolute():
                raise ValueError(f"session {field} escaped the managed session root")
        if prepared.descriptor_windows_path != wine_z_path(expected["descriptor_path"]):
            raise ValueError("session descriptor Windows path mismatch")

    def _reject_symlinked_session_root(self, expected_root: Path) -> None:
        root_abs = self.root.expanduser().absolute()
        root_resolved = self.root.expanduser().resolve(strict=False)
        if root_resolved != root_abs:
            raise ValueError("managed session root resolves through a symlink")
        expected_abs = expected_root.expanduser().absolute()
        expected_resolved = expected_root.expanduser().resolve(strict=False)
        if expected_resolved != expected_abs:
            raise ValueError("managed session path resolves through a symlink")
        for candidate in (self.root, expected_root.parent, expected_root):
            if candidate.exists() and candidate.is_symlink():
                raise ValueError("managed session path contains a symlink")

    def discard_current_pointer(self, app_id: int) -> dict[str, object]:
        """Remove a current-session pointer without trusting what it contains.

        Strict parsing of `current.json` and `session.json` is correct, but every
        ordinary recovery path went through the same read: runtime status,
        session preparation, retirement and profile deletion all start from
        `load_current()`. A malformed pointer therefore blocked fresh session
        preparation for that game indefinitely, even with no process running and
        a healthy table, profile and config - and the only repair was deleting
        files from a terminal.

        This is the separately authorized repair. The caller proves no owned
        Cheat Engine can still reference the session; this does not read the
        payload, so a corrupt one cannot make the repair fail. The session
        directory is left in place as evidence.
        """
        app_id = _positive_int(app_id, "AppID")
        pointer = self.root / str(app_id) / "current.json"
        was_symlink = pointer.is_symlink()
        if not was_symlink and not pointer.exists():
            return {"discarded": False, "was_symlink": False}
        if not was_symlink and not pointer.is_file():
            raise ValueError("current session pointer is not a regular file or symlink")
        durable_unlink(pointer)
        return {"discarded": True, "was_symlink": was_symlink}

    def _write_current_pointer(self, app_id: int, session_id: str) -> None:
        current = self.root / str(app_id) / "current.json"
        atomic_write_json(current, {"schema": self.SCHEMA, "session_id": session_id})


def render_descriptor(descriptor: SessionDescriptor) -> bytes:
    _uuid(descriptor.session_id)
    target_process = _process_name(descriptor.target_process, "target process")
    for field_name, value in (("table_path", descriptor.table_path), ("control_path", descriptor.control_path), ("status_path", descriptor.status_path)):
        _wine_z_protocol_path(value, field_name)
    if len(descriptor.startup) > MAX_STARTUP_ACTIONS:
        raise ValueError("too many startup actions")
    lines = [
        DESCRIPTOR_HEADER,
        _field("session_id", descriptor.session_id),
        _field("app_id", str(_positive_int(descriptor.app_id, "AppID"))),
        _field("is_shortcut", "1" if _strict_bool(descriptor.is_shortcut, "is_shortcut") else "0"),
        _field("ce_sha256", _sha(descriptor.ce_sha256)),
        _field("table_sha256", _sha(descriptor.table_sha256)),
        _field("table_path", descriptor.table_path),
        _field("target_process", target_process),
        _field("control_path", descriptor.control_path),
        _field("status_path", descriptor.status_path),
        _field("table_has_lua", "1" if _strict_bool(descriptor.table_has_lua, "table_has_lua") else "0"),
    ]
    seen_startup: set[tuple[int, str]] = set()
    for action in descriptor.startup:
        if action.kind not in {"active", "value"}:
            raise ValueError("unsupported startup action kind")
        _record_id(action.record_id)
        if action.kind == "active" and action.value not in {"0", "1"}:
            raise ValueError("startup active value must be '0' or '1'")
        startup_key = (action.record_id, action.kind)
        if startup_key in seen_startup:
            raise ValueError("duplicate startup action for MemoryRecord")
        seen_startup.add(startup_key)
        if action.proof and (action.kind != "active" or action.value != "1"):
            raise ValueError("startup proof eligibility requires an activation")
        lines.append("A\t{}\t{}\t{}\t{}".format(
            action.record_id, action.kind, percent_encode(action.value), "1" if action.proof else "0"))
    return _bounded_lines(lines)


def parse_descriptor(data: bytes) -> SessionDescriptor:
    lines = _decode_lines(data, DESCRIPTOR_HEADER)
    fields: dict[str, str] = {}
    startup: list[StartupAction] = []
    seen_startup: set[tuple[int, str]] = set()
    for line in lines[1:]:
        parts = line.split("\t")
        if parts[0] == "F" and len(parts) == 3:
            key = parts[1]
            if key in fields:
                raise ValueError(f"duplicate descriptor field: {key}")
            fields[key] = percent_decode(parts[2])
        elif parts[0] == "A" and len(parts) in (4, 5):
            rid = _parse_int(parts[1], "MemoryRecord ID", allow_zero=True, max_value=0x7FFFFFFF)
            kind = parts[2]
            if kind not in {"active", "value"}:
                raise ValueError("unsupported startup action kind")
            value = percent_decode(parts[3])
            if kind == "active" and value not in {"0", "1"}:
                raise ValueError("startup active value must be '0' or '1'")
            # A session prepared by an earlier build states no eligibility. Such
            # a session is retired as stale rather than launched, so the only
            # thing the absent column costs is advisory proof.
            proof = len(parts) == 5 and parts[4] == "1"
            if len(parts) == 5 and parts[4] not in {"0", "1"}:
                raise ValueError("startup proof eligibility must be '0' or '1'")
            if proof and (kind != "active" or value != "1"):
                raise ValueError("startup proof eligibility requires an activation")
            startup_key = (rid, kind)
            if startup_key in seen_startup:
                raise ValueError("duplicate startup action for MemoryRecord")
            seen_startup.add(startup_key)
            startup.append(StartupAction(rid, kind, value, (), proof))
            if len(startup) > MAX_STARTUP_ACTIONS:
                raise ValueError("too many startup actions")
        else:
            raise ValueError("invalid descriptor line")
    required = {"session_id", "app_id", "ce_sha256", "table_sha256", "table_path", "target_process", "control_path", "status_path"}
    allowed = required | {"is_shortcut", "table_has_lua"}
    if not required.issubset(fields) or not set(fields).issubset(allowed):
        raise ValueError("descriptor fields are incomplete or unknown")
    _uuid(fields["session_id"])
    app_id = _parse_int(fields["app_id"], "AppID", allow_zero=False, max_value=0xFFFFFFFF)
    target_process = _process_name(fields["target_process"], "target process")
    for field_name in ("table_path", "control_path", "status_path"):
        _wine_z_protocol_path(fields[field_name], field_name)
    return SessionDescriptor(
        fields["session_id"], app_id, _sha(fields["ce_sha256"]), _sha(fields["table_sha256"]), fields["table_path"], target_process,
        fields["control_path"], fields["status_path"], tuple(startup),
        None if "is_shortcut" not in fields else _parse_bool(fields["is_shortcut"], "is_shortcut"),
        None if "table_has_lua" not in fields else _parse_bool(fields["table_has_lua"], "table_has_lua"),
    )


def render_control(commands: Iterable[RuntimeCommand]) -> bytes:
    items = list(commands)
    if len(items) > MAX_COMMANDS:
        raise ValueError("too many runtime commands")
    lines = [CONTROL_HEADER]
    previous = -1
    for command in items:
        generation = _positive_int(command.generation, "generation", allow_zero=False)
        if generation <= previous:
            raise ValueError("runtime command generations must be strictly increasing")
        previous = generation
        _validate_runtime_command(command.kind, command.record_id, command.value, command.target_pid)
        rid = "-" if command.record_id is None else str(_record_id(command.record_id))
        value = "-" if command.value is None else percent_encode(command.value)
        if command.kind in {"query", "set_active", "set_value"} and command.record_id is None:
            raise ValueError("record command requires MemoryRecord ID")
        if command.kind in WHOLE_SESSION_COMMAND_KINDS and command.record_id is not None:
            raise ValueError(f"{command.kind} does not accept a MemoryRecord ID")
        if command.kind == "set_active" and command.value not in {"0", "1"}:
            raise ValueError("set_active value must be '0' or '1'")
        if command.kind == "set_value" and command.value is None:
            raise ValueError("set_value requires a value")
        if command.kind in {"query", "list_processes", "quiesce"} and command.value is not None:
            raise ValueError(f"{command.kind} does not accept a value")
        if command.kind == "retry_attach" and command.value is not None:
            _process_name(command.value, "retry_attach target")
        target_pid = "-" if command.target_pid is None else str(_positive_int(command.target_pid, "target PID"))
        lines.append(f"C\t{generation}\t{command.kind}\t{rid}\t{value}\t{target_pid}")
    return _bounded_lines(lines)


def parse_control(data: bytes) -> tuple[RuntimeCommand, ...]:
    lines = _decode_lines(data, CONTROL_HEADER)
    commands: list[RuntimeCommand] = []
    previous = -1
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != 6 or parts[0] != "C":
            raise ValueError("invalid control line")
        generation = _parse_int(parts[1], "generation", allow_zero=False, max_value=0x7FFFFFFF)
        if generation <= previous:
            raise ValueError("runtime command generations must be strictly increasing")
        previous = generation
        kind = parts[2]
        if kind not in RUNTIME_COMMAND_KINDS:
            raise ValueError("unsupported runtime command")
        rid = None if parts[3] == "-" else _parse_int(parts[3], "MemoryRecord ID", allow_zero=True, max_value=0x7FFFFFFF)
        value = None if parts[4] == "-" else percent_decode(parts[4])
        target_pid = None if parts[5] == "-" else _parse_int(parts[5], "target PID", allow_zero=False, max_value=0xFFFFFFFF)
        if kind in {"query", "set_active", "set_value"} and rid is None:
            raise ValueError("record command requires MemoryRecord ID")
        if kind in WHOLE_SESSION_COMMAND_KINDS and rid is not None:
            raise ValueError(f"{kind} does not accept a MemoryRecord ID")
        if kind == "set_active" and value not in {"0", "1"}:
            raise ValueError("set_active value must be '0' or '1'")
        if kind == "set_value" and value is None:
            raise ValueError("set_value requires a value")
        if kind in {"query", "list_processes", "quiesce"} and value is not None:
            raise ValueError(f"{kind} does not accept a value")
        if kind == "retry_attach" and value is not None:
            _process_name(value, "retry_attach target")
        _validate_runtime_command(kind, rid, value, target_pid)
        commands.append(RuntimeCommand(generation, kind, rid, value, target_pid))
        if len(commands) > MAX_COMMANDS:
            raise ValueError("too many runtime commands")
    return tuple(commands)


# Diagnostic fields an earlier bridge wrote that this one does not. They are
# accepted and ignored so a Cheat Engine that outlived a plugin update keeps a
# readable heartbeat: rejecting the whole status over one retired diagnostic
# turns a healthy session into "runtime protocol state is unreadable", which
# reads to a user as corruption and takes live control with it.
#
# `table_path_matches` was the path in CE's save dialog. It was only ever
# evidence about a table CE opened from its command line, which CE Decky no
# longer asks for; `table_load_state` is what there is to know now.
# Fields an earlier bridge published that this one no longer does. A status
# file left behind by a previous plugin version stays readable, so a stale
# session is reported as stale rather than as unparseable.
LEGACY_STATUS_FIELDS = frozenset({"table_path_matches", "windows_unsuppressed"})


def parse_status(data: bytes) -> RuntimeStatus:
    lines = _decode_lines(data, STATUS_HEADER)
    fields: dict[str, str] = {}
    results: list[RuntimeResult] = []
    startup_active_ids: list[int] = []
    seen_runtime_generations: set[int] = set()
    processes: list[tuple[int, str]] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if parts[0] == "F" and len(parts) == 3:
            if parts[1] in fields:
                raise ValueError(f"duplicate status field: {parts[1]}")
            fields[parts[1]] = percent_decode(parts[2])
        elif parts[0] == "R" and len(parts) in {7, 8}:
            generation = _parse_int(parts[1], "generation", allow_zero=True, max_value=0x7FFFFFFF)
            if generation > 0:
                if generation in seen_runtime_generations:
                    raise ValueError("duplicate runtime result generation")
                seen_runtime_generations.add(generation)
            rid = None if parts[2] == "-" else _parse_int(parts[2], "MemoryRecord ID", allow_zero=True, max_value=0x7FFFFFFF)
            ok = _parse_bool(parts[3], "result ok")
            active = None if parts[4] == "-" else _parse_bool(parts[4], "result active")
            value = None if parts[5] == "-" else _bounded_decoded(percent_decode(parts[5]), "result value", MAX_STATUS_TEXT_BYTES)
            error = None if parts[6] == "-" else _bounded_decoded(percent_decode(parts[6]), "result error", MAX_STATUS_TEXT_BYTES)
            error_code = None if len(parts) == 7 or parts[7] == "-" else _runtime_error_code(parts[7])
            if ok and error_code is not None:
                raise ValueError("successful runtime result carries an error code")
            results.append(RuntimeResult(generation, rid, ok, active, value, error, error_code))
            if len(results) > MAX_STATUS_RESULTS:
                raise ValueError("status contains too many runtime results")
        elif parts[0] == "S" and len(parts) == 2:
            record_id = _parse_int(parts[1], "startup active record ID", allow_zero=True, max_value=0x7FFFFFFF)
            if record_id in startup_active_ids or len(startup_active_ids) >= 1:
                raise ValueError("invalid startup active summary")
            startup_active_ids.append(record_id)
        elif parts[0] == "P" and len(parts) == 3:
            pid = _parse_int(parts[1], "PID", allow_zero=False, max_value=0xFFFFFFFF)
            name = _process_display_name(percent_decode(parts[2]), "process name")
            processes.append((pid, name))
            if len(processes) > MAX_STATUS_PROCESSES:
                raise ValueError("status contains too many process rows")
        else:
            raise ValueError("invalid status line")
    required = {"session_id", "app_id", "ce_sha256", "table_sha256", "descriptor_sha256", "heartbeat_ms", "attached", "target_process", "opened_process_id"}
    optional = {"focus_capability", "focus_error", "focus_discovery_attempts", "focus_candidates", "focus_attempts", "focus_successes", "focus_reason", "address_list_count", "table_load_state", "table_load_error", "table_load_route", "bridge_phase", "window_suppressions", "unsuppressed_sweeps", "window_over_game", "dialogs_dismissed", "last_dialog", "game_restores", "game_activations", "minimized_query", "restore_capability", "restore_error", "startup_state", "startup_completed", "startup_total"}
    if not required.issubset(fields) or set(fields) - required - optional - LEGACY_STATUS_FIELDS:
        raise ValueError("status fields are incomplete or unknown")
    attached = _parse_bool(fields["attached"], "attached")
    opened_process_id = _parse_int(fields["opened_process_id"], "opened process ID", allow_zero=True, max_value=0xFFFFFFFF)
    if attached != (opened_process_id > 0):
        raise ValueError("runtime status attach/PID state is inconsistent")
    return RuntimeStatus(
        session_id=_uuid(fields["session_id"]),
        app_id=_parse_int(fields["app_id"], "AppID", allow_zero=False, max_value=0xFFFFFFFF),
        ce_sha256=_sha(fields["ce_sha256"]),
        table_sha256=_sha(fields["table_sha256"]),
        descriptor_sha256=_sha(fields["descriptor_sha256"]),
        heartbeat_ms=_parse_int(fields["heartbeat_ms"], "heartbeat", allow_zero=True, max_value=0x7FFFFFFFFFFFFFFF),
        attached=attached,
        target_process=_process_name(fields["target_process"], "target process"),
        opened_process_id=opened_process_id,
        results=tuple(results),
        processes=tuple(processes),
        address_list_count=None if "address_list_count" not in fields else _parse_int(
            fields["address_list_count"], "AddressList count", allow_zero=True, max_value=0x7FFFFFFF
        ),
        table_load_state=None if "table_load_state" not in fields else _table_load_state(
            fields["table_load_state"]
        ),
        table_load_error=None if "table_load_error" not in fields else _bounded_decoded(
            fields["table_load_error"], "table load error", MAX_STATUS_TEXT_BYTES
        ),
        table_load_route=None if "table_load_route" not in fields else _table_load_route(
            fields["table_load_route"]
        ),
        bridge_phase=None if "bridge_phase" not in fields else _bridge_phase(fields["bridge_phase"]),
        window_suppressions=None if "window_suppressions" not in fields else _parse_int(
            fields["window_suppressions"], "window suppression count", allow_zero=True, max_value=0x7FFFFFFF
        ),
        unsuppressed_sweeps=None if "unsuppressed_sweeps" not in fields else _parse_int(
            fields["unsuppressed_sweeps"], "unsuppressed sweep count", allow_zero=True, max_value=0x7FFFFFFF
        ),
        window_over_game=None if "window_over_game" not in fields else _parse_bool(
            fields["window_over_game"], "window over game"
        ),
        dialogs_dismissed=None if "dialogs_dismissed" not in fields else _parse_int(
            fields["dialogs_dismissed"], "dismissed dialog count", allow_zero=True, max_value=0x7FFFFFFF
        ),
        last_dialog=None if "last_dialog" not in fields else _bounded_decoded(
            fields["last_dialog"], "dismissed dialog caption", MAX_STATUS_TEXT_BYTES
        ),
        focus_capability=None if "focus_capability" not in fields else _bounded_decoded(fields["focus_capability"], "focus capability", MAX_STATUS_TEXT_BYTES),
        focus_error=None if "focus_error" not in fields else _bounded_decoded(fields["focus_error"], "focus error", MAX_STATUS_TEXT_BYTES),
        startup_active_ids=tuple(startup_active_ids),
        focus_discovery_attempts=None if "focus_discovery_attempts" not in fields else _parse_int(fields["focus_discovery_attempts"], "focus discovery attempts", allow_zero=True, max_value=20),
        focus_candidates=None if "focus_candidates" not in fields else _parse_int(fields["focus_candidates"], "focus_candidates", allow_zero=True, max_value=0x7FFFFFFF),
        focus_attempts=None if "focus_attempts" not in fields else _parse_int(fields["focus_attempts"], "focus_attempts", allow_zero=True, max_value=0x7FFFFFFF),
        focus_successes=None if "focus_successes" not in fields else _parse_int(fields["focus_successes"], "focus_successes", allow_zero=True, max_value=0x7FFFFFFF),
        focus_reason=None if "focus_reason" not in fields else _focus_reason(fields["focus_reason"]),
        game_activations=None if "game_activations" not in fields else _parse_int(
            fields["game_activations"], "game activation count", allow_zero=True, max_value=0x7FFFFFFF
        ),
        game_restores=None if "game_restores" not in fields else _parse_int(
            fields["game_restores"], "game restore count", allow_zero=True, max_value=0x7FFFFFFF
        ),
        restore_error=None if "restore_error" not in fields else _bounded_decoded(
            fields["restore_error"], "restore error", MAX_STATUS_TEXT_BYTES
        ),
        restore_capability=None if "restore_capability" not in fields else _restore_capability(
            _bounded_decoded(fields["restore_capability"], "restore capability", MAX_STATUS_TEXT_BYTES)
        ),
        minimized_query=None if "minimized_query" not in fields else _minimized_query(
            _bounded_decoded(fields["minimized_query"], "minimized-state query", MAX_STATUS_TEXT_BYTES)
        ),
        startup_state=None if "startup_state" not in fields else _startup_state(fields["startup_state"]),
        startup_completed=None if "startup_completed" not in fields else _parse_int(
            fields["startup_completed"], "startup completed count", allow_zero=True, max_value=MAX_STARTUP_ACTIONS,
        ),
        startup_total=None if "startup_total" not in fields else _parse_int(
            fields["startup_total"], "startup action count", allow_zero=True, max_value=MAX_STARTUP_ACTIONS,
        ),
    )


# `failed` alone could not distinguish a startup that put the game back from one
# that left an earlier action - very often an enclosing script the user never
# chose - still applied. Older bridges only ever report the first three.
STARTUP_STATES = frozenset({"applied", "pending", "failed", "failed_rolled_back", "failed_partial"})
STARTUP_FAILURE_STATES = frozenset({"failed", "failed_rolled_back", "failed_partial"})


# Whether the bridge has this session's exact table in Cheat Engine's address
# list. Absent in status written by an earlier bridge, which relied on Cheat
# Engine opening the table named on its command line.
TABLE_LOAD_STATES = frozenset({"loaded", "pending", "failed"})


def _table_load_state(value: str) -> str:
    if value not in TABLE_LOAD_STATES:
        raise ValueError("table load state is invalid")
    return value


# How the bridge opened the session's table. `approved` carries the exact-SHA
# execution authorization the user gave in Review into Cheat Engine's own load,
# so the table's Lua script runs and Cheat Engine never asks; `prompted` is the
# older path form, which asks, and whose question is a modal form no one can
# reach over a running game.
TABLE_LOAD_ROUTES = frozenset({"approved", "prompted"})


def _table_load_route(value: str) -> str:
    if value not in TABLE_LOAD_ROUTES:
        raise ValueError("table load route is invalid")
    return value


# Whether the bridge has finished its own synchronous bootstrap.
BRIDGE_PHASES = frozenset({"starting", "ready"})


def _bridge_phase(value: str) -> str:
    if value not in BRIDGE_PHASES:
        raise ValueError("bridge phase is invalid")
    return value


# Which call the bridge proved can answer "is this window minimized", or that
# none can. A game is asked to come back only on a positive answer from one of
# these, so an unrecognised value must not read as a working capability: it
# would suppress the panel's own account of why a game was left dark. Absent in
# status written by an earlier bridge, and before the proof has been made.
MINIMIZED_QUERY_VALUES = frozenset({"user32.IsIconic", "IsIconic", "unavailable"})


# Where establishing the restore capability stopped. `ready` is the only value
# that means a game pushed aside can be asked back; `no-window` and
# `symbols-unresolved` are the two that can still become `ready` without a new
# session, because both are Cheat Engine still starting rather than a refusal.
RESTORE_CAPABILITY_VALUES = frozenset({
    "ready", "no-local-call", "no-window", "symbols-unresolved",
    "iconic-unanswered", "iconic-disagrees", "no-post",
})


def _focus_reason(value: str) -> str:
    if value not in {"idle", "detached", "unavailable", "no-window", "ambiguous", "already-foreground", "owner-changed", "confirmed", "call-error", "refused", "unconfirmed"}:
        raise ValueError("invalid focus reason")
    return value


def _restore_capability(value: str) -> str:
    if value not in RESTORE_CAPABILITY_VALUES:
        raise ValueError("restore capability is invalid")
    return value


def _minimized_query(value: str) -> str:
    if value not in MINIMIZED_QUERY_VALUES:
        raise ValueError("minimized-state query is invalid")
    return value


def _startup_state(value: str) -> str:
    if value not in STARTUP_STATES:
        raise ValueError("startup state is invalid")
    return value


def _runtime_error_code(value: str) -> str:
    allowed = {
        "record_missing", "target_detached", "address_list_unavailable",
        "record_read_failed", "mutation_failed", "attach_failed",
        # Cheat Engine accepted the change, finished, and the record is still
        # not in the requested state. Distinguished from the undifferentiated
        # failure because it is the one a user can act on.
        "activation_rejected",
        # A quiesce that could not put every record down. Its own code because
        # what follows from it is not a failure of the stop: the stop proceeds
        # either way, and this is the record of what was left switched on in a
        # game that is about to lose the Cheat Engine that could have undone it.
        "quiesce_unsettled",
    }
    if value not in allowed:
        raise ValueError("runtime result error code is invalid")
    return value


def render_status(status: RuntimeStatus) -> bytes:
    if len(status.results) > MAX_STATUS_RESULTS:
        raise ValueError("status contains too many runtime results")
    if len(status.processes) > MAX_STATUS_PROCESSES:
        raise ValueError("status contains too many process rows")
    if status.attached != (status.opened_process_id > 0):
        raise ValueError("runtime status attach/PID state is inconsistent")
    _process_name(status.target_process, "target process")
    lines = [
        STATUS_HEADER,
        _field("session_id", _uuid(status.session_id)),
        _field("app_id", str(_positive_int(status.app_id, "AppID"))),
        _field("ce_sha256", _sha(status.ce_sha256)),
        _field("table_sha256", _sha(status.table_sha256)),
        _field("descriptor_sha256", _sha(status.descriptor_sha256)),
        _field("heartbeat_ms", str(_positive_int(status.heartbeat_ms, "heartbeat", allow_zero=True))),
        _field("attached", "1" if status.attached else "0"),
        _field("target_process", status.target_process),
        _field("opened_process_id", str(_positive_int(status.opened_process_id, "opened process ID", allow_zero=True))),
    ]
    if status.address_list_count is not None:
        lines.append(_field("address_list_count", str(_positive_int(
            status.address_list_count, "AddressList count", allow_zero=True
        ))))
    if status.table_load_state is not None:
        lines.append(_field("table_load_state", _table_load_state(status.table_load_state)))
    if status.bridge_phase is not None:
        lines.append(_field("bridge_phase", _bridge_phase(status.bridge_phase)))
    if status.table_load_route is not None:
        lines.append(_field("table_load_route", _table_load_route(status.table_load_route)))
    if status.table_load_error is not None:
        lines.append(_field("table_load_error", _bounded_decoded(
            status.table_load_error, "table load error", MAX_STATUS_TEXT_BYTES
        )))
    if status.window_suppressions is not None:
        lines.append(_field("window_suppressions", str(_positive_int(
            status.window_suppressions, "window suppression count", allow_zero=True
        ))))
    if status.unsuppressed_sweeps is not None:
        lines.append(_field("unsuppressed_sweeps", str(_positive_int(
            status.unsuppressed_sweeps, "unsuppressed sweep count", allow_zero=True
        ))))
    if status.window_over_game is not None:
        lines.append(_field("window_over_game", "1" if status.window_over_game else "0"))
    if status.dialogs_dismissed is not None:
        lines.append(_field("dialogs_dismissed", str(_positive_int(
            status.dialogs_dismissed, "dismissed dialog count", allow_zero=True
        ))))
    if status.last_dialog is not None:
        lines.append(_field("last_dialog", _bounded_decoded(
            status.last_dialog, "dismissed dialog caption", MAX_STATUS_TEXT_BYTES
        )))
    if status.minimized_query is not None:
        lines.append(_field("minimized_query", _minimized_query(status.minimized_query)))
    if status.restore_capability is not None:
        lines.append(_field("restore_capability", _restore_capability(status.restore_capability)))
    if status.restore_error is not None:
        lines.append(_field("restore_error", status.restore_error))
    if len(status.startup_active_ids) > 1 or len(set(status.startup_active_ids)) != len(status.startup_active_ids):
        raise ValueError("invalid startup active summary")
    for record_id in status.startup_active_ids:
        lines.append(f"S\t{_record_id(record_id)}")
    for name in ("focus_capability", "focus_error"):
        value = getattr(status, name)
        if value is not None:
            lines.append(_field(name, value))
    for name in ("focus_discovery_attempts", "focus_candidates", "focus_attempts", "focus_successes"):
        value = getattr(status, name)
        if value is not None:
            lines.append(_field(name, str(_positive_int(value, name, allow_zero=True))))
    if status.focus_reason is not None:
        lines.append(_field("focus_reason", _focus_reason(status.focus_reason)))
    if status.game_activations is not None:
        lines.append(_field("game_activations", str(_positive_int(
            status.game_activations, "game activation count", allow_zero=True
        ))))
    if status.game_restores is not None:
        lines.append(_field("game_restores", str(_positive_int(
            status.game_restores, "game restore count", allow_zero=True
        ))))
    if status.startup_state is not None:
        lines.append(_field("startup_state", _startup_state(status.startup_state)))
    if status.startup_completed is not None:
        lines.append(_field("startup_completed", str(_positive_int(status.startup_completed + 1, "startup completed count") - 1)))
    if status.startup_total is not None:
        lines.append(_field("startup_total", str(_positive_int(status.startup_total + 1, "startup action count") - 1)))
    seen_runtime_generations: set[int] = set()
    for result in status.results:
        generation = _positive_int(result.generation, "generation", allow_zero=True)
        if generation > 0:
            if generation in seen_runtime_generations:
                raise ValueError("duplicate runtime result generation")
            seen_runtime_generations.add(generation)
        rid = "-" if result.record_id is None else str(_record_id(result.record_id))
        active = "-" if result.active is None else ("1" if result.active else "0")
        value_text = None if result.value is None else _bounded_decoded(result.value, "result value", MAX_STATUS_TEXT_BYTES)
        error_text = None if result.error is None else _bounded_decoded(result.error, "result error", MAX_STATUS_TEXT_BYTES)
        error_code = "-" if result.error_code is None else _runtime_error_code(result.error_code)
        if result.ok and result.error_code is not None:
            raise ValueError("successful runtime result carries an error code")
        value = "-" if value_text is None else percent_encode(value_text)
        error = "-" if error_text is None else percent_encode(error_text)
        lines.append(f"R\t{generation}\t{rid}\t{'1' if result.ok else '0'}\t{active}\t{value}\t{error}\t{error_code}")
    for pid, name in status.processes:
        bounded_name = _process_display_name(name, "process name")
        lines.append(f"P\t{_positive_int(pid, 'PID')}\t{percent_encode(bounded_name)}")
    return _bounded_lines(lines)


def _validate_runtime_command(kind: str, record_id: int | None, value: str | None, target_pid: int | None = None) -> None:
    if kind not in RUNTIME_COMMAND_KINDS:
        raise ValueError("unsupported runtime command")
    if record_id is not None:
        _record_id(record_id)
    if value is not None and not isinstance(value, str):
        raise ValueError("runtime command value must be a string or null")
    if value is not None and utf8_len(value, "runtime command value") > MAX_RUNTIME_COMMAND_VALUE_BYTES:
        raise ValueError("runtime command value is too long")
    if kind in {"query", "set_active", "set_value"} and record_id is None:
        raise ValueError("record command requires MemoryRecord ID")
    if kind == "query" and value is not None:
        raise ValueError("query does not accept a value")
    if kind == "set_active" and value not in {"0", "1"}:
        raise ValueError("set_active value must be '0' or '1'")
    if kind == "set_value" and value is None:
        raise ValueError("set_value requires an explicit string value")
    if kind in WHOLE_SESSION_COMMAND_KINDS and record_id is not None:
        raise ValueError(f"{kind} does not accept a MemoryRecord ID")
    if kind in {"list_processes", "quiesce"} and value is not None:
        raise ValueError(f"{kind} does not accept a value")
    if kind == "retry_attach" and value is not None:
        _process_name(value, "retry_attach target")
    if target_pid is not None:
        _positive_int(target_pid, "target PID")
    if kind == "retry_attach":
        if target_pid is not None and value is None:
            raise ValueError("exact-PID retry_attach requires an expected process basename")
    elif target_pid is not None:
        raise ValueError(f"{kind} does not accept a target PID")


def _bounded_decoded(value: str, field: str, max_bytes: int, *, single_line: bool = False) -> str:
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} is too long")
    if single_line and any(ch in value for ch in ("\x00", "\r", "\n")):
        raise ValueError(f"{field} contains forbidden control characters")
    return value


def _process_display_name(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or utf8_len(value, field) > MAX_STATUS_PROCESS_NAME_BYTES:
        raise ValueError(f"{field} is invalid")
    normalized = unicodedata.normalize("NFC", value)
    if normalized != value:
        raise ValueError(f"{field} must use canonical Unicode normalization")
    if (
        any(ch in value for ch in ("\x00", "\r", "\n"))
        or any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in value)
        or any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in value)
    ):
        raise ValueError(f"{field} contains unsafe display controls")
    return value


def _process_name(value: str, field: str) -> str:
    value = _process_display_name(value, field)
    if "/" in value or "\\" in value or not value.lower().endswith(".exe"):
        raise ValueError(f"{field} must be a basename ending in .exe")
    return value


def percent_encode(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("protocol value must be a string")
    out: list[str] = []
    for byte in utf8_bytes(value, "protocol value"):
        out.append(chr(byte) if byte in _SAFE else f"%{byte:02X}")
    return "".join(out)


def percent_decode(value: str) -> str:
    data = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "%":
            if index + 2 >= len(value) or not re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1:index + 3]):
                raise ValueError("invalid percent encoding")
            data.append(int(value[index + 1:index + 3], 16))
            index += 3
            continue
        code = ord(char)
        if code > 0x7F or code not in _SAFE:
            raise ValueError("unescaped protocol character")
        data.append(code)
        index += 1
    try:
        return bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("protocol value is not valid UTF-8") from exc


def wine_z_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_absolute():
        raise ValueError("Wine Z path requires an absolute POSIX path")
    return "Z:" + str(resolved).replace("/", "\\")


def _effective_startup_preferences(profile: GameProfile) -> list[StartupPreference]:
    """Merge explicit startup settings with optional last-confirmed runtime state.

    Explicit startup preferences remain backward-compatible. When autoload is enabled,
    the exact-SHA remembered state overlays the same record IDs. This never maps IDs
    across table hashes because ProfileStore restores remembered state per exact SHA.
    """
    if not profile.autoload_enabled:
        return list(profile.startup)
    return _effective_preferences(profile.startup, profile.remembered, profile.configured_values)


def _enclosing_script_ids(control, controls: dict[int, object]) -> list[int]:
    """IDs of the controls whose group encloses this one, outermost first.

    A Cheat Engine record inside a script does not exist until that script has
    run, so restoring a remembered child without its parents restores nothing.
    """
    found: list[int] = []
    for depth in range(1, len(control.path)):
        prefix = tuple(control.path[:depth])
        for candidate_id, candidate in controls.items():
            if candidate_id == control.id or tuple(candidate.path) != prefix:
                continue
            if candidate.kind == "group" or candidate.group_header:
                continue
            found.append(candidate_id)
            break
    return found


def _with_enclosing_scripts(
    preferences: list[StartupPreference],
    controls: dict[int, object],
    ambiguous: set[int],
) -> list[StartupPreference]:
    """Switch on the scripts that create every record this selection switches on."""
    by_id = {item.record_id: item for item in preferences}
    for preference in preferences:
        if preference.active is not True:
            continue
        control = controls.get(preference.record_id)
        if control is None:
            continue
        for ancestor_id in _enclosing_script_ids(control, controls):
            if ancestor_id in ambiguous:
                # The record cannot be created without this script, and the
                # script cannot be addressed: two MemoryRecords in this exact
                # table share its ID, so no command names one of them. Skipping
                # it left a plan that reads as acceptable and then waits for a
                # child Cheat Engine will never materialize, with the timeout
                # blamed on the child. A dependency that cannot be addressed
                # makes the plan invalid, and says which record it was.
                raise ValueError(
                    f"startup MemoryRecord {preference.record_id} needs enclosing script "
                    f"{ancestor_id}, which is ambiguous in exact table SHA"
                )
            existing = by_id.get(ancestor_id)
            if existing is None:
                by_id[ancestor_id] = StartupPreference(ancestor_id, True, None)
            elif existing.active is not True:
                by_id[ancestor_id] = StartupPreference(ancestor_id, True, existing.value)
    return [by_id[key] for key in sorted(by_id)]


def _without_orphan_off_preferences(
    preferences: list[StartupPreference],
    controls: dict[int, object],
) -> list[StartupPreference]:
    """Drop switch-off intent for records this startup will never materialize.

    A record created by a script exists only while that script runs. Remembering
    "off" for one - which `Disable all` used to do for the whole table - made
    the next Auto-load wait for a MemoryRecord that could not appear and then
    report the entire startup as failed, even though every cheat really was off.
    Switching a record off that is already absent is a no-op, so the safe plan
    is the one that never asks for it.
    """
    active_ids = {item.record_id for item in preferences if item.active is True}
    kept: list[StartupPreference] = []
    for preference in preferences:
        if preference.active is not False or preference.value is not None:
            kept.append(preference)
            continue
        control = controls.get(preference.record_id)
        if control is None:
            kept.append(preference)
            continue
        ancestors = _enclosing_script_ids(control, controls)
        if ancestors and not all(ancestor in active_ids for ancestor in ancestors):
            continue
        kept.append(preference)
    return kept


def effective_startup_plan(profile: GameProfile, inspection: TableInspection) -> list[StartupAction]:
    """The exact actions a session prepared from this profile would carry.

    Persistence used to budget the fields a profile stores, but the descriptor is
    built from an expansion of them: enclosing scripts a remembered record needs
    are added here and were never counted, so a profile could be accepted as
    within budget and then make every launch impossible. This is the one
    authority both sides use.
    """
    return _startup_actions(_effective_startup_preferences(profile), inspection)


def _held_off_switch_preferences(
    preferences: list[StartupPreference],
    controls: dict[int, object],
    ambiguous: set[int],
) -> list[StartupPreference]:
    """Write the off value to every switch a script would switch on by itself.

    A table's Auto Assembler script declares its own defaults, and one real table
    declares 22 of its 24 flags as on. Auto-load enables that script because a
    remembered cheat needs it, and everything else the script declares comes on
    with it while the panel counts the one cheat that was asked for. The panel
    holds those off for a press it makes itself; without this the next session
    puts them straight back.

    Sorted after the activation it belongs to by the ordering below, because a
    record created by a script has a deeper path than the script and does not
    exist until it has run. These count against the same startup budget as
    everything else, which is what bounds a script with thousands of flags under
    it; no table in the sampled corpus carries a tenth of that.
    """
    named = {preference.record_id for preference in preferences}
    switched_on = [
        control
        for preference in preferences
        if preference.active is True and (control := controls.get(preference.record_id)) is not None
    ]
    held: dict[int, StartupPreference] = {}
    for script in switched_on:
        script_path = getattr(script, "path", ())
        for record_id, control in controls.items():
            # A record whose ID this table uses twice is refused for the plan it
            # is named in, and this must not name one: an addition of ours would
            # then refuse a whole Auto-load the user's own selection could run.
            if record_id in named or record_id in ambiguous or control is script:
                continue
            path = getattr(control, "path", ())
            if len(path) <= len(script_path) or tuple(path[:len(script_path)]) != tuple(script_path):
                continue
            off = _switch_off_value(control)
            if off is not None:
                held[record_id] = StartupPreference(record_id=record_id, active=None, value=off)
    return list(held.values())


def _switch_off_value(control: object) -> str | None:
    """The off key of a switch this record's own script declares as on.

    Only a flag the script declares: its address is a symbol that script
    allocates, so the record resolves once the script has run. Holding off a
    record whose address some other script owns would ask startup for one that
    cannot resolve, and a startup action that fails rolls the whole plan back -
    which would make this hygiene cost the user the cheats they asked for.
    """
    on = getattr(control, "switch_on_value", None)
    values = getattr(control, "dropdown_values", ())
    declared = getattr(control, "declared_default", None)
    if not isinstance(on, str) or not on or len(values) != 2 or declared != on:
        return None
    return next((value for value, _ in values if value != on), None)


def _startup_actions(preferences: Iterable[StartupPreference], inspection: TableInspection) -> list[StartupAction]:
    controls = {}
    ambiguous: set[int] = set()
    for control in inspection.controls:
        if control.id is None:
            continue
        if control.id in controls:
            ambiguous.add(control.id)
        else:
            controls[control.id] = control
    actions: list[StartupAction] = []
    # A table author's own "attach to the game" record is machinery, not a
    # cheat: CE Decky has attached to the exact process long before a startup
    # plan runs, and switching it on re-opens that process by name, which can
    # move Cheat Engine off the exact PID it was given. A profile written before
    # this was recognised can still remember one as active, and replaying it
    # every Auto-load is the one thing it must not do. It stays switchable by
    # hand from the picker, where the user is present to see what happened.
    kept = [
        preference for preference in preferences
        if not getattr(controls.get(preference.record_id), "attach_only", False)
    ]
    expanded = _without_orphan_off_preferences(
        _with_enclosing_scripts(kept, controls, ambiguous), controls
    )
    expanded = expanded + _held_off_switch_preferences(expanded, controls, ambiguous)
    for preference in expanded:
        if preference.record_id in ambiguous:
            raise ValueError(f"startup MemoryRecord {preference.record_id} is ambiguous in exact table SHA")
        control = controls.get(preference.record_id)
        if control is None:
            raise ValueError(f"startup MemoryRecord {preference.record_id} is not present in exact table SHA")
        if control.kind == "group" or control.group_header:
            raise ValueError(f"startup MemoryRecord {preference.record_id} is a presentation-only group header")
        if preference.value is not None:
            if control.dropdown_read_only and preference.value not in {item[0] for item in control.dropdown_values}:
                raise ValueError(f"startup value for MemoryRecord {preference.record_id} is outside read-only dropdown")
            actions.append(StartupAction(preference.record_id, "value", preference.value, control.path))
        if preference.active is not None:
            actions.append(StartupAction(preference.record_id, "active", "1" if preference.active else "0", control.path))
    kind_rank = {"value": 0, "active": 1}
    actions.sort(key=lambda item: (len(item.path), tuple(part.casefold() for part in item.path), kind_rank[item.kind], item.record_id))
    # An activation with another activation of this plan nested inside it is the
    # enclosing script that cheat needs, not the cheat: switching it on proves
    # nothing on its own. What is left is what a successful startup may prove.
    activations = [action for action in actions if action.kind == "active" and action.value == "1"]
    enclosing = {
        action.record_id for action in activations
        if any(len(other.path) > len(action.path) and other.path[:len(action.path)] == action.path
               for other in activations)
    }
    eligible = {action.record_id for action in activations} - enclosing
    return [
        replace(action, proof=True) if action.kind == "active" and action.value == "1" and action.record_id in eligible else action
        for action in actions
    ]


def _wine_z_protocol_path(value: object, name: str) -> str:
    text = _required_string(value, name)
    if not text.startswith("Z:\\"):
        raise ValueError(f"{name} must use a Wine Z: path")
    return text

def _field(name: str, value: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise ValueError("invalid protocol field name")
    return f"F\t{name}\t{percent_encode(value)}"


def _bounded_lines(lines: list[str]) -> bytes:
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > MAX_PROTOCOL_BYTES:
        raise ValueError("protocol payload exceeds size limit")
    return data


def _decode_lines(data: bytes, header: str) -> list[str]:
    if len(data) <= 0 or len(data) > MAX_PROTOCOL_BYTES:
        raise ValueError("protocol payload size is invalid")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("protocol payload is not UTF-8") from exc
    if "\r" in text or "\x00" in text:
        raise ValueError("protocol payload contains forbidden control characters")
    lines = text.splitlines()
    if not lines or lines[0] != header:
        raise ValueError("protocol header mismatch")
    if any(not line for line in lines[1:]):
        raise ValueError("empty protocol lines are not allowed")
    return lines


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{name} contains forbidden control characters")
    if utf8_len(value, name) > 16 * 1024:
        raise ValueError(f"{name} is too long")
    return value


def _positive_int(value: object, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < (0 if allow_zero else 1):
        raise ValueError(f"{name} is out of range")
    return value


def _record_id(value: object) -> int:
    number = _positive_int(value, "MemoryRecord ID", allow_zero=True)
    if number > 0x7FFFFFFF:
        raise ValueError("MemoryRecord ID is out of range")
    return number


def _parse_int(text: str, name: str, *, allow_zero: bool, max_value: int) -> int:
    if not re.fullmatch(r"0|[1-9][0-9]*", text):
        raise ValueError(f"{name} is not a canonical decimal integer")
    value = int(text)
    if value < (0 if allow_zero else 1) or value > max_value:
        raise ValueError(f"{name} is out of range")
    return value


def _parse_bool(text: str, name: str) -> bool:
    if text == "1":
        return True
    if text == "0":
        return False
    raise ValueError(f"{name} must be 0 or 1")


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _bounded_directory_children(path: Path, limit: int, error: str) -> list[Path]:
    if limit <= 0:
        raise ValueError("directory inventory limit must be positive")
    children: list[Path] = []
    # os.scandir yields entries lazily, so the limit applies before an attacker-
    # controlled directory can be materialized into an unbounded Python list.
    with os.scandir(path) as scan:
        for entry in scan:
            if len(children) >= limit:
                raise ValueError(error)
            children.append(Path(entry.path))
    return children


def _sha(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("SHA-256 identity must be a string")
    normalized = value.strip().lower()
    if not _SHA_RE.fullmatch(normalized):
        raise ValueError("SHA-256 identity must be 64 lowercase hexadecimal characters")
    return normalized


def _md5(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("MD5 identity must be a string")
    normalized = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", normalized):
        raise ValueError("MD5 identity must be 32 lowercase hexadecimal characters")
    return normalized


def _uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("session_id must be a string UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("session_id must be a UUID") from exc
    if str(parsed) != value:
        raise ValueError("session_id must use canonical UUID form")
    return value
