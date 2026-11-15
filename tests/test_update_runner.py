from hashlib import sha256
from pathlib import Path
import json

import pytest

from ce_decky import update_runner
from ce_decky.decky_control import DeckyWebSocketClosed
from ce_decky.loader_memory import LoaderReading


class _Guard:
    """The loader as a script of readings; the last one repeats."""

    def __init__(self, *readings: LoaderReading) -> None:
        self.readings = list(readings) or [LoaderReading("ok", 1781, 120_000, 120_000)]
        self.reads = 0

    def __call__(self):
        return self

    def read(self) -> LoaderReading:
        self.reads += 1
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


class _Socket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _archive(tmp_path: Path, payload: bytes = b"CE Decky package") -> tuple[Path, str]:
    archive = tmp_path / "CE-Decky-v0.9.28.zip"
    archive.write_bytes(payload)
    return archive, sha256(payload).hexdigest()


def _args(tmp_path: Path, archive: Path, digest: str, **overrides):
    values = {
        "archive": str(archive),
        "digest": digest,
        "version": "0.9.28",
        "result": str(tmp_path / "plugin-update-result.json"),
        "log": str(tmp_path / "logs" / "update.log"),
        "keep_on_failure": str(tmp_path / "home" / "CE-Decky-v0.9.28.zip"),
        "decky_url": "http://127.0.0.1:1337",
        "not_before": 0.0,
    }
    values.update(overrides)
    return update_runner.parse_args([
        f"--{key.replace('_', '-')}={value}" for key, value in values.items() if value is not None
    ])


@pytest.fixture
def loader(monkeypatch):
    """A Decky that accepts the install and then reports the new version."""
    calls: dict[str, object] = {"installed": [], "restarts": 0, "version": "0.9.27"}
    monkeypatch.setattr(update_runner, "auth_token", lambda *_a, **_k: "token")
    monkeypatch.setattr(update_runner.DeckyWebSocket, "connect", classmethod(lambda *_a, **_k: _Socket()))

    def install(_ws, url, version, digest, replace):
        calls["installed"].append((url, version, digest, replace))
        calls["version"] = version

    def matches(_ws, _request_id):
        return [{"name": "CE Decky", "version": calls["version"], "disabled": False}]

    def reload(_ws):
        calls["restarts"] = int(calls["restarts"]) + 1
        return True

    monkeypatch.setattr(update_runner, "install_and_confirm", install)
    monkeypatch.setattr(update_runner, "loader_plugin_matches", matches)
    monkeypatch.setattr(update_runner, "request_frontend_reload", reload)
    monkeypatch.setattr(update_runner, "READBACK_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(update_runner, "READBACK_POLL_SECONDS", 0.01)
    # Never the real /proc: the device running this suite has a live loader.
    monkeypatch.setattr(update_runner, "LoaderMemoryGuard", _Guard())
    return calls


def test_a_successful_install_hands_decky_the_exact_file_restarts_and_reports(tmp_path: Path, loader):
    archive, digest = _archive(tmp_path)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is True
    assert result["error"] is None
    assert result["restart_requested"] is True
    url, version, sent_digest, replace = loader["installed"][0]
    assert url == archive.as_uri()
    assert (version, sent_digest, replace) == ("0.9.28", digest, True)
    # The staged copy goes; nothing is kept in the user's home after a success.
    assert not archive.exists()
    assert not (tmp_path / "home" / "CE-Decky-v0.9.28.zip").exists()
    written = json.loads(Path(str(_args(tmp_path, archive, digest).result)).read_text())
    assert written["ok"] is True and written["version"] == "0.9.28" and written["schema"] == 1
    assert (tmp_path / "logs" / "update.log").read_text().count("update_runner.") >= 2


def test_an_archive_that_changed_since_it_was_verified_installs_nothing(tmp_path: Path, loader):
    archive, digest = _archive(tmp_path)
    archive.write_bytes(b"something else entirely")
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is False
    assert "verified digest" in str(result["error"])
    assert loader["installed"] == []
    # Bytes that are not what the release says they are are not offered for a
    # manual install either, and they do not stay on the device.
    assert result["archive_kept_at"] is None
    assert not archive.exists()
    assert not (tmp_path / "home" / "CE-Decky-v0.9.28.zip").exists()


def test_a_missing_archive_is_reported_rather_than_installed(tmp_path: Path, loader):
    archive, digest = _archive(tmp_path)
    archive.unlink()
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is False
    assert "missing" in str(result["error"])
    assert result["archive_kept_at"] is None


def test_an_install_decky_never_completes_keeps_the_archive_for_a_manual_install(tmp_path: Path, loader, monkeypatch):
    archive, digest = _archive(tmp_path)
    monkeypatch.setattr(update_runner, "loader_plugin_matches", lambda _ws, _id: [
        {"name": "CE Decky", "version": "0.9.27", "disabled": False},
    ])
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is False
    assert "new version" in str(result["error"])
    kept = tmp_path / "home" / "CE-Decky-v0.9.28.zip"
    assert result["archive_kept_at"] == str(kept)
    assert kept.read_bytes() == b"CE Decky package"
    assert not archive.exists()
    assert json.loads((tmp_path / "plugin-update-result.json").read_text())["archive_kept_at"] == str(kept)


def test_a_socket_closed_by_the_install_is_settled_by_the_inventory(tmp_path: Path, loader, monkeypatch):
    archive, digest = _archive(tmp_path)

    def closes(_ws, _url, version, _digest, _replace):
        loader["version"] = version
        raise DeckyWebSocketClosed("Decky closed the socket while replacing the plugin")

    monkeypatch.setattr(update_runner, "install_and_confirm", closes)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is True
    assert result["restart_requested"] is True


def test_a_restart_that_cannot_be_requested_does_not_undo_a_completed_install(tmp_path: Path, loader, monkeypatch):
    archive, digest = _archive(tmp_path)

    def refuses(_ws):
        raise RuntimeError("Decky refused the reload")

    monkeypatch.setattr(update_runner, "request_frontend_reload", refuses)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is True
    assert result["restart_requested"] is False
    assert result["error"] is None


def test_the_install_waits_out_the_webhelper_spacing_it_was_given(tmp_path: Path, loader, monkeypatch):
    archive, digest = _archive(tmp_path)
    slept: list[float] = []
    monkeypatch.setattr(update_runner.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(update_runner.time, "time", lambda: 1000.0)
    update_runner.run(_args(tmp_path, archive, digest, not_before=1030.0))
    assert slept and round(slept[0]) == 30
    slept.clear()
    update_runner.run(_args(tmp_path, archive, digest, not_before=900.0))
    assert not any(value > 1 for value in slept)


def test_a_path_decky_cannot_be_handed_is_refused_with_the_archive_kept(tmp_path: Path, loader):
    # A path that does not survive the round trip through a file URI: Decky
    # would open something else, or nothing, and report neither.
    awkward = tmp_path / "Steam Deck games"
    awkward.mkdir()
    archive, digest = _archive(awkward)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is False
    assert "file URI" in str(result["error"])
    assert loader["installed"] == []
    # It was verified before that refusal, so the manual route still has it.
    assert result["archive_kept_at"] == str(tmp_path / "home" / "CE-Decky-v0.9.28.zip")


def test_the_token_is_never_asked_for_anywhere_but_loopback():
    """The credential that installs plugins does not leave this device.

    The socket that uses the token refused a non-loopback URL from the start.
    The request for the token itself did not, so a caller naming another host
    would have sent the credential there first and been refused afterwards.
    """
    from ce_decky import decky_control

    for url in ("http://example.net:1337", "https://127.0.0.1:1337", "http://decky.example.net:1337"):
        with pytest.raises(ValueError, match="loopback"):
            decky_control.auth_token(url, 1.0)
        with pytest.raises(ValueError, match="loopback"):
            decky_control.DeckyWebSocket.connect(url, "token", 1.0)
    assert decky_control.require_loopback("http://localhost:1337") == ("localhost", 1337)
    assert decky_control.require_loopback(decky_control.DEFAULT_DECKY_URL) == ("127.0.0.1", 1337)


def test_a_failure_after_the_install_was_asked_for_checks_what_landed(tmp_path: Path, loader, monkeypatch):
    """Falling over reading the answer is not the same as installing nothing.

    Decky can accept the request and this process can still fail on the reply.
    Reporting that as a failed update sends the user to install by hand a
    version their device is already running.
    """
    archive, digest = _archive(tmp_path)

    def accepted_then_broken(_ws, _url, version, _digest, _replace):
        loader["version"] = version
        loader["installed"].append(("accepted", version))
        raise RuntimeError("the reply could not be read")

    monkeypatch.setattr(update_runner, "install_and_confirm", accepted_then_broken)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is True
    assert result["error"] is None
    # Nothing is kept for a manual install of a version that is now installed.
    assert result["archive_kept_at"] is None
    assert not archive.exists()
    assert not (tmp_path / "home" / "CE-Decky-v0.9.28.zip").exists()


def test_a_failure_before_the_install_was_asked_for_stays_a_failure(tmp_path: Path, loader, monkeypatch):
    archive, digest = _archive(tmp_path)
    monkeypatch.setattr(update_runner, "auth_token", lambda *_a, **_k: (_ for _ in ()).throw(OSError("Decky is not answering")))
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is False
    assert "not answering" in str(result["error"])
    assert result["archive_kept_at"] == str(tmp_path / "home" / "CE-Decky-v0.9.28.zip")


OK = LoaderReading("ok", 1781, 120_000, 120_000)
RUNAWAY = LoaderReading("runaway", 1781, 120_000 + 3 * 1024 * 1024, 120_000)
EXITED = LoaderReading("exited", 1781, None, 120_000)


def _events(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "logs" / "update.log").read_text().splitlines()]


def test_a_loader_that_runs_away_before_the_install_settles_is_asked_nothing_more(tmp_path: Path, loader, monkeypatch):
    # Before the install, after it, then the first readback poll finds it grown.
    guard = _Guard(OK, OK, RUNAWAY)
    monkeypatch.setattr(update_runner, "LoaderMemoryGuard", guard)
    connects: list[int] = []
    monkeypatch.setattr(
        update_runner.DeckyWebSocket, "connect",
        classmethod(lambda *_a, **_k: connects.append(1) or _Socket()),
    )
    archive, digest = _archive(tmp_path)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is False
    assert "grew by 3.0 GiB of memory while CE Decky was being updated" in result["error"]
    assert "restart it" in result["error"]
    # One connection for the install itself, none for the inventory, none to
    # double check the failure, and no restart.
    assert connects == [1] and loader["restarts"] == 0
    assert result["archive_kept_at"] == str(tmp_path / "home" / "CE-Decky-v0.9.28.zip")
    incident = [event for event in _events(tmp_path) if event["event"] == "update_runner.loader_incident"]
    assert incident and incident[0]["loader_state"] == "runaway" and incident[0]["loader_growth_kib"] == 3 * 1024 * 1024


def test_a_loader_that_exits_after_the_install_settled_keeps_the_success_and_skips_the_restart(tmp_path: Path, loader, monkeypatch):
    # Before, after the install, the readback poll, then gone before the restart.
    monkeypatch.setattr(update_runner, "LoaderMemoryGuard", _Guard(OK, OK, OK, EXITED))
    archive, digest = _archive(tmp_path)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is True and result["error"] is None
    assert result["restart_requested"] is False and loader["restarts"] == 0
    assert not archive.exists() and result["archive_kept_at"] is None
    phases = [event.get("phase") for event in _events(tmp_path) if event["event"] == "update_runner.loader_memory"]
    assert phases == ["before_install", "after_install", "before_restart"]


def test_every_ordinary_install_records_the_loader_before_and_after(tmp_path: Path, loader):
    archive, digest = _archive(tmp_path)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is True
    readings = [event for event in _events(tmp_path) if event["event"] == "update_runner.loader_memory"]
    assert [event["phase"] for event in readings] == ["before_install", "after_install", "before_restart", "after_restart"]
    assert readings[0]["loader_rss_kib"] == 120_000 and readings[0]["loader_growth_kib"] == 0


def test_an_unobserved_loader_gates_nothing(tmp_path: Path, loader, monkeypatch):
    monkeypatch.setattr(update_runner, "LoaderMemoryGuard", _Guard(LoaderReading("unobserved", None, None, None)))
    archive, digest = _archive(tmp_path)
    result = update_runner.run(_args(tmp_path, archive, digest))
    assert result["ok"] is True and loader["restarts"] == 1
