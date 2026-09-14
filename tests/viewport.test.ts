import { afterEach, describe, expect, it } from "vitest";

import { MODAL_BOTTOM_PADDING, SHORT_SCREEN_MAX_HEIGHT, STEAM_BOTTOM_BAR_HEIGHT, chromeAround, isShortScreen, latchChrome, rowsOffTheBar, rowsThatFit, viewportHeight } from "../src/viewport";

const original = window.innerHeight;

function withHeight(height: number | undefined, body: () => void): void {
  Object.defineProperty(window, "innerHeight", { value: height, configurable: true });
  body();
}

afterEach(() => {
  Object.defineProperty(window, "innerHeight", { value: original, configurable: true });
});

describe("what Game Mode gives a screen to lay out in", () => {
  it("separates the two displays this plugin is developed against", () => {
    // Measured with scripts/target_ui_layout_probe.py: the page a modal opens
    // in is 844 CSS pixels tall on a 4K Steam Machine and 534 on a Steam Deck
    // LCD, and the quick access panel is 765 against 454. The breakpoint sits
    // in that gap rather than close to either end of it.
    for (const tall of [765, 844]) withHeight(tall, () => expect(isShortScreen()).toBe(false));
    for (const short of [454, 534]) withHeight(short, () => expect(isShortScreen()).toBe(true));
    expect(SHORT_SCREEN_MAX_HEIGHT).toBeGreaterThan(534);
    expect(SHORT_SCREEN_MAX_HEIGHT).toBeLessThan(765);
  });

  it("treats a screen it cannot measure as the full-size one this plugin shipped with", () => {
    // A host with no window, or one reporting nothing, is not a handheld. It is
    // a screen nobody measured, and changing every layout on the strength of a
    // missing number is the larger of the two mistakes available here.
    withHeight(0, () => {
      expect(viewportHeight()).toBeNull();
      expect(isShortScreen()).toBe(false);
      expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 264, minimum: 12 })).toBe(26);
    });
  });

  it("never returns more rows than the screen was built for, or fewer than it is worth", () => {
    // The cap is what keeps a television exactly as it was: a taller page there
    // would be a change to a screen that had no problem. The floor is what stops
    // a very short screen turning reading into pressing Next.
    withHeight(844, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 264, minimum: 12 })).toBe(26));
    withHeight(534, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 264, minimum: 12 })).toBe(18));
    withHeight(700, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 320, minimum: 12 })).toBe(25));
    withHeight(700, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 368, minimum: 12 })).toBe(22));
    // A handheld with both of a section's notes above the code: eleven rows
    // fit, twelve do not, and the measured height wins.
    withHeight(534, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 368, minimum: 12 })).toBe(11));
    // Below the readable minimum the measurement still decides, because drawing
    // past the edge of a display that was measured is the failure this exists
    // to prevent. A caller that cannot pin its own chrome measures it rather
    // than asking this to disregard the height.
    withHeight(354, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 264, minimum: 12 })).toBe(6));
    withHeight(300, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 264, minimum: 12 })).toBe(2));
    // Never zero: a page of no rows is not a page.
    withHeight(200, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 264, minimum: 12 })).toBe(1));
    // And it never knowingly exceeds what it was given.
    for (const height of [844, 765, 700, 641, 640, 534, 400, 354, 300, 200]) {
      withHeight(height, () => {
        const rows = rowsThatFit({ full: 26, rowHeight: 15, chrome: 264, minimum: 12 });
        expect(rows === 1 || 264 + rows * 15 <= height).toBe(true);
      });
    }
  });
});

describe("which page a height is read from", () => {
  it("refuses the one-pixel context this plugin's code actually runs in", () => {
    // A Decky plugin runs in Steam's shared JavaScript context, whose window is
    // 1x1, while its DOM is rendered in the page the user is looking at. Asking
    // the wrong one answered 1, and every screen fitted to it was the
    // arithmetic working on a height that was never a display. Three attempts
    // at the code screen corrected the arithmetic instead of the input.
    withHeight(1, () => expect(viewportHeight()).toBeNull());
    withHeight(1, () => expect(isShortScreen()).toBe(false));
    withHeight(1, () => expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 320, minimum: 12 })).toBe(26));
  });

  it("reads the height from the page a node belongs to", () => {
    const node = { ownerDocument: { defaultView: { innerHeight: 534 } } } as unknown as Element;
    withHeight(1, () => {
      expect(viewportHeight(node)).toBe(534);
      expect(isShortScreen(node)).toBe(true);
      expect(rowsThatFit({ full: 26, rowHeight: 15, chrome: 320, minimum: 12, node })).toBe(14);
    });
  });

  it("answers for the page each caller is actually in", () => {
    // The panel and each modal are separate pages with different heights, so
    // there is no one answer to cache: each screen asks with a node of its own.
    const panel = { ownerDocument: { defaultView: { innerHeight: 454 } } } as unknown as Element;
    const modal = { ownerDocument: { defaultView: { innerHeight: 534 } } } as unknown as Element;
    withHeight(1, () => {
      expect(viewportHeight(panel)).toBe(454);
      expect(viewportHeight(modal)).toBe(534);
      expect(isShortScreen(panel)).toBe(true);
      expect(isShortScreen(modal)).toBe(true);
    });
  });

  it("reads the room a list has from its own box rather than from an estimate", () => {
    // What is above a list is its own distance from the top of the page, and
    // what is below it is the footer. Neither moves when the number of rows
    // does, so this settles rather than chasing its own answer.
    const page = { defaultView: { innerHeight: 534 } };
    const list = {
      ownerDocument: page,
      getBoundingClientRect: () => ({ top: 180 }),
    } as unknown as Element;
    const footer = {
      ownerDocument: page,
      getBoundingClientRect: () => ({ height: 44 }),
    } as unknown as Element;
    withHeight(1, () => {
      expect(chromeAround(list, footer, 16)).toBe(240);
      // And the page that fits in what is left, rather than the one the screen
      // was built for.
      expect(rowsThatFit({ full: 6, rowHeight: 58, chrome: 240, minimum: 3, node: list })).toBe(5);
    });
  });

  it("reports no measurement rather than a wrong one", () => {
    const page = { defaultView: { innerHeight: 534 } };
    const rect = (top: number) => ({
      ownerDocument: page, getBoundingClientRect: () => ({ top, height: 0 }),
    } as unknown as Element);
    withHeight(1, () => {
      // Nothing rendered yet.
      expect(chromeAround(null, null)).toBeNull();
      // A list with nothing above it has not been laid out: every screen that
      // asks this draws a heading and a control or two over its list, so a top
      // of zero is the measurement arriving before the layout. Believing it
      // would hand the caller almost no chrome and a page too long to fit.
      expect(chromeAround(rect(0), null)).toBeNull();
      // A page with no credible height answers nothing, and the caller keeps
      // the page it was built for.
      const unmeasurable = {
        ownerDocument: { defaultView: { innerHeight: 1 } },
        getBoundingClientRect: () => ({ top: 10, height: 0 }),
      } as unknown as Element;
      expect(chromeAround(unmeasurable, null)).toBeNull();
    });
  });

  it("lets a screen with no room left say so, instead of taking the whole page", () => {
    // This used to answer null once the chrome filled the page, and both
    // callers read null as "use the page I was built for" - so the one
    // measurement proving there was no room produced the largest page.
    const page = { defaultView: { innerHeight: 534 } };
    const crowded = {
      ownerDocument: page, getBoundingClientRect: () => ({ top: 600, height: 0 }),
    } as unknown as Element;
    withHeight(1, () => {
      expect(chromeAround(crowded, null)).toBe(600);
      expect(rowsThatFit({ full: 6, rowHeight: 58, chrome: 600, minimum: 3, node: crowded })).toBe(1);
    });
  });

  it("holds a measurement rather than chasing the layout, and takes a smaller one", () => {
    // Re-measuring on every content change made the page ratchet: a filter
    // keystroke changed the row count, the re-measure read the layout the
    // previous answer had already shrunk, and the next keystroke shrank it
    // again, with nothing to bring it back. Only the ratchet's own direction is
    // refused, so a screen whose transient is over can still say so.
    const page = { defaultView: { innerHeight: 534 } };
    const at = (top: number) => ({
      ownerDocument: page, getBoundingClientRect: () => ({ top, height: 0 }),
    } as unknown as Element);
    withHeight(1, () => {
      const first = latchChrome(null, at(180), null, 16);
      expect(first).toEqual({ value: 196, settled: true });
      // A later, larger reading cannot replace it.
      expect(latchChrome(first, at(260), null, 16)).toEqual({ value: 196, settled: true });
      // But nothing credible yet stays nothing, so the first real layout wins.
      expect(latchChrome(null, at(0), null, 16)).toBeNull();
    });
  });

  it("replaces what a transient measured, once the screen has settled", () => {
    // The search screen searches the moment it opens and its own status rows
    // sit above the list while it does. Measured on a Steam Deck: chrome 371
    // while searching against 289 with the results in, which is two results a
    // page against four, on the same screen and the same game.
    const page = { defaultView: { innerHeight: 534 } };
    const at = (top: number) => ({
      ownerDocument: page, getBoundingClientRect: () => ({ top, height: 0 }),
    } as unknown as Element);
    withHeight(1, () => {
      const searching = latchChrome(null, at(355), null, 16, false);
      expect(searching).toEqual({ value: 371, settled: false });
      // Still searching, and a larger reading is still refused.
      expect(latchChrome(searching, at(400), null, 16, false)).toEqual({ value: 371, settled: false });
      // Settled, and the layout the reader is actually looking at is the answer.
      const settled = latchChrome(searching, at(273), null, 16, true);
      expect(settled).toEqual({ value: 289, settled: true });
      // Including where it is the larger of the two. A list showing a row that
      // says it is still loading has less above it than the same list with its
      // controls in place, and keeping the smaller there sized a window four
      // pixels past the bottom of a Steam Deck's display.
      const loading = latchChrome(null, at(300), null, 16, false);
      expect(loading).toEqual({ value: 316, settled: false });
      expect(latchChrome(loading, at(370), null, 16, true)).toEqual({ value: 386, settled: true });
      // And that one is final: a later transient cannot take the page back.
      expect(latchChrome(settled, at(355), null, 16, false)).toEqual({ value: 289, settled: true });
    });
  });
});

describe("what a window may not use, under its own footer", () => {
  it("leaves Steam's own modal padding as well as Steam's bar", () => {
    // Both measured on a Steam Deck with `scripts/target_panel_read.py
    // --metrics`, 2026-09-14: the bar is 41 pixels, and Steam's modal puts 26
    // above this plugin's own box in every window it draws. This used to carry
    // 16, which is this plugin's padding rather than Steam's, and the cheats
    // window ran 30 pixels past the bar - less than one row, which is the size
    // of mistake that is never noticed from a screenshot.
    expect(MODAL_BOTTOM_PADDING).toBe(26 + STEAM_BOTTOM_BAR_HEIGHT);
  });

  it("measures a list against the page, because Steam puts a window in the same place whatever height it is", () => {
    // The alternative was measuring each window against itself, on the reading
    // that the space above it was centring it would give back as it grew. It is
    // not: two of this plugin's windows, 53 pixels apart in height, both began
    // 64 pixels down the same 534 pixel page. So what is above a list is chrome
    // in the plainest sense, and a window measured against itself is a window
    // sized to take room that was never going to be there - which is exactly
    // the 30 pixels the cheats window ran past the bar.
    const page = { defaultView: { innerHeight: 534 } };
    const box = (top: number, height: number) => ({
      ownerDocument: page, getBoundingClientRect: () => ({ top, height }),
    } as unknown as Element);
    withHeight(1, () => {
      // The layout that window settled on: 233 down the page to the list, a 48
      // pixel footer under it.
      const chrome = chromeAround(box(233, 216), box(449, 48), MODAL_BOTTOM_PADDING);
      expect(chrome).toBe(348);
      // Four cheats of 43, and the fifth is what ran past the bar.
      expect(rowsThatFit({ full: 6, rowHeight: 43, chrome: chrome ?? 0, minimum: 3, node: box(233, 216) })).toBe(4);
    });
  });
});

describe("how far a screen's window ended from the bar, in rows", () => {
  const page = { defaultView: { innerHeight: 534 } };
  const footerAt = (bottom: number) => ({
    ownerDocument: page,
    getBoundingClientRect: () => ({ top: bottom - 48, height: 48, bottom }),
  } as unknown as Element);

  it("checks the statement the screen sized itself with, against where it ended up", () => {
    // Measured on a Steam Deck: Manage ended its footer at 494 on a 534 pixel
    // page, which is 27 past the 67 a window may not use, and the reader
    // reported the window running exactly 27 past the bottom. One row of 61.
    withHeight(1, () => {
      expect(rowsOffTheBar(footerAt(494), 61)).toBe(1);
      // The cheats window on the same page ends at 453, which is a row short of
      // where the bar begins once the window has taken its padding back.
      expect(rowsOffTheBar(footerAt(453), 43)).toBe(0);
    });
  });

  it("answers in both directions, because the arithmetic is wrong in both", () => {
    // The code view's section list on the same Deck ended 54 pixels above the
    // bar with rows of 37, which is a section the reader was being made to page
    // for. Negative is room; positive is debt.
    withHeight(1, () => {
      expect(rowsOffTheBar(footerAt(413), 37)).toBe(-1);
      expect(rowsOffTheBar(footerAt(340), 37)).toBe(-3);
    });
  });

  it("rounds debt up and room down, so neither answer asks for what it just refused", () => {
    withHeight(1, () => {
      // One pixel past what a window may not use is a row to give up.
      expect(rowsOffTheBar(footerAt(468), 43)).toBe(1);
      // A row and a half of room is one row of room.
      expect(rowsOffTheBar(footerAt(402), 43)).toBe(-1);
      expect(rowsOffTheBar(footerAt(553), 43)).toBe(2);
    });
  });

  it("says nothing where there is nothing to measure", () => {
    withHeight(1, () => {
      expect(rowsOffTheBar(null, 43)).toBe(0);
      expect(rowsOffTheBar(footerAt(494), 0)).toBe(0);
      // Not laid out yet, which is a footer at the origin rather than a screen
      // with nothing below its list.
      expect(rowsOffTheBar(footerAt(0), 43)).toBe(0);
      const unmeasurable = {
        ownerDocument: { defaultView: { innerHeight: 1 } },
        getBoundingClientRect: () => ({ top: 0, height: 48, bottom: 900 }),
      } as unknown as Element;
      expect(rowsOffTheBar(unmeasurable, 43)).toBe(0);
    });
  });
});

describe("the box a window is actually drawn in", () => {
  // Measured on a Steam Deck with `scripts/target_panel_read.py --metrics`: 452
  // pixels tall, spanning 40 to 492 on a 534 pixel page whose bar starts at
  // 493. So it is the usable page, and it knows both halves of this exactly.
  function inABox(
    { footerBottom, holds = 452, top = 40, content = holds }:
    { footerBottom: number; holds?: number; top?: number; content?: number },
  ): Element {
    const page = { defaultView: { innerHeight: 534 } };
    const box = {
      ownerDocument: page,
      clientHeight: holds,
      scrollHeight: content,
      parentElement: null,
      getBoundingClientRect: () => ({ top, height: holds, bottom: top + holds }),
    };
    return {
      ownerDocument: page,
      parentElement: box,
      getBoundingClientRect: () => ({ top: footerBottom - 48, height: 48, bottom: footerBottom }),
    } as unknown as Element;
  }

  it("gives up exactly what that box is keeping out of sight", () => {
    // The cheats window was 15 pixels clear of the bar by the arithmetic and
    // the box holding it was hiding 10 of its content anyway. Nothing about the
    // window's own height could have said so.
    withHeight(1, () => {
      expect(rowsOffTheBar(inABox({ footerBottom: 478, content: 462 }), 43)).toBe(1);
      // But not for a pixel or two of Steam's own rounding: a result given up
      // to reclaim two pixels is a worse screen than the two pixels are, and a
      // window hiding that much is not one anybody would call scrolling.
      expect(rowsOffTheBar(inABox({ footerBottom: 460, content: 454 }), 43)).toBe(0);
    });
  });

  it("does not read what is left under the footer inside that box as room", () => {
    // A footer ending at 445 leaves 47 before the box ends at 492, which looks
    // like a row of 43. It is not: Steam's own padding is in there under the
    // window, and a screen that took that row went straight back to scrolling.
    // The room is the page's to answer, and with 67 reserved under the footer
    // it says 22, which is no row at all.
    withHeight(1, () => {
      expect(rowsOffTheBar(inABox({ footerBottom: 445 }), 43)).toBe(0);
      expect(rowsOffTheBar(inABox({ footerBottom: 460 }), 43)).toBe(0);
    });
  });

  it("falls back to the page where there is no such box", () => {
    // A test renderer, and any host that lays nothing out.
    const page = { defaultView: { innerHeight: 534 } };
    const loose = {
      ownerDocument: page,
      parentElement: null,
      getBoundingClientRect: () => ({ top: 446, height: 48, bottom: 494 }),
    } as unknown as Element;
    withHeight(1, () => expect(rowsOffTheBar(loose, 61)).toBe(1));
  });
});
