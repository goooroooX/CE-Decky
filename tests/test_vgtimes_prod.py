from __future__ import annotations

import base64
from dataclasses import replace
import gzip
import json
from pathlib import Path

import pytest

from ce_decky.catalog import CatalogService, collect_parse_issues, vgtimes_record
from ce_decky.network import NetworkError, ProviderRateLimited
from ce_decky.providers import provider_definition
from ce_decky.vgtimes import (
    ASSET_HOSTS,
    MAX_SITEMAP_BYTES,
    BUSY_RETRY_SECONDS,
    CHALLENGE_BUSY_CODE,
    CHALLENGE_MISSING_CODE,
    PROVIDER_ID,
    ChallengeError,
    DownloadRefused,
    GameRef,
    ItemRef,
    VGTimesDownloadRequest,
    base_js_url,
    build_download_values,
    build_request,
    client_module_url,
    decompress,
    download_url,
    game_sitemap_urls,
    is_provider_file_host,
    item_url,
    parse_challenge_answer,
    parse_client_contract,
    parse_download_identity,
    parse_file_hosts,
    parse_game_sitemap,
    parse_item,
    parse_key_derivation,
    parse_language_strings,
    parse_page_bootstrap,
    parse_refusal_contract,
    parse_sitemap_index,
    parse_tables_listing,
    parse_user_ip,
    parse_waiting_page,
    probe_slot,
    retry_delay_for,
    retry_needs_new_token,
    solve_challenge,
    tables_listing_url,
    timing_elapsed_is_seconds_on_page,
    unpack_packed_script,
)


FIXTURES = Path(__file__).parent / "fixtures"
ITEM = "https://vgtimes.ru/games/1666-amsterdam/files/97634-x.html"


def item_html() -> str:
    return (FIXTURES / "vgtimes_item.html").read_text(encoding="utf-8")


def module_js() -> str:
    return (FIXTURES / "vgtimes_files_module.js").read_text(encoding="utf-8")


def challenge() -> dict:
    return json.loads((FIXTURES / "vgtimes_challenge.json").read_text(encoding="utf-8"))


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class _RouteNetwork:
    """Answers by route, because this chain is several different requests."""

    def __init__(self, routes, posts=None):
        self.routes = routes
        self.posts = posts or []
        self.calls: list[str] = []
        self.sent: list[tuple[str, dict]] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        for fragment, response in self.routes:
            if fragment in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")

    async def post(self, url, *, data, **kwargs):
        self.calls.append(url)
        self.sent.append((url, dict(data)))
        for fragment, responses in self.posts:
            if fragment in url:
                return responses.pop(0) if isinstance(responses, list) else responses
        raise AssertionError(f"unexpected POST: {url}")


# --- discovery -------------------------------------------------------------


def test_the_game_shards_are_selected_from_the_declared_index():
    index = parse_sitemap_index(
        b"<sitemapindex>"
        b"<sitemap><loc>https://vgtimes.ru/sitemaps/ru/games_sitemap_1.xml.gz</loc></sitemap>"
        b"<sitemap><loc>https://vgtimes.ru/sitemaps/ru/posts_sitemap_1.xml.gz</loc></sitemap>"
        b"<sitemap><loc>https://elsewhere.example/games_sitemap_9.xml.gz</loc></sitemap>"
        b"</sitemapindex>"
    )
    # A shard on another host is not this provider's sitemap, whatever it is named.
    assert index == ("https://vgtimes.ru/sitemaps/ru/games_sitemap_1.xml.gz",
                     "https://vgtimes.ru/sitemaps/ru/posts_sitemap_1.xml.gz")
    assert game_sitemap_urls(index) == ("https://vgtimes.ru/sitemaps/ru/games_sitemap_1.xml.gz",)


def test_shards_are_read_gzipped_or_plain():
    plain = "<urlset><loc>https://vgtimes.ru/games/x</loc></urlset>"
    assert decompress(plain.encode()) == plain
    assert decompress(gzip.compress(plain.encode())) == plain


def test_a_shard_that_did_not_finish_is_not_a_smaller_shard():
    # A truncated stream still yields whole `<loc>` elements up to the cut, so
    # the shard parses, the crawl counts it as read, and the partial game index
    # is cached for hours.
    whole = gzip.compress(
        b"<urlset><loc>https://vgtimes.ru/games/a/</loc><loc>https://vgtimes.ru/games/b/</loc></urlset>"
    )
    assert "games/b" in decompress(whole)
    with pytest.raises(ValueError, match="incomplete gzip"):
        decompress(whole[:-12])


def test_a_shard_is_expanded_within_the_bound_the_transfer_was_given():
    # The transfer is bounded; decompressing all of it and measuring afterwards
    # made that bound meaningless. 400 KB on the wire allocated 400 MB before
    # anything refused it, on a device where that matters.
    bomb = gzip.compress(b"\0" * (MAX_SITEMAP_BYTES * 4))
    assert len(bomb) < 1024 * 1024
    with pytest.raises(ValueError, match="byte limit"):
        decompress(bomb)


def test_game_slugs_come_from_the_sitemap_and_exclude_sub_pages():
    games = parse_game_sitemap((FIXTURES / "vgtimes_games_sitemap.xml").read_text())
    slugs = [game.slug for game in games]
    assert "atomic-heart" in slugs and "1666-amsterdam" in slugs
    # `/games/<slug>/files/` is not a game, and must not become one.
    assert all("/" not in slug for slug in slugs)
    assert len(slugs) == len(set(slugs))
    assert GameRef("atomic-heart", "u").title == "Atomic Heart"


def test_a_slug_is_validated_before_it_becomes_a_route():
    assert tables_listing_url("atomic-heart").endswith("/games/atomic-heart/files/cheats/tables/")
    for bad in ("", "../x", "Atomic Heart", "a/b", "-leading"):
        with pytest.raises(ValueError):
            tables_listing_url(bad)
    with pytest.raises(ValueError):
        item_url("atomic-heart", "not-a-number", "some-table")


def test_the_listing_yields_this_games_entries_only():
    html = (FIXTURES / "vgtimes_tables_listing.html").read_text()
    refs = parse_tables_listing(html, "atomic-heart")
    assert refs and all(ref.game_slug == "atomic-heart" for ref in refs)
    assert len({ref.file_id for ref in refs}) == len(refs)
    assert parse_tables_listing(html, "other-game") == ()


def test_an_entry_keeps_the_url_the_listing_published():
    # The site resolves an entry by its id whatever name follows, so a
    # placeholder worked and was still the wrong URL to keep: this is the source
    # page a row shows and the origin the imported table records, and provenance
    # that does not match the page it was read from cannot be followed back.
    refs = parse_tables_listing((FIXTURES / "vgtimes_tables_listing.html").read_text(), "atomic-heart")
    assert [ref.url for ref in refs] == [
        "https://vgtimes.ru/games/atomic-heart/files/63820-tablica-dlja-cheat-engine-upd-11-08-2023.html",
        "https://vgtimes.ru/games/atomic-heart/files/97512-tablica-dlja-cheat-engine-1-16-3-0.html",
    ]
    assert item_url("atomic-heart", "1", "some-table").endswith("/files/1-some-table.html")
    for bad_name in ("", "../x", "Upper", "a" * 200):
        with pytest.raises(ValueError):
            item_url("atomic-heart", "1", bad_name)


# --- item metadata ---------------------------------------------------------


def test_structured_data_is_preferred_over_prose():
    meta = parse_item(item_html())
    assert meta.title and meta.author == "fln1" and meta.uploader == "xam_xam"
    assert meta.published == "2026-08-31"
    assert meta.size_bytes == 1873  # exact bytes, not a rounded display size
    assert meta.is_table
    # The VirusTotal report linked beside the file carries the digest, so one is
    # known before the user chooses the row.
    assert meta.advertised_sha256 == "da398538959ee8bd26a8c41f9c017fbef156e9ce86a466031799e28786365c9e"
    # The site serves `.rar` as well as `.zip`, so a row says which it will get.
    assert meta.filename == "redaktor-inventarja_1756625113_923014.zip"
    assert meta.archive_password == "vgtimes"


def test_a_page_without_structured_data_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        parse_item("<html><body>Скачать (1.83 kB)</body></html>")
    with pytest.raises(ValueError):
        parse_item('<script type="application/ld+json">{"@type":"SoftwareApplication"}</script>')


def test_a_broken_structured_block_drops_itself_not_the_page():
    html = ('<script type="application/ld+json">{ broken </script>'
            '<script type="application/ld+json">{"@type":"SoftwareApplication","name":"T",'
            '"category":"G / Читы / Таблицы","fileSize":"10 Bytes"}</script>')
    meta = parse_item(html)
    assert meta.title == "T" and meta.size_bytes == 10


def test_an_entry_that_is_not_a_table_is_dropped():
    html = ('<script type="application/ld+json">{"@type":"SoftwareApplication","name":"Trainer",'
            '"category":"Game / Файлы и моды / Читы / Трейнеры","fileSize":"10 Bytes"}</script>')
    meta = parse_item(html)
    assert not meta.is_table
    with collect_parse_issues() as issues:
        assert vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), meta, 0.9) is None
    assert issues.degraded == 1


def test_download_identity_is_read_from_its_own_control():
    # Regression for the first live run, which took `data-id` from an unrelated
    # element earlier on the page and reported the file as id 1.
    identity = parse_download_identity(item_html())
    assert identity.file_id == "97634"
    assert identity.file_hash == "173226e19cea8a3cd71346221270571f"
    assert "op=showfile" in identity.show_path and "&amp;" not in identity.show_path
    assert parse_download_identity('<html><a href="/x">no control</a></html>') is None


def test_two_identities_that_disagree_are_markup_this_no_longer_understands():
    # The route's own lid is the authority and the attribute corroborates it,
    # which means the two disagreeing is a control that has been rebuilt rather
    # than a preference to resolve. Trusting the lid silently is how that goes
    # on looking readable.
    skewed = item_html().replace('data-id="97634"', 'data-id="11111"')
    assert parse_download_identity(skewed) is None


def test_a_vgtimes_row_carries_whatever_release_it_publishes():
    """The same rule as every other source: say the release where there is one.

    This source publishes no tag, so the filename and the title beside it are
    what it has. Leaving the field empty made an imported table from here a
    digest and a date, with nothing connecting it to the revision that was
    chosen in search.
    """
    meta = parse_item(item_html())
    with collect_parse_issues():
        row = vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), meta, 0.87)
    assert row is not None
    # Whatever this fixture publishes, the field is derived rather than dropped:
    # a string when the name or title carries one, and None when neither does.
    assert row.result.version is None or isinstance(row.result.version, str)

    versioned = replace(meta, filename="1666 Amsterdam v2.3.CT") if hasattr(meta, "filename") else None
    if versioned is not None:
        with collect_parse_issues():
            row = vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), versioned, 0.87)
        assert row is not None and row.result.version == "2.3"


def test_a_row_is_fetchable_by_the_plugin_itself():
    meta = parse_item(item_html())
    with collect_parse_issues():
        row = vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), meta, 0.87)
    definition = provider_definition("vgtimes")
    assert row.result.provider == PROVIDER_ID == definition.provider
    assert row.result.provider_display_name == definition.provider_display_name
    assert row.result.artifact_id == "game-1666-amsterdam:file-97634"
    assert row.result.topic_id == "1666-amsterdam"
    assert row.result.match_score == 0.87
    assert row.advertised_sha256 and row.size_exact
    # A handoff would ask a user with no keyboard and no mouse to drive a web
    # page, and the site does not require one.
    assert row.result.download_mode == "direct_https"
    assert isinstance(row.acquisition, VGTimesDownloadRequest)
    # The published archive password stays backend-owned: the row says one is
    # needed and never carries the value into anything the frontend can read.
    assert row.password_hint == "vgtimes"
    public = row.public_dict()
    assert public["password_required"] is True
    assert "password_hint" not in public


def test_a_row_names_the_game_its_own_category_files_it_under():
    # The site files each entry inside the game's own section, so its title
    # names no game: "a table for Cheat Engine" is a complete entry name there,
    # and on a list that merges five sources it reads as a table for something
    # else. The category is the page's own breadcrumb, game first.
    meta = parse_item(item_html())
    assert meta.game_title == "1666: Amsterdam"
    with collect_parse_issues():
        row = vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), meta, 0.9)
    assert row.result.table_title == "1666: Amsterdam: Редактор инвентаря, ресурсов и зелий"
    # The category decides whether an entry is a table at all; it is not a line
    # of the row. Shown, it led every row with the site's whole breadcrumb,
    # repeating the game the title already names.
    assert row.result.notes is None

    # And it is not repeated when the entry already names it.
    named = replace(meta, title="1666: Amsterdam - таблица")
    with collect_parse_issues():
        row = vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), named, 0.9)
    assert row.result.table_title == "1666: Amsterdam - таблица"


def test_an_exactly_oversized_vgtimes_artifact_is_dropped_at_the_row():
    from ce_decky.providers import MAX_ARTIFACT_BYTES

    meta = parse_item(item_html())
    huge = replace(meta, size_bytes=MAX_ARTIFACT_BYTES + 1)
    with collect_parse_issues() as issues:
        assert vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), huge, 0.9) is None
    assert issues.degraded == 1
    # And the ordinary size is still offered.
    with collect_parse_issues():
        assert vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), meta, 0.9) is not None


def test_a_page_that_publishes_no_filename_costs_that_row_alone():
    # The filename is not decoration on this provider: the site serves `.rar` as
    # well as `.zip`, and the suffix is what the staged download is written as
    # and what the archive layer reads. A page that publishes none leaves a row
    # that cannot say what it would fetch, so it is skipped and counted rather
    # than offered with a name invented for it.
    meta = parse_item(item_html().replace('class="filename"', 'class="filename-was-renamed"'))
    assert meta.filename is None
    with collect_parse_issues() as issues:
        assert vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), meta, 0.9) is None
    assert issues.degraded == 1 and issues.failed == 0


# --- the client contract the site declares ---------------------------------


def test_the_page_declares_the_client_and_the_servers_this_chain_uses():
    boot = parse_page_bootstrap(item_html())
    assert boot.user_ip == "203.0.113.7" == parse_user_ip(item_html())
    assert boot.files_server == "https://files.vgtimes.ru"
    assert boot.is_guest and boot.group == "5"
    assert boot.js_num == "591665" and boot.base_js_url.startswith("/engine/classes/min/")
    assert boot.file_hosts[:2] == ("files.vgtimes.ru", "files.vgtimes.com")
    assert base_js_url(boot).startswith("https://vgtimes.ru/engine/classes/min/")
    with pytest.raises(ChallengeError):
        parse_page_bootstrap("<html>no bootstrap</html>")


def test_a_file_server_outside_the_provider_is_refused():
    # The host comes from the page, so the page could name anything; the policy
    # is what keeps that from becoming outbound authority.
    html = item_html().replace("files_server_host='https://files.vgtimes.ru'",
                               "files_server_host='https://files.evil.example'")
    with pytest.raises(ChallengeError):
        parse_page_bootstrap(html)
    assert is_provider_file_host("nl-files.vgtimes.ru")
    assert is_provider_file_host("files.vgtimes.com")
    for bad in ("vgtimes.ru.evil.example", "evil.example", "notvgtimes.ru"):
        assert not is_provider_file_host(bad)


def test_a_malformed_or_foreign_host_list_drops_itself_and_not_the_page():
    html = ("<script>files_server_original_hosts='[not json',"
            "files_server_backup_hosts='[\"https:\\/\\/ru-files.vgtimes.ru\",\"https:\\/\\/evil.example\"]';</script>")
    assert parse_file_hosts(html) == ("ru-files.vgtimes.ru",)


def test_the_routes_the_fields_and_the_wait_come_from_the_module():
    # None of the three is written down in the adapter. The bundle carries one
    # module per page type and picks by branch, so the branch that selects a
    # files page is what says which module a files page loads: taking the first
    # template in the file instead returned a module with no chain in it.
    boot = parse_page_bootstrap(item_html())
    assert client_module_url((FIXTURES / "vgtimes_base_js.js").read_text(), boot) == (
        "https://vgtimes.ru/minified/gamebase-rev591665.js"
    )
    contract = parse_client_contract(module_js(), boot)
    assert contract.waiting_page_url == "https://vgtimes.ru/engine/ajax/files/files_waiting_page.php"
    assert contract.challenge_url == "https://files.vgtimes.ru/get_challenge.php"
    # Drawn as 30 by the module and enforced by the server: the same token was
    # refused at t+0 and t+10 and accepted from t+21, so the wait is honoured.
    assert contract.countdown_seconds == 30
    with pytest.raises(ChallengeError):
        parse_client_contract("var nothing = 1;", boot)
    with pytest.raises(ChallengeError):
        client_module_url("var nothing = 1;", boot)


def test_the_request_fields_are_the_modules_own_and_carry_only_true_values():
    boot = parse_page_bootstrap(item_html())
    identity = parse_download_identity(item_html())
    contract = parse_client_contract(module_js(), boot)
    assert build_request(contract.waiting_page_fields, identity, boot) == {
        "file_hash": identity.file_hash, "file_id": identity.file_id,
    }
    assert build_request(contract.challenge_fields, identity, boot, token="tok") == {
        "id": identity.file_hash, "file_id": identity.file_id,
        # The queue flow is one this adapter does not implement, and the site's
        # own client sends this empty when no queue is involved.
        "queue_hash": "", "download_token": "tok", "ip": boot.user_ip,
    }
    # A field asking for something this client does not have is the contract
    # having moved, and is reported rather than guessed at.
    with pytest.raises(ChallengeError):
        build_request({"screen": "screen_width"}, identity, boot)


def test_the_waiting_page_token_is_read_or_refused():
    assert parse_waiting_page({"download_token": "abc", "error": ""}) == "abc"
    for bad in ({"error": "file not found"}, {"download_token": ""}, "not an object"):
        with pytest.raises(ChallengeError):
            parse_waiting_page(bad)


# --- the challenge ---------------------------------------------------------


def test_the_key_derivation_is_read_from_the_challenge_rather_than_assumed():
    derivation = parse_key_derivation(unpack_packed_script(challenge()["result"]))
    assert (derivation.cipher, derivation.hash_name) == ("AES-CBC", "SHA-256")
    assert derivation.iterations == 1000 and derivation.key_bits == 256 and derivation.key_bytes == 32
    with pytest.raises(ChallengeError):
        parse_key_derivation("no derivation here")


def test_the_challenge_is_solved_from_the_data_the_server_supplies():
    # A real captured answer. Stage one is keyed by the response header the
    # script names, its plaintext is a JPEG, and stage two is keyed by that
    # image's own IPTC record. Nothing is guessed and nothing is executed.
    answer = challenge()
    image, final = solve_challenge(answer["result"], {"XC-Code-d": answer["header_key"]})
    assert (image.width, image.height) == (1, 11)
    # Which field holds which answer is the script's decision, not the adapter's.
    assert image.names == {"copyright": "2:116", "caption": "2:120"}
    assert image.named("copyright") == "d99ff8ce491c4f17da5b42028cfbc08e"
    assert image.named("caption") == "38791ded35b83f9858cbc6987f7432d8"
    assert "challenge_values" in final


def test_a_challenge_in_an_unknown_shape_is_refused_rather_than_guessed():
    answer = challenge()
    for headers in ({}, {"Some-Other-Header": "x"}, {"XC-Code-d": "the wrong key entirely"}):
        with pytest.raises(ChallengeError):
            solve_challenge(answer["result"], headers)
    with pytest.raises(ChallengeError):
        solve_challenge("function(){}", {"XC-Code-d": "key"})


def test_only_the_timing_formula_this_client_understands_is_answered():
    # "An expression that starts with `(function`" is true of any function the
    # site might put there next, and answering a different question with this
    # client's own timing is the confident wrong answer this adapter exists to
    # avoid.
    answer = challenge()
    image, final = solve_challenge(answer["result"], {"XC-Code-d": answer["header_key"]})
    values = build_download_values(
        final, image, elapsed_seconds=7, now_epoch=1788458900, now_iso="2026-09-03T20:08:20Z",
    )
    assert base64.b64decode(values["value64e"]).decode() == "1788458900|7|0"

    def refuses(script: str) -> bool:
        assert script != final
        try:
            build_download_values(
                script, image, elapsed_seconds=7, now_epoch=1788458900, now_iso="2026-09-03T20:08:20Z",
            )
        except ChallengeError as exc:
            return "unknown value" in str(exc)
        return False

    # A different question entirely.
    assert refuses(final.replace(
        "btoa(parseInt(Date.now()/1000)+'|'+pt+'|'+0)",
        "btoa(navigator.userAgent+'|'+screen.width)",
    ))
    # The shape is intact and `page_load_time` is still in the expression, but
    # the operand this client answers is a different variable. Matching the
    # shape and looking for `page_load_time` somewhere does not establish that
    # the operand came from it, so the name is bound to its own assignment.
    assert refuses(final.replace("+'|'+pt+'|'+0", "+'|'+q+'|'+0"))
    # And the operand keeps its name while being assigned something this client
    # has no way to know, with `page_load_time` still present beside it.
    assert refuses(final.replace(
        "var pt=(window.parent&&window.parent.page_load_time)"
        "?parseInt((Date.now()-window.parent.page_load_time)/1000):0;",
        "var pt=screen.width;var ignored=window.parent.page_load_time;",
    ))
    # The real expression is still answered, and with this client's own timing.
    assert base64.b64decode(build_download_values(
        final, image, elapsed_seconds=7, now_epoch=1788458900, now_iso="2026-09-03T20:08:20Z",
    )["value64e"]).decode() == "1788458900|7|0"


def test_the_elapsed_operand_is_bound_to_the_assignment_that_produces_it():
    # The unit behind the negative cases above: the name in the middle of the
    # `btoa()` join has to be the one the expression assigns from the moment the
    # page loaded, which is the only quantity this client knows.
    real = ("var pt=(window.parent&&window.parent.page_load_time)"
            "?parseInt((Date.now()-window.parent.page_load_time)/1000):0;")
    assert timing_elapsed_is_seconds_on_page(real, "pt")
    assert not timing_elapsed_is_seconds_on_page(real, "q")
    assert not timing_elapsed_is_seconds_on_page("var pt=window.parent.page_load_time;", "pt")
    assert not timing_elapsed_is_seconds_on_page("var pt=parseInt(Date.now()/1000);", "pt")
    # A different declaration keyword is still a declaration.
    assert timing_elapsed_is_seconds_on_page(
        "let secs = parseInt((Date.now()-page_load_time)/1000);", "secs",
    )


def test_the_answer_carries_only_what_this_client_can_honestly_say():
    answer = challenge()
    image, final = solve_challenge(answer["result"], {"XC-Code-d": answer["header_key"]})
    values = build_download_values(
        final, image, elapsed_seconds=41, now_epoch=1788458900, now_iso="2026-09-03T20:08:20Z",
    )
    assert values["value20n"] == image.named("copyright")
    assert values["value21e"] == image.named("caption")
    assert (values["value18w"], values["value19h"]) == (image.width, image.height)
    assert base64.b64decode(values["value64e"]).decode() == "1788458900|41|0"
    # A browser fills this slot with an automation probe. This client is not one
    # and computed none of it, so it goes out empty, and there is no path here
    # that can put anything else in it. The slot is named by the script.
    slot = probe_slot(final)
    assert slot == "fingerprints" and values[slot] == {}
    debug = values["debug_data"]
    assert debug["w"] == image.width and debug["n"] == image.named("copyright")
    assert "sw" not in debug and "sh" not in debug


def test_the_download_url_refuses_to_describe_this_client_as_a_browser():
    answer = challenge()
    image, final = solve_challenge(answer["result"], {"XC-Code-d": answer["header_key"]})
    values = build_download_values(
        final, image, elapsed_seconds=41, now_epoch=1788458900, now_iso="2026-09-03T20:08:20Z",
    )
    identity = parse_download_identity(item_html())
    boot = parse_page_bootstrap(item_html())
    slot = probe_slot(final)
    url = download_url(identity, boot, values, probe_field=slot)
    assert url.startswith("https://vgtimes.ru/index.php?do=files&op=showfile")
    assert "&ip=203.0.113.7&challenge_values=" in url
    assert json.loads(base64.b64decode(url.rsplit("challenge_values=", 1)[1]))[slot] == {}
    # The one refusal this adapter keeps: an invented fingerprint is a claim
    # about the client that is not true, and it is not needed either.
    with pytest.raises(ChallengeError):
        download_url(identity, boot, {**values, slot: {"webdriver": False}}, probe_field=slot)
    with pytest.raises(ChallengeError):
        download_url(identity, replace(boot, user_ip="not-an-address"), values, probe_field=slot)


# --- refusals --------------------------------------------------------------


def test_a_refusal_is_read_as_a_refusal_and_not_as_a_broken_contract():
    # The site answers a refused download with a well-formed body and an empty
    # script. Treating that as an unreadable challenge would report a defect
    # here for a decision made there, so the code is read first.
    with pytest.raises(DownloadRefused) as refusal:
        parse_challenge_answer({"error_code": "2", "result": ""})
    assert refusal.value.code == "2"
    assert parse_challenge_answer({"error_code": "", "result": "script"}) == "script"
    with pytest.raises(ChallengeError):
        parse_challenge_answer({"error_code": "", "result": ""})


def test_what_each_refusal_means_is_read_from_the_site_and_not_transcribed():
    contract = parse_refusal_contract(module_js())
    assert contract.messages == {"1": 1919, "2": 2524, "4": 2525, "5": 2526}
    assert contract.token_codes == frozenset({"4", "5"})
    assert contract.queue_codes == frozenset({"3"})
    assert parse_language_strings(item_html())[1919] == "Файл не найден"
    empty = parse_refusal_contract("function nothing(){}")
    assert not empty.messages and not empty.token_codes and not empty.queue_codes


def test_a_refusal_carries_the_words_the_site_would_have_shown():
    contract = parse_refusal_contract(module_js())
    strings = parse_language_strings(item_html())
    for code, busy, queued, new_token in (("1", False, False, False), ("2", True, False, False),
                                          ("3", False, True, False), ("4", False, False, True),
                                          ("5", False, False, True)):
        with pytest.raises(DownloadRefused) as refusal:
            parse_challenge_answer({"error_code": code, "result": ""}, contract=contract, strings=strings)
        assert refusal.value.code == code
        assert (refusal.value.busy, refusal.value.queued, refusal.value.needs_new_token) == (busy, queued, new_token)
        if code in contract.messages:
            assert refusal.value.message == strings[contract.messages[code]]
            assert refusal.value.message in str(refusal.value)


def test_code_two_still_means_one_download_at_a_time():
    # This is the one refusal whose meaning is a rule for the plugin rather than
    # a message for the user, so it is pinned to the words the site served on
    # 2026-09-03. A re-captured fixture that no longer says this fails here,
    # which is the point: the rule would need re-deriving before it is trusted.
    contract = parse_refusal_contract(module_js())
    message = parse_language_strings(item_html())[contract.messages[CHALLENGE_BUSY_CODE]]
    assert "только один файл одновременно" in message
    assert BUSY_RETRY_SECONDS >= 60


def test_code_one_still_means_the_file_is_not_there():
    # The second refusal whose meaning is a rule rather than a message: it
    # retires the row instead of being retried, because asking again reads the
    # same however often it is done and costs the countdown each time. Pinned to
    # the words the site served on 2026-09-04, when one entry whose page,
    # listing and download control were all intact answered with it while a
    # `.zip` and a second `.rar` resolved normally in the same minute.
    contract = parse_refusal_contract(module_js())
    message = parse_language_strings(item_html())[contract.messages[CHALLENGE_MISSING_CODE]]
    assert "не найден" in message.casefold()

    with pytest.raises(DownloadRefused) as raised:
        parse_challenge_answer({"error_code": CHALLENGE_MISSING_CODE, "result": ""}, contract=contract)
    assert raised.value.missing is True
    assert raised.value.busy is False
    # And nothing tries it again, whatever the attempt count.
    assert retry_delay_for(raised.value, attempt=0) is None


def test_what_to_do_about_a_refusal_follows_from_what_it_is():
    contract = parse_refusal_contract(module_js())

    def refusal_for(code):
        with pytest.raises(DownloadRefused) as raised:
            parse_challenge_answer({"error_code": code, "result": ""}, contract=contract)
        return raised.value

    # This client's own open download clears on its own, so waiting is the fix.
    assert retry_delay_for(refusal_for("2"), attempt=0) == BUSY_RETRY_SECONDS
    # The site is asking for the waiting page again, which costs a token and the
    # countdown but no delay of its own.
    assert retry_delay_for(refusal_for("4"), attempt=0) == 0
    assert retry_delay_for(refusal_for("5"), attempt=0) == 0
    # A queue is a flow this adapter does not implement, and a file the site
    # says it does not have will not appear by being asked again.
    assert retry_delay_for(refusal_for("3"), attempt=0) is None
    assert retry_delay_for(refusal_for("1"), attempt=0) is None
    # Retrying is bounded: a busy source that never clears is reported, not spun on.
    assert retry_delay_for(refusal_for("2"), attempt=3) is None
    # A busy retry reuses the token it already has: asking the waiting page for
    # another is the request that tells the site a download is starting, and a
    # client that answers "you already have one open" by opening another is
    # arguing with itself.
    assert retry_needs_new_token(refusal_for("2")) is False
    assert retry_needs_new_token(refusal_for("4")) is True


# --- the whole acquisition, offline ----------------------------------------


def _chain_network(challenge_responses=None):
    answer = challenge()
    body = json.dumps({"error_code": "", "result": answer["result"]}).encode()
    return _RouteNetwork(
        routes=[
            ("/games/", _Response(200, item_html().encode())),
            ("/engine/classes/min/", _Response(200, (FIXTURES / "vgtimes_base_js.js").read_bytes())),
            ("/minified/", _Response(200, (FIXTURES / "vgtimes_files_module.js").read_bytes())),
        ],
        posts=[
            ("files_waiting_page.php", _Response(200, b'{"download_token":"tok-1"}')),
            ("get_challenge.php", challenge_responses if challenge_responses is not None else [
                _Response(200, body, {"xc-code-d": answer["header_key"]}),
            ]),
        ],
    )


def _record(request: VGTimesDownloadRequest):
    meta = parse_item(item_html())
    with collect_parse_issues():
        row = vgtimes_record(ItemRef(ITEM, "1666-amsterdam", "97634"), meta, 0.9)
    return replace(row, acquisition=request)


@pytest.mark.asyncio
async def test_the_whole_chain_runs_offline_and_waits_out_the_countdown():
    waits: list[float] = []
    countdowns: list[tuple[int, str]] = []
    request = VGTimesDownloadRequest(ITEM, "97634")
    network = _chain_network()

    async def sleep(seconds):
        waits.append(seconds)

    def countdown(seconds, reason="preparing"):
        countdowns.append((seconds, reason))

    url, hosts = await request.resolve(
        network, _record(request), sleep=sleep, on_countdown=countdown,
    )
    # The server enforces the wait rather than drawing it, and the panel is told
    # about it as a countdown rather than being left with a stalled download.
    assert waits == [30] and countdowns == [(30, "preparing")]
    assert url.startswith("https://vgtimes.ru/index.php?do=files&op=showfile")
    # The showfile route is on the site and redirects to a file server, so both
    # belong in the exact per-acquisition policy, and nothing else does.
    assert "vgtimes.ru" in hosts and "files.vgtimes.ru" in hosts
    assert all("vgtimes" in host for host in hosts)
    # The token the waiting page issued is the one the challenge was sent.
    challenge_body = next(body for called, body in network.sent if "get_challenge" in called)
    assert challenge_body["download_token"] == "tok-1"


@pytest.mark.asyncio
async def test_a_busy_refusal_waits_and_reuses_its_token_rather_than_failing():
    answer = challenge()
    refused = _Response(200, json.dumps({"error_code": "2", "result": ""}).encode())
    served = _Response(
        200, json.dumps({"error_code": "", "result": answer["result"]}).encode(),
        {"xc-code-d": answer["header_key"]},
    )
    request = VGTimesDownloadRequest(ITEM, "97634")
    network = _chain_network([refused, served])
    waits: list[float] = []
    countdowns: list[tuple[int, str]] = []

    async def sleep(seconds):
        waits.append(seconds)

    def countdown(seconds, reason="preparing"):
        countdowns.append((seconds, reason))

    url, _ = await request.resolve(network, _record(request), sleep=sleep, on_countdown=countdown)
    assert url.startswith("https://vgtimes.ru/index.php")
    # The countdown, then the minute the busy refusal costs, both shown. A
    # refusal the site states is this client's own doing, not a throttle.
    assert waits == [30, BUSY_RETRY_SECONDS]
    # The second wait is named for what it is. Reported as preparation it read
    # as the countdown starting over from the beginning for no visible reason,
    # when the site had in fact said it is already serving this client a file.
    assert countdowns == [(30, "preparing"), (BUSY_RETRY_SECONDS, "busy")]
    # Exactly one waiting page request: the token stays valid inside its window.
    assert sum(1 for url, _ in network.sent if "files_waiting_page" in url) == 1
    tokens = [body["download_token"] for url, body in network.sent if "get_challenge" in url]
    assert tokens == ["tok-1", "tok-1"]


@pytest.mark.asyncio
async def test_a_throttled_challenge_waits_with_the_token_it_already_has():
    """A 429 after the token was issued must not restart the handshake.

    The generic acquisition retry re-enters this resolver from the top, which
    asks the waiting page for another token, and that request is the one that
    tells the site a download is starting. Its own rule allows a guest exactly
    one at a time, so the retry has to happen where the token still is.
    """
    answer = challenge()
    throttled = _Response(429, b"", {"retry-after": "2"})
    served = _Response(
        200, json.dumps({"error_code": "", "result": answer["result"]}).encode(),
        {"xc-code-d": answer["header_key"]},
    )
    request = VGTimesDownloadRequest(ITEM, "97634")
    network = _chain_network([throttled, served])
    waits: list[float] = []
    countdowns: list[tuple[int, str]] = []

    async def sleep(seconds):
        waits.append(seconds)

    def countdown(seconds, reason="preparing"):
        countdowns.append((seconds, reason))

    url, _ = await request.resolve(network, _record(request), sleep=sleep, on_countdown=countdown)
    assert url.startswith("https://vgtimes.ru/index.php")
    # The countdown, then the provider's own Retry-After. The second is named
    # for what it is: reported as ordinary preparation it would have shown a
    # provider refusing as this client's normal wait, and because the generic
    # retry never sees a limit this resolver handles itself, nothing else could
    # say so.
    assert waits == [30, 2]
    assert countdowns == [(30, "preparing"), (2, "rate_limited")]
    assert sum(1 for called, _ in network.sent if "files_waiting_page" in called) == 1
    tokens = [body["download_token"] for called, body in network.sent if "get_challenge" in called]
    assert tokens == ["tok-1", "tok-1"]


@pytest.mark.asyncio
async def test_a_challenge_that_stays_throttled_refuses_to_be_restarted():
    # Once the attempts are spent the limit leaves this resolver, and it leaves
    # marked as something the acquisition layer must not simply run again: doing
    # so would spend a second waiting page and a second countdown.
    request = VGTimesDownloadRequest(ITEM, "97634")
    throttled = _Response(429, b"", {"retry-after": "1"})
    network = _chain_network([throttled, throttled, throttled, throttled])

    async def sleep(seconds):
        return None

    with pytest.raises(ProviderRateLimited) as raised:
        await request.resolve(network, _record(request), sleep=sleep)
    assert raised.value.restartable is False
    assert sum(1 for called, _ in network.sent if "files_waiting_page" in called) == 1


@pytest.mark.asyncio
async def test_a_refusal_that_never_clears_reports_the_sites_own_sentence():
    refused = _Response(200, json.dumps({"error_code": "2", "result": ""}).encode())
    request = VGTimesDownloadRequest(ITEM, "97634")
    network = _chain_network([refused, refused, refused, refused])

    async def sleep(seconds):
        return None

    with pytest.raises(DownloadRefused) as raised:
        await request.resolve(network, _record(request), sleep=sleep)
    assert raised.value.busy
    assert "только один файл одновременно" in str(raised.value)


@pytest.mark.asyncio
async def test_the_chain_refuses_a_page_that_names_a_different_file():
    request = VGTimesDownloadRequest(ITEM, "12345")
    with pytest.raises(ChallengeError, match="different file"):
        await request.resolve(_chain_network(), _record(request), sleep=lambda seconds: None)


@pytest.mark.asyncio
async def test_a_rate_limited_step_is_a_wait_rather_than_a_failure():
    request = VGTimesDownloadRequest(ITEM, "97634")
    network = _RouteNetwork(routes=[("/games/", _Response(429, b"", {"retry-after": "12"}))])
    with pytest.raises(ProviderRateLimited) as raised:
        await request.resolve(network, _record(request), sleep=lambda seconds: None)
    assert raised.value.retry_after == "12"


def test_the_request_identity_is_validated_before_anything_is_fetched():
    for page, file_id in (("http://vgtimes.ru/x", "1"), ("https://evil.example/x", "1"), (ITEM, "abc")):
        with pytest.raises(ValueError):
            VGTimesDownloadRequest(page, file_id)
    request = VGTimesDownloadRequest(ITEM, "97634")
    # One download of this provider at a time, because the site says so.
    assert request.exclusive is True
    assert request.provider == PROVIDER_ID
    assert request.artifact_hosts >= ASSET_HOSTS


# --- the search job --------------------------------------------------------


def _search_network():
    shard = gzip.compress((FIXTURES / "vgtimes_games_sitemap.xml").read_bytes())
    return _RouteNetwork(routes=[
        ("/sitemap.xml", _Response(200, (
            b"<sitemapindex><sitemap><loc>https://vgtimes.ru/sitemaps/ru/games_sitemap_1.xml.gz</loc>"
            b"</sitemap></sitemapindex>"
        ))),
        ("games_sitemap_1", _Response(200, shard)),
        ("/files/cheats/tables/", _Response(200, (FIXTURES / "vgtimes_tables_listing.html").read_bytes())),
        ("/files/", _Response(200, item_html().encode())),
    ])


@pytest.mark.asyncio
async def test_the_search_ranks_the_sitemap_locally_and_reads_only_table_listings(tmp_path):
    network = _search_network()
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    rows = await service._vgtimes(["atomic heart"])
    assert rows and all(row.result.provider == "vgtimes" for row in rows)
    assert all(isinstance(row.acquisition, VGTimesDownloadRequest) for row in rows)
    # The category is the route here, so no entry has to be opened to find out
    # whether it is a table rather than a trainer or a save editor.
    assert any("/files/cheats/tables/" in url for url in network.calls)
    # And a game the query does not name is never listed at all.
    assert not any("/games/1666-amsterdam/files/cheats" in url for url in network.calls)


def _two_shard_network(second: _Response):
    shard = gzip.compress((FIXTURES / "vgtimes_games_sitemap.xml").read_bytes())
    index = (
        b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://vgtimes.ru/sitemaps/ru/games_sitemap_1.xml.gz</loc></sitemap>"
        b"<sitemap><loc>https://vgtimes.ru/sitemaps/ru/games_sitemap_2.xml.gz</loc></sitemap>"
        b"</sitemapindex>"
    )
    return _RouteNetwork(routes=[
        ("/sitemap.xml", _Response(200, index)),
        ("games_sitemap_1", _Response(200, shard)),
        ("games_sitemap_2", second),
        ("/files/cheats/tables/", _Response(200, (FIXTURES / "vgtimes_tables_listing.html").read_bytes())),
        ("/files/", _Response(200, item_html().encode())),
    ])


@pytest.mark.asyncio
async def test_a_crawl_that_lost_a_shard_is_used_but_never_published_as_the_index(tmp_path):
    """A partial crawl answers this search and is not written down.

    Cached as the index, one shard failing once would take every game it
    declared out of every search for the next six hours, and nothing would ever
    say so: the games would simply not exist.
    """
    network = _two_shard_network(_Response(503))
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        rows = await service._vgtimes(["atomic heart"])
    # What was read still answers the search in front of the user.
    assert rows
    assert issues.failed == 1
    assert not (tmp_path / "vgtimes-games.json").exists()

    # And the next search rebuilds rather than inheriting the hole.
    healthy = _two_shard_network(_Response(200, gzip.compress(
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://vgtimes.ru/games/other-game</loc></url></urlset>"
    )))
    service = CatalogService(healthy, tmp_path / "cache.json")  # type: ignore[arg-type]
    assert await service._vgtimes(["atomic heart"])
    assert any("games_sitemap_2" in url for url in healthy.calls)
    assert (tmp_path / "vgtimes-games.json").is_file()


def test_a_sitemap_has_to_be_the_document_it_was_asked_for():
    # Read for its `<loc>` elements alone, anything well formed could contribute
    # identity to this provider's index, and the origin of an entry is what
    # makes its slug this provider's game at all.
    with pytest.raises(ValueError, match="unexpected root"):
        parse_game_sitemap('<notaurlset><url><loc>https://vgtimes.ru/games/a</loc></url></notaurlset>')
    with pytest.raises(ValueError, match="unexpected root"):
        parse_sitemap_index(b'<urlset><url><loc>https://vgtimes.ru/a.xml</loc></url></urlset>')
    poisoned = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://elsewhere.example/games/poisoned</loc></url>"
        "<url><loc>http://vgtimes.ru/games/insecure</loc></url>"
        "<url><loc>https://vgtimes.ru/games/real</loc></url>"
        "</urlset>"
    )
    assert [game.slug for game in parse_game_sitemap(poisoned)] == ["real"]


def test_more_shards_than_this_can_read_is_refused_rather_than_trimmed():
    # Taking the first few of a longer list is the same defect as caching a
    # crawl that lost a page: every game in the shards that were dropped
    # disappears, the crawl still calls itself complete, and nothing says so.
    declared = tuple(f"https://vgtimes.ru/sitemaps/ru/games_sitemap_{n}.xml.gz" for n in range(9))
    assert len(game_sitemap_urls(declared[:2])) == 2
    with pytest.raises(ValueError, match="more game shards"):
        game_sitemap_urls(declared)
    # A shard list that is not a game shard list is simply empty, and the
    # caller treats that as a provider that declared none.
    assert game_sitemap_urls(("https://vgtimes.ru/sitemaps/ru/posts_sitemap_1.xml.gz",)) == ()


def test_a_shard_that_is_not_a_whole_document_is_refused():
    # The parser was pattern based, so a document that stopped early still
    # produced entries, counted as read, and would have been cached. Gzip
    # completeness alone does not cover a plain shard.
    whole = (FIXTURES / "vgtimes_games_sitemap.xml").read_text()
    assert parse_game_sitemap(whole)
    with pytest.raises(ValueError, match="complete XML"):
        parse_game_sitemap(whole[: whole.index("1666-amsterdam") - 30])
    with pytest.raises(ValueError, match="complete XML"):
        parse_sitemap_index(b'<sitemapindex><sitemap><loc>https://vgtimes.ru/a.xml</loc>')


@pytest.mark.asyncio
async def test_the_slug_index_is_cached_so_a_second_search_refetches_nothing(tmp_path):
    service = CatalogService(_search_network(), tmp_path / "cache.json")  # type: ignore[arg-type]
    await service._vgtimes(["atomic heart"])
    assert (tmp_path / "vgtimes-games.json").is_file()

    second = _search_network()
    service = CatalogService(second, tmp_path / "cache.json")  # type: ignore[arg-type]
    assert await service._vgtimes(["atomic heart"])
    assert not any("sitemap" in url for url in second.calls)


@pytest.mark.asyncio
async def test_a_cached_slug_that_is_not_a_slug_drops_the_whole_index(tmp_path):
    # A slug reaches a URL. A cache with one bad entry is not repaired around,
    # because reading the rest of it means trusting the file that produced it.
    cache = tmp_path / "vgtimes-games.json"
    cache.write_text(json.dumps({
        "schema": 1, "saved": 1e12, "slugs": ["atomic-heart", "../escape"],
    }), encoding="utf-8")
    service = CatalogService(_search_network(), tmp_path / "cache.json")  # type: ignore[arg-type]
    assert service._load_vgtimes_slug_cache() is None
    assert await service._vgtimes(["atomic heart"])


@pytest.mark.asyncio
async def test_a_game_with_no_tables_is_an_answer_rather_than_a_failure(tmp_path):
    shard = gzip.compress((FIXTURES / "vgtimes_games_sitemap.xml").read_bytes())
    network = _RouteNetwork(routes=[
        ("/sitemap.xml", _Response(200, (
            b"<sitemapindex><sitemap><loc>https://vgtimes.ru/sitemaps/ru/games_sitemap_1.xml.gz</loc>"
            b"</sitemap></sitemapindex>"
        ))),
        ("games_sitemap_1", _Response(200, shard)),
        ("/files/cheats/tables/", _Response(404)),
    ])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        assert await service._vgtimes(["atomic heart"]) == []
    assert issues.failed == 0


@pytest.mark.asyncio
async def test_an_unreadable_sitemap_is_a_failed_source(tmp_path):
    network = _RouteNetwork(routes=[("/sitemap.xml", _Response(503))])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    with pytest.raises(NetworkError):
        await service._vgtimes(["atomic heart"])
