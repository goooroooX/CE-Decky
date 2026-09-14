from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import asyncio
import json
import logging
import os
import subprocess
import struct
import sys
import threading

import pytest

from ce_decky import managed_ce
from ce_decky.managed_ce import (
    MANIFEST_PATH,
    ManagedCEManager,
    ManagedCERelease,
    _safe_status_text,
    _verify_proton_tool,
    discover_proton_tools,
    load_release_manifest,
)
from ce_decky.network import NetworkError, ResponseTooLarge
from ce_decky.paths import PluginPaths


def _fake_pe(path: Path, size: int = 300_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    path.write_bytes(data)


def _proton(paths: PluginPaths, name: str = "Proton 10") -> Path:
    root = paths.user_home / ".local/share/Steam/steamapps/common" / name
    root.mkdir(parents=True)
    script = root / "proton"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    os.chmod(script, 0o755)
    return root


class _FakeNetwork:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls: list[tuple[str, frozenset[str], int]] = []

    async def download(self, url: str, destination: Path, *, allowed_hosts: frozenset[str], max_bytes: int, **_kwargs):
        self.calls.append((url, allowed_hosts, max_bytes))
        destination.write_bytes(self.payload)


class _FailingNetwork:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def download(self, *_args, **_kwargs):
        raise self.error


def test_reviewed_manifest_pins_current_clean_windows_artifact():
    release = load_release_manifest()
    assert release.artifact_url == "https://d1dj9aohuk02ls.cloudfront.net/f/CheatEngine/2129/CheatEngine77.exe"
    assert release.allowed_hosts == ("d1dj9aohuk02ls.cloudfront.net",)
    assert release.sha256 == "cf0f4b6002555677984233c95856683959be83a5def3dc12175a8296eb22676b"
    assert release.size == 34_690_856
    assert release.silent_arguments == ()


def test_managed_validation_preserves_the_metadata_selected_executable(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    release = ManagedCERelease(
        "test", "CheatEngine-test.exe", "https://download.example/CheatEngine-test.exe",
        ("download.example",), "f" * 64, 1, 2, (), "2026-08-22", "test signer",
    )
    root = paths.ce_root / "installations" / release.sha256
    root.mkdir(parents=True)
    launcher = root / "Cheat Engine.exe"
    _fake_pe(launcher)
    _fake_pe(root / "cheatengine-x86_64.exe")
    file_count, total_bytes = managed_ce._validate_install_tree(root, allow_managed_manifest=False)
    (root / ".ce-decky-managed-install.json").write_text(json.dumps({
        "schema": 2,
        "artifact_sha256": release.sha256,
        "visible_version": release.visible_version,
        "executable": launcher.name,
        "executable_sha256": sha256(launcher.read_bytes()).hexdigest(),
        "materialization": "native-ce77-extraction",
        "file_count": file_count + 1,
        "total_bytes": total_bytes,
    }), encoding="utf-8")

    imported = managed_ce.validate_managed_installation(root, paths.ce_root, release)

    assert imported.executable == str(launcher.resolve())


def test_managed_tree_validation_bounds_empty_directories(tmp_path: Path, monkeypatch):
    root = tmp_path / "tree"
    root.mkdir()
    for index in range(3):
        (root / str(index)).mkdir()
    monkeypatch.setattr(managed_ce, "MAX_INSTALL_TREE_ENTRIES", 2)

    with pytest.raises(ValueError, match="tree entry bound"):
        managed_ce._validate_install_tree(root)


def test_release_manifest_rejects_credentials_and_unallowlisted_url_host(tmp_path: Path):
    for index, url in enumerate((
        "https://user@example.invalid/CheatEngine77.exe",
        "https://example.invalid/CheatEngine77.exe",
    )):
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        raw["artifact_url"] = url
        path = tmp_path / f"managed-{index}.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        try:
            load_release_manifest(path)
        except ValueError as exc:
            assert "URL" in str(exc) or "host policy" in str(exc)
        else:
            raise AssertionError("unsafe managed installer URL was accepted")


def test_installer_output_is_safe_for_qam_and_logs():
    assert _safe_status_text("before\n\x1b[31m\u202eafter", 100) == "before  [31m after"


def test_proton_discovery_returns_exact_script_identity(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    root = _proton(paths)
    first = discover_proton_tools(paths.user_home)
    assert [(item.name, item.path) for item in first] == [("Proton 10", str(root.resolve()))]
    assert first[0].proton_sha256 == sha256((root / "proton").read_bytes()).hexdigest()

    (root / "proton").write_text("#!/usr/bin/env python3\n# changed\n", encoding="utf-8")
    second = discover_proton_tools(paths.user_home)
    assert second[0].tool_id != first[0].tool_id
    try:
        _verify_proton_tool(first[0])
    except ValueError as exc:
        assert "identity changed" in str(exc)
    else:
        raise AssertionError("changed Proton script retained its old trusted identity")


def test_managed_install_downloads_verifies_promotes_and_recovers_completed_state(tmp_path: Path, monkeypatch, caplog):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    installer = b"MZ" + b"reviewed-installer" * 64
    release = ManagedCERelease(
        "test", "CheatEngine-test.exe", "https://download.example/CheatEngine-test.exe",
        ("download.example",), sha256(installer).hexdigest(), len(installer), len(installer) + 1,
        ("/VERYSILENT", "/ZBDIST", "/SUPPRESSMSGBOXES", "/NORESTART"),
        "2026-08-22", "test signer",
    )
    network = _FakeNetwork(installer)
    caplog.set_level(logging.INFO, logger="test")
    manager = ManagedCEManager(paths.user_home, paths.ce_root, paths.temp_root, network, logging.getLogger("test"))  # type: ignore[arg-type]
    manager.release = release
    corrupt_cache = paths.ce_root / "installers" / release.sha256 / release.artifact_filename
    corrupt_cache.parent.mkdir(parents=True)
    corrupt_cache.write_bytes(b"MZcorrupt")
    extraction_runs = 0

    def fake_extractor(_installer, destination, *, expected_size, expected_sha256):
        nonlocal extraction_runs
        assert expected_size == len(installer)
        assert expected_sha256 == release.sha256
        extraction_runs += 1
        _fake_pe(destination / "Cheat Engine.exe")
        (destination / "autorun").mkdir()
        (destination / "autorun" / "base.lua").write_text("return true\n", encoding="utf-8")

    monkeypatch.setattr(managed_ce, "extract_reviewed_installer", fake_extractor)

    async def exercise():
        started = await manager.start()
        await manager.wait(started["operation_id"])
        assert manager._task is not None and manager._task.exception() is None
        status = manager.status(started["operation_id"])
        assert status["state"] == "completed"
        imported = manager.completed_install(started["operation_id"])
        assert Path(imported.root) == (paths.ce_root / "installations" / release.sha256).resolve()
        assert Path(imported.executable).is_file()
        assert network.calls == [(release.artifact_url, frozenset(release.allowed_hosts), release.max_size)]
        assert manager.capability()["operation"]["state"] == "completed"
        manager.consume_completed(started["operation_id"])
        assert manager.capability()["operation"] is None

        reused = await manager.start()
        assert reused["state"] == "completed"
        assert extraction_runs == 1

        forced = await manager.start(force=True)
        assert forced["state"] == "downloading"
        assert forced["message"] == "Downloading a fresh reviewed Cheat Engine artifact for reinstall"
        await manager.wait(forced["operation_id"])
        assert manager._task is not None and manager._task.exception() is None
        assert manager.status(forced["operation_id"])["state"] == "completed"
        assert extraction_runs == 2
        assert network.calls == [
            (release.artifact_url, frozenset(release.allowed_hosts), release.max_size),
            (release.artifact_url, frozenset(release.allowed_hosts), release.max_size),
        ]
        messages = [record.getMessage() for record in caplog.records]
        assert any("event=managed_ce.started" in message and "force=true" in message for message in messages)
        assert sum("event=managed_ce.artifact_verified" in message for message in messages) == 2

        (Path(imported.root) / "autorun" / "base.lua").unlink()
        repaired = await manager.start()
        await manager.wait(repaired["operation_id"])
        assert manager._task is not None and manager._task.exception() is None
        assert manager.status(repaired["operation_id"])["state"] == "completed"
        assert extraction_runs == 3
        assert (Path(imported.root) / "autorun" / "base.lua").is_file()
        assert not list((paths.ce_root / "installations").glob(".*.invalid-*"))
        assert not list((paths.temp_root / "managed-ce").iterdir())
        await manager.close()

    asyncio.run(exercise())


def test_repeatedly_cancelled_close_drains_the_managed_extraction_worker(tmp_path: Path, monkeypatch):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    installer = b"MZ" + b"reviewed-installer" * 64
    release = ManagedCERelease(
        "test", "CheatEngine-test.exe", "https://download.example/CheatEngine-test.exe",
        ("download.example",), sha256(installer).hexdigest(), len(installer), len(installer) + 1,
        ("/VERYSILENT",), "2026-08-22", "test signer",
    )
    manager = ManagedCEManager(
        paths.user_home, paths.ce_root, paths.temp_root, _FakeNetwork(installer), logging.getLogger("test"),
    )  # type: ignore[arg-type]
    manager.release = release
    entered = threading.Event()
    resume = threading.Event()
    finished = threading.Event()

    def blocking_extractor(_installer, destination, *, expected_size, expected_sha256):
        entered.set()
        assert resume.wait(5)
        _fake_pe(destination / "Cheat Engine.exe")
        (destination / "autorun").mkdir()
        (destination / "autorun" / "base.lua").write_text("return true\n", encoding="utf-8")
        finished.set()

    monkeypatch.setattr(managed_ce, "extract_reviewed_installer", blocking_extractor)

    async def exercise():
        await manager.start()
        assert await asyncio.to_thread(entered.wait, 5)
        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0)
        # Extraction runs in a thread Python cannot stop. However often unload is
        # cancelled, close must not return while that writer is still in the
        # owned per-operation tree the cleanup below removes.
        for _ in range(3):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
            assert not finished.is_set()
        resume.set()

        with pytest.raises(asyncio.CancelledError):
            await closing
        assert finished.is_set()
        assert not list((paths.temp_root / "managed-ce").iterdir())

    asyncio.run(exercise())


def test_force_reinstall_restores_previous_valid_install_before_failed_tree_cleanup(tmp_path: Path, monkeypatch):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    _proton(paths)
    installer = b"MZ" + b"rollback-installer" * 64
    release = ManagedCERelease(
        "test", "CheatEngine-test.exe", "https://download.example/CheatEngine-test.exe",
        ("download.example",), sha256(installer).hexdigest(), len(installer), len(installer) + 1,
        ("/VERYSILENT", "/ZBDIST", "/SUPPRESSMSGBOXES", "/NORESTART"),
        "2026-08-23", "test signer",
    )
    tool = discover_proton_tools(paths.user_home)[0]
    first = paths.temp_root / "first-install"
    _fake_pe(first / "Cheat Engine.exe")
    (first / "old-marker.txt").write_text("known-good", encoding="utf-8")
    managed_ce._promote_installation(first, paths.ce_root, release, tool)

    replacement = paths.temp_root / "replacement-install"
    _fake_pe(replacement / "Cheat Engine.exe")
    (replacement / "new-marker.txt").write_text("reject-me", encoding="utf-8")
    destination = paths.ce_root / "installations" / release.sha256
    real_validate = managed_ce.validate_managed_installation
    real_discard = managed_ce._discard_owned_path

    def reject_promoted_tree(root, ce_root, selected_release=None):
        if Path(root) == destination and (destination / "new-marker.txt").exists():
            raise ValueError("post-promotion validation failed")
        return real_validate(root, ce_root, selected_release)

    def fail_rejected_cleanup(path):
        if ".failed-" in Path(path).name:
            raise OSError("simulated cleanup failure")
        return real_discard(path)

    monkeypatch.setattr(managed_ce, "validate_managed_installation", reject_promoted_tree)
    monkeypatch.setattr(managed_ce, "_discard_owned_path", fail_rejected_cleanup)

    try:
        managed_ce._promote_installation(replacement, paths.ce_root, release, tool, replace_valid=True)
    except ValueError as exc:
        assert "post-promotion validation failed" in str(exc)
    else:
        raise AssertionError("failed replacement was accepted")

    assert (destination / "old-marker.txt").read_text(encoding="utf-8") == "known-good"
    assert not (destination / "new-marker.txt").exists()
    restored = real_validate(destination, paths.ce_root, release)
    assert Path(restored.executable).is_file()



def test_force_reinstall_keeps_valid_promoted_install_when_previous_tree_cleanup_fails(tmp_path: Path, monkeypatch):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    _proton(paths)
    installer = b"MZ" + b"commit-installer" * 64
    release = ManagedCERelease(
        "test", "CheatEngine-test.exe", "https://download.example/CheatEngine-test.exe",
        ("download.example",), sha256(installer).hexdigest(), len(installer), len(installer) + 1,
        ("/VERYSILENT", "/ZBDIST", "/SUPPRESSMSGBOXES", "/NORESTART"),
        "2026-08-23", "test signer",
    )
    tool = discover_proton_tools(paths.user_home)[0]
    first = paths.temp_root / "cleanup-first"
    _fake_pe(first / "Cheat Engine.exe")
    (first / "old-marker.txt").write_text("old", encoding="utf-8")
    managed_ce._promote_installation(first, paths.ce_root, release, tool)

    replacement = paths.temp_root / "cleanup-replacement"
    _fake_pe(replacement / "Cheat Engine.exe")
    (replacement / "new-marker.txt").write_text("new", encoding="utf-8")
    real_discard = managed_ce._discard_owned_path

    def fail_previous_cleanup(path):
        if ".previous-" in Path(path).name:
            raise OSError("simulated previous-tree cleanup failure")
        return real_discard(path)

    monkeypatch.setattr(managed_ce, "_discard_owned_path", fail_previous_cleanup)
    promoted = managed_ce._promote_installation(
        replacement, paths.ce_root, release, tool, replace_valid=True,
    )

    destination = paths.ce_root / "installations" / release.sha256
    assert Path(promoted.root) == destination.resolve()
    assert (destination / "new-marker.txt").read_text(encoding="utf-8") == "new"
    assert not (destination / "old-marker.txt").exists()
    assert list((paths.ce_root / "installations").glob(f".{release.sha256}.previous-*"))


def test_managed_install_fails_closed_before_extraction_on_hash_mismatch(tmp_path: Path, monkeypatch):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    expected = b"MZexpected"
    release = ManagedCERelease(
        "test", "CheatEngine-test.exe", "https://download.example/CheatEngine-test.exe",
        ("download.example",), sha256(expected).hexdigest(), len(expected), 1024,
        ("/VERYSILENT", "/ZBDIST", "/SUPPRESSMSGBOXES", "/NORESTART"),
        "2026-08-22", "test signer",
    )
    manager = ManagedCEManager(paths.user_home, paths.ce_root, paths.temp_root, _FakeNetwork(b"MZwrong"), logging.getLogger("test"))  # type: ignore[arg-type]
    manager.release = release
    called = False

    def forbidden_extractor(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(managed_ce, "extract_reviewed_installer", forbidden_extractor)

    async def exercise():
        started = await manager.start()
        await manager.wait(started["operation_id"])
        status = manager.status(started["operation_id"])
        assert status["state"] == "failed"
        assert status["error"] == "Downloaded Cheat Engine artifact did not match the reviewed size and SHA-256 and was discarded. The existing managed installation was kept unchanged."
        await manager.close()

    asyncio.run(exercise())
    assert called is False


@pytest.mark.parametrize(("error", "expected"), [
    (NetworkError("provider download returned HTTP 404"), "Reviewed Cheat Engine download is unavailable (HTTP 404). Try again later. The existing managed installation was kept unchanged."),
    (NetworkError("provider hostname resolution failed"), "Could not resolve the reviewed Cheat Engine download host. Check the network connection and try again. The existing managed installation was kept unchanged."),
    (ResponseTooLarge("provider response exceeds the byte limit"), "Reviewed Cheat Engine download exceeded its safe size limit and was discarded. The existing managed installation was kept unchanged."),
])
def test_managed_install_reports_actionable_network_failure(tmp_path: Path, error: Exception, expected: str):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    payload = b"MZreviewed"
    release = ManagedCERelease(
        "test", "CheatEngine-test.exe", "https://download.example/CheatEngine-test.exe",
        ("download.example",), sha256(payload).hexdigest(), len(payload), 1024,
        (), "2026-08-23", "test signer",
    )
    manager = ManagedCEManager(paths.user_home, paths.ce_root, paths.temp_root, _FailingNetwork(error), logging.getLogger("test"))  # type: ignore[arg-type]
    manager.release = release

    async def exercise():
        started = await manager.start(force=True)
        await manager.wait(started["operation_id"])
        status = manager.status(started["operation_id"])
        assert status["state"] == "failed"
        assert status["error"] == expected
        await manager.close()

    asyncio.run(exercise())


def test_backend_survives_an_unusable_release_manifest_and_reports_it_on_demand(tmp_path: Path, monkeypatch):
    """A corrupt plugin-shipped manifest must not take the whole backend down.

    The manual Cheat Engine import fallback is the documented controller-accessible
    recovery path, so construction has to stay non-fatal and only the managed
    installer may fail closed.
    """
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    _proton(paths)

    def broken_manifest(*_args, **_kwargs):
        raise ValueError("managed CE release manifest is malformed")

    monkeypatch.setattr(managed_ce, "load_release_manifest", broken_manifest)
    manager = ManagedCEManager(paths.user_home, paths.ce_root, paths.temp_root, _FakeNetwork(b"MZ"), logging.getLogger("test"))  # type: ignore[arg-type]
    assert manager.release is None
    assert "malformed" in (manager.release_error or "")

    capability = manager.capability()
    assert capability["managed_install_available"] is False
    assert capability["release_manifest_loaded"] is False
    assert "malformed" in str(capability["reason"])

    async def exercise():
        try:
            await manager.start()
        except ValueError as exc:
            assert "malformed" in str(exc)
        else:
            raise AssertionError("managed install started without a reviewed release manifest")
        await manager.close()

    asyncio.run(exercise())


def test_capability_does_not_depend_on_proton_discovery(tmp_path: Path, monkeypatch):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()

    def unreadable(_home):
        raise OSError("steam library is unreadable")

    monkeypatch.setattr(managed_ce, "discover_proton_tools", unreadable)
    capability = managed_ce.managed_ce_capability(paths.user_home)
    assert capability["managed_install_available"] is True
    assert capability["release_manifest_loaded"] is True
    assert capability["native_extraction_enabled"] is True
    assert "extract" in str(capability["reason"])


def test_installer_log_drain_is_bounded_and_stops_at_stream_end():
    class _Stream:
        def __init__(self, chunks: list[bytes]) -> None:
            self.chunks = chunks

        async def read(self, _size: int) -> bytes:
            return self.chunks.pop(0) if self.chunks else b""

    class _Process:
        def __init__(self, chunks: list[bytes]) -> None:
            self.stdout = _Stream(chunks)

    limit = managed_ce.MAX_INSTALL_LOG_BYTES
    process = _Process([b"a" * limit, b"tail-of-installer-log"])
    retained = bytearray()
    asyncio.run(managed_ce._drain_process_output(process, retained))  # type: ignore[arg-type]
    assert len(retained) == limit
    assert bytes(retained).endswith(b"tail-of-installer-log")


def test_process_group_scan_treats_a_member_that_disappears_mid_read_as_gone(monkeypatch):
    marker = "a" * 32

    class _Entry:
        name = "123"
        path = "/virtual-proc/123"

    stat_reads = 0

    def fake_read(path: Path, **_kwargs):
        nonlocal stat_reads
        if path.name == "stat":
            stat_reads += 1
            return b"123 (installer) S 1 123 123 0 0\n" if stat_reads == 1 else None
        if path.name == "environ":
            return None
        raise AssertionError(path)

    monkeypatch.setattr(managed_ce.os, "scandir", lambda _root: [_Entry()])
    monkeypatch.setattr(managed_ce, "read_proc_bytes", fake_read)
    assert managed_ce._managed_process_group_state(123, marker, proc_root=Path("/virtual-proc")) == "gone"


def test_process_group_scan_reads_zero_size_procfs_records():
    if os.name != "posix" or not Path("/proc/self/stat").is_file():
        return
    marker = "b" * 32
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "CE_DECKY_MANAGED_INSTALLER": marker},
        start_new_session=True,
    )
    try:
        assert managed_ce._managed_process_group_state(child.pid, marker) == "matched"
    finally:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def test_owned_prefix_retirement_uses_only_the_exact_selected_proton_wineserver(tmp_path: Path, monkeypatch):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    root = _proton(paths, "Proton - Experimental")
    wineserver = root / "files/bin/wineserver"
    wineserver.parent.mkdir(parents=True)
    wineserver.write_bytes(b"#!/bin/sh\n")
    os.chmod(wineserver, 0o755)
    tool = discover_proton_tools(paths.user_home)[0]
    prefix = paths.ce_root / "installer-prefixes" / "release" / tool.tool_id
    (prefix / "pfx").mkdir(parents=True)
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class _Process:
        returncode = 0

        async def wait(self):
            return 0

        def kill(self):
            raise AssertionError("bounded wineserver command should not need SIGKILL")

    async def fake_exec(*argv, **kwargs):
        calls.append((tuple(str(value) for value in argv), kwargs))
        return _Process()

    monkeypatch.setattr(managed_ce.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(managed_ce._retire_owned_proton_prefix(tool, prefix, paths.ce_root))

    assert [argv for argv, _kwargs in calls] == [
        (str(wineserver.resolve()), "-k"),
        (str(wineserver.resolve()), "-w"),
    ]
    for _argv, kwargs in calls:
        assert kwargs["env"]["WINEPREFIX"] == str((prefix / "pfx").resolve())
        assert kwargs["start_new_session"] is True


def test_owned_prefix_retirement_accepts_wineserver_k_exit_after_server_stops(tmp_path: Path, monkeypatch):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    root = _proton(paths, "Proton - Experimental")
    wineserver = root / "files/bin/wineserver"
    wineserver.parent.mkdir(parents=True)
    wineserver.write_bytes(b"#!/bin/sh\n")
    os.chmod(wineserver, 0o755)
    tool = discover_proton_tools(paths.user_home)[0]
    prefix = paths.ce_root / "installer-prefixes" / "release" / tool.tool_id
    (prefix / "pfx").mkdir(parents=True)
    calls: list[tuple[str, ...]] = []

    class _Process:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

        async def wait(self):
            return self.returncode

        def kill(self):
            raise AssertionError("bounded wineserver command should not need SIGKILL")

    async def fake_exec(*argv, **_kwargs):
        command = tuple(str(value) for value in argv)
        calls.append(command)
        return _Process(1 if command[-1] == "-k" else 0)

    monkeypatch.setattr(managed_ce.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(managed_ce._retire_owned_proton_prefix(tool, prefix, paths.ce_root))

    assert [call[-1] for call in calls] == ["-k", "-w"]


def test_capability_and_install_never_mix_release_manifest_snapshots(tmp_path, monkeypatch):
    """One backend instance describes and installs the same release.

    `capability()` re-read the packaged manifest while every install path used
    the snapshot taken at construction, so a plugin update landing between the
    two could show release B while `start()` still verified and registered
    cached release A.
    """
    import logging

    from ce_decky.managed_ce import ManagedCEManager

    manager = ManagedCEManager(
        tmp_path / "home", tmp_path / "ce", tmp_path / "tmp", None, logging.getLogger("managed-snapshot"),
    )
    constructed = manager.release
    assert constructed is not None, "the plugin ships a reviewed release manifest"

    def _replaced():
        raise ValueError("the packaged manifest was replaced")

    monkeypatch.setattr("ce_decky.managed_ce.load_release_manifest", _replaced)
    capability = manager.capability()
    # Still the snapshot this instance would actually install.
    assert capability["release"] == constructed.public()
    assert capability["managed_install_available"] is True
    assert manager._require_release() is constructed


def test_managed_ce_capability_uses_native_extraction_without_proton_setup_identity():
    capability = managed_ce.managed_ce_capability()
    assert capability == {
        "schema": 3,
        "mode": "managed_install",
        "managed_install_available": True,
        "release_manifest_loaded": True,
        "network_download_enabled": True,
        "native_extraction_enabled": True,
        "reason": "Download and extract the reviewed Windows Cheat Engine release locally; Proton is used only when launching Cheat Engine.",
        "release": {
            "visible_version": "7.7",
            "artifact_filename": "CheatEngine77.exe",
            "sha256": "cf0f4b6002555677984233c95856683959be83a5def3dc12175a8296eb22676b",
            "size": 34690856,
            "reviewed_at": "2026-08-22",
            "rediscovery_available": True,
        },
    }
    assert not any("url" in key.lower() or "command" in key.lower() for key in capability)
