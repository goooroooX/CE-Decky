import { useUiSurface } from "./useUiSurface";
import { traceUiAction, traceUiEdit, startUiOperation } from "./uiActions";
import { ButtonItem, DialogButton, Field, Focusable, ModalRoot, PanelSection, PanelSectionRow, Spinner, TextField, showModal } from "@decky/ui";
import { toaster } from "@decky/api";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  cancelTableAcquisition,
  pollTableSearch,
  searchTables,
  startTableAcquisition,
} from "./api";
import { forgetRejectedArtifact, isRejectedArtifact, isArchiveFilename } from "./tableImport";
import { showActionFailure } from "./modals/ActionFailureModal";
import { TableAcquisitionModal } from "./modals/TableAcquisitionModal";
import { ModalActions, modalActionStyle } from "./components/ModalActions";
import { PROVIDER_PAGE_SIZE, advertisedRelease, clampPage, pageCount, pageItems, providerShortName, releaseLabel, tableRowRefusals, type BlockedLookups, type BlockedMark } from "./uiModel";
import { MODAL_BOTTOM_PADDING, latchChrome, rowsThatFit, viewportHeight, type LatchedChrome } from "./viewport";
import { CONTENTS_ONLY, FOCUS_SCROLL_CLASS, FocusScrollText, PanelRow, SectionHeading, SmallButton, focusFirstEnabled, usePageHeight, useRowHeight } from "./components/PanelDensity";
import { PagerFooter } from "./components/PagerFooter";
import { CompatibilityMark, gameCompatibility, isCompatibilityFailure } from "./components/CompatibilityMark";
import { SameTableMark } from "./components/SameTableMark";
import type { CompatibilityEvidence, ArtifactResolution, AcquisitionStatus, BlockedTableCause, CatalogResult, CatalogSearchOutcome, ProviderSearchSummary, TableSearchProgress, TableStatus } from "./types";
import { describeError } from "./errors";
import { logUi, logUiFailure, logUiWarning } from "./supportLog";

interface Props {
  compatibility?: readonly CompatibilityEvidence[];
  artifactResolutions?: readonly ArtifactResolution[];
  onRefreshProvenance?: () => Promise<{ resolutions: readonly ArtifactResolution[]; compatibility: readonly CompatibilityEvidence[] }>;
  gameIdentity: string;
  gameName: string;
  /**
   * The game a download started from this screen belongs to.
   *
   * Carried into the acquisition and used for nothing but the durable record a
   * failed one may write: the not-working list is read months later, and a
   * provider row or a bare archive name says nothing about what it was for.
   */
  appId?: number | null;
  shortcutExecutable?: string | null;
  initialQuery?: string;
  autoSearch?: boolean;
  localArtifacts?: Record<string, string>;
  /**
   * Available exact-SHA tables associated with **this game**.
   *
   * This is what the screen offers on its own: a row of its own for every one
   * of them that no provider row represents, and the copy a provider row is
   * answered from when that row is one this game imported before. Scoped to the
   * game on purpose, because a standalone row here is an offer to use that
   * table for the game being searched for, and a table stored for some other
   * game is not that. The picker behind **Stored** is where the rest of the
   * device's tables are chosen from, by a press that says which game they are
   * being taken for.
   */
  localTables?: readonly TableStatus[];
  /**
   * Every available table on the device, for exact-digest lookup only.
   *
   * Kept apart from `localTables` because it answers a different question. When
   * a provider row advertises a checksum, the only thing that matters is
   * whether those exact bytes are already here, and which game they happen to
   * be associated with says nothing about that. Nothing in this list is ever
   * offered as a row of its own; it only lets a row the user is already looking
   * at be answered from the device instead of downloaded again.
   */
  deviceTables?: readonly TableStatus[];
  /**
   * The provider rows a table on this device was imported from, `provider:id`.
   *
   * Kept apart from `localArtifacts` rather than merged into it because the two
   * carry different weight: a digest says these are the bytes, a provider row
   * says only that this row has been imported from before. A set rather than a
   * lookup to the table, because a row this names may have been imported for
   * another game, or the file may be gone: what is offered on a press comes
   * from `localTables`, which is this game's own available tables, and this
   * answers only for the rows that have nothing there to offer.
   */
  importedArtifacts?: ReadonlySet<string>;
  /**
   * What the screen around this one wants on the footer row, beside paging.
   *
   * The modal's own Close, which was a third full-width button under two
   * others. The catalog owns the row because the row is mostly paging; the
   * screen owns what closing it means.
   */
  footerActions?: ReactNode;
  /**
   * Exact table SHA-256s the user recorded as not working, to the reason.
   *
   * Durable, and it survives a fresh search: bytes that do not work do not
   * start working because the catalog was re-read.
   */
  blockedTables?: Record<string, BlockedMark>;
  /**
   * The same record, keyed by the provider row the bytes were downloaded from.
   *
   * A digest recognises a row only when the provider advertises one, and the
   * source most of these tables come from advertises none - so the mark was
   * invisible on precisely the rows it had been recorded from, and the same
   * table was found, downloaded and tried again on every search. A provider row
   * has an identity always, which is what this matches on.
   */
  blockedArtifacts?: Record<string, BlockedMark[]>;
  /**
   * Forget the marks on these exact tables, after the user asked to retry them.
   *
   * A game update is the ordinary reason a table starts working again, so this
   * has to be reachable from the screen that is showing the greyed rows rather
   * than only from the global list under Advanced.
   */
  onClearMarks?: (sha256s: string[]) => Promise<void>;
  /**
   * Re-read the marks, because this screen is a detached tree.
   *
   * A modal opened with `showModal` never sees its props change, so the two
   * lookups above are the record as it stood when it opened. Without a way to
   * re-read them, clearing a mark left the row it was on greyed out and still
   * counted, and a download that turned out not to be a table created a record
   * this screen could not know about and would offer again.
   */
  onRefreshBlocked?: () => Promise<BlockedLookups>;
  onLocalSelected?: (sha256: string) => Promise<void>;
  onImported: (sha256: string) => Promise<void>;
  /** Close the surrounding modal before Steam's browser takes the screen. */
}

/**
 * How long a search runs before the panel explains itself.
 *
 * An ordinary search answers well inside this, and a line that appears and
 * vanishes again is worse than the spinner it sits beside. Past it the user is
 * entitled to know what is still happening: one search measured on the
 * development device took 45 seconds, of which Playground was 44.8 and 37 of
 * those were its own 11.9 MB site index, while every other source had answered
 * inside three. The panel said only "45s".
 *
 * Ten seconds was that machine's idea of a long search, and on a Steam Deck it
 * meant the explanation never arrived: three searches measured there took 4.3,
 * 6.7 and 12.4 seconds, so two of them finished before the threshold and the
 * third reached it with about a poll to spare. The panel showed a spinner and a
 * count of seconds for eleven of the twelve. Three seconds is past the point
 * where a search reads as slow and short enough to still be explaining
 * something the user is waiting on.
 */
const SEARCH_EXPLAIN_AFTER_SECONDS = 3;
// And the first read happens as soon as the threshold is crossed rather than
// after another interval, so what it says arrives while the wait is still on.
const SEARCH_PROGRESS_POLL_MS = 2000;

/**
 * What this screen calls the search it just started.
 *
 * The backend keeps one progress record and nothing in it forbids two searches
 * at once, so a reader with no name for its own search is simply told about
 * whichever started last. It authorizes nothing and identifies nobody: it only
 * has to be different from the last one this device made.
 */
function newSearchToken(): string {
  return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

// The status line has to stay one short row on a QAM panel.
// `flex: 1` with `minWidth: 0` is what keeps the mark beside it from widening
// the panel: the title gives up its own width to an ellipsis first.
const TITLE_TEXT: React.CSSProperties = { flex: "1 1 auto", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
// The title and its mark share one line, with the mark against the right edge.
const TITLE_ROW: React.CSSProperties = { display: "flex", alignItems: "center", gap: 6, minWidth: 0 };
// One line, like the title above it. It asked for an ellipsis without asking to
// stay on one line, so it wrapped instead: a row carrying a file name, a source,
// a date and a size ran to three or four lines, and how tall a page of results
// was depended on which results were on it. What it loses to the clamp it gets
// back under the ring, the same reveal Manage's rows use.
const SUBTLE_TEXT: React.CSSProperties = { fontSize: "0.8em", opacity: 0.7, minWidth: 0 };
// The block holding one result's two lines. Named because the row is a Steam
// button and the marquee rule needs an element of this plugin's own around it.
const RESULT_LINES: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 2, textAlign: "left", minWidth: 0 };

/**
 * How tall one result row is, for fitting a page to the screen.
 *
 * A constant because the row is now a constant: the title and the detail line
 * are each clamped to one line and revealed under the ring instead of wrapping,
 * so a row carrying a file name, a source, a date and a size is the same height
 * as a row carrying a title alone. It used to be neither, which is why a page
 * of six results took a different amount of the screen on every search.
 */
const RESULT_ROW_HEIGHT = 58;

/**
 * The fewest results a page may hold before the measurement stops taking any.
 *
 * A search that returns three rows a page is still a search. One that returns
 * one is a list the reader pages through rather than reads.
 */
const MIN_RESULT_ROWS = 3;

// One empty set for every render that was given none, rather than a fresh one
// per render for a default that is only ever asked whether it holds something.
const EMPTY_ARTIFACT_SET: ReadonlySet<string> = new Set();
const EMPTY_LOCAL_TABLES: readonly TableStatus[] = [];

/**
 * What a row already is, said as a mark rather than as the first of nine
 * fields.
 *
 * "Local" and the retirement marks are the only part of that line that decides
 * whether the row can be pressed at all, and they were the first word of a
 * run-on sentence of file name, source, date and size, set in the same dimmed
 * grey as the rest of it. A filled chip is what the eye finds without reading,
 * which is the whole job here: a list of twenty rows is scanned for the ones
 * that are already on the device or already known not to work.
 *
 * It sits at the right of the title's own line rather than on a line of its
 * own, so a marked row is exactly as tall as an unmarked one and the column of
 * marks is in one place to scan down. Nothing about it may widen the panel: it
 * is the fixed half of that line and the title is the half that gives way.
 */
const ROW_MARK_BASE: React.CSSProperties = {
  flex: "0 0 auto",
  padding: "0 6px",
  borderRadius: 3,
  fontSize: "0.8em",
  lineHeight: "16px",
  fontWeight: 700,
  letterSpacing: "0.3px",
  textTransform: "uppercase",
  whiteSpace: "nowrap",
};
// The accent this product marks its own emphasis with, declared on the panel so
// one line changes it everywhere; the literal is the fallback for a tree that
// is somehow rendered outside that panel.
const ROW_MARK_LOCAL: React.CSSProperties = {
  ...ROW_MARK_BASE,
  background: "var(--ce-accent, hsla(203, 89%, 66%, 0.85))",
  color: "hsla(0, 0%, 0%, 0.86)",
};
// A row that cannot be pressed is stated in the colour of a refusal and not in
// the accent, which on this screen means "here it is already".
const ROW_MARK_REFUSED: React.CSSProperties = {
  ...ROW_MARK_BASE,
  background: "hsla(9, 74%, 62%, 0.82)",
  color: "hsla(0, 0%, 0%, 0.86)",
};
// The same accent, hollow, because it is the weaker of the two things this
// column says. Filled means the press costs nothing; outlined means a table on
// this device came from this row and the download still has to happen, which
// has to look like less than "Local" without looking like a refusal.
const ROW_MARK_IMPORTED: React.CSSProperties = {
  ...ROW_MARK_BASE,
  padding: "0 5px",
  boxShadow: "inset 0 0 0 1px var(--ce-accent, hsla(203, 89%, 66%, 0.85))",
  color: "var(--ce-accent, hsla(203, 89%, 66%, 0.85))",
};

/**
 * The whole of what a retired row says, in the two words a chip holds.
 *
 * A user scanning a list of twenty acts differently on each of these: a table
 * that ran and did not work may work again after the game updates, bytes that
 * were never a table never will, and a file the source no longer has is not
 * about this device at all. One shared "marked as not working" said none of
 * that, and repeated the word "marked" on a row whose greying already says it.
 */
const BLOCKED_MARK_TEXT: Record<BlockedTableCause, string> = {
  refused: "Failed",
  unusable: "Not a table",
  // Not "Not a table": these bytes may well hold a good one, and the user can
  // do something about it, which is exactly what a separate status is for.
  encrypted: "Encrypted",
  gone: "Gone",
  // An entry from a build that did not record the cause, and a cause a later
  // backend knows and this panel does not. Both are true and neither is exact.
  unknown: "Not working",
};

/** Publication date, which is what tells a user whether a table is current. */
function formatPosted(value: string | null): string | null {
  if (!value) return null;
  const posted = new Date(value);
  if (Number.isNaN(posted.getTime())) return null;
  return posted.toISOString().slice(0, 10);
}

/**
 * When a table already on this device arrived, as a date and nothing finer.
 *
 * The download where there was one, and otherwise when the file was opened
 * here. The same rule Manage's rows use, because the reader comparing a local
 * row against the source rows beside it is comparing the same thing.
 */
function localArrived(table: TableStatus): string | null {
  const origin = table.origins.length ? table.origins[table.origins.length - 1] : null;
  return formatPosted(origin?.retrieved_at ?? table.imported_at ?? null);
}

function formatSize(value: number | null): string {
  if (value === null) return "size unknown";
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

interface CachedSearch {
  results: CatalogResult[];
  failures: Array<{ provider: string; error: string; handoff_url?: string | null }>;
  sources: ProviderSearchSummary[];
  searchedAt: number;
  /** The source-selection revision this outcome was searched under. */
  revision: number;
}

// A provider search costs seconds of network work, and the modal remounts every
// time it is opened. Reuse the last outcome for the same game and query so
// reopening is instant; refreshing stays an explicit action.
const SEARCH_CACHE_LIMIT = 8;
const searchCache = new Map<string, CachedSearch>();

/**
 * Which set of table sources every cached outcome here was searched under.
 *
 * This cache is keyed by game and query alone, which was the whole of search
 * identity until the user could choose sources. Switching one off then left
 * reopening Search restoring rows from a source that is no longer asked, still
 * looking downloadable and only refused by the backend on the press; switching
 * one back on left the roster saying it was off until an explicit new search.
 *
 * Clearing on every change covers what is already stored. The revision covers
 * what is still in flight: a search that began before the change is stamped
 * with the revision it began under, so the outcome it writes afterwards is not
 * restored as though it had been searched under the new choice.
 */
let sourceSelectionRevision = 0;

/** Forget every cached outcome, because the set of sources searched has changed. */
export function forgetSearchOutcomes(): void {
  sourceSelectionRevision += 1;
  searchCache.clear();
}

/** The query half of a scope key, so a retry re-searches what produced the row. */
function queryForScope(
  scope: string, gameIdentity: string, shortcutExecutable: string | null | undefined,
): string | null {
  const prefix = `${gameIdentity}\u0000`;
  const suffix = `\u0000${shortcutExecutable ?? ""}`;
  if (!scope.startsWith(prefix) || !scope.endsWith(suffix)) return null;
  const value = scope.slice(prefix.length, scope.length - suffix.length);
  return value || null;
}

function cacheKey(gameIdentity: string, query: string, shortcutExecutable: string | null | undefined): string {
  // The backend may retry an abbreviated library name against the install
  // directory. That makes the shortcut target part of search identity: reusing
  // an outcome after the target path changes can return tables for the old game.
  return `${gameIdentity}\u0000${query.trim().toLowerCase()}\u0000${shortcutExecutable ?? ""}`;
}

function rememberSearch(key: string, value: CachedSearch): void {
  searchCache.delete(key);
  searchCache.set(key, value);
  while (searchCache.size > SEARCH_CACHE_LIMIT) {
    const oldest = searchCache.keys().next();
    if (oldest.done) break;
    searchCache.delete(oldest.value);
  }
}

function formatAge(milliseconds: number): string {
  const minutes = Math.floor(milliseconds / 60_000);
  if (minutes < 1) return "just now";
  if (minutes === 1) return "1 minute ago";
  if (minutes < 60) return `${minutes} minutes ago`;
  const hours = Math.floor(minutes / 60);
  return hours === 1 ? "1 hour ago" : `${hours} hours ago`;
}

/** Results CE Decky can acquire on its own; a controller cannot browse the web. */
const AUTOMATIC_DOWNLOAD_MODES = new Set(["direct_https"]);

function isAutomatic(result: CatalogResult): boolean {
  return AUTOMATIC_DOWNLOAD_MODES.has(result.download_mode);
}

function isTerminalAcquisition(status: AcquisitionStatus): boolean {
  return status.state === "imported" || status.state === "cancelled" || status.state === "failed";
}

interface LocalTableChoice {
  sha256: string;
  filename: string;
  size: number | null;
}

/**
 * How a saved table and the row offering it are known to be related.
 *
 * A boolean said only whether a digest matched, and the screen then described
 * every other case as a provider checksum that matched, on sources that publish
 * no checksum at all. The three cases are told apart because their sentences
 * are not variations of one sentence: what is proven, what is merely known, and
 * what is not being compared to anything.
 */
type TableProvenance =
  /** The provider published a checksum and it matches this exact local copy. */
  | "digest_match"
  /**
   * This table was downloaded from this provider row before.
   *
   * The source publishes no checksum, so nothing establishes that the row still
   * serves the same bytes. It is still the copy this row produced, it is still
   * on this device, and offline it is the only one there is: the uncertainty is
   * said out loud and the choice stays the user's.
   */
  | "origin_copy"
  /**
   * The same, on a source that does publish a checksum and now publishes another.
   *
   * Saying no checksum exists here would be false: one does, and it names bytes
   * that are not these. That is a stronger thing to be able to tell the reader
   * than the absence of any proof, and it is the difference between "nobody can
   * say" and "the online version has changed".
   */
  | "different_digest"
  /** A saved table being opened on its own, with no provider row in play. */
  | "local_only";

interface ExistingTableChoiceProps {
  table: LocalTableChoice;
  provenance: TableProvenance;
  /** The saved copy may be used. False only for an exact block on its own SHA. */
  canUse: boolean;
  canDownload: boolean;
  /** Why the saved copy cannot be used, when it cannot. */
  useBlockedReason?: string | null;
  /**
   * Why the download cannot run, when it cannot.
   *
   * A source that no longer has the file, bytes that were not a table, or a
   * transfer this session already found damaged. None of them says anything
   * about the copy already on this device, which is the half that still works.
   */
  downloadBlockedReason?: string | null;
  /**
   * Clear what is stopping the download, from here.
   *
   * The list's own Retry counts the rows it is showing a mark on, and this
   * refusal is one the row deliberately does not show: it is about the revision
   * the source is offering now, while the row is presenting the copy this
   * device holds. Absent when there is nothing of that kind to clear.
   */
  onRetryDownload?: (() => void) | null;
  onUse: () => void;
  onDownload: () => void;
  onClose: () => void;
}

const PROVENANCE_TEXT: Record<TableProvenance, { label: string; description: string }> = {
  digest_match: {
    label: "Use the saved copy or ask the provider again",
    description: "The provider's own checksum matches this copy, so downloading again returns the same bytes. Use saved copy is the default and works offline.",
  },
  origin_copy: {
    label: "Use the copy you already downloaded, or ask the source again",
    description: "This is a copy you downloaded from this source before. This source does not provide a checksum, so CE Decky cannot tell whether the online version has changed. You can use this saved copy now or download it again.",
  },
  different_digest: {
    label: "Use the copy you already downloaded, or fetch the current version",
    description: "This is a copy you downloaded from this source before. The source now advertises a different checksum for the online version. You can use this saved copy now or download the current version.",
  },
  local_only: {
    label: "Use the saved copy or ask its source again",
    description: "This table is on this device and needs no network. Download again asks its source for the file once more, and bytes that differ are validated and reviewed as a new exact table.",
  },
};

/**
 * The same window with the saved copy's own press off.
 *
 * Identity and provenance only: a copy the user has recorded as not working is
 * still exactly the bytes it was, and every one of these used to end by saying
 * it could be used now, two rows above the disabled press that would have done
 * it. The row under this one carries the reason and the way to lift it.
 */
const BLOCKED_PROVENANCE_TEXT: Record<TableProvenance, string> = {
  digest_match: "The provider's own checksum matches this copy, so downloading again returns the same bytes. Clear the mark on this copy before it can be used again.",
  origin_copy: "This is a copy you downloaded from this source before. This source does not provide a checksum, so CE Decky cannot tell whether the online version has changed. Clear the mark on this copy before it can be used again.",
  different_digest: "This is a copy you downloaded from this source before, and the source now advertises a different checksum for the online version. Clear the mark on this copy before it can be used again, or download the current version.",
  local_only: "This table is on this device and needs no network. Clear the mark on this copy before it can be used again.",
};

/** One small decision before network work replaces bytes already on the device. */
function ExistingTableChoiceModal({
  table, provenance, canUse, canDownload, useBlockedReason, downloadBlockedReason, onRetryDownload,
  onUse, onDownload, onClose,
}: ExistingTableChoiceProps) {
  useUiSurface("ExistingTableChoiceModal", table.sha256);
  const existingRef = useRef<HTMLDivElement | null>(null);
  const downloadRef = useRef<HTMLDivElement | null>(null);
  const retryRef = useRef<HTMLDivElement | null>(null);
  // The ring opens on the copy, which is the press this window exists for and
  // the one that works with no network. Where that copy is the blocked half it
  // opens on the download instead, so the window never opens on a dead control.
  //
  // Said twice, because once was not enough. `preferredFocus` is what Steam
  // reads when it places the ring itself, and this effect is what puts it right
  // if Steam has already placed it: a mount effect runs before Steam's own
  // assignment, so on its own it was overwritten and the window opened on
  // whichever button came first, marked as unavailable and unable to answer A.
  //
  // Retry is last and is reached the same way, because a window whose two
  // presses are both off is exactly the window that opens on a dead control:
  // it is then the only way out of the state the user is in, and a press
  // nothing can reach is no press at all.
  useEffect(() => { focusFirstEnabled(existingRef, downloadRef, retryRef); }, [canUse, canDownload, onRetryDownload]);
  const text = PROVENANCE_TEXT[provenance];
  // What the copy is, never what can be done with it, while what can be done
  // with it is off. The window was disabling Use saved copy and explaining, two
  // rows above, that the saved copy could be used now.
  const description = useBlockedReason ? BLOCKED_PROVENANCE_TEXT[provenance] : text.description;
  return (
    <ModalRoot onCancel={traceUiAction("catalog.saved_choice.cancel", onClose, { table_sha: table.sha256 })}>
      <Focusable style={{ minWidth: 420, maxWidth: 620 }}>
        <PanelSection>
          <SectionHeading>Table already on this device</SectionHeading>
          <PanelRow
            tone="header"
            truncate
            label={table.filename}
            description={`${table.sha256.slice(0, 12)} · ${formatSize(table.size)}`}
          />
          <PanelRow truncate label={text.label} description={description} />
          {/* Why half of this window is off, said where the button is rather
              than by a greyed control with nothing beside it. A blocked
              download and a blocked copy are separate records about separate
              bytes, and either one leaves the other press working. */}
          {downloadBlockedReason ? (
            <PanelRow
              truncate
              label="Downloading again is not available"
              description={downloadBlockedReason}
              actions={onRetryDownload ? (
                <div ref={retryRef} style={CONTENTS_ONLY}>
                  <DialogButton
                    style={modalActionStyle}
                    preferredFocus={!canUse && !canDownload}
                    onClick={traceUiAction("provider_catalog.retry_download", onRetryDownload, { table_sha: table.sha256, provenance })}
                  >Retry</DialogButton>
                </div>
              ) : undefined}
            />
          ) : null}
          {useBlockedReason ? (
            <PanelRow truncate label="This saved copy is marked as not working" description={useBlockedReason} />
          ) : null}
        </PanelSection>
        <ModalActions>
          <div ref={existingRef} style={CONTENTS_ONLY}>
            <DialogButton style={modalActionStyle} preferredFocus={canUse} disabled={!canUse} onClick={traceUiAction("provider_catalog.use_saved_copy", onUse, { table_sha: table.sha256, provenance })}>Use saved copy</DialogButton>
          </div>
          <div ref={downloadRef} style={CONTENTS_ONLY}>
            <DialogButton style={modalActionStyle} preferredFocus={!canUse && canDownload} disabled={!canDownload} onClick={traceUiAction("provider_catalog.download_again", onDownload, { table_sha: table.sha256, provenance })}>Download again</DialogButton>
          </div>
        </ModalActions>
      </Focusable>
    </ModalRoot>
  );
}

type CatalogRow =
  | { kind: "provider"; result: CatalogResult; localTable: LocalTableChoice | null; provenance: TableProvenance }
  | { kind: "local"; table: TableStatus };

export function ProviderCatalog({
  gameIdentity,
  gameName,
  artifactResolutions = [],
  compatibility = [],
  onRefreshProvenance,
  appId,
  shortcutExecutable,
  initialQuery,
  autoSearch = false,
  localArtifacts = {},
  localTables = EMPTY_LOCAL_TABLES,
  deviceTables = EMPTY_LOCAL_TABLES,
  importedArtifacts = EMPTY_ARTIFACT_SET,
  footerActions,
  blockedTables = {},
  blockedArtifacts = {},
  onClearMarks,
  onRefreshBlocked,
  onLocalSelected,
  onImported,
}: Props) {
  useUiSurface("ProviderCatalog", appId);
  const [query, setQuery] = useState(initialQuery ?? gameName);
  const [results, setResults] = useState<CatalogResult[]>([]);
  const [resultPage, setResultPage] = useState(0);
  const [failures, setFailures] = useState<Array<{ provider: string; error: string; handoff_url?: string | null }>>([]);
  const [sources, setSources] = useState<ProviderSearchSummary[]>([]);
  const [busy, setBusy] = useState(false);
  const [searchFinished, setSearchFinished] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searchedAt, setSearchedAt] = useState<number | null>(null);
  const [rejectedGeneration, setRejectedGeneration] = useState(0);
  // The provider rows this screen has imported from while it has been open.
  //
  // The marks it is handed are read once, when it opens: a detached modal never
  // sees its props change. That is normally enough, because a successful import
  // goes straight to Review and closes this screen. It is not enough when
  // something after the import fails, which is exactly when the user comes back
  // here: the table is on the device, this screen does not know, and the row
  // that produced it offers the same download over again.
  const [importedHere, setImportedHere] = useState<ReadonlySet<string>>(EMPTY_ARTIFACT_SET);
  const noteImportedFrom = (status: AcquisitionStatus) => {
    setImportedHere((rows) => new Set(rows).add(`${status.provider}:${status.artifact_id}`));
  };
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  // What the running search is doing, read from the backend rather than guessed
  // at. Only asked for once the wait is long enough to be worth explaining.
  const [progress, setProgress] = useState<TableSearchProgress | null>(null);
  // A search specifically, rather than the busy flag every press on this screen
  // sets: a download is also a long wait, and asking the backend what a search
  // is doing while one runs is a read per two seconds that answers nothing.
  const [searching, setSearching] = useState(false);
  // The name of the search being watched, as state rather than as a ref: it is
  // half of what the progress read is, so the read has to be rebuilt when it
  // changes. Held in a ref, a context switch that left `searching` true across
  // the change never re-ran the effect, and the poll went on asking for the
  // search of the game the user had just left while the new game's search ran
  // with nothing to show for itself.
  const [searchToken, setSearchToken] = useState<string | null>(null);
  const identityRef = useRef(`${gameIdentity}\u0000${gameName}\u0000${shortcutExecutable ?? ""}`);
  const contextGenerationRef = useRef(0);
  const busyRef = useRef(false);
  // The download window this screen opens, and closes when it leaves.
  const ownedModalCloseRef = useRef<(() => void) | null>(null);
  // The acquisition that window is holding, while it is holding one.
  //
  // The window cancels what it started when it unmounts, which is the ordinary
  // way a download ends. This is the case that is not ordinary: the panel can
  // change which game it is on while the window is open, and closing a modal
  // is asking Steam to unmount it rather than unmounting it, so the guarantee
  // that leaving a game takes its download with it should not rest on when that
  // happens. It is cleared as soon as the window closes, so it never names an
  // acquisition that has already imported.
  const ownedAcquisitionRef = useRef<string | null>(null);
  const dropOwnedAcquisition = () => {
    const acquisitionId = ownedAcquisitionRef.current;
    ownedAcquisitionRef.current = null;
    ownedModalCloseRef.current?.();
    ownedModalCloseRef.current = null;
    if (acquisitionId) void cancelTableAcquisition(acquisitionId).catch(() => undefined);
  };

  // Retirement marks belong to the exact cached result snapshot that proved the
  // bytes damaged, so refreshing another game's search cannot resurrect them.
  // The identity of the results on screen, not of the text field. Editing the
  // query does not replace the displayed result set until Search is pressed, so
  // deriving this from `query` moved retirement marks and the expired-authority
  // retry onto a search that had not run: a damaged artifact from the visible
  // results was recorded under the half-typed query, and the retry re-searched
  // that text instead of the one that produced the row the user clicked. Only a
  // completed search replaces both the results and their scope.
  const [searchScope, setSearchScope] = useState(() => cacheKey(gameIdentity, initialQuery ?? gameName, shortcutExecutable));
  // The record as this screen currently understands it. Seeded from the props
  // the modal was opened with and re-read whenever this screen is the thing
  // that changed it, because a detached tree is never handed new props.
  const [blockedView, setBlockedView] = useState<BlockedLookups>(
    () => ({ byDigest: blockedTables, byArtifact: blockedArtifacts }),
  );
  const [resolutionView, setResolutionView] = useState(artifactResolutions);
  // A record that a table did not work suppresses its own green here as well as
  // in the backend, so the screen agrees with itself the moment a mark is
  // written. Only that kind of record: a statement about a download or about a
  // source answers nothing about a table that loaded and worked.
  const [compatibilityView, setCompatibilityView] = useState<readonly CompatibilityEvidence[]>(
    compatibility.map((entry) => isCompatibilityFailure(blockedTables[entry.table_sha256]) ? { ...entry, invalidated: true } : entry),
  );
  const refreshBlocked = async () => {
    const generation = contextGenerationRef.current;
    if (onRefreshProvenance) {
      const resolutions = await onRefreshProvenance().catch((cause) => {
        logUiFailure("catalog.resolution_refresh_failed", cause);
        return null;
      });
      if (resolutions && generation === contextGenerationRef.current) {
        setResolutionView(resolutions.resolutions);
        setCompatibilityView(resolutions.compatibility);
      }
    }
    if (!onRefreshBlocked || generation !== contextGenerationRef.current) return;
    // Advisory: a lookup that cannot be re-read leaves the previous one in
    // place rather than failing the action that asked for it.
    const next = await onRefreshBlocked().catch(() => null);
    if (next && generation === contextGenerationRef.current) {
      setBlockedView(next);
      setCompatibilityView((entries) => entries.map((entry) => isCompatibilityFailure(next.byDigest[entry.table_sha256]) ? { ...entry, invalidated: true } : entry));
    }
  };

  useLayoutEffect(() => {
    const identity = `${gameIdentity}\u0000${gameName}\u0000${shortcutExecutable ?? ""}`;
    if (identityRef.current === identity) return;
    // A layout effect runs in the same commit before promise continuations can
    // resume after rerender. Invalidate old async work here rather than in a
    // passive effect so a late acquisition cannot be adopted by the new game.
    identityRef.current = identity;
    contextGenerationRef.current += 1;
    dropOwnedAcquisition();
    busyRef.current = false;
    setBusy(false);
    setSearchFinished(false);
    setQuery(initialQuery ?? gameName);
    setResults([]);
    setResultPage(0);
    setFailures([]);
    setSources([]);
    setError(null);
    setSearchedAt(null);
    // The search this screen was watching belongs to the game it was opened
    // for. A layout effect is where that has to be dropped, because the auto
    // search for the new game starts from a passive effect in this same commit.
    setSearching(false);
    setSearchToken(null);
    setProgress(null);
  }, [gameIdentity, gameName, shortcutExecutable, initialQuery]);

  // A provider search is tens of seconds of somebody else's network, so report
  // the elapsed time rather than an invented completion figure.
  useEffect(() => {
    if (!busy) {
      setProgress(null);
      setSearching(false);
      return;
    }
    const started = Date.now();
    setElapsedSeconds(0);
    const timer = window.setInterval(() => setElapsedSeconds(Math.round((Date.now() - started) / 1000)), 500);
    return () => window.clearInterval(timer);
  }, [busy]);

  // And once it has been long enough that a user is entitled to wonder, say
  // what each source is actually doing. Deliberately not from the first second:
  // an ordinary search answers well inside this, and a line that appears and
  // disappears is more distracting than the spinner it sits beside. It is one
  // bounded read every couple of seconds, and a read that fails leaves the
  // elapsed time on screen rather than replacing it with an error: nothing
  // about the search itself depends on this.
  const explaining = searching && elapsedSeconds >= SEARCH_EXPLAIN_AFTER_SECONDS;
  useEffect(() => {
    if (!explaining || !searchToken) return;
    const token = searchToken;
    let live = true;
    let timer: number | undefined;
    // One entry per search, not one per poll: the backend being unreachable is
    // the same fact fifteen times over a long search, and the panel's own log
    // is a bounded ring that the support bundle carries.
    let reported = false;
    const read = async () => {
      try {
        const answer = await pollTableSearch(token);
        // A record that is not running belongs to a search that has ended, and
        // the one on this screen has not: the backend names the sources when
        // the jobs are built, so an answer from before that describes the
        // previous search and must not be shown as this one.
        if (live) setProgress(answer?.running ? answer : null);
      } catch (cause) {
        if (!reported) {
          reported = true;
          logUiWarning("catalog.search_progress_unreadable", { reason: describeError(cause) });
        }
        if (live) setProgress(null);
      }
      // Scheduled after the answer has landed rather than on a fixed interval.
      // A read slower than the interval would otherwise have another started
      // beside it, and two answers can arrive in the other order and put an
      // older set of stages back on the screen; a backend that has stalled
      // would also collect one outstanding call every two seconds.
      if (live) timer = window.setTimeout(() => void read(), SEARCH_PROGRESS_POLL_MS);
    };
    void read();
    return () => {
      live = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [explaining, searchToken]);

  // Closing the window is what cancels the download: it owns the acquisition
  // and cancels what it started when it unmounts, so this screen leaving takes
  // the download with it without knowing anything about it.
  useEffect(() => () => {
    contextGenerationRef.current += 1;
    dropOwnedAcquisition();
  }, []);

  // A controller cannot usefully browse the web, so only results CE Decky can
  // download itself are offered. The rest are still counted, so a provider that
  // silently stops returning usable results is visible rather than absent.
  const automaticResults = useMemo(() => results.filter(isAutomatic), [results]);
  // A provider row and a local table are related in two different ways, and the
  // difference is what the press is told, never what the press is allowed to do.
  // An advertised digest proves the bytes are the same ones. An origin says only
  // that this row produced this local table before, and the same artifact ID can
  // serve changed bytes, so it proves nothing about what the row holds now.
  //
  // It still offers the copy. Two of the four sources publish no digest at all,
  // and a user who has already downloaded a table from one of them, and is now
  // offline, is a user who owns those bytes: refusing to reach them because the
  // source declines to publish a checksum decides something that is theirs to
  // decide. What the missing proof changes is the wording of the choice, which
  // says the bytes cannot be shown to be the same and offers the download.
  const localBySha = useMemo(
    () => new Map(
      [...deviceTables, ...localTables].filter((table) => table.available).map((table) => [table.sha256, table]),
    ),
    [deviceTables, localTables],
  );
  // The newest table this row produced, per row. Deterministic on purpose: the
  // first one encountered was whichever the backend happened to list first, so
  // one post that served several revisions offered an arbitrary one of them.
  const localByOrigin = useMemo(() => {
    const newest = new Map<string, { table: TableStatus; retrievedAt: number }>();
    for (const table of localTables) {
      if (!table.available) continue;
      for (const origin of table.origins) {
        const key = `${origin.provider}:${origin.artifact_id}`;
        const retrievedAt = Date.parse(origin.retrieved_at);
        const at = Number.isFinite(retrievedAt) ? retrievedAt : 0;
        const held = newest.get(key);
        if (!held || at > held.retrievedAt) newest.set(key, { table, retrievedAt: at });
      }
    }
    return new Map([...newest].map(([key, held]) => [key, held.table]));
  }, [localTables]);
  const resolvedResults = useMemo(() => {
    const mappings = new Map(resolutionView.map((entry) =>
      [`${entry.provider}:${entry.artifact_id}:${entry.artifact_sha256}`, entry.table_sha256]));
    return new Map(automaticResults.map((result) => {
      const advertised = result.advertised_sha256?.toLowerCase();
      const digest = !advertised ? null
        : mappings.get(`${result.provider}:${result.artifact_id}:${advertised}`)
          ?? localArtifacts[`sha:${advertised}`]
          ?? (!isArchiveFilename(result.filename) ? advertised : null);
      return [result, digest] as const;
    }));
  }, [automaticResults, resolutionView, localArtifacts]);
  const resolvedDigest = (result: CatalogResult) => resolvedResults.get(result) ?? null;
  const sameContentOrigins = useMemo(() => {
    const origins = new Map<string, Set<string>>();
    const add = (digest: string, origin: { provider: string; artifact_id: string }) => {
      const keys = origins.get(digest) ?? new Set<string>();
      keys.add(`${origin.provider}:${origin.artifact_id}`);
      origins.set(digest, keys);
    };
    for (const table of localBySha.values()) for (const origin of table.origins) add(table.sha256, origin);
    for (const entry of resolutionView) add(entry.table_sha256, entry);
    for (const [result, digest] of resolvedResults) if (digest) add(digest, result);
    return origins;
  }, [localBySha, resolutionView, resolvedResults]);
  /**
   * Everything one provider row passively says, decided in one place.
   *
   * The row, the chip beside it and the Retry count above the list were each
   * working it out again from the records, and they drifted: a row could carry
   * a mark the list counted and the row itself never showed, and a duplicate
   * glyph calculated from the revision the source is offering could sit beside
   * a compatibility mark and a Local about the copy this device holds. What a
   * press can act on has to be what the user can see, so it is derived once.
   */
  const providerRowMarks = (result: CatalogResult, localTable: LocalTableChoice | null) => {
    const refusals = rowRefusals(result, localTable);
    const { damaged, presented } = refusals;
    const blocked = presented;
    // Availability is physical and is not an advisory's to take away: bytes that
    // are on this device are on it whatever any record says about them. A copy
    // in the library has been imported and verified as a table, so a record
    // saying some download was not one, or that its archive is locked, or that
    // a source no longer serves it, cannot redefine the copy as absent. Those
    // are about the download side and belong to the press that goes there.
    const localAvailable = Boolean(localTable);
    // One exact table per mark, and both halves of what it says come from it. A
    // row can be about two sets of bytes at once: the copy this device holds
    // and the revision the source is offering now. Taking the failure from one
    // and the success from the other described neither, and could paint a
    // revision nobody has tried in the colour of an older one that failed. The
    // copy the row is presenting is the subject where there is one, because
    // that is the table the press in front of the user is about; otherwise it
    // is what the source says it would serve.
    const subjectDigest = localTable ? localTable.sha256 : resolvedDigest(result);
    const subjectRecord = subjectDigest ? blockedView.byDigest[subjectDigest.toLowerCase()] ?? null : null;
    // A row that can name no exact table at all has no positive claim for a
    // record to contradict, and the record it carries is then the only thing
    // there is to say about it. That is the ordinary case: most sources publish
    // no checksum, and a mark recorded against the row is what stops the same
    // table being downloaded and tried again every search.
    const failureRecord = subjectDigest
      ? subjectRecord
      : (isCompatibilityFailure(blocked) ? blocked : null);
    const carried = isCompatibilityFailure(failureRecord) ? failureRecord : null;
    // Said once. The chip drops only the record the glyph is already carrying;
    // a record about other bytes, such as a refused newer revision beside a
    // saved copy that works, is a different statement and is explained in the
    // window rather than in the row's one chip.
    const condition = blocked && !(carried && blocked.sha256 === carried.sha256 && blocked.cause === carried.cause)
      ? blocked
      : null;
    const artifactKey = `${result.provider}:${result.artifact_id}`;
    const importedBefore = importedArtifacts.has(artifactKey) || importedHere.has(artifactKey);
    const chip = localAvailable
      ? { text: "Local", style: ROW_MARK_LOCAL }
      : condition
        ? { text: BLOCKED_MARK_TEXT[condition.cause], style: ROW_MARK_REFUSED }
        : damaged
          ? { text: "Damaged", style: ROW_MARK_REFUSED }
          : importedBefore
            ? { text: "Imported", style: ROW_MARK_IMPORTED }
            : null;
    // Same-content provenance is about the same table as the marks beside it.
    // Where the subject is the saved copy, that copy's identity is this device's
    // own; where it is the revision the source names, the resolution proves it.
    // A saved copy never speaks for bytes a source is advertising and nobody has
    // resolved, because an older copy is no evidence about those.
    const duplicateSubject = localTable ? localTable.sha256 : resolvedDigest(result);
    const duplicate = Boolean(duplicateSubject && (sameContentOrigins.get(duplicateSubject)?.size ?? 0) > 1);
    // What a press that says "try these again" may act on: exactly the records
    // this row is putting in front of the user. A row presenting a stored copy
    // is showing that copy's mark and nothing else, whatever else it carries
    // about the revision the source is offering now, and this device's memory
    // that the last download from the row was damaged goes with the chip that
    // says so rather than with a mark about other bytes entirely. What is not
    // shown is cleared from the window that names the two revisions apart.
    const shown = (localAvailable ? [carried] : [carried, condition])
      .filter((mark): mark is BlockedMark => Boolean(mark));
    const clearDamaged = !localAvailable && damaged;
    const visibleFailure = shown.length > 0 || clearDamaged;
    return { ...refusals, artifactKey, localAvailable, subjectDigest, failureRecord, condition, chip, duplicate,
             clearTargets: shown, clearDamaged, visibleFailure };
  };

  const catalogRows = useMemo<CatalogRow[]>(() => {
    const represented = new Set<string>();
    const providerRows = automaticResults.map((result): CatalogRow => {
      const artifactKey = `${result.provider}:${result.artifact_id}`;
      const finalSha = resolvedDigest(result);
      const exactSha = finalSha && localBySha.has(finalSha) ? finalSha
        : result.advertised_sha256 ? localArtifacts[`sha:${result.advertised_sha256.toLowerCase()}`] : undefined;
      const exactLocal = exactSha
        ? localBySha.get(exactSha) ?? { sha256: exactSha, filename: result.filename, size: result.size_bytes }
        : undefined;
      const local = exactLocal ?? localByOrigin.get(artifactKey) ?? null;
      // Only the one table the row actually offers is folded into it. Every
      // other revision this row produced keeps its own local row, so a post that
      // has served several of them over time still reaches all of them.
      if (local) represented.add(local.sha256);
      return {
        kind: "provider",
        result,
        localTable: local,
        // Three cases, not two. A source that publishes a checksum which does
        // not resolve to this copy has said something about the online version;
        // one that publishes none has said nothing at all, and telling the
        // reader the second when the first is true is simply wrong.
        provenance: exactLocal
          ? "digest_match"
          : result.advertised_sha256 ? "different_digest" : "origin_copy",
      };
    });
    const localRows = (searchFinished ? localTables : EMPTY_LOCAL_TABLES)
      .filter((table) => table.available && !represented.has(table.sha256))
      .map((table): CatalogRow => ({ kind: "local", table }));
    return [...providerRows, ...localRows];
  }, [automaticResults, localArtifacts, localByOrigin, localBySha, localTables, searchFinished, resolvedResults]);
  // What the sources are doing while the search is still running, in the same
  // short form the finished tally uses. Sources that have answered are counted
  // rather than listed one by one: a user waiting wants to know what is still
  // out, and the row has to stay readable on a quick-access panel.
  const searchProgressText = useMemo(() => {
    if (!progress) return "";
    const count = (state: string) => progress.sources.filter((source) => source.state === state).length;
    const parts = progress.sources
      .filter((source) => source.state === "running")
      .map((source) => {
        const name = providerShortName(source.provider, source.name);
        return source.stage ? `${name}: ${source.stage}` : name;
      });
    const done = count("done");
    const failed = count("failed");
    // A source the user switched off is named for the same reason the finished
    // tally names it: a deliberately narrowed search otherwise looks like a
    // build that never had those sources, and this is the line that is on
    // screen while the narrowing is actually costing the user something.
    const off = count("off");
    if (done > 0) parts.push(`${done} answered`);
    if (failed > 0) parts.push(`${failed} could not`);
    if (off > 0) parts.push(`${off} off`);
    return parts.join(" \u00b7 ");
  }, [progress]);

  // How many sources this search is actually asking. The completed roster
  // beside it describes the search before this one, which is a different set
  // whenever the user has switched one off since.
  const searchingSourceCount = progress
    ? progress.sources.filter((source) => source.state !== "off").length
    : 0;

  // Every searched source is listed with its own count, including the ones that
  // answered with nothing: a provider that quietly stops returning results is
  // otherwise indistinguishable from a game that simply has no tables. The
  // backend supplies the roster because a source that neither produced a result
  // nor failed leaves no trace in either list.
  const sourceSummary = useMemo(() => {
    const shortName = (provider: string, fallback: string) => providerShortName(provider, fallback);
    const automaticByProvider = new Map<string, number>();
    const namesByProvider = new Map<string, string>();
    for (const result of results) {
      namesByProvider.set(result.provider, result.provider_display_name || result.provider);
      if (isAutomatic(result)) automaticByProvider.set(result.provider, (automaticByProvider.get(result.provider) ?? 0) + 1);
    }
    // A cached outcome from before the roster existed still has to name its
    // failed sources, so fall back to whichever providers left a trace.
    const failed = new Map(failures.map((failure) => [failure.provider, failure.error]));
    const roster: ProviderSearchSummary[] = sources.length > 0
      ? sources
      : [...new Set([...namesByProvider.keys(), ...failed.keys()])].map((provider) => ({
        provider,
        provider_display_name: namesByProvider.get(provider) ?? provider,
        results: 0,
        status: failed.has(provider) ? "unavailable" : "ok",
        error: failed.get(provider) ?? null,
      }));
    return roster.map((source) => {
      const name = shortName(source.provider, source.provider_display_name || namesByProvider.get(source.provider) || source.provider);
      const automatic = automaticByProvider.get(source.provider) ?? 0;
      if (source.status === "indexing") {
        const progress = source.total_pages
          ? `${source.indexed_pages ?? 0}/${source.total_pages}`
          : `${source.indexed_pages ?? 0} page(s)`;
        return `${name}: indexing ${progress} · ${automatic}`;
      }
      // A source the user switched off is named and said to be off. Dropping
      // it would leave a deliberately narrowed search looking like a build that
      // never had those sources, which is exactly the question this line
      // exists to answer.
      if (source.status === "disabled") return `${name}: off`;
      if (source.status === "stale") return `${name}: ${automatic} · stale index`;
      // A source being told to wait is not a source with nothing for this game,
      // and both used to read the same. What the cached listing still knows is
      // in the row's own help text; the wait is what decides whether trying
      // again in a moment is worth it.
      if (source.status === "cooldown") {
        const wait = source.retry_after_seconds ? ` · retry in ${source.retry_after_seconds}s` : "";
        return `${name}: ${automatic} · rate limited${wait}`;
      }
      // A source can answer partly and then stop, and the rows it did return
      // are on the screen. Reporting that as `n/a` contradicted the results
      // immediately below it, and the count here is of fresh rows only, since
      // a stale fallback row is not offered as a direct download.
      if (source.status !== "ok") return automatic > 0 ? `${name}: ${automatic} · partial` : `${name}: n/a`;
      // Never let a roster/result disagreement render as a negative count.
      const withheld = Math.max(0, source.results - automatic);
      return `${name}: ${automatic}${withheld > 0 ? `+${withheld}` : ""}`;
    }).join(" · ");
  }, [results, sources, failures]);

  // A search that asked nothing at all, which is not the same as a search that
  // found nothing. Only true once a search has produced a roster: an empty
  // roster is a screen that has not searched yet. Rows on screen rule it out
  // too, because a stale row from a source no longer in the registry is not
  // switched off and would leave this contradicting the results beneath it.
  const allSourcesOff = useMemo(
    () => results.length === 0
      && sources.length > 0
      && sources.every((source) => source.status === "disabled"),
    [results, sources],
  );

  // Recomputed when an acquisition retires an artifact, including the detached
  // Playground modal that owns its own acquisition and only reports on close.
  const rejectedArtifactIds = useMemo(
    () => new Set(results
      .filter((result) => isRejectedArtifact(searchScope, result.provider, result.artifact_id))
      .map((result) => `${result.provider}:${result.artifact_id}`)),
    [results, rejectedGeneration, searchScope],
  );

  /**
   * Everything one provider row's durable records decide, worked out once.
   *
   * The badge, the two presses and the retry count are four readings of the
   * same records, and they were four separate readings: the row learned that a
   * revision it has outlived says nothing about it while the retry count went
   * on counting that revision, so the panel offered to clear a mark under a row
   * that was not carrying one. They are one answer now.
   */
  const rowRefusals = (result: CatalogResult, localTable: LocalTableChoice | null) => {
    const artifactKey = `${result.provider}:${result.artifact_id}`;
    const refusals = tableRowRefusals(blockedView, {
      advertisedSha256: resolvedDigest(result) ?? result.advertised_sha256,
      artifactKey,
      savedSha256: localTable?.sha256 ?? null,
    });
    const damaged = rejectedArtifactIds.has(artifactKey);
    // The same rule the press itself keeps: only a record about whether this
    // exact table works stops the copy being used. A row whose download half is
    // refused for a condition of the source or of the bytes still has an
    // offline half to offer, and retiring it took that away.
    const savedUsable = Boolean(localTable) && !isCompatibilityFailure(refusals.saved);
    const downloadDead = Boolean(refusals.download) || damaged;
    // What the row is actually carrying. History is the record of a revision
    // this row is not offering, so it says something only where there is
    // nothing here to offer instead: a row holding a usable copy says Local and
    // that history is invisible on it, which is exactly why it must not be
    // counted as a mark the user could be asked to clear.
    const presented = refusals.download ?? refusals.saved ?? (savedUsable ? null : refusals.history);
    return { ...refusals, artifactKey, damaged, savedUsable, downloadDead, presented };
  };

  /**
   * How many results this screen puts on one page, on the screen it is drawn on.
   *
   * `PROVIDER_PAGE_SIZE` is what a 1280x800 modal fits and stays the answer
   * wherever there is room for it. A Steam Deck gives a modal 534 CSS pixels
   * against a television's 844, and this screen's chrome is not a fixed block:
   * the query field, the per-source summary and the banner a challenged source
   * raises are all above the list and only sometimes there. So the room the
   * list has is measured rather than estimated, from the list's own top and the
   * footer under it, neither of which moves when the row count does.
   */
  const [listNode, setListNode] = useState<HTMLDivElement | null>(null);
  const [footerNode, setFooterNode] = useState<HTMLDivElement | null>(null);
  // Measured while the screen is in the state it settles in, and then held.
  //
  // This screen searches the moment it opens, and its own status rows sit above
  // the list while it does. Latching the first credible answer therefore sized
  // the page against a layout that exists for a few seconds: on a Steam Deck's
  // 534 pixel page that was a chrome of 371 and two results a page, against 289
  // and four once the results were in, for the same game on the same screen.
  // The search ending is this screen's own settled moment.
  const [chrome, setChrome] = useState<LatchedChrome | null>(null);
  useLayoutEffect(() => {
    setChrome((held) => latchChrome(held, listNode, footerNode, MODAL_BOTTOM_PADDING, !searching));
  }, [listNode, footerNode, searching, catalogRows.length]);
  // Divided by a row that was measured rather than by a constant beside the
  // stylesheet that draws it: a short screen trims a row's padding, and the
  // two numbers drift silently.
  const resultRowHeight = useRowHeight(listNode, RESULT_ROW_HEIGHT);
  const pageSize = useMemo(() => (chrome === null ? PROVIDER_PAGE_SIZE : rowsThatFit({
    full: PROVIDER_PAGE_SIZE,
    rowHeight: resultRowHeight,
    chrome: chrome.value,
    minimum: MIN_RESULT_ROWS,
    node: listNode,
  })), [listNode, chrome, resultRowHeight]);
  const pages = pageCount(catalogRows.length, pageSize);
  const safePage = clampPage(resultPage, catalogRows.length, pageSize);
  const visibleRows = pageItems(catalogRows, safePage, pageSize);
  // As in the cheat picker: a full page is measured once and its height held,
  // so the last page of results does not shorten the window.
  const fullPageHeight = usePageHeight(listNode, visibleRows.length, pageSize);
  // The two numbers this page is sized from, so a page that comes out wrong on
  // a screen nobody here has is answerable from a support bundle.
  useEffect(() => {
    if (!listNode) return;
    logUi("catalog.page_sized", { viewport: viewportHeight(listNode), chrome: chrome?.value ?? null, settled: chrome?.settled ?? null, rows: pageSize });
  }, [pageSize, listNode, chrome]);

  /**
   * The marked rows on the page in front of the user, and what clearing them
   * has to forget.
   *
   * Scoped to what is actually rendered rather than to every row the search
   * returned: results whose download mode a controller cannot drive are
   * filtered out entirely and the rest paginate, so counting those named a
   * number nothing on screen accounted for and would have cleared marks on
   * tables the user had never seen. A row retired by a damaged download counts
   * too: that is the same statement about the same row, and since a fresh
   * search stopped clearing it, this press is the only thing that can.
   *
   * A row counts when it is actually presenting a mark, decided by exactly what
   * decides its badge and its two presses. A record the row has outlived is not
   * one: a source that has replaced its file and advertised a checksum for it
   * leaves a row with nothing refused, nothing said on it and both presses
   * live, and offering **Retry 1** over that asked the user to clear a record
   * the screen was not showing them.
   *
   * What a counted row clears is exactly the records it is showing. A row can
   * have earned one per revision over the years, and those are statements about
   * different tables: clearing the one in front of the user is what the press
   * says it does, and the rest stay in the device-wide list under Advanced
   * until something names them. Clearing one exact table clears it everywhere,
   * because what is cleared is bytes rather than a row.
   */
  const retryable = useMemo(() => {
    const digests = new Map<string, string>();
    // The subset whose green this press is allowed to take away, which is the
    // subset that had taken it away in the first place.
    const compatibility = new Set<string>();
    const rejected: Array<{ provider: string; artifactId: string }> = [];
    let rows = 0;
    for (const row of visibleRows) {
      // A saved table is shown on this screen in its own right, and a mark on
      // its exact bytes is shown with it, so the press that clears marks has to
      // reach it: the alternative was a red row on the screen whose only route
      // to Retry was to leave the screen.
      if (row.kind === "local") {
        const mark = blockedView.byDigest[row.table.sha256.toLowerCase()];
        if (!isCompatibilityFailure(mark)) continue;
        digests.set(mark.sha256, mark.reason);
        compatibility.add(mark.sha256);
        rows += 1;
        continue;
      }
      const { result, localTable } = row;
      const { clearTargets, clearDamaged, visibleFailure } = providerRowMarks(result, localTable);
      // Only what the row is actually showing, and only those exact bytes: a
      // count with no marked row under it is a press into the dark, and a press
      // that reached past what it names throws away advice the user never asked
      // to retest. One record can be reached through several origins and still
      // clears once, because what is cleared is the bytes rather than the row.
      if (!visibleFailure) continue;
      for (const mark of clearTargets) {
        digests.set(mark.sha256, mark.reason);
        if (isCompatibilityFailure(mark)) compatibility.add(mark.sha256);
      }
      // One download can leave both: the durable record the backend wrote and
      // this session's own memory that the row is damaged. Taking the durable
      // one and moving on left the session's behind, so clearing the row put
      // the same row back as Damaged and asked for a second press. They are the
      // same statement about the same row and are cleared together, and only
      // where the row is the one making that statement.
      if (clearDamaged) rejected.push({ provider: result.provider, artifactId: result.artifact_id });
      rows += 1;
    }
    return { digests, compatibility, rejected, count: rows };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalogRows, safePage, blockedView, rejectedArtifactIds]);

  const clearMarks = () => run(async () => {
    // Both marks say the same thing about the same row, so one press clears
    // both or the row stays inert with nothing left to explain it. Row by row
    // rather than by search: this press named the rows on one page, and a
    // snapshot spans pages.
    for (const { provider, artifactId } of retryable.rejected) {
      forgetRejectedArtifact(searchScope, provider, artifactId);
    }
    setRejectedGeneration((generationCount) => generationCount + 1);
    const digests = [...retryable.digests.keys()];
    if (onClearMarks && digests.length > 0) {
      // Clearing a record that a table did not work never brings its old green
      // back, so it is taken here rather than waited for: a status read that
      // fails afterwards must not leave this screen claiming a build was proven
      // that the backend no longer says was. A record about a download answers
      // nothing about that, so it leaves the evidence exactly where it is.
      setCompatibilityView((entries) => entries.map((entry) =>
        retryable.compatibility.has(entry.table_sha256) ? { ...entry, invalidated: true } : entry));
      try {
        await onClearMarks(digests);
      } finally {
        await refreshBlocked();
      }
    }
  });

  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    const generation = contextGenerationRef.current;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const operation = startUiOperation("catalog.run");
    try {
      await action();
      operation.completed({ stale: generation !== contextGenerationRef.current });
    } catch (reason) {
      operation.failed(reason, { stale: generation !== contextGenerationRef.current });
      // Before the generation check, because a failure that arrived after the
      // context moved on is still a failure that happened. This runner is the
      // catalog's own boundary and does not pass through the panel's, so
      // nothing else would record a transport error or a client-side
      // reconciliation that the backend never saw.
      logUiFailure("catalog.action_failed", reason, { stale: generation !== contextGenerationRef.current });
      if (generation !== contextGenerationRef.current) return;
      const message = describeError(reason);
      setError(message);
      // The same dialog the panel uses, for the same reason: a Steam
      // notification is gone before it has been read.
      if (!showActionFailure("Searching or downloading a table", reason)) {
        toaster.toast({ title: "CE Decky catalog", body: message });
      }
    } finally {
      if (generation === contextGenerationRef.current) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  const performSearch = async (): Promise<CatalogSearchOutcome | null> => {
    const generation = contextGenerationRef.current;
    const progressToken = newSearchToken();
    setSearchToken(progressToken);
    setSearching(true);
    const normalizedQuery = query.trim();
    if (!normalizedQuery) throw new Error("Enter a game name to search for tables.");
    // Deliberately keeps every mark. Searching again is how a user looks for a
    // different table, not a statement that the ones already proven bad have
    // been fixed - and clearing here made the marks useless in practice,
    // because opening this screen from the panel searches by itself. Retrying
    // a marked row is an explicit press of its own, below.
    // Stamped from before the request, so an outcome that lands after the user
    // has changed which sources are searched is not stored as current.
    const revision = sourceSelectionRevision;
    const outcome = await searchTables(
      { display_name: normalizedQuery, shortcut_executable: shortcutExecutable ?? null },
      progressToken,
    );
    if (generation !== contextGenerationRef.current) return null;
    const searched = Date.now();
    const scope = cacheKey(gameIdentity, normalizedQuery, shortcutExecutable);
    rememberSearch(scope, {
      results: outcome.results, failures: outcome.failures, sources: outcome.sources ?? [],
      searchedAt: searched, revision,
    });
    setSearchScope(scope);
    setResults(outcome.results);
    setResultPage(0);
    setFailures(outcome.failures);
    setSources(outcome.sources ?? []);
    setSearchedAt(searched);
    return outcome;
  };

  const search = () => run(async () => {
    const generation = contextGenerationRef.current;
    try {
      await performSearch();
    } finally {
      if (generation === contextGenerationRef.current) setSearchFinished(true);
    }
  });

  useEffect(() => {
    if (!autoSearch) return;
    const cachedScope = cacheKey(gameIdentity, query, shortcutExecutable);
    const cached = searchCache.get(cachedScope);
    if (cached && cached.revision === sourceSelectionRevision) {
      setSearchScope(cachedScope);
      setResults(cached.results);
      setResultPage(0);
      setFailures(cached.failures);
      setSources(cached.sources);
      setSearchedAt(cached.searchedAt);
      setSearchFinished(true);
      return;
    }
    void search();
    // Search exactly once for each game/target context. Query edits are explicit thereafter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoSearch, gameIdentity, shortcutExecutable]);

  const acquireOne = async (result: CatalogResult) => {
    const generation = contextGenerationRef.current;
    // A cached row and its acquisition authority have independent lifetimes:
    // this cache survives a backend reload, and the backend's own bounded
    // snapshot map does not. The row still looks normal, so the user only found
    // out on click. Refresh the search once and retry against the fresh
    // authority rather than reporting a dead end.
    let next: AcquisitionStatus;
    try {
      next = await startTableAcquisition(result.provider, result.artifact_id, result.search_id ?? null, appId ?? null);
    } catch (cause) {
      if (!/search snapshot is expired/i.test(describeError(cause))) throw cause;
      const snapshotQuery = queryForScope(searchScope, gameIdentity, shortcutExecutable) ?? gameName;
      const refreshed = await searchTables({
        display_name: snapshotQuery,
        shortcut_executable: shortcutExecutable ?? null,
      });
      if (generation !== contextGenerationRef.current) return;
      const match = refreshed.results.find((candidate) =>
        candidate.provider === result.provider && candidate.artifact_id === result.artifact_id);
      if (!match) {
        throw new Error("This table is no longer offered by its provider. Search again to see what is available now.");
      }
      rememberSearch(searchScope, {
        results: refreshed.results, failures: refreshed.failures, sources: refreshed.sources ?? [],
        revision: sourceSelectionRevision, searchedAt: Date.now(),
      });
      setResults(refreshed.results);
      setFailures(refreshed.failures);
      setSources(refreshed.sources ?? []);
      next = await startTableAcquisition(match.provider, match.artifact_id, match.search_id ?? null, appId ?? null);
    }
    if (generation !== contextGenerationRef.current) {
      if (!isTerminalAcquisition(next)) {
        await cancelTableAcquisition(next.acquisition_id).catch(() => undefined);
      }
      return;
    }
    // Every download gets a window of its own, because a download is the one
    // thing on this screen the user has to be able to watch. It used to run
    // where it was started, in a row under a list of results: the press
    // disabled the screen and put what was happening several screens below the
    // focus, so for most of a list the panel simply looked as though it had
    // stopped answering. A provider that serves its own wait was given this
    // window first, on the ground that a countdown of a minute has to be
    // readable; the same is true of a download, which also ends in a failure
    // worth reading or in the review screen.
    //
    // The window owns the acquisition from here: it polls it, it carries the
    // archive choice and any password, it cancels what it started if it is
    // closed, and on an import it hands straight over to the review this list
    // was opened to reach.
    let closed = false;
    let handle: { Close: () => void } | null = null;
    const close = () => {
      if (closed) return;
      closed = true;
      if (ownedModalCloseRef.current === close) {
        ownedModalCloseRef.current = null;
        ownedAcquisitionRef.current = null;
      }
      handle?.Close();
      // The window owns the whole acquisition, so its outcome reaches this list
      // only when the window closes. Two things arrive then: the artifact this
      // download retired, which is held here in memory, and the durable mark the
      // backend writes where it staged bytes that turned out not to be a table.
      // Without the second, this list offers the same row again on the next
      // look and the import is refused after another download.
      setRejectedGeneration((generationCount) => generationCount + 1);
      void refreshBlocked();
    };
    try {
      const imported = next;
      handle = showModal(<TableAcquisitionModal
        initialStatus={next}
        searchScope={searchScope}
        onImported={async (sha256) => { noteImportedFrom(imported); await onImported(sha256); }}
        onClose={close}
      />);
    } catch (cause) {
      if (!isTerminalAcquisition(next)) await cancelTableAcquisition(next.acquisition_id).catch(() => undefined);
      throw cause;
    }
    ownedModalCloseRef.current = close;
    ownedAcquisitionRef.current = next.acquisition_id;
  };

  const acquire = (result: CatalogResult) => run(() => acquireOne(result));

  const redownloadLocal = (table: TableStatus) => run(async () => {
    const originKeys = new Set(table.origins.map((origin) => `${origin.provider}:${origin.artifact_id}`));
    const matches = (candidate: CatalogResult) => {
      const digest = candidate.advertised_sha256?.toLowerCase();
      return digest === table.sha256.toLowerCase()
        || originKeys.has(`${candidate.provider}:${candidate.artifact_id}`);
    };
    let candidate = automaticResults.find(matches);
    if (!candidate) {
      const refreshed = await performSearch();
      candidate = refreshed?.results.filter(isAutomatic).find(matches);
    }
    if (!candidate) {
      throw new Error("This saved table is not currently offered by its provider. The local copy is unchanged and can still be used.");
    }
    await acquireOne(candidate);
  });

  /**
   * The one window that stands between a saved table and the network.
   *
   * Its two halves are gated separately on purpose. A provider row that is gone
   * upstream, served bytes that were not a table, or produced a damaged
   * transfer says nothing at all about the copy already on this device, and a
   * mark recorded against that copy's own SHA says nothing about the row. Each
   * disables its own press and leaves the other one working, because a source
   * failing is exactly when the saved copy is the thing the user needs.
   */
  const showExistingChoice = (
    table: LocalTableChoice,
    { result, provenance }: { result?: CatalogResult; provenance: TableProvenance },
  ) => {
    let closed = false;
    let handle: { Close: () => void } | null = null;
    const close = () => {
      if (closed) return;
      closed = true;
      handle?.Close();
    };
    const { download: downloadBlock, saved: savedBlock } = tableRowRefusals(blockedView, {
      advertisedSha256: result ? resolvedDigest(result) ?? result.advertised_sha256 : undefined,
      artifactKey: result ? `${result.provider}:${result.artifact_id}` : null,
      savedSha256: table.sha256,
    });
    const damaged = result ? rejectedArtifactIds.has(`${result.provider}:${result.artifact_id}`) : false;
    const downloadBlockedReason = downloadBlock?.reason
      ?? (damaged ? "The last download from this row did not produce a usable table." : null);
    // The row above this window shows the copy it is presenting, so a refusal
    // about the revision the source is offering now is named here or nowhere.
    // Clearing it here reaches the same records the list's own Retry does, and
    // the list is re-read afterwards so the row it came from agrees.
    const retryDownload = result && downloadBlockedReason && (downloadBlock || damaged)
      ? () => {
        // Closed from inside the work, not before it: this screen refuses a
        // second press while one is running, and closing first turned that
        // refusal into a window that shut and did nothing.
        void run(async () => {
          close();
          if (damaged) {
            forgetRejectedArtifact(searchScope, result.provider, result.artifact_id);
            setRejectedGeneration((generationCount) => generationCount + 1);
          }
          // Exactly the record this press is beside, which is the one about the
          // revision the source is offering now. The saved copy's own record,
          // where there is one, is the row's to clear and stays where it is.
          const digests = downloadBlock ? [downloadBlock.sha256] : [];
          if (onClearMarks && digests.length > 0) {
            try {
              await onClearMarks(digests);
            } finally {
              await refreshBlocked();
            }
          }
        });
      }
      : null;
    handle = showModal(<ExistingTableChoiceModal
      table={table}
      provenance={provenance}
      canUse={!busy && !isCompatibilityFailure(savedBlock) && Boolean(onLocalSelected)}
      canDownload={!busy && !downloadBlockedReason}
      useBlockedReason={isCompatibilityFailure(savedBlock) ? savedBlock?.reason ?? null : null}
      downloadBlockedReason={downloadBlockedReason}
      onRetryDownload={retryDownload}
      onUse={() => {
        close();
        if (onLocalSelected) void run(() => onLocalSelected(table.sha256));
      }}
      onDownload={() => {
        close();
        if (result) void acquire(result);
        else {
          const stored = localBySha.get(table.sha256);
          if (stored) void redownloadLocal(stored);
        }
      }}
      onClose={close}
    />);
  };

  // What the second line used to say, plus what Retry would do, behind the
  // row's own `?`. Both are worth having and neither is worth a line of a panel
  // that fits six results.
  const searchHelp = [
    searchedAt === null
      ? "Look this game up on the table sources CE Decky can download from."
      : `Searched ${formatAge(Date.now() - searchedAt)}. The per-source counts say how many usable tables each one returned; a source listed as n/a refused an anonymous request.`,
    retryable.count > 0
      ? `${retryable.count} row(s) on this page are marked, each with what happened to it: Failed means Cheat Engine ran a cheat from it and it came straight back off, Not a table means the download was not one, Encrypted means its archive is locked and only 7-Zip opens it, and Gone means the source no longer has the file. Retry ${retryable.count} drops those marks and offers them again.`
      : null,
  ].filter(Boolean).join(" ");

  return (
    <PanelSection>
      <SectionHeading>Search / Download</SectionHeading>
      <PanelSectionRow><TextField label="Search query" value={query} onChange={traceUiEdit("provider_catalog.search_query", (event: any) => setQuery(String(event.target.value ?? "")))} disabled={busy} /></PanelSectionRow>
      {/* One line: the counts on the left, the actions on the right. Each of
          these was a full-width button or a line of its own, which on a panel
          that fits six results is more chrome than content. The detail that
          used to occupy the second line - when this was searched, and what
          Retry does - is behind the row's own `?`, where it costs no height
          until it is asked for. */}
      {/* One line, revealed under the ring rather than wrapped.
          It used to wrap, so that the per-source tally - the half that says a
          source returned nothing - could not fall off the end. What that cost
          is a row whose height depends on what is beside it: `Retry N` appears
          only when this page has marked rows, it takes width from the text
          column when it does, and the tally then reflowed onto another line and
          moved the whole list under the reader's thumb.

          `tone="header"` draws it as what it is: the row that heads the
          results, on its own ground with a gap holding the list off itself,
          rather than one more result among the results.

          Clipping is no longer the same trade it was when that was written.
          These rows scroll their own text while the ring is on them, so the
          tail is reachable without costing a line, which is the treatment every
          other list in this version now uses. */}
      <PanelRow
        testId="search-controls"
        truncate
        scroll
        tone="header"
        label={searching
          ? `Searching${searchingSourceCount ? ` ${searchingSourceCount}` : ""} sources \u00b7 ${elapsedSeconds}s${searchProgressText ? ` \u00b7 ${searchProgressText}` : ""}`
          : searchedAt === null && !searchFinished
            ? "No search yet"
            : `${catalogRows.length} table(s)${sourceSummary ? ` \u00b7 ${sourceSummary}` : ""}`}
        trailing={busy ? <Spinner style={{ width: 14, height: 14, flexShrink: 0 }} /> : undefined}
        help={searchHelp}
        actions={(
          <>
            <SmallButton size="medium" disabled={busy || !query.trim()} onClick={traceUiAction("catalog.search", () => void search())}>
              {searchedAt === null && !searchFinished ? "Search" : "Search again"}
            </SmallButton>
            {/* Only when it would free something on this page, so the ordinary
                case is one row with one button. */}
            {onClearMarks && retryable.count > 0 && (
              <SmallButton size="medium" disabled={busy} onClick={traceUiAction("catalog.clear_marks", () => void clearMarks())}>
                {`Retry ${retryable.count}`}
              </SmallButton>
            )}
          </>
        )}
      />
      {error && <PanelSectionRow><Field label="Catalog error" description={error} /></PanelSectionRow>}
      {/* An empty answer has two completely different meanings, and only one of
          them is about this game. Every other source line is a count, so a
          search that asked nothing at all is the one case that needs a sentence
          rather than a tally the user has to add up. */}
      {allSourcesOff && (
        <PanelSectionRow>
          <Field
            label="Every table source is switched off"
            description="Nothing was searched. Switch a source back on under Advanced, Table sources."
          />
        </PanelSectionRow>
      )}
      {/* A provider that answers an anonymous request with a challenge is
          unavailable, and the source line above already says so as `n/a`. It
          gets no banner of its own: it repeated the counter the user has just
          read, above the results they came for, on every search. */}
      {/* One box around the list, so the page can measure the room it has. */}
      <div ref={setListNode} style={fullPageHeight === null ? undefined : { minHeight: fullPageHeight }} data-testid="catalog-list">
      {visibleRows.map((row) => {
        if (row.kind === "local") {
          const table = row.table;
          // A mark on these exact bytes refuses these exact bytes. Asking the
          // source for the file again is the other press on this row and the
          // only way the user finds out that a table refused for an older build
          // has been republished, so it stays reachable: the row opens, the
          // window says the copy is marked, and the ring lands on the download.
          const blocked = blockedView.byDigest[table.sha256.toLowerCase()];
          return (
            <PanelSectionRow key={`local:${table.sha256}`}>
              <div className={FOCUS_SCROLL_CLASS}>
              <ButtonItem
                layout="below"
                disabled={busy || !onLocalSelected}
                onClick={traceUiAction("catalog.local_table", () => showExistingChoice(table, { provenance: "local_only" }), { table_sha: table.sha256 })}
              >
                <div style={RESULT_LINES}>
                  <div style={TITLE_ROW}>
                    <span style={TITLE_TEXT}><FocusScrollText paced>{table.filename}</FocusScrollText></span>
                    <CompatibilityMark evidence={gameCompatibility(compatibilityView, appId, table.sha256)} blocked={blocked ?? null} />
                    {/* The chip says what this device can do with the bytes,
                        which for a row that exists because the bytes are here
                        is always the same thing. What is recorded about them is
                        the glyph beside it. */}
                    <span style={ROW_MARK_LOCAL}>Local</span>
                  </div>
                  <span style={SUBTLE_TEXT}><FocusScrollText paced>{[
                    // When, which release, how large: the three a reader scans
                    // for, in that order, ahead of the identity that answers
                    // which exact bytes these are.
                    localArrived(table),
                    // The release a source stated for these exact bytes, and
                    // nothing where no source stated one. This used to print the
                    // `.CT` file's own `CheatEngineTableVersion`, which is the
                    // version of Cheat Engine's table format rather than the
                    // table's: every table reads 45 or 46 whatever its game, so
                    // the row answered a question nobody asked with a number
                    // that looked like an answer to the one they did. It also
                    // put the `v` on itself instead of asking `releaseLabel`,
                    // which is how a stored `v2` would have printed as `vv2`.
                    advertisedRelease(table),
                    formatSize(table.size),
                    table.sha256.slice(0, 12),
                    "on this device",
                  ].filter(Boolean).join(" · ")}</FocusScrollText></span>
                </div>
              </ButtonItem>
              </div>
            </PanelSectionRow>
          );
        }
        const { result, localTable, provenance } = row;
        const artifactKey = `${result.provider}:${result.artifact_id}`;
        // A table on this device that this row produced, whether or not the row
        // can prove it still serves the same bytes. The proof decides what the
        // press says, never whether the press may reach the copy: the two
        // sources that publish no checksum are the ones a user is most likely to
        // have downloaded from already, and offline that copy is all they have.
        // Two records about two different sets of bytes, kept apart.
        //
        // A durable mark found by advertised digest or by provider row, and a
        // transfer this session already found damaged, are both statements about
        // what this row serves. A mark recorded against the saved copy's own SHA
        // is a statement about the copy. Reading the first as a reason to retire
        // the whole row took the saved copy down with the source: a file the
        // provider no longer has is not a file this device no longer has, and
        // that is exactly the moment the saved copy is what the user needs.
        const marks = providerRowMarks(result, localTable);
        const { history, savedUsable, downloadDead, subjectDigest, failureRecord, chip: mark } = marks;
        // A row with nothing here to offer and only a revision that came
        // straight back off behind it is not worth a download: the importer
        // refuses those exact bytes anyway, so the press would spend a transfer
        // to be told what the badge already says, and **Retry** is how the user
        // says to try the row again. A row holding a usable copy is never
        // retired by that history, which is about bytes it is not offering.
        const historyOnly = !savedUsable && !downloadDead && Boolean(history);
        // Retired only with neither half left. Either press being live is a row
        // worth opening, and the window says which of the two is off and why.
        const retired = (downloadDead && !savedUsable) || historyOnly;
        return (
          <PanelSectionRow key={artifactKey}>
            <div className={FOCUS_SCROLL_CLASS}>
            <ButtonItem
              layout="below"
              disabled={busy || retired}
              onClick={traceUiAction("catalog.result", () => {
                if (localTable && onLocalSelected) showExistingChoice(localTable, { result, provenance });
                else void acquire(result);
              }, { provider: result.provider, artifact_id: result.artifact_id, table_sha: localTable?.sha256 })}
            >
              <div style={RESULT_LINES}>
                {/* One statement about the row, not two. The durable record is
                    written from the same outcome that retires the row for this
                    session, so both marks were landing on the row together and
                    it read like two separate findings. The record is the one
                    that outlives the session, and it is also the only one of
                    the two that is right about a file the source no longer has,
                    which is not damaged at all. */}
                <div style={TITLE_ROW}>
                  {/* The marks stay outside the moving line and never shrink,
                      so a glyph and a chip are readable the moment the row is,
                      whatever the title is doing. */}
                  <span style={TITLE_TEXT}><FocusScrollText paced>{result.table_title}</FocusScrollText></span>
                  <CompatibilityMark evidence={gameCompatibility(compatibilityView, appId, subjectDigest)} blocked={failureRecord} />
                  {marks.duplicate ? <SameTableMark /> : null}
                  {mark ? <span style={mark.style}>{mark.text}</span> : null}
                </div>
                <span style={SUBTLE_TEXT}><FocusScrollText paced>{[
                  // When, which release, how large. One post commonly carries
                  // every revision of the same table, so its attachments share a
                  // filename, a title and the date of the post they sit in: the
                  // release tells them apart where there is one, and where there
                  // is not the uploader's own note on that exact file is the
                  // only thing that does. The date leads because it is what a
                  // reader scans a list of revisions by, and it is the one field
                  // nearly every row has.
                  formatPosted(result.posted_at) ?? null,
                  releaseLabel(result.version) ?? result.notes ?? null,
                  formatSize(result.size_bytes),
                  result.filename,
                  result.provider_display_name,
                  result.stale ? "stale cache" : null,
                ].filter(Boolean).join(" · ")}</FocusScrollText></span>
              </div>
            </ButtonItem>
            </div>
          </PanelSectionRow>
        );
      })}
      </div>
      {(pages > 0 || footerActions) && (
        <PagerFooter
          testId="catalog-footer"
          containerRef={setFooterNode}
          page={safePage}
          pages={pages}
          disabled={busy}
          onPage={setResultPage}
          preferNext
          trailing={footerActions}
        />
      )}
    </PanelSection>
  );
}
