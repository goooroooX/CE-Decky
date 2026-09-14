from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha1, sha256
from pathlib import Path, PurePosixPath
from typing import TypeVar
import asyncio
import math
import os
import re
import stat
import tempfile
import threading
import time
import uuid

from .activity_log import log_activity, log_failure
from .archive_import import ARGV_PASSWORD_FORMATS, argv_password_refusal
from .catalog import ArtifactRecord, CatalogService, PROVIDER_HOSTS, ProviderRateLimited
from .network import ArtifactGone, NetworkClient, NetworkError
from .operations import drain_through_cancellation, drained_to_thread
from .providers import MAX_ARTIFACT_BYTES as PROVIDER_MAX_ARTIFACT_BYTES, parse_retry_after
from .table_blocklist import CAUSE_ENCRYPTED, CAUSE_UNUSABLE, BlockedTableError
from .table_store import ArchiveWithoutTable, TableContentError, TableStore


_T = TypeVar("_T")

# Declared beside the provider contract, because the catalog has to refuse a
# row this would reject rather than offer it and find out at the transfer.
MAX_ARTIFACT_BYTES = PROVIDER_MAX_ARTIFACT_BYTES
MAX_DOWNLOAD_ENTRIES = 4096
ACQUISITION_TTL_SECONDS = 30 * 60

# What a provider's rate limit costs one download a user is waiting for.
#
# A 429 is a wait, not a refusal, and it was reported as a failed download: on
# the target the same artifact refused at 20:03:07 and downloaded normally at
# 20:03:35, because the whole provider was throttled for a few seconds while a
# background listing crawl and this download asked for the same budget. The
# user's own answer to that was to press the row again, which is this, only
# slower and only if they guessed that pressing again was worth trying.
#
# The waits are short because the evidence says the throttle is short: the same
# provider served a different artifact 15 s after the refusal. A provider that
# names its own `Retry-After` is believed instead, up to the cap, because it
# knows what it is enforcing and this does not.
DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS = (5, 15)
MAX_DOWNLOAD_RATE_LIMIT_WAIT_SECONDS = 120

# CE Decky will not put an archive password on another program's argv, so an
# encrypted member of an archive only 7-Zip opens can never be imported however
# the user answers. Both the import attempt and the inspection that precedes it
# say exactly this. `.rar` is in the same position as `.7z` and for the same
# reason: it is read through that same 7-Zip.
UNSUPPORTED_ENCRYPTED_7Z_MESSAGE = (
    "CE Decky does not open a password-protected archive that only 7-Zip reads, .7z or .rar: "
    "the password would have to be passed to another program on its command line. Re-pack the "
    "table as a zip, or use Local file to open the .CT directly."
)


def _optional_app_id(value: object) -> int | None:
    """The game a download was started for, or nothing.

    Validated here rather than trusted, because it arrives over the same RPC
    boundary as everything else and its only use is a display field in a durable
    record. A value that is not an AppID is dropped: a download must never fail
    over the game name a later refusal would have carried.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _member_unsupported(member: object) -> bool:
    """Whether no answer from the user can ever make this member importable."""
    return (
        isinstance(member, dict)
        and member.get("encrypted") is True
        and member.get("format") in ARGV_PASSWORD_FORMATS
    )


@dataclass
class Acquisition:
    acquisition_id: str
    record: ArtifactRecord
    state: str
    created: float
    updated: float
    staged_path: Path | None = None
    error: str | None = None
    bytes_received: int = 0
    inspection: dict[str, object] | None = None
    imported: dict[str, object] | None = None
    source_filename: str | None = None
    # The game this download was started for. Carried and never acted on: it
    # decides nothing about the transfer, and exists so a record written when
    # these bytes turn out not to be a table, or when the source no longer has
    # the file, can say what the user was looking for.
    app_id: int | None = None
    before_downloads: dict[str, tuple[int, int]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    provider_wait_until: float | None = None
    # Why this acquisition is waiting: `preparing` is the provider's own
    # countdown before it will hand over a link, `rate_limited` is the provider
    # refusing for now and being given time. The screen has to say which,
    # because they are different things to be told and only one of them is
    # something the provider promised.
    provider_wait_reason: str | None = None
    artifact_rejected: bool = False
    resolved_table_sha256: str | None = None
    failure_cause: str | None = None
    mutation_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # How many callers are inside a mutation of this acquisition right now.
    # Expiry is a background sweep over the same items, and holding the item
    # lock is not visible to it: an import that crossed the TTL boundary had its
    # own staged file unlinked underneath it by a concurrent poll.
    active_mutations: int = 0

    def public(self) -> dict[str, object]:
        with self.mutation_lock:
            return {
                "acquisition_id": self.acquisition_id,
                "provider": self.record.result.provider,
                "artifact_id": self.record.result.artifact_id,
                "filename": self.source_filename or self.record.result.filename,
                "state": self.state,
                "error": self.error,
                "artifact_rejected": self.artifact_rejected,
                "resolved_table_sha256": self.resolved_table_sha256,
                "failure_cause": self.failure_cause,
                "bytes_received": self.bytes_received,
                "expected_bytes": self.record.result.size_bytes,
                "provider_wait_seconds": max(0, math.ceil(self.provider_wait_until - time.time())) if self.provider_wait_until is not None else None,
                "provider_wait_reason": self.provider_wait_reason,
                "source_page": self.record.result.source_page if self.state == "browser_handoff" or self.record.result.download_mode != "direct_https" else None,
                "inspection": self.inspection,
                "imported": self.imported,
                "execution_consent": False if self.imported is not None else None,
            }


class AcquisitionManager:
    def __init__(self, catalog: CatalogService, network: NetworkClient, table_store: TableStore, staging_root: Path, downloads_root: Path, user_home: Path, sevenzip, logger=None) -> None:
        self.catalog = catalog
        self.network = network
        self.table_store = table_store
        self.staging_root = staging_root / "provider-downloads"
        self.downloads_root = downloads_root
        self.user_home = user_home
        self.sevenzip = sevenzip
        self.logger = logger
        # How a row whose file the source no longer has is written down. A
        # callable because the record belongs to the service that owns every
        # durable store, and because an acquisition built without one still has
        # to end the same way.
        self.record_missing: Callable[[str, str, str, int | None, str | None], None] | None = None
        self.record_resolution: Callable[[str, str, str, str], None] | None = None
        self.items: dict[str, Acquisition] = {}
        # One lock per provider that declares it serves one download at a time.
        self._provider_locks: dict[str, asyncio.Lock] = {}
        self._closing = False
        # Set while plugin data is being deleted; see `suspend()`.
        self._suspended = False

    def has_active(self) -> bool:
        """Whether any acquisition still owns a staged file or a live worker.

        Ownership is the staged file, the worker, and a browser handoff that has
        not been resolved yet - never the state name. `TERMINAL_STATES` is
        terminal for the *download*, and it includes `ready_to_import` and
        `needs_selection` - a finished download waiting for the user to pick a
        member or supply a password, which still holds its bytes in staging.
        Reading that set as "owns nothing" let deletion remove the file out from
        under an import the user was still answering.

        A `browser_handoff` owns nothing on disk *yet* and so has neither: its
        staging is created by `complete()` through `stable_snapshot()`, against
        the download snapshot it took when it started. Deleting staging while
        one is outstanding is deleting into the middle of that transaction.
        """
        return any(
            (item.task is not None and not item.task.done())
            or item.staged_path is not None
            or item.state == "browser_handoff"
            for item in self.items.values()
        )

    def suspend(self) -> None:
        """Refuse to start new acquisitions until resumed.

        Starting one does not pass through the service mutation boundary, so a
        deletion that only checked once could have staging created underneath it
        immediately afterwards.
        """
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False

    async def close(self) -> None:
        self._closing = True
        tasks = [item.task for item in self.items.values() if item.task and not item.task.done()]
        # Unconditional, unlike the completion record below it used to be. This
        # owner closes the shared HTTP client even when it holds no download of
        # its own, and that await was the last thing on the unload path with no
        # record of its own at all.
        log_activity(self.logger, "info", "acquisition.close_started", active=len(tasks), retained=len(self.items))
        for task in tasks:
            task.cancel()
        cancelled = False
        try:
            if tasks:
                # A download task unlinks its own staged file while handling
                # cancellation. Abandoning that wait would both orphan the
                # unlink and skip everything below it.
                cancelled = await drain_through_cancellation(
                    asyncio.gather(*tasks, return_exceptions=True),
                    label="acquisition.downloads", logger=self.logger, watching=tasks,
                )
            # Unload cancellation is not permission to orphan the native unlink
            # workers or skip another acquisition's staged file.
            staged = [item for item in self.items.values() if item.staged_path is not None]
            cleanup = asyncio.gather(*(
                drained_to_thread(_safe_unlink, item.staged_path) for item in staged
            ), return_exceptions=True)
            cancelled = await drain_through_cancellation(
                cleanup, label="acquisition.staged_cleanup", logger=self.logger,
            ) or cancelled
        finally:
            # Network ownership must be released even if an expired or staged
            # artifact is already inaccessible.
            closing_network = time.monotonic()
            await self.network.aclose()
            log_activity(
                self.logger, "info", "acquisition.close_completed",
                active_cancelled=len(tasks), retained=len(self.items),
                network_close_ms=int((time.monotonic() - closing_network) * 1000),
            )
        if cancelled:
            raise asyncio.CancelledError

    async def start(self, provider: str, artifact_id: str, search_id: str | None = None, app_id: int | None = None) -> dict[str, object]:
        self._expire()
        if self._closing:
            raise RuntimeError("plugin is unloading")
        if self._suspended:
            raise ValueError("plugin data is being deleted; start this download again when it finishes")
        record = (
            self.catalog.resolve(provider, artifact_id)
            if search_id is None else self.catalog.resolve(provider, artifact_id, search_id)
        )
        acquisition_id = uuid.uuid4().hex
        now = time.time()
        state = "downloading" if record.direct_url or record.acquisition else "browser_handoff"
        item = Acquisition(acquisition_id, record, state, now, now, app_id=_optional_app_id(app_id))
        if state == "browser_handoff":
            item.before_downloads = await asyncio.to_thread(snapshot_downloads, self.downloads_root)
            # close() and a deletion can both win while the filesystem snapshot
            # is in its worker. Do not publish an acquisition after the owner has
            # finished draining the item set it observed, and do not publish one
            # into a destructive transaction that has already checked what it is
            # about to remove - the guard above is on the wrong side of an await
            # for that.
            if self._closing:
                raise RuntimeError("plugin is unloading")
            if self._suspended:
                raise ValueError("plugin data is being deleted; start this download again when it finishes")
        else:
            item.task = asyncio.create_task(self._download_guarded(item))
        self.items[acquisition_id] = item
        log_activity(
            self.logger, "info", "acquisition.started",
            acquisition=acquisition_id[:12], provider=record.result.provider,
            artifact=record.result.artifact_id, route=state,
        )
        return item.public()

    def poll(self, acquisition_id: str) -> dict[str, object]:
        return self._get(acquisition_id).public()

    async def wait_ready(self, acquisition_id: str) -> None:
        item = self._get(acquisition_id)
        task = item.task
        if task is not None and not task.done():
            # asyncio.wait resolves without re-raising the download task's outcome.
            # A cancelled or failed acquisition must surface through its recorded
            # state and error, never as cancellation of the caller's RPC.
            await asyncio.wait({task})

    async def complete(self, acquisition_id: str, picked_path: str | None = None, member_path: str | None = None, password: str | None = None) -> dict[str, object]:
        await self.wait_ready(acquisition_id)
        return await drained_to_thread(self.complete_blocking, acquisition_id, picked_path, member_path, password)

    def complete_blocking(self, acquisition_id: str, picked_path: str | None = None, member_path: str | None = None, password: str | None = None) -> dict[str, object]:
        item = self._get(acquisition_id)
        with item.mutation_lock:
            # Expiry runs from any poll and deletes by age alone, so an import
            # that started just before the boundary and then hashed, extracted
            # and stored a large table across it could have its source removed
            # mid-operation. An acquisition being actively imported is not idle
            # state to reclaim, however long the import takes; the TTL resumes
            # from the moment it finishes. The item is re-checked under the lock
            # because a sweep can retire it between the lookup and this line.
            if self.items.get(acquisition_id) is not item:
                raise ValueError("acquisition is unknown or expired")
            item.active_mutations += 1
        try:
            return self._complete_guarded(item, acquisition_id, picked_path, member_path, password)
        finally:
            with item.mutation_lock:
                item.active_mutations -= 1
                item.updated = time.time()

    def _complete_guarded(self, item: Acquisition, acquisition_id: str, picked_path: str | None, member_path: str | None, password: str | None) -> dict[str, object]:
        with item.mutation_lock:
            log_activity(
                self.logger, "info", "acquisition.import_started",
                acquisition=acquisition_id[:12], provider=item.record.result.provider,
                picked_file=bool(picked_path), member_selected=bool(member_path), password_supplied=bool(password),
            )
            try:
                result = self._complete_locked(item, picked_path, member_path, password)
            except TableContentError as exc:
                # A damaged archive member is a terminal outcome for this exact
                # artifact, not an RPC fault: report the retired acquisition so
                # the caller can offer another table instead of a raw parser error.
                log_activity(
                    self.logger, "warning", "acquisition.artifact_rejected",
                    acquisition=acquisition_id[:12], provider=item.record.result.provider,
                    bytes=item.bytes_received, reason=str(exc)[:200],
                )
                return item.public()
            except Exception as exc:
                log_failure(
                    self.logger, "acquisition.import_failed", exc, expected=True,
                    acquisition=acquisition_id[:12], provider=item.record.result.provider,
                    state=item.state,
                )
                raise
            log_activity(
                self.logger, "info", "acquisition.import_completed",
                acquisition=acquisition_id[:12], provider=item.record.result.provider,
                state=result.get("state"), table_sha=str((item.imported or {}).get("sha256", "none"))[:12],
            )
            return result

    def _complete_locked(self, item: Acquisition, picked_path: str | None, member_path: str | None, password: str | None) -> dict[str, object]:
        browser_staged = False
        if item.state == "browser_handoff":
            if picked_path:
                selected = Path(picked_path).expanduser().absolute()
                if selected.is_symlink():
                    raise ValueError("selected browser download must not be a symlink")
                source = selected
            else:
                if item.record.result.download_mode == "file_picker":
                    raise ValueError("choose the downloaded table explicitly")
                source = detect_download(self.downloads_root, item.before_downloads, item.record)
            try:
                source.resolve(strict=True).relative_to(self.user_home.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise ValueError("selected browser download must remain under DECKY_USER_HOME") from exc
            item.staged_path = stable_snapshot(source, self.staging_root)
            item.source_filename = source.name
            item.inspection = self.table_store.inspect_source(str(item.staged_path), sevenzip=self.sevenzip())
            item.state = "ready_to_import"
            item.updated = time.time()
            browser_staged = True
        if browser_staged:
            return item.public()
        if item.state not in {"ready_to_import", "needs_selection"} or item.staged_path is None:
            raise ValueError(item.error or "acquisition is not ready for import")
        expected = item.record.advertised_sha256
        if expected and _sha256_file(item.staged_path) != expected:
            item.state = "failed"
            item.error = "downloaded artifact does not match the advertised SHA-256"
            raise ValueError(item.error)
        # Which table inside the artifact this is. For a direct `.CT` there is
        # no choice to record; for an archive the advertised digest identifies
        # the archive, and only a single-member one can stand for this table.
        inspection = item.inspection if isinstance(item.inspection, dict) else {}
        # A direct `.CT` is reported as a one-member "ct" inspection whose
        # member is the staging file, which is not provenance about anything.
        # Only a real archive can hold a table other than this one.
        members = list(inspection.get("members") or []) if inspection.get("format") not in (None, "ct") else []
        origin: dict[str, object] = {
            "provider": item.record.result.provider,
            "artifact_id": item.record.result.artifact_id,
            "topic_id": item.record.result.topic_id,
            "source_page": item.record.result.source_page,
            "original_filename": item.source_filename or item.record.result.filename,
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "advertised_sha256": item.record.advertised_sha256,
        }
        # The release the provider advertised for these exact bytes. Without it
        # an imported table is only a digest and a date, and nothing connects it
        # back to the revision that was chosen in search.
        if item.record.result.version:
            origin["version"] = item.record.result.version
        if members:
            origin["member_count"] = len(members)
            chosen = member_path or (members[0].get("path") if isinstance(members[0], dict) else None)
            if isinstance(chosen, str) and chosen:
                origin["member_path"] = chosen
        def _resolved(digest: str) -> None:
            item.resolved_table_sha256 = digest
            unambiguous = inspection.get("format") == "ct" or len(members) == 1
            if not unambiguous or self.record_resolution is None or not expected:
                return
            try:
                self.record_resolution(item.record.result.provider, item.record.result.artifact_id, expected, digest)
                log_activity(self.logger, "info", "acquisition.table_resolved",
                             provider=item.record.result.provider, artifact_id=item.record.result.artifact_id,
                             artifact_sha=expected[:12], table_sha=digest[:12])
            except (OSError, ValueError) as exc:
                log_activity(self.logger, "warning", "acquisition.resolution_not_recorded",
                             provider=item.record.result.provider, table_sha=digest[:12], reason=str(exc)[:200])

        def _import(with_password: str | None):
            return self.table_store.import_selection(
                str(item.staged_path), member_path=member_path, password=with_password,
                sevenzip=self.sevenzip(),
                source_filename=(item.source_filename or item.record.result.filename) if item.staged_path.suffix.casefold() == ".ct" else None,
                # A member extracted from this archive is validated and deleted
                # inside the import, so if it turns out not to be a table the
                # record is the only place its origin can come from - and a
                # provider almost never advertises the digest of a file inside
                # an archive, so without this the same post is offered again.
                origins=(f"{item.record.result.provider}:{item.record.result.artifact_id}",),
                # The game this download was started for, so a record written
                # because these bytes are not a table says what they were for.
                app_id=item.app_id,
            )

        try:
            try:
                artifact = _import(password)
            except ValueError as exc:
                # The provider published this archive's password in the public
                # text beside the attachment, and the parser already reads it.
                # It was then dropped, so a completed download dead-ended at an
                # empty password field the controller user had no way to fill.
                # Try the exact parsed hint once; it stays backend-owned and is
                # never shown, logged or persisted, and an explicit prompt is
                # still the fallback when it does not work.
                hint = item.record.password_hint
                if password is not None or not hint or "password" not in str(exc).casefold():
                    raise
                artifact = _import(hint)
        except TableContentError as exc:
            # The artifact arrived intact and is still unusable. Retire the
            # acquisition so the modal offers a way out instead of repeating an
            # import that can never succeed for these exact bytes.
            item.state = "failed"
            item.error = _damaged_table_message(exc)
            item.artifact_rejected = True
            item.updated = time.time()
            raise
        except BlockedTableError as exc:
            item.state = "failed"
            item.error = str(exc)
            item.failure_cause = exc.entry.cause
            item.artifact_rejected = exc.entry.cause == CAUSE_UNUSABLE
            if exc.entry.sha256 is not None and not item.artifact_rejected:
                _resolved(exc.entry.sha256)
            item.updated = time.time()
            return item.public()
        except ValueError as exc:
            message = str(exc)
            if any(argv_password_refusal(kind) in message for kind in ARGV_PASSWORD_FORMATS):
                # No password the user can type will ever satisfy this, because
                # CE Decky deliberately will not put one on another program's
                # argv. Leaving it selectable kept offering that impossible
                # action instead of saying the format is unsupported.
                item.state = "failed"
                item.error = UNSUPPORTED_ENCRYPTED_7Z_MESSAGE
                item.updated = time.time()
                return item.public()
            if "multiple .CT" in message or "choose one archive member" in message or "password" in message.casefold():
                item.state = "needs_selection"
                item.error = message
                item.updated = time.time()
                return item.public()
            raise
        _resolved(artifact.sha256)
        # Provenance is metadata about a table that is already downloaded,
        # validated and stored under its verified digest. Whatever the provider
        # described it with, failing to record that description must never lose
        # the table: the import is what the user asked for.
        try:
            self.table_store.add_origin(artifact.sha256, origin)
        except (OSError, ValueError, TypeError) as exc:
            log_activity(
                self.logger, "warning", "acquisition.origin_not_recorded",
                provider=item.record.result.provider, table_sha=artifact.sha256[:12],
                reason=str(exc)[:200],
            )
            if self.catalog.diagnostics is not None:
                try:
                    self.catalog.diagnostics.record_parse_issues(item.record.result.provider, degraded=1)
                except ValueError:
                    pass
        item.imported = self.table_store.get_table(artifact.sha256)
        item.state = "imported"
        item.error = None
        item.updated = time.time()
        if item.staged_path:
            _safe_unlink(item.staged_path)
            item.staged_path = None
        return item.public()

    async def cancel(self, acquisition_id: str) -> dict[str, object]:
        item = self._get(acquisition_id)
        if item.task and not item.task.done():
            item.task.cancel()
            await asyncio.gather(item.task, return_exceptions=True)
        result = await drained_to_thread(self._cancel_locked, item)
        log_activity(
            self.logger, "info", "acquisition.cancelled",
            acquisition=acquisition_id[:12], provider=item.record.result.provider,
            state=result.get("state"),
        )
        return result

    @staticmethod
    def _cancel_locked(item: Acquisition) -> dict[str, object]:
        with item.mutation_lock:
            # A late detached-modal cleanup must not rewrite a completed import
            # as cancelled. If cancellation wins first, complete_locked sees the
            # cancelled state and cannot read a staging file removed here.
            if item.state in {"imported", "cancelled"}:
                return item.public()
            if item.staged_path:
                _safe_unlink(item.staged_path)
                item.staged_path = None
            if item.state == "failed":
                # Preserve the useful terminal diagnostic while making Close
                # idempotently retire staging retained by a completion-time
                # validation failure.
                item.updated = time.time()
                return item.public()
            item.state = "cancelled"
            item.updated = time.time()
            return item.public()

    TERMINAL_STATES = frozenset({"ready_to_import", "needs_selection", "imported", "failed", "cancelled"})

    async def _download_guarded(self, item: Acquisition) -> None:
        """Guarantee that the worker never exits leaving a nonterminal state.

        Everything inside `_download()` reports its own outcome, but its
        bookkeeping - staging cleanup, download snapshots, diagnostics - can
        itself raise, and the item was then left `downloading` forever with no
        task behind it: the panel polled a row nothing would ever advance.
        """
        try:
            await self._download(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the state machine must always terminate
            log_failure(
                self.logger, "acquisition.worker_failed", exc, expected=False,
                acquisition=item.acquisition_id[:12], provider=item.record.result.provider,
            )
            if item.state not in self.TERMINAL_STATES:
                item.error = f"the download could not be completed: {str(exc)[:512]}"
                item.state = "failed"
                item.updated = time.time()
        finally:
            if item.state not in self.TERMINAL_STATES:
                item.error = item.error or "the download stopped without reporting an outcome"
                item.state = "failed"
                item.updated = time.time()

    async def _with_rate_limit_waits(
        self,
        item: Acquisition,
        phase: str,
        operation: Callable[[], Awaitable[_T]],
    ) -> _T:
        """Run one acquisition step, waiting out a provider rate limit rather than failing on it.

        The wait is the state this screen already has for a provider that makes
        a user wait: the row shows a countdown and Cancel keeps working, which
        is what makes a wait honest rather than a stall. A download attempt
        writes to the same staging path each time, and the transport removes
        what it wrote before it raises, so a retry never appends to a partial
        file.

        Only a rate limit the provider can simply be asked past again is waited
        out. Playground's link handshake issues its countdown token once, so a
        limit that lands after the token has been spent ends the attempt instead
        of replaying an exchange the provider has already closed.

        This deliberately does not consult the provider cooldown the background
        listing crawl keeps. That cooldown is a default of a minute when the
        provider names no number, and on the target the throttle it was set for
        had already cleared after fifteen seconds: honoring it here would have
        made the user wait longer than the provider ever asked them to.
        """
        attempt = 0
        while True:
            try:
                return await operation()
            except ProviderRateLimited as exc:
                if attempt >= len(DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS) or not exc.restartable:
                    raise
                named = parse_retry_after(exc.retry_after)
                # A provider that answers `Retry-After: 0`, or with a moment
                # that has already passed, is asking to be tried again at once.
                # Doing that is how one rate limit becomes several, so the floor
                # is a second: this is the only place that decides how hard a
                # provider that just refused gets asked again.
                wait = min(
                    max(1, named) if named is not None else DOWNLOAD_RATE_LIMIT_BACKOFF_SECONDS[attempt],
                    MAX_DOWNLOAD_RATE_LIMIT_WAIT_SECONDS,
                )
                attempt += 1
                item.state = "waiting_provider"
                item.provider_wait_until = time.time() + wait
                item.provider_wait_reason = "rate_limited"
                item.updated = time.time()
                log_activity(
                    self.logger, "info", "acquisition.rate_limited",
                    acquisition=item.acquisition_id[:12], provider=item.record.result.provider,
                    phase=phase, attempt=attempt, wait_seconds=wait, retry_after=exc.retry_after or "none",
                )
                # A throttle the download recovered from is invisible in
                # diagnostics otherwise: the terminal-failure path is the only
                # one that records anything, so a provider that makes every
                # transfer wait looks like one that simply serves files.
                if self.catalog.diagnostics is not None:
                    try:
                        self.catalog.diagnostics.record_throttle(item.record.result.provider, wait_seconds=wait)
                    except ValueError:
                        pass
                await asyncio.sleep(wait)
                item.state = "downloading"
                item.provider_wait_until = None
                item.provider_wait_reason = None
                item.updated = time.time()

    async def _download(self, item: Acquisition) -> None:
        """Run one acquisition, holding a provider's own concurrency rule.

        One provider serves a guest exactly one download at a time and refuses
        the second with its own error code, so a second acquisition of that
        provider waits for the first rather than being sent to be refused. The
        rule is declared by the provider's own request, and the wait is a
        visible provider wait rather than a download that appears stalled.
        """
        request = item.record.acquisition
        if not getattr(request, "exclusive", False):
            await self._download_once(item)
            return
        provider = item.record.result.provider
        lock = self._provider_locks.setdefault(provider, asyncio.Lock())
        if lock.locked():
            item.state = "waiting_provider"
            item.provider_wait_until = None
            item.provider_wait_reason = "preparing"
            item.updated = time.time()
            log_activity(
                self.logger, "info", "acquisition.provider_serialized",
                acquisition=item.acquisition_id[:12], provider=provider,
            )
        try:
            async with lock:
                await self._download_once(item)
        except asyncio.CancelledError:
            # `_download_once` records its own cancellation, so this only fires
            # for an acquisition cancelled while it was still queued behind
            # another. Left to the guard below it became "failed: the download
            # stopped without reporting an outcome", which is a spurious failure
            # reported to someone who pressed Cancel.
            if item.state not in self.TERMINAL_STATES:
                item.state = "cancelled"
                item.provider_wait_until = None
                item.provider_wait_reason = None
                item.updated = time.time()
                log_activity(
                    self.logger, "info", "acquisition.download_cancelled",
                    acquisition=item.acquisition_id[:12], provider=provider, phase="queued",
                )
            raise

    async def _download_once(self, item: Acquisition) -> None:
        _ensure_staging_directory(self.staging_root)
        suffix = PurePosixPath(item.record.result.filename).suffix
        destination = self.staging_root / f".provider-{uuid.uuid4().hex}{suffix}"
        item.staged_path = destination
        phase = "resolve"
        try:
            if item.record.direct_url is not None:
                url = item.record.direct_url
                allowed_hosts = PROVIDER_HOSTS[item.record.result.provider]
            else:
                def on_countdown(seconds: int, reason: str = "preparing") -> None:
                    """A wait the provider's own resolver is serving out.

                    The reason matters because these are different things to be
                    told: a countdown the provider always makes a guest sit
                    through, and the provider refusing right now. A resolver
                    that keeps a rate limit inside itself, because restarting
                    its chain would spend a second handshake, is the only place
                    that limit is visible, so it reports the kind and the
                    throttle is counted here exactly as the generic retry
                    counts its own.
                    """
                    reason = reason if reason in {"preparing", "rate_limited", "busy"} else "preparing"
                    item.state = "waiting_provider"
                    item.provider_wait_until = time.time() + seconds
                    item.provider_wait_reason = reason
                    item.updated = time.time()
                    log_activity(
                        self.logger, "info", "acquisition.provider_wait",
                        acquisition=item.acquisition_id[:12], provider=item.record.result.provider,
                        wait_seconds=seconds, reason=reason,
                    )
                    if reason == "rate_limited" and self.catalog.diagnostics is not None:
                        try:
                            self.catalog.diagnostics.record_throttle(
                                item.record.result.provider, wait_seconds=seconds,
                            )
                        except ValueError:
                            pass

                url, allowed_hosts = await self._with_rate_limit_waits(
                    item, "resolve",
                    lambda: self.catalog.resolve_download(item.record, on_countdown=on_countdown),
                )
                item.state = "downloading"
                item.provider_wait_until = None
                item.provider_wait_reason = None
                item.updated = time.time()
            phase = "download"
            request = item.record.acquisition
            source_referer = item.record.result.source_page
            if request is not None and not getattr(request, "send_source_referer", True):
                source_referer = None
            await self._with_rate_limit_waits(item, "download", lambda: self.network.download(
                url, destination, allowed_hosts=allowed_hosts,
                max_bytes=MAX_ARTIFACT_BYTES, referer=source_referer,
            ))
            phase = "inspect"
            prefix, bytes_received, inspection, actual_sha = await drained_to_thread(
                _inspect_download, destination, self.table_store, self.sevenzip,
                bool(item.record.advertised_sha256),
            )
            if prefix.startswith((b"<!doctype html", b"<html")):
                # An automatic row that turns into a challenge page is a
                # provider that is unavailable right now, not a workflow the
                # user can continue: browser use is not a controller workflow,
                # and moving an in-flight acquisition into a state with no
                # forward action simply stranded it until it was cancelled.
                await drained_to_thread(_safe_unlink, destination)
                item.staged_path = None
                self._fail_provider_unavailable(item, "the provider answered with a web page instead of the file")
                return
            item.bytes_received = bytes_received
            if item.record.size_exact and item.record.result.size_bytes is not None and item.bytes_received != item.record.result.size_bytes:
                raise NetworkError("downloaded artifact size does not match provider metadata")
            if item.record.advertised_sha256 and actual_sha != item.record.advertised_sha256:
                raise NetworkError("downloaded artifact does not match the advertised SHA-256")
            if item.record.blob_sha1:
                # The route that has no content digest to offer still has an
                # exact identity, so it is checked rather than trusted: a table
                # committed in a git tree is named by git's own object hash.
                observed_blob = await drained_to_thread(_git_blob_sha1_file, destination)
                if observed_blob != item.record.blob_sha1:
                    raise NetworkError("downloaded artifact does not match the advertised git object name")
            if inspection is None:
                raise NetworkError("downloaded artifact could not be inspected")
            item.inspection = inspection
            if self.catalog.diagnostics is not None:
                try:
                    self.catalog.diagnostics.record_download(item.record.result.provider, bytes_downloaded=item.bytes_received)
                except ValueError:
                    pass
            members = item.inspection.get("members", []) if item.inspection else []
            members = members if isinstance(members, list) else []
            needs_input = bool(
                len(members) > 1
                or any(isinstance(member, dict) and member.get("encrypted") is True for member in members)
            )
            if members and all(_member_unsupported(member) for member in members):
                # Every candidate is an encrypted 7z, so there is no selection
                # the user could make that leads anywhere. Left as
                # `needs_selection` this dead-ended with Cancel as the only
                # valid action, because the import that would have made the
                # outcome terminal was unreachable behind a disabled button.
                # A mixed archive stays selectable: its supported members work.
                item.state = "failed"
                item.error = UNSUPPORTED_ENCRYPTED_7Z_MESSAGE
                # These exact bytes can never yield a table, so the catalog must
                # stop offering them: another attempt costs a second provider
                # countdown and download for the same terminal answer.
                item.artifact_rejected = True
                # Durably, not only for this session. It was the one terminal
                # outcome that wrote nothing down, so the row came back on the
                # next search, was paid for again and ended here again, and it
                # was also the one mark in the search list with no entry behind
                # it in the list the user reads and clears. It is recorded as
                # what it is rather than as bytes that are not a table: this
                # archive may well hold a good one, and re-packing it as a zip
                # is something the user can actually do.
                await drained_to_thread(
                    _note_unopenable_archive, self.table_store, destination, item, actual_sha,
                )
            else:
                item.state = "needs_selection" if needs_input else "ready_to_import"
            item.updated = time.time()
            log_activity(
                self.logger, "info", "acquisition.download_ready",
                acquisition=item.acquisition_id[:12], provider=item.record.result.provider,
                bytes=item.bytes_received, expected_bytes=item.record.result.size_bytes,
                size_exact=item.record.size_exact, sha_expected=bool(item.record.advertised_sha256),
                format=item.inspection.get("format") if item.inspection else "unknown",
                members=len(members), state=item.state,
            )
        except asyncio.CancelledError:
            await drained_to_thread(_safe_unlink, destination)
            item.staged_path = None
            item.state = "cancelled"
            item.provider_wait_until = None
            item.provider_wait_reason = None
            item.updated = time.time()
            log_activity(
                self.logger, "info", "acquisition.download_cancelled",
                acquisition=item.acquisition_id[:12], provider=item.record.result.provider, phase=phase,
            )
            raise
        except TableContentError as exc:
            # A damaged table is an artifact defect, never provider unavailability:
            # recording it as a provider failure would blame and cool down a source
            # that delivered exactly the bytes it advertised.
            item.bytes_received = await asyncio.to_thread(_staged_size, destination)
            # These exact bytes are not a table, and this route deletes them
            # without ever reaching the import that records that. Take the
            # content identity while the file still exists, so the same download
            # is not paid for again on the next search.
            await drained_to_thread(_note_unusable_download, self.table_store, destination, exc, item)
            await drained_to_thread(_safe_unlink, destination)
            item.staged_path = None
            item.provider_wait_until = None
            item.provider_wait_reason = None
            item.error = _damaged_table_message(exc)
            item.state = "failed"
            item.artifact_rejected = True
            item.updated = time.time()
            log_activity(
                self.logger, "warning", "acquisition.artifact_rejected",
                acquisition=item.acquisition_id[:12], provider=item.record.result.provider,
                bytes=item.bytes_received, reason=str(exc)[:200],
            )
        except Exception as exc:
            await drained_to_thread(_safe_unlink, destination)
            item.staged_path = None
            item.provider_wait_until = None
            item.provider_wait_reason = None
            if isinstance(exc, NetworkError) and "HTTP 403" in str(exc):
                self._fail_provider_unavailable(
                    item, "the provider refused the download (HTTP 403)", http_status=403,
                )
                return
            if isinstance(exc, ArtifactGone):
                # The provider's own answer about this exact file, in the user's
                # terms and naming the source that gave it. The row is retired
                # with it: asking again reads the same however often it is done,
                # and on a provider that makes a guest sit through a countdown
                # first it costs that countdown to be told so.
                item.error = (
                    f"{item.record.result.provider_display_name} no longer has this file. "
                    "The page that offers it is still there; what it points at is gone."
                )
                item.state = "failed"
                item.artifact_rejected = True
                item.updated = time.time()
                log_activity(
                    self.logger, "warning", "acquisition.artifact_gone",
                    acquisition=item.acquisition_id[:12], provider=item.record.result.provider,
                    phase=phase, reason=(exc.detail or str(exc))[:200],
                )
                # Durably, not only for this session. Nothing was downloaded so
                # there is no content to key on, and the provider row is the
                # whole of the identity: without it the row is offered, waited
                # for and paid for again on the next search.
                #
                # Off the loop, like every other durable write on this path: it
                # takes the service's mutation lock and writes a file, and a
                # worker thread already holding that lock would otherwise stall
                # every poll and every other call for as long as it held it.
                # Bookkeeping on an outcome that is already decided, so it can
                # only be reported, never allowed to replace it.
                if self.record_missing is not None:
                    try:
                        await drained_to_thread(
                            self.record_missing,
                            item.record.result.provider, item.record.result.artifact_id, item.error,
                            item.app_id, item.source_filename or item.record.result.filename,
                        )
                    except Exception as write_failure:  # noqa: BLE001 - never replaces the outcome
                        log_failure(
                            self.logger, "acquisition.artifact_gone_not_recorded", write_failure,
                            expected=True, acquisition=item.acquisition_id[:12],
                            provider=item.record.result.provider,
                        )
                self._record_download_failure(item, exc)
                return
            item.error = str(exc)[:2048]
            item.state = "failed"
            item.updated = time.time()
            log_failure(
                self.logger, "acquisition.download_failed", exc, expected=True,
                acquisition=item.acquisition_id[:12], provider=item.record.result.provider,
                phase=phase, bytes=item.bytes_received,
            )
            self._record_download_failure(item, exc)

    def _record_download_failure(self, item: Acquisition, exc: BaseException) -> None:
        """Count this failed download against the source that answered it.

        Shared, because every way a download can end badly has to reach the one
        screen that says what each source is doing. A provider whose entries go
        missing is otherwise indistinguishable from one that simply serves.
        """
        if self.catalog.diagnostics is None:
            return
        try:
            status_match = re.search(r"\bHTTP ([1-5][0-9]{2})\b", item.error or "")
            http_status = int(status_match.group(1)) if status_match else None
            retry_after = exc.retry_after if isinstance(exc, ProviderRateLimited) else None
            self.catalog.diagnostics.record_failure(
                item.record.result.provider,
                error=item.error,
                http_status=http_status,
                retry_after=retry_after,
                download=True,
            )
        except ValueError:
            pass

    def _fail_provider_unavailable(
        self, item: Acquisition, reason: str, *, http_status: int | None = None
    ) -> None:
        """End an in-flight acquisition as a bounded provider-unavailable outcome.

        This is a failed download, so it is recorded as one. It used to return
        before the generic failure bookkeeping: `downloads_failed` never moved,
        the provider's own last error never described it, and the support bundle
        said the provider had been unavailable without saying why, which is the
        one question a released bug report has to answer from the archive.

        The deadline is preserved rather than written, because a failure that
        names none writes a cleared one, and this one carries no statement about
        when the provider will serve again.
        """
        provider = item.record.result.provider
        item.provider_wait_until = None
        item.provider_wait_reason = None
        item.error = (
            f"{reason}. Use Local file to open a table you downloaded yourself, or try another provider."
        )[:2048]
        item.state = "failed"
        item.updated = time.time()
        log_activity(
            self.logger, "info", "acquisition.provider_unavailable",
            acquisition=item.acquisition_id[:12], provider=provider,
            reason=reason[:512], http_status=http_status if http_status is not None else "none",
        )
        if self.catalog.diagnostics is not None:
            try:
                self.catalog.diagnostics.record_failure(
                    provider, error=reason[:2048], http_status=http_status,
                    download=True, preserve_cooldown=True,
                )
            except ValueError:
                pass

    def _get(self, acquisition_id: str) -> Acquisition:
        self._expire()
        if not isinstance(acquisition_id, str) or len(acquisition_id) != 32:
            raise ValueError("acquisition ID is invalid")
        try:
            return self.items[acquisition_id]
        except KeyError as exc:
            raise ValueError("acquisition is unknown or expired") from exc

    def _expire(self) -> None:
        now = time.time()
        expired = 0
        for key, item in list(self.items.items()):
            # Expiry has to hold the same lock every mutation holds, not merely
            # look at a counter beside it. Reading the counter without the lock
            # left the window between an importer proving the acquisition is
            # still there and its taking the lease: expiry could delete the item
            # and unlink its staging in that gap, and the import then read a
            # file that was no longer there. A lock somebody else holds means
            # the acquisition is in use, whatever the counter says.
            if not item.mutation_lock.acquire(blocking=False):
                continue
            try:
                if item.active_mutations > 0:
                    continue
                if now - item.updated <= ACQUISITION_TTL_SECONDS:
                    continue
                if item.task is not None and not item.task.done():
                    continue
                if self.items.get(key) is not item:
                    continue
                if item.staged_path:
                    _safe_unlink(item.staged_path)
                del self.items[key]
                expired += 1
            finally:
                item.mutation_lock.release()
        if expired:
            log_activity(self.logger, "info", "acquisition.expired", count=expired)


def _staged_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _note_unopenable_archive(table_store: TableStore, destination: Path, item, digest: str | None) -> None:
    """Best effort, on an outcome that is already terminal and already reported.

    The digest is taken from the download where the advertised one made it
    necessary and computed here otherwise, because these bytes are identified
    by nothing else: the archive is never extracted, so nothing downstream ever
    sees them again.
    """
    try:
        result = item.record.result
        table_store.note_unusable_bytes(
            digest or _sha256_file(destination),
            UNSUPPORTED_ENCRYPTED_7Z_MESSAGE,
            item.source_filename or result.filename,
            (f"{result.provider}:{result.artifact_id}",),
            item.app_id,
            CAUSE_ENCRYPTED,
        )
    except (OSError, ValueError):
        pass


def _note_unusable_download(table_store: TableStore, destination: Path, exc: TableContentError, item) -> None:
    """Best effort, on a path that is already failing for a reason of its own."""
    try:
        result = item.record.result
        table_store.note_unusable_bytes(
            _sha256_file(destination),
            _damaged_table_message(exc),
            item.source_filename or result.filename,
            (f"{result.provider}:{result.artifact_id}",),
            item.app_id,
        )
    except (OSError, ValueError):
        pass


def _damaged_table_message(exc: TableContentError) -> str:
    # An archive that holds no table is not damaged, it is the wrong file: the
    # download and the archive were both fine and there is simply no `.CT` in
    # it. Saying "damaged" there sends the user looking for a broken upload.
    # Asked of the type rather than of the sentence, so rewording the archive
    # layer's message cannot silently put this back the way it was.
    if isinstance(exc, ArchiveWithoutTable):
        return (
            "This download opened correctly and holds no Cheat Engine table. "
            "It is not offered again; choose a different table."
        )
    return f"{exc}. The file is damaged at its source; choose a different table."[:2048]


def _safe_unlink(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        # Staging cleanup is best effort; no failed unlink may strand network
        # resources or turn expiry into a process-wide RPC failure.
        return


def _inspect_download(destination: Path, table_store: TableStore, sevenzip, digest_required: bool) -> tuple[bytes, int, dict[str, object] | None, str | None]:
    with destination.open("rb") as handle:
        prefix = handle.read(512).lstrip().lower()
    size = destination.stat().st_size
    if prefix.startswith((b"<!doctype html", b"<html")):
        return prefix, size, None, None
    digest = _sha256_file(destination) if digest_required else None
    inspection = table_store.inspect_source(str(destination), sevenzip=sevenzip())
    return prefix, size, inspection, digest


def snapshot_downloads(root: Path) -> dict[str, tuple[int, int]]:
    if root.is_symlink():
        raise ValueError("Downloads directory must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    rows: dict[str, tuple[int, int]] = {}
    with os.scandir(root) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > MAX_DOWNLOAD_ENTRIES:
                raise ValueError("Downloads directory exceeds the entry limit")
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                rows[entry.name] = (info.st_size, info.st_mtime_ns)
    return rows


def detect_download(root: Path, before: dict[str, tuple[int, int]], record: ArtifactRecord) -> Path:
    first = snapshot_downloads(root)
    matches: list[Path] = []
    expected_suffix = PurePosixPath(record.result.filename).suffix.casefold()
    for name, identity in first.items():
        if before.get(name) == identity or PurePosixPath(name).suffix.casefold() != expected_suffix:
            continue
        expected_name = record.result.filename.casefold()
        actual_name = name.casefold()
        expected_path = PurePosixPath(expected_name)
        duplicate_pattern = f"{expected_path.stem} ("
        if actual_name != expected_name and not (
            actual_name.startswith(duplicate_pattern)
            and actual_name.endswith(")" + expected_path.suffix)
            and actual_name[len(duplicate_pattern): -(len(expected_path.suffix) + 1)].isdigit()
        ):
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        if record.result.size_bytes is not None and identity[0] != record.result.size_bytes:
            continue
        matches.append(path)
    second = snapshot_downloads(root)
    matches = [path for path in matches if second.get(path.name) == first.get(path.name)]
    if len(matches) != 1:
        raise ValueError("Downloads detection is ambiguous; choose the downloaded file explicitly")
    return matches[0]


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob_sha1_file(path: Path) -> str:
    """Reproduce git's object name for the staged bytes.

    A table committed in a repository tree advertises this rather than a digest
    of its content, and git defines it exactly: the SHA-1 of the header
    `blob <length>\\0` followed by the bytes. Streaming it keeps the check
    within the same bounded memory as the SHA-256 beside it.
    """
    digest = sha1()
    digest.update(b"blob %d\0" % path.stat().st_size)
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def stable_snapshot(source: Path, staging_root: Path) -> Path:
    if source.is_symlink():
        raise ValueError("selected browser download must be a regular file")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise ValueError("selected browser download must be a stable regular file") from exc
    _ensure_staging_directory(staging_root)
    fd, name = tempfile.mkstemp(prefix=".browser-", suffix=source.suffix, dir=staging_root)
    destination = Path(name)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1:
            raise ValueError("selected browser download must be a non-empty regular file")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("selected browser download exceeds the byte limit")
        total = 0
        with os.fdopen(source_fd, "rb") as src, os.fdopen(fd, "wb") as dst:
            source_fd = -1
            fd = -1
            while chunk := src.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_ARTIFACT_BYTES:
                    raise ValueError("selected browser download exceeds the byte limit")
                dst.write(chunk)
            after = os.fstat(src.fileno())
            dst.flush()
            os.fsync(dst.fileno())
        try:
            path_after = os.stat(source, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("selected browser download changed while being staged") from exc
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        path_identity = (path_after.st_dev, path_after.st_ino)
        if (
            identity_before != identity_after
            or path_identity != (before.st_dev, before.st_ino)
            or total != before.st_size
        ):
            raise ValueError("selected browser download changed while being staged")
        return destination
    except Exception:
        if source_fd >= 0:
            os.close(source_fd)
        if fd >= 0:
            os.close(fd)
        destination.unlink(missing_ok=True)
        raise


def _ensure_staging_directory(path: Path) -> None:
    """Create/revalidate a managed staging directory without accepting links."""
    absolute = path.expanduser().absolute()
    parent = absolute.parent
    if parent.is_symlink():
        raise ValueError("provider staging parent must not be a symlink")
    if parent.exists() and not parent.is_dir():
        raise ValueError("provider staging parent is not a directory")
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or parent.resolve(strict=True) != parent:
        raise ValueError("provider staging parent resolves through a symlink")
    if absolute.is_symlink():
        raise ValueError("provider staging directory must not be a symlink")
    if absolute.exists() and not absolute.is_dir():
        raise ValueError("provider staging path is not a directory")
    if not absolute.exists():
        absolute.mkdir(exist_ok=False)
    if absolute.is_symlink() or absolute.resolve(strict=True) != absolute:
        raise ValueError("provider staging directory resolves through a symlink")
