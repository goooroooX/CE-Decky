import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { CompatibilityMark, compatibilityGlyph, deviceCompatibilityHistory, gameCompatibility, manageCompatibility } from "../src/components/CompatibilityMark";
import { SameTableMark } from "../src/components/SameTableMark";
import type { BlockedMark } from "../src/uiModel";
import type { CompatibilityEvidence } from "../src/types";
afterEach(cleanup);
const evidence: CompatibilityEvidence = { app_id: 10, table_sha256: "a".repeat(64), target_process: "game.exe", pe_version: "1", steam_build_id: null, last_working_at: 1, invalidated: false, state: "matching" };
const mark = (cause: BlockedMark["cause"], reason = "it went straight back off"): BlockedMark =>
  ({ sha256: evidence.table_sha256, reason, cause, recordedAt: 2 });
/** One rendering of each state, in the order of their precedence. */
const STATES: Array<[string, { evidence?: CompatibilityEvidence; blocked?: BlockedMark }, string]> = [
  ["failed", { blocked: mark("refused") }, "rgb(230, 108, 86)"],
  ["retest", { evidence: { ...evidence, invalidated: true } }, "rgb(230, 187, 100)"],
  ["matching", { evidence }, "rgb(141, 220, 117)"],
  ["unknown", { evidence: { ...evidence, state: "unknown" } }, "rgb(141, 220, 117)"],
];

function paintedColor(props: { evidence?: CompatibilityEvidence; blocked?: BlockedMark }): string {
  const view = render(<CompatibilityMark {...props} />);
  const painted = (view.container.querySelector("span") as HTMLElement).style.color;
  view.unmount();
  return painted;
}

it("states each compatibility state as one passive glyph", () => {
  const view = render(<CompatibilityMark evidence={evidence} />);
  expect(screen.getByLabelText("Worked on this build").querySelector("circle")).toBeTruthy();
  // Nothing readable is on the row: the words are the accessible name.
  expect(view.container.textContent).toBe("");
  expect(view.container.querySelectorAll("button,[tabindex],a").length).toBe(0);

  view.rerender(<CompatibilityMark evidence={{ ...evidence, state: "retest" }} />);
  expect(screen.getByLabelText("Worked before; retest needed")).toBeTruthy();
  view.rerender(<CompatibilityMark evidence={{ ...evidence, invalidated: true }} />);
  expect(screen.getByLabelText("Worked before; retest needed")).toBeTruthy();
  view.rerender(<CompatibilityMark evidence={{ ...evidence, state: "unknown" }} />);
  expect(screen.getByLabelText("Worked before; current build unknown")).toBeTruthy();
});

it("shows a failure with or without positive history, and lets it outrank a green", () => {
  // The strongest state used to be the one with no glyph at all.
  const view = render(<CompatibilityMark blocked={mark("refused")} />);
  const failed = screen.getByLabelText(/Marked as not working/);
  expect(failed.getAttribute("aria-label")).toContain("it went straight back off");
  expect(failed.querySelector("circle")).toBeTruthy();
  expect(view.container.textContent).toBe("");
  expect(view.container.querySelectorAll("button,[tabindex],a").length).toBe(0);

  view.rerender(<CompatibilityMark evidence={evidence} blocked={mark("refused")} />);
  expect(screen.getByLabelText(/Marked as not working/)).toBeTruthy();
  expect(screen.queryByLabelText("Worked on this build")).toBeNull();
  // A record from a build that did not write the cause down is still this table
  // failing; a legacy entry keeps its place in the same language.
  view.rerender(<CompatibilityMark evidence={evidence} blocked={mark("unknown")} />);
  expect(screen.getByLabelText(/Marked as not working/)).toBeTruthy();
});

it("never turns a source or payload condition into a compatibility verdict", () => {
  // Nobody could open those bytes, the archive is locked, the source lost the
  // file: none of that says the table does not work, so none of it colours this
  // glyph and none of it takes away what a cheat has already proven. Each keeps
  // the chip that says what it actually is.
  for (const cause of ["unusable", "encrypted", "gone"] as const) {
    expect(compatibilityGlyph(evidence, mark(cause))).toBe("matching");
    expect(compatibilityGlyph({ ...evidence, invalidated: true }, mark(cause))).toBe("retest");
    expect(compatibilityGlyph({ ...evidence, state: "unknown" }, mark(cause))).toBe("unknown");
    expect(compatibilityGlyph(undefined, mark(cause))).toBeNull();
  }
  expect(compatibilityGlyph(undefined, null)).toBeNull();
  expect(compatibilityGlyph(evidence, mark("refused"))).toBe("failed");
  expect(compatibilityGlyph({ ...evidence, invalidated: true }, null)).toBe("retest");
  expect(compatibilityGlyph(evidence, null)).toBe("matching");
  expect(compatibilityGlyph({ ...evidence, state: "unknown" }, null)).toBe("unknown");
});

it.each(STATES)("draws %s in its own colour", (_state, props, color) => {
  // Colour is the part of this a user reads first and the part a refactor can
  // quietly collapse: four states painted one colour would still carry the
  // right names, the right precedence and the right glyphs, and say nothing.
  expect(paintedColor(props)).toBe(color);
});

it("keeps every state told apart from every other, by colour or by its ring", () => {
  // Colour is what a user reads first and what a refactor can quietly collapse.
  // Three of the four states have one each. The two successes share the green
  // on purpose, because they are the same claim at two strengths, so what has
  // to stay distinct there is the ring: whole where the build was compared,
  // dashed where this screen could not compare it.
  const colours = STATES.map(([, props]) => paintedColor(props));
  expect(new Set(colours).size).toBe(3);
  expect(paintedColor({ evidence })).toBe(paintedColor({ evidence: { ...evidence, state: "unknown" } }));

  const ring = (state: CompatibilityEvidence["state"]) => {
    const view = render(<CompatibilityMark evidence={{ ...evidence, state }} />);
    const dash = view.container.querySelector("circle")?.getAttribute("stroke-dasharray") ?? null;
    view.unmount();
    return dash;
  };
  expect(ring("matching")).toBeNull();
  expect(ring("unknown")).toBeTruthy();
});

it("marks same-content provenance passively and apart from any state colour", () => {
  const view = render(<SameTableMark />);
  expect(screen.getByLabelText("Same table bytes as another source")).toBeTruthy();
  expect(view.container.textContent).toBe("");
  expect(view.container.querySelectorAll("button,[tabindex],a").length).toBe(0);
});

it("aggregates device-wide history deterministically without a current-build claim", () => {
  const rows = [evidence, { ...evidence, app_id: 20, last_working_at: evidence.last_working_at + 1 }];
  const history = deviceCompatibilityHistory(rows);
  expect(history).toEqual(deviceCompatibilityHistory([...rows].reverse()));
  expect(history).toHaveLength(1);
  expect(history[0].app_id).toBe(20);
  const view = render(<CompatibilityMark evidence={history[0]} />);
  expect(screen.getByLabelText("Worked before; current build unknown")).toBeTruthy();

  // Superseded history is not a build claim and survives the aggregation: the
  // same exact table reads as needing a retest here and in Search, rather than
  // as merely unknown on the screen that has no game to compare against.
  const superseded = deviceCompatibilityHistory([{ ...rows[1], invalidated: true }, evidence]);
  expect(superseded).toHaveLength(1);
  view.rerender(<CompatibilityMark evidence={superseded[0]} />);
  expect(screen.getByLabelText("Worked before; retest needed")).toBeTruthy();
});

it("draws the weakest positive state as a success, qualified by its ring", () => {
  // A table nobody has tried carries no glyph at all, so a question mark on one
  // this device has proven could only read as ignorance about it. It is a
  // success and is drawn as one, in the same green: what the state does not
  // carry is the comparison with the build in front of the reader, and the ring
  // says that. The check itself stays whole, because a broken check is a check
  // that is hard to read rather than a qualified statement.
  const view = render(<CompatibilityMark evidence={{ ...evidence, state: "unknown" }} />);
  const proven = view.container.querySelector("path");
  expect(proven?.getAttribute("d")).toBe("M4.5 8l2.2 2.2 4.8-4.8");
  expect(proven?.getAttribute("stroke-dasharray")).toBeNull();
  expect(view.container.querySelector("circle")?.getAttribute("stroke-dasharray")).toBeTruthy();
  expect(view.container.querySelector("text")).toBeNull();

  // And the state that does carry the comparison is the same mark with a whole
  // ring around it.
  view.rerender(<CompatibilityMark evidence={{ ...evidence, state: "matching" }} />);
  expect(view.container.querySelector("path")?.getAttribute("d")).toBe("M4.5 8l2.2 2.2 4.8-4.8");
  expect(view.container.querySelector("circle")?.getAttribute("stroke-dasharray")).toBeNull();
});

it("shows a mark for every state the backend can produce, on every screen that asks", () => {
  // The backend emits three states - `retest`, `matching`, `unknown` - and the
  // panel adds `failed` from the separate not-working record. A table nobody
  // has tried is the only thing that carries no mark, and this is what keeps
  // that the only one: each state below has to survive the lookup its screen
  // makes and come out as a drawn glyph.
  const proven = { ...evidence, app_id: 10 };
  const forAnother = { ...evidence, app_id: 77, last_working_at: 5 };
  for (const state of ["retest", "matching", "unknown"] as const) {
    // Search, opened for the game that proved it.
    expect(compatibilityGlyph(gameCompatibility([{ ...proven, state }], 10, proven.table_sha256))).toBe(
      state === "matching" ? "matching" : state,
    );
    // Search, where only another game on this device proved it. The success is
    // real and every build claim in it belongs to that other game, so it comes
    // through demoted rather than dropped - which is what used to leave the row
    // blank. `retest` earned by a build mismatch is one of those claims and
    // demotes with the rest; a record a failure retired is not, and is kept
    // below.
    expect(compatibilityGlyph(gameCompatibility([{ ...forAnother, state }], 10, proven.table_sha256))).toBe("unknown");
    // Manage with a game, and Manage with none.
    const withGame = manageCompatibility([{ ...proven, state }], 10);
    expect(compatibilityGlyph(withGame.find((entry) => entry.table_sha256 === proven.table_sha256))).toBe(
      state === "matching" ? "matching" : state,
    );
    const deviceWide = manageCompatibility([{ ...proven, state }], null);
    expect(compatibilityGlyph(deviceWide.find((entry) => entry.table_sha256 === proven.table_sha256))).toBe("unknown");
  }
  // The one thing that is not a build claim, and so survives both demotions: a
  // record a later failure retired is a retest whichever screen is asking.
  const retired = { ...proven, invalidated: true, state: "unknown" as const };
  expect(compatibilityGlyph(gameCompatibility([{ ...retired, app_id: 77 }], 10, proven.table_sha256))).toBe("retest");
  const retiredDeviceWide = manageCompatibility([retired], null);
  expect(compatibilityGlyph(retiredDeviceWide.find((entry) => entry.table_sha256 === proven.table_sha256))).toBe("retest");
  // And the one absence: no record at all, on every one of those lookups.
  expect(gameCompatibility([], 10, proven.table_sha256)).toBeUndefined();
  expect(compatibilityGlyph(undefined)).toBeNull();
});
