import React from "react";
import { readSupportLog, resetSupportLog } from "../src/supportLog";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  validateEffectiveStartupPlan: vi.fn(),
  associateTable: vi.fn(), blockTable: vi.fn(), deleteTable: vi.fn(), revokeTable: vi.fn(), deleteManagedData: vi.fn(), cancelManagedCEInstall: vi.fn(), clearBlockedTables: vi.fn(),
  clearStartupPreference: vi.fn(), completeManagedCEInstall: vi.fn(), listBlockedTables: vi.fn(), unblockTable: vi.fn(),
  getCELaunchCapability: vi.fn(), getManagedCECapability: vi.fn(), getRuntimeStatus: vi.fn(), getStatus: vi.fn(),
  importCE: vi.fn(), importTable: vi.fn(), inspectTableSha: vi.fn(), inspectTableSource: vi.fn(), launchCEForGame: vi.fn(),
  startupLeftOn: vi.fn(), clearGameRunHolds: vi.fn(),
  listTableCode: vi.fn(), readTableCode: vi.fn(), createSupportBundle: vi.fn(),
  // Whether the game's own program still holds what the table scans for, read
  // while Review is being prepared. Resolved with the answer a game this device
  // has never launched gets, which is the ordinary one: nothing was checked, so
  // Review is the screen it always was.
  checkTableScans: vi.fn().mockResolvedValue({
    source: "file", present: [], missing: [], ambiguous: [], not_checked: [], elapsed_ms: 0,
    reason: "this device does not know which program this game runs",
  }),
  // What a game's own installed folder holds, read while Review is being
  // prepared. Resolved rather than bare: Review opens without it, and a
  // rejected read must not be what stops a table being authorized.
  listGameExecutables: vi.fn().mockResolvedValue({
    schema: 3, app_id: 10, install_dir: null, executables: [], truncated: false,
    source: null, declared_reason: null, reason: null, cause: null,
  }),
  // Which of the account's library this device holds, read while the game list
  // is built. The default says it could not be read, which is the answer that
  // keeps every entry: a test about something else must not have its list
  // quietly filtered by this.
  readLocalLibrary: vi.fn().mockResolvedValue({
    schema: 1, steam_app_ids: [], unstartable_app_ids: [], shortcut_app_ids: [],
    reason: "not read in this test", shortcuts_reason: "not read in this test",
  }),
  // The panel hands its record to the backend as it goes, so that the record
  // outlives the renderer it lives in. Mocked resolved rather than bare, so a
  // flush that fires mid-test settles instead of rejecting into the run.
  recordPanelLog: vi.fn().mockResolvedValue({ ok: true, accepted: 0 }),
  pollCELaunch: vi.fn(), pollManagedCEInstall: vi.fn(), prepareSession: vi.fn(), prepareTableCopy: vi.fn(), repairOwnedLaunchState: vi.fn(), repairSessionState: vi.fn(), runSelfTest: vi.fn(), saveProfile: vi.fn(),
  setAutoload: vi.fn(), setExecutionConsent: vi.fn(), setPinnedControl: vi.fn(), setRememberedCheats: vi.fn(), startCESelfTest: vi.fn(),
  startManagedCEInstall: vi.fn(), stopCEForGame: vi.fn(),
}));
const steam = vi.hoisted(() => ({ listInstalledGames: vi.fn(), listRunningGames: vi.fn(), readAppDetails: vi.fn() }));
const modalState = vi.hoisted(() => ({
  nodes: [] as any[], closes: [] as any[], events: [] as string[],
  /** Title of a window to refuse once, as a window that cannot open does. */
  failTitle: null as string | null,
}));
const decky = vi.hoisted(() => ({ openFilePicker: vi.fn(), toast: vi.fn() }));
// Whether Steam is showing the quick-access panel. The panel re-reads the
// authority whenever it comes back into view, which is what makes a mutation
// that landed while this browser view was being recreated visible at all.
const quickAccess = vi.hoisted(() => ({ visible: true, listeners: new Set<() => void>() }));
const runtimeClient = vi.hoisted(() => ({
  sendRuntimeCommandAndWait: vi.fn(), applyRuntimeSelection: vi.fn(), deactivateAllActiveControls: vi.fn(),
  queryRuntimeControls: vi.fn(), queryRuntimeControlsPartial: vi.fn(),
  // The product limit is a real export the panel reads, not a mock knob.
  MAX_LIVE_CONTROLS: 512,
  RuntimeOperationError: class RuntimeOperationError extends Error {},
  RuntimeOutcomeUnknownError: class RuntimeOutcomeUnknownError extends Error {},
  RuntimeQueryAbortedError: class RuntimeQueryAbortedError extends Error {},
}));

vi.mock("../src/api", () => api);
vi.mock("../src/steam/client", () => steam);
vi.mock("../src/runtimeClient", () => runtimeClient);
vi.mock("@decky/api", async () => {
  // The loader's own hook is state backed, so this stand-in has to re-render
  // the panel when visibility changes: reacting to the panel being shown again
  // is the entire behavior under test.
  const { useSyncExternalStore } = await import("react");
  return {
    FileSelectionType: { FILE: 0 }, definePlugin: (factory: any) => factory, openFilePicker: decky.openFilePicker, toaster: { toast: decky.toast },
    useQuickAccessVisible: () => useSyncExternalStore(
      (onChange: () => void) => { quickAccess.listeners.add(onChange); return () => { quickAccess.listeners.delete(onChange); }; },
      () => quickAccess.visible,
    ),
  };
});
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ButtonItem: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  ConfirmModal: ({ strTitle, strDescription, strOKButtonText, strCancelButtonText, onOK, onCancel }: any) => <div><span>{strTitle}</span><span>{strDescription}</span><button onClick={onOK}>{strOKButtonText}</button><button onClick={onCancel}>{strCancelButtonText}</button></div>,
  DialogButton: ({ children, onClick, disabled, preferredFocus, style }: any) => <button disabled={disabled} data-preferred-focus={preferredFocus} style={style} onClick={onClick}>{children}</button>,
  DropdownItem: ({ label, description, layout, childrenContainerWidth, contextMenuPositionOptions, rgOptions, selectedOption, onChange, disabled }: any) => <label data-layout={layout} data-children-width={childrenContainerWidth}><span>{label}</span>{description ? <span>{description}</span> : null}<select aria-label={label} data-menu-match-width={contextMenuPositionOptions?.bMatchWidth} data-menu-fit-window={contextMenuPositionOptions?.bFitToWindow} data-menu-shift-to-fit={contextMenuPositionOptions?.bShiftToFitWindow} disabled={disabled} value={selectedOption ?? ""} onChange={(event) => onChange?.({ data: event.target.value })}>{rgOptions.map((option: any) => <option key={String(option.data)} value={String(option.data)}>{String(option.label)}</option>)}</select></label>,
  ErrorBoundary: ({ children }: any) => <>{children}</>,
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  // Forwards its ref and keeps a test id it was given, because Steam's does
  // both: a screen that measures its own window asks for the element through
  // the ref, and a double that dropped it handed the component the shape a host
  // with no layout has rather than the one a device has.
  Focusable: React.forwardRef(({ children, navEntryPreferPosition, ...props }: any, ref: any) => <div ref={ref} data-testid={props["data-testid"] ?? "focusable"} data-flow-children={props["flow-children"]} data-nav-entry={navEntryPreferPosition}>{children}</div>), ModalRoot: ({ children, onCancel }: any) => <div><button aria-label="Controller Back" onClick={onCancel}>Controller Back</button>{children}</div>,
  PanelSection: ({ title, children }: any) => <section aria-label={title || "section"}>{children}</section>, PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  Spinner: () => <span role="progressbar">Loading</span>, Toggle: ({ value, onChange, disabled }: any) => <input data-testid="toggle" type="checkbox" checked={value} disabled={disabled} onChange={(event) => onChange?.(event.target.checked)} />, TextField: (props: any) => <label>{props.label}<input aria-label={props["aria-label"] ?? props.label} placeholder={props.placeholder} value={props.value} disabled={props.disabled} onChange={props.onChange} /></label>,
  ToggleField: ({ label, description, checked, onChange, disabled }: any) => <label>{label}<input aria-label={label} type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange?.(event.target.checked)} />{description}</label>,
  gamepadDialogClasses: { Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription", CompactPadding: "CompactPadding" },
  quickAccessControlsClasses: { PanelSection: "PanelSection" },
  showModal: (node: any) => {
    if (modalState.failTitle !== null && node?.props?.strTitle === modalState.failTitle) {
      modalState.failTitle = null;
      throw new Error("no window could be opened");
    }
    modalState.events.push("show");
    const handle = { Close: vi.fn(() => modalState.events.push("close")) };
    modalState.nodes.push(node);
    modalState.closes.push(handle.Close);
    return handle;
  },
  staticClasses: { Title: "title" },
}));

import pluginFactory from "../src/index";
import { ActionFailureModal } from "../src/modals/ActionFailureModal";
import { PriorDurableCommitError } from "../src/durableWrite";
import { AdvancedModal } from "../src/modals/AdvancedModal";
import { CheatSelectionModal } from "../src/modals/CheatSelectionModal";
import { ConfirmModal as DeckyConfirmModal } from "@decky/ui";
import { GamePickerModal } from "../src/modals/GamePickerModal";
import { TableReviewModal } from "../src/modals/TableReviewModal";
import { TableSearchModal } from "../src/modals/TableSearchModal";
import { CONTROL_PAGE_SIZE, PANEL_CATCH_UP_DELAY_MS } from "../src/uiModel";
import { MAX_LIVE_CONTROLS } from "../src/runtimeClient";

const SHA = "1".repeat(64);
const diagnostics = {
  uptime_s: 42.4, log_path: "/logs/ce.log", version: "0.5.1",
  storage: { tables: 2, table_bytes: 4096, profiles: 1, launch_owned_profiles: 0 },
  providers: { fearless: { state: "ready", counters: { searches: 3, results: 12, downloads_succeeded: 1, downloads_failed: 1, bytes_downloaded: 24409, errors: 1 }, last_http_status: 200, last_latency_ms: 812, last_error: null, cooldown_until_epoch_s: 0 } },
  sessions: { apps: [{ app_id: 10, session_count: 2, current_session_id: "abcdef1234", current_error: null, corrupt_entries: 0 }], total_sessions: 2, errors: [] },
  config_state_error: null, table_state_error: "tables unreadable", table_catalog_errors: [],
  profile_state_error: null, provider_state_error: null, session_state_error: null,
  capabilities: { native_extraction: true, shortcuts_vdf: false },
};
const game = { appId: 10, name: "Game", sortAs: "Game", isShortcut: false };
const details = { appId: 10, displayName: "Game", shortcutExe: "", isShortcut: false, compatToolName: "proton", compatToolDisplayName: "Proton", compatToolPriority: 1, platforms: ["windows"] };
const table = { sha256: SHA, filename: "Game.CT", size: 100, table_version: "45", has_lua: false, has_auto_assembler: false, has_embedded_files: false, executable_content: false, entry_count: 1, blob_path: "/managed/Game.CT", available: true, schema_version: 2, origins: [] };
const inspect = { sha256: SHA, table_version: "45", total_entries: 1, has_lua: false, has_auto_assembler: false, embedded_files: 0, process_candidates: ["game.exe"], controls: [{ id: 7, description: "Health", path: ["Health"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false, dropdown_values: [], dropdown_read_only: false }], ambiguous_record_ids: [], unsupported_record_id_count: 0 };

function status(withProfile = true, ceReady = true) {
  return {
    version: "0.5.0", user_home: "/home/deck", managed_root: "/home/deck/.cheat-engine-decky", settings_dir: "/settings", log_dir: "/logs", log_file: "", sevenzip: null,
    ce: { configured: ceReady, valid: ceReady, executable: ceReady ? "/home/deck/CE/Cheat Engine.exe" : null, sha256: ceReady ? "a".repeat(64) : null, version: ceReady ? "7.7" : null, reason: ceReady ? null : "not installed", managed: false, provenance: null },
    tables: withProfile ? [table] : [], profiles: withProfile ? [{ app_id: 10, name: "Game", is_shortcut: false, table_sha256: SHA, target_process: "game.exe", execution_consent_sha256: SHA, startup: [], pinned: [], previous_table_sha256: null, table_history: {}, table_library: [SHA], autoload_enabled: true, remembered: [{ record_id: 7, active: true, value: "100" }] }] : [],
    config_state_reason: null, table_state_reason: null, table_catalog_errors: [], profile_state_reason: null, features: {},
  };
}
function noRuntime() { return { prepared: null, status: null, next_generation: 1, status_age_ms: null, status_fresh: false, status_clock_skew: false, session_current: false, session_stale_reason: "none", connected: false }; }
function liveRuntime() { return { prepared: { session_id: "session", app_id: 10, ce_sha256: "a".repeat(64), table_sha256: SHA, descriptor_path: "/d", descriptor_sha256: "d".repeat(64), descriptor_md5: "e".repeat(32), control_path: "/c", status_path: "/s", descriptor_windows_path: "Z:\\d", is_shortcut: false }, status: { session_id: "session", app_id: 10, ce_sha256: "a".repeat(64), table_sha256: SHA, descriptor_sha256: "d".repeat(64), heartbeat_ms: Date.now(), attached: true, target_process: "game.exe", opened_process_id: 42, results: [{ generation: 1, record_id: 7, ok: true, active: true, value: "100", error: null }], processes: [[42, "game.exe"]] }, next_generation: 2, status_age_ms: 0, status_fresh: true, status_clock_skew: false, session_current: true, session_stale_reason: null, connected: true }; }
const launch = { schema: 2, modes: ["self_test", "attached"], self_test_prefix: "/prefix", operations: [], game: { app_id: 10, running: true, pids: [42], compat_data_path: "/compat", steam_client_install_path: "/steam", wine_prefix: "/compat/pfx", compat_tool_paths: [], conflicting_compat_data_paths: [], conflicting_steam_client_install_paths: [], conflicting_wine_prefixes: [], scanned: 1, reason: null }, compat_data: null, proton_tools: [], reason: null, observed_proton_tool: null, observed_proton_reason: null, ce_executable_sha256: "a".repeat(64), ce_ready: true, recovered: null, recovery_error: null };

function renderContent() { const plugin = (pluginFactory as any)(); return render(plugin.content); }

/**
 * Press the way to a local file, which is now inside the picker.
 *
 * A quick access panel row carrying a filename has space for two controls, so
 * the panel offers Search and one press for everything else about which table
 * this game runs. Opening a file is one of those, and the picker closes before
 * Decky's own picker opens, because a modal over a modal is a window behind a
 * window. The context modal is recorded rather than mounted here, so this does
 * what its button does.
 */
async function pressLocalFile() {
  fireEvent.click(screen.getByRole("button", { name: "Manage" }));
  // Waited for rather than read once. What the press opens depends on what the
  // panel has finished loading, and a case that opens it while the first status
  // read is still in flight found nothing there and threw - which on CI failed
  // the case and left the file picker's queued answer behind for whichever case
  // ran next to consume.
  const picker = await waitFor(() => {
    const found = [...modalState.nodes].reverse().find((node: any) => node?.props?.onOpenLocalFile);
    if (!found) throw new Error("the table picker did not open");
    return found;
  });
  await act(async () => {
    picker.props.onClose();
    picker.props.onOpenLocalFile();
  });
}

/**
 * What a failed press actually put in front of the user.
 *
 * A Steam notification slides away on its own timer and was gone before it
 * could be read, so a failure opens a dialog instead. The notification is still
 * the fallback for a dialog Steam refuses to open.
 */
function failureShown(): string | null {
  for (let index = modalState.nodes.length - 1; index >= 0; index -= 1) {
    const node = modalState.nodes[index];
    if (node?.type === ActionFailureModal) return String(node.props.message);
  }
  return null;
}

/**
 * Show a cheat row's details.
 *
 * An active record that takes a value already shows them - its editor is not
 * optional - so that row offers a disabled "Less" instead of "More". Waiting
 * for the editor rather than for the button keeps a test honest about which
 * state it is in.
 */
async function revealDetails(scope: any = screen): Promise<void> {
  const more = scope.queryByRole("button", { name: "More" });
  if (more) { fireEvent.click(more); return; }
  await scope.findByRole("button", { name: "Less" });
}

beforeEach(() => {
  // The panel now remembers an explicit game choice across remounts, so a
  // leftover one would leak into the next case.
  try { window.localStorage.clear(); } catch { /* jsdom always has it */ }
  quickAccess.visible = true; quickAccess.listeners.clear();
  vi.clearAllMocks(); modalState.nodes.length = 0; modalState.closes.length = 0; modalState.events.length = 0; modalState.failTitle = null;
  api.listBlockedTables.mockResolvedValue({ schema: 1, reason: null, tables: [] });
  // Re-established per case, because `clearMocks` clears the calls and leaves
  // the implementation: one case setting a real library would otherwise filter
  // every list after it. The default is the answer that keeps every entry.
  api.readLocalLibrary.mockReset().mockResolvedValue({
    schema: 1, steam_app_ids: [], unstartable_app_ids: [], shortcut_app_ids: [],
    reason: "not read in this test", shortcuts_reason: "not read in this test",
  });
  api.stopCEForGame.mockReset().mockResolvedValue({ stopped: false, recovered: false });
  // What an ordinary startup leaves on that nobody chose: nothing.
  api.startupLeftOn.mockReset().mockResolvedValue({ table_sha256: SHA, record_ids: [] });
  api.revokeTable.mockReset().mockImplementation(async () => {
    const detached = status(true) as any;
    Object.assign(detached.profiles[0], { table_sha256: null, execution_consent_sha256: null,
      autoload_enabled: false, table_history: { [SHA]: { execution_consent: false } } });
    api.getStatus.mockResolvedValue(detached);
    return detached.profiles[0];
  });
  api.completeManagedCEInstall.mockReset().mockRejectedValue(new Error("managed CE setup changed; refresh before completing it"));
  api.getStatus.mockResolvedValue(status(true)); api.getManagedCECapability.mockResolvedValue({ schema: 3, mode: "managed_install", managed_install_available: false, release_manifest_loaded: true, network_download_enabled: true, native_extraction_enabled: false, reason: "No extraction", release: null, operation: null });
  api.getCELaunchCapability.mockResolvedValue(launch); api.getRuntimeStatus.mockResolvedValue(liveRuntime()); api.inspectTableSha.mockResolvedValue(inspect);
  // `clearAllMocks` keeps queued `mockResolvedValueOnce` entries, and a case that
  // queues more than it consumes would otherwise answer the next case's first
  // query. Reset these before re-arming their defaults.
  runtimeClient.queryRuntimeControls.mockReset();
  runtimeClient.queryRuntimeControlsPartial.mockReset();
  // The file picker is the one this caught out: a case that failed before its
  // press consumed the path it queued handed that path to the next case, which
  // then imported a file while proving that a cancelled picker imports nothing.
  // One real failure read as four.
  decky.openFilePicker.mockReset();
  runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
  runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] });
  steam.listRunningGames.mockResolvedValue({ available: true, games: [game] }); steam.listInstalledGames.mockResolvedValue([game]); steam.readAppDetails.mockResolvedValue(details);
});
afterEach(() => cleanup());

describe("Home panel and managed setup", () => {
  async function openAdvanced() {
    await screen.findByText("Game.CT");
    fireEvent.click(await screen.findByRole("button", { name: "Advanced…" }));
    return waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === AdvancedModal);
      expect(node).toBeTruthy();
      return node;
    });
  }
  async function openPicker() {
    await screen.findByText("Game.CT");
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    return waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === CheatSelectionModal);
      expect(node).toBeTruthy();
      return node;
    });
  }

  it.each(["new_holder", "stale_first"])("uses explicitly confirmed refreshed Manage holders (%s)", async (caseName) => {
    const initial = status(true);
    const other = { ...initial.profiles[0], app_id: 20, name: "Other game" };
    if (caseName === "stale_first") initial.profiles.push(other);
    api.getStatus.mockResolvedValue(initial);
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const modal = modalState.nodes.find((node: any) => node.props.onRefreshHolders);
    const refreshed = status(true);
    refreshed.profiles[0].table_sha256 = "9".repeat(64);
    refreshed.profiles.push(other);
    api.getStatus.mockResolvedValue(refreshed);
    const holders = await modal.props.onRefreshHolders();
    expect(holders.holderIds[SHA]).toEqual([20]);
    api.revokeTable.mockClear();
    await modal.props.onRevoke(SHA, caseName === "stale_first" ? [10, 20] : holders.holderIds[SHA]);
    expect(api.revokeTable).toHaveBeenCalledWith(20, SHA);
    expect(api.revokeTable).not.toHaveBeenCalledWith(10, SHA);
    expect(api.stopCEForGame).toHaveBeenCalledWith(20, SHA);
  });

  it("never hands a superseded read to Manage as the current holders", async () => {
    // Two reads overlap and the slower one answers last. The panel already
    // refused to be repainted by it; a caller deciding which games to revoke
    // from, or a detached screen repainting itself, must be answered with what
    // was accepted rather than with the read that lost.
    const initial = status(true);
    const other = { ...initial.profiles[0], app_id: 20, name: "Other game" };
    api.getStatus.mockResolvedValue(initial);
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const modal = modalState.nodes.find((node: any) => node.props.onRefreshHolders);

    // The older read is held open while a newer one learns about a second
    // holder and is accepted.
    let releaseStale!: (value: unknown) => void;
    api.getStatus.mockImplementationOnce(() => new Promise((resolve) => { releaseStale = resolve; }));
    const stale = modal.props.onRefreshHolders();
    await waitFor(() => expect(releaseStale).toBeTypeOf("function"));
    const newer = status(true);
    newer.profiles.push(other);
    api.getStatus.mockResolvedValue(newer);
    expect((await modal.props.onRefreshHolders()).holderIds[SHA]).toEqual([10, 20]);
    await act(async () => { releaseStale(initial); });

    expect((await stale).holderIds[SHA]).toEqual([10, 20]);
    // And a confirmation made from the older set is refused rather than
    // quietly revoking half of the holders: the guard compares against what
    // was accepted, so the holder that read never knew about is still counted.
    api.revokeTable.mockClear();
    await expect(modal.props.onRevoke(SHA, [10])).rejects.toThrow(/holder games changed/);
    expect(api.revokeTable).not.toHaveBeenCalled();
  });

  it("makes an overtaken blocked-table read wait for the one that overtook it", async () => {
    // The same for the record a detached Search repaints its marks from.
    api.listBlockedTables.mockResolvedValue({ schema: 1, reason: null, tables: [] });
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Search", exact: true }));
    const search = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.props?.onRefreshBlocked);
      expect(node).toBeTruthy();
      return node;
    });

    let releaseOlder!: (value: unknown) => void;
    let releaseNewer!: (value: unknown) => void;
    api.listBlockedTables
      .mockImplementationOnce(() => new Promise((resolve) => { releaseOlder = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { releaseNewer = resolve; }));
    const older = search.props.onRefreshBlocked();
    const newer = search.props.onRefreshBlocked();
    await waitFor(() => expect(releaseNewer).toBeTypeOf("function"));

    let settled = false;
    void older.then(() => { settled = true; });
    await act(async () => { releaseOlder({ schema: 1, reason: null, tables: [] }); });
    expect(settled).toBe(false);

    await act(async () => {
      releaseNewer({ schema: 1, reason: null, tables: [{ key: SHA, sha256: SHA, reason: "did not switch on", cause: "refused", origins: [] }] });
    });
    expect(Object.keys((await older).byDigest)).toEqual([SHA]);
    expect(Object.keys((await newer).byDigest)).toEqual([SHA]);
  });

  it("makes an overtaken read wait for the one that overtook it", async () => {
    // The other completion order, and the one the accepted snapshot could not
    // answer: the newer read is still in flight when the older one lands, so
    // there is nothing accepted yet that is newer than the caller's question.
    // Answering from before the newer read is answering with a state already
    // known to be out of date.
    const initial = status(true);
    const other = { ...initial.profiles[0], app_id: 20, name: "Other game" };
    api.getStatus.mockResolvedValue(initial);
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const modal = modalState.nodes.find((node: any) => node.props.onRefreshHolders);

    let releaseOlder!: (value: unknown) => void;
    let releaseNewer!: (value: unknown) => void;
    api.getStatus
      .mockImplementationOnce(() => new Promise((resolve) => { releaseOlder = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { releaseNewer = resolve; }));
    const older = modal.props.onRefreshHolders();
    const newer = modal.props.onRefreshHolders();
    await waitFor(() => expect(releaseNewer).toBeTypeOf("function"));

    let settled = false;
    void older.then(() => { settled = true; });
    const updated = status(true);
    updated.profiles.push(other);
    await act(async () => { releaseOlder(initial); });
    // Still nothing true to say: the read that won has not answered yet.
    expect(settled).toBe(false);

    await act(async () => { releaseNewer(updated); });
    expect((await older).holderIds[SHA]).toEqual([10, 20]);
    expect((await newer).holderIds[SHA]).toEqual([10, 20]);
  });

  it.each(["winner pending", "winner already answered"])(
      "answers an overtaken read that failed with the read that overtook it (%s)", async (order) => {
    // A read that was overtaken and then failed used to report its own failure,
    // so a press could be refused over an error about a question the winning
    // read had already answered.
    const initial = status(true);
    const other = { ...initial.profiles[0], app_id: 20, name: "Other game" };
    api.getStatus.mockResolvedValue(initial);
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const modal = modalState.nodes.find((node: any) => node.props.onRefreshHolders);

    let failOlder!: (cause: unknown) => void;
    let releaseNewer!: (value: unknown) => void;
    api.getStatus
      .mockImplementationOnce(() => new Promise((_, reject) => { failOlder = reject; }))
      .mockImplementationOnce(() => new Promise((resolve) => { releaseNewer = resolve; }));
    const older = modal.props.onRefreshHolders().catch((cause: Error) => cause);
    const newer = modal.props.onRefreshHolders();
    await waitFor(() => expect(releaseNewer).toBeTypeOf("function"));
    const updated = status(true);
    updated.profiles.push(other);

    if (order === "winner already answered") {
      await act(async () => { releaseNewer(updated); });
      await act(async () => { failOlder(new Error("status unavailable")); });
    } else {
      let settled = false;
      void older.then(() => { settled = true; });
      await act(async () => { failOlder(new Error("status unavailable")); });
      // Nothing true to say yet, and its own failure is not an answer.
      expect(settled).toBe(false);
      await act(async () => { releaseNewer(updated); });
    }

    expect((await older as any).holderIds[SHA]).toEqual([10, 20]);
    expect((await newer).holderIds[SHA]).toEqual([10, 20]);
    // And the press that follows is decided from what is current: a
    // confirmation of the set the failed read never saw is refused.
    api.getStatus.mockResolvedValue(updated);
    api.revokeTable.mockClear();
    await expect(modal.props.onRevoke(SHA, [10])).rejects.toThrow(/holder games changed/);
    expect(api.revokeTable).not.toHaveBeenCalled();
  });

  it("fails an overtaken read with the read that overtook it", async () => {
    // Substituting an older snapshot for a newer read that failed would answer
    // a question nobody could answer, and the caller is deciding with it.
    const initial = status(true);
    api.getStatus.mockResolvedValue(initial);
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const modal = modalState.nodes.find((node: any) => node.props.onRefreshHolders);

    let releaseOlder!: (value: unknown) => void;
    let failNewer!: (cause: unknown) => void;
    api.getStatus
      .mockImplementationOnce(() => new Promise((resolve) => { releaseOlder = resolve; }))
      .mockImplementationOnce(() => new Promise((_, reject) => { failNewer = reject; }));
    // Both failures are observed from the moment they can happen: an unhandled
    // rejection here would be this test's own doing rather than the panel's.
    const older = modal.props.onRefreshHolders().catch((cause: Error) => cause);
    const newer = modal.props.onRefreshHolders().catch(() => "failed");
    await waitFor(() => expect(failNewer).toBeTypeOf("function"));
    await act(async () => { releaseOlder(initial); });
    await act(async () => { failNewer(new Error("status unavailable")); });

    expect(await older).toBeInstanceOf(Error);
    expect((await older as Error).message).toContain("status unavailable");
    expect(await newer).toBe("failed");
  });

  it("never hands a superseded blocked-table read to a detached screen", async () => {
    api.listBlockedTables.mockResolvedValue({ schema: 1, reason: null, tables: [] });
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Search", exact: true }));
    const search = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.props?.onRefreshBlocked);
      expect(node).toBeTruthy();
      return node;
    });

    let releaseStale!: (value: unknown) => void;
    api.listBlockedTables.mockImplementationOnce(() => new Promise((resolve) => { releaseStale = resolve; }));
    const stale = search.props.onRefreshBlocked();
    await waitFor(() => expect(releaseStale).toBeTypeOf("function"));
    const marked = { schema: 1, reason: null, tables: [{ key: SHA, sha256: SHA, reason: "did not switch on", cause: "refused", origins: [] }] };
    api.listBlockedTables.mockResolvedValue(marked);
    expect(Object.keys((await search.props.onRefreshBlocked()).byDigest)).toEqual([SHA]);
    await act(async () => { releaseStale({ schema: 1, reason: null, tables: [] }); });

    // The detached screen cannot see this panel's generations, so what it is
    // handed has to be the accepted record rather than the read that lost.
    expect(Object.keys((await stale).byDigest)).toEqual([SHA]);
  });

  it.each(["single", "all"])("reconciles an uncertain committed clear without retrying (%s)", async (mode) => {
    api.listBlockedTables.mockResolvedValue({ schema: 1, reason: null, tables: [{ key: SHA, sha256: SHA, reason: "failed", origins: [] }] });
    const write = mode === "single" ? api.unblockTable : api.clearBlockedTables;
    write.mockImplementation(async () => {
      api.listBlockedTables.mockResolvedValue({ schema: 1, reason: null, tables: [] });
      throw Object.assign(new Error("directory sync failed"), { pythonTraceback: "Traceback (most recent call last):\nDurabilityUnknownError: replaced" });
    });
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Advanced…", exact: true }));
    const advanced = modalState.nodes.find((node: any) => node.type === AdvancedModal);
    if (mode === "single") await expect(advanced.props.onUnblockTable(SHA)).resolves.toBeUndefined();
    else await expect(advanced.props.onClearBlockedTables()).resolves.toBeUndefined();
    expect(write).toHaveBeenCalledTimes(1);
  });

  it("requests the selected stopped game's build when history exists", async () => {
    const snapshot = status(true);
    snapshot.table_compatibility = { schema: 2, reason: null, entries: [{ app_id: 10, table_sha256: SHA, target_process: "game.exe", pe_version: null, steam_build_id: "1", last_working_at: 1, invalidated: false, state: "unknown" }] };
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();
    await waitFor(() => expect(api.getStatus).toHaveBeenCalledWith(10));
  });

  it("opens device-wide Manage with no selected game", async () => {
    const snapshot = status(true);
    snapshot.table_compatibility = { schema: 2, reason: null, entries: [{ app_id: 99, table_sha256: SHA, target_process: "game.exe", pe_version: "1", steam_build_id: null, last_working_at: 1, invalidated: false, state: "matching" }] };
    api.getStatus.mockResolvedValue(snapshot);
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    steam.listInstalledGames.mockResolvedValue([]);
    renderContent();
    const manage = await screen.findByRole("button", { name: "Manage", exact: true });
    await waitFor(() => expect((screen.getByRole("button", { name: "Manage", exact: true }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const modal = modalState.nodes.find((node: any) => node.props.canSelect === false);
    expect(modal).toBeTruthy();
    expect(modal.props.otherTables).toEqual([table]);
    // The route to a local file is offered here too: the file joins this
    // device's library with no game at all, and what waits for a game is the
    // association and the authorization that Use performs on the row it
    // produces. Withholding the press left this screen with no sign the route
    // existed, on the one screen that carries it.
    expect(modal.props.onOpenLocalFile).toBeTypeOf("function");
    expect(modal.props.compatibility).toEqual([expect.objectContaining({ app_id: 99, table_sha256: SHA, state: "unknown" })]);
  });

  it.each(["success", "refresh_failed", "reply_lost"])("clears Home after bulk deletion including uncertain outcomes (%s)", async (outcome) => {
    api.deleteManagedData.mockImplementation(async () => {
      if (outcome === "refresh_failed") api.getStatus.mockRejectedValue(new Error("status temporarily unavailable"));
      else api.getStatus.mockResolvedValue(status(false));
      if (outcome === "reply_lost") throw new Error("deletion reply lost");
      return { scope: "all", deleted: [], failed: [], readiness: null };
    });
    renderContent();
    await screen.findByText("Game.CT");
    const advanced = await openAdvanced();
    await act(async () => {
      if (outcome === "reply_lost") await expect(advanced.props.onDeleteManagedData("all")).rejects.toThrow("deletion reply lost");
      else await expect(advanced.props.onDeleteManagedData("all")).resolves.toMatchObject({ scope: "all", failed: [] });
    });
    expect(screen.queryByText("Game.CT")).toBeNull();
  });

  it("keeps this side exactly as it was when the deletion was refused", async () => {
    // The panel reconciles after a deletion whose reply it never got, because a
    // deletion commits before the call returns. A refusal is the opposite case:
    // the backend checked before touching anything and said so, and forgetting
    // the game the user chose for a deletion that did not happen is a
    // disagreement this side invents by itself.
    // As Decky Loader 3.2.6 delivers it: an empty message, with the Python text
    // only in the traceback. That loader is why `src/errors.ts` exists.
    api.deleteManagedData.mockRejectedValue(Object.assign(new Error(""), {
      name: "Python ValueError",
      pythonTraceback: "Traceback (most recent call last):\nValueError: nothing was deleted; a Cheat Engine process CE Decky owns is still running; stop it first",
    }));
    renderContent();
    await screen.findByText("Game.CT");
    const advanced = await openAdvanced();
    // Set after the panel has settled, because mounting with a live game list
    // is itself allowed to forget a remembered choice.
    window.localStorage.setItem("ce-decky.selected-game.v1", JSON.stringify({ appId: 99, isShortcut: false, at: Date.now() }));

    await act(async () => {
      await expect(advanced.props.onDeleteManagedData("all")).rejects.toThrow();
    });
    expect(window.localStorage.getItem("ce-decky.selected-game.v1")).not.toBeNull();
    // And the panel still describes what is still there.
    expect(screen.getByText("Game.CT")).toBeTruthy();
  });

  it("returns this side to first run when everything is deleted", async () => {
    // "Everything" promises a first-run CE Decky, and this side keeps durable
    // choices the file sweep cannot reach: the game the user picked by hand,
    // which a panel mounting with no game running restores by itself. A
    // deletion that leaves it is a first run that opens on the state it was
    // supposed to have forgotten.
    api.deleteManagedData.mockResolvedValue({ scope: "all", deleted: [], failed: [], readiness: null });
    renderContent();
    await screen.findByText("Game.CT");
    const advanced = await openAdvanced();
    window.localStorage.setItem("ce-decky.selected-game.v1", JSON.stringify({ appId: 99, isShortcut: false, at: Date.now() }));

    await act(async () => { await advanced.props.onDeleteManagedData("all"); });
    expect(window.localStorage.getItem("ce-decky.selected-game.v1")).toBeNull();

    // A narrower scope is not a first run and leaves the choice alone.
    window.localStorage.setItem("ce-decky.selected-game.v1", JSON.stringify({ appId: 99, isShortcut: false, at: Date.now() }));
    api.deleteManagedData.mockResolvedValue({ scope: "cache", deleted: [], failed: [], readiness: null });
    await act(async () => { await advanced.props.onDeleteManagedData("cache"); });
    expect(window.localStorage.getItem("ce-decky.selected-game.v1")).not.toBeNull();
  });

  it.each([false, true])("gives user-started setup one failure owner (reinstall=%s)", async (force) => {
    api.getStatus.mockResolvedValue(status(false, force));
    api.getManagedCECapability.mockResolvedValue({
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: null,
    });
    api.startManagedCEInstall.mockRejectedValue(new Error("native CE extraction failed"));
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();

    const button = await screen.findByRole("button", { name: force ? /^(Reinstall|Install)$/ : "Download and install CE" });
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(button);
    if (force) {
      await waitFor(() => expect(modalState.nodes).toHaveLength(1));
      act(() => modalState.nodes[0].props.onOK());
    }
    await waitFor(() => expect(api.startManagedCEInstall).toHaveBeenCalledWith(force));
    expect(await screen.findByText("native CE extraction failed")).toBeTruthy();
    await waitFor(() => expect(modalState.nodes.some((node) => node.props.strTitle === "Cheat Engine could not be downloaded")).toBe(true));
    expect(modalState.nodes.some((node) => node.type === ActionFailureModal)).toBe(false);
    const visible = modalState.nodes.filter((_node, index) => modalState.closes[index].mock.calls.length === 0);
    expect(visible).toHaveLength(1);
    expect(visible[0].props.strTitle).toBe("Cheat Engine could not be downloaded");
  });

  it("recovers from a lost first status read without remounting the panel", async () => {
    // A single transient RPC failure at mount used to leave Home an
    // indefinitely disabled loading screen: nothing retried it, and every
    // action rendered in that state is a no-op.
    api.getStatus.mockRejectedValueOnce(new Error("router closed")).mockResolvedValue(status(true));
    renderContent();

    const failure = await screen.findByTestId("setup-status-error");
    expect(failure.textContent).toContain("router closed");
    expect(screen.getByText("Loading plugin status…")).toBeTruthy();
    fireEvent.click(within(failure).getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.queryByText("Loading plugin status…")).toBeNull());
    expect(await screen.findByText("Game.CT", undefined, { timeout: 4000 })).toBeTruthy();
  }, 8000);

  it.each(["User canceled", new Error("User canceled")])(
    "treats local-table picker cancellation as benign (%#)",
    async (reason) => {
      decky.openFilePicker.mockRejectedValueOnce(reason);
      renderContent();
      expect(await screen.findByText("Game.CT")).toBeTruthy();

      await pressLocalFile();
      await waitFor(() => expect(decky.openFilePicker).toHaveBeenCalledTimes(1));

      expect(api.inspectTableSource).not.toHaveBeenCalled();
      expect(api.importTable).not.toHaveBeenCalled();
      expect(screen.queryByTestId("panel-error")).toBeNull();
      expect(decky.toast).not.toHaveBeenCalled();
    },
  );

  it("surfaces a real local-table picker failure without importing", async () => {
    decky.openFilePicker.mockRejectedValueOnce(new Error("permission denied"));
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();

    await pressLocalFile();

    expect((await screen.findByTestId("panel-error")).textContent).toContain("permission denied");
    expect(api.inspectTableSource).not.toHaveBeenCalled();
    expect(api.importTable).not.toHaveBeenCalled();
    expect(failureShown()).toBe("permission denied");
  });

  it("imports a direct CT before trying to inspect it as an archive", async () => {
    // Import is the route that hashes unusable exact bytes and records them for
    // the selected game. Preflighting a direct CT through archive inspection
    // rejected a malformed table first, so the actual controller workflow
    // bypassed the durable not-working record the backend already implements.
    decky.openFilePicker.mockResolvedValueOnce({
      path: "/home/deck/Damaged.CT",
      realpath: "/home/deck/Damaged.CT",
    });
    api.importTable.mockRejectedValueOnce(new Error("table XML is not well-formed"));
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();

    await pressLocalFile();

    await waitFor(() => expect(api.importTable).toHaveBeenCalledWith(
      "/home/deck/Damaged.CT", null, null, 10,
    ));
    expect(api.inspectTableSource).not.toHaveBeenCalled();
    expect(failureShown()).toBe("table XML is not well-formed");
  });

  it("reports a local import that committed, whatever the read after it does", async () => {
    // With no game chosen the file joins this device's library and that is the
    // whole of what the press promised. The status read after it is
    // reconciliation: awaited, one that failed for a moment reported the import
    // as failed for a table that is on the device, and the reader's next move
    // is to import it again.
    const blank = status(true);
    blank.profiles = [];
    api.getStatus.mockResolvedValue(blank);
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    decky.openFilePicker.mockResolvedValueOnce({
      path: "/home/deck/Table.CT", realpath: "/home/deck/Table.CT",
    });
    api.importTable.mockResolvedValueOnce({ sha256: "f".repeat(64), filename: "Table.CT" });
    renderContent();
    await waitFor(() => expect(api.getStatus).toHaveBeenCalled());
    api.getStatus.mockRejectedValue(new Error("the backend did not answer"));

    await pressLocalFile();

    await waitFor(() => expect(api.importTable).toHaveBeenCalledWith(
      "/home/deck/Table.CT", null, null, null,
    ));
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
      body: "Table.CT is on this device. Choose a game to use it.",
    })));
    expect(failureShown()).toBeNull();
  });

  it.each(["User canceled", new Error("User canceled")])(
    "treats Cheat Engine picker cancellation as benign (%#)",
    async (reason) => {
      decky.openFilePicker.mockRejectedValueOnce(reason);
      renderContent();
      expect(await screen.findByText("Game.CT")).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: "Advanced…" }));
      const advanced = await waitFor(() => {
        const node = modalState.nodes.find((item: any) => item.type === AdvancedModal);
        expect(node).toBeTruthy();
        return node;
      });

      await advanced.props.onPickCE();

      expect(api.importCE).not.toHaveBeenCalled();
      expect(screen.queryByTestId("panel-error")).toBeNull();
      expect(decky.toast).not.toHaveBeenCalled();
    },
  );

  it("surfaces a real Cheat Engine picker failure without importing", async () => {
    decky.openFilePicker.mockRejectedValueOnce(new Error("permission denied"));
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Advanced…" }));
    const advanced = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === AdvancedModal);
      expect(node).toBeTruthy();
      return node;
    });

    await expect(advanced.props.onPickCE()).rejects.toThrow("permission denied");

    expect((await screen.findByTestId("panel-error")).textContent).toContain("permission denied");
    expect(api.importCE).not.toHaveBeenCalled();
    expect(failureShown()).toBe("permission denied");
  });

  it("does not report a successful repair as failed when the optional setup capability is unreadable", async () => {
    // Managed install is optional and its failure is surfaced on its own, but it
    // was a hard member of the refresh chain that runs after a durable action.
    api.repairSessionState.mockResolvedValue({ discarded: true, was_symlink: false, app_id: 10 });
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();

    api.getManagedCECapability.mockRejectedValue(new Error("managed manifest unreadable"));
    fireEvent.click(await screen.findByRole("button", { name: "Advanced…" }));
    const advanced = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === AdvancedModal);
      expect(node).toBeTruthy();
      return node;
    });
    await advanced.props.onRepairSessionState();
    expect(api.repairSessionState).toHaveBeenCalledWith(10);
    expect(decky.toast).not.toHaveBeenCalledWith(expect.objectContaining({ body: "managed manifest unreadable" }));
  });

  it("auto-detects the one running library game and shows exact active table/live cheats", async () => {
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(screen.getByText(/Steam · game\.exe/)).toBeTruthy();
    expect(screen.getByText("1 active")).toBeTruthy();
    expect(screen.getByText("Health")).toBeTruthy();
  });

  it("reads its own runtime lines in place and keeps a stop for the ones the backend wrote", async () => {
    // Home spends almost all of its time on an attached session, so a stop on
    // that line is one the ring rests on with nothing to press on every pass
    // down a panel of controls. It had one because the line was cut to the row
    // and a cut line has to be openable - "Cheat Engine is not running for this
    // table. Details …" in a 300 pixel panel, on the television as well as on
    // the handheld. A line read in place wraps instead, so there is nothing left
    // on it to open. A message the backend wrote has no bounded length and
    // wrapping the whole of it would push the panel's own controls down the
    // screen, so that one keeps its stop and stays cut to the line.
    renderContent();
    expect(await screen.findByText("1 active")).toBeTruthy();
    const connected = screen.getByTestId("runtime-row");
    expect(connected.querySelector('[data-testid="focusable"]')).toBeNull();
    expect(connected.querySelector(".ce-decky-ellipsis")).toBeNull();

    cleanup();
    const refused = () => {
      const runtime = liveRuntime();
      runtime.status.table_load_state = "failed";
      runtime.status.table_load_error = "Cheat Engine refused to open the table";
      return runtime;
    };
    api.getRuntimeStatus.mockResolvedValue(refused());
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: refused(), results: refused().status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: refused(), results: refused().status.results, unavailable: [] });
    renderContent();
    const failed = await screen.findByTestId("runtime-row");
    await waitFor(() => expect(failed.textContent).toContain("Cheat Engine refused to open the table"));
    expect(failed.querySelector('[data-testid="focusable"]')).toBeTruthy();
    expect(failed.querySelector(".ce-decky-ellipsis")).toBeTruthy();
  });

  it("says the table was not opened instead of presenting an empty session as connected", async () => {
    // Cheat Engine attached with an empty address list answers "missing" for
    // every record, so no cheat has a switch and none of them can be pinned -
    // which used to be indistinguishable from a table CE Decky cannot support.
    const failed = () => {
      const runtime = liveRuntime();
      runtime.status.table_load_state = "failed";
      runtime.status.table_load_error = "Cheat Engine refused to open the table";
      return runtime;
    };
    api.getRuntimeStatus.mockResolvedValue(failed());
    // The live snapshot republishes the envelope it read, so the failure has to
    // survive that too or the row goes back to reporting a healthy session.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: failed(), results: failed().status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: failed(), results: failed().status.results, unavailable: [] });
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    // The finding leads: a short cause the backend named becomes the label,
    // which is the first thing on the row, and the generic wording it replaced
    // said only what the row already means.
    const row = screen.getByTestId("runtime-row");
    await waitFor(() => expect(row.textContent).toContain("Cheat Engine refused to open the table"));
    expect(row.textContent).not.toContain("Table not loaded");
    expect(row.textContent?.slice(0, 40)).toContain("Cheat Engine refused");
    expect(row.textContent).toContain("Stop Cheat Engine and start it again");
  });

  it("leads the line with a cause too long to be a label", async () => {
    // Longer than a label can carry, so it stays in the description - but at
    // the front of it, because the row is cut to one line and the front is
    // what the reader gets. Our own framing follows it.
    const long = "the table's script refused to run and Cheat Engine reported that its address list stayed empty";
    const failed = () => {
      const runtime = liveRuntime();
      runtime.status.table_load_state = "failed";
      runtime.status.table_load_error = long;
      return runtime;
    };
    api.getRuntimeStatus.mockResolvedValue(failed());
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: failed(), results: failed().status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: failed(), results: failed().status.results, unavailable: [] });
    renderContent();

    const row = await screen.findByTestId("runtime-row");
    await waitFor(() => expect(row.textContent).toContain(long));
    expect(row.textContent).toContain("Table not loaded");
    expect(row.textContent?.indexOf(long)).toBeLessThan(row.textContent!.indexOf("Stop Cheat Engine"));
  });

  it("does not offer live cheats for a session whose table was never opened", async () => {
    // The row above says what happened; this is the other half of it. Attached
    // is not loaded, and every record in that session answers "missing", so
    // nothing here may treat it as a session live control can run from.
    const failed = () => {
      const runtime = liveRuntime();
      runtime.status.table_load_state = "failed";
      runtime.status.table_load_error = "Cheat Engine refused to open the table";
      return runtime;
    };
    api.getRuntimeStatus.mockResolvedValue(failed());
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: failed(), results: failed().status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: failed(), results: failed().status.results, unavailable: [] });
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    // The pinned cheat of a healthy session is on this panel; this one has none
    // to offer, because the table it would come from is not in Cheat Engine.
    expect(screen.queryByText("1 active")).toBeNull();
  });

  it("asks before recording anything when Cheat Engine refuses one of its cheats", async () => {
    // Enabling cheats happens inside the configurator, so a message on the Home
    // panel is one a user never sees: the user is asked once, in front of
    // everything else. Nothing durable is written until that is answered -
    // marking first meant the answer could not undo it without a second write,
    // and every way of dismissing the dialog had to count as one of the two
    // answers, the controller's own Back button included.
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const pickerIndex = modalState.nodes.findIndex((node: any) => node.type === CheatSelectionModal);
    const picker = modalState.nodes[pickerIndex];

    await picker.props.onTableRefused("“Init” did not switch on: Cheat Engine ran it and it went straight back off.");

    // This screen goes first. The question opens as a window of its own, and
    // raised while the picker is still up it is a window behind a window: on
    // the device that is a toast saying the table did not work and nothing
    // else, with Apply available again and no way to reach the one answer that
    // helps. A table that ran a cheat and had it come straight back off is not
    // a thing to go on configuring either.
    expect(modalState.closes[pickerIndex]).toHaveBeenCalled();

    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });
    expect(confirm.props.strTitle).toBe("This table did not work");
    // Asked, never done silently: nothing is recorded and nothing is withdrawn.
    expect(api.blockTable).not.toHaveBeenCalled();
    expect(api.setExecutionConsent).not.toHaveBeenCalled();
  });

  it("writes nothing at all when the table is kept, however the dialog is dismissed", async () => {
    // Steam routes the controller's Back button to `onCancel`, the same handler
    // the "Keep it" button uses, so this one press has to be safe: a reflex
    // dismissal must not leave durable state behind, and keeping a table has to
    // keep it usable rather than leaving it marked as not working.
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);
    await picker.props.onTableRefused("“Init” did not switch on.");
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });
    // What the press actually does: the bytes stay, search goes on showing the
    // copy, and what the mark costs is using it again until it is cleared.
    expect(confirm.props.strDescription).not.toMatch(/search stops offering/);
    expect(confirm.props.strDescription).toMatch(/stays on this device/);
    expect(confirm.props.strDescription).toMatch(/until you clear the mark/);

    await act(async () => { confirm.props.onCancel(); });

    // Nothing was recorded, so nothing has to be undone: no mark to clear, no
    // authorization touched, no automatic loading changed.
    expect(api.blockTable).not.toHaveBeenCalled();
    expect(api.unblockTable).not.toHaveBeenCalled();
    expect(api.setExecutionConsent).not.toHaveBeenCalled();
    expect(api.setAutoload).not.toHaveBeenCalled();
  });

  it("reports an uncertain Failed write from exact readback without repeating it", async () => {
    api.blockTable.mockImplementation(async () => {
      api.listBlockedTables.mockResolvedValue({ schema: 1, reason: null, tables: [{ key: SHA, sha256: SHA, reason: "failed", cause: "refused", origins: [] }] });
      throw Object.assign(new Error("directory sync failed"), { pythonTraceback: "Traceback (most recent call last):\nDurabilityUnknownError: replaced" });
    });
    renderContent();
    const picker = await openPicker();
    await picker.props.onTableRefused("Init did not switch on.");
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });
    await act(async () => { confirm.props.onOK(); });
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({ body: expect.stringContaining("It is marked as not working") })));
    expect(api.blockTable).toHaveBeenCalledTimes(1);
  });

  it("still stops using the table when the mark cannot be recorded", async () => {
    // The user asked to stop using this table, and that has to happen whatever
    // the bookkeeping does. Best effort on top of a failure they have already
    // been told about: nothing here may replace it with one about a record.
    api.blockTable.mockRejectedValue(new Error("state is read-only"));
    api.setExecutionConsent.mockResolvedValue({});
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);
    await picker.props.onTableRefused("“Init” did not switch on.");
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });

    await act(async () => { confirm.props.onOK(); });

    await waitFor(() => expect(api.blockTable).toHaveBeenCalledWith(SHA, "“Init” did not switch on.", 10));
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.stringContaining("could not record"),
    })));
  });

  it("offers the repaired copy where a pattern is missing and the repair is proven", async () => {
    // The other door to the same press. Review can only ask the scan question
    // where this device knows which program the game runs, so a table that got
    // past Review unchecked meets the offer here - the first time a cheat from
    // it is switched on and comes straight back off.
    api.checkTableScans.mockResolvedValueOnce({
      source: "file", present: ["aobHealth"], missing: ["aobSpeed"], not_checked: [],
      elapsed_ms: 690, reason: null, repairable: true, ambiguous: [],
    });
    api.prepareTableCopy.mockResolvedValue({ ...table, sha256: "f".repeat(64) });
    renderContent();
    const picker = await openPicker();
    await picker.props.onTableRefused("“Init” did not switch on.");
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });

    // The answer that might leave them with a working table comes before the
    // one that retires it, and the pattern is named rather than implied.
    expect(confirm.props.strOKButtonText).toBe("Try a repaired copy");
    expect(confirm.props.strMiddleButtonText).toBe("Stop using it");
    expect(confirm.props.strCancelButtonText).toBe("Keep it");
    expect(confirm.props.strDescription).toContain("aobSpeed");

    await act(async () => { confirm.props.onOK(); });

    // Nothing durable: the copy is new bytes, and the consent for them is given
    // on the Review this opens rather than here.
    await waitFor(() => expect(api.prepareTableCopy).toHaveBeenCalledWith(SHA, 10, null));
    expect(api.blockTable).not.toHaveBeenCalled();
    expect(api.revokeTable).not.toHaveBeenCalled();
  });

  it("falls back to the two answers it has always had when no repair can be proved", async () => {
    // A pattern is missing and the rest of that script still needs the code
    // that would have to go. The dialog says so rather than offering a press
    // that refuses itself.
    api.checkTableScans.mockResolvedValueOnce({
      source: "file", present: [], missing: ["aobSpeed"], not_checked: [],
      elapsed_ms: 12, reason: null, repairable: false, ambiguous: [],
    });
    renderContent();
    const picker = await openPicker();
    await picker.props.onTableRefused("“Init” did not switch on.");
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });

    expect(confirm.props.strOKButtonText).toBe("Stop using it");
    expect(confirm.props.strMiddleButtonText).toBeUndefined();
    expect(confirm.props.strDescription).toContain("cannot make a copy without that pattern");
  });

  it("says what makes room when the record list has none", async () => {
    // The advisory list is bounded and refuses rather than dropping somebody
    // else's decision. That refusal names a press the user can reach, and
    // throwing it away left them told that something failed and nothing they
    // could do about it.
    const full = "the list of tables that did not work is full; clear one under Advanced, Tables that did not work, to record another";
    api.blockTable.mockRejectedValue(new Error(full));
    api.setExecutionConsent.mockResolvedValue({});
    renderContent();
    const picker = await openPicker();
    await picker.props.onTableRefused("“Init” did not switch on.");
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });

    await act(async () => { confirm.props.onOK(); });

    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.stringContaining("Tables that did not work"),
    })));
    // The table is still let go of, which is what the user actually asked for.
    await waitFor(() => expect(api.revokeTable).toHaveBeenCalledWith(10, SHA));
  });

  it("detaches and disarms through one command when the user stops using a table", async () => {
    api.blockTable.mockResolvedValue({});
    renderContent();
    const picker = await openPicker();
    await picker.props.onTableRefused("Init did not switch on.");
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });
    await act(async () => { confirm.props.onOK(); });
    await waitFor(() => expect(api.revokeTable).toHaveBeenCalledWith(10, SHA));
    expect(api.setAutoload).not.toHaveBeenCalled();
    expect(api.saveProfile).not.toHaveBeenCalled();
    expect(await screen.findByText("No table selected")).toBeTruthy();
  });

  it("says on Home when the selected table is one recorded as not working", async () => {
    // Keeping the table is one of the two answers the refusal dialog offers,
    // and the same bytes can be marked while another game was playing them, so
    // a marked table can legitimately still be selected. It stays usable - the
    // record is advice, not a trust decision - but the row has to say so.
    api.listBlockedTables.mockResolvedValue({
      schema: 1, reason: null,
      tables: [{ sha256: SHA, reason: "“Init” did not switch on.", filename: "Game.CT", app_id: 10, game_name: "Game", game_version: null, recorded_at: 1, origins: [] }],
    });
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    // Nothing about the mark belongs to auto-load; leaving it armed would start
    // a launch this case never asked for.
    const snapshot = status();
    snapshot.profiles[0].autoload_enabled = false;
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();

    expect(await screen.findByText(/marked as not working/)).toBeTruthy();
    // Advice, never a refusal: the user who kept it keeps being able to run it.
    await waitFor(() => expect((screen.getByRole("button", { name: "Load table & start CE" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it.each(["unusable", "encrypted", "gone"] as const)(
      "never calls the selected table not working for a %s record", async (cause) => {
    // A condition of a download or of a source says nothing about the table
    // this game is using, and on Home it would read as "the table you are
    // playing does not work". It belongs to the row that download came from and
    // to the full list under Advanced.
    api.listBlockedTables.mockResolvedValue({
      schema: 1, reason: null,
      tables: [{ sha256: SHA, reason: "there is no table in it", filename: "Game.CT", app_id: 10,
        game_name: "Game", game_version: null, recorded_at: 1, origins: [], cause }],
    });
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    const snapshot = status();
    snapshot.profiles[0].autoload_enabled = false;
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect(api.listBlockedTables).toHaveBeenCalled());
    expect(screen.queryByText(/marked as not working/)).toBeNull();
  });

  it("re-reads the authority when the panel is shown again", async () => {
    // Work started by a press outlives the panel that started it. Steam
    // recreates this browser view when a game returns to the foreground, which
    // is what stopping Cheat Engine does, so on the target the withdrawal
    // landed 340 ms after the replacement panel had already read the profile.
    // Nothing polls status, so that panel offered Load table & start CE for an
    // authorization that no longer existed until the plugin was reloaded.
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    const cleared = status(true);
    cleared.profiles[0].table_sha256 = null;
    cleared.profiles[0].execution_consent_sha256 = null;

    await act(async () => { quickAccess.visible = false; quickAccess.listeners.forEach((notify) => notify()); });
    api.getStatus.mockResolvedValue(cleared);
    await act(async () => { quickAccess.visible = true; quickAccess.listeners.forEach((notify) => notify()); });

    expect(await screen.findByText("No table selected")).toBeTruthy();
  });

  it("re-reads the authority on a panel that is created already visible", async () => {
    // Steam recreates the browser view while the quick-access panel is open, so
    // the replacement mounts with the loader already reporting `true` and never
    // sees a visibility transition at all. Its own first read races the write
    // the previous panel started, and the reread used to be skipped outright
    // because there was no status to compare against yet, which left the stale
    // authorization on screen for the rest of the session.
    const cleared = status(true);
    cleared.profiles[0].table_sha256 = null;
    cleared.profiles[0].execution_consent_sha256 = null;
    quickAccess.visible = true;
    // The first read is the one that raced the write; every later one sees it.
    api.getStatus.mockResolvedValueOnce(status(true));
    api.getStatus.mockResolvedValue(cleared);
    renderContent();

    // No visibility transition happens here at all, so the reread has to be
    // driven by the first status arriving while the panel is already on screen.
    await waitFor(() => expect(api.getStatus.mock.calls.length).toBeGreaterThan(1));
    await waitFor(() => expect(steam.readAppDetails).toHaveBeenCalled());
    expect(await screen.findByText("No table selected")).toBeTruthy();
    expect(screen.queryByText("Game.CT")).toBeNull();
  });

  it("catches up on a write that lands after the panel has read twice", async () => {
    // The immediate reread is issued one round trip after the panel's own first
    // read, which is not a moment related to the write it is racing: on the
    // target the withdrawal landed 340 ms after the replacement panel had read
    // the profile, and two reads that close together both miss it. Nothing
    // rereads after that, because the panel was created visible and stays
    // visible, so the stale authorization survives for the rest of the session.
    const cleared = status(true);
    cleared.profiles[0].table_sha256 = null;
    cleared.profiles[0].execution_consent_sha256 = null;
    quickAccess.visible = true;
    // Both of the reads the panel makes for itself land before the write does.
    api.getStatus.mockResolvedValueOnce(status(true));
    api.getStatus.mockResolvedValueOnce(status(true));
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect(api.getStatus.mock.calls.length).toBe(2));
    // The panel it replaced commits its withdrawal only now, and this panel is
    // still visible, so no visibility change is ever going to tell it.
    api.getStatus.mockResolvedValue(cleared);

    expect(await screen.findByText("No table selected", undefined, { timeout: 5000 })).toBeTruthy();
    // Once, not repeatedly: the catch-up must not have become a poll.
    const settled = api.getStatus.mock.calls.length;
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, PANEL_CATCH_UP_DELAY_MS * 2)); });
    expect(api.getStatus.mock.calls.length).toBe(settled);
  }, 15000);

  it("edits a table past the live budget as stored configuration, not as a live session", async () => {
    // Apply mutates and then re-reads every safe control to confirm, and both
    // exceed the same limit on a table this size, so a mutation that had
    // actually succeeded came back as a partial or unknown outcome. It is also
    // not "Cheat Engine is not running": the session is there and fine, and
    // saying otherwise sends the user looking for one.
    const big = { ...inspect, controls: Array.from({ length: MAX_LIVE_CONTROLS + 1 }, (_, index) => ({
      id: index + 1, description: `Cheat ${index + 1}`, path: [`Cheat ${index + 1}`],
      variable_type: "4 Bytes", kind: "value", group_header: false,
      has_assembler_script: false, dropdown_values: [], dropdown_read_only: false,
    })) };
    api.inspectTableSha.mockResolvedValue(big);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));

    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);
    // The session is live, and the picker is told exactly why it cannot use it.
    expect(picker.props.live).toBe(true);
    expect(picker.props.liveUnavailableReason).toContain(String(MAX_LIVE_CONTROLS));
  });

  it("drives an ordinary table live, with no stored-only reason", async () => {
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));

    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);
    expect(picker.props.liveUnavailableReason).toBeNull();
  });

  it("asks once for one table, however many times Apply is refused", async () => {
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);

    await picker.props.onTableRefused("refused once");
    await picker.props.onTableRefused("refused again");

    // Every press refuses the same way; one dialog for one table is the message.
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === DeckyConfirmModal)).toHaveLength(1));
    expect(modalState.nodes.filter((node: any) => node.type === DeckyConfirmModal)).toHaveLength(1);
    expect(api.blockTable).not.toHaveBeenCalled();
  });

  it("stops its live sweep when the panel is dismissed", async () => {
    // A refresh walks every actionable record in chunks, which for a large
    // table is over a hundred bridge round-trips. Closing the panel used to
    // leave all of them running against a component that no longer exists: the
    // generation counter stopped the result being adopted, never the work.
    const view = renderContent();
    await waitFor(() => expect(runtimeClient.queryRuntimeControlsPartial).toHaveBeenCalledTimes(1));
    const signal = runtimeClient.queryRuntimeControlsPartial.mock.calls[0][3] as AbortSignal;
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal.aborted).toBe(false);

    view.unmount();

    expect(signal.aborted).toBe(true);
  });

  it("does not start a duplicate auto-load when the exact runtime is already connected", async () => {
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(await screen.findByText("1 active")).toBeTruthy();
    await waitFor(() => expect(runtimeClient.queryRuntimeControlsPartial).toHaveBeenCalledTimes(1));

    expect(api.prepareSession).not.toHaveBeenCalled();
    expect(api.launchCEForGame).not.toHaveBeenCalled();
  });

  it("drops an automatic game selection as soon as the running-game context becomes ambiguous", async () => {
    const secondGame = { appId: 11, name: "Other", sortAs: "Other", isShortcut: false };
    steam.listRunningGames
      .mockResolvedValueOnce({ available: true, games: [game] })
      .mockResolvedValue({ available: true, games: [game, secondGame] });
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();

    await waitFor(() => expect(steam.listRunningGames.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 });
    expect(await screen.findByText("No game selected")).toBeTruthy();
    expect(screen.getByText("2 games running; choose one")).toBeTruthy();
  }, 6000);

  it("keeps the Cheat Engine owner visible after an ambiguous observation clears the game context", async () => {
    // Ownership is launcher-global, so losing the game context must not lose it:
    // Home used to drop the owner, hide Stop and re-offer setup actions the
    // backend still had to reject.
    const secondGame = { appId: 11, name: "Other", sortAs: "Other", isShortcut: false };
    api.getCELaunchCapability.mockResolvedValue({
      ...launch, owned_launch_owners: [{ app_id: 10, state: "matched", recovered: true }],
    });
    steam.listRunningGames
      .mockResolvedValueOnce({ available: true, games: [game] })
      .mockResolvedValue({ available: true, games: [game, secondGame] });
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();

    await waitFor(() => expect(steam.listRunningGames.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 });
    expect(await screen.findByText("No game selected")).toBeTruthy();
    expect(screen.getByTestId("ce-owned-elsewhere").textContent).toContain("Game");
    // The launcher-global snapshot is re-read on the detector cadence rather
    // than only when a game happens to be selected.
    await waitFor(() => expect(api.getCELaunchCapability).toHaveBeenCalledWith(null));
  }, 6000);

  it("refuses to attach at all when the game is running a known anti-cheat", async () => {
    // The observed process set already identified the launcher; it was used to
    // route around it and then discarded, so Start and Auto-load proceeded for
    // a game the documented boundary says to refuse. Nothing here touches the
    // anti-cheat itself.
    api.getCELaunchCapability.mockResolvedValue({
      ...launch,
      game: { ...launch.game, windows_executables: ["Game-Win64-Shipping.exe", "BELauncher.exe"] },
    });
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(await screen.findByText(/offline and single-player/)).toBeTruthy();
    expect((screen.getByRole("button", { name: "Load table & start CE" }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(steam.listRunningGames.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 });
    expect(api.launchCEForGame).not.toHaveBeenCalled();
  }, 6000);

  it("wakes Auto-load when the target this table is for finally appears", async () => {
    // The ordinary shape of a game that starts through a launcher: the prefix
    // holds `Launcher.exe` first and the game itself seconds later, with the
    // launch observation reporting `running` throughout. Auto-load correctly
    // declines to launch against a target that is proven absent, and then had
    // nothing to wake it: the effect watched whether the game was running,
    // which never changed, so the automatic workflow stayed asleep for the rest
    // of the session while the manual press beside it worked.
    let observed = ["Launcher.exe"];
    api.getCELaunchCapability.mockImplementation(async () => ({
      ...launch, game: { ...launch.game, windows_executables: observed },
    }));
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    api.prepareSession.mockResolvedValue({});
    api.launchCEForGame.mockResolvedValue({
      operation_id: "op", app_id: 10, mode: "attached", state: "connected",
      session_id: "session", message: "connected", error: null,
    });
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect(api.getCELaunchCapability.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 });
    expect(api.launchCEForGame).not.toHaveBeenCalled();

    observed = ["game.exe"];
    await waitFor(() => expect(api.launchCEForGame).toHaveBeenCalledTimes(1), { timeout: 6000 });
  }, 12000);

  it("refuses to start or auto-load against a target this game is not running", async () => {
    // The panel already proved the absence and said so in its own row, telling
    // the reader to repair the target before starting - and left the press
    // underneath live. The attach then waits for a process that is not there,
    // which on this panel looks exactly like Cheat Engine failing.
    api.getCELaunchCapability.mockResolvedValue({
      ...launch,
      game: { ...launch.game, windows_executables: ["Launcher.exe"] },
    });
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    // Said once, in the row under the press that it disables, and carrying the
    // repair: what the game did start, and where the target is set.
    expect(screen.getByText("Cannot start yet")).toBeTruthy();
    expect(screen.getByText(/game\.exe is not running in this game\. This game is running Launcher\.exe/)).toBeTruthy();
    expect(screen.queryByTestId("panel-target-not-running")).toBeNull();
    expect((screen.getByRole("button", { name: "Load table & start CE" }) as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect(steam.listRunningGames.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 });
    expect(api.launchCEForGame).not.toHaveBeenCalled();
  }, 6000);

  it("still starts where the absence is not proven", async () => {
    // An observation that saw nothing is a prefix that has not got going, not a
    // game running the wrong program, and a warning a reader cannot act on is
    // worse than none. The default fixture is exactly that case.
    const snapshot = status(true);
    // Off, because what this is about is the press being offered rather than
    // what happens when something takes it.
    snapshot.profiles[0].autoload_enabled = false;
    api.getStatus.mockResolvedValue(snapshot);
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(screen.queryByTestId("panel-target-not-running")).toBeNull();
    await waitFor(() => expect((screen.getByRole("button", { name: "Load table & start CE" }) as HTMLButtonElement).disabled).toBe(false));
  }, 6000);

  it("refuses to start or auto-load while another game owns Cheat Engine", async () => {
    api.getCELaunchCapability.mockResolvedValue({
      ...launch, owned_launch_owners: [{ app_id: 99, state: "matched", recovered: true }],
    });
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    // Setup names the owner, and the Cheats section refuses the start for the
    // same launcher-global reason instead of offering a button that must fail.
    expect((await screen.findAllByText(/Cheat Engine is still running for AppID 99/)).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Cannot start yet")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Load table & start CE" }) as HTMLButtonElement).disabled).toBe(true);
    // Auto-load must not enter its retry loop against an impossible global
    // precondition.
    await waitFor(() => expect(steam.listRunningGames.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 4000 });
    expect(api.launchCEForGame).not.toHaveBeenCalled();
  }, 6000);

  it("fails Home closed when connected runtime identity does not match the prepared exact descriptor", async () => {
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(await screen.findByText("Not connected")).toBeTruthy();
    // The picker stays reachable so this table can still be set up, but it must
    // never read or write the session whose identity does not match.
    expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    expect(modalState.nodes.find((node: any) => node.type === CheatSelectionModal).props.live).toBe(false);
    expect(runtimeClient.queryRuntimeControlsPartial).not.toHaveBeenCalled();
  });

  it("switches auto-load on when cheats are configured with nothing running", async () => {
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    const snapshot = status();
    snapshot.profiles[0].autoload_enabled = false;
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(await screen.findByText("Not connected")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);

    // With nothing running the selection can only reach Cheat Engine through
    // auto-load, so switching a cheat on there is the request to apply it.
    // What the picker owes the user is saying so before Apply, not a second
    // control to find.
    expect(picker.props.autoloadEnabled).toBe(false);
    await picker.props.onApplied([{ record_id: 7, active: true, value: "100" }], null);
    await waitFor(() => expect(api.setAutoload).toHaveBeenCalledWith(10, SHA, true));
    expect(api.setRememberedCheats).toHaveBeenCalledWith(10, SHA, [{ record_id: 7, active: true, value: "100" }]);
  });

  it("leaves auto-load alone when a value was typed but no cheat was switched on", async () => {
    // Typing a value and pinning a row is configuring the table, not asking for
    // it to run: the value belongs to a cheat that is still off, and the
    // runtime already declines to write one for a cheat that is off. Arming
    // auto-load here started Cheat Engine with the game for nothing.
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    const snapshot = status();
    snapshot.profiles[0].autoload_enabled = false;
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);

    await picker.props.onApplied([{ record_id: 7, active: null, value: "100" }], null);
    await waitFor(() => expect(api.setRememberedCheats).toHaveBeenCalledWith(10, SHA, [{ record_id: 7, active: null, value: "100" }]));
    expect(api.setAutoload).not.toHaveBeenCalled();
  });

  it("says Apply will start Cheat Engine with the game before the user presses it", async () => {
    // The behaviour is intended; the defect was that the first time the user
    // heard about it was the toast after the durable commit.
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    const snapshot = status();
    snapshot.profiles[0].autoload_enabled = false;
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));

    render(modalState.nodes.find((node: any) => node.type === CheatSelectionModal));
    // One line on the row, and the consequence a press has behind its `?`. The
    // three sentences used to be the row itself, which on a handheld wrapped to
    // five lines above a list that is counted in rows.
    const banner = await screen.findByTestId("cheats-stored-only");
    expect(banner.textContent).toContain("Apply saves this table's selection");
    expect(screen.queryByText(/switches on Load last table & cheats/)).toBeNull();
    fireEvent.click(within(banner).getByRole("button", { name: "?" }));
    expect(await screen.findByText(/switches on Load last table & cheats/)).toBeTruthy();
  });

  it("does not repeat the arming disclosure when auto-load is already on", async () => {
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));

    render(modalState.nodes.find((node: any) => node.type === CheatSelectionModal));
    await screen.findByText("Cheat Engine is not running");
    expect(screen.queryByText(/switches on Load last table & cheats/)).toBeNull();
  });

  it("switches auto-load off when the last cheat is switched off with nothing running", async () => {
    // Arming and disarming ask the same question, so a table left with every
    // cheat off does not keep starting Cheat Engine on the next launch for
    // nothing. The value stays stored for whenever the cheat is switched back
    // on; it is not work auto-load has to do on its own.
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    const snapshot = status();
    snapshot.profiles[0].autoload_enabled = true;
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);

    await picker.props.onApplied([{ record_id: 7, active: false, value: "100" }], null);
    await waitFor(() => expect(api.setAutoload).toHaveBeenCalledWith(10, SHA, false));
    expect(api.setRememberedCheats).toHaveBeenCalledWith(10, SHA, [{ record_id: 7, active: false, value: "100" }]);
  });

  it("reopens with nothing on after the last cheat was switched off", async () => {
    // The third thing the disarm has to be true of, and the one a reader
    // actually sees: the picker opened again with nothing running seeds every
    // row off and turns on only what this table was configured to do. The value
    // stays stored for whenever the cheat is switched back on.
    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[{ record_id: 7, active: false, value: "100" }]}
      configuredValues={[]}
      autoloadEnabled={false}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 0, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={vi.fn()}
      onCancel={vi.fn()}
    />);

    const row = await screen.findByTestId("cheat-row-7");
    expect((within(row).getByTestId("toggle") as HTMLInputElement).checked).toBe(false);
    expect(row.textContent).toContain("100");
  });

  it("leaves auto-load alone when a cheat is still on", async () => {
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    const snapshot = status();
    snapshot.profiles[0].autoload_enabled = true;
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);

    await picker.props.onApplied([
      { record_id: 7, active: true, value: null },
      { record_id: 9, active: false, value: null },
    ], null);
    await waitFor(() => expect(api.setRememberedCheats).toHaveBeenCalled());
    expect(api.setAutoload).not.toHaveBeenCalled();
  });

  it("never labels live cheats or opens the picker with an inspection from another exact table SHA", async () => {
    // A partially failed activation can leave a newer inspection cached while the
    // backend profile still points at the previous exact SHA.
    api.inspectTableSha.mockResolvedValue({ ...inspect, sha256: "7".repeat(64) });
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(await screen.findByText("0 active")).toBeTruthy();
    expect(screen.queryByText("Health")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    expect(await screen.findByText(/no longer matches this game's selected exact table SHA/)).toBeTruthy();
    expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(0);
  });

  it("requires one explicit confirmation before a managed reinstall transaction starts", async () => {
    api.getManagedCECapability.mockResolvedValue({
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: null,
    });
    api.getCELaunchCapability.mockResolvedValue({ ...launch, game: { ...launch.game, running: false } });
    renderContent();

    const reinstall = await screen.findByRole("button", { name: /^(Reinstall|Install)$/ });
    fireEvent.click(reinstall);
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    expect(modalState.nodes[0].type).toBe(DeckyConfirmModal);
    expect(api.startManagedCEInstall).not.toHaveBeenCalled();

    modalState.nodes[0].props.onCancel();
    expect(api.startManagedCEInstall).not.toHaveBeenCalled();
  });

  it("clears stale reinstall progress after the backend consumes a completed operation", async () => {
    const capability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: null,
    };
    const extracting = {
      operation_id: "f".repeat(32), state: "extracting", progress: null,
      message: "Extracting the reviewed Cheat Engine files locally", error: null, installed: null,
    };
    api.getManagedCECapability.mockResolvedValue(capability);
    api.startManagedCEInstall.mockResolvedValue(extracting);
    api.pollManagedCEInstall.mockResolvedValue({ ...extracting, state: "completed", message: "Managed Cheat Engine is ready", installed: { root: "/managed", executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), size: 1 } });
    api.completeManagedCEInstall.mockResolvedValue({ root: "/managed", executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), size: 1, completed_now: true });
    renderContent();

    fireEvent.click(await screen.findByRole("button", { name: /^(Reinstall|Install)$/ }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    modalState.nodes[0].props.onOK();
    await waitFor(() => expect(api.completeManagedCEInstall).toHaveBeenCalledWith("f".repeat(32)));
    await waitFor(() => expect(screen.queryByTestId("setup-progress")).toBeNull());
    expect((screen.getByRole("button", { name: /^(Reinstall|Install)$/ }) as HTMLButtonElement).disabled).toBe(false);
    expect(decky.toast).toHaveBeenCalledTimes(1);
    expect(decky.toast).toHaveBeenCalledWith({ title: "CE Decky", body: "Cheat Engine installation was verified." });
  });

  it.each(["lost start", "lost poll", "rejected cancel", "cancelled refresh", "consumed completion"])("reconciles managed setup with one owner (%s)", async (scenario) => {
    resetSupportLog();
    const active = { operation_id: "7".repeat(32), state: "verifying", progress: null, message: "Verifying", error: null, installed: null };
    const completed = { ...active, state: "completed", installed: { root: "/managed", executable: "/managed/CE.exe", sha256: "a".repeat(64), size: 1 } };
    const capability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready", operation: active,
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
    };
    api.getManagedCECapability.mockResolvedValue(capability);
    api.pollManagedCEInstall.mockResolvedValue(completed);
    api.completeManagedCEInstall.mockImplementation(async () => {
      api.getManagedCECapability.mockResolvedValue({ ...capability, operation: null });
      return { ...completed.installed, completed_now: true };
    });
    if (scenario === "lost start") {
      api.getManagedCECapability.mockResolvedValueOnce({ ...capability, operation: null });
      api.startManagedCEInstall.mockRejectedValue(new Error("start receipt lost"));
    }
    if (scenario === "lost poll") api.pollManagedCEInstall.mockRejectedValueOnce(new Error("poll receipt lost"));
    let finishCancel!: () => void;
    if (scenario.includes("cancel") || scenario === "consumed completion") {
      api.cancelManagedCEInstall.mockImplementationOnce(() => new Promise((resolve, reject) => {
        finishCancel = () => {
          if (scenario === "rejected cancel") reject(new Error("cancel receipt lost"));
          else if (scenario === "cancelled refresh") {
            api.getManagedCECapability.mockRejectedValue(new Error("capability read failed"));
            resolve({ ...active, state: "cancelled" });
          } else {
            api.completeManagedCEInstall.mockRejectedValueOnce(new Error("completion receipt lost")).mockResolvedValue({ ...completed.installed, completed_now: false });
            api.getStatus.mockResolvedValue({ ...status(true), ce: { ...status(true).ce, executable: completed.installed.executable } });
            api.getManagedCECapability.mockResolvedValue({ ...capability, operation: null });
            resolve(completed);
          }
        };
      }));
    }
    renderContent();
    if (scenario === "lost start") {
      fireEvent.click(await screen.findByRole("button", { name: /^(Reinstall|Install)$/ }));
      await waitFor(() => expect(modalState.nodes.length).toBe(1));
      act(() => modalState.nodes[0].props.onOK());
    }
    if (scenario.includes("cancel") || scenario === "consumed completion") {
      fireEvent.click(await screen.findByRole("button", { name: "Cancel", exact: true }));
      expect((screen.getByRole("button", { name: "Cancelling…" }) as HTMLButtonElement).disabled).toBe(true);
      await act(async () => { finishCancel(); });
      if (scenario === "rejected cancel") await waitFor(() => expect((screen.getByRole("button", { name: "Cancel", exact: true }) as HTMLButtonElement).disabled).toBe(false));
    }
    if (scenario === "cancelled refresh") {
      await waitFor(() => expect(decky.toast).toHaveBeenCalledWith({ title: "CE Decky", body: "Cheat Engine setup cancelled." }));
      expect(readSupportLog().entries.some((entry) => entry.event === "ui.operation_failed" && entry.fields.operation === "managed_setup.cancel")).toBe(false);
    } else {
      await waitFor(() => expect(api.completeManagedCEInstall).toHaveBeenCalledWith(active.operation_id), { timeout: 2500 });
      await waitFor(() => expect(screen.queryByTestId("setup-progress")).toBeNull());
      await waitFor(() => expect((screen.getByRole("button", { name: "Advanced…" }) as HTMLButtonElement).disabled).toBe(false));
    }
    if (scenario === "lost poll") expect(api.pollManagedCEInstall).toHaveBeenCalledTimes(2);
    if (scenario === "rejected cancel") expect(api.pollManagedCEInstall).toHaveBeenCalled();
    expect(api.startManagedCEInstall).toHaveBeenCalledTimes(scenario === "lost start" ? 1 : 0);
  });

  it("does not let a late abandoned capability read restore setup after Cancel", async () => {
    const active = { operation_id: "7".repeat(32), state: "verifying", progress: null, message: "Verifying", error: null, installed: null };
    const capability = { schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true, network_download_enabled: true, native_extraction_enabled: true, reason: "ready", release: null, operation: active };
    let late!: (value: unknown) => void;
    api.getManagedCECapability.mockResolvedValueOnce(capability)
      .mockImplementationOnce(() => new Promise((resolve) => { late = resolve; }))
      .mockResolvedValue({ ...capability, operation: null });
    api.pollManagedCEInstall.mockRejectedValue(new Error("poll unavailable"));
    api.cancelManagedCEInstall.mockResolvedValue({ ...active, state: "cancelled" });
    renderContent();
    await waitFor(() => expect(late).toBeTypeOf("function"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel", exact: true }));
    // The same budget the cancellation cases above use: the owner is inside a
    // poll backoff with a capability read still outstanding, so how long the
    // latch takes to clear is a property of the machine rather than of the
    // panel, and one second is under it on a loaded CI runner.
    await waitFor(() => expect((screen.getByRole("button", { name: "Advanced…" }) as HTMLButtonElement).disabled).toBe(false), { timeout: 2500 });
    await act(async () => { late(capability); });
    expect(screen.getByTestId("setup-progress").textContent).toContain("cancelled");
    expect(screen.queryByRole("button", { name: "Cancel", exact: true })).toBeNull();
  });

  it.each(["cancelled", "completed", "failed"])("records managed cancellation start and one correlated outcome (%s)", async (outcome) => {
    resetSupportLog();
    const active = { operation_id: "7".repeat(32), state: "verifying", progress: null, message: "Verifying", error: null, installed: null };
    api.getManagedCECapability.mockResolvedValue({
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready", operation: null,
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
    });
    api.startManagedCEInstall.mockResolvedValue(active);
    api.pollManagedCEInstall.mockImplementation(() => new Promise(() => undefined));
    if (outcome === "failed") api.completeManagedCEInstall.mockRejectedValue(new Error("managed CE setup changed; refresh before completing it"));
    else api.completeManagedCEInstall.mockResolvedValue({ completed_now: true });
    let finish!: (value: unknown) => void;
    let fail!: (cause: Error) => void;
    api.cancelManagedCEInstall.mockImplementationOnce(() => new Promise((resolve, reject) => { finish = resolve; fail = reject; }));
    renderContent();
    fireEvent.click(await screen.findByRole("button", { name: /^(Reinstall|Install)$/ }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    modalState.nodes[0].props.onOK();
    const cancel = await screen.findByRole("button", { name: "Cancel" });
    act(() => { cancel.click(); cancel.click(); });
    expect(api.cancelManagedCEInstall).toHaveBeenCalledOnce();
    const press = readSupportLog().entries.find((entry) => entry.event === "ui.action" && entry.fields.action === "home_panel.cancel");
    const events = () => readSupportLog().entries.filter((entry) => entry.fields.operation === "managed_setup.cancel");
    expect(events()).toHaveLength(1);
    expect(events()[0]).toMatchObject({ event: "ui.operation_started", fields: { operation_id: active.operation_id, action: "home_panel.cancel", interaction: press?.fields.interaction } });
    await act(async () => { if (outcome === "failed") fail(new Error("cancel refused")); else finish({ ...active, state: outcome }); });
    await waitFor(() => expect(events().map((entry) => entry.event)).toEqual(["ui.operation_started", outcome === "failed" ? "ui.operation_failed" : "ui.operation_completed"]));
    expect(events()[1].fields.interaction).toBe(press?.fields.interaction);
  });

  it("registers a setup when cancellation arrives after native promotion", async () => {
    const verifying = {
      operation_id: "7".repeat(32), state: "verifying", progress: null,
      message: "Verifying the extracted Cheat Engine tree", error: null, installed: null,
    };
    const completed = {
      ...verifying, state: "completed",
      message: "Managed Cheat Engine is ready; cancellation arrived after the irreversible promotion commit point",
      installed: { root: "/managed", executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), size: 1 },
    };
    const activeCapability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: verifying,
    };
    api.getManagedCECapability
      .mockResolvedValueOnce({ ...activeCapability, operation: null })
      .mockResolvedValue({ ...activeCapability, operation: null });
    api.startManagedCEInstall.mockResolvedValue(verifying);
    api.cancelManagedCEInstall.mockResolvedValue(completed);
    api.completeManagedCEInstall.mockResolvedValue({ ...completed.installed, completed_now: true });
    api.pollManagedCEInstall.mockImplementation(() => new Promise(() => undefined));
    renderContent();

    fireEvent.click(await screen.findByRole("button", { name: /^(Reinstall|Install)$/ }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    modalState.nodes[0].props.onOK();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(api.completeManagedCEInstall).toHaveBeenCalledWith("7".repeat(32)));
    await waitFor(() => expect(screen.queryByText(/cancellation arrived after the irreversible/)).toBeNull());
    expect(decky.toast).toHaveBeenCalledWith({
      title: "CE Decky",
      body: "Cancellation arrived after promotion; Cheat Engine installation was verified.",
    });
  });

  it("keeps home usable when a superseded observer sees the already consumed completion", async () => {
    // Reproduces the packaged setup lockout: the winning monitor consumed the
    // completion, this observer's own poll still resolved with the terminal
    // `completed` snapshot, and its completion RPC was then rejected. The
    // retained snapshot used to hide Reinstall CE and disable the whole
    // workflow with no controller-reachable way out.
    const capability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: null,
    };
    const extracting = {
      operation_id: "c".repeat(32), state: "extracting", progress: null,
      message: "Extracting the reviewed Cheat Engine files locally", error: null, installed: null,
    };
    api.getManagedCECapability.mockResolvedValue(capability);
    api.startManagedCEInstall.mockResolvedValue(extracting);
    api.pollManagedCEInstall.mockResolvedValue({
      ...extracting, state: "completed", message: "Managed Cheat Engine is ready",
      installed: { root: "/managed", executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), size: 1 },
    });
    api.completeManagedCEInstall.mockRejectedValueOnce(new Error("completion receipt lost")).mockResolvedValue({ executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), root: "/managed", size: 1, completed_now: false });
    renderContent();

    fireEvent.click(await screen.findByRole("button", { name: /^(Reinstall|Install)$/ }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    modalState.nodes[0].props.onOK();
    await waitFor(() => expect(api.completeManagedCEInstall).toHaveBeenCalledWith("c".repeat(32)));

    const reinstall = await screen.findByRole("button", { name: /^(Reinstall|Install)$/ });
    await waitFor(() => expect((reinstall as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText("completed · Managed Cheat Engine is ready")).toBeNull();
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(false);
    expect(decky.toast).not.toHaveBeenCalledWith({ title: "CE Decky", body: "Cheat Engine installation was verified." });
  });

  it("does not toast when another setup observer already committed the exact completion", async () => {
    const completed = {
      operation_id: "e".repeat(32), state: "completed", progress: null,
      message: "Managed Cheat Engine is ready", error: null,
      installed: { root: "/managed", executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), size: 1 },
    };
    api.getManagedCECapability
      .mockResolvedValueOnce({
        schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
        network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
        release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
        operation: completed,
      })
      .mockResolvedValue({
        schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
        network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
        release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
        operation: null,
      });
    api.completeManagedCEInstall.mockResolvedValue({ ...completed.installed, completed_now: false });
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });

    renderContent();

    await waitFor(() => expect(api.completeManagedCEInstall).toHaveBeenCalledWith("e".repeat(32)));
    await waitFor(() => expect(screen.queryByText("completed · Managed Cheat Engine is ready")).toBeNull());
    expect(decky.toast).not.toHaveBeenCalled();
  });

  it("reconciles an overlapping setup monitor after another frontend consumes completion", async () => {
    const oldStatus = status(false);
    const registered = status(false);
    // Reconciling this operation requires the registered installation to be the
    // managed release it was for; a merely valid Cheat Engine can be the one
    // rollback preserved.
    registered.ce.executable = `/home/deck/.cheat-engine-decky/ce/installations/${"b".repeat(64)}/cheatengine-x86_64.exe`;
    registered.ce.valid = true;
    registered.ce.sha256 = "c".repeat(64);
    const extracting = {
      operation_id: "9".repeat(32), state: "extracting", progress: null,
      message: "Extracting the reviewed Cheat Engine files locally", error: null, installed: null,
    };
    const activeCapability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: extracting,
    };
    api.completeManagedCEInstall.mockResolvedValue({ executable: registered.ce.executable, sha256: registered.ce.sha256, root: "/managed", size: 1, completed_now: false });
    api.getStatus.mockResolvedValueOnce(oldStatus).mockResolvedValue(registered);
    api.getManagedCECapability
      .mockResolvedValueOnce(activeCapability)
      .mockResolvedValue({ ...activeCapability, operation: null });
    api.pollManagedCEInstall.mockRejectedValue(new Error("managed CE install operation is unknown"));
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });

    renderContent();

    expect((await screen.findByTestId("setup-progress")).textContent).toContain("Extracting the reviewed Cheat Engine files locally");
    await waitFor(() => expect(api.pollManagedCEInstall).toHaveBeenCalledWith("9".repeat(32)), { timeout: 1500 });
    await waitFor(() => expect(screen.queryByTestId("setup-progress")).toBeNull());
    expect(screen.getByTestId("ce-row").textContent).toContain("cccccccc");
    expect(screen.queryByText("managed CE install operation is unknown")).toBeNull();
    expect(screen.queryByRole("progressbar")).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("does not clear an interrupted force reinstall of the release already registered", async () => {
    // Reinstalling the registered release means the identity predicate is
    // satisfied before the operation even starts, so it cannot distinguish
    // "this reinstall committed" from "the previous install is still there".
    const release = { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" };
    const registered = status(false);
    registered.ce.valid = true;
    registered.ce.executable = `/home/deck/.cheat-engine-decky/ce/installations/${"b".repeat(64)}/cheatengine-x86_64.exe`;
    const capability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release, operation: null,
    };
    api.getStatus.mockResolvedValue(registered);
    api.getManagedCECapability.mockResolvedValue(capability);
    api.startManagedCEInstall.mockResolvedValue({
      operation_id: "7".repeat(32), state: "downloading", progress: null,
      message: "Downloading a fresh reviewed Cheat Engine artifact for reinstall", error: null, installed: null,
    });
    api.pollManagedCEInstall.mockRejectedValue(new Error("managed CE install operation is unknown"));
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();

    const reinstall = await screen.findByRole("button", { name: /^(Reinstall|Install)$/ });
    await waitFor(() => expect((reinstall as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(reinstall);
    // A forced reinstall takes one explicit confirmation before it starts.
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    act(() => modalState.nodes[0].props.onOK());

    await waitFor(() => expect(api.startManagedCEInstall).toHaveBeenCalledWith(true));
    expect(await screen.findByText("managed CE install operation is unknown")).toBeTruthy();
    expect(decky.toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ body: "Cheat Engine installation was verified." }),
    );

    // Refusing the false success must not leave the panel blocked: the backend
    // has no operation, so nothing will ever advance that local snapshot, and
    // an active one keeps every ordinary Home action disabled.
    await waitFor(() => expect((screen.getByRole("button", { name: "Advanced…" }) as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false);
    // And the setup itself can be attempted again.
    expect((screen.getByRole("button", { name: /^(Reinstall|Install)$/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("does not hide a failed reinstall when the previous CE remains valid", async () => {
    const capability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: null,
    };
    const extracting = {
      operation_id: "8".repeat(32), state: "extracting", progress: null,
      message: "Extracting the reviewed Cheat Engine files locally", error: null, installed: null,
    };
    api.getManagedCECapability.mockResolvedValue(capability);
    api.startManagedCEInstall.mockResolvedValue(extracting);
    api.pollManagedCEInstall.mockResolvedValue({
      ...extracting, state: "failed", message: "Managed Cheat Engine setup failed",
      error: "replacement verification failed; previous CE preserved",
    });
    renderContent();

    fireEvent.click(await screen.findByRole("button", { name: /^(Reinstall|Install)$/ }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    modalState.nodes[0].props.onOK();

    expect(await screen.findByText("replacement verification failed; previous CE preserved")).toBeTruthy();
    expect(screen.getByTestId("ce-row").textContent).toContain("aaaaaaaa");
  });

  it("unblocks game selection after managed setup even when the optional launch refresh stalls", async () => {
    const capability = {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: null,
    };
    const extracting = {
      operation_id: "d".repeat(32), state: "extracting", progress: null,
      message: "Extracting the reviewed Cheat Engine files locally", error: null, installed: null,
    };
    api.getManagedCECapability.mockResolvedValue(capability);
    api.startManagedCEInstall.mockResolvedValue(extracting);
    api.pollManagedCEInstall.mockResolvedValue({ ...extracting, state: "completed", message: "Managed Cheat Engine is ready", installed: { root: "/managed", executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), size: 1 } });
    api.completeManagedCEInstall.mockResolvedValue({ root: "/managed", executable: "/managed/Cheat Engine.exe", sha256: "a".repeat(64), size: 1, completed_now: true });
    api.getCELaunchCapability.mockResolvedValueOnce({ ...launch, game: { ...launch.game, running: false } }).mockImplementation(() => new Promise(() => undefined));
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();

    fireEvent.click(await screen.findByRole("button", { name: /^(Reinstall|Install)$/ }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    modalState.nodes[0].props.onOK();
    await waitFor(() => expect(api.completeManagedCEInstall).toHaveBeenCalledWith("d".repeat(32)));
    await waitFor(() => expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false));
  });

  it.each([false, true])("disables conflicting edits and close-prompt mutations while pinning (dropdown=%s)", async (dropdown) => {
    let finish!: (value: number[]) => void;
    const onTogglePin = vi.fn(() => new Promise<number[]>((resolve) => { finish = resolve; }));
    const onApplied = vi.fn();
    const onCancel = vi.fn();
    const selectedInspection = dropdown ? { ...inspect, controls: inspect.controls.map((control: any) => ({ ...control, kind: "dropdown", dropdown_values: [["100", "Low"], ["250", "High"]], dropdown_read_only: true })) } : inspect;
    render(<CheatSelectionModal appId={10} inspection={selectedInspection as any} live pinned={[]}
      startupPreferences={[]} rememberedPreferences={[]} configuredValues={[]}
      onSaveConfiguredValues={vi.fn()} onValidateStartupPlan={vi.fn()}
      onTogglePin={onTogglePin} onApplied={onApplied} onCancel={onCancel} />);
    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "250" } });
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    const prompt = within(screen.getByTestId("unsaved-prompt"));
    act(() => {
      (screen.getByLabelText("Pinned") as HTMLInputElement).click();
      prompt.getByRole("button", { name: "Apply" }).click();
    });
    expect(onTogglePin).toHaveBeenCalledOnce();
    expect((within(screen.getByTestId("cheat-row-7")).getByTestId("toggle") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Value") as HTMLInputElement).disabled).toBe(true);
    for (const name of ["Apply", "Discard"]) {
      const button = prompt.getByRole("button", { name });
      expect((button as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(button);
    }
    expect(screen.getByTestId("unsaved-prompt")).toBeTruthy();
    expect(onApplied).not.toHaveBeenCalled();
    expect(onCancel).not.toHaveBeenCalled();
    await act(async () => { finish([7]); });
    expect((screen.getByLabelText("Value") as HTMLInputElement).disabled).toBe(false);
    expect((prompt.getByRole("button", { name: "Apply" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("commits a pin immediately as profile state and keeps it out of the Apply diff", async () => {
    const onTogglePin = vi.fn().mockResolvedValue([7]);
    const onApplied = vi.fn().mockResolvedValue(undefined);
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });

    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      autoloadEnabled={false}
      onTogglePin={onTogglePin}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    const pin = await screen.findByLabelText("Pinned");
    expect((pin as HTMLInputElement).checked).toBe(false);
    fireEvent.click(pin);
    await waitFor(() => expect(onTogglePin).toHaveBeenCalledWith(7, true));
    await waitFor(() => expect((screen.getByLabelText("Pinned") as HTMLInputElement).checked).toBe(true));

    // The pinned section is now reachable, and pinning alone is not a runtime mutation.
    fireEvent.change(screen.getByLabelText("Section"), { target: { value: "pinned" } });
    const row = screen.getByTestId("cheat-row-7");
    expect(row.textContent).toContain("Health");
    expect(row.textContent).toContain("pinned");
    expect(runtimeClient.applyRuntimeSelection).not.toHaveBeenCalled();
  });

  it("leaves Search responsible for review preparation failure without stacking an error modal", async () => {
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    api.inspectTableSha.mockRejectedValueOnce(new Error("Review preparation failed"));
    await expect(modalState.nodes[0].props.onSelected(SHA)).rejects.toThrow("Review preparation failed");
    expect(modalState.nodes).toHaveLength(1);
    expect(modalState.nodes[0].type).toBe(TableSearchModal);
    expect(modalState.closes[0]).not.toHaveBeenCalled();
  });

  it.each([true, false, null])("waits for activation A's own Stop and isolates activation B (confirmed=%s)", async (confirmed) => {
    api.getCELaunchCapability.mockResolvedValue({ ...launch, operations: [{ operation_id: "live", app_id: 10, state: "connected" }] });
    api.saveProfile.mockResolvedValue({});
    api.setExecutionConsent.mockResolvedValue({});
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    await act(async () => { await modalState.nodes[0].props.onSelected(SHA); });
    const a = modalState.nodes[1];
    // B may already have been prepared; it owns no activation or Stop yet.
    await act(async () => { await modalState.nodes[0].props.onSelected(SHA); });
    const b = modalState.nodes[2];
    let finishRead!: (value: any) => void;
    runtimeClient.queryRuntimeControlsPartial.mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve; }));
    let runA!: Promise<any>;
    const settledA = vi.fn();
    act(() => { runA = a.props.onUse("game.exe", vi.fn()).then(() => null, (cause: unknown) => cause).then((result: unknown) => { settledA(); return result; }); });
    await waitFor(() => expect(finishRead).toBeTypeOf("function"));
    let finishStop!: () => void;
    api.stopCEForGame.mockImplementationOnce(() => new Promise((resolve, reject) => {
      // A clean stop, because this is about which activation a Stop belongs to:
      // one that could not prove the game clean holds B as well, which is its
      // own case below.
      finishStop = () => confirmed === null
        ? resolve({ stopped: false, recovered: false })
        : confirmed
          ? resolve({ stopped: true, recovered: false, quiesce: { asked: true, answered: true, reason: null, cleanup_confirmed: true, records_put_down: 0, records_unsettled: [] } })
          : reject(new Error("Stop refused"));
    }));
    const stopA = a.props.onAbort().then(() => null, (cause: unknown) => cause);
    await act(async () => { finishRead({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] }); });
    expect(settledA).not.toHaveBeenCalled();
    expect(modalState.closes[1]).not.toHaveBeenCalled();
    await act(async () => { finishStop(); await stopA; });
    const outcome = await runA;
    if (confirmed) expect(outcome.message).toContain("Stopped at your request");
    else expect(outcome).toBeNull();

    let finishB!: (value: any) => void;
    runtimeClient.queryRuntimeControlsPartial.mockImplementationOnce(() => new Promise((resolve) => { finishB = resolve; }));
    let runB!: Promise<void>;
    act(() => { runB = b.props.onUse("game.exe", vi.fn()); });
    await waitFor(() => expect(finishB).toBeTypeOf("function"));
    // A's stale callback cannot stop B or write into B's state.
    expect(await a.props.onAbort()).toContain("nothing to stop");
    expect(api.stopCEForGame).toHaveBeenCalledOnce();
    await act(async () => { finishB({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] }); await runB; });
    expect(modalState.closes[2]).toHaveBeenCalledOnce();
  });

  it.each([
    ["a cheat it named", 1],
    ["nothing it could confirm", 0],
  ])("does not start another table in a game the last one may still have changed (%s)", async (_case, unsettled) => {
    // The stop ends the old Cheat Engine either way, and what that table left
    // changed can no longer be undone: its restore resolves symbols belonging
    // to the process that just ended. The backend holds the game from then on,
    // and the switch reads that hold before anything is saved.
    let capability: any = { ...launch, operations: [{ operation_id: "live", app_id: 10, state: "connected" }] };
    api.getCELaunchCapability.mockImplementation(async () => capability);
    api.stopCEForGame.mockImplementation(async () => {
      capability = { ...launch, operations: [], run_holds: { dirty: { since: 1, unsettled }, autoload_held: false } };
      return { stopped: true, recovered: false, quiesce: { asked: true, answered: true, reason: null, cleanup_confirmed: false, records_unsettled: unsettled ? ["6"] : [] } };
    });
    api.saveProfile.mockResolvedValue({});
    api.setExecutionConsent.mockResolvedValue({});
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    await act(async () => { await modalState.nodes[0].props.onSelected(SHA); });
    const review = modalState.nodes[1];
    await expect(review.props.onUse("other.exe", vi.fn())).rejects.toThrow("Restart the game before starting a table in it");
    expect(api.stopCEForGame).toHaveBeenCalledOnce();
    expect(api.saveProfile).not.toHaveBeenCalled();
    expect(api.setExecutionConsent).not.toHaveBeenCalled();
    expect(api.launchCEForGame).not.toHaveBeenCalled();
    // A second press finds no Cheat Engine to stop, and the hold is still the
    // backend's, so it is refused the same way.
    await expect(review.props.onUse("other.exe", vi.fn())).rejects.toThrow("Restart the game before starting a table in it");
    expect(api.saveProfile).not.toHaveBeenCalled();
    // Home says it before anything is pressed, and offers to clear it.
    expect((await screen.findByTestId("dirty-run")).textContent).toContain("no table is started in it until it has been restarted");

    // Once the backend has proven that run over, the switch goes through.
    capability = { ...launch, operations: [], run_holds: { dirty: null, autoload_held: false } };
    await act(async () => { await review.props.onUse("other.exe", vi.fn()).catch(() => undefined); });
    expect(api.saveProfile).toHaveBeenCalled();
  });

  it("clears the hold from Home when the reader asks to", async () => {
    let capability: any = { ...launch, run_holds: { dirty: { since: 1, unsettled: 0 }, autoload_held: false } };
    api.getCELaunchCapability.mockImplementation(async () => capability);
    api.clearGameRunHolds.mockImplementation(async () => {
      capability = { ...launch, run_holds: { dirty: null, autoload_held: false } };
      return { cleared: ["dirty"], run_holds: capability.run_holds };
    });
    renderContent();
    const row = await screen.findByTestId("dirty-run");
    fireEvent.click(within(row).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(api.clearGameRunHolds).toHaveBeenCalledWith(10));
    await waitFor(() => expect(screen.queryByTestId("dirty-run")).toBeNull());
  });

  it("switches tables as before where the stop proved the game clean", async () => {
    api.getCELaunchCapability.mockResolvedValue({ ...launch, operations: [{ operation_id: "live", app_id: 10, state: "connected" }] });
    api.stopCEForGame.mockResolvedValue({
      stopped: true, recovered: false,
      quiesce: { asked: true, answered: true, reason: null, cleanup_confirmed: true, records_put_down: 2, records_unsettled: [] },
    });
    api.saveProfile.mockResolvedValue({});
    api.setExecutionConsent.mockResolvedValue({});
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    await act(async () => { await modalState.nodes[0].props.onSelected(SHA); });
    const review = modalState.nodes[1];
    const outcome = await review.props.onUse("other.exe", vi.fn()).then(() => null, (cause: Error) => cause);
    expect(outcome?.message ?? "").not.toContain("Restart the game");
    expect(api.saveProfile).toHaveBeenCalled();
  });

  it("does not mark a continuing activation aborted when Stop fails", async () => {
    resetSupportLog();
    const owned = { ...launch, operations: [{ operation_id: "live", app_id: 10, state: "connected" }] };
    api.getCELaunchCapability.mockResolvedValue(owned);
    api.saveProfile.mockResolvedValue({});
    api.setExecutionConsent.mockResolvedValue({});
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    await act(async () => { await modalState.nodes[0].props.onSelected(SHA); });
    const review = modalState.nodes[1];
    let finishRead!: (value: any) => void;
    runtimeClient.queryRuntimeControlsPartial.mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve; }));
    let activation!: Promise<void>;
    act(() => { activation = review.props.onUse("game.exe", vi.fn()); });
    await waitFor(() => expect(finishRead).toBeTypeOf("function"));
    api.stopCEForGame.mockRejectedValueOnce(new Error("Stop not confirmed"));
    await expect(review.props.onAbort()).rejects.toThrow("Stop not confirmed");
    await act(async () => {
      finishRead({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] });
      await activation;
    });
    expect(modalState.closes[1]).toHaveBeenCalledOnce();
    expect(readSupportLog().entries.some((entry) => JSON.stringify(entry).includes("Stopped at your request"))).toBe(false);
  });

  it("hands Search to Review serially so Decky never has two workflow modals stacked", async () => {
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    expect(modalState.nodes[0].type).toBe(TableSearchModal);

    await modalState.nodes[0].props.onSelected(SHA);
    await waitFor(() => expect(modalState.nodes.length).toBe(2));
    expect(modalState.nodes[1].type).toBe(TableReviewModal);
    expect(modalState.events).toEqual(["show", "close", "show"]);
  });

  it("shows the state of the table this game is on, on the panel itself", async () => {
    // It was the one table with no mark anywhere: the row said "marked as not
    // working" in words and said nothing at all about a table proven to work,
    // so the state of the table a user is actually using was the one state they
    // had to open another screen to see.
    const proven = status(true);
    proven.table_compatibility = {
      schema: 3, reason: null,
      entries: [{
        app_id: 10, table_sha256: SHA, target_process: "game.exe", pe_version: "1",
        steam_build_id: null,
        last_working_at: 1789334726, invalidated: false, state: "matching",
      }],
    };
    api.getStatus.mockResolvedValue(proven);
    renderContent();

    // Waited for, because the mark needs the selected game as well as the
    // status, and the selection settles a round trip after the first render.
    await waitFor(() => expect(
      within(screen.getByTestId("table-row")).getByLabelText("Worked on this build"),
    ).toBeTruthy());
    // Before the name rather than beside the buttons: this row already carries
    // a filename and two presses in a 300 pixel column, and a third thing in
    // the trailing group laid Manage off the edge of the panel.
    const row = screen.getByTestId("table-row");
    const glyph = within(row).getByLabelText("Worked on this build");
    const manage = within(row).getByRole("button", { name: "Manage" });
    expect(glyph.compareDocumentPosition(manage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The name scrolls under the ring, so it sits in a marquee track of its
    // own: the mark leads the line that track is on.
    const name = within(row).getByText("Game.CT").closest(".ce-decky-marquee");
    expect(name?.previousSibling).toBe(glyph);
  });

  it("creates the profile a game's first imported table needs and then associates it", async () => {
    // `commitDesiredState` returns the moment its write succeeds and re-reads
    // the authority only when the reply was lost, so reusing the status this
    // screen last saw left the profile it had just written invisible. A game's
    // very first table downloaded, verified and stored, and the press ended in
    // "could not create the game profile" for a profile that was on disk, with
    // nothing associated and nothing offered for review. A game that already
    // had a profile skipped this branch, which is why every later import worked.
    const blank = { app_id: 10, name: "Game", is_shortcut: false, table_sha256: null, target_process: null, execution_consent_sha256: null, startup: [], pinned: [], previous_table_sha256: null, table_history: {}, table_library: [], autoload_enabled: false, remembered: [] };
    const withoutProfile = { ...status(true), profiles: [] };
    const created = { ...status(true), profiles: [blank] };
    const associated = { ...status(true), profiles: [{ ...blank, table_library: [SHA] }] };
    api.getStatus.mockResolvedValue(withoutProfile);
    api.saveProfile.mockImplementation(async () => { api.getStatus.mockResolvedValue(created); return {}; });
    api.associateTable.mockImplementation(async () => { api.getStatus.mockResolvedValue(associated); return {}; });

    renderContent();
    await waitFor(() => expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));

    await modalState.nodes[0].props.onSelected(SHA);
    expect(api.saveProfile).toHaveBeenCalledWith(10, "Game", false, null, null);
    await waitFor(() => expect(api.associateTable).toHaveBeenCalledWith(10, SHA));
    await waitFor(() => expect(modalState.nodes.length).toBe(2));
    expect(modalState.nodes[1].type).toBe(TableReviewModal);
  });

  it("keeps first-run CE setup prominent and table actions blocked without a game", async () => {
    api.getStatus.mockResolvedValue(status(false, false)); api.getRuntimeStatus.mockResolvedValue(noRuntime());
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();
    expect(await screen.findByRole("button", { name: "Download and install CE" })).toBeTruthy();
    expect(screen.getByText("No game selected")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("selects the game automatically when the other running app is a store launcher", async () => {
    // A game started through a third-party store leaves that store's own library
    // entry running beside it. Without this the observation stays ambiguous and
    // the user has to pick the game by hand every time.
    const launcher = { appId: 77, name: "Epic Games", sortAs: "Epic Games", isShortcut: true };
    steam.listRunningGames.mockResolvedValue({ available: true, games: [launcher, game] });
    steam.listInstalledGames.mockResolvedValue([launcher, game]);
    steam.readAppDetails.mockImplementation(async (appId: number) => appId === launcher.appId
      ? { ...details, appId: launcher.appId, displayName: "Epic Games", isShortcut: true, shortcutExe: "\"/home/deck/Games/Epic Games/Launcher/EpicGamesLauncher.exe\"" }
      : details);
    renderContent();

    expect(await screen.findByText(/Steam · game\.exe/)).toBeTruthy();
    expect(screen.queryByText("No game selected")).toBeNull();
  });

  it("offers the picker only what this device holds of the account's library", async () => {
    // Measured on a Steam Deck: Steam's library carried 39 non-Steam shortcuts
    // while the device's own store held 6, and nine of the twenty-two installed
    // apps were Proton builds and runtimes. Every one of those was offered.
    const elsewhere = { appId: 4093505389, name: "Neon Bazaar", sortAs: "Neon Bazaar", isShortcut: true };
    const here = { appId: 3407131886, name: "Halo Campaign Evolved", sortAs: "Halo", isShortcut: true };
    const proton = { appId: 3658110, name: "Proton 10.0", sortAs: "Proton 10.0", isShortcut: false };
    const queued = { appId: 1151340, name: "Fallout 76", sortAs: "Fallout 76", isShortcut: false };
    const second = { appId: 99, name: "Other Game", sortAs: "Other Game", isShortcut: false };
    // Two running games leave the choice open, which is the path that offers
    // the picker without a game already selected refusing the press.
    steam.listRunningGames.mockResolvedValue({ available: true, games: [game, second] });
    steam.listInstalledGames.mockResolvedValue([game, second, proton, queued, here, elsewhere]);
    api.readLocalLibrary.mockResolvedValue({
      schema: 1,
      steam_app_ids: [game.appId, second.appId, proton.appId],
      unstartable_app_ids: [proton.appId],
      shortcut_app_ids: [here.appId],
      reason: null,
      shortcuts_reason: null,
    });
    steam.readAppDetails.mockResolvedValue(details);
    renderContent();

    // Waited for, the way the neighbouring case does: the panel offers the
    // choice once it has settled on two running games, and the press before
    // that lands on a screen that is still deciding.
    expect(await screen.findByText("2 games running; choose one")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === GamePickerModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === GamePickerModal);
    // Proton is installed and is not a game; Fallout 76 is queued rather than
    // installed; Neon Bazaar is a shortcut belonging to another device.
    expect(picker.props.games.map((entry: any) => entry.appId).sort())
      .toEqual([game.appId, second.appId, here.appId].sort());
  });

  it("counts and marks only the real games once a launcher is filtered out", async () => {
    // The published running state was written before the launcher filtering and
    // kept the store's own entry, so Home counted it as a running game and the
    // picker offered it as one of the candidates the user might have meant.
    const launcher = { appId: 77, name: "Epic Games", sortAs: "Epic Games", isShortcut: true };
    const second = { appId: 99, name: "Other Game", sortAs: "Other Game", isShortcut: false };
    const unrelated = { appId: 55, name: "Not Running", sortAs: "Not Running", isShortcut: false };
    steam.listRunningGames.mockResolvedValue({ available: true, games: [launcher, game, second] });
    steam.listInstalledGames.mockResolvedValue([unrelated, launcher, game, second]);
    steam.readAppDetails.mockImplementation(async (appId: number) => appId === launcher.appId
      ? { ...details, appId: launcher.appId, displayName: "Epic Games", isShortcut: true, shortcutExe: "\"/home/deck/Games/Epic Games/Launcher/EpicGamesLauncher.exe\"" }
      : details);
    renderContent();

    // Two real games remain, so this stays ambiguous and Home asks. The count
    // it shows is the filtered one, not the three that were observed.
    expect(await screen.findByText("2 games running; choose one")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === GamePickerModal)).toHaveLength(1));
    const picker = modalState.nodes.find((node: any) => node.type === GamePickerModal);

    expect(picker.props.runningGames.map((entry: any) => entry.appId).sort()).toEqual([10, 99]);
    // The launcher is still in the library, simply not marked as a candidate.
    expect(picker.props.games.some((entry: any) => entry.appId === launcher.appId)).toBe(true);
  });

  it("never lets the unfiltered observation become an interactive answer", async () => {
    // Choose is not held while the launcher resolution is in flight, so
    // publishing the raw observation first and correcting it afterwards left a
    // window in which the picker opened on the uncorrected answer - and the
    // detector then exited on its own race guard without ever publishing the
    // corrected one, so it stayed wrong.
    const launcher = { appId: 77, name: "Epic Games", sortAs: "Epic Games", isShortcut: true };
    const second = { appId: 99, name: "Other Game", sortAs: "Other Game", isShortcut: false };
    steam.listRunningGames.mockResolvedValue({ available: true, games: [launcher, game, second] });
    steam.listInstalledGames.mockResolvedValue([launcher, game, second]);
    let releaseLauncherDetails!: (value: any) => void;
    const pendingLauncher = new Promise((resolve) => { releaseLauncherDetails = resolve; });
    steam.readAppDetails.mockImplementation(async (appId: number) => {
      if (appId !== launcher.appId) return details;
      // Held open, so the resolution the filtering needs is still in flight.
      await pendingLauncher;
      return { ...details, appId: launcher.appId, displayName: "Epic Games", isShortcut: true, shortcutExe: "\"/home/deck/Games/Epic Games/Launcher/EpicGamesLauncher.exe\"" };
    });
    renderContent();

    // Wait until the detector is actually inside the resolution it is blocked
    // on, which is the exact window this is about: in the old order the raw
    // observation has already been published by this point.
    await waitFor(() => expect(steam.readAppDetails).toHaveBeenCalledWith(launcher.appId));

    // Nothing about what is running may be published while it is unresolved:
    // three running games was the raw observation, and it never becomes state.
    expect(screen.queryByText("3 games running; choose one")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === GamePickerModal)).toHaveLength(1));
    const early = modalState.nodes.find((node: any) => node.type === GamePickerModal);
    expect(early.props.runningGames.some((entry: any) => entry.appId === launcher.appId)).toBe(false);

    await act(async () => { releaseLauncherDetails(null); });
  });

  it("refuses a game change while the game this profile is on is running", async () => {
    // Changing the game underneath a running one is a press away from every
    // question this plugin answers per game: which table is prepared, which
    // process is the target, which session an owned Cheat Engine holds.
    //
    // It is not free. A game is selected automatically *because* it is running,
    // so correcting a detection that landed on the wrong library entry now
    // waits for the game to stop. The row says what releases the press rather
    // than leaving a dead control, and the press itself comes back below.
    const other = { appId: 99, name: "Other Game", sortAs: "Other Game", isShortcut: false };
    steam.listRunningGames.mockResolvedValue({ available: true, games: [game] });
    steam.listInstalledGames.mockResolvedValue([game, other]);
    renderContent();
    expect(await screen.findByText(/Steam · game\.exe/)).toBeTruthy();

    await waitFor(() => expect((screen.getByRole("button", { name: "Change" }) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    expect(modalState.nodes).toHaveLength(0);
  });

  it("refuses a game change decided from a snapshot the game has already outrun", async () => {
    // The row is disabled from an observation up to three seconds old, and the
    // detector is deliberately suspended while a press is in flight. So a game
    // that starts after the last poll leaves an enabled button over a running
    // game, and disabling a control is presentation rather than enforcement.
    const other = { appId: 99, name: "Other Game", sortAs: "Other Game", isShortcut: false };
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    steam.listInstalledGames.mockResolvedValue([game, other]);
    api.getCELaunchCapability.mockResolvedValue({ ...launch, game: { ...launch.game, running: false } });
    renderContent();
    await screen.findByText("No game selected");
    fireEvent.click(await screen.findByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.length).toBeGreaterThan(0));
    await act(async () => { await modalState.nodes[0].props.onPick(game); });
    expect(await screen.findByText(/Steam · game\.exe/)).toBeTruthy();

    // The game starts here, between the render and the press.
    modalState.nodes.length = 0;
    steam.listRunningGames.mockResolvedValue({ available: true, games: [game] });
    const libraryReads = steam.listInstalledGames.mock.calls.length;
    expect((screen.getByRole("button", { name: "Change" }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Change" })); });

    // The refusal, and no picker: the press is answered before the library is
    // even read, because the answer decides whether there is anything to open.
    await waitFor(() => expect(screen.getByText(/running now/)).toBeTruthy());
    expect(modalState.nodes.filter((node: any) => node.type === GamePickerModal)).toHaveLength(0);
    expect(steam.listInstalledGames.mock.calls.length).toBe(libraryReads);
  });

  it("refuses a pick made after the game started while the picker was open", async () => {
    // The picker is open for as long as the user takes to read a library, and
    // the detector that would notice is suppressed for every second of it. The
    // refusal has to be made where the press was, so the selection stays where
    // it was and the modal says why.
    const other = { appId: 99, name: "Other Game", sortAs: "Other Game", isShortcut: false };
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    steam.listInstalledGames.mockResolvedValue([game, other]);
    api.getCELaunchCapability.mockResolvedValue({ ...launch, game: { ...launch.game, running: false } });
    renderContent();
    await screen.findByText("No game selected");
    fireEvent.click(await screen.findByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.length).toBeGreaterThan(0));
    await act(async () => { await modalState.nodes[0].props.onPick(game); });
    expect(await screen.findByText(/Steam · game\.exe/)).toBeTruthy();

    modalState.nodes.length = 0;
    modalState.events.length = 0;
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    await waitFor(() => expect(modalState.nodes.length).toBeGreaterThan(0));
    const picker = modalState.nodes.find((node: any) => node.type === GamePickerModal);
    expect(picker).toBeTruthy();

    // Started while the library was on the screen.
    steam.listRunningGames.mockResolvedValue({ available: true, games: [game] });
    await expect(act(async () => { await picker.props.onPick(other); })).rejects.toThrow(/running now/);

    // The context is still the game it was prepared for, and the picker is
    // still open: the refusal reaches the press that was made.
    expect(await screen.findByText(/Steam · game\.exe/)).toBeTruthy();
    expect(modalState.events).not.toContain("close");
  });

  it("keeps a manually chosen game usable for table search while nothing is running", async () => {
    // Searching, downloading, importing and authorizing a table are designed to
    // work for a chosen game; only launching Cheat Engine needs it running.
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    steam.listInstalledGames.mockResolvedValue([game]);
    api.getCELaunchCapability.mockResolvedValue({ ...launch, game: { ...launch.game, running: false } });
    renderContent();
    expect(await screen.findByText("No game selected")).toBeTruthy();

    await waitFor(() => expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.length).toBeGreaterThan(0));
    await act(async () => { await modalState.nodes[0].props.onPick(game); });

    expect(await screen.findByText(/Steam · game\.exe/)).toBeTruthy();
    await waitFor(() => expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByRole("button", { name: "Manage" }) as HTMLButtonElement).disabled).toBe(false);

    // And the game is still changeable from Home rather than from Advanced,
    // with the whole installed library behind the press: automatic detection
    // can land on the wrong entry, and this is where that is corrected.
    modalState.nodes.length = 0;
    expect((screen.getByRole("button", { name: "Change" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    await waitFor(() => expect(modalState.nodes.length).toBeGreaterThan(0));
    expect(modalState.nodes[0].props.games.map((entry: any) => entry.appId)).toEqual([game.appId]);
  });

  it("refreshes home when a game starts while the panel is open", async () => {
    // The running-game source is polled, so the panel must repaint from the next
    // observation without the user reopening or reloading anything.
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    steam.listInstalledGames.mockResolvedValue([game]);
    api.getStatus.mockResolvedValue(status(false));
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    api.getCELaunchCapability.mockResolvedValue({ ...launch, game: { ...launch.game, running: false } });
    renderContent();
    expect(await screen.findByText("No game selected")).toBeTruthy();

    steam.listRunningGames.mockResolvedValue({ available: true, games: [game] });
    vi.useFakeTimers();
    try {
      // The panel needs one poll to observe the game and further hydration
      // round trips to repaint. How many is an implementation detail, so drive
      // bounded polls on fake timers until the observation lands instead of
      // assuming an exact count.
      for (let poll = 0; poll < 10 && screen.queryByText("No game selected"); poll += 1) {
        await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
      }
      expect(screen.queryByText("No game selected")).toBeNull();
      // No profile exists for this game yet, so the row identifies it by AppID.
      expect(screen.getByText(/Steam · AppID 10/)).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not autoload or expose parallel workflow actions while managed setup survives a frontend reload", async () => {
    resetSupportLog();
    const capability = {
      schema: 3,
      mode: "managed_install",
      managed_install_available: true,
      release_manifest_loaded: true,
      network_download_enabled: true,
      native_extraction_enabled: true,
      reason: "setup active",
      release: { visible_version: "7.7", artifact_filename: "CE.exe", sha256: "b".repeat(64), size: 1, reviewed_at: "2026-08-23" },
      operation: {
        operation_id: "e".repeat(32),
        state: "extracting",
        progress: null,
        message: "Installing Cheat Engine",
        error: null,
        installed: null,
      },
    };
    api.getManagedCECapability.mockResolvedValue(capability);
    // The surviving operation must still be extracting while these assertions
    // run. The monitor - not a capability refresh that can be one poll behind -
    // owns the local snapshot, so a terminal poll result would legitimately
    // release the workflow instead of proving this gate.
    api.pollManagedCEInstall.mockResolvedValue({ ...capability.operation });

    renderContent();
    expect((await screen.findByTestId("setup-progress")).textContent).toContain("Installing Cheat Engine");
    expect(screen.queryByRole("button", { name: "Resume CE setup" })).toBeNull();
    await waitFor(() => expect(api.pollManagedCEInstall).toHaveBeenCalledWith("e".repeat(32)), { timeout: 1500 });
    expect(api.launchCEForGame).not.toHaveBeenCalled();
    const starts = readSupportLog().entries.filter((entry) => entry.event === "panel.action_started");
    expect(starts).toHaveLength(1);
    expect(starts[0].fields.automatic).toBe("true");
    expect(starts[0].fields.interaction).toBeUndefined();
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Advanced…" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("opens manual game fallback with the freshly enumerated library rather than stale state", async () => {
    api.getStatus.mockResolvedValue(status(false)); api.getRuntimeStatus.mockResolvedValue(noRuntime());
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();
    await screen.findByRole("button", { name: "Choose" });
    await waitFor(() => expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    expect(modalState.nodes[0].type).toBe(GamePickerModal);
    expect(modalState.nodes[0].props.games).toEqual([game]);
    await modalState.nodes[0].props.onPick(game);
    expect(steam.readAppDetails).toHaveBeenCalledWith(game.appId);
    expect(modalState.events).toEqual(["show", "close"]);
  });
  it("keeps Proton, library selection, and runtime refresh live inside the detached Advanced modal", async () => {
    const refreshed = liveRuntime();
    refreshed.status.opened_process_id = 99;
    const otherGame = { appId: 11, name: "Other", sortAs: "Other", isShortcut: false };
    const onRunCELaunchSelfTest = vi.fn().mockResolvedValue(undefined);
    const onRefreshGames = vi.fn().mockResolvedValue([game, otherGame]);
    render(<AdvancedModal
      status={status(true)}
      games={[game]}
      selectedGame={game}
      appDetails={details}
      inspection={inspect}
      targetProcess="game.exe"
      ceLaunch={{ ...launch, proton_tools: [
        { tool_id: "p1", name: "Proton One", path: "/p1", version: "1" },
        { tool_id: "p2", name: "Proton Two", path: "/p2", version: "2" },
      ] } as any}
      launchProtonToolId="p1"
      runtime={noRuntime()}
      selfTest={null}
      busy={false}
      onRefreshGames={onRefreshGames}
      onSaveTargetProcess={vi.fn()}
      onPickCE={vi.fn()}
      onClearCEImport={vi.fn()}
      onRunSelfTest={vi.fn().mockResolvedValue({ ok: true, checks: [] })}
      onLaunchProtonChange={vi.fn()}
      onRunCELaunchSelfTest={onRunCELaunchSelfTest}
      onRefreshRuntime={vi.fn().mockResolvedValue(refreshed)}
      onRefreshProcesses={vi.fn().mockResolvedValue(refreshed)}
      onRetryAttach={vi.fn().mockResolvedValue(refreshed)}
      onRepairSessionState={vi.fn()}
      onRepairOwnedLaunchState={vi.fn()}
      onClearStartup={vi.fn().mockResolvedValue(0)}
      onRevokeConsent={vi.fn()}
      onLoadDiagnostics={vi.fn().mockResolvedValue(diagnostics)}
      onCheckRemoval={vi.fn()}
      onDeleteManagedData={vi.fn()}
      onRefreshAll={vi.fn().mockResolvedValue({ status: status(true), ceLaunch: launch, runtime: refreshed })}
      onClose={vi.fn()}
    />);

    fireEvent.change(screen.getByLabelText("Self-test Proton"), { target: { value: "p2" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify" }));
    await waitFor(() => expect(onRunCELaunchSelfTest).toHaveBeenCalledWith("p2"));

    const refreshRuntime = screen.getByRole("button", { name: "Refresh runtime" }) as HTMLButtonElement;
    await waitFor(() => expect(refreshRuntime.disabled).toBe(false));
    fireEvent.click(refreshRuntime);
    expect(await screen.findByText(/Attached PID 99/)).toBeTruthy();

    // The library is re-read here, but the game itself is chosen on the panel:
    // Advanced only reports which entry was resolved.
    fireEvent.click(screen.getByRole("button", { name: "Refresh library" }));
    await waitFor(() => expect(onRefreshGames).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText("Game")).toBeNull();
    expect((screen.getByLabelText("Target process") as HTMLInputElement).value).toBe("game.exe");
  });

  /** Advanced's props reduced to the blocklist, which is all these assert. */
  const blocklistProps = (overrides: Record<string, unknown>) => ({
    status: status(true) as any,
    games: [game],
    selectedGame: game,
    appDetails: details,
    inspection: inspect as any,
    live: true,
    targetProcess: "game.exe",
    ceLaunch: launch as any,
    launchProtonToolId: "",
    runtime: noRuntime() as any,
    selfTest: null,
    busy: false,
    onRefreshGames: vi.fn().mockResolvedValue([game]),
    onSaveTargetProcess: vi.fn(),
    onPickCE: vi.fn(),
    onClearCEImport: vi.fn(),
    onRunSelfTest: vi.fn().mockResolvedValue({ ok: true, checks: [] }),
    onLaunchProtonChange: vi.fn(),
    onRunCELaunchSelfTest: vi.fn(),
    onRefreshRuntime: vi.fn(),
    onRefreshProcesses: vi.fn(),
    onRetryAttach: vi.fn(),
    onRepairSessionState: vi.fn(),
    onRepairOwnedLaunchState: vi.fn(),
    onClearStartup: vi.fn(),
    onRevokeConsent: vi.fn(),
    onLoadDiagnostics: vi.fn().mockResolvedValue(diagnostics),
    onCheckRemoval: vi.fn(),
    onRefreshAll: vi.fn(),
    onClose: vi.fn(),
    blockedTables: [],
    onUnblockTable: vi.fn(),
    onClearBlockedTables: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  }) as any;

  it("says on Advanced what a fixed copy was made from and what it cost", async () => {
    // A copy CE Decky made carries no origin, because no provider served these
    // bytes, so the row that names where a table came from was absent for
    // exactly the tables whose provenance is least obvious from the name.
    const snapshot = status(true) as any;
    const source = { ...snapshot.tables[0], sha256: "c".repeat(64), filename: "Downloaded.CT" };
    snapshot.tables = [
      source,
      { ...snapshot.tables[0], derived_from: { sha256: source.sha256, transforms: ["remove-signature", "drop-unmatched-scans"], scans: ["aobHealth"], orphaned: ["Health"] } },
    ];
    render(<AdvancedModal {...blocklistProps({ status: snapshot, blockedTables: [], onRefreshBlockedTables: vi.fn().mockResolvedValue({ tables: [], reason: null }) })} />);

    const row = await screen.findByTestId("active-table-derived");
    expect(row.textContent).toMatch(/Fixed copy/);
    // One line: where it came from, what went, and what it cost.
    expect(row.textContent).toMatch(/from Downloaded\.CT · signature removed · code for 1 missing pattern removed · 1 cheat gone/);
    // The whole account is behind the press every row here has, not on the row.
    expect(row.textContent).not.toMatch(/no source vouched/);
    fireEvent.click(within(row).getByRole("button", { name: "?" }));
    const opened = screen.getByTestId("active-table-derived").textContent ?? "";
    expect(opened).toMatch(/aobHealth/);
    expect(opened).toMatch(/still on this device/);
    expect(opened).toMatch(/its own authorization/);
  });

  it("keeps the blocklist to one panel row and reaches every entry through Review", async () => {
    // Distinct leading hex per entry: the row identity is the digest prefix.
    const entry = (index: number) => ({
      sha256: `${index}`.padStart(4, "0") + "f".repeat(60),
      reason: "Cheat Engine ran it and it went straight back off.",
      filename: `Table${index}.CT`,
      app_id: 10,
      game_name: "Example",
      game_version: "1.2.3",
      // A day apart each, because a record that collected over weeks is what
      // the row above them describes and a single date says nothing about one.
      recorded_at: 1_700_000_000 - index * 86_400,
    });
    const all = Array.from({ length: 20 }, (_, index) => entry(index));
    const onRefreshBlockedTables = vi.fn()
      .mockResolvedValueOnce({ tables: all, reason: null })
      .mockResolvedValue({ tables: all.slice(1), reason: null });
    const onUnblockTable = vi.fn().mockResolvedValue(undefined);
    render(<AdvancedModal
      status={status(true) as any}
      games={[game]}
      selectedGame={game}
      appDetails={details}
      inspection={inspect as any}
      live
      targetProcess="game.exe"
      ceLaunch={launch as any}
      launchProtonToolId=""
      runtime={noRuntime() as any}
      selfTest={null}
      busy={false}
      onRefreshGames={vi.fn().mockResolvedValue([game])}
      onSaveTargetProcess={vi.fn()}
      onPickCE={vi.fn()}
      onClearCEImport={vi.fn()}
      onRunSelfTest={vi.fn().mockResolvedValue({ ok: true, checks: [] })}
      onLaunchProtonChange={vi.fn()}
      onRunCELaunchSelfTest={vi.fn()}
      onRefreshRuntime={vi.fn()}
      onRefreshProcesses={vi.fn()}
      onRetryAttach={vi.fn()}
      onRepairSessionState={vi.fn()}
      onRepairOwnedLaunchState={vi.fn()}
      onClearStartup={vi.fn()}
      onRevokeConsent={vi.fn()}
      onLoadDiagnostics={vi.fn().mockResolvedValue(diagnostics)}
      onCheckRemoval={vi.fn()}
      onRefreshAll={vi.fn()}
      onClose={vi.fn()}
      blockedTables={[]}
      onRefreshBlockedTables={onRefreshBlockedTables}
      onUnblockTable={onUnblockTable}
      onClearBlockedTables={vi.fn().mockResolvedValue(undefined)}
    />);

    // Loaded on mount, not from the snapshot the panel happened to hold.
    expect(await screen.findByText("20 tables marked")).toBeTruthy();
    // The record is a growing list nobody opens Advanced to read, so the panel
    // costs one row and everything below it stays reachable.
    expect(screen.queryAllByTestId(/^blocked-table-/)).toHaveLength(0);
    expect(screen.getByText("newest 2023-11-14")).toBeTruthy();
    // And a description, because this is the one row here whose list is on
    // another screen: what it says is what cannot be read off the entries from
    // here, which is how far the record spreads and how far back it goes.
    expect(screen.getByTestId("blocked-tables").textContent).toMatch(/Example · since 2023-10-26/);
    // Advanced's own last section must not have been pushed off by the record.
    expect(screen.getByText("Plugin data on disk")).toBeTruthy();

    // What the record does is refuse something about exact bytes until it is
    // cleared. It does not take a table off this device or out of search, it is
    // not all one cause, and clearing one is not an amnesty on everything known
    // about those bytes: a success an entry invalidated stays invalidated until
    // a cheat proves the table again.
    //
    // Read with the row opened. A record that holds entries describes itself
    // through them, so the row above them says how many and how recent and
    // keeps its prose one press away: six lines of it above three entries was
    // a screen explaining itself instead of showing what it holds.
    expect(screen.getByTestId("blocked-tables").textContent).not.toMatch(/ran and did not work/);
    fireEvent.click(within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "?" }));
    const record = screen.getByTestId("blocked-tables").textContent ?? "";
    expect(record).not.toMatch(/Search will not offer|stops offering|search cannot offer/);
    expect(record).not.toMatch(/nothing else about it is remembered/);
    expect(record).toMatch(/until it is cleared/);
    // Nor is every entry about exact bytes or about a table that failed: a row
    // whose file a source no longer has is keyed by the row, because nothing
    // was ever downloaded for it.
    expect(record).not.toMatch(/[Ee]ach entry refuses something about those exact bytes/);
    expect(record).toMatch(/provider row whose file the source no longer has/);
    expect(record).toMatch(/never refuses a copy already on this device/);
    // Four causes are stored, and every explanatory state names the same four.
    // The locked archive is the one that used to go missing before the first
    // entry of that kind existed.
    const causes = [/ran and did not work/, /not a usable table/, /archive nothing here can open/, /source no longer has/];
    for (const cause of causes) expect(record).toMatch(cause);

    fireEvent.click(within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "Review" }));
    expect(screen.getAllByTestId(/^blocked-table-/)).toHaveLength(20);
    // The recorded reason is a whole sentence and it is the point of the row,
    // so it reveals itself while the ring is on that row rather than ending in
    // an ellipsis nothing can finish. The same reveal the pinned cheats use.
    const firstEntry = screen.getAllByTestId(/^blocked-table-/)[0];
    expect(firstEntry.className).toContain("ce-decky-focusscroll");
    // Both lines, the name and the reason: each is longer than the window on
    // its own and neither can be finished from an ellipsis.
    expect(firstEntry.querySelectorAll(".ce-decky-marquee").length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText(/game 1\.2\.3/).length).toBeGreaterThan(0);

    fireEvent.click(within(screen.getByTestId("blocked-table-0000ffffffff")).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(onUnblockTable).toHaveBeenCalledWith("0000" + "f".repeat(60)));
    // Cleared entries leave this screen without it being reopened.
    await waitFor(() => expect(screen.getByText("19 tables marked")).toBeTruthy());

    // Back returns to the panel, not to a closed Advanced.
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByText("Advanced / Diagnostics")).toBeTruthy();
    expect(screen.queryAllByTestId(/^blocked-table-/)).toHaveLength(0);
  });

  it("says the same four things about the record before anything is in it", async () => {
    // An empty screen and a populated one describe one list. The locked archive
    // is stored apart from bytes that are not a table, so a record with no
    // entries yet that named three causes was describing a different list from
    // the one the next entry would join.
    render(<AdvancedModal
      status={status(true) as any}
      games={[game]}
      selectedGame={game}
      appDetails={details}
      inspection={inspect as any}
      live
      targetProcess="game.exe"
      ceLaunch={launch as any}
      onCheckRemoval={vi.fn()}
      onRefreshAll={vi.fn()}
      onClose={vi.fn()}
      blockedTables={[]}
      onRefreshBlockedTables={vi.fn().mockResolvedValue({ tables: [], reason: null })}
      onUnblockTable={vi.fn()}
      onClearBlockedTables={vi.fn()}
    />);
    await waitFor(() => expect(screen.getByText("No tables marked")).toBeTruthy());
    // An empty record has no entries to describe it, so the row still says so
    // in its own line. The four causes are one press away, in the same place
    // they are for a record that holds something.
    expect(screen.getByTestId("blocked-tables").textContent).toMatch(/Nothing is recorded here yet/);
    fireEvent.click(within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "?" }));
    const record = screen.getByTestId("blocked-tables").textContent ?? "";
    for (const cause of [/ran and did not work/, /not a usable table/, /archive nothing here can open/, /source no longer has/]) {
      expect(record).toMatch(cause);
    }
  });

  it("pages a blocklist longer than one screen instead of stranding its older entries", async () => {
    // The panel used to cap the list at twelve rows, which left every entry
    // past the cap clearable only by emptying the whole record.
    const entry = (index: number) => ({
      sha256: `${index}`.padStart(4, "0") + "f".repeat(60),
      reason: "Cheat Engine ran it and it went straight back off.",
      filename: `Table${index}.CT`,
      app_id: 10,
      game_name: "Example",
      game_version: "1.2.3",
      recorded_at: 1_700_000_000,
    });
    const all = Array.from({ length: 60 }, (_, index) => entry(index));
    render(<AdvancedModal
      {...blocklistProps({
        onRefreshBlockedTables: vi.fn().mockResolvedValue({ tables: all, reason: null }),
        onUnblockTable: vi.fn().mockResolvedValue(undefined),
      })}
    />);

    expect(await screen.findByText("60 tables marked")).toBeTruthy();
    fireEvent.click(within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "Review" }));
    expect(screen.getAllByTestId(/^blocked-table-/)).toHaveLength(25);

    fireEvent.click(within(screen.getByTestId("blocked-tables-more")).getByRole("button", { name: "Show 25 more" }));
    expect(screen.getAllByTestId(/^blocked-table-/)).toHaveLength(50);

    // The last page names the exact remainder rather than a full page.
    fireEvent.click(within(screen.getByTestId("blocked-tables-more")).getByRole("button", { name: "Show 10 more" }));
    expect(screen.getAllByTestId(/^blocked-table-/)).toHaveLength(60);
    expect(screen.queryByTestId("blocked-tables-more")).toBeNull();
    // Including the oldest, which the capped panel could never clear on its own.
    expect(within(screen.getByTestId("blocked-table-0059ffffffff")).getByRole("button", { name: "Clear" })).toBeTruthy();
  });

  it("puts the ring back on the press that opened a sub-screen", async () => {
    // Each of these screens replaces the panel rather than sitting over it, so
    // returning rebuilds every row behind it. Without this the ring reopens at
    // the top of a diagnostics screen the reader had scrolled well past.
    render(<AdvancedModal
      {...blocklistProps({
        onRefreshBlockedTables: vi.fn().mockResolvedValue({
          tables: [{
            sha256: "a".repeat(64),
            reason: "Cheat Engine ran it and it went straight back off.",
            filename: "Table.CT",
            app_id: 10,
            game_name: "Example",
            game_version: "1.2.3",
            recorded_at: 1_700_000_000,
          }],
          reason: null,
        }),
      })}
    />);

    // Waiting for the control, not for the row around it. The summary row is
    // rendered from the first paint and carries only its help press until the
    // record has been read, so a wait for the row is a wait for nothing: on a
    // machine slower than this one the read landed after the press was looked
    // for, which is how this passed here and failed in CI.
    fireEvent.click(await within(await screen.findByTestId("blocked-tables")).findByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    const review = within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "Review" });
    expect(document.activeElement).toBe(review);
  });

  it.each(["single", "partial_all"])("re-reads detached Advanced after an uncertain clear (%s)", async (mode) => {
    const first = { key: "a".repeat(64), sha256: "a".repeat(64), filename: "First.CT", reason: "failed", recorded_at: 1 };
    const second = { ...first, key: "b".repeat(64), sha256: "b".repeat(64), filename: "Second.CT" };
    let entries = mode === "single" ? [first] : [first, second];
    const clear = async () => {
      entries = mode === "single" ? [] : [second];
      throw new Error("clear receipt lost");
    };
    render(<AdvancedModal {...blocklistProps({
      onRefreshBlockedTables: async () => ({ tables: entries, reason: null }),
      onUnblockTable: clear, onClearBlockedTables: clear,
    })} />);
    fireEvent.click(await within(await screen.findByTestId("blocked-tables")).findByRole("button", { name: "Review" }));
    fireEvent.click(screen.getByRole("button", { name: mode === "single" ? "Clear" : "Clear all", exact: true }));
    await waitFor(() => expect(screen.queryByTestId(`blocked-table-${first.sha256.slice(0, 12)}`)).toBeNull());
    if (mode === "partial_all") expect(screen.getByTestId(`blocked-table-${second.sha256.slice(0, 12)}`)).toBeTruthy();
  });

  it("says a blocked-table record that cannot be read is not an empty one", async () => {
    // Empty because it could not be read is not empty, and the difference is
    // whether a table already known not to work is about to be offered again.
    // The panel keeps saying so while collapsed: a failure state that only
    // appears once a screen is opened is one nobody opens the screen to find.
    render(<AdvancedModal
      {...blocklistProps({
        onRefreshBlockedTables: vi.fn().mockResolvedValue({ tables: [], reason: "blocked-table state has an unsupported or corrupt schema" }),
      })}
    />);

    expect(await screen.findByText("Record cannot be read")).toBeTruthy();
    expect(screen.getByText(/Nothing is being refused while this cannot be read/)).toBeTruthy();
    // And the one action that replaces it stays reachable, on the screen that
    // owns every other way of changing this record.
    fireEvent.click(within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "Review" }));
    expect(screen.getByRole("button", { name: "Clear all" })).toBeTruthy();
  });

  it("says the restore capability is still settling instead of showing nothing", async () => {
    // `no-window` and `symbols-unresolved` become `ready` on their own, and
    // while they hold, the bridge publishes no `minimized_query` at all. Keying
    // this row on that field hid exactly the two states whose whole purpose is
    // to explain a wait, and left a game that had gone dark unaccounted for.
    const runtime = liveRuntime() as any;
    runtime.status.restore_capability = "symbols-unresolved";
    runtime.status.restore_error = "getAddress IsIconic: unresolved";
    render(<AdvancedModal {...blocklistProps({ runtime })} />);

    const row = await screen.findByTestId("minimized-query-unavailable");
    expect(within(row).getByText("Still working out whether the game can be brought back")).toBeTruthy();
    expect(within(row).getByText(/settles by itself/)).toBeTruthy();
  });

  it("says nothing about the restore capability once it is ready", async () => {
    const runtime = liveRuntime() as any;
    runtime.status.restore_capability = "ready";
    runtime.status.minimized_query = "IsIconic";
    render(<AdvancedModal {...blocklistProps({ runtime })} />);

    await screen.findByTestId("blocked-tables");
    expect(screen.queryByTestId("minimized-query-unavailable")).toBeNull();
  });

  it("keeps the durable edit when re-reading the record afterwards fails", async () => {
    // The clear is committed before this refresh runs. A refresh that fails
    // must not take the press down with it as an unhandled rejection, and the
    // screen has to say that what it is showing may be stale rather than
    // silently showing the old entry as if nothing happened.
    const entry = {
      sha256: "a".repeat(64),
      reason: "Cheat Engine ran it and it went straight back off.",
      filename: "Table.CT",
      app_id: 10,
      game_name: "Example",
      game_version: "1.2.3",
      recorded_at: 1_700_000_000,
    };
    const onRefreshBlockedTables = vi.fn()
      .mockResolvedValueOnce({ tables: [entry], reason: null })
      .mockRejectedValue(new Error("state file vanished"));
    const onUnblockTable = vi.fn().mockResolvedValue(undefined);
    render(<AdvancedModal
      {...blocklistProps({ onRefreshBlockedTables, onUnblockTable })}
    />);

    expect(await screen.findByText("1 table marked")).toBeTruthy();
    fireEvent.click(within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "Review" }));
    fireEvent.click(within(screen.getByTestId(`blocked-table-${entry.sha256.slice(0, 12)}`)).getByRole("button", { name: "Clear" }));

    await waitFor(() => expect(onUnblockTable).toHaveBeenCalledWith(entry.sha256));
    expect(await screen.findByText(/could not be re-read just now/)).toBeTruthy();
  });

  it("shows the newest re-read of the record rather than whichever lands last", async () => {
    // Two quick edits start two reads. The older one resolving last used to
    // repaint entries the newer one had already seen cleared.
    const entry = (index: number) => ({
      sha256: `${index}`.padStart(4, "0") + "f".repeat(60),
      reason: "Cheat Engine ran it and it went straight back off.",
      filename: `Table${index}.CT`,
      app_id: 10,
      game_name: "Example",
      game_version: "1.2.3",
      recorded_at: 1_700_000_000,
    });
    const both = [entry(0), entry(1)];
    let releaseSlow: (() => void) | null = null;
    const slow = new Promise<{ tables: any[]; reason: null }>((resolve) => {
      releaseSlow = () => resolve({ tables: both, reason: null });
    });
    const onRefreshBlockedTables = vi.fn()
      .mockResolvedValueOnce({ tables: both, reason: null })
      .mockReturnValueOnce(slow)
      .mockResolvedValue({ tables: [], reason: null });
    const onUnblockTable = vi.fn().mockResolvedValue(undefined);
    render(<AdvancedModal
      {...blocklistProps({ onRefreshBlockedTables, onUnblockTable })}
    />);

    expect(await screen.findByText("2 tables marked")).toBeTruthy();
    fireEvent.click(within(screen.getByTestId("blocked-tables")).getByRole("button", { name: "Review" }));
    fireEvent.click(within(screen.getByTestId("blocked-table-0000ffffffff")).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(onRefreshBlockedTables).toHaveBeenCalledTimes(2));
    fireEvent.click(within(screen.getByTestId("blocked-table-0001ffffffff")).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(onRefreshBlockedTables).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.queryAllByTestId(/^blocked-table-/)).toHaveLength(0));

    // The stale read lands now, and changes nothing.
    releaseSlow?.();
    await slow;
    await waitFor(() => expect(screen.queryAllByTestId(/^blocked-table-/)).toHaveLength(0));
  });

  it("offers no blocklist screen when nothing has ever been marked", async () => {
    // An empty, readable record has nothing to manage, so the row states that
    // and stops there rather than leading to an empty screen.
    render(<AdvancedModal
      {...blocklistProps({ onRefreshBlockedTables: vi.fn().mockResolvedValue({ tables: [], reason: null }) })}
    />);

    expect(await screen.findByText("No tables marked")).toBeTruthy();
    expect(within(screen.getByTestId("blocked-tables")).queryByRole("button", { name: "Review" })).toBeNull();
  });

  const sourcesSnapshot = (overrides: Record<string, unknown> = {}) => ({
    schema: 1,
    sources: [
      {
        provider: "fearless", provider_display_name: "FearLess Cheat Engine", priority: 100,
        discovery: "search", linked_target: true, enabled: true, state: "ready",
        counters: {
          searches: 4, results: 12, downloads_succeeded: 2, downloads_failed: 0,
          bytes_downloaded: 4096, errors: 0, parse_degraded: 0, parse_failed: 0,
        },
        last_error: null, last_http_status: 200, last_latency_ms: 300,
        last_throttle_wait_s: null, cooldown_seconds: 0,
      },
      {
        provider: "github", provider_display_name: "GitHub", priority: 80,
        discovery: "search", linked_target: true, enabled: true, state: "error",
        counters: {
          searches: 6, results: 0, downloads_succeeded: 0, downloads_failed: 1,
          bytes_downloaded: 0, errors: 6, parse_degraded: 0, parse_failed: 2,
        },
        last_error: "GitHub returned HTTP 500", last_http_status: 500, last_latency_ms: 90,
        last_throttle_wait_s: null, cooldown_seconds: 0,
      },
    ],
    enabled_count: 2,
    total: 2,
    updated_at: null,
    selection_reason: null,
    diagnostics_reason: null,
    ...overrides,
  });

  it("shows what each table source has done and switches one off from its own screen", async () => {
    // The evidence is the reason the switch is worth offering: a source is
    // turned off after it has been seen failing, and the counters are the only
    // thing on the device that can say which one that is.
    const onSetProviderEnabled = vi.fn().mockResolvedValue(sourcesSnapshot({
      sources: [
        { ...sourcesSnapshot().sources[0] },
        { ...sourcesSnapshot().sources[1], enabled: false },
      ],
      enabled_count: 1,
    }));
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot()),
        onSetProviderEnabled,
      })}
    />);

    const panelRow = await screen.findByTestId("provider-sources");
    // Waited for, not assumed: the row renders as soon as this screen has a
    // way to read the sources, and what it says arrives with the answer.
    expect(await within(panelRow).findByText("2 of 2 sources on")).toBeTruthy();
    expect(within(panelRow).getByText(/10 searches · 12 results · 2 downloaded · 6 errors/)).toBeTruthy();

    fireEvent.click(within(panelRow).getByRole("button", { name: "Choose" }));
    const githubRow = await screen.findByTestId("provider-source-github");
    expect(within(githubRow).getByText(/6 searches · 0 results/)).toBeTruthy();
    expect(within(githubRow).getByText(/2 unreadable page\(s\)/)).toBeTruthy();
    expect(within(githubRow).getByText("Last attempt failed")).toBeTruthy();

    fireEvent.click(within(githubRow).getByRole("button", { name: "Switch off" }));
    await waitFor(() => expect(onSetProviderEnabled).toHaveBeenCalledWith("github", false));
    // Repainted from the answer the write returned, without a second read.
    await waitFor(() => expect(
      within(screen.getByTestId("provider-source-github")).getByRole("button", { name: "Switch on" }),
    ).toBeTruthy());
    expect(within(screen.getByTestId("provider-sources-summary")).getByText("1 of 2 sources on")).toBeTruthy();
  });

  it("keeps source selection open while a switch is being saved", async () => {
    let finish!: (value: unknown) => void;
    const onSetProviderEnabled = vi.fn(() => new Promise((resolve) => { finish = resolve; }));
    render(<AdvancedModal {...blocklistProps({
      onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot()), onSetProviderEnabled,
    })} />);
    const row = await screen.findByTestId("provider-sources");
    await within(row).findByText("2 of 2 sources on");
    fireEvent.click(within(row).getByRole("button", { name: "Choose" }));
    const source = await screen.findByTestId("provider-source-github");
    fireEvent.click(within(source).getByRole("button", { name: "Switch off" }));
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(screen.getByTestId("provider-source-github")).toBeTruthy();
    await act(async () => { finish(sourcesSnapshot()); });
  });

  it("says a source that has never been asked has not been searched rather than showing zeroes", async () => {
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot({
          sources: [{
            provider: "vgtimes", provider_display_name: "VGTimes", priority: 70,
            discovery: "search", linked_target: false, enabled: true, state: null, counters: null,
            last_error: null, last_http_status: null, last_latency_ms: null,
            last_throttle_wait_s: null, cooldown_seconds: 0,
          }],
          enabled_count: 1, total: 1,
        })),
      })}
    />);

    fireEvent.click(within(await screen.findByTestId("provider-sources")).getByRole("button", { name: "Choose" }));
    const row = await screen.findByTestId("provider-source-vgtimes");
    expect(within(row).getByText("Not searched yet")).toBeTruthy();
  });

  it("names the dead end when every source is switched off, and offers the one way out", async () => {
    const onResetProviderSources = vi.fn().mockResolvedValue(sourcesSnapshot());
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot({
          sources: sourcesSnapshot().sources.map((source: any) => ({ ...source, enabled: false })),
          enabled_count: 0,
        })),
        onResetProviderSources,
      })}
    />);

    const panelRow = await screen.findByTestId("provider-sources");
    expect(await within(panelRow).findByText("all off")).toBeTruthy();
    fireEvent.click(within(panelRow).getByRole("button", { name: "Choose" }));
    const summary = await screen.findByTestId("provider-sources-summary");
    expect(within(summary).getByText(/a table search cannot find anything/)).toBeTruthy();
    fireEvent.click(within(summary).getByRole("button", { name: "Switch all on" }));
    await waitFor(() => expect(onResetProviderSources).toHaveBeenCalled());
    await waitFor(() => expect(
      within(screen.getByTestId("provider-sources-summary")).getByText("2 of 2 sources on"),
    ).toBeTruthy());
  });

  it("says the source choice could not be read, and that every source is being searched meanwhile", async () => {
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot({
          selection_reason: "provider source selection has an unsupported or corrupt schema",
        })),
        onResetProviderSources: vi.fn(),
      })}
    />);

    const panelRow = await screen.findByTestId("provider-sources");
    expect(await within(panelRow).findByText(/Every source is being searched while this cannot be read/)).toBeTruthy();
  });

  it("does not invent zero counters when the record of what sources did is unreadable", async () => {
    // Zeroes state as fact that nothing has happened, which is a different and
    // much stronger claim than "this could not be read".
    const onResetProviderDiagnostics = vi.fn().mockResolvedValue(sourcesSnapshot());
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot({
          sources: sourcesSnapshot().sources.map((source: any) => ({ ...source, state: null, counters: null })),
          diagnostics_reason: "provider diagnostics state has an unsupported or corrupt schema",
        })),
        onResetProviderDiagnostics,
      })}
    />);

    const panelRow = await screen.findByTestId("provider-sources");
    // The positive assertion first, because it is what says the answer has
    // arrived: an absence checked before that passes for the wrong reason.
    expect(await within(panelRow).findByText(/What each source has done cannot be read/)).toBeTruthy();
    expect(within(panelRow).queryByText(/0 searches/)).toBeNull();

    fireEvent.click(within(panelRow).getByRole("button", { name: "Choose" }));
    const notice = await screen.findByTestId("provider-sources-diagnostics-error");
    // Ordinary use does not repair it, so the row must not say that it does.
    expect(within(notice).getByText(/Searching will not repair it/)).toBeTruthy();
    expect(within(screen.getByTestId("provider-source-github")).getByText("Counts unavailable")).toBeTruthy();

    fireEvent.click(within(notice).getByRole("button", { name: "Reset counts" }));
    await waitFor(() => expect(onResetProviderDiagnostics).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("provider-sources-diagnostics-error")).toBeNull());
  });

  it("offers only the repair that works while the source choice cannot be read", async () => {
    // Each individual switch writes by reading the same corrupt record first,
    // so leaving them enabled offered controls guaranteed to fail - and their
    // generic error would have replaced the explanation of the corruption.
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot({
          selection_reason: "provider source selection has an unsupported or corrupt schema",
        })),
        onSetProviderEnabled: vi.fn(),
        onResetProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot()),
      })}
    />);

    fireEvent.click(within(await screen.findByTestId("provider-sources")).getByRole("button", { name: "Choose" }));
    const row = await screen.findByTestId("provider-source-github");
    expect((within(row).getByRole("button", { name: "Switch off" }) as HTMLButtonElement).disabled).toBe(true);
    const summary = screen.getByTestId("provider-sources-summary");
    expect((within(summary).getByRole("button", { name: "Switch all on" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("says which sources another source's page can name, and which it cannot", async () => {
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot({
          sources: [
            { ...sourcesSnapshot().sources[1] },
            {
              provider: "vgtimes", provider_display_name: "VGTimes", priority: 70,
              discovery: "search", linked_target: false, enabled: true, state: "ready",
              counters: {
                searches: 1, results: 0, downloads_succeeded: 0, downloads_failed: 0,
                bytes_downloaded: 0, errors: 0, linked_reads: 0, parse_degraded: 0, parse_failed: 0,
              },
              last_error: null, last_http_status: 200, last_latency_ms: 50,
              last_throttle_wait_s: null, cooldown_seconds: 0,
            },
          ],
        })),
      })}
    />);

    fireEvent.click(within(await screen.findByTestId("provider-sources")).getByRole("button", { name: "Choose" }));
    fireEvent.click(within(await screen.findByTestId("provider-source-github")).getByRole("button", { name: "?" }));
    expect(await screen.findByText(/also read when another source's page names an exact page on it/)).toBeTruthy();
    fireEvent.click(within(screen.getByTestId("provider-source-vgtimes")).getByRole("button", { name: "?" }));
    expect(await screen.findByText(/nothing else points at this source/)).toBeTruthy();
  });

  it("names rows a source served through another source's page", async () => {
    render(<AdvancedModal
      {...blocklistProps({
        onLoadProviderSources: vi.fn().mockResolvedValue(sourcesSnapshot({
          sources: [{
            ...sourcesSnapshot().sources[1],
            counters: { ...sourcesSnapshot().sources[1].counters, searches: 0, results: 2, linked_reads: 1 },
          }],
        })),
      })}
    />);

    fireEvent.click(within(await screen.findByTestId("provider-sources")).getByRole("button", { name: "Choose" }));
    const row = await screen.findByTestId("provider-source-github");
    expect(within(row).getByText(/0 searches · 2 results · 1 through another source/)).toBeTruthy();
  });

  it("keeps legacy startup actions, CE de-registration and removal readiness reachable from Advanced", async () => {
    const legacy = status(true);
    legacy.profiles[0].startup = [{ record_id: 7, active: true, value: null }] as any;
    const onClearStartup = vi.fn().mockResolvedValue(0);
    const forgotten = status(true);
    forgotten.ce = { configured: false, valid: false, executable: null, sha256: null, reason: "not installed" };
    const onClearCEImport = vi.fn().mockResolvedValue({
      status: forgotten,
      ceLaunch: null,
      runtime: noRuntime(),
      appDetails: details,
      inspection: inspect,
      targetProcess: "game.exe",
    });
    const onCheckRemoval = vi.fn().mockResolvedValue({
      directories: [
        { key: "tables", label: "Tables", path: "/home/deck/.cheat-engine-decky/tables", purpose: "Every .CT you downloaded.", exists: true, file_count: 3, total_bytes: 2048, truncated: false, error: null },
        { key: "cache", label: "Provider cache", path: "/home/deck/.cheat-engine-decky/cache", purpose: "Search indexes.", exists: false, file_count: 0, total_bytes: 0, truncated: false, error: null },
      ],
      profiles_total: 2, owned_profiles: [], owned_steam_apps: 0, owned_shortcuts: 0,
      current_session_app_ids: [], session_errors: [], session_corrupt_entries: 0,
      profile_state_error: null, session_inventory_error: null, blockers: [],
      can_delete_managed_data: true, requires_frontend_restore: false,
      managed_root: "/home/deck/.cheat-engine-decky", requires_target_validation: true,
    });

    render(<AdvancedModal
      status={legacy as any}
      games={[game]}
      selectedGame={game}
      appDetails={details}
      inspection={inspect as any}
      live
      targetProcess="game.exe"
      ceLaunch={launch as any}
      launchProtonToolId=""
      runtime={noRuntime() as any}
      selfTest={null}
      busy={false}
      onRefreshGames={vi.fn().mockResolvedValue([game])}
      onSaveTargetProcess={vi.fn()}
      onPickCE={vi.fn()}
      onClearCEImport={onClearCEImport}
      onRunSelfTest={vi.fn().mockResolvedValue({ ok: true, checks: [] })}
      onLaunchProtonChange={vi.fn()}
      onRunCELaunchSelfTest={vi.fn()}
      onRefreshRuntime={vi.fn()}
      onRefreshProcesses={vi.fn()}
      onRetryAttach={vi.fn()}
      onRepairSessionState={vi.fn()}
      onRepairOwnedLaunchState={vi.fn()}
      onClearStartup={onClearStartup}
      onRevokeConsent={vi.fn()}
      onLoadDiagnostics={vi.fn().mockResolvedValue(diagnostics)}
      onCheckRemoval={onCheckRemoval}
      onRefreshAll={vi.fn()}
      onClose={vi.fn()}
    />);

    // A: legacy startup actions still run on every prepared session, so they must be
    // both visible and removable.
    expect(screen.getByText("1 run every time this exact table prepares a session")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(onClearStartup).toHaveBeenCalledTimes(1));
    // With none left the row goes away entirely rather than reporting a zero.
    await waitFor(() => expect(screen.queryByText("Legacy startup actions")).toBeNull());
    expect(screen.queryByRole("button", { name: "Clear" })).toBeNull();

    // D: a controller-only user must be able to undo a manual CE registration.
    fireEvent.click(screen.getByRole("button", { name: "Forget" }));
    await waitFor(() => expect(onClearCEImport).toHaveBeenCalledTimes(1));
    expect((await screen.findAllByText("not installed")).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Forget" })).toBeNull();
    expect((screen.getByRole("button", { name: "Verify" }) as HTMLButtonElement).disabled).toBe(true);

    // Check opens the report itself: the question a user has here is what is on
    // disk, not only whether deletion is currently allowed.
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    expect(await screen.findByText("Safe to remove")).toBeTruthy();
    // The verdict and its reasons are one header; each directory carries its own
    // size on the right, beside its explanation.
    expect(screen.getByTestId("removal-summary").textContent).toContain("3 file(s) · 2.0 KiB · 2 profile(s)");
    expect(screen.getByTestId("removal-directory-tables").textContent).toContain("3 · 2.0 KiB");
    expect(screen.getByTestId("removal-directory-cache").textContent).toContain("empty");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("button", { name: "Check" })).toBeTruthy();
    expect(document.body.textContent).toContain("/home/deck/.cheat-engine-decky");
  });

  it("disables Advanced process mutations when a connected bridge belongs to mismatched exact runtime identity", async () => {
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "f".repeat(64);
    render(<AdvancedModal
      status={status(true)}
      games={[game]}
      selectedGame={game}
      appDetails={details}
      inspection={inspect}
      targetProcess="game.exe"
      ceLaunch={launch as any}
      launchProtonToolId=""
      runtime={mismatched as any}
      selfTest={null}
      busy={false}
      onRefreshGames={vi.fn().mockResolvedValue([game])}
      onSaveTargetProcess={vi.fn()}
      onPickCE={vi.fn()}
      onClearCEImport={vi.fn()}
      onRunSelfTest={vi.fn().mockResolvedValue({ ok: true, checks: [] })}
      onLaunchProtonChange={vi.fn()}
      onRunCELaunchSelfTest={vi.fn()}
      onRefreshRuntime={vi.fn().mockResolvedValue(mismatched)}
      onRefreshProcesses={vi.fn().mockResolvedValue(mismatched)}
      onRetryAttach={vi.fn().mockResolvedValue(mismatched)}
      onRepairSessionState={vi.fn()}
      onRepairOwnedLaunchState={vi.fn()}
      onClearStartup={vi.fn().mockResolvedValue(0)}
      onRevokeConsent={vi.fn()}
      onLoadDiagnostics={vi.fn().mockResolvedValue(diagnostics)}
      onCheckRemoval={vi.fn()}
      onRefreshAll={vi.fn().mockResolvedValue({ status: status(true), ceLaunch: launch, runtime: mismatched, appDetails: details, inspection: inspect, targetProcess: "game.exe" })}
      onClose={vi.fn()}
    />);

    expect(screen.getByText("Stale / mismatched session")).toBeTruthy();
    // Retry attach needs an exact PID, which only a healthy session can supply.
    // Processes stays pressable, because a stale session is one of the states
    // whose whole recovery is choosing a different target.
    expect((screen.getByRole("button", { name: "Processes" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Retry attach" }) as HTMLButtonElement).disabled).toBe(true);
  });


  it("says once, at Review, that the table switches most of itself on", async () => {
    // The author's preset is not the user's choice, and a reader deciding
    // whether to use this table is entitled to know it turns 22 of its 24
    // cheats on by itself. One block, one sentence, and nothing on any other
    // screen.
    const flag = (id: number, declared: string | null, safe = true) => ({
      id, description: `Flag ${id}`, path: ["Enable", `Flag ${id}`], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1", declared_default: declared, switch_off_is_safe: safe,
    });
    const { unmount } = render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, controls: [flag(1, "1"), flag(2, "1"), flag(3, "0")] } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain("Of this table's 3 on/off cheats, 2 are switched on by the table itself");
    expect(block.textContent).toContain("only the ones you choose");
    unmount();
    // One default CE Decky may not write off stays running whenever its script
    // does, so the same screen may not promise the reader only their choices.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, controls: [flag(1, "1"), flag(2, "1", false), flag(3, "0")] } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    const unsafe = await screen.findByTestId("review-findings");
    expect(unsafe.textContent).toContain("1 of them stays on whenever another cheat from the same script is on");
    expect(unsafe.textContent).not.toContain("only the ones you choose");
  });

  it("says at Review that a signed table is one this Cheat Engine refuses", async () => {
    // The fact that removes a launch from the user's path: they learn it here
    // rather than by starting a game and watching nothing happen. A statement
    // about 16 measured tables, not a prediction about this one, and it refuses
    // nothing: the table is still importable and still consentable.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, has_signature: true } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain("This table is signed");
    expect(block.textContent).toContain("Cheat Engine does not open signed tables here");
    // What it will look like, which is the part that costs a user a game launch.
    expect(block.textContent).toContain("look like nothing happened");
    // Not a count of what this project measured: that belongs in the notes.
    expect(block.textContent).not.toContain("16");
    // Still usable: this screen states a fact, it does not take the press away.
    expect(screen.getByRole("button", { name: "Use this table" })).toBeTruthy();
  });

  it("offers the copy that opens, only where there is one to make", async () => {
    // The press that removes a game launch from the user's path: they learn the
    // table is refused here and leave with the copy that is not, rather than
    // starting a game to watch nothing happen. It consents to nothing - the
    // copy is a table of its own and Review opens on it.
    const onPrepareCopy = vi.fn().mockResolvedValue(undefined);
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, has_signature: true } as any}
      onUse={vi.fn()}
      onPrepareCopy={onPrepareCopy}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    const prepare = within(block).getByRole("button", { name: "Prepare a copy that works here" });
    // Drawn in its own green, which is the one thing on this screen that says
    // this press is an offer rather than another step. The colour is a class on
    // the box around it, so losing the box loses the colour silently.
    expect(prepare.closest(".ce-decky-prepare")).toBeTruthy();
    // And it sits inside the finding's own text rather than in a column beside
    // it: Steam gives a field's children a column of their own, so a paragraph
    // next to one button was a narrow strip down the left of the window with
    // half the width unused. Floated, the text runs under the button.
    const flowed = block.querySelector('div[style*="flow-root"]');
    expect(flowed).toBeTruthy();
    expect(flowed!.contains(prepare)).toBe(true);
    expect((flowed!.textContent || "")).toContain("This table is signed");
    fireEvent.click(prepare);
    expect(onPrepareCopy).toHaveBeenCalled();

    cleanup();
    // Nothing to prepare on a table this Cheat Engine will open as it is.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, has_signature: false } as any}
      onUse={vi.fn()}
      onPrepareCopy={vi.fn()}
      onCancel={vi.fn()}
    />);
    await screen.findByText("Review cheat table");
    expect(screen.queryByRole("button", { name: "Prepare a copy that works here" })).toBeNull();
  });

  it("offers the same one press for a pattern this build of the game does not hold", async () => {
    // One press for whatever is wrong. A table that is only missing a pattern
    // gets the offer too, and a repair the backend could not prove gets none:
    // a press that refuses itself when pressed is the non-information an honest
    // refusal exists to remove.
    const onPrepareCopy = vi.fn().mockResolvedValue(undefined);
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, has_signature: false, scan_count: 28 } as any}
      scanCheck={{ source: "file", present: [], missing: ["aobSpeed"], not_checked: [], elapsed_ms: 12, reason: null, repairable: true, ambiguous: [] } as any}
      scanCheckedProcess="game.exe"
      onUse={vi.fn()}
      onPrepareCopy={onPrepareCopy}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    fireEvent.click(within(block).getByRole("button", { name: "Prepare a copy that works here" }));
    // The program the answer on screen is about, so the copy is prepared
    // against the build the reader was shown.
    expect(onPrepareCopy).toHaveBeenCalledWith("game.exe");

    cleanup();
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, has_signature: false, scan_count: 28 } as any}
      scanCheck={{ source: "file", present: [], missing: ["aobSpeed"], not_checked: [], elapsed_ms: 12, reason: null, repairable: false, ambiguous: [] } as any}
      scanCheckedProcess="game.exe"
      onUse={vi.fn()}
      onPrepareCopy={vi.fn()}
      onCancel={vi.fn()}
    />);
    const second = await screen.findByTestId("review-findings");
    expect(second.textContent).toContain("aobSpeed");
    expect(within(second).queryByRole("button", { name: "Prepare a copy that works here" })).toBeNull();
  });

  it("tells the reader of a repaired copy what it cost them", async () => {
    // Both transforms in one copy, and the cheats that are gone with the hooks
    // that went. A repair that did not say so would be handing somebody a table
    // quietly missing what they came for.
    const derived = {
      ...table, sha256: "e".repeat(64), filename: "Game (repaired).CT",
      derived_from: {
        sha256: table.sha256,
        transforms: ["remove-signature", "drop-unmatched-scans"],
        scans: ["aobSpeed"], orphaned: ["Infinite boost"],
      },
    };
    render(<TableReviewModal
      table={derived as any}
      inspection={{ ...inspect, has_signature: false } as any}
      onUse={vi.fn()}
      onPrepareCopy={vi.fn()}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain("removing the signature");
    expect(block.textContent).toContain("aobSpeed");
    expect(block.textContent).toContain("One cheat is gone from this copy: Infinite boost");
  });

  it("tells the reader of a prepared copy what changed and who vouched for it", async () => {
    // The consent on this screen is for these exact bytes, and they are not the
    // bytes any source served. What changed, and what it cost, said where the
    // decision is made.
    const derived = {
      ...table, sha256: "d".repeat(64), filename: "Game (unsigned).CT",
      derived_from: { sha256: table.sha256, transforms: ["remove-signature"], scans: [], orphaned: [] },
    };
    render(<TableReviewModal
      table={derived as any}
      inspection={{ ...inspect, has_signature: false } as any}
      onUse={vi.fn()}
      onPrepareCopy={vi.fn()}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain(`CE Decky made this copy from ${table.sha256.slice(0, 12)}`);
    expect(block.textContent).toContain("Nothing else in the table changed");
    expect(block.textContent).toContain("no source vouched for these bytes");
    // Nothing left to prepare: this is what the press produces.
    expect(within(block).queryByRole("button", { name: "Prepare a copy that works here" })).toBeNull();
  });

  it("says a stop switches the table's cheats off, and names what it could not", async () => {
    // Switching a cheat off runs the script's own `[DISABLE]`, which is what
    // puts the game's bytes back, so the stop says it is about to. And a cheat
    // that would not come down is the one outcome worth interrupting somebody
    // for: nothing later can undo it, because the restore reads symbols
    // belonging to a Cheat Engine that no longer exists.
    api.stopCEForGame.mockResolvedValue({
      stopped: true, operation: null, recovered: false,
      quiesce: { asked: true, reason: "records did not settle", records_put_down: 2, records_unsettled: ["6"], elapsed_ms: 900 },
    });
    api.getStatus.mockResolvedValue(status(true));
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const manage = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.props.onRefreshHolders);
      expect(node).toBeTruthy();
      return node;
    });
    decky.toast.mockClear();
    await act(async () => { await manage.props.onRevoke(SHA, [10]); });

    const said = decky.toast.mock.calls.map((call: any[]) => String(call[0]?.body ?? ""));
    expect(said.some((line: string) => line.includes("Any cheats still on are switched off first"))).toBe(true);
    expect(said.some((line: string) => line.includes("One cheat could not be switched off"))).toBe(true);
    expect(said.some((line: string) => line.includes("Restart the game"))).toBe(true);
  });

  it("says so when it could not confirm the cheats came down at all", async () => {
    // The stop proceeds without the bridge's answer, so whatever was still on
    // stays on in a game that has just lost the Cheat Engine which could have
    // switched it off. An empty list of cheats is not the same news as no
    // answer, and reporting the second as the first read as a clean stop.
    api.stopCEForGame.mockResolvedValue({
      stopped: true, operation: null, recovered: false,
      quiesce: {
        asked: true, answered: false, cleanup_confirmed: false,
        reason: "the bridge did not answer before the stop had to proceed",
        records_put_down: null, records_unsettled: [], elapsed_ms: 15000,
      },
    });
    api.getStatus.mockResolvedValue(status(true));
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const manage = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.props.onRefreshHolders);
      expect(node).toBeTruthy();
      return node;
    });
    decky.toast.mockClear();
    await act(async () => { await manage.props.onRevoke(SHA, [10]); });

    const said = decky.toast.mock.calls.map((call: any[]) => String(call[0]?.body ?? ""));
    expect(said.some((line: string) => line.includes("before CE Decky could confirm your cheats were switched off"))).toBe(true);
    expect(said.some((line: string) => line.includes("restarting it clears that"))).toBe(true);
  });

  it("warns the same way when the bridge answered and confirmed nothing", async () => {
    // An answer saying the address list could not be read is an answer, and it
    // is not a game with nothing left switched on: zero records were looked at.
    // Every way of not knowing reaches the user as the same sentence.
    api.stopCEForGame.mockResolvedValue({
      stopped: true, operation: null, recovered: false,
      quiesce: {
        asked: true, answered: true, cleanup_confirmed: false, reason: "AddressList unavailable",
        records_put_down: 0, records_unsettled: [], elapsed_ms: 300,
      },
    });
    api.getStatus.mockResolvedValue(status(true));
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Manage", exact: true }));
    const manage = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.props.onRefreshHolders);
      expect(node).toBeTruthy();
      return node;
    });
    decky.toast.mockClear();
    await act(async () => { await manage.props.onRevoke(SHA, [10]); });

    const said = decky.toast.mock.calls.map((call: any[]) => String(call[0]?.body ?? ""));
    expect(said.some((line: string) => line.includes("before CE Decky could confirm your cheats were switched off"))).toBe(true);
  });

  it("names the byte pattern this copy of the game does not hold", async () => {
    // A script finds the game's code by scanning for one. Absent, every cheat
    // that script owns is dead at once and Cheat Engine says nothing, so the
    // reader learns it here rather than by starting a game.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, scan_count: 28 } as any}
      scanCheck={{
        source: "file", present: [], missing: ["aobAccelerationRateCalc"],
        proven_missing: ["aobAccelerationRateCalc"],
        not_checked: [], elapsed_ms: 690, reason: null,
      } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain("Of this table's 28 byte patterns, one is not");
    expect(block.textContent).toContain("aobAccelerationRateCalc");
    expect(block.textContent).toContain("will do nothing");
    // The limit that has to be in the wording: Cheat Engine searches the
    // running game, and this searched the program on disk.
    expect(block.textContent).toContain("not always what Cheat Engine sees in the running game");
  });

  it("does not call a cheat broken over a pattern searched for in the whole game", async () => {
    // A script can ask Cheat Engine to search the running game rather than one
    // named file, and this reads files. Not finding such a pattern in the
    // program is worth saying and is not proof: it can be in a library the game
    // loads, and telling the reader their cheats will do nothing would send
    // them away from a table that works.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, scan_count: 4 } as any}
      scanCheck={{
        source: "file", present: ["aobHealth"], missing: ["aobAmmo"], proven_missing: [],
        not_checked: [], elapsed_ms: 40, reason: null, repairable: false,
      } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain("aobAmmo");
    expect(block.textContent).toContain("not found in this game's main program");
    expect(block.textContent).toContain("may be in another file it loads");
    expect(block.textContent).not.toContain("will do nothing");
  });

  it("says when a pattern matches more than one place, and never that one is unique", async () => {
    // Cheat Engine's own documentation of its scanner says it "will return any
    // random match", so a pattern that is not unique in this build is a hook
    // that may land in unrelated code - on a table that otherwise looks
    // perfectly healthy, every pattern present.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, scan_count: 28 } as any}
      scanCheck={{
        source: "file", present: ["aobHealth", "aobSpeed"], missing: [], ambiguous: ["aobSpeed"],
        not_checked: [], elapsed_ms: 700, reason: null, repairable: null,
      } as any}
      scanCheckedProcess="game.exe"
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);

    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain("aobSpeed");
    expect(block.textContent).toContain("matches more than one place");
    expect(block.textContent).toContain("takes any one of the places it matches");
    // What it may not say is anything about the ones it stopped looking at.
    expect(block.textContent).not.toContain("of 28");
    expect(block.textContent).not.toContain("unique");
  });

  it("says nothing about a scan check that could not answer", async () => {
    // A pattern looked for in another of the game's files, one with no literal
    // first byte, a spent budget, a game this device has never launched: none
    // of them is evidence that the table is broken, and a screen that said so
    // on that basis would be telling the reader their table is the problem
    // when the check is.
    for (const check of [
      { source: "file", present: [], missing: [], not_checked: [{ name: "aobOne", reason: "another program" }], elapsed_ms: 2, reason: null },
      { source: "file", present: [], missing: [], not_checked: [], elapsed_ms: 0, reason: "this device does not know which program this game runs" },
      null,
    ]) {
      render(<TableReviewModal
        table={table as any}
        inspection={{ ...inspect, scan_count: 3 } as any}
        scanCheck={check as any}
        onUse={vi.fn()}
        onCancel={vi.fn()}
      />);
      await screen.findByText("Review cheat table");
      expect(screen.queryByTestId("review-findings")).toBeNull();
      cleanup();
    }
  });

  it("drops a scan finding the moment the reader picks a different program", async () => {
    // A scan answer is about one program, and this screen is where the reader
    // chooses which one Cheat Engine attaches to. What was true of the program
    // they moved away from is not true of the one in front of them.
    const onCheckScans = vi.fn().mockResolvedValue({
      source: "file", present: ["aobOne"], missing: [], not_checked: [], elapsed_ms: 4, reason: null,
    });
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, scan_count: 4, process_candidates: ["first.exe", "second.exe"] } as any}
      scanCheck={{
        source: "file", present: [], missing: ["aobOne"], not_checked: [], elapsed_ms: 5, reason: null,
      } as any}
      scanCheckedProcess="first.exe"
      onCheckScans={onCheckScans}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    // It opened on the program the answer was read against, so the finding stands.
    expect((await screen.findByTestId("review-findings")).textContent).toContain("aobOne");

    fireEvent.change(screen.getByLabelText("Game process") as HTMLSelectElement, { target: { value: "second.exe" } });

    // Gone at once rather than after the new answer arrives: nothing has been
    // established about the program now selected.
    await waitFor(() => expect(screen.queryByTestId("review-findings")).toBeNull());
    await waitFor(() => expect(onCheckScans).toHaveBeenCalledWith("second.exe"));
  });

  it("keeps the block to two findings, and drops the least consequential", async () => {
    // One block rather than a row per finding: a screen that asks one question
    // may not open with four answers. What goes is always the mildest of what
    // applies - a table that turns itself on is a caution, a table that will
    // not open and a table whose cheats are dead are not.
    const flag = (id: number) => ({
      id, description: `Flag ${id}`, path: ["Enable", `Flag ${id}`], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1", declared_default: "1",
    });
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, has_signature: true, scan_count: 9, controls: [flag(1), flag(2)] } as any}
      scanCheck={{
        source: "file", present: [], missing: ["aobOne", "aobTwo", "aobThree"],
        proven_missing: ["aobOne", "aobTwo", "aobThree"],
        not_checked: [], elapsed_ms: 12, reason: null,
      } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    const block = await screen.findByTestId("review-findings");
    expect(block.textContent).toContain("This table is signed");
    expect(block.textContent).toContain("3 are not in this copy of the game");
    // Named to a bound, counted past it: these are the author's own symbols.
    expect(block.textContent).toContain("aobOne, aobTwo and 1 more");
    expect(block.textContent).not.toContain("switched on by the table itself");
  });

  it("says nothing at Review about a table that switches nothing on by itself", async () => {
    // The test for every addition to this screen: a healthy table renders none
    // of it, and Review is the screen it always was.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, controls: [{
        id: 1, description: "Health", path: ["Health"], variable_type: "4 Bytes",
        kind: "value", group_header: false, has_assembler_script: false,
        dropdown_values: [], dropdown_read_only: false, switch_on_value: null, declared_default: null,
      }] } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    await screen.findByText("Review cheat table");
    expect(screen.queryByTestId("review-findings")).toBeNull();
  });

  it("offers the game's running executables when the table names no process", async () => {
    // The exact FearLess table for Voyage 12 names no process at all, and
    // the library entry points at a launcher rather than the game binary.
    const onUse = vi.fn().mockResolvedValue(undefined);
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, process_candidates: [] } as any}
      observedProcesses={[
        "crashpad_handler.exe", "Voyage12_Steam.exe", "explorer.exe", "plugplay.exe",
        "rpcss.exe", "LumenHollow-Win64-Shipping.exe", "services.exe", "steam.exe",
        "svchost.exe", "tabtip.exe", "winedevice.exe", "xalia.exe",
      ]}
      launchExecutable={"\"/home/deck/Games/Demo/Voyage12_Steam.exe\""}
      // The installed profile carries the older wrong automatic default. Fresh
      // exact launcher observation must repair the Review selection before the
      // user authorizes this table again.
      initialTargetProcess="Voyage12_Steam.exe"
      onUse={onUse}
      onCancel={vi.fn()}
    />);

    // The exact observation for this game on the target: two game executables
    // among ten Wine, Proton and crash-reporter processes.
    const selector = screen.getByLabelText("Game process") as HTMLSelectElement;
    expect([...selector.options].map((option) => option.textContent)).toEqual([
      "Voyage12_Steam.exe · running",
      "LumenHollow-Win64-Shipping.exe · running",
      "Enter another .exe basename…",
    ]);

    // Nothing named the process, so the game's own launch executable is skipped
    // in favour of the other running candidate, with no user action at all.
    expect(selector.value).toBe("LumenHollow-Win64-Shipping.exe");
    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    await waitFor(() => expect(onUse).toHaveBeenCalledWith("LumenHollow-Win64-Shipping.exe", expect.any(Function)));
  });

  it("offers the game's own launcher beside the table's process while nothing is running", async () => {
    // A non-Steam shortcut points at the launcher the game ships, and with the
    // game stopped that is the only other executable known to belong to it. It
    // is offered and marked, never chosen: the table's own hint stays the
    // default, so a table whose cheats live in the launcher can be pointed
    // there without typing a name blind.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, process_candidates: ["LumenHollow-Win64-Shipping.exe"] } as any}
      observedProcesses={[]}
      launchExecutable={'"/home/deck/Games/Demo/Voyage12_Steam.exe"'}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);

    const selector = screen.getByLabelText("Game process") as HTMLSelectElement;
    expect([...selector.options].map((option) => option.textContent)).toEqual([
      "LumenHollow-Win64-Shipping.exe",
      "Voyage12_Steam.exe \u00b7 launcher",
      "Enter another .exe basename…",
    ]);
    expect(selector.value).toBe("LumenHollow-Win64-Shipping.exe");
  });

  it("never offers a store client as this game's launcher", async () => {
    // A game started through a store's own client is launched by an executable
    // that belongs to no game at all, so it is not a candidate for anything.
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, process_candidates: ["LumenHollow-Win64-Shipping.exe"] } as any}
      observedProcesses={[]}
      launchExecutable={'"C:/Program Files/Epic Games/Launcher/EpicGamesLauncher.exe"'}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);

    const selector = screen.getByLabelText("Game process") as HTMLSelectElement;
    expect([...selector.options].map((option) => option.textContent)).toEqual([
      "LumenHollow-Win64-Shipping.exe",
      "Enter another .exe basename…",
    ]);
  });

  it("selects the running candidate when a table hint differs only in case", async () => {
    const onUse = vi.fn().mockResolvedValue(undefined);
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, process_candidates: ["Lumenhollow-Win64-Shipping.exe"] } as any}
      observedProcesses={["Voyage12_Steam.exe", "LumenHollow-Win64-Shipping.exe"]}
      launchExecutable={'"/home/deck/Games/Demo/Voyage12_Steam.exe"'}
      initialTargetProcess="Voyage12_Steam.exe"
      onUse={onUse}
      onCancel={vi.fn()}
    />);

    const selector = screen.getByLabelText("Game process") as HTMLSelectElement;
    expect(selector.value).toBe("LumenHollow-Win64-Shipping.exe");
    expect(screen.queryByLabelText("Process (.exe basename)")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    await waitFor(() => expect(onUse).toHaveBeenCalledWith("LumenHollow-Win64-Shipping.exe", expect.any(Function)));
  });

  it("looks for the game's processes again when it is given a way to", async () => {
    // The snapshot is taken before this screen opens and a modal never receives
    // new props, so starting the game while it is up used to change nothing at
    // all - under an instruction telling the user to start the game.
    const onRefreshProcesses = vi.fn().mockResolvedValue(["LumenHollow-Win64-Shipping.exe"]);
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, process_candidates: [] } as any}
      onRefreshProcesses={onRefreshProcesses}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    expect(screen.getByText(/Start the game and press Look again/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Refresh", exact: true }));

    await waitFor(() => expect(onRefreshProcesses).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/LumenHollow-Win64-Shipping\.exe · running/)).toBeTruthy();
  });

  it("marks the running games and preselects none when it is asked which is which", async () => {
    // More than one real game running is the one thing Home cannot settle on
    // its own. Handing over the whole installed library without saying which
    // entries caused the question left the candidates indistinguishable from a
    // few hundred games with nothing to do with it, and the first of those was
    // preselected - one press away from being taken as a deliberate answer.
    const one = { appId: 10, name: "Alpha", sortAs: "Alpha", isShortcut: false };
    const two = { appId: 11, name: "Beta", sortAs: "Beta", isShortcut: false };
    const unrelated = { appId: 12, name: "Aardvark", sortAs: "Aardvark", isShortcut: false };
    render(<GamePickerModal
      games={[unrelated, one, two] as any}
      runningGames={[one, two] as any}
      selectedGame={null}
      onPick={vi.fn()}
      onCancel={vi.fn()}
    />);

    expect(screen.getByText("More than one game is running")).toBeTruthy();
    const options = [...(screen.getByLabelText("Game") as HTMLSelectElement).options].map((option) => option.textContent);
    expect(options[0]).toBe("Choose a game…");
    expect(options[1]).toContain("Alpha");
    expect(options[1]).toContain("running");
    expect(options[2]).toContain("Beta");
    // The library stays available underneath, unmarked.
    expect(options[3]).toContain("Aardvark");
    expect(options[3]).not.toContain("running");
    // Nothing is chosen, so nothing can be accepted by one press.
    expect((screen.getByLabelText("Game") as HTMLSelectElement).value).toBe("");
    expect((screen.getByRole("button", { name: "Use this game" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("still preselects for an ordinary change of game", async () => {
    // Nothing is running, so this is the library the user came to read and the
    // first entry is a reasonable place to start.
    const one = { appId: 10, name: "Alpha", sortAs: "Alpha", isShortcut: false };
    render(<GamePickerModal games={[one] as any} selectedGame={null} onPick={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.getByText("Steam library")).toBeTruthy();
    const selector = screen.getByLabelText("Game") as HTMLSelectElement;
    expect(selector.value).toBe("10:steam");
    expect(selector.closest("label")?.dataset.layout).toBe("below");
    expect(selector.closest("label")?.dataset.childrenWidth).toBe("max");
    expect(selector.dataset.menuMatchWidth).toBe("true");
    expect(selector.dataset.menuFitWindow).toBe("true");
    expect(selector.dataset.menuShiftToFit).toBe("true");
    expect((screen.getByRole("button", { name: "Use this game" }) as HTMLButtonElement).disabled).toBe(false);
    const actions = screen.getByTestId("game-picker-actions");
    const useGame = within(actions).getByRole("button", { name: "Use this game" });
    const cancel = within(actions).getByRole("button", { name: "Cancel" });
    const focusRow = useGame.closest('[data-flow-children="row"]');
    expect(focusRow).toBeTruthy();
    expect(cancel.closest('[data-flow-children="row"]')).toBe(focusRow);
    expect(useGame.style.width).toBe("auto");
    expect(cancel.style.width).toBe("auto");
  });

  it("keeps the manual entry the only option when nothing names a process", async () => {
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspect, process_candidates: [] } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);

    // No way to look again was given, so the instruction says to reopen rather
    // than promising a refresh this screen cannot perform.
    expect(screen.getByText(/Start the game and reopen this screen, or enter the \.exe basename/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Refresh", exact: true })).toBeNull();
    expect(screen.getByLabelText("Process (.exe basename)")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("reaches the same reading of the table from the file it is stored as", async () => {
    // The other place a user asks what this exact SHA is: the row that says
    // where the file lives is the row that can open it.
    api.listTableCode.mockResolvedValue({
      schema: 1, sha256: SHA, size: 4096, records: 3, omitted_sections: 0,
      totals: { lua: 1, auto_assembler: 0, form: 0, embedded_file: 0 },
      sections: [{
        id: "lua:0", kind: "lua", title: "Table Lua script", path: [],
        bytes: 128, lines: 4, truncated: false, readable: true,
      }],
    });

    render(<AdvancedModal {...blocklistProps({ runtime: noRuntime() })} />);

    fireEvent.click(await screen.findByRole("button", { name: "Look inside" }));

    expect(await screen.findByText("Inside this table")).toBeTruthy();
    expect(screen.getByText("Table Lua script")).toBeTruthy();
    await waitFor(() => expect(api.listTableCode).toHaveBeenCalledWith(SHA));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Read" })));

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByText("Stored at")).toBeTruthy();
  });

  it("offers to read the table on the screen that asks whether it may run", async () => {
    // This is the one screen that asks whether an exact SHA may execute what it
    // carries, and the whole of the answer it offered was the marker count
    // above the button. Reading the scripts meant Desktop Mode, a file manager
    // and a text editor, none of which a user in Game Mode has.
    render(<TableReviewModal
      table={table as any}
      inspection={inspect as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);

    fireEvent.click(screen.getByRole("button", { name: "Look inside this table" }));

    // A sub-screen of the decision rather than a window over it: the process
    // choice and any activation in flight have to survive being read.
    expect(await screen.findByText("Inside this table")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Use this table" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("button", { name: "Use this table" })).toBeTruthy();
  });

  it("blocks controller Back while exact-table activation is in flight", async () => {
    let finishUse!: () => void;
    const pendingUse = new Promise<void>((resolve) => { finishUse = resolve; });
    const onCancel = vi.fn();

    render(<TableReviewModal
      table={table as any}
      inspection={inspect as any}
      live
      initialTargetProcess="game.exe"
      onUse={() => pendingUse}
      onCancel={onCancel}
    />);

    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onCancel).not.toHaveBeenCalled();

    finishUse();
    await waitFor(() => expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("says which step a long activation is on and offers to stop it", async () => {
    let finishUse!: () => void;
    let report!: (step: string, stoppable?: boolean) => void;
    const pendingUse = new Promise<void>((resolve) => { finishUse = resolve; });
    const onAbort = vi.fn().mockResolvedValue(null);
    const onCancel = vi.fn();

    render(<TableReviewModal
      table={table as any}
      inspection={inspect as any}
      live
      initialTargetProcess="game.exe"
      onUse={(_process: string, next: (step: string, stoppable?: boolean) => void) => { report = next; return pendingUse; }}
      onAbort={onAbort}
      onCancel={onCancel}
    />);

    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    // The first step is named before anything reports one, because the press
    // itself is what the user is waiting on an answer for.
    await waitFor(() => expect(screen.getByText(/Saving the selected table/)).toBeTruthy());
    // Cancel is not what a launch in flight needs: the thing to offer is a stop
    // for the Cheat Engine it is waiting on.
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    // Nothing has been started yet, so there is nothing to stop yet either. The
    // stop is on screen and not pressable, rather than promising a cancellation
    // this step cannot perform.
    expect((screen.getByRole("button", { name: /Stopping|Stop and cancel/ }) as HTMLButtonElement).disabled).toBe(true);

    report("Waiting for Cheat Engine to load the table and answer", true);
    await waitFor(() => expect(screen.getByText(/Waiting for Cheat Engine/)).toBeTruthy());
    await waitFor(() => expect((screen.getByRole("button", { name: "Stop and cancel" }) as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" }));
    await waitFor(() => expect(onAbort).toHaveBeenCalledTimes(1));

    finishUse();
    await waitFor(() => expect((screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByText(/Waiting for Cheat Engine/)).toBeNull();
  });

  it("offers the stop only for a step that owns something to stop", async () => {
    let stepReport!: (step: string, stoppable?: boolean) => void;
    const pendingUse = new Promise<void>(() => undefined);
    const onAbort = vi.fn().mockResolvedValue(null);

    render(<TableReviewModal
      table={table as any}
      inspection={inspect as any}
      live
      initialTargetProcess="game.exe"
      onUse={(_process: string, next: (step: string, stoppable?: boolean) => void) => { stepReport = next; return pendingUse; }}
      onAbort={onAbort}
      onCancel={vi.fn()}
    />);

    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    // An activation starts with durable writes that cannot be taken back, and
    // no Cheat Engine of its own yet. The stop was offered for these too, and
    // all it could do there was report that there was nothing to stop.
    await waitFor(() => expect(screen.getByRole("button", { name: "Stop and cancel" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" }));
    expect(onAbort).not.toHaveBeenCalled();

    // A launch is running, so the same button now stops the Cheat Engine it
    // owns, and it keeps that offer through the steps that use it.
    stepReport("Waiting for Cheat Engine to load the table and answer", true);
    await waitFor(() => expect((screen.getByRole("button", { name: "Stop and cancel" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" }));
    await waitFor(() => expect(onAbort).toHaveBeenCalledTimes(1));

    // A later step cannot re-arm a stop already requested for this activation.
    stepReport("Recording the authorization for this exact table");
    await waitFor(() => expect((screen.getByRole("button", { name: /Stopping|Stop and cancel/ }) as HTMLButtonElement).disabled).toBe(true));
  });

  it("blocks controller Back while runtime Apply is in flight", async () => {
    const initial = { ...liveRuntime().status.results[0], value: "100" };
    const confirmed = { ...initial, generation: 2, value: "200" };
    let finishApply!: (value: any) => void;
    const pendingApply = new Promise<any>((resolve) => { finishApply = resolve; });
    runtimeClient.queryRuntimeControls
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [initial] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [confirmed] });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [initial] , unavailable: [] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [confirmed] , unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockReturnValueOnce(pendingApply);
    const onApplied = vi.fn().mockResolvedValue(undefined);
    const onCancel = vi.fn();

    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={onCancel}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    const value = await screen.findByLabelText("Value");
    fireEvent.change(value, { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onCancel).not.toHaveBeenCalled();

    finishApply({ envelope: liveRuntime(), results: [confirmed] });
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("allows a safe Apply retry after a transient runtime failure without reopening the modal", async () => {
    const initial = { ...liveRuntime().status.results[0], value: "100" };
    const confirmed = { ...initial, generation: 2, value: "200" };
    runtimeClient.queryRuntimeControls
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [initial] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [confirmed] });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [initial] , unavailable: [] })
      // The reconciliation after the failed Apply: nothing reached the game, so
      // the staged edit survives and Apply can simply be pressed again.
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [initial] , unavailable: [] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [confirmed] , unavailable: [] });
    runtimeClient.applyRuntimeSelection
      .mockRejectedValueOnce(new Error("temporary bridge failure"))
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [confirmed] });
    const onApplied = vi.fn().mockResolvedValue(undefined);
    const onSnapshotInvalidated = vi.fn();

    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onSnapshotInvalidated={onSnapshotInvalidated}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    const value = await screen.findByLabelText("Value");
    expect(onSnapshotInvalidated).toHaveBeenCalledTimes(1);
    fireEvent.change(value, { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onSnapshotInvalidated.mock.calls.length).toBeGreaterThanOrEqual(2));
    // Once, beside Apply, which is what the user sees after scrolling to the
    // button at the bottom of a long cheat list. It used to be said there and
    // again above the list, which spent two blocks of a screen counted in rows
    // on one sentence and put one of them where the reader was not looking.
    expect((await screen.findAllByText("temporary bridge failure")).length).toBe(1);
    expect((screen.getByRole("button", { name: "Apply" }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect(onApplied.mock.calls[0][0]).toEqual([{ record_id: 7, active: null, value: "200" }]);
  });

});

describe("Cheat selection workflow", () => {
  const manyControls = Array.from({ length: 10 }, (_, index) => ({
    id: 100 + index,
    description: `Cheat ${index}`,
    path: ["Enable 1.0", `Cheat ${index}`],
    variable_type: "4 Bytes",
    kind: index % 2 === 0 ? "value" : "script",
    group_header: false,
    has_assembler_script: index % 2 === 1,
    dropdown_values: [],
    dropdown_read_only: false,
  }));
  const wideInspection = { ...inspect, total_entries: 10, controls: manyControls };

  function renderCheatModal(overrides: Record<string, any> = {}) {
    return render(<CheatSelectionModal
      appId={10}
      inspection={wideInspection as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onCancel={vi.fn()}
      {...overrides}
    />);
  }

  it("makes a value list of thousands usable with a controller instead of scrollable", async () => {
    // 0.9.14 raised the parser's ceiling on a value list to 16384 after a real
    // table declared 6508 items in each of three pickers. Handing that list
    // straight to one Decky dropdown made those tables parse and stay unusable:
    // a controller walks it one item at a time, there is no search and no jump,
    // and a read-only dropdown is the one control with no typed value to fall
    // back on.
    const values = Array.from({ length: 6508 }, (_, index) => [String(index + 1), `Item ${index + 1}`]);
    const picker = {
      id: 300, description: "Weapon", path: ["Weapon"], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: values, dropdown_read_only: true,
    };
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(), results: [{ record_id: 300, ok: true, active: false, value: "4211" }], unavailable: [],
    });
    renderCheatModal({ inspection: { ...inspect, total_entries: 1, controls: [picker] } as any });

    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    const options = () => (screen.getByLabelText("Value") as HTMLSelectElement).options;
    // Bounded whatever the table declares, and the record's own value is on
    // offer so the dropdown shows what this record is actually set to.
    expect(options().length).toBeLessThanOrEqual(24);
    expect(Array.from(options()).some((option) => option.value === "4211")).toBe(true);

    // Typing is how a value deep in the list is reached at all.
    fireEvent.change(screen.getByLabelText("Find a value"), { target: { value: "6508" } });
    await waitFor(() => expect(Array.from(options()).map((option) => option.value)).toContain("6508"));
    expect(options().length).toBeLessThanOrEqual(24);

    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "6508" } });
    await waitFor(() => expect((screen.getByLabelText("Value") as HTMLSelectElement).value).toBe("6508"));
  });

  it("draws a two-entry on/off record as a plain switch, with no list and no field", async () => {
    // The instruction this exists for: a table of `bEnable*` flags drew a
    // dropdown and a text field on every one of them, so switching on a cheat
    // meant answering a question the toggle beside it had already answered.
    const flag = {
      id: 302, description: "bEnableGodMode", path: ["bEnableGodMode"], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1",
    };
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(), results: [{ record_id: 302, ok: true, active: false, value: "0" }], unavailable: [],
    });
    renderCheatModal({ inspection: { ...inspect, total_entries: 1, controls: [flag] } as any });

    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    expect(screen.queryByLabelText("Value")).toBeNull();
    expect(screen.queryByLabelText("Custom value")).toBeNull();
    expect(screen.queryByLabelText("Find a value")).toBeNull();
    // The row's own switch is still there, and it is the whole control.
    expect(screen.getByTestId("cheat-row-302")).toBeTruthy();
    // A switch has no value of its own to save, so the note about a value
    // waiting for its script is never said of one.
    expect(screen.queryByText(/Value saved; written when/)).toBeNull();
  });

  it("holds off the flags a script would switch on by itself", async () => {
    // The table author's own defaults: one real table declares 22 of its 24
    // flags as on, so switching the script on for one cheat switched on most of
    // the table while the panel counted the one cheat and said `1 active`.
    const script = {
      id: 400, description: "Enable", path: ["Enable"], variable_type: "Auto Assembler Script",
      kind: "script", group_header: false, has_assembler_script: true,
      dropdown_values: [], dropdown_read_only: false, switch_on_value: null,
    };
    const flag = (id: number, name: string) => ({
      id, description: name, path: ["Enable", name], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1",
      // What the script declares for its own symbol: this is the flag it
      // switches on by itself, which is the whole reason to hold it off. And
      // the backend read this script's own code and found that writing the flag
      // off is something its hooks survive, which is what allows the write at
      // all: a table whose code was not read leaves its cheats on.
      declared_default: "1",
      switch_off_is_safe: true,
    });
    const chosen = flag(401, "bEnableGodMode");
    const unasked = flag(402, "bEnableOneHitKill");
    const live = liveRuntime();
    live.status.results = [
      { generation: 1, record_id: 400, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 401, ok: true, active: false, value: "0", error: null },
      { generation: 1, record_id: 402, ok: true, active: false, value: "0", error: null },
    ];
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: live, results: live.status.results, unavailable: [],
    });
    renderCheatModal({
      inspection: { ...inspect, total_entries: 3, controls: [script, chosen, unasked] } as any,
    });

    const row = await screen.findByTestId("cheat-row-401");
    fireEvent.click(within(row).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalled());
    const sent = runtimeClient.applyRuntimeSelection.mock.calls.at(-1)![1];
    const byId = new Map(sent.map((state: any) => [state.record_id, state]));
    // The script goes on because the chosen cheat needs it, the chosen cheat
    // goes on, and the one nobody asked for is written to its off key without
    // being switched at all.
    expect(byId.get(400)).toMatchObject({ active: true });
    expect(byId.get(401)).toMatchObject({ active: true });
    expect(byId.get(402)).toMatchObject({ active: null, value: "0" });
  });

  it("leaves on a cheat whose code was not read as surviving it, and hands the list up", async () => {
    // Writing that flag off is what killed a real game two minutes later: the
    // hook that reads it turns a pointer into an offset and, on the branch
    // taken when the flag is off, hands the game back the offset. So it stays
    // on, and the press that started the script says so, because the panel's
    // own count cannot show it: it is a value in the game rather than a record
    // Cheat Engine has activated.
    const script = {
      id: 400, description: "Enable", path: ["Enable"], variable_type: "Auto Assembler Script",
      kind: "script", group_header: false, has_assembler_script: true,
      dropdown_values: [], dropdown_read_only: false, switch_on_value: null,
    };
    const flag = (id: number, name: string, safe: boolean) => ({
      id, description: name, path: ["Enable", name], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1", declared_default: "1", switch_off_is_safe: safe,
    });
    const chosen = flag(401, "bEnableGodMode", true);
    const safe = flag(402, "bEnableOneHitKill", true);
    const unsafe = flag(403, "bEnableVitalsDrain", false);
    const live = liveRuntime();
    live.status.results = [
      { generation: 1, record_id: 400, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 401, ok: true, active: false, value: "0", error: null },
      { generation: 1, record_id: 402, ok: true, active: false, value: "0", error: null },
      { generation: 1, record_id: 403, ok: true, active: false, value: "1", error: null },
    ];
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: live, results: live.status.results, unavailable: [],
    });
    const applied = vi.fn().mockResolvedValue(undefined);
    renderCheatModal({
      inspection: { ...inspect, total_entries: 4, controls: [script, chosen, safe, unsafe] } as any,
      onApplied: applied,
    });

    const row = await screen.findByTestId("cheat-row-401");
    fireEvent.click(within(row).getByTestId("toggle"));
    // The row says it before anything is pressed, too.
    expect((await screen.findByTestId("cheat-row-403")).textContent).toContain("not safe to switch off");
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalled());
    const sent = runtimeClient.applyRuntimeSelection.mock.calls.at(-1)![1];
    const byId = new Map(sent.map((state: any) => [state.record_id, state]));
    // The one that was read and found safe is written off; the one that was not
    // is not written at all.
    expect(byId.get(402)).toMatchObject({ active: null, value: "0" });
    expect(byId.get(403)).toBeUndefined();
    await waitFor(() => expect(applied).toHaveBeenCalled());
    expect((applied.mock.calls.at(-1)![2] ?? []).map((control: any) => control.id)).toEqual([403]);
  });

  it("holds the same flags off when the reader switches the script on themselves", async () => {
    // The defaults arrive with the script whoever started it. Holding them off
    // only for a script CE Decky switched on meant doing it by hand brought the
    // whole table's declarations with it, which is the thing this prevents.
    const script = {
      id: 400, description: "Enable", path: ["Enable"], variable_type: "Auto Assembler Script",
      kind: "script", group_header: false, has_assembler_script: true,
      dropdown_values: [], dropdown_read_only: false, switch_on_value: null,
    };
    const unasked = {
      id: 402, description: "bEnableOneHitKill", path: ["Enable", "bEnableOneHitKill"],
      variable_type: "4 Bytes", kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1", declared_default: "1", switch_off_is_safe: true,
    };
    const chosen = {
      id: 401, description: "bEnableGodMode", path: ["Enable", "bEnableGodMode"],
      variable_type: "4 Bytes", kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1",
    };
    const live = liveRuntime();
    live.status.results = [
      { generation: 1, record_id: 400, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 401, ok: true, active: false, value: "0", error: null },
      { generation: 1, record_id: 402, ok: true, active: false, value: "0", error: null },
    ];
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: live, results: live.status.results, unavailable: [],
    });
    renderCheatModal({
      inspection: { ...inspect, total_entries: 3, controls: [script, chosen, unasked] } as any,
    });

    // An enclosing script is listed with the machinery rather than the cheats,
    // because it is not a choice most readers make. This is the one who does,
    // and switches on a cheat under it in the same press.
    const scripts = await screen.findByTestId("show-scripts");
    fireEvent.click(within(scripts).getByTestId("toggle"));
    const row = await screen.findByTestId("cheat-row-400");
    fireEvent.click(within(row).getByTestId("toggle"));
    fireEvent.click(within(await screen.findByTestId("cheat-row-401")).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalled());
    const sent = runtimeClient.applyRuntimeSelection.mock.calls.at(-1)![1];
    const byId = new Map(sent.map((state: any) => [state.record_id, state]));
    expect(byId.get(400)).toMatchObject({ active: true });
    // Written to its off key, and marked as CE Decky's own ask so a read-back
    // failure says whose it was.
    expect(byId.get(402)).toMatchObject({ active: null, value: "0", held_off: true });
  });

  it("writes nothing into the game for a value stored against a script that is off", async () => {
    // Measured on the device: the reader pinned two cheats, switched nothing on
    // and typed a value, and Apply came back with an error naming a flag they
    // had never seen. Every enclosing script in the table was treated as one
    // this press manages, so the flags under all of them were written to their
    // off keys - at addresses their scripts had not allocated yet. The value
    // the reader did type is a pre-game setting and was already deferred; the
    // press has nothing to send at all.
    const script = {
      id: 400, description: "Enable", path: ["Enable"], variable_type: "Auto Assembler Script",
      kind: "script", group_header: false, has_assembler_script: true,
      dropdown_values: [], dropdown_read_only: false, switch_on_value: null,
    };
    const damage = {
      id: 401, description: "fPlayerWeaponDamageMod", path: ["Enable", "fPlayerWeaponDamageMod"],
      variable_type: "Float", kind: "value", group_header: false, has_assembler_script: false,
      dropdown_values: [], dropdown_read_only: false, switch_on_value: null,
    };
    const unasked = {
      id: 402, description: "bEnableVitalsDrainRateMod", path: ["Enable", "bEnableVitalsDrainRateMod"],
      variable_type: "4 Bytes", kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: false,
      switch_on_value: "1", declared_default: "1",
    };
    const live = liveRuntime();
    live.status.results = [
      { generation: 1, record_id: 400, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 401, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 402, ok: true, active: false, value: "0", error: null },
    ];
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: live, results: live.status.results, unavailable: [],
    });
    const onSaveConfiguredValues = vi.fn().mockResolvedValue(undefined);
    renderCheatModal({
      inspection: { ...inspect, total_entries: 3, controls: [script, damage, unasked] } as any,
      onSaveConfiguredValues,
    });

    const row = await screen.findByTestId("cheat-row-401");
    fireEvent.click(within(row).getByRole("button", { name: "More" }));
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    // The value is kept for the next time this table is loaded, and the game is
    // not touched: nothing is sent, so nothing can read back as missing.
    await waitFor(() => expect(onSaveConfiguredValues).toHaveBeenCalledWith([{ record_id: 401, value: "4" }]));
    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalled());
    expect(runtimeClient.applyRuntimeSelection.mock.calls.at(-1)![1]).toEqual([]);
  });

  it("offers the list alone, and the free field behind one press", async () => {
    // A table never says its list is exhaustive - not one of the corpus's 1414
    // list records declares `DropDownReadOnly` - so the field has to stay
    // reachable. Beside every list it was a second control on a row that needed
    // one, which is the clutter drawing switches as switches exists to remove.
    const picker = {
      id: 305, description: "Weapon", path: ["Weapon"], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Pistol"], ["1", "Rifle"]], dropdown_read_only: false,
    };
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(), results: [{ record_id: 305, ok: true, active: false, value: "0" }], unavailable: [],
    });
    renderCheatModal({ inspection: { ...inspect, total_entries: 1, controls: [picker] } as any });

    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    expect(screen.getByLabelText("Value")).toBeTruthy();
    expect(screen.queryByLabelText("Custom value")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Type a value instead" }));

    // And the press is gone with it: the field it opens is the answer.
    expect(screen.getByLabelText("Custom value")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Type a value instead" })).toBeNull();
  });

  it("shows the field and no list for a list with one entry", async () => {
    // 332 records in the corpus declare a single value. That is a control with
    // nothing to choose from, and what the record actually has is a value.
    const single = {
      id: 306, description: "Item", path: ["Item"], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["7", "Medkit"]], dropdown_read_only: false,
    };
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(), results: [{ record_id: 306, ok: true, active: false, value: "7" }], unavailable: [],
    });
    renderCheatModal({ inspection: { ...inspect, total_entries: 1, controls: [single] } as any });

    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    // The field, labelled as a value rather than as a custom one: there is no
    // list beside it for it to be custom to.
    const field = screen.getByLabelText("Value");
    expect(field.tagName).toBe("INPUT");
    expect(screen.queryByLabelText("Custom value")).toBeNull();
    expect(screen.queryByRole("button", { name: "Type a value instead" })).toBeNull();
  });

  it("keeps the one entry a read-only list declares, which is all that record may be", async () => {
    // The field is what replaces a list of one, and a read-only record has no
    // field: taking its list away left the row with no control at all.
    const single = {
      id: 308, description: "Mode", path: ["Mode"], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["3", "Hardcore"]], dropdown_read_only: true,
    };
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(), results: [{ record_id: 308, ok: true, active: false, value: "3" }], unavailable: [],
    });
    renderCheatModal({ inspection: { ...inspect, total_entries: 1, controls: [single] } as any });

    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    const list = screen.getByLabelText("Value");
    expect(list.tagName).toBe("SELECT");
    expect(screen.queryByRole("button", { name: "Type a value instead" })).toBeNull();
  });

  it("keeps a value the list does not offer in front of the reader", async () => {
    // A record that allows free entry is for exactly this, and hiding what the
    // reader already set behind a press would hide their own answer.
    const picker = {
      id: 307, description: "Ammo", path: ["Ammo"], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Empty"], ["1", "Full"]], dropdown_read_only: false,
    };
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(), results: [{ record_id: 307, ok: true, active: false, value: "999" }], unavailable: [],
    });
    renderCheatModal({ inspection: { ...inspect, total_entries: 1, controls: [picker] } as any });

    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    expect((screen.getByLabelText("Custom value") as HTMLInputElement).value).toBe("999");

    // And clearing it does not take the field away mid-edit: the reader is
    // typing in it, which is the same as having asked for it.
    fireEvent.change(screen.getByLabelText("Custom value"), { target: { value: "" } });
    expect(screen.getByLabelText("Custom value")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Custom value"), { target: { value: "5" } });
    expect((screen.getByLabelText("Custom value") as HTMLInputElement).value).toBe("5");
  });

  it("leaves a value list that fits the panel exactly as it was", async () => {
    const picker = {
      id: 301, description: "Difficulty", path: ["Difficulty"], variable_type: "4 Bytes",
      kind: "dropdown", group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Easy"], ["1", "Hard"]], dropdown_read_only: true,
    };
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(), results: [{ record_id: 301, ok: true, active: false, value: "0" }], unavailable: [],
    });
    renderCheatModal({ inspection: { ...inspect, total_entries: 1, controls: [picker] } as any });

    fireEvent.click(await screen.findByRole("button", { name: "More" }));
    expect((screen.getByLabelText("Value") as HTMLSelectElement).options.length).toBe(2);
    expect(screen.queryByLabelText("Find a value")).toBeNull();
  });

  it("waits in the header beside the Scripts toggle rather than in a row of its own", async () => {
    // The header exists before the controls do and has spare width on its
    // right. A spinner in a row under it moved the whole dialog down for the
    // length of the read and then let it jump back up.
    let settle: (value: any) => void = () => undefined;
    runtimeClient.queryRuntimeControlsPartial.mockReturnValue(new Promise((resolve) => { settle = resolve; }));
    renderCheatModal();

    const spinner = await screen.findByTestId("cheats-loading");
    expect(spinner.parentElement).toBe(screen.getByTestId("show-scripts").parentElement);
    await act(async () => {
      settle({ envelope: liveRuntime(), results: [], unavailable: [] });
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.queryByTestId("cheats-loading")).toBeNull());
  });

  it("hands a refused Apply to the panel, from the runtime path that produces it", async () => {
    // The hook belongs on the mutation that activates cheats. Wired to the pin
    // write instead - which only stores durable metadata and never activates
    // anything - the whole flow is dead code that no amount of prop-level
    // testing notices.
    const onTableRefused = vi.fn().mockResolvedValue(undefined);
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(),
      results: [{ generation: 1, record_id: 100, ok: true, active: false, value: null, error: null }],
      unavailable: [],
    });
    const refusal: any = new Error("“Cheat 0” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    renderCheatModal({ onTableRefused });

    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/).length).toBeGreaterThan(0));
    const row = screen.getByTestId("cheat-row-100");
    fireEvent.click(within(row).getByTestId("toggle"));
    // A value record is not finished by being switched on, and Apply refuses
    // one with nothing in it before it reaches the runtime at all. The subject
    // here is what the runtime does with a complete request.
    fireEvent.change(within(row).getByLabelText("Value"), { target: { value: "500" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onTableRefused).toHaveBeenCalledTimes(1));
    expect(onTableRefused.mock.calls[0][0]).toMatch(/did not switch on/);
  });

  it("fits the 800p viewport by paging six single-row cheat blocks", async () => {
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: liveRuntime(), results: [] }, unavailable: [] });
    renderCheatModal();

    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));
    // A collapsed record is one row: no Active/Value/Pinned rows of its own and
    // no raw record ID on the primary surface.
    expect(screen.queryByLabelText("Pinned")).toBeNull();
    expect(screen.queryByLabelText("Value")).toBeNull();
    expect(screen.queryByText("ID 100", { exact: false })).toBeNull();
    expect(screen.getAllByRole("button", { name: "More" })).toHaveLength(CONTROL_PAGE_SIZE);
    // Paging and the modal actions share one compact footer, in their visual
    // order: list navigation first, commit and exit together on the right.
    const footer = screen.getByTestId("cheat-footer");
    const focusRows = new Set<Element>();
    for (const name of ["\u2039 Previous", "Next \u203a", "Apply", "Cancel"]) {
      const button = within(footer).getByRole("button", { name });
      expect(button).toBeTruthy();
      const focusRow = button.closest('[data-flow-children="row"]');
      expect(focusRow).toBeTruthy();
      focusRows.add(focusRow!);
    }
    expect(focusRows.size).toBe(1);
    // Reported from the device: Apply and Cancel were stuck together. The right
    // hand slot of the footer holds more than one control on this screen, and a
    // box with no gap in it is two buttons drawn as one wide one.
    const apply = within(footer).getByRole("button", { name: "Apply" });
    expect((apply.parentElement as HTMLElement).style.gap).toBe("8px");
    expect(within(footer).getByRole("button", { name: "Cancel" }).closest("div")?.parentElement)
      .toBe(apply.parentElement);
    const next = within(footer).getByRole("button", { name: "Next \u203a" });
    expect(next.closest('[data-flow-children="row"]')?.getAttribute("data-nav-entry")).toBe("4");
    expect(next.getAttribute("data-preferred-focus")).toBe("true");
    expect(screen.getByText("1 / 2")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Next \u203a" }));
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(4));
    expect(screen.getByText("2 / 2")).toBeTruthy();
  });

  it("keeps the ring on the pager the reader is using, through both ends of the list", async () => {
    // Pressing Next onto the last page disables Next under the thumb that was
    // on it, and Previous onto the first page does the same at the other end.
    // Either way the ring is dropped and the window is left with nothing
    // focused, which is the same defect the code window already carries a fix
    // for.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: liveRuntime(), results: [] }, unavailable: [] });
    renderCheatModal();
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    const footer = screen.getByTestId("cheat-footer");
    const pager = (name: "Previous" | "Next") => within(footer)
      .getByRole("button", { name: name === "Next" ? "Next \u203a" : "\u2039 Previous" });
    fireEvent.click(pager("Next"));
    await waitFor(() => expect(screen.getByText("2 / 2")).toBeTruthy());
    // The last page, so Next has just disabled itself and the ring moves to the
    // control beside it rather than off the window.
    expect((pager("Next") as HTMLButtonElement).disabled).toBe(true);
    expect(document.activeElement).toBe(pager("Previous"));

    fireEvent.click(pager("Previous"));
    await waitFor(() => expect(screen.getByText("1 / 2")).toBeTruthy());
    expect((pager("Previous") as HTMLButtonElement).disabled).toBe(true);
    expect(document.activeElement).toBe(pager("Next"));
  });

  it("leaves focus alone when narrowing the list puts it back to its first page", async () => {
    // The page also moves without a press. Focus is on the control that
    // narrowed the list by then, and pulling it down to the footer would move
    // the user off the thing they are using.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: liveRuntime(), results: [] }, unavailable: [] });
    renderCheatModal();
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));
    fireEvent.click(within(screen.getByTestId("cheat-footer")).getByRole("button", { name: "Next \u203a" }));
    await waitFor(() => expect(screen.getByText("2 / 2")).toBeTruthy());

    const search = screen.getByLabelText("Filter") as HTMLInputElement;
    search.focus();
    fireEvent.change(search, { target: { value: "Cheat 1" } });
    await waitFor(() => expect(screen.getByText("1 / 1")).toBeTruthy());
    expect(document.activeElement).toBe(search);
  });

  it("keeps value editing, pinning and the record ID behind that record's More", async () => {
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: liveRuntime(), results: [] }, unavailable: [] });
    renderCheatModal();
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    const row = screen.getByTestId("cheat-row-100");
    expect(row.classList.contains("ce-decky-open")).toBe(false);
    fireEvent.click(within(row).getByRole("button", { name: "More" }));
    // Only an open row is marked, so only its heading is lifted above the rows
    // it revealed.
    expect(row.classList.contains("ce-decky-open")).toBe(true);
    expect(within(row).getByLabelText("Value")).toBeTruthy();
    expect(within(row).getByLabelText("Pinned")).toBeTruthy();
    expect(row.textContent).toContain("ID 100");
    // Only one record expands at a time, so the page cannot grow without bound.
    expect(screen.getAllByRole("button", { name: "Less" })).toHaveLength(1);

    const other = screen.getByTestId("cheat-row-101");
    fireEvent.click(within(other).getByRole("button", { name: "More" }));
    expect(within(row).queryByLabelText("Value")).toBeNull();
    // A script record has no typed value, only pinning and identity.
    expect(within(other).queryByLabelText("Value")).toBeNull();
    expect(within(other).getByLabelText("Pinned")).toBeTruthy();

    fireEvent.click(within(other).getByRole("button", { name: "Less" }));
    expect(screen.queryByLabelText("Pinned")).toBeNull();
  });

  it("refuses to apply a cheat switched on with the value it needs left empty", async () => {
    // Switching one of these on without a value is not a cheat that does
    // nothing: Cheat Engine freezes whatever the game happens to hold at that
    // address. Apply took it, wrote it into this table's startup state and
    // reported success, so the next session switched it on the same way.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(),
      unavailable: [],
      results: manyControls.map((control) => ({
        generation: 1, record_id: control.id, ok: true, active: false, value: null, error: null,
      })),
    });
    const onSaveConfiguredValues = vi.fn().mockResolvedValue(undefined);
    const onValidateStartupPlan = vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true });
    const onApplied = vi.fn().mockResolvedValue(undefined);
    renderCheatModal({ onSaveConfiguredValues, onValidateStartupPlan, onApplied });
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    // Record 100 is a value record: switching it on is the whole of the input.
    fireEvent.click(within(screen.getByTestId("cheat-row-100")).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    const failure = await screen.findByTestId("cheat-apply-error");
    expect(failure.textContent).toContain("Cheat 0");
    expect(failure.textContent).toContain("needs a value");
    // Nothing durable, nothing live, and nothing validated: the refusal comes
    // before any of it, so the press costs one message and no round trip.
    expect(onSaveConfiguredValues).not.toHaveBeenCalled();
    expect(onValidateStartupPlan).not.toHaveBeenCalled();
    expect(onApplied).not.toHaveBeenCalled();
    expect(runtimeClient.applyRuntimeSelection).not.toHaveBeenCalled();

    // And it applies once the value is there.
    fireEvent.change(within(screen.getByTestId("cheat-row-100")).getByLabelText("Value"), { target: { value: "500" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onValidateStartupPlan).toHaveBeenCalledTimes(1));
  });

  it("reports a pinned cheat with no value when Apply is pressed, not when it is pinned", async () => {
    // Pinning is committed on its own and is only metadata about which cheats
    // the panel shows, so it behaves like the Active switch: it works, and
    // Apply is the one place on this screen that reports a missing value. What
    // it prevents is a switch on the panel with nowhere to type, which could
    // only ever be switched on empty.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(),
      unavailable: [],
      results: manyControls.map((control) => ({
        generation: 1, record_id: control.id, ok: true, active: false, value: null, error: null,
      })),
    });
    const onTogglePin = vi.fn().mockResolvedValue([100]);
    renderCheatModal({ onTogglePin });
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    const row = screen.getByTestId("cheat-row-100");
    fireEvent.click(within(row).getByRole("button", { name: "More" }));
    fireEvent.click(within(row).getByLabelText("Pinned"));

    // The pin goes through and says nothing, exactly as switching a cheat on
    // says nothing until Apply.
    await waitFor(() => expect(onTogglePin).toHaveBeenCalledWith(100, true));
    expect(screen.queryByTestId("cheat-apply-error")).toBeNull();

    // Switched on as well as pinned, so it is in both answers and must still
    // be named once.
    fireEvent.click(within(row).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    const failure = await screen.findByTestId("cheat-apply-error");
    expect(failure.textContent).toContain("needs a value");
    expect(failure.textContent?.match(/Cheat 0/g)).toHaveLength(1);
  });

  it("does not offer to discard a form put back the way it opened", async () => {
    // The count was of records the reader had touched, so switching a cheat on
    // and off again still said one unapplied change and asked what to do about
    // it - a prompt about nothing, which teaches people to dismiss the one
    // that matters.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(),
      unavailable: [],
      results: manyControls.map((control) => ({
        generation: 1, record_id: control.id, ok: true, active: false, value: null, error: null,
      })),
    });
    const onCancel = vi.fn();
    renderCheatModal({ onCancel });
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    const toggle = () => within(screen.getByTestId("cheat-row-100")).getByTestId("toggle");
    fireEvent.click(toggle());
    fireEvent.click(toggle());

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("unsaved-prompt")).toBeNull();
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("holds a short last page open so the window keeps its height", async () => {
    // Ten controls over pages of six leaves four on the last one, and without
    // this the window shrank on reaching it, taking Apply and the way out of
    // the screen somewhere new. The height is measured off a full page rather
    // than multiplied out of a pixel constant, because that constant is a copy
    // of what the stylesheet does and was already out by a couple of pixels.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: liveRuntime(), results: [] }, unavailable: [] });
    // jsdom measures nothing, so the list and its rows are given heights to be
    // read: six rows of 44 in a box of 264, which is the shape the arithmetic
    // is about. A mock that answered the same height for a row and for the
    // whole list made a short page estimate six full lists.
    const measured = vi.spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        const height = (this as HTMLElement).dataset?.testid === "cheat-list" ? 264 : 44;
        return { height, top: 0, bottom: 0, left: 0, right: 0, width: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
      });
    try {
      renderCheatModal();
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));
      await waitFor(() => expect(screen.getByTestId("cheat-list").style.minHeight).toBe("264px"));

      fireEvent.click(screen.getByRole("button", { name: "Next \u203a" }));
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(4));
      // Still the height the full page had, so nothing below the list moved.
      expect(screen.getByTestId("cheat-list").style.minHeight).toBe("264px");
    } finally {
      measured.mockRestore();
    }
  });

  it("sizes its page to the screen it is drawn on, footer and Steam's own padding included", async () => {
    // The layout this window settled on, read off a Steam Deck with
    // `scripts/target_panel_read.py --metrics`: 233 pixels down the page to the
    // list, a 48 pixel footer under it, and a 534 pixel page whose last 41 are
    // Steam's own bar with 26 of Steam's modal padding above that. Four cheats
    // fit. The fifth is the one that used to be offered, and it was drawn
    // behind the bar.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: [], unavailable: [] });
    const page = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
    const rect = (top: number, height: number) => ({
      top, height, bottom: top + height, left: 0, right: 0, width: 0, x: 0, y: top, toJSON: () => ({}),
    } as DOMRect);
    const measured = vi.spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        if ((this as HTMLElement).dataset?.testid === "cheat-list") return rect(233, 172);
        // The box below the list carries no test id of its own: an id there
        // would make the footer's own rows nest under one, and the panel reader
        // reports a nested row once, by its outer one. It is found by what it
        // holds instead. It ends at 453, and 453 plus the 67 a window may not
        // use is inside the page, which is the layout that fits.
        const first = this.firstElementChild as HTMLElement | null;
        if (first?.dataset?.testid === "cheat-footer") return rect(405, 48);
        return rect(0, 43);
      });
    try {
      renderCheatModal();
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(4));
    } finally {
      measured.mockRestore();
      if (page) Object.defineProperty(window, "innerHeight", page);
    }
  });

  it("gives up a row when what it drew still ends behind Steam's bar", async () => {
    // The arithmetic is on measurements taken while the screen was being laid
    // out, and those can be of a layout the reader never settles on. So the
    // same statement is checked against where the screen actually ended up:
    // here the footer ends at 497 on a 534 pixel page, which is 30 past the 67
    // a window may not use, and 30 is one row of 43.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: [], unavailable: [] });
    const page = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
    const rect = (top: number, height: number) => ({
      top, height, bottom: top + height, left: 0, right: 0, width: 0, x: 0, y: top, toJSON: () => ({}),
    } as DOMRect);
    const measured = vi.spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        if ((this as HTMLElement).dataset?.testid === "cheat-list") return rect(233, 216);
        const first = this.firstElementChild as HTMLElement | null;
        if (first?.dataset?.testid === "cheat-footer") return rect(449, 48);
        return rect(0, 43);
      });
    try {
      renderCheatModal();
      // Shrinking is bounded rather than spent once, because one row is not
      // always enough. This double cannot shrink its own window, so the layout
      // goes on reporting the same overflow and the rule spends its whole
      // allowance - which is the bound doing its job, and the point of having
      // one: a screen whose own chrome is taller than the display cannot be
      // paged out of that, and without a bound this would take the list away.
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(2));
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(2);
    } finally {
      measured.mockRestore();
      if (page) Object.defineProperty(window, "innerHeight", page);
    }
  });

  it("takes a row back when what it drew ended well above Steam's bar", async () => {
    // The same transient cuts both ways. A screen that measured its chrome
    // before part of it was drawn asks for too many rows; one that measured it
    // while a status row was still up asks for too few, and a Steam Deck left
    // 54 pixels of room under one of these lists. Room is only taken in whole
    // rows, and only while there is anything left in the list to put there.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: [], unavailable: [] });
    const page = Object.getOwnPropertyDescriptor(window, "innerHeight");
    Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
    const rect = (top: number, height: number) => ({
      top, height, bottom: top + height, left: 0, right: 0, width: 0, x: 0, y: top, toJSON: () => ({}),
    } as DOMRect);
    const measured = vi.spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        if ((this as HTMLElement).dataset?.testid === "cheat-list") return rect(233, 129);
        const first = this.firstElementChild as HTMLElement | null;
        // Ends at 410, which is 57 short of the 467 this page allows: one row
        // of 43 with 14 left over, so exactly one row is taken.
        if (first?.dataset?.testid === "cheat-footer") return rect(362, 48);
        return rect(0, 43);
      });
    try {
      renderCheatModal();
      // The arithmetic asked for four; the room under the window is another
      // row, and this list has six in it.
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(5));
      // And it takes it once. The double cannot shrink its own window, so the
      // room is still there to be read the next time round, and a rule that
      // took it again would go on taking it until it hit the page the screen
      // was built with.
      await new Promise((resolve) => setTimeout(resolve, 20));
      expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(5);
    } finally {
      measured.mockRestore();
      if (page) Object.defineProperty(window, "innerHeight", page);
    }
  });

  it("says the same thing in fewer lines on a handheld, and the same thing on a television", async () => {
    // Fitting a screen is not giving its page a row fewer. The counts and the
    // sentence under the status row are what a second line is spent on here,
    // and on a Steam Deck a second line is a cheat: the counts move onto the
    // line above and the sentence behind the question mark the row already
    // carries. Nothing is dropped, and a television keeps both where they were.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: [], unavailable: [] });
    const page = Object.getOwnPropertyDescriptor(window, "innerHeight");
    try {
      Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
      const tall = renderCheatModal({ live: false });
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));
      const onTelevision = screen.getByTestId("cheats-stored-only");
      expect(onTelevision.textContent).toContain("Apply");
      tall.unmount();

      Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
      renderCheatModal({ live: false });
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));
      const onAHandheld = screen.getByTestId("cheats-stored-only");
      expect(onAHandheld.textContent).not.toContain("Apply");
      // Still one press away, and still the same words.
      fireEvent.click(within(onAHandheld).getByRole("button", { name: "?" }));
      expect(onAHandheld.parentElement?.textContent).toContain("Apply");
    } finally {
      if (page) Object.defineProperty(window, "innerHeight", page);
    }
  });

  it("puts the two controls that narrow the list in one row where height is scarce, and a row each where it is not", async () => {
    // A row each is what Steam's own components do, and on a Steam Deck it cost
    // this screen 80 of 534 pixels for two controls holding one word between
    // them. On a television the trade is the wrong way round: a row halved is a
    // control halved, and Steam sets a dropdown's label beside its value, so
    // `All supported controls` came out as `All su...`.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: [], unavailable: [] });
    const page = Object.getOwnPropertyDescriptor(window, "innerHeight");
    try {
      Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
      const short = renderCheatModal();
      const paired = await screen.findByTestId("cheats-filters");
      expect(within(paired).getByLabelText("Section")).toBeTruthy();
      expect(within(paired).getByLabelText("Filter")).toBeTruthy();
      // Side by side they are one navigation container, so the controller
      // moves across rather than down between them.
      expect(paired.getAttribute("data-flow-children")).toBe("row");
      short.unmount();

      Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
      renderCheatModal();
      const stacked = await screen.findByTestId("cheats-filters");
      // Both still here, and neither is sharing a row with the other.
      expect(within(stacked).getByLabelText("Section")).toBeTruthy();
      expect(within(stacked).getByLabelText("Filter")).toBeTruthy();
      expect(stacked.getAttribute("data-flow-children")).toBeNull();
    } finally {
      if (page) Object.defineProperty(window, "innerHeight", page);
    }
  });

  it("keeps the tip on a screen with room for it and spends that row on a cheat otherwise", async () => {
    // The tip is read once and then costs a row of the list on every visit
    // after it. A television has the room; a handheld is choosing between the
    // tip and a cheat.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: [], unavailable: [] });
    const page = Object.getOwnPropertyDescriptor(window, "innerHeight");
    try {
      Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
      const tall = renderCheatModal();
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));
      expect(screen.getByText(/can pin it onto the CE Decky panel/)).toBeTruthy();
      tall.unmount();

      Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
      renderCheatModal();
      await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));
      expect(screen.queryByText(/can pin it onto the CE Decky panel/)).toBeNull();
    } finally {
      if (page) Object.defineProperty(window, "innerHeight", page);
    }
  });

  it("drops a refusal as soon as the reader does anything else", async () => {
    // A refusal describes one press against the state at that moment. Paging
    // away leaves it on screen about a press nobody is still making, and there
    // was no way to get rid of it short of a successful Apply.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: liveRuntime(),
      unavailable: [],
      results: manyControls.map((control) => ({
        generation: 1, record_id: control.id, ok: true, active: false, value: null, error: null,
      })),
    });
    renderCheatModal();
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    fireEvent.click(within(screen.getByTestId("cheat-row-100")).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await screen.findByTestId("cheat-apply-error");

    fireEvent.click(screen.getByRole("button", { name: "Next \u203a" }));
    await waitFor(() => expect(screen.queryByTestId("cheat-apply-error")).toBeNull());
  });

  it("keeps a closed cheat to two lines and shows the whole of it when opened", async () => {
    // A Cheat Engine record is named in the field its author also writes notes
    // in, so a real table's names run to a paragraph. Those wrapped, one record
    // could be five lines tall, and a page of six could not be read without
    // scrolling the window. Closed rows now scroll their own text under the
    // ring instead of growing; `More` is the press that says show me all of it.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: liveRuntime(), results: [] }, unavailable: [] });
    renderCheatModal();
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    const row = screen.getByTestId("cheat-row-100");
    // Both lines are clamped, and the row is marked as the one whose focus
    // drives its own reveal.
    expect(row.classList.contains("ce-decky-focusscroll")).toBe(true);
    expect(row.classList.contains("ce-decky-cheatrow")).toBe(true);
    expect(row.querySelectorAll(".ce-decky-marquee").length).toBe(2);

    fireEvent.click(within(row).getByRole("button", { name: "More" }));
    // Opened, nothing is cut: the name wraps rather than scrolling, and the
    // record's full group opens the block.
    expect(row.querySelectorAll(".ce-decky-marquee").length).toBe(0);
    expect(row.querySelectorAll(".ce-decky-wrap").length).toBeGreaterThan(0);
    expect(row.textContent).toContain("Enable 1.0 \u203a Cheat 0");
    // And the group is not repeated on the identity row at the end of it.
    expect(row.textContent).toContain("ID 100");
    expect(row.textContent?.match(/Enable 1\.0/g)).toHaveLength(1);
  });

  it("reveals the value editor as soon as a cheat that needs one is switched on", async () => {
    runtimeClient.queryRuntimeControls.mockResolvedValue({
      envelope: liveRuntime(),
      results: manyControls.map((control) => ({
        generation: 1, record_id: control.id, ok: true, active: false, value: null, error: null,
      })),
    });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{
      envelope: liveRuntime(),
      results: manyControls.map((control) => ({
        generation: 1, record_id: control.id, ok: true, active: false, value: null, error: null,
      })),
    }, unavailable: [] });
    renderCheatModal();
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(CONTROL_PAGE_SIZE));

    // A script record is complete once it is active, so nothing opens.
    const script = screen.getByTestId("cheat-row-101");
    fireEvent.click(within(script).getByTestId("toggle"));
    expect(within(script).queryByLabelText("Value")).toBeNull();

    // A value record does nothing until a value is written, so its editor
    // appears without the user having to find it behind More.
    const value = screen.getByTestId("cheat-row-100");
    expect(within(value).queryByLabelText("Value")).toBeNull();
    fireEvent.click(within(value).getByTestId("toggle"));
    expect(within(value).getByLabelText("Value")).toBeTruthy();
    // The reveal is forced for as long as the cheat is on, so the button says
    // what the row is actually showing and cannot pretend to change it. It used
    // to read "More" over an already-open row and do nothing when pressed.
    const toggleDetail = within(value).getByRole("button", { name: "Less" }) as HTMLButtonElement;
    expect(toggleDetail.disabled).toBe(true);
    fireEvent.click(toggleDetail);
    expect(within(value).getByLabelText("Value")).toBeTruthy();

    // Switching the cheat off releases it: the editor is no longer required, so
    // the row collapses and its own More works again.
    fireEvent.click(within(value).getByTestId("toggle"));
    expect(within(value).queryByLabelText("Value")).toBeNull();
    const released = within(value).getByRole("button", { name: "More" }) as HTMLButtonElement;
    expect(released.disabled).toBe(false);
    fireEvent.click(released);
    expect(within(value).getByLabelText("Value")).toBeTruthy();
    expect(within(value).getByRole("button", { name: "Less" })).toBeTruthy();
  });

  it("keeps Cheat Engine's unreadable placeholder out of the compact row", async () => {
    runtimeClient.queryRuntimeControls.mockResolvedValue({
      envelope: liveRuntime(),
      results: [{ generation: 1, record_id: 100, ok: true, active: false, value: "??", error: null }],
    });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{
      envelope: liveRuntime(),
      results: [{ generation: 1, record_id: 100, ok: true, active: false, value: "??", error: null }],
    }, unavailable: [] });
    renderCheatModal();

    const row = await screen.findByTestId("cheat-row-100");
    expect(row.textContent).toContain("Cheat 0");
    expect(row.textContent).not.toContain("??");
  });

  it("shows the breadcrumb as context and keeps the leaf name as the row label", async () => {
    runtimeClient.queryRuntimeControls.mockResolvedValue({
      envelope: liveRuntime(),
      results: [{ generation: 1, record_id: 100, ok: true, active: false, value: "42", error: null }],
    });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{
      envelope: liveRuntime(),
      results: [{ generation: 1, record_id: 100, ok: true, active: false, value: "42", error: null }],
    }, unavailable: [] });
    renderCheatModal();

    const row = await screen.findByTestId("cheat-row-100");
    expect(row.textContent).toContain("Cheat 0");
    expect(row.textContent).toContain("Enable 1.0");
    expect(row.textContent).toContain("= 42");
  });
});

describe("Launching the selected table", () => {
  function withoutAutoload() {
    const snapshot = status(true);
    snapshot.profiles[0].autoload_enabled = false;
    return snapshot;
  }

  it("names the cheats a start left on that nobody chose", async () => {
    // A script startup started brings its author's defaults, and the ones whose
    // code was not read as surviving being written off stay on beside the cheat
    // that was chosen. "Table loaded" is about the chosen ones, so these are
    // named after it rather than left for the reader to find in the game.
    const drain = {
      ...inspect.controls[0], id: 77, description: "Vitals drain", path: ["Script", "Vitals drain"],
      kind: "dropdown", dropdown_values: [["0", "Off"], ["1", "On"]], switch_on_value: "1",
      declared_default: "1", switch_off_is_safe: false,
    };
    api.inspectTableSha.mockResolvedValue({ ...inspect, controls: [...inspect.controls, drain] });
    api.startupLeftOn.mockResolvedValue({ table_sha256: SHA, record_ids: [77] });
    api.getStatus.mockResolvedValue(withoutAutoload());
    api.getRuntimeStatus.mockResolvedValueOnce(noRuntime()).mockResolvedValue(liveRuntime());
    api.launchCEForGame.mockResolvedValue({
      operation_id: "op", app_id: 10, mode: "attached", state: "connected",
      session_id: "session", message: "connected", error: null,
    });
    renderContent();

    const start = await screen.findByRole("button", { name: "Load table & start CE" });
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(start);
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("One more cheat from this table is on: Vitals drain") }),
    ), { timeout: 4000 });
    expect(api.startupLeftOn).toHaveBeenCalledWith(10);
  });

  it("does not start the table again after Stop, with Auto-load on", async () => {
    // Auto-load starts the table whenever the game runs without it, and a Stop
    // is exactly that state: without this, pressing Stop brought Cheat Engine
    // straight back, and pressing it again did the same. The Stop asks the
    // backend to hold Auto-load for this run, and Auto-load reads that hold.
    let capability: any = { ...launch, operations: [{ operation_id: "live", app_id: 10, state: "connected" }] };
    api.getCELaunchCapability.mockImplementation(async () => capability);
    api.getRuntimeStatus.mockImplementation(async () => (capability.operations.length ? liveRuntime() : noRuntime()));
    api.stopCEForGame.mockImplementation(async () => {
      capability = { ...launch, operations: [], run_holds: { dirty: null, autoload_held: true } };
      return {
        stopped: true, recovered: false,
        quiesce: { asked: true, answered: true, reason: null, cleanup_confirmed: true, records_put_down: 1, records_unsettled: [] },
      };
    });
    api.launchCEForGame.mockResolvedValue({
      operation_id: "op", app_id: 10, mode: "attached", state: "connected",
      session_id: "session", message: "connected", error: null,
    });
    renderContent();
    const stop = await screen.findByRole("button", { name: "Stop CE" });
    await waitFor(() => expect((stop as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(stop);
    await waitFor(() => expect(api.stopCEForGame).toHaveBeenCalledWith(10, null, true));
    // Long enough for the Auto-load that used to follow a Stop to have started.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 1500)); });
    expect(api.launchCEForGame).not.toHaveBeenCalled();
    // The switch still reads on, so the reader is told why nothing started.
    expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
      body: expect.stringContaining("Auto-load will not start it again until this game is restarted."),
    }));

    // The next run of the game is what Auto-load is for, once the backend has
    // proven this one over.
    capability = { ...launch, operations: [], run_holds: { dirty: null, autoload_held: false } };
    await waitFor(() => expect(api.launchCEForGame).toHaveBeenCalled(), { timeout: 8000 });
  }, 15000);

  it("does not start the table again in a game a stop left unconfirmed", async () => {
    // The table's own `[ENABLE]` run over the patches it left would miss its
    // scans, fail its startup and have that failure recorded against a table
    // that works. The stop warned; the start is what is held.
    api.getStatus.mockResolvedValue(withoutAutoload());
    let capability: any = { ...launch, operations: [{ operation_id: "live", app_id: 10, state: "connected" }] };
    api.getCELaunchCapability.mockImplementation(async () => capability);
    api.getRuntimeStatus.mockImplementation(async () => (capability.operations.length ? liveRuntime() : noRuntime()));
    api.stopCEForGame.mockImplementation(async () => {
      capability = { ...launch, operations: [], run_holds: { dirty: { since: 1, unsettled: 1 }, autoload_held: true } };
      return {
        stopped: true, recovered: false,
        quiesce: { asked: true, answered: true, reason: null, cleanup_confirmed: false, records_put_down: 1, records_unsettled: ["6"] },
      };
    });
    renderContent();
    const stop = await screen.findByRole("button", { name: "Stop CE" });
    await waitFor(() => expect((stop as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(stop);
    await waitFor(() => expect(api.stopCEForGame).toHaveBeenCalledOnce());

    const start = await screen.findByRole("button", { name: "Load table & start CE" });
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(start);
    expect(await screen.findByText(/Restart the game before starting a table in it/)).toBeTruthy();
    expect(api.launchCEForGame).not.toHaveBeenCalled();
  });

  it("starts Cheat Engine for the table this game already authorized", async () => {
    api.getStatus.mockResolvedValue(withoutAutoload());
    api.getRuntimeStatus.mockResolvedValueOnce(noRuntime()).mockResolvedValue(liveRuntime());
    api.prepareSession.mockResolvedValue({});
    api.launchCEForGame.mockResolvedValue({
      operation_id: "op", app_id: 10, mode: "attached", state: "connected",
      session_id: "session", message: "connected", error: null,
    });
    renderContent();

    const start = await screen.findByRole("button", { name: "Load table & start CE" });
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(start);

    // The backend launch transaction prepares the session; the controller must
    // not prepare a second one whose table snapshot Cheat Engine never opens.
    await waitFor(() => expect(api.launchCEForGame).toHaveBeenCalled());
    expect(api.prepareSession).not.toHaveBeenCalled();
    // The already authorized table is reused; nothing is re-imported or re-consented.
    expect(api.importTable).not.toHaveBeenCalled();
    expect(api.setExecutionConsent).not.toHaveBeenCalled();
  });

  it("shows the compatibility a manual start proved, without waiting for another action", async () => {
    // Startup applies one record at a time and can settle seconds after the
    // bridge answers, and the backend persists positive proof only when a
    // status read observes startup terminal. A manual start that stopped at the
    // first live read left that proof unconsumed, and where it did land, Search
    // and Manage kept showing the snapshot from before it.
    const evidence = {
      app_id: 10, table_sha256: SHA, target_process: "game.exe", pe_version: "1",
      steam_build_id: null, last_working_at: 1, invalidated: false, state: "matching",
    };
    let proved = false;
    const startingUp = (state: string) => {
      const runtime = liveRuntime();
      (runtime.status as any).startup_state = state;
      (runtime.status as any).startup_completed = state === "applied" ? 1 : 0;
      (runtime.status as any).startup_total = 1;
      return runtime;
    };
    api.getStatus.mockImplementation(async () => ({
      ...withoutAutoload(),
      table_compatibility: { schema: 2, reason: null, entries: proved ? [evidence] : [] },
    }));
    let reads = 0;
    api.getRuntimeStatus.mockImplementation(async () => {
      reads += 1;
      if (reads === 1) return noRuntime();
      if (reads === 2) return startingUp("pending");
      // The read that observes startup terminal is the one that lets the
      // backend persist the proof.
      proved = true;
      return startingUp("applied");
    });
    api.launchCEForGame.mockResolvedValue({
      operation_id: "op", app_id: 10, mode: "attached", state: "connected",
      session_id: "session", message: "connected", error: null,
    });
    renderContent();

    const start = await screen.findByRole("button", { name: "Load table & start CE" });
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(start);
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: "Table loaded and Cheat Engine connected." }),
    ), { timeout: 4000 });

    for (const name of ["Search", "Manage"]) {
      fireEvent.click(await screen.findByRole("button", { name, exact: true }));
      expect(modalState.nodes.at(-1).props.compatibility).toEqual([evidence]);
      await act(async () => {
        const props = modalState.nodes.at(-1).props;
        (props.onCancel ?? props.onClose)();
      });
    }
  });

  it("does not report a table Cheat Engine never opened as loaded", async () => {
    // The bridge attaches and reports itself ready whether or not it got the
    // table in, so a connected session proves nothing about the table this
    // press is about. Home tells the user to stop it and start it again, which
    // is this very button, so saying "Table loaded and Cheat Engine connected"
    // here is the answer they would act on.
    const failed = () => {
      const runtime = liveRuntime();
      runtime.status.table_load_state = "failed";
      runtime.status.table_load_route = "prompted";
      runtime.status.table_load_error = "Cheat Engine refused to open the table";
      return runtime;
    };
    api.getStatus.mockResolvedValue(withoutAutoload());
    api.getRuntimeStatus.mockResolvedValueOnce(noRuntime()).mockResolvedValue(failed());
    api.launchCEForGame.mockResolvedValue({
      operation_id: "op", app_id: 10, mode: "attached", state: "connected",
      session_id: "session", message: "connected", error: null,
    });
    renderContent();

    const start = await screen.findByRole("button", { name: "Load table & start CE" });
    await waitFor(() => expect((start as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(start);

    await waitFor(() => expect(api.launchCEForGame).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId("runtime-row").textContent).toContain("Cheat Engine refused to open the table"));
    expect(decky.toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ body: "Table loaded and Cheat Engine connected." }),
    );
  });

  it("follows the game that actually started instead of a manual preparation choice", async () => {
    // Choosing a game with nothing running is a preparation screen. It must not
    // survive a different game starting, or Home keeps showing, searching and
    // configuring the game the user is not playing.
    const other = { appId: 11, name: "Other", sortAs: "Other", isShortcut: false };
    let running: any[] = [];
    steam.listRunningGames.mockImplementation(async () => ({ available: true, games: running }));
    steam.listInstalledGames.mockResolvedValue([game, other]);
    steam.readAppDetails.mockImplementation(async (appId: number) => ({
      ...details, appId, displayName: appId === 11 ? "Other" : "Game",
    }));
    renderContent();

    await waitFor(() => expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    await act(async () => { await modalState.nodes[0].props.onPick(game); });
    await waitFor(() => expect(screen.getByTestId("game-row").textContent).toContain("Game"));

    // A different game starts. The manual pick was a preparation choice, not an
    // override for the game now running.
    running = [other];
    await waitFor(() => expect(screen.getByTestId("game-row").textContent).toContain("Other"), { timeout: 6000 });
  }, 10000);

  it("blocks auto-load and names the repair when the selected table file is gone", async () => {
    // Start already required the exact table to be present. Auto-load did not,
    // so it stayed armed and every attempt failed in session preparation.
    const snapshot = status(true);
    snapshot.tables = [];
    api.getStatus.mockResolvedValue(snapshot);
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    renderContent();

    expect(await screen.findByText(/table file is missing/)).toBeTruthy();
    await waitFor(() => expect(api.launchCEForGame).not.toHaveBeenCalled());
  });

  it("names the game that still owns Cheat Engine instead of only disabling setup", async () => {
    api.getCELaunchCapability.mockResolvedValue({
      ...launch,
      owned_launch_owners: [{ app_id: 11, state: "connected", recovered: false }],
    });
    steam.listInstalledGames.mockResolvedValue([game, { appId: 11, name: "Other", sortAs: "Other", isShortcut: false }]);
    renderContent();

    const row = await screen.findByTestId("ce-owned-elsewhere");
    // The library entry's name is used once it is loaded; until then the exact
    // AppID is still a next step, which a disabled button was not.
    expect(row.textContent).toMatch(/Other|AppID 11/);
    expect(row.textContent).toContain("stop it first");
  });

  it("keeps the workflow usable when the optional managed setup status cannot be read", async () => {
    // Managed setup is optional: the backend keeps the import fallback alive
    // when it cannot offer a download. A null capability used to block game
    // selection, search, Advanced and the runtime actions of an already valid
    // imported Cheat Engine for the lifetime of the panel.
    api.getManagedCECapability.mockRejectedValueOnce(new Error("managed release manifest is unreadable"));
    renderContent();

    expect(await screen.findByText("Setup status unavailable")).toBeTruthy();
    const change = await screen.findByRole("button", { name: "Change" });
    // Change is refused here for a reason of its own: this profile's game is
    // running. What this case is about is everything the unreadable capability
    // must not block.
    await waitFor(() => expect((change as HTMLButtonElement).disabled).toBe(true));
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Advanced…" }) as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByText("Setup status unavailable")).toBeNull());
  });

  it("retries auto-load after a transient startup failure instead of latching it for the session", async () => {
    // A failed Auto-load used to store its attempt key and never clear it, so a
    // game whose target process appeared a moment late never auto-loaded again
    // until the panel was remounted.
    vi.useFakeTimers();
    try {
      api.getRuntimeStatus.mockResolvedValue(noRuntime());
      api.prepareSession.mockResolvedValue({});
      api.launchCEForGame
        .mockRejectedValueOnce(new Error("the game process is not observable yet"))
        .mockResolvedValue({
          operation_id: "op", app_id: 10, mode: "attached", state: "connected",
          session_id: "session", message: "connected", error: null,
        });
      renderContent();

      await vi.waitFor(() => expect(api.launchCEForGame).toHaveBeenCalledTimes(1));
      await vi.advanceTimersByTimeAsync(5000);
      await vi.waitFor(() => expect(api.launchCEForGame).toHaveBeenCalledTimes(2));
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives up on auto-load after a bounded number of retries rather than looping", async () => {
    vi.useFakeTimers();
    try {
      api.getRuntimeStatus.mockResolvedValue(noRuntime());
      api.prepareSession.mockResolvedValue({});
      api.launchCEForGame.mockRejectedValue(new Error("the game process is not observable yet"));
      renderContent();

      await vi.waitFor(() => expect(api.launchCEForGame).toHaveBeenCalledTimes(1));
      for (const delay of [4000, 10000, 25000]) {
        await vi.advanceTimersByTimeAsync(delay + 1000);
      }
      await vi.advanceTimersByTimeAsync(120000);
      expect(api.launchCEForGame.mock.calls.length).toBeLessThanOrEqual(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it("names the exact blocker instead of only disabling the start action", async () => {
    const snapshot = withoutAutoload();
    snapshot.profiles[0].execution_consent_sha256 = null;
    api.getStatus.mockResolvedValue(snapshot);
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    renderContent();

    const start = await screen.findByRole("button", { name: "Load table & start CE" });
    expect((start as HTMLButtonElement).disabled).toBe(true);
    expect(await screen.findByText(/not authorized yet/)).toBeTruthy();
    expect(api.launchCEForGame).not.toHaveBeenCalled();
  });
});

describe("Pinned live controls", () => {
  function pinnedStatus() {
    const snapshot = status(true);
    snapshot.profiles[0].pinned = [7];
    return snapshot;
  }

  it("records a refused enable from the Home pinned toggle, not only from Apply", async () => {
    // The panel's own toggle is `applyRuntimeSelection` too, so it can produce
    // the refusal; wiring only the picker left it able to produce one and
    // unable to record it.
    api.getStatus.mockResolvedValue(pinnedStatus());
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] });
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    fireEvent.click(within(row).getByTestId("toggle"));

    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });
    expect(confirm.props.strDescription).toContain("“Health” did not switch on");
  });

  it("shows a stopped table gone from a panel that was not there when it was stopped", async () => {
    // The answer closes Configure cheats, which takes the quick access panel
    // with it, and then makes five durable writes - the last of them clearing
    // the selection. Steam builds a new panel when the user looks again, and
    // that panel reads the authority once on its way up, which is a read racing
    // a chain of writes it knows nothing about. It won: the table the user had
    // just stopped using was still on the panel with its Load and Configure
    // rows. The answer says when it has finished instead.
    api.getStatus.mockResolvedValue(pinnedStatus());
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] });
    api.blockTable.mockResolvedValue({});
    api.setExecutionConsent.mockResolvedValue({});
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    const asking = renderContent();

    fireEvent.click(within(await screen.findByTestId("pinned-cheat-7")).getByTestId("toggle"));
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });

    // The panel that asked is gone, and a new one is already showing the table -
    // and a live read that failed while it was hydrating.
    asking.unmount();
    runtimeClient.queryRuntimeControlsPartial.mockRejectedValue(new Error("Resident bridge is disconnected"));
    const watching = renderContent();
    expect(await watching.findByText("Game.CT")).toBeTruthy();
    expect(await watching.findByText(/Live cheat state could not be refreshed/)).toBeTruthy();

    // Every read from here answers with the table cleared, which is what the
    // writes below are doing while the new panel is not looking.
    const cleared: any = status();
    cleared.profiles[0].table_sha256 = null;
    cleared.profiles[0].execution_consent_sha256 = null;
    cleared.tables = [];
    api.getStatus.mockResolvedValue(cleared);

    await act(async () => { confirm.props.onOK(); });

    await waitFor(() => expect(watching.queryByText("Game.CT")).toBeNull());
    // And the read failure from the session that table had goes with it: it is
    // about the live state of a selected table, so with no table it cannot be
    // true of anything, and nothing re-hydrates a game whose table was taken
    // away under it.
    expect(watching.queryByText("Attention")).toBeNull();
    expect(watching.queryByText(/Live cheat state could not be refreshed/)).toBeNull();
  });

  it("asks even when closing the picker took the panel with it", async () => {
    // The question is a window this panel opens after closing another one, and
    // closing that one is itself a way for the panel to go. Dropping the
    // question when the panel has gone therefore dropped it into the gap it had
    // just made: the notification arrived, the window did not, and the table
    // went on applying. The window is Steam's and outlives the panel.
    api.getStatus.mockResolvedValue(pinnedStatus());
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    // Held open, so the operation that raised the question still owns the
    // panel's latch and the question is still waiting on it when the panel goes.
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] })
      .mockReturnValue(new Promise(() => undefined));
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    const view = renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    fireEvent.click(within(row).getByTestId("toggle"));
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
      body: "This table did not work.",
    })));
    expect(modalState.nodes.find((item: any) => item.type === DeckyConfirmModal)).toBeUndefined();

    // The panel goes while the question is still waiting, which is exactly what
    // closing the picker does on the device.
    await act(async () => { view.unmount(); });

    const confirm = modalState.nodes.find((item: any) => item.type === DeckyConfirmModal);
    expect(confirm?.props?.strTitle).toBe("This table did not work");
  });

  it("asks even when the latch it waits for is never released", async () => {
    // The wait is right: two callers raise this from inside their own action,
    // which owns the panel's latch until that action returns, and an answer
    // given while it is held is refused. Waiting forever is not: the question
    // is the only thing that gets a refused table out of the way, so a latch
    // that never comes free must not take it with it. The table went on
    // applying, and nothing anywhere said why.
    api.getStatus.mockResolvedValue(pinnedStatus());
    api.setExecutionConsent.mockResolvedValue({});
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    // The reconciliation the catch runs after the refusal, never answered, so
    // the operation that started it owns the latch for good.
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] })
      .mockReturnValue(new Promise(() => undefined));
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    vi.useFakeTimers();
    try {
      renderContent();
      const row = await vi.waitFor(() => screen.getByTestId("pinned-cheat-7"));
      fireEvent.click(within(row).getByTestId("toggle"));
      await vi.waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
        body: "This table did not work.",
      })));
      expect(modalState.nodes.find((item: any) => item.type === DeckyConfirmModal)).toBeUndefined();

      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      const confirm = modalState.nodes.find((item: any) => item.type === DeckyConfirmModal);
      expect(confirm?.props?.strTitle).toBe("This table did not work");
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not accept Stop using it and then drop it on the latch it waited for", async () => {
    // The wait above gives up on a latch that never comes free, so the question
    // is put while an operation still holds it. The answer then makes five
    // durable writes, and `runAction` refuses on the spot while that latch is
    // held: the dialog had already closed itself, so the press was accepted,
    // discarded, and reported as an operation the user never started, over a
    // table that went on applying.
    api.getStatus.mockResolvedValue(pinnedStatus());
    api.setExecutionConsent.mockResolvedValue({});
    api.blockTable.mockResolvedValue({});
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] })
      .mockReturnValue(new Promise(() => undefined));
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    vi.useFakeTimers();
    try {
      renderContent();
      const row = await vi.waitFor(() => screen.getByTestId("pinned-cheat-7"));
      fireEvent.click(within(row).getByTestId("toggle"));
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      const confirm = modalState.nodes.find((item: any) => item.type === DeckyConfirmModal);
      expect(confirm?.props?.strTitle).toBe("This table did not work");

      decky.toast.mockClear();
      await act(async () => { confirm.props.onOK(); });
      // Still held, so nothing durable may run yet, and nothing may claim it did.
      await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
      expect(api.blockTable).not.toHaveBeenCalled();
      // What the user is told is about their own table and what to do about it,
      // not about an operation they did not start.
      const bodies = decky.toast.mock.calls.map((call: any) => call[0]?.body);
      expect(bodies.some((body: string) => /still busy(.|\n)*not stopped/.test(body ?? ""))).toBe(true);
      expect(bodies).not.toContain("Another CE Decky operation is still running.");
    } finally {
      vi.useRealTimers();
    }
  });

  it("waits for a busy latch and then stops using the table", async () => {
    resetSupportLog();
    // The other half: the operation holding the latch finishes shortly after
    // the press, and the answer the user gave is the one that happens.
    api.getStatus.mockResolvedValue(pinnedStatus());
    api.setExecutionConsent.mockResolvedValue({});
    api.blockTable.mockResolvedValue({});
    api.listBlockedTables.mockResolvedValue({ tables: [], reason: null });
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    let releaseReconcile: (value: any) => void = () => undefined;
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] })
      .mockReturnValue(new Promise((resolve) => { releaseReconcile = resolve; }));
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    vi.useFakeTimers();
    try {
      renderContent();
      const row = await vi.waitFor(() => screen.getByTestId("pinned-cheat-7"));
      fireEvent.click(within(row).getByTestId("toggle"));
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      const confirm = modalState.nodes.find((item: any) => item.type === DeckyConfirmModal);
      expect(confirm?.props?.strTitle).toBe("This table did not work");

      await act(async () => { confirm.props.onOK(); });
      expect(api.blockTable).not.toHaveBeenCalled();
      const press = readSupportLog().entries.find((entry) => entry.event === "ui.action" && entry.fields.action === "panel.table_refusal.stop");
      expect(press?.fields.interaction).toBeTruthy();
      // The operation that owned the latch finishes, within the answer's wait.
      await act(async () => {
        releaseReconcile({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] });
        await vi.advanceTimersByTimeAsync(100);
      });
      await vi.waitFor(() => expect(api.blockTable).toHaveBeenCalled());
      await vi.waitFor(() => expect(api.revokeTable).toHaveBeenCalledWith(10, SHA));
      await vi.waitFor(() => {
        const events = readSupportLog().entries.filter((entry) => entry.fields.interaction === press?.fields.interaction && entry.event.startsWith("panel.action_"));
        expect(events.map((entry) => entry.event)).toEqual(["panel.action_started", "panel.action_completed"]);
        expect(events.every((entry) => entry.fields.action === "panel.table_refusal.stop")).toBe(true);
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps a deletion that happened when the read after it fails", async () => {
    // The read is reconciliation. A deletion that succeeded is committed
    // whatever the read does, and reporting it as failed would leave the row
    // saying the press did not work while the retry meets a table that is no
    // longer there.
    api.getStatus.mockResolvedValue(status());
    api.deleteTable.mockResolvedValue({ sha256: SHA, size: 100 });
    renderContent();
    await screen.findByText("Game.CT");

    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    const picker = [...modalState.nodes].reverse().find((node: any) => node?.props?.onDelete);
    expect(picker).toBeTruthy();

    // The delete lands; the read after it does not.
    api.getStatus.mockRejectedValueOnce(new Error("the backend did not answer"));
    await act(async () => { await picker.props.onDelete(SHA); });

    expect(api.deleteTable).toHaveBeenCalledWith(SHA);
    // No failure was reported for a press that worked.
    const bodies = decky.toast.mock.calls.map((call: any) => call[0]?.body ?? "");
    expect(bodies.some((body: string) => /did not answer/.test(body))).toBe(false);
  });

  it("does not hold a committed deletion behind the read that follows it", async () => {
    // The read is reconciliation. Awaiting it left the row on screen, the
    // window's latch held and the way out blocked, for a table the backend had
    // already destroyed, for as long as an unrelated read took to answer.
    api.getStatus.mockResolvedValue(status());
    api.deleteTable.mockResolvedValue({ sha256: SHA, size: 100 });
    renderContent();
    await screen.findByText("Game.CT");

    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    const picker = [...modalState.nodes].reverse().find((node: any) => node?.props?.onDelete);
    expect(picker).toBeTruthy();

    // A read that never answers at all.
    api.getStatus.mockReturnValueOnce(new Promise(() => undefined));
    let settled = false;
    await act(async () => {
      void picker.props.onDelete(SHA).then(() => { settled = true; });
    });
    expect(settled).toBe(true);
    expect(api.deleteTable).toHaveBeenCalledWith(SHA);
  });

  it("asks again about a table whose question could not be put the first time", async () => {
    // The guard that keeps one question to one table used to be taken when it
    // was decided to ask, several awaits before the window opens. Anything that
    // stopped the wait in between left the table recorded as asked about with
    // nothing having been asked, and every later refusal returned at that
    // guard: a table that refused every cheat could be applied for as long as
    // the user cared to press, and the one question that would have retired it
    // was never put again.
    api.getStatus.mockResolvedValue(pinnedStatus());
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] });
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    renderContent();

    const toggle = async () => {
      const row = await screen.findByTestId("pinned-cheat-7");
      fireEvent.click(within(row).getByTestId("toggle"));
    };

    // The first question cannot be put at all.
    modalState.failTitle = "This table did not work";
    await toggle();
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
      body: "This table did not work.",
    })));
    expect(modalState.nodes.find((item: any) => item.type === DeckyConfirmModal)).toBeUndefined();

    // The next press is asked, because nothing was ever asked before it.
    await toggle();
    const confirm = await waitFor(() => {
      const found = modalState.nodes.find((node: any) => node.type === DeckyConfirmModal);
      expect(found).toBeTruthy();
      return found;
    });
    expect(confirm.props.strTitle).toBe("This table did not work");
  });

  it("waits for the latch its own toggle holds before asking what to do", async () => {
    // The dialog is raised from inside the toggle's `runAction`, which owns the
    // global latch until that call returns - and the reconciliation after the
    // refusal is several round trips. Asking while the latch was held closed
    // the dialog and had the answer rejected as "another operation is still
    // running": nothing happened, and nothing said so.
    api.getStatus.mockResolvedValue(pinnedStatus());
    api.setExecutionConsent.mockResolvedValue({});
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    // The reconciliation the catch runs after the refusal, held open so the
    // originating operation still owns the latch.
    let releaseReconcile!: (value: any) => void;
    const reconciling = new Promise((resolve) => { releaseReconcile = resolve; });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] })
      .mockReturnValue(reconciling);
    const refusal: any = new Error("“Health” did not switch on: Cheat Engine ran it and it went straight back off.");
    refusal.tableRefused = true;
    runtimeClient.applyRuntimeSelection.mockRejectedValue(refusal);
    renderContent();

    fireEvent.click(within(await screen.findByTestId("pinned-cheat-7")).getByTestId("toggle"));
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(expect.objectContaining({
      body: "This table did not work.",
    })));

    // Reported already, but not yet asked: the latch is still held.
    expect(modalState.nodes.find((item: any) => item.type === DeckyConfirmModal)).toBeUndefined();

    // Long enough that any deadline worth picking would have fired: the wait is
    // on the latch being released, not on a clock.
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 250)); });
    expect(modalState.nodes.find((item: any) => item.type === DeckyConfirmModal)).toBeUndefined();

    await act(async () => {
      releaseReconcile({ envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] });
      await Promise.resolve();
    });

    const dialog = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === DeckyConfirmModal);
      expect(node).toBeTruthy();
      return node;
    });
    dialog.props.onOK();
    await waitFor(() => expect(api.revokeTable).toHaveBeenCalledWith(10, SHA));
  });

  it("controls a pinned cheat from home and persists the confirmed state", async () => {
    const confirmed = { generation: 2, record_id: 7, ok: true, active: false, value: "100", error: null };
    api.getStatus.mockResolvedValue(pinnedStatus());
    api.setRememberedCheats.mockResolvedValue({});
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results: [confirmed] });
    runtimeClient.queryRuntimeControls
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: liveRuntime().status.results })
      .mockResolvedValue({ envelope: liveRuntime(), results: [confirmed] });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: liveRuntime().status.results , unavailable: [] })
      .mockResolvedValue({ envelope: liveRuntime(), results: [confirmed] , unavailable: [] });
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    expect(row.textContent).toContain("Health");
    fireEvent.click(within(row).getByTestId("toggle"));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledWith(10, [
      { record_id: 7, active: false, value: null, path: ["Health"], label: "Health" },
    ]));
    await waitFor(() => expect(api.setRememberedCheats).toHaveBeenCalledWith(10, SHA, [
      { record_id: 7, active: false, value: "100" },
    ]));
  });

  it("keeps pinned rows visible after a rejected runtime mutation", async () => {
    const live = liveRuntime();
    api.getStatus.mockResolvedValue(pinnedStatus());
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.applyRuntimeSelection.mockRejectedValue(new Error("activation did not settle"));
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({
      envelope: live,
      results: live.status.results,
      unavailable: [],
    });
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    fireEvent.click(within(row).getByTestId("toggle"));

    expect(await screen.findByText("activation did not settle")).toBeTruthy();
    expect(screen.getByTestId("pinned-cheat-7")).toBeTruthy();
  });

  it("refuses a pinned value cheat that has no value to switch on with", async () => {
    // Pin commits on its own press, so Cancel closes the picker without ever
    // reaching the Apply that refuses a value control with nothing in it. The
    // panel has nowhere to type one, so the switch could only ever have sent an
    // activation with no value, which freezes whatever the game held at that
    // instant.
    const snapshot = pinnedStatus();
    snapshot.profiles[0].remembered = [];
    const blank = liveRuntime();
    blank.status.results = [{ generation: 1, record_id: 7, ok: true, active: false, value: null, error: null }];
    api.getStatus.mockResolvedValue(snapshot);
    api.getRuntimeStatus.mockResolvedValue(blank);
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: blank, results: blank.status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: blank, results: blank.status.results, unavailable: [] });
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    fireEvent.click(within(row).getByTestId("toggle"));

    expect(await screen.findByText(/Health needs a value before it can be switched on/)).toBeTruthy();
    expect(runtimeClient.applyRuntimeSelection).not.toHaveBeenCalled();
  });

  it("writes the value the row shows, rather than one nothing displayed", async () => {
    // Three resolutions of one number - the row's, the refusal's and the
    // command's - reading three different sources. A row showing what Cheat
    // Engine holds while the press sent what Configure stored is a switch that
    // does not mean what the row above it says.
    const snapshot = pinnedStatus();
    snapshot.profiles[0].remembered = [];
    (snapshot.profiles[0] as any).configured_values = [{ record_id: 7, value: "100" }];
    const live = liveRuntime();
    live.status.results = [{ generation: 1, record_id: 7, ok: true, active: false, value: "40", error: null }];
    api.getStatus.mockResolvedValue(snapshot);
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: live, results: live.status.results, unavailable: [] });
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    expect(row.textContent).toContain("= 40");
    fireEvent.click(within(row).getByTestId("toggle"));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledWith(10, [
      { record_id: 7, active: true, value: "40", path: ["Health"], label: "Health" },
    ]));
  });

  it("switches a pinned value cheat on with the value it was proven to have", async () => {
    // Cheat Engine cannot read this record yet, so the row shows the choice
    // this table confirmed. The press used to send nothing at all, which
    // enables the record on whatever the game happens to hold - the exact state
    // the missing-value refusal exists to prevent, reached through the one
    // screen with nowhere to type a number.
    const snapshot = pinnedStatus();
    const live = liveRuntime();
    live.status.results = [{ generation: 1, record_id: 7, ok: true, active: false, value: null, error: null }];
    api.getStatus.mockResolvedValue(snapshot);
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results: live.status.results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: live, results: live.status.results, unavailable: [] });
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    expect(row.textContent).toContain("= 100");
    fireEvent.click(within(row).getByTestId("toggle"));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledWith(10, [
      { record_id: 7, active: true, value: "100", path: ["Health"], label: "Health" },
    ]));
  });

  it("switches a pinned switch off at its own off key, not by releasing it", async () => {
    // Releasing a frozen record leaves it at the value it was frozen at, so a
    // pinned cheat switched off here read as off on the panel and went on
    // running in the game. Configure cheats has carried both keys all along.
    const binary = {
      ...inspect,
      controls: [{
        id: 7, description: "Godmode", path: ["Godmode"], variable_type: "4 Bytes", kind: "dropdown",
        group_header: false, has_assembler_script: false,
        dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], dropdown_read_only: true,
        switch_on_value: "1",
      }],
    };
    const live = liveRuntime();
    live.status.results = [{ generation: 1, record_id: 7, ok: true, active: true, value: "1", error: null }];
    const confirmed = [{ generation: 2, record_id: 7, ok: true, active: false, value: "0", error: null }];
    api.getStatus.mockResolvedValue(pinnedStatus());
    api.inspectTableSha.mockResolvedValue(binary);
    api.getRuntimeStatus.mockResolvedValue(live);
    api.setRememberedCheats.mockResolvedValue({});
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: confirmed });
    runtimeClient.queryRuntimeControls
      .mockResolvedValueOnce({ envelope: live, results: live.status.results })
      .mockResolvedValue({ envelope: live, results: confirmed });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: live, results: live.status.results, unavailable: [] })
      .mockResolvedValue({ envelope: live, results: confirmed, unavailable: [] });
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-7");
    fireEvent.click(within(row).getByTestId("toggle"));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledWith(10, [
      {
        record_id: 7, active: false, value: null, switch_values: { on: "1", off: "0" },
        path: ["Godmode"], label: "Godmode",
      },
    ]));
  });

  it("refuses to control a record this exact table did not pin", async () => {
    api.getStatus.mockResolvedValue(status(true));
    renderContent();
    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(screen.queryByTestId("pinned-cheat-7")).toBeNull();
    expect(runtimeClient.applyRuntimeSelection).not.toHaveBeenCalled();
  });

  it("switches on enclosing scripts before a pinned child cheat", async () => {
    const nested = {
      ...inspect,
      total_entries: 2,
      controls: [
        { id: 20, description: "Party Damage Reduction", path: ["Party Damage Reduction"], variable_type: "Auto Assembler Script", kind: "script", group_header: false, has_assembler_script: true, dropdown_values: [], dropdown_read_only: false },
        { id: 21, description: "Reduction %", path: ["Party Damage Reduction", "Reduction %"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false, dropdown_values: [], dropdown_read_only: false },
      ],
    };
    const initial = [
      { generation: 1, record_id: 20, ok: true, active: false, value: null, error: null },
      { generation: 2, record_id: 21, ok: true, active: false, value: "40", error: null },
    ];
    const confirmed = initial.map((result) => ({ ...result, active: true }));
    const snapshot = pinnedStatus();
    snapshot.profiles[0].pinned = [21];
    snapshot.profiles[0].remembered = [];
    api.getStatus.mockResolvedValue(snapshot);
    api.inspectTableSha.mockResolvedValue(nested);
    const live = liveRuntime();
    live.status.results = initial;
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.queryRuntimeControls
      .mockResolvedValueOnce({ envelope: live, results: initial })
      .mockResolvedValue({ envelope: live, results: confirmed });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: live, results: initial , unavailable: [] })
      .mockResolvedValue({ envelope: live, results: confirmed , unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: live, results: confirmed });
    api.setRememberedCheats.mockResolvedValue({});
    renderContent();

    const row = await screen.findByTestId("pinned-cheat-21");
    fireEvent.click(within(row).getByTestId("toggle"));

    // The child carries the value its row shows rather than nothing: a switch
    // states the value it is turning on with, and where that value is already
    // what Cheat Engine holds, `applyRuntimeSelection` sends no `set_value` for
    // it anyway. The script above it takes no value, because it has none.
    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledWith(10, [
      { record_id: 20, active: true, value: null, path: ["Party Damage Reduction"], label: "Party Damage Reduction" },
      { record_id: 21, active: true, value: "40", path: ["Party Damage Reduction", "Reduction %"], label: "Reduction %" },
    ]));
  });

  it("disables nested children before the scripts that create them", async () => {
    const nested = {
      ...inspect,
      total_entries: 2,
      controls: [
        { id: 20, description: "Parent", path: ["Parent"], variable_type: "Auto Assembler Script", kind: "script", group_header: false, has_assembler_script: true, dropdown_values: [], dropdown_read_only: false },
        { id: 21, description: "Child", path: ["Parent", "Child"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false, dropdown_values: [], dropdown_read_only: false },
      ],
    };
    const results = [
      { generation: 1, record_id: 20, ok: true, active: true, value: null, error: null },
      { generation: 2, record_id: 21, ok: true, active: true, value: "40", error: null },
    ];
    api.inspectTableSha.mockResolvedValue(nested);
    const live = liveRuntime();
    live.status.results = results;
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: live, results }, unavailable: [] });
    runtimeClient.deactivateAllActiveControls.mockResolvedValue({ queried: 2, active: 2, deactivated: 2, deactivatedIds: [21, 20], envelope: live });
    api.setRememberedCheats.mockResolvedValue({});
    renderContent();

    // Two records are on and one of them is the script that creates the other,
    // so the panel names the cheat count its own switches match and says where
    // the second came from rather than adding it in.
    await screen.findByText("1 active (1 script)");
    const disable = await screen.findByRole("button", { name: "Disable all" });
    fireEvent.click(disable);

    await waitFor(() => expect(runtimeClient.deactivateAllActiveControls).toHaveBeenCalledWith(10, [21, 20], new Map()));
  });

  it("remembers only the cheats Disable all actually switched off", async () => {
    // Marking the whole table as touched wrote an explicit "off" for records
    // that were never on, for the enclosing script that is CE Decky's own
    // machinery, and for children that ceased to exist when it went off - and
    // the next Auto-load then waited for MemoryRecords that could not appear.
    const nested = {
      ...inspect,
      total_entries: 3,
      controls: [
        { id: 20, description: "Parent", path: ["Parent"], variable_type: "Auto Assembler Script", kind: "script", group_header: false, has_assembler_script: true, dropdown_values: [], dropdown_read_only: false },
        { id: 21, description: "Child", path: ["Parent", "Child"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false, dropdown_values: [], dropdown_read_only: false },
        { id: 22, description: "Other", path: ["Parent", "Other"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false, dropdown_values: [], dropdown_read_only: false },
      ],
    };
    const results = [
      { generation: 1, record_id: 20, ok: true, active: true, value: null, error: null },
      { generation: 2, record_id: 21, ok: true, active: true, value: "40", error: null },
      { generation: 3, record_id: 22, ok: true, active: false, value: "7", error: null },
    ];
    api.inspectTableSha.mockResolvedValue(nested);
    const live = liveRuntime();
    live.status.results = results;
    api.getRuntimeStatus.mockResolvedValue(live);
    const after = [
      { generation: 4, record_id: 20, ok: true, active: false, value: null, error: null },
      { generation: 5, record_id: 22, ok: true, active: false, value: "7", error: null },
    ];
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: live, results });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: live, results, unavailable: [] })
      .mockResolvedValue({ envelope: live, results: after, unavailable: [21] });
    // Only the child and its script were active, so only they were switched off.
    runtimeClient.deactivateAllActiveControls.mockResolvedValue({
      queried: 3, active: 2, deactivated: 2, deactivatedIds: [21, 20], envelope: live,
    });
    api.setRememberedCheats.mockResolvedValue({});
    renderContent();

    await screen.findByText("1 active (1 script)");
    fireEvent.click(await screen.findByRole("button", { name: "Disable all" }));

    await waitFor(() => expect(api.setRememberedCheats).toHaveBeenCalled());
    const [, , remembered] = api.setRememberedCheats.mock.calls[0];
    const ids = remembered.map((item: any) => item.record_id);
    expect(ids).not.toContain(20);
    expect(ids).not.toContain(22);
    expect(remembered.find((item: any) => item.record_id === 21)).toEqual({ record_id: 21, active: false, value: null });
  });

  it("says the game may still be changed when a failed Apply already mutated it", async () => {
    // The low-level helper does not roll back commands it already had
    // acknowledged, so the form used to keep showing the user's intent while
    // the game held something else - and Discard then reset the form without
    // restoring anything.
    const before = { generation: 1, record_id: 7, ok: true, active: false, value: "100", error: null };
    const after = { generation: 2, record_id: 7, ok: true, active: true, value: "100", error: null };
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [before], unavailable: [] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [after], unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockRejectedValueOnce(new Error("MemoryRecord 7 did not acknowledge"));
    const onApplied = vi.fn().mockResolvedValue(undefined);

    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      autoloadEnabled={false}
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    const row = screen.getByTestId("cheat-row-7");
    const toggle = within(row).getByTestId("toggle") as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(await screen.findByText(/Partly applied/)).toBeTruthy();
    // The row now reports what Cheat Engine actually holds, not the intent.
    await waitFor(() => expect((within(screen.getByTestId("cheat-row-7")).getByTestId("toggle") as HTMLInputElement).checked).toBe(true));
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("discloses a configured value already saved when the runtime half changed nothing", async () => {
    // Configured values are committed before any runtime write on purpose, so a
    // runtime Apply that fails without touching the game still leaves durable
    // state behind - and reconciliation, which only looks at the live delta,
    // saw nothing and let Discard look ordinary.
    const before = { generation: 1, record_id: 7, ok: true, active: false, value: "100", error: null };
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [before], unavailable: [] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [before], unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockRejectedValueOnce(new Error("bridge went away"));
    const onSaveConfiguredValues = vi.fn().mockResolvedValue(undefined);

    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      autoloadEnabled={false}
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={onSaveConfiguredValues}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onCancel={vi.fn()}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(await screen.findByLabelText("Value"), { target: { value: "250" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onSaveConfiguredValues).toHaveBeenCalled());
    expect(await screen.findByText(/saved configuration was written/)).toBeTruthy();
  });

  it("names both the game and the saved configuration when one Apply changed both", async () => {
    // The two are independent: an Apply can commit this table's configuration
    // and then change Cheat Engine before a later step fails. Describing only
    // the live half left a configured value that survives into the next
    // Auto-load with no disclosure at all.
    const before = { generation: 1, record_id: 7, ok: true, active: false, value: "100", error: null };
    const after = { generation: 2, record_id: 7, ok: true, active: true, value: "250", error: null };
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [before], unavailable: [] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [after], unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results: [after] });
    const onSaveConfiguredValues = vi.fn().mockResolvedValue(undefined);
    // The durable half of the transaction is what fails, after the game has
    // already been changed.
    const onApplied = vi.fn().mockRejectedValue(new Error("profile write rejected"));

    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      autoloadEnabled={false}
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={onSaveConfiguredValues}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(await screen.findByLabelText("Value"), { target: { value: "250" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onApplied).toHaveBeenCalled());
    const partial = await screen.findByText(/Closing does not undo any of it/);
    expect(partial.textContent).toMatch(/commands were already accepted/);
    expect(partial.textContent).toMatch(/saved configuration was written/);
  });

  it("asks before closing over a partial Apply even with nothing left staged", async () => {
    const before = { generation: 1, record_id: 7, ok: true, active: false, value: "100", error: null };
    const after = { generation: 2, record_id: 7, ok: true, active: true, value: "100", error: null };
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [before], unavailable: [] })
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [after], unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockRejectedValueOnce(new Error("MemoryRecord 7 did not acknowledge"));
    const onCancel = vi.fn();

    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      autoloadEnabled={false}
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onCancel={onCancel}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    fireEvent.click(within(screen.getByTestId("cheat-row-7")).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByText(/Closing does not undo any of it/)).toBeTruthy();

    // Reconciliation cleared the dirty sets, so the count alone would have let
    // this close silently over state that was already committed.
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onCancel).not.toHaveBeenCalled();
    expect(await screen.findByTestId("unsaved-prompt")).toBeTruthy();
  });

  it("keeps a table beyond the live-control budget connected instead of failing it", async () => {
    // The limit is a live-control budget, not a launch precondition: the design
    // says such a table stays usable for inspection and Auto-load while only
    // picker/snapshot/bulk operations degrade. Awaiting the snapshot as part of
    // the success transaction reported a connected, attached session as failed.
    const controls = Array.from({ length: 513 }, (_, index) => ({
      id: 1000 + index, description: `Cheat ${index}`, path: [`Cheat ${index}`],
      variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false,
      dropdown_values: [], dropdown_read_only: false,
    }));
    api.inspectTableSha.mockResolvedValue({ ...inspect, total_entries: controls.length, controls });
    const live = liveRuntime();
    live.status.results = [];
    api.getRuntimeStatus.mockResolvedValue(live);
    renderContent();

    expect(await screen.findByText("Game.CT")).toBeTruthy();
    expect(await screen.findByText(/Live controls are unavailable for a table this size/)).toBeTruthy();
    // The snapshot is skipped outright rather than attempted and failed.
    expect(runtimeClient.queryRuntimeControlsPartial).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Disable all" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("does not rewrite remembered cheats when Disable all loses the target", async () => {
    api.setRememberedCheats.mockClear();
    runtimeClient.deactivateAllActiveControls.mockRejectedValue(new Error("target process is not attached"));
    renderContent();

    await screen.findByText("1 active");
    fireEvent.click(await screen.findByRole("button", { name: "Disable all" }));

    await waitFor(() => expect(runtimeClient.deactivateAllActiveControls).toHaveBeenCalled());
    expect(api.setRememberedCheats).not.toHaveBeenCalled();
  });
});

describe("Advanced diagnostics", () => {
  function renderAdvanced(overrides: Record<string, any> = {}) {
    return render(<AdvancedModal
      status={status(true)}
      games={[game]}
      selectedGame={game}
      appDetails={details}
      inspection={null}
      targetProcess="game.exe"
      ceLaunch={launch}
      launchProtonToolId=""
      runtime={noRuntime()}
      selfTest={null}
      busy={false}
      onRefreshGames={vi.fn()}
      onSaveTargetProcess={vi.fn()}
      onPickCE={vi.fn()}
      onClearCEImport={vi.fn()}
      onRunSelfTest={vi.fn()}
      onLaunchProtonChange={vi.fn()}
      onRunCELaunchSelfTest={vi.fn()}
      onRefreshRuntime={vi.fn()}
      onRefreshProcesses={vi.fn()}
      onRetryAttach={vi.fn()}
      onRepairSessionState={vi.fn()}
      onRepairOwnedLaunchState={vi.fn()}
      onRepairProfileState={vi.fn()}
      onClearStartup={vi.fn()}
      onRevokeConsent={vi.fn()}
      onCheckRemoval={vi.fn()}
      onLoadDiagnostics={vi.fn().mockResolvedValue(diagnostics)}
      onRefreshAll={vi.fn()}
      onClose={vi.fn()}
      {...overrides}
    />);
  }

  it("says a self-test that only a non-blocking check failed is a warning, and names it", async () => {
    // The backend's `ok` is about blockers alone, and the checks that answer
    // whether a later bug report will have any evidence in it are deliberately
    // not blockers. This screen used to render `ok` as PASS and display the
    // failure nowhere at all, so the diagnostic detected exactly the condition
    // it was added for and told the user everything was fine.
    const mixed = {
      ok: true,
      checks: [
        { name: "settings", ok: true, detail: "/settings", blocking: true },
        { name: "system_journal", ok: false, detail: "OPENSSL_3.4.0 not found in libcrypto", blocking: false },
      ],
    };
    const view = renderAdvanced({ onRunSelfTest: vi.fn().mockResolvedValue(mixed) });

    fireEvent.click(within(view.container).getByRole("button", { name: "Self-test" }));

    await within(view.container).findByText("Self-test WARN");
    expect(within(view.container).queryByText("Self-test PASS")).toBeNull();
    const failure = await within(view.container).findByTestId("self-test-failure-system_journal");
    expect(failure.textContent).toContain("System journal (warning)");
    expect(failure.textContent).toContain("OPENSSL_3.4.0 not found in libcrypto");
    // And the count that used to be the only hint is still there.
    expect(within(view.container).getByText("1/2 checks")).toBeTruthy();
  });

  it("keeps an all-green self-test an unqualified PASS with nothing listed under it", async () => {
    const view = renderAdvanced({
      onRunSelfTest: vi.fn().mockResolvedValue({
        ok: true, checks: [{ name: "settings", ok: true, detail: "/settings", blocking: true }],
      }),
    });

    fireEvent.click(within(view.container).getByRole("button", { name: "Self-test" }));

    await within(view.container).findByText("Self-test PASS");
    expect(within(view.container).queryByTestId("self-test-failure-settings")).toBeNull();
  });

  it("keeps a blocking failure a FAIL and shows the blocker's reason", async () => {
    const view = renderAdvanced({
      onRunSelfTest: vi.fn().mockResolvedValue({
        ok: false,
        checks: [
          { name: "managed_root", ok: false, detail: "read-only file system", blocking: true },
          { name: "host_7zip", ok: false, detail: "7z not found", blocking: false },
        ],
      }),
    });

    fireEvent.click(within(view.container).getByRole("button", { name: "Self-test" }));

    await within(view.container).findByText("Self-test FAIL");
    const blocker = within(view.container).getByTestId("self-test-failure-managed_root");
    expect(blocker.textContent).toContain("Managed root (blocker)");
    expect(blocker.textContent).toContain("read-only file system");
    // The warning beside it is not hidden by the blocker.
    expect(within(view.container).getByTestId("self-test-failure-host_7zip").textContent).toContain("7z not found");
  });

  it("tells the release a table was downloaded as apart from Cheat Engine's table format", () => {
    // Search offers a FearLess revision as v1.0.6; Advanced described the same
    // file as "version 52", which is the CT format number the file declares.
    // Two different numbers under one word read as a contradiction.
    const withOrigin = status(true);
    withOrigin.tables = [{
      ...table,
      origins: [{
        provider: "fearless", artifact_id: "topic-1:attachment-2", topic_id: "1",
        source_page: "https://fearlessrevolution.com/viewtopic.php?t=1",
        original_filename: "Game.CT", retrieved_at: "2026-08-31T00:00:00Z",
        advertised_sha256: null, version: "1.0.6",
      }],
    }];
    renderAdvanced({ status: withOrigin });

    expect(screen.getByText(/v1\.0\.6 · CE table 45/)).toBeTruthy();
    expect(screen.queryByText(/version 45/)).toBeNull();
  });

  it("shows no release for a table imported before one was recorded", () => {
    renderAdvanced();

    expect(screen.getByText(/CE table 45/).textContent).not.toMatch(/v\d/);
  });

  it("says what Cheat Engine asked when a dialog was answered for the user", () => {
    // Over a running game a Cheat Engine dialog cannot be read or answered, and
    // on this hardware it takes the screen from the game, so CE Decky closes
    // it. Closing is an answer, and an answer nobody gave has to be visible.
    const withDialog = liveRuntime();
    withDialog.status = { ...withDialog.status, dialogs_dismissed: 2, last_dialog: "Confirmation" } as any;
    renderAdvanced({ runtime: withDialog });

    const row = screen.getByTestId("dialogs-dismissed");
    expect(row.textContent).toContain("2 Cheat Engine dialog(s) dismissed");
    expect(row.textContent).toContain("Confirmation");
  });

  it("stays quiet about dialogs while none has been dismissed", () => {
    renderAdvanced({ runtime: liveRuntime() });

    expect(screen.queryByTestId("dialogs-dismissed")).toBeNull();
  });

  it("says a window is over the game when the sweep could not take it down", () => {
    // A table can build a window of its own now that its Lua script runs, and
    // Cheat Engine does not always let that one be hidden. It is the only case
    // where the game is losing its screen at this moment rather than having
    // lost it, so it is said outright and above the count that worked.
    const stuck = liveRuntime();
    stuck.status = {
      ...stuck.status, window_suppressions: 3, unsuppressed_sweeps: 12, window_over_game: true,
    } as any;
    renderAdvanced({ runtime: stuck });

    const row = screen.getByTestId("window-over-game");
    expect(row.textContent).toContain("A Cheat Engine window could not be hidden");
    expect(row.textContent).toContain("Stop Cheat Engine");
    // The sweep runs on a timer, so twelve failed attempts are one window and
    // not twelve of them. Nothing here claims a number of windows.
    expect(row.textContent).not.toContain("12");
    expect(screen.queryByTestId("unsuppressed-sweeps")).toBeNull();
    // The one that worked is still reported, and separately.
    expect(screen.getByTestId("window-suppressions").textContent).toContain("3 Cheat Engine window(s) hidden");
  });

  it("says the screen came back once the window went down", () => {
    // The count only climbs, so it cannot be what tells the user the game is
    // covered: a window that eventually went down used to leave the panel
    // saying the screen was still lost for the rest of the session.
    const recovered = liveRuntime();
    recovered.status = {
      ...recovered.status, window_suppressions: 1, unsuppressed_sweeps: 12, window_over_game: false,
    } as any;
    renderAdvanced({ runtime: recovered });

    expect(screen.queryByTestId("window-over-game")).toBeNull();
    const row = screen.getByTestId("unsuppressed-sweeps");
    expect(row.textContent).toContain("12 attempt(s) to hide a Cheat Engine window failed");
  });

  it("stays quiet about unhidden windows while every sweep worked", () => {
    const clean = liveRuntime();
    clean.status = { ...clean.status, window_suppressions: 2 } as any;
    renderAdvanced({ runtime: clean });

    expect(screen.queryByTestId("window-over-game")).toBeNull();
    expect(screen.queryByTestId("unsuppressed-sweeps")).toBeNull();
    expect(screen.getByTestId("window-suppressions")).toBeTruthy();
  });

  it("offers the game's own processes when no Cheat Engine can be asked for PIDs", async () => {
    // Processes exists to recover from an attachment that chose the wrong
    // program, which is very often the same situation in which Cheat Engine
    // will not start at all - so disabling it there disabled it in exactly the
    // case it was written for. Without a session it now reads the game's own
    // Windows processes from this machine instead of refusing.
    const observed = { ...launch, game: { ...launch.game, windows_executables: ["Game-Win64-Shipping.exe", "explorer.exe"] } };
    const onRefreshAll = vi.fn().mockResolvedValue({
      status: status(true), ceLaunch: observed, runtime: { ...noRuntime(), prepared: liveRuntime().prepared },
      appDetails: details, inspection: null, targetProcess: "game.exe",
    });
    renderAdvanced({ runtime: { ...noRuntime(), prepared: liveRuntime().prepared }, ceLaunch: observed, onRefreshAll });

    const processes = screen.getByRole("button", { name: "Processes" }) as HTMLButtonElement;
    expect(processes.disabled).toBe(false);
    fireEvent.click(processes);

    await waitFor(() => expect(onRefreshAll).toHaveBeenCalledTimes(1));
    const row = await screen.findByTestId("observed-targets");
    expect(row.textContent).toContain("2 process(es) observed in the game");
    expect(row.textContent).toContain("Cheat Engine is not running for this game");
  });

  it("saves a process chosen from the game's own processes as the target", async () => {
    const observed = { ...launch, game: { ...launch.game, windows_executables: ["Game-Win64-Shipping.exe"] } };
    const onSaveTargetProcess = vi.fn().mockResolvedValue(undefined);
    const onRefreshAll = vi.fn().mockResolvedValue({
      status: status(true), ceLaunch: observed, runtime: noRuntime(),
      appDetails: details, inspection: null, targetProcess: "game.exe",
    });
    renderAdvanced({ ceLaunch: observed, onRefreshAll, onSaveTargetProcess });

    fireEvent.click(screen.getByRole("button", { name: "Processes" }));
    await screen.findByTestId("observed-targets");

    fireEvent.change(screen.getByLabelText("Target from observed processes"), {
      target: { value: "Game-Win64-Shipping.exe" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save as target" }));

    await waitFor(() => expect(onSaveTargetProcess).toHaveBeenCalledWith("Game-Win64-Shipping.exe"));
  });

  it("asks the running Cheat Engine for exact PIDs while the session is live", async () => {
    const onRefreshProcesses = vi.fn().mockResolvedValue(liveRuntime());
    renderAdvanced({ runtime: liveRuntime(), onRefreshProcesses });

    expect((screen.getByRole("button", { name: "Processes" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Processes" }));

    await waitFor(() => expect(onRefreshProcesses).toHaveBeenCalledTimes(1));
    // The exact-PID list is the answer here, so the machine-side fallback with
    // no PIDs in it must not also be on screen.
    expect(screen.queryByTestId("observed-targets")).toBeNull();
  });

  it("toasts a self-test whose only failure was non-blocking as a warning rather than as passed", async () => {
    // The toast is what a user reads when the modal is already closing, so
    // "passed" for a result carrying a failed check is the same defect as the
    // summary row: it names the opposite of what the diagnostic found.
    api.runSelfTest.mockResolvedValueOnce({
      ok: true,
      checks: [
        { name: "settings", ok: true, detail: "/settings", blocking: true },
        { name: "panel_journal", ok: false, detail: "state root is not writable", blocking: false },
      ],
    });
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Advanced…" }));
    const advanced = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === AdvancedModal);
      expect(node).toBeTruthy();
      return node;
    });
    const view = render(advanced);

    fireEvent.click(within(view.container).getByRole("button", { name: "Self-test" }));

    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: "Plugin self-test found 1 warning." }),
    ));
    expect(decky.toast).not.toHaveBeenCalledWith(expect.objectContaining({ body: "Plugin self-test passed." }));
  });

  it("sends actual UI actions and failures through the panel's Collect callback to the bundle RPC", async () => {
    const { resetSupportLog, readSupportLog } = await import("../src/supportLog");
    resetSupportLog();
    api.runSelfTest.mockRejectedValueOnce(new Error("self-test refused"));
    api.createSupportBundle.mockResolvedValue({
      schema: 1, path: "/user/support.zip", filename: "support.zip", size_bytes: 100,
      member_count: 1, uncompressed_bytes: 200, notes: [], removed_older_bundles: [],
    });
    renderContent();
    await screen.findByText("Game.CT");
    fireEvent.click(screen.getByRole("button", { name: "Advanced…" }));
    const advanced = await waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === AdvancedModal);
      expect(node).toBeTruthy();
      return node;
    });
    const view = render(advanced);
    fireEvent.click(within(view.container).getByRole("button", { name: "Self-test" }));
    await waitFor(() => expect(readSupportLog().entries).toEqual(expect.arrayContaining([
      expect.objectContaining({ event: "panel.action_failed", fields: expect.objectContaining({ action: "advanced_modal.self_test" }) }),
    ])));
    const collect = within(view.container).getByRole("button", { name: "Collect" });
    await waitFor(() => expect((collect as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(collect);
    await waitFor(() => expect(api.createSupportBundle).toHaveBeenCalledOnce());
    const [entries, dropped] = api.createSupportBundle.mock.calls[0];
    expect(dropped).toBe(0);
    expect(entries).toEqual(expect.arrayContaining([
      expect.objectContaining({ event: "ui.action", fields: expect.objectContaining({ action: "home_panel.advanced", app_id: "10", table_sha: SHA }) }),
      expect.objectContaining({ event: "ui.action", fields: expect.objectContaining({ action: "advanced_modal.self_test" }) }),
      expect.objectContaining({ event: "ui.operation_failed", fields: expect.objectContaining({ operation: "advanced.invoke", message: "self-test refused" }) }),
      expect.objectContaining({ event: "ui.action", fields: expect.objectContaining({ action: "advanced_modal.collect" }) }),
    ]));
    await within(view.container).findByText("/user/support.zip");
  });

  it("writes a support bundle and shows the path in a dialog that has to be dismissed", async () => {
    // A user in Game Mode has to find this file afterwards from a desktop
    // session or a file transfer, so the path has to survive being read. A
    // toast that scrolls away with the path in it is the same as no path.
    const bundle = {
      schema: 1, path: "/home/deck/ce-decky-support-0.9.7-20260901-120000.zip",
      filename: "ce-decky-support-0.9.7-20260901-120000.zip",
      size_bytes: 1024 * 512, member_count: 31, uncompressed_bytes: 2_000_000,
      notes: [], removed_older_bundles: [],
    };
    const onCollectSupportBundle = vi.fn().mockResolvedValue(bundle);
    renderAdvanced({ onCollectSupportBundle });

    fireEvent.click(screen.getByRole("button", { name: "Collect" }));

    await waitFor(() => expect(onCollectSupportBundle).toHaveBeenCalledTimes(1));
    const dialog = await waitFor(() => {
      const found = modalState.nodes[modalState.nodes.length - 1];
      expect(found).toBeTruthy();
      return found;
    });
    const rendered = render(dialog);
    // Scoped to the dialog: the path is deliberately on the Advanced screen as
    // well, and an unscoped query would pass on either one alone.
    const inDialog = within(rendered.container);
    expect(inDialog.getByText("Support bundle saved")).toBeTruthy();
    expect(inDialog.getByText(bundle.path)).toBeTruthy();
    // Exactly one action: there is nothing to decide, only something to read.
    expect(inDialog.getByRole("button", { name: "OK" })).toBeTruthy();
    expect(inDialog.queryByRole("button", { name: "Cancel" })).toBeNull();
    // The path stays on the Advanced screen too, for a user who dismissed it.
    expect((await screen.findByTestId("support-bundle-path")).textContent).toContain(bundle.path);
    rendered.unmount();
  });

  it("says what could not be collected instead of implying the archive is complete", async () => {
    const onCollectSupportBundle = vi.fn().mockResolvedValue({
      schema: 1, path: "/home/deck/bundle.zip", filename: "bundle.zip",
      size_bytes: 2048, member_count: 4, uncompressed_bytes: 4096,
      notes: [{ member: "state/profiles.json", reason: "ValueError: path is a symlink", kind: "omitted" }],
      removed_older_bundles: [],
    });
    renderAdvanced({ onCollectSupportBundle });

    fireEvent.click(screen.getByRole("button", { name: "Collect" }));

    const notes = await screen.findByTestId("support-bundle-notes");
    expect(notes.textContent).toContain("1 item(s) not collected");
    expect(notes.textContent).toContain("state/profiles.json");
  });

  it("does not count nothing to collect as something it could not collect", async () => {
    // A Steam Deck that has never launched Cheat Engine has no launch logs, and
    // a plugin reloaded a moment ago has no log file yet. Both were reported as
    // items that could not be collected, on a device where nothing was wrong.
    const onCollectSupportBundle = vi.fn().mockResolvedValue({
      schema: 1, path: "/home/deck/bundle.zip", filename: "bundle.zip",
      size_bytes: 2048, member_count: 4, uncompressed_bytes: 4096,
      notes: [
        { member: "logs/plugin/", reason: "no plugin log files were found", kind: "absent" },
        { member: "logs/ce-launch/", reason: "no launch logs were found", kind: "absent" },
      ],
      removed_older_bundles: [],
    });
    renderAdvanced({ onCollectSupportBundle });

    fireEvent.click(screen.getByRole("button", { name: "Collect" }));

    // The path row proves the bundle was collected and the screen settled; the
    // notes row is the one that must not be there.
    await screen.findByTestId("support-bundle-path");
    expect(screen.queryByTestId("support-bundle-notes")).toBeNull();
  });

  it("does not count a member the archive carries as one it could not collect", async () => {
    // The count was written as everything that is not `truncated`, so `partial`,
    // added to the collector afterwards, was reported as a missing file on every
    // healthy bundle. Both of these are in the archive and both read.
    const onCollectSupportBundle = vi.fn().mockResolvedValue({
      schema: 1, path: "/home/deck/bundle.zip", filename: "bundle.zip",
      size_bytes: 2048, member_count: 4, uncompressed_bytes: 4096,
      notes: [
        { member: "logs/journal.txt", reason: "included, truncated to its size limit", kind: "truncated" },
        { member: "logs/plugin/one.log", reason: "only its newest part is here", kind: "partial" },
      ],
      removed_older_bundles: [],
    });
    renderAdvanced({ onCollectSupportBundle });

    fireEvent.click(screen.getByRole("button", { name: "Collect" }));

    const shortened = await screen.findByTestId("support-bundle-shortened");
    expect(shortened.textContent).toContain("2 item(s) shortened");
    expect(screen.queryByTestId("support-bundle-notes")).toBeNull();
  });

  it("reports a failed collection in place rather than leaving the row unchanged", async () => {
    // Collecting is read-only and reported here rather than through the
    // parent's toast, so without this the press looked like it did nothing.
    const onCollectSupportBundle = vi.fn().mockRejectedValue(new Error("no space left on device"));
    renderAdvanced({ onCollectSupportBundle });

    fireEvent.click(screen.getByRole("button", { name: "Collect" }));

    expect(await screen.findByText("no space left on device")).toBeTruthy();
  });

  it("says why diagnostics could not be read instead of rendering an empty screen", async () => {
    // This callback is the one Advanced invokes directly rather than through
    // the parent's action wrapper, so nothing else reported its failure and the
    // screen was neither loading, nor an error, nor a snapshot.
    const onLoadDiagnostics = vi.fn().mockRejectedValue(new Error("diagnostics unavailable"));
    renderAdvanced({ onLoadDiagnostics });

    fireEvent.click(screen.getByRole("button", { name: "Debug" }));
    await waitFor(() => expect(onLoadDiagnostics).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("diagnostics unavailable")).toBeTruthy();
  });

  it("refreshes the detached repair screen after authority moved but sync failed", async () => {
    const corrupt = status(true);
    corrupt.profile_state_reason = "profiles state has an unsupported or corrupt schema";
    const onRepairProfileState = vi.fn().mockRejectedValue(new Error("profile authority was quarantined; durability unknown"));
    const onRefreshAll = vi.fn().mockResolvedValue({
      status: status(true), ceLaunch: launch, runtime: noRuntime(), appDetails: details,
      inspection: null, targetProcess: "game.exe",
    });
    renderAdvanced({ status: corrupt, onRepairProfileState, onRefreshAll });
    fireEvent.click(within(screen.getByTestId("profile-state-repair")).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(onRefreshAll).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByTestId("profile-state-repair")).toBeNull());
  });

  it("offers a controller repair when the game settings file cannot be read", async () => {
    const onRepairProfileState = vi.fn().mockResolvedValue(undefined);
    const corrupt = status(true);
    corrupt.profile_state_reason = "profiles state has an unsupported or corrupt schema";
    renderAdvanced({ status: corrupt, onRepairProfileState });

    expect(screen.getByTestId("profile-state-repair").textContent).toContain("corrupt schema");
    fireEvent.click(within(screen.getByTestId("profile-state-repair")).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(onRepairProfileState).toHaveBeenCalledTimes(1));
  });

  it("counts what a provider published that could not be read, apart from what it cost", async () => {
    // A source that quietly stops being readable otherwise looks like a game
    // with no tables. An unreadable page is the critical one - nothing on it is
    // downloadable - and dropped description is not, so they are not one number.
    const withParseIssues = {
      ...diagnostics,
      providers: {
        fearless: {
          ...diagnostics.providers.fearless,
          counters: { ...diagnostics.providers.fearless.counters, parse_degraded: 4, parse_failed: 2 },
        },
      },
    };
    renderAdvanced({ onLoadDiagnostics: vi.fn().mockResolvedValue(withParseIssues) });

    fireEvent.click(screen.getByRole("button", { name: "Debug" }));
    const row = await screen.findByTestId("debug-provider-fearless");
    expect(row.textContent).toContain("2 unreadable page(s)");
    expect(row.textContent).toContain("4 partly read");
  });

  it("marks diagnostics from a removed provider as historical", async () => {
    const withRetiredProvider = {
      ...diagnostics,
      providers: {
        ...diagnostics.providers,
        opencheattables: { ...diagnostics.providers.fearless, retired: true },
      },
    };
    renderAdvanced({ onLoadDiagnostics: vi.fn().mockResolvedValue(withRetiredProvider) });

    fireEvent.click(screen.getByRole("button", { name: "Debug" }));
    const row = await screen.findByTestId("debug-provider-opencheattables");
    expect(row.textContent).toContain("opencheattables (retired)");
    expect(row.textContent).toContain("historical provider");
  });

  it("names each app's sessions, pages them, and keeps them last on the screen", async () => {
    // Every game this device has ever prepared a session for keeps a row here,
    // so this is the one list on Debug that grows on its own: it sat between
    // the storage totals and the providers, and the rows below it were reached
    // by paging past however many games the device happens to hold. A row also
    // said `AppID 1971870` and nothing else, which is the one thing on this
    // screen a reader cannot look up without leaving it.
    const many = {
      ...diagnostics,
      sessions: {
        total_sessions: 16,
        errors: [],
        apps: [
          { app_id: 10, session_count: 2, current_session_id: "abcdef1234", current_error: null, corrupt_entries: 0 },
          ...Array.from({ length: 7 }, (_, index) => ({
            app_id: 500 + index, session_count: 2, current_session_id: null,
            current_error: null, corrupt_entries: 0,
          })),
          { app_id: 900, session_count: 1, current_session_id: null, current_error: null, corrupt_entries: 3 },
        ],
      },
    };
    renderAdvanced({ onLoadDiagnostics: vi.fn().mockResolvedValue(many) });
    fireEvent.click(screen.getByRole("button", { name: "Debug" }));

    // The library names the game this device still holds; the rest keep the
    // number, which is what is known about them.
    expect((await screen.findByTestId("debug-session-10")).textContent).toContain("Game \u00b7 AppID 10");
    // Anything unreadable is first, so it is not behind a page of working games.
    expect(screen.getByTestId("debug-session-900").textContent).toContain("3 corrupt");
    const summary = screen.getByTestId("debug-sessions-summary");
    expect(summary.textContent).toContain("9 app(s)");
    expect(summary.textContent).toContain("1 with something to read");

    // Six at a time, and the rest behind the pager.
    expect(screen.getAllByTestId(/^debug-session-/)).toHaveLength(6);
    expect(screen.queryByTestId("debug-session-506")).toBeNull();
    fireEvent.click(within(screen.getByTestId("debug-sessions-pager")).getByRole("button", { name: "Next" }));
    expect(await screen.findByTestId("debug-session-506")).toBeTruthy();

    // And the section is the last one on the screen, so nothing a reader came
    // for is behind it.
    const sections = Array.from(document.querySelectorAll("section"));
    const sessions = sections.findIndex((node) => node.textContent?.includes("Sessions by app"));
    const capabilities = sections.findIndex((node) => node.textContent?.includes("Capabilities"));
    const providers = sections.findIndex((node) => node.textContent?.includes("Providers"));
    expect(sessions).toBeGreaterThan(providers);
    expect(sessions).toBeGreaterThan(capabilities);
  });

  it("says the saved process is not among what this game actually started", async () => {
    // The check that catches a target chosen before the game had ever run. A
    // table can be authorized against an executable read out of the game's own
    // installed folder, which is the only evidence there is while nothing is
    // running, and this is the first chance to check it: pressing start with
    // the wrong name attaches to nothing and looks exactly like Cheat Engine
    // failing.
    const running = {
      ...launch,
      game: {
        ...launch.game, app_id: 10, running: true, pids: [4321],
        windows_executables: ["Game-Win64-Shipping.exe", "explorer.exe"],
      },
    };
    renderAdvanced({ ceLaunch: running });
    const row = await screen.findByTestId("target-not-running");
    expect(row.textContent).toContain("game.exe is not running in this game");
    // What it did start, with the Wine machinery dropped: none of it ever owns
    // a game's memory, and a dozen of them turns a fix into a search.
    expect(row.textContent).toContain("Game-Win64-Shipping.exe");
    expect(row.textContent).not.toContain("explorer.exe");
    const onSaveTargetProcess = vi.fn();
    cleanup();
    renderAdvanced({ ceLaunch: running, onSaveTargetProcess });
    fireEvent.click(await screen.findByRole("button", { name: "Use Game-Win64-Shipping.exe" }));
    await waitFor(() => expect(onSaveTargetProcess).toHaveBeenCalledWith("Game-Win64-Shipping.exe"));
  });

  it("names several candidates without picking one of them", async () => {
    // One press writing a target stops the Cheat Engine running now, so it is
    // offered only where there is a single answer. With more than one this
    // cannot rank them, and guessing is what the row exists to catch.
    const two = {
      ...launch,
      game: {
        ...launch.game, app_id: 10, running: true, pids: [4321],
        windows_executables: ["Game-Win64-Shipping.exe", "GameLauncher.exe", "explorer.exe"],
      },
    };
    renderAdvanced({ ceLaunch: two });
    const row = await screen.findByTestId("target-not-running");
    expect(row.textContent).toContain("Game-Win64-Shipping.exe, GameLauncher.exe");
    // The `?` that opens the row's help is still there; what is not is a press
    // that writes one of the names.
    expect(within(row).queryByRole("button", { name: /^Use / })).toBeNull();
    expect(row.textContent).toContain("Set the one this table is for");
  });

  it("says nothing when all the game has running is Wine's own machinery", async () => {
    // `running` stays true while wineserver and the Proton chain still report
    // the AppID after the game has exited, so this is what a game that is gone
    // looks like as well as one that is starting. Offering explorer.exe as a
    // one-press repair there overwrites a working target for nothing.
    renderAdvanced({ ceLaunch: { ...launch, game: {
      ...launch.game, running: true, windows_executables: ["explorer.exe", "services.exe"],
    } } });
    await screen.findByLabelText("Target process");
    expect(screen.queryByTestId("target-not-running")).toBeNull();
  });

  it("says nothing about a target whose absence it cannot prove", async () => {
    // A warning a user cannot act on is worse than none. A game that is not
    // running started nothing, and an observation with no Windows process in it
    // is a prefix that has not got going yet.
    renderAdvanced({ ceLaunch: { ...launch, game: { ...launch.game, running: false } } });
    await screen.findByLabelText("Target process");
    expect(screen.queryByTestId("target-not-running")).toBeNull();

    cleanup();
    renderAdvanced({ ceLaunch: { ...launch, game: { ...launch.game, running: true, windows_executables: [] } } });
    await screen.findByLabelText("Target process");
    expect(screen.queryByTestId("target-not-running")).toBeNull();
  });

  it("shows the backend diagnostics snapshot that nothing else surfaces", async () => {
    const onLoadDiagnostics = vi.fn().mockResolvedValue(diagnostics);
    renderAdvanced({ onLoadDiagnostics });

    fireEvent.click(screen.getByRole("button", { name: "Debug" }));
    await waitFor(() => expect(onLoadDiagnostics).toHaveBeenCalledTimes(1));

    expect(await screen.findByText(/2 tables/)).toBeTruthy();
    expect(screen.getByTestId("debug-provider-fearless").textContent).toContain("1/2 downloads");
    expect(screen.getByTestId("debug-session-10").textContent).toContain("current abcdef12");
    expect(screen.getByTestId("debug-state-Tables").textContent).toContain("tables unreadable");
    // It is evidence, so the workflow controls must not be reachable from here.
    expect(screen.queryByRole("button", { name: "Verify" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("button", { name: "Verify" })).toBeTruthy();
  });

  it("names the observed Proton, the compatdata directory and the game's own prefix", () => {
    renderAdvanced({
      ceLaunch: {
        ...launch,
        observed_proton_tool: { tool_id: "p7", name: "GE-Proton", path: "/tools/ge", proton_sha256: "c".repeat(64), source: "library" },
        compat_data: { app_id: 10, state: "resolved", compat_data_path: "/compat/10", candidates: ["/compat/10"], unsafe_paths: [] },
        game: { ...launch.game, wine_prefix: "/compat/10/pfx", windows_executables: ["game.exe"], launch_executable: "game.exe" },
      },
    });

    expect(screen.getByText("GE-Proton")).toBeTruthy();
    expect(screen.getByText(`p7 · ${"c".repeat(8)} · library`)).toBeTruthy();
    expect(screen.getByText("Compatdata resolved")).toBeTruthy();
    expect(screen.getByText("/compat/10")).toBeTruthy();
    expect(screen.getByText("/compat/10/pfx")).toBeTruthy();
    expect(screen.getByText("game.exe · Steam launched game.exe")).toBeTruthy();
  });

  it("offers guarded repair for malformed durable launch ownership", async () => {
    const onRepairOwnedLaunchState = vi.fn().mockResolvedValue({
      status: status(true), ceLaunch: launch, runtime: noRuntime(), appDetails: details,
      inspection: null, targetProcess: "game.exe",
    });
    renderAdvanced({
      ceLaunch: {
        ...launch,
        recovery_error: "the owned Cheat Engine launch record is present but invalid",
        owned_launch_owners: [{ app_id: 10, state: "invalid", recovered: true }],
      },
      onRepairOwnedLaunchState,
    });

    expect(screen.getByTestId("launch-ownership-repair")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Repair" }));
    await waitFor(() => expect(onRepairOwnedLaunchState).toHaveBeenCalledTimes(1));
  });

  it("says why an attach cannot happen when no Proton identity was observed", () => {
    renderAdvanced({
      ceLaunch: { ...launch, observed_proton_tool: null, observed_proton_reason: "no running game", compat_data: null },
    });
    expect(screen.getByText("Proton not observed")).toBeTruthy();
    expect(screen.getByText("no running game")).toBeTruthy();
    expect(screen.getByText("Compatdata not resolved")).toBeTruthy();
  });

  it("details the exact table this game is configured to use", () => {
    renderAdvanced({
      inspection: inspect,
      status: {
        ...status(true),
        tables: [{ ...table, size: 4096, origins: [{ provider: "fearless", artifact_id: "a", topic_id: "t", source_page: "https://example/t", original_filename: "Game v45.CT", retrieved_at: "2026-08-20T10:00:00Z", advertised_sha256: SHA }] }],
      },
    });

    expect(screen.getByText("Game.CT")).toBeTruthy();
    expect(screen.getByText(`${SHA.slice(0, 12)} · 4.0 KiB · CE table 45 · 2026-08-20`)).toBeTruthy();
    expect(screen.getByText("From FearLess Cheat Engine")).toBeTruthy();
    expect(screen.getByText("https://example/t")).toBeTruthy();
    expect(screen.getByText("/managed/Game.CT")).toBeTruthy();
    expect(screen.getByText("Execution authorized")).toBeTruthy();
    expect(screen.getByText("0 pinned · 1 remembered · 1 table(s) imported for this game · auto-load on")).toBeTruthy();
    expect(screen.getByText("1 entries · 1 controls")).toBeTruthy();
  });

  it("reports the resolved library entry instead of offering a second game picker", () => {
    renderAdvanced({ selectedGame: null });
    // Choosing a game is the panel's job; Advanced would otherwise be a second
    // control for the same decision, one screen deeper than the first.
    expect(screen.queryByLabelText("Game")).toBeNull();
    expect(screen.getByText("Steam library")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Refresh library" })).toBeTruthy();
  });
});

describe("Selection and staged-change persistence", () => {
  it("restores an explicit game choice after the panel is unmounted", async () => {
    // Opening any Decky modal closes the panel, so a manual pick that lives only
    // in React state disappears the moment the picker closes.
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    api.getStatus.mockResolvedValue(status(true));
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    const first = renderContent();
    // The loading panel renders the same button with an inert handler, so a
    // click that lands before hydration finishes is silently dropped.
    await screen.findByRole("button", { name: "Choose" });
    await waitFor(() => expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    await act(async () => { await modalState.nodes[0].props.onPick(game); });
    await waitFor(() => expect(screen.getByText(/Steam · game\.exe/)).toBeTruthy());

    first.unmount();
    cleanup();
    renderContent();
    expect(await screen.findByText(/Steam · game\.exe/)).toBeTruthy();
  });

  it("lets a running game supersede a game picked while nothing was running", async () => {
    const other = { appId: 11, name: "Other", sortAs: "Other", isShortcut: false };
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    steam.listInstalledGames.mockResolvedValue([game, other]);
    api.getStatus.mockResolvedValue(status(true));
    api.getRuntimeStatus.mockResolvedValue(noRuntime());
    const first = renderContent();
    // The loading panel renders the same button with an inert handler, so a
    // click that lands before hydration finishes is silently dropped.
    await screen.findByRole("button", { name: "Choose" });
    await waitFor(() => expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    await waitFor(() => expect(modalState.nodes.length).toBe(1));
    await act(async () => { await modalState.nodes[0].props.onPick(game); });
    await waitFor(() => expect(screen.getByText(/Steam · game\.exe/)).toBeTruthy());

    // The user then starts a different game; that observation outranks the pick.
    first.unmount();
    cleanup();
    steam.listRunningGames.mockResolvedValue({ available: true, games: [other] });
    steam.readAppDetails.mockResolvedValue({ ...details, appId: other.appId, displayName: "Other" });
    renderContent();
    expect(await screen.findByText("Other")).toBeTruthy();

    // And the superseded pick must not come back on the next remount either.
    cleanup();
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();
    expect(await screen.findByText("No game selected")).toBeTruthy();
  });

  it("cannot discard and close in the same turn that confirmed Apply starts", async () => {
    let fail!: (cause: Error) => void;
    const onValidateStartupPlan = vi.fn(() => new Promise<any>((_resolve, reject) => { fail = reject; }));
    const onCancel = vi.fn();
    render(<CheatSelectionModal appId={10} inspection={inspect as any} live pinned={[]}
      startupPreferences={[]} rememberedPreferences={[]} configuredValues={[]}
      onSaveConfiguredValues={vi.fn()} onValidateStartupPlan={onValidateStartupPlan}
      onTogglePin={vi.fn()} onApplied={vi.fn()} onCancel={onCancel} />);
    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "250" } });
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    const prompt = within(screen.getByTestId("unsaved-prompt"));
    const apply = prompt.getByRole("button", { name: "Apply" });
    const discard = prompt.getByRole("button", { name: "Discard" });
    act(() => { apply.click(); discard.click(); });
    expect(onValidateStartupPlan).toHaveBeenCalledOnce();
    expect(onCancel).not.toHaveBeenCalled();
    await act(async () => { fail(new Error("plan refused")); });
    expect((await screen.findAllByText("plan refused")).length).toBeGreaterThan(0);
  });

  it("offers to apply staged cheats instead of dropping them on Back", async () => {
    const onApplied = vi.fn().mockResolvedValue(undefined);
    const onCancel = vi.fn();
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results: liveRuntime().status.results });
    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={onCancel}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "250" } });
    expect(screen.getByText(/1 unapplied/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.getByTestId("unsaved-prompt")).toBeTruthy();

    fireEvent.click(within(screen.getByTestId("unsaved-prompt")).getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByTestId("unsaved-prompt")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    fireEvent.click(within(screen.getByTestId("unsaved-prompt")).getByRole("button", { name: "Discard" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(runtimeClient.applyRuntimeSelection).not.toHaveBeenCalled();
  });

  const nestedInspection = {
    ...inspect,
    total_entries: 2,
    controls: [
      { id: 20, description: "Party Damage Reduction", path: ["Party Damage Reduction"], variable_type: "Auto Assembler Script", kind: "script", group_header: false, has_assembler_script: true, dropdown_values: [], dropdown_read_only: false },
      { id: 21, description: "Reduction %", path: ["Party Damage Reduction", "Reduction %"], variable_type: "4 Bytes", kind: "value", group_header: false, has_assembler_script: false, dropdown_values: [], dropdown_read_only: false },
    ],
  };

  function renderNested(results: any[]) {
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ ...{ envelope: liveRuntime(), results }, unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results });
    const onApplied = vi.fn().mockResolvedValue(undefined);
    render(<CheatSelectionModal
      appId={10}
      inspection={nestedInspection as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);
    return onApplied;
  }

  it("sets a table up with no Cheat Engine running, writing nothing to a session", async () => {
    const onApplied = vi.fn().mockResolvedValue(undefined);
    render(<CheatSelectionModal
      appId={10}
      inspection={nestedInspection as any}
      live={false}
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[{ record_id: 21, active: null, value: "40" }]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    const child = await screen.findByTestId("cheat-row-21");
    expect(screen.getByText("Cheat Engine is not running")).toBeTruthy();
    expect(runtimeClient.queryRuntimeControlsPartial).not.toHaveBeenCalled();
    // The stored choice for this exact table is what the picker starts from.
    await revealDetails(within(child));
    expect((within(child).getByLabelText("Value") as HTMLInputElement).value).toBe("40");

    fireEvent.click(within(child).getByTestId("toggle"));
    fireEvent.change(within(child).getByLabelText("Value"), { target: { value: "95" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect(runtimeClient.applyRuntimeSelection).not.toHaveBeenCalled();
    // The cheat and its value are remembered; the script that creates it is
    // not, because nothing about it is the user's choice. Auto-load derives the
    // scripts a remembered record needs from the table itself, so storing one
    // added nothing and put it back on with every cheat under it off.
    expect(onApplied.mock.calls[0][0]).toEqual([
      { record_id: 21, active: true, value: "95" },
    ]);
    expect(onApplied.mock.calls[0][1]).toBeNull();
  });

  it("shows the effective legacy startup state before Cheat Engine is running", async () => {
    render(<CheatSelectionModal
      appId={10}
      inspection={nestedInspection as any}
      live={false}
      pinned={[]}
      startupPreferences={[{ record_id: 21, active: true, value: "30" }]}
      rememberedPreferences={[{ record_id: 21, active: null, value: "40" }]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={vi.fn()}
      onCancel={vi.fn()}
    />);

    const child = await screen.findByTestId("cheat-row-21");
    expect((within(child).getByTestId("toggle") as HTMLInputElement).checked).toBe(true);
    await revealDetails(within(child));
    expect((within(child).getByLabelText("Value") as HTMLInputElement).value).toBe("40");
  });

  it("hides the enclosing scripts until the header toggle asks for them", async () => {
    renderNested([
      { generation: 1, record_id: 20, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 21, ok: true, active: false, value: "??", error: null },
    ]);

    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(1));
    expect(screen.queryByTestId("cheat-row-20")).toBeNull();
    expect(screen.getByTestId("cheat-row-21")).toBeTruthy();
    expect(screen.getByText("2 supported · 1 shown · 1 script hidden")).toBeTruthy();

    fireEvent.click(within(screen.getByTestId("show-scripts")).getByTestId("toggle"));
    await waitFor(() => expect(screen.getAllByTestId(/^cheat-row-/)).toHaveLength(2));
    expect(screen.getByText("2 supported · 2 shown")).toBeTruthy();
  });

  it("switches on the enclosing script itself instead of refusing the cheat", async () => {
    const onApplied = renderNested([
      { generation: 1, record_id: 20, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 21, ok: true, active: false, value: "??", error: null },
    ]);

    const child = await screen.findByTestId("cheat-row-21");
    fireEvent.click(within(child).getByTestId("toggle"));
    fireEvent.change(within(child).getByLabelText("Value"), { target: { value: "95" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledTimes(1));
    const desired = runtimeClient.applyRuntimeSelection.mock.calls[0][1];
    expect(desired.find((item: any) => item.record_id === 20)).toMatchObject({ active: true });
    expect(desired.find((item: any) => item.record_id === 21)).toMatchObject({ active: true, value: "95" });
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    // The script had to be switched on, but the user never chose it, so it is
    // not written into the profile as though they had. Remembering it put the
    // script back on in the next session with every cheat under it off.
    expect(onApplied.mock.calls[0][0].map((item: any) => item.record_id)).toEqual([21]);
    expect(screen.queryByText("Cannot apply cheats")).toBeNull();
  });

  it("releases the enclosing script when the last cheat under it is switched off", async () => {
    // A script exists to create the records inside it, so one left running
    // afterwards keeps its patch in the game for no cheat at all.
    const onApplied = renderNested([
      { generation: 1, record_id: 20, ok: true, active: true, value: null, error: null },
      { generation: 1, record_id: 21, ok: true, active: true, value: "95", error: null },
    ]);

    const child = await screen.findByTestId("cheat-row-21");
    fireEvent.click(within(child).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledTimes(1));
    const desired = runtimeClient.applyRuntimeSelection.mock.calls[0][1];
    expect(desired.find((item: any) => item.record_id === 21)).toMatchObject({ active: false });
    expect(desired.find((item: any) => item.record_id === 20)).toMatchObject({ active: false });
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect(onApplied.mock.calls[0][0].map((item: any) => item.record_id)).toEqual([21]);
  });

  it("takes a cheat it could not switch off down with the script that made it", async () => {
    // A cheat CE Decky leaves on, because the table's own code was not read as
    // surviving it being switched off, must not be left running for ever. It is
    // not switched off either - that write is the thing that kills the game -
    // so what ends it is the script going down: the allocation the flag lives
    // in goes with it, and the game is back to what it was.
    const inspection = {
      ...nestedInspection,
      controls: [
        ...nestedInspection.controls,
        {
          id: 22, description: "bEnableVitalsDrain", path: ["Party Damage Reduction", "bEnableVitalsDrain"],
          variable_type: "4 Bytes", kind: "dropdown", group_header: false, has_assembler_script: false,
          dropdown_values: [["0", "Off"], ["1", "On"]], dropdown_read_only: false,
          switch_on_value: "1", declared_default: "1", switch_off_is_safe: false,
        },
      ],
    };
    const results = [
      { generation: 1, record_id: 20, ok: true, active: true, value: null, error: null },
      { generation: 1, record_id: 21, ok: true, active: true, value: "95", error: null },
      { generation: 1, record_id: 22, ok: true, active: false, value: "1", error: null },
    ];
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results, unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results });
    render(<CheatSelectionModal
      appId={10}
      inspection={inspection as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onCancel={vi.fn()}
    />);

    const child = await screen.findByTestId("cheat-row-21");
    fireEvent.click(within(child).getByTestId("toggle"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(runtimeClient.applyRuntimeSelection).toHaveBeenCalledTimes(1));
    const desired = runtimeClient.applyRuntimeSelection.mock.calls[0][1];
    // The script goes down, which is what takes the flag with it...
    expect(desired.find((item: any) => item.record_id === 20)).toMatchObject({ active: false });
    // ...and nothing is written to the flag itself.
    expect(desired.find((item: any) => item.record_id === 22)).toBeUndefined();
  });

  it("drops a script an older build left in the profile even when it stays on", async () => {
    // The script is legitimately on, because a cheat inside it is on, so
    // nothing releases it. It is still not a choice the user made, and the
    // startup profile derives it from the table, so the stale entry goes.
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results: [] });
    const results = [
      { generation: 1, record_id: 20, ok: true, active: true, value: null, error: null },
      { generation: 1, record_id: 21, ok: true, active: true, value: "95", error: null },
    ];
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results, unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results });
    const onApplied = vi.fn().mockResolvedValue(undefined);
    render(<CheatSelectionModal
      appId={10}
      inspection={nestedInspection as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[
        { record_id: 20, active: true, value: null },
        { record_id: 21, active: true, value: "95" },
      ]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    const child = await screen.findByTestId("cheat-row-21");
    fireEvent.change(within(child).getByLabelText("Value"), { target: { value: "80" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    // The script stays on in Cheat Engine; only the profile entry goes.
    expect(runtimeClient.applyRuntimeSelection.mock.calls[0][1]
      .find((item: any) => item.record_id === 20)).toBeUndefined();
    expect(onApplied.mock.calls[0][0].map((item: any) => item.record_id)).toEqual([21]);
  });

  it("saves a value pre-set before the game without writing it or failing", async () => {
    const onApplied = renderNested([
      { generation: 1, record_id: 20, ok: true, active: false, value: null, error: null },
      { generation: 1, record_id: 21, ok: true, active: false, value: "??", error: null },
    ]);

    const child = await screen.findByTestId("cheat-row-21");
    await revealDetails(within(child));
    fireEvent.change(within(child).getByLabelText("Value"), { target: { value: "95" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    // The reason it is not written now is stated as an aside, not as another
    // control the user has to deal with.
    expect(within(child).getByText(/Value saved; written when/)).toBeTruthy();
    // Nothing is written while the script that creates the address is off, but
    // the choice is kept for this exact table.
    expect(runtimeClient.applyRuntimeSelection.mock.calls[0][1]).toEqual([]);
    expect(onApplied.mock.calls[0][0]).toEqual([{ record_id: 21, active: null, value: "95" }]);
    expect(screen.queryByText("Cannot apply cheats")).toBeNull();
  });

  it("keeps a typed value that Cheat Engine cannot read back yet", async () => {
    const unreadable = { generation: 2, record_id: 7, ok: true, active: null, value: "??", error: null };
    runtimeClient.queryRuntimeControls
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [{ ...unreadable, generation: 1 }] })
      .mockResolvedValue({ envelope: liveRuntime(), results: [unreadable] });
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: liveRuntime(), results: [{ ...unreadable, generation: 1 }] , unavailable: [] })
      .mockResolvedValue({ envelope: liveRuntime(), results: [unreadable] , unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results: [unreadable] });
    const onApplied = vi.fn().mockResolvedValue(undefined);
    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "250" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    // The unreadable placeholder must not replace the value on screen, and the
    // typed choice - never CE's `??` - is what this table remembers.
    expect((screen.getByLabelText("Value") as HTMLInputElement).value).toBe("250");
    expect(onApplied.mock.calls[0][0]).toEqual([{ record_id: 7, active: null, value: "250" }]);
  });

  function renderConfigured(overrides: Record<string, any>, results: any[]) {
    runtimeClient.queryRuntimeControls.mockResolvedValue({ envelope: liveRuntime(), results });
    runtimeClient.queryRuntimeControlsPartial.mockResolvedValue({ envelope: liveRuntime(), results, unavailable: [] });
    runtimeClient.applyRuntimeSelection.mockResolvedValue({ envelope: liveRuntime(), results });
    return render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={vi.fn().mockResolvedValue(undefined)}
      onCancel={vi.fn()}
      {...overrides}
    />);
  }

  it("offers an empty field rather than Cheat Engine's own unreadable placeholder", async () => {
    renderConfigured({}, [{ generation: 1, record_id: 7, ok: true, active: true, value: "??", error: null }]);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    // Nothing is stored for this record and Cheat Engine cannot read it, so the
    // editor is what an editor with no value is: empty. It used to be primed
    // with `??`, which the user had to clear before they could type.
    expect((screen.getByLabelText("Value") as HTMLInputElement).value).toBe("");
    expect(screen.getByTestId("cheat-row-7").textContent).not.toContain("??");
  });

  it("commits the typed value as this table's own configuration before writing to Cheat Engine", async () => {
    const unreadable = { generation: 1, record_id: 7, ok: true, active: true, value: "??", error: null };
    const onSaveConfiguredValues = vi.fn().mockResolvedValue(undefined);
    renderConfigured({ onSaveConfiguredValues }, [unreadable]);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "9999" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onSaveConfiguredValues).toHaveBeenCalledTimes(1));
    expect(onSaveConfiguredValues.mock.calls[0][0]).toEqual([{ record_id: 7, value: "9999" }]);
    // The choice is durable before anything can fail in the session, which is
    // what stopped it from surviving a record Cheat Engine could not read.
    expect(onSaveConfiguredValues.mock.invocationCallOrder[0])
      .toBeLessThan(runtimeClient.applyRuntimeSelection.mock.invocationCallOrder[0]);
  });

  it("prefers the stored value only where Cheat Engine has no readable answer", async () => {
    const configuredValues = [{ record_id: 7, value: "9999" }];
    const unreadable = renderConfigured(
      { configuredValues },
      [{ generation: 1, record_id: 7, ok: true, active: true, value: "??", error: null }],
    );
    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    expect((screen.getByLabelText("Value") as HTMLInputElement).value).toBe("9999");
    unreadable.unmount();

    // A live session that can read the address is the truth for it: the stored
    // configuration must not paint a stale number over what the game holds.
    renderConfigured(
      { configuredValues },
      [{ generation: 1, record_id: 7, ok: true, active: true, value: "42", error: null }],
    );
    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    expect((screen.getByLabelText("Value") as HTMLInputElement).value).toBe("42");
  });

  it("drops the stored value when the field is emptied instead of writing a blank", async () => {
    const onSaveConfiguredValues = vi.fn().mockResolvedValue(undefined);
    renderConfigured(
      { configuredValues: [{ record_id: 7, value: "9999" }], onSaveConfiguredValues },
      [{ generation: 1, record_id: 7, ok: true, active: true, value: "42", error: null }],
    );

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => expect(onSaveConfiguredValues).toHaveBeenCalledTimes(1));
    expect(onSaveConfiguredValues.mock.calls[0][0]).toEqual([]);
    expect(runtimeClient.applyRuntimeSelection.mock.calls[0][1]).toEqual([]);
  });

  it("builds a second Apply on the value the first one committed", async () => {
    const onSaveConfiguredValues = vi.fn().mockResolvedValue(undefined);
    const readable = { generation: 1, record_id: 7, ok: true, active: true, value: "42", error: null };
    renderConfigured({ onSaveConfiguredValues }, [readable]);

    await screen.findAllByTestId(/^cheat-row-/);
    await revealDetails();
    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onSaveConfiguredValues).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Value"), { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(onSaveConfiguredValues).toHaveBeenCalledTimes(2));
    // The parent re-renders nothing while this modal is open, so a second Apply
    // that rebuilt the map from the original prop would revert the first one.
    expect(onSaveConfiguredValues.mock.calls[1][0]).toEqual([{ record_id: 7, value: "200" }]);
  });
});

describe("Durable writes reconcile one mutation at a time", () => {
  function backendRejection(message: string): Error {
    // A Python traceback is the only proof the backend itself refused; anything
    // else is transport loss and has to be reconciled against the profile.
    return Object.assign(new Error(""), {
      name: "Python ValueError",
      pythonTraceback: `Traceback (most recent call last):\nValueError: ${message}`,
    });
  }

  /** A profile that answers with whatever the backend has actually stored. */
  function trackedStatus(initial: { remembered?: any[]; autoload?: boolean; pinned?: number[] } = {}) {
    const state = {
      remembered: initial.remembered ?? [{ record_id: 7, active: true, value: "100" }],
      autoload: initial.autoload ?? true,
      pinned: initial.pinned ?? [],
    };
    api.getStatus.mockImplementation(async () => {
      const snapshot = status(true);
      snapshot.profiles[0].remembered = state.remembered;
      snapshot.profiles[0].autoload_enabled = state.autoload;
      snapshot.profiles[0].pinned = state.pinned;
      return snapshot;
    });
    return state;
  }

  async function openPicker() {
    await screen.findByText("Game.CT");
    await waitFor(() => expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Configure cheats" }));
    await waitFor(() => expect(modalState.nodes.filter((node: any) => node.type === CheatSelectionModal)).toHaveLength(1));
    return modalState.nodes.find((node: any) => node.type === CheatSelectionModal);
  }

  it("keeps the saved selection when only the auto-load half of Apply is refused", async () => {
    // Both writes used to sit behind one commit, so a backend refusal of the
    // second was read as proof the first had not happened either - and the
    // selection the backend was already holding was reported as unsaved.
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    const state = trackedStatus({ autoload: false });
    api.setRememberedCheats.mockImplementation(async (_appId: number, _sha: string, next: any[]) => {
      state.remembered = next;
      return {};
    });
    api.setAutoload.mockRejectedValue(backendRejection("auto-load is not available for this table"));
    renderContent();

    const picker = await openPicker();
    await expect(picker.props.onApplied([{ record_id: 7, active: true, value: "100" }], null))
      .rejects.toBeInstanceOf(PriorDurableCommitError);
    expect(api.setRememberedCheats).toHaveBeenCalledWith(10, SHA, [{ record_id: 7, active: true, value: "100" }]);
    expect(state.remembered).toEqual([{ record_id: 7, active: true, value: "100" }]);
  });

  it("names the already-saved selection in the picker when auto-load is refused", async () => {
    const onApplied = vi.fn().mockRejectedValue(
      new PriorDurableCommitError(backendRejection("auto-load refused"), "The cheat selection for this table"),
    );
    render(<CheatSelectionModal
      appId={10}
      inspection={inspect as any}
      live={false}
      autoloadEnabled={false}
      pinned={[]}
      startupPreferences={[]}
      rememberedPreferences={[]}
      configuredValues={[]}
      onSaveConfiguredValues={vi.fn().mockResolvedValue(undefined)}
      onValidateStartupPlan={vi.fn().mockResolvedValue({ action_count: 1, limit: 2048, fits: true })}
      onTogglePin={vi.fn().mockResolvedValue([])}
      onApplied={onApplied}
      onCancel={vi.fn()}
    />);

    await screen.findAllByTestId(/^cheat-row-/);
    const record = screen.getByTestId("cheat-row-7");
    fireEvent.click(within(record).getByTestId("toggle"));
    // As above: this record takes a value, and Apply refuses one switched on
    // with nothing in it before any of the durable writes under test run.
    fireEvent.change(within(record).getByLabelText("Value"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    expect(await screen.findByText(/Partly applied/)).toBeTruthy();
    expect(await screen.findByText(/saved configuration was written/)).toBeTruthy();
  });

  it("does not report Disable all as failed when only the write receipt is lost", async () => {
    const live = liveRuntime();
    api.getRuntimeStatus.mockResolvedValue(live);
    const state = trackedStatus();
    runtimeClient.deactivateAllActiveControls.mockResolvedValue({
      queried: 1, active: 1, deactivated: 1, deactivatedIds: [7], envelope: live,
    });
    const off = [{ generation: 2, record_id: 7, ok: true, active: false, value: "100", error: null }];
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: live, results: live.status.results, unavailable: [] })
      .mockResolvedValue({ envelope: live, results: off, unavailable: [] });
    // The backend commits, then the reply is lost on the way back.
    api.setRememberedCheats.mockImplementation(async (_appId: number, _sha: string, next: any[]) => {
      state.remembered = next;
      throw new Error("Decky connection lost");
    });
    renderContent();

    await screen.findByText("1 active");
    fireEvent.click(await screen.findByRole("button", { name: "Disable all" }));

    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("Switched off 1 cheat") }),
    ));
    expect(screen.queryByText(/was not remembered/)).toBeNull();
  });

  it("still reports Disable all as unremembered when the profile did not take the write", async () => {
    const live = liveRuntime();
    api.getRuntimeStatus.mockResolvedValue(live);
    trackedStatus();
    runtimeClient.deactivateAllActiveControls.mockResolvedValue({
      queried: 1, active: 1, deactivated: 1, deactivatedIds: [7], envelope: live,
    });
    const off = [{ generation: 2, record_id: 7, ok: true, active: false, value: "100", error: null }];
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: live, results: live.status.results, unavailable: [] })
      .mockResolvedValue({ envelope: live, results: off, unavailable: [] });
    api.setRememberedCheats.mockRejectedValue(new Error("Decky connection lost"));
    renderContent();

    await screen.findByText("1 active");
    fireEvent.click(await screen.findByRole("button", { name: "Disable all" }));

    expect(await screen.findByText(/switched off, but that was not remembered/)).toBeTruthy();
  });

  it("does not report the auto-load switch as failed when only its receipt is lost", async () => {
    const mismatched = liveRuntime();
    mismatched.status.descriptor_sha256 = "9".repeat(64);
    api.getRuntimeStatus.mockResolvedValue(mismatched);
    const state = trackedStatus({ autoload: true });
    api.setAutoload.mockImplementation(async (_appId: number, _sha: string, enabled: boolean) => {
      state.autoload = enabled;
      throw new Error("Decky connection lost");
    });
    renderContent();

    await screen.findByText("Game.CT");
    const toggle = await screen.findByLabelText("Load last table & cheats");
    fireEvent.click(toggle);

    await waitFor(() => expect(state.autoload).toBe(false));
    expect(screen.queryByText("Decky connection lost")).toBeNull();
  });

  it("adopts the profile's pin state when the pin receipt is lost", async () => {
    const state = trackedStatus();
    api.setPinnedControl.mockImplementation(async (_appId: number, _sha: string, recordId: number, pinned: boolean) => {
      state.pinned = pinned ? [...state.pinned, recordId] : state.pinned.filter((id) => id !== recordId);
      throw new Error("Decky connection lost");
    });
    renderContent();

    const picker = await openPicker();
    await expect(picker.props.onTogglePin(7, true)).resolves.toEqual([7]);
  });
});

describe("The live snapshot never overturns a launch", () => {
  /** A game with nothing running yet, whose runtime appears once CE is started. */
  function armedAutoload() {
    let launched = false;
    api.getRuntimeStatus.mockImplementation(async () => (launched ? liveRuntime() : noRuntime()));
    api.launchCEForGame.mockImplementation(async () => {
      launched = true;
      return {
        operation_id: "op", app_id: 10, mode: "attached", state: "connected",
        session_id: "session", message: "connected", error: null,
      };
    });
  }

  it("auto-loads successfully even when the live-state read fails", async () => {
    // The snapshot is read-only and is not what makes a launch succeed. Awaiting
    // it inside the success transaction reported an attached, started session as
    // a failed Auto-load - and one the retry path then refused to retry, because
    // ownership was already visible.
    armedAutoload();
    runtimeClient.queryRuntimeControlsPartial.mockRejectedValue(new Error("bridge read timed out"));
    renderContent();

    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("auto-loaded") }),
    ));
  });

  it("says why the live state is missing instead of implying the session is fine", async () => {
    armedAutoload();
    runtimeClient.queryRuntimeControlsPartial.mockRejectedValue(new Error("bridge read timed out"));
    renderContent();

    expect(await screen.findByText(/^bridge read timed out\. The live cheat state could not be read/)).toBeTruthy();
  });

  it.each(["Search", "Manage"])("publishes first autoload evidence to %s without remounting", async (surface) => {
    armedAutoload();
    api.validateEffectiveStartupPlan.mockResolvedValue({ action_count: 1, limit: 2048, fits: true });
    let confirmed = false;
    const evidence = { app_id: 10, table_sha256: SHA, target_process: "game.exe", pe_version: "1", steam_build_id: null, last_working_at: 1, invalidated: false, state: "matching" };
    api.getStatus.mockImplementation(async () => ({ ...status(true), table_compatibility: { schema: 2, reason: null, entries: confirmed ? [evidence] : [] } }));
    runtimeClient.queryRuntimeControlsPartial.mockImplementation(async () => {
      confirmed = true;
      return { envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] };
    });
    renderContent();
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("auto-loaded") }),
    ));
    fireEvent.click(screen.getByRole("button", { name: surface, exact: true }));
    await waitFor(() => expect(modalState.nodes.length).toBeGreaterThan(0));
    expect(modalState.nodes.at(-1).props.compatibility).toEqual([evidence]);
    expect(api.launchCEForGame).toHaveBeenCalledTimes(1);
  });

  it("keeps autoload successful when compatibility reread fails", async () => {
    armedAutoload();
    runtimeClient.queryRuntimeControlsPartial.mockImplementation(async () => {
      api.getStatus.mockRejectedValue(new Error("advisory status unavailable"));
      return { envelope: liveRuntime(), results: liveRuntime().status.results, unavailable: [] };
    });
    renderContent();
    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("auto-loaded") }),
    ));
    expect(screen.queryByText("advisory status unavailable")).toBeNull();
    expect(api.launchCEForGame).toHaveBeenCalledTimes(1);
  });

  it("still auto-loads and shows the live state when the read succeeds", async () => {
    armedAutoload();
    renderContent();

    await waitFor(() => expect(decky.toast).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.stringContaining("auto-loaded") }),
    ));
    expect(await screen.findByText("1 active")).toBeTruthy();
  });
});

describe("Advanced profile writes reconcile a lost receipt", () => {
  /** A profile that answers with whatever the backend has actually stored. */
  function trackedStatus(initial: { startup?: any[]; consent?: string | null } = {}) {
    const state = {
      startup: initial.startup ?? [{ record_id: 7, active: true, value: "100" }],
      consent: initial.consent === undefined ? SHA : initial.consent,
    };
    api.getStatus.mockImplementation(async () => {
      const snapshot = status(true);
      snapshot.profiles[0].startup = state.startup;
      snapshot.profiles[0].execution_consent_sha256 = state.consent;
      return snapshot;
    });
    return state;
  }

  async function openAdvanced() {
    await screen.findByText("Game.CT");
    fireEvent.click(await screen.findByRole("button", { name: "Advanced…" }));
    return waitFor(() => {
      const node = modalState.nodes.find((item: any) => item.type === AdvancedModal);
      expect(node).toBeTruthy();
      return node;
    });
  }

  it("reports the startup actions as cleared when the profile confirms it", async () => {
    const state = trackedStatus();
    api.clearStartupPreference.mockImplementation(async () => {
      state.startup = [];
      throw new Error("Decky connection lost");
    });
    renderContent();

    const advanced = await openAdvanced();
    await expect(advanced.props.onClearStartup()).resolves.toBe(0);
  });

  it("still fails when the profile still holds the startup actions", async () => {
    trackedStatus();
    api.clearStartupPreference.mockRejectedValue(new Error("Decky connection lost"));
    renderContent();

    const advanced = await openAdvanced();
    await expect(advanced.props.onClearStartup()).rejects.toThrow("Decky connection lost");
  });

  it("does not report a withdrawn authorization as a failed revocation", async () => {
    const state = trackedStatus();
    api.getCELaunchCapability.mockResolvedValue({ ...launch, recovered: null, operations: [] });
    api.revokeTable.mockImplementation(async () => {
      const detached = status(true) as any;
      Object.assign(detached.profiles[0], { table_sha256: null, execution_consent_sha256: null,
        autoload_enabled: false, table_history: { [SHA]: { execution_consent: false } } });
      api.getStatus.mockResolvedValue(detached);
      throw new Error("Decky connection lost");
    });
    api.stopCEForGame.mockResolvedValue({ stopped: false, recovered: false });
    renderContent();

    const advanced = await openAdvanced();
    await advanced.props.onRevokeConsent();
    expect(decky.toast).not.toHaveBeenCalledWith(
      expect.objectContaining({ body: "Decky connection lost" }),
    );
  });

  it("does not stop CE for a replacement table through a stale Revoke callback", async () => {
    trackedStatus();
    renderContent();
    const advanced = await openAdvanced();
    const replacement = status(true);
    replacement.profiles[0].table_sha256 = "9".repeat(64);
    replacement.profiles[0].execution_consent_sha256 = "9".repeat(64);
    api.getStatus.mockResolvedValue(replacement);
    api.stopCEForGame.mockClear();
    api.revokeTable.mockClear();
    await advanced.props.onRevokeConsent().catch(() => undefined);
    await waitFor(() => expect(failureShown()).toContain("The selected table changed"));
    expect(api.stopCEForGame).not.toHaveBeenCalled();
    expect(api.revokeTable).not.toHaveBeenCalled();
  });

  it("does not accept another table's profile as proof this one was revoked", async () => {
    // A switch from this table to another between the failed write and the
    // verification leaves the new table's consent field trivially different
    // from this SHA. Meanwhile this table's own consent was archived into
    // `table_history` and is restored the moment it is selected again, so
    // accepting that as a withdrawal reports an authorization that comes back.
    const OTHER = "9".repeat(64);
    let switched = false;
    api.getStatus.mockImplementation(async () => {
      const snapshot = status(true);
      if (switched) {
        snapshot.profiles[0].table_sha256 = OTHER;
        snapshot.profiles[0].execution_consent_sha256 = OTHER;
      }
      return snapshot;
    });
    api.getCELaunchCapability.mockResolvedValue({ ...launch, recovered: null, operations: [] });
    api.revokeTable.mockImplementation(async () => {
      switched = true;
      throw new Error("Decky connection lost");
    });
    api.stopCEForGame.mockResolvedValue({ stopped: false, recovered: false });
    renderContent();

    const advanced = await openAdvanced();
    await advanced.props.onRevokeConsent().catch(() => undefined);
    await waitFor(() => expect(failureShown()).toBe("Decky connection lost"));
  });

  it("still fails when the profile still carries execution consent", async () => {
    trackedStatus();
    api.getCELaunchCapability.mockResolvedValue({ ...launch, recovered: null, operations: [] });
    api.revokeTable.mockRejectedValue(new Error("Decky connection lost"));
    api.stopCEForGame.mockResolvedValue({ stopped: false, recovered: false });
    renderContent();

    const advanced = await openAdvanced();
    await advanced.props.onRevokeConsent().catch(() => undefined);
    await waitFor(() => expect(failureShown()).toBe("Decky connection lost"));
  });
});

describe("Unknown durable outcomes are never stated as definite", () => {
  it("does not claim Disable all was not remembered when it cannot tell", async () => {
    const live = liveRuntime();
    api.getRuntimeStatus.mockResolvedValue(live);
    runtimeClient.deactivateAllActiveControls.mockResolvedValue({
      queried: 1, active: 1, deactivated: 1, deactivatedIds: [7], envelope: live,
    });
    const off = [{ generation: 2, record_id: 7, ok: true, active: false, value: "100", error: null }];
    runtimeClient.queryRuntimeControlsPartial
      .mockResolvedValueOnce({ envelope: live, results: live.status.results, unavailable: [] })
      .mockResolvedValue({ envelope: live, results: off, unavailable: [] });
    // Neither the write nor the authority that would settle it can be reached.
    api.setRememberedCheats.mockRejectedValue(new Error("Decky connection lost"));
    let statusReads = 0;
    api.getStatus.mockImplementation(async () => {
      statusReads += 1;
      if (statusReads > 1) throw new Error("status unavailable");
      return status(true);
    });
    renderContent();

    await screen.findByText("1 active");
    fireEvent.click(await screen.findByRole("button", { name: "Disable all" }));

    const message = await screen.findByText(/could not confirm whether/);
    expect(message.textContent).toContain("The cheats were switched off.");
    expect(message.textContent).not.toContain("was not remembered");
  });
});

describe("Managed data removal", () => {
  const DIRECTORIES = [
    { key: "tables", label: "Tables", path: "/managed/tables", purpose: "Tables.", exists: true, file_count: 2, total_bytes: 2048, truncated: false, error: null },
    { key: "ce", label: "Cheat Engine", path: "/managed/ce", purpose: "CE.", exists: true, file_count: 9000, total_bytes: 4_000_000_000, truncated: false, error: null },
    { key: "state", label: "Profiles and sessions", path: "/managed/state", purpose: "Profiles.", exists: true, file_count: 80, total_bytes: 4096, truncated: false, error: null },
    { key: "cache", label: "Provider cache", path: "/managed/cache", purpose: "Cache.", exists: true, file_count: 4, total_bytes: 8192, truncated: false, error: null },
    { key: "tmp", label: "Staging", path: "/managed/tmp", purpose: "Staging.", exists: false, file_count: 0, total_bytes: 0, truncated: false, error: null },
    { key: "settings", label: "Decky settings", path: "/settings", purpose: "Settings.", exists: true, file_count: 1, total_bytes: 488, truncated: false, error: null },
    { key: "logs", label: "Logs", path: "/logs", purpose: "Logs.", exists: true, file_count: 5, total_bytes: 3100, truncated: false, error: null },
  ];

  function readiness(overrides: Record<string, any> = {}) {
    return {
      directories: DIRECTORIES, profiles_total: 1, current_session_app_ids: [], live_owned_launch: false,
      session_errors: [], session_corrupt_entries: 0, profile_state_error: null, session_inventory_error: null,
      blockers: [], can_delete_managed_data: true, managed_root: "/managed", requires_target_validation: true,
      ...overrides,
    };
  }

  function renderAdvanced(overrides: Record<string, any> = {}) {
    return render(<AdvancedModal
      status={status(true)} games={[game]} selectedGame={game} appDetails={details} inspection={null}
      targetProcess="game.exe" ceLaunch={launch} launchProtonToolId="" runtime={noRuntime()} selfTest={null}
      busy={false} onRefreshGames={vi.fn()} onSaveTargetProcess={vi.fn()} onPickCE={vi.fn()}
      onClearCEImport={vi.fn()} onRunSelfTest={vi.fn()} onLaunchProtonChange={vi.fn()}
      onRunCELaunchSelfTest={vi.fn()} onRefreshRuntime={vi.fn()} onRefreshProcesses={vi.fn()}
      onRetryAttach={vi.fn()} onRepairSessionState={vi.fn()} onRepairOwnedLaunchState={vi.fn()}
      onRepairProfileState={vi.fn()} onClearStartup={vi.fn()} onRevokeConsent={vi.fn()}
      onLoadDiagnostics={vi.fn()} onRefreshAll={vi.fn()} onClose={vi.fn()}
      onCheckRemoval={vi.fn().mockResolvedValue(readiness())}
      onDeleteManagedData={vi.fn()}
      {...overrides}
    />);
  }

  /** Answer the mandatory confirmation Steam's own dialog puts in the way. */
  function confirmDeletion() {
    const node = modalState.nodes.filter((item: any) => item.type === DeckyConfirmModal).pop();
    expect(node).toBeTruthy();
    render(node);
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
  }

  async function openRemoval(overrides: Record<string, any> = {}) {
    const view = renderAdvanced(overrides);
    fireEvent.click(screen.getByRole("button", { name: "Check" }));
    await screen.findByTestId("removal-summary");
    return view;
  }

  it("never deletes on the header press alone", async () => {
    const onDeleteManagedData = vi.fn();
    await openRemoval({ onDeleteManagedData });

    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    // The press opens the choice; nothing is removed until it is confirmed.
    expect(onDeleteManagedData).not.toHaveBeenCalled();
    expect(await screen.findByTestId("delete-scope-summary")).toBeTruthy();
  });

  it("deletes nothing until the confirmation is answered", async () => {
    // Choosing a scope is not consenting to it. Without this the destructive
    // press was the same press that selected what to destroy.
    const onDeleteManagedData = vi.fn();
    await openRemoval({ onDeleteManagedData });

    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete these" }));
    expect(onDeleteManagedData).not.toHaveBeenCalled();

    const node = modalState.nodes.filter((item: any) => item.type === DeckyConfirmModal).pop();
    expect(node).toBeTruthy();
    // The dialog names the consequence, not just the scope.
    render(node);
    expect(screen.getByText(/cannot be undone/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(onDeleteManagedData).not.toHaveBeenCalled();
  });

  it("names exactly what each scope removes before it is confirmed", async () => {
    await openRemoval();
    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));

    // Cache scope: the disposable three, and none of the user's own data.
    const cacheSummary = await screen.findByTestId("delete-scope-summary");
    expect(cacheSummary.textContent).toContain("9 file(s)");
    expect(screen.getByTestId("delete-directory-cache")).toBeTruthy();
    expect(screen.queryByTestId("delete-directory-tables")).toBeNull();
    expect(screen.queryByTestId("delete-directory-ce")).toBeNull();

    fireEvent.change(screen.getByLabelText("What to delete"), { target: { value: "all" } });
    expect(screen.getByTestId("delete-directory-tables")).toBeTruthy();
    expect(screen.getByTestId("delete-directory-ce")).toBeTruthy();
    // The consequence is behind the row's own question mark: it is two or three
    // lines of prose read once, and this window has to fit a directory row for
    // every place the widest scope touches. It is still one press away, and it
    // is still what the confirmation names.
    fireEvent.click(within(screen.getByTestId("delete-scope-summary")).getByRole("button", { name: "?" }));
    expect(screen.getByTestId("delete-scope-summary").parentElement?.textContent).toContain("first-run state");
  });

  it("passes the confirmed scope and adopts the report the backend returns", async () => {
    const after = readiness({ directories: DIRECTORIES.map((item) => ({ ...item, file_count: 0, total_bytes: 0, exists: false })) });
    const onDeleteManagedData = vi.fn().mockResolvedValue({
      scope: "all",
      deleted: [{ key: "ce", label: "Cheat Engine", removed_files: 9000, removed_bytes: 4_000_000_000, error: null }],
      failed: [],
      readiness: after,
    });
    await openRemoval({ onDeleteManagedData });

    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    fireEvent.change(await screen.findByLabelText("What to delete"), { target: { value: "all" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete these" }));
    confirmDeletion();

    await waitFor(() => expect(onDeleteManagedData).toHaveBeenCalledWith("all"));
    expect(await screen.findByTestId("removal-deleted")).toBeTruthy();
  });

  it("keeps everything and says why when the backend refuses", async () => {
    const onDeleteManagedData = vi.fn().mockRejectedValue(new Error("a Cheat Engine process CE Decky owns is still running; stop it first"));
    await openRemoval({ onDeleteManagedData });

    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete these" }));
    confirmDeletion();

    expect(await screen.findByTestId("delete-error")).toBeTruthy();
    expect(screen.getByTestId("delete-error").textContent).toContain("still running");
  });

  it("rechecks removal after a failed deletion even while the deletion handler holds its latch", async () => {
    const onCheckRemoval = vi.fn().mockResolvedValue(readiness());
    await openRemoval({ onCheckRemoval, onDeleteManagedData: vi.fn().mockRejectedValue(new Error("directory sync failed")) });
    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete these" }));
    confirmDeletion();
    await screen.findByTestId("delete-error");
    await waitFor(() => expect(onCheckRemoval).toHaveBeenCalledTimes(2));
  });

  it("says what a partly failed deletion did remove, not that nothing happened", async () => {
    // The rest of a confirmed deletion really happened. Reporting it as nothing
    // is the same untruth the durable-write reconciliation exists to stop.
    const onDeleteManagedData = vi.fn().mockResolvedValue({
      scope: "cache",
      deleted: [
        { key: "cache", label: "Provider cache", removed_files: 4, removed_bytes: 8192, error: null },
        { key: "logs", label: "Logs", removed_files: 0, removed_bytes: 0, error: "permission denied" },
      ],
      failed: [{ key: "logs", label: "Logs", removed_files: 0, removed_bytes: 0, error: "permission denied" }],
      readiness: readiness(),
    });
    const onRefreshAll = vi.fn().mockResolvedValue(undefined);
    await openRemoval({ onDeleteManagedData, onRefreshAll });

    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete these" }));
    confirmDeletion();

    const row = await screen.findByTestId("removal-deleted");
    expect(row.textContent).toContain("Partly deleted");
    expect(row.textContent).toContain("4 file(s)");
    expect(row.textContent).toContain("Logs could not be cleared");
  });

  it("catches the rest of the panel up after data it was describing is gone", async () => {
    const onRefreshAll = vi.fn().mockResolvedValue(undefined);
    const onDeleteManagedData = vi.fn().mockResolvedValue({
      scope: "all", deleted: [], failed: [], readiness: readiness(),
    });
    await openRemoval({ onDeleteManagedData, onRefreshAll });

    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete these" }));
    confirmDeletion();

    await waitFor(() => expect(onRefreshAll).toHaveBeenCalled());
  });

  it("offers no deletion at all while an owned Cheat Engine is running", async () => {
    // The tree being deleted is what that process is executing out of.
    await openRemoval({ onCheckRemoval: vi.fn().mockResolvedValue(readiness({ live_owned_launch: true, can_delete_managed_data: false, blockers: ["a Cheat Engine process CE Decky owns is still running; stop it first"] })) });
    expect((screen.getByRole("button", { name: "Delete…" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("still offers deletion when the only problem is corrupt state", async () => {
    // Corrupt profile state makes the plugin untidy to uninstall, and is a
    // reason to delete rather than a reason to refuse.
    await openRemoval({ onCheckRemoval: vi.fn().mockResolvedValue(readiness({ can_delete_managed_data: false, blockers: ["profile state is corrupt or unreadable"] })) });
    expect((screen.getByRole("button", { name: "Delete…" }) as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("Cheat Engine acquisition and its provenance", () => {
  function capability(overrides: Record<string, any> = {}) {
    return {
      schema: 3, mode: "managed_install", managed_install_available: true, release_manifest_loaded: true,
      network_download_enabled: true, native_extraction_enabled: true, reason: "ready",
      release: {
        visible_version: "7.7", artifact_filename: "CheatEngine77.exe", sha256: "b".repeat(64),
        size: 34690856, reviewed_at: "2026-08-22", rediscovery_available: true,
      },
      operation: null,
      ...overrides,
    };
  }

  function operation(state: string, error: string | null = null) {
    return {
      operation_id: "op", state, progress: null,
      message: state === "failed" ? "Managed Cheat Engine installation failed" : "Downloading",
      error, installed: null, provenance: null,
    };
  }

  function provenance(overrides: Record<string, any> = {}) {
    return {
      schema: 1, source: "reviewed-url", verified_by: "reviewed SHA-256",
      artifact_sha256: "cf0f4b6002555677984233c95856683959be83a5def3dc12175a8296eb22676b",
      artifact_bytes: 34690856, origin_host: "d1dj9aohuk02ls.cloudfront.net",
      helper_host: null, helper_format: null, reviewed_at: "2026-08-22",
      reviewed_subject: "CN=Cheat Engine EZ, O=Cheat Engine EZ, C=NL",
      signature_subject: null, signature_common_name: null, signature_key_sha256: null,
      signature_digest: null, signature_note: null,
      ...overrides,
    };
  }

  function managedStatus(ce: Record<string, any> = {}) {
    const snapshot = status(true);
    Object.assign(snapshot.ce, { managed: true, provenance: provenance() }, ce);
    return snapshot;
  }

  function lastConfirm() {
    return modalState.nodes.filter((item: any) => item.type === DeckyConfirmModal).pop();
  }

  /** The dialog's rendered body, so its structure can be read as well as its text. */
  function confirmBody(): HTMLElement {
    const node = lastConfirm();
    expect(node).toBeTruthy();
    return render(<div>{node.props.strDescription}</div>).container;
  }

  function confirmText(): string {
    return confirmBody().textContent ?? "";
  }

  it("shows the version the installed executable declares, not the manifest's label", async () => {
    // A rediscovered artifact can be a newer release than the packaged manifest
    // names, and saying 7.7 while 7.8 is installed would be a plain lie.
    api.getStatus.mockResolvedValue(managedStatus({ version: "7.8" }));
    api.getManagedCECapability.mockResolvedValue(capability());
    renderContent();

    expect(await screen.findByText("Cheat Engine 7.8 · Ready")).toBeTruthy();
  });

  it("still labels a Cheat Engine that declares no version at all", async () => {
    api.getStatus.mockResolvedValue(managedStatus({ version: null }));
    api.getManagedCECapability.mockResolvedValue(capability());
    renderContent();

    expect(await screen.findByText("Cheat Engine 7.7 · Ready")).toBeTruthy();
  });

  it("explains how to finish setup by hand when no download route worked", async () => {
    api.getStatus.mockResolvedValue(status(false, false));
    api.getManagedCECapability.mockResolvedValue(capability());
    api.startManagedCEInstall.mockResolvedValue(operation("downloading"));
    api.pollManagedCEInstall.mockResolvedValue(operation(
      "failed",
      "The reviewed Cheat Engine download link no longer works, and the replacement found on cheatengine.org could not be proven to come from Cheat Engine's own publisher, so it was discarded.",
    ));
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();

    await waitFor(() => expect((screen.getByRole("button", { name: "Download and install CE" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Download and install CE" }));

    await waitFor(() => expect(lastConfirm()).toBeTruthy(), { timeout: 4000 });
    expect(lastConfirm().props.strTitle).toBe("Cheat Engine could not be downloaded");
    const text = confirmText();
    expect(text).toContain("could not be proven to come from Cheat Engine's own publisher");
    expect(text).toContain("Nothing was installed");
    // The way out has to be the one the user can actually take.
    expect(text).toContain("Import it from");
    expect(text).toContain(".zip");
    // The cause, the reassurance and the way out are separate paragraphs. This
    // renders as HTML, where a "\n\n" between them would simply collapse.
    expect(confirmBody().querySelectorAll("p").length).toBe(3);
    expect(modalState.nodes).toHaveLength(1);
    expect(modalState.nodes.some((node) => node.type === ActionFailureModal)).toBe(false);
  }, 8000);

  it("offers manual recovery when setup itself could not be started", async () => {
    api.getStatus.mockResolvedValue(status(false, false));
    api.getManagedCECapability.mockResolvedValue(capability());
    api.startManagedCEInstall.mockRejectedValue(new Error("managed CE release manifest is unavailable"));
    steam.listRunningGames.mockResolvedValue({ available: true, games: [] });
    renderContent();

    await waitFor(() => expect((screen.getByRole("button", { name: "Download and install CE" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Download and install CE" }));

    await waitFor(() => expect(lastConfirm()).toBeTruthy());
    expect(confirmText()).toContain("managed CE release manifest is unavailable");
    expect(modalState.nodes).toHaveLength(1);
    expect(modalState.nodes.some((node) => node.type === ActionFailureModal)).toBe(false);
  });

  function renderDiagnostics(ce: Record<string, any>) {
    const snapshot = status(true);
    Object.assign(snapshot.ce, ce);
    return render(<AdvancedModal
      status={snapshot} games={[game]} selectedGame={game} appDetails={details} inspection={null}
      targetProcess="game.exe" ceLaunch={launch} launchProtonToolId="" runtime={noRuntime()} selfTest={null}
      busy={false} onRefreshGames={vi.fn()} onSaveTargetProcess={vi.fn()} onPickCE={vi.fn()}
      onClearCEImport={vi.fn()} onRunSelfTest={vi.fn()} onLaunchProtonChange={vi.fn()}
      onRunCELaunchSelfTest={vi.fn()} onRefreshRuntime={vi.fn()} onRefreshProcesses={vi.fn()}
      onRetryAttach={vi.fn()} onRepairSessionState={vi.fn()} onRepairOwnedLaunchState={vi.fn()}
      onRepairProfileState={vi.fn()} onClearStartup={vi.fn()} onRevokeConsent={vi.fn()}
      onLoadDiagnostics={vi.fn()} onRefreshAll={vi.fn()} onClose={vi.fn()}
      onCheckRemoval={vi.fn()} onDeleteManagedData={vi.fn()}
    />);
  }

  it("states that an ordinary install came from the reviewed link", () => {
    renderDiagnostics({ managed: true, provenance: provenance() });
    expect(screen.getByTestId("ce-provenance-source").textContent)
      .toContain("Downloaded by CE Decky from the reviewed link");
    expect(screen.getByTestId("ce-provenance-artifact").textContent)
      .toContain("d1dj9aohuk02ls.cloudfront.net");
    // What vouched for it is no longer a row: the digest being the reviewed one
    // is why anything was installed, so the verdict restated the artifact.
    expect(screen.queryByTestId("ce-provenance-proof")).toBeNull();
  });

  it("shows the whole installer digest to whoever asks the row for it", () => {
    // The row shows twelve characters, which is an identity to recognise and
    // not one to check. Asking it what it means gave a paragraph that did not
    // contain the hash at all, so the full one was on no screen anywhere.
    renderDiagnostics({ managed: true, provenance: provenance() });

    const artifact = screen.getByTestId("ce-provenance-artifact");
    expect(artifact.textContent).not.toContain("cf0f4b6002555677984233c95856683959be83a5def3dc12175a8296eb22676b");
    fireEvent.click(within(artifact).getByRole("button", { name: "?" }));
    expect(artifact.textContent).toContain("cf0f4b6002555677984233c95856683959be83a5def3dc12175a8296eb22676b");
  });

  it("states the rediscovery nuance and what proved the artifact", () => {
    // Rediscovery succeeds silently on purpose - a working setup should not
    // interrupt anyone - so this screen is where it gets said.
    renderDiagnostics({
      managed: true,
      provenance: provenance({
        source: "rediscovered",
        helper_host: "d2wwjzpygc2eff.cloudfront.net",
        helper_format: "Inno Setup Setup Data (6.7.0)",
        signature_common_name: "Cheat Engine EZ", signature_digest: "sha1",
        signature_key_sha256: "c6b1fd6c80b1687e348bf4d08bfc5234ea793f4e832c112b7774a4b959ba98c1",
      }),
    });

    expect(screen.getByTestId("ce-provenance-source").textContent)
      .toContain("using the current cheatengine.org link");
    // The reviewed hash is the authority on both routes; the signature is
    // corroboration, never a second way in, so it is what the artifact row
    // says when it is asked rather than a verdict of its own.
    const artifact = screen.getByTestId("ce-provenance-artifact");
    fireEvent.click(within(artifact).getByRole("button", { name: "?" }));
    expect(artifact.textContent).toContain("signed by Cheat Engine EZ");
    expect(artifact.textContent).toContain("only thing that authorizes an install");
    expect(screen.getByTestId("ce-provenance-helper").textContent)
      .toContain("d2wwjzpygc2eff.cloudfront.net");
  });

  it("does not claim the reviewed link for a cache entry with no route recorded", () => {
    renderDiagnostics({ managed: true, provenance: provenance({ source: "cache" }) });
    expect(screen.getByTestId("ce-provenance-source").textContent)
      .toContain("the link it used was not recorded");
  });

  it("says when the extra signature check could not run beside a hash that settled it", () => {
    // The note only ever exists on the rediscovered route, so a condition that
    // hid it there hid it everywhere.
    renderDiagnostics({
      managed: true,
      provenance: provenance({
        source: "rediscovered", verified_by: "reviewed SHA-256",
        helper_host: "d2wwjzpygc2eff.cloudfront.net",
        signature_note: "openssl is unavailable, so the signature cannot be checked",
      }),
    });
    expect(screen.getByTestId("ce-provenance-note").textContent)
      .toContain("openssl is unavailable");
  });

  it("never prints a working download link on the diagnostics screen", () => {
    renderDiagnostics({
      managed: true,
      provenance: provenance({ source: "rediscovered", helper_host: "helper.example" }),
    });
    expect(document.body.textContent).not.toContain("https://");
  });

  it("says an imported Cheat Engine has no download provenance to show", () => {
    renderDiagnostics({ managed: false, provenance: null });
    expect(screen.getByTestId("ce-provenance-source").textContent).toContain("Imported by you");
  });

  it("does not invent provenance for an installation promoted before it was recorded", () => {
    renderDiagnostics({ managed: true, provenance: null });
    expect(screen.getByTestId("ce-provenance-source").textContent)
      .toContain("before it recorded how");
  });
});

describe("running-game detector logging", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("logs a failure once, its recovery once, and a later failure again", async () => {
    // The latch exists because this runs every three seconds and a line per
    // tick would bury the ring buffer. A latch that is raised and never cleared
    // is worse than no latch at all: it records the first failure and then
    // silences every real one after it, which is what happened while the clear
    // sat below the early return that the ordinary one-game case takes.
    const { resetSupportLog, readSupportLog } = await import("../src/supportLog");
    resetSupportLog();
    api.getStatus.mockResolvedValue(status(true));
    steam.listRunningGames
      .mockRejectedValueOnce(new Error("Steam observation unavailable"))
      .mockResolvedValue({ available: true, games: [game] });

    renderContent();

    await waitFor(() => expect(
      readSupportLog().entries.filter((entry) => entry.event === "games.detection_failed"),
    ).toHaveLength(1));

    // The second pass succeeds with exactly one game running, which is the
    // path that returns before the end of the detector.
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    await waitFor(() => expect(
      readSupportLog().entries.filter((entry) => entry.event === "games.detection_recovered"),
    ).toHaveLength(1));

    // And a later failure is reported rather than swallowed by a latch that
    // was never lowered.
    steam.listRunningGames.mockRejectedValue(new Error("Steam observation lost again"));
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    await waitFor(() => expect(
      readSupportLog().entries.filter((entry) => entry.event === "games.detection_failed"),
    ).toHaveLength(2));
  });
});

describe("cheat picker initial query logging", () => {
  const pickerProps = () => ({
    appId: 10, inspection: inspect as any, live: true, liveUnavailableReason: null,
    autoloadEnabled: false, pinned: [], startupPreferences: [], rememberedPreferences: [],
    configuredValues: [], onApplied: vi.fn(), onTogglePin: vi.fn(), onSaveConfiguredValues: vi.fn(),
    onTableRefused: vi.fn(), onValidateStartupPlan: vi.fn(), onSnapshot: vi.fn(),
    onSnapshotInvalidated: vi.fn(), onCancel: vi.fn(),
  });

  it("does not record closing the picker mid-query as a failure", async () => {
    // The query owns an AbortController and the cleanup aborts it, so the
    // runtime client throws between batches. That is the user doing what the
    // control is for, and the panel's own boundaries already treat it as such.
    const { resetSupportLog, readSupportLog } = await import("../src/supportLog");
    resetSupportLog();
    runtimeClient.queryRuntimeControlsPartial.mockRejectedValue(
      new runtimeClient.RuntimeQueryAbortedError("aborted"),
    );

    const view = render(<CheatSelectionModal {...(pickerProps() as any)} />);
    await waitFor(() => expect(runtimeClient.queryRuntimeControlsPartial).toHaveBeenCalled());
    view.unmount();

    await waitFor(() => expect(
      readSupportLog().entries.filter((entry) => entry.event === "cheats.initial_query_failed"),
    ).toHaveLength(0));
  });

  it("records a real initial-query failure", async () => {
    const { resetSupportLog, readSupportLog } = await import("../src/supportLog");
    resetSupportLog();
    runtimeClient.queryRuntimeControlsPartial.mockRejectedValue(new Error("session identity did not match"));

    render(<CheatSelectionModal {...(pickerProps() as any)} />);

    await waitFor(() => expect(
      readSupportLog().entries.filter((entry) => entry.event === "cheats.initial_query_failed"),
    ).toHaveLength(1));
  });
});


describe("Manage catalogue readback", () => {
  it("reopens unavailable entries and keeps every profile holding one SHA", async () => {
    const snapshot = status(true);
    const missing = { ...table, sha256: "b".repeat(64), filename: "Missing.CT", available: false };
    const elsewhere = { ...missing, sha256: "c".repeat(64), filename: "Elsewhere.CT" };
    snapshot.tables.push(missing, elsewhere);
    snapshot.profiles[0].table_library.push(missing.sha256);
    snapshot.profiles.push({ ...snapshot.profiles[0], app_id: 20, name: "Second Game" });
    snapshot.profiles.push({ ...snapshot.profiles[0], app_id: 30, name: "Third Game" });
    api.getStatus.mockResolvedValue(snapshot);
    renderContent();
    await screen.findByText("Game.CT");
    for (let opening = 0; opening < 2; opening++) {
      fireEvent.click(screen.getByRole("button", { name: "Manage" }));
      const picker = [...modalState.nodes].reverse().find((node: any) => node?.props?.onOpenLocalFile);
      expect(picker.props.tables.map((item: any) => item.sha256)).toContain(missing.sha256);
      expect(picker.props.otherTables.map((item: any) => item.sha256)).toContain(elsewhere.sha256);
      expect(picker.props.selectedBy[SHA]).toEqual({ names: ["Game", "Second Game", "Third Game"], count: 3 });
      await act(async () => { picker.props.onClose(); });
    }
  });
});


it("passes bounded holder display data to Manage for 10000 long profile names", async () => {
  const snapshot = status(true);
  snapshot.profiles = Array.from({ length: 10_000 }, (_, i) => ({
    ...snapshot.profiles[0], app_id: i + 10, name: `${i}`.padEnd(1024, "x"),
  }));
  api.getStatus.mockResolvedValue(snapshot);
  renderContent();
  await screen.findByText("Game.CT");
  fireEvent.click(screen.getByRole("button", { name: "Manage" }));
  const picker = [...modalState.nodes].reverse().find((node: any) => node?.props?.onOpenLocalFile);
  const holders = picker.props.selectedBy[SHA];
  expect(holders.count).toBe(10_000);
  expect(holders.names).toHaveLength(3);
  expect(JSON.stringify(holders).length).toBeLessThan(220);
});


describe("Compatibility survives a later preference refusal", () => {
  // `reported` is what `confirm_table_working` told the client. False stands for
  // the post-commit-uncertain write: the evidence is already in the backend and
  // the RPC still refused to claim it, which is the case that used to leave
  // Search and Manage showing the previous snapshot until they were remounted.
  it.each([["picker", true], ["pinned", true], ["picker", false], ["pinned", false]] as const)(
      "converges Search and Manage after %s activation and refused remembering, reported proof %s", async (surface, reported) => {
    api.validateEffectiveStartupPlan.mockResolvedValue({ action_count: 1, limit: 2048, fits: true });
    let confirmed = false;
    const evidence = { app_id: 10, table_sha256: SHA, target_process: "game.exe", pe_version: "1", steam_build_id: null, last_working_at: 1, invalidated: false, state: "matching" };
    api.getStatus.mockImplementation(async () => {
      const snapshot = status(true);
      snapshot.profiles[0].autoload_enabled = false;
      snapshot.profiles[0].pinned = [7];
      return { ...snapshot, table_compatibility: { schema: 2, reason: null, entries: confirmed ? [evidence] : [] } };
    });
    runtimeClient.queryRuntimeControlsPartial.mockImplementation(async () => ({
      envelope: liveRuntime(), results: [{ ...liveRuntime().status.results[0], active: confirmed }], unavailable: [],
    }));
    runtimeClient.applyRuntimeSelection.mockImplementation(async () => {
      confirmed = true;
      return {
        envelope: liveRuntime(), results: liveRuntime().status.results,
        compatibilityConfirmed: reported, compatibilityMayHaveChanged: true,
      };
    });
    api.setRememberedCheats.mockRejectedValue(Object.assign(new Error("preference refused"), {
      name: "Python ValueError", pythonTraceback: "Traceback (most recent call last):\nValueError: preference refused",
    }));
    renderContent();
    await screen.findByText("Game.CT");
    let pickerView: ReturnType<typeof render> | undefined;
    let picker: any;
    if (surface === "picker") {
      fireEvent.click(await screen.findByRole("button", { name: "Configure cheats" }));
      picker = await waitFor(() => {
        const node = modalState.nodes.find((node: any) => node.type === CheatSelectionModal);
        expect(node).toBeTruthy();
        return node;
      });
      pickerView = render(picker);
      fireEvent.click(within(await screen.findByTestId("cheat-row-7")).getByTestId("toggle"));
      fireEvent.click(screen.getByRole("button", { name: "Apply", exact: true }));
    } else {
      fireEvent.click(within(await screen.findByTestId("pinned-cheat-7")).getByTestId("toggle"));
    }
    await waitFor(() => expect(api.setRememberedCheats).toHaveBeenCalled());
    await waitFor(() => expect(failureShown()).toContain("preference refused"));
    pickerView?.unmount();
    if (picker) await act(async () => picker.props.onCancel());
    const failure = modalState.nodes.find((node: any) => node.type === ActionFailureModal);
    await act(async () => failure.props.onClose());
    for (const name of ["Search", "Manage"]) {
      fireEvent.click(await screen.findByRole("button", { name, exact: true }));
      expect(modalState.nodes.at(-1).props.compatibility).toEqual([evidence]);
      await act(async () => {
        const props = modalState.nodes.at(-1).props;
        (props.onCancel ?? props.onClose)();
      });
    }
  });
});
