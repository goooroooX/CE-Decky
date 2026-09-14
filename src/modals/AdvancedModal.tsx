import { useUiSurface } from "../useUiSurface";
import { traceUiAction, traceUiEdit, startUiOperation } from "../uiActions";
import {
  ConfirmModal,
  DialogButton,
  DropdownItem,
  Focusable,
  ModalRoot,
  PanelSection,
  PanelSectionRow,
  TextField,
  showModal,
} from "@decky/ui";
import { useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ActionGroup, ActionRow, CONTENTS_ONLY, DensePanel, PanelRow, SectionHeading, SmallButton, focusFirstEnabled } from "../components/PanelDensity";
import { DestructiveAction, ModalActions, modalActionStyle } from "../components/ModalActions";
import { DebugDetails } from "../components/DebugDetails";
import { describeError } from "../errors";
import { TableCodeModal } from "./TableCodeModal";
import { openExternalWeb } from "../externalNavigation";
import { logUiFailure } from "../supportLog";
import type { AppDetailsSnapshot, GameSummary } from "../steam/client";
import { absentLiveTarget, blockedKey, blockedRecordedOn, blockedRowDetail, blockedRowLabel, divergentLiveTarget, isExactRuntimeSession, isValidProcessBasename, isWineRuntimeExecutable, launchOwnership, providerDisplayName, releaseLabel, runtimeAttachCandidates, selfTestCheckLabel, selfTestSummary } from "../uiModel";
import type { BlockedTable, CELaunchCapability, CEStatus, DiagnosticsSnapshot, ManagedDataDeletion, ManagedDataScope, PluginStatus, ProviderSourceStatus, ProviderSourcesSnapshot, RemovalReadiness, RuntimeEnvelope, SelfTestResult, SupportBundleResult, TableInspection } from "../types";

export interface AdvancedContextSnapshot {
  status: PluginStatus;
  ceLaunch: CELaunchCapability | null;
  runtime: RuntimeEnvelope | null;
  appDetails: AppDetailsSnapshot | null;
  inspection: TableInspection | null;
  targetProcess: string;
}

interface Props {
  status: PluginStatus;
  games: GameSummary[];
  selectedGame: GameSummary | null;
  appDetails: AppDetailsSnapshot | null;
  inspection: TableInspection | null;
  targetProcess: string;
  ceLaunch: CELaunchCapability | null;
  launchProtonToolId: string;
  runtime: RuntimeEnvelope | null;
  selfTest: SelfTestResult | null;
  busy: boolean;
  onRefreshGames: () => Promise<GameSummary[]>;
  onSaveTargetProcess: (value: string) => Promise<void> | void;
  onPickCE: () => Promise<void> | void;
  onClearCEImport: () => Promise<AdvancedContextSnapshot>;
  onRunSelfTest: () => Promise<SelfTestResult>;
  onLaunchProtonChange: (toolId: string) => void;
  onRunCELaunchSelfTest: (toolId: string) => Promise<void>;
  onRefreshRuntime: () => Promise<RuntimeEnvelope | null>;
  onRefreshProcesses: () => Promise<RuntimeEnvelope>;
  onRetryAttach: (name: string, pid: number) => Promise<RuntimeEnvelope>;
  /** Discard a current-session pointer this build cannot read. */
  onRepairSessionState: () => Promise<unknown>;
  /** Quarantine malformed durable CE ownership after backend absence proof. */
  onRepairOwnedLaunchState: () => Promise<unknown>;
  /** Quarantine an unreadable profile store and start a clean one. */
  onRepairProfileState: () => Promise<unknown>;
  onClearStartup: () => Promise<number>;
  onRevokeConsent: () => Promise<void> | void;
  onCheckRemoval: () => Promise<RemovalReadiness>;
  /** Delete plugin data at one of the three named scopes. Irreversible. */
  onDeleteManagedData: (scope: ManagedDataScope) => Promise<ManagedDataDeletion>;
  /** Read-only backend diagnostics for the debug view. */
  onLoadDiagnostics: () => Promise<DiagnosticsSnapshot>;
  /** Write one archive of logs, configuration and state for a bug report. */
  onCollectSupportBundle: () => Promise<SupportBundleResult>;
  /** Which table sources are on, and what each one has been doing. */
  onLoadProviderSources?: () => Promise<ProviderSourcesSnapshot>;
  /** Switch one source on or off. Returns the whole set as it now stands. */
  onSetProviderEnabled?: (providerId: string, enabled: boolean) => Promise<ProviderSourcesSnapshot | null>;
  /** Switch every source back on, including when the record cannot be read. */
  onResetProviderSources?: () => Promise<ProviderSourcesSnapshot | null>;
  /** Replace an unreadable record of what each source has done. */
  onResetProviderDiagnostics?: () => Promise<ProviderSourcesSnapshot | null>;
  /** Tables recorded as not working, and the two ways to undo that. */
  blockedTables?: BlockedTable[];
  /** Why the record is empty, when it is empty because it could not be read. */
  blockedTablesReason?: string | null;
  onRefreshBlockedTables?: () => Promise<{ tables: BlockedTable[]; reason: string | null }>;
  onUnblockTable?: (sha256: string) => Promise<void>;
  onClearBlockedTables?: () => Promise<void>;
  onRefreshAll: () => Promise<AdvancedContextSnapshot>;
  onClose: () => void;
  /** Temporary campaign-only target harness; absent in ordinary builds. */
  targetHarness?: ReactNode;
}

/**
 * What actually stopped the game being asked back, in the user's terms.
 *
 * Asking a game to come back needs two calls: one that says whether a window is
 * minimized, and one that posts the request. Either can be missing, and the
 * outcome is the same dark game, but the two are not the same defect and a
 * report that names the wrong one sends its reader to a call that works.
 */
const RESTORE_CAPABILITY_LABEL: Record<string, string> = {
  "no-post": "Cannot ask the game to come back",
  "no-local-call": "This Cheat Engine cannot be asked about windows",
  "no-window": "Still working out whether the game can be brought back",
  "symbols-unresolved": "Still working out whether the game can be brought back",
  "iconic-unanswered": "Cannot tell whether the game is minimized",
  "iconic-disagrees": "Cannot tell whether the game is minimized",
};

/**
 * The capability states that become `ready` on their own.
 *
 * They are Cheat Engine still starting rather than a refusal, so the row says
 * to wait rather than telling the user their game cannot be brought back.
 */
const RESTORE_CAPABILITY_SETTLING = new Set(["no-window", "symbols-unresolved"]);

const RESTORE_CAPABILITY_HELP: Record<string, string> = {
  "no-post": "This one answers that question. What it cannot do is post the request, so the answer cannot be acted on and nothing is sent.",
  "no-local-call": "This Cheat Engine offers no way to call into Windows at all, so neither half of that can be done.",
  "no-window": "Cheat Engine has not yet shown a window to test the question against. This usually settles by itself within a few seconds of attaching.",
  "symbols-unresolved": "Cheat Engine builds the table it looks these calls up in while it starts, and it has nothing to answer with yet. This usually settles by itself within a few seconds of attaching, and the game is asked back as soon as it does.",
  "iconic-unanswered": "This Cheat Engine does not answer that question, so nothing is sent at all.",
  "iconic-disagrees": "The call that should answer it says Cheat Engine's own hidden window is minimized, which it is not, so its answer is not trusted for the game either and nothing is sent.",
};

/** How many blocked tables the review screen adds per press of Show more. */
const BLOCKED_TABLES_PAGE = 25;

/** A byte count as the short human figure a diagnostics row needs. */
/** The Cheat Engine installation's own story, for the one screen that owns it.
 *
 * Ordinary setup downloads the exact artifact the packaged manifest reviewed
 * and nothing here is interesting. It becomes interesting when that link stops
 * working: CE Decky then reads the current link out of the download helper
 * cheatengine.org hands out, and what comes back may be a newer release than
 * the one that was reviewed. That succeeds silently on purpose - a working
 * setup should not interrupt anyone - so this is where it is stated.
 *
 * The working URL is deliberately not shown. It is a live download route that
 * nothing here needs, and printing it only invites fetching it by hand.
 */
function InstallerProvenance({ ce }: { ce: CEStatus }) {
  if (!ce.valid) return null;
  if (!ce.managed) {
    return (
      <PanelRow
        truncate
        testId="ce-provenance-source"
        label="Cheat Engine source"
        description="Imported by you"
        help="This Cheat Engine was imported rather than downloaded by CE Decky, so its origin is whatever you pointed the plugin at. Forget it in Fallback below to go back to a managed download."
      />
    );
  }
  const provenance = ce.provenance;
  if (!provenance) {
    return (
      <PanelRow
        truncate
        testId="ce-provenance-source"
        label="Cheat Engine source"
        description="Downloaded by CE Decky before it recorded how"
        help="This installation predates CE Decky recording where its artifact came from. It is verified and usable; only the record is absent. Reinstalling from Home records it."
      />
    );
  }
  const rediscovered = provenance.source === "rediscovered";
  const unrecorded = provenance.source === "cache";
  const signed = provenance.signature_common_name || provenance.signature_subject;
  return (
    <>
      <PanelRow
        truncate
        testId="ce-provenance-source"
        label="Cheat Engine source"
        description={rediscovered
          ? "Downloaded by CE Decky using the current cheatengine.org link"
          : unrecorded
            ? "Downloaded by CE Decky; the link it used was not recorded"
            : "Downloaded by CE Decky from the reviewed link"}
        help={rediscovered
          ? "The download link this build of CE Decky ships stopped working, so setup read the current one from the download helper cheatengine.org offers and used that instead. The helper is parsed, never run. What it pointed at still had to be the reviewed release before anything was installed."
          : unrecorded
            ? "This artifact was already in CE Decky's own verified cache when setup ran, from a download whose route predates this record. Its bytes are the reviewed release either way - that is what the cache is checked against before it is reused."
            : "Setup used the exact download link this build of CE Decky ships, and the artifact matched the reviewed release byte for byte."}
      />
      {/* What vouched for it was a row of its own and said nothing the row
          below does not: the digest being the reviewed one is the whole reason
          anything was installed, so "matches the reviewed release" was the
          artifact row restated as a verdict. What it actually carried was in
          its help, and that belongs with the artifact it is about. */}
      <PanelRow
        truncate
        testId="ce-provenance-artifact"
        label="Installer artifact"
        description={`${provenance.artifact_sha256.slice(0, 12)} · ${formatBytes(provenance.artifact_bytes)} · ${provenance.origin_host}`}
        help={`The Windows installer this Cheat Engine was extracted from, its size, and the host that served it. The installer was never executed: CE Decky parses it and writes out its payload itself.

SHA-256 ${provenance.artifact_sha256}

That digest is the one this build reviewed, and it is the only thing that authorizes an install: the extractor reads that exact artifact's format and nothing else. The publisher recorded at review time was ${provenance.reviewed_subject}.${signed ? ` This copy's Authenticode signature was also checked against the exact publisher key CE Decky reviewed${provenance.signature_digest ? ` (${provenance.signature_digest})` : ""}, and it covers these exact bytes, signed by ${signed}. No certificate authority is trusted for that; the key itself is pinned.` : ""}`}
      />
      {provenance.helper_host && (
        <PanelRow
          truncate
          testId="ce-provenance-helper"
          label="Download helper"
          description={`${provenance.helper_host}${provenance.helper_format ? ` · ${provenance.helper_format}` : ""}`}
          help="cheatengine.org hands out a third-party download manager rather than the installer itself, and the current installer link is a value inside it. CE Decky reads that value out of it and never runs it."
        />
      )}
      {provenance.signature_note && (
        <PanelRow
          truncate
          testId="ce-provenance-note"
          label="Signature was not checked"
          description={provenance.signature_note}
          help="The artifact's SHA-256 already proved it is the reviewed release, so this did not block setup. It only means the extra signature check could not also run, and with it the reason an artifact that was not the reviewed release would have been refused for."
        />
      )}
    </>
  );
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  const units = ["KiB", "MiB", "GiB"];
  let value = size / 1024;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[index]}`;
}

/**
 * What one table source has actually been doing, in one line.
 *
 * A source is switched off after it has been seen failing, timing out, or
 * finding nothing, so the row that offers the switch has to carry that evidence
 * rather than only the source's name. A source that has never been asked for
 * anything says so instead of showing a row of zeroes, because nothing recorded
 * and everything recorded as zero are different facts.
 */
function describeSource(source: ProviderSourceStatus, countsUnreadable: boolean): string {
  const counters = source.counters;
  // Two different reasons for having no counters, and only one of them says
  // anything about this source: nothing has asked it yet, or the whole record
  // of what every source has done could not be read.
  if (!counters) return countsUnreadable ? "Counts unavailable" : "Not searched yet";
  const parts = [
    `${counters.searches} search${counters.searches === 1 ? "" : "es"}`,
    `${counters.results} result${counters.results === 1 ? "" : "s"}`,
  ];
  // Stated separately, because otherwise a source reached only through another
  // source's page reads as "0 searches" beside a result count nothing on the
  // row explains.
  if (counters.linked_reads) parts.push(`${counters.linked_reads} through another source`);
  if (counters.downloads_succeeded || counters.downloads_failed) {
    parts.push(`${counters.downloads_succeeded} downloaded${counters.downloads_failed ? `, ${counters.downloads_failed} failed` : ""}`);
  }
  if (counters.errors) parts.push(`${counters.errors} error${counters.errors === 1 ? "" : "s"}`);
  if (counters.parse_failed) parts.push(`${counters.parse_failed} unreadable page(s)`);
  if (counters.downloads_throttled) parts.push(`${counters.downloads_throttled} rate limit(s) waited out`);
  // Only while something will actually ask it. A source that is off is not
  // serving out a wait; the deadline is just what it last said.
  if (source.enabled && source.cooldown_seconds > 0) parts.push(`waiting ${source.cooldown_seconds}s`);
  return parts.join(" · ");
}

/**
 * The state word for a source, in the user's terms rather than the store's.
 *
 * Absent when this source has never been asked for anything: the description
 * beside it already says so, and repeating it here put the same sentence on the
 * row twice.
 */
function sourceStateLabel(source: ProviderSourceStatus): string | undefined {
  if (!source.enabled) return "Off";
  if (source.cooldown_seconds > 0) return "Waiting";
  // The last attempt of any kind, search or download: a failed download writes
  // this state too, so a later successful one now clears it rather than leaving
  // a source reporting a finished download, no error, and a failed last
  // attempt at the same time.
  if (source.state === "error") return "Last attempt failed";
  if (source.state === "ready") return "Working";
  return undefined;
}

// The frontend can outlive a reload of an older backend, so a readiness result
// that predates the directory inventory reports no directories rather than
// taking the whole screen down with it.
function managedDirectories(removal: RemovalReadiness): RemovalReadiness["directories"] {
  return removal.directories ?? [];
}

/**
 * What each deletion scope actually removes, in the user's terms.
 *
 * The confirmation has to name the consequence rather than the directories,
 * because the directories are what the list below already shows and the
 * consequence is what the press cannot be taken back from.
 */
const DELETION_SCOPES: ReadonlyArray<{
  scope: ManagedDataScope;
  label: string;
  keys: readonly string[];
  consequence: string;
}> = [
  {
    scope: "cache",
    label: "Search cache, staging and logs",
    keys: ["cache", "tmp", "logs"],
    consequence: "Nothing you set up is lost. Table search rebuilds its index the next time you use it.",
  },
  {
    scope: "setup",
    label: "That, plus every game's saved setup",
    keys: ["cache", "tmp", "logs", "state"],
    consequence: "Every game's selected table, target process, authorization, pinned and remembered cheats are forgotten, every table source is switched back on, and every table you marked as not working is offered again. Your tables and the installed Cheat Engine are kept, and CE Decky still knows which Cheat Engine is registered; choose one again from Stored on the home panel, which needs no network.",
  },
  {
    scope: "all",
    label: "Everything, including tables and Cheat Engine",
    keys: ["cache", "tmp", "logs", "settings", "state", "tables", "ce"],
    consequence: "CE Decky returns to its first-run state. Cheat Engine has to be downloaded again and every imported table is gone.",
  },
];

const supportPathStyle: CSSProperties = {
  margin: "8px 0",
  padding: "6px 8px",
  borderRadius: 4,
  background: "rgba(0, 0, 0, 0.35)",
  fontFamily: "monospace",
  fontSize: 13,
  wordBreak: "break-all",
};

const supportNoteStyle: CSSProperties = {
  marginTop: 8,
  fontSize: 12,
  color: "hsla(0, 0%, 100%, 0.6)",
};

/**
 * The bundle members that are genuinely absent.
 *
 * Named by what they are rather than by what they are not: this was written as
 * everything that is not `truncated`, so `partial`, added to the collector
 * afterwards, was counted as an item that could not be collected. One of those
 * fires on every bundle this device produces, so every healthy archive reported
 * a missing file. Only `omitted` means the archive does not carry something it
 * could have: `absent` is nothing to collect, which a device that has never
 * launched Cheat Engine and a plugin that has just been reloaded both are, and
 * counting those made a healthy bundle report two failures.
 */
function omittedNotes(bundle: SupportBundleResult): SupportBundleResult["notes"] {
  return bundle.notes.filter((note) => note.kind === "omitted");
}

/** Members the archive carries with less in them than the whole of the source. */
function shortenedNotes(bundle: SupportBundleResult): SupportBundleResult["notes"] {
  return bundle.notes.filter((note) => note.kind === "truncated" || note.kind === "partial");
}

function totalManagedFiles(removal: RemovalReadiness): number {
  return managedDirectories(removal).reduce((total, directory) => total + directory.file_count, 0);
}

function totalManagedBytes(removal: RemovalReadiness): number {
  return managedDirectories(removal).reduce((total, directory) => total + directory.total_bytes, 0);
}

/**
 * The screens that replace this panel, named by the press that opens each.
 *
 * Every one of them returns in place of the panel rather than over it, so
 * leaving one rebuilds every row behind it and the ring would open at the top
 * of a screen the reader had scrolled well past. Which press opened the screen
 * is what says where to put the ring back, the same rule the table-code window
 * already follows for its own sections.
 */
type SubScreen = "sources" | "blocked" | "removal" | "code" | "debug";

export function AdvancedModal(props: Props) {
  useUiSurface("AdvancedModal");
  const {
    status, games, selectedGame, appDetails, inspection, targetProcess, ceLaunch, launchProtonToolId,
    blockedTables: blockedTablesProp = [], blockedTablesReason: blockedTablesReasonProp = null,
    onRefreshBlockedTables, onUnblockTable, onClearBlockedTables,
    onLoadProviderSources, onSetProviderEnabled, onResetProviderSources, onResetProviderDiagnostics,
    onLoadDiagnostics, onCollectSupportBundle,
    runtime, selfTest, busy, onRefreshGames, onSaveTargetProcess,
    onPickCE, onClearCEImport, onRunSelfTest, onLaunchProtonChange, onRunCELaunchSelfTest, onRefreshRuntime,
    onRefreshProcesses, onRetryAttach, onRepairSessionState, onRepairOwnedLaunchState, onRepairProfileState, onClearStartup, onRevokeConsent, onCheckRemoval, onDeleteManagedData, onRefreshAll, onClose,
    targetHarness,
  } = props;
  const [gamesView, setGamesView] = useState(games);
  const [selectedGameView] = useState(selectedGame);
  const [appDetailsView, setAppDetailsView] = useState(appDetails);
  const [inspectionState, setInspectionState] = useState(inspection);
  const [targetDraft, setTargetDraft] = useState(targetProcess);
  const [protonDraft, setProtonDraft] = useState(launchProtonToolId);
  const [statusView, setStatusView] = useState(status);
  const [ceLaunchView, setCELaunchView] = useState(ceLaunch);
  const [runtimeView, setRuntimeView] = useState(runtime);
  const [selfTestView, setSelfTestView] = useState(selfTest);
  // Three states rather than the backend's two: `ok` means no blocker, and the
  // checks that answer whether a later bug report will have any evidence in it
  // are deliberately not blockers. Rendering `ok` alone said PASS while one of
  // them had failed and showed its reason nowhere.
  const selfTestSummaryView = selfTestView ? selfTestSummary(selfTestView) : null;
  const [attachCandidate, setAttachCandidate] = useState("");
  // A basename chosen from the game's own observed processes, for when there
  // is no Cheat Engine to ask for exact PIDs.
  const [observedTargetDraft, setObservedTargetDraft] = useState("");
  // Whether the observed-process fallback has been asked for. It stays hidden
  // until then, because on a healthy session the exact-PID list is the answer
  // and two process lists at once is one more than anyone needs.
  const [observedTargetsShown, setObservedTargetsShown] = useState(false);
  const [startupCount, setStartupCount] = useState<number | null>(null);
  const [removal, setRemoval] = useState<RemovalReadiness | null>(null);
  const [removalOpen, setRemovalOpen] = useState(false);
  // Deleting plugin data is irreversible, so the scope is chosen and confirmed
  // on a screen of its own rather than behind a single press in the header.
  const [deleteScope, setDeleteScope] = useState<ManagedDataScope | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleted, setDeleted] = useState<ManagedDataDeletion | null>(null);
  const [debug, setDebug] = useState<DiagnosticsSnapshot | null>(null);
  const [debugOpen, setDebugOpen] = useState(false);
  const [debugError, setDebugError] = useState<string | null>(null);
  // The last archive written in this session, and why one could not be.
  // Collecting is read-only, so a failure here is reported in place rather than
  // as a toast over a screen the user is about to keep reading.
  const [supportBundle, setSupportBundle] = useState<SupportBundleResult | null>(null);
  const [supportBundleError, setSupportBundleError] = useState<string | null>(null);
  const [localBusy, setLocalBusy] = useState(false);
  const localBusyRef = useRef(false);
  // Which table sources are on, and what each one last did. Loaded when this
  // screen opens rather than passed in, because nothing on the panel needs it
  // and reading it on every panel refresh would be a file read per poll.
  const [providerSources, setProviderSources] = useState<ProviderSourcesSnapshot | null>(null);
  const [providerSourcesError, setProviderSourcesError] = useState<string | null>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  // The same generation guard the blocked-table list uses: two quick switches
  // must not leave the screen showing whichever read happened to land last.
  const providerSourcesRef = useRef(0);
  // Which re-read of the blocked-table record is the current one, so an
  // earlier one that lands later cannot repaint entries already cleared.
  const blockedReloadRef = useRef(0);
  const blocked = busy || localBusy;

  const reconcileProtonDraft = (nextLaunch: CELaunchCapability | null) => {
    setProtonDraft((current) => nextLaunch?.observed_proton_tool?.tool_id
      ?? (nextLaunch?.proton_tools.some((tool) => tool.tool_id === current) ? current : nextLaunch?.proton_tools[0]?.tool_id ?? ""));
  };

  const reconcileContext = (next: AdvancedContextSnapshot) => {
    setStatusView(next.status);
    setCELaunchView(next.ceLaunch);
    reconcileProtonDraft(next.ceLaunch);
    setRuntimeView(next.runtime);
    setAppDetailsView(next.appDetails);
    setInspectionState(next.inspection);
    setTargetDraft(next.targetProcess);
    setAttachCandidate("");
    setObservedTargetDraft("");
    setStartupCount(null);
    setRemoval(null);
  };

  // Like every other view here, this renders its own copy: the modal is opened
  // with a snapshot of the parent's props and never re-rendered from them, so
  // without this a cleared entry stayed on screen until Advanced was reopened.
  const [blockedTables, setBlockedTables] = useState<BlockedTable[]>(blockedTablesProp);
  const [blockedTablesReason, setBlockedTablesReason] = useState<string | null>(blockedTablesReasonProp);
  // The record holds up to 512 entries, so it gets a screen of its own rather
  // than a column on the panel that everything below it has to be scrolled
  // past. Pressing Show more extends the page; the list is newest first, which
  // is the half anyone is looking for.
  const [blockedOpen, setBlockedOpen] = useState(false);
  // The code view is a sub-screen of this modal rather than a modal over it, the
  // same way the blocked-table list is: Decky's focus stack is steadier with one
  // root, and this one is opened from a row deep inside a scrolled panel.
  const [codeOpen, setCodeOpen] = useState(false);
  const [blockedPages, setBlockedPages] = useState(1);
  const returnFocusRef = useRef<SubScreen | null>(null);
  const subScreenOpenerRefs = useRef(new Map<SubScreen, HTMLDivElement>());
  const openerRef = (screen: SubScreen) => (node: HTMLDivElement | null) => {
    if (node) subScreenOpenerRefs.current.set(screen, node);
    else subScreenOpenerRefs.current.delete(screen);
  };
  // Recorded where the screen is actually opened rather than at the press, so a
  // press that fails to open one leaves nothing behind to be spent on the next.
  const openSubScreen = (screen: SubScreen, open: () => void) => {
    returnFocusRef.current = screen;
    open();
  };
  const subScreenOpen = removalOpen || sourcesOpen || blockedOpen || codeOpen || debugOpen;
  useEffect(() => {
    if (subScreenOpen) return;
    const screen = returnFocusRef.current;
    if (!screen) return;
    returnFocusRef.current = null;
    const holder = subScreenOpenerRefs.current.get(screen);
    if (holder) focusFirstEnabled({ current: holder });
  }, [subScreenOpen]);
  const shownBlockedTables = useMemo(
    () => blockedTables.slice(0, blockedPages * BLOCKED_TABLES_PAGE),
    [blockedTables, blockedPages],
  );
  // The day of the newest record, which is what dates the whole summary.
  /**
   * What this device can call each AppID, for the screens that show numbers.
   *
   * The Steam library first, because it is current and it is already here. The
   * not-working records second, and only to fill a gap: they carry the name a
   * game had when the mark was written, which is what still answers for a game
   * that has since been uninstalled, and which must not outrank a library entry
   * that has been renamed since.
   */
  const gameNames = useMemo(() => {
    const names = new Map<number, string>();
    for (const entry of blockedTables) {
      if (typeof entry.app_id === "number" && entry.game_name) names.set(entry.app_id, entry.game_name);
    }
    for (const game of gamesView) names.set(game.appId, game.name);
    return names;
  }, [gamesView, blockedTables]);
  const newestBlockedAt = blockedTables.length > 0 ? blockedRecordedOn(blockedTables[0].recorded_at) : null;
  // Empty because it could not be read is not empty, and the difference is
  // whether a table already known not to work is about to be offered and
  // imported again. Both states are named on the panel, not only inside.
  const blockedSummaryLabel = blockedTablesReason
    ? "Record cannot be read"
    : blockedTables.length === 0
      ? "No tables marked"
      : `${blockedTables.length} table${blockedTables.length === 1 ? "" : "s"} marked`;
  // What this record holds, said once, so an empty screen and a populated one
  // cannot describe different lists. Four causes are stored, and the one that
  // used to go missing before the first entry existed is the locked archive,
  // which is kept apart from bytes that are not a table because the user can
  // act on it.
  //
  // They are named in the help rather than on the row. On the device this
  // heading was six lines of prose above three entries, which is a screen
  // explaining itself instead of showing what it holds, and the height it took
  // is height the entries do not get. A record with entries in it describes
  // itself: each one carries the sentence it was recorded with, and the row
  // above them keeps only what cannot be read off them - how many, how recent,
  // and what a press does. A record with nothing in it has no entries to say
  // any of that, so that is the one state that still needs a line of its own.
  const BLOCKED_CAUSES = "a table that ran and did not work, a download that was not a usable table or an archive nothing here can open, or a provider row whose file the source no longer has";
  const blockedSummaryDescription = blockedTablesReason
    ? `${blockedTablesReason} Nothing is being refused while this cannot be read; Clear all replaces it with an empty record.`
    : blockedTables.length === 0
      ? "Nothing is recorded here yet."
      : undefined;
  const blockedSummaryHelp = `Each entry is something not to try again until it is cleared, and what it holds is ${BLOCKED_CAUSES}. A download or a source condition never refuses a copy already on this device. CE Decky records a table by its exact contents when Cheat Engine runs it and it comes straight back off, when a download turns out not to be a usable table at all, and when its archive is one only 7-Zip opens and every file inside it is locked, which is kept apart because re-packing such a download is something the user can act on. A row whose file the source says it no longer has is recorded too, by that row rather than by contents, because nothing was ever downloaded to key it on. A cheat table finds the game's code by scanning for patterns, so the first case is almost always a table written for a different build of the game - which is why the game's version is kept beside it. Clearing an entry removes that refusal and nothing else: a table it marked can be chosen and imported again, while what is already known about it, including a success an entry invalidated, stays as it was until a cheat proves the table again.`;
  const blockedManageable = (blockedTables.length > 0 || Boolean(blockedTablesReason))
    && Boolean(onClearBlockedTables || onUnblockTable);
  /**
   * Re-read the record after an edit, without ever rejecting or repainting a
   * newer answer with an older one.
   *
   * This is called from a commit's success path, where the durable edit has
   * already happened: a refresh that fails must not take that press down with
   * it, and two quick edits must not leave the screen showing whichever read
   * happened to land last. It resolves rather than rejects for the same reason.
   */
  const reloadBlockedTables = async () => {
    if (!onRefreshBlockedTables) return;
    const generation = blockedReloadRef.current + 1;
    blockedReloadRef.current = generation;
    try {
      const listed = await onRefreshBlockedTables();
      if (blockedReloadRef.current !== generation) return;
      setBlockedTables(listed.tables);
      setBlockedTablesReason(listed.reason);
    } catch (cause) {
      if (blockedReloadRef.current !== generation) return;
      logUiFailure("advanced.blocked_tables_refresh", cause);
      setBlockedTablesReason("This list could not be re-read just now, so it may be out of date. Refresh before deciding what to do next.");
    }
  };
  // The prop is whatever the panel last read, which can be older than this
  // screen: a refresh started as Advanced opened would land after the snapshot
  // was taken and never reach it.
  useEffect(() => { void reloadBlockedTables().catch(() => undefined); }, []);

  /**
   * Re-read which sources are on, without ever repainting a newer answer with
   * an older one.
   *
   * Called from a switch's success path, where the durable write has already
   * happened, so it resolves rather than rejects: a refresh that fails must not
   * take a saved change down with it.
   */
  const reloadProviderSources = async (next?: ProviderSourcesSnapshot | null) => {
    const generation = providerSourcesRef.current + 1;
    providerSourcesRef.current = generation;
    if (next) {
      setProviderSources(next);
      setProviderSourcesError(null);
      return;
    }
    if (!onLoadProviderSources) return;
    try {
      const snapshot = await onLoadProviderSources();
      if (providerSourcesRef.current !== generation) return;
      setProviderSources(snapshot);
      setProviderSourcesError(null);
    } catch (cause) {
      if (providerSourcesRef.current !== generation) return;
      logUiFailure("advanced.provider_sources_refresh", cause);
      setProviderSourcesError(describeError(cause));
    }
  };
  useEffect(() => { void reloadProviderSources().catch(() => undefined); }, []);

  // One line for the panel: how many sources are on, and what they have done
  // between them. The totals are the reason the switch is worth offering at
  // all - a source with hundreds of errors and no results is the one a user
  // wants to stop waiting for.
  const sourceTotals = useMemo(() => {
    const rows = providerSources?.sources ?? [];
    return rows.reduce(
      (total, source) => {
        const counters = source.counters;
        if (!counters) return total;
        return {
          measured: total.measured + 1,
          searches: total.searches + counters.searches,
          results: total.results + counters.results,
          downloads: total.downloads + counters.downloads_succeeded,
          errors: total.errors + counters.errors,
        };
      },
      { measured: 0, searches: 0, results: 0, downloads: 0, errors: 0 },
    );
  }, [providerSources]);
  const sourcesSummaryLabel = providerSourcesError && !providerSources
    // Only while there is nothing to show. Once a read has succeeded, this
    // same field also carries the failure of a switch, and labelling that as
    // an unreadable list contradicted the rows sitting under it.
    ? "Sources could not be read"
    : providerSources?.selection_reason
      // Not "5 of 5 sources on": that is what is happening, but stating it as
      // the user's own choice would hide that their choice is the thing that
      // was lost.
      ? "Your choice of sources cannot be read"
      : providerSources
        ? `${providerSources.enabled_count} of ${providerSources.total} sources on`
        : "Table sources";
  const sourcesSummaryDescription = providerSourcesError
    ? providerSourcesError
    : providerSources?.selection_reason
      ? `${providerSources.selection_reason} Every source is being searched while this cannot be read; Switch all on replaces the record.`
      : providerSources?.diagnostics_reason
        // Never a row of zeroes here. Nothing was measured, and totals of zero
        // state as fact that nothing has happened, which is a different and
        // much more misleading claim than saying the record is unreadable.
        ? `What each source has done cannot be read: ${providerSources.diagnostics_reason}`
        : providerSources
          ? sourceTotals.measured === 0
            ? "No source has been searched yet."
            : `${sourceTotals.searches} search${sourceTotals.searches === 1 ? "" : "es"} · ${sourceTotals.results} result${sourceTotals.results === 1 ? "" : "s"} · ${sourceTotals.downloads} downloaded · ${sourceTotals.errors} error${sourceTotals.errors === 1 ? "" : "s"}`
          : "Reading which sources are on…";
  // A switch writes by reading the record first, so while that read fails every
  // one of them is guaranteed to fail too - and its error would replace the
  // explanation of the corruption with a generic one. Switch all on is the
  // action that works, because it replaces the record without reading it.
  const switchesBlocked = Boolean(providerSources?.selection_reason);
  const sourcesSummaryHelp = "The sites CE Decky searches for cheat tables. All of them are on to begin with, and switching one off stops it completely: it is not searched, it is not read when another source's page points at it, and rows it already left in the search cache are no longer offered. Nothing you have already downloaded is affected. The counts beside each source are what it has actually done on this device, which is what makes the choice worth making: a source that never answers is only costing you the wait.";

  const invoke = async <T,>(
    action: () => Promise<T> | T,
    onSuccess?: (value: T) => void,
    onFailure?: (cause: unknown) => void,
  ) => {
    if (busy || localBusyRef.current) return;
    localBusyRef.current = true;
    setLocalBusy(true);
    const operation = startUiOperation("advanced.invoke", { callback: action.name || "inline" });
    try {
      const value = await action();
      onSuccess?.(value);
      operation.completed();
    } catch (cause) {
      operation.failed(cause);
      // Parent actions already surface a CE Decky toast; contain the rejected
      // event promise here. A caller that is not routed through the parent's
      // error path says so by passing its own handler.
      onFailure?.(cause);
    } finally {
      localBusyRef.current = false;
      setLocalBusy(false);
    }
  };

  const repair = (write: () => Promise<unknown>) => invoke(async () => {
    try {
      await write();
    } finally {
      // A repair can publish its change before reporting a durability error.
      // This detached screen must adopt the readback on either outcome.
      try { reconcileContext(await onRefreshAll()); }
      catch (cause) { logUiFailure("advanced.status_after_repair_failed", cause); }
    }
  });

  /**
   * Perform a deletion the user has now confirmed twice: once by choosing the
   * scope, once in Steam's own dialog. Failure keeps the choice on screen with
   * the reason, because nothing was removed in that case.
   */
  const performDelete = (scope: ManagedDataScope) => invoke(
    () => onDeleteManagedData(scope),
    (result) => {
      // Best effort on the backend, because a readiness report that could not
      // be produced after the files went is not a deletion that did not happen.
      if (result.readiness) setRemoval(result.readiness);
      setDeleted(result);
      setDeleteScope(null);
      // Everything else on screen was describing data that may now be gone:
      // the selected table, the registered Cheat Engine, the game's own
      // profile. Catching up is best-effort - the deletion already happened.
      void Promise.resolve().then(onRefreshAll).then(reconcileContext).catch((cause) => logUiFailure("advanced.delete_refresh_failed", cause));
      // These two are not part of that context and were left saying what the
      // deleted files used to hold: a scope that removes `state/` switches
      // every source back on and erases every blocked-table mark, so the rows
      // above would have gone on reporting "all off" and a record of tables
      // this no longer refuses.
      void reloadProviderSources().catch(() => undefined);
      void reloadBlockedTables().catch(() => undefined);
    },
    (cause) => {
      setDeleteError(`${describeError(cause)} Anything already removed stays removed; the rows above have been re-read.`);
      // The deletion commits before the call returns, so a failure after that
      // point is a report that did not arrive rather than files that are still
      // there. Refreshing on this path too is what stops the screen going on
      // describing state that is gone.
      // Wrapped, because catching up is best effort on a path that is already
      // reporting a failure: a refresh that rejects, or a host that hands back
      // no promise at all, must not become a second error on top of the one the
      // user is being shown.
      void Promise.resolve().then(onRefreshAll).then(reconcileContext).catch((cause) => logUiFailure("advanced.delete_refresh_failed", cause));
      void reloadProviderSources();
      void reloadBlockedTables();
      void Promise.resolve().then(onCheckRemoval).then((next) => { if (next) setRemoval(next); })
        .catch((cause) => logUiFailure("advanced.removal_refresh_failed", cause));
    },
  );

  /**
   * Write one support archive and then say, in one dialog, exactly where it is.
   *
   * The path is the whole point of the press: a user in Game Mode has to find
   * this file afterwards from a desktop session or a file transfer, and a toast
   * that scrolls away with the path in it is the same as no path at all. So the
   * result is a dialog that has to be dismissed, with one button, and the path
   * on a line of its own.
   */
  const collectSupportBundle = () => invoke(
    onCollectSupportBundle,
    (bundle) => {
      setSupportBundle(bundle);
      // A log that is in the archive and readable is not a missing member,
      // whether it was trimmed to a budget or holds less of a window than was
      // offered. Only a member that is not there at all is something the reader
      // has to be told about, and this counted by exclusion until a third kind
      // arrived and every healthy bundle started reporting a problem.
      const missing = omittedNotes(bundle).length;
      const confirmation = showModal(
        <ConfirmModal
          bAlertDialog
          strTitle="Support bundle saved"
          strOKButtonText="OK"
          strDescription={(
            <div>
              <div>Attach this file to a GitHub issue and describe what happened.</div>
              <div style={supportPathStyle}>{bundle.path}</div>
              <div>
                {`${formatBytes(bundle.size_bytes)} · ${bundle.member_count} file(s)`}
                {missing > 0 ? ` · ${missing} item(s) could not be collected; the archive lists them in manifest.json.` : ""}
              </div>
              <div style={supportNoteStyle}>
                It holds CE Decky's logs, settings, game profiles and the cheat tables in use. It holds no password and no Cheat Engine or game files. Remove anything you would rather not publish before attaching it.
              </div>
            </div>
          )}
          onOK={traceUiAction("advanced_modal.support_bundle_saved", () => confirmation.Close())}
          onCancel={traceUiAction("advanced_modal.support_bundle_dismiss", () => confirmation.Close())}
        />,
      );
    },
    (cause) => setSupportBundleError(describeError(cause)),
  );

  const loadDebug = () => {
    setDebugError(null);
    // Diagnostics are read directly rather than through the parent's action
    // wrapper, so nothing else reports this failure. Without the handler the
    // screen sat empty - not loading, no error, no snapshot - exactly when the
    // backend could not produce diagnostics.
    void invoke(onLoadDiagnostics, setDebug, (cause) => setDebugError(describeError(cause)));
  };

  const openDebug = () => {
    setDebugOpen(true);
    loadDebug();
  };

  const close = () => {
    if (!busy && !localBusyRef.current) onClose();
  };

  /**
   * Leave this screen, then navigate Steam to the page.
   *
   * `NavigateToExternalWeb` navigates the Steam UI underneath; a Decky modal
   * stays mounted above it, so the browser opens with its own chrome visible
   * around this dialog and its page never reachable. Dismissing first is what
   * makes the page the thing on screen.
   */
  const openSourcePage = (url: string) => {
    if (busy || localBusyRef.current) return;
    onClose();
    openExternalWeb(url);
  };

  const profile = selectedGameView
    ? statusView.profiles.find((candidate) => candidate.app_id === selectedGameView.appId && candidate.is_shortcut === selectedGameView.isShortcut) ?? null
    : null;
  const inspectionView = inspectionState && profile?.table_sha256 === inspectionState.sha256 ? inspectionState : null;
  const runtimeSessionReady = Boolean(
    selectedGameView
    && profile?.table_sha256
    && isExactRuntimeSession(runtimeView, selectedGameView.appId, profile.table_sha256),
  );
  // Why the exact-PID list is not the answer right now.
  //
  // Processes exists to recover from an attachment that chose the wrong
  // program, and that is very often the same situation in which Cheat Engine
  // will not start or will not stay attached - so a button that only worked
  // with a healthy connected session was disabled in exactly the case it was
  // written for. It is now always pressable for a selected game: with a session
  // it asks Cheat Engine, which is the only thing that can supply an exact
  // Windows PID, and without one it falls back to the game's own processes as
  // observed on this machine, which names a target for the next start.
  const exactPidUnavailableReason = runtimeSessionReady || !selectedGameView
    ? null
    : !profile?.table_sha256
      ? "This game has no active table, so no Cheat Engine can be asked for exact PIDs."
      : !runtimeView?.prepared
        ? "No session has been prepared for this table yet, so no Cheat Engine can be asked for exact PIDs."
        : !runtimeView.connected
          ? "Cheat Engine is not running for this game, so no exact PID can be offered."
          : runtimeView.session_stale_reason
            ?? "The running session belongs to a different game or table than the one selected here.";
  // The game's own Windows executables, read from this machine's process table
  // rather than from Cheat Engine. No PID is offered with them: these are
  // Linux-side observations of the game's processes, and only Cheat Engine can
  // name a Windows PID. Choosing one sets the target the next start attaches to.
  const observedTargets = useMemo(() => {
    const observed = ceLaunchView?.game?.windows_executables ?? [];
    const valid = observed.filter((name) => isValidProcessBasename(name));
    const unique = [...new Set(valid)];
    return unique
      .map((name) => ({ name, runtimeNoise: isWineRuntimeExecutable(name) }))
      .sort((left, right) =>
        Number(left.runtimeNoise) - Number(right.runtimeNoise)
        || (left.name.toLowerCase() < right.name.toLowerCase() ? -1 : 1));
  }, [ceLaunchView?.game?.windows_executables]);
  const selectedObservedTarget = observedTargets.find((candidate) => candidate.name === observedTargetDraft) ?? null;
  const runtimeProcessOptions = useMemo(
    () => runtimeAttachCandidates(runtimeView?.status?.processes ?? [])
      .flatMap((candidate) => candidate.pids.map((pid) => ({
        name: candidate.name, pid, runtimeNoise: Boolean(candidate.runtimeNoise),
      }))),
    [runtimeView?.status?.processes],
  );
  const selectedAttach = runtimeProcessOptions.find((candidate) => `${candidate.pid}:${candidate.name}` === attachCandidate) ?? null;
  const liveTargetOverride = divergentLiveTarget(runtimeView, profile?.target_process);
  // What this game is actually running, when the process saved for it is not
  // among that. `null` wherever the absence cannot be proved.
  const absentTargetCandidates = absentLiveTarget(ceLaunchView?.game ?? null, profile?.target_process);
  // Offered only where there is one answer. With several, the press would be
  // picking for the user out of a list this cannot rank, and what it writes
  // stops the Cheat Engine running now; the field above and Processes below are
  // the route for that, and the row names the candidates either way.
  const absentTargetAction = absentTargetCandidates?.length === 1 ? (
    <SmallButton
      disabled={blocked || !profile?.table_sha256}
      onClick={traceUiAction("advanced_modal.use_running_target", () => { void invoke(() => onSaveTargetProcess(absentTargetCandidates[0])); }, { process: absentTargetCandidates[0] })}
    >{`Use ${absentTargetCandidates[0]}`}</SmallButton>
  ) : undefined;
  // Both of these are built here rather than inside the row's props. A prop
  // whose value spans lines is emitted into the tracked bundle with the line
  // broken after the prop before it, which leaves that line ending in a space
  // and `git diff --check` refusing the bundle - the same reason a comment
  // between props is refused.
  const absentTargetDetail = absentTargetCandidates === null
    ? ""
    : absentTargetCandidates.length === 1
      ? `The game is running and started ${absentTargetCandidates[0]}. Saving that uses it from the next start.`
      : `The game is running and started ${absentTargetCandidates.join(", ")}. Set the one this table is for in Target process above, or pick it under Processes.`;
  // The bridge loads the session's exact table itself, because Cheat Engine only
  // opens one named on its command line once its main window is shown. Saying
  // which of the two happened is the difference between "this table is not
  // supported" and "this session never got the table".
  const tableLoad = runtimeView?.status?.table_load_state ?? null;
  const tableLoadDetail = tableLoad === null || tableLoad === "loaded"
    ? ""
    : tableLoad === "pending"
      ? " \u00b7 table not loaded yet"
      : ` \u00b7 table could not be opened${runtimeView?.status?.table_load_error ? `: ${runtimeView.status.table_load_error}` : ""}`;
  // Import, Forget and the self-test change or occupy the global Cheat Engine
  // registration, which the backend refuses while *any* game owns a live one -
  // this one included. Filtering the selected game out of that answer is what
  // left those buttons enabled beside actions that silently stop CE instead.
  const ownership = launchOwnership({
    capability: ceLaunchView,
    scopeAppId: selectedGameView?.appId ?? null,
    selectedAppId: selectedGameView?.appId ?? null,
    nameOf: (appId) => games.find((candidate) => candidate.appId === appId)?.name ?? null,
  });
  const ceIdentityBlockedReason = ceLaunchView ? ownership.identityBlockedReason : null;
  const invalidSelectedOwnership = (ceLaunchView?.owned_launch_owners ?? []).some(
    (owner) => owner.app_id === selectedGameView?.appId && owner.state === "invalid",
  );
  const targetDraftValid = isValidProcessBasename(targetDraft.trim());
  const observedProton = ceLaunchView?.observed_proton_tool ?? null;
  const compatState = ceLaunchView?.compat_data?.state ?? "unknown";
  const compatPath = ceLaunchView?.compat_data?.compat_data_path ?? ceLaunchView?.game?.compat_data_path ?? null;
  const prefixConflicts = ceLaunchView?.game?.conflicting_wine_prefixes ?? [];
  // The catalog entry behind the profile's exact table SHA: filename, size,
  // origin and availability live there rather than in the parsed inspection.
  const activeTable = profile?.table_sha256
    ? statusView.tables.find((table) => table.sha256 === profile.table_sha256) ?? null
    : null;
  const tableOrigin = activeTable?.origins[activeTable.origins.length - 1] ?? null;
  // A table published for another store's build commonly differs from the
  // running executable in letter case alone, which reads as a contradiction
  // between two rows unless it is named as the ordinary thing it is.
  const hintCaseOnlyMismatch = Boolean(
    profile?.target_process
    && inspectionState?.process_candidates.some((hint) =>
      hint !== profile.target_process && hint.toLowerCase() === profile.target_process!.toLowerCase()),
  );
  // Only an https page is offered; the origin record is provider data.
  const originOpenable = /^https:\/\//i.test(tableOrigin?.source_page ?? "");

  if (removalOpen && removal && deleteScope !== null) {
    const chosen = DELETION_SCOPES.find((item) => item.scope === deleteScope) ?? DELETION_SCOPES[0];
    const affected = managedDirectories(removal).filter((directory) => chosen.keys.includes(directory.key));
    const files = affected.reduce((total, directory) => total + directory.file_count, 0);
    const bytes = affected.reduce((total, directory) => total + directory.total_bytes, 0);
    return (
      <ModalRoot onCancel={traceUiAction("advanced_modal.delete.cancel_back", () => { if (!busy && !localBusyRef.current) setDeleteScope(null); })}>
        <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
          <DensePanel>
            <PanelSection>
              <SectionHeading>Delete plugin data</SectionHeading>
              <PanelSectionRow>
                <DropdownItem
                  label="What to delete"
                  rgOptions={DELETION_SCOPES.map((item) => ({ data: item.scope, label: item.label }))}
                  selectedOption={deleteScope}
                  onChange={traceUiAction("advanced_modal.what_to_delete", (option) => { setDeleteError(null); setDeleteScope(String(option.data) as ManagedDataScope); }, (option) => ({ scope: String(option.data) }))}
                  disabled={localBusy}
                />
              </PanelSectionRow>
              {/* What this press removes, and what that costs behind a
                  question mark. The consequence is two or three lines of prose
                  read once, and this window has to fit a directory row for
                  every place the chosen scope touches: on a Steam Deck the
                  widest scope did not. The number is never a guess - it comes
                  from the same report the previous screen showed. */}
              <PanelRow
                testId="delete-scope-summary"
                tone="header"
                truncate
                label={`${files} file(s) · ${formatBytes(bytes)}`}
                help={`${chosen.consequence} Everything listed below is under ${removal.managed_root}.`}
              />
              {/* One line each. The widest scope touches seven of these, and a
                  row carrying its path under its name is two lines, which is
                  fourteen on a window that has a dropdown, a summary and two
                  presses to fit as well: on a Steam Deck that window ran past
                  the bottom of the display. The paths are all one directory
                  under the same managed root, and that root is named once,
                  above, where it is read once rather than seven times. */}
              {affected.map((directory) => (
                <PanelRow
                  key={directory.key}
                  truncate
                  testId={`delete-directory-${directory.key}`}
                  label={directory.label}
                  trailing={directory.exists ? `${directory.file_count} · ${formatBytes(directory.total_bytes)}` : "empty"}
                />
              ))}
              {deleteError && <PanelRow testId="delete-error" label="Nothing was deleted" description={deleteError} />}
            </PanelSection>
          </DensePanel>
          {/* One action group rather than two siblings in a plain box. Steam
              derives which control is beside which from the geometry, so two
              buttons in a box are two separate steps and the stick moved down
              from one to the other rather than across, on the one screen here
              whose two presses are "destroy this" and "do not". */}
          <ModalActions>
            {/* Choosing a scope is not consenting to it. The press that
                actually deletes asks once more, in Steam's own controller
                dialog, and names the consequence rather than the scope - a
                confirmation that only repeats the button is not one. */}
            <DestructiveAction
              disabled={localBusy}
              onClick={traceUiAction("advanced_modal.delete_these", () => {
                const confirm = showModal(
                  <ConfirmModal
                    strTitle="Delete this plugin data?"
                    strDescription={`${chosen.label}. ${chosen.consequence} This cannot be undone.`}
                    strOKButtonText="Delete"
                    strCancelButtonText="Keep it"
                    onCancel={traceUiAction("advanced_modal.delete.keep", () => confirm.Close(), { scope: deleteScope })}
                    onOK={traceUiAction("advanced_modal.delete.confirm", () => {
                      confirm.Close();
                      void performDelete(deleteScope);
                    }, { scope: deleteScope })}
                  />,
                );
              }, { scope: deleteScope })}
            >Delete these</DestructiveAction>
            <DialogButton style={modalActionStyle} disabled={localBusy} onClick={traceUiAction("advanced_modal.cancel", () => { if (!busy && !localBusyRef.current) setDeleteScope(null); })}>Cancel</DialogButton>
          </ModalActions>
        </Focusable>
      </ModalRoot>
    );
  }

  if (removalOpen && removal) {
    return (
      <ModalRoot onCancel={traceUiAction("advanced_modal.removal.back", () => { if (!busy && !localBusyRef.current) setRemovalOpen(false); })}>
        <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
          <DensePanel>
            <PanelSection>
              <SectionHeading>Plugin data on disk</SectionHeading>
              {/* The list's own first row of data heads it, the same way the
                  other screens that start with a summary do: the verdict and
                  everything qualifying it are one answer, and splitting them
                  across rows made the reason read like another directory in the
                  list below.

                  Only a running owned Cheat Engine forbids deletion. The other
                  things that make removal untidy - corrupt profile state,
                  ambiguous sessions - are reasons to delete, and gating on them
                  would trap the user in the state this escapes. */}
              <PanelRow
                tone="header"
                testId="removal-summary"
                label={removal.can_delete_managed_data ? "Safe to remove" : "Removal is blocked"}
                description={[
                  `${totalManagedFiles(removal)} file(s) · ${formatBytes(totalManagedBytes(removal))} · ${removal.profiles_total} profile(s)`,
                  ...removal.blockers,
                ].join(" · ")}
                actions={(
                  <SmallButton
                    disabled={blocked || removal.live_owned_launch}
                    onClick={traceUiAction("advanced_modal.delete", () => { setDeleteError(null); setDeleted(null); setDeleteScope("cache"); })}
                  >Delete…</SmallButton>
                )}
              />
              {deleted && (
                <PanelRow
                  testId="removal-deleted"
                  label={deleted.failed.length > 0 ? "Partly deleted" : "Deleted"}
                  description={[
                    `${deleted.deleted.reduce((total, item) => total + item.removed_files, 0)} file(s) · ${formatBytes(deleted.deleted.reduce((total, item) => total + item.removed_bytes, 0))} removed.`,
                    // Naming what survived is the point: the rest really was
                    // deleted, and saying nothing happened would be the same
                    // untruth the durable-write reconciliation exists to stop.
                    ...deleted.failed.map((item) => `${item.label} could not be cleared: ${item.error}`),
                  ].join(" ")}
                />
              )}
              {managedDirectories(removal).map((directory) => (
                <PanelRow
                  key={directory.key}
                  truncate
                  testId={`removal-directory-${directory.key}`}
                  label={directory.label}
                  description={directory.error ?? directory.path}
                  trailing={directory.exists
                    ? `${directory.file_count} · ${formatBytes(directory.total_bytes)}${directory.truncated ? "+" : ""}`
                    : "empty"}
                  help={`${directory.purpose} ${directory.path}`}
                />
              ))}
            </PanelSection>
          </DensePanel>
          {/* A screen that ends in a list carries a small way out, the same one
              every paged screen here ends with: a full-size button was a band
              of height this window does not have on a handheld. */}
          <div style={{ padding: "0 16px 6px" }}>
            <ActionGroup style={{ justifyContent: "flex-end", gap: 8 }}>
              <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.back", () => { if (!busy && !localBusyRef.current) setRemovalOpen(false); })}>Back</SmallButton>
            </ActionGroup>
          </div>
        </Focusable>
      </ModalRoot>
    );
  }

  /**
   * Which sites CE Decky may search, one row per source.
   *
   * A screen of its own rather than a column on the diagnostics list: each row
   * carries the evidence for the decision it offers - what that source has
   * searched, found, downloaded and failed on this device - and that does not
   * fit beside everything else. Switching one off is durable and reversible,
   * and takes effect on the next search rather than retroactively: tables
   * already downloaded from it are untouched.
   */
  if (sourcesOpen) {
    const rows = providerSources?.sources ?? [];
    const allOff = providerSources !== null && providerSources.enabled_count === 0 && rows.length > 0;
    return (
      <ModalRoot onCancel={traceUiAction("advanced_modal.sources.back", () => { if (!busy && !localBusyRef.current) setSourcesOpen(false); })}>
        <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
          <DensePanel>
            <PanelSection>
              <SectionHeading>Table sources</SectionHeading>
              {/* The list's own first row of data heads it: the totals for every
                  source, set a step darker than the panel and holding the
                  per-source rows off itself. */}
              <PanelRow
                tone="header"
                testId="provider-sources-summary"
                label={sourcesSummaryLabel}
                description={allOff && !providerSourcesError
                  ? "Every source is off, so a table search cannot find anything. Switch at least one back on."
                  : sourcesSummaryDescription}
                help={sourcesSummaryHelp}
                actions={onResetProviderSources ? (
                  <SmallButton
                    disabled={blocked}
                    onClick={traceUiAction("advanced_modal.switch_all_on", () => { void invoke(onResetProviderSources, (next) => { void reloadProviderSources(next); }, (cause) => setProviderSourcesError(describeError(cause))); })}
                  >
                    Switch all on
                  </SmallButton>
                ) : undefined}
              />
              {providerSources?.diagnostics_reason && (
                <PanelRow
                  truncate
                  testId="provider-sources-diagnostics-error"
                  label="Counts are unavailable"
                  description={`${providerSources.diagnostics_reason} Searching will not repair it; Reset counts replaces the record with an empty one.`}
                  help="The switches themselves still work. What could not be read is the separate record of what each source has done. Ordinary use does not fix it: every search and download loads that whole record before adding to it, and a load that fails is deliberately ignored so a broken counter can never fail a search. So it stays broken until it is replaced, which is what Reset counts does. Nothing else is affected, and the counts start again from the next search."
                  actions={onResetProviderDiagnostics ? (
                    <SmallButton
                      disabled={blocked}
                      onClick={traceUiAction("advanced_modal.reset_counts", () => { void invoke(onResetProviderDiagnostics, (next) => { void reloadProviderSources(next); }, (cause) => setProviderSourcesError(describeError(cause))); })}
                    >
                      Reset counts
                    </SmallButton>
                  ) : undefined}
                />
              )}
              {rows.map((source) => (
                <PanelRow
                  key={source.provider}
                  truncate
                  testId={`provider-source-${source.provider}`}
                  label={source.provider_display_name}
                  description={describeSource(source, Boolean(providerSources?.diagnostics_reason))}
                  trailing={sourceStateLabel(source)}
                  help={[
                    // Both halves come from the registry rather than from one
                    // of them standing for the other: only two of these sources
                    // are ever named by another source's page, and describing
                    // all five as if they were claimed a route three of them
                    // do not have.
                    source.discovery === "linked_source"
                      ? "This source is never searched on its own. It is read only when another source's page names an exact page on it, and switching it off stops that."
                      : source.linked_target
                        ? "Searched for every game, and also read when another source's page names an exact page on it. Switching it off stops both."
                        : "Searched for every game. Switching it off stops that; nothing else points at this source.",
                    source.enabled && source.cooldown_seconds > 0
                      ? "This source asked to be left alone for a while and searches honour that. A download you start yourself is not held by it, so a table can still be fetched from this source meanwhile."
                      : null,
                    source.last_error ? `Last error: ${source.last_error}` : null,
                    source.last_http_status ? `Last HTTP status ${source.last_http_status}.` : null,
                  ].filter(Boolean).join(" ")}
                  actions={onSetProviderEnabled ? (
                    <SmallButton
                      disabled={blocked || switchesBlocked}
                      onClick={traceUiAction("advanced_modal.source.toggle", () => {
                        void invoke(
                          () => onSetProviderEnabled(source.provider, !source.enabled),
                          (next) => { void reloadProviderSources(next); },
                          (cause) => setProviderSourcesError(describeError(cause)),
                        );
                      }, { provider: source.provider, enabled: !source.enabled })}
                    >
                      {source.enabled ? "Switch off" : "Switch on"}
                    </SmallButton>
                  ) : undefined}
                />
              ))}
              {rows.length === 0 && (
                <PanelRow
                  label={providerSourcesError ? "Sources could not be read" : "Reading sources…"}
                  description={providerSourcesError ?? "Asking the backend which sources this build can use."}
                />
              )}
            </PanelSection>
          </DensePanel>
          <div style={{ padding: "0 16px 8px" }}>
            <DialogButton disabled={blocked} onClick={traceUiAction("advanced_modal.back_2", () => { if (!busy && !localBusyRef.current) setSourcesOpen(false); })}>Back</DialogButton>
          </div>
        </Focusable>
      </ModalRoot>
    );
  }

  /**
   * Every table the record refuses, on a screen that can hold them all.
   *
   * On the panel this was a capped list: the newest twelve rows with a count of
   * the rest, which made the entries past the cap unreachable by any route
   * except emptying the whole record. Here the page extends instead, so a
   * single entry can always be cleared - which matters because the two things
   * recorded expire differently. A table that failed against an older build of
   * the game becomes correct again when the game updates; a download that was
   * never a table stays wrong forever. Clear all re-offers both.
   */
  // Reading a table is not running it, and it is the one thing this screen can
  // say about an exact SHA that a count of markers cannot.
  if (codeOpen && activeTable) {
    return (
      <TableCodeModal
        sha256={activeTable.sha256}
        filename={activeTable.filename}
        onBack={() => setCodeOpen(false)}
      />
    );
  }

  if (blockedOpen) {
    const remaining = blockedTables.length - shownBlockedTables.length;
    return (
      <ModalRoot onCancel={traceUiAction("advanced_modal.blocked.back", () => { if (!busy && !localBusyRef.current) setBlockedOpen(false); })}>
        <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
          <DensePanel>
            <PanelSection>
              <SectionHeading>Tables that did not work</SectionHeading>
              {/* As above: the totals head the list they belong to. */}
              <PanelRow
                tone="header"
                testId="blocked-tables-summary"
                label={blockedSummaryLabel}
                description={blockedSummaryDescription}
                trailing={newestBlockedAt ? `newest ${newestBlockedAt}` : undefined}
                help={blockedSummaryHelp}
                actions={onClearBlockedTables ? (
                  <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.clear_all", () => { void invoke(async () => { try { await onClearBlockedTables(); setBlockedPages(1); } finally { void reloadBlockedTables(); } }); })}>Clear all</SmallButton>
                ) : undefined}
              />
              {/* The game leads, because that is what this list is scanned by:
                  it spans every game and is read months later, and a row that
                  said only `winmm-x64.zip` named neither a game nor a table.
                  A record for a file the source no longer has never produced
                  bytes, so where it has no name of its own the provider row it
                  came from is the whole of its identity, and is what names it,
                  explains it and clears it.

                  `scroll`: the name and the detail are both routinely longer
                  than this window, and both reveal themselves while the ring is
                  on the row. What the detail opens with is what places the
                  record - the day, the release, the build - because the line it
                  used to open with is a sentence the reveal has to travel the
                  whole of before it reaches any of them. */}
              {shownBlockedTables.map((entry) => (
                <PanelRow
                  key={blockedKey(entry)}
                  truncate
                  scroll
                  testId={`blocked-table-${blockedKey(entry).slice(0, 12)}`}
                  label={blockedRowLabel(entry)}
                  description={blockedRowDetail(entry, status.tables)}
                  help={[
                    // The whole of the recorded sentence, for a reader who
                    // wants the half the row shortened away, and the identity
                    // this record is cleared by.
                    entry.reason,
                    entry.sha256 ? `SHA-256 ${entry.sha256}` : `Provider row ${entry.origins?.join(", ") ?? blockedKey(entry)}`,
                  ].join(" \u00b7 ")}
                  actions={onUnblockTable ? (
                    <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.blocked.clear", () => { void invoke(async () => { try { await onUnblockTable(blockedKey(entry)); } finally { void reloadBlockedTables(); } }); }, { blocked_key: blockedKey(entry) })}>Clear</SmallButton>
                  ) : undefined}
                />
              ))}
              {remaining > 0 && (
                <ActionRow testId="blocked-tables-more">
                  <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.blocked.show_more", () => setBlockedPages((pages) => pages + 1))}>
                    {`Show ${Math.min(remaining, BLOCKED_TABLES_PAGE)} more`}
                  </SmallButton>
                </ActionRow>
              )}
            </PanelSection>
          </DensePanel>
          <div style={{ padding: "0 16px 8px" }}>
            <DialogButton disabled={blocked} onClick={traceUiAction("advanced_modal.back_3", () => { if (!busy && !localBusyRef.current) setBlockedOpen(false); })}>Back</DialogButton>
          </div>
        </Focusable>
      </ModalRoot>
    );
  }

  if (debugOpen) {
    return (
      <ModalRoot onCancel={traceUiAction("advanced_modal.debug.back", () => setDebugOpen(false))}>
        <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
          <DensePanel>
            <DebugDetails
              snapshot={debug}
              loading={blocked}
              error={debugError}
              gameNames={gameNames}
              onRefresh={loadDebug}
              onBack={() => setDebugOpen(false)}
            />
          </DensePanel>
        </Focusable>
      </ModalRoot>
    );
  }

  return (
    <ModalRoot onCancel={traceUiAction("advanced_modal.close", close)}>
      <Focusable style={{ minWidth: 440, maxWidth: 680 }}>
        {/* Diagnostics is a dense reference screen, not a workflow: every action
            here is rare, so none of them earns a full-width row. */}
        <DensePanel>
        <PanelSection>
          <SectionHeading>Advanced / Diagnostics</SectionHeading>
          <PanelRow
            truncate
            label={`CE Decky ${statusView.version}`}
            description={statusView.ce.valid ? `Cheat Engine ${statusView.ce.version ?? "of unknown version"} · ${statusView.ce.sha256?.slice(0, 8)}` : statusView.ce.reason ?? "Cheat Engine is not ready"}
            help="Diagnostics and recovery for when the normal panel cannot finish something. Refresh re-reads every backend fact, Self-test checks the plugin's own paths and permissions, and Debug opens the backend's full diagnostics snapshot. Nothing on this screen is needed for ordinary use."
            actions={(
              <>
                <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.refresh", () => { void invoke(onRefreshAll, reconcileContext); })}>Refresh</SmallButton>
                <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.self_test", () => { void invoke(onRunSelfTest, setSelfTestView); })}>Self-test</SmallButton>
                <div ref={openerRef("debug")} style={CONTENTS_ONLY}>
                  <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.debug", () => openSubScreen("debug", openDebug))}>Debug</SmallButton>
                </div>
              </>
            )}
          />
          {selfTestSummaryView && <PanelRow truncate label={selfTestSummaryView.label} description={selfTestSummaryView.counts} />}
          {selfTestSummaryView?.failures.map((check) => (
            <PanelRow
              key={check.name}
              status
              testId={`self-test-failure-${check.name}`}
              label={`${selfTestCheckLabel(check.name)} ${check.blocking === false ? "(warning)" : "(blocker)"}`}
              description={check.detail || "the check failed and reported no detail"}
            />
          ))}
          {/* Degrading a corrupt profile store to an empty list keeps the panel
              alive, but every per-game write re-opens the same file, so without
              this the plugin looks healthy while no game can be configured. */}
          {statusView.profile_state_reason && (
            <PanelRow
              testId="profile-state-repair"
              truncate
              label="Game settings cannot be read"
              description={statusView.profile_state_reason}
              help="CE Decky keeps every game's table choice, authorization and cheat selection in one file. When that file is unreadable, no game can be configured until it is replaced. Discarding it keeps the unreadable file as evidence and starts an empty one; your Cheat Engine installation and your imported tables are untouched, and you re-choose a table per game afterwards from Stored on the home panel, which needs no network."
              actions={<SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.profile.discard", () => { void repair(onRepairProfileState); })}>Discard</SmallButton>}
            />
          )}
        </PanelSection>

        {/* The first thing to reach for when something is wrong, so it sits
            above the diagnostics rather than at the bottom with them. Reading
            state is all it does; nothing here changes anything. */}
        <PanelSection>
          <SectionHeading>Report a problem</SectionHeading>
          <PanelRow
            truncate
            testId="support-bundle"
            label="Collect support bundle"
            description={supportBundleError
              ? supportBundleError
              : supportBundle
                ? `Saved ${supportBundle.filename} (${formatBytes(supportBundle.size_bytes)}) in your home folder.`
                : "Writes one .zip of logs, settings and state into your home folder, and tells you where."}
            help="Everything needed to answer a bug report without the device: CE Decky's own log across every plugin load, what Proton and Cheat Engine printed, the session records naming the exact table and target a Cheat Engine was given, your settings and game profiles, the tables in use, the provider diagnostics behind a search, and what this panel did. Attach the file to a GitHub issue with a description of what happened, and a screenshot if it is something you can see. It holds no password and no Cheat Engine or game files; it does hold your game names, your paths and your cheat tables, so remove anything you would rather not publish first."
            actions={<SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.collect", () => { setSupportBundleError(null); void collectSupportBundle(); })}>Collect</SmallButton>}
          />
          {supportBundle && (
            <PanelRow
              truncate
              testId="support-bundle-path"
              label="Saved to"
              description={supportBundle.path}
              help="The full path of the archive that was just written. Copy it off the device in Desktop Mode, or with whatever file transfer you already use."
            />
          )}
          {supportBundle && omittedNotes(supportBundle).length > 0 && (
            <PanelRow
              truncate
              scroll
              testId="support-bundle-notes"
              label={`${omittedNotes(supportBundle).length} item(s) not collected`}
              description={omittedNotes(supportBundle).slice(0, 3).map((note) => `${note.member}: ${note.reason}`).join(" · ")}
              help="Every file the collector could not take is listed inside the archive, in manifest.json, with the reason. A state file that cannot be read is very often the fault being reported rather than a fault in collecting it, so the archive is still worth attaching."
            />
          )}
          {/* Its own row, and its own words. These members are in the archive
              and they read; saying so beside the count of what is missing is
              what keeps the count meaning what it says. */}
          {supportBundle && shortenedNotes(supportBundle).length > 0 && (
            <PanelRow
              truncate
              scroll
              testId="support-bundle-shortened"
              label={`${shortenedNotes(supportBundle).length} item(s) shortened`}
              description={shortenedNotes(supportBundle).slice(0, 3).map((note) => `${note.member}: ${note.reason}`).join(" · ")}
              help="These files are in the archive and readable. A log longer than the budget it is given is kept from its end, so the newest part is there and the oldest is not. manifest.json names each one and what was left out."
            />
          )}
        </PanelSection>

        <PanelSection>
          <SectionHeading>Cheat Engine installation</SectionHeading>
          <InstallerProvenance ce={statusView.ce} />
        </PanelSection>

        <PanelSection>
          <SectionHeading>Fallback</SectionHeading>
          {/* Every action here sits beside the state it acts on, so a floating
              button never has to explain itself in its own label. */}
          <PanelRow
            truncate
            label="Registered Cheat Engine"
            description={ceIdentityBlockedReason ?? statusView.ce.executable ?? statusView.ce.reason ?? "None registered"}
            help="CE Decky downloads and manages its own Windows Cheat Engine, and this is the fallback for when it cannot. Import takes either a Windows Cheat Engine already on this machine, or a .zip of a Cheat Engine installation directory packed up on a Windows machine - use the archive when the official installer is no longer downloadable. Test runs the registered Cheat Engine on its own to prove it works before a game depends on it; Forget drops the registration so the managed one is used again. None of this touches your tables."
            actions={(
              <>
                <SmallButton disabled={blocked || Boolean(ceIdentityBlockedReason)} onClick={traceUiAction("advanced_modal.import", () => { void invoke(onPickCE); })}>Import…</SmallButton>
                <SmallButton disabled={blocked || !statusView.ce.valid || !protonDraft || Boolean(ceIdentityBlockedReason)} onClick={traceUiAction("advanced_modal.test", () => { void invoke(() => onRunCELaunchSelfTest(protonDraft)); })}>Test</SmallButton>
                {statusView.ce.configured && (
                  <SmallButton disabled={blocked || Boolean(ceIdentityBlockedReason)} onClick={traceUiAction("advanced_modal.forget", () => { void invoke(onClearCEImport, reconcileContext); })}>Forget</SmallButton>
                )}
              </>
            )}
          />
          <PanelRow
            truncate
            label={selectedGameView ? appDetailsView?.displayName || selectedGameView.name : "Steam library"}
            description={selectedGameView
              ? `${selectedGameView.isShortcut ? "Non-Steam" : "Steam"} \u00b7 AppID ${selectedGameView.appId}`
              : `${gamesView.length} games and shortcuts enumerated`}
            help="Which library entry CE Decky acts on. It follows the running game by itself, and Change game on the panel picks a different one; this row only reports what was resolved and re-reads Steam's library and shortcuts."
            actions={<SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.refresh_library", () => { void invoke(onRefreshGames, setGamesView); })}>Refresh library</SmallButton>}
          />
          {selectedGameView && (
            <>
              <PanelSectionRow><TextField label="Target process" value={targetDraft} onChange={traceUiEdit("advanced_modal.target_process", (event: any) => setTargetDraft(String(event.target.value ?? "")))} disabled={blocked} /></PanelSectionRow>
              <PanelRow
                truncate
                label="Target process"
                description={targetDraftValid ? `Cheat Engine will attach to ${targetDraft.trim()}.` : "Enter one .exe basename; paths and control characters are not accepted."}
                help="The exact Windows .exe inside the game that Cheat Engine opens. It is normally confirmed for you when you review a table, so set it here only when attachment picked the wrong process - a launcher or a crash handler instead of the game itself. Type the file name only, such as Game-Win64-Shipping.exe. A Cheat Engine running for this game is stopped first, because it was started for the old target."
                actions={<SmallButton disabled={blocked || !profile?.table_sha256 || !targetDraftValid} onClick={traceUiAction("advanced_modal.save", () => { void invoke(() => onSaveTargetProcess(targetDraft)); }, { process: targetDraft })}>Save</SmallButton>}
              />
              {/* The check that catches a target chosen before the game had
                  ever run. A table can be authorized against an executable read
                  out of the game's own installed folder, which is the only
                  evidence there is while nothing is running, and this is what
                  turns the first real start into the answer: the game is up,
                  these are the Windows programs it started, and the one saved
                  for it is not among them.

                  Said here rather than left to a press that does nothing. The
                  divergence row further down is the other half and a different
                  question, about a Cheat Engine that has already attached to
                  something else; this one fires before anything attaches. */}
              {absentTargetCandidates && (
                <PanelRow
                  truncate
                  scroll
                  testId="target-not-running"
                  label={`${profile?.target_process} is not running in this game`}
                  description={absentTargetDetail}
                  help="Cheat Engine attaches to one exact Windows .exe inside the game. When a table names none and the game had never been started, CE Decky offers the executables in the game's own installed folder, which is the only evidence there is at that point. This is the first chance to check that choice against what the game actually runs."
                  actions={absentTargetAction}
                />
              )}
            </>
          )}
        </PanelSection>

        {/* Attaching Cheat Engine to a game depends entirely on three facts that
            are otherwise invisible: which Proton build the game is actually
            running under, which compatdata directory Steam gave it, and which
            Wine prefix inside that directory the game reports. When an attach
            fails, this is the section that says which one of them is missing. */}
        <PanelSection>
          <SectionHeading>Proton and prefix</SectionHeading>
          <PanelRow
            truncate
            label={observedProton ? observedProton.name : "Proton not observed"}
            description={observedProton
              ? `${observedProton.tool_id} · ${observedProton.proton_sha256.slice(0, 8)} · ${observedProton.source}`
              : ceLaunchView?.observed_proton_reason ?? "No running game to observe a Proton identity from."}
            help="The exact Proton build CE Decky saw the selected game running under. An attached launch uses this identity and nothing else: if it cannot be observed, CE Decky refuses to attach rather than guessing a compatible one."
          />
          {observedProton && <PanelRow truncate label="Proton path" description={observedProton.path} />}
          <PanelRow
            truncate
            label={compatState === "resolved" ? "Compatdata resolved" : compatState === "unknown" ? "Compatdata not resolved" : `Compatdata ${compatState}`}
            description={compatPath ?? `${ceLaunchView?.compat_data?.candidates.length ?? 0} candidate(s) · no single exact directory`}
            help="Steam's per-game compatdata directory, resolved independently from the library metadata. The attached launch requires this to equal the prefix the running game itself reports; a disagreement fails closed instead of writing into the wrong game's prefix."
          />
          <PanelRow
            truncate
            label="Wine prefix"
            description={ceLaunchView?.game?.wine_prefix ?? "The running game reported no prefix."}
          />
          {prefixConflicts.length > 0 && (
            <PanelRow truncate label={`${prefixConflicts.length} conflicting prefix path(s)`} description={prefixConflicts.join(" · ")} />
          )}
          <PanelRow
            truncate
            label={ceLaunchView?.game?.running ? `Game running · ${ceLaunchView.game.pids.length} process(es)` : "Game not running"}
            description={ceLaunchView?.game?.running
              ? `${(ceLaunchView.game.windows_executables ?? []).join(", ") || "no Windows executables observed"}${ceLaunchView.game.launch_executable ? ` · Steam launched ${ceLaunchView.game.launch_executable}` : ""}`
              : ceLaunchView?.game?.reason ?? ceLaunchView?.reason ?? "Nothing observed for this AppID."}
            help="What CE Decky can see of the game's own processes right now: how many it owns, which Windows .exe basenames they are, and which one Steam asked Proton to start. The target process must be one of these."
          />
          {(ceLaunchView?.proton_tools.length ?? 0) > 0 && (
            <PanelSectionRow><DropdownItem
              label="Self-test Proton"
              rgOptions={(ceLaunchView?.proton_tools ?? []).map((tool) => ({ data: tool.tool_id, label: tool.name }))}
              selectedOption={protonDraft}
              onChange={traceUiAction("advanced_modal.self_test_proton", (option) => { const value = String(option.data); setProtonDraft(value); onLaunchProtonChange(value); }, (option) => ({ proton: String(option.data) }))}
              disabled={blocked}
            /></PanelSectionRow>
          )}
          <PanelRow
            truncate
            label="Self-test launch"
            description={`Start Cheat Engine alone in ${ceLaunchView?.self_test_prefix ?? "its own prefix"}, with no game attached.`}
            help="Starts Cheat Engine on its own, in a throwaway prefix, with no game and no table. It answers one question: can the selected Proton build run this exact Cheat Engine at all? Use it when attaching to a game fails and you need to know whether Cheat Engine or the game's own prefix is at fault."
            actions={<SmallButton disabled={blocked || !statusView.ce.valid || !protonDraft || Boolean(ceIdentityBlockedReason)} onClick={traceUiAction("advanced_modal.verify", () => { void invoke(() => onRunCELaunchSelfTest(protonDraft)); })}>Verify</SmallButton>}
          />
          {ceIdentityBlockedReason && <PanelRow truncate label="Cheat Engine is in use" description={ceIdentityBlockedReason} />}
          {ceLaunchView?.recovery_error && <PanelRow truncate label="Recovery" description={ceLaunchView.recovery_error} />}
          {invalidSelectedOwnership && (
            <PanelRow
              testId="launch-ownership-repair"
              truncate
              label="Launch ownership cannot be read"
              description="The durable Cheat Engine ownership record is malformed."
              help="CE Decky will quarantine this record only after a complete process-table scan proves that no process carrying any CE Decky descriptor can still be running. If that proof is incomplete, repair stays blocked and nothing is discarded."
              actions={<SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.ownership.repair", () => { void repair(onRepairOwnedLaunchState); })}>Repair</SmallButton>}
            />
          )}
        </PanelSection>

        {(inspectionView || activeTable) && (
          <PanelSection>
          <SectionHeading>Active table</SectionHeading>
            {activeTable && (
              <PanelRow
                truncate
                label={activeTable.filename}
                description={[
                  activeTable.sha256.slice(0, 12),
                  formatBytes(activeTable.size),
                  // The release the publisher gave these bytes, which is what
                  // search offered them under. The number beside it is Cheat
                  // Engine's own table format and now says so: unlabelled, the
                  // two read as one thing and disagree, so an imported v1.0.6
                  // was described here as "version 52".
                  releaseLabel(tableOrigin?.version),
                  activeTable.table_version ? `CE table ${activeTable.table_version}` : null,
                  tableOrigin ? tableOrigin.retrieved_at.slice(0, 10) : null,
                  tableOrigin?.advertised_sha256 && tableOrigin.advertised_sha256 !== activeTable.sha256 ? "advertised SHA differed" : null,
                  activeTable.available ? null : "file missing",
                ].filter(Boolean).join(" · ")}
                help="The exact table this game is configured to use. Everything CE Decky does with it is keyed by this SHA-256: the consent you gave, the cheats it remembers, and the pins on the panel all belong to these exact bytes and to no other copy of the same table."
              />
            )}
            {tableOrigin && (
              <PanelRow
                truncate
                label={`From ${providerDisplayName(tableOrigin.provider)}`}
                description={tableOrigin.source_page}
                help="The page this exact file was downloaded from. Open reaches it in the Steam browser; a table imported from a local file has no origin recorded."
                actions={originOpenable ? (
                  <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.open", () => { openSourcePage(tableOrigin.source_page); })}>Open</SmallButton>
                ) : undefined}
              />
            )}
            {activeTable && (
              <PanelRow
                truncate
                label="Stored at"
                description={activeTable.blob_path}
                help="CE Decky's own copy of the file, stored under its SHA-256 rather than its name, so two tables that happen to share a filename cannot overwrite each other. The original you imported is left where it was, and this copy is never edited in place. Look inside opens what the table can execute, as text; it runs nothing."
                actions={activeTable.available ? (
                  <div ref={openerRef("code")} style={CONTENTS_ONLY}>
                    <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.look_inside", () => openSubScreen("code", () => setCodeOpen(true)))}>Look inside</SmallButton>
                  </div>
                ) : undefined}
              />
            )}
            <PanelRow
              truncate
              label={profile?.execution_consent_sha256 === profile?.table_sha256 ? "Execution authorized" : "Not authorized"}
              description={`${profile?.pinned.length ?? 0} pinned · ${profile?.remembered.length ?? 0} remembered · ${profile?.table_library.length ?? 0} table(s) imported for this game${profile?.autoload_enabled ? " · auto-load on" : ""}`}
              help="Whether you have authorized this exact table's executable content to run, and how much state this game keeps for it. Revoke clears that authorization and nothing else; the table and its remembered cheats stay. A Cheat Engine running for this game is stopped first, because it is running under the authorization being withdrawn."
              actions={profile?.table_sha256 && profile.execution_consent_sha256 === profile.table_sha256 ? (
                <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.consent.revoke", () => { void invoke(onRevokeConsent); })}>
                  {ownership.ownedBySelected ? "Stop CE and revoke" : "Revoke"}
                </SmallButton>
              ) : undefined}
            />
            {inspectionView && (
              <>
                <PanelRow
                  truncate
                  label={`${inspectionView.total_entries} entries · ${inspectionView.controls.length} controls`}
                  description={`${`${inspectionView.has_lua ? "Lua " : ""}${inspectionView.has_auto_assembler ? "AutoAssembler " : ""}${inspectionView.embedded_files ? `${inspectionView.embedded_files} embedded ` : ""}`.trim() || "no executable content"}${inspectionView.unsupported_record_id_count ? ` · ${inspectionView.unsupported_record_id_count} unsupported` : ""}${inspectionView.ambiguous_record_ids.length ? ` · ${inspectionView.ambiguous_record_ids.length} ambiguous` : ""}`}
                  help="What is actually inside the table. Controls are the records CE Decky can drive; unsupported and ambiguous records are skipped rather than guessed at. Lua and AutoAssembler are executable content, which is why the table needs explicit authorization."
                />
                <PanelRow
                  truncate
                  label="Process hints"
                  description={inspectionView.process_candidates.join(", ") || "None"}
                  help={hintCaseOnlyMismatch
                    ? `This table names its process differently from the one saved for this game - ${profile?.target_process} - in letter case only. That is the table author's build, not a mismatch: CE Decky attaches to the executable this game actually runs.`
                    : "The process this table's author wrote it against. It is evidence about the table, not the target: the saved target process above is what Cheat Engine attaches to."}
                />
              </>
            )}
            {/* Startup actions are the pre-0.9 mechanism, replaced by the
                remembered state Configure cheats writes. A profile that has
                none - every profile made since - does not need a row saying so. */}
            {(startupCount ?? profile?.startup.length ?? 0) > 0 && (
              <PanelRow
                truncate
                label="Legacy startup actions"
                description={startupCount !== null
                  ? `${startupCount} remain for this exact table`
                  : `${profile?.startup.length} run every time this exact table prepares a session`}
                help="An older way of replaying cheats when a table is prepared, kept so a profile made before Configure cheats existed still works. Clearing them is safe: the cheats you confirm now are remembered separately."
                actions={<SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.startup.clear", () => { void invoke(onClearStartup, setStartupCount); })}>Clear</SmallButton>}
              />
            )}
          </PanelSection>
        )}

        <PanelSection>
          <SectionHeading>Runtime</SectionHeading>
          <PanelRow
            truncate
            label={runtimeSessionReady ? "Connected" : runtimeView?.connected ? "Stale / mismatched session" : runtimeView?.prepared ? "Prepared / disconnected" : "Not prepared"}
            description={runtimeSessionReady && runtimeView?.status?.attached
              ? `Attached PID ${runtimeView.status.opened_process_id} · ${runtimeView.status.target_process}${tableLoadDetail}`
              : runtimeView?.session_stale_reason ?? (runtimeView?.connected ? "Prepared/status identity no longer matches this game and table." : "No active session")}
            help="The live link between CE Decky and the Cheat Engine it started for this game and this exact table. Refresh runtime re-reads that state. Processes lists what CE Decky can attach to: with a Cheat Engine running it asks that Cheat Engine, which is the only thing that can give an exact Windows PID; without one it reads the game's own processes from this machine, and one of those can be saved as the target for the next start."
            actions={(
              <>
                <SmallButton disabled={blocked || !selectedGameView} onClick={traceUiAction("advanced_modal.refresh_runtime", () => { void invoke(onRefreshRuntime, setRuntimeView); })}>Refresh runtime</SmallButton>
                {/* One button, two sources. Which one can answer depends on
                    whether a Cheat Engine is running for this exact table, and
                    that is not something to make the user work out first. */}
                <SmallButton
                  disabled={blocked || !selectedGameView}
                  onClick={traceUiAction("advanced_modal.processes", () => {
                    if (runtimeSessionReady) {
                      void invoke(onRefreshProcesses, (next) => { setRuntimeView(next); setAttachCandidate(""); });
                      return;
                    }
                    setObservedTargetsShown(true);
                    void invoke(onRefreshAll, (next) => { reconcileContext(next); setObservedTargetsShown(true); });
                  })}
                >Processes</SmallButton>
              </>
            )}
          />
          {/* Without a Cheat Engine to ask, the game's own processes are still
              observable, and choosing one of those is the recovery that matters:
              it is the target the next start attaches to. No PID goes with them,
              because a Linux process table cannot name a Windows PID. */}
          {observedTargetsShown && !runtimeSessionReady && selectedGameView && (
            <>
              <PanelRow
                truncate
                testId="observed-targets"
                label={observedTargets.length > 0
                  ? `${observedTargets.length} process(es) observed in the game`
                  : ceLaunchView?.game?.running ? "No Windows executables observed yet" : "The game is not running"}
                description={exactPidUnavailableReason
                  ? `${exactPidUnavailableReason} These are the game's own processes instead, read from this machine.`
                  : "The game's own processes, read from this machine."}
                help="What CE Decky can see of the game's Windows processes without asking Cheat Engine anything. Use this when Cheat Engine will not start or will not stay attached, which is exactly when the exact-PID list cannot be produced. Choosing one saves it as this game's target process, and the next Cheat Engine start attaches to it. A Cheat Engine running for this game is stopped first, because it was started for the old target."
              />
              {observedTargets.length > 0 && (
                <>
                  <PanelSectionRow><DropdownItem
                    label="Target from observed processes"
                    rgOptions={[
                      { data: "", label: "Choose an observed .exe…" },
                      ...observedTargets.map((candidate) => ({
                        data: candidate.name,
                        label: candidate.runtimeNoise ? `${candidate.name} · Wine/Proton helper` : candidate.name,
                      })),
                    ]}
                    selectedOption={observedTargetDraft}
                    onChange={traceUiAction("advanced_modal.target_from_observed_processes", (option) => setObservedTargetDraft(String(option.data)), (option) => ({ process: String(option.data) }))}
                    disabled={blocked}
                  /></PanelSectionRow>
                  {/* Kept selectable: for an unusual game one of these really
                      is the right answer, and this screen is the expert escape
                      hatch. Said out loud first, because a helper does not own
                      the game's memory and saving one durably is a change that
                      only shows up as Cheat Engine attaching to nothing. */}
                  {selectedObservedTarget?.runtimeNoise && (
                    <PanelRow
                      truncate
                      testId="observed-target-helper"
                      label={`${selectedObservedTarget.name} is a Wine or Proton helper`}
                      description="These run beside the game rather than being it, so this is almost never the process that owns the game's memory."
                      help="Wine and Proton start their own programs inside the same prefix as the game: services, launchers and helper processes. Cheat Engine can attach to one, and it will simply find nothing to change. It stays selectable because for an unusual game one of them really is the executable that runs it, but if you did not come here for that, pick the entry that looks like the game."
                    />
                  )}
                  <ActionRow>
                    <SmallButton
                      disabled={blocked || !profile?.table_sha256 || !selectedObservedTarget}
                      onClick={traceUiAction("advanced_modal.save_as_target", () => {
                        if (!selectedObservedTarget) return;
                        void invoke(() => onSaveTargetProcess(selectedObservedTarget.name));
                      }, { process: selectedObservedTarget?.name })}
                    >Save as target</SmallButton>
                  </ActionRow>
                  {!profile?.table_sha256 && (
                    <PanelRow
                      truncate
                      label="No table is selected for this game"
                      description="A target process is stored against the table this game uses, so choose a table first."
                    />
                  )}
                </>
              )}
            </>
          )}
          {runtimeView?.prepared && <PanelRow truncate label="Session" description={`${runtimeView.prepared.session_id.slice(0, 8)} · table ${runtimeView.prepared.table_sha256.slice(0, 8)}`} />}
          {/* Silent while Cheat Engine behaves. A game that loses its audio or
              its controller has almost always had a window put over it, and
              after the fact that is otherwise impossible to attribute. */}
          {/* The one case where a window is on the game right now rather than
              having been taken off it, and the only one the user can see for
              themselves. It goes above the count of the ones that worked,
              because it is the answer to "why is the game covered". */}
          {runtimeView?.status?.window_over_game === true && (
            <PanelRow
              truncate
              testId="window-over-game"
              label="A Cheat Engine window could not be hidden"
              description="A window is over the game and CE Decky cannot take it down. Stop Cheat Engine to get the screen back."
              help="A cheat table can build a window of its own, and Cheat Engine does not always let CE Decky hide one. While it is there it takes the running game's screen, audio and controller input. Stopping Cheat Engine removes it. This is worth reporting with the table that caused it."
            />
          )}
          {/* The same windows after the fact. The sweep runs on a timer, so this
              counts the attempts that failed and not the windows they were
              aimed at: it is a diagnostic for a report, which is why it is said
              in attempts rather than dressed up as a number of windows. */}
          {runtimeView?.status?.window_over_game === false && (runtimeView?.status?.unsuppressed_sweeps ?? 0) > 0 && (
            <PanelRow
              truncate
              testId="unsuppressed-sweeps"
              label={`${runtimeView?.status?.unsuppressed_sweeps} attempt(s) to hide a Cheat Engine window failed`}
              description="A window was over the game earlier in this session and is not now."
              help="CE Decky sweeps for Cheat Engine windows on a timer. Each sweep that saw a window it could not put down is counted here, so one stubborn window raises this once per sweep for as long as it was on screen. It is worth reporting with the table that caused it."
            />
          )}
          {(runtimeView?.status?.window_suppressions ?? 0) > 0 && (
            <PanelRow
              truncate
              testId="window-suppressions"
              label={`${runtimeView?.status?.window_suppressions} Cheat Engine window(s) hidden`}
              description="Cheat Engine mapped a window over the game after it started; CE Decky put it back down."
              help="CE Decky never lets Cheat Engine's own windows appear, because a window that does takes the running game's audio and controller input with it. Cheat Engine still maps one occasionally after startup, and this counts how many times that had to be undone. It is a normal thing to see once; a number that keeps climbing is worth reporting."
            />
          )}
          {/* A dialog Cheat Engine put over the game was answered on the
              user's behalf, because over a running game nobody could read it or
              reach it. What it asked is the one thing worth saying. */}
          {(runtimeView?.status?.dialogs_dismissed ?? 0) > 0 && (
            <PanelRow
              truncate
              testId="dialogs-dismissed"
              label={`${runtimeView?.status?.dialogs_dismissed} Cheat Engine dialog(s) dismissed`}
              description={runtimeView?.status?.last_dialog
                ? `The last one was "${runtimeView.status.last_dialog}".`
                : "Cheat Engine asked something over the game."}
              help="Cheat Engine sometimes asks a question in a window of its own. Over a running game that window cannot be read or answered, and on this hardware it takes the screen from the game while it is there, so CE Decky closes it, which Cheat Engine treats as cancelling. If a table stopped doing what it should right after this appeared, the cancelled question is the likely reason."
            />
          )}
          {/* A game that draws nothing while its sound and controller keep
              working is minimized behind Cheat Engine, and if this Cheat Engine
              cannot be asked which windows are minimized, CE Decky will not
              guess: the same request sent to a maximized window would take the
              game out of fullscreen. Silent only once the pair is established,
              which is what `ready` means: the states that are still settling
              publish no `minimized_query` at all, so keying this on that field
              hid exactly the rows that explain a wait. */}
          {runtimeView?.status?.restore_capability !== undefined
            && runtimeView?.status?.restore_capability !== null
            && runtimeView.status.restore_capability !== "ready" && (
            <PanelRow
              truncate
              testId="minimized-query-unavailable"
              label={RESTORE_CAPABILITY_LABEL[runtimeView.status.restore_capability ?? ""]
                ?? "Cannot bring the game back"}
              description={RESTORE_CAPABILITY_SETTLING.has(runtimeView.status.restore_capability ?? "")
                ? "This usually settles by itself within a few seconds."
                : "If the game keeps its sound and its controller but draws nothing, bring it back from Steam."}
              help={`Starting Cheat Engine takes the foreground, and a game that loses it minimizes itself. CE Decky normally asks such a game to come back, but only after asking Windows whether that window really is minimized: sent to a maximized window the same request means 'back to windowed size', which would take a game that was fine out of fullscreen. ${RESTORE_CAPABILITY_HELP[runtimeView.status.restore_capability ?? ""] ?? "This Cheat Engine did not establish that it can do both, so nothing is sent at all."}${runtimeView.status.restore_error ? ` Cheat Engine said: ${runtimeView.status.restore_error}` : ""}`}
            />
          )}
          {/* Strict parsing is correct, but without a repair the game could
              never prepare another session, and the only fix was deleting files
              from a terminal. The backend proves nothing owns the session first. */}
          {runtimeView?.session_state_reason && (
            <PanelSectionRow><PanelRow
              testId="session-state-repair"
              truncate
              label="Session state cannot be read"
              description={runtimeView.session_state_reason}
              help="CE Decky keeps a pointer to this game's current Cheat Engine session. When that pointer or its metadata is unreadable, no new session can be prepared for this game. Discarding it is safe once no Cheat Engine CE Decky owns is running; your tables, authorizations and cheat choices are untouched."
              actions={<SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.session.discard", () => { void repair(onRepairSessionState); })}>Discard</SmallButton>}
            /></PanelSectionRow>
          )}
          {runtimeProcessOptions.length > 0 && (
            <>
              <PanelSectionRow><DropdownItem
                label="Exact process PID"
                rgOptions={[
                  { data: "", label: "Choose an observed .exe process…" },
                  ...runtimeProcessOptions.map((candidate) => ({
                    data: `${candidate.pid}:${candidate.name}`,
                    label: candidate.runtimeNoise
                      ? `${candidate.name} · PID ${candidate.pid} · Wine/Proton helper`
                      : `${candidate.name} · PID ${candidate.pid}`,
                  })),
                ]}
                selectedOption={attachCandidate}
                onChange={traceUiAction("advanced_modal.exact_process_pid", (option) => setAttachCandidate(String(option.data)), (option) => ({ selection: String(option.data) }))}
                disabled={blocked || !runtimeSessionReady}
              /></PanelSectionRow>
              <ActionRow>
                <SmallButton
                  disabled={blocked || !runtimeSessionReady || !selectedAttach}
                  onClick={traceUiAction("advanced_modal.retry_attach", () => {
                    if (!selectedAttach) return;
                    void invoke(() => onRetryAttach(selectedAttach.name, selectedAttach.pid), (next) => setRuntimeView(next));
                  }, { process: selectedAttach?.name, pid: selectedAttach?.pid })}
                >Retry attach</SmallButton>
              </ActionRow>
              {/* Retry attach changes the live bridge target only. Without a way
                  to make that durable, the next session starts from the saved
                  basename and repeats the same failed attachment. */}
              {/* Saving stops the Cheat Engine that is attached right now, which
                  is very often the session the user just recovered by hand with
                  Retry attach. Saying so before the press is the difference
                  between a choice and a surprise. */}
              {liveTargetOverride && (
                <PanelSectionRow><PanelRow
                  testId="live-target-override"
                  truncate
                  label={`Attached to ${liveTargetOverride}, this session only`}
                  description={`${profile?.target_process} is still what this game will use next time. Saving stops the Cheat Engine running now and uses ${liveTargetOverride} from the next start.`}
                  actions={(
                    <SmallButton
                      disabled={blocked || !profile?.table_sha256}
                      onClick={traceUiAction("advanced_modal.stop_ce_and_save", () => { void invoke(() => onSaveTargetProcess(liveTargetOverride)); })}
                    >Stop CE and save</SmallButton>
                  )}
                /></PanelSectionRow>
              )}
            </>
          )}
          {(runtimeView?.status?.processes.length ?? 0) > 0 && runtimeProcessOptions.length === 0 && (
            <PanelRow label="Attach candidates" description="The bridge reported no valid .exe process basenames." />
          )}
        </PanelSection>

        {/* The sources a table search asks. It sits beside the blocked-table
            record because the two answer the same question from opposite
            sides - why a search offered nothing, or offered something that
            could not be used - and because both are durable choices this user
            made rather than state the plugin arrived at on its own.

            The panel carries the count and the totals; the switches live on a
            screen of their own, so a list that grows with every source added
            is not something everything below has to be scrolled past. */}
        {onLoadProviderSources && (
          <PanelSection>
            <SectionHeading>Table sources</SectionHeading>
            <PanelRow
              truncate
              testId="provider-sources"
              label={sourcesSummaryLabel}
              description={sourcesSummaryDescription}
              trailing={providerSources && providerSources.total > 0 && providerSources.enabled_count === 0 ? "all off" : undefined}
              help={sourcesSummaryHelp}
              actions={(
                <div ref={openerRef("sources")} style={CONTENTS_ONLY}>
                  <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.choose", () => openSubScreen("sources", () => { setSourcesOpen(true); void reloadProviderSources(); }))}>
                    Choose
                  </SmallButton>
                </div>
              )}
            />
          </PanelSection>
        )}

        {/* Durable and keyed by content, so what each entry refuses follows
            those exact bytes rather than a row: a table that did not work
            cannot be newly chosen, and a download that was not a table cannot
            be imported. A copy already on this device stays where it is and
            search goes on showing it. Everything here is a record of something
            that already happened, and clearing one is how a table that a game
            update made correct again becomes usable.

            The record itself is not a control surface: it grows on its own,
            holds up to 512 entries, and nobody opens Advanced to read it. So
            the panel carries the one line that says whether anything is being
            refused, and the entries live on a screen of their own that nothing
            below has to be scrolled past. */}
        <PanelSection>
          <SectionHeading>Tables that did not work</SectionHeading>
          <PanelRow
            truncate
            testId="blocked-tables"
            label={blockedSummaryLabel}
            description={blockedSummaryDescription}
            trailing={newestBlockedAt ? `newest ${newestBlockedAt}` : undefined}
            help={blockedSummaryHelp}
            actions={blockedManageable ? (
              <div ref={openerRef("blocked")} style={CONTENTS_ONLY}>
                <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.review", () => openSubScreen("blocked", () => { setBlockedPages(1); setBlockedOpen(true); }))}>Review</SmallButton>
              </div>
            ) : undefined}
          />
        </PanelSection>

        <PanelSection>
          <SectionHeading>Plugin data</SectionHeading>
          <PanelRow
            truncate
            label={removal ? removal.can_delete_managed_data ? "Safe to remove" : "Removal is blocked" : "Plugin data on disk"}
            description={removal
              ? `${totalManagedFiles(removal)} file(s) · ${formatBytes(totalManagedBytes(removal))} under ${removal.managed_root}`
              : "See what CE Decky has put on disk and what removing it would leave behind."}
            help="Reports what removing CE Decky would leave on disk - your downloaded tables, game profiles and its managed Cheat Engine - and anything that would block deleting it, such as a Cheat Engine process CE Decky still owns or session state it cannot read. Nothing is deleted by looking: the report itself offers deletion, and that asks which data to remove and confirms it first."
            actions={<div ref={openerRef("removal")} style={CONTENTS_ONLY}>
              <SmallButton disabled={blocked} onClick={traceUiAction("advanced_modal.check", () => { void invoke(onCheckRemoval, (next) => { setRemoval(next); openSubScreen("removal", () => setRemovalOpen(true)); }); })}>Check</SmallButton>
            </div>}
          />
          {removal && !removal.can_delete_managed_data && (
            <PanelRow truncate label={`${removal.blockers.length} blocker(s)`} description={removal.blockers.join("; ")} />
          )}
        </PanelSection>

        {targetHarness}
        </DensePanel>

        <div style={{ padding: "0 16px 8px" }}><DialogButton disabled={blocked} onClick={traceUiAction("advanced_modal.close_2", close)}>Close</DialogButton></div>
      </Focusable>
    </ModalRoot>
  );
}
