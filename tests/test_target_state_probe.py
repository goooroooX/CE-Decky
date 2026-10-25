from __future__ import annotations

from pathlib import Path

import pytest

from ce_decky.profiles import ProfileStore
from ce_decky.session_protocol import PreparedSession, RuntimeStatus
from scripts import target_state_probe


def _fake_process(proc: Path, pid: int, *entries: bytes) -> None:
    root = proc / str(pid)
    root.mkdir(parents=True)
    (root / "environ").write_bytes(b"\0".join(entries) + b"\0")


def test_target_state_probe_is_bounded_read_only_and_hides_installer_marker(tmp_path: Path):
    home = tmp_path / "home"
    settings = tmp_path / "settings"
    proc = tmp_path / "proc"
    home.mkdir()
    settings.mkdir()
    proc.mkdir()
    state = home / ".cheat-engine-decky" / "state"
    state.mkdir(parents=True)

    # Loading the default config is enough to prove the probe does not create or
    # rewrite config state on its own.
    config_path = settings / "config.json"
    before_config_exists = config_path.exists()
    ProfileStore(state / "profiles.json").upsert(
        app_id=77,
        name="Safe Game",
        is_shortcut=False,
        table_sha256=None,
        target_process=None,
    )
    _fake_process(
        proc,
        123,
        b"CE_DECKY_MANAGED_INSTALLER=do-not-emit-this-value",
        b"UNRELATED_SECRET=must-not-leak",
    )

    report = target_state_probe.probe(home, settings, 77, proc)

    assert report["schema"] == 1
    assert report["config"]["configured"] is False
    assert report["profile"]["app_id"] == 77
    assert report["runtime"]["state"] == "absent"
    assert report["owned_ce_launch"]["state"] == "absent"
    assert report["managed_installer_processes"] == [{"pid": 123, "pgid": 123}]
    assert config_path.exists() is before_config_exists
    serialized = str(report)
    assert "do-not-emit-this-value" not in serialized
    assert "must-not-leak" not in serialized


def test_target_state_probe_reports_profile_parse_failure_as_evidence(tmp_path: Path):
    home = tmp_path / "home"
    settings = tmp_path / "settings"
    proc = tmp_path / "proc"
    home.mkdir()
    settings.mkdir()
    proc.mkdir()
    state = home / ".cheat-engine-decky" / "state"
    state.mkdir(parents=True)
    (state / "profiles.json").write_text("{broken", encoding="utf-8")

    report = target_state_probe.probe(home, settings, 9, proc)

    assert report["profile_count"] == 0
    assert report["profile_state_error"]
    assert report["profile"] is None
    assert report["runtime"]["state"] == "absent"


class _RuntimeStoreStub:
    def __init__(self, prepared: PreparedSession, status: RuntimeStatus, target_process: str):
        self.prepared = prepared
        self.status = status
        self.target_process = target_process

    def load_current(self, app_id: int):
        assert app_id == self.prepared.app_id
        return self.prepared

    def validated_descriptor(self, prepared: PreparedSession):
        assert prepared == self.prepared
        return type("Descriptor", (), {"target_process": self.target_process})()

    def read_status_observation(self, prepared: PreparedSession):
        assert prepared == self.prepared
        import time
        return self.status, time.time_ns()

    def next_generation(self, prepared: PreparedSession):
        assert prepared == self.prepared
        return 7


def _runtime_fixture(tmp_path: Path, *, is_shortcut: bool | None = False):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    profiles = ProfileStore(state / "profiles.json")
    profile = profiles.upsert(
        app_id=77,
        name="Safe Game",
        is_shortcut=False,
        table_sha256="b" * 64,
        target_process="game.exe",
    )
    profile = profiles.set_execution_consent(app_id=77, table_sha256="b" * 64, consent=True)
    prepared = PreparedSession(
        session_id="123e4567-e89b-42d3-a456-426614174000",
        app_id=77,
        ce_sha256="c" * 64,
        table_sha256="b" * 64,
        descriptor_path=str(tmp_path / "descriptor.txt"),
        descriptor_sha256="d" * 64,
        control_path=str(tmp_path / "control.txt"),
        status_path=str(tmp_path / "status.txt"),
        descriptor_windows_path="Z:\\safe\\descriptor.txt",
        descriptor_md5="e" * 32,
        is_shortcut=is_shortcut,
    )
    status = RuntimeStatus(
        prepared.session_id, 77, "c" * 64, "b" * 64, "d" * 64, 1, True, "game.exe", 4242, (), (), 17, "loaded"
    )
    return profile, prepared, status


def test_target_state_probe_marks_legacy_prepared_identity_stale(tmp_path: Path):
    profile, prepared, status = _runtime_fixture(tmp_path, is_shortcut=None)
    report = target_state_probe._runtime_snapshot(
        _RuntimeStoreStub(prepared, status, "game.exe"), profile, "c" * 64, 77, {"state": "matched", "record": None}
    )
    assert report["identity_current"] is False
    assert report["connected"] is False
    assert report["state"] == "stale"
    assert report["status"]["address_list_count"] == 17
    assert report["status"]["table_load_state"] == "loaded"


def test_target_state_probe_does_not_call_a_proven_gone_owned_launch_connected(tmp_path: Path):
    profile, prepared, status = _runtime_fixture(tmp_path)
    report = target_state_probe._runtime_snapshot(
        _RuntimeStoreStub(prepared, status, "game.exe"),
        profile,
        "c" * 64,
        77,
        {"state": "gone_or_reused", "record": {"session_id": prepared.session_id}},
    )
    assert report["identity_current"] is True
    assert report["status_fresh"] is True
    assert report["connected"] is False
    assert report["terminal_reason"] == "owned_bridge_process_gone"


def _target_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    settings = tmp_path / "settings"
    home.mkdir()
    settings.mkdir()
    (home / ".cheat-engine-decky" / "state").mkdir(parents=True)
    (tmp_path / "proc").mkdir(exist_ok=True)
    return home, settings


def _game_process(proc_root: Path, pid: int, app_id: int) -> None:
    directory = proc_root / str(pid)
    directory.mkdir(parents=True)
    (directory / "environ").write_bytes(f"SteamAppId={app_id}\x00PATH=/usr/bin\x00".encode())
    (directory / "cmdline").write_bytes(b"/usr/bin/game\x00")


def test_the_report_says_which_appids_are_running(tmp_path: Path):
    """Without it there is no tracked route from a running game to its profile.

    The probe correlates by AppID, and the AppID was the one thing nothing
    tracked could answer, so finding one meant an ad-hoc snippet against the
    production module every time a measurement needed it.
    """
    home, settings = _target_home(tmp_path)
    proc_root = tmp_path / "proc"
    _game_process(proc_root, 4321, app_id=620)

    report = target_state_probe.probe(home, settings, None, proc_root)

    assert report["running_app_ids"] == [620]
    assert report["running_app_ids_error"] is None


def test_every_profile_says_what_it_selects_without_being_asked_for_one(tmp_path: Path):
    home, settings = _target_home(tmp_path)
    store = ProfileStore(home / ".cheat-engine-decky" / "state" / "profiles.json")
    store.upsert(app_id=620, name="A", is_shortcut=False, table_sha256="a" * 64, target_process="a.exe")
    store.upsert(app_id=621, name="B", is_shortcut=False, table_sha256="b" * 64, target_process="b.exe")

    report = target_state_probe.probe(home, settings, None, tmp_path / "proc")

    # A count answers nothing a setup question asks.
    assert report["profile_count"] == 2
    assert report["profiles_reported"] == 2
    assert {row["app_id"]: row["table_sha256"] for row in report["profiles"]} == {
        620: "a" * 64, 621: "b" * 64,
    }
    assert all(row["target_process"] for row in report["profiles"])


def test_the_profile_list_is_bounded(tmp_path: Path, monkeypatch):
    home, settings = _target_home(tmp_path)
    store = ProfileStore(home / ".cheat-engine-decky" / "state" / "profiles.json")
    for index in range(6):
        store.upsert(
            app_id=700 + index, name=f"G{index}", is_shortcut=False,
            table_sha256=f"{index:064d}", target_process="g.exe",
        )
    monkeypatch.setattr(target_state_probe, "MAX_REPORTED_PROFILES", 4)

    report = target_state_probe.probe(home, settings, None, tmp_path / "proc")

    assert report["profile_count"] == 6
    assert report["profiles_reported"] == 4
    assert len(report["profiles"]) == 4


def test_a_profile_store_that_cannot_be_read_reports_no_profiles_and_says_why(tmp_path: Path):
    home, settings = _target_home(tmp_path)
    (home / ".cheat-engine-decky" / "state" / "profiles.json").write_text("{ not json", encoding="utf-8")

    report = target_state_probe.probe(home, settings, None, tmp_path / "proc")

    assert report["profiles"] == []
    assert report["profile_state_error"]


def test_the_running_game_is_inside_the_bound_however_its_name_sorts(tmp_path: Path):
    """The cap must not sort out the one profile the report was reached for.

    Profiles come back sorted by name and the cap took the first of them, so a
    device with more profiles than the cap could report an AppID as running and
    omit that AppID's profile because its name sorts late. That is exactly the
    second call this field exists to remove.
    """
    home, settings = _target_home(tmp_path)
    store = ProfileStore(home / ".cheat-engine-decky" / "state" / "profiles.json")
    for index in range(6):
        store.upsert(
            app_id=800 + index, name=f"A{index}", is_shortcut=False,
            table_sha256=f"{index:064d}", target_process="a.exe",
        )
    store.upsert(app_id=900, name="zzz last by name", is_shortcut=False,
                 table_sha256="f" * 64, target_process="z.exe")
    proc_root = tmp_path / "proc"
    _game_process(proc_root, 4321, app_id=900)

    report = target_state_probe.probe(home, settings, None, proc_root)

    reported = [row["app_id"] for row in report["profiles"]]
    assert report["running_app_ids"] == [900]
    assert reported[0] == 900, "the running game leads the bounded list"
    assert len(reported) == len(set(reported)), "no profile is reported twice"
    assert report["profiles_reported"] == len(reported)


def test_the_bound_still_holds_with_the_running_game_inside_it(tmp_path: Path, monkeypatch):
    home, settings = _target_home(tmp_path)
    store = ProfileStore(home / ".cheat-engine-decky" / "state" / "profiles.json")
    for index in range(8):
        store.upsert(
            app_id=800 + index, name=f"A{index}", is_shortcut=False,
            table_sha256=f"{index:064d}", target_process="a.exe",
        )
    proc_root = tmp_path / "proc"
    _game_process(proc_root, 4321, app_id=807)
    monkeypatch.setattr(target_state_probe, "MAX_REPORTED_PROFILES", 3)

    report = target_state_probe.probe(home, settings, None, proc_root)

    reported = [row["app_id"] for row in report["profiles"]]
    assert len(reported) == 3
    assert reported[0] == 807
    assert report["profile_count"] == 8
    assert report["profiles_reported"] == 3


def test_an_unreadable_profile_store_claims_no_profile_for_a_running_game(tmp_path: Path):
    """A missing profile and an unreadable store are different answers."""
    home, settings = _target_home(tmp_path)
    (home / ".cheat-engine-decky" / "state" / "profiles.json").write_text("{ not json", encoding="utf-8")
    proc_root = tmp_path / "proc"
    _game_process(proc_root, 4321, app_id=900)

    report = target_state_probe.probe(home, settings, None, proc_root)

    assert report["running_app_ids"] == [900]
    assert report["profiles"] == []
    assert report["profiles_reported"] == 0
    assert report["profile_state_error"]


def test_the_report_says_which_games_a_stop_holds(tmp_path: Path):
    """The holds a stop takes are what the device checks after one, and nothing else read them."""
    from ce_decky.game_run_holds import GameRunHolds, new_hold
    home, settings = _target_home(tmp_path)
    state = home / ".cheat-engine-decky" / "state"
    state.mkdir(parents=True, exist_ok=True)
    GameRunHolds(state / "game_run_holds.json").save({
        10: {"dirty": new_hold(10, "dirty", ("game.exe",), None, 2), "stopped": new_hold(10, "stopped", ("game.exe",), None)},
    })
    report = target_state_probe.probe(home, settings, None, tmp_path / "proc")
    assert [(item["app_id"], item["kind"], item["unsettled"], item["targets"]) for item in report["run_holds"]] == [
        (10, "dirty", 2, ["game.exe"]), (10, "stopped", 0, ["game.exe"]),
    ]
    assert report["run_holds_error"] is None
    # A file the backend refuses every start on is said to be one.
    (state / "game_run_holds.json").write_text('{"schema": 99}')
    report = target_state_probe.probe(home, settings, None, tmp_path / "proc")
    assert report["run_holds"] == [] and report["run_holds_error"]


def test_the_appid_profile_names_each_remembered_record_and_pin(tmp_path: Path):
    # Which cheats the next Auto-load replays is each remembered record's own
    # state and value; counts alone sent that question to profiles.json by hand.
    from ce_decky.profiles import StartupPreference

    home, settings, proc = tmp_path / "home", tmp_path / "settings", tmp_path / "proc"
    for path in (home, settings, proc):
        path.mkdir()
    state = home / ".cheat-engine-decky" / "state"
    state.mkdir(parents=True)
    store = ProfileStore(state / "profiles.json")
    sha = "a" * 64
    store.upsert(app_id=77, name="Safe Game", is_shortcut=False, table_sha256=sha, target_process=None)
    store.set_remembered(app_id=77, table_sha256=sha, states=[StartupPreference(5, True, None), StartupPreference(6, False, "100")])
    store.set_pinned(app_id=77, table_sha256=sha, record_id=5, pinned=True)

    report = target_state_probe.probe(home, settings, 77, proc)

    assert report["profile"]["remembered"] == [
        {"record_id": 5, "active": True, "value": None},
        {"record_id": 6, "active": False, "value": "100"},
    ]
    assert report["profile"]["pinned"] == [5]
    assert report["profile"]["configured_values"] == []
    assert report["profile"]["startup"] == []
    assert "remembered" not in report["profiles"][0]


PLAN_TABLE = """<?xml version="1.0" encoding="utf-8"?>
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


def test_the_startup_plan_is_the_planners_own_record_by_record(tmp_path: Path):
    # Which values Auto-load writes and which scripts it starts was answered by
    # a count, and the rest was reasoned out by hand from the profile.
    from ce_decky.profiles import ConfiguredValue, StartupPreference
    from ce_decky.table_store import TableStore

    home, settings, proc = tmp_path / "home", tmp_path / "settings", tmp_path / "proc"
    for path in (home, settings, proc):
        path.mkdir()
    managed = home / ".cheat-engine-decky"
    (managed / "state").mkdir(parents=True)
    source = tmp_path / "plan.CT"
    source.write_text(PLAN_TABLE, encoding="utf-8")
    sha = TableStore(managed / "tables").import_ct(str(source)).sha256
    store = ProfileStore(managed / "state" / "profiles.json")
    store.upsert(app_id=78, name="Safe Game", is_shortcut=False, table_sha256=sha, target_process=None)
    store.set_remembered(app_id=78, table_sha256=sha, states=[StartupPreference(3, True, "5"), StartupPreference(2, False, None)])
    store.set_configured_values(app_id=78, table_sha256=sha, values=[ConfiguredValue(2, "250")])

    report = target_state_probe.probe(home, settings, 78, proc, startup_plan=True)

    plan = report["startup_plan"]
    assert plan["error"] is None and plan["autoload_enabled"] is False
    steps = [(item["record_id"], item["kind"], item["value"]) for item in plan["actions"]]
    assert (1, "active", "1") in steps
    assert (3, "value", "5") in steps and (3, "active", "1") in steps
    assert (2, "value", "250") in steps, "a setting typed in Configure under a script startup runs"
    assert plan["action_count"] == len(steps)
    assert next(item["label"] for item in plan["actions"] if item["record_id"] == 3) == "Enable › Ammo"
    assert report["profile"]["configured_values"] == [{"record_id": 2, "value": "250"}]
    assert plan["left_on"] == []

    # Without the flag the table is not read at all.
    assert "startup_plan" not in target_state_probe.probe(home, settings, 78, proc)


def test_a_startup_plan_for_a_table_the_store_does_not_hold_says_so(tmp_path: Path):
    home, settings, proc = tmp_path / "home", tmp_path / "settings", tmp_path / "proc"
    for path in (home, settings, proc):
        path.mkdir()
    state = home / ".cheat-engine-decky" / "state"
    state.mkdir(parents=True)
    ProfileStore(state / "profiles.json").upsert(
        app_id=79, name="Safe Game", is_shortcut=False, table_sha256="b" * 64, target_process=None,
    )

    plan = target_state_probe.probe(home, settings, 79, proc, startup_plan=True)["startup_plan"]

    assert plan["actions"] == [] and plan["error"]


def test_omitted_paths_come_from_the_install_authority(monkeypatch, tmp_path: Path):
    from scripts import target_plugin_install

    monkeypatch.setattr(target_plugin_install, "resolve_install_authority", lambda: {
        "source": "live_backend", "user_home": str(tmp_path / "home"), "settings_dir": str(tmp_path / "settings"),
    })

    assert target_state_probe._paths(None, None) == (tmp_path / "home", tmp_path / "settings", "authority:live_backend")
    # A path given on the command line wins over the authority's.
    assert target_state_probe._paths(tmp_path / "mine", None)[0] == tmp_path / "mine"
    assert target_state_probe._paths(tmp_path / "a", tmp_path / "b") == (tmp_path / "a", tmp_path / "b", "arguments")

    monkeypatch.setattr(target_plugin_install, "resolve_install_authority", lambda: {"source": "recorded_install", "user_home": None, "settings_dir": None})
    with pytest.raises(ValueError, match="pass --home"):
        target_state_probe._paths(None, None)


def test_every_profile_says_whether_this_device_holds_its_game(tmp_path: Path, monkeypatch):
    """A profile outlives its game, so the list is not a list of what can be started here."""
    home, settings = _target_home(tmp_path)
    store = ProfileStore(home / ".cheat-engine-decky" / "state" / "profiles.json")
    store.upsert(app_id=620, name="A", is_shortcut=False, table_sha256=None, target_process=None)
    store.upsert(app_id=621, name="B", is_shortcut=False, table_sha256=None, target_process=None)
    store.upsert(app_id=3000000123, name="C", is_shortcut=True, table_sha256=None, target_process=None)
    store.upsert(app_id=3000000124, name="D", is_shortcut=True, table_sha256=None, target_process=None)
    monkeypatch.setattr(target_state_probe, "local_library", lambda _home: {
        "steam_app_ids": [620], "unstartable_app_ids": [], "shortcut_app_ids": [3000000123],
        "reason": None, "shortcuts_reason": None,
    })
    monkeypatch.setattr(target_state_probe, "shortcut_program", lambda _home, app_id: "/store/bin/launcher")

    report = target_state_probe.probe(home, settings, 3000000123, tmp_path / "proc")

    rows = {row["app_id"]: row for row in report["profiles"]}
    assert {app_id: row["on_device"] for app_id, row in rows.items()} == {
        620: True, 621: False, 3000000123: True, 3000000124: False,
    }
    assert rows[3000000123]["starts"] == "/store/bin/launcher" and rows[3000000124]["starts"] is None
    assert rows[3000000123]["starts_the_game"] is False
    assert "starts" not in rows[620]
    assert report["profile"]["on_device"] is True and report["profile"]["starts"] == "/store/bin/launcher"

    # Read in part: what was listed is there, and an absence is not an answer.
    monkeypatch.setattr(target_state_probe, "local_library", lambda _home: {
        "steam_app_ids": [620], "unstartable_app_ids": [], "shortcut_app_ids": [3000000123],
        "reason": "partial", "shortcuts_reason": "partial",
    })
    rows = {row["app_id"]: row["on_device"] for row in target_state_probe.probe(home, settings, None, tmp_path / "proc")["profiles"]}
    assert rows == {620: True, 621: None, 3000000123: True, 3000000124: None}


def test_every_profile_says_which_of_its_tables_are_held_worked_and_do_not_work(tmp_path: Path, monkeypatch):
    """Which game a device check can use turns on these, and reading three state files by hand was the route."""
    from ce_decky.table_blocklist import CAUSE_REFUSED, TableBlocklist
    from ce_decky.table_compatibility import TableCompatibility

    home, settings = _target_home(tmp_path)
    state = home / ".cheat-engine-decky" / "state"
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    store = ProfileStore(state / "profiles.json")
    for digest in (a, b, c):
        store.upsert(app_id=620, name="A", is_shortcut=False, table_sha256=digest, target_process="a.exe")
    compatibility = TableCompatibility(state / "table_compatibility.json")
    for digest in (a, b):
        compatibility.record(620, digest, "a.exe", {"pe_version": "1.0"}, failure_epoch="0" * 32)
    # A failure recorded after b worked is what undoes b's record.
    TableBlocklist(state / "blocked_tables.json").block(sha256=b, reason="refused", app_id=620, cause=CAUSE_REFUSED)
    monkeypatch.setattr(target_state_probe.TableStore, "list_tables",
                        lambda self: [{"sha256": a, "filename": "a.CT"}, {"sha256": b, "filename": "b.CT"}])

    report = target_state_probe.probe(home, settings, 620, tmp_path / "proc")

    assert report["table_evidence_errors"] == {}
    row = report["profiles"][0]
    assert (row["tables_held"], row["tables_worked"], row["tables_not_working"]) == (2, 1, 1)
    tables = {item["sha256"]: item for item in report["profile"]["tables"]}
    assert tables[a]["filename"] == "a.CT" and tables[a]["worked"]["pe_version"] == "1.0" and tables[a]["not_working"] is None
    assert tables[b]["worked"] is None and tables[b]["not_working"] == CAUSE_REFUSED
    assert tables[c] == {"sha256": c, "filename": None, "held": False, "worked": None, "not_working": None}


def test_a_state_file_that_cannot_be_read_gives_no_count_rather_than_zero(tmp_path: Path):
    home, settings = _target_home(tmp_path)
    state = home / ".cheat-engine-decky" / "state"
    ProfileStore(state / "profiles.json").upsert(app_id=620, name="A", is_shortcut=False, table_sha256="a" * 64, target_process=None)
    state.mkdir(parents=True, exist_ok=True)
    (state / "table_compatibility.json").write_text("{", encoding="utf-8")

    report = target_state_probe.probe(home, settings, None, tmp_path / "proc")

    assert "table_compatibility" in report["table_evidence_errors"]
    assert report["profiles"][0]["tables_worked"] is None
