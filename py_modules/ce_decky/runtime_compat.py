"""Narrow import supplement for the Python 3.14 frozen Decky host.

CPython v3.14.8 Lib/pathlib/types.py is shipped verbatim, with its license,
under stdlib_fallback/python314. Native modules always take precedence.
"""
from __future__ import annotations

import importlib
import importlib.util
import pathlib
import sys

PATHLIB_TYPES_FALLBACK_USED = False


def ensure_pathlib_types() -> None:
    """Run before importing anyio/httpcore; never replace a complete stdlib."""
    global PATHLIB_TYPES_FALLBACK_USED
    if sys.version_info[:2] != (3, 14):
        return
    name = "pathlib.types"
    try:
        importlib.import_module(name)
        return
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
    source = pathlib.Path(__file__).resolve().parents[1] / "stdlib_fallback" / "python314" / "pathlib_types.py"
    spec = importlib.util.spec_from_file_location(name, source)
    if spec is None or spec.loader is None:
        raise ImportError("Decky pathlib.types supplement could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if sys.modules.get(name) is module:
            del sys.modules[name]
        raise
    pathlib.types = module
    PATHLIB_TYPES_FALLBACK_USED = True
