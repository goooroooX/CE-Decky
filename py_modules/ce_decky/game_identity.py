from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
import re
import unicodedata

from .text import utf8_len

TRADEMARKS = str.maketrans({'™': '', '®': '', '©': ''})
REQUEST_RE = re.compile(r'\[?\s*request\s*\]?', re.I)
PLATFORM_TAGS = {'steam', 'gog', 'epic', 'gamepass'}

# Only isolated roman numerals are converted. This intentionally excludes I,
# because single "i" tokens are too noisy in natural titles.
ROMAN = {
    'ii': '2', 'iii': '3', 'iv': '4', 'v': '5', 'vi': '6', 'vii': '7',
    'viii': '8', 'ix': '9', 'x': '10',
}

# Decorations which often appear in a shortcut name but are not part of the
# catalog's canonical title. We keep the unstripped form as the primary alias.
EDITION_PHRASES = (
    'ultimate edition', 'deluxe edition', 'complete edition',
    'game of the year edition', 'goty edition', 'definitive edition',
)
PLATFORM_DECORATIONS = (
    'steam', 'gog', 'epic games', 'epic', 'game pass', 'gamepass',
    'dx11', 'dx12', 'directx 11', 'directx 12', 'x64', 'win64',
)
STOPWORDS = {'the', 'a', 'an', 'edition'}
STRONG_IDENTITY_QUALIFIERS = {'remake', 'remaster', 'remastered', 'classic', 'hd', 'vr'}

MAX_IDENTITY_TEXT_BYTES = 4096
MAX_QUERY_ALIASES = 16
MAX_CANDIDATES = 1000


def _identity_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f'{field} contains control characters')
    if utf8_len(value, field) > MAX_IDENTITY_TEXT_BYTES:
        raise ValueError(f'{field} is too long')
    return value


def _split_alnum_boundaries(s: str) -> str:
    # ELEX2 -> ELEX 2, Cyberpunk2077 -> Cyberpunk 2077, but leave 2K intact
    # only when the digit/letter transition is a conventional suffix/prefix.
    s = re.sub(r'(?<=[A-Za-zА-Яа-яЁё])(?=\d)', ' ', s)
    s = re.sub(r'(?<=\d)(?=[A-Za-zА-Яа-яЁё])', ' ', s)
    return s


def normalize_title(s: str) -> str:
    s = _identity_text(s, 'game identity')
    s = unicodedata.normalize('NFKC', s.translate(TRADEMARKS)).casefold()
    s = _split_alnum_boundaries(s)
    s = re.sub(r'[\[\](){}:;,_+\-/\\|"“”‘’]+', ' ', s)
    tokens = [ROMAN.get(t, t) for t in re.findall(r'[\w]+', s, flags=re.UNICODE)]
    return ' '.join(tokens)


def _strip_decorations(primary: str) -> str:
    out = primary
    for phrase in EDITION_PHRASES:
        out = re.sub(rf'\b{re.escape(phrase)}\b', ' ', out)
    # Backward compatible short GOTY/complete/deluxe/ultimate forms.
    out = re.sub(r'\b(goty|complete|deluxe|ultimate)\b', ' ', out)
    for phrase in PLATFORM_DECORATIONS:
        out = re.sub(rf'\b{re.escape(phrase)}\b', ' ', out)
    return re.sub(r'\s+', ' ', out).strip()


def _segments(name: str) -> list[str]:
    """Produce title segments, including bilingual provider labels.

    Playground frequently uses forms such as
    "The Witcher 3: Wild Hunt / Ведьмак 3: Дикая Охота". Both halves are
    valid identities and must be searchable independently.
    """
    raw = unicodedata.normalize('NFKC', name.translate(TRADEMARKS))
    # Slash and pipe are only treated as identity separators when both halves
    # contain letters. This avoids damaging ordinary punctuation-heavy names.
    parts = [raw]
    for sep in (' / ', ' | '):
        if sep in raw:
            maybe = [p.strip() for p in raw.split(sep) if p.strip()]
            if len(maybe) >= 2 and all(re.search(r'[A-Za-zА-Яа-яЁё]', p) for p in maybe):
                parts.extend(maybe)
    return parts


def aliases(display_name: str, executable: str | None = None) -> list[str]:
    out: list[str] = []
    for segment in _segments(display_name):
        primary = normalize_title(segment)
        if primary and primary not in out:
            out.append(primary)
        fallback = _strip_decorations(primary)
        if fallback and fallback not in out:
            out.append(fallback)

    # Search identity is ranked, never merged. The name the library shows is the
    # only source used while it exists: Steam's own name for a library game is
    # authoritative, and merging in a second source would let a folder that
    # happens to name a different game - a mod installed into its base game's
    # directory - outrank the entry the user actually chose. A name that finds
    # nothing is retried against the install directory by the search itself,
    # which is a decision made on results rather than in advance.
    if out:
        return out

    directory = install_directory_title(executable)
    if directory:
        return [directory]

    # An executable is named for whatever the build happens to be, so it is the
    # last resort and only for an entry with no usable name at all: a repack of
    # `Cadence: A Silent Requiem` ships `Cadence.exe`, and the alias
    # `cadence` matched the unrelated `Cadence of Fortune` confidently.
    exe = executable_stem(executable)
    if exe:
        norm = normalize_title(exe)
        if norm:
            out.append(norm)
    return out


# Repack folders carry the title plus packaging words and the group that made
# them; none of that is part of the game's identity.
_RELEASE_GROUP_RE = re.compile(r'-[A-Za-z0-9_]{2,}$')
_PACKAGING_WORDS = frozenset({
    'digital', 'deluxe', 'ultimate', 'premium', 'complete', 'definitive', 'collectors',
    'collector', 'anniversary', 'enhanced', 'goty', 'edition', 'repack', 'multi',
})
# Directories that describe where a build lives or where a library is kept,
# never which game it is.
_STRUCTURAL_DIRECTORIES = frozenset({
    'home', 'deck', 'games', 'game', 'applications', 'emulation', 'tools', 'launchers',
    'roms', 'bin', 'binaries', 'win64', 'win32', 'x64', 'x86', 'data', 'content',
    'steamapps', 'common', 'program files', 'program files x86', 'users', 'local',
})
MIN_DIRECTORY_TITLE_CHARS = 4


def install_directory_title(executable: str | None) -> str | None:
    """The game title a repack wrote into the directory it installed into.

    A library entry's name is the user's, and users abbreviate and misspell it;
    the folder a repack installs into carries the whole title. Measured across
    the installed library, this is what lets `NX: Crimson Tide Resynced` and the
    misspelled `Neon Bazar Kyoto` reach their real topics.

    Only the launched program's own path is read, never the arguments after it,
    so an emulator launcher contributes its launcher directory - which is
    structural, and therefore nothing - rather than a ROM path.
    """
    if not executable:
        return None
    text = executable.strip()
    if text.startswith('"'):
        text = text[1:].split('"', 1)[0]
    else:
        text = text.split(' ', 1)[0]
    if not text:
        return None
    path = PureWindowsPath(text) if ('\\' in text or re.match(r'^[A-Za-z]:', text)) else PurePosixPath(text)

    best: list[str] = []
    for part in path.parent.parts:
        cleaned = _RELEASE_GROUP_RE.sub('', part).replace('.', ' ').replace('_', ' ')
        words = [
            word for word in normalize_title(cleaned).split()
            if word not in _PACKAGING_WORDS and not _is_year(word)
        ]
        title = ' '.join(words)
        # `bin64` and friends survive a plain structural-name check but say just
        # as little, so a component made only of structural words and numbers is
        # not a title either.
        if not title or len(title) < MIN_DIRECTORY_TITLE_CHARS:
            continue
        if all(word in _STRUCTURAL_DIRECTORIES or word.isdigit() for word in words):
            continue
        # A game binary can sit several directories below the install root, and
        # the deepest folder is then a build path. The most words wins, and the
        # deepest wins a tie, because that is the folder closest to the game.
        if len(words) >= len(best):
            best = words
    return ' '.join(best) or None


def _is_year(word: str) -> bool:
    return len(word) == 4 and word.isdigit() and 1970 <= int(word) <= 2099


def executable_stem(executable: str | None) -> str | None:
    if not executable:
        return None
    s = executable.strip().strip('"')
    # Support either Windows or POSIX shortcut strings regardless of host OS.
    name = PureWindowsPath(s).name if ('\\' in s or re.match(r'^[A-Za-z]:', s)) else PurePosixPath(s).name
    if not name.casefold().endswith('.exe'):
        return None
    return name[:-4]


def compact(s: str) -> str:
    return re.sub(r'[^\w]+', '', normalize_title(s), flags=re.UNICODE)


def _tokens(s: str) -> list[str]:
    return normalize_title(s).split()


def _meaningful_tokens(s: str) -> set[str]:
    return {t for t in _tokens(s) if t not in STOPWORDS}


def _numeric_tokens(s: str) -> set[str]:
    return {t for t in _tokens(s) if t.isdigit()}


def _year_tokens(s: str) -> set[int]:
    return {int(t) for t in _tokens(s) if t.isdigit() and len(t) == 4 and 1900 <= int(t) <= 2099}


def _qualifiers(s: str) -> set[str]:
    return set(_tokens(s)) & STRONG_IDENTITY_QUALIFIERS


def _strip_year(s: str, year: int) -> str:
    return re.sub(rf'\b{year}\b', ' ', normalize_title(s)).strip()


def _acronym(s: str) -> str:
    toks = [t for t in _tokens(s) if t not in STOPWORDS]
    letters = ''.join(t if t.isdigit() else t[0] for t in toks if t)
    return letters


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def _trigrams(s: str) -> set[str]:
    c = compact(s)
    if len(c) < 3:
        return {c} if c else set()
    return {c[i:i+3] for i in range(len(c)-2)}


def _identity_prefix(title: str) -> str:
    """Remove article/table decorations before title similarity is measured.

    This is deliberately narrow. It is for provider result labels, not a
    general natural-language parser.
    """
    n = normalize_title(title)
    markers = (
        ' таблица ', ' cheat engine ', ' trainer ', ' трейнер ',
        ' table for cheat engine ', ' cheat table ',
    )
    indexes = [n.find(m) for m in markers if n.find(m) > 0]
    if indexes:
        n = n[:min(indexes)].strip()
    return n


@dataclass(frozen=True)
class Candidate:
    title: str
    provider_trust: float = 0.5
    platform_tags: tuple[str, ...] = ()
    has_artifact: bool = True
    provider_id: str | None = None
    # Prefer provider-native release metadata when available. Four-digit years
    # in shortcut names are disambiguators, not ordinary sequel numbers.
    release_year: int | None = None

    def __post_init__(self) -> None:
        _identity_text(self.title, 'candidate title')
        if isinstance(self.provider_trust, bool) or not isinstance(self.provider_trust, (int, float)) or not (0.0 <= float(self.provider_trust) <= 1.0):
            raise ValueError('provider trust must be between 0 and 1')
        if not isinstance(self.platform_tags, tuple) or len(self.platform_tags) > 32:
            raise ValueError('candidate platform tags must be a bounded tuple')
        for tag in self.platform_tags:
            _identity_text(tag, 'platform tag')
        if not isinstance(self.has_artifact, bool):
            raise ValueError('candidate has_artifact must be boolean')
        if self.provider_id is not None:
            _identity_text(self.provider_id, 'provider ID')
        if self.release_year is not None and (isinstance(self.release_year, bool) or not isinstance(self.release_year, int) or not (1900 <= self.release_year <= 2099)):
            raise ValueError('candidate release year is invalid')


@dataclass(frozen=True)
class MatchBreakdown:
    confidence: float
    exact: float
    chars: float
    tokens: float
    trigrams: float
    prefix: float
    acronym: float
    numeric_conflict: bool
    request_penalty: float
    platform_bonus: float
    matched_query: str
    matched_candidate: str
    year_conflict: bool = False
    year_uncertain: bool = False
    qualifier_conflict: bool = False


class MatchAction(str, Enum):
    AUTO = 'auto'
    ASK = 'ask'
    REJECT = 'reject'


@dataclass(frozen=True)
class RankedCandidate:
    candidate: Candidate
    match: MatchBreakdown


@dataclass(frozen=True)
class MatchDecision:
    action: MatchAction
    ranked: tuple[RankedCandidate, ...]
    reason: str


def _pair_score(q: str, c: str) -> MatchBreakdown:
    qn = normalize_title(q)
    cn = normalize_title(c)
    qtok = _meaningful_tokens(qn)
    ctok = _meaningful_tokens(cn)
    qnum, cnum = _numeric_tokens(qn), _numeric_tokens(cn)

    numeric_conflict = bool(qnum and cnum and qnum.isdisjoint(cnum))
    exact = 1.0 if qn == cn or compact(qn) == compact(cn) else 0.0
    chars = SequenceMatcher(None, qn, cn).ratio()
    token_score = _dice(qtok, ctok)
    trigram_score = _dice(_trigrams(qn), _trigrams(cn))
    prefix = 1.0 if (qn.startswith(cn) or cn.startswith(qn)) and min(len(compact(qn)), len(compact(cn))) >= 4 else 0.0

    qa, ca = _acronym(qn), _acronym(cn)
    qc, cc = compact(qn), compact(cn)
    acronym = 1.0 if (
        len(qa) >= 3 and len(ca) >= 3 and qa == ca
        or len(qa) >= 3 and qa == cc
        or len(ca) >= 3 and ca == qc
    ) else 0.0

    # Weighted fusion. Exact/acronym/prefix signals deliberately dominate raw
    # SequenceMatcher. Numeric disagreement is a safety condition, not a small
    # fuzzy penalty.
    conf = (
        0.34 * exact
        + 0.18 * chars
        + 0.20 * token_score
        + 0.12 * trigram_score
        + 0.10 * prefix
        + 0.16 * acronym
    )
    # Strong containment is common when a shortcut adds an edition label or a
    # catalog adds a subtitle. Reward it only when at least two meaningful
    # tokens agree and there is no numeric disagreement.
    if not numeric_conflict and len(qtok & ctok) >= 2 and (qtok <= ctok or ctok <= qtok):
        conf = max(conf, 0.90 if prefix else 0.86)
    if not numeric_conflict and acronym == 1.0:
        conf = max(conf, 0.93)
    # Bare base title vs year-qualified catalog entries is intentionally
    # reviewable, not rejected and not auto-selected.
    if not numeric_conflict and prefix == 1.0 and not qnum and cnum:
        conf = max(conf, 0.80)
    if numeric_conflict:
        conf = min(conf, 0.34)
    conf = min(conf, 1.0)

    return MatchBreakdown(
        confidence=conf, exact=exact, chars=chars, tokens=token_score,
        trigrams=trigram_score, prefix=prefix, acronym=acronym,
        numeric_conflict=numeric_conflict, request_penalty=0.0,
        platform_bonus=0.0, matched_query=qn, matched_candidate=cn,
    )


def match_breakdown(query_aliases: list[str], c: Candidate, desired_platform: str | None = None) -> MatchBreakdown:
    if not isinstance(query_aliases, list) or not query_aliases or len(query_aliases) > MAX_QUERY_ALIASES:
        raise ValueError('query aliases must be a non-empty bounded list')
    query_aliases = [_identity_text(value, 'query alias') for value in query_aliases]
    if desired_platform is not None:
        desired_platform = _identity_text(desired_platform, 'desired platform')
    if not c.has_artifact:
        return MatchBreakdown(float('-inf'), 0, 0, 0, 0, 0, 0, False, 0, 0, '', normalize_title(c.title))

    candidate_variants = aliases(c.title)
    for v in aliases(_identity_prefix(c.title)):
        if v not in candidate_variants:
            candidate_variants.append(v)
    if not candidate_variants:
        # A title with no scorable identity left in it, which live provider
        # data does contain: one forum topic is titled exactly `.`. It used to
        # reach the assertion below and raise out of the whole provider job, so
        # one such row cost every result that source would have returned. It is
        # the same answer as a candidate with no artifact: nothing to match.
        return MatchBreakdown(float('-inf'), 0, 0, 0, 0, 0, 0, False, 0, 0, '', normalize_title(c.title))

    query_years = set().union(*(_year_tokens(q) for q in query_aliases)) if query_aliases else set()
    scoring_queries = list(query_aliases)
    # When provider-native release metadata confirms a year in the shortcut
    # name, compare the textual identity without that year as well. This lets
    # `Dead Space (2023)` match `Dead Space Remake` without treating `2023` as
    # title noise for every other candidate.
    if c.release_year is not None and c.release_year in query_years:
        for q in list(query_aliases):
            stripped = _strip_year(q, c.release_year)
            if stripped and stripped not in scoring_queries:
                scoring_queries.append(stripped)

    best: MatchBreakdown | None = None
    for q in scoring_queries:
        for cv in candidate_variants:
            cur = _pair_score(q, cv)
            if best is None or cur.confidence > best.confidence:
                best = cur
    assert best is not None

    # Numeric safety is evaluated against the provider identity prefix, not
    # against the full result label. Otherwise a table version like [3.0]
    # could make "Resident Evil 4 ... [3.0]" look numerically compatible
    # with a query for Resident Evil 3.
    identity_nums = _numeric_tokens(_identity_prefix(c.title))
    query_num_sets = [_numeric_tokens(q) for q in query_aliases if _numeric_tokens(q)]
    identity_numeric_conflict = bool(
        identity_nums and query_num_sets and all(nums.isdisjoint(identity_nums) for nums in query_num_sets)
    )

    candidate_identity = _identity_prefix(c.title)
    candidate_years = _year_tokens(candidate_identity)
    year_conflict = bool(
        query_years and (
            (c.release_year is not None and c.release_year not in query_years)
            or (candidate_years and candidate_years.isdisjoint(query_years))
        )
    )
    year_uncertain = bool(
        query_years and c.release_year is None and not candidate_years
    )
    query_quals = set().union(*(_qualifiers(q) for q in query_aliases)) if query_aliases else set()
    candidate_quals = _qualifiers(candidate_identity)
    qualifier_conflict = bool(query_quals and query_quals != candidate_quals)
    candidate_extra_qualifier = bool(candidate_quals - query_quals)

    request_penalty = 1.0 if REQUEST_RE.search(c.title) else 0.0
    platform_bonus = 0.0
    if desired_platform and desired_platform.casefold() in {x.casefold() for x in c.platform_tags}:
        platform_bonus = 0.03

    adjusted = best.confidence + platform_bonus + min(max(c.provider_trust - 0.5, -0.5), 0.5) * 0.04
    if request_penalty:
        adjusted -= 0.55
    if c.release_year is not None and c.release_year in query_years:
        adjusted += 0.03
    adjusted = max(0.0, min(1.0, adjusted))
    if best.numeric_conflict or identity_numeric_conflict or year_conflict:
        adjusted = min(adjusted, 0.34)
    # A year supplied by the shortcut but absent from provider metadata/title
    # is not safe enough for AUTO. Likewise a strong edition identity mismatch
    # such as Remake vs Classic must require confirmation.
    if year_uncertain:
        adjusted = min(adjusted, 0.84)
    if qualifier_conflict:
        adjusted = min(adjusted, 0.69)
    elif candidate_extra_qualifier and not (c.release_year is not None and c.release_year in query_years):
        adjusted = min(adjusted, 0.84)
    return MatchBreakdown(
        confidence=adjusted,
        exact=best.exact, chars=best.chars, tokens=best.tokens,
        trigrams=best.trigrams, prefix=best.prefix, acronym=best.acronym,
        numeric_conflict=best.numeric_conflict or identity_numeric_conflict,
        request_penalty=request_penalty,
        platform_bonus=platform_bonus,
        matched_query=best.matched_query,
        matched_candidate=best.matched_candidate,
        year_conflict=year_conflict,
        year_uncertain=year_uncertain,
        qualifier_conflict=qualifier_conflict,
    )


def decide_match(
    query_aliases: list[str],
    candidates: list[Candidate],
    desired_platform: str | None = None,
    *,
    auto_threshold: float = 0.88,
    ask_threshold: float = 0.70,
    auto_margin: float = 0.07,
) -> MatchDecision:
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise ValueError('candidates must be a bounded list')
    for value, name in ((auto_threshold, 'auto threshold'), (ask_threshold, 'ask threshold'), (auto_margin, 'auto margin')):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0.0 <= float(value) <= 1.0):
            raise ValueError(f'{name} must be between 0 and 1')
    if ask_threshold > auto_threshold:
        raise ValueError('ask threshold must not exceed auto threshold')
    # Validate aliases even when the candidate list is empty.
    if not isinstance(query_aliases, list) or not query_aliases or len(query_aliases) > MAX_QUERY_ALIASES:
        raise ValueError('query aliases must be a non-empty bounded list')
    for alias in query_aliases:
        _identity_text(alias, 'query alias')
    ranked = sorted(
        (RankedCandidate(c, match_breakdown(query_aliases, c, desired_platform)) for c in candidates if c.has_artifact),
        key=lambda x: x.match.confidence,
        reverse=True,
    )
    if not ranked or ranked[0].match.confidence < ask_threshold:
        return MatchDecision(MatchAction.REJECT, tuple(ranked), 'no candidate reached the review threshold')

    top = ranked[0]
    second_conf = ranked[1].match.confidence if len(ranked) > 1 else 0.0
    margin = top.match.confidence - second_conf
    if (
        top.match.confidence >= auto_threshold
        and margin >= auto_margin
        and not top.match.numeric_conflict
    ):
        return MatchDecision(MatchAction.AUTO, tuple(ranked), f'high-confidence match, margin={margin:.3f}')
    return MatchDecision(MatchAction.ASK, tuple(ranked), f'ambiguous or medium-confidence match, margin={margin:.3f}')


def query_plan(display_name: str, executable: str | None = None, *, max_queries: int = 3) -> list[str]:
    """Small provider query set, ordered by fidelity.

    Providers should not be spammed with dozens of fuzzy variants. Search uses
    a few human-readable aliases, while fuzzy matching is performed locally on
    returned candidates.
    """
    if isinstance(max_queries, bool) or not isinstance(max_queries, int) or not (1 <= max_queries <= 8):
        raise ValueError('max_queries must be an integer between 1 and 8')
    planned = aliases(display_name, executable)[:max_queries]
    if not planned:
        raise ValueError('game identity produced no searchable aliases')
    return planned


def score(query_aliases: list[str], c: Candidate, desired_platform: str | None = None) -> float:
    """Backward-compatible scalar used by the older topic-ranking prototype."""
    m = match_breakdown(query_aliases, c, desired_platform)
    if m.confidence == float('-inf'):
        return float('-inf')
    # Keep old tests/consumers working while preserving a large REQUEST gap.
    return m.confidence * 10.0 - m.request_penalty * 5.0
