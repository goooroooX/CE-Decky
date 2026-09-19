import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ButtonItem: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  DialogButton: ({ children, onClick, disabled, style }: any) => (
    <button disabled={disabled} data-background={style?.background} onClick={onClick}>{children}</button>
  ),
  Focusable: ({ children, onActivate }: any) => <div data-testid="focusable" onClick={() => onActivate?.(new CustomEvent("activate"))}>{children}</div>,
  ModalRoot: ({ children, onCancel }: any) => <div role="dialog"><button onClick={() => onCancel?.()}>controller-back</button>{children}</div>,
  PanelSection: ({ title, children }: any) => <section aria-label={title || "section"}>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  Spinner: () => <span role="progressbar">Loading</span>,
  ToggleField: ({ label, description, checked, onChange, disabled }: any) => (
    <label>{label}<input aria-label={label} type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange?.(event.target.checked)} />{description}</label>
  ),
}));

import { HomePanel } from "../src/components/HomePanel";
import { UpdateModal } from "../src/modals/UpdateModal";
import { panelUpdateOffer, updateSummary } from "../src/uiModel";
import type { PluginUpdateOperation, PluginUpdateState } from "../src/types";

const game = { appId: 10, name: "Game", sortAs: "Game", isShortcut: false };
const table = {
  sha256: "1".repeat(64), filename: "Game.CT", size: 100, table_version: "45", has_lua: false,
  has_auto_assembler: false, has_embedded_files: false, executable_content: false, entry_count: 20,
  blob_path: "/managed/Game.CT", available: true, schema_version: 2, origins: [],
};

function props(overrides: Record<string, unknown> = {}) {
  return {
    pluginVersion: "v0.9.27", updateVersion: null, onUpdate: vi.fn(), mascotVisible: true,
    ceReady: true, ceStatusText: "Cheat Engine 7.7 · Ready",
    installAvailable: true, installBusy: false, setupPending: false, setupStatusError: null,
    onRetrySetupStatus: vi.fn(), installOperation: null,
    ceSource: "Managed" as const, ceSha256: "a".repeat(64), onInstall: vi.fn(), onCancelInstall: vi.fn(),
    reinstallLabel: "Reinstall CE", onReinstall: vi.fn(), game, appDetails: null, runningDetectionAvailable: true,
    runningGameCount: 1, targetProcess: "game.exe", onChooseGame: vi.fn(), table, tableSource: "OpenCT",
    onSearchTable: vi.fn(), onOpenImportedTables: vi.fn(), runtimeReady: true, runtimeText: "Connected",
    runtimeTextComplete: true, liveControlsUnavailable: false, liveSnapshotError: null,
    startRuntimeAvailable: false, startRuntimeBlockedReason: null, onStartRuntime: vi.fn(),
    activeCheatLabels: [], activeCheatSnapshotReady: true, activeScriptCount: 0, pinnedCount: 0, pinnedRows: [],
    pinnedBusyRecordId: null, onTogglePinnedCheat: vi.fn(), onChooseCheats: vi.fn(), onDisableAllCheats: vi.fn(),
    autoloadEnabled: false, autoloadBlockedReason: null, onAutoloadChange: vi.fn(),
    ceRunning: false, ceIdentityBlockedReason: null, launchPending: false, onStopCE: vi.fn(),
    onAdvanced: vi.fn(), busy: false, error: null,
    ...overrides,
  };
}

const state = (overrides: Partial<PluginUpdateState> = {}): PluginUpdateState => ({
  current_version: "0.9.27", auto_check: true, latest_version: "0.9.28", update_available: true,
  checked_at: 1_760_000_000, last_error: null, page_url: "https://github.com/x/y/releases",
  last_result: null, install_supported: true, checking: false, operation: null, ...overrides,
});

const operation = (overrides: Partial<PluginUpdateOperation> = {}): PluginUpdateOperation => ({
  operation_id: "op-1", state: "downloading", version: "0.9.28",
  message: "Downloading CE-Decky-v0.9.28.zip", error: null, ...overrides,
});

afterEach(() => cleanup());

describe("the update offer on the home panel", () => {
  it("draws nothing at all when there is no update to offer", () => {
    const view = render(<HomePanel {...props()} />);
    expect(screen.queryByText(/Update to v/)).toBeNull();
    expect(screen.queryByTestId("panel-update")).toBeNull();
    // Not merely invisible: a disabled control is still a stop the controller
    // walks through, and the panel has to be exactly what it was before.
    const controls = view.container.querySelectorAll("button");
    const labels = [...controls].map((node) => node.textContent);
    expect(labels.some((label) => label?.includes("Update"))).toBe(false);
  });

  it("offers the exact version, in the mascot's orange, and opens the confirmation", () => {
    const onUpdate = vi.fn();
    render(<HomePanel {...props({ updateVersion: "0.9.28", onUpdate })} />);
    const button = screen.getByText("Update to v0.9.28");
    expect(button.getAttribute("data-background")).toBe("var(--ce-update-accent)");
    // Named, so the component tests and the device's own panel reader can both
    // find it by the same id every other row here carries.
    expect(screen.getByTestId("panel-update")).toBeTruthy();
    fireEvent.click(button);
    expect(onUpdate).toHaveBeenCalledTimes(1);
  });

  it("is withheld while the panel is busy rather than acting on a stale offer", () => {
    render(<HomePanel {...props({ updateVersion: "0.9.28", busy: true })} />);
    expect((screen.getByText("Update to v0.9.28") as HTMLButtonElement).disabled).toBe(true);
  });

  it("hides the mascot when the user switched it off, and nothing else moves", () => {
    const shown = render(<HomePanel {...props()} />);
    expect(shown.container.querySelectorAll("img").length).toBe(1);
    const sections = screen.getAllByRole("region").length;
    shown.rerender(<HomePanel {...props({ mascotVisible: false })} />);
    expect(shown.container.querySelectorAll("img").length).toBe(0);
    expect(screen.getAllByRole("region").length).toBe(sections);
  });

  it("keeps the two things above the sections independent of each other", () => {
    // The mascot is a preference and the offer is a finding: either can be on
    // the panel without the other, and switching the image off must not take
    // the press with it.
    const view = render(<HomePanel {...props({ mascotVisible: false, updateVersion: "0.9.28" })} />);
    expect(view.container.querySelectorAll("img").length).toBe(0);
    expect(screen.getByTestId("panel-update")).toBeTruthy();
    expect(screen.getByText("Update to v0.9.28")).toBeTruthy();

    view.rerender(<HomePanel {...props({ mascotVisible: true, updateVersion: null })} />);
    expect(view.container.querySelectorAll("img").length).toBe(1);
    expect(screen.queryByTestId("panel-update")).toBeNull();
  });
});

describe("the update confirmation", () => {
  const modal = (overrides: Record<string, unknown> = {}) => ({
    currentVersion: "0.9.27", targetVersion: "0.9.28", gameRunning: false,
    onStart: vi.fn(async () => operation()),
    onPoll: vi.fn(async () => operation()),
    onCancelUpdate: vi.fn(async () => operation({ state: "cancelled", message: "The update was cancelled" })),
    onClose: vi.fn(),
    ...overrides,
  });

  it("names both versions and says what the install costs before the press", () => {
    render(<UpdateModal {...modal()} />);
    expect(screen.getByText("v0.9.27 to v0.9.28")).toBeTruthy();
    expect(screen.getByText("Steam's interface restarts")).toBeTruthy();
    expect(screen.getByText(/cannot be cancelled once it starts installing/)).toBeTruthy();
  });

  it("says that a running game can be interrupted, only when one is running", () => {
    const view = render(<UpdateModal {...modal({ gameRunning: true })} />);
    expect(screen.getByText(/can interrupt the game that is running/)).toBeTruthy();
    view.rerender(<UpdateModal {...modal({ gameRunning: false })} />);
    expect(screen.queryByText(/can interrupt the game that is running/)).toBeNull();
  });

  it("starts the update on the press and then reports what it is doing", async () => {
    const props = modal();
    render(<UpdateModal {...props} />);
    await act(async () => { fireEvent.click(screen.getByText("Update")); });
    expect(props.onStart).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Downloading CE-Decky-v0.9.28.zip")).toBeTruthy();
    // Not closed: the download is the only part of an update this can still
    // report or stop, so the window stays until Decky takes over.
    expect(props.onClose).not.toHaveBeenCalled();
  });

  it("offers Stop while it is still downloading and stops asking once Decky has it", async () => {
    vi.useFakeTimers();
    try {
      const installing = operation({ state: "installing", message: "Installing through Decky" });
      const props = modal({ onPoll: vi.fn(async () => installing) });
      render(<UpdateModal {...props} />);
      await act(async () => { fireEvent.click(screen.getByText("Update")); });
      expect(screen.getByText("Stop")).toBeTruthy();
      await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
      expect(screen.getByText("Steam's interface is restarting")).toBeTruthy();
      // Nothing left to press, and the controller's Back is refused too. The
      // actions row goes rather than carrying a sentence where its controls were.
      expect(screen.queryByText("Stop")).toBeNull();
      expect(screen.queryByTestId("modal-actions")).toBeNull();
      fireEvent.click(screen.getByText("controller-back"));
      expect(props.onClose).not.toHaveBeenCalled();
      const polls = props.onPoll.mock.calls.length;
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(props.onPoll.mock.calls.length).toBe(polls);
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives the window a way out if the interface restart never happens", async () => {
    // Installing refuses Back because the restart is what closes this window.
    // When the restart is the thing that failed, refusing for ever leaves a
    // window with no exit at all, on a panel whose backend is already replaced.
    vi.useFakeTimers();
    try {
      const props = modal({ onPoll: vi.fn(async () => operation({ state: "installing", message: "Installing through Decky" })) });
      render(<UpdateModal {...props} />);
      await act(async () => { fireEvent.click(screen.getByText("Update")); });
      await act(async () => { await vi.advanceTimersByTimeAsync(1100); });
      expect(screen.getByText("Steam's interface is restarting")).toBeTruthy();
      fireEvent.click(screen.getByText("controller-back"));
      expect(props.onClose).not.toHaveBeenCalled();

      await act(async () => { await vi.advanceTimersByTimeAsync(45_000); });
      expect(screen.getByText("Steam's interface has not restarted")).toBeTruthy();
      expect(screen.getByText(/does not stop it/)).toBeTruthy();
      fireEvent.click(screen.getByText("Close"));
      expect(props.onClose).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops a download on request and offers the press again", async () => {
    const props = modal();
    render(<UpdateModal {...props} />);
    await act(async () => { fireEvent.click(screen.getByText("Update")); });
    await act(async () => { fireEvent.click(screen.getByText("Stop")); });
    expect(props.onCancelUpdate).toHaveBeenCalledWith("op-1");
    await waitFor(() => expect(screen.getByText("Try again")).toBeTruthy());
  });

  it("reports a refused start in place rather than closing over it", async () => {
    const props = modal({ onStart: vi.fn(async () => { throw new Error("a Cheat Engine setup is still running"); }) });
    render(<UpdateModal {...props} />);
    await act(async () => { fireEvent.click(screen.getByText("Update")); });
    expect(screen.getByText(/Cheat Engine setup is still running/)).toBeTruthy();
    expect(props.onClose).not.toHaveBeenCalled();
    expect((screen.getByText("Update") as HTMLButtonElement).disabled).toBe(false);
  });
});

describe("what the update section says it is", () => {
  it("leads with the version to install when there is one", () => {
    const summary = updateSummary(state(), "0.9.27");
    expect(summary.label).toBe("v0.9.28 is available");
    expect(summary.description).toContain("on v0.9.27");
  });

  it("never reads a failed check as being up to date", () => {
    const summary = updateSummary(
      state({ update_available: false, latest_version: "0.9.27", last_error: "GitHub is rate limiting this device" }),
      "0.9.27",
    );
    expect(summary.label).not.toContain("up to date");
    expect(summary.description).toContain("rate limiting");
  });

  it("separates never checked, switched off and up to date", () => {
    expect(updateSummary(state({ update_available: false, checked_at: null, latest_version: null }), "0.9.27").description)
      .toContain("No check has run yet");
    expect(updateSummary(state({ update_available: false, checked_at: null, latest_version: null, auto_check: false }), "0.9.27").description)
      .toContain("Automatic checking is off");
    expect(updateSummary(state({ update_available: false, latest_version: "0.9.27" }), "0.9.27").label)
      .toContain("up to date");
    expect(updateSummary(null, "0.9.27").label).toBe("CE Decky v0.9.27");
  });
});

describe("which version the panel itself offers", () => {
  it("offers one only when it exists, is wanted, and can be installed here", () => {
    expect(panelUpdateOffer(state())).toBe("0.9.28");
    // Switched off, the finding stays in Advanced and leaves the panel alone.
    expect(panelUpdateOffer(state({ auto_check: false }))).toBeNull();
    // No interpreter for the installer: an orange button whose every press
    // fails is worse than the row in Advanced that says why.
    expect(panelUpdateOffer(state({ install_supported: false }))).toBeNull();
    expect(panelUpdateOffer(state({ update_available: false }))).toBeNull();
    expect(panelUpdateOffer(state({ latest_version: null }))).toBeNull();
    expect(panelUpdateOffer(null)).toBeNull();
  });
});
