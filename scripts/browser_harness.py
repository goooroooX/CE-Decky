#!/usr/bin/env python3
"""Bootstrap the pinned project-local Chrome and run CE Decky's browser QA."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CACHE_ROOT = ROOT / "build" / "browser-runtime"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 1024


@dataclass(frozen=True)
class BrowserPin:
    version: str
    url: str
    size: int
    sha256: str
    archive_root: str
    executable: str
    executable_size: int
    executable_sha256: str


# Chrome for Testing is the official automation-oriented Chrome distribution.
# Update this tuple deliberately from the official last-known-good Stable feed,
# then download once and record the exact archive size/SHA before committing.
LINUX_X64_PIN = BrowserPin(
    version="152.0.7977.64",
    url=(
        "https://storage.googleapis.com/chrome-for-testing-public/"
        "152.0.7977.64/linux64/chrome-linux64.zip"
    ),
    size=194_030_544,
    sha256="8b592f066af71f054aab2cc80fc26f73c775c6d44ebb99d16ade924b24756c2e",
    archive_root="chrome-linux64",
    executable="chrome",
    executable_size=290_598_216,
    executable_sha256="3ed7df7904694145caf8da676d053f68300e8d2778a507e27b15f142c4efd0af",
)


def current_pin() -> BrowserPin:
    if not sys.platform.startswith("linux") or os.uname().machine not in {"x86_64", "amd64"}:
        raise RuntimeError("the project-local browser harness currently supports Linux x86_64 only")
    return LINUX_X64_PIN


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, pin: BrowserPin) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError("pinned Chrome archive is absent") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size != pin.size:
        raise ValueError("pinned Chrome archive type/size does not match its reviewed identity")
    if _sha256(path) != pin.sha256:
        raise ValueError("pinned Chrome archive SHA-256 does not match its reviewed identity")


def _download_archive(path: Path, pin: BrowserPin) -> None:
    if pin.size > MAX_ARCHIVE_BYTES:
        raise ValueError("pinned Chrome archive exceeds the local browser download limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    request = urllib.request.Request(pin.url, headers={"User-Agent": "CE-Decky-browser-harness/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("xb") as output:
            if response.geturl() != pin.url:
                raise ValueError("pinned Chrome download redirected away from its exact reviewed URL")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != pin.size:
                raise ValueError("pinned Chrome server length differs from the reviewed archive size")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > pin.size or total > MAX_ARCHIVE_BYTES:
                    raise ValueError("pinned Chrome download exceeded its exact size limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        verify_archive(temporary, pin)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_member(info: zipfile.ZipInfo, pin: BrowserPin) -> tuple[PurePosixPath, int]:
    path = PurePosixPath(info.filename)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("pinned Chrome archive contains an unsafe member path")
    if path.parts[0] != pin.archive_root:
        raise ValueError("pinned Chrome archive member is outside its expected root")
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if info.is_dir():
        if kind not in {0, stat.S_IFDIR}:
            raise ValueError("pinned Chrome archive directory has an unexpected file type")
    elif kind not in {0, stat.S_IFREG}:
        raise ValueError("pinned Chrome archive contains a non-regular file")
    return path, mode


def extract_archive(archive: Path, destination: Path, pin: BrowserPin) -> None:
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError("pinned Chrome archive member count is outside the local harness limit")
        total = 0
        reviewed: list[tuple[zipfile.ZipInfo, PurePosixPath, int]] = []
        seen: set[str] = set()
        for info in members:
            path, mode = _safe_member(info, pin)
            key = path.as_posix()
            if key in seen:
                raise ValueError("pinned Chrome archive contains a duplicate member path")
            seen.add(key)
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise ValueError("pinned Chrome archive exceeds the extracted-size limit")
            reviewed.append((info, path, mode))
        for info, path, mode in reviewed:
            target = destination.joinpath(*path.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(info) as input_file, target.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
            target.chmod(0o755 if mode & 0o111 else 0o644)


def _install_root(pin: BrowserPin, cache_root: Path = CACHE_ROOT) -> Path:
    return cache_root / "installations" / f"{pin.version}-{pin.sha256[:12]}"


def browser_path(pin: BrowserPin, cache_root: Path = CACHE_ROOT) -> Path:
    return _install_root(pin, cache_root) / pin.archive_root / pin.executable


def _installation_valid(pin: BrowserPin, cache_root: Path = CACHE_ROOT) -> bool:
    install = _install_root(pin, cache_root)
    marker = install / ".ce-decky-browser.json"
    executable = browser_path(pin, cache_root)
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
        info = executable.lstat()
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        payload == {"version": pin.version, "archive_size": pin.size, "archive_sha256": pin.sha256}
        and stat.S_ISREG(info.st_mode)
        and bool(info.st_mode & stat.S_IXUSR)
        and info.st_size == pin.executable_size
        and _sha256(executable) == pin.executable_sha256
    )


def ensure_browser(pin: BrowserPin, cache_root: Path = CACHE_ROOT) -> Path:
    if _installation_valid(pin, cache_root):
        return browser_path(pin, cache_root)
    install = _install_root(pin, cache_root)
    if install.exists() or install.is_symlink():
        raise ValueError(f"project-local Chrome cache is invalid; remove only {install} and retry")
    archive = cache_root / "downloads" / f"chrome-linux64-{pin.version}.zip"
    try:
        verify_archive(archive, pin)
    except ValueError:
        if archive.exists() or archive.is_symlink():
            raise ValueError(f"project-local Chrome archive is invalid; remove only {archive} and retry")
        print(f"browser harness: downloading pinned Chrome for Testing {pin.version}", flush=True)
        _download_archive(archive, pin)
    installations = cache_root / "installations"
    installations.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".chrome-stage-", dir=installations))
    try:
        extract_archive(archive, staging, pin)
        executable = staging / pin.archive_root / pin.executable
        if not executable.is_file() or executable.is_symlink() or not os.access(executable, os.X_OK):
            raise ValueError("extracted Chrome executable is absent, linked, or not executable")
        if executable.stat().st_size != pin.executable_size or _sha256(executable) != pin.executable_sha256:
            raise ValueError("extracted Chrome executable identity does not match the reviewed pin")
        (staging / ".ce-decky-browser.json").write_text(
            json.dumps(
                {"version": pin.version, "archive_size": pin.size, "archive_sha256": pin.sha256},
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
        os.replace(staging, install)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return browser_path(pin, cache_root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the local pinned browser without downloading")
    parser.add_argument("--install-only", action="store_true", help="bootstrap and verify Chrome without running QA")
    parser.add_argument("--profile", choices=("browser", "auto"), default="browser", help="QA profile to delegate to")
    parser.add_argument("--stage", choices=("browser-headless-probe",), help="rerun one exact browser QA stage")
    args = parser.parse_args(argv)
    if args.stage and args.profile != "browser":
        parser.error("--stage browser-headless-probe requires --profile browser")
    pin = current_pin()
    if args.check:
        if not _installation_valid(pin):
            print("browser harness: INCOMPLETE - pinned project-local Chrome is not installed")
            return 2
        executable = browser_path(pin)
    else:
        try:
            executable = ensure_browser(pin)
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            print(f"browser harness: FAILED - {exc}")
            return 1
    print(f"browser harness: ready Chrome for Testing {pin.version} ({executable})", flush=True)
    if args.check or args.install_only:
        return 0
    environment = os.environ.copy()
    environment["CE_DECKY_BROWSER"] = str(executable)
    command = [sys.executable, str(ROOT / "scripts" / "qa.py"), "--profile", args.profile]
    if args.stage:
        command.extend(("--stage", args.stage))
    return subprocess.run(command, cwd=ROOT, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
