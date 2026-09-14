from pathlib import Path
import hashlib
import json
import logging
import os
import struct
import time

import pytest

from ce_decky.paths import PluginPaths
from ce_decky.service import PluginService
from ce_decky.session_protocol import RuntimeStatus, parse_control, render_status


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
    <CheatEntry>
      <ID>2</ID><Description>Mode</Description><VariableType>4 Bytes</VariableType>
      <DropDownList>0:Off\n1:On</DropDownList><DropDownReadOnly>1</DropDownReadOnly>
    </CheatEntry>
  </CheatEntries>
</CheatTable>
""",
        encoding="utf-8",
    )


def _service(tmp_path: Path) -> PluginService:
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("workflow"))
    service.initialize()
    return service


@pytest.mark.parametrize("route", ["save_profile", "associate_table", "set_execution_consent"])
def test_a_table_marked_as_not_working_cannot_be_newly_taken_up(tmp_path: Path, route):
    # Search refuses the saved copy while the mark stands, so no other route may
    # quietly step around it. The refusal names the press that lifts it and
    # needs no network, which is what makes it advice rather than a wall.
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    sha = service.import_table(str(source))["sha256"]
    # Consent is bound to the profile's selected table, so that route is the one
    # where the table is already chosen and only the authorization is new.
    selected = sha if route == "set_execution_consent" else None
    service.save_profile(10, "Game", False, selected, "game.exe")
    service.block_table(sha, "a cheat went straight back off", 10)
    calls = {
        "save_profile": lambda: service.save_profile(10, "Game", False, sha, "game.exe"),
        "associate_table": lambda: service.associate_table(10, sha),
        "set_execution_consent": lambda: service.set_execution_consent(10, sha, True),
    }
    with pytest.raises(ValueError, match="marked as not working"):
        calls[route]()
    assert service.profile_store.get(10).execution_consent_sha256 is None

    # Cleared, and every path it stood in is open again.
    service.unblock_table(sha)
    calls[route]()
    profile = service.profile_store.get(10)
    if route == "save_profile":
        assert profile.table_sha256 == sha
    elif route == "associate_table":
        assert sha in profile.table_library
    else:
        assert profile.execution_consent_sha256 == sha


@pytest.mark.parametrize("damage", ["corrupt json", "unsupported schema", "unreadable"])
def test_unreadable_not_working_state_refuses_nothing(tmp_path: Path, monkeypatch, damage):
    # Advisory state that cannot be read blocks nothing, here as everywhere
    # else. One damaged file would otherwise make every table on the device
    # unselectable while the list that shows and clears these records reads as
    # empty, leaving the user with no record to point at and no press to lift.
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, None, "game.exe")
    if damage == "unreadable":
        def fail(_digest):
            raise OSError("blocked table list cannot be read")
        monkeypatch.setattr(service.table_blocklist, "find", fail)
    else:
        service.table_blocklist.path.write_text(
            "{" if damage == "corrupt json" else json.dumps({"schema": 99, "tables": []}),
            encoding="utf-8",
        )
    service.save_profile(10, "Game", False, sha, "game.exe")
    service.associate_table(10, sha)
    service.set_execution_consent(10, sha, True)
    profile = service.profile_store.get(10)
    assert profile.table_sha256 == sha and profile.execution_consent_sha256 == sha


def test_a_mark_written_later_leaves_the_table_already_in_use_alone(tmp_path: Path):
    # The mark is recorded while the game is running and the table is the one
    # being played. Withdrawing what the user already authorized is Revoke's
    # offer to make, not this guard's, so the selection and the authorization
    # stay and can still be written back unchanged.
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, sha, "game.exe")
    service.set_execution_consent(10, sha, True)
    service.block_table(sha, "a cheat went straight back off", 10)
    service.save_profile(10, "Game", False, sha, "game.exe")
    service.associate_table(10, sha)
    service.set_execution_consent(10, sha, True)
    profile = service.profile_store.get(10)
    assert profile.table_sha256 == sha and profile.execution_consent_sha256 == sha
    # Withdrawing it stays possible, and taking it up again then needs the mark
    # cleared like any other new selection.
    service.set_execution_consent(10, sha, False)
    with pytest.raises(ValueError, match="marked as not working"):
        service.set_execution_consent(10, sha, True)


@pytest.mark.parametrize("cause", ["unusable", "encrypted", "gone"])
def test_a_source_or_payload_condition_never_refuses_or_dates_a_stored_table(tmp_path: Path, monkeypatch, cause):
    # None of these says the table does not work for this game: they are about
    # bytes that never became a table, or about a provider row. So none of them
    # refuses the table, retires what a cheat already proved about it, or stands
    # between it and a fresh proof.
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, None, "game.exe")
    monkeypatch.setattr(service, "_compatibility_fingerprints",
                        lambda profiles, current_app_id=None, recorded_paths=None: {app_id: {"pe_version": "1"} for app_id in profiles})
    floor, epochs = service.table_blocklist.failure_epochs()
    service.table_compatibility.record(10, sha, "game.exe", {"pe_version": "1"}, failure_epoch=epochs.get(sha, floor))
    service.table_blocklist.block(sha256=sha, reason="there is no table in it", cause=cause)

    service.save_profile(10, "Game", False, sha, "game.exe")
    assert service.profile_store.get(10).table_sha256 == sha
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "matching"
    # The epoch a fresh proof would be written against is the one the evidence
    # already carries, so recording another success stays possible.
    after_floor, after_epochs = service.table_blocklist.failure_epochs()
    assert after_epochs.get(sha, after_floor) == epochs.get(sha, floor)

    # A record that the table did not work is the one that dates it.
    service.block_table(sha, "a cheat went straight back off", 10)
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "retest"
    service.unblock_table(sha)
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "retest"


@pytest.mark.parametrize("cause", ["unusable", "encrypted"])
def test_a_later_condition_never_reopens_a_table_the_user_marked(tmp_path: Path, cause):
    # A stricter parser deciding the file is not a table is a statement about
    # the file. It must not lift the refusal a failed cheat earned, because the
    # only thing that lifts that is the user clearing it.
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, None, "game.exe")
    service.block_table(sha, "a cheat went straight back off", 10)
    service.table_blocklist.block(sha256=sha, reason="there is no table in it", cause=cause)
    with pytest.raises(ValueError, match="marked as not working"):
        service.save_profile(10, "Game", False, sha, "game.exe")
    service.unblock_table(sha)
    service.save_profile(10, "Game", False, sha, "game.exe")
    assert service.profile_store.get(10).table_sha256 == sha


def test_a_failed_cheat_takes_the_green_even_when_the_list_has_no_room(tmp_path: Path, monkeypatch):
    # The cheat did not work, and that is true whether or not there is room to
    # write it down. A bounded list that refuses rather than dropping somebody
    # else's decision is a reason to tell the user how to make room, never a
    # reason to leave a table they just watched fail reading as proven.
    from ce_decky.table_blocklist import MAX_BLOCKED_TABLES
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, None, "game.exe")
    monkeypatch.setattr(service, "_compatibility_fingerprints",
                        lambda profiles, current_app_id=None, recorded_paths=None: {app_id: {"pe_version": "1"} for app_id in profiles})
    floor, epochs = service.table_blocklist.failure_epochs()
    service.table_compatibility.record(10, sha, "game.exe", {"pe_version": "1"}, failure_epoch=epochs.get(sha, floor))
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "matching"
    for index in range(MAX_BLOCKED_TABLES):
        service.table_blocklist.block(sha256=f"{index:064x}", reason="did not switch on", now=1000 + index)

    with pytest.raises(ValueError, match="clear one under"):
        service.block_table(sha, "a cheat went straight back off", 10)

    # Every decision already recorded stands, and the one the user just made is
    # reflected where it can be: the table is no longer claiming a proven build.
    assert len(service.table_blocklist.list_blocked()) == MAX_BLOCKED_TABLES
    assert service.table_blocklist.find(sha) is None
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "retest"


def test_clearing_a_legacy_failure_does_not_return_an_old_success_to_green(tmp_path: Path, monkeypatch):
    # The record is from a build that did not write its cause down and carries
    # no epoch of its own, which is the shape that used to let the green come
    # back the moment the mark went.
    import json as _json
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, None, "game.exe")
    monkeypatch.setattr(service, "_compatibility_fingerprints",
                        lambda profiles, current_app_id=None, recorded_paths=None: {app_id: {"pe_version": "1"} for app_id in profiles})
    floor, epochs = service.table_blocklist.failure_epochs()
    service.table_compatibility.record(10, sha, "game.exe", {"pe_version": "1"}, failure_epoch=epochs.get(sha, floor))
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "matching"
    service.table_blocklist.path.write_text(_json.dumps({
        "schema": 4, "failure_floor": floor, "failure_epochs": epochs,
        "tables": [{"sha256": sha, "reason": "did not switch on", "filename": None, "app_id": 10,
                    "game_name": "Game", "game_version": None, "recorded_at": 1700000000,
                    "origins": [], "cause": "something-later"}],
    }), encoding="utf-8")
    # While it stands the panel shows the failure itself, which outranks every
    # positive record whatever the stored evidence compares as. What must not
    # survive the clear is the green underneath it.
    from ce_decky.table_blocklist import is_compatibility_failure
    assert is_compatibility_failure(service.table_blocklist.find(sha))
    service.unblock_table(sha)
    assert service.table_blocklist.find(sha) is None
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "retest"


@pytest.mark.parametrize("cause", ["unusable", "encrypted", "gone"])
def test_only_a_compatibility_cause_advances_the_failure_epoch(tmp_path: Path, cause):
    from ce_decky.table_blocklist import TableBlocklist
    blocked = TableBlocklist(tmp_path / "blocked.json")
    digest = "a" * 64
    floor, epochs = blocked.failure_epochs()
    blocked.block(sha256=digest, reason="there is no table in it", cause=cause)
    after_floor, after_epochs = blocked.failure_epochs()
    assert after_epochs.get(digest, after_floor) == epochs.get(digest, floor)
    blocked.block(sha256=digest, reason="a cheat went straight back off", cause="refused")
    dated_floor, dated_epochs = blocked.failure_epochs()
    assert dated_epochs.get(digest, dated_floor) != epochs.get(digest, floor)


def test_revoke_retires_session_and_preserves_local_library(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = service.paths.user_home / "Game.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    sha = table["sha256"]
    service.save_profile(10, "Removed game", True, sha, "game.exe")
    service.set_execution_consent(10, sha, True)
    service.prepare_session(10)
    revoked = service.revoke_table(10, sha)
    assert revoked["table_sha256"] is None
    assert not revoked["autoload_enabled"]
    assert service.session_store.load_current(10) is None
    assert service.table_store.verified_blob(sha).is_file()
    assert sha in revoked["table_library"]
    assert service.revoke_table(10, sha) == revoked
    service.delete_table(sha)


def _import_fake_ce(service: PluginService) -> Path:
    ce_root = service.paths.user_home / "CE"
    (ce_root / "autorun").mkdir(parents=True, exist_ok=True)
    (ce_root / "main.lua").write_text("require('defines')\n", encoding="utf-8")
    exe = ce_root / "Cheat Engine.exe"
    _write_fake_pe(exe)
    service.import_ce(str(exe))
    return exe


def test_archive_inspection_profile_session_and_runtime_command_workflow(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "source.CT"
    _write_ct(source)

    preflight = service.inspect_table_source(str(source))
    assert preflight["format"] == "ct"
    table = service.import_table(str(source))
    inspection = service.inspect_table_sha(table["sha256"])
    assert inspection["process_candidates"] == ["game.exe"]
    assert [control["id"] for control in inspection["controls"]] == [1, 2]

    profile = service.save_profile(10, "Game", False, table["sha256"], "game.exe")
    assert profile["execution_consent_sha256"] is None
    profile = service.set_startup_preference(10, table["sha256"], 2, True, "1")
    assert profile["startup"] == [{"record_id": 2, "active": True, "value": "1"}]
    with pytest.raises(ValueError, match="outside.*dropdown"):
        service.set_startup_preference(10, table["sha256"], 2, True, "2")
    service.set_execution_consent(10, table["sha256"], True)

    prepared = service.prepare_session(10)
    assert prepared["app_id"] == 10
    assert prepared["ce_sha256"] == service.get_status()["ce"]["sha256"]
    assert prepared["table_sha256"] == table["sha256"]
    runtime = service.get_runtime_status(10)
    assert runtime["prepared"]["session_id"] == prepared["session_id"]
    assert runtime["status"] is None

    with pytest.raises(ValueError, match="missing or ambiguous"):
        service.write_runtime_commands(10, [{"generation": 1, "kind": "query", "record_id": 999}])
    with pytest.raises(ValueError, match="read-only dropdown"):
        service.write_runtime_commands(10, [{"generation": 1, "kind": "set_value", "record_id": 2, "value": "9"}])
    # Which of the four a bridge that is not connected is: this session has a
    # prepared record and no heartbeat at all behind it.
    with pytest.raises(ValueError, match="has not reported its state"):
        service.write_runtime_commands(10, [{"generation": 1, "kind": "query", "record_id": 1}])

    live = RuntimeStatus(
        prepared["session_id"], 10, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, True, "game.exe", 123, (), (),
    )
    Path(prepared["status_path"]).write_bytes(render_status(live))
    service.write_runtime_commands(10, [
        {"generation": 1, "kind": "query", "record_id": 1},
        {"generation": 2, "kind": "set_active", "record_id": 2, "value": "1"},
    ])
    control_path = Path(prepared["control_path"])
    parsed = parse_control(control_path.read_bytes())
    assert [(item.generation, item.kind, item.record_id, item.value) for item in parsed] == [
        (1, "query", 1, None),
        (2, "set_active", 2, "1"),
    ]


def test_exact_sha_consent_and_strict_rpc_types(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "source.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(10, "Game", False, table["sha256"], "game.exe")

    with pytest.raises(ValueError, match="consent"):
        service.prepare_session(10)
    with pytest.raises(ValueError, match="AppID"):
        service.save_profile(True, "Bad", False, table["sha256"], "game.exe")
    with pytest.raises(ValueError, match="generation"):
        service.write_runtime_commands(10, [{"generation": True, "kind": "list_processes"}])
    with pytest.raises(ValueError, match="unknown"):
        service.write_runtime_commands(10, [{"generation": 1, "kind": "list_processes", "extra": 1}])


def test_private_runtime_materializes_without_touching_the_source_ce_tree(tmp_path: Path):
    service = _service(tmp_path)
    paths = service.paths
    ce_root = paths.user_home / "CE"
    (ce_root / "autorun").mkdir(parents=True)
    (ce_root / "main.lua").write_text("require('defines')\n", encoding="utf-8")
    exe = ce_root / "Cheat Engine.exe"
    _write_fake_pe(exe)
    source_marker = ce_root / "autorun" / "upstream.lua"
    source_marker.write_text("return true\n", encoding="utf-8")
    service.import_ce(str(exe))

    ct = tmp_path / "source.CT"
    _write_ct(ct)
    table = service.import_table(str(ct))
    service.save_profile(10, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(10, table["sha256"], True)

    source_before = {p.relative_to(ce_root): p.read_bytes() for p in ce_root.rglob("*") if p.is_file()}
    runtime = service.prepare_private_ce_runtime()
    source_after = {p.relative_to(ce_root): p.read_bytes() for p in ce_root.rglob("*") if p.is_file()}

    assert source_after == source_before
    assert str(runtime["executable"]).startswith("/")
    runtime_root = Path(str(runtime["executable"])).parent
    assert (runtime_root / "autorun" / "000_ce_decky_bridge.lua").is_file()


def test_pinned_control_rpc_is_validated_against_exact_table(tmp_path: Path):
    service = _service(tmp_path)
    source = service.paths.user_home / "pin.CT"
    source.write_text('''<?xml version="1.0"?><CheatTable><CheatEntries><CheatEntry><ID>42</ID><Description>"Money"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry></CheatEntries></CheatTable>''')
    table = service.import_table(str(source))
    service.save_profile(42, "Game", False, table["sha256"], "game.exe")
    profile = service.set_pinned_control(42, table["sha256"], 42, True)
    assert profile["pinned"] == [42]
    with pytest.raises(ValueError, match="MemoryRecord"):
        service.set_pinned_control(42, table["sha256"], 999, True)
    assert service.clear_pinned_controls(42, table["sha256"])["pinned"] == []


def test_runtime_record_commands_require_attached_target_but_process_controls_do_not(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "unattached.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(87, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(87, table["sha256"], True)
    prepared = service.prepare_session(87)
    status = RuntimeStatus(
        prepared["session_id"], 87, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, False, "game.exe", 0, (), (),
    )
    Path(prepared["status_path"]).write_bytes(render_status(status))
    assert service.get_runtime_status(87)["connected"] is True

    process_receipt = service.write_runtime_commands(87, [{"generation": 1, "kind": "list_processes"}])
    assert process_receipt["next_generation"] == 2
    with pytest.raises(ValueError, match="not attached"):
        service.write_runtime_commands(87, [{"generation": 2, "kind": "query", "record_id": 1}])


def test_group_headers_are_rejected_by_profile_and_runtime_mutation_apis(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "group.CT"
    source.write_text(
        """<?xml version="1.0"?><CheatTable><CheatEntries>
<CheatEntry><ID>1</ID><Description>"Group"</Description><GroupHeader>1</GroupHeader><CheatEntries>
<CheatEntry><ID>2</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>
</CheatEntries></CheatEntry></CheatEntries></CheatTable>""",
        encoding="utf-8",
    )
    table = service.import_table(str(source))
    service.save_profile(86, "Game", False, table["sha256"], "game.exe")
    with pytest.raises(ValueError, match="presentation-only"):
        service.set_startup_preference(86, table["sha256"], 1, True, None)
    with pytest.raises(ValueError, match="presentation-only"):
        service.set_pinned_control(86, table["sha256"], 1, True)

    service.set_execution_consent(86, table["sha256"], True)
    prepared = service.prepare_session(86)
    status = RuntimeStatus(
        prepared["session_id"], 86, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, True, "game.exe", 123, (), (),
    )
    Path(prepared["status_path"]).write_bytes(render_status(status))
    with pytest.raises(ValueError, match="presentation-only"):
        service.write_runtime_commands(86, [{"generation": 1, "kind": "query", "record_id": 1}])


def test_runtime_status_freshness_uses_status_file_mtime(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "source.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(88, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(88, table["sha256"], True)
    prepared = service.prepare_session(88)

    status = RuntimeStatus(
        prepared["session_id"], 88, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, True, "game.exe", 123, (), (),
    )
    status_path = Path(prepared["status_path"])
    status_path.write_bytes(render_status(status))
    fresh = service.get_runtime_status(88)
    assert fresh["connected"] is True and fresh["status_fresh"] is True
    assert isinstance(fresh["status_age_ms"], int) and fresh["status_age_ms"] < 3000

    old = time.time() - 30
    os.utime(status_path, (old, old))
    stale = service.get_runtime_status(88)
    assert stale["connected"] is False and stale["status_fresh"] is False
    assert stale["status_age_ms"] >= 25_000


def test_runtime_status_future_mtime_is_not_connected(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "future.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(188, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(188, table["sha256"], True)
    prepared = service.prepare_session(188)
    status = RuntimeStatus(
        prepared["session_id"], 188, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, True, "game.exe", 42, (), (),
    )
    path = Path(prepared["status_path"])
    path.write_bytes(render_status(status))
    future = time.time() + 60
    os.utime(path, (future, future))
    envelope = service.get_runtime_status(188)
    assert envelope["status_clock_skew"] is True
    assert envelope["status_fresh"] is False
    assert envelope["connected"] is False
    with pytest.raises(ValueError, match="clock-skewed heartbeat"):
        service.retire_session(188, prepared["session_id"])
    with pytest.raises(ValueError, match="clock-skewed heartbeat"):
        service.prepare_session(188)


def test_runtime_set_value_requires_explicit_value(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "source.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(10, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(10, table["sha256"], True)
    service.prepare_session(10)
    with pytest.raises(ValueError, match="explicit string value"):
        service.write_runtime_commands(10, [{"generation": 1, "kind": "set_value", "record_id": 1}])


def test_runtime_session_becomes_stale_when_profile_or_ce_identity_changes(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "stale.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(501, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(501, table["sha256"], True)
    prepared = service.prepare_session(501)
    status = RuntimeStatus(
        prepared["session_id"], 501, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, True, "game.exe", 123, (), (),
    )
    Path(prepared["status_path"]).write_bytes(render_status(status))
    assert service.get_runtime_status(501)["connected"] is True

    service.set_execution_consent(501, table["sha256"], False)
    stale = service.get_runtime_status(501)
    assert stale["session_current"] is False
    assert "consent" in stale["session_stale_reason"]
    assert stale["connected"] is False
    with pytest.raises(ValueError, match="prepared session is stale"):
        service.write_runtime_commands(501, [{"generation": 1, "kind": "list_processes"}])


def test_runtime_session_becomes_stale_when_target_process_changes(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "retarget.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(502, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(502, table["sha256"], True)
    prepared = service.prepare_session(502)
    service.save_profile(502, "Game", False, table["sha256"], "other.exe")
    stale = service.get_runtime_status(502)
    assert stale["session_current"] is False
    assert "target process changed" in stale["session_stale_reason"]


def test_runtime_session_becomes_stale_when_it_predates_the_table_script_identity(tmp_path: Path):
    # The bridge decides on this field whether it may use the table-load route
    # that lets Cheat Engine ask about the table's own Lua script, and that
    # question cannot be answered from Game Mode. A session prepared before the
    # field existed states nothing, so it is retired and prepared again rather
    # than leaving the bridge to guess.
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "legacy-session.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(509, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(509, table["sha256"], True)
    prepared = service.prepare_session(509)
    assert service.get_runtime_status(509)["session_current"] is True

    descriptor_path = Path(prepared["descriptor_path"])
    descriptor = descriptor_path.read_bytes()
    legacy = b"".join(
        line + b"\n" for line in descriptor.split(b"\n")
        if line and not line.startswith(b"F\ttable_has_lua\t")
    )
    assert legacy != descriptor
    descriptor_path.write_bytes(legacy)
    # The prepared record names the descriptor's own digests, so a session whose
    # descriptor was edited has to be re-described exactly as the store would.
    metadata = json.loads((descriptor_path.parent / "session.json").read_text())
    metadata["descriptor_sha256"] = hashlib.sha256(legacy).hexdigest()
    metadata["descriptor_md5"] = hashlib.md5(legacy).hexdigest()
    (descriptor_path.parent / "session.json").write_text(json.dumps(metadata))

    stale = service.get_runtime_status(509)
    assert stale["session_current"] is False
    assert "executable-script identity" in stale["session_stale_reason"]


def test_runtime_session_becomes_stale_after_ce_reimport(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "ce-change.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(503, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(503, table["sha256"], True)
    prepared = service.prepare_session(503)

    other = service.paths.user_home / "CE2" / "Cheat Engine.exe"
    other.parent.mkdir(parents=True)
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    data[-1] = 1
    other.write_bytes(data)
    service.import_ce(str(other))
    stale = service.get_runtime_status(503)
    assert stale["session_current"] is False
    assert "Cheat Engine identity changed" in stale["session_stale_reason"]


def test_consent_can_be_revoked_after_table_blob_is_lost(tmp_path: Path):
    service = _service(tmp_path)
    source = tmp_path / "consent.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(601, "Game", False, table["sha256"], "game.exe")
    assert service.set_execution_consent(601, table["sha256"], True)["execution_consent_sha256"] == table["sha256"]

    Path(table["blob_path"]).unlink()
    revoked = service.set_execution_consent(601, table["sha256"], False)
    assert revoked["execution_consent_sha256"] is None
    with pytest.raises(ValueError, match="blob"):
        service.set_execution_consent(601, table["sha256"], True)


def test_retire_session_is_exact_id_guarded_and_unblocks_removal_readiness(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "retire.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(602, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(602, table["sha256"], True)
    prepared = service.prepare_session(602)

    # A prepared session is plugin-owned state that removal deletes along with
    # everything else, so it is reported and does not block. Replacing the
    # profile it belongs to is a different question and stays guarded below.
    readiness = service.get_removal_readiness()
    assert readiness["current_session_app_ids"] == [602]
    assert readiness["live_owned_launch"] is False
    assert readiness["can_delete_managed_data"] is True
    with pytest.raises(ValueError, match="prepared session is current"):
        service.delete_profile(602)
    with pytest.raises(ValueError, match="session changed"):
        service.retire_session(602, "0" * 32)

    retired = service.retire_session(602, prepared["session_id"])
    assert retired == {
        "retired": True,
        "session_id": prepared["session_id"],
        "requires_target_validation": True,
    }
    assert service.get_runtime_status(602)["prepared"] is None
    readiness = service.get_removal_readiness()
    assert readiness["current_session_app_ids"] == []
    assert readiness["can_delete_managed_data"] is True
    inventory = service.get_session_inventory()
    app = next(item for item in inventory["apps"] if item["app_id"] == 602)
    assert app["session_count"] == 1
    assert app["current_session_id"] is None
    assert service.delete_profile(602)["deleted"] is True


def test_retire_session_refuses_fresh_connected_bridge_but_allows_stale_heartbeat(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "retire-connected.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(603, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(603, table["sha256"], True)
    prepared = service.prepare_session(603)
    status = RuntimeStatus(
        prepared["session_id"], 603, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, True, "game.exe", 123, (), (),
    )
    status_path = Path(prepared["status_path"])
    status_path.write_bytes(render_status(status))
    with pytest.raises(ValueError, match="fresh heartbeat"):
        service.retire_session(603, prepared["session_id"])

    old = time.time() - 30
    os.utime(status_path, (old, old))
    assert service.retire_session(603, prepared["session_id"])["retired"] is True


def test_session_replacement_is_blocked_while_bridge_heartbeat_is_fresh(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "replace-live.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(699, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(699, table["sha256"], True)
    prepared = service.prepare_session(699)
    live = RuntimeStatus(
        prepared["session_id"], 699, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
        1, True, "game.exe", 123, (), (),
    )
    status_path = Path(prepared["status_path"])
    status_path.write_bytes(render_status(live))

    with pytest.raises(ValueError, match="fresh heartbeat"):
        service.prepare_session(699)

    old = time.time() - 30
    os.utime(status_path, (old, old))
    replacement = service.prepare_session(699)
    assert replacement["session_id"] != prepared["session_id"]


def test_session_replacement_rechecks_for_bridge_that_becomes_live_during_preparation(tmp_path: Path, monkeypatch):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "replace-race.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(698, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(698, table["sha256"], True)
    prepared = service.prepare_session(698)
    status_path = Path(prepared["status_path"])

    original_validate = service._validated_ce_import

    def validate_after_bridge_resumes():
        live = RuntimeStatus(
            prepared["session_id"], 698, prepared["ce_sha256"], table["sha256"], prepared["descriptor_sha256"],
            1, True, "game.exe", 123, (), (),
        )
        status_path.write_bytes(render_status(live))
        return original_validate()

    monkeypatch.setattr(service, "_validated_ce_import", validate_after_bridge_resumes)
    with pytest.raises(ValueError, match="fresh heartbeat"):
        service.prepare_session(698)
    assert service.session_store.load_current(698).session_id == prepared["session_id"]


def test_full_off_target_user_workflow_restores_exact_sha_preferences_on_rollback(tmp_path: Path):
    service = _service(tmp_path)
    _import_fake_ce(service)

    source_v1 = tmp_path / "game-v1.CT"
    _write_ct(source_v1)
    table_v1 = service.import_table(str(source_v1))
    service.save_profile(700, "Game", False, table_v1["sha256"], "game.exe")
    service.set_startup_preference(700, table_v1["sha256"], 2, True, "1")
    service.set_pinned_control(700, table_v1["sha256"], 1, True)
    service.set_execution_consent(700, table_v1["sha256"], True)
    prepared_v1 = service.prepare_session(700)

    source_v2 = tmp_path / "game-v2.CT"
    _write_ct(source_v2)
    source_v2.write_text(source_v2.read_text(encoding="utf-8").replace("Health", "Health v2"), encoding="utf-8")
    table_v2 = service.import_table(str(source_v2))
    assert table_v2["sha256"] != table_v1["sha256"]

    switched = service.save_profile(700, "Game", False, table_v2["sha256"], "game.exe")
    assert switched["previous_table_sha256"] == table_v1["sha256"]
    assert switched["execution_consent_sha256"] is None
    assert switched["startup"] == []
    assert switched["pinned"] == []
    stale_v1 = service.get_runtime_status(700)
    assert stale_v1["prepared"]["session_id"] == prepared_v1["session_id"]
    assert stale_v1["session_current"] is False
    assert "table SHA changed" in stale_v1["session_stale_reason"]

    service.set_startup_preference(700, table_v2["sha256"], 2, False, "0")
    service.set_pinned_control(700, table_v2["sha256"], 2, True)
    service.set_execution_consent(700, table_v2["sha256"], True)

    rolled_back = service.save_profile(700, "Game", False, table_v1["sha256"], "game.exe")
    assert rolled_back["previous_table_sha256"] == table_v2["sha256"]
    assert rolled_back["execution_consent_sha256"] == table_v1["sha256"]
    assert rolled_back["startup"] == [{"record_id": 2, "active": True, "value": "1"}]
    assert rolled_back["pinned"] == [1, 2]

    prepared_again = service.prepare_session(700)
    assert prepared_again["table_sha256"] == table_v1["sha256"]
    assert prepared_again["session_id"] != prepared_v1["session_id"]
    assert service.get_runtime_status(700)["session_current"] is True



def test_game_table_library_autoload_and_remembered_state_are_exact_sha_validated(tmp_path: Path):
    service = _service(tmp_path)
    source = tmp_path / "source.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(711, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(711, table["sha256"], True)

    associated = service.associate_table(711, table["sha256"])
    assert associated["table_library"] == [table["sha256"]]
    remembered = service.set_remembered_cheats(711, table["sha256"], [
        {"record_id": 1, "active": False, "value": "250"},
        {"record_id": 2, "active": True, "value": "1"},
    ])
    assert remembered["remembered"] == [
        {"record_id": 1, "active": False, "value": "250"},
        {"record_id": 2, "active": True, "value": "1"},
    ]
    enabled = service.set_autoload(711, table["sha256"], True)
    assert enabled["autoload_enabled"] is True

    with pytest.raises(ValueError, match="read-only dropdown"):
        service.set_remembered_cheats(711, table["sha256"], [
            {"record_id": 2, "active": True, "value": "9"},
        ])
    with pytest.raises(ValueError, match="duplicate MemoryRecord"):
        service.set_remembered_cheats(711, table["sha256"], [
            {"record_id": 1, "active": True, "value": None},
            {"record_id": 1, "active": False, "value": None},
        ])


def test_configured_values_are_exact_sha_validated_and_outlive_live_reconciliation(tmp_path: Path):
    """The value a user types is durable on its own.

    It used to reach the profile only through the remembered state derived from
    the post-Apply re-read, so a record Cheat Engine could not read yet - or one
    whose activation failed - persisted the live answer instead of the choice.
    """
    service = _service(tmp_path)
    source = tmp_path / "source.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(712, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(712, table["sha256"], True)

    configured = service.set_configured_values(712, table["sha256"], [{"record_id": 1, "value": "250"}])
    assert configured["configured_values"] == [{"record_id": 1, "value": "250"}]

    # The live re-read reported a zero for the same record; the configuration stands.
    reconciled = service.set_remembered_cheats(712, table["sha256"], [
        {"record_id": 1, "active": True, "value": "0"},
    ])
    assert reconciled["configured_values"] == [{"record_id": 1, "value": "250"}]
    assert reconciled["remembered"] == [{"record_id": 1, "active": True, "value": "0"}]

    with pytest.raises(ValueError, match="blank or Cheat Engine"):
        service.set_configured_values(712, table["sha256"], [{"record_id": 1, "value": "??"}])
    with pytest.raises(ValueError, match="read-only dropdown"):
        service.set_configured_values(712, table["sha256"], [{"record_id": 2, "value": "9"}])
    with pytest.raises(ValueError, match="duplicate MemoryRecord"):
        service.set_configured_values(712, table["sha256"], [
            {"record_id": 1, "value": "1"},
            {"record_id": 1, "value": "2"},
        ])
    with pytest.raises(ValueError, match="malformed"):
        service.set_configured_values(712, table["sha256"], [{"record_id": 1}])
    assert service.profile_store.get(712).configured_values[0].value == "250"  # type: ignore[union-attr]


def test_one_launch_prepares_exactly_one_session_and_collects_superseded_ones(tmp_path: Path):
    """Every prepare writes a full exact-SHA table snapshot.

    The controller used to prepare a session purely to satisfy a precondition in
    the launch path, which prepared another one, so each launch left a snapshot
    Cheat Engine never opened. Nothing collected the superseded directories
    either, so ordinary play grew the managed root without bound.
    """
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "collect.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(10, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(10, table["sha256"], True)

    app_root = service.session_store.root / "10"

    def session_dirs() -> set[str]:
        return {entry.name for entry in app_root.iterdir() if entry.is_dir()}

    first = service.prepare_session(10)
    assert session_dirs() == {first["session_id"]}

    # The launch path owns session creation now: it prepares one, not a second.
    prepared, _, _, target_process = service._attached_launch_inputs(10)
    assert prepared.session_id != first["session_id"]
    # Supervision can only watch the game leave if the launch is handed the
    # exact process this profile confirmed.
    assert target_process == "game.exe"
    assert len(session_dirs()) == 2

    for _ in range(6):
        service._release_launch_reservation(10)
        service.prepare_session(10)
    # Current plus a short evidence history, not one directory per launch.
    assert len(session_dirs()) <= 4
    current = service.session_store.load_current(10)
    assert current is not None and current.session_id in session_dirs()


def test_corrupt_current_session_can_be_repaired_entirely_through_the_backend(tmp_path: Path):
    """Strict parsing is right; having no repair for it is not.

    Every ordinary recovery path - runtime status, preparation, retirement,
    profile deletion - begins from the same `load_current()` read, so a
    malformed pointer blocked this game indefinitely even with no process
    running, and the only fix was deleting files from a terminal.
    """
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "repair.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    service.save_profile(10, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(10, table["sha256"], True)
    service.prepare_session(10)

    pointer = service.session_store.root / "10" / "current.json"
    pointer.write_text("{ not json", encoding="utf-8")

    # The read degrades instead of taking the RPC down, and names the condition.
    runtime = service.get_runtime_status(10)
    assert runtime["prepared"] is None
    assert runtime["session_state_reason"]

    # A readable session is retired normally, never discarded by this path.
    with pytest.raises(ValueError, match="readable"):
        service.repair_session_state(11)

    assert service.repair_session_state(10)["discarded"] is True
    assert not pointer.exists()
    # The session directory stays as evidence for whatever corrupted it.
    assert any(entry.is_dir() for entry in (service.session_store.root / "10").iterdir())

    # And the game can prepare a session again.
    assert service.prepare_session(10)["session_id"]


def test_a_corrupt_config_can_still_be_repaired_by_registering_cheat_engine(tmp_path: Path):
    """Read paths degraded a corrupt config; every write path required it to load."""
    service = _service(tmp_path)
    service.paths.config_path.write_text("{ not json", encoding="utf-8")

    status = service.get_status()
    assert status["ce"]["valid"] is False

    _import_fake_ce(service)
    assert service.get_status()["ce"]["valid"] is True
    assert service.config_store.load().imported_ce_executable

    service.paths.config_path.write_text('{"schema": 99}', encoding="utf-8")
    service.clear_ce_import()
    assert service.config_store.load().imported_ce_executable is None


@pytest.mark.parametrize("readback_active", [True, False])
def test_working_evidence_requires_accepted_activation_and_later_readback(tmp_path, monkeypatch, readback_active):
    from dataclasses import replace
    from ce_decky.session_protocol import RuntimeResult
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "working.CT"
    _write_ct(source)
    table = service.import_table(str(source))
    digest = table["sha256"]
    service.save_profile(10, "Game", False, digest, "game.exe")
    service.set_execution_consent(10, digest, True)
    prepared = service.prepare_session(10)
    status = RuntimeStatus(prepared["session_id"], 10, prepared["ce_sha256"], digest,
                           prepared["descriptor_sha256"], 1, True, "game.exe", 123, (), (),
                           table_load_state="loaded", focus_candidates=1, focus_attempts=1, focus_successes=1, focus_reason="confirmed",
                           focus_discovery_attempts=8, startup_active_ids=(1,), focus_capability="IsWindowVisible=executeCodeLocal", focus_error="Ex unavailable")
    path = Path(prepared["status_path"])
    path.write_bytes(render_status(status))
    monkeypatch.setattr(service, "_game_fingerprint", lambda profile: {"pe_version": "1", "steam_build_id": None})
    monkeypatch.setattr(service, "_compatibility_fingerprints", lambda profiles, current_app_id=None, recorded_paths=None: {app_id: {"pe_version": "1", "steam_build_id": "42"} for app_id in profiles})
    service.write_runtime_commands(10, [{"generation": 1, "kind": "set_active", "record_id": 1, "value": "1"}])
    assert not service.confirm_table_working(10, digest, prepared["session_id"], 1)
    status = replace(status, results=(RuntimeResult(1, 1, True, True, None, None),))
    path.write_bytes(render_status(status))
    service.get_runtime_status(10)
    assert service.table_compatibility.snapshot()["entries"] == []
    service.write_runtime_commands(10, [{"generation": 2, "kind": "query", "record_id": 1}])
    status = replace(status, results=(RuntimeResult(2, 1, True, readback_active, None, None),))
    path.write_bytes(render_status(status))
    assert service.confirm_table_working(10, digest, prepared["session_id"], 1) is readback_active
    entries = service.get_status()["table_compatibility"]["entries"]
    assert len(entries) == int(readback_active)
    if readback_active:
        assert entries[0]["state"] == "matching"
        service.block_table(digest, "activation did not settle", 10)
        service.unblock_table(digest)
        assert service.get_status()["table_compatibility"]["entries"][0]["state"] == "retest"
        assert not service.confirm_table_working(10, digest, prepared["session_id"], 1)

    import zipfile
    from ce_decky.session_protocol import parse_status
    with zipfile.ZipFile(service.create_support_bundle([], 0)["path"]) as archive:
        saved = parse_status(archive.read(f"state/sessions/10/{prepared['session_id']}/status.txt"))
        assert saved.focus_reason == "confirmed" and saved.focus_attempts == 1
        assert saved.focus_discovery_attempts == 8 and saved.startup_active_ids == (1,)
        assert saved.focus_capability == "IsWindowVisible=executeCodeLocal"
        assert saved.focus_error == "Ex unavailable"


def test_a_closed_game_is_still_comparable_from_the_path_its_evidence_recorded(tmp_path, monkeypatch):
    """The case a non-Steam shortcut is always in once the game stops.

    `pe_version` is read off a live executable and a shortcut has no build id,
    so with nothing running there was nothing to compare and a proven table read
    as "current build unknown" - which is the state Manage is usually opened in.
    The evidence now carries the path it was read from, and that answers off the
    disk with the game closed.
    """
    service = _service(tmp_path)
    source = tmp_path / "Game.CT"
    source.write_text("<CheatTable><CheatEntries/></CheatTable>", encoding="utf-8")
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(900, "Shortcut Game", True, None, "game.exe")

    executable = tmp_path / "game.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setattr("ce_decky.service.read_pe_version", lambda path: "1.2" if Path(path) == executable else None)
    # Nothing is running, which is the whole point.
    monkeypatch.setattr("ce_decky.service.observe_game_executable_paths", lambda wanted: {})

    floor, epochs = service.table_blocklist.failure_epochs()
    service.table_compatibility.record(900, sha, "game.exe", {
        "pe_version": "1.2", "steam_build_id": None, "executable_path": str(executable),
    }, failure_epoch=epochs.get(sha, floor))

    entry = service._compatibility_snapshot()["entries"][0]
    assert entry["state"] == "matching"
    # The path was what the comparison was made from; it is not published to
    # the panel, which reads none of it and whose own record is collected into
    # an archive attached to public issues.
    assert "executable_path" not in entry

    # And a build that has moved on since is a retest rather than a green.
    monkeypatch.setattr("ce_decky.service.read_pe_version", lambda path: "9.9")
    assert service._compatibility_snapshot()["entries"][0]["state"] == "retest"

    # A record with no path keeps exactly the answer it had before: nothing to
    # compare, so nothing claimed in either direction.
    service.table_compatibility.record(900, sha, "game.exe", {
        "pe_version": "1.2", "steam_build_id": None,
    }, failure_epoch=epochs.get(sha, floor))
    assert service._compatibility_snapshot()["entries"][0]["state"] == "unknown"


def test_a_closed_game_is_compared_against_its_own_program_not_the_last_one_proven(tmp_path, monkeypatch):
    """One game, two programs proven, and only one of them is the target.

    A launcher and the game it starts are both a table's target somewhere, so a
    game can hold a record for each. The path the offline comparison reads was
    the newest of them whatever program it was about, so a table proven on the
    game was being checked against the launcher's version: a retest nobody owes,
    or - where one build stamps both files alike - a match nothing checked.
    """
    service = _service(tmp_path)
    source = tmp_path / "Game.CT"
    source.write_text("<CheatTable><CheatEntries/></CheatTable>", encoding="utf-8")
    sha = service.import_table(str(source))["sha256"]
    service.save_profile(900, "Shortcut Game", True, None, "game.exe")

    game = tmp_path / "game.exe"
    launcher = tmp_path / "launcher.exe"
    for binary in (game, launcher):
        binary.write_bytes(b"MZ")
    versions = {game: "1.2", launcher: "9.9"}
    monkeypatch.setattr("ce_decky.service.read_pe_version", lambda path: versions.get(Path(path)))
    monkeypatch.setattr("ce_decky.service.observe_game_executable_paths", lambda wanted: {})

    floor, epochs = service.table_blocklist.failure_epochs()
    epoch = epochs.get(sha, floor)
    service.table_compatibility.record(900, sha, "game.exe", {
        "pe_version": "1.2", "steam_build_id": None, "executable_path": str(game),
    }, failure_epoch=epoch)
    # Recorded later, and about the other program entirely.
    service.table_compatibility.record(900, "b" * 64, "launcher.exe", {
        "pe_version": "9.9", "steam_build_id": None, "executable_path": str(launcher),
    }, failure_epoch=epochs.get("b" * 64, floor))

    states = {entry["table_sha256"]: entry["state"] for entry in service._compatibility_snapshot()["entries"]}
    assert states[sha] == "matching"
    # The other record is about a program this profile does not target, so it
    # claims nothing either way - which is the rule that was already here.
    assert states["b" * 64] == "unknown"


def test_working_evidence_does_not_compare_versions_of_different_processes(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.save_profile(10, "Game", False, None, "new.exe")
    service.table_compatibility.record(10, "a" * 64, "old.exe", {"pe_version": "1", "steam_build_id": "42"}, failure_epoch="0" * 32)
    monkeypatch.setattr(service, "_game_fingerprint", lambda profile: {"pe_version": "1", "steam_build_id": None})
    monkeypatch.setattr(service, "_compatibility_fingerprints", lambda profiles, current_app_id=None, recorded_paths=None: {app_id: {"pe_version": "1", "steam_build_id": "42"} for app_id in profiles})
    assert service.get_status()["table_compatibility"]["entries"][0]["state"] == "unknown"


def test_compatibility_history_does_not_break_corrupt_profile_recovery(tmp_path):
    service = _service(tmp_path)
    service.table_compatibility.record(10, "a" * 64, "game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    service.profile_store.path.write_text("{broken")
    status = service.get_status()
    assert status["profile_state_reason"]
    assert status["table_compatibility"]["entries"][0]["state"] == "unknown"


@pytest.mark.parametrize("after_replace", [False, True])
@pytest.mark.parametrize("clear_all", [False, True])
def test_failed_survives_positive_store_write_failure_and_clear(tmp_path, monkeypatch, after_replace, clear_all):
    from ce_decky.atomic import DurabilityUnknownError
    service = _service(tmp_path)
    digest = "a" * 64
    service.save_profile(10, "Game", False, None, "game.exe")
    service.table_compatibility.record(10, digest, "game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    original = service.table_compatibility.invalidate
    def fail(sha):
        if after_replace:
            original(sha)
            raise DurabilityUnknownError("directory fsync failed")
        raise OSError("positive store cannot be written")
    monkeypatch.setattr(service.table_compatibility, "invalidate", fail)
    service.block_table(digest, "activation failed", 10)
    assert service.table_blocklist.find(digest).reason == "activation failed"
    if clear_all:
        service.table_blocklist.clear()
    else:
        service.unblock_table(digest)
    reopened = _service(tmp_path)
    assert reopened.table_blocklist.find(digest) is None
    assert reopened.get_status()["table_compatibility"]["entries"][0]["state"] == "retest"
    import zipfile
    with zipfile.ZipFile(reopened.create_support_bundle([], 0)["path"]) as archive:
        saved = json.loads(archive.read("state/blocked_tables.json"))
        assert saved["failure_epochs"][digest]


def test_compatibility_snapshot_observes_processes_and_libraries_once(tmp_path, monkeypatch):
    from unittest.mock import Mock
    service = _service(tmp_path)
    for app_id in range(10, 20):
        service.save_profile(app_id, "Game", False, None, "game.exe")
        service.table_compatibility.record(app_id, "a" * 64, "game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    processes = Mock(return_value={})
    libraries = Mock(return_value=(None, []))
    monkeypatch.setattr("ce_decky.service.observe_game_executable_paths", processes)
    monkeypatch.setattr("ce_decky.service.discover_steam_library_roots", libraries)
    assert len(service.get_status()["table_compatibility"]["entries"]) == 10
    assert processes.call_count == libraries.call_count == 1
    assert len(processes.call_args.args[0]) == 10


@pytest.mark.parametrize("outcome", ["pending", "applied", "failed_rolled_back", "wrong_leaf", "failure_cleared", "already_active"])
def test_startup_compatibility_requires_complete_exact_leaf_success(tmp_path, monkeypatch, outcome):
    from ce_decky.session_protocol import RuntimeResult
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "startup.CT"
    _write_ct(source)
    digest = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, digest, "game.exe")
    service.set_execution_consent(10, digest, True)
    service.set_startup_preference(10, digest, 1, True)
    prepared = service.prepare_session(10)
    status = RuntimeStatus(prepared["session_id"], 10, prepared["ce_sha256"], digest,
        prepared["descriptor_sha256"], 1, True, "game.exe", 123,
        (RuntimeResult(0, 2 if outcome == "wrong_leaf" else 1, True, True, None, None),), (),
        table_load_state="loaded", startup_state=outcome if outcome in ("pending", "failed_rolled_back") else "applied",
        startup_completed=0 if outcome == "pending" else 1, startup_total=1,
        startup_active_ids=() if outcome == "already_active" else (2 if outcome == "wrong_leaf" else 1,))
    Path(prepared["status_path"]).write_bytes(render_status(status))
    if outcome == "failure_cleared":
        service.block_table(digest, "failed", 10)
        service.unblock_table(digest)
    service.get_runtime_status(10)
    assert len(service.table_compatibility.snapshot()["entries"]) == int(outcome == "applied")


def test_revoke_stop_binds_the_session_after_validating_the_table(tmp_path, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "revoke.CT"
    _write_ct(source)
    digest = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, digest, "game.exe")
    service.set_execution_consent(10, digest, True)
    service.prepare_session(10)
    # The profile still validates A, but the supervisor now owns replacement B.
    monkeypatch.setattr(service.ce_launch, "current_for_app", lambda app_id: {"session_id": "replacement", "operation_id": "new"})
    stop = AsyncMock()
    monkeypatch.setattr(service.ce_launch, "stop", stop)
    with pytest.raises(ValueError, match="session changed"):
        asyncio.run(service.stop_ce_for_game(10, digest))
    stop.assert_not_called()


@pytest.mark.parametrize("child_action", ["value", "off", "on"])
def test_startup_leaf_eligibility_depends_only_on_enabled_descendants(tmp_path, child_action):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "nested.CT"
    source.write_text('<CheatTable><CheatEntries><CheatEntry><ID>1</ID><Description>Parent</Description><VariableType>Auto Assembler Script</VariableType><AssemblerScript>[ENABLE]\n[DISABLE]</AssemblerScript><CheatEntries><CheatEntry><ID>2</ID><Description>Child</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry></CheatEntries></CheatEntry></CheatEntries></CheatTable>')
    digest = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, digest, "game.exe")
    service.set_execution_consent(10, digest, True)
    service.set_startup_preference(10, digest, 1, True)
    service.set_startup_preference(10, digest, 2, None if child_action == "value" else child_action == "on", "2" if child_action == "value" else None)
    service.prepare_session(10)
    assert service._startup_compatibility_pending[10]["leaves"] == ({2} if child_action == "on" else {1})


def test_large_startup_earliest_success_uses_summary_instead_of_evicted_results(tmp_path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "large.CT"
    source.write_text('<CheatTable><CheatEntries>' + ''.join(f'<CheatEntry><ID>{i}</ID><Description>Control {i:04}</Description><VariableType>4 Bytes</VariableType><Address>game.exe+{i}</Address></CheatEntry>' for i in range(1, 152)) + '</CheatEntries></CheatTable>')
    digest = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, digest, "game.exe")
    service.set_execution_consent(10, digest, True)
    service.set_remembered_cheats(10, digest, [{"record_id": i, "active": True if i == 1 else None, "value": None if i == 1 else "2"} for i in range(1, 152)])
    service.set_autoload(10, digest, True)
    prepared = service.prepare_session(10)
    from ce_decky.session_protocol import RuntimeResult
    runtime = RuntimeStatus(prepared["session_id"], 10, prepared["ce_sha256"], digest, prepared["descriptor_sha256"], 1,
        True, "game.exe", 123, tuple(RuntimeResult(0, i, True, False, "2", None) for i in range(24, 152)), (),
        table_load_state="loaded", startup_state="applied", startup_completed=151, startup_total=151, startup_active_ids=(1,))
    Path(prepared["status_path"]).write_bytes(render_status(runtime))
    service.get_runtime_status(10)
    assert service.table_compatibility.snapshot()["entries"][0]["table_sha256"] == digest


def test_corrupt_empty_failure_epochs_are_reported_and_repaired(tmp_path):
    service = _service(tmp_path)
    service.table_blocklist.path.write_text(json.dumps({"schema": 4, "tables": [], "failure_floor": "bad", "failure_epochs": {}}))
    assert service.list_blocked_tables()["reason"]
    assert service.clear_blocked_tables() == 0
    assert service.list_blocked_tables()["reason"] is None
    floor, epochs = service.table_blocklist.failure_epochs()
    assert len(floor) == 32 and epochs == {}


def test_historical_games_do_not_cause_manifest_reads(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from types import SimpleNamespace
    service = _service(tmp_path)
    profiles = {i: SimpleNamespace(app_id=i, target_process="game.exe", is_shortcut=False) for i in range(1, 4097)}
    monkeypatch.setattr("ce_decky.service.observe_game_executable_paths", lambda targets: {10: "/game.exe"})
    monkeypatch.setattr("ce_decky.service.read_pe_version", lambda path: "1")
    builds = Mock(return_value="1")
    monkeypatch.setattr("ce_decky.service.steam_build_id", builds)
    assert set(service._compatibility_fingerprints(profiles)) == {10}
    assert builds.call_count == 1


@pytest.mark.parametrize("boundary", ["record", "failure_epochs", "get"])
def test_startup_proof_retries_transient_io_until_saved(tmp_path, monkeypatch, boundary):
    from dataclasses import asdict
    from unittest.mock import Mock
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "retry.CT"
    _write_ct(source)
    digest = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, digest, "game.exe")
    service.set_execution_consent(10, digest, True)
    service.set_startup_preference(10, digest, 1, True)
    prepared = service.prepare_session(10)
    runtime = RuntimeStatus(prepared["session_id"], 10, prepared["ce_sha256"], digest, prepared["descriptor_sha256"], 1,
        True, "game.exe", 123, (), (), table_load_state="loaded", startup_state="applied",
        startup_completed=1, startup_total=1, startup_active_ids=(1,))
    Path(prepared["status_path"]).write_bytes(render_status(runtime))
    envelope = {"prepared": prepared, "connected": True, "status": asdict(runtime)}
    owner = {"record": service.table_compatibility, "failure_epochs": service.table_blocklist, "get": service.profile_store}[boundary]
    original = getattr(owner, boundary)
    flaky = Mock(side_effect=OSError("temporary I/O"))
    monkeypatch.setattr(owner, boundary, flaky)
    with pytest.raises(OSError):
        service._observe_startup_compatibility(10, envelope)
    assert 10 in service._startup_compatibility_pending
    monkeypatch.setattr(owner, boundary, original)
    saved = Mock(wraps=service.table_compatibility.record)
    monkeypatch.setattr(service.table_compatibility, "record", saved)
    service._observe_startup_compatibility(10, envelope)
    service._observe_startup_compatibility(10, envelope)
    assert saved.call_count == 1
    assert len(service.table_compatibility.snapshot()["entries"]) == 1


def test_stopped_current_game_compares_steam_build_without_history_scan(tmp_path, monkeypatch):
    from unittest.mock import Mock
    service = _service(tmp_path)
    service.save_profile(10, "Game", False, None, "game.exe")
    service.table_compatibility.record(10, "a" * 64, "game.exe", {"steam_build_id": "1"}, failure_epoch="0" * 32)
    monkeypatch.setattr("ce_decky.service.observe_game_executable_paths", lambda targets: {})
    builds = Mock(return_value="1")
    monkeypatch.setattr("ce_decky.service.steam_build_id", builds)
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "matching"
    builds.return_value = "2"
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "retest"
    assert builds.call_count == 2
    assert service.get_status()["table_compatibility"]["entries"][0]["state"] == "unknown"
    assert builds.call_count == 2


def test_evidence_without_a_target_process_leaves_status_readable(tmp_path):
    import json
    service = _service(tmp_path)
    service.save_profile(10, "Game", False, None, "game.exe")
    service.table_compatibility.record(10, "a" * 64, "game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    path = service.table_compatibility.path
    payload = json.loads(path.read_text())
    payload["entries"][0]["target_process"] = None
    path.write_text(json.dumps(payload))
    compatibility = service.get_status(10)["table_compatibility"]
    assert compatibility["entries"] == [] and compatibility["reason"]


def test_process_case_change_preserves_compatibility_and_live_target(tmp_path, monkeypatch):
    from unittest.mock import Mock
    service = _service(tmp_path)
    service.save_profile(10, "Game", False, None, "Game.exe")
    service.table_compatibility.record(10, "a" * 64, "Game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    guard = Mock(side_effect=ValueError("owned launch"))
    monkeypatch.setattr(service, "_assert_no_live_owned_launch", guard)
    service.save_profile(10, "Game", False, None, "game.exe")
    guard.assert_not_called()
    monkeypatch.setattr(service, "_compatibility_fingerprints", lambda profiles, current_app_id=None, recorded_paths=None: {10: {"pe_version": "1"}})
    assert service.get_status(10)["table_compatibility"]["entries"][0]["state"] == "matching"
    with pytest.raises(ValueError, match="owned launch"):
        service.save_profile(10, "Game", False, None, "different.exe")


def test_case_only_profile_edit_keeps_prepared_session_current(tmp_path):
    service = _service(tmp_path)
    _import_fake_ce(service)
    source = tmp_path / "case.CT"
    _write_ct(source)
    digest = service.import_table(str(source))["sha256"]
    service.save_profile(10, "Game", False, digest, "Game.exe")
    service.set_execution_consent(10, digest, True)
    service.prepare_session(10)
    service.save_profile(10, "Game", False, digest, "game.exe")
    assert service.get_runtime_status(10)["session_current"] is True
