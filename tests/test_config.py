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
