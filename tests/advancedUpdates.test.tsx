import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  DialogButton: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  DropdownItem: ({ label }: any) => <div>{label}</div>,
  Focusable: ({ children, onActivate }: any) => <div onClick={() => onActivate?.(new CustomEvent("activate"))}>{children}</div>,
  ModalRoot: ({ children, onCancel }: any) => <div role="dialog"><button onClick={() => onCancel?.()}>controller-back</button>{children}</div>,
  PanelSection: ({ title, children }: any) => <section aria-label={title || "section"}>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  TextField: ({ label, value }: any) => <label>{label}<input readOnly value={value ?? ""} /></label>,
  ToggleField: ({ label, description, checked, onChange, disabled }: any) => (
    <label>{label}<input aria-label={label} type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange?.(event.target.checked)} />{description}</label>
  ),
}));

vi.mock("@decky/api", () => ({ toaster: { toast: vi.fn() }, callable: () => vi.fn() }));

const openExternalWeb = vi.fn();
vi.mock("../src/externalNavigation", () => ({ openExternalWeb: (url: string) => openExternalWeb(url) }));

import { AdvancedModal } from "../src/modals/AdvancedModal";
import type { PluginUpdateState } from "../src/types";

const SHA = "1".repeat(64);

const updateState = (overrides: Partial<PluginUpdateState> = {}): PluginUpdateState => ({
  current_version: "0.9.27", auto_check: true, latest_version: "0.9.28", update_available: true,
  checked_at: 1_760_000_000, last_error: null, page_url: "https://github.com/x/y/releases/tag/v0.9.28",
  last_result: null, install_supported: true, checking: false, operation: null, ...overrides,
});

function status(preferences: { mascot_visible: boolean } = { mascot_visible: true }) {
  return {
    version: "0.9.27", user_home: "/home/u", managed_root: "/home/u/.ce", settings_dir: "/settings",
    log_dir: "/logs", log_file: "/logs/ce.log", sevenzip: "/usr/bin/7z",
    ce: { configured: true, valid: true, executable: "/ce/ce.exe", sha256: "a".repeat(64), version: "7.7", reason: null, managed: true, provenance: null },
    tables: [], profiles: [], config_state_reason: null, table_state_reason: null,
    profile_state_reason: null, preferences, features: {},
  } as any;
}

function props(overrides: Record<string, unknown> = {}) {
  const callback = () => vi.fn(async () => undefined);
  return {
    status: status(), games: [], selectedGame: null, appDetails: null, inspection: null,
    targetProcess: "", ceLaunch: null, launchProtonToolId: "", runtime: null, selfTest: null, busy: false,
    onRefreshGames: vi.fn(async () => []), onSaveTargetProcess: callback(), onPickCE: callback(),
    onClearCEImport: vi.fn(async () => ({}) as any), onRunSelfTest: vi.fn(async () => ({ ok: true, checks: [] })),
    onLaunchProtonChange: vi.fn(), onRunCELaunchSelfTest: callback(),
    onRefreshRuntime: vi.fn(async () => null), onRefreshProcesses: vi.fn(async () => ({}) as any),
    onRetryAttach: vi.fn(async () => ({}) as any), onRepairSessionState: callback(),
    onRepairOwnedLaunchState: callback(), onRepairProfileState: callback(),
    onClearStartup: vi.fn(async () => 0), onRevokeConsent: callback(),
    onCheckRemoval: vi.fn(async () => ({}) as any), onDeleteManagedData: vi.fn(async () => ({}) as any),
    onLoadDiagnostics: vi.fn(async () => ({}) as any), onCollectSupportBundle: vi.fn(async () => ({}) as any),
    onRefreshAll: vi.fn(async () => ({}) as any), onClose: vi.fn(),
    update: updateState(), onSetUpdateAutoCheck: vi.fn(async (enabled: boolean) => updateState({ auto_check: enabled })),
    onCheckForUpdate: vi.fn(async () => updateState()), onStartUpdate: vi.fn(),
    onSetMascotVisible: vi.fn(async (visible: boolean) => ({ mascot_visible: visible })),
    ...overrides,
  } as any;
}

afterEach(() => { cleanup(); openExternalWeb.mockClear(); });

describe("Advanced, Plugin updates", () => {
  it("states the finding, offers the press and carries the switch", async () => {
    const view = props();
    render(<AdvancedModal {...view} />);
    expect(screen.getByText("v0.9.28 is available")).toBeTruthy();
    fireEvent.click(screen.getByText("Update to v0.9.28"));
    expect(view.onStartUpdate).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText("Check now"));
    await waitFor(() => expect(view.onCheckForUpdate).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByLabelText("Check for updates automatically"));
    await waitFor(() => expect(view.onSetUpdateAutoCheck).toHaveBeenCalledWith(false));
  });

  it("states a finding it will not offer to install here", () => {
    render(<AdvancedModal {...props({ update: updateState({ install_supported: false }) })} />);
    // The finding is still stated, and the press that would certainly fail is not.
    expect(screen.getByText("v0.9.28 is available")).toBeTruthy();
    expect(screen.queryByText("Update to v0.9.28")).toBeNull();
    expect(screen.getByText("Automatic installation is unavailable here")).toBeTruthy();
  });

  it("never reads a check that did not finish as being up to date", () => {
    render(<AdvancedModal {...props({
      update: updateState({ update_available: false, latest_version: "0.9.27", last_error: "GitHub is rate limiting this device" }),
    })} />);
    expect(screen.queryByText(/is up to date/)).toBeNull();
    expect(screen.getByText(/rate limiting/)).toBeTruthy();
  });

  it("names the kept file and the release page after a failed install", () => {
    const kept = "/home/u/CE-Decky-update-v0.9.28.zip";
    render(<AdvancedModal {...props({
      update: updateState({
        update_available: false, latest_version: "0.9.27",
        last_result: { version: "0.9.28", ok: false, error: "Decky refused the install", archive_kept_at: kept, at: 2, restart_requested: false },
      }),
    })} />);
    expect(screen.getByText("The last update did not install")).toBeTruthy();
    expect(screen.getByText(new RegExp(kept.replace(/[/.]/g, "\\$&")))).toBeTruthy();
    fireEvent.click(screen.getByText("Release page"));
    expect(openExternalWeb).toHaveBeenCalledWith("https://github.com/x/y/releases/tag/v0.9.28");
  });

  it("carries the mascot switch and reports what the press stored", async () => {
    const view = props({ status: status({ mascot_visible: true }) });
    render(<AdvancedModal {...view} />);
    const toggle = screen.getByLabelText("Show the mascot") as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    fireEvent.click(toggle);
    await waitFor(() => expect(view.onSetMascotVisible).toHaveBeenCalledWith(false));
    await waitFor(() => expect((screen.getByLabelText("Show the mascot") as HTMLInputElement).checked).toBe(false));
  });
});
