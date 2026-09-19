"""Whether a newer CE Decky release exists, and what it is made of.

This module decides; it never fetches. The service owns every request, the way
it does for `github_discovery.py`, so the whole of this file is exercised
offline against captured payloads.

What it is allowed to conclude is deliberately narrow. A release is an offer
only when the tag parses as a version this project could have produced, that
version is strictly newer than the running one, and the release carries both
the archive and the checksum file the release workflow publishes beside it. The
checksum file is what makes the offer usable at all: it is the release's own
statement of what the archive's bytes are, and nothing downloads without it.

Prereleases are not offered. The release workflow marks them, GitHub's own
`releases/latest` route excludes them, and both halves are checked here rather
than trusted, because the route is a URL and the flag is a field.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import time
from typing import Any
from urllib.parse import urlsplit

from .atomic import atomic_write_json, load_json

# Where this plugin's own releases are published. One place, because a URL that
# is retyped is a URL that can point somewhere else after an edit.
REPOSITORY = "goooroooX/CE-Decky"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{REPOSITORY}/releases"
# The hosts a release's own assets are served from. `github_discovery.py`
# declares the same set for the provider that reads other people's releases;
# this one is about our own, and the two are held apart on purpose so that
# switching the GitHub table source off never disables updates.
API_HOSTS = frozenset({"api.github.com"})
ASSET_HOSTS = frozenset({
    "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
})
# What the release workflow publishes: one archive named for its version, and
# one checksum file beside it.
SUMS_ASSET_NAME = "SHA256SUMS"
ARCHIVE_PREFIX = "CE-Decky-v"
ARCHIVE_SUFFIX = ".zip"

MAX_RELEASE_BYTES = 512 * 1024
MAX_SUMS_BYTES = 64 * 1024
# The 0.9.27 archive is a little over a megabyte. The bound is what stops an
# unbounded body rather than a prediction of how the package grows.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UPDATE_STATE_BYTES = 16 * 1024

# How long after the last table search the plugin still considers its user
# active enough to owe an update check. The same day the provider index uses,
# and for the same reason: a device nobody is using should not ask GitHub
# anything, for ever, on a timer.
ACTIVE_WINDOW_SECONDS = 24 * 60 * 60
# How often an armed device actually asks. One request, so the cost is the
# round trip rather than the budget; a release is not published hourly.
CHECK_INTERVAL_SECONDS = 6 * 60 * 60
# How often the scheduler looks at whether a check is owed, and how long it
# waits before the first look. Steam, Decky and every other plugin start in the
# same seconds, and nothing here is owed that soon.
SCHEDULE_INTERVAL_SECONDS = 5 * 60
SCHEDULE_FIRST_DELAY_SECONDS = 3 * 60
# The floor under a forced check. Check now is a press, and a press can repeat;
# the anonymous GitHub budget is shared with the table source that searches it.
FORCED_CHECK_FLOOR_SECONDS = 60.0
# How long a started install may stay unfinished before the next load reads it
# as one that failed. The runner writes its outcome either way; this is for the
# case where it could not, which is the case a user is otherwise left in with a
# panel that says an update is in progress for ever.
INSTALL_PENDING_LIMIT_SECONDS = 15 * 60

_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,6})(?:-([0-9A-Za-z.-]{1,32}))?$")
_SUMS_LINE_RE = re.compile(r"^([0-9a-fA-F]{64})\s[\s*](\S.*)$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(RuntimeError):
    """A release could not be read, or is not one this build may install."""


@dataclass(frozen=True)
class Version:
    """A released version, ordered the way this project numbers them.

    Numeric components compare as numbers rather than as text, so `0.9.10`
    follows `0.9.9` and `1.0.0` follows every `0.9.x`. A prerelease sorts below
    the release it is a prerelease of, which is what keeps `1.0.0-rc.1` from
    being offered to somebody already on `1.0.0`.
    """

    release: tuple[int, int, int]
    prerelease: tuple[str, ...]

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        text = ".".join(str(part) for part in self.release)
        return f"{text}-{'.'.join(self.prerelease)}" if self.prerelease else text

    def _key(self) -> tuple[Any, ...]:
        # A release outranks its own prereleases, hence the leading flag; the
        # identifiers below it are compared as text, which is enough for the
        # `rc.N` this project uses and never claims more.
        return (self.release, 1 if not self.prerelease else 0, self.prerelease)

    def __lt__(self, other: "Version") -> bool:
        return self._key() < other._key()

    def __le__(self, other: "Version") -> bool:
        return self._key() <= other._key()


def parse_version(value: Any) -> Version:
    """One version string, or a refusal. Never a guess at what was meant."""
    if not isinstance(value, str):
        raise UpdateError("version is not text")
    # Deliberately not stripped. These strings come from a machine: this
    # package's own `__version__` and a release's own tag. Padding in one is a
    # payload that is not what it claims to be, and quietly accepting it is how
    # a version nobody wrote gets compared against.
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise UpdateError(f"version is not one this project publishes: {value!r}"[:200])
    major, minor, patch, prerelease = match.groups()
    parts = tuple(prerelease.split(".")) if prerelease else ()
    if any(not part for part in parts):
        raise UpdateError("version prerelease is malformed")
    return Version((int(major), int(minor), int(patch)), parts)


def version_from_tag(tag: Any) -> Version:
    """The version a release tag names. The tag is exactly `v<version>`."""
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("release tag is not in this project's form")
    return parse_version(tag[1:])


@dataclass(frozen=True)
class ReleaseOffer:
    """A release this build could install, with everything needed to do it."""

    version: str
    tag: str
    archive_name: str
    archive_url: str
    sums_url: str
    page_url: str


def _asset_url(asset: Any, expected_name: str) -> str | None:
    """The download URL of one named asset, once its host is one of ours.

    The API answers with URLs, and a URL is a claim: an asset that names a host
    this release cannot have published from is dropped rather than followed.
    """
    if not isinstance(asset, dict) or asset.get("name") != expected_name:
        return None
    url = asset.get("browser_download_url")
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in ASSET_HOSTS:
        raise UpdateError("release asset is served from an unexpected host")
    return url


def release_version(payload: Any) -> Version:
    """The version this release publishes, whether or not it is an update.

    Recorded even when it is older than the running build, because the durable
    record says what the newest release is and a field that answered with the
    running version instead would state something that is not true on any
    development build. What a screen acts on is the comparison, not this.
    """
    if not isinstance(payload, dict):
        raise UpdateError("release payload is not an object")
    if payload.get("draft") is True or payload.get("prerelease") is True:
        raise UpdateError("newest release is not a stable release")
    offered = version_from_tag(payload.get("tag_name"))
    if offered.is_prerelease:
        raise UpdateError("newest release is not a stable release")
    return offered


def parse_release(payload: Any, *, current_version: str) -> ReleaseOffer | None:
    """What this release offers a device on `current_version`, if anything.

    `None` means the release is readable and is not an update: the current
    version is the same or newer. An unreadable release, a prerelease, a draft
    and a release missing either of its two assets all raise, because each of
    those is something to say on the screen rather than silence.
    """
    offered = release_version(payload)
    tag = payload.get("tag_name")
    running = parse_version(current_version)
    if offered <= running:
        return None
    archive_name = f"{ARCHIVE_PREFIX}{offered}{ARCHIVE_SUFFIX}"
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) > 64:
        raise UpdateError("release assets are missing")
    archive_url = next((found for asset in assets if (found := _asset_url(asset, archive_name))), None)
    sums_url = next((found for asset in assets if (found := _asset_url(asset, SUMS_ASSET_NAME))), None)
    if archive_url is None:
        raise UpdateError(f"release {offered} carries no {archive_name}")
    if sums_url is None:
        raise UpdateError(f"release {offered} carries no {SUMS_ASSET_NAME}")
    return ReleaseOffer(
        version=str(offered),
        tag=str(tag),
        archive_name=archive_name,
        archive_url=archive_url,
        sums_url=sums_url,
        page_url=f"{RELEASES_PAGE_URL}/tag/{tag}",
    )


def digest_from_sums(body: bytes, archive_name: str) -> str:
    """The archive's digest as its own release states it.

    `sha256sum` writes one line per file, and a release may one day carry more
    than one. The line for this exact name is the only one read, and a name
    that appears twice is refused rather than resolved by taking the first.
    """
    if len(body) > MAX_SUMS_BYTES:
        raise UpdateError("release checksum file is too large")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError("release checksum file is not text") from exc
    found: list[str] = []
    for line in text.splitlines()[:64]:
        match = _SUMS_LINE_RE.fullmatch(line.strip("\r"))
        if match is not None and PurePosixPath(match.group(2).strip()).name == archive_name:
            found.append(match.group(1).lower())
    if len(found) != 1:
        raise UpdateError(f"release checksum file does not state one digest for {archive_name}")
    return found[0]


class UpdateStateStore:
    """What the last check found and what the last install did.

    A cache and a report, never an authorization: nothing here decides that an
    archive may be installed, which is what the digest does. An unreadable
    record therefore reads as never checked, offers nothing and refuses
    nothing, exactly as the blocklist refuses nothing when it cannot be read.
    """

    SCHEMA = 1

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            raw = load_json(self.path, {}, max_bytes=MAX_UPDATE_STATE_BYTES)
        except (ValueError, OSError):
            # A failed read of the medium is the same answer as a file that
            # says nothing: never checked. `load_json` turns most of those into
            # `ValueError` itself, and the one it does not - a read that fails
            # part way through - would otherwise reach a caller this promises
            # never to raise at.
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != self.SCHEMA:
            return {}
        return raw

    def save(self, record: dict[str, Any]) -> None:
        atomic_write_json(self.path, {**record, "schema": self.SCHEMA}, max_bytes=MAX_UPDATE_STATE_BYTES)

    def update(self, **fields: Any) -> dict[str, Any]:
        record = {**self.load(), **fields}
        self.save(record)
        return record


def checked_now(offer: ReleaseOffer | None, latest: str, *, current_version: str) -> dict[str, Any]:
    """The record one successful check leaves behind.

    `latest` is what the release itself published, which on a development build
    is legitimately older than what is running; the offer is what this device
    could install, which is nothing in that case.
    """
    now = time.time()
    return {
        "checked_at": now,
        # When GitHub was last asked, whatever it answered. The pacing reads
        # this one, so that a device which cannot reach GitHub is not asking
        # again at every tick of the scheduler.
        "attempted_at": now,
        "current_version": current_version,
        "latest_version": latest,
        "archive_name": offer.archive_name if offer is not None else None,
        "page_url": offer.page_url if offer is not None else RELEASES_PAGE_URL,
        "last_error": None,
    }
