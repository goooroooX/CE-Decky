from __future__ import annotations

from pathlib import Path

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
