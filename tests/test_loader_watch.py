import os
from pathlib import Path
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from ce_decky.loader_watch import LoaderWatch, ProcessStat, is_alive, read_stat

PY_MODULES = Path(__file__).resolve().parents[1] / "py_modules"


def _write_stat(root: Path, pid: int, *, ppid: int, start: int, state: str = "S", comm: str = "proc") -> None:
    # Fields 3..22 after `comm`; only state (3), ppid (4) and starttime (22) are read.
    fields = [state, str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 10
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")


def test_read_stat_counts_fields_from_the_last_parenthesis(tmp_path: Path):
    _write_stat(tmp_path, 1781, ppid=1598, start=620, comm="Decky Loader) (x")
    assert read_stat(1781, tmp_path) == ProcessStat(pid=1781, ppid=1598, state="S", start_ticks=620)
    assert read_stat(4242, tmp_path) is None


def test_is_alive_refuses_a_reused_pid_and_a_zombie(tmp_path: Path):
    recorded = ProcessStat(pid=1781, ppid=1598, state="S", start_ticks=620)
    _write_stat(tmp_path, 1781, ppid=1598, start=620)
    assert is_alive(recorded, tmp_path)
    _write_stat(tmp_path, 1781, ppid=1598, start=999)
    assert not is_alive(recorded, tmp_path)
    _write_stat(tmp_path, 1781, ppid=1598, start=620, state="Z")
    assert not is_alive(recorded, tmp_path)


class _Parent:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def __call__(self) -> int:
        return self.pid


def _forkserver_tree(root: Path) -> None:
    # The device's shape: plugin -> forkserver 2256 -> loader 1781 -> bootloader 1598 -> init.
    _write_stat(root, 2256, ppid=1781, start=691, comm="PluginLoader")
    _write_stat(root, 1781, ppid=1598, start=620, comm="Decky Loader")
    _write_stat(root, 1598, ppid=1, start=587, comm="PluginLoader")


def test_the_forkserver_dying_alone_is_not_the_loader_dying(tmp_path: Path):
    _forkserver_tree(tmp_path)
    parent = _Parent(2256)
    watch = LoaderWatch(lambda _observed: None, getppid=parent, proc_root=tmp_path)
    try:
        assert watch.arm() == {"armed": True, "parent_pid": 2256, "grandparent_pid": 1781}
        assert watch.check() is None
        parent.pid = 1
        (tmp_path / "2256" / "stat").unlink()
        assert watch.check() is None
        (tmp_path / "1781" / "stat").unlink()
        assert watch.check() == {"parent_pid": 2256, "grandparent_pid": 1781, "now_parent_pid": 1}
    finally:
        watch.stop()


def test_a_grandparent_that_is_init_leaves_the_parent_as_the_loader(tmp_path: Path):
    _write_stat(tmp_path, 1781, ppid=1, start=620, comm="Decky Loader")
    parent = _Parent(1781)
    watch = LoaderWatch(lambda _observed: None, getppid=parent, proc_root=tmp_path)
    try:
        assert watch.arm()["armed"] is True
        assert watch.check() is None
        parent.pid = 1
        assert watch.check() == {"parent_pid": 1781, "grandparent_pid": None, "now_parent_pid": 1}
    finally:
        watch.stop()


@pytest.mark.parametrize(
    ("ppid", "tree", "reason"),
    [
        (1, False, "no_parent"),
        (2256, False, "parent_unreadable"),
        (2256, True, "grandparent_unreadable"),
    ],
)
def test_an_ancestry_it_cannot_read_arms_nothing(tmp_path: Path, ppid: int, tree: bool, reason: str):
    if tree:
        _write_stat(tmp_path, 2256, ppid=1781, start=691)
    watch = LoaderWatch(lambda _observed: None, getppid=_Parent(ppid), proc_root=tmp_path)
    armed = watch.arm()
    assert armed["armed"] is False and armed["reason"] == reason
    assert watch.check() is None


def test_the_thread_reports_once_and_a_stop_silences_it(tmp_path: Path):
    _forkserver_tree(tmp_path)
    parent = _Parent(2256)
    seen: list[dict[str, object]] = []
    watch = LoaderWatch(seen.append, getppid=parent, proc_root=tmp_path, poll_s=0.01)
    assert watch.arm()["armed"] is True
    parent.pid = 1
    for pid in (2256, 1781, 1598):
        (tmp_path / str(pid) / "stat").unlink()
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert len(seen) == 1

    stopped: list[dict[str, object]] = []
    _forkserver_tree(tmp_path)
    quiet = _Parent(2256)
    other = LoaderWatch(stopped.append, getppid=quiet, proc_root=tmp_path, poll_s=0.01)
    assert other.arm()["armed"] is True
    other.stop()
    quiet.pid = 1
    (tmp_path / "1781" / "stat").unlink()
    time.sleep(0.1)
    assert stopped == []


# A loader that starts the plugin the way Decky does, under a wrapper that exits
# with its child the way the PyInstaller bootloader does.
_LOADER = textwrap.dedent(
    """
    import multiprocessing, os, sys, time
    from pathlib import Path

    def plugin(marker, parent_file):
        from ce_decky.loader_watch import LoaderWatch
        def lost(observed):
            Path(marker).write_text("lost")
            os._exit(0)
        watch = LoaderWatch(lost, poll_s=0.05)
        if not watch.arm()["armed"]:
            Path(marker).write_text("unarmed")
            os._exit(1)
        Path(parent_file).write_text(str(os.getppid()))
        # The main thread spins in Python without ever yielding, the way the
        # plugin's socket reader does at EOF; the watch has to get its turn anyway.
        deadline = time.monotonic() + 60
        spun = 0
        while time.monotonic() < deadline:
            spun += 1

    if __name__ == "__main__":
        context = multiprocessing.get_context(sys.argv[1])
        child = context.Process(target=plugin, args=(sys.argv[2], sys.argv[3]))
        child.start()
        Path(sys.argv[4]).write_text(f"{os.getpid()} {child.pid}")
        time.sleep(60)
    """
)
_BOOTLOADER = "import subprocess, sys; sys.exit(subprocess.call(sys.argv[1:]))"


def _wait_for(path: Path, seconds: float = 15.0) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path.name} never appeared")


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
@pytest.mark.parametrize("method", ["forkserver", "fork"])
def test_a_real_plugin_process_leaves_when_its_loader_is_killed(tmp_path: Path, method: str):
    script = tmp_path / "loader.py"
    script.write_text(_LOADER)
    marker, parent_file, pids_file = tmp_path / "marker", tmp_path / "parent", tmp_path / "pids"
    env = {**os.environ, "PYTHONPATH": str(PY_MODULES)}
    bootloader = subprocess.Popen(
        [sys.executable, "-c", _BOOTLOADER, sys.executable, str(script), method,
         str(marker), str(parent_file), str(pids_file)],
        env=env, start_new_session=True,
    )
    try:
        loader_pid, _plugin_pid = (int(value) for value in _wait_for(pids_file).split())
        parent = int(_wait_for(parent_file))
        assert not marker.exists()
        if method == "forkserver":
            # The forkserver is the parent, and it dying alone is not the loader dying.
            assert parent != loader_pid
            _kill(parent)
            time.sleep(0.5)
            assert not marker.exists()
        else:
            assert parent == loader_pid
        _kill(loader_pid)
        assert _wait_for(marker) == "lost"
        bootloader.wait(timeout=15)
    finally:
        # The whole tree is in the session the bootloader leads, the orphaned
        # plugin included, so the group is what is ended rather than a pid that
        # may already have been reaped and reused.
        try:
            os.killpg(bootloader.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        bootloader.wait(timeout=15)
