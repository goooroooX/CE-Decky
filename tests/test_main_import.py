from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_main_imports_its_packaged_modules_without_loader_path_injection():
    smoke = """
import importlib.util
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root))
sys.modules["decky"] = types.ModuleType("decky")
spec = importlib.util.spec_from_file_location("ce_decky_source_main", root / "main.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
assert isinstance(module.Plugin, type)
"""
    subprocess.run(
        [sys.executable, "-I", "-c", smoke, str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
