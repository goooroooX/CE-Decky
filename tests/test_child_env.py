from __future__ import annotations

from ce_decky.archive_import import _sevenzip_env
from ce_decky.ce_launch import CELaunchPlan, launch_environment
from ce_decky.child_env import child_environment


def test_the_bundles_own_library_path_is_undone_for_a_system_binary(monkeypatch):
    """Decky's loader is a PyInstaller bundle and every child inherited its libs.

    On the target `/usr/bin/7z` is a `#!/bin/sh` wrapper, so `/bin/sh` resolved
    the bundle's readline and exited on an undefined symbol before 7-Zip ran:
    every `.7z` and `.rar` the plugin was asked to open failed, and the probe
    that asks 7-Zip which formats it carries read that as a build with no RAR
    handler. PyInstaller saves what was there as `_ORIG`, which is what gets
    restored.
    """
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/real/lib")
    env = child_environment()
    assert env["LD_LIBRARY_PATH"] == "/opt/real/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env

    # Nothing to restore means the variable belonged to the bundle alone.
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/_MEI123/hook.so")
    stripped = child_environment()
    assert "LD_LIBRARY_PATH" not in stripped and "LD_PRELOAD" not in stripped

    # An environment that never had one is left exactly as it is.
    monkeypatch.delenv("LD_PRELOAD")
    assert child_environment({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_every_spawned_system_binary_starts_from_that_environment(monkeypatch):
    # The two that run a system binary from a path this plugin resolved. Both
    # were reading `os.environ` directly, so the bundle's loader settings
    # reached 7-Zip and Cheat Engine alike.
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEI123")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in _sevenzip_env()
    assert _sevenzip_env()["LC_ALL"] == "C"

    plan = CELaunchPlan.__new__(CELaunchPlan)
    object.__setattr__(plan, "env_overrides", (("CE_DECKY_TEST", "1"),))
    launched = launch_environment(plan)
    assert "LD_LIBRARY_PATH" not in launched
    assert launched["CE_DECKY_TEST"] == "1"
