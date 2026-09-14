#!/usr/bin/env python3
"""Report the box Steam's Game Mode UI gives this plugin to lay out in.

The same build is read on a handheld and on a television, and those are not the
same screen in the way that matters here. Game Mode does not hand the plugin
physical pixels: Steam scales its whole UI to the output and the frontend sees
CSS pixels, so what decides whether a screen fits is that logical viewport and
nothing else. A 4K panel and a 1280x800 one can differ by hundreds of CSS pixels
of height, or by none at all, and which of the two it is cannot be deduced from
the resolution, the device or a screenshot: a capture is in output pixels, and
the scale between the two is exactly the unknown.

So it is read from the pages themselves, over the CEF debugging endpoint Steam
already exposes, read-only. For each page this evaluates one expression that
only measures: the viewport, the device pixel ratio, and, when the plugin has
something on screen, the height its own quick access panel and any open modal
sheet actually occupy against that viewport. Nothing is clicked, nothing is
injected and no page state is changed.

The last part is what makes a layout complaint into a number. "The modal does
not fit on the Deck" is a report; `sheet=1084 viewport=800` is the same report
with the amount named, and it is what says whether a fix has to remove 284 CSS
pixels or 40.

Run it on the device whose screen is in question. Run it while the screen being
measured is the one on the display: a modal that is not open has no height, and
this says so rather than reporting a zero.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from . import host_platform
    from .target_ui_freeze_probe import DevToolsSocket, ProbeError
else:
    import host_platform
    from target_ui_freeze_probe import DevToolsSocket, ProbeError

SCHEMA = 1
DEFAULT_ENDPOINT = "http://localhost:8080"
DEFAULT_TIMEOUT = 5.0
MAX_PAGES = 64
#: Steam's own page titles. The quick access page is where a Decky plugin's
#: panel lives, so it leads; the rest are reported because a modal a plugin
#: opens is drawn by the main menu page rather than by the panel that opened it.
INTERESTING_TITLES = ("QuickAccess", "MainMenu", "SP", "Steam Big Picture Mode")

#: One expression, evaluated in each page, that reads geometry and nothing else.
#:
#: `ce-decky-dense` is this plugin's own wrapper, so its rect is the panel's real
#: height rather than the section heights added up. A modal sheet is Steam's own
#: element and carries no class this plugin controls, so it is found as the
#: tallest positioned block that is neither the document nor the backdrop, which
#: is what a sheet is; when nothing is open there is none and the field is null
#: rather than zero.
MEASURE_EXPRESSION = """
(() => {
  const rect = (node) => {
    if (!node) return null;
    const box = node.getBoundingClientRect();
    return { top: Math.round(box.top), height: Math.round(box.height), width: Math.round(box.width) };
  };
  const panel = document.querySelector('.ce-decky-dense');
  // Text this plugin asked to be cut to one line, that actually is cut. A line
  // the reader cannot finish where it stands is the whole reason a row of pure
  // state has to be reachable, so this is what says which rows need a press and
  // which only look like they might.
  const clipped = [];
  for (const node of document.querySelectorAll('.ce-decky-ellipsis')) {
    const over = node.scrollWidth - node.clientWidth;
    if (over <= 0) continue;
    clipped.push({ text: (node.textContent || '').slice(0, 120), overflow: over, width: node.clientWidth });
  }
  let sheet = null;
  for (const node of document.querySelectorAll('div')) {
    const style = getComputedStyle(node);
    if (style.position !== 'fixed' && style.position !== 'absolute') continue;
    const box = node.getBoundingClientRect();
    if (box.width < 200 || box.width > window.innerWidth * 0.95) continue;
    if (box.height < 120) continue;
    if (sheet === null || box.height > sheet.height) sheet = rect(node);
  }
  return {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    client: { width: document.documentElement.clientWidth, height: document.documentElement.clientHeight },
    device_pixel_ratio: window.devicePixelRatio,
    screen: { width: window.screen.width, height: window.screen.height },
    plugin_panel: rect(panel),
    clipped_text: clipped.slice(0, 12),
    clipped_count: clipped.length,
    tallest_sheet: sheet,
    plugin_present: panel !== null,
  };
})()
"""


def pages(endpoint: str, timeout: float) -> list[dict[str, object]]:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/list", timeout=timeout) as response:
            payload = json.loads(response.read(2 * 1024 * 1024).decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ProbeError(f"Steam CEF endpoint {endpoint} did not answer: {exc}") from exc
    if not isinstance(payload, list):
        raise ProbeError("Steam CEF endpoint returned an unexpected payload")
    found = [page for page in payload[:MAX_PAGES] if isinstance(page, dict)]
    ranked = sorted(found, key=lambda page: (
        next((index for index, title in enumerate(INTERESTING_TITLES)
              if str(page.get("title", "")).startswith(title)), len(INTERESTING_TITLES)),
        str(page.get("title", "")),
    ))
    return ranked


def measure(page: dict[str, object], timeout: float) -> dict[str, object]:
    url = page.get("webSocketDebuggerUrl")
    title = str(page.get("title", ""))
    if not isinstance(url, str):
        return {"page": title, "ok": False, "reason": "page exposes no debugger URL"}
    deadline = time.monotonic() + timeout
    connection = None
    try:
        connection = DevToolsSocket(url, timeout)
        request = connection.send("Runtime.evaluate", {
            "expression": MEASURE_EXPRESSION,
            "returnByValue": True,
            "awaitPromise": False,
        })
        reply = connection.await_reply(request, deadline)
    except TimeoutError:
        return {"page": title, "ok": False, "reason": f"page did not answer within {timeout:g}s"}
    except (ProbeError, OSError) as exc:
        return {"page": title, "ok": False, "reason": str(exc)}
    finally:
        if connection is not None:
            connection.close()
    result = reply.get("result", {})
    if not isinstance(result, dict) or "result" not in result:
        return {"page": title, "ok": False, "reason": "the page returned no measurement"}
    value = result["result"].get("value") if isinstance(result["result"], dict) else None
    if not isinstance(value, dict):
        return {"page": title, "ok": False, "reason": "the page returned an unreadable measurement"}
    return {"page": title, "ok": True, **value}


def summarize(report: dict[str, object]) -> str:
    if not report.get("ok"):
        return f"{report.get('page')}: {report.get('reason')}"
    viewport = report.get("viewport") or {}
    parts = [
        f"{report.get('page')}: viewport={viewport.get('width')}x{viewport.get('height')} css px",
        f"dpr={report.get('device_pixel_ratio')}",
    ]
    panel = report.get("plugin_panel")
    if isinstance(panel, dict):
        parts.append(f"plugin-panel={panel.get('width')}x{panel.get('height')}@{panel.get('top')}")
    sheet = report.get("tallest_sheet")
    if isinstance(sheet, dict):
        parts.append(f"tallest-sheet={sheet.get('width')}x{sheet.get('height')}@{sheet.get('top')}")
    clipped = report.get("clipped_text")
    if isinstance(clipped, list) and clipped:
        parts.append(f"clipped={report.get('clipped_count')}")
        for entry in clipped:
            if isinstance(entry, dict):
                parts.append(f"\n    cut by {entry.get('overflow')}px: {entry.get('text')}")
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Steam CEF debugging endpoint")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-page deadline in seconds")
    parser.add_argument("--json", action="store_true", help="print the whole report instead of one line per page")
    args = parser.parse_args(argv)
    try:
        host_platform.require("The Game Mode layout probe", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)

    if not 0.5 <= args.timeout <= 60:
        parser.error("--timeout must be between 0.5 and 60 seconds")
    try:
        found = pages(args.endpoint, args.timeout)
    except ProbeError as exc:
        print(f"layout probe failed: {exc}", file=sys.stderr)
        return 1
    reports = [measure(page, args.timeout) for page in found]
    if args.json:
        json.dump({"schema": SCHEMA, "host": host_platform.describe().describe(), "pages": reports},
                  sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(host_platform.describe().describe())
        for report in reports:
            print(summarize(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
