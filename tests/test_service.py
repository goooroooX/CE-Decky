from pathlib import Path
import asyncio
import json
from hashlib import sha256
import logging
import struct
import threading
import time
from unittest.mock import Mock, patch

import pytest

from ce_decky import __version__
from ce_decky.ce_import import ImportedCE
from ce_decky.ce_runtime import BRIDGE_NAME, MANIFEST_NAME, _runtime_key
from ce_decky.paths import PluginPaths
from ce_decky.operations import drained_to_thread
from ce_decky.service import PluginService, _bounded_directory_size
import ce_decky.service as service_module


def _write_fake_pe(path: Path) -> None:
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    path.write_bytes(data)


def test_service_initializes_and_selftests(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    status = service.get_status()
    assert status["version"] == __version__
    assert status["plugin_dir"] == str(paths.plugin_dir)
    assert status["features"]["launch_integration"] is True
    result = service.self_test()
    assert result["ok"] is True
    names = {check["name"] for check in result["checks"]}
    assert "tls_trust_roots" in names
    assert paths.config_path.is_file()


def test_imported_ce_must_resolve_under_decky_user_home(tmp_path: Path):
    import struct

    paths = PluginPaths.for_tests(tmp_path / "root")
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    external = tmp_path / "external" / "Cheat Engine.exe"
    external.parent.mkdir(parents=True)
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    external.write_bytes(data)
    try:
        service.import_ce(str(external))
    except ValueError as exc:
        assert "DECKY_USER_HOME" in str(exc)
    else:
        raise AssertionError("external CE path was accepted")


def test_status_detects_ce_byte_drift_after_import(tmp_path: Path):
    import struct

    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    exe = paths.user_home / "CE" / "Cheat Engine.exe"
    exe.parent.mkdir(parents=True)
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    exe.write_bytes(data)
    service.import_ce(str(exe))
    assert service.get_status()["ce"]["valid"] is True
    data[-1] = 1
    exe.write_bytes(data)
    status = service.get_status()["ce"]
    assert status["valid"] is False
    assert "changed since import" in status["reason"]


def test_import_ce_rejects_executable_directly_in_user_home(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    exe = paths.user_home / "Cheat Engine.exe"
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    exe.write_bytes(data)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    with pytest.raises(ValueError, match="dedicated installation directory"):
        service.import_ce(str(exe))


def test_import_ce_rejects_ce_decky_managed_root_as_source(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    exe = paths.managed_root / "ce" / "runtime" / "fake" / "Cheat Engine.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    exe.write_bytes(data)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    with pytest.raises(ValueError, match="original Cheat Engine installation"):
        service.import_ce(str(exe))


def test_status_rejects_imported_ce_path_that_later_resolves_outside_home(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    install = paths.user_home / "CE"
    install.mkdir(parents=True)
    ce = install / "Cheat Engine.exe"
    _write_fake_pe(ce)
    service.import_ce(str(ce))

    outside = tmp_path / "outside" / "Cheat Engine.exe"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(ce.read_bytes())
    ce.unlink()
    ce.symlink_to(outside)

    status = service.get_status()
    assert status["ce"]["configured"] is True
    assert status["ce"]["valid"] is False
    assert "outside DECKY_USER_HOME" in status["ce"]["reason"]


def test_service_exposes_offline_provider_contract_diagnostics_and_removal_readiness(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    capabilities = service.get_provider_capabilities()
    assert [item["provider"] for item in capabilities[:4]] == ["fearless", "playground", "github", "thecheatscript"]
    assert all(item["network_state"] == "ready" for item in capabilities[:3])
    assert capabilities[-1]["network_state"] == "disabled"
    # The capability list is the whole registry, and every enabled, ready
    # provider in it is one a search actually queries.
    assert [item["discovery"] for item in capabilities[:4]] == ["search", "search", "search", "search"]
    assert service.get_provider_diagnostics() == {"schema": 1, "providers": {}}
    assert service.get_status()["features"]["provider_offline_core"] is True
    assert service.get_status()["features"]["provider_network"] is True

    service.save_profile(77, "Game", False, None, None)
    ready = service.get_removal_readiness()
    assert ready["can_delete_managed_data"] is True
    assert ready["blockers"] == []

    diag = service.diagnostics_snapshot()
    assert diag["storage"]["profiles"] == 1
    assert diag["capabilities"]["provider_offline_core"] is True
    assert "providers" in diag
    # The FearLess listing index has a refresh cycle of its own, so Advanced
    # has to be able to read when it was last fully refreshed and what the last
    # background pass fetched.
    index = diag["fearless_index"]
    assert index["refresh_age_seconds"] == 24 * 60 * 60
    assert index["stale_pages"] == 0
    assert index["last_refresh_pages"] == 0
    assert index["fully_refreshed_at"] is None
    assert index["last_refresh_at"] is None


def test_service_provider_query_plan_and_local_candidate_ranking(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    plan = service.plan_provider_search("ELEX II - GOG", r"C:\\Games\\ELEX2.exe")
    assert plan["network_enabled"] is True
    assert plan["queries"][0] == "elex 2 gog"
    assert "elex 2" in plan["queries"]
    # Exactly the providers the search runs, so a plan step never describes a
    # search that is not going to happen.
    assert [item["provider"] for item in plan["providers"]] == [
        "fearless", "playground", "github", "thecheatscript", "vgtimes",
    ]
    assert all(item["discovery"] == "search" for item in plan["providers"])

    ranked = service.evaluate_provider_candidates(
        "Resident Evil 4 (2023)", None,
        [
            {"title": "Resident Evil 4", "provider_trust": .8, "release_year": 2005},
            {"title": "Resident Evil 4 Remake", "provider_trust": .8, "release_year": 2023},
        ],
    )
    assert ranked["action"] == "auto"
    assert ranked["ranked"][0]["candidate"]["title"] == "Resident Evil 4 Remake"

    with pytest.raises(ValueError, match="malformed"):
        service.evaluate_provider_candidates("Game", None, [{"title": "Game", "unexpected": True}])


def test_service_reports_manifest_pinned_managed_install(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    managed = service.get_managed_ce_capability()
    assert managed["mode"] == "managed_install"
    assert managed["managed_install_available"] is True
    assert managed["release_manifest_loaded"] is True
    assert managed["network_download_enabled"] is True
    assert managed["native_extraction_enabled"] is True
    # The capability describes the shipped install, and says nothing about which
    # target gate once covered it.
    assert "gate" not in managed


def test_managed_ce_start_reserves_runtime_identity_before_async_manager_start(monkeypatch, tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    operation_id = "e" * 32

    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fake_start(*, force: bool = False):
            assert force is True
            entered.set()
            await release.wait()
            return {
                "operation_id": operation_id,
                "state": "downloading",
                "progress": None,
                "message": "Downloading",
                "error": None,
                "installed": None,
            }

        monkeypatch.setattr(service.managed_ce, "start", fake_start)
        task = asyncio.create_task(service.start_managed_ce_install(True))
        await entered.wait()
        with pytest.raises(ValueError, match="managed Cheat Engine setup is in progress"):
            service.clear_ce_import()
        with pytest.raises(ValueError, match="managed Cheat Engine setup is in progress"):
            service.prepare_private_ce_runtime()
        release.set()
        operation = await task
        assert operation["operation_id"] == operation_id
        assert service._managed_ce_reservation == operation_id

    asyncio.run(scenario())

    monkeypatch.setattr(service.managed_ce, "status", lambda _operation_id: {
        "operation_id": operation_id,
        "state": "failed",
        "progress": None,
        "message": "Failed",
        "error": "boom",
        "installed": None,
    })
    assert service.poll_managed_ce_install(operation_id)["state"] == "failed"
    assert service._managed_ce_reservation is None
    service.clear_ce_import()


def test_managed_ce_completion_consumes_operation_after_identity_commit(monkeypatch, tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    operation_id = "f" * 32
    installed = ImportedCE(
        executable=str(paths.ce_root / "installations" / ("a" * 64) / "Cheat Engine.exe"),
        root=str(paths.ce_root / "installations" / ("a" * 64)),
        sha256="a" * 64,
        size=300_000,
    )
    consumed = Mock()
    service._managed_ce_reservation = operation_id
    monkeypatch.setattr(service.managed_ce, "completed_install", lambda _operation_id: installed)
    monkeypatch.setattr(service.managed_ce, "consume_completed", consumed)

    result = service.complete_managed_ce_install(operation_id)

    assert result == {**installed.as_dict(), "completed_now": True}
    config = service.config_store.load()
    assert config.imported_ce_executable == installed.executable
    assert config.imported_ce_root == installed.root
    assert config.imported_ce_sha256 == installed.sha256
    consumed.assert_called_once_with(operation_id)
    assert service._managed_ce_reservation is None

    repeated = service.complete_managed_ce_install(operation_id)
    assert repeated == {**installed.as_dict(), "completed_now": False}
    consumed.assert_called_once_with(operation_id)

    # A consumed operation must report as unknown even to the observer that
    # holds the completion receipt. Serving a sticky `completed` snapshot lets a
    # superseded frontend monitor repaint terminal setup progress over the
    # cleared one and lock the home workflow.
    with pytest.raises(ValueError, match="managed CE install operation is unknown"):
        service.poll_managed_ce_install(operation_id)

    with pytest.raises(ValueError, match="managed CE setup changed"):
        service.complete_managed_ce_install("a" * 32)
    with pytest.raises(ValueError, match="managed CE install operation is unknown"):
        service.poll_managed_ce_install("a" * 32)


def test_removal_readiness_fails_closed_on_orphan_or_corrupt_session_state(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    orphan = paths.state_root / "sessions" / "88"
    orphan.mkdir(parents=True)
    (orphan / "unexpected.txt").write_text("x", encoding="utf-8")
    readiness = service.get_removal_readiness()
    assert readiness["can_delete_managed_data"] is False
    assert readiness["session_corrupt_entries"] == 1
    assert any("session state" in blocker for blocker in readiness["blockers"])


def test_diagnostics_snapshot_reports_corrupt_subsystems_instead_of_failing(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    service.provider_diagnostics.path.parent.mkdir(parents=True, exist_ok=True)
    service.provider_diagnostics.path.write_text("not-json", encoding="utf-8")
    sessions = paths.state_root / "sessions"
    sessions.parent.mkdir(parents=True, exist_ok=True)
    if sessions.exists() or sessions.is_symlink():
        if sessions.is_dir() and not sessions.is_symlink():
            import shutil
            shutil.rmtree(sessions)
        else:
            sessions.unlink()
    outside = tmp_path / "outside-sessions"
    outside.mkdir()
    sessions.symlink_to(outside, target_is_directory=True)

    snapshot = service.diagnostics_snapshot()
    assert snapshot["providers"] == {}
    assert snapshot["provider_state_error"]
    assert snapshot["sessions"] == {"apps": [], "total_sessions": 0, "errors": []}
    assert snapshot["session_state_error"]


def test_status_and_selftest_remain_diagnostic_when_config_is_corrupt(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    paths.config_path.write_text('{"schema":1,"schema":1}', encoding="utf-8")

    status = service.get_status()
    assert status["config_state_reason"]
    assert status["ce"]["configured"] is False
    assert status["ce"]["valid"] is False
    result = service.self_test()
    check = next(item for item in result["checks"] if item["name"] == "config_state")
    assert check["ok"] is False
    assert result["ok"] is False

    snapshot = service.diagnostics_snapshot()
    assert snapshot["config_state_error"]


def test_status_and_diagnostics_survive_pathological_table_catalog(monkeypatch, tmp_path: Path):
    import ce_decky.table_store as table_store
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    service.table_store.meta_root.mkdir(parents=True, exist_ok=True)
    (service.table_store.meta_root / "a").write_text("x", encoding="utf-8")
    (service.table_store.meta_root / "b").write_text("x", encoding="utf-8")
    monkeypatch.setattr(table_store, "MAX_TABLE_METADATA_ENTRIES", 1)
    status = service.get_status()
    assert status["tables"] == []
    assert "entry limit" in status["table_state_reason"]
    snapshot = service.diagnostics_snapshot()
    assert "entry limit" in snapshot["table_state_error"]
    result = service.self_test()
    assert next(c for c in result["checks"] if c["name"] == "table_catalog_state")["ok"] is False


def test_status_bounds_profile_catalog_diagnostic(monkeypatch, tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    def fail_listing():
        raise ValueError("x" * 1_024)

    monkeypatch.setattr(service.profile_store, "list_profiles", fail_listing)
    status = service.get_status()

    assert status["profiles"] == []
    assert status["profile_state_reason"] == "x" * 512


def test_service_close_drains_every_owner_after_one_close_fails():
    events: list[str] = []

    class Owner:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def close(self) -> None:
            events.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} failed")

    service = object.__new__(PluginService)
    service.logger = logging.getLogger("test-service-close")
    service.ce_launch = Owner("ce-launch", fail=True)  # type: ignore[assignment]
    service.managed_ce = Owner("managed-ce")  # type: ignore[assignment]
    service.provider_catalog = Owner("provider-catalog")  # type: ignore[assignment]
    service.acquisitions = Owner("acquisitions")  # type: ignore[assignment]
    service.plugin_updates = Owner("plugin-updates")  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="ce-launch failed"):
        asyncio.run(service.close())

    assert events == ["ce-launch", "managed-ce", "provider-catalog", "acquisitions", "plugin-updates"]


def test_service_close_drains_every_owner_after_one_close_is_cancelled():
    events: list[str] = []

    class Owner:
        def __init__(self, name: str, *, cancel: bool = False) -> None:
            self.name = name
            self.cancel = cancel

        async def close(self) -> None:
            events.append(self.name)
            if self.cancel:
                raise asyncio.CancelledError

    service = object.__new__(PluginService)
    service.logger = logging.getLogger("test-service-close-cancelled")
    service.ce_launch = Owner("ce-launch", cancel=True)  # type: ignore[assignment]
    service.managed_ce = Owner("managed-ce")  # type: ignore[assignment]
    service.provider_catalog = Owner("provider-catalog")  # type: ignore[assignment]
    service.acquisitions = Owner("acquisitions")  # type: ignore[assignment]
    service.plugin_updates = Owner("plugin-updates")  # type: ignore[assignment]

    # An owner that drained through cancellation re-raises it; the owners after
    # it still have their own processes, cache writes and sockets to release.
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.close())

    assert events == ["ce-launch", "managed-ce", "provider-catalog", "acquisitions", "plugin-updates"]


def test_service_close_says_which_owner_it_is_on_before_it_gets_there(caplog):
    """The one outcome this loop cannot report for itself is an owner that hangs.

    Decky gives a plugin five seconds to stop and then sends SIGKILL, so a close
    that blocks writes nothing at all: the record ends on whatever came before
    it. That is what was observed on the device, where the journal ended after
    the operation registry closed and the plugin was killed five seconds later,
    leaving which owner held the unload to be guessed from the gap.
    """
    entered: list[str] = []

    class Owner:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        async def close(self) -> None:
            entered.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} failed")

    service = object.__new__(PluginService)
    service.logger = logging.getLogger("test-service-close-records")
    service.ce_launch = Owner("ce-launch")  # type: ignore[assignment]
    service.managed_ce = Owner("managed-ce", fail=True)  # type: ignore[assignment]
    service.provider_catalog = Owner("provider-catalog")  # type: ignore[assignment]
    service.acquisitions = Owner("acquisitions")  # type: ignore[assignment]
    service.plugin_updates = Owner("plugin-updates")  # type: ignore[assignment]

    with caplog.at_level(logging.INFO, logger="test-service-close-records"):
        with pytest.raises(RuntimeError, match="managed-ce failed"):
            asyncio.run(service.close())

    messages = [record.getMessage() for record in caplog.records]
    # Entered before the owner runs, so the last "started" with no "ended" after
    # it names the owner that never came back.
    started = [message for message in messages if "event=backend.owner_close_started" in message]
    ended = [message for message in messages if "event=backend.owner_close_ended" in message]
    assert len(started) == 5
    assert len(ended) == 5
    for owner in ("ce_launch", "managed_ce", "provider_catalog", "acquisitions", "plugin_updates"):
        assert any(f"owner={owner}" in message for message in started), owner
    # The failure is recorded as the outcome of that owner rather than left to
    # be inferred from a separate failure record.
    assert any("owner=managed_ce" in message and "outcome=failed" in message for message in ended)
    assert any("owner=ce_launch" in message and "outcome=completed" in message for message in ended)
    assert entered == ["ce-launch", "managed-ce", "provider-catalog", "acquisitions", "plugin-updates"]


def test_cancelled_thread_mutation_is_drained_before_cancellation_returns():
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def mutation() -> None:
        started.set()
        release.wait(timeout=5)
        finished.set()

    async def exercise() -> None:
        task = asyncio.create_task(drained_to_thread(mutation))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(exercise())


def test_directory_inventory_bounds_directories_as_well_as_files(tmp_path: Path, monkeypatch):
    import ce_decky.service as service_module

    root = tmp_path / "inventory"
    root.mkdir()
    for index in range(3):
        (root / str(index)).mkdir()
    monkeypatch.setattr(service_module, "MAX_INVENTORY_ENTRIES", 2)

    files, total, truncated, error = _bounded_directory_size(root)
    assert (files, total, truncated, error) == (0, 0, True, None)


def test_relaunch_rebuilds_the_runtime_cheat_engine_dirtied_and_refuses_while_ce_is_live(tmp_path: Path, monkeypatch):
    """Cheat Engine writes into its own directory, which blocked every later launch."""
    import struct

    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    root = paths.user_home / "CE"
    (root / "autorun").mkdir(parents=True)
    (root / "main.lua").write_text("require('defines')\n", encoding="utf-8")
    (root / "autorun" / "upstream.lua").write_text("print('upstream')\n", encoding="utf-8")
    exe = root / "Cheat Engine.exe"
    data = bytearray(300_000)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    exe.write_bytes(data)
    service.import_ce(str(exe))

    first = service.prepare_private_ce_runtime()
    ceshare = Path(str(first["root"])) / "autorun" / "ceshare"
    ceshare.mkdir()
    (ceshare / "processlist.txt").write_text("74a54d29c321bd7298fd4de3e46f26b0\n", encoding="utf-8")

    # A live owned Cheat Engine must not have its tree replaced underneath it.
    monkeypatch.setattr(service.ce_launch, "has_live_owned_launch", lambda **_: True)
    with pytest.raises(ValueError, match="stop Cheat Engine and try again"):
        service.prepare_private_ce_runtime()

    monkeypatch.setattr(service.ce_launch, "has_live_owned_launch", lambda **_: False)
    second = service.prepare_private_ce_runtime()
    assert second == first
    assert not (ceshare / "processlist.txt").exists()


def test_version_backfill_cannot_resurrect_a_concurrently_replaced_ce(tmp_path: Path, monkeypatch):
    """Labelling a legacy registration must never roll back a committed identity.

    The label pass fills in a missing version for a Cheat Engine registered
    before this plugin read versions. It holds no mutation lock while it reads
    the PE, so an import committed in that gap would be overwritten if it saved
    the whole config snapshot it began with. The backfill therefore re-reads
    under the lock and commits only while the exact identity it measured is
    still the registered one.
    """
    import ce_decky.service as service_module

    # The fixture PE carries no version resource; the label itself is not what
    # this test is about, only the write that records it.
    monkeypatch.setattr(service_module, "read_pe_version", lambda path: "7.7")

    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    first = paths.user_home / "CE-A" / "Cheat Engine.exe"
    first.parent.mkdir(parents=True)
    _write_fake_pe(first)
    service.import_ce(str(first))

    second = paths.user_home / "CE-B" / "Cheat Engine.exe"
    second.parent.mkdir(parents=True)
    _write_fake_pe(second)
    second.write_bytes(second.read_bytes() + b"\x00")

    # Reproduce the legacy registration the backfill exists for.
    stale = service.config_store.load()
    stale.imported_ce_version = None
    service.config_store.save(stale)
    assert service.config_store.load().imported_ce_executable == str(first)

    reading = threading.Event()
    committed = threading.Event()
    original = service._backfill_ce_version

    def backfill_after_a_competing_import(observed, version):
        reading.set()
        assert committed.wait(5)
        original(observed, version)

    service._backfill_ce_version = backfill_after_a_competing_import  # type: ignore[method-assign]

    label_result: list[object] = []
    worker = threading.Thread(target=lambda: label_result.append(service._label_legacy_ce_registration()))
    worker.start()
    assert reading.wait(5)
    service.import_ce(str(second))
    committed.set()
    worker.join(10)
    assert not worker.is_alive()

    assert service.config_store.load().imported_ce_executable == str(second)
    assert service.get_status()["ce"]["executable"] == str(second)


def test_reading_the_status_of_a_legacy_registration_writes_nothing(tmp_path: Path, monkeypatch):
    """`get_status()` is on the panel's poll and on every read-only helper.

    Labelling a pre-version registration used to happen inside it, so a call
    that promises to write nothing could commit durable config for that one
    case. The label now happens at load, which is an explicit mutation
    boundary, and the read path is a read again.
    """
    import ce_decky.service as service_module

    monkeypatch.setattr(service_module, "read_pe_version", lambda path: "7.7")
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    executable = paths.user_home / "CE" / "Cheat Engine.exe"
    executable.parent.mkdir(parents=True)
    _write_fake_pe(executable)
    service.import_ce(str(executable))

    # The legacy shape, written after load so nothing has labelled it.
    stale = service.config_store.load()
    stale.imported_ce_version = None
    service.config_store.save(stale)
    before = paths.config_path.read_bytes()

    status = service.get_status()

    assert paths.config_path.read_bytes() == before
    assert status["ce"]["valid"] is True
    # The label is absent rather than invented: the status reports what is
    # recorded, and the next load is what records it.
    assert status["ce"]["version"] is None


def test_a_legacy_registration_is_labelled_once_at_load(tmp_path: Path, monkeypatch):
    import ce_decky.service as service_module

    monkeypatch.setattr(service_module, "read_pe_version", lambda path: "7.7")
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    executable = paths.user_home / "CE" / "Cheat Engine.exe"
    executable.parent.mkdir(parents=True)
    _write_fake_pe(executable)
    service.import_ce(str(executable))
    stale = service.config_store.load()
    stale.imported_ce_version = None
    service.config_store.save(stale)

    PluginService(paths, logging.getLogger("test")).initialize()

    assert service.config_store.load().imported_ce_version == "7.7"


def test_an_unusable_legacy_registration_is_left_alone_at_load(tmp_path: Path, monkeypatch):
    """A registration that no longer validates gets no label and no write."""
    import ce_decky.service as service_module

    monkeypatch.setattr(service_module, "read_pe_version", lambda path: "7.7")
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    executable = paths.user_home / "CE" / "Cheat Engine.exe"
    executable.parent.mkdir(parents=True)
    _write_fake_pe(executable)
    service.import_ce(str(executable))
    stale = service.config_store.load()
    stale.imported_ce_version = None
    service.config_store.save(stale)
    executable.unlink()
    before = paths.config_path.read_bytes()

    PluginService(paths, logging.getLogger("test")).initialize()

    assert paths.config_path.read_bytes() == before


def test_runtime_commands_are_refused_for_a_cheat_engine_running_an_older_bridge(tmp_path: Path):
    """Bridge compatibility is a host boundary, not a panel decision.

    An attached Cheat Engine survives a plugin update on purpose, and Decky is
    known to retain overlapping frontend observers across a reload - so an older
    frontend can reach a freshly loaded backend without the guard the new one
    has and send current commands to the previous resident protocol.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    packaged = service._packaged_bridge_sha256()
    assert packaged

    # A record written by this build is compatible only when the strict full
    # manifest, aggregate directory key and live executable/bridge bytes agree.
    executable_bytes = b"exact recovered CE executable"
    executable_sha = __import__("hashlib").sha256(executable_bytes).hexdigest()
    tree_sha = "d" * 64
    runtime_key = _runtime_key(executable_sha, tree_sha, packaged)
    runtime_root = paths.ce_root / "runtime" / runtime_key
    (runtime_root / "autorun").mkdir(parents=True)
    executable = runtime_root / "cheatengine-x86_64.exe"
    executable.write_bytes(executable_bytes)
    (runtime_root / "autorun" / BRIDGE_NAME).write_bytes(service.bridge_source.read_bytes())
    manifest = {
        "root": str(runtime_root),
        "executable": str(executable),
        "source_executable_sha256": executable_sha,
        "source_tree_sha256": tree_sha,
        "bridge_sha256": packaged,
        "file_count": 1,
        "total_bytes": len(executable_bytes),
    }
    (runtime_root / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    current = {"executable": str(executable), "bridge_sha256": packaged}
    assert service._recovered_bridge_mismatch(current, expected_ce_sha256=executable_sha) is None

    # The same process after an update that changed the bridge.
    assert "previous CE Decky version" in (
        service._recovered_bridge_mismatch({**current, "bridge_sha256": "b" * 64}) or ""
    )

    # A record from before the bridge identity was recorded cannot claim a match.
    assert service._recovered_bridge_mismatch({**current, "bridge_sha256": ""}) is not None

    # The runtime it executes from was superseded or collected.
    assert "incompatible" in (
        service._recovered_bridge_mismatch(
            {"executable": str(paths.ce_root / "runtime" / "gone" / "ce.exe"), "bridge_sha256": packaged},
        ) or ""
    )

    # And the manifest itself is checked, not just the record.
    (runtime_root / MANIFEST_NAME).write_text(
        json.dumps({**manifest, "bridge_sha256": "c" * 64}), encoding="utf-8",
    )
    assert "incompatible" in (service._recovered_bridge_mismatch(current) or "")

    # A bridge-only manifest used to be accepted despite claiming the complete
    # CE/tree/bridge runtime identity.
    (runtime_root / MANIFEST_NAME).write_text(json.dumps({"bridge_sha256": packaged}), encoding="utf-8")
    assert "manifest schema" in (service._recovered_bridge_mismatch(current) or "")

    # Failure to hash this build's bridge is compatibility-unknown and must
    # block commands rather than silently authorize them.
    service._packaged_bridge_sha256 = lambda: None  # type: ignore[method-assign]
    assert "cannot identify its packaged" in (service._recovered_bridge_mismatch(current) or "")


def _managed_data_service(tmp_path: Path) -> tuple[PluginService, PluginPaths]:
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    for path, name in (
        (paths.tables_root, "table.CT"),
        (paths.ce_root, "cheatengine.exe"),
        (paths.state_root, "profiles.json"),
        (paths.cache_root, "fearless-index.json"),
        (paths.temp_root, "staged.part"),
        (paths.settings_dir, "config.json"),
        (paths.log_dir, "ce.log"),
    ):
        path.mkdir(parents=True, exist_ok=True)
        (path / name).write_text("x" * 16, encoding="utf-8")
    return service, paths


def _present(paths: PluginPaths) -> set[str]:
    named = {
        "tables": paths.tables_root, "ce": paths.ce_root, "state": paths.state_root,
        "cache": paths.cache_root, "tmp": paths.temp_root, "settings": paths.settings_dir,
        "logs": paths.log_dir,
    }
    return {key for key, path in named.items() if any(path.iterdir())}


def test_deleting_managed_data_clears_only_the_chosen_scope(tmp_path: Path):
    """Each scope is a superset of the one before it and nothing beyond it."""
    service, paths = _managed_data_service(tmp_path)
    assert _present(paths) == {"tables", "ce", "state", "cache", "tmp", "settings", "logs"}

    asyncio.run(service.delete_managed_data("cache"))
    assert _present(paths) == {"tables", "ce", "state", "settings"}

    service, paths = _managed_data_service(tmp_path / "second")
    asyncio.run(service.delete_managed_data("setup"))
    assert _present(paths) == {"tables", "ce", "settings"}

    service, paths = _managed_data_service(tmp_path / "third")
    asyncio.run(service.delete_managed_data("all"))
    assert _present(paths) == set()
    # The tree is re-established, so the next operation does not race a
    # missing directory.
    for path in (paths.tables_root, paths.ce_root, paths.state_root, paths.cache_root, paths.temp_root):
        assert path.is_dir()


def test_deleting_managed_data_rejects_an_unknown_scope(tmp_path: Path):
    service, paths = _managed_data_service(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        asyncio.run(service.delete_managed_data("../../etc"))
    assert _present(paths) == {"tables", "ce", "state", "cache", "tmp", "settings", "logs"}


def test_deleting_managed_data_refuses_while_an_owned_cheat_engine_runs(tmp_path: Path):
    """The blockers that stop removal stop deletion, re-read here not trusted."""
    service, paths = _managed_data_service(tmp_path)
    service.ce_launch.has_live_owned_launch = Mock(return_value=True)
    with pytest.raises(ValueError, match="still running"):
        asyncio.run(service.delete_managed_data("all"))
    assert _present(paths) == {"tables", "ce", "state", "cache", "tmp", "settings", "logs"}


def test_deleting_managed_data_refuses_while_a_download_is_in_flight(tmp_path: Path):
    """Staging belongs to that download until it reaches a terminal state."""
    service, paths = _managed_data_service(tmp_path)
    service.acquisitions.has_active = Mock(return_value=True)
    with pytest.raises(ValueError, match="download"):
        asyncio.run(service.delete_managed_data("cache"))
    assert "tmp" in _present(paths)


def test_deleting_managed_data_refuses_while_a_plugin_update_is_in_flight(tmp_path: Path):
    """An update owns a staged archive under the tree this deletes."""
    service, paths = _managed_data_service(tmp_path)
    service.plugin_updates.has_active_operation = Mock(return_value=True)
    with pytest.raises(ValueError, match="plugin update"):
        asyncio.run(service.delete_managed_data("all"))
    assert "tmp" in _present(paths)


def test_deleting_managed_data_never_follows_a_symlink_out_of_the_owned_tree(tmp_path: Path):
    """Recursive deletion through a symlink would delete outside the plugin."""
    service, paths = _managed_data_service(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link = paths.cache_root / "escape"
    link.symlink_to(outside, target_is_directory=True)

    asyncio.run(service.delete_managed_data("cache"))

    # The link itself goes; what it pointed at is untouched.
    assert not link.exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_a_stored_table_is_destroyed_only_when_no_game_is_on_it(tmp_path: Path):
    """The one route that destroys a user's table bytes, and its one refusal.

    Content-addressed and irreversible: the same bytes can be imported again
    from a file or a source, and on a device with neither they are gone. The
    case a user cannot undo by pressing something else is deleting the table a
    game currently has selected, which may be running right now and whose
    authorization is recorded against those exact bytes.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    source = tmp_path / "Game.CT"
    source.write_text("<CheatTable><CheatEntries/></CheatTable>", encoding="utf-8")
    stored = service.import_table(str(source), None, None, None)
    digest = str(stored["sha256"])
    assert any(item["sha256"] == digest for item in service.table_store.list_tables())

    service.save_profile(10, "Game", False, digest, None)
    with pytest.raises(ValueError, match="selected table for Game"):
        service.delete_table(digest)
    assert any(item["sha256"] == digest for item in service.table_store.list_tables())

    # Cleared from the game, and now it is the user's to remove.
    service.save_profile(10, "Game", False, None, None)
    removed = service.delete_table(digest)
    assert removed["sha256"] == digest
    assert service.table_store.list_tables() == []
    # The bytes are gone, not merely unlisted.
    with pytest.raises(ValueError):
        service.table_store.verified_blob(digest)
    # And a second press says so rather than pretending it worked.
    with pytest.raises(ValueError, match="no such table"):
        service.delete_table(digest)


def test_corrupt_profile_state_does_not_block_deleting_managed_data(tmp_path: Path):
    """A full reset is how a user escapes corrupt state, so it must not be gated on it.

    `can_delete_managed_data` reports whether the plugin is tidy to uninstall,
    and unreadable profile or session state counts against that. Reusing it here
    made the recovery path refuse in exactly the situation it exists for.
    """
    service, paths = _managed_data_service(tmp_path)
    (paths.state_root / "profiles.json").write_text("{ not json", encoding="utf-8")
    assert service.get_removal_readiness()["can_delete_managed_data"] is False

    asyncio.run(service.delete_managed_data("all"))
    assert _present(paths) == set()


def test_deleting_managed_data_refuses_while_cheat_engine_is_being_installed(tmp_path: Path):
    """The extractor is writing into `ce_root`; deleting it destroys the install."""
    service, paths = _managed_data_service(tmp_path)
    service.managed_ce.has_active_operation = Mock(return_value=True)
    with pytest.raises(ValueError, match="installation is still in progress"):
        asyncio.run(service.delete_managed_data("all"))
    assert _present(paths) == {"tables", "ce", "state", "cache", "tmp", "settings", "logs"}


def test_the_setup_scope_never_orphans_the_installation_it_keeps(tmp_path: Path):
    """Which Cheat Engine is registered lives in the settings directory.

    Deleting that while keeping `ce` left a multi-gigabyte installation the
    panel reported as absent and only a full reset could reach.
    """
    service, paths = _managed_data_service(tmp_path)
    asyncio.run(service.delete_managed_data("setup"))
    assert any(paths.ce_root.iterdir())
    assert paths.config_path.is_file() or any(paths.settings_dir.iterdir())


def test_a_directory_that_cannot_be_cleared_is_reported_not_hidden(tmp_path: Path):
    """A partly completed deletion says what went, not that nothing did."""
    service, paths = _managed_data_service(tmp_path)
    real = service_module._clear_managed_directory

    def fail_on_logs(key: str, label: str, path: Path):
        if key == "logs":
            return {"key": key, "label": label, "removed_files": 0, "removed_bytes": 0, "error": "permission denied"}
        return real(key, label, path)

    with patch.object(service_module, "_clear_managed_directory", side_effect=fail_on_logs):
        result = asyncio.run(service.delete_managed_data("cache"))

    # The cache really was cleared, and the caller is told both halves.
    assert not any(paths.cache_root.iterdir())
    assert [item["key"] for item in result["failed"]] == ["logs"]
    cleared = {item["key"] for item in result["deleted"] if item["error"] is None}
    assert {"cache", "tmp"} <= cleared


def test_counting_deleted_bytes_never_walks_through_a_symlinked_directory(tmp_path: Path):
    """`Path.rglob` follows them on Python 3.11, inside a destructive call."""
    service, paths = _managed_data_service(tmp_path)
    outside = tmp_path / "elsewhere"
    (outside / "deep").mkdir(parents=True)
    for index in range(5):
        (outside / "deep" / f"{index}.bin").write_bytes(b"y" * 4096)
    nested = paths.cache_root / "nested"
    nested.mkdir()
    (nested / "own.bin").write_bytes(b"z" * 8)
    (nested / "escape").symlink_to(outside, target_is_directory=True)

    result = asyncio.run(service.delete_managed_data("cache"))

    cache = next(item for item in result["deleted"] if item["key"] == "cache")
    # Only the bytes actually inside the owned tree are counted or removed.
    assert cache["removed_bytes"] < 4096
    assert (outside / "deep" / "0.bin").is_file()


def test_a_search_cannot_restore_the_index_between_the_stop_and_the_delete(tmp_path: Path):
    """Stopping the running index is a pre-pass an ordinary search undoes."""
    service, _paths = _managed_data_service(tmp_path)
    catalog = service.provider_catalog

    async def exercise() -> None:
        await catalog.suspend_index()
        catalog._fearless_total_pages = 4
        catalog._fearless_pages = {}
        # A search arriving now must not start a background index that would
        # write the file back after deletion.
        catalog._start_fearless_index()
        assert catalog._fearless_index_task is None
        catalog.resume_index()
        catalog._start_fearless_index()

    asyncio.run(exercise())


def test_the_background_index_still_runs_after_its_cache_is_deleted(tmp_path: Path):
    """The daily refresh is driven by search; deletion must not disable it.

    Suspension is what stops a search restoring the index mid-deletion, so a
    suspension that outlived the operation would leave the plugin never
    indexing again - and only a plugin reload would recover it.
    """
    service, _paths = _managed_data_service(tmp_path)
    catalog = service.provider_catalog

    asyncio.run(service.delete_managed_data("cache"))

    assert catalog._index_suspended is False
    # Once a search has learned the page count again the pass starts normally.
    # Both halves of that search, because the pass is armed by Search as well as
    # taught by it, and both of the files the marker is written to were in the
    # cache directory the deletion emptied.
    catalog._fearless_total_pages = 2
    catalog._fearless_pages = {}
    catalog._note_search_activity()

    async def start() -> None:
        catalog._start_fearless_index()
        task = catalog._fearless_index_task
        assert task is not None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(start())


def test_a_refused_deletion_leaves_the_background_index_alone(tmp_path: Path):
    """Being refused is not a reason to cancel a pass that was already running."""
    service, _paths = _managed_data_service(tmp_path)
    catalog = service.provider_catalog
    service.ce_launch.has_live_owned_launch = Mock(return_value=True)
    suspended = Mock(side_effect=AssertionError("a refused deletion must not suspend the index"))
    catalog.suspend_index = suspended

    with pytest.raises(ValueError, match="still running"):
        asyncio.run(service.delete_managed_data("cache"))
    assert catalog._index_suspended is False


def _staged_acquisition(service, tmp_path: Path, state: str):
    """One acquisition in `state` that still owns a file in staging."""
    from ce_decky.acquisition import Acquisition
    from ce_decky.catalog import ArtifactRecord
    from ce_decky.providers import CatalogResult

    staged = service.paths.temp_root / "provider-downloads" / "Example.CT"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"table bytes")
    record = ArtifactRecord(CatalogResult(
        provider="playground", provider_display_name="Playground", topic_id="1",
        artifact_id="page-1:file-2", table_title="Example", filename="Example.CT",
        version=None, size_bytes=None, source_page="https://www.playground.ru/cheat/example-1",
        download_mode="direct_https", match_score=.9, provider_rank=85,
    ))
    item = Acquisition(
        acquisition_id="a" * 32, record=record, state=state,
        created=time.time(), updated=time.time(), staged_path=staged,
    )
    service.acquisitions.items[item.acquisition_id] = item
    return staged


@pytest.mark.parametrize("state", ["ready_to_import", "needs_selection"])
def test_a_downloaded_table_waiting_on_the_user_still_owns_its_staged_file(tmp_path: Path, state: str):
    """`TERMINAL_STATES` is terminal for the download, not for file ownership.

    A finished download waiting for a member choice or a password keeps its
    bytes in staging, and reading that set as "owns nothing" deleted the file
    out from under an import the user was still answering.
    """
    service, _paths = _managed_data_service(tmp_path / state)
    staged = _staged_acquisition(service, tmp_path, state)

    assert service.acquisitions.has_active() is True
    with pytest.raises(ValueError, match="download"):
        asyncio.run(service.delete_managed_data("cache"))
    assert staged.is_file()


def test_an_imported_acquisition_no_longer_blocks_deletion(tmp_path: Path):
    """Ownership ends when the staged file does, or nothing could ever be deleted."""
    service, _paths = _managed_data_service(tmp_path)
    _staged_acquisition(service, tmp_path, "imported")
    service.acquisitions.items["a" * 32].staged_path = None

    assert service.acquisitions.has_active() is False
    asyncio.run(service.delete_managed_data("cache"))


def test_no_new_download_may_start_while_plugin_data_is_being_deleted(tmp_path: Path):
    """Starting one does not pass through the mutation boundary a single check holds."""
    service, _paths = _managed_data_service(tmp_path)
    service.acquisitions.suspend()
    try:
        with pytest.raises(ValueError, match="being deleted"):
            asyncio.run(service.acquisitions.start("playground", "page-1:file-2"))
    finally:
        service.acquisitions.resume()
    # And the suspension is lifted by the deletion that raised it.
    asyncio.run(service.delete_managed_data("cache"))
    assert service.acquisitions._suspended is False


def test_a_reserved_cheat_engine_install_refuses_deletion_before_it_has_an_operation(tmp_path: Path):
    """The reservation covers the gap before the manager can publish anything.

    Asking only the manager let deletion through the one moment the reservation
    exists to close.
    """
    service, paths = _managed_data_service(tmp_path)
    service._managed_ce_reservation = "starting"
    assert service.managed_ce.has_active_operation() is False

    with pytest.raises(ValueError, match="installation is still in progress"):
        asyncio.run(service.delete_managed_data("all"))
    assert _present(paths) == {"tables", "ce", "state", "cache", "tmp", "settings", "logs"}


def test_the_size_report_refuses_a_managed_root_that_became_a_symlink(tmp_path: Path):
    """`is_dir()` follows the link, so the report measured through it."""
    from ce_decky.service import _bounded_directory_size

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"x" * 123)
    link = tmp_path / "linked-root"
    link.symlink_to(outside, target_is_directory=True)

    files, total, truncated, error = _bounded_directory_size(link)
    assert (files, total, truncated) == (0, 0, False)
    assert error == "path is a symlink"


@pytest.mark.asyncio
async def test_switching_a_source_off_stops_the_request_it_already_has_out(tmp_path: Path):
    """The whole press, from the switch the user presses to the request dropped.

    Checking the switch before starting background work and again before
    writing anything down leaves the one already in flight running: a user who
    switches a source off is entitled to have this device stop talking to it
    now, not once the current answer arrives.
    """
    import asyncio

    released = asyncio.Event()

    class _Blocked:
        async def get(self, url, **kwargs):
            await released.wait()
            raise AssertionError("the request should have been dropped")

    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    service.provider_catalog.network = _Blocked()
    service.provider_catalog._start_playground_sitemap({})
    task = service.provider_catalog._playground_index_task
    assert task is not None
    await asyncio.sleep(0)
    assert not task.done()

    service.set_provider_enabled("playground", False)

    released.set()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert service._disabled_provider_ids() == frozenset({"playground"})


def test_provider_sources_are_all_on_until_the_user_switches_one_off(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    snapshot = service.get_provider_sources()
    assert snapshot["enabled_count"] == snapshot["total"] > 0
    assert all(row["enabled"] for row in snapshot["sources"])
    # Never asked for anything, so nothing is recorded. That is a different
    # fact from every counter reading zero, and the row says so.
    assert all(row["state"] is None and row["counters"] is None for row in snapshot["sources"])

    after = service.set_provider_enabled("github", False)
    assert after["enabled_count"] == snapshot["total"] - 1
    assert {row["provider"] for row in after["sources"] if not row["enabled"]} == {"github"}
    assert service._disabled_provider_ids() == frozenset({"github"})
    # And the search itself is built from the same answer.
    assert "github" not in {
        item["provider"] for item in service.plan_provider_search("Example")["providers"]
    }
    assert service.plan_provider_search("Example")["switched_off"] == ["github"]

    assert service.set_provider_enabled("github", True)["enabled_count"] == snapshot["total"]


def test_only_a_source_this_build_can_reach_is_switchable(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    with pytest.raises(ValueError):
        service.set_provider_enabled("nothing-like-this", False)
    # Declared in the registry, but disabled in this build: not a choice to offer.
    with pytest.raises(ValueError):
        service.set_provider_enabled("cheatenginenet", False)
    with pytest.raises(ValueError):
        service.set_provider_enabled("github", "off")


def test_an_unreadable_source_choice_searches_everything_and_says_why(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    (paths.state_root / "provider_sources.json").write_text(
        json.dumps({"schema": 42, "disabled": ["github"]}), encoding="utf-8",
    )

    # Fail-open: a preference nobody can read must not be able to make the
    # plugin find nothing with no visible cause.
    assert service._disabled_provider_ids() == frozenset()
    snapshot = service.get_provider_sources()
    assert snapshot["enabled_count"] == snapshot["total"]
    assert snapshot["selection_reason"]
    assert service.diagnostics_snapshot()["provider_selection"]["reason"]

    # Resetting is the repair, and it does not depend on reading the file first.
    repaired = service.reset_provider_sources()
    assert repaired["selection_reason"] is None
    assert repaired["enabled_count"] == repaired["total"]


def test_switching_a_source_off_carries_its_recorded_counters(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    service.provider_diagnostics.record_search_success("github", results=3, latency_ms=120)
    service.provider_diagnostics.record_failure("vgtimes", error="HTTP 500", http_status=500)

    rows = {row["provider"]: row for row in service.set_provider_enabled("github", False)["sources"]}
    assert rows["github"]["enabled"] is False
    # The evidence the switch was made on survives the switch: switching a
    # source off is not clearing what it did.
    assert rows["github"]["counters"]["results"] == 3
    assert rows["github"]["state"] == "ready"
    assert rows["vgtimes"]["last_error"] == "HTTP 500"
    assert rows["vgtimes"]["last_http_status"] == 500


def test_deleting_the_provider_cache_drains_a_search_that_is_still_running(tmp_path: Path):
    """The rebuild must not come from the search that preceded the deletion."""
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    async def scenario():
        import types as _types

        started = asyncio.Event()
        release = asyncio.Event()
        catalog = service.provider_catalog

        async def slow(self, *args):
            started.set()
            await release.wait()
            return []

        async def empty(self, *args):
            return []

        # The real `search()` runs, so the task it registers is the thing the
        # deletion has to find and drain.
        catalog._fearless = _types.MethodType(slow, catalog)  # type: ignore[method-assign]
        for name in ("_playground", "_github_search", "_thecheatscript", "_vgtimes"):
            setattr(catalog, name, _types.MethodType(empty, catalog))

        running = asyncio.create_task(catalog.search("Example"))
        await started.wait()
        await service.delete_managed_data("cache")
        assert running.done()
        release.set()
        assert not (paths.cache_root / "provider-results.json").exists()
        # And searching works again once the deletion has finished.
        assert catalog._searches_suspended is False
        await catalog.close()

    asyncio.run(scenario())


def test_an_unreadable_counter_record_is_reported_and_repairable(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    (paths.cache_root / "providers.json").write_text('{"schema": 9, "providers": {}}', encoding="utf-8")

    snapshot = service.get_provider_sources()
    assert snapshot["diagnostics_reason"]
    # Never zeroed: nothing was measured, and a counter of zero is a different
    # and much stronger claim than "this could not be read".
    assert all(row["counters"] is None for row in snapshot["sources"])
    # The switches are unaffected by a broken counter record.
    assert snapshot["selection_reason"] is None

    repaired = asyncio.run(service.reset_provider_diagnostics())
    assert repaired["diagnostics_reason"] is None
    assert all(row["counters"] is None for row in repaired["sources"])
    service.provider_diagnostics.record_search_success("fearless", results=2, latency_ms=5)
    rows = {row["provider"]: row for row in service.get_provider_sources()["sources"]}
    assert rows["fearless"]["counters"]["results"] == 2


def test_switching_a_source_is_refused_while_the_record_cannot_be_read(tmp_path: Path):
    # The panel disables these for the same reason: the write reads the record
    # first, so every one of them is guaranteed to fail while it is corrupt.
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    (paths.state_root / "provider_sources.json").write_text(
        json.dumps({"schema": 42, "disabled": []}), encoding="utf-8",
    )
    with pytest.raises(ValueError):
        service.set_provider_enabled("github", False)
    # Replacing it unread is the action that works.
    assert service.reset_provider_sources()["selection_reason"] is None
    assert service.set_provider_enabled("github", False)["enabled_count"] > 0


def test_the_state_directory_report_names_everything_it_would_delete(tmp_path: Path):
    # This text is what a user confirms an irreversible deletion against, and
    # the scope erases the blocked-table record and the source choice as well as
    # the profiles it used to name.
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    purpose = next(
        entry["purpose"] for entry in service.get_removal_readiness()["directories"]
        if entry["key"] == "state"
    )
    assert "switched off" in purpose
    assert "marked as not working" in purpose


def test_cancelling_a_deletion_while_it_quiesces_leaves_nothing_suspended(tmp_path: Path):
    """Quiescing is awaits of its own, and cancellation there ran no deletion.

    Leaving the flags set refused every later download and search as though a
    deletion were still in progress, until the plugin was reloaded.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    async def scenario():
        entered = asyncio.Event()

        async def hang():
            entered.set()
            await asyncio.Event().wait()

        service.provider_catalog.suspend_searches = hang  # type: ignore[method-assign]
        deleting = asyncio.create_task(service.delete_managed_data("cache"))
        await entered.wait()
        deleting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deleting

        assert service.acquisitions._suspended is False
        assert service.provider_catalog._searches_suspended is False
        assert service.provider_catalog._index_suspended is False

    asyncio.run(scenario())


def test_a_report_that_fails_after_the_files_are_gone_is_not_a_failed_deletion(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    (paths.cache_root / "provider-results.json").write_text("{}", encoding="utf-8")

    def broken():
        raise ValueError("removal readiness could not be read")

    service.get_removal_readiness = broken  # type: ignore[method-assign]
    result = asyncio.run(service.delete_managed_data("cache"))

    # The deletion committed before the report was attempted, so raising here
    # would have told the panel nothing was removed for files that are gone.
    assert not (paths.cache_root / "provider-results.json").exists()
    assert result["readiness"] is None
    assert "could not be read" in str(result["readiness_error"])
    assert result["scope"] == "cache"


def test_resetting_the_counters_drains_a_search_that_would_repopulate_them(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    (paths.cache_root / "providers.json").write_text('{"schema": 9, "providers": {}}', encoding="utf-8")

    async def scenario():
        import types as _types

        started = asyncio.Event()
        release = asyncio.Event()
        catalog = service.provider_catalog

        async def slow(self, *args):
            started.set()
            await release.wait()
            return []

        async def empty(self, *args):
            return []

        catalog._fearless = _types.MethodType(slow, catalog)  # type: ignore[method-assign]
        for name in ("_playground", "_github_search", "_thecheatscript", "_vgtimes"):
            setattr(catalog, name, _types.MethodType(empty, catalog))

        running = asyncio.create_task(catalog.search("Example"))
        await started.wait()
        repaired = await service.reset_provider_diagnostics()
        # A search started before the repair is a writer of the record being
        # replaced, and would have put its own counters into the new one.
        assert running.done()
        assert repaired["diagnostics_reason"] is None
        assert all(row["counters"] is None for row in repaired["sources"])
        assert catalog._searches_suspended is False
        release.set()
        await catalog.close()

    asyncio.run(scenario())


def test_cancelling_a_counter_reset_leaves_searching_available(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()

    async def scenario():
        entered = asyncio.Event()

        async def hang():
            service.provider_catalog._searches_suspended = True
            entered.set()
            await asyncio.Event().wait()

        service.provider_catalog.suspend_searches = hang  # type: ignore[method-assign]
        resetting = asyncio.create_task(service.reset_provider_diagnostics())
        await entered.wait()
        resetting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await resetting
        assert service.provider_catalog._searches_suspended is False

    asyncio.run(scenario())


def test_bulk_removal_does_not_report_success_when_directory_sync_fails(tmp_path, monkeypatch):
    root = tmp_path / "owned"
    root.mkdir()
    (root / "entry").write_text("data")
    synced = []
    def refuse(path):
        synced.append(path)
        raise OSError("directory durability unknown")
    monkeypatch.setattr(service_module, "fsync_directory", refuse)
    result = service_module._clear_managed_directory("cache", "Cache", root)
    assert synced == [root]
    assert list(root.iterdir()) == []
    assert "durability unknown" in result["error"]


def test_a_slow_cache_write_never_delays_stopping_an_owned_cheat_engine(monkeypatch):
    """The prologue's mandatory half must not queue behind a cache write.

    The unload prologue is the only part of an unload this host reliably runs,
    and the catalog's half of it is a write to disk. Any write that is slow for
    any reason, in front of the owned-process stop, spends Decky's five seconds
    on durability and leaves a Cheat Engine running, which is the one thing the
    prologue exists to prevent. The order is what guarantees it, so the order is
    what is fixed here rather than the cost of any particular write.
    """
    order: list[str] = []
    held = threading.Event()
    release = threading.Event()

    class Catalog:
        def begin_close(self) -> None:
            order.append("catalog")
            held.set()
            # What a contended lock feels like from here.
            assert release.wait(timeout=5)

    class Launch:
        def begin_close(self) -> None:
            order.append("ce_launch")

    class Updates:
        def begin_close(self) -> None:
            order.append("plugin_updates")

    service = object.__new__(PluginService)
    service.logger = logging.getLogger("test-begin-close-order")
    service.ce_launch = Launch()  # type: ignore[assignment]
    service.provider_catalog = Catalog()  # type: ignore[assignment]
    service.plugin_updates = Updates()  # type: ignore[assignment]

    worker = threading.Thread(target=service.begin_close)
    worker.start()
    try:
        assert held.wait(timeout=5)
        # The stop has already happened by the time the cache write is reached.
        assert order == ["ce_launch", "catalog"]
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_the_self_test_answers_for_the_evidence_the_plugin_depends_on(tmp_path, monkeypatch):
    """What a self-test is for is the failure nobody would otherwise notice.

    Two of these were silently broken on a real device for as long as they
    existed: the system journal could not be collected at all, because
    `journalctl` inherited the loader bundle's library path, and it was found by
    opening a support archive rather than by anything asking. The panel's own
    durable record has the same shape: it matters only at the moment somebody
    needs it, and by then it is too late to learn it was unwritable.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("self-test"))
    service.initialize()

    named = {check["name"]: check for check in service.self_test()["checks"]}

    assert named["panel_journal"]["ok"] is True
    assert named["managed_root_space"]["ok"] is True
    # Nothing is registered in a fresh state directory, and that is not a fault.
    assert named["cheat_engine_identity"]["ok"] is True
    assert "no Cheat Engine registered" in str(named["cheat_engine_identity"]["detail"])

    # A journal that cannot be read is reported rather than passed over.
    monkeypatch.setattr(service_module.journal_records, "available", lambda: True)
    monkeypatch.setattr(
        service_module.journal_records, "collect",
        lambda **kwargs: {"ok": False, "reason": "libcrypto.so.3: version OPENSSL_3.4.0 not found", "lines": []},
    )
    broken = {check["name"]: check for check in service.self_test()["checks"]}
    assert broken["system_journal"]["ok"] is False
    assert "OPENSSL_3.4.0" in str(broken["system_journal"]["detail"])
    # Diagnostics never refuse the plugin: a journal nobody can read is worth
    # saying and is not a reason to call the installation broken.
    assert broken["system_journal"]["blocking"] is False
    assert service.self_test()["ok"] is True


def test_a_panel_record_left_readable_is_a_self_test_failure(tmp_path, monkeypatch):
    """It holds game names, table identities and refusals, and it is 0600."""
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("self-test-mode"))
    service.initialize()
    journal = service_module.frontend_journal.journal_path(paths.state_root)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}\n", encoding="utf-8")
    journal.chmod(0o644)

    named = {check["name"]: check for check in service.self_test()["checks"]}

    assert named["panel_journal"]["ok"] is False
    assert "0600" in str(named["panel_journal"]["detail"])


def test_preferences_move_out_of_the_configuration_at_load(tmp_path: Path):
    """What an 0.9.28 build wrote into the identity file is lifted out of it.

    Observed on a Steam Deck: this version put two preferences in `config.json`,
    the published release before it parses that file strictly, and the device
    came back from an update saying Cheat Engine was not installed while the
    installation and its registration sat untouched on disk. The choices are the
    user's, so they are moved rather than dropped, and the file goes back to the
    shape every build reads.
    """
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    paths.config_path.write_text(json.dumps({
        "schema": 1,
        "imported_ce_executable": "/home/u/CE/ce.exe",
        "imported_ce_sha256": "c" * 64,
        "imported_ce_root": "/home/u/CE",
        "acknowledged_security_notice": True,
        "update_auto_check": False,
        "mascot_visible": False,
    }), encoding="utf-8")

    service = PluginService(paths, logging.getLogger("test-preferences"))
    service.initialize()

    written = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert "update_auto_check" not in written and "mascot_visible" not in written
    # The registration survives the move untouched, which is the whole point.
    assert written["imported_ce_sha256"] == "c" * 64
    assert written["acknowledged_security_notice"] is True
    # And the user's own choices are where they belong now.
    stored = service.preferences.load()
    assert (stored.update_auto_check, stored.mascot_visible) == (False, False)
    assert service.get_status()["preferences"] == {"mascot_visible": False}
    assert service.get_status()["config_state_reason"] is None


def test_a_switch_is_stored_where_an_older_build_will_not_trip_over_it(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test-preferences-write"))
    service.initialize()
    service.set_mascot_visible(False)
    service.set_update_auto_check(False)

    # Nothing about the identity file changed, so every build still reads it.
    written = json.loads(paths.config_path.read_text(encoding="utf-8"))
    assert set(written) == {
        "schema", "imported_ce_executable", "imported_ce_sha256",
        "imported_ce_root", "imported_ce_version", "acknowledged_security_notice",
    }
    stored = json.loads((paths.settings_dir / "preferences.json").read_text(encoding="utf-8"))
    assert stored == {"schema": 1, "update_auto_check": False, "mascot_visible": False}


def test_a_switch_changed_since_the_move_is_not_overwritten_by_the_old_copy(tmp_path: Path):
    """The configuration's copy is a seed, used once, and only where there is
    nothing else: a device whose configuration still carries it because an
    earlier attempt could not finish must not have today's choice replaced by
    last week's."""
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    paths.config_path.write_text(json.dumps({"schema": 1, "mascot_visible": False}), encoding="utf-8")
    (paths.settings_dir / "preferences.json").write_text(
        json.dumps({"schema": 1, "mascot_visible": True, "update_auto_check": True}), encoding="utf-8",
    )

    service = PluginService(paths, logging.getLogger("test-preferences-seed"))
    service.initialize()

    assert service.preferences.load().mascot_visible is True
    assert "mascot_visible" not in json.loads(paths.config_path.read_text(encoding="utf-8"))


def test_deleting_managed_data_holds_the_updater_down_for_the_whole_transaction(tmp_path: Path):
    """The tree an update stages into and records into is the tree being cleared.

    Neither starting an update nor the update scheduler's own tick passes
    through the mutation boundary the deletion takes, so a single refusal check
    could not hold: a check finishing afterwards writes the update record back
    into a state directory that has just been emptied, and an update started
    after the authoritative re-check stages an archive into the temporary root
    being removed.
    """
    service, paths = _managed_data_service(tmp_path)
    drained: list[bool] = []
    original = service.plugin_updates.drain_check

    async def watched() -> None:
        drained.append(service.plugin_updates._suspended)
        await original()

    service.plugin_updates.drain_check = watched
    asyncio.run(service.delete_managed_data("setup"))

    # Held down before anything was removed, and let go afterwards.
    assert drained == [True], "the check already out is waited for, under the refusal"
    assert service.plugin_updates._suspended is False
    # And nothing wrote the update record back into the cleared directory.
    assert (paths.state_root / "plugin-update.json").exists() is False


def test_removal_readiness_names_the_work_the_deletion_itself_would_refuse(tmp_path: Path):
    """One question, one answer.

    Advanced labels the device safe to remove from out of this report, and the
    call it then makes refuses work in progress that this report never
    mentioned - so the screen said one thing and the press said another.
    """
    # A plain service, because the managed-data fixture deliberately writes a
    # corrupt profile store and that is a blocker of its own.
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    assert service.get_removal_readiness()["can_delete_managed_data"] is True

    service.plugin_updates._operation = {
        "operation_id": "a" * 32, "state": "downloading", "version": "0.9.28",
        "message": "Downloading", "error": None,
    }
    readiness = service.get_removal_readiness()
    assert readiness["can_delete_managed_data"] is False
    assert any("plugin update" in blocker for blocker in readiness["blockers"])
    # And the deletion refuses for the same reason, in the same words.
    with pytest.raises(ValueError, match="plugin update"):
        asyncio.run(service.delete_managed_data("cache"))


def test_a_plugin_update_and_a_cheat_engine_setup_refuse_each_other(tmp_path: Path):
    """Two transactions that each replace what the other is writing into.

    Each manager has a moment between deciding to start and having an operation
    to show for it, so two checks that each ask the other manager can both pass
    inside it. One reservation, taken under the mutation boundary, is what makes
    the answer one answer.
    """
    service, _paths = _managed_data_service(tmp_path)

    # An update holding the reservation refuses a setup, including in the gap
    # before the update manager has anything to show.
    service._plugin_update_reservation = "starting"
    with pytest.raises(ValueError, match="plugin update"):
        asyncio.run(service.start_managed_ce_install())
    service._plugin_update_reservation = None

    # And the other way round, in the same gap.
    service._managed_ce_reservation = "starting"
    with pytest.raises(ValueError, match="Cheat Engine setup"):
        asyncio.run(service.start_plugin_update("0.9.28"))
    service._managed_ce_reservation = None

    # A settled update gives the reservation back.
    service._plugin_update_reservation = "abc"
    service._reconcile_plugin_update_reservation({"operation_id": "abc", "state": "failed"})
    assert service._plugin_update_reservation is None


def test_deleting_managed_data_refuses_an_update_that_has_only_been_reserved(tmp_path: Path):
    service, _paths = _managed_data_service(tmp_path)
    service._plugin_update_reservation = "starting"
    readiness = service.get_removal_readiness()
    assert readiness["can_delete_managed_data"] is False
    assert any("plugin update" in blocker for blocker in readiness["blockers"])
    with pytest.raises(ValueError, match="plugin update"):
        asyncio.run(service.delete_managed_data("cache"))


def test_deleting_everything_takes_the_recovery_archive_with_it(tmp_path: Path):
    """The one file this plugin writes outside the directories it sweeps."""
    service, paths = _managed_data_service(tmp_path)
    kept = service.plugin_updates.kept_archive_path
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(b"a verified release nobody installed")

    asyncio.run(service.delete_managed_data("setup"))
    assert kept.exists() is True, "a narrower scope leaves the user's own file alone"

    asyncio.run(service.delete_managed_data("all"))
    assert kept.exists() is False


def test_reading_the_status_changes_nothing_about_an_update(tmp_path: Path):
    """`get_status()` writes nothing, and the updater is part of that promise.

    The panel polls this call and so do read-only helpers, whose whole value is
    that observing the device does not change it. The updater's reconciling -
    folding in a runner's result, writing down when a start time in this clock's
    future was first seen, removing a recovery archive this plugin has outgrown -
    happens at the boundaries that may write, not here.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    updates = service.plugin_updates

    body = b"a verified release this device already runs"
    updates.kept_archive_path.parent.mkdir(parents=True, exist_ok=True)
    updates.kept_archive_path.write_bytes(body)
    updates.state.update(
        install={
            "attempt": "a" * 32, "operation_id": "o" * 32, "version": "9.9.9",
            "started_at": time.time() + 30 * 24 * 3600, "digest": "b" * 64,
        },
        recovery={
            "attempt": "c" * 32, "version": "0.0.1", "sha256": sha256(body).hexdigest(),
            "path": str(updates.kept_archive_path),
        },
    )
    updates.result_path.write_text(json.dumps({
        "schema": 1, "attempt": "a" * 32, "version": "9.9.9", "ok": False,
        "error": "Decky refused the install", "archive_kept_at": None,
        "finished_at": time.time(), "restart_requested": False,
    }), encoding="utf-8")

    before = {
        path: path.read_bytes()
        for path in (updates.state.path, updates.result_path, updates.kept_archive_path)
    }
    status = service.get_status()
    assert isinstance(status["update"], dict)
    for path, content in before.items():
        assert path.is_file(), f"{path.name} was removed by a status read"
        assert path.read_bytes() == content, f"{path.name} was rewritten by a status read"

    # And the boundary that may write does all of it.
    updates.maintain()
    assert updates.result_path.exists() is False
    assert updates.state.load()["last_result"]["ok"] is False
    assert updates.kept_archive_path.exists() is False


def test_deleting_saved_setup_keeps_the_recovery_archive_and_what_proves_it(tmp_path: Path):
    """The file is in the user's home and its proof is in the state directory.

    This scope deliberately keeps the first and clears the second, which left a
    large file in that home with nothing to say what version it is, what its
    digest is, or that it can be installed at all.
    """
    service, _paths = _managed_data_service(tmp_path)
    updates = service.plugin_updates
    body = b"a verified release nobody installed"
    updates.kept_archive_path.parent.mkdir(parents=True, exist_ok=True)
    updates.kept_archive_path.write_bytes(body)
    recovery = {
        "attempt": "a" * 32, "version": "9.9.9", "sha256": sha256(body).hexdigest(),
        "path": str(updates.kept_archive_path),
    }
    updates.state.update(recovery=recovery)

    asyncio.run(service.delete_managed_data("setup"))
    assert updates.kept_archive_path.is_file()
    assert updates.state.load()["recovery"] == recovery
    assert service.get_status()["update"]["recovery"]["version"] == "9.9.9"

    asyncio.run(service.delete_managed_data("all"))
    assert updates.kept_archive_path.exists() is False
    assert updates.state.load().get("recovery") is None


def test_a_refused_deletion_says_so_before_it_says_why(tmp_path: Path):
    """The panel reconciles after a deletion it cannot confirm, and not after one
    that never happened, so the refusal has to be recognisable as one.

    `src/managedDeletion.ts` matches this prefix; the two halves are here so a
    change to either is a failing test rather than a panel that quietly forgets
    the user's own choices for a deletion the backend refused.
    """
    from ce_decky.service import DELETION_REFUSED_PREFIX

    service, _paths = _managed_data_service(tmp_path)
    service._plugin_update_reservation = "starting"
    with pytest.raises(ValueError) as refused:
        asyncio.run(service.delete_managed_data("all"))
    assert str(refused.value).startswith(DELETION_REFUSED_PREFIX)
    assert "plugin update" in str(refused.value)
    assert DELETION_REFUSED_PREFIX == "nothing was deleted; "


def test_deleting_the_state_forgets_what_could_not_be_written_to_it(tmp_path: Path):
    """Storage that will not take writes is why somebody reaches for this.

    The updater keeps what it could not write in memory on purpose, so that a
    device in that state still knows what it learned. Once the file is gone this
    backend would be the only thing left asserting any of it, and a first run
    would differ before and after a plugin reload.
    """
    service, paths = _managed_data_service(tmp_path)
    updates = service.plugin_updates
    updates._unwritten.update({
        "latest_version": "9.9.9", "checked_at": time.time(), "attempted_at": time.time(),
        "last_result": {"attempt": "a" * 32, "version": "9.9.9", "ok": False, "error": "x",
                        "archive_kept_at": None, "at": time.time(), "restart_requested": False},
    })
    assert service.get_status()["update"]["latest_version"] == "9.9.9"

    asyncio.run(service.delete_managed_data("all"))

    update = service.get_status()["update"]
    assert update["latest_version"] is None
    assert update["last_result"] is None
    assert update["checked_at"] is None
    assert updates.should_check() is False or updates._stored().get("attempted_at") is None
    # And nothing put the file back into the directory that was just emptied.
    assert (paths.state_root / "plugin-update.json").exists() is False


def test_deleting_saved_setup_forgets_the_same_memory_and_keeps_the_recovery(tmp_path: Path):
    service, _paths = _managed_data_service(tmp_path)
    updates = service.plugin_updates
    body = b"a verified release nobody installed"
    updates.kept_archive_path.parent.mkdir(parents=True, exist_ok=True)
    updates.kept_archive_path.write_bytes(body)
    recovery = {
        "attempt": "a" * 32, "version": "9.9.9", "sha256": sha256(body).hexdigest(),
        "path": str(updates.kept_archive_path),
    }
    updates.state.update(recovery=recovery)
    updates._unwritten["latest_version"] = "9.9.9"

    asyncio.run(service.delete_managed_data("setup"))

    assert service.get_status()["update"]["latest_version"] is None, "the volatile half is forgotten"
    assert updates.kept_archive_path.is_file(), "and the file this scope keeps is still there"
    assert service.get_status()["update"]["recovery"]["version"] == "9.9.9"


def test_reading_removal_readiness_changes_nothing_about_an_update(tmp_path: Path):
    """Check says "nothing is deleted by looking", and the panel reader is
    allowed to press it for exactly that reason.

    It reached the updater's own reconciling, which folds in a runner's result
    and deletes it, writes a normalised start time, and removes a recovery
    archive this plugin has outgrown. Asking what a deletion would refuse is not
    a deletion.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    updates = service.plugin_updates

    body = b"a verified release this device already runs"
    updates.kept_archive_path.parent.mkdir(parents=True, exist_ok=True)
    updates.kept_archive_path.write_bytes(body)
    updates.state.update(
        install={
            "attempt": "a" * 32, "operation_id": "o" * 32, "version": "9.9.9",
            "started_at": time.time() + 30 * 24 * 3600, "digest": "b" * 64,
        },
        recovery={
            "attempt": "c" * 32, "version": "0.0.1", "sha256": sha256(body).hexdigest(),
            "path": str(updates.kept_archive_path),
        },
    )
    updates.result_path.write_text(json.dumps({
        "schema": 1, "attempt": "a" * 32, "version": "9.9.9", "ok": False,
        "error": "Decky refused the install", "archive_kept_at": None,
        "finished_at": time.time(), "restart_requested": False,
    }), encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in (updates.state.path, updates.result_path, updates.kept_archive_path)
    }

    readiness = service.get_removal_readiness()
    assert isinstance(readiness["blockers"], list)
    for path, content in before.items():
        assert path.is_file(), f"{path.name} was removed by reading readiness"
        assert path.read_bytes() == content, f"{path.name} was rewritten by reading readiness"

    # And the deletion itself, which is a mutation boundary, does settle it.
    asyncio.run(service.delete_managed_data("cache"))
    assert updates.result_path.exists() is False
    assert updates.state.load()["last_result"]["ok"] is False


def test_readiness_still_reports_an_installer_that_is_out_there(tmp_path: Path):
    """Looking without touching must not turn into looking without seeing."""
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("test"))
    service.initialize()
    service.plugin_updates.state.update(install={
        "attempt": "a" * 32, "operation_id": "o" * 32, "version": "9.9.9",
        "started_at": time.time(), "digest": "b" * 64,
    })
    readiness = service.get_removal_readiness()
    assert readiness["can_delete_managed_data"] is False
    assert any("plugin update" in blocker for blocker in readiness["blockers"])


SIGNED_FIXTURE = (
    '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
    '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Health"</Description>'
    '<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry></CheatEntries>\n'
    '  <LuaScript>print("hello")</LuaScript>\n'
    '  <Signature><SignedHash>' + "h" * 165 + '</SignedHash><PublicKey>' + "k" * 128 + '</PublicKey></Signature>\n'
    '</CheatTable>\n'
)


def test_a_signed_table_can_be_stored_again_without_its_signature(tmp_path: Path):
    """The one place CE Decky makes executable content rather than carrying it.

    What comes out is an ordinary table: its own digest, its own inspection and
    its own consent still to give. What it deliberately does not get is an
    origin, because no provider served these bytes and none of them vouched for
    what CE Decky produced.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("derive"))
    service.initialize()
    source = tmp_path / "signed.CT"
    source.write_text(SIGNED_FIXTURE, encoding="utf-8")
    original = service.import_table(str(source))

    derived = service.prepare_table_copy(str(original["sha256"]))

    assert derived["sha256"] != original["sha256"]
    assert derived["derived_from"] == {
        "sha256": original["sha256"], "transforms": ["remove-signature"], "scans": [], "orphaned": [],
    }
    # Nothing a provider said about the source follows the bytes CE Decky made.
    assert list(derived["origins"]) == []
    assert derived["filename"].endswith("(unsigned).CT")
    # The same table: every cheat is still there, and the signature is not.
    inspection = service.inspect_table_sha(str(derived["sha256"]))
    assert inspection["has_signature"] is False
    assert inspection["total_entries"] == original["entry_count"]
    # The source is untouched and still listed as what it was.
    assert service.inspect_table_sha(str(original["sha256"]))["has_signature"] is True


def test_deriving_from_a_table_with_no_signature_is_refused(tmp_path: Path):
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("derive-plain"))
    service.initialize()
    source = tmp_path / "plain.CT"
    source.write_text(SIGNED_FIXTURE.replace("Signature>", "NotASignature>"), encoding="utf-8")
    table = service.import_table(str(source))

    with pytest.raises(ValueError, match="carries no signature"):
        service.prepare_table_copy(str(table["sha256"]))


def test_deriving_the_same_table_twice_stores_it_once(tmp_path: Path):
    """The transform is deterministic, so the second derivation is the first."""
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("derive-twice"))
    service.initialize()
    source = tmp_path / "signed.CT"
    source.write_text(SIGNED_FIXTURE, encoding="utf-8")
    original = service.import_table(str(source))

    first = service.prepare_table_copy(str(original["sha256"]))
    second = service.prepare_table_copy(str(original["sha256"]))
    assert first["sha256"] == second["sha256"]
    assert second["derived_from"] == first["derived_from"]
    stored = [table for table in service.get_status()["tables"] if table["sha256"] == first["sha256"]]
    assert len(stored) == 1


def test_a_stored_signed_table_carries_the_fact_to_the_row(tmp_path: Path):
    """The mark is on a listed row, and a row is listed from metadata.

    Review parses the table and can read the signature itself. Search and
    Manage never parse anything: they list what the store recorded, so the fact
    has to be in the record or the chip can never be drawn.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("signed-status"))
    service.initialize()
    signed_source = tmp_path / "signed.CT"
    signed_source.write_text(SIGNED_FIXTURE, encoding="utf-8")
    plain_source = tmp_path / "plain.CT"
    plain_source.write_text(SIGNED_FIXTURE.replace("Signature>", "NotASignature>"), encoding="utf-8")

    signed = service.import_table(str(signed_source))
    plain = service.import_table(str(plain_source))

    assert signed["has_signature"] is True
    assert plain["has_signature"] is False
    listed = {table["sha256"]: table for table in service.get_status()["tables"]}
    assert listed[signed["sha256"]]["has_signature"] is True
    assert listed[plain["sha256"]]["has_signature"] is False
    # The copy CE Decky makes is not signed, and the row says which table it
    # was made from so the two can be told apart in one list.
    derived = service.prepare_table_copy(str(signed["sha256"]))
    assert derived["has_signature"] is False
    assert derived["derived_from"] == {
        "sha256": signed["sha256"], "transforms": ["remove-signature"], "scans": [], "orphaned": [],
    }


def test_a_table_stored_before_the_signature_was_read_is_filled_in_at_startup(tmp_path: Path):
    """Every table already on a device was recorded without this field.

    Without the startup pass the mark would appear only on tables imported
    after the upgrade, which is exactly the set the user does not have yet. The
    answer is read from the bytes, which is the only place it has ever been.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("signed-backfill"))
    service.initialize()
    source = tmp_path / "signed.CT"
    source.write_text(SIGNED_FIXTURE, encoding="utf-8")
    table = service.import_table(str(source))
    digest = str(table["sha256"])

    # An older build's record: schema 4, and no answer about the signature.
    record = service.table_store.meta_root / f"{digest}.json"
    stored = json.loads(record.read_text(encoding="utf-8"))
    stored.pop("has_signature")
    stored["schema_version"] = 4
    record.write_text(json.dumps(stored), encoding="utf-8")
    # Read back before the pass: unknown, and a row says nothing rather than
    # calling a signed table unsigned.
    assert service.table_store.get_table(digest)["has_signature"] is None

    PluginService(paths, logging.getLogger("signed-backfill-restart")).initialize()

    assert service.table_store.get_table(digest)["has_signature"] is True


def test_filling_the_signature_in_keeps_what_the_reader_only_repairs(tmp_path: Path):
    """The pass adds one field; it does not rewrite the record in the reader's image.

    The metadata reader drops provenance it cannot parse rather than refusing
    the whole table, which is right for a read and would be data loss on a
    write: a pass that runs over every table on the device at startup would
    take those rows off the disk for good.
    """
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("signed-backfill-keeps"))
    service.initialize()
    source = tmp_path / "signed.CT"
    source.write_text(SIGNED_FIXTURE, encoding="utf-8")
    digest = str(service.import_table(str(source))["sha256"])

    record = service.table_store.meta_root / f"{digest}.json"
    stored = json.loads(record.read_text(encoding="utf-8"))
    stored.pop("has_signature")
    stored["schema_version"] = 4
    stored["origins"] = [{"provider": "fearless", "artifact_id": "not a full origin row"}]
    record.write_text(json.dumps(stored), encoding="utf-8")

    PluginService(paths, logging.getLogger("signed-backfill-keeps-restart")).initialize()

    written = json.loads(record.read_text(encoding="utf-8"))
    assert written["has_signature"] is True
    assert written["origins"] == stored["origins"]


SCAN_FIXTURE = (
    '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
    '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Health"</Description>'
    '<VariableType>Auto Assembler Script</VariableType>'
    '<AssemblerScript>[ENABLE]\n'
    'aobscanmodule(aobPresent,game.exe,48 8B 01 48 89 54 24)\n'
    'aobscanmodule(aobAbsent,game.exe,F3 0F 59 F0 48 8B C3)\n'
    '</AssemblerScript></CheatEntry></CheatEntries>\n'
    '</CheatTable>\n'
)


def _scan_program(tmp_path: Path, name: str = "game.exe") -> Path:
    """A file holding one of the fixture's two patterns and not the other."""
    path = tmp_path / name
    path.write_bytes(b"\x00" * 64 + bytes.fromhex("488B0148895424") + b"\x11" * 64)
    return path


def test_the_scan_check_names_the_pattern_this_copy_of_the_game_does_not_hold(tmp_path: Path, monkeypatch):
    """The finding a user reads before they consent, rather than after a launch.

    A script finds the game's code by scanning for a byte pattern. One absent
    and every cheat that script owns is dead at once, with nothing said, which
    is what a table written for an older build looks like from the outside.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-check"))
    service.initialize()
    source = tmp_path / "scanner.CT"
    source.write_text(SCAN_FIXTURE, encoding="utf-8")
    table = service.import_table(str(source))
    program = _scan_program(tmp_path)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    assert answer["missing"] == ["aobAbsent"]
    assert answer["present"] == ["aobPresent"]
    assert answer["not_checked"] == [] and answer["reason"] is None
    assert answer["source"] == "file"
    # And the count the sentence is built from reached the panel with it.
    assert service.inspect_table_sha(str(table["sha256"]))["scan_count"] == 2


MODULE_SCAN_FIXTURE = (
    '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
    '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Health"</Description>'
    '<VariableType>Auto Assembler Script</VariableType>'
    '<AssemblerScript>[ENABLE]\n'
    'aobscanmodule(aobPresent,game.exe,48 8B 01 48 89 54 24)\n'
    'aobscanmodule(aobInEngine,engine.dll,C3 90 41 57 48 83 EC)\n'
    'aobscanmodule(aobElsewhere,other.dll,F3 0F 59 F0 48 8B C3)\n'
    '</AssemblerScript></CheatEntry></CheatEntries>\n'
    '</CheatTable>\n'
)


def test_the_check_reads_the_game_s_other_files_for_the_patterns_that_name_them(tmp_path: Path, monkeypatch):
    """A game ships its code in more than one file.

    A script scanning the engine's own library is making a claim about that
    library, so reporting the pattern absent from the program would be inventing
    a finding - and saying nothing has read half the table and shown the reader
    the healthy half. The file is beside the program, so it is looked for there
    and searched in turn.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-modules"))
    service.initialize()
    source = tmp_path / "modules.CT"
    source.write_text(MODULE_SCAN_FIXTURE, encoding="utf-8")
    table = service.import_table(str(source))
    game = tmp_path / "game"
    game.mkdir()
    program = game / "game.exe"
    program.write_bytes(b"\x00" * 64 + bytes.fromhex("488B0148895424") + b"\x11" * 64)
    (game / "engine.dll").write_bytes(b"\x22" * 32 + bytes.fromhex("C39041574883EC") + b"\x33" * 32)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    # The program's own pattern, and the engine's, each answered from the file
    # that was supposed to hold it.
    assert answer["present"] == ["aobPresent", "aobInEngine"]
    assert answer["missing"] == []
    # And the one whose file this device does not have stays exactly as
    # unchecked as it was, with the reason it already carried.
    assert [row["name"] for row in answer["not_checked"]] == ["aobElsewhere"]
    assert "other.dll" in answer["not_checked"][0]["reason"]


def test_a_pattern_absent_from_the_file_that_names_it_is_missing_rather_than_unchecked(tmp_path: Path, monkeypatch):
    """The file was read, so the answer is about the file rather than about not looking."""
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-module-miss"))
    service.initialize()
    source = tmp_path / "modules.CT"
    source.write_text(MODULE_SCAN_FIXTURE, encoding="utf-8")
    table = service.import_table(str(source))
    game = tmp_path / "game"
    game.mkdir()
    program = game / "game.exe"
    program.write_bytes(b"\x00" * 64 + bytes.fromhex("488B0148895424") + b"\x11" * 64)
    (game / "engine.dll").write_bytes(b"\x44" * 128)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    assert answer["missing"] == ["aobInEngine"]
    assert answer["present"] == ["aobPresent"]


def test_a_game_whose_program_this_device_does_not_know_is_not_checked(tmp_path: Path):
    """Guessing a path here is the invariant this project is most careful about.

    A table reviewed for a game this device has never launched gets the Review
    it always got: the answer says the check did not run, and nothing on screen
    claims the table is broken.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-unknown"))
    service.initialize()
    source = tmp_path / "scanner.CT"
    source.write_text(SCAN_FIXTURE, encoding="utf-8")
    table = service.import_table(str(source))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    assert answer["reason"] is not None
    assert answer["missing"] == [] and answer["present"] == []


SIGNED_SCAN_FIXTURE = (
    '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
    '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Health"</Description>'
    '<VariableType>Auto Assembler Script</VariableType>'
    '<AssemblerScript>[ENABLE]\n'
    'aobscanmodule(aobPresent,game.exe,48 8B 01 48 89 54 24)\n'
    'aobscanmodule(aobAbsent,game.exe,F3 0F 59 F0 48 8B C3)\n'
    '</AssemblerScript></CheatEntry>'
    '<CheatEntry><ID>2</ID><Description>"Speed"</Description>'
    '<VariableType>4 Bytes</VariableType><Address>aobAbsent+8</Address></CheatEntry>'
    '</CheatEntries>\n'
    '  <Signature><SignedHash>' + "h" * 165 + '</SignedHash><PublicKey>' + "k" * 128 + '</PublicKey></Signature>\n'
    '</CheatTable>\n'
)


def test_a_signed_table_missing_a_pattern_is_repaired_in_one_copy(tmp_path: Path, monkeypatch):
    """One press, one derived table, both transforms recorded on it.

    A user who met the signature and the missing pattern one press at a time
    would make two copies, give consent three times and be left with two
    near-identical tables to tell apart. So whatever is wrong goes in one step,
    and the copy says what was done to it and what that cost.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("prepare-both"))
    service.initialize()
    source = tmp_path / "signed-scanner.CT"
    source.write_text(SIGNED_SCAN_FIXTURE, encoding="utf-8")
    original = service.import_table(str(source))
    program = _scan_program(tmp_path)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    derived = service.prepare_table_copy(str(original["sha256"]), 4242)

    assert derived["sha256"] != original["sha256"]
    assert derived["derived_from"] == {
        "sha256": original["sha256"],
        "transforms": ["remove-signature", "drop-unmatched-scans"],
        "scans": ["aobAbsent"],
        # The cost, named in the words the table is read in: that cheat is
        # reached through the hook that went, so it is gone from this copy.
        "orphaned": ["Speed"],
    }
    assert derived["has_signature"] is False
    # And the copy is one that works here: the pattern the program does hold is
    # still scanned for, and the one it does not is not.
    answer = service.check_table_scans(str(derived["sha256"]), 4242)
    assert answer["missing"] == [] and answer["present"] == ["aobPresent"]
    # The source is untouched.
    assert service.inspect_table_sha(str(original["sha256"]))["has_signature"] is True


def test_a_table_with_nothing_wrong_is_refused_without_claiming_it_was_checked(tmp_path: Path):
    """A refusal may not answer a question nobody asked.

    Where this device does not know which program the game runs, no pattern was
    looked for, and saying every pattern is present would be the one claim this
    has no evidence for.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("prepare-nothing"))
    service.initialize()
    source = tmp_path / "plain.CT"
    source.write_text(SIGNED_FIXTURE.replace("Signature>", "NotASignature>"), encoding="utf-8")
    table = service.import_table(str(source))

    with pytest.raises(ValueError, match="could not check its patterns"):
        service.prepare_table_copy(str(table["sha256"]), 4242)


def test_a_repair_that_cannot_be_proved_produces_nothing_and_says_which_proof(tmp_path: Path, monkeypatch):
    """Every step after the edit is a proof rather than a hope.

    A script whose surviving code still names what the removal took away is one
    Cheat Engine refuses to compile, which is the same outcome as the missing
    pattern this was meant to fix. Nothing is stored, and the refusal names the
    reference that is still there rather than saying a repair was refused.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("prepare-refused"))
    service.initialize()
    source = tmp_path / "entangled.CT"
    source.write_text(
        SIGNED_SCAN_FIXTURE.replace(
            'aobscanmodule(aobAbsent,game.exe,F3 0F 59 F0 48 8B C3)\n',
            'aobscanmodule(aobAbsent,game.exe,F3 0F 59 F0 48 8B C3)\n'
            'registersymbol(aobPresent)\n'
            'readmem(aobAbsent,8)\n',
        ),
        encoding="utf-8",
    )
    table = service.import_table(str(source))
    program = _scan_program(tmp_path)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    before = {row["sha256"] for row in service.get_status()["tables"]}
    with pytest.raises(ValueError, match="still names aobAbsent"):
        service.prepare_table_copy(str(table["sha256"]), 4242)
    assert {row["sha256"] for row in service.get_status()["tables"]} == before
    # And the screen that would offer the press was told the same thing first.
    assert service.check_table_scans(str(table["sha256"]), 4242)["repairable"] is False


def test_the_scan_check_says_whether_the_repair_it_would_offer_exists(tmp_path: Path, monkeypatch):
    """A press is offered for a repair that was made and proven, never for a gap."""
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-repairable"))
    service.initialize()
    source = tmp_path / "scanner.CT"
    source.write_text(SCAN_FIXTURE, encoding="utf-8")
    table = service.import_table(str(source))
    program = _scan_program(tmp_path)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    assert answer["missing"] == ["aobAbsent"] and answer["repairable"] is True
    # Three states, not two: nothing missing is nobody asked, and a screen may
    # not read that as a repair that was refused.
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (None, None))
    assert service.check_table_scans(str(table["sha256"]), 4242)["repairable"] is None


def test_a_table_that_scans_for_nothing_says_so_rather_than_nothing(tmp_path: Path, monkeypatch):
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-none"))
    service.initialize()
    source = tmp_path / "plain.CT"
    source.write_text(SIGNED_FIXTURE.replace("Signature>", "NotASignature>"), encoding="utf-8")
    table = service.import_table(str(source))
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (_scan_program(tmp_path), "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    assert answer["reason"] is not None and answer["missing"] == []
    assert service.inspect_table_sha(str(table["sha256"]))["scan_count"] == 0


def test_the_program_is_resolved_from_what_this_device_already_established(tmp_path: Path, monkeypatch):
    """Three answers, strongest first, and no fourth.

    The running game outranks a record of one, which outranks what Steam says
    it starts. Each is something this device established; a path assembled from
    a convention is the one thing that may never happen here.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-resolve"))
    service.initialize()
    program = _scan_program(tmp_path)
    service.save_profile(4242, "A game", False, None, "game.exe")

    # Nothing knows where it is: no answer, and nothing is invented.
    monkeypatch.setattr(service_module, "observe_game_executable_path", lambda app_id, process: None)
    monkeypatch.setattr(service, "_recorded_game_executable_path", lambda app_id, process: None)
    monkeypatch.setattr(service, "_installed_game_program_path", lambda app_id, process: None)
    assert service._game_program_path(4242) == (None, None)

    # What Steam says it starts, for a game that has never run here. This is
    # the ordinary case for a table chosen the day it is downloaded.
    monkeypatch.setattr(service, "_installed_game_program_path", lambda app_id, process: str(program))
    assert service._game_program_path(4242) == (program, "installed")

    # A record of the last time a cheat for it worked outranks that.
    monkeypatch.setattr(service, "_recorded_game_executable_path", lambda app_id, process: str(program))
    assert service._game_program_path(4242)[1] == "recorded"

    # And the game as it is running outranks all of them, because it is the
    # only one that is about this moment rather than a record of an earlier one.
    monkeypatch.setattr(service_module, "observe_game_executable_path", lambda app_id, process: str(program))
    assert service._game_program_path(4242)[1] == "running"


def test_a_shortcut_is_answered_from_the_game_directory_steam_recorded(tmp_path: Path, monkeypatch):
    """A non-Steam shortcut has no manifest and no install folder.

    What Steam has for one is the command it starts, and that is commonly a
    launcher rather than the program a table's patterns are in: the game this
    project measures against names one at the top of its folder and keeps the
    program three directories down. So the command is read for its directory
    and the program is looked for under it by the exact name the screen is
    about to propose.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-shortcut"))
    service.initialize()
    game = tmp_path / "A Game"
    deep = game / "Binaries" / "Win64"
    deep.mkdir(parents=True)
    launcher = game / "launcher.exe"
    launcher.write_bytes(b"\x90" * 32)
    program = deep / "game-Win64-Shipping.exe"
    program.write_bytes(b"\x90" * 32)
    service.save_profile(4242, "A game", True, None, "game-Win64-Shipping.exe")
    monkeypatch.setattr(service_module, "observe_game_executable_path", lambda app_id, process: None)
    monkeypatch.setattr(service, "_recorded_game_executable_path", lambda app_id, process: None)
    monkeypatch.setattr(service_module, "shortcut_program", lambda user_home, app_id: str(launcher))

    assert service._game_program_path(4242) == (program, "shortcut")
    # And a name that is not under that directory is no answer rather than
    # somebody else's file.
    assert service._game_program_path(4242, "elsewhere.exe") == (None, None)


def test_the_program_asked_about_is_the_one_review_proposes(tmp_path: Path, monkeypatch):
    """A table's first Review has no saved process, and the game may be running.

    The profile stores a target process only once the user has pressed **Use
    this table**, so a game's first table had nothing to resolve against and
    was never checked - including while the game was running in front of the
    reader. What Review is about to propose is what the answer has to be about.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-proposed"))
    service.initialize()
    program = _scan_program(tmp_path)
    service.save_profile(4242, "A game", False, None, None)
    asked: list[str] = []

    def running(app_id, process):
        asked.append(process)
        return str(program) if process == "proposed.exe" else None

    monkeypatch.setattr(service_module, "observe_game_executable_path", running)
    monkeypatch.setattr(service, "_recorded_game_executable_path", lambda app_id, process: None)
    monkeypatch.setattr(service, "_installed_game_program_path", lambda app_id, process: None)

    # With no proposal and no saved process there is nothing to ask about.
    assert service._game_program_path(4242) == (None, None)
    assert asked == []
    # With one, that is the program the answer is about.
    assert service._game_program_path(4242, "proposed.exe") == (program, "running")
    assert asked == ["proposed.exe"]
    # And a process name that is not one is refused rather than resolved.
    assert service._game_program_path(4242, "../../etc/passwd") == (None, None)


def test_a_quiesce_waits_for_the_generation_it_issued(tmp_path: Path):
    """The bridge answers under the generation the command was written at.

    `write_commands` returns the next free generation rather than the one it
    wrote, so a stop that took the answer's identity from it waited for a
    result nothing publishes: every quiesce spent the whole bound and the
    record of which cheats were left on was discarded with it.
    """
    from ce_decky.session_protocol import RuntimeCommand, RuntimeResult

    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("quiesce-generation"))
    service.initialize()
    prepared = Mock(session_id="6d6f9d2a-0000-4000-8000-000000000001")
    issued: list[RuntimeCommand] = []

    class Store:
        def load_current(self, app_id):
            return prepared

        def read_status(self, session):
            return Mock(attached=True, results=(
                RuntimeResult(generation=7, record_id=None, ok=False, active=None,
                              value="put_down=2;unsettled=9", error="records did not settle",
                              error_code="quiesce_unsettled"),
            ))

        def next_generation(self, session):
            return 7

        def write_commands(self, session, commands):
            issued.extend(commands)
            # What the real store returns: the generation after the one written.
            return commands[-1].generation + 1

    service.session_store = Store()
    started = time.monotonic()
    answer = service._quiesce_session(4242)
    assert time.monotonic() - started < service_module.QUIESCE_WAIT_SECONDS / 2
    assert [command.generation for command in issued] == [7]
    assert answer["asked"] is True
    assert answer["records_put_down"] == 2
    assert answer["records_unsettled"] == ["9"]
    assert answer["reason"] == "records did not settle"


def test_a_pattern_the_whole_game_is_searched_for_is_reported_and_left_alone(tmp_path: Path, monkeypatch):
    """The screen says what was searched; the repair only removes what it can prove.

    A script that asks Cheat Engine to search the running process is looking in
    every library the game has loaded, and this device read one file. So the
    pattern is named to the reader and the hook that needs it stays in the
    table: cutting it out would make the copy less of a table than the one it
    was made from, on a question nobody answered.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-scope"))
    service.initialize()
    source = tmp_path / "process-wide.CT"
    source.write_text(
        SCAN_FIXTURE.replace(
            "aobscanmodule(aobAbsent,game.exe,F3 0F 59 F0 48 8B C3)",
            "aobscan(aobAbsent,F3 0F 59 F0 48 8B C3)",
        ),
        encoding="utf-8",
    )
    table = service.import_table(str(source))
    program = _scan_program(tmp_path)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)
    assert answer["missing"] == ["aobAbsent"]
    # Nothing to take out, so no press is offered for one.
    assert answer["repairable"] is False

    before = {row["sha256"] for row in service.get_status()["tables"]}
    with pytest.raises(ValueError, match="more of the game than this device can read"):
        service.prepare_table_copy(str(table["sha256"]), 4242)
    assert {row["sha256"] for row in service.get_status()["tables"]} == before


def test_a_quiesce_nobody_answered_is_not_an_empty_list_of_cheats(tmp_path: Path, monkeypatch):
    """Not knowing and nothing left on are different answers to the stop.

    The stop proceeds without the bridge's answer, so whatever was still on
    stays on in a game that has just lost the Cheat Engine which could have
    switched it off. Reporting that as an empty list read on screen as a clean
    stop, and the user was told nothing at all.
    """
    from ce_decky.session_protocol import RuntimeCommand

    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("quiesce-unanswered"))
    service.initialize()
    monkeypatch.setattr(service_module, "QUIESCE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(service_module, "QUIESCE_POLL_SECONDS", 0.05)
    prepared = Mock(session_id="6d6f9d2a-0000-4000-8000-000000000002")

    class Store:
        def load_current(self, app_id):
            return prepared

        def read_status(self, session):
            # Attached, alive, and saying nothing about this command.
            return Mock(attached=True, results=())

        def next_generation(self, session):
            return 3

        def write_commands(self, session, commands):
            return commands[-1].generation + 1

    service.session_store = Store()
    answer = service._quiesce_session(4242)

    assert answer["asked"] is True
    assert answer["answered"] is False
    assert answer["records_unsettled"] == []
    assert answer["reason"] == "the bridge did not answer before the stop had to proceed"
    assert RuntimeCommand  # the command type is what the store above was handed


def test_a_pattern_the_game_holds_two_files_for_is_not_proved_absent(tmp_path: Path, monkeypatch):
    """Which of them the game loads is not something a file listing can say.

    A game can ship two files under one module name - one per architecture, one
    per plugin directory - and reading whichever the walk reached first would
    put a finding on screen about a file the game may never load, and let the
    repair cut the hook that needs it.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-two-modules"))
    service.initialize()
    source = tmp_path / "engine-scanner.CT"
    source.write_text(
        SCAN_FIXTURE.replace(
            "aobscanmodule(aobAbsent,game.exe,F3 0F 59 F0 48 8B C3)",
            "aobscanmodule(aobAbsent,engine.dll,F3 0F 59 F0 48 8B C3)",
        ),
        encoding="utf-8",
    )
    table = service.import_table(str(source))
    game = tmp_path / "game"
    (game / "win64").mkdir(parents=True)
    (game / "plugins").mkdir(parents=True)
    program = _scan_program(game)
    # Two files of that name, neither of them holding the pattern.
    (game / "win64" / "engine.dll").write_bytes(b"\x00" * 128)
    (game / "plugins" / "engine.dll").write_bytes(b"\x00" * 128)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    assert answer["missing"] == [] and answer["proven_missing"] == []
    assert [row["name"] for row in answer["not_checked"]] == ["aobAbsent"]
    assert "more than one engine.dll" in answer["not_checked"][0]["reason"]
    assert answer["repairable"] is None
    # And the refusal does not answer for the pattern nobody looked for.
    with pytest.raises(ValueError, match="the rest could not be checked here"):
        service.prepare_table_copy(str(table["sha256"]), 4242)


def test_a_stop_says_when_the_game_was_not_established_to_be_put_back(tmp_path: Path, monkeypatch):
    """An answer is not the same as a game that was put back.

    The bridge can answer that it could not read the address list, or that it
    is not attached, and neither of those is a game with nothing left switched
    on. Only a walk that finished with nothing on, or a game that has exited
    and taken every patch with it, is a stop nobody has to be warned about.
    """
    from ce_decky.session_protocol import RuntimeResult

    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("quiesce-confirmed"))
    service.initialize()
    service.save_profile(4242, "A game", False, None, "game.exe")
    monkeypatch.setattr(service_module, "QUIESCE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(service_module, "QUIESCE_POLL_SECONDS", 0.05)
    prepared = Mock(session_id="6d6f9d2a-0000-4000-8000-000000000003")
    # The answer with three values, not a path that is absent for every reason
    # at once: a scan that ran out of budget, a process this cannot read and a
    # game that has exited are different things, and only the last is clean.
    target_state = "present"
    monkeypatch.setattr(service_module, "game_target_state", lambda app_id, process: target_state)

    def answering(value: str, ok: bool, code: str | None):
        class Store:
            def load_current(self, app_id):
                return prepared

            def read_status(self, session):
                return Mock(attached=True, results=(
                    RuntimeResult(generation=9, record_id=None, ok=ok, active=None,
                                  value=value, error=None if ok else "records did not settle", error_code=code),
                ))

            def next_generation(self, session):
                return 9

            def write_commands(self, session, commands):
                return commands[-1].generation + 1
        return Store()

    # The address list could not be read, so nothing was even looked at.
    service.session_store = answering("put_down=0;unsettled=", False, "address_list_unavailable")
    assert service._quiesce_session(4242)["cleanup_confirmed"] is False

    # A walk that finished with nothing left on is the clean case.
    service.session_store = answering("put_down=3;unsettled=", True, None)
    assert service._quiesce_session(4242)["cleanup_confirmed"] is True

    # A bridge that is not answering at all, with the game still running.
    class Silent:
        def load_current(self, app_id):
            return prepared

        def read_status(self, session):
            return None

    service.session_store = Silent()
    answer = service._quiesce_session(4242)
    assert answer["asked"] is False and answer["cleanup_confirmed"] is False

    # A scan that established nothing is not a game that exited, and it is the
    # answer this gets exactly when the device is under load or the process
    # cannot be read.
    target_state = "unknown"
    assert service._quiesce_session(4242)["cleanup_confirmed"] is False

    # The same silence once the game itself is proven gone: it took every patch
    # with it, so there is nothing to warn anybody about.
    target_state = "absent"
    assert service._quiesce_session(4242)["cleanup_confirmed"] is True


def test_a_symbol_two_scripts_mean_differently_is_not_answered_for(tmp_path: Path, monkeypatch):
    """Review may not call a symbol healthy, or broken, by document order.

    One table can carry a build for one graphics backend and a build for
    another, each scanning under the same symbol for its own pattern. The check
    reads the first of them, so what it found says nothing about the cheats the
    other owns.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-repeated"))
    service.initialize()
    source = tmp_path / "two-backends.CT"
    source.write_text(
        SCAN_FIXTURE.replace(
            '</AssemblerScript></CheatEntry></CheatEntries>',
            '</AssemblerScript></CheatEntry>'
            '<CheatEntry><ID>2</ID><Description>"Vulkan"</Description>'
            '<VariableType>Auto Assembler Script</VariableType>'
            '<AssemblerScript>[ENABLE]\n'
            'aobscanmodule(aobPresent,game.exe,F3 0F 59 F0 48 8B C3)\n'
            '</AssemblerScript></CheatEntry></CheatEntries>',
        ),
        encoding="utf-8",
    )
    table = service.import_table(str(source))
    program = _scan_program(tmp_path)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    # The first occurrence is in the program, and that is not an answer about
    # the second, so nothing is claimed either way.
    assert answer["present"] == [] and answer["missing"] == ["aobAbsent"]
    repeated = [row for row in answer["not_checked"] if row["name"] == "aobPresent"]
    assert repeated and "not the same pattern" in repeated[0]["reason"]


def test_a_pattern_missing_from_a_file_beside_the_program_is_reported_not_removed(tmp_path: Path, monkeypatch):
    """`aobscanmodule` searches the module the running process loaded.

    A file of that name in the game's own directory is very probably that
    module and is not established to be it: this device reads files and the
    process's own module list is not one of them. So the reader is told the
    pattern was not found there, and the repair leaves the hook alone.
    """
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("scan-sibling-module"))
    service.initialize()
    source = tmp_path / "engine-scanner.CT"
    source.write_text(
        SCAN_FIXTURE.replace(
            "aobscanmodule(aobAbsent,game.exe,F3 0F 59 F0 48 8B C3)",
            "aobscanmodule(aobAbsent,engine.dll,F3 0F 59 F0 48 8B C3)",
        ),
        encoding="utf-8",
    )
    table = service.import_table(str(source))
    game = tmp_path / "game"
    game.mkdir()
    program = _scan_program(game)
    # The only file of that name, and it does not hold the pattern.
    (game / "engine.dll").write_bytes(b"\x00" * 128)
    monkeypatch.setattr(service, "_game_program_path", lambda app_id, target=None: (program, "running"))

    answer = service.check_table_scans(str(table["sha256"]), 4242)

    # Said to the reader...
    assert answer["missing"] == ["aobAbsent"]
    # ...and not something a copy may be made by taking out.
    assert answer["proven_missing"] == []
    assert answer["repairable"] is False
