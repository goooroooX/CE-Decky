#!/usr/bin/env python3
"""Switch a game to one exact table under test and start Cheat Engine for it.

An explicitly mutating development helper for an agreed device test, such as
a table repair. The live plugin only changes the selected table while it owns
no Cheat Engine for the game, `associate_table` adds a table to the game's
library without selecting it, and consent is bound to the selected SHA, so the
order is fixed and was being rediscovered by hand on every switch:

0. before anything is stopped, the destination is proved: `inspect_table_sha`
   reads the exact stored file, `list_blocked_tables` says whether it is marked
   as not working, and where Cheat Engine is running or `--consent` is given,
   `get_ce_launch_capability` has to observe the game's Proton build. A file
   the parser cannot read is recorded as not working by that inspection, as it
   is whenever the panel shows it;
1. if `get_ce_launch_capability` names this game as owning a Cheat Engine,
   recovered or not, or a bridge is connected, `stop_ce_for_game` with
   Auto-load held, which quiesces the active records before Cheat Engine
   exits; anything short of a proven stop with every record confirmed put
   down ends the switch before the profile is touched. Ownership that cannot
   be read stops nothing;
2. `save_profile` with the game's own name, shortcut flag and target process,
   which selects the table;
3. with `--consent` only, `set_execution_consent` for that exact SHA;
4. with consent in place, `launch_ce_for_game` as a press, which lifts the
   Auto-load hold, through the Proton build the backend observes the game
   running under, then `get_runtime_status` until the bridge reports this
   exact table loaded and attached. Without consent, or after a step that
   failed once Cheat Engine was stopped, the hold stays, so nothing starts a
   table by itself until a launch is pressed.

Every step is the production RPC, and each answer is in the report. A failed
or unconfirmed step stops the switch and says which one, and nothing is
retried: a stop or launch that timed out may still have happened. Remembered
cheats, pins and values are never copied from another SHA, and no record is
switched on; `target_runtime_control.py` does that afterwards.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import time

if __package__:
    from . import host_platform, target_plugin_rpc
else:
    import host_platform
    import target_plugin_rpc

MODULE_ROOT = Path(__file__).resolve().parents[1] / "py_modules"
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from ce_decky.table_blocklist import COMPATIBILITY_CAUSES  # noqa: E402


class SwitchRefused(RuntimeError):
    pass


#: How long the bridge gets to report the new table loaded and attached,
#: polled every `WAIT_INTERVAL_S`: two minutes, and running past it is reported
#: as Cheat Engine possibly still starting rather than as a failure.
WAIT_ATTEMPTS = 60
WAIT_INTERVAL_S = 2.0


def _profile(rpc, app_id):
    profiles = rpc("list_profiles", [])
    matches = [profile for profile in profiles or [] if isinstance(profile, dict) and profile.get("app_id") == app_id]
    if len(matches) != 1:
        raise SwitchRefused(f"no single game profile for AppID {app_id}")
    return matches[0]


def switch(app_id, table_sha, rpc, *, consent=False, sleep=time.sleep, steps=None):
    if type(app_id) is not int or not 0 < app_id <= 0xFFFFFFFF:
        raise SwitchRefused("give the exact AppID")
    if not isinstance(table_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", table_sha):
        raise SwitchRefused("give the exact table SHA-256")
    steps = [] if steps is None else steps
    profile = _profile(rpc, app_id)
    if not profile.get("target_process"):
        raise SwitchRefused("the game profile names no target process; set it in the panel first")

    # The destination is proved before anything running is stopped: a table
    # that is missing, damaged, unreadable or marked as not working would
    # otherwise end a working Cheat Engine and then fail to be selected.
    # `save_profile` still makes the same checks when it selects it.
    try:
        rpc("inspect_table_sha", [table_sha, app_id])
    except RuntimeError as exc:
        raise SwitchRefused(f"the destination table cannot be used: {exc}") from exc
    if profile.get("table_sha256") != table_sha:
        blocked = rpc("list_blocked_tables", []) or {}
        if any(isinstance(row, dict) and row.get("sha256") == table_sha and row.get("cause") in COMPATIBILITY_CAUSES
               for row in blocked.get("tables") or []):
            raise SwitchRefused("the destination table is marked as not working; clear that mark first")

    runtime = rpc("get_runtime_status", [app_id]) or {}
    current = (runtime.get("prepared") or {}).get("table_sha256")
    # Ownership is the launcher's answer, which also covers a Cheat Engine
    # recovered from its record after a reload and not connected to a bridge.
    capability = rpc("get_ce_launch_capability", [app_id]) or {}
    if capability.get("ownership_state_error"):
        raise SwitchRefused(f"Cheat Engine ownership cannot be read ({capability['ownership_state_error']})")
    owners = [owner for owner in capability.get("owned_launch_owners") or []
              if isinstance(owner, dict) and owner.get("app_id") == app_id]
    if any(owner.get("state") in {"invalid", "unreadable"} for owner in owners):
        raise SwitchRefused("this game's Cheat Engine ownership record cannot be trusted; repair it in Advanced first")
    running = bool(owners or runtime.get("connected"))
    tool = capability.get("observed_proton_tool") or {}
    if (running or consent) and not tool.get("tool_id"):
        raise SwitchRefused("the backend observes no Proton build for the running game, so nothing was stopped")
    if running:
        # Auto-load is held, or an open panel can start the old table again
        # between this stop and the selection; the explicit launch lifts it.
        stopped = rpc("stop_ce_for_game", [app_id, current, True]) or {}
        quiesce = stopped.get("quiesce")
        steps.append({"step": "stop", "table_sha256": current, "stopped": stopped.get("stopped"),
                      "recovered": stopped.get("recovered"),
                      "state": (stopped.get("operation") or {}).get("state"), "quiesce": quiesce})
        if stopped.get("stopped") is not True:
            raise SwitchRefused("Cheat Engine was not proven stopped; inspect it before switching")
        if (not isinstance(quiesce, dict) or quiesce.get("cleanup_confirmed") is not True
                or quiesce.get("records_unsettled")):
            raise SwitchRefused("the stop did not confirm that every active record was put down, so the game "
                                "is held from every start until it exits; nothing was selected")

    selected = rpc("save_profile", [app_id, profile["name"], bool(profile.get("is_shortcut")), table_sha,
                                    profile["target_process"]])
    steps.append({"step": "select", "table_sha256": (selected or {}).get("table_sha256")})
    if (selected or {}).get("table_sha256") != table_sha:
        raise SwitchRefused("the profile did not select this exact table")

    if consent:
        granted = rpc("set_execution_consent", [app_id, table_sha, True])
        steps.append({"step": "consent", "execution_consent_sha256": (granted or {}).get("execution_consent_sha256")})
        selected = granted
    if (selected or {}).get("execution_consent_sha256") != table_sha:
        steps.append({"step": "launch", "skipped": "this exact table has no execution consent"})
        return {"schema": 1, "app_id": app_id, "table_sha256": table_sha, "ok": True, "launched": False, "steps": steps}

    if not tool.get("tool_id"):
        raise SwitchRefused("the backend observes no Proton build for the running game")
    launch = rpc("launch_ce_for_game", [app_id, tool["tool_id"], False])
    steps.append({"step": "launch", "tool": tool.get("name"), "session_id": (launch or {}).get("session_id"),
                  "state": (launch or {}).get("state"), "error": (launch or {}).get("error")})
    if (launch or {}).get("error"):
        raise SwitchRefused(f"the launch was refused: {launch['error']}")

    for attempt in range(WAIT_ATTEMPTS):
        envelope = rpc("get_runtime_status", [app_id]) or {}
        status = envelope.get("status") or {}
        if status.get("table_load_state") == "failed":
            raise SwitchRefused(f"Cheat Engine could not load the table: {status.get('table_load_error')}")
        if (envelope.get("connected") and status.get("attached") is True
                and status.get("table_load_state") == "loaded" and status.get("table_sha256") == table_sha):
            steps.append({"step": "connected", "session_id": status.get("session_id"),
                          "opened_process_id": status.get("opened_process_id")})
            return {"schema": 1, "app_id": app_id, "table_sha256": table_sha, "ok": True, "launched": True,
                    "session_id": status.get("session_id"), "steps": steps}
        if attempt + 1 < WAIT_ATTEMPTS:
            sleep(WAIT_INTERVAL_S)
    raise SwitchRefused("the bridge did not report this table loaded and attached in time; Cheat Engine may still be starting")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--appid", type=int, required=True)
    parser.add_argument("--table-sha", required=True)
    parser.add_argument("--consent", action="store_true",
                        help="grant execution consent to this exact SHA; only inside an agreed device test")
    parser.add_argument("--out", type=Path, help="also write the report to this new file")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The table switch helper", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    steps: list[dict[str, object]] = []
    stream = None
    try:
        if args.out:
            stream = args.out.open("x", encoding="utf-8")
        report = switch(args.appid, args.table_sha,
                        lambda method, values: target_plugin_rpc.call(method, values, target_plugin_rpc.DEFAULT_DECKY_URL, 60),
                        consent=args.consent, steps=steps)
        code = 0
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        report = {"schema": 1, "app_id": args.appid, "table_sha256": args.table_sha, "ok": False,
                  "error": str(exc), "steps": steps}
        print(f"table switch: {exc}", file=sys.stderr)
        code = 2
    encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if stream:
        stream.write(encoded)
        stream.close()
    print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
