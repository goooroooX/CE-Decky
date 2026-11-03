#!/usr/bin/env python3
"""Read what the running Cheat Engine holds for exact records, through the live bridge.

A question about a cheat on screen, such as why a row has no `Current`, is a
question about what the bridge answered for that record: its active state and
the value Cheat Engine reads there, `??` where it cannot. Nothing else on this
device holds that answer. The table says where a record points and the plugin's
state says what it remembers, and inferring the live answer from either is the
shape of a claim nobody reproduced.

It asks the way Configure cheats asks when it opens: one `query` command per
record, written at the session's next generation through the backend's own
`write_runtime_commands`, then `get_runtime_status` until the bridge has
acknowledged every one of them in the same exact session.

What it guarantees:

- it sends `query` and nothing else, so it switches nothing, writes no value and
  freezes nothing; there is no option that sends another kind;
- the session must be connected and its prepared and reported identities must
  agree, as the panel requires, and every acknowledgement must come from that
  same session and name the record it was asked about, or it refuses;
- it is bounded: at most 64 records a call, and the acknowledgement wait is the
  panel's own, 96 polls 125 ms apart.

What it changes: like the panel's own refresh, the queries are appended to the
session's control log and advance its generation. A press in the panel at the
same moment can claim the same generation, and the backend refuses whichever is
second, so run it while nobody is pressing anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import host_platform  # noqa: E402
from target_plugin_install import DEFAULT_DECKY_URL, DeckyWebSocketClosed  # noqa: E402
import target_plugin_rpc  # noqa: E402

MAX_RECORDS = 64
ACK_POLL_ATTEMPTS = 96
ACK_POLL_INTERVAL_S = 0.125

Rpc = Callable[[str, list[object]], Any]


class QueryRefused(RuntimeError):
    """The session could not answer, said in the terms the refusal turned on."""


def _identity(envelope: dict[str, Any]) -> tuple[object, ...]:
    prepared = envelope.get("prepared") or {}
    return tuple(prepared.get(key) for key in ("session_id", "table_sha256", "ce_sha256", "descriptor_sha256"))


def _require_connected(envelope: dict[str, Any], app_id: int) -> None:
    prepared, status = envelope.get("prepared"), envelope.get("status")
    if not envelope.get("connected") or not prepared or not status:
        raise QueryRefused(envelope.get("session_state_reason") or "the bridge is not connected for this AppID")
    if not envelope.get("session_current") or prepared.get("app_id") != app_id or status.get("app_id") != app_id:
        raise QueryRefused("the runtime session is stale or belongs to another AppID")
    for key in ("session_id", "table_sha256", "ce_sha256", "descriptor_sha256"):
        if prepared.get(key) != status.get(key):
            raise QueryRefused(f"the bridge's {key} does not match the prepared session")


def query(app_id: int, record_ids: list[int], rpc: Rpc, sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
    if not record_ids or len(record_ids) > MAX_RECORDS or len(set(record_ids)) != len(record_ids):
        raise QueryRefused(f"give between 1 and {MAX_RECORDS} distinct record IDs")
    before = rpc("get_runtime_status", [app_id])
    _require_connected(before, app_id)
    start = before.get("next_generation")
    if isinstance(start, bool) or not isinstance(start, int):
        raise QueryRefused(before.get("session_state_reason") or "the session's control log cannot be read")
    commands = [{"generation": start + index, "kind": "query", "record_id": record_id} for index, record_id in enumerate(record_ids)]
    receipt = rpc("write_runtime_commands", [app_id, commands])
    if not isinstance(receipt, dict) or not receipt.get("ok") or receipt.get("count") != len(commands) \
            or receipt.get("next_generation") != start + len(commands):
        raise QueryRefused(f"the backend's write receipt does not match the queries: {receipt!r}"[:400])
    for attempt in range(ACK_POLL_ATTEMPTS):
        envelope = rpc("get_runtime_status", [app_id])
        if _identity(envelope) != _identity(before):
            raise QueryRefused("the session changed while the queries were outstanding")
        by_generation = {item.get("generation"): item for item in (envelope.get("status") or {}).get("results") or []}
        if all(command["generation"] in by_generation for command in commands):
            results = []
            for command in commands:
                result = by_generation[command["generation"]]
                if result.get("record_id") != command["record_id"]:
                    raise QueryRefused(f"generation {command['generation']} was acknowledged for another record")
                results.append({key: result.get(key) for key in ("record_id", "ok", "active", "value", "error_code", "error")})
            prepared = before["prepared"]
            return {
                "app_id": app_id,
                "session_id": prepared.get("session_id"),
                "table_sha256": prepared.get("table_sha256"),
                "results": results,
            }
        if attempt + 1 < ACK_POLL_ATTEMPTS:
            sleep(ACK_POLL_INTERVAL_S)
    raise QueryRefused(f"generations {start}-{start + len(commands) - 1} were not acknowledged in time")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--appid", type=int, required=True, help="exact AppID of the game the session is for")
    parser.add_argument("--record", type=int, action="append", required=True, help="a MemoryRecord ID; repeat for more")
    parser.add_argument("--decky-url", default=DEFAULT_DECKY_URL)
    parser.add_argument("--timeout", type=float, default=30.0, help="per RPC, in seconds")
    parsed = parser.parse_args(argv)
    try:
        host_platform.require("The live runtime query helper", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if parsed.timeout <= 0 or parsed.timeout > 300:
        print("target runtime query: --timeout must be greater than zero and at most 300 seconds", file=sys.stderr)
        return 2
    url = parsed.decky_url.rstrip("/")

    def rpc(method: str, args: list[object]) -> Any:
        return target_plugin_rpc.call(method, args, url, parsed.timeout)

    try:
        report = query(parsed.appid, parsed.record, rpc)
    except QueryRefused as exc:
        print(f"target runtime query: {exc}", file=sys.stderr)
        return 2
    except (OSError, DeckyWebSocketClosed, TimeoutError, RuntimeError, ValueError) as exc:
        print(f"target runtime query: {exc}", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
