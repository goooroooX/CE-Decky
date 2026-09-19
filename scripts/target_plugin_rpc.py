#!/usr/bin/env python3
"""Call one method on the live CE Decky backend, through Decky itself.

Every other target helper here reads state or replaces the package. This one
drives the backend that is actually loaded, which is the only way to reach
behaviour that belongs to the live plugin process rather than to its files: an
owned launch, a search that arms the background pass, an operation that has to
exist at the moment the plugin is stopped.

It exists because the alternative is a hand-written websocket against Decky's
API in a shell, which is the shape the tracked-helper rule is about: it looks
fine, it carries none of the guarantees below, and the fact it produced cannot
be reproduced by anybody reading the report.

What it guarantees:

- the transport is Decky's own authenticated websocket, taken from
  `target_plugin_install.py`, so there is one implementation of it here;
- the call names the plugin exactly and the method exactly, and a refusal from
  the backend is printed as the backend's own message rather than swallowed;
- it is bounded: one connection, one call, one timeout, no retry loop;
- it writes nothing itself. What the named method does is the method's business,
  and a mutating method is a mutation: that is the point of the helper, and it
  is why it refuses to run where the device is not the target.

It is a development tool. Nothing in the product calls it, and it is not a
substitute for the read-only probes: reach for `target_state_probe.py` or
`target_plugin_log.py` for a question they already answer.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import secrets  # noqa: E402

import host_platform  # noqa: E402
from target_plugin_install import (  # noqa: E402
    DEFAULT_DECKY_URL,
    PLUGIN_NAME,
    DeckyWebSocket,
    DeckyWebSocketClosed,
    _auth_token,
    _await_reply,
)

def request_id() -> int:
    """A different id for every call, because a reply is not private to its socket.

    Decky delivers a plugin method's result to clients other than the one that
    asked, so a fixed id cannot tell this call's answer from the replay of an
    earlier one carrying the same id. Two identical calls a few seconds apart
    were observed on this project's device to return one answer twice: the
    backend's own log showed one call, and the helper printed a reply for two.
    An id drawn per call makes that indistinguishable case impossible.
    """
    return secrets.randbelow(1_000_000) + 1000


def call(method: str, args: list[object], decky_url: str, timeout: float) -> object:
    """One method on the live backend, with its answer or its refusal."""
    identity = request_id()
    token = _auth_token(decky_url, min(timeout, 10.0))
    with DeckyWebSocket.connect(decky_url, token, timeout) as socket:
        socket.send_json({
            "type": 0,
            "route": "loader/call_plugin_method",
            # Decky passes what follows the method name as positional arguments,
            # so the list is splatted rather than handed over as one value: a
            # nested list arrives as a single argument and the backend refuses
            # it, which is a confusing way to learn this.
            "args": [PLUGIN_NAME, method, *args],
            "id": identity,
        })
        return _await_reply(socket, identity)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("method", help="exact backend method name, as `src/api.ts` names it")
    parser.add_argument(
        "args", nargs="?", default="[]",
        help="the method's positional arguments as one JSON array; default none",
    )
    parser.add_argument("--decky-url", default=DEFAULT_DECKY_URL)
    parser.add_argument("--timeout", type=float, default=30.0)
    parsed = parser.parse_args(argv)

    try:
        host_platform.require("The live backend RPC helper", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if parsed.timeout <= 0 or parsed.timeout > 300:
        print("target plugin rpc: --timeout must be greater than zero and at most 300 seconds", file=sys.stderr)
        return 2
    try:
        arguments = json.loads(parsed.args)
    except json.JSONDecodeError as exc:
        print(f"target plugin rpc: arguments are not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, list):
        print("target plugin rpc: arguments must be a JSON array", file=sys.stderr)
        return 2
    try:
        answer = call(parsed.method, arguments, parsed.decky_url.rstrip("/"), parsed.timeout)
    except (OSError, DeckyWebSocketClosed, TimeoutError, RuntimeError, ValueError) as exc:
        print(f"target plugin rpc: {exc}", file=sys.stderr)
        return 2
    json.dump(answer, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
