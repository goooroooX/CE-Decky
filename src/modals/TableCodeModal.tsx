import { useUiSurface } from "../useUiSurface";
import { traceUiAction, startUiOperation } from "../uiActions";
import { Focusable, ModalRoot, PanelSection } from "@decky/ui";
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { listTableCode, readTableCode } from "../api";
import { PagerFooter } from "../components/PagerFooter";
import { CONTENTS_ONLY, DensePanel, PanelNote, PanelRow, SectionHeading, SmallButton, focusFirstEnabled, useFittedRows, usePageHeight, useRowHeight } from "../components/PanelDensity";
import { describeError } from "../errors";
import { logUi, logUiFailure } from "../supportLog";
import { sharedContextDepth, tidySegment } from "../uiModel";
import { MODAL_BOTTOM_PADDING, latchChrome, rowsThatFit, viewportHeight, type LatchedChrome } from "../viewport";
import type { TableCodeBody, TableCodeIndex, TableCodeSection } from "../types";

interface Props {
  sha256: string;
  /** The file's own name, so the screen says which table is being read. */
  filename: string;
  onBack: () => void;
}

/**
 * The window the code is read through, in rows of text rather than in lines.
 *
 * It is fixed, and the page is built to fill it exactly once. A page of eighty
 * lines was taller than the modal, and a controller cannot scroll a block of
 * text: Steam moves the panel by moving focus, and a plain block of code has
 * nothing in it to focus, so focus jumped from the row above it straight to
 * Next and everything between the two was unreachable. Nothing was wrong with
 * the paging; the page was simply bigger than the thing showing it.
 *
 * Counted in rows because a long line wraps into several of them, which is what
 * makes a fixed count of lines overflow anyway: one `print('… WARNING: …')` is
 * three rows on this screen. `CHARS_PER_ROW` is that wrap point, measured
 * against the panel's own monospace at this size and deliberately short, since
 * guessing it long is what puts a page off the bottom of the screen again.
 *
 * The row count leaves room for the two notes a section can carry above it,
 * because those appear exactly on the sections whose code is worth reading
 * carefully and must not be what pushes the controls off the screen.
 *
 * It is the window a full-size screen gets. A handheld gives this page 534 CSS
 * pixels where a television gives 844, and twenty-six rows plus this screen's
 * own chrome does not fit in the first: the sheet was drawn from 115 pixels
 * above the top of the display, so its heading, the row naming the section and
 * the first lines of every page were off the screen with no press that could
 * reach them, and the ring opened at the bottom because the bottom was the only
 * part on it. `rowsThatFit` is what turns that into a page that fits, and it
 * never returns more than this, so nothing changes on a screen that had room.
 */
const ROWS_PER_PAGE = 26;
/**
 * The fewest rows this screen is still itself with.
 *
 * Below about this, paging stops being reading and becomes pressing Next, and
 * a page holding less than a short function tells the reader nothing about what
 * an exact SHA would run.
 *
 * It is a preference, not a floor over the display. It used to be the second,
 * on the ground that a screen too small for this many rows does not arise on
 * anything measured, and it does: a handheld gives this screen 534 pixels, of
 * which everything that is not code takes 320, and 424 where the section
 * carries both of its notes. That leaves fourteen rows, or seven with the
 * notes, and holding out for twelve would draw the page past the display. What
 * goes off a Steam Deck is the top, taking the heading, the row naming the
 * section and the first lines of every page with it, so `rowsThatFit` lets the
 * measured height win and this stays the preference for a screen with the room.
 */
const MIN_ROWS_PER_PAGE = 12;
/**
 * Everything on this screen that is not a row of code, in CSS pixels.
 *
 * Steam's own modal padding and button bar, this panel's heading, the row that
 * names the section, and the footer holding the paging controls and the way out.
 *
 * That footer used to be two rows, a pager inside the list and a button bar
 * under it, and the figure below was measured while it was. It is therefore
 * about one row too generous now and the page is one row shorter than the
 * screen could hold, which is the direction to be wrong in: the other one puts
 * the top of the window off the display. It stays until somebody measures it
 * again on a device, because deriving a new number by subtracting an estimate
 * of the row that went is exactly the inference the paragraph below is about.
 *
 * Measured on the device rather than derived. `target_ui_layout_probe.py` on a
 * Steam Deck LCD reports the page this modal opens in as 854x534 CSS pixels,
 * and the sheet on that page as 335 tall while it was showing a single 15 pixel
 * row of code. Everything that is not code is therefore 320, and the earlier
 * 264 was an inference from a different screen state that this one never
 * matched.
 *
 * It is deliberately an estimate again. A previous version measured this at
 * runtime, from the difference between the sheet and the block inside it, which
 * was sound reasoning and produced a page of one row on the device: the sheet
 * does not shrink with its contents, so the difference is not the quantity it
 * looks like. Reading a number off a device once, and stating where it came
 * from, is worth more here than a mechanism that is wrong in a way nobody can
 * see from the source. `code.page_sized` records what this screen decided, so
 * the next display that disagrees says so with numbers.
 */
const CODE_PAGE_CHROME = 320;
/**
 * What one note above the code costs, when a section carries one.
 *
 * Both of them are fixed sentences and the panel is the same width on every
 * display measured, so their height is a property of the text rather than of
 * the screen. This is an estimate of it and is deliberately generous: spending
 * a row that was not needed costs one line of a page, and reclaiming one that
 * was needed puts the top of the screen back off the display.
 */
const NOTE_HEIGHT = 52;
const CHARS_PER_ROW = 68;
const ROW_HEIGHT = 15;

/** How many sections the list starts with, extended by a press. */
const PAGE_SECTIONS = 12;
/**
 * How tall one section row is, for fitting a page of them to the screen.
 *
 * A starting point rather than the answer: the row is measured, because a short
 * screen trims what a row is drawn with and a constant beside that stylesheet
 * would go on describing the old one.
 */
const SECTION_ROW_HEIGHT = 58;
/** The fewest sections a page may hold before the measurement stops taking any. */
const MIN_SECTION_ROWS = 3;

const KIND_LABELS: Record<TableCodeSection["kind"], string> = {
  lua: "Lua",
  auto_assembler: "Auto Assembler",
  form: "Window",
  embedded_file: "Embedded file",
};

/**
 * The last code point drawn at one column in the block's monospace face.
 *
 * Latin, its supplements and extensions, IPA, the combining marks, Greek,
 * Cyrillic and Armenian all end here, and a table's comments are written in
 * those. Everything above it is assumed to be two columns wide, which is what
 * East Asian text, box drawing, arrows, symbols and emoji actually are in a
 * monospace face, and what an unknown code point in a font this project does
 * not control has to be assumed to be.
 *
 * The direction of the guess is the whole point. A list of the ranges known to
 * be wide leaves every range nobody thought of counted as narrow, so a page of
 * emoji or of some symbol family outside the list holds more drawn rows than
 * the count believes and the bottom of it goes off the block again. Guessing
 * wide costs a page; guessing narrow loses code on the one screen that exists
 * to show exactly what an exact SHA would run.
 */
const LAST_NARROW_CODE_POINT = 0x058f;

/** Columns one code point takes in the block's monospace face. */
function columnsOf(text: string): number {
  let columns = 0;
  for (const character of text) {
    // A tab advances to the next eight-column stop and is counted at its
    // widest, for the same reason: a row estimated short is a row that fits.
    if (character === "\t") columns += 8;
    else columns += (character.codePointAt(0) ?? 0) <= LAST_NARROW_CODE_POINT ? 1 : 2;
  }
  return columns;
}

/**
 * The code itself: a fixed frame holding one element per drawn row.
 *
 * The frame is the same height on every page, including a last page holding two
 * lines. A block that grew and shrank would move Previous, Next and Back under
 * the user's thumb between presses, which is the thing paging exists to avoid.
 */
const CODE_BLOCK: CSSProperties = {
  // The height is set where the block is used, because how many rows fit is a
  // property of the display rather than of this stylesheet.
  fontFamily: "'DejaVu Sans Mono', 'Consolas', monospace",
  fontSize: 11,
  lineHeight: `${ROW_HEIGHT}px`,
  padding: "6px 8px",
  margin: "0 0 4px",
  borderRadius: 4,
  background: "hsla(0, 0%, 0%, 0.32)",
  color: "hsla(0, 0%, 100%, 0.86)",
  overflow: "hidden",
};

/**
 * One drawn row, which is not allowed to become two.
 *
 * The page used to be a single block of text that the browser wrapped, so how
 * much of it was on screen depended on a width estimate agreeing with a font
 * this project does not ship: an estimate that read a character narrow put the
 * end of the page under the block's own `overflow`, where no press could reach
 * it and nothing said it was there. `pre` cannot wrap, so the page is exactly
 * as tall as the rows it holds whatever the estimate did, and a row the
 * estimate got wrong is one row that ends early rather than a page that does.
 */
const CODE_ROW: CSSProperties = {
  height: ROW_HEIGHT,
  whiteSpace: "pre",
  overflow: "hidden",
};

/** The footer's padding, which is the modal's rather than the code block's. */
const CODE_FOOTER: CSSProperties = { padding: "var(--ce-footer-padding, 0 16px 6px)" };

function sizeText(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${Math.max(1, Math.round(bytes / 1024))} KiB`;
}

/**
 * What one exact table can execute, in the user's own hands.
 *
 * `.CT` import is not execution consent, and the decision this product asks for
 * is whether an exact SHA may run what it carries. The only answer offered for
 * that was a count of markers: "Lua AutoAssembler 1 embedded file(s)". Reading
 * the scripts themselves meant Desktop Mode, a file manager and a text editor,
 * none of which a user in Game Mode has, so in practice the consent screen
 * asked a question it gave no way to answer.
 *
 * It reads and never runs. Every section is normalized the way a control label
 * is, because a script that can render as something other than what it is would
 * be a worse lie here than anywhere else on the panel, and an embedded payload's
 * bytes are described rather than returned.
 */
export function TableCodeModal({ sha256, filename, onBack }: Props) {
  useUiSurface("TableCodeModal", sha256);
  const [index, setIndex] = useState<TableCodeIndex | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [section, setSection] = useState<TableCodeBody | null>(null);
  const [sectionBusy, setSectionBusy] = useState(false);
  const sectionBusyRef = useRef(false);
  const [page, setPage] = useState(0);
  const [sectionPage, setSectionPage] = useState(0);
  const [listNode, setListNode] = useState<HTMLDivElement | null>(null);
  const [listFooterNode, setListFooterNode] = useState<HTMLDivElement | null>(null);
  // A late answer must not repaint a screen that has moved on, and this tree is
  // unmounted by its host rather than by itself.
  const liveRef = useRef(true);

  useEffect(() => () => { liveRef.current = false; }, []);

  useEffect(() => {
    logUi("panel.modal_opened", { modal: "table_code" });
    void (async () => {
      try {
        const answer = await listTableCode(sha256);
        if (liveRef.current) setIndex(answer);
      } catch (cause) {
        logUiFailure("table_code.index_failed", cause);
        if (liveRef.current) setError(describeError(cause));
      } finally {
        if (liveRef.current) setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sha256]);

  const open = async (entry: TableCodeSection) => {
    if (sectionBusyRef.current) return;
    sectionBusyRef.current = true;
    lastOpenedSectionRef.current = entry.id;
    setSectionBusy(true);
    setError(null);
    const operation = startUiOperation("table_code.read", { table_sha: sha256, section: entry.id });
    try {
      const body = await readTableCode(sha256, entry.id);
      operation.completed({ stale: !liveRef.current });
      if (!liveRef.current) return;
      setSection(body);
      setPage(0);
    } catch (cause) {
      operation.failed(cause);
      logUiFailure("table_code.section_failed", cause, { section: entry.id });
      if (liveRef.current) setError(describeError(cause));
    } finally {
      sectionBusyRef.current = false;
      if (liveRef.current) setSectionBusy(false);
    }
  };

  const sections = index?.sections ?? [];
  // What this screen fits, measured, rather than twelve rows and a press that
  // adds twelve more. A handheld fits four of these and the reader was walking
  // the stick to the bottom of the window to ask for the next twelve.
  const [listChrome, setListChrome] = useState<LatchedChrome | null>(null);
  useLayoutEffect(() => {
    setListChrome((held) => latchChrome(held, listNode, listFooterNode, MODAL_BOTTOM_PADDING, !loading));
  }, [listNode, listFooterNode, loading]);
  const sectionRowHeight = useRowHeight(listNode, SECTION_ROW_HEIGHT);
  const sectionsAsked = useMemo(() => (listChrome === null ? PAGE_SECTIONS : rowsThatFit({
    full: PAGE_SECTIONS,
    rowHeight: sectionRowHeight,
    chrome: listChrome.value,
    minimum: MIN_SECTION_ROWS,
    node: listNode,
  })), [listNode, listChrome, sectionRowHeight]);
  // How far this list's window ended from Steam's bar, after that arithmetic.
  // The same question every paged screen here asks once it has sized itself,
  // and this is the screen that shows it is worth asking in both directions: a
  // Steam Deck left 54 pixels of room under a list whose rows are 37.
  const sectionsFitted = useFittedRows(
    listFooterNode, sectionRowHeight, listChrome?.settled === true, sections.length > sectionsAsked,
    error ? 1 : 0,
  );
  // Never past the page this screen was built with, for the same reason
  // `rowsThatFit` caps there.
  const sectionsPerPage = Math.min(PAGE_SECTIONS, Math.max(1, sectionsAsked + sectionsFitted));
  const sectionPages = Math.max(1, Math.ceil(sections.length / sectionsPerPage));
  const safeSectionPage = Math.min(sectionPage, sectionPages - 1);
  const visible = useMemo(
    () => sections.slice(safeSectionPage * sectionsPerPage, (safeSectionPage + 1) * sectionsPerPage),
    [sections, safeSectionPage, sectionsPerPage],
  );
  // A page of sections is held at the height a full page of them takes, so the
  // last page and a table with three sections in it leave the window exactly
  // where a full one does. Without it the window shrank on the last page and
  // took the pager and the way out somewhere new, which on a screen read by
  // repeating one press is the control moving out from under the thumb.
  const sectionPageHeight = usePageHeight(listNode, visible.length, sectionsPerPage);
  // What every one of these sections sits under says nothing about any of them,
  // and it is not free: each row is one truncated line, so a table whose author
  // nests everything under two instruction steps spent that whole line on the
  // same 128 characters and cut off the size and the line count behind them.
  const sharedPath = useMemo(
    () => sharedContextDepth(sections.map((entry) => entry.path)),
    [sections],
  );
  const contextOf = (path: readonly string[]): string | null => {
    const rest = path.slice(sharedPath).map(tidySegment).filter(Boolean);
    return rest.length > 0 ? rest.join(" \u203a ") : null;
  };
  // The section as the rows the window shows, worked out once per section. A
  // page is a fixed number of those rather than of lines, because a line can be
  // wider than the window and every part of it has to be reachable.
  const rows = useMemo(() => codeRows(section?.lines ?? []), [section]);
  // How many of those the display in front of the reader can actually hold. The
  // notes are part of the answer because they are part of the screen: a section
  // that carries one has that much less room for its code, and reserving their
  // height only where they appear is what keeps the ordinary section at the
  // full page. A page is worked out per section rather than per page, so the
  // block never changes height under a thumb that is paging through one.
  /**
   * Everything on this screen that is not a row of code, for this section.
   *
   * The constant plus whatever notes this particular section carries, because a
   * section that carries one has that much less room for its code and reserving
   * their height only where they appear is what keeps an ordinary section at
   * the full page. `code.page_sized` records what this arrived at, against the
   * page height it was measured with, so a display that disagrees says so in
   * numbers rather than in a report that the screen looks wrong.
   */
  const estimatedChrome = CODE_PAGE_CHROME
    + (section?.sanitized ? NOTE_HEIGHT : 0)
    + (section?.truncated ? NOTE_HEIGHT : 0);
  /**
   * A node of this screen, so the page it is drawn in can be asked its height.
   *
   * Not for measuring anything itself. This plugin's code runs in Steam's
   * shared context, whose window is one pixel tall, and its DOM is rendered in
   * the page the user is looking at; a node is the only route from one to the
   * other. Held in state rather than in a ref alone, because the first render
   * has no node and the page height has to be asked again once there is one.
   */
  const [pageNode, setPageNode] = useState<HTMLDivElement | null>(null);
  const rowsPerPage = useMemo(() => rowsThatFit({
    full: ROWS_PER_PAGE,
    rowHeight: ROW_HEIGHT,
    chrome: estimatedChrome,
    minimum: MIN_ROWS_PER_PAGE,
    node: pageNode,
  }), [estimatedChrome, pageNode]);
  // The two numbers this page is sized from, recorded once per section. The
  // height is read from the page and the chrome is a figure measured on one
  // device, so a page that comes out wrong on another is answerable from a
  // support bundle rather than from a second device session.
  useEffect(() => {
    if (!section) return;
    logUi("code.page_sized", {
      section: section.id,
      viewport: viewportHeight(pageNode),
      chrome: estimatedChrome,
      rows: rowsPerPage,
    });
  }, [section, rowsPerPage]);
  const pages = Math.max(1, Math.ceil(rows.length / rowsPerPage));
  const safePage = Math.min(page, pages - 1);
  const pageRows = useMemo(
    () => rows.slice(safePage * rowsPerPage, (safePage + 1) * rowsPerPage),
    [rows, safePage, rowsPerPage],
  );

  // Where the focus ring sits while a script is being read.
  //
  // Reading one of these is a single press repeated: the window holds a fixed
  // page of rows, and the only thing to do with a page once it has been read is
  // ask for the next one. Focus opened on the first row of the panel instead,
  // so every reader began by walking the stick down the whole window to find
  // Next, and the last page then disabled Next under the thumb that was on it,
  // which drops the ring entirely and leaves a window with nothing focused.
  //
  // So the press that matters holds the ring: Next while there is a next page,
  // and the way out of the window once there is not, which by then is the only
  // press left.
  //
  // Re-asserted on every page rather than only on those two, because the row
  // these controls sit on rebuilds itself once: a group with one control left
  // to press is a plain box and a group with two is a navigation container, and
  // the first press of Next is what takes it from one to the other. That
  // replaces both buttons, and the ring goes with the button that was under the
  // thumb. So which of them the reader is working is remembered instead of read
  // back off the screen, and putting the ring back is what survives the rebuild.
  const nextRef = useRef<HTMLDivElement | null>(null);
  const previousRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLDivElement | null>(null);
  const sectionButtonRefs = useRef(new Map<string, HTMLDivElement>());
  const lastOpenedSectionRef = useRef<string | null>(null);
  const returningToListRef = useRef(false);
  const initialListFocusedRef = useRef(false);
  const focusedSectionRef = useRef<string | null>(null);
  const turnedRef = useRef<"next" | "previous">("next");
  const firstSectionId = sections[0]?.id ?? null;

  const closeSection = () => {
    returningToListRef.current = true;
    setSection(null);
  };

  useEffect(() => {
    if (!section) {
      focusedSectionRef.current = null;
      if (returningToListRef.current) {
        if (sectionBusy) return;
        returningToListRef.current = false;
        const sectionId = lastOpenedSectionRef.current;
        const holder = sectionId ? sectionButtonRefs.current.get(sectionId) : null;
        if (holder) focusFirstEnabled({ current: holder });
        return;
      }
      // The list arrives after the modal has mounted, so Steam's initial focus
      // has already landed elsewhere. Put it on the first section the user can
      // open once that row actually exists; returning from a section is handled
      // above and must keep the exact row that opened it.
      if (!initialListFocusedRef.current && !loading && !error && firstSectionId) {
        const holder = sectionButtonRefs.current.get(firstSectionId);
        if (holder) {
          initialListFocusedRef.current = true;
          focusFirstEnabled({ current: holder });
        }
      }
      return;
    }
    // A section that has just been opened is read forwards, whatever the last
    // press in the section before it was.
    if (focusedSectionRef.current !== section.id) {
      focusedSectionRef.current = section.id;
      turnedRef.current = "next";
    }
    const holder = safePage >= pages - 1
      ? closeRef
      : turnedRef.current === "previous" ? previousRef : nextRef;
    // The control the reader was working first, then where the ring goes when
    // that control has just disabled itself under the thumb.
    focusFirstEnabled(holder, nextRef, closeRef);
  }, [section, sectionBusy, safePage, pages, loading, error, firstSectionId]);

  if (section) {
    // Which lines of the file this page holds. A line wide enough to take more
    // than one page is on both of them, which is what happened to it.
    const from = pageRows[0]?.line ?? 1;
    const to = pageRows[pageRows.length - 1]?.line ?? from;
    return (
      <ModalRoot onCancel={traceUiAction("table_code_modal.close_section", closeSection)}>
        <Focusable style={{ minWidth: 440, maxWidth: 720 }}>
          <DensePanel>
            <PanelSection>
              <SectionHeading>{KIND_LABELS[section.kind]}</SectionHeading>
              <PanelRow
                tone="header"
                truncate
                testId="table-code-section"
                label={section.title}
                description={[
                  section.readable ? `lines ${from}-${to} of ${section.lines.length}` : "content not shown",
                  section.total_lines !== section.lines.length && section.readable
                    ? `${section.total_lines} in the file`
                    : null,
                  // Behind the counter: which page of this script is on screen
                  // is what a reader needs while paging through it, and where
                  // the script sits does not change between pages.
                  contextOf(section.path),
                ].filter(Boolean).join(" · ")}
              />
              {!section.readable && (
                <PanelRow
                  truncate
                  label="This is a payload, not a script"
                  description="CE Decky reports that the table carries this file and never hands its bytes to the panel. Its name and size are the whole of what can be said about it here."
                />
              )}
              {section.sanitized && (
                <PanelNote>
                  A line carried an invisible or text-reordering character, which was removed before
                  this was shown. What Cheat Engine would run is the file, not this rendering of it.
                </PanelNote>
              )}
              {section.truncated && (
                <PanelNote>
                  This section is longer than the panel will show and was cut. The whole of it is in
                  the file, and in the support bundle from Advanced.
                </PanelNote>
              )}
              {pageRows.length > 0 && (
                <div ref={setPageNode} style={{ ...CODE_BLOCK, height: rowsPerPage * ROW_HEIGHT }} data-testid="table-code-block">
                  {pageRows.map((row, index) => (
                    <div key={`${row.line}:${index}`} style={CODE_ROW} data-testid="table-code-row">{row.text}</div>
                  ))}
                </div>
              )}
            </PanelSection>
          </DensePanel>
          {/* The footer every paged screen here ends with. This one used to
              page from a row inside the code, labelled with the page number,
              and put the way out on a row of its own below it: two rows of a
              window whose whole purpose is fitting more lines of a script on
              screen. The ring is placed by this screen rather than by the
              footer, because reading ends on the last page and the way out is
              the only press left there. */}
          <PagerFooter
            testId="table-code-footer"
            style={CODE_FOOTER}
            page={safePage}
            pages={pages}
            previousRef={previousRef}
            nextRef={nextRef}
            focusOnTurn={false}
            onPage={(next, turned) => { turnedRef.current = turned; setPage(next); }}
            trailing={<div ref={closeRef} style={CONTENTS_ONLY}><SmallButton onClick={traceUiAction("table_code_modal.back_to_the_list", closeSection)}>Back to the list</SmallButton></div>}
          />
        </Focusable>
      </ModalRoot>
    );
  }

  return (
    <ModalRoot onCancel={traceUiAction("table_code_modal.cancel_back", onBack)}>
      <Focusable style={{ minWidth: 440, maxWidth: 720 }}>
        <DensePanel>
          <PanelSection>
            <SectionHeading>Inside this table</SectionHeading>
            <PanelRow
              tone="header"
              truncate
              testId="table-code-summary"
              label={loading ? "Reading the table…" : summaryLabel(index, sections.length)}
              description={[
                filename,
                sha256.slice(0, 12),
                index ? sizeText(index.size) : null,
                // Beside the count of what runs, because the two are read
                // against each other: a table of eighteen cheats behind two
                // scripts said only "2 Auto Assembler", and a reader who had
                // just seen the eighteen had no way to tell whether the rest
                // were being withheld or simply have no code to show.
                index ? `${index.records} cheat(s) declared` : null,
              ].filter(Boolean).join(" · ")}
              help="Everything in this table that Cheat Engine can execute, which is what using it authorizes. CE Decky reads it and runs nothing: the scripts are shown as text, and an embedded file is named rather than opened. Most cheats carry no code of their own - they are an address and a value, and one script often creates dozens of them - so this list is normally far shorter than the cheat list, and it is the cheat list, not this one, where addresses and offsets are readable."
            />
            {error && <PanelRow truncate testId="table-code-error" label="Could not read the table" description={error} />}
            {!loading && !error && sections.length === 0 && (
              <PanelRow
                label="Nothing to show"
                description="This table declares no Lua, no Auto Assembler script, no window and no embedded file. It is addresses and values only."
              />
            )}
            <div ref={setListNode} style={sectionPageHeight === null ? undefined : { minHeight: sectionPageHeight }} data-testid="table-code-list">
            {visible.map((entry) => (
              <PanelRow
                key={entry.id}
                truncate
                testId={`table-code-${entry.id.replace(":", "-")}`}
                label={entry.title}
                description={[
                  KIND_LABELS[entry.kind],
                  sizeText(entry.bytes),
                  entry.readable ? `${entry.lines} line(s)` : "not shown",
                  entry.truncated ? "cut to fit" : null,
                  // Last, because it is the part of this row a reader can do
                  // without: one truncated line holds all of it only sometimes,
                  // and what it holds should be what tells these rows apart.
                  contextOf(entry.path),
                ].filter(Boolean).join(" · ")}
                actions={(
                  <div
                    ref={(node) => {
                      if (node) sectionButtonRefs.current.set(entry.id, node);
                      else sectionButtonRefs.current.delete(entry.id);
                    }}
                    style={CONTENTS_ONLY}
                  >
                    <SmallButton disabled={sectionBusy} onClick={traceUiAction("table_code_modal.read_section", () => void open(entry), { table_sha: sha256, section: entry.id })}>
                      {entry.readable ? "Read" : "About"}
                    </SmallButton>
                  </div>
                )}
              />
            ))}
            </div>
            {index && index.omitted_sections > 0 && (
              <PanelNote>
                {`${index.omitted_sections} further section(s) are in the file and beyond what this screen will list.`}
              </PanelNote>
            )}
          </PanelSection>
        </DensePanel>
        {/* The same footer every paged screen here ends with, rather than a
            row inside the list asking for twelve more and a full width button
            under it. */}
        <PagerFooter
          testId="table-code-footer"
          containerRef={setListFooterNode}
          style={CODE_FOOTER}
          page={safeSectionPage}
          pages={sectionPages}
          preferNext
          onPage={setSectionPage}
          trailing={<SmallButton onClick={traceUiAction("table_code_modal.back", onBack)}>Back</SmallButton>}
        />
      </Focusable>
    </ModalRoot>
  );
}

/** One rendered row of the block, and the line of the file it came from. */
export interface CodeRow {
  text: string;
  /** 1-based line in the section, repeated by every row a long line takes. */
  line: number;
}

/**
 * A section as the rows the window actually shows, losing nothing.
 *
 * Paging between whole lines cannot be lossless: the backend allows one line of
 * up to 2048 bytes, the window is 26 rows of about 68 columns, and a line past
 * that simply got a page of its own with everything after the 26th row clipped
 * by the block. No later page held it, and nothing on screen said so, on the
 * screen whose whole purpose is showing exactly what an exact SHA would run.
 *
 * So a long line is cut into rows here instead, at the width of the block and
 * counted in columns rather than characters, and paging is then arithmetic on
 * rows. Every byte the backend hands over is reachable by pressing Next.
 *
 * Each row is then drawn in an element that cannot wrap, so how tall a page is
 * does not depend on this width estimate being right about a font: what the
 * estimate decides is where a line is cut, not whether the page fits.
 */
export function codeRows(lines: readonly string[]): CodeRow[] {
  const rows: CodeRow[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    let text = "";
    let columns = 0;
    for (const character of lines[index]) {
      const cost = columnsOf(character);
      if (columns + cost > CHARS_PER_ROW && text.length > 0) {
        rows.push({ text, line: index + 1 });
        text = "";
        columns = 0;
      }
      text += character;
      columns += cost;
    }
    // An empty line is a row of its own: it is in the file and it is what
    // separates one block of a script from the next.
    rows.push({ text, line: index + 1 });
  }
  return rows;
}

function summaryLabel(index: TableCodeIndex | null, count: number): string {
  if (!index) return "Could not be read";
  const parts = [
    index.totals.lua ? `${index.totals.lua} Lua` : null,
    index.totals.auto_assembler ? `${index.totals.auto_assembler} Auto Assembler` : null,
    index.totals.form ? `${index.totals.form} window` : null,
    index.totals.embedded_file ? `${index.totals.embedded_file} embedded file` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : `${count} section(s)`;
}
