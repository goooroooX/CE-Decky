#!/usr/bin/env python3
"""Discover and exercise an installed Chrome-compatible headless browser."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
READY_TITLE = "CE Decky headless-ready"


def browser_candidates(*, environment: dict[str, str] | None = None, windows: bool | None = None) -> list[Path | str]:
    env = environment or os.environ
    is_windows = os.name == "nt" if windows is None else windows
    candidates: list[Path | str] = []
    override = env.get("CE_DECKY_BROWSER", "").strip()
    if override:
        candidates.append(Path(override))
    if is_windows:
        for base in (env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA")):
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
        for base in (env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)")):
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    else:
        candidates.extend(("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))
    return candidates


def discover_browser(*, environment: dict[str, str] | None = None, windows: bool | None = None) -> str | None:
    for candidate in browser_candidates(environment=environment, windows=windows):
        if isinstance(candidate, Path):
            if candidate.is_file():
                return str(candidate)
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def _stop_browser(process: subprocess.Popen[object]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _log_tail(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return "browser produced no diagnostic log"
    return lines[-1] if lines else "browser produced no diagnostic log"


def _devtools_ready(profile: Path) -> tuple[bool, str | None]:
    try:
        lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        if not 1 <= port <= 65535:
            return False, None
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            version = json.load(response)
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
            targets = json.load(response)
    except (OSError, ValueError, json.JSONDecodeError):
        return False, None
    browser = version.get("Browser") if isinstance(version, dict) else None
    ready = isinstance(targets, list) and any(
        isinstance(target, dict) and target.get("type") == "page" and target.get("title") == READY_TITLE
        for target in targets
    )
    return ready, browser if isinstance(browser, str) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path)
    args = parser.parse_args(argv)
    browser = str(args.browser) if args.browser else discover_browser()
    if not browser:
        print("browser probe: INCOMPLETE - Chrome/Chromium not detected; set CE_DECKY_BROWSER")
        return 2
    work_root = ROOT / "build" / "browser-probe"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="run-", dir=work_root) as temporary:
        temp = Path(temporary)
        page = "<!doctype html><title>pending</title><main>headless-ready</main>" \
            f"<script>document.title={json.dumps(READY_TITLE)}</script>"
        page_url = f"data:text/html;charset=utf-8,{quote(page)}"
        profile = temp / "profile"
        stderr_path = temp / "browser.err.txt"
        stdout_path = temp / "browser.out.txt"
        command = [
            browser,
            "--headless",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-breakpad",
            "--disable-component-extensions-with-background-pages",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--password-store=basic",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            page_url,
        ]
        process: subprocess.Popen[object] | None = None
        ready = False
        browser_version: str | None = None
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    command,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=os.name == "posix",
                )
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    ready, browser_version = _devtools_ready(profile)
                    if ready:
                        break
                    time.sleep(0.1)
        except OSError as exc:
            print(f"browser probe: INCOMPLETE - {exc}")
            return 2
        finally:
            if process is not None:
                _stop_browser(process)
        if not ready:
            print(f"browser probe: FAILED - {_log_tail(stderr_path)}")
            return 1
    print(f"browser probe: PASS ({browser_version or browser})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
