"""The Cheat Script sitemap discovery and Yandex Disk acquisition contract.

The provider's robots policy disallows search and feed routes, but its declared
sitemap carries game titles in post slugs. Discovery therefore reads only that
sitemap and a bounded set of locally ranked posts. Current posts point at
Yandex Disk public resources; the documented anonymous API supplies the exact
filename, byte count and SHA-256 before the user chooses an artifact, then
supplies a transient download URL only when acquisition starts.

This module adapts the project-owned prototype. No provider or Yandex source
code is copied; their public HTML, sitemap and API responses are protocol
evidence only.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import PurePosixPath
import re
from typing import Awaitable, Callable
from urllib.parse import urlencode, urljoin, urlsplit

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from xml.etree.ElementTree import ParseError

from .network import (
    NetworkClient,
    NetworkError,
    ProviderRateLimited,
)
from .providers import ARTIFACT_SUFFIXES


PROVIDER_ID = "thecheatscript"
MAX_SITEMAP_PAGES = 16
MAX_SITEMAP_ENTRIES = 5_000
MAX_ARTIFACTS_PER_POST = 8
MAX_API_BYTES = 64 * 1024
FIRST_RESOLVABLE_YEAR = 2024
# The one place a downloadable artifact suffix is declared. Retyped here it
# went out of step the moment `.rar` was added to it.
SUPPORTED_SUFFIXES = ARTIFACT_SUFFIXES
YANDEX_PUBLIC_API = "https://cloud-api.yandex.net/v1/disk/public/resources"
SITE_HOSTS = frozenset({"thecheatscript.com", "www.thecheatscript.com"})
YANDEX_API_HOSTS = frozenset({"cloud-api.yandex.net"})
YANDEX_ARTIFACT_HOSTS = frozenset({"downloader.disk.yandex.ru"})
YANDEX_STORAGE_HOST_RE = re.compile(r"^s[a-z0-9]{1,32}\.storage\.yandex\.net$")

YANDEX_RE = re.compile(
    r"(?<![A-Za-z0-9])https://disk\.yandex\.(?:com|ru)/d/"
    r"[A-Za-z0-9_-]{1,64}(?![A-Za-z0-9_-])"
)
MEGA_RE = re.compile(
    r"(?<![A-Za-z0-9])https://mega\.nz/(?:file|folder)/"
    r"[A-Za-z0-9_-]+#[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
)
BUILD_RE = re.compile(r"(?<![A-Za-z0-9])[bp](\d{6,10})(?![0-9])")
GAME_VERSION_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])v\.?\s*(\d+(?:\.\d+){1,3})(?=[\s_|+-]*table\b)"
)
TABLE_VERSION_RE = re.compile(r"(?i)table\s*v\.?\s*(\d+(?:[._]\d+){0,3})")
POST_PATH_RE = re.compile(r"^/\d{4}/\d{2}/[^/]+\.html?$", re.I)
SITEMAP_PAGE_RE = re.compile(r"^\d{1,3}$")


@dataclass(frozen=True)
class PostRef:
    url: str
    title: str
    lastmod: str | None


@dataclass(frozen=True)
class PostArtifact:
    link: str
    supported: bool
    game_build: str | None
    table_version: str | None


@dataclass(frozen=True)
class YandexArtifact:
    filename: str
    size_bytes: int
    advertised_sha256: str | None

    @property
    def version_tokens(self) -> tuple[str | None, str | None]:
        return parse_version_tokens(self.filename)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_root(data: bytes):
    try:
        return fromstring(
            data,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )
    except (DefusedXmlException, ParseError) as exc:
        raise ValueError("The Cheat Script sitemap is not safe XML") from exc


def _site_url(url: str, *, post: bool = False, sitemap_page: bool = False) -> bool:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or (parts.hostname or "").casefold() not in SITE_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.port not in {None, 443}
        or parts.fragment
    ):
        return False
    if post:
        return bool(POST_PATH_RE.fullmatch(parts.path)) and not parts.query
    if sitemap_page:
        query = parts.query.split("=", 1)
        return (
            parts.path == "/sitemap.xml"
            and len(query) == 2
            and query[0] == "page"
            and bool(SITEMAP_PAGE_RE.fullmatch(query[1]))
        )
    return False


def is_post_url(url: str) -> bool:
    """Validate a cached or freshly parsed provider post URL."""
    return _site_url(url, post=True)


def parse_sitemap_index(data: bytes) -> tuple[str, ...]:
    root = _xml_root(data)
    if _local_name(root.tag) != "sitemapindex":
        raise ValueError("The Cheat Script sitemap index has an unexpected root")
    pages: list[str] = []
    for item in root:
        if _local_name(item.tag) != "sitemap":
            continue
        loc = next((child.text for child in item if _local_name(child.tag) == "loc"), None)
        url = loc.strip() if isinstance(loc, str) else ""
        if not _site_url(url, sitemap_page=True) or url in pages:
            continue
        pages.append(url)
        if len(pages) > MAX_SITEMAP_PAGES:
            raise ValueError("The Cheat Script sitemap declares too many pages")
    if not pages:
        raise ValueError("The Cheat Script sitemap declares no usable pages")
    return tuple(pages)


def title_from_url(url: str) -> str:
    slug = urlsplit(url).path.rsplit("/", 1)[-1]
    slug = re.sub(r"\.html?$", "", slug, flags=re.I)
    return " ".join(part.capitalize() for part in slug.split("-") if part)


def parse_sitemap_page(data: bytes) -> tuple[PostRef, ...]:
    root = _xml_root(data)
    if _local_name(root.tag) != "urlset":
        raise ValueError("The Cheat Script sitemap page has an unexpected root")
    refs: list[PostRef] = []
    seen: set[str] = set()
    for item in root:
        if _local_name(item.tag) != "url":
            continue
        fields = {_local_name(child.tag): child.text for child in item}
        loc = fields.get("loc")
        url = loc.strip() if isinstance(loc, str) else ""
        if not _site_url(url, post=True) or url in seen:
            continue
        seen.add(url)
        lastmod = fields.get("lastmod")
        refs.append(PostRef(
            url,
            title_from_url(url),
            lastmod.strip() if isinstance(lastmod, str) and lastmod.strip() else None,
        ))
        if len(refs) > MAX_SITEMAP_ENTRIES:
            raise ValueError("The Cheat Script sitemap page has too many entries")
    return tuple(refs)


def parse_version_tokens(text: str) -> tuple[str | None, str | None]:
    build = BUILD_RE.search(text) or GAME_VERSION_RE.search(text)
    version = TABLE_VERSION_RE.search(text)
    return (
        build.group(1) if build else None,
        version.group(1).replace("_", ".") if version else None,
    )


def parse_post(html: str) -> tuple[PostArtifact, ...]:
    text = " ".join(re.sub(r"<[^>]+>", " ", html).split())
    build, version = parse_version_tokens(text)
    found: list[PostArtifact] = []
    seen: set[str] = set()
    for supported, pattern in ((True, YANDEX_RE), (False, MEGA_RE)):
        for link in pattern.findall(html):
            if link in seen:
                continue
            seen.add(link)
            found.append(PostArtifact(link, supported, build, version))
            if len(found) >= MAX_ARTIFACTS_PER_POST:
                return tuple(found)
    return tuple(found)


def post_year(url: str) -> int | None:
    """The year the site's own post path declares, which every post carries."""
    path = urlsplit(url).path
    return int(path[1:5]) if POST_PATH_RE.fullmatch(path) else None


def is_probably_resolvable(ref: PostRef) -> bool:
    """Whether this post is new enough to still host a resolvable artifact.

    The optimization is worth keeping: posts older than the provider's current
    host era resolve to nothing, and fetching them spends requests to find that
    out. What it must not do is let anything it reads withhold a post on its
    own. There are two years here and they answer separately: the path the site
    assigns, which is structural and always present, and `lastmod`, which is
    optional description. A post is excluded only when everything known about it
    says it is old, so a current post carrying a stale or unreadable date stays
    eligible on its path, and a post whose path is old while its own date says
    it was touched this year stays eligible on that date. Neither identity
    withholds an artifact by disagreeing with the other, and a post with no
    readable year at all is fetched rather than refused for what it did not say.
    """
    declared = (ref.lastmod or "")[:4]
    declared_year = int(declared) if declared.isdigit() else None
    published = post_year(ref.url)
    known = [year for year in (published, declared_year) if year is not None]
    if not known:
        return True
    return max(known) >= FIRST_RESOLVABLE_YEAR


def rank_posts(
    refs: tuple[PostRef, ...],
    scorer: Callable[[str], float],
    *,
    floor: float = 0.55,
    limit: int = 8,
) -> tuple[tuple[PostRef, float], ...]:
    scored = [
        (ref, scorer(ref.title))
        for ref in refs
        if is_probably_resolvable(ref)
    ]
    scored = [pair for pair in scored if pair[1] >= floor]
    scored.sort(key=lambda pair: (-pair[1], pair[0].url))
    return tuple(scored[:limit])


def parse_yandex_public(payload: object) -> YandexArtifact:
    if not isinstance(payload, dict) or payload.get("type") != "file":
        raise ValueError("Yandex public key does not resolve to a single file")
    name = payload.get("name")
    size = payload.get("size")
    if (
        not isinstance(name, str)
        or not name
        or PurePosixPath(name).name != name
        or PurePosixPath(name).suffix.casefold() not in SUPPORTED_SUFFIXES
    ):
        raise ValueError("Yandex public resource has no supported filename")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ValueError("Yandex public resource has no usable size")
    raw_digest = payload.get("sha256")
    digest = raw_digest.casefold() if isinstance(raw_digest, str) else None
    return YandexArtifact(
        filename=name,
        size_bytes=size,
        advertised_sha256=digest if digest and re.fullmatch(r"[0-9a-f]{64}", digest) else None,
    )


def post_slug(post_url: str) -> str:
    path = urlsplit(post_url).path
    return re.sub(r"\.html?$", "", path.strip("/").replace("/", "-"), flags=re.I)


def artifact_id_for(post_url: str, public_key: str) -> str:
    key = sha256(public_key.encode("utf-8")).hexdigest()[:20]
    return f"post-{post_slug(post_url)}:yandex-{key}"


@dataclass(frozen=True)
class TheCheatScriptDownloadRequest:
    public_key: str

    def __post_init__(self) -> None:
        if not YANDEX_RE.fullmatch(self.public_key):
            raise ValueError("Yandex public key is invalid")

    @property
    def provider(self) -> str:
        return PROVIDER_ID

    @property
    def artifact_hosts(self) -> frozenset[str]:
        return YANDEX_ARTIFACT_HOSTS

    @property
    def send_source_referer(self) -> bool:
        # Yandex's downloader currently refuses the third-party post Referer.
        return False

    async def resolve(
        self,
        network: NetworkClient,
        record: object,
        *,
        sleep: Callable[[float], Awaitable[object]],
        on_countdown: Callable[[int], None] | None = None,
    ) -> str | tuple[str, frozenset[str]]:
        del sleep, on_countdown
        result = getattr(record, "result", None)
        if (
            getattr(result, "provider", None) != PROVIDER_ID
            or getattr(record, "acquisition", None) is not self
        ):
            raise ValueError("artifact has no The Cheat Script download request")
        response = await network.get(
            download_href_url(self),
            allowed_hosts=YANDEX_API_HOSTS,
            max_bytes=MAX_API_BYTES,
            headers={"Accept": "application/json"},
            retries=0,
        )
        if response.status == 429:
            raise ProviderRateLimited(
                response.headers.get("retry-after"),
                "Yandex public API returned HTTP 429",
            )
        if response.status != 200:
            raise NetworkError(f"Yandex public API returned HTTP {response.status}")
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NetworkError("Yandex public API returned invalid JSON") from exc
        public_href = parse_download_href(payload)
        redirect = await network.get(
            public_href,
            allowed_hosts=YANDEX_ARTIFACT_HOSTS,
            max_bytes=MAX_API_BYTES,
            retries=0,
            follow_redirects=False,
        )
        if redirect.status == 429:
            raise ProviderRateLimited(
                redirect.headers.get("retry-after"),
                "Yandex public downloader returned HTTP 429",
            )
        if redirect.status not in {301, 302, 303, 307, 308}:
            raise NetworkError(
                f"Yandex public downloader returned HTTP {redirect.status} without a storage redirect"
            )
        url, hosts = parse_storage_redirect(
            public_href,
            redirect.headers.get("location"),
        )
        return url, hosts


def public_metadata_url(request: TheCheatScriptDownloadRequest) -> str:
    return f"{YANDEX_PUBLIC_API}?{urlencode({'public_key': request.public_key})}"


def download_href_url(request: TheCheatScriptDownloadRequest) -> str:
    return f"{YANDEX_PUBLIC_API}/download?{urlencode({'public_key': request.public_key})}"


def parse_download_href(payload: object) -> str:
    href = payload.get("href") if isinstance(payload, dict) else None
    if not isinstance(href, str) or not href:
        raise NetworkError("Yandex public download answer carries no link")
    parts = urlsplit(href)
    if (
        parts.scheme != "https"
        or (parts.hostname or "").casefold() not in YANDEX_ARTIFACT_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.port not in {None, 443}
        or parts.fragment
    ):
        raise NetworkError("Yandex public download link is outside the provider host policy")
    return href


def parse_storage_redirect(
    public_href: str,
    location: str | None,
) -> tuple[str, frozenset[str]]:
    if not isinstance(location, str) or not location:
        raise NetworkError("Yandex public downloader redirect has no location")
    url = urljoin(public_href, location)
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if (
        parts.scheme != "https"
        or not YANDEX_STORAGE_HOST_RE.fullmatch(host)
        or parts.username is not None
        or parts.password is not None
        or parts.port not in {None, 443}
        or parts.fragment
    ):
        raise NetworkError("Yandex storage redirect is outside the provider host policy")
    return url, frozenset({host})
