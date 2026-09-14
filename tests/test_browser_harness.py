from __future__ import annotations

import hashlib
from pathlib import Path
import stat
import zipfile

import pytest

from scripts.browser_harness import BrowserPin, LINUX_X64_PIN, extract_archive, verify_archive


def _pin(payload: bytes) -> BrowserPin:
    return BrowserPin(
        version="1.2.3.4",
        url="https://storage.googleapis.com/chrome-for-testing-public/1.2.3.4/linux64/chrome-linux64.zip",
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        archive_root="chrome-linux64",
        executable="chrome",
        executable_size=7,
        executable_sha256=hashlib.sha256(b"browser").hexdigest(),
    )


def test_linux_browser_pin_is_exact_official_chrome_for_testing_identity():
    assert LINUX_X64_PIN.url == (
        f"https://storage.googleapis.com/chrome-for-testing-public/{LINUX_X64_PIN.version}/"
        "linux64/chrome-linux64.zip"
    )
    assert LINUX_X64_PIN.size > 100_000_000
    assert len(LINUX_X64_PIN.sha256) == 64
    assert LINUX_X64_PIN.executable_size > LINUX_X64_PIN.size
    assert len(LINUX_X64_PIN.executable_sha256) == 64


def test_archive_verification_requires_exact_size_and_sha(tmp_path: Path):
    archive = tmp_path / "chrome.zip"
    archive.write_bytes(b"reviewed chrome fixture")
    verify_archive(archive, _pin(archive.read_bytes()))
    archive.write_bytes(b"tampered chrome fixture")
    with pytest.raises(ValueError, match="size|SHA-256"):
        verify_archive(archive, _pin(b"reviewed chrome fixture"))


def test_extraction_preserves_executable_and_rejects_traversal(tmp_path: Path):
    archive = tmp_path / "chrome.zip"
    pin = _pin(b"unused")
    with zipfile.ZipFile(archive, "w") as output:
        info = zipfile.ZipInfo("chrome-linux64/chrome")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        output.writestr(info, b"browser")
    destination = tmp_path / "safe"
    extract_archive(archive, destination, pin)
    executable = destination / "chrome-linux64" / "chrome"
    assert executable.read_bytes() == b"browser"
    assert executable.stat().st_mode & stat.S_IXUSR

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as output:
        output.writestr("chrome-linux64/../escape", b"escape")
    with pytest.raises(ValueError, match="unsafe member path"):
        extract_archive(unsafe, tmp_path / "rejected", pin)
