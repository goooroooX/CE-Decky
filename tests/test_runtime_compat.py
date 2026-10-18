from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.version_info[:2] != (3, 14), reason="requires actual Python 3.14")
@pytest.mark.parametrize("mode", ["native", "missing", "dependency-error", "load-error"])
def test_pathlib_supplement_in_cold_interpreter(mode):
    code = r'''
import importlib.abc
import importlib.util
import pathlib
import sys
sys.path.insert(0, sys.argv[1] + "/py_modules")
from ce_decky import runtime_compat as compat
mode = sys.argv[2]
before_path = list(sys.path)
parent = pathlib
if mode == "native":
    import pathlib.types
    native = pathlib.types
    compat.ensure_pathlib_types()
    assert pathlib.types is native
    assert not compat.PATHLIB_TYPES_FALLBACK_USED
else:
    class Missing(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "pathlib.types":
                missing = "supplement_dependency" if mode == "dependency-error" else fullname
                raise ModuleNotFoundError("No module named " + missing, name=missing)
    sys.meta_path.insert(0, Missing())
    sys.modules.pop("pathlib.types", None)
    if hasattr(pathlib, "types"):
        del pathlib.types
    if mode == "load-error":
        class BrokenLoader:
            def create_module(self, spec):
                return None
            def exec_module(self, module):
                raise RuntimeError("supplement execution failed")
        from importlib.machinery import ModuleSpec
        compat.importlib.util.spec_from_file_location = lambda *a: ModuleSpec("pathlib.types", BrokenLoader())
    if mode in {"dependency-error", "load-error"}:
        try:
            compat.ensure_pathlib_types()
        except (ModuleNotFoundError, RuntimeError) as exc:
            assert "supplement" in str(exc)
        else:
            raise AssertionError("original error was hidden")
        assert "pathlib.types" not in sys.modules
        assert not hasattr(pathlib, "types")
    else:
        compat.ensure_pathlib_types()
        assert compat.PATHLIB_TYPES_FALLBACK_USED
        assert pathlib.types is sys.modules["pathlib.types"]
        assert pathlib.types.PathInfo
        assert "stdlib_fallback" in pathlib.types.__file__
        compat.ensure_pathlib_types()
        assert compat.PATHLIB_TYPES_FALLBACK_USED
assert sys.path == before_path
assert sys.modules["pathlib"] is parent
'''
    subprocess.run([sys.executable, "-I", "-c", code, str(ROOT), mode],
                   check=True, capture_output=True, text=True, timeout=20)


def test_other_python_branches_do_not_import_the_supplement():
    if sys.version_info[:2] == (3, 14):
        pytest.skip("covered by the Python 3.14 cold subprocesses")
    from ce_decky import runtime_compat
    before = dict(sys.modules)
    runtime_compat.ensure_pathlib_types()
    assert not runtime_compat.PATHLIB_TYPES_FALLBACK_USED
    assert "pathlib.types" not in set(sys.modules) - set(before)
