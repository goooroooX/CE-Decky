from pathlib import Path
import sys

_PLUGIN_ROOT = Path(__file__).resolve().parent
_PY_MODULES = _PLUGIN_ROOT / "py_modules"
_VENDOR = _PY_MODULES / "vendor"
for _path in (_PY_MODULES, _VENDOR):
    if _path.is_dir():
        sys.path.insert(0, str(_path))


def _ensure_decky_stdlib() -> None:
    """Supply pure-Python CPython modules omitted by Decky's runtime."""
    fallback = _PY_MODULES / "stdlib_fallback"

    try:
        import xml.etree.ElementTree  # noqa: F401
    except ModuleNotFoundError as cause:
        if cause.name not in {"xml", "xml.etree", "xml.etree.ElementTree"}:
            raise
        if not (fallback / "xml" / "etree" / "ElementTree.py").is_file():
            raise ModuleNotFoundError("Decky XML stdlib fallback is missing from the plugin package")
        for name in tuple(sys.modules):
            if name == "xml" or name.startswith("xml."):
                sys.modules.pop(name, None)
        if str(fallback) not in sys.path:
            sys.path.insert(0, str(fallback))
        import xml.etree.ElementTree  # noqa: F401

    try:
        import html.parser  # noqa: F401
        import _markupbase  # noqa: F401
    except ModuleNotFoundError as cause:
        if cause.name not in {"html", "html.entities", "html.parser", "_markupbase"}:
            raise
        required = (
            fallback / "html" / "__init__.py",
            fallback / "html" / "entities.py",
            fallback / "html" / "parser.py",
            fallback / "_markupbase.py",
        )
        if not all(path.is_file() for path in required):
            raise ModuleNotFoundError("Decky HTML stdlib fallback is missing from the plugin package")
        for name in tuple(sys.modules):
            if name == "html" or name.startswith("html.") or name == "_markupbase":
                sys.modules.pop(name, None)
        if str(fallback) not in sys.path:
            sys.path.insert(0, str(fallback))
        import html.parser  # noqa: F401
        import _markupbase  # noqa: F401


_ensure_decky_stdlib()
del _ensure_decky_stdlib

from ce_decky.plugin import Plugin

__all__ = ["Plugin"]
