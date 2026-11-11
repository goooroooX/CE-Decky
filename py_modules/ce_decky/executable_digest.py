"""SHA-256 of a game's own executable, for comparing compatibility evidence.

A table's positive record is compared with the game in front of the reader by
the version the game's executable declares and by Steam's build ID. Some games
have neither: a non-Steam shortcut has no build ID by construction, and plenty
of executables carry no version resource. For those, the exact bytes of the
executable are the only comparable thing left, and this module is where they are
read.

What the digest is, and what it is not: it identifies the executable file's
bytes. It is not a version, it says nothing about the DLLs and data beside it,
and it does not make an activated cheat a proven effect. It is advisory, like
everything in the positive store, and nothing here refuses or authorizes
anything.

Three rules shape the code.

- The read never happens where it can be felt. A game's executable has been
  measured at hundreds of megabytes, so it is read by one worker thread of this
  module's own, never on Decky's event loop, never under the service's mutation
  lock and never inside a status call: a status call asks, gets `pending`, and
  asks again later.
- A digest is about the file identity it was read from, and is reused only for
  that identity: path, device, inode, size and both modification and change
  times. A same-size rewrite that restores the modification time still moves the
  change time, which the kernel sets and a writer cannot. This is advisory reuse
  on a local filesystem, not a defence against someone able to forge inode
  metadata.
- A digest of the file on disk is a statement about a running game only when the
  running game is shown to be those bytes: every process that named the path has
  that exact device and inode mapped, and the file had not been changed since
  before the earliest of those processes started. A game keeps running the
  executable it started with after the file is replaced, and an executable
  rewritten in place under a running game would move its change time past the
  start. Where any of that cannot be shown, the answer is `unavailable`, which
  compares as unknown, never as a match.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading
import time

from .activity_log import log_activity
from .atomic import file_identity, read_proc_bytes, sha256_regular_file
from .ce_launch import _process_start_time

# The largest executable this will read. Chosen from the measurement in
# `docs/FIELD_NOTES.md`, which `scripts/executable_digest_bench.py` produces: the
# game executable that prompted this is 457 MiB, and this is more than eight
# times that. A larger one is not a game this can compare.
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
# How long one digest may take before it is abandoned between two chunks. The
# same measurement read those 457 MiB in 0.27 seconds from the page cache, and a
# whole 4 GiB at that rate is under three. A cold read was not measured, so this
# is a hundred times the measured read: room for a slow disk and a busy device,
# while still stopping one stuck file holding the only worker for longer than a
# user waits.
DEADLINE_S = 30.0
CHUNK_BYTES = 1024 * 1024
# Results kept, newest used last out. A device compares one selected game and
# whatever is running, so this is far more than an ordinary panel ever needs,
# and each entry is a few hundred bytes.
MAX_RESULTS = 64
# Files waiting for the worker. A status read asks for at most the selected game
# and the games that are running.
MAX_QUEUED = 8
# A failed read is tried again for the same identity after these delays, and
# then not again until the file changes or the backend restarts. A file that
# changes gets a new identity and so a new first attempt.
RETRY_DELAYS_S = (30.0, 300.0)
# `/proc/<pid>/maps` of a running Proton game was measured at 628 KB and 7002
# mappings on this project's Steam Machine; this is more than ten times that.
MAX_MAPS_BYTES = 8 * 1024 * 1024
# How much earlier than the process the file's last change must be. A process
# start is known to a clock tick and the boot time it is counted from is derived
# from two clocks read a moment apart, so a file changed within this margin of
# the start is not one that can be said to predate it.
START_MARGIN_S = 2.0
# Proven (processes, file identity) pairs kept, so a status read does not parse
# a running game's whole memory map every time it asks.
MAX_PROVEN_RUNS = 32

Identity = tuple[int, int, int, int, int]


@dataclass(frozen=True)
class DigestLookup:
    """One answer: `ready` with a digest, `pending`, or `unavailable` with a reason."""

    state: str
    digest: str | None = None
    reason: str | None = None


@dataclass
class _Failure:
    reason: str
    attempts: int
    retry_at: float | None


def regular_file_identity(path: str | Path) -> Identity | None:
    """The identity of `path` as it is now, or `None` for anything but a regular file."""
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return file_identity(info)


def _maps_holds(raw: bytes, dev: int, inode: int) -> bool:
    """Whether one `/proc/<pid>/maps` maps this device and inode.

    The device is printed as hexadecimal major and minor numbers. A filesystem
    whose `stat` device differs from the one its mappings report, which some
    layered filesystems do, maps nothing here and is refused rather than
    matched by inode alone.
    """
    major, minor = os.major(dev), os.minor(dev)
    wanted = str(inode).encode("ascii")
    for line in raw.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5 or fields[4] != wanted:
            continue
        device = fields[3].split(b":")
        if len(device) != 2:
            continue
        try:
            if int(device[0], 16) == major and int(device[1], 16) == minor:
                return True
        except ValueError:
            continue
    return False


def _booted_at(proc_root: Path) -> float | None:
    try:
        uptime = float((proc_root / "uptime").read_text(encoding="ascii")[:64].split()[0])
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        return None
    return None if ticks <= 0 else time.time() - uptime


class ExecutableDigests:
    """One bounded hashing facility, shared by every caller in this backend.

    `lookup` never reads a file. It answers from what is already known for that
    exact identity, and otherwise queues the read and answers `pending`. Two
    callers asking about one identity share one read, and a panel opened again
    with nothing changed is answered from memory. Nothing is kept on disk: a
    restarted backend reads again, once, when it is next asked.
    """

    def __init__(self, logger, *, reader=sha256_regular_file, clock=time.monotonic,
                 proc_root: Path = Path("/proc"), max_file_bytes: int = MAX_FILE_BYTES,
                 deadline_s: float = DEADLINE_S, max_results: int = MAX_RESULTS,
                 max_queued: int = MAX_QUEUED, on_settled=None) -> None:
        self.logger = logger
        # Called from the worker once a read has an answer, so what waits for
        # one does not have to wait for somebody to ask again.
        self._on_settled = on_settled
        self._reader = reader
        self._clock = clock
        self._proc_root = proc_root
        self._max_file_bytes = max_file_bytes
        self._deadline_s = deadline_s
        self._max_results = max_results
        self._max_queued = max_queued
        self._lock = threading.Lock()
        self._results: OrderedDict[tuple[str, Identity], str | _Failure] = OrderedDict()
        self._queue: deque[tuple[str, Identity]] = deque()
        self._running: tuple[str, Identity] | None = None
        self._worker: threading.Thread | None = None
        self._closed = threading.Event()
        self._saturated = False
        self._proven: OrderedDict[tuple, bool] = OrderedDict()

    # -- the cache ---------------------------------------------------------

    def lookup(self, path: str, identity: Identity | None) -> DigestLookup:
        """What is known about the bytes at `path` with exactly this identity."""
        if identity is None:
            return DigestLookup("unavailable", reason="executable is not a readable regular file")
        key = (str(path), tuple(identity))
        with self._lock:
            if self._closed.is_set():
                return DigestLookup("unavailable", reason="backend is closing")
            known = self._results.get(key)
            if isinstance(known, str):
                self._results.move_to_end(key)
                return DigestLookup("ready", digest=known)
            if isinstance(known, _Failure):
                if known.retry_at is None or self._clock() < known.retry_at:
                    return DigestLookup("unavailable", reason=known.reason)
            if key == self._running or key in self._queue:
                return DigestLookup("pending")
            if len(self._queue) >= self._max_queued:
                if not self._saturated:
                    self._saturated = True
                    log_activity(self.logger, "warning", "executable_digest.queue_full", queued=len(self._queue))
                return DigestLookup("unavailable", reason="too many executables are waiting to be read")
            self._saturated = False
            self._queue.append(key)
            if self._worker is None:
                worker = threading.Thread(target=self._work, name="ce-decky-executable-digest", daemon=True)
                try:
                    worker.start()
                except RuntimeError:
                    # A status read must not fail because a thread could not be
                    # had; the comparison is unknown and is asked for again.
                    self._queue.remove(key)
                    return DigestLookup("unavailable", reason="no worker thread could be started")
                self._worker = worker
            return DigestLookup("pending")

    def close(self) -> None:
        """Refuse new work and stop the read in progress at its next chunk.

        A read blocked inside the kernel is not interrupted by this; it finishes
        and its answer is discarded, and `on_settled` is not called for it. The
        worker writes nothing itself; what `on_settled` writes is the caller's,
        and one already running when this is called finishes its own write.
        """
        with self._lock:
            self._closed.set()
            self._queue.clear()

    def _work(self) -> None:
        while True:
            with self._lock:
                if self._closed.is_set() or not self._queue:
                    self._worker = None
                    self._running = None
                    return
                key = self._running = self._queue.popleft()
                previous = self._results.get(key)
            path, identity = key
            started = self._clock()

            def should_stop() -> bool:
                return self._closed.is_set() or self._clock() - started > self._deadline_s

            try:
                digest, _ = self._reader(
                    Path(path), max_bytes=self._max_file_bytes, expected_identity=identity,
                    should_stop=should_stop, chunk_bytes=CHUNK_BYTES,
                )
                result: str | _Failure = digest
            except (OSError, ValueError) as exc:
                reason = str(exc)[:160] or type(exc).__name__
                if self._clock() - started > self._deadline_s:
                    reason = "reading the executable took longer than its bound"
                attempts = previous.attempts + 1 if isinstance(previous, _Failure) else 1
                # A file too large or not a regular file stays that way until it
                # changes, and a changed file is a new identity: trying the same
                # identity again would read nothing new.
                permanent = "outside the hashing bound" in reason or "regular file" in reason
                retry_at = None if permanent or attempts > len(RETRY_DELAYS_S) else self._clock() + RETRY_DELAYS_S[attempts - 1]
                result = _Failure(reason, attempts, retry_at)
            elapsed = self._clock() - started
            with self._lock:
                self._running = None
                if self._closed.is_set():
                    self._worker = None
                    return
                self._results[key] = result
                self._results.move_to_end(key)
                while len(self._results) > self._max_results:
                    self._results.popitem(last=False)
            name = Path(path).name
            if isinstance(result, str):
                log_activity(self.logger, "info", "executable_digest.read", executable=name,
                             size=identity[2], seconds=round(elapsed, 3), digest=result[:12])
            elif result.retry_at is None:
                log_activity(self.logger, "warning", "executable_digest.retries_exhausted", executable=name,
                             size=identity[2], attempts=result.attempts, reason=result.reason)
            else:
                log_activity(self.logger, "warning", "executable_digest.unavailable", executable=name,
                             size=identity[2], attempts=result.attempts, reason=result.reason)
            if self._on_settled is not None and not self._closed.is_set():
                try:
                    self._on_settled()
                except Exception as exc:  # noqa: BLE001 - the worker outlives what it reports to
                    log_activity(self.logger, "warning", "executable_digest.settle_failed",
                                 executable=name, reason=str(exc)[:160] or type(exc).__name__)

    # -- the running game ----------------------------------------------------

    def running_identity(self, path: str, pids) -> tuple[Identity | None, str | None]:
        """The identity of `path`, where every one of `pids` is running those bytes.

        Returns the identity, or `None` and the reason it could not be shown.
        A proven answer is remembered for exactly those processes, with their
        start times, and that exact file identity, so asking again while nothing
        changed costs one `lstat` and one small read per process.
        """
        if not pids:
            return None, "no running process named the executable"
        before = regular_file_identity(path)
        if before is None:
            return None, "executable is not a readable regular file"
        starts = []
        for pid in pids:
            ticks = _process_start_time(pid, self._proc_root)
            if ticks is None:
                return None, "a game process could not be read"
            starts.append((pid, ticks))
        key = (str(path), before, tuple(starts))
        with self._lock:
            if self._proven.get(key):
                self._proven.move_to_end(key)
                return before, None
        booted = _booted_at(self._proc_root)
        if booted is None:
            return None, "the process start time could not be read"
        hz = os.sysconf("SC_CLK_TCK")
        dev, inode, _, mtime_ns, ctime_ns = before
        changed = max(mtime_ns, ctime_ns) / 1e9
        for pid, ticks in starts:
            if changed > booted + ticks / hz - START_MARGIN_S:
                return None, "the executable changed after the game started"
            try:
                raw = read_proc_bytes(self._proc_root / str(pid) / "maps", max_bytes=MAX_MAPS_BYTES)
            except ValueError:
                return None, "a game process's mappings could not be read"
            if not raw or not _maps_holds(raw, dev, inode):
                return None, "the running game does not map this executable"
            if _process_start_time(pid, self._proc_root) != ticks:
                return None, "a game process was replaced while it was read"
        if regular_file_identity(path) != before:
            return None, "the executable changed while it was read"
        with self._lock:
            self._proven[key] = True
            while len(self._proven) > MAX_PROVEN_RUNS:
                self._proven.popitem(last=False)
        return before, None
