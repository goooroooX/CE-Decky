from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from ce_decky.catalog import (
    GITHUB_INDEX_TTL_SECONDS,
    MAX_GITHUB_INDEX_QUERIES,
    CatalogService,
    collect_parse_issues,
    github_tree_record,
)
from ce_decky.github_discovery import (
    MAX_SEARCH_QUERIES,
    RepoCandidate,
    TreeTable,
    build_search_queries,
    git_blob_sha1,
    ASSET_HOSTS,
    PAGE_HOSTS,
    is_provider_asset,
    is_provider_page,
    is_rate_limited,
    parse_repository_search,
    parse_tree_tables,
    rank_repositories,
    rate_limit_retry_after,
    raw_url,
    releases_url,
    repo_from_source,
    tree_url,
    verify_blob_sha1,
)
from ce_decky.network import NetworkError, ProviderRateLimited
from ce_decky.providers import ProviderDiagnosticsStore, provider_definition


FIXTURES = Path(__file__).parent / "fixtures"


class _Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}


class _RouteNetwork:
    """Answers by route, because a repository costs a variable request count."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        for fragment, response in self.routes:
            if fragment in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")


def _recorded_cooldown(diagnostics: ProviderDiagnosticsStore, provider: str) -> float:
    """The deadline this provider is held to as a whole source, if any."""
    state = diagnostics.snapshot()["providers"].get(provider, {})
    return float(state.get("cooldown_until_epoch_s", 0) or 0)


def _candidate(**fields) -> RepoCandidate:
    base = {
        "full_name": "someone/Sort-Them-Ducks-CT",
        "description": "Cheat table",
        "stars": 3,
        "pushed_at": "2026-08-01T00:00:00Z",
        "topics": (),
        "archived": False,
        "fork": False,
        "default_branch": "main",
        "html_url": "https://github.com/someone/Sort-Them-Ducks-CT",
    }
    base.update(fields)
    return RepoCandidate(**base)


def _search_payload(*names: str) -> dict:
    return {"items": [
        {
            "full_name": name,
            "description": "",
            "stargazers_count": 1,
            "pushed_at": "2026-08-01T00:00:00Z",
            "topics": [],
            "archived": False,
            "fork": False,
            "default_branch": "main",
            "html_url": f"https://github.com/{name}",
        }
        for name in names
    ]}


def test_the_search_spends_a_bounded_slice_of_an_anonymous_minute():
    # Ten searches a minute is the whole device's budget, so one game search
    # takes a fixed slice of it rather than one request per alias.
    queries = build_search_queries(["sort them ducks", "sort them ducks deluxe", "duck sorter"])
    assert len(queries) == MAX_SEARCH_QUERIES
    # Forks and archived repositories are excluded by the index itself, because
    # a page of results is the scarce thing rather than the filtering.
    assert all("fork:false archived:false" in query for query in queries)
    assert build_search_queries(["", "  "]) == []


def test_the_words_the_query_supplied_are_stripped_before_a_name_is_scored():
    # Every candidate carries them, spelled differently: one as a token, one
    # glued onto the name with no separator at all. Left in, they are shared
    # vocabulary that pulls every candidate toward every game.
    assert _candidate(full_name="a/Elden-Ring-CT-TGA").match_text == "Elden Ring TGA"
    assert _candidate(full_name="a/eldenringcheatengine").match_text == "eldenring"
    # A name that is nothing but boilerplate keeps its text rather than becoming
    # empty and matching everything.
    assert _candidate(full_name="a/cheat-table").match_text == "cheat table"


def test_multiplayer_cheats_and_the_tool_itself_are_never_offered():
    assert _candidate(full_name="a/CheatEngine7.5").is_generic
    assert _candidate(full_name="a/cheat-tables").is_generic
    assert _candidate(full_name="a/Game-CT", description="aimbot and wallhack ESP").is_out_of_scope
    assert _candidate(full_name="a/Game-CT", topics=("undetected", "spoofer")).is_out_of_scope
    assert not _candidate().is_out_of_scope

    ranked = rank_repositories(
        (_candidate(full_name="a/CheatEngine7.5"), _candidate(full_name="b/aimbot-esp")),
        lambda name: 1.0,
    )
    assert ranked == ()


def test_ranking_orders_by_the_game_and_breaks_ties_on_stars():
    scores = {"Sort Them Ducks": 0.9, "Other Game": 0.6, "Unrelated": 0.1}
    ranked = rank_repositories(
        (
            _candidate(full_name="a/Unrelated"),
            _candidate(full_name="b/Other-Game", stars=1),
            _candidate(full_name="c/Other-Game", stars=90),
            _candidate(full_name="d/Sort-Them-Ducks"),
            # An archived repository is dropped even when the index returns one.
            _candidate(full_name="e/Sort-Them-Ducks", archived=True),
        ),
        lambda name: scores.get(name, 0.0),
        limit=3,
    )
    assert [item[0].full_name for item in ranked] == ["d/Sort-Them-Ducks", "c/Other-Game", "b/Other-Game"]


def test_a_malformed_search_row_costs_that_row_only():
    payload = {"items": [
        {"full_name": "not a repo"},
        {"full_name": "a/b", "default_branch": "../etc", "html_url": "https://github.com/a/b"},
        "not an object",
    ]}
    candidates = parse_repository_search(payload)
    assert [item.full_name for item in candidates] == ["a/b"]
    # A branch that is not a branch leaves the repository without one. It used
    # to become "main", which is a guessed ref, and a guessed ref is a guessed
    # path: the tree route would then have gone looking for tables on a branch
    # nobody named and reported a repository with a table as one without.
    assert candidates[0].default_branch is None
    assert parse_repository_search({"items": [{"full_name": "a/b", "html_url": "x"}]})[0].default_branch is None
    with pytest.raises(ValueError):
        parse_repository_search(["not", "an", "object"])


def test_a_tree_answer_has_to_say_that_it_is_complete():
    # GitHub states this on every tree answer, so completeness is read from what
    # it says rather than from the absence of a warning. Read as if it had been
    # whole, a repository whose committed table sits past the cut looks like a
    # repository with no table, which is a wrong answer rather than a missing
    # one, and this route's only integrity anchor is the git object name of the
    # blobs it did return.
    entries = [{"type": "blob", "path": "Ducks.CT", "sha": "a" * 40, "size": 5}]
    assert len(parse_tree_tables({"truncated": False, "tree": entries})) == 1
    for unclaimed in ({}, {"truncated": None}, {"truncated": "false"}, {"truncated": 0}, {"truncated": True}):
        with pytest.raises(ValueError, match="complete"):
            parse_tree_tables({**unclaimed, "tree": entries})


def test_a_declared_page_is_used_only_when_it_is_one_of_this_providers():
    """A URL the API hands back is a claim, and this one becomes provenance.

    It is what a row shows as its source, what an imported table records as
    where it came from, and the Referer the artifact transfer names, so a URL
    that points somewhere else is replaced by the page the repository certainly
    has rather than carried.
    """
    def declared(url):
        return parse_repository_search({"items": [
            {"full_name": "owner/repo", "html_url": url},
        ]})[0].html_url

    assert declared("https://github.com/owner/repo") == "https://github.com/owner/repo"
    for foreign in (
        "https://elsewhere.example/x",
        "http://github.com/owner/repo",
        "https://github.com.evil.example/x",
        "",
        None,
    ):
        assert declared(foreign) == "https://github.com/owner/repo"
    assert is_provider_page("https://github.com/a/b")
    assert not is_provider_page("https://raw.githubusercontent.com/a/b/main/x.CT")
    # Held to what the transport will accept structurally, not merely to scheme
    # and host: these pass a hostname check and are refused later, so a row
    # promising a direct download was offered for a URL that could only fail
    # once the user had selected it.
    for unusable in (
        "https://user@github.com/a/b",
        "https://github.com:444/a/b",
        "http://github.com/a/b",
    ):
        assert not is_provider_page(unusable)
        assert not is_provider_asset(unusable.replace("github.com", "raw.githubusercontent.com"))
    # The artifact hosts are a different, wider set, and a page is not one.
    assert is_provider_asset("https://raw.githubusercontent.com/a/b/main/x.CT")
    assert is_provider_asset("https://objects.githubusercontent.com/x")
    assert not is_provider_asset("https://elsewhere.example/x.CT")


def test_the_modules_host_sets_stay_inside_the_registrys_own_policy():
    # Declared in the module so its parsing stays independent of the registry,
    # and held inside it here so the two cannot drift into a route the registry
    # never allowed.
    declared = provider_definition("github").hosts
    assert PAGE_HOSTS <= declared
    assert ASSET_HOSTS <= declared


def test_an_identity_that_reaches_a_url_is_read_whole_or_not_at_all():
    """Truncating before validating turns a rejected value into a passing one.

    A 300 character branch shortened to 255 is a branch nobody named, and the
    tree route builds a raw file URL out of it: that is the same guess as
    substituting a likely one, arrived at differently.
    """
    long_branch = "release/" + "a" * 300
    payload = {"items": [{
        "full_name": "owner/repo", "html_url": "https://github.com/owner/repo",
        "default_branch": long_branch,
    }]}
    assert parse_repository_search(payload)[0].default_branch is None
    # The same rule for the repository identity itself, and for the page a row
    # will name as its source.
    assert parse_repository_search({"items": [
        {"full_name": "owner/" + "r" * 300, "html_url": "https://github.com/x"},
    ]}) == ()
    over_long_url = "https://github.com/owner/repo?" + "q" * 3000
    assert parse_repository_search({"items": [
        {"full_name": "owner/repo", "html_url": over_long_url},
    ]})[0].html_url == "https://github.com/owner/repo"


@pytest.mark.asyncio
async def test_an_over_long_branch_never_becomes_a_tree_request(tmp_path):
    payload = {"items": [{
        "full_name": "someone/Sort-Them-Ducks-CT", "description": "", "stargazers_count": 1,
        "pushed_at": "2026-08-01T00:00:00Z", "topics": [], "archived": False, "fork": False,
        "default_branch": "b" * 256, "html_url": "https://github.com/someone/Sort-Them-Ducks-CT",
    }]}
    network = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(payload).encode())),
        ("/releases", _Response(200, b"[]")),
    ])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    assert await service._github_search(["sort them ducks"]) == []
    assert not any("/git/trees/" in url for url in network.calls)


def test_a_single_player_table_feature_is_not_proof_of_a_multiplayer_cheat():
    """`unlock all` is what an ordinary table does.

    Used as a standalone exclusion it discarded a repository for describing a
    perfectly normal capability, before anything checked whether it even carries
    a table. `bypass` alone is the same kind of word: it needs the thing being
    bypassed named before it means anything here.
    """
    def candidate(description="", topics=()):
        return _candidate(description=description, topics=topics)

    assert not candidate("Single player table, unlock all items and costumes").is_out_of_scope
    assert not candidate("Single-player table: no recoil, infinite ammo").is_out_of_scope
    assert not candidate("bypasses the launcher check on startup").is_out_of_scope
    assert not candidate("skip intro video bypass").is_out_of_scope
    # Still refused, because the first product invariant rules these out
    # wherever they are found.
    for hostile in ("aimbot and wallhack", "undetected spoofer", "HWID changer", "external cheat"):
        assert candidate(hostile).is_out_of_scope
    for hostile in ("anti-cheat bypass", "bypass EAC", "battleye bypass", "bypass for Denuvo"):
        assert candidate(hostile).is_out_of_scope
    assert candidate(topics=("aimbot",)).is_out_of_scope

    # And a repository is ranked rather than dropped for the ordinary phrase.
    ranked = rank_repositories(
        (candidate("Table with unlock all items"),), lambda name: 1.0,
    )
    assert len(ranked) == 1


def test_a_title_that_is_not_spelled_in_latin_still_produces_a_query():
    # Restricted to ASCII letters and digits this produced no query at all, so
    # the provider silently disappeared for exactly the library entries a user
    # is least able to retype into something else.
    for alias in ("атомное сердце", "ファイナルファンタジー", "warhammer 40 000"):
        queries = build_search_queries([alias])
        assert queries and queries[0].startswith(f"{alias} cheat table")
    # A name with nothing readable in it still yields nothing rather than a
    # query made of punctuation.
    assert build_search_queries(["...", "   "]) == []


def test_a_committed_table_carries_gits_own_object_name():
    payload = {"truncated": False, "tree": [
        {"type": "blob", "path": "tables/Ducks.CT", "sha": "a" * 40, "size": 120},
        {"type": "blob", "path": "README.md", "sha": "b" * 40, "size": 10},
        {"type": "tree", "path": "tables", "sha": "c" * 40},
        {"type": "blob", "path": "broken.CT", "sha": "not-a-sha"},
        # Read whole or not at all, here too: a 41 character object name is not
        # a 40 character one, and a path longer than this reads names a
        # different file.
        {"type": "blob", "path": "over-long.CT", "sha": "c" * 41, "size": 1},
        {"type": "blob", "path": "d" * 1100 + ".CT", "sha": "e" * 40, "size": 1},
    ]}
    tables = parse_tree_tables(payload)
    assert [(item.path, item.blob_sha1, item.size_bytes) for item in tables] == [
        ("tables/Ducks.CT", "a" * 40, 120),
    ]
    assert tables[0].filename == "Ducks.CT"


def test_the_git_object_name_is_reproduced_exactly():
    # Not an assumption about git: the same bytes are hashed by git itself.
    data = b"table bytes\n"
    expected = subprocess.run(
        ["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True,
    ).stdout.decode().strip()
    assert git_blob_sha1(data) == expected
    assert verify_blob_sha1(data, expected)
    assert not verify_blob_sha1(data + b"x", expected)
    assert not verify_blob_sha1(data, "not-a-sha")


def test_a_route_never_takes_a_path_from_an_untrusted_string():
    assert raw_url("a/b", "main", "tables/Ducks.CT") == "https://raw.githubusercontent.com/a/b/main/tables/Ducks.CT"
    for bad in ("../secret", "/etc/passwd", "tables/../../secret", "a/./b", "a//b"):
        with pytest.raises(ValueError):
            raw_url("a/b", "main", bad)
    with pytest.raises(ValueError):
        tree_url("a/b", "main;rm -rf /")
    assert repo_from_source("/owner/repo/releases/tag/v1") == "owner/repo"
    assert repo_from_source("/owner") is None


def test_a_repository_identity_is_never_a_pair_of_path_segments():
    """One of the two routes takes this identity from another provider's page.

    `.` and `..` are not repository names but they are path segments, and left
    accepted they built `/repos/../../releases`, which is a different API route
    once the URL is normalized.
    """
    for hostile in ("/../../etc", "/./x/releases", "/a/../../b", "/owner/.."):
        assert repo_from_source(hostile) is None
    for hostile in ("../..", "./x", "a/..", "x/..."):
        with pytest.raises(ValueError):
            releases_url(hostile)
        with pytest.raises(ValueError):
            tree_url(hostile, "main")
    # A dot inside a name is ordinary and stays allowed.
    assert repo_from_source("/o.k/r-e_p.o") == "o.k/r-e_p.o"
    assert parse_repository_search({"items": [{"full_name": "../..", "html_url": "x"}]}) == ()


def test_an_exhausted_anonymous_budget_reads_as_a_wait():
    # GitHub answers an exhausted budget with 403 and a reset timestamp, and
    # uses 429 only for secondary limits. Both are waits.
    assert is_rate_limited(403, {"X-RateLimit-Remaining": "0"})
    assert is_rate_limited(429, {})
    assert not is_rate_limited(403, {"x-ratelimit-remaining": "12"})
    assert not is_rate_limited(404, {})
    assert rate_limit_retry_after({"retry-after": "30"}) == "30"
    assert rate_limit_retry_after({"x-ratelimit-reset": str(int(time.time()) + 40)}) is not None
    # A reset that has already passed, or none at all, yields no invented wait.
    assert rate_limit_retry_after({"x-ratelimit-reset": str(int(time.time()) - 10)}) is None
    assert rate_limit_retry_after({}) is None


def test_a_tree_row_takes_its_release_from_the_only_place_it_has_one():
    """Every provider that can name a release does, or the row lies by omission.

    A repository tree entry has no tag to read, so the filename is all there is,
    and it is parsed exactly as every other provider's is. A version shown for
    one source and withheld for another tells the user this table has none
    rather than that this source did not say, and the screen that lists stored
    tables is where that difference decides which revision they keep.
    """
    with collect_parse_issues():
        versioned = github_tree_record(_candidate(), TreeTable("tables/Ducks v1.4.CT", "a" * 40, 120), 0.9)
        # The release is in the directory and nowhere else, which is how a
        # repository that keeps every revision publishes one.
        in_path = github_tree_record(_candidate(), TreeTable("tables/v2.1/Ducks.CT", "c" * 40, 120), 0.9)
        bare = github_tree_record(_candidate(), TreeTable("tables/Ducks.CT", "b" * 40, 120), 0.9)
    assert versioned is not None and versioned.result.version == "1.4"
    assert in_path is not None and in_path.result.version == "2.1"
    # And nothing invented where neither the name nor the path carries one.
    assert bare is not None and bare.result.version is None


def test_a_tree_row_names_the_repository_and_keeps_its_anchor():
    with collect_parse_issues():
        record = github_tree_record(_candidate(), TreeTable("tables/Ducks.CT", "a" * 40, 120), 0.9)
    assert record is not None
    definition = provider_definition("github")
    assert record.result.provider_display_name == definition.provider_display_name
    assert record.result.provider_rank == definition.priority
    assert record.result.artifact_id == f"someone/Sort-Them-Ducks-CT:blob-{'a' * 40}"
    assert record.result.source_page == "https://github.com/someone/Sort-Them-Ducks-CT"
    assert record.direct_url == "https://raw.githubusercontent.com/someone/Sort-Them-Ducks-CT/main/tables/Ducks.CT"
    assert record.blob_sha1 == "a" * 40
    assert record.size_exact is True
    # A committed file has no publisher-declared version; nothing is inferred.
    assert record.result.version is None
    # The git object name is not a content digest and never presents itself as
    # one, because everything that identifies a table by SHA-256 would key on it.
    assert record.public_dict()["advertised_sha256"] is None
    assert "blob_sha1" not in record.public_dict()


@pytest.mark.asyncio
async def test_a_repository_is_offered_only_when_it_carries_something(tmp_path):
    # Measured live during the spike: candidates included single-file
    # repositories whose whole content was a README promising a table, and an
    # application whose description claimed to be one. Requiring an artifact
    # removes them without a spam-phrase list that would need maintaining.
    network = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, b"[]")),
        ("/git/trees/", _Response(200, json.dumps({"truncated": False, "tree": [
            {"type": "blob", "path": "README.md", "sha": "b" * 40, "size": 10},
        ]}).encode())),
    ])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    assert await service._github_search(["sort them ducks"]) == []


@pytest.mark.asyncio
async def test_releases_are_read_first_and_the_tree_only_when_they_carry_nothing(tmp_path):
    releases = (FIXTURES / "github_releases.json").read_bytes()
    network = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, releases)),
        ("/git/trees/", _Response(500)),
    ])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    rows = await service._github_search(["sort them ducks"])
    assert [row.result.artifact_id for row in rows] == ["someone/Sort-Them-Ducks-CT:asset-7"]
    assert rows[0].advertised_sha256 == "b" * 64
    # The tree costs a second core request out of sixty an hour, so it is not
    # asked for when the releases already answered.
    assert not any("/git/trees/" in url for url in network.calls)


@pytest.mark.asyncio
async def test_repository_search_index_is_reused_for_one_day(tmp_path):
    releases = (FIXTURES / "github_releases.json").read_bytes()
    cache = tmp_path / "cache.json"
    first = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, releases)),
    ])
    assert await CatalogService(first, cache)._github_search(["sort them ducks"])  # type: ignore[arg-type]

    second = _RouteNetwork([("/releases", _Response(200, releases))])
    rows = await CatalogService(second, cache)._github_search(["sort them ducks"])  # type: ignore[arg-type]
    assert rows
    assert not any("/search/repositories" in url for url in second.calls)


@pytest.mark.asyncio
async def test_stale_repository_index_is_a_soft_offline_fallback(tmp_path):
    releases = (FIXTURES / "github_releases.json").read_bytes()
    cache = tmp_path / "cache.json"
    first = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, releases)),
    ])
    assert await CatalogService(first, cache)._github_search(["sort them ducks"])  # type: ignore[arg-type]
    index_path = tmp_path / "github-repository-index.json"
    stored = json.loads(index_path.read_text(encoding="utf-8"))
    stored["queries"][0]["fetched"] = time.time() - GITHUB_INDEX_TTL_SECONDS - 1
    index_path.write_text(json.dumps(stored), encoding="utf-8")

    offline = _RouteNetwork([
        ("/search/repositories", _Response(500)),
        ("/releases", _Response(200, releases)),
    ])
    with collect_parse_issues() as issues:
        rows = await CatalogService(offline, cache)._github_search(["sort them ducks"])  # type: ignore[arg-type]
    assert rows
    assert issues.unavailable and "stale local copy" in issues.unavailable


def test_repository_index_cache_is_bounded_by_recent_use(tmp_path):
    service = CatalogService(None, tmp_path / "cache.json")  # type: ignore[arg-type]
    for index in range(MAX_GITHUB_INDEX_QUERIES + 1):
        service._save_github_index_query(f"query-{index}", (_candidate(),))
    stored = json.loads((tmp_path / "github-repository-index.json").read_text(encoding="utf-8"))
    assert len(stored["queries"]) == MAX_GITHUB_INDEX_QUERIES
    assert "query-0" not in {entry["query"] for entry in stored["queries"]}


@pytest.mark.asyncio
async def test_a_repository_with_no_usable_branch_never_reaches_the_tree_route(tmp_path):
    payload = {"items": [{
        "full_name": "someone/Sort-Them-Ducks-CT", "description": "", "stargazers_count": 1,
        "pushed_at": "2026-08-01T00:00:00Z", "topics": [], "archived": False, "fork": False,
        "default_branch": "..", "html_url": "https://github.com/someone/Sort-Them-Ducks-CT",
    }]}
    network = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(payload).encode())),
        ("/releases", _Response(200, b"[]")),
    ])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        assert await service._github_search(["sort them ducks"]) == []
    # No request is made for a ref nobody named, and the skip is counted rather
    # than looking like a repository that carries nothing.
    assert not any("/git/trees/" in url for url in network.calls)
    assert issues.degraded == 1


@pytest.mark.asyncio
async def test_a_committed_table_is_found_when_no_release_carries_one(tmp_path):
    network = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, b"[]")),
        ("/git/trees/", _Response(200, json.dumps({"truncated": False, "tree": [
            {"type": "blob", "path": "Ducks.CT", "sha": "a" * 40, "size": 120},
        ]}).encode())),
    ])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    rows = await service._github_search(["sort them ducks"])
    assert [row.blob_sha1 for row in rows] == ["a" * 40]
    assert rows[0].direct_url.startswith("https://raw.githubusercontent.com/")


@pytest.mark.asyncio
async def test_the_search_budget_is_a_wait_rather_than_a_failed_source(tmp_path):
    # GitHub reports an exhausted anonymous budget as 403 with a reset stamp
    # rather than as 429 with a `Retry-After`. Both become the one wait the rest
    # of the catalog knows how to sit out, and that wait belongs to the one
    # route it was asked of.
    reset = int(time.time()) + 30
    network = _RouteNetwork([
        ("/search/repositories", _Response(403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset),
        })),
    ])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    with collect_parse_issues():
        await service._github_search(["sort them ducks"])
    refused = service._github_search_refused()
    assert isinstance(refused, ProviderRateLimited)
    assert service._github_search_cooldown_until <= reset + 1


@pytest.mark.asyncio
async def test_an_exhausted_search_budget_still_uses_the_stale_repository_index(tmp_path):
    # Repository search is metered apart from the rest of the API, so an
    # exhausted search budget is a refresh this cannot make rather than a source
    # that has stopped answering: the stale window covers it, and the release
    # and tree reads below it are still live.
    releases = (FIXTURES / "github_releases.json").read_bytes()
    cache = tmp_path / "cache.json"
    first = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, releases)),
    ])
    assert await CatalogService(first, cache)._github_search(["sort them ducks"])  # type: ignore[arg-type]
    index_path = tmp_path / "github-repository-index.json"
    stored = json.loads(index_path.read_text(encoding="utf-8"))
    stored["queries"][0]["fetched"] = time.time() - GITHUB_INDEX_TTL_SECONDS - 1
    index_path.write_text(json.dumps(stored), encoding="utf-8")

    reset = int(time.time()) + 30
    limited = _RouteNetwork([
        ("/search/repositories", _Response(403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset),
        })),
        ("/releases", _Response(200, releases)),
    ])
    diagnostics = ProviderDiagnosticsStore(tmp_path / "providers.json")
    service = CatalogService(limited, cache, diagnostics)  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        rows = await service._github_search(["sort them ducks"])
    assert rows
    assert any("/releases" in url for url in limited.calls)
    assert issues.unavailable and "stale local copy" in issues.unavailable
    # The search owns this report, because the refusal is scoped to the one
    # route that was refused and writes no deadline for the source.
    assert not issues.unavailable_recorded
    # A budget one of three routes spends is not a cooldown for the source: it
    # would refuse the cached index and the live reads that just produced these
    # rows, for every game, until it ran out. The gate the next search meets is
    # what says so.
    assert diagnostics.can_attempt("github")
    assert not _recorded_cooldown(diagnostics, "github")


@pytest.mark.asyncio
async def test_a_refused_alias_never_costs_the_candidates_another_alias_recovered(tmp_path):
    # One search asks for one index per alias. Meeting the refusal again on the
    # second alias, with nothing cached behind that one, failed the whole source
    # and threw away the candidates the first alias had just recovered from its
    # own stale copy.
    releases = (FIXTURES / "github_releases.json").read_bytes()
    cache = tmp_path / "cache.json"
    first = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, releases)),
    ])
    assert await CatalogService(first, cache)._github_search(["sort them ducks"])  # type: ignore[arg-type]
    index_path = tmp_path / "github-repository-index.json"
    stored = json.loads(index_path.read_text(encoding="utf-8"))
    stored["queries"][0]["fetched"] = time.time() - GITHUB_INDEX_TTL_SECONDS - 1
    index_path.write_text(json.dumps(stored), encoding="utf-8")

    limited = _RouteNetwork([
        ("/search/repositories", _Response(403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 30),
        })),
        ("/releases", _Response(200, releases)),
    ])
    service = CatalogService(limited, cache)  # type: ignore[arg-type]
    aliases = ["sort them ducks", "quack quest"]
    # Two aliases have to be two queries, or the latch is not what this proves.
    assert len(build_search_queries(aliases)) == 2
    with collect_parse_issues() as issues:
        rows = await service._github_search(aliases)
    assert rows
    # The refusal is latched, so the alias with no copy of its own costs one
    # request and the aliases after it cost none.
    assert len([url for url in limited.calls if "/search/repositories" in url]) == 1
    assert any("/releases" in url for url in limited.calls)
    assert issues.unavailable and "stale local copy" in issues.unavailable


@pytest.mark.asyncio
async def test_a_second_search_inside_the_refusal_asks_the_index_for_nothing(tmp_path):
    # The refusal has a deadline written on it, and the copy on this disk plus
    # the live release and tree reads are what the source can still answer with
    # while it runs. Spending another search request to be told so again is the
    # one thing that must not happen.
    releases = (FIXTURES / "github_releases.json").read_bytes()
    cache = tmp_path / "cache.json"
    first = _RouteNetwork([
        ("/search/repositories", _Response(200, json.dumps(_search_payload("someone/Sort-Them-Ducks-CT")).encode())),
        ("/releases", _Response(200, releases)),
    ])
    assert await CatalogService(first, cache)._github_search(["sort them ducks"])  # type: ignore[arg-type]
    index_path = tmp_path / "github-repository-index.json"
    stored = json.loads(index_path.read_text(encoding="utf-8"))
    stored["queries"][0]["fetched"] = time.time() - GITHUB_INDEX_TTL_SECONDS - 1
    index_path.write_text(json.dumps(stored), encoding="utf-8")

    limited = _RouteNetwork([
        ("/search/repositories", _Response(403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 300),
        })),
        ("/releases", _Response(200, releases)),
    ])
    diagnostics = ProviderDiagnosticsStore(tmp_path / "providers.json")
    service = CatalogService(limited, cache, diagnostics)  # type: ignore[arg-type]
    with collect_parse_issues():
        assert await service._github_search(["sort them ducks"])
    searched = len([url for url in limited.calls if "/search/repositories" in url])

    limited.calls.clear()
    with collect_parse_issues() as issues:
        rows = await service._github_search(["sort them ducks"])
    assert searched == 1
    assert rows
    assert not any("/search/repositories" in url for url in limited.calls)
    assert any("/releases" in url for url in limited.calls)
    assert issues.unavailable and "stale local copy" in issues.unavailable


@pytest.mark.asyncio
async def test_an_exhausted_search_budget_with_no_local_copy_answers_nothing(tmp_path):
    # Nothing is recoverable, so the source answers with no rows and says why.
    # It is still not a cooldown for the source: a search of another game whose
    # index this device does hold must not be refused before it can read it.
    network = _RouteNetwork([
        ("/search/repositories", _Response(403, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 30),
        })),
    ])
    diagnostics = ProviderDiagnosticsStore(tmp_path / "providers.json")
    service = CatalogService(network, tmp_path / "cache.json", diagnostics)  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        assert await service._github_search(["sort them ducks"]) == []
    assert issues.unavailable and "no local copy to fall back on" in issues.unavailable
    assert diagnostics.can_attempt("github")
    assert not _recorded_cooldown(diagnostics, "github")


@pytest.mark.asyncio
async def test_one_unreadable_repository_never_costs_the_others(tmp_path):
    payload = json.dumps(_search_payload("someone/Sort-Them-Ducks-CT", "other/Sort-Them-Ducks")).encode()
    releases = (FIXTURES / "github_releases.json").read_bytes()

    class _Network:
        def __init__(self):
            self.calls: list[str] = []

        async def get(self, url, **kwargs):
            self.calls.append(url)
            if "/search/repositories" in url:
                return _Response(200, payload)
            if "someone/Sort-Them-Ducks-CT" in url:
                return _Response(500)
            if "/releases" in url:
                return _Response(200, releases)
            raise AssertionError(f"unexpected URL: {url}")

    service = CatalogService(_Network(), tmp_path / "cache.json")  # type: ignore[arg-type]
    with collect_parse_issues() as issues:
        rows = await service._github_search(["sort them ducks"])
    assert [row.result.topic_id for row in rows] == ["other/Sort-Them-Ducks"]
    assert issues.failed == 1


@pytest.mark.asyncio
async def test_an_answer_that_is_not_json_is_a_provider_failure(tmp_path):
    network = _RouteNetwork([("/search/repositories", _Response(200, b"<html>challenge</html>"))])
    service = CatalogService(network, tmp_path / "cache.json")  # type: ignore[arg-type]
    with pytest.raises(NetworkError, match="not valid JSON"):
        await service._github_search(["sort them ducks"])


def test_the_real_search_and_tree_answers_this_adapter_was_written_against():
    """Captured GitHub answers, not payloads shaped to suit this parser.

    Discovery reads the repository index because code search refuses an
    anonymous client, so these two answers are the whole route: which
    repositories a query returns, and which of their committed files are
    tables. A parser that still passes the hand-built cases and stops reading a
    real answer fails here rather than on a device with a spent rate limit.
    """
    candidates = parse_repository_search(
        json.loads((FIXTURES / "github_repo_search.json").read_text(encoding="utf-8"))
    )
    # Eleven were returned; the ones that are not `owner/repo` are dropped
    # rather than failing the answer they arrived in.
    assert len(candidates) == 6
    first = candidates[0]
    assert first.full_name == "The-Grand-Archives/Elden-Ring-CT-TGA"
    assert first.stars == 337
    # A repository's own default branch is read, never assumed to be `main`.
    assert first.default_branch == "master"
    assert {candidate.default_branch for candidate in candidates} > {"main"}

    tables = parse_tree_tables(
        json.loads((FIXTURES / "github_tree.json").read_text(encoding="utf-8"))
    )
    assert [table.path for table in tables] == [
        "tables/0.32_767.ct",
        "tables/0.32table__129.ct",
        "tables/0.bountytrainsteamv2_108.ct",
        "tables/0.conflicksrsb_441.ct",
    ]
    # Git's object name is what a committed file advertises instead of a
    # content digest, and it is carried exactly as given.
    assert all(len(table.blob_sha1) == 40 for table in tables)
