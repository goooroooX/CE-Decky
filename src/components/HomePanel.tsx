import { traceUiAction } from "../uiActions";
import { ButtonItem, NavEntryPositionPreferences, PanelSection, PanelSectionRow, Spinner, ToggleField } from "@decky/ui";
import { useRef } from "react";
import { CheatRow } from "./CheatRow";
import { CompatibilityMark } from "./CompatibilityMark";
import { ActionRow, CONTENTS_ONLY, DensePanel, PanelRow, SectionHeading, SmallButton } from "./PanelDensity";
import { HEXPAW_DATA_URI } from "../assets/hexpaw";
import type { BlockedMark, PinnedCheatRow } from "../uiModel";
import type { AppDetailsSnapshot, GameSummary } from "../steam/client";
import type { CompatibilityEvidence, ManagedCEInstallStatus, TableStatus } from "../types";

interface Props {
  pluginVersion: string | null;
  ceReady: boolean;
  ceStatusText: string;
  installAvailable: boolean;
  installBusy: boolean;
  managedCancelling?: boolean;
  setupPending: boolean;
  /** Why the managed setup status could not be read, or `null` when it was. */
  setupStatusError: string | null;
  onRetrySetupStatus: () => void;
  installOperation: ManagedCEInstallStatus | null;
  ceSource: "Managed" | "Imported" | null;
  ceSha256: string | null;
  onInstall: () => void;
  onCancelInstall: () => void;
  reinstallLabel: string;
  onReinstall: () => void;
  game: GameSummary | null;
  appDetails: AppDetailsSnapshot | null;
  runningDetectionAvailable: boolean;
  runningGameCount: number;
  /**
   * Whether the game this profile is on is running right now.
   *
   * Changing the game underneath a running one is a press away from every
   * question this plugin answers per game: which table is prepared, which
   * process is the target, which session an owned Cheat Engine holds. The row
   * says so and the press is refused while it is true; stopping the game
   * releases it.
   */
  selectedGameRunning?: boolean;
  targetProcess: string | null;
  /**
   * The Windows programs this game did start, when the saved target is not one.
   *
   * `null` wherever that cannot be proved, which is most of the time. A table
   * can be authorized against an executable read out of the game's own
   * installed folder, which is the only evidence there is while nothing is
   * running, so the first real start is the first chance to check it: pressing
   * start with the wrong name attaches to nothing and says nothing, and the
   * user has no way to know which of the two things is wrong.
   */
  targetNotRunning?: readonly string[] | null;
  onChooseGame: () => void;
  table: TableStatus | null;
  tableSource: string;
  onSearchTable: () => void;
  /**
   * The box around Search, so the panel can put the ring back on it.
   *
   * Searching is what a user does next about a table that has just been
   * retired, and the answer that retires one is given with this panel usually
   * not on screen at all. `src/panelFocus.ts` carries that request across the
   * gap; this is the control it names.
   */
  searchButtonRef?: { current: HTMLDivElement | null };
  /**
   * Open with the ring on Search rather than on Advanced.
   *
   * Steam settles the initial focus of a panel it has just built, and it
   * settles it before anything this plugin does afterwards, so a panel that
   * comes back to a pending request has to ask for that control rather than
   * take it. The imperative move is still there for the panel that was alive
   * the whole time and is not being built again.
   *
   * Read once, at the mount. Which control asks for the initial focus is not a
   * thing to move around under a live screen: the request is retired the moment
   * it is acted on, and letting that retirement flow back into this prop would
   * hand Advanced the preference again on the render straight after the ring
   * landed on Search.
   */
  preferSearchFocus?: boolean;
  /**
   * Set when this game still points at a table whose file is gone.
   *
   * That is not the same as having chosen nothing, and it used to render as
   * "No table selected": the row said the user had never picked one while the
   * profile, its authorization and its saved cheats were all still there, and
   * the reason it could not start was on a screen they had no cause to open.
   */
  selectedTableMissing?: string | null;
  /**
   * Why the selected table is recorded as not working, when it is.
   *
   * Keeping a table the refusal dialog asked about is a real answer, and a mark
   * recorded against the same bytes for another game reaches this one too, so a
   * marked table can legitimately still be the selected one. It stays usable -
   * the record is advice, never a trust decision - but the row has to say so,
   * because it otherwise looks exactly like a table that works.
   */
  tableMarkedNotWorking?: string | null;
  /**
   * What is known about the table this game is actually on, as the same glyph
   * Manage and Search use.
   *
   * It was the one table with no mark anywhere on the panel: the row said
   * "marked as not working" in words and said nothing at all about a table that
   * had been proven to work, so the state of the table a user is using was the
   * one state they had to open another screen to see.
   */
  tableEvidence?: CompatibilityEvidence;
  tableBlocked?: BlockedMark | null;
  onOpenImportedTables: () => void;
  runtimeReady: boolean;
  runtimeText: string;
  /**
   * `runtimeText` says a short complete fact and can be read where it is.
   *
   * The row truncates, and a truncated row with no focus stop cannot be opened
   * by any controller press, so the reason a session failed or recovered has to
   * keep its stop. Decided where the text is, because only that branch knows
   * whether the line carries a backend message or a Windows process name.
   */
  runtimeTextComplete: boolean;
  /** The table exceeds the live-control budget, so no snapshot is possible. */
  liveControlsUnavailable: boolean;
  /** The bridge is attached but never got this table into Cheat Engine. */
  tableLoadFailed?: boolean;
  /** Why the last live-state read failed, when one did. The session itself is
      unaffected: reading the live state is not what makes a launch succeed. */
  liveSnapshotError: string | null;
  /** Start CE for the table this profile already authorized, without re-importing it. */
  startRuntimeAvailable: boolean;
  startRuntimeBlockedReason: string | null;
  onStartRuntime: () => void;
  activeCheatLabels: string[];
  activeCheatSnapshotReady: boolean;
  /**
   * Enclosing scripts and attach-only records that are on.
   *
   * Counted apart from the cheats because they are not choices: CE Decky
   * switches them on itself to reach the cheat that needs them, and the picker
   * lists them apart for the same reason. Said rather than hidden, because a
   * user who switched one cheat on and is told two things are running should be
   * able to see where the second came from.
   */
  activeScriptCount: number;
  pinnedCount: number;
  /** Pinned controls promoted onto this panel, with their live active state. */
  pinnedRows: PinnedCheatRow[];
  pinnedBusyRecordId: number | null;
  onTogglePinnedCheat: (recordId: number, active: boolean) => void;
  onChooseCheats: () => void;
  /** Switch off every active control without stopping Cheat Engine. */
  onDisableAllCheats: () => void;
  autoloadEnabled: boolean;
  /** Why auto-load cannot be changed right now, or `null` when it can. */
  autoloadBlockedReason: string | null;
  onAutoloadChange: (enabled: boolean) => void;
  ceRunning: boolean;
  /** Why the global Cheat Engine identity cannot be changed, or `null`. */
  ceIdentityBlockedReason: string | null;
  /** A launch that has started and is still waiting for the resident bridge. */
  launchPending: boolean;
  onStopCE: () => void;
  onAdvanced: () => void;
  busy: boolean;
  error: string | null;
}

/**
 * The compact CE Decky quick-access panel.
 *
 * Everything above the cheats is context a user reads once and changes rarely,
 * so Cheat Engine, the current game and the selected table are one row each and
 * carry their own small action instead of a full-width button. The cheats
 * themselves - the reason the panel exists - keep the full-width controls and
 * must be reachable without scrolling the quick-access column.
 */
export function HomePanel(props: Props) {
  const {
    pluginVersion,
    ceReady,
    ceStatusText,
    installAvailable,
    installBusy,
    managedCancelling = false,
    setupPending,
    setupStatusError,
    onRetrySetupStatus,
    installOperation,
    ceSource,
    ceSha256,
    onInstall,
    onCancelInstall,
    reinstallLabel,
    onReinstall,
    game,
    appDetails,
    runningDetectionAvailable,
    runningGameCount,
    selectedGameRunning = false,
    targetProcess,
    targetNotRunning = null,
    onChooseGame,
    table,
    tableSource,
    onSearchTable,
    searchButtonRef,
    preferSearchFocus = false,
    selectedTableMissing = null,
    tableMarkedNotWorking = null,
    tableEvidence,
    tableBlocked = null,
    onOpenImportedTables,
    runtimeReady,
    runtimeText,
    runtimeTextComplete,
    liveControlsUnavailable,
    tableLoadFailed,
    liveSnapshotError,
    startRuntimeAvailable,
    startRuntimeBlockedReason,
    onStartRuntime,
    activeCheatLabels,
    activeCheatSnapshotReady,
    activeScriptCount,
    pinnedCount,
    pinnedRows,
    pinnedBusyRecordId,
    onTogglePinnedCheat,
    onChooseCheats,
    onDisableAllCheats,
    autoloadEnabled,
    autoloadBlockedReason,
    onAutoloadChange,
    ceRunning,
    ceIdentityBlockedReason,
    launchPending,
    onStopCE,
    onAdvanced,
    busy,
    error,
  } = props;
  const workflowBlocked = busy || setupPending;
  const searchDisabled = workflowBlocked || !game;
  // See `preferSearchFocus`: the mount decides, and nothing after it does.
  //
  // Only where Search can take it. A disabled control refuses the initial focus
  // and Advanced has been told not to ask for it, which is a panel opening with
  // nothing asking at all; the panel moves the ring itself once the control can
  // be pressed, and until then Advanced is where a panel opens.
  const openOnSearch = useRef(preferSearchFocus && !searchDisabled).current;
  // A table past the live-control budget has no snapshot and never will, so the
  // runtime row must not point at Configure cheats - the one screen that
  // refuses a table this size. The session itself is fine.
  // A session that never got its table has nothing to refresh, so the hint
  // about opening Configure cheats would be advice to press a button that can
  // only report the same emptiness. The runtime row says what happened instead.
  // The hint, and whether the reader can finish it where it stands, decided the
  // same way the line under it is: copy this frontend wrote is known and wraps,
  // while a message the backend wrote has no bounded length and keeps a stop
  // rather than pushing the panel's controls down the screen.
  const liveStateHint = liveControlsUnavailable
    ? {
      text: "Live controls are unavailable for a table this size. Cheats saved for this table still load automatically.",
      complete: true,
    }
    // A failed read of the live state says so instead of implying the session
    // is fine and the user simply has not looked yet. Cheat Engine is attached
    // either way, so this never contradicts a successful launch.
    : liveSnapshotError
      ? {
        text: `Live cheat state could not be read: ${liveSnapshotError} Open Configure cheats to try again.`,
        complete: false,
      }
      : { text: "Open Configure cheats to refresh the live state.", complete: true };
  // The runtime row carries a hint only while a connected session cannot be
  // read; the rest of the time it repeats state the row's own label already
  // gives.
  const runtimeHint = runtimeReady && !activeCheatSnapshotReady && !tableLoadFailed ? liveStateHint : null;
  // What is on, counted as the user chose it. A table's own scripts are on
  // because a cheat needed them, so they are named as what they are instead of
  // being added to a number that is meant to match the switches on this panel.
  const activeCheatSummary = activeScriptCount > 0
    ? `${activeCheatLabels.length} active (${activeScriptCount} script${activeScriptCount === 1 ? "" : "s"})`
    : `${activeCheatLabels.length} active`;
  // Read in place unless whichever of the two is on the row was written
  // somewhere this frontend cannot see the length of.
  const runtimeRowStatus = launchPending || (runtimeHint === null ? runtimeTextComplete : runtimeHint.complete);
  const ceDetail = [ceSource, ceSha256 ? ceSha256.slice(0, 8) : null, pluginVersion]
    .filter(Boolean).join(" · ");
  // Changing the game underneath a running one is a press away from every
  // question this plugin answers per game: which table is prepared, which
  // process is the target, which session an owned Cheat Engine holds. The panel
  // says nothing about it - a running game is the ordinary state, and a line
  // explaining a press nobody made costs a row of a 300 pixel column every
  // session - so `README.md` carries it under **Pick the game** instead.

  // Whether the row under the start press is about to say why it cannot be
  // pressed. A target this game is not running is one of those reasons, and it
  // is said there in full - what the game did start, and where the setting
  // lives - so its own row above stands only where that row does not: while
  // Cheat Engine is connected, while a launch is running, and behind a refusal
  // that outranks it, which is what an anti-cheat is.
  const startBlockedRowShown = Boolean(
    table && !runtimeReady && !launchPending && !startRuntimeAvailable && startRuntimeBlockedReason,
  );
  const gameChangeBlocked = Boolean(game) && selectedGameRunning;
  const gameDetail = game
    ? [game.isShortcut ? "Non-Steam" : "Steam", targetProcess || `AppID ${game.appId}`].join(" · ")
    : runningDetectionAvailable
      ? runningGameCount > 1 ? `${runningGameCount} games running; choose one` : "Start a game or choose one"
      : "Automatic detection is unavailable; choose one";

  return (
    <DensePanel>
      {/* HexPaw sits above the first section rather than inside one, so the
          panel opens with the plugin's own mark and no section rule above it.
          The QAM is a narrow, dense surface, so this is deliberately small:
          one fixed width, the artwork's own ratio for the height, and no
          interaction of any kind.

          The margin is set inline for two reasons. The density stylesheet gives
          every direct child of the panel a 6px bottom margin, because every
          other one is a section - this is not, and an inline style is what
          outranks that rule without deepening the specificity war in
          `PanelDensity`. The negative top margin then takes back part of the
          space Decky's own header leaves under the title, so a decorative mark
          costs the column as little height as possible; the few pixels left
          underneath keep the artwork off the first section's rule, which it
          otherwise sits directly on. Width stays an HTML attribute so the
          artwork is still 112px wide if the density CSS cannot resolve Steam's
          class names and is not injected at all. */}
      <div style={{ display: "flex", justifyContent: "center", padding: 0, margin: "-8px 0 3px" }}>
        <img
          src={HEXPAW_DATA_URI}
          alt="HexPaw, the CE Decky mascot"
          width={112}
          style={{ width: 112, height: "auto", display: "block" }}
        />
      </div>
      <PanelSection>
        <SectionHeading>Setup</SectionHeading>
        {ceReady ? (
          <PanelSectionRow>
            <PanelRow
              testId="ce-row"
              truncate
              label={ceStatusText}
              description={ceDetail || "Ready"}
              actions={setupPending ? undefined : (
                <SmallButton disabled={busy || ceRunning || Boolean(ceIdentityBlockedReason) || !installAvailable} onClick={traceUiAction("home_panel.reinstall", onReinstall, { app_id: game?.appId, table_sha: table?.sha256 })}>{reinstallLabel.startsWith("Reinstall") ? "Reinstall" : "Install"}</SmallButton>
              )}
            />
          </PanelSectionRow>
        ) : (
          <>
            <PanelSectionRow><PanelRow testId="ce-row" label="Cheat Engine is not installed" description={ceStatusText} /></PanelSectionRow>
            {/* Until this succeeds it is the only action the panel is about,
                so first-run setup keeps a full-width control. */}
            {!setupPending && !setupStatusError && (
              <PanelSectionRow><ButtonItem layout="below" disabled={busy || Boolean(ceIdentityBlockedReason) || !installAvailable} onClick={traceUiAction("home_panel.download_and_install_ce", () => onInstall(), { app_id: game?.appId, table_sha: table?.sha256 })}>Download and install CE</ButtonItem></PanelSectionRow>
            )}
          </>
        )}
        {/* Naming the game that actually holds Cheat Engine is the difference
            between a dead button and a next step. */}
        {ceIdentityBlockedReason && (
          <PanelSectionRow>
            <PanelRow testId="ce-owned-elsewhere" truncate label="Cheat Engine setup is busy" description={ceIdentityBlockedReason} />
          </PanelSectionRow>
        )}
        {setupStatusError && (
          <PanelSectionRow>
            <PanelRow
              testId="setup-status-error"
              truncate
              label="Setup status unavailable"
              description={setupStatusError}
              actions={<SmallButton disabled={busy} onClick={traceUiAction("home_panel.retry", onRetrySetupStatus, { app_id: game?.appId, table_sha: table?.sha256 })}>Retry</SmallButton>}
            />
          </PanelSectionRow>
        )}
        {installOperation && (
          <PanelSectionRow>
            <PanelRow
              testId="setup-progress"
              truncate
              label={installOperation.state.replace(/_/g, " ")}
              description={`${installOperation.message}${installOperation.error ? ` · ${installOperation.error}` : ""}`}
              actions={installBusy ? <SmallButton disabled={managedCancelling} onClick={traceUiAction("home_panel.cancel", onCancelInstall, { app_id: game?.appId, table_sha: table?.sha256 })}>{managedCancelling ? "Cancelling…" : "Cancel"}</SmallButton> : undefined}
            />
          </PanelSectionRow>
        )}
        {installBusy && (
          <PanelSectionRow>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: 24 }}>
              <Spinner aria-label="CE setup in progress" style={{ width: 18, height: 18, flexShrink: 0 }} />
            </div>
          </PanelSectionRow>
        )}

        {/* Automatic detection can land on the wrong library entry, and a user
            may prepare a table for a game that is not running, so changing the
            game must never require the Advanced screen. */}
        <PanelSectionRow>
          {/* `scroll`: the name of a game and the name of a table are both
              routinely longer than a 300 pixel column that also carries two
              presses, and the mark now leading the table's name takes more of
              that line again. They reveal themselves while the ring is on the
              row, which is what Manage's rows and a pinned cheat's two lines
              already do. */}
          <PanelRow
            testId="game-row"
            truncate
            scroll
            label={game ? appDetails?.displayName || game.name : "No game selected"}
            description={gameDetail}
            actions={<SmallButton disabled={workflowBlocked || gameChangeBlocked} onClick={traceUiAction("home_panel.choose_game", onChooseGame, { app_id: game?.appId, table_sha: table?.sha256 })}>{game ? "Change" : "Choose"}</SmallButton>}
          />
        </PanelSectionRow>

        {/* Before the name, not beside the buttons. This row already carries a
            filename and two presses in a 300 pixel column, and a third thing in
            the trailing group laid Manage off the edge of the panel - the same
            arithmetic that kept this row to two controls in the first place.
            Leading, the mark costs the name's width, which the name can give,
            and Search and Manage stay where they are on every other row. */}
        <PanelSectionRow>
          <PanelRow
            testId="table-row"
            truncate
            scroll
            label={table ? table.filename : selectedTableMissing ? "Selected table is missing" : "No table selected"}
            description={table
              ? `${tableSource} · ${table.sha256.slice(0, 8)}${tableMarkedNotWorking ? " · marked as not working" : ""}`
              : selectedTableMissing ?? "Search online, or open one this device already has"}
            leadingMark={table ? <CompatibilityMark evidence={tableEvidence} blocked={tableBlocked} /> : undefined}
            actions={(
              <>
                <div ref={searchButtonRef} style={CONTENTS_ONLY}>
                  <SmallButton preferredFocus={openOnSearch} disabled={searchDisabled} onClick={traceUiAction("home_panel.search", onSearchTable, { app_id: game?.appId, table_sha: table?.sha256 })}>Search</SmallButton>
                </div>
                {/* Two, because three do not fit. A quick access panel is 300
                    pixels wide and this row already carries a filename: Search,
                    Local file and a third control were laid out past the edge on
                    the device, which is not a row a controller can walk.

                    So everything that is not an online search is behind one
                    press: the tables this game has, the tables the device has,
                    opening a local file, and removing one. Manage, because that
                    screen is where a table is chosen and where it is destroyed,
                    and a press that only opened things would understate the
                    second of those.

                    Always present, because it is now the only way to a local
                    file, and a first run has no table to make it appear. */}
                <SmallButton disabled={workflowBlocked} onClick={traceUiAction("home_panel.manage", onOpenImportedTables, { app_id: game?.appId, table_sha: table?.sha256 })}>Manage</SmallButton>
              </>
            )}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection>
        <SectionHeading>Cheats</SectionHeading>
        <PanelSectionRow>
          {/* A launch this panel owns is the one state where the runtime line
              has nothing to report but the wait. It used to report the wait as
              a sequence: the session is prepared, then it is running but not
              attached, then it is not running for this table again - three
              descriptions of the same not-yet that the poll cycled through
              while the user was trying to read one of them. None of them was
              wrong and none of them was any use, because what is happening is
              that the launch the user started is still running. */}
          <PanelRow
            testId="runtime-row"
            truncate
            tone="header"
            status={runtimeRowStatus}
            label={launchPending ? "Starting Cheat Engine" : tableLoadFailed ? "Table not loaded" : !runtimeReady ? "Not connected" : activeCheatSnapshotReady ? activeCheatSummary : "Connected"}
            description={launchPending
              ? "Loading the table and waiting for Cheat Engine to answer, usually within fifteen seconds on a handheld. Cancel CE launch below stops it."
              : runtimeHint?.text ?? runtimeText}
            trailing={launchPending ? <Spinner style={{ width: 14, height: 14 }} /> : undefined}
          />
        </PanelSectionRow>
        {/* Pinning promotes a control onto this panel so the cheats a user
            actually uses are one press away instead of behind the picker. */}
        {pinnedRows.map((row) => (
          <PanelSectionRow key={row.recordId}>
            <CheatRow
              variant="panel"
              testId={`pinned-cheat-${row.recordId}`}
              label={row.label}
              summary={row.summary}
              active={row.active}
              disabled={workflowBlocked || pinnedBusyRecordId !== null}
              highlighted={pinnedBusyRecordId === row.recordId}
              onActiveChange={traceUiAction("home_panel.toggle_cheat", (active) => onTogglePinnedCheat(row.recordId, active), (active) => ({ app_id: game?.appId, table_sha: table?.sha256, record_id: row.recordId, active }))}
            />
          </PanelSectionRow>
        ))}
        {pinnedCount > 0 && pinnedRows.length === 0 && (
          <PanelSectionRow><PanelRow status label="Pinned controls" truncate description={`${pinnedCount} pinned; connect Cheat Engine to use them here.`} /></PanelSectionRow>
        )}
        {pinnedCount === 0 && activeCheatLabels.slice(0, 4).map((label, index) => (
          <PanelSectionRow key={`${index}:${label}`}><PanelRow label={label} truncate /></PanelSectionRow>
        ))}
        {pinnedCount === 0 && activeCheatLabels.length > 4 && (
          <PanelSectionRow><PanelRow label={`+${activeCheatLabels.length - 4} more`} truncate /></PanelSectionRow>
        )}

        {/* A table stays selected across restarts, so the second run must be
            able to start Cheat Engine again without searching for it again. */}
        {/* Before the press rather than after it. Starting Cheat Engine against
            a process this game is not running ends in an attach that never
            happens, which on this panel looks exactly like Cheat Engine
            failing, so the press is refused while this stands. Advanced is
            where it is repaired, because that is where the target lives and
            where what the game did start is listed. This row is the one that
            says so wherever the refusal under the press is not on screen. */}
        {table && targetNotRunning && !startBlockedRowShown && (
          <PanelSectionRow>
            <PanelRow
              status
              testId="panel-target-not-running"
              label={`${targetProcess} is not running in this game`}
              description={`This game is running ${targetNotRunning.join(", ")}. Cheat Engine attaches to one exact program, so set the target under Advanced before starting it.`}
            />
          </PanelSectionRow>
        )}
        {table && !runtimeReady && (
          <>
            <PanelSectionRow><ButtonItem layout="below" disabled={workflowBlocked || !startRuntimeAvailable} onClick={traceUiAction("home_panel.load_table_start_ce", () => onStartRuntime(), { app_id: game?.appId, table_sha: table?.sha256 })}>Load table & start CE</ButtonItem></PanelSectionRow>
            {/* A launch that is running is not a launch that cannot start, and
                the panel said the second: every reason here is a condition the
                user has to do something about, and "Cheat Engine is already
                running for this game" told somebody who pressed start twenty
                seconds ago that their own launch was the obstacle. The runtime
                line above says what is happening while it happens, so there is
                nothing left for this row to add. */}
            {!launchPending && !startRuntimeAvailable && startRuntimeBlockedReason && (
              <PanelSectionRow><PanelRow status label="Cannot start yet" description={startRuntimeBlockedReason} /></PanelSectionRow>
            )}
          </>
        )}
        <PanelSectionRow><ButtonItem layout="below" disabled={workflowBlocked || !table} onClick={traceUiAction("home_panel.configure_cheats", () => onChooseCheats(), { app_id: game?.appId, table_sha: table?.sha256 })}>Configure cheats</ButtonItem></PanelSectionRow>
        {error && <PanelSectionRow><PanelRow testId="panel-error" label="Attention" description={error} /></PanelSectionRow>}
        {/* Switching everything off, diagnostics and the stop control are rare,
            so they share one row and never take a full-width button from the
            cheats. Disabling every cheat is not stopping Cheat Engine: the
            session stays up, so the same cheats can be switched back on. */}
        {/* ActionRow already supplies the one horizontal controller group.
            Putting Steam's PanelSectionRow around it adds an outer navigation
            row and makes these side-by-side actions behave like a vertical
            step in the QAM instead. */}
        {/* Advanced is the way on from this panel and the half of this row a
            user actually walks to, so it leads the row and takes its free
            width. Both say the same thing to Steam, which enters a row at the
            control covering most of the one above it and falls back to the
            order of the controls only when two of them are exactly as wide. */}
        <ActionRow testId="panel-actions" navEntryPreferPosition={NavEntryPositionPreferences.PREFERRED_CHILD}>
          {/* Advanced is where a panel opens, unless the last answer named
              somewhere else: two controls both asking for the initial focus is
              not a preference Steam can settle. */}
          <SmallButton grow preferredFocus={!openOnSearch} disabled={workflowBlocked} onClick={traceUiAction("home_panel.advanced", onAdvanced, { app_id: game?.appId, table_sha: table?.sha256 })}>Advanced…</SmallButton>
          <SmallButton disabled={workflowBlocked || !runtimeReady || !activeCheatSnapshotReady} onClick={traceUiAction("home_panel.disable_all", onDisableAllCheats, { app_id: game?.appId, table_sha: table?.sha256 })}>Disable all</SmallButton>
          {/* A launch is seconds of waiting for the bridge and is allowed five
              minutes by the backend's own deadline, which can stop it safely
              for the whole of that, so the one relevant recovery action must
              not be disabled by the action that started it. */}
          {launchPending
            ? <SmallButton onClick={traceUiAction("home_panel.cancel_ce_launch", onStopCE, { app_id: game?.appId, table_sha: table?.sha256 })}>Cancel CE launch</SmallButton>
            : ceRunning && <SmallButton disabled={workflowBlocked} onClick={traceUiAction("home_panel.stop_ce", onStopCE, { app_id: game?.appId, table_sha: table?.sha256 })}>Stop CE</SmallButton>}
        </ActionRow>
      </PanelSection>

      <PanelSection>
        <SectionHeading>Auto-load</SectionHeading>
        <PanelSectionRow>
          {/* Blocking the toggle in both directions meant an armed profile
              whose table went missing could never be disarmed from Game Mode,
              and the old intent came back the moment the table was repaired.
              Switching automatic execution off always reduces authority. */}
          <ToggleField
            label="Load last table & cheats"
            description={autoloadBlockedReason ?? "This exact game and table SHA only."}
            checked={autoloadEnabled}
            disabled={workflowBlocked || (autoloadBlockedReason !== null && !autoloadEnabled)}
            onChange={traceUiAction("home_panel.load_last_table_cheats", onAutoloadChange, (enabled) => ({ app_id: game?.appId, table_sha: table?.sha256, enabled }))}
            bottomSeparator="none"
          />
        </PanelSectionRow>
      </PanelSection>
    </DensePanel>
  );
}
