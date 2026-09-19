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


@pytest.fixture
def spawned(monkeypatch):
    """Every detached installer this would have started, and how."""
    started: list[dict] = []

    def popen(command, **kwargs):
        started.append({"command": command, **kwargs})
        return object()

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


def test_a_failed_check_is_a_line_on_the_screen_rather_than_an_exception(tmp_path: Path):
    network = FakeNetwork()
    network.failure = ProviderRateLimited("60", "GitHub is rate limiting this device")
    manager = _manager(tmp_path, network)
    snapshot = asyncio.run(manager.check(forced=True))
    assert snapshot["update_available"] is False
    assert "rate limiting" in snapshot["last_error"]
    assert snapshot["checked_at"] is not None


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
    assert manager.snapshot()["update_available"] is False


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
