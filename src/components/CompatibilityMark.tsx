import type { CompatibilityEvidence } from "../types";
import type { BlockedMark } from "../uiModel";

/**
 * The four states one exact table can be in, strongest first.
 *
 * Search and Manage say this with one glyph each and no words, because a user
 * scanning twenty rows reads a colour before a sentence, and the two screens
 * used to say the same thing three different ways: a circled check here, a
 * different character and the word `Worked before` there, and the strongest
 * state of all - these exact bytes were tried and did not work - as a red word
 * in a third place, or as nothing at all.
 *
 * Precedence is the order of this union. A durable failure outranks every
 * positive record, including one that still compares as matching, because the
 * failure is about the same bytes and is newer than whatever earned the green.
 */
export type CompatibilityGlyph = "failed" | "retest" | "matching" | "unknown";
//: `unknown` is the weakest positive state and not an absence: a table nobody
//: has tried carries no glyph at all. It says this device proved these exact
//: bytes and this screen cannot say whether the proof is about the build in
//: front of the reader, which is what Manage opened with no game selected can
//: honestly claim.

/**
 * The durable causes that are a statement about whether this exact table works.
 *
 * `unusable`, `encrypted` and `gone` are about the bytes or about the source
 * that served them: a file nobody could open never failed at anything, and a
 * file the provider no longer has is not about this device at all. They keep
 * their own cause-specific chip and never colour this glyph.
 */
const COMPATIBILITY_FAILURE_CAUSES = ["refused", "unknown"] as const;

export function isCompatibilityFailure(mark: BlockedMark | null | undefined): boolean {
  return Boolean(mark) && (COMPATIBILITY_FAILURE_CAUSES as readonly string[]).includes(mark!.cause);
}

/**
 * What a row should show, from the two durable records about the table on it.
 *
 * `blocked` is the durable record the row is carrying, which the caller picks:
 * the record on these exact bytes where there is one. A cause that is not about
 * whether the table works leaves no claim in either direction rather than
 * turning into a verdict here.
 */
export function compatibilityGlyph(
  evidence: CompatibilityEvidence | undefined,
  blocked?: BlockedMark | null,
): CompatibilityGlyph | null {
  if (isCompatibilityFailure(blocked)) return "failed";
  // Everything else a record can say is about the bytes that were downloaded or
  // about the source that served them, and none of it is evidence about whether
  // a table that did load worked for a game. It has a chip of its own and
  // leaves proven history where it is.
  if (!evidence) return null;
  if (evidence.invalidated || evidence.state === "retest") return "retest";
  return evidence.state === "matching" ? "matching" : "unknown";
}

// `unknown` is green because it is a success: this device ran a cheat from
// these exact bytes and they worked. What it does not carry is the comparison
// with the build in front of the reader, and that is said by the ring around
// the check rather than by taking the colour away - a grey mark on a list where
// an untried table carries no mark at all reads as ignorance about a table that
// has in fact been proven.
const GLYPH_COLOR: Record<CompatibilityGlyph, string> = {
  failed: "hsl(9, 74%, 62%)",
  retest: "#e6bb64",
  matching: "#8ddc75",
  unknown: "#8ddc75",
};

const GLYPH_LABEL: Record<CompatibilityGlyph, string> = {
  failed: "Marked as not working",
  retest: "Worked before; retest needed",
  matching: "Worked on this build",
  unknown: "Worked before; current build unknown",
};

function glyphBody(state: CompatibilityGlyph) {
  if (state === "matching") return <path d="M4.5 8l2.2 2.2 4.8-4.8" fill="none" stroke="currentColor" strokeWidth="1.6" />;
  if (state === "failed") return <path d="M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2" fill="none" stroke="currentColor" strokeWidth="1.6" />;
  if (state === "retest") {
    // A closed turn with a head on it: the same "go round again" idea the row's
    // own wording used to carry, in the space a chip used to take.
    return <>
      <path d="M10.8 6.1a3.4 3.4 0 1 0 .5 2.5" fill="none" stroke="currentColor" strokeWidth="1.5" />
      <path d="M8.2 5.6l2.8.3-.4 2.7" fill="none" stroke="currentColor" strokeWidth="1.5" />
    </>;
  }
  // Worked before, and this screen cannot compare it with the build in front of
  // the reader. The check is the same check and is drawn whole: a broken one is
  // a check that is hard to read rather than a qualified statement. What is
  // qualified is the ring, and `CompatibilityMark` draws that.
  return <path d="M4.5 8l2.2 2.2 4.8-4.8" fill="none" stroke="currentColor" strokeWidth="1.6" />;
}

/**
 * One passive, non-focusable statement about an exact table.
 *
 * Everything readable is in `title` and `aria-label`: the visible surface is
 * the glyph, so a row keeps its height and its single controller focus stop.
 */
export function CompatibilityMark({ evidence, blocked }: { evidence?: CompatibilityEvidence; blocked?: BlockedMark | null }) {
  const state = compatibilityGlyph(evidence, blocked);
  if (!state) return null;
  const label = state === "failed" && blocked?.reason
    ? `${GLYPH_LABEL.failed}: ${blocked.reason}`
    : GLYPH_LABEL[state];
  return <span
    role="img"
    title={label}
    aria-label={label}
    style={{ color: GLYPH_COLOR[state], whiteSpace: "nowrap", flexShrink: 0, lineHeight: 0 }}
  >
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false" style={{ verticalAlign: "middle" }}>
      {/* The ring carries the qualification, not the mark inside it: a light
          dash says this device proved these bytes and nothing here compared
          them with the build the reader is on. */}
      <circle
        cx="8" cy="8" r="6.5" fill="none" stroke="currentColor"
        strokeDasharray={state === "unknown" ? "2.6 2.2" : undefined}
      />
      {glyphBody(state)}
    </svg>
  </span>;
}

/**
 * What Manage shows for every row it lists, whichever game each one belongs to.
 *
 * This screen is one list of two groups: the tables this game has, and the
 * tables the device holds for its other games. Filtering the evidence to the
 * selected game answered for the first group and left the second with no mark
 * at all, so a table that had been proven to work showed exactly what a table
 * nobody has ever tried shows.
 *
 * Every row here keeps the state the backend computed for it, including the
 * rows of the other group. That state is not a claim about the selected game
 * and never was: it is computed per record, against the profile and the current
 * build of the game that record belongs to, and every row in that group names
 * its own game. Downgrading it to "worked before, current build unknown" would
 * be throwing away an answer that is already correct for the game the row is
 * about.
 *
 * `deviceCompatibilityHistory` does drop it, and is still right to: that is the
 * Manage opened with no game at all, where the rows are one per set of bytes
 * rather than one per game, so a build claim would be made on behalf of a game
 * the row does not name.
 */
export function manageCompatibility(
  entries: CompatibilityEvidence[],
  appId: number | null,
): CompatibilityEvidence[] {
  if (appId === null) return deviceCompatibilityHistory(entries);
  const mine = entries.filter((entry) => entry.app_id === appId);
  const claimed = new Set(mine.map((entry) => entry.table_sha256));
  // Newest first, so one table proven by two games is represented by the
  // record that was most recently true.
  const others = new Map<string, CompatibilityEvidence>();
  for (const entry of [...entries].sort((a, b) => b.last_working_at - a.last_working_at || a.app_id - b.app_id)) {
    if (claimed.has(entry.table_sha256) || others.has(entry.table_sha256)) continue;
    others.set(entry.table_sha256, entry);
  }
  return [...mine, ...others.values()];
}

/**
 * What one exact table's record is, on a screen that is about one game.
 *
 * Search lists candidates for the game it was opened for, so it looked only for
 * that game's own record and showed nothing at all for a table another game on
 * this device had proven. That is the same hole `manageCompatibility` exists to
 * close, in the screen next door: a table this device has proven showed exactly
 * what a table nobody has ever tried shows.
 *
 * This game's record is used as it stands, because it is a claim about the game
 * in front of the reader. Another game's is kept and demoted: the success is
 * real and the build comparison behind it is not about this game, so it comes
 * through as the weakest positive state, which says worked before and nothing
 * about the build. `invalidated` survives either way - a record a later failure
 * retired is a retest whichever game is asking.
 */
export function gameCompatibility(
  entries: readonly CompatibilityEvidence[],
  appId: number | null | undefined,
  sha256: string | null | undefined,
): CompatibilityEvidence | undefined {
  // No table, no record: a row that has not resolved to exact bytes yet has
  // nothing this could be about.
  if (!sha256) return undefined;
  // A screen with no game of its own has only the demoted answer to give, which
  // is what the fallback below produces.
  const mine = appId === null || appId === undefined
    ? undefined
    : entries.find((entry) => entry.app_id === appId && entry.table_sha256 === sha256);
  if (mine) return mine;
  const others = entries
    .filter((entry) => entry.table_sha256 === sha256)
    .sort((a, b) => b.last_working_at - a.last_working_at || a.app_id - b.app_id);
  return others.length ? { ...others[0], state: "unknown" } : undefined;
}

/**
 * The newest success per exact table, for a Manage with no game selected.
 *
 * No current game can establish a current-build match, so the comparison is
 * dropped and what is left is history. Whether that history was superseded is
 * not a build claim and is kept: a success a later failure invalidated is a
 * table to retest whichever screen is asking, and dropping the flag here made
 * device-wide Manage say "current build unknown" about the same exact SHA that
 * Search was calling out for a retest.
 */
export function deviceCompatibilityHistory(entries: CompatibilityEvidence[]): CompatibilityEvidence[] {
  const newest = new Map<string, CompatibilityEvidence>();
  for (const entry of [...entries].sort((a, b) => b.last_working_at - a.last_working_at || a.app_id - b.app_id)) {
    if (!newest.has(entry.table_sha256)) newest.set(entry.table_sha256, { ...entry, state: "unknown" });
  }
  return [...newest.values()];
}
