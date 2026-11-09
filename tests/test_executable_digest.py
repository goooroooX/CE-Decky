import hashlib
import os
from pathlib import Path
import threading
import time

import pytest

from ce_decky.atomic import file_identity, sha256_regular_file
from ce_decky.executable_digest import ExecutableDigests, regular_file_identity


def _exe(tmp_path: Path, data: bytes = b"MZ" + b"\x90" * 5000, name: str = "game.exe") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# -- the reader -----------------------------------------------------------------


def test_the_reader_streams_the_exact_digest_with_the_identity_it_read(tmp_path):
    path = _exe(tmp_path)
    digest, info = sha256_regular_file(path, max_bytes=1 << 20, chunk_bytes=7)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert file_identity(info) == regular_file_identity(path)


@pytest.mark.parametrize("make", ["symlink", "directory", "empty", "missing", "oversize"])
def test_the_reader_refuses_anything_but_a_bounded_regular_file(tmp_path, make):
    real = _exe(tmp_path, name="real.exe")
    path = tmp_path / "game.exe"
    if make == "symlink":
        path.symlink_to(real)
    elif make == "directory":
        path.mkdir()
    elif make == "empty":
        path.write_bytes(b"")
    elif make == "oversize":
        path.write_bytes(b"x" * 100)
    with pytest.raises(ValueError):
        sha256_regular_file(path, max_bytes=99 if make == "oversize" else 1 << 20)


def test_the_reader_refuses_a_file_that_is_not_the_identity_it_was_asked_for(tmp_path):
    path = _exe(tmp_path)
    before = regular_file_identity(path)
    replacement = _exe(tmp_path, b"MZ" + b"\x00" * 5000, name="new.exe")
    os.replace(replacement, path)
    with pytest.raises(ValueError, match="changed before reading"):
        sha256_regular_file(path, max_bytes=1 << 20, expected_identity=before)


def test_the_reader_refuses_a_file_rewritten_while_it_reads(tmp_path):
    path = _exe(tmp_path)
    calls = []

    def rewrite():
        calls.append(1)
        if len(calls) == 2:
            with path.open("r+b") as handle:
                handle.write(b"ZM")
        return False

    with pytest.raises(ValueError, match="changed while reading"):
        sha256_regular_file(path, max_bytes=1 << 20, should_stop=rewrite, chunk_bytes=1000)


def test_a_same_size_rewrite_with_its_time_put_back_is_still_a_new_identity(tmp_path):
    path = _exe(tmp_path)
    before = os.stat(path)
    time.sleep(0.01)
    path.write_bytes(b"MZ" + b"\x00" * 5000)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
    assert file_identity(after) != file_identity(before)


def test_the_reader_stops_between_chunks_when_asked(tmp_path):
    path = _exe(tmp_path)
    with pytest.raises(ValueError, match="stopped"):
        sha256_regular_file(path, max_bytes=1 << 20, should_stop=lambda: True)


# -- the cache and its worker ---------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _settle(digests, path, identity, bound=5.0):
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        answer = digests.lookup(path, identity)
        if answer.state != "pending":
            return answer
        time.sleep(0.005)
    raise AssertionError("the digest never settled")


def test_a_lookup_never_reads_and_one_identity_is_read_once(tmp_path):
    path = _exe(tmp_path)
    identity = regular_file_identity(path)
    gate = threading.Event()
    reads = []

    def reader(target, **kwargs):
        reads.append(target)
        gate.wait(5)
        return sha256_regular_file(target, **kwargs)

    digests = ExecutableDigests(None, reader=reader)
    # Two callers asking while the read is still running share it.
    assert digests.lookup(str(path), identity).state == "pending"
    assert digests.lookup(str(path), identity).state == "pending"
    gate.set()
    answer = _settle(digests, str(path), identity)
    assert answer.state == "ready" and answer.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    # Asked again with nothing changed: answered from memory.
    assert digests.lookup(str(path), identity).digest == answer.digest
    assert len(reads) == 1


def test_a_changed_file_is_a_new_read_and_never_the_old_digest(tmp_path):
    path = _exe(tmp_path)
    digests = ExecutableDigests(None)
    first = _settle(digests, str(path), regular_file_identity(path))
    path.write_bytes(b"MZ" + b"\x01" * 6000)
    second = _settle(digests, str(path), regular_file_identity(path))
    assert first.digest != second.digest == hashlib.sha256(path.read_bytes()).hexdigest()
    # The identity the first answer was read for is still answered by it, and
    # only by it.
    assert digests.lookup(str(path), None).state == "unavailable"


def test_a_failed_read_is_retried_on_a_bounded_schedule_and_then_left_alone(tmp_path):
    path = _exe(tmp_path)
    identity = regular_file_identity(path)
    clock = _Clock()
    reads = []

    def failing(target, **kwargs):
        reads.append(target)
        raise OSError("device went away")

    digests = ExecutableDigests(None, reader=failing, clock=clock)
    assert _settle(digests, str(path), identity).state == "unavailable"
    # Every poll in between answers from the failure without reading.
    for _ in range(5):
        assert digests.lookup(str(path), identity).state == "unavailable"
    assert len(reads) == 1
    clock.now += 31
    assert _settle(digests, str(path), identity).state == "unavailable"
    assert len(reads) == 2
    clock.now += 301
    assert _settle(digests, str(path), identity).state == "unavailable"
    assert len(reads) == 3
    clock.now += 100000
    assert digests.lookup(str(path), identity).state == "unavailable"
    assert len(reads) == 3


def test_an_oversized_executable_is_unknown_and_never_read_again(tmp_path):
    path = _exe(tmp_path)
    identity = regular_file_identity(path)
    clock = _Clock()
    digests = ExecutableDigests(None, clock=clock, max_file_bytes=10)
    answer = _settle(digests, str(path), identity)
    assert answer.state == "unavailable" and "bound" in answer.reason
    clock.now += 100000
    assert digests.lookup(str(path), identity).state == "unavailable"


def test_a_full_queue_answers_unavailable_without_reading(tmp_path):
    gate = threading.Event()

    def reader(target, **kwargs):
        gate.wait(5)
        return sha256_regular_file(target, **kwargs)

    digests = ExecutableDigests(None, reader=reader, max_queued=1)
    paths = [_exe(tmp_path, name=f"g{index}.exe") for index in range(3)]
    states = []
    for path in paths:
        states.append(digests.lookup(str(path), regular_file_identity(path)).state)
        time.sleep(0.05)
    # One running, one waiting, and the third turned away.
    assert states == ["pending", "pending", "unavailable"]
    gate.set()
    assert _settle(digests, str(paths[1]), regular_file_identity(paths[1])).state == "ready"


def test_a_thread_that_cannot_start_is_an_unknown_rather_than_a_failed_status(tmp_path, monkeypatch):
    path = _exe(tmp_path)
    identity = regular_file_identity(path)
    digests = ExecutableDigests(None)

    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    answer = digests.lookup(str(path), identity)
    assert answer.state == "unavailable" and "worker" in answer.reason
    assert not digests._queue and digests._worker is None
    monkeypatch.undo()
    assert _settle(digests, str(path), identity).state == "ready"


def test_results_are_bounded(tmp_path):
    digests = ExecutableDigests(None, max_results=2)
    paths = [_exe(tmp_path, name=f"g{index}.exe") for index in range(3)]
    for path in paths:
        _settle(digests, str(path), regular_file_identity(path))
    assert len(digests._results) == 2


def test_close_stops_the_read_and_refuses_new_work(tmp_path):
    path = _exe(tmp_path, b"MZ" + b"\x90" * 50000)
    started = threading.Event()

    def reader(target, **kwargs):
        stop = kwargs["should_stop"]

        def slow():
            started.set()
            time.sleep(0.01)
            return stop()

        kwargs["should_stop"] = slow
        kwargs["chunk_bytes"] = 1000
        return sha256_regular_file(target, **kwargs)

    digests = ExecutableDigests(None, reader=reader)
    identity = regular_file_identity(path)
    assert digests.lookup(str(path), identity).state == "pending"
    assert started.wait(5)
    worker = digests._worker
    digests.close()
    worker.join(5)
    assert not worker.is_alive()
    assert digests.lookup(str(path), identity).state == "unavailable"
    assert digests._results == {}


# -- the running game -------------------------------------------------------------


def _proc(tmp_path: Path, exe: Path, *, pid=4242, start_ticks=None, mapped=True):
    """A `/proc` holding one process that maps `exe`, started now unless told otherwise."""
    root = tmp_path / "proc"
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    hz = os.sysconf("SC_CLK_TCK")
    uptime = 10000.0
    (root / "uptime").write_text(f"{uptime:.2f} 1.00\n", encoding="ascii")
    if start_ticks is None:
        start_ticks = int(uptime * hz)
    (root / str(pid) / "stat").write_text(
        f"{pid} (game.exe) S " + " ".join(["0"] * 18) + f" {start_ticks} 0 0\n", encoding="ascii")
    info = os.stat(exe)
    inode = info.st_ino if mapped else info.st_ino + 1
    line = f"140000000-140001000 r--p 00000000 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x} {inode}"
    (root / str(pid) / "maps").write_text(
        f"00400000-00401000 r-xp 00000000 00:00 0\n{line}                    {exe}\n", encoding="ascii")
    return root


def test_a_running_game_is_its_file_only_when_it_maps_that_inode_and_started_after_it(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    proc = _proc(tmp_path, exe)
    # ctime cannot be set back, so the process is moved forward instead.
    monkeypatch.setattr("ce_decky.executable_digest._booted_at", lambda root: time.time() - 10000.0 + 5)
    digests = ExecutableDigests(None, proc_root=proc)
    identity, reason = digests.running_identity(str(exe), (4242,))
    assert reason is None and identity == regular_file_identity(exe)
    # Asked again with nothing changed: answered without reading the map.
    (proc / "4242" / "maps").unlink()
    assert digests.running_identity(str(exe), (4242,)) == (identity, None)


def test_a_file_changed_after_the_game_started_is_not_what_the_game_runs(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    proc = _proc(tmp_path, exe)
    # The process started a minute ago, and the file changed just now.
    monkeypatch.setattr("ce_decky.executable_digest._booted_at", lambda root: time.time() - 10000.0 - 60)
    identity, reason = ExecutableDigests(None, proc_root=proc).running_identity(str(exe), (4242,))
    assert identity is None and "changed after the game started" in reason


def test_a_game_that_maps_another_inode_is_not_this_file(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    proc = _proc(tmp_path, exe, mapped=False)
    monkeypatch.setattr("ce_decky.executable_digest._booted_at", lambda root: time.time() - 10000.0 + 5)
    identity, reason = ExecutableDigests(None, proc_root=proc).running_identity(str(exe), (4242,))
    assert identity is None and "does not map" in reason


def test_a_replaced_or_unreadable_process_proves_nothing(tmp_path, monkeypatch):
    exe = _exe(tmp_path)
    proc = _proc(tmp_path, exe)
    monkeypatch.setattr("ce_decky.executable_digest._booted_at", lambda root: time.time() - 10000.0 + 5)
    digests = ExecutableDigests(None, proc_root=proc)
    assert digests.running_identity(str(exe), ())[0] is None
    assert digests.running_identity(str(exe), (999,))[0] is None
    # The same PID started again is a different process.
    starts = iter([1000000, 1000001])
    monkeypatch.setattr("ce_decky.executable_digest._process_start_time", lambda pid, root: next(starts))
    identity, reason = digests.running_identity(str(exe), (4242,))
    assert identity is None and "replaced" in reason
