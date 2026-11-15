"""Whether Decky's loader is still a loader worth asking, while a plugin is replaced.

Decky replaces a plugin through `_install()`, and that path carries two upstream
defects `docs/FIELD_NOTES.md` records, read in v3.2.8 and again in v3.3.1: the hot-reload watcher is re-enabled in the middle of the install, so
the plugin is loaded, stopped and loaded again, and a plugin socket at EOF is
read in a loop that never suspends. On the device, an ordinary replacement of
another plugin through Decky's own store was followed within minutes by the
loader process holding gigabytes and the machine killing Steam to recover. What
starts the growth is not known; what is known is that a caller which goes on
opening connections and asking for a webhelper restart while it happens is
adding work to a process that is already failing, and that the time to stop
asking is when the growth is visible rather than after the machine has gone.

So both callers that replace this plugin (the detached update runner and the
development install helper) take a reading of the loader before they ask for
anything, and read it again before every later request. A loader that has
exited, or has grown far beyond anything a replacement costs, is an incident:
they stop asking, say so, and leave the rest to the person at the device.

The loader is found by the process title Decky gives it, `Decky Loader v...`,
which is also how `scripts/target_memory_watch.py` and earlyoom name it. Exactly
one such process is required, and it is remembered by pid and start time so a
reused pid is never read as the loader. Anything else is `unobserved`, and an
unobserved loader gates nothing: this only ever stops a caller on evidence.
Reading `/proc/<pid>/cmdline`, `stat` and `status` of the root-owned loader is
permitted to the plugin's own user; nothing else of it is touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .loader_watch import PROC_ROOT, ProcessStat, is_alive, read_stat

LOADER_TITLE_PREFIX = "Decky Loader "
# Growth over the reading taken before the request that marks an incident. A
# healthy loader on the device holds about a hundred MiB and a replacement
# changes it by roughly one plugin's footprint, while the runaway this guards
# against reached twelve GiB within five minutes; two GiB sits far from both.
RUNAWAY_GROWTH_KIB = 2 * 1024 * 1024

_VMRSS = re.compile(r"^VmRSS:\s+(\d+) kB", re.MULTILINE)


@dataclass(frozen=True)
class LoaderReading:
    """One reading, in the shape the logs carry it."""

    state: str  # "ok", "exited", "runaway" or "unobserved"
    pid: int | None
    rss_kib: int | None
    baseline_kib: int | None

    @property
    def incident(self) -> bool:
        return self.state in {"exited", "runaway"}

    def as_fields(self) -> dict[str, object]:
        growth = None
        if self.rss_kib is not None and self.baseline_kib is not None:
            growth = self.rss_kib - self.baseline_kib
        return {
            "loader_state": self.state, "loader_pid": self.pid,
            "loader_rss_kib": self.rss_kib, "loader_growth_kib": growth,
        }

    def describe(self) -> str:
        """What was seen, for an incident; the caller says what it was doing."""
        if self.state == "exited":
            return "Decky's loader exited"
        if self.state == "runaway" and self.rss_kib is not None and self.baseline_kib is not None:
            grown = (self.rss_kib - self.baseline_kib) / (1024 * 1024)
            return f"Decky's loader grew by {grown:.1f} GiB of memory"
        return "Decky's loader could not be observed"


def find_loader(proc_root: Path = PROC_ROOT) -> ProcessStat | None:
    """The one process titled as Decky's loader, or None when not exactly one."""
    found: list[ProcessStat] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            title = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if not title.startswith(LOADER_TITLE_PREFIX):
            continue
        stat = read_stat(int(entry.name), proc_root)
        if stat is not None:
            found.append(stat)
    return found[0] if len(found) == 1 else None


def rss_kib(pid: int, proc_root: Path = PROC_ROOT) -> int | None:
    try:
        status = (proc_root / str(pid) / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _VMRSS.search(status)
    return int(match.group(1)) if match else None


class LoaderMemoryGuard:
    """The loader as it was before the first request, and how it is now."""

    def __init__(self, proc_root: Path = PROC_ROOT, growth_limit_kib: int = RUNAWAY_GROWTH_KIB) -> None:
        self._proc_root = proc_root
        self._limit = growth_limit_kib
        self._loader = find_loader(proc_root)
        self._baseline = rss_kib(self._loader.pid, proc_root) if self._loader is not None else None
        if self._baseline is None:
            self._loader = None

    def read(self) -> LoaderReading:
        if self._loader is None or self._baseline is None:
            return LoaderReading("unobserved", None, None, None)
        pid = self._loader.pid
        current = rss_kib(pid, self._proc_root) if is_alive(self._loader, self._proc_root) else None
        # Asked again after the size, so a loader that was replaced between the
        # two reads is reported as gone rather than as some other process's size.
        if current is None or not is_alive(self._loader, self._proc_root):
            return LoaderReading("exited", pid, None, self._baseline)
        state = "runaway" if current - self._baseline >= self._limit else "ok"
        return LoaderReading(state, pid, current, self._baseline)
