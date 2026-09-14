#!/usr/bin/env python3
"""Say whether Steam's UI is still executing JavaScript, and where it is stuck.

A wedged Steam UI takes the controller with it: the Quick Access panel stops
drawing, B reaches the game instead of the panel, the Steam button does nothing,
and the only way out is the power button.  Nothing about that state survives the
reboot, so it has to be read while it is happening.

This probe attaches to Steam's own CEF debugging endpoint read-only.  For each
page it evaluates one constant expression under a short deadline: an answer
proves that page's main thread is running JavaScript, and a timeout proves it is
not.  For a page that does not answer it then asks V8 to break, which a
synchronous loop cannot refuse, and prints the call stack that was running - the
function that wedged the UI, by name and source position.

It never injects input, never clicks, and never evaluates anything the page can
observe.  ``--resume`` leaves the paused target running again; without it the
target stays paused so a human can look at it in the inspector.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from . import host_platform
else:
    import host_platform
from typing import Any

DEFAULT_ENDPOINT = "http://localhost:8080"
DEFAULT_EVALUATE_TIMEOUT = 3.0
MAX_PAGES = 64
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_STACK_FRAMES = 40
# Steam names its Quick Access page this way; the panel a plugin renders lives
# inside it, so it is the one that matters most and it is reported first.
INTERESTING_TITLES = ("QuickAccess", "SP", "MainMenu", "Steam Big Picture Mode")


class ProbeError(RuntimeError):
    """A diagnostic could not be produced; never a statement about the UI."""


def _pages(endpoint: str, timeout: float) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/list", timeout=timeout) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProbeError(f"Steam CEF endpoint {endpoint} did not answer: {exc}") from exc
    if not isinstance(payload, list):
        raise ProbeError("Steam CEF endpoint returned an unexpected payload")
    pages = [item for item in payload[:MAX_PAGES] if isinstance(item, dict) and item.get("type") == "page"]
    if not pages:
        raise ProbeError("Steam CEF endpoint listed no pages")
    return pages


def _rank(page: dict[str, Any]) -> tuple[int, str]:
    title = str(page.get("title", ""))
    for index, name in enumerate(INTERESTING_TITLES):
        if title.startswith(name):
            return index, title
    return len(INTERESTING_TITLES), title


class DevToolsSocket:
    """The smallest WebSocket client that can carry the DevTools protocol.

    No third-party dependency is available on the target, and the probe has to
    work on a machine that is already in trouble, so this speaks the framing
    itself: text frames only, no extensions offered, no fragmentation sent.
    """

    def __init__(self, url: str, timeout: float) -> None:
        if not url.startswith("ws://"):
            raise ProbeError(f"unsupported DevTools URL: {url}")
        host_and_path = url[len("ws://"):]
        host_port, _, path = host_and_path.partition("/")
        host, _, port = host_port.partition(":")
        self._id = 0
        self._buffer = b""
        self._socket = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self._socket.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host_port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(request.encode("ascii"))
        header = self._read_until(b"\r\n\r\n")
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise ProbeError(f"DevTools refused the WebSocket upgrade: {header[:120]!r}")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise ProbeError("DevTools closed the connection during the handshake")
            self._buffer += chunk
            if len(self._buffer) > MAX_FRAME_BYTES:
                raise ProbeError("DevTools handshake exceeded its byte budget")
        head, _, rest = self._buffer.partition(marker)
        self._buffer = rest
        return head + marker

    def _recv_exactly(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._socket.recv(65536)
            if not chunk:
                raise ProbeError("DevTools closed the connection")
            self._buffer += chunk
        head, self._buffer = self._buffer[:count], self._buffer[count:]
        return head

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        self._id += 1
        payload = json.dumps({"id": self._id, "method": method, "params": params or {}}).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise ProbeError("DevTools request exceeded its byte budget")
        header = bytearray([0x81])
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + masked)
        return self._id

    def _frame(self) -> dict[str, Any] | None:
        first, second = self._recv_exactly(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exactly(8))[0]
        if length > MAX_FRAME_BYTES:
            raise ProbeError("DevTools frame exceeded its byte budget")
        body = self._recv_exactly(length) if length else b""
        if opcode == 0x8:
            raise ProbeError("DevTools closed the connection")
        if opcode != 0x1:
            return None
        try:
            message = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None
        return message if isinstance(message, dict) else None

    def await_reply(self, request_id: int, deadline: float) -> dict[str, Any]:
        """Read frames until this request answers, or the deadline passes."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            self._socket.settimeout(remaining)
            try:
                message = self._frame()
            except (socket.timeout, TimeoutError) as exc:
                raise TimeoutError from exc
            if message is not None and message.get("id") == request_id:
                return message

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def _describe_frame(frame: dict[str, Any]) -> str:
    location = frame.get("location") if isinstance(frame.get("location"), dict) else {}
    line = location.get("lineNumber")
    column = location.get("columnNumber")
    where = f"{frame.get('url') or '<anonymous>'}"
    if isinstance(line, int):
        where += f":{line + 1}" + (f":{column + 1}" if isinstance(column, int) else "")
    return f"{frame.get('functionName') or '(anonymous)'} at {where}"


def _inspect(page: dict[str, Any], timeout: float, resume: bool) -> dict[str, Any]:
    url = str(page.get("webSocketDebuggerUrl", ""))
    report: dict[str, Any] = {"title": page.get("title"), "id": page.get("id"), "javascript": "unknown"}
    if not url:
        report["error"] = "page exposes no DevTools socket"
        return report
    try:
        connection = DevToolsSocket(url, timeout)
    except (ProbeError, OSError) as exc:
        report["error"] = f"could not attach: {exc}"
        return report
    try:
        request = connection.send("Runtime.evaluate", {"expression": "1+1", "returnByValue": True})
        try:
            connection.await_reply(request, time.monotonic() + timeout)
            report["javascript"] = "running"
            return report
        except TimeoutError:
            report["javascript"] = "wedged"
        # A synchronous loop cannot answer an evaluate, but it cannot refuse a
        # debugger break either: this is what names the code that is looping.
        paused = _pause_and_read_stack(connection, timeout)
        report.update(paused)
        if resume and paused.get("stack"):
            resumed = connection.send("Debugger.resume")
            try:
                connection.await_reply(resumed, time.monotonic() + timeout)
                report["resumed"] = True
            except TimeoutError:
                report["resumed"] = False
        return report
    finally:
        connection.close()


def _pause_and_read_stack(connection: DevToolsSocket, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout * 3
    try:
        enable = connection.send("Debugger.enable")
        connection.await_reply(enable, deadline)
        pause = connection.send("Debugger.pause")
        connection.await_reply(pause, deadline)
    except TimeoutError:
        return {"stack": [], "pause": "the debugger itself did not answer; the whole renderer is blocked"}
    # `Debugger.paused` arrives as an event, so read frames until it shows up.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"stack": [], "pause": "paused was requested but no break was reported"}
        connection._socket.settimeout(remaining)  # noqa: SLF001 - one client, one socket
        try:
            message = connection._frame()  # noqa: SLF001
        except (socket.timeout, TimeoutError, ProbeError):
            return {"stack": [], "pause": "paused was requested but no break was reported"}
        if message is None or message.get("method") != "Debugger.paused":
            continue
        frames = message.get("params", {}).get("callFrames", [])
        return {
            "pause": message.get("params", {}).get("reason", "paused"),
            "stack": [_describe_frame(frame) for frame in frames[:MAX_STACK_FRAMES] if isinstance(frame, dict)],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Steam CEF debugging endpoint")
    parser.add_argument("--timeout", type=float, default=DEFAULT_EVALUATE_TIMEOUT, help="seconds to wait for one answer")
    parser.add_argument("--all", action="store_true", help="probe every page, not only Steam's own UI pages")
    parser.add_argument("--resume", action="store_true", help="let a paused target run again after reading its stack")
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    args = parser.parse_args()
    try:
        host_platform.require("The Steam UI freeze probe", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if not 0.5 <= args.timeout <= 60:
        print("ui freeze probe: --timeout must be between 0.5 and 60 seconds", file=sys.stderr)
        return 2

    try:
        pages = sorted(_pages(args.endpoint, args.timeout), key=_rank)
    except ProbeError as exc:
        # The endpoint not answering at all is itself a result worth printing.
        print(f"ui freeze probe: {exc}", file=sys.stderr)
        return 1
    if not args.all:
        pages = [page for page in pages if _rank(page)[0] < len(INTERESTING_TITLES)] or pages[:1]

    reports = [_inspect(page, args.timeout, args.resume) for page in pages]
    if args.json:
        print(json.dumps({"endpoint": args.endpoint, "pages": reports}, indent=2))
        return 0
    for report in reports:
        state = report.get("javascript")
        print(f"{report.get('title')}: javascript {state}" + (f" · {report['error']}" if report.get("error") else ""))
        if report.get("pause"):
            print(f"  break: {report['pause']}")
        for frame in report.get("stack", []):
            print(f"    {frame}")
        if "resumed" in report:
            print(f"  resumed: {report['resumed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
