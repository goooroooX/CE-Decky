/**
 * How much room Steam's Game Mode has actually given this plugin.
 *
 * Game Mode never hands the frontend physical pixels. Steam picks a device
 * pixel ratio for the display and scales its whole UI, so what a layout has to
 * fit into is the CSS viewport, and that is not derivable from the resolution:
 * a 4K television and a 1280x800 handheld differ here by hundreds of CSS pixels
 * of height and by none at all of width. Measured with
 * `scripts/target_ui_layout_probe.py` on both devices this plugin is developed
 * against:
 *
 * | Page                  | Steam Machine, 4K | Steam Deck LCD |
 * |-----------------------|-------------------|----------------|
 * | Quick Access panel    | 855 x 765         | 854 x 454      |
 * | The page modals open in | 1500 x 844      | 854 x 534      |
 * | Device pixel ratio    | 2.56              | 1.5            |
 *
 * The width is the same on both, near enough that no layout here has ever
 * needed to care. The height is not: a handheld gives a modal 534 CSS pixels
 * where the television gives 844, and a screen built to the second number
 * simply hangs off the first one, top and bottom, with no controller press that
 * can reach what went past the edge.
 *
 * So height is the whole of what this module reports, and it reports it as one
 * question with one answer. `SHORT_SCREEN_MAX_HEIGHT` sits in the wide gap
 * between the two columns above rather than close to either, because what a
 * future display reports is unknown and the cost of the two mistakes is not
 * equal: treating a tall screen as short spends vertical room it had, while
 * treating a short screen as tall puts content where nothing can reach it.
 */

/**
 * Above this many CSS pixels of height, a screen is laid out as it always was.
 *
 * Every measurement above is either near 500 or near 800, and this is the
 * middle of that gap. It is a height and never a width or a resolution: the
 * same 1280x800 panel reports a different CSS viewport depending on the ratio
 * Steam chose, and the ratio is exactly what a layout cannot see.
 */
export const SHORT_SCREEN_MAX_HEIGHT = 640;

/**
 * The height of the page this code is running in, or `null` where there is none.
 *
 * A modal and the quick access panel are separate Steam pages with separate
 * viewports, so each one measures its own rather than the display's. Under a
 * test renderer or any host without a window there is no measurement, and the
 * callers below treat that as the full-size screen this plugin was built for
 * rather than inventing a number.
 */
const MIN_CREDIBLE_PAGE_HEIGHT = 200;

/**
 * The window a rendered node actually belongs to, which is not this one.
 *
 * A Decky plugin's code runs in Steam's shared JavaScript context, and that
 * context's own window is one pixel by one: `window.innerHeight` there is `1`,
 * and so is `window.screen.height`. The plugin's DOM is not there. It is
 * rendered into the page the user is looking at, which on a Steam Deck LCD is
 * 854x534 CSS pixels and on a 4K television 1500x844, and the only way to reach
 * that page from here is through a node that is in it.
 *
 * This cost three attempts at the code screen to find. Every one of them
 * corrected the arithmetic on the assumption that the height was right: the
 * height was `1`, so a page of one row was the arithmetic working. Reading it
 * from the node is what makes the number mean the display.
 */
function pageOf(node?: Element | null): Window | null {
  const view = node?.ownerDocument?.defaultView;
  if (view) return view;
  return typeof window === "undefined" ? null : window;
}

/**
 * The height of the page a node is rendered in, or `null` where there is none.
 *
 * `node` is how a caller asks about the page it is actually on. Without one
 * this can only ask its own context, which for anything inside a modal is the
 * shared context and answers `1`.
 *
 * A height below `MIN_CREDIBLE_PAGE_HEIGHT` is not a display. It is a context
 * that renders nothing, and it is reported as no measurement rather than as a
 * very small screen, because a layout fitted to one pixel is not a layout.
 */
export function viewportHeight(node?: Element | null): number | null {
  const view = pageOf(node);
  const height = view?.innerHeight;
  return typeof height === "number" && height >= MIN_CREDIBLE_PAGE_HEIGHT ? height : null;
}

/**
 * Whether this page is one of the short ones, and has to be laid out for it.
 *
 * Unmeasurable reads as not short. A layout that keeps its full-size shape on a
 * screen nobody could measure is the same layout this plugin has always shipped;
 * one that switches to the handheld shape there would change every screen on
 * every host that cannot answer, which is the larger of the two mistakes.
 */
export function isShortScreen(node?: Element | null): boolean {
  const height = viewportHeight(node);
  return height !== null && height <= SHORT_SCREEN_MAX_HEIGHT;
}

/**
 * Fit a count of fixed-height rows into what this screen actually has.
 *
 * `full` is what the screen was built with and stays the answer wherever there
 * is room for it, which is what keeps a television's screens exactly as they
 * were. `chrome` is everything on the screen that is not those rows: the
 * heading, the row that heads the list, the notes a section can carry, the
 * pager, the button below it and the padding Steam's own modal adds around all
 * of it. The result is never below `minimum`, because a page of two lines is
 * not a smaller version of this screen, it is a different and worse one.
 */
export function rowsThatFit(
  { full, rowHeight, chrome, minimum, node }:
  { full: number; rowHeight: number; chrome: number; minimum: number; node?: Element | null },
): number {
  const height = viewportHeight(node);
  if (height === null) return full;
  const fitted = Math.floor((height - chrome) / rowHeight);
  if (fitted >= minimum) return Math.min(full, fitted);
  // Below the readable minimum, and the measurement still decides.
  //
  // This briefly refused to believe a viewport that left less than half a page,
  // on the ground that no display is that small, and returned the readable
  // minimum instead. That is drawing past the edge of a measured display on
  // purpose, which is the failure this exists to prevent, and it treated a
  // symptom of the caller's own estimate as a fact about the screen. The
  // uncertain number is `chrome`; a caller that cannot pin it measures it
  // rather than asking this to disregard the height.
  //
  // Never zero, because a page of no rows is not a page and leaves the reader
  // nothing to page through.
  return Math.max(1, fitted);
}

/**
 * The strip of Game Mode a window may not draw into.
 *
 * Steam paints its own bar along the bottom of the page, over whatever is
 * behind it. A window measured against `window.innerHeight` therefore fits the
 * page and is still cut: the last rows of it are behind that bar.
 *
 * Measured on both machines this is developed against, 2026-09-14, by asking
 * the page what element is under the middle of its bottom edge: the same
 * element on each, 41 pixels tall, at the bottom of the page. A Steam Deck
 * reports a 534 pixel page with the bar starting at 493, and a 4K Steam Machine
 * an 844 pixel page with it starting at 803. So it is the same reserve on both
 * rather than a share of the height, which is what makes it a constant here.
 */
export const STEAM_BOTTOM_BAR_HEIGHT = 41;

/**
 * What sits under a screen's own footer before the display ends.
 *
 * Steam's own modal padding plus the bar above, because a page is only usable
 * down to where that bar starts and a window's last pixel is not its footer's.
 * One number for every screen that measures itself, rather than one per screen:
 * it is a property of the window both are drawn in, and two copies of it drift.
 *
 * The padding is Steam's and not this plugin's, so it is measured rather than
 * chosen: `scripts/target_panel_read.py --metrics` reports it at both ends of
 * each window, and on a Steam Deck on 2026-09-14 both of this plugin's paged
 * windows read the same 26 pixels above this plugin's box and 26 below it. It
 * was 16 here, which is this plugin's own padding and ten short of Steam's, so
 * a screen that fitted itself ended ten pixels into the bar. That is under a
 * row, which is the size of mistake that hides: it does not clip a control, it
 * takes the bottom off one.
 *
 * Nothing is counted twice. What is above a list is measured from the top of
 * the page, so Steam's padding above is already in it; this is the other end,
 * where a footer's own height stops and the window still has Steam's 26 and
 * then the bar's 41 under it.
 */
export const MODAL_BOTTOM_PADDING = 26 + STEAM_BOTTOM_BAR_HEIGHT;


// How far up from a footer this looks for the box a window is drawn in.
const MAX_ANCESTORS_SEARCHED = 12;

/**
 * How much of this screen the box Steam draws it in is keeping out of sight.
 *
 * The outermost ancestor that is still a box rather than the page, because that
 * is the one a window has to fit inside. Measured on a Steam Deck: 452 pixels
 * tall, spanning 40 to 492 on a 534 pixel page whose bar starts at 493 - so it
 * is the usable page, and asking it is asking the only thing that knows.
 *
 * What it hides is exactly what has to go, and that is the whole of what it is
 * asked. What is left under a footer inside it is deliberately never read as
 * room: Steam's own padding is in there under the window, so a screen that
 * counted it took a row that did not fit and went straight back to scrolling.
 *
 * Zero where there is no such box, which is a test renderer, a host with no
 * layout, and a node not in a document.
 */
function hiddenByTheBoxItIsDrawnIn(node: Element): number {
  const height = viewportHeight(node);
  if (height === null) return 0;
  let parent = node.parentElement ?? null;
  for (let step = 0; parent !== null && step < MAX_ANCESTORS_SEARCHED; step += 1) {
    const box = parent;
    parent = box.parentElement;
    const holds = box.clientHeight;
    // Not laid out, and not the page: a box as tall as the display is what the
    // window is drawn on rather than what it is drawn in.
    if (holds <= 1 || holds >= height) continue;
    const hides = box.scrollHeight - holds;
    // The first box actually keeping something out of sight, rather than the
    // first box that could: a wrapper sized by its own content hides nothing
    // and does not answer for the box around it.
    if (hides > 1) return hides;
  }
  return 0;
}

/**
 * How far a screen's window ended from Steam's bar, in whole rows.
 *
 * Positive is rows it has to give up, negative is rows of room it did not use.
 *
 * `chromeAround` is arithmetic on measurements taken while a screen was being
 * laid out, and those can be of a layout the reader never settles on. Manage
 * measured what was above its list before the control that filters that list
 * had been drawn, sized a page 52 pixels taller than there was room for, and
 * ended 27 pixels behind Steam's bar - stable, and wrong, with nothing in the
 * arithmetic able to notice.
 *
 * So the same statement is checked afterwards against where the screen actually
 * ended up. It is deliberately the same statement: a footer's bottom plus what
 * a window may not use is where this screen ends, and it has to be above the
 * page. The footer is asked rather than the window because the footer is the
 * last thing a screen of this shape draws and every one of them already holds a
 * node for it, while Steam's own box around them is nobody's to hang a ref on.
 *
 * Answered in rows because rows are what a caller can do anything about, and
 * in both directions because the arithmetic is wrong in both: the same
 * transient that made Manage's page too long by a row leaves the code view's
 * section list a row short of what its window has room for. What a caller does
 * with that, and how many times it may act on it, is the caller's rule:
 * `useFittedRows` is the one every screen here uses.
 *
 * Zero wherever there is nothing to measure, which is the first render and any
 * host without layout.
 */
export function rowsOffTheBar(footer: Element | null, rowHeight: number): number {
  if (footer === null || rowHeight <= 0) return 0;
  const height = viewportHeight(footer);
  if (height === null) return 0;
  if (typeof footer.getBoundingClientRect !== "function") return 0;
  const bottom = footer.getBoundingClientRect().bottom;
  if (!(bottom > 0)) return 0;
  // What the box this window is drawn in is keeping out of sight, which is the
  // one statement about fitting that rests on nothing. The cheats window ended
  // 15 pixels clear of the bar by the arithmetic below and that box was hiding
  // 10 of its content anyway, so a screen that "fitted" moved under the
  // reader's thumb. Only this direction: what is left under the footer inside
  // that box is not room, because Steam's own padding is in there with it, and
  // reading it as room offered a row that did not fit.
  // A quarter of a row is the least that is worth a whole row. Steam's own
  // rounding leaves a pixel or two hidden on a window nobody would call
  // scrolling, and giving up a result to reclaim two pixels is a worse screen
  // than the two pixels are.
  const hidden = hiddenByTheBoxItIsDrawnIn(footer);
  if (hidden * 4 > rowHeight) return Math.ceil(hidden / rowHeight);
  const over = bottom + MODAL_BOTTOM_PADDING - height;
  if (!Number.isFinite(over)) return 0;
  // Past the bar, rounded up: half a row behind it is a row behind it.
  if (over > 0) return Math.ceil(over / rowHeight);
  // Short of it, rounded down: only whole rows of room are room, and a screen
  // that rounded this one up would be asking for the row it just refused.
  // Written out rather than negated in place, because negating a floor of zero
  // is `-0`, and `-0` is not `0` to anything comparing with `Object.is`.
  const room = Math.floor(-over / rowHeight);
  return room > 0 ? -room : 0;
}

/**
 * How many fixed-height rows fit between a list's own top and the page's bottom.
 *
 * `rowsThatFit` takes `chrome` as a figure somebody measured on a device, which
 * is right for a screen whose chrome is a fixed block of known parts. Two of
 * this plugin's lists are not that: the cheats list sits under a section picker
 * and a filter whose heights are Steam's, and the results list sits under a
 * query field, a source summary and a banner that is there only sometimes. A
 * number estimated for those is a number that is wrong on one of the screens.
 *
 * So it is read instead of estimated, from two nodes the caller already has:
 * everything above the list is its own distance from the top of the page, and
 * everything below it is the footer's height plus whatever the modal pads with.
 * Neither depends on how many rows the answer turns out to be, so this settles
 * rather than oscillating: what is above a list does not move when the list gets
 * shorter, and the footer keeps its height.
 *
 * `null` where there is nothing to measure yet, which is the first render and
 * any host without layout. The caller then uses the page it was built for.
 */
export function chromeAround(
  list: Element | null,
  footer: Element | null,
  bottomPadding = 0,
): number | null {
  const height = viewportHeight(list ?? footer);
  if (height === null || list === null) return null;
  if (typeof list.getBoundingClientRect !== "function") return null;
  // The distance down the page, and not down the window the list is in. Steam
  // starts a modal at the same place whatever height it is - measured on a
  // Steam Deck, two of this plugin's windows 53 pixels apart in height both
  // began 64 pixels down - so what is above a list really is chrome and does
  // not move when the list does.
  const top = list.getBoundingClientRect().top;
  const below = footer && typeof footer.getBoundingClientRect === "function"
    ? footer.getBoundingClientRect().height
    : 0;
  // A list with nothing above it has not been laid out yet: it is rendered
  // under a heading and a control or two on every screen that asks this, and a
  // top of zero is the measurement arriving before the layout rather than a
  // screen that is all list. That is the one answer refused, because believing
  // it hands the caller a chrome of almost nothing and a page too long for the
  // display.
  //
  // A chrome that fills the page is NOT refused. It is the honest answer for a
  // screen with no room left, and refusing it was backwards: both callers read
  // `null` as "use the page I was built for", so the one measurement proving
  // there was no room produced the largest page. `rowsThatFit` takes it from
  // here and lets the measurement decide, down to a single row.
  //
  // Rounded, because a fraction of a pixel is not a row.
  const chrome = Math.round(top + below + bottomPadding);
  if (!Number.isFinite(chrome) || top <= 0) return null;
  return chrome;
}

/**
 * The room a list has, measured while the screen is in the state it settles in.
 *
 * The measurement is only stable while what it measures is: the distance from
 * the top of the page to the list is a fact about the controls above it, and
 * those do not move when the number of rows changes - unless the modal they are
 * in is centred, in which case a shorter list re-centres everything and the
 * next measurement reads a larger top.
 *
 * Measuring again on every content change turned that into a ratchet. A filter
 * keystroke changed the row count, the re-measure read the layout the previous
 * answer had already shrunk, and the page stepped down again on the next
 * keystroke until it hit the caller's own minimum, with nothing to bring it
 * back.
 *
 * So a measurement is taken and held, and the only thing that may replace it is
 * a smaller one. That direction is safe from the ratchet, which only ever reads
 * larger, and it is the one the search screen needs: its own status rows sit
 * above the list while a search runs, and a search starts the moment the screen
 * opens. Latching the first answer there sized the page against a layout that
 * exists for a few seconds. Measured on a Steam Deck's 534 pixel page: chrome
 * 371 while searching against 289 once the results were in, which is two rows
 * of results against four, on the same screen and the same game.
 *
 * `settled` says the transient is over, and the caller names what its own
 * transient is. A measurement taken then is final, because the layout it read
 * is the one the reader is going to be looking at.
 */
export function latchChrome(
  held: LatchedChrome | null,
  list: Element | null,
  footer: Element | null,
  bottomPadding = 0,
  settled = true,
): LatchedChrome | null {
  if (held?.settled) return held;
  const measured = chromeAround(list, footer, bottomPadding);
  if (measured === null) return held;
  if (held === null) return { value: measured, settled };
  // The first settled measurement wins outright. What was held before it was
  // taken against a layout the reader never sees, and that layout is not
  // reliably the taller one: a list showing a row that says it is still loading
  // has less above it than the same list with its controls in place, so a rule
  // that only ever took the smaller kept the transient's answer and sized the
  // window four pixels past the bottom of a Steam Deck's display.
  if (settled) return { value: measured, settled: true };
  // While the transient is still up, only a smaller reading replaces the held
  // one: a larger one is the re-centring this latch was written for.
  return { value: Math.min(held.value, measured), settled: false };
}

/** A chrome measurement, and whether it was taken once the screen had settled. */
export interface LatchedChrome {
  value: number;
  settled: boolean;
}
