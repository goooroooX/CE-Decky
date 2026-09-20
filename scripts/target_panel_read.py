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

`--press` then activates one of this plugin's own controls, by the name on it:
`--press Manage`, `--press "Configure cheats"`, and where a name is on several
rows at once, `--press imported-table-33a60e6b:Use`. It finds that control
inside this plugin's own DOM and clicks it, so what runs is the handler the
plugin wrote for it, and it can be repeated to go deeper.

**Any of this plugin's controls, and every one of them is reported.** This is a
testing tool, and a screen behind a press that writes is still a screen somebody
has to look at: **Use**, **Apply**, **Delete** and the rest are reachable here.
What that costs is a rule, not a refusal - the operator has to be told what was
pressed on their device. So every press this makes is named on its own line as
it happens, with the surface it was found on, and a run that pressed anything
ends with the list of what it pressed, in order. A report from a run that drove
the device repeats that list: a press nobody was told about is the defect this
rule exists to prevent, not the press itself.

Only controls that are actually drawn are reachable: a modal Steam has closed
can stay in the document with its handlers intact, and a press that landed on
one of those would run a screen nobody is looking at while reporting itself as
a press that worked.

Two limits remain, and neither is about which name is allowed. A name that is on
more than one control presses nothing and says where each of them is, because
**Cancel** leaves a dialog on one screen and stops a running Cheat Engine
installation on another, and guessing between them is exactly the press nobody
consented to. Write `<data-testid>:<name>` to name the one row instead, which is
identity by where the control is rather than by what it says. And a control that
is disabled is reported as disabled rather than activated, because a press Steam
would have refused is not evidence about anything.

None of this fabricates controller input, and none of it reaches Steam's own
interface: it is this plugin's own buttons, the ones its component tests press
by the same names. A running game and Steam's own interface are what still needs
a person at the device.

Works the same against the machine it runs on and against a Steam Deck over
SSH, because it is the device's own CEF endpoint either way: run it on the
device, as with every other target helper.
"""
from __future__ import annotations

import argparse
import json
import re
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


# How a press names one control rather than a word that is on several.
#
# A bare name is matched against every surface this plugin is drawing, and some
# words are on more than one of them: **Search** opens the search screen from
# the panel and spends a provider request inside it, **Cancel** leaves a dialog
# and stops a Cheat Engine installation in progress on the panel, **Use** is on
# every row of a list. A name that lands on several presses nothing and reports
# where each one is; `<data-testid>:<name>` then names the one row to look in,
# which is identity by where the control is rather than by what it says.
SCOPE_SEPARATOR = ":"
_SCOPE_RE = re.compile(r"[A-Za-z0-9_]+(?:-[A-Za-z0-9_]+)+")

# Activates one of this plugin's own controls, found by the name on it.
#
# Every match is collected before anything is clicked, and a name on more than
# one control presses none of them: this reaches the presses that write, and a
# press that landed on whichever control happened to be first in the document is
# one nobody asked for. What comes back names where each match was, so the
# caller can both report the press it made and describe the ones it refused.
_PRESS = r"""
(() => {
  const wanted = %(label)s;
  const scope = %(scope)s;
  const surface = (node) => {
    for (let at = node; at; at = at.parentElement) {
      const id = at.getAttribute && at.getAttribute("data-testid");
      if (id) return id;
    }
    return null;
  };
  // One entry per control, not one per wrapper it happens to sit inside: this
  // plugin's dense wrapper nests, so a plain walk found the same button three
  // times and reported four rows as twelve. The depth kept is the deepest
  // wrapper that holds it, because that is the screen it belongs to: taking
  // the first would file a modal's own button under the panel behind it.
  const byButton = new Map();
  const panels = Array.from(document.querySelectorAll(".ce-decky-dense"));
  for (let depth = 0; depth < panels.length; depth += 1) {
    const panel = panels[depth];
    const within = scope === null ? [panel] : Array.from(panel.querySelectorAll('[data-testid="' + scope + '"]'));
    for (const box of within) {
      for (const button of Array.from(box.querySelectorAll("button"))) {
        const label = (button.textContent || "").replace(/\s+/g, " ").trim();
        if (label !== wanted) continue;
        // Only what is actually drawn. A modal Steam has closed can stay in the
        // document with no box at all, and its buttons still carry their
        // handlers: a press that landed on one of those would run a screen the
        // reader is not looking at and report itself as a press that worked.
        if (button.getClientRects().length === 0) continue;
        const held = byButton.get(button);
        if (held === undefined || depth > held.depth) byButton.set(button, { button, where: surface(button), depth });
      }
    }
  }
  const found = Array.from(byButton.values());
  if (found.length === 0) return JSON.stringify({ pressed: false, reason: "no control with that name is on screen" });
  // The screen in front of the reader, and only that one. Opening a modal
  // leaves the one under it in the document and drawn, so the same row exists
  // once per screen that is open: a press has to mean the one on top, which is
  // the last of them in document order, or every second visit to a screen
  // makes its own controls ambiguous.
  const front = Math.max.apply(null, found.map((match) => match.depth));
  const facing = found.filter((match) => match.depth === front);
  if (facing.length > 1) {
    const places = facing.map((match) => match.where || "an unnamed surface");
    return JSON.stringify({
      pressed: false, matches: places,
      reason: "that name is on " + facing.length + " controls of the screen in front ("
        + places.join(", ") + "); name the one row as <data-testid>:<name>",
    });
  }
  const only = facing[0];
  if (only.button.disabled) {
    return JSON.stringify({ pressed: false, where: only.where, reason: "that control is disabled right now" });
  }
  only.button.click();
  return JSON.stringify({ pressed: true, label: wanted, where: only.where });
})()
"""


def press_target(label: str) -> tuple[str | None, str]:
    """The row to look in and the name to look for, out of one `--press` word.

    `<data-testid>:<name>` scopes the search to that one row; anything else is
    a bare name looked for across every surface. Every test id this plugin
    writes is one hyphenated token - `manage-list`, `table-row`,
    `imported-table-33a60e6b` - so that is what a scope has to look like, and a
    control whose own wording carries a colon keeps it. Getting that wrong is
    loud rather than silent: the press then finds no control of that name and
    says so.
    """
    head, sep, rest = label.partition(SCOPE_SEPARATOR)
    if sep and rest and _SCOPE_RE.fullmatch(head):
        return head, rest
    return None, label


def _refusal_rank(answer: dict[str, Any]) -> int:
    """How much one page's refusal actually saw, so the best one is reported."""
    if answer.get("matches"):
        return 3
    if answer.get("where"):
        return 2
    return 0 if answer.get("reason") == "no page answered" else 1


def press(pages: list[dict[str, Any]], label: str, timeout: float) -> dict[str, Any]:
    """Activate one of this plugin's own controls, and say which one it was.

    Any control this plugin draws, including the ones that write: this is a
    testing tool and the screen behind such a press is a screen somebody has to
    look at. What the caller owes in return is the report - the answer names the
    label and the surface it was found on, and the caller prints both.
    """
    scope, name = press_target(label)
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
        # Several pages answer and most of them draw nothing of this plugin's,
        # so "not on screen" from one of those must not bury "that name is on
        # four controls" from the page that is actually showing the screen. The
        # refusal kept is the one that saw the most.
        if _refusal_rank(answer) >= _refusal_rank(last):
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

    # What this run did to the device, in the order it did it. Printed as each
    # press lands and again at the end, because a press that writes is the one
    # thing a reader of this output must not have to infer.
    made: list[str] = []
    for label in args.press:
        pressed = press(pages, label, args.timeout)
        if not pressed.get("pressed"):
            print(f"panel read: could not press {label!r}: {pressed.get('reason')}", file=sys.stderr)
            if made:
                print(f"presses made before that: {', '.join(made)}", file=sys.stderr)
            return 1
        # The name that was pressed and the surface it was found on, which for
        # a scoped press is what the scope asked for rather than the scope
        # repeated back. One wording, on the line and in the closing list.
        where = pressed.get("where")
        name = str(pressed.get("label") or label)
        made.append(f"{name} on {where}" if where else name)
        print(f"pressed {made[-1]}")
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
        # In the report too, not only on the terminal: a JSON answer is what
        # gets pasted into a finding, and a run that drove the device has to
        # carry what it pressed wherever its output goes.
        print(json.dumps({"schema": 1, "pressed": made, "surfaces": found}, indent=2, sort_keys=True))
    else:
        for answer in found:
            _print(answer, args.testid, args.metrics)
    if made:
        # Last line, after the screen: this ran against a real device, and the
        # operator is owed the list of what it pressed on theirs.
        print(f"this run pressed, in order: {', '.join(made)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
