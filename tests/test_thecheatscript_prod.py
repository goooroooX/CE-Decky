from __future__ import annotations

import asyncio
from pathlib import Path
from hashlib import sha256
import json
import types
from dataclasses import replace

import pytest

from ce_decky.acquisition import AcquisitionManager
from ce_decky.catalog import CatalogService, collect_parse_issues, thecheatscript_record
from ce_decky.network import NetworkError
from ce_decky.table_store import TableStore
from ce_decky.thecheatscript import (
    PostRef,
    TheCheatScriptDownloadRequest,
    artifact_id_for,
    parse_download_href,
    parse_post,
    parse_sitemap_index,
    is_post_url,
    parse_sitemap_page,
    parse_storage_redirect,
    parse_version_tokens,
    parse_yandex_public,
    is_probably_resolvable,
    rank_posts,
    YANDEX_API_HOSTS,
    YANDEX_ARTIFACT_HOSTS,
)


SITEMAP_INDEX = b'''<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.thecheatscript.com/sitemap.xml?page=1</loc></sitemap>
</sitemapindex>'''

SITEMAP_PAGE = b'''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.thecheatscript.com/2026/08/sort-them-ducks.html</loc><lastmod>2026-09-01T15:21:30Z</lastmod></url>
  <url><loc>https://www.thecheatscript.com/2022/01/sort-them-ducks-old.html</loc><lastmod>2022-01-01T00:00:00Z</lastmod></url>
</urlset>'''

POST_HTML = b'''<html><body>
  <a href="https://disk.yandex.com/d/V9hKfs2cE9WpBQ">Sort Them Ducks b20260816 | Table v0.9</a>
  <a href="https://disk.yandex.com/d/V9hKfs2cE9WpBQ">same public resource</a>
</body></html>'''

PUBLIC_METADATA = {
    "name": "Sort Them Ducks b20260816_Table v1.0_The Cheat Script.ct",
    "type": "file",
    "size": 21319,
    "sha256": "daf3e1142f6276065c7c6c544331dbc69a70df332ff94f813545b20c6f9e01ca",
}

CT = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>'
STORAGE_URL = "https://s668vla.storage.yandex.net/get/table?signature=transient"


class _Response:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class _ProviderNetwork:
    def __init__(self, *, metadata: dict[str, object] | None = None, artifact: bytes = CT) -> None:
        self.calls: list[str] = []
        self.metadata = metadata or PUBLIC_METADATA
        self.artifact = artifact
        self.download_calls: list[tuple[str, frozenset[str], str | None]] = []

    async def get(self, url: str, **_kwargs):
        self.calls.append(url)
        if url == "https://www.thecheatscript.com/sitemap.xml":
            return _Response(200, SITEMAP_INDEX)
        if url == "https://www.thecheatscript.com/sitemap.xml?page=1":
            return _Response(200, SITEMAP_PAGE)
        if url == "https://www.thecheatscript.com/2026/08/sort-them-ducks.html":
            return _Response(200, POST_HTML)
        if url.startswith("https://cloud-api.yandex.net/v1/disk/public/resources/download?"):
            return _Response(200, b'{"href":"https://downloader.disk.yandex.ru/disk/table?token=transient"}')
        if url.startswith("https://downloader.disk.yandex.ru/"):
            return _Response(302, headers={"location": STORAGE_URL})
        if url.startswith("https://cloud-api.yandex.net/v1/disk/public/resources?"):
            return _Response(200, json.dumps(self.metadata).encode())
        raise AssertionError(f"unexpected URL: {url}")

    async def download(
        self,
        url: str,
        destination,
        *,
        allowed_hosts,
        referer=None,
        **_kwargs,
    ):
        self.download_calls.append((url, allowed_hosts, referer))
        destination.write_bytes(self.artifact)

    async def aclose(self) -> None:
        return None


def test_declared_sitemap_yields_bounded_post_titles():
    assert parse_sitemap_index(SITEMAP_INDEX) == (
        "https://www.thecheatscript.com/sitemap.xml?page=1",
    )
    refs = parse_sitemap_page(SITEMAP_PAGE)
    assert [ref.title for ref in refs] == ["Sort Them Ducks", "Sort Them Ducks Old"]
    ranked = rank_posts(refs, lambda title: 0.9 if title == "Sort Them Ducks" else 0.8)
    assert [(ref.title, score) for ref, score in ranked] == [("Sort Them Ducks", 0.9)]


def test_sitemap_xml_refuses_dtd_entities_and_malformed_input():
    unsafe = b'<!DOCTYPE x [<!ENTITY y "expanded">]><urlset><url>&y;</url></urlset>'
    for payload in (unsafe, b"<urlset><url>"):
        with pytest.raises(ValueError, match="not safe XML"):
            parse_sitemap_page(payload)


def test_post_identity_and_yandex_metadata_keep_build_separate_from_table_version():
    artifacts = parse_post(POST_HTML.decode())
    assert len(artifacts) == 1
    assert artifacts[0].supported is True
    assert artifacts[0].game_build == "20260816"
    assert artifacts[0].table_version == "0.9"
    public = parse_yandex_public(PUBLIC_METADATA)
    assert public.version_tokens == ("20260816", "1.0")
    assert public.advertised_sha256 == PUBLIC_METADATA["sha256"]
    assert parse_version_tokens("Survival Log v1.0.14331_Table v1.0.ct") == ("1.0.14331", "1.0")


def test_yandex_download_link_is_fail_closed_to_the_declared_host_policy():
    good = "https://downloader.disk.yandex.ru/disk/table?token=transient"
    assert parse_download_href({"href": good}) == good
    for href in (
        "http://downloader.disk.yandex.ru/table",
        "https://evil.invalid/table",
        "https://cloud-api.yandex.net/table",
        "https://www.thecheatscript.com/table",
        "https://user:password@downloader.disk.yandex.ru/table",
        "https://downloader.disk.yandex.ru:444/table",
        "https://downloader.disk.yandex.ru/table#secret",
    ):
        with pytest.raises(NetworkError):
            parse_download_href({"href": href})
    assert YANDEX_API_HOSTS == frozenset({"cloud-api.yandex.net"})
    assert YANDEX_ARTIFACT_HOSTS == frozenset({"downloader.disk.yandex.ru"})


def test_yandex_storage_redirect_becomes_one_exact_runtime_host():
    url, hosts = parse_storage_redirect(
        "https://downloader.disk.yandex.ru/disk/table?token=transient",
        STORAGE_URL,
    )
    assert url == STORAGE_URL
    assert hosts == frozenset({"s668vla.storage.yandex.net"})
    for location in (
        "https://storage.yandex.net/table",
        "https://s668vla.storage.yandex.net.evil.invalid/table",
        "https://user@sa.storage.yandex.net/table",
        "http://sa.storage.yandex.net/table",
    ):
        with pytest.raises(NetworkError):
            parse_storage_redirect(
                "https://downloader.disk.yandex.ru/disk/table",
                location,
            )


def test_bad_described_metadata_does_not_drop_obtainable_bytes():
    ref = PostRef(
        "https://www.thecheatscript.com/2026/08/sort-them-ducks.html",
        "Sort Them Ducks",
        "not-a-date",
    )
    artifact = parse_post(POST_HTML.decode())[0]
    with collect_parse_issues() as issues:
        record = thecheatscript_record(ref, artifact, PUBLIC_METADATA, 0.9)
    assert record is not None
    assert record.result.filename.endswith(".ct")
    assert record.result.posted_at is None
    assert record.result.version == "1.0"
    assert record.result.size_bytes == 21319
    assert record.advertised_sha256 == PUBLIC_METADATA["sha256"]
    assert issues.degraded == 1


@pytest.mark.asyncio
async def test_search_and_deferred_resolution_use_the_production_contract(tmp_path):
    network = _ProviderNetwork()
    service = CatalogService(network, tmp_path / "catalog.json")  # type: ignore[arg-type]

    async def empty(self, *_args):
        return []

    service._fearless = types.MethodType(empty, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]
    outcome = await service.search("Sort Them Ducks")

    rows = [row for row in outcome["results"] if row["provider"] == "thecheatscript"]
    assert len(rows) == 1
    row = rows[0]
    assert row["artifact_id"] == artifact_id_for(
        "https://www.thecheatscript.com/2026/08/sort-them-ducks.html",
        "https://disk.yandex.com/d/V9hKfs2cE9WpBQ",
    )
    assert row["filename"].endswith(".ct")
    assert row["version"] == "1.0"
    assert row["size_bytes"] == 21319
    assert row["advertised_sha256"] == PUBLIC_METADATA["sha256"]
    assert next(source for source in outcome["sources"] if source["provider"] == "thecheatscript")["status"] == "ok"

    record = service.resolve("thecheatscript", row["artifact_id"], row["search_id"])
    assert isinstance(record.acquisition, TheCheatScriptDownloadRequest)
    url, hosts = await service.resolve_download(record)
    assert url == STORAGE_URL
    assert hosts == frozenset({"s668vla.storage.yandex.net"})
    assert "disk.yandex.com" not in hosts
    assert all("token=transient" not in cached.read_text(encoding="utf-8") for cached in tmp_path.glob("*.json"))
    assert all("V9hKfs2cE9WpBQ" not in cached.read_text(encoding="utf-8") for cached in tmp_path.glob("*.json"))

    await service._thecheatscript(["sort them ducks"])
    assert network.calls.count("https://www.thecheatscript.com/sitemap.xml") == 1

    wrong = replace(record, result=replace(record.result, provider="fearless"))
    with pytest.raises(ValueError, match="no The Cheat Script"):
        await record.acquisition.resolve(network, wrong, sleep=asyncio.sleep)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_acquisition_downloads_checks_and_imports_the_exact_yandex_bytes(tmp_path):
    metadata = {
        "name": "Sort Them Ducks_Table v1.0_The Cheat Script.ct",
        "type": "file",
        "size": len(CT),
        "sha256": sha256(CT).hexdigest(),
    }
    network = _ProviderNetwork(metadata=metadata)
    service = CatalogService(network, tmp_path / "catalog.json")  # type: ignore[arg-type]

    async def empty(self, *_args):
        return []

    service._fearless = types.MethodType(empty, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]
    outcome = await service.search("Sort Them Ducks")
    row = next(item for item in outcome["results"] if item["provider"] == "thecheatscript")
    home = tmp_path / "home"
    manager = AcquisitionManager(
        service,
        network,  # type: ignore[arg-type]
        TableStore(tmp_path / "tables"),
        tmp_path / "staging",
        home / "Downloads",
        home,
        lambda: None,
    )
    try:
        started = await manager.start("thecheatscript", row["artifact_id"], row["search_id"])
        acquisition_id = str(started["acquisition_id"])
        await manager.wait_ready(acquisition_id)
        assert manager.poll(acquisition_id)["state"] == "ready_to_import"
        completed = await manager.complete(acquisition_id)
        assert completed["state"] == "imported"
        assert completed["imported"]["sha256"] == metadata["sha256"]
        assert network.download_calls == [
            (
                STORAGE_URL,
                frozenset({"s668vla.storage.yandex.net"}),
                None,
            )
        ]
    finally:
        await manager.close()
        await service.close()


def test_public_resource_identity_is_required_but_digest_description_is_best_effort():
    with pytest.raises(ValueError):
        parse_yandex_public({"type": "dir", "name": "tables", "size": 10})
    with pytest.raises(ValueError):
        parse_yandex_public({"type": "file", "name": "readme.txt", "size": 10})
    with pytest.raises(ValueError):
        parse_yandex_public({"type": "file", "name": "folder/table.ct", "size": 10})
    artifact = parse_yandex_public({"type": "file", "name": "table.ct", "size": 10, "sha256": "bad"})
    assert artifact.advertised_sha256 is None


def test_sitemap_cache_rejects_future_and_foreign_entries(tmp_path, monkeypatch):
    service = CatalogService(_ProviderNetwork(), tmp_path / "catalog.json")  # type: ignore[arg-type]
    cache = tmp_path / "thecheatscript-sitemap.json"
    monkeypatch.setattr("ce_decky.catalog.time.time", lambda: 100.0)
    cache.write_text(json.dumps({
        "schema": 1,
        "saved": 101.0,
        "entries": [["https://www.thecheatscript.com/2026/08/sort-them-ducks.html", None]],
    }))
    assert service._load_thecheatscript_sitemap_cache() is None
    cache.write_text(json.dumps({
        "schema": 1,
        "saved": 100.0,
        "entries": [["https://cloud-api.yandex.net/2026/08/sort-them-ducks.html", None]],
    }))
    assert service._load_thecheatscript_sitemap_cache() is None


def test_an_absent_or_unreadable_post_date_is_not_an_availability_test():
    """`lastmod` is described metadata, not a statement about the artifact.

    The known-old optimization is worth keeping: posts from before the
    provider's current host era resolve to nothing, and fetching them spends
    requests to find that out. But an optional sitemap field became a hard
    discovery gate, so a post that declared no date, or one this could not read,
    was dropped before its page could say what it actually hosts.
    """
    current = "https://www.thecheatscript.com/2026/08/sort-them-ducks.html"
    legacy = "https://www.thecheatscript.com/2019/01/sort-them-ducks.html"

    assert is_probably_resolvable(PostRef(current, "T", "2026-09-01T15:21:30Z"))
    # The year comes from the post path the site itself assigns when the field
    # is missing or unreadable, rather than the post being refused for what it
    # did not say.
    assert is_probably_resolvable(PostRef(current, "T", None))
    assert is_probably_resolvable(PostRef(current, "T", "not-a-date"))
    # The optimization still holds where the path does say the post is old.
    assert not is_probably_resolvable(PostRef(legacy, "T", None))
    # There are two years here and neither withholds a post by disagreeing with
    # the other. A current post carrying a stale or unreadable date stays
    # eligible on its path, which is the case that mattered: optional
    # description was overriding the route's own identity and dropping a post
    # the site itself files under this year.
    assert is_probably_resolvable(PostRef(current, "T", "2019-01-01T00:00:00Z"))
    # And a post whose path is old while its own date says it was touched this
    # year stays eligible on that date, because a post can be updated where it
    # was first published and refusing it is the same error in the other
    # direction.
    assert is_probably_resolvable(PostRef(legacy, "T", "2026-01-01T00:00:00Z"))
    # A post is excluded only when everything known about it says it is old.
    assert not is_probably_resolvable(PostRef(legacy, "T", "2019-01-01T00:00:00Z"))


def test_a_post_with_no_readable_date_still_reaches_ranking():
    # The existing "bad described metadata" coverage builds records directly, so
    # it passed while a real search dropped the row before it was ever fetched.
    refs = (
        PostRef("https://www.thecheatscript.com/2026/08/sort-them-ducks.html", "Sort Them Ducks", None),
        PostRef("https://www.thecheatscript.com/2026/08/other-game.html", "Other Game", "bad"),
    )
    ranked = rank_posts(refs, lambda title: 0.9 if "Ducks" in title else 0.8)
    assert [ref.title for ref, _ in ranked] == ["Sort Them Ducks", "Other Game"]


@pytest.mark.asyncio
async def test_a_sitemap_crawl_that_lost_a_page_is_used_but_not_cached(tmp_path):
    """A partial crawl answers this search and is not written down as the index.

    Cached as complete, one page failing once would take every post it declared
    out of every search for the next six hours, and nothing would say so.
    """
    index = b'''<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://www.thecheatscript.com/sitemap.xml?page=1</loc></sitemap>
  <sitemap><loc>https://www.thecheatscript.com/sitemap.xml?page=2</loc></sitemap>
</sitemapindex>'''

    class _Network:
        def __init__(self, second):
            self.second = second
            self.calls: list[str] = []

        async def get(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith("/sitemap.xml"):
                return _Response(200, index)
            if "page=1" in url:
                return _Response(200, SITEMAP_PAGE)
            if "page=2" in url:
                return self.second
            if url.endswith(".html"):
                return _Response(200, POST_HTML)
            return _Response(200, json.dumps(PUBLIC_METADATA).encode())

    cache = tmp_path / "results.json"
    network = _Network(_Response(503))
    service = CatalogService(network, cache)  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        rows = await service._thecheatscript(["sort them ducks"])
    assert rows and issues.failed == 1
    assert not cache.with_name("thecheatscript-sitemap.json").exists()

    # A complete crawl is what gets written down.
    healthy = _Network(_Response(200, SITEMAP_PAGE))
    service = CatalogService(healthy, cache)  # type: ignore[arg-type]
    assert await service._thecheatscript(["sort them ducks"])
    assert cache.with_name("thecheatscript-sitemap.json").is_file()


FIXTURES = Path(__file__).parent / "fixtures"


def test_the_real_sitemap_and_post_shapes_this_adapter_was_written_against():
    """Captured markup, not a string this test wrote to suit its own parser.

    A hand-built input proves the parser against the shape the test author had
    in mind. These three are what the site actually served when the adapter was
    written, so a rewrite that still passes the hand-built cases and stops
    reading the real page fails here instead of on a device.
    """
    pages = parse_sitemap_index(
        (FIXTURES / "thecheatscript_sitemap_index.xml").read_bytes()
    )
    assert pages == (
        "https://www.thecheatscript.com/sitemap.xml?page=1",
        "https://www.thecheatscript.com/sitemap.xml?page=2",
        "https://www.thecheatscript.com/sitemap.xml?page=3",
    )

    refs = parse_sitemap_page(
        (FIXTURES / "thecheatscript_sitemap_page.xml").read_bytes()
    )
    assert len(refs) == 6
    # The slug is the only title this source publishes, and the date is what
    # excludes posts from before the publisher moved to its current host.
    assert refs[0].url == "https://www.thecheatscript.com/2026/08/tv-archive-tidy-up-together.html"
    assert refs[0].title == "Tv Archive Tidy Up Together"
    assert refs[0].lastmod == "2026-09-02T04:34:46Z"
    assert all(is_post_url(ref.url) for ref in refs)

    artifacts = parse_post(
        (FIXTURES / "thecheatscript_post_yandex.html").read_text(encoding="utf-8")
    )
    # One supported mirror, read out of the markup around it rather than guessed.
    assert len(artifacts) == 1
    assert artifacts[0].link == "https://disk.yandex.com/d/V9hKfs2cE9WpBQ"
    assert artifacts[0].supported is True
    assert artifacts[0].table_version == "1.0"
