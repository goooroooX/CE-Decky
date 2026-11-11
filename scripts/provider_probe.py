#!/usr/bin/env python3
"""Bounded live diagnostics for CE Decky's production provider transport."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))
# The runtime dependencies the plugin ships, as `main.py` adds them, so a
# plain `python3` runs this; appended, so an installed copy still wins.
sys.path.append(str(ROOT / "py_modules" / "vendor"))

from ce_decky.catalog import (  # noqa: E402
    MAX_HTML_BYTES,
    MAX_SITEMAP_BYTES,
    PLAYGROUND_ARTIFACT_HOSTS,
    PROVIDER_HOSTS,
    ForumListing,
    PlaygroundDownloadRequest,
    decode_html,
    FEARLESS_BASE_URL,
    FEARLESS_FORUM_URL,
    parse_fearless_forum_page,
    parse_playground_page,
    resolve_playground_download,
)
from ce_decky.github_discovery import (  # noqa: E402
    build_search_queries as github_search_queries,
    is_rate_limited as github_is_rate_limited,
    parse_repository_search as parse_github_repository_search,
    rank_repositories as rank_github_repositories,
    search_url as github_search_url,
)
from ce_decky.thecheatscript import parse_sitemap_index as parse_thecheatscript_sitemap_index  # noqa: E402
from ce_decky.vgtimes import (  # noqa: E402
    parse_tables_listing as parse_vgtimes_tables_listing,
    tables_listing_url as vgtimes_tables_listing_url,
)

# One game known to publish tables, used only to prove the listing route answers
# and still parses into entries. Nothing is downloaded.
VGTIMES_PROBE_SLUG = "atomic-heart"

# A game whose tables live on GitHub, used only to prove the route answers and
# that its answer still parses into ranked candidates.
GITHUB_PROBE_QUERY = github_search_queries(["elden ring"])[0]
from ce_decky.network import NetworkClient  # noqa: E402
from ce_decky.network_tls import build_verified_ssl_context  # noqa: E402


@dataclass(frozen=True)
class Route:
    key: str
    provider: str
    url: str
    referer: str


ROUTES = (
    # FearLess is reached through its own table-forum listing rather than its
    # search route, which challenges anonymous HTTP, so the listing and one
    # topic are what this checks.
    Route("fearless-forum", "fearless", f"{FEARLESS_FORUM_URL}", FEARLESS_BASE_URL),
    Route("playground-item", "playground", "https://www.playground.ru/dome_keeper/cheat/dome_keeper_tablitsa_dlya_cheat_engine_upd_28_09_2022_cfemen-1236756", "https://www.playground.ru/"),
    Route("thecheatscript-sitemap", "thecheatscript", "https://www.thecheatscript.com/sitemap.xml", "https://www.thecheatscript.com/"),
    # The repository index is the only discovery route an anonymous client has:
    # code search answers 401, so a table cannot be found by file extension.
    # This spends one of ten anonymous searches a minute and downloads nothing.
    Route("github-search", "github", github_search_url(GITHUB_PROBE_QUERY, per_page=5), "https://github.com/"),
    # The game is its own path segment here and tables have their own listing,
    # so one listing is a table-only answer and nothing else has to be opened.
    Route("vgtimes-tables", "vgtimes", vgtimes_tables_listing_url(VGTIMES_PROBE_SLUG), "https://vgtimes.ru/"),
)

PLAYGROUND_ITEM = next(route.url for route in ROUTES if route.key == "playground-item")


def fearless_followup_routes(listing: ForumListing) -> tuple[Route, ...]:
    """Routes the listing itself authorizes without assuming its page size."""
    routes: list[Route] = []
    if listing.total_pages > 1:
        if listing.page_step is None:
            raise ValueError("FearLess listing has no unambiguous page size")
        routes.append(Route(
            "fearless-forum-page", "fearless",
            f"{FEARLESS_FORUM_URL}&start={listing.page_step}", FEARLESS_FORUM_URL,
        ))
    if listing.rows:
        topic_id = listing.rows[0][0]
        routes.append(Route(
            "fearless-topic", "fearless",
            f"{FEARLESS_BASE_URL}viewtopic.php?f=4&t={topic_id}", FEARLESS_FORUM_URL,
        ))
    return tuple(routes)


async def probe_routes() -> int:
    context, _ = build_verified_ssl_context()
    network = NetworkClient(context, user_agent="CE-Decky-direct-probe/0.4")
    failures = 0
    try:
        routes = list(ROUTES)
        index = 0
        while index < len(routes):
            route = routes[index]
            index += 1
            try:
                response = await network.get(
                    route.url,
                    allowed_hosts=PROVIDER_HOSTS[route.provider],
                    max_bytes=MAX_SITEMAP_BYTES if route.key == "thecheatscript-sitemap" else MAX_HTML_BYTES,
                    referer=route.referer,
                )
                detail = ""
                if route.key.startswith("fearless-forum") and response.status == 200:
                    listing = parse_fearless_forum_page(decode_html(response.body))
                    detail = (
                        f" topics={len(listing.rows)} pages={listing.total_pages}"
                        f" listed={listing.total_topics} page_step={listing.page_step}"
                    )
                    if route.key == "fearless-forum":
                        routes[index:index] = fearless_followup_routes(listing)
                if route.key == "playground-item" and response.status == 200:
                    html = decode_html(response.body)
                    file_nodes = len(re.findall(r"<pg-file\b", html, re.I))
                    detail = f" pg-file={file_nodes}"
                if route.key == "thecheatscript-sitemap" and response.status == 200:
                    detail = f" pages={len(parse_thecheatscript_sitemap_index(response.body))}"
                if route.key == "vgtimes-tables" and response.status == 200:
                    entries = parse_vgtimes_tables_listing(decode_html(response.body), VGTIMES_PROBE_SLUG)
                    detail = f" tables={len(entries)}"
                if route.key == "github-search":
                    remaining = response.headers.get("x-ratelimit-remaining", "-")
                    if github_is_rate_limited(response.status, response.headers):
                        detail = f" budget=exhausted remaining={remaining}"
                    elif response.status == 200:
                        candidates = parse_github_repository_search(json.loads(response.body))
                        ranked = rank_github_repositories(candidates, lambda title: 1.0)
                        detail = (
                            f" candidates={len(candidates)} ranked={len(ranked)}"
                            f" remaining={remaining}"
                        )
                print(
                    f"{route.key:19} status={response.status:<3} bytes={len(response.body):<8} "
                    f"type={response.headers.get('content-type', '-').split(';', 1)[0]}{detail}"
                )
                if response.status != 200:
                    failures += 1
            except Exception as exc:
                failures += 1
                print(f"{route.key:19} error={type(exc).__name__}:{str(exc)[:120]}")
    finally:
        await network.aclose()
    print(f"routes: {len(routes) - failures} reachable, {failures} non-direct")
    return 0


async def probe_playground_api() -> int:
    context, _ = build_verified_ssl_context()
    network = NetworkClient(context, user_agent="CE-Decky-direct-probe/0.4")
    try:
        page = await network.get(
            PLAYGROUND_ITEM,
            allowed_hosts=PROVIDER_HOSTS["playground"],
            max_bytes=MAX_HTML_BYTES,
            referer="https://www.playground.ru/",
        )
        if page.status != 200:
            print(f"playground-direct page_status={page.status} result=failed")
            return 1
        records, _ = parse_playground_page(decode_html(page.body), PLAYGROUND_ITEM, 1.0)
        if not records or not isinstance(records[0].acquisition, PlaygroundDownloadRequest):
            raise ValueError("Playground item has no production direct-download identity")
        record = records[0]

        async def countdown(seconds: float) -> object:
            print(
                f"playground-direct phase=countdown wait={int(seconds)}s "
                f"file={record.result.filename} sha256={'yes' if record.advertised_sha256 else 'no'}",
                flush=True,
            )
            await asyncio.sleep(seconds)
            return None

        download_link = await resolve_playground_download(network, record, sleep=countdown)
        filename = record.result.filename
        digest = record.advertised_sha256
        parts = urlsplit(download_link)
        with tempfile.TemporaryDirectory(prefix="ce-decky-provider-probe-") as directory:
            destination = Path(directory) / filename
            downloaded = await network.download(
                download_link,
                destination,
                allowed_hosts=PLAYGROUND_ARTIFACT_HOSTS,
                max_bytes=64 * 1024 * 1024,
                referer=PLAYGROUND_ITEM,
            )
            actual_digest = sha256(destination.read_bytes()).hexdigest()
            digest_ok = digest is None or actual_digest == digest
            print(
                f"playground-direct result={'downloaded' if digest_ok else 'digest-mismatch'} "
                f"host={parts.hostname or '-'} final_host={urlsplit(downloaded.url).hostname or '-'} "
                f"bytes={destination.stat().st_size} sha256={'verified' if digest_ok and digest else 'unavailable'} "
                f"token=redacted"
            )
            if not digest_ok:
                return 1
        return 0
    except Exception as exc:
        print(f"playground-direct result=failed error={type(exc).__name__}:{str(exc)[:160]}")
        return 1
    finally:
        await network.aclose()


# The most listing pages one pacing measurement may read. The question it
# answers is how a few requests at one interval are met, never how many the
# forum will take: a sample larger than this is the crawl, not a measurement.
MAX_PACE_PAGES = 8
MIN_PACE_INTERVAL_S = 1.0
MAX_PACE_INTERVAL_S = 60.0


async def probe_fearless_pace(pages: int, interval: float, *, network=None, sleep=asyncio.sleep, clock=None) -> dict:
    """Read the first `pages` FearLess listing pages `interval` seconds apart and say how each was met.

    One request at a time through the production transport, with no retry,
    and the first answer that is not a page ends it: a refusal is the finding,
    and asking again after one is the thing being measured not to do.
    """
    import time

    if not 1 <= pages <= MAX_PACE_PAGES:
        raise ValueError(f"--pages must be 1 to {MAX_PACE_PAGES}")
    if not MIN_PACE_INTERVAL_S <= interval <= MAX_PACE_INTERVAL_S:
        raise ValueError(f"--interval must be {MIN_PACE_INTERVAL_S:g} to {MAX_PACE_INTERVAL_S:g} seconds")
    clock = clock or time.monotonic
    owned = network is None
    if owned:
        context, _ = build_verified_ssl_context()
        network = NetworkClient(context, user_agent="CE-Decky-direct-probe/0.4")
    requests: list[dict] = []
    step: int | None = None
    started = clock()
    try:
        for index in range(pages):
            if index:
                if step is None:
                    break
                await sleep(interval)
            url = FEARLESS_FORUM_URL if index == 0 else f"{FEARLESS_FORUM_URL}&start={index * step}"
            began = clock()
            response = await network.get(
                url, allowed_hosts=PROVIDER_HOSTS["fearless"], max_bytes=MAX_HTML_BYTES,
                referer=FEARLESS_BASE_URL, retries=0,
            )
            entry = {
                "page": index, "status": response.status,
                "latency_ms": round((clock() - began) * 1000), "at_s": round(began - started, 3),
                "retry_after": response.headers.get("retry-after"),
            }
            requests.append(entry)
            if response.status != 200:
                break
            listing = parse_fearless_forum_page(decode_html(response.body))
            entry["topics"] = len(listing.rows)
            if index == 0:
                step = listing.page_step
                pages = min(pages, listing.total_pages)
    finally:
        if owned:
            await network.aclose()
    refused = [entry for entry in requests if entry["status"] != 200]
    return {
        "interval_s": interval, "pages_asked": pages, "requests": requests,
        "read": len(requests) - len(refused), "refused_at_page": refused[0]["page"] if refused else None,
        "elapsed_s": round(clock() - started, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", action="store_true", help="probe public provider route status without downloading tables")
    parser.add_argument("--playground-api", action="store_true", help="resolve one known Playground file through its anonymous public client protocol")
    parser.add_argument("--fearless-pace", action="store_true",
                        help="read the first --pages FearLess listing pages --interval seconds apart and report how each was met")
    parser.add_argument("--pages", type=int, default=4, help=f"listing pages to read, at most {MAX_PACE_PAGES}")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between requests")
    args = parser.parse_args(argv)
    if args.fearless_pace:
        try:
            report = asyncio.run(probe_fearless_pace(args.pages, args.interval))
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(report, indent=2))
        return 0 if report["refused_at_page"] is None else 1
    if args.playground_api:
        return asyncio.run(probe_playground_api())
    if args.routes:
        return asyncio.run(probe_routes())
    parser.error("select --routes, --playground-api or --fearless-pace")


if __name__ == "__main__":
    raise SystemExit(main())
