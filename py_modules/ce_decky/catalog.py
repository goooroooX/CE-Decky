from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
import asyncio
import threading
import io
import json
import math
from pathlib import PurePosixPath
import re
import time
import uuid
from typing import Awaitable, Callable, Iterable, Iterator, NamedTuple, Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .activity_log import log_activity, log_failure
from defusedxml.ElementTree import iterparse

from .atomic import atomic_write_json, load_json
from .game_identity import Candidate, aliases, install_directory_title, match_breakdown, query_plan
# `ProviderRateLimited` is raised by the transport as well as by the provider
# parsers here, so it lives beside the transport and is re-exported for the
# callers that have always imported it from this module.
from .network import (
    NetworkClient,
    NetworkError,
    ProviderChallenged,
    ProviderCooldown,
    ProviderRateLimited,
    is_transport_ready_url,
)
from .operations import drain_through_cancellation
from .providers import (
    ARTIFACT_SUFFIXES,
    MAX_ARTIFACT_BYTES,
    PROVIDER_REGISTRY,
    CatalogResult,
    ProviderSearchFailure,
    SearchOutcome,
    parse_retry_after,
    provider_definition,
    searchable_providers,
    sort_results,
)
from .github_discovery import (
    MAX_CANDIDATES as GITHUB_MAX_CANDIDATES,
    RepoCandidate as GitHubRepoCandidate,
    TreeTable as GitHubTreeTable,
    build_search_queries as github_search_queries,
    is_rate_limited as github_is_rate_limited,
    parse_repository_search as parse_github_repository_search,
    parse_tree_tables as parse_github_tree_tables,
    rank_repositories as rank_github_repositories,
    rate_limit_retry_after as github_retry_after,
    raw_url as github_raw_url,
    releases_url as github_releases_url,
    is_provider_asset as github_is_provider_asset,
    is_provider_page as github_is_provider_page,
    repo_from_source as github_repo_from_source,
    repo_page_url as github_repo_page_url,
    search_url as github_search_url,
    tree_url as github_tree_url,
)
from .vgtimes import (
    MAX_SITEMAP_BYTES as VGTIMES_MAX_SITEMAP_BYTES,
    SITE_HOSTS as VGTIMES_SITE_HOSTS,
    GameRef as VGTimesGameRef,
    ItemMetadata as VGTimesItemMetadata,
    ItemRef as VGTimesItemRef,
    VGTimesDownloadRequest,
    artifact_id_for as vgtimes_artifact_id,
    decompress as vgtimes_decompress,
    game_sitemap_urls as vgtimes_game_sitemap_urls,
    parse_game_sitemap as parse_vgtimes_game_sitemap,
    parse_item as parse_vgtimes_item,
    parse_sitemap_index as parse_vgtimes_sitemap_index,
    SLUG_RE as VGTIMES_SLUG_RE,
    parse_tables_listing as parse_vgtimes_tables_listing,
    tables_listing_url as vgtimes_tables_listing_url,
)
from .thecheatscript import (
    MAX_API_BYTES as THECHEATSCRIPT_MAX_API_BYTES,
    MAX_SITEMAP_ENTRIES as THECHEATSCRIPT_MAX_SITEMAP_ENTRIES,
    PostArtifact as TheCheatScriptPostArtifact,
    PostRef as TheCheatScriptPostRef,
    SITE_HOSTS as THECHEATSCRIPT_SITE_HOSTS,
    TheCheatScriptDownloadRequest,
    YANDEX_API_HOSTS as THECHEATSCRIPT_API_HOSTS,
    artifact_id_for as thecheatscript_artifact_id,
    is_post_url as is_thecheatscript_post_url,
    parse_post as parse_thecheatscript_post,
    parse_sitemap_index as parse_thecheatscript_sitemap_index,
    parse_sitemap_page as parse_thecheatscript_sitemap_page,
    parse_yandex_public,
    post_slug as thecheatscript_post_slug,
    public_metadata_url as thecheatscript_public_metadata_url,
    rank_posts as rank_thecheatscript_posts,
    title_from_url as thecheatscript_title_from_url,
)


# Derived from the provider registry rather than repeated here. A provider that
# declares no hosts stays absent, so asking for one still fails closed instead of
# resolving to an empty allowance.
PROVIDER_HOSTS = {
    provider_id: definition.hosts
    for provider_id, definition in PROVIDER_REGISTRY.items()
    if definition.hosts
}
# Exact CDN hosts observed from the public anonymous API. Keep this explicit:
# allowing a guessed wildcard would turn one provider response into arbitrary
# outbound artifact authority.
PLAYGROUND_ARTIFACT_HOSTS = PROVIDER_HOSTS["playground"] | frozenset({"dl1.gamedl.ru", "dl3.gamedl.ru"})
# Results CE Decky can download itself; a browser handoff is not an answer.
AUTOMATIC_DOWNLOAD_MODES = frozenset({"direct_https"})
# What a row may be published as, from the one place that says so. Retyped
# here it went out of step the moment `.rar` was added: every source below
# went on dropping those rows at its own listing filter.
SUPPORTED_SUFFIXES = ARTIFACT_SUFFIXES
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_SITEMAP_BYTES = 16 * 1024 * 1024
MAX_SITEMAP_CACHE_BYTES = 32 * 1024 * 1024
MAX_THECHEATSCRIPT_SITEMAP_CACHE_BYTES = 4 * 1024 * 1024
MAX_RESULTS = 100
MAX_TOPIC_FETCHES = 8
# How long one source may hold a search somebody is watching. Every request is
# bounded on its own, and that is not the same thing: a provider whose chain is
# a sitemap index, its pages, its ranked posts and a metadata call each can time
# out in turn and keep the whole search waiting for minutes while every other
# source finished long ago. When this expires the source is unavailable for this
# search and the rest of the answer is delivered.
PROVIDER_SEARCH_BUDGET_SECONDS = 45.0
# How long a source may spend checking an index it already has a copy of. The
# copy is the answer either way; this is only how long it is worth waiting to
# find out whether a fresher one is a moment away.
PROVIDER_INDEX_REFRESH_SECONDS = 5.0
# How long a copy of Playground's site index is used without asking whether a
# newer one exists. The entries are cheat pages, one per page, so what an
# out-of-date copy costs is a table for a game that had no cheat page at all
# when it was written; a game already in it is found exactly as before. Against
# that, asking on every search is a round trip every time and, while the site is
# answering slowly, the whole of `PROVIDER_INDEX_REFRESH_SECONDS` every time.
PLAYGROUND_INDEX_TTL_SECONDS = 6 * 60 * 60
# What must be left of the budget before the fallback pass on the installation
# directory's own name is worth starting. It is a second whole set of provider
# jobs, and started with a sliver left every one of them times out at once, so
# the user is shown a screen of failed sources in place of the answer the first
# pass had already produced.
PROVIDER_SEARCH_RETRY_MINIMUM_SECONDS = 8.0
# GitHub answers in JSON, and a recursive tree of a repository that builds its
# table from thousands of fragments is the largest of these answers.
MAX_GITHUB_API_BYTES = 4 * 1024 * 1024
# Repository search is GitHub's scarce ten-requests-per-minute bucket. Cache the
# normalized index answer by exact query for a day, while release and tree reads
# remain live so a repository already found can still publish a new table.
GITHUB_INDEX_TTL_SECONDS = 24 * 60 * 60
GITHUB_INDEX_STALE_SECONDS = 7 * 24 * 60 * 60
MAX_GITHUB_INDEX_QUERIES = 32
MAX_GITHUB_INDEX_CACHE_BYTES = 4 * 1024 * 1024
# What to wait when GitHub refuses a repository search without saying for how
# long. The anonymous search bucket refills every minute, and the reset stamp it
# usually sends is preferred over this.
GITHUB_SEARCH_DEFAULT_COOLDOWN_SECONDS = 60
FEARLESS_BASE_URL = provider_definition("fearless").base_url
PLAYGROUND_BASE_URL = provider_definition("playground").base_url
THECHEATSCRIPT_BASE_URL = provider_definition("thecheatscript").base_url
VGTIMES_BASE_URL = provider_definition("vgtimes").base_url
# One search reads at most this many of the game slugs the sitemap declares, and
# then at most this many of that game's own table listings. The sitemap carries
# tens of thousands of games, so the slug index is cached the way Playground's
# is and the ranking runs locally over it.
MAX_VGTIMES_GAMES = 3
MAX_VGTIMES_ITEMS = 6
MAX_VGTIMES_SITEMAP_CACHE_BYTES = 16 * 1024 * 1024
MAX_VGTIMES_SLUGS = 200_000
# Which board is indexed is this project's choice rather than something the site
# declares, so it stays here; where that board lives comes from the registry.
FEARLESS_FORUM_ID = "4"
FEARLESS_FORUM_URL = f"{FEARLESS_BASE_URL}viewforum.php?f={FEARLESS_FORUM_ID}"

# FearLess prefixes a topic title with a lowercase `z` to sort it to the end of
# its own listing. That marker is the forum's ordering and not part of the
# game's name, and carrying it into scoring cost the whole topic: the marker in
# front of a title scored 0.41 against a search for that game where the title
# alone scored 0.95, far under the floor a topic has to clear to be fetched at
# all, so a table with thousands of downloads at the source was simply absent
# from the answer. Those two figures are from the topic this was found on, whose
# title is not spelled here. On this device's index 132 of 16568 topics carried
# the marker, current releases included.
#
# Case is the whole of the rule and is not cosmetic: `Z Remastered` is a real
# game on the same listing, so only the lowercase marker is a marker.
_FEARLESS_SORT_MARKER = re.compile(r"^z\s+(?=\S)")


def fearless_topic_title(title: str) -> str:
    """The game's own name, without the forum's own ordering marker."""
    return _FEARLESS_SORT_MARKER.sub("", title, count=1)
# How many topics one listing page holds is the forum's setting, not this
# project's, and it is what every stored offset means. It used to be assumed to
# be 50: a forum that moved to another size would have left this crawling every
# other page, keeping an index with holes in it that looked complete and
# rejecting the offsets it had already stored. It is read from the listing's own
# pagination now, and only these bounds stay fixed.
MAX_FEARLESS_PAGE_STEP = 500
MAX_FEARLESS_ROWS_PER_PAGE = 500
MAX_FEARLESS_INDEX_PAGES = 500
MAX_FEARLESS_INDEX_TOPICS = 20_000
MAX_FEARLESS_TOPIC_TITLE = 256
MAX_FEARLESS_TOPIC_ID_DIGITS = 20
MAX_FEARLESS_INDEX_CACHE_BYTES = 32 * 1024 * 1024
# The Search marker's own file, beside the index it arms, and the whole of what
# it holds is one timestamp.
#
# The marker rides in the index cache as well, which is where it is read from
# when it is the newer of the two, but it cannot only live there: that file is a
# third of a megabyte written under a lock a native crawl worker holds across
# its own `fsync`, so it was rewritten for the marker at most once an hour and
# the unload prologue, which is spending Decky's five seconds, refuses to wait
# for that lock at all. A search was therefore recorded late or not at all: lost
# at the process boundary when a crawl held the lock at that moment, and lost
# outright when the process was killed, ran out of memory or lost power before
# any unload.
#
# A file of its own has none of that. It is a few dozen bytes, nothing else
# writes it, and it is written as the search happens rather than owed to a later
# write of something else.
FEARLESS_SEARCH_MARKER_NAME = "fearless-search.json"
MAX_FEARLESS_SEARCH_MARKER_BYTES = 4096
FEARLESS_INDEX_PERSIST_EVERY = 8
FEARLESS_REFRESH_PAGES = 2
# Leading pages a search refreshes instead when the index has gone a while
# without a background pass. New and freshly bumped topics are what a listing
# sorted by activity puts at the front, so after a long gap the front is both
# the stalest part of the index and the part most likely to hold what the user
# is searching for. Eight pages is about four hundred topics, and it is paid
# only by the first search after the gap: the pass it arms brings the rest up to
# date behind it.
FEARLESS_STALE_REFRESH_PAGES = 8
# How long a page may go unrefreshed before a search treats the whole index as
# needing that wider front. Twice the per-page freshness below, so an index a
# running background pass is merely part way through does not trigger it.
FEARLESS_STALE_REFRESH_AFTER_SECONDS = 48 * 60 * 60
FEARLESS_INDEX_REQUEST_INTERVAL_SECONDS = 1.0
# How many listing pages one background pass refreshes before stopping. Per-page
# freshness comes due for most of the index at once, so a pass that walks
# whatever is pending walks nearly the whole forum: measured here, a pass began
# with 247 of 332 pages pending and was rate limited three seconds in, and that
# rate limit is the provider's, so it stopped the searches the user was running
# as well. A pass this size is over in half a minute and a full refresh still
# fits comfortably inside the day the per-page freshness allows.
FEARLESS_INDEX_PAGES_PER_RUN = 32
# How long after a pass starts before another may. Without this the pass that
# stops short is restarted by the next search, which is every search.
FEARLESS_INDEX_RUN_INTERVAL_SECONDS = 30 * 60
# How often the plugin looks at whether a pass is owed while nobody is using
# it. Every pass used to be started by a search, so a device whose owner was
# playing rather than searching indexed nothing at all: on this one the listing
# stood at 97 of 333 pages with the oldest of them 21 hours old, which is a page
# ageing out for every page a search happened to add. The interval is short
# relative to the pacing above rather than equal to it, because this only asks
# whether a pass is owed; the pacing, the per-page freshness and the quiet-search
# rule are what decide whether one actually starts, and all three are unchanged.
FEARLESS_INDEX_SCHEDULE_INTERVAL_SECONDS = 5 * 60
# How long after the plugin loads before the first such look. Steam, Decky and
# every other plugin are starting in the same seconds, and nothing here is owed
# so soon that it cannot wait for that.
FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS = 3 * 60
# How long after a search the listing goes on refreshing itself.
#
# The crawl's job is to bring the index up to date, not to hold it there for
# ever. A plugin is loaded for as long as Steam is running, and a user may not
# open Search for months: left to a timer alone, a device nobody is searching on
# would ask this forum for its whole listing every day, permanently, for an
# index nothing is going to read. So the timer is armed by the one thing that
# says the index is worth having, which is somebody using Search.
#
# A day, because that is the per-page freshness below: one search buys a window
# in which every page comes due at most once, the pass walks them, and then
# nothing is pending and the crawl goes quiet on its own well before the window
# closes. Searching again re-arms it.
FEARLESS_INDEX_ACTIVE_WINDOW_SECONDS = 24 * 60 * 60
# How many rate limits one pass absorbs before leaving the rest to a later one.
# The retry was unbounded, so a provider that answers every request with 429
# held one page forever, re-arming the cooldown that blocks the user's searches
# each time round.
FEARLESS_INDEX_RATE_LIMITS_PER_RUN = 2
# How long after the last search a background pass waits before asking for
# anything. The provider has one budget of requests and the user is watching a
# screen for one of them; this crawl is not.
FEARLESS_INDEX_QUIET_SECONDS = 10.0
# How long one pass gives way for before abandoning itself. Giving way has no
# natural end: a user searching steadily holds the crawl for as long as they
# keep doing it, and a pass that waits forever is a task that never finishes.
# The pass is dropped instead, and the pacing above decides when another may
# start.
FEARLESS_INDEX_QUIET_WAIT_LIMIT_SECONDS = 300.0
# A cached listing page is re-read once a day. Freshness is tracked per page,
# so the daily pass re-reads only what has actually aged out - the leading
# pages every search already refreshed are skipped rather than rebuilt.
FEARLESS_INDEX_MAX_AGE_SECONDS = 24 * 60 * 60
FEARLESS_DEFAULT_COOLDOWN_SECONDS = 60
# Playground groups every cheat page for a game under one taxonomy. Only the
# `table` category holds Cheat Engine tables; trainers, save files, save
# editors, cheat mods, codes, fixes and unlockers live beside them and are not
# distinguishable by filename, because both a table and a save editor ship as
# `.zip`. Reading the category listing replaces fetching every cheat page.
PLAYGROUND_TABLE_CATEGORY = "table"
MAX_PLAYGROUND_CATEGORY_SLUGS = 2
MAX_PLAYGROUND_ENTRY_DEPTH = 6
PLAYGROUND_ENTRY_RE = re.compile(r"^/([a-z0-9_]+)/cheat/([a-z0-9_\-]+-\d+)$")
# A category link has no `-<id>` suffix; it is what each entry uses to say
# whether it is a table, a save, a trainer or a save editor.
PLAYGROUND_CATEGORY_RE = re.compile(r"^/([a-z0-9_]+)/cheat/([a-z0-9_]+)$")
CACHE_TTL_SECONDS = 6 * 60 * 60
STALE_TTL_SECONDS = 7 * 24 * 60 * 60
# A version is metadata only when the publisher marks it as one. A bare decimal
# in an attachment block is normally its size (the target exposed 78.21 KiB and
# 505.53 KiB as bogus versions), while an unmarked integer is commonly a game
# number or download count. Keep this deliberately narrower than general number
# extraction rather than presenting a confident guess to the user.
VERSION_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(?:v|ver(?:sion)?|version|версия)\s*[:=_-]?\s*"
    r"(\d+(?:[._-]\d+){0,3})(?:[^a-z0-9]|$)"
)
# Filenames also commonly carry an unlabelled dotted release (`Table_1.0.5.CT`).
# Read that shape only from the filename, never the surrounding attachment
# block, and bound every component so a long build identity is not a version.
FILENAME_VERSION_RE = re.compile(
    r"(?i)(?:^|[^a-z0-9])(\d{1,4}(?:[._-]\d{1,4}){1,3})(?:[^a-z0-9]|$)"
)
# phpBB renders the uploader's own attachment comment in its own element, next
# to but apart from the size line, and that comment is where FearLess publishes
# the release: one post carries every revision of a table, all sharing a
# filename and the post's date, so an unmarked `1.0.6` there is the only thing
# telling them apart. Reading it from that element is not the bare-decimal guess
# the marker rule above refuses: a size never appears in it.
COMMENT_VERSION_RE = re.compile(r"^(\d{1,4}(?:[._-]\d{1,4}){1,3})(?![0-9.])")
# A publisher who versions in whole numbers writes the comment as just that
# number. Accept it only when it is the entire comment, so a note that opens
# with a count ("3 new options") is never read as a release.
BARE_COMMENT_VERSION_RE = re.compile(r"^(\d{1,4})$")
DOWNLOADS_RE = re.compile(r"(?:Downloaded|Скачиваний:)\s*([0-9][0-9, ]*)", re.I)
PASSWORD_RE = re.compile(r"(?i)(?:password|пароль)\s*[:=]\s*([A-Za-z0-9._@+\-]{1,64})")
SIZE_RE = re.compile(r"(?i)([0-9]+(?:[.,][0-9]+)?)\s*(bytes?|ki?b|mi?b|gi?b|кб|мб)")
TOPIC_RE = re.compile(r"(?:^|[?&])t=(\d+)")
ATTACHMENT_RE = re.compile(r"(?:^|[?&])id=(\d+)")
PLAYGROUND_ID_RE = re.compile(r"-(\d+)$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLAYGROUND_IDENTITY_RE = re.compile(r"^[0-9]{1,20}$")
MAX_PLAYGROUND_API_BYTES = 64 * 1024
MAX_PLAYGROUND_COUNTDOWN_SECONDS = 120


class AcquisitionRequest(Protocol):
    """A provider's own way of turning a stored identity into a live link.

    Some providers cannot be downloaded from a URL captured at search time: the
    link does not exist until it is asked for, and it expires. Each such
    provider owns that exchange here rather than adding a field to
    `ArtifactRecord` and a branch to `resolve_download`, which is what a second
    and third deferred provider would otherwise cost.
    """

    @property
    def provider(self) -> str:
        """Whose cooldown and diagnostics govern the exchange."""

    @property
    def artifact_hosts(self) -> frozenset[str]:
        """Exact hosts the resolved link must be within, never a wildcard."""

    @property
    def send_source_referer(self) -> bool:
        """Whether artifact transfer may name the catalog page as Referer."""

    @property
    def exclusive(self) -> bool:
        """Whether the provider serves this client one download at a time.

        Optional, and read with a default: a provider that says nothing about
        concurrency is downloaded concurrently as before. A provider that does
        say so has its acquisitions serialized, because being refused for a rule
        the provider already stated is a worse answer than waiting for it.
        """

    async def resolve(
        self,
        network: NetworkClient,
        record: ArtifactRecord,
        *,
        sleep: Callable[[float], Awaitable[object]],
        on_countdown: Callable[..., None] | None = None,
    ) -> str | tuple[str, frozenset[str]]:
        """Return a link, plus a runtime-derived exact host set when needed.

        `on_countdown(seconds)` reports a wait this resolver is serving out, and
        takes an optional second argument naming what kind it is: `"preparing"`
        for the countdown a provider always makes a guest sit through, and
        `"rate_limited"` for the provider refusing right now. A resolver that
        keeps a rate limit inside itself is the only place that limit is
        visible, so saying which it is here is what keeps a refusal from being
        shown as ordinary preparation.
        """


@dataclass(frozen=True)
class PlaygroundDownloadRequest:
    file_id: str
    post_id: str

    def __post_init__(self) -> None:
        if not PLAYGROUND_IDENTITY_RE.fullmatch(self.file_id) or not PLAYGROUND_IDENTITY_RE.fullmatch(self.post_id):
            raise ValueError("Playground download identity is invalid")

    @property
    def provider(self) -> str:
        return "playground"

    @property
    def artifact_hosts(self) -> frozenset[str]:
        return PLAYGROUND_ARTIFACT_HOSTS

    @property
    def send_source_referer(self) -> bool:
        return True

    async def resolve(
        self,
        network: NetworkClient,
        record: ArtifactRecord,
        *,
        sleep: Callable[[float], Awaitable[object]],
        on_countdown: Callable[..., None] | None = None,
    ) -> str:
        return await resolve_playground_download(
            network, record, sleep=sleep, on_countdown=on_countdown
        )


@dataclass(frozen=True)
class ArtifactRecord:
    result: CatalogResult
    direct_url: str | None = None
    advertised_sha256: str | None = None
    # A table committed in a git tree advertises a git object name rather than a
    # digest of its bytes, and git defines that name exactly, so this route
    # keeps an integrity anchor of its own instead of downloading unverifiable
    # bytes. It stays backend-only: it is not a content digest and nothing that
    # identifies a table by SHA-256 may key on it.
    blob_sha1: str | None = None
    password_hint: str | None = None
    stale: bool = False
    size_exact: bool = False
    # One slot for every provider whose link must be fetched rather than stored.
    acquisition: AcquisitionRequest | None = None

    def public_dict(self) -> dict[str, object]:
        value = self.result.as_dict()
        value["stale"] = self.stale
        value["advertised_sha256"] = self.advertised_sha256
        value["password_required"] = self.password_hint is not None
        return value


class BrowserHandoff(NetworkError):
    def __init__(self, url: str) -> None:
        super().__init__("HTTP 403 challenge; continue in the system browser")
        self.url = url


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.casefold() != "sid"]
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, urlencode(query), ""))


def _text(value: object, limit: int = 4096) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def decode_html(body: bytes) -> str:
    """Decode provider HTML without trusting incorrect legacy charset labels.

    Playground currently serves some pages containing Windows-1251 bytes while
    declaring UTF-8. Prefer strict UTF-8 and use the site's legacy encoding only
    when the payload is not valid UTF-8; replacement decoding would corrupt game
    titles, filenames, sizes, and source-link labels used by the catalog parser.
    """
    try:
        return body.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return body.decode("windows-1251", "replace")


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _version(*values: str) -> str | None:
    match = VERSION_RE.search(" ".join(values))
    if match is None and values:
        match = FILENAME_VERSION_RE.search(values[0])
    return match.group(1).replace("_", ".").replace("-", ".") if match else None


def _comment_version(comment: str) -> str | None:
    text = comment.strip()
    if not text:
        return None
    # Position first, marker second. The release this attachment is opens the
    # comment; a marked number further along is usually about something else,
    # and searching for it first read "2.0 fixes for v1.9 saves" as 1.9.
    match = (
        COMMENT_VERSION_RE.match(text)
        or BARE_COMMENT_VERSION_RE.match(text)
        or VERSION_RE.search(text)
    )
    return match.group(1).replace("_", ".").replace("-", ".") if match else None


# Everything a provider says *about* a table is publisher-authored text around
# the one thing that matters: the link. Version, size, author, date, download
# count and notes are description, and rejecting a row because one of them is
# malformed loses a table that is right there and downloadable. Drop the
# description instead, and skip the row only when the identity that makes it
# obtainable is itself unusable.
DESCRIBED_RESULT_FIELDS = ("version", "size_bytes", "author", "posted_at", "download_count", "notes")
MAX_PARSE_ISSUE_SAMPLES = 5


@dataclass
class ParseIssues:
    """What a provider published that could not be read, while a search ran.

    `degraded` counts description dropped or a single row skipped: tables stay
    obtainable. `failed` counts a page that could not be read at all, which is
    what a source rebuilding its markup looks like from here. Both are reported
    once per provider per search rather than per row, so a redesigned page is a
    number rather than a flood.
    """

    degraded: int = 0
    failed: int = 0
    samples: list[str] = field(default_factory=list)
    # Why this provider could not fully answer, when it still returned rows.
    # A job that hits a cooldown or a rate limit part way through has two things
    # to say at once, and returning a list could only say one of them: every
    # list was read as success, so a provider that had just recorded a cooldown
    # had it cleared again by the same search, and its cached rows were not
    # restored because nothing had failed.
    unavailable: str | None = None
    # Whether the job already wrote this to the provider's own diagnostics. A
    # provider that recorded its own cooldown has, and recording it again would
    # double-count the error and overwrite what it said; a provider that simply
    # stopped has not, and left to the search it was recorded nowhere at all.
    unavailable_recorded: bool = False

    def mark_unavailable(self, reason: str, *, recorded: bool = False) -> None:
        if self.unavailable is None:
            self.unavailable = reason[:2048]
            self.unavailable_recorded = recorded

    def note(self, *, critical: bool, reason: str) -> None:
        if critical:
            self.failed += 1
        else:
            self.degraded += 1
        if len(self.samples) < MAX_PARSE_ISSUE_SAMPLES:
            self.samples.append(reason[:200])


_PARSE_ISSUES: ContextVar[ParseIssues | None] = ContextVar("ce_decky_parse_issues", default=None)

# The source selection one search runs against, frozen for its whole duration.
#
# `search()` reads the choice once so that the jobs it builds, the cached rows
# it re-serves and the roster it returns all describe the same set of sources.
# A linked read happens deep inside a provider job, where that snapshot cannot
# be passed as an argument without threading it through every adapter, and
# reading the choice live there meant one search could act on two different
# answers: a switch pressed mid-search changed which pages were followed while
# the roster still described the selection the search began with.
#
# Deliberately not used by the background listing crawl, which is not part of
# any search and must observe a switch the moment it is pressed.
_SEARCH_SELECTION: ContextVar[frozenset[str] | None] = ContextVar(
    "ce_decky_search_selection", default=None,
)


# Whether this host's 7-Zip can open a RAR, frozen for the whole of one search.
#
# A row is dropped for it, at the one funnel every provider's rows pass through,
# because the alternative is offering a table that costs a provider countdown
# and a download to be refused at the archive - which is the same reason a row
# whose exact size is already too large is dropped here rather than at the
# transfer. It is a property of the device and not of the provider, so it is
# frozen per search exactly as the source selection is rather than probed once
# per row: the answer comes from running 7-Zip.
_RAR_OPENABLE: ContextVar[bool] = ContextVar("ce_decky_rar_openable", default=True)
# Which search a stage note belongs to. A note is written from inside a provider
# job, several call frames below the search that started it, and two searches
# can overlap: the panel runs one at a time but nothing in the backend requires
# that. Without this, a job belonging to the search that has been replaced went
# on writing into the record the screen is now reading, and finishing that job
# marked the newer search's source done. Child tasks inherit the context they
# were created in, so the jobs of one search carry its number and no other's.
_SEARCH_GENERATION: ContextVar[int] = ContextVar("ce_decky_search_generation", default=0)
# What the screen waiting on a search calls that search. The generation above
# says which search may write the record; this says which caller may read it.
# Without it there is one latest record and no way for a reader to prove it is
# about their own search: two overlapping searches meant one panel could be
# shown the other's sources, and the one that finished first blanked the line
# of the one still running.
_SEARCH_TOKEN: ContextVar[str | None] = ContextVar("ce_decky_search_token", default=None)


@dataclass
class _SearchSourceProgress:
    """What one source is doing while a search is still running."""

    provider: str
    name: str
    state: str
    stage: str | None

    def as_dict(self) -> dict[str, object]:
        return {"provider": self.provider, "name": self.name, "state": self.state, "stage": self.stage}


@dataclass
class _SearchProgress:
    """One search, for the screen that is waiting on it."""

    generation: int
    # What its own caller calls it. A reader without this token is not the
    # caller of this search and is told nothing.
    token: str | None
    started_at: float
    running: bool
    sources: dict[str, _SearchSourceProgress]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": 1,
            "running": self.running,
            "elapsed_ms": int((time.monotonic() - self.started_at) * 1000),
            "sources": [source.as_dict() for source in self.sources.values()],
        }



@contextmanager
def searching_with_rar_support(openable: bool) -> Iterator[None]:
    """Freeze whether a .rar row can be offered at all, for one search."""
    token = _RAR_OPENABLE.set(openable)
    try:
        yield
    finally:
        _RAR_OPENABLE.reset(token)


@contextmanager
def searching_with_selection(disabled: frozenset[str]) -> Iterator[None]:
    """Freeze the switched-off sources for everything this search does."""
    token = _SEARCH_SELECTION.set(disabled)
    try:
        yield
    finally:
        _SEARCH_SELECTION.reset(token)


# The rows one provider has found so far, while it is still finding them.
#
# A provider builds its answer as it goes and returns it at the end, and the
# search's own deadline cancels it: everything it had was thrown away with the
# coroutine, so a source that spent forty seconds and had already read half its
# pages contributed nothing at all. This is the same shape `ParseIssues` has and
# for the same reason: it is entered outside the wait, so it survives the
# cancellation and the search can keep what was already found.
_PROVIDER_ROWS: ContextVar[list | None] = ContextVar("ce_decky_provider_rows", default=None)


@contextmanager
def collect_provider_rows() -> Iterator[list]:
    """Collect the rows one provider finds, so a deadline cannot discard them."""
    rows: list = []
    token = _PROVIDER_ROWS.set(rows)
    try:
        yield rows
    finally:
        _PROVIDER_ROWS.reset(token)


def _provider_rows() -> list:
    """The list this provider's rows go into, whoever is collecting it.

    A provider called outside a search, which is what every focused test does,
    gets an ordinary list of its own and behaves exactly as it always has.
    """
    rows = _PROVIDER_ROWS.get()
    return rows if rows is not None else []


@contextmanager
def collect_parse_issues() -> Iterator[ParseIssues]:
    """Collect the parse issues raised inside this task, for one provider."""
    issues = ParseIssues()
    token = _PARSE_ISSUES.set(issues)
    try:
        yield issues
    finally:
        _PARSE_ISSUES.reset(token)


def _note_parse_issue(reason: str, *, critical: bool = False) -> None:
    issues = _PARSE_ISSUES.get()
    if issues is not None:
        issues.note(critical=critical, reason=reason)


def _diagnostic_error(exc: BaseException) -> str:
    """One provider failure as a bounded single line of diagnostics text."""
    text = f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ").replace("\x00", " ")
    return text[:1024] or type(exc).__name__


def _note_provider_unavailable(reason: str, *, recorded: bool = False) -> None:
    """Say that this provider could not fully answer, without losing its rows.

    The job keeps returning what it did obtain. What this adds is the other half
    of the answer, so the search reports the source as unavailable, leaves the
    provider's own recorded deadline alone, and falls back to its cached rows.

    `recorded` says the job already wrote this to the provider's diagnostics,
    which a provider that set its own cooldown has. Everything else is recorded
    by the search, because the screen that exists to say which source is
    failing cannot say it from a value that was never written down.
    """
    issues = _PARSE_ISSUES.get()
    if issues is not None:
        issues.mark_unavailable(reason, recorded=recorded)


def acquirable_size(size_bytes: object, *, size_exact: bool) -> bool:
    """Whether a row this size can actually be downloaded.

    Only an exact size answers this. A described or rounded one is publisher
    text and the streaming bound still governs it at transfer time, but a size
    the provider states exactly is knowable now, and offering a row that is
    certain to be refused costs the user a press, a provider countdown and a
    wait to be told something the search already knew.
    """
    if not size_exact or isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        return True
    return size_bytes <= MAX_ARTIFACT_BYTES


def acquirable_archive(filename: object) -> bool:
    """Whether this host can open what the row would download.

    Only asked of `.rar`, because it is the one artifact kind whose handler a
    7-Zip build may simply not have: the reduced `7za`/`7zr` builds omit it. A
    `.zip` is read by Python itself and a `.7z` by every build that exists.
    """
    if not isinstance(filename, str) or not filename.casefold().endswith(".rar"):
        return True
    return _RAR_OPENABLE.get()


def catalog_result_or_none(**fields: object) -> CatalogResult | None:
    if not acquirable_archive(fields.get("filename")):
        _note_parse_issue("row skipped: this device's 7-Zip cannot open .rar archives")
        return None
    try:
        return CatalogResult(**fields)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        first = str(exc)[:120]
    try:
        result = CatalogResult(**{**fields, **{name: None for name in DESCRIBED_RESULT_FIELDS}})  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        _note_parse_issue(f"row skipped: {exc}"[:200])
        return None
    _note_parse_issue(f"description dropped: {first}")
    return result


def rows_from_page(parse: Callable[[], list[ArtifactRecord]]) -> list[ArtifactRecord]:
    """One unreadable page costs that page, never the rest of the provider.

    A provider redesigns its markup, or publishes one broken post, without
    warning. Every other topic already fetched still holds tables CE Decky can
    download, so a parse that raises is an empty page here rather than a failed
    source.
    """
    try:
        return parse()
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        _note_parse_issue(f"page unreadable: {exc}", critical=True)
        return []


def _size(value: str) -> int | None:
    match = SIZE_RE.search(value)
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).casefold()
    factor = 1 if unit.startswith("byte") else 1024 if unit in {"kb", "kib", "кб"} else 1024 ** 2 if unit in {"mb", "mib", "мб"} else 1024 ** 3
    result = round(number * factor)
    return result if 0 <= result <= 64 * 1024 * 1024 else None


class ForumListing(NamedTuple):
    """One listing page, and what it says about the listing it belongs to."""

    rows: list[tuple[str, str]]
    current_page: int
    total_pages: int
    total_topics: int | None
    # The offset step between listing pages, taken from the pagination's own
    # numeric links. `None` when this page exposes no independent step, either
    # because there is one page or because phpBB renders the useful current
    # page number as a non-link.
    page_step: int | None
    # True when numeric pagination explicitly contradicts itself, its totals,
    # or the bounds. False with no independent step evidence, which is normal
    # when the current page is an active span and only page one remains linked.
    page_step_invalid: bool


def parse_fearless_forum_page(html: str) -> ForumListing:
    """Parse one bounded FearLess table-forum listing page.

    The table forum exposes every topic title and ID through its listing pages
    even though its search route challenges anonymous HTTP. Global
    announcements and sticky policy rows are deliberately excluded. How many
    topics a page holds is read from the pagination rather than assumed, because
    that number is what every stored offset means.
    """
    soup = BeautifulSoup(html, "html.parser")
    current_page: int | None = None
    total_pages: int | None = None
    total_topics: int | None = None
    page_step: int | None = None
    for pagination in soup.select("div.pagination"):
        page_text = _text(pagination.get_text(" ", strip=True), 1024)
        page_match = re.search(r"\bPage\s+(\d+)\s+of\s+(\d+)\b", page_text, re.I)
        if not page_match:
            continue
        current_page = int(page_match.group(1))
        total_pages = int(page_match.group(2))
        topic_match = re.search(r"\b([\d, ]+)\s+topics\b", page_text, re.I)
        if topic_match:
            total_topics = int(topic_match.group(1).replace(",", "").replace(" ", ""))
        forum_links = [
            link for link in pagination.find_all("a", href=True)
            if "viewforum.php" in str(link.get("href"))
            and dict(parse_qsl(urlsplit(urljoin(FEARLESS_FORUM_URL, str(link.get("href")))).query)).get("f") == FEARLESS_FORUM_ID
        ]
        # A numeric page link explicitly relates its one-based page number to
        # the listing offset. Offsets alone are ambiguous: a lone start=100 can
        # mean page 2 of a 100-row listing or page 3 of a 50-row one.
        declared_steps: set[int] = set()
        invalid_relation = False
        for link in forum_links:
            label = _text(link.get_text(" ", strip=True), 64)
            if not label.isdigit():
                continue
            page_number = int(label)
            if not 1 <= page_number <= total_pages:
                invalid_relation = True
                continue
            declared = dict(parse_qsl(urlsplit(urljoin(FEARLESS_FORUM_URL, str(link.get("href")))).query)).get("start")
            if page_number == 1:
                if declared not in {None, "0"}:
                    invalid_relation = True
                continue
            if declared is None or not declared.isdigit():
                invalid_relation = True
                continue
            offset = int(declared)
            divisor = page_number - 1
            if offset <= 0 or offset % divisor:
                invalid_relation = True
                continue
            step = offset // divisor
            if 0 < step <= MAX_FEARLESS_PAGE_STEP:
                declared_steps.add(step)
            else:
                invalid_relation = True
        if total_pages > 1 and not invalid_relation and len(declared_steps) == 1:
            candidate = next(iter(declared_steps))
            if (
                total_topics is None
                or (total_pages - 1) * candidate < total_topics <= total_pages * candidate
            ):
                page_step = candidate
            else:
                invalid_relation = True
        elif len(declared_steps) > 1:
            invalid_relation = True
        break
    if current_page is None or total_pages is None:
        raise ValueError("FearLess forum listing has no bounded page count")
    if not 1 <= total_pages <= MAX_FEARLESS_INDEX_PAGES:
        raise ValueError("FearLess forum page count exceeds the configured limit")
    if not 1 <= current_page <= total_pages:
        raise ValueError("FearLess forum current page is invalid")
    if total_topics is not None and not 0 <= total_topics <= MAX_FEARLESS_INDEX_TOPICS:
        raise ValueError("FearLess forum topic count exceeds the configured limit")

    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in soup.select("li.row"):
        classes = {str(value).casefold() for value in item.get("class", [])}
        if classes & {"global-announce", "announce", "sticky"}:
            continue
        link = item.select_one("a.topictitle[href]")
        if link is None:
            continue
        target = canonical_url(urljoin(FEARLESS_FORUM_URL, str(link.get("href", ""))))
        query = dict(parse_qsl(urlsplit(target).query))
        topic_id = query.get("t")
        title = _text(link.get_text(" ", strip=True), MAX_FEARLESS_TOPIC_TITLE)
        if (
            query.get("f") != FEARLESS_FORUM_ID
            or not topic_id
            or not topic_id.isdigit()
            or len(topic_id) > MAX_FEARLESS_TOPIC_ID_DIGITS
            or not title
            or topic_id in seen
        ):
            continue
        seen.add(topic_id)
        rows.append((topic_id, title))
    return ForumListing(
        rows[:MAX_FEARLESS_ROWS_PER_PAGE], current_page, total_pages,
        total_topics, page_step, invalid_relation,
    )


def parse_phpbb_attachments(
    html: str,
    *,
    provider: str,
    display_name: str,
    topic_id: str,
    topic_title: str,
    source_page: str,
    score: float,
    rank: int,
    hosts: frozenset[str],
) -> list[ArtifactRecord]:
    soup = BeautifulSoup(html, "html.parser")
    selectors = "div.inline-attachment dl.file a.postlink, dl.attachbox dl.file a.postlink, dl.attachbox a.postlink"
    rows: list[ArtifactRecord] = []
    seen: set[str] = set()
    for link in soup.select(selectors):
        filename = _text(link.get_text(" ", strip=True), 1024)
        if PurePosixPath(filename).suffix.casefold() not in SUPPORTED_SUFFIXES:
            continue
        direct = canonical_url(urljoin(source_page, str(link.get("href", ""))))
        if not is_transport_ready_url(direct, hosts):
            # A relative attachment link resolves against the topic and is this
            # provider's by construction, but an absolute one is whatever the
            # page said. Offered as a direct download it was a row the transport
            # could only refuse once the user had chosen it.
            _note_parse_issue("row skipped: attachment link is not this provider's own")
            continue
        if not urlsplit(direct).path.endswith("/download/file.php") and "/download/file.php" not in urlsplit(direct).path:
            continue
        match = ATTACHMENT_RE.search(urlsplit(direct).query)
        attachment_id = match.group(1) if match else sha256(direct.encode()).hexdigest()[:20]
        artifact_id = f"topic-{topic_id}:attachment-{attachment_id}"
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        post = link.find_parent("div", id=re.compile(r"^p\d+$")) or link.find_parent(class_=re.compile(r"(^|\s)post(\s|$)"))
        author_el = post.select_one(".author .username, .postprofile a.username") if post else None
        time_el = post.select_one("time[datetime]") if post else None
        file_block = link.find_parent("dl", class_="file")
        block = file_block or link.parent
        nearby = _text(block.get_text(" ", strip=True) if block else filename, 2048)
        comment_el = file_block.select_one("dd em") if file_block else None
        # Written on the live forum as `- Updated to 1.04.02 ...`: the dash is
        # the uploader separating the note from the file name above it, and on a
        # row that shows the note first it is a stray mark before the sentence.
        comment = _text(comment_el.get_text(" ", strip=True), 512).lstrip("-\u2013\u2014 ").strip() if comment_el else ""
        downloads = DOWNLOADS_RE.search(nearby)
        password = PASSWORD_RE.search(nearby)
        result = catalog_result_or_none(
            provider=provider,
            provider_display_name=display_name,
            topic_id=topic_id,
            artifact_id=artifact_id,
            table_title=topic_title,
            filename=filename,
            version=_comment_version(comment) or _version(filename, nearby),
            size_bytes=_size(nearby),
            source_page=source_page,
            download_mode="direct_https",
            match_score=score,
            provider_rank=rank,
            author=_text(author_el.get_text(" ", strip=True)) if author_el else None,
            posted_at=_iso(str(time_el.get("datetime"))) if time_el else None,
            download_count=int(downloads.group(1).replace(",", "").replace(" ", "")) if downloads else None,
            # The uploader's own note on this exact attachment, which on this
            # provider is the only thing telling one row from another: a topic
            # carries every revision of one table, so ten rows share the file
            # name, the topic title and the date of the post they sit in, and
            # differ by a sentence saying what each one changed. The whole block
            # was being kept instead, which is that sentence with the size and
            # the download count already shown beside it stuck on the end.
            #
            # Dropped whole where the block declares an archive password. That
            # password is published in the open beside the file and the backend
            # uses it by itself, but it is deliberately never displayed, logged
            # or persisted, and a note that quotes it would do all three.
            notes=None if password else (comment or None),
        )
        if result is None:
            continue
        rows.append(ArtifactRecord(result, direct_url=direct, password_hint=password.group(1) if password else None))
    return rows


def parse_playground_sitemap(data: bytes) -> list[tuple[str, str, str]]:
    """The cheat pages this provider's own sitemap declares.

    The root has to be a sitemap and every entry has to be one of this
    provider's own HTTPS pages. Read by path shape alone, an entry from any
    origin whose path happened to look right became a cached entry of this
    provider, and its origin was then thrown away: a later search matched it and
    failed at the host boundary, for a game that never existed here.
    """
    if len(data) > MAX_SITEMAP_BYTES:
        raise ValueError("Playground sitemap exceeds the byte limit")
    rows: list[tuple[str, str, str]] = []
    root_checked = False
    for event, element in iterparse(io.BytesIO(data), events=("start", "end")):
        if not root_checked:
            if element.tag.rsplit("}", 1)[-1] != "urlset":
                raise ValueError("Playground sitemap has an unexpected root")
            root_checked = True
        if event != "end":
            continue
        if element.tag.rsplit("}", 1)[-1] != "loc":
            element.clear()
            continue
        url = _text(element.text).strip()
        element.clear()
        if not url or not is_transport_ready_url(url, PROVIDER_HOSTS["playground"]):
            continue
        path = urlsplit(url).path.rstrip("/")
        match = PLAYGROUND_ID_RE.search(path)
        if not match or "/cheat/" not in path:
            continue
        slug = path.rsplit("/", 1)[-1][: -(len(match.group(1)) + 1)]
        title = " ".join(part for part in slug.replace("_", "-").split("-") if part)
        if title:
            rows.append((match.group(1), title, canonical_url(url)))
        if len(rows) >= 100_000:
            raise ValueError("Playground sitemap exceeds the entry limit")
    return rows


def _index_within_ttl(retrieved: object, ttl: float) -> bool:
    """Whether a cached index is young enough not to be asked about at all.

    A timestamp that is missing, unreadable, or in the future is old: those are
    a cache written by something that did not record one, or a clock that has
    moved, and neither is a reason to stop refreshing an index for good.
    """
    if isinstance(retrieved, bool) or not isinstance(retrieved, (int, float)):
        return False
    age = time.time() - float(retrieved)
    return 0 <= age < ttl


def sitemap_prefilter_tokens(game_aliases: list[str]) -> list[str]:
    """Cheap tokens that a sitemap title must contain to be worth scoring.

    Playground's sitemap carries tens of thousands of entries, and running the
    full match scorer over all of them dominated the search: 50,000 titles cost
    over twenty seconds to rank for fifteen matches. Its titles are already
    normalized to lowercase ASCII words, so requiring the longest distinctive
    word of some alias is a safe gate in front of the real scorer, which still
    decides every surviving candidate.
    """
    tokens: set[str] = set()
    for alias in game_aliases:
        words = [word for word in re.split(r"[^a-z0-9]+", alias.casefold()) if len(word) >= 3]
        if words:
            tokens.add(max(words, key=len))
    return sorted(tokens)


def playground_game_slug(url: str) -> str | None:
    """Return the game slug of one Playground cheat page URL."""
    match = PLAYGROUND_ENTRY_RE.match(urlsplit(url).path.rstrip("/"))
    return match.group(1) if match else None


def parse_playground_category(html: str, slug: str) -> tuple[dict[str, str | None], int]:
    """Map this game's `table` entries to their publication timestamps.

    Returns the table entries and how many entries of any kind the listing
    carried, because those are different answers: a game with no tables at all
    is not the same as a listing this parser could not read.

    The category page is not table-only. When a game has no Cheat Engine table,
    Playground still lists its trainers, save files and save editors here, and
    each entry declares its real category next to its own timestamp. That link
    is the only language-independent marker of what an entry actually is.

    The listing is also the only place that carries a machine-readable date per
    entry; the item page's own ``<time>`` elements belong to its comments.
    """
    soup = BeautifulSoup(html, "html.parser")

    def entry_url(link: object) -> str | None:
        candidate = canonical_url(urljoin(f"{PLAYGROUND_BASE_URL}{slug}/cheat/", str(link["href"])))
        parts = urlsplit(candidate)
        if (parts.hostname or "").casefold() not in PROVIDER_HOSTS["playground"]:
            return None
        match = PLAYGROUND_ENTRY_RE.match(parts.path.rstrip("/"))
        return candidate if match and match.group(1) == slug else None

    def declared_category(link: object) -> str | None:
        """The category this one entry belongs to, or nothing when unclear.

        Only the entry's own container answers this. An ancestor holding a second
        entry is the shared listing, and one offering several categories is the
        page's category navigation; both are unknown rather than guessed, and an
        unknown entry stays listed instead of being dropped.
        """
        node = link
        for _ in range(MAX_PLAYGROUND_ENTRY_DEPTH):
            node = getattr(node, "parent", None)
            if node is None:
                return None
            categories: set[str] = set()
            # A post links its own entry more than once - the cover image and the
            # title - so distinct entries are what separates one post from the
            # shared listing around it.
            siblings: set[str] = set()
            for candidate in node.find_all("a", href=True):
                sibling = entry_url(candidate)
                if sibling is not None:
                    siblings.add(sibling)
                    continue
                path = urlsplit(urljoin(f"{PLAYGROUND_BASE_URL}{slug}/cheat/", str(candidate["href"]))).path.rstrip("/")
                match = PLAYGROUND_CATEGORY_RE.match(path)
                if match and match.group(1) == slug:
                    categories.add(match.group(2))
            if len(siblings) > 1 or len(categories) > 1:
                return None
            if len(categories) == 1:
                return next(iter(categories))
        return None

    pages: dict[str, str | None] = {}
    entries = 0
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        url = entry_url(link)
        if url is None or url in seen:
            continue
        seen.add(url)
        entries += 1
        category = declared_category(link)
        if category is None or category == PLAYGROUND_TABLE_CATEGORY:
            pages.setdefault(url, None)

    for element in soup.find_all("time", attrs={"datetime": True}):
        posted = _iso(str(element.get("datetime")))
        if posted is None:
            continue
        node = element
        for _ in range(MAX_PLAYGROUND_ENTRY_DEPTH):
            node = node.parent
            if node is None:
                break
            link = node.find("a", href=True)
            urls = [entry_url(candidate) for candidate in node.find_all("a", href=True)]
            found = next((url for url in urls if url is not None), None)
            if found is not None:
                # Only date an entry this listing kept; a timestamp must never
                # reinstate one the category filter already excluded.
                if found in pages and pages[found] is None:
                    pages[found] = posted
                break
            if link is None:
                continue
    return pages, entries


def parse_playground_page(html: str, page_url: str, score: float) -> tuple[list[ArtifactRecord], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    title = _text((soup.select_one("h1") or soup.title).get_text(" ", strip=True) if (soup.select_one("h1") or soup.title) else page_url)
    page_match = PLAYGROUND_ID_RE.search(urlsplit(page_url).path.rstrip("/"))
    topic_id = page_match.group(1) if page_match else sha256(page_url.encode()).hexdigest()[:20]
    records: list[ArtifactRecord] = []
    for node in soup.select("pg-file[data-file-id]"):
        filename = _text(node.get("data-name"), 1024)
        if PurePosixPath(filename).suffix.casefold() not in SUPPORTED_SUFFIXES:
            continue
        file_id = _text(node.get("data-file-id"), 128)
        if not file_id:
            continue
        size_raw = _text(node.get("data-size"), 32)
        count_raw = _text(node.get("data-download-count"), 32)
        digest = _text(node.get("data-hash"), 64).casefold()
        digest = digest if SHA256_RE.fullmatch(digest) else None
        post = node.find_parent(class_="js-post")
        post_id = _text(post.get("data-post-id"), 32) if post is not None else ""
        direct_request = (
            PlaygroundDownloadRequest(file_id, post_id)
            if PLAYGROUND_IDENTITY_RE.fullmatch(file_id) and PLAYGROUND_IDENTITY_RE.fullmatch(post_id)
            else None
        )
        result = catalog_result_or_none(
            provider="playground",
            provider_display_name="Playground",
            topic_id=topic_id,
            artifact_id=f"page-{topic_id}:file-{file_id}",
            table_title=title,
            filename=filename,
            # The page's own heading is this attachment's nearby text, which is
            # where a release is written when the file name does not carry one.
            version=_version(filename, title),
            size_bytes=int(size_raw) if size_raw.isdigit() else _size(size_raw),
            source_page=page_url,
            download_mode="direct_https" if direct_request is not None else "system_browser_downloads",
            match_score=score,
            provider_rank=85,
            download_count=int(count_raw) if count_raw.isdigit() else None,
        )
        if result is None:
            continue
        records.append(ArtifactRecord(
            result,
            advertised_sha256=digest,
            # Playground's integer data-size is display metadata rather than a
            # byte-accurate integrity promise: target evidence observed 24,310
            # advertised for a valid 24,309-byte exact-SHA artifact.
            size_exact=False,
            acquisition=direct_request,
        ))
    source = None
    for link in soup.find_all("a", href=True):
        if _text(link.get_text(" ", strip=True)).casefold() in {"источник", "source"}:
            source = canonical_url(urljoin(page_url, str(link["href"])))
            break
    return records, source


def _json_object(data: bytes) -> dict[str, object]:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Playground API response contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=unique_pairs)
    except (UnicodeDecodeError, ValueError) as exc:
        detail = str(exc) if "duplicate keys" in str(exc) else "Playground API returned invalid JSON"
        raise NetworkError(detail) from exc
    if not isinstance(value, dict):
        raise NetworkError("Playground API response is not an object")
    return value


def _countdown(value: object) -> int:
    if value is None or value is False:
        return 0
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_PLAYGROUND_COUNTDOWN_SECONDS:
        raise NetworkError("Playground API returned an invalid countdown")
    return value


async def resolve_playground_download(
    network: NetworkClient,
    record: ArtifactRecord,
    *,
    sleep: Callable[[float], Awaitable[object]],
    on_countdown: Callable[..., None] | None = None,
) -> str:
    request = record.acquisition
    if record.result.provider != "playground" or not isinstance(request, PlaygroundDownloadRequest):
        raise ValueError("artifact has no Playground direct-download request")
    params = {
        "file_id": request.file_id,
        "post_id": request.post_id,
        "file_name": record.result.filename,
    }

    async def fetch(payload: dict[str, str], *, restartable: bool) -> dict[str, object]:
        url = f"{PLAYGROUND_BASE_URL}api/file.download?" + urlencode(payload)
        response = await network.get(
            url,
            allowed_hosts=PROVIDER_HOSTS["playground"],
            max_bytes=MAX_PLAYGROUND_API_BYTES,
            headers={"Accept": "application/json"},
            referer=record.result.source_page,
            retries=0,
        )
        if response.status == 429:
            raise ProviderRateLimited(
                response.headers.get("retry-after"),
                "Playground API returned HTTP 429",
                restartable=restartable,
            )
        if response.status != 200:
            raise NetworkError(f"Playground API returned HTTP {response.status}")
        return _json_object(response.body)

    # The opening request spends nothing: it is asked again from the start, so a
    # rate limit here is a wait like any other. The lock exchange below is the
    # opposite - its token is issued once and the countdown has already been
    # served - so a limit there ends the attempt rather than replaying it.
    response = await fetch(params, restartable=True)
    wait_seconds = _countdown(response.get("wait_time", 0))
    if wait_seconds:
        lock_hash = response.get("lock_hash")
        if not isinstance(lock_hash, str) or not 1 <= len(lock_hash.encode("utf-8")) <= 512 or any(ord(char) < 0x20 for char in lock_hash):
            raise NetworkError("Playground API returned an invalid countdown token")
        if on_countdown is not None:
            on_countdown(wait_seconds)
        await sleep(wait_seconds)
        response = await fetch({**params, "lock_hash": lock_hash, "log": "0"}, restartable=False)
        if _countdown(response.get("wait_time", 0)) != 0:
            raise NetworkError("Playground API repeated its countdown")
    link = response.get("download_link")
    if not isinstance(link, str) or not link:
        raise NetworkError("Playground API returned no direct download link")
    return urljoin(record.result.source_page, link)


def parse_github_releases(payload: object, repo: str, score: float) -> list[ArtifactRecord]:
    """Rows for one repository's release assets.

    The repository is passed as the identity it is, not recovered from a URL.
    It was read back out of whichever page URL the caller had, so a repository
    that declared a page somewhere else took its rows' `topic_id` and
    `artifact_id` from that URL: the identity the panel selects by, and the key
    the result list dedupes on, stopped naming the repository the table is in.
    """
    if not isinstance(payload, list) or len(payload) > 100:
        raise ValueError("GitHub releases payload is invalid")
    definition = provider_definition("github")
    repo_url = github_repo_page_url(repo)
    rows: list[ArtifactRecord] = []
    for release in payload:
        if not isinstance(release, dict) or release.get("draft") is True:
            continue
        assets = release.get("assets", [])
        if not isinstance(assets, list) or len(assets) > 1000:
            raise ValueError("GitHub release assets are invalid")
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = _text(asset.get("name"), 1024)
            if PurePosixPath(name).suffix.casefold() not in SUPPORTED_SUFFIXES:
                continue
            asset_id = asset.get("id")
            url = asset.get("browser_download_url")
            if isinstance(asset_id, bool) or not isinstance(asset_id, int) or not isinstance(url, str):
                continue
            if not github_is_provider_asset(url):
                # The host policy would refuse this at transfer time anyway, so
                # offering it only spends the user's press finding that out.
                _note_parse_issue("row skipped: release asset is not served by this provider")
                continue
            digest_raw = asset.get("digest")
            digest = digest_raw[7:].casefold() if isinstance(digest_raw, str) and digest_raw.casefold().startswith("sha256:") else None
            digest = digest if digest and SHA256_RE.fullmatch(digest) else None
            page = release.get("html_url") if github_is_provider_page(release.get("html_url")) else repo_url
            result = catalog_result_or_none(
                provider=definition.provider,
                provider_display_name=definition.provider_display_name,
                topic_id=repo,
                artifact_id=f"{repo}:asset-{asset_id}",
                table_title=_text(release.get("name")) or _text(release.get("tag_name")) or repo,
                filename=name,
                version=_text(release.get("tag_name"), 256) or None,
                size_bytes=asset.get("size") if isinstance(asset.get("size"), int) else None,
                source_page=canonical_url(page),
                download_mode="direct_https",
                match_score=score,
                provider_rank=definition.priority,
                author=None,
                posted_at=_iso(release.get("published_at") if isinstance(release.get("published_at"), str) else None),
                download_count=asset.get("download_count") if isinstance(asset.get("download_count"), int) else None,
            )
            if result is None:
                continue
            if not acquirable_size(result.size_bytes, size_exact=True):
                _note_parse_issue("row skipped: artifact is larger than this can download")
                continue
            rows.append(ArtifactRecord(result, direct_url=canonical_url(url), advertised_sha256=digest, size_exact=True))
    return rows


def github_tree_record(
    candidate: GitHubRepoCandidate,
    table: GitHubTreeTable,
    score: float,
) -> ArtifactRecord | None:
    """One table committed in a repository tree.

    The row carries the git object name rather than a content digest, because
    that is what the tree API advertises and it is exactly reproducible from the
    downloaded bytes. A repository publishes no author for a committed file and
    that field stays empty; the release it does sometimes publish is in the path
    the file is committed at, which is read the same way every other source's
    is rather than left blank for this one alone.
    """
    definition = provider_definition("github")
    posted_at = _iso(candidate.pushed_at)
    if candidate.pushed_at and posted_at is None:
        _note_parse_issue("description dropped: repository push date is invalid")
    if candidate.default_branch is None:
        _note_parse_issue("row skipped: repository names no usable default branch")
        return None
    try:
        direct_url = github_raw_url(candidate.full_name, candidate.default_branch, table.path)
    except ValueError as exc:
        _note_parse_issue(f"row skipped: {exc}")
        return None
    result = catalog_result_or_none(
        provider=definition.provider,
        provider_display_name=definition.provider_display_name,
        topic_id=candidate.full_name,
        artifact_id=f"{candidate.full_name}:blob-{table.blob_sha1}",
        table_title=table.filename,
        filename=table.filename,
        # A tree entry has no release tag, so its name and the path it is
        # committed at are what it publishes: `tables/v1.4/Ducks.CT` says the
        # release in the directory and nowhere else. Read the same way every
        # other source's is, because a version shown for one and withheld for
        # another tells the user this table has none rather than that this
        # source did not say.
        version=_version(table.filename, table.path),
        size_bytes=table.size_bytes,
        source_page=canonical_url(candidate.html_url),
        download_mode="direct_https",
        match_score=score,
        provider_rank=definition.priority,
        posted_at=posted_at,
        notes=f"committed in {candidate.default_branch}" if candidate.default_branch else None,
    )
    if result is None:
        return None
    if not acquirable_size(result.size_bytes, size_exact=table.size_bytes is not None):
        _note_parse_issue("row skipped: artifact is larger than this can download")
        return None
    return ArtifactRecord(
        result,
        direct_url=direct_url,
        blob_sha1=table.blob_sha1,
        size_exact=table.size_bytes is not None,
    )


def vgtimes_record(
    ref: VGTimesItemRef,
    metadata: VGTimesItemMetadata,
    score: float,
) -> ArtifactRecord | None:
    """One table entry, from the page's own structured data.

    An entry that does not declare the table category is a trainer, a save or a
    save editor, and is dropped rather than offered as something it is not. The
    filename is read rather than derived because the site serves `.rar` as well
    as `.zip`, and a row has to say which before the user chooses it.
    """
    if not metadata.is_table:
        _note_parse_issue("row skipped: entry is not a Cheat Engine table")
        return None
    try:
        request = VGTimesDownloadRequest(ref.url, ref.file_id)
    except ValueError as exc:
        _note_parse_issue(f"row skipped: {exc}")
        return None
    posted_at = _iso(metadata.published)
    if metadata.published and posted_at is None:
        _note_parse_issue("description dropped: entry date is invalid")
    definition = provider_definition("vgtimes")
    # The site files each entry inside the game's own section, so its title
    # routinely names no game at all: "a table for Cheat Engine" is a complete
    # entry name there, and on a screen that merges five sources into one list
    # it reads as a table for something else entirely. The category is the
    # page's own breadcrumb, game first, so naming it here derives nothing.
    table_title = metadata.title
    game_title = metadata.game_title
    if game_title and game_title.casefold() not in table_title.casefold():
        table_title = f"{game_title}: {table_title}"
    result = catalog_result_or_none(
        provider=definition.provider,
        provider_display_name=definition.provider_display_name,
        topic_id=ref.game_slug,
        artifact_id=vgtimes_artifact_id(ref.game_slug, ref.file_id),
        table_title=table_title,
        filename=metadata.filename or "",
        # No tag here either, so everything this source does publish about the
        # file is read: its name, the title above it, and the description beside
        # it, in that order of authority.
        version=_version(metadata.filename or "", table_title, metadata.description),
        size_bytes=metadata.size_bytes,
        source_page=ref.url,
        # The plugin fetches this itself. A handoff would ask a user with no
        # keyboard and no mouse to drive a web page, and the site does not
        # require one.
        download_mode="direct_https",
        match_score=score,
        provider_rank=definition.priority,
        author=metadata.author,
        posted_at=posted_at,
        # Nothing: the category's job is deciding whether an entry is a table at
        # all, and the row already carries the game it names. Shown, it led every
        # VGTimes row with the site's whole breadcrumb, repeating that game and
        # adding three words of site navigation before anything about the file.
        notes=None,
    )
    if result is None:
        return None
    if not acquirable_size(result.size_bytes, size_exact=metadata.size_bytes is not None):
        _note_parse_issue("row skipped: artifact is larger than this can download")
        return None
    return ArtifactRecord(
        result,
        advertised_sha256=metadata.advertised_sha256,
        # Published in the open beside the file and needed before the import: an
        # archive nobody can open is not a table the user can use. It stays
        # backend-owned, like every other provider hint.
        password_hint=metadata.archive_password,
        size_exact=metadata.size_bytes is not None,
        acquisition=request,
    )


def thecheatscript_record(
    ref: TheCheatScriptPostRef,
    artifact: TheCheatScriptPostArtifact,
    metadata: object,
    score: float,
) -> ArtifactRecord | None:
    """Normalize one supported post link after Yandex describes its bytes.

    The public-resource response owns the exact filename, size and digest. The
    post text is only a fallback for version/build description, so a malformed
    description cannot cost an otherwise obtainable artifact.
    """
    if not artifact.supported:
        _note_parse_issue("row skipped: unsupported legacy artifact host")
        return None
    try:
        request = TheCheatScriptDownloadRequest(artifact.link)
        public = parse_yandex_public(metadata)
    except (ValueError, TypeError) as exc:
        _note_parse_issue(f"row skipped: {exc}")
        return None
    build, version = public.version_tokens
    posted_at = _iso(ref.lastmod)
    if ref.lastmod and posted_at is None:
        _note_parse_issue("description dropped: provider post date is invalid")
    definition = provider_definition("thecheatscript")
    result = catalog_result_or_none(
        provider=definition.provider,
        provider_display_name=definition.provider_display_name,
        topic_id=thecheatscript_post_slug(ref.url),
        artifact_id=thecheatscript_artifact_id(ref.url, artifact.link),
        table_title=ref.title,
        filename=public.filename,
        version=version or artifact.table_version,
        size_bytes=public.size_bytes,
        source_page=ref.url,
        download_mode="direct_https",
        match_score=score,
        provider_rank=definition.priority,
        posted_at=posted_at,
        notes=f"game build {build or artifact.game_build}" if build or artifact.game_build else None,
    )
    if result is None:
        return None
    if not acquirable_size(result.size_bytes, size_exact=True):
        _note_parse_issue("row skipped: artifact is larger than this can download")
        return None
    return ArtifactRecord(
        result,
        advertised_sha256=public.advertised_sha256,
        size_exact=True,
        acquisition=request,
    )


class CatalogService:
    def __init__(
        self, network: NetworkClient, cache_path, diagnostics=None, logger=None,
        disabled_providers: Callable[[], Iterable[str]] | None = None,
        rar_openable: Callable[[], bool] | None = None,
    ) -> None:
        self.network = network
        self.cache_path = cache_path
        self.diagnostics = diagnostics
        self.logger = logger
        # Which sources this user switched off. A callable rather than a set,
        # because the answer changes under a long-lived service the moment
        # Advanced writes it, and a set captured at construction would keep
        # searching a source the user has just turned off until the next reload.
        self._disabled_providers = disabled_providers
        # Whether this device's 7-Zip has a RAR handler. A callable for the same
        # reason as the selection above: the answer comes from running 7-Zip, so
        # it is asked once per search rather than once per row, and a service
        # built without one offers `.rar` as it would any other archive.
        self._rar_openable = rar_openable
        # Interactive searches in flight, and whether new ones are refused.
        # Deleting the cache quiesced the background listing crawl and the
        # downloads but not these, so a search the user had closed could finish
        # around the deletion and write the results cache, the provider
        # diagnostics and its own snapshot authority straight back.
        self._search_tasks: set[asyncio.Task] = set()
        self._searches_suspended = False
        # What the search running right now is doing, for the screen that is
        # waiting on it. A search is tens of seconds of somebody else's network
        # on a device with no way to see any of it, and the panel could say only
        # how long it had been waiting: on this device one search took 45
        # seconds, of which Playground was 44.8 and 37 of those were its own
        # 11.9 MB site index, while the other four sources had all answered
        # inside three seconds. The newest search is what
        # the screen reads; an older one still running carries its own number
        # and cannot write into the record that replaced it.
        self._search_progress: _SearchProgress | None = None
        self._search_generation = 0
        self.artifacts: dict[tuple[str, str], ArtifactRecord] = {}
        self._searches: dict[str, dict[tuple[str, str], ArtifactRecord]] = {}
        (
            self._fearless_pages,
            self._fearless_total_pages,
            self._fearless_total_topics,
            self._fearless_fetched,
            self._fearless_page_step,
            self._searched_at,
        ) = self._load_fearless_index()
        # The marker has two homes and the newest of them is the answer. The
        # index cache carries it for the ordinary path, where a search that was
        # already writing pages records it for free; the small file beside it
        # carries the searches that never reached the index, which is every one
        # the unload prologue had to write while a crawl worker held the index
        # lock. Reading only the index lost exactly those.
        self._searched_at = max(self._searched_at, self._load_search_marker())
        # What the recorded marker already says, so an ordinary search does not
        # rewrite a third of a megabyte to record something it already holds.
        self._searched_persisted_at = self._searched_at
        self._fearless_last_refresh_at: float | None = None
        self._fearless_last_refresh_pages = 0
        self._fearless_index_task: asyncio.Task[None] | None = None
        # When the last background pass over the listing began, and when the
        # last interactive search finished. The pass is paced by the first and
        # gives way to the second: both are asking the same provider, and only
        # one of them has somebody waiting on it.
        #
        # Monotonic, because both are elapsed time inside this one process and
        # never a moment anything else has to agree with. This device's clock is
        # corrected: the install helper treats a persisted moment in its own
        # future as a clock that moved, the Search marker is refused when it is
        # one, and the panel measures every bounded wait on a clock that cannot
        # be moved. A correction of an hour backwards would otherwise hold the
        # next pass for ninety minutes of real time and make a search that
        # finished an hour ago look like one that is still running; forwards,
        # both gates open at once and the crawl competes with the search it is
        # supposed to give way to.
        self._fearless_index_ran_at: float | None = None
        self._last_search_at = 0.0
        # When this process last established that its copy of Playground's site
        # index is current, whether by fetching one or by being told the copy it
        # has is. A 304 proves the copy is current and rewriting eleven
        # megabytes to record that would cost more than the request did, so it
        # is remembered here instead; a fresh process asks once and then not
        # again for the life of the answer.
        self._playground_index_checked_at: float | None = None
        # The one refresh of Playground's site index that may be in flight
        # behind a search, started when the conditional request for it did not
        # answer inside its own few seconds.
        self._playground_index_task: asyncio.Task[None] | None = None
        self._fearless_index_writes: set[asyncio.Task[None]] = set()
        self._fearless_index_error: str | None = None
        self._fearless_cooldown_until = 0.0
        # A repository-search refusal, and when it runs out.
        #
        # GitHub meters repository search apart from the rest of its API, so an
        # exhausted search bucket says nothing about the release and tree reads
        # that actually produce the rows, and nothing about the index already on
        # this disk. Written as a provider cooldown it would refuse both, which
        # is the whole source, for a budget only one of its three routes spends.
        # So it is held here instead, scoped to the route that was refused, and
        # the provider's own cooldown stays what it is: the deadline for GitHub
        # as a source.
        self._github_search_refusal: ProviderRateLimited | None = None
        self._github_search_cooldown_until = 0.0
        self._fearless_index_lock = asyncio.Lock()
        # The index cache has more than one writer and they do not share a
        # thread: a crawl writes it from an `asyncio.to_thread` worker, and the
        # unload prologue writes it from the thread the unload runs on, because
        # by then there is no loop left to await on. `atomic_write_json` makes
        # each replacement atomic and says nothing about which of two of them
        # lands last, so a worker that started earlier could replace the
        # prologue's newer snapshot with its older one and take the search
        # marker with it.
        #
        # A thread lock rather than the `asyncio.Lock` above, because the
        # prologue cannot await. The generation is what makes it an order rather
        # than only a queue: a snapshot older than what is already on disk is
        # dropped instead of written.
        self._index_write_lock = threading.Lock()
        self._index_generation = 0
        self._index_written_generation = 0
        # The one task that looks, on its own, at whether a listing pass is
        # owed. Everything it can start is started by a search too; what it adds
        # is that a device nobody is searching on still refreshes its index.
        self._index_schedule_task: asyncio.Task[None] | None = None
        # While this is set the background index neither starts nor is restarted.
        # A pre-pass that only stops the current task is not enough: an ordinary
        # search between the stop and the delete starts a new one, and its write
        # restores the file moments after the user asked for it to be gone.
        self._index_suspended = False

    def search_progress(self, token: str) -> dict[str, object] | None:
        """What the caller's own search is doing, or nothing.

        Read-only and cheap, because the screen that shows it asks every couple
        of seconds while it waits. It is deliberately a separate read rather
        than something the search itself returns: the search returns once, at
        the end, which is the moment this stops being of any use.

        Answered only to the caller whose search this is. Nothing here forbids
        two searches at once, and the panel serializing its own presses is not
        that guarantee, so a record with one owner and no name would be read by
        whoever asked last.
        """
        progress = self._search_progress
        if progress is None or progress.token is None or progress.token != token:
            return None
        return progress.as_dict()

    def _begin_search_progress(self, jobs: list[str], skipped: list[str]) -> None:
        self._search_generation += 1
        generation = self._search_generation
        _SEARCH_GENERATION.set(generation)
        sources: dict[str, _SearchSourceProgress] = {}
        for definition in searchable_providers():
            provider = definition.provider
            if provider not in jobs and provider not in skipped:
                continue
            off = provider in skipped
            sources[provider] = _SearchSourceProgress(
                provider=provider,
                name=definition.provider_display_name,
                # A source the user switched off is not waiting on anything, and
                # saying so is the answer to "why is this one not in the list".
                state="off" if off else "running",
                stage=None if off else "asking the source",
            )
        self._search_progress = _SearchProgress(
            generation=generation, token=_SEARCH_TOKEN.get(),
            started_at=time.monotonic(), running=True, sources=sources,
        )

    def _current_search_progress(self) -> "_SearchProgress | None":
        """The record this caller's own search owns, if it is still the newest."""
        progress = self._search_progress
        if progress is None or progress.generation != _SEARCH_GENERATION.get():
            return None
        return progress

    def _note_search_stage(self, provider: str, stage: str) -> None:
        """Say what one source is doing, in the words the panel will show.

        Never a failure path: a search that cannot describe itself still has to
        return its tables, so this only ever writes into a record the panel may
        or may not be reading.
        """
        progress = self._current_search_progress()
        if progress is None or not progress.running:
            return
        source = progress.sources.get(provider)
        if source is not None and source.state == "running":
            source.stage = stage[:120]

    def _finish_search_provider(self, provider: str, *, error: bool) -> None:
        progress = self._current_search_progress()
        if progress is None:
            return
        source = progress.sources.get(provider)
        if source is None:
            return
        source.state = "failed" if error else "done"
        source.stage = None

    def _end_search_progress(self) -> None:
        progress = self._current_search_progress()
        if progress is not None:
            progress.running = False

    def disabled_provider_ids(self) -> frozenset[str]:
        """The sources this user switched off, or none when that is unknowable.

        Deliberately fail-open, exactly as an unreadable blocklist refuses
        nothing: reading a corrupt preference as "every source is off" would
        leave a search finding nothing at all with no visible cause. The reason
        is not swallowed - the service that owns the record reports it to
        Advanced, the diagnostics snapshot and the support bundle.
        """
        if self._disabled_providers is None:
            return frozenset()
        try:
            values = self._disabled_providers()
            return frozenset(value for value in values if isinstance(value, str))
        except Exception as exc:  # noqa: BLE001 - a preference must not fail a search
            log_failure(
                self.logger, "catalog.provider_selection_unreadable", exc, expected=True,
            )
            return frozenset()

    async def suspend_searches(self) -> None:
        """Refuse new searches and drain the ones already running.

        Deleting the provider cache promises that what is on disk goes and is
        rebuilt by the next search. A search already in flight breaks that
        promise from the other end: the user closes the screen, deletes the
        cache, and the search that was still running writes the results file,
        the provider diagnostics and its own artifact authority back, so the
        rebuild is from the search that preceded the deletion.

        Cancelling is correct rather than merely convenient here: the person
        watching asked for that state to be gone, and every search path already
        treats cancellation as the operation owner's to see rather than as a
        provider failure.
        """
        self._searches_suspended = True
        tasks = tuple(self._search_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            log_activity(
                self.logger, "info", "catalog.searches_suspended", searches=len(tasks),
            )

    def resume_searches(self) -> None:
        """Allow searches again. Always paired with suspend."""
        self._searches_suspended = False

    async def suspend_index(self) -> None:
        """Stop the background index and refuse to restart it until resumed.

        The listing index is held in memory as well as on disk, and a refresh in
        flight writes it back. Stopping the current task alone is a pre-pass an
        ordinary search undoes: deletion has to hold the index down across the
        whole operation, not just up to the moment the files go.
        """
        self._index_suspended = True
        await self.discard_cached_index()

    def resume_index(self) -> None:
        """Allow the background index to run again. Always paired with suspend."""
        self._index_suspended = False

    async def discard_cached_index(self) -> None:
        """Quiesce the background index and forget everything it holds."""
        tasks = [task for task in (self._fearless_index_task, self._playground_index_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._fearless_index_writes:
            await asyncio.gather(*tuple(self._fearless_index_writes), return_exceptions=True)
        self._fearless_index_task = None
        self._playground_index_task = None
        self._playground_index_checked_at = None
        # Nothing is left to pace: the index this would have spaced passes over
        # is gone, and the next search is what has to rebuild it.
        self._fearless_index_ran_at = None
        self._fearless_pages = {}
        self._fearless_page_step = None
        self._fearless_total_pages = None
        self._fearless_total_topics = None
        self._fearless_fetched = {}
        self._fearless_last_refresh_at = None
        self._fearless_last_refresh_pages = 0
        self._fearless_index_error = None
        self._fearless_cooldown_until = 0.0
        # The marker is about an index that no longer exists, and both of the
        # files it is written to are in the cache directory being emptied. The
        # next search is what rebuilds all of it.
        self._searched_at = 0.0
        self._searched_persisted_at = 0.0
        log_activity(self.logger, "info", "catalog.index_discarded")

    def forget_searches(self) -> None:
        """Drop the per-search artifact snapshots that point at deleted bytes."""
        self._searches.clear()

    def begin_close(self) -> None:
        """Everything this service owes that must not wait for an `await`.

        Decky stops a plugin by signalling it and then closing its socket, and
        `docs/FIELD_NOTES.md` records what that does: both ends of that socket
        read at EOF in a loop that never suspends, the loop is starved, and
        nothing this process has suspended runs again before SIGKILL five
        seconds later. So the unload's synchronous prologue is the only part of
        it that reliably happens, and what must happen goes here.

        For this service that is the search marker, and by the time a stop
        arrives it is usually already on disk: the search writes it as it
        happens, into the small file that exists for it. What is left for the
        prologue is the search whose own write failed, on a device that was full
        or read-only for that moment, and which the scheduler would have offered
        again in five minutes it is not going to get. So this stays, and it is
        the same write: one atomic replacement of a few dozen bytes, taking no
        lock, which is what this thread can finish before it is killed.

        It is deliberately not the index cache. That is a third of a megabyte
        behind a lock a crawl worker holds across its own `fsync`, so a prologue
        writing it had to be bounded, and a bounded write is one that can
        decline to happen.
        """
        try:
            written = self._persist_search_marker()
        except Exception as exc:  # noqa: BLE001 - unload never fails on a cache write
            log_failure(self.logger, "fearless.search_marker_not_persisted", exc, expected=True)
            return
        if written:
            log_activity(self.logger, "info", "fearless.search_marker_persisted", phase="begin_close")

    async def close(self) -> None:
        schedule = self._index_schedule_task
        self._index_schedule_task = None
        candidates = (self._fearless_index_task, self._playground_index_task, schedule)
        had_task = any(task is not None for task in candidates)
        live = self._drainable(candidates)
        # Written before anything is waited on, because everything below it is a
        # wait and a wait is what Decky's five second stop budget kills silently.
        if had_task or self._fearless_index_writes:
            log_activity(
                self.logger, "info", "catalog.close_started",
                live_tasks=len(live), held_tasks=sum(task is not None for task in candidates),
                cache_writes=len(self._fearless_index_writes),
            )
        cancelled = False
        for task in live:
            task.cancel()
        if live:
            # The last record this close writes without suspending. What follows
            # it is the first real await of the whole unload, so if the journal
            # ends here the answer is about the await rather than about the
            # cancellation above it.
            log_activity(self.logger, "info", "catalog.index_tasks_cancelled", tasks=len(live))
            cancelled = await drain_through_cancellation(
                asyncio.gather(*live, return_exceptions=True),
                label="catalog.index_tasks", logger=self.logger, watching=live,
            )
        # After the scheduler is stopped, because this is the write that
        # scheduler owed and will now never make. Synchronous, like every other
        # write of this marker, so it cannot be the await an unload dies at.
        self._persist_owed_search_marker()
        pending_writes = len(self._fearless_index_writes)
        writes = self._drainable(tuple(self._fearless_index_writes))
        if writes:
            # Each pending write is a native worker Python cannot stop; unload
            # cancellation is not permission to orphan one mid-file.
            cancelled = await drain_through_cancellation(
                asyncio.gather(*writes, return_exceptions=True),
                label="catalog.cache_writes", logger=self.logger, watching=writes,
            ) or cancelled
        if had_task or pending_writes:
            log_activity(
                self.logger, "info", "catalog.close_completed",
                index_task=had_task, cache_writes=pending_writes, drained=len(live) + len(writes),
            )
        if cancelled:
            raise asyncio.CancelledError

    def _persist_owed_search_marker(self) -> None:
        """Write down a search that armed the background pass and never reached disk.

        Normally there is nothing owed by the time an orderly close reaches
        this: the search wrote the marker as it happened, and on the device the
        unload prologue has already run. It is here for the two states that are
        left. One is a search whose own write failed on a device that was full
        for that moment, which the scheduler would have offered again and now
        never will. The other is a caller that reached `close()` without the
        prologue, which is a test or a developer helper rather than the plugin.

        It never raises: a diagnostics-grade cache write is not a reason to fail
        unload.
        """
        try:
            if not self._persist_search_marker():
                return
        except Exception as exc:  # noqa: BLE001 - unload never fails on a cache write
            log_failure(self.logger, "fearless.search_marker_not_persisted", exc, expected=True)
            return
        log_activity(self.logger, "info", "fearless.search_marker_persisted")

    @staticmethod
    def _drainable(tasks: tuple[asyncio.Task | None, ...]) -> list[asyncio.Task]:
        """The tasks this loop can actually wait on, out of the ones held.

        On the device there is one loop for the whole plugin process and this
        filter removes nothing: `docs/FIELD_NOTES.md` records Decky's own
        `run_forever` and the reading that confirms it, and the scheduler
        started during load is still pending on that loop at unload.

        It is here for every other caller. A test, a developer helper or a probe
        may construct this service, run something on one loop and close it from
        another, and a task belonging to a loop that is closed, or merely to a
        different one, cannot be made to run again by anything this coroutine
        does. Handing one to `asyncio.gather` asks that other loop to schedule a
        callback, which on Python 3.11, the version the authoritative CI gate
        runs and the version Decky ships on the device, is `RuntimeError: Event
        loop is closed` raised out of `close()`. Python 3.13 takes a shortcut
        for a future that is already done and hides it.

        So the drain list is what this loop started and can still stop.
        Everything else is already quiesced, and waiting on it is neither
        possible nor owed.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - close() is always awaited
            return []
        return [
            task for task in tasks
            if task is not None and not task.done() and task.get_loop() is running
        ]

    async def search(
        self, display_name: str, executable: str | None = None, progress_token: str | None = None,
    ) -> dict[str, object]:
        # Set before anything else this search does, so that every record it
        # begins, including the second one an install-directory retry begins,
        # carries the name its own caller will ask for it by.
        _SEARCH_TOKEN.set(progress_token)
        if self._searches_suspended:
            raise ValueError("plugin data is being deleted; search again when it finishes")
        # This coroutine owns no task of its own: it runs inside the operation
        # the RPC created. Registering that task is what lets a deletion cancel
        # and drain a search somebody closed the screen on, which would
        # otherwise finish afterwards and write the cache back.
        task = asyncio.current_task()
        if task is not None:
            self._search_tasks.add(task)
        try:
            return await self._search(display_name, executable)
        finally:
            if task is not None:
                self._search_tasks.discard(task)
            # What the background pass waits out. Recorded on the way out of
            # every search, including one that failed, because a provider that
            # has just refused a search is the last one to ask again.
            self._last_search_at = time.monotonic()

    async def _search(self, display_name: str, executable: str | None = None) -> dict[str, object]:
        game_aliases = aliases(display_name, executable)
        queries = query_plan(display_name, executable)
        deadline = time.monotonic() + PROVIDER_SEARCH_BUDGET_SECONDS
        # Read once for the whole search, so the jobs that ran, the cached rows
        # re-served and the roster the panel is shown all describe the same set
        # of sources even if Advanced writes a new choice while this runs.
        disabled = self.disabled_provider_ids()
        with searching_with_selection(disabled), searching_with_rar_support(self.rar_openable()):
            return await self._search_with(game_aliases, queries, display_name, executable, deadline, disabled)

    def rar_openable(self) -> bool:
        """Whether a `.rar` row can be offered on this device at all.

        Advisory in the safe direction: a probe that cannot be run leaves the
        row offered, because hiding tables a device may well be able to open is
        the worse of the two mistakes and the archive layer refuses it again,
        with the same reason, if it turns out it cannot.
        """
        if self._rar_openable is None:
            return True
        try:
            return bool(self._rar_openable())
        except Exception as exc:  # noqa: BLE001 - a probe never fails a search
            log_failure(self.logger, "catalog.rar_probe_failed", exc, expected=True)
            return True

    async def _search_with(
        self, game_aliases: list[str], queries: list[str], display_name: str,
        executable: str | None, deadline: float, disabled: frozenset[str],
    ) -> dict[str, object]:
        records, failures = await self._collect(
            game_aliases, queries, deadline=deadline, disabled=disabled,
        )
        # The library entry's name is the only identity used while it finds
        # something. Only when it finds nothing usable is the directory the game
        # was installed into tried, because a repack names that folder after the
        # whole title and a user abbreviates or mistypes the entry. Deciding this
        # on results rather than in advance keeps a folder that names a different
        # game - a mod inside its base game's directory - from ever outranking
        # the entry the user chose.
        retry = install_directory_title(executable)
        if retry and retry not in game_aliases and not any(
            row.result.download_mode in AUTOMATIC_DOWNLOAD_MODES for row in records
        ):
            # The fallback spends what is left of the one budget, never a second
            # one: two passes each given the full allowance is the same source
            # holding a controller-visible search for twice as long. It needs
            # enough of that budget left to be worth starting, though: with a
            # sliver of it every job in the second pass times out at once, and
            # the user is shown a screen of failed sources where the first pass
            # had simply found nothing.
            if deadline - time.monotonic() >= PROVIDER_SEARCH_RETRY_MINIMUM_SECONDS:
                retried_records, retried_failures = await self._collect(
                    [retry], [retry], deadline=deadline, disabled=disabled,
                )
                if any(row.result.download_mode in AUTOMATIC_DOWNLOAD_MODES for row in retried_records):
                    records, failures = retried_records, retried_failures
        return self._finish_search(display_name, executable, records, failures, disabled=disabled)

    async def _collect(
        self, game_aliases: list[str], queries: list[str], *,
        deadline: float | None = None, disabled: frozenset[str] | None = None,
    ) -> tuple[list[ArtifactRecord], list[dict[str, object]]]:
        # Each job is paired with the provider it belongs to. Attribution used to
        # be positional, matching this list against a separate tuple of IDs, so
        # adding a provider or reordering one silently credited every row of
        # every provider to the wrong source.
        #
        # Which providers run is the registry's answer, not this list's: a
        # provider declared enabled and searchable here and nowhere else was
        # advertised as a source that could never return a row.
        builders = {
            "fearless": lambda definition: self._fearless(game_aliases),
            "playground": lambda definition: self._playground(game_aliases),
            "thecheatscript": lambda definition: self._thecheatscript(game_aliases),
            "github": lambda definition: self._github_search(game_aliases),
            "vgtimes": lambda definition: self._vgtimes(game_aliases),
        }
        jobs = []
        unwired: list[str] = []
        switched_off = self.disabled_provider_ids() if disabled is None else disabled
        skipped: list[str] = []
        for definition in searchable_providers():
            if definition.provider in switched_off:
                # Not a failure and not an outage: the user said not to ask this
                # source. It is skipped before its job is built, so nothing it
                # owns - no request, no index, no cooldown - is touched at all.
                skipped.append(definition.provider)
                continue
            builder = builders.get(definition.provider)
            if builder is None:
                unwired.append(definition.provider)
                continue
            jobs.append((definition.provider, builder(definition)))
        if skipped:
            log_activity(
                self.logger, "info", "catalog.sources_switched_off",
                providers=",".join(sorted(skipped)), searched=len(jobs),
            )
        # What each of them is doing, for the screen that is waiting on this.
        self._begin_search_progress([provider for provider, _ in jobs], skipped)
        async def measured(provider, job):
            started = time.monotonic()
            # The wall-clock moment this provider's own work began. Reconstructing
            # it later from the latency gives the moment the whole fan-out
            # finished minus this job's duration, and those are the same only
            # when this job is the last to finish: one provider holding the
            # gather open for another forty seconds moves the reconstructed
            # start past a refusal that really did land during this job.
            started_at = time.time()
            # Each provider runs in its own task, so its own collectors see only
            # what its own pages produced.
            with collect_parse_issues() as issues, collect_provider_rows() as collected:
                budget = (
                    PROVIDER_SEARCH_BUDGET_SECONDS if deadline is None
                    else max(0.0, deadline - time.monotonic())
                )
                try:
                    rows = await asyncio.wait_for(job, budget)
                except asyncio.TimeoutError:
                    # Whatever it had already found is the answer, and the
                    # source is reported as having stopped part way through.
                    # This is the state a provider that breaks out of its own
                    # page loop on a rate limit already reports, and the search
                    # was throwing away up to forty seconds of real pages to
                    # report the same thing with nothing behind it.
                    issues.mark_unavailable(
                        f"provider did not answer within the {PROVIDER_SEARCH_BUDGET_SECONDS:g} second search budget"
                    )
                    rows = list(collected)
                    self._finish_search_provider(provider, error=not rows)
                    return rows, (time.monotonic() - started) * 1000, issues, started_at
                except asyncio.CancelledError:
                    # An abandoned search is not this source failing, and the
                    # record it would be written into goes with that search.
                    raise
                except BaseException:
                    self._finish_search_provider(provider, error=True)
                    raise
            self._finish_search_provider(provider, error=False)
            return rows, (time.monotonic() - started) * 1000, issues, started_at
        try:
            batches = await asyncio.gather(*(measured(provider, job) for provider, job in jobs), return_exceptions=True)
        finally:
            self._end_search_progress()
        records: list[ArtifactRecord] = []
        failures: list[dict[str, object]] = []
        for (provider, _), batch in zip(jobs, batches):
            if isinstance(batch, BaseException):
                # Cancellation and process-level exceptions are control flow,
                # not a provider result. Turning CancelledError into a blank
                # `! fearless:` row also lets cancelled searches continue as if
                # they had completed, so preserve them for the operation owner.
                if isinstance(batch, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise batch
                message = str(batch).strip()[:2048] or f"{type(batch).__name__} (no diagnostic message)"
                failure: dict[str, object] = {"provider": provider, "error": message}
                if isinstance(batch, ProviderCooldown):
                    # Nothing was contacted, so there is nothing to record. It
                    # used to be recorded as an ordinary failure, and a failure
                    # with no status and no `Retry-After` writes a cleared
                    # deadline: one search during a cooldown therefore made the
                    # next search eligible immediately, which is the opposite of
                    # what the cooldown that produced it was for.
                    failures.append(failure)
                    log_activity(
                        self.logger, "info", "catalog.provider_in_cooldown", provider=provider,
                    )
                    continue
                if isinstance(batch, BrowserHandoff):
                    failure["handoff_url"] = batch.url
                    # Deliberately a lookup with a fallback rather than the
                    # strict accessor: this runs while handling a provider's
                    # failure, and an unregistered ID must not turn one
                    # provider's handoff into a failed search.
                    definition = PROVIDER_REGISTRY.get(provider)
                    display = definition.provider_display_name if definition else provider
                    rank = definition.priority if definition else 0
                    handoff_id = sha256(batch.url.encode("utf-8")).hexdigest()[:20]
                    records.append(ArtifactRecord(CatalogResult(
                        provider=provider,
                        provider_display_name=display,
                        topic_id="browser-search",
                        artifact_id=f"browser-search:{handoff_id}",
                        table_title=f"Continue {display} search in the system browser",
                        filename="Downloaded table.CT",
                        version=None,
                        size_bytes=None,
                        source_page=batch.url,
                        download_mode="file_picker",
                        match_score=0.0,
                        provider_rank=rank,
                        notes="The provider challenged anonymous HTTP; choose the downloaded table explicitly.",
                    )))
                failures.append(failure)
                log_failure(
                    self.logger, "catalog.provider_failed", batch, expected=True,
                    provider=provider,
                )
                if self.diagnostics is not None:
                    status = (
                        429 if isinstance(batch, ProviderRateLimited)
                        else 403 if isinstance(batch, (BrowserHandoff, ProviderChallenged))
                        else None
                    )
                    retry_after = batch.retry_after if isinstance(batch, ProviderRateLimited) else None
                    try:
                        self.diagnostics.record_failure(provider, error=message, http_status=status, retry_after=retry_after)
                    except ValueError:
                        pass
                continue
            provider_rows, latency_ms, issues, started_at = batch
            records.extend(provider_rows)
            # A job's batch is not all one provider's: a page it read can name
            # an exact page at another source, and those rows carry that
            # source's identity, are downloaded from it and are refused when it
            # is switched off. Counting the whole batch here credited them to
            # the provider that merely linked them. `_linked_source` records
            # them against the provider that answered, so only this job's own
            # rows are counted here.
            own_rows = sum(1 for row in provider_rows if row.result.provider == provider)
            if issues.unavailable is not None:
                # Rows and a refusal at once. The provider recorded whatever it
                # needed to when it hit this, so nothing is written here:
                # recording a success would clear the deadline it just set, and
                # recording a failure without a status would clear it too.
                failures.append({"provider": provider, "error": issues.unavailable})
                log_activity(
                    self.logger, "info", "catalog.provider_partially_available",
                    provider=provider, results=own_rows, reason=issues.unavailable[:200],
                )
                if not issues.unavailable_recorded and self.diagnostics is not None:
                    try:
                        # Never a deadline: this failure names none, and writing
                        # a cleared one is how a cooldown came to be erased by
                        # the thing that observed it.
                        self.diagnostics.record_failure(
                            provider, error=issues.unavailable, latency_ms=latency_ms,
                            preserve_cooldown=True,
                        )
                    except ValueError:
                        pass
            elif self.diagnostics is not None:
                try:
                    self.diagnostics.record_search_success(
                        provider, results=own_rows, latency_ms=latency_ms, http_status=200,
                        started_at=started_at,
                    )
                except ValueError:
                    pass
            self._report_parse_issues(provider, issues, results=own_rows)
        for provider in unwired:
            # A provider the registry declares searchable and this file has no
            # job for is a defect here, not an outage there. It is reported as a
            # failed source rather than dropped, because dropping it quietly is
            # exactly how a source came to be advertised and never searched.
            message = "CE Decky has no search adapter for this provider"
            failures.append({"provider": provider, "error": message})
            log_activity(self.logger, "error", "catalog.provider_not_wired", provider=provider)
        return records, failures

    def _report_parse_issues(self, provider: str, issues: ParseIssues, *, results: int) -> None:
        """Say what could not be read, without failing what could.

        A provider that quietly stops being readable otherwise looks like a game
        with no tables, so the counts are kept per provider and shown under
        Advanced, and one bounded line per search goes to the log.
        """
        if not issues.degraded and not issues.failed:
            return
        log_activity(
            self.logger, "warning" if issues.failed else "info", "catalog.parse_issues",
            provider=provider, unreadable_pages=issues.failed, degraded_rows=issues.degraded,
            results=results, samples=" | ".join(issues.samples),
        )
        if self.diagnostics is not None:
            try:
                self.diagnostics.record_parse_issues(provider, degraded=issues.degraded, failed=issues.failed)
            except ValueError:
                pass

    def _finish_search(
        self,
        display_name: str,
        executable: str | None,
        records: list[ArtifactRecord],
        failures: list[dict[str, object]],
        *,
        disabled: frozenset[str] | None = None,
    ) -> dict[str, object]:
        switched_off = self.disabled_provider_ids() if disabled is None else disabled
        failed_ids = {str(item["provider"]) for item in failures}
        cache_key = json.dumps([display_name, executable], ensure_ascii=False, separators=(",", ":"))
        cached = self._load_cache(cache_key, disabled=switched_off)
        if failed_ids:
            records.extend(replace(row, stale=True) for row in cached if row.result.provider in failed_ids)
        records = self._dedupe(records)[:MAX_RESULTS]
        if records:
            self._save_cache(cache_key, records)
        elif cached:
            # Ordered here rather than trusted as written: this file was saved
            # by whichever version last searched this game, so an answer served
            # wholly from it would otherwise keep presenting an order this one
            # no longer produces.
            records = self._dedupe(replace(row, stale=True) for row in cached)[:MAX_RESULTS]
        search_id = uuid.uuid4().hex
        snapshot = {(row.result.provider, row.result.artifact_id): row for row in records}
        self.artifacts = snapshot  # Compatibility for direct in-process callers.
        self._searches[search_id] = snapshot
        while len(self._searches) > 8:
            del self._searches[next(iter(self._searches))]
        results = []
        for row in records:
            public = row.public_dict()
            public["search_id"] = search_id
            results.append(public)
        # The same registry answer the jobs were built from, so a search cannot
        # summarize a set of sources it did not run. This is the registry's
        # answer and not the user's, which is what keeps a source the user
        # switched off in the roster, said to be off: dropping it would make a
        # deliberately narrowed search indistinguishable from a version that
        # never had those sources, which is the one question the roster exists
        # to answer. A source that is only ever followed as another provider's
        # link is absent here whether it is on or off, because it is not a
        # source this search ran either way.
        source_names = {
            definition.provider: definition.provider_display_name
            for definition in searchable_providers()
        }
        errors = {str(item["provider"]): str(item["error"]) for item in failures}
        result_counts = {
            provider: sum(1 for row in records if row.result.provider == provider)
            for provider in source_names
        }
        fearless_status = self.fearless_index_status()
        sources: list[dict[str, object]] = []
        for provider, provider_display_name in source_names.items():
            off = provider in switched_off
            source: dict[str, object] = {
                "provider": provider,
                "provider_display_name": provider_display_name,
                "results": result_counts[provider],
                "status": "disabled" if off else "unavailable" if provider in errors else "ok",
                "error": None if off else errors.get(provider),
            }
            # A cooldown is something the index knows about itself, and it is
            # the reason this source answered nothing: reported as a bare
            # failure it read as `n/a`, which is what a source with nothing for
            # this game reads as too. Any other failure is left as one.
            cooled_down = fearless_status.get("status") == "cooldown"
            if provider == "fearless" and not off and (provider not in errors or cooled_down):
                source.update(fearless_status)
                source.update({
                    "provider": provider,
                    "provider_display_name": provider_display_name,
                    "results": result_counts[provider],
                    # The search's own account of it is more specific than the
                    # index's, and says what the listing still knows.
                    "error": errors.get(provider) or fearless_status.get("error"),
                })
            sources.append(source)
        log_activity(
            self.logger, "info", "catalog.search_completed",
            search=search_id[:12], results=len(results), failures=len(failures),
            counts=";".join(f"{provider}:{result_counts[provider]}" for provider in source_names),
            switched_off=",".join(sorted(provider for provider in source_names if provider in switched_off)) or None,
            fearless_pages=fearless_status.get("indexed_pages"),
            fearless_total=fearless_status.get("total_pages"),
            stale=bool(records and all(row.stale for row in records)),
        )
        return {
            "search_id": search_id,
            "results": results,
            "failures": failures,
            "sources": sources,
            "stale": bool(records and all(row.stale for row in records)),
        }

    def resolve(self, provider: str, artifact_id: str, search_id: str | None = None) -> ArtifactRecord:
        # The one funnel from a selected row to a download, so it is where a
        # source switched off after the search has to be refused. A search
        # snapshot outlives the screen it was made for: Advanced can be opened
        # from the same panel, and a row already on screen would otherwise still
        # start a transfer from a source the user has just turned off.
        if provider in self.disabled_provider_ids():
            raise ValueError(
                "this table's source is switched off; switch it back on under Advanced to download from it"
            )
        snapshot = self.artifacts
        if search_id is not None:
            if not isinstance(search_id, str) or not re.fullmatch(r"[0-9a-f]{32}", search_id):
                raise ValueError("provider search snapshot is invalid")
            try:
                snapshot = self._searches[search_id]
            except KeyError as exc:
                raise ValueError("provider search snapshot is expired; search again") from exc
        try:
            return snapshot[(provider, artifact_id)]
        except KeyError as exc:
            raise ValueError("provider artifact selection is unknown or expired; search again") from exc

    async def resolve_download(
        self,
        record: ArtifactRecord,
        *,
        on_countdown: Callable[..., None] | None = None,
    ) -> tuple[str, frozenset[str]]:
        if record.direct_url is not None:
            return record.direct_url, PROVIDER_HOSTS[record.result.provider]
        request = record.acquisition
        if request is not None:
            # Deliberately not gated by the persisted cooldown the background
            # listing crawl keeps. `AcquisitionManager._with_rate_limit_waits`
            # owns throttling for a download somebody is watching, so that a
            # real 429 becomes a visible countdown that honours the provider's
            # own `Retry-After` and then retries. Consulting the crawl's
            # cooldown here failed that download outright, with a generic error,
            # for a limit recorded by a search rather than by this transfer, and
            # for a default minute the provider may never have asked for.
            import asyncio
            resolved = await request.resolve(
                self.network,
                record,
                sleep=asyncio.sleep,
                on_countdown=on_countdown,
            )
            if isinstance(resolved, tuple):
                if (
                    len(resolved) != 2
                    or not isinstance(resolved[0], str)
                    or not isinstance(resolved[1], frozenset)
                    or not resolved[1]
                    or not all(isinstance(host, str) and host for host in resolved[1])
                ):
                    raise ValueError("provider acquisition returned an invalid resolved host policy")
                return resolved
            url = resolved
            return url, request.artifact_hosts
        raise ValueError("provider artifact has no direct download route")

    async def _fearless(self, game_aliases: list[str]) -> list[ArtifactRecord]:
        """Search the resumable FearLess forum index without using search.php."""
        # What arms the background pass. Recorded here rather than around the
        # whole search, and on the way in rather than on the way out, because
        # the marker means one exact thing: a search asked this index for
        # something. A search that never reached this provider, because the user
        # switched it off, has not made its listing worth crawling, and the pass
        # this very search starts at the end of it is gated on the marker.
        self._note_search_activity()
        provider_ready = True
        if self.diagnostics is not None:
            try:
                if not self.diagnostics.can_attempt("fearless"):
                    provider_ready = False
            except ValueError:
                pass
        cooldown_remaining = self._fearless_cooldown_remaining()
        if cooldown_remaining > 0:
            provider_ready = False
            self._fearless_cooldown_until = time.time() + cooldown_remaining
        if not provider_ready and not self._fearless_pages:
            raise ProviderCooldown("provider is in persisted cooldown")
        had_index = bool(self._fearless_pages)
        # Measured before page zero is read, because reading it is what makes
        # the answer no. How long ago the index last had any attention at all:
        # with the background pass armed by Search, an index nobody has searched
        # for is an index no pass has walked, and this is the first search after
        # that gap. A listing sorted by activity puts new and freshly bumped
        # topics at the front, so the front is both the stalest part of the
        # index and the part most likely to hold what is being searched for now.
        #
        # Only where there are timestamps to judge it by. An index cached before
        # per-page freshness existed carries none, and that says nothing about
        # when it was last attended to: every page of it is due anyway, so the
        # pass this search arms is what brings it up to date, and reading eight
        # pages inside the search as well would spend the user's wait on work
        # that is already about to happen.
        newest_page_age = (
            time.time() - max(self._fearless_fetched.values())
            if self._fearless_fetched else None
        )
        index_went_unattended = (
            had_index
            and newest_page_age is not None
            and newest_page_age > FEARLESS_STALE_REFRESH_AFTER_SECONDS
        )
        if provider_ready:
            try:
                # Page zero is the authority for the current page size. Read it
                # before calculating any other leading offset, because a resize
                # invalidates every offset cached under the former step.
                self._note_search_stage("fearless", "reading the forum listing")
                await self._refresh_fearless_page(0)
                refresh_count = 1
                if index_went_unattended:
                    refresh_count = FEARLESS_STALE_REFRESH_PAGES
                elif had_index:
                    refresh_count = FEARLESS_REFRESH_PAGES
                if self._fearless_total_pages is not None:
                    refresh_count = min(refresh_count, self._fearless_total_pages)
                step = self._fearless_page_step
                if refresh_count > 1:
                    if step is None:
                        raise ValueError("FearLess forum listing declares no page size")
                    for index in range(1, refresh_count):
                        await self._refresh_fearless_page(index * step)
                # A one-page index owes no background pass, so persist the
                # synchronous page here instead of leaving it memory-only.
                if self._fearless_total_pages == 1:
                    await self._persist_fearless_index()
                self._fearless_index_error = None
                self._fearless_cooldown_until = 0.0
            except ProviderRateLimited as exc:
                self._fearless_index_error = str(exc)[:2048]
                self._set_fearless_cooldown(exc, record=bool(self._fearless_pages))
                provider_ready = False
                if not self._fearless_pages:
                    raise
            except ProviderChallenged as exc:
                self._fearless_index_error = str(exc)[:2048]
                if not self._fearless_pages:
                    raise
                # The cached index still answers, and the topic reads that would
                # follow are against the same challenged site.
                _note_provider_unavailable(str(exc)[:2048])
                provider_ready = False
            except (NetworkError, ValueError) as exc:
                self._fearless_index_error = str(exc)[:2048]
                if not self._fearless_pages:
                    raise
        topics: dict[str, str] = {}
        for start in sorted(self._fearless_pages):
            for topic_id, title in self._fearless_pages[start]:
                topics.setdefault(topic_id, fearless_topic_title(title))
        tokens = sitemap_prefilter_tokens(game_aliases)
        ranked: list[tuple[float, str, str]] = []
        for topic_id, title in topics.items():
            if tokens and not any(token in title.casefold() for token in tokens):
                continue
            score = match_breakdown(game_aliases, Candidate(title, provider_trust=0.7)).confidence
            if score >= 0.55:
                ranked.append((score, topic_id, title))
        ranked.sort(reverse=True)

        rows: list[ArtifactRecord] = _provider_rows()
        if not provider_ready:
            # The index answers from memory, so this returns without asking the
            # provider anything. Returning an empty list alone read as a
            # successful search of zero results, which cleared the very deadline
            # that stopped it and told the user the source was fine.
            #
            # What the index does know is said here, because "n/a" beside a
            # complete 332 page listing is the screen refusing to tell the user
            # the one thing it can: that this source has matches for the game
            # and cannot be asked for them for another minute.
            reason = self._fearless_index_error or "provider is in persisted cooldown"
            matches = len(ranked)
            waiting = max(0, math.ceil(self._fearless_cooldown_until - time.time()))
            if matches:
                reason = f"{reason}; {matches} matching topic(s) in the cached listing"
            if waiting:
                reason = f"{reason}; asking again in {waiting}s"
            _note_provider_unavailable(reason)
            self._start_fearless_index()
            return rows
        try:
            fetching = ranked[:MAX_TOPIC_FETCHES]
            for index, (score, topic_id, title) in enumerate(fetching, start=1):
                # The listing is one read and this is up to forty, so this is
                # the part of a long search that is actually still moving.
                self._note_search_stage("fearless", f"reading topic {index} of {len(fetching)}")
                page = f"{FEARLESS_BASE_URL}viewtopic.php?f={FEARLESS_FORUM_ID}&t={topic_id}"
                response = await self.network.get(
                    page,
                    allowed_hosts=PROVIDER_HOSTS["fearless"],
                    max_bytes=MAX_HTML_BYTES,
                    referer=FEARLESS_FORUM_URL,
                )
                if response.status == 429:
                    exc = ProviderRateLimited(response.headers.get("retry-after"))
                    self._fearless_index_error = str(exc)[:2048]
                    self._set_fearless_cooldown(exc, record=True)
                    # Whatever topics were already read stay in the answer, and
                    # the search still says the source stopped serving it.
                    _note_provider_unavailable(str(exc)[:2048], recorded=True)
                    break
                if response.status == 403:
                    # A challenge is not one unreadable topic. Skipped as merely
                    # "not 200", a provider challenging every matching topic
                    # answered with an empty list that read as success.
                    challenge = f"provider answered HTTP 403 for topic {topic_id}"
                    self._fearless_index_error = challenge
                    _note_provider_unavailable(challenge)
                    break
                if response.status != 200:
                    continue
                rows.extend(rows_from_page(lambda: parse_phpbb_attachments(
                    decode_html(response.body),
                    provider="fearless",
                    display_name="FearLess Cheat Engine",
                    topic_id=topic_id,
                    topic_title=title,
                    source_page=page,
                    score=score,
                    rank=100,
                    hosts=PROVIDER_HOSTS["fearless"],
                )))
        finally:
            # Do not compete with the interactive topic reads for the provider's
            # request budget. The resumable crawl starts only after search has
            # returned its bounded current matches.
            self._start_fearless_index()
        return rows

    async def _refresh_fearless_page(self, start: int) -> None:
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError("FearLess forum page offset is invalid")
        async with self._fearless_index_lock:
            step = self._fearless_page_step
            if step is not None and start % step:
                raise ValueError("FearLess forum page offset is not a page boundary")
            if step is None and start:
                raise ValueError("FearLess forum page size is not known yet")
            url = FEARLESS_FORUM_URL if start == 0 else f"{FEARLESS_FORUM_URL}&start={start}"
            response = await self.network.get(
                url,
                allowed_hosts=PROVIDER_HOSTS["fearless"],
                max_bytes=MAX_HTML_BYTES,
                referer=FEARLESS_BASE_URL,
                retries=0,
            )
            if response.status == 429:
                raise ProviderRateLimited(response.headers.get("retry-after"))
            if response.status == 403:
                raise ProviderChallenged("FearLess listing answered HTTP 403")
            if response.status != 200:
                raise NetworkError(f"FearLess forum listing returned HTTP {response.status}")
            listing = parse_fearless_forum_page(decode_html(response.body))
            if listing.page_step_invalid:
                raise ValueError("FearLess forum listing declares contradictory page geometry")
            if start == 0:
                if listing.current_page != 1:
                    raise ValueError("FearLess first forum offset did not return page one")
                if listing.total_pages > 1 and listing.page_step is None:
                    raise ValueError("FearLess forum listing declares no unambiguous page size")
                listing_step = listing.page_step if listing.total_pages > 1 else None
            else:
                # Page zero establishes and changes index geometry. A later
                # page may not expose another numeric page >= 2 because phpBB
                # renders the current page as a span. In that ordinary case the
                # already-proven step remains authoritative; explicit geometry
                # on the page must still agree with it.
                if step is None:
                    raise ValueError("FearLess forum page size is not known yet")
                if listing.page_step is not None and listing.page_step != step:
                    raise ValueError("FearLess forum page size contradicts the current index")
                listing_step = step
            if start == 0 and listing_step != step:
                # Every stored offset is a multiple of the size a page was when
                # it was stored, so a listing that now pages differently is not
                # describing the same index. Keeping the old offsets would leave
                # an index with holes in it that still looked complete.
                if step is not None or self._fearless_pages:
                    log_activity(
                        self.logger, "warning", "catalog.fearless_page_step_changed",
                        previous=step, current=listing_step, dropped=len(self._fearless_pages),
                    )
                    self._fearless_pages = {}
                    self._fearless_fetched = {}
                    if start:
                        raise ValueError("FearLess forum page size changed under the index")
                step = listing_step
                self._fearless_page_step = step
            if listing.total_pages == 1 and start:
                raise ValueError("FearLess single-page forum offset must be zero")
            if step is not None:
                if start != (listing.current_page - 1) * step:
                    raise ValueError("FearLess forum page number does not match its offset")
                if listing.total_topics is not None and not (
                    (listing.total_pages - 1) * step
                    < listing.total_topics
                    <= listing.total_pages * step
                ):
                    raise ValueError("FearLess forum totals contradict the current page size")
            if step is not None and start >= listing.total_pages * step:
                raise ValueError("FearLess forum page offset exceeds the advertised index")
            self._fearless_pages[start] = listing.rows
            self._fearless_fetched[start] = time.time()
            self._fearless_total_pages = listing.total_pages
            self._fearless_total_topics = listing.total_topics
            if step is None:
                self._fearless_pages = {
                    offset: page_rows for offset, page_rows in self._fearless_pages.items()
                    if offset == 0
                }
            else:
                self._fearless_pages = {
                    offset: page_rows for offset, page_rows in self._fearless_pages.items()
                    if offset < listing.total_pages * step
                }
            self._fearless_fetched = {
                offset: fetched for offset, fetched in self._fearless_fetched.items()
                if offset in self._fearless_pages
            }

    def _pending_fearless_page_starts(self) -> list[int]:
        """Listing pages the index still owes: never fetched, or aged out.

        Freshness is per page, so the daily pass is a refresh rather than a
        rebuild: a page a search already re-read stays out of the list, and a
        page that was fetched once and never revisited is what actually gets
        another request.
        """
        if self._fearless_total_pages is None:
            return []
        cutoff = time.time() - FEARLESS_INDEX_MAX_AGE_SECONDS
        pending: list[int] = []
        if self._fearless_page_step is None:
            # The first page is the only offset that is a page boundary whatever
            # the size of a page, and reading it is what learns the rest.
            return [] if self._fearless_fetched.get(0, 0.0) > cutoff and 0 in self._fearless_pages else [0]
        for page_number in range(self._fearless_total_pages):
            start = page_number * self._fearless_page_step
            if start not in self._fearless_pages or self._fearless_fetched.get(start, 0.0) <= cutoff:
                pending.append(start)
        return pending

    def _note_search_activity(self) -> None:
        """Remember that Search was used, and put it on disk while it is true.

        The marker has its own small file precisely so that this can happen
        here, at the moment it becomes true, rather than being owed to some
        later write: it is a few dozen bytes, it takes no lock, and nothing else
        writes it. What it used to be owed to was the index cache, a third of a
        megabyte, so the arming reached disk at most once an hour and often only
        at unload. An unload is not a promise: a backend that is killed, runs
        out of memory or loses power never runs one, and the search that armed
        the pass was lost with it.

        The write is synchronous, and on the loop thread that is a feature
        rather than a compromise. The other two writers of this file, the
        scheduler tick and the unload prologue, are on that same thread, so
        there is exactly one order for all three and no snapshot of an older
        moment can land behind a newer one.

        A storage failure here fails no search. It is recorded, the marker stays
        owed in memory, and the scheduler's next tick offers it again.
        """
        self._searched_at = time.time()
        try:
            self._persist_search_marker()
        except Exception as exc:  # noqa: BLE001 - a marker write never fails a search
            log_failure(self.logger, "fearless.search_marker_not_persisted", exc, expected=True)

    async def _persist_search_activity(self) -> None:
        """Offer again a marker the search's own write could not land.

        The search writes it as it happens, so this is normally a comparison
        that finds nothing owed. It is here for the device that was full or
        read-only for that moment: the arming is still in memory, and this is
        what puts it on disk five minutes later instead of leaving it to an
        unload that may never run.

        A coroutine because the tick awaits it, and the write inside it is the
        same synchronous one every other caller makes. Failure propagates to the
        tick, which records it as the environment and backs off by one interval.
        """
        self._persist_search_marker()

    def _index_is_armed(self) -> bool:
        """Whether Search has been used recently enough to owe a background pass.

        Without this the crawl is a timer and nothing else: a plugin is loaded
        for as long as Steam is running, so a device whose owner has not opened
        Search for months would ask this forum for its whole listing every day,
        for an index nothing is going to read.
        """
        return time.time() - self._searched_at <= FEARLESS_INDEX_ACTIVE_WINDOW_SECONDS

    def start_background_index(self) -> None:
        """Begin looking, on a timer, at whether a listing pass is owed.

        Called once when the plugin loads. Every pass this can start could
        already be started by a search, and each one is still paced, still gives
        way to an interactive search and still refuses a switched-off source;
        the only thing that changes is who asks. Without it the crawl ran only
        while somebody was searching, which on a device in ordinary use is a
        listing that never finishes refreshing itself.

        A bootstrap is deliberately not one of the things it does: until a
        search has read the listing once, this process does not know the
        forum's pagination, and reading it in order to find out would ask a
        third-party source for pages on behalf of a user who has not asked it
        for anything yet.
        """
        if self._index_schedule_task is not None and not self._index_schedule_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Constructed outside a loop, which is a test or a probe rather than
            # the plugin. There is nothing to schedule against.
            return
        self._index_schedule_task = asyncio.create_task(self._schedule_fearless_index())
        log_activity(
            self.logger, "info", "fearless.index_scheduled",
            first_in_s=FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS,
            every_s=FEARLESS_INDEX_SCHEDULE_INTERVAL_SECONDS,
            indexed=len(self._fearless_pages), total=self._fearless_total_pages,
            pending=len(self._pending_fearless_page_starts()),
            # Whether it will actually ask for any of them, which is the half of
            # this a report needs: a scheduler that is running and a scheduler
            # that is running and armed look identical from the outside.
            armed=self._index_is_armed(),
            searched_h_ago=None if self._searched_at <= 0 else round((time.time() - self._searched_at) / 3600, 1),
        )

    async def _schedule_fearless_index(self) -> None:
        """Ask for a pass at intervals, for as long as this service is loaded.

        One tick's failure is one tick's failure. The whole loop used to sit in
        a single `try`, so the first exception to reach it ended the coroutine
        for the rest of the session: nothing restarts this until the next plugin
        load, and a plugin is loaded for as long as Steam is running. A full
        disk during one marker write would therefore have turned "refresh the
        listing on a timer" off until the device was rebooted, silently.

        So the tick is what is guarded, and the interval is the backoff: a
        storage failure that is still there in five minutes fails one more tick
        and no more than that. Cancellation stays terminal, because that is
        unload asking this to stop.
        """
        try:
            await asyncio.sleep(FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS)
            while True:
                try:
                    running = self._fearless_index_task
                    if running is None or running.done():
                        self._start_fearless_index()
                    # A search writes its own marker as it happens, so this is
                    # the retry rather than the write: it is what puts an arming
                    # on disk when the moment the search tried was a full or
                    # read-only device.
                    await self._persist_search_activity()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a tick failure fails no search
                    # A full or read-only device is the ordinary reason a tick
                    # fails and it recurs every interval, so it is recorded as
                    # the environment rather than as a defect: the stack trace
                    # of the same `OSError` every five minutes is noise in the
                    # one file a report is read from.
                    log_failure(
                        self.logger, "fearless.index_tick_failed", exc,
                        expected=isinstance(exc, (OSError, ValueError)),
                    )
                await asyncio.sleep(FEARLESS_INDEX_SCHEDULE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a scheduler failure fails no search
            log_failure(self.logger, "fearless.index_schedule_failed", exc, expected=False)

    def _start_fearless_index(self) -> None:
        if self._index_suspended:
            return
        if not self._index_is_armed():
            return
        if "fearless" in self.disabled_provider_ids():
            return
        if self._fearless_total_pages is None:
            return
        if self._fearless_index_task is not None and not self._fearless_index_task.done():
            return
        if self._index_pass_is_paced():
            # A pass that stopped short is otherwise restarted by the next
            # search, and the next search is every search.
            return
        pending = self._pending_fearless_page_starts()
        if not pending:
            return
        log_activity(
            self.logger, "info", "fearless.index_started",
            indexed=len(self._fearless_pages), total=self._fearless_total_pages,
            pending=len(pending), pages_this_run=min(len(pending), FEARLESS_INDEX_PAGES_PER_RUN),
        )
        self._fearless_index_ran_at = time.monotonic()
        self._fearless_index_task = asyncio.create_task(self._build_fearless_index())

    def _index_pass_is_paced(self) -> bool:
        """Whether the last pass is recent enough that another one has to wait.

        Elapsed time inside this process, measured on the clock that measures
        elapsed time. A wall clock corrected backwards would hold the next pass
        for the correction plus the interval, and one corrected forwards would
        let it start at once.
        """
        return (
            self._fearless_index_ran_at is not None
            and time.monotonic() - self._fearless_index_ran_at < FEARLESS_INDEX_RUN_INTERVAL_SECONDS
        )

    def _searches_are_quiet(self) -> bool:
        """Whether the provider's request budget is the crawl's to spend.

        A search has somebody watching a screen for it and this does not, so
        while one is running, or has only just finished, the crawl asks for
        nothing.
        """
        if self._search_tasks:
            return False
        return time.monotonic() - self._last_search_at >= FEARLESS_INDEX_QUIET_SECONDS

    async def _wait_for_quiet_searches(self) -> bool:
        """Hold the crawl until no search is running. False if it should stop."""
        give_up_at = time.monotonic() + FEARLESS_INDEX_QUIET_WAIT_LIMIT_SECONDS
        while not self._searches_are_quiet():
            if "fearless" in self.disabled_provider_ids():
                return False
            if time.monotonic() >= give_up_at:
                log_activity(
                    self.logger, "info", "fearless.index_gave_way",
                    indexed=len(self._fearless_pages), total=self._fearless_total_pages,
                )
                return False
            # Whichever comes first: the moment searches could be quiet again,
            # or the moment this stops waiting at all. Sleeping the quiet
            # interval alone meant the limit was only looked at afterwards, so
            # giving way always overshot it by up to that interval.
            quiet_in = FEARLESS_INDEX_QUIET_SECONDS - (time.monotonic() - self._last_search_at)
            give_up_in = give_up_at - time.monotonic()
            await asyncio.sleep(max(0.05, min(FEARLESS_INDEX_QUIET_SECONDS, quiet_in, give_up_in)))
        return True

    async def _build_fearless_index(self) -> None:
        persisted = 0
        refreshed = 0
        rate_limits = 0
        try:
            while True:
                if refreshed >= FEARLESS_INDEX_PAGES_PER_RUN:
                    # The rest is a later pass's. Walking whatever is pending is
                    # what asked this provider for hundreds of pages at once.
                    break
                if not await self._wait_for_quiet_searches():
                    break
                # Checked every iteration, not only at the start. This crawl
                # outlives the search that started it by minutes: it walks
                # hundreds of listing pages with a pause between each and sleeps
                # through any cooldown the forum asks for, so a switch pressed
                # while it runs would otherwise keep requesting the one source
                # the user has just said to stop asking.
                if "fearless" in self.disabled_provider_ids():
                    log_activity(
                        self.logger, "info", "fearless.index_switched_off",
                        indexed=len(self._fearless_pages), total=self._fearless_total_pages,
                    )
                    break
                pending = self._pending_fearless_page_starts()
                if not pending:
                    break
                start = pending[0]
                fetched = False
                # Set when this pass has decided to stop rather than to try the
                # next page. Breaking the inner loop alone put the decision back
                # to the outer one, which starts the whole giving-way allowance
                # again: a pass waking from a cooldown into searches that never
                # stop would give way for five minutes, come back, and give way
                # for five minutes more, without end.
                give_up = False
                while True:
                    cooldown = self._fearless_cooldown_remaining()
                    if cooldown > 0:
                        self._fearless_cooldown_until = time.time() + cooldown
                        await asyncio.sleep(cooldown)
                        # A cooldown can be hours long, and the switch is what
                        # this wakes up to find changed. Leaving the retry loop
                        # without having fetched anything, so the outer loop
                        # observes the switch and stops rather than counting a
                        # page this never read.
                        if "fearless" in self.disabled_provider_ids():
                            break
                    # Checked here rather than only at the top of the outer
                    # loop, because both sleeps above are long enough for a
                    # search to have started underneath one: waking straight
                    # into the request is how this crawl would go on competing
                    # for the provider with the search the user is watching.
                    if not await self._wait_for_quiet_searches():
                        give_up = True
                        break
                    try:
                        await self._refresh_fearless_page(start)
                        fetched = True
                        break
                    except ProviderRateLimited as exc:
                        self._fearless_index_error = str(exc)[:2048]
                        cooldown = self._set_fearless_cooldown(exc, record=True)
                        rate_limits += 1
                        log_failure(
                            self.logger, "fearless.index_rate_limited", exc, expected=True,
                            indexed=len(self._fearless_pages), total=self._fearless_total_pages,
                            retry_seconds=cooldown, limits_this_run=rate_limits,
                        )
                        if rate_limits >= FEARLESS_INDEX_RATE_LIMITS_PER_RUN:
                            # Sitting out the cooldown and asking again for the
                            # same page kept this pass alive indefinitely, and
                            # every refusal it collected re-armed the cooldown
                            # that stops the user's own searches.
                            break
                        await asyncio.sleep(cooldown)
                if give_up:
                    break
                if rate_limits >= FEARLESS_INDEX_RATE_LIMITS_PER_RUN:
                    log_activity(
                        self.logger, "info", "fearless.index_backed_off",
                        indexed=len(self._fearless_pages), total=self._fearless_total_pages,
                        refreshed=refreshed, limits=rate_limits,
                    )
                    break
                if not fetched:
                    continue
                self._fearless_index_error = None
                self._fearless_cooldown_until = 0.0
                persisted += 1
                refreshed += 1
                if persisted >= FEARLESS_INDEX_PERSIST_EVERY:
                    await self._persist_fearless_index()
                    persisted = 0
                await asyncio.sleep(FEARLESS_INDEX_REQUEST_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log_activity(
                self.logger, "info", "fearless.index_paused",
                indexed=len(self._fearless_pages), total=self._fearless_total_pages,
            )
            raise
        except Exception as exc:
            self._fearless_index_error = str(exc)[:2048]
            log_failure(
                self.logger, "fearless.index_failed", exc,
                expected=isinstance(exc, (NetworkError, ValueError)),
                indexed=len(self._fearless_pages), total=self._fearless_total_pages,
            )
        finally:
            # What the last pass actually did is the only honest answer to
            # "is this rebuilding the index every time"; report it either way.
            self._fearless_last_refresh_at = time.time()
            self._fearless_last_refresh_pages = refreshed
            if persisted or self._fearless_pages:
                try:
                    await self._persist_fearless_index()
                except Exception as exc:
                    self._fearless_index_error = str(exc)[:2048]
                    log_failure(
                        self.logger, "fearless.index_persist_failed", exc,
                        expected=isinstance(exc, (OSError, ValueError)),
                        indexed=len(self._fearless_pages), total=self._fearless_total_pages,
                    )
            if self._fearless_total_pages is not None and len(self._fearless_pages) >= self._fearless_total_pages:
                log_activity(
                    self.logger, "info", "fearless.index_completed",
                    indexed=len(self._fearless_pages), total=self._fearless_total_pages,
                    topics=len({topic_id for rows in self._fearless_pages.values() for topic_id, _ in rows}),
                )

    def fearless_index_status(self) -> dict[str, object]:
        indexed_pages = len(self._fearless_pages)
        total_pages = self._fearless_total_pages
        unique_topics = len({topic_id for rows in self._fearless_pages.values() for topic_id, _ in rows})
        complete = total_pages is not None and indexed_pages >= total_pages
        cooldown_remaining = max(0, math.ceil(self._fearless_cooldown_until - time.time()))
        if cooldown_remaining:
            status = "cooldown"
        elif self._fearless_index_error is not None:
            status = "stale" if indexed_pages else "unavailable"
        else:
            status = "ok" if complete else "indexing"
        # The whole index is only as fresh as its least recently fetched page,
        # and that is what tells a reader whether a full refresh has happened.
        # A page cached before per-page freshness existed carries no timestamp
        # and is already counted as due, so one of those makes "fully refreshed"
        # unanswerable rather than older: reporting the oldest timestamp we do
        # have would claim a refresh that never covered those pages.
        fetched = [self._fearless_fetched.get(start) for start in sorted(self._fearless_pages)]
        oldest = min(fetched) if complete and fetched and all(fetched) else None
        return {
            "status": status,
            "error": self._fearless_index_error,
            "indexed_pages": indexed_pages,
            "total_pages": total_pages,
            "indexed_topics": unique_topics,
            "retry_after_seconds": cooldown_remaining or None,
            "refresh_age_seconds": FEARLESS_INDEX_MAX_AGE_SECONDS,
            "fully_refreshed_at": oldest,
            "stale_pages": len(self._pending_fearless_page_starts()),
            "last_refresh_at": self._fearless_last_refresh_at,
            "last_refresh_pages": self._fearless_last_refresh_pages,
        }

    def _fearless_cooldown_remaining(self) -> int:
        remaining = max(0, math.ceil(self._fearless_cooldown_until - time.time()))
        if self.diagnostics is None:
            return remaining
        try:
            providers = self.diagnostics.snapshot().get("providers", {})
            state = providers.get("fearless", {}) if isinstance(providers, dict) else {}
            persisted_until = float(state.get("cooldown_until_epoch_s", 0)) if isinstance(state, dict) else 0.0
            return max(remaining, max(0, math.ceil(persisted_until - time.time())))
        except (TypeError, ValueError):
            return remaining

    def _set_fearless_cooldown(self, exc: ProviderRateLimited, *, record: bool) -> int:
        cooldown = parse_retry_after(exc.retry_after) or FEARLESS_DEFAULT_COOLDOWN_SECONDS
        self._fearless_cooldown_until = time.time() + cooldown
        if record and self.diagnostics is not None:
            try:
                self.diagnostics.record_failure(
                    "fearless",
                    error=str(exc),
                    http_status=429,
                    retry_after=exc.retry_after,
                )
            except ValueError:
                pass
        return cooldown

    async def _persist_fearless_index(self) -> None:
        payload, generation = self._fearless_index_snapshot()
        write = asyncio.create_task(asyncio.to_thread(self._write_index_snapshot, payload, generation))
        self._fearless_index_writes.add(write)
        write.add_done_callback(self._fearless_index_writes.discard)
        # The cache write is a bounded native worker: cancellation must hold
        # until it is done rather than leave a half-written index behind.
        if await drain_through_cancellation(
            write, label="catalog.index_cache_write", logger=self.logger, watching=(write,),
        ):
            raise asyncio.CancelledError
        if write.result():
            self._note_marker_persisted(payload)

    def _fearless_index_snapshot(self) -> tuple[dict[str, object], int]:
        """One payload and the place it takes in the order of writes.

        Both under the lock, and that is the point rather than an abundance of
        caution: the number has to be taken at the same moment as the contents
        it describes. Numbering afterwards lets two writers read the state in
        one order and be numbered in the other, which is how an older snapshot
        would win with a newer number.

        The wait for that lock is unbounded, and every caller of this has a loop
        under it, so somebody else's `fsync` costs it nothing. The one caller
        that could not afford to wait was the unload prologue, and it writes the
        Search marker's own small file instead of competing for this one.
        """
        self._index_write_lock.acquire()
        try:
            self._index_generation += 1
            return self._fearless_index_payload(), self._index_generation
        finally:
            self._index_write_lock.release()

    def _write_index_snapshot(self, payload: dict[str, object], generation: int) -> bool:
        """Put one snapshot on disk unless a newer one is already there.

        Runs on a native worker, and more than one of them may be owed at once.
        The lock orders them against each other and the generation decides the
        outcome when they are out of order, so the file only ever moves forward.
        """
        self._index_write_lock.acquire()
        try:
            if generation <= self._index_written_generation:
                return False
            atomic_write_json(
                self.cache_path.with_name("fearless-index.json"),
                payload,
                max_bytes=MAX_FEARLESS_INDEX_CACHE_BYTES,
            )
            self._index_written_generation = generation
        finally:
            self._index_write_lock.release()
        return True

    def _search_marker_path(self):
        return self.cache_path.with_name(FEARLESS_SEARCH_MARKER_NAME)

    def _load_search_marker(self) -> float:
        """When the marker's own file says Search was last used.

        Zero for a device that has never searched, for a file this version does
        not recognise, and for anything the bounds below reject. Every one of
        those means the index cache is the only record, which is what this read
        was added beside rather than in place of.
        """
        try:
            raw = load_json(
                self._search_marker_path(), {}, max_bytes=MAX_FEARLESS_SEARCH_MARKER_BYTES,
            )
        except (OSError, ValueError, TypeError):
            return 0.0
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            return 0.0
        searched = raw.get("searched")
        # The same bounds the index cache's copy gets: a marker from the future
        # would arm the pass for as long as the clock stayed wrong.
        if (
            isinstance(searched, bool)
            or not isinstance(searched, (int, float))
            or not 0 < float(searched) <= time.time() + FEARLESS_INDEX_MAX_AGE_SECONDS
        ):
            return 0.0
        return float(searched)

    def _persist_search_marker(self) -> bool:
        """Put an owed Search marker in its own file. Returns whether it wrote.

        Synchronous and lock-free by design. The search calls it as the arming
        becomes true, and the unload prologue calls it with no loop left under
        it and Decky's five seconds running out; one atomic replacement of a few
        dozen bytes is what both of those can afford. Every caller is on the
        loop thread, so the three of them are strictly ordered.

        The value recorded as durable is the one that went into the payload
        rather than whatever `_searched_at` holds afterwards, for the reason
        `_note_marker_persisted` carries: a search can land while the write is
        in flight, and believing it would leave the next process reading the
        older marker with nothing owing it.
        """
        if self._searched_at <= self._searched_persisted_at:
            return False
        searched = self._searched_at
        atomic_write_json(
            self._search_marker_path(),
            {"schema": 1, "searched": searched},
            max_bytes=MAX_FEARLESS_SEARCH_MARKER_BYTES,
        )
        self._searched_persisted_at = max(self._searched_persisted_at, searched)
        return True

    def _note_marker_persisted(self, payload: dict[str, object]) -> None:
        """Believe the marker that the bytes which landed actually carry.

        Taken from the payload rather than from `_searched_at`, which a search
        can advance while the write is in flight: reading it afterwards recorded
        a marker as durable that was never in the file, and the next reload
        would have found the older one with nothing owing it.
        """
        written = payload.get("searched")
        if isinstance(written, (int, float)) and not isinstance(written, bool):
            self._searched_persisted_at = max(self._searched_persisted_at, float(written))

    def _fearless_index_payload(self) -> dict[str, object]:
        return {
            "schema": 1,
            "saved": time.time(),
            "total_pages": self._fearless_total_pages,
            "total_topics": self._fearless_total_topics,
            # What every offset below is a multiple of. Stored with them because
            # an offset means nothing without it, and a cache written before
            # this existed is read back as unusable rather than reinterpreted.
            "page_step": self._fearless_page_step,
            "pages": {
                str(start): [[topic_id, title] for topic_id, title in rows]
                for start, rows in sorted(self._fearless_pages.items())
            },
            # Optional and per page: a cache written before this existed simply
            # reads back as due for its first daily refresh.
            "fetched": {
                str(start): self._fearless_fetched[start]
                for start in sorted(self._fearless_pages)
                if start in self._fearless_fetched
            },
            # When Search was last used. Optional, and a cache written before it
            # existed reads back as never searched, which leaves the background
            # pass disarmed until the next search arms it - which is the state
            # that is correct for it anyway.
            "searched": self._searched_at,
        }

    @staticmethod
    def _no_fearless_index() -> tuple[dict[int, list[tuple[str, str]]], None, None, dict[int, float], None, float]:
        """What a cache that cannot be read leaves this process holding.

        A function rather than a constant, and this is not a style choice: the
        two empty containers in it become the service's own pages and
        timestamps, so a single shared tuple hands every service that failed to
        load a cache the same two dictionaries. The first one to index anything
        fills them in for all of them.
        """
        return {}, None, None, {}, None, 0.0

    def _load_fearless_index(
        self,
    ) -> tuple[dict[int, list[tuple[str, str]]], int | None, int | None, dict[int, float], int | None, float]:
        try:
            raw = load_json(
                self.cache_path.with_name("fearless-index.json"),
                {},
                max_bytes=MAX_FEARLESS_INDEX_CACHE_BYTES,
            )
            if not isinstance(raw, dict) or raw.get("schema") != 1:
                return self._no_fearless_index()
            total_pages = raw.get("total_pages")
            total_topics = raw.get("total_topics")
            if isinstance(total_pages, bool) or not isinstance(total_pages, int) or not 1 <= total_pages <= MAX_FEARLESS_INDEX_PAGES:
                return self._no_fearless_index()
            page_step = raw.get("page_step")
            if total_pages == 1:
                if page_step is not None:
                    return self._no_fearless_index()
            elif (
                isinstance(page_step, bool)
                or not isinstance(page_step, int)
                or not 0 < page_step <= MAX_FEARLESS_PAGE_STEP
            ):
                # Without the size a multi-page listing was, its offsets cannot
                # be read. A single-page listing has only the exact offset zero.
                return self._no_fearless_index()
            if total_topics is not None and (
                isinstance(total_topics, bool)
                or not isinstance(total_topics, int)
                or not 0 <= total_topics <= MAX_FEARLESS_INDEX_TOPICS
            ):
                return self._no_fearless_index()
            raw_pages = raw.get("pages")
            if not isinstance(raw_pages, dict) or len(raw_pages) > total_pages:
                return self._no_fearless_index()
            pages: dict[int, list[tuple[str, str]]] = {}
            for raw_start, raw_rows in raw_pages.items():
                if not isinstance(raw_start, str) or not raw_start.isdigit():
                    return self._no_fearless_index()
                start = int(raw_start)
                if page_step is None:
                    if start != 0:
                        return self._no_fearless_index()
                elif start % page_step or start >= total_pages * page_step:
                    return self._no_fearless_index()
                if not isinstance(raw_rows, list) or len(raw_rows) > MAX_FEARLESS_ROWS_PER_PAGE:
                    return self._no_fearless_index()
                rows: list[tuple[str, str]] = []
                for item in raw_rows:
                    if (
                        not isinstance(item, list)
                        or len(item) != 2
                        or not isinstance(item[0], str)
                        or not item[0].isdigit()
                        or len(item[0]) > MAX_FEARLESS_TOPIC_ID_DIGITS
                        or not isinstance(item[1], str)
                        or not item[1]
                        or len(item[1].encode("utf-8")) > MAX_FEARLESS_TOPIC_TITLE * 4
                    ):
                        return self._no_fearless_index()
                    rows.append((item[0], item[1]))
                pages[start] = rows
            if sum(len(rows) for rows in pages.values()) > MAX_FEARLESS_INDEX_TOPICS:
                return self._no_fearless_index()
            # Freshness is advisory: a cache without it, or with anything the
            # bounds reject, simply reads back as due for a refresh rather than
            # discarding sixteen thousand usable topics.
            fetched: dict[int, float] = {}
            raw_fetched = raw.get("fetched")
            if isinstance(raw_fetched, dict) and len(raw_fetched) <= total_pages:
                horizon = time.time() + FEARLESS_INDEX_MAX_AGE_SECONDS
                for key, value in raw_fetched.items():
                    if not isinstance(key, str) or not key.isdigit():
                        continue
                    offset = int(key)
                    if offset not in pages:
                        continue
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    if not 0 < float(value) <= horizon:
                        continue
                    fetched[offset] = float(value)
            # The same bounds the page timestamps get: a marker from the future
            # would arm the pass for as long as the clock stayed wrong.
            searched = raw.get("searched")
            if (
                isinstance(searched, bool)
                or not isinstance(searched, (int, float))
                or not 0 < float(searched) <= time.time() + FEARLESS_INDEX_MAX_AGE_SECONDS
            ):
                searched = 0.0
            return pages, total_pages, total_topics, fetched, page_step, float(searched)
        except (OSError, ValueError, TypeError):
            return self._no_fearless_index()

    async def _thecheatscript(self, game_aliases: list[str]) -> list[ArtifactRecord]:
        definition = provider_definition("thecheatscript")
        if self.diagnostics is not None:
            try:
                if not self.diagnostics.can_attempt(definition.provider):
                    raise ProviderCooldown("provider is in persisted cooldown")
            except ValueError:
                pass

        async def get(
            url: str,
            *,
            allowed_hosts: frozenset[str],
            max_bytes: int,
            referer: str | None = None,
        ):
            response = await self.network.get(
                url,
                allowed_hosts=allowed_hosts,
                max_bytes=max_bytes,
                headers={"Accept": "application/json"} if url.startswith("https://cloud-api.yandex.net/") else None,
                referer=referer,
            )
            if response.status == 429:
                raise ProviderRateLimited(response.headers.get("retry-after"))
            if response.status == 403:
                raise ProviderChallenged(f"provider answered HTTP 403 for {urlsplit(url).path[:120]}")
            return response

        unique_refs = self._load_thecheatscript_sitemap_cache()
        if unique_refs is None:
            sitemap_url = f"{THECHEATSCRIPT_BASE_URL}sitemap.xml"
            response = await get(
                sitemap_url,
                allowed_hosts=THECHEATSCRIPT_SITE_HOSTS,
                max_bytes=MAX_SITEMAP_BYTES,
            )
            if response.status != 200:
                raise NetworkError(f"The Cheat Script sitemap returned HTTP {response.status}")
            try:
                sitemap_pages = parse_thecheatscript_sitemap_index(response.body)
            except (ValueError, TypeError) as exc:
                raise NetworkError(f"The Cheat Script sitemap could not be read: {exc}") from exc

            refs: list[TheCheatScriptPostRef] = []
            pages_read = 0
            for page_url in sitemap_pages:
                try:
                    page = await get(
                        page_url,
                        allowed_hosts=THECHEATSCRIPT_SITE_HOSTS,
                        max_bytes=MAX_SITEMAP_BYTES,
                        referer=sitemap_url,
                    )
                    if page.status != 200:
                        _note_parse_issue(f"sitemap page returned HTTP {page.status}", critical=True)
                        continue
                    page_refs = parse_thecheatscript_sitemap_page(page.body)
                    if len(refs) + len(page_refs) > THECHEATSCRIPT_MAX_SITEMAP_ENTRIES:
                        raise NetworkError("The Cheat Script sitemap has too many entries")
                    refs.extend(page_refs)
                    pages_read += 1
                except ProviderRateLimited:
                    raise
                except ProviderChallenged as exc:
                    # The pages after this one are the same challenged site, and
                    # read as ordinary unreadable pages the crawl kept asking it
                    # for every one of them. The posts this would go on to fetch
                    # are that site too, so the job stops here rather than
                    # spending them to be challenged one at a time.
                    if pages_read == 0:
                        raise
                    _note_provider_unavailable(str(exc)[:2048])
                    return []
                except (NetworkError, ValueError, TypeError) as exc:
                    _note_parse_issue(f"sitemap page unreadable: {exc}", critical=True)
            if pages_read == 0:
                raise NetworkError("The Cheat Script sitemap pages could not be read")
            unique_refs = tuple({ref.url: ref for ref in refs}.values())
            if pages_read == len(sitemap_pages):
                try:
                    self._save_thecheatscript_sitemap_cache(unique_refs)
                except (OSError, ValueError) as exc:
                    log_failure(
                        self.logger,
                        "catalog.thecheatscript_cache_failed",
                        exc,
                        expected=True,
                        entries=len(unique_refs),
                    )
            else:
                # What was read is still the best answer this search can give,
                # and it is used for this search. It is not written down as the
                # index: a page that failed once would otherwise take every post
                # it declared out of every search for the next six hours, and
                # nothing would ever say so.
                log_activity(
                    self.logger, "warning", "catalog.thecheatscript_partial_index",
                    pages_read=pages_read, pages_declared=len(sitemap_pages), entries=len(unique_refs),
                )
        ranked = rank_thecheatscript_posts(
            unique_refs,
            lambda title: match_breakdown(
                game_aliases, Candidate(title, provider_trust=0.70)
            ).confidence,
            limit=MAX_TOPIC_FETCHES,
        )
        rows: list[ArtifactRecord] = _provider_rows()
        for ref, score in ranked:
            try:
                post = await get(
                    ref.url,
                    allowed_hosts=THECHEATSCRIPT_SITE_HOSTS,
                    max_bytes=MAX_HTML_BYTES,
                    referer=THECHEATSCRIPT_BASE_URL,
                )
            except ProviderRateLimited:
                raise
            except ProviderChallenged as exc:
                # Skipped page by page, a provider challenging every matching
                # post returned an empty list that read as a successful search.
                _note_provider_unavailable(str(exc)[:2048])
                break
            except NetworkError as exc:
                _note_parse_issue(f"post unreadable: {exc}", critical=True)
                continue
            if post.status != 200:
                _note_parse_issue(f"post returned HTTP {post.status}", critical=True)
                continue
            try:
                artifacts = parse_thecheatscript_post(decode_html(post.body))
            except (ValueError, TypeError) as exc:
                _note_parse_issue(f"post unreadable: {exc}", critical=True)
                continue
            for artifact in artifacts:
                if not artifact.supported:
                    _note_parse_issue("row skipped: unsupported legacy artifact host")
                    continue
                try:
                    request = TheCheatScriptDownloadRequest(artifact.link)
                    metadata = await get(
                        thecheatscript_public_metadata_url(request),
                        allowed_hosts=THECHEATSCRIPT_API_HOSTS,
                        max_bytes=THECHEATSCRIPT_MAX_API_BYTES,
                    )
                except ProviderRateLimited:
                    raise
                except (NetworkError, ValueError) as exc:
                    _note_parse_issue(f"row skipped: public metadata unavailable: {exc}")
                    continue
                if metadata.status != 200:
                    _note_parse_issue(f"row skipped: public metadata returned HTTP {metadata.status}")
                    continue
                try:
                    payload = json.loads(metadata.body)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    _note_parse_issue(f"row skipped: public metadata is invalid JSON: {exc}")
                    continue
                record = thecheatscript_record(ref, artifact, payload, score)
                if record is not None:
                    rows.append(record)
        return rows

    async def _vgtimes(self, game_aliases: list[str]) -> list[ArtifactRecord]:
        """Rank the games the sitemap declares, then read only their tables.

        The game is its own path segment here and tables have their own listing,
        so `/games/<slug>/files/cheats/tables/` is a table-only answer: unlike
        Playground, no entry has to be opened to find out whether it is a table
        rather than a trainer or a save editor. That makes the whole cost of a
        search one cached slug index plus a listing and an item page per ranked
        game.
        """
        definition = provider_definition("vgtimes")
        if self.diagnostics is not None:
            try:
                if not self.diagnostics.can_attempt(definition.provider):
                    raise ProviderCooldown("provider is in persisted cooldown")
            except ValueError:
                pass

        async def get(url: str, *, max_bytes: int, referer: str | None = None):
            response = await self.network.get(
                url, allowed_hosts=VGTIMES_SITE_HOSTS, max_bytes=max_bytes, referer=referer,
            )
            if response.status == 429:
                raise ProviderRateLimited(response.headers.get("retry-after"))
            if response.status == 403:
                raise ProviderChallenged(f"provider answered HTTP 403 for {urlsplit(url).path[:120]}")
            return response

        slugs = self._load_vgtimes_slug_cache()
        if slugs is None:
            response = await get(f"{VGTIMES_BASE_URL}sitemap.xml", max_bytes=VGTIMES_MAX_SITEMAP_BYTES)
            if response.status != 200:
                raise NetworkError(f"VGTimes sitemap returned HTTP {response.status}")
            try:
                shards = vgtimes_game_sitemap_urls(parse_vgtimes_sitemap_index(response.body))
            except (ValueError, TypeError) as exc:
                raise NetworkError(f"VGTimes sitemap could not be read: {exc}") from exc
            if not shards:
                raise NetworkError("VGTimes sitemap declares no game shards")
            found: list[str] = []
            shards_read = 0
            capped = False
            for shard in shards:
                try:
                    page = await get(shard, max_bytes=VGTIMES_MAX_SITEMAP_BYTES, referer=VGTIMES_BASE_URL)
                    if page.status != 200:
                        _note_parse_issue(f"sitemap shard returned HTTP {page.status}", critical=True)
                        continue
                    games = parse_vgtimes_game_sitemap(vgtimes_decompress(page.body))
                    found.extend(game.slug for game in games)
                    shards_read += 1
                except ProviderRateLimited:
                    raise
                except ProviderChallenged as exc:
                    # The listings and item pages this would go on to read are
                    # the same challenged site.
                    if shards_read == 0:
                        raise
                    _note_provider_unavailable(str(exc)[:2048])
                    return []
                except (NetworkError, ValueError, TypeError, OSError) as exc:
                    _note_parse_issue(f"sitemap shard unreadable: {exc}", critical=True)
                if len(found) >= MAX_VGTIMES_SLUGS:
                    capped = True
                    break
            if shards_read == 0:
                raise NetworkError("VGTimes sitemap shards could not be read")
            slugs = tuple(dict.fromkeys(found))[:MAX_VGTIMES_SLUGS]
            if shards_read == len(shards) and not capped:
                try:
                    self._save_vgtimes_slug_cache(slugs)
                except (OSError, ValueError) as exc:
                    log_failure(
                        self.logger, "catalog.vgtimes_cache_failed", exc, expected=True, entries=len(slugs),
                    )
            else:
                # Used for this search, never written down as the index. A shard
                # that failed once would otherwise take every game it declared
                # out of every search until the cache expired.
                log_activity(
                    self.logger, "warning", "catalog.vgtimes_partial_index",
                    shards_read=shards_read, shards_declared=len(shards), capped=capped, slugs=len(slugs),
                )
        # The same cheap gate Playground's sitemap needs, and for the same
        # reason: scoring tens of thousands of slugs dominates a search, and a
        # slug is already lowercase ASCII words separated by hyphens.
        tokens = sitemap_prefilter_tokens(game_aliases)
        ranked: list[tuple[float, str]] = []
        for slug in slugs:
            title = VGTimesGameRef(slug, "").title
            if tokens and not any(token in slug for token in tokens):
                continue
            score = match_breakdown(game_aliases, Candidate(title, provider_trust=0.65)).confidence
            if score >= 0.55:
                ranked.append((score, slug))
        ranked.sort(reverse=True)
        rows: list[ArtifactRecord] = _provider_rows()
        for score, slug in ranked[:MAX_VGTIMES_GAMES]:
            listing_url = vgtimes_tables_listing_url(slug)
            try:
                listing = await get(listing_url, max_bytes=MAX_HTML_BYTES, referer=VGTIMES_BASE_URL)
            except ProviderRateLimited:
                raise
            except ProviderChallenged as exc:
                _note_provider_unavailable(str(exc)[:2048])
                break
            except NetworkError as exc:
                _note_parse_issue(f"table listing unreadable: {exc}", critical=True)
                continue
            if listing.status == 404:
                # A game with no tables at all is an answer, not a failure.
                continue
            if listing.status != 200:
                _note_parse_issue(f"table listing returned HTTP {listing.status}", critical=True)
                continue
            try:
                refs = parse_vgtimes_tables_listing(decode_html(listing.body), slug)
            except (ValueError, TypeError) as exc:
                _note_parse_issue(f"table listing unreadable: {exc}", critical=True)
                continue
            for ref in refs[:MAX_VGTIMES_ITEMS]:
                try:
                    item = await get(ref.url, max_bytes=MAX_HTML_BYTES, referer=listing_url)
                except ProviderRateLimited:
                    raise
                except ProviderChallenged as exc:
                    _note_provider_unavailable(str(exc)[:2048])
                    return rows
                except NetworkError as exc:
                    _note_parse_issue(f"item page unreadable: {exc}", critical=True)
                    continue
                if item.status != 200:
                    _note_parse_issue(f"item page returned HTTP {item.status}", critical=True)
                    continue
                try:
                    metadata = parse_vgtimes_item(decode_html(item.body))
                except (ValueError, TypeError) as exc:
                    _note_parse_issue(f"item page unreadable: {exc}", critical=True)
                    continue
                record = vgtimes_record(ref, metadata, score)
                if record is not None:
                    rows.append(record)
        return rows

    async def _playground(self, game_aliases: list[str]) -> list[ArtifactRecord]:
        if self.diagnostics is not None:
            try:
                if not self.diagnostics.can_attempt("playground"):
                    raise ProviderCooldown("provider is in persisted cooldown")
            except ValueError:
                pass
        async def get(url: str, *, max_bytes: int, referer: str | None = None, headers=None, hosts=None):
            """Every Playground read, so a rate limit is one everywhere.

            None of these translated 429 before. A limit on the sitemap fell
            through to the cache and the crawl carried on asking the provider it
            had just been told to wait for; a limit on a page or a category was
            simply "not 200", so no cooldown was established and the diagnostics
            never learned it had been throttled at all.
            """
            response = await self.network.get(
                url,
                allowed_hosts=hosts or PROVIDER_HOSTS["playground"],
                max_bytes=max_bytes,
                headers=headers,
                referer=referer,
            )
            if response.status == 429:
                raise ProviderRateLimited(response.headers.get("retry-after"))
            if response.status == 403:
                raise ProviderChallenged(f"provider answered HTTP 403 for {urlsplit(url).path[:120]}")
            return response

        sitemap_url = f"{PLAYGROUND_BASE_URL}sitemap/cheat/list.xml"
        cache = self._load_sitemap_cache()
        cached = cache.get("entries")
        cached_index = cached if isinstance(cached, list) and cached else None
        headers: dict[str, str] = {}
        if cache.get("etag"):
            headers["If-None-Match"] = str(cache["etag"])
        if cache.get("last_modified"):
            headers["If-Modified-Since"] = str(cache["last_modified"])

        # An index this device already has must not hold up a search somebody is
        # watching. Measured here, this file is 11.9 MB across 50000 games and
        # took 37 of one search's 45 seconds because the copy upstream had
        # changed eleven minutes earlier, so the conditional request came back
        # 200 rather than 304; the same file loads from disk in 30 milliseconds.
        # The request is still made, because a 304 is usually immediate and
        # answering from a fresh index is better than answering from a stale
        # one, but it gets a few seconds of its own rather than the search's
        # whole allowance, and what it cannot deliver in that time it delivers
        # to the next search instead.
        #
        # A rate limit or a challenge is deliberately not caught here: those are
        # the provider saying stop, they establish its cooldown, and a cached
        # index is not a reason to carry on asking.
        if cached_index is not None and self._playground_index_is_current(cache):
            # Young enough that asking is a round trip for an answer already
            # known, and while the site is slow it is five seconds of one.
            index: object = cached_index
        elif cached_index is not None:
            self._note_search_stage("playground", "checking the site index")
            index = cached_index
            try:
                response = await asyncio.wait_for(
                    get(sitemap_url, max_bytes=MAX_SITEMAP_BYTES, headers=headers),
                    PROVIDER_INDEX_REFRESH_SECONDS,
                )
            except asyncio.TimeoutError:
                # Slow right now says nothing about how long it will take, so
                # the whole of it is fetched behind the search it would have
                # held up, for the search after this one.
                self._start_playground_sitemap(headers)
                response = None
            if response is not None and response.status == 304:
                # The one answer that proves the copy on disk is current, which
                # is what the six hours are counted from. A 500 proves nothing
                # about freshness, and counting it would hide a changed index
                # behind a server that was briefly unwell.
                self._playground_index_checked_at = time.time()
            elif response is not None and response.status == 200:
                # Nothing here may cost the search the answer it already has.
                # The copy on disk was usable before this request and a refresh
                # that cannot be read, or cannot be written down, does not make
                # it less so.
                try:
                    index = parse_playground_sitemap(response.body)
                    self._playground_index_checked_at = time.time()
                except (ValueError, TypeError) as exc:
                    log_failure(
                        self.logger, "playground.sitemap_unreadable", exc, expected=True,
                    )
                else:
                    try:
                        self._write_playground_sitemap(response, index)
                    except (OSError, ValueError) as exc:
                        # Read but not kept: this search uses it and the next
                        # one asks again, which is the ordinary consequence of
                        # a cache that could not be written.
                        self._playground_index_checked_at = None
                        log_failure(
                            self.logger, "playground.sitemap_not_written", exc, expected=True,
                        )
            # Anything else this device is not going to act on leaves the copy
            # it already had standing, and leaves it due for another look.
        else:
            # Nothing to answer from, so this one is the search.
            self._note_search_stage("playground", "reading the site index")
            response = await get(sitemap_url, max_bytes=MAX_SITEMAP_BYTES, headers=headers)
            if response.status == 200:
                index = parse_playground_sitemap(response.body)
                self._write_playground_sitemap(response, index)
            else:
                raise NetworkError(f"Playground sitemap returned HTTP {response.status}")
        ranked: list[tuple[float, str, str, str]] = []
        tokens = sitemap_prefilter_tokens(game_aliases)
        for raw in index if isinstance(index, list) else []:
            if not isinstance(raw, (list, tuple)) or len(raw) != 3:
                continue
            page_id, title, url = map(str, raw)
            if tokens and not any(token in title.casefold() for token in tokens):
                continue
            score = match_breakdown(game_aliases, Candidate(title, provider_trust=0.65)).confidence
            if score >= 0.55:
                ranked.append((score, page_id, title, url))
        ranked.sort(reverse=True)
        selected, posted_by_url = await self._playground_table_pages(ranked, get)
        rows: list[ArtifactRecord] = _provider_rows()
        fetching = selected[:MAX_TOPIC_FETCHES]
        for index, (score, _, _, url) in enumerate(fetching, start=1):
            self._note_search_stage("playground", f"reading page {index} of {len(fetching)}")
            try:
                response = await get(url, max_bytes=MAX_HTML_BYTES, referer=PLAYGROUND_BASE_URL)
            except ProviderChallenged as exc:
                # Whatever pages were already read stay in the answer, and the
                # search still says the source stopped serving it. Letting this
                # leave the job would have thrown those rows away.
                _note_provider_unavailable(str(exc)[:2048])
                return rows
            if response.status != 200:
                continue
            try:
                page_rows, source = parse_playground_page(decode_html(response.body), url, score)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                _note_parse_issue(f"page unreadable: {exc}", critical=True)
                continue
            posted = posted_by_url.get(canonical_url(url))
            if posted is not None:
                page_rows = [
                    replace(record, result=replace(record.result, posted_at=posted))
                    for record in page_rows
                ]
            rows.extend(page_rows)
            if source:
                host = (urlsplit(source).hostname or "").casefold()
                if host in {"github.com", "www.github.com"}:
                    try:
                        rows.extend(await self._linked_source("github", lambda: self._github(source, score)))
                    except (NetworkError, ValueError, TypeError, KeyError):
                        # The general Playground result remains useful even when
                        # its optional exact GitHub source cannot be resolved,
                        # or answers with a payload this cannot read.
                        pass
                elif host in PROVIDER_HOSTS["fearless"]:
                    fearless = provider_definition("fearless")
                    match = TOPIC_RE.search(urlsplit(source).query)
                    if match:
                        async def read_topic():
                            direct = await self.network.get(source, allowed_hosts=fearless.hosts, max_bytes=MAX_HTML_BYTES, referer=url)
                            if direct.status == 429:
                                raise ProviderRateLimited(direct.headers.get("retry-after"))
                            if direct.status != 200:
                                # Raised rather than returned empty, so the
                                # provider that answered is the one it is
                                # recorded against. The Playground row this
                                # enriches is unaffected either way.
                                raise NetworkError(f"FearLess topic returned HTTP {direct.status}")
                            return rows_from_page(lambda: parse_phpbb_attachments(decode_html(direct.body), provider=fearless.provider, display_name=fearless.provider_display_name, topic_id=match.group(1), topic_title=page_rows[0].result.table_title if page_rows else url, source_page=source, score=score, rank=fearless.priority, hosts=fearless.hosts))

                        try:
                            rows.extend(await self._linked_source("fearless", read_topic))
                        except (NetworkError, ValueError, TypeError, KeyError):
                            pass
        return rows

    async def _linked_source(
        self, provider: str, read: Callable[[], Awaitable[list[ArtifactRecord]]]
    ) -> list[ArtifactRecord]:
        """Read another provider's exact page, on that provider's own terms.

        A page one source publishes may name an exact repository or topic at
        another. That read belongs to the provider that answers it, not to the
        one that linked it: it used to skip that provider's cooldown entirely,
        so a source deliberately waiting still received requests because
        something else pointed at it, and any refusal was attributed to nobody
        and recorded nowhere. The linking provider's own row is unaffected
        either way, which is why the caller keeps swallowing the failure.

        A source the user switched off is not read here either. This is the
        route that made switching one off incomplete: a Playground page can name
        the exact GitHub repository a table lives in, so a user who turned
        GitHub off still had GitHub contacted, and a GitHub row still appeared
        in the answer, because something else had pointed at it.
        """
        definition = PROVIDER_REGISTRY.get(provider)
        if definition is None or not definition.linked_target:
            # A defect here rather than an outage there: a page naming a
            # provider the registry does not declare as a linked target is a
            # route nothing said existed, and the screen that describes what
            # switching a source off stops is built from that declaration.
            log_activity(
                self.logger, "error", "catalog.linked_source_not_declared", provider=provider,
            )
            return []
        # The selection this search began with, not whatever it is now: one
        # search acts on one answer, or the roster it returns describes a set of
        # sources that is not the set it actually read.
        switched_off = _SEARCH_SELECTION.get()
        if switched_off is None:
            switched_off = self.disabled_provider_ids()
        if provider in switched_off:
            log_activity(
                self.logger, "info", "catalog.linked_source_switched_off", provider=provider,
            )
            return []
        if self.diagnostics is not None:
            try:
                if not self.diagnostics.can_attempt(provider):
                    log_activity(
                        self.logger, "info", "catalog.linked_source_in_cooldown", provider=provider,
                    )
                    return []
            except ValueError:
                pass
        # Its own collector, so what could not be read on this provider's page
        # is counted against this provider. Nested inside the calling job's
        # collector, every unreadable page a linked read met was added to the
        # provider that linked it, which is the same misattribution as the rows.
        try:
            with collect_parse_issues() as issues:
                rows = await read()
        except ProviderRateLimited as exc:
            if self.diagnostics is not None:
                try:
                    self.diagnostics.record_failure(
                        provider, error=_diagnostic_error(exc), http_status=429, retry_after=exc.retry_after,
                    )
                except ValueError:
                    pass
            raise
        except (NetworkError, ValueError, TypeError, KeyError) as exc:
            # The caller keeps its own row whatever happened here, and that is
            # why this failure had nowhere to go: swallowed there, the provider
            # that actually answered recorded nothing at all, so a source
            # answering 500 for every repository another page names looked
            # perfectly healthy in Advanced.
            if self.diagnostics is not None:
                try:
                    self.diagnostics.record_linked_failure(provider, error=_diagnostic_error(exc))
                except ValueError:
                    pass
            raise
        self._report_parse_issues(provider, issues, results=len(rows))
        if rows and self.diagnostics is not None:
            try:
                # Counted as a linked read rather than as a search, because no
                # search of this provider ran. Crediting these rows to the
                # provider that linked them made one Playground row and two
                # GitHub rows read as three Playground results and nothing at
                # all from GitHub, on the one screen that says what each source
                # is doing and offers to switch it off.
                self.diagnostics.record_linked_success(provider, results=len(rows))
            except ValueError:
                pass
        return rows

    async def _playground_table_pages(
        self, ranked: list[tuple[float, str, str, str]], get: Callable[..., Awaitable[object]]
    ) -> tuple[list[tuple[float, str, str, str]], dict[str, str | None]]:
        """Keep only the pages this game's `table` category actually lists.

        The sitemap covers every cheat kind, so ranking alone returns trainers,
        save files, save editors and cheat mods that no `.CT` workflow can use,
        and each one costs its own page fetch. When the category listing cannot
        be read the ranked order is returned unchanged, so a listing change
        degrades to the previous behavior instead of hiding real tables.
        """
        slugs: list[str] = []
        for _, _, _, url in ranked:
            slug = playground_game_slug(url)
            if slug and slug not in slugs:
                slugs.append(slug)
            if len(slugs) >= MAX_PLAYGROUND_CATEGORY_SLUGS:
                break
        if not slugs:
            return ranked, {}
        listed: dict[str, str | None] = {}
        entries = 0
        for slug in slugs:
            category_url = f"{PLAYGROUND_BASE_URL}{slug}/cheat/{PLAYGROUND_TABLE_CATEGORY}"
            try:
                response = await get(
                    category_url, max_bytes=MAX_HTML_BYTES, referer=PLAYGROUND_BASE_URL,
                )
            except ProviderRateLimited:
                raise
            except ProviderChallenged:
                # A challenge is not a listing this could not read. Degraded to
                # the ranked fallback it looked like one, and the item reads
                # that follow are against the same challenged site.
                raise
            except NetworkError:
                return ranked, {}
            if response.status != 200:
                return ranked, {}
            try:
                slug_pages, slug_entries = parse_playground_category(decode_html(response.body), slug)
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                _note_parse_issue(f"category listing unreadable: {exc}", critical=True)
                continue
            listed.update(slug_pages)
            entries += slug_entries
        if entries == 0:
            # Nothing was readable at all, so the listing itself is the doubt.
            return ranked, {}
        # A listing that carried entries and no table is a real answer: this game
        # has trainers or saves and no Cheat Engine table. Returning the ranked
        # order here would put a save file at the top of a table search.
        return [row for row in ranked if canonical_url(row[3]) in listed], listed

    async def _github(self, source: str, score: float) -> list[ArtifactRecord]:
        repo = github_repo_from_source(urlsplit(source).path)
        if repo is None:
            return []
        return await self._github_release_rows(repo, score)

    async def _github_api(self, url: str) -> object:
        """One anonymous API call, with the budget read as a wait.

        GitHub reports an exhausted anonymous budget as 403 with a remaining
        count of zero and a reset timestamp rather than as 429 with a
        `Retry-After`, so both shapes are turned into the one cooldown the rest
        of the catalog already knows how to wait out.
        """
        response = await self.network.get(
            url,
            allowed_hosts=PROVIDER_HOSTS["github"],
            max_bytes=MAX_GITHUB_API_BYTES,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        )
        if github_is_rate_limited(response.status, response.headers):
            raise ProviderRateLimited(github_retry_after(response.headers), "GitHub anonymous API budget is exhausted")
        if response.status != 200:
            raise NetworkError(f"GitHub returned HTTP {response.status}")
        try:
            return json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NetworkError(f"GitHub answer is not valid JSON: {exc}") from exc

    async def _github_release_rows(self, repo: str, score: float) -> list[ArtifactRecord]:
        payload = await self._github_api(github_releases_url(repo))
        return parse_github_releases(payload, repo, score)

    @property
    def _github_index_cache_path(self):
        return self.cache_path.with_name("github-repository-index.json")

    def _github_index_cache_entries(self) -> list[dict[str, object]]:
        raw = load_json(
            self._github_index_cache_path, {}, max_bytes=MAX_GITHUB_INDEX_CACHE_BYTES,
        )
        if raw == {}:
            return []
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            raise ValueError("GitHub repository index cache schema is invalid")
        entries = raw.get("queries")
        if not isinstance(entries, list) or len(entries) > MAX_GITHUB_INDEX_QUERIES:
            raise ValueError("GitHub repository index cache query list is invalid")
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _github_candidate_cache_item(candidate: GitHubRepoCandidate) -> dict[str, object]:
        return {
            "full_name": candidate.full_name,
            "description": candidate.description,
            "stargazers_count": candidate.stars,
            "pushed_at": candidate.pushed_at,
            "topics": list(candidate.topics),
            "archived": candidate.archived,
            "fork": candidate.fork,
            "default_branch": candidate.default_branch,
            "html_url": candidate.html_url,
        }

    def _write_github_index_cache(self, entries: list[dict[str, object]]) -> None:
        def used_at(entry: dict[str, object]) -> float:
            value = entry.get("used")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return 0.0
            return float(value) if math.isfinite(float(value)) else 0.0

        ordered = sorted(entries, key=used_at)
        atomic_write_json(
            self._github_index_cache_path,
            {"schema": 1, "queries": ordered[-MAX_GITHUB_INDEX_QUERIES:]},
            max_bytes=MAX_GITHUB_INDEX_CACHE_BYTES,
        )

    def _load_github_index_query(
        self, query: str,
    ) -> tuple[tuple[GitHubRepoCandidate, ...] | None, bool]:
        """Return a bounded cached repository answer and whether it is fresh."""
        try:
            entries = self._github_index_cache_entries()
            now = time.time()
            for entry in entries:
                if entry.get("query") != query:
                    continue
                fetched = entry.get("fetched")
                items = entry.get("items")
                if (
                    isinstance(fetched, bool)
                    or not isinstance(fetched, (int, float))
                    or not math.isfinite(float(fetched))
                    or fetched > now
                    or now - fetched > GITHUB_INDEX_STALE_SECONDS
                    or not isinstance(items, list)
                ):
                    raise ValueError("GitHub repository index cache entry is invalid")
                candidates = parse_github_repository_search({"items": items})
                # This is a real LRU rather than an insertion-order cap. A cache
                # hit is advisory, so a failed touch never costs the answer.
                entry["used"] = now
                try:
                    self._write_github_index_cache(entries)
                except (OSError, ValueError) as exc:
                    log_failure(
                        self.logger, "catalog.github_index_cache_touch_failed", exc,
                        expected=True,
                    )
                return candidates, now - fetched <= GITHUB_INDEX_TTL_SECONDS
        except (OSError, ValueError, TypeError) as exc:
            log_failure(
                self.logger, "catalog.github_index_cache_unreadable", exc,
                expected=True,
            )
        return None, False

    def _save_github_index_query(
        self, query: str, candidates: tuple[GitHubRepoCandidate, ...],
    ) -> None:
        now = time.time()
        try:
            try:
                entries = self._github_index_cache_entries()
            except (OSError, ValueError, TypeError):
                # Optional cache damage is repaired by the fresh answer that was
                # just parsed; it must never turn that answer into a failed search.
                entries = []
            entries = [entry for entry in entries if entry.get("query") != query]
            entries.append({
                "query": query,
                "fetched": now,
                "used": now,
                "items": [self._github_candidate_cache_item(candidate) for candidate in candidates],
            })
            self._write_github_index_cache(entries)
        except (OSError, ValueError, TypeError) as exc:
            log_failure(
                self.logger, "catalog.github_index_cache_write_failed", exc,
                expected=True,
            )

    def _github_search_refused(self) -> ProviderRateLimited | None:
        """The repository-search refusal still in force, or nothing.

        Read before every repository-search request, so one refusal ends the
        search requests of the whole run rather than being met once per alias.
        The other two GitHub routes never ask: they are metered separately and
        are the ones that turn a cached repository into rows.
        """
        if self._github_search_refusal is None:
            return None
        if time.time() >= self._github_search_cooldown_until:
            self._github_search_refusal = None
            self._github_search_cooldown_until = 0.0
            return None
        return self._github_search_refusal

    def _latch_github_search_refusal(self, exc: ProviderRateLimited) -> None:
        """Hold a repository-search refusal for as long as GitHub asked."""
        cooldown = parse_retry_after(exc.retry_after) or GITHUB_SEARCH_DEFAULT_COOLDOWN_SECONDS
        self._github_search_refusal = exc
        self._github_search_cooldown_until = time.time() + cooldown

    async def _github_search(self, game_aliases: list[str]) -> list[ArtifactRecord]:
        """Find repositories that carry a table for this game, then read them.

        GitHub is not a catalog of games and CE Decky ships no per-game
        registry, so a repository is reached by searching the index at run time
        and is then offered only when it actually carries something to download.
        That last part is a fact about the repository rather than a guess about
        its name: the live spike found single-file repositories whose whole
        content was a README promising a table, and an application whose
        description claimed to be one.

        A refused repository search is never raised out of here. GitHub meters
        that route apart from the release and tree reads that actually produce
        the rows, so turning it into a failure of the source wrote a provider
        cooldown for a budget one of three routes spends, and the next search of
        any game was then refused before it could read the index already on this
        disk. What the refusal does is latch, so no further search request is
        made until GitHub's own deadline, and say so through the search's
        partially-available report. A release or tree read that is refused still
        raises: that one is the core budget, and it is the whole source.
        """
        definition = provider_definition("github")
        if self.diagnostics is not None:
            try:
                if not self.diagnostics.can_attempt(definition.provider):
                    raise ProviderCooldown("provider is in persisted cooldown")
            except ValueError:
                pass
        candidates: list[GitHubRepoCandidate] = []
        queries = github_search_queries(game_aliases)
        if not queries:
            return []
        for query in queries:
            cached, fresh = self._load_github_index_query(query)
            if fresh and cached is not None:
                candidates.extend(cached)
                continue
            # An exhausted search budget is exactly a refresh this cannot make,
            # which is what the stale window is for. A search asks for one index
            # per alias, so meeting that refusal once per alias spent the rest of
            # them on an answer already known: the second alias raised the
            # refusal again with nothing cached behind it, and the whole source
            # failed carrying the candidates the first alias had just recovered.
            # The refusal is latched instead, and every alias after it is served
            # from whatever copy is on this disk.
            refused = self._github_search_refused()
            if refused is None:
                try:
                    payload = await self._github_api(github_search_url(query))
                except ProviderRateLimited as exc:
                    self._latch_github_search_refusal(exc)
                    refused = exc
                except NetworkError as exc:
                    if cached is None:
                        raise
                    candidates.extend(cached)
                    _note_provider_unavailable(
                        f"GitHub repository index unavailable; using a stale local copy: {exc}"
                    )
                    continue
                else:
                    try:
                        parsed = parse_github_repository_search(payload)
                        candidates.extend(parsed)
                        self._save_github_index_query(query, parsed)
                    except (ValueError, TypeError) as exc:
                        _note_parse_issue(f"repository search unreadable: {exc}", critical=True)
                    continue
            if cached is not None:
                candidates.extend(cached)
                _note_provider_unavailable(
                    f"GitHub repository index unavailable; using a stale local copy: {refused}"
                )
                continue
            # No copy of this alias's index at all. That costs this alias and
            # nothing else: an alias that was answered, from the network or from
            # the disk, keeps its candidates and they still reach the live
            # release and tree reads below.
            _note_provider_unavailable(
                f"GitHub repository index unavailable and no local copy to fall back on: {refused}"
            )
        ranked = rank_github_repositories(
            tuple(candidates),
            lambda title: match_breakdown(
                game_aliases, Candidate(title, provider_trust=0.55)
            ).confidence,
            limit=GITHUB_MAX_CANDIDATES,
        )
        rows: list[ArtifactRecord] = _provider_rows()
        for candidate, score in ranked:
            rows.extend(await self._github_repository_rows(candidate, score))
        return rows

    async def _github_repository_rows(
        self, candidate: GitHubRepoCandidate, score: float
    ) -> list[ArtifactRecord]:
        """Releases first, because that is where tables actually are.

        The best maintained table the spike found commits no artifact at all: it
        publishes a release asset assembled by its own CI from thousands of
        fragments. The tree is read only when the releases carry nothing, so a
        repository costs one core request out of sixty an hour rather than two.
        """
        try:
            rows = await self._github_release_rows(candidate.full_name, score)
        except ProviderRateLimited:
            raise
        except (NetworkError, ValueError, TypeError) as exc:
            _note_parse_issue(f"repository releases unreadable: {exc}", critical=True)
            return []
        if rows:
            return rows
        branch = candidate.default_branch
        if branch is None:
            # No branch this understands means no raw file route, and guessing
            # the likeliest one would be guessing a path. The repository keeps
            # whatever its releases offered, which here is nothing.
            _note_parse_issue("repository tree skipped: no usable default branch")
            return []
        try:
            payload = await self._github_api(github_tree_url(candidate.full_name, branch))
            tables = parse_github_tree_tables(payload)
        except ProviderRateLimited:
            raise
        except (NetworkError, ValueError, TypeError) as exc:
            _note_parse_issue(f"repository tree unreadable: {exc}", critical=True)
            return []
        found = [github_tree_record(candidate, table, score) for table in tables]
        return [row for row in found if row is not None]

    @staticmethod
    def _dedupe(rows: Iterable[ArtifactRecord]) -> list[ArtifactRecord]:
        selected: dict[tuple[str, str], ArtifactRecord] = {}
        for row in rows:
            key = (row.result.provider, row.result.artifact_id)
            if key not in selected or row.result.match_score > selected[key].result.match_score:
                selected[key] = row
        return [next(row for row in selected.values() if row.result is result) for result in sort_results([row.result for row in selected.values()])]

    def _save_cache(self, query: str, rows: list[ArtifactRecord]) -> None:
        # Freshness is per row, because one answer routinely mixes it: a search
        # that one provider failed re-serves that provider's cached rows beside
        # rows another provider just returned. A single timestamp for the file
        # cannot say both, and whichever way it was written it was wrong for
        # half the answer - stale rows kept being refreshed forever, or a table
        # fetched seconds ago inherited an age of almost seven days.
        now = time.time()
        previous = self._cached_fetch_times(query)
        public_rows: list[dict[str, object]] = []
        for row in rows[:MAX_RESULTS]:
            result = row.result.as_dict()
            # The note goes, whatever the parser made of it. A provider
            # publishes an archive password in the open beside its file, and
            # this file must not hold one however that text reached the row:
            # the guarantee is the cache's own and does not depend on every
            # adapter above it having stripped it first. A stale row therefore
            # loses the uploader's note, which is display text and nothing else.
            result["notes"] = None
            key = f"{row.result.provider}:{row.result.artifact_id}"
            public_rows.append({
                "result": result,
                "advertised_sha256": row.advertised_sha256,
                "fetched": previous.get(key, now) if row.stale else now,
            })
        atomic_write_json(self.cache_path, {
            "schema": 1,
            "query": query,
            "saved": now,
            # Cache only display/source-page data. Direct URLs may be signed or
            # short-lived, and password hints are never persistent state.
            "rows": public_rows,
        })

    def _cached_rows(self, query: str) -> list[dict[str, object]]:
        """The rows this query already has cached, with the age each was given."""
        try:
            raw = load_json(self.cache_path, None, max_bytes=2 * 1024 * 1024)
        except (OSError, ValueError):
            return []
        if not isinstance(raw, dict) or raw.get("schema") != 1 or raw.get("query") != query:
            return []
        rows = raw.get("rows")
        if not isinstance(rows, list):
            return []
        # A file written before rows carried their own age falls back to the
        # file's, which is exactly what it meant then.
        saved = raw.get("saved", 0)
        fallback = float(saved) if isinstance(saved, (int, float)) and not isinstance(saved, bool) else 0.0
        output: list[dict[str, object]] = []
        for item in rows[:MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            fetched = item.get("fetched")
            age = float(fetched) if isinstance(fetched, (int, float)) and not isinstance(fetched, bool) else fallback
            output.append({**item, "fetched": age})
        return output

    def _cached_fetch_times(self, query: str) -> dict[str, float]:
        times: dict[str, float] = {}
        for item in self._cached_rows(query):
            result = item.get("result")
            if not isinstance(result, dict):
                continue
            key = f"{result.get('provider')}:{result.get('artifact_id')}"
            times[key] = float(item["fetched"])
        return times

    def _load_cache(self, query: str, *, disabled: frozenset[str] | None = None) -> list[ArtifactRecord]:
        """The rows still worth re-serving for this query.

        A source the user switched off must not go on answering out of what it
        already wrote here. Nothing about a cached row is weaker than a fresh
        one: it names the same provider, offers the same source page and starts
        the same download, so re-serving it is that source still supplying
        results after being told to stop. Its rows are dropped here, at the one
        point every re-served row passes through, which covers both ways they
        reach an answer - the stale fallback for a provider that failed, and the
        whole cached answer used when a search returns nothing.
        """
        switched_off = self.disabled_provider_ids() if disabled is None else disabled
        try:
            now = time.time()
            output: list[ArtifactRecord] = []
            dropped: dict[str, int] = {}
            for item in self._cached_rows(query):
                # Each row expires on its own age, so a row another provider
                # returned an hour ago is not aged out by one that has been
                # unreachable for a week, and vice versa.
                fetched = float(item["fetched"])
                if not math.isfinite(fetched) or fetched > now or now - fetched > STALE_TTL_SECONDS:
                    continue
                # One unreadable cached row is not a reason to withhold the rest
                # of a stale answer, which is all the user has while the
                # provider is unreachable.
                try:
                    result = CatalogResult(**item["result"])
                except (ValueError, TypeError, KeyError):
                    continue
                if result.provider in switched_off:
                    dropped[result.provider] = dropped.get(result.provider, 0) + 1
                    continue
                if result.download_mode == "direct_https":
                    result = replace(result, download_mode="source_handoff")
                # By name, because these were positional and a field inserted
                # in the middle of the record silently made every restored row
                # claim it needed an archive password and stop being stale.
                output.append(ArtifactRecord(
                    result,
                    advertised_sha256=item.get("advertised_sha256"),
                    stale=True,
                ))
            if dropped:
                log_activity(
                    self.logger, "info", "catalog.cached_rows_switched_off",
                    providers=";".join(f"{provider}:{count}" for provider, count in sorted(dropped.items())),
                    served=len(output),
                )
            return output
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def _playground_index_is_current(self, cache: dict[str, object]) -> bool:
        """Whether the copy on disk is recent enough to use without asking."""
        if _index_within_ttl(cache.get("retrieved"), PLAYGROUND_INDEX_TTL_SECONDS):
            return True
        return _index_within_ttl(self._playground_index_checked_at, PLAYGROUND_INDEX_TTL_SECONDS)

    def _write_playground_sitemap(self, response, index: object) -> None:
        """Keep the index this device answers Playground searches from."""
        atomic_write_json(
            self.cache_path.with_name("playground-sitemap.json"),
            {
                "schema": 1,
                "etag": response.headers.get("etag"),
                "last_modified": response.headers.get("last-modified"),
                "retrieved": time.time(),
                "entries": index,
            },
            max_bytes=MAX_SITEMAP_CACHE_BYTES,
        )

    def _start_playground_sitemap(self, headers: dict[str, str]) -> None:
        """Fetch the whole index behind the search that would have waited on it.

        Bounded by being one task at a time and by starting only where a search
        has just found the request too slow to wait for: it is not a crawl and
        it does not repeat. It is quiesced with the listing crawl beside it,
        because a source the user has switched off, or plugin data being
        deleted, must not have a request outstanding against it either.
        """
        if self._index_suspended:
            return
        if "playground" in self.disabled_provider_ids():
            return
        if self._playground_index_task is not None and not self._playground_index_task.done():
            return
        log_activity(self.logger, "info", "playground.sitemap_refresh_started")
        self._playground_index_task = asyncio.create_task(self._refresh_playground_sitemap(headers))

    async def _refresh_playground_sitemap(self, headers: dict[str, str]) -> None:
        url = f"{PLAYGROUND_BASE_URL}sitemap/cheat/list.xml"
        try:
            response = await self.network.get(
                url,
                allowed_hosts=PROVIDER_HOSTS["playground"],
                max_bytes=MAX_SITEMAP_BYTES,
                headers=headers,
            )
            # Switched off while this was in flight. A source that is off is a
            # source this device does not contact, and an answer that arrives
            # after the switch is not permission to write its index down.
            if "playground" in self.disabled_provider_ids():
                log_activity(self.logger, "info", "playground.sitemap_refresh_switched_off")
                return
            if response.status in (429, 403):
                # What the foreground read makes of these, made of them here
                # too. A refusal collected in the background established no
                # cooldown at all, so the next search walked straight into the
                # provider that had just said no.
                self._record_playground_refusal(response)
                return
            if response.status != 200:
                # A 304 says the copy on disk is current, and anything else is
                # this device's problem to have at the next search rather than
                # in the background: nothing here is waiting on an answer.
                if response.status == 304:
                    self._playground_index_checked_at = time.time()
                log_activity(
                    self.logger, "info", "playground.sitemap_refresh_skipped", status=response.status,
                )
                return
            index = parse_playground_sitemap(response.body)
            self._write_playground_sitemap(response, index)
            self._playground_index_checked_at = time.time()
            log_activity(
                self.logger, "info", "playground.sitemap_refreshed",
                entries=len(index) if isinstance(index, list) else None,
            )
        except Exception as exc:  # noqa: BLE001 - a background refresh fails no search
            log_failure(self.logger, "playground.sitemap_refresh_failed", exc, expected=True)

    def _record_playground_refusal(self, response) -> None:
        """Teach the provider's own state what the background read was told.

        A rate limit or a challenge is the provider speaking about itself, and
        it is the same statement whether a user was waiting for the request or
        not. Written here rather than raised, because nothing is waiting on this
        one: what it must not do is leave the next search to discover it again.
        """
        if response.status == 429:
            exc: Exception = ProviderRateLimited(response.headers.get("retry-after"))
            status, retry_after = 429, response.headers.get("retry-after")
        else:
            exc = ProviderChallenged("provider answered HTTP 403 for the site index")
            status, retry_after = 403, None
        log_failure(
            self.logger, "playground.sitemap_refresh_refused", exc, expected=True, status=status,
        )
        if self.diagnostics is None:
            return
        try:
            self.diagnostics.record_failure(
                "playground", error=str(exc)[:2048], http_status=status, retry_after=retry_after,
                background=True,
            )
        except ValueError:
            pass

    def stop_provider_background_work(self, provider: str) -> None:
        """Drop the background work a source that was just switched off owns.

        The switch is checked before starting anything and again before writing
        anything down, and neither of those stops a request that is already out:
        a user who switches a source off is entitled to have this device stop
        talking to it, not to have it stop once the current answer arrives.
        """
        if provider == "playground" and self._playground_index_task is not None:
            if not self._playground_index_task.done():
                self._playground_index_task.cancel()
                log_activity(self.logger, "info", "playground.sitemap_refresh_cancelled")
        if provider == "fearless" and self._fearless_index_task is not None:
            if not self._fearless_index_task.done():
                self._fearless_index_task.cancel()
                log_activity(self.logger, "info", "fearless.index_cancelled")

    def _load_sitemap_cache(self) -> dict[str, object]:
        try:
            raw = load_json(self.cache_path.with_name("playground-sitemap.json"), {}, max_bytes=MAX_SITEMAP_CACHE_BYTES)
            return raw if isinstance(raw, dict) and raw.get("schema") == 1 else {}
        except (OSError, ValueError):
            return {}

    def _save_vgtimes_slug_cache(self, slugs: tuple[str, ...]) -> None:
        atomic_write_json(
            self.cache_path.with_name("vgtimes-games.json"),
            {"schema": 1, "saved": time.time(), "slugs": list(slugs)},
            max_bytes=MAX_VGTIMES_SITEMAP_CACHE_BYTES,
        )

    def _load_vgtimes_slug_cache(self) -> tuple[str, ...] | None:
        """The cached game slugs, or nothing when they cannot be trusted.

        A slug reaches a URL, so a cache entry that is not one is not repaired:
        the whole index is dropped and rebuilt from the sitemap rather than
        being read as if the bad entry were the only thing wrong with it.
        """
        try:
            raw = load_json(
                self.cache_path.with_name("vgtimes-games.json"),
                None,
                max_bytes=MAX_VGTIMES_SITEMAP_CACHE_BYTES,
            )
            saved_raw = raw.get("saved") if isinstance(raw, dict) else None
            if isinstance(saved_raw, bool) or not isinstance(saved_raw, (int, float)):
                return None
            saved = float(saved_raw)
            age = time.time() - saved
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != 1
                or not math.isfinite(saved)
                or age < 0
                or age > CACHE_TTL_SECONDS
            ):
                return None
            slugs = raw.get("slugs")
            if not isinstance(slugs, list) or not slugs or len(slugs) > MAX_VGTIMES_SLUGS:
                return None
            for slug in slugs:
                if not isinstance(slug, str) or not VGTIMES_SLUG_RE.fullmatch(slug):
                    return None
            return tuple(dict.fromkeys(slugs))
        except (OSError, ValueError, TypeError):
            return None

    def _save_thecheatscript_sitemap_cache(
        self, refs: tuple[TheCheatScriptPostRef, ...]
    ) -> None:
        atomic_write_json(
            self.cache_path.with_name("thecheatscript-sitemap.json"),
            {
                "schema": 1,
                "saved": time.time(),
                "entries": [[ref.url, ref.lastmod] for ref in refs],
            },
            max_bytes=MAX_THECHEATSCRIPT_SITEMAP_CACHE_BYTES,
        )

    def _load_thecheatscript_sitemap_cache(
        self,
    ) -> tuple[TheCheatScriptPostRef, ...] | None:
        try:
            raw = load_json(
                self.cache_path.with_name("thecheatscript-sitemap.json"),
                None,
                max_bytes=MAX_THECHEATSCRIPT_SITEMAP_CACHE_BYTES,
            )
            saved_raw = raw.get("saved") if isinstance(raw, dict) else None
            if (
                isinstance(saved_raw, bool)
                or not isinstance(saved_raw, (int, float))
            ):
                return None
            saved = float(saved_raw)
            age = time.time() - saved
            if (
                not isinstance(raw, dict)
                or raw.get("schema") != 1
                or not math.isfinite(saved)
                or age < 0
                or age > CACHE_TTL_SECONDS
            ):
                return None
            entries = raw.get("entries")
            if not isinstance(entries, list) or not entries or len(entries) > THECHEATSCRIPT_MAX_SITEMAP_ENTRIES:
                return None
            refs: list[TheCheatScriptPostRef] = []
            seen: set[str] = set()
            for item in entries:
                if not isinstance(item, list) or len(item) != 2:
                    return None
                url, lastmod = item
                if not isinstance(url, str) or not is_thecheatscript_post_url(url) or url in seen:
                    return None
                if lastmod is not None and (not isinstance(lastmod, str) or len(lastmod.encode("utf-8")) > 128):
                    return None
                seen.add(url)
                refs.append(TheCheatScriptPostRef(url, thecheatscript_title_from_url(url), lastmod))
            return tuple(refs)
        except (OSError, ValueError, TypeError):
            return None
