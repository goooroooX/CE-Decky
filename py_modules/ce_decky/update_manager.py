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
# Decky still has something to install. One file, replaced by the next attempt.
KEPT_ARCHIVE_PREFIX = "CE-Decky-update-"
# Interpreters this may run the detached installer with, in order. Decky's own
# runtime is a PyInstaller bundle, so `sys.executable` is the loader rather than
# a Python that can be given a module to run.
SYSTEM_INTERPRETERS = ("/usr/bin/python3", "/usr/local/bin/python3")

# How long a caller that arrived during a check waits for that check's answer
# before being given the record as it stands. The request underneath has its own
# timeout; this is only the guarantee that a screen is answered at all.
CHECK_JOIN_TIMEOUT_SECONDS = 90.0

# The states an update is over in. `installing` is deliberately not one of
# them: the install is happening in another process and this plugin is being
# replaced, so a second start would download again and spawn a second installer
# into the middle of that. What reports the outcome is the record the next
# backend reads, and a retry after a failure goes through it.
_SETTLED_STATES = frozenset({"failed", "cancelled"})


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
    ) -> None:
        self.paths = paths
        self.network = network
        self.logger = logger
        self.current_version = current_version
        self._auto_check = auto_check
        self._last_search_activity = last_search_activity
        self.state = UpdateStateStore(paths.state_root / STATE_FILENAME)
        self.result_path = paths.state_root / RESULT_FILENAME
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

    # ---------------------------------------------------------------- reading

    def snapshot(self, fresh: dict[str, object] | None = None) -> dict[str, object]:
        """What the panel needs, and nothing a screen cannot act on.

        `fresh` is what a check has just established, laid over what is stored.
        It exists for the one case where those differ: the record write failed,
        deliberately without failing the check that reached GitHub, and the
        caller of that check would otherwise be answered out of storage that
        still holds the previous answer. A device that cannot write its own
        state is not a reason to tell the user something that is no longer
        true.
        """
        record = self.state.load()
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
        record = self.state.load()
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
            started = pending.get("started_at")
            target = pending.get("version")
            if target == self.current_version:
                result = {
                    "attempt": attempt, "version": target, "ok": True, "error": None,
                    "archive_kept_at": None, "at": time.time(), "restart_requested": True,
                }
            elif isinstance(started, (int, float)) and time.time() - float(started) > INSTALL_PENDING_LIMIT_SECONDS:
                result = {
                    "attempt": attempt,
                    "version": target, "ok": False, "archive_kept_at": pending.get("archive_kept_at"),
                    "error": "the update was started and never reported back", "at": time.time(),
                    "restart_requested": False,
                }
        if result is None:
            return
        fields: dict[str, object] = {"last_result": result, "install": None}
        if result["ok"]:
            # A completed update makes every earlier finding stale, and what
            # replaces it is nothing rather than this version: the record
            # answers what the newest release is, and after an install nobody
            # has asked that question yet. The next check answers it.
            fields.update({"latest_version": None, "checked_at": None, "last_error": None, "archive_name": None})
        self.state.update(**fields)
        self.result_path.unlink(missing_ok=True)
        log_activity(
            self.logger, "info", "update.result_consumed",
            ok=result["ok"], version=result.get("version"), error=result.get("error"),
        )

    # --------------------------------------------------------------- checking

    def _is_armed(self) -> bool:
        """Whether somebody has used this device recently enough to owe a check."""
        return time.time() - float(self._last_search_activity() or 0.0) <= ACTIVE_WINDOW_SECONDS

    def _is_due(self) -> bool:
        record = self.state.load()
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
        # The floor is about presses. Pacing a press against the background
        # timer as well would answer Check now from a record the user cannot
        # see the age of, on a device whose scheduler happened to tick first.
        if forced and self._last_forced_at and time.monotonic() - self._last_forced_at < FORCED_CHECK_FLOOR_SECONDS:
            return self.snapshot()
        if self._checking:
            return await self._join_check()
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
        written = self._record(**answer)
        log_activity(
            self.logger, "info", "update.checked",
            forced=forced, current=self.current_version, latest=latest,
            available=offer is not None, recorded=written,
        )
        return self.snapshot(None if written else answer)

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
        # longer there to finish.
        self._reconcile_installing()
        return dict(self._operation)

    def has_active_operation(self) -> bool:
        self._reconcile_installing()
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
            result = self.state.load().get("last_result")
            if isinstance(result, dict) and result.get("attempt") == self._attempt:
                if result.get("ok"):
                    # The install landed, so this plugin is being replaced and
                    # there is nothing here to settle or retry: what says so is
                    # the record, which the backend that loads next reads.
                    return
                self._set(
                    operation_id, state="failed", message="The update could not be installed",
                    error=str(result.get("error") or "the updater reported a failure"),
                )
                log_activity(
                    self.logger, "info", "update.install_settled_by_result",
                    attempt=self._attempt, version=result.get("version"),
                )
                return
        process = self._process
        if process is None or process.poll() is None:
            return
        # The runner writes its result before it exits, so a process that is
        # gone without one did not get that far.
        self._record(install=None, last_result={
            "attempt": self._attempt,
            "version": operation.get("version"),
            "ok": False,
            "error": "the updater stopped without reporting what happened",
            "archive_kept_at": None,
            "at": time.time(),
            "restart_requested": False,
        })
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
            self._set(operation_id, state="installing", message="Installing through Decky; Steam's interface will restart")
            self._spawn_runner(offer, archive, digest)
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

    def _spawn_runner(self, offer: ReleaseOffer, archive: Path, digest: str) -> None:
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
        kept = self.paths.user_home / f"{KEPT_ARCHIVE_PREFIX}v{offer.version}.zip"
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
        """
        try:
            self.state.update(**fields)
            return True
        except Exception as exc:  # noqa: BLE001 - a record write never fails the work
            log_failure(self.logger, "update.record_not_written", exc, expected=True)
            return False

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
