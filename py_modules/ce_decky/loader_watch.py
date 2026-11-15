"""Leave when Decky's loader is gone, rather than outlive it.

`docs/FIELD_NOTES.md` records what happens to this process when the loader that
started it dies: the unit runs with `KillMode=process`, so systemd ends the
loader alone and every plugin process is left running; the plugin end of the
loader's socket then reads at EOF in a loop that never suspends, and the
process grows until the kernel or earlyoom ends it. On the device that orphan
was one of the processes the machine had to kill to recover, and the restarted
loader had already started a second backend of this plugin beside it.

Nothing on the event loop can notice, because the loop is the thing that has
been taken away. A thread can: the spin is Python code, and the interpreter
switches threads on its own interval whatever the running one does, which
`tests/test_loader_watch.py` proves against a main thread that never yields.
It asks one question a second, whether this process still has the parent it
was started under, and only once that has changed does it read anything else.

The parent alone does not say whether the loader is gone. On the device the
plugin's parent is the `multiprocessing` forkserver, whose own parent is the
loader, and the forkserver can in principle be ended while the loader lives.
Leaving then would close the plugin's end of the socket under a loader that is
still reading it, which is the loader half of the same EOF defect. So the
grandparent recorded at start has to be gone as well, by the identity it had
then (pid and start time, so a reused pid is not taken for it). Under the
forkserver that is the loader itself; under `fork` or `spawn` the parent is the
loader and the grandparent is the PyInstaller bootloader that only waits for
it, so in both shapes the grandparent is gone exactly when the loader is.
A grandparent that is PID 1 counts as gone, because that is the case where the
parent was the only thing between this process and init.

Where the ancestry cannot be read at start, nothing is armed and the plugin
behaves as it did before this existed: a watch that cannot tell the two cases
apart is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import threading
from typing import Callable

# How often the thread asks. One `getppid` a second costs nothing, and the
# orphan this exists to end grows for as long as it is left running.
#
# Review note (2026-11-15, the maintainer's decision on a proposal raised after
# `scripts/target_loader_death_probe.py` measured the orphan growing by up to
# about 100 MiB in the time it took to leave): not polled faster. A loader
# dying is not the ordinary case, and a check that never finds anything in it
# is not made more often for the sake of the one that does.
POLL_S = 1.0

PROC_ROOT = Path("/proc")


@dataclass(frozen=True)
class ProcessStat:
    """What `/proc/<pid>/stat` says about one process, for this purpose."""

    pid: int
    ppid: int
    state: str
    start_ticks: int


def read_stat(pid: int, proc_root: Path = PROC_ROOT) -> ProcessStat | None:
    """One process's parent, state and start time, or None when it is gone.

    `comm` is the second field and can hold spaces and parentheses (Decky names
    the loader `Decky Loader`), so the fields are counted from the last `)`.
    """
    try:
        text = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = text.rfind(")")
    if end < 0:
        return None
    rest = text[end + 1:].split()
    # rest[0] is field 3 (state), rest[1] field 4 (ppid), rest[19] field 22
    # (starttime, in clock ticks since boot).
    if len(rest) < 20:
        return None
    try:
        return ProcessStat(pid=pid, ppid=int(rest[1]), state=rest[0], start_ticks=int(rest[19]))
    except ValueError:
        return None


def is_alive(expected: ProcessStat, proc_root: Path = PROC_ROOT) -> bool:
    """Whether the process recorded as `expected` still exists and still runs."""
    current = read_stat(expected.pid, proc_root)
    if current is None or current.start_ticks != expected.start_ticks:
        return False
    # A zombie has already exited; it is only waiting to be reaped.
    return current.state not in {"Z", "X", "x"}


class LoaderWatch:
    """A daemon thread that calls `on_lost` once, when the loader is gone."""

    def __init__(
        self,
        on_lost: Callable[[dict[str, object]], None],
        *,
        getppid: Callable[[], int] = os.getppid,
        proc_root: Path = PROC_ROOT,
        poll_s: float = POLL_S,
    ) -> None:
        self._on_lost = on_lost
        self._getppid = getppid
        self._proc_root = proc_root
        self._poll_s = poll_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._parent: int | None = None
        self._grandparent: ProcessStat | None = None

    def arm(self) -> dict[str, object]:
        """Record the ancestry and start watching it; say what was recorded.

        The returned fields are for the log line that says whether the watch is
        armed, and if not, why.
        """
        parent = self._getppid()
        if parent <= 1:
            return {"armed": False, "reason": "no_parent", "parent_pid": parent}
        parent_stat = read_stat(parent, self._proc_root)
        if parent_stat is None:
            return {"armed": False, "reason": "parent_unreadable", "parent_pid": parent}
        grandparent: ProcessStat | None = None
        if parent_stat.ppid > 1:
            grandparent = read_stat(parent_stat.ppid, self._proc_root)
            if grandparent is None:
                return {
                    "armed": False, "reason": "grandparent_unreadable",
                    "parent_pid": parent, "grandparent_pid": parent_stat.ppid,
                }
        self._parent = parent
        self._grandparent = grandparent
        self._thread = threading.Thread(target=self._run, name="ce-decky-loader-watch", daemon=True)
        self._thread.start()
        return {
            "armed": True, "parent_pid": parent,
            "grandparent_pid": grandparent.pid if grandparent is not None else parent_stat.ppid,
        }

    def stop(self) -> None:
        """Stop watching. Safe to call from any thread, armed or not."""
        self._stop.set()

    def check(self) -> dict[str, object] | None:
        """One poll: what was observed when the loader is gone, else None."""
        if self._parent is None:
            return None
        current = self._getppid()
        if current == self._parent:
            return None
        if self._grandparent is not None and is_alive(self._grandparent, self._proc_root):
            return None
        return {
            "parent_pid": self._parent,
            "grandparent_pid": self._grandparent.pid if self._grandparent is not None else None,
            "now_parent_pid": current,
        }

    def _run(self) -> None:
        while not self._stop.wait(self._poll_s):
            try:
                lost = self.check()
            except Exception:  # noqa: BLE001 - a failed read is asked again next second
                continue
            if lost is not None and not self._stop.is_set():
                self._on_lost(lost)
                return
