from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import math
from pathlib import PurePosixPath
import re
import time
import threading
from types import MappingProxyType
import unicodedata
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from .atomic import atomic_write_json, load_json
from .text import utf8_len

_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")
# What a table is actually published as. `.rar` is here because the sources
# publish in it and not as a preference: one source served a game's only table
# as `.rar` and the row was dropped before the user ever saw it, and on another
# search five of that source's six rows went the same way. It is opened through
# the same 7-Zip route as `.7z`, which not every 7-Zip build can do, so a host
# whose 7-Zip cannot read it drops those rows at the search rather than after a
# download.
#
# Public, and the only copy: every adapter filters its own listing by suffix
# before a row is ever built, and each of those filters was a set of its own.
# Adding `.rar` here left five of them behind, so the sources that publish in it
# went on dropping those rows at the parser and the format was downloadable from
# exactly one source. A filter that disagrees with what a row may carry is a
# provider that silently has less than it has.
ARTIFACT_SUFFIXES = frozenset({".ct", ".zip", ".7z", ".7zip", ".rar"})
# `target_gate` below, and in `_ALLOWED_NETWORK_STATES` and the diagnostics
# states further down, is a historical spelling that is deliberately kept. It
# names no target gate and reveals nothing: as a download mode and as a network
# state it is an accepted value no provider and no adapter emits, and in the
# diagnostics it is the initial state meaning this source has never been asked
# for anything, which is replaced by `ready`, `error` or `cooldown` on the first
# request. It is not campaign material and the public-release cleanup does not
# remove it. Renaming it would rewrite a value already persisted in each
# device's provider diagnostics, so it needs a compatible read for the old
# spelling rather than a search and replace, and nothing user-visible would
# change: no screen renders any of the three.
_ALLOWED_DOWNLOAD_MODES = {
    "direct_https",
    "source_handoff",
    "system_browser_downloads",
    "file_picker",
    "cef_auto_anon_wait",
    "cef_authenticated",
    "target_gate",
}
# The largest artifact any download route will transfer. It lives here because
# it is a property of the product rather than of the downloader: a row whose
# exact size is already known to be above it can never be acquired, so offering
# it costs the user a press and a wait to be told what was knowable at search
# time.
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_TEXT_BYTES = 4096
_MAX_RESULTS_PER_PROVIDER = 500
_MAX_PROVIDERS = 32
_MAX_DIAGNOSTIC_PROVIDERS = 64
_MAX_ERROR_BYTES = 2048
_MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60
_MAX_COUNTER = 0x7FFFFFFFFFFFFFFF
_MAX_SIZE = 0x7FFFFFFFFFFFFFFF
_ADAPTER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
# On `target_gate` here, see the note above `_ALLOWED_DOWNLOAD_MODES`.
_ALLOWED_NETWORK_STATES = {"target_gate", "ready", "disabled"}
# How a provider is reached. "search" means a table search queries it and
# expects rows back from it. "linked_source" means it is only ever resolved
# as the exact source another provider's page points at, so it can serve a
# table without a search of its own ever running against it.
_ALLOWED_DISCOVERY = {"search", "linked_source"}


@dataclass(frozen=True)
class ProviderDefinition:
    provider: str
    provider_display_name: str
    priority: int
    enabled_by_default: bool
    adapter_kind: str
    network_state: str = "target_gate"
    # Host policy belongs to the provider, not to a separate map that has to be
    # kept in step with this list. It stays backend-only and never reaches
    # `as_dict`, because a catalog result exposes an opaque artifact ID and the
    # routes behind it are not the frontend's to know.
    hosts: frozenset[str] = frozenset()
    # Where the provider lives. Declared once here because the alternative is
    # what this replaced: the same origin retyped at every call site that builds
    # a route, so a provider had no single place that said where it is.
    base_url: str = ""
    # Whether a table search runs this provider. It is declared here because the
    # registry is what the panel and the search plan are built from: GitHub was
    # registered as enabled and ready while no search job existed for it, so the
    # plugin offered a source that could never return a row.
    discovery: str = "search"
    # Whether another provider's page can name an exact page on this one and
    # have it read. `discovery` cannot answer this: GitHub is searched and is
    # also a linked target, while The Cheat Script and VGTimes are searched and
    # are named by nobody. Declared here because the screen that offers to
    # switch a source off has to say what switching it off actually stops, and
    # it was describing all five sources as if they were reached both ways.
    linked_target: bool = False
    # Whether a download from this provider begins with a wait it serves out
    # itself, before any byte moves: a countdown its own client contract
    # enforces, or a one-at-a-time rule that queues the second download behind
    # the first. A provider that says nothing here downloads immediately and
    # needs no wait of its own.
    #
    # It is a property of the source rather than a decision about a screen. It
    # once chose which downloads got a window of their own, which is no longer a
    # choice anyone makes: every download has one. What it still records is
    # which sources cost the user a wait before anything is transferred, which
    # is a real difference between them and belongs where a source is declared.
    serves_download_wait: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not _PROVIDER_RE.fullmatch(self.provider):
            raise ValueError("provider ID is invalid")
        if not isinstance(self.provider_display_name, str) or not self.provider_display_name.strip():
            raise ValueError("provider display name must not be blank")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F or unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in self.provider_display_name):
            raise ValueError("provider display name contains unsafe control characters")
        if utf8_len(self.provider_display_name, "provider display name") > _MAX_TEXT_BYTES:
            raise ValueError("provider display name is too long")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not (-10_000 <= self.priority <= 10_000):
            raise ValueError("provider priority is invalid")
        if not isinstance(self.enabled_by_default, bool):
            raise ValueError("provider enabled_by_default must be boolean")
        if not isinstance(self.adapter_kind, str) or not _ADAPTER_RE.fullmatch(self.adapter_kind):
            raise ValueError("provider adapter kind is invalid")
        if self.network_state not in _ALLOWED_NETWORK_STATES:
            raise ValueError("provider network state is invalid")
        if self.discovery not in _ALLOWED_DISCOVERY:
            raise ValueError("provider discovery is invalid")
        if not isinstance(self.linked_target, bool):
            raise ValueError("provider linked_target must be boolean")
        if not isinstance(self.serves_download_wait, bool):
            raise ValueError("provider serves_download_wait must be boolean")
        if self.discovery == "linked_source" and not self.linked_target:
            # Reached by no search and by no link is reached by nothing at all.
            raise ValueError("a linked-source provider must be a linked target")
        if not isinstance(self.hosts, frozenset) or not all(
            isinstance(host, str) and _HOST_RE.fullmatch(host) for host in self.hosts
        ):
            raise ValueError("provider host policy is invalid")
        if self.base_url:
            parts = urlsplit(self.base_url)
            if parts.scheme != "https" or not parts.path.endswith("/") or parts.query or parts.fragment:
                raise ValueError("provider base URL must be an HTTPS URL ending in a path separator")
            if (parts.hostname or "").casefold() not in self.hosts:
                raise ValueError("provider base URL is outside the provider's own host policy")

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("hosts", None)
        return value


DEFAULT_PROVIDER_DEFINITIONS: tuple[ProviderDefinition, ...] = (
    ProviderDefinition(
        "fearless", "FearLess Cheat Engine", 100, True, "phpbb", "ready",
        frozenset({"fearlessrevolution.com", "www.fearlessrevolution.com"}),
        base_url="https://fearlessrevolution.com/",
        linked_target=True,
    ),
    # Its own client contract makes a guest sit through a countdown before it
    # will hand over a link, so a download here is a wait with a Cancel on it
    # long before it is a transfer.
    ProviderDefinition(
        "playground", "Playground", 85, True, "catalog", "ready",
        frozenset({"playground.ru", "www.playground.ru"}),
        base_url="https://www.playground.ru/",
        serves_download_wait=True,
    ),
    # Searched through the repository index and also resolved as the exact
    # source another catalog's page names. Both routes end in the same place: a
    # repository's release assets, or a table committed in its tree.
    ProviderDefinition(
        "github", "GitHub", 80, True, "github_releases", "ready",
        frozenset({
            "github.com", "api.github.com",
            "objects.githubusercontent.com", "release-assets.githubusercontent.com",
            "raw.githubusercontent.com",
        }),
        base_url="https://github.com/",
        linked_target=True,
    ),
    ProviderDefinition(
        "thecheatscript", "The Cheat Script", 75, True, "sitemap", "ready",
        frozenset({
            "thecheatscript.com", "www.thecheatscript.com",
            "cloud-api.yandex.net", "downloader.disk.yandex.ru",
        }),
        base_url="https://www.thecheatscript.com/",
    ),
    # The file servers are declared by the item page rather than frozen here,
    # but they still have to be under the provider's own domains, and the two
    # named here are the ones this adapter reaches on its own: the site itself
    # and the challenge endpoint every acquisition goes through.
    # The same wait twice over: the module the item page loads declares a
    # countdown the server enforces, and the site serves a guest one download at
    # a time, so a second acquisition queues behind the first.
    ProviderDefinition(
        "vgtimes", "VGTimes", 70, True, "sitemap", "ready",
        frozenset({"vgtimes.ru", "www.vgtimes.ru", "files.vgtimes.ru"}),
        base_url="https://vgtimes.ru/",
        serves_download_wait=True,
    ),
    ProviderDefinition("cheatenginenet", "CheatEngine.net", 40, False, "phpbb", "disabled"),
)

# Read-only on purpose. Host policy is derived from this registry, so a mutable
# mapping would let a caller change a provider's identity while the allowances
# already computed from it stayed behind, which is the divergence the registry
# exists to remove.
PROVIDER_REGISTRY: Mapping[str, ProviderDefinition] = MappingProxyType({
    definition.provider: definition for definition in DEFAULT_PROVIDER_DEFINITIONS
})


def selectable_providers() -> tuple[ProviderDefinition, ...]:
    """Every provider the user is allowed to switch on or off.

    Wider than `searchable_providers()` on purpose: a source reached only as the
    exact page another catalog links is still a source this user may not want
    contacted, and GitHub is reached both ways. Narrower than the whole registry,
    because a provider this build never uses at all is not a choice to offer.
    """
    return tuple(
        definition for definition in DEFAULT_PROVIDER_DEFINITIONS
        if definition.enabled_by_default and definition.network_state != "disabled"
    )


def normalized_provider_id(value: Any) -> str:
    """The one provider-ID grammar, for the modules that store one.

    Public because the durable source selection records provider IDs of its own
    and must accept exactly what the registry accepts. A second copy of this
    regular expression somewhere else is how the two come to disagree.
    """
    return _provider_id(value)


def searchable_providers() -> tuple[ProviderDefinition, ...]:
    """The providers a table search actually queries, in registry order.

    Search jobs, the per source result summary a search returns and the query
    plan the panel shows are all built from this one answer. They used to be
    three hard-coded lists, so a provider could be registered as enabled and
    ready, be counted as a capability, and still never be searched.
    """
    return tuple(
        definition for definition in DEFAULT_PROVIDER_DEFINITIONS
        if definition.enabled_by_default
        and definition.discovery == "search"
        and definition.network_state != "disabled"
    )


def provider_definition(provider_id: str) -> ProviderDefinition:
    """The one place a provider's identity is read from.

    Raising for an unknown ID is deliberate: a provider that is searched but not
    declared would otherwise acquire a blank display name and rank zero, and be
    ranked as if it were the worst source rather than an unregistered one.
    """
    definition = PROVIDER_REGISTRY.get(_provider_id(provider_id))
    if definition is None:
        raise KeyError(f"provider is not registered: {provider_id}")
    return definition


@dataclass(frozen=True)
class CatalogResult:
    provider: str
    provider_display_name: str
    topic_id: str
    artifact_id: str
    table_title: str
    filename: str
    version: str | None
    size_bytes: int | None
    source_page: str
    download_mode: str
    match_score: float
    provider_rank: int = 0
    author: str | None = None
    posted_at: str | None = None
    download_count: int | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        _provider_id(self.provider)
        _bounded_text(self.provider_display_name, "provider display name", required=True)
        _bounded_text(self.topic_id, "topic ID", required=True)
        _bounded_text(self.artifact_id, "artifact ID", required=True)
        _bounded_text(self.table_title, "table title", required=True)
        _artifact_filename(self.filename)
        if self.version is not None:
            _bounded_text(self.version, "version")
        if self.size_bytes is not None and (isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0 or self.size_bytes > _MAX_SIZE):
            raise ValueError("provider artifact size must be a bounded non-negative integer or null")
        _https_page(self.source_page)
        if self.download_mode not in _ALLOWED_DOWNLOAD_MODES:
            raise ValueError("unsupported provider download mode")
        if isinstance(self.match_score, bool) or not isinstance(self.match_score, (int, float)) or not math.isfinite(float(self.match_score)) or not (0.0 <= float(self.match_score) <= 1.0):
            raise ValueError("provider match score must be finite and between 0 and 1")
        if isinstance(self.provider_rank, bool) or not isinstance(self.provider_rank, int) or not (-10_000 <= self.provider_rank <= 10_000):
            raise ValueError("provider rank is invalid")
        if self.author is not None:
            _bounded_text(self.author, "author")
        if self.posted_at is not None:
            _iso_datetime(self.posted_at)
        if self.download_count is not None and (isinstance(self.download_count, bool) or not isinstance(self.download_count, int) or self.download_count < 0 or self.download_count > _MAX_COUNTER):
            raise ValueError("provider download count must be a bounded non-negative integer or null")
        if self.notes is not None:
            _bounded_text(self.notes, "notes")

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class SearchProvider(Protocol):
    provider_id: str
    provider_display_name: str
    provider_rank: int

    async def search(self, query: str) -> Sequence[CatalogResult]: ...


@dataclass(frozen=True)
class ProviderSearchFailure:
    provider: str
    error: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderSearchSummary:
    """One searched source and what it actually contributed.

    A provider that answers successfully with no results produces neither a
    result nor a failure, so without this roster it disappears from the UI and
    becomes indistinguishable from a provider that was never searched.
    """

    provider: str
    provider_display_name: str
    results: int
    status: str
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SearchOutcome:
    results: tuple[CatalogResult, ...]
    failures: tuple[ProviderSearchFailure, ...]
    sources: tuple[ProviderSearchSummary, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "results": [row.as_dict() for row in self.results],
            "failures": [failure.as_dict() for failure in self.failures],
            "sources": [source.as_dict() for source in self.sources],
        }


async def search_all(
    query: str,
    providers: Sequence[SearchProvider],
    *,
    timeout_s: float = 12.0,
    diagnostics: "ProviderDiagnosticsStore | None" = None,
) -> SearchOutcome:
    query = _search_query(query)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not (0.1 <= float(timeout_s) <= 120.0):
        raise ValueError("provider search timeout must be between 0.1 and 120 seconds")
    if not isinstance(providers, Sequence) or len(providers) > _MAX_PROVIDERS:
        raise ValueError("provider search contains too many providers")

    seen_provider_ids: set[str] = set()
    display_names: dict[str, str] = {}
    counted: dict[str, int] = {}
    normalized: list[SearchProvider] = []
    failures: list[ProviderSearchFailure] = []
    now = time.time()
    for provider in providers:
        provider_id = _provider_id(getattr(provider, "provider_id", None))
        if provider_id in seen_provider_ids:
            raise ValueError(f"duplicate provider ID: {provider_id}")
        seen_provider_ids.add(provider_id)
        _bounded_text(getattr(provider, "provider_display_name", None), "provider display name", required=True)
        rank = getattr(provider, "provider_rank", None)
        if isinstance(rank, bool) or not isinstance(rank, int) or not (-10_000 <= rank <= 10_000):
            raise ValueError(f"provider {provider_id} rank is invalid")
        display_names[provider_id] = str(provider.provider_display_name)
        if diagnostics is not None and not diagnostics.can_attempt(provider_id, now=now):
            failures.append(ProviderSearchFailure(provider_id, "provider is in cooldown"))
            continue
        normalized.append(provider)

    async def run_one(provider: SearchProvider) -> tuple[str, tuple[CatalogResult, ...], str | None, float]:
        provider_id = provider.provider_id
        start = time.monotonic()
        try:
            rows = await asyncio.wait_for(provider.search(query), timeout=float(timeout_s))
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
                raise ValueError("provider search must return a sequence")
            if len(rows) > _MAX_RESULTS_PER_PROVIDER:
                raise ValueError("provider returned too many results")
            checked: list[CatalogResult] = []
            for row in rows:
                if not isinstance(row, CatalogResult):
                    raise ValueError("provider result must be CatalogResult")
                if row.provider != provider_id or row.provider_display_name != provider.provider_display_name:
                    raise ValueError("provider result identity mismatch")
                if row.provider_rank != provider.provider_rank:
                    raise ValueError("provider result rank mismatch")
                checked.append(row)
            return provider_id, tuple(checked), None, (time.monotonic() - start) * 1000
        except asyncio.TimeoutError:
            return provider_id, (), "provider search timed out", (time.monotonic() - start) * 1000
        except Exception as exc:
            return provider_id, (), _safe_error(exc), (time.monotonic() - start) * 1000

    batches = await asyncio.gather(*(run_one(provider) for provider in normalized))
    rows: list[CatalogResult] = []
    for provider_id, provider_rows, error, latency_ms in batches:
        if error is None:
            try:
                deduped = _dedupe_provider_rows(provider_rows)
            except Exception as exc:
                error = _safe_error(exc)
        if error is not None:
            failures.append(ProviderSearchFailure(provider_id, error))
            if diagnostics is not None:
                diagnostics.record_failure(provider_id, error=error, latency_ms=latency_ms)
            continue
        rows.extend(deduped)
        counted[provider_id] = counted.get(provider_id, 0) + len(deduped)
        if diagnostics is not None:
            diagnostics.record_search_success(provider_id, results=len(deduped), latency_ms=latency_ms)

    errors = {failure.provider: failure.error for failure in failures}
    sources = tuple(
        ProviderSearchSummary(
            provider=provider_id,
            provider_display_name=display_names.get(provider_id, provider_id),
            results=counted.get(provider_id, 0),
            status="unavailable" if provider_id in errors else "ok",
            error=errors.get(provider_id),
        )
        for provider_id in sorted(display_names)
    )
    return SearchOutcome(
        tuple(sort_results(rows)),
        tuple(sorted(failures, key=lambda item: item.provider)),
        sources,
    )


# Match scores for one game cluster tightly, so comparing them at any finer
# resolution than what the score means lets hundredths of a point outrank a
# table that is years newer. The score is not a pure title measurement either:
# `match_breakdown` shifts it by the source's trust, so two providers that
# recognised the same game equally plainly still differ in the third decimal.
# Splitting on an even scale turned that noise into rank: one live search put a
# five-year-old table at 0.932 above every table of this year at 0.906.
#
# These are the thresholds `decide_match` already reads the same numbers by:
# confident enough to accept without asking, plausible enough to offer, and
# weaker than that. Inside one band the tables are equally plausible matches for
# the game, so recency decides.
CONFIDENT_MATCH_SCORE = 0.88
PLAUSIBLE_MATCH_SCORE = 0.70


def relevance_band(score: float) -> int:
    """How plausible this row is as a match, coarsely enough to be meaningful."""
    value = float(score)
    if value >= CONFIDENT_MATCH_SCORE:
        return 2
    if value >= PLAUSIBLE_MATCH_SCORE:
        return 1
    return 0


def sort_results(rows: Sequence[CatalogResult]) -> list[CatalogResult]:
    # Relevance band first, then recency: for a cheat table, newer is what the
    # user is actually looking for. Provider priority and download count only
    # break ties between equally recent results.
    return sorted(
        rows,
        key=lambda row: (
            -relevance_band(row.match_score),
            -_timestamp(row.posted_at),
            -row.provider_rank,
            -(row.download_count or 0),
            row.table_title.casefold(),
            row.filename.casefold(),
            row.provider,
            row.artifact_id,
        ),
    )


def parse_retry_after(value: str | None, *, now: float | None = None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Retry-After must be a string or null")
    raw = value.strip()
    if not raw or len(raw) > 128:
        return None
    if raw.isdigit():
        return min(int(raw), _MAX_RETRY_AFTER_SECONDS)
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    base = _epoch(time.time() if now is None else now, "Retry-After base time")
    seconds = max(0, int(parsed.timestamp() - base + 0.999999))
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


class ProviderDiagnosticsStore:
    SCHEMA = 1
    COUNTER_KEYS = (
        "searches",
        "results",
        "downloads_succeeded",
        "downloads_failed",
        "bytes_downloaded",
        # A rate limit an artifact download waited out and then recovered from.
        # It is not a failed download and must never be counted as one, but a
        # provider that throttles every transfer is invisible otherwise: the
        # user waits and the diagnostics say the download simply succeeded.
        "downloads_throttled",
        "errors",
        # Rows this provider produced because another provider's page named an
        # exact page on it. Counted apart from `searches` on purpose: no search
        # of this provider ran, and adding one would say it had been asked
        # something it was never asked. Without it a source reached only this
        # way reads as "0 searches" beside a result count nothing explains.
        "linked_reads",
        # How much of what a provider published could not be read. `degraded`
        # lost description or a single row while tables stayed obtainable;
        # `failed` is a page that could not be read at all, which is what a
        # source rebuilding its markup looks like from here.
        "parse_degraded",
        "parse_failed",
    )

    def __init__(self, path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _snapshot_unlocked(self) -> dict[str, object]:
        raw = self._load()
        now = time.time()
        providers = {}
        for provider_id, state in sorted(raw["providers"].items()):
            item = dict(state)
            item["state"] = "cooldown" if float(item["cooldown_until_epoch_s"]) > now else item["state"]
            # Keep retired rows as support history without presenting them as a
            # provider the current registry can still search or download from.
            item["retired"] = provider_id not in PROVIDER_REGISTRY
            providers[provider_id] = item
        return {"schema": self.SCHEMA, "providers": providers}

    def _can_attempt_unlocked(self, provider_id: str, *, now: float | None = None) -> bool:
        provider_id = _provider_id(provider_id)
        current = _epoch(time.time() if now is None else now, "provider current time")
        state = self._load()["providers"].get(provider_id)
        if state is None:
            return True
        return float(state["cooldown_until_epoch_s"]) <= current

    def _record_search_success_unlocked(
        self,
        provider_id: str,
        *,
        results: int,
        latency_ms: float,
        http_status: int | None = None,
        started_at: float | None = None,
    ) -> None:
        if isinstance(results, bool) or not isinstance(results, int) or results < 0:
            raise ValueError("provider result counter is invalid")
        state, root = self._state_for_update(provider_id)
        previous_status = state["last_http_status"]
        state["state"] = "ready"
        state["counters"]["searches"] = _checked_add(state["counters"]["searches"], 1)
        state["counters"]["results"] = _checked_add(state["counters"]["results"], results)
        state["last_latency_ms"] = _latency(latency_ms)
        state["last_http_status"] = _http_status(http_status)
        # A refusal this search did not see outlives it. One search makes
        # several requests, and a provider can refuse one of them while
        # answering the rest: a site index refused while the game's own pages
        # were served is exactly that, and the search then answered from the
        # index this device already had.
        #
        # Two ways that shows up, and both are the same statement. A deadline
        # the provider asked for has its own end written on it, so nothing here
        # needs to clear it early. A refusal carrying no deadline, which is what
        # a challenge is, has only when it happened: recorded by background
        # maintenance after this search began, it is newer than anything this
        # search learned, and clearing it would lose the one record of it.
        #
        # The provider is still `ready`, because it did answer this search. What
        # is kept is what it said about the request it refused.
        now = time.time()
        # The caller's own start where it has one. The fallback is only for a
        # caller that measured nothing else, and it is the weaker answer: it
        # reads as the moment this call was made minus how long the work took,
        # which is the same thing only when nothing delayed the call.
        began = float(started_at) if started_at is not None else now - max(0.0, float(latency_ms)) / 1000.0
        refused_at = state.get("background_refusal_at")
        newer_refusal = refused_at is not None and float(refused_at) >= began
        if float(state["cooldown_until_epoch_s"]) > now or newer_refusal:
            # Whatever the refusal wrote stays, including the status it carried.
            state["last_http_status"] = _http_status(previous_status)
        else:
            state["last_error"] = None
            state["cooldown_until_epoch_s"] = 0.0
            state["background_refusal_at"] = None
        self._save(root)

    def _record_failure_unlocked(
        self,
        provider_id: str,
        *,
        error: str,
        latency_ms: float | None = None,
        http_status: int | None = None,
        retry_after: str | None = None,
        now: float | None = None,
        download: bool = False,
        preserve_cooldown: bool = False,
        background: bool = False,
    ) -> None:
        error = _bounded_text(error, "provider error", required=True, max_bytes=_MAX_ERROR_BYTES)
        state, root = self._state_for_update(provider_id)
        existing_cooldown = float(state["cooldown_until_epoch_s"])
        state["state"] = "error"
        state["counters"]["errors"] = _checked_add(state["counters"]["errors"], 1)
        if download:
            state["counters"]["downloads_failed"] = _checked_add(state["counters"]["downloads_failed"], 1)
        elif not background:
            state["counters"]["searches"] = _checked_add(state["counters"]["searches"], 1)
        # `background` is work this device does for itself, keeping an index
        # current. What the provider said about it is the provider's own state
        # and is recorded like any other, but no search of it happened: the
        # screen names that counter "searches", and counting one here made a
        # single search the user pressed for appear as two.
        state["last_error"] = error
        state["last_latency_ms"] = None if latency_ms is None else _latency(latency_ms)
        state["last_http_status"] = _http_status(http_status)
        retry_seconds = parse_retry_after(retry_after, now=now)
        if retry_seconds is None and http_status in {429, 503}:
            retry_seconds = 60
        base = _epoch(time.time() if now is None else now, "provider current time")
        if background:
            # When this happened, which is the whole of what a refusal carrying
            # no deadline has. A search that succeeds afterwards is not evidence
            # about the request this one refused.
            state["background_refusal_at"] = base
        if retry_seconds:
            state["cooldown_until_epoch_s"] = base + retry_seconds
        elif not preserve_cooldown:
            state["cooldown_until_epoch_s"] = 0.0
        else:
            # A failure that names no deadline has none to write, and writing a
            # cleared one would erase a deadline the provider did ask for. Only
            # a caller that knows its failure is not the provider's whole story
            # asks for this.
            state["cooldown_until_epoch_s"] = existing_cooldown
        self._save(root)

    def _record_linked_failure_unlocked(
        self, provider_id: str, *, error: str, http_status: int | None = None,
    ) -> None:
        """An optional read of this provider, made because another one linked it.

        Attributed to the provider that answered, and deliberately narrower than
        a search failure. It does not touch `searches`, because no search of
        this provider happened and inflating that count would misdescribe what
        the provider was asked to do. It does not set `state` either: a
        repository this provider could not serve for somebody else's page is not
        evidence that its own search is broken. And it never writes
        `cooldown_until_epoch_s`, because a failure carrying no deadline writes
        a cleared one, which is how a cooldown came to be erased by the very
        thing that observed it.
        """
        error = _bounded_text(error, "provider error", required=True, max_bytes=_MAX_ERROR_BYTES)
        state, root = self._state_for_update(provider_id)
        state["counters"]["errors"] = _checked_add(state["counters"]["errors"], 1)
        state["last_error"] = error
        state["last_http_status"] = _http_status(http_status)
        self._save(root)

    def _record_linked_success_unlocked(self, provider_id: str, *, results: int) -> None:
        """Rows this provider served for a page another provider linked.

        The counterpart of `_record_linked_failure_unlocked`, and deliberately
        as narrow. Attributed to the provider that answered, because the rows
        carry its identity, are downloaded from it and are refused when it is
        switched off - and crediting them to the provider that linked them made
        one Playground row plus two GitHub rows read as three Playground
        results and nothing at all from GitHub, on the one screen that exists to
        say what each source is doing.

        It does not touch `searches`, `state` or `cooldown_until_epoch_s`: no
        search of this provider ran, one page answering for somebody else is not
        evidence that its own search works, and a success carrying no deadline
        must not erase one the provider did ask for.
        """
        if isinstance(results, bool) or not isinstance(results, int) or results < 0:
            raise ValueError("provider result counter is invalid")
        if not results:
            return
        state, root = self._state_for_update(provider_id)
        state["counters"]["results"] = _checked_add(state["counters"]["results"], results)
        state["counters"]["linked_reads"] = _checked_add(state["counters"]["linked_reads"], 1)
        self._save(root)

    def _record_parse_issues_unlocked(self, provider_id: str, *, degraded: int, failed: int) -> None:
        for name, value in (("degraded", degraded), ("failed", failed)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_COUNTER:
                raise ValueError(f"provider parse {name} counter is invalid")
        if not degraded and not failed:
            return
        state, root = self._state_for_update(provider_id)
        # Reading a provider's page imperfectly is not the provider failing:
        # the state and last error belong to the transport, and a search that
        # still returned tables must not be presented as an error.
        state["counters"]["parse_degraded"] = _checked_add(state["counters"]["parse_degraded"], degraded)
        state["counters"]["parse_failed"] = _checked_add(state["counters"]["parse_failed"], failed)
        self._save(root)

    def _record_throttle_unlocked(self, provider_id: str, *, wait_seconds: int) -> None:
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or wait_seconds < 0 or wait_seconds > _MAX_RETRY_AFTER_SECONDS:
            raise ValueError("provider throttle wait is invalid")
        state, root = self._state_for_update(provider_id)
        # A provider that asked to be tried again later and then served the file
        # is not in error and is not in cooldown: the state, the last error and
        # the persisted cooldown all belong to attempts that ended. Only the
        # counter and the wait this one actually cost are recorded here.
        state["counters"]["downloads_throttled"] = _checked_add(state["counters"]["downloads_throttled"], 1)
        state["last_throttle_wait_s"] = wait_seconds
        self._save(root)

    def _record_download_unlocked(self, provider_id: str, *, bytes_downloaded: int) -> None:
        if isinstance(bytes_downloaded, bool) or not isinstance(bytes_downloaded, int) or bytes_downloaded < 0 or bytes_downloaded > _MAX_COUNTER:
            raise ValueError("downloaded byte count is invalid")
        state, root = self._state_for_update(provider_id)
        state["counters"]["downloads_succeeded"] = _checked_add(state["counters"]["downloads_succeeded"], 1)
        state["counters"]["bytes_downloaded"] = _checked_add(state["counters"]["bytes_downloaded"], bytes_downloaded)
        state["last_error"] = None
        # `state` is the last attempt of any kind, which is how a failed
        # download already writes it: `record_failure(download=True)` sets
        # `error`. Leaving a success to clear only half of that let a provider
        # show a completed download, no last error and "last attempt failed" at
        # the same time. A throttle is still not an attempt that ended, which is
        # why `record_throttle` deliberately writes neither.
        state["state"] = "ready"
        # The status of the last attempt, and the last attempt returned the
        # file. Leaving the 429 or 500 of an earlier one behind described this
        # provider by an attempt that has since been superseded.
        state["last_http_status"] = None
        # `cooldown_until_epoch_s` is deliberately not cleared. It is a deadline
        # this provider asked for and `can_attempt` still honours, and one route
        # serving a file is not the provider withdrawing it: a download somebody
        # is watching is throttled by its own transfer rather than by the
        # crawl's deadline, so a success here is expected during a cooldown and
        # is not evidence that the cooldown is over. The throttle counters and
        # the last wait it cost are history and stay.
        self._save(root)

    def _reset_unlocked(self) -> bool:
        """Replace the whole record with an empty one, readable or not.

        The repair primitive, for the same reason the blocklist and the source
        selection have one. A corrupt file here is not repaired by using the
        plugin: every recording path loads it first, that load raises, and every
        caller swallows the failure precisely so a broken counter cannot fail a
        search. So without this the only account of what each source is doing
        stays broken for good, while the screen that shows it says the record
        rebuilds itself.

        Returns whether anything was replaced, which is unknown for a record
        that could not be read: it is still replaced, and reported as nothing
        removed, because nothing about its contents is known.
        """
        try:
            existing = len(self._load()["providers"])
        except (OSError, ValueError):
            self._save({"schema": self.SCHEMA, "providers": {}})
            return False
        if not existing:
            return False
        self._save({"schema": self.SCHEMA, "providers": {}})
        return True

    def _clear_unlocked(self, provider_id: str) -> bool:
        provider_id = _provider_id(provider_id)
        root = self._load()
        existed = root["providers"].pop(provider_id, None) is not None
        if existed:
            self._save(root)
        return existed

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._snapshot_unlocked()

    def can_attempt(self, provider_id: str, *, now: float | None = None) -> bool:
        with self._lock:
            return self._can_attempt_unlocked(provider_id, now=now)

    def record_search_success(
        self, provider_id: str, *, results: int, latency_ms: float,
        http_status: int | None = None, started_at: float | None = None,
    ) -> None:
        with self._lock:
            self._record_search_success_unlocked(
                provider_id, results=results, latency_ms=latency_ms,
                http_status=http_status, started_at=started_at,
            )

    def record_failure(
        self, provider_id: str, *, error: str, latency_ms: float | None = None,
        http_status: int | None = None, retry_after: str | None = None,
        now: float | None = None, download: bool = False, preserve_cooldown: bool = False,
        background: bool = False,
    ) -> None:
        with self._lock:
            self._record_failure_unlocked(
                provider_id, error=error, latency_ms=latency_ms, http_status=http_status,
                retry_after=retry_after, now=now, download=download,
                preserve_cooldown=preserve_cooldown, background=background,
            )

    def record_linked_failure(
        self, provider_id: str, *, error: str, http_status: int | None = None,
    ) -> None:
        """Record a failed optional read another provider's page pointed at."""
        with self._lock:
            self._record_linked_failure_unlocked(provider_id, error=error, http_status=http_status)

    def record_linked_success(self, provider_id: str, *, results: int) -> None:
        """Record rows this provider served for a page another provider linked."""
        with self._lock:
            self._record_linked_success_unlocked(provider_id, results=results)

    def record_parse_issues(self, provider_id: str, *, degraded: int = 0, failed: int = 0) -> None:
        with self._lock:
            self._record_parse_issues_unlocked(provider_id, degraded=degraded, failed=failed)

    def record_throttle(self, provider_id: str, *, wait_seconds: int) -> None:
        """Record a rate limit that was waited out rather than one that ended an attempt."""
        with self._lock:
            self._record_throttle_unlocked(provider_id, wait_seconds=wait_seconds)

    def record_download(self, provider_id: str, *, bytes_downloaded: int) -> None:
        with self._lock:
            self._record_download_unlocked(provider_id, bytes_downloaded=bytes_downloaded)

    def clear(self, provider_id: str) -> bool:
        with self._lock:
            return self._clear_unlocked(provider_id)

    def reset(self) -> bool:
        """Replace an unusable record with an empty one. See `_reset_unlocked`."""
        with self._lock:
            return self._reset_unlocked()

    def _state_for_update(self, provider_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        provider_id = _provider_id(provider_id)
        root = self._load()
        state = root["providers"].get(provider_id)
        if state is None:
            if len(root["providers"]) >= _MAX_DIAGNOSTIC_PROVIDERS:
                raise ValueError("too many provider diagnostics entries")
            state = _empty_provider_state()
            root["providers"][provider_id] = state
        return state, root

    def _load(self) -> dict[str, Any]:
        raw = load_json(self.path, {"schema": self.SCHEMA, "providers": {}})
        schema = raw.get("schema") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema", "providers"}
            or isinstance(schema, bool)
            or not isinstance(schema, int)
            or schema != self.SCHEMA
        ):
            raise ValueError("provider diagnostics state has an unsupported or corrupt schema")
        providers = raw.get("providers")
        if not isinstance(providers, dict) or len(providers) > _MAX_DIAGNOSTIC_PROVIDERS:
            raise ValueError("provider diagnostics providers object is invalid")
        checked: dict[str, Any] = {}
        for provider_id, state in providers.items():
            provider_id = _provider_id(provider_id)
            checked[provider_id] = _validate_provider_state(state)
        return {"schema": self.SCHEMA, "providers": checked}

    def _save(self, root: dict[str, Any]) -> None:
        atomic_write_json(self.path, root)


def _empty_provider_state() -> dict[str, Any]:
    return {
        "state": "target_gate",
        "counters": {key: 0 for key in ProviderDiagnosticsStore.COUNTER_KEYS},
        "last_http_status": None,
        "last_latency_ms": None,
        "last_error": None,
        "cooldown_until_epoch_s": 0.0,
        "last_throttle_wait_s": None,
        "background_refusal_at": None,
    }


_REQUIRED_STATE_KEYS = frozenset({"state", "counters", "last_http_status", "last_latency_ms", "last_error", "cooldown_until_epoch_s"})
# A field added by a later release is absent from state an earlier one wrote,
# for the same reason a counter is, and refusing the whole file for that would
# throw away every provider's history on upgrade. An unknown key is still a
# corrupt file.
_OPTIONAL_STATE_KEYS = frozenset({"last_throttle_wait_s", "background_refusal_at"})


def _validate_provider_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or not _REQUIRED_STATE_KEYS <= set(state) <= (_REQUIRED_STATE_KEYS | _OPTIONAL_STATE_KEYS):
        raise ValueError("provider diagnostics entry is malformed")
    # `target_gate` is this store's "never asked yet"; see the note above
    # `_ALLOWED_DOWNLOAD_MODES`. It is written to disk, so it stays readable.
    if state["state"] not in {"target_gate", "ready", "error", "cooldown"}:
        raise ValueError("provider diagnostics state is invalid")
    counters = state["counters"]
    # A counter added by a later release is absent from state this one wrote,
    # and refusing the whole file for that would throw away every provider's
    # history on upgrade. Unknown keys are still a corrupt file.
    if not isinstance(counters, dict) or not set(counters) <= set(ProviderDiagnosticsStore.COUNTER_KEYS):
        raise ValueError("provider diagnostics counters are invalid")
    checked_counters = {}
    for key in ProviderDiagnosticsStore.COUNTER_KEYS:
        value = counters.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _MAX_COUNTER:
            raise ValueError("provider diagnostics counter is invalid")
        checked_counters[key] = value
    last_error = state["last_error"]
    if last_error is not None:
        last_error = _bounded_text(last_error, "provider error", max_bytes=_MAX_ERROR_BYTES)
    cooldown = state["cooldown_until_epoch_s"]
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(float(cooldown)) or cooldown < 0 or cooldown > 4_102_444_800:
        raise ValueError("provider cooldown timestamp is invalid")
    refusal_at = state.get("background_refusal_at")
    if refusal_at is not None and (
        isinstance(refusal_at, bool) or not isinstance(refusal_at, (int, float))
        or not math.isfinite(float(refusal_at)) or refusal_at < 0 or refusal_at > 4_102_444_800
    ):
        raise ValueError("provider background refusal timestamp is invalid")
    throttle_wait = state.get("last_throttle_wait_s")
    if throttle_wait is not None and (
        isinstance(throttle_wait, bool) or not isinstance(throttle_wait, int)
        or throttle_wait < 0 or throttle_wait > _MAX_RETRY_AFTER_SECONDS
    ):
        raise ValueError("provider throttle wait is invalid")
    return {
        "state": state["state"],
        "counters": checked_counters,
        "last_http_status": _http_status(state["last_http_status"]),
        "last_latency_ms": None if state["last_latency_ms"] is None else _latency(state["last_latency_ms"]),
        "last_error": last_error,
        "cooldown_until_epoch_s": float(cooldown),
        "last_throttle_wait_s": throttle_wait,
        "background_refusal_at": None if refusal_at is None else float(refusal_at),
    }


def _dedupe_provider_rows(rows: Sequence[CatalogResult]) -> list[CatalogResult]:
    # Artifact IDs are provider-scoped immutable identities. Duplicate observations
    # may differ in ranking telemetry, but immutable acquisition identity must agree.
    best: dict[str, CatalogResult] = {}
    for row in rows:
        previous = best.get(row.artifact_id)
        if previous is not None and _artifact_identity(previous) != _artifact_identity(row):
            raise ValueError(f"provider reused artifact ID {row.artifact_id} for conflicting metadata")
        if previous is None or _row_score(row) < _row_score(previous):
            best[row.artifact_id] = row
    return list(best.values())


def _artifact_identity(row: CatalogResult) -> tuple[object, ...]:
    return (
        row.provider, row.provider_display_name, row.topic_id, row.artifact_id,
        row.filename, row.version, row.size_bytes, row.source_page, row.download_mode,
    )


def _row_score(row: CatalogResult) -> tuple[float, int, float, int, str, str]:
    return (
        -float(row.match_score),
        -row.provider_rank,
        -_timestamp(row.posted_at),
        -(row.download_count or 0),
        row.table_title.casefold(),
        row.filename.casefold(),
    )


def _provider_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROVIDER_RE.fullmatch(value):
        raise ValueError("provider ID is invalid")
    return value


def _search_query(value: Any) -> str:
    text = _bounded_text(value, "provider query", required=True, max_bytes=1024).strip()
    if not text:
        raise ValueError("provider query must not be blank")
    return text


def _bounded_text(value: Any, field: str, *, required: bool = False, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{field} contains unsafe control characters")
    if any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in value):
        raise ValueError(f"{field} contains bidirectional control characters")
    if required and not value.strip():
        raise ValueError(f"{field} must not be blank")
    if utf8_len(value, field) > max_bytes:
        raise ValueError(f"{field} is too long")
    return value


def _artifact_filename(value: Any) -> str:
    value = _bounded_text(value, "provider filename", required=True)
    path = PurePosixPath(value)
    if path.name != value or value in {".", ".."} or path.suffix.casefold() not in ARTIFACT_SUFFIXES:
        raise ValueError("provider filename must be a basename ending in .CT/.zip/.7z/.7zip/.rar")
    return value


def _https_page(value: Any) -> str:
    value = _bounded_text(value, "provider source page", required=True)
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("provider source page must be an HTTPS URL without userinfo")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider source page has an invalid port") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("provider source page has an invalid port")
    try:
        address = ipaddress.ip_address(parsed.hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("provider source page literal IP must be globally routable")
    host = parsed.hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("provider source page hostname must not be localhost")
    return value


def _iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("provider posted_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("provider posted_at must include a timezone")
    try:
        timestamp = parsed.timestamp()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("provider posted_at is outside the supported range") from exc
    if not math.isfinite(timestamp):
        raise ValueError("provider posted_at is outside the supported range")
    return parsed


def _timestamp(value: str | None) -> float:
    if value is None:
        return 0.0
    return _iso_datetime(value).timestamp()


def _http_status(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 100 or value > 599:
        raise ValueError("provider HTTP status is invalid")
    return value


def _latency(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0 or value > 24 * 60 * 60 * 1000:
        raise ValueError("provider latency is invalid")
    return int(round(float(value)))


def _epoch(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{field} is invalid")
    result = float(value)
    if result < 0 or result > 4_102_444_800:
        raise ValueError(f"{field} is outside the supported range")
    return result


def _checked_add(current: int, amount: int) -> int:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("provider diagnostics counter increment is invalid")
    if current > _MAX_COUNTER - amount:
        raise ValueError("provider diagnostics counter overflow")
    return current + amount

def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    text = text.replace("\r", " ").replace("\n", " ").replace("\x00", " ")
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= _MAX_ERROR_BYTES:
        return text
    return encoded[:_MAX_ERROR_BYTES].decode("utf-8", errors="ignore")
