"""Auto-load intent and exact expanded startup-plan contracts.

Each test fails on the code that preceded it: a configured value was written at
startup for a cheat the user had explicitly switched off, `Disable all` left
explicit-off intent for records their own script destroys, the persisted budget
ignored the enclosing scripts the descriptor adds, and Auto-load could not be
disarmed once its own prerequisites broke.
"""
from pathlib import Path
import logging
import struct

import pytest

from ce_decky.ct_inspector import inspect_table
from ce_decky.paths import PluginPaths
from ce_decky.profiles import ConfiguredValue, GameProfile, StartupPreference
from ce_decky.service import PluginService
from ce_decky.session_protocol import MAX_STARTUP_ACTIONS, effective_startup_plan

FLAG_TABLE = """<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry>
      <ID>1</ID><Description>Enable</Description><VariableType>Auto Assembler Script</VariableType>
      <AssemblerScript>[ENABLE]
lblGodMode:
cmp dword ptr [bEnableGodMode],1
jne short lblGodModeSkip
mulss xmm0,[fGodModeMod]
lblGodModeSkip:
jmp lblGodModeRet
lblOneHitKill:
cmp dword ptr [bEnableOneHitKill],1
jne short lblOneHitKillSkip
mulss xmm1,[fOneHitKillMod]
lblOneHitKillSkip:
jmp lblOneHitKillRet
bEnableGodMode:
  dd 1
bEnableOneHitKill:
  dd 1</AssemblerScript>
      <CheatEntries>
        <CheatEntry>
          <ID>2</ID><Description>bEnableGodMode</Description><VariableType>4 Bytes</VariableType>
          <Address>bEnableGodMode</Address>
          <DropDownList>0:Disabled
1:Enabled</DropDownList>
        </CheatEntry>
        <CheatEntry>
          <ID>3</ID><Description>bEnableOneHitKill</Description><VariableType>4 Bytes</VariableType>
          <Address>bEnableOneHitKill</Address>
          <DropDownList>0:Disabled
1:Enabled</DropDownList>
        </CheatEntry>
      </CheatEntries>
    </CheatEntry>
  </CheatEntries>
</CheatTable>
"""

NESTED_TABLE = """<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry>
      <ID>1</ID><Description>Enable</Description><VariableType>Auto Assembler Script</VariableType>
      <AssemblerScript>[ENABLE]</AssemblerScript>
      <CheatEntries>
        <CheatEntry>
          <ID>2</ID><Description>Health</Description><VariableType>4 Bytes</VariableType>
          <Address>game.exe+1234</Address>
        </CheatEntry>
        <CheatEntry>
          <ID>3</ID><Description>Ammo</Description><VariableType>4 Bytes</VariableType>
          <Address>game.exe+5678</Address>
        </CheatEntry>
      </CheatEntries>
    </CheatEntry>
  </CheatEntries>
</CheatTable>
"""


def _write_fake_pe(path: Path) -> None:
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    path.write_bytes(data)


def _service(tmp_path: Path, name: str) -> PluginService:
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger(name))
    service.initialize()
    ce_root = service.paths.user_home / "CE"
    (ce_root / "autorun").mkdir(parents=True, exist_ok=True)
    (ce_root / "main.lua").write_text("require('defines')\n", encoding="utf-8")
    executable = ce_root / "Cheat Engine.exe"
    _write_fake_pe(executable)
    service.import_ce(str(executable))
    return service


def _nested_profile(service: PluginService, tmp_path: Path, app_id: int):
    source = tmp_path / f"nested-{app_id}.CT"
    source.write_text(NESTED_TABLE, encoding="utf-8")
    table = service.import_table(str(source))
    service.save_profile(app_id, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(app_id, table["sha256"], True)
    return table


def _inspection(service: PluginService, digest: str):
    return inspect_table(service.table_store.verified_blob(digest), digest)


def _profile(**overrides) -> GameProfile:
    base = dict(
        app_id=1, name="Game", is_shortcut=False, table_sha256="a" * 64,
        target_process="game.exe", execution_consent_sha256="a" * 64,
        autoload_enabled=True,
    )
    base.update(overrides)
    return GameProfile(**base)


def test_an_explicitly_off_cheat_does_not_write_its_configured_value_at_startup(tmp_path: Path):
    service = _service(tmp_path, "m26")
    table = _nested_profile(service, tmp_path, 920)
    inspection = _inspection(service, table["sha256"])
    profile = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(2, False, None), StartupPreference(3, True, None)],
        configured_values=[ConfiguredValue(2, "9999")],
    )

    actions = effective_startup_plan(profile, inspection)
    by_record = {(action.record_id, action.kind) for action in actions}
    assert (2, "value") not in by_record, "a dormant value must not be written for an explicitly-off cheat"
    assert (3, "active") in by_record, "the independent cheat must still be applied"
    # The enclosing script the applied cheat needs is still derived.
    assert (1, "active") in by_record


def test_a_script_that_switches_its_own_flags_on_has_them_held_off_at_startup(tmp_path: Path):
    """The table author's defaults are not the user's selection.

    One real table declares 22 of its 24 flags as on, so enabling the script the
    remembered cheat needs brings the rest of the table with it while the panel
    counts the one cheat. The panel holds them off for a press it makes itself;
    without this the next Auto-load puts them straight back.
    """
    service = _service(tmp_path, "m41")
    source = tmp_path / "flags.CT"
    source.write_text(FLAG_TABLE, encoding="utf-8")
    table = service.import_table(str(source))
    service.save_profile(930, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(930, table["sha256"], True)
    inspection = _inspection(service, table["sha256"])
    profile = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(2, True, "1")],
    )

    actions = effective_startup_plan(profile, inspection)
    by_record = {(action.record_id, action.kind): action for action in actions}
    # The script the chosen cheat needs, the chosen cheat itself with its own on
    # key, and the flag nobody asked for written to its off key.
    assert (1, "active") in by_record
    assert by_record[(2, "value")].value == "1"
    assert by_record[(3, "value")].value == "0"
    # Never switched, only written: a record that is off is off.
    assert (3, "active") not in by_record
    # And the write lands after the script that creates the record.
    order = [(action.record_id, action.kind) for action in actions]
    assert order.index((1, "active")) < order.index((3, "value"))


UNSAFE_FLAG_TABLE = """<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry>
      <ID>1</ID><Description>Enable</Description><VariableType>Auto Assembler Script</VariableType>
      <AssemblerScript>[ENABLE]
lblSafe:
cmp dword ptr [bEnableSafe],1
jne short lblSafeSkip
mulss xmm0,[fSafeMod]
lblSafeSkip:
jmp lblSafeRet
lblUnsafe:
sub rcx,rsi
cmp dword ptr [bEnableUnsafe],1
jne lblUnsafeSkip
add rcx,rsi
lblUnsafeSkip:
jmp lblUnsafeRet
bEnableSafe:
  dd 1
bEnableUnsafe:
  dd 1</AssemblerScript>
      <CheatEntries>
        <CheatEntry>
          <ID>2</ID><Description>Asked for</Description><VariableType>4 Bytes</VariableType>
          <Address>game.exe+1234</Address>
        </CheatEntry>
        <CheatEntry>
          <ID>3</ID><Description>bEnableSafe</Description><VariableType>4 Bytes</VariableType>
          <Address>bEnableSafe</Address>
          <DropDownList>0:Disabled
1:Enabled</DropDownList>
        </CheatEntry>
        <CheatEntry>
          <ID>4</ID><Description>bEnableUnsafe</Description><VariableType>4 Bytes</VariableType>
          <Address>bEnableUnsafe</Address>
          <DropDownList>0:Disabled
1:Enabled</DropDownList>
        </CheatEntry>
      </CheatEntries>
    </CheatEntry>
  </CheatEntries>
</CheatTable>
"""


def test_auto_load_holds_off_only_the_flags_whose_code_says_it_may(tmp_path: Path):
    """Apply and Auto-load put the same cheats on, so they may not disagree.

    A flag whose hook hands the game back an offset on the branch taken when it
    is off is one Apply leaves alone. Writing it at startup instead would make
    the guarantee depend on which of the two switched the script on, and the
    crash it causes arrives minutes later with nothing to connect it to a press.
    """
    service = _service(tmp_path, "m43")
    source = tmp_path / "unsafe-flags.CT"
    source.write_text(UNSAFE_FLAG_TABLE, encoding="utf-8")
    table = service.import_table(str(source))
    service.save_profile(932, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(932, table["sha256"], True)
    inspection = _inspection(service, table["sha256"])
    profile = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(2, True, None)],
    )

    actions = effective_startup_plan(profile, inspection)
    by_record = {(action.record_id, action.kind): action for action in actions}

    assert by_record[(3, "value")].value == "0", "the flag whose code was read stays held off"
    assert (4, "value") not in by_record, "the one that could not be proven stays on"
    assert (4, "active") not in by_record


def test_a_cheat_the_user_switched_off_keeps_its_own_choice_at_startup(tmp_path: Path):
    """Holding a script's defaults off may not overwrite an explicit selection."""
    service = _service(tmp_path, "m42")
    source = tmp_path / "flags-explicit.CT"
    source.write_text(FLAG_TABLE, encoding="utf-8")
    table = service.import_table(str(source))
    service.save_profile(931, "Game", False, table["sha256"], "game.exe")
    service.set_execution_consent(931, table["sha256"], True)
    inspection = _inspection(service, table["sha256"])
    profile = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(2, True, "1"), StartupPreference(3, True, "1")],
    )

    actions = effective_startup_plan(profile, inspection)
    by_record = {(action.record_id, action.kind): action for action in actions}
    assert by_record[(3, "value")].value == "1"
    assert (3, "active") in by_record


def test_an_orphan_off_action_is_never_asked_of_a_record_its_script_will_not_create(tmp_path: Path):
    service = _service(tmp_path, "m12")
    table = _nested_profile(service, tmp_path, 921)
    inspection = _inspection(service, table["sha256"])
    # Exactly what `Disable all` used to persist: everything explicitly off.
    profile = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(1, False, None), StartupPreference(2, False, None), StartupPreference(3, False, None)],
    )

    actions = effective_startup_plan(profile, inspection)
    assert {action.record_id for action in actions} == {1}, (
        "children whose enclosing script this startup leaves off can never materialize"
    )


def test_persistence_counts_the_enclosing_scripts_the_descriptor_will_add(tmp_path: Path):
    service = _service(tmp_path, "m18")
    table = _nested_profile(service, tmp_path, 922)
    inspection = _inspection(service, table["sha256"])
    profile = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(2, True, "1"), StartupPreference(3, True, "2")],
    )
    # Two records with an active state and a value are four explicit fields; the
    # descriptor also activates the script that creates them.
    assert len(effective_startup_plan(profile, inspection)) == 5


def test_only_the_cheats_a_plan_switches_on_can_prove_the_table_works(tmp_path: Path):
    service = _service(tmp_path, "m18b")
    table = _nested_profile(service, tmp_path, 927)
    inspection = _inspection(service, table["sha256"])
    profile = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(2, True, None), StartupPreference(3, True, None)],
    )
    actions = effective_startup_plan(profile, inspection)
    # The enclosing script is added by the expansion and is machinery; the two
    # cheats inside it are what a successful startup could prove.
    assert {action.record_id for action in actions if action.proof} == {2, 3}
    off = _profile(
        table_sha256=table["sha256"], execution_consent_sha256=table["sha256"],
        remembered=[StartupPreference(2, False, None)],
    )
    assert not any(action.proof for action in effective_startup_plan(off, inspection))


def test_the_startup_plan_validator_answers_before_any_runtime_mutation(tmp_path: Path):
    service = _service(tmp_path, "m19")
    table = _nested_profile(service, tmp_path, 923)
    digest = table["sha256"]
    service.set_configured_values(923, digest, [{"record_id": 2, "value": "5"}])

    plan = service.validate_effective_startup_plan(
        923, digest, [{"record_id": 3, "active": True, "value": None}], None,
    )
    # The already-persisted configured value and the implicit script both count,
    # which is exactly what the panel's own field count could not see.
    assert plan["fits"] is True
    assert plan["action_count"] == 3
    assert plan["limit"] == MAX_STARTUP_ACTIONS


def test_autoload_can_always_be_switched_off_even_with_the_table_blob_gone(tmp_path: Path):
    service = _service(tmp_path, "m14")
    table = _nested_profile(service, tmp_path, 924)
    digest = table["sha256"]
    service.set_autoload(924, digest, True)

    blob = Path(str(service.table_store.verified_blob(digest)))
    blob.unlink()
    with pytest.raises(ValueError):
        service.set_autoload(924, digest, True)

    profile = service.set_autoload(924, digest, False)
    assert profile["autoload_enabled"] is False


def test_a_target_seen_before_the_spawn_survives_a_reload(tmp_path: Path):
    """Durable target liveness, so a fast exit is not missed twice over.

    Only a target that was observed alive may later be proved gone. That fact
    lived in one supervisor's local variable, so a game that exited between two
    three-second scans - or during a plugin reload - reset it, after which
    absence was ignored for the rest of the session and an owned Cheat Engine
    outlived the game it was started for.
    """
    import json
    import logging as _logging

    from ce_decky.ce_launch import CELaunchSupervisor

    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    supervisor = CELaunchSupervisor(
        paths.user_home, paths.ce_root, paths.state_root, _logging.getLogger("target-seen"),
        display_resolver=lambda _home: ":0",
    )
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 5,
        "app_id": 700,
        "session_id": "123e4567-e89b-42d3-a456-426614174000",
        "pid": 4242,
        "pgid": 4242,
        "tool_id": "a" * 64,
        "executable": str((paths.ce_root / "runtime/cheatengine-x86_64.exe").absolute()),
        "descriptor_windows_path": "Z:\\s\\descriptor.txt",
        "descriptor_sha256": "d" * 64,
        "baseline_game_pids": [10],
        "bridge_sha256": "",
        "target_process": "game.exe",
        "target_seen": True,
        "started_at": 1.0,
    }
    (supervisor.launch_record_root / "700.json").write_text(json.dumps(record), encoding="utf-8")

    read = supervisor._read_record(700)
    assert read is not None and read["target_seen"] is True

    # A record written before this contract keeps the stricter behaviour.
    legacy = {key: value for key, value in record.items() if key != "target_seen"}
    legacy["schema"] = 4
    (supervisor.launch_record_root / "701.json").write_text(
        json.dumps({**legacy, "app_id": 701}), encoding="utf-8",
    )
    older = supervisor._read_record(701)
    assert older is not None and older["target_seen"] is False


def test_marking_the_target_seen_is_bound_to_the_exact_session(tmp_path: Path):
    import json
    import logging as _logging

    from ce_decky.ce_launch import CELaunchSupervisor

    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    supervisor = CELaunchSupervisor(
        paths.user_home, paths.ce_root, paths.state_root, _logging.getLogger("target-seen-exact"),
        display_resolver=lambda _home: ":0",
    )
    supervisor.launch_record_root.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": 5, "app_id": 702, "session_id": "123e4567-e89b-42d3-a456-426614174000",
        "pid": 4242, "pgid": 4242, "tool_id": "a" * 64,
        "executable": str((paths.ce_root / "runtime/cheatengine-x86_64.exe").absolute()),
        "descriptor_windows_path": "Z:\\s\\descriptor.txt", "descriptor_sha256": "d" * 64,
        "baseline_game_pids": [10], "bridge_sha256": "", "target_process": "game.exe",
        "target_seen": False, "started_at": 1.0,
    }
    (supervisor.launch_record_root / "702.json").write_text(json.dumps(record), encoding="utf-8")

    # A different session must never mark this record.
    supervisor._mark_target_seen(702, "00000000-0000-4000-8000-000000000000")
    assert supervisor._read_record(702)["target_seen"] is False

    supervisor._mark_target_seen(702, "123e4567-e89b-42d3-a456-426614174000")
    assert supervisor._read_record(702)["target_seen"] is True


def test_the_legacy_startup_rpc_is_budgeted_against_the_expanded_plan(tmp_path: Path):
    """The old startup path skipped the exact expanded-plan guard.

    Remembered and configured writes count the enclosing scripts the descriptor
    will add; this one counted stored fields only, so a preference whose record
    needs an ancestor could be accepted and then make session preparation
    impossible.
    """
    service = _service(tmp_path, "r7")
    table = _nested_profile(service, tmp_path, 930)
    digest = table["sha256"]
    inspection = _inspection(service, digest)

    # The nested child needs its enclosing script, so the plan is larger than
    # the single field being stored.
    service.set_startup_preference(930, digest, 2, True, None)
    profile = service.profile_store.get(930)
    plan = effective_startup_plan(profile, inspection)
    assert {action.record_id for action in plan} == {1, 2}

    # With the ceiling lowered to the size of the stored fields alone, the
    # implicit ancestor is what pushes it over and the write is refused.
    import ce_decky.service as service_module

    original = service_module.MAX_STARTUP_ACTIONS
    service_module.MAX_STARTUP_ACTIONS = 1
    try:
        with pytest.raises(ValueError, match="expands to"):
            service.set_startup_preference(930, digest, 3, True, None)
    finally:
        service_module.MAX_STARTUP_ACTIONS = original

    # The refused preference was not persisted.
    assert {item.record_id for item in service.profile_store.get(930).startup} == {2}


READ_ONLY_EMPTY_TABLE = """<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry>
      <ID>1</ID><Description>Locked</Description><VariableType>4 Bytes</VariableType>
      <Address>game.exe+1234</Address>
      <DropDownReadOnly>1</DropDownReadOnly>
    </CheatEntry>
  </CheatEntries>
</CheatTable>
"""


def test_a_read_only_record_with_no_declared_values_accepts_none_of_them(tmp_path: Path):
    """A record that declares itself read-only accepts only what it declares.

    Classifying it by whether the list happened to be non-empty made an empty
    one a plain value control: the picker offered a text field, and every
    backend guard was written as `read_only and values`, whose second half was
    false, so any value was accepted into the startup descriptor and the live
    command stream.
    """
    service = _service(tmp_path, "m5")
    source = tmp_path / "read-only.CT"
    source.write_text(READ_ONLY_EMPTY_TABLE, encoding="utf-8")
    table = service.import_table(str(source))
    digest = table["sha256"]
    service.save_profile(940, "Game", False, digest, "game.exe")
    service.set_execution_consent(940, digest, True)

    control = next(item for item in _inspection(service, digest).controls if item.id == 1)
    assert control.dropdown_read_only is True
    assert control.dropdown_values == ()
    # The frontend renders a text field for `value` and only a chooser for a
    # dropdown, so the classification is what keeps it non-editable.
    assert control.kind == "dropdown"

    # The live path validates the value only once it has a prepared session to
    # validate it against.
    service.prepare_session(940)
    for write in (
        lambda: service.set_startup_preference(940, digest, 1, None, "999"),
        lambda: service.set_remembered_cheats(940, digest, [{"record_id": 1, "active": None, "value": "999"}]),
        lambda: service.set_configured_values(940, digest, [{"record_id": 1, "value": "999"}]),
        lambda: service.write_runtime_commands(940, [{"generation": 1, "kind": "set_value", "record_id": 1, "value": "999"}]),
    ):
        with pytest.raises(ValueError, match="read-only dropdown"):
            write()
