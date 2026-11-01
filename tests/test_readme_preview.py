from html.parser import HTMLParser
import base64
from io import BytesIO
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import readme_preview as preview


class Images(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.images = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


def test_rewrites_the_actual_src_even_when_canonical_src_follows_it(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(preview, "fetch_badge", lambda url, assets: calls.append(url) or "file:///local.svg")
    parser = preview.PreviewHTML(tmp_path)
    parser.feed('<p><img src="https://camo.githubusercontent.com/abc" alt="Downloads" '
                'data-canonical-src="https://img.shields.io/endpoint?url=https%3A%2F%2Fsite%2Fdownloads.json&amp;style=flat-square" '
                'style="max-width: 100%;"> A &amp; B &#35; &#x23;</p>')
    result = "".join(parser.parts)
    image = Images(result).images[0]
    assert image["src"] == "file:///local.svg"
    assert image["data-canonical-src"] == calls[0]
    assert "&amp;style=flat-square" in result
    assert result.endswith(" A &amp; B &#35; &#x23;</p>")


def test_local_images_and_normal_markup_survive(monkeypatch, tmp_path):
    monkeypatch.setattr(preview, "ROOT", tmp_path)
    image = tmp_path / "art work.png"
    image.write_bytes(b"fixture")
    parser = preview.PreviewHTML(tmp_path / "assets")
    parser.feed('<h1>Title</h1><!--comment--><img src="art%20work.png?raw=true" width="300" />'
                '<br clear="right"><a href="#title">link</a><img src="https://example.org/image.png">')
    result = "".join(parser.parts)
    images = Images(result).images
    assert images[0] == {"src": image.as_uri(), "width": "300"}
    assert images[1]["src"] == "https://example.org/image.png"
    assert result.startswith("<h1>Title</h1><!--comment-->")
    assert '<br clear="right"><a href="#title">link</a>' in result


def test_local_image_must_exist_inside_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(preview, "ROOT", tmp_path / "repo")
    for source in ("missing.png", "../outside.png"):
        parser = preview.PreviewHTML(tmp_path / "assets")
        with pytest.raises(ValueError, match="missing or outside"):
            parser.feed(f'<img src="{source}">')


def test_explicit_downloads_payload_overrides_only_the_endpoint(monkeypatch, tmp_path):
    payload = tmp_path / "downloads.json"
    payload.write_text(json.dumps({"schemaVersion": 1, "label": "Downloads", "message": "1234", "color": "blue"}))
    override = preview.downloads_badge(payload)
    assert urlsplit(override).path == "/static/v1"
    assert parse_qs(urlsplit(override).query) == {
        "label": ["Downloads"], "message": ["1234"], "color": ["blue"], "style": ["flat-square"],
    }
    calls = []
    monkeypatch.setattr(preview, "fetch_badge", lambda url, assets: calls.append(url) or "file:///local.svg")
    parser = preview.PreviewHTML(tmp_path, override)
    parser.feed('<img alt="Downloads" src="https://img.shields.io/endpoint?url=site">'
                '<img alt="Other" src="https://img.shields.io/endpoint?url=other">'
                '<img src="https://img.shields.io/github/v/release/owner/repo?style=flat-square">')
    assert calls == [override, "https://img.shields.io/endpoint?url=other",
                     "https://img.shields.io/github/v/release/owner/repo?style=flat-square"]


@pytest.mark.parametrize("data", [[], {}, {"schemaVersion": 1, "label": "Downloads", "message": 1234, "color": "blue"}])
def test_invalid_local_payload_is_refused(tmp_path, data):
    payload = tmp_path / "downloads.json"
    payload.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="Shields v1"):
        preview.downloads_badge(payload)


def test_badge_download_must_be_svg(monkeypatch, tmp_path):
    url = "https://img.shields.io/github/license/owner/repo"
    monkeypatch.setattr(preview, "urlopen", lambda request, timeout: BytesIO(b'<html>unavailable</html>'))
    with pytest.raises(ValueError, match="other than SVG"):
        preview.fetch_badge(url, tmp_path / "assets")
    assert not (tmp_path / "assets").exists()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" aria-label="License: GPL-3.0"/>'
    monkeypatch.setattr(preview, "urlopen", lambda request, timeout: BytesIO(svg))
    result = preview.fetch_badge(url, tmp_path / "assets")
    assert Path(unquote_path(result)).read_bytes() == svg


def unquote_path(uri):
    from urllib.request import url2pathname

    return url2pathname(urlsplit(uri).path)


def test_markdown_uses_checkout_text_and_repository_context(monkeypatch, tmp_path):
    monkeypatch.setattr(preview, "ROOT", tmp_path)
    (tmp_path / "README.md").write_text("# New README\n", encoding="utf-8")
    monkeypatch.setattr(preview.shutil, "which", lambda name, path: "/bin/gh")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "owner/repo\n" if len(calls) == 1 else "<h1>New README</h1>")

    monkeypatch.setattr(preview.subprocess, "run", run)
    assert preview.github_markdown() == "<h1>New README</h1>"
    assert calls[0][0] == ["/bin/gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]
    assert calls[1][0] == ["/bin/gh", "api", "markdown", "--method", "POST", "--input", "-"]
    assert json.loads(calls[1][1]["input"]) == {"text": "# New README\n", "mode": "gfm", "context": "owner/repo"}
    assert all(kwargs["cwd"] == tmp_path and kwargs["check"] and kwargs["timeout"] == 30 for _, kwargs in calls)


@pytest.mark.parametrize("outcome", ["success", "timeout", "bad-png", "failed", "unloaded-images", "connect-timeout"])
def test_browser_is_bounded_and_cleaned_up_without_reusing_a_stale_png(monkeypatch, tmp_path, outcome):
    page = tmp_path / "readme.html"
    page.write_text("<h1>README</h1>")
    output = tmp_path / "readme.png"
    output.write_bytes(b"\x89PNG\r\n\x1a\nstale")
    monkeypatch.setattr(preview.browser_harness, "current_pin", lambda: "pin")
    monkeypatch.setattr(preview.browser_harness, "ensure_browser", lambda pin: Path("/pinned/chrome"))
    stopped = []
    commands = []
    methods = []
    closed = []

    class Browser:
        def __init__(self, command, **kwargs):
            commands.append(command)
            self.profile = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--user-data-dir=")))
            assert self.profile.is_dir()
            assert not output.exists()

    class Connection:
        def send(self, method, params):
            methods.append(method)
            return method

        def await_reply(self, request, deadline):
            assert 0 < deadline - preview.time.monotonic() <= 30
            if outcome == "timeout":
                raise TimeoutError("capture timed out")
            if outcome == "failed":
                return {"error": {"message": "capture failed"}}
            if request == "Runtime.evaluate":
                return {"result": {"result": {"value": outcome != "unloaded-images"}}}
            if request == "Page.captureScreenshot":
                png = b"not a PNG" if outcome == "bad-png" else b"\x89PNG\r\n\x1a\nfixture"
                return {"result": {"data": base64.b64encode(png).decode("ascii")}}
            return {"result": {}}

        def close(self):
            closed.append(True)

    def connect(profile, page, process, deadline):
        if outcome == "connect-timeout":
            raise TimeoutError("Chrome page did not appear")
        return Connection()

    monkeypatch.setattr(preview.subprocess, "Popen", Browser)
    monkeypatch.setattr(preview, "connect_page", connect)
    monkeypatch.setattr(preview, "_stop_browser", stopped.append)
    if outcome == "success":
        preview.screenshot(page, output)
    else:
        with pytest.raises((ValueError, TimeoutError)):
            preview.screenshot(page, output)
    assert len(stopped) == 1
    assert not stopped[0].profile.exists()
    assert commands[0][0] == "/pinned/chrome"
    assert page.as_uri() == commands[0][-1]
    assert closed == ([] if outcome == "connect-timeout" else [True])
    if outcome == "unloaded-images":
        assert "Page.captureScreenshot" not in methods


def test_connect_uses_the_owned_profile_port_and_exact_page(monkeypatch, tmp_path):
    page = tmp_path / "readme.html"
    (tmp_path / "DevToolsActivePort").write_text("43210\n/devtools/browser/owned\n")
    targets = [{"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://wrong"},
               {"type": "page", "url": page.as_uri(), "webSocketDebuggerUrl": "ws://127.0.0.1:43210/owned"}]
    calls = []

    def urlopen(url, timeout):
        calls.append((url, timeout))
        return BytesIO(json.dumps(targets).encode())

    monkeypatch.setattr(preview, "urlopen", urlopen)
    monkeypatch.setattr(preview, "DevToolsSocket", lambda url, timeout: (url, timeout))
    connection = preview.connect_page(tmp_path, page, SimpleNamespace(poll=lambda: None), preview.time.monotonic() + 30)
    assert connection == ("ws://127.0.0.1:43210/owned", 1)
    assert calls == [("http://127.0.0.1:43210/json/list", 1)]


def test_missing_devtools_port_has_a_deadline_and_not_an_endless_wait(monkeypatch, tmp_path):
    ticks = iter(range(5))
    monkeypatch.setattr(preview.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(preview.time, "sleep", lambda duration: None)
    with pytest.raises(TimeoutError, match="did not become ready"):
        preview.connect_page(tmp_path, tmp_path / "readme.html", SimpleNamespace(poll=lambda: None), 3)


def test_a_browser_that_exited_is_reported_without_waiting_for_the_deadline(tmp_path):
    with pytest.raises(ValueError, match="Chrome exited"):
        preview.connect_page(tmp_path, tmp_path / "readme.html", SimpleNamespace(poll=lambda: 1), preview.time.monotonic() + 30)


@pytest.mark.parametrize("error", [subprocess.CalledProcessError(1, ["gh", "api"], stderr="API unavailable"),
                                   RuntimeError("unsupported browser platform")])
def test_early_failure_is_reported_and_removes_an_old_preview(monkeypatch, tmp_path, capsys, error):
    def fail():
        raise error

    (tmp_path / "readme.png").write_bytes(b"old preview")
    monkeypatch.setattr(preview, "github_markdown", fail)
    assert preview.main(["--output-dir", str(tmp_path)]) == 1
    diagnostic = capsys.readouterr().out
    assert "FAILED" in diagnostic
    assert (error.stderr if isinstance(error, subprocess.CalledProcessError) else str(error)) in diagnostic
    assert not (tmp_path / "readme.png").exists()


def test_output_error_is_not_masked_by_cleanup(monkeypatch, tmp_path, capsys):
    destination = tmp_path / "occupied"
    destination.write_text("existing file")
    assert preview.main(["--output-dir", str(destination)]) == 1
    assert "FAILED" in capsys.readouterr().out
    assert destination.read_text() == "existing file"


def test_one_command_renders_local_images_and_live_badges(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(preview, "ROOT", tmp_path)
    (tmp_path / "mascot.png").write_bytes(b"mascot")
    monkeypatch.setattr(preview, "github_markdown", lambda: '<h1>CE Decky</h1>'
                        '<img alt="Release" src="https://img.shields.io/github/v/release/owner/repo">'
                        '<img src="mascot.png">')
    monkeypatch.setattr(preview, "fetch_badge", lambda url, assets: "file:///release.svg")

    def render(page, png):
        images = Images(page.read_text(encoding="utf-8")).images
        assert images[0]["src"] == "file:///release.svg"
        assert images[1]["src"] == (tmp_path / "mascot.png").as_uri()
        png.write_bytes(b"fixture")

    monkeypatch.setattr(preview, "screenshot", render)
    destination = tmp_path / "preview"
    assert preview.main(["--output-dir", str(destination)]) == 0
    assert (destination / "readme.png").read_bytes() == b"fixture"
    assert str(destination / "readme.png") in capsys.readouterr().out
