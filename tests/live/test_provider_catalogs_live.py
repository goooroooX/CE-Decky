"""Opt-in, read-only live provider smoke tests.

These tests exercise production catalog, transport, acquisition, archive, and
TableStore code against a small set of public artifacts. They never execute a
table and write only below pytest's temporary directory.

Run explicitly with:

    CE_DECKY_RUN_LIVE_PROVIDER_TESTS=1 python -m pytest tests/live -v -s

Provider pages and artifacts are third-party mutable state. A failure is a
compatibility signal to investigate, not a reason to weaken deterministic CI.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ce_decky.acquisition import AcquisitionManager
from ce_decky.catalog import (
    ArtifactRecord,
    CatalogService,
    MAX_HTML_BYTES,
    PROVIDER_HOSTS,
    decode_html,
    parse_phpbb_attachments,
    parse_playground_page,
)
from ce_decky.network import NetworkClient
from ce_decky.network_tls import build_verified_ssl_context
from ce_decky.ct_inspector import inspect_table
from ce_decky.table_store import TableStore
from scripts.table_ui_probe import summarize_inspection


pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.environ.get("CE_DECKY_RUN_LIVE_PROVIDER_TESTS") != "1",
        reason="set CE_DECKY_RUN_LIVE_PROVIDER_TESTS=1 to contact live providers",
    ),
]


FEARLESS_TOPIC = "https://fearlessrevolution.com/viewtopic.php?f=4&t=19746"
FEARLESS_ATTACHMENTS = ("45392", "45369")
GITHUB_SOURCE = "https://github.com/xAranaktu/FIFA-18---Career-Mode-Cheat-Table"
GITHUB_VERSIONS = ("02.06.2018", "30.05.2018")
# A game whose table is committed in a repository tree rather than published as
# a release asset, so the route with git's object name as its only integrity
# anchor is the one this exercises.
GITHUB_TREE_GAME = "baldur s gate 3"
GITHUB_TREE_REPO = "themaoci/BaldursGate3-CheatTable"
# A game whose tables VGTimes publishes. The whole acquisition runs, including
# the countdown the server enforces and the challenge it answers with, so this
# case is slow by design rather than by accident.
VGTIMES_GAME = "atomic heart"
PLAYGROUND_PAGES = (
    "https://www.playground.ru/dome_keeper/cheat/dome_keeper_tablitsa_dlya_cheat_engine_upd_28_09_2022_cfemen-1236756",
    "https://www.playground.ru/elex_2/cheat/elex_2_tablitsa_dlya_cheat_engine_upd_06_03_2022_tuuuup-1184402",
)


def _network() -> NetworkClient:
    context, _ = build_verified_ssl_context()
    return NetworkClient(context, user_agent="CE-Decky-live-test/0.1")


async def _acquire(record: ArtifactRecord, tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    network = _network()
    catalog = CatalogService(network, tmp_path / "catalog.json")
    catalog.artifacts[(record.result.provider, record.result.artifact_id)] = record
    home = tmp_path / "home"
    store = TableStore(tmp_path / "tables")
    manager = AcquisitionManager(
        catalog,
        network,
        store,
        tmp_path / "staging",
        home / "Downloads",
        home,
        lambda: None,
    )
    try:
        started = await manager.start(record.result.provider, record.result.artifact_id)
        acquisition_id = str(started["acquisition_id"])
        await manager.wait_ready(acquisition_id)
        ready = manager.poll(acquisition_id)
        assert ready["state"] in {"ready_to_import", "needs_selection"}, ready.get("error")
        members = (ready.get("inspection") or {}).get("members", [])
        member_path = members[0]["path"] if ready["state"] == "needs_selection" else None
        completed = await manager.complete(acquisition_id, member_path=member_path)
        assert completed["state"] == "imported"
        assert completed["execution_consent"] is False
        imported = completed["imported"]
        assert isinstance(imported, dict)
        digest = imported["sha256"]
        filename = imported["filename"]
        assert isinstance(digest, str) and isinstance(filename, str)
        inspection = inspect_table(store.verified_blob(digest), digest)
        controller = summarize_inspection(filename, inspection)
        assert controller["navigation_complete"] is True
        return completed, controller
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.direct_provider
@pytest.mark.parametrize("attachment_id", FEARLESS_ATTACHMENTS)
async def test_fearless_known_attachment_downloads_and_imports(attachment_id: str, tmp_path: Path):
    network = _network()
    try:
        response = await network.get(
            FEARLESS_TOPIC,
            allowed_hosts=PROVIDER_HOSTS["fearless"],
            max_bytes=MAX_HTML_BYTES,
            referer="https://fearlessrevolution.com/",
        )
        assert response.status == 200
        records = parse_phpbb_attachments(
            decode_html(response.body),
            provider="fearless",
            display_name="FearLess Cheat Engine",
            topic_id="19746",
            topic_title="Dome Keeper",
            source_page=FEARLESS_TOPIC,
            score=1.0,
            rank=100, hosts=PROVIDER_HOSTS["fearless"],
        )
        record = next(row for row in records if row.result.artifact_id.endswith(f"attachment-{attachment_id}"))
    finally:
        await network.aclose()
    completed, controller = await _acquire(record, tmp_path)
    assert completed["imported"]["origins"][0]["provider"] == "fearless"
    assert controller["safe_actionable_controls"] + controller["groups"] <= controller["inspected_controls"]


@pytest.mark.asyncio
@pytest.mark.direct_provider
@pytest.mark.parametrize("version", GITHUB_VERSIONS)
async def test_exact_source_github_release_downloads_and_imports(version: str, tmp_path: Path):
    network = _network()
    catalog = CatalogService(network, tmp_path / "discovery.json")
    try:
        records = await catalog._github(GITHUB_SOURCE, 1.0)
        record = next(row for row in records if row.result.version == version)
    finally:
        await network.aclose()
    completed, controller = await _acquire(record, tmp_path)
    assert completed["imported"]["origins"][0]["provider"] == "github"
    assert controller["safe_actionable_controls"] + controller["groups"] <= controller["inspected_controls"]


@pytest.mark.asyncio
@pytest.mark.direct_provider
async def test_playground_known_items_download_and_import_directly(tmp_path: Path):
    selected_records = []
    network = _network()
    try:
        for page in PLAYGROUND_PAGES:
            response = await network.get(
                page,
                allowed_hosts=PROVIDER_HOSTS["playground"],
                max_bytes=MAX_HTML_BYTES,
                referer="https://www.playground.ru/",
            )
            assert response.status == 200
            page_records, source = parse_playground_page(decode_html(response.body), page, 1.0)
            assert page_records and page_records[0].advertised_sha256
            assert page_records[0].result.size_bytes and page_records[0].result.size_bytes > 0
            assert page_records[0].result.download_mode == "direct_https"
            assert page_records[0].acquisition is not None
            assert source and "fearlessrevolution.com" in source
            selected_records.append(page_records[0])
    finally:
        await network.aclose()

    acquired = await asyncio.gather(*(_acquire(record, tmp_path / str(index)) for index, record in enumerate(selected_records)))
    completed, controllers = zip(*acquired)
    assert all(item["state"] == "imported" for item in completed)
    assert all(item["imported"]["origins"][0]["provider"] == "playground" for item in completed)
    assert all(item["navigation_complete"] is True for item in controllers)


@pytest.mark.asyncio
@pytest.mark.direct_provider
async def test_thecheatscript_current_yandex_artifact_downloads_and_imports(tmp_path: Path):
    network = _network()
    catalog = CatalogService(network, tmp_path / "discovery.json")
    try:
        records = await catalog._thecheatscript(["sort them ducks"])
        record = next(row for row in records if row.result.table_title == "Sort Them Ducks")
        assert record.result.download_mode == "direct_https"
        assert record.result.size_bytes and record.result.size_bytes > 0
        assert record.advertised_sha256
    finally:
        await catalog.close()
        await network.aclose()
    completed, controller = await _acquire(record, tmp_path)
    assert completed["imported"]["origins"][0]["provider"] == "thecheatscript"
    assert completed["state"] == "imported"
    assert controller["navigation_complete"] is True


@pytest.mark.asyncio
@pytest.mark.direct_provider
async def test_github_repository_search_finds_a_committed_table_and_verifies_its_object_name(tmp_path: Path):
    """The tree route advertises git's object name instead of a content digest.

    Downloading through the production path is what proves that name belongs to
    the bytes the raw route actually serves, rather than only to the object the
    tree API described.
    """
    network = _network()
    catalog = CatalogService(network, tmp_path / "discovery.json")
    try:
        records = await catalog._github_search([GITHUB_TREE_GAME])
        record = next(row for row in records if row.result.topic_id == GITHUB_TREE_REPO)
        assert record.result.download_mode == "direct_https"
        assert record.blob_sha1 and record.advertised_sha256 is None
        assert record.result.size_bytes and record.result.size_bytes > 0
    finally:
        await catalog.close()
        await network.aclose()
    completed, controller = await _acquire(record, tmp_path)
    assert completed["state"] == "imported"
    assert completed["imported"]["origins"][0]["provider"] == "github"
    assert controller["navigation_complete"] is True


@pytest.mark.asyncio
@pytest.mark.direct_provider
async def test_vgtimes_search_downloads_and_imports_through_its_own_chain(tmp_path: Path):
    """The whole VGTimes chain, with no browser and no invented claim.

    Discovery is the game's own table listing, the digest comes from the report
    the item page links, and acquisition waits out the countdown the server
    enforces before answering the challenge. The download is verified against
    that digest by the ordinary acquisition path, so this asserts the outcome
    rather than the arithmetic.
    """
    network = _network()
    catalog = CatalogService(network, tmp_path / "discovery.json")
    try:
        records = await catalog._vgtimes([VGTIMES_GAME])
        record = next(row for row in records if row.advertised_sha256 and not row.password_hint)
        assert record.result.download_mode == "direct_https"
        assert record.result.size_bytes and record.result.size_bytes > 0
        assert record.acquisition is not None and record.acquisition.exclusive is True
    finally:
        await catalog.close()
        await network.aclose()
    completed, controller = await _acquire(record, tmp_path)
    assert completed["state"] == "imported"
    assert completed["imported"]["origins"][0]["provider"] == "vgtimes"
    assert controller["navigation_complete"] is True
