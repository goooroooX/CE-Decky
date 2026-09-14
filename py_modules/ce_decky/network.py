from __future__ import annotations

from dataclasses import dataclass
import asyncio
import ipaddress
import os
import socket
import ssl
from pathlib import Path
from typing import Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from . import __version__


MAX_REDIRECTS = 5
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
TRANSIENT_STATUSES = {502, 503, 504}
# A form body is a provider handshake, never an upload: the largest one this
# sends is a few identifiers and a token.
MAX_FORM_BYTES = 8 * 1024
MAX_FORM_FIELDS = 32


class NetworkError(RuntimeError):
    pass


class ResponseTooLarge(NetworkError):
    pass


class ProviderRateLimited(NetworkError):
    """A provider answered HTTP 429, which is a wait rather than a refusal.

    The header is carried verbatim because what a bounded wait should be is the
    caller's decision: a background crawl can afford the provider's own number,
    while a download a user is watching cannot afford an unbounded one. Keeping
    the class here rather than beside one caller is what lets the transport
    raise it, since every layer above already imports this one.
    """

    def __init__(
        self,
        retry_after: str | None,
        message: str = "HTTP 429 provider cooldown",
        *,
        restartable: bool = True,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        # Whether simply making the same request again is a sound thing to do.
        # A plain GET is: nothing was spent reaching it. A step that already
        # consumed a one-shot exchange - a countdown token a provider issues
        # once - is not, and a caller that waits and retries anyway would be
        # replaying a handshake the provider has already closed.
        self.restartable = restartable


class ProviderChallenged(NetworkError):
    """A provider answered a page with a challenge rather than with content.

    Separate from an ordinary failure because the product contract treats it as
    one: a challenged provider is unavailable for this query and is not handed
    to the user, since browsing the web with a controller is not a workflow.
    Read as merely "not 200" it was skipped page by page, and a provider that
    challenged every matching page returned an empty list that read as a
    successful search of no results.
    """

    def __init__(self, message: str = "provider answered with a challenge", *, status: int = 403) -> None:
        super().__init__(message)
        self.status = status


class ArtifactGone(NetworkError):
    """The provider says it does not have this artifact any more.

    Its own answer about this one file, not a fault of the transport and not a
    state of the provider: the page that offered it is still listed and still
    reachable, and what it points at is not there. It is separate because it is
    the one download failure that will read the same however many times it is
    asked, so the row is retired instead of being offered again for the price of
    another provider countdown.

    A provider that serves artifacts over plain HTTPS says this with 404 or 410.
    One whose own client contract answers in its own vocabulary raises it from
    its resolver, which is the only place that vocabulary is understood.
    """

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(f"the provider no longer has this file{f': {detail}' if detail else ''}")
        # What the provider actually answered, for the log. The sentence the
        # user reads is composed where the provider's own display name is
        # known, which is not here.
        self.detail = detail


class ProviderCooldown(NetworkError):
    """A provider is not being asked, because a deadline it named has not passed.

    Distinct from a failure of the provider, which is what it used to be
    reported as: recording it as one overwrote the very deadline that produced
    it, so a single search during a cooldown made the next search eligible
    immediately. Nothing was contacted, so there is nothing to record.
    """


@dataclass(frozen=True)
class NetworkResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value.split("%", 1)[0])
    return bool(address.is_global)


def _validate_max_bytes(max_bytes: int) -> None:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_RESPONSE_BYTES:
        raise ValueError("response byte limit is invalid")


def is_transport_ready_url(url: object, allowed_hosts: frozenset[str]) -> bool:
    """The structural half of `validate_network_url`, answerable without DNS.

    A URL already known while a search runs can be held to this before a row
    that promises a direct download is built from it. The row was otherwise
    offered as downloadable and then refused by the transport after the user
    selected it, which is a guaranteed failure presented as a choice. Resolution
    and public-address policy stay in the transport, because they need the
    network and can change between search and download.
    """
    if not isinstance(url, str) or len(url.encode("utf-8")) > 8192:
        return False
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    try:
        if parts.port not in {None, 443}:
            return False
    except ValueError:
        return False
    return parts.hostname.rstrip(".").casefold() in allowed_hosts


async def validate_network_url(url: str, allowed_hosts: frozenset[str]) -> str:
    if not isinstance(url, str) or len(url.encode("utf-8")) > 8192:
        raise NetworkError("network URL is invalid or too long")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise NetworkError("provider URL must use HTTPS and include a hostname")
    if parts.username is not None or parts.password is not None:
        raise NetworkError("provider URL must not contain user information")
    if parts.port not in {None, 443}:
        raise NetworkError("provider URL uses a disallowed port")
    host = parts.hostname.rstrip(".").casefold()
    if host not in allowed_hosts:
        raise NetworkError("provider URL host is not allowlisted")
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise NetworkError("provider URL resolves to a non-public address")
        return host
    loop = asyncio.get_running_loop()
    try:
        answers = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NetworkError("provider hostname resolution failed") from exc
    addresses = {str(answer[4][0]) for answer in answers}
    if not addresses or any(not _public_address(address) for address in addresses):
        raise NetworkError("provider hostname has a non-public DNS answer")
    return host


class DownloadDestinationError(NetworkError):
    """The local file a download would be written to could not be opened.

    A subclass of `NetworkError` so every existing caller keeps treating it as
    a failed download, while a caller that is deciding whether to try a
    *different* download route can tell that nothing about the network failed.
    """


class NetworkClient:
    """Cancellation-aware bounded provider transport.

    DNS policy is evaluated before each request and redirect. The operating
    system resolver remains the accepted resolver-to-connect trust boundary;
    certificate and hostname verification are still enforced by TLS.
    """

    def __init__(self, ssl_context: ssl.SSLContext, *, user_agent: str | None = None) -> None:
        self._client = httpx.AsyncClient(
            verify=ssl_context,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(DEFAULT_TIMEOUT_SECONDS, connect=8.0),
            headers={"User-Agent": user_agent or f"CE-Decky/{__version__}", "Accept-Encoding": "gzip, deflate"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
        referer: str | None = None,
        retries: int = 1,
        follow_redirects: bool = True,
    ) -> NetworkResponse:
        _validate_max_bytes(max_bytes)
        if isinstance(retries, bool) or not isinstance(retries, int) or retries not in {0, 1, 2}:
            raise ValueError("network retry count is invalid")
        if not isinstance(follow_redirects, bool):
            raise ValueError("network redirect policy must be boolean")
        request_headers = dict(headers or {})
        if referer is not None:
            request_headers["Referer"] = referer
        current = url
        redirects = 0
        attempt = 0
        while True:
            await validate_network_url(current, allowed_hosts)
            try:
                response = await self._one(current, request_headers, max_bytes)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < retries:
                    attempt += 1
                    await asyncio.sleep(0.15 * attempt)
                    continue
                raise NetworkError("provider request failed") from exc
            if response.status in TRANSIENT_STATUSES and attempt < retries:
                attempt += 1
                await asyncio.sleep(0.15 * attempt)
                continue
            if response.status in {301, 302, 303, 307, 308}:
                if not follow_redirects:
                    return response
                location = response.headers.get("location")
                if not location:
                    raise NetworkError("provider redirect is missing Location")
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise NetworkError("provider redirect limit exceeded")
                current = urljoin(current, location)
                attempt = 0
                continue
            return response

    async def post(
        self,
        url: str,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        data: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        referer: str | None = None,
    ) -> NetworkResponse:
        """One form-encoded request, sent once and never redirected.

        A provider whose download link does not exist until it is asked for has
        to be asked with a POST. It is deliberately narrower than `get`: a POST
        is not safely repeatable, so a transient failure is reported rather than
        retried, and a redirect is returned to the caller rather than followed,
        because where a POST is redirected to is the caller's decision and not
        this transport's.
        """
        _validate_max_bytes(max_bytes)
        fields = dict(data)
        if len(fields) > MAX_FORM_FIELDS or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in fields.items()
        ):
            raise ValueError("provider form body is invalid")
        if sum(len(key.encode("utf-8")) + len(value.encode("utf-8")) for key, value in fields.items()) > MAX_FORM_BYTES:
            raise ValueError("provider form body exceeds the byte limit")
        request_headers = dict(headers or {})
        if referer is not None:
            request_headers["Referer"] = referer
        await validate_network_url(url, allowed_hosts)
        try:
            return await self._one(url, request_headers, max_bytes, method="POST", data=fields)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise NetworkError("provider request failed") from exc

    async def download(
        self,
        url: str,
        destination: Path,
        *,
        allowed_hosts: frozenset[str],
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
        referer: str | None = None,
    ) -> NetworkResponse:
        _validate_max_bytes(max_bytes)
        request_headers = dict(headers or {})
        if referer is not None:
            request_headers["Referer"] = referer
        destination.parent.mkdir(parents=True, exist_ok=True)
        current = url
        redirects = 0
        attempt = 0
        while True:
            await validate_network_url(current, allowed_hosts)
            try:
                response = await self._download_one(current, request_headers, destination, max_bytes)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < 1:
                    attempt += 1
                    await asyncio.sleep(0.15)
                    continue
                raise NetworkError("provider download failed") from exc
            if response.status in TRANSIENT_STATUSES and attempt < 1:
                destination.unlink(missing_ok=True)
                attempt += 1
                await asyncio.sleep(0.15)
                continue
            if response.status in {301, 302, 303, 307, 308}:
                destination.unlink(missing_ok=True)
                location = response.headers.get("location")
                if not location:
                    raise NetworkError("provider redirect is missing Location")
                redirects += 1
                if redirects > MAX_REDIRECTS:
                    raise NetworkError("provider redirect limit exceeded")
                current = urljoin(current, location)
                redirect_parts = urlsplit(current)
                if redirect_parts.scheme.casefold() != "https" or not redirect_parts.hostname:
                    scheme = redirect_parts.scheme.casefold() or "missing"
                    host_state = "present" if redirect_parts.hostname else "missing"
                    raise NetworkError(f"provider redirect target is invalid (scheme={scheme}, host={host_state})")
                attempt = 0
                continue
            if response.status == 429:
                # A rate limit is not a failed download: the provider is asking
                # for a wait, and the caller that knows whether anyone is
                # waiting for this decides how long to give it.
                destination.unlink(missing_ok=True)
                raise ProviderRateLimited(
                    response.headers.get("retry-after"),
                    "provider download returned HTTP 429",
                )
            if response.status in {404, 410}:
                # The provider answered about this exact file: it is not there.
                # Asking again cannot change that, and on a provider that makes
                # a guest sit through a countdown first it costs that countdown
                # to be told the same thing.
                destination.unlink(missing_ok=True)
                raise ArtifactGone(f"HTTP {response.status}")
            if response.status != 200:
                destination.unlink(missing_ok=True)
                raise NetworkError(f"provider download returned HTTP {response.status}")
            return response

    async def _download_one(self, url: str, headers: Mapping[str, str], destination: Path, max_bytes: int) -> NetworkResponse:
        async with self._client.stream("GET", url, headers=headers) as response:
            normalized_headers = {key.casefold(): value for key, value in response.headers.items()}
            length = normalized_headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        raise ResponseTooLarge("provider response exceeds the byte limit")
                except ValueError as exc:
                    raise NetworkError("provider Content-Length is invalid") from exc
            total = 0
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                fd = os.open(destination, flags, 0o600)
            except FileExistsError as exc:
                raise DownloadDestinationError("provider download destination already exists") from exc
            except OSError as exc:
                raise DownloadDestinationError("provider download destination could not be created safely") from exc
            try:
                with os.fdopen(fd, "wb") as handle:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ResponseTooLarge("provider response exceeds the byte limit")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            return NetworkResponse(str(response.url), response.status_code, normalized_headers, b"")

    async def _one(
        self,
        url: str,
        headers: Mapping[str, str],
        max_bytes: int,
        *,
        method: str = "GET",
        data: Mapping[str, str] | None = None,
    ) -> NetworkResponse:
        async with self._client.stream(method, url, headers=headers, data=data) as response:
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    if int(length) > max_bytes:
                        raise ResponseTooLarge("provider response exceeds the byte limit")
                except ValueError as exc:
                    raise NetworkError("provider Content-Length is invalid") from exc
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLarge("provider response exceeds the byte limit")
                chunks.append(chunk)
            return NetworkResponse(
                str(response.url),
                response.status_code,
                {key.casefold(): value for key, value in response.headers.items()},
                b"".join(chunks),
            )
