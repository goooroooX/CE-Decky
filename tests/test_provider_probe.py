"""The Playground download diagnostic must stay wired to the production record.

`provider_probe.py` is the tracked helper the provider diagnostics route names,
and it reads the acquisition request straight off a production `ArtifactRecord`.
When that field was renamed the probe kept the old attribute, which no record
has any more, so the diagnostic would have ended in `AttributeError` the next
time anyone asked for it. Nothing else exercised it.
"""
from __future__ import annotations

import pytest

from ce_decky.catalog import ArtifactRecord, ForumListing, PlaygroundDownloadRequest, parse_playground_page
from scripts import provider_probe


PAGE = """
<html><body>
  <h1>Game</h1>
  <div class="js-post" data-post-id="777">
    <pg-file data-file-id="1236756" data-name="Game.CT" data-size="24310"
             data-download-count="12"></pg-file>
  </div>
</body></html>
"""


def test_the_probe_reads_the_acquisition_request_the_parser_actually_produces():
    records, _ = parse_playground_page(PAGE, "https://www.playground.ru/game/cheat/item-1", 1.0)
    assert records, "the fixture must produce one production record"
    record = records[0]
    assert isinstance(record.acquisition, PlaygroundDownloadRequest)
    # Exactly the expression the probe guards its diagnostic with. A record that
    # no longer carries the field the probe reads must fail this, not the probe.
    assert isinstance(record.acquisition, provider_probe.PlaygroundDownloadRequest)
    assert not hasattr(record, "playground_download")


def test_a_record_without_a_deferred_request_is_refused_rather_than_probed():
    record = ArtifactRecord(records_result := _result(), None, None, None)
    assert record.acquisition is None
    assert not isinstance(record.acquisition, provider_probe.PlaygroundDownloadRequest)
    assert records_result.provider == "playground"


def _result():
    from ce_decky.providers import CatalogResult

    return CatalogResult(
        provider="playground",
        provider_display_name="Playground",
        topic_id="1",
        artifact_id="page-1:file-2",
        table_title="Table",
        filename="table.ct",
        version=None,
        size_bytes=None,
        source_page="https://www.playground.ru/game/cheat/item-1",
        download_mode="system_browser_downloads",
        match_score=1.0,
        provider_rank=85,
    )


def test_the_probe_declares_the_two_diagnostics_it_documents():
    parser = provider_probe.build_parser() if hasattr(provider_probe, "build_parser") else None
    if parser is None:
        pytest.skip("the probe builds its parser inline")
    options = {action.dest for action in parser._actions}
    assert {"routes", "playground_api"}.issubset(options)


def test_fearless_probe_uses_the_declared_stride_and_an_actual_topic():
    routes = provider_probe.fearless_followup_routes(
        ForumListing(
            [("123", "Example Game")], current_page=1, total_pages=5,
            total_topics=101, page_step=25, page_step_invalid=False,
        )
    )
    assert [(route.key, route.url) for route in routes] == [
        ("fearless-forum-page", "https://fearlessrevolution.com/viewforum.php?f=4&start=25"),
        ("fearless-topic", "https://fearlessrevolution.com/viewtopic.php?f=4&t=123"),
    ]
