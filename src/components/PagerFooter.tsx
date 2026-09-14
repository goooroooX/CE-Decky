import { NavEntryPositionPreferences } from "@decky/ui";
import { useEffect, useRef, type CSSProperties, type MutableRefObject, type ReactNode, type Ref } from "react";

import { traceUiAction } from "../uiActions";
import { ActionGroup, CONTENTS_ONLY, PAGER_FOOTER_CLASS, SmallButton, focusFirstEnabled } from "./PanelDensity";

/**
 * The row a paged screen ends with: paging in the middle, the way out at the
 * right edge.
 *
 * Both used to be full-width buttons stacked under each other, so a screen that
 * shows six rows spent three rows of its height on two presses and a third that
 * is the same press on every screen in the product. They are one row of small
 * controls, and one row is also what keeps the window still: a screen whose
 * pager comes and goes on its own line changes height while it is being read,
 * and the way out moves with it.
 *
 * A grid rather than `space-between`, because what has to be centred is centred
 * on the row and not on whatever is left over beside the button: the two outer
 * tracks are equal whether or not either of them holds anything. The grid is
 * also the one ActionGroup for the whole footer, so Previous, Next and the
 * screen's own action all stay on one left/right controller path.
 */
const FOOTER_FOCUS_ROW: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "1fr auto 1fr",
  alignItems: "center",
  gap: 8,
  // Read from the row rather than written here, so a short screen can close it
  // up: an inline length outranks a stylesheet, and this row is drawn outside
  // the dense wrapper where the scoped rules cannot reach it anyway.
  padding: "var(--ce-footer-row-padding, 6px 0 2px)",
};
const FOOTER_PAGER: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "center", gap: 8 };
// The same eight pixels the row itself uses. A screen can put more than one
// control here - Configure cheats ends in Apply and the way out - and a box with
// no gap in it stuck them together, which is the same mistake a row's own action
// group was carrying until its fallback gap was written back in.
const FOOTER_TRAILING: CSSProperties = { display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8 };
// Between the two paging controls, which is where a reader looks for it and the
// only place it does not cost a line of its own. Tabular figures so the row
// does not shift under the thumb as the page number changes width.
const FOOTER_PAGE_COUNT: CSSProperties = {
  flex: "0 0 auto",
  fontSize: 12,
  lineHeight: "16px",
  opacity: 0.75,
  whiteSpace: "nowrap",
  fontVariantNumeric: "tabular-nums",
};

interface Props {
  testId: string;
  /** The page on screen, zero based and already clamped to what exists. */
  page: number;
  pages: number;
  /** Turn to this page. Never called with a page outside the list. */
  onPage: (page: number, turned: "next" | "previous") => void;
  disabled?: boolean;
  /** The screen's own action, against the right edge. */
  trailing?: ReactNode;
  /**
   * Which paging control the ring lands on when it enters this row from above.
   *
   * Previous is the first control in the row and is disabled on page one, so a
   * reader arriving from the list landed on a button that does nothing and read
   * the whole footer as dead. Next is the press they came down here to make.
   *
   * Two things say that, and they are two halves of one statement: the group
   * asks Steam to enter at its preferred child, and the child says it is the
   * preferred one. Splitting them into separate props looked tidier and was
   * simply broken - `PREFERRED_CHILD` with nothing marked preferred falls back
   * to the first child, which is Previous, which is the control this exists to
   * skip.
   */
  preferNext?: boolean;
  containerRef?: Ref<HTMLDivElement>;
  style?: CSSProperties;
  /**
   * The two paging controls, for a screen that puts the ring back itself.
   *
   * Handed out rather than kept private because one screen's rule is more than
   * this component's: reading a script ends on the last page, so the code view
   * moves the ring to the way out there, and re-asserts it whenever a section
   * is opened rather than only after a press. Two effects both moving the ring
   * in one commit is a race decided by which of them React happens to flush
   * first, so a screen that owns the rule says so and this one stands aside.
   */
  previousRef?: MutableRefObject<HTMLDivElement | null>;
  nextRef?: MutableRefObject<HTMLDivElement | null>;
  /** Whether this row puts the ring back after a press. The default is that it does. */
  focusOnTurn?: boolean;
  /**
   * Where the ring goes when neither paging control can be pressed any more.
   *
   * A press can leave both of them disabled: the last page of a list that has
   * just shrunk under a filter is the first page as well. Without somewhere to
   * put it the ring is dropped and the window has nothing focused, so a screen
   * names the control it would rather the reader landed on, which is the way
   * out and never the screen's own decision.
   */
  fallbackRef?: MutableRefObject<HTMLDivElement | null>;
}

/**
 * One footer row, shared by every paged screen so they page the same way.
 *
 * The pager is drawn as soon as there is a list, including a list of one page,
 * where both controls are simply disabled. A pager that appears with the second
 * page is a control the reader has to find twice, and on a screen whose list
 * shrinks under a filter it is one that moves the row under their thumb.
 */
export function PagerFooter({
  testId, page, pages, onPage, disabled, trailing, preferNext, containerRef, style,
  previousRef, nextRef, focusOnTurn = true, fallbackRef,
}: Props) {
  // Where the ring goes when a page is turned.
  //
  // The two paging controls share one action group, which is a plain box while
  // only one of them can be pressed and a navigation container once both can,
  // so the first press of Next on page one rebuilds the row and unmounts the
  // button that press was made on. Which control turned the page is recorded at
  // the press and the ring is put back after it, on the control beside it when
  // that one has just disabled itself.
  //
  // Only after a press. A page number also moves when a fresh search puts the
  // list back to its first page, and focus is elsewhere by then: taking it down
  // to the pager would move the user off the screen they are using.
  const turnedRef = useRef<"next" | "previous" | null>(null);
  const ownPreviousRef = useRef<HTMLDivElement | null>(null);
  const ownNextRef = useRef<HTMLDivElement | null>(null);
  const previousPageRef = previousRef ?? ownPreviousRef;
  const nextPageRef = nextRef ?? ownNextRef;
  useEffect(() => {
    const turned = turnedRef.current;
    turnedRef.current = null;
    if (!turned || !focusOnTurn) return;
    const pressed = turned === "previous" ? previousPageRef : nextPageRef;
    const beside = turned === "previous" ? nextPageRef : previousPageRef;
    const boxes = [pressed, beside];
    if (fallbackRef) boxes.push(fallbackRef);
    focusFirstEnabled(...boxes);
  }, [page]);
  return (
    <div ref={containerRef} className={PAGER_FOOTER_CLASS} data-testid={testId} style={style}>
      <ActionGroup
        style={FOOTER_FOCUS_ROW}
        navEntryPreferPosition={preferNext ? NavEntryPositionPreferences.PREFERRED_CHILD : undefined}
      >
        <span />
        {pages > 0 ? (
          <div style={FOOTER_PAGER}>
            <div ref={previousPageRef} style={CONTENTS_ONLY}>
              <SmallButton
                disabled={disabled || page === 0}
                onClick={traceUiAction("pager.previous", () => { turnedRef.current = "previous"; onPage(Math.max(0, page - 1), "previous"); }, { screen: testId, page: page - 1 })}
              >{"\u2039 Previous"}</SmallButton>
            </div>
            <span style={FOOTER_PAGE_COUNT}>{`${page + 1} / ${pages}`}</span>
            <div ref={nextPageRef} style={CONTENTS_ONLY}>
              <SmallButton
                preferredFocus={preferNext}
                disabled={disabled || page >= pages - 1}
                onClick={traceUiAction("pager.next", () => { turnedRef.current = "next"; onPage(Math.min(pages - 1, page + 1), "next"); }, { screen: testId, page: page + 1 })}
              >{"Next \u203a"}</SmallButton>
            </div>
          </div>
        ) : <span />}
        <div style={FOOTER_TRAILING}>{trailing}</div>
      </ActionGroup>
    </div>
  );
}
