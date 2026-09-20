import { useUiSurface } from "../useUiSurface";
import { traceUiAction, traceUiEdit, startUiOperation } from "../uiActions";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { DialogButton, Field, ModalRoot, PanelSection, PanelSectionRow } from "@decky/ui";
import { modalActionStyle } from "../components/ModalActions";
import { CONTENTS_ONLY, DensePanel, FilterField, PREPARE_ACTION_CLASS, PanelRow, SectionHeading, SmallButton, focusFirstEnabled, useFittedRows, usePageHeight, useRowHeight } from "../components/PanelDensity";
import { PagerFooter } from "../components/PagerFooter";
import { tableHolderLabel, type TableHolders } from "../tableHolders";
import { CompatibilityMark, isCompatibilityFailure } from "../components/CompatibilityMark";
import { SignedMark } from "../components/SignedMark";
import { DerivedMark } from "../components/DerivedMark";
import type { CompatibilityEvidence, TableStatus } from "../types";
import { describeError } from "../errors";
import { MANAGE_PAGE_SIZE, MANAGE_ROW_HEIGHT, MIN_MANAGE_ROWS, advertisedRelease, clampPage, derivedFromLabel, pageCount, pageItems, shortBlockedReason, tableIsSigned, type BlockedMark } from "../uiModel";
import { MODAL_BOTTOM_PADDING, latchChrome, rowsThatFit, viewportHeight, type LatchedChrome } from "../viewport";
import { logUi } from "../supportLog";

interface Props {
  compatibility?: readonly CompatibilityEvidence[];
  /** Verified local tables this game has already imported, newest association first. */
  tables: readonly TableStatus[];
  /**
   * Every other verified table on this device, which this game has no
   * association with.
   *
   * The association is the thing that gets lost. Deleting the plugin's setup
   * state keeps `tables/` on purpose and removes `state/`, and repairing a
   * corrupt profile store replaces it with an empty one; both say the imported
   * tables are kept and can be chosen again afterwards. Reaching one, though,
   * went entirely through the game's own library, so after either recovery the
   * bytes were on the device and nothing in Game Mode could name them: Search
   * needs a provider to produce a row that maps to them, and Local file needs
   * the original file, which is exactly what the user may no longer have.
   *
   * Listing them here is the route that needs neither. Choosing one is a
   * deliberate press that associates it with the game that is selected now, and
   * then it is an ordinary import: the review screen opens and the exact SHA is
   * consented to again. Nothing here guesses which game a table used to belong
   * to, because that is the fact the recovery discarded.
   */
  otherTables?: readonly TableStatus[];
  /**
   * Which game each table belongs to, by exact SHA, for the start of its row.
   *
   * This list spans every game on the device and a file name answers nothing:
   * `CD_Inventory_2145.CT` names no game, and neither does `winmm-x64.zip`. The
   * game leads for the same reason it leads in the list of tables that did not
   * work, which is the other list here that is read across games.
   *
   * A table no profile holds has no name to lead with and keeps the row it
   * always had. Nothing is guessed: this is library membership, which is the
   * association a game actually made.
   */
  owners?: Readonly<Record<string, string>>;
  /** The table this game has selected, whatever state its authorization is in. */
  activeSha256: string | null;
  /**
   * Whether that selected table is still authorized to execute.
   *
   * Selected and authorized are two facts and this screen used only the first,
   * so **Use** was disabled for the selected table on the ground that there was
   * nothing left to do to it. Withdrawing its authorization leaves it selected
   * and unusable, and re-authorizing it means opening it once through the
   * review screen, which is exactly what **Use** does: the one press that fixes
   * the state was the one press the state disabled, while the panel was telling
   * the user to come here and do it.
   */
  activeAuthorized?: boolean;
  canSelect?: boolean;
  holderIds?: Readonly<Record<string, readonly number[]>>;
  onRevoke?: (sha256: string, confirmedHolderIds: readonly number[]) => Promise<void>;
  onRefreshHolders?: () => Promise<{ holders: Readonly<Record<string, TableHolders>>; holderIds?: Readonly<Record<string, readonly number[]>>; activeSha256: string | null }>;
  /**
   * Reason by exact table SHA for every table recorded as not working.
   *
   * Search greys these rows, and this screen offers the same bytes by a route
   * that never asks a provider anything: without the mark here, the one table a
   * user is most likely to reach for again after a table failed is the one that
   * just failed, presented exactly like the others.
   */
  blockedReasons?: Readonly<Record<string, BlockedMark>>;
  /**
   * Open a `.CT` or archive from the device's filesystem, this window first.
   *
   * It lives here because the quick access panel is 300 pixels wide and a row
   * carrying a filename has space for two controls, not three. Search is one of
   * them; everything else about which exact table this game runs is behind the
   * other, and opening a file is one of those things rather than a separate
   * kind of act. It is also the only one that works on a device that has never
   * held a table, which is why this window is reachable with nothing in it.
   *
   * This window closes before the picker opens. Decky's file picker is a modal
   * of its own, and a modal raised over another one is a window behind a
   * window, which on the device is a screen that looks like it did nothing.
   */
  onOpenLocalFile?: () => void;
  /** Resolves when the selection workflow has settled, so the row can stay latched. */
  onSelect: (sha256: string) => void | Promise<void>;
  /**
   * Which game has each table selected, by exact SHA, where any has.
   *
   * A table some game is on cannot be removed: the backend refuses it, and it
   * is right to. Knowing that here is what keeps a press from being offered
   * only to be refused, and lets the row say which game is holding it instead
   * of leaving the reader to guess why nothing happened. The backend check
   * stays, because this is a snapshot and the answer can change under it.
   */
  selectedBy?: Readonly<Record<string, TableHolders>>;
  /**
   * Destroy one stored table, after this window has confirmed it.
   *
   * Content-addressed and irreversible. **This must reject when the deletion
   * failed**, because this window removes the row on success and has no other
   * way to tell the two apart: a rejection swallowed on the way here is a row
   * that disappears from a table still on the device.
   *
   * It must also not raise a window of its own for a failure. This screen is a
   * modal, one raised over it is a window behind a window, and the message
   * belongs on the row the press was made on.
   */
  onDelete?: (sha256: string) => Promise<void>;
  /**
   * Make a copy of one signed table that this Cheat Engine will open.
   *
   * Offered on the row that carries the mark, because this screen is where a
   * table the user already has is chosen and a signed one is refused with no
   * message at all. It consents to nothing: the copy is a new table with its
   * own digest, and it resolves with Review open on it, the same way `onSelect`
   * does. A failure must reject rather than raise a window of its own.
   */
  onPrepareCopy?: (sha256: string) => Promise<void>;
  onClose: () => void;
}

/**
 * What a row has to say for a table to be told apart from another.
 *
 * A digest and a size do not answer the question this screen exists for, which
 * is which of these to use: several revisions of one table share a filename and
 * differ only by what the source called them and when they arrived. So the
 * release comes first where a source stated one, then how many records it
 * holds, then where it came from and when, and the exact digest and size last.
 * A field the table does not carry is left out rather than shown empty.
 *
 * Returned in pieces rather than as one sentence, because a row that is also
 * saying something about its own state has to put that first and the release
 * immediately after it: the reader is choosing between revisions, and a status
 * that pushed the release past the end of the line left them choosing between
 * identical names.
 */
/**
 * One width for every control on a table row.
 *
 * `Use`, `Revoke`, `Delete` and `Confirm` are all different lengths, and a
 * row lays its controls out from the right, so `Use` landed in a different
 * column depending on which of them was beside it: the list had its buttons
 * in three columns and nothing lined up down the screen.
 *
 * The width is the longest of those four rather than a round number, so none
 * is ever cut, and the side padding is tightened to pay for it: what a button
 * takes here comes out of a name the reader has to finish reading. Together
 * the pair is narrower than it was while being the same width on every row.
 */
const rowActionStyle = {
  ...modalActionStyle,
  padding: "6px 8px",
  minWidth: 70,
  justifyContent: "center" as const,
};

/**
 * The footer's own padding, which is the modal's rather than the panel's.
 *
 * This row sits outside the dense wrapper, where a section's own inline padding
 * does not reach it, so it carries the same sixteen pixels every modal's bottom
 * actions were drawn with.
 */
const MANAGE_FOOTER = { padding: "var(--ce-footer-padding, 0 16px 6px)" };

// The filter takes the width the count beside it is not using, and keeps a
// floor under it. A table's name is what is typed into it, and a box cut off at
// some fraction of the row is a box you cannot read back what you typed into,
// on the screen whose names are `NeonBazaar-ItemNoDecreaseUpdate.CT`. Given
// only a share of the row it moved with the words beside it: the line above the
// list is longer when no game is chosen, and the box shrank to whatever that
// sentence left. The count and its line clip to an ellipsis first, which is
// what the row's own `truncate` is for.
const MANAGE_FILTER: CSSProperties = { flex: "1 1 260px", minWidth: 180 };

function describeParts(table: TableStatus): Array<string | null> {
  // One provenance record for the whole row. The release used to be searched
  // for separately, walking back to any origin that had one, so a row could
  // pair a version from the source a table came from once with the provider and
  // date of the source it came from last, and say so as though the three
  // belonged together.
  const origin = table.origins.length ? table.origins[table.origins.length - 1] : null;
  return [
    advertisedRelease(table),
    `${table.entry_count} ${table.entry_count === 1 ? "record" : "records"}`,
    // Where these bytes came from, in the one slot the row already spends on
    // that question. A table CE Decky derived came from another table on this
    // device, and calling it a local file would be naming a file that was never
    // opened; a derived table carries no origin, so this slot is free on
    // exactly the rows that need it.
    derivedFromLabel(table) ?? (origin ? origin.provider : "Local file"),
    // The download where there was one, and otherwise when the file was opened
    // here: both answer when this copy arrived, which is the question a reader
    // comparing two of them is asking.
    arrived(origin?.retrieved_at ?? table.imported_at ?? null),
    `${table.sha256.slice(0, 8)}`,
    `${Math.max(1, Math.round(table.size / 1024))} KiB`,
  ];
}

/**
 * The release this table is, in the words the user was offered it under.
 *
 * The version the provider advertised for these bytes is the whole of it: that
 * is what search showed and what tells one revision from another, since a post
 * carries every revision of a table and its attachments share a filename and a
 * title. Taken from the one origin the rest of the row is describing, never
 * searched for across all of them, because a version that belongs to a
 * different download is a different table's release.
 *
 * `table_version` used to be the fallback, on the recorded belief that it was
 * the author's own release field. It is not. It is the `.CT` file's
 * `CheatEngineTableVersion` attribute, which is the version of Cheat Engine's
 * own table format: every table on this device reads 45 or 46 whatever its game
 * or source, because those are CE 7.5 and 7.6. A Neon Bazaar table whose
 * source advertised nothing was therefore shown as `v46`, which named the
 * format of the file rather than anything about the cheats in it. So a row now
 * claims a version only where a source stated one, and the format version keeps
 * the places that already label it honestly as `CE table 46`.
 */


/**
 * When this copy arrived on this device, as a date and nothing finer.
 *
 * The download, not the post: `retrieved_at` is stamped by the acquisition that
 * fetched the file, so it answers "when did I get this" rather than "when did
 * the author publish it", and those differ by years on an old post. A table
 * opened from a file has no origin and answers with the arrival the store
 * stamped instead, so a stored list is not half dated.
 *
 * A time of day is noise on a row being scanned, and the question here is which
 * of two copies is the newer one. A table imported before arrival was tracked
 * has neither date and says nothing rather than inventing one from a file
 * timestamp, which records when the bytes were written and not when the user
 * chose them.
 */
function arrived(at: string | null): string | null {
  if (!at) return null;
  const when = new Date(at);
  if (Number.isNaN(when.getTime())) return null;
  return when.toISOString().slice(0, 10);
}

/**
 * The tables this game already has, selectable without a provider.
 *
 * Every imported table is retained by exact SHA in the game's own library, but
 * the only ways back to one were a provider search result that still mapped to
 * it or the original file on disk. A table imported from a file the user later
 * moved, or whose provider row stopped coming back, became unreachable state -
 * which is not what "switch between imported tables" is supposed to mean.
 */
export function ImportedTablesModal({ compatibility = [], tables, otherTables = [], owners, activeSha256: initialActiveSha256, activeAuthorized = true, canSelect = true, blockedReasons = {}, selectedBy: initialSelectedBy = {}, holderIds: initialHolderIds = {}, onRevoke, onRefreshHolders, onOpenLocalFile, onSelect, onDelete, onPrepareCopy, onClose }: Props) {
  useUiSurface("ImportedTablesModal");
  const [selectedBy, setSelectedBy] = useState(initialSelectedBy);
  const [holderIds, setHolderIds] = useState(initialHolderIds);
  const [activeSha256, setActiveSha256] = useState(initialActiveSha256);
  // Selecting a table is an async workflow that opens Review. A second press
  // while the first is still running produced a rejection nothing surfaced, so
  // the modal owns its own latch rather than relying on the parent's.
  const selectingRef = useRef(false);
  const [selecting, setSelecting] = useState(false);
  const select = (sha256: string) => {
    if (selectingRef.current) return;
    setArmed(null);
    setFailure(null);
    selectingRef.current = true;
    setSelecting(true);
    const operation = startUiOperation("manage.select", { table_sha: sha256 });
    void (async () => onSelect(sha256))()
      .then(() => operation.completed())
      // Said on the row the press was made on, exactly as a failed removal is.
      // This screen is a modal, and a window raised over it can appear behind
      // it and read as a press that did nothing.
      .catch((cause) => { operation.failed(cause); setFailure(describeError(cause)); })
      .finally(() => {
        selectingRef.current = false;
        setSelecting(false);
      });
  };
  /**
   * Make the copy of a signed table that this Cheat Engine will open.
   *
   * The same latch and the same failure surface as `select`, because it ends
   * the same way: the parent closes this window and opens Review on the copy,
   * where the consent for those bytes is given.
   */
  const prepareCopy = (sha256: string) => {
    if (selectingRef.current || !onPrepareCopy) return;
    setArmed(null);
    setFailure(null);
    selectingRef.current = true;
    setSelecting(true);
    const operation = startUiOperation("manage.prepare_copy", { table_sha: sha256 });
    void (async () => onPrepareCopy(sha256))()
      .then(() => operation.completed())
      .catch((cause) => { operation.failed(cause); setFailure(describeError(cause)); })
      .finally(() => {
        selectingRef.current = false;
        setSelecting(false);
      });
  };
  /**
   * Which row's delete has been pressed once, if any.
   *
   * The confirmation is the row itself rather than a window over this one. A
   * modal raised over a modal is a window behind a window, which on the device
   * is a screen that looks as though the press did nothing, and this project
   * has already paid for that lesson once. Two presses on the same control,
   * with the row saying what the second one does, needs no second window and
   * is a shape a controller can walk. Arming one row disarms any other, and
   * anything else the reader does with this screen puts it back.
   */
  const [armed, setArmed] = useState<string | null>(null);
  const revoke = (sha256: string) => {
    if (selectingRef.current || !onRevoke) return;
    selectingRef.current = true;
    setSelecting(true);
    setFailure(null);
    const operation = startUiOperation("manage.revoke", { table_sha: sha256 });
    void (async () => {
      try {
        await onRevoke(sha256, holderIds[sha256] ?? []);
        operation.completed();
        setSelectedBy((current) => ({ ...current, [sha256]: { count: 0, names: [] } }));
        setActiveSha256((current) => current === sha256 ? null : current);
      } catch (cause) {
        operation.failed(cause);
        setFailure(describeError(cause));
      } finally {
        // A multi-holder operation may commit only some withdrawals. Reconcile
        // that partial result without turning a completed withdrawal into failure.
        if (onRefreshHolders) {
          try {
            const next = await onRefreshHolders();
            setSelectedBy(next.holders);
            setHolderIds(next.holderIds ?? {});
            setActiveSha256(next.activeSha256);
          } catch (cause) {
            startUiOperation("manage.refresh_holders", { table_sha: sha256 }).failed(cause);
          }
        }
        selectingRef.current = false;
        setSelecting(false);
      }
    })();
  };
  const remove = (sha256: string) => {
    if (selectingRef.current || !onDelete) return;
    selectingRef.current = true;
    setSelecting(true);
    setFailure(null);
    const operation = startUiOperation("manage.delete", { table_sha: sha256 });
    void (async () => onDelete(sha256))()
      // Only what actually happened. The row goes on the resolution, so a
      // rejection has to reach here as one: a failure swallowed on the way
      // would take the row away from a table still on the device.
      .then(() => {
        operation.completed();
        restoreAfterDelete.current = Math.max(0, visible.findIndex((entry) => entry.table.sha256 === sha256));
        setRemoved((current) => [...current, sha256]);
      })
      .catch((cause) => { operation.failed(cause); setFailure(describeError(cause)); })
      .finally(() => {
        selectingRef.current = false;
        setSelecting(false);
      });
  };
  const close = () => {
    if (!selectingRef.current) onClose();
  };
  /**
   * What a row is called, and what it is doing.
   *
   * Which group a row is in is answered by the ground it is drawn on rather
   * than by a heading, because the two groups are one list so that the screen
   * has one page budget: a page opening in the middle of the second group
   * carried no boundary at all, and a run of rows belonging to other games
   * looked exactly like this game's own. The words remain on the first of them,
   * for a reader who wants the boundary named.
   *
   * In use is in use, whichever game is on it. Saying it only for this game's
   * own table left a row another game is running looking like any other, with a
   * missing Delete and nothing on the line to account for it.
   */
  const rowLabel = (table: TableStatus, group: "mine" | "device", index: number): string => {
    const holders = selectedBy[table.sha256];
    const holderCount = holders?.count ?? 0;
    const usedHere = table.sha256 === activeSha256;
    // One statement, not two. "in use" and "in use by Neon Bazaar" are the
    // same sentence on a row that already leads with that game, and the row
    // carried both. Where this game is the only one on the table the flag is
    // the whole of it; where others hold it too, naming every one of them is,
    // and the flag says nothing the naming does not. Needing authorization is
    // the exception either way: no holder list says that.
    const soleHolder = usedHere && holderCount <= 1;
    return [
      owners?.[table.sha256] ? `${owners[table.sha256]} \u00b7 ${table.filename}` : table.filename,
      usedHere && !activeAuthorized ? "needs authorizing" : soleHolder ? "in use" : null,
      soleHolder ? null : tableHolderLabel(holders),
      canSelect && group === "device" && (index === 0 || visible[index - 1].group === "mine")
        ? "elsewhere on this device"
        : null,
    ].filter(Boolean).join(" · ");
  };
  /**
   * What this row is doing, in a few words, before anything that describes it.
   *
   * Every state on this screen answers the same two questions in the same
   * order: what is up with this row, and which release is it. They used to be
   * answered the other way round and at length - a row that could not be used
   * opened with a sentence naming the screen the mark is cleared on, and a row
   * with a confirmation armed replaced its whole description with a paragraph,
   * so the release the reader is choosing between was off the end of the line
   * in exactly the two states where a mistake is expensive.
   *
   * Each of these is a state, not a sentence: what follows from it is on the
   * controls, which say Confirm where a press is armed and offer nothing where
   * a press is refused, and what it is about is on the line behind it.
   */
  const rowStatus = (table: TableStatus): string | null => {
    const held = (selectedBy[table.sha256]?.count ?? 0) > 0 || table.sha256 === activeSha256;
    if (armed === table.sha256) {
      // The consequence in the fewest words that still distinguish the two
      // presses, because they differ in exactly one way that matters: one keeps
      // the bytes and the other does not. The bytes here may be the only copy
      // left - the source may be gone, the row it came from may have changed,
      // the original file may have been moved - so the delete says what is lost
      // rather than promising a re-import this screen cannot keep.
      return held
        ? `Confirm: revoke and detach \u00b7 ${tableHolderLabel(selectedBy[table.sha256]) ?? "held by the current game"} \u00b7 local file kept`
        : "Confirm: delete from this device \u00b7 comes back only from a file or a new download";
    }
    // Every state that applies, not the first of them. Returning on the first
    // match dropped the line that accounts for a missing Delete from exactly
    // the rows that have one missing for two reasons at once: a table marked as
    // not working which another game is also holding said only that it was
    // marked, and the button was gone with nothing on the line explaining it.
    return [
      // The reason the one press on this row is off, said before the record it
      // is off for: a mark this screen shows and Search refuses the same bytes
      // for is not a mark this screen may quietly step around.
      // The state and nothing else. Where a mark is cleared and how a damaged
      // entry is repaired are the same two sentences on every row that has
      // them, so they say nothing about the row they are on: what belongs here
      // is what is true of this table, and the release and date behind it are
      // what the reader came to compare. The routes out are in the README.
      isCompatibilityFailure(blockedReasons[table.sha256]) ? "Marked as not working" : null,
      !table.available ? "File missing or damaged" : null,
      // Which game has it is on the label, where in use is said for this game's
      // own table too, so this only has to account for the absent Delete.
      held ? "In use, cannot be deleted" : null,
    ].filter(Boolean).join(" \u00b7 ") || null;
  };
  const rowDescription = (table: TableStatus): string => [
    rowStatus(table),
    ...describeParts(table),
    // The mark itself is the glyph on the label, in the same state language
    // Search uses. What is said here is the head of the recorded sentence behind
    // it, which is the part a user can act on - and only for a record about
    // whether the table works. An archive or source failure has no source
    // context on this screen and belongs to the row it came from and to the
    // full list under Advanced.
    isCompatibilityFailure(blockedReasons[table.sha256])
      ? shortBlockedReason(blockedReasons[table.sha256]?.reason)
      : null,
  ].filter(Boolean).join(" \u00b7 ");
  /**
   * The presses one row carries.
   *
   * `Use` is withheld from a table another game owns. A cheat table is written
   * against one game's code, so applying one game's to another cannot work, and
   * the press was not merely useless: it associates the table with this game
   * before the review screen opens, and cancelling that review does not take
   * the association back. One press on such a row put Half-Life 2's table
   * permanently into Neon Bazaar's library, where it then read as one of
   * that game's own tables.
   *
   * Owned by another game, not merely listed in the second group. A table this
   * device holds that no profile claims is the one route back after a recovery
   * that discards the profile store: the bytes are still here, the association
   * is gone, and Search needs a provider row while Local file needs the
   * original file. Offline with neither, this press is the only thing that can
   * name them, which is what the second group was added for.
   *
   * Use keeps the ring wherever it stands: it is what this screen is for, and
   * it holds the ring through `preferredFocus` rather than by being the first
   * control, which a signed row's extra press is in front of. Delete is last
   * and is offered only where deleting is possible at all, so a row for the
   * table this game is using shows nothing to press.
   */
  const rowActions = (table: TableStatus, usable: boolean, group: "mine" | "device") => (
    <>
      {/* Ahead of Use, on the row that carries the amber mark. A signed table
          is refused with no message at all, so the press that makes the copy
          which does open belongs where the user is choosing which table to use
          rather than one screen further in. It consents to nothing: Review
          opens on the copy and the consent is given there.

          In front rather than among them, because a row lays its controls out
          from the right: anywhere else, a third press moved Use and whichever
          of Revoke or Delete the row carries into a different column from every
          other row, and a list whose buttons do not line up down the screen is
          what `rowActionStyle` exists to prevent. Measured on a Steam Deck the
          three of them sit inside the column Steam allows, with about nine
          pixels to spare, which is why the chip that says why this press is
          here is drawn in front of the row's name instead of in that column. */}
      {onPrepareCopy && canSelect && tableIsSigned(table) && table.available
        && (group === "mine" || !owners?.[table.sha256]) && (
        <div className={PREPARE_ACTION_CLASS} style={CONTENTS_ONLY}>
          <DialogButton style={rowActionStyle} disabled={selecting}
            onClick={traceUiAction("imported_tables_modal.prepare_copy", () => prepareCopy(table.sha256), { table_sha: table.sha256 })}
          >Prepare</DialogButton>
        </div>
      )}
      {canSelect && (group === "mine" || !owners?.[table.sha256]) && <DialogButton
        style={rowActionStyle}
        preferredFocus={usable}
        disabled={selecting || !usable}
        onClick={traceUiAction("imported_tables_modal.use", () => select(table.sha256), { table_sha: table.sha256 })}
      >Use</DialogButton>}
      {onRevoke && ((selectedBy[table.sha256]?.count ?? 0) > 0 || table.sha256 === activeSha256) && (
        <DialogButton style={rowActionStyle} disabled={selecting}
          onClick={traceUiAction("imported_tables_modal.revoke_or_confirm", () => {
            if (armed === table.sha256) {
              setArmed(null);
              revoke(table.sha256);
            } else {
              setFailure(null);
              setArmed(table.sha256);
            }
          }, { table_sha: table.sha256, confirm: armed === table.sha256 })}
        >{armed === table.sha256 ? "Confirm" : "Revoke"}</DialogButton>
      )}
      {/* Not offered where the answer is already known. Every table a game has
          selected is refused by the backend, so a press here could only ever
          spend itself on that refusal, and the row says which game holds it
          instead of leaving the reader to work out why nothing happened. The
          backend check stays: this is a snapshot and the answer can change
          under it. */}
      {onDelete && !((selectedBy[table.sha256]?.count ?? 0) > 0) && table.sha256 !== activeSha256 && (
        <DialogButton
          style={rowActionStyle}
          disabled={selecting}
          onClick={traceUiAction("imported_tables_modal.delete_or_confirm", () => {
            if (armed === table.sha256) {
              setArmed(null);
              remove(table.sha256);
              return;
            }
            setFailure(null);
            setArmed(table.sha256);
          }, { table_sha: table.sha256, confirm: armed === table.sha256 })}
        >{armed === table.sha256 ? "Confirm" : "Delete"}</DialogButton>
      )}
    </>
  );
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(0);
  /**
   * Tables this window has removed, which the arrays it was given still hold.
   *
   * This screen is opened once with the lists as they were, and the panel that
   * owns those lists cannot replace the props of a window already on screen. So
   * a deletion that succeeded left its own row sitting there, offering presses
   * against bytes that are gone. The window reconciles what it did itself, and
   * only what actually succeeded: `onDelete` rejects on failure, and a row is
   * taken away on the resolution rather than on the press.
   */
  const [removed, setRemoved] = useState<readonly string[]>([]);
  /** What went wrong with the last press, said on this screen rather than over it. */
  const [failure, setFailure] = useState<string | null>(null);

  /**
   * Everything this screen can act on, in the order a reader needs it.
   *
   * One sequence rather than two lists, because two lists paged separately put
   * twice the rows on a screen that fits one page of them. The order carries
   * what the two headings used to: the table this game is on comes first,
   * because the panel sends the reader here to authorize exactly that one and
   * sorting it alphabetically could put it pages away; then the rest of this
   * game's own; then what the device holds for other games.
   */
  const entries = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const gone = new Set(removed);
    const matches = (table: TableStatus) => !gone.has(table.sha256) && (!needle
      || table.filename.toLowerCase().includes(needle)
      || table.sha256.startsWith(needle));
    // Deterministic, so a page does not reshuffle under a thumb: by filename,
    // then by digest for two tables that share one.
    const byName = (left: TableStatus, right: TableStatus) =>
      left.filename.localeCompare(right.filename) || left.sha256.localeCompare(right.sha256);
    const mine = tables.filter(matches).sort(byName);
    const active = mine.filter((table) => table.sha256 === activeSha256);
    const rest = mine.filter((table) => table.sha256 !== activeSha256);
    const device = otherTables.filter(matches).sort(byName);
    return [
      ...active.map((table) => ({ table, group: "mine" as const })),
      ...rest.map((table) => ({ table, group: "mine" as const })),
      ...device.map((table) => ({ table, group: "device" as const })),
    ];
  }, [tables, otherTables, activeSha256, filter, removed]);
  const [listNode, setListNode] = useState<HTMLDivElement | null>(null);
  const [footerNode, setFooterNode] = useState<HTMLDivElement | null>(null);
  // What this screen actually fits, read from the screen. It was the one paged
  // list here that measured nothing and took six as an answer, and six ran off
  // the bottom of a Steam Deck's 534 pixel page. Nothing on this screen is
  // transient, so the first credible measurement is the settled one.
  const [chrome, setChrome] = useState<LatchedChrome | null>(null);
  useLayoutEffect(() => {
    setChrome((held) => latchChrome(held, listNode, footerNode, MODAL_BOTTOM_PADDING));
  }, [listNode, footerNode]);
  const tableRowHeight = useRowHeight(listNode, MANAGE_ROW_HEIGHT);
  const asked = useMemo(() => (chrome === null ? MANAGE_PAGE_SIZE : rowsThatFit({
    full: MANAGE_PAGE_SIZE,
    rowHeight: tableRowHeight,
    chrome: chrome.value,
    minimum: MIN_MANAGE_ROWS,
    node: listNode,
  })), [listNode, chrome, tableRowHeight]);
  // How far this window ended from Steam's bar, after that arithmetic has had
  // its go. This is the screen that made the rule worth having: it measured
  // what was above its list before the filter had been drawn, and the page it
  // sized from that ran 27 pixels behind the bar.
  const fitted = useFittedRows(footerNode, tableRowHeight, chrome !== null, entries.length > asked, failure ? 1 : 0);
  // Never past the page this screen was built with, for the same reason
  // `rowsThatFit` caps there: room found after the fact is not a reason to
  // change a screen that had no problem.
  const pageSize = Math.min(MANAGE_PAGE_SIZE, Math.max(1, asked + fitted));
  useEffect(() => {
    if (!listNode) return;
    logUi("manage.page_sized", {
      viewport: viewportHeight(listNode), chrome: chrome?.value ?? null, rows: pageSize, fitted,
    });
  }, [pageSize, listNode, chrome, fitted]);
  const pages = pageCount(entries.length, pageSize);
  const safePage = clampPage(page, entries.length, pageSize);
  const visible = pageItems(entries, safePage, pageSize);
  // A page of rows is held at the height a page of rows takes, measured off the
  // rows themselves. Every row on this list is one line of name over one line
  // of state, so one of them times a page is what a page costs, and the window
  // stays where it is on the last page and on a device whose whole library is
  // three tables and never fills one.
  const pageHeight = usePageHeight(listNode, visible.length, pageSize);
  // What this screen still has, which is what it was given minus what it has
  // removed. Asking the props instead left a screen whose last table had just
  // been deleted saying nothing matches a filter nobody set, and offering a
  // filter and a pager for rows that are gone.
  const remaining = useMemo(() => {
    const gone = new Set(removed);
    return tables.concat(otherTables).filter((table) => !gone.has(table.sha256));
  }, [tables, otherTables, removed]);
  const filterable = remaining.length > pageSize || filter.trim().length > 0;
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const backRef = useRef<HTMLDivElement | null>(null);
  const restoreAfterDelete = useRef<number | null>(null);
  useEffect(() => {
    if (selecting || restoreAfterDelete.current === null) return;
    const index = Math.min(restoreAfterDelete.current, visible.length - 1);
    restoreAfterDelete.current = null;
    const candidates = [...visible.slice(index), ...visible.slice(0, index).reverse()];
    focusFirstEnabled(...candidates.map(({ table }) => ({ current: rowRefs.current.get(table.sha256) ?? null })), backRef);
  }, [selecting, removed, safePage]);
  const mineCount = entries.filter((entry) => entry.group === "mine").length;
  const deviceCount = entries.length - mineCount;

  /**
   * Anything that moves the reader is an answer to the armed question.
   *
   * The confirmation is two presses on one control, which only means anything
   * while they are one interaction. Paging the row away and coming back to it
   * hours later must not find it still armed and one press from destroying a
   * file, so every navigation here disarms it.
   */
  const moveTo = (next: () => void) => {
    setArmed(null);
    setFailure(null);
    next();
  };
  return (
    <ModalRoot onCancel={traceUiAction("imported_tables_modal.close", close)} onEscKeypress={traceUiAction("imported_tables_modal.close_2", close)}>
      <DensePanel tightRows>
        {/* First, and one row tall. It is the only way to a local file and the
            only thing here that works on a device holding nothing, and a list
            of tables is exactly what a reader never scrolls to the end of: put
            below several pages of them it may as well not exist.

            Drawn whether or not a game is chosen, and pressable either way.
            The file joins this device's library with no game at all, which is
            what the backend has always allowed; what waits for a game is the
            association and the authorization, and Use on the row this import
            produces is where those happen. Taking the section away left the
            reader with no sign the route existed on the one screen that carries
            it. */}
        <PanelSection>
          <SectionHeading>Open a file</SectionHeading>
          <PanelSectionRow>
            <PanelRow
              testId="open-local-table"
              truncate
              label="A table on this device"
              description="A .CT, or a .zip, .7z or .rar holding one. It is reviewed and authorized like any other."
              actions={(
                <SmallButton
                  disabled={selecting || !onOpenLocalFile}
                  onClick={traceUiAction("imported_tables_modal.local_file", () => {
                    // The prop stays optional: a screen opened without this
                    // route may not press it.
                    if (!onOpenLocalFile) return;
                    // This window first: Decky's picker is a modal of its own,
                    // and one raised over another is a window behind a window.
                    onClose();
                    onOpenLocalFile();
                  })}
                >Local file</SmallButton>
              )}
            />
          </PanelSectionRow>
        </PanelSection>
        <PanelSection>
          <SectionHeading>Tables</SectionHeading>
          {/* Said here, on the screen the press was made on. A window raised
              over this one is a window behind a window on the device, and the
              row this is about is still in front of the reader. */}
          {failure && (
            <PanelSectionRow>
              <PanelRow testId="manage-failure" status label="That did not work" description={failure} />
            </PanelSectionRow>
          )}
          {remaining.length === 0 && (
            <PanelSectionRow>
              <Field
                label="Nothing imported yet"
                description="Open a file above, or search online."
              />
            </PanelSectionRow>
          )}
          {remaining.length > 0 && entries.length === 0 && (
            <PanelSectionRow>
              <Field label="Nothing matches that" description="Clear the filter to see every table." />
            </PanelSectionRow>
          )}
          {/* The row that heads the list, drawn as one. As an ordinary field it
              was the same weight as the tables under it, so the first thing on
              the list looked like the first table on it. `tone="header"` is the
              shape this panel already uses for a row that names what follows:
              its own ground, an accent, and a gap under it holding the rows off
              itself.

              One short line under it. It carried three sentences explaining
              that choosing a table associates it and opens the review screen,
              which is what the next press shows the reader anyway, and it wrapped
              to three lines of a page that is counted in rows. What is left is
              the part that is not obvious from doing it: this screen needs
              neither a source nor a network. */}
          {/* The filter rides on the row that heads the list rather than taking
              a row of its own. A row each is what Steam's own components do,
              and on a Steam Deck this one cost 46 of 534 pixels for a control
              holding at most a few characters - which is one table off the
              page, on the screen whose whole job is listing them. Its word is
              inside the box, as the placeholder, where it is gone the moment
              there is anything to read.

              In the row's action group and not beside its trailing text: that
              slot is for a word, it is dimmed and set small on purpose, and a
              row whose only control sat there would not be wrapped in anything
              the controller can enter.

              Drawn whenever the filter is reachable, not only when the list
              has rows in it: a filter that matches nothing empties the list,
              and a filter that disappears with the last row it matched is one
              nobody can clear.

              Its line is short because the filter beside it takes the width
              that line is not using, and a sentence that does not fit is a
              sentence nobody reads; `scroll` reveals whatever still clips while
              the reader is on the row.

              It is also the only thing heading this list in either state. With
              no game selected there used to be a bare sentence above it, a
              `Field` carrying a description and no label, which Steam draws at
              description weight: a pale line that read as an offcut rather than
              as a heading, with this row doing the heading's work directly
              under it. The sentence is this row's description now, so one row
              heads the list whether or not a game is chosen. */}
          {(entries.length > 0 || filterable) && (
            <PanelSectionRow>
              <PanelRow
                testId="manage-summary"
                tone="header"
                truncate
                scroll
                fillWithActions={filterable}
                label={canSelect ? `${mineCount} here, ${deviceCount} elsewhere` : `${mineCount + deviceCount} on this device`}
                description={canSelect ? "No provider or network needed." : "Choose a game to use one."}
                actions={filterable ? (
                  <div style={MANAGE_FILTER}>
                    <FilterField
                      placeholder="Filter"
                      disabled={selecting}
                      value={filter}
                      onChange={traceUiEdit("imported_tables_modal.filter_by_name_or_digest", (event: any) => moveTo(() => {
                        setFilter(String(event.target.value ?? ""));
                        setPage(0);
                      }))}
                    />
                  </div>
                ) : undefined}
              />
            </PanelSectionRow>
          )}
          {/* One box around the page, so it can be held at the height a whole
              page of rows takes. Reaching the end of the list, or filtering it
              down to a couple of rows, used to shorten the window and take
              every control under it with it. */}
          <div ref={setListNode} style={pageHeight === null ? undefined : { minHeight: pageHeight }} data-testid="manage-list">
          {/* `scroll`: a table's name and what it is doing are both routinely
              longer than this window, and both are what tell one row from the
              next, so the end of each is exactly what a one-line row cannot
              show. They reveal themselves while the ring is on the row, the
              same way a pinned cheat's two lines do on the panel.

              The signed chip goes in front of a row's name, where the two
              glyphs do not. A row's controls sit in a column Steam caps at a
              share of the row's width, and measured on a Steam Deck that column
              has about nine pixels of room left once a signed row has its three
              presses: a chip there fits only while the row carries no
              compatibility glyph beside it, and the moment it does, that row's
              buttons stand in a different column from every other row on the
              screen. In front of the name it takes its width from the name
              instead, which clips and reveals on the ring like every other name
              here. The glyphs stay where glyphs are: sixteen pixels each, and
              two of them together are well inside what that column holds. */}
          {visible.map(({ table, group }, index) => (
            <PanelSectionRow key={table.sha256}>
              <PanelRow
                testId={`${group === "mine" ? "imported" : "stored"}-table-${table.sha256.slice(0, 8)}`}
                truncate
                scroll
                tone={group === "device" ? "aside" : "own"}
                label={rowLabel(table, group, index)}
                description={rowDescription(table)}
                leadingMark={tableIsSigned(table) ? <SignedMark /> : undefined}
                mark={<>
                  <CompatibilityMark evidence={compatibility.find((entry) => entry.table_sha256 === table.sha256)} blocked={blockedReasons[table.sha256] ?? null} />
                  {table.derived_from ? <DerivedMark /> : null}
                </>}
                actions={<div style={CONTENTS_ONLY} ref={(node) => {
                  if (node) rowRefs.current.set(table.sha256, node);
                  else rowRefs.current.delete(table.sha256);
                }}>{rowActions(table, table.available && !isCompatibilityFailure(blockedReasons[table.sha256])
                  && (table.sha256 !== activeSha256 || !activeAuthorized), group)}</div>}
              />
            </PanelSectionRow>
          ))}
          </div>
        </PanelSection>
      </DensePanel>
      {/* The same footer the search screen ends with: paging in the middle,
          the way out at the right edge, on one row that is there whatever the
          list is doing. Manage used to page from a row of its own inside the
          list and leave Back below it, so a filter that took the list down to
          one page moved every control under it. */}
      <PagerFooter
        testId="manage-footer"
        style={MANAGE_FOOTER}
        containerRef={setFooterNode}
        page={safePage}
        pages={pages}
        preferNext
        disabled={selecting}
        onPage={(next) => moveTo(() => setPage(next))}
        trailing={<div ref={backRef} style={CONTENTS_ONLY}><SmallButton disabled={selecting} onClick={traceUiAction("imported_tables_modal.back", close)}>Back</SmallButton></div>}
      />
    </ModalRoot>
  );
}
