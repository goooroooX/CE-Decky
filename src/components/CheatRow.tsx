import { Field, Toggle } from "@decky/ui";
import type { CSSProperties, ReactNode } from "react";
import { ActionGroup, CHEAT_ROW_CLASS, FOCUS_SCROLL_CLASS, FocusScrollText, OPEN_ROW_CLASS, REVEAL_CLASS, WrapText } from "./PanelDensity";

interface Props {
  /** Leaf name of the control; the part a user actually recognises. */
  label: string;
  /** Compact context line: group breadcrumb, current value, pin state. */
  summary?: string;
  /** `null` when this exact control exposes no active state to toggle. */
  active: boolean | null;
  disabled?: boolean;
  onActiveChange?: (active: boolean) => void;
  /** Extra inline controls placed right of the toggle, such as a More button. */
  actions?: ReactNode;
  /** Revealed detail rows rendered under the compact header row. */
  body?: ReactNode;
  highlighted?: boolean;
  /**
   * `modal` packs many records into one dialog, so each becomes its own tinted
   * block. `panel` is one item in the quick-access column and must line up with
   * every other Decky row, so it keeps the standard separator and adds no block
   * of its own. Neither uses Steam's `compact` field padding: in the
   * quick-access column that is a full-bleed variant that drops the inline
   * padding and pulls the row outside the section.
   */
  variant?: "modal" | "panel";
  testId?: string;
}

/**
 * One cheat as a single controller-navigable row.
 *
 * The viewport is the constraint: a record that expands into separate title,
 * Active, Value and Pinned rows fits about three per screen and pushes the
 * modal header off the display. Keeping the name, its context and the Active
 * toggle on one row is what makes a real 17-control table usable.
 *
 * A closed row is exactly two lines, whichever variant it is and whatever it is
 * called. A Cheat Engine record is named by whoever wrote the table, and the
 * field they name it in is the same one they write their notes in, so a real
 * table's record names run to a paragraph: "Reduction % (100 = immune, 0 = no
 * reduction)" is one of the shorter ones. In the modal those wrapped, so one
 * record could be five lines tall, a page of six could not be read without
 * scrolling the window, and how much of the screen a page took depended on
 * which records happened to be on it. The panel variant had the reveal from the
 * start; this is the same treatment, and the name and the note now scroll
 * themselves under the ring instead of growing the row.
 *
 * An open row is the exception, and deliberately: `More` is the press that says
 * show me the whole of this, so its block wraps rather than scrolls, and
 * nothing in it is cut.
 */
export function CheatRow({
  label, summary, active, disabled, onActiveChange, actions, body, highlighted, variant = "modal", testId,
}: Props) {
  const panel = variant === "panel";
  // A row showing its detail block is a row whose whole text is on screen: it
  // wraps, the same way an opened `PanelRow` does, and it is the one row on the
  // list allowed to be as tall as its own name.
  const open = Boolean(body);
  const line = (text: ReactNode) => open
    ? <WrapText>{text}</WrapText>
    : <FocusScrollText paced>{text}</FocusScrollText>;
  // Steam sizes its gamepad toggle at a fixed 38x22 with an absolutely
  // positioned 22px knob that translates 16px when on. A flex row's default
  // `flex-shrink: 1` narrows that box while the knob keeps its geometry, so the
  // switch is painted clipped and spills over whatever follows it.
  const toggle = active === null ? null : (() => {
    const control = <Toggle value={active} disabled={disabled} onChange={(checked) => onActiveChange?.(checked)} />;
    // On the panel the switch competes with the cheat's own name for a single
    // 300px column, so it is scaled down from its right edge: Steam's knob is
    // absolutely positioned inside a fixed 38x22 box, and scaling the box is
    // the only way to shrink it without the knob keeping its own geometry.
    return (
      <div style={panel ? panelToggleBoxStyle : toggleBoxStyle}>
        {panel ? <div style={panelToggleScaleStyle}>{control}</div> : control}
      </div>
    );
  })();
  return (
    <div
      style={panel ? undefined : blockStyle(Boolean(highlighted))}
      className={[
        FOCUS_SCROLL_CLASS,
        panel ? null : CHEAT_ROW_CLASS,
        body ? OPEN_ROW_CLASS : null,
      ].filter(Boolean).join(" ")}
      data-testid={testId}
    >
      <Field
        label={panel ? <FocusScrollText>{label}</FocusScrollText> : line(label)}
        description={summary ? panel ? <FocusScrollText>{summary}</FocusScrollText> : line(summary) : undefined}
        bottomSeparator={panel ? "standard" : "none"}
        childrenLayout="inline"
        childrenContainerWidth="min"
        verticalAlignment="center"
      >
        {/* A `Focusable` is itself a focus target, so the group counts enabled
            controls rather than merely rendered children. A disabled More
            beside an enabled toggle is still the one-control case where Steam
            otherwise lands on the wrapper and swallows A. */}
        {actions && toggle ? (
          <ActionGroup style={{ gap: 10 }}>
            {toggle}
            {actions}
          </ActionGroup>
        ) : toggle ?? actions}
      </Field>
      {body ? <div className={REVEAL_CLASS}>{body}</div> : null}
    </div>
  );
}

export const toggleBoxStyle: CSSProperties = {
  flex: "0 0 auto",
  display: "flex",
  alignItems: "center",
  minWidth: 38,
};

/** The same switch, narrowed to the width its scaled-down box actually needs. */
const panelToggleBoxStyle: CSSProperties = {
  flex: "0 0 auto",
  display: "flex",
  alignItems: "center",
  justifyContent: "flex-end",
  width: 32,
  minWidth: 32,
};

const panelToggleScaleStyle: CSSProperties = {
  // Steam's box cannot shrink without clipping its knob, so it keeps its 38px
  // and the scale - anchored to the right edge the row aligns on - brings it
  // back inside the narrower column.
  flex: "0 0 38px",
  transform: "scale(0.84)",
  transformOrigin: "100% 50%",
};

function blockStyle(highlighted: boolean): CSSProperties {
  return {
    width: "100%",
    borderRadius: 4,
    // Read from the panel rather than written here, so a short screen can close
    // the gap between rows without this file knowing which screen it is on. An
    // inline length cannot be overridden by a stylesheet; a variable can.
    marginBottom: "var(--ce-cheat-row-gap, 4px)",
    background: highlighted ? "rgba(255, 255, 255, 0.09)" : "rgba(255, 255, 255, 0.04)",
    boxShadow: highlighted ? "inset 0 0 0 1px rgba(255, 255, 255, 0.18)" : undefined,
  };
}

export const cheatRowActionStyle: CSSProperties = {
  minWidth: 0,
  padding: "4px 10px",
  fontSize: 12,
  lineHeight: "16px",
};
