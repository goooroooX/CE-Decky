"""What the provider caches hold, reported rather than read by hand.

Every question about why a search answered the way it did used to be a
`python3 -` over `fearless-index.json`: how much of the listing is indexed, how
stale it is, how much it still owes. That is the shape `AGENTS.md` names as the
tracked-helper mistake, and a figure read that way is not one this project may
record.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ce_decky.catalog import CatalogService, FEARLESS_INDEX_MAX_AGE_SECONDS, FEARLESS_SEARCH_MARKER_NAME
from scripts import target_state_probe


def _probe(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    settings = tmp_path / "settings"
    proc = tmp_path / "proc"
    for path in (home, settings, proc):
        path.mkdir(parents=True, exist_ok=True)
    return target_state_probe.probe(home, settings, None, proc)


def test_the_reported_freshness_window_is_the_crawls_own():
    # Repeated rather than imported, because importing the catalog pulls in the
    # HTML and XML parsers a probe under the system interpreter has no reason to
    # need. Repeated means it can drift, so it is held here instead.
    assert target_state_probe.PROVIDER_PAGE_FRESHNESS_SECONDS == FEARLESS_INDEX_MAX_AGE_SECONDS


def test_the_reported_search_marker_is_the_file_the_plugin_writes():
    # Repeated for the same reason as the window above, and held equal here.
    assert target_state_probe.SEARCH_MARKER_NAME == FEARLESS_SEARCH_MARKER_NAME


def test_a_device_with_no_provider_cache_says_so_rather_than_failing(tmp_path: Path):
    report = _probe(tmp_path)
    caches = report["provider_caches"]["files"]
    assert report["provider_cache_error"] is None
    assert caches["fearless-index.json"] == {"present": False}
    assert caches["playground-sitemap.json"] == {"present": False}


def test_the_listing_is_reported_by_shape_and_never_by_content(tmp_path: Path):
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    cache.mkdir(parents=True)
    now = time.time()
    (cache / "fearless-index.json").write_text(json.dumps({
        "schema": 1,
        "total_pages": 4,
        "total_topics": 200,
        "page_step": 50,
        "pages": {"0": [["1", "A Game"]], "50": [["2", "Another"]], "100": [["3", "Third"]]},
        # One page read moments ago, one two days ago, one with no timestamp at
        # all; the fourth page of the listing was never read.
        "fetched": {"0": now - 60, "50": now - 2 * 24 * 60 * 60},
        "searched": now - 3600,
    }), encoding="utf-8")

    listing = _probe(tmp_path)["provider_caches"]["files"]["fearless-index.json"]["listing"]
    assert listing["indexed_pages"] == 3 and listing["total_pages"] == 4
    assert listing["topics"] == 200 and listing["page_step"] == 50
    assert listing["newest_page_age_h"] == 0.0
    assert listing["oldest_page_age_h"] == 48.0
    # Never read, read too long ago, and read without a timestamp are all due.
    assert listing["pages_due"] == 3
    assert listing["searched_age_h"] == 1.0
    assert listing["background_pass_armed"] is True
    # The topics themselves are the content and are never emitted.
    assert "A Game" not in json.dumps(listing)


def test_an_index_nobody_has_searched_for_reports_a_disarmed_pass(tmp_path: Path):
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    cache.mkdir(parents=True)
    (cache / "fearless-index.json").write_text(json.dumps({
        "schema": 1, "total_pages": 1, "pages": {"0": []}, "fetched": {},
    }), encoding="utf-8")

    listing = _probe(tmp_path)["provider_caches"]["files"]["fearless-index.json"]["listing"]
    assert listing["background_pass_armed"] is False
    assert listing["searched_age_h"] is None


def test_a_search_recorded_only_in_its_own_marker_still_arms_the_reported_pass(tmp_path: Path):
    """The plugin reads the newer of the two records, and so does this report.

    The marker rides in the index cache and in a small file of its own, and the
    unload prologue writes only the small one: the index is behind a lock a
    native writer holds across its own `fsync`, and a stop cannot wait for it.
    A report that read the index alone would answer a question about the
    background pass with a moment the plugin itself no longer believes.
    """
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    cache.mkdir(parents=True)
    now = time.time()
    stale = now - (FEARLESS_INDEX_MAX_AGE_SECONDS + 7200)
    (cache / "fearless-index.json").write_text(json.dumps({
        "schema": 1, "total_pages": 1, "pages": {"0": []}, "fetched": {}, "searched": stale,
    }), encoding="utf-8")
    listing = _probe(tmp_path)["provider_caches"]["files"]["fearless-index.json"]["listing"]
    assert listing["background_pass_armed"] is False

    (cache / FEARLESS_SEARCH_MARKER_NAME).write_text(
        json.dumps({"schema": 1, "searched": now - 3600}), encoding="utf-8",
    )
    report = _probe(tmp_path)["provider_caches"]["files"]
    listing = report["fearless-index.json"]["listing"]
    assert listing["background_pass_armed"] is True
    assert listing["searched_age_h"] == 1.0
    # And the marker's own file is reported as a cache of its own.
    assert report[FEARLESS_SEARCH_MARKER_NAME]["present"] is True


def test_an_unreadable_search_marker_leaves_the_index_to_answer(tmp_path: Path):
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    cache.mkdir(parents=True)
    now = time.time()
    (cache / "fearless-index.json").write_text(json.dumps({
        "schema": 1, "total_pages": 1, "pages": {"0": []}, "fetched": {}, "searched": now - 3600,
    }), encoding="utf-8")
    (cache / FEARLESS_SEARCH_MARKER_NAME).write_text("{not json", encoding="utf-8")

    listing = _probe(tmp_path)["provider_caches"]["files"]["fearless-index.json"]["listing"]
    assert listing["background_pass_armed"] is True
    assert listing["searched_age_h"] == 1.0


def test_an_unreadable_index_is_described_rather_than_failing_the_report(tmp_path: Path):
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    cache.mkdir(parents=True)
    (cache / "fearless-index.json").write_text("{not json", encoding="utf-8")

    entry = _probe(tmp_path)["provider_caches"]["files"]["fearless-index.json"]
    assert entry["present"] is True and "listing_error" in entry


def _armed_by_the_plugin(tmp_path: Path) -> bool:
    """What a freshly loaded backend would do with these exact files."""
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    service = CatalogService(None, cache / "provider-results.json")  # type: ignore[arg-type]
    return service._index_is_armed()


def _armed_by_the_probe(tmp_path: Path) -> dict:
    return _probe(tmp_path)["provider_caches"]["files"]["fearless-index.json"]["listing"]


def _index(cache: Path, searched: object) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "total_pages": 1, "pages": {"0": []}, "fetched": {}}
    if searched is not None:
        payload["searched"] = searched
    (cache / "fearless-index.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("offset, armed", [
    (-3600.0, True),
    (-(FEARLESS_INDEX_MAX_AGE_SECONDS + 3600), False),
    # Further into the future than a page stays fresh: the plugin reads this as
    # never searched, so the crawl is disarmed. The probe used to compute a
    # negative age, find it below the window and report the pass armed, which is
    # the opposite answer at the one moment the report is being read - after a
    # clock correction, or over a cache something corrupted.
    (2 * FEARLESS_INDEX_MAX_AGE_SECONDS, False),
])
def test_the_probe_arms_the_pass_exactly_when_the_plugin_would(tmp_path: Path, offset: float, armed: bool):
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    now = time.time()
    _index(cache, now + offset)

    assert _armed_by_the_probe(tmp_path)["background_pass_armed"] is armed
    assert _armed_by_the_plugin(tmp_path) is armed

    # And again with the moment in the marker's own file instead of the index.
    _index(cache, None)
    (cache / FEARLESS_SEARCH_MARKER_NAME).write_text(
        json.dumps({"schema": 1, "searched": now + offset}), encoding="utf-8",
    )

    assert _armed_by_the_probe(tmp_path)["background_pass_armed"] is armed
    assert _armed_by_the_plugin(tmp_path) is armed


@pytest.mark.parametrize("searched", [0, -1, True, "recently", None, float("nan"), float("inf")])
def test_a_moment_the_plugin_refuses_never_arms_the_reported_pass(tmp_path: Path, searched: object):
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    _index(cache, searched)

    assert _armed_by_the_probe(tmp_path)["background_pass_armed"] is False
    assert _armed_by_the_plugin(tmp_path) is False


def test_a_refused_moment_is_reported_as_refused_rather_than_as_absent(tmp_path: Path):
    """A cache this device cannot act on is what the report is being read for."""
    cache = tmp_path / "home" / ".cheat-engine-decky" / "cache"
    now = time.time()
    _index(cache, now + 2 * FEARLESS_INDEX_MAX_AGE_SECONDS)
    listing = _armed_by_the_probe(tmp_path)
    assert listing["searched_rejected"] is True
    assert listing["searched_age_h"] is None

    _index(cache, now - 3600)
    listing = _armed_by_the_probe(tmp_path)
    assert listing["searched_rejected"] is False
    assert listing["searched_age_h"] == 1.0
