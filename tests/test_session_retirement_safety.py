from pathlib import Path
import logging
import os
import struct
import time

import pytest

from ce_decky.paths import PluginPaths
from ce_decky.service import PluginService
from ce_decky.session_protocol import RuntimeStatus, render_status


def _write_fake_pe(path: Path) -> None:
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    path.write_bytes(data)


def _write_ct(path: Path) -> None:
    path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry>
      <ID>1</ID><Description>Health</Description><VariableType>4 Bytes</VariableType>
      <Address>game.exe+1234</Address>
    </CheatEntry>
  </CheatEntries>
</CheatTable>
""",
        encoding="utf-8",
    )


def test_retire_session_blocks_fresh_bridge_even_after_profile_identity_becomes_stale(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("retire-safety"))
    service.initialize()

    ce_root = paths.user_home / "CE"
    (ce_root / "autorun").mkdir(parents=True, exist_ok=True)
    executable = ce_root / "Cheat Engine.exe"
    _write_fake_pe(executable)
    service.import_ce(str(executable))

    source = tmp_path / "retire-stale-profile.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(604, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(604, table["sha256"], True)
    prepared = service.prepare_session(604)

    live = RuntimeStatus(
        prepared["session_id"],
        604,
        prepared["ce_sha256"],
        table["sha256"],
        prepared["descriptor_sha256"],
        1,
        True,
        "game.exe",
        123,
        (),
        (),
    )
    status_path = Path(prepared["status_path"])
    status_path.write_bytes(render_status(live))

    service.set_execution_consent(604, table["sha256"], False)
    runtime = service.get_runtime_status(604)
    assert runtime["session_current"] is False
    assert runtime["status_fresh"] is True
    assert runtime["connected"] is False

    with pytest.raises(ValueError, match="fresh heartbeat"):
        service.retire_session(604, prepared["session_id"])

    old = time.time() - 30
    os.utime(status_path, (old, old))
    assert service.retire_session(604, prepared["session_id"])["retired"] is True


def _prepared_with_live_bridge(service: PluginService, tmp_path: Path, app_id: int):
    ce_root = service.paths.user_home / "CE"
    (ce_root / "autorun").mkdir(parents=True, exist_ok=True)
    executable = ce_root / "Cheat Engine.exe"
    _write_fake_pe(executable)
    service.import_ce(str(executable))

    source = tmp_path / f"owned-launch-{app_id}.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(app_id, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(app_id, table["sha256"], True)
    prepared = service.prepare_session(app_id)
    status = RuntimeStatus(
        prepared["session_id"],
        app_id,
        prepared["ce_sha256"],
        table["sha256"],
        prepared["descriptor_sha256"],
        1,
        True,
        "game.exe",
        123,
        (),
        (),
    )
    Path(prepared["status_path"]).write_bytes(render_status(status))
    return prepared, table


def test_session_replacement_stays_blocked_while_ce_decky_owns_no_proven_exit(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("owned-launch-blocked"))
    service.initialize()
    _prepared_with_live_bridge(service, tmp_path, 811)

    with pytest.raises(ValueError, match="fresh heartbeat"):
        service.prepare_session(811)


def test_confirmed_owned_process_exit_allows_switching_table_without_ending_the_game(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("owned-launch-exit"))
    service.initialize()
    prepared, table = _prepared_with_live_bridge(service, tmp_path, 812)
    status_path = Path(prepared["status_path"])

    # The supervisor records this exact fact when it proves the owned Cheat
    # Engine process group is gone; the heartbeat below predates it.
    service.ce_launch._exits[(812, prepared["session_id"])] = os.stat(status_path).st_mtime + 1

    replacement = service.prepare_session(812)
    assert replacement["session_id"] != prepared["session_id"]
    assert replacement["table_sha256"] == table["sha256"]


def test_a_heartbeat_newer_than_the_confirmed_exit_still_fails_closed(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("owned-launch-race"))
    service.initialize()
    prepared, _ = _prepared_with_live_bridge(service, tmp_path, 813)
    status_path = Path(prepared["status_path"])

    service.ce_launch._exits[(813, prepared["session_id"])] = os.stat(status_path).st_mtime - 1

    with pytest.raises(ValueError, match="fresh heartbeat"):
        service.prepare_session(813)
