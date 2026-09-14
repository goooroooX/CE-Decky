"""Rediscover the Cheat Engine installer URL when the pinned one stops working.

The release manifest pins one HTTPS URL, and nothing official republishes it:
the path segment is opaque and changes between releases, Cheat Engine's own
`latestversion.txt` announces a version without a URL, and its updater simply
opens the website. The website's Windows button serves a third-party download
manager rather than the installer, but that stub holds the clean installer's URL
inside its compiled Inno script - which is where this module reads it from.

Every step is bounded and fails closed, and nothing downloaded here is ever
executed:

1. the download page is fetched from the manifest's pinned host;
2. exactly one off-site `.exe` link on that page is accepted as the stub, and
   ambiguity is a refusal rather than a choice;
3. the stub is downloaded into plugin-owned staging under a hard byte cap;
4. its Inno metadata is parsed, and exactly one Cheat Engine installer URL must
   fall out of it.

Resolving a URL settles where an artifact lives, never whether to trust it. The
caller still holds what it finds to the reviewed SHA-256, which is also the only
artifact the pinned native extractor can read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import re

from .atomic import read_regular_bytes
from .inno_reader import metadata_urls, read_setup_metadata

MAX_URL_BYTES = 2048
MAX_PAGE_LINKS = 4096

#: `/f/CheatEngine/<id>/CheatEngine<version>.exe` on the publisher's CDN. The
#: host and the opaque id both change between releases; the shape does not.
_CE_INSTALLER_URL = re.compile(
    r"https://[a-z0-9.-]+/f/CheatEngine/[0-9]+/CheatEngine[0-9]+\.exe",
    re.IGNORECASE,
)
_HREF = re.compile(r"""href\s*=\s*["']([^"'>\s]+)["']""", re.IGNORECASE)
_HOSTNAME = re.compile(r"[a-z0-9.-]+")


@dataclass(frozen=True)
class FallbackPolicy:
    """The bounded permission the manifest grants this rediscovery path."""

    page_url: str
    page_hosts: tuple[str, ...]
    max_page_bytes: int
    max_stub_bytes: int


@dataclass(frozen=True)
class ResolvedInstaller:
    """Where a rediscovered artifact lives, and what led there."""

    url: str
    host: str
    filename: str
    stub_host: str
    setup_id: str

    def evidence(self) -> dict[str, object]:
        """Provenance worth keeping. The URL itself is deliberately absent."""
        record = asdict(self)
        record.pop("url")
        return record


def safe_installer_url(url: str) -> str:
    """Accept only a plain HTTPS URL naming a Windows executable."""
    if not isinstance(url, str) or not url or len(url.encode("utf-8")) > MAX_URL_BYTES:
        raise ValueError("rediscovered installer URL is invalid")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not _HOSTNAME.fullmatch(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("rediscovered installer URL is invalid")
    name = parsed.path.rsplit("/", 1)[-1]
    if not name or name in {".", ".."} or not name.lower().endswith(".exe"):
        raise ValueError("rediscovered installer URL does not name an executable")
    return url


def find_stub_url(page: str, policy: FallbackPolicy, base_url: str | None = None) -> str:
    """The one off-site `.exe` the download page offers.

    The page's own links point at translations and non-Windows builds; the
    Windows button is the only one that leaves the site. More than one candidate
    means the page changed shape, and guessing which is the download would be
    exactly the wrong reflex.

    Relative links resolve against `base_url`, the URL the page was actually
    served from. A same-host redirect would otherwise have them resolved
    against the pinned path they no longer sit under.
    """
    candidates: set[str] = set()
    base = base_url or policy.page_url
    for index, href in enumerate(_HREF.finditer(page)):
        if index >= MAX_PAGE_LINKS:
            raise ValueError("Cheat Engine download page has an unreasonable link count")
        target = urljoin(base, href.group(1).strip())
        try:
            safe = safe_installer_url(target)
        except ValueError:
            continue
        host = urlsplit(safe).hostname or ""
        if host.casefold() in policy.page_hosts:
            continue
        candidates.add(safe)
    if len(candidates) != 1:
        raise ValueError(
            f"Cheat Engine download page offers {len(candidates)} download links "
            "instead of exactly one"
        )
    return candidates.pop()


def resolve_from_stub(stub: bytes) -> tuple[str, str]:
    """Return `(installer URL, setup id)` read out of the stub's metadata."""
    metadata = read_setup_metadata(stub)
    found = {
        url for url in metadata_urls(metadata.blocks)
        if _CE_INSTALLER_URL.fullmatch(url)
    }
    if len(found) != 1:
        raise ValueError(
            f"download helper names {len(found)} Cheat Engine installers "
            "instead of exactly one"
        )
    return safe_installer_url(found.pop()), metadata.setup_id


async def resolve_installer(
    network,
    policy: FallbackPolicy,
    staging: Path,
) -> ResolvedInstaller:
    """Rediscover the current installer URL. Never downloads the installer."""
    page_response = await network.get(
        policy.page_url,
        allowed_hosts=frozenset(policy.page_hosts),
        max_bytes=policy.max_page_bytes,
    )
    if page_response.status != 200:
        raise ValueError(
            f"Cheat Engine download page is unavailable (HTTP {page_response.status})"
        )
    page = page_response.body.decode("utf-8", "replace")
    # `get` follows redirects within the pinned host allowlist, so the URL the
    # page came from is not necessarily the one that was asked for.
    stub_url = find_stub_url(page, policy, page_response.url)
    stub_host = urlsplit(stub_url).hostname or ""

    # The stub's host is whatever the pinned page linked to, so the allowlist is
    # derived from that page rather than from an attacker-supplied redirect.
    stub_path = staging / "download-helper.bin"
    await network.download(
        stub_url,
        stub_path,
        allowed_hosts=frozenset({stub_host.casefold()}),
        max_bytes=policy.max_stub_bytes,
    )
    stub = read_regular_bytes(stub_path, max_bytes=policy.max_stub_bytes)
    if stub is None:
        raise ValueError("download helper could not be read back")

    url, setup_id = resolve_from_stub(stub)
    parsed = urlsplit(url)
    return ResolvedInstaller(
        url=url,
        host=(parsed.hostname or "").casefold(),
        filename=parsed.path.rsplit("/", 1)[-1],
        stub_host=stub_host.casefold(),
        setup_id=setup_id,
    )
