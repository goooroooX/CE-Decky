"""Decky's own loopback control socket, and the two routes an update needs.

Decky installs plugins, and it is the only thing on the device that can: the
plugins directory belongs to root and nothing here runs as root. Its loader
exposes an authenticated JSON RPC on loopback, hands its token to any local
process that asks, and installs a plugin from a URL the caller names. That is
the whole of the privilege this plugin borrows, and it borrows it for exactly
two routes: install this exact archive, and restart Steam's webhelper.

Everything here is transport and envelope handling. What may be installed is
decided before any of it runs: `plugin_update.py` decides what the release is,
the archive's SHA-256 is verified against the release's own checksum file, and
the user has pressed a control that says what will happen. Nothing in this file
reads a version, chooses an artifact, or relaxes any of that.

The install prompt is answered here rather than shown, because it is Decky
asking its own frontend to confirm what a user already confirmed on this
plugin's own screen. Every field of that prompt is checked against what was
asked for first: another plugin's name, another version, another digest or a
second prompt for one install are each refused.

`scripts/target_plugin_install.py` drives the same two routes for development
installs and imports them from here, so the envelope handling that had to
change when Decky renumbered its pushed events lives in one file.
"""

from __future__ import annotations

from hashlib import sha1
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, build_opener
import base64
import json
import secrets
import socket
import struct

DEFAULT_DECKY_URL = "http://127.0.0.1:1337"
PLUGIN_NAME = "CE Decky"
MAX_WS_MESSAGE_BYTES = 8 * 1024 * 1024
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# The websocket message types Decky has used for a server-pushed event. The
# loader supplies exactly one of them per release, and the install prompt is
# pushed rather than returned, so both have to be recognised.
_INSTALL_PROMPT_TYPES = (3, 5)
# Decky's install type for replacing a plugin that is already installed. A
# fresh install is 0, and an update is never that: a device offering this
# update is a device already holding this plugin.
INSTALL_TYPE_REPLACE = 4


class DeckyWebSocketClosed(RuntimeError):
    """The Decky websocket closed before returning the requested RPC reply."""


def next_request_id() -> int:
    """An id no other client's reply can be carrying.

    Decky delivers a reply to sockets that did not ask for it, so an id used by
    convention rather than drawn cannot tell this call's answer from a replay of
    somebody else's. It matters wherever a wrong answer would change a
    conclusion: the readback that decides an install landed is drawn from here.

    The install handshake below keeps its fixed ids on purpose. Its prompt is
    validated field by field against what was asked for, and what it concludes
    is checked afterwards by that same readback, so an early reply there can
    only stop a wait rather than invent an outcome.
    """
    return secrets.randbelow(1_000_000) + 1000


def require_loopback(decky_url: str) -> tuple[str, int]:
    """The host and port of a Decky URL, once it is one this may talk to.

    Decky's token is what authorizes installing a plugin on this device, and it
    is handed to whoever asks on loopback. Every route to it therefore checks
    where it is being asked, not only the socket that uses it afterwards: the
    token request used to take the URL as given, so a caller that named another
    host would have sent the credential there and only the connection after it
    would have refused.
    """
    parsed = urlsplit(decky_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Decky URL must be loopback HTTP")
    return parsed.hostname, parsed.port or 80


def receive_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise DeckyWebSocketClosed("Decky websocket closed before the response completed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class DeckyWebSocket:
    """Small RFC 6455 client for Decky's loopback-only JSON RPC socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    @classmethod
    def connect(cls, decky_url: str, token: str, timeout: float) -> "DeckyWebSocket":
        hostname, port = require_loopback(decky_url)
        sock = socket.create_connection((hostname, port), timeout=timeout)
        sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = f"/ws?auth={quote(token, safe='')}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostname}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: {decky_url}\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            if len(response) > 64 * 1024:
                sock.close()
                raise RuntimeError("Decky websocket handshake headers are too large")
            response.extend(receive_exact(sock, 1))
        head = bytes(response).split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
        lines = head.split("\r\n")
        headers = {
            key.strip().casefold(): value.strip()
            for line in lines[1:]
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
        expected = base64.b64encode(sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")
        if not lines or " 101 " not in f" {lines[0]} " or headers.get("sec-websocket-accept") != expected:
            sock.close()
            raise RuntimeError("Decky rejected the websocket handshake")
        return cls(sock)

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self._sock.close()

    def __enter__(self) -> "DeckyWebSocket":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", first, 0x80 | size)
        elif size <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, size)
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def send_json(self, value: dict[str, object]) -> None:
        self._send_frame(0x1, json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def receive_json(self) -> dict[str, Any]:
        fragments = bytearray()
        text_started = False
        while True:
            first, second = struct.unpack("!BB", receive_exact(self._sock, 2))
            final = bool(first & 0x80)
            opcode = first & 0x0F
            if first & 0x70 or second & 0x80:
                raise RuntimeError("Decky sent an invalid websocket frame")
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", receive_exact(self._sock, 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", receive_exact(self._sock, 8))[0]
            if length > MAX_WS_MESSAGE_BYTES or len(fragments) + length > MAX_WS_MESSAGE_BYTES:
                raise RuntimeError("Decky websocket response exceeds the bounded message limit")
            payload = receive_exact(self._sock, length)
            if opcode == 0x8:
                raise DeckyWebSocketClosed("Decky websocket closed before the RPC completed")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                if text_started:
                    raise RuntimeError("Decky started a second fragmented websocket message")
                text_started = True
                fragments.extend(payload)
            elif opcode == 0x0 and text_started:
                fragments.extend(payload)
            else:
                raise RuntimeError("Decky sent an unsupported websocket frame")
            if final:
                try:
                    value = json.loads(fragments.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError("Decky websocket response is not valid JSON") from exc
                if not isinstance(value, dict):
                    raise RuntimeError("Decky websocket response must be a JSON object")
                return value


def rpc_error(message: dict[str, Any]) -> RuntimeError:
    error = message.get("error")
    if isinstance(error, dict):
        detail = error.get("message") or error.get("error") or error.get("name")
    else:
        detail = None
    return RuntimeError(f"Decky RPC failed: {detail or 'unknown error'}")


def await_reply(ws: Any, request_id: int) -> Any:
    while True:
        message = ws.receive_json()
        if message.get("id") != request_id:
            continue
        if message.get("type") == -1:
            raise rpc_error(message)
        if message.get("type") == 1:
            return message.get("result")


def loader_plugin_matches(ws: Any, request_id: int) -> list[dict[str, Any]]:
    ws.send_json({"type": 0, "route": "loader/get_plugins", "args": [], "id": request_id})
    plugins = await_reply(ws, request_id)
    if not isinstance(plugins, list):
        raise RuntimeError("Decky loader plugin inventory is not a list")
    return [item for item in plugins if isinstance(item, dict) and item.get("name") == PLUGIN_NAME]


def request_frontend_reload(ws: Any) -> bool:
    """Request the self-terminating webhelper reload.

    Decky normally closes the calling websocket while executing this RPC. The
    caller must still prove the webhelper process set changed before accepting
    that ambiguous transport outcome.
    """
    ws.send_json({"type": 0, "route": "utilities/restart_webhelper", "args": [], "id": 5})
    try:
        await_reply(ws, 5)
    except DeckyWebSocketClosed:
        return False
    return True


def install_and_confirm(ws: Any, artifact_url: str, version: str, package_sha: str, replace: bool) -> None:
    install_type = INSTALL_TYPE_REPLACE if replace else 0
    ws.send_json({
        "type": 0,
        "route": "utilities/install_plugin",
        "args": [artifact_url, PLUGIN_NAME, version, package_sha, install_type],
        "id": 1,
    })
    confirmation_sent = False
    while True:
        message = ws.receive_json()
        # Decky changed the envelope its events arrive in: v3.2.6 sent this one
        # as type 3 and v3.2.8-pre1 sends the identical payload as type 5. The
        # prompt is the only thing that carries the confirmation id, so a loader
        # whose number is not recognised here does not fail loudly - it waits
        # for a message that already went past, and the install never happens.
        # Both numbers name one event; everything about it is still validated.
        if message.get("type") in _INSTALL_PROMPT_TYPES and message.get("event") == "loader/add_plugin_install_prompt":
            args = message.get("args")
            if (
                not isinstance(args, list)
                or len(args) < 5
                or args[0] != PLUGIN_NAME
                or args[1] != version
                or args[3] != package_sha
                or args[4] != install_type
                or not isinstance(args[2], str)
                or not args[2]
            ):
                raise RuntimeError("Decky requested confirmation for an unexpected plugin")
            if confirmation_sent:
                raise RuntimeError("Decky requested duplicate confirmation for one install")
            ws.send_json({
                "type": 0,
                "route": "utilities/confirm_plugin_install",
                "args": [args[2]],
                "id": 2,
            })
            confirmation_sent = True
            continue
        if message.get("id") == 1:
            if message.get("type") == -1:
                raise rpc_error(message)
            # install_plugin returns after creating the confirmation request.
            # It is not installation completion and must never terminate this loop.
            continue
        if message.get("id") == 2:
            if message.get("type") == -1:
                raise rpc_error(message)
            if message.get("type") == 1:
                if not confirmation_sent:
                    raise RuntimeError("Decky completed an install that was not explicitly confirmed")
                return


def auth_token(decky_url: str, timeout: float) -> str:
    require_loopback(decky_url)
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"{decky_url}/auth/token", timeout=timeout) as response:
        token = response.read(4096).decode("utf-8").strip()
    if not token:
        raise RuntimeError("Decky returned an empty auth token")
    return token
