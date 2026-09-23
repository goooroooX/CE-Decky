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
  // A switch Steam draws is a div: not a button, not an input, and with no name
  // on it at all. This plugin marks its own with `data-switch`, which is what
  // lets one be reported and pressed by the name of the control rather than by
  // where it happens to sit in the row. The row itself counts, because a switch
  // that is the whole row carries both marks on the one element.
  const switchNodes = (row) => (row.getAttribute("data-switch") ? [row] : [])
    .concat(Array.from(row.querySelectorAll("[data-switch]")));
  const switches = (row) => switchNodes(row).map((el) => ({
    kind: "switch",
    label: el.getAttribute("data-switch") || "switch",
    checked: el.getAttribute("data-checked") === "true",
    unknown: el.getAttribute("data-checked") === "unknown" ? true : undefined,
  }));
  const control = (el) => {
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
  };
  const controls = (row) => switches(row).concat(Array.from(row.querySelectorAll("button, input, select")).map(control));
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
    // A modal's own footer, which is outside the dense block on purpose: it is
    // the decision the window exists for, and a reader that could see every row
    // of a window and not its `Use this table` could open a screen and never
    // answer it. Found from the window rather than from the block, and reported
    // as a row of this surface so it is pressed by name like any other.
    let footerHost = panel.parentElement;
    for (let hop = 0; footerHost && hop < MAX_DEPTH; hop++) {
      // Any of this plugin's own action groups, which all end in `-actions`:
      // the shared modal footer, and the ones a screen names for itself.
      const footer = footerHost.querySelector('[data-testid$="-actions"]');
      // One inside the block is already one of its rows: a panel's own action
      // row would otherwise be reported twice, once as itself and once as this
      // screen's footer.
      if (footer && !panel.contains(footer)) {
        const box = footer.getBoundingClientRect();
        rows.push({
          testid: footer.dataset.testid,
          text: text(footer).slice(0, 300),
          height: Math.round(box.height),
          controls: controls(footer),
          clipped: footer.querySelectorAll(".ce-decky-marquee[data-clipped]").length,
        });
        break;
      }
      footerHost = footerHost.parentElement;
    }
    // Buttons that sit in no row at all. Steam's full-width buttons carry no
    // test id, so `Load table & start CE` and `Configure cheats` were pressable
    // by name and yet absent from every read, which cost a session a search
    // through the source to learn they were there.
    const loose = Array.from(panel.querySelectorAll("button")).filter((el) => !el.closest("[data-testid]"));
    if (loose.length) {
      rows.push({
        testid: "(untagged)",
        text: loose.map((el) => text(el)).join(" / ").slice(0, 300),
        // Not one box, so no height of its own to report.
        height: null,
        controls: loose.map(control),
        clipped: 0,
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
# The window that holds Steam's own menus, which is not always the focused one.
#
# With a game running the focused window instance is the game, and it carries no
# `MenuStore` at all: a probe that only ever asked the focused window reported
# the quick access panel as unopenable exactly when a device session is most
# worth having. The main Steam UI window is what has the menus, so it is looked
# for by that rather than by focus.
_MENU_WINDOW = """
  function menuWindow() {
    const store = window.SteamUIStore;
    if (!store) return null;
    const candidates = [];
    try {
      if (typeof store.GetFocusedWindowInstance === "function") candidates.push(store.GetFocusedWindowInstance());
    } catch (error) { /* a focused window this build will not hand over is not an answer */ }
    const windows = store.WindowStore;
    if (windows) {
      for (const name of ["GamepadUIMainWindowInstance", "MainWindowInstance", "SteamUIWindowInstance"]) {
        try { if (windows[name]) candidates.push(windows[name]); } catch (error) { /* same */ }
      }
      try {
        const all = windows.SteamUIWindows || windows.m_rgSteamUIWindows || [];
        for (const item of all) candidates.push(item);
      } catch (error) { /* same */ }
    }
    for (const candidate of candidates) {
      if (candidate && candidate.MenuStore) return candidate;
    }
    return null;
  }
"""

_CLOSE = """
(() => {
""" + _MENU_WINDOW + """
  const win = menuWindow();
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
""" + _MENU_WINDOW + """
  const win = menuWindow();
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
  // A screen is its dense block and, for a modal, the footer beside it: the
  // footer holds the decision the window exists for, and it sits outside the
  // block by design. Both are the same screen, so they share its depth.
  const panels = Array.from(document.querySelectorAll(".ce-decky-dense")).map((panel) => {
    const boxes = [panel];
    for (let host = panel.parentElement, hop = 0; host && hop < 6; host = host.parentElement, hop++) {
      const footer = host.querySelector('[data-testid$="-actions"]');
      if (footer && !panel.contains(footer)) { boxes.push(footer); break; }
    }
    return boxes;
  });
  for (let depth = 0; depth < panels.length; depth += 1) {
    const boxes = panels[depth];
    const within = [];
    for (const box of boxes) {
      if (scope === null) { within.push(box); continue; }
      if (box.getAttribute && box.getAttribute("data-testid") === scope) within.push(box);
      for (const inner of Array.from(box.querySelectorAll('[data-testid="' + scope + '"]'))) within.push(inner);
    }
    for (const box of within) {
      const pressable = Array.from(box.querySelectorAll("button"))
        .map((node) => ({ node, label: (node.textContent || "").replace(/\s+/g, " ").trim() }))
        // This plugin's own switches, by the name it marked them with. Steam
        // draws one as a div, so it is neither a button nor an input and has no
        // name to match: without the mark a reader could see every row of a
        // screen and reach none of the switches, which is most of what this
        // product is.
        .concat(((box.getAttribute && box.getAttribute("data-switch") ? [box] : [])
          .concat(Array.from(box.querySelectorAll("[data-switch]")))).map((node) => ({
          // The switch itself, which Steam draws as a checkbox, rather than
          // whatever else the marked box happens to hold: a mark that wraps a
          // whole labelled row has more inside it than the control.
          node: node.querySelector('[role="checkbox"], [role="switch"]')
            || node.querySelector('[role], [tabindex], button') || node,
          label: node.getAttribute("data-switch") || "",
        })));
      for (const { node: button, label } of pressable) {
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


# What one row is made of, for a question the summary cannot answer.
#
# Two of them keep coming up on a device. A row's text is cut to fit a report,
# so a finding block several sentences long can only be read in part - and
# whether it says a third thing is exactly what a screen that promises at most
# two findings has to be checked for. And a control this reader cannot name,
# such as one of Steam's own switches, is invisible in the summary: what it is
# made of is what says how to reach it.
#
# It reads and presses nothing.
_DESCRIBE = r"""
(() => {
  const wanted = %(testid)s;
  const text = (node) => (node.textContent || "").replace(/\s+/g, " ").trim();
  const seen = [];
  const rows = wanted === null
    ? Array.from(document.querySelectorAll(".ce-decky-dense [data-testid]"))
    : Array.from(document.querySelectorAll('[data-testid="' + wanted + '"]'));
  for (const row of rows) {
    if (row.getClientRects().length === 0) continue;
    const parts = [];
    const candidates = row.querySelectorAll(
      'button, input, select, [role], [tabindex], [class*="Toggle"], [class*="toggle"],'
      + ' [class*="Dropdown"], [class*="dropdown"], [data-switch], [data-dropdown]');
    for (const node of Array.from(candidates).slice(0, 24)) {
      const box = node.getBoundingClientRect();
      const data = {};
      for (const name of Array.from(node.attributes || [])) {
        if (name.name.startsWith("data-")) data[name.name] = String(name.value).slice(0, 80);
      }
      parts.push({
        tag: node.tagName.toLowerCase(),
        role: node.getAttribute("role") || null,
        name: text(node).slice(0, 60) || node.getAttribute("aria-label") || null,
        classes: String(node.className || "").split(/\s+/).filter(Boolean).slice(0, 4),
        data: Object.keys(data).length ? data : undefined,
        drawn: box.width > 0 && box.height > 0,
      });
    }
    seen.push({ testid: row.dataset.testid, text: text(row).slice(0, 4000), parts });
    if (seen.length >= 40) break;
  }
  return JSON.stringify({ rows: seen });
})()
"""


# Choosing one option of a dropdown, which is Steam's own menu rather than this
# plugin's own control.
#
# A `DropdownItem` draws a control here and opens its options somewhere else:
# the list is Steam's context menu, in Steam's own markup, and nothing this
# plugin drew is in it. A reader that stopped at the plugin's own controls could
# therefore open **Choose game** and not choose a game, which is the first step
# of the workflow this exists to walk. So the control is opened, the menu is
# waited for, the option is matched by the text a user would read, and what it
# did is reported; a menu that did not appear, or an option that is not in it,
# closes the menu again and names what was there instead.
_CHOOSE = r"""
(async () => {
  const wanted = %(option)s;
  const scope = %(scope)s;
  const drawn = (node) => node.getClientRects().length > 0;
  const text = (node) => (node.textContent || "").replace(/\s+/g, " ").trim();
  const panels = Array.from(document.querySelectorAll(".ce-decky-dense")).filter(drawn);
  const roots = scope === null
    ? (panels.length ? [panels[panels.length - 1]] : [])
    : Array.from(document.querySelectorAll('[data-testid="' + scope + '"]')).filter(drawn);
  const found = [];
  for (const root of roots) {
    for (const node of Array.from(root.querySelectorAll('[class*="dropdown" i], [data-dropdown]'))) {
      if (!drawn(node)) continue;
      // The control, not the label inside it: the outermost match wins and
      // anything it contains is the same control seen again.
      if (found.some((held) => held.contains(node))) continue;
      found.push(node);
    }
  }
  if (found.length === 0) return JSON.stringify({ chosen: false, reason: "no dropdown is on the screen in front" });
  if (found.length > 1) {
    return JSON.stringify({
      chosen: false,
      reason: "the screen in front has " + found.length + " dropdowns; name the row as <data-testid>:<option>",
      showing: found.map((node) => text(node).slice(0, 60)),
    });
  }
  const control = found[0];
  const before = text(control);
  control.click();
  const items = async () => {
    const deadline = Date.now() + 3000;
    while (Date.now() < deadline) {
      const shown = Array.from(document.querySelectorAll(
        '[class*="contextmenu" i] [class*="item" i], [role="menuitem"], [class*="ContextMenuItem" i]')).filter(drawn);
      if (shown.length > 0) return shown;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return [];
  };
  const shown = await items();
  if (shown.length === 0) {
    return JSON.stringify({ chosen: false, reason: "the dropdown was opened and no menu appeared", showing: before });
  }
  const labels = shown.map(text);
  let at = labels.findIndex((label) => label === wanted);
  if (at < 0) {
    // A shorter name is allowed to stand for one option and never for a choice
    // between two: `Atomic Heart` is the whole of what a caller wants to write,
    // and `Atomic Heart 2` beside it is exactly the pick nobody asked for.
    const starting = labels.map((label, index) => ({ label, index })).filter((item) => item.label.startsWith(wanted));
    if (starting.length > 1) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      return JSON.stringify({
        chosen: false, reason: "that name is the start of " + starting.length + " of this dropdown's options",
        options: starting.map((item) => item.label).slice(0, 40),
      });
    }
    if (starting.length === 1) at = starting[0].index;
  }
  if (at < 0) {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    return JSON.stringify({
      chosen: false, reason: "that option is not in this dropdown",
      options: labels.slice(0, 40),
    });
  }
  shown[at].click();
  await new Promise((resolve) => setTimeout(resolve, 300));
  return JSON.stringify({ chosen: true, option: labels[at], was: before, showing: text(control) });
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


def _row_text(answer: dict[str, Any], testid: str) -> str | None:
    """What one row of a read says, wherever in the answer that row is."""
    for surface in answer.get("surfaces") or []:
        for row in surface.get("rows") or []:
            if row.get("testid") == testid:
                return str(row.get("text") or "")
        deeper = _row_text(surface, testid)
        if deeper is not None:
            return deeper
    return None


def choose(pages: list[dict[str, Any]], label: str, timeout: float) -> dict[str, Any]:
    """Pick one option of a dropdown, through Steam's own menu.

    A write like any press: what a dropdown chooses is the game a profile is
    for, the process Cheat Engine attaches to, or which cheats a screen lists.
    The caller names it in its report exactly as it names a press.
    """
    scope, option = press_target(label)
    expression = _CHOOSE % {"option": json.dumps(option), "scope": json.dumps(scope or None)}
    last: dict[str, Any] = {"chosen": False, "reason": "no page answered"}
    for page in pages:
        url = str(page.get("webSocketDebuggerUrl", ""))
        if not url:
            continue
        connection = DevToolsSocket(url, timeout)
        try:
            request = connection.send("Runtime.evaluate", {
                "expression": expression, "returnByValue": True, "awaitPromise": True,
                "timeout": int(timeout * 1000),
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
        if answer.get("chosen"):
            return answer
        # A page that saw a dropdown says more than one that saw none.
        if answer.get("options") or answer.get("showing"):
            last = answer
        elif last.get("reason") == "no page answered":
            last = answer
    return last


def describe(pages: list[dict[str, Any]], testid: str | None, timeout: float) -> dict[str, Any]:
    """What one row is made of: its whole text, and every control inside it.

    For the two questions the summary cannot answer - what a long row actually
    says, and how to reach a control this reader cannot name. Read only.
    """
    expression = _DESCRIBE % {"testid": json.dumps(testid)}
    best: dict[str, Any] = {"rows": []}
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
        # The page that is actually drawing the screen is the one with rows on
        # it; the others answer with none and must not bury it.
        if len(answer.get("rows") or []) > len(best.get("rows") or []):
            best = answer
    return best


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
            if metrics and row.get("height") is not None:
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
    parser.add_argument(
        "--choose", action="append", default=[], metavar="OPTION",
        help="pick one option of a dropdown on the screen in front, by the text a reader sees;"
             " write <data-testid>:<option> where the screen has more than one",
    )
    parser.add_argument(
        "--wait-for", metavar="TESTID:TEXT",
        help="wait until that row says that, then stop; for a screen that is still doing something",
    )
    parser.add_argument(
        "--wait-seconds", type=float, default=30.0,
        help="how long --wait-for may wait, at most 300",
    )
    parser.add_argument("--testid", help="report only the rows carrying this test id")
    parser.add_argument(
        "--describe", nargs="?", const="", metavar="TESTID",
        help="print one row's whole text and every control inside it, or every row of the screen in front",
    )
    parser.add_argument("--metrics", action="store_true", help="report the height of each row and surface")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Steam's CEF debugging endpoint")
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds this will not run past")
    parser.add_argument("--json", action="store_true", help="print the whole answer as JSON")
    args = parser.parse_args()
    # Steam's own debugging endpoint is what this needs, not a process table.
    host_platform.require("The panel reader", needs_procfs=False)
    if args.timeout < 1 or args.timeout > 120:
        parser.error("--timeout must be between 1 and 120 seconds")
    if args.wait_seconds < 1 or args.wait_seconds > 300:
        parser.error("--wait-seconds must be between 1 and 300 seconds")

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
    for label in args.choose:
        picked = choose(pages, label, args.timeout)
        if not picked.get("chosen"):
            print(f"panel read: could not choose {label!r}: {picked.get('reason')}", file=sys.stderr)
            for name in (picked.get("options") or [])[:40]:
                print(f"  option: {name}", file=sys.stderr)
            if made:
                print(f"presses made before that: {', '.join(made)}", file=sys.stderr)
            return 1
        made.append(f"chose {picked.get('option')}")
        print(f"{made[-1]} (was {picked.get('was') or 'nothing'})")
        # The screen re-renders on the choice, exactly as it does on a press.
        time.sleep(1.5)
        try:
            pages = _pages(args.endpoint, args.timeout)
        except ProbeError as exc:
            print(f"panel read: {exc}", file=sys.stderr)
            return 1
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

    if args.describe is not None:
        answer = describe(pages, args.describe or None, args.timeout)
        if args.json:
            print(json.dumps(answer, indent=2, sort_keys=True))
            return 0
        rows = answer.get("rows") or []
        if not rows:
            print("panel read: no row of that name is on screen", file=sys.stderr)
            return 1
        for row in rows:
            print(f"{row.get('testid')}:")
            print(f"  text: {row.get('text')}")
            for part in row.get("parts") or []:
                bits = [part.get("tag")]
                if part.get("role"):
                    bits.append(f"role={part['role']}")
                if part.get("name"):
                    bits.append(f"name={part['name']!r}")
                if part.get("classes"):
                    bits.append("class=" + ".".join(part["classes"]))
                if part.get("data"):
                    bits.append(str(part["data"]))
                if not part.get("drawn"):
                    bits.append("not drawn")
                print("   - " + " ".join(str(bit) for bit in bits))
        return 0

    if args.wait_for:
        scope, wanted = press_target(args.wait_for)
        if scope is None:
            parser.error("--wait-for is written <data-testid>:<text the row has to carry>")
        deadline = time.monotonic() + args.wait_seconds
        while True:
            saw = None
            for page in pages:
                try:
                    answer = read_page(args.endpoint, page, args.timeout)
                except (ProbeError, TimeoutError, OSError):
                    continue
                if answer and _row_text(answer, scope) is not None:
                    saw = _row_text(answer, scope)
                    if wanted in saw:
                        print(f"{scope} says {saw!r}")
                        return 0
            if time.monotonic() >= deadline:
                print(
                    f"panel read: {scope} did not say {wanted!r} within {args.wait_seconds:.0f}s"
                    + (f"; it says {saw!r}" if saw is not None else "; that row is not on screen"),
                    file=sys.stderr,
                )
                return 1
            # Long enough that this is not a busy loop on the device, short
            # enough that a state it is waiting for is not sat on.
            time.sleep(1.0)

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
