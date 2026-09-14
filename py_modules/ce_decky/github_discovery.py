"""Turn a game name into GitHub repositories that actually carry a table.

GitHub was registered as a source long before it could answer a search. The
artifact half was already solved: production consumes a repository's release
assets once another catalog hands it an exact `owner/repo`. What was missing is
how that repository is reached when nobody linked it, and `DESIGN.md` forbids
shipping a per-game registry. Searching the repository index at run time is not
such a registry, so this module is only discovery plus the integrity anchor the
routes it finds need.

Anonymous limits, read from the API itself during the research spike: ten search
requests per minute and sixty core requests per hour, counted per client IP, so
every device carries its own budget. Code search answers HTTP 401 anonymously,
so a table cannot be found by file extension and the repository index is the
only route.

Two artifact routes exist and they differ in what they can prove. A release
asset may advertise a SHA-256, which the release parser already reads. A file
committed in the tree advertises a git blob SHA-1, which is not a digest of the
bytes alone but is still exactly verifiable, because git defines the object
name as the SHA-1 of `blob <length>\\0` followed by the content.
`git_blob_sha1` reproduces that, so the tree route is not left without an
anchor.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from pathlib import PurePosixPath
import re
import time
from typing import Callable, Mapping
from urllib.parse import quote, urlencode

from .network import is_transport_ready_url
from .providers import ARTIFACT_SUFFIXES

API_ROOT = "https://api.github.com"
RAW_HOST = "raw.githubusercontent.com"
# Where this provider's own pages and its own artifacts live. Declared here for
# the same reason the registry declares them: a URL the API hands back is a
# claim, and one that names somewhere else is not this provider's. A regression
# holds these inside the registry's own host policy so the two cannot drift.
PAGE_HOSTS = frozenset({"github.com"})
ASSET_HOSTS = frozenset({
    "github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", RAW_HOST,
})

# How much of an anonymous minute one search may spend. Ten searches a minute is
# the whole device's budget, shared with anything else that queries GitHub, so a
# single game search takes a small fixed slice of it rather than one request per
# alias.
MAX_SEARCH_QUERIES = 2
# How many ranked repositories are inspected. Each one costs at least one core
# request out of sixty an hour, and a second when its releases carry nothing.
MAX_CANDIDATES = 3
SEARCH_PER_PAGE = 20
RELEASES_PER_PAGE = 10
MAX_SEARCH_ITEMS = 1000
MAX_TREE_ENTRIES = 50_000
MAX_TREE_ROWS = 40
# A repository match is a far noisier signal than a forum topic title, so the
# floor does most of the filtering.
MATCH_FLOOR = 0.55

# The one place a downloadable artifact suffix is declared. Retyped here it
# went out of step the moment `.rar` was added to it.
SUPPORTED_SUFFIXES = ARTIFACT_SUFFIXES
# A repository identity reaches an API path, and one of the two routes that
# supplies it is a link on another provider's page, which is third-party text.
# `.` and `..` are not repository names but they are path segments: left
# accepted, `github.com/../../x` built `/repos/../../releases`, which resolves
# to an entirely different route once the URL is normalized. Both halves are
# built from the same part pattern so they cannot drift apart again.
REPO_CHARS_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


def is_repo_part(value: str) -> bool:
    """An owner or repository name, and never a bare path segment.

    A name made only of dots is not a name, it is `.` or `..`, and the check is
    written as a predicate rather than folded into a pattern because expressing
    "not all dots" inside a composed regex is where the first attempt at this
    quietly stopped rejecting `./x`.
    """
    return bool(REPO_CHARS_RE.fullmatch(value)) and value.strip(".") != ""


def is_repo(value: str) -> bool:
    """An exact `owner/repo`, with both halves being names rather than segments."""
    parts = value.split("/")
    return len(parts) == 2 and all(is_repo_part(part) for part in parts)


# Git's own reference grammar is narrower than this, but what matters here is
# that a branch reaches two URL builders: a name carrying a `..` segment is
# refused outright rather than relied on being percent-encoded downstream.
BRANCH_RE = re.compile(r"^(?!\.)(?!.*\.\.)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
BLOB_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")

# Repositories that distribute Cheat Engine itself, or a general table dump, are
# not a table for the game being searched for. They match every query weakly and
# would otherwise crowd out the one repository that is about the game.
TOOL_NAME_RE = re.compile(r"(?i)(?:^|[/_\-])cheat[\s_-]*engine[\s_-]*[0-9]+(?:\.[0-9]+)*(?:$|[/_\-])")
GENERIC_NAME_RE = re.compile(r"(?i)^(?:[a-z0-9._-]+/)?(?:ce[\s_-]*)?cheat[\s_-]*(?:engine[\s_-]*)?tables?$")

# Words every candidate carries, because they are the words the query supplied.
# Left in the name they are shared vocabulary that pulls every candidate toward
# every game, and the live spike showed the cost: for one game the maintained
# table is named `Elden-Ring-CT-TGA` and a second real one `eldenringcheatengine`,
# so one carries the boilerplate as a token and the other glued on with no
# separator. Both are removed before the name is matched, and a name that is
# nothing but boilerplate keeps its text rather than becoming empty.
BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:cheat[\s_-]*engine[\s_-]*tables?|cheat[\s_-]*tables?|cheat[\s_-]*engine|ce[\s_-]*tables?|tables?|ct)\b"
)
SQUASHED_BOILERPLATE_RE = re.compile(r"(?i)(?:cheatenginetables?|cheattables?|cheatengine|cetables?)")

# A table is a single-player memory edit the user runs on their own machine.
# These words name the other thing entirely: cheats built to be used against
# other players and to stay hidden from anti-cheat. The first product invariant
# rules those out, and the repository index is full of them, so they are dropped
# here rather than offered and refused later.
OUT_OF_SCOPE_RE = re.compile(
    r"(?i)\b(?:aim[\s_-]*bot|trigger[\s_-]*bot|wall[\s_-]*hack|esp|"
    r"spoofer|hwid|undetected|injector|internal[\s_-]*cheat|external[\s_-]*cheat)\b"
)
# `bypass` on its own is not one of those words. It is ordinary in a
# single-player repository, which is why it needs the thing being bypassed
# named before it means anything here. `unlock all` and `no recoil` were on that
# list too and are simply table capabilities: a repository was discarded for
# describing what a perfectly ordinary table does, before anything checked
# whether it even carries one.
ANTI_CHEAT_NAME = r"anti[\s_-]*cheat|eac|easy[\s_-]*anti[\s_-]*cheat|battl?eye|vanguard|denuvo|punkbuster|drm"
OUT_OF_SCOPE_BYPASS_RE = re.compile(
    rf"(?i)\b(?:(?:{ANTI_CHEAT_NAME})\b[\w\s_/-]{{0,24}}\bbypass"
    rf"|bypass\b[\w\s_/-]{{0,24}}\b(?:{ANTI_CHEAT_NAME}))\b"
)


def _text(value: object, limit: int = 4096) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _identity(value: object, limit: int) -> str:
    """A field that ends up in a URL, read whole or not at all.

    Truncating one of these before validating it is how a value that was too
    long becomes a different value that passes: a 300 character branch shortened
    to 255 is a branch nobody named, and building a raw file route from it is
    the same guess as substituting a likely one.
    """
    return value if isinstance(value, str) and len(value) <= limit else ""


@dataclass(frozen=True)
class RepoCandidate:
    """One repository the index offered, with only the fields ranking uses."""

    full_name: str
    description: str
    stars: int
    pushed_at: str | None
    topics: tuple[str, ...]
    archived: bool
    fork: bool
    # Absent when the search answer named no branch this understands. It is not
    # replaced with a likely one: the tree route builds a raw file URL out of
    # it, and a guessed ref is a guessed path, which this project does not do.
    # The release route needs no branch and is unaffected.
    default_branch: str | None
    html_url: str

    def __post_init__(self) -> None:
        if not is_repo(self.full_name):
            raise ValueError("repository identity is invalid")
        if self.default_branch is not None and (
            not BRANCH_RE.fullmatch(self.default_branch) or len(self.default_branch) > 255
        ):
            raise ValueError("repository default branch is invalid")

    @property
    def name_text(self) -> str:
        """The repository name alone, separators normalized.

        The owner is deliberately excluded: an account name is almost never the
        game, and including it only dilutes the match.
        """
        return re.sub(r"[._-]+", " ", self.full_name.split("/", 1)[-1]).strip()

    @property
    def match_text(self) -> str:
        """The name with the shared vocabulary of the search removed.

        Stripping runs twice because the boilerplate appears both as separated
        words and glued onto the name.
        """
        squashed = SQUASHED_BOILERPLATE_RE.sub(" ", self.name_text)
        stripped = " ".join(BOILERPLATE_RE.sub(" ", squashed).split())
        return stripped or self.name_text

    @property
    def is_out_of_scope(self) -> bool:
        """A multiplayer cheat rather than a single-player table."""
        text = f"{self.full_name} {self.description} {' '.join(self.topics)}"
        return bool(OUT_OF_SCOPE_RE.search(text) or OUT_OF_SCOPE_BYPASS_RE.search(text))

    @property
    def is_generic(self) -> bool:
        """Cheat Engine itself, or a table dump that is about no game."""
        return bool(TOOL_NAME_RE.search(self.full_name) or GENERIC_NAME_RE.match(self.full_name))


@dataclass(frozen=True)
class TreeTable:
    """A table committed in a repository tree, with its git object name."""

    path: str
    blob_sha1: str
    size_bytes: int | None

    def __post_init__(self) -> None:
        if not self.path or not BLOB_SHA1_RE.fullmatch(self.blob_sha1):
            raise ValueError("committed table identity is invalid")

    @property
    def filename(self) -> str:
        return PurePosixPath(self.path).name


def build_search_queries(game_aliases: list[str], *, limit: int = MAX_SEARCH_QUERIES) -> list[str]:
    """One query per alias, narrowed to table repositories.

    Forks and archived repositories are excluded by the index rather than after
    the fact, because a page of results is the scarce thing: an anonymous client
    gets ten searches a minute, and a row spent on a repository that will be
    dropped is a row the real one cannot use.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 8):
        raise ValueError("GitHub query limit is invalid")
    queries: list[str] = []
    for alias in game_aliases:
        # Unicode-aware on purpose. Restricted to ASCII letters and digits this
        # produced no query at all for a Cyrillic or CJK library entry, so the
        # provider silently disappeared for exactly the titles a user is least
        # able to retype. Tokens are bounded so one pathological name cannot
        # build an unbounded query.
        tokens = [token[:64] for token in re.split(r"[^\w]+", alias if isinstance(alias, str) else "", flags=re.UNICODE) if token]
        cleaned = " ".join(tokens[:12]).strip()
        if not cleaned:
            continue
        query = f"{cleaned} cheat table in:name,description,topics fork:false archived:false"
        if query not in queries:
            queries.append(query)
        if len(queries) >= limit:
            break
    return queries


def search_url(query: str, *, per_page: int = SEARCH_PER_PAGE) -> str:
    return f"{API_ROOT}/search/repositories?{urlencode({'q': query, 'per_page': per_page})}"


def releases_url(repo: str, *, per_page: int = RELEASES_PER_PAGE) -> str:
    _require_repo(repo)
    return f"{API_ROOT}/repos/{repo}/releases?{urlencode({'per_page': per_page})}"


def tree_url(repo: str, branch: str) -> str:
    _require_repo(repo)
    if not BRANCH_RE.fullmatch(branch):
        raise ValueError("repository branch is invalid")
    return f"{API_ROOT}/repos/{repo}/git/trees/{quote(branch, safe='')}?recursive=1"


def raw_url(repo: str, branch: str, path: str) -> str:
    _require_repo(repo)
    if not BRANCH_RE.fullmatch(branch):
        raise ValueError("repository branch is invalid")
    segments = path.split("/")
    if not path or path.startswith("/") or any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("committed table path is invalid")
    safe_path = "/".join(quote(part, safe="") for part in path.split("/"))
    return f"https://{RAW_HOST}/{repo}/{quote(branch, safe='')}/{safe_path}"


def repo_page_url(repo: str) -> str:
    """The page a repository actually has, built rather than taken."""
    _require_repo(repo)
    return f"https://github.com/{repo}"


def is_provider_page(url: object) -> bool:
    """Whether a URL the API declared is one of this provider's own pages."""
    return isinstance(url, str) and len(url) <= 2048 and is_transport_ready_url(url, PAGE_HOSTS)


def is_provider_asset(url: object) -> bool:
    """Whether a declared download URL is one this provider actually serves.

    Held to what the transport will accept structurally, not merely to scheme
    and host: userinfo and a non-default port pass a hostname check and are
    refused later, so a row promising a direct download was offered for a URL
    that could only ever fail once the user selected it.
    """
    return isinstance(url, str) and len(url) <= 2048 and is_transport_ready_url(url, ASSET_HOSTS)


def repo_from_source(source: str) -> str | None:
    """The exact `owner/repo` a catalog link names, or nothing."""
    parts = [part for part in source.split("/") if part]
    if len(parts) < 2 or not all(is_repo_part(part) for part in parts[:2]):
        return None
    return f"{parts[0]}/{parts[1]}"


def _require_repo(repo: str) -> None:
    if not is_repo(repo):
        raise ValueError("repository identity is invalid")


def parse_repository_search(payload: object) -> tuple[RepoCandidate, ...]:
    """Read a repository search answer, dropping rows rather than failing."""
    if not isinstance(payload, dict):
        raise ValueError("repository search answer is not an object")
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > MAX_SEARCH_ITEMS:
        raise ValueError("repository search answer has no usable items")
    found: list[RepoCandidate] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        full_name = _identity(item.get("full_name"), 201)
        if not is_repo(full_name):
            continue
        stars = item.get("stargazers_count")
        topics = item.get("topics")
        branch = _identity(item.get("default_branch"), 255)
        if not BRANCH_RE.fullmatch(branch):
            # The default branch is what every raw file URL is built from. A
            # name this does not understand, or none at all, leaves the
            # repository without one rather than acquiring the likeliest guess,
            # which would send this looking for tables on a ref nobody named.
            branch = None
        found.append(RepoCandidate(
            full_name=full_name,
            description=_text(item.get("description")),
            stars=stars if isinstance(stars, int) and not isinstance(stars, bool) else 0,
            pushed_at=_text(item.get("pushed_at"), 64) or None,
            topics=tuple(topic for topic in topics if isinstance(topic, str))[:40] if isinstance(topics, list) else (),
            archived=bool(item.get("archived")),
            fork=bool(item.get("fork")),
            default_branch=branch or None,
            # This is what a row shows as its source, what an imported table
            # records as where it came from, and the Referer the artifact
            # transfer names. A declared URL is used only when it is one of
            # this provider's own pages; anything else falls back to the page
            # the repository certainly has. It is also read whole rather than
            # shortened, because a truncated URL is a different page.
            html_url=(
                _identity(item.get("html_url"), 2048)
                if is_provider_page(item.get("html_url"))
                else repo_page_url(full_name)
            ),
        ))
    return tuple(found)


def rank_repositories(
    candidates: tuple[RepoCandidate, ...],
    scorer: Callable[[str], float],
    *,
    floor: float = MATCH_FLOOR,
    limit: int = MAX_CANDIDATES,
    allow_generic: bool = False,
) -> tuple[tuple[RepoCandidate, float], ...]:
    """Order candidates by how well they match the game, not by popularity.

    Stars break ties only, and the name is scored rather than a concatenation of
    everything known about the repository: the live spike showed that folding
    the description in dilutes the match far enough for the best maintained
    table to lose to an unrelated repository with a terse name.
    """
    scored: list[tuple[RepoCandidate, float]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.full_name in seen:
            continue
        seen.add(candidate.full_name)
        if candidate.archived:
            continue
        if candidate.is_generic and not allow_generic:
            continue
        if candidate.is_out_of_scope:
            continue
        value = scorer(candidate.match_text)
        if value >= floor:
            scored.append((candidate, value))
    scored.sort(key=lambda pair: (-pair[1], -pair[0].stars, pair[0].full_name))
    return tuple(scored[:limit])


def parse_tree_tables(payload: object) -> tuple[TreeTable, ...]:
    """Tables committed in the repository tree, each with its git object name."""
    if not isinstance(payload, dict):
        raise ValueError("repository tree answer is not an object")
    if payload.get("truncated") is not False:
        # GitHub states this on every tree answer, so completeness is read from
        # what it says rather than from the absence of a warning: a missing,
        # null or differently typed flag is an answer that did not claim to be
        # whole. Read as if it had been, a repository whose committed table sits
        # past the cut looks like a repository with no table, which is a wrong
        # answer rather than a missing one.
        raise ValueError("repository tree answer does not declare itself complete")
    entries = payload.get("tree")
    if not isinstance(entries, list) or len(entries) > MAX_TREE_ENTRIES:
        raise ValueError("repository tree answer has no usable entries")
    rows: list[TreeTable] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        # Both read whole: a shortened path names a different file and a
        # shortened object name is a different object. Either would be caught by
        # the git object check at transfer time, which is exactly the point at
        # which it is too late to be useful.
        path = _identity(entry.get("path"), 1024)
        if not path or PurePosixPath(path).suffix.casefold() not in SUPPORTED_SUFFIXES:
            continue
        blob = _identity(entry.get("sha"), 40)
        if not BLOB_SHA1_RE.fullmatch(blob):
            continue
        size = entry.get("size")
        try:
            rows.append(TreeTable(
                path=path,
                blob_sha1=blob,
                size_bytes=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
            ))
        except ValueError:
            continue
        if len(rows) >= MAX_TREE_ROWS:
            break
    return tuple(rows)


def git_blob_sha1(data: bytes) -> str:
    """Reproduce git's object name for a blob: SHA-1 of `blob <len>\\0` + content."""
    return sha1(b"blob %d\0" % len(data) + data).hexdigest()


def verify_blob_sha1(data: bytes, expected: str) -> bool:
    return bool(BLOB_SHA1_RE.fullmatch(expected or "")) and git_blob_sha1(data) == expected


def rate_limit_retry_after(headers: Mapping[str, str], *, now: float | None = None) -> str | None:
    """How long GitHub says to wait, as the header value a cooldown carries.

    GitHub answers an exhausted anonymous budget with 403 and a reset timestamp
    rather than with `Retry-After`, so a wait is only expressible by converting
    that timestamp. A missing or already-passed reset yields nothing rather than
    a guessed number.
    """
    lowered = {key.casefold(): value for key, value in headers.items()}
    retry_after = lowered.get("retry-after")
    if retry_after and str(retry_after).strip().isdigit():
        return str(retry_after).strip()
    try:
        reset = int(str(lowered.get("x-ratelimit-reset")).strip())
    except (TypeError, ValueError):
        return None
    remaining = int(reset - (time.time() if now is None else now))
    return str(remaining) if 0 < remaining <= 24 * 60 * 60 else None


def is_rate_limited(status: int, headers: Mapping[str, str]) -> bool:
    """Whether an answer is the anonymous budget rather than a refusal.

    GitHub reports an exhausted budget as 403 with `x-ratelimit-remaining: 0`,
    and uses 429 for secondary limits, so both are waits rather than failures.
    """
    if status == 429:
        return True
    lowered = {key.casefold(): value for key, value in headers.items()}
    return status == 403 and str(lowered.get("x-ratelimit-remaining", "")).strip() == "0"
