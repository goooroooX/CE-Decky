from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

from .activity_log import log_activity, log_failure, safe_log_text

T = TypeVar("T")


def drainable_tasks(tasks: Sequence["asyncio.Task | None"]) -> list["asyncio.Task"]:
    """The tasks the running loop can actually wait on, out of the ones held.

    On the device there is one loop for the whole plugin process and this filter
    removes nothing: `docs/FIELD_NOTES.md` records Decky's own `run_forever` and
    the reading that confirms it, and a scheduler started during load is still
    pending on that loop at unload.

    It is here for every other caller. A test, a developer helper or a probe may
    construct an owner, run something on one loop and close it from another, and
    a task belonging to a loop that is closed, or merely to a different one,
    cannot be made to run again by anything the closing coroutine does. Handing
    one to `asyncio.gather` asks that other loop to schedule a callback, which
    on Python 3.11 - the version the authoritative CI gate runs and the version
    Decky ships on the device - is `RuntimeError: Event loop is closed` raised
    out of `close()`. Python 3.13 takes a shortcut for a future that is already
    done and hides it, which is why this is a rule rather than something a local
    run discovers.

    So a drain list is what this loop started and can still stop. Everything
    else is already quiesced, and waiting on it is neither possible nor owed.
    """
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - close() is always awaited
        return []
    return [
        task for task in tasks
        if task is not None and not task.done() and task.get_loop() is running
    ]

# How long a drain may take before it says so, and how often it says it again.
#
# Decky gives a plugin five seconds to stop and then sends SIGKILL. Every drain
# on the unload path is unbounded on purpose, because cancelling the awaiter of
# an `asyncio.to_thread` worker does not stop the worker and abandoning it is
# what leaves a half-written file behind. The cost of that choice is that a
# drain which outlives the budget writes nothing at all: the record it would
# have written comes after the wait, the process is killed during it, and the
# journal simply ends. So a wait that is no longer ordinary reports itself while
# it is still waiting, which is the only moment it can.
#
# One second is well inside the budget and far outside what any of these drains
# costs when it is healthy; on this project's device every owner but one closes
# in 0 ms.
DRAIN_REPORT_AFTER_S = 1.0
DRAIN_REPORT_EVERY_S = 2.0
# A drain may legitimately be long: a managed Cheat Engine extraction is minutes
# of native work. It is described while it is happening and then left alone.
MAX_DRAIN_REPORTS = 8
# Turn counts at which a drain that is going round without waiting says so.
#
# A healthy drain of a slow worker turns a handful of times: once per report
# interval. Thousands of turns means every await is raising before it suspends,
# which is a spin rather than a wait, and it is invisible in wall clock terms
# because no interval ever expires to report one.
DRAIN_SPIN_TURNS = frozenset({64, 4096, 262144})


def _pending_description(watching: "Sequence[asyncio.Future[Any]]") -> tuple[int, str | None]:
    """How much of what is being drained is still running, and what it is.

    A `gather` future says nothing about its children, so the caller hands over
    the futures it gathered. The description is the coroutine Python already
    holds for the task, which names this project's own function and the line it
    is suspended on: that is the answer to "what is the unload waiting for", and
    it is our own source rather than anything the user typed.
    """
    pending = [item for item in watching if not item.done()]
    if not pending:
        return 0, None
    coroutine = getattr(pending[0], "get_coro", None)
    described = repr(coroutine()) if callable(coroutine) else repr(pending[0])
    return len(pending), safe_log_text(described, 200)


async def drain_through_cancellation(
    future: "asyncio.Future[Any]",
    *,
    label: str = "drain",
    logger: Any = None,
    watching: "Sequence[asyncio.Future[Any]]" = (),
) -> bool:
    """Await one future to a terminal state, reporting whether we were cancelled.

    A single `asyncio.shield` only survives the first cancellation: a second one
    is raised out of the handler that was meant to drain, which abandons exactly
    the native worker the shield exists to protect. Unload can be cancelled more
    than once, so hold every cancellation until the future itself is done and let
    the caller re-raise it afterwards.

    The wait is unbounded, and it says so out loud once it stops being ordinary.
    `label` names the drain, and `watching` is what was gathered, so the record
    can say how much of it is still running and which coroutine the first of
    those is suspended in. Nothing here ever stops waiting: the timeout belongs
    to the shield wrapper that this creates and discards, never to `future`.
    """
    cancelled = False
    started = time.monotonic()
    reports = 0
    turns = 0
    cancellations = 0
    while not future.done():
        turns += 1
        try:
            await asyncio.wait_for(
                asyncio.shield(future),
                timeout=DRAIN_REPORT_EVERY_S if reports else DRAIN_REPORT_AFTER_S,
            )
        except asyncio.CancelledError:
            cancelled = True
            cancellations += 1
            # A cancellation this absorbs is the whole point of the helper, and
            # it is also the one thing that can stop it making progress: a
            # cancellation redelivered on every resumption is raised before this
            # ever suspends, so the loop turns without yielding and the work it
            # is waiting for never gets to run. Said once, with the count, so a
            # drain that is spinning is distinguishable from one that is waiting.
            if cancellations == 1 and reports < MAX_DRAIN_REPORTS:
                reports += 1
                log_activity(
                    logger, "warning", "drain.cancellation_absorbed",
                    drain=label, waited_ms=int((time.monotonic() - started) * 1000),
                )
        except TimeoutError:
            # The wrapper expired, never the future: `shield` is what `wait_for`
            # cancelled, and the work behind it is untouched and still running.
            if reports < MAX_DRAIN_REPORTS:
                reports += 1
                pending, description = _pending_description(watching)
                log_activity(
                    logger, "warning", "drain.waiting",
                    drain=label, waited_ms=int((time.monotonic() - started) * 1000),
                    pending=pending or None, on=description,
                )
        except BaseException:
            # The future's own outcome belongs to whoever reads its result.
            pass
        if turns in DRAIN_SPIN_TURNS and reports < MAX_DRAIN_REPORTS:
            reports += 1
            pending, description = _pending_description(watching)
            log_activity(
                logger, "warning", "drain.spinning",
                drain=label, turns=turns, cancellations=cancellations,
                waited_ms=int((time.monotonic() - started) * 1000),
                pending=pending or None, on=description,
            )
    if reports:
        log_activity(
            logger, "info", "drain.finished",
            drain=label, waited_ms=int((time.monotonic() - started) * 1000), cancelled=cancelled,
        )
    return cancelled


async def drained_to_thread(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run a bounded native worker without letting cancellation orphan it."""
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    # `asyncio.to_thread` cannot stop its native worker. Hold cancellation until
    # it reaches a terminal state so task cancellation is a real filesystem or
    # process mutation drain boundary.
    if await drain_through_cancellation(worker):
        raise asyncio.CancelledError
    return worker.result()


class OperationRegistry:
    """Track plugin-owned work across Decky unload.

    Cooperative async work created with ``create`` is cancelled on unload.
    Bounded blocking filesystem work submitted with ``run_blocking`` is different:
    cancelling ``asyncio.to_thread`` does not stop the underlying thread.  Those
    operations are therefore shielded from caller cancellation and *drained* during
    unload so an atomic import cannot keep mutating plugin state after Decky has
    declared the plugin unloaded.
    """

    def __init__(self, logger=None) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._drain_on_close: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._logger = logger

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def create(self, awaitable: Awaitable[T], *, label: str = "async_operation") -> asyncio.Task[T]:
        """Create cancellable cooperative async work owned by the plugin."""
        if self._closing:
            # Callers commonly pass an already-created coroutine. Close it when
            # refusing new work so Python does not emit an un-awaited coroutine
            # warning during plugin teardown.
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            exc = RuntimeError("plugin is unloading")
            log_failure(self._logger, "operation.rejected", exc, expected=True, name=label)
            raise exc
        task = asyncio.create_task(self._execute(label, awaitable))
        self._track(task, drain_on_close=False)
        return task

    async def run_blocking(self, func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run bounded blocking work without allowing false cancellation semantics.

        ``asyncio.to_thread`` keeps executing after its awaiter is cancelled.  Shielding
        the tracked task means Decky RPC cancellation can abandon the response while the
        registry still owns the real work; ``close`` then waits for it to finish.
        """
        if self._closing:
            exc = RuntimeError("plugin is unloading")
            log_failure(
                self._logger, "operation.rejected", exc, expected=True,
                name=getattr(func, "__name__", "blocking_operation"),
            )
            raise exc
        task: asyncio.Task[T] = asyncio.create_task(
            self._execute(getattr(func, "__name__", "blocking_operation"), asyncio.to_thread(func, *args, **kwargs))
        )
        self._track(task, drain_on_close=True)
        return await asyncio.shield(task)

    async def _execute(self, label: str, awaitable: Awaitable[T]) -> T:
        operation_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        quiet = label.startswith(("get_", "list_", "poll_", "inspect_", "diagnostics_", "plan_", "evaluate_"))
        log_activity(self._logger, "debug" if quiet else "info", "operation.started", operation=operation_id, name=label)
        try:
            result = await awaitable
        except asyncio.CancelledError:
            log_activity(
                self._logger, "info", "operation.cancelled",
                operation=operation_id, name=label,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise
        except Exception as exc:
            log_failure(
                self._logger, "operation.failed", exc,
                expected=isinstance(exc, (ValueError, RuntimeError, OSError)),
                operation=operation_id, name=label,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise
        duration_ms = round((time.monotonic() - started) * 1000)
        log_activity(
            self._logger,
            "info" if not quiet or duration_ms >= 1000 else "debug",
            "operation.completed",
            operation=operation_id, name=label, duration_ms=duration_ms,
        )
        return result

    def _track(self, task: asyncio.Task[Any], *, drain_on_close: bool) -> None:
        self._tasks.add(task)
        if drain_on_close:
            self._drain_on_close.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            self._drain_on_close.discard(completed)

        task.add_done_callback(done)

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        log_activity(
            self._logger, "info", "operations.close_started",
            active=len(tasks), blocking=sum(task in self._drain_on_close for task in tasks),
        )
        for task in tasks:
            if task not in self._drain_on_close:
                task.cancel()
        cancelled = False
        if tasks:
            # Cancelling plugin unload must not cancel the awaiter for a native
            # worker that Python cannot stop. Drain every tracked operation
            # first, then preserve the caller's cancellation.
            cancelled = await drain_through_cancellation(
                asyncio.gather(*tasks, return_exceptions=True),
                label="operations.tracked", logger=self._logger, watching=tasks,
            )
        self._tasks.clear()
        self._drain_on_close.clear()
        log_activity(self._logger, "info", "operations.close_completed", drained=len(tasks))
        if cancelled:
            raise asyncio.CancelledError
