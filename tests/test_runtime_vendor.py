"""The committed runtime payload is checked as a payload, not as a directory.

`py_modules/vendor` is third-party code this project ships because the official
Decky Store builder installs no Python dependencies. Nothing about it is
obvious from reading it: whether it is the tree the lock produced, whether an
edit landed in it afterwards, and whether anything in it is a native artifact
are all questions only a digest and a suffix answer. These are those answers.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from scripts import update_runtime_vendor as vendor


def test_the_committed_tree_is_the_one_the_lock_produced():
    assert vendor.failures() == []


def test_every_module_the_backend_imports_directly_is_present():
    for name in vendor.REQUIRED_VENDOR_FILES:
        assert (vendor.VENDOR / name).is_file(), name


def test_the_shipped_tree_carries_no_native_artifact():
    native = [
        path.relative_to(vendor.ROOT).as_posix()
        for path in vendor.VENDOR.rglob("*")
        if path.is_file() and path.suffix.casefold() in vendor.NATIVE_SUFFIXES
    ]
    assert native == []


def test_the_distribution_metadata_and_its_licenses_are_retained():
    """A notice file that packaging drops is a license obligation dropped."""
    distributions = sorted(path.name for path in vendor.VENDOR.glob("*.dist-info"))
    assert len(distributions) == 10, distributions
    for path in vendor.VENDOR.glob("*.dist-info"):
        assert (path / "METADATA").is_file(), path.name
        assert (path / "RECORD").is_file(), path.name


def test_no_bytecode_is_shipped():
    """Asked of what is committed, because the working tree fills with it.

    Anything that imports from this payload writes `__pycache__` into it, so
    the directory on disk is not the question. What ships is what Git holds.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--", "py_modules/vendor"],
        cwd=vendor.ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert tracked, "the vendor tree is not committed"
    assert [name for name in tracked if name.endswith(".pyc") or "__pycache__" in name] == []


def test_the_interpreter_running_this_is_used_when_it_has_pip():
    assert vendor._installer_python() == sys.executable


def test_the_cached_environment_is_borrowed_when_it_does_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """SteamOS ships a python3 with no pip, which is how this is met in practice."""
    venv = tmp_path / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text("", encoding="utf-8")
    monkeypatch.setattr(vendor, "ROOT", tmp_path)
    monkeypatch.setattr(vendor.importlib.util, "find_spec", lambda name: None)

    assert vendor._installer_python() == str(venv / "python")


def test_no_interpreter_with_pip_is_one_sentence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Not a traceback out of a failed pip subprocess, which is what it was."""
    monkeypatch.setattr(vendor, "ROOT", tmp_path)
    monkeypatch.setattr(vendor.importlib.util, "find_spec", lambda name: None)

    with pytest.raises(SystemExit) as refused:
        vendor._installer_python()

    assert "--bootstrap" in str(refused.value)


def _recorded(root: Path, lock: str, tree: str) -> None:
    (root / "requirements-runtime.vendor.sha256").write_text(f"{lock}\n{tree}\n", encoding="ascii")


def _tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lock = tmp_path / "requirements-runtime.lock"
    lock.write_text("httpx==0.28.1 --hash=sha256:00\n", encoding="utf-8")
    payload = tmp_path / "py_modules" / "vendor"
    (payload / "httpx").mkdir(parents=True)
    (payload / "httpx" / "__init__.py").write_text("__version__ = '0.28.1'\n", encoding="utf-8")
    (payload / "bs4").mkdir()
    (payload / "bs4" / "__init__.py").write_text("", encoding="utf-8")
    (payload / "defusedxml").mkdir()
    (payload / "defusedxml" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(vendor, "ROOT", tmp_path)
    monkeypatch.setattr(vendor, "LOCK", lock)
    monkeypatch.setattr(vendor, "VENDOR", payload)
    monkeypatch.setattr(vendor, "STATE", tmp_path / "requirements-runtime.vendor.sha256")
    _recorded(tmp_path, vendor.lock_digest(), vendor.tree_digest(payload))
    return payload


def test_a_tree_edited_after_it_was_recorded_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    payload = _tree(tmp_path, monkeypatch)
    (payload / "httpx" / "__init__.py").write_text("__version__ = '9.9.9'\n", encoding="utf-8")

    found = vendor.failures()
    assert any("does not match its recorded digest" in failure for failure in found), found


def test_a_lock_bumped_without_a_rebuild_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _tree(tmp_path, monkeypatch)
    vendor.LOCK.write_text("httpx==0.29.0 --hash=sha256:11\n", encoding="utf-8")

    found = vendor.failures()
    assert any("without a vendor rebuild" in failure for failure in found), found


def test_a_native_artifact_that_arrived_later_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    payload = _tree(tmp_path, monkeypatch)
    (payload / "httpx" / "_speedups.so").write_bytes(b"\x7fELF")
    _recorded(tmp_path, vendor.lock_digest(), vendor.tree_digest(payload))

    found = vendor.failures()
    assert any("non-pure artifacts" in failure for failure in found), found


def test_a_missing_backend_import_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    payload = _tree(tmp_path, monkeypatch)
    (payload / "bs4" / "__init__.py").unlink()
    _recorded(tmp_path, vendor.lock_digest(), vendor.tree_digest(payload))

    found = vendor.failures()
    assert any("missing required modules" in failure for failure in found), found


def test_bytecode_left_beside_the_payload_does_not_read_as_an_edit(tmp_path: Path):
    """Using the tree is not changing it, and the gate must not say otherwise."""
    payload = tmp_path / "vendor"
    (payload / "httpx").mkdir(parents=True)
    (payload / "httpx" / "__init__.py").write_text("x\n", encoding="utf-8")
    before = vendor.tree_digest(payload)
    cache = payload / "httpx" / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-313.pyc").write_bytes(b"\x00compiled")

    assert vendor.tree_digest(payload) == before


def test_the_digest_separates_a_name_from_the_content_beside_it(tmp_path: Path):
    """Two trees that concatenate to the same bytes must not hash the same."""
    left = tmp_path / "left"
    (left / "a").mkdir(parents=True)
    (left / "a" / "bc").write_text("d", encoding="utf-8")
    right = tmp_path / "right"
    (right / "a").mkdir(parents=True)
    (right / "a" / "b").write_text("cd", encoding="utf-8")

    assert vendor.tree_digest(left) != vendor.tree_digest(right)
