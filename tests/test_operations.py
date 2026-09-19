import asyncio
import logging
import threading

import pytest

from ce_decky import operations
from ce_decky.operations import OperationRegistry, drainable_tasks, drained_to_thread


@pytest.mark.asyncio
async def test_close_cancels_owned_cooperative_tasks():
    registry = OperationRegistry()
    entered = asyncio.Event()

    async def sleeper():
        entered.set()
        await asyncio.sleep(60)

    task = registry.create(sleeper())
    await entered.wait()
    await registry.close()
    assert task.cancelled()
    assert registry.closing
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_close_drains_blocking_thread_instead_of_pretending_to_cancel_it():
    registry = OperationRegistry()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_import() -> str:
        entered.set()
        assert release.wait(timeout=5)
        finished.set()
        return "done"

    call = asyncio.create_task(registry.run_blocking(blocking_import))
    await asyncio.to_thread(entered.wait, 2)
    assert registry.active_count == 1

    closing = asyncio.create_task(registry.close())
    await asyncio.sleep(0)
    assert not closing.done()
    assert not finished.is_set()

    release.set()
    assert await call == "done"
    await closing
    assert finished.is_set()
    assert registry.closing
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_cancelled_close_still_drains_blocking_thread():
    registry = OperationRegistry()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_import() -> str:
        entered.set()
        assert release.wait(timeout=5)
        finished.set()
        return "done"

    call = asyncio.create_task(registry.run_blocking(blocking_import))
    assert await asyncio.to_thread(entered.wait, 2)
    closing = asyncio.create_task(registry.close())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    assert not finished.is_set()

    release.set()
    assert await call == "done"
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert finished.is_set()
    assert registry.closing
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_repeatedly_cancelled_close_still_drains_blocking_thread():
    registry = OperationRegistry()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_import() -> str:
        entered.set()
        assert release.wait(timeout=5)
        finished.set()
        return "done"

    call = asyncio.create_task(registry.run_blocking(blocking_import))
    assert await asyncio.to_thread(entered.wait, 2)
    closing = asyncio.create_task(registry.close())
    await asyncio.sleep(0)
    # A shutdown that cancels more than once must not abandon the native worker
    # on the second attempt, which is exactly when a single shield gives up.
    for _ in range(3):
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert not finished.is_set()

    release.set()
    assert await call == "done"
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert finished.is_set()
    assert registry.closing
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_repeatedly_cancelled_thread_helper_still_returns_after_the_worker():
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def mutation() -> None:
        entered.set()
        assert release.wait(timeout=5)
        finished.set()

    async def caller() -> None:
        await drained_to_thread(mutation)

    task = asyncio.create_task(caller())
    assert await asyncio.to_thread(entered.wait, 2)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


@pytest.mark.asyncio
async def test_cancelled_rpc_does_not_orphan_underlying_blocking_mutation():
    registry = OperationRegistry()
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_import() -> None:
        entered.set()
        assert release.wait(timeout=5)
        finished.set()

    caller = asyncio.create_task(registry.run_blocking(blocking_import))
    await asyncio.to_thread(entered.wait, 2)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    # Shielding keeps the to_thread task registry-owned after the RPC waiter is gone.
    assert registry.active_count == 1
    closing = asyncio.create_task(registry.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert finished.is_set()
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_a_drain_that_stops_being_ordinary_says_so_while_it_is_waiting(caplog, monkeypatch):
    """A wait writes its record after it ends, and a killed process has no after.

    Decky gives a plugin five seconds to stop and then sends SIGKILL. Every
    drain on the unload path is unbounded on purpose, because cancelling the
    awaiter of a native worker does not stop the worker. The cost is that a
    drain which outlives that budget used to write nothing at all: on this
    project's device the journal simply ended inside one and which wait it was
    had to be guessed. So the wait reports itself while it is still waiting,
    which is the only moment it can, and names what it is waiting on.
    """
    monkeypatch.setattr(operations, "DRAIN_REPORT_AFTER_S", 0.01)
    monkeypatch.setattr(operations, "DRAIN_REPORT_EVERY_S", 0.01)
    logger = logging.getLogger("test-drain-reports")
    release = asyncio.Event()

    async def a_wait_nobody_can_see() -> None:
        await release.wait()

    slow = asyncio.create_task(a_wait_nobody_can_see())
    with caplog.at_level(logging.INFO, logger=logger.name):
        draining = asyncio.create_task(operations.drain_through_cancellation(
            asyncio.gather(slow, return_exceptions=True),
            label="test.owner", logger=logger, watching=(slow,),
        ))
        for _ in range(200):
            if any("event=drain.waiting" in record.getMessage() for record in caplog.records):
                break
            await asyncio.sleep(0.01)
        release.set()
        assert await draining is False

    messages = [record.getMessage() for record in caplog.records]
    waiting = [message for message in messages if "event=drain.waiting" in message]
    assert waiting, "the drain never said it was waiting"
    assert "drain=test.owner" in waiting[0]
    assert "pending=1" in waiting[0]
    # The coroutine Python already holds for the task, which names our own
    # function and the line it is suspended on. That is the answer to what the
    # unload is waiting for.
    assert "a_wait_nobody_can_see" in waiting[0]
    # And it never reports for ever, however long the work legitimately takes.
    assert len(waiting) <= operations.MAX_DRAIN_REPORTS
    assert any("event=drain.finished" in message and "drain=test.owner" in message for message in messages)


@pytest.mark.asyncio
async def test_an_ordinary_drain_stays_silent(caplog):
    """Every plugin load ends in one of these, and almost all of them are free."""
    logger = logging.getLogger("test-drain-quiet")
    done = asyncio.create_task(asyncio.sleep(0))
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        assert await operations.drain_through_cancellation(
            asyncio.gather(done, return_exceptions=True),
            label="test.quick", logger=logger, watching=(done,),
        ) is False

    assert not [record for record in caplog.records if "event=drain." in record.getMessage()]


@pytest.mark.asyncio
async def test_a_drain_that_is_spinning_rather_than_waiting_says_which_it_is(caplog):
    """A cancellation redelivered on every resumption never reaches a timeout.

    The helper absorbs cancellation on purpose, so that unload cannot abandon a
    native worker. If the cancellation comes back every time the coroutine is
    resumed, the absorbing loop turns without ever suspending: the work it is
    waiting for does not get to run, no report interval ever expires, and the
    whole drain is invisible. That is indistinguishable in a log from a drain
    that was never entered, which is why the turns are counted.
    """
    logger = logging.getLogger("test-drain-spin")
    forever = asyncio.get_running_loop().create_future()

    async def caller() -> None:
        await operations.drain_through_cancellation(
            forever, label="test.spin", logger=logger, watching=(forever,),
        )

    task = asyncio.create_task(caller())
    await asyncio.sleep(0)
    with caplog.at_level(logging.INFO, logger=logger.name):
        # Cancelled far more often than a healthy drain ever suspends.
        for _ in range(200):
            task.cancel()
            await asyncio.sleep(0)
        forever.set_result(None)
        await task

    messages = [record.getMessage() for record in caplog.records]
    absorbed = [message for message in messages if "event=drain.cancellation_absorbed" in message]
    spinning = [message for message in messages if "event=drain.spinning" in message]
    assert absorbed, "an absorbed cancellation was never recorded"
    assert spinning, "a drain going round without waiting never said so"
    assert "drain=test.spin" in spinning[0]
    assert "cancellations=" in spinning[0]
    # Bounded, so a spin cannot itself become the flood that hides the answer.
    assert len(absorbed) == 1
    assert len(spinning) <= len(operations.DRAIN_SPIN_TURNS)


def test_a_task_belonging_to_a_closed_loop_is_never_drained():
    """The filter every owner's `close()` puts between itself and `gather`.

    A task belonging to a loop that has been closed cannot be made to run again
    by the loop doing the closing, and asking anyway is not a no-op: `gather`
    registers a callback on that task's loop, which on Python 3.11 - what CI
    runs and what Decky ships on the device - raises `Event loop is closed` out
    of unload. Python 3.13 takes a shortcut for a task that is already done and
    hides it, so this is a rule rather than something a local run discovers.
    """
    finished = asyncio.new_event_loop()
    try:
        elsewhere = finished.create_task(asyncio.sleep(0))
        finished.run_until_complete(elsewhere)
    finally:
        finished.close()

    async def ask() -> None:
        live = asyncio.get_running_loop().create_task(asyncio.sleep(60))
        try:
            assert drainable_tasks((elsewhere, live, None)) == [live]
            # And what the filter exists to prevent, asked on the loop that
            # would otherwise ask it: a finished task from a closed loop still
            # reaches for that loop the moment anything waits on it.
            await asyncio.gather(*drainable_tasks((elsewhere,)), return_exceptions=True)
        finally:
            live.cancel()
            await asyncio.gather(live, return_exceptions=True)

    asyncio.run(ask())
