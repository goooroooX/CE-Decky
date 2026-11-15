from pathlib import Path

from ce_decky.loader_memory import LoaderMemoryGuard, find_loader


def _process(root: Path, pid: int, title: str, *, rss_kib: int, start: int = 620, state: str = "S") -> None:
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cmdline").write_bytes(title.replace(" ", "\x00").encode() + b"\x00")
    fields = [state, "1598"] + ["0"] * 17 + [str(start)] + ["0"] * 10
    (directory / "stat").write_text(f"{pid} (comm) " + " ".join(fields) + "\n")
    (directory / "status").write_text(f"Name:\tx\nVmRSS:\t  {rss_kib} kB\n")


def _device(root: Path) -> None:
    # The titles the device's own process table carries.
    _process(root, 1598, "/home/deck/homebrew/services/PluginLoader", rss_kib=4_000, start=587)
    _process(root, 1781, "Decky Loader v3.3.1 (/home/deck/homebrew/services/PluginLoader)", rss_kib=120_000)
    _process(root, 2256, "/home/deck/homebrew/services/PluginLoader -B -S -I -c import sys; from multiprocessing.forkserver import main", rss_kib=9_000, start=691)
    _process(root, 2336, "CE Decky (/home/deck/homebrew/plugins/CE-Decky/main.py)", rss_kib=90_000, start=700)
    (root / "self").mkdir()


def test_the_loader_is_the_one_process_titled_as_it(tmp_path: Path):
    _device(tmp_path)
    assert find_loader(tmp_path).pid == 1781
    _process(tmp_path, 4000, "Decky Loader v3.3.1 (elsewhere)", rss_kib=1)
    assert find_loader(tmp_path) is None


def test_a_reading_is_ok_until_the_loader_grows_past_the_limit(tmp_path: Path):
    _device(tmp_path)
    guard = LoaderMemoryGuard(tmp_path, growth_limit_kib=1_000_000)
    first = guard.read()
    assert first.state == "ok" and first.as_fields()["loader_growth_kib"] == 0
    _process(tmp_path, 1781, "Decky Loader v3.3.1 (x)", rss_kib=1_119_999)
    assert guard.read().state == "ok"
    _process(tmp_path, 1781, "Decky Loader v3.3.1 (x)", rss_kib=1_120_000)
    runaway = guard.read()
    assert runaway.state == "runaway" and runaway.incident
    assert "grew by 1.0 GiB" in runaway.describe()


def test_a_loader_that_is_gone_or_replaced_has_exited(tmp_path: Path):
    _device(tmp_path)
    guard = LoaderMemoryGuard(tmp_path)
    # Same pid, another process: the start time says so.
    _process(tmp_path, 1781, "Decky Loader v3.3.1 (x)", rss_kib=120_000, start=9_999)
    assert guard.read().state == "exited"
    _process(tmp_path, 1781, "Decky Loader v3.3.1 (x)", rss_kib=120_000, state="Z")
    assert guard.read().state == "exited"
    for name in ("stat", "status", "cmdline"):
        (tmp_path / "1781" / name).unlink()
    exited = guard.read()
    assert exited.state == "exited" and exited.incident
    assert exited.describe() == "Decky's loader exited"


def test_no_loader_at_the_start_is_unobserved_and_never_an_incident(tmp_path: Path):
    guard = LoaderMemoryGuard(tmp_path)
    reading = guard.read()
    assert reading.state == "unobserved" and not reading.incident
