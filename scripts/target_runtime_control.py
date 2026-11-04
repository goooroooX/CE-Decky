#!/usr/bin/env python3
"""Mutate exact table records through production, wait for ACK and read back.

This is an explicitly mutating development helper. Supply the AppID, exact
selected table SHA and a JSON array of set_active/set_value commands. It never
evaluates Lua, reads arbitrary addresses or copies profile/consent state. Every
accepted batch is awaited before the read-only query batch is issued, and all
results remain bound to the original session. Failed or partial operations are
reported, never automatically retried or rolled back. See docs/TABLE_REPAIR.md.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

if __package__:
    from . import host_platform, target_plugin_rpc, target_runtime_query
else:
    import host_platform
    import target_plugin_rpc
    import target_runtime_query

Refused = target_runtime_query.QueryRefused


def validate_actions(actions):
    if not isinstance(actions, list) or not 1 <= len(actions) <= target_runtime_query.MAX_RECORDS:
        raise Refused("give 1 to 64 exact record mutations")
    seen = set()
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"record_id", "kind", "value"}:
            raise Refused("each mutation needs record_id, kind and value")
        record_id = action["record_id"]
        if type(record_id) is not int or not 0 <= record_id <= 0x7FFFFFFF or record_id in seen:
            raise Refused("record IDs must be supported and distinct")
        seen.add(record_id)
        kind, value = action["kind"], action["value"]
        if not isinstance(value, str) or not value or "\0" in value or len(value.encode("utf-8")) > 4096:
            raise Refused("mutation values must be bounded non-empty text")
        if kind not in ("set_active", "set_value") or kind == "set_active" and value not in ("0", "1"):
            raise Refused("only explicit set_active 0/1 and set_value mutations are supported")


def control(app_id, table_sha, actions, rpc, *, sleep=time.sleep, progress=None):
    validate_actions(actions)
    if not isinstance(table_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", table_sha):
        raise Refused("give the exact selected table SHA-256")
    before = rpc("get_runtime_status", [app_id])
    target_runtime_query._require_connected(before, app_id)
    identity = target_runtime_query._identity(before)
    if before["prepared"]["table_sha256"] != table_sha:
        raise Refused("selected session table SHA differs from the authorized test")
    status = before["status"]
    if status.get("attached") is not True or type(status.get("opened_process_id")) is not int or status["opened_process_id"] <= 0:
        raise Refused("the exact session is not attached to a positive target PID")
    start = before.get("next_generation")
    if type(start) is not int or start < 1:
        raise Refused("the current control generation is unavailable")
    commands = [{**action, "generation": start + i} for i, action in enumerate(actions)]
    if progress is not None:
        progress.update(phase="mutation_submission", mutation_may_have_happened=True,
                        session_id=before["prepared"]["session_id"], commands=commands)
    receipt = rpc("write_runtime_commands", [app_id, commands])
    if (not isinstance(receipt, dict) or receipt.get("ok") is not True or receipt.get("count") != len(commands)
            or receipt.get("next_generation") != start + len(commands)):
        raise Refused("the backend did not acknowledge this exact mutation batch receipt")
    if progress is not None:
        progress["phase"] = "awaiting_mutation_ack"

    def require_same_target(envelope, *, require_fresh=True):
        if target_runtime_query._identity(envelope) != identity:
            raise Refused("session changed after mutation; inspect the preserved runtime log before further work")
        if require_fresh:
            target_runtime_query._require_connected(envelope, app_id)
        current = envelope.get("status") or {}
        if (not envelope.get("session_current") or current.get("app_id") != app_id
                or any(current.get(key) != status.get(key)
                       for key in ("session_id", "table_sha256", "ce_sha256", "descriptor_sha256"))):
            raise Refused("reported bridge identity changed while awaiting mutation")
        if current.get("attached") is not True or current.get("opened_process_id") != status["opened_process_id"]:
            raise Refused("attached target changed after mutation; do not infer effect from this readback")

    acknowledged = None
    by_generation = {}
    wanted = {command["generation"]: command for command in commands}
    for attempt in range(target_runtime_query.ACK_POLL_ATTEMPTS):
        envelope = rpc("get_runtime_status", [app_id])
        require_same_target(envelope, require_fresh=False)
        results = (envelope.get("status") or {}).get("results") or []
        seen_in_sample = set()
        for result in results:
            generation = result.get("generation")
            if generation not in wanted:
                continue
            if generation in seen_in_sample or result.get("record_id") != wanted[generation]["record_id"]:
                raise Refused("ambiguous or wrong-record mutation acknowledgement")
            seen_in_sample.add(generation)
            if generation in by_generation and by_generation[generation] != result:
                raise Refused("mutation acknowledgement changed between samples")
            by_generation[generation] = result
        if progress is not None:
            progress["observed_acknowledgements"] = [by_generation[c["generation"]] for c in commands
                                                      if c["generation"] in by_generation]
        if len(by_generation) == len(commands) and envelope.get("connected"):
            require_same_target(envelope)
            acknowledged = [by_generation[command["generation"]] for command in commands]
            break
        if attempt + 1 < target_runtime_query.ACK_POLL_ATTEMPTS:
            sleep(target_runtime_query.ACK_POLL_INTERVAL_S)
    if acknowledged is None:
        raise Refused("mutation ACK timed out; mutation may have happened, inspect its exact runtime log")
    ok = all(result.get("ok") is True for result in acknowledged)
    if progress is not None:
        progress.update(phase="mutation_acknowledged", acknowledged=acknowledged)
    readback = None
    if ok:
        def readback_rpc(method, args):
            answer = rpc(method, args)
            if method == "get_runtime_status":
                require_same_target(answer)
            return answer
        if progress is not None:
            progress["phase"] = "readback"
        readback = target_runtime_query.query(app_id, [action["record_id"] for action in actions], readback_rpc, sleep=sleep)
        if readback["session_id"] != before["prepared"]["session_id"] or readback["table_sha256"] != table_sha:
            raise Refused("the readback belongs to another session")
        ok = all(result.get("ok") is True for result in readback["results"])
        for action, result in zip(actions, readback["results"]):
            if action["kind"] == "set_active" and result.get("active") is not (action["value"] == "1"):
                ok = False
    return {"schema": 1, "app_id": app_id, "table_sha256": table_sha,
            "session_id": before["prepared"]["session_id"], "ok": ok,
            "commands": commands, "acknowledged": acknowledged, "readback": readback,
            "limits": "Exact command ACK and record readback; gameplay effect and restoration are separate tests."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--appid", type=int, required=True)
    parser.add_argument("--table-sha", required=True)
    parser.add_argument("--commands", required=True, help="JSON array; this executes mutations in the running game")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        host_platform.require("The exact runtime control helper", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    stream = None
    progress = {"phase": "preflight", "mutation_may_have_happened": False}
    try:
        if len(args.commands.encode("utf-8")) > 256 * 1024:
            raise Refused("command JSON exceeds the bound")
        actions = json.loads(args.commands)
        validate_actions(actions)
        if args.out:
            stream = args.out.open("x", encoding="utf-8")
        report = control(args.appid, args.table_sha, actions,
                         lambda method, values: target_plugin_rpc.call(method, values, target_plugin_rpc.DEFAULT_DECKY_URL, 15),
                         progress=progress)
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if stream:
            stream.write(encoded)
        print(encoded, end="")
        return 0 if report["ok"] else 1
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        report = {"ok": False, "error": str(exc), "app_id": args.appid, "table_sha256": args.table_sha, **progress}
        if stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        print(json.dumps(report, indent=2))
        print(f"runtime control: {exc}", file=sys.stderr)
        return 2
    finally:
        if stream:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
