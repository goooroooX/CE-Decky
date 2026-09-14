from __future__ import annotations

from pathlib import Path
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

try:
    from scripts.frontend_source_digest import is_current as frontend_bundle_is_current
    from scripts.update_runtime_vendor import failures as runtime_vendor_failures
except ModuleNotFoundError:  # Direct `python scripts/package_plugin.py` execution.
    from frontend_source_digest import is_current as frontend_bundle_is_current
    from update_runtime_vendor import failures as runtime_vendor_failures

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / "package.json").read_text())["version"]
STAGE = ROOT / "build" / "CE-Decky"
ARTIFACT = ROOT / "artifacts" / f"CE-Decky-v{VERSION}.zip"
ZIP_TIMESTAMP = (2026, 8, 15, 0, 0, 0)

COPY_FILES = [
    "dist",
    "py_modules",
    "main.py",
    "package.json",
    "plugin.json",
    "README.md",
    "LICENSE",
]
# The Decky CLI packages a fixed set of top-level files and strips the
# `defaults/` prefix from everything under it, which is the only route a
# notices or license file has into the Store artifact. One canonical copy lives
# there and both packagers put it at the root of the archive, so the two builds
# ship the same paths instead of one of them shipping fewer.
DEFAULTS = ROOT / "defaults"


def _reject_source_links(source: Path) -> None:
    if source.is_symlink():
        raise SystemExit(f"packaging source must not be a symlink: {source.relative_to(ROOT)}")
    if source.is_dir():
        for path in source.rglob("*"):
            if path.is_symlink():
                raise SystemExit(f"packaging source contains a symlink: {path.relative_to(ROOT)}")


def _write_reproducible_member(zf: zipfile.ZipFile, path: Path, arcname: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"packaging stage contains a non-regular member: {path}")
    info = zipfile.ZipInfo(str(arcname).replace("\\", "/"), ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    info.external_attr = ((stat.S_IFREG | mode) & 0xFFFF) << 16
    with path.open("rb") as handle:
        zf.writestr(info, handle.read())


def verify_installed_backend(archive: Path = ARTIFACT, package_root: str = "CE-Decky") -> None:
    """Import the backend exactly from the freshly packed plugin tree.

    The Store artifact is a different build of the same runtime contract, so
    `scripts/check_store_artifact.py` runs this against that ZIP rather than
    keeping a second copy of the isolation this sets up.

    Decky supplies the ``decky`` module at runtime, so this is the only host
    integration represented by a minimal stub.  ``-I`` prevents the checkout,
    its virtual environment, and ``PYTHONPATH`` from hiding a missing packaged
    module or dependency.
    """
    smoke = """
import importlib.util
import importlib.abc
import pathlib
import sys
import types

root = pathlib.Path(sys.argv[1])
fallback = root / "py_modules" / "stdlib_fallback"

class MissingDeckyStdlib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname == "xml" or fullname.startswith("xml.")) and str(fallback) not in sys.path:
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
assert pathlib.Path(etree.__file__).is_relative_to(fallback)
assert pathlib.Path(html.parser.__file__).is_relative_to(fallback)
assert pathlib.Path(_markupbase.__file__).is_relative_to(fallback)
"""
    with tempfile.TemporaryDirectory(prefix="ce-decky-package-") as temporary:
        with zipfile.ZipFile(archive) as packed:
            packed.extractall(temporary)
        plugin_root = Path(temporary) / package_root
        subprocess.run(
            [sys.executable, "-I", "-c", smoke, str(plugin_root)],
            check=True,
            capture_output=True,
            text=True,
        )


def main() -> None:
    if not (ROOT / "dist" / "index.js").is_file():
        raise SystemExit("dist/index.js missing: restore dependencies and run the official `pnpm run build`")
    if not frontend_bundle_is_current():
        raise SystemExit("dist/index.js is stale for current frontend sources: run the official `pnpm run build` before packaging")
    # The runtime payload is the committed tree now, because the official Decky
    # Store builder copies `py_modules/` as the repository holds it and installs
    # nothing. Packaging trusts that tree only once this says it is the recorded
    # one, which is the same check `scripts/verify_repo.py` runs.
    for failure in runtime_vendor_failures():
        raise SystemExit(failure)
    # Preserve the QA logs and other local state under build/.
    shutil.rmtree(STAGE, ignore_errors=True)
    STAGE.mkdir(parents=True)
    for item in COPY_FILES:
        source = ROOT / item
        _reject_source_links(source)
        target = STAGE / item
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                # The source map is 1.8x the bundle it describes and embeds the
                # whole TypeScript source. It stays in the repository for anyone
                # debugging a released build; it is not shipped to every device.
                ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", ".ce-decky-source.sha256", "index.js.map",
                ),
            )
        else:
            shutil.copy2(source, target)
    for source in sorted(DEFAULTS.iterdir()):
        _reject_source_links(source)
        target = STAGE / source.name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(source, target)
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.unlink(missing_ok=True)
    with zipfile.ZipFile(ARTIFACT, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(STAGE.rglob("*"), key=lambda item: item.relative_to(STAGE).as_posix()):
            if path.is_dir():
                continue
            _write_reproducible_member(zf, path, Path("CE-Decky") / path.relative_to(STAGE))
    with zipfile.ZipFile(ARTIFACT) as zf:
        bad = zf.testzip()
        if bad:
            raise SystemExit(f"packaging produced corrupt member: {bad}")
    verify_installed_backend()
    print(ARTIFACT)


if __name__ == "__main__":
    main()
