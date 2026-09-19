#!/usr/bin/env python3
"""What CE Decky is showing right now, as text rather than as a picture.

A screenshot is the honest record of a frame and a poor way to answer "how many
rows fit", "is that button disabled", "what does the footer say". It costs a
megabyte and a model's whole attention to read six words off it, and a run that
checks a layout on two devices reads it a dozen times.

This reads the same thing out of the page instead. Every screen this plugin
draws is built from its own components, and those carry the markers it already
uses for its own tests: `data-testid` on a row, the dense wrapper's class on the
block, the marquee on a clipped line. So the panel and each of its modals can be
serialised as a short tree of rows, controls and the text actually rendered,
which is what a question about a layout is usually about.

    python3 scripts/target_panel_read.py --open          # open the panel, then read it
    python3 scripts/target_panel_read.py --open --press Manage --metrics
    python3 scripts/target_panel_read.py                 # every CE Decky surface
    python3 scripts/target_panel_read.py --testid manage-footer
    python3 scripts/target_panel_read.py --json          # the same, for a report
    python3 scripts/target_panel_read.py --metrics       # what each page measures

`--open` opens the quick access panel on this plugin's own page. It calls
Steam's own `MenuStore.OpenQuickAccessMenu`, through
`SteamUIStore.GetFocusedWindowInstance()`, which is the exact route `@decky/ui`
gives every plugin for the same purpose, and then Decky's own
`deckyState.setActivePlugin`.

`--press` then opens one of this plugin's own screens, by the name on the
control: `--press Manage`, `--press "Configure cheats"`, `--press "Look inside"`.
It finds that control inside this plugin's own DOM and activates it, so what
runs is the handler the plugin wrote for it.

Two limits make that safe to have here. The name has to be one of the presses
that opens a screen, listed in `NAVIGATION_PRESSES` below: anything that
authorizes, downloads, writes or destroys is not in that list and is refused by
name, so this cannot press **Use this table**, **Delete** or **Apply**. And a
control that is disabled is reported as disabled rather than activated, because
a press that Steam would have refused is not evidence about anything.

A name is matched against every surface this plugin is drawing, so a name that
means one thing here and another there is not in that list either: **Search**
opens the search screen from the panel and spends a provider request inside it,
**Cancel** leaves a dialog and stops a running Cheat Engine installation on the
panel. Those are in `SCOPED_PRESSES` instead, written `<data-testid>:<name>`,
which is the same name looked for only inside the one row the reader prints
that id for - identity by where the control is, rather than by what it says.

None of this fabricates controller input, and none of it reaches Steam's own
interface: it is this plugin's own buttons, the ones its component tests press
by the same names. So reaching one of this plugin's screens is a thing to do
rather than a thing to ask somebody for. What is outside the list - a running
game, Steam's own interface, and the controls that authorize, download, write
or destroy - is what still needs a person at the device.

Works the same against the machine it runs on and against a Steam Deck over
SSH, because it is the device's own CEF endpoint either way: run it on the
device, as with every other target helper.
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

# What one page may answer with. A panel is a few dozen rows; this is the bound
# that turns a page gone wrong into a refusal rather than a flood.
MAX_RESULT_BYTES = 2 * 1024 * 1024
# How deep into a row this walks looking for text and controls. A row is a label
# over a description with its controls beside it, and its own reveal under it.
MAX_DEPTH = 12

# One expression, evaluated in each page, returning this plugin's surfaces.
#
# `ce-decky-dense` is the wrapper every screen here draws inside, so it is what
# separates this plugin's DOM from the rest of Steam's. Within it, the rows are
# the elements carrying `data-testid`, which the components already set for the
# component tests.
_READ = """
(() => {
  const MAX_DEPTH = %(depth)d;
  const text = (node) => (node.textContent || "").replace(/\\s+/g, " ").trim();
  const controls = (row) => Array.from(row.querySelectorAll("button, input, select")).map((el) => {
    const tag = el.tagName.toLowerCase();
    const label = tag === "button" ? text(el) : (el.getAttribute("aria-label") || el.getAttribute("placeholder") || "");
    const item = { kind: tag, label };
    if (el.disabled) item.disabled = true;
    if (tag === "input" && el.type === "checkbox") item.checked = Boolean(el.checked);
    else if (tag === "input") {
      item.value = String(el.value ?? "");
      // What an empty box is showing. A control this plugin labels inside
      // itself has nothing above it to read, so the hint is the whole of what
      // the screen says about it, and whether Steam's own input took the
      // attribute at all is not answerable from the label above.
      const hint = el.getAttribute("placeholder");
      if (hint) item.placeholder = hint;
    }
    if (el === document.activeElement) item.focused = true;
    return item;
  });
  // How far down the page a window may actually be seen. Steam paints its own
  // bar along the bottom, over whatever is behind it, so a window that fits the
  // page is still cut when it reaches past this. Asked of the page rather than
  // assumed: whatever element is under the middle of the bottom edge, if it is
  // not one of this plugin's own, is what a window has to stay above.
  const under = document.elementFromPoint(Math.round(window.innerWidth / 2), window.innerHeight - 2);
  const bar = under && !under.closest(".ce-decky-dense") ? under.getBoundingClientRect() : null;
  const usable = bar && bar.top > window.innerHeight / 2 ? Math.round(bar.top) : window.innerHeight;
  const surfaces = [];
  for (const panel of Array.from(document.querySelectorAll(".ce-decky-dense"))) {
    const rows = [];
    for (const row of Array.from(panel.querySelectorAll("[data-testid]"))) {
      // A row nested inside another row is reported once, by the outer one.
      let depth = 0;
      for (let parent = row.parentElement; parent && parent !== panel; parent = parent.parentElement) {
        if (parent.hasAttribute("data-testid")) { depth = -1; break; }
        if (++depth > MAX_DEPTH) break;
      }
      if (depth < 0) continue;
      const box = row.getBoundingClientRect();
      rows.push({
        testid: row.dataset.testid,
        text: text(row).slice(0, 300),
        height: Math.round(box.height),
        controls: controls(row),
        clipped: row.querySelectorAll(".ce-decky-marquee[data-clipped]").length,
      });
    }
    // The window this surface is drawn in, rather than the surface itself.
    // What a modal here puts below its dense block - the footer, a refusal, the
    // prompt Cancel opens - is outside that block, so a verdict taken from the
    // block called a window that ran past the bottom of the display a fit, with
    // room to spare, while its footer was behind Steam's own bar. The window is
    // the outermost box that is still a box rather than the page: an overlay
    // covers the page, so the last ancestor narrower and shorter than the page
    // is the thing that has to fit in it.
    let frame = panel;
    for (let parent = panel.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      const outer = parent.getBoundingClientRect();
      if (outer.width >= window.innerWidth || outer.height >= window.innerHeight) break;
      frame = parent;
    }
    const box = frame.getBoundingClientRect();
    const inner = panel.getBoundingClientRect();
    // This plugin's own outermost box inside that window, taken as the dense
    // block's parent, because every screen here renders the block as the first
    // child of the one element it hangs its own ref on and puts the footer in
    // that same parent. Steam's padding is then the gap at each end, and the
    // one under a footer is the one a screen fitting itself has to leave and
    // cannot see.
    //
    // The gap at each end is Steam's only if that parent really is this
    // plugin's outermost box; a screen with a wrapper of its own above it would
    // have its own padding counted here as Steam's. Both ends are reported for
    // that reason: Steam's modal is symmetric, and two figures that differ are
    // the sign that one of them is not all Steam's.
    const ours = frame === panel ? inner : (panel.parentElement || panel).getBoundingClientRect();
    // Whether anything between this plugin's box and the page actually
    // scrolls, and by how much. A window that fits the page and still moves
    // under the thumb is a box inside it that is shorter than what it holds,
    // and nothing about the window's own height says which box or by how much.
    let scroll = null;
    for (let parent = panel.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      const hidden = parent.scrollHeight - parent.clientHeight;
      const holds = parent.clientHeight;
      if (holds > 1 && holds < window.innerHeight) {
        const rect = parent.getBoundingClientRect();
        const under = Math.round(rect.bottom - ours.bottom);
        // The outermost box that is still a box rather than the page, because
        // that is the one a window has to fit in. Overwritten rather than kept,
        // so an inner wrapper sized by its own content does not answer for it.
        scroll = { holds: Math.round(holds), top: Math.round(rect.top), bottom: Math.round(rect.bottom), under, by: Math.max(0, Math.round(hidden)) };
        if (hidden > 1) break;
      }
    }
    surfaces.push({
      rows,
      height: Math.round(box.height),
      top: Math.round(box.top),
      // Where this window ends against the page it is drawn in. A window that
      // runs past the bottom of a handheld is the failure this whole screen of
      // measurements is about, and it is one subtraction nobody should be doing
      // by hand every time.
      bottom: Math.round(box.bottom),
      panel_height: Math.round(inner.height),
      panel_top: Math.round(inner.top),
      padding_above: Math.round(ours.top - box.top),
      padding_below: Math.round(box.bottom - ours.bottom),
      scroll,
      page_height: usable,
      headings: Array.from(panel.querySelectorAll(".ce-decky-heading")).map((el) => text(el)).slice(0, 12),
      // What the panel draws that is not a row. The mascot is a switch the user
      // owns, so whether it is on the screen is a question about this panel
      // that rows alone cannot answer.
      images: Array.from(panel.querySelectorAll("img")).map((el) => ({
        alt: el.getAttribute("alt") || "",
        width: Math.round(el.getBoundingClientRect().width),
        height: Math.round(el.getBoundingClientRect().height),
      })),
    });
  }
  return JSON.stringify({
    page: { height: window.innerHeight, width: window.innerWidth, ratio: window.devicePixelRatio, usable },
    focused: document.activeElement ? text(document.activeElement).slice(0, 80) : null,
    surfaces,
  });
})()
"""


# Steam's own navigation, reached the way `@decky/ui` reaches it, plus Decky's
# own plugin selection. `999` is the tab Decky registers itself as, which is the
# value its `QuickAccessTab` enum carries.
# Closing is the pair of opening, and it exists for what a panel does across the
# two rather than for tidiness: this one re-reads the backend every time it comes
# back into view, so anything that changed while it was on screen is only picked
# up after it has actually gone away. Without a way to close it, no probe here
# can produce that transition, and a reader that can only ever open the panel
# reports a stale panel as the panel.
_CLOSE = """
(() => {
  const store = window.SteamUIStore;
  const win = store && store.GetFocusedWindowInstance ? store.GetFocusedWindowInstance() : null;
  const menus = win && win.MenuStore;
  if (!menus) {
    return JSON.stringify({ closed: false, reason: "this page has no Steam menu store to close" });
  }
  for (const name of ["CloseSideMenus", "HideSideMenus", "CloseQuickAccessMenu"]) {
    if (typeof menus[name] === "function") {
      menus[name]();
      return JSON.stringify({ closed: true, via: name });
    }
  }
  return JSON.stringify({ closed: false, reason: "this Steam build offers no menu close call this knows" });
})()
"""

_OPEN = """
(() => {
  const store = window.SteamUIStore;
  const win = store && store.GetFocusedWindowInstance ? store.GetFocusedWindowInstance() : null;
  if (!win || !win.MenuStore || typeof win.MenuStore.OpenQuickAccessMenu !== "function") {
    return JSON.stringify({ opened: false, reason: "this page has no Steam menu store to open" });
  }
  win.MenuStore.OpenQuickAccessMenu(999);
  const loader = window.DeckyPluginLoader;
  const plugins = loader && loader.plugins ? Object.values(loader.plugins) : [];
  const wanted = plugins.find((plugin) => String(plugin && plugin.name || "").toLowerCase().includes("ce decky"));
  if (!wanted) {
    return JSON.stringify({
      opened: true,
      selected: null,
      reason: "Decky is not holding a plugin whose name looks like this one",
      plugins: plugins.map((plugin) => String(plugin && plugin.name || "")).slice(0, 20),
    });
  }
  if (loader.deckyState && typeof loader.deckyState.setActivePlugin === "function") {
    loader.deckyState.setActivePlugin(wanted.name);
  }
  return JSON.stringify({ opened: true, selected: wanted.name });
})()
"""


# The presses this may make, which are the ones that open a screen and nothing
# else. Every other control on this plugin's surfaces either authorizes, spends
# a network, writes durable state or destroys something, and a helper that can
# reach those is one press away from doing it to a real device by accident.
NAVIGATION_PRESSES = (
    "Manage",
    "Configure cheats",
    "Advanced\u2026",
    "Look inside",
    "Debug",
    "Local file",
    # Reads what this plugin has put on disk and opens the report. It writes
    # nothing: the report is what offers deletion, and that is the press below.
    "Check",
    # Opens the screen that asks what to delete, and nothing else: the presses
    # that actually remove anything are "Delete these" and the OK in Steam's own
    # confirmation behind it, and neither is in this list or reachable from it.
    "Delete\u2026",
    "Back",
    "Back to the list",
    "Close",
    "\u2039 Previous",
    "Next \u203a",
)

# The presses whose name is not enough, and what makes them one press.
#
# A name is matched against every surface this plugin is drawing, so a label
# that means one thing on one screen and another elsewhere is not a name at all:
# **Search** opens the search screen on the panel and spends a provider request
# inside it, **Cancel** leaves a dialog and stops a Cheat Engine installation in
# progress on the panel. Those are named by the row they are in - the same
# `data-testid` the reader prints - so the control is identified by where it is
# rather than by what it happens to say.
SCOPED_PRESSES = (
    # The panel's own way into the search screen, which is the row the table
    # lives in rather than the button inside the screen it opens.
    "table-row:Search",
)

# Activates one of this plugin's own controls, found by the name on it. The
# allowlist is checked here as well as in the caller, so the expression sent to
# the page can only ever name one of them.
_PRESS = r"""
(() => {
  const wanted = %(label)s;
  const scope = %(scope)s;
  for (const panel of Array.from(document.querySelectorAll(".ce-decky-dense"))) {
    const within = scope === null ? panel : panel.querySelector('[data-testid="' + scope + '"]');
    if (within === null) continue;
    for (const button of Array.from(within.querySelectorAll("button"))) {
      const label = (button.textContent || "").replace(/\s+/g, " ").trim();
      if (label !== wanted) continue;
      if (button.disabled) return JSON.stringify({ pressed: false, reason: "that control is disabled right now" });
      button.click();
      return JSON.stringify({ pressed: true, label });
    }
  }
  return JSON.stringify({ pressed: false, reason: "no control with that name is on screen" });
})()
"""


def press(pages: list[dict[str, Any]], label: str, timeout: float) -> dict[str, Any]:
    """Activate one of this plugin's own navigation controls.

    `label` is either a name from `NAVIGATION_PRESSES` or one of the
    `SCOPED_PRESSES`, written `<data-testid>:<name>`, which is the same name
    looked for only inside that one row.
    """
    scope, _, name = label.rpartition(":") if label in SCOPED_PRESSES else ("", "", label)
    expression = _PRESS % {"label": json.dumps(name), "scope": json.dumps(scope or None)}
    last: dict[str, Any] = {"pressed": False, "reason": "no page answered"}
    for page in pages:
        url = str(page.get("webSocketDebuggerUrl", ""))
        if not url:
            continue
        connection = DevToolsSocket(url, timeout)
        try:
            request = connection.send("Runtime.evaluate", {
                "expression": expression, "returnByValue": True, "timeout": int(timeout * 1000),
            })
            reply = connection.await_reply(request, time.monotonic() + timeout)
        except (ProbeError, TimeoutError, OSError):
            continue
        finally:
            connection.close()
        value = ((reply.get("result") or {}).get("result") or {}).get("value")
        if not isinstance(value, str):
            continue
        try:
            answer = json.loads(value)
        except ValueError:
            continue
        if answer.get("pressed"):
            return answer
        last = answer
    return last


def open_panel(pages: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
    """Open the quick access panel on this plugin's page, through Steam's own API."""
    last: dict[str, Any] = {"opened": False, "reason": "no page answered"}
    for page in pages:
        url = str(page.get("webSocketDebuggerUrl", ""))
        if not url:
            continue
        connection = DevToolsSocket(url, timeout)
        try:
            request = connection.send("Runtime.evaluate", {
                "expression": _OPEN, "returnByValue": True, "timeout": int(timeout * 1000),
            })
            reply = connection.await_reply(request, time.monotonic() + timeout)
        except (ProbeError, TimeoutError, OSError):
            continue
        finally:
            connection.close()
        value = ((reply.get("result") or {}).get("result") or {}).get("value")
        if not isinstance(value, str):
            continue
        try:
            answer = json.loads(value)
        except ValueError:
            continue
        if answer.get("opened"):
            return answer
        last = answer
    return last


def close_panel(pages: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
    """Ask Steam to put the quick access menu away, through its own call."""
    last: dict[str, Any] = {"closed": False, "reason": "no page answered"}
    for page in pages:
        url = str(page.get("webSocketDebuggerUrl", ""))
        if not url:
            continue
        connection = DevToolsSocket(url, timeout)
        try:
            request = connection.send("Runtime.evaluate", {
                "expression": _CLOSE, "returnByValue": True, "timeout": int(timeout * 1000),
            })
            reply = connection.await_reply(request, time.monotonic() + timeout)
        except (ProbeError, TimeoutError, OSError):
            continue
        finally:
            connection.close()
        value = ((reply.get("result") or {}).get("result") or {}).get("value")
        if not isinstance(value, str):
            continue
        try:
            answer = json.loads(value)
        except ValueError:
            continue
        if answer.get("closed"):
            return answer
        last = answer
    return last


def read_page(endpoint: str, page: dict[str, Any], timeout: float) -> dict[str, Any] | None:
    """This plugin's surfaces in one page, or nothing where it draws none."""
    url = str(page.get("webSocketDebuggerUrl", ""))
    if not url:
        return None
    connection = DevToolsSocket(url, timeout)
    try:
        request = connection.send("Runtime.evaluate", {
            "expression": _READ % {"depth": MAX_DEPTH},
            "returnByValue": True,
            "timeout": int(timeout * 1000),
        })
        reply = connection.await_reply(request, time.monotonic() + timeout)
    finally:
        connection.close()
    outcome = reply.get("result") or {}
    # A page that threw is not a page with nothing on it, and reporting the two
    # the same way is how a broken read of a drawing panel came back as "CE
    # Decky is not drawing anything right now". That sentence sent a session
    # looking at the device for a fault that was in this file: the expression
    # read a `const` declared below the loop that read it, so every page holding
    # a surface threw and every page without one answered honestly.
    thrown = outcome.get("exceptionDetails")
    if thrown:
        described = (thrown.get("exception") or {}).get("description") or thrown.get("text") or "threw"
        raise ProbeError(f"reading {page.get('title', '')!r} failed: {str(described).splitlines()[0]}")
    result = outcome.get("result") or {}
    value = result.get("value")
    if not isinstance(value, str) or len(value) > MAX_RESULT_BYTES:
        return None
    try:
        answer = json.loads(value)
    except ValueError:
        return None
    if not answer.get("surfaces"):
        return None
    answer["title"] = str(page.get("title", ""))
    return answer


def _overflow(surface: dict[str, Any]) -> int:
    """How far past the bottom of its page this surface runs, or zero."""
    bottom = surface.get("bottom")
    page = surface.get("page_height")
    if not isinstance(bottom, int) or not isinstance(page, int) or page <= 1:
        return 0
    return max(0, bottom - page)


def _room(surface: dict[str, Any]) -> int:
    """How much of the page is left under this surface."""
    bottom = surface.get("bottom")
    page = surface.get("page_height")
    if not isinstance(bottom, int) or not isinstance(page, int) or page <= 1:
        return 0
    return max(0, page - bottom)


def _print(answer: dict[str, Any], testid: str | None, metrics: bool) -> None:
    page = answer.get("page") or {}
    usable = page.get("usable")
    strip = "" if usable in (None, page.get("height")) else f", usable to {usable} (Steam's own bar below that)"
    print(f"{answer.get('title')}: page {page.get('width')}x{page.get('height')} dpr {page.get('ratio')}{strip}")
    if answer.get("focused"):
        print(f"  ring on: {answer['focused']}")
    for surface in answer.get("surfaces", []):
        over = _overflow(surface)
        if metrics or over:
            fit = (f"runs {over}px past the bottom of the page" if over
                   else f"fits, with {_room(surface)}px to spare")
            inside = surface.get("panel_height")
            block = ""
            if isinstance(inside, int) and inside != surface.get("height"):
                # What Steam's own modal puts around this plugin's box, which is
                # the part of a window nothing here can shrink and every screen
                # that fits itself has to leave room for.
                above = surface.get("padding_above")
                below = surface.get("padding_below")
                pad = (f", {above}px of Steam's own padding above this plugin's box and {below}px below it"
                       if isinstance(above, int) and isinstance(below, int) else "")
                block = f", {inside}px of it this plugin's list block{pad}"
            print(f"  window: {surface.get('height')}px tall{block}, starting {surface.get('top')}px down: {fit}")
            scroll = surface.get("scroll")
            if isinstance(scroll, dict):
                cut = f"{scroll.get('by')}px hidden" if scroll.get("by") else "nothing hidden"
                print(
                    f"    the box it is drawn in holds {scroll.get('holds')}px, "
                    f"{scroll.get('top')} to {scroll.get('bottom')}, "
                    f"{scroll.get('under')}px of it under this plugin's box: {cut}"
                )
        headings = surface.get("headings") or []
        if headings:
            print(f"  headings: {' / '.join(headings)}")
        images = surface.get("images") or []
        # Said either way, because "no image" is an answer somebody is asking
        # for once the mascot can be switched off.
        if images:
            drawn = ", ".join(
                f"{image.get('alt') or 'image'} {image.get('width')}x{image.get('height')}" for image in images
            )
            print(f"  images: {drawn}")
        else:
            print("  images: none")
        for row in surface.get("rows", []):
            if testid and row.get("testid") != testid:
                continue
            marks = []
            if metrics:
                marks.append(f"{row.get('height')}px")
            if row.get("clipped"):
                marks.append(f"{row['clipped']} clipped")
            shape = f" [{', '.join(marks)}]" if marks else ""
            print(f"  - {row.get('testid')}{shape}: {row.get('text')}")
            for control in row.get("controls", []):
                state = []
                if control.get("disabled"):
                    state.append("disabled")
                if control.get("focused"):
                    state.append("ring")
                if "checked" in control:
                    state.append("on" if control["checked"] else "off")
                if control.get("value"):
                    state.append(f"value={control['value']!r}")
                if control.get("placeholder"):
                    state.append(f"showing {control['placeholder']!r} while empty")
                suffix = f" ({', '.join(state)})" if state else ""
                print(f"      {control.get('kind')}: {control.get('label')}{suffix}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--open", action="store_true", help="open the quick access panel on this plugin's page first")
    parser.add_argument(
        "--reopen", action="store_true",
        help="close the quick access panel and open it again, so the panel re-reads the backend before it is read",
    )
    parser.add_argument(
        "--press", action="append", default=[], metavar="LABEL",
        help="open one of this plugin's screens by the name on its control; repeat to go deeper",
    )
    parser.add_argument("--testid", help="report only the rows carrying this test id")
    parser.add_argument("--metrics", action="store_true", help="report the height of each row and surface")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Steam's CEF debugging endpoint")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds this will not run past")
    parser.add_argument("--json", action="store_true", help="print the whole answer as JSON")
    args = parser.parse_args()
    # Steam's own debugging endpoint is what this needs, not a process table.
    host_platform.require("The panel reader", needs_procfs=False)
    if args.timeout < 1 or args.timeout > 120:
        parser.error("--timeout must be between 1 and 120 seconds")

    try:
        pages = _pages(args.endpoint, args.timeout)
    except ProbeError as exc:
        print(f"panel read: {exc}", file=sys.stderr)
        return 1
    if args.reopen:
        closed = close_panel(pages, args.timeout)
        if not closed.get("closed"):
            print(f"panel read: could not close the panel: {closed.get('reason')}", file=sys.stderr)
            return 1
        print(f"closed the quick access panel via {closed.get('via')}")
        # Long enough for the panel to see the change and settle, and short
        # enough that this stays one command.
        time.sleep(1.5)
    if args.open or args.reopen:
        opened = open_panel(pages, args.timeout)
        if not opened.get("opened"):
            print(f"panel read: could not open the panel: {opened.get('reason')}", file=sys.stderr)
            return 1
        print(f"opened the quick access panel on {opened.get('selected') or 'Decky'}")
        if opened.get("reason"):
            print(f"  note: {opened['reason']}")
        # The panel mounts a frame or two later, and reading it before it has
        # rendered answers that this plugin is drawing nothing.
        time.sleep(1.5)
        try:
            pages = _pages(args.endpoint, args.timeout)
        except ProbeError as exc:
            print(f"panel read: {exc}", file=sys.stderr)
            return 1

    for label in args.press:
        if label not in NAVIGATION_PRESSES and label not in SCOPED_PRESSES:
            allowed = ", ".join(NAVIGATION_PRESSES + SCOPED_PRESSES)
            print(
                f"panel read: {label!r} is not one of the presses this may make. It opens screens "
                f"and nothing else: {allowed}",
                file=sys.stderr,
            )
            return 1
        pressed = press(pages, label, args.timeout)
        if not pressed.get("pressed"):
            print(f"panel read: could not press {label!r}: {pressed.get('reason')}", file=sys.stderr)
            return 1
        print(f"pressed {label}")
        # The screen it opens mounts a frame or two later.
        time.sleep(1.5)
        try:
            pages = _pages(args.endpoint, args.timeout)
        except ProbeError as exc:
            print(f"panel read: {exc}", file=sys.stderr)
            return 1

    found: list[dict[str, Any]] = []
    # A page that could not be read at all is kept, because "no surface here" and
    # "this read failed" are different answers and only one of them is about
    # the screen.
    failures: list[str] = []
    for page in pages:
        try:
            answer = read_page(args.endpoint, page, args.timeout)
        except (ProbeError, TimeoutError, OSError) as exc:
            failures.append(str(exc))
            continue
        if answer:
            found.append(answer)
    if not found:
        for failure in failures:
            print(f"panel read: {failure}", file=sys.stderr)
        print(
            "panel read: CE Decky is not drawing anything right now. Open the quick access "
            "panel, or the screen in question, and read again.",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps({"schema": 1, "surfaces": found}, indent=2, sort_keys=True))
    else:
        for answer in found:
            _print(answer, args.testid, args.metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
