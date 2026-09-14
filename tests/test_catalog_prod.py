from __future__ import annotations

import asyncio
import json
import threading
import time
import types
import pytest
from pathlib import Path
import ce_decky.catalog as catalog_module

from ce_decky.catalog import (
    STALE_TTL_SECONDS,
    ArtifactRecord,
    CatalogService,
    PLAYGROUND_ARTIFACT_HOSTS,
    decode_html,
    parse_github_releases,
    parse_fearless_forum_page,
    parse_phpbb_attachments,
    PROVIDER_HOSTS,
    collect_parse_issues,
    fearless_topic_title,
    searching_with_rar_support,
    catalog_result_or_none,
    rows_from_page,
    parse_playground_category,
    parse_playground_page,
    parse_playground_sitemap,
    ParseIssues,
    playground_game_slug,
    sitemap_prefilter_tokens,
    ProviderRateLimited,
    resolve_playground_download,
    BrowserHandoff,
    NetworkError,
)
from ce_decky.providers import CatalogResult


FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture
def unpaced_crawl(monkeypatch):
    """The listing crawl without the second it waits between two pages.

    For a test about which pages are read and what the index ends up holding,
    rather than about the pacing itself. At the real interval a four page crawl
    is four seconds of a test suite sleeping, and the twelve page one was
    eleven; the tests that are about the pacing set their own value and are not
    given this.
    """
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_REQUEST_INTERVAL_SECONDS", 0.0)


def test_provider_html_decoder_preserves_utf8_and_legacy_cyrillic():
    assert decode_html("Таблица".encode("utf-8")) == "Таблица"
    assert decode_html("Таблица".encode("windows-1251")) == "Таблица"


def test_the_forums_own_sort_marker_is_not_part_of_the_game_name():
    """FearLess sorts a topic to the end of its listing with a lowercase `z`.

    Scored as part of the name it cost the whole topic: the marker in front of a
    title reached 0.41 against a search for that game where the title alone
    reached 0.95, far below the floor a topic must clear to be fetched, so a
    table with thousands of downloads at the source was absent from the answer.
    Those two figures are from the topic this was found on; the title below is a
    stand-in that exercises the rule rather than reproducing them. On one
    device's index 132 of 16568 topics carried the marker, current releases
    among them.
    """
    from ce_decky.game_identity import Candidate, match_breakdown

    assert fearless_topic_title("z Hidden Blade") == "Hidden Blade"
    assert fearless_topic_title("z S.T.A.L.K.E.R. 2: Heart of Chornobyl") == "S.T.A.L.K.E.R. 2: Heart of Chornobyl"
    scored = match_breakdown(["Hidden Blade"], Candidate(fearless_topic_title("z Hidden Blade"), provider_trust=0.7))
    assert scored.confidence >= 0.9

    # Case is the whole of the rule. `Z Remastered` is a real game on the same
    # listing, and a title that is only the marker is not a marker at all.
    assert fearless_topic_title("Z Remastered") == "Z Remastered"
    assert fearless_topic_title("z") == "z"
    assert fearless_topic_title("Zero Escape") == "Zero Escape"


def test_a_rar_row_is_dropped_only_where_it_could_not_be_opened():
    """A device whose 7-Zip has no RAR handler must not be offered the row.

    Offering it costs a provider countdown and a whole download to be refused at
    the archive, which is the same reason a row of a known impossible size is
    dropped at the search. Everywhere else the row is offered, because the
    sources publish tables as `.rar` and dropping them is what made one game's
    only table at one source invisible.
    """
    fields = dict(
        provider="vgtimes", provider_display_name="VGTimes", topic_id="game", artifact_id="game:file-1",
        table_title="Game: a table", filename="table_1770108183_468931.rar", version=None,
        size_bytes=18015, source_page="https://vgtimes.ru/games/game/files/1-table.html",
        download_mode="direct_https", match_score=1.0, provider_rank=70,
    )
    with collect_parse_issues() as issues, searching_with_rar_support(True):
        assert catalog_result_or_none(**fields) is not None
    assert issues.degraded == 0

    with collect_parse_issues() as issues, searching_with_rar_support(False):
        assert catalog_result_or_none(**fields) is None
    assert issues.degraded == 1

    # And a `.zip` from the same source is never subject to it.
    with collect_parse_issues() as issues, searching_with_rar_support(False):
        assert catalog_result_or_none(**{**fields, "filename": "table.zip"}) is not None
    assert issues.degraded == 0


def test_every_source_offers_the_same_archive_kinds_as_a_row_may_carry():
    """One set, or a source silently has less than it has.

    Each adapter filtered its own listing by suffix before a row was ever built,
    and each filter was a copy. Adding `.rar` to what a row may carry left five
    of those copies behind, so the sources that publish in it went on dropping
    those rows at the parser and the format was downloadable from exactly one
    source out of five.
    """
    from ce_decky import github_discovery, thecheatscript
    from ce_decky.catalog import SUPPORTED_SUFFIXES
    from ce_decky.providers import ARTIFACT_SUFFIXES

    assert ".rar" in ARTIFACT_SUFFIXES
    assert SUPPORTED_SUFFIXES == ARTIFACT_SUFFIXES
    assert github_discovery.SUPPORTED_SUFFIXES == ARTIFACT_SUFFIXES
    assert thecheatscript.SUPPORTED_SUFFIXES == ARTIFACT_SUFFIXES

    # And it reaches a row on the source whose own listing was dropping it.
    body = (
        '<div class="post" id="p1"><div class="inline-attachment"><dl class="file">'
        '<dt><a class="postlink" href="./download/file.php?id=42">Game.rar</a></dt>'
        '<dd>(116.52 KiB) Downloaded 54 times</dd></dl></div></div>'
    )
    with collect_parse_issues(), searching_with_rar_support(True):
        rows = parse_phpbb_attachments(
            body, provider="fearless", display_name="FearLess Cheat Engine",
            topic_id="10", topic_title="Example Game",
            source_page="https://fearlessrevolution.com/viewtopic.php?t=10",
            score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
        )
    assert [row.result.filename for row in rows] == ["Game.rar"]


def test_current_fearless_attachment_shape_is_canonical_and_stable():
    rows = parse_phpbb_attachments(
        (FIXTURES / "fearless_current.html").read_text(), provider="fearless",
        display_name="FearLess Cheat Engine", topic_id="10", topic_title="Example Game",
        source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
    )
    assert len(rows) == 1
    assert rows[0].result.artifact_id == "topic-10:attachment-42"
    assert rows[0].direct_url == "https://fearlessrevolution.com/download/file.php?id=42"
    assert rows[0].result.download_count == 1234
    assert rows[0].result.version == "2"


def test_an_attachment_keeps_the_uploaders_own_note_about_it():
    """One topic carries every revision of one table, told apart by a sentence
    the uploader wrote on each file.

    Ten rows of one live topic shared the file name, the topic title and the
    date of the post they sit in, and differed only by that note and by their
    size, two of which matched as well. What was kept instead was the whole
    block, which is that note with the size and the download count already shown
    beside it stuck on the end.
    """
    body = (
        '<div class="post" id="p1"><div class="inline-attachment"><dl class="file">'
        '<dt><a class="postlink" href="./download/file.php?id=42">Game.CT</a></dt>'
        'NOTE<dd>(116.52 KiB) Downloaded 54 times</dd></dl></div></div>'
    )

    def parsed(note: str):
        with collect_parse_issues():
            return parse_phpbb_attachments(
                body.replace("NOTE", note), provider="fearless",
                display_name="FearLess Cheat Engine", topic_id="10", topic_title="Example Game",
                source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9,
                rank=100, hosts=PROVIDER_HOSTS["fearless"],
            )[0].result

    written = parsed("<dd><em>Added max trust gain - Added drop item type</em></dd>")
    assert written.notes == "Added max trust gain - Added drop item type"

    # The live forum writes it as `- Updated to 1.04.02 ...`: the dash separates
    # the note from the file name above it, and on a row that shows the note
    # first it is a stray mark before the sentence.
    assert parsed("<dd><em>- Updated to 1.04.02 but some scripts still broken</em></dd>").notes == (
        "Updated to 1.04.02 but some scripts still broken"
    )
    assert written.download_count == 54

    # Nothing written on it says nothing, rather than repeating the size and the
    # count the row already shows.
    assert parsed("").notes is None

    # A published archive password is used by the backend and never displayed,
    # logged or persisted, so a note that quotes it is dropped whole.
    hidden = parsed("<dd><em>Repacked. Password: secret</em></dd>")
    assert hidden.notes is None
    assert "secret" not in json.dumps(hidden.as_dict(), ensure_ascii=False)


def test_attachment_version_requires_an_explicit_publisher_marker():
    def parsed(filename: str, metadata: str) -> str | None:
        html = f'''<div class="post" id="p1"><div class="inline-attachment"><dl class="file">
          <dt><a class="postlink" href="./download/file.php?id=42">{filename}</a></dt>
          <dd>{metadata}</dd></dl></div></div>'''
        rows = parse_phpbb_attachments(
            html, provider="fearless", display_name="FearLess Cheat Engine",
            topic_id="10", topic_title="Example Game",
            source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
        )
        return rows[0].result.version

    # These exact decimal shapes were displayed as v78.21 and v505.53 on the
    # target even though they are attachment sizes, not publisher versions.
    assert parsed("Unmarked.CT", "78.21 KiB · Downloaded 505 times") is None
    assert parsed("Unmarked.CT", "505.53 KiB") is None
    assert parsed("Table_v1.2.CT", "505.53 KiB") == "1.2"
    assert parsed("Table.CT", "Version: 3_4 · 78.21 KiB") == "3.4"
    assert parsed("Table_1.0.5.CT", "505.53 KiB") == "1.0.5"
    assert parsed("Table_Build.9397770.CT", "78.21 KiB") is None


def test_attachment_comment_carries_the_revision_a_shared_filename_hides():
    # One FearLess post carries every revision of its table: the attachments
    # share a filename and the post's date, and only the uploader's comment says
    # which release each one is. phpBB renders that comment in its own element,
    # so reading a version there is not the bare-decimal guess the size line
    # forced the marker rule to refuse.
    def parsed(comment: str, filename: str = "Example.CT") -> tuple[str | None, int | None]:
        html = f'''<div class="post" id="p1"><div class="inline-attachment"><dl class="file">
          <dt><a class="postlink" href="./download/file.php?id=42">{filename}</a></dt>
          <dd><em>{comment}</em></dd>
          <dd>(132.01 KiB) Downloaded 352 times</dd></dl></div></div>'''
        rows = parse_phpbb_attachments(
            html, provider="fearless", display_name="FearLess Cheat Engine",
            topic_id="10", topic_title="Example Game",
            source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
        )
        return rows[0].result.version, rows[0].result.size_bytes

    # The exact shapes the target's own post published for six revisions.
    assert parsed("1.0.6") == ("1.0.6", 135178)
    assert parsed("1.0.4 Read changelog")[0] == "1.0.4"
    assert parsed("1.0.2 - Added Convert Target to Ally.")[0] == "1.0.2"
    assert parsed("1.0.1 (Meteorite+Rel-i343-Meteorite-2607-CU3)")[0] == "1.0.1"
    assert parsed("0.1.0")[0] == "0.1.0"
    # A whole-number release is accepted only as the entire comment, and a
    # marked one anywhere in it; prose that merely opens with a number is not
    # a version, and a comment without one falls back to the filename.
    assert parsed("2")[0] == "2"
    assert parsed("Rebuilt for game version 5.5")[0] == "5.5"
    # The release opens the comment; a marked number further along is about
    # something else, and reading that one instead labelled the file wrongly.
    assert parsed("2.0 fixes for v1.9 saves")[0] == "2.0"
    assert parsed("3 new options")[0] is None
    assert parsed("Read the changelog")[0] is None
    assert parsed("Read the changelog", "Example_v1.2.CT")[0] == "1.2"


def test_unreadable_description_never_withholds_a_downloadable_table():
    """Described metadata is dropped; the table it describes is not."""
    # A post carrying a bidirectional override is exactly the shape the result's
    # own text validation refuses - and refusing it used to raise out of the
    # parser, failing the whole provider and losing every other topic already
    # fetched, for a table sitting behind a working link.
    html = '''<div class="post" id="p1"><time datetime="2026-08-01T12:00:00Z"></time>
      <div class="inline-attachment"><dl class="file">
        <dt><a class="postlink" href="./download/file.php?id=42">Example.CT</a></dt>
        <dd><em>1.0.6 \u202eoverride</em></dd>
        <dd>(132.01 KiB) Downloaded 352 times</dd></dl>
      <dl class="file">
        <dt><a class="postlink" href="./download/file.php?id=43">Second.CT</a></dt>
        <dd><em>1.0.7</em></dd>
        <dd>(100.00 KiB) Downloaded 5 times</dd></dl></div></div>'''
    rows = parse_phpbb_attachments(
        html, provider="fearless", display_name="FearLess Cheat Engine",
        topic_id="10", topic_title="Example Game",
        source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
    )

    assert [row.result.filename for row in rows] == ["Example.CT", "Second.CT"]
    assert rows[0].direct_url == "https://fearlessrevolution.com/download/file.php?id=42"
    # Every described field went with the one that could not be validated,
    # because which of them carried the refused text is not knowable here.
    assert rows[0].result.version is None
    assert rows[0].result.size_bytes is None
    assert rows[0].result.download_count is None
    # The row beside it keeps everything it published.
    assert (rows[1].result.version, rows[1].result.download_count) == ("1.0.7", 5)


def test_a_row_with_no_usable_identity_is_the_only_one_dropped():
    html = '''<div class="post" id="p1"><div class="inline-attachment"><dl class="file">
        <dt><a class="postlink" href="./download/file.php?id=42">../escape.CT</a></dt></dl>
      <dl class="file">
        <dt><a class="postlink" href="./download/file.php?id=43">Good.CT</a></dt></dl></div></div>'''
    rows = parse_phpbb_attachments(
        html, provider="fearless", display_name="FearLess Cheat Engine",
        topic_id="10", topic_title="Example Game",
        source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
    )

    assert [row.result.filename for row in rows] == ["Good.CT"]


def test_one_unreadable_page_costs_that_page_and_not_the_provider():
    def broken() -> list:
        raise ValueError("provider markup changed")

    assert rows_from_page(broken) == []


class _RecordingLogger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.lines.append(("info", message))

    def warning(self, message: str) -> None:
        self.lines.append(("warning", message))


class _RecordingDiagnostics:
    def __init__(self) -> None:
        self.parse_issues: list[tuple[str, int, int]] = []

    def record_parse_issues(self, provider: str, *, degraded: int = 0, failed: int = 0) -> None:
        self.parse_issues.append((provider, degraded, failed))


def test_parse_issues_are_counted_by_how_much_they_cost():
    html = '''<div class="post" id="p1"><div class="inline-attachment"><dl class="file">
        <dt><a class="postlink" href="./download/file.php?id=42">Example.CT</a></dt>
        <dd><em>1.0.6 \u202eoverride</em></dd></dl></div></div>'''

    def broken() -> list:
        raise ValueError("provider markup changed")

    with collect_parse_issues() as issues:
        rows = parse_phpbb_attachments(
            html, provider="fearless", display_name="FearLess Cheat Engine",
            topic_id="10", topic_title="Example Game",
            source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
        )
        rows_from_page(broken)

    # Losing the description is not losing the table, and the counts say which
    # of the two happened.
    assert len(rows) == 1
    assert (issues.degraded, issues.failed) == (1, 1)
    assert any("description dropped" in sample for sample in issues.samples)
    assert any("page unreadable" in sample for sample in issues.samples)


def test_parse_issues_reach_the_log_and_the_provider_counters(tmp_path):
    logger, diagnostics = _RecordingLogger(), _RecordingDiagnostics()
    service = CatalogService(None, tmp_path / "results.json", diagnostics, logger)  # type: ignore[arg-type]

    degraded_only = ParseIssues()
    degraded_only.note(critical=False, reason="description dropped: version is too long")
    service._report_parse_issues("fearless", degraded_only, results=6)
    # Tables were still returned, so this is not the provider failing.
    assert [level for level, _ in logger.lines] == ["info"]
    assert "degraded_rows=1" in logger.lines[0][1]
    assert diagnostics.parse_issues == [("fearless", 1, 0)]

    unreadable = ParseIssues()
    unreadable.note(critical=True, reason="page unreadable: provider markup changed")
    service._report_parse_issues("playground", unreadable, results=0)
    assert logger.lines[1][0] == "warning"
    assert diagnostics.parse_issues[-1] == ("playground", 0, 1)

    # Nothing to say is said as nothing.
    service._report_parse_issues("fearless", ParseIssues(), results=6)
    assert len(logger.lines) == 2
    assert len(diagnostics.parse_issues) == 2


def _fearless_listing(
    *topics: tuple[str, str], page: int = 1, pages: int = 2,
    total: int = 51, page_step: int = 50,
    visible_pages: tuple[int, ...] | None = None,
) -> str:
    rows = "".join(
        f'<li class="row bg1"><dl class="row-item topic_read"><dt><a class="topictitle" '
        f'href="./viewtopic.php?f=4&amp;t={topic_id}&amp;sid=secret">{title}</a></dt></dl></li>'
        for topic_id, title in topics
    )
    visible = visible_pages if visible_pages is not None else tuple(range(1, pages + 1))
    pagination = []
    for page_number in visible:
        if page_number == page:
            pagination.append(f'<li class="active"><span>{page_number}</span></li>')
            continue
        suffix = "" if page_number == 1 else f"&amp;start={(page_number - 1) * page_step}"
        pagination.append(
            f'<li><a href="./viewforum.php?f=4{suffix}">{page_number}</a></li>'
        )
    return f"""
      <div class="pagination">{total} topics
        <span class="sr-only">Page {page} of {pages}</span>
        <ul>{''.join(pagination)}</ul>
      </div>
      <li class="row sticky"><a class="topictitle" href="./viewtopic.php?f=4&amp;t=20">Rules</a></li>
      <li class="row global-announce"><a class="topictitle" href="./viewtopic.php?f=21&amp;t=831">CE</a></li>
      {rows}
    """


def test_fearless_listing_parser_bounds_pages_and_excludes_policy_rows():
    listing = parse_fearless_forum_page(
        _fearless_listing(("123", "Example Game"), ("124", "Другой стол"))
    )
    assert listing.rows == [("123", "Example Game"), ("124", "Другой стол")]
    assert listing.current_page == 1
    assert listing.total_pages == 2
    assert listing.total_topics == 51


def test_how_big_a_listing_page_is_comes_from_the_listing():
    # Every stored offset is a multiple of this, so assuming it is the one way
    # to build an index with holes in it that still looks complete.
    assert parse_fearless_forum_page(_fearless_listing(("1", "A"))).page_step == 50
    # Sparse/ellipsis pagination remains exact because page four names the
    # relation 4 -> start 150, not merely an offset with no meaning attached.
    many = _fearless_listing(
        ("1", "A"), pages=4, total=151, visible_pages=(1, 2, 4),
    )
    assert parse_fearless_forum_page(many).page_step == 50
    # A lone visible third-page offset still says exactly what the step is.
    sparse = _fearless_listing(
        ("1", "A"), pages=3, total=101, visible_pages=(1, 3),
    )
    assert parse_fearless_forum_page(sparse).page_step == 50
    # The same offset behind a Next label is ambiguous and must not be guessed.
    ambiguous = sparse.replace(">3</a>", ">Next</a>")
    ambiguous_listing = parse_fearless_forum_page(ambiguous)
    assert ambiguous_listing.page_step is None
    assert ambiguous_listing.page_step_invalid is False
    inconsistent = many.replace("start=150", "start=100")
    inconsistent_listing = parse_fearless_forum_page(inconsistent)
    assert inconsistent_listing.page_step is None
    assert inconsistent_listing.page_step_invalid is True
    # A true one-page pagination has no viewforum links and needs no step.
    single = parse_fearless_forum_page(_fearless_listing(("1", "A"), pages=1, total=1))
    assert single.total_pages == 1
    assert single.page_step is None
    # An offset past what this indexes at all is not a page size.
    absurd = _fearless_listing(("1", "A")).replace("start=50", "start=999999999")
    assert parse_fearless_forum_page(absurd).page_step is None


def test_fearless_listing_parser_drops_unbounded_numeric_topic_identity():
    listing = parse_fearless_forum_page(
        _fearless_listing(("1" * 21, "Oversized identity"), ("123", "Example Game"))
    )
    assert listing.rows == [("123", "Example Game")]


def test_legacy_openct_attachment_and_password_hint():
    rows = parse_phpbb_attachments(
        (FIXTURES / "openct_legacy.html").read_text(), provider="opencheattables",
        display_name="Open Cheat Tables", topic_id="8", topic_title="Example Game",
        source_page="https://opencheattables.com/viewtopic.php?t=8", score=.8, rank=90,
        hosts=frozenset({"opencheattables.com"}),
    )
    assert rows[0].result.filename == "Example_pack.zip"
    assert rows[0].password_hint == "fixture-only"


def test_playground_public_sitemap_and_pg_file_metadata():
    entries = parse_playground_sitemap((FIXTURES / "playground_sitemap.xml").read_bytes())
    assert entries == [("123", "example game", "https://www.playground.ru/cheat/example-game-123")]
    rows, source = parse_playground_page((FIXTURES / "playground_item.html").read_text(encoding="utf-8"), entries[0][2], .95)
    assert rows[0].result.artifact_id == "page-123:file-77"
    assert rows[0].advertised_sha256 == "a" * 64
    assert rows[0].result.download_mode == "direct_https"
    assert rows[0].acquisition is not None
    assert rows[0].acquisition.file_id == "77"
    assert rows[0].acquisition.post_id == "456"
    assert rows[0].result.size_bytes == 1234
    assert rows[0].size_exact is False
    assert source == "https://github.com/example/tables"


def test_playground_artifact_allowlist_covers_only_observed_cdn_hosts():
    # Target evidence on 2026-08-27 resolved real anonymous downloads through
    # dl1 and dl3. A wildcard or guessed sibling remains outside authority.
    assert "dl1.gamedl.ru" in PLAYGROUND_ARTIFACT_HOSTS
    assert "dl3.gamedl.ru" in PLAYGROUND_ARTIFACT_HOSTS
    assert "dl2.gamedl.ru" not in PLAYGROUND_ARTIFACT_HOSTS


def test_playground_localized_size_metadata():
    html = '<h1>Игра</h1><pg-file data-file-id="7" data-name="Game.CT" data-size="11.95 Кб"></pg-file>'
    rows, _ = parse_playground_page(html, "https://www.playground.ru/game/cheat/game-123", .9)
    assert rows[0].result.size_bytes == 12_237


def test_github_exact_release_assets_and_digest():
    payload = json.loads((FIXTURES / "github_releases.json").read_text())
    rows = parse_github_releases(payload, "example/tables", .9)
    assert len(rows) == 1
    assert rows[0].result.artifact_id == "example/tables:asset-7"
    assert rows[0].advertised_sha256 == "b" * 64


def test_a_release_row_takes_its_identity_from_the_repository_not_a_url():
    """The repository is the identity, not something recovered from a page URL.

    Read back out of whichever URL the caller happened to have, a repository
    that declares a page somewhere else took its rows' `topic_id` and
    `artifact_id` from that URL, so the identity the panel selects by and the
    key the result list dedupes on stopped naming the repository the table is
    actually in.
    """
    payload = [{
        "id": 1, "draft": False, "tag_name": "v2", "name": "R",
        "published_at": "2026-08-01T00:00:00Z",
        "html_url": "https://elsewhere.example/release",
        "assets": [
            {"id": 7, "name": "Good.zip", "size": 5, "browser_download_url":
             "https://github.com/owner/repo/releases/download/v2/Good.zip"},
            {"id": 8, "name": "Elsewhere.zip", "size": 5, "browser_download_url":
             "https://elsewhere.example/Elsewhere.zip"},
        ],
    }]
    with collect_parse_issues() as issues:
        rows = parse_github_releases(payload, "owner/repo", .9)
    assert [row.result.artifact_id for row in rows] == ["owner/repo:asset-7"]
    assert rows[0].result.topic_id == "owner/repo"
    # A release page that is not this provider's page is not this row's source.
    assert rows[0].result.source_page == "https://github.com/owner/repo"
    # An asset the provider does not serve is dropped here rather than at the
    # transfer, where the host policy would refuse it after the user pressed.
    assert issues.degraded == 1


def test_catalog_cache_never_persists_direct_urls_or_passwords(tmp_path):
    path = tmp_path / "results.json"
    service = CatalogService(None, path)  # type: ignore[arg-type]
    row = ArtifactRecord(CatalogResult(
        provider="fearless", provider_display_name="FearLess Cheat Engine", topic_id="1",
        artifact_id="topic-1:attachment-2", table_title="Example", filename="Example.CT",
        version=None, size_bytes=5, source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
        download_mode="direct_https", match_score=.9, provider_rank=100, notes="Password: secret",
    ), direct_url="https://fearlessrevolution.com/download/file.php?id=2&token=secret", password_hint="secret")
    service._save_cache("Example", [row])
    raw = path.read_text(encoding="utf-8")
    assert "download/file.php" not in raw and "secret" not in raw
    cached = service._load_cache("Example")
    assert cached[0].direct_url is None
    assert cached[0].result.download_mode == "source_handoff"


def test_catalog_cache_never_persists_playground_post_identity(tmp_path):
    rows, _ = parse_playground_page(
        (FIXTURES / "playground_item.html").read_text(encoding="utf-8"),
        "https://www.playground.ru/cheat/example-game-123",
        .95,
    )
    path = tmp_path / "results.json"
    service = CatalogService(None, path)  # type: ignore[arg-type]
    service._save_cache("Example", rows)
    assert '"456"' not in path.read_text(encoding="utf-8")
    cached = service._load_cache("Example")
    assert cached[0].acquisition is None
    assert cached[0].result.download_mode == "source_handoff"


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class _SequenceNetwork:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    async def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.pop(0)


class _FearlessIndexNetwork:
    def __init__(self):
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "viewforum.php" in url:
            start = 50 if "start=50" in url else 0
            body = _fearless_listing(
                (("124", "Other Game") if start else ("123", "Example Game")),
                page=2 if start else 1,
            ).encode()
            return _Response(200, body)
        if "viewtopic.php" in url and "t=123" in url:
            return _Response(200, (FIXTURES / "fearless_current.html").read_bytes())
        raise AssertionError(f"unexpected URL: {url}")


class _SinglePageFearlessNetwork:
    def __init__(self):
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if "viewforum.php" in url:
            return _Response(200, _fearless_listing(
                ("123", "Example Game"), pages=1, total=1,
            ).encode())
        if "viewtopic.php" in url and "t=123" in url:
            return _Response(200, (FIXTURES / "fearless_current.html").read_bytes())
        raise AssertionError(f"unexpected URL: {url}")


@pytest.mark.asyncio
async def test_a_running_search_says_what_each_source_is_doing(tmp_path):
    # A search is tens of seconds of somebody else's network on a device with
    # no way to see any of it: one measured here took 45 seconds, of which
    # Playground was 44.8 and the other four sources had all answered inside
    # three, and the screen could say only how long it had been waiting.
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    assert service.search_progress("t1") is None

    _begin(service, ["fearless", "playground"], ["github"], token="t1")
    running = service.search_progress("t1")
    assert running is not None
    assert running["running"] is True
    assert running["elapsed_ms"] >= 0
    by_provider = {source["provider"]: source for source in running["sources"]}
    assert by_provider["fearless"]["state"] == "running"
    assert by_provider["fearless"]["name"] == "FearLess Cheat Engine"
    # A source the user switched off is named and said to be off, which is the
    # answer to "why is this one not in the list".
    assert by_provider["github"]["state"] == "off"
    assert by_provider["github"]["stage"] is None

    service._note_search_stage("fearless", "reading topic 3 of 40")
    assert _source(service, "fearless")["stage"] == "reading topic 3 of 40"

    service._finish_search_provider("playground", error=False)
    service._finish_search_provider("fearless", error=True)
    assert _source(service, "playground") == {
        "provider": "playground", "name": "Playground", "state": "done", "stage": None,
    }
    assert _source(service, "fearless")["state"] == "failed"
    # A finished source is not still doing something, whatever the last stage
    # written about it said.
    assert _source(service, "fearless")["stage"] is None

    service._end_search_progress()
    assert service.search_progress("t1")["running"] is False
    # Nothing that arrives after the search has ended can put it back.
    service._note_search_stage("fearless", "reading topic 4 of 40")
    assert _source(service, "fearless")["stage"] is None
    await service.close()


def _begin(service, jobs: list[str], skipped: list[str], *, token: str) -> None:
    """Start a progress record the way a search does, named by its own caller."""
    from ce_decky import catalog

    catalog._SEARCH_TOKEN.set(token)
    service._begin_search_progress(jobs, skipped)


def _source(service, provider: str, token: str = "t1") -> dict:
    progress = service.search_progress(token)
    assert progress is not None
    return next(source for source in progress["sources"] if source["provider"] == provider)


@pytest.mark.asyncio
async def test_an_older_search_cannot_write_into_the_record_that_replaced_it(tmp_path):
    # A stage note is written from inside a provider job, several frames below
    # the search that started it, and nothing in the backend forbids two
    # searches overlapping. Keyed by provider alone, the job of a search the
    # user had moved on from went on describing the newer one, and finishing
    # that job marked the newer search's source done while it was still going.
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]

    async def older():
        _begin(service, ["fearless"], [], token="older")
        # Whatever this one does from here belongs to a search that is gone.
        await asyncio.sleep(0)
        service._note_search_stage("fearless", "reading topic 1 of 40")
        service._finish_search_provider("fearless", error=True)
        service._end_search_progress()

    async def newer():
        _begin(service, ["fearless"], [], token="newer")
        service._note_search_stage("fearless", "reading the forum listing")

    # Each runs in its own task, so each carries its own search number exactly
    # as a provider job does.
    first = asyncio.create_task(older())
    await asyncio.sleep(0)
    await asyncio.create_task(newer())
    await first

    progress = service.search_progress("newer")
    assert progress is not None
    assert progress["running"] is True
    assert progress["sources"] == [{
        "provider": "fearless", "name": "FearLess Cheat Engine",
        "state": "running", "stage": "reading the forum listing",
    }]
    await service.close()


@pytest.mark.asyncio
async def test_a_source_that_runs_out_of_budget_keeps_what_it_already_found(tmp_path):
    """Forty seconds of real pages is an answer, not something to throw away.

    A provider builds its rows as it goes and the deadline cancelled it, so
    everything it had went with the coroutine and the source was reported as
    having answered nothing. Reporting a source that stopped part way through
    while keeping its rows is a state this already has: it is what a provider
    that breaks out of its own page loop on a rate limit reports.
    """
    from ce_decky.catalog import collect_provider_rows, _provider_rows

    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    found = ArtifactRecord(CatalogResult(
        provider="fearless", provider_display_name="FearLess Cheat Engine", topic_id="topic-1",
        artifact_id="topic-1:attachment-1", table_title="Found before the deadline",
        filename="Found.CT", version=None, size_bytes=None,
        source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
        download_mode="direct_https", match_score=0.9,
    ))

    async def slow_provider():
        _provider_rows().append(found)
        await asyncio.sleep(30)
        raise AssertionError("the deadline should have stopped this")

    with collect_provider_rows() as collected:
        try:
            await asyncio.wait_for(slow_provider(), 0.01)
        except asyncio.TimeoutError:
            pass
    # The list the search reads is the one the provider was filling.
    assert collected == [found]
    await service.close()


SITEMAP_XML = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    b'<url><loc>https://www.playground.ru/cheat/quite-another-title-999</loc></url>'
    b'</urlset>'
)


# Old enough that a search has reason to ask whether a newer index exists.
_OLDER_THAN_TTL = catalog_module.PLAYGROUND_INDEX_TTL_SECONDS + 3600


def _playground_cache(tmp_path: Path, entries: list, *, age: float = _OLDER_THAN_TTL) -> Path:
    path = tmp_path / "playground-sitemap.json"
    path.write_text(json.dumps({
        "schema": 1, "etag": 'W/"cached"', "last_modified": "Sat, 05 Sep 2026 15:00:26 GMT",
        "retrieved": time.time() - age, "entries": entries,
    }), encoding="utf-8")
    return path


class _SlowSitemapNetwork:
    """A site index that never answers, and a device that already has one."""

    def __init__(self):
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        if "sitemap" in url:
            await asyncio.sleep(30)
        raise AssertionError(f"unexpected URL: {url}")


class _RefusingNetwork:
    """Any request at all is the failure this is testing for."""

    async def get(self, url, **kwargs):
        raise AssertionError(f"nothing should have been asked: {url}")


@pytest.mark.asyncio
async def test_a_recent_site_index_is_not_asked_about_at_all(tmp_path):
    # Asking on every search is a round trip for an answer already known, and
    # while the site is slow it is the whole of the short refresh window every
    # time. The entries are cheat pages, so what an out-of-date copy costs is a
    # game that had no cheat page when it was written.
    service = CatalogService(_RefusingNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    _playground_cache(tmp_path, [["1", "nothing", "https://www.playground.ru/cheat/nothing-1"]], age=60)

    assert await service._playground(["example game"]) == []
    await service.close()


@pytest.mark.asyncio
async def test_an_index_older_than_the_window_is_asked_about(tmp_path):
    network = _SequenceNetwork([_Response(304, b"", {"etag": 'W/"cached"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    _playground_cache(tmp_path, [["1", "nothing", "https://www.playground.ru/cheat/nothing-1"]])

    assert await service._playground(["example game"]) == []
    assert len(network.calls) == 1
    await service.close()


@pytest.mark.asyncio
async def test_being_told_the_copy_is_current_stops_this_process_asking_again(tmp_path):
    # A not-modified proves the copy on disk is current. Rewriting eleven
    # megabytes to record that would cost more than the request did, so it is
    # remembered for this process instead: without it, an index past the window
    # that never changes upstream is asked about on every search forever.
    network = _SequenceNetwork([_Response(304, b"", {"etag": 'W/"cached"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    _playground_cache(tmp_path, [["1", "nothing", "https://www.playground.ru/cheat/nothing-1"]])

    await service._playground(["example game"])
    assert len(network.calls) == 1

    # The second search asks nothing, and there is no second response to give.
    await service._playground(["example game"])
    assert len(network.calls) == 1
    await service.close()


def test_a_timestamp_that_cannot_be_trusted_is_treated_as_old():
    # A cache written by something that recorded none, and a clock that has
    # moved, are both reasons to ask rather than to stop refreshing for good.
    from ce_decky.catalog import _index_within_ttl

    assert _index_within_ttl(time.time() - 60, 3600) is True
    assert _index_within_ttl(time.time() - 7200, 3600) is False
    assert _index_within_ttl(time.time() + 7200, 3600) is False
    for rejected in (None, "recently", True, [], {}):
        assert _index_within_ttl(rejected, 3600) is False


@pytest.mark.asyncio
async def test_a_site_index_this_device_has_does_not_hold_up_a_search(tmp_path, monkeypatch):
    # Measured on the development device: 11.9 MB across 50000 games, 37 of one
    # search's 45 seconds, because the copy upstream had changed eleven minutes
    # earlier so the conditional request came back 200 rather than 304. The same
    # file loads from disk in 30 milliseconds.
    monkeypatch.setattr(catalog_module, "PROVIDER_INDEX_REFRESH_SECONDS", 0.02)
    network = _SlowSitemapNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    _playground_cache(tmp_path, [["1", "nothing like the game", "https://www.playground.ru/cheat/nothing-1"]])

    started = time.monotonic()
    rows = await service._playground(["example game"])
    elapsed = time.monotonic() - started

    assert rows == []
    assert elapsed < 5.0
    # And the whole of it is fetched behind the search it would have held up.
    assert service._playground_index_task is not None
    await service.close()


@pytest.mark.asyncio
async def test_a_background_refresh_teaches_the_provider_what_it_was_told(tmp_path):
    # A refusal is the provider speaking about itself, and it says the same
    # thing whether somebody was waiting for the request or not. Collected in
    # the background and dropped, it left the next search to walk straight into
    # the provider that had just said no.
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    network = _SequenceNetwork([_Response(429, b"", {"retry-after": "600"})])
    service = CatalogService(network, tmp_path / "results.json", store)  # type: ignore[arg-type]

    await service._refresh_playground_sitemap({})

    playground = store.snapshot()["providers"]["playground"]
    assert playground["last_http_status"] == 429
    assert playground["cooldown_until_epoch_s"] > time.time()
    await service.close()


@pytest.mark.asyncio
async def test_a_background_refresh_that_lands_after_the_switch_writes_nothing(tmp_path):
    # A source that is off is a source this device does not contact, and an
    # answer that arrives after the switch is not permission to keep its index.
    network = _SequenceNetwork([_Response(200, SITEMAP_XML, {"etag": 'W/"new"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    service._disabled_providers = lambda: ["playground"]  # type: ignore[assignment]

    await service._refresh_playground_sitemap({})

    assert not (tmp_path / "playground-sitemap.json").exists()
    await service.close()


@pytest.mark.asyncio
async def test_a_refresh_that_cannot_be_read_leaves_the_answer_already_held(tmp_path):
    # The copy on disk was usable before the request. A refresh that comes back
    # malformed does not make it less so, and letting the parser escape marked
    # the whole source failed for a search that had its answer in hand.
    network = _SequenceNetwork([_Response(200, b"<not-a-sitemap/>", {"etag": 'W/"new"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    cache = _playground_cache(tmp_path, [["1", "nothing", "https://www.playground.ru/cheat/nothing-1"]])
    before = cache.read_text(encoding="utf-8")

    # No exception reaches the search: it went on with the copy it had.
    assert await service._playground(["example game"]) == []
    assert cache.read_text(encoding="utf-8") == before
    assert service._playground_index_checked_at is None
    await service.close()


@pytest.mark.asyncio
async def test_a_refresh_that_cannot_be_written_still_answers_this_search(tmp_path, monkeypatch):
    network = _SequenceNetwork([_Response(200, SITEMAP_XML, {"etag": 'W/"new"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    _playground_cache(tmp_path, [["1", "nothing", "https://www.playground.ru/cheat/nothing-1"]])

    def refuse(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(catalog_module, "atomic_write_json", refuse)

    assert await service._playground(["example game"]) == []
    # Read but not kept, so the next search asks again rather than believing a
    # copy it never managed to write.
    assert service._playground_index_checked_at is None
    await service.close()


@pytest.mark.asyncio
async def test_a_server_error_does_not_pass_for_a_freshness_check(tmp_path):
    # Only a not-modified proves the copy is current. Counting a 500 hid a
    # changed index behind a server that was briefly unwell, for six hours.
    network = _SequenceNetwork([_Response(500), _Response(500)])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    _playground_cache(tmp_path, [["1", "nothing", "https://www.playground.ru/cheat/nothing-1"]])

    assert await service._playground(["example game"]) == []
    assert service._playground_index_checked_at is None

    # Still due for another look on the very next search.
    assert await service._playground(["example game"]) == []
    assert len(network.calls) == 2
    await service.close()


@pytest.mark.asyncio
async def test_a_site_index_that_has_not_changed_leaves_the_copy_on_disk(tmp_path):
    network = _SequenceNetwork([_Response(304, b"", {"etag": 'W/"cached"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    cache = _playground_cache(tmp_path, [["1", "nothing like the game", "https://www.playground.ru/cheat/nothing-1"]])
    before = cache.read_text(encoding="utf-8")

    assert await service._playground(["example game"]) == []

    # A conditional request was made, it was answered immediately, and nothing
    # about the copy this device answers from changed.
    assert [kwargs["headers"]["If-None-Match"] for _, kwargs in network.calls] == ['W/"cached"']
    assert cache.read_text(encoding="utf-8") == before
    assert service._playground_index_task is None
    await service.close()


@pytest.mark.asyncio
async def test_a_fresh_site_index_that_arrives_in_time_is_the_one_used(tmp_path):
    network = _SequenceNetwork([_Response(200, SITEMAP_XML, {"etag": 'W/"new"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    cache = _playground_cache(tmp_path, [["1", "nothing like the game", "https://www.playground.ru/cheat/nothing-1"]])

    await service._playground(["example game"])

    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["etag"] == 'W/"new"'
    assert saved["entries"] == [["999", "quite another title", "https://www.playground.ru/cheat/quite-another-title-999"]]
    assert service._playground_index_task is None
    await service.close()


@pytest.mark.asyncio
async def test_a_device_with_no_site_index_still_waits_for_one(tmp_path, monkeypatch):
    # There is nothing to answer from, so this request is the search rather
    # than an attempt to improve on what is already here.
    monkeypatch.setattr(catalog_module, "PROVIDER_INDEX_REFRESH_SECONDS", 0.02)
    network = _SequenceNetwork([_Response(200, SITEMAP_XML, {"etag": 'W/"first"'})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    await service._playground(["example game"])

    saved = json.loads((tmp_path / "playground-sitemap.json").read_text(encoding="utf-8"))
    assert saved["etag"] == 'W/"first"'
    assert service._playground_index_task is None
    await service.close()


@pytest.mark.asyncio
async def test_the_fallback_pass_is_not_started_on_a_sliver_of_the_budget(tmp_path):
    # The second pass is a whole set of provider jobs sharing what is left of
    # the one budget. Started with almost nothing left, every one of them times
    # out at once and the user is shown a screen of failed sources where the
    # first pass had simply found nothing.
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    passes: list[list[str]] = []

    async def collect(aliases, queries, *, deadline=None, disabled=None):
        passes.append(list(aliases))
        return [], []

    service._collect = collect  # type: ignore[assignment]
    executable = '"/home/deck/Games/SkyForge7/bin/SkyForge7.exe"'

    await service._search_with(
        ["Entry Name"], ["Entry Name"], "Entry Name", executable,
        time.monotonic() + 0.05, frozenset(),
    )
    assert len(passes) == 1

    # With a budget worth having, it runs exactly as it did.
    passes.clear()
    await service._search_with(
        ["Entry Name"], ["Entry Name"], "Entry Name", executable,
        time.monotonic() + catalog_module.PROVIDER_SEARCH_BUDGET_SECONDS, frozenset(),
    )
    assert len(passes) == 2
    await service.close()


@pytest.mark.asyncio
async def test_a_search_delivers_the_rows_a_timed_out_source_had_already_read(tmp_path):
    # End to end through the fan-out: the source is reported as having stopped
    # part way through, and its pages are in the answer rather than discarded.
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    found = ArtifactRecord(CatalogResult(
        provider="fearless", provider_display_name="FearLess Cheat Engine", topic_id="topic-1",
        artifact_id="topic-1:attachment-1", table_title="Read before the deadline",
        filename="Read.CT", version=None, size_bytes=None,
        source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
        download_mode="direct_https", match_score=0.9,
    ))

    async def slow_fearless(_aliases):
        catalog_module._provider_rows().append(found)
        await asyncio.sleep(30)
        raise AssertionError("the deadline should have stopped this")

    service._fearless = slow_fearless  # type: ignore[assignment]
    others = frozenset({"playground", "thecheatscript", "github", "vgtimes"})

    records, failures = await service._collect(
        ["example game"], ["example game"],
        deadline=time.monotonic() + 0.05, disabled=others,
    )

    assert [row.result.artifact_id for row in records] == ["topic-1:attachment-1"]
    assert [failure["provider"] for failure in failures] == ["fearless"]
    assert "search budget" in str(failures[0]["error"])
    await service.close()


@pytest.mark.asyncio
async def test_a_provider_outside_a_search_still_owns_its_own_rows(tmp_path):
    # Every focused test calls a provider directly, with nothing collecting.
    from ce_decky.catalog import _provider_rows

    first = _provider_rows()
    first.append("a")
    assert _provider_rows() == []


def test_a_search_token_is_bounded_and_constrained_like_any_other_argument():
    # It arrives from the frontend, so it is checked there rather than trusted
    # for being ours. It names nothing and authorizes nothing; it only has to be
    # a bounded token this can compare.
    from ce_decky.service import _search_token

    assert _search_token(None) is None
    assert _search_token("s1a2b3c4") == "s1a2b3c4"
    for rejected in ("", "a" * 65, "has space", "../etc", "tab\there", 7, b"bytes", ["s1"]):
        with pytest.raises(ValueError, match="search progress token is malformed"):
            _search_token(rejected)


@pytest.mark.asyncio
async def test_a_progress_record_is_answered_only_to_the_search_that_made_it(tmp_path):
    # Nothing here forbids two searches at once, and one panel serializing its
    # own presses is not that guarantee. With one record and no name on it, the
    # caller of A was shown B's sources, and B finishing blanked the line of A
    # while A was still running.
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]

    async def search_a():
        _begin(service, ["fearless"], [], token="a")
        service._note_search_stage("fearless", "reading the forum listing")
        # Held open while B runs from start to finish.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def search_b():
        _begin(service, ["playground"], [], token="b")
        service._finish_search_provider("playground", error=False)
        service._end_search_progress()

    first = asyncio.create_task(search_a())
    await asyncio.sleep(0)
    await asyncio.create_task(search_b())
    await first

    # B's caller sees B, and only B.
    b_progress = service.search_progress("b")
    assert b_progress is not None
    assert [source["provider"] for source in b_progress["sources"]] == ["playground"]
    # A's caller is told nothing rather than told about B: the record it would
    # have been shown is not about its search, and saying so is the only honest
    # answer this can give.
    assert service.search_progress("a") is None
    # An unnamed search cannot be read at all.
    assert service.search_progress("") is None
    await service.close()


@pytest.mark.asyncio
async def test_a_search_that_ends_leaves_no_source_reported_as_working(tmp_path):
    # Whatever a provider does, the record it leaves behind must not say the
    # screen is still waiting on it: that line is the only thing the user has.
    network = _SinglePageFearlessNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    await service.search("Example Game", progress_token="own")

    progress = service.search_progress("own")
    assert progress is not None
    assert progress["running"] is False
    assert [source for source in progress["sources"] if source["state"] == "running"] == []
    # And it is answered to its own caller only.
    assert service.search_progress("somebody-else") is None
    await service.close()


@pytest.mark.asyncio
async def test_ambiguous_first_fearless_page_cannot_establish_index_geometry(tmp_path):
    html = _fearless_listing(
        ("123", "Example Game"), pages=3, total=101, visible_pages=(1, 3),
    ).replace(">3</a>", ">Next</a>")
    network = _SequenceNetwork([_Response(200, html.encode())])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="no unambiguous page size"):
        await service._refresh_fearless_page(0)
    assert service._fearless_pages == {}
    assert service._fearless_page_step is None
    await service.close()


@pytest.mark.asyncio
async def test_single_page_fearless_index_is_searchable_persisted_and_reloaded(tmp_path):
    network = _SinglePageFearlessNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    rows = await service._fearless(["example game"])

    assert [row.result.artifact_id for row in rows] == ["topic-123:attachment-42"]
    assert service._fearless_page_step is None
    assert sorted(service._fearless_pages) == [0]
    assert service._fearless_index_task is None
    await service.close()

    payload = json.loads((tmp_path / "fearless-index.json").read_text(encoding="utf-8"))
    assert payload["total_pages"] == 1
    assert payload["page_step"] is None
    reloaded = CatalogService(_SinglePageFearlessNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    assert reloaded._fearless_page_step is None
    assert sorted(reloaded._fearless_pages) == [0]
    assert reloaded.fearless_index_status()["status"] == "ok"
    await reloaded.close()


@pytest.mark.asyncio
async def test_fearless_index_builds_in_background_persists_and_refreshes_leading_pages(tmp_path, unpaced_crawl):
    network = _FearlessIndexNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    rows = await service._fearless(["example game"])
    assert [row.result.artifact_id for row in rows] == ["topic-123:attachment-42"]
    first_status = service.fearless_index_status()
    assert first_status["status"] == "indexing"
    assert first_status["indexed_pages"] == 1
    assert first_status["total_pages"] == 2

    assert service._fearless_index_task is not None
    await service._fearless_index_task
    status = service.fearless_index_status()
    assert {key: status[key] for key in (
        "status", "error", "indexed_pages", "total_pages", "indexed_topics", "retry_after_seconds",
        "stale_pages", "last_refresh_pages", "refresh_age_seconds",
    )} == {
        "status": "ok", "error": None, "indexed_pages": 2,
        "total_pages": 2, "indexed_topics": 2, "retry_after_seconds": None,
        # The search had already read the leading page, so the background pass
        # owed exactly the one page that was missing.
        "stale_pages": 0, "last_refresh_pages": 1, "refresh_age_seconds": 24 * 60 * 60,
    }
    assert status["fully_refreshed_at"] is not None
    assert sorted(service._fearless_pages) == [0, 50]
    assert (tmp_path / "fearless-index.json").is_file()

    # A later search refreshes both leading pages while reusing the complete
    # bounded index; it never touches the challenged search.php route.
    before = len(network.calls)
    await service._fearless(["missing game"])
    refreshed = [url for url, _ in network.calls[before:] if "viewforum.php" in url]
    assert refreshed == [
        "https://fearlessrevolution.com/viewforum.php?f=4",
        "https://fearlessrevolution.com/viewforum.php?f=4&start=50",
    ]
    assert all("search.php" not in url for url, _ in network.calls)
    # A fresh index is not rebuilt: the background pass owes nothing and the
    # search's own leading-page reads are the only requests.
    assert service._fearless_index_task is None or service._fearless_index_task.done()
    assert service.fearless_index_status()["stale_pages"] == 0
    await service.close()


@pytest.mark.asyncio
async def test_nonzero_fearless_page_rejects_explicit_stride_contradiction(tmp_path):
    first = _fearless_listing(("123", "Example Game")).encode()
    contradictory = _fearless_listing(
        ("124", "Other Game"), page=2, pages=3, total=51,
        page_step=25, visible_pages=(1, 2, 3),
    ).encode()
    service = CatalogService(
        _SequenceNetwork([_Response(200, first), _Response(200, contradictory)]),
        tmp_path / "results.json",  # type: ignore[arg-type]
    )

    await service._refresh_fearless_page(0)
    with pytest.raises(ValueError, match="contradicts the current index"):
        await service._refresh_fearless_page(50)
    assert service._fearless_page_step == 50
    assert sorted(service._fearless_pages) == [0]
    await service.close()


class _DeepFearlessNetwork:
    """A listing deeper than the leading pages a search refreshes by itself."""

    def __init__(self, pages: int = 4):
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "viewforum.php" in url:
            start = int(url.rsplit("start=", 1)[1]) if "start=" in url else 0
            number = start // 50 + 1
            body = _fearless_listing(
                (str(100 + number), f"Indexed Topic {number}"),
                page=number, pages=self.pages, total=self.pages * 50,
            ).encode()
            return _Response(200, body)
        raise AssertionError(f"unexpected URL: {url}")

    def listings(self, since: int = 0) -> list[int]:
        return [
            int(url.rsplit("start=", 1)[1]) if "start=" in url else 0
            for url, _ in self.calls[since:] if "viewforum.php" in url
        ]


@pytest.mark.asyncio
async def test_a_background_pass_refreshes_a_bounded_number_of_pages(tmp_path, monkeypatch):
    # Per-page freshness comes due for most of the index at once, so a pass that
    # walks whatever is pending walks nearly the whole forum. Measured here, one
    # began with 247 of 332 pages pending and was rate limited three seconds in,
    # and that rate limit is the provider's: it stopped the user's searches too.
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_PAGES_PER_RUN", 2)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_SECONDS", 0.0)
    network = _DeepFearlessNetwork(pages=8)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task

    # Page 0 is the search's own, the two after it are the whole of the pass,
    # and five of the eight pages are left for a later one.
    assert network.listings() == [0, 50, 100]
    assert service.fearless_index_status()["stale_pages"] == 5

    # And the next search does not simply start it again, which is what made a
    # pass that stops short indistinguishable from one that never stopped.
    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task.done()
    await service.close()


@pytest.mark.asyncio
async def test_a_background_pass_gives_up_after_being_told_to_wait_twice(tmp_path, monkeypatch):
    """A refusal that is retried forever holds the user's searches down with it.

    The retry was unbounded: a provider answering every request with 429 kept
    one page alive indefinitely, and every refusal re-armed the cooldown that
    stops the interactive search as well.
    """
    class _RateLimited:
        def __init__(self):
            self.calls = 0

        async def get(self, url, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                return _Response(200, _fearless_listing(
                    ("123", "Indexed Topic"), page=self.calls, pages=8, total=400,
                ).encode())
            return _Response(429, b"", {"retry-after": "0"})

    # The provider names no wait, so the default applies; nothing here is about
    # how long that is.
    monkeypatch.setattr(catalog_module, "FEARLESS_DEFAULT_COOLDOWN_SECONDS", 0)
    network = _RateLimited()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    await asyncio.wait_for(service._fearless_index_task, 5)

    # It stopped rather than sitting out cooldown after cooldown on one page.
    assert network.calls <= 2 + catalog_module.FEARLESS_INDEX_RATE_LIMITS_PER_RUN
    await service.close()


@pytest.mark.asyncio
async def test_a_background_pass_waits_for_the_search_to_finish(tmp_path, monkeypatch):
    # The provider has one budget of requests and the user is watching a screen
    # for one of them. This crawl is not.
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_SECONDS", 30.0)
    network = _DeepFearlessNetwork(pages=8)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    before = len(network.listings())

    # A search has just finished, so the pass asks for nothing yet.
    service._last_search_at = time.monotonic()
    await asyncio.sleep(0.05)
    assert len(network.listings()) == before
    assert not service._fearless_index_task.done()

    # And a search running now holds it just the same.
    service._last_search_at = time.monotonic() - 3600
    service._search_tasks.add(asyncio.current_task())  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    assert len(network.listings()) == before

    service._search_tasks.discard(asyncio.current_task())  # type: ignore[arg-type]
    service._fearless_index_task.cancel()
    await asyncio.gather(service._fearless_index_task, return_exceptions=True)
    await service.close()


@pytest.mark.asyncio
async def test_a_pass_asleep_on_a_cooldown_does_not_wake_into_a_search(tmp_path, monkeypatch):
    # Both sleeps in the retry loop are long enough for a search to have started
    # underneath one. Waking straight into the request is how this crawl went on
    # competing for the provider with the search the user is watching.
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_SECONDS", 30.0)
    monkeypatch.setattr(catalog_module, "FEARLESS_DEFAULT_COOLDOWN_SECONDS", 0)
    network = _DeepFearlessNetwork(pages=8)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    before = len(network.listings())
    # Asleep on a cooldown, and a search starts while it is.
    service._fearless_cooldown_until = time.time() + 0.05
    service._last_search_at = time.monotonic()

    await asyncio.sleep(0.3)

    # The cooldown is over and the pass has woken, and it has still asked for
    # nothing, because the search has not finished being quiet.
    assert service._fearless_cooldown_remaining() == 0
    assert len(network.listings()) == before

    service._fearless_index_task.cancel()
    await asyncio.gather(service._fearless_index_task, return_exceptions=True)
    await service.close()


@pytest.mark.asyncio
async def test_giving_way_after_a_cooldown_ends_the_pass_too(tmp_path, monkeypatch):
    # The give-up decision was made inside the retry loop and only broke that,
    # so the outer loop began the whole allowance again: a pass waking from a
    # cooldown into searches that never stop gave way for its limit, came back,
    # and gave way for the limit again, without end.
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_SECONDS", 30.0)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_WAIT_LIMIT_SECONDS", 0.2)
    network = _DeepFearlessNetwork(pages=8)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    waits: list[bool] = []
    original = service._wait_for_quiet_searches

    async def counted():
        answer = await original()
        waits.append(answer)
        return answer

    service._wait_for_quiet_searches = counted  # type: ignore[assignment]

    # The cooldown is set before the pass has run at all, so it waits once
    # while searches are quiet and then goes to sleep on the cooldown.
    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    service._fearless_cooldown_until = time.time() + 0.15
    await asyncio.sleep(0.05)

    # A search starts while it sleeps, which is the case this is about.
    service._last_search_at = time.monotonic()
    await asyncio.wait_for(service._fearless_index_task, 5)

    # The pass waited once before choosing a page and once before asking for
    # it, and the second answer ended the pass. A third wait is the decision
    # being handed back to the loop that starts the allowance again.
    assert waits == [True, False]
    await service.close()


@pytest.mark.asyncio
async def test_switching_a_source_off_cancels_the_request_it_has_out(tmp_path):
    # Checking the switch before starting and again before writing does not
    # stop a request already in flight: a user who switches a source off is
    # entitled to have this device stop talking to it now.
    released = asyncio.Event()

    class _Blocked:
        async def get(self, url, **kwargs):
            await released.wait()
            return _Response(200, SITEMAP_XML, {"etag": 'W/"new"'})

    service = CatalogService(_Blocked(), tmp_path / "results.json")  # type: ignore[arg-type]
    service._start_playground_sitemap({})
    task = service._playground_index_task
    assert task is not None
    await asyncio.sleep(0)
    assert not task.done()

    service.stop_provider_background_work("playground")

    released.set()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not (tmp_path / "playground-sitemap.json").exists()
    await service.close()


@pytest.mark.asyncio
async def test_a_search_that_succeeded_anyway_does_not_erase_the_refusal(tmp_path):
    """One search makes several requests, and a provider can refuse one of them.

    The site index refused with 429 while the game's own pages were served is
    exactly that, and the search then answered from the index this device
    already had. Recording that success cleared the deadline the refusal had
    just written, so the next search walked straight back into it.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure(
        "playground", error="HTTP 429 provider cooldown", http_status=429,
        retry_after="600", background=True,
    )
    deadline = store.snapshot()["providers"]["playground"]["cooldown_until_epoch_s"]
    assert deadline > time.time()

    store.record_search_success("playground", results=3, latency_ms=1200.0, http_status=200)

    playground = store.snapshot()["providers"]["playground"]
    assert playground["cooldown_until_epoch_s"] == deadline
    assert playground["last_error"] == "HTTP 429 provider cooldown"
    assert store.can_attempt("playground") is False
    # Keeping the index current is not a search of the provider, and the screen
    # names that counter "searches": counting it made one press appear as two.
    assert playground["counters"]["searches"] == 1
    assert playground["counters"]["errors"] == 1


def test_a_challenge_the_search_did_not_see_survives_its_success(tmp_path):
    # The sibling of the rate-limit case, and the harder half: a challenge
    # carries no deadline, so there is nothing with an end written on it to
    # keep. What it has is when it happened, and background maintenance
    # recorded it after this search began, so the search knows nothing about
    # the request it refused.
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure(
        "playground", error="provider answered HTTP 403 for the site index",
        http_status=403, background=True,
    )

    # A search that has been running for a second, so the refusal above landed
    # while it ran.
    store.record_search_success("playground", results=3, latency_ms=1000.0, http_status=200)

    playground = store.snapshot()["providers"]["playground"]
    assert playground["last_error"] == "provider answered HTTP 403 for the site index"
    assert playground["last_http_status"] == 403
    # It blocks nothing, because a challenge names no deadline and the source
    # did answer this search.
    assert store.can_attempt("playground") is True
    assert playground["counters"]["searches"] == 1


@pytest.mark.asyncio
async def test_another_provider_holding_the_fan_out_open_cannot_age_out_a_refusal(tmp_path):
    """When a job began is the job's own fact, not the fan-out's.

    Every provider runs at once and the diagnostics are written after all of
    them have finished. Reconstructing when one began from how long it took
    gives the moment the whole fan-out ended minus that duration, and those
    agree only when that job was the last to finish. One slow source holding
    the others open therefore moved the reconstructed start past a refusal that
    really had landed while the job was running, and the success cleared it.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    service = CatalogService(None, tmp_path / "results.json", store)  # type: ignore[arg-type]
    released = asyncio.Event()

    async def quick_playground(_aliases):
        # The refusal lands while this job is running, from work it started.
        store.record_failure(
            "playground", error="provider answered HTTP 403 for the site index",
            http_status=403, background=True,
        )
        return []

    async def slow_fearless(_aliases):
        await released.wait()
        return []

    service._playground = quick_playground  # type: ignore[assignment]
    service._fearless = slow_fearless  # type: ignore[assignment]
    others = frozenset({"thecheatscript", "github", "vgtimes"})

    collecting = asyncio.create_task(service._collect(
        ["example game"], ["example game"],
        deadline=time.monotonic() + 30, disabled=others,
    ))
    # Long enough that a start reconstructed from this job's own duration would
    # land after the refusal above.
    await asyncio.sleep(0.3)
    released.set()
    await collecting

    playground = store.snapshot()["providers"]["playground"]
    assert playground["last_error"] == "provider answered HTTP 403 for the site index"
    assert playground["last_http_status"] == 403
    await service.close()


def test_a_refusal_older_than_the_search_is_cleared_by_its_success(tmp_path):
    # The other side of the same rule: a challenge from before this search began
    # is something the search has now overtaken, so its success speaks for it.
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure(
        "playground", error="provider answered HTTP 403 for the site index",
        http_status=403, background=True, now=time.time() - 3600,
    )

    store.record_search_success("playground", results=3, latency_ms=1000.0, http_status=200)

    playground = store.snapshot()["providers"]["playground"]
    assert playground["last_error"] is None
    assert playground["last_http_status"] == 200
    assert playground["background_refusal_at"] is None


@pytest.mark.asyncio
async def test_a_deadline_that_has_passed_is_cleared_by_the_next_success(tmp_path):
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure(
        "playground", error="HTTP 429 provider cooldown", http_status=429,
        retry_after="1", now=time.time() - 3600,
    )
    assert store.can_attempt("playground") is True

    store.record_search_success("playground", results=3, latency_ms=1200.0, http_status=200)

    playground = store.snapshot()["providers"]["playground"]
    assert playground["cooldown_until_epoch_s"] == 0.0
    assert playground["last_error"] is None


@pytest.mark.asyncio
async def test_giving_way_to_searches_has_an_end(tmp_path, monkeypatch):
    # Giving way has no natural end: somebody searching steadily holds the pass
    # for as long as they keep doing it, and a pass that waits forever is a task
    # that never finishes. It drops itself, and the pacing decides when another
    # may start.
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_SECONDS", 30.0)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_WAIT_LIMIT_SECONDS", 0.0)
    network = _DeepFearlessNetwork(pages=8)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    service._last_search_at = time.monotonic()
    await asyncio.wait_for(service._fearless_index_task, 5)

    # The search's own page and nothing from the pass.
    assert network.listings() == [0]
    await service.close()


@pytest.mark.asyncio
async def test_deleting_the_index_does_not_leave_the_rebuild_waiting_for_the_pacing(tmp_path, unpaced_crawl):
    # Passes are spaced half an hour apart so one that stops short is not
    # restarted by every search. After the index has been deleted there is
    # nothing to space: the next search is what has to build it again.
    network = _DeepFearlessNetwork(pages=4)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["nothing matches this"])
    assert service._fearless_index_ran_at is not None

    await service.discard_cached_index()

    assert service._fearless_index_ran_at is None
    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    await service.close()


@pytest.mark.asyncio
async def test_fearless_index_refreshes_only_the_pages_that_aged_out(tmp_path, unpaced_crawl):
    """A day-old page is re-read; every page still inside the day is not.

    Freshness is tracked per page precisely so the daily refresh cannot turn
    into a full rebuild on the next search.
    """
    network = _DeepFearlessNetwork(pages=4)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    status = service.fearless_index_status()
    assert status["indexed_pages"] == 4 and status["stale_pages"] == 0
    assert status["last_refresh_pages"] == 3

    # Age one page the search's own leading refresh does not cover.
    aged = time.time() - (24 * 60 * 60 + 60)
    service._fearless_fetched[150] = aged
    assert service.fearless_index_status()["stale_pages"] == 1
    assert service.fearless_index_status()["fully_refreshed_at"] == aged

    before = len(network.calls)
    # As if the interval between background passes had elapsed: what this is
    # about is which pages a pass reads, not how often one may start.
    service._fearless_index_ran_at = None
    await service._fearless(["nothing matches this"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    # Pages 0 and 50 are the search's own leading refresh; 150 is the one the
    # background pass owed. Page 100 was still fresh and is never requested.
    assert network.listings(before) == [0, 50, 150]
    after = service.fearless_index_status()
    assert after["last_refresh_pages"] == 1
    assert after["stale_pages"] == 0
    assert after["fully_refreshed_at"] > aged
    await service.close()


@pytest.mark.asyncio
async def test_fearless_index_freshness_survives_a_cache_written_without_it(tmp_path, unpaced_crawl):
    """An index cached before per-page freshness existed is due, not discarded."""
    network = _FearlessIndexNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["example game"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    await service.close()

    cache = tmp_path / "fearless-index.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["fetched"]
    payload.pop("fetched")
    cache.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = CatalogService(_FearlessIndexNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    status = reloaded.fearless_index_status()
    assert status["indexed_topics"] == 2
    assert status["stale_pages"] == 2
    assert status["fully_refreshed_at"] is None
    await reloaded.close()


@pytest.mark.asyncio
async def test_fearless_index_never_claims_a_full_refresh_it_did_not_make(tmp_path, unpaced_crawl):
    """One page without a timestamp makes "fully refreshed" unanswerable.

    A page cached before per-page freshness existed is already counted as due.
    Reporting the oldest timestamp the index does have would then claim a full
    refresh that never covered that page.
    """
    network = _FearlessIndexNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["example game"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    await service.close()

    cache = tmp_path / "fearless-index.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    starts = sorted(payload["fetched"])
    assert len(starts) >= 2
    payload["fetched"].pop(starts[0])
    cache.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = CatalogService(_FearlessIndexNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    status = reloaded.fearless_index_status()
    assert status["stale_pages"] == 1
    assert status["fully_refreshed_at"] is None
    await reloaded.close()


@pytest.mark.asyncio
async def test_fearless_background_index_cools_down_and_resumes_after_429(tmp_path, monkeypatch):
    class RateLimitedNetwork(_FearlessIndexNetwork):
        limited = False

        async def get(self, url, **kwargs):
            if "viewforum.php" in url and "start=50" in url and not self.limited:
                self.limited = True
                self.calls.append((url, kwargs))
                return _Response(429)
            return await super().get(url, **kwargs)

    monkeypatch.setattr(catalog_module, "FEARLESS_DEFAULT_COOLDOWN_SECONDS", 0.01)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_REQUEST_INTERVAL_SECONDS", 0.0)
    service = CatalogService(RateLimitedNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["missing game"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    assert service.fearless_index_status()["status"] == "ok"
    await service.close()


@pytest.mark.asyncio
async def test_playground_anonymous_countdown_resolves_transient_direct_link():
    rows, _ = parse_playground_page(
        (FIXTURES / "playground_item.html").read_text(encoding="utf-8"),
        "https://www.playground.ru/cheat/example-game-123",
        .95,
    )
    network = _SequenceNetwork([
        _Response(200, b'{"wait_time":"2","lock_hash":"transient-secret"}'),
        _Response(200, b'{"wait_time":null,"download_link":"//dl1.gamedl.ru/files/Example.CT?token=signed"}'),
    ])
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    link = await resolve_playground_download(network, rows[0], sleep=sleep)  # type: ignore[arg-type]
    assert link == "https://dl1.gamedl.ru/files/Example.CT?token=signed"
    assert waits == [2]
    assert "lock_hash=transient-secret" in network.calls[1][0][0]
    assert "transient-secret" not in str(rows[0].public_dict())


@pytest.mark.asyncio
async def test_playground_direct_resolver_rejects_repeated_countdown():
    rows, _ = parse_playground_page(
        (FIXTURES / "playground_item.html").read_text(encoding="utf-8"),
        "https://www.playground.ru/cheat/example-game-123",
        .95,
    )
    network = _SequenceNetwork([
        _Response(200, b'{"wait_time":1,"lock_hash":"token"}'),
        _Response(200, b'{"wait_time":1,"lock_hash":"token-2"}'),
    ])

    async def sleep(_seconds):
        return None

    with pytest.raises(NetworkError, match="repeated"):
        await resolve_playground_download(network, rows[0], sleep=sleep)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_playground_direct_resolver_rejects_duplicate_json_keys():
    rows, _ = parse_playground_page(
        (FIXTURES / "playground_item.html").read_text(encoding="utf-8"),
        "https://www.playground.ru/cheat/example-game-123",
        .95,
    )
    network = _SequenceNetwork([_Response(200, b'{"wait_time":0,"wait_time":1}')])

    async def sleep(_seconds):
        return None

    with pytest.raises(NetworkError, match="duplicate"):
        await resolve_playground_download(network, rows[0], sleep=sleep)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_playground_direct_resolver_preserves_429_retry_after():
    rows, _ = parse_playground_page(
        (FIXTURES / "playground_item.html").read_text(encoding="utf-8"),
        "https://www.playground.ru/cheat/example-game-123",
        .95,
    )
    network = _SequenceNetwork([_Response(429, headers={"retry-after": "90"})])

    async def sleep(_seconds):
        return None

    with pytest.raises(ProviderRateLimited) as caught:
        await resolve_playground_download(network, rows[0], sleep=sleep)  # type: ignore[arg-type]
    assert caught.value.retry_after == "90"
    # Nothing has been spent to reach the opening request, so the caller that is
    # allowed to wait may simply ask again from the start.
    assert caught.value.restartable is True


@pytest.mark.asyncio
async def test_playground_direct_resolver_will_not_replay_a_spent_lock_exchange():
    """The countdown token is issued once and the countdown has already been served."""
    rows, _ = parse_playground_page(
        (FIXTURES / "playground_item.html").read_text(encoding="utf-8"),
        "https://www.playground.ru/cheat/example-game-123",
        .95,
    )
    network = _SequenceNetwork([
        _Response(200, b'{"wait_time":"2","lock_hash":"transient-secret"}'),
        _Response(429, headers={"retry-after": "90"}),
    ])

    async def sleep(_seconds):
        return None

    with pytest.raises(ProviderRateLimited) as caught:
        await resolve_playground_download(network, rows[0], sleep=sleep)  # type: ignore[arg-type]
    assert caught.value.retry_after == "90"
    assert caught.value.restartable is False


@pytest.mark.asyncio
async def test_playground_direct_resolution_reaches_the_provider_during_a_cooldown(tmp_path):
    # The user pressed Download on a row in front of them. A cooldown a failed
    # *search* recorded is not a reason to refuse that without ever asking, and
    # the acquisition layer waits out a real refusal with the provider's own
    # number instead of a default minute nobody asked for.
    rows, _ = parse_playground_page(
        (FIXTURES / "playground_item.html").read_text(encoding="utf-8"),
        "https://www.playground.ru/cheat/example-game-123",
        .95,
    )

    class Diagnostics:
        def can_attempt(self, provider):
            raise AssertionError("a user-selected download must not consult the listing cooldown")

    network = _SequenceNetwork([_Response(429, b"", {"retry-after": "5"})])
    service = CatalogService(network, tmp_path / "cache.json", Diagnostics())  # type: ignore[arg-type]
    with pytest.raises(ProviderRateLimited) as raised:
        await service.resolve_download(rows[0])
    # And it arrives as the wait it is, carrying the provider's own number.
    assert raised.value.retry_after == "5"
    assert network.calls


@pytest.mark.asyncio
async def test_a_challenged_provider_becomes_an_explicit_browser_row(tmp_path):
    # No adapter raises this today, and the machinery stays because a challenge
    # is a thing providers do rather than a thing one provider did: a search
    # that meets one still has to offer the user a way through instead of
    # reporting an empty catalog.
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]

    async def challenged(self, aliases):
        raise BrowserHandoff("https://fearlessrevolution.com/search.php?keywords=example")

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(challenged, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]
    outcome = await service.search("Example")
    row = next(item for item in outcome["results"] if item["artifact_id"].startswith("browser-search:"))
    assert row["provider"] == "fearless"
    assert row["download_mode"] == "file_picker"
    failure = next(item for item in outcome["failures"] if item["provider"] == "fearless")
    assert failure["handoff_url"].endswith("keywords=example")


@pytest.mark.asyncio
async def test_provider_failure_retains_only_its_stale_cached_rows(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]
    fear = ArtifactRecord(CatalogResult(
        provider="fearless", provider_display_name="FearLess Cheat Engine", topic_id="1",
        artifact_id="topic-1:attachment-2", table_title="Example", filename="Old.CT", version=None,
        size_bytes=1, source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
        download_mode="source_handoff", match_score=.8, provider_rank=100,
    ))
    service._save_cache(json.dumps(["Example", None], separators=(",", ":")), [fear])
    async def fearless(self, aliases):
        raise RuntimeError("fixture failure")
    async def playground(self, aliases):
        return []
    service._fearless = types.MethodType(fearless, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(playground, service)  # type: ignore[method-assign]
    outcome = await service.search("Example")
    assert outcome["results"][0]["provider"] == "fearless"
    assert outcome["results"][0]["stale"] is True


@pytest.mark.asyncio
async def test_challenged_provider_search_creates_no_browser_only_result(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]

    async def fearless(self, aliases):
        raise NetworkError("FearLess forum listing returned HTTP 403")

    async def playground(self, aliases):
        return []

    service._fearless = types.MethodType(fearless, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(playground, service)  # type: ignore[method-assign]
    outcome = await service.search("Example")
    assert not [row for row in outcome["results"] if row["provider"] == "fearless"]
    assert next(source for source in outcome["sources"] if source["provider"] == "fearless")["status"] == "unavailable"


@pytest.mark.asyncio
async def test_provider_failure_never_returns_a_blank_diagnostic(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]

    async def fearless(self, aliases):
        raise RuntimeError()

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(fearless, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]
    outcome = await service.search("Example")
    failure = next(item for item in outcome["failures"] if item["provider"] == "fearless")
    assert failure["error"] == "RuntimeError (no diagnostic message)"


@pytest.mark.asyncio
async def test_provider_cancellation_is_not_converted_to_a_search_result(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]

    async def cancelled(self, aliases):
        raise asyncio.CancelledError()

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(cancelled, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await service.search("Example")


@pytest.mark.asyncio
async def test_directory_identity_is_retried_only_after_no_downloadable_result(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]
    calls: list[tuple[list[str], list[str]]] = []

    def record(artifact_id: str, *, mode: str = "direct_https") -> ArtifactRecord:
        return ArtifactRecord(CatalogResult(
            provider="fearless", provider_display_name="FearLess Cheat Engine", topic_id="1",
            artifact_id=artifact_id, table_title="Shadow Recon Operative Blade", filename="Table_v1.CT",
            version="1", size_bytes=1, source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
            download_mode=mode, match_score=.9, provider_rank=100,
        ))

    async def collect(self, game_aliases, queries, *, deadline=None, disabled=None):
        # One Search holds one deadline, and the fallback pass spends what is
        # left of it rather than being handed a second full budget.
        assert deadline is not None
        calls.append((list(game_aliases), list(queries)))
        if len(calls) == 1:
            return [record("browser-only", mode="file_picker")], []
        return [record("directory-result")], []

    service._collect = types.MethodType(collect, service)  # type: ignore[method-assign]
    outcome = await service.search(
        "SRO: Hidden Blade",
        '"/home/deck/Games/Shadow.Recon.Operative.Hidden.Blade.2025-Group/HiddenBladeDelta.exe"',
    )
    assert calls == [
        (["sro hidden blade"], ["sro hidden blade"]),
        (["shadow recon operative hidden blade"], ["shadow recon operative hidden blade"]),
    ]
    assert [row["artifact_id"] for row in outcome["results"]] == ["directory-result"]

    calls.clear()

    async def downloadable(self, game_aliases, queries, *, deadline=None, disabled=None):
        calls.append((list(game_aliases), list(queries)))
        return [record("library-name-result")], []

    service._collect = types.MethodType(downloadable, service)  # type: ignore[method-assign]
    outcome = await service.search(
        "Ashen Vale",
        '"/home/deck/Games/Ember.Fields.4/EmberFields4.exe"',
    )
    assert len(calls) == 1
    assert [row["artifact_id"] for row in outcome["results"]] == ["library-name-result"]


@pytest.mark.asyncio
async def test_playground_conditional_304_reuses_bounded_index(tmp_path):
    cache_path = tmp_path / "results.json"
    sitemap_cache = cache_path.with_name("playground-sitemap.json")
    sitemap_cache.write_text(json.dumps({
        "schema": 1, "etag": '"fixture"', "last_modified": "Sat, 16 Aug 2026 00:00:00 GMT",
        "retrieved": 1, "entries": [["123", "example game", "https://www.playground.ru/cheat/example-game-123"]],
    }), encoding="utf-8")
    network = _SequenceNetwork([
        _Response(304),
        _Response(200, (FIXTURES / "playground_item.html").read_bytes()),
        _Response(200, (FIXTURES / "github_releases.json").read_bytes()),
    ])
    service = CatalogService(network, cache_path)  # type: ignore[arg-type]
    rows = await service._playground(["example game"])
    assert any(row.result.provider == "playground" for row in rows)
    assert network.calls[0][1]["headers"]["If-None-Match"] == '"fixture"'


def test_corrupt_catalog_cache_fails_to_empty(tmp_path):
    path = tmp_path / "results.json"
    path.write_text("not-json", encoding="utf-8")
    service = CatalogService(None, path)  # type: ignore[arg-type]
    assert service._load_cache("Example") == []


@pytest.mark.asyncio
async def test_github_anonymous_budget_is_a_wait_rather_than_a_failure(tmp_path):
    # GitHub reports an exhausted anonymous budget as 403 with a remaining count
    # of zero and a reset timestamp, never as 429 with a `Retry-After`. Reading
    # it as the cooldown it is lets the same waiting the rest of the catalog
    # already does apply here, instead of reporting the source as broken.
    reset = str(int(time.time()) + 45)
    service = CatalogService(_SequenceNetwork([
        _Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
    ]), tmp_path / "cache.json")  # type: ignore[arg-type]
    with pytest.raises(ProviderRateLimited) as raised:
        await service._github("https://github.com/example/tables", .9)
    assert 0 < int(raised.value.retry_after) <= 45


def test_playground_category_listing_yields_only_this_game_s_entry_pages():
    """The `table` category page is what separates tables from trainers, saves,
    save editors and cheat mods: filenames cannot, because a table and a save
    editor both ship as `.zip`. The parser must therefore collect exactly the
    cheat pages that listing declares for this game, and nothing else."""
    slug = "lumen_hollow_voyage_12"
    html = """
      <div class="post-content">
        <time datetime="2026-07-30T22:01:36+03:00">30 июля</time>
        <a href="/lumen_hollow_voyage_12/cheat/tablitsa_9-1863314">Table 9</a>
      </div>
      <div class="post-content">
        <a href="https://www.playground.ru/lumen_hollow_voyage_12/cheat/tablitsa_5-1767615">Table 5</a>
      </div>
      <a href="/lumen_hollow_voyage_12/cheat/trainers">Sibling category, not an entry</a>
      <a href="/other_game/cheat/tablitsa-1">Another game</a>
      <a href="https://example.com/lumen_hollow_voyage_12/cheat/spoofed-1">Foreign host</a>
    """
    # The listing is the only place carrying a machine-readable date per entry;
    # an entry without one stays listed rather than being dropped, and so does an
    # entry whose own category cannot be read.
    assert parse_playground_category(html, slug) == ({
        "https://www.playground.ru/lumen_hollow_voyage_12/cheat/tablitsa_9-1863314": "2026-07-30T19:01:36Z",
        "https://www.playground.ru/lumen_hollow_voyage_12/cheat/tablitsa_5-1767615": None,
    }, 2)
    assert playground_game_slug("https://www.playground.ru/lumen_hollow_voyage_12/cheat/tablitsa_9-1863314") == slug
    assert playground_game_slug("https://www.playground.ru/cheat/table") is None


def test_playground_category_listing_drops_entries_of_another_kind():
    """`/cheat/table` is not table-only.

    Observed on the target: `Cadence: A Silent Requiem` has no Cheat
    Engine table, and its `table` listing returned a third-party trainer and a save
    file instead. Each entry names its own category beside its timestamp, which
    is the only language-independent marker of what it really is.
    """
    slug = "cadence_a_silent_requiem"
    html = """
      <div class="post-content">
        <div class="post-metadata">
          <time datetime="2026-08-27T22:24:06+03:00">вчера</time>
          <a href="/cadence_a_silent_requiem/cheat/save">Сохранения</a>
        </div>
        <div class="post-title">
          <a href="/cadence_a_silent_requiem/cheat/sohranenie_100-1870076">Save 100%</a>
        </div>
      </div>
      <div class="post-content">
        <div class="post-metadata">
          <a href="/cadence_a_silent_requiem/cheat/trainer">Трейнеры</a>
        </div>
        <div class="post-title">
          <a href="/cadence_a_silent_requiem/cheat/trejner_4_ot_trainerapp-1870038">Trainer</a>
        </div>
      </div>
      <div class="post-content">
        <div class="post-metadata">
          <a href="/cadence_a_silent_requiem/cheat/table">Таблицы</a>
        </div>
        <div class="post-title">
          <a href="/cadence_a_silent_requiem/cheat/tablitsa-1870099">Real table</a>
        </div>
      </div>
    """
    pages, entries = parse_playground_category(html, slug)
    assert entries == 3
    assert list(pages) == ["https://www.playground.ru/cadence_a_silent_requiem/cheat/tablitsa-1870099"]


def test_sitemap_prefilter_keeps_every_scored_match():
    """Scoring the whole Playground sitemap dominated the search: 50,000 titles
    cost over twenty seconds to rank for fifteen matches. The prefilter must
    therefore be a pure speed gate that changes no outcome."""
    from ce_decky.game_identity import Candidate, aliases, match_breakdown

    titles = [
        "lumen hollow voyage 12 tablitsa 9 dlya cheat engine ot tableauthor",
        "lumen hollow voyage 12 trejner 32 ot trainerapp",
        "mirefall 2033 redux tablitsa 21 dlya cheat engine",
        "cadence a silent requiem trejner 4 ot trainerapp",
        "colonysim chit mod redaktirovat harakteristiki personazha",
    ]
    game_aliases = aliases("Lumen Hollow: Voyage 12", None)
    tokens = sitemap_prefilter_tokens(game_aliases)
    assert tokens == ["hollow"]

    def scored(candidates):
        return {
            title for title in candidates
            if match_breakdown(game_aliases, Candidate(title, provider_trust=0.65)).confidence >= 0.55
        }

    survivors = [title for title in titles if any(token in title.casefold() for token in tokens)]
    assert scored(survivors) == scored(titles)
    assert len(survivors) < len(titles)

    # A name whose words are all short must not silently filter everything out.
    assert sitemap_prefilter_tokens(["ai"]) == []


def _deferred_record(request):
    return ArtifactRecord(CatalogResult(
        provider="playground", provider_display_name="Playground", topic_id="1",
        artifact_id="page-1:file-2", table_title="Example", filename="Example.CT", version=None,
        size_bytes=1, source_page="https://www.playground.ru/x/cheat/y-1",
        download_mode="direct_https", match_score=.8, provider_rank=85,
    ), acquisition=request)


class _OtherProviderRequest:
    """A deferred provider that is not Playground, which is the whole point."""

    provider = "thecheatscript"
    artifact_hosts = frozenset({"downloader.example.invalid"})

    def __init__(self):
        self.calls = []

    async def resolve(self, network, record, *, sleep, on_countdown=None):
        self.calls.append(record.result.artifact_id)
        return "https://downloader.example.invalid/table.CT?token=transient"


@pytest.mark.asyncio
async def test_acquisition_is_resolved_through_the_request_not_a_provider_branch(tmp_path):
    # Adding a deferred provider must not mean another field on ArtifactRecord
    # and another branch in resolve_download.
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]
    request = _OtherProviderRequest()
    url, hosts = await service.resolve_download(_deferred_record(request))
    assert url.startswith("https://downloader.example.invalid/")
    assert hosts == frozenset({"downloader.example.invalid"})
    assert hosts != PLAYGROUND_ARTIFACT_HOSTS
    assert request.calls == ["page-1:file-2"]


@pytest.mark.asyncio
async def test_a_dynamic_resolve_consults_no_listing_cooldown_at_all(tmp_path):
    """Throttling a download somebody is watching belongs to the acquisition.

    This used to refuse outright when the background listing crawl held a
    cooldown for the provider, which contradicted the retry state machine that
    owns download throttling: that one waits out a real 429 as a visible
    countdown honouring the provider's own `Retry-After`, while this failed the
    download with a generic error for a limit a search recorded and for a
    default minute the provider may never have asked for. An earlier defect
    here was that the gate asked about Playground whatever provider the record
    named; the gate itself is what is gone now.
    """
    asked = []

    class _Diagnostics:
        def can_attempt(self, provider_id):
            asked.append(provider_id)
            return False

    service = CatalogService(None, tmp_path / "cache.json", _Diagnostics())  # type: ignore[arg-type]
    request = _OtherProviderRequest()
    url, _ = await service.resolve_download(_deferred_record(request))
    assert url.startswith("https://downloader.example.invalid/")
    assert request.calls == ["page-1:file-2"]
    assert asked == []


@pytest.mark.asyncio
async def test_a_record_with_no_acquisition_route_still_refuses(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await service.resolve_download(_deferred_record(None))


@pytest.mark.asyncio
async def test_a_searchable_provider_with_no_adapter_is_reported_rather_than_dropped(tmp_path, monkeypatch):
    """The registry decides which providers a search runs.

    GitHub was registered as enabled and ready while the search scheduled three
    hard-coded jobs and summarized the same three, so a source could be offered
    to the user that no search would ever query. Deriving both from the registry
    is what removes that, and a provider the registry declares searchable with
    nothing wired to run it is a defect here: it is reported as a failed source
    rather than quietly left out, which is how the divergence stayed invisible.
    """
    from ce_decky import catalog as catalog_module
    from ce_decky.providers import ProviderDefinition, searchable_providers

    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(empty, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]

    unwired = ProviderDefinition("cheatenginenet", "CheatEngine.net", 40, True, "phpbb", "ready")
    monkeypatch.setattr(
        catalog_module, "searchable_providers",
        lambda: searchable_providers() + (unwired,),
    )
    outcome = await service.search("Example")
    failure = next(item for item in outcome["failures"] if item["provider"] == "cheatenginenet")
    assert "no search adapter" in failure["error"]
    source = next(item for item in outcome["sources"] if item["provider"] == "cheatenginenet")
    assert source["status"] == "unavailable"


@pytest.mark.asyncio
async def test_the_search_summary_names_exactly_the_providers_it_ran(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(empty, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]
    outcome = await service.search("Example")
    # Exactly the registry's searchable providers, in registry order. GitHub
    # carries a search of its own now as well as the exact-source route, so it
    # is one of the sources a search reports on.
    assert [item["provider"] for item in outcome["sources"]] == [
        "fearless", "playground", "github", "thecheatscript", "vgtimes",
    ]


@pytest.mark.asyncio
async def test_an_index_cached_without_its_page_size_is_not_reinterpreted(tmp_path, unpaced_crawl):
    """An offset means nothing without the size of a page it counts.

    A cache written before the size was recorded, or one whose recorded size is
    out of bounds, is dropped and rebuilt rather than read as if pages were the
    size they happen to be today.
    """
    service = CatalogService(_FearlessIndexNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["example game"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    await service.close()

    cache = tmp_path / "fearless-index.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["page_step"] == 50
    payload.pop("page_step")
    cache.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = CatalogService(_FearlessIndexNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    assert reloaded._fearless_pages == {}
    assert reloaded._fearless_page_step is None
    await reloaded.close()


@pytest.mark.asyncio
async def test_a_listing_that_pages_differently_drops_the_index_it_no_longer_describes(tmp_path, unpaced_crawl):
    """The failure this prevents is silent: an index with holes that looks whole.

    Every stored offset is a multiple of the page size at the time it was
    stored. If the forum changes that size, continuing to walk the old offsets
    reads every other page and reports a complete index over half a forum.
    """
    service = CatalogService(_FearlessIndexNetwork(), tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["example game"])
    assert service._fearless_index_task is not None
    await service._fearless_index_task
    assert service._fearless_page_step == 50 and service._fearless_pages

    class ResizedNetwork(_FearlessIndexNetwork):
        async def get(self, url, **kwargs):
            if "viewforum.php" in url:
                self.calls.append((url, kwargs))
                start = int(url.rsplit("start=", 1)[1]) if "start=" in url else 0
                topic = ("999", "Fresh Match") if start == 25 else ("123", "Example Game")
                return _Response(200, _fearless_listing(
                    topic, page=2 if start else 1, pages=2, total=26, page_step=25,
                ).encode())
            if "viewtopic.php" in url and "t=999" in url:
                self.calls.append((url, kwargs))
                return _Response(200, (FIXTURES / "fearless_current.html").read_bytes())
            return await super().get(url, **kwargs)

    resized = ResizedNetwork()
    service.network = resized
    rows = await service._fearless(["fresh match"])
    assert service._fearless_page_step == 25
    assert sorted(service._fearless_pages) == [0, 25]
    assert [
        int(url.rsplit("start=", 1)[1]) if "start=" in url else 0
        for url, _ in resized.calls if "viewforum.php" in url
    ] == [0, 25]
    assert [row.result.artifact_id for row in rows] == ["topic-999:attachment-42"]
    await service.close()


class _CountingDiagnostics:
    """Enough of the diagnostics store to see what a search records."""

    def __init__(self, cooldown: bool) -> None:
        self.cooldown = cooldown
        self.failures: list[dict[str, object]] = []
        self.successes: list[str] = []

    def can_attempt(self, provider):
        return not self.cooldown

    def record_failure(self, provider, **kwargs):
        self.failures.append({"provider": provider, **kwargs})

    def record_search_success(self, provider, **kwargs):
        self.successes.append(provider)

    def record_parse_issues(self, provider, **kwargs):
        pass


@pytest.mark.asyncio
async def test_a_search_skipped_for_a_cooldown_records_nothing_that_clears_it(tmp_path):
    """A cooldown skip is not a provider failure, because nothing was contacted.

    Recorded as one it wrote a cleared deadline, since a failure with no status
    and no `Retry-After` has no cooldown of its own: one search during a
    cooldown therefore made the next search eligible immediately, which is the
    opposite of what the deadline that produced it was for.
    """
    diagnostics = _CountingDiagnostics(cooldown=True)
    service = CatalogService(None, tmp_path / "cache.json", diagnostics)  # type: ignore[arg-type]

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(empty, service)  # type: ignore[method-assign]
    service._playground = types.MethodType(empty, service)  # type: ignore[method-assign]
    outcome = await service.search("Example")

    skipped = {str(item["provider"]) for item in outcome["failures"]}
    assert {"github", "thecheatscript", "vgtimes"} <= skipped
    assert all("cooldown" in str(item["error"]) for item in outcome["failures"])
    # The source is reported as unavailable, so it never looks like a game with
    # no tables, and nothing about it is written back.
    assert all(item["status"] == "unavailable" for item in outcome["sources"] if item["provider"] in skipped)
    assert diagnostics.failures == []


def test_a_real_cooldown_survives_a_search_that_only_observed_it(tmp_path):
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("vgtimes", error="HTTP 429", http_status=429, retry_after="600")
    deadline = store.snapshot()["providers"]["vgtimes"]["cooldown_until_epoch_s"]
    assert not store.can_attempt("vgtimes") and deadline > time.time()
    # This is what recording the skip used to do to it.
    store.record_failure("vgtimes", error="provider is in persisted cooldown")
    assert store.snapshot()["providers"]["vgtimes"]["cooldown_until_epoch_s"] == 0.0


@pytest.mark.asyncio
async def test_a_user_selected_download_is_not_gated_by_the_listing_cooldown(tmp_path):
    """The acquisition retry state machine owns throttling for a download.

    It waits out a real 429 as a visible countdown honouring the provider's own
    `Retry-After`. Consulting the background crawl's cooldown here failed that
    download outright, with a generic error, for a limit a search recorded and
    for a default minute the provider may never have asked for.
    """
    resolved: list[str] = []

    class _Request:
        provider = "vgtimes"
        artifact_hosts = frozenset({"vgtimes.ru"})
        send_source_referer = True

        async def resolve(self, network, record, *, sleep, on_countdown=None):
            resolved.append(record.result.artifact_id)
            return "https://vgtimes.ru/index.php?do=files&op=showfile", self.artifact_hosts

    record = ArtifactRecord(CatalogResult(
        provider="vgtimes", provider_display_name="VGTimes", topic_id="g",
        artifact_id="game-g:file-1", table_title="T", filename="T.CT", version=None,
        size_bytes=5, source_page="https://vgtimes.ru/games/g/files/1-t.html",
        download_mode="direct_https", match_score=.9, provider_rank=70,
    ), acquisition=_Request())
    service = CatalogService(None, tmp_path / "cache.json", _CountingDiagnostics(cooldown=True))  # type: ignore[arg-type]
    url, hosts = await service.resolve_download(record)
    assert resolved == ["game-g:file-1"] and "vgtimes.ru" in hosts


def _cache_row(provider: str, artifact_id: str) -> ArtifactRecord:
    return ArtifactRecord(CatalogResult(
        provider=provider, provider_display_name=provider, topic_id="1",
        artifact_id=artifact_id, table_title="Example", filename="Example.CT",
        version=None, size_bytes=5, source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
        download_mode="direct_https", match_score=.9, provider_rank=100,
    ))


def _fetch_times(cache) -> dict[str, float]:
    payload = json.loads(cache.read_text())
    return {
        f"{item['result']['provider']}:{item['result']['artifact_id']}": item["fetched"]
        for item in payload["rows"]
    }


def test_each_cached_row_expires_on_its_own_age(tmp_path):
    """One answer routinely mixes freshness, and one timestamp cannot say both.

    A search that one provider failed re-serves that provider's cached rows
    beside rows another provider has just returned. Whichever single age the
    file was given was wrong for half of it: refreshing it kept a dead source
    alive forever, and preserving it aged a table fetched seconds ago to almost
    seven days.
    """
    cache = tmp_path / "results.json"
    service = CatalogService(None, cache)  # type: ignore[arg-type]
    service._save_cache("q", [_cache_row("fearless", "a")])
    original = _fetch_times(cache)["fearless:a"]

    # Six days pass, and only that provider's row is in the cache.
    payload = json.loads(cache.read_text())
    payload["rows"][0]["fetched"] = original - 6 * 24 * 3600
    payload["saved"] = original - 6 * 24 * 3600
    cache.write_text(json.dumps(payload), encoding="utf-8")

    # Now fearless fails and playground answers. The mixed result is written.
    stale = service._load_cache("q")
    assert [row.result.provider for row in stale] == ["fearless"] and stale[0].stale
    service._save_cache("q", [stale[0], _cache_row("playground", "b")])

    ages = _fetch_times(cache)
    assert ages["fearless:a"] == pytest.approx(original - 6 * 24 * 3600)
    assert time.time() - ages["playground:b"] < 60


def test_a_row_older_than_the_stale_horizon_is_dropped_alone(tmp_path):
    cache = tmp_path / "results.json"
    service = CatalogService(None, cache)  # type: ignore[arg-type]
    service._save_cache("q", [_cache_row("fearless", "a"), _cache_row("playground", "b")])
    payload = json.loads(cache.read_text())
    payload["rows"][0]["fetched"] = time.time() - (STALE_TTL_SECONDS + 60)
    cache.write_text(json.dumps(payload), encoding="utf-8")
    # The expired row goes; the one beside it, which is hours old, stays.
    assert [row.result.provider for row in service._load_cache("q")] == ["playground"]


def test_a_cache_written_before_rows_carried_their_own_age_still_reads(tmp_path):
    cache = tmp_path / "results.json"
    service = CatalogService(None, cache)  # type: ignore[arg-type]
    service._save_cache("q", [_cache_row("fearless", "a")])
    payload = json.loads(cache.read_text())
    del payload["rows"][0]["fetched"]
    cache.write_text(json.dumps(payload), encoding="utf-8")
    # The file's own timestamp is exactly what it used to mean.
    assert [row.result.provider for row in service._load_cache("q")] == ["fearless"]
    payload["saved"] = time.time() - (STALE_TTL_SECONDS + 60)
    cache.write_text(json.dumps(payload), encoding="utf-8")
    assert service._load_cache("q") == []


def test_a_restored_cached_row_keeps_the_shape_it_was_saved_with(tmp_path):
    # `blob_sha1` was inserted before `password_hint` while this reconstruction
    # was still positional, so every restored row claimed it needed an archive
    # password and stopped being marked stale.
    cache = tmp_path / "results.json"
    service = CatalogService(None, cache)  # type: ignore[arg-type]
    row = ArtifactRecord(CatalogResult(
        provider="fearless", provider_display_name="FearLess Cheat Engine", topic_id="1",
        artifact_id="topic-1:attachment-2", table_title="Example", filename="Example.CT",
        version=None, size_bytes=5, source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
        download_mode="direct_https", match_score=.9, provider_rank=100,
    ), advertised_sha256="a" * 64)
    service._save_cache("q", [row])

    restored = service._load_cache("q")[0]
    assert restored.advertised_sha256 == "a" * 64
    assert restored.blob_sha1 is None
    assert restored.password_hint is None
    assert restored.stale is True
    assert restored.direct_url is None and restored.acquisition is None
    public = restored.public_dict()
    assert public["password_required"] is False
    assert public["stale"] is True
    # A cached row is never offered as something this can fetch by itself.
    assert restored.result.download_mode == "source_handoff"


@pytest.mark.asyncio
async def test_a_provider_in_cooldown_says_so_rather_than_reading_as_empty(tmp_path):
    """A search that asked nothing is not a search that went well.

    FearLess answers from its own cached forum index, so a cooldown made it
    return an empty list rather than raise. Every returned list was read as
    success, which cleared the very deadline that had stopped it, told the user
    the source was fine with zero results, and skipped restoring its cached
    rows because nothing had failed.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    class _Net:
        def __init__(self):
            self.calls: list[str] = []

        async def get(self, url, **kwargs):
            self.calls.append(url)
            raise AssertionError("a provider in cooldown must not be asked")

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("fearless", error="HTTP 429", http_status=429, retry_after="600")
    deadline = store.snapshot()["providers"]["fearless"]["cooldown_until_epoch_s"]

    network = _Net()
    service = CatalogService(network, tmp_path / "results.json", store)  # type: ignore[arg-type]
    service._fearless_pages = {0: [("123", "example game")]}
    # A previous search left rows for this game in the cache.
    service._save_cache(json.dumps(["Example Game", None], ensure_ascii=False, separators=(",", ":")),
                        [_cache_row("fearless", "topic-123:attachment-1")])

    async def empty(self, *args):
        return []

    for name in ("_playground", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))
    outcome = await service.search("Example Game")

    assert network.calls == []
    source = next(item for item in outcome["sources"] if item["provider"] == "fearless")
    # Not a successful search of nothing, and not a bare failure either: a
    # source that has an index and is being told to wait says both, because
    # `n/a` is also what a source with nothing for this game reads as.
    assert source["status"] == "cooldown" and "cooldown" in str(source["error"])
    assert source["retry_after_seconds"] and source["retry_after_seconds"] <= 600
    # The deadline the provider asked for is untouched.
    assert store.snapshot()["providers"]["fearless"]["cooldown_until_epoch_s"] == deadline
    # And the cached answer is what the user is shown meanwhile.
    assert [row["artifact_id"] for row in outcome["results"]] == ["topic-123:attachment-1"]
    assert outcome["stale"] is True
    await service.close()


@pytest.mark.asyncio
async def test_a_provider_that_never_answers_does_not_hold_the_whole_search(tmp_path, monkeypatch):
    """Every request is bounded; a provider's whole chain was not.

    A source whose sitemap index, pages, ranked posts and metadata calls each
    time out in turn kept a search somebody is watching waiting for minutes
    while every other source had already finished.
    """
    monkeypatch.setattr(catalog_module, "PROVIDER_SEARCH_BUDGET_SECONDS", 0.3)
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]

    async def never(self, *args):
        await asyncio.Event().wait()

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(never, service)  # type: ignore[method-assign]
    for name in ("_playground", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))

    started = time.monotonic()
    outcome = await service.search("Example")
    assert time.monotonic() - started < 3
    slow = next(item for item in outcome["sources"] if item["provider"] == "fearless")
    assert slow["status"] == "unavailable" and "did not answer" in str(slow["error"])
    # Everything that did answer is still delivered.
    assert all(
        item["status"] == "ok" for item in outcome["sources"] if item["provider"] != "fearless"
    )
    await service.close()


@pytest.mark.asyncio
async def test_a_linked_source_is_read_on_its_own_providers_terms(tmp_path):
    """A page one source links belongs to the provider that answers it.

    The read used to skip that provider's cooldown entirely, so a source
    deliberately waiting still received requests because something else pointed
    at it, and any refusal was attributed to nobody.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("github", error="HTTP 429", http_status=429, retry_after="600")
    deadline = store.snapshot()["providers"]["github"]["cooldown_until_epoch_s"]

    asked: list[str] = []

    async def read():
        asked.append("github")
        return []

    service = CatalogService(None, tmp_path / "results.json", store)  # type: ignore[arg-type]
    assert await service._linked_source("github", read) == []
    assert asked == []
    assert store.snapshot()["providers"]["github"]["cooldown_until_epoch_s"] == deadline

    # A limit the linked provider answers with is recorded against that
    # provider rather than against the one that linked it.
    async def throttled():
        raise ProviderRateLimited("30", "GitHub returned HTTP 429")

    store2 = ProviderDiagnosticsStore(tmp_path / "providers2.json")
    service = CatalogService(None, tmp_path / "results2.json", store2)  # type: ignore[arg-type]
    with pytest.raises(ProviderRateLimited):
        await service._linked_source("github", throttled)
    assert not store2.can_attempt("github")


@pytest.mark.asyncio
async def test_an_ordinary_linked_source_failure_is_recorded_against_its_owner(tmp_path):
    """The linking provider swallows this, so nothing else can record it.

    Left unattributed, a source answering 500 for every repository another
    page names looked perfectly healthy in Advanced, and the one screen that
    exists to say which source is failing said nothing at all.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    # Its own search is fine, which is exactly what must not be overwritten.
    store.record_search_success("github", results=3, latency_ms=12, http_status=200)
    before = store.snapshot()["providers"]["github"]
    service = CatalogService(None, tmp_path / "results.json", store)  # type: ignore[arg-type]

    async def answered_500():
        raise NetworkError("GitHub returned HTTP 500")

    with pytest.raises(NetworkError):
        await service._linked_source("github", answered_500)

    after = store.snapshot()["providers"]["github"]
    assert after["counters"]["errors"] == before["counters"]["errors"] + 1
    assert "HTTP 500" in str(after["last_error"])
    # Narrower than a search failure on purpose: no search of this provider
    # happened, its own state is not relabelled by an optional read somebody
    # else's page asked for, and no deadline is written, because a failure
    # carrying none writes a cleared one.
    assert after["counters"]["searches"] == before["counters"]["searches"]
    assert after["state"] == before["state"] == "ready"
    assert after["cooldown_until_epoch_s"] == before["cooldown_until_epoch_s"] == 0.0
    assert store.can_attempt("github")

    # A payload the linked provider answered with that cannot be read is the
    # same kind of event, and the caller swallows exactly these too.
    async def unreadable():
        raise ValueError("GitHub releases payload is invalid")

    with pytest.raises(ValueError):
        await service._linked_source("github", unreadable)
    assert store.snapshot()["providers"]["github"]["counters"]["errors"] == before["counters"]["errors"] + 2


@pytest.mark.asyncio
async def test_a_playground_row_survives_a_linked_github_failure_that_is_recorded(tmp_path):
    """End to end: the row the user can use stays, and the failure has an owner."""
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    sitemap_cache = (tmp_path / "results.json").with_name("playground-sitemap.json")
    sitemap_cache.write_text(json.dumps({
        "schema": 1, "etag": '"fixture"', "last_modified": None, "retrieved": time.time() - _OLDER_THAN_TTL,
        "entries": [["123", "example game", "https://www.playground.ru/cheat/example-game-123"]],
    }), encoding="utf-8")

    network = _SequenceNetwork([
        _Response(304),
        _Response(200, (FIXTURES / "playground_item.html").read_bytes()),
        # The exact GitHub repository that page names.
        _Response(500),
    ])
    service = CatalogService(network, tmp_path / "results.json", store)  # type: ignore[arg-type]
    rows = await service._playground(["example game"])

    assert rows and all(row.result.provider == "playground" for row in rows)
    assert store.snapshot()["providers"]["github"]["counters"]["errors"] == 1
    assert store.snapshot()["providers"]["github"]["counters"]["searches"] == 0


@pytest.mark.asyncio
async def test_playground_treats_a_rate_limit_as_one_everywhere(tmp_path):
    """A limit on any Playground read is the provider asking for a wait.

    None of them translated 429 before. On the sitemap it fell through to the
    cache and the crawl carried on asking the provider it had just been told to
    wait for; on a page or a category it was simply "not 200", so no cooldown
    was established and the diagnostics never learned it had been throttled.
    """
    sitemap_cache = (tmp_path / "results.json").with_name("playground-sitemap.json")
    sitemap_cache.write_text(json.dumps({
        "schema": 1, "etag": '"fixture"', "last_modified": None, "retrieved": time.time() - _OLDER_THAN_TTL,
        "entries": [["123", "example game", "https://www.playground.ru/cheat/example-game-123"]],
    }), encoding="utf-8")

    network = _SequenceNetwork([_Response(429, b"", {"retry-after": "45"})])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    with pytest.raises(ProviderRateLimited) as raised:
        await service._playground(["example game"])
    # One request, and none of the follow-up reads a cached sitemap would have
    # authorized against a provider that just refused.
    assert len(network.calls) == 1
    assert raised.value.retry_after == "45"


def test_an_exactly_oversized_artifact_is_never_offered(tmp_path):
    # The transport refuses this size, so offering the row costs a press, a
    # provider countdown and a wait to be told what the search already knew.
    from ce_decky.catalog import acquirable_size
    from ce_decky.providers import MAX_ARTIFACT_BYTES

    assert acquirable_size(MAX_ARTIFACT_BYTES, size_exact=True)
    assert not acquirable_size(MAX_ARTIFACT_BYTES + 1, size_exact=True)
    # A described or rounded size is publisher text, and the streaming bound
    # still governs it at transfer time.
    assert acquirable_size(MAX_ARTIFACT_BYTES + 1, size_exact=False)
    assert acquirable_size(None, size_exact=True)


@pytest.mark.asyncio
async def test_one_search_holds_one_budget_across_both_of_its_passes(tmp_path, monkeypatch, unpaced_crawl):
    """The install-directory fallback spends what is left, not a second budget.

    Two passes each given the full allowance is the same source holding a
    controller-visible Search for twice as long, which is the multiplication the
    budget was introduced to stop.
    """
    monkeypatch.setattr(catalog_module, "PROVIDER_SEARCH_BUDGET_SECONDS", 0.6)
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]

    async def never(self, *args):
        await asyncio.Event().wait()

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(never, service)  # type: ignore[method-assign]
    for name in ("_playground", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))

    started = time.monotonic()
    outcome = await service.search(
        "SRO: Hidden Blade",
        '"/home/deck/Games/Shadow.Recon.Operative.Hidden.Blade.2025-Group/HiddenBladeDelta.exe"',
    )
    elapsed = time.monotonic() - started
    # One budget, not two. The bound is generous enough not to be flaky and
    # still far below the two full budgets this used to take.
    assert elapsed < 1.2
    slow = next(item for item in outcome["sources"] if item["provider"] == "fearless")
    assert slow["status"] == "unavailable" and "search budget" in str(slow["error"])
    await service.close()


@pytest.mark.asyncio
async def test_a_challenged_fearless_topic_stops_the_provider_rather_than_one_page(tmp_path):
    """A challenge is not one unreadable topic.

    Skipped as merely "not 200", a provider challenging every matching topic
    answered with an empty list, and every returned list reads as a successful
    search: the source was shown as healthy with no results, and its cached rows
    were not restored because nothing had failed.
    """
    listing = _fearless_listing(("123", "example game"), page=1)
    requested: list[str] = []

    class _Net:
        async def get(self, url, **kwargs):
            requested.append(url)
            if "viewforum.php" in url:
                return _Response(200, listing.encode())
            return _Response(403)

    service = CatalogService(_Net(), tmp_path / "results.json")  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        rows = await service._fearless(["example game"])
    assert rows == []
    assert issues.unavailable is not None and "403" in issues.unavailable
    # One topic was asked and the rest were not.
    assert sum(1 for url in requested if "viewtopic.php" in url) == 1
    await service.close()


@pytest.mark.asyncio
async def test_a_challenged_provider_page_is_unavailable_rather_than_empty(tmp_path):
    from ce_decky.network import ProviderChallenged

    # The Cheat Script: its sitemap answers, and every matching post challenges.
    sitemap_cache = (tmp_path / "results.json").with_name("thecheatscript-sitemap.json")
    sitemap_cache.write_text(json.dumps({
        "schema": 1, "saved": time.time(),
        "entries": [["https://www.thecheatscript.com/2026/08/example-game.html", "2026-08-01T00:00:00Z"]],
    }), encoding="utf-8")

    class _Net:
        def __init__(self):
            self.calls: list[str] = []

        async def get(self, url, **kwargs):
            self.calls.append(url)
            return _Response(403)

    network = _Net()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        rows = await service._thecheatscript(["example game"])
    assert rows == []
    assert issues.unavailable is not None and "403" in issues.unavailable
    assert len(network.calls) == 1
    await service.close()


def _playground_sitemap_cache(cache_path, entries):
    cache_path.with_name("playground-sitemap.json").write_text(json.dumps({
        "schema": 1, "etag": '"fixture"', "last_modified": None, "retrieved": time.time() - _OLDER_THAN_TTL,
        "entries": entries,
    }), encoding="utf-8")


class _RoutedNetwork:
    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        for fragment, response in self.routes:
            if fragment in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")


@pytest.mark.asyncio
async def test_a_challenged_playground_category_stops_the_provider(tmp_path):
    """A challenge is not a listing this could not read.

    Degraded to the ranked fallback it looked like one, and the item reads that
    follow are against the same challenged site, so the provider went on asking
    and then answered with an empty list that reads as a successful search.
    """
    from ce_decky.network import ProviderChallenged

    cache = tmp_path / "results.json"
    _playground_sitemap_cache(cache, [
        ["123", "example game", "https://www.playground.ru/example_game/cheat/example-game-123"],
    ])
    network = _RoutedNetwork([
        ("sitemap/cheat/list.xml", _Response(304)),
        ("/cheat/table", _Response(403)),
        ("/cheat/example-game-123", _Response(200, (FIXTURES / "playground_item.html").read_bytes())),
    ])
    service = CatalogService(network, cache)  # type: ignore[arg-type]
    with pytest.raises(ProviderChallenged):
        await service._playground(["example game"])
    # The category was asked and nothing after it was.
    assert not any("/cheat/example-game-123" in url for url in network.calls)
    await service.close()


@pytest.mark.asyncio
async def test_a_challenge_part_way_through_keeps_the_rows_already_read(tmp_path):
    """Rows and a refusal at once, which is what the search contract supports.

    Letting the challenge leave the job would have thrown away pages that had
    already been read and were perfectly usable.
    """
    cache = tmp_path / "results.json"
    # Ranked so the page that answers is read first and the challenge lands on
    # the second, which is the order this is about.
    _playground_sitemap_cache(cache, [
        ["123", "example game", "https://www.playground.ru/example_game/cheat/example-game-123"],
        ["124", "example game two", "https://www.playground.ru/example_game/cheat/example-game-124"],
    ])
    network = _RoutedNetwork([
        ("sitemap/cheat/list.xml", _Response(304)),
        # Not a challenge, so the ranked order is used and both items are read.
        ("/cheat/table", _Response(500)),
        ("/cheat/example-game-123", _Response(200, (FIXTURES / "playground_item.html").read_bytes())),
        # That page names an exact GitHub repository, which is read on its own
        # provider's terms and carries nothing here.
        ("api.github.com", _Response(200, b"[]")),
        ("/cheat/example-game-124", _Response(403)),
    ])
    service = CatalogService(network, cache)  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        rows = await service._playground(["example game"])

    # The page that answered is still an answer.
    assert rows and all(row.result.provider == "playground" for row in rows)
    # And the source says it stopped, so its status is not a healthy zero.
    assert issues.unavailable is not None and "403" in issues.unavailable
    # Nothing was asked after the challenge.
    assert network.calls[-1].endswith("example-game-124")
    await service.close()


@pytest.mark.asyncio
async def test_a_challenged_playground_read_is_a_rate_limit_sibling(tmp_path):
    from ce_decky.network import ProviderChallenged

    sitemap_cache = (tmp_path / "results.json").with_name("playground-sitemap.json")
    sitemap_cache.write_text(json.dumps({
        "schema": 1, "etag": '"fixture"', "last_modified": None, "retrieved": time.time() - _OLDER_THAN_TTL,
        "entries": [["123", "example game", "https://www.playground.ru/cheat/example-game-123"]],
    }), encoding="utf-8")
    network = _SequenceNetwork([_Response(403)])
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    with pytest.raises(ProviderChallenged):
        await service._playground(["example game"])
    assert len(network.calls) == 1


def test_an_attachment_link_has_to_be_this_providers_own(tmp_path):
    """A relative link resolves against the topic; an absolute one is a claim.

    Offered as a direct download, an absolute foreign or plain-HTTP attachment
    was a row the transport could only refuse once the user had chosen it.
    """
    import re as _re

    html = (FIXTURES / "fearless_current.html").read_text()
    href = _re.search(r'href="([^"]*download/file\.php[^"]*)"', html).group(1)
    for hostile in (
        "http://elsewhere.example/download/file.php?id=99",
        "https://elsewhere.example/download/file.php?id=99",
        "http://fearlessrevolution.com/download/file.php?id=99",
        "https://user@fearlessrevolution.com/download/file.php?id=99",
        "https://fearlessrevolution.com:444/download/file.php?id=99",
    ):
        with collect_parse_issues() as issues:
            rows = parse_phpbb_attachments(
                html.replace(href, hostile), provider="fearless",
                display_name="FearLess Cheat Engine", topic_id="10", topic_title="Example Game",
                source_page="https://fearlessrevolution.com/viewtopic.php?t=10", score=.9, rank=100,
                hosts=PROVIDER_HOSTS["fearless"],
            )
        assert rows == [], hostile
        assert issues.degraded == 1, hostile

    # The ordinary relative link is unaffected.
    with collect_parse_issues():
        rows = parse_phpbb_attachments(
            html, provider="fearless", display_name="FearLess Cheat Engine", topic_id="10",
            topic_title="Example Game", source_page="https://fearlessrevolution.com/viewtopic.php?t=10",
            score=.9, rank=100, hosts=PROVIDER_HOSTS["fearless"],
        )
    assert len(rows) == 1


def test_a_playground_sitemap_entry_has_to_be_playgrounds_own():
    # Matched by path shape alone, an entry from any origin became a cached
    # entry of this provider and its origin was then discarded, so a later
    # search matched a game that never existed here and failed at the host
    # boundary.
    with pytest.raises(ValueError, match="unexpected root"):
        parse_playground_sitemap(b'<notasitemap><loc>https://www.playground.ru/g/cheat/t-1</loc></notasitemap>')
    poisoned = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://elsewhere.example/g/cheat/table-1</loc></url>"
        b"<url><loc>http://www.playground.ru/g/cheat/table-2</loc></url>"
        b"<url><loc>https://www.playground.ru/g/cheat/table-3</loc></url>"
        b"</urlset>"
    )
    assert [row[0] for row in parse_playground_sitemap(poisoned)] == ["3"]


@pytest.mark.asyncio
async def test_a_challenged_sitemap_crawl_stops_the_provider(tmp_path):
    """The pages after a challenge are the same challenged site.

    Read as ordinary unreadable pages, the crawl asked for every one of them and
    then went on to fetch the posts it had found, which are that site too.
    """
    from ce_decky.network import ProviderChallenged

    index = (
        b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://www.thecheatscript.com/sitemap.xml?page=1</loc></sitemap>"
        b"<sitemap><loc>https://www.thecheatscript.com/sitemap.xml?page=2</loc></sitemap>"
        b"</sitemapindex>"
    )
    page = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://www.thecheatscript.com/2026/08/example-game.html</loc>"
        b"<lastmod>2026-08-01T00:00:00Z</lastmod></url></urlset>"
    )

    class _Net:
        def __init__(self, first_page_answers):
            self.first_page_answers = first_page_answers
            self.calls: list[str] = []

        async def get(self, url, **kwargs):
            self.calls.append(url)
            if url.endswith("/sitemap.xml"):
                return _Response(200, index)
            if "page=1" in url and self.first_page_answers:
                return _Response(200, page)
            return _Response(403)

    # Every page challenged: the provider fails with the status it answered.
    network = _Net(False)
    service = CatalogService(network, tmp_path / "a.json")  # type: ignore[arg-type]
    with pytest.raises(ProviderChallenged):
        await service._thecheatscript(["example game"])
    assert len(network.calls) == 2
    await service.close()

    # One page read, then challenged: unavailable, no post fetched, not cached.
    network = _Net(True)
    service = CatalogService(network, tmp_path / "b.json")  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        assert await service._thecheatscript(["example game"]) == []
    assert issues.unavailable is not None and "403" in issues.unavailable
    assert not any(url.endswith(".html") for url in network.calls)
    assert not (tmp_path / "thecheatscript-sitemap.json").exists()
    await service.close()


@pytest.mark.asyncio
async def test_an_unavailable_source_is_recorded_where_advanced_can_see_it(tmp_path):
    """The screen that says which source is failing reads what was written down.

    A source that stopped without setting a cooldown of its own wrote nothing at
    all, so the search said unavailable and the diagnostics said nothing.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    store.record_failure("vgtimes", error="HTTP 429", http_status=429, retry_after="600")
    deadline = store.snapshot()["providers"]["vgtimes"]["cooldown_until_epoch_s"]
    errors = store.snapshot()["providers"]["vgtimes"]["counters"]["errors"]

    service = CatalogService(None, tmp_path / "results.json", store)  # type: ignore[arg-type]

    async def challenged(self, *args):
        catalog_module._note_provider_unavailable("provider answered HTTP 403 for /files/cheats/tables/")
        return []

    async def empty(self, *args):
        return []

    service._vgtimes = types.MethodType(challenged, service)  # type: ignore[method-assign]
    for name in ("_fearless", "_playground", "_github_search", "_thecheatscript"):
        setattr(service, name, types.MethodType(empty, service))
    outcome = await service.search("Example")

    source = next(item for item in outcome["sources"] if item["provider"] == "vgtimes")
    assert source["status"] == "unavailable"
    state = store.snapshot()["providers"]["vgtimes"]
    assert state["counters"]["errors"] == errors + 1
    assert "403" in str(state["last_error"])
    # Never a deadline: this failure names none.
    assert state["cooldown_until_epoch_s"] == deadline
    await service.close()


@pytest.mark.asyncio
async def test_a_source_that_recorded_its_own_stop_is_not_recorded_twice(tmp_path):
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    service = CatalogService(None, tmp_path / "results.json", store)  # type: ignore[arg-type]

    async def already_recorded(self, *args):
        store.record_failure("fearless", error="HTTP 429", http_status=429, retry_after="600")
        catalog_module._note_provider_unavailable("HTTP 429 provider cooldown", recorded=True)
        return []

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(already_recorded, service)  # type: ignore[method-assign]
    for name in ("_playground", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))
    await service.search("Example")

    state = store.snapshot()["providers"]["fearless"]
    # One error, and the deadline the provider set is intact rather than
    # overwritten by a second record that names none.
    assert state["counters"]["errors"] == 1
    assert not store.can_attempt("fearless")
    await service.close()


@pytest.mark.asyncio
async def test_a_switched_off_source_is_never_asked_and_is_reported_as_off(tmp_path):
    # Skipped before its job is built, so nothing it owns is touched: no
    # request, no index and no cooldown. And named in the roster rather than
    # dropped from it, because a narrowed search and a build without that
    # source produce the same empty answer otherwise.
    service = CatalogService(
        None, tmp_path / "results.json",  # type: ignore[arg-type]
        disabled_providers=lambda: {"github", "vgtimes"},
    )
    asked: list[str] = []

    def stub(name):
        async def run(self, *args):
            asked.append(name)
            return []
        return run

    for name in ("_fearless", "_playground", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(stub(name), service))
    outcome = await service.search("Example")

    assert "_github_search" not in asked and "_vgtimes" not in asked
    assert sorted(asked) == ["_fearless", "_playground", "_thecheatscript"]
    off = {row["provider"]: row for row in outcome["sources"] if row["status"] == "disabled"}
    assert set(off) == {"github", "vgtimes"}
    assert off["github"]["error"] is None
    assert not outcome["failures"]
    await service.close()


@pytest.mark.asyncio
async def test_a_switched_off_source_is_not_read_when_another_page_names_it(tmp_path):
    # The route that made switching one off incomplete: a Playground page can
    # name the exact GitHub repository a table lives in, so GitHub was still
    # contacted because something else pointed at it.
    service = CatalogService(
        None, tmp_path / "results.json",  # type: ignore[arg-type]
        disabled_providers=lambda: {"github"},
    )
    read = False

    async def read_github():
        nonlocal read
        read = True
        return []

    assert await service._linked_source("github", read_github) == []
    assert read is False
    # The source that was left on is still followed through the same route.
    assert await service._linked_source("fearless", read_github) == []
    assert read is True
    await service.close()


def test_a_switched_off_source_stops_answering_out_of_the_search_cache(tmp_path):
    path = tmp_path / "results.json"
    service = CatalogService(None, path)  # type: ignore[arg-type]
    rows = [
        ArtifactRecord(CatalogResult(
            provider=provider, provider_display_name=provider, topic_id="1",
            artifact_id=f"{provider}-1", table_title="Example", filename="Example.CT",
            version=None, size_bytes=5, source_page=f"https://{provider}.example.com/t/1",
            download_mode="source_handoff", match_score=.9, provider_rank=10,
        ))
        for provider in ("fearless", "github")
    ]
    service._save_cache("Example", rows)

    assert {row.result.provider for row in service._load_cache("Example")} == {"fearless", "github"}
    off = CatalogService(
        None, path, disabled_providers=lambda: {"github"},  # type: ignore[arg-type]
    )
    assert {row.result.provider for row in off._load_cache("Example")} == {"fearless"}


def test_a_row_from_a_switched_off_source_can_no_longer_start_a_download(tmp_path):
    # A search snapshot outlives the screen it was made for: Advanced is opened
    # from the same panel, so a row already on screen must not still resolve.
    service = CatalogService(
        None, tmp_path / "results.json",  # type: ignore[arg-type]
        disabled_providers=lambda: {"github"},
    )
    record = ArtifactRecord(CatalogResult(
        provider="github", provider_display_name="GitHub", topic_id="owner/repo",
        artifact_id="github:owner/repo:1", table_title="Example", filename="Example.CT",
        version=None, size_bytes=5, source_page="https://github.com/owner/repo",
        download_mode="direct_https", match_score=.9, provider_rank=80,
    ))
    service.artifacts = {("github", record.result.artifact_id): record}
    with pytest.raises(ValueError) as refused:
        service.resolve("github", record.result.artifact_id)
    assert "switched off" in str(refused.value)


@pytest.mark.asyncio
async def test_an_unreadable_source_choice_searches_everything_rather_than_nothing(tmp_path):
    # Fail-open on purpose, exactly as an unreadable blocklist refuses nothing:
    # reading a corrupt preference as "every source is off" leaves a search
    # finding nothing at all with no visible cause.
    def broken():
        raise ValueError("provider source selection has an unsupported or corrupt schema")

    service = CatalogService(
        None, tmp_path / "results.json", disabled_providers=broken,  # type: ignore[arg-type]
    )
    asked: list[str] = []

    def stub(name):
        async def run(self, *args):
            asked.append(name)
            return []
        return run

    for name in ("_fearless", "_playground", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(stub(name), service))
    outcome = await service.search("Example")
    assert len(asked) == 5
    assert not any(row["status"] == "disabled" for row in outcome["sources"])
    await service.close()


@pytest.mark.asyncio
async def test_the_background_listing_crawl_stops_when_its_source_is_switched_off(tmp_path):
    """The crawl outlives the search that started it, so the switch has to reach it.

    It walks the forum's listing pages with a pause between each and sleeps
    through any cooldown the forum asks for, so checking the choice only where
    the search job is built would leave the one source the user has just
    switched off being requested for minutes afterwards.
    """
    network = _FearlessIndexNetwork()
    off = False
    service = CatalogService(
        network, tmp_path / "results.json",  # type: ignore[arg-type]
        disabled_providers=lambda: {"fearless"} if off else set(),
    )
    rows = await service._fearless(["example game"])
    assert rows and service._fearless_index_task is not None
    assert service.fearless_index_status()["indexed_pages"] == 1

    # Switched off while the crawl still owes a page.
    off = True
    await service._fearless_index_task
    assert service.fearless_index_status()["indexed_pages"] == 1
    assert not any("start=50" in url for url, _ in network.calls)
    # And it is not started again by a later search either.
    before = len(network.calls)
    service._start_fearless_index()
    assert service._fearless_index_task is None or service._fearless_index_task.done()
    assert len(network.calls) == before
    await service.close()


@pytest.mark.asyncio
async def test_rows_a_linked_page_produced_are_counted_against_the_source_that_served_them(tmp_path):
    """One Playground row plus two GitHub rows is not three Playground results.

    The rows carry GitHub's identity, are downloaded from GitHub and are refused
    when GitHub is switched off, so crediting them to the source that merely
    linked them corrupted the one screen that says what each source is doing and
    offers to switch it off.
    """
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    service = CatalogService(None, tmp_path / "results.json", store)  # type: ignore[arg-type]

    def row(provider: str, artifact: str) -> ArtifactRecord:
        return ArtifactRecord(CatalogResult(
            provider=provider, provider_display_name=provider, topic_id="1",
            artifact_id=artifact, table_title="Example", filename="Example.CT",
            version=None, size_bytes=5, source_page=f"https://{provider}.example.com/t/1",
            download_mode="source_handoff", match_score=.9, provider_rank=10,
        ))

    async def playground(self, *args):
        linked = await self._linked_source(
            "github", lambda: _resolved([row("github", "gh-1"), row("github", "gh-2")]),
        )
        return [row("playground", "pg-1"), *linked]

    async def empty(self, *args):
        return []

    service._playground = types.MethodType(playground, service)  # type: ignore[method-assign]
    for name in ("_fearless", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))
    await service.search("Example")

    providers = store.snapshot()["providers"]
    assert providers["playground"]["counters"]["results"] == 1
    assert providers["playground"]["counters"]["searches"] == 1
    # GitHub's own search ran in this test and returned nothing, so both of
    # these rows provably came from the page Playground linked - and they are
    # counted as a linked read rather than as a second search, because no
    # second search of GitHub happened.
    assert providers["github"]["counters"]["searches"] == 1
    assert providers["github"]["counters"]["results"] == 2
    assert providers["github"]["counters"]["linked_reads"] == 1
    await service.close()


async def _resolved(rows):
    return rows


@pytest.mark.asyncio
async def test_a_page_read_through_a_link_reports_its_own_unreadable_pages(tmp_path):
    from ce_decky.providers import ProviderDiagnosticsStore

    store = ProviderDiagnosticsStore(tmp_path / "providers.json")
    service = CatalogService(None, tmp_path / "results.json", store)  # type: ignore[arg-type]

    def row(provider: str) -> ArtifactRecord:
        return ArtifactRecord(CatalogResult(
            provider=provider, provider_display_name=provider, topic_id="1",
            artifact_id=f"{provider}-1", table_title="Example", filename="Example.CT",
            version=None, size_bytes=5, source_page=f"https://{provider}.example.com/t/1",
            download_mode="source_handoff", match_score=.9, provider_rank=10,
        ))

    async def read_github():
        catalog_module._note_parse_issue("repository tree unreadable", critical=True)
        return [row("github")]

    async def playground(self, *args):
        return [row("playground"), *await self._linked_source("github", read_github)]

    async def empty(self, *args):
        return []

    service._playground = types.MethodType(playground, service)  # type: ignore[method-assign]
    for name in ("_fearless", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))
    await service.search("Example")

    providers = store.snapshot()["providers"]
    assert providers["github"]["counters"]["parse_failed"] == 1
    assert providers["playground"]["counters"]["parse_failed"] == 0
    await service.close()


@pytest.mark.asyncio
async def test_a_linked_read_of_a_provider_no_page_may_name_is_refused(tmp_path):
    # A route the registry never declared is a defect here, not an outage
    # there, and the screen that says what switching a source off stops is
    # built from that declaration.
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    read = False

    async def read_it():
        nonlocal read
        read = True
        return []

    assert await service._linked_source("vgtimes", read_it) == []
    assert read is False
    await service.close()


@pytest.mark.asyncio
async def test_one_search_follows_links_under_the_selection_it_began_with(tmp_path):
    """A switch pressed mid-search must not change what that search reads.

    The roster it returns describes the selection it started with, so acting on
    a newer one halfway through would make the answer describe a set of sources
    that is not the set it actually read.
    """
    off: set[str] = set()
    service = CatalogService(
        None, tmp_path / "results.json",  # type: ignore[arg-type]
        disabled_providers=lambda: set(off),
    )
    read = False

    async def read_github():
        nonlocal read
        read = True
        return []

    async def playground(self, *args):
        # The user switches GitHub off while this search is still running.
        off.add("github")
        return await self._linked_source("github", read_github)

    async def empty(self, *args):
        return []

    service._playground = types.MethodType(playground, service)  # type: ignore[method-assign]
    for name in ("_fearless", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))
    outcome = await service.search("Example")

    assert read is True
    assert {row["provider"] for row in outcome["sources"] if row["status"] == "disabled"} == set()
    # The next search is the one that observes it.
    assert service.disabled_provider_ids() == frozenset({"github"})
    await service.close()


@pytest.mark.asyncio
async def test_a_search_in_flight_cannot_rebuild_the_cache_a_deletion_removed(tmp_path):
    """Closing the Search screen does not stop the search behind it.

    Deleting the provider cache promises what is on disk goes and is rebuilt by
    the next search. A search still running when the files went wrote the
    results cache straight back, so the rebuild was from the search before the
    deletion.
    """
    cache = tmp_path / "results.json"
    service = CatalogService(None, cache)  # type: ignore[arg-type]
    started = asyncio.Event()
    release = asyncio.Event()

    def row() -> ArtifactRecord:
        return ArtifactRecord(CatalogResult(
            provider="fearless", provider_display_name="FearLess Cheat Engine", topic_id="1",
            artifact_id="topic-1:attachment-1", table_title="Example", filename="Example.CT",
            version=None, size_bytes=5, source_page="https://fearlessrevolution.com/viewtopic.php?t=1",
            download_mode="source_handoff", match_score=.9, provider_rank=100,
        ))

    async def slow(self, *args):
        started.set()
        await release.wait()
        return [row()]

    async def empty(self, *args):
        return []

    service._fearless = types.MethodType(slow, service)  # type: ignore[method-assign]
    for name in ("_playground", "_github_search", "_thecheatscript", "_vgtimes"):
        setattr(service, name, types.MethodType(empty, service))

    running = asyncio.create_task(service.search("Example"))
    await started.wait()
    await service.suspend_searches()
    assert running.done()
    assert not cache.exists()
    # And nothing new starts while the deletion holds.
    with pytest.raises(ValueError, match="being deleted"):
        await service.search("Example")
    service.resume_searches()
    release.set()
    await service.close()


@pytest.mark.asyncio
async def test_the_listing_goes_on_refreshing_after_a_search_without_another_one(tmp_path, monkeypatch):
    """A pass used to exist only for as long as somebody was searching.

    Every pass was started from inside a search, so a device whose owner plays
    rather than searches indexed nothing at all: on the maintainer's machine the
    listing stood at 97 of 333 pages with the oldest of them 21 hours old, which
    is a page ageing out for every page a search happened to add. One search now
    buys a window in which the pass walks whatever comes due, with nobody at the
    screen for any of it.
    """
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_SECONDS", 0.0)
    network = _FearlessIndexNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]

    # An index this process already holds the shape of, with both of its pages
    # owed. Learning that shape stays a search's job: until one has read the
    # listing once, nothing here knows the forum's pagination, and reading it to
    # find out would ask a third party for pages on behalf of a user who has not
    # asked for anything.
    service._fearless_total_pages = 2
    service._fearless_page_step = 50
    service.start_background_index()
    await asyncio.sleep(0.1)
    assert network.calls == []

    # The search that arms it. Nothing else about the pass changes.
    service._note_search_activity()
    for _ in range(400):
        if service.fearless_index_status()["indexed_pages"] >= 2:
            break
        await asyncio.sleep(0.01)
    assert service.fearless_index_status()["indexed_pages"] == 2
    assert any("start=50" in url for url, _ in network.calls)
    # Nobody searched for any of it.
    assert not service._search_tasks
    await service.close()


@pytest.mark.asyncio
async def test_a_device_nobody_searches_on_asks_the_forum_for_nothing(tmp_path, monkeypatch):
    """The other half of arming it, and the reason the arming exists.

    A plugin is loaded for as long as Steam is running, and a user may not open
    Search for months. On a timer alone that device would ask this forum for its
    whole listing every day, for ever, for an index nothing is going to read.
    """
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_QUIET_SECONDS", 0.0)
    network = _FearlessIndexNetwork()
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    service._fearless_total_pages = 2
    service._fearless_page_step = 50

    # Searched, but longer ago than the window the search bought.
    service._searched_at = time.time() - (catalog_module.FEARLESS_INDEX_ACTIVE_WINDOW_SECONDS + 60)
    service.start_background_index()
    await asyncio.sleep(0.2)

    assert network.calls == []
    assert service.fearless_index_status()["indexed_pages"] == 0
    await service.close()


@pytest.mark.asyncio
async def test_a_search_after_a_long_gap_reads_a_wider_front_of_the_listing(tmp_path, unpaced_crawl):
    """The first search after the crawl has been asleep for a while.

    A listing sorted by activity puts new and freshly bumped topics at the
    front, so after a gap the front is both the stalest part of the index and
    the part most likely to hold what is being searched for now. The pass this
    search arms brings the rest up behind it.
    """
    network = _DeepFearlessNetwork(pages=12)
    service = CatalogService(network, tmp_path / "results.json")  # type: ignore[arg-type]
    await service._fearless(["nothing matches this"])
    # Read before the pass it armed has run, so this is the search's own cost.
    ordinary = len(network.listings())
    assert service._fearless_index_task is not None
    await service._fearless_index_task

    # Every page last read well before the window this rule is about.
    stale = time.time() - (catalog_module.FEARLESS_STALE_REFRESH_AFTER_SECONDS + 60)
    for start in list(service._fearless_fetched):
        service._fearless_fetched[start] = stale
    since = len(network.calls)
    await service._fearless(["nothing matches this"])

    # Page zero plus seven more, inside the search rather than behind it.
    assert network.listings(since)[:catalog_module.FEARLESS_STALE_REFRESH_PAGES] == [
        index * 50 for index in range(catalog_module.FEARLESS_STALE_REFRESH_PAGES)
    ]
    assert ordinary < catalog_module.FEARLESS_STALE_REFRESH_PAGES
    if service._fearless_index_task is not None:
        await service._fearless_index_task
    await service.close()


@pytest.mark.asyncio
async def test_the_background_schedule_stops_with_the_plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS", 30.0)
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    service.start_background_index()
    task = service._index_schedule_task
    assert task is not None and not task.done()
    # Starting again adopts the one that is already running.
    service.start_background_index()
    assert service._index_schedule_task is task
    await service.close()
    assert task.done()


def test_close_never_waits_on_a_task_whose_loop_is_already_gone(tmp_path, monkeypatch):
    """A synthetic case: a service closed from a loop that did not start it.

    Not the host's shape. Decky gives a plugin process one event loop and keeps
    it for the life of that process, so nothing here crosses loops in
    production; `docs/FIELD_NOTES.md` carries the upstream source and the device
    reading that establish it, and an earlier version of this docstring claimed
    the opposite and cost this project two false review findings.

    What it guards is the weaker property that makes this service usable from a
    test, a helper or a probe: a task that is terminal, or that belongs to some
    other loop, is never handed to `asyncio.gather` during close. Doing so asks
    that other loop to schedule a callback, which on Python 3.11 raises out of
    `close()`; 3.13 has a shortcut for an already-done future and hides it, so
    what is asserted is the rule rather than the exception.
    """
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS", 30.0)
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]

    async def load() -> None:
        service.start_background_index()
        # A provider pass of the load's own, so the index task crosses the same
        # boundary as the scheduler rather than only the scheduler doing so.
        service._fearless_index_task = asyncio.create_task(asyncio.sleep(30))

    asyncio.run(load())
    schedule = service._index_schedule_task
    index = service._fearless_index_task
    assert schedule is not None and schedule.done()
    assert index is not None and index.done()

    waited: list[object] = []
    real_gather = asyncio.gather

    def spy(*awaitables, **kwargs):
        waited.extend(awaitables)
        return real_gather(*awaitables, **kwargs)

    monkeypatch.setattr(catalog_module.asyncio, "gather", spy)

    asyncio.run(service.close())

    assert schedule not in waited
    assert index not in waited
    assert all(not getattr(item, "done", bool)() for item in waited)
    assert service._index_schedule_task is None


@pytest.mark.asyncio
async def test_a_search_is_durable_before_anything_stops_the_plugin(tmp_path, monkeypatch):
    """The arming is on disk at the search, not at the stop that may never come.

    The marker used to be owed to a write of something else: the index cache is
    a third of a megabyte, so it carried the marker at most once an hour, and a
    search that finds the index already complete and fresh starts no crawl that
    would carry it there either. Until the scheduler's first tick, three minutes
    after load, the arming existed only in memory, and an unload is not a
    promise: a backend that is killed, runs out of memory or loses power never
    runs one.

    So the search writes its own small file, and this asserts the state after
    that write and nothing else: no close, no prologue, no tick.
    """
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS", 30.0)
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    # A complete index, every page fresh, so the search this is about has
    # nothing to crawl and starts no pass of its own.
    now = time.time()
    service._fearless_total_pages = 2
    service._fearless_page_step = 50
    service._fearless_pages = {0: [("1", "A topic")], 50: [("2", "Another topic")]}
    service._fearless_fetched = {0: now, 50: now}
    # Last searched well outside the window, and that is what is on disk.
    service._searched_at = time.time() - (catalog_module.FEARLESS_INDEX_ACTIVE_WINDOW_SECONDS + 3600)
    async with service._fearless_index_lock:
        await service._persist_fearless_index()
    assert not CatalogService(None, tmp_path / "results.json")._index_is_armed()  # type: ignore[arg-type]

    # The search, and then nothing at all: this is the process that is about to
    # be killed without an unload.
    service._note_search_activity()
    service.start_background_index()

    reloaded = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    assert reloaded._index_is_armed()
    assert reloaded._fearless_total_pages == 2
    assert reloaded._pending_fearless_page_starts() == []
    # Nothing was owed to the stop, so an orderly close writes nothing more.
    assert service._searched_persisted_at == service._searched_at
    # And the index itself was not rewritten for a timestamp.
    index = json.loads((tmp_path / "fearless-index.json").read_text(encoding="utf-8"))
    assert index["searched"] < reloaded._searched_at

    await service.close()


@pytest.mark.asyncio
async def test_a_marker_the_search_could_not_write_is_offered_again(tmp_path, monkeypatch):
    """A full device fails no search, and does not silently disarm the pass.

    The write is a few dozen bytes and it still happens on somebody's storage.
    When it fails the search must not, the arming stays owed in memory, and the
    thing that offers it again is the scheduler's next tick five minutes later.
    """
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    service._fearless_total_pages = 2
    service._fearless_page_step = 50
    real_persist = service._persist_search_marker

    monkeypatch.setattr(service, "_persist_search_marker", _full_disk)
    service._note_search_activity()
    searched = service._searched_at

    assert service._searched_persisted_at < searched
    assert not (tmp_path / catalog_module.FEARLESS_SEARCH_MARKER_NAME).exists()

    # The device recovers, and the tick is what puts it on disk.
    monkeypatch.setattr(service, "_persist_search_marker", real_persist)
    await service._persist_search_activity()

    assert service._searched_persisted_at == searched
    assert CatalogService(None, tmp_path / "results.json")._index_is_armed()  # type: ignore[arg-type]


def test_the_search_marker_is_written_with_no_event_loop_at_all(tmp_path, monkeypatch):
    """Deliberately not a coroutine, because the guarantee is that it is not one.

    The search writes its own marker, so what the unload prologue is left with
    is the search whose write failed: the device was full for that moment, the
    arming is owed in memory, and the scheduler tick that would offer it again
    in five minutes is being stopped. An unload on this host may never get past
    its first suspension point, `docs/FIELD_NOTES.md` records why, so this write
    happens in the calling thread before anything can await, and this case
    proves it by running it where there is no loop to await on at all.
    """
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    now = time.time()
    service._fearless_total_pages = 2
    service._fearless_page_step = 50
    service._fearless_pages = {0: [("1", "A topic")], 50: [("2", "Another topic")]}
    service._fearless_fetched = {0: now, 50: now}
    service._searched_at = time.time() - (catalog_module.FEARLESS_INDEX_ACTIVE_WINDOW_SECONDS + 3600)
    asyncio.run(_persist(service))
    assert not CatalogService(None, tmp_path / "results.json")._index_is_armed()  # type: ignore[arg-type]

    # The search, and the one moment its own write could not land.
    real_persist = service._persist_search_marker
    monkeypatch.setattr(service, "_persist_search_marker", _full_disk)
    service._note_search_activity()
    monkeypatch.setattr(service, "_persist_search_marker", real_persist)
    assert service._searched_persisted_at < service._searched_at

    service.begin_close()

    reloaded = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    assert reloaded._index_is_armed()
    # And it is not owed twice: the orderly close finds nothing left to write.
    assert service._searched_persisted_at == service._searched_at


def _full_disk() -> bool:
    raise OSError("No space left on device")


async def _persist(service) -> None:
    async with service._fearless_index_lock:
        await service._persist_fearless_index()


@pytest.mark.asyncio
async def test_an_older_index_write_never_replaces_a_newer_one(tmp_path):
    """Two native writers, and only one of them may be last.

    A crawl writes this cache from a worker thread while the loop stays free,
    and cancelling that worker's awaiter does not stop the worker: unload
    cancels the crawl and then writes the index itself, so a second worker can
    be started while the first is still inside its own `fsync`. Atomic
    replacement makes each write whole and says nothing about which lands last,
    so the older worker could put its snapshot back over the newer one and take
    the search marker with it.
    """
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    now = time.time()
    service._fearless_total_pages = 2
    service._fearless_page_step = 50
    service._fearless_pages = {0: [("1", "A topic")], 50: [("2", "Another topic")]}
    service._fearless_fetched = {0: now, 50: now}
    service._searched_at = now - 7200
    await _persist(service)

    # The first worker takes its snapshot, and is then held before it writes.
    released = threading.Event()
    real_write = service._write_index_snapshot

    def held(payload, generation):
        assert released.wait(timeout=5)
        return real_write(payload, generation)

    old_payload, old_generation = service._fearless_index_snapshot()
    worker = asyncio.create_task(asyncio.to_thread(held, old_payload, old_generation))
    await asyncio.sleep(0)

    # A search lands while that worker is still holding its older snapshot, and
    # a second write takes a newer one and completes first.
    service._note_search_activity()
    searched = service._searched_at
    await _persist(service)

    released.set()
    # The older snapshot is dropped rather than written, and the marker on disk
    # is still the one the newer write carried.
    assert await worker is False
    assert service._searched_persisted_at == searched

    reloaded = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    assert reloaded._searched_at == searched
    assert reloaded._index_is_armed()


@pytest.mark.asyncio
async def test_the_marker_believed_is_the_one_the_bytes_carry(tmp_path, monkeypatch):
    """A search during the write must not be recorded as already durable.

    The cursor used to be read from memory after the worker returned, so a
    search that advanced it mid-write was believed to be on disk when the bytes
    that landed carried the older value. The next reload would then find the
    older marker with nothing owing it.
    """
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    now = time.time()
    service._fearless_total_pages = 2
    service._fearless_page_step = 50
    service._fearless_pages = {0: [("1", "A topic")], 50: [("2", "Another topic")]}
    service._fearless_fetched = {0: now, 50: now}
    service._searched_at = now - 7200
    written = service._searched_at

    real_write = service._write_index_snapshot

    def advance_then_write(payload, generation):
        # The search lands while the bytes for the older marker are going down.
        service._searched_at = time.time()
        return real_write(payload, generation)

    monkeypatch.setattr(service, "_write_index_snapshot", advance_then_write)
    async with service._fearless_index_lock:
        await service._persist_fearless_index()

    assert service._searched_persisted_at == written
    assert service._searched_at > service._searched_persisted_at


@pytest.mark.asyncio
async def test_a_search_survives_an_unload_a_cache_write_was_holding(tmp_path):
    """The one contention the prologue has to carry a search across.

    An older native writer is inside `atomic_write_json` holding the lock that
    orders the index cache, and a search lands after it took its snapshot. The
    marker used to ride only in that cache, so the search had nowhere to put it:
    the unload prologue cannot wait for that lock, because it is spending
    Decky's five seconds and an owned Cheat Engine comes first. It tried anyway,
    gave up, and left the arming owed in memory only, and there is no memory
    afterwards: real Decky starves the loop after the first await and SIGKILL
    follows, so the asynchronous repair never ran. The older writer then landed
    its own older marker and the next process read that.

    The marker has its own small file now, which that lock does not guard, and
    the search writes it as it happens. The next process takes the newer of the
    two records.
    """
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    now = time.time()
    service._fearless_total_pages = 2
    service._fearless_page_step = 50
    service._fearless_pages = {0: [("1", "A topic")], 50: [("2", "Another topic")]}
    service._fearless_fetched = {0: now, 50: now}
    # An hour inside the window the last search armed, so the reload below turns
    # on this search rather than on the recorded one.
    service._searched_at = now - (catalog_module.FEARLESS_INDEX_ACTIVE_WINDOW_SECONDS - 3600)
    await _persist(service)
    recorded = service._searched_at

    # The older writer takes its snapshot and is then held inside the write,
    # with the physical lock that orders the cache in its hands.
    released = threading.Event()
    real_write = service._write_index_snapshot

    def held(payload, generation):
        assert released.wait(timeout=5)
        return real_write(payload, generation)

    old_payload, old_generation = service._fearless_index_snapshot()
    worker = asyncio.create_task(asyncio.to_thread(held, old_payload, old_generation))
    await asyncio.sleep(0)
    service._index_write_lock.acquire()
    try:
        # The user searches again, and Decky stops the plugin.
        service._note_search_activity()
        searched = service._searched_at
        started = time.monotonic()
        service.begin_close()
        waited = time.monotonic() - started
    finally:
        service._index_write_lock.release()

    # Bounded: the prologue never waits on the cache lock at all.
    assert waited < 1.0

    # The older writer now lands its own snapshot, carrying the older marker,
    # and nothing else runs: this is the real stop, so there is no `close()`.
    released.set()
    assert await worker is True
    index = json.loads((tmp_path / "fearless-index.json").read_text(encoding="utf-8"))
    assert index["searched"] == recorded

    # A fresh process, which is all that is left after SIGKILL.
    reloaded = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    assert reloaded._searched_at == searched
    assert reloaded._index_is_armed()
    # And the day it is armed for is counted from the search, not from the
    # older moment the index cache still carries.
    assert reloaded._searched_at > recorded


def test_the_crawls_own_pacing_survives_a_clock_correction(monkeypatch, tmp_path):
    """Both gates are elapsed time inside this process, and this clock is corrected.

    A pass is paced thirty minutes from the last one and gives way for ten
    seconds after a search. Measured on the wall clock, an hour moved backwards
    holds the next pass for ninety minutes of real time and makes a search that
    finished an hour ago look like one still running; an hour forwards opens
    both at once, and the crawl asks the provider for pages beside the search it
    is supposed to be giving way to.
    """
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    elapsed = [1000.0]
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: elapsed[0])

    wall = [1_700_000_000.0]
    monkeypatch.setattr(catalog_module.time, "time", lambda: wall[0])
    service._last_search_at = elapsed[0]
    service._fearless_index_ran_at = elapsed[0]

    for correction in (-3600.0, 7200.0):
        wall[0] += correction
        # Nothing has actually elapsed, so neither gate has moved.
        assert service._searches_are_quiet() is False
        assert service._index_pass_is_paced() is True

    # A second short of each bound, still shut.
    elapsed[0] += catalog_module.FEARLESS_INDEX_QUIET_SECONDS - 1
    assert service._searches_are_quiet() is False
    elapsed[0] += 1
    assert service._searches_are_quiet() is True

    elapsed[0] = 1000.0 + catalog_module.FEARLESS_INDEX_RUN_INTERVAL_SECONDS - 1
    assert service._index_pass_is_paced() is True
    elapsed[0] += 1
    assert service._index_pass_is_paced() is False


@pytest.mark.asyncio
async def test_one_failed_tick_does_not_end_the_background_schedule(tmp_path, monkeypatch):
    """A plugin is loaded for as long as Steam runs, and so is this timer.

    The whole loop used to sit in one `try`, so the first exception to reach it
    ended the coroutine for the rest of the session: nothing restarts the
    scheduler until the next plugin load. One full disk during a marker write
    would have turned "refresh the listing on a timer" off until the device was
    rebooted, and said so in one log line nobody was reading.
    """
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_FIRST_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(catalog_module, "FEARLESS_INDEX_SCHEDULE_INTERVAL_SECONDS", 0.01)
    service = CatalogService(None, tmp_path / "results.json")  # type: ignore[arg-type]
    ticks: list[int] = []

    async def fail_once() -> None:
        ticks.append(len(ticks))
        if len(ticks) == 1:
            raise OSError("No space left on device")

    started: list[int] = []
    monkeypatch.setattr(service, "_persist_search_activity", fail_once)
    monkeypatch.setattr(service, "_start_fearless_index", lambda: started.append(len(started)))
    service.start_background_index()
    schedule = service._index_schedule_task
    assert schedule is not None

    for _ in range(400):
        if len(ticks) >= 3:
            break
        await asyncio.sleep(0.01)

    # The failing tick is one tick, and the ones after it happen.
    assert len(ticks) >= 3
    assert not schedule.done()
    # Including the half a search actually waits on: a later tick still asks
    # for a pass, rather than only surviving to sleep again.
    assert len(started) >= 3

    # And cancellation is still what ends it.
    await service.close()
    assert schedule.done()
