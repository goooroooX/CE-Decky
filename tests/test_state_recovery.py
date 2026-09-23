"""Controller recovery and stale-session replacement contracts.

Each test fails on the code that preceded it: corrupt `control.txt`/`status.txt`
wedged a game outside every controller repair, a corrupt `profiles.json`
refused every per-game write with no way to replace it, and a readable but
stale session made the next launch impossible even though the launch prepares a
fresh session itself.
"""
from pathlib import Path
import logging
import os
import struct
import time

import pytest

from ce_decky.paths import PluginPaths
from ce_decky.service import PluginService
from ce_decky.session_protocol import RuntimeResult, RuntimeStatus, render_status


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


def _service(tmp_path: Path, name: str) -> PluginService:
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger(name))
    service.initialize()
    return service


def _prepared(service: PluginService, tmp_path: Path, app_id: int, *, suffix: str = ""):
    ce_root = service.paths.user_home / "CE"
    (ce_root / "autorun").mkdir(parents=True, exist_ok=True)
    (ce_root / "main.lua").write_text("require('defines')\n", encoding="utf-8")
    executable = ce_root / "Cheat Engine.exe"
    if not executable.exists():
        _write_fake_pe(executable)
        service.import_ce(str(executable))
    source = tmp_path / f"table-{app_id}{suffix}.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(app_id, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(app_id, table["sha256"], True)
    return service.prepare_session(app_id), table


def _stale_heartbeat(prepared: dict, table: dict, app_id: int) -> bytes:
    return render_status(RuntimeStatus(
        prepared["session_id"], app_id, prepared["ce_sha256"], table["sha256"],
        prepared["descriptor_sha256"], 1, True, "game.exe", 123, (), (),
    ))


@pytest.mark.parametrize("corrupt", ["status", "control", "generation"])
def test_corrupt_mutable_protocol_is_reported_as_repairable_runtime_state(tmp_path: Path, corrupt: str):
    service = _service(tmp_path, "h1-report")
    prepared, table = _prepared(service, tmp_path, 901)

    if corrupt == "status":
        Path(prepared["status_path"]).write_bytes(b"not a status file\n")
    elif corrupt == "control":
        Path(prepared["control_path"]).write_bytes(b"\xff\xfe garbage\n")
    else:
        # A heartbeat acknowledging a generation the control log never issued.
        status = RuntimeStatus(
            prepared["session_id"], 901, prepared["ce_sha256"], table["sha256"],
            prepared["descriptor_sha256"], 9, True, "game.exe", 123,
            (RuntimeResult(7, 1, True, True, None, None),), (),
        )
        Path(prepared["status_path"]).write_bytes(render_status(status))

    runtime = service.get_runtime_status(901)
    assert runtime["prepared"] is not None
    assert runtime["connected"] is False
    assert runtime["status_unreadable"] is True
    assert runtime["next_generation"] is None
    assert isinstance(runtime["session_state_reason"], str)
    assert "runtime protocol state is unreadable" in runtime["session_state_reason"]


def test_corrupt_mutable_protocol_can_be_repaired_from_the_controller(tmp_path: Path):
    service = _service(tmp_path, "h1-repair")
    prepared, _ = _prepared(service, tmp_path, 902)
    Path(prepared["status_path"]).write_bytes(b"garbage\n")

    outcome = service.repair_session_state(902)
    assert outcome["discarded"] is True
    # The game is usable again without touching the filesystem by hand.
    replacement = service.prepare_session(902)
    assert replacement["session_id"] != prepared["session_id"]


def test_readable_healthy_session_still_refuses_the_repair(tmp_path: Path):
    service = _service(tmp_path, "h1-guard")
    _prepared(service, tmp_path, 903)
    with pytest.raises(ValueError, match="readable"):
        service.repair_session_state(903)


def test_unreadable_heartbeat_keeps_session_replacement_fail_closed(tmp_path: Path):
    service = _service(tmp_path, "h1-fail-closed")
    prepared, _ = _prepared(service, tmp_path, 904)
    Path(prepared["status_path"]).write_bytes(b"truncated")

    # An unparseable heartbeat is an unknown bridge, not an absent one.
    with pytest.raises(ValueError, match="fresh heartbeat|clock-skewed"):
        service.prepare_session(904)

    service.ce_launch._exits[(904, prepared["session_id"])] = time.time() + 60
    assert service.prepare_session(904)["session_id"] != prepared["session_id"]


def test_runtime_commands_name_the_protocol_repair_instead_of_a_stale_heartbeat(tmp_path: Path):
    service = _service(tmp_path, "h1-commands")
    prepared, _ = _prepared(service, tmp_path, 905)
    Path(prepared["status_path"]).write_bytes(b"garbage\n")
    with pytest.raises(ValueError, match="repair this game's session state"):
        service.write_runtime_commands(905, [{"generation": 1, "kind": "query", "record_id": 1}])


def test_corrupt_profile_store_can_be_replaced_from_the_controller(tmp_path: Path):
    service = _service(tmp_path, "h2")
    _prepared(service, tmp_path, 906)
    store = service.profile_store.path
    store.write_text('{"schema": 5, "profiles": [', encoding="utf-8")

    status = service.get_status()
    assert status["profiles"] == []
    assert isinstance(status["profile_state_reason"], str)
    with pytest.raises(ValueError):
        service.save_profile(907, "Other", False, None, None)

    outcome = service.repair_profile_state()
    assert outcome["discarded"] is True
    quarantined = store.with_name(str(outcome["quarantined"]))
    assert quarantined.is_file(), "the unreadable file must be preserved as evidence"
    assert service.get_status()["profile_state_reason"] is None
    # A game can be configured again without a terminal.
    assert service.save_profile(907, "Other", False, None, None)["app_id"] == 907


def test_readable_profile_store_refuses_the_repair(tmp_path: Path):
    service = _service(tmp_path, "h2-guard")
    with pytest.raises(ValueError, match="readable"):
        service.repair_profile_state()


@pytest.mark.parametrize("mutation", ["table", "target"])
def test_launch_replaces_a_stale_readable_session_instead_of_refusing(tmp_path: Path, mutation: str):
    service = _service(tmp_path, "h3")
    app_id = 910
    prepared, table = _prepared(service, tmp_path, app_id)
    Path(prepared["status_path"]).write_bytes(_stale_heartbeat(prepared, table, app_id))
    old = time.time() - 60
    os.utime(Path(prepared["status_path"]), (old, old))

    if mutation == "table":
        other = tmp_path / "second.CT"
        other.write_text(
            (tmp_path / f"table-{app_id}.CT").read_text(encoding="utf-8").replace("Health", "Ammo"),
            encoding="utf-8",
        )
        second = service.import_table(str(other))
        service.save_profile(app_id, "Game", False, second["sha256"], "game.exe")
        service.set_execution_consent(app_id, second["sha256"], True)
    else:
        service.save_profile(app_id, "Game", False, table["sha256"], "other.exe")

    assert service.get_runtime_status(app_id)["session_stale_reason"] is not None

    fresh, *_ = service._attached_launch_inputs(app_id, False)
    assert fresh.session_id != prepared["session_id"]
    assert service.get_runtime_status(app_id)["session_stale_reason"] is None
    service._release_launch_reservation(app_id)


def test_launch_still_refuses_a_session_whose_execution_consent_was_revoked(tmp_path: Path):
    """Self-healing a stale session must not become a way around consent."""
    service = _service(tmp_path, "h3-consent")
    prepared, table = _prepared(service, tmp_path, 911)
    Path(prepared["status_path"]).write_bytes(_stale_heartbeat(prepared, table, 911))
    old = time.time() - 60
    os.utime(Path(prepared["status_path"]), (old, old))
    service.set_execution_consent(911, table["sha256"], False)

    with pytest.raises(ValueError, match="execution consent"):
        service._attached_launch_inputs(911, False)
