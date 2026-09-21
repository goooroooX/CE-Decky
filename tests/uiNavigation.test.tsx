import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ButtonItem: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  DialogButton: ({ children, onClick, disabled, preferredFocus, style }: any) => <button disabled={disabled} data-preferred-focus={preferredFocus} data-flex={style?.flex} onClick={onClick}>{children}</button>,
  Field: ({ label, description, children, padding, bottomSeparator }: any) => <div data-padding={padding ?? "standard"} data-separator={bottomSeparator ?? "standard"}><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: ({ children, onActivate, navEntryPreferPosition, ...props }: any) => <div data-testid="focusable" data-flow-children={props["flow-children"]} data-nav-entry={navEntryPreferPosition} onClick={() => onActivate?.(new CustomEvent("activate"))}>{children}</div>,
  Toggle: ({ value, onChange, disabled }: any) => <input data-testid="toggle" type="checkbox" checked={value} disabled={disabled} onChange={(event) => onChange?.(event.target.checked)} />,
  PanelSection: ({ title, children }: any) => <section aria-label={title || "section"}>{children}</section>,
  PanelSectionRow: ({ children }: any) => <div data-panel-section-row="true">{children}</div>,
  Spinner: () => <span role="progressbar">Loading</span>,
  gamepadDialogClasses: { Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription", FieldLeftColumn: "FieldLeftColumn", CompactPadding: "CompactPadding", WithBottomSeparatorStandard: "WithBottomSeparatorStandard", WithBottomSeparatorThick: "WithBottomSeparatorThick" },
  quickAccessControlsClasses: { PanelSection: "PanelSection" },
  ToggleField: ({ label, description, checked, onChange, disabled }: any) => (
    <label>{label}<input aria-label={label} type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange?.(event.target.checked)} />{description}</label>
  ),
}));

import { HomePanel } from "../src/components/HomePanel";
import { ActionGroup, PanelRow, SmallButton } from "../src/components/PanelDensity";
import { CheatRow } from "../src/components/CheatRow";

const game = { appId: 10, name: "Game", sortAs: "Game", isShortcut: false };
const details = {
  appId: 10, displayName: "Game", shortcutExe: "",
  isShortcut: false, compatToolName: "proton", compatToolDisplayName: "Proton", compatToolPriority: 1, platforms: ["windows"],
};
const table = {
  sha256: "1".repeat(64), filename: "Game.CT", size: 100, table_version: "45", has_lua: false,
  has_auto_assembler: false, has_embedded_files: false, executable_content: false, entry_count: 20,
  blob_path: "/managed/Game.CT", available: true, schema_version: 2, origins: [],
};

function props(overrides: Record<string, unknown> = {}) {
  return {
    pluginVersion: "v0.5.0", ceReady: true, ceStatusText: "Cheat Engine 7.7 · Ready",
    installAvailable: true, installBusy: false, setupPending: false, installOperation: null,
    ceSource: "Managed" as const, ceSha256: "a".repeat(64), onInstall: vi.fn(), onCancelInstall: vi.fn(),
    reinstallLabel: "Reinstall CE", onReinstall: vi.fn(), game, appDetails: details, runningDetectionAvailable: true,
    runningGameCount: 1, targetProcess: "game.exe", onChooseGame: vi.fn(), table, tableSource: "OpenCT",
    onSearchTable: vi.fn(), onOpenLocalTable: vi.fn(), runtimeReady: true, runtimeText: "Connected · game.exe", runtimeTextComplete: false,
    activeCheatLabels: ["Health", "Ammo"], activeCheatSnapshotReady: true, pinnedCount: 0, pinnedRows: [],
    pinnedBusyRecordId: null, onTogglePinnedCheat: vi.fn(), startRuntimeAvailable: false, startRuntimeBlockedReason: null,
    onStartRuntime: vi.fn(), onChooseCheats: vi.fn(), onDisableAllCheats: vi.fn(), autoloadEnabled: false, autoloadBlockedReason: null, onAutoloadChange: vi.fn(),
    ceRunning: true, onStopCE: vi.fn(), onAdvanced: vi.fn(), busy: false, error: null,
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("compact QAM home navigation", () => {
  it("keeps the normal workflow in the intended top-to-bottom order", () => {
    render(<HomePanel {...props()} />);
    // The headings are CE Decky's own elements: Steam's section title cannot
    // carry the rule that separates one section from the next.
    const sections = screen.getAllByRole("region").map((node) => node.textContent?.slice(0, 20));
    expect(sections.length).toBe(3);
    expect(screen.getByText("Setup")).toBeTruthy();
    expect(screen.getByText("Cheats")).toBeTruthy();
    expect(screen.getByText("Auto-load")).toBeTruthy();
    // Setup is three status rows carrying their own small actions.
    expect(screen.getByTestId("ce-row")).toBeTruthy();
    expect(screen.getByTestId("game-row")).toBeTruthy();
    expect(screen.getByTestId("table-row")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Search" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Manage" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Configure cheats" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Advanced…" })).toBeTruthy();
  });

  it("refuses a game change while the game this profile is on is running", () => {
    // Changing the game underneath a running one is a press away from every
    // question this plugin answers per game: which table is prepared, which
    // process is the target, which session an owned Cheat Engine holds.
    render(<HomePanel {...props({ selectedGameRunning: true })} />);
    const row = screen.getByTestId("game-row");
    expect((within(row).getByRole("button", { name: "Change" }) as HTMLButtonElement).disabled).toBe(true);
    // The game row goes on saying what game this is and nothing else. A line
    // explaining a press nobody made costs a row of a 300 pixel column every
    // session, and written into the game row it took the place of the game's
    // own identity; `README.md` carries the explanation instead.
    expect(row.textContent).toContain("Steam \u00b7 game.exe");
    expect(row.textContent).not.toContain("Stop the game");
    // The section keeps the three rows it had; the order they are in is held by
    // the top-to-bottom case above.
    expect(screen.getByTestId("ce-row")).toBeTruthy();
    expect(screen.getByTestId("table-row")).toBeTruthy();

    // Not running: the ordinary state, no sentence about it and no refusal.
    cleanup();
    render(<HomePanel {...props({ selectedGameRunning: false })} />);
    const idle = screen.getByTestId("game-row");
    expect((within(idle).getByRole("button", { name: "Change" }) as HTMLButtonElement).disabled).toBe(false);
    expect(idle.textContent).toContain("Steam \u00b7 game.exe");

    // And the press that resolves "which of these is running" is never the one
    // refused: with no game chosen there is nothing to change away from.
    cleanup();
    render(<HomePanel {...props({ game: null, selectedGameRunning: true, runningGameCount: 2 })} />);
    expect((screen.getByRole("button", { name: "Choose" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("offers Disable all beside Stop CE, and only while the runtime is connected", () => {
    const onDisableAllCheats = vi.fn();
    render(<HomePanel {...props({ runtimeReady: true, ceRunning: true, onDisableAllCheats })} />);
    const actions = screen.getByTestId("panel-actions");
    expect(within(actions).getAllByRole("button").map((node) => node.textContent))
      .toEqual(["Advanced…", "Disable all", "Stop CE"]);
    fireEvent.click(within(actions).getByRole("button", { name: "Disable all" }));
    expect(onDisableAllCheats).toHaveBeenCalledTimes(1);

    cleanup();
    render(<HomePanel {...props({ runtimeReady: false })} />);
    expect((screen.getByRole("button", { name: "Disable all" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps Disable all and Advanced on one left/right controller path", () => {
    render(<HomePanel {...props({ runtimeReady: true, activeCheatSnapshotReady: true, ceRunning: false })} />);

    const actions = screen.getByTestId("panel-actions");
    const disable = within(actions).getByRole("button", { name: "Disable all" });
    const advanced = within(actions).getByRole("button", { name: "Advanced…" });
    const disableGroup = disable.closest('[data-flow-children="row"]');
    expect(disableGroup).toBeTruthy();
    expect(advanced.closest('[data-flow-children="row"]')).toBe(disableGroup);
    expect(actions.closest('[data-panel-section-row="true"]')).toBeNull();
    expect(disableGroup?.getAttribute("data-nav-entry")).toBe("4");
    expect(advanced.getAttribute("data-preferred-focus")).toBe("true");
    expect(document.activeElement).not.toBe(advanced);

    // Steam enters a row at the control that covers most of the width of the
    // one above it, so leading the row is not on its own what puts the ring on
    // Advanced: it also has to be the control that takes the row's free width,
    // and the actions beside it have to keep their own.
    expect(within(actions).getAllByRole("button")[0]).toBe(advanced);
    expect(advanced.getAttribute("data-flex")).toBe("1 1 auto");
    expect(disable.getAttribute("data-flex")).toBe("0 0 auto");
  });

  it("does not stop the ring on a status row that says something short and complete", () => {
    // Paging down a panel that is mostly controls, the ring rested on lines of
    // state with nothing on them to show that it had: no control, and nothing
    // a press would reveal. A read-only row is otherwise a stop of its own, and
    // has to stay one where the text can be too long for its line.
    render(<HomePanel {...props({
      runtimeReady: false,
      runtimeText: "Table selected; Cheat Engine is not connected.",
      runtimeTextComplete: true,
      activeCheatSnapshotReady: false,
      ceRunning: false,
      pinnedCount: 2,
      pinnedRows: [],
      startRuntimeAvailable: false,
      startRuntimeBlockedReason: "Start the game first.",
    })} />);

    expect(screen.getByTestId("runtime-row").querySelector('[data-testid="focusable"]')).toBeNull();
    for (const label of ["Pinned controls", "Cannot start yet"]) {
      expect(screen.getByText(label).closest('[data-testid="focusable"]')).toBeNull();
    }

    // The same row keeps its stop while it carries the one thing worth reading
    // there, which is also the text that may not fit the line.
    cleanup();
    render(<HomePanel {...props({
      runtimeReady: true,
      activeCheatSnapshotReady: false,
      liveSnapshotError: "the bridge did not answer",
    })} />);
    expect(screen.getByTestId("runtime-row").querySelector('[data-testid="focusable"]')).toBeTruthy();
  });

  it("never both cuts a row's text and says it is read where it stands", () => {
    // The two are the same claim from opposite ends. Cutting a line is what
    // makes a row worth reaching, because the reader cannot finish it where it
    // is; saying a row is read in place is saying there is nothing to reach.
    // Home's runtime line had both, so the ring rested on it and the end of its
    // sentence was hidden behind a press: "Cheat Engine is not running for this
    // table. Details …", cut in a 300 pixel panel on every display, not only on
    // the handheld. The row wraps now and costs the line it needed.
    render(<HomePanel {...props({
      runtimeReady: false,
      runtimeText: "Cheat Engine is not running for this table. Details are under Advanced.",
      runtimeTextComplete: true,
      activeCheatSnapshotReady: false,
      pinnedCount: 2,
      pinnedRows: [],
    })} />);

    for (const testId of ["runtime-row"]) {
      const row = screen.getByTestId(testId);
      expect(row.querySelector('[data-testid="focusable"]')).toBeNull();
      expect(row.querySelector(".ce-decky-ellipsis")).toBeNull();
      expect(row.querySelectorAll(".ce-decky-wrap").length).toBeGreaterThan(0);
    }
    // The whole sentence is on the row, with no press needed to finish it.
    expect(screen.getByText("Cheat Engine is not running for this table. Details are under Advanced.")).toBeTruthy();
    expect(screen.getByText("2 pinned; connect Cheat Engine to use them here.").className).toBe("ce-decky-wrap");
  });

  it("keeps the runtime row reachable while it carries a failure or a recovery", () => {
    // The line that says why a session is not usable is the longest text on
    // this panel and the one a user has to read in full: the backend writes
    // part of it. The row truncates, so without a stop of its own there is no
    // controller press anywhere that can finish it.
    const loadFailed = "the address list is empty and Cheat Engine reported nothing it could open. "
      + "Stop Cheat Engine and start it again; if that repeats, this table cannot be used with this Cheat Engine.";
    render(<HomePanel {...props({
      runtimeReady: false,
      tableLoadFailed: true,
      runtimeText: loadFailed,
      runtimeTextComplete: false,
      activeCheatSnapshotReady: false,
    })} />);

    const row = screen.getByTestId("runtime-row");
    const stop = row.querySelector('[data-testid="focusable"]');
    expect(stop).toBeTruthy();
    // Cut to the line until it is opened, and opening it is the press the stop
    // exists for.
    const texts = (selector: string) => [...row.querySelectorAll(selector)].map((node) => node.textContent);
    expect(texts(".ce-decky-ellipsis")).toEqual(["Table not loaded", loadFailed]);
    fireEvent.click(stop as Element);
    expect(texts(".ce-decky-wrap")).toEqual(["Table not loaded", loadFailed]);
    expect(row.querySelector(".ce-decky-ellipsis")).toBeNull();

    cleanup();
    render(<HomePanel {...props({
      runtimeReady: false,
      runtimeText: "Cheat Engine is running an earlier resident bridge. Stop it and start it again to use live cheats.",
      runtimeTextComplete: false,
      activeCheatSnapshotReady: false,
    })} />);
    expect(screen.getByTestId("runtime-row").querySelector('[data-testid="focusable"]')).toBeTruthy();
  });

  it("keeps a pinned cheat row a stop once it has a control to press", () => {
    // The note that stands in for these rows while nothing is connected says
    // something short and complete and is read where it is. The rows themselves
    // carry the one control on this panel that switches a cheat, so the ring
    // has to reach them, and the note must not be what teaches the row's rule.
    render(<HomePanel {...props({
      runtimeReady: true,
      activeCheatSnapshotReady: true,
      pinnedCount: 1,
      pinnedRows: [{ recordId: 7, label: "Health", summary: "Health points", active: true }],
    })} />);

    const row = screen.getByTestId("pinned-cheat-7");
    expect(within(row).getByTestId("toggle")).toBeTruthy();
    expect((within(row).getByTestId("toggle") as HTMLInputElement).disabled).toBe(false);
    expect(screen.queryByText("Pinned controls")).toBeNull();
  });

  it("tags the registered Cheat Engine as managed or imported with its compact SHA", () => {
    render(<HomePanel {...props()} />);
    expect(screen.getByText(`Managed · ${"a".repeat(8)} · v0.5.0`)).toBeTruthy();

    cleanup();
    render(<HomePanel {...props({ ceSource: "Imported", ceStatusText: "Cheat Engine · Ready" })} />);
    expect(screen.getByText(`Imported · ${"a".repeat(8)} · v0.5.0`)).toBeTruthy();
  });

  it("keeps session/descriptor terminology off the normal home panel", () => {
    render(<HomePanel {...props({ error: "Something went wrong." })} />);
    const sections = screen.getAllByRole("region").map((node) => node.getAttribute("aria-label"));
    expect(sections).not.toContain("Session");
    expect(screen.getByTestId("panel-error").textContent).toContain("Something went wrong.");
  });

  it("promotes pinned controls onto the panel as live toggles", () => {
    const onTogglePinnedCheat = vi.fn();
    render(<HomePanel {...props()} />);
    expect(screen.queryByText("Pinned controls")).toBeNull();

    cleanup();
    render(<HomePanel {...props({
      pinnedCount: 2,
      pinnedRows: [
        { recordId: 7, label: "Health", summary: "Enable 1.0 · = 100", active: true },
        { recordId: 9, label: "Ammo", summary: "Enable 1.0", active: false },
      ],
      onTogglePinnedCheat,
    })} />);
    const row = screen.getByTestId("pinned-cheat-7");
    expect(row.textContent).toContain("Health");
    expect(row.textContent).toContain("= 100");
    fireEvent.click(within(screen.getByTestId("pinned-cheat-9")).getByTestId("toggle"));
    expect(onTogglePinnedCheat).toHaveBeenCalledWith(9, true);
  });

  it("keeps what a row reveals inside that row, text input included", () => {
    // Every Steam `Field` carries `padding: 0 20px`; its text input carries
    // none, so the value editor ran the full width of the block while the
    // labels around it stayed inset, and the whole detail block sat flush with
    // the row's own heading instead of under it.
    const { container } = render(<HomePanel {...props()} />);
    const css = container.querySelector(".ce-decky-dense style")?.textContent ?? "";
    expect(css).toContain(".ce-decky-reveal { padding: 0 0 3px 12px; }");
    // Steam's fields are opaque and their rectangles do not line up with the
    // row's block, so the revealed rows are flattened onto it rather than boxed
    // - a box left stray strips of block tint down three sides. The focused
    // field keeps Steam's highlight, which is the controller's position.
    expect(css).toContain(":not(.gpfocus):not(.gpfocuswithin) { background: transparent; }");
    // An open row's heading is lifted a step above its own revealed rows, so it
    // reads as one card whether or not the controller is sitting on it. Steam's
    // focus colour still wins, which is why both rules exclude it.
    expect(css).toContain(".ce-decky-open.ce-decky-open.ce-decky-open >");
    expect(css).toContain(":not(.gpfocus):not(.gpfocuswithin) { background: hsla(0, 0%, 100%, 0.06); }");
    // The input group is given the same inset the fields beside it already have,
    // and loses the 22px bottom margin that left a hole between two rows.
    expect(css).toContain(".ce-decky-reveal .DialogInputLabelGroup { padding: 2px 20px 5px; margin-bottom: 0; }");
    expect(css).toContain(".ce-decky-reveal .DialogInput { height: 32px;");
    // One step down the type scale from the row's own 14px/11px heading.
    expect(css).toContain(".ce-decky-reveal .FieldLabel { font-size: 13px; line-height: 16px; }");
    expect(css).toContain(".ce-decky-reveal .DialogLabel { font-size: 11px; line-height: 14px;");
    // An aside about what Apply will do is inset like the fields around it but
    // costs no label and no control-sized row.
    expect(css).toContain(".ce-decky-note { padding: 2px 20px 5px; font-size: 11px;");
  });

  it("says one thing about a launch it owns, and says it in one place", () => {
    // The poll walks a session through three states while it starts, and the
    // runtime line reported each of them: prepared, then running but not
    // attached, then not running for this table again. Three descriptions of
    // the same not-yet, cycling under a reader trying to finish one of them,
    // while the row below said the launch was the reason it could not start.
    render(<HomePanel {...props({
      launchPending: true,
      runtimeReady: false,
      activeCheatSnapshotReady: false,
      runtimeText: "Cheat Engine is not running for this table. Details are under Advanced.",
      runtimeTextComplete: true,
      startRuntimeAvailable: false,
      startRuntimeBlockedReason: "Cheat Engine is already running for this game; stop it before starting a new session.",
    })} />);

    const row = screen.getByTestId("runtime-row");
    expect(row.textContent).toContain("Starting Cheat Engine");
    expect(row.textContent).toContain("Cancel CE launch below stops it.");
    // Nothing of the poll's own narration is left on it, and the ring does not
    // rest on a line whose whole content is a wait.
    expect(row.textContent).not.toContain("Details are under Advanced");
    expect(row.querySelector('[data-testid="focusable"]')).toBeNull();
    // And the row that lists the conditions to fix says nothing: a launch in
    // progress is not one of them.
    expect(screen.queryByText("Cannot start yet")).toBeNull();
    expect(screen.getByRole("button", { name: "Cancel CE launch" })).toBeTruthy();
  });

  it("scrolls the game's name and the table's under the ring on the panel's own rows", () => {
    // Both are routinely longer than a 300 pixel column that also carries two
    // presses, and the compatibility mark before the table's name takes more of
    // it again: the end of each is what tells one from the next.
    render(<HomePanel {...props()} />);
    for (const testId of ["game-row", "table-row"]) {
      const row = screen.getByTestId(testId);
      expect(row.classList.contains("ce-decky-focusscroll")).toBe(true);
      expect(row.querySelectorAll(".ce-decky-marquee > span").length).toBe(2);
    }
  });

  it("keeps a pinned cheat to two lines and scrolls the text of the focused one", () => {
    // A table names its own records, and "Reduction % (100 = immune, 0 = no
    // reduction)" is a real one: wrapped, a single cheat cost four lines of the
    // one column every other cheat also has to fit into.
    const { container } = render(<HomePanel {...props({
      pinnedCount: 1,
      pinnedRows: [{
        recordId: 7,
        label: "Reduction % (100 = immune, 0 = no reduction)",
        summary: "Enable 1.0 \u203a Party Damage Reduction \u00b7 = 95",
        active: true,
      }],
    })} />);
    const row = screen.getByTestId("pinned-cheat-7");
    // The row itself is what focus is judged against, so the rule can move only
    // the text of the cheat the user is actually on.
    expect(row.classList.contains("ce-decky-focusscroll")).toBe(true);
    const tracks = row.querySelectorAll(".ce-decky-marquee > span");
    expect(tracks.length).toBe(2);
    expect(tracks[0].textContent).toBe("Reduction % (100 = immune, 0 = no reduction)");
    expect(tracks[1].textContent).toContain("= 95");

    const css = container.querySelector(".ce-decky-dense style")?.textContent ?? "";
    // The shift used to be `left: 100%` minus `translateX(-100%)`, which is the
    // overflow when there is one and zero when there is not, and needs no
    // measurement - but it is also two percentages resolved against two boxes,
    // so the endpoint moves whenever either width does, including when this
    // rule takes the clamp off at the moment the animation starts. On the
    // device both lines of a focused pinned cheat twitched, on rows whose text
    // fits. Every distance below is a fixed length now.
    expect(css).toContain(".ce-decky-marquee > span { display: inline-block; position: relative; left: 0;");
    expect(css).toContain("max-width: 100%; overflow: hidden; text-overflow: ellipsis;");
    expect(css).not.toContain("translateX(-100%)");
    expect(css).not.toContain("left: 100%");
    // The line that is cut scrolls over the overflow this component measured,
    // and only that line carries an animation: a line the reader can already
    // finish has none at all rather than one that resolves to no distance.
    expect(css).toContain(".ce-decky-focusscroll:focus-within .ce-decky-marquee[data-clipped] > span");
    // Steam moves real DOM focus and also marks the ancestor itself.
    expect(css).toContain(".ce-decky-focusscroll.gpfocuswithin .ce-decky-marquee[data-clipped] > span");
    // And marks a control inside the row rather than the row, which is what a
    // list whose only focus targets are its buttons gives it. Its own rule: a
    // parser that does not know `:has()` drops the rule it is written in, and
    // this one is the one that can be lost without costing the pair above.
    expect(css).toContain(".ce-decky-focusscroll:has(.gpfocuswithin) .ce-decky-marquee[data-clipped] > span");
    // The duration is the line's own where the line published one, and the flat
    // seven seconds where it did not: the panel's two-line cheats are unpaced.
    expect(css).toContain("animation: ce-decky-marquee-shift var(--ce-marquee-seconds, 7s) ease-in-out 0.7s infinite alternate");
    expect(css).toContain("@keyframes ce-decky-marquee-shift { from { transform: translateX(0); }"
      + " to { transform: translateX(var(--ce-marquee-shift, 0px)); } }");
    expect(css).not.toContain("marquee-shift-lead");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    // Reduced motion asks for no movement, not for less to read. Taking the
    // animation away on its own left the line clipped and one line tall, and
    // these rows carry controls, so `PanelRow` gives them no press-to-wrap
    // focusable either: a filename past the cut, and the recorded reason a
    // table did not work, had no route to the rest of themselves at all. The
    // focused line that is cut wraps instead, which is the same reveal in the
    // one form that is not motion.
    for (const focus of [
      ":focus-within",
      ".gpfocuswithin",
      ":has(.gpfocuswithin)",
    ]) {
      const reveal = css
        .split("@media (prefers-reduced-motion: reduce)")
        .filter((block) => block.includes(`.ce-decky-focusscroll${focus} .ce-decky-marquee[data-clipped]`));
      expect(reveal.length, focus).toBeGreaterThan(0);
      const body = reveal.join(" ");
      expect(body, focus).toContain("white-space: normal");
      expect(body, focus).toContain("max-width: none");
      expect(body, focus).toContain("text-overflow: clip");
    }
  });

  it("marks only the line that is actually cut, with the distance it is cut by", () => {
    // Which of the two things a focused line does is a fact about the line, not
    // an arrangement in the stylesheet: it can only be known by measuring, and
    // the measurement is what the reveal then travels over. jsdom lays nothing
    // out, so the two widths are what is stubbed here.
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

    let restore = stubWidths(260, 200);
    try {
      render(<HomePanel {...props({
        pinnedCount: 1,
        pinnedRows: [{ recordId: 7, label: "Reduction % (100 = immune, 0 = no reduction)", summary: "Enable 1.0", active: true }],
      })} />);
      const cut = screen.getByTestId("pinned-cheat-7").querySelectorAll(".ce-decky-marquee[data-clipped]");
      expect(cut.length).toBe(2);
      expect((cut[0].firstElementChild as HTMLElement).style.getPropertyValue("--ce-marquee-shift")).toBe("-60px");
      // Unpaced, so it keeps the flat duration the stylesheet falls back to.
      expect((cut[0].firstElementChild as HTMLElement).style.getPropertyValue("--ce-marquee-seconds")).toBe("");
    } finally {
      restore();
    }

    // A line the reader can already finish is not marked, so no rule reaches it
    // and it stays where it is.
    cleanup();
    restore = stubWidths(200, 200);
    try {
      render(<HomePanel {...props({
        pinnedCount: 1,
        pinnedRows: [{ recordId: 7, label: "Health", summary: "Enable 1.0", active: true }],
      })} />);
      const row = screen.getByTestId("pinned-cheat-7");
      expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBe(0);
      expect(row.querySelectorAll(".ce-decky-marquee").length).toBe(2);
    } finally {
      restore();
    }
  });

  it("keeps the mark on a focused line that reduced motion has wrapped", () => {
    // The reveal under reduced motion is a wrapped line, and a wrapped line
    // measures as having nothing to reveal. Answering that withdraws the very
    // mark the reveal rule matches on, which unwraps the line, which makes it
    // overflow again: a flip-flop that re-renders the row for as long as it
    // holds focus. The answer from before the reveal is the true one.
    const widths = { scroll: 260, client: 200 };
    const target = HTMLElement.prototype;
    const prior = {
      scrollWidth: Object.getOwnPropertyDescriptor(target, "scrollWidth"),
      clientWidth: Object.getOwnPropertyDescriptor(target, "clientWidth"),
    };
    Object.defineProperty(target, "scrollWidth", { configurable: true, get: () => widths.scroll });
    Object.defineProperty(target, "clientWidth", { configurable: true, get: () => widths.client });
    const priorMatchMedia = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      onchange: null,
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;
    try {
      render(<HomePanel {...props({
        pinnedCount: 1,
        pinnedRows: [{ recordId: 7, label: "Reduction % (100 = immune, 0 = no reduction)", summary: "Enable 1.0", active: true }],
      })} />);
      const row = screen.getByTestId("pinned-cheat-7");
      expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBe(2);

      // Steam marks the row, and the line wraps: now it fits its own box.
      row.classList.add("gpfocuswithin");
      widths.scroll = 200;
      act(() => { fireEvent.focusIn(row); });

      expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBe(2);

      // Focus leaves, the line is clamped again, and the answer is taken anew.
      row.classList.remove("gpfocuswithin");
      act(() => { fireEvent.focusOut(row); });
      expect(row.querySelectorAll(".ce-decky-marquee[data-clipped]").length).toBe(0);
    } finally {
      window.matchMedia = priorMatchMedia;
      for (const [name, descriptor] of Object.entries(prior)) {
        if (descriptor) Object.defineProperty(target, name, descriptor);
        else delete (target as unknown as Record<string, unknown>)[name];
      }
    }
  });

  it("narrows the switch on the panel without letting Steam clip its knob", () => {
    render(<HomePanel {...props({
      pinnedCount: 1,
      pinnedRows: [{ recordId: 7, label: "Health", summary: "Enable 1.0", active: true }],
    })} />);
    const toggle = within(screen.getByTestId("pinned-cheat-7")).getByTestId("toggle");
    // Steam's knob is absolutely positioned inside a fixed 38px box, so the box
    // keeps its own width and the whole thing is scaled from the edge the row
    // aligns on instead.
    const scaled = toggle.parentElement as HTMLElement;
    expect(scaled.style.transform).toBe("scale(0.84)");
    expect(scaled.style.transformOrigin).toBe("100% 50%");
    expect(scaled.style.flex).toBe("0 0 38px");
    expect((scaled.parentElement as HTMLElement).style.width).toBe("32px");
  });

  it("renders the live-state line as the heading of the cheats under it", () => {
    render(<HomePanel {...props({
      pinnedCount: 1,
      pinnedRows: [{ recordId: 7, label: "Health", summary: "Enable 1.0", active: true }],
    })} />);
    const runtime = screen.getByTestId("runtime-row");
    expect(runtime.classList.contains("ce-decky-rowhead")).toBe(true);
    // A tinted block that also carries a separator reads as one more row in the
    // list rather than as the thing introducing it.
    expect(runtime.querySelector("[data-separator]")?.getAttribute("data-separator")).toBe("none");
    expect(screen.getByTestId("pinned-cheat-7").classList.contains("ce-decky-rowhead")).toBe(false);
  });

  it("explains a pin that cannot be controlled until Cheat Engine is connected", () => {
    render(<HomePanel {...props({ pinnedCount: 3, pinnedRows: [], runtimeReady: false })} />);
    expect(screen.getByText("3 pinned; connect Cheat Engine to use them here.")).toBeTruthy();
  });

  it("bounds the active-cheat summary instead of creating an unbounded home focus list", () => {
    const labels = Array.from({ length: 12 }, (_, index) => `Cheat ${index + 1}`);
    render(<HomePanel {...props({ activeCheatLabels: labels })} />);
    expect(screen.getByText("Cheat 4")).toBeTruthy();
    expect(screen.queryByText("Cheat 5")).toBeNull();
    expect(screen.getByText("+8 more")).toBeTruthy();
  });

  it("offers the second-run runtime entry point only while a table is selected and idle", () => {
    const onStartRuntime = vi.fn();
    render(<HomePanel {...props()} />);
    expect(screen.queryByRole("button", { name: "Load table & start CE" })).toBeNull();

    cleanup();
    render(<HomePanel {...props({ runtimeReady: false, startRuntimeAvailable: true, onStartRuntime })} />);
    fireEvent.click(screen.getByRole("button", { name: "Load table & start CE" }));
    expect(onStartRuntime).toHaveBeenCalledTimes(1);

    cleanup();
    render(<HomePanel {...props({ runtimeReady: false, startRuntimeAvailable: false, startRuntimeBlockedReason: "Start the game first." })} />);
    expect((screen.getByRole("button", { name: "Load table & start CE" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText("Start the game first.")).toBeTruthy();
  });

  it("exposes autoload as one explicit per-game toggle, blocked with its reason", () => {
    const onAutoloadChange = vi.fn();
    render(<HomePanel {...props({ onAutoloadChange })} />);
    fireEvent.click(screen.getByLabelText("Load last table & cheats"));
    expect(onAutoloadChange).toHaveBeenCalledWith(true);

    cleanup();
    render(<HomePanel {...props({ autoloadBlockedReason: "Start the game first." })} />);
    expect((screen.getByLabelText("Load last table & cheats") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByLabelText("Load last table & cheats").parentElement?.textContent).toContain("Start the game first.");
  });

  it("keeps compact reinstall progress and cancellation reachable while the previous CE remains valid", () => {
    const onCancelInstall = vi.fn();
    const onReinstall = vi.fn();
    render(<HomePanel {...props({
      installBusy: true,
      setupPending: true,
      installOperation: {
        operation_id: "op",
        state: "extracting",
        progress: null,
        message: "Installing replacement",
        error: null,
        installed: null,
      },
      onCancelInstall,
      onReinstall,
      reinstallLabel: "Reinstall CE",
      ceRunning: false,
    })} />);
    expect(screen.getByTestId("setup-progress").textContent).toContain("Installing replacement");
    expect(screen.getByRole("progressbar")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Resume CE setup" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reinstall" })).toBeNull();
    expect(onReinstall).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancelInstall).toHaveBeenCalledTimes(1);
  });

  it("keeps a failed reinstall visible after monitoring stops while the previous CE remains usable", () => {
    render(<HomePanel {...props({
      installBusy: false,
      setupPending: false,
      installOperation: {
        operation_id: "op",
        state: "failed",
        progress: null,
        message: "Managed Cheat Engine installation failed",
        error: "download timed out",
        installed: null,
      },
      ceRunning: false,
    })} />);

    const progress = screen.getByTestId("setup-progress").textContent ?? "";
    expect(progress).toContain("Managed Cheat Engine installation failed");
    expect(progress).toContain("download timed out");
    expect(screen.getByRole("button", { name: "Reinstall" })).toBeTruthy();
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("blocks CE replacement while the plugin-owned Cheat Engine process is still running", () => {
    const onReinstall = vi.fn();
    render(<HomePanel {...props({ ceRunning: true, onReinstall, reinstallLabel: "Reinstall CE" })} />);
    const reinstall = screen.getByRole("button", { name: "Reinstall" }) as HTMLButtonElement;
    expect(reinstall.disabled).toBe(true);
    fireEvent.click(reinstall);
    expect(onReinstall).not.toHaveBeenCalled();
  });

  it("opens on Advanced, and on Search when the last answer retired a table", () => {
    // Advanced is where this panel opens: it is the way on from here and the
    // half of the bottom row a user actually walks to.
    render(<HomePanel {...props()} />);
    expect(screen.getByRole("button", { name: "Advanced…" }).dataset.preferredFocus).toBe("true");
    expect(screen.getByRole("button", { name: "Search" }).dataset.preferredFocus).toBe("false");

    // Not after a table has just been confirmed as one that did not work.
    // Finding another one is the next thing that happens, and the panel Steam
    // builds when the user looks again is the first chance to say so.
    cleanup();
    const { rerender } = render(<HomePanel {...props({ preferSearchFocus: true })} />);
    expect(screen.getByRole("button", { name: "Search" }).dataset.preferredFocus).toBe("true");
    expect(screen.getByRole("button", { name: "Advanced…" }).dataset.preferredFocus).toBe("false");

    // The request is retired the moment the ring lands on Search, and that
    // must not hand the preference back to Advanced under a live screen.
    rerender(<HomePanel {...props({ preferSearchFocus: false })} />);
    expect(screen.getByRole("button", { name: "Search" }).dataset.preferredFocus).toBe("true");
    expect(screen.getByRole("button", { name: "Advanced…" }).dataset.preferredFocus).toBe("false");
  });

  it("leaves Advanced asking when Search cannot take the ring yet", () => {
    // A disabled control refuses the initial focus, so handing it the
    // preference and taking it off Advanced opens a panel with nothing asking
    // for the ring at all. The panel moves it itself once Search can be
    // pressed; until then this is where a panel opens.
    render(<HomePanel {...props({ preferSearchFocus: true, setupPending: true })} />);
    expect(screen.getByRole("button", { name: "Search" }).dataset.preferredFocus).toBe("false");
    expect(screen.getByRole("button", { name: "Advanced…" }).dataset.preferredFocus).toBe("true");
    cleanup();
    // Same for a panel that has no game to search for a table for.
    render(<HomePanel {...props({ preferSearchFocus: true, game: null, table: null })} />);
    expect(screen.getByRole("button", { name: "Search" }).dataset.preferredFocus).toBe("false");
    expect(screen.getByRole("button", { name: "Advanced…" }).dataset.preferredFocus).toBe("true");
  });

  it("hands the panel a box around Search to put the ring back on", () => {
    // The panel that was on screen the whole time is not being built again, so
    // nothing re-reads a preference for it: it moves the ring itself, and this
    // is the box it looks in.
    const searchButtonRef = { current: null as HTMLDivElement | null };
    render(<HomePanel {...props({ searchButtonRef })} />);
    const search = within(searchButtonRef.current as HTMLDivElement).getByRole("button");
    expect(search.textContent).toBe("Search");
  });

  it("keeps the table row to two controls, whatever this game has", () => {
    // A quick access panel is 300 pixels wide and this row already carries a
    // filename: Search, Local file and a third control were laid out past the
    // edge on the device, which is not a row a controller can walk. So the row
    // is Search and one press for everything else about which table this game
    // runs, and that press does not come and go with a count, because it is now
    // the only way to a local file and a first run has no table to reveal it.
    const onOpenImportedTables = vi.fn();
    for (const state of [{ table: null }, { table: null, game: null }, {}]) {
      cleanup();
      render(<HomePanel {...props({ ...state, onOpenImportedTables })} />);
      const row = screen.getByTestId("table-row");
      expect(within(row).getAllByRole("button").map((node) => node.textContent)).toEqual(["Search", "Manage"]);
    }
    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    expect(onOpenImportedTables).toHaveBeenCalled();
  });

  it("opens Advanced without leaking the controller click event into the game list", () => {
    const onAdvanced = vi.fn();
    render(<HomePanel {...props({ onAdvanced })} />);
    fireEvent.click(screen.getByRole("button", { name: "Advanced…" }));
    expect(onAdvanced).toHaveBeenCalledWith();
  });

  it("makes surviving first-run setup the only normal workflow after a frontend reload", () => {
    const onInstall = vi.fn();
    render(<HomePanel {...props({
      ceReady: false,
      ceStatusText: "not installed",
      setupPending: true,
      installBusy: true,
      installOperation: {
        operation_id: "op",
        state: "extracting",
        progress: null,
        message: "Installing Cheat Engine",
        error: null,
        installed: null,
      },
      onInstall,
    })} />);

    expect(screen.queryByRole("button", { name: "Resume CE setup" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Download and install CE" })).toBeNull();
    expect(onInstall).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Configure cheats" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Advanced…" }) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("quick-access panel density", () => {
  it("keeps setup to one row per subject so the cheats stay above the fold", () => {
    render(<HomePanel {...props()} />);
    const sections = screen.getAllByRole("region");
    const setup = sections[0];
    // Cheat Engine, the game and the table are one row each, and every one of
    // their actions rides on that row instead of owning a full-width button.
    expect(within(setup).getAllByTestId(/-row$/)).toHaveLength(3);
    expect(within(setup).queryByText("AppID 10")).toBeNull();
    for (const name of ["Reinstall", "Change", "Search", "Manage"]) {
      expect(within(setup).getByRole("button", { name })).toBeTruthy();
    }
  });

  it("never asks Steam for the full-bleed compact field padding", () => {
    // In the quick-access column `padding="compact"` drops the inline padding
    // and pulls the row 16px outside the section, which hangs labels off the
    // left edge and pushes the row's controls past the right edge of the panel.
    const { container } = render(<HomePanel {...props({ pinnedRows: [{ recordId: 7, label: "Health", summary: "", active: true }] })} />);
    const paddings = Array.from(container.querySelectorAll("[data-padding]"))
      .map((node) => node.getAttribute("data-padding"));
    expect(paddings.length).toBeGreaterThan(0);
    expect(paddings).not.toContain("compact");
  });

  it("scopes its density rules to this plugin and resolves Steam's real class names", () => {
    const { container } = render(<HomePanel {...props()} />);
    const wrapper = container.querySelector(".ce-decky-dense");
    expect(wrapper).toBeTruthy();
    const css = wrapper?.querySelector("style")?.textContent ?? "";
    // Every rule must be scoped; a bare Steam class would restyle the whole QAM.
    // An at-rule is judged by what it actually contains: `@keyframes` declares
    // an animation name and no selector at all, so it only has to carry this
    // plugin's prefix, while `@media` wraps real selectors that must each be
    // scoped exactly like a top-level rule.
    for (const rule of css.split("\n").filter(Boolean)) {
      if (rule.startsWith("@keyframes ")) {
        expect(rule.startsWith("@keyframes ce-decky-")).toBe(true);
        continue;
      }
      if (rule.startsWith("@media ")) {
        const inner = rule.slice(rule.indexOf("{") + 1, rule.lastIndexOf("}"));
        const selectors = inner.split("{")[0].split(",").map((part) => part.trim()).filter(Boolean);
        expect(selectors.length).toBeGreaterThan(0);
        for (const selector of selectors) expect(selector.startsWith(".ce-decky-dense")).toBe(true);
        continue;
      }
      expect(rule.startsWith(".ce-decky-dense")).toBe(true);
    }
    expect(css).toContain(".FieldLabel");
    expect(css).toContain(".FieldDescription");
    // Steam styles field padding through a three-class selector, so the scoped
    // rule has to outrank it rather than rely on stylesheet order.
    expect(css).toContain(".Field.Field.Field");
    // Steam's own section title and section spacing are bare content hashes on
    // the shipped client, so CE Decky draws its own heading and reaches the
    // section spacing structurally rather than by a discovered class name.
    expect(css).toContain(".ce-decky-heading::after");
    expect(css).toContain(".ce-decky-dense.ce-decky-dense.ce-decky-dense > div");
    // Row separators are off: the inset stripe they left read as a stray mark
    // rather than a division, and row spacing already separates the panel. The
    // rule is still emitted for the class Steam paints the line on, so the only
    // line left is the one dividing two sections.
    expect(css).toContain(".WithBottomSeparatorStandard::after { display: none; }");
    expect(css).toContain(".WithBottomSeparatorThick::after { display: none; }");
    expect(css).not.toContain("left: 15%");
    // A long label must be cut short by its own ellipsis rather than widen the
    // row and push its controls past the panel edge.
    expect(css).toContain(".FieldLeftColumn { min-width: 0; }");
  });

  it("puts the rare runtime actions on one shared row", () => {
    const onStopCE = vi.fn();
    const onAdvanced = vi.fn();
    render(<HomePanel {...props({ ceRunning: true, onStopCE, onAdvanced })} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop CE" }));
    fireEvent.click(screen.getByRole("button", { name: "Advanced…" }));
    expect(onStopCE).toHaveBeenCalledWith();
    expect(onAdvanced).toHaveBeenCalledWith();
  });
});

describe("row help", () => {
  it("hides an explanation behind ? until it is asked for", () => {
    render(<PanelRow label="Target process" description="game.exe" help="The exact Windows .exe Cheat Engine opens." />);
    expect(screen.queryByText("The exact Windows .exe Cheat Engine opens.")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "?" }));
    expect(screen.getByText("The exact Windows .exe Cheat Engine opens.")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "×" }));
    expect(screen.queryByText("The exact Windows .exe Cheat Engine opens.")).toBeNull();
  });

  it("adds no control to a row that has nothing to explain", () => {
    render(<PanelRow label="Storage" description="2 tables" />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("controller focus targets", () => {
  it("never wraps a lone action in a Focusable that would swallow its press", () => {
    // Steam gives a Focusable its own focus; around a single button it becomes
    // the focus target and pressing A does nothing at all.
    const onClick = vi.fn();
    const { container, rerender } = render(<PanelRow label="Game" actions={<SmallButton onClick={onClick}>Choose</SmallButton>} />);
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Choose" }));
    expect(onClick).toHaveBeenCalledTimes(1);

    rerender(<PanelRow label="Table" actions={<><SmallButton onClick={vi.fn()}>Search</SmallButton><SmallButton onClick={vi.fn()}>Local file</SmallButton></>} />);
    expect(container.querySelectorAll("[data-testid=focusable]").length).toBeGreaterThan(0);
  });

  it("keeps a lone help button out of a Focusable too", () => {
    const { container } = render(<PanelRow label="Runtime" help="What this row means." />);
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "?" }));
    expect(screen.getByText("What this row means.")).toBeTruthy();
  });

  it("makes a row with nothing to press a focus target so the panel can scroll to it", () => {
    // Steam scrolls a panel by moving focus into it, so a run of read-only rows
    // at the end of a screen is not merely unfocusable: it cannot be scrolled
    // to at all. Debug is almost entirely such rows and ended at whatever
    // fitted on one screen.
    const { container, rerender } = render(<PanelRow label="Storage" description="2 tables" />);
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(1);

    // A row whose only action is disabled has nothing reachable on it either,
    // so it is the same case rather than the "has its own control" one.
    rerender(<PanelRow label="Blocked" actions={<SmallButton disabled onClick={vi.fn()}>Clear</SmallButton>} />);
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(1);

    // The header tone is a list's own first row of data, not the caption above
    // it, so a summary with nothing to press is the same case again. Home's
    // runtime line is exactly that row, and it truncates: left out, it was the
    // one row on the panel that could neither be reached nor opened.
    rerender(<PanelRow tone="header" truncate label="Providers" description="5 sources" />);
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(1);
  });

  it("does not rebuild a row because the press it is running disabled its button", () => {
    // Almost every action here disables itself for the length of the press it
    // started: Use in the imported-table list, Read in the code view. Deciding
    // the row's wrapper from that took the row from "has a control" to "has
    // none" and back, and each of those unmounts everything inside it - so the
    // button the user had just pressed went away under the focus that was on
    // it, twice, on an ordinary press.
    const { container, rerender } = render(
      <PanelRow label="Table.CT" actions={<SmallButton onClick={vi.fn()}>Use</SmallButton>} />,
    );
    const before = container.querySelector("button");
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(0);

    rerender(<PanelRow label="Table.CT" actions={<SmallButton disabled onClick={vi.fn()}>Use</SmallButton>} />);
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(0);
    // The same element, not a new one that happens to look like it: this is the
    // whole point, because focus lives on the element rather than on the row.
    expect(container.querySelector("button")).toBe(before);

    rerender(<PanelRow label="Table.CT" actions={<SmallButton onClick={vi.fn()}>Use</SmallButton>} />);
    expect(container.querySelector("button")).toBe(before);
  });

  it("keeps a header row's own controls reachable on the row", () => {
    // The plugin-data screen's summary carries Delete, so the row that heads
    // that list is the one case where the header tone and a control meet.
    const onClick = vi.fn();
    render(<PanelRow tone="header" label="Safe to remove" actions={<SmallButton onClick={onClick}>Delete…</SmallButton>} />);
    fireEvent.click(screen.getByRole("button", { name: "Delete…" }));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("focusable")).toBeNull();
  });

  it("heads a list with its own first row of data rather than with the caption", () => {
    // The row that heads a list is the summary the list starts with: the totals
    // for every table source, the count of tables that did not work, Home's
    // live runtime state above the pinned cheats. The caption above it stays
    // text and a rule.
    const { container, rerender } = render(<PanelRow tone="header" label="5 of 5 sources on" />);
    expect(container.firstElementChild?.classList.contains("ce-decky-rowhead")).toBe(true);

    rerender(<PanelRow label="FearLess Cheat Engine" />);
    expect(container.firstElementChild?.classList.contains("ce-decky-rowhead")).toBe(false);
  });

  it("keeps a row's separator when opening it renders nothing under it", () => {
    // The help block draws its own separator, so a row that opens one gives up
    // its own. Wrapping renders nothing underneath, and taking the separator
    // there merged the row with the one below it.
    const long = "ready · 37 searches · 314 results · 11/12 downloads";
    const { container } = render(<PanelRow truncate label="fearless" description={long} />);
    const separatorOf = () => container.querySelector("[data-separator]")?.getAttribute("data-separator");
    expect(separatorOf()).toBe("standard");

    fireEvent.click(screen.getByTestId("focusable"));
    expect(within(screen.getByTestId("focusable")).getByText(long).className).toBe("ce-decky-wrap");
    expect(separatorOf()).toBe("standard");
  });

  it("gives a read-only row an activation even where there is nothing to open", () => {
    // A Focusable holding a control is a target because of that control; one
    // holding only text is a target only if it can be activated. On the target
    // every read-only row could be reached except the two carrying no
    // `truncate`, and the last section of Debug stayed unreachable.
    render(<PanelRow label="Enabled" description="provider_network, host_7zip" />);
    expect(screen.getByTestId("focusable").onclick).toBeTypeOf("function");
  });

  it("wraps a truncated read-only row when it is pressed instead of pressing nothing", () => {
    // The rows this makes focusable are exactly the ones whose text is cut off,
    // so the press has the same job the `?` button has on a row that has one.
    const long = "ready · 37 searches · 314 results · 11/12 downloads · 1 errors · HTTP 200";
    render(<PanelRow truncate label="fearless" description={long} />);
    const row = screen.getByTestId("focusable");
    expect(within(row).getByText(long).className).toBe("ce-decky-ellipsis");

    fireEvent.click(row);
    expect(within(row).getByText(long).className).toBe("ce-decky-wrap");

    fireEvent.click(row);
    expect(within(row).getByText(long).className).toBe("ce-decky-ellipsis");
  });

  it("counts enabled controls instead of rendered siblings before grouping them", () => {
    const onClick = vi.fn();
    const { container, rerender } = render(
      <ActionGroup>
        <SmallButton disabled onClick={vi.fn()}>Unavailable</SmallButton>
        <SmallButton onClick={onClick}>Available</SmallButton>
      </ActionGroup>,
    );
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Available" }));
    expect(onClick).toHaveBeenCalledTimes(1);

    rerender(
      <ActionGroup>
        <SmallButton onClick={vi.fn()}>First</SmallButton>
        <SmallButton onClick={vi.fn()}>Second</SmallButton>
      </ActionGroup>,
    );
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(1);
  });

  it("does not rebuild a group of actions because the press it is running disabled them", () => {
    // The same latch the row itself has, one level down. Advanced's Runtime row
    // carries Refresh runtime, Processes and `?`, and every one of the first
    // two disables both for the length of its own press: the group dropped to
    // one reachable control, swapped its Focusable for a plain div, and every
    // button in it was unmounted and mounted again when the press finished.
    const { container, rerender } = render(
      <ActionGroup>
        <SmallButton onClick={vi.fn()}>Refresh runtime</SmallButton>
        <SmallButton onClick={vi.fn()}>Processes</SmallButton>
      </ActionGroup>,
    );
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(1);
    const before = container.querySelector("button");

    rerender(
      <ActionGroup>
        <SmallButton disabled onClick={vi.fn()}>Refresh runtime</SmallButton>
        <SmallButton disabled onClick={vi.fn()}>Processes</SmallButton>
      </ActionGroup>,
    );
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(1);
    expect(container.querySelector("button")).toBe(before);

    rerender(
      <ActionGroup>
        <SmallButton onClick={vi.fn()}>Refresh runtime</SmallButton>
        <SmallButton onClick={vi.fn()}>Processes</SmallButton>
      </ActionGroup>,
    );
    expect(container.querySelector("button")).toBe(before);
    expect((container.querySelector("button") as HTMLButtonElement).disabled).toBe(false);
  });

  it("lets a group go back to a plain block when a control is gone rather than disabled", () => {
    // A group holding one reachable control is a container Steam can land on
    // whose activation does nothing, which is what grouping only two or more
    // avoids. Holding the wrapper across a press must not put that back for a
    // row that has genuinely lost a control: the search row loses Retry once
    // the marks it would clear are gone.
    const { container, rerender } = render(
      <ActionGroup>
        <SmallButton onClick={vi.fn()}>Search</SmallButton>
        <SmallButton onClick={vi.fn()}>Retry 2</SmallButton>
      </ActionGroup>,
    );
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(1);

    rerender(
      <ActionGroup>
        <SmallButton onClick={vi.fn()}>Search</SmallButton>
      </ActionGroup>,
    );
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(0);
  });

  it("does not hide a cheat toggle behind a group when its neighbour is disabled", () => {
    const onActiveChange = vi.fn();
    const { container } = render(
      <CheatRow
        label="Health"
        active={false}
        onActiveChange={onActiveChange}
        actions={<button disabled>More</button>}
      />,
    );
    expect(container.querySelectorAll("[data-testid=focusable]")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("toggle"));
    expect(onActiveChange).toHaveBeenCalledWith(true);
  });

  it("marks a cheat's switch so a probe on a device can name and read it", () => {
    // Steam draws a toggle as a div: not a button, not an input, and with no
    // name on it. `scripts/target_panel_read.py` could report every row of a
    // screen and reach none of the switches, which is most of what this product
    // is, so the mark is the contract between the two.
    const { container, rerender } = render(<CheatRow label="Health" active={false} onActiveChange={vi.fn()} />);
    const box = container.querySelector('[data-switch="active"]');
    expect(box).toBeTruthy();
    expect(box!.getAttribute("data-checked")).toBe("false");
    rerender(<CheatRow label="Health" active onActiveChange={vi.fn()} />);
    expect(container.querySelector('[data-switch="active"]')!.getAttribute("data-checked")).toBe("true");
    // A record with no active state of its own draws no switch, so there is
    // nothing to mark and nothing for a probe to press.
    rerender(<CheatRow label="Health" active={null} onActiveChange={vi.fn()} />);
    expect(container.querySelector('[data-switch="active"]')).toBeNull();
  });
});
