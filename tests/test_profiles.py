from pathlib import Path
import json

import pytest

from ce_decky.profiles import ConfiguredValue, ProfileStore, StartupPreference

SHA = "a" * 64
SHA2 = "b" * 64


def test_revoke_detaches_once_and_never_restores_authorization(tmp_path: Path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=10, name="Removed game", is_shortcut=True, table_sha256=SHA, target_process="game.exe")
    store.set_execution_consent(app_id=10, table_sha256=SHA, consent=True)
    store.set_autoload(app_id=10, table_sha256=SHA, enabled=True)
    saves = []
    save = store._save
    monkeypatch.setattr(store, "_save", lambda profiles: (saves.append(True), save(profiles)))
    revoked = store.revoke_table(app_id=10, table_sha256=SHA)
    assert revoked.table_sha256 is None
    assert revoked.execution_consent_sha256 is None
    assert not revoked.autoload_enabled
    assert revoked.table_library == [SHA]
    assert not revoked.table_history[SHA].execution_consent
    assert store.revoke_table(app_id=10, table_sha256=SHA) == revoked
    assert len(saves) == 1
    restored = store.upsert(app_id=10, name="Removed game", is_shortcut=True, table_sha256=SHA, target_process="game.exe")
    assert restored.execution_consent_sha256 is None
    assert not restored.autoload_enabled


def test_revoke_refuses_a_changed_selection(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=10, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")
    with pytest.raises(ValueError, match="exact selected table SHA"):
        store.revoke_table(app_id=10, table_sha256=SHA)
    assert store.get(10).table_sha256 == SHA2


def test_profile_crud_and_exact_sha_startup_binding(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    profile = store.upsert(app_id=123, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    assert profile.table_sha256 == SHA
    profile = store.set_startup(app_id=123, table_sha256=SHA, record_id=7, active=True, value="100")
    assert profile.startup[0].record_id == 7
    assert profile.startup[0].active is True
    reloaded = ProfileStore(tmp_path / "profiles.json").get(123)
    assert reloaded is not None and reloaded.startup[0].value == "100"

    changed = store.upsert(app_id=123, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")
    assert changed.startup == []
    with pytest.raises(ValueError, match="exact selected table SHA"):
        store.set_startup(app_id=123, table_sha256=SHA, record_id=7, active=True, value=None)


def test_profile_rpc_types_fail_closed_including_bool_as_int(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    for bad in [True, False, 0, -1, 2**40, "12"]:
        with pytest.raises(ValueError, match="AppID"):
            store.upsert(app_id=bad, name="Game", is_shortcut=False, table_sha256=None, target_process=None)  # type: ignore[arg-type]
    store.upsert(app_id=1, name="Game", is_shortcut=True, table_sha256=SHA, target_process=None)
    with pytest.raises(ValueError, match="MemoryRecord"):
        store.set_startup(app_id=1, table_sha256=SHA, record_id=True, active=True, value=None)  # type: ignore[arg-type]


def test_corrupt_persistent_profile_state_is_not_silently_repaired(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"schema": 1, "profiles": [{"app_id": 1, "name": "A", "is_shortcut": False}, {"app_id": 1, "name": "B", "is_shortcut": False}]}))
    with pytest.raises(ValueError, match="duplicate AppID"):
        ProfileStore(path).list_profiles()


def test_target_process_is_basename_only(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    for bad in ["../game.exe", "C:\\game.exe", "game", ""]:
        if bad == "":
            store.upsert(app_id=1, name="Game", is_shortcut=False, table_sha256=None, target_process=bad)
        else:
            with pytest.raises(ValueError, match="basename"):
                store.upsert(app_id=1, name="Game", is_shortcut=False, table_sha256=None, target_process=bad)


def test_profile_schema_1_loads_and_migrates_on_save(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"schema": 1, "profiles": [{"app_id": 9, "name": "Legacy", "is_shortcut": False}]}))
    store = ProfileStore(path)
    assert store.get(9) is not None
    store.upsert(app_id=9, name="Legacy", is_shortcut=False, table_sha256=None, target_process=None)
    assert json.loads(path.read_text())["schema"] == 5


def test_legacy_null_launch_ownership_loads_and_is_not_written_back(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "schema": 4,
        "profiles": [{
            "app_id": 9, "name": "Legacy", "is_shortcut": False,
            "table_sha256": None, "target_process": None, "execution_consent_sha256": None,
            "startup": [], "pinned": [], "previous_table_sha256": None, "table_history": {},
            "table_library": [], "autoload_enabled": False, "remembered": [],
            "launch_ownership": None,
        }],
    }))
    store = ProfileStore(path)
    profile = store.get(9)
    assert profile is not None
    assert "launch_ownership" not in profile.as_dict()
    store.upsert(app_id=9, name="Legacy", is_shortcut=False, table_sha256=None, target_process=None)
    assert "launch_ownership" not in json.loads(path.read_text())["profiles"][0]


def test_profile_recording_removed_steam_launch_ownership_fails_closed(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "schema": 4,
        "profiles": [{
            "app_id": 9, "name": "Legacy", "is_shortcut": False,
            "table_sha256": None, "target_process": None, "execution_consent_sha256": None,
            "startup": [], "pinned": [], "previous_table_sha256": None, "table_history": {},
            "table_library": [], "autoload_enabled": False, "remembered": [],
            "launch_ownership": {"is_shortcut": False, "source_before": "", "source_after": "", "slots": []},
        }],
    }))
    with pytest.raises(ValueError, match="removed feature"):
        ProfileStore(path).list_profiles()


def test_per_sha_preferences_consent_and_pins_restore_when_switching_back(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=55, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_startup(app_id=55, table_sha256=SHA, record_id=3, active=True, value=None)
    store.set_pinned(app_id=55, table_sha256=SHA, record_id=3, pinned=True)
    store.set_execution_consent(app_id=55, table_sha256=SHA, consent=True)

    other = store.upsert(app_id=55, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")
    assert other.startup == []
    assert other.pinned == []
    assert other.execution_consent_sha256 is None
    assert other.previous_table_sha256 == SHA
    assert SHA in other.table_history

    store.set_pinned(app_id=55, table_sha256=SHA2, record_id=8, pinned=True)
    restored = store.upsert(app_id=55, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    assert [item.record_id for item in restored.startup] == [3]
    assert restored.pinned == [3]
    assert restored.execution_consent_sha256 == SHA
    assert restored.previous_table_sha256 == SHA2
    assert SHA2 in restored.table_history


def test_a_table_cleared_from_a_game_can_be_selected_again_offline(tmp_path: Path):
    """The exact sequence a user reaches by answering the refusal dialog.

    **Stop using it** clears the selection and records that table as the one
    this game came from, so it can be chosen again without hunting for it. The
    user then clears the mark under Advanced and loads the same file, which is
    the only way back for somebody with no network and no other table: the
    previous pointer named the table being selected, and the write was refused
    on the way to disk with the plugin telling them their own downloaded table
    could not be selected.
    """
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=7, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_pinned(app_id=7, table_sha256=SHA, record_id=4, pinned=True)
    store.set_execution_consent(app_id=7, table_sha256=SHA, consent=True)

    cleared = store.upsert(app_id=7, name="Game", is_shortcut=False, table_sha256=None, target_process="game.exe")
    assert cleared.table_sha256 is None
    assert cleared.previous_table_sha256 == SHA

    again = store.upsert(app_id=7, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    assert again.table_sha256 == SHA
    # Nothing to go back to: the table this game came from is the one it is on.
    assert again.previous_table_sha256 is None
    # Everything archived against those exact bytes comes back with them.
    assert again.pinned == [4]
    assert again.execution_consent_sha256 == SHA
    assert SHA not in again.table_history
    assert SHA in again.table_library
    # Readable on the next load, which is what the refusal was protecting.
    reloaded = ProfileStore(tmp_path / "profiles.json").get(7)
    assert reloaded is not None and reloaded.table_sha256 == SHA


def test_a_cleared_selection_still_reaches_a_different_table(tmp_path: Path):
    """The pointer only goes when the table it names is the one being selected.

    Clearing a selection and then choosing some other table must leave the
    first one reversible, which is the whole reason `previous` survives a
    cleared selection.
    """
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=8, name="Game", is_shortcut=False, table_sha256=SHA, target_process=None)
    store.upsert(app_id=8, name="Game", is_shortcut=False, table_sha256=None, target_process=None)
    other = store.upsert(app_id=8, name="Game", is_shortcut=False, table_sha256=SHA2, target_process=None)
    assert other.previous_table_sha256 == SHA
    assert SHA in other.table_library


def test_pinned_controls_are_exact_sha_bound_and_deduplicated(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=4, name="Game", is_shortcut=False, table_sha256=SHA, target_process=None)
    assert store.set_pinned(app_id=4, table_sha256=SHA, record_id=9, pinned=True).pinned == [9]
    assert store.set_pinned(app_id=4, table_sha256=SHA, record_id=9, pinned=True).pinned == [9]
    assert store.set_pinned(app_id=4, table_sha256=SHA, record_id=7, pinned=True).pinned == [7, 9]
    with pytest.raises(ValueError, match="exact selected table SHA"):
        store.set_pinned(app_id=4, table_sha256=SHA2, record_id=1, pinned=True)
    assert store.set_pinned(app_id=4, table_sha256=SHA, record_id=9, pinned=False).pinned == [7]
    assert store.clear_pinned(app_id=4, table_sha256=SHA).pinned == []


def test_new_startup_control_is_pinned_once_but_explicit_unpin_survives_edits(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=5, name="Game", is_shortcut=False, table_sha256=SHA, target_process=None)

    created = store.set_startup(app_id=5, table_sha256=SHA, record_id=7, active=True, value=None)
    assert created.pinned == [7]

    assert store.set_pinned(app_id=5, table_sha256=SHA, record_id=7, pinned=False).pinned == []
    edited = store.set_startup(app_id=5, table_sha256=SHA, record_id=7, active=False, value=None)
    assert edited.pinned == []

    store.clear_startup(app_id=5, table_sha256=SHA, record_id=7)
    readded = store.set_startup(app_id=5, table_sha256=SHA, record_id=7, active=True, value=None)
    assert readded.pinned == [7]


def test_profile_rejects_excessive_startup_entries_from_persisted_state(tmp_path: Path):
    path = tmp_path / "profiles.json"
    startup = [{"record_id": i, "active": True, "value": None} for i in range(1025)]
    path.write_text(json.dumps({
        "schema": 3,
        "profiles": [{
            "app_id": 1, "name": "Game", "is_shortcut": False,
            "table_sha256": SHA, "target_process": None, "execution_consent_sha256": None,
            "startup": startup, "pinned": [], "previous_table_sha256": None,
            "table_history": {}, "launch_ownership": None,
        }],
    }))
    with pytest.raises(ValueError, match="startup"):
        ProfileStore(path).list_profiles()


def test_profile_name_rejects_control_bidi_and_excessive_utf8(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    for bad in ["bad\nname", "bad\x00name", "safe\u202eexe", "😀" * 300]:
        with pytest.raises(ValueError, match="profile name"):
            store.upsert(app_id=123, name=bad, is_shortcut=False, table_sha256=None, target_process=None)


def test_profile_kind_change_resets_sha_bound_preferences_and_consent(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=502, name="Steam Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_startup(app_id=502, table_sha256=SHA, record_id=7, active=True, value="10")
    store.set_pinned(app_id=502, table_sha256=SHA, record_id=7, pinned=True)
    store.set_execution_consent(app_id=502, table_sha256=SHA, consent=True)
    store.upsert(app_id=502, name="Steam Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")

    replaced = store.upsert(
        app_id=502,
        name="Shortcut",
        is_shortcut=True,
        table_sha256=SHA,
        target_process="shortcut.exe",
    )

    assert replaced.is_shortcut is True
    assert replaced.table_sha256 == SHA
    assert replaced.startup == []
    assert replaced.pinned == []
    assert replaced.execution_consent_sha256 is None
    assert replaced.previous_table_sha256 is None
    assert replaced.table_history == {}


def test_profile_mutations_enforce_limits_before_persisting_unreadable_state(tmp_path: Path, monkeypatch):
    import ce_decky.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "_MAX_STARTUP_CONTROLS", 2)
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=1, name="Game", is_shortcut=False, table_sha256=SHA, target_process=None)
    store.set_startup(app_id=1, table_sha256=SHA, record_id=1, active=True, value=None)
    store.set_startup(app_id=1, table_sha256=SHA, record_id=2, active=True, value=None)
    with pytest.raises(ValueError, match="too many startup"):
        store.set_startup(app_id=1, table_sha256=SHA, record_id=3, active=True, value=None)
    assert [item.record_id for item in store.get(1).startup] == [1, 2]

    monkeypatch.setattr(profiles_mod, "_MAX_PROFILES", 1)
    with pytest.raises(ValueError, match="profile limit"):
        store.upsert(app_id=2, name="Other", is_shortcut=False, table_sha256=None, target_process=None)
    assert [profile.app_id for profile in store.list_profiles()] == [1]


def test_profile_parser_rejects_unknown_fields_and_impossible_previous_sha(tmp_path: Path):
    path = tmp_path / "profiles.json"
    base = {
        "app_id": 1, "name": "Game", "is_shortcut": False,
        "table_sha256": SHA, "target_process": None, "execution_consent_sha256": None,
        "startup": [], "pinned": [], "previous_table_sha256": None,
        "table_history": {}, "launch_ownership": None,
    }
    bad = dict(base, unexpected="ignored-before")
    path.write_text(json.dumps({"schema": 3, "profiles": [bad]}))
    with pytest.raises(ValueError, match="unknown fields"):
        ProfileStore(path).list_profiles()

    bad = dict(base, previous_table_sha256=SHA)
    path.write_text(json.dumps({"schema": 3, "profiles": [bad]}))
    with pytest.raises(ValueError, match="previous table SHA"):
        ProfileStore(path).list_profiles()


def test_profile_name_rejects_lone_unicode_surrogate_as_validation_error(tmp_path: Path):
    with pytest.raises(ValueError, match="valid Unicode"):
        ProfileStore(tmp_path / "profiles.json").upsert(
            app_id=123, name="bad\ud800", is_shortcut=False, table_sha256=None, target_process=None
        )


def test_profiles_state_rejects_boolean_schema(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"schema": True, "profiles": []}), encoding="utf-8")
    store = ProfileStore(path)
    with pytest.raises(ValueError, match="unsupported or corrupt schema"):
        store.list_profiles()


def test_profile_target_process_rejects_bidi_or_invisible_controls(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    with pytest.raises(ValueError, match="unambiguous basename"):
        store.upsert(app_id=1, name="Game", is_shortcut=False, table_sha256=None, target_process="game\u202e.exe")
    with pytest.raises(ValueError, match="unambiguous basename"):
        store.upsert(app_id=1, name="Game", is_shortcut=False, table_sha256=None, target_process="ga\u200bme.exe")


def test_saving_a_profile_is_the_only_association_a_selection_needs(tmp_path: Path):
    """Selecting a table associates it, so nothing has to associate it again.

    Activation used to save the profile and then call `associate_table` for the
    same digest, which persists nothing new and only adds a second write that
    can fail or lose its reply after the first is already durable.
    """
    store = ProfileStore(tmp_path / "profiles.json")
    saved = store.upsert(app_id=91, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    assert saved.table_sha256 == SHA
    assert saved.table_library == [SHA]
    assert store.associate_table(app_id=91, table_sha256=SHA).table_library == [SHA]


def test_schema3_profile_derives_table_library_and_schema5_persists_autoload_remembered_state(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "schema": 3,
        "profiles": [{
            "app_id": 91, "name": "Legacy", "is_shortcut": False,
            "table_sha256": SHA, "target_process": "game.exe", "execution_consent_sha256": SHA,
            "startup": [], "pinned": [], "previous_table_sha256": SHA2,
            "table_history": {
                SHA2: {"startup": [], "pinned": [], "execution_consent": False},
            },
            "launch_ownership": None,
        }],
    }), encoding="utf-8")

    store = ProfileStore(path)
    migrated = store.get(91)
    assert migrated is not None
    assert migrated.table_library == [SHA, SHA2]
    assert migrated.autoload_enabled is False
    assert migrated.remembered == []

    associated = store.associate_table(app_id=91, table_sha256="c" * 64)
    assert associated.table_library == [SHA, SHA2, "c" * 64]
    autoload = store.set_autoload(app_id=91, table_sha256=SHA, enabled=True)
    assert autoload.autoload_enabled is True
    remembered = store.set_remembered(
        app_id=91,
        table_sha256=SHA,
        states=[{"record_id": 7, "active": True, "value": "100"}],  # type: ignore[list-item]
    )
    assert [(item.record_id, item.active, item.value) for item in remembered.remembered] == [(7, True, "100")]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 5


def test_schema3_max_history_migrates_without_becoming_unsavable(tmp_path: Path):
    path = tmp_path / "profiles.json"
    history_shas = [f"{index:064x}" for index in range(1, 257)]
    current_sha = "f" * 64
    previous_sha = "e" * 64
    assert current_sha not in history_shas and previous_sha not in history_shas
    path.write_text(json.dumps({
        "schema": 3,
        "profiles": [{
            "app_id": 911, "name": "Full legacy history", "is_shortcut": False,
            "table_sha256": current_sha, "target_process": "game.exe",
            "execution_consent_sha256": current_sha, "startup": [], "pinned": [],
            "previous_table_sha256": previous_sha,
            "table_history": {
                digest: {"startup": [], "pinned": [], "execution_consent": False}
                for digest in history_shas
            },
            "launch_ownership": None,
        }],
    }), encoding="utf-8")

    store = ProfileStore(path)
    migrated = store.get(911)
    assert migrated is not None
    assert len(migrated.table_library) == 258
    assert current_sha in migrated.table_library
    assert previous_sha in migrated.table_library

    saved = store.upsert(
        app_id=911, name="Full legacy history", is_shortcut=False,
        table_sha256=current_sha, target_process="game.exe",
    )
    assert len(saved.table_library) == 258
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 5
    assert len(raw["profiles"][0]["table_library"]) == 258


def test_full_table_library_evicts_only_optional_association(tmp_path: Path):
    path = tmp_path / "profiles.json"
    history_shas = [f"{index:064x}" for index in range(1, 257)]
    current_sha = "f" * 64
    previous_sha = "e" * 64
    optional_shas = [f"{0x1000 + index:064x}" for index in range(254)]
    table_library = [current_sha, previous_sha, *history_shas, *optional_shas]
    assert len(table_library) == 512
    path.write_text(json.dumps({
        "schema": 4,
        "profiles": [{
            "app_id": 912, "name": "Full library", "is_shortcut": False,
            "table_sha256": current_sha, "target_process": "game.exe",
            "execution_consent_sha256": current_sha, "startup": [], "pinned": [],
            "previous_table_sha256": previous_sha,
            "table_history": {
                digest: {"startup": [], "pinned": [], "execution_consent": False, "remembered": []}
                for digest in history_shas
            },
            "table_library": table_library, "autoload_enabled": False, "remembered": [],
            "launch_ownership": None,
        }],
    }), encoding="utf-8")

    store = ProfileStore(path)
    new_sha = "d" * 64
    associated = store.associate_table(app_id=912, table_sha256=new_sha)
    assert len(associated.table_library) == 512
    assert new_sha in associated.table_library
    assert current_sha in associated.table_library
    assert previous_sha in associated.table_library
    assert set(history_shas).issubset(associated.table_library)
    assert optional_shas[0] not in associated.table_library


def test_remembered_state_and_consent_restore_only_for_same_exact_sha(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=92, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_execution_consent(app_id=92, table_sha256=SHA, consent=True)
    store.set_autoload(app_id=92, table_sha256=SHA, enabled=True)
    store.set_remembered(
        app_id=92,
        table_sha256=SHA,
        states=[{"record_id": 4, "active": True, "value": None}],  # type: ignore[list-item]
    )

    other = store.upsert(app_id=92, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")
    assert other.execution_consent_sha256 is None
    assert other.remembered == []
    assert other.autoload_enabled is True

    restored = store.upsert(app_id=92, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    assert restored.execution_consent_sha256 == SHA
    assert [(item.record_id, item.active) for item in restored.remembered] == [(4, True)]
    assert SHA in restored.table_library and SHA2 in restored.table_library


def test_effective_startup_action_budget_rejects_invalid_persisted_or_mutated_state(tmp_path: Path):
    import ce_decky.profiles as profiles_mod

    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=93, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_startup(app_id=93, table_sha256=SHA, record_id=1, active=True, value="1")
    original = profiles_mod.MAX_EFFECTIVE_STARTUP_ACTIONS
    profiles_mod.MAX_EFFECTIVE_STARTUP_ACTIONS = 2
    try:
        with pytest.raises(ValueError, match="effective startup state exceeds 2 action limit"):
            store.set_remembered(
                app_id=93, table_sha256=SHA,
                states=[StartupPreference(2, True, "2")],
            )
    finally:
        profiles_mod.MAX_EFFECTIVE_STARTUP_ACTIONS = original

    current = store.get(93)
    assert current is not None
    assert current.remembered == []


def test_steam_shortcut_identity_change_resets_the_per_game_table_library(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=94, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_execution_consent(app_id=94, table_sha256=SHA, consent=True)
    store.set_autoload(app_id=94, table_sha256=SHA, enabled=True)
    store.upsert(app_id=94, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")
    assert store.upsert(
        app_id=94, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe",
    ).table_library == [SHA, SHA2]

    # A Steam/shortcut identity change discards startup, pins, consent, history and
    # autoload; the table library must be reset with them instead of keeping the
    # discarded profile's exact SHA associated with the new identity.
    reset = store.upsert(app_id=94, name="Game", is_shortcut=True, table_sha256=None, target_process=None)
    assert reset.table_library == []
    assert reset.table_history == {}
    assert reset.previous_table_sha256 is None
    assert reset.autoload_enabled is False
    assert reset.remembered == []


def test_configured_values_survive_a_live_remembered_rewrite(tmp_path: Path):
    """The typed value is this table's configuration, not a copy of live state.

    Cheat Engine reports whatever the address holds - including a zero from a
    failed activation - and that answer is what `set_remembered` persists. The
    configured value has to be a separate layer, or the next reconciliation
    overwrites the choice the user just made.
    """
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    store.upsert(app_id=51, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_configured_values(app_id=51, table_sha256=SHA, values=[ConfiguredValue(7, "9999")])
    store.set_remembered(app_id=51, table_sha256=SHA, states=[StartupPreference(7, True, "0")])

    reloaded = ProfileStore(path).get(51)
    assert reloaded is not None
    assert [(item.record_id, item.value) for item in reloaded.configured_values] == [(7, "9999")]
    assert [(item.record_id, item.value) for item in reloaded.remembered] == [(7, "0")]
    assert reloaded.as_dict()["configured_values"] == [{"record_id": 7, "value": "9999"}]


def test_configured_values_reject_blank_and_unreadable_placeholder(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=52, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    for bad in ["", "   ", "??"]:
        with pytest.raises(ValueError, match="blank or Cheat Engine"):
            store.set_configured_values(app_id=52, table_sha256=SHA, values=[ConfiguredValue(7, bad)])
    with pytest.raises(ValueError, match="duplicate"):
        store.set_configured_values(
            app_id=52, table_sha256=SHA, values=[ConfiguredValue(7, "1"), ConfiguredValue(7, "2")],
        )
    with pytest.raises(ValueError, match="exact selected table SHA"):
        store.set_configured_values(app_id=52, table_sha256=SHA2, values=[ConfiguredValue(7, "1")])


def test_configured_values_restore_only_for_the_same_exact_sha(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles.json")
    store.upsert(app_id=53, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    store.set_configured_values(app_id=53, table_sha256=SHA, values=[ConfiguredValue(4, "250")])

    other = store.upsert(app_id=53, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")
    assert other.configured_values == []

    restored = store.upsert(app_id=53, name="Game", is_shortcut=False, table_sha256=SHA, target_process="game.exe")
    assert [(item.record_id, item.value) for item in restored.configured_values] == [(4, "250")]


def test_schema4_typed_values_migrate_into_configured_values_without_placeholders(tmp_path: Path):
    """A profile written before this fix keeps the values it still has.

    Schema 4 kept the typed value only inside the live remembered state, so the
    migration promotes it - except for the placeholder and blank answers that
    were never a user choice in the first place.
    """
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({
        "schema": 4,
        "profiles": [{
            "app_id": 61,
            "name": "Game",
            "is_shortcut": False,
            "table_sha256": SHA,
            "target_process": "game.exe",
            "execution_consent_sha256": SHA,
            "startup": [],
            "pinned": [],
            "previous_table_sha256": SHA2,
            "table_history": {SHA2: {
                "startup": [],
                "pinned": [],
                "execution_consent": False,
                "remembered": [{"record_id": 9, "active": True, "value": "5"}],
            }},
            "table_library": [SHA, SHA2],
            "autoload_enabled": True,
            "remembered": [
                {"record_id": 1, "active": True, "value": "9999"},
                {"record_id": 2, "active": True, "value": "??"},
                {"record_id": 3, "active": True, "value": None},
            ],
        }],
    }), encoding="utf-8")

    store = ProfileStore(path)
    migrated = store.get(61)
    assert migrated is not None
    assert [(item.record_id, item.value) for item in migrated.configured_values] == [(1, "9999")]

    saved = store.set_autoload(app_id=61, table_sha256=SHA, enabled=True)
    assert [(item.record_id, item.value) for item in saved.configured_values] == [(1, "9999")]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == 5
    assert raw["profiles"][0]["configured_values"] == [{"record_id": 1, "value": "9999"}]
    assert raw["profiles"][0]["table_history"][SHA2]["configured_values"] == [{"record_id": 9, "value": "5"}]

    # The migrated history is still restorable as configuration for its own SHA.
    switched = store.upsert(app_id=61, name="Game", is_shortcut=False, table_sha256=SHA2, target_process="game.exe")
    assert [(item.record_id, item.value) for item in switched.configured_values] == [(9, "5")]


@pytest.mark.parametrize("boundary", ["rename_sync", "replacement"])
def test_profile_repair_failure_after_quarantine_is_not_a_refusal(tmp_path, monkeypatch, boundary):
    from ce_decky import atomic
    path = tmp_path / "profiles.json"
    path.write_text("broken")
    store = ProfileStore(path)
    def refuse(*args):
        raise OSError("disk failure")
    if boundary == "rename_sync":
        monkeypatch.setattr(atomic, "fsync_directory", refuse)
    else:
        monkeypatch.setattr(store, "_save", refuse)
    with pytest.raises(atomic.DurabilityUnknownError):
        store.quarantine_corrupt_state()
    assert not path.exists()
    assert next(tmp_path.glob("profiles.json.invalid-*")).read_text() == "broken"
