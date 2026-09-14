import asyncio
import importlib
import logging
import sys
import threading
import types
from pathlib import Path

import pytest

from ce_decky import __version__


def _plugin_module(monkeypatch):
    fake_decky = types.ModuleType("decky")
    fake_decky.logger = logging.getLogger("fake-decky")
    monkeypatch.setitem(sys.modules, "decky", fake_decky)
    sys.modules.pop("ce_decky.plugin", None)
    return importlib.import_module("ce_decky.plugin")


def test_each_entry_point_works_called_on_a_loop_of_its_own(monkeypatch, tmp_path: Path):
    """Load, read and unload, each on a loop that is closed before the next.

    Deliberately not the host's shape, and the name says so. Decky runs one
    event loop per plugin process and keeps it for the life of that process:
    `_main`, every RPC and `_unload` are tasks on that one loop, and
    `docs/FIELD_NOTES.md` carries the upstream source and the device reading
    that establish it. An earlier version of this test claimed to be the Decky
    contract, and a static review then read that claim and reported two defects
    that the real host does not have.

    What it is for is the weaker property it actually proves: no entry point
    depends on state the previous one left on a live loop, which is what lets a
    developer helper or a probe call one of them on its own.
    """
    module = _plugin_module(monkeypatch)

    env = {
        "DECKY_USER_HOME": str(tmp_path / "home"),
        "DECKY_PLUGIN_SETTINGS_DIR": str(tmp_path / "decky" / "settings"),
        "DECKY_PLUGIN_RUNTIME_DIR": str(tmp_path / "decky" / "runtime"),
        "DECKY_PLUGIN_LOG_DIR": str(tmp_path / "decky" / "logs"),
        "DECKY_PLUGIN_DIR": str(tmp_path / "plugin"),
        "DECKY_PLUGIN_LOG": str(tmp_path / "decky" / "logs" / "test.log"),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    plugin = module.Plugin()
    asyncio.run(plugin._main())
    status = asyncio.run(plugin.get_status())
    assert status["version"] == __version__
    assert status["features"]["launch_integration"] is True
    assert Path(status["managed_root"]).is_dir()
    asyncio.run(plugin._unload())
    asyncio.run(plugin._uninstall())
    assert plugin.operations.closing


def test_unload_drains_blocking_mutation_before_service_close(monkeypatch):
    module = _plugin_module(monkeypatch)
    plugin = module.Plugin()
    events: list[str] = []
    started = threading.Event()
    release = threading.Event()

    class Service:
        async def close(self):
            events.append("service-close")

    plugin.service = Service()  # type: ignore[assignment]

    def mutation():
        events.append("worker-start")
        started.set()
        assert release.wait(2)
        events.append("worker-end")

    async def scenario():
        worker = asyncio.create_task(plugin.operations.run_blocking(mutation))
        assert await asyncio.to_thread(started.wait, 1)
        unloading = asyncio.create_task(plugin._unload())
        await asyncio.sleep(0)
        assert "service-close" not in events
        release.set()
        await worker
        await unloading

    asyncio.run(scenario())
    assert events == ["worker-start", "worker-end", "service-close"]


def test_unload_closes_the_service_even_when_the_operation_drain_is_cancelled(monkeypatch):
    module = _plugin_module(monkeypatch)
    plugin = module.Plugin()
    events: list[str] = []

    class Service:
        async def close(self):
            events.append("service-close")

    plugin.service = Service()  # type: ignore[assignment]

    async def cancelled_close():
        events.append("operations-close")
        raise asyncio.CancelledError

    monkeypatch.setattr(plugin.operations, "close", cancelled_close)

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await plugin._unload()

    asyncio.run(scenario())
    # Closing the service is what retires owned Cheat Engine processes, staged
    # files and network sessions; a cancelled drain must not skip it.
    assert events == ["operations-close", "service-close"]


def test_unload_does_what_it_must_before_its_first_await(monkeypatch):
    """The first `await` of an unload may never come back on this host.

    `docs/FIELD_NOTES.md` records the mechanism: Decky's stop signals the plugin
    and closes its socket, both ends of that socket then read at EOF in a loop
    that never suspends, the event loop is starved and SIGKILL arrives five
    seconds later. Everything after the first suspension point is therefore work
    that may simply not happen, so what must happen is done before it.
    """
    module = _plugin_module(monkeypatch)
    plugin = module.Plugin()
    order: list[str] = []
    starved = asyncio.Event()

    class Service:
        def begin_close(self) -> None:
            order.append("begin_close")

        async def close(self) -> None:  # pragma: no cover - never reached here
            order.append("service_close")

    async def never_returns() -> None:
        order.append("operations_close")
        await starved.wait()

    plugin.service = Service()  # type: ignore[assignment]
    monkeypatch.setattr(plugin.operations, "close", never_returns)

    # Every suspension point of the unload, including the one the loop probes
    # use, and not only the drains. A diagnostic that yields is still a yield,
    # and putting one in front of the prologue would give a starved loop the
    # chance to take everything behind it.
    real_sleep = asyncio.sleep

    async def recorded_sleep(delay, *args, **kwargs):
        order.append("await")
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(module.asyncio, "sleep", recorded_sleep)

    async def scenario() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(plugin._unload(), 0.2)

    asyncio.run(scenario())

    # The prologue ran first, before anything suspended at all. What happens
    # after the suspension point is not asserted here: cancelling the wait is
    # only an approximation of a starved loop, and a real one would simply never
    # come back. The property is the order.
    assert order[0] == "begin_close"
    assert "await" in order and order.index("begin_close") < order.index("await")
    assert order.index("begin_close") < order.index("operations_close")
