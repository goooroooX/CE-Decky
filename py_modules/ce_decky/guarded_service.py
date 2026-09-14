from __future__ import annotations

from .atomic import fsync_directory
from .service import PluginService
from .session_protocol import RuntimeCommand

MAX_RUNTIME_RPC_COMMANDS = 64


class GuardedPluginService(PluginService):
    """Production RPC policy layered over the reusable service core.

    The session protocol remains backward-compatible with existing prepared state,
    while Decky's live writer is deliberately stricter: only one bounded batch may
    be outstanding so every mutation can remain inside the bridge result-retention
    window and receive an exact acknowledgement.
    """

    _fsync_directory = staticmethod(fsync_directory)

    @staticmethod
    def _runtime_command(raw: dict[str, object]) -> RuntimeCommand:
        command = PluginService._runtime_command(raw)
        if command.kind == "retry_attach" and not command.value:
            raise ValueError("retry_attach requires an explicit target basename ending in .exe")
        return command

    def write_runtime_commands(self, app_id: int, commands: list[dict[str, object]]) -> dict[str, object]:
        with self._mutation_lock:
            if not isinstance(commands, list):
                raise ValueError("commands must be a list")
            if not commands:
                raise ValueError("at least one runtime command is required")
            if len(commands) > MAX_RUNTIME_RPC_COMMANDS:
                raise ValueError(f"runtime command batch exceeds {MAX_RUNTIME_RPC_COMMANDS} commands")

            parsed = [self._runtime_command(item) for item in commands]
            runtime = self.get_runtime_status(app_id)
            status = runtime.get("status")
            results = status.get("results") if isinstance(status, dict) else None
            acknowledged = 0
            if isinstance(results, (list, tuple)):
                for result in results:
                    if not isinstance(result, dict):
                        continue
                    generation = result.get("generation")
                    if isinstance(generation, int) and not isinstance(generation, bool) and generation > acknowledged:
                        acknowledged = generation

            if parsed[0].generation != acknowledged + 1:
                raise ValueError(
                    "wait for exact bridge acknowledgement of the previous runtime batch before issuing another batch"
                )
            return super().write_runtime_commands(app_id, commands)
