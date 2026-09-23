from hashlib import sha256
from pathlib import Path
import asyncio
import json
import logging
import time

import pytest

from ce_decky.network import NetworkResponse, ProviderRateLimited
from ce_decky.paths import PluginPaths
from ce_decky.plugin_update import ARCHIVE_PREFIX, CHECK_INTERVAL_SECONDS, INSTALL_PENDING_LIMIT_SECONDS, UpdateStateStore
from ce_decky import update_manager
from ce_decky.update_manager import PluginUpdateManager

ARCHIVE_BYTES = b"CE Decky 0.9.28 package"
ARCHIVE_DIGEST = sha256(ARCHIVE_BYTES).hexdigest()


def _release_payload(version: str = "0.9.28") -> dict:
    archive = f"{ARCHIVE_PREFIX}{version}.zip"
    base = f"https://github.com/goooroooX/CE-Decky/releases/download/v{version}"
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": archive, "browser_download_url": f"{base}/{archive}"},
            {"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS"},
        ],
    }


class FakeNetwork:
    """The transport, answering exactly what the release route would answer."""

    def __init__(self, *, release: dict | None = None, sums: bytes | None = None, archive: bytes = ARCHIVE_BYTES):
        self.release = _release_payload() if release is None else release
        self.sums = sums if sums is not None else f"{ARCHIVE_DIGEST}  {ARCHIVE_PREFIX}0.9.28.zip\n".encode()
        self.archive = archive
        self.calls: list[str] = []
        self.status = 200
        self.failure: Exception | None = None

    async def get(self, url, *, allowed_hosts, max_bytes, headers=None, **_kwargs):
        self.calls.append(url)
        if self.failure is not None:
            raise self.failure
        if "api.github.com" in url:
            assert "api.github.com" in allowed_hosts
            return NetworkResponse(url=url, status=self.status, headers={}, body=json.dumps(self.release).encode())
        assert "github.com" in allowed_hosts
        return NetworkResponse(url=url, status=200, headers={}, body=self.sums)

    async def download(self, url, destination: Path, *, allowed_hosts, max_bytes, **_kwargs):
        self.calls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.archive)
        return NetworkResponse(url=url, status=200, headers={}, body=b"")


def _manager(tmp_path: Path, network, *, auto_check=True, searched_at=None, version="0.9.27") -> PluginUpdateManager:
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    return PluginUpdateManager(
        paths, network, logging.getLogger("test-update"),
        current_version=version,
        auto_check=lambda: auto_check,
        last_search_activity=lambda: time.time() if searched_at is None else searched_at,
    )


def _refuses(**_fields):
    """A state store that takes nothing, for the device whose disk has stopped."""
    raise OSError("read-only file system")


class _Clock:
    """Wall time this test moves, for ageing something out without waiting."""

    def __init__(self, base: float) -> None:
        self.base = base
        self.offset = 0.0

    def advance(self, seconds: float) -> None:
        self.offset += seconds

    def __call__(self) -> float:
        return self.base + self.offset


class FakeProcess:
    """The detached installer, as much of it as the manager ever touches.

    It holds it only to notice one that has gone without finishing, so what a
    test needs from it is an exit code that is not there yet and one that is.
    """

    def __init__(self) -> None:
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


@pytest.fixture
def spawned(monkeypatch):
    """Every detached installer this would have started, and how."""
    started: list[dict] = []

    def popen(command, **kwargs):
        process = FakeProcess()
        started.append({"command": command, "process": process, **kwargs})
        return process

    monkeypatch.setattr(update_manager.subprocess, "Popen", popen)
    monkeypatch.setattr(update_manager, "system_interpreter", lambda: "/usr/bin/python3")
    return started


def test_a_check_records_the_offer_and_the_panel_can_read_it(tmp_path: Path):
    manager = _manager(tmp_path, FakeNetwork())
    snapshot = asyncio.run(manager.check(forced=False))
    assert snapshot["latest_version"] == "0.9.28"
    assert snapshot["update_available"] is True
    assert snapshot["last_error"] is None
    assert snapshot["checked_at"] is not None
    # And it is durable, so the panel that mounts after a reload still knows.
    assert UpdateStateStore(manager.state.path).load()["latest_version"] == "0.9.28"


def test_a_device_on_the_newest_release_is_offered_nothing(tmp_path: Path):
    manager = _manager(tmp_path, FakeNetwork(release=_release_payload("0.9.27")))
    snapshot = asyncio.run(manager.check(forced=False))
    assert snapshot["update_available"] is False
    assert snapshot["latest_version"] == "0.9.27"


def test_a_build_ahead_of_the_release_records_what_was_actually_published(tmp_path: Path):
    """A development build is newer than the newest release, and says so.

    The record answers what the newest release is. Answering with the running
    version because there was nothing to install would state something untrue on
    every build between two releases, which is every build this is developed on.
    """
    manager = _manager(tmp_path, FakeNetwork(release=_release_payload("0.9.27")), version="0.9.28")
    snapshot = asyncio.run(manager.check(forced=False))
    assert snapshot["latest_version"] == "0.9.27"
    assert snapshot["current_version"] == "0.9.28"
    assert snapshot["update_available"] is False


def test_checking_is_armed_by_search_activity_and_paced_by_the_interval(tmp_path: Path):
    network = FakeNetwork()
    quiet = _manager(tmp_path / "quiet", network, searched_at=time.time() - update_manager.ACTIVE_WINDOW_SECONDS - 60)
    assert quiet.should_check() is False

    off = _manager(tmp_path / "off", network, auto_check=False)
    assert off.should_check() is False

    armed = _manager(tmp_path / "armed", network)
    assert armed.should_check() is True
    asyncio.run(armed.check(forced=False))
    # One check buys the whole interval; the next tick asks for nothing.
    assert armed.should_check() is False


def test_check_now_ignores_the_arming_but_keeps_its_floor(tmp_path: Path):
    network = FakeNetwork()
    manager = _manager(tmp_path, network, searched_at=0.0)
    assert manager.should_check() is False
    assert asyncio.run(manager.check(forced=True))["latest_version"] == "0.9.28"
    assert len(network.calls) == 1
    # Pressed again straight away, it answers from what it already knows.
    asyncio.run(manager.check(forced=True))
    assert len(network.calls) == 1


def test_a_background_check_does_not_spend_the_floor_a_press_is_owed(tmp_path: Path):
    """The floor is about presses, and a tick is not one.

    Sharing it meant a scheduler tick seconds earlier answered Check now from a
    record whose age the user cannot see, which is the one thing that press
    exists to settle.
    """
    network = FakeNetwork()
    manager = _manager(tmp_path, network)
    asyncio.run(manager.check(forced=False))
    assert len(network.calls) == 1
    asyncio.run(manager.check(forced=True))
    assert len(network.calls) == 2


def test_a_download_that_fails_leaves_nothing_in_the_staging_tree(tmp_path: Path, spawned):
    """The partial file is the caller's problem and the caller never sees it.

    The path only reaches `_run` when the download returns it, so a transfer
    that raises has to take its own file with it or leave a partial archive in
    the managed tree until something else happens to clear it.
    """
    network = FakeNetwork()
    staging = PluginPaths.for_tests(tmp_path).temp_root / "updates"

    async def half_a_download(url, destination: Path, **_kwargs):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"half of an arch")
        raise OSError("the connection went away")

    network.download = half_a_download  # type: ignore[assignment]
    manager = _manager(tmp_path, network)

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    assert list(staging.glob("*")) == []
    assert spawned == []


def test_a_failed_install_names_this_runs_offer_and_not_an_older_one(tmp_path: Path, spawned):
    """What the last update tried is this run's answer, not a stale finding."""
    network = FakeNetwork()
    manager = _manager(tmp_path, network)
    # A check that found something, the way an armed device would have.
    asyncio.run(manager.check(forced=True))
    assert manager.snapshot()["latest_version"] == "0.9.28"
    # The release is pulled before the press lands.
    network.release = _release_payload("0.9.27")

    async def scenario():
        await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)

    asyncio.run(scenario())
    result = manager.snapshot()["last_result"]
    assert result["ok"] is False
    assert result["version"] is None
    assert "already the newest" in result["error"]


def test_a_failed_check_is_a_line_on_the_screen_rather_than_an_exception(tmp_path: Path):
    network = FakeNetwork()
    network.failure = ProviderRateLimited("60", "GitHub is rate limiting this device")
    manager = _manager(tmp_path, network)
    snapshot = asyncio.run(manager.check(forced=True))
    assert snapshot["update_available"] is False
    assert "rate limiting" in snapshot["last_error"]
    # A check that could not be made is not a check: the date a screen shows is
    # the last one that answered, and this one has never answered. What it does
    # leave is the moment it asked, which is what paces the next attempt.
    assert snapshot["checked_at"] is None
    assert manager.state.load()["attempted_at"] is not None
    assert manager.should_check() is False


def test_an_update_downloads_verifies_and_hands_the_exact_file_to_the_installer(tmp_path: Path, spawned):
    manager = _manager(tmp_path, FakeNetwork())

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "installing"
    assert status["version"] == "0.9.28"
    assert status["error"] is None
    command = spawned[0]["command"]
    assert command[:3] == ["/usr/bin/python3", "-m", "ce_decky.update_runner"]
    options = dict(zip(command[3::2], command[4::2]))
    assert options["--digest"] == ARCHIVE_DIGEST
    assert options["--version"] == "0.9.28"
    assert Path(options["--archive"]).read_bytes() == ARCHIVE_BYTES
    assert options["--keep-on-failure"].endswith("CE-Decky-update.zip")
    assert spawned[0]["start_new_session"] is True
    # The record says an install is in flight, for the backend that loads next.
    assert manager.state.load()["install"]["version"] == "0.9.28"


def test_an_archive_that_does_not_match_the_release_checksum_installs_nothing(tmp_path: Path, spawned):
    network = FakeNetwork(archive=b"not the release at all")
    manager = _manager(tmp_path, network)

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    assert "not what this release says" in status["error"]
    assert spawned == []
    staging = PluginPaths.for_tests(tmp_path).temp_root / "updates"
    assert not any(staging.glob("*.zip"))


def test_an_update_is_refused_when_the_release_is_not_newer(tmp_path: Path, spawned):
    manager = _manager(tmp_path, FakeNetwork(release=_release_payload("0.9.27")))

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    assert "already the newest" in status["error"]
    assert spawned == []


def test_a_device_with_no_interpreter_says_so_before_downloading_anything(tmp_path: Path, monkeypatch):
    network = FakeNetwork()
    monkeypatch.setattr(update_manager, "system_interpreter", lambda: None)
    manager = _manager(tmp_path, network)
    with pytest.raises(ValueError, match="manually from Decky"):
        asyncio.run(manager.start())
    assert network.calls == []
    assert manager.snapshot()["install_supported"] is False


def test_an_install_that_has_reached_decky_can_no_longer_be_cancelled(tmp_path: Path, spawned):
    manager = _manager(tmp_path, FakeNetwork())

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        with pytest.raises(ValueError, match="cannot be cancelled"):
            await manager.cancel(started["operation_id"])

    asyncio.run(scenario())


def test_a_second_update_is_refused_while_one_is_still_running(tmp_path: Path, spawned):
    """Two presses are one update, and the second says so rather than racing it."""
    manager = _manager(tmp_path, FakeNetwork())

    async def scenario():
        started = await manager.start()
        with pytest.raises(ValueError, match="already running"):
            await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        # Still refused afterwards: this one has reached Decky and the plugin is
        # being replaced, so there is nothing here for a second one to do.
        with pytest.raises(ValueError, match="already running"):
            await manager.start()
        return manager.status(started["operation_id"])

    assert asyncio.run(scenario())["state"] == "installing"
    assert len(spawned) == 1


def test_the_runners_result_is_folded_into_the_record_and_consumed(tmp_path: Path):
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.28")
    manager.state.update(install={"version": "0.9.28", "started_at": time.time()}, latest_version="0.9.28")
    manager.result_path.write_text(json.dumps({
        "schema": 1, "version": "0.9.28", "ok": True, "error": None,
        "archive_kept_at": None, "finished_at": 1.0, "restart_requested": True,
    }), encoding="utf-8")
    manager.consume_runner_result()
    record = manager.state.load()
    assert record["last_result"]["ok"] is True
    assert record["install"] is None
    assert not manager.result_path.exists()
    snapshot = manager.snapshot()
    assert snapshot["update_available"] is False
    # What the newest release is has not been asked since the install, and the
    # record says that rather than answering with the version now running.
    assert snapshot["latest_version"] is None
    assert snapshot["checked_at"] is None


def _kept(manager: PluginUpdateManager, body: bytes = b"a verified release") -> str:
    """Put a recovery archive on this device and return its digest."""
    path = manager.kept_archive_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return sha256(body).hexdigest()


def test_a_failed_install_reports_where_the_archive_was_left(tmp_path: Path):
    manager = _manager(tmp_path, FakeNetwork())
    digest = _kept(manager)
    manager.state.update(install={
        "attempt": "a" * 32, "version": "0.9.28", "digest": digest,
        "started_at": time.time(), "archive_kept_at": str(manager.kept_archive_path),
    })
    manager.result_path.write_text(json.dumps({
        "schema": 1, "attempt": "a" * 32, "version": "0.9.28", "ok": False,
        "error": "Decky refused the install", "archive_kept_at": str(manager.kept_archive_path),
        "finished_at": 2.0, "restart_requested": False,
    }), encoding="utf-8")
    manager.consume_runner_result()
    snapshot = manager.snapshot()
    assert snapshot["last_result"]["ok"] is False
    assert "refused" in snapshot["last_result"]["error"]
    # The file is offered as its own fact, and only because it is provably the
    # archive this attempt verified.
    assert snapshot["recovery"]["path"] == str(manager.kept_archive_path)
    assert snapshot["recovery"]["version"] == "0.9.28"
    assert snapshot["recovery"]["sha256"] == digest


def test_an_install_that_never_reported_back_is_settled_by_the_running_version(tmp_path: Path):
    # The version now running is the one that was being installed, so it worked
    # whatever the runner managed to write.
    arrived = _manager(tmp_path / "arrived", FakeNetwork(), version="0.9.28")
    arrived.state.update(install={"version": "0.9.28", "started_at": time.time()})
    arrived.consume_runner_result()
    assert arrived.state.load()["last_result"]["ok"] is True

    # Still the old version, and older than any install takes: it did not.
    stalled = _manager(tmp_path / "stalled", FakeNetwork(), version="0.9.27")
    stalled.state.update(install={
        "version": "0.9.28",
        "started_at": time.time() - update_manager.INSTALL_PENDING_LIMIT_SECONDS - 60,
    })
    stalled.consume_runner_result()
    result = stalled.state.load()["last_result"]
    assert result["ok"] is False
    assert "never reported back" in result["error"]

    # Recent and unfinished is neither: it is still happening.
    running = _manager(tmp_path / "running", FakeNetwork(), version="0.9.27")
    running.state.update(install={"version": "0.9.28", "started_at": time.time()})
    running.consume_runner_result()
    assert running.state.load().get("last_result") is None


def test_an_install_that_refused_itself_is_not_reported_as_a_failed_check(tmp_path: Path, spawned):
    """Two different questions, asked separately on the screen.

    A device whose check had just succeeded reported `the last check did not
    finish: this is already the newest release`, which is an install refusing
    itself written into the field the check summary reads.
    """
    manager = _manager(tmp_path, FakeNetwork(release=_release_payload("0.9.27")))

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    snapshot = manager.snapshot()
    assert snapshot["last_error"] is None
    assert snapshot["last_result"]["ok"] is False
    assert "already the newest" in snapshot["last_result"]["error"]
    assert snapshot["last_result"]["archive_kept_at"] is None


def test_the_scheduler_asks_on_its_own_and_is_paced_by_the_interval(tmp_path: Path, monkeypatch):
    """The half of automatic checking that no press is behind.

    A device whose owner is playing rather than searching still owes itself a
    check once its window is open, and it must not spend a request per tick to
    find that out. The timings are the only thing shortened here; what decides
    whether a pass happens is the arming, the interval and the switch.
    """
    network = FakeNetwork()
    monkeypatch.setattr(update_manager, "SCHEDULE_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(update_manager, "SCHEDULE_INTERVAL_SECONDS", 0.01)
    manager = _manager(tmp_path, network)

    async def scenario():
        manager.start_background_checks()
        for _ in range(20):
            await asyncio.sleep(0.01)
        asked_once = len(network.calls)
        # The record is aged past the interval, which is the only thing that
        # makes another pass due. `attempted_at` is what the pacing reads: a
        # check that failed still counts against the interval, while the date a
        # screen shows is the last check that answered.
        aged = time.time() - update_manager.CHECK_INTERVAL_SECONDS - 1
        manager.state.update(checked_at=aged, attempted_at=aged)
        for _ in range(20):
            await asyncio.sleep(0.01)
        asked_again = len(network.calls)
        await manager.close()
        return asked_once, asked_again

    once, again = asyncio.run(scenario())
    assert once == 1, "twenty ticks inside one interval are one request"
    assert again == 2, "the tick after the interval expires asks again"


def test_the_scheduler_asks_for_nothing_while_the_switch_is_off(tmp_path: Path, monkeypatch):
    network = FakeNetwork()
    monkeypatch.setattr(update_manager, "SCHEDULE_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(update_manager, "SCHEDULE_INTERVAL_SECONDS", 0.01)
    manager = _manager(tmp_path, network, auto_check=False)

    async def scenario():
        manager.start_background_checks()
        for _ in range(20):
            await asyncio.sleep(0.01)
        await manager.close()
        return len(network.calls)

    assert asyncio.run(scenario()) == 0


def test_closing_stops_the_scheduler_rather_than_leaving_it_running(tmp_path: Path, monkeypatch):
    """Unload cancels it; a task left behind would outlive the plugin's loop."""
    network = FakeNetwork()
    monkeypatch.setattr(update_manager, "SCHEDULE_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(update_manager, "SCHEDULE_INTERVAL_SECONDS", 0.01)
    manager = _manager(tmp_path, network)

    async def scenario():
        manager.start_background_checks()
        await asyncio.sleep(0.05)
        await manager.close()
        return manager._schedule_task

    task = asyncio.run(scenario())
    assert task is not None and task.done()


def test_a_check_that_reached_github_is_not_failed_by_its_own_record(tmp_path: Path, monkeypatch):
    """The record is a report, and a report that cannot be written is not the
    answer being wrong. The search marker follows the same rule."""
    manager = _manager(tmp_path, FakeNetwork())

    def refuse(**_fields):
        raise OSError("no space left on device")

    monkeypatch.setattr(manager.state, "update", refuse)
    snapshot = asyncio.run(manager.check(forced=True))
    # Nothing was written, so there is nothing to report from the record; the
    # call still succeeded and said so rather than raising at the panel.
    assert snapshot["current_version"] == "0.9.27"
    assert snapshot["checking"] is False


def test_a_search_asks_now_rather_than_at_the_next_tick(tmp_path: Path):
    """What arms the check is a user being here, so the answer is owed now.

    Observed on a Steam Deck: the tick landed twenty-four seconds before the
    search, so the device that had just been searched on waited the whole of the
    next interval before asking, and the panel said nothing about an update that
    was one request away.
    """
    network = FakeNetwork()
    manager = _manager(tmp_path, network)

    async def scenario():
        manager.note_user_activity()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if network.calls:
                break
        await manager.close()

    asyncio.run(scenario())
    assert len(network.calls) == 1
    assert manager.snapshot()["update_available"] is True


def test_a_search_asks_for_nothing_the_rules_already_refuse(tmp_path: Path):
    network = FakeNetwork()
    off = _manager(tmp_path / "off", network, auto_check=False)

    async def scenario(manager):
        manager.note_user_activity()
        for _ in range(10):
            await asyncio.sleep(0.01)
        await manager.close()

    asyncio.run(scenario(off))
    assert network.calls == []

    # And a device that checked minutes ago waits out its interval, however
    # often it is searched on.
    paced = _manager(tmp_path / "paced", network)
    asyncio.run(paced.check(forced=False))
    assert len(network.calls) == 1
    asyncio.run(scenario(paced))
    assert len(network.calls) == 1


def test_a_search_outside_a_loop_is_not_an_error(tmp_path: Path):
    """Probes and tests call the service without one; the offer just declines."""
    manager = _manager(tmp_path, FakeNetwork())
    manager.note_user_activity()


def test_an_installer_that_could_not_start_leaves_no_install_pending(tmp_path: Path, monkeypatch):
    """A spawn that failed is not an install that is still happening.

    The record is written before the installer is started, because it is what
    the next backend reads to know one was in flight at all. When the start
    itself fails there is no such process, and leaving the entry behind had the
    backend that loads next report an install that started and never reported
    back - on a device where nothing was ever installed.
    """
    manager = _manager(tmp_path, FakeNetwork())
    monkeypatch.setattr(update_manager, "system_interpreter", lambda: "/usr/bin/python3")
    manager._interpreter = "/usr/bin/python3"

    def refuse(command, **kwargs):
        raise OSError("no such interpreter")

    monkeypatch.setattr(update_manager.subprocess, "Popen", refuse)

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    record = manager.state.load()
    assert record.get("install") is None
    assert record["last_result"]["ok"] is False

    # And the backend that loads next reads it as nothing rather than as an
    # install that stalled, however long the device sits there.
    later = _manager(tmp_path, FakeNetwork())
    later.consume_runner_result()
    assert later.state.load()["last_result"]["ok"] is False
    assert "never reported back" not in str(later.state.load()["last_result"]["error"])


def test_a_check_recorded_in_this_clocks_future_is_owed_rather_than_skipped(tmp_path: Path):
    """A handheld whose battery ran flat boots behind everything written on it.

    The interval is an age, and a moment in the future has none. Measuring one
    anyway leaves the device refusing to check until wall time catches up with
    a check that has not happened.
    """
    manager = _manager(tmp_path, FakeNetwork())
    manager.state.update(checked_at=time.time() + 30 * 24 * 60 * 60)
    assert manager.should_check() is True

    # An ordinary recent check is still paced, which is what this must not cost.
    manager.state.update(checked_at=time.time())
    assert manager.should_check() is False


def test_closing_from_another_loop_does_not_reach_for_the_one_that_is_gone(tmp_path: Path):
    """Load and unload, each on a loop that is closed before the next runs.

    Deliberately not the device's shape: Decky keeps one event loop for the
    plugin process, as `docs/FIELD_NOTES.md` records. It is every other caller -
    a test, a probe, a developer helper - and what one of them met: the
    scheduler started during load was handed to `gather` by a `close()` running
    somewhere else, which on Python 3.11, the version CI runs and the version
    Decky ships on the device, is `RuntimeError: Event loop is closed` raised
    out of unload itself.
    """
    manager = _manager(tmp_path, FakeNetwork())

    async def load() -> None:
        manager.start_background_checks()

    asyncio.run(load())
    asyncio.run(manager.close())
    assert manager._closing is True


def test_an_installer_that_exits_without_reporting_settles_the_operation(tmp_path: Path, spawned):
    """The install is handed over, the plugin is not replaced, and it stops there.

    Handing the archive over is the moment this process stops being able to
    report anything, and the reason is that Decky is about to stop it. When it
    does not - the loader refuses, the runner cannot start, Python falls over
    before writing anything - this backend is still here, and the operation it
    left behind said `installing` for as long as the plugin stayed loaded:
    every retry was refused as an update already running, and one transient
    failure cost the device its self-update until something reloaded it.
    """
    manager = _manager(tmp_path, FakeNetwork())

    async def scenario():
        started = await manager.start(expected_version="0.9.28")
        await asyncio.gather(manager._task, return_exceptions=True)
        assert manager.status(started["operation_id"])["state"] == "installing"
        assert manager.has_active_operation() is True
        # The installer is gone and left nothing behind.
        spawned[0]["process"].returncode = 1
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    assert "without reporting" in status["error"]
    # And the device can try again, without a reload and without a pending
    # install nobody will ever answer for.
    assert manager.has_active_operation() is False
    record = manager.state.load()
    assert record.get("install") is None
    assert record["last_result"]["ok"] is False


def test_a_failed_result_settles_the_operation_that_started_it(tmp_path: Path, spawned):
    """The runner reported, and the plugin is still here to read it."""
    manager = _manager(tmp_path, FakeNetwork())

    async def scenario():
        started = await manager.start(expected_version="0.9.28")
        await asyncio.gather(manager._task, return_exceptions=True)
        kept = str(tmp_path / "home" / "CE-Decky-update.zip")
        manager.result_path.write_text(json.dumps({
            "schema": 1, "attempt": manager._attempt, "version": "0.9.28", "ok": False,
            "error": "Decky refused the install", "archive_kept_at": kept,
            "finished_at": time.time(), "restart_requested": False,
        }), encoding="utf-8")
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    assert "refused" in status["error"]
    assert manager.has_active_operation() is False
    # Nothing was preserved, so nothing is offered as a manual route.
    assert manager.snapshot()["recovery"] is None


def test_a_result_from_another_attempt_never_settles_this_one(tmp_path: Path):
    """One attempt's outcome is not the next attempt's outcome.

    The result file is written by a process that outlives the backend that
    started it, so one left by an attempt already settled - by the running
    version, by the pending limit, by this backend noticing the installer had
    gone - must not be folded in as what the attempt pending now did.
    """
    manager = _manager(tmp_path, FakeNetwork())
    manager.state.update(install={
        "attempt": "b" * 32, "version": "0.9.28", "started_at": time.time(), "archive_kept_at": None,
    })
    manager.result_path.write_text(json.dumps({
        "schema": 1, "attempt": "a" * 32, "version": "0.9.28", "ok": False,
        "error": "an older attempt failed", "finished_at": 1.0, "restart_requested": False,
    }), encoding="utf-8")

    manager.consume_runner_result()
    record = manager.state.load()
    assert record.get("last_result") is None, "a stale result settles nothing"
    assert record["install"]["attempt"] == "b" * 32, "and leaves the attempt that is pending alone"
    assert manager.result_path.exists() is False, "and is not left to be found again"


def test_the_install_is_refused_when_the_release_is_not_the_one_that_was_confirmed(tmp_path: Path, spawned):
    """The press is consent for a named version, not for whatever is newest.

    The confirmation says which version replaces which, and this call reads the
    newest release again before installing. A release published between the two
    is one nobody has agreed to, so it stops rather than handing Decky an
    archive the user never saw named.
    """
    manager = _manager(tmp_path, FakeNetwork(release=_release_payload("0.9.29")))

    async def scenario():
        started = await manager.start(expected_version="0.9.28")
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    assert "0.9.29" in status["error"] and "0.9.28" in status["error"]
    assert spawned == [], "nothing is downloaded and no installer is started"
    # And what a screen is told to do about it is confirm the one that exists.
    assert manager.snapshot()["latest_version"] == "0.9.29"


def test_a_second_caller_joins_the_check_already_running(tmp_path: Path):
    """A press during a check waits for that check rather than describing it.

    Advanced opens with one copy of this snapshot and is never re-rendered from
    the panel, so a snapshot saying a check is running is what that screen went
    on saying after the check had finished and found something.
    """
    network = FakeNetwork()
    released = asyncio.Event()
    original = network.get

    async def held(url, **kwargs):
        if "api.github.com" in url:
            await released.wait()
        return await original(url, **kwargs)

    network.get = held
    manager = _manager(tmp_path, network)

    async def scenario():
        first = asyncio.create_task(manager.check(forced=False))
        for _ in range(10):
            await asyncio.sleep(0)
        joined = asyncio.create_task(manager.check(forced=True))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not joined.done(), "the second caller waits rather than being handed a running check"
        released.set()
        return await first, await joined

    started, joined = asyncio.run(scenario())
    assert started["latest_version"] == "0.9.28"
    assert joined["checking"] is False
    assert joined["latest_version"] == "0.9.28"
    assert len(network.calls) == 1, "one request answered both callers"


def test_a_check_that_could_not_be_written_down_still_answers_with_what_it_found(tmp_path: Path, monkeypatch):
    """A device that cannot save the answer is not told the previous one.

    The record is a report and a failed write deliberately does not fail the
    check. Rebuilding the reply out of storage afterwards, though, answered the
    press with the state before it.
    """
    manager = _manager(tmp_path, FakeNetwork())
    manager.state.update(checked_at=time.time() - 1, attempted_at=time.time() - 1, latest_version=None)

    def refuse(**_fields):
        raise OSError("read-only file system")

    monkeypatch.setattr(manager.state, "update", refuse)
    snapshot = asyncio.run(manager.check(forced=True))
    assert snapshot["latest_version"] == "0.9.28"
    assert snapshot["update_available"] is True


def test_a_failed_check_does_not_stamp_an_old_finding_with_todays_date(tmp_path: Path):
    """Two questions: what the project has published, and when this last asked.

    A check that failed used to write its moment into the field that says when
    the last check succeeded, so a device that could not reach GitHub at all
    read as one that had confirmed an offer today.
    """
    network = FakeNetwork()
    manager = _manager(tmp_path, network)
    found = asyncio.run(manager.check(forced=True))
    assert found["update_available"] is True
    succeeded_at = found["checked_at"]

    network.failure = ProviderRateLimited("60", "GitHub is rate limiting this device")
    manager._last_forced_at = 0.0
    failed = asyncio.run(manager.check(forced=True))
    assert failed["checked_at"] == succeeded_at, "the date shown is still the last check that answered"
    assert "rate limiting" in failed["last_error"]
    assert failed["latest_version"] == "0.9.28", "and what was found is still what was found"


def test_a_suspended_updater_neither_checks_nor_starts(tmp_path: Path, spawned):
    """Deletion clears the tree this stages into and records into."""
    manager = _manager(tmp_path, FakeNetwork())
    manager.suspend()
    assert manager.should_check() is False
    with pytest.raises(ValueError, match="deleted"):
        asyncio.run(manager.check(forced=True))
    with pytest.raises(ValueError, match="deleted"):
        asyncio.run(manager.start(expected_version="0.9.28"))
    assert spawned == []

    manager.resume()
    assert asyncio.run(manager.check(forced=True))["latest_version"] == "0.9.28"


def test_an_outcome_nobody_can_act_on_is_said_once_and_then_let_go(tmp_path: Path):
    """"The last update did not install" is not a permanent fixture of a screen.

    It was written and never taken back, so a refusal from weeks ago - and a
    success, which is a line worth seeing exactly once - sat under the update
    state until another install replaced it.
    """
    manager = _manager(tmp_path, FakeNetwork())

    # An update that worked: seen once, gone at the next check that answered.
    manager.state.update(last_result={
        "attempt": "a" * 32, "version": "0.9.27", "ok": True, "error": None,
        "archive_kept_at": None, "at": time.time(), "restart_requested": True,
    })
    assert asyncio.run(manager.check(forced=True))["last_result"] is None

    # A refusal that kept nothing: there is no action attached to it either.
    manager._last_forced_at = 0.0
    manager.state.update(last_result={
        "attempt": "b" * 32, "version": None, "ok": False, "error": "this is already the newest release",
        "archive_kept_at": None, "at": time.time(), "restart_requested": False,
    })
    assert asyncio.run(manager.check(forced=True))["last_result"] is None


def test_a_recovery_survives_a_later_attempt_that_verified_nothing(tmp_path: Path):
    """Two facts with two lifetimes, and the shorter one must not take the other.

    A failed install leaves a verified release on the device. A retry that fails
    before it downloads anything - the release moved, the network went - is the
    newer news and replaces what the last update did; the file from before it is
    still there and is still the only thing a user can install by hand.
    """
    manager = _manager(tmp_path, FakeNetwork(release=_release_payload("0.9.27")), version="0.9.27")
    digest = _kept(manager)
    manager.state.update(recovery={
        "attempt": "a" * 32, "version": "0.9.28", "sha256": digest, "path": str(manager.kept_archive_path),
    })

    async def scenario():
        started = await manager.start(expected_version="0.9.28")
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    snapshot = manager.snapshot()
    assert snapshot["last_result"]["ok"] is False, "the newer failure is what the last update did"
    assert snapshot["recovery"]["version"] == "0.9.28", "and the older file is still offered"


def test_a_recovery_is_never_attributed_to_an_attempt_that_did_not_write_it(tmp_path: Path, spawned):
    """One file, successive attempts, so being there says nothing about whose it is.

    An installer for B that exits before preserving anything leaves A's archive
    exactly where it was. Reporting it as B's sends the user into Decky's manual
    installer with bytes that are a real release and not the one the screen
    names.
    """
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.27")
    _kept(manager, b"the release an earlier attempt verified")

    async def scenario():
        started = await manager.start(expected_version="0.9.28")
        await asyncio.gather(manager._task, return_exceptions=True)
        spawned[0]["process"].returncode = 1
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    snapshot = manager.snapshot()
    assert snapshot["recovery"] is None, "a file this attempt cannot prove is not this attempt's"
    assert snapshot["last_result"]["archive_kept_at"] is None


def test_a_recovery_this_attempt_did_write_is_offered_when_the_result_never_arrives(tmp_path: Path, spawned):
    """The case the manual route exists for, and the one it used to lose.

    The installer verifies the archive and moves it before it writes anything
    else, and the two failures are correlated: a disk that cannot take the
    result file is one that has just been written to.
    """
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.27")

    async def scenario():
        started = await manager.start(expected_version="0.9.28")
        await asyncio.gather(manager._task, return_exceptions=True)
        # What the runner would have left: the exact archive it verified.
        _kept(manager, ARCHIVE_BYTES)
        spawned[0]["process"].returncode = 1
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    recovery = manager.snapshot()["recovery"]
    assert recovery["sha256"] == ARCHIVE_DIGEST
    assert recovery["version"] == "0.9.28"


def test_a_recovery_that_has_been_replaced_is_no_longer_the_verified_release(tmp_path: Path):
    """What makes it the release is the digest, and it is re-read to say so."""
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.27")
    digest = _kept(manager)
    manager.state.update(recovery={
        "attempt": "a" * 32, "version": "0.9.28", "sha256": digest, "path": str(manager.kept_archive_path),
    })
    assert manager.snapshot()["recovery"] is not None

    manager.kept_archive_path.write_bytes(b"something else entirely")
    assert manager.snapshot()["recovery"] is None
    manager.kept_archive_path.unlink()
    assert manager.snapshot()["recovery"] is None


def test_a_successful_install_removes_the_file_an_earlier_failure_left(tmp_path: Path):
    """One file, replaced by the next attempt and removed by a success.

    That is what the storage contract says this is, and it was the half that was
    not done: the archive stayed in the user's home directory after the update
    it was a fallback for had succeeded.
    """
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.28")
    kept = tmp_path / "home" / "CE-Decky-update.zip"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"a verified release")
    stranger = tmp_path / "home" / "holiday-photos.zip"
    stranger.write_bytes(b"not ours")
    manager.state.update(
        last_result={
            "attempt": "d" * 32, "version": "0.9.28", "ok": False, "error": "Decky refused the install",
            "archive_kept_at": str(kept), "at": time.time() - 60, "restart_requested": False,
        },
        install={"attempt": "e" * 32, "version": "0.9.28", "started_at": time.time()},
    )

    manager.state.update(recovery={
        "attempt": "d" * 32, "version": "0.9.28", "sha256": sha256(b"a verified release").hexdigest(),
        "path": str(kept),
    })
    manager.consume_runner_result()
    assert manager.state.load()["last_result"]["ok"] is True
    assert kept.exists() is False
    assert manager.state.load()["recovery"] is None, "the record goes with the file"

    # And it removes what it put there, not whatever a path happens to name.
    manager.state.update(last_result={
        "attempt": "f" * 32, "version": "0.9.28", "ok": False, "error": "x",
        "archive_kept_at": str(stranger), "at": time.time(), "restart_requested": False,
    }, install={"attempt": "g" * 32, "version": "0.9.28", "started_at": time.time()})
    manager.consume_runner_result()
    assert stranger.exists() is True


def test_only_a_check_that_answered_lets_go_of_the_last_outcome(tmp_path: Path):
    """A check that could not be made says nothing about an install that failed."""
    network = FakeNetwork()
    network.failure = ProviderRateLimited("60", "GitHub is rate limiting this device")
    manager = _manager(tmp_path, network)
    spent = {
        "attempt": "h" * 32, "version": "0.9.28", "ok": False, "error": "Decky refused the install",
        "archive_kept_at": None, "at": time.time(), "restart_requested": False,
    }
    manager.state.update(last_result=spent)
    snapshot = asyncio.run(manager.check(forced=True))
    assert snapshot["last_result"] == spent
    assert "rate limiting" in snapshot["last_error"]


def test_the_file_removed_by_a_success_is_the_one_this_wrote(tmp_path: Path):
    """A path in the record is what this is about to delete, so it is checked.

    Both halves: the name this gives the file it keeps, and the one directory it
    ever writes it to.
    """
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.28")
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir(parents=True, exist_ok=True)
    impostor = elsewhere / "CE-Decky-update.zip"
    impostor.write_bytes(b"not the copy this put anywhere")
    manager.state.update(
        last_result={
            "attempt": "i" * 32, "version": "0.9.28", "ok": False, "error": "Decky refused the install",
            "archive_kept_at": str(impostor), "at": time.time(), "restart_requested": False,
        },
        install={"attempt": "j" * 32, "version": "0.9.28", "started_at": time.time()},
    )
    manager.consume_runner_result()
    assert impostor.exists() is True


def _pending(tmp_path: Path, **fields) -> PluginUpdateManager:
    """A backend loading with an installer already out there, as the record says."""
    manager = _manager(tmp_path, FakeNetwork(), version=fields.pop("version", "0.9.27"))
    manager.state.update(install={
        "attempt": "k" * 32, "operation_id": "o" * 32, "version": "0.9.28",
        "started_at": time.time(), "archive_kept_at": str(manager.kept_archive_path),
        **fields,
    })
    return manager


def test_an_install_that_outlived_its_backend_is_still_running(tmp_path: Path, spawned):
    """The installer is detached on purpose, so a reload does not end it.

    Nothing in the new process is its parent and nothing holds its operation, so
    without the record it read as a device with no update in flight: a second
    privileged install could start beside the first, and deleting plugin data
    was allowed underneath it.
    """
    manager = _pending(tmp_path)
    manager.consume_runner_result()

    assert manager.has_active_operation() is True
    with pytest.raises(ValueError, match="already running"):
        asyncio.run(manager.start(expected_version="0.9.28"))
    assert spawned == [], "and nothing is downloaded or started beside it"
    # It is the same install, by the identity the record carries.
    assert manager._operation["operation_id"] == "o" * 32
    assert manager._attempt == "k" * 32


def test_an_adopted_install_is_settled_by_the_result_that_arrives_later(tmp_path: Path):
    manager = _pending(tmp_path)
    manager.consume_runner_result()
    assert manager.has_active_operation() is True

    manager.result_path.write_text(json.dumps({
        "schema": 1, "attempt": "k" * 32, "version": "0.9.28", "ok": False,
        "error": "Decky refused the install", "archive_kept_at": None,
        "finished_at": time.time(), "restart_requested": False,
    }), encoding="utf-8")
    assert manager.has_active_operation() is False
    assert manager.state.load()["last_result"]["error"] == "Decky refused the install"


def test_an_install_older_than_any_install_stops_being_authoritative(tmp_path: Path):
    """Including one whose record says nothing about when it started."""
    stale = _pending(tmp_path / "stale", started_at=time.time() - INSTALL_PENDING_LIMIT_SECONDS - 60)
    stale.consume_runner_result()
    assert stale.has_active_operation() is False
    assert "never reported back" in stale.state.load()["last_result"]["error"]

    nameless = _pending(tmp_path / "nameless", started_at="whenever")
    nameless.consume_runner_result()
    assert nameless.has_active_operation() is False


def test_an_install_recorded_in_this_clocks_future_still_ages_out(tmp_path: Path):
    """A handheld that boots behind its own state must not be stuck for ever.

    Measuring a negative age as zero on every read looks like an answer and is
    not one: nothing accumulates, so the attempt stays young for as many days as
    the clock is behind and the device refuses every update and every deletion
    for all of them. The first read that sees a moment in the future writes down
    when it saw it, and the limit runs from there.
    """
    future = time.time() + 30 * 24 * 3600
    manager = _pending(tmp_path, started_at=future)
    manager.consume_runner_result()
    # Adopted, because as far as this clock can tell it has just begun.
    assert manager.has_active_operation() is True
    observed = manager.state.load()["install"]["started_at"]
    assert observed < future, "the moment it was noticed replaces the one it could not measure"

    # Reading it again does not move that origin.
    manager.snapshot()
    assert manager.state.load()["install"]["started_at"] == observed

    # And the limit runs from it, so the attempt does expire.
    manager.state.update(install={**manager.state.load()["install"], "started_at": observed - INSTALL_PENDING_LIMIT_SECONDS - 60})
    manager._operation = None
    manager.consume_runner_result()
    assert manager.has_active_operation() is False
    assert "never reported back" in manager.state.load()["last_result"]["error"]


def test_a_future_start_this_device_cannot_write_down_is_still_aged(tmp_path: Path, monkeypatch):
    """A read-only disk does not give the device its clock back.

    The origin is written so that the next backend measures from the same place;
    when it cannot be written, this backend still has to be able to age the
    attempt, which is what its own memory of the record is for.
    """
    manager = _pending(tmp_path, started_at=time.time() + 30 * 24 * 3600)

    def refuse(**_fields):
        raise OSError("read-only file system")

    monkeypatch.setattr(manager.state, "update", refuse)
    manager.consume_runner_result()
    assert manager.has_active_operation() is True
    observed = manager._stored()["install"]["started_at"]
    assert observed <= time.time()


def test_a_check_that_cannot_be_written_down_is_still_what_this_backend_knows(tmp_path: Path, monkeypatch):
    """A read-only disk does not make a backend forget what it just read.

    The one caller holding the answer used to be the only one who had it: every
    later status read went back to the file, the pacing went back to the file,
    and the next search asked GitHub all over again.
    """
    manager = _manager(tmp_path, FakeNetwork())
    manager.state.update(checked_at=time.time() - CHECK_INTERVAL_SECONDS - 1, attempted_at=time.time() - CHECK_INTERVAL_SECONDS - 1)

    def refuse(**_fields):
        raise OSError("read-only file system")

    monkeypatch.setattr(manager.state, "update", refuse)
    answered = asyncio.run(manager.check(forced=True))
    assert answered["latest_version"] == "0.9.28"
    # Every later reader agrees with the caller that asked.
    assert manager.snapshot()["latest_version"] == "0.9.28"
    assert manager.snapshot()["update_available"] is True
    # And the pacing counts it, so a search does not spend the budget again.
    assert manager.should_check() is False


def test_a_failure_that_cannot_be_written_down_is_not_forgotten_either(tmp_path: Path, monkeypatch):
    network = FakeNetwork()
    network.failure = ProviderRateLimited("60", "GitHub is rate limiting this device")
    manager = _manager(tmp_path, network)

    def refuse(**_fields):
        raise OSError("read-only file system")

    monkeypatch.setattr(manager.state, "update", refuse)
    asyncio.run(manager.check(forced=True))
    assert "rate limiting" in manager.snapshot()["last_error"]
    assert manager.should_check() is False, "the attempt still paces the next one"


def test_a_second_press_joins_the_press_already_running(tmp_path: Path):
    """The floor stops a second request, not a second caller hearing the answer.

    The floor was read first, so a second Check now inside a minute - a slow
    one, a panel closed and opened over it - was handed the snapshot of a check
    still running and never heard how it ended, which is the state the join
    exists to remove.
    """
    network = FakeNetwork()
    released = asyncio.Event()
    original = network.get

    async def held(url, **kwargs):
        if "api.github.com" in url:
            await released.wait()
        return await original(url, **kwargs)

    network.get = held
    manager = _manager(tmp_path, network)

    async def scenario():
        first = asyncio.create_task(manager.check(forced=True))
        for _ in range(10):
            await asyncio.sleep(0)
        second = asyncio.create_task(manager.check(forced=True))
        for _ in range(10):
            await asyncio.sleep(0)
        assert not second.done(), "a press during a press waits for it"
        released.set()
        return await first, await second

    one, two = asyncio.run(scenario())
    assert one["checking"] is False and two["checking"] is False
    assert one["latest_version"] == two["latest_version"] == "0.9.28"
    assert len(network.calls) == 1, "and the floor still allowed only one request"


def test_an_adopted_install_that_has_run_out_of_time_stops_being_reported(tmp_path: Path):
    """Adoption must not leave a panel reporting an install for ever.

    The status call projects rather than writes, so what it stops saying is said
    by what it can see: an attempt older than any install is not adopted, and
    the first boundary that may write settles it durably.
    """
    manager = _pending(tmp_path)
    manager.consume_runner_result()
    assert manager.snapshot()["operation"]["state"] == "installing"

    record = manager.state.load()
    manager.state.update(install={
        **record["install"], "started_at": time.time() - INSTALL_PENDING_LIMIT_SECONDS - 60,
    })
    manager._operation = None
    assert manager.snapshot()["operation"] is None, "a read reports it as over"
    assert manager.state.load()["install"] is not None, "and changes nothing while doing so"

    # The boundary that may write is what closes it.
    assert manager.has_active_operation() is False
    assert "never reported back" in manager.state.load()["last_result"]["error"]
    assert manager.state.load()["install"] is None


def test_a_future_start_on_a_disk_that_takes_nothing_still_ages_out(tmp_path: Path, monkeypatch):
    """The fallback has to survive being read again, which is all it ever is.

    Reconciling read the file rather than what this backend had worked out, so
    the moment a future start time was first noticed was written down in memory
    and then thrown away and taken again on the next status read. The attempt
    was newly begun for ever, and the device went on refusing every update and
    every deletion.
    """
    manager = _pending(tmp_path, started_at=time.time() + 30 * 24 * 3600)
    monkeypatch.setattr(manager.state, "update", _refuses)

    manager.consume_runner_result()
    first = manager._stored()["install"]["started_at"]
    assert manager.has_active_operation() is True

    # Read again, and again: the origin is where it was.
    clock = _Clock(base=time.time())
    monkeypatch.setattr(update_manager.time, "time", clock)
    manager.snapshot()
    manager.has_active_operation()
    assert manager._stored()["install"]["started_at"] == first

    # And the limit runs from it, on a disk that is still taking nothing.
    clock.advance(INSTALL_PENDING_LIMIT_SECONDS + 60)
    assert manager.has_active_operation() is False
    assert "never reported back" in manager._stored()["last_result"]["error"]
    assert manager._stored().get("install") is None
    assert manager.snapshot()["operation"]["state"] == "failed"

    # A new update may still fail on a disk that takes nothing - it has to write
    # down that an installer is being started before it starts one - but it must
    # not fail because this device believes the old one is still running.
    async def retry():
        started = await manager.start(expected_version="0.9.28")
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(retry())
    assert status["state"] == "failed", "the disk is what stops it"
    assert "already running" not in str(status["error"]), "and not this device's own memory"


def test_a_recovery_for_a_version_this_device_already_runs_is_retired(tmp_path: Path):
    """The manual route works by this plugin being replaced without it noticing.

    Nothing in this transaction runs when a user installs the saved release
    through Decky themselves: the plugin is replaced and the next backend simply
    starts at that version. Offering them an installer for what they are already
    running is the one outcome the fallback must not end in.
    """
    holding = _manager(tmp_path / "holding", FakeNetwork(), version="0.9.27")
    digest = _kept(holding)
    recovery = {"attempt": "a" * 32, "version": "0.9.28", "sha256": digest, "path": str(holding.kept_archive_path)}
    holding.state.update(recovery=recovery)
    assert holding.snapshot()["recovery"]["version"] == "0.9.28"
    assert holding.kept_archive_path.is_file()

    for version in ("0.9.28", "0.9.29"):
        arrived = _manager(tmp_path / f"arrived-{version}", FakeNetwork(), version=version)
        _kept(arrived)
        arrived.state.update(recovery={**recovery, "path": str(arrived.kept_archive_path)})
        # A read stops offering it and leaves the device exactly as it was.
        assert arrived.snapshot()["recovery"] is None
        assert arrived.kept_archive_path.is_file()
        # The boundary that may write is what removes it, file and record.
        arrived.maintain()
        assert arrived.kept_archive_path.exists() is False
        assert arrived.state.load()["recovery"] is None

    # A record this cannot read deletes nothing and offers nothing.
    unreadable = _manager(tmp_path / "unreadable", FakeNetwork(), version="0.9.28")
    _kept(unreadable)
    unreadable.state.update(recovery={**recovery, "version": None, "path": str(unreadable.kept_archive_path)})
    assert unreadable.snapshot()["recovery"] is None
    unreadable.maintain()
    assert unreadable.kept_archive_path.is_file(), "nothing is deleted on a record this cannot read"


def test_an_install_waits_at_its_boundary_until_its_owner_admits_it(tmp_path: Path, spawned, monkeypatch):
    """The backend is replaced once `installing` is published, so its owner decides when.

    A Cheat Engine stop that has not yet written down what it left exists only
    in this process: the update waits for it, still cancellable, and never
    starts the installer before the owner has published the install.
    """
    monkeypatch.setattr(update_manager, "INSTALL_ADMISSION_POLL_SECONDS", 0.01)
    answers = iter([False, False, True])
    seen: list[str] = []
    manager = _manager(tmp_path, FakeNetwork())

    def admit(commit):
        seen.append(str(manager._operation["state"]))
        if not next(answers):
            return False
        commit()
        return True
    manager._admit_install = admit

    async def scenario():
        started = await manager.start()
        assert manager.replacement_committed() is False
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    assert asyncio.run(scenario())["state"] == "installing"
    assert seen == ["downloading"] * 3
    assert len(spawned) == 1
    assert manager.replacement_committed() is True


def test_an_install_its_owner_never_admits_is_not_installed(tmp_path: Path, spawned, monkeypatch):
    monkeypatch.setattr(update_manager, "INSTALL_ADMISSION_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(update_manager, "INSTALL_ADMISSION_POLL_SECONDS", 0.01)
    manager = _manager(tmp_path, FakeNetwork())
    manager._admit_install = lambda commit: False

    async def scenario():
        started = await manager.start()
        await asyncio.gather(manager._task, return_exceptions=True)
        return manager.status(started["operation_id"])

    status = asyncio.run(scenario())
    assert status["state"] == "failed"
    assert "still being stopped" in status["error"]
    assert spawned == []
    assert manager.replacement_committed() is False
