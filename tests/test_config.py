from pathlib import Path
import json

import pytest

from ce_decky.config import Config, ConfigStore


def test_config_roundtrip_and_strict_types(tmp_path: Path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.save(Config(imported_ce_executable="/home/u/CE/ce.exe", imported_ce_sha256="A" * 64, imported_ce_root="/home/u/CE"))
    loaded = store.load()
    assert loaded.imported_ce_sha256 == "a" * 64
    assert loaded.acknowledged_security_notice is False


def test_config_rejects_bool_schema_string_bool_unknown_and_bad_paths(tmp_path: Path):
    path = tmp_path / "config.json"
    cases = [
        {"schema": True},
        {"schema": 1, "acknowledged_security_notice": "false"},
        {"schema": 1, "unknown": 1},
        {"schema": 1, "imported_ce_executable": ["x"]},
        {"schema": 1, "imported_ce_root": "/home/u\nother"},
        {"schema": 1, "imported_ce_sha256": "z" * 64},
        {"schema": 1, "imported_ce_version": "7.7\u202eexe"},
    ]
    for raw in cases:
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError):
            ConfigStore(path).load()


def test_config_save_revalidates_constructed_object(tmp_path: Path):
    store = ConfigStore(tmp_path / "config.json")
    with pytest.raises(ValueError, match="schema"):
        store.save(Config(schema=True))  # type: ignore[arg-type]


def test_config_rejects_partial_imported_ce_identity(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text('{"schema":1,"imported_ce_executable":"/tmp/ce.exe"}')
    with pytest.raises(ValueError, match="identity must contain executable, root, and SHA-256 together"):
        ConfigStore(path).load()
    path.write_text('{"schema":1,"imported_ce_version":"7.7"}')
    with pytest.raises(ValueError, match="identity must contain executable, root, and SHA-256 together"):
        ConfigStore(path).load()


def test_config_has_bounded_persistent_file_size(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_bytes(b'{"padding":"' + b'x' * (300 * 1024) + b'"}')
    with pytest.raises(ValueError, match="size"):
        ConfigStore(path).load()


def test_config_preferences_default_on_and_reject_non_boolean(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text('{"schema":1}')
    loaded = ConfigStore(path).load()
    assert loaded.update_auto_check is True
    assert loaded.mascot_visible is True
    for raw in ({"schema": 1, "update_auto_check": "yes"}, {"schema": 1, "mascot_visible": 0}):
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="must be boolean"):
            ConfigStore(path).load()


def test_config_migration_writes_this_versions_shape_and_keeps_every_value(tmp_path: Path):
    path = tmp_path / "config.json"
    # Exactly what an 0.9.27 device holds: a registered Cheat Engine, an
    # acknowledged notice, and no preference keys at all.
    legacy = {
        "schema": 1,
        "imported_ce_executable": "/home/u/CE/ce.exe",
        "imported_ce_sha256": "b" * 64,
        "imported_ce_root": "/home/u/CE",
        "imported_ce_version": "7.7",
        "acknowledged_security_notice": True,
    }
    path.write_text(json.dumps(legacy))
    store = ConfigStore(path)
    assert store.migrate() is True
    written = json.loads(path.read_text())
    assert written == {**legacy, "update_auto_check": True, "mascot_visible": True}
    # What this version would have written from nothing but the same values.
    reference = tmp_path / "reference.json"
    ConfigStore(reference).save(store.load())
    assert json.loads(reference.read_text()) == written
    # Settled: a second load has nothing to rewrite.
    assert store.migrate() is False


def test_config_migration_creates_the_first_file_and_keeps_switched_off_choices(tmp_path: Path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    assert store.migrate() is True
    assert json.loads(path.read_text())["mascot_visible"] is True
    path.write_text('{"schema":1,"update_auto_check":false,"mascot_visible":false}')
    assert store.migrate() is True
    written = json.loads(path.read_text())
    assert (written["update_auto_check"], written["mascot_visible"]) == (False, False)
    assert store.load().update_auto_check is False


def test_config_migration_leaves_an_unreadable_file_alone(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text('{"schema":1,"unknown":1}')
    with pytest.raises(ValueError):
        ConfigStore(path).migrate()
    assert json.loads(path.read_text()) == {"schema": 1, "unknown": 1}
