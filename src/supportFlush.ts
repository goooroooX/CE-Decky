/**
 * Hand the panel's record to the backend while the panel still works.
 *
 * `supportLog.ts` keeps what the panel did in a ring buffer, and that buffer
 * lives in the renderer Steam gives the Quick Access panel. One class of defect
 * destroys it as a condition of being recovered: a wedged panel is fixed by
 * restarting Steam's webhelper, which replaces the renderer, so the support
 * bundle collected afterwards reports `frontend_entries=0` and nothing about
 * what the panel had been doing survives. That is not hypothetical; it is what
 * happened to the 2026-09-12 panel-close incident.
 *
 * So the entries are flushed as they are produced. The backend appends them to
 * a bounded file that outlives the renderer, the plugin reload and the reboot,
 * and the support bundle collects it beside the live ring.
 *
 * This is diagnostics, and it holds itself to what that permits:
 *
 * - it costs nothing while nothing is being recorded. The ring is quiet when
 *   the panel is idle, so an idle panel makes no calls at all;
 * - a failure is never reported to the user and never retried into a loop. A
 *   rejected flush leaves the cursor where it was, and the entries go again
 *   with the next one;
 * - it never delays anything a user is waiting on. Nothing awaits it.
 *
 * Lifetime is the frontend's, not the panel's. Opening any modal unmounts the
 * panel, and the modal's own failures are exactly what a report needs, so this
 * is started by `definePlugin` and stopped by `onDismount`.
 */

import { recordPanelLog } from "./api";
import {
  confirmSupportLogFlush,
  drainSupportLog,
  observeSupportLog,
  unobserveSupportLog,
  unflushedSupportLogCount,
  type SupportLogLevel,
} from "./supportLog";

/** Ordinary cadence. Long enough to batch a burst, short enough to survive one. */
const FLUSH_INTERVAL_MS = 5000;
/**
 * How soon a failure is written down.
 *
 * A warning or an error is very often the last thing recorded before whatever
 * it is warning about stops the panel, so it does not wait for the interval.
 * Short but not zero: one press can record several entries, and they should go
 * as one call.
 */
const FAILURE_FLUSH_DELAY_MS = 400;

/**
 * Distinguishes one frontend lifetime from the next in the durable record.
 *
 * A new value in the file means the renderer was replaced, which is the
 * difference between a panel that closed and a panel that was restarted out of
 * a wedge. `Math.random` is right for this: it is a grouping key, not an
 * identity, and it must not depend on a crypto API being exposed to this page.
 */
const SESSION = Math.random().toString(36).slice(2, 10);

/**
 * Which renderer this module was evaluated in.
 *
 * The panel's record outlives the renderer that wrote it, and an install has to
 * be able to tell one generation of it from the next: the frontend being
 * replaced is alive while its replacement is being asked for, its entries reach
 * the file on a five second timer, and Decky's last load of the plugin tells
 * that outgoing frontend to import the bundle again. A late flush from it lands
 * after any boundary a reader can take, and neither the moment on it nor the
 * batch id in it says which renderer produced it: the batch id is per module,
 * and that re-import is a new module in the old renderer.
 *
 * So the id is kept on the global object, which is per renderer: every module
 * evaluated in one renderer finds the same value, and a renderer that has just
 * been created has none and makes one. A global that refuses the property is
 * handled rather than thrown on, and leaves the id per module, which is what it
 * was before this existed.
 */
const RENDERER_KEY = "__ceDeckyRenderer";

function rendererId(): string {
  const host = globalThis as Record<string, unknown>;
  const existing = host[RENDERER_KEY];
  if (typeof existing === "string" && existing) return existing;
  const created = Math.random().toString(36).slice(2, 10);
  try {
    host[RENDERER_KEY] = created;
  } catch {
    // A frozen or sealed global. Nothing to recover: the id is this module's
    // own from here, which is strictly weaker and never wrong about identity.
  }
  return created;
}

const RENDERER = rendererId();

/** Which renderer this panel is in, for the record that outlives it. */
export function panelRenderer(): string {
  return RENDERER;
}

/**
 * How many times this module's plugin factory has run.
 *
 * One module can be asked for more than one panel, and that is the case the
 * durable record has to be able to describe. Decky imports this bundle as
 * `index.js?t=${Date.now()}` and a browser returns one module instance per
 * resolved URL, so two imports issued in the same millisecond resolve to the
 * same URL, evaluate this module once and call its factory twice. Two rows,
 * one module, one `SESSION`.
 *
 * The batch id above is therefore the wrong key for a row: it says which loaded
 * module wrote the entry, which is what it is for, and cannot tell two panels
 * from that module apart. A counter can, and a counter beside the batch id is
 * unique in both directions - across modules because `SESSION` differs, within
 * one because this does.
 */
let panels = 0;

/**
 * The id of one panel, for the record that says whether it is still there.
 *
 * Called once per plugin factory invocation, which is once per row Decky adds.
 * The mount that opens a row and the dismount that closes it carry this, so
 * something reading the file afterwards can pair them; nothing else about a
 * panel is identity, and this is deliberately not derived from anything that
 * would repeat if the module were evaluated again.
 */
export function nextPanelInstance(): string {
  panels += 1;
  return `${SESSION}-${panels}`;
}

/**
 * The timer functions, taken from the global rather than from `window`.
 *
 * This runs at `definePlugin` time, which is module evaluation for anything
 * that loads the built bundle - including the packaging smoke check, which
 * imports `dist/index.js` in bare Node to prove it loads at all. There is no
 * `window` there, and a diagnostics timer must not be the reason the bundle
 * fails to evaluate. Where there are no timers there is also nothing to flush
 * to, so the absence is handled rather than worked around.
 */
const timers = globalThis as {
  setInterval?: (handler: () => void, ms: number) => unknown;
  clearInterval?: (handle: unknown) => void;
  setTimeout?: (handler: () => void, ms: number) => unknown;
  clearTimeout?: (handle: unknown) => void;
};

/**
 * Stop a timer from holding its host's event loop open.
 *
 * Node returns a `Timeout` object carrying `unref`; a browser returns a number
 * and has no such concept, because nothing there waits for a page to go idle.
 * It matters because the packaging smoke check loads the built bundle in Node
 * and calls this plugin's factory: without this, the diagnostics interval keeps
 * that process alive until something kills it. That is a hang rather than a
 * failure, which is the worst shape a defect can take in a gate, and it is not
 * hypothetical - it is what this function was added for.
 */
function unrefTimer(handle: unknown): void {
  (handle as { unref?: () => void } | null)?.unref?.();
}

/**
 * How many panels are using the flush machinery below, and everything it holds.
 *
 * Module scope rather than per call, and that is the whole of this design. The
 * ring, the count of what has been recorded and the cursor that says how much
 * of it the backend has kept are all module-global, because there is one record
 * per loaded module. One flush loop per panel over one shared cursor is
 * therefore not two independent queues, it is two queues racing one another:
 * both drain the same entries before either hand-over is confirmed, so the file
 * gets them twice, and nothing orders the two requests, so a newer batch can
 * land before an older one and leave the record ending on an entry that was
 * recorded before its last line. That last part is what the file is for - a
 * record ending on `panel.dismounted` is a panel that closed - and an ordering
 * artifact of the flushing reads as a wedge.
 *
 * One module can hold two panels: Decky imports this bundle as
 * `index.js?t=${Date.now()}` and a browser returns one module instance per
 * resolved URL, so two imports issued inside one millisecond call the factory
 * twice. So the machinery is one per module, counted rather than duplicated:
 * the first panel starts it, later panels join it, and the last one out stops
 * it and makes the final hand-over.
 */
let users = 0;
/**
 * Every hand-over, one after the next.
 *
 * Two of these in flight at once is not a race about a lock: the backend
 * appends under one, so the bytes are safe either way. What is not safe is the
 * order they arrive in and what each of them carries. The cursor only moves
 * when a hand-over is confirmed, so a second batch drained before the first is
 * confirmed repeats every entry of the first, and nothing makes the older
 * request reach the file before the newer one.
 *
 * So there is one queue for the whole module, every panel's stop included, and
 * each drain is taken only once the hand-over before it has settled. It
 * deliberately outlives the panels: a queue rebuilt per panel is the race
 * above, and one that survives the last release keeps the order of what the
 * next panel records.
 */
let chain: Promise<void> = Promise.resolve();
let queued = false;
let failureTimer: unknown = null;
let interval: unknown = null;
let observer: ((level: SupportLogLevel) => void) | null = null;

function handOver(): Promise<void> {
  // Read here rather than at enqueue time: what is owed is whatever the ring
  // holds now that the previous hand-over has been confirmed, which is what
  // keeps a confirmed entry from being sent a second time.
  const { entries, dropped, cursor } = drainSupportLog();
  if (entries.length === 0) return Promise.resolve();
  return recordPanelLog(entries, dropped, SESSION)
    .then((result) => {
      // Only a hand-over the backend says it kept moves the cursor. A backend
      // that could not write reports `ok: false` rather than raising, and those
      // entries go again with the next flush.
      //
      // `ok` rather than `accepted === entries.length`: a backend that wrote
      // everything it could keep still reports fewer than it was given when one
      // entry was malformed enough to be dropped, and those entries will be
      // dropped again every time. Holding the cursor for them would resend the
      // same batch for ever and duplicate the rest of it on every pass. A write
      // that did not fully land is `ok: false`, which is the case the cursor is
      // actually being held for.
      if (result?.ok) confirmSupportLogFlush(cursor);
    })
    .catch(() => {
      // Deliberately silent, and deliberately not logged through `logUi`: a
      // failed flush that recorded its own failure would record another one on
      // the next attempt, and the ring would fill with nothing else.
    });
}

/**
 * Put one hand-over at the end of the queue.
 *
 * At most one waits behind the one in flight, because a drain taken later
 * carries everything an earlier one would have: a third would send nothing.
 * `force` is for a panel's stop, which has an entry of its own to hand over and
 * must not be answered by a queued drain that was taken before it.
 */
function enqueue(force = false): void {
  if (queued && !force) return;
  queued = true;
  chain = chain
    .then(() => {
      queued = false;
      return handOver();
    })
    .catch(() => undefined);
}

/**
 * Start flushing for one panel, and return the function that releases it.
 *
 * Called once per plugin factory invocation. The first call starts the shared
 * timer and listener; a later one joins them, because the record they write to
 * is shared too. Each returned function releases only its own hold, and the
 * last release stops the timer, gives up the listener and makes the final
 * hand-over, so an ordinary dismount still ends the record with the entry that
 * says it was ordinary.
 */
export function startSupportLogFlush(): () => void {
  if (typeof timers.setInterval !== "function" || typeof timers.clearInterval !== "function") {
    // Not a browser: nothing records, nothing flushes, and stopping is free.
    return () => undefined;
  }
  users += 1;
  if (users === 1) {
    interval = timers.setInterval(() => {
      if (unflushedSupportLogCount() > 0) enqueue();
    }, FLUSH_INTERVAL_MS);
    unrefTimer(interval);
    observer = (level: SupportLogLevel): void => {
      if (level === "info" || failureTimer !== null) return;
      if (typeof timers.setTimeout !== "function") return;
      failureTimer = timers.setTimeout(() => {
        failureTimer = null;
        enqueue();
      }, FAILURE_FLUSH_DELAY_MS);
      unrefTimer(failureTimer);
    };
    observeSupportLog(observer);
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    users -= 1;
    // Its own entries go now either way: a panel that has just recorded its
    // dismount must not have to wait for the next tick of a timer that belongs
    // to whichever panel is still open.
    enqueue(true);
    if (users > 0) return;
    timers.clearInterval?.(interval);
    interval = null;
    if (failureTimer !== null) {
      timers.clearTimeout?.(failureTimer);
      failureTimer = null;
    }
    if (observer !== null) {
      unobserveSupportLog(observer);
      observer = null;
    }
  };
}
