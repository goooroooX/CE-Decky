import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  listTableCode: vi.fn(),
  readTableCode: vi.fn(),
}));

vi.mock("../src/api", () => api);
vi.mock("@decky/api", () => ({ toaster: { toast: vi.fn() } }));
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  DialogButton: ({ children, onClick, disabled, preferredFocus }: any) => <button disabled={disabled} data-preferred-focus={preferredFocus ? "true" : undefined} onClick={onClick}>{children}</button>,
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: React.forwardRef(({ children, onActivate, navEntryPreferPosition }: any, ref: any) => <div ref={ref} data-testid="focusable" data-nav-entry={navEntryPreferPosition} onClick={onActivate}>{children}</div>),
  ModalRoot: ({ children }: any) => <div>{children}</div>,
  PanelSection: ({ title, children }: any) => <section aria-label={title}>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  Toggle: () => null,
  gamepadDialogClasses: { Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription", CompactPadding: "CompactPadding" },
  showModal: vi.fn(() => ({ Close: vi.fn() })),
}));

import { TableCodeModal, codeRows } from "../src/modals/TableCodeModal";

const SHA = "f".repeat(64);

function index(sections: any[], totals: Record<string, number> = {}, records = 12) {
  return {
    schema: 1,
    sha256: SHA,
    size: 65536,
    sections,
    totals: { lua: 0, auto_assembler: 0, form: 0, embedded_file: 0, ...totals },
    records,
    omitted_sections: 0,
  };
}

const script = {
  id: "aa:0",
  kind: "auto_assembler",
  title: "Infinite Health",
  path: ["Player"],
  bytes: 512,
  lines: 24,
  truncated: false,
  readable: true,
};

beforeEach(() => {
  vi.clearAllMocks();
  api.listTableCode.mockResolvedValue(index([script], { auto_assembler: 1 }));
  api.readTableCode.mockResolvedValue({
    schema: 1,
    id: "aa:0",
    kind: "auto_assembler",
    title: "Infinite Health",
    path: ["Player"],
    lines: ["[ENABLE]", "aobscanmodule(inj,game.exe,89 04 8A)", "[DISABLE]"],
    total_lines: 3,
    truncated: false,
    sanitized: false,
    readable: true,
  });
});

afterEach(cleanup);

describe("looking inside a table", () => {
  it("says how many cheats the table declares beside how few of them run code", async () => {
    // A real table here holds eighteen cheats behind two scripts. The screen
    // said "2 Auto Assembler" and nothing else, which reads as a table whose
    // other sixteen cheats are being withheld rather than one whose cheats are
    // addresses a script creates.
    api.listTableCode.mockResolvedValue(index([script], { auto_assembler: 1 }, 18));
    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    await screen.findByRole("button", { name: "Read" });
    const summary = screen.getByTestId("table-code-summary");
    expect(summary.textContent).toContain("1 Auto Assembler");
    expect(summary.textContent).toContain("18 cheat(s) declared");
  });

  it("starts only one section read for repeated presses before React commits", async () => {
    let fail!: (cause: Error) => void;
    api.readTableCode.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject; }));
    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    const read = await screen.findByRole("button", { name: "Read" });
    act(() => { read.click(); read.click(); });
    expect(api.readTableCode).toHaveBeenCalledOnce();
    await act(async () => { fail(new Error("read refused")); });
    expect(await screen.findByText("read refused")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Read" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("lists what the table can execute and reads one script on a press", async () => {
    // The decision this product asks for is whether an exact SHA may run what
    // it carries, and the only answer on offer was a count of the markers.
    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);

    expect(await screen.findByText("Infinite Health")).toBeTruthy();
    // Named by the cheat and by the groups it sits under, because one table
    // here carries ninety-nine scripts and thirty of them are called "Enable".
    // The groups come last: the row is one truncated line, so what tells these
    // rows apart has to be in front of where they sit.
    expect(screen.getByText(/Auto Assembler · 512 B · 24 line\(s\) · Player/)).toBeTruthy();
    // The index is asked for once and carries no body text: one real table
    // holds half a megabyte of scripts across its records.
    expect(api.listTableCode).toHaveBeenCalledTimes(1);
    expect(api.readTableCode).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Read" }));
    await waitFor(() => expect(api.readTableCode).toHaveBeenCalledWith(SHA, "aa:0"));
    expect(await screen.findByText(/aobscanmodule/)).toBeTruthy();
  });

  it("says an embedded payload is described rather than opened", async () => {
    // Its bytes are the one thing in a table that exists to be executed
    // somewhere else, so the view names it and never carries it.
    api.listTableCode.mockResolvedValue(index([{
      ...script, id: "file:0", kind: "embedded_file", title: "celua_teleport.lua", readable: false, lines: 1,
    }], { embedded_file: 1 }));
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "file:0", kind: "embedded_file", title: "celua_teleport.lua", path: [],
      lines: [], total_lines: 0, truncated: false, sanitized: false, readable: false,
    });

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "About" }));

    expect(await screen.findByText("This is a payload, not a script")).toBeTruthy();
  });

  it("lets a press reach the end of a line the backend allowed whole", async () => {
    // The backend allows one displayed line of 2048 bytes and reports it as
    // not truncated, and the window is 26 rows. A line between those two was
    // clipped by the block with no page holding the rest of it, on the screen
    // that exists to show exactly what an exact SHA would execute.
    const line = `${"a".repeat(2040)}SENTINEL`;
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "aa:0", kind: "auto_assembler", title: "Infinite Health", path: [],
      lines: [line], total_lines: 1, truncated: false, sanitized: false, readable: true,
    });

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Read" }));
    await screen.findByTestId("table-code-footer");

    // Nothing here says the section was cut, because it was not.
    expect(screen.queryByText(/longer than the panel will show/)).toBeNull();

    const shown = () => screen.getByTestId("table-code-section").parentElement?.textContent ?? "";
    // The window holds 26 rows of it and not the whole line, which is the
    // whole point: the block clips what it is given and cannot scroll.
    expect(shown()).not.toContain("SENTINEL");

    let seen = shown();
    for (;;) {
      const next = screen.getByRole("button", { name: "Next \u203a" }) as HTMLButtonElement;
      if (next.disabled) break;
      fireEvent.click(next);
      seen += shown();
    }
    expect(seen).toContain("SENTINEL");
  });

  it("draws a page as fixed rows that cannot wrap into a taller one", async () => {
    // The page used to be one block of text the browser wrapped, so how much of
    // it was on screen depended on a width estimate agreeing with a font this
    // project does not ship: read one character narrow and the end of the page
    // went under the block's own overflow, unreachable and unannounced. Every
    // line here is emoji, which is exactly the family such an estimate misses.
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "aa:0", kind: "auto_assembler", title: "Infinite Health", path: [],
      lines: Array.from({ length: 40 }, (_, index) => `${"\u{1f600}".repeat(34)}${index}`),
      total_lines: 40, truncated: false, sanitized: false, readable: true,
    });

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Read" }));
    await screen.findByTestId("table-code-block");

    // One element per drawn row, never more of them than the window holds, and
    // each one is a row that cannot become two.
    const rows = () => screen.getAllByTestId("table-code-row");
    expect(rows().length).toBeLessThanOrEqual(26);
    expect(rows()[0].style.whiteSpace).toBe("pre");

    // And the last line is still reachable, which is what the rows are for.
    let seen = "";
    for (;;) {
      seen += screen.getByTestId("table-code-block").textContent ?? "";
      expect(rows().length).toBeLessThanOrEqual(26);
      const next = screen.getByRole("button", { name: "Next \u203a" }) as HTMLButtonElement;
      if (next.disabled) break;
      fireEvent.click(next);
    }
    expect(seen).toContain("39");
  });

  it("cuts the page to what a handheld screen actually holds, and leaves a full-size one alone", async () => {
    // A handheld gives this page 534 CSS pixels where a television gives 844,
    // and twenty-six rows plus this screen's own chrome does not fit the first:
    // the sheet was drawn from above the top of the display, so the heading,
    // the row naming the section and the first lines of every page had no press
    // that could reach them. The block is still the same height on every page
    // of one section, which is what stops Next moving under a thumb.
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "aa:0", kind: "auto_assembler", title: "Infinite Health", path: [],
      lines: Array.from({ length: 120 }, (_, index) => `line ${index}`),
      total_lines: 120, truncated: false, sanitized: false, readable: true,
    });

    const tall = window.innerHeight;
    try {
      Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
      render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Read" }));
      await screen.findByTestId("table-code-block");

      const short = screen.getAllByTestId("table-code-row").length;
      expect(short).toBeGreaterThanOrEqual(12);
      expect(short).toBeLessThan(26);
      const block = screen.getByTestId("table-code-block");
      expect(block.style.height).toBe(`${short * 15}px`);
      fireEvent.click(screen.getByRole("button", { name: "Next \u203a" }));
      expect(screen.getAllByTestId("table-code-row").length).toBe(short);

      // The screen this plugin was built on keeps exactly the page it had.
      cleanup();
      Object.defineProperty(window, "innerHeight", { value: 844, configurable: true });
      render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Read" }));
      await screen.findByTestId("table-code-block");
      expect(screen.getAllByTestId("table-code-row").length).toBe(26);
    } finally {
      Object.defineProperty(window, "innerHeight", { value: tall, configurable: true });
    }
  });

  it("still fits a handheld when the section carries both of its notes", async () => {
    // Both notes can be true of one section, and each costs a row of chrome
    // above the code. At 534 pixels that leaves room for eleven rows, and the
    // readability minimum used to round it back to twelve and draw the page 14
    // pixels past the display. What goes off a Steam Deck is the top, so what
    // was lost was the heading, the row naming the section and the first lines
    // of every page: the exact failure this screen's page sizing exists to fix,
    // reached through the one state the sizing did not account for.
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "aa:0", kind: "auto_assembler", title: "Infinite Health", path: [],
      lines: Array.from({ length: 120 }, (_, index) => `line ${index}`),
      total_lines: 400, truncated: true, sanitized: true, readable: true,
    });

    const tall = window.innerHeight;
    try {
      Object.defineProperty(window, "innerHeight", { value: 534, configurable: true });
      render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
      fireEvent.click(await screen.findByRole("button", { name: "Read" }));
      await screen.findByTestId("table-code-block");

      const rows = screen.getAllByTestId("table-code-row").length;
      // Its own layout model has to fit the display it measured: the chrome
      // this screen spends, both notes included, plus every row it drew.
      expect(320 + 52 + 52 + rows * 15).toBeLessThanOrEqual(534);
      // And it is still a page worth paging through. Seven on the narrowest
      // case this screen has: a handheld page carrying both of its notes.
      expect(rows).toBeGreaterThanOrEqual(7);
      expect(screen.getByRole("button", { name: "Next \u203a" })).toBeTruthy();
    } finally {
      Object.defineProperty(window, "innerHeight", { value: tall, configurable: true });
    }
  });

  it("opens a script with the ring on Next and hands it to the way out on the last page", async () => {
    // Reading one of these is a single press repeated, and the ring was on the
    // first row of the panel instead: every reader began by walking the stick
    // down the whole window to find Next. The last page then disabled Next
    // under the thumb that was on it, which drops the ring entirely, so the
    // window ended with nothing focused at all.
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "aa:0", kind: "auto_assembler", title: "Infinite Health", path: [],
      lines: Array.from({ length: 40 }, (_, index) => `line ${index}`),
      total_lines: 40, truncated: false, sanitized: false, readable: true,
    });

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Read" }));
    await screen.findByTestId("table-code-footer");

    const next = () => screen.getByRole("button", { name: "Next \u203a" }) as HTMLButtonElement;
    // The row rendering and the ring landing on it are separate commits, so
    // waiting for the row does not mean the ring has moved yet.
    await waitFor(() => expect(document.activeElement).toBe(next()));

    // Two pages of forty rows, so one press is the last press.
    fireEvent.click(next());
    expect(next().disabled).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back to the list" }));
  });

  it("keeps the ring on Previous for a reader turning back through a section", async () => {
    // Always putting it back on Next would take it off Previous the moment it
    // was used, which is the control a reader reaches for after deciding they
    // read a page too fast.
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "aa:0", kind: "auto_assembler", title: "Infinite Health", path: [],
      lines: Array.from({ length: 120 }, (_, index) => `line ${index}`),
      total_lines: 120, truncated: false, sanitized: false, readable: true,
    });

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Read" }));
    await screen.findByTestId("table-code-footer");

    fireEvent.click(screen.getByRole("button", { name: "Next \u203a" }));
    fireEvent.click(screen.getByRole("button", { name: "Next \u203a" }));
    fireEvent.click(screen.getByRole("button", { name: "\u2039 Previous" }));
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "\u2039 Previous" }));

    // And back onto Next when turning back has used Previous up, because the
    // first page disables it under the thumb exactly as the last disables Next.
    fireEvent.click(screen.getByRole("button", { name: "\u2039 Previous" }));
    expect((screen.getByRole("button", { name: "\u2039 Previous" }) as HTMLButtonElement).disabled).toBe(true);
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Next \u203a" }));
  });

  it("puts the ring on the way out of a section that has only one page", async () => {
    // There is no next page to ask for, so the only press left is the one that
    // goes back, and it is the one the ring belongs on.
    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Read" }));
    await screen.findByTestId("table-code-block");

    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "Back to the list" })),
    );
    // Checked after the ring settled, because an absence proves nothing until
    // something proves the answer arrived. The footer is there either way, so
    // the window does not change height between a section that pages and one
    // that does not; what says there is nothing to page is the pair of controls
    // being unpressable.
    const footer = screen.getByTestId("table-code-footer");
    expect((within(footer).getByRole("button", { name: "\u2039 Previous" }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(footer).getByRole("button", { name: "Next \u203a" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("returns the ring to the section that opened the preview", async () => {
    const second = { ...script, id: "aa:1", title: "Infinite Ammo" };
    api.listTableCode.mockResolvedValue(index([script, second], { auto_assembler: 2 }));
    api.readTableCode.mockImplementation(async (_sha: string, id: string) => ({
      schema: 1, id, kind: "auto_assembler", title: id === "aa:1" ? "Infinite Ammo" : "Infinite Health",
      path: ["Player"], lines: ["[ENABLE]"], total_lines: 1,
      truncated: false, sanitized: false, readable: true,
    }));

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    const buttons = await screen.findAllByRole("button", { name: "Read" });
    buttons[1].focus();
    fireEvent.click(buttons[1]);
    await screen.findByText("[ENABLE]");

    fireEvent.click(screen.getByRole("button", { name: "Back to the list" }));
    const restored = (await screen.findAllByRole("button", { name: "Read" }))[1];
    await waitFor(() => expect(document.activeElement).toBe(restored));
  });

  it("says when what is on screen is not what would run", async () => {
    // A script that can render as something other than what it is would be a
    // worse lie here than anywhere else on the panel.
    api.readTableCode.mockResolvedValue({
      schema: 1, id: "aa:0", kind: "auto_assembler", title: "Infinite Health", path: [],
      lines: ["print('safe')"], total_lines: 900, truncated: true, sanitized: true, readable: true,
    });

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Read" }));

    expect(await screen.findByText(/invisible or text-reordering character/)).toBeTruthy();
    expect(screen.getByText(/longer than the panel will show/)).toBeTruthy();
  });

  it("reports a table it could not read instead of showing an empty list", async () => {
    api.listTableCode.mockRejectedValue(new Error("table blob is missing or is not a regular file"));

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);

    expect(await screen.findByText("Could not read the table")).toBeTruthy();
    expect(screen.queryByText("Nothing to show")).toBeNull();
  });

  it("says plainly when a table carries nothing executable at all", async () => {
    api.listTableCode.mockResolvedValue(index([]));

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);

    expect(await screen.findByText("Nothing to show")).toBeTruthy();
  });
});

describe("filling the reading window exactly once", () => {
  const pageOf = (rows: ReturnType<typeof codeRows>, page: number) => rows.slice(page * 26, (page + 1) * 26);

  it("pages by the rows a line takes rather than by the lines themselves", () => {
    // A page of eighty lines was taller than the modal, and a controller cannot
    // scroll a block of text: Steam moves the panel by moving focus, and there
    // is nothing to focus inside the code, so focus jumped from the row above
    // it straight to Next and everything between was unreachable.
    const short = Array.from({ length: 100 }, (_, index) => `mov eax,${index}`);
    const rows = codeRows(short);
    expect(rows).toHaveLength(100);
    expect(pageOf(rows, 1)[0]).toEqual({ text: "mov eax,26", line: 27 });

    // One long line takes several rows, which is what made a fixed count of
    // lines overflow anyway: a page holds fewer of those lines.
    const wrapped = codeRows(Array.from({ length: 100 }, () => "x".repeat(200)));
    expect(wrapped.length).toBeGreaterThan(rows.length);
    expect(pageOf(wrapped, 0).filter((row) => row.line === 1)).toHaveLength(3);
  });

  it("keeps the tail of a line the backend allows reachable rather than clipping it", () => {
    // The backend allows one line of 2048 bytes and the window is 26 rows of
    // 68 columns, so a legal line does not fit on one page. It used to get a
    // page of its own and everything past the 26th row was cut off by the
    // block, with no later page holding it and nothing on screen saying so, on
    // the one screen that exists to show exactly what would be run.
    const line = `${"a".repeat(2040)}SENTINEL`;
    const rows = codeRows([line]);
    expect(rows.map((row) => row.text).join("")).toBe(line);
    expect(rows.every((row) => row.line === 1)).toBe(true);
    expect(rows.length).toBeGreaterThan(26);

    const pages = Math.ceil(rows.length / 26);
    const shown = Array.from({ length: pages }, (_, page) => pageOf(rows, page).map((row) => row.text).join(""));
    expect(shown.join("")).toContain("SENTINEL");
  });

  it("counts a wide character at the width it is actually drawn", () => {
    // Half as many of these fit on a row, and counting them as one character
    // each put a third of such a line below the bottom of the block.
    const wide = codeRows(["\u4e2d".repeat(68)]);
    expect(wide).toHaveLength(2);
    expect(wide[0].text).toHaveLength(34);
    expect(wide.map((row) => row.text).join("")).toBe("\u4e2d".repeat(68));

    // A tab advances to the next eight-column stop and is counted at that.
    expect(codeRows(["\t".repeat(9)]).length).toBeGreaterThan(1);
  });

  it("assumes a code point nobody listed is wide rather than narrow", () => {
    // A list of the ranges known to be wide leaves every family nobody thought
    // of counted as one column: emoji and the symbol blocks were, so a page of
    // them held more drawn rows than the count believed and the bottom of it
    // went back under the block's own overflow.
    for (const character of ["\u{1f600}", "\u2588", "\u2192", "\u{1f1fa}", "\u3000"]) {
      expect(codeRows([character.repeat(34)])).toHaveLength(1);
      expect(codeRows([character.repeat(35)])).toHaveLength(2);
    }

    // Latin, Greek and Cyrillic stay one column, so an ordinary comment does
    // not cost twice the pages it should.
    for (const character of ["a", "\u00e9", "\u03b1", "\u0434"]) {
      expect(codeRows([character.repeat(68)])).toHaveLength(1);
      expect(codeRows([character.repeat(69)])).toHaveLength(2);
    }
  });

  it("gives a line taller than the whole window as many pages as it needs", () => {
    const monster = ["short", "y".repeat(20_000), "short"];
    const rows = codeRows(monster);
    expect(rows.filter((row) => row.line === 2).map((row) => row.text).join("")).toBe("y".repeat(20_000));
    expect(rows[rows.length - 1]).toEqual({ text: "short", line: 3 });
  });

  it("gives an empty section one page rather than none", () => {
    expect(codeRows([])).toEqual([]);
    expect(Math.max(1, Math.ceil(codeRows([]).length / 26))).toBe(1);
  });

  it("keeps an empty line in the file as a row of its own", () => {
    expect(codeRows(["[ENABLE]", "", "code"])).toEqual([
      { text: "[ENABLE]", line: 1 },
      { text: "", line: 2 },
      { text: "code", line: 3 },
    ]);
  });
});

describe("the section list, as a page", () => {
  it("lands the ring on Next when the reader comes down into the pager", async () => {
    // Previous is the first control in that row and is disabled on page one,
    // so the reader arriving from the sections landed on a button that does
    // nothing. Both halves say it: the group asks Steam to enter at its
    // preferred child, and Next says it is that child. This screen still opens
    // on its first section, because it moves the ring there itself once the
    // index arrives.
    api.listTableCode.mockResolvedValue(index(
      Array.from({ length: 20 }, (_, position) => ({ ...script, id: `aa:${position}`, title: `Cheat ${position}` })),
      { auto_assembler: 20 },
    ));

    render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
    const footer = await screen.findByTestId("table-code-footer");

    expect(footer.querySelector("[data-nav-entry]")?.getAttribute("data-nav-entry")).toBe("4");
    expect(screen.getByRole("button", { name: "Next \u203a" }).getAttribute("data-preferred-focus")).toBe("true");
  });

  it("holds the list at the height a full page takes, so the window does not move under the thumb", async () => {
    // Reading a table is one press repeated, and the press is in the footer. A
    // window that shortens on the last page moves it out from under the thumb.
    api.listTableCode.mockResolvedValue(index(
      Array.from({ length: 20 }, (_, position) => ({ ...script, id: `aa:${position}`, title: `Cheat ${position}` })),
      { auto_assembler: 20 },
    ));
    const rect = (height: number) => ({
      top: 0, height, bottom: height, left: 0, right: 0, width: 0, x: 0, y: 0, toJSON: () => ({}),
    } as DOMRect);
    const measured = vi.spyOn(Element.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: Element) {
        return rect((this as HTMLElement).dataset?.testid === "table-code-list" ? 348 : 58);
      });
    try {
      render(<TableCodeModal sha256={SHA} filename="Game.CT" onBack={vi.fn()} />);
      await screen.findByTestId("table-code-footer");
      await waitFor(() => expect(screen.getByTestId("table-code-list").style.minHeight).toBe("348px"));
    } finally {
      measured.mockRestore();
    }
  });
});
