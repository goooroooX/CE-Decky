import { useUiSurface } from "../useUiSurface";
import { traceUiAction, traceUiEdit, startUiOperation } from "../uiActions";
import {
  DialogButton,
  DropdownItem,
  Field,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  Spinner,
  TextField,
  Toggle,
  ToggleField,
} from "@decky/ui";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { modalActionStyle } from "../components/ModalActions";
import { CheatRow, cheatRowActionStyle, toggleBoxStyle } from "../components/CheatRow";
import { ActionGroup, CONTENTS_ONLY, DensePanel, FilterField, PanelNote, PanelRow, RefusalBlock, SectionHeading, SideBySide, SmallButton, SwitchBox, useFittedRows, usePageHeight, useRowHeight } from "../components/PanelDensity";
import { PagerFooter } from "../components/PagerFooter";
import { applyRuntimeSelection, queryRuntimeControlsPartial, RuntimeQueryAbortedError, type RuntimeDesiredState } from "../runtimeClient";
import {
  CONTROL_PAGE_SIZE,
  clampPage,
  controlAcceptsTypedValue,
  DROPDOWN_SEARCH_THRESHOLD,
  describeValueChoices,
  matchingDropdownValues,
  inactiveAncestorControl,
  inactiveAncestorControls,
  controlIsSwitch,
  controlNeedsValueInput,
  controlsMissingRequiredValue,
  pinnedMissingRequiredValue,
  unsavedChangeCount,
  missingRequiredValueReason,
  controlRowContext,
  controlRowLabel,
  controlSections,
  controlsForSection,
  displayableControlValue,
  presentableControlValue,
  effectiveStartupPreferences,
  enclosingControlIds,
  scriptListedControlIds,
  filterControls,
  pageCount,
  pageItems,
  rememberedSelection,
  rememberedSelectionBudgetError,
  safeActionableControls,
  switchValuesFor,
  switchesLeftOn,
  switchesToHoldOff,
  unusedActiveScripts,
} from "../uiModel";
import type { ConfiguredValue, RuntimeEnvelope, RuntimeResult, StartupPreference, TableControl, TableInspection } from "../types";
import { describeError, leadWithCause } from "../errors";
import { durableResidue } from "../durableWrite";
import { logUi, logUiFailure } from "../supportLog";
import { MODAL_BOTTOM_PADDING, isShortScreen, latchChrome, rowsThatFit, viewportHeight, type LatchedChrome } from "../viewport";

interface StagedState {
  active: boolean | null;
  value: string | null;
}

/**
 * How tall one closed cheat block is, for fitting a page to the screen.
 *
 * The 40 pixels `CHEAT_ROW_CLASS` holds a closed block's field to, plus the
 * four between one block and the next. It is a
 * constant because the row is now a constant: both of its lines are clamped to
 * one, and `CHEAT_ROW_CLASS` holds the height of a row that has only one of
 * them. A row opened by `More` is taller and is not part of this: it is one row
 * of the page and the reader put it there.
 */
const CHEAT_ROW_HEIGHT = 44;

/**
 * The fewest cheats a page may hold before the measurement stops taking any.
 *
 * A page of one is not a smaller version of this screen. Below this, whatever
 * the screen says, the reader is better served by a page they scroll than by
 * paging through a table two records at a time.
 */
const MIN_CHEAT_ROWS = 3;

interface Props {
  appId: number;
  inspection: TableInspection;
  /**
   * `false` when Cheat Engine is not running for this table.
   *
   * The picker then edits this table's stored configuration instead of a live
   * session, so a user can set a game up before starting it. Nothing is written
   * to Cheat Engine and nothing pretends to be a live state.
   */
  live: boolean;
  /**
   * Why this table cannot be driven live even though Cheat Engine is running.
   *
   * A table past the live-control budget cannot be read back in full, and Apply
   * requires exactly that: it mutates, then re-reads every safe control to
   * confirm. Both reads are guaranteed to exceed the same limit, so a mutation
   * that had actually succeeded came back as a partial or unknown outcome.
   *
   * Deliberately not expressed as `live: false`. That means "Cheat Engine is
   * not running", which would be a false statement here and would put the wrong
   * sentence in front of the user; what it decides about Apply and Auto-load is
   * right either way, because a cheat switched on for a table this size can
   * only reach Cheat Engine through the next start.
   */
  liveUnavailableReason?: string | null;
  pinned: number[];
  startupPreferences: StartupPreference[];
  rememberedPreferences: StartupPreference[];
  configuredValues: ConfiguredValue[];
  onCompatibilityConfirmed?: () => Promise<void>;
  /**
   * `leftOn` is what this press left switched on because the table's own code
   * was not read as surviving those cheats being switched off. Only this screen
   * knows which scripts a press started, so the list comes from here.
   */
  onApplied: (
    remembered: StartupPreference[],
    envelope: RuntimeEnvelope | null,
    leftOn?: readonly TableControl[],
  ) => Promise<void> | void;
  /** Whether this game already starts Cheat Engine by itself for this table, which decides what Apply has to disclose. */
  autoloadEnabled: boolean;
  /** Persist one pin/unpin for this exact table and resolve the confirmed pinned IDs. */
  onTogglePin: (recordId: number, pinned: boolean) => Promise<number[]>;
  /** Persist user-entered values independently from mutable live CE reads. */
  onSaveConfiguredValues: (values: ConfiguredValue[]) => Promise<void>;
  /**
   * Cheat Engine ran a cheat from this table and it came straight back off.
   *
   * That is about the table, not about this dialog: it is what a table written
   * for a different build of the game does, every time. The panel records it
   * against the exact table and asks the user what to do with it, because a
   * message inside this dialog is the one place a user configuring cheats will
   * scroll past without reading.
   */
  onTableRefused?: (reason: string) => Promise<void> | void;
  /** Ask the backend for the exact expanded startup plan this selection implies. */
  onValidateStartupPlan: (
    remembered: StartupPreference[],
    configuredValues: ConfiguredValue[],
  ) => Promise<{ action_count: number; limit: number; fits: boolean }>;
  onSnapshot?: (results: RuntimeResult[], envelope: RuntimeEnvelope) => void;
  onSnapshotInvalidated?: () => void;
  onCancel: () => void;
}

export function CheatSelectionModal({ appId, inspection, live, liveUnavailableReason = null, autoloadEnabled, pinned, startupPreferences, rememberedPreferences, configuredValues, onApplied, onCompatibilityConfirmed, onTogglePin, onSaveConfiguredValues, onTableRefused, onValidateStartupPlan, onSnapshot, onSnapshotInvalidated, onCancel }: Props) {
  useUiSurface("CheatSelectionModal", inspection.sha256);
  // Every path that reads or writes the running session asks this, never `live`
  // on its own: a session that cannot answer a full read is as unusable for
  // those as no session at all, and treating it otherwise is what manufactured
  // failed Applies on large tables.
  const storedOnly = !live || Boolean(liveUnavailableReason);
  /**
   * What this screen does while nothing is running, in one line and in full.
   *
   * It was three sentences in a plain field, so on a handheld it wrapped to
   * four or five lines above a list that is counted in rows. The line says the
   * whole of what a reader has to decide on - Apply stores this, it runs later
   * - and everything behind it, including the rule that arms Auto-load and the
   * exact reason live control is unavailable, is one press away.
   */
  const storedOnlyLine = "Apply saves this table's selection; it runs when the game next starts.";
  const storedOnlyHelp = liveUnavailableReason
    ? `${liveUnavailableReason} Apply saves the selection for this exact table and does not change the Cheat Engine running now; it is applied the next time this game starts Cheat Engine.`
    : autoloadEnabled
      ? "Nothing is running for this table, so Apply saves the selection for this exact table and CE Decky applies it when this game next starts."
      : "Nothing is running for this table. Apply saves the selection for this exact table, and switching a cheat on also switches on Load last table & cheats, so CE Decky starts Cheat Engine with this game and applies it; switching every cheat off again switches that back off. A value on its own is only stored, and is written when its cheat is switched on.";
  // Pinning is a profile write, never a runtime mutation, so it is committed
  // immediately and kept out of the Apply diff.
  const [pinnedView, setPinnedView] = useState<number[]>(pinned);
  const [pinning, setPinning] = useState(false);
  const pinningRef = useRef(false);
  const safeControls = useMemo(() => safeActionableControls(inspection), [inspection]);
  const controlById = useMemo(
    () => new Map(safeControls.flatMap((control) => control.id === null ? [] : [[control.id, control] as const])),
    [safeControls],
  );
  // This modal owns the configured values while it is open. The parent renders
  // it once through Decky's modal root, so the prop never updates after a save;
  // keeping the committed map here is what lets a second Apply build on the
  // first instead of reverting it. It is a ref, not state, because the initial
  // exact-session read must depend on the table and session only - re-running
  // that query would discard the user's unapplied edits.
  const configuredByIdRef = useRef(new Map(configuredValues.map((item) => [item.record_id, item.value])));
  const sections = useMemo(() => controlSections(inspection, safeControls), [inspection, safeControls]);
  const enclosingIds = useMemo(() => enclosingControlIds(safeControls), [safeControls]);
  // What the Scripts toggle hides, which is more than what Apply treats as a
  // dependency: an attach-only record is machinery too, but nothing is ever
  // switched on through it.
  const scriptListedIds = useMemo(() => scriptListedControlIds(safeControls), [safeControls]);
  const [showScripts, setShowScripts] = useState(false);
  // Set when an Apply failed after the first runtime command: the modal is then
  // showing reconciled live state, not the user's staged intent, and closing
  // cannot undo what the game already holds.
  /**
   * What an aborted Apply left behind. These are independent: one Apply can
   * change the running game *and* commit this table's saved configuration, and
   * a mutually exclusive answer described only the live half - after which
   * reconciliation cleared the dirty sets, so the close prompt fell silent
   * about a configured value that survives into the next Auto-load.
   */
  const [partialCommit, setPartialCommit] = useState<{
    runtimeChanged: boolean;
    durableChanged: boolean;
    durableUnknown: boolean;
  } | null>(null);
  const notePartialCommit = (next: Partial<{ runtimeChanged: boolean; durableChanged: boolean; durableUnknown: boolean }>) =>
    setPartialCommit((current) => ({
      runtimeChanged: false, durableChanged: false, durableUnknown: false, ...current, ...next,
    }));
  const [sectionKey, setSectionKey] = useState("all");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState<number | null>(null);
  // Which record's value search is on screen and what has been typed into it.
  // Kept with the record rather than beside it so that opening another row
  // starts empty, and closing this one forgets a query that named nothing.
  const [valueQuery, setValueQuery] = useState<{ id: number; text: string } | null>(null);
  // The one record whose free field the reader asked for. A table never says its
  // list is exhaustive - not one of the 1414 list records in the corpus declares
  // `DropDownReadOnly` - so the field has to stay reachable, and putting it
  // beside every list put two controls in front of a reader who needed one.
  const [typedValue, setTypedValue] = useState<number | null>(null);
  const [lastConfirmed, setLastConfirmed] = useState<Record<number, StagedState>>({});
  const [confirmingClose, setConfirmingClose] = useState(false);
  const [staged, setStaged] = useState<Record<number, StagedState>>({});
  const [touchedValues, setTouchedValues] = useState<Set<number>>(new Set<number>());
  const [touchedActive, setTouchedActive] = useState<Set<number>>(new Set<number>());
  const [loading, setLoading] = useState(true);
  // Records an inactive enclosing script has not created yet. They are not an
  // error; they simply cannot be read or set until their parent runs.
  const [unavailableRecords, setUnavailableRecords] = useState<Set<number>>(new Set());
  const [applying, setApplying] = useState(false);
  const applyingRef = useRef(false);
  // What the last Apply left switched on because the table's own code was not
  // read as surviving those cheats being switched off. Handed to the caller,
  // which is what puts it in front of the user.
  const leftOnRef = useRef<readonly TableControl[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [errorTitle, setErrorTitle] = useState("Cannot apply");

  useEffect(() => {
    let cancelled = false;
    // A local flag only suppressed the state update; the multi-batch query kept
    // issuing generations against the same control log after this modal was
    // gone, so an invisible orphan could race whatever the user pressed next.
    const aborter = new AbortController();
    setLoading(true);
    setError(null);
    if (storedOnly) {
      // Nothing usable is running, so the only truth available is what this exact
      // table was configured to do. Everything else is off, which is what it
      // will be when Cheat Engine loads the table.
      const next: Record<number, StagedState> = {};
      for (const control of safeControls) {
        if (control.id === null) continue;
        next[control.id] = { active: false, value: null };
      }
      for (const preference of effectiveStartupPreferences(startupPreferences, rememberedPreferences)) {
        if (!(preference.record_id in next)) continue;
        next[preference.record_id] = {
          active: preference.active ?? false,
          value: preference.value,
        };
      }
      // Nothing is running, so the value this table was configured with is the
      // only real answer for it; the remembered copy above may hold whatever
      // Cheat Engine happened to report last.
      for (const [recordId, configured] of configuredByIdRef.current) {
        if (!(recordId in next)) continue;
        next[recordId] = { active: next[recordId].active, value: configured };
      }
      setStaged(next);
      setLastConfirmed(next);
      setLoading(false);
      return () => { cancelled = true; aborter.abort(); };
    }
    // The previous Home snapshot is no longer authoritative once this modal
    // starts a fresh exact-session read. If the read fails, Home must degrade to
    // "Connected" rather than continue showing stale cheat states.
    onSnapshotInvalidated?.();
    // Cheat Engine only creates the record inside a script while that script
    // runs, so an inactive parent legitimately has children that cannot be read
    // yet. Aborting the whole model on the first of them left this picker with
    // no confirmed state at all, which then broke its own ancestor-activation
    // logic: selecting a child could no longer see a confirmed-off parent to
    // switch on first. Initialize every readable record and represent the rest
    // as blocked by their parent.
    void queryRuntimeControlsPartial(
      appId,
      safeControls.flatMap((control) => control.id === null ? [] : [control.id]),
      undefined,
      aborter.signal,
    )
      .then(({ results, unavailable, envelope }) => {
        if (cancelled) return;
        const next: Record<number, StagedState> = {};
        for (const result of results) {
          if (result.record_id === null) continue;
          // A live session is the truth for a value Cheat Engine can actually
          // read. It answers `??` for an address that does not exist yet, and
          // adopting that placeholder is exactly what used to lose the value
          // the user configured, so the stored choice stands in for it.
          //
          // Where there is no stored choice either, this record has no value
          // rather than a value of `??`. Keeping the placeholder put it in the
          // editor itself, which is a field the user then has to clear before
          // they can type into it, and a dropdown whose selection matches none
          // of the options it is offering.
          next[result.record_id] = {
            active: result.active,
            value: displayableControlValue(result.value) === null
              ? configuredByIdRef.current.get(result.record_id) ?? null
              : result.value,
          };
        }
        setStaged(next);
        setLastConfirmed(next);
        setUnavailableRecords(new Set(
          unavailable.flatMap((result) => result.record_id === null ? [] : [result.record_id]),
        ));
        onSnapshot?.(results, envelope);
      })
      .catch((cause) => {
        // This query owns an AbortController and its cleanup aborts it, and the
        // runtime client throws between batches when that happens. Closing the
        // picker mid-query is the user doing what the control is for, so it is
        // not a failure and must not be recorded as one, exactly as the panel's
        // own boundaries already treat it.
        if (cause instanceof RuntimeQueryAbortedError) return;
        // Otherwise: this modal owns its own query, so nothing else records a
        // session or acknowledgement identity that did not match after the
        // backend answered every call.
        logUiFailure("cheats.initial_query_failed", cause, { appId, table: inspection.sha256.slice(0, 12) });
        if (!cancelled) setError(describeError(cause));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; aborter.abort(); };
  }, [appId, inspection.sha256, storedOnly]);

  const selectedSection = sections.find((section) => section.key === sectionKey) ?? sections[0];
  const sectionControls = useMemo(
    () => controlsForSection(safeControls, selectedSection, pinnedView),
    [safeControls, selectedSection, pinnedView],
  );
  const searched = useMemo(() => filterControls(sectionControls, search), [sectionControls, search]);
  // The scripts that build a table's cheats are machinery, not choices: Apply
  // switches on whatever the selected cheats need, so they are hidden until the
  // user asks to see them.
  const hiddenScripts = searched.filter((control) => control.id !== null && scriptListedIds.has(control.id)).length;
  const matching = showScripts ? searched : searched.filter((control) => control.id === null || !scriptListedIds.has(control.id));
  /**
   * How many cheats this screen puts on one page, on the screen it is drawn on.
   *
   * `CONTROL_PAGE_SIZE` is what a 1280x800 modal fits and stays the answer
   * wherever there is room for it. A Steam Deck gives a modal 534 CSS pixels
   * against a television's 844, and six rows plus this screen's own chrome do
   * not fit in the first of those: the window scrolled, which on a controller
   * is a page the reader has to work to see the end of.
   *
   * The chrome is read rather than estimated, because it is Steam's own
   * dropdown and text field above the list and this plugin's footer below it,
   * and a figure guessed for that is wrong on one of the two screens. Both
   * nodes are held in state rather than in refs, because the first render has
   * neither and the answer has to be asked again once it does.
   */
  const [listNode, setListNode] = useState<HTMLDivElement | null>(null);
  const [footerNode, setFooterNode] = useState<HTMLDivElement | null>(null);
  // The window itself, which this screen asks only how tall a screen it is on.
  const [windowNode, setWindowNode] = useState<HTMLDivElement | null>(null);
  // Measured once and then held. Re-measuring on every content change made the
  // page ratchet: a filter keystroke changed the row count, the re-measure read
  // the layout the previous answer had already shrunk, and the next keystroke
  // shrank it again down to the minimum with nothing to bring it back.
  // The wait for the running Cheat Engine's values is this screen's transient:
  // a row saying so sits above the list while it is out, and latching against
  // that sized the page for a layout the reader never settles on.
  const [chrome, setChrome] = useState<LatchedChrome | null>(null);
  useLayoutEffect(() => {
    setChrome((held) => latchChrome(held, listNode, footerNode, MODAL_BOTTOM_PADDING, !loading));
  }, [listNode, footerNode, loading]);
  const cheatRowHeight = useRowHeight(listNode, CHEAT_ROW_HEIGHT);
  // How far this screen's window ended from Steam's bar, after the arithmetic
  // above has had its go. Every paged screen here asks the same question the
  // same way, and answers it in rows of its own list.
  const asked = useMemo(() => (chrome === null ? CONTROL_PAGE_SIZE : rowsThatFit({
    full: CONTROL_PAGE_SIZE,
    rowHeight: cheatRowHeight,
    chrome: chrome.value,
    minimum: MIN_CHEAT_ROWS,
    node: listNode,
  })), [listNode, chrome, cheatRowHeight]);
  // Both of the blocks this screen mounts under its list on its own: a refusal
  // it has to report, and the acknowledgement it asks for before closing over
  // unapplied edits. Either one moves the footer down without the list or the
  // footer changing at all.
  const fitLayout = (error ? 1 : 0) | (confirmingClose ? 2 : 0);
  const fitted = useFittedRows(footerNode, cheatRowHeight, chrome?.settled === true, matching.length > asked, fitLayout);
  // Never past the page this screen was built with: `rowsThatFit` caps
  // there so a television stays exactly as it was, and room found after the
  // fact is not a reason to change a screen that had no problem.
  const pageSize = Math.min(CONTROL_PAGE_SIZE, Math.max(1, asked + fitted));
  // The two numbers this page is sized from, recorded when they settle. The
  // height is read from the page and the chrome from its own nodes, so a page
  // that comes out wrong on a screen nobody here has is answerable from a
  // support bundle rather than from a second device session.
  useEffect(() => {
    if (!listNode) return;
    // Not the control count: it is read here but changes with every filter
    // keystroke, and including it would either log a stale number or write a
    // record per keystroke into a bounded ring. What this table holds is
    // already on `panel.modal_opened`.
    logUi("cheats.page_sized", { viewport: viewportHeight(listNode), chrome: chrome?.value ?? null, settled: chrome?.settled ?? null, rows: pageSize, fitted });
  }, [pageSize, listNode, chrome, fitted]);
  const pages = pageCount(matching.length, pageSize);
  const safePage = clampPage(page, matching.length, pageSize);
  // The way out of the window, which is where the ring goes when a page turn
  // leaves neither paging control pressable. `PagerFooter` owns the rest of
  // that rule, because it is the same rule on every paged screen here.
  const cancelRef = useRef<HTMLDivElement | null>(null);
  const visible = pageItems(matching, safePage, pageSize);
  // What a full page of this list measured, so a short last page holds the
  // window at the same height instead of moving Apply and the way out.
  // Keyed to the page size it was measured for: this screen settles on its
  // page a commit after it opens, and a height held from the page it started
  // with padded the window past the bottom of a Steam Deck's display.
  const fullPageHeight = usePageHeight(listNode, visible.length, pageSize);
  const activeCount = Object.values(staged).filter((state) => state.active === true).length;
  // Whether this screen is drawn on a handheld, which decides how much of it is
  // a second line. `windowNode` is how it asks about the page it is actually in,
  // which is never the page this code runs in.
  const tight = isShortScreen(windowNode);
  const countsLine = `${safeControls.length} supported \u00b7 ${matching.length} shown${!showScripts && hiddenScripts > 0 ? ` \u00b7 ${hiddenScripts} script${hiddenScripts === 1 ? "" : "s"} hidden` : ""}`;

  const update = (recordId: number, patch: Partial<StagedState>) => {
    setStaged((current) => ({
      ...current,
      [recordId]: { active: current[recordId]?.active ?? null, value: current[recordId]?.value ?? null, ...patch },
    }));
  };

  const touchValue = (recordId: number, value: string) => {
    if (applyingRef.current || pinningRef.current) return;
    clearError();
    update(recordId, { value });
    setTouchedValues((current) => new Set(current).add(recordId));
  };

  /**
   * Drop the last refusal, which every action after it makes history.
   *
   * A refusal describes one press against the state at that moment, and it
   * stops being true the moment anything moves: switching off the cheat it
   * named, typing the value it asked for, or simply paging away to something
   * else all leave a red block on screen about a press nobody is still making.
   * It is cleared by every user-driven change here rather than only by the next
   * Apply, because the reader has to be able to get rid of it.
   *
   * `partialCommit` is deliberately not touched. That is not a refusal, it is
   * the acknowledgement that something reached the running game or this table's
   * saved state, and it is owed until the window is closed.
   */
  const clearError = () => setError(null);
  /**
   * What the refusal on screen is about, which is not always Apply.
   *
   * Pinning refuses here too, and it is a different press: reporting it under
   * "Cannot apply" tells the reader their Apply failed when they pressed Pin,
   * and sends them looking at the wrong control. The heading follows whatever
   * set the line.
   */
  const refuse = (title: string, detail: string) => {
    setErrorTitle(title);
    setError(detail);
  };

  const togglePin = async (recordId: number, next: boolean) => {
    if (pinningRef.current || applyingRef.current) return;
    pinningRef.current = true;
    setPinning(true);
    setError(null);
    const operation = startUiOperation("cheats.pin", { app_id: appId, table_sha: inspection.sha256, record_id: recordId, pinned: next });
    try {
      setPinnedView(await onTogglePin(recordId, next));
      operation.completed();
    } catch (cause) {
      operation.failed(cause);
      refuse("Cannot pin", describeError(cause));
    } finally {
      pinningRef.current = false;
      setPinning(false);
    }
  };

  const touchActive = (recordId: number, active: boolean) => {
    if (applyingRef.current || pinningRef.current) return;
    // The two edits that can make a refusal false outright: it very often names
    // this exact record, and switching it off or filling its value is the
    // reader doing what it asked.
    clearError();
    update(recordId, { active });
    setTouchedActive((current) => new Set(current).add(recordId));
  };

  /**
   * Re-read the exact session after a failed Apply and say what it really holds.
   *
   * `applyRuntimeSelection()` sends several batches and deliberately does not
   * roll back the ones already acknowledged, which is only safe if the caller
   * then reconciles. Without this the form kept showing the user's intent while
   * the game held something else - most often an enclosing script that was
   * switched on before a child underneath it failed - and closing with Discard
   * reset the form without restoring anything.
   *
   * A failure before any command took effect is the ordinary transient case, so
   * the staged edits survive it and Apply can simply be pressed again. Only a
   * confirmed difference from the last confirmed state adopts the live answer
   * and labels the outcome partial.
   */
  const reconcileLiveState = async (): Promise<boolean> => {
    let observed;
    try {
      observed = await queryRuntimeControlsPartial(
        appId,
        safeControls.flatMap((control) => control.id === null ? [] : [control.id]),
      );
    } catch (cause) {
      // The session could not be re-read, so the modal cannot claim to know the
      // live state either. Say that rather than implying the intent is current.
      // It is also the reconciliation after a failed Apply, which is the moment
      // a report most needs a record of: the game may hold something the form
      // no longer describes.
      logUiFailure("cheats.reconcile_failed", cause, { appId });
      notePartialCommit({ runtimeChanged: true });
      return true;
    }
    setUnavailableRecords(new Set(
      observed.unavailable.flatMap((result) => result.record_id === null ? [] : [result.record_id]),
    ));
    const reconciled: Record<number, StagedState> = {};
    let changed = false;
    for (const result of observed.results) {
      if (result.record_id === null) continue;
      const configured = configuredByIdRef.current.get(result.record_id) ?? null;
      const state: StagedState = {
        active: result.active,
        value: displayableControlValue(result.value) === null ? configured : result.value,
      };
      reconciled[result.record_id] = state;
      const previous = lastConfirmed[result.record_id];
      if (!previous || previous.active !== state.active || previous.value !== state.value) changed = true;
    }
    onSnapshot?.(observed.results, observed.envelope);
    setLastConfirmed(reconciled);
    if (!changed) return false;
    setStaged(reconciled);
    setTouchedActive(new Set<number>());
    setTouchedValues(new Set<number>());
    notePartialCommit({ runtimeChanged: true });
    return true;
  };

  const apply = async () => {
    if (applyingRef.current || pinningRef.current || loading) return;
    applyingRef.current = true;
    setApplying(true);
    setErrorTitle("Cannot apply");
    setError(null);
    setPartialCommit(null);
    // Configured values are committed before any runtime write so a later
    // read-back cannot overwrite the user's typed choice. That ordering is
    // deliberate, but it means a failure after it leaves durable state the
    // close prompt used to offer to "discard".
    let committedDurable = false;
    let mutatedRuntime = false;
    const operation = startUiOperation("cheats.apply", { app_id: appId, table_sha: inspection.sha256, active_changes: touchedActive.size, value_changes: touchedValues.size });
    try {
      // Switching a cheat on switches on every script that encloses it, because
      // Cheat Engine only creates the inner record while the outer script runs.
      // The user asked for the cheat; making them hunt for its parents first is
      // a puzzle, not a safety boundary.
      const effective: Record<number, StagedState> = { ...staged };
      const effectiveTouchedActive = new Set(touchedActive);
      // Scripts CE Decky switched on or off by itself. They are sent to Cheat
      // Engine like any other change, but they are not a user's choice, so they
      // never reach the remembered state - one that did switched itself back on
      // in the next session with every cheat under it off.
      const pluginManaged = new Set<number>();
      const activeById = new Map<number, boolean | null>(
        safeControls.flatMap((control) => control.id === null ? [] : [[control.id, staged[control.id]?.active ?? null] as const]),
      );
      // What is actually running, which is not what `staged` says: staged is
      // this screen's pending state and already carries the reader's unapplied
      // toggles. The difference between it and the state above is the set of
      // scripts this press starts, which is the only set whose declared
      // defaults arrive with it.
      const activeBefore = new Map<number, boolean | null>(
        safeControls.flatMap((control) => control.id === null ? [] : [[control.id, lastConfirmed[control.id]?.active ?? null] as const]),
      );
      for (const recordId of [...touchedActive, ...touchedValues]) {
        if (effective[recordId]?.active !== true) continue;
        const control = controlById.get(recordId);
        if (!control) continue;
        for (const ancestor of inactiveAncestorControls(control, safeControls, activeById)) {
          if (ancestor.id === null) continue;
          effective[ancestor.id] = { active: true, value: effective[ancestor.id]?.value ?? null };
          effectiveTouchedActive.add(ancestor.id);
          if (!touchedActive.has(ancestor.id)) pluginManaged.add(ancestor.id);
          activeById.set(ancestor.id, true);
        }
      }

      // Before anything is committed or written. A value or dropdown record is
      // not finished by being switched on: Cheat Engine freezes whatever the
      // game happens to hold at that address, which is not what was asked for
      // and is indistinguishable afterwards from a cheat that did not work.
      // Apply took it, saved it into this table's startup state and reported
      // success, so the next session switched it on again the same way.
      //
      // Scoped to the records this press switches on. A record already on and
      // already without a value is a state this screen did not create, and
      // refusing until it is dealt with would block every other change the user
      // came here to make; clearing the field of one that is already on is a
      // supported ask, which drops the stored value without writing a blank
      // into a game holding a real number.
      const desiredById = new Map(Object.entries(effective).map(([id, state]) => [Number(id), state]));
      // One list, deduplicated: a record this press switches on and that is
      // also pinned is in both answers, and naming it twice reads as two cheats
      // with the same name.
      const missingValues = [...new Set([
        ...controlsMissingRequiredValue(safeControls, desiredById, touchedActive),
        // And every cheat pinned onto the panel, whether this press switched it
        // on or not. A pinned control is a switch on the quick access panel
        // with nowhere to type, so one pinned without a value can only ever be
        // switched on empty - the same thing this refuses above, reached from
        // a screen that has no field to fix it on.
        ...pinnedMissingRequiredValue(safeControls, desiredById, pinnedView),
      ])];
      const missingValueReason = missingRequiredValueReason(missingValues);
      if (missingValueReason) throw new Error(missingValueReason);

      // A script exists to create the records inside it, so one left running
      // after the last cheat that needed it went off is residue: it keeps its
      // patch in the game for nothing. Switch it off in the same Apply, and let
      // the deepest-first order release an inner script before its parent is
      // judged, so a chain of them clears in one pass.
      for (const scriptId of unusedActiveScripts(safeControls, activeById)) {
        effective[scriptId] = { active: false, value: effective[scriptId]?.value ?? null };
        effectiveTouchedActive.add(scriptId);
        if (!touchedActive.has(scriptId)) pluginManaged.add(scriptId);
        activeById.set(scriptId, false);
      }
      // An enclosing script this Apply neither switched on nor released is
      // still not a choice: it is on because something inside it is, and the
      // startup profile derives it from the table anyway. Without this, a
      // script left on by an older build stayed in the profile until the cheat
      // under it happened to be switched off.
      for (const scriptId of enclosingIds) {
        if (!touchedActive.has(scriptId)) pluginManaged.add(scriptId);
      }

      // A value whose enclosing script is still off has no address yet, so it
      // cannot be written now. That is a pre-game setting, not an error: keep it
      // in this table's own config and write it when the script is switched on.
      const deferredValues = new Set<number>();
      for (const recordId of touchedValues) {
        const control = controlById.get(recordId);
        if (control && inactiveAncestorControl(control, safeControls, activeById)) deferredValues.add(recordId);
      }

      const stagedStates = safeControls.flatMap((control) => {
        if (control.id === null) return [];
        const state = effective[control.id];
        return state
          ? [{ record_id: control.id, active: state.active, value: state.value, switch_values: switchValuesFor(control) }]
          : [];
      });
      const prospectiveRemembered = rememberedSelection(
        safeControls, stagedStates, rememberedPreferences, effectiveTouchedActive, touchedValues, pluginManaged,
      );
      const budgetError = rememberedSelectionBudgetError(startupPreferences, prospectiveRemembered);
      if (budgetError) throw new Error(budgetError);

      // The value the user typed is this table's own configuration, and it is
      // committed before anything is written to Cheat Engine. A record whose
      // address does not exist yet reads back as `??`, and a failed activation
      // reads back as whatever the game holds, so deriving the durable value
      // from the confirmed live state is what used to discard the choice.
      // Clearing the field is equally deliberate: it drops the entry.
      const nextConfigured = new Map(configuredByIdRef.current);
      let configuredChanged = false;
      for (const recordId of touchedValues) {
        const value = displayableControlValue(effective[recordId]?.value);
        const previous = nextConfigured.get(recordId) ?? null;
        if (value === null) {
          configuredChanged ||= nextConfigured.delete(recordId);
          continue;
        }
        if (value !== previous) configuredChanged = true;
        nextConfigured.set(recordId, value);
      }
      const prospectiveConfigured = [...nextConfigured]
        .map(([record_id, value]) => ({ record_id, value }))
        .sort((left, right) => left.record_id - right.record_id);
      // The local budget counts only the fields this Apply is about to write.
      // The backend budget also counts the configured values already stored and
      // every enclosing script the exact table implies, so a selection could
      // pass here, mutate the running game, and only then be refused
      // persistence. Ask the one authority before the first runtime command.
      const plan = await onValidateStartupPlan(prospectiveRemembered, prospectiveConfigured);
      if (!plan.fits) {
        throw new Error(
          `This selection expands to ${plan.action_count} startup actions for this exact table; the safe per-session limit is ${plan.limit}. Reduce the selection or its saved values before applying.`,
        );
      }
      if (configuredChanged) {
        await onSaveConfiguredValues(prospectiveConfigured);
        configuredByIdRef.current = nextConfigured;
        committedDurable = true;
      }

      if (storedOnly) {
        // With no usable session there is nothing to write to and nothing to read
        // back: the staged selection is the whole result, kept for the next
        // time this exact table is loaded.
        //
        // The parent commits it durably. Clearing the dirty flags before that
        // returned made a failed write look saved: Back then closed without the
        // unsaved-change confirmation and the choices were lost.
        setStaged(effective);
        await onApplied(prospectiveRemembered, null);
        setLastConfirmed(effective);
        setTouchedActive(new Set<number>());
        setTouchedValues(new Set<number>());
        operation.completed({ stored_only: true });
        return;
      }

      const touchedIds = new Set<number>([...effectiveTouchedActive, ...touchedValues]);
      const desired: RuntimeDesiredState[] = safeControls.flatMap((control) => {
        if (control.id === null || !touchedIds.has(control.id)) return [];
        const state = effective[control.id];
        if (!state) return [];
        const active = effectiveTouchedActive.has(control.id) ? state.active : null;
        // Emptying the field asks for the stored configuration to be dropped,
        // not for a blank to be written into the game's memory.
        const value = touchedValues.has(control.id) && !deferredValues.has(control.id)
          ? displayableControlValue(state.value)
          : null;
        if (active === null && value === null) return [];
        return [{
          record_id: control.id,
          active,
          value,
          switch_values: switchValuesFor(control),
          path: control.path,
          label: controlRowLabel(control),
        }];
      });
      // A script carries the table author's own defaults with it, so every
      // switch under it that nobody asked for is written to its off key in the
      // same call that starts it. Without this the panel counts the one cheat
      // that was asked for while the game runs everything the script declared:
      // one real table turns on 22 of its 24 flags this way.
      //
      // Exactly the scripts this press starts, which is neither more nor less
      // than the set whose defaults arrive with it. `pluginManaged` was the
      // wrong source in both directions: it carries every enclosing script in
      // the table, because the startup profile is derived from it, so flags
      // were written down under scripts that are not running - at addresses
      // those scripts had not allocated yet, which read back as nothing and
      // failed an Apply that switched nothing on, naming a flag the user had
      // never touched. And it deliberately excludes a script the user switched
      // on themselves, so doing that by hand brought the whole table's defaults
      // with it and nothing held them off. A script that was already running
      // is not here either: its defaults were dealt with when it started, and
      // writing them again would undo a flag switched on since.
      const startedHere = safeControls.filter((control) => control.id !== null
        && enclosingIds.has(control.id)
        && activeById.get(control.id) === true
        && activeBefore.get(control.id) !== true);
      const heldOff = switchesToHoldOff(startedHere, safeControls, touchedIds);
      // What the same rule refuses to write: cheats that are on in the game
      // because this table's own code was not read as surviving them being
      // switched off. Only this press knows which scripts it started, so this
      // is where that list comes from.
      leftOnRef.current = switchesLeftOn(startedHere, safeControls, touchedIds);
      for (const { control, value } of heldOff) {
        if (control.id === null) continue;
        desired.push({
          record_id: control.id,
          active: null,
          value,
          switch_values: switchValuesFor(control),
          path: control.path,
          label: controlRowLabel(control),
          held_off: true,
        });
      }

      // Any mutation/revalidation failure after this point makes the previous
      // Home snapshot stale. Successful final query below republishes a fresh one.
      //
      // Both are claims about a write, so neither is made where there is
      // nothing to write: a press that only stored a value for a script that is
      // still off touches the game not at all, and saying it did is what puts
      // "the game may have been changed" in front of somebody it was not.
      if (desired.length > 0) {
        onSnapshotInvalidated?.();
        mutatedRuntime = true;
      }
      const confirmed = await applyRuntimeSelection(appId, desired);
      if (confirmed.compatibilityMayHaveChanged) {
        await onCompatibilityConfirmed?.().catch((cause) => logUiFailure("cheats.compatibility_refresh_failed", cause, { appId }));
      }
      // A record the user deliberately switched off may no longer exist, because
      // its enclosing script created it. Re-query what is still addressable
      // rather than turning that expected outcome into a failed Apply.
      const finalState = await queryRuntimeControlsPartial(
        appId,
        safeControls.flatMap((control) => control.id === null ? [] : [control.id]),
        confirmed.envelope,
      );
      setUnavailableRecords(new Set(
        finalState.unavailable.flatMap((result) => result.record_id === null ? [] : [result.record_id]),
      ));
      // What the script's own defaults cost, read back rather than assumed: a
      // flag this did not manage to put down is a cheat running that nobody
      // asked for, and it is the one thing a later report needs to see.
      if (heldOff.length > 0) {
        const finalById = new Map(finalState.results.flatMap(
          (result) => result.record_id === null ? [] : [[result.record_id, result] as const],
        ));
        // Over the scripts that actually held something off, not over the ones
        // this press manages: a script the reader switched on themselves holds
        // its defaults off like any other and belongs in the record too.
        for (const scriptId of new Set(heldOff.map((item) => item.script))) {
          const mine = heldOff.filter((item) => item.script === scriptId);
          if (mine.length === 0) continue;
          logUi("runtime.flags_held_off", {
            session: confirmed.envelope?.prepared?.session_id ?? null,
            script_record_id: scriptId,
            written: mine.length,
            failed: mine.filter((item) => item.control.id !== null && finalById.get(item.control.id)?.value !== item.value).length,
          });
        }
      }
      // Cheat Engine answers `??` for an address it cannot read yet, including
      // every value deliberately deferred above. The typed choice is what this
      // table has to remember, so it stands in wherever the read-back is blank.
      const rememberedStates = finalState.results.map((result) => {
        if (result.record_id === null || !touchedValues.has(result.record_id)) return result;
        const typed = staged[result.record_id]?.value ?? null;
        return displayableControlValue(result.value) === null && typed !== null ? { ...result, value: typed } : result;
      });
      const remembered = rememberedSelection(
        safeControls, rememberedStates, rememberedPreferences, effectiveTouchedActive, touchedValues, pluginManaged,
      );
      const finalBudgetError = rememberedSelectionBudgetError(startupPreferences, remembered);
      if (finalBudgetError) {
        throw new Error(leadWithCause(finalBudgetError, "The runtime changes were confirmed, but the remembered-state safety budget changed before they could be saved."));
      }
      onSnapshot?.(finalState.results, finalState.envelope);
      // Cheat Engine answers `??` for a record it cannot read yet, so adopting
      // the re-read blindly would wipe the value the user just chose. Keep the
      // typed value on screen until Cheat Engine reports a real one.
      const confirmedState: Record<number, StagedState> = {};
      for (const result of finalState.results) {
        if (result.record_id === null) continue;
        const typed = touchedValues.has(result.record_id) ? staged[result.record_id]?.value ?? null : null;
        const configured = configuredByIdRef.current.get(result.record_id) ?? null;
        confirmedState[result.record_id] = {
          active: result.active,
          value: displayableControlValue(result.value) === null ? typed ?? configured : result.value,
        };
      }
      setStaged(confirmedState);
      // Cheat Engine has been mutated and verified, but the remembered state
      // and auto-load are written by the parent. Adopting the confirmed state
      // before that returned meant a failed persistence left CE changed while
      // the modal no longer knew it owed a write, so the next session restored
      // the old selection. Stay dirty until the durable half commits.
      await onApplied(remembered, finalState.envelope ?? confirmed.envelope, leftOnRef.current);
      setLastConfirmed(confirmedState);
      setTouchedActive(new Set<number>());
      setTouchedValues(new Set<number>());
      operation.completed({ stored_only: false });
    } catch (cause) {
      operation.failed(cause, { mutated_runtime: mutatedRuntime, committed_durable: committedDurable });
      setError(describeError(cause));
      // Handed up before reconciliation, which takes several round trips: the
      // table is the subject either way, and the user should not watch this
      // dialog work for seconds before being told what happened.
      if ((cause as { tableRefused?: unknown })?.tableRefused === true) {
        try {
          await onTableRefused?.(describeError(cause));
        } catch {
          // Recording this is a courtesy on top of an Apply that already
          // failed; it must never replace the failure the user is being shown.
        }
      }
      // A failed Apply is not an Apply that did nothing: `applyRuntimeSelection`
      // sends several batches and does not roll back the ones already
      // acknowledged, so the form kept showing the user's intent while the game
      // held something else - and closing with Discard then reset the form
      // without restoring the runtime. Reconcile against the exact session
      // before the user is offered that choice.
      // Applying sends several batches and does not roll back the ones already
      // acknowledged, so a failure here can leave the game holding part of an
      // intent. That is the single most consequential thing this panel does,
      // and it left no trace.
      logUiFailure("cheats.apply_failed", cause, {
        appId,
        table: inspection.sha256.slice(0, 12),
        mutated_runtime: mutatedRuntime,
        committed_durable: committedDurable,
      });
      // Reconciliation reports the runtime half; the durable half is known here
      // and stands on its own. Both are recorded, because one Apply can leave
      // both behind and a single answer described only the live game.
      if (mutatedRuntime) await reconcileLiveState();
      // The durable half is more than this modal's own configured-value write:
      // the parent commits the remembered selection and Auto-load as separate
      // mutations, so a failure of the later one arrives carrying the fact that
      // the earlier one is already stored.
      const residue = durableResidue(cause);
      if (committedDurable || residue.committed) notePartialCommit({ durableChanged: true });
      if (residue.unknown) notePartialCommit({ durableUnknown: true });
      return;
    } finally {
      applyingRef.current = false;
      setApplying(false);
    }
  };

  // What actually differs from the last confirmed state, not what was touched
  // on the way there: a cheat switched on and off again is not a change, and
  // counting it offered to discard a form that had nothing in it.
  const unsavedCount = unsavedChangeCount(staged, lastConfirmed, [...touchedActive, ...touchedValues]);
  const stagedActiveById = new Map<number, boolean | null>(
    safeControls.flatMap((control) => control.id === null ? [] : [[control.id, staged[control.id]?.active ?? null] as const]),
  );
  const blockedBy = (control: TableControl) => inactiveAncestorControl(control, safeControls, stagedActiveById);

  const requestClose = () => {
    if (applyingRef.current || pinningRef.current) return;
    // Reconciliation after a partial Apply clears the dirty sets, so counting
    // unapplied edits alone let the modal close silently over state that was
    // already committed. Anything left behind is acknowledged first.
    if (unsavedCount > 0 || partialCommit) {
      setConfirmingClose(true);
      return;
    }
    onCancel();
  };

  return (
    <ModalRoot onCancel={traceUiAction("cheat_selection_modal.request_close", requestClose)}>
      <Focusable ref={setWindowNode} style={{ minWidth: 420, maxWidth: 620 }}>
        <DensePanel>
        <PanelSection>
          <SectionHeading>Configure cheats</SectionHeading>
          <PanelSectionRow>
            {/* Steam's `verticalAlignment="center"` centres a field's children
                against its label row, not against the label plus description, so
                a control beside a two-line summary always sits level with the
                first line. The whole header is therefore laid out as this field's
                label, which keeps Steam's own row geometry and tint while the
                alignment inside the row is CE Decky's to decide. */}
            <Field bottomSeparator="thick" label={(
            <div style={headerRowStyle}>
              {/* Two lines on a screen with the height for them, and one
                  line of the same words on a handheld, where the second line
                  is a cheat. Nothing is dropped: the counts are the same
                  counts, separated the way every other summary here separates
                  them. */}
              <div style={headerTextStyle}>
                <div style={headerLabelStyle}>
                  {`${activeCount} ${storedOnly ? "selected" : "active"}${unsavedCount > 0 ? ` \u00b7 ${unsavedCount} unapplied` : ""}`}
                  {tight ? <span style={headerInlineCountStyle}>{` \u00b7 ${countsLine}`}</span> : null}
                </div>
                {tight ? null : <div style={headerDescriptionStyle}>{countsLine}</div>}
              </div>
              {/* The header row is the one part of this dialog that exists
                  before the controls do, and it has spare width on its right.
                  A spinner in a row of its own under the header pushed the
                  whole dialog down and then let it jump back up, so the wait
                  is reported in place instead. */}
              {loading && (
                <div style={headerSpinnerStyle} data-testid="cheats-loading">
                  <Spinner style={{ width: 16, height: 16 }} />
                </div>
              )}
              {/* One control, so no Focusable around it: a Focusable is itself a
                  focus target and would swallow the press. */}
              <div style={headerToggleStyle} data-testid="show-scripts">
                <span style={headerToggleLabelStyle}>Scripts</span>
                {/* Steam's gamepad toggle is a fixed 38x22 box with an absolutely
                    positioned knob, so a flex row's default shrink narrows the
                    box while the knob keeps its geometry and the switch is
                    painted clipped against the row's right edge. */}
                <div style={toggleBoxStyle}>
                  <SwitchBox name="scripts" checked={showScripts} style={CONTENTS_ONLY}>
                    <Toggle
                      value={showScripts}
                      disabled={applying}
                      onChange={traceUiAction("cheat_selection_modal.show_scripts", (checked) => { clearError(); setShowScripts(checked); setPage(0); setExpanded(null); }, (shown) => ({ shown }))}
                    />
                  </SwitchBox>
                </div>
              </div>
            </div>
            )} />
          </PanelSectionRow>
          {storedOnly && (
            <PanelSectionRow>
              {/* Nothing usable is running, so a cheat switched on here can only
                  ever reach Cheat Engine through Auto-load, and Apply switches
                  it on for exactly that reason. Saying so here is the point: the
                  first time the user heard about it used to be the toast after
                  the commit. A table too large to drive live says that instead
                  of claiming Cheat Engine is not running, which would be false
                  and would send the user looking for a session that is there.

                  On a handheld that sentence goes behind the question mark the
                  row already carries, leaving its title and that mark. It is
                  read once and then costs a cheat on every visit after it, and
                  the mark is where the rest of this screen already puts what a
                  reader may want a second time. */}
              <PanelRow
                truncate
                scroll
                testId="cheats-stored-only"
                label={liveUnavailableReason ? "Live control is unavailable for this table" : "Cheat Engine is not running"}
                description={tight ? undefined : storedOnlyLine}
                help={tight ? `${storedOnlyLine} ${storedOnlyHelp}` : storedOnlyHelp}
              />
            </PanelSectionRow>
          )}
          {partialCommit && (
            <PanelSectionRow>
              {/* Every surface this Apply reached is named, because it can have
                  reached more than one and closing undoes none of them. */}
              <Field
                label="Partly applied"
                description={[
                  partialCommit.runtimeChanged
                    ? "Some commands were already accepted before this failed, so the rows below show what Cheat Engine actually holds rather than what was staged."
                    : null,
                  partialCommit.durableChanged
                    ? "This table's saved configuration was written, so it stays stored and is used the next time this table is loaded."
                    : null,
                  partialCommit.durableUnknown
                    ? "CE Decky could not confirm whether this table's saved state was written; refresh before deciding what to do."
                    : null,
                  "Closing does not undo any of it.",
                ].filter(Boolean).join(" ")}
              />
            </PanelSectionRow>
          )}
          {!loading && (
            <>
              {/* The two controls that narrow the list, in the height of one
                  row. A row each is what Steam's own components do and it cost
                  this screen 80 of a Steam Deck's 534 pixels, which is two more
                  cheats on the page than either control is worth. */}
              <PanelSectionRow>
                <SideBySide testId="cheats-filters">
                  <DropdownItem
                    label="Section"
                    rgOptions={sections.map((section) => ({ data: section.key, label: section.label }))}
                    selectedOption={selectedSection.key}
                    onChange={traceUiAction("cheat_selection_modal.section", (option) => { clearError(); setSectionKey(String(option.data)); setSearch(""); setPage(0); setExpanded(null); }, (option) => ({ section: String(option.data) }))}
                    disabled={applying}
                  />
                  <FilterField
                    value={search}
                    onChange={traceUiEdit("cheat_selection_modal.filter", (event: any) => { clearError(); setSearch(String(event.target.value ?? "")); setPage(0); setExpanded(null); })}
                    disabled={applying}
                  />
                </SideBySide>
              </PanelSectionRow>
              {/* One compact block per cheat: name, group breadcrumb and the
                  Active toggle on a single controller-navigable row, with value
                  editing, pinning and the raw record ID behind More. Eight
                  four-row records overflowed the 800p Game Mode viewport and
                  clipped the modal header. */}
              {/* One box around the list, so the page can measure how much
                  room it actually has: what is above it is its own distance
                  from the top of the page, and none of that moves when the
                  number of rows in it does. */}
              <div ref={setListNode} style={fullPageHeight === null ? undefined : { minHeight: fullPageHeight }} data-testid="cheat-list">
              {visible.map((control) => {
                if (control.id === null) return null;
                const recordId = control.id;
                const state = staged[recordId];
                const isPinned = pinnedView.includes(recordId);
                const context = controlRowContext(control);
                // The staged value is the semantic string; the row shows a
                // display copy so invisible/bidi characters cannot reorder it.
                // A switch carries its own answer in the toggle beside it, so
                // `= 1` on the summary line is the same fact twice.
                const value = controlIsSwitch(control) ? null : presentableControlValue(state?.value);
                // An active record whose value still has to be supplied always
                // shows its editor, even before the user opens the details.
                const valueRequired = state?.active === true && controlNeedsValueInput(control);
                const isExpanded = expanded === recordId || valueRequired;
                // A record its enclosing script has not created yet is shown as
                // waiting for that script rather than as a broken control.
                const blockedByParent = !storedOnly && unavailableRecords.has(recordId);
                // A real table here declares 6508 items in one picker, and a
                // controller walks a Decky dropdown one item at a time with no
                // search and no way to jump. Past a screenful, the list is
                // narrowed by typing and only what is offered is rendered.
                const searchable = control.kind === "dropdown"
                  && !controlIsSwitch(control)
                  && control.dropdown_values.length > DROPDOWN_SEARCH_THRESHOLD;
                // A switch has no list to offer, so none is computed for it and
                // the render below has one condition rather than two.
                // A list of one is a list with nothing to choose from, and the
                // corpus holds 332 of them. What that record has is a value, so
                // it gets the field and no list - unless the author declared the
                // list read-only, because then that one entry is the only value
                // the record may take and there is no field to offer instead.
                const listed = control.kind === "dropdown" && !controlIsSwitch(control)
                  && (control.dropdown_values.length > 1 || !controlAcceptsTypedValue(control));
                const choices = listed && isExpanded
                  ? matchingDropdownValues(
                    control.dropdown_values,
                    (searchable && valueQuery?.id === recordId ? valueQuery.text : ""),
                    state?.value ?? null,
                  )
                  : null;
                // The field is drawn for a record that has no list to choose
                // from, for one the reader asked to type into, and for one
                // already holding a value the list does not offer - which is
                // what a record that allows free entry is for, and hiding it
                // would hide the reader's own answer behind a press.
                const offList = Boolean(state?.value)
                  && !control.dropdown_values.some(([value]) => value === state?.value);
                const typing = controlAcceptsTypedValue(control)
                  && (!listed || typedValue === recordId || offList);
                const summary = [
                  // The group, cut to fit, while the row is closed. An open row
                  // states the whole path at the top of its own block, so
                  // keeping the short form here as well put the same breadcrumb
                  // on the screen twice, neither copy complete.
                  isExpanded ? null : context,
                  blockedByParent ? "its script has not run yet" : null,
                  value ? `= ${value}` : null,
                  isPinned ? "pinned" : null,
                  // Said on the row, before anything is pressed: a cheat this
                  // table switches on by itself and that CE Decky may not
                  // switch off is one the reader is about to wonder about.
                  control.declared_default !== null
                    && control.declared_default === control.switch_on_value
                    && control.switch_off_is_safe !== true
                    ? "not safe to switch off" : null,
                ].filter(Boolean).join(" \u00b7 ");
                return (
                  <PanelSectionRow key={recordId}>
                    <CheatRow
                      testId={`cheat-row-${recordId}`}
                      label={controlRowLabel(control)}
                      summary={summary}
                      active={state?.active ?? null}
                      disabled={applying || pinning}
                      highlighted={isExpanded}
                      onActiveChange={traceUiAction("cheat_selection_modal.active", (checked) => touchActive(recordId, checked), (active) => ({ app_id: appId, table_sha: inspection.sha256, record_id: recordId, active }))}
                      actions={(
                        // An active cheat that takes a value keeps its editor
                        // open whatever this button does, so the button both
                        // said "More" over an already-open row and did nothing
                        // when pressed. It reports the row's real state and is
                        // disabled for exactly as long as it cannot change it -
                        // switching the cheat off releases it.
                        <DialogButton
                          style={cheatRowActionStyle}
                          disabled={applying || valueRequired}
                          onClick={traceUiAction("cheat_selection_modal.expand_record", () => { clearError(); setExpanded((current) => current === recordId ? null : recordId); }, { record_id: recordId, open: expanded !== recordId })}
                        >
                          {isExpanded ? "Less" : "More"}
                        </DialogButton>
                      )}
                      body={isExpanded ? (
                        <>
                          {/* The whole of what this record is called, first,
                              because that is what the press was for: the closed
                              row shows one line of a name a table author wrote
                              in the same field they write their notes in, and
                              the ring's own reveal travels it rather than
                              showing it. Here it wraps and nothing is cut.

                              Its own group is named with it where it has one.
                              That used to be on the `Record` field below, which
                              is where the raw ID lives, so the breadcrumb was
                              on the row's summary line and again three fields
                              down, and neither copy was the whole path. */}
                          <PanelNote>{control.path.join(" \u203a ")}</PanelNote>
                          {/* And what the author wrote about it, where that is
                              something other than the name itself. Most records
                              carry one field and this is it, so this row is
                              usually the name again and is left out. */}
                          {control.description.trim() && control.description.trim() !== controlRowLabel(control)
                            ? <PanelNote>{control.description.trim()}</PanelNote>
                            : null}
                          {searchable && listed && (
                            <TextField
                              label="Find a value"
                              value={valueQuery?.id === recordId ? valueQuery.text : ""}
                              onChange={traceUiEdit("cheat_selection_modal.find_a_value", (event: any) => setValueQuery({ id: recordId, text: String(event.target.value ?? "") }), { record_id: recordId })}
                              disabled={applying}
                            />
                          )}
                          {choices && choices.total > 0 && (
                            <DropdownItem
                              label="Value"
                              description={searchable ? describeValueChoices(choices) : undefined}
                              rgOptions={choices.options.map(([value, label]) => ({ data: value, label: label || value }))}
                              selectedOption={state?.value ?? ""}
                              onChange={traceUiAction("cheat_selection_modal.value", (option) => touchValue(recordId, String(option.data)), { record_id: recordId })}
                              disabled={applying || pinning}
                            />
                          )}
                          {control.kind === "dropdown" && control.dropdown_read_only && control.dropdown_values.length === 0 && (
                            <Field label="Value" description="This read-only dropdown declares no values, so it cannot be changed." />
                          )}
                          {/* What this costs the reader, and nothing about how
                              it works. Both halves used to explain the
                              machinery - that the enclosing script is what
                              creates this record's address - which is a
                              sentence about Cheat Engine's internals in a place
                              the user came to set a number, and which they
                              cannot act on either way. What is left is the one
                              thing that is theirs: something else is about to
                              be switched on, or this value is not live yet. */}
                          {/* A switch has no value of its own to save, so only
                              the first half of this is ever true of one: it is
                              about to switch its enclosing script on with it. */}
                          {blockedBy(control) && (controlAcceptsTypedValue(control) || (controlIsSwitch(control) && state?.active === true)) && (
                            <PanelNote>
                              {state?.active === true
                                ? `Also switches on \u201c${controlRowLabel(blockedBy(control)!)}\u201d.`
                                : `Value saved; written when \u201c${controlRowLabel(blockedBy(control)!)}\u201d is switched on.`}
                            </PanelNote>
                          )}
                          {/* One press for the field the ordinary path does not
                              need. The list is the answer for a record that has
                              one, and the value a record can only reach by
                              typing is one press away rather than a second
                              control on every row. */}
                          {controlAcceptsTypedValue(control) && listed && !typing && (
                            <SmallButton
                              disabled={applying || pinning}
                              onClick={traceUiAction("cheat_selection_modal.type_a_value", () => setTypedValue(recordId), { record_id: recordId })}
                            >Type a value instead</SmallButton>
                          )}
                          {typing && (
                            <TextField
                              label={listed ? "Custom value" : "Value"}
                              value={state?.value ?? ""}
                              onChange={traceUiEdit("cheat_selection_modal.edit_value", (event: any) => {
                                // Typing in it is asking for it, which is what
                                // keeps it open: a field shown because the
                                // record held a value its list does not offer
                                // would otherwise vanish the moment the reader
                                // cleared that value, mid-edit and with the
                                // press they need back behind where it was.
                                setTypedValue(recordId);
                                touchValue(recordId, String(event.target.value ?? ""));
                              }, { record_id: recordId })}
                              disabled={applying || pinning}
                            />
                          )}
                          <SwitchBox name="pinned" checked={isPinned} style={CONTENTS_ONLY}><ToggleField
                            label="Pinned"
                            description="Show this control directly on the CE Decky panel for this exact table."
                            checked={isPinned}
                            onChange={traceUiAction("cheat_selection_modal.pinned", (checked) => void togglePin(recordId, checked), (pinned) => ({ app_id: appId, table_sha: inspection.sha256, record_id: recordId, pinned }))}
                            disabled={applying || pinning}
                            bottomSeparator="none"
                          /></SwitchBox>
                          {/* The control's own kind was the row's heading, so
                              this line alone read as a lowercase "value" or
                              "script" among properly cased labels. It is what
                              the record *is*, which belongs beside its ID.

                              Its group is not here: it opens this block, in
                              full, rather than being repeated in part at the
                              end of it. */}
                          <Field
                            label="Record"
                            description={`${control.kind} \u00b7 ID ${recordId}`}
                            bottomSeparator="none"
                          />
                        </>
                      ) : null}
                    />
                  </PanelSectionRow>
                );
              })}
              </div>
              {matching.length === 0 && <PanelSectionRow><Field label="No controls" description="No supported controls match this section/filter." /></PanelSectionRow>}
            </>
          )}
        </PanelSection>
        </DensePanel>
        {/* Apply is at the bottom of a scrolling list, so the failure it
            reports has to be beside the button that produced it. It used to be
            said twice, here and again above the list, which on a screen counted
            in rows spent two blocks on one sentence and put one of them where
            the reader was not looking.

            `RefusalBlock` carries its own styling rather than a class from the
            panel stylesheet, because this sits outside the dense wrapper that
            stylesheet is scoped to and wrapping it in a second one would mount
            a second copy of the whole sheet for one border. */}
        {error && (
          <div style={bottomErrorStyle}>
            <RefusalBlock testId="cheat-apply-error">
              <Field label={errorTitle} description={error} bottomSeparator="none" />
            </RefusalBlock>
          </div>
        )}
        {/* The prompt Cancel opens, beside the Cancel that opens it. It used to
            be a row near the top of the list, which on any table worth paging
            through is off the screen from the footer: the press did something
            and looked as though it had done nothing, so Cancel and the
            controller's Back both read as dead. Same treatment as the failure
            above it, and for the same reason. */}
        {confirmingClose && (
          <div style={bottomErrorStyle} data-testid="unsaved-prompt">
            {/* Discard resets this form. It has never restored anything already
                written, and saying otherwise after a partial Apply is what made
                the durable and live state a surprise. */}
            <Field
              label={unsavedCount > 0
                ? `${unsavedCount} unapplied change${unsavedCount === 1 ? "" : "s"}`
                : "Some of this Apply was already committed"}
              description={partialCommit
                ? "Discard only drops the edits still on screen; what was already written to Cheat Engine or saved for this table stays as it is."
                : "Apply them to the running Cheat Engine, or discard them and close."}
              bottomSeparator="none"
            />
            <ActionGroup style={{ gap: 8, padding: "4px 0 0" }}>
              <DialogButton style={modalActionStyle} disabled={applying || pinning} onClick={traceUiAction("cheat_selection_modal.apply", () => { if (applyingRef.current || pinningRef.current) return; setConfirmingClose(false); void apply(); })}>Apply</DialogButton>
              <DialogButton style={modalActionStyle} disabled={applying || pinning} onClick={traceUiAction("cheat_selection_modal.discard", () => { if (applyingRef.current || pinningRef.current) return; setConfirmingClose(false); setStaged(lastConfirmed); setTouchedActive(new Set<number>()); setTouchedValues(new Set<number>()); onCancel(); })}>Discard</DialogButton>
              <DialogButton style={modalActionStyle} preferredFocus disabled={applying} onClick={traceUiAction("cheat_selection_modal.keep_editing", () => setConfirmingClose(false))}>Keep editing</DialogButton>
            </ActionGroup>
          </div>
        )}
        {/* The footer every paged screen here ends with: paging in the middle,
            what this screen is for against the right edge. Apply and Cancel act
            on the whole staged selection rather than on the page, and they stay
            on the same left/right controller axis as the paging controls
            because the whole row is one action group. */}
        {/* Everything below the list in one box, because that is what the page
            measures itself against. The tip sat outside the footer it is
            measured by, so its height was room the page thought it had: on a
            Steam Deck the window came out four pixels past the bottom of the
            display, which is exactly what the tip costs. */}
        <div ref={setFooterNode}>
        <PagerFooter
          testId="cheat-footer"
          style={cheatFooterStyle}
          page={safePage}
          pages={pages}
          disabled={applying}
          preferNext
          fallbackRef={cancelRef}
          onPage={(next) => { clearError(); setExpanded(null); setPage(next); }}
          trailing={(
            <>
              <SmallButton disabled={loading || applying || pinning} onClick={traceUiAction("cheat_selection_modal.apply_2", () => void apply())}>Apply</SmallButton>
              <div ref={cancelRef} style={CONTENTS_ONLY}>
                <SmallButton disabled={applying || pinning} onClick={traceUiAction("cheat_selection_modal.cancel", requestClose)}>Cancel</SmallButton>
              </div>
            </>
          )}
        />
        {/* Pinning is the feature that makes the panel worth opening, and it is
            behind a row's More, where nobody finds it by accident.

            Not on a short screen, where it is the price of a cheat. The tip is
            read once and then costs a row of the list on every visit after it,
            and a handheld has four of those to spend; a television has the room
            and keeps it. */}
        {!isShortScreen(windowNode) && (
          <div style={modalNoteStyle}>
            Tip: a cheat's <b>More</b> can pin it onto the CE Decky panel. Scripts load automatically when a cheat needs them.
          </div>
        )}
        </div>
      </Focusable>
    </ModalRoot>
  );
}

const modalNoteStyle: CSSProperties = {
  padding: "0 16px 10px",
  fontSize: 12,
  lineHeight: "16px",
  opacity: 0.7,
};

const headerRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  width: "100%",
};

const headerTextStyle: CSSProperties = {
  flex: "1 1 auto",
  minWidth: 0,
};

const headerLabelStyle: CSSProperties = {
  fontSize: 14,
  lineHeight: "17px",
};

// The counts, inline after the active total rather than under it. Set like the
// description they replace, so the row reads as one statement with a quiet half.
const headerInlineCountStyle: CSSProperties = {
  fontSize: 11,
  color: "hsla(0, 0%, 100%, 0.6)",
  whiteSpace: "nowrap",
};

const headerDescriptionStyle: CSSProperties = {
  fontSize: 11,
  lineHeight: "14px",
  marginTop: 1,
  color: "hsla(0, 0%, 100%, 0.6)",
};

const headerSpinnerStyle: CSSProperties = {
  flex: "0 0 auto",
  display: "flex",
  alignItems: "center",
};

const headerToggleStyle: CSSProperties = {
  flex: "0 0 auto",
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const headerToggleLabelStyle: CSSProperties = {
  fontSize: 12,
  opacity: 0.8,
  whiteSpace: "nowrap",
};

const bottomErrorStyle: CSSProperties = {
  padding: "0 16px 8px",
};

const cheatFooterStyle: CSSProperties = {
  padding: "var(--ce-footer-padding, 6px 16px 4px)",
};
