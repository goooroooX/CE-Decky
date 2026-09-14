import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { nextPanelInstance, startSupportLogFlush } from "../src/supportFlush";
import { logUi, logUiFailure, resetSupportLog } from "../src/supportLog";

/**
 * What the backend would have written down, in the order the requests reached
 * it. Appended when a request is answered rather than when it is made, because
 * that is the only order the file can be written in.
 */
const journal: { event: string; request: number }[] = [];
/** The requests the panel has made and this test has not answered yet. */
const open: { index: number; entries: { event: string }[]; answer: (ok: boolean) => void }[] = [];
let requests = 0;
let concurrent = 0;
let mostConcurrent = 0;

vi.mock("../src/api", () => ({
  recordPanelLog: (entries: { event: string }[]) => {
    const index = requests++;
    concurrent += 1;
    mostConcurrent = Math.max(mostConcurrent, concurrent);
    return new Promise((resolve) => {
      open.push({
        index,
        entries,
        answer: (ok: boolean) => {
          concurrent -= 1;
          if (ok) for (const entry of entries) journal.push({ event: entry.event, request: index });
          resolve({ ok, accepted: ok ? entries.length : 0 });
        },
      });
    });
  },
}));

/** Let every promise already settled run its continuations. */
async function settle(): Promise<void> {
  for (let turn = 0; turn < 8; turn += 1) await Promise.resolve();
}

/** Answer the oldest request the panel is still waiting on. */
async function answerOldest(ok = true): Promise<void> {
  const request = open.shift();
  if (!request) throw new Error("the panel has no request open");
  request.answer(ok);
  await settle();
}

/**
 * The panel's record on its way to the file that outlives the renderer.
 *
 * The file answers one question the live ring cannot: was the last thing the
 * panel did an ordinary close, or did it stop mid-work. That answer is the last
 * record in the file, so anything that can reorder or repeat what is appended
 * corrupts the only signal the file exists to carry. These cases are about that
 * property and not about the transport.
 */
describe("panel log hand-over", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetSupportLog();
    journal.length = 0;
    open.length = 0;
    requests = 0;
    concurrent = 0;
    mostConcurrent = 0;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("never has two hand-overs open at once, the final one included", async () => {
    logUi("panel.mounted");
    const stop = startSupportLogFlush();

    // The ordinary cadence takes the first batch, and the backend is slow to
    // answer it. This is the window the defect lived in.
    vi.advanceTimersByTime(5000);
    await settle();
    expect(open).toHaveLength(1);

    // More work, and then the close. Neither may be sent while the first
    // request is still open: until it is answered the cursor has not moved, so
    // a second drain taken now would repeat every entry of the first.
    logUiFailure("panel.something_failed", new Error("no"));
    vi.advanceTimersByTime(400);
    await settle();
    logUi("panel.dismounted");
    stop();
    await settle();
    expect(open).toHaveLength(1);
    expect(mostConcurrent).toBe(1);

    await answerOldest();
    // Now the rest goes, as one request, once.
    expect(open).toHaveLength(1);
    await answerOldest();
    expect(open).toHaveLength(0);

    expect(mostConcurrent).toBe(1);
    expect(journal.map((record) => record.event)).toEqual([
      "panel.mounted",
      "panel.something_failed",
      "panel.dismounted",
    ]);
    // The whole point of the file: a clean close ends on the entry that says it
    // was clean, and a record ending on anything else is a panel that stopped
    // mid-work rather than one that was closed.
    expect(journal[journal.length - 1].event).toBe("panel.dismounted");
  });

  it("gives every plugin factory invocation a lifetime of its own", () => {
    // Decky imports this bundle as `index.js?t=${Date.now()}`, and a browser
    // returns one module instance per resolved URL: two imports issued inside
    // one millisecond evaluate this module once and call its factory twice.
    // Both panels then share every module-level value, the batch id included,
    // so the record that says whether a row is still there cannot be keyed on
    // it - two rows would read as one, and dismounting either would take the
    // other's evidence with it.
    const first = nextPanelInstance();
    const second = nextPanelInstance();

    expect(first).not.toBe(second);
    // And they are recognisably from the same loaded module, which is what the
    // batch id is for and is worth keeping.
    expect(first.split("-")[0]).toBe(second.split("-")[0]);
  });

  it.each([
    ["the panel that started it stops first", true],
    ["the panel that joined it stops first", false],
  ])("keeps the failure flush while another panel is open: %s", async (_name, firstStopsFirst) => {
    // Two panels, one module, one support-log listener slot. A stop used to
    // clear it blind, and a failure recorded by the survivor then waited for
    // the interval instead of going at once, which is the entry least able to
    // wait. It has to hold whichever of them goes first.
    const first = startSupportLogFlush();
    const second = startSupportLogFlush();
    const [leaving, staying] = firstStopsFirst ? [first, second] : [second, first];
    leaving();
    await settle();
    // Whatever the stopping panel handed over is not what this is about.
    for (const _ of [...open]) await answerOldest();
    journal.length = 0;

    logUiFailure("panel.something_failed", new Error("boom"));
    vi.advanceTimersByTime(400);
    await settle();

    expect(open).toHaveLength(1);
    await answerOldest();
    expect(journal.map((record) => record.event)).toEqual(["panel.something_failed"]);

    // And the survivor's own stop is still the final ordered hand-over.
    logUi("panel.dismounted");
    staying();
    await settle();
    expect(open).toHaveLength(1);
    await answerOldest();
    expect(journal[journal.length - 1].event).toBe("panel.dismounted");
  });

  it("gives two panels of one module a single hand-over queue over the one cursor", async () => {
    // The ring, the count of what was recorded and the cursor that says how
    // much of it the backend kept are all module-global, because there is one
    // record per loaded module. A flush loop per panel over that one cursor is
    // two queues racing: both drain the same entries before either hand-over is
    // confirmed, so the file gets them twice, and nothing orders the two
    // requests against each other.
    logUi("panel.mounted", { panel_instance: "m-1" });
    logUi("panel.mounted", { panel_instance: "m-2" });
    const first = startSupportLogFlush();
    const second = startSupportLogFlush();

    // The cadence fires for both panels, and the backend is slow to answer.
    vi.advanceTimersByTime(5000);
    await settle();
    expect(open).toHaveLength(1);
    expect(mostConcurrent).toBe(1);

    // A dismount recorded while that hand-over is still in flight, and the
    // panel that recorded it releasing: the batch it is owed is queued behind
    // the one already sent rather than beside it.
    logUi("panel.dismounted", { panel_instance: "m-1" });
    first();
    await settle();
    expect(open).toHaveLength(1);
    expect(mostConcurrent).toBe(1);

    await answerOldest();
    expect(open).toHaveLength(1);
    await answerOldest();
    second();
    await settle();
    for (const _ of [...open]) await answerOldest();

    // Every entry once, in the order it was recorded: a newer batch landing
    // before an older one would leave a mount after the dismount that closed
    // it, and the record would read as a panel that stopped mid-work.
    expect(journal.map((record) => record.event)).toEqual([
      "panel.mounted", "panel.mounted", "panel.dismounted",
    ]);
    expect(mostConcurrent).toBe(1);
  });

  it("sends a refused batch again rather than leaving a hole in the record", async () => {
    logUi("panel.mounted");
    const stop = startSupportLogFlush();

    vi.advanceTimersByTime(5000);
    await settle();
    // A full or read-only filesystem: reported, never raised, and the cursor
    // stays where it was.
    await answerOldest(false);
    expect(journal).toHaveLength(0);

    logUi("panel.dismounted");
    stop();
    await settle();
    expect(open).toHaveLength(1);
    await answerOldest();

    expect(mostConcurrent).toBe(1);
    expect(journal.map((record) => record.event)).toEqual(["panel.mounted", "panel.dismounted"]);
  });
});
