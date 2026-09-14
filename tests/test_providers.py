from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
import json
from pathlib import Path

import pytest

from ce_decky.providers import (
    CatalogResult,
    DEFAULT_PROVIDER_DEFINITIONS,
    ProviderDefinition,
    ProviderDiagnosticsStore,
    parse_retry_after,
    provider_definition,
    search_all,
    searchable_providers,
    sort_results,
)


def row(provider="a", display="A", artifact="one", score=0.8, rank=10, **kwargs):
    return CatalogResult(
        provider=provider,
        provider_display_name=display,
        topic_id=kwargs.pop("topic_id", "topic"),
        artifact_id=artifact,
        table_title=kwargs.pop("table_title", "Game table"),
        filename=kwargs.pop("filename", "Game.CT"),
        version=kwargs.pop("version", "1.0"),
        size_bytes=kwargs.pop("size_bytes", 123),
        source_page=kwargs.pop("source_page", "https://example.com/topic"),
        download_mode=kwargs.pop("download_mode", "target_gate"),
        match_score=score,
        provider_rank=rank,
        **kwargs,
    )


class Provider:
    def __init__(self, provider_id, display, rank, rows=None, error=None, delay=0):
        self.provider_id = provider_id
        self.provider_display_name = display
        self.provider_rank = rank
        self.rows = rows or []
        self.error = error
        self.delay = delay

    async def search(self, query):
        assert query
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.rows


def test_default_provider_contract_is_unique_and_network_coded():
    ids = [item.provider for item in DEFAULT_PROVIDER_DEFINITIONS]
    assert len(ids) == len(set(ids))
    assert ids[:4] == ["fearless", "playground", "github", "thecheatscript"]
    assert all(item.network_state == "ready" for item in DEFAULT_PROVIDER_DEFINITIONS[:4])
    assert DEFAULT_PROVIDER_DEFINITIONS[-1].network_state == "disabled"
    assert DEFAULT_PROVIDER_DEFINITIONS[-1].enabled_by_default is False


def test_only_the_providers_a_search_runs_are_declared_searchable():
    # Every enabled, ready provider now has a search job behind it, which is the
    # invariant this guards: a provider is declared searchable only where the
    # catalog can actually answer for it. GitHub carries both routes, its own
    # repository search and the exact source another provider's page names.
    assert [item.provider for item in searchable_providers()] == [
        "fearless", "playground", "github", "thecheatscript", "vgtimes",
    ]
    assert provider_definition("github").discovery == "search"
    # A disabled provider stays out whatever it declares.
    assert provider_definition("cheatenginenet") not in searchable_providers()


def test_a_linked_source_provider_stays_out_of_a_search():
    linked = ProviderDefinition(
        "linked", "Linked", 10, True, "phpbb", "ready",
        discovery="linked_source", linked_target=True,
    )
    assert linked.discovery == "linked_source"


def test_a_provider_reached_by_no_search_and_no_link_is_refused_at_declaration():
    # Reached by nothing at all, which is a declaration mistake rather than a
    # provider this build simply does not use.
    with pytest.raises(ValueError):
        ProviderDefinition("linked", "Linked", 10, True, "phpbb", "ready", discovery="linked_source")


def test_being_a_linked_target_is_declared_rather_than_inferred_from_discovery():
    # Only a Playground page names another provider's exact page, and it names
    # two of them. The other three are searched and linked by nobody, so
    # "searched" cannot stand for "also read through a link".
    from ce_decky.providers import PROVIDER_REGISTRY

    targets = {
        definition.provider for definition in PROVIDER_REGISTRY.values()
        if definition.linked_target
    }
    assert targets == {"fearless", "github"}


def test_a_provider_that_serves_its_own_wait_is_declared_rather_than_named_at_the_screen():
    """Which sources make the user wait before a byte moves is declared here.

    A countdown its own client contract enforces, and a one-download-at-a-time
    rule on top of it, is a real difference between sources and a real cost to
    the user, so which sources carry it is declared here rather than recognised
    somewhere else by provider ID.
    """
    from ce_decky.providers import PROVIDER_REGISTRY

    waiting = {
        definition.provider for definition in PROVIDER_REGISTRY.values()
        if definition.serves_download_wait
    }
    assert waiting == {"playground", "vgtimes"}
    with pytest.raises(ValueError):
        ProviderDefinition("x", "X", 10, True, "phpbb", "ready", serves_download_wait="yes")


def test_an_unknown_discovery_is_refused_at_declaration():
    with pytest.raises(ValueError):
        ProviderDefinition("x", "X", 10, True, "phpbb", "ready", discovery="someday")


def test_retired_provider_diagnostics_are_explicit_history(tmp_path):
    path = tmp_path / "providers.json"
    store = ProviderDiagnosticsStore(path)
    store.record_failure("opencheattables", error="HTTP 403 challenge")
    store.record_search_success("fearless", results=1, latency_ms=1)

    providers = ProviderDiagnosticsStore(path).snapshot()["providers"]
    assert providers["opencheattables"]["retired"] is True
    assert providers["fearless"]["retired"] is False


def test_catalog_result_rejects_unsafe_or_ambiguous_metadata():
    with pytest.raises(ValueError, match="filename"):
        row(filename="dir/Game.CT")
    with pytest.raises(ValueError, match="HTTPS"):
        row(source_page="http://example.com/topic")
    with pytest.raises(ValueError, match="globally routable"):
        row(source_page="https://127.0.0.1/topic")
    with pytest.raises(ValueError, match="userinfo"):
        row(source_page="https://user:pass@example.com/topic")
    with pytest.raises(ValueError, match="score"):
        row(score=1.1)
    with pytest.raises(ValueError, match="timezone"):
        row(posted_at="2026-08-15T10:00:00")


def test_sort_results_preserves_cross_provider_provenance_and_ranks_locally():
    rows = [
        row("a", "A", "x", 0.8, 10, download_count=50),
        row("b", "B", "x", 0.8, 20, download_count=1),
        row("a", "A", "y", 0.9, 1),
    ]
    ordered = sort_results(rows)
    assert [(item.provider, item.artifact_id) for item in ordered] == [("a", "y"), ("b", "x"), ("a", "x")]


@pytest.mark.asyncio
async def test_search_all_isolates_failures_timeouts_and_dedupes_within_provider(tmp_path):
    diagnostics = ProviderDiagnosticsStore(tmp_path / "providers.json")
    good = Provider(
        "good",
        "Good",
        10,
        rows=[row("good", "Good", "same", 0.8, 10), row("good", "Good", "same", 0.9, 10), row("good", "Good", "other", 0.7, 10)],
    )
    bad = Provider("bad", "Bad", 9, error=RuntimeError("parser broke\nsecret-ish line"))
    slow = Provider("slow", "Slow", 8, rows=[row("slow", "Slow", "slow", 1.0, 8)], delay=0.2)
    outcome = await search_all("Example Game", [good, bad, slow], timeout_s=0.1, diagnostics=diagnostics)
    assert [(item.provider, item.artifact_id) for item in outcome.results] == [("good", "same"), ("good", "other")]
    assert {failure.provider for failure in outcome.failures} == {"bad", "slow"}
    assert all("\n" not in failure.error for failure in outcome.failures)
    snap = diagnostics.snapshot()["providers"]
    assert snap["good"]["counters"]["searches"] == 1
    assert snap["good"]["counters"]["results"] == 2
    assert snap["bad"]["counters"]["errors"] == 1
    assert snap["slow"]["counters"]["errors"] == 1


@pytest.mark.asyncio
async def test_search_all_rejects_provider_identity_rank_and_duplicate_provider_ids():
    mismatched = Provider("good", "Good", 10, rows=[row("other", "Good", "x", 0.8, 10)])
    outcome = await search_all("Game", [mismatched])
    assert outcome.results == ()
    assert "identity mismatch" in outcome.failures[0].error

    rank_bad = Provider("good", "Good", 10, rows=[row("good", "Good", "x", 0.8, 11)])
    outcome = await search_all("Game", [rank_bad])
    assert "rank mismatch" in outcome.failures[0].error

    with pytest.raises(ValueError, match="duplicate provider ID"):
        await search_all("Game", [Provider("x", "X", 1), Provider("x", "X", 1)])


def test_retry_after_supports_delta_and_http_date_and_bounds():
    now = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc).timestamp()
    assert parse_retry_after("120", now=now) == 120
    assert parse_retry_after(str(10**9), now=now) == 86400
    future = format_datetime(datetime(2026, 8, 15, 10, 2, tzinfo=timezone.utc), usegmt=True)
    assert parse_retry_after(future, now=now) == 120
    assert parse_retry_after("not a date", now=now) is None
    assert parse_retry_after(None, now=now) is None


def test_diagnostics_cooldown_counters_clear_and_persistence(tmp_path):
    path = tmp_path / "providers.json"
    store = ProviderDiagnosticsStore(path)
    store.record_failure("fearless", error="HTTP 429", http_status=429, retry_after="90", now=1000.0)
    assert store.can_attempt("fearless", now=1089.0) is False
    assert store.can_attempt("fearless", now=1090.0) is True
    snap = store.snapshot()["providers"]["fearless"]
    assert snap["last_http_status"] == 429
    assert snap["counters"]["errors"] == 1
    assert snap["counters"]["searches"] == 1

    store.record_search_success("fearless", results=7, latency_ms=12.6, http_status=200)
    store.record_download("fearless", bytes_downloaded=1234)
    snap = ProviderDiagnosticsStore(path).snapshot()["providers"]["fearless"]
    assert snap["counters"] == {
        "searches": 2,
        "results": 7,
        "downloads_succeeded": 1,
        "downloads_failed": 0,
        "bytes_downloaded": 1234,
        "downloads_throttled": 0,
        "errors": 1,
        "linked_reads": 0,
        "parse_degraded": 0,
        "parse_failed": 0,
    }
    assert snap["last_latency_ms"] == 13
    assert snap["last_error"] is None
    assert snap["last_throttle_wait_s"] is None
    assert store.clear("fearless") is True
    assert store.clear("fearless") is False


def test_diagnostics_count_a_recovered_throttle_without_calling_it_a_failure(tmp_path):
    """A 429 an artifact download waited out and recovered from is still worth knowing about.

    Only the terminal-failure path recorded anything, so a provider that makes
    every transfer wait was indistinguishable in Advanced from one that serves
    files immediately. It is not an error and it is not a persisted cooldown:
    counting it as either would put a provider into a state it recovered from.
    """
    path = tmp_path / "providers.json"
    store = ProviderDiagnosticsStore(path)
    store.record_throttle("playground", wait_seconds=30)
    store.record_download("playground", bytes_downloaded=4096)
    snap = ProviderDiagnosticsStore(path).snapshot()["providers"]["playground"]
    assert snap["counters"]["downloads_throttled"] == 1
    assert snap["counters"]["downloads_succeeded"] == 1
    assert snap["counters"]["downloads_failed"] == 0
    assert snap["counters"]["errors"] == 0
    assert snap["last_throttle_wait_s"] == 30
    assert snap["last_error"] is None
    assert snap["cooldown_until_epoch_s"] == 0.0
    assert store.can_attempt("playground") is True
    with pytest.raises(ValueError, match="throttle wait"):
        store.record_throttle("playground", wait_seconds=-1)


def test_diagnostics_keep_provider_history_written_before_the_throttle_field(tmp_path):
    """An upgrade must not throw away every provider's counters for a field it added."""
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"schema": 1, "providers": {"playground": {
        "state": "ready",
        "counters": {"searches": 3, "results": 9, "downloads_succeeded": 1, "downloads_failed": 0, "bytes_downloaded": 12, "errors": 0},
        "last_http_status": 200, "last_latency_ms": 40, "last_error": None, "cooldown_until_epoch_s": 0.0,
    }}}))
    snap = ProviderDiagnosticsStore(path).snapshot()["providers"]["playground"]
    assert snap["counters"]["searches"] == 3
    assert snap["counters"]["downloads_throttled"] == 0
    assert snap["last_throttle_wait_s"] is None

    path.write_text(json.dumps({"schema": 1, "providers": {"playground": {
        "state": "ready", "counters": {}, "last_http_status": None, "last_latency_ms": None,
        "last_error": None, "cooldown_until_epoch_s": 0.0, "invented_field": 1,
    }}}))
    with pytest.raises(ValueError, match="malformed"):
        ProviderDiagnosticsStore(path).snapshot()


def test_diagnostics_rejects_corrupt_and_symlinked_state(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({"schema": 1, "providers": {"Bad ID": {}}}))
    with pytest.raises(ValueError):
        ProviderDiagnosticsStore(path).snapshot()

    real = tmp_path / "real.json"
    real.write_text('{"schema":1,"providers":{}}')
    path.unlink()
    path.symlink_to(real)
    with pytest.raises(ValueError, match="regular file"):
        ProviderDiagnosticsStore(path).snapshot()


def test_provider_metadata_rejects_lone_unicode_surrogate_as_validation_error():
    with pytest.raises(ValueError, match="valid Unicode"):
        row(table_title="bad\ud800")


def test_provider_rejects_control_bidi_invalid_port_and_conflicting_artifact_identity():
    with pytest.raises(ValueError, match="control"):
        row(table_title="bad\tlabel")
    with pytest.raises(ValueError, match="bidirectional"):
        row(table_title="safe\u202eexe")
    with pytest.raises(ValueError, match="invalid port"):
        row(source_page="https://example.com:99999/topic")

    provider = Provider(
        "good", "Good", 10,
        rows=[
            row("good", "Good", "same", .8, 10, filename="one.CT"),
            row("good", "Good", "same", .9, 10, filename="two.CT"),
        ],
    )
    outcome = asyncio.run(search_all("Game", [provider]))
    assert outcome.results == ()
    assert "conflicting metadata" in outcome.failures[0].error


def test_provider_definition_and_catalog_numeric_edges_are_fail_closed():
    from ce_decky.providers import ProviderDefinition
    with pytest.raises(ValueError, match="adapter"):
        ProviderDefinition("x", "X", 1, True, "bad adapter")
    with pytest.raises(ValueError, match="network state"):
        ProviderDefinition("x", "X", 1, True, "catalog", "mystery")
    with pytest.raises(ValueError, match="score"):
        row(score=float("nan"))
    with pytest.raises(ValueError, match="artifact size"):
        row(size_bytes=2**63)
    with pytest.raises(ValueError, match="download count"):
        row(download_count=2**63)


def test_provider_text_and_url_controls_are_rejected():
    with pytest.raises(ValueError, match="unsafe control"):
        row(table_title="safe\tmisleading")
    with pytest.raises(ValueError, match="control"):
        row(table_title="abc\u202edef")
    with pytest.raises(ValueError, match="localhost"):
        row(source_page="https://localhost/topic")


def test_retry_after_and_diagnostics_reject_non_finite_times(tmp_path):
    with pytest.raises(ValueError, match="base time"):
        parse_retry_after("Sat, 15 Aug 2026 10:02:00 GMT", now=float("nan"))
    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    with pytest.raises(ValueError, match="current time"):
        store.can_attempt("fearless", now=float("inf"))
    with pytest.raises(ValueError, match="latency"):
        store.record_search_success("fearless", results=1, latency_ms=float("nan"))


def test_diagnostics_rejects_counter_overflow_before_persist(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text(json.dumps({
        "schema": 1,
        "providers": {"fearless": {
            "state": "ready",
            "counters": {"searches": 2**63 - 1, "results": 0, "downloads_succeeded": 0,
                         "downloads_failed": 0, "bytes_downloaded": 0, "errors": 0},
            "last_http_status": None, "last_latency_ms": None, "last_error": None,
            "cooldown_until_epoch_s": 0.0,
        }},
    }))
    store = ProviderDiagnosticsStore(path)
    with pytest.raises(ValueError, match="overflow"):
        store.record_search_success("fearless", results=0, latency_ms=1)


def test_diagnostics_rejects_non_finite_persisted_cooldown(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text('{"schema":1,"providers":{"fearless":{"state":"ready","counters":{"searches":0,"results":0,"downloads_succeeded":0,"downloads_failed":0,"bytes_downloaded":0,"errors":0},"last_http_status":null,"last_latency_ms":null,"last_error":null,"cooldown_until_epoch_s":NaN}}}')
    with pytest.raises(ValueError):
        ProviderDiagnosticsStore(path).snapshot()


def test_provider_diagnostics_reject_boolean_schema(tmp_path: Path):
    path = tmp_path / "providers.json"
    path.write_text('{"schema":true,"providers":{}}', encoding="utf-8")
    store = ProviderDiagnosticsStore(path)
    with pytest.raises(ValueError, match="unsupported or corrupt schema"):
        store.snapshot()


@pytest.mark.asyncio
async def test_search_outcome_lists_every_searched_source_including_silent_ones():
    """A provider that answers with no results leaves no result and no failure,
    so without an explicit roster it vanishes from the UI and looks exactly like
    a provider that was never searched."""
    outcome = await search_all("Example Game", [
        Provider("playground", "Playground", 85, rows=[row("playground", "Playground", "one", 0.8, 85)]),
        Provider("opencheattables", "Open Cheat Tables", 90, rows=[]),
        Provider("fearless", "FearLess Cheat Engine", 100, error=RuntimeError("HTTP 403 challenge")),
    ])
    assert [(source.provider, source.results, source.status) for source in outcome.sources] == [
        ("fearless", 0, "unavailable"),
        ("opencheattables", 0, "ok"),
        ("playground", 1, "ok"),
    ]
    assert [source.provider_display_name for source in outcome.sources] == [
        "FearLess Cheat Engine", "Open Cheat Tables", "Playground",
    ]
    assert "HTTP 403 challenge" in str(outcome.as_dict()["sources"][0]["error"])


def test_sort_results_prefers_recency_inside_a_relevance_band():
    """Match scores for one game cluster tightly, so an exact comparison let a
    hundredth of a point outrank a table that is a year newer."""
    old_high_rank = row("fearless", "FearLess", "old", 0.87, 100, posted_at="2025-04-28T04:04:14Z")
    fresh_low_rank = row("playground", "Playground", "fresh", 0.86, 85, posted_at="2026-07-30T19:01:36Z")
    ordered = sort_results([old_high_rank, fresh_low_rank])
    assert [item.artifact_id for item in ordered] == ["fresh", "old"]

    # A clearly worse match still cannot jump the band on recency alone.
    unrelated = row("playground", "Playground", "unrelated", 0.55, 85, posted_at="2026-08-01T00:00:00Z")
    assert [item.artifact_id for item in sort_results([old_high_rank, unrelated])] == ["old", "unrelated"]


def test_sort_results_bands_by_what_a_score_means_not_by_an_even_scale():
    """Splitting the score evenly made hundredths of a point a rank of their own.

    Both of these recognised the same game plainly, and the gap between them is
    the source's trust plus title noise. On an even scale they landed in
    different bands and a 2021 table was offered above every 2026 one.
    """
    old = row("github", "GitHub", "old", 0.932, 100, posted_at="2021-05-22T20:44:32Z")
    fresh = row("playground", "Playground", "fresh", 0.906, 85, posted_at="2026-07-03T04:07:45Z")
    assert [item.artifact_id for item in sort_results([old, fresh])] == ["fresh", "old"]

    # A match only plausible enough to offer is still a band below a confident
    # one, however new it is: this is a table for a different game.
    plausible = row("fearless", "FearLess", "plausible", 0.868, 100, posted_at="2026-08-01T00:00:00Z")
    assert [item.artifact_id for item in sort_results([old, plausible])] == ["old", "plausible"]


def test_host_policy_is_declared_on_the_provider_and_never_exposed():
    playground = provider_definition("playground")
    assert playground.hosts == frozenset({"playground.ru", "www.playground.ru"})
    # A catalog result exposes an opaque artifact ID; the routes behind it are
    # backend-only, so the public dictionary must not grow a host list.
    assert "hosts" not in playground.as_dict()
    assert playground.base_url == "https://www.playground.ru/"
    assert set(playground.as_dict()) == {
        "provider", "provider_display_name", "priority",
        "enabled_by_default", "adapter_kind", "network_state", "discovery", "base_url",
        "linked_target", "serves_download_wait",
    }


def test_catalog_host_map_is_derived_from_the_registry():
    from ce_decky.catalog import PROVIDER_HOSTS

    for provider_id, hosts in PROVIDER_HOSTS.items():
        assert hosts == provider_definition(provider_id).hosts
    # A provider that declares no hosts stays absent, so asking for one fails
    # closed rather than resolving to an empty allowance.
    assert "cheatenginenet" not in PROVIDER_HOSTS
    assert provider_definition("cheatenginenet").hosts == frozenset()


def test_a_malformed_host_policy_is_refused_at_declaration():
    for bad in (frozenset({"HOST.example"}), frozenset({""}), frozenset({"-bad.example"}), {"set.example"}):
        with pytest.raises(ValueError):
            ProviderDefinition("x", "X", 10, True, "phpbb", "ready", bad)


def test_the_strict_accessor_refuses_an_unregistered_provider():
    assert provider_definition("fearless").priority == 100
    with pytest.raises(KeyError):
        provider_definition("definitely-not-a-provider")
    with pytest.raises(ValueError):
        provider_definition("Not A Provider")


def test_failure_handling_never_depends_on_the_strict_accessor():
    # A provider's own failure must not become a failed search because its ID is
    # not registered, so the browser-handoff path uses a lookup with a fallback.
    from ce_decky.catalog import PROVIDER_REGISTRY

    assert PROVIDER_REGISTRY.get("definitely-not-registered") is None


def test_the_registry_is_read_only_so_derived_policy_cannot_diverge():
    from ce_decky.providers import PROVIDER_REGISTRY

    with pytest.raises(TypeError):
        PROVIDER_REGISTRY["injected"] = provider_definition("fearless")  # type: ignore[index]
    with pytest.raises(TypeError):
        del PROVIDER_REGISTRY["fearless"]  # type: ignore[attr-defined]


def test_module_annotations_resolve():
    # `from __future__ import annotations` hides an undefined name in a module
    # level annotation until something evaluates it, which is how the registry's
    # own type went unnoticed.
    import typing

    from ce_decky import providers

    assert typing.get_type_hints(providers)["PROVIDER_REGISTRY"]


def test_a_base_url_must_belong_to_the_provider_that_declares_it():
    # The registry is where a route's origin comes from, so a base URL that
    # points somewhere the host policy does not allow would hand every call site
    # built from it an allowance the policy never granted.
    hosts = frozenset({"example.com"})
    assert ProviderDefinition("x", "X", 10, True, "phpbb", "ready", hosts, "https://example.com/").base_url
    for bad in ("http://example.com/", "https://elsewhere.example/", "https://example.com",
                "https://example.com/?q=1"):
        with pytest.raises(ValueError):
            ProviderDefinition("x", "X", 10, True, "phpbb", "ready", hosts, bad)


def test_every_provider_that_is_reached_over_the_network_says_where_it_is():
    for definition in DEFAULT_PROVIDER_DEFINITIONS:
        if definition.network_state == "disabled":
            continue
        assert definition.base_url, definition.provider
        assert definition.base_url.endswith("/")


def test_a_linked_read_failure_is_narrower_than_a_search_failure(tmp_path):
    """Recorded against the provider that answered, and nothing more.

    A read another provider's page asked for is not a search of this one, so
    inflating that count would misdescribe what it was asked to do. Its own
    state is not relabelled either, and no deadline is written: a failure
    carrying none writes a cleared one, which is how a cooldown came to be
    erased by the very thing that observed it.
    """
    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("github", error="HTTP 429", http_status=429, retry_after="600")
    deadline = store.snapshot()["providers"]["github"]["cooldown_until_epoch_s"]
    searches = store.snapshot()["providers"]["github"]["counters"]["searches"]
    errors = store.snapshot()["providers"]["github"]["counters"]["errors"]

    store.record_linked_failure("github", error="NetworkError: GitHub returned HTTP 500")

    state = store.snapshot()["providers"]["github"]
    assert state["counters"]["errors"] == errors + 1
    assert state["counters"]["searches"] == searches
    assert state["cooldown_until_epoch_s"] == deadline
    assert not store.can_attempt("github")
    assert "HTTP 500" in state["last_error"]

    # It is still held to the same bounded, single-line error contract.
    with pytest.raises(ValueError):
        store.record_linked_failure("github", error="two\nlines")
    with pytest.raises(ValueError):
        store.record_linked_failure("github", error="")


def test_selectable_providers_are_the_ones_this_build_can_actually_reach():
    from ce_decky.providers import selectable_providers

    selectable = {definition.provider for definition in selectable_providers()}
    # Wider than searchable: a source reached only as the exact page another
    # catalog links is still a source a user may not want contacted.
    assert {definition.provider for definition in searchable_providers()} <= selectable
    # Narrower than the registry: a provider this build never uses is not a
    # choice to offer.
    assert "cheatenginenet" not in selectable
    assert selectable == {"fearless", "playground", "github", "thecheatscript", "vgtimes"}


def test_the_provider_id_grammar_has_one_public_owner():
    from ce_decky.providers import normalized_provider_id

    assert normalized_provider_id("github") == "github"
    for bad in ("../etc", "GitHub", "", "a" * 65, None, 7):
        with pytest.raises(ValueError):
            normalized_provider_id(bad)


def test_a_successful_download_clears_a_failed_last_attempt(tmp_path):
    # `record_failure(download=True)` writes this state, so leaving a success to
    # clear only half of it let one row report a finished download, no last
    # error, and "last attempt failed" at the same time.
    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("vgtimes", error="HTTP 500", http_status=500, download=True)
    assert store.snapshot()["providers"]["vgtimes"]["state"] == "error"

    store.record_download("vgtimes", bytes_downloaded=2048)
    entry = store.snapshot()["providers"]["vgtimes"]
    assert entry["state"] == "ready"
    assert entry["last_error"] is None
    assert entry["counters"]["downloads_succeeded"] == 1
    assert entry["counters"]["downloads_failed"] == 1


def test_a_waited_out_rate_limit_is_still_not_an_attempt_that_ended(tmp_path):
    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("vgtimes", error="HTTP 500", http_status=500)
    store.record_throttle("vgtimes", wait_seconds=30)
    entry = store.snapshot()["providers"]["vgtimes"]
    assert entry["state"] == "error"
    assert entry["last_error"] == "HTTP 500"


def test_a_linked_read_is_counted_as_itself_and_never_as_a_search(tmp_path):
    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("github", error="HTTP 429", http_status=429, retry_after="600")
    before = store.snapshot()["providers"]["github"]

    store.record_linked_success("github", results=2)
    entry = store.snapshot()["providers"]["github"]
    assert entry["counters"]["results"] == 2
    assert entry["counters"]["linked_reads"] == 1
    assert entry["counters"]["searches"] == before["counters"]["searches"]
    # One page answering for somebody else is not evidence that this provider's
    # own search works, and a success naming no deadline must not erase one the
    # provider did ask for.
    assert entry["state"] == before["state"]
    assert entry["cooldown_until_epoch_s"] == before["cooldown_until_epoch_s"]

    assert store.record_linked_success("github", results=0) is None
    assert store.snapshot()["providers"]["github"]["counters"]["linked_reads"] == 1


def test_a_corrupt_counter_record_is_repaired_by_reset_and_not_by_use(tmp_path):
    path = tmp_path / "providers.json"
    path.write_text('{"schema": 9, "providers": {}}', encoding="utf-8")
    store = ProviderDiagnosticsStore(path)

    # Using the plugin cannot repair it: every recording path loads the whole
    # record first, and every caller swallows that failure on purpose so a
    # broken counter can never fail a search.
    with pytest.raises(ValueError):
        store.record_search_success("fearless", results=1, latency_ms=1)
    with pytest.raises(ValueError):
        store.snapshot()

    assert store.reset() is False
    assert store.snapshot()["providers"] == {}
    store.record_search_success("fearless", results=1, latency_ms=1)
    assert store.snapshot()["providers"]["fearless"]["counters"]["results"] == 1


def test_resetting_a_readable_counter_record_reports_that_it_held_something(tmp_path):
    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    assert store.reset() is False
    store.record_search_success("fearless", results=1, latency_ms=1)
    assert store.reset() is True
    assert store.snapshot()["providers"] == {}


def test_a_successful_download_supersedes_the_status_of_the_attempt_before_it(tmp_path):
    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure(
        "vgtimes", error="HTTP 429", http_status=429, retry_after="600", download=True, now=1000.0,
    )
    assert store.snapshot()["providers"]["vgtimes"]["last_http_status"] == 429

    store.record_throttle("vgtimes", wait_seconds=45)
    store.record_download("vgtimes", bytes_downloaded=4096)
    entry = store.snapshot()["providers"]["vgtimes"]

    # The last attempt returned the file, so it is not described by the status
    # of one that has been superseded.
    assert entry["last_http_status"] is None
    assert entry["last_error"] is None
    # The deadline the provider asked for is a promise `can_attempt` still
    # honours, and one route serving a file is not the provider withdrawing it:
    # a watched download is throttled by its own transfer rather than by the
    # crawl's deadline, so succeeding during a cooldown is expected.
    assert entry["cooldown_until_epoch_s"] > 1000.0
    assert store.can_attempt("vgtimes", now=1100.0) is False
    # What the throttling cost stays as history.
    assert entry["counters"]["downloads_throttled"] == 1
    assert entry["last_throttle_wait_s"] == 45
