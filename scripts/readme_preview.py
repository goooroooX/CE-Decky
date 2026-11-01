#!/usr/bin/env python3
"""Render the README's first screen with GitHub Markdown, real badges and pinned Chrome."""

from __future__ import annotations

import argparse
import base64
from contextlib import closing, suppress
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

if __package__:
    from . import browser_harness
    from .browser_probe import _stop_browser
    from .target_ui_freeze_probe import DevToolsSocket
else:
    import browser_harness
    from browser_probe import _stop_browser
    from target_ui_freeze_probe import DevToolsSocket


ROOT = Path(__file__).resolve().parents[1]
STYLE = """
body {margin:0;background:#fff;color:#1f2328;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
main {max-width:1012px;margin:0 auto;padding:24px 32px;overflow:hidden}
h1,h2 {padding-bottom:.3em;border-bottom:1px solid #d1d9e0;line-height:1.25;font-weight:600}
h1 {margin:0 0 16px;font-size:32px} h2 {margin:24px 0 16px;font-size:24px}
p {margin:0 0 16px} a {color:#0969da;text-decoration:none}
img {max-width:100%;vertical-align:middle} img[align=right] {margin-left:16px}
.anchor {display:none} ul {padding-left:2em} li+li {margin-top:.25em}
"""
IMAGES_READY = """(async () => {
  if (document.readyState !== 'complete') {
    await new Promise(resolve => addEventListener('load', resolve, {once: true}));
  }
  await document.fonts.ready;
  return [...document.images].every(image => image.complete && image.naturalWidth > 0);
})()"""


def github_markdown() -> str:
    search_path = os.pathsep.join((os.environ.get("PATH", ""), str(Path.home() / ".local" / "bin")))
    gh = shutil.which("gh", path=search_path)
    if not gh:
        raise ValueError("GitHub CLI (gh) is required; authenticate with gh auth login")
    repository = subprocess.run(
        [gh, "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()
    return subprocess.run(
        [gh, "api", "markdown", "--method", "POST", "--input", "-"],
        input=json.dumps({"text": (ROOT / "README.md").read_text(encoding="utf-8"),
                          "mode": "gfm", "context": repository}),
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=30,
    ).stdout


def downloads_badge(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, dict) or data.get("schemaVersion") != 1
            or data.get("label") != "Downloads"
            or not isinstance(data.get("message"), str)
            or not isinstance(data.get("color"), str)):
        raise ValueError("--downloads-json needs a Shields v1 Downloads payload with string message and color")
    return "https://img.shields.io/static/v1?" + urlencode({
        "label": data["label"], "message": data["message"],
        "color": data["color"], "style": "flat-square",
    })


def fetch_badge(url: str, assets: Path) -> str:
    request = Request(url, headers={"User-Agent": "CE-Decky-README-preview"})
    with urlopen(request, timeout=30) as response:
        svg = response.read()
    if ET.fromstring(svg).tag != "{http://www.w3.org/2000/svg}svg":
        raise ValueError(f"Shields returned something other than SVG: {url}")
    assets.mkdir(parents=True, exist_ok=True)
    target = assets / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".svg")
    target.write_bytes(svg)
    return target.resolve().as_uri()


class PreviewHTML(HTMLParser):
    def __init__(self, assets: Path, downloads_url: str | None = None):
        super().__init__(convert_charrefs=False)
        self.assets = assets
        self.downloads_url = downloads_url
        self.parts: list[str] = []

    def _start(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool = False) -> None:
        if tag != "img":
            self.parts.append(self.get_starttag_text())
            return
        attributes = dict(attrs)
        source = attributes.get("data-canonical-src") or attributes.get("src") or ""
        parsed = urlsplit(source)
        if parsed.scheme == "https" and parsed.netloc == "img.shields.io":
            use_override = parsed.path == "/endpoint" and attributes.get("alt") == "Downloads"
            url = self.downloads_url if use_override and self.downloads_url else source
            replacement = fetch_badge(url, self.assets)
        elif not parsed.scheme and not parsed.netloc and parsed.path:
            image = (ROOT / unquote(parsed.path)).resolve()
            if not image.is_relative_to(ROOT) or not image.is_file():
                raise ValueError(f"README image is missing or outside the checkout: {source}")
            replacement = image.as_uri()
        else:
            self.parts.append(self.get_starttag_text())
            return
        attributes["src"] = replacement
        rendered = " ".join(key if value is None else f'{key}="{escape(value, quote=True)}"'
                            for key, value in attributes.items())
        self.parts.append(f"<{tag} {rendered}" + (" />" if closed else ">"))

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, closed=True)

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(f"<!--{data}-->")


def connect_page(profile: Path, page: Path, process, deadline: float) -> DevToolsSocket:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError("Chrome exited before its preview page was ready")
        try:
            port = int((profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
            with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                pages = json.load(response)
            for target in pages:
                if target.get("type") == "page" and target.get("url") == page.as_uri():
                    return DevToolsSocket(target["webSocketDebuggerUrl"], timeout=1)
        except (OSError, ValueError, IndexError):
            pass
        time.sleep(0.1)
    raise TimeoutError("Chrome preview page did not become ready within 30 seconds")


def screenshot(page: Path, output: Path) -> None:
    output.unlink(missing_ok=True)
    browser = browser_harness.ensure_browser(browser_harness.current_pin())
    log = output.with_suffix(".chrome.log")
    with tempfile.TemporaryDirectory(prefix="chrome-", dir=output.parent) as profile:
        with log.open("wb") as diagnostics:
            process = subprocess.Popen([
                str(browser), "--headless", "--disable-gpu", "--hide-scrollbars",
                "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                "--allow-file-access-from-files", "--window-size=1100,650", "--remote-debugging-port=0",
                f"--user-data-dir={profile}", page.as_uri(),
            ], stdout=diagnostics, stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
            try:
                deadline = time.monotonic() + 30
                with closing(connect_page(Path(profile), page, process, deadline)) as connection:
                    def call(method, params):
                        reply = connection.await_reply(connection.send(method, params), deadline)
                        if "error" in reply:
                            raise ValueError(f"{method} failed: {reply['error']}")
                        return reply.get("result", {})

                    call("Emulation.setDeviceMetricsOverride", {
                        "width": 1100, "height": 650, "deviceScaleFactor": 1, "mobile": False,
                    })
                    ready = call("Runtime.evaluate", {
                        "expression": IMAGES_READY, "awaitPromise": True, "returnByValue": True,
                    })
                    if ready.get("result", {}).get("value") is not True:
                        raise ValueError("README images did not all load")
                    result = call("Page.captureScreenshot", {"format": "png"})
                    output.write_bytes(base64.b64decode(result.get("data", ""), validate=True))
            finally:
                _stop_browser(process)
    if not output.is_file() or not output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(f"Chrome did not produce a PNG; see {log}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build" / "readme-preview")
    parser.add_argument("--downloads-json", type=Path, help="explicit local Downloads payload for an unpublished endpoint")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    try:
        output.mkdir(parents=True, exist_ok=True)
        (output / "readme.png").unlink(missing_ok=True)
        override = downloads_badge(args.downloads_json) if args.downloads_json else None
        fragment = PreviewHTML(output / "assets", override)
        fragment.feed(github_markdown())
        fragment.close()
        page = output / "readme.html"
        page.write_text('<!doctype html><meta charset="utf-8"><title>CE Decky README preview</title>'
                        f"<style>{STYLE}</style><main>{''.join(fragment.parts)}</main>", encoding="utf-8")
        screenshot(page, output / "readme.png")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as exc:
        with suppress(OSError):
            (output / "readme.png").unlink(missing_ok=True)
        print(f"README preview: FAILED - {exc}")
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip())
        return 1
    if args.downloads_json:
        print(f"README preview: Downloads uses local payload {args.downloads_json}; other badges are live")
    print(f"README preview: {output / 'readme.png'} (1100x650; GitHub HTML with local layout styling)")
    print(f"HTML: {page}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
