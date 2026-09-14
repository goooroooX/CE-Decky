"""The counters that say which of the backend's repeating paths was awake."""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import threading
import types

import pytest

from ce_decky import poll_counters


@pytest.fixture(autouse=True)
def _clean_counters():
    poll_counters.reset()
    yield
    poll_counters.reset()


def test_snapshot_names_every_path_before_anything_has_run():
    snapshot = poll_counters.snapshot()

    assert snapshot["schema"] == poll_counters.SCHEMA
    assert set(snapshot["paths"]) == set(poll_counters.PATHS)
    assert all(row["calls"] == 0 for row in snapshot["paths"].values())


def test_reading_the_counters_is_not_one_of_the_counts():
    with poll_counters.timed(poll_counters.RPC_RUNTIME_STATUS):
        pass
    first = poll_counters.snapshot()
    for _ in range(5):
        poll_counters.snapshot()
    second = poll_counters.snapshot()

    assert first["paths"] == second["paths"]
    assert second["paths"][poll_counters.RPC_RUNTIME_STATUS]["calls"] == 1


def test_a_path_that_raised_still_paid_for_its_pass():
    with pytest.raises(ValueError):
        with poll_counters.timed(poll_counters.SUPERVISOR_TARGET_SCAN):
            raise ValueError("scan failed")

    assert poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_TARGET_SCAN]["calls"] == 1


def test_an_unknown_path_is_refused_rather_than_silently_created():
    with pytest.raises(ValueError):
        poll_counters.record("rpc.invented", 0.0, 0.0)
    with pytest.raises(ValueError):
        with poll_counters.timed("rpc.invented"):
            pass

    assert set(poll_counters.snapshot()["paths"]) == set(poll_counters.PATHS)


def test_counts_from_several_threads_are_not_lost():
    def run() -> None:
        for _ in range(200):
            poll_counters.record(poll_counters.RPC_CE_LAUNCH_CAPABILITY, 0.001, 0.0005)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    row = poll_counters.snapshot()["paths"][poll_counters.RPC_CE_LAUNCH_CAPABILITY]
    assert row["calls"] == 800
    assert row["wall_seconds"] == pytest.approx(0.8, abs=1e-6)
    assert row["cpu_seconds"] == pytest.approx(0.4, abs=1e-6)


def test_the_supervisor_scan_counts_itself_and_the_table_walk_it_runs(tmp_path):
    from ce_decky import ce_launch

    # No process table at all, so the scan returns without following anything up
    # and what is counted is the pass rather than a particular answer.
    ce_launch.game_target_state(620, "portal2.exe", proc_root=tmp_path / "absent")

    paths = poll_counters.snapshot()["paths"]
    assert paths[poll_counters.SUPERVISOR_TARGET_SCAN]["calls"] == 1
    assert paths[poll_counters.PRIMITIVE_GAME_CONTAINER]["calls"] == 1


def test_a_target_state_with_no_process_name_never_reaches_the_table(tmp_path):
    from ce_decky import ce_launch

    assert ce_launch.game_target_state(620, "", proc_root=tmp_path) == "unknown"

    paths = poll_counters.snapshot()["paths"]
    assert paths[poll_counters.SUPERVISOR_TARGET_SCAN]["calls"] == 1
    assert paths[poll_counters.PRIMITIVE_GAME_CONTAINER]["calls"] == 0


def _plugin_class(monkeypatch):
    fake_decky = types.ModuleType("decky")
    fake_decky.logger = logging.getLogger("fake-decky-poll-counters")
    monkeypatch.setitem(sys.modules, "decky", fake_decky)
    sys.modules.pop("ce_decky.plugin", None)
    return importlib.import_module("ce_decky.plugin").Plugin


def test_the_panel_rpcs_are_counted_and_the_snapshot_rpc_is_not(monkeypatch):
    Plugin = _plugin_class(monkeypatch)

    class ServiceSpy:
        def get_runtime_status(self, app_id):
            return {"app_id": app_id}

        def get_ce_launch_capability(self, app_id=None):
            return {"app_id": app_id}

        def get_poll_counters(self):
            return poll_counters.snapshot()

    class OperationsSpy:
        closing = False

        async def run_blocking(self, function, *args):
            return function(*args)

    async def exercise() -> dict[str, object]:
        plugin = Plugin()
        plugin.operations = OperationsSpy()
        plugin.service = ServiceSpy()
        await plugin.get_runtime_status(620)
        await plugin.get_runtime_status(620)
        await plugin.get_ce_launch_capability(620)
        return await plugin.get_poll_counters()

    snapshot = asyncio.run(exercise())

    paths = snapshot["paths"]
    assert paths[poll_counters.RPC_RUNTIME_STATUS]["calls"] == 2
    assert paths[poll_counters.RPC_CE_LAUNCH_CAPABILITY]["calls"] == 1
    # Nothing the snapshot itself touched appears anywhere in it.
    assert paths[poll_counters.SUPERVISOR_TARGET_SCAN]["calls"] == 0
    assert poll_counters.snapshot()["paths"] == paths


def test_the_baseline_check_is_counted_once_per_pid_not_once_per_tick(tmp_path):
    """What it costs is a question about the number of PIDs, not the cadence.

    The supervision loop runs once a second and calls this once for every PID
    recorded at launch. A game under Proton leaves dozens, so a counter on the
    tick would have said one a second and explained nothing.
    """
    from ce_decky import ce_launch

    for pid in (4321, 4322, 4323):
        ce_launch.game_process_state(pid, 620, proc_root=tmp_path / "absent")

    paths = poll_counters.snapshot()["paths"]
    assert paths[poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"] == 3
    # It reads one process's own files and never walks the table.
    assert paths[poll_counters.PRIMITIVE_GAME_CONTAINER]["calls"] == 0


def test_a_refused_pid_still_counts_because_the_loop_still_asked(tmp_path):
    from ce_decky import ce_launch

    assert ce_launch.game_process_state(0, 620, proc_root=tmp_path) == "gone_or_reused"

    assert poll_counters.snapshot()["paths"][poll_counters.SUPERVISOR_BASELINE_CHECK]["calls"] == 1
