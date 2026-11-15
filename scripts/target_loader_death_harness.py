"""The loader half of `target_loader_death_probe.py`, run inside Decky's own runtime.

Not a command. `target_loader_death_probe.py` starts Decky's frozen
`PluginLoader` through the PyInstaller resource-tracker route that
`decky_compat_probe.py` also uses, puts this directory on `sys.path` and calls
`run()`. What runs is Decky's own plugin host: its `PluginWrapper` starts the
plugin through `multiprocessing` exactly as the service does, so the plugin's
parent is the forkserver and its grandparent is this process, and its socket is
Decky's own `LocalSocket`. One thing differs, and only inside the plugin
process: Decky derives the Steam user's home from `pwd`, which would point the
plugin at the real managed root beside the live backend, so the subclass below
moves that one path into the probe's own directory before Decky's
`initialize()` runs. The homebrew directory moves through Decky's own
`UNPRIVILEGED_PATH`, set by the probe.

The subclass is defined at module level so the forkserver can import it by name;
importing this file anywhere without Decky's runtime defines nothing.
"""

from __future__ import annotations

import os

try:  # Only Decky's frozen runtime carries this package.
    from decky_loader import helpers as _helpers
    from decky_loader.plugin import sandboxed_plugin as _sandboxed
    from decky_loader.plugin.sandboxed_plugin import SandboxedPlugin
except ImportError:  # pragma: no cover - exercised only inside the runtime
    SandboxedPlugin = None  # type: ignore[assignment,misc]

HOME_VARIABLE = "LOADER_DEATH_PROBE_USER_HOME"

if SandboxedPlugin is not None:  # pragma: no cover - exercised only inside the runtime

    class HomedSandboxedPlugin(SandboxedPlugin):  # type: ignore[misc,valid-type]
        def initialize(self, socket):
            home = os.environ[HOME_VARIABLE]
            _helpers.get_home_path = lambda *_args, **_kwargs: home
            _sandboxed.get_home_path = lambda *_args, **_kwargs: home
            return super().initialize(socket)


def run(pids_file: str) -> None:  # pragma: no cover - exercised only inside the runtime
    """Hold one plugin through Decky's own wrapper until killed."""
    import asyncio
    import json

    from decky_loader.plugin.plugin import PluginWrapper

    async def main() -> None:
        async def emit(_event, _args):
            return None

        plugins = os.path.join(os.environ["UNPRIVILEGED_PATH"], "plugins")
        wrapper = PluginWrapper(os.path.join(plugins, "CE-Decky", "main.py"), "CE-Decky", plugins, emit)
        original = wrapper.sandboxed_plugin
        wrapper.sandboxed_plugin = HomedSandboxedPlugin(
            original.name, original.passive, original.flags, original.file, original.plugin_directory,
            original.plugin_path, original.version, original.author, original.api_version,
        )
        wrapper.start()
        with open(pids_file, "w", encoding="utf-8") as handle:
            json.dump({"loader": os.getpid(), "plugin": wrapper.proc.pid}, handle)
        while True:
            await asyncio.sleep(1)

    asyncio.run(main())
