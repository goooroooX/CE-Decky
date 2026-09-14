import type { CSSProperties, ReactNode, RefObject } from "react";

import { DialogButton } from "@decky/ui";

import { ActionGroup, mediumActionStyle } from "./PanelDensity";

/**
 * How a modal's own bottom actions are sized.
 *
 * They were a full-width row: `flex: 1 1 0` each, so two buttons took half the
 * dialog apiece and one took the whole of it. That is Steam's shape for a
 * dialog whose only content is a question, and these dialogs are screens, where
 * the same treatment makes the way out as loud as the decision above it and
 * costs a handheld a band of height it does not have.
 *
 * There are two sizes and the difference between them is what the row is for.
 * A screen that ends in a decision - authorize this exact table, cancel this
 * download - carries it here, at this size. A screen that ends in a list
 * carries `PagerFooter` instead, where the way out sits beside the paging
 * controls and is the same size as they are: those are one row of navigation,
 * and a row mixing an 18 pixel line with a 16 pixel one reads as two kinds of
 * control where there is only one.
 */
export const modalActionStyle = mediumActionStyle;

/**
 * The one action on a screen that destroys something, marked as that.
 *
 * The same red the rest of the product refuses things in, and the same one a
 * refusal block is drawn with, so no screen invents a second one. Tinted rather
 * than filled: a solid red button reads as the screen's primary action, and on
 * a screen whose other press is the way out that is exactly backwards. What
 * this says is which of the two presses cannot be taken back.
 *
 * The mark is a frame around the button and not the button's own background,
 * which is the whole point of it being a component rather than a style. Steam
 * shows a focused button by painting it light and its label dark, through a
 * class; an inline background and box shadow outrank that class, so the button
 * kept its red while the label went dark on it - which on this tint is a label
 * nobody can read, on the one press in the product that cannot be taken back.
 * Marking the box behind it leaves Steam's focused appearance untouched: the
 * red is a ring around the button either way, and the label is Steam's to
 * colour.
 */
export function DestructiveAction(
  { children, disabled, onClick }: { children: ReactNode; disabled?: boolean; onClick: () => void },
) {
  return (
    <div style={destructiveFrameStyle}>
      {/* Never hand Steam's controller click event to a workflow callback, for
          the same reason `SmallButton` does not: several of them forward their
          argument, and this one's argument would be an event on its way to
          Steam's own game list. */}
      <DialogButton style={mediumActionStyle} disabled={disabled} onClick={() => onClick()}>{children}</DialogButton>
    </div>
  );
}

const destructiveFrameStyle: CSSProperties = {
  display: "inline-flex",
  padding: 2,
  borderRadius: 4,
  background: "hsla(9, 74%, 40%, 0.30)",
  boxShadow: "inset 0 0 0 1px hsla(9, 74%, 62%, 0.75)",
};

/**
 * A modal's bottom actions, as one horizontal controller group.
 *
 * Steam derives which control is beside which from the geometry, but two
 * siblings in a plain box are two separate steps, so the stick moved down from
 * one to the other rather than across: on the review screen "Use this table"
 * and "Cancel" sit side by side and behaved as if they were stacked. The group
 * `ActionGroup` builds is the same one the panel's own action rows use, and it
 * carries the rule that matters here too: a lone action is not wrapped, because
 * a `Focusable` around one button is a focus target whose activation does
 * nothing, and a group is kept across a press that disables one of its members.
 */
export function ModalActions(
  { children, containerRef }: { children: ReactNode; containerRef?: RefObject<HTMLDivElement | null> },
) {
  return (
    <div ref={containerRef} data-testid="modal-actions" style={{ padding: "0 16px 8px" }}>
      <ActionGroup style={{ justifyContent: "flex-end", gap: 8 }}>{children}</ActionGroup>
    </div>
  );
}
