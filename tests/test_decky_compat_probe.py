import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

from scripts import decky_compat_probe as compat


def test_changed_runtime_bytes_are_refused_before_execution(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"not an executable")
    plugin = tmp_path / "plugin.zip"
    plugin.write_bytes(b"not a zip")
    result = compat.probe(plugin, runtime, expected_sha="0" * 64, version="v3.3.0")
    assert result["status"] == "INCOMPLETE" and "digest mismatch" in result["error"]


def test_unknown_binary_cannot_start_a_second_loader_on_a_device(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"unknown")
    plugin = tmp_path / "plugin.zip"
    plugin.write_bytes(b"zip")
    result = compat.probe(plugin, runtime, expected_sha=compat.digest(runtime), version="v9.0.0")
    assert result["status"] == "INCOMPLETE" and "isolation" in result["error"]


def test_non_linux_host_is_refused_before_any_child_process(monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"fixture")
    plugin = tmp_path / "plugin.zip"
    plugin.write_bytes(b"fixture")
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps({"sha256": compat.digest(runtime)}))
    monkeypatch.setattr(compat, "PIN", pin)
    monkeypatch.setattr(compat.sys, "platform", "win32")
    monkeypatch.setattr(compat.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("unsupported host started a process"))
    result = compat.probe(plugin, runtime, expected_sha=compat.digest(runtime), version="fixture")
    assert result["status"] == "INCOMPLETE" and "requires Linux" in result["error"]


def _inputs():
    directory = os.environ.get("CE_DECKY_COMPAT_PLUGIN_DIR")
    runtime = os.environ.get("CE_DECKY_COMPAT_RUNTIME")
    if not directory or not runtime:
        if os.environ.get("CE_DECKY_REQUIRE_FROZEN") == "1":
            pytest.fail("required frozen Decky inputs are missing")
        pytest.skip("exact artifact/runtime inputs are supplied by the dedicated CI job or target check")
    archives = list(Path(directory).glob("CE-Decky-v*.zip"))
    assert len(archives) == 1, "name one exact package directory without stale artifacts"
    return archives[0], Path(runtime), json.loads(compat.PIN.read_text())


def test_actual_packaged_runtime_and_broken_negative_control(tmp_path):
    archive, runtime, pin = _inputs()
    passed = compat.probe(archive, runtime, expected_sha=pin["sha256"], version=pin["version"])
    assert passed["status"] == "PASS", passed
    assert passed["frozen"] and passed["python"].startswith("3.14.")
    assert passed["pathlib_types_fallback"] is True
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(broken, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "CE-Decky/main.py":
                assert b"ensure_pathlib_types()" in data
                data = data.replace(b"ensure_pathlib_types()", b"pass # deliberately disabled for negative control")
            target.writestr(info, data)
    failed = compat.probe(broken, runtime, expected_sha=pin["sha256"], version=pin["version"])
    assert failed["status"] == "FAIL", failed
    assert failed["plugin_sha256"] != passed["plugin_sha256"]


def test_packaged_artifact_also_works_in_an_ordinary_complete_interpreter():
    archive, _, _ = _inputs()
    result = compat.probe(archive, Path(sys.executable), expected_sha=compat.digest(Path(sys.executable)),
                          version="ordinary-python-control", python_control=True)
    assert result["status"] == "PASS", result
    assert result["frozen"] is False


def test_previous_supported_frozen_runtime_is_preserved():
    archive, _, pin = _inputs()
    runtime = os.environ.get("CE_DECKY_COMPAT_BASELINE_RUNTIME")
    if not runtime:
        if os.environ.get("CE_DECKY_REQUIRE_FROZEN") == "1" and os.environ.get("GITHUB_ACTIONS") == "true":
            pytest.fail("required previous-runtime baseline input is missing")
        pytest.skip("previous runtime is exercised in disposable CI, not on a live Decky device")
    baseline = pin["previous_supported"]
    result = compat.probe(archive, Path(runtime), expected_sha=baseline["sha256"], version=baseline["version"])
    assert result["status"] == "PASS", result
    assert result["frozen"] and result["python"].startswith("3.11.")
