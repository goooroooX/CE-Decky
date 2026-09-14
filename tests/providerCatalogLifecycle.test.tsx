import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  searchTables: vi.fn(),
  startTableAcquisition: vi.fn(),
  pollTableAcquisition: vi.fn(),
  completeTableAcquisition: vi.fn(),
  cancelTableAcquisition: vi.fn(),
}));
const decky = vi.hoisted(() => ({ toast: vi.fn() }));

vi.mock("../src/api", () => api);
vi.mock("@decky/api", () => ({
  FileSelectionType: { FILE: 0 },
  toaster: { toast: decky.toast },
}));
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ButtonItem: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  DropdownItem: ({ label, rgOptions, selectedOption, onChange, disabled }: any) => (
    <label>{label}<select aria-label={label} disabled={disabled} value={selectedOption ?? ""} onChange={(event) => onChange?.({ data: event.target.value, label: event.target.value })}>
      {rgOptions.map((option: any, index: number) => <option key={`${option.data}:${index}`} value={String(option.data)}>{String(option.label)}</option>)}
    </select></label>
  ),
  DialogButton: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: ({ children }: any) => <div>{children}</div>,
  gamepadDialogClasses: { Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription", CompactPadding: "CompactPadding" },
  Navigation: { NavigateToExternalWeb: vi.fn() },
  PanelSection: ({ title, children }: any) => <section aria-label={title}>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  Spinner: () => <span role="progressbar">Loading</span>,
  TextField: (props: any) => <label>{props.label}<input aria-label={props["aria-label"] ?? props.label} placeholder={props.placeholder} value={props.value} onChange={props.onChange} /></label>,
}));

import { ProviderCatalog } from "../src/providerCatalog";

const result = {
  provider: "playground", provider_display_name: "Playground", topic_id: "1", artifact_id: "page-1:file-2",
  table_title: "Example", filename: "Example.zip", version: null, size_bytes: null,
  source_page: "https://www.playground.ru/cheat/example-1", download_mode: "browser_handoff",
  match_score: 1, provider_rank: 1, author: null, posted_at: null, download_count: null,
  notes: null, stale: false, advertised_sha256: null, password_required: false,
};
const browserAcquisition = {
  acquisition_id: "c".repeat(32), provider: "playground", artifact_id: result.artifact_id,
  filename: "Example.zip", state: "browser_handoff", error: null, bytes_received: 0,
  expected_bytes: null, provider_wait_seconds: null, source_page: result.source_page,
  inspection: null, imported: null, execution_consent: null,
};

const directResult = {
  ...result,
  provider: "opencheattables",
  provider_display_name: "Open Cheat Tables",
  artifact_id: "topic-1:attachment-2",
  download_mode: "direct_https",
};
const downloadingAcquisition = { ...browserAcquisition, state: "downloading", source_page: null };

describe("Provider catalog lifecycle", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.searchTables.mockResolvedValue({
      results: [result], failures: [],
      sources: [{ provider: "playground", provider_display_name: "Playground", results: 1, status: "ok", error: null }],
      stale: false,
    });
    api.startTableAcquisition.mockResolvedValue(browserAcquisition);
    api.cancelTableAcquisition.mockImplementation(async (id: string) => ({
      ...browserAcquisition,
      acquisition_id: id,
      state: "cancelled",
      source_page: null,
    }));
  });
  afterEach(() => cleanup());

  it("never offers a source a controller cannot download", async () => {
    // Browsing the web with a controller is not a workflow, so a result whose
    // only route is the system browser is counted but never presented as an
    // action that leads nowhere.
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    expect(await screen.findByText(/Playground: 0\+1/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Example\.zip/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Choose downloaded file" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Open exact source page" })).toBeNull();
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
  });

  it("uses the async cancellation contract when an active acquisition unmounts", async () => {
    api.searchTables.mockResolvedValue({ results: [directResult], failures: [], stale: false });
    api.startTableAcquisition.mockResolvedValue(downloadingAcquisition);
    const view = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.zip/ }));
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());

    view.unmount();

    await waitFor(() => expect(api.cancelTableAcquisition).toHaveBeenCalledWith(downloadingAcquisition.acquisition_id));
  });
});
