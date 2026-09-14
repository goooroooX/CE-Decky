from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_uses_packaged_stdlib_for_full_production_import_graph(tmp_path: Path) -> None:
    plugin_root = tmp_path / "CE-Decky"
    fallback = plugin_root / "py_modules" / "stdlib_fallback"
    package = plugin_root / "py_modules" / "ce_decky"
    plugin_root.mkdir()
    shutil.copy2(ROOT / "main.py", plugin_root / "main.py")
    shutil.copytree(ROOT / "py_modules" / "stdlib_fallback", fallback)
    shutil.copytree(
        ROOT / "py_modules" / "ce_decky",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    smoke = r'''
import importlib.abc
import importlib.util
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1])
fallback = root / "py_modules" / "stdlib_fallback"
missing_xml = sys.argv[2] == "xml-and-html"

class MissingDeckyStdlib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if missing_xml and (fullname == "xml" or fullname.startswith("xml.")) and str(fallback) not in sys.path:
            cause = ModuleNotFoundError("No module named 'xml.etree'")
            cause.name = "xml.etree"
            raise cause
        if fullname == "html.parser" and str(fallback) not in sys.path:
            cause = ModuleNotFoundError("No module named 'html.parser'")
            cause.name = "html.parser"
            raise cause
        return None

sys.meta_path.insert(0, MissingDeckyStdlib())
sys.modules["decky"] = types.ModuleType("decky")
spec = importlib.util.spec_from_file_location("ce_decky_package_main", root / "main.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
assert isinstance(module.Plugin, type)
import xml.etree.ElementTree as etree
import html.parser
import _markupbase
from ce_decky.catalog import BeautifulSoup
if missing_xml:
    assert pathlib.Path(etree.__file__).is_relative_to(fallback)
assert pathlib.Path(html.parser.__file__).is_relative_to(fallback)
assert pathlib.Path(_markupbase.__file__).is_relative_to(fallback)
assert etree.fromstring("<CheatTable><CheatEntries /></CheatTable>").tag == "CheatTable"
assert BeautifulSoup("<p>A&amp;B</p>", "html.parser").get_text() == "A&B"
'''
    for missing in ("xml-and-html", "html-only"):
        subprocess.run(
            [sys.executable, "-I", "-c", smoke, str(plugin_root), missing],
            check=True,
            capture_output=True,
            text=True,
        )
