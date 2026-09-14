/**
 * A bounded in-memory record of what the panel did, for the support bundle.
 *
 * The backend writes to Decky's plugin log, so every backend fact survives a
 * bug report. Nothing on the frontend did: a modal that opened on the wrong
 * state, a press that produced an error, a poll that stopped, all of it existed
 * only in the browser console of a device nobody can reach. A UI bug therefore
 * arrived as a screenshot and a sentence, with no way to say what the panel had
 * been told at the time.
 *
 * This is deliberately a ring buffer and not a file. It costs nothing while
 * nothing goes wrong, and it cannot grow without bound during a long session.
 * It is read for a support bundle, when the entries are handed to the backend
 * to be written into the archive.
 *
 * It is also drained as it goes, which is a second thing entirely. A panel that
 * wedges is recovered by restarting Steam's webhelper, and that destroys the
 * renderer this array lives in: the bundle collected afterwards carries an
 * empty one, which is precisely the case the evidence was wanted for. So
 * `drainSupportLog` hands new entries to the backend while the panel still
 * works, and the backend writes them where they outlive it. The cursor is
 * separate from the ring, so draining costs nothing and takes nothing away
 * from a bundle collected in the same session.
 */

import { describeError, pythonExceptionClass, pythonTracebackSummary } from "./errors";

/** Entries kept before the oldest are dropped. */
export const MAX_SUPPORT_LOG_ENTRIES = 500;
/** Longest single field value kept, in characters. */
const MAX_FIELD_CHARS = 240;
/** Longest stack excerpt kept for one failure, in characters. */
const MAX_STACK_CHARS = 1600;

export type SupportLogLevel = "info" | "warning" | "error";

export interface SupportLogEntry {
  /** Wall-clock time, so entries line up with the backend log's timestamps. */
  at: string;
  level: SupportLogLevel;
  event: string;
  fields: Record<string, string>;
}

/**
 * Field names whose value is never recorded, only their presence.
 *
 * An archive password is the one secret the panel handles, and this log is
 * written into a file that gets attached to a public issue, so the name of the
 * field is all that survives. The list matches the backend's own, because a
 * caller should not have to remember which side of the RPC it is logging on.
 */
const REDACTED_FIELD = /(?:password|passwd|token|secret|cookie|authorization|api[_-]?key)/i;

const ring: SupportLogEntry[] = [];
let dropped = 0;
/**
 * Evictions this session's durable record has not been told about yet.
 *
 * Separate from `dropped` above, which is the session's own total and is what a
 * support bundle asks for. The durable record is appended to in batches, and
 * every record of a batch carries the count it was handed as `dropped_before`:
 * a cumulative total there stamps every record written for the rest of the
 * session with the same number, so one burst of loss reads as loss that never
 * stopped, and nothing says when it happened. What that field can honestly say
 * is what was lost before this batch, which is this.
 */
let droppedPending = 0;
/** What the batch currently in flight was told, so a confirmed one clears it. */
let droppedReported = 0;
/** The cursor that batch carried, so only its own confirmation clears it. */
let droppedReportedAt = -1;
/** Entries pushed since this frontend started, including those already evicted. */
let pushed = 0;
/** How many of `pushed` have been handed to the backend for durable keeping. */
let handedOver = 0;
/** Called when an entry lands, so a failure can be flushed without waiting. */
let listener: ((level: SupportLogLevel) => void) | null = null;

/** Fields whose value is bounded by `MAX_STACK_CHARS` rather than the ordinary field bound. */
const LONG_FIELDS = new Set(["stack"]);

/** Strip control characters and bound the length of one recorded value. */
function safeValue(value: unknown, limit: number = MAX_FIELD_CHARS): string {
  let text: string;
  if (value === null || value === undefined) text = "none";
  else if (typeof value === "boolean") text = value ? "true" : "false";
  else if (typeof value === "number") text = Number.isFinite(value) ? String(value) : "nan";
  else if (typeof value === "string") text = value;
  else {
    try {
      text = JSON.stringify(value) ?? String(value);
    } catch {
      text = "<unserializable>";
    }
  }
  // Control characters, and the bidirectional overrides that let a recorded
  // value reorder the line it is read in, never survive into the archive.
  text = text.replace(/[\u0000-\u001f\u007f-\u009f\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, " ");
  text = text.replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function push(level: SupportLogLevel, event: string, fields: Record<string, unknown>): void {
  const recorded: Record<string, string> = {};
  for (const name of Object.keys(fields).sort()) {
    const value = fields[name];
    if (value === undefined) continue;
    // A stack is the whole of the evidence for a frontend defect, so it is
    // bounded by its own limit rather than by the ordinary field bound. It is
    // sanitised exactly like every other value; only the length differs.
    recorded[safeValue(name)] = REDACTED_FIELD.test(name)
      ? (value === null || value === "" ? "none" : "<redacted>")
      : safeValue(value, LONG_FIELDS.has(name) ? MAX_STACK_CHARS : MAX_FIELD_CHARS);
  }
  ring.push({ at: new Date().toISOString(), level, event: safeValue(event), fields: recorded });
  pushed += 1;
  // Oldest first: a session long enough to overflow this is one where what just
  // happened matters more than what happened an hour ago.
  while (ring.length > MAX_SUPPORT_LOG_ENTRIES) {
    ring.shift();
    dropped += 1;
    droppedPending += 1;
  }
  // Notified after the ring has settled, so a listener that drains immediately
  // sees this entry. Never allowed to throw into the caller: this is a log
  // statement inside whatever was already going wrong.
  if (listener !== null) {
    try {
      listener(level);
    } catch {
      /* a diagnostics listener never breaks the path it is observing */
    }
  }
}

/** Record one ordinary panel event. */
export function logUi(event: string, fields: Record<string, unknown> = {}): void {
  push("info", event, fields);
}

/** Record something the panel handled but that should not have happened. */
export function logUiWarning(event: string, fields: Record<string, unknown> = {}): void {
  push("warning", event, fields);
}

/**
 * Record a failure with everything that survived the RPC boundary.
 *
 * Decky loses a backend exception's message on the way to the frontend and
 * carries the real text inside `pythonTraceback`, so the message a person saw,
 * the Python exception class and the JavaScript stack are three different
 * facts. A bug report that has all three can be read without the device.
 */
export function logUiFailure(event: string, cause: unknown, fields: Record<string, unknown> = {}): void {
  const traceback = (cause as { pythonTraceback?: unknown } | null)?.pythonTraceback;
  const stack = cause instanceof Error && typeof cause.stack === "string" ? cause.stack : null;
  push("error", event, {
    ...fields,
    message: describeError(cause),
    error_type: cause instanceof Error ? cause.name : typeof cause,
    python_error: pythonExceptionClass(traceback),
    python_message: pythonTracebackSummary(traceback),
    // The stack is the only field allowed past the ordinary field bound: for a
    // frontend defect it is the entire evidence. Sanitising bounds it again on
    // the way into the ring, to the same limit.
    stack: stack === null ? undefined : stack.replace(/\s+/g, " ").slice(0, MAX_STACK_CHARS),
  });
}

/**
 * Every entry held right now, oldest first, plus how many were dropped.
 *
 * Reading does not clear: a user who collects a bundle twice for the same
 * problem should get the same history in both, and the second collection is
 * usually the one taken after reproducing it again.
 */
export function readSupportLog(): { entries: SupportLogEntry[]; dropped: number; capacity: number } {
  return { entries: [...ring], dropped, capacity: MAX_SUPPORT_LOG_ENTRIES };
}

/** Drop everything recorded so far. Used by tests, and by nothing else. */
export function resetSupportLog(): void {
  ring.length = 0;
  dropped = 0;
  droppedPending = 0;
  droppedReported = 0;
  droppedReportedAt = -1;
  pushed = 0;
  handedOver = 0;
  listener = null;
}

/**
 * Entries recorded since the last successful hand-over, oldest first.
 *
 * Taking them does not remove them from the ring: the ring still answers a
 * support bundle collected in this session, and this is a second copy for the
 * case that session does not survive to be asked. What it does move is the
 * cursor, so the next drain returns only what is new.
 *
 * A drain that outruns the ring returns what the ring still holds rather than
 * what was recorded: entries evicted before they were ever handed over are gone,
 * and `dropped` is what says so. That only happens if 500 entries land between
 * two flushes, which is a panel in a loop and is itself the finding.
 *
 * The `dropped` reported here is what has been lost since the last hand-over
 * the backend kept, not the session's total: it is stamped on every record of
 * the batch as `dropped_before`, and a total would put the same number on every
 * record written for the rest of the session. The total is still what
 * `readSupportLog` answers with, because a bundle is asking a different
 * question - how much this session lost altogether.
 */
export function drainSupportLog(): { entries: SupportLogEntry[]; dropped: number; cursor: number } {
  const outstanding = Math.min(pushed - handedOver, ring.length);
  const cursor = pushed;
  if (outstanding <= 0) return { entries: [], dropped: droppedPending, cursor };
  droppedReported = droppedPending;
  droppedReportedAt = cursor;
  return { entries: ring.slice(ring.length - outstanding), dropped: droppedPending, cursor };
}

/**
 * Mark everything up to `cursor` as durably kept.
 *
 * Separate from the drain so a rejected hand-over leaves the cursor where it
 * was and the next flush carries the same entries again. The cursor only ever
 * moves forward: two flushes in flight at once must not let the older one
 * retire what the newer already retired.
 */
export function confirmSupportLogFlush(cursor: number): void {
  if (cursor <= handedOver) return;
  handedOver = cursor;
  // Only what that batch was told about, and only for the batch that was told:
  // an eviction between the drain and this confirmation belongs to the next
  // batch, and a confirmation for some other batch clears nothing, because the
  // count is the one a particular hand-over carried.
  if (cursor === droppedReportedAt) {
    droppedPending = Math.max(0, droppedPending - droppedReported);
    droppedReported = 0;
    droppedReportedAt = -1;
  }
}

/** How many recorded entries have not yet been handed over. */
export function unflushedSupportLogCount(): number {
  return Math.max(0, pushed - handedOver);
}

/**
 * Watch entries as they land, so a failure can be kept without waiting for the
 * next tick of a timer.
 *
 * One listener, because there is one flush loop for this module's record: two
 * panels from one loaded module share it rather than starting one each.
 */
export function observeSupportLog(observer: ((level: SupportLogLevel) => void) | null): void {
  listener = observer;
}

/**
 * Give the slot up, and only if it is still this observer's.
 *
 * One loaded module can hold two panels, so the one that stops first must not
 * take the other's listener with it: what that costs is the fast flush for a
 * failure, which is the entry most worth having and the least able to wait for
 * the next tick of a timer.
 */
export function unobserveSupportLog(observer: (level: SupportLogLevel) => void): void {
  if (listener === observer) listener = null;
}
