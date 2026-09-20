import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  cancelTableAcquisition: vi.fn(),
  completeTableAcquisition: vi.fn(),
  listTableCode: vi.fn(),
  pollTableAcquisition: vi.fn(),
  readTableCode: vi.fn(),
}));

vi.mock("../src/api", () => api);
vi.mock("@decky/api", () => ({ toaster: { toast: vi.fn() } }));
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  DialogButton: ({ children, onClick, disabled, preferredFocus }: any) => <button disabled={disabled} data-preferred-focus={preferredFocus ? "true" : undefined} onClick={onClick}>{children}</button>,
  DropdownItem: ({ label, rgOptions, selectedOption, onChange, disabled }: any) => (
    <label>{label}<select aria-label={label} disabled={disabled} value={selectedOption ?? ""} onChange={(event) => onChange?.({ data: event.target.value })}>
      {rgOptions.map((option: any) => <option key={String(option.data)} value={String(option.data)}>{option.label}</option>)}
    </select></label>
  ),
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: React.forwardRef(({ children, navEntryPreferPosition, ...props }: any, ref: any) => <div ref={ref} data-testid={props["data-testid"]} data-flow-children={props["flow-children"]} data-nav-entry={navEntryPreferPosition}>{children}</div>),
  ModalRoot: ({ children, onCancel, onEscKeypress }: any) => <div>
    <button aria-label="Controller Back" onClick={onCancel}>Controller Back</button>
    {onEscKeypress ? <button aria-label="Escape" onClick={onEscKeypress}>Escape</button> : null}
    {children}
  </div>,
  PanelSection: ({ children }: any) => <section>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div data-panel-row="true">{children}</div>,
  Spinner: () => <span>Loading</span>,
  TextField: (props: any) => <label>{props.label}<input aria-label={props["aria-label"] ?? props.label} placeholder={props.placeholder} value={props.value} disabled={props.disabled} onChange={props.onChange} /></label>,
  gamepadDialogClasses: {
    Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription",
    FieldLeftColumn: "FieldLeftColumn", CompactPadding: "CompactPadding",
    WithBottomSeparatorStandard: "WithBottomSeparatorStandard",
    WithBottomSeparatorThick: "WithBottomSeparatorThick",
  },
}));

import { ArchiveImportModal } from "../src/modals/ArchiveImportModal";
import { GamePickerModal } from "../src/modals/GamePickerModal";
import { aggregateTableHolders } from "../src/tableHolders";
import { ImportedTablesModal } from "../src/modals/ImportedTablesModal";
import { TableAcquisitionModal } from "../src/modals/TableAcquisitionModal";
import { TableReviewModal } from "../src/modals/TableReviewModal";
import { manageCompatibility } from "../src/components/CompatibilityMark";
import { useFittedRows } from "../src/components/PanelDensity";
import { readSupportLog, resetSupportLog } from "../src/supportLog";

const SHA = "1".repeat(64);
const table = {
  sha256: SHA, filename: "Game.CT", size: 100, table_version: "45",
  has_lua: false, has_auto_assembler: false, has_embedded_files: false,
  executable_content: false, entry_count: 1, blob_path: "/tables/Game.CT",
  available: true, schema_version: 2, origins: [],
};
const inspection = {
  sha256: SHA, table_version: "45", total_entries: 1, has_lua: false,
  has_auto_assembler: false, has_forms: false, embedded_files: 0,
  process_candidates: ["game.exe"], controls: [], ambiguous_record_ids: [],
  unsupported_record_id_count: 0,
};
const acquisition = {
  acquisition_id: "a".repeat(32), provider: "fearless", artifact_id: "page:file",
  filename: "Game.CT", state: "cancelled", error: null, bytes_received: 0,
  expected_bytes: null, provider_wait_seconds: null, source_page: null,
  inspection: null, imported: null, execution_consent: null,
};
const modalCases: Array<[string, () => React.ReactElement]> = [
  ["game picker", () => <GamePickerModal games={[{ appId: 10, name: "Game", sortAs: "Game", isShortcut: false }]} selectedGame={null} onPick={vi.fn()} onCancel={vi.fn()} />],
  ["archive picker", () => <ArchiveImportModal members={[{ path: "Game.CT", size: 100, packed_size: 80, encrypted: false, format: "zip" }]} onImport={vi.fn()} onCancel={vi.fn()} />],
  ["imported tables", () => <ImportedTablesModal tables={[table] as any} activeSha256={null} onSelect={vi.fn()} onClose={vi.fn()} />],
  ["acquisition", () => <TableAcquisitionModal initialStatus={acquisition as any} searchScope="game" onImported={vi.fn()} onClose={vi.fn()} />],
  ["review", () => <TableReviewModal table={table as any} inspection={inspection as any} onUse={vi.fn()} onCancel={vi.fn()} />],
];

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("Manage speaks the same compatibility language as Search", () => {
  // One state language across both screens: the same exact table SHA renders
  // the same glyph, and the strongest state of all - these bytes were tried and
  // did not work - is visible here at all, which it was not while Manage
  // suppressed the mark and appended the words "marked as not working" to a
  // description instead.
  const evidence = {
    app_id: 10, table_sha256: SHA, target_process: "game.exe", pe_version: "1",
    steam_build_id: null, last_working_at: 1, invalidated: false, state: "matching" as const,
  };
  const refused = { sha256: SHA, reason: "it went straight back off", cause: "refused" as const, recordedAt: 2 };

  it("shows the failure glyph and the recorded sentence, not the old wording", () => {
    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[evidence]} blockedReasons={{ [SHA]: refused }}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByLabelText(/Marked as not working/)).toBeTruthy();
    // Red outranks the green this table had earned before it failed.
    expect(screen.queryByLabelText("Worked on this build")).toBeNull();
    expect(screen.queryByText(/marked as not working/)).toBeNull();
    expect(screen.getByText(/it went straight back off/)).toBeTruthy();
  });

  it("returns to retest rather than to green once the failure is cleared", () => {
    // Clearing Failed makes the evidence invalidated rather than matching, and
    // nothing but a new proof can bring the green back.
    const view = render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[{ ...evidence, invalidated: true }]} blockedReasons={{}}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByLabelText("Worked before; retest needed")).toBeTruthy();
    expect(screen.queryByLabelText("Worked on this build")).toBeNull();
    view.rerender(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[evidence]} blockedReasons={{}}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByLabelText("Worked on this build")).toBeTruthy();
  });

  it("does not offer Use for a table it is showing as not working", () => {
    // The same bytes are refused in Search while the mark stands, and this
    // screen reaches them without asking a provider anything, so offering Use
    // here was the one route around the user's own record.
    const view = render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[evidence]} blockedReasons={{ [SHA]: refused }}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect((screen.getByRole("button", { name: "Use", exact: true }) as HTMLButtonElement).disabled).toBe(true);
    // The state, in the fewest words that say it. Where the mark is cleared is
    // not on this line: it named another screen and another list on a row that
    // has a release and a date the reader needs first.
    expect(screen.getByText(/Marked as not working/)).toBeTruthy();
    // Cleared, and the press comes back with the bytes that never went away.
    view.rerender(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[{ ...evidence, invalidated: true }]} blockedReasons={{}}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect((screen.getByRole("button", { name: "Use", exact: true }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByLabelText("Worked before; retest needed")).toBeTruthy();
  });

  it.each(["unusable", "encrypted", "gone"] as const)(
      "still offers Use and keeps proven history through a %s record", (cause) => {
    // Search says the same thing about the same SHA: these are conditions of a
    // download or of a source, so they neither refuse a verified stored table
    // nor erase what a cheat has already proven for it.
    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[evidence]}
      blockedReasons={{ [SHA]: { ...refused, cause, reason: "the archive does not open" } }}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect((screen.getByRole("button", { name: "Use", exact: true }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByLabelText("Worked on this build")).toBeTruthy();
    expect(screen.queryByLabelText(/Marked as not working/)).toBeNull();
    // Nor is its sentence read onto a row that has no source context for it.
    expect(screen.queryByText(/the archive does not open/)).toBeNull();
    expect(screen.queryByText(/Marked as not working/)).toBeNull();
  });

  it("leads a row with the game whose library holds the table", () => {
    // This list spans every game on the device and a file name answers
    // nothing: `CD_Inventory_2145.CT` names no game. The game leads here for
    // the same reason it leads in the list of tables that did not work.
    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      owners={{ [SHA]: "Neon Bazaar" }}
      compatibility={[]} blockedReasons={{}}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByText(/^Neon Bazaar \u00b7 /)).toBeTruthy();
  });

  it("says a table is in use once, not twice on the same row", () => {
    // The row already leads with the game, so "in use \u00b7 in use by Neon
    // Bazaar" was the same sentence twice. Where this game is the only one on
    // the table the flag is the whole of it.
    render(<ImportedTablesModal tables={[table] as any} activeSha256={SHA}
      owners={{ [SHA]: "Neon Bazaar" }}
      selectedBy={{ [SHA]: { names: ["Neon Bazaar"], count: 1 } }}
      compatibility={[]} blockedReasons={{}}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    const label = screen.getByTestId(`imported-table-${SHA.slice(0, 8)}`).textContent ?? "";
    expect(label).toContain("Neon Bazaar \u00b7 Game.CT \u00b7 in use");
    expect(label).not.toContain("in use by");
  });

  it("leaves a table no profile holds with the row it always had", () => {
    // Nothing is guessed here: this is library membership, and a table no game
    // has imported is named by its file and nothing else.
    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      owners={{}} compatibility={[]} blockedReasons={{}}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.queryByText(/Neon Bazaar/)).toBeNull();
    expect(screen.getByText("Game.CT")).toBeTruthy();
  });

  it("reveals a clipped name and description by scrolling them under focus", () => {
    // The same reveal the pinned cheats use on the panel. A table's name and
    // what it is doing are both routinely longer than this window, and both are
    // what tell one row from the next, so the end of them is exactly what a
    // one-line row cannot show.
    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[]} blockedReasons={{}}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    const row = screen.getByTestId(`imported-table-${SHA.slice(0, 8)}`);
    expect(row.className).toContain("ce-decky-focusscroll");
    // Both lines, not only the name.
    expect(row.querySelectorAll(".ce-decky-marquee").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("Game.CT").closest(".ce-decky-marquee")).toBeTruthy();
  });

  it("measures again when the row takes focus, not only when it was built", () => {
    // One measurement on mount is a measurement of whatever the layout was at
    // that instant, and a line measured before its box has a width publishes no
    // overflow at all: it never animates, and nothing asks again until the row
    // is rebuilt. On the device that was the first row of Manage sitting still
    // while a row reached later moved. Focus is the moment the answer is about
    // to matter, so focus asks again.
    const widths = { scroll: 0, client: 0 };
    const target = HTMLElement.prototype;
    const prior = {
      scrollWidth: Object.getOwnPropertyDescriptor(target, "scrollWidth"),
      clientWidth: Object.getOwnPropertyDescriptor(target, "clientWidth"),
    };
    Object.defineProperty(target, "scrollWidth", { configurable: true, get: () => widths.scroll });
    Object.defineProperty(target, "clientWidth", { configurable: true, get: () => widths.client });
    try {
      render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
        compatibility={[]} blockedReasons={{}} onSelect={vi.fn()} onClose={vi.fn()} />);
      const row = screen.getByTestId(`imported-table-${SHA.slice(0, 8)}`);
      expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBe(0);

      widths.scroll = 2000;
      widths.client = 400;
      fireEvent.focusIn(row);
      expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBeGreaterThanOrEqual(2);
    } finally {
      for (const [name, descriptor] of Object.entries(prior)) {
        if (descriptor) Object.defineProperty(target, name, descriptor);
        else delete (target as unknown as Record<string, unknown>)[name];
      }
    }
  });

  it("keeps the mark while the reveal has the clamp off the line", () => {
    // The rule that reveals a focused line takes `max-width` and `overflow` off
    // the line's own box, so measured against itself a focused line has nothing
    // left to reveal. Answering that with zero withdraws the very mark the rule
    // matches on: the reveal stops, the line re-clamps, and the next
    // measurement does it again. Both windows sat perfectly still.
    const marqueeWidths = (track: HTMLElement, view: HTMLElement, clamped: boolean) => {
      Object.defineProperty(track, "scrollWidth", { configurable: true, get: () => 2000 });
      // Un-clamped, the line's own box is its whole content; the window it is
      // shown through does not move either way.
      Object.defineProperty(track, "clientWidth", { configurable: true, get: () => (clamped ? 400 : 2000) });
      Object.defineProperty(view, "clientWidth", { configurable: true, get: () => 400 });
    };

    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[]} blockedReasons={{}} onSelect={vi.fn()} onClose={vi.fn()} />);
    const row = screen.getByTestId(`imported-table-${SHA.slice(0, 8)}`);
    for (const view of Array.from(row.querySelectorAll<HTMLElement>(".ce-decky-marquee"))) {
      marqueeWidths(view.firstElementChild as HTMLElement, view, true);
    }
    fireEvent.focusIn(row);
    expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBeGreaterThanOrEqual(2);

    // Focused: the reveal is running and the clamp is off. The mark survives.
    for (const view of Array.from(row.querySelectorAll<HTMLElement>(".ce-decky-marquee"))) {
      marqueeWidths(view.firstElementChild as HTMLElement, view, false);
    }
    fireEvent.focusIn(row);
    expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBeGreaterThanOrEqual(2);
  });

  it("takes as long as the line is long, so the longest is not the fastest", () => {
    // One duration for every distance is one speed per line. A table's recorded
    // reason overflows this sheet by well over a thousand pixels, which seven
    // flat seconds turned into a line moving past the reader rather than one
    // being read. jsdom lays nothing out, so the two widths are stubbed.
    const stubWidths = (scroll: number, client: number) => {
      const target = HTMLElement.prototype;
      const prior = {
        scrollWidth: Object.getOwnPropertyDescriptor(target, "scrollWidth"),
        clientWidth: Object.getOwnPropertyDescriptor(target, "clientWidth"),
      };
      Object.defineProperty(target, "scrollWidth", { configurable: true, get: () => scroll });
      Object.defineProperty(target, "clientWidth", { configurable: true, get: () => client });
      return () => {
        for (const [name, descriptor] of Object.entries(prior)) {
          if (descriptor) Object.defineProperty(target, name, descriptor);
          else delete (target as unknown as Record<string, unknown>)[name];
        }
      };
    };

    let restore = stubWidths(2000, 400);
    try {
      render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
        compatibility={[]} blockedReasons={{}} onSelect={vi.fn()} onClose={vi.fn()} />);
      const track = screen.getByText("Game.CT").closest(".ce-decky-marquee")!.firstElementChild as HTMLElement;
      expect(track.style.getPropertyValue("--ce-marquee-shift")).toBe("-1600px");
      // 1600 pixels at the paced speed, rather than 1600 pixels in seven
      // seconds, which is more than two hundred a second.
      expect(track.style.getPropertyValue("--ce-marquee-seconds")).toBe("20.0s");
    } finally {
      restore();
      cleanup();
    }

    // And a line so long that its own length asks for over a minute is capped:
    // a reveal that slow reads as a line that is not moving, and a paragraph is
    // read by opening the row, which wraps it.
    cleanup();
    restore = stubWidths(9000, 400);
    try {
      render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
        compatibility={[]} blockedReasons={{}} onSelect={vi.fn()} onClose={vi.fn()} />);
      const track = screen.getByText("Game.CT").closest(".ce-decky-marquee")!.firstElementChild as HTMLElement;
      expect(track.style.getPropertyValue("--ce-marquee-seconds")).toBe("30.0s");
    } finally {
      restore();
      cleanup();
    }

    // A line that only just overflows keeps the whole window it already had:
    // the flat seven seconds are the floor, not the rule.
    restore = stubWidths(500, 400);
    try {
      render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
        compatibility={[]} blockedReasons={{}} onSelect={vi.fn()} onClose={vi.fn()} />);
      const track = screen.getByText("Game.CT").closest(".ce-decky-marquee")!.firstElementChild as HTMLElement;
      expect(track.style.getPropertyValue("--ce-marquee-seconds")).toBe("7.0s");
    } finally {
      restore();
    }
  });

  it("holds the mark against the controls rather than at the end of a clipped name", () => {
    // A row's name is cut to one line, and real tables carry names long enough
    // for that to be the whole row. A glyph drawn after the text was inside the
    // part that got cut, so the state this list exists to show was invisible on
    // exactly the rows whose names were longest.
    const view = render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[evidence]} blockedReasons={{ [SHA]: refused }}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    const glyph = screen.getByLabelText(/Marked as not working/);
    expect(glyph.closest(".ce-decky-ellipsis")).toBeNull();
    // Beside the row's own controls, which is what puts every row's mark in the
    // same column.
    const use = screen.getByRole("button", { name: "Use", exact: true });
    const trailing = glyph.parentElement?.parentElement;
    expect(trailing && trailing.contains(use)).toBe(true);
    view.unmount();
  });

  it("keeps every mark passive, so no row gains a controller stop", () => {
    const view = render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      compatibility={[evidence]} blockedReasons={{ [SHA]: refused }}
      onSelect={vi.fn()} onClose={vi.fn()} />);
    const glyph = screen.getByLabelText(/Marked as not working/);
    expect(glyph.querySelectorAll("button,[tabindex],a").length).toBe(0);
    expect(glyph.getAttribute("tabindex")).toBeNull();
    expect(view.container.querySelectorAll("svg[focusable=\"false\"]").length).toBeGreaterThan(0);
  });
});

describe("device-wide table revocation", () => {
  it("binds a new confirmation to refreshed holder identities", async () => {
    const revoke = vi.fn().mockRejectedValue(new Error("Holder games changed"));
    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      selectedBy={{ [SHA]: { count: 1, names: ["Old"] } }}
      holderIds={{ [SHA]: [10] }}
      onRefreshHolders={async () => ({ holders: { [SHA]: { count: 1, names: ["New"] } }, holderIds: { [SHA]: [20] }, activeSha256: null })}
      onRevoke={revoke} onSelect={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Revoke", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm", exact: true }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Revoke", exact: true }) as HTMLButtonElement).disabled).toBe(false));
    expect(revoke).toHaveBeenLastCalledWith(SHA, [10]);
    fireEvent.click(screen.getByRole("button", { name: "Revoke", exact: true }));
    expect(screen.getByText(/Confirm: revoke and detach/).textContent).toContain("New");
    fireEvent.click(screen.getByRole("button", { name: "Confirm", exact: true }));
    await waitFor(() => expect(revoke).toHaveBeenLastCalledWith(SHA, [20]));
  });

  it("refreshes the current selection after only some holders were revoked", async () => {
    render(<ImportedTablesModal tables={[table] as any} activeSha256={SHA}
      selectedBy={{ [SHA]: { count: 2, names: ["Game", "Other"] } }}
      onRevoke={async () => { throw new Error("Other could not stop"); }}
      onRefreshHolders={async () => ({ holders: { [SHA]: { count: 1, names: ["Other"] } }, activeSha256: null })}
      onSelect={vi.fn()} onDelete={vi.fn()} onClose={vi.fn()} />);
    expect((screen.getByRole("button", { name: "Use", exact: true }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Revoke", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm", exact: true }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Use", exact: true }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole("button", { name: "Delete", exact: true })).toBeNull();
    expect(screen.getByText(/Other could not stop/)).toBeTruthy();
  });

  it("confirms all holders, preserves the row, then offers deletion without a game", async () => {
    const revoke = vi.fn().mockResolvedValue(undefined);
    const remove = vi.fn().mockResolvedValue(undefined);
    render(<ImportedTablesModal tables={[]} otherTables={[table] as any} activeSha256={null}
      canSelect={false} selectedBy={{ [SHA]: { count: 2, names: ["Removed game", "Other game"] } }}
      onRevoke={revoke} onSelect={vi.fn()} onDelete={remove} onClose={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Use", exact: true })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete", exact: true })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Revoke", exact: true }));
    expect(revoke).not.toHaveBeenCalled();
    expect(screen.getByText(/Confirm: revoke and detach/).textContent).toContain("Removed game, Other game");
    fireEvent.click(screen.getByRole("button", { name: "Confirm", exact: true }));
    await waitFor(() => expect((screen.getByRole("button", { name: "Delete", exact: true }) as HTMLButtonElement).disabled).toBe(false));
    expect(revoke).toHaveBeenCalledWith(SHA, []);
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Delete", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm", exact: true }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(SHA));
  });
});

describe("action failures and immediate cancellation", () => {
  it.each(["select", "delete"])("releases Manage after a synchronous %s failure and records the exact table", async (kind) => {
    resetSupportLog();
    const fail = vi.fn(() => { throw new Error("synchronous refusal"); });
    const close = vi.fn();
    render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
      onSelect={kind === "select" ? fail : vi.fn()} onDelete={kind === "delete" ? fail : undefined} onClose={close} />);
    fireEvent.click(screen.getByRole("button", { name: kind === "select" ? "Use" : "Delete", exact: true }));
    if (kind === "delete") fireEvent.click(screen.getByRole("button", { name: "Confirm", exact: true }));
    await screen.findByText("synchronous refusal");
    fireEvent.click(screen.getByRole("button", { name: "Back", exact: true }));
    expect(close).toHaveBeenCalledOnce();
    expect(readSupportLog().entries).toEqual(expect.arrayContaining([
      expect.objectContaining({ event: "ui.operation_failed", fields: expect.objectContaining({ table_sha: SHA, operation: `manage.${kind}` }) }),
    ]));
  });

  it.each(["archive", "game", "review"])("ignores a same-turn Cancel after starting %s work", async (kind) => {
    let finish!: () => void;
    const work = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    const cancel = vi.fn();
    if (kind === "archive") render(<ArchiveImportModal members={[{ path: "a.CT", size: 1, packed_size: 1, encrypted: false, format: "zip" }]} onImport={work} onCancel={cancel} />);
    else if (kind === "game") render(<GamePickerModal games={[{ appId: 10, name: "Game", sortAs: "Game", isShortcut: false }]} selectedGame={null} onPick={work} onCancel={cancel} />);
    else render(<TableReviewModal table={table as any} inspection={inspection as any} onUse={work} onCancel={cancel} />);
    const start = screen.getByRole("button", { name: kind === "archive" ? "Import" : kind === "game" ? "Use this game" : "Use this table", exact: true });
    const back = screen.getByRole("button", { name: "Cancel", exact: true });
    act(() => { start.click(); back.click(); });
    expect(work).toHaveBeenCalledOnce();
    expect(cancel).not.toHaveBeenCalled();
    await act(async () => { finish(); });
  });

  it("latches Stop and cancel until the first stop finishes", async () => {
    let finishUse!: () => void;
    let finishStop!: () => void;
    const onUse = vi.fn((_process, report) => { report("Waiting for CE", true); return new Promise<void>((resolve) => { finishUse = resolve; }); });
    const onAbort = vi.fn(() => new Promise<null>((resolve) => { finishStop = () => resolve(null); }));
    render(<TableReviewModal table={table as any} inspection={inspection as any} onUse={onUse} onAbort={onAbort} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    const stop = await screen.findByRole("button", { name: "Stop and cancel" });
    act(() => { stop.click(); stop.click(); });
    expect(onAbort).toHaveBeenCalledOnce();
    await act(async () => { finishUse(); });
    expect((screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => { finishStop(); });
  });

  it.each(["rejected", "nothing to stop"])("releases an unsuccessful Stop while activation remains pending (%s)", async (result) => {
    let finishUse!: () => void;
    const onUse = vi.fn((_process, report) => { report("Waiting for CE", true); return new Promise<void>((resolve) => { finishUse = resolve; }); });
    const onAbort = result === "rejected" ? vi.fn().mockRejectedValue(new Error("Stop was refused")) : vi.fn().mockResolvedValue("There is nothing to stop");
    render(<TableReviewModal table={table as any} inspection={inspection as any} onUse={onUse} onAbort={onAbort} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" })); });
    const retry = screen.getByRole("button", { name: "Stop and cancel" }) as HTMLButtonElement;
    expect(retry.disabled).toBe(false);
    await act(async () => { retry.click(); });
    expect(onAbort).toHaveBeenCalledTimes(2);
    await act(async () => { finishUse(); });
  });

  it("keeps Stop latched after the stop RPC resolves until its activation settles", async () => {
    let finishUse!: () => void;
    const onUse = vi.fn((_process, report) => { report("Waiting for CE", true); return new Promise<void>((resolve) => { finishUse = resolve; }); });
    const onAbort = vi.fn().mockResolvedValue(null);
    render(<TableReviewModal table={table as any} inspection={inspection as any} onUse={onUse} onAbort={onAbort} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Stop and cancel" })); });
    const stop = screen.getByRole("button", { name: /Stopping|Stop and cancel/ });
    expect((stop as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(stop);
    expect(onAbort).toHaveBeenCalledOnce();
    await act(async () => { finishUse(); });
    expect((screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Use this table" }));
    expect((screen.getByRole("button", { name: "Stop and cancel" }) as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { finishUse(); });
  });

  it.each([false, true])("correlates acquisition cancellation from its press to its terminal event (failure=%s)", async (fails) => {
    resetSupportLog();
    let finish!: (value: unknown) => void;
    let fail!: (cause: Error) => void;
    api.cancelTableAcquisition.mockResolvedValue({ ...acquisition, state: "cancelled" }).mockImplementationOnce(() => new Promise((resolve, reject) => { finish = resolve; fail = reject; }));
    const current = { ...acquisition, state: "downloading" };
    api.pollTableAcquisition.mockResolvedValue(current);
    render(<TableAcquisitionModal initialStatus={current as any} searchScope="game" onImported={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Cancel acquisition", exact: true }));
    const started = readSupportLog().entries.find((entry) => entry.event === "ui.operation_started" && entry.fields.operation === "acquisition.cancel");
    const press = readSupportLog().entries.find((entry) => entry.event === "ui.action" && entry.fields.action === "table_acquisition_modal.cancel");
    expect(started?.fields).toMatchObject({ acquisition: current.acquisition_id, interaction: press?.fields.interaction, action: "table_acquisition_modal.cancel" });
    expect(api.cancelTableAcquisition).toHaveBeenCalledOnce();
    expect(readSupportLog().entries.filter((entry) => entry.fields.operation === "acquisition.cancel")).toHaveLength(1);
    await act(async () => { if (fails) fail(new Error("cancel refused")); else finish({ ...current, state: "cancelled" }); });
    const events = readSupportLog().entries.filter((entry) => entry.fields.operation === "acquisition.cancel");
    expect(events.map((entry) => entry.event)).toEqual(["ui.operation_started", fails ? "ui.operation_failed" : "ui.operation_completed"]);
    expect(events[1].fields.interaction).toBe(press?.fields.interaction);
  });
});

describe("detached modal density", () => {
  it.each(modalCases)("records the lifetime of %s even when its host unmounts it", (_name, modal) => {
    resetSupportLog();
    const view = render(modal());
    view.unmount();
    const events = readSupportLog().entries.filter((entry) => entry.event.startsWith("ui.surface_"));
    expect(events.map((entry) => entry.event)).toEqual(["ui.surface_opened", "ui.surface_closed"]);
    expect(events[0].fields.surface_instance).toBe(events[1].fields.surface_instance);
  });

  it.each(modalCases)("keeps the scoped heading and row styles in the %s", (_name, modal) => {
    render(modal());
    const heading = document.querySelector(".ce-decky-heading");
    expect(heading).toBeTruthy();
    expect(heading?.closest(".ce-decky-dense")).toBeTruthy();
  });

  /** The whole row a labelled fact is drawn on, whichever shape the screen uses. */
  const rowHolding = (label: string): string => {
    const field = screen.getByText(label).closest("div");
    return field?.closest("[data-panel-row]")?.textContent ?? "";
  };

  it("says so when the look for the game's processes fails, and keeps the last answer", async () => {
    // This screen is where the user picks the exact executable Cheat Engine
    // will attach to, and the press that refills that list swallowed its own
    // failure: the spinner stopped, the stale choices stayed, and nothing said
    // the look had not happened. The panel underneath records it, and this
    // screen is drawn over that panel.
    const onRefreshProcesses = vi.fn()
      .mockRejectedValueOnce(new Error("the process table could not be read"))
      .mockResolvedValueOnce(["Game-Win64-Shipping.exe", "Launcher.exe"]);
    render(<TableReviewModal table={table as any} inspection={inspection as any}
      onRefreshProcesses={onRefreshProcesses} onUse={vi.fn()} onCancel={vi.fn()} />);

    fireEvent.click(screen.getByRole("button", { name: /Look for the game's processes again|Refresh/ }));
    const failure = await screen.findByTestId("review-rescan-error");
    expect(failure.textContent).toContain("the process table could not be read");
    // The choices are the previous answer, and the screen says they are.
    expect(failure.textContent).toContain("last look that worked");

    // A look that works clears it and brings the new list.
    fireEvent.click(screen.getByRole("button", { name: /Look for the game's processes again|Refresh/ }));
    await waitFor(() => expect(screen.queryByTestId("review-rescan-error")).toBeNull());
  });

  it("puts the review screen's facts two to a line on a short screen only", () => {
    // The screen opens with four lines of identity nobody presses, and on a
    // handheld those four rows are most of the display: the sheet was drawn
    // from above the top of the screen, so its own heading was off it. Two to a
    // line halves that. A display with the room keeps the rows it had, which is
    // the shape it was designed and looked at in.
    const tall = window.innerHeight;
    try {
      Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
      render(<TableReviewModal table={table as any} inspection={inspection as any}
        onRefreshProcesses={vi.fn()} onUse={vi.fn()} onCancel={vi.fn()} />);
      // Each fact has a row to itself: the file above it is not on this one.
      expect(rowHolding("Source")).not.toContain("Game.CT");
      // Stacked, the button that looks again says what it looks for.
      expect(screen.getByRole("button", { name: "Refresh", exact: true })).toBeTruthy();

      cleanup();
      Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
      render(<TableReviewModal table={table as any} inspection={inspection as any}
        onRefreshProcesses={vi.fn()} onUse={vi.fn()} onCancel={vi.fn()} />);
      // The file and where it came from share a row, and so do what is in it
      // and what that can execute.
      expect(rowHolding("Source")).toContain("Game.CT");
      expect(rowHolding("Contents")).toContain("Executable content");
      // Paired, the four read as one block, so the group is separated from the
      // row after it by the same air that separates its own two columns, and
      // not by the hairline that divides one row of the block from the next.
      const pairOf = (label: string) => screen.getByText(label).closest("div")?.parentElement?.parentElement;
      expect(pairOf("Source")?.style.marginBottom).toBe("");
      expect(pairOf("Contents")?.style.marginBottom).toBe(pairOf("Contents")?.style.gap);
      // The press that refills the process control sits beside it rather than
      // taking a full row under a control that already has the width.
      expect(screen.queryByRole("button", { name: "Look for the game's processes again" })).toBeNull();
      const rescan = screen.getByRole("button", { name: "Refresh" });
      // And it is in one group with the control it fills rather than beside it
      // in a plain box, which Steam reads as a separate step down.
      const group = rescan.closest('[data-flow-children="row"]');
      expect(group).toBeTruthy();
      expect(group?.querySelector("select")).toBeTruthy();
    } finally {
      Object.defineProperty(window, "innerHeight", { value: tall, configurable: true });
    }
  });

  it("offers this game's own files when nothing names a process and nothing runs", () => {
    // The case a user hits first: a table that names no process, downloaded for
    // a Steam game that is not running. A Steam library entry's launch
    // executable is observable only in the running game's own process table, so
    // there was no candidate at all and Use this table could not be pressed
    // until the game had been started once.
    const nameless = { ...inspection, process_candidates: [] };
    const installed = {
      schema: 1, app_id: 220, install_dir: "/games/Half-Life 2", truncated: false, reason: null,
      executables: [
        { name: "hl2.exe", directory: "", depth: 0, size_bytes: 127072 },
        { name: "vrad.exe", directory: "bin", depth: 1, size_bytes: 1 },
      ],
    };
    render(<TableReviewModal table={table as any} inspection={nameless as any}
      installedExecutables={installed as any} onUse={vi.fn()} onCancel={vi.fn()} />);

    const picker = screen.getByLabelText("Game process") as HTMLSelectElement;
    // The one root executable is the default, and the tools beside it are
    // offered rather than chosen.
    expect(picker.value).toBe("hl2.exe");
    expect([...picker.options].map((option) => option.textContent))
      .toEqual(["hl2.exe · in this game's files", "vrad.exe · in this game's files", "Enter another .exe basename…"]);
    // And the press can be made, which is the whole point.
    expect((screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement).disabled).toBe(false);
    // Said where the choice is made: this is the game's own folder and nothing
    // has been seen running, so the first real start is what checks it.
    // The rest of that sentence moved behind the row's own question mark: the
    // window has to fit a handheld, and what is left is what the row is for.
    expect(screen.getByTestId("review-installed-choice").textContent).toContain("not an observation");
  });

  it("offers the one program Steam starts rather than a folder full of candidates", () => {
    // Half-Life 2 is the case this was rebuilt for. Its folder holds `hl2.exe`
    // and twenty-seven SDK compilers, and the picker offered the lot; Steam's
    // own record names the one line the client starts the game from.
    const nameless = { ...inspection, process_candidates: [] };
    const declared = {
      schema: 2, app_id: 220, install_dir: "/games/Half-Life 2", truncated: false,
      source: "steam", declared_reason: null, reason: null,
      executables: [{ name: "hl2.exe", directory: "", depth: 0, size_bytes: 127072, declared: true, description: "Play" }],
    };
    render(<TableReviewModal table={table as any} inspection={nameless as any}
      installedExecutables={declared as any} onUse={vi.fn()} onCancel={vi.fn()} />);

    const picker = screen.getByLabelText("Game process") as HTMLSelectElement;
    expect(picker.value).toBe("hl2.exe");
    expect([...picker.options].map((option) => option.textContent))
      .toEqual(["hl2.exe \u00b7 Steam starts this", "Enter another .exe basename\u2026"]);
    expect((screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement).disabled).toBe(false);
    // And it says which claim this is: what Steam starts is a statement by the
    // client that has to start it, not a file somebody found on the disk.
    expect(screen.getByTestId("review-declared-choice").textContent).toContain("Steam's own record");
    expect(screen.queryByTestId("review-installed-choice")).toBeNull();
  });

  it("says a Linux build has nothing to attach to, instead of asking for an .exe", () => {
    // A Steam Deck's Half-Life 2 is the native build: `hl2.sh`, `hl2_linux` and
    // not one `.exe` anywhere, while Steam's record names `hl2.exe` from the
    // Windows depot the other machine has. Asking the reader to type a basename
    // is asking them to name something that does not exist on this device.
    const nameless = { ...inspection, process_candidates: [] };
    const nothing = {
      schema: 3, app_id: 220, install_dir: "/games/Half-Life 2", truncated: false,
      source: "files", declared_reason: "the program Steam starts for this game is not in its installed files",
      reason: "this game's files hold no Windows executable", cause: "no_windows_executable",
      executables: [],
    };
    render(<TableReviewModal table={table as any} inspection={nameless as any}
      installedExecutables={nothing as any} onUse={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.getByTestId("review-no-windows-program").textContent).toContain("installed as a Linux build");
    expect(screen.queryByLabelText("Game process")).toBeNull();
    expect(screen.queryByLabelText("Process (.exe basename)")).toBeNull();
    expect((screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("lets the device's own proof outrank a name the table carries", () => {
    // The table names `hl2.exe` and this device's Half-Life 2 is `hl2_linux`.
    // A name is not a program: the walk looked and there is nothing here to
    // attach to, so counting the hint as a candidate hid the one row that says
    // so and left `Use this table` live over a process that does not exist.
    const nothing = {
      schema: 3, app_id: 220, install_dir: "/games/Half-Life 2", truncated: false,
      source: "files", declared_reason: "the program Steam starts for this game is not in its installed files",
      reason: "this game's files hold no Windows executable", cause: "no_windows_executable",
      executables: [],
    };
    const onUse = vi.fn();
    render(<TableReviewModal table={table as any} inspection={{ ...inspection, process_candidates: ["hl2.exe"] } as any}
      initialTargetProcess="hl2.exe" launchExecutable="/games/Half-Life 2/hl2.exe"
      installedExecutables={nothing as any} onUse={onUse} onCancel={vi.fn()} />);

    expect(screen.getByTestId("review-no-windows-program")).toBeTruthy();
    const use = screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement;
    expect(use.disabled).toBe(true);
    fireEvent.click(use);
    expect(onUse).not.toHaveBeenCalled();
  });

  it("lets a Windows process actually running overturn that proof", () => {
    // The one thing that does outrank the walk, because it is the device
    // contradicting itself rather than a file or an account naming a depot.
    const nothing = {
      schema: 3, app_id: 220, install_dir: "/games/Half-Life 2", truncated: false,
      source: "files", declared_reason: null,
      reason: "this game's files hold no Windows executable", cause: "no_windows_executable",
      executables: [],
    };
    render(<TableReviewModal table={table as any} inspection={{ ...inspection, process_candidates: [] } as any}
      observedProcesses={["hl2.exe"]} installedExecutables={nothing as any}
      onUse={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.queryByTestId("review-no-windows-program")).toBeNull();
    expect((screen.getByLabelText("Game process") as HTMLSelectElement).value).toBe("hl2.exe");
    expect((screen.getByRole("button", { name: "Use this table" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("keeps the manual entry where nobody has actually looked", () => {
    // A walk that hit its own bound says nobody looked, not that there is
    // nothing: the manual entry is the way through that.
    const nameless = { ...inspection, process_candidates: [] };
    const unknown = {
      schema: 3, app_id: 220, install_dir: "/games/Game", truncated: true,
      source: "files", declared_reason: null,
      reason: "this game's files were too many to search through", cause: "too_many_files",
      executables: [],
    };
    render(<TableReviewModal table={table as any} inspection={nameless as any}
      installedExecutables={unknown as any} onUse={vi.fn()} onCancel={vi.fn()} />);

    expect(screen.queryByTestId("review-no-windows-program")).toBeNull();
    expect(screen.getByLabelText("Game process")).toBeTruthy();
  });

  it("keeps the game's own files below every stronger signal", () => {
    const installed = {
      schema: 1, app_id: 220, install_dir: "/games/Game", truncated: false, reason: null,
      executables: [{ name: "hl2.exe", directory: "", depth: 0, size_bytes: 1 }],
    };
    render(<TableReviewModal table={table as any} inspection={inspection as any}
      observedProcesses={["Game.exe"]} installedExecutables={installed as any}
      onUse={vi.fn()} onCancel={vi.fn()} />);
    const picker = screen.getByLabelText("Game process") as HTMLSelectElement;
    // Running outranks what the table names, which outranks the folder.
    expect(picker.value).toBe("Game.exe");
    expect(screen.queryByTestId("review-installed-choice")).toBeNull();
  });

  it("puts the ring back on Look inside when the reader returns from the code", async () => {
    // Reading the table replaces this tree, so coming back mounts the review
    // again and Steam's initial focus lands where it would on a screen just
    // opened. The press the reader is still in the middle of is the one that
    // opened the code, so the ring belongs back on it.
    api.listTableCode.mockResolvedValue({
      schema: 1, sha256: SHA, size: 100, sections: [], truncated: false,
      totals: { lua: 0, auto_assembler: 0, form: 0, embedded_file: 0 },
    });
    render(<TableReviewModal table={{ ...table, executable_content: true } as any} inspection={inspection as any}
      onUse={vi.fn()} onCancel={vi.fn()} />);

    const lookInside = screen.getByRole("button", { name: "Look inside" });
    fireEvent.click(lookInside);
    expect(await screen.findByText("Inside this table")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Look inside" })));
  });

  it("opens every modal's action row on a press that can actually be made", () => {
    // A window that opens with the ring on a control it has just marked
    // unavailable answers nothing: the first press does nothing, and the reader
    // has to find out by pressing. Which control that is depends on the state
    // the window opened in, so it is decided from that state rather than by the
    // order the buttons happen to be in.
    const preferred = () => [...document.querySelectorAll("button")]
      .filter((button) => button.getAttribute("data-preferred-focus") === "true")
      .map((button) => button.textContent);

    render(<TableReviewModal table={table as any} inspection={inspection as any}
      onUse={vi.fn()} onCancel={vi.fn()} />);
    expect(preferred()).toEqual(["Use this table"]);

    // The same window with nothing to attach to: the decision cannot be made
    // and no press down here makes it, so the ring opens on the way out.
    cleanup();
    render(<TableReviewModal
      table={table as any}
      inspection={{ ...inspection, process_candidates: [] } as any}
      onUse={vi.fn()}
      onCancel={vi.fn()}
    />);
    expect(preferred()).toEqual(["Cancel"]);
  });

  it("offers the tables kept on the device when this game has no library left", async () => {
    // Deleting the plugin's setup state keeps `tables/` and removes `state/`,
    // and repairing a corrupt profile store replaces it with an empty one. Both
    // tell the user the imported tables are kept and can be chosen again, and
    // the only ways back to one went through the association those recoveries
    // discard: Search needs a provider row that maps to the bytes, and Local
    // file needs the original file. Offline, with neither, the plugin held the
    // exact bytes and nothing in Game Mode could name them.
    const onSelect = vi.fn();
    const stored = { ...table, sha256: "c".repeat(64), filename: "Kept.CT" };
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[stored] as any}
      activeSha256={null}
      onSelect={onSelect}
      onClose={vi.fn()}
    />);

    // The two groups are one ordered list now: this game's own first, and the
    // device's marked where the boundary falls rather than by a heading a page
    // may not carry.
    expect(screen.getByText(/0 here, 1 elsewhere/)).toBeTruthy();
    const row = screen.getByTestId(`stored-table-${stored.sha256.slice(0, 8)}`);
    expect(row.textContent).toContain("elsewhere on this device");
    expect(row.textContent).toContain("Kept.CT");
    // Explicit: nothing is chosen for the user, and what the press does is
    // stated where the press is.
    // One short line, because it sits above a list counted in rows and the
    // three sentences it carried explained what the next press shows anyway.
    // What is left is the part that doing it does not tell you.
    expect(screen.getByText("No provider or network needed.")).toBeTruthy();
    fireEvent.click(within(row).getByRole("button", { name: "Use" }));
    expect(onSelect).toHaveBeenCalledWith(stored.sha256);
  });

  it("says which release a row is, from the record the rest of the row describes", () => {
    // The version inside a .CT is the author's own field and is very often
    // identical across a whole source, which makes a screen of rows that all
    // say the same thing. What tells revisions apart is the release the
    // provider advertised, which is what the search screen showed.
    const detailed = {
      ...table, table_version: "45", entry_count: 12,
      origins: [{ provider: "fearless", artifact_id: "a", topic_id: "t", source_page: "p",
                  original_filename: "Game.CT", retrieved_at: "2026-09-01T10:30:00Z",
                  advertised_sha256: null, version: "1.0.12" }],
    };
    render(<ImportedTablesModal tables={[detailed] as any} activeSha256={null} onSelect={vi.fn()} onClose={vi.fn()} />);
    const row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(row.textContent).toContain("v1.0.12");
    expect(row.textContent).not.toContain("v45");
    // A tag that already carries its own marker is not given a second one:
    // GitHub advertises `v2` verbatim, which read as `vv2`.
    expect(row.textContent).not.toContain("vv");
    expect(row.textContent).toContain("12 records");
    expect(row.textContent).toContain("fearless");
    // The download date, which is what `retrieved_at` is stamped with, and not
    // the date of the post the file came from.
    expect(row.textContent).toContain("2026-09-01");
  });

  it("describes one row from one provenance record", () => {
    // The release used to be searched for across every origin while the
    // provider and date came from the newest, so a row could pair a version
    // from the source a table came from once with the name and date of the one
    // it came from last, and present the three as a single fact.
    const twice = {
      ...table, table_version: "45",
      origins: [
        { provider: "fearless", artifact_id: "a", topic_id: "t", source_page: "p",
          original_filename: "Game.CT", retrieved_at: "2026-01-01T00:00:00Z",
          advertised_sha256: null, version: "1.0.0" },
        { provider: "playground", artifact_id: "b", topic_id: "t", source_page: "p",
          original_filename: "Game.CT", retrieved_at: "2026-09-01T00:00:00Z",
          advertised_sha256: null },
      ],
    };
    render(<ImportedTablesModal tables={[twice] as any} activeSha256={null} onSelect={vi.fn()} onClose={vi.fn()} />);
    const row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(row.textContent).toContain("playground");
    expect(row.textContent).toContain("2026-09-01");
    // The newest origin advertised no release, so the row claims none. A
    // version belonging to the older download is a different table's release,
    // and the file's own `CheatEngineTableVersion` is the version of Cheat
    // Engine's table format rather than of the table: every table reads 45 or
    // 46 whatever its game, so showing it named the file's format as though it
    // were the thing the reader is choosing between.
    expect(row.textContent).not.toContain("v45");
    expect(row.textContent).not.toContain("v1.0.0");
  });

  it("shows the release a source stated, and only that", () => {
    const stated = {
      ...table, table_version: "46", origins: [
        { provider: "fearless", artifact_id: "a", topic_id: "t", source_page: "p",
          original_filename: "Game.CT", retrieved_at: "2026-09-01T00:00:00Z",
          advertised_sha256: null, version: "1.05.01" },
      ],
    };
    render(<ImportedTablesModal tables={[stated] as any} activeSha256={null} onSelect={vi.fn()} onClose={vi.fn()} />);
    const row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(row.textContent).toContain("v1.05.01");
    expect(row.textContent).not.toContain("v46");
  });

  it("dates a local import, and claims no date where nothing recorded one", () => {
    const local = { ...table, table_version: "45", origins: [], imported_at: "2026-08-14T09:00:00Z" };
    render(<ImportedTablesModal tables={[local] as any} activeSha256={null} onSelect={vi.fn()} onClose={vi.fn()} />);
    let row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    // No source stated a release for a file opened from this device, so the row
    // claims none.
    expect(row.textContent).not.toContain("v45");
    expect(row.textContent).toContain("Local file");
    // Nothing downloaded it, so the date is when it arrived here instead.
    expect(row.textContent).toContain("2026-08-14");

    cleanup();
    render(<ImportedTablesModal tables={[{ ...local, imported_at: null }] as any}
      activeSha256={null} onSelect={vi.fn()} onClose={vi.fn()} />);
    row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(row.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}/);
  });

  it("takes two presses to destroy a table, and never the ring off Use", () => {
    // Content-addressed and irreversible: on a device with no network and no
    // original file those bytes are gone. The confirmation is the row itself
    // rather than a window over this one, because a modal raised over a modal
    // is a window behind a window on the device.
    const onDelete = vi.fn().mockResolvedValue(undefined);
    const stored = { ...table, sha256: "c".repeat(64), filename: "Kept.CT" };
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[stored] as any}
      activeSha256={null}
      onSelect={vi.fn()}
      onDelete={onDelete}
      onClose={vi.fn()}
    />);

    const row = screen.getByTestId(`stored-table-${stored.sha256.slice(0, 8)}`);
    expect(within(row).getAllByRole("button").map((node) => node.textContent)).toEqual(["Use", "Delete"]);
    expect(within(row).getByRole("button", { name: "Use" }).dataset.preferredFocus).toBe("true");

    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    expect(onDelete).not.toHaveBeenCalled();
    // What actually happens, rather than a recovery this screen cannot promise.
    expect(row.textContent).toContain("Confirm: delete from this device");
    expect(row.textContent).toContain("comes back only from a file or a new download");
    expect(row.textContent).not.toMatch(/can be imported again/);
    fireEvent.click(within(row).getByRole("button", { name: "Confirm" }));
    expect(onDelete).toHaveBeenCalledWith(stored.sha256);
  });

  it("offers no way to destroy a table where the caller cannot", () => {
    const stored = { ...table, sha256: "c".repeat(64), filename: "Kept.CT" };
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[stored] as any}
      activeSha256={null}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);
    const row = screen.getByTestId(`stored-table-${stored.sha256.slice(0, 8)}`);
    expect(within(row).getAllByRole("button").map((node) => node.textContent)).toEqual(["Use"]);
  });

  it("lets a table whose authorization was withdrawn be authorized again", () => {
    // Selected and authorized are two facts. Withdrawing the authorization
    // leaves the table selected and unusable, and the only way back is opening
    // it once through the review screen, which is what Use does.
    const onSelect = vi.fn();
    render(<ImportedTablesModal
      tables={[table] as any}
      activeSha256={table.sha256}
      activeAuthorized={false}
      onSelect={onSelect}
      onClose={vi.fn()}
    />);
    const row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(row.textContent).toContain("needs authorizing");
    const use = within(row).getByRole("button", { name: "Use" }) as HTMLButtonElement;
    expect(use.disabled).toBe(false);
    fireEvent.click(use);
    expect(onSelect).toHaveBeenCalledWith(table.sha256);

    cleanup();
    render(<ImportedTablesModal
      tables={[table] as any}
      activeSha256={table.sha256}
      activeAuthorized
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);
    const settled = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(settled.textContent).toContain("in use");
    expect((within(settled).getByRole("button", { name: "Use" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("marks a signed row, offers the copy on it, and names what a derived row came from", async () => {
    // Cheat Engine refuses a signed table by returning false and saying
    // nothing, so a row that offers one has to say so before it is pressed and
    // the copy that does open has to be reachable from the same row. The
    // derived copy is an ordinary flat row that names the table it came from.
    const onPrepareCopy = vi.fn().mockResolvedValue(undefined);
    const signed = { ...table, has_signature: true };
    render(<ImportedTablesModal
      tables={[signed] as any}
      activeSha256={null}
      onSelect={vi.fn()}
      onPrepareCopy={onPrepareCopy}
      onClose={vi.fn()}
    />);
    const row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(within(row).getByText("Signed")).toBeTruthy();
    // In front of the two presses every row has, because a row lays its
    // controls out from the right: among them, the extra press stood Use and
    // Delete in a different column from every other row on the screen, which
    // is what measuring it on a Steam Deck showed.
    expect(within(row).getAllByRole("button").map((button) => button.textContent))
      .toEqual(["Prepare", "Use"]);
    // And the chip is in front of the name rather than in the column the
    // controls share, which has about nine pixels to spare on a signed row:
    // a compatibility glyph beside it there would put that row's buttons in a
    // column of their own again.
    const chip = within(row).getByText("Signed");
    const name = within(row).getByText(/Game\.CT/);
    expect(chip.compareDocumentPosition(name) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(row).getAllByRole("button").some((button) => button.contains(chip))).toBe(false);
    const prepare = within(row).getByRole("button", { name: "Prepare" });
    expect(prepare.closest(".ce-decky-prepare")).toBeTruthy();
    fireEvent.click(prepare);
    expect(onPrepareCopy).toHaveBeenCalledWith(table.sha256);

    cleanup();
    // An unsigned table has nothing to prepare, and no mark on it.
    render(<ImportedTablesModal
      tables={[{ ...table, has_signature: false }] as any}
      activeSha256={null}
      onSelect={vi.fn()}
      onPrepareCopy={vi.fn()}
      onClose={vi.fn()}
    />);
    const plain = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(within(plain).queryByText("Signed")).toBeNull();
    expect(within(plain).queryByRole("button", { name: "Prepare" })).toBeNull();

    cleanup();
    // The copy: where it came from, in the slot the row already spends on
    // provenance, rather than being called a local file it never was.
    const derived = {
      ...table, sha256: "d".repeat(64), filename: "Game (unsigned).CT",
      has_signature: false, derived_from: { sha256: table.sha256, transform: "remove-signature" },
    };
    render(<ImportedTablesModal tables={[derived] as any} activeSha256={null} onSelect={vi.fn()} onClose={vi.fn()} />);
    const made = screen.getByTestId(`imported-table-${derived.sha256.slice(0, 8)}`);
    expect(made.textContent).toContain(`derived from ${table.sha256.slice(0, 12)}`);
    expect(made.textContent).not.toContain("Local file");
  });

  it("says nothing about a signature the store has not read yet", () => {
    // Three states, and the third is not the second: a record written before
    // the store read the question carries no answer, and a row that turned
    // that into "not signed" would make the one claim it has no evidence for.
    render(<ImportedTablesModal
      tables={[{ ...table, has_signature: undefined }] as any}
      activeSha256={null}
      onSelect={vi.fn()}
      onPrepareCopy={vi.fn()}
      onClose={vi.fn()}
    />);
    const row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(within(row).queryByText("Signed")).toBeNull();
    expect(within(row).queryByRole("button", { name: "Prepare" })).toBeNull();
  });

  it("gives the game picker's stacked dropdown air under it", () => {
    // The dense field padding is four pixels top and bottom, which centres an
    // inline control and leaves a stacked one sitting on the bottom edge of its
    // own block. On the device the game dropdown did exactly that.
    const { container } = render(<GamePickerModal
      games={[{ appId: 10, name: "Game", sortAs: "Game", isShortcut: false }]}
      selectedGame={null} onPick={vi.fn()} onCancel={vi.fn()} />);
    expect(container.querySelector(".ce-decky-fieldbelow select")).toBeTruthy();
    const css = container.querySelector(".ce-decky-dense style")?.textContent ?? "";
    expect(css).toContain(".ce-decky-fieldbelow");
    expect(css).toMatch(/\.ce-decky-fieldbelow[^{]*\{ padding-bottom: 10px; \}/);

    // And it survives a short screen, which is where it used to go. The
    // short-screen rule takes every field down to two pixels and outranks the
    // ordinary one for this class by two levels of specificity, so a handheld
    // lost this air without either screen that needs it changing a line. The
    // rule that puts it back is inside the media query and outranks that.
    const short = css.slice(css.indexOf("@media (max-height:"));
    expect(short).toMatch(/\.ce-decky-fieldbelow[^{]*\{ padding-bottom: 10px; \}/);
    const takenAway = short.indexOf("padding-top: 2px; padding-bottom: 2px;");
    expect(takenAway).toBeGreaterThan(-1);
    expect(short.indexOf(".ce-decky-fieldbelow")).toBeGreaterThan(takenAway);
  });

  it("opens a local file from here, closing this window first", () => {
    // Decky's picker is a modal of its own, and one raised over another is a
    // window behind a window. This is also the only route to a local file, so
    // it is offered on a device that has never held a table.
    const onOpenLocalFile = vi.fn();
    const onClose = vi.fn();
    render(<ImportedTablesModal
      tables={[]}
      activeSha256={null}
      onOpenLocalFile={onOpenLocalFile}
      onSelect={vi.fn()}
      onClose={onClose}
    />);
    const row = screen.getByTestId("open-local-table");
    fireEvent.click(within(row).getByRole("button", { name: "Local file" }));
    expect(onClose).toHaveBeenCalled();
    expect(onOpenLocalFile).toHaveBeenCalled();
    expect(onClose.mock.invocationCallOrder[0]).toBeLessThan(onOpenLocalFile.mock.invocationCallOrder[0]);
  });

  it("says it holds nothing once the last table has been deleted", async () => {
    // The screen asked the props it was opened with, so after deleting the only
    // table it said nothing matched a filter nobody had set, and kept offering
    // a filter and a pager for rows that were gone.
    const only = { ...table, sha256: "c".repeat(64), filename: "Only.CT" };
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[only] as any}
      activeSha256={null}
      onDelete={vi.fn().mockResolvedValue(undefined)}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);

    const rowId = `stored-table-${only.sha256.slice(0, 8)}`;
    fireEvent.click(within(screen.getByTestId(rowId)).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(screen.getByTestId(rowId)).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(screen.queryByTestId(rowId)).toBeNull());

    expect(screen.getByText("Nothing imported yet")).toBeTruthy();
    expect(screen.queryByText("Nothing matches that")).toBeNull();
  });

  it("says why a table could not be used, on the row rather than over it", async () => {
    // The same contract as the press beside it: this screen is a modal, and a
    // window raised over it can appear behind it and read as a press that did
    // nothing.
    const onSelect = vi.fn().mockRejectedValue(new Error("the table's file is gone"));
    render(<ImportedTablesModal
      tables={[table] as any}
      activeSha256={null}
      onSelect={onSelect}
      onClose={vi.fn()}
    />);

    const row = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    fireEvent.click(within(row).getByRole("button", { name: "Use" }));
    await waitFor(() => expect(screen.getByTestId("manage-failure").textContent).toContain("file is gone"));
    // The screen stays, and the row can be pressed again.
    expect(screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`)).toBeTruthy();
    expect((within(row).getByRole("button", { name: "Use" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("draws the row that heads the list as a heading", () => {
    // As an ordinary field it carried the same weight as the tables under it,
    // so the first thing on the list read as the first table on it.
    render(<ImportedTablesModal
      tables={[table] as any}
      activeSha256={null}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);
    const summary = screen.getByTestId("manage-summary");
    expect(summary.className).toContain("rowhead");
    expect(summary.textContent).toContain("1 here, 0 elsewhere");
    // And the tables under it are not headings. They carry the focus-scroll
    // class of their own, which is about revealing a clipped line and is not a
    // tone.
    expect(screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`).className).not.toContain("rowhead");
  });

  it("draws a row from another game on its own ground", () => {
    // The two groups are one list so the screen has one page budget, and the
    // boundary was a few words at the end of one label: a page that opens in
    // the middle of the second group carried no boundary at all.
    const mine = { ...table, sha256: "a".repeat(64), filename: "Mine.CT" };
    const theirs = { ...table, sha256: "b".repeat(64), filename: "Theirs.CT" };
    render(<ImportedTablesModal
      tables={[mine] as any}
      otherTables={[theirs] as any}
      activeSha256={null}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);
    expect(screen.getByTestId(`imported-table-${mine.sha256.slice(0, 8)}`).className).not.toContain("rowaside");
    expect(screen.getByTestId(`stored-table-${theirs.sha256.slice(0, 8)}`).className).toContain("rowaside");
  });

  it("mounts one page of table rows for the whole screen, not one per list", () => {
    // Two sections paging six each is twelve rows plus two pagers, two headings,
    // the file row, the filter and the way out: the same unreachable screen the
    // paging was added to prevent. One budget for the screen, one pager.
    const mine = Array.from({ length: 6 }, (_, index) => ({
      ...table, sha256: `a${index}`.padEnd(64, "0"), filename: `Mine-${index}.CT`,
    }));
    const device = Array.from({ length: 30 }, (_, index) => ({
      ...table, sha256: `b${index}`.padEnd(64, "0"), filename: `Theirs-${String(index).padStart(2, "0")}.CT`,
    }));
    render(<ImportedTablesModal
      tables={mine as any}
      otherTables={device as any}
      activeSha256={null}
      onOpenLocalFile={vi.fn()}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);

    expect(screen.getAllByRole("button", { name: "Use" }).length).toBe(6);
    expect(screen.getByTestId("manage-footer").textContent).toContain("1 / 6");
    // This game's own come first, and the whole catalogue is still accounted for.
    expect(screen.getByText(/6 here, 30 elsewhere/)).toBeTruthy();
    expect(screen.getByTestId(`imported-table-${mine[0].sha256.slice(0, 8)}`)).toBeTruthy();

    // And the device's tables are reached by paging the one list.
    fireEvent.click(within(screen.getByTestId("manage-footer")).getByRole("button", { name: "Next \u203a" }));
    expect(screen.getAllByRole("button", { name: "Use" }).length).toBe(6);
    expect(screen.getByTestId(`stored-table-${device[0].sha256.slice(0, 8)}`)).toBeTruthy();
  });

  it("fits a page of tables to the screen rather than always drawing six", () => {
    // Manage was the one paged list here that measured nothing: six was written
    // as the answer for a handheld, and on a Steam Deck's 534 pixel page a page
    // of six ran off the bottom of the window.
    const height = window.innerHeight;
    Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
    // A list that starts 300 pixels down, with a 40 pixel footer under it and
    // 57 below that, which is the modal's own padding plus the 41 pixel bar
    // Steam paints along the bottom of the page: 137 left, two rows of 58.
    const rects = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        const testId = this.dataset.testid;
        // The rows are 58 tall, which is what the page divides the room by now
        // that it measures a row instead of reading a constant.
        const box = testId === "manage-footer" ? { top: 340, height: 40 }
          : testId === "manage-list" ? { top: 300, height: 58 }
            : { top: 0, height: 58 };
        return { ...box, width: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
      });
    try {
      const mine = Array.from({ length: 8 }, (_, index) => ({
        ...table, sha256: `a${index}`.padEnd(64, "0"), filename: `Mine-${index}.CT`,
      }));
      render(<ImportedTablesModal
        tables={mine as any}
        otherTables={[] as any}
        activeSha256={null}
        onSelect={vi.fn()}
        onClose={vi.fn()}
      />);

      expect(screen.getAllByRole("button", { name: "Use" }).length).toBe(2);
      expect(screen.getByTestId("manage-footer").textContent).toContain("1 / 4");
    } finally {
      rects.mockRestore();
      Object.defineProperty(window, "innerHeight", { value: height, configurable: true });
    }
  });

  it("holds the last page at the height the first page measured, gaps and all", () => {
    // Reported from the device: the window moved by a couple of pixels between
    // the first page and the last. A page of six rows is six rows plus the five
    // gaps between them, and counting rows alone loses exactly those.
    const row = 24;
    const page = row * 6 + 10;
    const rects = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        const height = this.dataset.testid === "manage-list" ? page : row;
        return { height, width: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect;
      });
    try {
      const mine = Array.from({ length: 8 }, (_, index) => ({
        ...table, sha256: `a${index}`.padEnd(64, "0"), filename: `Mine-${index}.CT`,
      }));
      render(<ImportedTablesModal
        tables={mine as any}
        otherTables={[] as any}
        activeSha256={null}
        onSelect={vi.fn()}
        onClose={vi.fn()}
      />);

      const list = () => screen.getByTestId("manage-list");
      expect(list().style.minHeight).toBe(`${page}px`);
      // The last page holds two rows and the same height.
      fireEvent.click(within(screen.getByTestId("manage-footer")).getByRole("button", { name: "Next \u203a" }));
      expect(screen.getAllByRole("button", { name: "Use" }).length).toBe(2);
      expect(list().style.minHeight).toBe(`${page}px`);
    } finally {
      rects.mockRestore();
    }
  });

  it("keeps Manage the same size with one short page, and pages from the same footer Search does", () => {
    // A device holding two tables has one page and never fills one, which is
    // the state this screen is usually opened in: the pager is still there so
    // it is not a control that appears with the second page, the way out is
    // beside it on the same row rather than under the list, and the list holds
    // a whole page of height so filtering does not resize the window.
    const rows = 24;
    const rects = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue({ height: rows, width: 0, top: 0, left: 0, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) } as DOMRect);
    try {
      const mine = Array.from({ length: 2 }, (_, index) => ({
        ...table, sha256: `a${index}`.padEnd(64, "0"), filename: `Mine-${index}.CT`,
      }));
      render(<ImportedTablesModal
        tables={mine as any}
        otherTables={[] as any}
        activeSha256={null}
        onSelect={vi.fn()}
        onClose={vi.fn()}
      />);

      const footer = screen.getByTestId("manage-footer");
      expect(footer.textContent).toContain("1 / 1");
      expect(within(footer).getByRole("button", { name: "\u2039 Previous" })).toHaveProperty("disabled", true);
      expect(within(footer).getByRole("button", { name: "Next \u203a" })).toHaveProperty("disabled", true);
      expect(within(footer).getByRole("button", { name: "Back" })).toBeTruthy();
      // Six rows of height for a page of two, read off a row rather than from a
      // pixel constant.
      expect(screen.getByTestId("manage-list").style.minHeight).toBe(`${rows * 6}px`);
    } finally {
      rects.mockRestore();
    }
  });

  it("puts the table the panel sent the reader here to authorize on the first page", () => {
    // Home tells the user to authorize the selected table here, and sorting by
    // filename could put it pages away: the one row the screen exists for was
    // the one row that needed searching for.
    const others = Array.from({ length: 20 }, (_, index) => ({
      ...table, sha256: `a${index}`.padEnd(64, "0"), filename: `Aaa-${String(index).padStart(2, "0")}.CT`,
    }));
    const active = { ...table, sha256: "z".repeat(64), filename: "Zzz-last.CT" };
    render(<ImportedTablesModal
      tables={[...others, active] as any}
      activeSha256={active.sha256}
      activeAuthorized={false}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);

    const row = screen.getByTestId(`imported-table-${active.sha256.slice(0, 8)}`);
    expect(row.textContent).toContain("needs authorizing");
    expect((within(row).getByRole("button", { name: "Use" }) as HTMLButtonElement).disabled).toBe(false);
    // First on the page, ahead of every name that sorts before it.
    const first = screen.getAllByRole("button", { name: "Use" })[0];
    expect(within(row).getByRole("button", { name: "Use" })).toBe(first);
  });

  it("does not offer to use another game's table for this one", () => {
    // A cheat table is written against one game's code, and the press was not
    // merely useless: it associated the table with this game before the review
    // opened, and cancelling that review did not take the association back. One
    // press put Half-Life 2's table permanently into Neon Bazaar's library.
    const theirs = { ...table, sha256: "f".repeat(64), filename: "Other.CT" };
    const orphan = { ...table, sha256: "a".repeat(64), filename: "Orphan.CT" };
    const onSelect = vi.fn();
    render(<ImportedTablesModal
      tables={[table] as any}
      otherTables={[theirs, orphan] as any}
      owners={{ [theirs.sha256]: "Half-Life 2" }}
      activeSha256={null}
      onSelect={onSelect}
      onDelete={vi.fn()}
      onClose={vi.fn()}
    />);
    // This game's own row still offers it.
    const mine = screen.getByTestId(`imported-table-${table.sha256.slice(0, 8)}`);
    expect(within(mine).getByRole("button", { name: "Use" })).toBeTruthy();
    // The other game's row does not, and keeps what the group is for.
    const other = screen.getByTestId(`stored-table-${theirs.sha256.slice(0, 8)}`);
    expect(within(other).queryByRole("button", { name: "Use" })).toBeNull();
    expect(within(other).getByRole("button", { name: "Delete" })).toBeTruthy();
    // A table no profile claims keeps it: after a recovery that discards the
    // profile store, this press is the only thing left that can name the bytes.
    const unclaimed = screen.getByTestId(`stored-table-${orphan.sha256.slice(0, 8)}`);
    expect(within(unclaimed).getByRole("button", { name: "Use" })).toBeTruthy();
  });

  it("marks a row belonging to another game with that game's own answer", () => {
    // The list is one screen of two groups, and the evidence used to be
    // filtered to the selected game, so every row in the second group showed
    // exactly what a table nobody has ever tried shows. The state is computed
    // per record against its own game's build, and each of those rows names
    // the game it is about, so it is kept rather than downgraded.
    const theirs = { ...table, sha256: "e".repeat(64), filename: "Other.CT" };
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[theirs] as any}
      activeSha256={null}
      canSelect={false}
      compatibility={manageCompatibility([{
        app_id: 900, table_sha256: theirs.sha256, target_process: "other.exe",
        pe_version: "1", steam_build_id: null, last_working_at: 5,
        invalidated: false, state: "matching",
      }] as any, 10)}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);
    const row = screen.getByTestId(`stored-table-${theirs.sha256.slice(0, 8)}`);
    expect(within(row).getByLabelText("Worked on this build")).toBeTruthy();
  });

  it("keeps every state on the row, not just the first one that applies", () => {
    // A held row that is also marked not working lost the line accounting for
    // its missing Delete: the status returned on the first match, so the row
    // said only that it was marked and the button was simply gone.
    const held = { ...table, sha256: "d".repeat(64), filename: "Held.CT" };
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[held] as any}
      activeSha256={null}
      canSelect={false}
      selectedBy={{ [held.sha256]: { count: 1, names: ["Another Game"] } }}
      blockedReasons={{ [held.sha256]: {
        sha256: held.sha256, reason: "it went straight back off", cause: "refused" as const, recordedAt: 2,
      } }}
      onSelect={vi.fn()}
      onDelete={vi.fn()}
      onClose={vi.fn()}
    />);
    const row = screen.getByTestId(`stored-table-${held.sha256.slice(0, 8)}`);
    expect(row.textContent).toContain("Marked as not working");
    expect(row.textContent).toContain("In use, cannot be deleted");
    expect(within(row).queryByRole("button", { name: "Delete" })).toBeNull();
    // And no help button for it: the row already reveals its whole line under
    // the ring, so a `?` would be a second control for text that is already
    // reachable.
    expect(within(row).queryByRole("button", { name: "?" })).toBeNull();
  });

  it("does not offer to delete a table a game is on, and says which game", () => {
    // The backend refuses every table a profile selects, so the press could
    // only spend itself on that refusal.
    const held = { ...table, sha256: "c".repeat(64), filename: "Held.CT" };
    const free = { ...table, sha256: "d".repeat(64), filename: "Free.CT" };
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[held, free] as any}
      activeSha256={null}
      selectedBy={{ [held.sha256]: { names: ["Another Game"], count: 1 } }}
      onDelete={vi.fn()}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);

    const heldRow = screen.getByTestId(`stored-table-${held.sha256.slice(0, 8)}`);
    expect(within(heldRow).queryByRole("button", { name: "Delete" })).toBeNull();
    // In use is in use, whichever game is on it, and it is said on the label
    // exactly as it is for this game's own table. Without it a row another game
    // is running looked like any other, with a missing Delete and nothing on
    // the line to account for it.
    expect(heldRow.textContent).toContain("in use by Another Game");
    expect(heldRow.textContent).toContain("In use, cannot be deleted");
    const freeRow = screen.getByTestId(`stored-table-${free.sha256.slice(0, 8)}`);
    expect(within(freeRow).getByRole("button", { name: "Delete" })).toBeTruthy();
  });

  it("disarms a confirmation the reader has navigated away from", () => {
    // Two presses on one control only mean anything while they are one
    // interaction. Coming back to a row hours later must not find it one press
    // from destroying a file.
    const many = Array.from({ length: 12 }, (_, index) => ({
      ...table, sha256: `a${index}`.padEnd(64, "0"), filename: `Table-${String(index).padStart(2, "0")}.CT`,
    }));
    render(<ImportedTablesModal
      tables={many as any}
      activeSha256={null}
      onDelete={vi.fn()}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);

    const armedRow = () => screen.getByTestId(`imported-table-${many[0].sha256.slice(0, 8)}`);
    fireEvent.click(within(armedRow()).getByRole("button", { name: "Delete" }));
    expect(within(armedRow()).getByRole("button", { name: "Confirm" })).toBeTruthy();

    fireEvent.click(within(screen.getByTestId("manage-footer")).getByRole("button", { name: "Next \u203a" }));
    fireEvent.click(within(screen.getByTestId("manage-footer")).getByRole("button", { name: "\u2039 Previous" }));
    expect(within(armedRow()).getByRole("button", { name: "Delete" })).toBeTruthy();

    // And filtering it away and back is the same answer.
    fireEvent.click(within(armedRow()).getByRole("button", { name: "Delete" }));
    fireEvent.change(screen.getByLabelText("Filter"), { target: { value: "Table-05" } });
    fireEvent.change(screen.getByLabelText("Filter"), { target: { value: "" } });
    expect(within(armedRow()).getByRole("button", { name: "Delete" })).toBeTruthy();
  });

  it("takes a deleted row away, and keeps one whose deletion failed", async () => {
    // This window is opened once with the lists as they were, and the panel
    // cannot re-prop a window already on screen, so it reconciles what it did
    // itself - and only what actually succeeded.
    const gone = { ...table, sha256: "c".repeat(64), filename: "Gone.CT" };
    const stays = { ...table, sha256: "d".repeat(64), filename: "Stays.CT" };
    const onDelete = vi.fn()
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error("the table store is read-only"));
    render(<ImportedTablesModal
      tables={[]}
      otherTables={[gone, stays] as any}
      activeSha256={null}
      onDelete={onDelete}
      onSelect={vi.fn()}
      onClose={vi.fn()}
    />);

    const goneId = `stored-table-${gone.sha256.slice(0, 8)}`;
    fireEvent.click(within(screen.getByTestId(goneId)).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(screen.getByTestId(goneId)).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(screen.queryByTestId(goneId)).toBeNull());

    const staysId = `stored-table-${stays.sha256.slice(0, 8)}`;
    fireEvent.click(within(screen.getByTestId(staysId)).getByRole("button", { name: "Delete" }));
    fireEvent.click(within(screen.getByTestId(staysId)).getByRole("button", { name: "Confirm" }));
    // The row survives, and the reason is said on this screen rather than in a
    // window raised over it.
    await waitFor(() => expect(screen.getByTestId("manage-failure").textContent).toContain("read-only"));
    expect(screen.getByTestId(staysId)).toBeTruthy();
  });

  it("cannot close imported-table selection while its durable handoff is pending", async () => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = () => resolve(); });
    const onClose = vi.fn();
    render(<ImportedTablesModal
      tables={[table] as any}
      activeSha256={null}
      onSelect={() => pending}
      onClose={onClose}
    />);

    fireEvent.click(screen.getByRole("button", { name: "Use" }));
    const back = screen.getByRole("button", { name: "Back" }) as HTMLButtonElement;
    expect(back.disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Escape" }));
    expect(onClose).not.toHaveBeenCalled();

    await act(async () => { finish(); await pending; });
    await waitFor(() => expect(back.disabled).toBe(false));
  });
});


describe("Manage recovery after removal", () => {
  it("keeps an active filter reachable after crossing the page threshold", async () => {
    const tables = Array.from({ length: 7 }, (_, i) => ({ ...table,
      sha256: String(i + 1).repeat(64), filename: i ? `Other-${i}.CT` : "Match.CT" }));
    render(<ImportedTablesModal tables={tables as any} activeSha256={null}
      onDelete={vi.fn().mockResolvedValue(undefined)} onSelect={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Filter"), { target: { value: "Match" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await screen.findByText("Nothing matches that");
    // Focus lands from an effect after the row it was on is gone, so this is
    // awaited exactly like the deletion tests below it: reading it in the same
    // tick as the render passed on a fast machine and raced on a loaded one.
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back", exact: true })));
    fireEvent.change(screen.getByLabelText("Filter"), { target: { value: "" } });
    expect(screen.getAllByRole("button", { name: "Use" })).toHaveLength(6);
  });

  it("restores focus to a surviving action, then Back after the last deletion", async () => {
    const other = { ...table, sha256: "b".repeat(64), filename: "Next.CT" };
    render(<ImportedTablesModal tables={[table, other] as any} activeSha256={null}
      onDelete={vi.fn().mockResolvedValue(undefined)} onSelect={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[0]);
    const confirm = screen.getByRole("button", { name: "Confirm" });
    confirm.focus(); fireEvent.click(confirm);
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Use" })));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back", exact: true })));
  });

  it("offers removal but never Use for an unavailable catalogue entry", () => {
    render(<ImportedTablesModal tables={[{ ...table, available: false }] as any} activeSha256={null}
      onDelete={vi.fn()} onSelect={vi.fn()} onClose={vi.fn()} />);
    expect((screen.getByRole("button", { name: "Use" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole("button", { name: "Delete" })).toBeTruthy();
    expect(screen.getByTestId(`imported-table-${SHA.slice(0, 8)}`).textContent).toContain("File missing or damaged");
  });

  it("names every holder even when the current game also holds the digest", () => {
    render(<ImportedTablesModal tables={[table] as any} activeSha256={SHA}
      selectedBy={{ [SHA]: { names: ["Current", "Another", "Third"], count: 3 } }}
      onDelete={vi.fn()} onSelect={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByTestId(`imported-table-${SHA.slice(0, 8)}`).textContent).toContain("in use by Current, Another, Third");
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
  });
});


it("bounds the rendered holder label for the maximum profile count and long names", () => {
  const profiles = Array.from({ length: 10_000 }, (_, i) => ({
    app_id: i + 1, name: `${i}`.padEnd(1024, "x"), table_sha256: SHA,
  }));
  render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
    selectedBy={aggregateTableHolders(profiles)} onDelete={vi.fn()} onSelect={vi.fn()} onClose={vi.fn()} />);
  const row = screen.getByTestId(`imported-table-${SHA.slice(0, 8)}`);
  expect(row.textContent).toContain("(+9997)");
  expect(row.textContent!.length).toBeLessThan(500);
  expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
});

it("protects a held table even when no holder names are displayed", () => {
  render(<ImportedTablesModal tables={[table] as any} activeSha256={null}
    selectedBy={{ [SHA]: { names: [], count: 7 } }} onDelete={vi.fn()} onSelect={vi.fn()} onClose={vi.fn()} />);
  expect(screen.getByText(/in use by 7 games/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
});


it("keeps long information values clipped to one passive line", async () => {
  const { InfoFields } = await import("../src/components/PanelDensity");
  const value = "A very long source filename ".repeat(30);
  const view = render(<InfoFields items={[{ label: "Source", description: value }]} />);
  const line = screen.getByText(value.trim());
  expect(line.style.whiteSpace).toBe("nowrap");
  expect(line.style.overflow).toBe("hidden");
  expect(line.style.textOverflow).toBe("ellipsis");
  expect(line.style.height).toBe("14px");
  expect(view.container.querySelectorAll("button,[tabindex]").length).toBe(0);
});

it("keeps Manage's filter in the row that heads its list, and reachable when it matches nothing", () => {
  // A row of its own cost 46 of a Steam Deck's 534 pixels for a control that
  // holds a few characters, which is one table off the page on the screen whose
  // whole job is listing them. And the row it rides on used to be drawn only
  // while the list had rows in it, so a filter matching nothing took away the
  // only control that could clear it.
  const tables = Array.from({ length: 12 }, (_, index) => ({
    ...table, sha256: `${index}`.padStart(64, "a"), filename: `Table-${index}.CT`,
  }));
  render(<ImportedTablesModal tables={tables as any} activeSha256={null} canSelect
    onDelete={vi.fn().mockResolvedValue(undefined)} onSelect={vi.fn().mockResolvedValue(undefined)} onClose={vi.fn()} />);

  const summary = screen.getByTestId("manage-summary");
  expect(within(summary).getByLabelText("Filter")).toBeTruthy();
  // The word is in the box rather than above it, where it costs no height and
  // is gone the moment there is anything to read.
  expect(within(summary).getByLabelText("Filter").getAttribute("placeholder")).toBe("Filter");

  // And it takes the width the count beside it is not using, rather than a
  // number chosen in the file: cut at a fraction of the row, the box could not
  // show back what had been typed into it, on the screen whose names are as
  // long as `NeonBazaar-ItemNoDecreaseUpdate.CT`.
  const filled = within(summary).getByLabelText("Filter").closest("[style*='width: 100%']");
  expect(filled).toBeTruthy();

  fireEvent.change(within(summary).getByLabelText("Filter"), { target: { value: "nothing matches this" } });
  expect(screen.getByText("Nothing matches that")).toBeTruthy();
  expect(within(screen.getByTestId("manage-summary")).getByLabelText("Filter")).toBeTruthy();
});

it("lands the ring on Next when the reader comes down into a pager", () => {
  // Previous is the first control in that row and is disabled on page one, so a
  // reader arriving from the list landed on a button that does nothing and read
  // the whole footer as dead. Next is the press they came down to make.
  const tables = Array.from({ length: 12 }, (_, index) => ({
    ...table, sha256: `${index}`.padStart(64, "b"), filename: `Table-${index}.CT`,
  }));
  render(<ImportedTablesModal tables={tables as any} activeSha256={null} canSelect
    onDelete={vi.fn().mockResolvedValue(undefined)} onSelect={vi.fn().mockResolvedValue(undefined)} onClose={vi.fn()} />);

  const footer = screen.getByTestId("manage-footer");
  const group = footer.querySelector("[data-nav-entry]");
  // Two halves of one statement, and both are needed: the group asks Steam to
  // enter at its preferred child, and the child says it is that child.
  // `PREFERRED_CHILD` with nothing marked preferred falls back to the first
  // one, which is Previous - the control this exists to skip.
  expect(group?.getAttribute("data-nav-entry")).toBe("4");
  expect(screen.getByRole("button", { name: "Next \u203a" }).getAttribute("data-preferred-focus")).toBe("true");
});

it("offers a local file whether or not a game is chosen", () => {
  // The file joins this device's library with no game at all; what waits for a
  // game is the association and the authorization, and Use on the row the
  // import produces is where those happen. Taking the section away left the
  // reader with no sign the route existed on the one screen that carries it.
  const onOpenLocalFile = vi.fn();
  const onClose = vi.fn();
  render(<ImportedTablesModal tables={[] as any} activeSha256={null} canSelect={false}
    onOpenLocalFile={onOpenLocalFile} onDelete={vi.fn()} onSelect={vi.fn()} onClose={onClose} />);
  const row = screen.getByTestId("open-local-table");
  const press = within(row).getByRole("button", { name: "Local file" }) as HTMLButtonElement;
  expect(press.disabled).toBe(false);
  fireEvent.click(press);
  // This window first: Decky's picker is a modal of its own.
  expect(onClose).toHaveBeenCalled();
  expect(onOpenLocalFile).toHaveBeenCalled();
});

it("heads its list with one row whether or not a game is chosen", () => {
  // With no game selected there used to be a bare sentence above the header
  // row: a field carrying a description and no label, which Steam draws at
  // description weight, so it read as an offcut with the real heading directly
  // under it. The sentence is that heading's description now.
  const tables = Array.from({ length: 3 }, (_, index) => ({
    ...table, sha256: `${index}`.padStart(64, "c"), filename: `Table-${index}.CT`,
  }));
  const chosen = render(<ImportedTablesModal tables={tables as any} activeSha256={null} canSelect
    onDelete={vi.fn().mockResolvedValue(undefined)} onSelect={vi.fn().mockResolvedValue(undefined)} onClose={vi.fn()} />);
  expect(screen.getByTestId("manage-summary").textContent).toContain("No provider or network needed");
  expect(screen.queryByText(/Choose a game to use one/)).toBeNull();
  chosen.unmount();

  render(<ImportedTablesModal tables={[] as any} otherTables={tables as any} activeSha256={null} canSelect={false}
    onDelete={vi.fn().mockResolvedValue(undefined)} onSelect={vi.fn().mockResolvedValue(undefined)} onClose={vi.fn()} />);
  const heading = screen.getByTestId("manage-summary");
  expect(heading.textContent).toContain("Choose a game to use one");
  // And it is the only thing saying it, on the row that already heads the list.
  expect(screen.getAllByText(/Choose a game to use one/)).toHaveLength(1);
});


describe("a page that was fitted before another block joined the screen", () => {
  const ROW = 43;

  function Screen() {
    const [failed, setFailed] = React.useState(false);
    const [confirming, setConfirming] = React.useState(false);
    // One footer node, the way a screen has one: its identity never changes and
    // neither does its size. All that changes is where it ends up, which is
    // what a block appearing above the list does to it and what this hook could
    // not see.
    const place = React.useRef(494);
    const footer = React.useMemo(() => ({
      ownerDocument: { defaultView: { innerHeight: 534 } },
      getBoundingClientRect: () => ({ top: place.current - 48, height: 48, bottom: place.current }),
    }) as unknown as Element, []);
    // One key for every block this screen can mount on its own, which is what
    // keeps a second block from needing a second parameter nobody passes.
    const fitted = useFittedRows(footer, ROW, true, true, (failed ? 1 : 0) | (confirming ? 2 : 0));
    // One row past Steam's bar to begin with, and one row lower again while a
    // failure row is drawn. Every row the page gives up lifts the footer by
    // exactly one row.
    place.current = (failed || confirming ? 537 : 494) + fitted * ROW;
    return (
      <>
        <span data-testid="fitted">{fitted}</span>
        <button data-testid="fail" onClick={() => setFailed((held) => !held)}>fail</button>
        <button data-testid="confirm" onClick={() => setConfirming((held) => !held)}>confirm</button>
      </>
    );
  }

  it("gives up another row when a failure row pushes its footer back past the bar", () => {
    // These screens report a failed press on a row of their own. It moves the
    // footer without changing its identity or its size, which is all this hook
    // used to watch, so a page that had already fitted went behind Steam's bar
    // and stayed there - the exact class the fitter was written to remove.
    render(<Screen />);
    expect(screen.getByTestId("fitted").textContent).toBe("-1");

    fireEvent.click(screen.getByTestId("fail"));
    expect(screen.getByTestId("fitted").textContent).toBe("-2");
  });

  it("takes the row back when that block goes away again", () => {
    // The correction budget belongs to the layout it was spent on. Growing is
    // refused once a screen has shrunk, which is what keeps one from chasing
    // rows that do not exist - but carried across a layout change it left the
    // page a row short for the rest of the window's life, for a failure the
    // reader had already dealt with.
    render(<Screen />);
    fireEvent.click(screen.getByTestId("fail"));
    expect(screen.getByTestId("fitted").textContent).toBe("-2");

    fireEvent.click(screen.getByTestId("fail"));
    expect(screen.getByTestId("fitted").textContent).toBe("-1");
  });

  it("does the same for a second block that has nothing to do with the first", () => {
    // The cheat picker mounts two of these on its own: a refusal it reports and
    // the acknowledgement it asks for before closing over unapplied edits. One
    // flag per block is a flag somebody forgets, so the screen composes them
    // into one key and this is what says that key is what gets watched.
    render(<Screen />);
    expect(screen.getByTestId("fitted").textContent).toBe("-1");

    fireEvent.click(screen.getByTestId("confirm"));
    expect(screen.getByTestId("fitted").textContent).toBe("-2");
  });
});
