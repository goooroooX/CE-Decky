import { beforeEach, describe, expect, it } from "vitest";

import {
  MAX_SUPPORT_LOG_ENTRIES,
  confirmSupportLogFlush,
  drainSupportLog,
  logUi,
  logUiFailure,
  logUiWarning,
  observeSupportLog,
  readSupportLog,
  resetSupportLog,
  unflushedSupportLogCount,
} from "../src/supportLog";

/**
 * The panel's own record of what it did.
 *
 * Nothing on the frontend used to be written down anywhere, so a UI bug arrived
 * as a screenshot and a sentence. These cases guard the two properties that make
 * the record safe to attach to a public issue and useful once it is there: it
 * cannot grow without bound during a long session, and it carries the failure
 * text a person actually saw rather than an empty Decky error object.
 */
describe("panel support log", () => {
  beforeEach(() => resetSupportLog());

  it("records events with their fields and a timestamp", () => {
    logUi("panel.modal_opened", { modal: "table_search", app_id: 10 });

    const { entries, dropped } = readSupportLog();
    expect(dropped).toBe(0);
    expect(entries).toHaveLength(1);
    expect(entries[0].event).toBe("panel.modal_opened");
    expect(entries[0].level).toBe("info");
    expect(entries[0].fields).toEqual({ app_id: "10", modal: "table_search" });
    expect(Number.isNaN(Date.parse(entries[0].at))).toBe(false);
  });

  it("drops the oldest entries rather than growing without bound", () => {
    for (let index = 0; index < MAX_SUPPORT_LOG_ENTRIES + 25; index += 1) {
      logUi("panel.action_completed", { index });
    }

    const { entries, dropped } = readSupportLog();
    expect(entries).toHaveLength(MAX_SUPPORT_LOG_ENTRIES);
    expect(dropped).toBe(25);
    // Oldest first: what just happened is what a report is about.
    expect(entries[entries.length - 1].fields.index).toBe(String(MAX_SUPPORT_LOG_ENTRIES + 24));
  });

  it("never records the value of a password field, only that there was one", () => {
    logUiWarning("panel.archive_locked", { password: "hunter2", member: "table.CT" });

    const { entries } = readSupportLog();
    expect(entries[0].fields.password).toBe("<redacted>");
    expect(JSON.stringify(entries[0])).not.toContain("hunter2");
  });

  it("strips control characters and bidirectional overrides from recorded text", () => {
    logUi("panel.game_hydrating", { name: "Game\n‮exe.eltit‬" });

    const recorded = readSupportLog().entries[0].fields.name;
    expect(recorded).not.toContain("\n");
    expect(recorded).not.toContain("‮");
  });

  it("keeps the backend message Decky loses on the way to the frontend", () => {
    // Decky's PyError carries an empty `message` and the real text inside
    // `pythonTraceback`, so a failure recorded from `message` alone says
    // nothing at all about what the backend refused.
    const cause = Object.assign(new Error(""), {
      name: "Python ValueError",
      pythonTraceback: 'Traceback (most recent call last):\n  File "x", line 1\nValueError: no execution consent',
    });

    logUiFailure("panel.action_failed", cause, { action: "activateTable" });

    const entry = readSupportLog().entries[0];
    expect(entry.level).toBe("error");
    expect(entry.fields.action).toBe("activateTable");
    expect(entry.fields.message).toBe("no execution consent");
    expect(entry.fields.python_error).toBe("ValueError");
  });

  it("records a stack for a frontend defect, bounded and on one line", () => {
    logUiFailure("panel.render_crashed", new Error("cannot read property of undefined"));

    const entry = readSupportLog().entries[0];
    expect(entry.fields.message).toBe("cannot read property of undefined");
    expect(entry.fields.stack).toBeDefined();
    expect(entry.fields.stack).not.toContain("\n");
  });

  it("does not clear when read, so collecting twice describes the same session", () => {
    logUi("panel.mounted");

    expect(readSupportLog().entries).toHaveLength(1);
    expect(readSupportLog().entries).toHaveLength(1);
  });
  it("keeps a failure stack past the ordinary field bound", () => {
    // The stack was sliced to its own limit and then sanitised through the
    // ordinary one, so almost all of the evidence a frontend defect leaves was
    // discarded on the way into the ring while the code said it was kept.
    const stack = `Error: broke\n${"    at someFrame (webpack://src/index.tsx:1:1)\n".repeat(80)}`;
    const cause = new Error("broke");
    cause.stack = stack;

    logUiFailure("panel.action_failed", cause);

    const recorded = readSupportLog().entries[0].fields.stack;
    expect(recorded.length).toBeGreaterThan(1000);
    expect(recorded.length).toBeLessThanOrEqual(1601);
    // Every other field is still held to the ordinary bound.
    logUi("panel.action_completed", { note: "n".repeat(400) });
    expect(readSupportLog().entries[1].fields.note.length).toBeLessThanOrEqual(241);
  });
});

/**
 * The second copy, and why the cursor is separate from the ring.
 *
 * The ring lives in the renderer Steam gives the Quick Access panel, and a
 * wedged panel is recovered by restarting Steam's webhelper, which replaces
 * that renderer. The bundle collected afterwards therefore reports
 * `frontend_entries=0`, which is exactly the case the evidence was wanted for.
 * So entries are drained to the backend as they are produced.
 *
 * These cases guard what makes that safe: draining takes nothing away from a
 * bundle collected in the same session, a hand-over the backend did not keep is
 * carried again rather than lost, and a burst is recorded as a burst.
 */
describe("panel support log hand-over", () => {
  beforeEach(() => resetSupportLog());

  it("hands over only what is new, and leaves the ring intact for a bundle", () => {
    logUi("panel.mounted");
    logUi("panel.visibility_changed", { visible: true });

    const first = drainSupportLog();
    expect(first.entries.map((entry) => entry.event)).toEqual([
      "panel.mounted",
      "panel.visibility_changed",
    ]);
    confirmSupportLogFlush(first.cursor);

    // Reading for a support bundle still sees everything: the cursor moved, the
    // ring did not.
    expect(readSupportLog().entries).toHaveLength(2);
    expect(unflushedSupportLogCount()).toBe(0);
    expect(drainSupportLog().entries).toEqual([]);

    logUi("panel.dismounted");
    const second = drainSupportLog();
    expect(second.entries.map((entry) => entry.event)).toEqual(["panel.dismounted"]);
  });

  it("carries the same entries again when the backend did not keep them", () => {
    logUi("panel.action_failed");

    const attempt = drainSupportLog();
    expect(attempt.entries).toHaveLength(1);
    // The cursor is deliberately not confirmed, which is what a rejected flush
    // leaves behind.
    expect(unflushedSupportLogCount()).toBe(1);
    expect(drainSupportLog().entries.map((entry) => entry.event)).toEqual(["panel.action_failed"]);

    confirmSupportLogFlush(attempt.cursor);
    expect(unflushedSupportLogCount()).toBe(0);
  });

  it("never lets an older hand-over retire what a newer one already retired", () => {
    logUi("panel.first");
    const older = drainSupportLog();
    logUi("panel.second");
    const newer = drainSupportLog();

    confirmSupportLogFlush(newer.cursor);
    // The older answer arriving late must not reopen entries already kept.
    confirmSupportLogFlush(older.cursor);

    expect(unflushedSupportLogCount()).toBe(0);
    expect(drainSupportLog().entries).toEqual([]);
  });

  it("does not claim to hand over entries the ring has already evicted", () => {
    for (let index = 0; index < MAX_SUPPORT_LOG_ENTRIES + 25; index += 1) {
      logUi("panel.action_completed", { index });
    }

    const drained = drainSupportLog();

    // A panel that recorded 525 entries between two flushes is a panel in a
    // loop, and that is itself the finding. What it cannot do is return entries
    // that no longer exist.
    expect(drained.entries).toHaveLength(MAX_SUPPORT_LOG_ENTRIES);
    expect(drained.dropped).toBe(25);
    expect(drained.entries[drained.entries.length - 1].fields.index).toBe("524");
  });

  it("tells a watcher about a failure as soon as it lands", () => {
    const seen: string[] = [];
    observeSupportLog((level) => seen.push(level));

    logUi("panel.mounted");
    logUiWarning("panel.search_stalled");
    logUiFailure("panel.action_failed", new Error("no such table"));

    expect(seen).toEqual(["info", "warning", "error"]);
  });

  it("never lets a watcher that throws break the path it is observing", () => {
    observeSupportLog(() => {
      throw new Error("the watcher is broken");
    });

    expect(() => logUi("panel.mounted")).not.toThrow();
    expect(readSupportLog().entries).toHaveLength(1);
  });
});

describe("what a batch says was lost before it", () => {
  beforeEach(() => {
    resetSupportLog();
  });

  it("reports the loss since the last kept hand-over, not the session's total", () => {
    // Every record of a batch carries this number as `dropped_before`, so a
    // session total would stamp the same number on every record written for the
    // rest of the session: one burst of loss would read as loss that never
    // stopped, with nothing saying when it happened.
    for (let index = 0; index <= MAX_SUPPORT_LOG_ENTRIES + 6; index += 1) logUi(`panel.event_${index}`);
    const overflowed = drainSupportLog();
    expect(overflowed.dropped).toBe(7);
    confirmSupportLogFlush(overflowed.cursor);

    // The ring is full now, so this one entry evicts exactly one more: the next
    // batch says one, which is what happened before it, and not eight.
    logUi("panel.after_the_burst");
    const next = drainSupportLog();
    expect(next.entries.map((entry) => entry.event)).toEqual(["panel.after_the_burst"]);
    expect(next.dropped).toBe(1);

    // And the session's own total is still what a bundle asks for, because that
    // is a different question: how much this session lost altogether.
    expect(readSupportLog().dropped).toBe(8);
  });

  it("keeps the count owed when the backend refuses the batch that carried it", () => {
    for (let index = 0; index <= MAX_SUPPORT_LOG_ENTRIES + 2; index += 1) logUi(`panel.event_${index}`);
    const refused = drainSupportLog();
    expect(refused.dropped).toBe(3);
    // No confirmation: the entries and the count they carried are owed again.
    expect(drainSupportLog().dropped).toBe(3);

    const kept = drainSupportLog();
    confirmSupportLogFlush(kept.cursor);
    // Nothing recorded since, so nothing is owed.
    expect(drainSupportLog().dropped).toBe(0);
  });

  it("clears the count only for the batch that carried it", () => {
    // Two drains before either is confirmed is not how the one flush queue
    // behaves, but the count belongs to a particular hand-over and confirming
    // an older one must not retire what a newer one reported.
    for (let index = 0; index <= MAX_SUPPORT_LOG_ENTRIES; index += 1) logUi(`panel.event_${index}`);
    const first = drainSupportLog();
    expect(first.dropped).toBe(1);
    for (let index = 0; index < 3; index += 1) logUi(`panel.more_${index}`);
    const second = drainSupportLog();
    expect(second.dropped).toBe(4);

    // The older hand-over lands: it never carried the newer count.
    confirmSupportLogFlush(first.cursor);
    expect(drainSupportLog().dropped).toBe(4);

    confirmSupportLogFlush(second.cursor);
    expect(drainSupportLog().dropped).toBe(0);
  });

  it("does not clear a loss that happened after the batch in flight was taken", () => {
    for (let index = 0; index <= MAX_SUPPORT_LOG_ENTRIES; index += 1) logUi(`panel.event_${index}`);
    const inFlight = drainSupportLog();
    expect(inFlight.dropped).toBe(1);
    // The ring is full, so each of these evicts one while that hand-over is
    // still open: they belong to the next batch, not to the one in flight.
    for (let index = 0; index < 4; index += 1) logUi(`panel.during_${index}`);
    confirmSupportLogFlush(inFlight.cursor);

    expect(drainSupportLog().dropped).toBe(4);
  });
});
