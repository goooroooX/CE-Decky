#!/usr/bin/env python3
"""What Steam's own JavaScript says this device's library holds.

The panel's game list is built from `SteamClient.InstallFolder.GetInstallFolders`
and from `appStore.allApps`, and what those actually return on a device is not a
question memory may answer: a title installed on another machine turning up in
the picker is exactly the kind of thing that is argued about and never measured.
This reads them, read only, and prints what is there including the fields this
plugin does not use, because the field that settles such a question is usually
one nobody had typed yet.

It asks a named question rather than taking JavaScript from the caller. Each one
is a `JSON.stringify` over a Steam API this plugin already depends on, evaluated
in the page Steam runs that API in, with no argument interpolated into it:

- `install-folders`: every install folder and the apps it reports, with each
  app's whole record. This is the list the picker's Steam entries come from.
- `library-apps`: `appStore.allApps` counted by `app_type`, with the non-Steam
  shortcuts listed. This is where the picker's shortcut entries come from.
- `app-details`: what `appStore` holds for one AppID, for a title that is in the
  list and should not be.

Compare the answer with the device's own `steamapps/appmanifest_*.acf` files,
which are what an install actually is. A disagreement between the two is the
finding; this helper does not decide which is right.

    python3 scripts/target_steam_library_probe.py install-folders
    python3 scripts/target_steam_library_probe.py library-apps --json
    python3 scripts/target_steam_library_probe.py app-details --app-id 220

It never injects input, never clicks, and evaluates nothing a page can observe.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import host_platform
from target_ui_freeze_probe import (  # the one DevTools client, not a second copy of it
    DEFAULT_ENDPOINT,
    DevToolsSocket,
    ProbeError,
    _pages,
)

# The page Steam runs its own stores and clients in. The quick access panel and
# the library render elsewhere and do not hold these APIs.
CONTEXT_TITLE = "SharedJSContext"
# What one answer may carry back. A library of a few thousand titles is well
# inside this; a machine that exceeds it gets a refusal rather than a truncated
# answer read as a complete one.
MAX_RESULT_BYTES = 8 * 1024 * 1024

QUESTIONS: dict[str, str] = {
    # Every field of every app record, so the one that says "installed" is in
    # the answer whether or not this plugin knows its name yet.
    # The stringify happens inside the async function: stringifying the promise
    # itself answers `{}`, which reads exactly like a device with no libraries.
    "install-folders": """
        (async () => {
          const folders = await SteamClient.InstallFolder.GetInstallFolders();
          return JSON.stringify((folders ?? []).map((folder) => ({
            label: folder.strFolderPath ?? folder.strDriveName ?? null,
            is_mounted: folder.bIsMounted ?? null,
            apps: (folder.vecApps ?? []).map((app) => ({ ...app })),
          })));
        })()
    """,
    "library-apps": """
        JSON.stringify((() => {
          const apps = (window.appStore && window.appStore.allApps) || [];
          const byType = {};
          for (const app of apps) {
            const key = String(app.app_type);
            byType[key] = (byType[key] ?? 0) + 1;
          }
          const shortcut = 1 << 30;
          return {
            total: apps.length,
            by_app_type: byType,
            shortcuts: apps.filter((app) => app.app_type === shortcut)
              .map((app) => ({ appid: app.appid, display_name: app.display_name })),
          };
        })())
    """,
    "app-details": """
        JSON.stringify((() => {
          const overview = window.appStore && window.appStore.GetAppOverviewByAppID
            ? window.appStore.GetAppOverviewByAppID(APP_ID)
            : null;
          if (!overview) return null;
          const record = {};
          for (const key of Object.keys(overview)) {
            const value = overview[key];
            if (value === null || ["string", "number", "boolean"].includes(typeof value)) record[key] = value;
          }
          return record;
        })())
    """,
}


def _evaluate(endpoint: str, expression: str, timeout: float) -> Any:
    """Evaluate one expression in Steam's shared context and return its value."""
    pages = _pages(endpoint, timeout)
    shared = [page for page in pages if str(page.get("title", "")).startswith(CONTEXT_TITLE)]
    if not shared:
        titles = ", ".join(sorted({str(page.get("title", "")) for page in pages})) or "none"
        raise ProbeError(f"Steam is not running a {CONTEXT_TITLE} page; it offered: {titles}")
    url = str(shared[0].get("webSocketDebuggerUrl", ""))
    connection = DevToolsSocket(url, timeout)
    try:
        request = connection.send("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "timeout": int(timeout * 1000),
        })
        reply = connection.await_reply(request, time.monotonic() + timeout)
    finally:
        connection.close()
    result = (reply.get("result") or {}).get("result") or {}
    if (reply.get("result") or {}).get("exceptionDetails"):
        detail = json.dumps(reply["result"]["exceptionDetails"])[:400]
        raise ProbeError(f"Steam refused the question: {detail}")
    value = result.get("value")
    if not isinstance(value, str):
        raise ProbeError(f"Steam answered with {result.get('type')!r} rather than the JSON asked for")
    if len(value) > MAX_RESULT_BYTES:
        raise ProbeError("Steam's answer exceeded the byte budget this reads")
    try:
        return json.loads(value)
    except ValueError as exc:
        raise ProbeError(f"Steam's answer was not readable JSON: {exc}") from exc


def _summarise(question: str, answer: Any) -> list[str]:
    lines: list[str] = []
    if question == "install-folders" and isinstance(answer, list):
        for folder in answer:
            apps = folder.get("apps") or []
            lines.append(f"{folder.get('label')}: {len(apps)} app(s), mounted={folder.get('is_mounted')}")
            fields = sorted({key for app in apps for key in app})
            lines.append(f"  fields on an app record: {', '.join(fields) or 'none'}")
            for app in apps:
                name = app.get("strAppName") or app.get("strName") or "?"
                lines.append(f"  {app.get('nAppID')} {name}")
        return lines
    if question == "library-apps" and isinstance(answer, dict):
        lines.append(f"{answer.get('total')} app(s) in appStore, by app_type: {answer.get('by_app_type')}")
        for shortcut in answer.get("shortcuts") or []:
            lines.append(f"  shortcut {shortcut.get('appid')} {shortcut.get('display_name')}")
        return lines
    lines.append(json.dumps(answer, indent=2, sort_keys=True)[:4000])
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", choices=sorted(QUESTIONS), help="which read-only question to ask Steam")
    parser.add_argument("--app-id", type=int, default=0, help="the AppID, for the questions that take one")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Steam's CEF debugging endpoint")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds this will not run past")
    parser.add_argument("--json", action="store_true", help="print Steam's whole answer as JSON")
    args = parser.parse_args()
    # Steam's own debugging endpoint is what this needs, not a process table,
    # which is the same requirement the other two CEF probes declare.
    host_platform.require("The Steam library probe", needs_procfs=False)
    if args.timeout < 1 or args.timeout > 120:
        parser.error("--timeout must be between 1 and 120 seconds")

    expression = QUESTIONS[args.question]
    if "APP_ID" in expression:
        if not 1 <= args.app_id <= 0xFFFFFFFF:
            parser.error(f"{args.question} needs --app-id")
        # The only value that reaches the page, and it is an integer this has
        # already bounded rather than text from the caller.
        expression = expression.replace("APP_ID", str(int(args.app_id)))

    try:
        answer = _evaluate(args.endpoint, expression, args.timeout)
    except (ProbeError, TimeoutError, OSError) as exc:
        print(f"steam library probe: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"schema": 1, "question": args.question, "answer": answer}, indent=2, sort_keys=True))
    else:
        for line in _summarise(args.question, answer):
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
