import { isCompatibilityFailure, manageCompatibility } from "./components/CompatibilityMark";
import { ManagedSetupOwner } from "./managedSetup";
import { traceUiAction, currentUiAction, startUiOperation, type UiActionContext } from "./uiActions";
import {
  ConfirmModal as DeckyConfirmModal,
  ErrorBoundary,
  staticClasses,
  showModal,
} from "@decky/ui";
import {
  FileSelectionType,
  definePlugin,
  openFilePicker,
  toaster,
  useQuickAccessVisible,
} from "@decky/api";
import { Component, useCallback, useEffect, useMemo, useRef, useState, type PropsWithChildren } from "react";
import {
  associateTable,
  blockTable,
  deleteTable,
  checkTableScans,
  deriveUnsignedTable,
  revokeTable,
  cancelManagedCEInstall,
  clearBlockedTables,
  clearCEImport,
  clearStartupPreference,
  completeManagedCEInstall,
  getCELaunchCapability,
  createSupportBundle,
  getDiagnosticsSnapshot,
  listGameExecutables,
  readLocalLibrary,
  listRunningAppIds,
  getManagedCECapability,
  deleteManagedData,
  getRemovalReadiness,
  getRuntimeStatus,
  getStatus,
  importCE,
  importCEArchive,
  importTable,
  inspectTableSha,
  inspectTableSource,
  launchCEForGame,
  getProviderSources,
  listBlockedTables,
  pollCELaunch,
  pollManagedCEInstall,
  repairSessionState,
  repairOwnedLaunchState,
  repairProfileState,
  resetProviderDiagnostics,
  resetProviderSources,
  runSelfTest,
  saveProfile,
  setAutoload,
  setConfiguredValues,
  validateEffectiveStartupPlan,
  setPinnedControl,
  setExecutionConsent,
  setProviderEnabled,
  setRememberedCheats,
  setMascotVisible,
  setUpdateAutoCheck,
  checkForUpdate,
  startPluginUpdate,
  pollPluginUpdate,
  cancelPluginUpdate,
  startCESelfTest,
  startManagedCEInstall,
  stopCEForGame,
  unblockTable,
} from "./api";
import {
  listInstalledGames,
  listRunningGames,
  readAppDetails,
  type AppDetailsSnapshot,
  type GameSummary,
  type RunningGamesSnapshot,
} from "./steam/client";
import { forgetSearchOutcomes } from "./providerCatalog";
import { commitSourceSelection, countsReadable, everySourceOn, sourceSwitched } from "./providerSelection";
import { canAutoImportLocalMember, forgetAllRejectedArtifacts } from "./tableImport";
import { isDeckyFilePickerCancellation } from "./deckyFilePicker";
import { MAX_LIVE_CONTROLS, RuntimeOperationError, RuntimeQueryAbortedError, applyRuntimeSelection, deactivateAllActiveControls, queryRuntimeControlsPartial, sendRuntimeCommandAndWait } from "./runtimeClient";
import { aggregateTableHolders, tableHolderIds, tableOwnerNames } from "./tableHolders";
import { PANEL_CATCH_UP_DELAY_MS, absentLiveTarget, panelUpdateOffer, antiCheatBlockedReason, blockedTableLookups, controlNeedsValueInput, controlRowLabel, switchOffValues, switchValuesFor, switchesToHoldOff, gamesOnThisDevice, providerDisplayName, refusedStartupEnable, divergentLiveTarget, enclosingControlIds, launchOwnership, inactiveAncestorControls, isExactAttachedRuntime, isExactRuntimeSession, importedTableArtifacts, isValidProcessBasename, latestRuntimeResult, localTableArtifacts, pinnedCheatRows, pinnedControlValue, rememberedSelection, rememberedSelectionBudgetError, safeActionableControls, scriptListedControlIds, selfTestSummary, unusedActiveScripts, withoutKnownLaunchers } from "./uiModel";
import { HomePanel } from "./components/HomePanel";
import { focusFirstEnabled } from "./components/PanelDensity";
import { showActionFailure } from "./modals/ActionFailureModal";
import { AdvancedModal, type AdvancedContextSnapshot } from "./modals/AdvancedModal";
import { UpdateModal } from "./modals/UpdateModal";
import { ArchiveImportModal } from "./modals/ArchiveImportModal";
import { CheatSelectionModal } from "./modals/CheatSelectionModal";
import { GamePickerModal } from "./modals/GamePickerModal";
import { ImportedTablesModal } from "./modals/ImportedTablesModal";
import { TableReviewModal } from "./modals/TableReviewModal";
import { TableSearchModal } from "./modals/TableSearchModal";
import type {
  BlockedTable,
  CELaunchCapability,
  CELaunchStatus,
  GameExecutableListing,
  GameProfile,
  ManagedCECapability,
  ManagedCEInstallStatus,
  PluginStatus,
  PluginUpdateOperation,
  RuntimeEnvelope,
  RuntimeResult,
  SelfTestResult,
  StartupPreference,
  TableInspection,
  TableScanCheck,
  TableStatus,
} from "./types";
import { causeAsLabel, describeError, leadWithCause } from "./errors";
import { notifyAuthorityChanged, subscribeAuthorityChanged } from "./panelAuthority";
import { requestPanelFocus, subscribePanelFocus, takePanelFocus } from "./panelFocus";
import { logUi, logUiFailure, logUiWarning, readSupportLog } from "./supportLog";
import { nextPanelInstance, panelRenderer, startSupportLogFlush } from "./supportFlush";
import { elapsedSince, monotonicNow, rendererStartedAt } from "./elapsed";
import { PriorDurableCommitError, commitDesiredState, configuredValuesMatch, describeCommitFailure, rememberedMatches } from "./durableWrite";
import { forgetSelectedGame, readMascotVisible, readSelectedGame, rememberMascotVisible, rememberSelectedGame } from "./selectionMemory";
import { refusedBeforeDeleting } from "./managedDeletion";

// Bounded Auto-load backoff. A game settles in seconds, not minutes: the target
// executable can appear after its launcher, and the first bridge heartbeat and
// record query can both land before Cheat Engine is ready to answer. Three
// widening retries cover that without ever becoming a respawn loop, and the
// length of this list is the attempt budget.
const AUTOLOAD_RETRY_DELAYS_MS = [4000, 10000, 25000];
// A backend that is still starting, or a websocket lost across a reload, needs
// a second or two rather than a remount of the whole panel.
const BOOTSTRAP_RETRY_DELAYS_MS = [1500, 4000, 10000];
// The loader's panel-visibility hook, when the running loader has one. It
// arrived with API version 2, an older loader connects at version 1 and leaves
// it undefined, and a plugin that calls undefined as a hook renders nothing at
// all. Resolve it once here so the panel degrades to its previous behavior
// instead of crashing, and so the hook order of a mount cannot change.
const QUICK_ACCESS_VISIBLE: (() => boolean) | null =
  typeof useQuickAccessVisible === "function" ? useQuickAccessVisible : null;
// One poll of a launch operation. The backend's own bridge deadline is five
// minutes; this only bounds a single Decky callable that never settles.
const LAUNCH_POLL_TIMEOUT_MS = 8000;

/**
 * Name the step a long activation is on, for the screen that started it.
 *
 * Activating a table is several minutes in the worst case, almost all of it
 * spent waiting for Cheat Engine to open the table and answer, and the screen
 * that started it is a modal with every control disabled for the duration.
 */
/**
 * Name the step an activation is on, and say whether it owns something that can
 * be stopped while it runs it. Review offers its stop for exactly the steps
 * that answer yes: before the first Cheat Engine is started there is nothing to
 * stop, and the durable writes that run first cannot be taken back.
 */
type StepReporter = (step: string, stoppable?: boolean) => void;
interface TableActivation {
  appId: number | null;
  finished: boolean;
  stopState: "idle" | "pending" | "confirmed" | "failed";
  stopAttempt: Promise<void> | null;
}

/** Reject after `ms` if `promise` has not settled, without leaking the timer. */
async function withTimeout<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: number | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_, reject) => { timer = window.setTimeout(() => reject(new Error(message)), ms); }),
    ]);
  } finally {
    if (timer !== undefined) window.clearTimeout(timer);
  }
}

// How long auto-load waits for the resident bridge to finish applying startup.
// The bridge resolves one startup action at a time and may legitimately spend a
// whole per-action budget on a single one: `MAX_STARTUP_WAIT_TICKS` and
// `MAX_ACTIVATION_WAIT_TICKS` in `ce_decky_bridge.lua` are both 40 ticks of
// `POLL_MS` 250 ms, so 10 s each, plus one heartbeat period before that outcome
// is published. A fixed total budget was therefore shorter than one action, and
// Auto-load reported "the saved cheats had not been applied yet" for a startup
// that went on to apply them. `tests/test_bridge_asset.py` keeps this in step
// with the exact Lua constants.
/**
 * How long the question about a refused table waits for the panel to go idle.
 *
 * It waits at all because two of the three callers raise it from inside their
 * own action, which owns the panel's latch until that action returns, and an
 * answer given while the latch is held is refused. It waits only this long
 * because a latch that never comes free would otherwise take the question with
 * it, and a question asked slightly too early is recoverable while a question
 * never asked is not: the table goes on applying and nothing says why.
 */
/** The one panel error that stops being true when the table goes away. */
const LIVE_READ_FAILURE_PREFIX = "Live cheat state could not be refreshed: ";

const REFUSAL_ASK_WAIT_MS = 4000;
// How long the answer waits for the latch it needs, having been put by a wait
// that is allowed to give up on that same latch. The question can therefore be
// asked while an operation still holds it, and the answer makes five durable
// writes that cannot run beside another operation, so it waits again rather
// than being thrown away at the press.
const REFUSAL_ANSWER_WAIT_MS = 8000;

const BRIDGE_STARTUP_ACTION_BUDGET_MS = 11_000;
const STARTUP_OUTCOME_DELAY_MS = 500;

/** The exact record the bridge reported a startup failure for, if it named one. */
function startupFailureReason(envelope: RuntimeEnvelope | null): string {
  const state = envelope?.status?.startup_state;
  // "Auto-load failed" is only an accurate account of the game's state when the
  // bridge proved it rolled its earlier actions back. When it could not, the
  // table may still be partly applied - including enclosing scripts the user
  // never sees in their own selection - so say so and name the recovery.
  const residue = state === "failed_partial" || state === "failed"
    ? " Some of it may still be applied to the game; stop Cheat Engine to clear it."
    : "";
  const failed = (envelope?.status?.results ?? []).find((result) => result.generation === 0 && !result.ok);
  if (!failed) return `Auto-load could not restore the saved cheats.${residue}`;
  const record = failed.record_id === null ? "a saved cheat" : `MemoryRecord ${failed.record_id}`;
  return `${leadWithCause(failed.error ?? "Cheat Engine did not accept the change", `Auto-load could not restore ${record}.`)}${residue}`;
}

/**
 * What a refused **Change** says, in the terms the user can act in.
 *
 * `README.md` carries the same rule under **Pick the game**, because a row
 * explaining a press nobody made costs a line of a narrow panel every session.
 */
const GAME_RUNNING_REFUSAL = "That game is running now. Everything CE Decky holds is for one game, so quit it and the press comes back.";

function currentProfile(status: PluginStatus | null, game: GameSummary | null): GameProfile | null {
  if (!status || !game) return null;
  return status.profiles.find((profile) =>
    profile.app_id === game.appId && profile.is_shortcut === game.isShortcut,
  ) ?? null;
}

function operationIsActive(operation: ManagedCEInstallStatus | null): boolean {
  return Boolean(operation && ["downloading", "extracting", "verifying"].includes(operation.state));
}

/** What one read of the durable not-working record answers with. */
type BlockedTableRead = { tables: BlockedTable[]; reason: string | null };

function Content() {
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [managedCE, setManagedCE] = useState<ManagedCECapability | null>(null);
  const [managedCEError, setManagedCEError] = useState<string | null>(null);
  const [managedInstall, setManagedInstall] = useState<ManagedCEInstallStatus | null>(null);
  // The capability carries both launcher-global ownership and facts about one
  // exact game, so the AppID it was fetched for travels with it: a failed
  // refresh after a game switch used to leave the previous game's snapshot
  // describing the new one.
  const [ceLaunch, setCELaunch] = useState<{ appId: number | null; capability: CELaunchCapability } | null>(null);
  const [ceLaunchError, setCELaunchError] = useState<string | null>(null);
  const [launchProtonToolId, setLaunchProtonToolId] = useState("");
  const [selfTest, setSelfTest] = useState<SelfTestResult | null>(null);
  const [selectedGame, setSelectedGame] = useState<GameSummary | null>(null);
  const [appDetails, setAppDetails] = useState<AppDetailsSnapshot | null>(null);
  const [inspection, setInspection] = useState<TableInspection | null>(null);
  const [targetProcess, setTargetProcess] = useState("");
  const [games, setGames] = useState<GameSummary[]>([]);
  const [runningGames, setRunningGames] = useState<RunningGamesSnapshot>({ available: false, games: [] });
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  // A launch that is still waiting for the resident bridge. It is a real owned
  // Cheat Engine the backend can stop, so Home keeps Stop available for it even
  // while the action that started it holds the global busy latch.
  const [launchInProgress, setLaunchInProgressState] = useState<{ appId: number; operationId: string } | null>(null);
  // Review is a detached modal: its handlers close over the render that opened
  // it and would read `null` here for the whole wait. The ref is what lets that
  // screen offer the same Stop that Home does.
  const launchInProgressRef = useRef<{ appId: number; operationId: string } | null>(null);
  const setLaunchInProgress = useCallback((pending: { appId: number; operationId: string } | null) => {
    launchInProgressRef.current = pending;
    setLaunchInProgressState(pending);
  }, []);
  const [runtime, setRuntime] = useState<RuntimeEnvelope | null>(null);
  const [liveSnapshot, setLiveSnapshot] = useState<{ sessionId: string; tableSha256: string; results: RuntimeResult[] } | null>(null);
  // Why the live snapshot is missing, when it is missing because the read
  // failed rather than because this table has no live controls at all.
  const [liveSnapshotError, setLiveSnapshotError] = useState<string | null>(null);
  /**
   * Drop the live snapshot, saying why when there is a reason to say.
   *
   * A snapshot dropped because the context changed is not the same as one
   * dropped because the read failed, and leaving the previous failure on screen
   * described a healthy session as broken.
   */
  const dropLiveSnapshot = useCallback((reason: string | null = null) => {
    setLiveSnapshot(null);
    setLiveSnapshotError(reason);
  }, []);
  const [pinnedBusyRecordId, setPinnedBusyRecordId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const statusRef = useRef<PluginStatus | null>(null);
  const selectedGameRef = useRef<GameSummary | null>(null);
  const busyRef = useRef(false);
  const pinnedBusyRef = useRef<number | null>(null);
  const statusGenerationRef = useRef(0);
  // The status read that is currently the authority in flight. A read that has
  // been overtaken answers with this one rather than from before it.
  const statusInFlightRef = useRef<{ generation: number; promise: Promise<PluginStatus> } | null>(null);
  const blockedGenerationRef = useRef(0);
  const gameGenerationRef = useRef(0);
  const selectionSourceRef = useRef<"auto" | "manual" | null>(null);
  const managedOwnerRef = useRef<ManagedSetupOwner | null>(null);
  const managedCancelRef = useRef(false);
  const [managedCancelling, setManagedCancelling] = useState(false);
  const managedCapabilityGenerationRef = useRef(0);
  const launchGenerationRef = useRef(0);
  const runtimeGenerationRef = useRef(0);
  const detectionBusyRef = useRef(false);
  // Whether the running-game observation is currently failing. The detector
  // runs every three seconds, so failure and recovery are each logged once
  // rather than once a tick.
  const detectionFailedRef = useRef(false);
  // Closing the panel has to stop the reads the panel started. A live refresh
  // walks every actionable record of the table in chunks, which for a large
  // table is over a hundred bridge round-trips: without this the whole sweep
  // ran to completion against a component that no longer exists, writing state
  // into a dead tree for seconds after the panel was dismissed. The picker has
  // had this since the same defect was found there; Home had not.
  const panelAborterRef = useRef<AbortController>(new AbortController());
  const contextModalDepthRef = useRef(0);
  const autoloadAttemptRef = useRef<string | null>(null);
  // A failed Auto-load is usually a startup race, not a decision: the target
  // process appears after the launcher, the bridge has not finished its first
  // heartbeat, or a record query runs while the game is still settling. The
  // attempt key alone would latch that first failure for the lifetime of this
  // mounted panel, so keep a small bounded retry beside it.
  const autoloadRetryRef = useRef<{ key: string; attempts: number } | null>(null);
  const autoloadRetryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [autoloadRetryTick, setAutoloadRetryTick] = useState(0);
  const clearAutoloadRetry = useCallback(() => {
    if (autoloadRetryTimerRef.current !== null) {
      clearTimeout(autoloadRetryTimerRef.current);
      autoloadRetryTimerRef.current = null;
    }
    autoloadRetryRef.current = null;
  }, []);
  useEffect(() => () => {
    if (autoloadRetryTimerRef.current !== null) clearTimeout(autoloadRetryTimerRef.current);
  }, []);
  const runningGamesRef = useRef<GameSummary[]>([]);
  const restoredSelectionRef = useRef(false);

  const showContextModal = useCallback((render: (close: () => void) => any): (() => void) => {
    contextModalDepthRef.current += 1;
    let closed = false;
    let handle: { Close: () => void } | null = null;
    const close = () => {
      if (closed) return;
      closed = true;
      contextModalDepthRef.current = Math.max(0, contextModalDepthRef.current - 1);
      handle?.Close();
    };
    try {
      handle = showModal(render(close));
    } catch (cause) {
      contextModalDepthRef.current = Math.max(0, contextModalDepthRef.current - 1);
      throw cause;
    }
    return close;
  }, []);

  const refreshStatus = useCallback(async (): Promise<PluginStatus> => {
    const generation = statusGenerationRef.current + 1;
    statusGenerationRef.current = generation;
    // Decky can leave several overlapping observers refreshing the same backend.
    // A slower response must never overwrite a newer one and resurrect a stale
    // Cheat Engine identity that the whole workflow keys off - and it must not
    // be handed back as this device's state either. The caller is deciding
    // something with it: which games hold a table, what a detached screen
    // repaints from. Returning the read that was just rejected made the guard
    // protect this component and nothing else.
    //
    // Nor is the last accepted snapshot a substitute, because the read that
    // overtook this one may still be in flight: answering from before it is
    // answering with a state that is already known to be out of date. A
    // superseded call waits for the read that beat it and answers with that,
    // and fails with it where it fails, because the caller asked what is true
    // now and the honest answers are the current one or none.
    //
    // What an answer from this generation is worth is decided in one place, for
    // the answer and for the failure alike. Deciding it only where the read
    // succeeded meant a read that was overtaken and then failed still reported
    // its own failure, and the caller acted on an error about a question the
    // winning read had already answered.
    const overtakenBy = (): Promise<PluginStatus> | null => {
      if (generation === statusGenerationRef.current) return null;
      const winner = statusInFlightRef.current;
      if (winner && winner.generation !== generation) return winner.promise;
      // Nothing newer is running: the generation moved for a local correction
      // this panel made itself, which is the accepted state.
      return statusRef.current ? Promise.resolve(statusRef.current) : null;
    };
    const attempt = (async (): Promise<PluginStatus> => {
      let next: PluginStatus;
      try {
        next = await getStatus(selectedGameRef.current?.appId ?? null);
      } catch (cause) {
        return await (overtakenBy() ?? Promise.reject(cause));
      }
      const winner = overtakenBy();
      if (winner) return winner;
      // Overtaken with nothing better to answer with: this read is all there is.
      if (generation !== statusGenerationRef.current) return next;
      statusRef.current = next;
      setStatus(next);
      return next;
    })();
    statusInFlightRef.current = { generation, promise: attempt };
    return attempt;
  }, []);

  /**
   * Take one table out of what this panel believes it has, without asking.
   *
   * For the one mutation that is already committed by the time the panel hears
   * about it. Reading the authority again is reconciliation, and reconciliation
   * that fails must not turn a deletion that happened into one reported as
   * failed, so the local answer is corrected first and the read follows.
   *
   * The generation moves with it, because an older read already in flight
   * carries a list from before the deletion and would otherwise land afterwards
   * and put the row back.
   */
  const forgetStoredTable = useCallback((sha256: string) => {
    statusGenerationRef.current += 1;
    const current = statusRef.current;
    if (!current) return;
    const next = { ...current, tables: current.tables.filter((table) => table.sha256 !== sha256) };
    statusRef.current = next;
    setStatus(next);
  }, []);

  const refreshManagedCE = useCallback(async (): Promise<ManagedCECapability> => {
    const generation = managedCapabilityGenerationRef.current + 1;
    managedCapabilityGenerationRef.current = generation;
    let next: ManagedCECapability;
    try {
      next = await getManagedCECapability();
    } catch (cause) {
      // Managed setup is optional: the backend keeps the controller-accessible
      // import fallback alive when it cannot offer a download. Record that this
      // one capability is unreadable rather than leaving `managedCE` null, which
      // the panel used to treat as "still loading" forever and which disabled
      // game selection, table search, Advanced and the runtime actions of an
      // otherwise healthy imported Cheat Engine until the panel was remounted.
      if (generation === managedCapabilityGenerationRef.current) setManagedCEError(describeError(cause));
      throw cause;
    }
    if (generation !== managedCapabilityGenerationRef.current) return next;
    setManagedCEError(null);
    setManagedCE(next);
    // A live monitor owns the local progress snapshot. The capability view can
    // be one poll behind it, so adopting it here would repaint progress the
    // monitor already advanced or cleared - including terminal setup state
    // after a consumed completion.
    if (!managedOwnerRef.current) setManagedInstall(next.operation);
    return next;
  }, []);

  /**
   * Refresh managed setup without letting it fail a durable mutation.
   *
   * Managed install is optional by design and its failure is already surfaced
   * separately, but it was a hard member of the refresh chains that run after
   * import, clear-CE, session repair and Advanced refresh - so a state change
   * that had already been committed could still be reported as failed, and the
   * caller's `.then(... close())` never ran.
   */
  const refreshManagedCEOptional = useCallback(
    (): Promise<unknown> => refreshManagedCE().catch(() => undefined),
    [refreshManagedCE],
  );

  const refreshCELaunch = useCallback(async (appId: number | null): Promise<CELaunchCapability> => {
    const generation = launchGenerationRef.current + 1;
    launchGenerationRef.current = generation;
    let next: CELaunchCapability;
    try {
      next = await getCELaunchCapability(appId);
    } catch (cause) {
      // Launch state that could not be read is unknown, not clear: record it so
      // ownership stays fail-closed and Home can say which fact is missing.
      if (generation === launchGenerationRef.current) {
        setCELaunch(null);
        setCELaunchError(describeError(cause));
      }
      throw cause;
    }
    if (generation !== launchGenerationRef.current) return next;
    setCELaunchError(null);
    setCELaunch({ appId, capability: next });
    setLaunchProtonToolId((current) => next.observed_proton_tool?.tool_id
      ?? (next.proton_tools.some((tool) => tool.tool_id === current) ? current : next.proton_tools[0]?.tool_id ?? ""));
    return next;
  }, []);

  const refreshRuntime = useCallback(async (appId: number): Promise<RuntimeEnvelope> => {
    const generation = runtimeGenerationRef.current + 1;
    runtimeGenerationRef.current = generation;
    const next = await getRuntimeStatus(appId);
    if (generation !== runtimeGenerationRef.current) return next;
    if (selectedGameRef.current?.appId !== appId) return next;
    setRuntime(next);
    return next;
  }, []);

  // Startup resolves one record at a time and waits for any that an enclosing
  // script has to create, so a terminal answer can be a few seconds out. Poll
  // for it rather than reading the first status after connect.
  const awaitStartupOutcome = useCallback(async (
    appId: number, connected: RuntimeEnvelope,
  ): Promise<"applied" | "pending" | "failed"> => {
    // A table can chain many actions, so the budget is per action rather than
    // for the whole startup: every action the bridge finishes is proof it is
    // working rather than stuck, and earns the next one its own budget.
    //
    // Progress comes from the bridge's own monotonic counter where it publishes
    // one. Counting generation-0 results instead stopped growing after the
    // bridge's 128-result window filled, so a large plan whose later actions
    // needed materialization or async activation looked stalled and was
    // reported pending while it was still advancing.
    const settledActions = (envelope: RuntimeEnvelope) => {
      const published = envelope.status?.startup_completed;
      if (typeof published === "number") return published;
      return (envelope.status?.results ?? []).filter((result) => result.generation === 0).length;
    };
    let observed = connected;
    let settled = settledActions(observed);
    let deadline = monotonicNow() + BRIDGE_STARTUP_ACTION_BUDGET_MS;
    for (;;) {
      const state = observed.status?.startup_state;
      // A bridge from before this contract reports nothing. Treating that as a
      // failure would break auto-load for a Cheat Engine still running from
      // before an update, so accept it as the old best-effort behaviour.
      if (state === undefined || state === null || state === "applied") return "applied";
      if (state === "failed" || state === "failed_rolled_back" || state === "failed_partial") return "failed";
      if (monotonicNow() >= deadline) return "pending";
      await new Promise((resolve) => setTimeout(resolve, STARTUP_OUTCOME_DELAY_MS));
      observed = await refreshRuntime(appId);
      const observedSettled = settledActions(observed);
      if (observedSettled > settled) {
        settled = observedSettled;
        deadline = monotonicNow() + BRIDGE_STARTUP_ACTION_BUDGET_MS;
      }
    }
  }, [refreshRuntime]);

  /**
   * Let a started Cheat Engine finish applying startup, then reconcile what it
   * proved.
   *
   * The backend persists positive compatibility only when a status read
   * observes startup in a terminal state, and startup can settle seconds after
   * the bridge answers. A manual start that stopped at the first live read
   * therefore left a genuine activation in pending proof that nothing ever
   * consumed, and where the proof did land, Search and Manage kept the snapshot
   * from before it.
   *
   * Advisory throughout: this observes and rereads, and a Cheat Engine that is
   * running with the table open stays a successful start whatever it saw.
   */
  const reconcileStartupCompatibility = useCallback(async (
    appId: number, connected: RuntimeEnvelope,
  ): Promise<void> => {
    try {
      await awaitStartupOutcome(appId, connected);
      await refreshStatus();
    } catch (cause) {
      logUiFailure("launch.compatibility_refresh_failed", cause, { app_id: appId });
    }
  }, [awaitStartupOutcome, refreshStatus]);

  /**
   * Resolve once no CE Decky operation owns the global latch.
   *
   * Signalled by `runAction` releasing it, never polled and never timed out. A
   * deadline was worse than useless here: its only effect was to hand back
   * control while the latch was still held, so the caller went on to offer an
   * action that the same latch was guaranteed to refuse - which is the failure
   * this exists to prevent. A reconciliation may legitimately take longer than
   * any bound worth picking.
   *
   * Unmounting resolves every waiter, because whatever they were going to do
   * belongs to a panel that is gone; callers check for that themselves.
   */
  const idleWaitersRef = useRef<Array<() => void>>([]);
  const releaseIdleWaiters = useCallback(() => {
    const waiting = idleWaitersRef.current;
    idleWaitersRef.current = [];
    for (const resolve of waiting) resolve();
  }, []);
  const whenIdle = useCallback(async (): Promise<void> => {
    if (!busyRef.current) return;
    await new Promise<void>((resolve) => { idleWaitersRef.current.push(resolve); });
  }, []);

  // False once this panel is gone, for work that outlives the press that started
  // it and must not act on state nobody is updating any more.
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      releaseIdleWaiters();
    };
  }, [releaseIdleWaiters]);

  // `automatic` marks work nobody pressed. It is still recorded and still
  // reaches the panel's own error row; what it must not do is put a dialog over
  // a running game, titled as something the user did, for something they did
  // not do.
  const runAction = useCallback(async <T,>(
    action: () => Promise<T>,
    options?: {
      automatic?: boolean;
      /** Captured on the originating press, before any deferred wait. */
      interaction?: UiActionContext;
      /**
       * The caller shows the failure, so nothing here does.
       *
       * For a press made inside a screen of this plugin's own: a dialog raised
       * over a modal is a window behind a window on the device, and the row the
       * press was made on is still in front of the reader, which is where the
       * message belongs. The failure is still logged and still reaches the
       * panel's error row, and the rejection still reaches the caller, which is
       * how it knows the press failed.
       *
       * Not the same as `automatic`, which is for work nobody pressed.
       */
      failureShownByCaller?: boolean;
    },
  ): Promise<T> => {
    const interaction = options?.automatic ? undefined : options?.interaction ?? currentUiAction();
    const name = interaction?.action || action.name || "action";
    if (busyRef.current) {
      // Throwing before the handler below meant a second fast press produced an
      // error that was never toasted and never caught by the caller.
      const message = "Another CE Decky operation is still running.";
      logUiWarning("panel.action_rejected", { ...interaction, action: name, reason: "busy" });
      setError(message);
      toaster.toast({ title: "CE Decky", body: message });
      throw new Error(message);
    }
    busyRef.current = true;
    setBusy(true);
    setError(null);
    const started = monotonicNow();
    logUi("panel.action_started", { ...interaction, action: name, automatic: Boolean(options?.automatic) });
    try {
      const result = await action();
      logUi("panel.action_completed", { ...interaction, action: name, duration_ms: elapsedSince(started) });
      return result;
    } catch (cause) {
      const message = describeError(cause);
      logUiFailure("panel.action_failed", cause, { ...interaction, action: name, duration_ms: elapsedSince(started) });
      setError(message);
      // A dialog rather than a Steam notification, which slides away on its own
      // timer: on a handheld the whole message was gone before it had been
      // read, and the panel row that also holds it is usually behind whatever
      // the workflow opened next. The notification stays as the fallback for a
      // dialog Steam refuses to open, because a failure nobody is told about is
      // worse than one told badly.
      if (options?.failureShownByCaller) {
        // Logged and recorded above; the caller renders it where the press was.
      } else if (options?.automatic || !showActionFailure("The last thing you pressed", cause)) {
        toaster.toast({ title: "CE Decky", body: message });
      }
      throw cause;
    } finally {
      busyRef.current = false;
      setBusy(false);
      releaseIdleWaiters();
    }
  }, [releaseIdleWaiters]);



  const bootstrap = useCallback(async (): Promise<void> => {
    const results = await Promise.allSettled([refreshStatus(), refreshManagedCE(), refreshCELaunch(null)]);
    const failed = results.find((result) => result.status === "rejected");
    if (failed?.status === "rejected") setError(describeError(failed.reason));
  }, [refreshStatus, refreshManagedCE, refreshCELaunch]);

  // Tables the user recorded as not working, by exact content. Re-read whenever
  // one is added or cleared, so a search opened afterwards greys the same rows
  // Advanced lists.
  const [blockedTables, setBlockedTables] = useState<BlockedTable[]>([]);
  // What the last accepted read of that list held, for a caller whose own read
  // was superseded by a local correction rather than by another read.
  const blockedTablesRef = useRef<BlockedTableRead>({ tables: [], reason: null });
  // The read that is currently the authority in flight, which a superseded call
  // waits for rather than answering from before it.
  const blockedInFlightRef = useRef<{ generation: number; promise: Promise<BlockedTableRead> } | null>(null);
  // A search result advertises a content digest only sometimes and a provider
  // row always, so without the second lookup the mark was invisible on exactly
  // the rows it had been recorded from and the same table was offered on every
  // search. Built by one function, because the search screen is a detached tree
  // that has to rebuild them from a fresh read rather than from new props.
  const blockedLookups = useMemo(() => blockedTableLookups(blockedTables), [blockedTables]);
  // The exact table a refusal dialog is currently open for, if any.
  const refusalDialogRef = useRef<string | null>(null);
  // Why the record is empty, when it is empty because it could not be read.
  // Without this an unreadable record and a record with nothing in it are the
  // same "No tables marked", and the protection is failing open silently.
  const [blockedTablesReason, setBlockedTablesReason] = useState<string | null>(null);
  const refreshBlockedTables = useCallback(async (): Promise<BlockedTableRead> => {
    const generation = ++blockedGenerationRef.current;
    // Superseded reads wait for the read that beat them, for the same reason
    // status does: a detached Search adopts what this returns and cannot see
    // that this component rejected it, and the snapshot from before the newer
    // read is not an answer to what is true now.
    const attempt = (async (): Promise<BlockedTableRead> => {
      // Advisory, so an unreadable record is no reason to break anything that
      // uses it: the list is simply empty and Advanced reports why.
      const listed = await listBlockedTables().catch(
        (cause) => ({ tables: [], reason: describeError(cause) }),
      );
      const read = { tables: listed?.tables ?? [], reason: listed?.reason ?? null };
      if (generation !== blockedGenerationRef.current) {
        const winner = blockedInFlightRef.current;
        if (winner && winner.generation !== generation) return winner.promise;
        return blockedTablesRef.current;
      }
      blockedTablesRef.current = read;
      setBlockedTables(read.tables);
      setBlockedTablesReason(read.reason);
      return read;
    })();
    blockedInFlightRef.current = { generation, promise: attempt };
    return attempt;
  }, []);

  // One lost `getStatus()` during mount used to leave the panel an
  // indefinitely disabled loading screen: nothing retried it, and every action
  // rendered in that state is a no-op, so the only recovery was remounting the
  // whole panel. Retry on a bounded backoff; Home also offers an explicit Retry.
  useEffect(() => {
    if (status !== null || bootstrapAttempt >= BOOTSTRAP_RETRY_DELAYS_MS.length) return;
    const timer = window.setTimeout(() => {
      setBootstrapAttempt((attempt) => attempt + 1);
      void bootstrap();
    }, BOOTSTRAP_RETRY_DELAYS_MS[bootstrapAttempt]);
    return () => window.clearTimeout(timer);
  }, [status, bootstrapAttempt, bootstrap]);

  useEffect(() => {
    // A fresh controller per mount, before anything it has to cover starts: an
    // aborted one must never outlive its panel and silence the next one's first
    // refresh, and a sweep must never start against the previous one.
    const aborter = new AbortController();
    panelAborterRef.current = aborter;
    void bootstrap();
    // Advisory and small; read once so a search opened straight away already
    // greys what Advanced would list.
    void refreshBlockedTables();
    return () => {
      launchGenerationRef.current += 1;
      runtimeGenerationRef.current += 1;
      gameGenerationRef.current += 1;
      // Stop the refresh sweeps at their next chunk boundary. A generation
      // counter only stops the result being adopted; the round-trips carried on
      // regardless, and they are the expensive part.
      aborter.abort();
    };
  }, [bootstrap, refreshBlockedTables]);

  // Whether the quick-access panel is on screen right now.
  //
  // The loader supplies this; an older loader API does not, and a hook that is
  // not there must not take the whole panel down with it. `QUICK_ACCESS_VISIBLE`
  // is decided once for the frontend's lifetime, so the hook order of any one
  // mount is still fixed.
  const quickAccessVisible = QUICK_ACCESS_VISIBLE ? QUICK_ACCESS_VISIBLE() : true;
  useEffect(() => { logUi("panel.visibility_changed", { visible: quickAccessVisible }); }, [quickAccessVisible]);
  // Whether the panel has a status snapshot at all yet. This is a dependency of
  // the reread below rather than a bare read of the ref, because a panel that
  // mounts already visible never sees a visibility transition: the loader
  // initializes the hook from the tab's current visibility, so a browser view
  // Steam recreates while the panel is open starts at `true` and stays there.
  // Without this the reread ran once against an empty status, returned, and was
  // never scheduled again, which left exactly the stale authorization the
  // reread exists to clear.
  const statusReady = status !== null;
  // Whether this panel has already scheduled its one delayed catch-up read.
  const initialCatchUpRef = useRef(false);
  useEffect(() => {
    if (!quickAccessVisible || !statusReady) return;
    // Re-read the authority every time this panel comes back into view.
    //
    // Work started by a press outlives the press, and it can outlive the panel:
    // Steam recreates this browser view when a game returns to the foreground -
    // which is exactly what stopping Cheat Engine does - so a mutation that
    // lands a moment later reports to a component nobody renders any more. The
    // panel that comes back read the profile just before that write and never
    // reads it again, because nothing here polls status. On the target that
    // left Home showing an execution authorization the user had just withdrawn,
    // with Load table & start CE enabled for it, until the plugin was reloaded.
    //
    // Every durable write still refreshes on its own path; this only closes the
    // window where the panel that receives the answer is not the panel that
    // asked. A read of the exact same authority is idempotent, and doing it
    // while the user is looking at the panel is the moment it is worth paying
    // for.
    const reread = () => {
      void refreshStatus().catch(() => undefined);
      void refreshBlockedTables().catch(() => undefined);
    };
    reread();
    // Once per panel, and only behind the first reread it performs: an ordinary
    // status update must not schedule another one, or this stops being a
    // catch-up and becomes a poll. A panel that mounts hidden gets its first
    // reread when it is first shown, and that one is worth following too, since
    // the write it is racing belongs to the panel this one replaced either way.
    if (initialCatchUpRef.current) return;
    initialCatchUpRef.current = true;
    const timer = window.setTimeout(reread, PANEL_CATCH_UP_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [quickAccessVisible, statusReady, refreshStatus, refreshBlockedTables]);

  // The exact half of the same problem. The catch-up above is a timer, and a
  // timer covers a write that lands a moment late; it cannot cover an answer
  // whose last write is five round trips after its first, made by a dialog that
  // outlived the panel entirely. Such an answer says when it has finished, and
  // whichever panel is alive then reads the authority again.
  useEffect(() => subscribeAuthorityChanged(() => {
    void (async () => {
      const next = await refreshStatus().catch(() => null);
      // A failed live read is about the live state of a selected table. Once
      // there is no selected table it cannot be true of anything, and it was
      // outliving the table it described: hydration is the only thing that
      // clears it and nothing re-hydrates a game whose table was taken away
      // under it, so the panel kept "Live cheat state could not be refreshed"
      // with no table and no session to refresh it from.
      if (next && !currentProfile(next, selectedGameRef.current)?.table_sha256) {
        setError((current) => current?.startsWith(LIVE_READ_FAILURE_PREFIX) ? null : current);
      }
    })();
    void refreshBlockedTables().catch(() => undefined);
  }), [refreshStatus, refreshBlockedTables]);

  // Where the ring goes once a table has been retired.
  //
  // Searching is the next thing a user does about a table that did not work,
  // and the answer that retires one is given from a window that has usually
  // taken this panel with it, so the request arrives either at a panel Steam is
  // about to build or at one that is in the middle of the writes the answer
  // makes. Taken at the mount for the first, and heard for the second.
  const searchButtonRef = useRef<HTMLDivElement | null>(null);
  const [preferSearchFocus, setPreferSearchFocus] = useState(false);
  // Taken in an effect rather than in the state initializer it belongs to by
  // shape. Taking one retires it, which makes it a side effect, and an
  // initializer is allowed to run more than once for a single mount: the run
  // that took the request would not be the run whose value React keeps, and the
  // one answer this panel exists to act on would be spent on nothing. The
  // effect runs before the panel below has finished reading the authority, so
  // the screen the user actually sees is still built knowing about it.
  useEffect(() => {
    if (takePanelFocus("search")) setPreferSearchFocus(true);
    return subscribePanelFocus(() => {
      if (takePanelFocus("search")) setPreferSearchFocus(true);
    });
  }, []);
  // A request this panel never acted on goes back rather than dying with it.
  // Taking one is what retires it, and a panel that was hidden the whole time
  // it held one has retired something nobody ever saw: the quick access panel
  // is exactly what an answer to this question closes, and Steam may well build
  // another before it is looked at again.
  const outstandingFocusRef = useRef(false);
  useEffect(() => { outstandingFocusRef.current = preferSearchFocus; }, [preferSearchFocus]);
  useEffect(() => () => { if (outstandingFocusRef.current) requestPanelFocus("search"); }, []);
  // Deliberately without a dependency list. Search is disabled while the panel
  // is busy and while it has no game, and both are true when the request
  // arrives: the answer is still writing, and the panel below has not finished
  // reading the authority. So this asks after every commit until the control it
  // wants can actually be pressed, and the guard makes every other one free.
  useEffect(() => {
    if (!preferSearchFocus) return;
    // A hidden panel is not somewhere a ring can be seen, and Steam settles the
    // focus of the one it shows next. Waiting costs nothing here: this runs
    // again when the panel is looked at, and the loader's own visibility is
    // what re-renders it.
    if (!quickAccessVisible) return;
    if (!focusFirstEnabled(searchButtonRef)) return;
    setPreferSearchFocus(false);
    logUi("panel.focus_moved", { control: "search" });
  });

  const clearGameContext = useCallback(() => {
    gameGenerationRef.current += 1;
    runtimeGenerationRef.current += 1;
    selectedGameRef.current = null;
    selectionSourceRef.current = null;
    forgetSelectedGame();
    setSelectedGame(null);
    setAppDetails(null);
    setInspection(null);
    setTargetProcess("");
    setRuntime(null);
    dropLiveSnapshot();
    // Ownership of Cheat Engine is launcher-global, not part of the game
    // context: dropping it here removed the Stop action and the owner's name at
    // exactly the moment observation became ambiguous, and re-enabled setup
    // actions the backend still had to reject. Re-read it at launcher scope.
    void refreshCELaunch(null).catch(() => undefined);
    autoloadAttemptRef.current = null;
    clearAutoloadRetry();
  }, [clearAutoloadRetry, refreshCELaunch]);

  const hydrateGame = useCallback(async (game: GameSummary, source: "auto" | "manual"): Promise<AdvancedContextSnapshot | null> => {
    logUi("panel.game_hydrating", { app_id: game.appId, shortcut: game.isShortcut, source });
    const generation = gameGenerationRef.current + 1;
    gameGenerationRef.current = generation;
    runtimeGenerationRef.current += 1;
    const details = await readAppDetails(game.appId);
    if (generation !== gameGenerationRef.current) return null;
    if (details.isShortcut !== game.isShortcut) {
      throw new Error(`Steam identity changed for AppID ${game.appId}; refusing to reuse the old game profile.`);
    }
    const canonical = { ...game, name: details.displayName || game.name, sortAs: details.displayName || game.sortAs };
    const profile = currentProfile(statusRef.current, canonical);
    let nextInspection: TableInspection | null = null;
    let hydrationError: string | null = null;
    if (profile?.table_sha256 && statusRef.current?.tables.some((table) => table.sha256 === profile.table_sha256 && table.available)) {
      try {
        nextInspection = await inspectTableSha(profile.table_sha256, canonical.appId);
      } catch (cause) {
        logUiFailure("panel.table_inspection_failed", cause, { app_id: canonical.appId, table_sha: profile.table_sha256.slice(0, 12) });
        hydrationError = leadWithCause(describeError(cause), "The saved table could not be inspected.");
      }
    }
    if (generation !== gameGenerationRef.current) return null;

    let nextRuntime: RuntimeEnvelope | null = null;
    let nextSnapshot: { sessionId: string; tableSha256: string; results: RuntimeResult[] } | null = null;
    try {
      nextRuntime = await getRuntimeStatus(canonical.appId);
      const exactReady = Boolean(
        nextInspection
        && profile?.table_sha256
        && isExactAttachedRuntime(nextRuntime, canonical.appId, profile.table_sha256),
      );
      const snapshotIds = nextInspection
        ? safeActionableControls(nextInspection).flatMap((control) => control.id === null ? [] : [control.id])
        : [];
      // A table beyond the live-control budget stays usable for inspection and
      // Auto-load by design, so hydration keeps its connected runtime and
      // simply has no live snapshot to show.
      if (exactReady && nextInspection && snapshotIds.length <= MAX_LIVE_CONTROLS) {
        // A child its script has not created yet is an expected per-record
        // state, not a broken session: it must not fail the whole hydration.
        const queried = await queryRuntimeControlsPartial(
          canonical.appId, snapshotIds, undefined, panelAborterRef.current.signal,
        );
        nextRuntime = queried.envelope;
        const sessionId = queried.envelope.prepared?.session_id;
        const tableSha256 = queried.envelope.prepared?.table_sha256;
        if (sessionId && tableSha256 && queried.envelope.status?.session_id === sessionId && queried.envelope.session_current) {
          nextSnapshot = { sessionId, tableSha256, results: queried.results };
        }
      }
    } catch (cause) {
      // The panel going away is not a failure to report to a user who is no
      // longer looking at it, and there is nothing left to hydrate.
      if (cause instanceof RuntimeQueryAbortedError) return null;
      // The backend can complete every RPC and the operation still fail here:
      // a session or acknowledgement identity that does not match, a bridge
      // result that says not ok, a read-back that disagrees. Those are known
      // only to the panel, so a bundle without this line shows a healthy
      // backend beside a user reporting stale cheats.
      logUiFailure("runtime.hydration_failed", cause, { appId: canonical.appId });
      hydrationError ??= `${LIVE_READ_FAILURE_PREFIX}${describeError(cause)}`;
    }
    if (generation !== gameGenerationRef.current) return null;

    selectedGameRef.current = canonical;
    selectionSourceRef.current = source;
    // Opening any modal unmounts this panel, so an explicit choice has to be
    // written down or the picker appears to do nothing at all.
    if (source === "manual") rememberSelectedGame({ appId: canonical.appId, isShortcut: canonical.isShortcut });
    setSelectedGame(canonical);
    setAppDetails(details);
    setTargetProcess(profile?.target_process ?? "");
    setInspection(nextInspection);
    setRuntime(nextRuntime);
    setLiveSnapshot(nextSnapshot);
    // Hydration surfaces its own read failure as the panel error, so the
    // snapshot note starts clean for the game that was just selected.
    setLiveSnapshotError(null);
    // Cleared on a read that worked, not only replaced on one that did not. A
    // failure from an earlier hydration outlived the session it described:
    // stopping a table left "Live cheat state could not be refreshed" on a
    // panel with no table and no session to refresh it from.
    setError(hydrationError ?? null);
    // Identity has already changed, so the previous game's launch snapshot must
    // not describe this one for even one render.
    setCELaunch((current) => current && current.appId === null ? current : null);
    const nextLaunch = await refreshCELaunch(canonical.appId).catch((cause) => {
      setError(leadWithCause(describeError(cause), "Cheat Engine launch state for this game could not be read."));
      return null;
    });
    if (statusRef.current?.table_compatibility?.entries.some((entry) => entry.app_id === canonical.appId)) {
      await refreshStatus().catch((cause) => logUiFailure("panel.compatibility_refresh_failed", cause, { app_id: canonical.appId }));
    }
    const currentStatus = statusRef.current;
    if (!currentStatus || generation !== gameGenerationRef.current) return null;
    return {
      status: currentStatus,
      ceLaunch: nextLaunch,
      runtime: nextRuntime,
      appDetails: details,
      inspection: nextInspection,
      targetProcess: profile?.target_process ?? "",
    };
  }, [refreshCELaunch]);

  const shortcutExecutableCacheRef = useRef(new Map<number, string | null>());

  const resolveShortcutExecutables = useCallback(async (games: readonly GameSummary[]): Promise<Map<number, string | null>> => {
    const cache = shortcutExecutableCacheRef.current;
    for (const game of games) {
      if (cache.has(game.appId)) continue;
      if (!game.isShortcut) {
        // Steam library apps expose no shortcut executable, and a Steam app is
        // never one of the third-party launchers this filter targets.
        cache.set(game.appId, null);
        continue;
      }
      try {
        cache.set(game.appId, (await readAppDetails(game.appId)).shortcutExe || null);
      } catch {
        // An unreadable entry stays a candidate: this filter may only remove a
        // launcher it positively identified, never a game it failed to read.
        cache.set(game.appId, null);
      }
    }
    return cache;
  }, []);

  const detectRunningGame = useCallback(async () => {
    // Detached Decky modals are intentionally bound to the game context they
    // were opened from. Do not silently swap the Home selection underneath an
    // in-progress Search/Review/Cheats/Advanced workflow; backend launch/runtime
    // checks still revalidate the actual running process state at mutation time.
    if (detectionBusyRef.current || busyRef.current || contextModalDepthRef.current > 0) return;
    detectionBusyRef.current = true;
    try {
      let snapshot = await listRunningGames(runningGamesRef.current, async () => {
        // Only reached when this Steam build exposes no running-app query. The
        // backend proposes AppIDs Steam itself declared; they are matched against
        // the Steam library here, and every launch path still resolves the
        // prefix and Proton identity independently.
        const observed = await listRunningAppIds();
        return observed.available ? observed.app_ids : null;
      });
      // A user action or modal may have started while GameSessions/library
      // enumeration was awaiting Steam. Never let that older detector pass
      // overwrite or clear the newer explicit UI context.
      if (busyRef.current || contextModalDepthRef.current > 0) return;
      // A game started through a third-party store leaves that store's own
      // library entry running beside it. Resolving the shortcut executables of
      // an ambiguous observation often leaves exactly one real game, which the
      // user then does not have to select by hand. Steam library apps have no
      // shortcut executable and are never dropped.
      //
      // Resolved before anything is published, never after. Publishing the raw
      // observation first and correcting it afterwards made the launcher a live
      // running candidate for as long as the resolution took, and Choose is not
      // held during it: a press landing in that window opened the picker on the
      // uncorrected answer, and the detector then exited on its own race guard
      // without ever publishing the corrected one.
      if (snapshot.games.length > 1) {
        const executables = await resolveShortcutExecutables(snapshot.games);
        if (busyRef.current || contextModalDepthRef.current > 0) return;
        const games = withoutKnownLaunchers(snapshot.games, (game) => executables.get(game.appId) ?? null);
        if (games.length !== snapshot.games.length) snapshot = { ...snapshot, games: [...games] };
      }
      runningGamesRef.current = snapshot.games;
      setRunningGames(snapshot);
      // A running game supersedes a game the user picked by hand while nothing
      // was running, so that pick must not be restored on any later remount.
      if (snapshot.games.length > 0) forgetSelectedGame();
      const selectedBeforeDetection = selectedGameRef.current;
      if (!selectedBeforeDetection) {
        // With no game context nothing else refreshed launcher ownership, so a
        // recovered owner that exited stayed on Home and kept blocking setup
        // until the user opened Advanced or picked a game.
        await refreshCELaunch(null).catch(() => undefined);
      }
      if (selectedBeforeDetection) {
        // Keep Home lifecycle truth fresh even when the game identity itself did
        // not change: owned CE may exit, a prepared bridge may go stale, or a
        // manually selected game may start after selection. These are backend
        // observations for the already explicit AppID, never a guessed identity.
        await refreshCELaunch(selectedBeforeDetection.appId).catch(() => undefined);
        const runtimeRefreshGeneration = runtimeGenerationRef.current + 1;
        try {
          await refreshRuntime(selectedBeforeDetection.appId);
        } catch {
          // A failed runtime refresh must not leave an old connected/attached
          // envelope driving Home controls. Do not clear a newer result written
          // by a user action that raced this background detector pass.
          if (
            runtimeGenerationRef.current === runtimeRefreshGeneration
            && !busyRef.current
            && contextModalDepthRef.current === 0
            && selectedGameRef.current?.appId === selectedBeforeDetection.appId
          ) {
            setRuntime(null);
            dropLiveSnapshot();
          }
        }
      }
      if (busyRef.current || contextModalDepthRef.current > 0) return;
      // Here, not at the end of this function. The observation has been taken
      // and the stale-action guards have passed, which is the whole of what
      // recovery means, and every ordinary outcome below returns before the
      // end: one game running, a remembered selection restored. Clearing the
      // latch down there meant that after one failure the common cases never
      // reached it, so no recovery was ever recorded and, far worse, the latch
      // stayed raised and suppressed every later failure. A latch that never
      // clears is worse than no latch at all: it turns one logged failure into
      // silence about all the rest.
      if (detectionFailedRef.current) {
        detectionFailedRef.current = false;
        logUi("games.detection_recovered");
      }
      // An observation with an AppID nothing could resolve is ambiguous however
      // few games it resolved, so it can never drive automatic selection.
      const unresolvedRunning = (snapshot.unresolvedAppIds ?? []).length > 0;
      if (snapshot.games.length === 1 && !unresolvedRunning) {
        const only = snapshot.games[0];
        const current = selectedGameRef.current;
        const sameAsRunning = Boolean(
          current && current.appId === only.appId && current.isShortcut === only.isShortcut,
        );
        // A manual choice made while its own game runs is an override and is
        // kept. A manual choice made to prepare a game that is not running is a
        // preparation screen, and it must not survive a different game actually
        // starting: Home would keep showing, searching and configuring the game
        // the user is not playing, and Auto-load would look dead for the one
        // they are, until they noticed and pressed Change.
        if (current && selectionSourceRef.current === "manual" && sameAsRunning) return;
        if (!sameAsRunning) {
          await hydrateGame(only, "auto");
        }
        return;
      }
      if (!selectedGameRef.current && snapshot.games.length === 0 && !unresolvedRunning && !restoredSelectionRef.current) {
        // Nothing is running and nothing is selected: this is the only moment a
        // remembered manual pick is still what the user meant. It is honoured
        // once per mount, and only while that exact library entry still exists.
        restoredSelectionRef.current = true;
        const remembered = readSelectedGame();
        if (remembered) {
          const installed = await loadGames();
          if (busyRef.current || contextModalDepthRef.current > 0 || selectedGameRef.current) return;
          const match = installed.find((candidate) =>
            candidate.appId === remembered.appId && candidate.isShortcut === remembered.isShortcut);
          if (match) {
            await hydrateGame(match, "manual");
            return;
          }
          forgetSelectedGame();
        }
      }
      const selected = selectedGameRef.current;
      if (selected && selectionSourceRef.current === "auto") {
        // Automatic context is authoritative only while exactly one library game is
        // observed running. If a second game starts (or observation becomes empty),
        // keeping the previous auto-selection would silently guess which game the
        // user intends to mutate.
        clearGameContext();
      }
    } catch (cause) {
      if (busyRef.current || contextModalDepthRef.current > 0) return;
      // Latched, because this runs every three seconds and a line per tick
      // would bury the ring buffer it is written into. Parts of this pipeline
      // are entirely Steam-side, so when they fail there is no backend record
      // to compensate and this is the only trace there will be.
      if (!detectionFailedRef.current) {
        detectionFailedRef.current = true;
        logUiFailure("games.detection_failed", cause, {
          hadSelection: Boolean(selectedGameRef.current),
          selectionSource: selectionSourceRef.current,
        });
      }
      runningGamesRef.current = [];
      setRunningGames({ available: false, games: [] });
      if (selectedGameRef.current && selectionSourceRef.current === "auto") {
        // Losing the observation channel invalidates an automatic selection just
        // as surely as seeing 0/2+ running games. Keep manual selections as an
        // explicit user choice, but never preserve an auto-selected AppID on a
        // stale snapshot after Steam observation itself failed.
        clearGameContext();
      } else if (!selectedGameRef.current) {
        setError(describeError(cause));
      }
    } finally {
      detectionBusyRef.current = false;
    }
  }, [clearGameContext, hydrateGame, refreshCELaunch, refreshRuntime]);

  /**
   * Whether the game this panel is on is running, asked now.
   *
   * Home reads that from the detector's snapshot, which is up to three seconds
   * old and is deliberately not refreshed while a press is in flight or a
   * context modal is open. That is right for a row that describes the state and
   * wrong for the one decision that turns on it: a game started after the last
   * poll, or between the render and the press, or while the picker was open,
   * leaves a stopped snapshot standing over a running game, and the picker is
   * open for exactly as long as the detector is suppressed.
   *
   * So this asks Steam itself, and it asks about one exact AppID rather than
   * publishing an observation: nothing here writes `runningGames`, and a pass
   * that raced this one still owns that.
   */
  const selectedGameRunningNow = useCallback(async (): Promise<GameSummary | null> => {
    const game = selectedGameRef.current;
    if (!game) return null;
    const snapshot = await listRunningGames(runningGamesRef.current, async () => {
      const observed = await listRunningAppIds();
      return observed.available ? observed.app_ids : null;
    });
    if (!snapshot.available) return null;
    const running = snapshot.games.some((candidate) => (
      candidate.appId === game.appId && candidate.isShortcut === game.isShortcut
    )) || (snapshot.unresolvedAppIds ?? []).includes(game.appId);
    return running ? game : null;
  }, []);

  /**
   * Refuse to move the game context off a game that is running.
   *
   * Everything this plugin holds is per game: the selected table, the process
   * it attaches to, the consent given for those exact bytes, and any Cheat
   * Engine it is running. Moving that context onto another game while the first
   * one is playing leaves all of it pointing somewhere else, and the disabled
   * button is presentation: it describes the last snapshot and cannot enforce
   * anything at the moment of the press.
   *
   * An observation that could not be taken is not an observation that the game
   * stopped, and it is also not a reason to strand a user with no way to change
   * games. It is recorded and the press goes ahead: this refusal protects the
   * per-game context, while the launch paths that would actually touch a
   * running game resolve identity for themselves and fail closed on their own.
   */
  const refuseWhileSelectedGameRuns = useCallback(async (stage: "open" | "commit"): Promise<void> => {
    let running: GameSummary | null = null;
    try {
      running = await selectedGameRunningNow();
    } catch (cause) {
      logUiWarning("games.change_guard_unreadable", { stage, reason: describeError(cause) });
      return;
    }
    if (!running) return;
    logUiWarning("games.change_refused_running", { stage, app_id: running.appId, shortcut: running.isShortcut });
    throw new Error(GAME_RUNNING_REFUSAL);
  }, [selectedGameRunningNow]);

  useEffect(() => {
    if (!status) return;
    void detectRunningGame();
    const timer = window.setInterval(() => void detectRunningGame(), 3000);
    return () => window.clearInterval(timer);
  }, [status?.version, detectRunningGame]);

  const profile = currentProfile(status, selectedGame);
  const activeTable = profile?.table_sha256
    ? status?.tables.find((table) => table.sha256 === profile.table_sha256 && table.available) ?? null
    : null;
  // A table can be marked as not working and still be the selected one: keeping
  // it is one of the two answers the refusal dialog offers, and a mark recorded
  // for another game reaches this one by exact bytes. Nothing said so anywhere
  // outside Search, so Home showed the row a working table has. It stays
  // usable - the record is advice, not a trust decision - but it says what it
  // is, because being offered a table that is already known not to work is the
  // whole thing the record exists to prevent.
  // Only a record about whether this table works. A statement about a download
  // or about a source belongs to the row that download came from and to the
  // full list under Advanced; on the table this game is using it would read as
  // "the table you are playing does not work", which is not what it says.
  const selectedTableMark = profile?.table_sha256 ? blockedLookups.byDigest[profile.table_sha256] : undefined;
  const selectedTableMarkedNotWorking = isCompatibilityFailure(selectedTableMark)
    ? selectedTableMark?.reason ?? null
    : null;
  const ceLaunchView = ceLaunch?.capability ?? null;
  // Facts about one exact game are only valid from a snapshot fetched for it.
  const ceLaunchGame = ceLaunch && ceLaunch.appId === selectedGame?.appId ? ceLaunch.capability.game : null;
  // Whether the game this profile is on is running, from Steam's own list of
  // running apps rather than from the launch capability's `running`, which is
  // true while any process still reports the AppID: `docs/FIELD_NOTES.md`
  // records that `wineserver`, the Proton chain and Steam's reaper all outlive
  // the game doing exactly that, so a control gated on it would stay refused
  // after the game was gone. This clears when the game does, and it is the same
  // list the game row's own "N games running" is counted from.
  const selectedGameRunning = runningGames.games.some((candidate) => (
    candidate.appId === selectedGame?.appId && candidate.isShortcut === selectedGame?.isShortcut
  ));
  // Ownership of the one Cheat Engine CE Decky may run is launcher-global, and
  // every consumer now reads it from the same place: setup identity mutations,
  // manual Start, table activation and Auto-load previously disagreed about the
  // same invariant and offered actions the backend had to reject.
  const ownership = launchOwnership({
    capability: ceLaunchView,
    readError: ceLaunchError,
    scopeAppId: ceLaunch?.appId ?? null,
    selectedAppId: selectedGame?.appId ?? null,
    // The library may not be enumerated yet, and the owner is very often one of
    // the games currently observed running, so both sources are consulted
    // before falling back to a bare AppID.
    nameOf: (appId) => games.find((candidate) => candidate.appId === appId)?.name
      ?? runningGames.games.find((candidate) => candidate.appId === appId)?.name
      ?? null,
  });
  const ceRunning = ownership.ownedBySelected;
  // The exact running-process set already positively identifies a known
  // anti-cheat launcher; that signal was consumed only to keep the launcher out
  // of target selection and then discarded, so Start and Auto-load proceeded
  // silently for a game the documented boundary says to refuse. Nothing here
  // touches the anti-cheat itself.
  const antiCheatReason = antiCheatBlockedReason(ceLaunchGame?.windows_executables ?? []);
  // What the game started instead of the one program this table is for, where
  // that absence is proven, resolved once for the three places that ask: the
  // row the reader sees, the refusal under the start press, and Auto-load.
  const targetNotRunning = useMemo(
    () => absentLiveTarget(ceLaunchGame, profile?.target_process),
    [ceLaunchGame, profile?.target_process],
  );
  // The same answer as one bit, because that is what an effect may depend on.
  // The list itself is a new array on every observation, so depending on it
  // would wake Auto-load on the detector's own cadence instead of on the thing
  // that changed - and depending on neither is what left Auto-load asleep
  // through the transition it exists for, a game starting its launcher first
  // and the target appearing seconds later with `running` true throughout.
  const targetProvenAbsent = targetNotRunning !== null;
  const ceIdentityBlockedReason = ownership.blockedReason;
  const runtimeSessionReady = Boolean(
    selectedGame
    && profile?.table_sha256
    && isExactRuntimeSession(runtime, selectedGame.appId, profile.table_sha256),
  );
  // A Cheat Engine that outlived a plugin update can still be running the
  // previous resident bridge. Ownership and Stop stay available; live control
  // does not, because current commands would reach an old protocol whose
  // compatibility is an accident rather than a contract.
  const recoveredBridgeMismatch = ceLaunchView?.recovered_bridge_mismatch ?? null;
  const runtimeReady = Boolean(
    selectedGame
    && profile?.table_sha256
    && !recoveredBridgeMismatch
    && isExactAttachedRuntime(runtime, selectedGame.appId, profile.table_sha256),
  );
  // A partially failed activation can leave the freshly inspected table in state
  // while the backend profile still points at the previous exact SHA. Home must
  // never label live results or open the cheat picker with a mismatched table, so
  // treat the inspection as usable only for the profile's exact current SHA.
  const currentInspection = inspection && inspection.sha256 === profile?.table_sha256 ? inspection : null;
  const safeControls = useMemo(() => safeActionableControls(currentInspection), [currentInspection]);
  const activeCheatSnapshotReady = Boolean(
    runtimeReady
    && liveSnapshot
    && liveSnapshot.sessionId === runtime?.prepared?.session_id
    && liveSnapshot.tableSha256 === profile?.table_sha256,
  );
  // What is on, split the way this panel presents it. A table's enclosing
  // scripts and its attach-only record are machinery rather than choices: CE
  // Decky switches them on itself and the picker lists them apart from the
  // cheats. Counting them in with the cheats made the panel say two were on
  // above a single switch that was, which is the panel disagreeing with its own
  // controls; and the labels under it named a script the user never chose.
  const { activeCheatLabels, activeScriptCount } = useMemo(() => {
    if (!activeCheatSnapshotReady || !liveSnapshot) return { activeCheatLabels: [] as string[], activeScriptCount: 0 };
    const scriptListed = scriptListedControlIds(safeControls);
    const labels: string[] = [];
    let scripts = 0;
    for (const control of safeControls) {
      if (control.id === null) continue;
      const result = latestRuntimeResult(liveSnapshot.results, control.id);
      if (!result?.ok || result.active !== true) continue;
      if (scriptListed.has(control.id)) scripts += 1;
      else labels.push(control.path.join(" › "));
    }
    return { activeCheatLabels: labels, activeScriptCount: scripts };
  }, [activeCheatSnapshotReady, liveSnapshot, safeControls]);

  const pinnedRows = useMemo(
    () => activeCheatSnapshotReady && liveSnapshot
      ? pinnedCheatRows(safeControls, profile?.pinned ?? [], liveSnapshot.results, profile?.remembered ?? [], profile?.configured_values ?? [])
      : [],
    [activeCheatSnapshotReady, liveSnapshot, safeControls, profile?.pinned, profile?.remembered, profile?.configured_values],
  );

  const recordLiveSnapshot = useCallback((results: RuntimeResult[], envelope: RuntimeEnvelope) => {
    const game = selectedGameRef.current;
    const sessionId = envelope.prepared?.session_id;
    const tableSha256 = envelope.prepared?.table_sha256;
    const selectedProfile = currentProfile(statusRef.current, game);
    if (
      !game
      || !sessionId
      || !tableSha256
      || selectedProfile?.table_sha256 !== tableSha256
      || !isExactAttachedRuntime(envelope, game.appId, tableSha256)
    ) {
      return;
    }
    setRuntime(envelope);
    setLiveSnapshot({ sessionId, tableSha256, results });
    setLiveSnapshotError(null);
  }, []);

  /**
   * Read the live state of every safe control, when that is possible at all.
   *
   * The snapshot is what powers pinned rows and Disable all; it is not what
   * makes a launch succeed. A table larger than the live-control budget is
   * explicitly supported for inspection and Auto-load, so awaiting the snapshot
   * as part of the success transaction turned a connected, attached, correctly
   * started session into a reported failure - and for Auto-load into one the
   * retry loop could not even retry, because Cheat Engine was already running.
   *
   * Never throws. Returns `null` when the table is beyond that budget or when
   * the read itself failed; either way the caller keeps its own outcome and
   * Home degrades to live controls being unavailable, saying why. Awaiting this
   * as part of the success transaction is exactly what turned a connected,
   * attached, correctly started session into a reported failure for a read-only
   * query that has no bearing on it.
   */
  const captureLiveSnapshot = useCallback(async (
    appId: number, tableInspection: TableInspection,
  ): Promise<RuntimeEnvelope | null> => {
    const recordIds = safeActionableControls(tableInspection)
      .flatMap((control) => control.id === null ? [] : [control.id]);
    if (recordIds.length > MAX_LIVE_CONTROLS) return null;
    try {
      // A record inside a script does not exist until that script has run, so
      // one unreadable record is an expected per-record state, not a broken
      // session. Requiring every record to answer meant a single unmaterialized
      // child threw away the whole snapshot: pinned controls disappeared and
      // Disable all went dead while the bridge was perfectly healthy.
      const queried = await queryRuntimeControlsPartial(
        appId, recordIds, undefined, panelAborterRef.current.signal,
      );
      recordLiveSnapshot(queried.results, queried.envelope);
      return queried.envelope;
    } catch (cause) {
      // A sweep the closing panel abandoned says nothing about the session, so
      // it must not leave "live controls unavailable" behind it either.
      if (cause instanceof RuntimeQueryAbortedError) return null;
      logUiFailure("runtime.live_snapshot_failed", cause, { appId });
      dropLiveSnapshot(describeError(cause));
      return null;
    }
  }, [recordLiveSnapshot, dropLiveSnapshot]);

  // The backend knows whether it installed this Cheat Engine itself, which it
  // answers from the installation's own location rather than by matching the
  // SHA the manifest currently pins.
  const managedReleaseInstalled = Boolean(status?.ce.valid && status.ce.managed);
  // Every Cheat Engine declares its own version in its PE resources, and that
  // is the only answer that does not depend on a label written by hand. Fall
  // back to the manifest's label only for a build declaring none.
  const ceVersion = status?.ce.version
    ?? (status?.ce.managed ? managedCE?.release?.visible_version ?? null : null);
  const ceStatusText = status?.ce.valid
    ? ceVersion
      ? `Cheat Engine ${ceVersion} · Ready`
      : "Cheat Engine · Ready"
    : status?.ce.reason ?? managedCE?.reason ?? "Cheat Engine is required before live cheats can run.";
  // A `completed` snapshot only means "setup still owns the workflow" while the
  // backend still owns that exact operation. Once completion is consumed the CE
  // identity is durably registered, so a snapshot retained by a superseded or
  // failed monitor must neither block the workflow nor keep a stale progress row
  // visible. Failed and cancelled snapshots stay visible on purpose.
  const managedInstallSnapshot = managedInstall && managedInstall.state === "completed" && !managedCE?.operation
    ? null
    : managedInstall;
  const managedSetupPending = Boolean(managedInstallSnapshot && ["downloading", "extracting", "verifying", "completed"].includes(managedInstallSnapshot.state));
  const installAvailable = Boolean(managedCE && !managedSetupPending && managedCE.managed_install_available);
  const installBusy = operationIsActive(managedInstallSnapshot);
  const tableSource = activeTable?.origins.length
    ? activeTable.origins[activeTable.origins.length - 1].provider
    : "Local";
  // A session override from Advanced -> Retry attach changes the live target
  // only. Saying "Connected" without naming that divergence let Home imply the
  // profile target and the process actually being controlled were the same.
  const liveTargetOverride = divergentLiveTarget(runtime, profile?.target_process);
  // A table larger than the live-control budget still loads, attaches and
  // auto-loads; only the live picker, snapshot and bulk operations degrade.
  const beyondLiveControlBudget = safeControls.length > MAX_LIVE_CONTROLS;
  // Cheat Engine attached to the game with an empty address list is not a table
  // CE Decky cannot support, and used to be indistinguishable from one: every
  // record answered "missing", so no cheat had a switch and none of them could
  // be pinned. The bridge now says whether it got the table in, and Home says so
  // rather than presenting a session that can do nothing as Connected.
  const tableLoadFailed = runtimeSessionReady && runtime?.status?.table_load_state === "failed";
  // The runtime line, and whether the reader can finish it where it stands.
  // Both come out of the same branch because they had drifted: a failure or
  // recovery message decided here and a reachability decided somewhere else
  // leaves text on screen that nothing can reveal.
  //
  // What decides it is who wrote the sentence. Every line this frontend writes
  // itself is known copy of a known length, and a row read in place wraps, so
  // there is nothing on it to open and no reason to rest the ring on it: the
  // panel is a column of controls, and a stop that shows nothing for being
  // reached is paid for on every pass down it. The two lines that carry a
  // message the backend wrote are the exception, because that text has no
  // bounded length and wrapping the whole of it inline would push the panel's
  // own controls down the screen. Those keep their stop, stay cut to the line,
  // and open on a press.
  const { text: runtimeText, complete: runtimeTextComplete, label: runtimeLabel } = ((): { text: string; complete: boolean; label?: string } => {
    if (recoveredBridgeMismatch && runtimeSessionReady) {
      return { text: `${recoveredBridgeMismatch}. Stop it and start it again to use live cheats.`, complete: false };
    }
    if (tableLoadFailed) {
      // Two lines of screen used to carry no finding at all: the label said
      // `Table not loaded`, which is what the row means anyway, and the cause
      // the backend named sat behind `Cheat Engine is running, but it could not
      // open this…`, where the cut fell. A short cause takes the label instead,
      // so the first thing on the row is the finding; a long one leads the
      // description, which is the same rule one line down. The row keeps its
      // cut and its stop either way, because the panel below it is a column of
      // controls and a failure may not push them down the screen.
      const cause = runtime?.status?.table_load_error ?? null;
      const promoted = causeAsLabel(cause);
      const next = "Stop Cheat Engine and start it again; if that repeats, this table cannot be used with this Cheat Engine.";
      if (promoted) return { text: next, complete: false, label: promoted };
      return {
        text: cause ? leadWithCause(cause, next) : `Cheat Engine could not open this table. ${next}`,
        complete: cause === null,
      };
    }
    if (runtimeReady) {
      // An attached session is the state Home spends almost all of its time in.
      // A process name is a value this frontend did not choose, but it is one
      // token and the row wraps it rather than cutting it, so its length is not
      // a reason to make this line a stop.
      return liveTargetOverride
        ? {
          text: `Connected · ${liveTargetOverride} · this session only; ${profile?.target_process} is still saved`,
          complete: true,
        }
        : { text: `Connected · ${runtime?.status?.target_process ?? targetProcess}`, complete: true };
    }
    if (runtimeSessionReady) {
      return { text: "Cheat Engine is running, but it has not attached to the game process yet.", complete: true };
    }
    if (runtime?.connected) {
      return {
        text: "Cheat Engine is running for a different game or table; select this table again to reconnect.",
        complete: true,
      };
    }
    if (runtime?.prepared) {
      return { text: "Cheat Engine is not running for this table. Details are under Advanced.", complete: true };
    }
    return activeTable
      ? { text: "Table selected; Cheat Engine is not connected.", complete: true }
      : { text: "Select a table first.", complete: true };
  })();
  // A selected table survives a restart, so Home owns the second-run entry point
  // into the runtime. Say exactly what is missing instead of only disabling it.
  const startRuntimeBlockedReason: string | null = (() => {
    if (runtimeReady) return "Cheat Engine is already connected for this exact table.";
    if (profile?.table_sha256 && !activeTable) {
      return "The selected table file is missing. Download or open it again to re-import it.";
    }
    if (!selectedGame) return "Choose the game this table belongs to first.";
    if (!status?.ce.valid) return status?.ce.reason ?? "Install or import Cheat Engine first.";
    if (!profile?.table_sha256) return "Select a table for this game first.";
    if (profile.execution_consent_sha256 !== profile.table_sha256) {
      return "This exact table is not authorized yet. Open it once from Manage and confirm it.";
    }
    if (!profile.target_process) return "The exact game .exe is not confirmed yet. Open the table once to review it.";
    if (antiCheatReason) return antiCheatReason;
    if (ownership.blockedReason) return ownership.blockedReason;
    if (ceLaunchGame?.app_id !== selectedGame.appId || !ceLaunchGame.running) {
      return ceLaunchGame?.reason ?? "Start the game first; Cheat Engine attaches to the running game.";
    }
    // A game that is up and is not running the one program this table is for.
    // Home already said so in its own row and told the reader to repair it
    // before starting, and the press underneath stayed live: the attach then
    // waits for a process that is not there, which on this panel is
    // indistinguishable from Cheat Engine failing. `absentLiveTarget` answers
    // only where the absence is proven, so an observation that could not see
    // everything still starts.
    if (targetNotRunning) {
      return `${profile.target_process} is not running in this game. This game is running ${targetNotRunning.join(", ")}. Set the target under Advanced first.`;
    }
    if (ceRunning) return "Cheat Engine is already running for this game; stop it before starting a new session.";
    return null;
  })();

  /**
   * Publish one launch operation into the launcher snapshot immediately.
   *
   * A launch that reaches `starting` and then waits for the resident bridge is
   * a real owned Cheat Engine, and the backend can already stop it safely. Home
   * only adopted the operation once it reached `connected`, so for up to five
   * minutes the panel had no ownership snapshot from the action it had just
   * performed - and therefore no Stop.
   */
  const setCELaunchOperation = useCallback((operation: CELaunchStatus) => {
    setCELaunch((current) => {
      if (!current) return current;
      const base = current.capability;
      return {
        ...current,
        capability: {
          ...base,
          operations: [
            ...base.operations.filter((candidate) => candidate.operation_id !== operation.operation_id),
            operation,
          ],
        },
      };
    });
  }, []);

  const awaitLaunchOutcome = async (started: CELaunchStatus, waitForStop = false): Promise<CELaunchStatus> => {
    let operation = started;
    const deadline = monotonicNow() + 330_000;
    const pending = () => ["starting", "running"].includes(operation.state)
      || (waitForStop && operation.state === "connected");
    while (pending() && monotonicNow() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 700));
      try {
        // One poll that never settles used to hold the global busy latch open
        // for the whole panel lifetime, because the deadline below could not
        // advance while it was being awaited. A lost poll is not a lost launch,
        // so bound each one and let the outer deadline stay the real limit.
        operation = await withTimeout(
          pollCELaunch(operation.operation_id),
          LAUNCH_POLL_TIMEOUT_MS,
          "Cheat Engine launch status did not answer in time.",
        );
        setCELaunchOperation(operation);
      } catch {
        // Keep the last known operation and try again until the deadline.
      }
    }
    return operation;
  };

  const runManagedSetup = useCallback(async (force: boolean) => {
    if (managedOwnerRef.current) return;
    const owner = new ManagedSetupOwner({
      // The owner may abandon a read for Cancel. Only its accepted observation
      // may publish operation state, never a late callable's side effect.
      capability: getManagedCECapability,
      status: refreshStatus,
      start: startManagedCEInstall,
      poll: pollManagedCEInstall,
      cancel: cancelManagedCEInstall,
      complete: completeManagedCEInstall,
      publish: (operation) => {
        setManagedInstall(operation);
        setManagedCE((current) => current ? { ...current, operation } : current);
      },
      signal: panelAborterRef.current.signal,
      completed: (receipt, cancelled) => {
        void refreshCELaunch(selectedGameRef.current?.appId ?? null).catch(() => undefined);
        if (receipt.completed_now) toaster.toast({
          title: "CE Decky",
          body: cancelled
            ? "Cancellation arrived after promotion; Cheat Engine installation was verified."
            : "Cheat Engine installation was verified.",
        });
      },
    });
    // Reserve ownership before the first await, including start receipt recovery.
    managedOwnerRef.current = owner;
    try {
      await owner.run(force, managedCE ?? await refreshManagedCE());
    } finally {
      if (managedOwnerRef.current === owner) managedOwnerRef.current = null;
    }
  }, [managedCE, refreshCELaunch, refreshManagedCE, refreshStatus]);

  useEffect(() => {
    const operation = managedCE?.operation;
    if (
      !operation
      || !["downloading", "extracting", "verifying", "completed"].includes(operation.state)
      || busyRef.current
      || managedOwnerRef.current
    ) return;
    void runAction(() => runManagedSetup(false), { automatic: true }).catch(() => undefined);
  }, [managedCE?.operation?.operation_id, managedCE?.operation?.state, runAction, runManagedSetup]);

  const chooseManagedSetup = (force: boolean) => {
    const capability = managedCE;
    if (!capability) return;
    if (managedSetupPending) return;
    if (force) {
      const confirm = showModal(
        <DeckyConfirmModal
          strTitle="Reinstall Cheat Engine"
          strDescription="CE Decky will download and extract the reviewed Cheat Engine release again. The current installation keeps working until the replacement is verified, and setup can be cancelled at any time."
          strOKButtonText="Reinstall"
          strCancelButtonText="Cancel"
          onOK={traceUiAction("panel.reinstall.confirm", () => { confirm.Close(); startManagedSetup(capability, true); })}
          onCancel={traceUiAction("panel.reinstall.cancel", () => confirm.Close())}
        />,
      );
      return;
    }
    startManagedSetup(capability, force);
  };

  /** Explain a setup that could not obtain Cheat Engine by any route.
   *
   * Both download routes failing is the one setup outcome the user cannot act
   * on from the panel itself, and a one-line error under the button does not
   * carry the way out. Import is a real completion path, so it is named here
   * with the steps it actually needs.
   */
  const showManagedSetupFailure = (detail: string) => {
    // The body is paragraphs rather than newlines: this renders as HTML, where
    // a "\n\n" would collapse and run the cause straight into the way out.
    const dialog = showModal(
      <DeckyConfirmModal
        bAlertDialog
        strTitle="Cheat Engine could not be downloaded"
        strDescription={(
          <div data-testid="managed-setup-failure">
            <p>{detail}</p>
            <p>Nothing was installed and your existing setup is unchanged.</p>
            <p>
              You can finish setup yourself. Install Cheat Engine on a Windows machine
              from cheatengine.org, then bring its installation folder to this device -
              copy the folder across, or pack it into a .zip. Import it from
              Advanced, under Registered Cheat Engine. Everything after that works
              exactly as a downloaded Cheat Engine does.
            </p>
          </div>
        )}
        strOKButtonText="Close"
        onOK={traceUiAction("panel.setup_failure.close", () => dialog.Close())}
        onCancel={traceUiAction("panel.setup_failure.cancel", () => dialog.Close())}
      />,
    );
  };

  const startManagedSetup = (capability: ManagedCECapability, force: boolean) => {
    if (!capability.managed_install_available) {
      setError(capability.reason || "Managed Cheat Engine extraction is unavailable.");
      return;
    }
    // The dialog belongs to setup failing, not to the panel refusing a press.
    // Catching inside the action makes that structural: a press refused because
    // another operation is running never reaches this body at all.
    void runAction(async () => {
      try {
        await runManagedSetup(force);
      } catch (cause) {
        showManagedSetupFailure(describeError(cause));
        throw cause;
      }
    }, { failureShownByCaller: true }).catch(() => undefined);
  };

  const cancelManagedSetup = async () => {
    const owner = managedOwnerRef.current;
    const operation = owner?.operation;
    if (!owner || !operation || !operationIsActive(operation) || managedCancelRef.current) return;
    managedCancelRef.current = true;
    setManagedCancelling(true);
    const cancellation = startUiOperation("managed_setup.cancel", { operation_id: operation.operation_id });
    try {
      const state = await owner.cancel();
      if (state === "cancelled") toaster.toast({ title: "CE Decky", body: "Cheat Engine setup cancelled." });
      cancellation.completed({ state });
    } catch (cause) {
      cancellation.failed(cause);
      logUiFailure("managed_setup.cancel_failed", cause, { operation_id: operation.operation_id });
      setError(describeError(cause));
    } finally {
      managedCancelRef.current = false;
      setManagedCancelling(false);
    }
  };

  const requestOwnedCEStop = async (appId: number, tableSha256?: string): Promise<boolean> => {
    const result = await (tableSha256 ? stopCEForGame(appId, tableSha256) : stopCEForGame(appId));
    logUi("panel.stop_requested", { app_id: appId, stopped: result.stopped, recovered: result.recovered });
    if (result.recovered && !result.stopped) {
      throw new Error("CE Decky could not prove that the owned Cheat Engine process stopped; the session was left unchanged.");
    }
    dropLiveSnapshot();
    return result.stopped;
  };

  const stopOwnedCE = async (appId: number, tableSha256?: string): Promise<void> => {
    await requestOwnedCEStop(appId, tableSha256);
    await refreshCELaunch(appId).catch(() => undefined);
    await refreshRuntime(appId).catch(() => undefined);
  };

  const ensureAttachedRuntime = async (game: GameSummary, process: string, report: StepReporter = () => undefined): Promise<RuntimeEnvelope> => {
    report("Starting Cheat Engine");
    const capability = await refreshCELaunch(game.appId);
    if (!capability.ce_ready) throw new Error(capability.reason || "Cheat Engine is not ready.");
    if (!capability.game?.running) throw new Error(capability.game?.reason || "The selected game is not running.");
    // The backend launch transaction prepares the session itself, so preparing
    // one here only wrote a second exact-SHA table snapshot that Cheat Engine
    // never opened; both were retained.
    logUi("panel.launch_requested", {
      app_id: game.appId, process,
      proton: capability.observed_proton_tool?.tool_id ?? null,
      game_running: capability.game?.running ?? false,
      windows_executables: (capability.game?.windows_executables ?? []).join(","),
    });
    const started = await launchCEForGame(game.appId, capability.observed_proton_tool?.tool_id ?? null);
    // Adopt the owned operation before waiting on it, so Home can show and stop
    // a launch that is still waiting for the bridge.
    setCELaunchOperation(started);
    setLaunchInProgress({ appId: game.appId, operationId: started.operation_id });
    // The launch is adopted, so from here there is an exact owned operation to
    // stop, and it stays stoppable through attach: what is running is the same
    // Cheat Engine either way.
    report("Waiting for Cheat Engine to load the table and answer", true);
    let operation: CELaunchStatus;
    try {
      operation = await awaitLaunchOutcome(started);
    } finally {
      setLaunchInProgress(null);
    }
    if (operation.state !== "connected") {
      logUiWarning("panel.launch_not_connected", {
        app_id: game.appId, state: operation.state,
        message: operation.message, error: operation.error,
      });
      throw new Error(operation.error || operation.message || "Cheat Engine did not connect to the game session.");
    }
    logUi("panel.launch_connected", { app_id: game.appId, session: operation.session_id.slice(0, 12) });
    setCELaunchOperation(operation);
    void refreshCELaunch(game.appId).catch(() => undefined);
    let observed = await refreshRuntime(game.appId);
    if (!observed.connected || observed.prepared?.session_id !== operation.session_id || observed.status?.session_id !== operation.session_id) {
      throw new Error("Cheat Engine started, but the exact fresh bridge session was not confirmed.");
    }
    if (!observed.status?.attached) {
      const exact = (observed.status?.processes ?? []).filter(([, name]) => name.toLowerCase() === process.toLowerCase());
      if (exact.length === 1) {
        report(`Attaching to ${process}`, true);
        const [pid, name] = exact[0];
        const attached = await sendRuntimeCommandAndWait(game.appId, { kind: "retry_attach", value: name, target_pid: pid });
        observed = attached.envelope;
        setRuntime(observed);
      }
    }
    // Attached is not loaded. A Cheat Engine that could not open the table
    // attaches to the game anyway and reports itself perfectly healthy, with an
    // address list that answers "missing" for every record: nothing to switch
    // on, nothing to pin, no live value to read. Every caller here is asking
    // for a runtime it can use, and all three of them used to say "Table
    // loaded" for this one, leaving Home's own row as the only account of it.
    if (observed.status?.table_load_state === "failed") {
      logUiWarning("panel.table_not_loaded", {
        app_id: game.appId, route: observed.status?.table_load_route ?? null,
        error: observed.status?.table_load_error ?? null,
      });
      throw new Error(
        observed.status?.table_load_error
        || "Cheat Engine started and attached to the game, but it could not open this table.",
      );
    }
    return observed;
  };

  const ensureProfileAssociation = async (sha256: string): Promise<void> => {
    const game = selectedGameRef.current;
    if (!game) throw new Error("No game is selected.");
    let profile = currentProfile(await refreshStatus(), game);
    if (!profile) {
      // Exact desired state, so a lost reply is asked about rather than
      // reported as a failed import of a table the backend has already stored.
      await commitDesiredState({
        subject: "the profile for this game",
        write: () => saveProfile(game.appId, appDetails?.displayName || game.name, game.isShortcut, null, null),
        verify: async () => Boolean(currentProfile(await refreshStatus(), game)),
      });
      // Ask the authority again rather than reusing what this screen last saw.
      // `commitDesiredState` returns the moment its write succeeds and re-reads
      // only when the reply was lost, so on the ordinary path nothing had
      // refreshed the status since before this profile existed: the cached one
      // still held no profile for the game, and a first table that had just
      // been downloaded, verified and stored ended in "could not create the
      // game profile" for a profile that was on disk. Nothing was associated
      // and nothing was offered for review, so the only way back to those bytes
      // was to download them again. A game that already had a profile never
      // entered this branch, which is why every later import worked.
      profile = currentProfile(await refreshStatus(), game);
    }
    if (!profile) throw new Error("Could not create the game profile for the imported table.");
    await commitDesiredState({
      subject: "this table's place in the game's library",
      write: () => associateTable(game.appId, sha256),
      verify: async () => Boolean(currentProfile(await refreshStatus(), game)?.table_library?.includes(sha256)),
    });
    await refreshStatus();
  };

  const activateTable = async (table: TableStatus, nextInspection: TableInspection, process: string, activation: TableActivation, report: StepReporter = () => undefined) => {
    const game = selectedGameRef.current;
    if (!game) throw new Error("No game is selected.");
    if (!isValidProcessBasename(process)) throw new Error("Target process must be an unambiguous .exe basename.");
    // Stopping the Cheat Engine an activation is using ends the activation. The
    // live read that follows a connection swallows a failed query on purpose,
    // so without this the run that the user just stopped went on to report a
    // loaded table and a connected Cheat Engine that are no longer there.
    const stopRequested = async () => {
      // An attempted stop owns this activation until its actual RPC settles.
      // A failed attempt allows progress; confirmed stopping prevents success.
      await activation.stopAttempt?.catch(() => undefined);
      if (activation.stopState === "confirmed") {
        throw new Error("Stopped at your request: Cheat Engine was stopped, so the table was not activated.");
      }
    };
    try {
      const beforeStatus = statusRef.current ?? await refreshStatus();
      const beforeProfile = currentProfile(beforeStatus, game);
      const changingIdentity = beforeProfile?.table_sha256 !== table.sha256 || beforeProfile?.target_process !== process;
      let live = await refreshCELaunch(game.appId);
      const beforeRuntime = await refreshRuntime(game.appId).catch(() => null);
      const beforeRuntimeReady = Boolean(
        beforeProfile?.table_sha256
        && isExactAttachedRuntime(beforeRuntime, game.appId, beforeProfile.table_sha256),
      );
      const hadOwnedCE = Boolean(live && (
        live.recovered?.app_id === game.appId
        || live.operations.some((operation) => operation.app_id === game.appId && ["starting", "running", "connected"].includes(operation.state))
      ));
      if (hadOwnedCE && (changingIdentity || !beforeRuntimeReady)) {
        report("Stopping the Cheat Engine that is already running");
        await stopOwnedCE(game.appId);
      }
      report("Saving the selected table");
      // Two independent durable mutations, reconciled one at a time. A
      // rejected Decky reply is not proof the write did not land, and this
      // sequence has already stopped the previous owned Cheat Engine by the
      // time it runs: reporting a committed activation as a failure left the
      // game without its table and the panel describing state it did not have.
      // The separate association the selection used to make is gone with it -
      // saving the profile already puts the selected digest in the table
      // library, so it only added a second boundary that could fail.
      await commitDesiredState({
        subject: "the selected table and its target process",
        write: () => saveProfile(game.appId, appDetails?.displayName || game.name, game.isShortcut, table.sha256, process),
        verify: async () => {
          const saved = currentProfile(await refreshStatus(), game);
          return saved?.table_sha256 === table.sha256 && saved?.target_process === process;
        },
      });
      try {
        report("Recording the authorization for this exact table");
        await commitDesiredState({
          subject: "the authorization to run this table",
          write: () => setExecutionConsent(game.appId, table.sha256, true),
          verify: async () => currentProfile(await refreshStatus(), game)?.execution_consent_sha256 === table.sha256,
        });
      } catch (cause) {
        throw new PriorDurableCommitError(cause, "The selected table");
      }
      setTargetProcess(process);
      setInspection(nextInspection);
      const nextStatus = await refreshStatus();
      const nextProfile = currentProfile(nextStatus, game);
      if (!nextProfile || nextProfile.table_sha256 !== table.sha256 || nextProfile.execution_consent_sha256 !== table.sha256) {
        throw new Error("Backend did not confirm the selected exact-SHA table and authorization.");
      }
      live = await refreshCELaunch(game.appId);
      const currentRuntime = await refreshRuntime(game.appId).catch(() => null);
      const existingReady = Boolean(
        !changingIdentity
        && hadOwnedCE
        && isExactAttachedRuntime(currentRuntime, game.appId, table.sha256),
      );
      if (statusRef.current?.ce.valid && live.game?.running && !existingReady) {
        const observed = await ensureAttachedRuntime(game, process, report);
        await stopRequested();
        if (observed.connected && observed.status?.attached) {
          report("Waiting for the saved cheats to be applied", true);
          await reconcileStartupCompatibility(game.appId, observed);
          await stopRequested();
          report("Reading the table's live values", true);
          await captureLiveSnapshot(game.appId, nextInspection);
          await stopRequested();
          toaster.toast({ title: "CE Decky", body: hadOwnedCE ? "Table switched; the game kept running." : "Table loaded and Cheat Engine connected." });
        } else {
          dropLiveSnapshot();
          toaster.toast({ title: "CE Decky", body: "Table loaded. Cheat Engine connected, but the target process still needs an exact PID selection in Advanced." });
        }
      } else if (existingReady) {
        // The owned Cheat Engine this reads through is already running, so this
        // step owns something the user can stop as much as a launch does.
        report("Reading the table's live values", true);
        await captureLiveSnapshot(game.appId, nextInspection);
        await stopRequested();
      }
      await stopRequested();
      autoloadAttemptRef.current = null;
      clearAutoloadRetry();
    } catch (cause) {
      // The activation transaction spans several independently durable backend
      // mutations (profile, association, consent, launch/session). If a later
      // step fails, immediately reconcile Home with the backend before Review
      // can be dismissed; otherwise the next visible panel could keep showing
      // the pre-activation table/profile until another unrelated refresh.
      dropLiveSnapshot();
      await Promise.allSettled([
        refreshStatus(),
        refreshCELaunch(game.appId),
        refreshRuntime(game.appId),
      ]);
      throw cause;
    }
  };

  interface PreparedReview {
    table: TableStatus;
    inspection: TableInspection;
    /** What the game's own program holds of what this table scans for, where that is knowable. */
    scanCheck: TableScanCheck | null;
    observedProcesses: string[];
    launchExecutable: string | null;
    /** What this game's own installed folder holds, for a table that names nothing. */
    installedExecutables: GameExecutableListing | null;
    initialTargetProcess: string | null;
  }

  const prepareReview = async (sha256: string): Promise<PreparedReview> => {
    const nextStatus = await refreshStatus();
    const table = nextStatus.tables.find((candidate) => candidate.sha256 === sha256 && candidate.available);
    if (!table) throw new Error("The imported exact table SHA is no longer available.");
    const nextInspection = await inspectTableSha(sha256, selectedGameRef.current?.appId ?? null);
    // Before the screen opens, because it is one of the things the screen is
    // asking the reader to decide on. Best effort in every direction: a game
    // this device has never launched has no program to look in, and the answer
    // then says so and Review is the screen it always was.
    const scanCheck = await checkTableScans(sha256, selectedGameRef.current?.appId ?? null).catch((cause) => {
      logUiFailure("panel.scan_check_failed", cause, { table: sha256.slice(0, 12) });
      return null;
    });
    const existing = currentProfile(nextStatus, selectedGameRef.current);
    // Review is a detached modal, so the running-process observation has to be
    // taken here: it cannot arrive from Home after the modal is open. A failed
    // or empty observation only means the manual entry stays the way forward.
    const game = selectedGameRef.current;
    const observed = game
      ? await refreshCELaunch(game.appId)
        .then((capability) => capability.game)
        .catch(() => null)
      : null;
    const observedProcesses = observed?.windows_executables ?? [];
    // Read for a Steam game whichever way the observation went, because the
    // observation is about what is running and this is about what is
    // installed: a game that is running has already answered, and one that is
    // not is the case this exists for. Never for a non-Steam shortcut, which
    // has no Steam manifest and answers with its own recorded target instead.
    // Best effort: Review opens without it, as it always did.
    const installedExecutables = game && !game.isShortcut
      ? await listGameExecutables(game.appId).catch((cause) => {
        logUiFailure("panel.game_files_unreadable", cause, { app_id: game.appId });
        return null;
      })
      : null;
    return {
      table,
      inspection: nextInspection,
      scanCheck,
      observedProcesses,
      installedExecutables,
      // Steam records a non-Steam shortcut's target in AppDetails, but a Steam
      // library entry's launch executable is only observable in the running
      // game's own process table.
      launchExecutable: observed?.launch_executable
        ?? (game?.isShortcut ? appDetails?.shortcutExe ?? null : null),
      initialTargetProcess: existing?.target_process ?? null,
    };
  };

  /**
   * The copy of a signed table this Cheat Engine will open, ready to review.
   *
   * Cheat Engine refuses a signed table by returning false and saying nothing,
   * so the copy is what the user actually needs and the press that makes it is
   * offered where they meet the fact. The copy is a table of its own - a new
   * digest, its own inspection, no origin, because no provider served these
   * bytes - so it is associated and reviewed exactly like an import, and the
   * consent is given on its own Review rather than on the press that made it.
   */
  const prepareUnsignedCopy = async (sha256: string): Promise<PreparedReview> => {
    const derived = await deriveUnsignedTable(sha256);
    await ensureProfileAssociation(derived.sha256);
    return prepareReview(derived.sha256);
  };

  const showPreparedReview = ({ table, inspection: nextInspection, scanCheck, observedProcesses, launchExecutable, installedExecutables, initialTargetProcess }: PreparedReview) => {
    let currentActivation: TableActivation | null = null;
    logUi("panel.modal_opened", {
      modal: "table_review", table_sha: table.sha256.slice(0, 12),
      controls: nextInspection.controls.length, entries: nextInspection.total_entries,
      unsupported: nextInspection.unsupported_record_id_count,
      target: initialTargetProcess, observed: observedProcesses.join(","),
      // How much the screen had to work with, which is what a report about a
      // table that could not be authorized turns on.
      installed: installedExecutables?.executables.length ?? 0,
      installed_reason: installedExecutables?.reason ?? null,
      // Which of the two answers the screen is working from, and why the
      // stronger one was not available. A review that offered a folder full of
      // candidates is only answerable from a support archive with this on it.
      installed_source: installedExecutables?.source ?? null,
      declared_reason: installedExecutables?.declared_reason ?? null,
    });
    showContextModal((close) => (
      <TableReviewModal
        table={table}
        inspection={nextInspection}
        scanCheck={scanCheck}
        observedProcesses={observedProcesses}
        launchExecutable={launchExecutable}
        installedExecutables={installedExecutables}
        initialTargetProcess={initialTargetProcess}
        onRefreshProcesses={async () => {
          const game = selectedGameRef.current;
          if (!game) return [];
          return (await refreshCELaunch(game.appId)).game?.windows_executables ?? [];
        }}
        onUse={async (process, report) => {
          await runAction(async () => {
            const activation: TableActivation = { appId: selectedGameRef.current?.appId ?? null, finished: false, stopState: "idle", stopAttempt: null };
            currentActivation = activation;
            try {
              await activateTable(table, nextInspection, process, activation, report);
            } finally {
              // Even a separate activation failure cannot release the panel
              // while its uncancelled Stop callable can still mutate the game.
              await activation.stopAttempt?.catch(() => undefined);
              activation.finished = true;
            }
          });
          close();
        }}
        onPrepareCopy={async () => {
          const review = await runAction(async function prepareUnsignedTableCopy() {
            return prepareUnsignedCopy(table.sha256);
          }, { failureShownByCaller: true });
          // Serial handoff, the way Search and Manage hand over to Review: the
          // screen being replaced closes first, and the copy's own Review opens
          // over the panel rather than over a window on its way out.
          close();
          showPreparedReview(review);
        }}
        onAbort={async () => {
          const activation = currentActivation;
          if (!activation || activation.finished || activation.appId === null) return "There is nothing to stop; this activation is no longer running.";
          if (activation.stopState !== "pending" && activation.stopState !== "confirmed") {
            activation.stopState = "pending";
            activation.stopAttempt = Promise.resolve().then(async () => {
              try {
                const stopped = await requestOwnedCEStop(activation.appId!);
                activation.stopState = stopped ? "confirmed" : "failed";
              } catch (cause) {
                activation.stopState = "failed";
                throw cause;
              }
            });
          }
          await activation.stopAttempt;
          return activation.stopState === "confirmed" ? null : "There is nothing to stop; no owned Cheat Engine was stopped.";
        }}
        onCancel={close}
      />
    ));
  };

  const localArtifactMap = useMemo(() => localTableArtifacts(status?.tables ?? []), [status]);
  const importedArtifactMap = useMemo(() => importedTableArtifacts(status?.tables ?? []), [status]);
  // Every table this game imported, still present and verified. These stay in
  // Search even when no provider answers, and remain the source of the separate
  // imported-table picker as well.
  const importedTables = useMemo(() => {
    if (!status || !profile) return [];
    const library = new Set(profile.table_library);
    return status.tables.filter((table) => library.has(table.sha256) && table.available);
  }, [status, profile]);
  /**
   * Every other verified table on this device, which this game has no
   * association with.
   *
   * Reaching a stored table went entirely through the game's own library, and
   * the association is precisely what two supported recoveries throw away.
   * Deleting the plugin's setup state keeps `tables/` and removes `state/`;
   * repairing a corrupt profile store replaces it with an empty one. Both say
   * the imported tables are kept and can be chosen again, and afterwards there
   * was no way in Game Mode to name them: the library was empty, so Home did
   * not offer Imported, Search was handed no local tables and needs a provider
   * row to map to the bytes, and Local file needs the original file, which is
   * the thing the user may no longer have. Offline that is the whole of it.
   *
   * So the list of what is on the device is derived from the tables themselves.
   * Nothing here is chosen for the user and nothing guesses which game a table
   * used to belong to: choosing one is a press that associates it with the game
   * selected now and opens the ordinary review, where the exact SHA is
   * authorized again.
   */
  const storedTables = useMemo(() => {
    if (!status) return [];
    const library = new Set(profile?.table_library ?? []);
    return status.tables.filter((table) => !library.has(table.sha256));
  }, [status, profile]);

  // Every verified table on the device, for the two screens that answer about
  // exact bytes rather than about this game: the picker behind Stored, and the
  // digest lookup a provider row is resolved through. It is never the set Search
  // offers rows of its own from, because a standalone row there is an offer to
  // use that table for the game being searched for, and a table stored for some
  // other game is not that.
  /**
   * Which game has each stored table selected, by exact SHA.
   *
   * The backend refuses to remove a table any game is on, so the press that
   * would be refused is not offered, and the row says which game is holding it
   * rather than leaving the reader to work out why nothing happened. Read from
   * every profile rather than from the selected game alone, because the table
   * another game is on is exactly the one this screen lists as unassociated.
   */
  const tableHolders = useMemo(() => aggregateTableHolders(status?.profiles ?? []), [status]);

  const searchableTables = useMemo(
    () => [...importedTables, ...storedTables.filter((table) => table.available)], [importedTables, storedTables],
  );

  const clearFailedMark = async (sha256?: string) => {
    try {
      await commitDesiredState({
        subject: "clearing the not-working mark",
        write: () => sha256 === undefined ? clearBlockedTables() : unblockTable(sha256),
        verify: async () => {
          const listed = await refreshBlockedTables();
          if (listed.reason) throw new Error(listed.reason);
          return sha256 === undefined ? listed.tables.length === 0
            : !listed.tables.some((entry) => entry.sha256 === sha256 || entry.key === sha256);
        },
      });
    } finally {
      await refreshBlockedTables();
    }
  };

  const openTableSearch = () => {
    const game = selectedGameRef.current;
    if (!status || !game) return;
    logUi("panel.modal_opened", { modal: "table_search", app_id: game.appId });
    showContextModal((close) => (
      <TableSearchModal
        gameIdentity={`${game.appId}:${game.isShortcut ? "shortcut" : "steam"}`}
        gameName={appDetails?.displayName || game.name}
        appId={game.appId}
        shortcutExecutable={game.isShortcut ? appDetails?.shortcutExe ?? null : null}
        compatibility={status?.table_compatibility?.entries ?? []}
        artifactResolutions={status?.artifact_resolutions?.entries ?? []}
        onRefreshProvenance={async () => {
          const next = await refreshStatus();
          return { resolutions: next.artifact_resolutions?.entries ?? [], compatibility: next.table_compatibility?.entries ?? [] };
        }}
        localArtifacts={localArtifactMap}
        localTables={importedTables}
        deviceTables={searchableTables}
        importedArtifacts={importedArtifactMap}
        blockedTables={blockedLookups.byDigest}
        blockedArtifacts={blockedLookups.byArtifact}
        onRefreshBlocked={async () => blockedTableLookups((await refreshBlockedTables()).tables)}
        onClearMarks={(sha256s) => runAction(async () => {
          // Scoped to the tables on this screen. Advanced still clears the
          // whole record; from here, clearing every other game's marks to
          // retry one table would be a much larger thing than it looks.
          //
          // The re-read happens whatever any one of these did, so a failure
          // part way through leaves the panel showing what is actually
          // recorded rather than the marks it had before the press.
          try {
            for (const sha256 of sha256s) await clearFailedMark(sha256);
            logUi("panel.marks_cleared", { count: sha256s.length });
          } finally {
            await refreshBlockedTables().catch(() => undefined);
          }
        })}
        onSelected={async (sha256) => {
          const review = await runAction(async () => {
            await ensureProfileAssociation(sha256);
            return prepareReview(sha256);
          }, { failureShownByCaller: true });
          // Avoid stacking Review above Search and then closing the lower modal;
          // Decky's modal/focus stack is more stable when the handoff is serial.
          close();
          showPreparedReview(review);
        }}
        onCancel={close}
      />
    ));
  };

  const openImportedTables = () => {
    const game = selectedGameRef.current;
    if (!status) return;
    logUi("panel.modal_opened", {
      modal: "imported_tables", app_id: game?.appId,
      tables: importedTables.length, stored: storedTables.length,
    });
    showContextModal((close) => (
      <ImportedTablesModal
        compatibility={manageCompatibility(status.table_compatibility?.entries ?? [], game?.appId ?? null)}
        tables={status.tables.filter((table) => profile?.table_library.includes(table.sha256))}
        otherTables={storedTables}
        owners={tableOwnerNames(status.profiles, game?.appId ?? null)}
        canSelect={Boolean(game)}
        activeSha256={currentProfile(statusRef.current, game)?.table_sha256 ?? null}
        activeAuthorized={Boolean(
          currentProfile(statusRef.current, game)?.execution_consent_sha256
          && currentProfile(statusRef.current, game)?.execution_consent_sha256
            === currentProfile(statusRef.current, game)?.table_sha256,
        )}
        blockedReasons={blockedLookups.byDigest}
        onOpenLocalFile={openLocalTable}
        selectedBy={tableHolders}
        holderIds={tableHolderIds(status.profiles)}
        onRefreshHolders={async () => {
          const next = await refreshStatus();
          return { holders: aggregateTableHolders(next.profiles), holderIds: tableHolderIds(next.profiles), activeSha256: currentProfile(next, game)?.table_sha256 ?? null };
        }}
        onRevoke={async (sha256, confirmedHolderIds) => {
          await runAction(async () => {
            const latest = await refreshStatus();
            const holders = latest.profiles.filter((holder) => holder.table_sha256 === sha256);
            if (holders.some((holder) => !confirmedHolderIds.includes(holder.app_id))) {
              throw new Error("The holder games changed. Review the refreshed list and confirm again.");
            }
            let failure: unknown = null;
            for (const holder of holders) {
              try {
                const current = (await refreshStatus()).profiles.find((item) => item.app_id === holder.app_id);
                if (current?.table_sha256 !== sha256) continue;
                await revokeProfileTable(holder.app_id, sha256);
              } catch (cause) {
                failure ??= cause;
              }
            }
            if (failure) throw failure;
          }, { failureShownByCaller: true });
        }}
        onDelete={async (sha256) => {
          await runAction(async function deleteStoredTable() {
            await deleteTable(sha256);
          }, { failureShownByCaller: true });
          // Committed, and the answer is durable the moment the panel's own
          // list agrees with it. What follows is reconciliation and is not
          // awaited: a read that fails cannot make a deletion that happened
          // into one reported as failed, and a read that is merely slow cannot
          // hold the row on screen, keep the window's latch, or block the way
          // out, all for a table that is already gone. The generation moved
          // with the local answer, so a read in flight cannot put it back.
          forgetStoredTable(sha256);
          void refreshStatus().catch((cause) => {
            logUiFailure("panel.status_after_delete_failed", cause, { table: sha256.slice(0, 12) });
          });
        }}
        onPrepareCopy={async (sha256) => {
          const review = await runAction(async function prepareUnsignedTableCopyFromManage() {
            return prepareUnsignedCopy(sha256);
          }, { failureShownByCaller: true });
          close();
          showPreparedReview(review);
        }}
        onSelect={async (sha256) => {
          // The same contract as the press beside it. This screen is a modal,
          // a window raised over it can appear behind it, and the row the press
          // was made on is still in front of the reader: the rejection reaches
          // the window, which says so on the row rather than over it. Swallowed
          // here, a table whose file has gone or whose association could not be
          // written produced exactly the failure Delete was changed to avoid.
          const review = await runAction(async function useStoredTable() {
            await ensureProfileAssociation(sha256);
            return prepareReview(sha256);
          }, { failureShownByCaller: true });
          close();
          showPreparedReview(review);
        }}
        onClose={close}
      />
    ));
  };

  const importPickedTable = async (source: string, memberPath?: string | null, password?: string | null): Promise<PreparedReview | null> => {
    const appId = selectedGameRef.current?.appId ?? null;
    const table = await importTable(source, memberPath ?? null, password ?? null, appId);
    // With no game chosen the file still joins this device's library, which is
    // what the backend has always allowed: the AppID it takes is optional and
    // it uses one only to name a table in the not-working record. What cannot
    // happen yet is the rest - a table is associated with a game and authorized
    // for one, and there is no game to do either for. So it is imported, said
    // to have been, and waits in Manage for a game to be chosen and Use pressed,
    // which is the same path a table already on the device takes.
    if (appId === null) {
      // The import is committed by the line above, so what follows is
      // reconciliation and is not awaited. Awaited, a status read that failed
      // for a moment reported the whole action as a failed import, for a table
      // that is on the device - and the reader's next move is to import it
      // again. The same commit boundary Delete draws, for the same reason.
      void refreshStatus().catch((cause) => {
        logUiFailure("panel.status_after_local_import_failed", cause, { table: table.sha256.slice(0, 12) });
      });
      toaster.toast({ title: "CE Decky", body: `${table.filename} is on this device. Choose a game to use it.` });
      return null;
    }
    await ensureProfileAssociation(table.sha256);
    return prepareReview(table.sha256);
  };

  const openLocalTable = () => {
    if (!status) return;
    // Decky's native file picker is itself a detached modal, but unlike our own
    // showContextModal() wrapper we cannot observe its close lifecycle. Hold the
    // automatic running-game detector for the whole picker/import handoff so a
    // second game starting mid-pick cannot silently retarget the import/profile.
    contextModalDepthRef.current += 1;
    let contextReleased = false;
    const releaseContext = () => {
      if (contextReleased) return;
      contextReleased = true;
      contextModalDepthRef.current = Math.max(0, contextModalDepthRef.current - 1);
    };
    void runAction(async () => {
      let selected;
      try {
        // `includeFolders` must be true even when only a file may be chosen:
        // with it false the picker lists the starting directory's matching files
        // and nothing else, so there is no way to navigate anywhere. The last
        // argument is Decky's page size, not a selection limit - passing 1 there
        // renders exactly one entry per directory however many it holds - so it
        // is left at the default.
        selected = await openFilePicker(
          FileSelectionType.FILE,
          status.user_home,
          true,
          true,
          undefined,
          ["ct", "CT", "zip", "7z", "7zip", "rar"],
          true,
          false,
        );
      } catch (cause) {
        if (isDeckyFilePickerCancellation(cause)) { logUi("table.file_picker_cancelled"); return; }
        throw cause;
      }
      const source = selected.realpath || selected.path;
      logUi("table.file_picked", { app_id: selectedGameRef.current?.appId, archive: !source.toLowerCase().endsWith(".ct") });
      // A direct table has no member to choose. Send it through import itself
      // so invalid exact bytes reach the durable not-working record together
      // with the selected game; inspecting them first rejected the file before
      // the only route that can record that outcome ever saw it.
      if (source.toLowerCase().endsWith(".ct")) {
        return importPickedTable(source);
      }
      const sourceInspection = await inspectTableSource(source);
      if (sourceInspection.members.length === 0) {
        return importPickedTable(source);
      }
      if (canAutoImportLocalMember(sourceInspection.members)) {
        return importPickedTable(source, sourceInspection.members[0].path);
      }
      showContextModal((close) => (
        <ArchiveImportModal
          members={sourceInspection.members}
          onImport={async (memberPath, password) => {
            const review = await runAction(() => importPickedTable(source, memberPath, password));
            close();
            if (review) showPreparedReview(review);
          }}
          onCancel={close}
        />
      ));
      return null;
    }).finally(releaseContext).then((review) => {
      if (review) showPreparedReview(review);
    }).catch(() => undefined);
  };

  /**
   * Cheat Engine ran a cheat from this table and it came straight back off.
   *
   * A table finds the game's code by scanning for byte patterns, so this is
   * what a table written for a different build of the game does - every time,
   * for as long as that build is installed. The record is kept against the
   * exact bytes so the same file is not found, downloaded and reviewed again,
   * and the user is asked once whether to stop using it here.
   *
   * Nothing durable is written until that question is answered. Marking first
   * and asking afterwards meant the answer could not undo what had already
   * happened without a second write, and every way of dismissing the dialog -
   * the controller's own Back button included - had to be treated as one of the
   * two answers. Asking first makes dismissal mean exactly what it looks like.
   *
   * The whole thing is best effort on top of an Apply that already failed: the
   * user is being shown that failure either way, and nothing here may replace
   * it with an error about bookkeeping.
   */
  const recordRefusedTable = async (game: GameSummary, tableSha256: string, reason: string) => {
    toaster.toast({ title: "CE Decky", body: "This table did not work." });
    // Apply can be pressed again while this is up, and every press refuses the
    // same way. One dialog for one table is the whole message.
    //
    // The guard is taken when the dialog opens, not when it is decided to open
    // one. It used to be taken here, several awaits before `showModal`, and
    // anything that stopped the wait in between - the panel closing, the idle
    // latch not coming free - left the table marked as asked about with nothing
    // ever having been asked. Every later Apply then returned at this line, so
    // a table that had refused every cheat could be applied for as long as the
    // user cared to press, and the one question that would have retired it was
    // never put again.
    if (refusalDialogRef.current === tableSha256) return;
    let held = false;
    const dismiss = () => {
      if (held && refusalDialogRef.current === tableSha256) refusalDialogRef.current = null;
      held = false;
    };
    // Detached, and only once the global latch is free. Two callers reach this
    // from inside their own `runAction`, which owns that latch until after the
    // catch that called this returns - so awaiting the dialog here would
    // deadlock, and opening it there let the user answer before the latch was
    // released. That answer was rejected as "another operation is still
    // running" with the dialog already closed: nothing happened, and nothing
    // said so.
    // Every branch of this says so. The question is the only thing that gets a
    // table out of the way, and when it does not appear the user is left with a
    // notification, a table that still applies, and nothing anywhere saying
    // which of the several ways to not-ask was taken. It cost two rounds on the
    // device to find that out once; it should cost a log line next time.
    const table = tableSha256.slice(0, 12);
    logUi("panel.table_refusal_asking", { table });
    void (async () => {
      // Bounded, because a latch that is never released must not swallow the
      // question. Two callers reach this from inside their own `runAction`,
      // which owns the latch until after the catch that called this returns, so
      // asking immediately let the user answer before it was released and the
      // answer was rejected as "another operation is still running" with the
      // dialog already closed. Waiting for that is right; waiting for it
      // forever is how the question is lost.
      if (busyRef.current) {
        const waited = await Promise.race([
          whenIdle().then(() => "idle" as const),
          new Promise<"timeout">((resolve) => { window.setTimeout(() => resolve("timeout"), REFUSAL_ASK_WAIT_MS); }),
        ]);
        if (waited === "timeout") logUiWarning("panel.table_refusal_wait_timed_out", { table });
      }
      // Asked whether this panel is still on screen or not.
      //
      // It used to be dropped when the panel had gone, on the ground that its
      // handlers act on refs nobody updates any more. That reasoning was about
      // a wait that outlived the press; it stopped being true when the answer
      // became a window this panel opens after closing another one, because
      // closing that one is itself a way for the panel to go. The question then
      // fell into the gap it had just made: the notification arrived, the
      // window did not, and the table went on applying.
      //
      // The window is Steam's, not this panel's, so it outlives the panel
      // perfectly well. Nothing in it reads live state either: the answer
      // re-checks the selected game against the one it was asked about and
      // refuses if they differ, which is the same check it always made and the
      // only one that matters here.
      if (!mountedRef.current) logUiWarning("panel.table_refusal_asked_after_panel_closed", { table });
      // Another Apply may have got here first while this one was waiting.
      if (refusalDialogRef.current === tableSha256) {
        logUi("panel.table_refusal_already_open", { table });
        return;
      }
      refusalDialogRef.current = tableSha256;
      held = true;
      // A window that could not be opened must not leave the table looking as
      // though it had been asked about: the guard comes straight back off.
      try {
        openRefusalDialog();
        logUi("panel.table_refusal_asked", { table });
      } catch (cause) {
        dismiss();
        logUiFailure("panel.table_refusal_dialog_failed", cause, { table });
      }
    })();

    function openRefusalDialog(): void {
      // Keeping the table writes nothing at all, which is also what the
      // controller's Back button does: Steam routes gamepad cancel to
      // `onCancel`, so that handler is reached both by the button and by a
      // reflex press to clear the screen, and neither may leave durable state
      // behind. Everything durable happens on the explicit "Stop using it".
      const confirm = showModal(
      <DeckyConfirmModal
        strTitle="This table did not work"
        strDescription={`${reason} Stop using it for this game? These exact bytes are then marked as not working: the copy stays on this device and search still shows it, but nothing can be set to use it again until you clear the mark, and a source may still offer another version. Keeping it changes nothing.`}
        strOKButtonText="Stop using it"
        strCancelButtonText="Keep it"
        onCancel={traceUiAction("panel.table_refusal.keep", () => { dismiss(); confirm.Close(); })}
        onOK={traceUiAction("panel.table_refusal.stop", () => {
          const interaction = currentUiAction();
          dismiss();
          confirm.Close();
          void answerWhenIdle();

          /**
           * The answer, once the latch it needs is actually free.
           *
           * The question is put by a wait that is allowed to give up on that
           * latch, because a latch that never comes free must not swallow it.
           * That leaves this press arriving while an operation still holds it,
           * and `runAction` refuses on the spot: the dialog had already closed
           * itself, so the user's answer was accepted-looking and then dropped
           * with "Another CE Decky operation is still running" over a table
           * that went on applying. Waiting here is the same reasoning as the
           * wait that put the question, one step later.
           *
           * If it never comes free the answer is still not silently lost. It
           * says the withdrawal could not be made and that the table is still
           * in use, which is a thing the user can act on, rather than a toast
           * about an operation they did not start.
           */
          async function answerWhenIdle(): Promise<void> {
            if (busyRef.current) {
              await Promise.race([
                whenIdle(),
                new Promise<void>((resolve) => { window.setTimeout(resolve, REFUSAL_ANSWER_WAIT_MS); }),
              ]);
            }
            // Re-read rather than trusting the race: the panel unmounting
            // releases every waiter without the operation having finished, and
            // another press may have taken the latch in the gap.
            if (busyRef.current) {
              const message = `${game.name} is still busy with the last thing you pressed, so this table was not stopped. Try Stop using it again in a moment.`;
              logUiWarning("panel.table_refusal_answer_blocked", { table });
              setError(message);
              toaster.toast({ title: "CE Decky", body: message });
              return;
            }
            try {
              await runAction(async () => {
                // Whatever this got through, it says so on the way out. Every
                // step below is a durable write, the panel that asked is very
                // likely gone by now, and a partial answer changes the
                // authority exactly as much as a whole one does.
                try {
                  await answerStopUsing();
                } finally {
                  notifyAuthorityChanged();
                }
              }, { interaction });
            } catch {
              // `runAction` has already reported it, in a dialog where it can.
              return;
            }
            // The user has just said this table does not work, so the next
            // thing they are going to do is look for one that does. The panel
            // they say it from is usually gone by now - the question closes
            // Configure cheats, which takes the quick access panel with it - so
            // this is a request the panel takes when it comes back rather than
            // a control being focused here.
            //
            // Only when the answer went through. A failure puts a dialog of its
            // own over the panel, and moving the ring underneath that is moving
            // it somewhere the user cannot see it.
            logUi("panel.focus_requested", { control: "search", table });
            requestPanelFocus("search");
          }

          async function answerStopUsing(): Promise<void> {
            // This dialog outlives the press that opened it, and withdrawing an
            // authorization always acts on whatever game is selected now. A
            // game switched underneath it would have had the wrong table
            // revoked, so the answer only counts for the game it was asked
            // about.
            const selected = selectedGameRef.current;
            // A profile is keyed by AppID *and* by whether the entry is a
            // shortcut, and the backend treats a write that disagrees about the
            // second one as a different game: it resets the profile, dropping
            // the table library and every table's archived preferences with it.
            // Everything below reads the profile through `selected` and writes
            // it through `game`, so the two identities have to be the same one
            // before any of that runs.
            if (!selected || selected.appId !== game.appId || selected.isShortcut !== game.isShortcut) {
              throw new Error(`${game.name} is no longer the selected game, so nothing was changed. Select it again to stop using this table.`);
            }
            // The mark is written here, on the answer, rather than before the
            // question. A failure to record it is reported and never stops the
            // authorization from being withdrawn: the user asked to stop using
            // this table, and that has to happen whatever the bookkeeping does.
            let marked: boolean | null = false;
            // What the backend said when it could not record it. The advisory
            // list is bounded and refuses rather than dropping somebody else's
            // decision, and the way to make room is a press on a screen the user
            // can reach: throwing that sentence away left them with a statement
            // that something failed and nothing they could do about it.
            let markFailure: string | null = null;
            try {
              await blockTable(tableSha256, reason, game.appId);
              // Recorded from here on, whatever the re-read of the list does:
              // reporting a write that landed as one that did not is the
              // protection failing closed while the user is told it failed.
              marked = true;
              await refreshBlockedTables();
            } catch (cause) {
              logUiFailure("panel.refusal_mark_failed", cause, { table_sha: tableSha256.slice(0, 12) });
              markFailure = describeError(cause).slice(0, 240);
              const listed = await refreshBlockedTables();
              marked = listed.reason ? null : listed.tables.some((entry) => entry.sha256 === tableSha256);
            }
            // One exact-SHA command withdraws authorization, disarms automatic
            // startup and detaches the selection while retaining local bytes.
            await revokeProfileTable(game.appId, tableSha256);
            toaster.toast({
              title: "CE Decky",
              body: [
                "No longer using this table.",
                marked === null
                  ? "The not-working mark could not be confirmed; refresh the list under Advanced."
                  : marked
                    ? "It is marked as not working; clear that under Advanced."
                    : markFailure
                      ? leadWithCause(markFailure, "CE Decky could not record that it did not work.")
                      : "CE Decky could not record that it did not work, so search may offer it again.",
              ].filter(Boolean).join(" "),
            });
          }
        })}
      />,
      );
    }
  };

  const openCheatSelection = () => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256) return;
    if (!currentInspection || currentInspection.sha256 !== current.table_sha256) {
      // Defence in depth: Home must never open the picker against a table other
      // than the profile's exact current SHA. Say so instead of doing nothing.
      setError("The inspected table no longer matches this game's selected exact table SHA. Open it again from Manage.");
      return;
    }
    logUi("panel.modal_opened", {
      modal: "cheat_selection", app_id: game.appId,
      table_sha: current.table_sha256.slice(0, 12), live: runtimeReady,
      // Apply mutates and then re-reads every safe control to confirm, and both
      // exceed the same limit on a table this size, so a mutation that had
      // succeeded came back as a partial or unknown outcome.
      beyond_live_budget: beyondLiveControlBudget,
      pinned: current.pinned.length, remembered: current.remembered.length,
    });
    showContextModal((close) => {
      // This screen closes before the question is put. A table that ran a cheat
      // and had it come straight back off is not a thing to configure, and the
      // question about it opens as a window of its own: raised while this one
      // is still up it is a window behind a window, which on the device is a
      // toast saying the table did not work and nothing else, with Apply
      // available again and no way to reach the one answer that helps.
      const askAboutRefusedTable = (reason: string) => {
        close();
        return recordRefusedTable(game, current.table_sha256 as string, reason);
      };
      return (
      <CheatSelectionModal
        appId={game.appId}
        inspection={currentInspection}
        live={runtimeReady}
        liveUnavailableReason={beyondLiveControlBudget
          ? `This table has more than ${MAX_LIVE_CONTROLS} switchable cheats, which is more than CE Decky can read back from Cheat Engine in one go.`
          : null}
        pinned={current.pinned}
        startupPreferences={current.startup}
        rememberedPreferences={current.remembered}
        configuredValues={current.configured_values ?? []}
        onTogglePin={(recordId, pinned) => runAction(() => togglePinnedControl(recordId, pinned))}
        onTableRefused={askAboutRefusedTable}
        onSaveConfiguredValues={async (values) => {
          await commitDesiredState({
            subject: "this table's configured values",
            write: () => setConfiguredValues(game.appId, current.table_sha256 as string, values),
            verify: async () => configuredValuesMatch(
              currentProfile(await refreshStatus(), game)?.configured_values, values,
            ),
          });
          // Catching up Home is not part of the commit: a failed refresh used
          // to be reported as a failed write of state that is already durable.
          await refreshStatus().catch(() => undefined);
        }}
        onValidateStartupPlan={(remembered, values) =>
          validateEffectiveStartupPlan(game.appId, current.table_sha256 as string, remembered, values)}
        onSnapshot={recordLiveSnapshot}
        onSnapshotInvalidated={() => dropLiveSnapshot()}
        autoloadEnabled={Boolean(current.autoload_enabled)}
        onCompatibilityConfirmed={async () => {
          await refreshStatus().catch((cause) => logUiFailure("picker.compatibility_refresh_failed", cause, { app_id: game.appId }));
        }}
        onApplied={async (remembered: StartupPreference[], envelope) => {
          // A selection made with nothing running is only ever going to reach
          // Cheat Engine through auto-load, so switching cheats on there means
          // asking for them - turning auto-load on is what the user just asked
          // for, not a separate decision they have to find afterwards. What was
          // actually wrong was the disclosure: the first time the user heard
          // about it was the toast after the commit, so the picker now says it
          // before Apply instead.
          //
          // Both directions ask the same question, or they would fight: has the
          // user switched a cheat on for this table? Only that is a request to
          // run something, and only that starts Cheat Engine with the game.
          //
          // A stored value is not one. It is the setting a cheat uses once it
          // is switched on - the same rule the runtime already follows, where a
          // value belonging to a cheat that is off is kept and not written - so
          // counting it here armed auto-load for a user who had opened the
          // picker, typed a number and pinned a row without selecting anything.
          // A record explicitly switched off is not one either, and arming used
          // to count any record merely mentioned, so a disarm here would have
          // been undone by the very next Apply.
          const autoloadHasWork = remembered.some((preference) => preference.active === true);
          const armAutoload = !envelope && !current.autoload_enabled && autoloadHasWork;
          // The last cheat going off with nothing running leaves auto-load with
          // nothing to do, and armed it would still start Cheat Engine on the
          // next launch of this game for no cheat at all.
          const disarmAutoload = !envelope && current.autoload_enabled && !autoloadHasWork;
          await runAction(async () => {
            const autoloadTarget = armAutoload || disarmAutoload ? armAutoload : null;
            // Two independent durable mutations, reconciled one at a time. Both
            // are exact desired state and fully inspectable, so a lost reply is
            // reconcilable rather than a failure - but only per write: grouped
            // behind one commit, a refusal of the second was read as proof that
            // the first had not happened either, and the selection the backend
            // had already stored was reported as unsaved.
            await commitDesiredState({
              subject: "the cheats you confirmed for this table",
              write: () => setRememberedCheats(game.appId, current.table_sha256 as string, remembered),
              verify: async () => rememberedMatches(
                currentProfile(await refreshStatus(), game)?.remembered, remembered,
              ),
            });
            if (autoloadTarget !== null) {
              try {
                await commitDesiredState({
                  subject: "the automatic loading of this table",
                  write: () => setAutoload(game.appId, current.table_sha256 as string, autoloadTarget),
                  verify: async () => currentProfile(await refreshStatus(), game)?.autoload_enabled === autoloadTarget,
                });
              } catch (cause) {
                throw new PriorDurableCommitError(cause, "The cheat selection for this table");
              }
            }
            if (envelope) setRuntime(envelope);
            await refreshStatus().catch(() => undefined);
          });
          close();
          toaster.toast({
            title: "CE Decky",
            body: envelope
              ? "Cheats applied and the confirmed state was saved for this exact table."
              : armAutoload
                ? "Cheats saved and auto-load switched on, so they run when this game starts."
                : disarmAutoload
                  ? "Every cheat is off for this table, so auto-load was switched off too."
                  : "Cheats saved for this exact table; they are switched on when it is next loaded.",
          });
        }}
        onCancel={close}
      />
      );
    });
  };

  /**
   * Start Cheat Engine for the exact table this game already authorized.
   *
   * Table selection, consent and target process are durable, so the second run
   * of a game must not have to search for and re-review the same table just to
   * reach the runtime. This reuses the same attach path as Review and auto-load
   * and never re-authorizes anything.
   */
  const startRuntimeForSelectedTable = () => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256 || !current.target_process) return;
    const tableSha = current.table_sha256;
    const process = current.target_process;
    if (current.execution_consent_sha256 !== tableSha) {
      setError("This exact table is not authorized yet. Open it once from Manage and confirm it.");
      return;
    }
    void runAction(async () => {
      // The disabled state this press came from is up to a poll old, and what
      // it is about is what the game is running right now. Read again, and only
      // a proven absence stops the launch: a read that fails proves nothing,
      // and a game that is running the target is the ordinary case.
      const live = await refreshCELaunch(game.appId).catch(() => null);
      const targetNotRunning = live ? absentLiveTarget(live.game, process) : null;
      if (targetNotRunning) {
        throw new Error(`${process} is not running in this game. This game is running ${targetNotRunning.join(", ")}. Set the target under Advanced, then start Cheat Engine.`);
      }
      const nextInspection = inspection?.sha256 === tableSha
        ? inspection
        : await inspectTableSha(tableSha, game.appId);
      setInspection(nextInspection);
      const observed = await ensureAttachedRuntime(game, process);
      if (observed.connected && observed.status?.attached) {
        await reconcileStartupCompatibility(game.appId, observed);
        await captureLiveSnapshot(game.appId, nextInspection);
        toaster.toast({ title: "CE Decky", body: "Table loaded and Cheat Engine connected." });
      } else {
        dropLiveSnapshot();
        toaster.toast({ title: "CE Decky", body: "Cheat Engine connected, but the target process still needs an exact PID selection in Advanced." });
      }
      autoloadAttemptRef.current = null;
      clearAutoloadRetry();
    }).catch(() => undefined);
  };

  /**
   * Toggle one pinned control straight from the panel.
   *
   * Pinning exists so the cheats a user actually uses are one press away, so
   * this must be a real runtime mutation with the same confirmation and
   * remembered-state persistence the picker's Apply performs - only narrowed to
   * this exact record.
   */
  const togglePinnedCheat = (recordId: number, active: boolean) => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256 || !currentInspection || !runtimeReady || !activeCheatSnapshotReady || !liveSnapshot) return;
    if (pinnedBusyRef.current || busyRef.current) return;
    if (!current.pinned.includes(recordId)) return;
    const tableSha = current.table_sha256;
    const controls = safeActionableControls(currentInspection);
    const activeById = new Map<number, boolean | null>(
      controls.flatMap((control) => {
        if (control.id === null) return [];
        const result = latestRuntimeResult(liveSnapshot.results, control.id);
        return [[control.id, result?.ok ? result.active : null] as const];
      }),
    );
    const control = controls.find((candidate) => candidate.id === recordId);
    if (!control) return;
    // A cheat that does nothing until it is given a number, pinned onto a panel
    // that has nowhere to type one. Configure refuses to apply one of those
    // empty, but pinning commits on its own press and Cancel closes without
    // ever reaching that refusal, so the switch could arrive here with no value
    // anywhere - and switching it on then freezes whatever the game happens to
    // hold at that instant, which is the state that refusal exists to prevent.
    //
    // One value, resolved exactly where the row resolves it: this is what the
    // row shows, what has to exist before the switch may be pressed, and what
    // the press writes. Reading one source to decide and another to send is how
    // a row reading `100` sent nothing and activated on whatever the game held.
    const needsValue = controlNeedsValueInput(control);
    const valueToApply = needsValue
      ? pinnedControlValue(
          latestRuntimeResult(liveSnapshot.results, recordId)?.value,
          current.remembered?.find((item) => item.record_id === recordId)?.value,
          current.configured_values?.find((item) => item.record_id === recordId)?.value,
        )
      : null;
    if (active && needsValue && valueToApply === null) {
      setError(`${controlRowLabel(control)} needs a value before it can be switched on. Open Configure cheats, enter one there, and apply.`);
      return;
    }
    // Project what this one toggle leaves behind, so the scripts it switched on
    // can be released again the moment nothing under them is on. A script left
    // running keeps its patch in the game for no cheat at all.
    const projected = new Map(activeById);
    projected.set(recordId, active);
    const ancestors = active ? inactiveAncestorControls(control, controls, activeById) : [];
    for (const ancestor of ancestors) if (ancestor.id !== null) projected.set(ancestor.id, true);
    const released = unusedActiveScripts(controls, projected);
    const releasedRows = released.flatMap((scriptId) => {
      const script = controls.find((candidate) => candidate.id === scriptId);
      return script ? [{ record_id: scriptId, active: false, value: null, path: script.path, label: controlRowLabel(script) }] : [];
    });
    // The scripts above carry the table author's own defaults, so every switch
    // under them that this press did not ask for is written to its off key in
    // the same call. Otherwise one pinned cheat switches on everything its
    // script declares, and the panel counts the one it was asked for.
    const heldOff = active ? switchesToHoldOff(ancestors, controls, new Set([recordId])) : [];
    const desired = active
      ? [
          ...ancestors.flatMap((ancestor) => ancestor.id === null ? [] : [{
            record_id: ancestor.id,
            active: true,
            value: null,
            path: ancestor.path,
            label: controlRowLabel(ancestor),
          }]),
          {
            record_id: recordId,
            active,
            value: active ? valueToApply : null,
            switch_values: switchValuesFor(control),
            path: control.path,
            label: controlRowLabel(control),
          },
          ...heldOff.flatMap(({ control: held, value }) => held.id === null ? [] : [{
            record_id: held.id,
            active: null,
            value,
            switch_values: switchValuesFor(held),
            path: held.path,
            label: controlRowLabel(held),
          }]),
          ...releasedRows,
        ]
      : [
          { record_id: recordId, active, value: null, path: control.path, label: controlRowLabel(control) },
          ...releasedRows,
        ];
    pinnedBusyRef.current = recordId;
    setPinnedBusyRecordId(recordId);
    void runAction(async () => {
      try {
        const confirmed = await applyRuntimeSelection(game.appId, desired);
        if (confirmed.compatibilityMayHaveChanged) {
          await refreshStatus().catch((cause) => logUiFailure("pinned.compatibility_refresh_failed", cause, { app_id: game.appId }));
        }
        // An unrelated unmaterialized child must not turn a successful pinned
        // toggle into an error.
        const finalState = await queryRuntimeControlsPartial(
          game.appId,
          controls.flatMap((control) => control.id === null ? [] : [control.id]),
          confirmed.envelope,
        );
        // What the script's own defaults cost, read back rather than assumed:
        // a flag this did not manage to put down is a cheat running that
        // nobody asked for.
        if (heldOff.length > 0) {
          const finalById = new Map(finalState.results.flatMap(
            (result) => result.record_id === null ? [] : [[result.record_id, result] as const],
          ));
          for (const script of ancestors) {
            const mine = heldOff.filter((item) => item.script === script.id);
            if (script.id === null || mine.length === 0) continue;
            logUi("runtime.flags_held_off", {
              session: confirmed.envelope?.prepared?.session_id ?? null,
              script_record_id: script.id,
              written: mine.length,
              failed: mine.filter((item) => item.control.id !== null && finalById.get(item.control.id)?.value !== item.value).length,
            });
          }
        }
        const touched = new Set<number>([recordId]);
        // The scripts around this cheat are CE Decky's own bookkeeping, so they
        // are sent to Cheat Engine but never written into the profile as a
        // choice - the startup profile derives them from the table anyway. Only
        // the pinned record itself is the user's, even when it is a script.
        const managedScripts = new Set(released);
        for (const scriptId of enclosingControlIds(controls)) {
          if (scriptId !== recordId) managedScripts.add(scriptId);
        }
        const remembered = rememberedSelection(
          controls, finalState.results, current.remembered, touched, new Set<number>(), managedScripts,
        );
        // Cheat Engine already holds this change, so Home must show it whether
        // or not the durable half lands. Publishing the snapshot first also
        // keeps a failed persistence from leaving the panel showing the state
        // from before the toggle.
        recordLiveSnapshot(finalState.results, finalState.envelope);
        const budgetError = rememberedSelectionBudgetError(current.startup, remembered);
        if (budgetError) {
          throw new Error(leadWithCause(budgetError, "The runtime change was confirmed, but it was not remembered."));
        }
        try {
          await commitDesiredState({
            subject: "this pinned cheat's state",
            write: () => setRememberedCheats(game.appId, tableSha, remembered),
            verify: async () => rememberedMatches(
              currentProfile(await refreshStatus(), game)?.remembered, remembered,
            ),
          });
        } catch (cause) {
          // "It was not remembered" is a definite claim, and the reconciler
          // reaches one case where CE Decky knows no such thing. Saying both at
          // once contradicted itself.
          throw describeCommitFailure(cause, {
            definite: "The runtime change was confirmed, but it was not remembered:",
            unknown: "The runtime change was confirmed.",
          });
        }
        // Catching Home up is not part of the write: a failed refresh used to
        // report durable state that is already stored as a failed change.
        await refreshStatus().catch(() => undefined);
      } catch (cause) {
        // This is `applyRuntimeSelection` too, so a cheat Cheat Engine ran and
        // handed straight back off means here exactly what it means in the
        // picker. Wiring it only there left the panel's own toggle able to
        // produce the refusal and unable to record it.
        if ((cause as { tableRefused?: unknown })?.tableRefused === true) {
          await recordRefusedTable(game, tableSha, describeError(cause));
        }
        // A failed mutation is still a live session. Reconcile the actual CE
        // state and keep all pinned rows visible; only invalidate the snapshot
        // when even that exact-session query fails.
        try {
          const reconciled = await queryRuntimeControlsPartial(
            game.appId,
            controls.flatMap((control) => control.id === null ? [] : [control.id]),
          );
          recordLiveSnapshot(reconciled.results, reconciled.envelope);
        } catch (reason) {
          dropLiveSnapshot(describeError(reason));
        }
        throw cause;
      }
    }).catch(() => undefined).finally(() => {
      pinnedBusyRef.current = null;
      setPinnedBusyRecordId(null);
    });
  };

  /**
   * Switch off every active control without stopping Cheat Engine.
   *
   * A user who wants the game back the way it was should not have to reopen the
   * picker and find whatever they switched on, and should not have to end the
   * session either: the same table stays loaded and ready to be used again.
   */
  const disableAllCheats = () => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256 || !currentInspection || !runtimeReady) return;
    if (pinnedBusyRef.current || busyRef.current) return;
    const tableSha = current.table_sha256;
    const controls = safeActionableControls(currentInspection);
    // A parent script can destroy the child MemoryRecords it created. Query and
    // disable leaves before their enclosing scripts so every command still has
    // a live target when the bridge processes it.
    const recordIds = [...controls]
      .sort((left, right) => right.path.length - left.path.length)
      .flatMap((control) => control.id === null ? [] : [control.id]);
    void runAction(async () => {
      dropLiveSnapshot();
      const result = await deactivateAllActiveControls(game.appId, recordIds, switchOffValues(controls));
      // A child destroyed by a parent this call switched off is the intended
      // outcome; the helper already accepted it and the caller must not undo
      // that by demanding the record still answer.
      const finalState = await queryRuntimeControlsPartial(game.appId, recordIds, result.envelope);
      // Only the records this call actually switched off are a choice. Marking
      // the whole table as touched wrote an explicit "off" for every supported
      // record, including scripts that are CE Decky's own machinery and children
      // that ceased to exist when their script went off - and the next Auto-load
      // then waited for MemoryRecords that could never appear and failed.
      const touched = new Set<number>(result.deactivatedIds);
      const remembered = rememberedSelection(
        controls, finalState.results, current.remembered, touched, new Set<number>(),
        enclosingControlIds(controls),
      );
      // The game has already been changed. Whatever happens to the durable half,
      // Home shows what Cheat Engine actually holds now.
      recordLiveSnapshot(finalState.results, finalState.envelope);
      const budgetError = rememberedSelectionBudgetError(current.startup, remembered);
      if (budgetError) {
        throw new Error(leadWithCause(budgetError, "The cheats were switched off, but that was not remembered."));
      }
      try {
        // A lost receipt here is reconcilable: the write is exact desired state
        // the profile can be asked about, so it must not be reported as a
        // failure after every cheat really was switched off.
        await commitDesiredState({
          subject: "the cheats that were switched off",
          write: () => setRememberedCheats(game.appId, tableSha, remembered),
          verify: async () => rememberedMatches(
            currentProfile(await refreshStatus(), game)?.remembered, remembered,
          ),
        });
      } catch (cause) {
        throw describeCommitFailure(cause, {
          definite: "The cheats were switched off, but that was not remembered:",
          unknown: "The cheats were switched off.",
        });
      }
      await refreshStatus().catch(() => undefined);
      toaster.toast({
        title: "CE Decky",
        body: result.deactivated === 0
          ? "No cheat was active; Cheat Engine is still running."
          : `Switched off ${result.deactivated} cheat${result.deactivated === 1 ? "" : "s"}; Cheat Engine is still running.`,
      });
    }).catch(() => undefined);
  };

  const toggleAutoload = async (enabled: boolean) => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256) return;
    // Only arming is gated: a user must always be able to withdraw automatic
    // execution, including while the prerequisites for performing it are broken.
    if (enabled && autoloadBlockedReason) return;
    await runAction(async () => {
      await commitDesiredState({
        subject: enabled ? "switching Load last table & cheats on" : "switching Load last table & cheats off",
        write: () => setAutoload(game.appId, current.table_sha256 as string, enabled),
        verify: async () => currentProfile(await refreshStatus(), game)?.autoload_enabled === enabled,
      });
      await refreshStatus().catch(() => undefined);
      autoloadAttemptRef.current = null;
      clearAutoloadRetry();
    }).catch(() => undefined);
  };

  // Auto-load needs every identity it will act on to be settled first. It does
  // not need the game to be running: setting a game up before playing it is the
  // normal way to use this, and Configure cheats switches this on by itself when
  // a selection is made with nothing running.
  const autoloadBlockedReason = !selectedGame
    ? "Choose a game first."
    : !profile?.table_sha256
      ? "Select a table for this game first."
      : profile.execution_consent_sha256 !== profile.table_sha256
        ? "Review and authorize this exact table first."
        : !profile.target_process
          ? "Confirm the game's target process first."
          // Start already required the exact table to be present and verified.
          // Auto-load did not, so a table deleted or corrupted outside the plugin
          // left Load last table & cheats armed and every attempt failed in
          // session preparation, which could never succeed.
          : !activeTable
            ? "The selected table file is missing. Download or open it again to re-import it."
            : null;

  useEffect(() => {
    if (!selectedGame || !profile?.autoload_enabled || !profile.table_sha256 || !profile.target_process) return;
    if (
      // Managed setup is an optional capability whose failure the panel already
      // degrades: gating Auto-load on it silently disabled the automatic
      // workflow for a perfectly valid imported Cheat Engine while the manual
      // Start beside it still worked. Only an active setup transition blocks.
      !status?.ce.valid
      || profile.execution_consent_sha256 !== profile.table_sha256
      // The exact table must exist and be verified. Without this an externally
      // deleted or corrupted blob left Auto-load armed and every attempt failed
      // in session preparation, which could never succeed.
      || !activeTable
      || ceRunning
      // Another game's owned Cheat Engine makes every launch here impossible,
      // so do not enter the bounded retry loop against it.
      || ownership.blockedReason !== null
      // Automatic execution is the one path with no contemporaneous user
      // decision, so a known anti-cheat must stop it before it starts.
      || antiCheatReason !== null
      || runtime?.connected
      || busyRef.current
      || contextModalDepthRef.current > 0
      || managedSetupPending
    ) return;
    const key = `${selectedGame.appId}:${profile.table_sha256}:${profile.target_process}`;
    if (autoloadAttemptRef.current === key) return;
    autoloadAttemptRef.current = key;
    void (async () => {
      try {
        const capability = await refreshCELaunch(selectedGame.appId);
        const latestOwnership = launchOwnership({
          capability,
          scopeAppId: selectedGame.appId,
          selectedAppId: selectedGame.appId,
        });
        if (
          capability.game?.app_id !== selectedGame.appId
          || !capability.game.running
          || antiCheatBlockedReason(capability.game.windows_executables ?? []) !== null
          // Nobody is watching this one, so a launch the current observation
          // already proves cannot attach must not be made at all. Re-armed by
          // the next change, which is what starting the right program is.
          || absentLiveTarget(capability.game, profile.target_process) !== null
          || latestOwnership.blockedReason !== null
          || latestOwnership.ownedBySelected
        ) {
          autoloadAttemptRef.current = null;
          clearAutoloadRetry();
          return;
        }
        // Capability discovery can yield while a detached workflow opens or the
        // selected profile changes. Revalidate the exact UI identity immediately
        // before acquiring the global mutation latch; never launch behind another
        // modal or for a stale captured AppID/table/process tuple.
        const latestGame = selectedGameRef.current;
        const latestProfile = currentProfile(statusRef.current, latestGame);
        if (
          busyRef.current
          || contextModalDepthRef.current > 0
          || latestGame?.appId !== selectedGame.appId
          || latestGame?.isShortcut !== selectedGame.isShortcut
          || latestProfile?.table_sha256 !== profile.table_sha256
          || latestProfile?.target_process !== profile.target_process
          || latestProfile?.execution_consent_sha256 !== profile.table_sha256
          || !latestProfile?.autoload_enabled
        ) {
          autoloadAttemptRef.current = null;
          clearAutoloadRetry();
          return;
        }
        await runAction(async () => {
          const observed = await ensureAttachedRuntime(selectedGame, profile.target_process as string);
          if (!observed.connected) throw new Error("Auto-load started Cheat Engine, but the bridge did not stay connected.");
          const autoloadInspection = inspection?.sha256 === profile.table_sha256
            ? inspection
            : await inspectTableSha(profile.table_sha256 as string, selectedGame.appId);
          // A fresh heartbeat proves the bridge is alive, not that the cheats
          // came back. Startup applies records one at a time and waits for any
          // an enclosing script must create, so wait for it to reach a terminal
          // state before saying anything to the user.
          const settled = await awaitStartupOutcome(selectedGame.appId, observed);
          await captureLiveSnapshot(selectedGame.appId, autoloadInspection);
          if (settled === "failed") {
            const envelope = await refreshRuntime(selectedGame.appId);
            const reason = startupFailureReason(envelope);
            // Auto-load runs with nobody watching, and a table written for a
            // different build of the game fails here first - every launch,
            // before the picker is ever opened. Record it from this path too.
            const refused = refusedStartupEnable(envelope?.status?.results ?? []);
            if (refused && profile.table_sha256) {
              await recordRefusedTable(selectedGame, profile.table_sha256, reason);
            }
            throw new Error(reason);
          }
          if (settled === "pending") {
            throw new Error("Auto-load started Cheat Engine, but the saved cheats had not been applied yet.");
          }
          // Runtime observation can persist the first compatibility evidence.
          // Converge Search/Manage without changing the successful launch outcome.
          await refreshStatus().catch((cause) => logUiFailure(
            "autoload.compatibility_refresh_failed", cause, { app_id: selectedGame.appId },
          ));
        }, { automatic: true });
        toaster.toast({ title: "CE Decky", body: "Last authorized table and confirmed cheats were auto-loaded." });
        clearAutoloadRetry();
      } catch {
        // runAction already surfaced the exact blocker, so this decides only
        // whether the same identity may be attempted again. Latching the key
        // here turned an ordinary startup race into a panel-lifetime failure:
        // Auto-load stayed inert after the condition became healthy and the
        // user had to press Load table & start CE by hand. Retry a bounded
        // number of times on a backoff instead, and never in a tight loop.
        const previous = autoloadRetryRef.current;
        const attempts = previous?.key === key ? previous.attempts + 1 : 1;
        autoloadRetryRef.current = { key, attempts };
        if (autoloadRetryTimerRef.current !== null) clearTimeout(autoloadRetryTimerRef.current);
        autoloadRetryTimerRef.current = null;
        if (attempts < AUTOLOAD_RETRY_DELAYS_MS.length + 1) {
          autoloadAttemptRef.current = null;
          autoloadRetryTimerRef.current = setTimeout(
            () => setAutoloadRetryTick(tick => tick + 1),
            AUTOLOAD_RETRY_DELAYS_MS[attempts - 1],
          );
        }
      }
    })();
    // `targetProvenAbsent` is a wake-up rather than something this reads: the
    // body takes its own fresh observation. It is here because the transition
    // this exists for - a launcher first, the game seconds later - changes no
    // other dependency, and without it Auto-load slept through it.
  }, [selectedGame?.appId, profile?.autoload_enabled, profile?.table_sha256, profile?.target_process, profile?.execution_consent_sha256, status?.ce.valid, activeTable?.sha256, ceRunning, ownership.blockedReason, antiCheatReason, ceLaunchGame?.running, targetProvenAbsent, runtime?.connected, inspection?.sha256, managedCE, managedSetupPending, autoloadRetryTick, clearAutoloadRetry, refreshCELaunch, runAction, captureLiveSnapshot]);

  const pickCE = async (): Promise<boolean> => {
    if (!status) return false;
    let selected;
    try {
      // Folders are listed so the picker can be navigated at all; only a file
      // may be submitted. A .zip is a whole Cheat Engine installation directory
      // packed up on a Windows machine, which is the fallback for the day the
      // official installer this plugin extracts is no longer downloadable. The
      // trailing page-size argument is left at Decky's default; passing 1 there
      // shows one entry per directory.
      selected = await openFilePicker(FileSelectionType.FILE, status.user_home, true, true, undefined, ["exe", "zip"], true, false);
    } catch (cause) {
      if (isDeckyFilePickerCancellation(cause)) { logUi("ce.file_picker_cancelled"); return false; }
      throw cause;
    }
    const source = selected.realpath || selected.path;
    logUi("ce.file_picked", { archive: /\.zip$/i.test(source) });
    if (/\.zip$/i.test(source)) {
      const installed = await importCEArchive(source);
      await Promise.all([refreshStatus(), refreshManagedCEOptional(), refreshCELaunch(selectedGameRef.current?.appId ?? null)]);
      toaster.toast({
        title: "CE Decky",
        body: `Imported ${installed.file_count} files from ${installed.archive_root || "the archive root"} as Cheat Engine ${installed.sha256.slice(0, 8)}.`,
      });
      return true;
    }
    await importCE(source);
    await Promise.all([refreshStatus(), refreshManagedCEOptional(), refreshCELaunch(selectedGameRef.current?.appId ?? null)]);
    return true;
  };

  const refreshRuntimeProcesses = async (): Promise<RuntimeEnvelope> => {
    const game = selectedGameRef.current;
    if (!game) throw new Error("Select a game first.");
    const result = await sendRuntimeCommandAndWait(game.appId, { kind: "list_processes" });
    setRuntime(result.envelope);
    return result.envelope;
  };

  const retryExactAttach = async (name: string, pid: number): Promise<RuntimeEnvelope> => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256) throw new Error("Select an active table first.");
    if (!isValidProcessBasename(name) || !Number.isSafeInteger(pid) || pid < 1) {
      throw new Error("Choose one valid observed .exe process and exact PID.");
    }
    const result = await sendRuntimeCommandAndWait(game.appId, { kind: "retry_attach", value: name, target_pid: pid });
    const next = result.envelope;
    setRuntime(next);
    if (!next.status?.attached || next.status.opened_process_id !== pid) {
      throw new RuntimeOperationError(`Resident bridge did not confirm attach to PID ${pid}.`, next);
    }
    if (inspection?.sha256 === current.table_sha256) {
      await captureLiveSnapshot(game.appId, inspection);
    }
    return next;
  };

  const loadGames = async () => {
    const listed = await listInstalledGames();
    // Steam's library is the account's; this device's own manifests and its own
    // shortcut store are what say which of it is here. Best effort on purpose:
    // a backend that cannot answer leaves the whole list rather than hiding a
    // game the user has.
    const library = await readLocalLibrary().catch((cause) => {
      logUiFailure("panel.local_library_unreadable", cause);
      return null;
    });
    const next = [...gamesOnThisDevice(listed, library)];
    logUi("panel.games_listed", {
      listed: listed.length,
      offered: next.length,
      installed: library?.steam_app_ids.length ?? null,
      unstartable: library?.unstartable_app_ids.length ?? null,
      shortcuts: library?.shortcut_app_ids.length ?? null,
      reason: library?.reason ?? null,
      shortcuts_reason: library?.shortcuts_reason ?? null,
    });
    setGames(next);
    return next;
  };

  const runCELaunchSelfTest = async (toolId: string) => {
    if (!toolId) throw new Error("Choose an installed Proton tool first.");
    const operation = await awaitLaunchOutcome(await startCESelfTest(toolId), true);
    await refreshCELaunch(selectedGameRef.current?.appId ?? null);
    if (operation.state !== "stopped" || !operation.bridge) {
      throw new Error(operation.error || operation.message || "Cheat Engine self-test did not complete.");
    }
    toaster.toast({ title: "CE Decky", body: "Cheat Engine started and the bridge answered; Steam state was unchanged." });
  };

  const saveAdvancedTarget = async (value: string) => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    const process = value.trim();
    if (!game || !current?.table_sha256) throw new Error("Select an active table first.");
    if (!isValidProcessBasename(process)) throw new Error("Target process must be one .exe basename, not a path.");
    const capability = await refreshCELaunch(game.appId);
    if (capability.recovered || capability.operations.some((operation) => operation.app_id === game.appId && ["starting", "running", "connected"].includes(operation.state))) {
      await stopOwnedCE(game.appId);
    }
    // The same exact desired state the picker saves, and the same reason to
    // ask the profile rather than trust a rejected reply: this has already
    // stopped a running Cheat Engine by now.
    await commitDesiredState({
      subject: "the target process",
      write: () => saveProfile(game.appId, appDetails?.displayName || game.name, game.isShortcut, current.table_sha256 as string, process),
      verify: async () => currentProfile(await refreshStatus(), game)?.target_process === process,
    });
    setTargetProcess(process);
    // Catching Home up is not part of the write. The profile is already stored,
    // so a failed status read must not report the saved target as unsaved.
    await refreshStatus().catch(() => undefined);
    autoloadAttemptRef.current = null;
    clearAutoloadRetry();
  };

  const togglePinnedControl = async (recordId: number, pinned: boolean): Promise<number[]> => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256) throw new Error("Select an active table first.");
    const tableSha = current.table_sha256;
    // Pinning is exact desired state, so a lost reply is asked about rather than
    // reported as a failed pin the profile is already holding - which left the
    // picker showing the previous pin state over a committed one.
    const committed: { pinned: number[] | null } = { pinned: null };
    await commitDesiredState({
      subject: pinned ? "pinning this cheat to Home" : "unpinning this cheat from Home",
      write: async () => {
        committed.pinned = (await setPinnedControl(game.appId, tableSha, recordId, pinned)).pinned;
      },
      verify: async () => {
        const after = currentProfile(await refreshStatus(), game);
        if (!after || after.pinned.includes(recordId) !== pinned) return false;
        committed.pinned = after.pinned;
        return true;
      },
    });
    await refreshStatus().catch(() => undefined);
    return committed.pinned ?? [];
  };

  const clearStartupActions = async (): Promise<number> => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (!game || !current?.table_sha256) throw new Error("Select an active table first.");
    const tableSha = current.table_sha256;
    // Clearing is exact desired state - an empty startup list for this exact
    // table - so a lost reply is asked about rather than reported as a failed
    // clear of actions the profile no longer holds.
    const committed: { remaining: number | null } = { remaining: null };
    await commitDesiredState({
      subject: "this table's saved startup actions",
      write: async () => {
        committed.remaining = (await clearStartupPreference(game.appId, tableSha, null)).startup.length;
      },
      verify: async () => {
        const after = currentProfile(await refreshStatus(), game);
        // Identity moving out from under the write is not a confirmation of it.
        if (!after || after.table_sha256 !== tableSha || after.startup.length > 0) return false;
        committed.remaining = after.startup.length;
        return true;
      },
    });
    await refreshStatus().catch(() => undefined);
    return committed.remaining ?? 0;
  };

  const forgetCEImport = async (): Promise<AdvancedContextSnapshot> => {
    await clearCEImport();
    return refreshAdvancedContext();
  };

  const revokeProfileTable = async (appId: number, tableSha: string) => {
    const latest = (await refreshStatus()).profiles.find((item) => item.app_id === appId);
    const alreadyRevoked = latest?.table_sha256 === null && latest.previous_table_sha256 === tableSha
      && latest.table_history[tableSha]?.execution_consent === false && !latest.autoload_enabled;
    if (!latest || (latest.table_sha256 !== tableSha && !alreadyRevoked)) {
      throw new Error("The selected table changed. Refresh Manage before revoking it.");
    }
    if (!alreadyRevoked) await stopOwnedCE(appId, tableSha);
    // Withdrawal is exact desired state and the profile is the authority on it,
    // so a lost reply must not report a revocation that already happened as a
    // failure - which invited the user to press it again.
    await commitDesiredState({
      subject: "withdrawing this table's execution authorization",
      write: () => revokeTable(appId, tableSha),
      verify: async () => {
        const after = (await refreshStatus()).profiles.find((item) => item.app_id === appId);
        // An unreadable profile proves nothing about consent, and neither does
        // one that has moved to another table: that table's consent field
        // trivially differs from this SHA, while this table's own consent was
        // archived into `table_history` when the selection changed and is
        // restored the moment it is selected again. Reporting the withdrawal as
        // done would leave an authorization that comes back by itself. Fail
        // closed on identity, exactly as clearing the startup actions does.
        return Boolean(after && after.table_sha256 === null && !after.autoload_enabled
          && after.execution_consent_sha256 === null
          && after.table_history[tableSha]?.execution_consent === false);
      },
    });
    // Consent is already withdrawn; a failed status read is not a failed
    // revocation.
    await refreshStatus().catch(() => undefined);
  };

  const revokeConsent = async () => {
    const game = selectedGameRef.current;
    const current = currentProfile(statusRef.current, game);
    if (game && current?.table_sha256) await revokeProfileTable(game.appId, current.table_sha256);
  };

  const refreshAdvancedContext = async (gameOverride?: GameSummary | null): Promise<AdvancedContextSnapshot> => {
    const game = gameOverride === undefined ? selectedGameRef.current : gameOverride;
    const [nextStatus, , nextLaunch, nextDetails, nextRuntime] = await Promise.all([
      refreshStatus(),
      refreshManagedCEOptional(),
      refreshCELaunch(game?.appId ?? null),
      game ? readAppDetails(game.appId) : Promise.resolve(null),
      game ? refreshRuntime(game.appId) : Promise.resolve(null),
    ]);
    if (game && nextDetails?.isShortcut !== game.isShortcut) {
      throw new Error(`Steam identity changed for AppID ${game.appId}; refusing to refresh the old game context.`);
    }
    const nextProfile = currentProfile(nextStatus, game);
    let nextInspection: TableInspection | null = null;
    if (nextProfile?.table_sha256 && nextStatus.tables.some((candidate) => candidate.sha256 === nextProfile.table_sha256 && candidate.available)) {
      nextInspection = await inspectTableSha(nextProfile.table_sha256, game?.appId ?? null);
    }
    return {
      status: nextStatus,
      ceLaunch: nextLaunch,
      runtime: nextRuntime,
      appDetails: nextDetails,
      inspection: nextInspection,
      targetProcess: nextProfile?.target_process ?? "",
    };
  };

  const openAdvanced = (gameOptions: GameSummary[] = games) => {
    if (!status) return;
    showContextModal((close) => (
      <AdvancedModal
        status={status}
        games={gameOptions}
        selectedGame={selectedGame}
        appDetails={appDetails}
        inspection={inspection}
        targetProcess={targetProcess}
        ceLaunch={ceLaunchView}
        launchProtonToolId={launchProtonToolId}
        runtime={runtime}
        selfTest={selfTest}
        busy={busy}
        onRefreshGames={() => runAction(loadGames)}
        onSaveTargetProcess={(value) => runAction(() => saveAdvancedTarget(value)).then(() => { close(); })}
        onPickCE={() => runAction(pickCE).then((imported) => { if (imported) close(); })}
        onClearCEImport={() => runAction(forgetCEImport)}
        onRunSelfTest={() => runAction(async () => {
          const result = await runSelfTest();
          setSelfTest(result);
          toaster.toast({ title: "CE Decky", body: selfTestSummary(result).toast });
          return result;
        })}
        onLaunchProtonChange={setLaunchProtonToolId}
        onRunCELaunchSelfTest={(toolId) => runAction(() => runCELaunchSelfTest(toolId))}
        onRefreshRuntime={() => selectedGameRef.current
          ? runAction(() => refreshRuntime(selectedGameRef.current!.appId))
          : Promise.resolve(null)}
        onRefreshProcesses={() => runAction(refreshRuntimeProcesses)}
        onRetryAttach={(name, pid) => runAction(() => retryExactAttach(name, pid))}
        onRepairSessionState={() => runAction(async () => {
          const game = selectedGameRef.current;
          if (!game) throw new Error("Choose a game first.");
          return repairSessionState(game.appId);
        })}
        onRepairOwnedLaunchState={() => runAction(async () => {
          const game = selectedGameRef.current;
          if (!game) throw new Error("Choose a game first.");
          return repairOwnedLaunchState(game.appId);
        })}
        onRepairProfileState={() => runAction(async () => {
          return repairProfileState();
        })}
        onClearStartup={() => runAction(clearStartupActions)}
        onRevokeConsent={() => runAction(revokeConsent).then(() => { close(); })}
        onLoadDiagnostics={() => getDiagnosticsSnapshot()}
        onCollectSupportBundle={() => {
          // Read the panel's own record at the moment of the press, so the
          // archive describes the session that produced the problem.
          const panel = readSupportLog();
          return createSupportBundle(panel.entries, panel.dropped);
        }}
        blockedTables={blockedTables}
        blockedTablesReason={blockedTablesReason}
        onRefreshBlockedTables={() => refreshBlockedTables()}
        onUnblockTable={(sha256) => runAction(() => clearFailedMark(sha256))}
        onClearBlockedTables={() => runAction(() => clearFailedMark())}
        onLoadProviderSources={() => getProviderSources()}
        onSetProviderEnabled={(providerId, enabled) => runAction(() => commitSourceSelection(
          `${enabled ? "using" : "not using"} ${providerDisplayName(providerId)}`,
          () => setProviderEnabled(providerId, enabled),
          sourceSwitched(providerId, enabled),
        ))}
        onResetProviderSources={() => runAction(() => commitSourceSelection(
          "using every table source",
          () => resetProviderSources(),
          everySourceOn,
        ))}
        onResetProviderDiagnostics={() => runAction(() => commitSourceSelection(
          "the counters being cleared",
          () => resetProviderDiagnostics(),
          countsReadable,
        ))}
          onCheckRemoval={() => runAction(getRemovalReadiness)}
        onDeleteManagedData={(scope) => runAction(async () => {
          // A refusal is not an uncertain outcome. The backend checks before it
          // touches anything and says so in the first words of the message, so
          // there is nothing on this side to reconcile and every durable choice
          // of the user's stays where it is: forgetting the game they chose for
          // a deletion that explicitly did not happen is a disagreement this
          // side invents by itself.
          let refused = false;
          try {
            return await deleteManagedData(scope);
          } catch (cause) {
            refused = refusedBeforeDeleting(cause);
            throw cause;
          } finally {
            // Everything the deleted files were backing on this side goes with
            // them: cached search outcomes name sources whose choice may have
            // been reset, and the retirement marks are the ephemeral half of a
            // durable blocked-table record that has just been erased.
            //
            // In a finally, because a deletion commits before the call returns:
            // a reply lost after that point leaves the files gone and this
            // side describing them, and reconciling only on the success path is
            // exactly how it kept describing them.
            if (!refused) {
              forgetSearchOutcomes();
              if (scope === "all") {
                // "Everything" promises a first-run CE Decky, and this side
                // keeps durable choices of its own that the file sweep cannot
                // reach: the game the user picked by hand, which a panel
                // mounting with no game running restores by itself, and the
                // remembered answer for whether the mascot is drawn before the
                // backend has said. A deletion that leaves either of them is a
                // first run that opens on the state it was told to forget.
                forgetSelectedGame();
                setSelectedGame(null);
                selectedGameRef.current = null;
                rememberMascotVisible(true);
              }
              if (scope !== "cache") {
                forgetAllRejectedArtifacts();
                // Invalidate before reconciliation, including an uncertain
                // reply. Reads started before deletion cannot publish the old
                // authority.
                statusGenerationRef.current += 1;
                runtimeGenerationRef.current += 1;
                blockedGenerationRef.current += 1;
                statusRef.current = null;
                setStatus(null);
                setRuntime(null);
                dropLiveSnapshot();
                blockedTablesRef.current = { tables: [], reason: null };
                setBlockedTables([]);
                await refreshStatus().catch((cause) => {
                  logUiFailure("panel.status_after_bulk_delete_failed", cause, { scope });
                });
                await refreshBlockedTables().catch(() => undefined);
              }
            }
          }
        })}
        update={updateState}
        onSetUpdateAutoCheck={(enabled) => runAction(async () => {
          const next = await setUpdateAutoCheck(enabled);
          await refreshStatus().catch(() => undefined);
          return next;
        })}
        onCheckForUpdate={() => runAction(async () => {
          const next = await checkForUpdate();
          await refreshStatus().catch(() => undefined);
          return next;
        })}
        onStartUpdate={(target) => { close(); openUpdateModal(target); }}
        onSetMascotVisible={(visible) => runAction(async () => {
          const next = await setMascotVisible(visible);
          await refreshStatus().catch(() => undefined);
          return next;
        })}
        onRefreshAll={() => runAction(() => refreshAdvancedContext())}
        onClose={close}
      />
    ));
  };

  /**
   * What the panel knows about updates right now, from the status it already reads.
   *
   * `updateVersion` applies the one rule the home panel has about this: with
   * automatic checking switched off nothing is offered there, whatever an
   * earlier check found. Advanced still shows the finding, because that screen
   * is where the switch is and a user who has just turned it back on should see
   * why it matters.
   */
  const updateState = status?.update ?? null;
  const offeredUpdateVersion = updateState?.update_available ? updateState.latest_version : null;
  const panelUpdateVersion = panelUpdateOffer(updateState);
  // An update this panel did not start, and cannot have: every modal closes the
  // panel, so the window that started one is gone by the time it matters, and
  // the backend goes on with it. The panel adopts it from the status it already
  // reads - holding down everything the plugin being replaced would interrupt,
  // and offering the way back into the window that reports it.
  // Only an update that is still happening. An operation the backend has
  // already settled is history, and handing it to the window as something to
  // follow made every control on that window a no-op: it opened busy, waiting
  // for news about work that had already ended, so Try again, Not now and Back
  // all did nothing until a poll happened to say what the status already had.
  const settledUpdate = ["failed", "cancelled"];
  const runningUpdate = updateState?.operation && !settledUpdate.includes(updateState.operation.state)
    ? updateState.operation
    : null;
  const updateRunning = runningUpdate !== null;
  // The backend is the authority and this is the frame before it answers: a
  // panel is rebuilt every time a modal opens, and guessing "on" showed the
  // image to the user who had just switched it off, once per rebuild.
  const mascotVisible = status?.preferences?.mascot_visible ?? readMascotVisible();
  useEffect(() => {
    if (status?.preferences) rememberMascotVisible(status.preferences.mascot_visible);
  }, [status?.preferences?.mascot_visible]);

  const openUpdateModal = (requested?: string, adopt?: PluginUpdateOperation | null) => {
    // What the control that was pressed was showing, and only then what this
    // panel last read. Advanced keeps its own snapshot and is not re-rendered
    // from here, so a check run there can find a version this closure has
    // never seen: taking the parent's copy opened a confirmation for the older
    // version, or opened nothing at all.
    const target = requested ?? adopt?.version ?? offeredUpdateVersion;
    if (!status || !target) return;
    showContextModal((close) => (
      <UpdateModal
        currentVersion={status.version}
        targetVersion={target}
        gameRunning={runningGamesRef.current.length > 0}
        adopted={adopt ?? null}
        onStart={(targetVersion) => startPluginUpdate(targetVersion)}
        onPoll={(operationId) => pollPluginUpdate(operationId)}
        onCancelUpdate={(operationId) => cancelPluginUpdate(operationId)}
        onClose={() => {
          close();
          // What the press changed about this device is in the status the panel
          // reads: a cancelled update, a failed one, and the record of what the
          // last check found all live there.
          void refreshStatus().catch(() => undefined);
        }}
      />
    ));
  };

  const openGamePicker = (gameOptions: GameSummary[]) => {
    // Only the ambiguity this cannot resolve by itself is marked. One running
    // game is auto-selected and never reaches here, and an ordinary Change game
    // is about the library rather than about what is running.
    const ambiguousRunning = !selectedGameRef.current && runningGamesRef.current.length > 1
      ? runningGamesRef.current
      : [];
    showContextModal((close) => (
      <GamePickerModal
        games={gameOptions}
        runningGames={ambiguousRunning}
        selectedGame={selectedGameRef.current}
        onPick={async (game) => {
          await runAction(async () => {
            // Asked again here, and not only where this was opened. The picker
            // is open for as long as the user takes to read a library, the
            // detector is suppressed for all of it, and the game can start in
            // that window. The modal keeps the refusal in front of the press
            // that made it and the selection stays where it was.
            await refuseWhileSelectedGameRuns("commit");
            const next = await hydrateGame(game, "manual");
            if (!next) throw new Error("Game selection was superseded before its exact context could be confirmed.");
          });
          close();
        }}
        onCancel={close}
      />
    ));
  };

  if (!status) {
    // While the first read is still in flight every control here is a
    // placeholder and stays disabled. Once it has failed, Retry is the one
    // control that has to be pressable: nothing else in this branch does
    // anything, so without it the panel could only recover by being remounted.
    const bootstrapFailed = error !== null;
    return <HomePanel
      pluginVersion={null}
      updateVersion={null}
      onUpdate={() => undefined}
      mascotVisible={mascotVisible}
      updateRunning={false}
      ceReady={false}
      ceStatusText="Loading plugin status…"
      installAvailable={false}
      installBusy={false}
      setupPending={false}
      setupStatusError={error}
      onRetrySetupStatus={() => { setBootstrapAttempt(0); void bootstrap(); }}
      installOperation={null}
      ceSource={null}
      ceSha256={null}
      onInstall={() => undefined}
      onCancelInstall={() => undefined}
      reinstallLabel="Reinstall CE"
      onReinstall={() => undefined}
      game={null}
      appDetails={null}
      runningDetectionAvailable={false}
      runningGameCount={0}
      selectedGameRunning={false}
      targetProcess={null}
      onChooseGame={() => undefined}
      table={null}
      tableSource="Local"
      onSearchTable={() => undefined}
      tableMarkedNotWorking={null}
      onOpenImportedTables={() => undefined}
      runtimeReady={false}
      runtimeText="Loading…"
      runtimeTextComplete
      liveControlsUnavailable={false}
      liveSnapshotError={null}
      startRuntimeAvailable={false}
      startRuntimeBlockedReason={null}
      onStartRuntime={() => undefined}
      activeCheatLabels={[]}
      activeScriptCount={0}
      activeCheatSnapshotReady={false}
      pinnedCount={0}
      pinnedRows={[]}
      pinnedBusyRecordId={null}
      onTogglePinnedCheat={() => undefined}
      onChooseCheats={() => undefined}
      onDisableAllCheats={() => undefined}
      autoloadEnabled={false}
      autoloadBlockedReason="Loading…"
      onAutoloadChange={() => undefined}
      ceRunning={false}
      ceIdentityBlockedReason={null}
      launchPending={false}
      onStopCE={() => undefined}
      onAdvanced={() => undefined}
      busy={!bootstrapFailed}
      error={error}
    />;
  }

  return (
    <>
      <HomePanel
      pluginVersion={`v${status.version}`}
      updateVersion={panelUpdateVersion}
      updateRunning={updateRunning}
      onUpdate={() => openUpdateModal(panelUpdateVersion ?? undefined, runningUpdate)}
      mascotVisible={mascotVisible}
      ceReady={status.ce.valid}
      ceStatusText={ceStatusText}
      installAvailable={installAvailable}
      installBusy={installBusy}
      setupStatusError={managedCEError}
      onRetrySetupStatus={() => { void refreshManagedCE().catch(() => undefined); }}
      setupPending={managedSetupPending}
      installOperation={managedInstallSnapshot}
      ceSource={status.ce.valid ? managedReleaseInstalled ? "Managed" : "Imported" : null}
      ceSha256={status.ce.sha256}
      onInstall={() => chooseManagedSetup(false)}
      managedCancelling={managedCancelling}
      onCancelInstall={() => void cancelManagedSetup()}
      reinstallLabel={managedReleaseInstalled ? "Reinstall CE" : "Install managed CE"}
      onReinstall={() => chooseManagedSetup(true)}
      game={selectedGame}
      appDetails={appDetails}
      runningDetectionAvailable={runningGames.available}
      runningGameCount={runningGames.games.length}
      selectedGameRunning={selectedGameRunning}
      targetProcess={profile?.target_process ?? null}
      targetNotRunning={targetNotRunning}
      onChooseGame={() => {
        void runAction(async () => {
          // Before the library is even read, because the answer decides whether
          // there is anything to open. The row's disabled state is up to three
          // seconds old and this is the press itself.
          await refuseWhileSelectedGameRuns("open");
          return await loadGames();
        }).then(openGamePicker).catch(() => undefined);
      }}
      table={activeTable}
      tableSource={tableSource}
      onSearchTable={openTableSearch}
      searchButtonRef={searchButtonRef}
      preferSearchFocus={preferSearchFocus}
      selectedTableMissing={profile?.table_sha256 && !activeTable
        ? "The file for this game's selected table is gone. Download or open it again, or pick another."
        : null}
      tableMarkedNotWorking={selectedTableMarkedNotWorking}
      tableEvidence={profile?.table_sha256
        ? status?.table_compatibility?.entries.find((entry) => (
          entry.app_id === selectedGame?.appId && entry.table_sha256 === profile.table_sha256
        ))
        : undefined}
      tableBlocked={selectedTableMark ?? null}
      onOpenImportedTables={openImportedTables}
      runtimeReady={runtimeReady}
      runtimeText={runtimeText}
      runtimeTextComplete={runtimeTextComplete}
      runtimeLabel={runtimeLabel}
      liveControlsUnavailable={beyondLiveControlBudget}
      tableLoadFailed={tableLoadFailed}
      liveSnapshotError={liveSnapshotError}
      startRuntimeAvailable={startRuntimeBlockedReason === null}
      startRuntimeBlockedReason={startRuntimeBlockedReason}
      onStartRuntime={startRuntimeForSelectedTable}
      activeCheatLabels={activeCheatLabels}
      activeScriptCount={activeScriptCount}
      activeCheatSnapshotReady={activeCheatSnapshotReady}
      pinnedCount={profile?.pinned.length ?? 0}
      pinnedRows={pinnedRows}
      pinnedBusyRecordId={pinnedBusyRecordId}
      onTogglePinnedCheat={togglePinnedCheat}
      onChooseCheats={openCheatSelection}
      onDisableAllCheats={disableAllCheats}
      autoloadEnabled={profile?.autoload_enabled ?? false}
      autoloadBlockedReason={autoloadBlockedReason}
      onAutoloadChange={(enabled) => { void toggleAutoload(enabled); }}
      ceRunning={ceRunning}
      ceIdentityBlockedReason={ceIdentityBlockedReason}
      launchPending={launchInProgress !== null && launchInProgress.appId === selectedGame?.appId}
      onStopCE={() => {
        const pending = launchInProgress;
        if (pending) {
          // The launch that is still waiting owns the busy latch, so cancelling
          // it cannot go through `runAction`; the backend stop is safe for a
          // `starting`/`running` operation and the awaiting caller then ends
          // with the stopped state instead of its own timeout.
          void stopOwnedCE(pending.appId).catch((cause) => setError(describeError(cause)));
          return;
        }
        if (selectedGameRef.current) void runAction(() => stopOwnedCE(selectedGameRef.current!.appId)).catch(() => undefined);
      }}
      onAdvanced={() => openAdvanced()}
      busy={busy || (managedCE === null && managedCEError === null)}
      error={error}
      />
    </>
  );
}

/**
 * Record a render crash on its way to Decky's own boundary.
 *
 * `ErrorBoundary` from `@decky/ui` takes no error callback, so a component that
 * threw while rendering produced Decky's fallback and nothing else: the one
 * class of UI bug a user cannot describe was also the one that left no trace.
 * This sits inside it, writes the failure to the support log, and then rethrows
 * on the next render so Decky's boundary still handles the display exactly as
 * before.
 */
class SupportLogBoundary extends Component<PropsWithChildren, { error: unknown }> {
  state: { error: unknown } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error };
  }

  componentDidCatch(error: unknown, info: { componentStack?: string | null }) {
    logUiFailure("panel.render_crashed", error, { component_stack: info?.componentStack ?? null });
  }

  render() {
    if (this.state.error !== null) throw this.state.error;
    return this.props.children;
  }
}

export default definePlugin(() => {
  // Started here rather than inside the panel, and stopped by `onDismount`
  // below. Opening any modal unmounts the panel, and what a modal recorded
  // before something went wrong is exactly what a report needs, so the flush
  // has to outlive every one of them.
  const stopSupportLogFlush = startSupportLogFlush();
  // One id per factory invocation, which is one per row Decky adds, and never
  // the batch id the whole loaded module shares: two imports of this bundle
  // issued in the same millisecond resolve to one module URL and call this
  // factory twice, so the module is not what a row is.
  const panelInstance = nextPanelInstance();
  // The renderer beside the row, because the row says which panel this is and
  // the renderer says which generation of the frontend it belongs to. An
  // install reads the second to know whether a record it is looking at came
  // from the frontend it replaced.
  const panelLifetime = {
    panel_instance: panelInstance,
    panel_renderer: panelRenderer(),
    // When this renderer started, which is what says which side of a frontend
    // reload a row belongs to: one created by the reload started after it was
    // asked for, and the one being replaced started before. A moment rather
    // than an age, because an age is compared against however long the reader
    // has been waiting and would classify this same fixed record differently
    // on two reads of it.
    renderer_started_at_ms: String(rendererStartedAt()),
  };
  logUi("panel.mounted", panelLifetime);
  return {
    name: "CE Decky",
    titleView: <div className={staticClasses.Title}>CE Decky</div>,
    content: <ErrorBoundary><SupportLogBoundary><Content /></SupportLogBoundary></ErrorBoundary>,
    icon: <div style={{ fontWeight: 700 }}>CE</div>,
    onDismount() {
      // Recorded before the flush is stopped, so the durable record ends on the
      // entry that says this was an ordinary dismount. A record that stops on
      // anything else is a frontend that did not get to say goodbye, which is
      // what separates a closed panel from a wedged one.
      logUi("panel.dismounted", panelLifetime);
      stopSupportLogFlush();
      console.log("CE Decky frontend dismounted");
    },
  };
});
