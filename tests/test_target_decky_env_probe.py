from __future__ import annotations

import json
from pathlib import Path

from scripts import target_decky_env_probe


def _process(proc: Path, pid: int, env: dict[str, str]) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    payload = b"\0".join(f"{key}={value}".encode("utf-8") for key, value in env.items()) + b"\0"
    (directory / "environ").write_bytes(payload)


def _plugin_dir(root: Path, name: str = "CE Decky") -> Path:
    plugin = root / "arbitrary-install-directory"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    return plugin


def test_resolves_one_matching_process_and_emits_only_allowlist(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    plugin = _plugin_dir(tmp_path / "plugin-root")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    _process(
        proc,
        101,
        {
            "DECKY_PLUGIN_NAME": "CE Decky",
            "DECKY_USER_HOME": str(home),
            "DECKY_PLUGIN_DIR": str(plugin),
            "DECKY_PLUGIN_RUNTIME_DIR": str(runtime),
            "SECRET_TOKEN": "must-not-leak",
        },
    )

    report = target_decky_env_probe.probe(proc, "CE Decky")
    assert report["schema"] == 2
    assert report["ok"] is True
    selected = report["selected"]
    assert selected["pid"] == 101
    assert selected["identity_source"] == "DECKY_PLUGIN_NAME"
    assert selected["environment"]["DECKY_PLUGIN_RUNTIME_DIR"] == str(runtime)
    assert "SECRET_TOKEN" not in selected["environment"]
    assert selected["paths"]["DECKY_PLUGIN_RUNTIME_DIR"]["is_dir"] is True


def test_nonmatching_plugin_name_is_not_selected(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    _process(
        proc,
        10,
        {
            "DECKY_PLUGIN_NAME": "Other Plugin",
            "DECKY_USER_HOME": "/home/deck",
            "DECKY_PLUGIN_DIR": "/tmp/other",
        },
    )
    report = target_decky_env_probe.probe(proc, "CE Decky")
    assert report["state"] == "missing"
    assert report["selected"] is None


def test_missing_plugin_name_uses_plugin_json_not_directory_basename(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    plugin = _plugin_dir(tmp_path / "root", "CE Decky")
    _process(
        proc,
        12,
        {
            "DECKY_USER_HOME": "/home/deck",
            "DECKY_PLUGIN_DIR": str(plugin),
        },
    )
    report = target_decky_env_probe.probe(proc, "CE Decky")
    assert report["ok"] is True
    assert report["selected"]["identity_source"] == "plugin.json"

    proc2 = tmp_path / "proc2"
    proc2.mkdir()
    wrong = _plugin_dir(tmp_path / "wrong-root", "Other Plugin")
    _process(
        proc2,
        13,
        {
            "DECKY_USER_HOME": "/home/deck",
            "DECKY_PLUGIN_DIR": str(wrong),
        },
    )
    assert target_decky_env_probe.probe(proc2, "CE Decky")["state"] == "missing"


def test_missing_plugin_name_without_readable_metadata_is_not_guessed(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    plugin = tmp_path / "CE Decky"
    plugin.mkdir()
    _process(
        proc,
        14,
        {
            "DECKY_USER_HOME": "/home/deck",
            "DECKY_PLUGIN_DIR": str(plugin),
        },
    )
    assert target_decky_env_probe.probe(proc, "CE Decky")["state"] == "missing"


def test_multiple_matching_processes_are_ambiguous(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid in (10, 11):
        _process(
            proc,
            pid,
            {
                "DECKY_PLUGIN_NAME": "CE Decky",
                "DECKY_USER_HOME": "/home/deck",
                "DECKY_PLUGIN_DIR": f"/tmp/plugin-{pid}",
            },
        )
    report = target_decky_env_probe.probe(proc, "CE Decky")
    assert report["state"] == "ambiguous"
    assert len(report["candidates"]) == 2


def test_explicit_pid_selects_only_that_process(tmp_path: Path):
    proc = tmp_path / "proc"
    proc.mkdir()
    for pid in (20, 21):
        _process(
            proc,
            pid,
            {
                "DECKY_PLUGIN_NAME": "CE Decky",
                "DECKY_USER_HOME": "/home/deck",
                "DECKY_PLUGIN_DIR": f"/tmp/plugin-{pid}",
            },
        )
    report = target_decky_env_probe.probe(proc, "CE Decky", 21)
    assert report["ok"] is True
    assert report["selected"]["pid"] == 21
