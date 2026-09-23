"""Checking for a newer CE Decky, and installing the one that is offered.

The transaction, which `plugin_update.py` deliberately holds none of: the
requests, the durable record, the download, the digest check, and the detached
installer that outlives this process. What may be installed is still decided
there, and the privileged half is still Decky's own loader.

Checking is armed by the user rather than by a clock. A plugin is loaded for as
long as Steam is running, so a timer alone would ask GitHub about a device
nobody is using, for ever. The arming is the same one the provider index uses:
somebody searched for a table recently. `Check now` is the exception, because a
press is a user saying they want an answer now.

The install is one transaction with a hard boundary in the middle. Everything
before the boundary is ours and is cancellable: read the release, download the
archive, verify it against the release's own checksum file. Everything after it
belongs to Decky, which replaces this plugin and stops this process, so the
boundary is where a detached runner takes over and this manager stops being
able to report anything at all. What happened is read back from the runner's
own result file by the backend that loads next.
"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
import asyncio
import json
import os
import shutil
import subprocess
import re
import time
import uuid

from .activity_log import log_activity, log_failure
from .child_env import child_environment
from .network import NetworkError, ProviderRateLimited
from .operations import drainable_tasks
from .paths import PluginPaths
from .plugin_update import (
    ACTIVE_WINDOW_SECONDS,
    API_HOSTS,
    ASSET_HOSTS,
    CHECK_INTERVAL_SECONDS,
    FORCED_CHECK_FLOOR_SECONDS,
    INSTALL_PENDING_LIMIT_SECONDS,
    LATEST_RELEASE_URL,
    MAX_ARCHIVE_BYTES,
    MAX_RELEASE_BYTES,
    MAX_SUMS_BYTES,
    RELEASES_PAGE_URL,
    SCHEDULE_FIRST_DELAY_SECONDS,
    SCHEDULE_INTERVAL_SECONDS,
    ReleaseOffer,
    UpdateError,
    UpdateStateStore,
    checked_now,
    digest_from_sums,
    parse_release,
    parse_version,
    release_version,
)

RESULT_FILENAME = "plugin-update-result.json"
STATE_FILENAME = "plugin-update.json"
# Deliberately not a `.log`: the support bundle collects Decky's own plugin
# logs by that suffix and by age, and a file of ours in that sweep would
# displace one of them. This one is named and collected on its own.
RUNNER_LOG_FILENAME = "plugin-update-runner.jsonl"
# How long after a webhelper replacement the next one waits. Decky stops its own
# service after three inside a minute, and only root can start it again.
WEBHELPER_SPACING_SECONDS = 65.0
# Where a failed install leaves the verified archive, so the manual route in
# Decky still has something to install.
#
# One file with one name, rather than one per version. The name used to carry
# the version it was an installer for, which made "one file, replaced by the
# next attempt" false: a device that failed on two releases kept both, nothing
# ever removed the older one, and the plugin's own deletion did not know either
# of them existed. What the row on screen needs is the path, and it reads that
# out of the record.
KEPT_ARCHIVE_NAME = "CE-Decky-update.zip"
# Interpreters this may run the detached installer with, in order. Decky's own
# runtime is a PyInstaller bundle, so `sys.executable` is the loader rather than
# a Python that can be given a module to run.
SYSTEM_INTERPRETERS = ("/usr/bin/python3", "/usr/local/bin/python3")

# How long a caller that arrived during a check waits for that check's answer
# before being given the record as it stands. The request underneath has its own
# timeout; this is only the guarantee that a screen is answered at all.
CHECK_JOIN_TIMEOUT_SECONDS = 90.0

# How long a verified update waits at the install boundary for a Cheat Engine
# stop to finish, and how often it asks. A stop is bounded by its quiesce wait
# and its termination grace, about half a minute between them; one that has
# not finished by this is not one to replace the backend underneath.
INSTALL_ADMISSION_WAIT_SECONDS = 60.0
INSTALL_ADMISSION_POLL_SECONDS = 0.5

# The states an update is over in. `installing` is deliberately not one of
# them: the install is happening in another process and this plugin is being
# replaced, so a second start would download again and spawn a second installer
# into the middle of that. What reports the outcome is the record the next
# backend reads, and a retry after a failure goes through it.
_SETTLED_STATES = frozenset({"failed", "cancelled"})

_SHA_RE = re.compile(r"[0-9a-f]{64}")


def _admit_always(commit: Callable[[], None]) -> bool:
    commit()
    return True


def system_interpreter() -> str | None:
    """A real Python this plugin can hand a module to, or nothing.

    Nothing is a refusal rather than a fallback: the installer has to outlive
    this process, and there is no way to do that through an interpreter that is
    actually Decky's frozen loader. SteamOS carries `/usr/bin/python3`, so this
    is the branch nobody is expected to meet, and it says what to do instead.
    """
    found = shutil.which("python3")
    for candidate in ((found,) if found else ()) + SYSTEM_INTERPRETERS:
        path = Path(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


class PluginUpdateManager:
    def __init__(
        self,
        paths: PluginPaths,
        network,
        logger,
        *,
        current_version: str,
        auto_check: Callable[[], bool],
        last_search_activity: Callable[[], float],
        admit_install: Callable[[Callable[[], None]], bool] | None = None,
    ) -> None:
        self.paths = paths
        # Asked at the one point after which this backend will be replaced. It
        # runs `commit`, which publishes `installing`, and answers True only
        # when the owner holds nothing a replacement would lose. The owner
        # decides that and commits under its own lock, so what it refuses once
        # the install is published and what it makes the install wait for are
        # one decision.
        self._admit_install = admit_install or _admit_always
        self.network = network
        self.logger = logger
        self.current_version = current_version
        self._auto_check = auto_check
        self._last_search_activity = last_search_activity
        self.state = UpdateStateStore(paths.state_root / STATE_FILENAME)
        self.result_path = paths.state_root / RESULT_FILENAME
        # Named once, because three things have to agree about it: what the
        # installer is told to keep, what this removes after a success or a
        # deletion, and what a screen offers as the manual route.
        self.kept_archive_path = paths.user_home / KEPT_ARCHIVE_NAME
        self._offer: ReleaseOffer | None = None
        # Looked up once per plugin load. The status call asks whether an
        # install is possible at all, and that must not become a PATH scan per
        # status read on a device where nothing about it can change while this
        # process runs.
        self._interpreter = system_interpreter()
        self._operation: dict[str, object] | None = None
        self._task: "asyncio.Task[None] | None" = None
        self._schedule_task: "asyncio.Task[None] | None" = None
        self._activity_task: "asyncio.Task[None] | None" = None
        self._checking = False
        # What this knows and the disk does not, because a write failed. Read
        # over the record by everything that reads it, and emptied field by
        # field as writes succeed.
        self._unwritten: dict[str, object] = {}
        # Set while a check is out, so that a second caller joins that one
        # rather than being handed a snapshot that says a check is running and
        # never hearing how it ended.
        self._check_finished: "asyncio.Event | None" = None
        self._last_forced_at = 0.0
        self._closing = False
        # Refused while the tree this stages into is being deleted. Starting an
        # update does not pass through the service mutation boundary, the same
        # reason downloads have this.
        self._suspended = False
        # The installer this backend started, and the identity of that attempt.
        # The handle is kept so that a runner which exits without replacing
        # this plugin can be noticed by the process that started it, rather
        # than leaving an operation installing until something reloads.
        self._process: "subprocess.Popen | None" = None
        self._attempt: str | None = None
        # What the recovery file was last proved to be, against the size and
        # modification time it had when that was proved. A status read happens
        # every second and re-reading a megabyte for each of them answers a
        # question that has not changed.
        self._archive_identity: tuple[str, int, int] | None = None
        # When this backend first saw an install whose start time is in this
        # clock's future, by attempt. Memory, so that a status read can measure
        # an age without writing anything down.
        self._observed_starts: dict[str, float] = {}

    # ---------------------------------------------------------------- reading

    def _stored(self) -> dict[str, object]:
        """The durable record, with anything this process could not write over it.

        The overlay exists for one case: a check reached GitHub, and the record
        write failed - deliberately, because a report that cannot be saved is
        not a check that did not happen. Everything after that used to read the
        previous answer off the disk, so the one caller holding the result and
        every later reader disagreed, an offer found a moment ago disappeared on
        the next status read, and the pacing - which is also read from here -
        let the next search ask GitHub all over again.

        It is memory, so it lasts exactly as long as this backend does, and any
        successful write replaces it.
        """
        return {**self.state.load(), **self._unwritten} if self._unwritten else self.state.load()

    def snapshot(self, fresh: dict[str, object] | None = None) -> dict[str, object]:
        """What the panel needs, and nothing a screen cannot act on.

        A projection, and nothing else. The panel polls this call and so do
        read-only developer helpers, and `get_status()` promises all of them
        that it writes nothing: a probe that changes the device by observing it
        is the one thing a diagnostic must never be. So an install this backend
        did not start is adopted in memory here, and the durable side - folding
        in a runner's result, writing down when a start time in this clock's
        future was first seen, removing a recovery archive this plugin has
        outgrown - happens in `maintain()`, which the boundaries that are
        allowed to change the device call: loading, polling an update, starting
        one, refusing a deletion, and the scheduler's own tick.
        """
        if self._operation is None:
            self._adopt_pending_install(self._stored().get("install"), persist=False)
        record = self._stored()
        if fresh:
            record = {**record, **fresh}
        latest = record.get("latest_version")
        available = False
        if isinstance(latest, str):
            try:
                available = parse_version(self.current_version) < parse_version(latest)
            except UpdateError:
                available = False
        return {
            "current_version": self.current_version,
            "auto_check": bool(self._auto_check()),
            "latest_version": latest if isinstance(latest, str) else None,
            "update_available": available,
            "checked_at": record.get("checked_at"),
            "last_error": record.get("last_error"),
            "page_url": record.get("page_url") or RELEASES_PAGE_URL,
            "last_result": record.get("last_result"),
            # Its own field, with its own life: an attempt that fails before it
            # has downloaded anything replaces what the last update did and
            # leaves this alone, because this is a file on the device that can
            # still be installed by hand.
            "recovery": self._recovery_view(),
            "install_supported": self._interpreter is not None,
            "checking": self._checking,
            "operation": dict(self._operation) if self._operation is not None else None,
        }

    def consume_runner_result(self) -> None:
        """Fold what the detached installer reported into the durable record.

        Read at load, because the process that wrote it did so while this one
        was starting. The runner owns that file and this owns the record, which
        is what keeps two processes from writing one file; the file is removed
        once what it said is somewhere a screen can read.

        An install that left no result at all is the case this also settles: if
        the version now running is the one that was being installed it plainly
        worked, and if it is not, and the attempt is older than any install
        takes, it is reported as one that did not finish rather than left
        pending for ever.
        """
        # The effective record rather than the file. On a device whose storage
        # has stopped taking writes, this is called again on every status read,
        # and reading the file each time discarded what the last call worked
        # out: the moment a future start time was first noticed was written
        # down in memory and then thrown away and taken again, so the attempt
        # was newly begun for ever and the device went on refusing every update
        # and every deletion.
        record = self._stored()
        pending = record.get("install")
        attempt = pending.get("attempt") if isinstance(pending, dict) else None
        result: dict[str, object] | None = None
        try:
            raw = json.loads(self.result_path.read_text(encoding="utf-8")) if self.result_path.is_file() else None
            if isinstance(raw, dict) and raw.get("schema") == 1:
                # Only the attempt this record is waiting on. A result file is
                # written by a process that outlives the backend that started
                # it, so one left by an attempt that was already settled - by
                # the running version, by the pending limit, or by this backend
                # reconciling a runner that exited - would otherwise be folded
                # in as the outcome of whatever attempt is pending now, and a
                # screen would be told about an install that is one attempt
                # behind. An orphan is logged and removed rather than used.
                if attempt is not None:
                    claimed = raw.get("attempt") == attempt
                elif isinstance(pending, dict):
                    # A record written before attempts existed. The version is
                    # the whole of what it can be matched on, and it is enough:
                    # such a record can only be left by an install this device
                    # started before it was updated to a build that stamps one.
                    claimed = raw.get("version") == pending.get("version")
                else:
                    claimed = False
                if not claimed:
                    log_activity(
                        self.logger, "info", "update.result_discarded",
                        reported=raw.get("attempt"), pending=attempt, waiting=isinstance(pending, dict),
                    )
                    self.result_path.unlink(missing_ok=True)
                else:
                    result = {
                        "attempt": attempt,
                        "version": raw.get("version"),
                        "ok": bool(raw.get("ok")),
                        "error": raw.get("error"),
                        "archive_kept_at": raw.get("archive_kept_at"),
                        "at": raw.get("finished_at"),
                        "restart_requested": bool(raw.get("restart_requested")),
                    }
        except (OSError, ValueError) as exc:
            log_failure(self.logger, "update.result_unreadable", exc, expected=True)
        if result is None and isinstance(pending, dict):
            started = self._observed_start(pending, persist=True)
            target = pending.get("version")
            if target == self.current_version:
                result = {
                    "attempt": attempt, "version": target, "ok": True, "error": None,
                    "archive_kept_at": None, "at": time.time(), "restart_requested": True,
                }
            elif started is None or time.time() - started > INSTALL_PENDING_LIMIT_SECONDS:
                # Or it says nothing about when it started, which is a record
                # that can never be aged out and would otherwise refuse every
                # update this device ever tries again.
                result = {
                    "attempt": attempt,
                    "version": target, "ok": False, "archive_kept_at": None,
                    "error": "the update was started and never reported back", "at": time.time(),
                    "restart_requested": False,
                }
        if result is None:
            # Nothing settled it, and the record still says an installer is out
            # there. It is: this backend is new, the process is not its child,
            # and the only thing that knows about it is this record. Adopting it
            # as the operation this backend owns is what makes a reload stop
            # being a way to start a second privileged install beside the first,
            # or to delete the tree underneath it.
            # Reached from `maintain()` and from loading, both of which may
            # write, so the origin this works out is written down.
            self._adopt_pending_install(pending, persist=True)
            return
        fields: dict[str, object] = {"last_result": result, "install": None}
        if result["ok"]:
            # A completed update makes every earlier finding stale, and what
            # replaces it is nothing rather than this version: the record
            # answers what the newest release is, and after an install nobody
            # has asked that question yet. The next check answers it.
            fields.update({"latest_version": None, "checked_at": None, "last_error": None, "archive_name": None})
            # And the file an earlier attempt left for a manual install is no
            # longer a route to anywhere: this device is now on the version it
            # holds. One file, replaced by the next attempt and removed by a
            # success, which is what the storage contract says it is.
            self.discard_kept_archive()
            fields["recovery"] = None
        else:
            # What this attempt left behind, and only if the file on this device
            # is provably that attempt's archive. There is one such file and
            # successive attempts write to it, so a file being there says
            # nothing about whose it is: without the digest, the archive an
            # earlier failure left was offered as the release this one failed to
            # install. A failure that proves nothing leaves the recovery this
            # device is already holding exactly where it is.
            proven = self._recovery_record(
                result.get("attempt"), result.get("version"),
                pending.get("digest") if isinstance(pending, dict) else None,
            )
            if proven is not None:
                fields["recovery"] = proven
            result["archive_kept_at"] = proven["path"] if proven is not None else None
        # Settling is a report, not an authorization, so it follows the same
        # rule as every other write here: a disk that cannot take it does not
        # get to keep this backend believing an install is still running. The
        # runner's file is left where it is when the write failed, because the
        # backend that loads next has nothing else to read it from.
        if self._record(**fields):
            self.result_path.unlink(missing_ok=True)
        log_activity(
            self.logger, "info", "update.result_consumed",
            ok=result["ok"], version=result.get("version"), error=result.get("error"),
        )

    def _settled_by_record(self, operation_id: str) -> bool:
        """Move the operation to what the record now says this attempt did.

        A successful install is deliberately left where it is: the plugin is
        being replaced, so there is nothing here to settle or to retry, and
        what says it worked is the record the next backend reads.
        """
        result = self._stored().get("last_result")
        if not isinstance(result, dict) or result.get("attempt") != self._attempt:
            return False
        if result.get("ok"):
            return True
        self._set(
            operation_id, state="failed", message="The update could not be installed",
            error=str(result.get("error") or "the updater reported a failure"),
        )
        log_activity(
            self.logger, "info", "update.install_settled_by_record",
            attempt=self._attempt, version=result.get("version"),
        )
        return True

    def _observed_start(self, pending: dict[str, object], *, persist: bool) -> float | None:
        """When this install started, on a clock that can be measured against.

        A handheld whose battery ran flat boots behind everything written on
        it, so a moment recorded before that boot is in this clock's future
        afterwards. Treating the difference as zero each time it is read looks
        like an answer and is not one: nothing ever accumulates, so the attempt
        is young at every read for as many days as the clock is behind, and the
        device goes on refusing every update and every deletion for all of them.

        What it needs is an origin, so the first read that sees a moment in the
        future writes this one down instead. The limit then runs from when the
        clock was noticed, every later read and the next backend measure from
        the same place, and a write that fails still leaves it in memory, where
        this backend reads its own record from.
        """
        started = pending.get("started_at")
        if not isinstance(started, (int, float)):
            return None
        now = time.time()
        if float(started) <= now:
            return float(started)
        # Remembered here first, so that reading the state does not write to it
        # and so that repeated reads measure from one moment rather than from
        # each of themselves. The key is the attempt, because that is what the
        # origin belongs to.
        key = str(pending.get("attempt") or started)
        observed = self._observed_starts.setdefault(key, now)
        if persist and pending.get("started_at") != observed:
            self._record(install={**pending, "started_at": observed})
            log_activity(
                self.logger, "info", "update.pending_start_normalised",
                attempt=pending.get("attempt"), recorded=started, observed=observed,
            )
        return observed

    def _adopt_pending_install(self, pending: object, *, persist: bool) -> None:
        """Own an install this backend did not start but the record is waiting on.

        The operation it rebuilds is deliberately not cancellable and carries
        the attempt it belongs to, so a result written later still settles it
        and the process this one cannot see is never raced by a second one. It
        cannot be polled by a screen - the window that started it died with the
        backend that did - and it does not need to be: what a screen reads is
        the record.
        """
        if not isinstance(pending, dict):
            return
        # Only an install that could still be running. An older one is settled
        # by the record itself, and adopting it would block every later update
        # behind an installer that stopped existing long ago.
        started = self._observed_start(pending, persist=persist)
        if started is None or time.time() - started > INSTALL_PENDING_LIMIT_SECONDS:
            return
        attempt = pending.get("attempt")
        owned = self._operation
        if (
            owned is not None
            and owned.get("state") == "installing"
            and self._attempt == attempt
        ):
            # Already ours. This is reached from the status call, which a screen
            # makes every second, so saying so again would be a line a second in
            # the journal and a new identity for one install.
            return
        self._attempt = attempt if isinstance(attempt, str) else None
        version = pending.get("version")
        self._operation = {
            "operation_id": str(pending.get("operation_id") or uuid.uuid4().hex),
            "state": "installing",
            "version": version if isinstance(version, str) else None,
            "message": "Installing through Decky; Steam's interface will restart",
            "error": None,
            # Says where it came from, because nothing this backend holds can
            # stop it and a screen asking to must be told why rather than
            # silently ignored.
            "adopted": True,
        }
        log_activity(
            self.logger, "info", "update.pending_install_adopted",
            attempt=self._attempt, version=version, started_at=pending.get("started_at"),
        )

    def _archive_matches(self, digest: object) -> bool:
        """Whether the recovery file on this device is the archive that digest names.

        There is one such file and successive attempts write to it, so its
        existence says nothing about which attempt it belongs to. A verified
        release is exactly its digest, and re-reading it is the only thing that
        can tell one from another - so the answer is cached against the file's
        size and modification time, and recomputed the moment either moves.
        """
        if not isinstance(digest, str) or not _SHA_RE.fullmatch(digest):
            return False
        path = self.kept_archive_path
        try:
            if path.is_symlink() or not path.is_file():
                return False
            info = path.stat()
            if info.st_size <= 0:
                return False
        except OSError:
            return False
        identity = (digest, info.st_size, info.st_mtime_ns)
        if self._archive_identity == identity:
            return True
        try:
            observed = _file_digest(path)
        except OSError as exc:
            log_failure(self.logger, "update.kept_archive_unreadable", exc, expected=True)
            return False
        if observed != digest:
            return False
        self._archive_identity = identity
        return True

    def _recovery_record(self, attempt: object, version: object, digest: object) -> dict[str, object] | None:
        """What a preserved archive is, once it is proven to be that archive.

        This is deliberately not part of what the last update did. Those are two
        facts with two lifetimes: an attempt that failed before it downloaded
        anything replaces the first and must not erase the second, which is
        still a file on this device that the user can install by hand.
        """
        if not self._archive_matches(digest):
            return None
        return {
            "attempt": attempt if isinstance(attempt, str) else None,
            "version": version if isinstance(version, str) else None,
            "sha256": digest,
            "path": str(self.kept_archive_path),
        }

    def _recovery_view(self) -> dict[str, object] | None:
        """The recovery this device is holding, re-proved before it is offered.

        Three things stop it being one, and none of them changes anything here:
        this is what a status read is allowed to do. The record may be one this
        cannot read. The file may no longer be the release it says, which is
        what the digest answers. And this device may already be running that
        version - the whole point of the file is that a user can install it
        through Decky by hand, and when they have, nothing in this plugin's own
        transaction runs to notice, because the plugin is simply replaced and
        the next backend starts at the version the file holds. Removing it is
        `_retire_superseded_recovery`, at a boundary that may write.
        """
        recovery = self._stored().get("recovery")
        if not isinstance(recovery, dict) or self._recovery_superseded(recovery) is not False:
            return None
        if not self._archive_matches(recovery.get("sha256")):
            return None
        return recovery

    def _recovery_superseded(self, recovery: dict[str, object]) -> bool | None:
        """Whether this plugin has reached the version that file holds.

        `None` is a record this cannot read, which is neither offered nor acted
        on: nothing is deleted on the strength of a version that will not parse.
        """
        try:
            return parse_version(str(recovery.get("version"))) <= parse_version(self.current_version)
        except UpdateError:
            return None

    def _retire_superseded_recovery(self) -> None:
        """Remove a recovery archive this plugin has outgrown, file and record."""
        recovery = self._stored().get("recovery")
        if not isinstance(recovery, dict) or self._recovery_superseded(recovery) is not True:
            return
        # The file this removes is its own, by name and by directory, the same
        # one it would have offered.
        self.discard_kept_archive()
        log_activity(
            self.logger, "info", "update.recovery_superseded",
            version=recovery.get("version"), running=self.current_version,
        )

    def reset_after_state_deletion(self) -> None:
        """Forget what the deleted state file was backing, without writing.

        Everything this holds in memory is a copy of, or an answer about, that
        file: what a failed write kept so this backend could go on knowing it,
        when a start time in this clock's future was first seen, the release a
        check found, the operation adopted from a pending install, and what the
        recovery archive was last proved to be. A deletion that removes the file
        and leaves those makes this backend the only thing on the device still
        asserting them - which is most likely exactly when it happens, because
        storage that cannot be written is why a user reaches for Delete
        everything in the first place.

        Deliberately not written down. Writing anything here would recreate the
        file in the directory the user has just had emptied.
        """
        self._unwritten.clear()
        self._observed_starts.clear()
        self._archive_identity = None
        self._offer = None
        self._attempt = None
        self._process = None
        self._operation = None
        log_activity(self.logger, "info", "update.state_forgotten")

    def preserved_recovery(self) -> dict[str, object] | None:
        """What proves the recovery archive, for carrying across a deletion.

        The archive is in the user's home and what proves it is in the state
        directory, so a scope that clears one and not the other leaves a large
        file with nothing to say what version it is or that it can be installed
        at all. Only a recovery that is still provably there is carried.
        """
        return self._recovery_view()

    def restore_recovery(self, recovery: dict[str, object]) -> None:
        """Put back what a deletion cleared, for the file it deliberately kept."""
        if not isinstance(recovery, dict) or not self._archive_matches(recovery.get("sha256")):
            return
        self._record(recovery=recovery)
        log_activity(self.logger, "info", "update.recovery_carried", version=recovery.get("version"))

    def discard_kept_archive(self) -> bool:
        """Remove the recovery archive this device is holding, and forget it.

        Called by a successful install, which makes it an installer for the
        past, and by deleting the plugin's data, which is a user saying to
        leave nothing behind. Returns whether a file was removed.
        """
        removed = self._remove_kept_archive(self.kept_archive_path)
        self._archive_identity = None
        if self._stored().get("recovery") is not None:
            self._record(recovery=None)
        return removed

    def _remove_kept_archive(self, path: Path) -> bool:
        """Delete one recovery archive, and nothing that is not one.

        Deliberately narrow: the name this writes, in the one directory it ever
        writes it to, a regular file, never a symlink. It is removing something
        from the user's own home directory, and the only thing it is entitled
        to remove there is the copy it put there itself.
        """
        # Its own name, in the one directory this ever writes it to. The record
        # is a file on disk and the path in it is what this is about to delete,
        # so what makes the claim above true is that both halves are checked
        # rather than either one.
        if path.parent != self.paths.user_home or path.name != KEPT_ARCHIVE_NAME:
            return False
        try:
            if path.is_symlink() or not path.is_file():
                return False
            path.unlink()
        except OSError as exc:
            log_failure(self.logger, "update.kept_archive_not_removed", exc, expected=True)
            return False
        log_activity(self.logger, "info", "update.kept_archive_removed", path=str(path))
        return True

    def _settled_result_fields(self) -> dict[str, object]:
        """Whether what the last install did is still worth a row on a screen.

        Said once. An outcome with nothing left to act on - an update that
        worked, or one that failed before anything was verified - is a line the
        user has either seen or no longer needs, and leaving it there meant a
        refusal from weeks ago sat under the update state for ever.

        The one outcome that stays is the one with an action attached: a failed
        install that kept the verified release for a manual install. That row
        names a path, and it goes only when the path does - when the file is
        gone, or when this device is already on that version and the file is
        an installer for the past.
        """
        return {} if self._stored().get("last_result") is None else {"last_result": None}

    # --------------------------------------------------------------- checking

    def _is_armed(self) -> bool:
        """Whether somebody has used this device recently enough to owe a check."""
        return time.time() - float(self._last_search_activity() or 0.0) <= ACTIVE_WINDOW_SECONDS

    def _is_due(self) -> bool:
        record = self._stored()
        # What paces this is the last time GitHub was asked, not the last time
        # it answered. They are the same on a device that is working; on one
        # that is offline, pacing on the answer would ask again at every tick
        # of the scheduler, which is the anonymous budget spent on a question
        # that cannot be answered. What a screen is told about is the last
        # successful check, which is a different field for that reason.
        attempted = record.get("attempted_at")
        checked = attempted if isinstance(attempted, (int, float)) else record.get("checked_at")
        if not isinstance(checked, (int, float)):
            return True
        age = time.time() - float(checked)
        # A recorded moment in this clock's future has no age, and a device this
        # happens to is not one that should stop asking until wall time catches
        # up with it. It happens: a handheld whose battery ran flat boots with
        # the clock behind everything written on it, and the interval below
        # would then be measured against a check that has not happened yet.
        if age < 0:
            return True
        return age >= CHECK_INTERVAL_SECONDS

    def should_check(self) -> bool:
        return (
            not self._closing
            and not self._suspended
            and bool(self._auto_check())
            and not self._checking
            and self._is_armed()
            and self._is_due()
        )

    async def check(self, *, forced: bool) -> dict[str, object]:
        """Ask GitHub once what the newest stable release is.

        A forced check ignores the arming and the interval, because it is a
        press. It keeps one floor, which is the anonymous request budget this
        shares with the table source that searches GitHub: a control can be
        pressed as fast as a finger moves.

        One request at a time, and a caller who arrives during one waits for
        it rather than being handed a snapshot of it. That snapshot says a
        check is running, and the screen that took it is opened with a copy and
        never re-rendered from the panel, so it said a check was running for as
        long as it stayed open - after the check itself had finished and found
        something.
        """
        if self._closing:
            raise RuntimeError("plugin is unloading")
        if self._suspended:
            raise ValueError("plugin data is being deleted; check again when it finishes")
        # Joining comes first, and before the floor. Whether a caller may start
        # a request and whether it may hear how the running one ended are two
        # questions, and answering the second with the floor is how a second
        # Check now - a slow one, a panel closed and opened over it - was handed
        # a snapshot that said a check was running and never heard the end of
        # it. That is the state the join exists to remove.
        if self._checking:
            return await self._join_check()
        # The floor is about presses, and about the anonymous GitHub budget this
        # shares with the table source. Pacing a press against the background
        # timer as well would answer Check now from a record the user cannot
        # see the age of, on a device whose scheduler happened to tick first.
        if forced and self._last_forced_at and time.monotonic() - self._last_forced_at < FORCED_CHECK_FLOOR_SECONDS:
            return self.snapshot()
        self._checking = True
        self._check_finished = asyncio.Event()
        if forced:
            self._last_forced_at = time.monotonic()
        try:
            latest, offer = await self._read_latest_release()
        except Exception as exc:  # noqa: BLE001 - every failure is a line on a screen
            # Cleared before the snapshot rather than in a `finally`, which runs
            # after the returned expression is evaluated: the screen would
            # otherwise be handed a failed check that says it is still running.
            self._finish_check()
            # `attempted_at` is what paces the next one; `checked_at` stays at
            # the last check that actually answered. Writing the moment of a
            # failure there stamped whatever the record still held - an offer
            # found days ago - with today's date, so a device that could not
            # reach GitHub read as one that had just confirmed an update.
            self._record(attempted_at=time.time(), last_error=_reason(exc))
            log_failure(
                self.logger, "update.check_failed", exc,
                expected=isinstance(exc, (UpdateError, NetworkError, ValueError, OSError)), forced=forced,
            )
            return self.snapshot()
        finally:
            self._finish_check()
        self._offer = offer
        answer = checked_now(offer, latest, current_version=self.current_version)
        # Laid over the record for the answer this returns, so that a state
        # write this deliberately does not fail the check over cannot make the
        # check report the previous answer either.
        # A check that answered is also the moment to let go of an outcome
        # nobody can act on any more.
        self._record(**answer, **self._settled_result_fields())
        log_activity(
            self.logger, "info", "update.checked",
            forced=forced, current=self.current_version, latest=latest,
            available=offer is not None, unwritten=sorted(self._unwritten),
        )
        return self.snapshot()

    def _finish_check(self) -> None:
        """Let go of the single-flight gate, once, and wake anybody waiting."""
        self._checking = False
        waiter, self._check_finished = self._check_finished, None
        if waiter is not None:
            waiter.set()

    async def _join_check(self) -> dict[str, object]:
        """Wait out the check already running, and answer with what it found.

        Bounded, because this is a screen waiting: the request underneath has
        its own timeout, and a wait that outlives it answers with the record as
        it stands rather than never returning.
        """
        waiter = self._check_finished
        if waiter is not None:
            try:
                await asyncio.wait_for(waiter.wait(), timeout=CHECK_JOIN_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, RuntimeError) as exc:
                log_failure(self.logger, "update.check_join_timed_out", exc, expected=True)
        return self.snapshot()

    async def _read_latest_release(self) -> tuple[str, ReleaseOffer | None]:
        """The newest stable release, and what of it this device can install."""
        response = await self.network.get(
            LATEST_RELEASE_URL,
            allowed_hosts=API_HOSTS,
            max_bytes=MAX_RELEASE_BYTES,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        if response.status == 404:
            raise UpdateError("this project has published no release yet")
        if response.status in {403, 429}:
            # GitHub answers an exhausted anonymous budget with either, and both
            # mean the same thing to a user: wait. The header it names is kept
            # because the transport's own type carries it.
            raise ProviderRateLimited(
                response.headers.get("retry-after"),
                "GitHub is rate limiting this device; it allows sixty anonymous requests an hour",
            )
        if response.status != 200:
            raise UpdateError(f"GitHub answered {response.status}")
        try:
            payload = json.loads(response.body)
        except ValueError as exc:
            raise UpdateError("the release answer is not readable JSON") from exc
        return str(release_version(payload)), parse_release(payload, current_version=self.current_version)

    def note_user_activity(self) -> None:
        """Somebody just used the plugin, so ask now rather than at the next tick.

        The timer exists for the device nobody is touching. When a search has
        just happened the user is here, and holding the answer for up to one
        whole interval is what made a panel say nothing about an update that was
        one request away. Everything that decides whether a check happens at all
        is unchanged: the switch, the interval since the last check, and a check
        already running.

        It never raises and never waits: the caller is a search returning its
        own answer, and this is not part of that answer.
        """
        try:
            if not self.should_check():
                return
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        except Exception as exc:  # noqa: BLE001 - a check offer never fails a search
            log_failure(self.logger, "update.activity_check_not_started", exc, expected=True)
            return
        if self._activity_task is not None and not self._activity_task.done():
            return
        self._activity_task = loop.create_task(self._checked_after_activity())

    async def _checked_after_activity(self) -> None:
        try:
            await self.check(forced=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the check records its own failures
            log_failure(self.logger, "update.activity_check_failed", exc, expected=True)

    def start_background_checks(self) -> None:
        """Look, on a timer, at whether a check is owed. Started once per load."""
        if self._schedule_task is not None and not self._schedule_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Constructed outside a loop, which is a test or a probe rather than
            # the plugin. There is nothing to schedule against.
            return
        self._schedule_task = asyncio.create_task(self._schedule())
        log_activity(
            self.logger, "info", "update.checks_scheduled",
            first_in_s=SCHEDULE_FIRST_DELAY_SECONDS, every_s=SCHEDULE_INTERVAL_SECONDS,
            auto_check=bool(self._auto_check()), armed=self._is_armed(),
        )

    async def _schedule(self) -> None:
        """One tick's failure is one tick's failure, and the interval is the backoff."""
        try:
            await asyncio.sleep(SCHEDULE_FIRST_DELAY_SECONDS)
            while True:
                try:
                    # A device nobody is looking at still has an installer that
                    # may have stopped and a recovery archive that may have been
                    # used. The status call no longer writes, so this tick is
                    # where that is noticed when no screen is open.
                    self.maintain()
                    if self.should_check():
                        # Awaited rather than spawned. A task nobody retrieves
                        # the result of reports its failure as an unretrieved
                        # exception at some later garbage collection, which is a
                        # line in the log with no run attached to it; this tick
                        # has nothing else to do while the request is out, and
                        # unload cancels it through this coroutine.
                        await self.check(forced=False)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a tick failure fails nothing else
                    log_failure(self.logger, "update.check_tick_failed", exc, expected=True)
                await asyncio.sleep(SCHEDULE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log_failure(self.logger, "update.check_schedule_failed", exc, expected=False)

    # -------------------------------------------------------------- installing

    def status(self, operation_id: str) -> dict[str, object]:
        if self._operation is None or self._operation.get("operation_id") != operation_id:
            raise ValueError("plugin update operation is unknown")
        # Asked here as well as where a second update would be refused: this is
        # the call the window watching an install makes every second, and it is
        # the one that has to stop saying `installing` when the installer is no
        # longer there to finish. It is a poll of one operation rather than the
        # panel's status read, so it is allowed to change what it finds.
        self.maintain()
        return dict(self._operation)

    def maintain(self) -> None:
        """The durable half of reconciling, at a boundary that may write.

        Everything here is something a read must not do: consuming the file a
        detached installer left and deleting it, writing down the moment a start
        time in this clock's future was first seen, and removing a recovery
        archive whose version this plugin has reached. The status call projects
        what is already known; this is where the device changes, and it is
        called from the paths that are allowed to change it.
        """
        # In this order: fold in what an installer left and settle an attempt
        # that has outlived any install, settle the operation this backend is
        # holding, adopt one it is not, and let go of a recovery archive this
        # plugin has outgrown.
        self.consume_runner_result()
        self._reconcile_installing()
        if self._operation is None:
            self._adopt_pending_install(self._stored().get("install"), persist=True)
        self._retire_superseded_recovery()

    def peek_active_operation(self) -> bool:
        """Whether an update is happening, without changing anything to find out.

        For the readiness report, which is a screen saying what a deletion would
        refuse and promising that nothing is deleted by looking - and for the
        panel reader that is allowed to press **Check** for exactly that reason.
        The answer is this process plus the durable pending record, read the way
        the status call reads it: adoption in memory, nothing written, nothing
        removed.

        It errs towards busy. A result file the boundary has not folded in yet
        says the installer has reported, so an attempt with one is not treated
        as running; anything else that looks like an install in flight is
        reported as one, because a report that says the device is free when it
        is not is the failure that matters here.
        """
        if self._task is not None and not self._task.done():
            return True
        if self._operation is None:
            self._adopt_pending_install(self._stored().get("install"), persist=False)
            if self._operation is not None and self.result_path.is_file():
                return False
        operation = self._operation
        return operation is not None and str(operation.get("state")) not in _SETTLED_STATES

    def has_active_operation(self) -> bool:
        """Whether an update is happening, including one this backend did not start.

        The record is asked as well as this process, because the installer is a
        detached process that outlives the backend: after a reload the only
        thing that knows an install is still out there is what it wrote down.
        Everything that has to hold that boundary asks through here - a second
        update, and deleting the tree the first one is installing from.
        """
        self.maintain()
        if self._task is not None and not self._task.done():
            return True
        operation = self._operation
        return operation is not None and str(operation.get("state")) not in _SETTLED_STATES

    def _reconcile_installing(self) -> None:
        """Settle an install whose installer is gone while this plugin is not.

        Handing the archive to the detached runner is the point where this
        process stops being able to report anything, and that is true because
        Decky is about to stop it. When it does not - the loader refuses the
        install, the runner cannot start, Python falls over before it writes
        anything - nothing replaces this backend, and the operation it left
        behind said `installing` for as long as the plugin stayed loaded. Every
        retry was then refused as an update already running, so one transient
        failure cost the device its self-update until something reloaded it.

        Two things can settle it here, and both are facts rather than timeouts:
        the runner's own result for this exact attempt, and the exit of the
        process this backend started. An exit with no result is its own
        outcome, not a success and not silence.
        """
        operation = self._operation
        if operation is None or operation.get("state") != "installing":
            return
        try:
            self._settle_installing(operation)
        except Exception as exc:  # noqa: BLE001 - reconciling never fails the call that asked
            log_failure(self.logger, "update.install_not_reconciled", exc, expected=True)

    def _settle_installing(self, operation: dict[str, object]) -> None:
        operation_id = str(operation.get("operation_id"))
        if self.result_path.is_file():
            self.consume_runner_result()
        # Asked whether or not there was a file to fold in a moment ago: what
        # settles this operation is the record, and the fold may already have
        # happened - `maintain()` does it before this runs.
        if self._settled_by_record(operation_id):
            return
        process = self._process
        if process is None:
            # An install this backend adopted from the record rather than
            # started: the process is not its child and cannot be asked
            # anything. What settles it is the record - a result arriving late,
            # or the attempt outliving any install - and nothing else can, so
            # asking the record is the whole of what there is to do.
            if operation.get("adopted"):
                self.consume_runner_result()
                self._settled_by_record(operation_id)
            return
        if process.poll() is None:
            return
        # The runner writes its result before it exits, so a process that is
        # gone without one did not get that far.
        # What it left behind, if it got that far. The installer verifies the
        # archive and moves it to this exact path before it writes anything
        # else, and the two failures are correlated: a disk that cannot take the
        # result file is a disk that has just been written to. Discarding it
        # here threw away the one thing the manual route is for, in exactly the
        # case it exists for - and taking it on trust would have offered an
        # earlier attempt's archive as this one's, so it is proved first.
        pending = self._stored().get("install")
        proven = self._recovery_record(
            self._attempt, operation.get("version"),
            pending.get("digest") if isinstance(pending, dict) else None,
        )
        fields: dict[str, object] = {"install": None, "last_result": {
            "attempt": self._attempt,
            "version": operation.get("version"),
            "ok": False,
            "error": "the updater stopped without reporting what happened",
            "archive_kept_at": proven["path"] if proven is not None else None,
            "at": time.time(),
            "restart_requested": False,
        }}
        if proven is not None:
            fields["recovery"] = proven
        self._record(**fields)
        self._set(
            operation_id, state="failed", message="The update could not be installed",
            error="the updater stopped without reporting what happened",
        )
        self._process = None
        log_activity(
            self.logger, "warning", "update.installer_exited_without_result",
            attempt=self._attempt, code=process.returncode,
        )

    async def start(self, *, expected_version: str | None = None) -> dict[str, object]:
        """Begin one update: read the release again, fetch it, verify it, install.

        The release is read again rather than taken from the last check,
        because what is installed must be what is published now and because the
        answer is one request. It is also the last moment anything here can
        refuse.

        `expected_version` is what the user was shown when they pressed. The
        press is consent for a named version, not for whatever `latest` says by
        the time the request is served, so a release that has moved on between
        the two stops this and asks again rather than installing something
        nobody confirmed. `None` is for a caller with no screen - a test or a
        probe - and states that there is no confirmation to honour.
        """
        if self._closing:
            raise RuntimeError("plugin is unloading")
        if self._suspended:
            raise ValueError("plugin data is being deleted; update again when it finishes")
        if self.has_active_operation():
            raise ValueError("a plugin update is already running")
        if self._interpreter is None:
            raise ValueError(
                "this device has no python3 to run the updater with; "
                "install the release manually from Decky instead"
            )
        operation_id = uuid.uuid4().hex
        self._process = None
        self._attempt = None
        self._operation = {
            "operation_id": operation_id,
            "state": "checking",
            "version": expected_version,
            "message": "Reading the newest release",
            "error": None,
        }
        self._task = asyncio.create_task(self._run(operation_id, expected_version))
        return self.status(operation_id)

    async def cancel(self, operation_id: str) -> dict[str, object]:
        """Stop an update that has not reached Decky yet.

        Only the half this process owns can be cancelled. Once the installer is
        spawned the plugin is being replaced, and there is nothing here left to
        stop; the screen says so before the press rather than discovering it.
        """
        self.status(operation_id)
        if str(self._operation.get("state")) == "installing":  # type: ignore[union-attr]
            raise ValueError("this update is already installing and cannot be cancelled")
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        return self.status(operation_id)

    async def _run(self, operation_id: str, expected_version: str | None = None) -> None:
        archive: Path | None = None
        # This run's own offer. Reading `self._offer` here would name whatever
        # an earlier check happened to find, on exactly the path where this run
        # found something different or nothing at all.
        offer: ReleaseOffer | None = None
        try:
            latest, offer = await self._read_latest_release()
            self._record(**checked_now(offer, latest, current_version=self.current_version))
            if offer is None:
                raise UpdateError("this is already the newest release")
            # What the user confirmed, against what the project is publishing
            # now. They are the same on every ordinary press; when they are not,
            # the release moved between the screen and this request, and nobody
            # has agreed to the one that is there now.
            if expected_version is not None and offer.version != expected_version:
                raise UpdateError(
                    f"this release is now v{offer.version}, not the v{expected_version} you confirmed; "
                    "check again and confirm the version you want"
                )
            self._offer = offer
            self._set(operation_id, state="downloading", version=offer.version, message=f"Downloading {offer.archive_name}")
            archive, digest = await self._fetch_verified_archive(offer)
            await self._await_install_admission(operation_id)
            self._spawn_runner(offer, archive, digest, operation_id)
        except asyncio.CancelledError:
            _discard(archive)
            self._set(operation_id, state="cancelled", message="The update was cancelled", error=None)
            log_activity(self.logger, "info", "update.cancelled", operation=operation_id[:12])
            raise
        except Exception as exc:  # noqa: BLE001 - the operation carries the outcome
            _discard(archive)
            reason = _reason(exc)
            self._set(operation_id, state="failed", message="The update could not be installed", error=reason)
            # Recorded as what the last update did, never as what the last check
            # found. They are different questions and the screen asks them
            # separately: an install that refused itself was being reported as a
            # check that did not finish, on a device whose check had in fact
            # just succeeded. Nothing is kept here for a manual install, because
            # nothing got as far as being verified.
            # `install` is cleared with it. Everything that can fail here fails
            # before the installer is started - the spawn is the last statement
            # of the last call in the block - so a pending install left in the
            # record on this path is one that never began, and the next backend
            # would read it as an update that started and never reported back.
            self._record(install=None, last_result={
                "version": offer.version if offer is not None else None,
                "ok": False,
                "error": reason,
                "archive_kept_at": None,
                "at": time.time(),
                "restart_requested": False,
            })
            log_failure(
                self.logger, "update.install_failed", exc,
                expected=isinstance(exc, (UpdateError, NetworkError, ValueError, OSError)),
                operation=operation_id[:12],
            )

    async def _fetch_verified_archive(self, offer: ReleaseOffer) -> tuple[Path, str]:
        """The release's archive on disk, proven to be what the release says.

        The checksum file is fetched first and on its own: it is the release's
        own statement about the bytes, it is a few dozen of them, and reading
        it first means a release that cannot say what its archive is costs no
        download at all.
        """
        sums = await self.network.get(offer.sums_url, allowed_hosts=ASSET_HOSTS, max_bytes=MAX_SUMS_BYTES)
        if sums.status != 200:
            raise UpdateError(f"the release checksum file answered {sums.status}")
        digest = digest_from_sums(sums.body, offer.archive_name)
        staging = self.paths.temp_root / "updates"
        _clear(staging)
        staging.mkdir(parents=True, exist_ok=True)
        destination = staging / offer.archive_name
        # Every way out of the transfer but the one that returns the file takes
        # the file with it. A cancelled or failed download leaves a partial
        # archive in the managed tree otherwise, and the caller cannot clean it
        # up because it never learned the path.
        try:
            response = await self.network.download(
                offer.archive_url, destination, allowed_hosts=ASSET_HOSTS, max_bytes=MAX_ARCHIVE_BYTES,
            )
            if response.status != 200:
                raise UpdateError(f"the release archive answered {response.status}")
            observed = await asyncio.to_thread(_file_digest, destination)
            if observed != digest:
                raise UpdateError("the downloaded archive is not what this release says it is")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        log_activity(
            self.logger, "info", "update.archive_verified",
            version=offer.version, bytes=destination.stat().st_size, sha256=digest[:12],
        )
        return destination, digest

    async def _await_install_admission(self, operation_id: str) -> None:
        """Publish `installing` only where the owner says this backend may be replaced.

        A stop that has ended Cheat Engine and not yet written down what it
        left is held only in this process, and the install replaces it. So the
        update waits for it here, still cancellable, and gives up rather than
        install over one that does not finish.
        """
        def commit() -> None:
            self._set(operation_id, state="installing", message="Installing through Decky; Steam's interface will restart")

        deadline = time.monotonic() + INSTALL_ADMISSION_WAIT_SECONDS
        waited = False
        while not self._admit_install(commit):
            if not waited:
                waited = True
                self._set(operation_id, message="Waiting for Cheat Engine to finish stopping before installing")
                log_activity(self.logger, "info", "update.install_waiting_for_stop", operation=operation_id[:12])
            if time.monotonic() >= deadline:
                log_activity(self.logger, "warning", "update.install_refused_stop_in_progress", operation=operation_id[:12])
                raise UpdateError(
                    "Cheat Engine was still being stopped in a game, so the update was not installed; "
                    "update again once it has stopped"
                )
            await asyncio.sleep(INSTALL_ADMISSION_POLL_SECONDS)

    def replacement_committed(self) -> bool:
        """Whether an install that will replace this backend has been handed on.

        This process's own `installing` operation, or one a backend before it
        handed to a runner that is still going, read the way the readiness
        report reads it: nothing written, nothing removed.
        """
        if self._operation is None:
            self._adopt_pending_install(self._stored().get("install"), persist=False)
            if self._operation is not None and self.result_path.is_file():
                return False
        operation = self._operation
        return operation is not None and operation.get("state") == "installing"

    def _spawn_runner(self, offer: ReleaseOffer, archive: Path, digest: str, operation_id: str) -> None:
        """Hand the install to a process Decky stopping this one cannot take.

        Detached into its own session on purpose: the install replaces this
        plugin, and a child in this process group would be stopped in the
        middle of it. Everything the runner needs is imported before the
        install begins, which is what lets it keep working while the tree it
        was imported from is being replaced.
        """
        interpreter = self._interpreter
        if interpreter is None:
            raise ValueError("this device has no python3 to run the updater with")
        record = self.state.load()
        replaced_at = record.get("webhelper_replaced_at")
        not_before = float(replaced_at) + WEBHELPER_SPACING_SECONDS if isinstance(replaced_at, (int, float)) else 0.0
        attempt = uuid.uuid4().hex
        kept = self.kept_archive_path
        environment = child_environment()
        environment["PYTHONPATH"] = str(self.paths.plugin_dir / "py_modules")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            interpreter, "-m", "ce_decky.update_runner",
            "--archive", str(archive),
            "--digest", digest,
            "--version", offer.version,
            "--result", str(self.result_path),
            "--log", str(self.paths.log_dir / RUNNER_LOG_FILENAME),
            "--keep-on-failure", str(kept),
            "--not-before", f"{not_before:.3f}",
            # Carried through the whole attempt: the record this writes below,
            # the installer's arguments, and the result it leaves behind. What
            # it is for is that a result can outlive the attempt it belongs to,
            # and one attempt must never settle another.
            "--attempt", attempt,
        ]
        # Strict, unlike every other write to this record: it is written before
        # the installer is started and it is what the next backend reads to know
        # an install was in flight at all. A device that cannot write a few dozen
        # bytes into its own state directory is not one to start replacing a
        # plugin on, so the failure is the operation's and no installer runs.
        self.state.update(
            install={
                "attempt": attempt,
                # What the archive for this attempt is. A recovery file found
                # afterwards is only this attempt's if it carries this digest;
                # without it, the file an earlier failure left was reported as
                # the release this one had failed to install.
                "digest": digest,
                # Carried so that a backend loading after this one adopts the
                # same operation rather than inventing a second identity for an
                # install that is already happening.
                "operation_id": operation_id,
                "version": offer.version,
                "started_at": time.time(),
                "archive_kept_at": str(kept),
            },
            webhelper_replaced_at=time.time(),
        )
        self._attempt = attempt
        # Said before the act rather than after it, and this is the reason:
        # starting the installer is the one thing here that cannot be taken
        # back, and the caller treats anything raised out of this method as an
        # install that did not happen - it deletes the archive and records a
        # failure. A line written afterwards would be a statement that can
        # throw, standing between a running installer and the process that
        # would then delete the file out from under it. The runner's own log
        # says what happened next; this one says what was asked for.
        log_activity(
            self.logger, "info", "update.installer_spawning",
            attempt=attempt, version=offer.version, interpreter=interpreter,
            waits_s=round(max(not_before - time.time(), 0.0), 1),
        )
        # Detached and in its own session, and still held: the session is what
        # keeps Decky stopping this plugin from stopping the install, and the
        # handle is only how the process that is still here notices an
        # installer that has gone without finishing.
        self._process = subprocess.Popen(  # noqa: S603 - fixed argv, plugin-owned paths, no shell
            command,
            cwd=str(self.paths.state_root),
            env=environment,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _record(self, **fields: object) -> bool:
        """Write the durable record without letting it fail what it describes.

        The record is a report. A check that reached GitHub and got an answer
        succeeded whether or not a full disk let the answer be written down, and
        reporting it as a failed check would be a statement about the wrong
        thing. The same rule the search marker follows.

        What it does not do any more is let the answer be lost with the write.
        A write that failed leaves its fields in memory, where every reader of
        this record looks first, so that this backend goes on knowing what it
        just learned - including the pacing, which would otherwise ask GitHub
        again at the next search, every search, for as long as the disk stayed
        unwritable.
        """
        try:
            self.state.update(**fields)
        except Exception as exc:  # noqa: BLE001 - a record write never fails the work
            self._unwritten.update(fields)
            log_failure(self.logger, "update.record_not_written", exc, expected=True)
            return False
        # Written, so the copy in memory is no longer what this knows and the
        # file is: anything it still held about these fields is now stale.
        for field in fields:
            self._unwritten.pop(field, None)
        return True

    def _set(self, operation_id: str, **values: object) -> None:
        if self._operation is not None and self._operation.get("operation_id") == operation_id:
            self._operation.update(values)

    # ------------------------------------------------------------- suspending

    def suspend(self) -> None:
        """Refuse checks and new updates until resumed.

        For deletion, and for the same reason downloads have it: neither
        starting an update nor a check on the scheduler's own tick passes
        through the service mutation boundary, so a deletion that checked once
        could have an archive staged under it, or its freshly cleared state
        directory written back into, immediately afterwards.
        """
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False

    async def drain_check(self) -> None:
        """Wait out a check that is already running. Never raises.

        The refusal above stops the next one; this is for the one that is
        already out, because what it does when it returns is write the record
        into the directory a deletion is about to clear.
        """
        if self._checking:
            await self._join_check()

    # ---------------------------------------------------------------- closing

    def begin_close(self) -> None:
        self._closing = True

    async def close(self) -> None:
        """Stop what this loop started, and wait for it.

        Only what this loop started: a task belonging to another loop, or to one
        that has already been closed, cannot be cancelled or waited on from
        here, and asking anyway raises `Event loop is closed` out of unload on
        the Python the device runs. On the device this removes nothing, because
        Decky keeps one loop for the whole plugin process; it is for every other
        caller, and `drainable_tasks` is where the reasoning is written down.
        """
        self._closing = True
        tasks = drainable_tasks((self._schedule_task, self._activity_task, self._task))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discard(archive: Path | None) -> None:
    if archive is not None:
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass


def _clear(staging: Path) -> None:
    """Leave no earlier attempt's archive behind in the staging directory."""
    try:
        if staging.is_dir() and not staging.is_symlink():
            for entry in staging.iterdir():
                if entry.is_file() and not entry.is_symlink():
                    entry.unlink(missing_ok=True)
    except OSError:
        pass


def _reason(exc: BaseException) -> str:
    """What a screen can show about a failure, bounded and never a traceback."""
    text = str(exc).strip() or exc.__class__.__name__
    return text[:400]
