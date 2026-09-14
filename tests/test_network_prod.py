from __future__ import annotations

import asyncio
from pathlib import Path
import socket
import ssl

import httpx
import pytest

from ce_decky import __version__
from ce_decky.network import NetworkClient, NetworkError, ResponseTooLarge, validate_network_url


def _public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])


def test_default_user_agent_tracks_backend_version():
    client = NetworkClient(ssl.create_default_context())
    try:
        assert client._client.headers["user-agent"] == f"CE-Decky/{__version__}"
    finally:
        asyncio.run(client.aclose())


@pytest.mark.asyncio
async def test_redirect_cookie_referer_and_bounded_body(monkeypatch):
    _public_dns(monkeypatch)
    seen = []
    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/artifact", "set-cookie": "session=fixture; Path=/"})
        assert request.headers["referer"] == "https://example.com/topic"
        assert "session=fixture" in request.headers["cookie"]
        return httpx.Response(200, content=b"table")
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    response = await client.get("https://example.com/start", allowed_hosts=frozenset({"example.com"}), max_bytes=16, referer="https://example.com/topic")
    assert response.body == b"table" and len(seen) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_get_can_return_one_validated_redirect_without_following_it(monkeypatch):
    _public_dns(monkeypatch)
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://storage.example/artifact"})

    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    response = await client.get(
        "https://example.com/start",
        allowed_hosts=frozenset({"example.com"}),
        max_bytes=16,
        follow_redirects=False,
    )
    assert response.status == 302
    assert response.headers["location"] == "https://storage.example/artifact"
    assert len(seen) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_content_length_and_stream_limits(monkeypatch):
    _public_dns(monkeypatch)
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 20)), follow_redirects=False)
    with pytest.raises(ResponseTooLarge):
        await client.get("https://example.com/a", allowed_hosts=frozenset({"example.com"}), max_bytes=10)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_limit", [0, -1, False, 1.5, 64 * 1024 * 1024 + 1])
async def test_download_rejects_invalid_byte_limit_before_staging(monkeypatch, tmp_path: Path, bad_limit):
    _public_dns(monkeypatch)
    client = NetworkClient(ssl.create_default_context())
    target = tmp_path / "invalid-limit.CT"
    try:
        with pytest.raises(ValueError, match="byte limit"):
            await client.download("https://example.com/a", target, allowed_hosts=frozenset({"example.com"}), max_bytes=bad_limit)
        assert not target.exists()
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_retries", [False, True, 1.5, 3])
async def test_get_rejects_non_integer_or_out_of_range_retry_count(monkeypatch, bad_retries):
    _public_dns(monkeypatch)
    client = NetworkClient(ssl.create_default_context())
    try:
        with pytest.raises(ValueError, match="retry count"):
            await client.get("https://example.com/a", allowed_hosts=frozenset({"example.com"}), max_bytes=8, retries=bad_retries)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_dns_rejects_any_non_public_answer(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ])
    with pytest.raises(NetworkError, match="non-public"):
        await validate_network_url("https://example.com/a", frozenset({"example.com"}))


@pytest.mark.asyncio
async def test_tls_or_transport_failure_is_closed_and_cancellation_propagates(monkeypatch):
    _public_dns(monkeypatch)
    async def handler(request: httpx.Request):
        await asyncio.sleep(10)
        return httpx.Response(200)
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    task = asyncio.create_task(client.get("https://example.com/a", allowed_hosts=frozenset({"example.com"}), max_bytes=10))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()


@pytest.mark.asyncio
async def test_statuses_and_transient_retry_are_explicit(monkeypatch):
    _public_dns(monkeypatch)
    calls = 0
    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if request.url.path == "/forbidden":
            return httpx.Response(403, content=b"challenge")
        if request.url.path == "/limited":
            return httpx.Response(429, headers={"retry-after": "17"})
        return httpx.Response(503 if calls == 1 else 200, content=b"ok")
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    assert (await client.get("https://example.com/forbidden", allowed_hosts=frozenset({"example.com"}), max_bytes=32)).status == 403
    assert (await client.get("https://example.com/limited", allowed_hosts=frozenset({"example.com"}), max_bytes=32)).headers["retry-after"] == "17"
    calls = 0
    assert (await client.get("https://example.com/retry", allowed_hosts=frozenset({"example.com"}), max_bytes=32)).body == b"ok"
    assert calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_download_limit_removes_partial_staging(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 32)), follow_redirects=False)
    target = tmp_path / "artifact.CT"
    with pytest.raises(ResponseTooLarge):
        await client.download("https://example.com/a", target, allowed_hosts=frozenset({"example.com"}), max_bytes=8)
    assert not target.exists()
    await client.aclose()


@pytest.mark.asyncio
async def test_download_refuses_preexisting_destination_without_deleting_it(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"new")),
        follow_redirects=False,
    )
    target = tmp_path / "occupied.CT"
    target.write_bytes(b"existing")
    try:
        with pytest.raises(NetworkError, match="already exists"):
            await client.download(
                "https://example.com/a",
                target,
                allowed_hosts=frozenset({"example.com"}),
                max_bytes=32,
            )
        assert target.read_bytes() == b"existing"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_download_refuses_symlink_destination_without_touching_target(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    outside = tmp_path / "outside.CT"
    outside.write_bytes(b"outside")
    target = tmp_path / "artifact.CT"
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"new")),
        follow_redirects=False,
    )
    try:
        with pytest.raises(NetworkError, match="already exists|created safely"):
            await client.download(
                "https://example.com/a",
                target,
                allowed_hosts=frozenset({"example.com"}),
                max_bytes=32,
            )
        assert target.is_symlink()
        assert outside.read_bytes() == b"outside"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_real_local_http_redirect_cookie_referer_and_chunked_body(monkeypatch):
    requests = []
    async def policy(url, allowed_hosts):
        return "127.0.0.1"
    monkeypatch.setattr("ce_decky.network.validate_network_url", policy)

    async def serve(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        requests.append(request)
        path = request.split(b" ", 2)[1]
        if path == b"/start":
            writer.write(b"HTTP/1.1 302 Found\r\nLocation: /final\r\nSet-Cookie: fixture=yes; Path=/\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = NetworkClient(ssl.create_default_context())
    try:
        response = await client.get(
            f"http://127.0.0.1:{port}/start",
            allowed_hosts=frozenset({"127.0.0.1"}), max_bytes=8,
            referer="https://example.test/topic",
        )
        assert response.body == b"abcde"
        assert len(requests) == 2
        assert b"referer: https://example.test/topic" in requests[1].lower()
        assert b"cookie: fixture=yes" in requests[1].lower()
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_tls_connect_failure_does_not_fall_back_to_insecure(monkeypatch):
    _public_dns(monkeypatch)
    def handler(request: httpx.Request):
        raise httpx.ConnectError("certificate verify failed", request=request)
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(NetworkError):
        await client.get("https://example.com/a", allowed_hosts=frozenset({"example.com"}), max_bytes=8, retries=0)
    await client.aclose()


@pytest.mark.asyncio
async def test_partial_stream_failure_removes_download_staging(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("connection closed")
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=BrokenStream())), follow_redirects=False)
    target = tmp_path / "partial.CT"
    with pytest.raises(NetworkError):
        await client.download("https://example.com/a", target, allowed_hosts=frozenset({"example.com"}), max_bytes=32)
    assert not target.exists()
    await client.aclose()


@pytest.mark.asyncio
async def test_a_form_post_is_sent_once_and_never_redirected(monkeypatch):
    """A provider whose link does not exist until it is asked for needs a POST.

    It is deliberately narrower than `get`: a POST is not safely repeatable, so
    a transient failure is reported rather than retried, and where a redirected
    POST would go is the caller's decision rather than the transport's.
    """
    _public_dns(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "https://elsewhere.example/x"})
        return httpx.Response(200, json={"download_token": "tok"})

    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    try:
        response = await client.post(
            "https://example.com/waiting",
            allowed_hosts=frozenset({"example.com"}), max_bytes=1024,
            data={"file_hash": "abc", "file_id": "1"},
            referer="https://example.com/item",
        )
        assert response.status == 200 and b"download_token" in response.body
        assert seen[0].method == "POST"
        assert seen[0].content == b"file_hash=abc&file_id=1"
        assert seen[0].headers["referer"] == "https://example.com/item"

        redirected = await client.post(
            "https://example.com/moved",
            allowed_hosts=frozenset({"example.com"}), max_bytes=1024, data={"a": "b"},
        )
        assert redirected.status == 302
        assert len(seen) == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_a_post_still_answers_to_the_host_and_size_policy(monkeypatch):
    _public_dns(monkeypatch)
    client = NetworkClient(ssl.create_default_context())
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 64)),
        follow_redirects=False,
    )
    try:
        with pytest.raises(NetworkError):
            await client.post(
                "https://elsewhere.example/a",
                allowed_hosts=frozenset({"example.com"}), max_bytes=1024, data={"a": "b"},
            )
        with pytest.raises(NetworkError):
            await client.post(
                "http://example.com/a",
                allowed_hosts=frozenset({"example.com"}), max_bytes=1024, data={"a": "b"},
            )
        with pytest.raises(NetworkError):
            await client.post(
                "https://example.com/a",
                allowed_hosts=frozenset({"example.com"}), max_bytes=8, data={"a": "b"},
            )
        # A form body is a provider handshake, never an upload.
        with pytest.raises(ValueError):
            await client.post(
                "https://example.com/a",
                allowed_hosts=frozenset({"example.com"}), max_bytes=1024,
                data={"a": "x" * 9000},
            )
        with pytest.raises(ValueError):
            await client.post(
                "https://example.com/a",
                allowed_hosts=frozenset({"example.com"}), max_bytes=1024,
                data={"a": 5},  # type: ignore[dict-item]
            )
    finally:
        await client.aclose()
