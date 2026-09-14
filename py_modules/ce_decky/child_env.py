"""The environment a child process of this plugin may inherit.

Decky's loader is a PyInstaller bundle. PyInstaller points ``LD_LIBRARY_PATH``
at its own unpacked libraries so the frozen interpreter finds them, and saves
whatever was there before as ``LD_LIBRARY_PATH_ORIG``. A plugin backend inherits
both, and until this existed every system binary the plugin ran inherited them
again.

On this project's target that is not a subtle preference. ``/usr/bin/7z`` is a
``#!/bin/sh`` wrapper, so ``/bin/sh`` itself resolved the bundle's readline and
exited with ``undefined symbol: rl_trim_arg_from_keyseq`` before 7-Zip ran at
all. Every ``.7z`` and ``.rar`` the plugin was asked to open failed that way, and
the probe that asks 7-Zip which formats it carries read the failure as a build
with no RAR handler, so those rows were dropped from searches on a device whose
7-Zip has one.

Restoring the ``_ORIG`` value, and unsetting the variable when there was none,
is the convention PyInstaller documents for exactly this handoff.
"""

from __future__ import annotations

import os

# Dynamic-loader variables a bundled interpreter sets for itself. Anything the
# plugin executes is a system binary against the system's own libraries.
_LOADER_VARIABLES = ("LD_LIBRARY_PATH", "LD_PRELOAD")


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """This process's environment, with the bundle's loader settings undone."""
    env = dict(os.environ if base is None else base)
    for name in _LOADER_VARIABLES:
        original = env.pop(f"{name}_ORIG", None)
        if original:
            env[name] = original
        else:
            env.pop(name, None)
    return env
