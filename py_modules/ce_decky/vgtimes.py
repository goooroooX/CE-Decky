"""VGTimes provider: discovery, metadata and its own download chain.

Of the sources evaluated for this project this has the cleanest discovery
shape. The game is its own path segment and tables have their own listing, so
`/games/<slug>/files/cheats/tables/` is a table-only answer where Playground
needs the category of every entry read one at a time. Item pages publish a
schema.org `SoftwareApplication` block carrying the title, the category that
confirms an entry really is a table, the author, the date and an exact byte
count, and the VirusTotal report linked beside it carries the artifact's
SHA-256, so a digest is known before the user chooses the row.

Acquisition is the site's own chain, and nothing about it is written down here
that the site does not write down itself. The page declares this client's
address, its group, the file server it will use and the bundle its client is
built from; the bundle names the module a files page loads; that module carries
both routes, the fields each one is sent, the countdown and what its own client
does with every refusal code; and the challenge answer states its key
derivation, the header it is keyed by, the image fields it wants read and the
shape of the object it expects back. A path or a parameter frozen in here would
be a snapshot of one day's site, and the day it moved this would send a
confident wrong answer instead of saying it no longer understands the contract.

The chain, measured against the live site on 2026-09-03:

1. `POST` the waiting page with the fields the module names, for a download
   token.
2. Wait out the countdown, which the server enforces rather than draws: the
   same token was refused at t+0 and t+10 seconds and accepted from t+21.
3. `POST` the challenge endpoint on the declared file server, which answers
   with a `packer`-compressed script and a response header.
4. Stage one decrypts an AES-256-CBC blob keyed by PBKDF2-HMAC-SHA256 of that
   header over the blob's own salt; the plaintext is a JPEG. The client reads
   its pixel dimensions and the IPTC records the script names. Stage two is
   keyed by one of those records and yields the final script.
5. That script appends the answers to the page's own `showfile` URL.

The object it builds has one slot a browser fills with an automation probe.
This client is not a browser, computes none of it, and sends that slot empty,
which the site serves; `build_download_values` has no path that fills it and
`download_url` refuses a non-empty one, so this adapter never describes itself
as something it is not.

Nothing is executed. The challenge script is unpacked by substitution over the
dictionary it carries with it, and read with regular expressions.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import pbkdf2_hmac
import json
import re
import time
from urllib.parse import urlsplit
import zlib

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

from .aes_cbc import decrypt_cbc
from .network import ArtifactGone, NetworkClient, NetworkError, ProviderRateLimited, is_transport_ready_url
from .operations import drained_to_thread
from .providers import parse_retry_after

PROVIDER_ID = "vgtimes"
BASE = "https://vgtimes.ru"
SITE_HOSTS = frozenset({"vgtimes.ru", "www.vgtimes.ru"})
# The host observed serving the challenge and, after the download redirect, the
# artifact. The wider list the site declares for its file servers is read from
# the page by `parse_file_hosts` rather than frozen here, but every one of them
# still has to sit under the provider's own domains before it becomes outbound
# authority: a page that names something else is refused rather than followed.
ASSET_HOSTS = frozenset({"files.vgtimes.ru"})
FILE_HOST_SUFFIXES = (".vgtimes.ru", ".vgtimes.com")
FILE_HOST_NAMES = frozenset({"vgtimes.ru", "vgtimes.com"})

MAX_SITEMAP_BYTES = 16 * 1024 * 1024
MAX_SITEMAP_ENTRIES = 200_000
MAX_GAME_SHARDS = 8
MAX_CHALLENGE_BYTES = 1 * 1024 * 1024
MAX_CHALLENGE_IMAGE_BYTES = 256 * 1024
MAX_WAITING_PAGE_BYTES = 512 * 1024
MAX_MODULE_BYTES = 8 * 1024 * 1024
MAX_COUNTDOWN_SECONDS = 600

LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
GAME_SLUG_RE = re.compile(r"^/games/([a-z0-9][a-z0-9-]{0,120})/?$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,120}$")
ITEM_RE = re.compile(r"/games/([a-z0-9-]{1,120})/files/(?:cheats/(?:tables/)?)?(\d{1,12})-([a-z0-9-]{1,180})\.html")
FILE_HOSTS_RE = re.compile(r"files_server_(?:original|backup)_hosts='(\[[^']*\])'")
JSON_LD_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.I | re.S)
VIRUSTOTAL_RE = re.compile(r"https://www\.virustotal\.com/gui/file/([0-9a-f]{64})")
FILE_SIZE_RE = re.compile(r"^\s*(\d{1,15})\s*bytes?\s*$", re.I)
# The exact name the site will serve, published beside the download control.
# Worth reading rather than deriving from the title: the site serves `.rar` as
# well as `.zip`, and a row has to say which before the user chooses it.
FILENAME_RE = re.compile(r'class="filename"[^>]*>\s*([^<\s][^<]{0,250}?)\s*<')
ARCHIVE_PASSWORD_RE = re.compile(r'class="passwd[^"]*"[^>]*>[^:<]{0,60}:\s*([^<\s][^<]{0,60}?)\s*<')
# Anchored on the control itself. Reading `data-id` from the page at large picks
# up the first unrelated element that happens to carry one, which is what the
# first live run did.
DOWNLOAD_ANCHOR_RE = re.compile(r'<a\b[^>]*\bdata-href="/index\.php\?do=files&(?:amp;)?op=showfile[^"]*"[^>]*>', re.I)
DOWNLOAD_ID_RE = re.compile(r'data-id="(\d{1,12})"')
DOWNLOAD_HASH_RE = re.compile(r'data-hash="([0-9a-f]{32})"')
SHOWFILE_RE = re.compile(r'data-href="(/index\.php\?do=files&(?:amp;)?op=showfile[^"]*)"')
SHOWFILE_LID_RE = re.compile(r"[?&]lid=(\d{1,12})")
SHOW_PATH_RE = re.compile(r"^/index\.php\?do=files&op=showfile[A-Za-z0-9_=&%.:/-]{0,512}$")

# The category path the site puts in its own structured data. An entry that does
# not declare it is a trainer, a save or an editor, and those are not tables.
TABLE_CATEGORY = "таблиц"


class ChallengeError(ValueError):
    """The site's client contract is not the shape this adapter can read.

    A type of its own because it is neither a provider outage nor a bad
    artifact: it means the contract moved, which is a thing to report and
    re-derive rather than retry or work around.
    """


@dataclass(frozen=True)
class GameRef:
    slug: str
    url: str

    @property
    def title(self) -> str:
        return " ".join(word.capitalize() for word in self.slug.split("-") if word)


@dataclass(frozen=True)
class ItemRef:
    url: str
    game_slug: str
    file_id: str


@dataclass(frozen=True)
class DownloadIdentity:
    """What the page's own control identifies the file by."""

    file_id: str
    file_hash: str
    show_path: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"\d{1,12}", self.file_id) or not re.fullmatch(r"[0-9a-f]{32}", self.file_hash):
            raise ChallengeError("download identity is invalid")
        if not SHOW_PATH_RE.fullmatch(self.show_path):
            raise ChallengeError("download route is invalid")


@dataclass(frozen=True)
class ItemMetadata:
    title: str
    category: str
    author: str | None
    uploader: str | None
    published: str | None
    size_bytes: int | None
    advertised_sha256: str | None
    description: str
    filename: str | None = None
    archive_password: str | None = None

    @property
    def is_table(self) -> bool:
        return TABLE_CATEGORY in self.category.casefold()

    @property
    def game_title(self) -> str:
        """The game this entry is filed under, in the site's own spelling.

        The category is the page's own breadcrumb with the game first, so this
        needs no derivation from the URL slug and keeps punctuation a slug has
        already lost. Blank when the page declares no category, which is the
        same page `is_table` refuses.
        """
        return self.category.split("/")[0].strip()


def _text(value: object, limit: int = 4096) -> str:
    return value[:limit] if isinstance(value, str) else ""


def parse_file_hosts(html: str) -> tuple[str, ...]:
    """The file-server hosts the page declares for itself.

    The site publishes an original and a backup list and can move between them,
    so the policy is read from the source rather than inferred from one observed
    URL. A malformed list drops itself and never the page, and a host outside
    the provider's own domains is dropped rather than granted authority.
    """
    hosts: list[str] = []
    for raw in FILE_HOSTS_RE.findall(html):
        try:
            declared = json.loads(raw.replace("\\/", "/"))
        except ValueError:
            continue
        for entry in declared if isinstance(declared, list) else []:
            host = (urlsplit(entry).hostname or "").casefold() if isinstance(entry, str) else ""
            if host and is_provider_file_host(host) and host not in hosts:
                hosts.append(host)
    return tuple(hosts)


def is_provider_file_host(host: str) -> bool:
    """Whether a host the page named is one of the provider's own."""
    lowered = host.casefold().rstrip(".")
    return lowered in FILE_HOST_NAMES or lowered.endswith(FILE_HOST_SUFFIXES)


def _locations(document: str, what: str, *, root_name: str) -> list[str]:
    """Every `<loc>` of a sitemap document that parsed as a whole document.

    Read structurally rather than by pattern. A regular expression is happy with
    a prefix, so a document that stopped early still produced entries, counted
    as read, and was cached: an index with a piece missing that reports itself
    complete is the failure this whole crawl is bounded to avoid.
    """
    try:
        root = fromstring(
            document, forbid_dtd=True, forbid_entities=True, forbid_external=True,
        )
    except (DefusedXmlException, ParseError, ValueError) as exc:
        raise ValueError(f"VGTimes {what} is not a complete XML document: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1] != root_name:
        # A document that parsed is not the document that was asked for. Read
        # for its `<loc>` elements alone, anything well formed could contribute
        # identity to this provider's index.
        raise ValueError(f"VGTimes {what} has an unexpected root")
    found: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "loc":
            continue
        text = (element.text or "").strip()
        if text:
            found.append(text)
        if len(found) > MAX_SITEMAP_ENTRIES:
            raise ValueError(f"VGTimes {what} declares too many entries")
    return found


def parse_sitemap_index(payload: bytes) -> tuple[str, ...]:
    """The shards the sitemap index declares, bounded and HTTPS only."""
    if len(payload) > MAX_SITEMAP_BYTES:
        raise ValueError("VGTimes sitemap index exceeds the byte limit")
    found: list[str] = []
    for url in _locations(payload.decode("utf-8", "replace"), "sitemap index", root_name="sitemapindex"):
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() not in SITE_HOSTS:
            continue
        if url not in found:
            found.append(url)
    return tuple(found)


def game_sitemap_urls(index_urls: tuple[str, ...]) -> tuple[str, ...]:
    """The game shards the index declares, refused rather than trimmed.

    Taking the first few of a longer list is the same defect as caching a crawl
    that lost a page: every game in the shards that were dropped disappears, the
    crawl still calls itself complete, and nothing says so. A site that declares
    more shards than this expects is a site this no longer understands.
    """
    found = tuple(url for url in index_urls if "games_sitemap" in url)
    if len(found) > MAX_GAME_SHARDS:
        raise ValueError("VGTimes sitemap declares more game shards than this can read")
    return found


def decompress(payload: bytes) -> str:
    """Sitemap shards ship gzipped; a plain shard is accepted too.

    Expanded a chunk at a time and stopped at the same limit the transfer was
    bounded by. Decompressing the whole thing first and measuring afterwards
    made that bound meaningless: 400 KB on the wire allocated 400 MB before
    anything refused it, on a device where that matters.
    """
    if payload[:2] != b"\x1f\x8b":
        return payload.decode("utf-8", "replace")
    expander = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    out = bytearray()
    for offset in range(0, len(payload), 256 * 1024):
        out += expander.decompress(payload[offset:offset + 256 * 1024], MAX_SITEMAP_BYTES - len(out) + 1)
        if len(out) > MAX_SITEMAP_BYTES:
            raise ValueError("VGTimes sitemap shard exceeds the byte limit")
    while expander.unconsumed_tail:
        out += expander.decompress(expander.unconsumed_tail, MAX_SITEMAP_BYTES - len(out) + 1)
        if len(out) > MAX_SITEMAP_BYTES:
            raise ValueError("VGTimes sitemap shard exceeds the byte limit")
    if not expander.eof:
        # A truncated stream still yields whole `<loc>` elements up to the cut,
        # so the shard parses, the crawl counts it as read, and the partial game
        # index is cached for hours. A shard that did not finish is not a
        # smaller shard, it is an unknown one.
        raise ValueError("VGTimes sitemap shard is an incomplete gzip stream")
    return bytes(out).decode("utf-8", "replace")


def parse_game_sitemap(xml: str) -> tuple[GameRef, ...]:
    """The game slugs one shard declares. `/games/<slug>/files/` is not a game.

    The shard has to parse as a whole document: a partial one is not a shorter
    list of games, it is an unknown one, and it would otherwise be cached as
    the complete index for hours.
    """
    games: list[GameRef] = []
    seen: set[str] = set()
    for url in _locations(xml, "sitemap shard", root_name="urlset"):
        # The origin is what makes a slug this provider's. Matched by path
        # alone, an entry from anywhere contributed a game to the cached index
        # and its origin was then discarded, so the slug was afterwards treated
        # as one of this site's own.
        if not is_transport_ready_url(url, SITE_HOSTS):
            continue
        match = GAME_SLUG_RE.match(urlsplit(url).path)
        if match is None or match.group(1) in seen:
            continue
        seen.add(match.group(1))
        games.append(GameRef(match.group(1), url))
    return tuple(games)


def tables_listing_url(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError(f"game slug is invalid: {slug!r}")
    return f"{BASE}/games/{slug}/files/cheats/tables/"


ENTRY_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,179}$")


def item_url(slug: str, file_id: str, name: str) -> str:
    """The entry URL the listing published, rather than one built around its id.

    The site resolves an entry by its numeric id whatever name follows it, so a
    placeholder worked. It was still the wrong URL to keep: this is what a row
    shows as its source page and what the imported table records as where it
    came from, and provenance that does not match the page it was read from is
    provenance nobody can follow back.
    """
    if not SLUG_RE.fullmatch(slug) or not re.fullmatch(r"\d{1,12}", file_id):
        raise ValueError("item identity is invalid")
    if not ENTRY_NAME_RE.fullmatch(name):
        raise ValueError("item name is invalid")
    return f"{BASE}/games/{slug}/files/{file_id}-{name}.html"


def parse_tables_listing(html: str, slug: str) -> tuple[ItemRef, ...]:
    """Collect the table entries of one game's own table-only listing."""
    found: list[ItemRef] = []
    seen: set[str] = set()
    for game_slug, file_id, name in ITEM_RE.findall(html):
        if game_slug != slug or file_id in seen:
            continue
        try:
            url = item_url(game_slug, file_id, name)
        except ValueError:
            # A link this cannot read costs that entry, never the listing.
            continue
        seen.add(file_id)
        found.append(ItemRef(url, game_slug, file_id))
    return tuple(found)


def _structured_blocks(html: str) -> list[dict[str, object]]:
    blocks: list[dict[str, object]] = []
    for raw in JSON_LD_RE.findall(html):
        try:
            value = json.loads(raw)
        except ValueError:
            continue  # a broken block drops itself, never the page
        for candidate in value if isinstance(value, list) else [value]:
            if isinstance(candidate, dict):
                blocks.append(candidate)
    return blocks


def parse_item(html: str) -> ItemMetadata:
    """Read the page's own structured data, falling back on nothing.

    Prose is not consulted: the site publishes what is needed in a machine
    readable block, and guessing from rendered text is how a size or a version
    becomes wrong.
    """
    application = next(
        (block for block in _structured_blocks(html) if block.get("@type") == "SoftwareApplication"),
        None,
    )
    if application is None:
        raise ValueError("item page carries no SoftwareApplication block")
    title = _text(application.get("name"), 512)
    if not title:
        raise ValueError("item page declares no name")
    size = None
    size_match = FILE_SIZE_RE.match(_text(application.get("fileSize"), 64))
    if size_match:
        size = int(size_match.group(1))
    digest_match = VIRUSTOTAL_RE.search(html)
    name_match = FILENAME_RE.search(html)
    # Published in the open beside the file, and needed before the download: an
    # archive nobody can open is not a table the user can use.
    password_match = ARCHIVE_PASSWORD_RE.search(html)

    def person(value: object) -> str | None:
        return _text(value.get("name"), 256) or None if isinstance(value, dict) else None

    return ItemMetadata(
        title=title,
        category=_text(application.get("category"), 512),
        author=person(application.get("author")),
        uploader=person(application.get("provider")),
        published=_text(application.get("datePublished"), 32) or None,
        size_bytes=size,
        advertised_sha256=digest_match.group(1) if digest_match else None,
        description=_text(application.get("description"), 4096),
        filename=name_match.group(1) if name_match else None,
        archive_password=password_match.group(1) if password_match else None,
    )


def parse_download_identity(html: str) -> DownloadIdentity | None:
    """Read the control's own attributes, never the page's first matching one."""
    anchor = DOWNLOAD_ANCHOR_RE.search(html)
    if anchor is None:
        return None
    element = anchor.group(0)
    show = SHOWFILE_RE.search(element)
    file_hash = DOWNLOAD_HASH_RE.search(element)
    if not (show and file_hash):
        return None
    show_path = show.group(1).replace("&amp;", "&")
    file_id = DOWNLOAD_ID_RE.search(element)
    lid = SHOWFILE_LID_RE.search(show_path)
    # The route's own lid is the authority and the attribute corroborates it,
    # which means the two disagreeing is markup this no longer understands
    # rather than a preference to resolve. Trusting the lid silently is how a
    # control that has been rebuilt goes on looking readable.
    if lid and file_id and lid.group(1) != file_id.group(1):
        return None
    identity = lid.group(1) if lid else (file_id.group(1) if file_id else None)
    if identity is None:
        return None
    try:
        return DownloadIdentity(identity, file_hash.group(1), show_path)
    except ChallengeError:
        return None


def artifact_id_for(slug: str, file_id: str) -> str:
    return f"game-{slug}:file-{file_id}"


# ---------------------------------------------------------------------------
# Acquisition
#
# Two rules belong to the caller, and both come from the site saying so. A guest
# may have one download open at a time, so a single download must be in flight
# per provider rather than that being discovered from a refusal. And such a
# refusal is this client's own doing rather than a provider failure, so it is
# reported as a wait.
#
# What opens that lock was measured rather than assumed: asking the waiting page
# for a token does not, since a second file was served while an unused token for
# a first one was outstanding. Fetching the artifact does, and the lock outlived
# a completed download by at least 185 seconds, still refusing at 32, 108 and
# 185 seconds, with no upper bound established. So a retry here waits a minute
# at a time, gives up rather than pretending a loop will outlast it, and reports
# the site's own sentence.
# ---------------------------------------------------------------------------

PACKED_RE = re.compile(r"}\('(.*)',(\d+),(\d+),'(.*?)'\.split\('\|'\)", re.S)
BLOB_RE = re.compile(r"'(\{\"ct\":.*?\})'\s*,\s*(.{1,160}?)\)\s*[.;]", re.S)
WORD_RE = re.compile(r"\b\w+\b")
RESPONSE_HEADER_RE = re.compile(r"getResponseHeader\(\s*['\"]([A-Za-z0-9_-]{1,64})['\"]\s*\)")
METADATA_NAME_RE = re.compile(r"allMetaData\.(\w{1,64})")
METADATA_FIELD_RE = re.compile(r"allMetaData\.(\w{1,64})\s*=\s*\w+\[\s*['\"](\d{1,3}:\d{1,3})['\"]\s*\]")
KDF_ITERATIONS_RE = re.compile(r"iterations\s*:\s*(\d{1,7})")
KDF_HASH_RE = re.compile(r"hash\s*:\s*['\"]([A-Za-z0-9-]{1,32})['\"]")
CIPHER_RE = re.compile(r"name\s*:\s*['\"](AES-[A-Z]{3})['\"]\s*,\s*length\s*:\s*(\d{2,4})")
VALUE_KEY_RE = re.compile(r"'(value\w+)'\s*:\s*")
VALUES_BLOCK_RE = re.compile(r"=\s*(\{'value\w+'\s*:.*?\})\s*;", re.S)
PROBE_SLOT_RE = re.compile(r"\w+\.(\w{1,64})\s*=\s*_0x\w+\(\)\s*;")
# The one value in the object this client computes rather than echoes:
# `btoa(<unix seconds>|<seconds on the page>|0)`. It is recognized by the
# operands it is built from, because "an expression that starts with
# `(function`" is true of any function the site might put there next, and
# answering a different question with this client's timing is exactly the
# confident wrong answer this adapter exists to avoid.
#
# The middle operand is captured rather than merely matched, because it is the
# only one whose value this client supplies. Requiring `page_load_time` to
# appear somewhere in the expression does not establish that the operand is
# derived from it, so the name is bound to its own assignment below.
TIMING_VALUE_RE = re.compile(
    r"^\(function\(\)\s*\{.*?\bbtoa\(\s*parseInt\(\s*Date\.now\(\)\s*/\s*1000\s*\)\s*"
    r"\+\s*'\|'\s*\+\s*(\w{1,64})\s*\+\s*'\|'\s*\+\s*0\s*\)",
    re.S,
)


def timing_elapsed_is_seconds_on_page(script: str, name: str) -> bool:
    """Whether that exact operand is the seconds this client can answer with.

    It has to be the variable the expression itself assigns from the moment the
    page loaded, `(Date.now() - page_load_time) / 1000`, because that is the
    only quantity CE Decky knows and the only one it reports. An expression that
    mentions `page_load_time` while passing something else in that position is a
    different question, and answering it with elapsed seconds would be the
    confident wrong answer rather than a refusal.
    """
    binding = re.compile(
        r"\b(?:var|let|const)\s+" + re.escape(name) + r"\s*=[^;]{0,400}?"
        r"parseInt\(\s*\(\s*Date\.now\(\)\s*-\s*[\w.]{0,64}\bpage_load_time\b\s*\)"
        r"\s*/\s*1000\s*\)",
        re.S,
    )
    return bool(binding.search(script))
DEBUG_BLOCK_RE = re.compile(r"\w+\.(\w{1,64})\s*=\s*\{([^{}]*\bcdt\s*:[^{}]*)\}\s*;", re.S)
USER_IP_RE = re.compile(r"user_ip\s*=\s*'((?:\d{1,3}\.){3}\d{1,3}|[0-9a-f:]{2,45})'")
DLE_GROUP_RE = re.compile(r"dle_group\s*=\s*'(\d{1,4})'")
FILES_SERVER_RE = re.compile(r"files_server_host\s*=\s*'(https://[A-Za-z0-9.-]{1,255})'")
BASE_JS_RE = re.compile(r"base_js\s*=\s*'(/[^']{1,2048})'")
JS_NUM_RE = re.compile(r"js_num\s*=\s*'?(\d{1,12})'?")
# The bundle loads a different module per page type, so the files module is the
# one inside the branch that selects a files page. Taking the first template in
# the file instead picks up whichever page type happens to be written first,
# which on 2026-09-03 was a module with no download chain in it at all.
MODULE_BRANCH_RE = re.compile(r"""module\s*==\s*['"]files['"]""")
MODULE_TEMPLATE_RE = re.compile(r"loadScript\(\s*'(/[A-Za-z0-9_./-]{1,200})'\s*\+\s*js_num\s*\+\s*'(\.js)'")
WAITING_PAGE_RE = re.compile(r"\$\.post\(\s*\"(/[A-Za-z0-9_./-]{0,200}waiting_page\.php)\"\s*,\s*\{([^{}]{0,400})\}")
CHALLENGE_PATH_RE = re.compile(
    r"url\s*:\s*files_server_host\s*\+\s*\"(/[A-Za-z0-9_./-]{1,200}\.php)\""
    r"[\s\S]{0,200}?data\s*:\s*\{([^{}]{0,400})\}"
)
COUNTDOWN_RE = re.compile(r"timeLeft\s*=\s*(\d{1,4})")
REQUEST_FIELD_RE = re.compile(r"(\w{1,64})\s*:\s*(\w{1,64})\s*(?:,|$)")
# The module's own refusal branches: which language string it shows for a code,
# which codes make it ask for a fresh token, and which one puts it in a queue.
REFUSAL_MESSAGE_RE = re.compile(r"error_code\s*==\s*'(\d{1,3})'\)\s*(?:var\s+error_text\s*=|\{\s*DLEalert\(\s*)lmm\((\d{1,6})\)")
REFUSAL_TOKEN_RE = re.compile(r"((?:error_code\s*==\s*'\d{1,3}'\s*\|\|?\s*(?:data\.)?)+error_code\s*==\s*'\d{1,3}')\)\s*\{[^}]{0,80}data-download-token\"\s*,\s*\"\"")
REFUSAL_QUEUE_RE = re.compile(r"error_code\s*==\s*'(\d{1,3})'\)\s*\{[\s\S]{0,240}?files_queue\.php")
CODE_RE = re.compile(r"'(\d{1,3})'")
LANGUAGE_TABLE_RE = re.compile(r"lmm_js\s*=\s*\{(.*?)\};", re.S)
LANGUAGE_STRING_RE = re.compile(r"(\d{1,6})\s*:\s*'((?:[^'\\]|\\.){0,400})'")
GUEST_GROUP = "5"

# The site's own client shows a language string per refusal code and behaves
# differently for some of them. Both are read from the site: the module maps a
# code to a string id, the page carries the string table, and the module's
# branches say which codes it retries with a fresh token and which one puts the
# client in a queue. Only the meaning of code 2 is named here, because it is a
# rule about how the plugin must behave rather than a message to show: the
# string the site serves for it says a guest may download only one file at a
# time. A regression asserts the site still says that, so if the meaning moves
# this stops being a guess and starts being a failure.
CHALLENGE_BUSY_CODE = "2"
# Code 1 is named here for the same reason and on the same terms: its string is
# the site saying it does not have the file, which is a rule about how the
# plugin must behave rather than a message to show. Observed on 2026-09-04 for
# one entry whose page, listing and download control were all intact and whose
# `.rar` was refused after the full countdown, while a `.zip` and a second
# `.rar` resolved normally in the same minute. A regression asserts the site
# still says it, so a renumbering fails rather than passing silently.
CHALLENGE_MISSING_CODE = "1"
# Measured, not assumed, and deliberately not tuned. See the note above.
BUSY_RETRY_SECONDS = 60
MAX_REFUSAL_ATTEMPTS = 3
# The longest this will hold a download somebody is watching for one throttled
# challenge, however long the provider asks for.
MAX_CHALLENGE_WAIT_SECONDS = 120


@dataclass(frozen=True)
class RefusalContract:
    """What the site's own client does with each refusal code.

    Read from the module that handles them rather than transcribed here, so a
    site that renumbers its codes moves this with it. An absent branch is
    absent, not assumed: a code with no declared behavior is simply a refusal.
    """

    messages: dict[str, int]
    token_codes: frozenset[str]
    queue_codes: frozenset[str]


class DownloadRefused(ValueError):
    """The site refused this download for now, and said so in its own terms.

    Separate from `ChallengeError` because nothing is broken: the file exists,
    the contract is understood, and the answer is no for a reason the site knows
    and states.
    """

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        contract: RefusalContract | None = None,
    ) -> None:
        detail = f": {message}" if message else ""
        super().__init__(f"the source refused this download (error_code {code}){detail}")
        self.code = code
        self.message = message
        self._contract = contract

    @property
    def needs_new_token(self) -> bool:
        """The site's client clears the token and returns to the waiting page."""
        return bool(self._contract and self.code in self._contract.token_codes)

    @property
    def queued(self) -> bool:
        """The site's client joins a download queue instead of failing."""
        return bool(self._contract and self.code in self._contract.queue_codes)

    @property
    def busy(self) -> bool:
        """One download at a time, and this client already has one open."""
        return self.code == CHALLENGE_BUSY_CODE

    @property
    def missing(self) -> bool:
        """The site says the file behind this entry is not there."""
        return self.code == CHALLENGE_MISSING_CODE


@dataclass(frozen=True)
class PageBootstrap:
    """What an item page declares about the client and the servers it will use."""

    user_ip: str
    files_server: str
    group: str
    base_js_url: str
    js_num: str
    file_hosts: tuple[str, ...]

    @property
    def is_guest(self) -> bool:
        return self.group == GUEST_GROUP

    @property
    def artifact_hosts(self) -> frozenset[str]:
        """Exact hosts the artifact may be served from, never a wildcard.

        The showfile route is on the site itself and redirects to a file server,
        so both belong here, and the file servers are the ones the page declared
        after the provider-domain check `parse_file_hosts` already applied.
        """
        return SITE_HOSTS | ASSET_HOSTS | frozenset(self.file_hosts)


@dataclass(frozen=True)
class ClientContract:
    """The routes, the fields they are sent and the wait, from the module."""

    waiting_page_url: str
    waiting_page_fields: dict[str, str]
    challenge_url: str
    challenge_fields: dict[str, str]
    countdown_seconds: int


@dataclass(frozen=True)
class KeyDerivation:
    """How the challenge says its own blobs are keyed."""

    cipher: str
    hash_name: str
    iterations: int
    key_bits: int

    @property
    def key_bytes(self) -> int:
        return self.key_bits // 8


@dataclass(frozen=True)
class ChallengeStage:
    """One encrypted blob and the expression the script keys it with."""

    blob: dict[str, str]
    key_expression: str

    @property
    def header_name(self) -> str | None:
        found = RESPONSE_HEADER_RE.search(self.key_expression)
        return found.group(1) if found else None

    @property
    def metadata_name(self) -> str | None:
        found = METADATA_NAME_RE.search(self.key_expression)
        return found.group(1) if found else None


@dataclass(frozen=True)
class ChallengeImage:
    """The image the challenge asks the client to look at."""

    width: int
    height: int
    fields: dict[str, str]
    names: dict[str, str]

    def named(self, name: str) -> str:
        field = self.names.get(name)
        if field is None or field not in self.fields:
            raise ChallengeError(f"challenge image carries no answer for {name!r}")
        return self.fields[field]


def parse_page_bootstrap(html: str) -> PageBootstrap:
    """Read what the page says about this client and about its own servers."""
    address = USER_IP_RE.search(html)
    server = FILES_SERVER_RE.search(html)
    group = DLE_GROUP_RE.search(html)
    base_js = BASE_JS_RE.search(html)
    js_num = JS_NUM_RE.search(html)
    if not (address and server and group and base_js and js_num):
        raise ChallengeError("page does not declare the client bootstrap this chain needs")
    host = (urlsplit(server.group(1)).hostname or "").casefold()
    if host not in ASSET_HOSTS and not is_provider_file_host(host):
        raise ChallengeError(f"page names a file server outside the declared policy: {host}")
    return PageBootstrap(
        user_ip=address.group(1),
        files_server=server.group(1).rstrip("/"),
        group=group.group(1),
        base_js_url=base_js.group(1),
        js_num=js_num.group(1),
        file_hosts=parse_file_hosts(html),
    )


def parse_user_ip(html: str) -> str:
    """The address the site believes the client has, which it asks to be echoed."""
    return parse_page_bootstrap(html).user_ip


def client_module_url(base_js: str, bootstrap: PageBootstrap) -> str:
    """The files module URL, built the way the site's own bundle builds it."""
    branch = MODULE_BRANCH_RE.search(base_js)
    if branch is None:
        raise ChallengeError("client bundle does not select a files page")
    template = MODULE_TEMPLATE_RE.search(base_js, branch.end())
    if template is None:
        raise ChallengeError("client bundle does not name the files module")
    return f"{BASE}{template.group(1)}{bootstrap.js_num}{template.group(2)}"


def base_js_url(bootstrap: PageBootstrap) -> str:
    if not bootstrap.base_js_url.startswith("/"):
        raise ChallengeError("client bundle route is invalid")
    return f"{BASE}{bootstrap.base_js_url}"


def _request_fields(body: str) -> dict[str, str]:
    return dict(REQUEST_FIELD_RE.findall(body))


def parse_client_contract(module_js: str, bootstrap: PageBootstrap) -> ClientContract:
    """Read the two routes, their fields and the wait, out of the module.

    The field names are parsed beside the routes rather than written down here
    for the same reason as everything else in this chain: a site that renames a
    parameter should make this say it no longer understands the contract, not
    make it send a confidently wrong request.
    """
    waiting = WAITING_PAGE_RE.search(module_js)
    challenge = CHALLENGE_PATH_RE.search(module_js)
    countdown = COUNTDOWN_RE.search(module_js)
    if not (waiting and challenge and countdown):
        raise ChallengeError("files module does not declare the download chain")
    waiting_fields = _request_fields(waiting.group(2))
    challenge_fields = _request_fields(challenge.group(2))
    if not waiting_fields or not challenge_fields:
        raise ChallengeError("files module declares a route with no request fields")
    seconds = int(countdown.group(1))
    if not 0 < seconds <= MAX_COUNTDOWN_SECONDS:
        raise ChallengeError(f"declared countdown is out of bounds: {seconds}")
    return ClientContract(
        waiting_page_url=f"{BASE}{waiting.group(1)}",
        waiting_page_fields=waiting_fields,
        challenge_url=f"{bootstrap.files_server}{challenge.group(1)}",
        challenge_fields=challenge_fields,
        countdown_seconds=seconds,
    )


def build_request(
    fields: dict[str, str],
    identity: DownloadIdentity,
    bootstrap: PageBootstrap,
    *,
    token: str = "",
) -> dict[str, str]:
    """Fill the module's own request object with what this client actually has.

    Each value is the JavaScript variable the module names, and only the ones
    this client can honestly supply are known. A field asking for anything else
    is the contract having moved, and is reported rather than guessed at: the
    queue flow in particular is one this adapter does not implement, and its
    hash is empty here exactly as it is in the site's own client when no queue
    is involved.
    """
    known = {
        "file_hash": identity.file_hash,
        "file_id": identity.file_id,
        "download_token": token,
        "user_ip": bootstrap.user_ip,
        "queue_hash": "",
    }
    built: dict[str, str] = {}
    for name, variable in fields.items():
        if variable not in known:
            raise ChallengeError(f"the files module asks for a value this client does not have: {variable}")
        built[name] = known[variable]
    return built


def parse_waiting_page(payload: object) -> str:
    """The token the waiting page issues, which the countdown then makes valid."""
    if not isinstance(payload, dict):
        raise ChallengeError("waiting page answer is not an object")
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        raise ChallengeError(f"waiting page refused: {error.strip()[:200]}")
    token = payload.get("download_token")
    if not isinstance(token, str) or not token or len(token) > 512:
        raise ChallengeError("waiting page issued no download token")
    return token


def parse_refusal_contract(module_js: str) -> RefusalContract:
    """What the site's client does with each refusal code, read from its code."""
    messages = {code: int(string_id) for code, string_id in REFUSAL_MESSAGE_RE.findall(module_js)}
    token = REFUSAL_TOKEN_RE.search(module_js)
    queue = REFUSAL_QUEUE_RE.findall(module_js)
    return RefusalContract(
        messages=messages,
        token_codes=frozenset(CODE_RE.findall(token.group(1))) if token else frozenset(),
        queue_codes=frozenset(queue),
    )


def retry_delay_for(refusal: DownloadRefused, attempt: int, *, max_attempts: int = MAX_REFUSAL_ATTEMPTS) -> int | None:
    """How long to wait before asking again, or `None` when asking again is wrong.

    The three answers come from what the refusal is. A busy refusal is this
    client's own previous download still counted as open, so it clears on its
    own and waiting is the only fix. A token refusal is the site asking for the
    waiting page again, which costs a token and the countdown but no delay of
    its own. A queue is a flow this adapter does not implement, and a file the
    site says it does not have will not appear by being asked twice.
    """
    if attempt >= max_attempts:
        return None
    if refusal.busy:
        return BUSY_RETRY_SECONDS
    if refusal.needs_new_token:
        return 0
    return None


def challenge_retry_delay(
    limit: ProviderRateLimited,
    attempt: int,
    *,
    max_attempts: int = MAX_REFUSAL_ATTEMPTS,
) -> int | None:
    """How long to wait before asking the challenge again with the same token.

    A rate limit that lands once the token has been issued and its countdown
    served cannot be answered by starting over, so it is waited out where the
    token still is. The provider's own number is honoured when it names one and
    bounded either way, because somebody is watching this download.
    """
    if attempt >= max_attempts:
        return None
    named = parse_retry_after(limit.retry_after)
    if named is not None:
        return min(max(1, named), MAX_CHALLENGE_WAIT_SECONDS)
    return min(BUSY_RETRY_SECONDS, MAX_CHALLENGE_WAIT_SECONDS)


def retry_needs_new_token(refusal: DownloadRefused) -> bool:
    """Whether asking again means asking the waiting page again.

    It usually does not. A token stays valid inside its window: the same one was
    accepted by three challenge calls in a row on 2026-09-03, at 21, 31 and 45
    seconds after it was issued. So a busy refusal is retried by asking the
    challenge again with the token already in hand, which costs one request and
    spends no second countdown. A client that answers "you already have one
    open" by opening another is arguing with itself.
    """
    return refusal.needs_new_token


def parse_language_strings(html: str) -> dict[int, str]:
    """The page's own string table, which is where its messages come from."""
    table = LANGUAGE_TABLE_RE.search(html)
    if table is None:
        return {}
    return {
        int(string_id): text.replace("\\'", "'")
        for string_id, text in LANGUAGE_STRING_RE.findall(table.group(1))
    }


def parse_challenge_answer(
    payload: object,
    *,
    contract: RefusalContract | None = None,
    strings: dict[int, str] | None = None,
) -> str:
    """The challenge script, or the site's own refusal to serve this download.

    Reading the code first is what keeps a refusal from being reported as a
    contract this adapter no longer understands: the answer is well formed and
    the result is simply empty.
    """
    if not isinstance(payload, dict):
        raise ChallengeError("challenge answer is not an object")
    code = payload.get("error_code")
    if isinstance(code, str) and code.strip():
        code = code.strip()[:8]
        string_id = (contract.messages if contract else {}).get(code)
        message = (strings or {}).get(string_id) if string_id is not None else None
        raise DownloadRefused(code, message=message, contract=contract)
    result = payload.get("result")
    if not isinstance(result, str) or not result:
        raise ChallengeError("challenge answer carries no script")
    return result


def unpack_packed_script(text: str) -> str:
    """Undo the `packer` compression the challenge script ships in.

    A substitution over a dictionary the payload carries with it, not an
    interpreter: nothing from the answer is executed, here or anywhere else in
    this adapter.
    """
    if len(text) > MAX_CHALLENGE_BYTES:
        raise ChallengeError("challenge script exceeds size limit")
    match = PACKED_RE.search(text)
    if match is None:
        raise ChallengeError("challenge script is not in the expected packed form")
    body, radix, count = match.group(1), int(match.group(2)), int(match.group(3))
    words = match.group(4).split("|")
    if radix < 2 or count > 20_000:
        raise ChallengeError("challenge dictionary is out of bounds")
    try:
        body = body.encode().decode("unicode_escape")
    except UnicodeDecodeError as exc:
        raise ChallengeError(f"challenge body is not decodable: {exc}") from exc
    table = {}
    for index in range(count - 1, -1, -1):
        name = _base_n(index, radix)
        table[name] = words[index] if index < len(words) and words[index] else name
    return WORD_RE.sub(lambda found: table.get(found.group(0), found.group(0)), body)


def _base_n(value: int, radix: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = "" if value < radix else _base_n(value // radix, radix)
    value %= radix
    return out + (chr(value + 29) if value > 35 else digits[value])


def parse_key_derivation(script: str) -> KeyDerivation:
    """The KDF and cipher the challenge script states for its own blobs."""
    iterations = KDF_ITERATIONS_RE.search(script)
    hash_name = KDF_HASH_RE.search(script)
    cipher = CIPHER_RE.search(script)
    if not (iterations and hash_name and cipher):
        raise ChallengeError("challenge script does not state its key derivation")
    rounds, bits = int(iterations.group(1)), int(cipher.group(2))
    if not 0 < rounds <= 1_000_000 or bits not in (128, 192, 256):
        raise ChallengeError(f"challenge key derivation is out of bounds: {rounds} rounds, {bits} bits")
    if cipher.group(1) != "AES-CBC":
        raise ChallengeError(f"challenge uses a cipher this cannot read: {cipher.group(1)}")
    return KeyDerivation(cipher.group(1), hash_name.group(1), rounds, bits)


def challenge_stages(script: str) -> tuple[ChallengeStage, ChallengeStage]:
    """The two blobs, each with the expression the script keys it with."""
    found: list[ChallengeStage] = []
    for raw, key_expression in BLOB_RE.findall(script)[:2]:
        try:
            blob = json.loads(raw.replace("\\/", "/"))
        except ValueError as exc:
            raise ChallengeError(f"challenge blob is not readable: {exc}") from exc
        if not all(isinstance(blob.get(key), str) for key in ("ct", "iv", "s")):
            raise ChallengeError("challenge blob is missing its ciphertext, IV or salt")
        found.append(ChallengeStage(blob, key_expression.strip()))
    if len(found) != 2:
        raise ChallengeError("challenge script does not carry two blobs")
    return found[0], found[1]


def decrypt_challenge_blob(blob: dict[str, str], password: str, derivation: KeyDerivation) -> bytes:
    """Open one blob with the key material the server supplied for it."""
    try:
        ciphertext = b64decode(blob["ct"], validate=True)
        iv = bytes.fromhex(blob["iv"])
        salt = bytes.fromhex(blob["s"])
    except (ValueError, BinasciiError) as exc:
        raise ChallengeError(f"challenge blob is malformed: {exc}") from exc
    if len(ciphertext) > MAX_CHALLENGE_BYTES:
        raise ChallengeError("challenge blob exceeds size limit")
    try:
        key = pbkdf2_hmac(
            derivation.hash_name.replace("-", "").lower(),
            password.encode("utf-8"), salt, derivation.iterations, derivation.key_bytes,
        )
        return decrypt_cbc(key, iv, ciphertext)
    except ValueError as exc:
        raise ChallengeError(f"challenge blob did not decrypt: {exc}") from exc


def parse_image_fields(script: str) -> dict[str, str]:
    """Which IPTC field the script reads for each name it then uses."""
    fields = dict(METADATA_FIELD_RE.findall(script))
    if not fields:
        raise ChallengeError("challenge script names no image fields")
    return fields


def parse_challenge_image(jpeg: bytes, names: dict[str, str]) -> ChallengeImage:
    """Read the dimensions and the IPTC records the script asked for."""
    if len(jpeg) > MAX_CHALLENGE_IMAGE_BYTES or jpeg[:2] != b"\xff\xd8":
        raise ChallengeError("challenge image is not a bounded JPEG")
    offset, size, records = 2, None, {}
    while offset < len(jpeg) - 3 and jpeg[offset] == 0xFF:
        marker = jpeg[offset + 1]
        offset += 2
        if marker in (0xDA, 0xD9):
            break
        length = (jpeg[offset] << 8) + jpeg[offset + 1]
        segment = jpeg[offset + 2:offset + length]
        if marker in (0xC0, 0xC1, 0xC2) and len(segment) >= 5:
            size = ((segment[3] << 8) | segment[4], (segment[1] << 8) | segment[2])
        elif marker == 0xED and segment.startswith(b"Photoshop 3.0\x00"):
            records = _iptc_records(_photoshop_resource(segment[14:], 0x0404))
        offset += length
    if size is None:
        raise ChallengeError("challenge image declares no dimensions")
    missing = [field for field in names.values() if field not in records]
    if missing:
        raise ChallengeError(f"challenge image carries no {', '.join(sorted(missing))}")
    return ChallengeImage(size[0], size[1], {key: value[0] for key, value in records.items()}, dict(names))


def _photoshop_resource(data: bytes, wanted: int) -> bytes:
    offset = 0
    while offset < len(data) - 11:
        if data[offset:offset + 4] != b"8BIM":
            offset += 1
            continue
        offset += 4
        resource = (data[offset] << 8) | data[offset + 1]
        offset += 2
        name_length = data[offset]
        offset += 1 + name_length + ((1 + name_length) % 2)
        length = int.from_bytes(data[offset:offset + 4], "big")
        offset += 4
        payload = data[offset:offset + length]
        offset += length + (length % 2)
        if resource == wanted:
            return payload
    return b""


def _iptc_records(data: bytes) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    offset = 0
    while offset < len(data) - 4:
        if data[offset] != 0x1C:
            offset += 1
            continue
        record, dataset = data[offset + 1], data[offset + 2]
        offset += 3
        if data[offset] & 0x80:
            count = data[offset] & 0x7F
            offset += 1
            length = int.from_bytes(data[offset:offset + count], "big")
            offset += count
        else:
            length = (data[offset] << 8) | data[offset + 1]
            offset += 2
        out.setdefault(f"{record}:{dataset}", []).append(
            data[offset:offset + length].decode("utf-8", "replace")
        )
        offset += length
    return out


def solve_challenge(result: str, headers: dict[str, str]) -> tuple[ChallengeImage, str]:
    """Open both stages and return the image answers and the final script.

    The first stage is keyed by a response header the script names itself, and
    the second by one of the image fields it just read, so neither key is
    written down here.
    """
    script = unpack_packed_script(result)
    derivation = parse_key_derivation(script)
    names = parse_image_fields(script)
    first, second = challenge_stages(script)
    header = first.header_name
    if header is None:
        raise ChallengeError("challenge does not name the header it is keyed by")
    lowered = {name.casefold(): value for name, value in headers.items()}
    key = lowered.get(header.casefold())
    if not key:
        raise ChallengeError(f"challenge answer carries no {header} header")
    try:
        jpeg = b64decode(decrypt_challenge_blob(first.blob, key, derivation), validate=True)
    except (ValueError, BinasciiError) as exc:
        raise ChallengeError(f"challenge image did not decrypt: {exc}") from exc
    image = parse_challenge_image(jpeg, names)
    metadata_name = second.metadata_name
    if metadata_name is None:
        raise ChallengeError("challenge does not name the image field its second stage is keyed by")
    final = decrypt_challenge_blob(second.blob, image.named(metadata_name), derivation)
    return image, final.decode("utf-8", "replace")


def build_download_values(
    final_script: str,
    image: ChallengeImage,
    *,
    elapsed_seconds: int,
    now_epoch: int,
    now_iso: str,
) -> dict[str, object]:
    """Fill in the object the final script builds, with only what is true.

    The constants are echoed back as the server sent them, the image values are
    what was read from the JPEG, and the timestamp is this client's own. The
    slot the script fills from its automation probe is sent empty, because this
    client computed no fingerprint; it is the one field with no source, and it
    deliberately has no way to acquire one.
    """
    block = VALUES_BLOCK_RE.search(final_script)
    if block is None:
        raise ChallengeError("final script declares no value object")
    body = block.group(1)
    keys = list(VALUE_KEY_RE.finditer(body))
    if not keys:
        raise ChallengeError("final script value object is empty")
    values: dict[str, object] = {}
    for index, key in enumerate(keys):
        end = keys[index + 1].start() if index + 1 < len(keys) else len(body)
        raw = body[key.end():end].rstrip().rstrip("}").rstrip().rstrip(",").strip()
        values[key.group(1)] = _value_for(key.group(1), raw, image, elapsed_seconds, now_epoch)
    probe = PROBE_SLOT_RE.search(final_script)
    if probe:
        values[probe.group(1)] = {}
    debug = DEBUG_BLOCK_RE.search(final_script)
    if debug:
        values[debug.group(1)] = _debug_values(debug.group(2), image, now_iso)
    return values


def probe_slot(final_script: str) -> str | None:
    """The field the site's own script fills with its automation probe."""
    found = PROBE_SLOT_RE.search(final_script)
    return found.group(1) if found else None


def _value_for(name: str, raw: str, image: ChallengeImage, elapsed_seconds: int, now_epoch: int) -> object:
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if raw == "width":
        return image.width
    if raw == "height":
        return image.height
    metadata = METADATA_NAME_RE.fullmatch(raw)
    if metadata:
        return image.named(metadata.group(1))
    timing = TIMING_VALUE_RE.match(raw)
    if timing and timing_elapsed_is_seconds_on_page(raw, timing.group(1)):
        # `btoa(now|seconds spent on the page|0)`, which is this client's own
        # timing and is reported as it happened.
        return b64encode(f"{now_epoch}|{max(0, elapsed_seconds)}|0".encode()).decode()
    raise ChallengeError(f"final script asks for an unknown value: {name}")


def _debug_values(block: str, image: ChallengeImage, now_iso: str) -> dict[str, object]:
    """The script's own debug block, minus the fields only a screen can answer."""
    out: dict[str, object] = {"cdt": now_iso}
    for field, raw in re.findall(r"\b(\w{1,32})\s*:\s*([^,{}]+)", block):
        value = raw.strip()
        if value == "width":
            out[field] = image.width
        elif value == "height":
            out[field] = image.height
        elif METADATA_NAME_RE.fullmatch(value):
            out[field] = image.named(METADATA_NAME_RE.fullmatch(value).group(1))
        elif value.isdigit():
            out[field] = int(value)
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            out[field] = value[1:-1]
        # Anything else is something only a browser can answer, such as the
        # screen it is drawn on, and this client does not have one to report.
    return out


def download_url(
    identity: DownloadIdentity,
    bootstrap: PageBootstrap,
    values: dict[str, object],
    *,
    probe_field: str | None,
) -> str:
    """The URL the final script navigates to, built from the page's own control."""
    if probe_field is not None and values.get(probe_field) != {}:
        raise ChallengeError("this adapter does not describe itself as a browser")
    if not USER_IP_RE.fullmatch(f"user_ip='{bootstrap.user_ip}'"):
        raise ChallengeError(f"client address is invalid: {bootstrap.user_ip!r}")
    payload = b64encode(json.dumps(values, separators=(",", ":")).encode("utf-8")).decode()
    return f"{BASE}{identity.show_path}&ip={bootstrap.user_ip}&challenge_values={payload}"


@dataclass(frozen=True)
class VGTimesDownloadRequest:
    """The provider's own way of turning a stored identity into a live link.

    Nothing captured at search time can be downloaded: the link does not exist
    until the site is asked for it, the asking costs a wait the server enforces,
    and the answer expires. So the row stores the page it came from and this
    runs the whole chain when the user starts the acquisition, never before.
    """

    page_url: str
    file_id: str

    def __post_init__(self) -> None:
        parts = urlsplit(self.page_url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() not in SITE_HOSTS:
            raise ValueError("VGTimes page URL is invalid")
        if not re.fullmatch(r"\d{1,12}", self.file_id):
            raise ValueError("VGTimes file identity is invalid")

    @property
    def provider(self) -> str:
        return PROVIDER_ID

    @property
    def artifact_hosts(self) -> frozenset[str]:
        """The static half of the policy; the page adds its own file servers."""
        return SITE_HOSTS | ASSET_HOSTS

    @property
    def send_source_referer(self) -> bool:
        # The showfile route is the site's own control being followed, so the
        # page it is on is exactly the right Referer for it.
        return True

    @property
    def exclusive(self) -> bool:
        """One download of this provider at a time, because the site says so.

        A guest may have one download open at a time; the site refuses a second
        with its own error code and its own sentence. Holding one in flight per
        provider is therefore a rule to follow rather than a refusal to discover
        the hard way, and it is measured: the lock follows the artifact fetch
        rather than the preparation, and outlived a completed download by at
        least 185 seconds.
        """
        return True

    async def resolve(
        self,
        network: NetworkClient,
        record: object,
        *,
        sleep: Callable[[float], Awaitable[object]],
        on_countdown: Callable[..., None] | None = None,
    ) -> tuple[str, frozenset[str]]:
        result = getattr(record, "result", None)
        if (
            getattr(result, "provider", None) != PROVIDER_ID
            or getattr(record, "acquisition", None) is not self
        ):
            raise ValueError("artifact has no VGTimes download request")
        html = await self._read(network, self.page_url, SITE_HOSTS, MAX_WAITING_PAGE_BYTES * 8)
        bootstrap = parse_page_bootstrap(html)
        identity = parse_download_identity(html)
        if identity is None:
            raise ChallengeError("item page carries no download control")
        if identity.file_id != self.file_id:
            raise ChallengeError("item page names a different file than the row did")
        strings = parse_language_strings(html)
        bundle = await self._read(network, base_js_url(bootstrap), SITE_HOSTS, MAX_MODULE_BYTES)
        module = await self._read(network, client_module_url(bundle, bootstrap), SITE_HOSTS, MAX_MODULE_BYTES)
        contract = parse_client_contract(module, bootstrap)
        refusals = parse_refusal_contract(module)
        token: str | None = None
        issued = 0.0
        attempt = 0
        while True:
            if token is None:
                token = await self._token(network, contract, identity, bootstrap)
                issued = time.monotonic()
                # The server enforces this rather than drawing it, so it is
                # waited out and reported as a countdown rather than a stall.
                if on_countdown is not None:
                    on_countdown(contract.countdown_seconds)
                await sleep(contract.countdown_seconds)
            try:
                answer, headers = await self._challenge(network, contract, identity, bootstrap, token)
                script = parse_challenge_answer(answer, contract=refusals, strings=strings)
            except ProviderRateLimited as limit:
                # Waited out here, with the token already in hand. Left to the
                # generic acquisition retry it would re-enter this resolver from
                # the top and ask the waiting page for another token, and that
                # request is the one that tells the site a download is starting,
                # which is exactly what its one-at-a-time rule forbids. The
                # token stays valid inside its window, so asking the challenge
                # again is the whole retry.
                delay = challenge_retry_delay(limit, attempt)
                if delay is None:
                    raise ProviderRateLimited(
                        limit.retry_after, str(limit), restartable=False,
                    ) from limit
                attempt += 1
                if on_countdown is not None:
                    # Named for what it is. Reported as the ordinary countdown
                    # it would have described a provider refusing as this
                    # client's normal preparation, and the throttle would have
                    # been counted nowhere, because the generic retry never
                    # sees a limit this resolver handles itself.
                    on_countdown(delay, "rate_limited")
                await sleep(delay)
                continue
            except DownloadRefused as refusal:
                if refusal.missing:
                    # Not a refusal to work around: the entry is listed and the
                    # file behind it is gone, so this is the one answer that
                    # reads the same however often it is asked.
                    raise ArtifactGone(refusal.message or f"error_code {refusal.code}") from refusal
                delay = retry_delay_for(refusal, attempt)
                if delay is None:
                    raise
                attempt += 1
                if retry_needs_new_token(refusal):
                    token = None
                if delay:
                    if on_countdown is not None:
                        # Not the countdown this site makes every guest sit
                        # through: this is the site saying it is already serving
                        # this client a file and will not start a second. Named
                        # as preparation it read as the countdown restarting
                        # from the beginning for no reason anyone could see.
                        on_countdown(delay, "busy" if refusal.busy else "preparing")
                    await sleep(delay)
                continue
            break
        # Pure-Python PBKDF2 and AES over a few kilobytes: 220 ms measured on
        # this device, which is short but is still the backend's only thread,
        # and the panel is polling it while this runs.
        image, final = await drained_to_thread(solve_challenge, script, headers)
        now = time.time()
        values = build_download_values(
            final,
            image,
            elapsed_seconds=int(max(0.0, time.monotonic() - issued)),
            now_epoch=int(now),
            now_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        )
        url = download_url(identity, bootstrap, values, probe_field=probe_slot(final))
        return url, bootstrap.artifact_hosts

    async def _read(self, network: NetworkClient, url: str, hosts: frozenset[str], max_bytes: int) -> str:
        response = await network.get(url, allowed_hosts=hosts, max_bytes=max_bytes, retries=0)
        if response.status == 429:
            raise ProviderRateLimited(response.headers.get("retry-after"), "VGTimes returned HTTP 429")
        if response.status != 200:
            raise NetworkError(f"VGTimes returned HTTP {response.status} for a page this chain needs")
        return response.body.decode("utf-8", "replace")

    async def _token(
        self,
        network: NetworkClient,
        contract: ClientContract,
        identity: DownloadIdentity,
        bootstrap: PageBootstrap,
    ) -> str:
        response = await network.post(
            contract.waiting_page_url,
            allowed_hosts=SITE_HOSTS,
            max_bytes=MAX_WAITING_PAGE_BYTES,
            data=build_request(contract.waiting_page_fields, identity, bootstrap),
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            referer=self.page_url,
        )
        if response.status == 429:
            raise ProviderRateLimited(response.headers.get("retry-after"), "VGTimes waiting page returned HTTP 429")
        if response.status != 200:
            raise NetworkError(f"VGTimes waiting page returned HTTP {response.status}")
        return parse_waiting_page(_json_body(response.body, "waiting page"))

    async def _challenge(
        self,
        network: NetworkClient,
        contract: ClientContract,
        identity: DownloadIdentity,
        bootstrap: PageBootstrap,
        token: str,
    ) -> tuple[object, dict[str, str]]:
        response = await network.post(
            contract.challenge_url,
            allowed_hosts=SITE_HOSTS | ASSET_HOSTS | frozenset(bootstrap.file_hosts),
            max_bytes=MAX_CHALLENGE_BYTES,
            data=build_request(contract.challenge_fields, identity, bootstrap, token=token),
            headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
            referer=self.page_url,
        )
        if response.status == 429:
            # Nothing is spent by asking again: the token stays valid inside its
            # window, so this is restartable in the ordinary way.
            raise ProviderRateLimited(response.headers.get("retry-after"), "VGTimes challenge returned HTTP 429")
        if response.status != 200:
            raise NetworkError(f"VGTimes challenge returned HTTP {response.status}")
        return _json_body(response.body, "challenge"), dict(response.headers)


def _json_body(body: bytes, what: str) -> object:
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ChallengeError(f"VGTimes {what} answer is not valid JSON: {exc}") from exc
