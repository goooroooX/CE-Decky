#!/usr/bin/env python3
"""Rebuild and check the committed pure-Python runtime dependency tree.

The official Decky Store builder copies `py_modules/` exactly as the repository
holds it and has no Python dependency phase of its own, so the backend's third
party imports have to be in the tree rather than produced at packaging time.
That is the reason this project commits `py_modules/vendor`, which is a
deliberate exception to keeping generated dependencies out of Git.

Two digests are recorded beside the lock, in `requirements-runtime.vendor.sha256`:
the lock's own digest, and a digest of the committed tree. `--check` compares
both and is what `scripts/verify_repo.py` runs on every profile, so an edited
tree and a lock that moved without a rebuild are each refused before packaging.

What that check is worth, exactly: it proves the committed tree is the one that
was recorded and that the lock has not moved since. It does not re-derive the
tree from the index, because doing so would put a network fetch in every
repository stage. `--rebuild-check` is the stronger form, and it is a maintainer
command rather than a gate: it installs from the lock into a temporary
directory and compares that against what is committed.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "requirements-runtime.lock"
VENDOR = ROOT / "py_modules" / "vendor"
STATE = ROOT / "requirements-runtime.vendor.sha256"
# The three imports the production backend makes directly. A tree that cannot
# answer these is not a runtime payload whatever else it holds.
REQUIRED_VENDOR_FILES = ("httpx/__init__.py", "bs4/__init__.py", "defusedxml/__init__.py")
NATIVE_SUFFIXES = {".exe", ".dll", ".pyd", ".so", ".dylib"}


def lock_digest() -> str:
    return sha256(LOCK.read_bytes()).hexdigest()


def _tracked(root: Path, path: Path) -> bool:
    """Bytecode is not part of the tree, even while it sits in it.

    Anything that imports from this payload writes `__pycache__` into it: the
    backend does on the device, and the test run does here the moment a suite
    puts `py_modules/vendor` on `sys.path`. None of it is committed and none of
    it is packaged, so a digest that counted it would report the tree as edited
    for having been used.
    """
    relative = path.relative_to(root)
    return "__pycache__" not in relative.parts and relative.suffix != ".pyc"


def tree_digest(root: Path) -> str:
    """One digest over every tracked file in `root`, name and content, order fixed.

    Lengths are hashed beside the values so that a name ending where the next
    one begins cannot produce the same stream as a different tree.
    """
    digest = sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_dir() or not _tracked(root, path):
            continue
        name = path.relative_to(root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _installer_python() -> str:
    """An interpreter that actually has `pip`, or a sentence saying none does.

    SteamOS ships a system `python3` with no `pip` module at all, so running
    this helper the way every command here is written starts it under an
    interpreter that cannot install. It could create the environment, because
    `venv` and `ensurepip` are both there, which is why `qa.py --bootstrap`
    works from it and only this helper does not. That used to surface as a pip
    traceback about a failed subprocess; the development environment this
    project already caches is the answer, and its absence is one line.
    """
    if importlib.util.find_spec("pip") is not None:
        return sys.executable
    venv = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv.is_file():
        return str(venv)
    raise SystemExit(
        "this interpreter has no pip and there is no .venv to borrow one from: "
        "run `python scripts/qa.py --bootstrap` first"
    )


def _install(target: Path) -> None:
    """Install the locked distributions into `target` and make them shippable."""
    subprocess.run([
        _installer_python(), "-m", "pip", "install", "--disable-pip-version-check",
        "--require-hashes", "--no-deps", "--no-compile", "--only-binary=:all:",
        "--target", str(target), "-r", str(LOCK),
    ], check=True)
    shutil.rmtree(target / "bin", ignore_errors=True)
    for path in sorted(target.rglob("__pycache__"), reverse=True):
        shutil.rmtree(path, ignore_errors=True)
    for path in target.rglob("*.pyc"):
        path.unlink(missing_ok=True)
    for record in target.glob("*.dist-info/RECORD"):
        lines = record.read_text(encoding="utf-8").splitlines()
        normalized = [line for line in lines if not line.startswith("../../bin/") and "/__pycache__/" not in line]
        record.write_text("\n".join(normalized) + "\n", encoding="utf-8", newline="\n")
    native = [
        path for path in target.rglob("*")
        if path.is_file() and path.suffix.casefold() in NATIVE_SUFFIXES
    ]
    if native:
        raise SystemExit(f"runtime dependency lock produced non-pure artifacts: {native}")
    missing = [name for name in REQUIRED_VENDOR_FILES if not (target / name).is_file()]
    if missing:
        raise SystemExit(f"runtime dependency lock did not provide: {missing}")


def rebuild() -> None:
    temporary = VENDOR.with_name(f"{VENDOR.name}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    try:
        _install(temporary)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    shutil.rmtree(VENDOR, ignore_errors=True)
    temporary.replace(VENDOR)
    STATE.write_text(f"{lock_digest()}\n{tree_digest(VENDOR)}\n", encoding="ascii", newline="\n")


def failures() -> list[str]:
    """Why the committed tree is not the recorded one, or nothing."""
    if not VENDOR.is_dir():
        return ["py_modules/vendor is absent: run `python scripts/update_runtime_vendor.py`"]
    if not STATE.is_file():
        return [f"{STATE.name} is absent: run `python scripts/update_runtime_vendor.py`"]
    recorded = STATE.read_text(encoding="ascii").split()
    if len(recorded) != 2:
        return [f"{STATE.name} does not hold a lock digest and a tree digest"]
    recorded_lock, recorded_tree = recorded
    found: list[str] = []
    if recorded_lock != lock_digest():
        found.append(
            "requirements-runtime.lock changed without a vendor rebuild: "
            "run `python scripts/update_runtime_vendor.py`"
        )
    if recorded_tree != tree_digest(VENDOR):
        found.append(
            "py_modules/vendor does not match its recorded digest: "
            "run `python scripts/update_runtime_vendor.py` and commit the result"
        )
    missing = [name for name in REQUIRED_VENDOR_FILES if not (VENDOR / name).is_file()]
    if missing:
        found.append(f"py_modules/vendor is missing required modules: {missing}")
    native = [
        path.relative_to(ROOT).as_posix() for path in VENDOR.rglob("*")
        if path.is_file() and _tracked(VENDOR, path) and path.suffix.casefold() in NATIVE_SUFFIXES
    ]
    if native:
        found.append(f"py_modules/vendor holds non-pure artifacts: {native}")
    return found


def _rebuild_check() -> list[str]:
    with tempfile.TemporaryDirectory(prefix="ce-decky-vendor-") as temporary:
        target = Path(temporary) / "vendor"
        target.mkdir()
        _install(target)
        if tree_digest(target) != tree_digest(VENDOR):
            return ["a fresh install from the lock does not match the committed tree"]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="verify the committed tree without touching it")
    group.add_argument(
        "--rebuild-check", action="store_true",
        help="install from the lock into a temporary directory and compare it with the committed tree",
    )
    arguments = parser.parse_args()
    if arguments.check or arguments.rebuild_check:
        found = failures()
        if not found and arguments.rebuild_check:
            found = _rebuild_check()
        for failure in found:
            print(failure, file=sys.stderr)
        raise SystemExit(1 if found else 0)
    rebuild()
    print(f"{VENDOR.relative_to(ROOT).as_posix()}: {tree_digest(VENDOR)}")


if __name__ == "__main__":
    main()
