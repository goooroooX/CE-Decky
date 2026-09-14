"""What the plugin log has to say for a support bundle to be worth reading.

Each case names one question a bug report actually arrives with, and asserts
that the log answers it. The failure mode being guarded against is not a crash:
it is a log that records that something happened without recording what.
"""

from __future__ import annotations

from pathlib import Path
import logging
import struct

from ce_decky.paths import PluginPaths
from ce_decky.service import PluginService


def _write_fake_pe(path: Path) -> None:
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    path.write_bytes(data)


def _service(tmp_path: Path) -> PluginService:
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("support-logging"))
    service.initialize()
    return service


def _import_table(service: PluginService, tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source.CT"
    source.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>Health</Description>
      <VariableType>4 Bytes</VariableType><Address>game.exe+1234</Address></CheatEntry>
  </CheatEntries>
</CheatTable>
""",
        encoding="utf-8",
    )
    return service.import_table(str(source))


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_the_log_says_what_a_table_parsed_as_not_only_that_it_was_inspected(tmp_path: Path, caplog):
    """A report that a table shows no cheats needs the parse result, not the digest."""
    service = _service(tmp_path)
    table = _import_table(service, tmp_path)

    with caplog.at_level(logging.INFO, logger="support-logging"):
        service.inspect_table_sha(str(table["sha256"]))
        # The panel re-inspects on every context refresh. One line per table.
        service.inspect_table_sha(str(table["sha256"]))

    inspected = [line for line in _messages(caplog) if "event=table.inspected" in line]
    assert len(inspected) == 1
    assert "entries=1" in inspected[0]
    assert "controls=1" in inspected[0]
    assert "actionable=1" in inspected[0]
    assert "process_hints=game.exe" in inspected[0]
    assert "table_format=45" in inspected[0]


def test_the_log_says_what_was_saved_for_a_game_not_only_that_a_profile_was_saved(tmp_path: Path, caplog):
    service = _service(tmp_path)
    table = _import_table(service, tmp_path)
    digest = str(table["sha256"])

    with caplog.at_level(logging.INFO, logger="support-logging"):
        service.save_profile(10, "Game", False, digest, "game.exe")
        service.set_execution_consent(10, digest, True)
        service.set_autoload(10, digest, True)

    messages = _messages(caplog)
    saved = next(line for line in messages if "event=profile.saved" in line)
    assert "app_id=10" in saved
    assert f"table_sha={digest[:12]}" in saved
    assert "target=game.exe" in saved
    assert "changed_table=true" in saved
    consent = next(line for line in messages if "event=profile.consent_set" in line)
    assert "consent=true" in consent
    autoload = next(line for line in messages if "event=profile.autoload_set" in line)
    assert "enabled=true" in autoload


def test_runtime_state_is_logged_when_it_changes_and_stays_quiet_when_it_does_not(tmp_path: Path, caplog):
    """The panel polls this constantly; the log must carry the transitions only."""
    service = _service(tmp_path)

    with caplog.at_level(logging.INFO, logger="support-logging"):
        # No session at all is a state worth recording once: it is what a game
        # that will not start Cheat Engine looks like from here.
        service.get_runtime_status(10)
        service.get_runtime_status(10)

    changes = [line for line in _messages(caplog) if "event=runtime.state_changed" in line]
    assert len(changes) == 1
    assert "app_id=10" in changes[0]
    assert "connected=false" in changes[0]
    assert "stale_reason=no prepared session exists for AppID" in changes[0]


def test_a_deleted_profile_stops_suppressing_the_next_runtime_line_for_its_appid(tmp_path: Path, caplog):
    service = _service(tmp_path)
    service.save_profile(10, "Game", False, None, None)

    with caplog.at_level(logging.INFO, logger="support-logging"):
        service.get_runtime_status(10)
        service.delete_profile(10)
        service.get_runtime_status(10)

    changes = [line for line in _messages(caplog) if "event=runtime.state_changed" in line]
    assert len(changes) == 2
    assert "event=profile.deleted" in " ".join(_messages(caplog))


def test_focus_outcomes_are_logged_once_with_attempts_and_candidates(tmp_path, caplog, monkeypatch):
    service = _service(tmp_path)
    envelope = {"connected": True, "session_current": True, "prepared": {"session_id": "session"},
                "status": {"focus_candidates": 1, "focus_attempts": 6, "focus_successes": 0, "focus_reason": "refused"}}
    monkeypatch.setattr(service, "_read_runtime_status", lambda app_id: envelope)
    with caplog.at_level(logging.INFO, logger="support-logging"):
        service.get_runtime_status(10)
        service.get_runtime_status(10)
    changes = [line for line in _messages(caplog) if "event=runtime.state_changed" in line]
    assert len(changes) == 1
    assert all(value in changes[0] for value in ["focus_candidates=1", "focus_attempts=6", "focus_successes=0", "focus_reason=refused"])
