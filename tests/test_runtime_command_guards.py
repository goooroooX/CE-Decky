from __future__ import annotations

import threading

import pytest

from ce_decky.guarded_service import GuardedPluginService, MAX_RUNTIME_RPC_COMMANDS
from ce_decky.service import PluginService


def _service(monkeypatch, *, results=()):
    service = GuardedPluginService.__new__(GuardedPluginService)
    service._mutation_lock = threading.RLock()
    service.get_runtime_status = lambda app_id: {"status": {"results": results}}
    monkeypatch.setattr(
        PluginService,
        "write_runtime_commands",
        lambda self, app_id, commands: {"ok": True, "count": len(commands), "next_generation": commands[-1]["generation"] + 1},
    )
    return service


def test_retry_attach_requires_explicit_target_basename():
    with pytest.raises(ValueError, match="explicit target"):
        GuardedPluginService._runtime_command({"generation": 1, "kind": "retry_attach"})
    command = GuardedPluginService._runtime_command(
        {"generation": 1, "kind": "retry_attach", "value": "Game.exe"}
    )
    assert command.value == "Game.exe"


def test_runtime_rpc_batch_is_bounded(monkeypatch):
    service = _service(monkeypatch)
    commands = [
        {"generation": generation, "kind": "list_processes"}
        for generation in range(1, MAX_RUNTIME_RPC_COMMANDS + 2)
    ]
    with pytest.raises(ValueError, match="batch exceeds"):
        service.write_runtime_commands(7, commands)


def test_runtime_rpc_requires_previous_batch_ack_before_next(monkeypatch):
    service = _service(monkeypatch)
    first = service.write_runtime_commands(7, [{"generation": 1, "kind": "list_processes"}])
    assert first["ok"] is True

    with pytest.raises(ValueError, match="exact bridge acknowledgement"):
        service.write_runtime_commands(7, [{"generation": 2, "kind": "list_processes"}])

    service.get_runtime_status = lambda app_id: {
        "status": {"results": ({"generation": 1, "ok": True},)}
    }
    second = service.write_runtime_commands(7, [{"generation": 2, "kind": "list_processes"}])
    assert second["next_generation"] == 3


def test_a_bridge_that_is_not_connected_says_which_of_the_four_it_is():
    """One sentence covered four states that need four different answers.

    A session that was never prepared, one whose Cheat Engine is proven gone,
    one whose state cannot be read and one that is simply busy running the
    table's own script all reported "not connected with a fresh heartbeat", and
    only the last of them is worth trying again.
    """
    from ce_decky.service import _disconnected_bridge_reason

    prepared = {"app_id": 10, "session_id": "s"}
    assert "no prepared session" in _disconnected_bridge_reason(
        {"prepared": None, "session_stale_reason": None}
    )
    assert "has exited" in _disconnected_bridge_reason(
        {"prepared": prepared, "terminal_reason": "owned_bridge_process_exited"}
    )
    assert "not reported its state" in _disconnected_bridge_reason(
        {"prepared": prepared, "session_current": True, "status": None}
    )
    assert "dated in the future" in _disconnected_bridge_reason(
        {"prepared": prepared, "session_current": True, "status": {}, "status_clock_skew": True}
    )
    busy = _disconnected_bridge_reason(
        {"prepared": prepared, "session_current": True, "status": {}, "status_age_ms": 7400}
    )
    assert "7s" in busy and "the table's own script" in busy
