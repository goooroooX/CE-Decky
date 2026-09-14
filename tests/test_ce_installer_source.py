"""Regressions for rediscovering the installer URL when the pinned one dies.

This path reaches out to a page CE Decky does not control and parses a binary
another company signed, so ambiguity has to be a refusal at every step: one
download link, one installer URL, one host allowlist derived from what the
pinned page actually said.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from ce_decky import inno_reader
from ce_decky.ce_installer_source import (
    FallbackPolicy,
    find_stub_url,
    resolve_from_stub,
    resolve_installer,
    safe_installer_url,
)
from ce_decky.network import NetworkResponse

POLICY = FallbackPolicy(
    page_url="https://www.cheatengine.org/downloads.php",
    page_hosts=("www.cheatengine.org", "cheatengine.org"),
    max_page_bytes=1024 * 1024,
    max_stub_bytes=32 * 1024 * 1024,
)
INSTALLER = "https://cdn.example/f/CheatEngine/2129/CheatEngine77.exe"
STUB = "https://helper.example/Gl9OXLQsE.exe"


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _stub_bytes(*urls: str) -> bytes:
    """A minimal Inno installer whose metadata holds `urls` as UTF-16 text."""
    # Inno never writes an empty chunk - its own reader requires at least one
    # byte after each chunk CRC - so a stub with no URLs still carries text.
    payload = ("\x00".join(urls) or "no download links here").encode("utf-16le")
    framed = struct.pack("<I", _crc(payload)) + payload
    header = struct.pack("<qB", len(framed), 0)
    block = struct.pack("<I", _crc(header)) + header + framed

    prefix = b"MZ" + b"\x00" * 62
    setup0 = b"Inno Setup Setup Data (6.7.0)".ljust(inno_reader.SETUP_ID_SIZE, b"\x00") + block
    header_offset = len(prefix) + 64
    fields = struct.pack(
        "<IqqIiqqI", 2, 0, 0, 0, 0, header_offset, header_offset + len(setup0), 0,
    )
    table = inno_reader.LOADER_TABLE_ID + fields
    table += struct.pack("<I", _crc(table))
    return prefix + table + setup0


class _FakeNetwork:
    def __init__(self, page: str, stub: bytes) -> None:
        self.page = page
        self.stub = stub
        self.gets: list[tuple[str, frozenset[str], int]] = []
        self.downloads: list[tuple[str, frozenset[str], int]] = []

    async def get(self, url: str, *, allowed_hosts, max_bytes, **_kwargs):
        self.gets.append((url, allowed_hosts, max_bytes))
        return NetworkResponse(url, 200, {}, self.page.encode("utf-8"))

    async def download(self, url: str, destination: Path, *, allowed_hosts, max_bytes, **_kwargs):
        self.downloads.append((url, allowed_hosts, max_bytes))
        destination.write_bytes(self.stub)


def _page(*hrefs: str) -> str:
    links = "".join(f'<a href="{href}">download</a>' for href in hrefs)
    return f"<html><body>{links}</body></html>"


@pytest.mark.parametrize("url", [
    "http://cdn.example/CheatEngine77.exe",
    "https://user:secret@cdn.example/CheatEngine77.exe",
    "https://cdn.example/CheatEngine77.exe?token=1",
    "https://cdn.example/CheatEngine77.exe#fragment",
    "https://cdn.example:8443/CheatEngine77.exe",
    "https://cdn.example/CheatEngine77.zip",
    "https://cdn.example/",
    "not a url",
])
def test_only_a_plain_https_executable_url_is_accepted(url: str):
    with pytest.raises(ValueError):
        safe_installer_url(url)


def test_a_plain_https_executable_url_is_accepted():
    assert safe_installer_url(INSTALLER) == INSTALLER


def test_the_one_off_site_download_link_is_the_stub():
    # The page's own links point at translations and non-Windows builds; only
    # the Windows button leaves the site.
    page = _page(
        "/download/ru_RU.rar",
        "https://cheatengine.org/download/CheatEngineLinux771.zip",
        "https://www.cheatengine.org/local.exe",
        STUB,
    )
    assert find_stub_url(page, POLICY) == STUB


@pytest.mark.parametrize("hrefs, count", [
    ((), 0),
    (("/download/ru_RU.rar",), 0),
    ((STUB, "https://other.example/Another.exe"), 2),
])
def test_an_ambiguous_download_page_is_refused(hrefs: tuple[str, ...], count: int):
    with pytest.raises(ValueError, match=f"offers {count} download links"):
        find_stub_url(_page(*hrefs), POLICY)


def test_a_relative_download_link_resolves_against_the_page():
    # A same-site relative link is the page's own, never the off-site button.
    with pytest.raises(ValueError, match="offers 0 download links"):
        find_stub_url(_page("setup.exe"), POLICY)


def test_relative_links_resolve_against_the_url_the_page_was_served_from():
    # `get` follows redirects inside the pinned host allowlist, so resolving
    # against the requested path would build links from a page that moved.
    page = _page("../cdn/helper.exe")
    served = "https://www.cheatengine.org/pages/downloads.php"
    # Against the served URL the link stays on-site and is correctly ignored.
    with pytest.raises(ValueError, match="offers 0 download links"):
        find_stub_url(page, POLICY, served)


def test_a_redirected_page_is_resolved_against_where_it_actually_came_from(tmp_path: Path):
    import asyncio

    class _Redirected(_FakeNetwork):
        async def get(self, url: str, *, allowed_hosts, max_bytes, **_kwargs):
            self.gets.append((url, allowed_hosts, max_bytes))
            return NetworkResponse(
                "https://www.cheatengine.org/pages/downloads.php", 200, {},
                self.page.encode("utf-8"),
            )

    network = _Redirected(_page(STUB), _stub_bytes(INSTALLER))
    assert asyncio.run(resolve_installer(network, POLICY, tmp_path)).stub_host == "helper.example"


def test_the_installer_url_is_read_out_of_the_stub():
    url, setup_id = resolve_from_stub(_stub_bytes(INSTALLER, "https://ads.example/zbd"))
    assert url == INSTALLER
    assert setup_id == "Inno Setup Setup Data (6.7.0)"


@pytest.mark.parametrize("urls, count", [
    ((), 0),
    (("https://ads.example/offer.exe",), 0),
    ((INSTALLER, "https://cdn2.example/f/CheatEngine/3000/CheatEngine78.exe"), 2),
])
def test_a_stub_naming_anything_but_one_installer_is_refused(urls: tuple[str, ...], count: int):
    with pytest.raises(ValueError, match=f"names {count} Cheat Engine installers"):
        resolve_from_stub(_stub_bytes(*urls))


def test_resolution_derives_each_host_allowlist_from_what_it_was_told(tmp_path: Path):
    import asyncio

    network = _FakeNetwork(_page(STUB), _stub_bytes(INSTALLER))
    resolved = asyncio.run(resolve_installer(network, POLICY, tmp_path))

    assert resolved.url == INSTALLER
    assert resolved.host == "cdn.example"
    assert resolved.filename == "CheatEngine77.exe"
    assert resolved.stub_host == "helper.example"
    # The page is fetched from the pinned hosts, and the helper only from the
    # host that pinned page actually named.
    assert network.gets == [(POLICY.page_url, frozenset(POLICY.page_hosts), POLICY.max_page_bytes)]
    assert network.downloads == [(STUB, frozenset({"helper.example"}), POLICY.max_stub_bytes)]


def test_recorded_evidence_never_carries_the_download_route(tmp_path: Path):
    import asyncio

    network = _FakeNetwork(_page(STUB), _stub_bytes(INSTALLER))
    evidence = asyncio.run(resolve_installer(network, POLICY, tmp_path)).evidence()
    assert "url" not in evidence
    assert INSTALLER not in str(evidence)
    assert evidence == {
        "host": "cdn.example",
        "filename": "CheatEngine77.exe",
        "stub_host": "helper.example",
        "setup_id": "Inno Setup Setup Data (6.7.0)",
    }


def test_an_unavailable_download_page_is_refused(tmp_path: Path):
    import asyncio

    class _Missing(_FakeNetwork):
        async def get(self, url: str, *, allowed_hosts, max_bytes, **_kwargs):
            return NetworkResponse(url, 404, {}, b"")

    network = _Missing(_page(STUB), _stub_bytes(INSTALLER))
    with pytest.raises(ValueError, match="download page is unavailable"):
        asyncio.run(resolve_installer(network, POLICY, tmp_path))
