from hashlib import sha256
from pathlib import Path
import asyncio
import json
import logging
import time

import pytest

from ce_decky.network import NetworkResponse, ProviderRateLimited
from ce_decky.paths import PluginPaths
from ce_decky.plugin_update import ARCHIVE_PREFIX, UpdateStateStore
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
    assert options["--keep-on-failure"].endswith("CE-Decky-update-v0.9.28.zip")
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


def test_a_failed_install_reports_where_the_archive_was_left(tmp_path: Path):
    manager = _manager(tmp_path, FakeNetwork())
    kept = str(tmp_path / "home" / "CE-Decky-update-v0.9.28.zip")
    manager.state.update(install={"version": "0.9.28", "started_at": time.time(), "archive_kept_at": kept})
    manager.result_path.write_text(json.dumps({
        "schema": 1, "version": "0.9.28", "ok": False, "error": "Decky refused the install",
        "archive_kept_at": kept, "finished_at": 2.0, "restart_requested": False,
    }), encoding="utf-8")
    manager.consume_runner_result()
    result = manager.snapshot()["last_result"]
    assert result["ok"] is False
    assert result["archive_kept_at"] == kept
    assert "refused" in result["error"]


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
        kept = str(tmp_path / "home" / "CE-Decky-update-v0.9.28.zip")
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
    # And the manual route is where a screen reads it from.
    assert manager.snapshot()["last_result"]["archive_kept_at"].endswith("CE-Decky-update-v0.9.28.zip")


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


def test_a_failed_install_that_kept_the_release_keeps_its_row(tmp_path: Path):
    """That row names a path, and it goes when the path does.

    The manual route is the only thing a failed install leaves a user, and a
    check succeeding a minute later says nothing about whether they have
    installed it yet.
    """
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.27")
    kept = tmp_path / "home" / "CE-Decky-update-v0.9.28.zip"
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"a verified release")
    failed = {
        "attempt": "c" * 32, "version": "0.9.28", "ok": False, "error": "Decky refused the install",
        "archive_kept_at": str(kept), "at": time.time(), "restart_requested": False,
    }
    manager.state.update(last_result=failed)
    assert asyncio.run(manager.check(forced=True))["last_result"]["archive_kept_at"] == str(kept)

    # Installed by hand, or deleted: the row has nothing left to name.
    kept.unlink()
    manager._last_forced_at = 0.0
    assert asyncio.run(manager.check(forced=True))["last_result"] is None

    # And the same file, against a device that is already on that version, is
    # an installer for the past.
    kept.write_bytes(b"a verified release")
    arrived = _manager(tmp_path / "arrived", FakeNetwork(), version="0.9.28")
    arrived.state.update(last_result=failed)
    assert asyncio.run(arrived.check(forced=True))["last_result"] is None


def test_a_successful_install_removes_the_file_an_earlier_failure_left(tmp_path: Path):
    """One file, replaced by the next attempt and removed by a success.

    That is what the storage contract says this is, and it was the half that was
    not done: the archive stayed in the user's home directory after the update
    it was a fallback for had succeeded.
    """
    manager = _manager(tmp_path, FakeNetwork(), version="0.9.28")
    kept = tmp_path / "home" / "CE-Decky-update-v0.9.28.zip"
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

    manager.consume_runner_result()
    assert manager.state.load()["last_result"]["ok"] is True
    assert kept.exists() is False

    # And it removes what it put there, not whatever a path happens to name.
    manager.state.update(last_result={
        "attempt": "f" * 32, "version": "0.9.28", "ok": False, "error": "x",
        "archive_kept_at": str(stranger), "at": time.time(), "restart_requested": False,
    }, install={"attempt": "g" * 32, "version": "0.9.28", "started_at": time.time()})
    manager.consume_runner_result()
    assert stranger.exists() is True
