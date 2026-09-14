"""Monotonic counters for the repeating paths a live session runs.

What the backend costs while a session is attached is measured for the process
as a whole, and the process is running several loops at once: the supervisor's
one-second baseline check, its three-second target scan, and whatever the panel
asks for. A figure for the task cannot say which of them produced it, and two of
the cadences under suspicion are the same three seconds, so a wakeup count
cannot separate them either.

These counters name the path instead. Each one records how many times its path
ran and how much wall and processor time it spent there, so a reading taken
either side of a window says which loop was alive in it and what that loop cost,
rather than leaving both to be argued from a total.

Two rules keep the instrument from destroying what it measures, and both are
structural here rather than left to the caller:

* nothing is logged per call, because a line per invocation would add work at
  exactly the rate being measured;
* :func:`snapshot` counts nothing, so reading the counters never becomes one of
  the counts. Whatever call carries a snapshot out of the process must not be a
  path that appears below.

Processor time is per thread, from :func:`time.thread_time`, and the counted
paths run on the worker threads the RPC and supervision loops hand them to. It
is therefore the time that path actually spent on a core, not the time it was
alive, which is the difference that matters on a device held at a power limit.
"""
from __future__ import annotations

from contextlib import contextmanager
import secrets
import threading
import time
from typing import Iterator

#: The two calls the frontend detector makes, once per interval, for a selected
#: game. They are counted at the RPC boundary rather than inside the service, so
#: an internal caller such as the support bundle is not mistaken for the panel.
RPC_CE_LAUNCH_CAPABILITY = "rpc.get_ce_launch_capability"
RPC_RUNTIME_STATUS = "rpc.get_runtime_status"
#: The supervisor's own scan, whose cadence is known. It is the baseline that
#: says whether the window and the reader were working at all.
SUPERVISOR_TARGET_SCAN = "supervisor.game_target_state"
#: The process-table walk both suspects run. It bounds the total cost of walking
#: the table without attributing it to either caller on its own.
PRIMITIVE_GAME_CONTAINER = "primitive.observe_game_container"
#: The supervisor's one-second baseline check, counted per PID rather than per
#: tick. The tick runs once a second; this runs once per baseline PID inside it,
#: and a game under Proton leaves dozens of those, so the count and the cadence
#: are different questions and only the count says what the check costs.
SUPERVISOR_BASELINE_CHECK = "supervisor.game_process_state"
#: The cheap confirmation that the target this session already saw is still the
#: same process. Counted separately from the scan it replaces, so what the cheap
#: path costs and how often it falls through are both readable.
SUPERVISOR_TARGET_IDENTITY = "supervisor.target_identity_check"

PATHS = (
    RPC_CE_LAUNCH_CAPABILITY,
    RPC_RUNTIME_STATUS,
    SUPERVISOR_TARGET_SCAN,
    SUPERVISOR_BASELINE_CHECK,
    SUPERVISOR_TARGET_IDENTITY,
    PRIMITIVE_GAME_CONTAINER,
)

SCHEMA = 1

_lock = threading.Lock()
_calls: dict[str, int] = {path: 0 for path in PATHS}
_wall: dict[str, float] = {path: 0.0 for path in PATHS}
_cpu: dict[str, float] = {path: 0.0 for path in PATHS}
_since = time.monotonic()
# Which run of the backend these counters belong to. Cumulative counters are
# only subtractable within one process, and uptime cannot prove that on its own:
# a snapshot taken five seconds after load, a restart, and a second snapshot
# eighty seconds into the new process all read as an ordinary rising uptime, and
# subtracting them silently mixes two incarnations. A reader compares this
# instead, and a value it has not seen before is a restart rather than a delta.
_incarnation = secrets.token_hex(8)


def record(path: str, wall_seconds: float, cpu_seconds: float) -> None:
    """Add one completed pass of ``path``.

    A pass is counted whether it succeeded or raised: the cost was paid either
    way, and a path that fails at its own cadence is exactly what a reading has
    to be able to show.
    """
    if path not in _calls:
        raise ValueError(f"unknown counted path: {path!r}")
    with _lock:
        _calls[path] += 1
        _wall[path] += max(0.0, wall_seconds)
        _cpu[path] += max(0.0, cpu_seconds)


@contextmanager
def timed(path: str) -> Iterator[None]:
    """Count and time one pass of ``path``."""
    if path not in _calls:
        raise ValueError(f"unknown counted path: {path!r}")
    started_wall = time.monotonic()
    started_cpu = time.thread_time()
    try:
        yield
    finally:
        record(path, time.monotonic() - started_wall, time.thread_time() - started_cpu)


def snapshot() -> dict[str, object]:
    """Read every counter without incrementing anything.

    ``uptime_seconds`` is the backend's own clock since these counters started,
    so two snapshots bound their window from inside the process being measured
    rather than from the reader's wall clock and whatever the transport cost.
    ``incarnation`` says which run of the backend they belong to, which is what
    makes subtracting two of them legitimate.
    """
    with _lock:
        paths = {
            path: {
                "calls": _calls[path],
                "wall_seconds": round(_wall[path], 6),
                "cpu_seconds": round(_cpu[path], 6),
            }
            for path in PATHS
        }
        uptime = time.monotonic() - _since
        incarnation = _incarnation
    return {
        "schema": SCHEMA,
        "incarnation": incarnation,
        "uptime_seconds": round(uptime, 6),
        "paths": paths,
    }


def reset() -> None:
    """Return every counter to zero, as a fresh incarnation.

    For tests; nothing in production calls it. It takes a new incarnation
    because that is what a reset is from a reader's point of view: counters that
    no longer continue the ones it saw.
    """
    global _since, _incarnation
    with _lock:
        for path in PATHS:
            _calls[path] = 0
            _wall[path] = 0.0
            _cpu[path] = 0.0
        _since = time.monotonic()
        _incarnation = secrets.token_hex(8)
