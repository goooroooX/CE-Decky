import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

/** Compatibility state is a glyph, so a row is found and asserted by its meaning. */
const NOT_WORKING = /Marked as not working/;
const SAME_TABLE = /Same table bytes as another source/;
/** The chip each source or payload condition keeps, which is never the compatibility mark. */
const BLOCKED_CHIPS = { refused: "Failed", gone: "Gone", unusable: "Not a table", encrypted: "Encrypted" };
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  searchTables: vi.fn(),
  startTableAcquisition: vi.fn(),
  pollTableAcquisition: vi.fn(),
  pollTableSearch: vi.fn(),
  completeTableAcquisition: vi.fn(),
  cancelTableAcquisition: vi.fn(),
  getProviderSources: vi.fn(),
  setProviderEnabled: vi.fn(),
  resetProviderSources: vi.fn(),
}));

vi.mock("../src/api", () => api);
vi.mock("@decky/api", () => ({
  FileSelectionType: { FILE: 0 },
  openFilePicker: vi.fn(),
  toaster: { toast: vi.fn() },
}));
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ButtonItem: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  DropdownItem: ({ label, rgOptions, selectedOption, onChange, disabled }: any) => (
    <label>{label}<select aria-label={label} disabled={disabled} value={selectedOption ?? ""} onChange={(event) => onChange?.({ data: event.target.value, label: event.target.value })}>
      <option value="" />
      {rgOptions.map((option: any, index: number) => <option key={`${option.data}:${index}`} value={String(option.data)}>{typeof option.label === "string" ? option.label : String(option.data)}</option>)}
    </select></label>
  ),
  DialogButton: ({ children, onClick, disabled, preferredFocus }: any) => <button disabled={disabled} data-preferred-focus={preferredFocus} onClick={onClick}>{children}</button>,
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: ({ children, navEntryPreferPosition, ...props }: any) => <div data-flow-children={props["flow-children"]} data-nav-entry={navEntryPreferPosition}>{children}</div>,
  ModalRoot: ({ children, onCancel }: any) => <div><button aria-label="Controller Back" onClick={onCancel}>Controller Back</button>{children}</div>,
  PanelSection: ({ title, children }: any) => <section aria-label={title}>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  Spinner: () => <span role="progressbar">Loading</span>,
  TextField: (props: any) => <label>{props.label}<input aria-label={props["aria-label"] ?? props.label} placeholder={props.placeholder} value={props.value} onChange={props.onChange} /></label>,
  gamepadDialogClasses: { Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription", CompactPadding: "CompactPadding" },
  showModal: vi.fn(() => ({ Close: vi.fn() })),
}));

import { ProviderCatalog } from "../src/providerCatalog";
import { showModal } from "@decky/ui";
import { showActionFailure } from "../src/modals/ActionFailureModal";
import { forgetSearchOutcomes } from "../src/providerCatalog";
import { forgetAllRejectedArtifacts, isRejectedArtifact, rememberRejectedArtifact } from "../src/tableImport";
import { blockedTableLookups } from "../src/uiModel";
import { commitSourceSelection, countsReadable, everySourceOn, sourceSwitched } from "../src/providerSelection";
import { DurableOutcomeUnknownError } from "../src/durableWrite";

const result = {
  provider: "opencheattables", provider_display_name: "Open Cheat Tables", topic_id: "1", artifact_id: "topic-1:attachment-2",
  table_title: "Example", filename: "Example.CT", version: "1.0", size_bytes: 100, source_page: "https://opencheattables.com/viewtopic.php?t=1",
  download_mode: "direct_https", match_score: 0.95, provider_rank: 90, author: null, posted_at: null, download_count: null,
  notes: null, stale: false, advertised_sha256: null, password_required: false,
};

const localTable = {
  sha256: "f".repeat(64), filename: "Saved Example.CT", size: 2048, table_version: "1.0",
  has_lua: false, has_auto_assembler: true, has_embedded_files: false, executable_content: true,
  entry_count: 12, blob_path: "/managed/table.CT", available: true, schema_version: 1,
  origins: [{
    provider: result.provider, artifact_id: result.artifact_id, topic_id: result.topic_id,
    source_page: result.source_page, original_filename: result.filename,
    retrieved_at: "2026-09-06T12:00:00Z", advertised_sha256: null,
  }],
};

/**
 * One entry of the not-working record, as the backend publishes it.
 *
 * The two lookups a row is gated by are a projection of this, and the defect
 * these cover was in that projection, so a test that writes the maps by hand
 * cannot see it: it is `blockedTableLookups` that puts one refusal into both.
 */
function blockedEntry(fields: Partial<Parameters<typeof blockedTableLookups>[0][number]> = {}) {
  return {
    key: "f".repeat(64),
    sha256: "f".repeat(64),
    reason: "Cheat Engine ran it and it went straight back off.",
    filename: "Saved Example.CT",
    app_id: 10,
    game_name: "Example",
    game_version: null,
    recorded_at: 1757160000,
    origins: [`${result.provider}:${result.artifact_id}`],
    cause: "refused" as const,
    ...fields,
  };
}

function blockedProps(...entries: ReturnType<typeof blockedEntry>[]) {
  const lookups = blockedTableLookups(entries);
  return { blockedTables: lookups.byDigest, blockedArtifacts: lookups.byArtifact };
}

function renderLastModal() {
  const element = vi.mocked(showModal).mock.calls.at(-1)?.[0] as React.ReactElement | undefined;
  if (!element) throw new Error("no modal was opened");
  return render(element);
}

const waitingAcquisition = {
  acquisition_id: "w".repeat(32), provider: "playground", artifact_id: "page-1:file-2",
  filename: "Example.CT", state: "waiting_provider", error: null, bytes_received: 0,
  expected_bytes: 100, provider_wait_seconds: 42, source_page: null, inspection: null,
  imported: null, execution_consent: null,
};

const browserAcquisition = {
  acquisition_id: "c".repeat(32), provider: "playground", artifact_id: "page-1:file-2",
  filename: "Example.zip", state: "browser_handoff", error: null, bytes_received: 0,
  expected_bytes: null, provider_wait_seconds: null, source_page: "https://www.playground.ru/cheat/example-1",
  inspection: null, imported: null, execution_consent: null,
};

describe("ProviderCatalog controller workflow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    forgetAllRejectedArtifacts();
    forgetSearchOutcomes();
    window.open = vi.fn();
    api.searchTables.mockResolvedValue({ results: [result], failures: [], stale: false });
    api.cancelTableAcquisition.mockImplementation(async (id: string) => ({ ...browserAcquisition, acquisition_id: id, state: "cancelled", source_page: null }));
    api.pollTableSearch.mockResolvedValue(null);
  });

  afterEach(() => {
    vi.useRealTimers();
    cleanup();
  });

  it.each([false, true])("explains another origin of exact archive-resolved bytes (failed=%s)", async (failed) => {
    const archiveSha = "a".repeat(64);
    const candidate = { ...result, provider: "vgtimes", artifact_id: "game-example:file-1",
      filename: "Pack.rar", advertised_sha256: archiveSha };
    api.searchTables.mockResolvedValue({ results: [candidate], failures: [], stale: false });
    let marked = failed;
    const marks = () => blockedTableLookups(marked ? [blockedEntry()] : []);
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      deviceTables={[localTable]} artifactResolutions={[{ provider: "vgtimes", artifact_id: candidate.artifact_id,
        artifact_sha256: archiveSha, table_sha256: localTable.sha256 }]}
      blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
      onClearMarks={async () => { marked = false; }} onRefreshBlocked={async () => marks()}
      onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    const row = await screen.findByRole("button", { name: SAME_TABLE });
    expect(row.textContent).toContain("Pack.rar");
    // The duplicate glyph is provenance and composes beside either state.
    expect(within(row).getByLabelText(SAME_TABLE)).toBeTruthy();
    expect(row.textContent).not.toContain("Same table");
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
    if (failed) {
      expect(within(row).getByLabelText(NOT_WORKING)).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: /Retry/ }));
      await waitFor(() => expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull());
      expect(row.textContent).toContain("Local");
    } else {
      expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull();
      expect(row.textContent).toContain("Local");
    }
  });

  it("keeps the search summary one line whether or not Retry is beside it", async () => {
    // Retry appears only when this page has marked rows, and it takes width
    // from the text column when it does. While that text wrapped, its
    // per-source tally reflowed onto another line the moment the button
    // appeared and moved the whole list under the reader's thumb.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: localTable.sha256 }], failures: [], stale: false,
    });
    const marks = () => blockedTableLookups([blockedEntry()]);
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      appId={10} deviceTables={[localTable]}
      blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
      onClearMarks={vi.fn()} onRefreshBlocked={async () => marks()}
      onLocalSelected={vi.fn()} onImported={vi.fn()} />);

    const retry = await screen.findByRole("button", { name: /^Retry / });
    expect(retry).toBeTruthy();
    // The row clips to one line and reveals the rest under the ring, rather
    // than growing to hold it.
    const row = screen.getByTestId("search-controls");
    expect(row.querySelectorAll(".ce-decky-marquee").length).toBeGreaterThan(0);
    expect(row.querySelectorAll(".ce-decky-wrap").length).toBe(0);
  });

  it("leads a result with when, which release and how large, on one line each", async () => {
    // A post carries every revision of one table, so its rows share a title, a
    // filename and the date of the post they sit in: what a reader scans is the
    // date, the release and the size, and those were last on a line that wrapped
    // to three or four. Both lines are clamped now and revealed under the ring,
    // so a page of results is a fixed number of pixels tall.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, posted_at: "2026-08-14T09:00:00Z", size_bytes: 4096 }],
      failures: [], stale: false,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    const row = await screen.findByRole("button", { name: /Example/ });
    const detail = row.textContent ?? "";
    expect(detail.indexOf("2026-08-14")).toBeLessThan(detail.indexOf("v1.0"));
    expect(detail.indexOf("v1.0")).toBeLessThan(detail.indexOf("4 KiB"));
    expect(detail.indexOf("4 KiB")).toBeLessThan(detail.indexOf("Example.CT"));
    expect(detail.indexOf("Example.CT")).toBeLessThan(detail.indexOf("Open Cheat Tables"));
    // The title and the detail are one clamped line each, inside a row marked
    // as the one whose focus drives its own reveal.
    const block = row.closest(".ce-decky-focusscroll");
    expect(block).toBeTruthy();
    expect(block?.querySelectorAll(".ce-decky-marquee").length).toBe(2);
  });

  it("claims a release for a saved copy only where a source stated one", async () => {
    // It used to print the `.CT` file's own `CheatEngineTableVersion`, which is
    // the version of Cheat Engine's table format: every table reads 45 or 46
    // whatever its game, so the row named the file's format as though it were
    // the release the reader is choosing between.
    api.searchTables.mockResolvedValue({ results: [], failures: [], stale: false });
    const unstated = {
      ...localTable, table_version: "46",
      origins: [{ ...localTable.origins[0], version: undefined }],
    };
    const { rerender } = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      localTables={[unstated]} onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    let row = await screen.findByRole("button", { name: /Saved Example\.CT/ });
    expect(row.textContent).not.toContain("v46");
    // And the date this copy arrived leads it, as it does on a source row.
    expect(row.textContent).toContain("2026-09-06");

    rerender(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      localTables={[{ ...unstated, origins: [{ ...localTable.origins[0], version: "1.05.01" }] }]}
      onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    row = await screen.findByRole("button", { name: /Saved Example\.CT/ });
    expect(row.textContent).toContain("v1.05.01");
    expect(row.textContent).not.toContain("v46");
  });

  it("does not restore stale green evidence when clearing Failed without a readable refresh", async () => {
    let marked = true;
    const marks = () => blockedTableLookups(marked ? [blockedEntry()] : []);
    api.searchTables.mockResolvedValue({ results: [{ ...result, advertised_sha256: localTable.sha256 }], failures: [], stale: false });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      appId={10} deviceTables={[localTable]} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
      compatibility={[{ app_id: 10, table_sha256: localTable.sha256, target_process: "game.exe",
        pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" }]}
      onClearMarks={async () => { marked = false; }} onRefreshBlocked={async () => marks()}
      onRefreshProvenance={async () => { throw new Error("status unavailable"); }}
      onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    await screen.findByRole("button", { name: NOT_WORKING });
    expect(screen.queryByLabelText("Worked on this build")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Retry/ }));
    await waitFor(() => expect(screen.queryByLabelText(NOT_WORKING)).toBeNull());
    expect(screen.queryByLabelText("Worked on this build")).toBeNull();
    expect(screen.getAllByLabelText("Worked before; retest needed").length).toBeGreaterThan(0);
  });

  describe("availability and compatibility are said separately", () => {
    // The advisory record is about whether the table works. Whether the bytes
    // are on this device is a different fact and the user acts on it offline,
    // so a failure must not take `Local` away, and the same exact SHA has to
    // read the same whether Search folded it into a provider row or is showing
    // it as a row of its own.
    const twoOrigins = { ...localTable, origins: [...localTable.origins, {
      provider: "vgtimes", artifact_id: "game-example:file-1", topic_id: null,
      source_page: "https://vgtimes.example/hidden-blade", original_filename: "Pack.rar",
      retrieved_at: "2026-09-07T12:00:00Z", advertised_sha256: "a".repeat(64),
    }] };

    it.each([
      ["unusable" as const, "there is no table in it"],
      ["encrypted" as const, "the archive does not open"],
      ["gone" as const, "the source no longer has it"],
    ])("keeps a verified stored copy usable through a %s record", async (cause, reason) => {
      // These are statements about a download or about a source. A copy in the
      // library was imported and verified as a table, so none of them can make
      // it absent, rename it, take away the one press that needs no network, or
      // retire the row that press is on. The record carries the provider origin
      // the backend writes, which is how it reaches the row at all.
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any}
        {...blockedProps(blockedEntry({ cause, reason }))}
        compatibility={[{ app_id: 10, table_sha256: localTable.sha256, target_process: "game.exe",
          pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" }]}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Example\.CT/ }) as HTMLButtonElement;
      expect(row.textContent).toContain("Local");
      expect(row.textContent).not.toContain(BLOCKED_CHIPS[cause]);
      expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull();
      // Nor does it answer whether the table worked: proven history stands.
      expect(within(row).getByLabelText("Worked on this build")).toBeTruthy();
      expect(row.disabled).toBe(false);
      fireEvent.click(row);
      const modal = renderLastModal();
      expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(false);
      // The download half is where the condition belongs, and it says so.
      expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
      expect(modal.container.textContent).toContain(reason);
    });

    it.each(["unusable", "encrypted", "gone"] as const)(
        "keeps proven history green when a re-read brings back a %s record", async (cause) => {
      // The screen is detached, so it overlays what a re-read tells it. That
      // overlay is for records about whether the table works and for nothing
      // else, or a source failure would quietly turn a proven table amber here
      // while Manage went on showing it green for the same exact SHA.
      const refused = "b".repeat(64);
      let entries = [
        blockedEntry({ cause, reason: "a condition of the download", origins: [] }),
        blockedEntry({ key: refused, sha256: refused, origins: [`${result.provider}:other`],
          reason: "it went straight back off" }),
      ];
      const marks = () => blockedTableLookups(entries);
      api.searchTables.mockResolvedValue({
        results: [{ ...result, artifact_id: "other", advertised_sha256: refused }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        compatibility={[{ app_id: 10, table_sha256: localTable.sha256, target_process: "game.exe",
          pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" }]}
        onClearMarks={async (digests) => { entries = entries.filter((entry) => !digests.includes(entry.sha256)); }}
        onRefreshBlocked={async () => marks()}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const stored = await screen.findByRole("button", { name: /Saved Example\.CT/ });
      expect(within(stored).getByLabelText("Worked on this build")).toBeTruthy();
      // Clearing the other row's failure re-reads every record, which is the
      // moment the overlay used to reach this one.
      fireEvent.click(screen.getByRole("button", { name: /^Retry 1$/ }));
      await waitFor(() => expect(entries).toHaveLength(1));
      const after = await screen.findByRole("button", { name: /Saved Example\.CT/ });
      expect(within(after).getByLabelText("Worked on this build")).toBeTruthy();
      expect(within(after).queryByLabelText("Worked before; retest needed")).toBeNull();
    });

    it("takes the green only from the table a cleared record said did not work", async () => {
      // One press clearing both kinds at once, with the status read that would
      // have corrected it refused. Clearing a record that a table did not work
      // never brings its old green back, so this screen takes it at the press
      // rather than waiting; a record about a download never took it away, so
      // clearing one must not take it either.
      const offered = "b".repeat(64);
      const proven = (sha: string) => ({ app_id: 10, table_sha256: sha, target_process: "game.exe",
        pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" as const });
      let entries = [
        blockedEntry({ origins: [] }),
        blockedEntry({ key: offered, sha256: offered, cause: "unusable" as const,
          reason: "there is no table in it", origins: [`${result.provider}:other`] }),
      ];
      const marks = () => blockedTableLookups(entries);
      api.searchTables.mockResolvedValue({
        results: [{ ...result, artifact_id: "other", advertised_sha256: offered }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        compatibility={[proven(localTable.sha256), proven(offered)]}
        onClearMarks={async (digests) => { entries = entries.filter((entry) => !digests.includes(entry.sha256)); }}
        onRefreshBlocked={async () => marks()}
        onRefreshProvenance={async () => { throw new Error("status unavailable"); }}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      // Two marked rows on the page: the stored copy that did not work, and the
      // row whose download was not a table.
      await screen.findByRole("button", { name: NOT_WORKING });
      expect(screen.getByRole("button", { name: /Not a table.*Example\.CT/ })).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: /^Retry 2$/ }));
      await waitFor(() => expect(entries).toHaveLength(0));

      const stored = await screen.findByRole("button", { name: /Saved Example\.CT/ });
      expect(within(stored).getByLabelText("Worked before; retest needed")).toBeTruthy();
      const offeredRow = screen.getAllByRole("button", { name: /Example\.CT/ })
        .find((node) => !node.textContent?.includes("Saved Example.CT"))!;
      expect(within(offeredRow).getByLabelText("Worked on this build")).toBeTruthy();
    });

    it("still refuses the saved copy for a record about whether the table works", async () => {
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} {...blockedProps(blockedEntry({ origins: [] }))}
        onClearMarks={vi.fn()} onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: NOT_WORKING });
      expect(row.textContent).toContain("Local");
      fireEvent.click(row);
      renderLastModal();
      expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(true);
    });

    it("keeps Local on a failed provider row that folded a stored copy", async () => {
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} {...blockedProps(blockedEntry())}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: NOT_WORKING });
      expect(row.textContent).toContain("Local");
      expect(within(row).queryByLabelText(SAME_TABLE)).toBeNull();
    });

    it("composes failure, duplicate provenance and Local on one row", async () => {
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[twoOrigins] as any} {...blockedProps(blockedEntry())}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: NOT_WORKING });
      expect(within(row).getByLabelText(SAME_TABLE)).toBeTruthy();
      expect(row.textContent).toContain("Local");
    });

    it("keeps Local and returns to retest when the failure is cleared", async () => {
      let marked = true;
      const marks = () => blockedTableLookups(marked ? [blockedEntry()] : []);
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        compatibility={[{ app_id: 10, table_sha256: localTable.sha256, target_process: "game.exe",
          pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" }]}
        onClearMarks={async () => { marked = false; }} onRefreshBlocked={async () => marks()}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: NOT_WORKING });
      expect(row.textContent).toContain("Local");
      fireEvent.click(screen.getByRole("button", { name: /Retry/ }));
      await waitFor(() => expect(screen.queryByLabelText(NOT_WORKING)).toBeNull());
      // Nothing but a new success brings the green back.
      expect(screen.getByLabelText("Worked before; retest needed")).toBeTruthy();
      expect(screen.queryByLabelText("Worked on this build")).toBeNull();
      expect(screen.getByRole("button", { name: /Worked before/ }).textContent).toContain("Local");
    });

    it("claims no availability for a failed row with nothing stored", async () => {
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: localTable.sha256 }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        {...blockedProps(blockedEntry())} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: NOT_WORKING });
      expect(row.textContent).not.toContain("Local");
    });

    it("says the same thing about one exact table on either kind of row", async () => {
      const readRow = async () => {
        const row = await screen.findByRole("button", { name: NOT_WORKING });
        return {
          label: within(row).getByLabelText(NOT_WORKING).getAttribute("aria-label"),
          local: row.textContent?.includes("Local"),
        };
      };
      const folded = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} {...blockedProps(blockedEntry())}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const foldedRow = await readRow();
      folded.unmount();
      cleanup();
      // A search that returns some other row leaves the stored table to be
      // listed on its own, which is the other presentation of the same SHA.
      api.searchTables.mockResolvedValue({
        results: [{ ...result, artifact_id: "topic-9:other", filename: "Other.CT", table_title: "Other" }],
        failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} {...blockedProps(blockedEntry())}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      expect(await readRow()).toEqual(foldedRow);
    });
  });

  describe("one mark is about one exact table", () => {
    // A row can be about two sets of bytes at once: the copy this device holds
    // and the revision the source is offering now. A mark that took its failure
    // from one and its success from the other described neither, and could
    // paint a revision nobody has tried in the colour of an older one.
    const revision = "b".repeat(64);
    const worksOnThisBuild = (sha: string) => ({
      app_id: 10, table_sha256: sha, target_process: "game.exe", pe_version: "1",
      steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" as const,
    });

    it.each([["with", true], ["without", false]])(
        "keeps a saved copy's failure off a newer revision, %s evidence for it", async (_label, evidence) => {
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: revision }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} {...blockedProps(blockedEntry())}
        compatibility={evidence ? [worksOnThisBuild(revision)] : []}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Example\.CT/ });
      // The row is presenting the stored copy, so the mark is about that copy.
      expect(within(row).getByLabelText(NOT_WORKING)).toBeTruthy();
      expect(row.textContent).toContain("Local");
      // Nothing here claims anything about the bytes the source now advertises.
      expect(within(row).queryByLabelText("Worked on this build")).toBeNull();
      expect(within(row).queryByLabelText(/Worked before/)).toBeNull();
    });

    it("keeps a newer revision's refusal off the stored copy that works", async () => {
      // The other direction, and the one that used to read as a verdict on
      // bytes this device has been running happily: the source published a
      // revision that was tried and refused, and the copy the row is
      // presenting is the older one that works.
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: revision }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any}
        {...blockedProps(blockedEntry({ key: revision, sha256: revision,
          reason: "the newer revision went straight back off" }))}
        compatibility={[worksOnThisBuild(localTable.sha256)]}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Example\.CT/ });
      expect(within(row).getByLabelText("Worked on this build")).toBeTruthy();
      expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull();
      expect(row.textContent).toContain("Local");
      // The refusal is about the download, and the window is where the two sets
      // of bytes are told apart in words.
      fireEvent.click(row);
      expect(renderLastModal().container.textContent).toContain("Downloading again is not available");
    });

    it("marks the offered revision, not an unrelated record, when nothing is stored", async () => {
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: revision }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        {...blockedProps(blockedEntry())} compatibility={[worksOnThisBuild(revision)]}
        onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Example\.CT/ });
      expect(within(row).getByLabelText("Worked on this build")).toBeTruthy();
      expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull();
    });
  });

  it("keeps a not-working record device-wide while a success stays with the game that proved it", async () => {
    // The two halves of the family are scoped differently on purpose. Positive
    // evidence is about one game and one table: another game borrows no green
    // from it. The record that a table did not work is about the bytes, is
    // cleared once for the whole device, and is shown in every game's search
    // until it is. Changing either of those is a product decision, not a tidy-up.
    const proven = { app_id: 10, table_sha256: localTable.sha256, target_process: "game.exe",
      pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" as const };
    api.searchTables.mockResolvedValue({ results: [], failures: [], stale: false });
    const view = render(<ProviderCatalog gameIdentity="11:steam" gameName="Other" autoSearch appId={11}
      localTables={[localTable] as any} compatibility={[proven]}
      onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    await screen.findByRole("button", { name: /Saved Example\.CT/ });
    expect(screen.queryByLabelText("Worked on this build")).toBeNull();
    view.unmount();
    cleanup();

    render(<ProviderCatalog gameIdentity="11:steam" gameName="Other" autoSearch appId={11}
      localTables={[localTable] as any} compatibility={[proven]}
      {...blockedProps(blockedEntry({ app_id: 10, game_name: "Example", origins: [] }))}
      onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    const marked = await screen.findByRole("button", { name: /Saved Example\.CT/ });
    expect(within(marked).getByLabelText(NOT_WORKING)).toBeTruthy();
    expect(marked.textContent).toContain("Local");
  });

  describe("Retry is scoped to what the screen is showing", () => {
    it("offers Retry for a saved table the list itself marked red", async () => {
      // The red row is in front of the user on this screen; sending them to
      // Advanced to clear it was the one route the screen already had.
      api.searchTables.mockResolvedValue({ results: [], failures: [], stale: false });
      const cleared: string[][] = [];
      let marked = true;
      const marks = () => blockedTableLookups(marked ? [blockedEntry({ origins: [] })] : []);
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        compatibility={[{ app_id: 10, table_sha256: localTable.sha256, target_process: "game.exe",
          pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" }]}
        onClearMarks={async (digests) => { cleared.push(digests); marked = false; }}
        onRefreshBlocked={async () => marks()}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Saved Example\.CT/ });
      expect(within(row).getByLabelText(NOT_WORKING)).toBeTruthy();
      fireEvent.click(await screen.findByRole("button", { name: "Retry 1", exact: true }));
      await waitFor(() => expect(screen.queryByLabelText(NOT_WORKING)).toBeNull());
      expect(cleared).toEqual([[localTable.sha256]]);
      // The bytes never went anywhere, and the old success does not come back
      // green: it is history until a cheat proves the table again.
      const cleanRow = screen.getByRole("button", { name: /Saved Example\.CT/ });
      expect(cleanRow.textContent).toContain("Local");
      expect(within(cleanRow).getByLabelText("Worked before; retest needed")).toBeTruthy();
    });

    it("never counts a refusal the row in front of the user does not show", async () => {
      // The row is presenting a saved copy that works. The refusal is about the
      // revision the source is offering now, and a count with no marked row
      // under it is a press into the dark.
      const revision = "b".repeat(64);
      // A second record this row earned years ago, about bytes it is not
      // offering now. Nothing in front of the user names it, so no press here
      // may quietly drop it either.
      const older = "d".repeat(64);
      let entries = [
        blockedEntry({ key: revision, sha256: revision, origins: [],
          reason: "the newer revision went straight back off" }),
        blockedEntry({ key: older, sha256: older, recorded_at: 1700000000,
          reason: "an older revision went straight back off" }),
      ];
      const marks = () => blockedTableLookups(entries);
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: revision }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        compatibility={[{ app_id: 10, table_sha256: localTable.sha256, target_process: "game.exe",
          pe_version: "1", steam_build_id: null, last_working_at: 1788998400, invalidated: false, state: "matching" }]}
        onClearMarks={async (digests) => { entries = entries.filter((entry) => !digests.includes(entry.sha256)); }}
        onRefreshBlocked={async () => marks()}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Example\.CT/ });
      expect(within(row).getByLabelText("Worked on this build")).toBeTruthy();
      expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull();

      // The window is where the two revisions are named apart, so it is where
      // the one the row does not show is cleared.
      fireEvent.click(row);
      renderLastModal();
      expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
      fireEvent.click(screen.getByRole("button", { name: "Retry", exact: true }));
      await waitFor(() => expect(entries.map((entry) => entry.sha256)).toEqual([older]));
      // Clearing the download's record leaves the copy that worked alone.
      const after = await screen.findByRole("button", { name: /Example\.CT/ });
      expect(within(after).getByLabelText("Worked on this build")).toBeTruthy();
      expect(after.textContent).toContain("Local");
    });
  });

  describe("a press clears the exact table it is beside", () => {
    // Both halves of the row carrying a record at once is the case that used to
    // make one press act on two tables: the stored copy is marked, and the
    // revision the source is offering now is refused for a reason of its own.
    const revision = "b".repeat(64);

    it.each([
      ["refused", "refused" as const, "the newer revision went straight back off"],
      ["gone", "gone" as const, "the source no longer has it"],
      ["not a table", "unusable" as const, "there is no table in it"],
      ["encrypted", "encrypted" as const, "the archive does not open"],
    ])("clears the stored copy and leaves the current revision's %s record", async (_label, cause, reason) => {
      let entries = [
        blockedEntry({ origins: [] }),
        blockedEntry({ key: revision, sha256: revision, cause, reason, origins: [] }),
      ];
      const marks = () => blockedTableLookups(entries);
      const onClearMarks = vi.fn(async (digests: string[]) => {
        entries = entries.filter((entry) => !digests.includes(entry.sha256));
      });
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: revision }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        onClearMarks={onClearMarks} onRefreshBlocked={async () => marks()}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: NOT_WORKING });
      // What the row is showing: the stored copy's mark, and that the bytes are
      // here. The current revision's own record is not on this row at all.
      expect(row.textContent).toContain("Local");
      expect(row.textContent).not.toContain(BLOCKED_CHIPS[cause]);
      fireEvent.click(screen.getByRole("button", { name: /^Retry 1$/ }));
      await waitFor(() => expect(onClearMarks).toHaveBeenCalledWith([localTable.sha256]));
      expect(entries.map((entry) => entry.sha256)).toEqual([revision]);
      // Cleared, the row opens, and the window is where the other one is named.
      const usable = await screen.findByRole("button", { name: /Example\.CT/ });
      expect((usable as HTMLButtonElement).disabled).toBe(false);
      fireEvent.click(usable);
      expect(renderLastModal().container.textContent).toContain(reason);
    });

    it("keeps this session's memory of a damaged download the row is not showing", async () => {
      // The row is presenting the stored copy and its mark. That the last
      // download from this row came back damaged is a statement about the row,
      // it is not on the row while Local is, and it is not this press's to
      // forget: the window is where that half is named and retried.
      // The search scope this screen keeps that memory under, which is its own
      // identity: game, query and shortcut target.
      const scope = "10:steam\u0000example\u0000";
      rememberRejectedArtifact(scope, {
        provider: result.provider, artifact_id: result.artifact_id, artifact_rejected: true,
      } as any);
      let entries = [blockedEntry({ origins: [] })];
      const marks = () => blockedTableLookups(entries);
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        localTables={[localTable] as any} blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        onClearMarks={async (digests) => { entries = entries.filter((entry) => !digests.includes(entry.sha256)); }}
        onRefreshBlocked={async () => marks()}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: NOT_WORKING });
      expect(row.textContent).toContain("Local");
      fireEvent.click(screen.getByRole("button", { name: /^Retry 1$/ }));
      await waitFor(() => expect(entries).toHaveLength(0));
      expect(isRejectedArtifact(scope, result.provider, result.artifact_id)).toBe(true);
    });

    it("still clears a condition the row is the one showing", async () => {
      // No saved copy: the refusal on what the source would serve is the row's
      // own statement, so the list's press is what clears it.
      let entries = [blockedEntry({ key: revision, sha256: revision, cause: "unusable" as const,
        reason: "there is no table in it", origins: [] })];
      const marks = () => blockedTableLookups(entries);
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: revision }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
        blockedTables={marks().byDigest} blockedArtifacts={marks().byArtifact}
        onClearMarks={async (digests) => { entries = entries.filter((entry) => !digests.includes(entry.sha256)); }}
        onRefreshBlocked={async () => marks()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Not a table.*Example\.CT/ });
      expect(row).toBeTruthy();
      fireEvent.click(screen.getByRole("button", { name: /^Retry 1$/ }));
      await waitFor(() => expect(entries).toHaveLength(0));
    });
  });

  describe("the saved-or-download window is one honest statement", () => {
    const revision = "b".repeat(64);
    const openFor = (props: Record<string, unknown>) => render(<ProviderCatalog
      gameIdentity="10:steam" gameName="Example" autoSearch appId={10}
      localTables={[localTable] as any} onLocalSelected={vi.fn()} onImported={vi.fn()}
      onRefreshBlocked={async () => ({ byDigest: {}, byArtifact: {} })} {...props} />);

    it("never says a copy can be used while the press that uses it is off", async () => {
      openFor({ ...blockedProps(blockedEntry({ origins: [] })), onClearMarks: vi.fn() });
      fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
      const modal = renderLastModal();
      expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(true);
      expect(modal.container.textContent).not.toContain("can use this saved copy now");
      expect(modal.container.textContent).not.toContain("works offline");
      expect(modal.container.textContent).toContain("Clear the mark on this copy");
      // The identity of the copy is still stated; only the promise is gone.
      expect(modal.container.textContent).toContain(localTable.sha256.slice(0, 12));
    });

    it("keeps the ordinary wording when the copy is usable", async () => {
      openFor({});
      fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
      const modal = renderLastModal();
      expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(false);
      expect(modal.container.textContent).toContain("You can use this saved copy now");
      expect(modal.container.textContent).not.toContain("Clear the mark on this copy");
    });

    it.each([
      ["a usable copy takes the ring", false, "Use saved copy"],
      ["a marked copy hands it to the download", true, "Download again"],
    ])("opens the ring on a press that can answer: %s", async (_label, blockSaved, expected) => {
      // The window is reached only where one of its two presses is live, and
      // Retry is last in the same order, so the state where both are off and it
      // is the only way on cannot open on a dead control either.
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: revision }], failures: [], stale: false,
      });
      const marks = blockedTableLookups(blockSaved ? [blockedEntry({ origins: [] })] : []);
      openFor({ blockedTables: marks.byDigest, blockedArtifacts: marks.byArtifact, onClearMarks: vi.fn() });
      fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
      renderLastModal();
      await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: expected, exact: true })));
    });

  });

  describe("duplicate provenance is symmetric", () => {
    // One half of a known pair marked and the other not was worse than neither:
    // the glyph exists to explain why two unrelated-looking rows are the same
    // saved bytes, and it can only do that on both of them.
    const archiveSha = "a".repeat(64);
    const saved = { ...localTable, origins: [...localTable.origins, {
      provider: "vgtimes", artifact_id: "game-example:file-1", topic_id: null,
      source_page: "https://vgtimes.example/hidden-blade", original_filename: "Pack.rar",
      retrieved_at: "2026-09-07T12:00:00Z", advertised_sha256: archiveSha,
    }] };
    const archiveRow = { ...result, provider: "vgtimes", provider_display_name: "VGTimes",
      artifact_id: "game-example:file-1", filename: "Pack.rar", table_title: "Example pack",
      advertised_sha256: archiveSha };

    it("marks both the digestless row and the archive row it duplicates", async () => {
      api.searchTables.mockResolvedValue({ results: [result, archiveRow], failures: [], stale: false });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
        localTables={[saved] as any}
        artifactResolutions={[{ provider: "vgtimes", artifact_id: archiveRow.artifact_id,
          artifact_sha256: archiveSha, table_sha256: saved.sha256 }]}
        onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      await waitFor(() => expect(screen.getAllByLabelText(SAME_TABLE)).toHaveLength(2));
      for (const filename of ["Example\\.CT", "Pack\\.rar"]) {
        const row = screen.getByRole("button", { name: new RegExp(filename) });
        expect(within(row).getByLabelText(SAME_TABLE)).toBeTruthy();
      }
    });

    it("leaves a digestless row with only its own origin unmarked", async () => {
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
        localTables={[localTable] as any} onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      await screen.findByRole("button", { name: /Example/ });
      expect(screen.queryByLabelText(SAME_TABLE)).toBeNull();
    });

    it("marks the saved copy it is presenting, not the bytes the source now names", async () => {
      // The row is showing this device's copy, and the compatibility mark and
      // Local beside the duplicate glyph are about that copy, so the glyph is
      // about it too. The copy has a second known origin; the revision the
      // source is advertising has none.
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: "c".repeat(64) }], failures: [], stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
        localTables={[saved] as any} onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const row = await screen.findByRole("button", { name: /Example\.CT/ });
      expect(within(row).getByLabelText(SAME_TABLE)).toBeTruthy();
      expect(row.textContent).toContain("Local");
    });

    it("keeps a duplicated online revision off a row presenting a single-origin copy", async () => {
      // Both halves in one search: the row showing this device's own copy makes
      // no duplicate claim, because that copy has one origin, while the row with
      // nothing saved speaks for the revision two sources now agree on.
      const shared = "c".repeat(64);
      api.searchTables.mockResolvedValue({ results: [
        { ...result, advertised_sha256: shared },
        { ...result, provider: "vgtimes", provider_display_name: "VGTimes",
          artifact_id: "game-example:file-9", filename: "Other.CT", table_title: "Other", advertised_sha256: shared },
      ], failures: [], stale: false });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
        localTables={[localTable] as any} onLocalSelected={vi.fn()} onImported={vi.fn()} />);
      const presenting = await screen.findByRole("button", { name: /Example\.CT/ });
      expect(within(presenting).queryByLabelText(SAME_TABLE)).toBeNull();
      expect(presenting.textContent).toContain("Local");
      const offering = screen.getByRole("button", { name: /Other\.CT/ });
      expect(within(offering).getByLabelText(SAME_TABLE)).toBeTruthy();
    });
  });

  it("never infers a duplicate from a filename or from archive bytes nobody has resolved", async () => {
    // A name is not an identity, and an archive whose final table has never
    // been extracted has no proven identity to be the same as anything.
    const namesake = { ...result, provider: "vgtimes", artifact_id: "game-example:file-2",
      filename: "Saved Example.CT", advertised_sha256: "c".repeat(64) };
    const archive = { ...result, provider: "vgtimes", artifact_id: "game-example:file-3",
      filename: "Pack.rar", advertised_sha256: "d".repeat(64) };
    api.searchTables.mockResolvedValue({ results: [namesake, archive], failures: [], stale: false });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      deviceTables={[localTable]} onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    await screen.findByRole("button", { name: /Pack\.rar/ });
    expect(screen.queryByLabelText(SAME_TABLE)).toBeNull();
  });

  it("does not label a row's own saved bytes as a duplicate", async () => {
    api.searchTables.mockResolvedValue({ results: [{ ...result, advertised_sha256: localTable.sha256 }], failures: [], stale: false });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      deviceTables={[localTable]} onLocalSelected={vi.fn()} onImported={vi.fn()} />);
    await screen.findByRole("button", { name: /Local.*Example.CT/ });
    expect(screen.queryByLabelText(SAME_TABLE)).toBeNull();
    expect(screen.queryByText("Same table")).toBeNull();
  });

  it("pages long provider result sets instead of creating an unbounded focus list", async () => {
    const results = Array.from({ length: 14 }, (_, index) => ({ ...result, artifact_id: `artifact-${index}`, filename: `Table-${index}.CT` }));
    api.searchTables.mockResolvedValueOnce({ results, failures: [], stale: false });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} footerActions={<button onClick={vi.fn()}>Close</button>} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    expect(await screen.findByText(/14 table\(s\)/)).toBeTruthy();
    // The page counter sits between the two controls that change it rather than
    // at the end of the summary line, which is where a reader looks for it and
    // the only place it costs no height of its own.
    expect(screen.getByTestId("catalog-footer").textContent).toContain("1 / 3");
    const footerFocusRows = new Set(
      ["Previous", "Next", "Close"].map((name) => screen.getByRole("button", { name: new RegExp(name) }).closest('[data-flow-children="row"]')),
    );
    expect([...footerFocusRows].every(Boolean)).toBe(true);
    expect(footerFocusRows.size).toBe(1);
    const next = screen.getByRole("button", { name: /Next/ });
    expect(next.closest('[data-flow-children="row"]')?.getAttribute("data-nav-entry")).toBe("4");
    expect(next.getAttribute("data-preferred-focus")).toBe("true");
    expect(screen.getByRole("button", { name: /Table-0\.CT/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Table-8\.CT/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    expect(screen.getByRole("button", { name: /Table-8\.CT/ })).toBeTruthy();
    // The ring stays on the control that turned the page even as the enabled
    // paging direction changes inside the shared footer group.
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /Next/ }));
    expect(screen.getByTestId("catalog-footer").textContent).toContain("2 / 3");

    fireEvent.click(screen.getByRole("button", { name: /Next/ }));
    fireEvent.click(screen.getByRole("button", { name: /Previous/ }));
    expect(screen.getByTestId("catalog-footer").textContent).toContain("2 / 3");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /Previous/ }));

    // And onto the control beside it once turning back has disabled it.
    fireEvent.click(screen.getByRole("button", { name: /Previous/ }));
    expect((screen.getByRole("button", { name: /Previous/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: /Next/ }));
  });

  it("leaves focus alone when a fresh search puts the list back to its first page", async () => {
    // A page number also moves without a press. Focus is on the search row by
    // then, and pulling it down to the pager would move the user off the screen
    // they are using.
    const results = Array.from({ length: 14 }, (_, index) => ({ ...result, artifact_id: `artifact-${index}`, filename: `Table-${index}.CT` }));
    api.searchTables.mockResolvedValue({ results, failures: [], stale: false });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    await screen.findByTestId("catalog-footer");
    fireEvent.click(screen.getByRole("button", { name: /Next/ }));

    const again = screen.getByRole("button", { name: /^Search( again)?$/ });
    again.focus();
    fireEvent.click(again);
    await waitFor(() => expect(screen.getByTestId("catalog-footer").textContent).toContain("1 / 3"));
    expect(document.activeElement).toBe(again);
  });

  it("shows the publication date on a result and orders the newest first", async () => {
    // Freshness is what tells a user whether a table still matches the game
    // build, and match scores for one game cluster too tightly to order by.
    const older = { ...result, artifact_id: "old", table_title: "Older table", posted_at: "2025-04-28T04:04:14Z" };
    const newer = { ...result, artifact_id: "new", table_title: "Newer table", posted_at: "2026-07-30T19:01:36Z" };
    api.searchTables.mockResolvedValueOnce({ results: [newer, older], failures: [], sources: [], stale: false });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const rows = await screen.findAllByRole("button", { name: /Example\.CT/ });
    expect(rows[0].textContent).toContain("Newer table");
    expect(rows[0].textContent).toContain("2026-07-30");
    expect(rows[1].textContent).toContain("2025-04-28");
  });

  it("isolates provider failures while retaining healthy results", async () => {
    api.searchTables.mockResolvedValueOnce({
      results: [result],
      failures: [{ provider: "fearless", error: "HTTP 403 challenge" }],
      sources: [
        { provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 0, status: "unavailable", error: "HTTP 403 challenge" },
        { provider: "opencheattables", provider_display_name: "Open Cheat Tables", results: 1, status: "ok", error: null },
        { provider: "playground", provider_display_name: "Playground", results: 0, status: "ok", error: null },
      ],
      stale: false,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    // A blocked source keeps its own visible count rather than vanishing, so a
    // provider that quietly stops working is distinguishable from a game with
    // no tables. The dead "continue in the browser" handoff is gone.
    // Every searched source is named and counted, including one that answered
    // with nothing: silence and blockage must not look the same.
    expect(await screen.findByText(/FearLess: n\/a/)).toBeTruthy();
    expect(screen.getByText(/OpenCT: 1/)).toBeTruthy();
    expect(screen.getByText(/Playground: 0/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Example\.CT/ })).toBeTruthy();
  });

  it("puts the state of the indexed listing behind the summary row's own press", async () => {
    // That source has no search route this project may use, so this device
    // reads its listing pages and searches the copy. A table on a page it has
    // not read yet is absent from the copy rather than from the source, which
    // is the one thing a result count cannot say and the reader can act on.
    api.searchTables.mockResolvedValueOnce({
      results: [result], failures: [],
      sources: [{
        provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 1,
        status: "indexing", error: null,
        indexed_pages: 12, total_pages: 42, indexed_topics: 900, stale_pages: 30,
        refresh_age_seconds: 86_400, fully_refreshed_at: null,
        last_refresh_at: Math.round(Date.now() / 1000) - 600, last_refresh_pages: 5,
        retry_after_seconds: null,
      }],
      stale: false,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByTestId("search-controls");

    // Not on the row: it is one line, and this is what the press is for.
    expect(row.textContent).not.toMatch(/listing pages are indexed/);
    fireEvent.click(within(row).getByRole("button", { name: "?" }));
    const opened = screen.getByTestId("search-controls").textContent ?? "";
    expect(opened).toMatch(/12 of its 42 listing pages are indexed here, holding 900 tables/);
    expect(opened).toMatch(/30 pages have still to be read/);
    expect(opened).toMatch(/The last pass read 5 pages/);
  });

  it("names a switched-off source as off rather than dropping it from the roster", async () => {
    // A narrowed search and a build that never had those sources produce the
    // same empty answer otherwise, and only one of them is about this game.
    api.searchTables.mockResolvedValueOnce({
      results: [result], failures: [],
      sources: [
        { provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 1, status: "ok", error: null },
        { provider: "github", provider_display_name: "GitHub", results: 0, status: "disabled", error: null },
      ],
      stale: false,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    expect(await screen.findByText(/GitHub: off/)).toBeTruthy();
    expect(screen.getByText(/FearLess: 0\+1/)).toBeTruthy();
    expect(screen.queryByText("Every table source is switched off")).toBeNull();
  });

  it("says a search asked nothing at all rather than reporting no tables found", async () => {
    api.searchTables.mockResolvedValueOnce({
      results: [], failures: [],
      sources: [
        { provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 0, status: "disabled", error: null },
        { provider: "github", provider_display_name: "GitHub", results: 0, status: "disabled", error: null },
      ],
      stale: false,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    expect(await screen.findByText("Every table source is switched off")).toBeTruthy();
    expect(screen.getByText(/Switch a source back on under Advanced/)).toBeTruthy();
  });

  it("still lists this game's own tables when every source is switched off", async () => {
    // Switching every source off is a statement about the network, not about
    // the tables already on this device. The screen says nothing was searched
    // and still offers what is here, which is the whole of what it can offer.
    api.searchTables.mockResolvedValue({
      results: [], failures: [],
      sources: [
        { provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 0, status: "disabled", error: null },
        { provider: "github", provider_display_name: "GitHub", results: 0, status: "disabled", error: null },
      ],
      stale: false,
    });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    expect(await screen.findByText("Every table source is switched off")).toBeTruthy();
    const saved = await screen.findByRole("button", { name: /Saved Example\.CT.*Local/ });
    expect((saved as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(saved);
    renderLastModal();
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(onLocalSelected).toHaveBeenCalledWith(localTable.sha256);
  });

  it("does not restore a cached outcome after the set of sources has changed", async () => {
    // The cache is keyed by game and query alone, which was the whole of search
    // identity until a source could be switched off. Reopening Search then
    // restored rows from a source that is no longer asked, still looking
    // downloadable, and only refused by the backend on the press.
    api.searchTables.mockResolvedValue({
      results: [result], failures: [],
      sources: [{ provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 1, status: "ok", error: null }],
      stale: false,
    });
    const view = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" initialQuery="Example" autoSearch onImported={vi.fn()} />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(1));
    view.unmount();

    // Reopening without a change reuses the outcome and searches nothing.
    const reopened = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" initialQuery="Example" autoSearch onImported={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: /Example\.CT/ })).toBeTruthy());
    expect(api.searchTables).toHaveBeenCalledTimes(1);
    reopened.unmount();

    // A source switched off invalidates it, so reopening asks the backend.
    forgetSearchOutcomes();
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" initialQuery="Example" autoSearch onImported={vi.fn()} />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(2));
  });

  it("does not store an outcome searched under a set of sources that has since changed", async () => {
    // The clear above covers what is already cached. This covers what is still
    // in flight: an answer that lands after the change describes the old set.
    let release: (() => void) | null = null;
    const pending = new Promise((resolve) => {
      release = () => resolve({
        results: [result], failures: [],
        sources: [{ provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 1, status: "ok", error: null }],
        stale: false,
      });
    });
    api.searchTables.mockReturnValueOnce(pending);
    const view = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" initialQuery="Example" autoSearch onImported={vi.fn()} />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(1));

    forgetSearchOutcomes();
    release?.();
    await waitFor(() => expect(screen.getByRole("button", { name: /Example\.CT/ })).toBeTruthy());
    view.unmount();

    api.searchTables.mockResolvedValue({ results: [result], failures: [], sources: [], stale: false });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" initialQuery="Example" autoSearch onImported={vi.fn()} />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(2));
  });

  it("shows bounded FearLess background-index progress separately from result count", async () => {
    api.searchTables.mockResolvedValueOnce({
      results: [result], failures: [],
      sources: [
        { provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 0, status: "indexing", error: null, indexed_pages: 7, total_pages: 331, indexed_topics: 350 },
        { provider: "opencheattables", provider_display_name: "Open Cheat Tables", results: 1, status: "ok", error: null },
        { provider: "playground", provider_display_name: "Playground", results: 0, status: "ok", error: null },
      ],
      stale: false,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    expect(await screen.findByText(/FearLess: indexing 7\/331 · 0/)).toBeTruthy();
  });

  it("opens a download window for every source, not only the ones that serve their own wait", async () => {
    // A download used to run where it was started: the press disabled the
    // screen and put what was happening in a row under the results, which is
    // several screens below the focus for most of a list, so the panel looked
    // as though it had stopped answering. Only the two sources that make the
    // user wait out their own countdown got a window, on the ground that a
    // minute-long wait has to be readable - and the same is true of a download,
    // which also ends in a failure worth reading or in the review screen.
    const waiting = { ...waitingAcquisition, provider: result.provider, artifact_id: result.artifact_id };
    api.startTableAcquisition.mockResolvedValueOnce(waiting);
    api.pollTableAcquisition.mockResolvedValue(waiting);
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));

    await waitFor(() => expect(showModal).toHaveBeenCalled());
    // Nothing about the download is left behind in the list under it.
    expect(screen.queryByText(/42s remaining/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Cancel acquisition" })).toBeNull();
    const modal = renderLastModal();
    expect(modal.getByText("42s remaining")).toBeTruthy();
    expect(modal.getByRole("button", { name: "Cancel acquisition" })).toBeTruthy();
  });

  it("shows one dialog for one failure however many boundaries report it", async () => {
    // A press from this screen routinely crosses two reporters: the panel's own
    // `runAction` shows the failure and rethrows, and this screen shows it
    // again, so a failed import or a failed Clear opened two dialogs the user
    // had to dismiss in turn.
    const cause = new Error("the table could not be associated");
    expect(showActionFailure("The last thing you pressed", cause)).toBe(true);
    expect(showModal).toHaveBeenCalledTimes(1);
    expect(showActionFailure("Searching or downloading a table", cause)).toBe(true);
    expect(showModal).toHaveBeenCalledTimes(1);

    // A different failure is still a failure of its own.
    expect(showActionFailure("Searching or downloading a table", new Error("and another"))).toBe(true);
    expect(showModal).toHaveBeenCalledTimes(2);
  });

  it("cancels a started acquisition if the window that would own it cannot open", async () => {
    // The download is already running on the backend by the time the window is
    // asked for, so a window that cannot open must not leave it running with
    // nothing on screen that could report or stop it.
    const playground = { ...result, provider: "playground", provider_display_name: "Playground", artifact_id: "page-1:file-2" };
    const downloading = { ...waitingAcquisition, state: "downloading", provider_wait_seconds: null };
    api.searchTables.mockResolvedValueOnce({ results: [playground], failures: [], sources: [], stale: false });
    api.startTableAcquisition.mockResolvedValueOnce(downloading);
    (showModal as any).mockImplementationOnce(() => { throw new Error("modal unavailable"); });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(api.cancelTableAcquisition).toHaveBeenCalledWith(downloading.acquisition_id));
    expect(await screen.findByText("modal unavailable")).toBeTruthy();
  });

  it("marks the row it just imported from without being handed new marks", async () => {
    // The marks this screen is given are read once, when it opens. That is
    // normally enough because a successful import goes straight to Review and
    // closes the screen, and it is not enough exactly when the user comes back
    // here: something after the import failed, the table is on the device, and
    // the row that produced it was offering the same download over again.
    api.startTableAcquisition.mockResolvedValue({
      acquisition_id: "a".repeat(32), provider: result.provider, artifact_id: result.artifact_id, filename: result.filename,
      state: "ready_to_import", error: null, bytes_received: 100, expected_bytes: 100, provider_wait_seconds: null,
      source_page: null, inspection: { format: "ct", members: [{ path: result.filename, size: 100, packed_size: null, encrypted: false, format: "ct" }] },
      imported: null, execution_consent: null,
    });
    api.completeTableAcquisition.mockResolvedValue({
      acquisition_id: "a".repeat(32), provider: result.provider, artifact_id: result.artifact_id, filename: result.filename,
      state: "imported", error: null, bytes_received: 100, expected_bytes: 100, provider_wait_seconds: null,
      source_page: null, inspection: null, imported: { sha256: "b".repeat(64), filename: result.filename }, execution_consent: false,
    });
    // The handoff the window makes after an import is what fails here, which is
    // the whole reason this screen is still there to be looked at.
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn().mockRejectedValue(new Error("review failed"))} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());
    renderLastModal();
    await waitFor(() => expect(api.completeTableAcquisition).toHaveBeenCalled());

    const row = await screen.findByRole("button", { name: /Imported.*Example\.CT/ });
    expect(row.textContent).toContain("Imported");
  });

  it("stops offering an artifact whose exact bytes are a damaged table", async () => {
    api.startTableAcquisition.mockResolvedValue({
      acquisition_id: "a".repeat(32), provider: result.provider, artifact_id: result.artifact_id, filename: result.filename,
      state: "failed", error: "the file is not a valid Cheat Engine table: its XML is malformed (mismatched tag: line 393, column 10). The file is damaged at its source; choose a different table.",
      bytes_received: 100, expected_bytes: 100, provider_wait_seconds: null, source_page: null,
      inspection: null, imported: null, execution_consent: null, artifact_rejected: true,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));

    // The window says what happened, and the list learns it when the window
    // closes: the outcome of a download belongs to the thing that ran it.
    await waitFor(() => expect(showModal).toHaveBeenCalled());
    const modal = renderLastModal();
    expect(modal.getByText(/choose a different table/)).toBeTruthy();
    fireEvent.click(modal.getByRole("button", { name: "Close" }));

    const row = await screen.findByRole("button", { name: /Damaged/ });
    expect((row as HTMLButtonElement).disabled).toBe(true);
    expect(api.startTableAcquisition).toHaveBeenCalledTimes(1);
  });

  it("keeps a retired artifact marked across a fresh search", async () => {
    api.startTableAcquisition.mockResolvedValue({
      acquisition_id: "a".repeat(32), provider: result.provider, artifact_id: result.artifact_id, filename: result.filename,
      state: "failed", error: "the file is not a valid Cheat Engine table: its XML is malformed (mismatched tag: line 393, column 10).",
      bytes_received: 100, expected_bytes: 100, provider_wait_seconds: null, source_page: null,
      inspection: null, imported: null, execution_consent: null, artifact_rejected: true,
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());
    fireEvent.click(renderLastModal().getByRole("button", { name: "Close" }));
    expect((await screen.findByRole("button", { name: /Damaged/ }) as HTMLButtonElement).disabled).toBe(true);

    // Searching again is how a user looks for a different table, not a claim
    // that the broken one was fixed - and this screen searches by itself when
    // it is opened, so clearing here made the mark last until the next glance
    // at it and no longer. Retrying is its own press.
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(2));
    expect((await screen.findByRole("button", { name: /Damaged/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("says what a long search is doing rather than only how long it has taken", async () => {
    // A search is tens of seconds of somebody else's network and the panel
    // could say only "45s". One measured on the development device took
    // exactly that, and 44.8 of it was Playground alone, which is the fact the
    // user needed and had no way to see.
    let finish!: (value: unknown) => void;
    api.searchTables.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    api.pollTableSearch.mockResolvedValue({
      schema: 1,
      running: true,
      elapsed_ms: 12000,
      sources: [
        { provider: "fearless", name: "FearLess Cheat Engine", state: "running", stage: "reading topic 3 of 40" },
        { provider: "playground", name: "Playground", state: "done", stage: null },
        { provider: "github", name: "GitHub", state: "failed", stage: null },
        { provider: "vgtimes", name: "VGTimes", state: "off", stage: null },
      ],
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });

    // Nothing is asked for while the wait is still an ordinary one: a line that
    // appears and vanishes again is worse than the spinner beside it. The
    // window is short, because a Steam Deck's searches were 4.3, 6.7 and 12.4
    // seconds and a ten second threshold explained none of the first two.
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(api.pollTableSearch).not.toHaveBeenCalled();
    expect(screen.getByTestId("search-controls").textContent).toMatch(/Searching sources · \d+s/);

    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(api.pollTableSearch).toHaveBeenCalled();
    // What is still out is named with what it is doing; what has answered is
    // counted, because the row has to stay readable on a quick-access panel.
    expect(screen.getByText(/FearLess: reading topic 3 of 40/)).toBeTruthy();
    expect(screen.getByText(/1 answered/)).toBeTruthy();
    expect(screen.getByText(/1 could not/)).toBeTruthy();
    // A source the user switched off is named while the narrowing is actually
    // costing them something, not only in the tally afterwards.
    expect(screen.getByText(/1 off/)).toBeTruthy();
    // And the count is of the sources this search is asking, which is the
    // roster it is running against rather than the one before it.
    expect(screen.getByText(/Searching 3 sources/)).toBeTruthy();

    await act(async () => {
      finish({ results: [result], failures: [], stale: false });
      await Promise.resolve();
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
    // And it goes away with the search, rather than describing one that ended.
    expect(screen.queryByText(/reading topic 3 of 40/)).toBeNull();
  });

  it("never starts a progress read beside one that has not answered", async () => {
    // A read slower than the interval had another started beside it, and two
    // answers can land in the other order and put an older set of stages back
    // on the screen; a backend that has stalled also collects one outstanding
    // call every two seconds.
    let finish!: (value: unknown) => void;
    api.searchTables.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const pending: Array<(value: unknown) => void> = [];
    api.pollTableSearch.mockImplementation(() => new Promise((resolve) => { pending.push(resolve); }));
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });

    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    // Twenty seconds past the first read and it is still the only one.
    expect(pending).toHaveLength(1);

    await act(async () => {
      pending[0]({
        schema: 1, running: true, elapsed_ms: 30000,
        sources: [{ provider: "fearless", name: "FearLess Cheat Engine", state: "running", stage: "reading the forum listing" }],
      });
      await Promise.resolve();
    });
    expect(screen.getByText(/FearLess: reading the forum listing/)).toBeTruthy();

    // The next one is scheduled by that answer landing, not by the clock.
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(pending).toHaveLength(2);

    await act(async () => {
      finish({ results: [result], failures: [], stale: false });
      await Promise.resolve();
    });
  });

  it("follows the game it is now on rather than the search it was watching", async () => {
    // The token was held in a ref, so the read that uses it was built once and
    // never again while the screen kept saying it was searching. Changing game
    // while a long search was still pending left `searching` true across the
    // change, so the effect never re-ran: it went on asking about the search
    // for the game the user had just left, and the new game's search had
    // nothing to show for itself but a clock.
    const holds: Array<(value: unknown) => void> = [];
    api.searchTables.mockImplementation(() => new Promise((resolve) => { holds.push(resolve); }));
    const stageFor = (name: string) => ({
      schema: 1,
      running: true,
      elapsed_ms: 12000,
      sources: [{ provider: "fearless", name: "FearLess Cheat Engine", state: "running", stage: `reading ${name}` }],
    });
    api.pollTableSearch.mockImplementation(async (token: string) =>
      token === api.searchTables.mock.calls[api.searchTables.mock.calls.length - 1][1]
        ? stageFor("the second game")
        // The backend answers nothing to a search that is not this caller's,
        // which is what left the line with only a clock on it.
        : null);

    // Before the render, because the auto search starts on mount and the clock
    // that decides when the panel explains itself starts with it.
    vi.useFakeTimers();
    const view = render(<ProviderCatalog autoSearch gameIdentity="10:steam" gameName="First" onImported={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
    const first = api.searchTables.mock.calls[0][1];
    expect(api.pollTableSearch).toHaveBeenCalledWith(first);

    // The screen moves to another game while the first search is still out.
    view.rerender(<ProviderCatalog autoSearch gameIdentity="11:steam" gameName="Second" onImported={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });

    const second = api.searchTables.mock.calls[api.searchTables.mock.calls.length - 1][1];
    expect(second).not.toBe(first);
    // Every read from here is about the search this screen is now running.
    const asked = api.pollTableSearch.mock.calls.map(([token]: [string]) => token);
    expect(asked[asked.length - 1]).toBe(second);
    expect(screen.getByText(/FearLess: reading the second game/)).toBeTruthy();

    // And the first search answering late changes nothing on this screen.
    await act(async () => {
      holds[0]({ results: [], failures: [], stale: false });
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(2500);
    });
    expect(screen.getByText(/FearLess: reading the second game/)).toBeTruthy();
  });

  it("does not count this search by the roster of the one before it", async () => {
    // The completed roster describes the previous search, which is a different
    // set as soon as the user has switched a source off since: the line said
    // "Searching 5 sources" while three were being asked.
    api.searchTables.mockResolvedValueOnce({
      results: [result],
      failures: [],
      sources: Array.from({ length: 5 }, (_, index) => ({
        provider: `p${index}`, provider_display_name: `Source ${index}`, results: 1, status: "ok", error: null,
      })),
      stale: false,
    });
    const searchButton = () => screen.getByRole("button", { name: /^Search( again)?$/ }) as HTMLButtonElement;
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(searchButton());
    await screen.findByRole("button", { name: /Example\.CT/ });
    // Waited for the second press to be possible, not merely for the first
    // search's results to be on screen. The busy flag is cleared in the
    // enclosing runner's `finally`, a continuation after the results are
    // rendered, so the commit this find returns on can still have Search
    // disabled. A press onto a disabled control is dropped, no second search
    // starts, and the row goes on showing the first search's completed count,
    // which is this assertion failing for a reason that is not this panel.
    await waitFor(() => expect(searchButton().disabled).toBe(false));

    let finish!: (value: unknown) => void;
    api.searchTables.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    api.pollTableSearch.mockResolvedValue(null);
    vi.useFakeTimers();
    fireEvent.click(searchButton());
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });

    // No count at all until this search says what it is asking. Read off the
    // row rather than matched as a whole text node, and waited for rather than
    // asserted on the tick the clock happened to stop at: what this is about is
    // where the count comes from, not how many frames the panel needed.
    const controls = () => screen.getByTestId("search-controls").textContent ?? "";
    // Given room rather than caught on the tick the clock happened to stop at.
    // `waitFor` cannot be used while the timers are fake: it waits on a real
    // clock that nothing here advances.
    for (let attempt = 0; attempt < 20 && !/Searching sources/.test(controls()); attempt += 1) {
      await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    }
    // Compared as text, so a failure says what the row actually read.
    expect(controls()).toMatch(/Searching sources · \d+s/);
    expect(controls()).not.toMatch(/Searching \d+ sources/);

    await act(async () => {
      finish({ results: [result], failures: [], stale: false });
      await Promise.resolve();
    });
  });

  it("asks for its own search by name rather than for whatever ran last", async () => {
    // The backend keeps one record and allows two searches at once, so a
    // reader with no name for its own search is told about whichever started
    // last. The token this screen mints is what the record is answered to.
    let finish!: (value: unknown) => void;
    api.searchTables.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    api.pollTableSearch.mockResolvedValue(null);
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });

    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
    const token = api.searchTables.mock.calls[0][1];
    expect(token).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
    expect(api.pollTableSearch).toHaveBeenCalledWith(token);

    await act(async () => {
      finish({ results: [result], failures: [], stale: false });
      await Promise.resolve();
    });
  });

  it("does not call an import or a cancel a search", async () => {
    // Every press on this screen sets the same busy flag, and the status line
    // read that as searching: a download, a Retry or a cancel was labelled
    // "Searching 5 sources" with a clock running beside it.
    api.startTableAcquisition.mockReturnValueOnce(new Promise(() => undefined));
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const table = await screen.findByRole("button", { name: /Example\.CT/ });

    vi.useFakeTimers();
    fireEvent.click(table);
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });

    expect(screen.queryByText(/Searching/)).toBeNull();
    // The results the user is looking at are still described.
    expect(screen.getByText(/1 table\(s\)/)).toBeTruthy();
  });

  it("asks what a search is doing only while one is running", async () => {
    // Every press on this screen sets the same busy flag, and a download is a
    // long wait too: asking the backend what a search is doing while one runs
    // is a read every two seconds that can only answer about something else.
    api.startTableAcquisition.mockReturnValueOnce(new Promise(() => undefined));
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const table = await screen.findByRole("button", { name: /Example\.CT/ });
    api.pollTableSearch.mockClear();

    vi.useFakeTimers();
    fireEvent.click(table);
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(20000); });

    expect(api.pollTableSearch).not.toHaveBeenCalled();
  });

  it("never describes this search with the record of the one before it", async () => {
    // The backend names the sources when the jobs are built, which is a moment
    // into the search, so an answer that arrives before that is the previous
    // search's record. It is not running, and this one is.
    let finish!: (value: unknown) => void;
    api.searchTables.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    api.pollTableSearch.mockResolvedValue({
      schema: 1,
      running: false,
      elapsed_ms: 44000,
      sources: [{ provider: "fearless", name: "FearLess Cheat Engine", state: "done", stage: null }],
    });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });

    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
    expect(api.pollTableSearch).toHaveBeenCalled();
    expect(screen.getByText(/Searching sources · 12s$/)).toBeTruthy();
    expect(screen.queryByText(/answered/)).toBeNull();

    await act(async () => {
      finish({ results: [result], failures: [], stale: false });
      await Promise.resolve();
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByRole("button", { name: /Example\.CT/ })).toBeTruthy();
  });

  it("keeps searching when the progress read fails", async () => {
    // Nothing about the search depends on this line, so a read that fails
    // leaves the elapsed time on screen rather than replacing it with an error.
    let finish!: (value: unknown) => void;
    api.searchTables.mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    api.pollTableSearch.mockRejectedValue(new Error("backend is restarting"));
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    // Let the press commit before the clock moves. A search is started from
    // an async action, so which of its state updates have landed by the time
    // the timers are advanced is otherwise left to microtask interleaving.
    await act(async () => { await Promise.resolve(); });

    await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
    expect(api.pollTableSearch).toHaveBeenCalled();
    expect(screen.getByTestId("search-controls").textContent).toMatch(/Searching sources · \d+s/);
    expect(screen.queryByText(/backend is restarting/)).toBeNull();

    await act(async () => {
      finish({ results: [result], failures: [], stale: false });
      await Promise.resolve();
    });
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByRole("button", { name: /Example\.CT/ })).toBeTruthy();
  });

  it("keeps a healthy download going when one status read is refused", async () => {
    // A failed read of the state is not a failed download. Writing `failed`
    // here did not only mislabel it: unmounting the window cancels anything
    // not terminal, so one refused RPC became the abort of a transfer that was
    // still running, and every provider download goes through this window.
    api.startTableAcquisition.mockResolvedValue(waitingAcquisition);
    api.pollTableAcquisition
      .mockRejectedValueOnce(new Error("the backend did not answer"))
      .mockResolvedValue({ ...waitingAcquisition, state: "downloading", bytes: 4096 });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());

    const modal = renderLastModal();
    // The read is retried, the download it was reading is untouched, and the
    // window never claimed it had failed.
    await waitFor(() => expect(api.pollTableAcquisition.mock.calls.length).toBeGreaterThan(1));
    await waitFor(() => expect(modal.queryByText("the backend did not answer")).toBeNull());
    expect(api.cancelTableAcquisition).not.toHaveBeenCalled();
  });

  it("never turns an unreadable status into a cancelled download", async () => {
    // Retrying one refused read was only half of it. Once the budget ran out
    // the window still wrote `failed` into the acquisition, which is not a
    // label but an action: a terminal status turns the only press here into
    // Close, and Close cancels what it closes. A transfer that was running
    // perfectly well behind several refused status reads was therefore
    // cancelled because this window had stopped being able to look at it.
    api.startTableAcquisition.mockResolvedValue(waitingAcquisition);
    api.pollTableAcquisition.mockRejectedValue(new Error("the backend did not answer"));
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());

    const modal = renderLastModal();
    // Every retry in the budget is spent, and then the window says what it can
    // and cannot know, in a row of its own.
    await waitFor(() => expect(modal.getByTestId("acquisition-status-unreadable")).toBeTruthy(), { timeout: 15_000 });
    expect(modal.getByTestId("acquisition-status-unreadable").textContent).toContain("has not been stopped");
    // The acquisition is untouched: not terminal, so the press is still the one
    // that stops it on purpose rather than one that stops it on the way out.
    expect(modal.getByRole("button", { name: "Cancel acquisition" })).toBeTruthy();
    expect(modal.queryByRole("button", { name: "Close" })).toBeNull();
    expect(api.cancelTableAcquisition).not.toHaveBeenCalled();

    // And the same acquisition resumes the moment a read works again.
    api.pollTableAcquisition.mockResolvedValue({
      ...waitingAcquisition, state: "downloading", bytes_received: 4096,
    });
    fireEvent.click(modal.getByRole("button", { name: "Retry status" }));
    await waitFor(() => expect(modal.queryByTestId("acquisition-status-unreadable")).toBeNull());
    expect(api.startTableAcquisition).toHaveBeenCalledTimes(1);
    expect(api.cancelTableAcquisition).not.toHaveBeenCalled();
  }, 20_000);

  it("lets the user out of a download whose acquisition the backend has forgotten", async () => {
    // Close cancels what it closes, and cancelling an acquisition the backend
    // has already dropped raises the same error the poll did. Both presses this
    // window has, Close and the controller's Back, went down that path, so the
    // one answer that is terminal on arrival shut the user inside the window it
    // was reported in.
    api.startTableAcquisition.mockResolvedValue(waitingAcquisition);
    api.pollTableAcquisition.mockRejectedValue(new Error("acquisition is unknown or expired"));
    api.cancelTableAcquisition.mockRejectedValue(new Error("acquisition is unknown or expired"));
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());

    const modal = renderLastModal();
    const close = await modal.findByRole("button", { name: "Close" });
    fireEvent.click(close);
    const handle = vi.mocked(showModal).mock.results.at(-1)?.value as { Close: () => void };
    await waitFor(() => expect(handle.Close).toHaveBeenCalled());
    // Nothing was asked of the backend about an acquisition it says it does not
    // have: the window knew that before the press.
    expect(api.cancelTableAcquisition).not.toHaveBeenCalled();
  });

  it("leaves the list usable when the download it started loses its backend state", async () => {
    // The window polls the download it owns, so a lost acquisition is reported
    // there. What this screen has to guarantee is that it is not left holding
    // the press: the list stays searchable, because whatever went wrong went
    // wrong in front of the user rather than under a disabled screen.
    api.startTableAcquisition.mockResolvedValue(waitingAcquisition);
    // The backend's own sentence, which is the one answer that is terminal on
    // arrival: there is nothing left to poll and nothing left to cancel.
    api.pollTableAcquisition.mockRejectedValue(new Error("acquisition is unknown or expired"));
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());

    const modal = renderLastModal();
    await waitFor(() => expect(modal.getByText("acquisition is unknown or expired")).toBeTruthy());
    expect((screen.getByRole("button", { name: /^Search( again)?$/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("offers no browser step even if the backend still reports a handoff", async () => {
    // The browser handoff is not a controller workflow, so it is not presented
    // as one. A handoff state stays visible and cancellable instead of showing
    // buttons whose completion needs a keyboard and a mouse.
    api.startTableAcquisition.mockResolvedValue({ ...browserAcquisition, state: "browser_handoff" });
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());

    const modal = renderLastModal();
    expect(modal.getByText(/browser handoff/)).toBeTruthy();
    expect(modal.queryByRole("button", { name: "Detect completed download" })).toBeNull();
    expect(modal.queryByRole("button", { name: "Choose downloaded file" })).toBeNull();
    expect(modal.queryByRole("button", { name: "Open exact source page" })).toBeNull();
    fireEvent.click(modal.getByRole("button", { name: "Cancel acquisition" }));
    await waitFor(() => expect(api.cancelTableAcquisition).toHaveBeenCalledWith(browserAcquisition.acquisition_id));
  });

  it("does not restore a cancelled acquisition into a different game context", async () => {
    api.startTableAcquisition.mockResolvedValue(browserAcquisition);
    let resolveCancel!: (value: typeof browserAcquisition) => void;
    api.cancelTableAcquisition.mockReturnValueOnce(new Promise<typeof browserAcquisition>((resolve) => { resolveCancel = resolve; }));
    const view = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());
    fireEvent.click(renderLastModal().getByRole("button", { name: "Cancel acquisition" }));

    view.rerender(<ProviderCatalog gameIdentity="11:steam" gameName="Other Game" onImported={vi.fn()} />);
    await waitFor(() => expect((screen.getByRole("button", { name: /^Search( again)?$/ }) as HTMLButtonElement).disabled).toBe(false));
    resolveCancel({ ...browserAcquisition, state: "cancelled", source_page: null });
    await act(async () => { await Promise.resolve(); });

    // Nothing about the download that belonged to the game just left reaches
    // the list this screen is now showing.
    expect(screen.queryByText(/cancelled ·/)).toBeNull();
  });

  it("invalidates provider state when AppID changes even if the game name is identical", async () => {
    // Leaving a game takes its download with it. The window cancels what it
    // started when it unmounts, and closing a modal is asking Steam to unmount
    // it rather than unmounting it, so this screen cancels the acquisition it
    // handed over as well rather than resting the guarantee on that timing.
    api.startTableAcquisition.mockResolvedValue(browserAcquisition);
    const view = render(<ProviderCatalog gameIdentity="10:steam" gameName="Same Name" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());

    view.rerender(<ProviderCatalog gameIdentity="11:steam" gameName="Same Name" onImported={vi.fn()} />);

    await waitFor(() => expect(api.cancelTableAcquisition).toHaveBeenCalledWith(browserAcquisition.acquisition_id));
    expect(screen.queryByRole("button", { name: /Example\.CT/ })).toBeNull();
  });

  it("cancels a late nonterminal acquisition and releases busy state when game identity changes", async () => {
    let resolveStart!: (value: typeof browserAcquisition) => void;
    api.startTableAcquisition.mockReturnValue(new Promise<typeof browserAcquisition>((resolve) => { resolveStart = resolve; }));
    const view = render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());

    view.rerender(<ProviderCatalog gameIdentity="11:steam" gameName="Other Game" onImported={vi.fn()} />);
    await waitFor(() => expect((screen.getByRole("button", { name: /^Search( again)?$/ }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole("button", { name: /Example\.CT/ })).toBeNull();

    resolveStart(browserAcquisition);
    await waitFor(() => expect(api.cancelTableAcquisition).toHaveBeenCalledWith(browserAcquisition.acquisition_id));
  });

  it("greys a table already recorded as not working, even when it is already local", async () => {
    // The mark is durable and keyed by content: a fresh search does not make
    // bytes work, and the reason the local copy is on disk is that using it
    // failed. So the row stays visible, says why, and cannot be pressed.
    const onLocalSelected = vi.fn();
    api.searchTables.mockResolvedValue({
      // Advertised in upper case, recorded normalised: a case mismatch would
      // silently offer bytes already known not to work, and would equally
      // silently hide the local copy of them behind another download.
      results: [{ ...result, advertised_sha256: "F".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${"f".repeat(64)}`]: "f".repeat(64) }}
      blockedTables={{ ["f".repeat(64)]: { sha256: "f".repeat(64), reason: "Cheat Engine ran it and it went straight back off.", cause: "refused" as const } }}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: NOT_WORKING });
    expect((row as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(row);
    expect(onLocalSelected).not.toHaveBeenCalled();
  });

  it("marks a row by the provider it came from when no digest is advertised", async () => {
    // The whole point of the fix. Most results advertise no content digest at
    // all - the source these tables mostly come from advertises none - so a
    // mark that can only be found by digest was invisible on exactly the rows
    // it had been recorded from, and the same table was offered on every
    // search, downloaded again and proved broken again.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(blockedEntry({ reason: "it went straight back off" }))}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: NOT_WORKING });
    expect((row as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps a saved copy usable when the source no longer has the file", async () => {
    // Gone is a statement about the provider, not about this device. Retiring
    // the whole row on it took the copy the user had already downloaded with
    // it, which is exactly the copy they need when the source has stopped
    // serving one.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        // A file the source no longer has produced no bytes, so the record has
        // no digest and is keyed by the provider row itself.
        key: `${result.provider}:${result.artifact_id}`,
        sha256: null,
        reason: "the source no longer has this file",
        cause: "gone" as const,
      }))}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Local.*Example\.CT/ });
    expect((row as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(row);
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("the source no longer has this file");
    const saved = screen.getByRole("button", { name: "Use saved copy" });
    expect((saved as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(saved);
    expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64));
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
  });

  it("keeps a saved copy usable when the row's own bytes turned out not to be a table", async () => {
    // The record is against what the row serves. The copy already imported from
    // it was a table when it arrived, and still is.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        // Bytes that were not a table are never imported, so the digest on this
        // record is the download's and never the saved copy's.
        key: "c".repeat(64),
        sha256: "c".repeat(64),
        reason: "the download was not a Cheat Engine table",
        cause: "unusable" as const,
      }))}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Local.*Example\.CT/ });
    expect((row as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(row);
    renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64));
  });

  it("refuses the saved copy only for a mark against that copy's own bytes", async () => {
    // The other direction, and the one record that does say something about
    // this device: these exact bytes were run and did not work. The download is
    // untouched by it, because it is what might return different ones.
    //
    // One record reaches the row twice. It is keyed by its exact SHA and again
    // by every provider row it was downloaded from, so that the mark is visible
    // on a source that advertises no digest, and read as one answer that
    // projection refused the download along with the copy. A table refused for
    // an older build of the game is exactly the one whose source may have
    // published a fix, and re-downloading is how the user would find out.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry())}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect((row as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(row);
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("went straight back off");
    const download = screen.getByRole("button", { name: "Download again" });
    expect((download as HTMLButtonElement).disabled).toBe(false);
    // The ring opens on the half that can be pressed.
    expect(document.activeElement).toBe(download);
    fireEvent.click(download);
    expect(onLocalSelected).not.toHaveBeenCalled();
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());
  });

  it("keeps a newer saved copy usable behind an older revision's refusal", async () => {
    // One provider row serves several revisions over the years and each can
    // earn its own record. An older revision that came straight back off is a
    // statement about those bytes: it must not stop the row offering the newer
    // copy this device actually holds, nor asking the source for what it serves
    // now, which is a third set of bytes neither record has seen.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), recorded_at: 1700000000,
        reason: "the older revision went straight back off",
      }))}
      onLocalSelected={onLocalSelected}
      onClearMarks={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Local.*Example\.CT/ });
    expect((row as HTMLButtonElement).disabled).toBe(false);
    // The row says Local and carries no mark, so there is nothing on it the
    // user could be asked to clear: history about a revision this row is not
    // offering is invisible here and must not be counted either.
    expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull();
    fireEvent.click(row);
    renderLastModal();
    const saved = screen.getByRole("button", { name: "Use saved copy" });
    expect((saved as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(saved);
    expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64));
  });

  it("lets the newest thing that happened to a row decide its download", async () => {
    // Two records on one row: a revision refused in the past, and the source
    // saying since then that it no longer has the file. Keeping one mark per
    // row let whichever was written last stand for both, so the source failure
    // could be hidden behind the older refusal and a download the durable
    // record should stop was offered.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(
        // In the order the backend publishes them, newest first, which is the
        // order that hid the source failure: the older record was written last
        // and stood for the row.
        blockedEntry({
          key: `${result.provider}:${result.artifact_id}`, sha256: null, cause: "gone" as const,
          recorded_at: 1750000000, reason: "the source no longer has this file",
        }),
        blockedEntry({
          key: "a".repeat(64), sha256: "a".repeat(64), recorded_at: 1700000000,
          reason: "the older revision went straight back off",
        }),
      )}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("the source no longer has this file");
    expect(modal.container.textContent).not.toContain("went straight back off");
    expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("lets a newer unusable download decide it too", async () => {
    // The same shape with the other kind of source failure, and written in the
    // order the record actually arrives in, oldest last.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(
        blockedEntry({
          key: "c".repeat(64), sha256: "c".repeat(64), cause: "unusable" as const,
          recorded_at: 1750000000, reason: "the download was not a Cheat Engine table",
        }),
        blockedEntry({
          key: "a".repeat(64), sha256: "a".repeat(64), recorded_at: 1700000000,
          reason: "the older revision went straight back off",
        }),
      )}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("not a Cheat Engine table");
    expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("still refuses a download the source has said it will serve", async () => {
    // The control. An advertised digest naming bytes already known not to work
    // is the one case where the source has said what the press would fetch, and
    // it is refused however old that record is.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "a".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), recorded_at: 1700000000,
        reason: "the older revision went straight back off",
      }))}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    // The row is presenting the stored copy, which carries no record of its
    // own, so it says what it can offer and makes no claim about the revision
    // the source now names. The refusal of that revision is one press away,
    // where the two sets of bytes can be told apart in words.
    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect(row.textContent).toContain("Local");
    expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull();
    fireEvent.click(row);
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("Downloading again is not available");
    expect(modal.container.textContent).toContain("went straight back off");
    expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("lets a fresh checksum answer for an older revision that was not a table", async () => {
    // `unusable` is written against the exact digest of what was downloaded. A
    // result advertising a different one is direct evidence that the record is
    // not about these bytes: the row was replaced, and refusing the new file
    // over the old one refuses bytes nothing here has ever seen.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "b".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), cause: "unusable" as const,
        reason: "the download was not a Cheat Engine table",
      }))}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(false);
    expect(modal.container.textContent).not.toContain("not a Cheat Engine table");
  });

  it("lets a fresh checksum answer for an older archive nothing could open", async () => {
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "b".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), cause: "encrypted" as const,
        reason: "the archive does not open",
      }))}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(false);
    expect(modal.container.textContent).not.toContain("the archive does not open");
  });

  it("still refuses the download when the fresh checksum names the blocked bytes", async () => {
    // The control for the two above. The source has said what it will serve and
    // it is exactly what did not work.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "a".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), cause: "unusable" as const,
        reason: "the download was not a Cheat Engine table",
      }))}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("not a Cheat Engine table");
  });

  it("keeps a source that names nothing conservative about an older failure", async () => {
    // Nothing here can tell an old failure from the current file, so the record
    // stands and Retry is what says to try the row anyway. The offline half is
    // untouched by that.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), cause: "unusable" as const,
        reason: "the download was not a Cheat Engine table",
      }))}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("not a Cheat Engine table");
    expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("says the checksum has changed rather than that there is none", async () => {
    // The source does publish one; it simply names other bytes now. Saying no
    // checksum exists is false, and it is the weaker of the two things that can
    // be said: "nobody can tell" against "the online version has changed".
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "b".repeat(64) }], failures: [], stale: false,
    });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    const modal = renderLastModal();
    expect(modal.container.textContent).toContain("advertises a different checksum");
    expect(modal.container.textContent).not.toContain("does not provide a checksum");
    expect((screen.getByRole("button", { name: "Download again" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64));
  });

  it("offers no Retry for a record the row has outlived", async () => {
    // The source replaced its file and advertised a checksum for it, so the old
    // record is about a revision this row no longer offers: nothing is refused,
    // the row says nothing, and both presses are live. Offering Retry over that
    // asked the user to clear a record the screen was not showing them.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "b".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), cause: "unusable" as const,
        reason: "the download was not a Cheat Engine table",
      }))}
      onClearMarks={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect((row as HTMLButtonElement).disabled).toBe(false);
    expect(row.textContent).not.toContain("Not a table");
    expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull();
  });

  it("offers no Retry for an outlived archive record either", async () => {
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "b".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), cause: "encrypted" as const,
        reason: "the archive does not open",
      }))}
      onClearMarks={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect(row.textContent).not.toContain("Encrypted");
    expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull();
  });

  it("clears the record the row is showing and leaves the other revision's alone", async () => {
    // The source has said it will serve exactly the bytes that did not work, so
    // the row is presenting that record and Retry is what says to try it again.
    // One press clears those exact bytes and no others: a row can have earned a
    // record per revision over the years, and they are statements about
    // different tables. The record about the revision this row is no longer
    // offering stays where it is, which is the device-wide list under Advanced,
    // rather than being dropped by a press that never named it.
    const current = "a".repeat(64);
    const older = "d".repeat(64);
    let entries = [
      blockedEntry({ key: current, sha256: current, cause: "unusable" as const,
        recorded_at: 1750000000, reason: "the download was not a Cheat Engine table" }),
      blockedEntry({ key: older, sha256: older, recorded_at: 1700000000,
        reason: "an older revision went straight back off" }),
    ];
    const marks = () => blockedTableLookups(entries);
    const onClearMarks = vi.fn(async (digests: string[]) => {
      entries = entries.filter((entry) => !digests.includes(entry.sha256));
    });
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: current }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      blockedTables={marks().byDigest}
      blockedArtifacts={marks().byArtifact}
      onClearMarks={onClearMarks}
      onRefreshBlocked={async () => marks()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    await screen.findByRole("button", { name: /Not a table.*Example\.CT/ });
    fireEvent.click(screen.getByRole("button", { name: /^Retry 1$/ }));
    await waitFor(() => expect(onClearMarks).toHaveBeenCalledTimes(1));
    expect(onClearMarks.mock.calls[0][0]).toEqual([current]);
    expect(entries.map((entry) => entry.sha256)).toEqual([older]);

    // The row is carrying nothing now: what it is offering is unblocked, and a
    // record about bytes it is not offering was never this row's to show.
    await waitFor(() => expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull());
    const row = screen.getByRole("button", { name: /Example\.CT/ });
    expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull();
    expect(row.textContent).not.toContain("Not a table");
    expect(onClearMarks).toHaveBeenCalledTimes(1);
  });

  it("keeps Retry where the source names nothing and the record still stands", async () => {
    const onClearMarks = vi.fn().mockResolvedValue(undefined);
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(blockedEntry({
        key: "a".repeat(64), sha256: "a".repeat(64), cause: "unusable" as const,
        reason: "the download was not a Cheat Engine table",
      }))}
      onClearMarks={onClearMarks}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    await screen.findByRole("button", { name: /Not a table.*Example\.CT/ });
    fireEvent.click(screen.getByRole("button", { name: /^Retry 1$/ }));
    await waitFor(() => expect(onClearMarks).toHaveBeenCalledWith(["a".repeat(64)]));
  });

  it("leaves a row from a different provider row alone", async () => {
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(blockedEntry({ reason: "it went straight back off", origins: ["fearless:topic-9:attachment-9"] }))}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    expect(((await screen.findByRole("button", { name: /Example\.CT/ })) as HTMLButtonElement).disabled).toBe(false);
  });

  it("opens a marked local row so its download is still reachable", async () => {
    // A standalone saved table whose exact bytes are refused. Disabling the
    // whole row left the one press that could replace those bytes behind a
    // control nothing could reach, on the row that most needs it.
    api.searchTables.mockResolvedValue({ results: [], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      {...blockedProps(blockedEntry({ origins: [] }))}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Saved Example\.CT/ });
    expect(within(row).getByLabelText(NOT_WORKING)).toBeTruthy();
    // The two facts stand together and neither is the other's to take away: the
    // bytes are here, and the press that uses this exact copy is refused until
    // the record is cleared.
    expect(row.textContent).toContain("Local");
    expect((row as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(row);
    const modal = renderLastModal();
    expect((screen.getByRole("button", { name: "Use saved copy" }) as HTMLButtonElement).disabled).toBe(true);
    expect(modal.container.textContent).toContain("went straight back off");
    const download = screen.getByRole("button", { name: "Download again" });
    expect((download as HTMLButtonElement).disabled).toBe(false);
    expect(document.activeElement).toBe(download);
  });

  it("offers to retry exactly the marked tables on this screen, and only when there are some", async () => {
    // Clearing the whole record to retry one table is a much larger thing than
    // it looks on a screen showing one game, so the press names its count and
    // hands back only those digests.
    const onClearMarks = vi.fn().mockResolvedValue(undefined);
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    const view = render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(blockedEntry({ reason: "it went straight back off" }))}
      onClearMarks={onClearMarks}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /^Retry 1$/ }));
    await waitFor(() => expect(onClearMarks).toHaveBeenCalledWith(["f".repeat(64)]));

    // Nothing marked on this page, nothing to explain: the control is absent.
    view.unmount();
    render(<ProviderCatalog
      gameIdentity="10:steam" gameName="Example" onClearMarks={onClearMarks} onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    await screen.findByRole("button", { name: /Example\.CT/ });
    expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull();
  });

  it.each([false, true])("refreshes detached rows after a failed clear receipt (partial=%s)", async (partial) => {
    const first = blockedEntry();
    const second = blockedEntry({ key: "e".repeat(64), sha256: "e".repeat(64), origins: ["opencheattables:other"] });
    let entries = [first, second];
    api.searchTables.mockResolvedValue({ results: [
      { ...result, advertised_sha256: first.sha256 },
      { ...result, artifact_id: "other", filename: "Other.CT", table_title: "Other", advertised_sha256: second.sha256 },
    ], failures: [], stale: false });
    const lookups = blockedTableLookups(entries);
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" autoSearch
      blockedTables={lookups.byDigest} blockedArtifacts={lookups.byArtifact}
      onClearMarks={async () => { entries = partial ? [second] : []; throw new Error("receipt lost"); }}
      onRefreshBlocked={async () => blockedTableLookups(entries)} onImported={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry 2", exact: true }));
    await waitFor(() => expect(screen.queryAllByLabelText(NOT_WORKING)).toHaveLength(partial ? 1 : 0));
    expect((screen.getByRole("button", { name: /Example.CT/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("un-greys the row it just cleared instead of leaving it marked", async () => {
    // This screen is opened with `showModal`, so it never sees its props change
    // and the marks it was handed are the record as it stood at open. Without
    // re-reading them, clearing one left the row it was on greyed out and still
    // counted, so the press looked like it had done nothing at all.
    const onClearMarks = vi.fn().mockResolvedValue(undefined);
    const onRefreshBlocked = vi.fn().mockResolvedValue({ byDigest: {}, byArtifact: {} });
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(blockedEntry())}
      onClearMarks={onClearMarks}
      onRefreshBlocked={onRefreshBlocked}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    expect((await screen.findByRole("button", { name: NOT_WORKING }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(await screen.findByRole("button", { name: /^Retry 1$/ }));

    await waitFor(() => expect(onRefreshBlocked).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByLabelText(NOT_WORKING)).toBeNull());
    expect(((await screen.findByRole("button", { name: /Example\.CT/ })) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull();
  });

  it("re-reads the marks after a download turns out not to be a table", async () => {
    // The backend records those bytes durably where they were staged, against
    // the provider row they came from. This screen was opened before that
    // existed, so without re-reading it would offer the row again on the next
    // look and the import would be refused after a second download.
    api.startTableAcquisition.mockResolvedValue({
      acquisition_id: "a".repeat(32), provider: result.provider, artifact_id: result.artifact_id, filename: result.filename,
      state: "failed", error: "the file is not a valid Cheat Engine table: its XML is malformed.",
      bytes_received: 100, expected_bytes: 100, provider_wait_seconds: null, source_page: null,
      inspection: null, imported: null, execution_consent: null, artifact_rejected: true,
    });
    const onRefreshBlocked = vi.fn().mockResolvedValue(blockedTableLookups([
      blockedEntry({ key: "e".repeat(64), sha256: "e".repeat(64), reason: "not a table", cause: "unusable" as const }),
    ]));
    render(<ProviderCatalog
      gameIdentity="10:steam" gameName="Example" onRefreshBlocked={onRefreshBlocked} onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    // The download runs in its own window, so the record it leaves reaches this
    // list when that window closes, along with the artifact it retired.
    await waitFor(() => expect(showModal).toHaveBeenCalled());
    fireEvent.click(renderLastModal().getByRole("button", { name: "Close" }));

    await waitFor(() => expect(onRefreshBlocked).toHaveBeenCalledTimes(1));
    // Now recognised by the durable record rather than only for this session,
    // and said as what that record is: bytes that were never a table, not a
    // table that ran and failed.
    expect(await screen.findByRole("button", { name: /Not a table/ })).toBeTruthy();
  });

  it("counts only the marked rows that are actually on the page", async () => {
    // The count used to come from every row the search returned, including
    // download modes a controller cannot drive - which are filtered out and
    // never rendered - and every later page. So the press named a number
    // nothing on screen accounted for, and cleared marks on tables the user had
    // never seen, which is exactly what scoping it to this screen was for.
    const onClearMarks = vi.fn().mockResolvedValue(undefined);
    const hidden = {
      ...result, artifact_id: "topic-9:attachment-9", filename: "Hidden.CT",
      download_mode: "browser_handoff", advertised_sha256: null,
    };
    const later = Array.from({ length: 8 }, (_, index) => ({
      ...result, artifact_id: `topic-2:attachment-${index}`, filename: `Later${index}.CT`, advertised_sha256: null,
    }));
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: null }, hidden, ...later], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(
        // On page 1 and visible.
        blockedEntry({ key: "a".repeat(64), sha256: "a".repeat(64) }),
        // Never rendered at all: its download mode is not one a controller can drive.
        blockedEntry({ key: "b".repeat(64), sha256: "b".repeat(64), origins: ["opencheattables:topic-9:attachment-9"] }),
        // On a later page.
        blockedEntry({ key: "c".repeat(64), sha256: "c".repeat(64), origins: ["opencheattables:topic-2:attachment-7"] }),
      )}
      onClearMarks={onClearMarks}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    fireEvent.click(await screen.findByRole("button", { name: /^Retry 1$/ }));
    await waitFor(() => expect(onClearMarks).toHaveBeenCalledWith(["a".repeat(64)]));
  });

  it("offers to retry a row retired by a damaged download", async () => {
    // That mark is the same statement about the same row, and a fresh search
    // stopped clearing it, so without this the row stayed inert for the rest of
    // the session with nothing able to free it.
    api.startTableAcquisition.mockResolvedValue({
      acquisition_id: "a".repeat(32), provider: result.provider, artifact_id: result.artifact_id, filename: result.filename,
      state: "failed", error: "the file is not a valid Cheat Engine table: its XML is malformed.",
      bytes_received: 100, expected_bytes: 100, provider_wait_seconds: null, source_page: null,
      inspection: null, imported: null, execution_consent: null, artifact_rejected: true,
    });
    const onClearMarks = vi.fn().mockResolvedValue(undefined);
    render(<ProviderCatalog
      gameIdentity="10:steam" gameName="Example" onClearMarks={onClearMarks} onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());
    fireEvent.click(renderLastModal().getByRole("button", { name: "Close" }));
    expect((await screen.findByRole("button", { name: /Damaged/ }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(await screen.findByRole("button", { name: /^Retry 1$/ }));

    // No durable record to clear: this one lives only in the session.
    await waitFor(() => expect(screen.queryByText(/^Damaged$/)).toBeNull());
    expect(onClearMarks).not.toHaveBeenCalled();
    expect(((await screen.findByRole("button", { name: /Example\.CT/ })) as HTMLButtonElement).disabled).toBe(false);
  });

  it("clears both records one failed download leaves, in one press", async () => {
    // A rejected acquisition writes twice: this session's own memory that the
    // row is damaged, and the durable record the backend keeps. Taking the
    // durable one and moving on left the session's behind, so the press cleared
    // half of it and put the same row straight back as Damaged, with a second
    // Retry beside it and nothing to say why.
    api.startTableAcquisition.mockResolvedValue({
      acquisition_id: "a".repeat(32), provider: result.provider, artifact_id: result.artifact_id, filename: result.filename,
      state: "failed", error: "the file is not a valid Cheat Engine table: its XML is malformed.",
      bytes_received: 100, expected_bytes: 100, provider_wait_seconds: null, source_page: null,
      inspection: null, imported: null, execution_consent: null, artifact_rejected: true,
    });
    const durable = blockedTableLookups([
      blockedEntry({ key: "e".repeat(64), sha256: "e".repeat(64), cause: "unusable" as const, reason: "not a table" }),
    ]);
    const onClearMarks = vi.fn().mockResolvedValue(undefined);
    const onRefreshBlocked = vi.fn()
      .mockResolvedValueOnce(durable)
      // The press cleared it, so the record is gone on the read after it.
      .mockResolvedValue({ byDigest: {}, byArtifact: {} });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      onClearMarks={onClearMarks}
      onRefreshBlocked={onRefreshBlocked}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));
    await waitFor(() => expect(showModal).toHaveBeenCalled());
    fireEvent.click(renderLastModal().getByRole("button", { name: "Close" }));

    // Both exist now, and the durable one is what the row says.
    await waitFor(() => expect(onRefreshBlocked).toHaveBeenCalledTimes(1));
    expect((await screen.findByRole("button", { name: /Not a table/ }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(await screen.findByRole("button", { name: /^Retry 1$/ }));

    await waitFor(() => expect(onClearMarks).toHaveBeenCalledWith(["e".repeat(64)]));
    await waitFor(() => expect(screen.queryByText(/^Not a table$/)).toBeNull());
    // And nothing of the session's own half is left: no Damaged, no second
    // press, and the row is pressable again.
    expect(screen.queryByText(/^Damaged$/)).toBeNull();
    expect(screen.queryByRole("button", { name: /^Retry \d+$/ })).toBeNull();
    expect(((await screen.findByRole("button", { name: /Example\.CT/ })) as HTMLButtonElement).disabled).toBe(false);
  });

  it("starts a download for the game the search was opened for", async () => {
    // The record a failed download writes is read months later in a list that
    // spans every game, and this press is the only thing that knows which game
    // the row was found for: the bytes never enter the catalog, and a row whose
    // file the source has lost produces no bytes at all.
    api.startTableAcquisition.mockResolvedValue(waitingAcquisition);
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" appId={10} onImported={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Example\.CT/ }));

    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalledWith(
      result.provider, result.artifact_id, null, 10,
    ));
  });

  it("says which of the things that happened to a retired row happened, in one mark", async () => {
    // One shared "marked as not working" said none of it and repeated the word
    // "marked" on a row whose greying already says it. These are three
    // different statements and a user acts on each differently: a table that
    // ran and did not work may work again after the game updates, bytes that
    // were never a table never will, and a file the source no longer has is
    // not about this device at all. The first of those is compatibility and is
    // the glyph; the other two are about the bytes or the source and stay
    // chips, because pretending they mean "this table is incompatible" is the
    // conflation this row exists to avoid.
    const rows = [
      { ...result, artifact_id: "topic-1:a", filename: "Refused.CT", advertised_sha256: null },
      { ...result, artifact_id: "topic-1:b", filename: "Junk.CT", advertised_sha256: null },
      { ...result, artifact_id: "topic-1:c", filename: "Missing.CT", advertised_sha256: null },
      { ...result, artifact_id: "topic-1:d", filename: "Older.CT", advertised_sha256: null },
      { ...result, artifact_id: "topic-1:e", filename: "Locked.CT", advertised_sha256: null },
    ];
    api.searchTables.mockResolvedValue({ results: rows, failures: [], stale: false });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      {...blockedProps(
        blockedEntry({ key: "a".repeat(64), sha256: "a".repeat(64), reason: "it went straight back off", origins: ["opencheattables:topic-1:a"] }),
        blockedEntry({ key: "b".repeat(64), sha256: "b".repeat(64), reason: "there is no table in it", cause: "unusable" as const, origins: ["opencheattables:topic-1:b"] }),
        blockedEntry({ key: "opencheattables:topic-1:c", sha256: null, reason: "the source no longer has it", cause: "gone" as const, origins: ["opencheattables:topic-1:c"] }),
        // Written before the cause was recorded: still true, no longer exact.
        blockedEntry({ key: "d".repeat(64), sha256: "d".repeat(64), reason: "did not switch on", cause: undefined, origins: ["opencheattables:topic-1:d"] }),
        // Not "Not a table": these bytes may hold a good one, and re-packing
        // the archive is something the user can act on.
        blockedEntry({ key: "e".repeat(64), sha256: "e".repeat(64), reason: "the archive does not open", cause: "encrypted" as const, origins: ["opencheattables:topic-1:e"] }),
      )}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    // The exact refusal and the legacy record from before the cause was written
    // down are both negative evidence about the table itself.
    await waitFor(() => expect(screen.getAllByLabelText(NOT_WORKING)).toHaveLength(2));
    for (const filename of ["Refused.CT", "Older.CT"]) {
      const row = screen.getByRole("button", { name: new RegExp(filename.replace(".", "\\.")) });
      expect(within(row).getByLabelText(NOT_WORKING)).toBeTruthy();
      expect(row.textContent).not.toContain("Failed");
      expect(row.textContent).not.toContain("Not working");
    }
    expect(screen.getByRole("button", { name: /Not a table.*Junk\.CT/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Gone.*Missing\.CT/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Encrypted.*Locked\.CT/ })).toBeTruthy();
    for (const filename of ["Junk.CT", "Missing.CT", "Locked.CT"]) {
      const row = screen.getByRole("button", { name: new RegExp(filename.replace(".", "\\.")) });
      expect(within(row).queryByLabelText(NOT_WORKING)).toBeNull();
    }
  });

  it("keeps a row's mark on the title's own line, where it cannot widen the panel", async () => {
    // A mark on a line of its own made a retired row taller than the rows
    // around it and pushed the detail line down, and a mark that is allowed to
    // claim width would widen the whole quick-access panel. It shares the
    // title's line: the mark is the fixed half and the title is the half that
    // gives way to an ellipsis.
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "a".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${"a".repeat(64)}`]: "a".repeat(64) }}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const chip = await screen.findByText("Local");
    const line = chip.parentElement as HTMLElement;
    expect(line.style.display).toBe("flex");
    // The title is on it, and the detail line below is not.
    const title = line.firstElementChild as HTMLElement;
    expect(title.textContent).toBe("Example");
    expect(line.textContent).toBe("ExampleLocal");
    // The half that gives way, and the half that does not.
    expect(title.style.flex).toBe("1 1 auto");
    expect(title.style.minWidth).toBe("0");
    expect(chip.style.flex).toBe("0 0 auto");
  });

  it("offers a table whose bytes are not the ones recorded as not working", async () => {
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "a".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      blockedTables={{ ["f".repeat(64)]: { sha256: "f".repeat(64), reason: "not a table", cause: "unusable" as const } }}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect((row as HTMLButtonElement).disabled).toBe(false);
  });

  it("contains a rejected local-table selection instead of leaking an unhandled promise", async () => {
    const onLocalSelected = vi.fn().mockRejectedValue(new Error("review failed"));
    api.searchTables.mockResolvedValue({
      results: [{ ...result, advertised_sha256: "f".repeat(64) }], failures: [], stale: false,
    });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${"f".repeat(64)}`]: "f".repeat(64) }}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const local = await screen.findByRole("button", { name: /Local.*Example\.CT/ });
    fireEvent.click(local);
    renderLastModal();
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(await screen.findByText("review failed")).toBeTruthy();
  });

  it("marks a row a table was imported from even where the source advertises no digest", async () => {
    // Two of the four sources advertise no content digest at all, so a table
    // already imported from one of them came back through every later search
    // with nothing on the row to say so, and the only way to find out was to
    // sit through the download again. The mark is the weaker of the two: it
    // says the row has been imported from before, and the press below is
    // unchanged, because the same artifact ID can serve changed bytes.
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      importedArtifacts={new Set([`${result.provider}:${result.artifact_id}`])}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect(row.textContent).toContain("Imported");
    expect(row.textContent).not.toContain("Local");
    expect((row as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(row);
    expect(onLocalSelected).not.toHaveBeenCalled();
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());
  });

  it("says Local rather than Imported when the digest proves the bytes", async () => {
    // Both are true of the same row once a digest is advertised, and only one
    // of them is worth saying: Local is the one that means the press is free.
    const same = { ...result, advertised_sha256: "f".repeat(64) };
    api.searchTables.mockResolvedValue({ results: [same], failures: [], stale: false });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${"f".repeat(64)}`]: "f".repeat(64) }}
      importedArtifacts={new Set([`${result.provider}:${result.artifact_id}`])}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect(row.textContent).toContain("Local");
    expect(row.textContent).not.toContain("Imported");
  });

  it("downloads a new revision instead of reopening older local bytes under the same artifact ID", async () => {
    // A provider can replace the bytes behind one artifact ID. Resolving the
    // local table by that ID first labelled the fresh result Local and opened
    // the previously imported revision without any download attempt.
    const revised = { ...result, advertised_sha256: "b".repeat(64) };
    api.searchTables.mockResolvedValue({ results: [revised], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{
        [`${result.provider}:${result.artifact_id}`]: "f".repeat(64),
        [`sha:${"f".repeat(64)}`]: "f".repeat(64),
      }}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect(row.textContent).not.toContain("Local");
    fireEvent.click(row);
    expect(onLocalSelected).not.toHaveBeenCalled();
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());
  });

  it("defaults to existing local bytes when the fresh result advertises the same exact digest", async () => {
    const same = { ...result, advertised_sha256: "f".repeat(64) };
    api.searchTables.mockResolvedValue({ results: [same], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${"f".repeat(64)}`]: "f".repeat(64) }}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const local = await screen.findByRole("button", { name: /Local.*Example\.CT/ });
    fireEvent.click(local);
    renderLastModal();
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    await waitFor(() => expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64)));
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
  });

  it("uses the editable query and reuses a local table the result proves by digest", async () => {
    const digest = { ...result, advertised_sha256: "f".repeat(64) };
    api.searchTables.mockResolvedValue({ results: [digest], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${"f".repeat(64)}`]: "f".repeat(64) }}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.change(screen.getByLabelText("Search query"), { target: { value: "Example Deluxe" } });
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledWith(
      { display_name: "Example Deluxe", shortcut_executable: null },
      expect.stringMatching(/^[A-Za-z0-9_-]{1,64}$/),
    ));
    const local = await screen.findByRole("button", { name: /Local.*Example\.CT/ });
    fireEvent.click(local);
    renderLastModal();
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64));
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
  });

  it("reacquires a result that advertises no digest at all", async () => {
    // Without a content digest the provider has proven nothing about these
    // bytes, and its artifact ID is not identity, so a previous import of the
    // same row must not stand in for a fresh one.
    api.searchTables.mockResolvedValue({ results: [result], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${"f".repeat(64)}`]: "f".repeat(64) }}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    expect(row.textContent).not.toContain("Local");
    fireEvent.click(row);
    expect(onLocalSelected).not.toHaveBeenCalled();
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());
  });

  it("offers the copy a no-digest row produced, and says the row cannot prove it", async () => {
    // The user has downloaded this table from this row already. Offline, that
    // copy is all they have, and refusing to reach it because the source
    // declines to publish a checksum decides something that is theirs. What the
    // missing checksum changes is what the choice says, not that there is one.
    api.searchTables.mockResolvedValue({ results: [result], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: /Local.*Example\.CT/ });
    fireEvent.click(row);
    const modal = renderLastModal();
    expect(modal.container.textContent).toContain("does not provide a checksum");
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64));
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
  });

  it("still downloads a no-digest row from the same choice", async () => {
    // The other half of the same press. Nothing about the copy already here
    // stops the row being asked what it holds now.
    api.searchTables.mockResolvedValue({ results: [result], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    renderLastModal();
    fireEvent.click(screen.getByRole("button", { name: "Download again" }));
    expect(onLocalSelected).not.toHaveBeenCalled();
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());
  });

  it("never offers another game's stored table as a result for this one", async () => {
    // Search rows are offers to use that table for the game being searched
    // for, and a table stored for some other game is not that. Handing the
    // device-wide list here put every one of them into this game's results as
    // a standalone Local row, which is the Stored picker leaking into the
    // game-specific surface.
    const mine = { ...localTable, sha256: "a".repeat(64), filename: "Mine.CT" };
    const theirs = { ...localTable, sha256: "b".repeat(64), filename: "Theirs.CT", origins: [] };
    api.searchTables.mockResolvedValue({ results: [], failures: [], stale: false });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[mine] as any}
      deviceTables={[mine, theirs] as any}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    await waitFor(() => expect(screen.getByRole("button", { name: /Mine\.CT/ })).toBeTruthy());
    expect(screen.queryByRole("button", { name: /Theirs\.CT/ })).toBeNull();
  });

  it("still answers a provider row from bytes stored for another game", async () => {
    // The device-wide list is not a source of rows, it is a digest lookup: a
    // row the user is already looking at, advertising a checksum that is
    // already on this device, is answered from the device rather than
    // downloaded again, whichever game happens to hold the association.
    const theirs = { ...localTable, sha256: "b".repeat(64), filename: "Theirs.CT", origins: [] };
    const advertised = { ...result, advertised_sha256: theirs.sha256 };
    api.searchTables.mockResolvedValue({ results: [advertised], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${theirs.sha256}`]: theirs.sha256 }}
      localTables={[] as any}
      deviceTables={[theirs] as any}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

    const row = await screen.findByRole("button", { name: /Example\.CT/ });
    fireEvent.click(row);
    renderLastModal();
    // The real stored table, by its own digest, rather than a fresh download.
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(onLocalSelected).toHaveBeenCalledWith(theirs.sha256);
    expect(api.startTableAcquisition).not.toHaveBeenCalled();
  });

  it("says the row has been downloaded from before when nothing usable is here", async () => {
    // No local table for this game, so there is nothing for the press to open
    // and it goes and gets one. The row still says it has been here before,
    // which is the whole of what the artifact ID establishes.
    api.searchTables.mockResolvedValue({ results: [result], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      importedArtifacts={new Set([`${result.provider}:${result.artifact_id}`])}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    const row = await screen.findByRole("button", { name: /Imported.*Example\.CT/ });
    expect(row.textContent).not.toContain("Local");
    fireEvent.click(row);
    expect(vi.mocked(showModal)).not.toHaveBeenCalled();
    expect(onLocalSelected).not.toHaveBeenCalled();
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());
  });

  it("offers the newest table one no-digest row produced, and keeps the rest reachable", async () => {
    // A post commonly serves every revision of the same table, so one row leaves
    // one local table per set of bytes. Which of them the row offers was
    // whichever the backend happened to list first; it is the most recently
    // retrieved one now, and the others keep their own rows.
    const older = {
      ...localTable,
      sha256: "a".repeat(64),
      filename: "Older Example.CT",
      origins: [{ ...localTable.origins[0], retrieved_at: "2026-01-01T00:00:00Z" }],
    };
    api.searchTables.mockResolvedValue({ results: [result], failures: [], stale: false });
    const onLocalSelected = vi.fn();
    // Listed oldest first, so "the first one encountered" would pick the wrong one.
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[older, localTable]}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    // The older revision is still on the screen as its own row.
    await screen.findByRole("button", { name: /Older Example\.CT.*Local/ });
    expect(screen.queryByRole("button", { name: /Saved Example\.CT.*Local.*on this device/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Local.*Example\.CT.*Open Cheat Tables/ }));
    renderLastModal();
    fireEvent.click(screen.getByRole("button", { name: "Use saved copy" }));
    expect(onLocalSelected).toHaveBeenCalledWith("f".repeat(64));
  });

  it("keeps the game's local tables usable when the provider search fails", async () => {
    api.searchTables.mockRejectedValue(new Error("network is unreachable"));
    const onLocalSelected = vi.fn();
    render(<ProviderCatalog
      autoSearch
      gameIdentity="10:steam"
      gameName="Example"
      localTables={[localTable]}
      onLocalSelected={onLocalSelected}
      onImported={vi.fn()}
    />);

    const local = await screen.findByRole("button", { name: /Saved Example\.CT.*Local/ });
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(1));
    fireEvent.click(local);
    renderLastModal();
    const existing = screen.getByRole("button", { name: "Use saved copy" });
    expect(document.activeElement).toBe(existing);
    fireEvent.click(existing);
    await waitFor(() => expect(onLocalSelected).toHaveBeenCalledWith(localTable.sha256));
  });

  it("offers an explicit fresh download for a provider row with a local copy", async () => {
    const same = { ...result, advertised_sha256: localTable.sha256 };
    api.searchTables.mockResolvedValue({ results: [same], failures: [], stale: false });
    render(<ProviderCatalog
      gameIdentity="10:steam"
      gameName="Example"
      localArtifacts={{ [`sha:${localTable.sha256}`]: localTable.sha256 }}
      localTables={[localTable]}
      onLocalSelected={vi.fn()}
      onImported={vi.fn()}
    />);
    fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Local.*Example\.CT/ }));
    renderLastModal();
    fireEvent.click(screen.getByRole("button", { name: "Download again" }));
    await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalledWith(
      same.provider, same.artifact_id, null, null,
    ));
  });

  it("does not reuse a cached directory-backed search after the shortcut target changes", async () => {
    const view = render(<ProviderCatalog
      autoSearch
      gameIdentity="path-sensitive:steam"
      gameName="Abbreviated Name"
      shortcutExecutable='"/games/First Game/First.exe"'
      onImported={vi.fn()}
    />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledWith(
      {
        display_name: "Abbreviated Name",
        shortcut_executable: '"/games/First Game/First.exe"',
      },
      expect.stringMatching(/^[A-Za-z0-9_-]{1,64}$/),
    ));

    view.rerender(<ProviderCatalog
      autoSearch
      gameIdentity="path-sensitive:steam"
      gameName="Abbreviated Name"
      shortcutExecutable='"/games/Second Game/Second.exe"'
      onImported={vi.fn()}
    />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledWith(
      {
        display_name: "Abbreviated Name",
        shortcut_executable: '"/games/Second Game/Second.exe"',
      },
      expect.stringMatching(/^[A-Za-z0-9_-]{1,64}$/),
    ));
    expect(api.searchTables).toHaveBeenCalledTimes(2);
  });

  describe("challenged providers", () => {
    it("reports a challenged source in the source counts and nowhere else", async () => {
      // Stable v1 skips a provider that refuses anonymous downloads. Browser use
      // is not a controller workflow, so an Open button here was both a dead end
      // and a contradiction of the design contract - and the banner that
      // replaced it only repeated the `n/a` already on the source line.
      api.searchTables.mockResolvedValue({
        results: [],
        failures: [{ provider: "opencheattables", error: "HTTP 403", handoff_url: "https://example.invalid/topic" }],
        stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
      fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

      expect(await screen.findByText(/OpenCT: n\/a/)).toBeTruthy();
      expect(screen.queryByTestId("provider-handoff-opencheattables")).toBeNull();
      expect(screen.queryByText(/is unavailable/)).toBeNull();
    });

    it("says a source is partial when it answered and then stopped", async () => {
      // The backend deliberately reports rows and unavailability together, for
      // a source that returns tables and then meets a rate limit or a
      // challenge. Rendered as `n/a`, one screen said a source had nothing
      // while showing its downloadable rows immediately below.
      api.searchTables.mockResolvedValue({
        results: [
          {
            provider: "fearless", provider_display_name: "FearLess Cheat Engine", topic_id: "1",
            artifact_id: "topic-1:attachment-1", table_title: "Example", filename: "Example.CT",
            version: null, size_bytes: 10, source_page: "https://fearlessrevolution.com/viewtopic.php?t=1",
            download_mode: "direct_https", match_score: 0.9, provider_rank: 100, stale: false,
          },
          {
            provider: "fearless", provider_display_name: "FearLess Cheat Engine", topic_id: "1",
            artifact_id: "topic-1:attachment-2", table_title: "Example 2", filename: "Example2.CT",
            version: null, size_bytes: 10, source_page: "https://fearlessrevolution.com/viewtopic.php?t=1",
            download_mode: "direct_https", match_score: 0.9, provider_rank: 100, stale: false,
          },
        ],
        failures: [{ provider: "fearless", error: "HTTP 429 provider cooldown" }],
        sources: [{
          provider: "fearless", provider_display_name: "FearLess Cheat Engine",
          results: 2, status: "unavailable", error: "HTTP 429 provider cooldown",
        }],
        stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
      fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

      expect(await screen.findByText(/FearLess: 2 · partial/)).toBeTruthy();
      expect(screen.queryByText(/FearLess: n\/a/)).toBeNull();
    });

    it("still says n/a for a source that answered nothing at all", async () => {
      api.searchTables.mockResolvedValue({
        results: [],
        failures: [{ provider: "fearless", error: "HTTP 429 provider cooldown" }],
        sources: [{
          provider: "fearless", provider_display_name: "FearLess Cheat Engine",
          results: 0, status: "unavailable", error: "HTTP 429 provider cooldown",
        }],
        stale: false,
      });
      render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" onImported={vi.fn()} />);
      fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

      expect(await screen.findByText(/FearLess: n\/a/)).toBeTruthy();
    });
  });

  describe("archive identity", () => {
    it("keeps a multi-member archive reopenable instead of routing back to one member", async () => {
      // The advertised digest identifies the archive, not whichever table inside
      // it was imported first. Collapsing it to that table made every other
      // member unreachable: the row opened the imported one and member selection
      // could never be reached again.
      const archiveSha = "a".repeat(64);
      api.searchTables.mockResolvedValue({
        results: [{ ...result, advertised_sha256: archiveSha }], failures: [], stale: false,
      });
      const onLocalSelected = vi.fn();
      render(<ProviderCatalog
        gameIdentity="10:steam"
        gameName="Example"
        // What Home builds for a table imported from a two-member archive.
        localArtifacts={{ [`sha:${"f".repeat(64)}`]: "f".repeat(64) }}
        onLocalSelected={onLocalSelected}
        onImported={vi.fn()}
      />);
      fireEvent.click(screen.getByRole("button", { name: /^Search( again)?$/ }));

      const row = await screen.findByRole("button", { name: /Example\.CT/ });
      expect(row.textContent).not.toContain("Local");
      fireEvent.click(row);
      expect(onLocalSelected).not.toHaveBeenCalled();
      await waitFor(() => expect(api.startTableAcquisition).toHaveBeenCalled());
    });

  });
});

describe("Table source selection", () => {
  const roster = (disabled: string[] = []) => ({
    schema: 1,
    sources: ["fearless", "playground", "github", "thecheatscript", "vgtimes"].map((provider) => ({
      provider,
      provider_display_name: provider,
      priority: 10,
      discovery: "search",
      linked_target: provider === "github" || provider === "fearless",
      enabled: !disabled.includes(provider),
      state: null,
      counters: null,
      last_error: null,
      last_http_status: null,
      last_latency_ms: null,
      last_throttle_wait_s: null,
      cooldown_seconds: 0,
    })),
    enabled_count: 5 - disabled.length,
    total: 5,
    updated_at: null,
    selection_reason: null,
    diagnostics_reason: null,
  });

  /** A rejection that carries no Python traceback: the reply was lost, not refused. */
  const lostReply = () => new Error("Failed to fetch");

  /** The backend's own name for a failure raised after the content was in place. */
  const durabilityUnknown = () => Object.assign(new Error("directory could not be synced"), {
    pythonTraceback: 'Traceback (most recent call last):\n  File "x", line 1\nDurabilityUnknownError: provider_sources.json was replaced',
  });

  beforeEach(() => {
    vi.clearAllMocks();
    forgetSearchOutcomes();
    api.searchTables.mockResolvedValue({
      results: [result], failures: [],
      sources: [{ provider: "fearless", provider_display_name: "FearLess Cheat Engine", results: 1, status: "ok", error: null }],
      stale: false,
    });
  });

  afterEach(cleanup);

  it("settles a switch whose reply was lost as the committed change it is", async () => {
    // The reconciliation path is the only one that reads the record back, and
    // it was testing one provider's desired position against every provider in
    // the roster: false for the other four, so a stored choice was reported as
    // a failed switch.
    api.setProviderEnabled.mockRejectedValue(lostReply());
    api.getProviderSources.mockResolvedValue(roster(["github"]));

    const settled = await commitSourceSelection(
      "not using GitHub",
      () => api.setProviderEnabled("github", false),
      sourceSwitched("github", false),
    );

    expect(api.getProviderSources).toHaveBeenCalledTimes(1);
    expect(settled.sources.find((source: any) => source.provider === "github").enabled).toBe(false);
  });

  it("settles a switch back on the same way", async () => {
    api.setProviderEnabled.mockRejectedValue(durabilityUnknown());
    api.getProviderSources.mockResolvedValue(roster());

    const settled = await commitSourceSelection(
      "using GitHub",
      () => api.setProviderEnabled("github", true),
      sourceSwitched("github", true),
    );

    expect(settled.enabled_count).toBe(5);
  });

  it("still reports a switch the backend really did refuse", async () => {
    // A Python traceback that is not the post-replace one is proof the backend
    // refused before anything was written, and reconciliation must not soften
    // it into a success.
    api.setProviderEnabled.mockRejectedValue(Object.assign(new Error("not switchable"), {
      pythonTraceback: 'Traceback (most recent call last):\n  File "x", line 1\nValueError: provider is not a switchable table source',
    }));
    api.getProviderSources.mockResolvedValue(roster());

    await expect(commitSourceSelection(
      "not using GitHub",
      () => api.setProviderEnabled("github", false),
      sourceSwitched("github", false),
    )).rejects.toThrow(/not switchable/);
    expect(api.getProviderSources).not.toHaveBeenCalled();
  });

  it("reports an unknown outcome when the record cannot be read back either", async () => {
    api.setProviderEnabled.mockRejectedValue(lostReply());
    api.getProviderSources.mockRejectedValue(new Error("state file vanished"));

    await expect(commitSourceSelection(
      "not using GitHub",
      () => api.setProviderEnabled("github", false),
      sourceSwitched("github", false),
    )).rejects.toBeInstanceOf(DurableOutcomeUnknownError);
  });

  it("never reads the authority a third time to produce its answer", async () => {
    // A commit returns having either written a snapshot or read one back to
    // settle itself. Asking again could only add a read whose failure would
    // report an established commit as a failed action.
    api.setProviderEnabled.mockResolvedValue(roster(["github"]));
    api.getProviderSources.mockRejectedValue(new Error("state file vanished"));

    const settled = await commitSourceSelection(
      "not using GitHub",
      () => api.setProviderEnabled("github", false),
      sourceSwitched("github", false),
    );

    expect(api.getProviderSources).not.toHaveBeenCalled();
    expect(settled!.enabled_count).toBe(4);
  });

  it("settles a counter reset on the record becoming readable again", async () => {
    // Not on the counters being empty: the reset is only offered while that
    // record is unreadable, and an emptiness test would be settled by whichever
    // search ran next rather than by the repair.
    api.resetProviderSources.mockRejectedValue(lostReply());
    api.getProviderSources.mockResolvedValue(roster());

    const settled = await commitSourceSelection(
      "the counters being cleared",
      () => api.resetProviderSources(),
      countsReadable,
    );

    expect(settled!.diagnostics_reason).toBeNull();
  });

  it("settles a reset of every source against the whole roster", async () => {
    api.resetProviderSources.mockRejectedValue(lostReply());
    api.getProviderSources.mockResolvedValue(roster());

    const settled = await commitSourceSelection(
      "using every table source",
      () => api.resetProviderSources(),
      everySourceOn,
    );

    expect(settled.sources.every((source: any) => source.enabled)).toBe(true);
  });

  it("forgets cached searches even when the switch had to be reconciled", async () => {
    // A commit whose reply was lost changed the same file a successful one
    // does, so leaving the cache alone on that path is exactly how an outcome
    // outlives the choice that invalidated it.
    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" initialQuery="Example" autoSearch onImported={vi.fn()} />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(1));
    cleanup();

    api.setProviderEnabled.mockRejectedValue(lostReply());
    api.getProviderSources.mockResolvedValue(roster(["github"]));
    await commitSourceSelection(
      "not using GitHub",
      () => api.setProviderEnabled("github", false),
      sourceSwitched("github", false),
    );

    render(<ProviderCatalog gameIdentity="10:steam" gameName="Example" initialQuery="Example" autoSearch onImported={vi.fn()} />);
    await waitFor(() => expect(api.searchTables).toHaveBeenCalledTimes(2));
  });
});
