import { traceUiAction } from "../uiActions";
import { DialogButton, Field, Focusable, PanelSectionRow, TextField, gamepadDialogClasses, type NavEntryPositionPreferences } from "@decky/ui";
import { Children, isValidElement, useLayoutEffect, useRef, useState, type ChangeEventHandler, type CSSProperties, type ReactNode } from "react";

import { SHORT_SCREEN_MAX_HEIGHT, isShortScreen, rowsOffTheBar } from "../viewport";

export const DENSE_PANEL_CLASS = "ce-decky-dense";
const ELLIPSIS_CLASS = "ce-decky-ellipsis";
const WRAP_CLASS = "ce-decky-wrap";
const HEADING_CLASS = "ce-decky-heading";
const HEADING_TRAILING_CLASS = "ce-decky-heading-trailing";
const MARQUEE_CLASS = "ce-decky-marquee";
const MARQUEE_ANIMATION = "ce-decky-marquee-shift";
/** How long a line that is actually cut takes to show its end. */
const REVEAL_SECONDS = 7;
/**
 * How fast a paced line is allowed to travel, in CSS pixels per second.
 *
 * One duration for every distance is one speed per line, and the longest line
 * is the fastest: a table's recorded reason overflows a 460 pixel sheet by well
 * over a thousand pixels, which the flat seven seconds turned into roughly two
 * hundred pixels a second. That is a line moving past the reader rather than a
 * line being read. Halved once on the device and then taken down another fifth,
 * both times because it was still faster than it reads. A paced line takes as
 * long as its own length needs, so every one of them moves at this speed and
 * the longest is no longer the fastest.
 *
 * The seven seconds stay the floor, so a line that only just overflows is not
 * made brisk by the same rule: it already had the whole window to reveal a
 * couple of words and still does. The panel's own pinned cheats are unpaced and
 * unchanged - two short lines in a 300 pixel column never reach this speed.
 */
const MARQUEE_PACE_PX_PER_SECOND = 80;
/**
 * The longest a paced reveal may take, whatever the line's own length says.
 *
 * A recorded reason is bounded at a kilobyte, which is thousands of pixels and
 * over a minute of travel at the speed above. That is not a slow reveal, it is a
 * line that reads as not moving at all, and no marquee is the way to read a
 * paragraph anyway: a row that long is opened instead, which wraps it. The cap
 * keeps the extreme case visibly alive rather than pretending it is readable.
 */
const MARQUEE_PACE_MAX_SECONDS = 30;
/** The measured overflow of one line, published to the animation as a length. */
const MARQUEE_SHIFT_VAR = "--ce-marquee-shift";
/** How long this line's own travel takes, for a paced one. */
const MARQUEE_SECONDS_VAR = "--ce-marquee-seconds";
/** Marks the row whose focus drives its own marquee. */
export const FOCUS_SCROLL_CLASS = "ce-decky-focusscroll";
/**
 * Marks a row whose control sits under its label rather than beside it.
 *
 * The dense field padding is four pixels top and bottom, which is right for a
 * control the row centres vertically and cramped for one the row stacks: the
 * control ends where the block does, with the label's own breathing room above
 * it and none underneath. The game picker's dropdown sat on the bottom edge of
 * its own block because of it.
 */
export const BELOW_FIELD_CLASS = "ce-decky-fieldbelow";
const HEADER_ROW_CLASS = "ce-decky-rowhead";
/**
 * A row that belongs to something other than the list it is being shown in.
 *
 * Darker ground and nothing else: no accent, which is what marks the row that
 * heads a list, and no extra gap, because these come in runs rather than one at
 * a time. It answers which group a row is in before the row is read, which a
 * few words at the end of one label cannot do on a page that opens in the
 * middle of the second group.
 */
const ASIDE_ROW_CLASS = "ce-decky-rowaside";
/**
 * A row that belongs to the game the reader has in front of them.
 *
 * Marked rather than merely left alone. Manage lists this game's tables above
 * every other table on the device, and the only thing separating them was that
 * the others were set a step darker - so this game's own read as the plain
 * ones and the list read as one run of grey.
 *
 * Ground only, and grey-blue rather than grey. Three versions were drawn on the
 * screen before this one: the accent edge the row heading a list carries, which
 * made an ordinary row read as a second heading in the middle of the list; a
 * wash of that accent, which reads as one tinted block rather than as rows; and
 * a step lighter in plain grey, which is invisible next to rows a step darker in
 * plain grey, because a list read in one column reads the step and not which way
 * it went. A hue is the dimension nothing else in this list is using.
 */
const OWN_ROW_CLASS = "ce-decky-rowown";
/**
 * One cheat's closed block in the Configure cheats list.
 *
 * Marks the row so its header keeps one height whatever it is called. Both
 * lines are clamped to one line each by the marquee, which settles the tall
 * case; this settles the short one, so a record with no context line is the
 * same height as a record with one and a page of them is a fixed number of
 * pixels rather than a number that depends on which records are on it.
 */
export const CHEAT_ROW_CLASS = "ce-decky-cheatrow";
/**
 * The row a paged screen ends with, for the one rule that has to reach it.
 *
 * It is drawn outside the dense wrapper, because what a screen puts below its
 * list is not part of the list, so the scoped rules above cannot see it. This
 * class is how a short screen reaches it anyway, and it is the only thing here
 * that is not scoped.
 */
export const PAGER_FOOTER_CLASS = "ce-decky-pager";

/**
 * Marks a screen whose rows give their controls less room than the default.
 *
 * Everything a control takes on a row comes out of the name beside it, and on
 * one screen those names are what the reader is there to compare. This is not
 * a rule for every dense row: the quick access panel carries the same controls
 * in a far narrower column, and tightening it there moved every button on the
 * panel. A screen opts in.
 */
const TIGHT_ROWS_CLASS = "ce-decky-tightrows";

/** The detail block a row reveals under itself. */
export const REVEAL_CLASS = "ce-decky-reveal";
const NOTE_CLASS = "ce-decky-note";
/**
 * The one press drawn in the mascot's orange, and how it keeps Steam's focus.
 *
 * It was an inline background on the button, which outranks the class Steam
 * paints a focused button with: the control stayed orange under the ring, so
 * on a handheld there was no way to see where the ring was. The colour is a
 * rule that stops applying while anything inside is focused, which hands the
 * focused appearance back to Steam without knowing any of its class names.
 *
 * The class always goes on a box around the control rather than on the control
 * itself. Steam's own button is a component out of its shipped bundle, and
 * whether it merges a className with its own or replaces them with it is not
 * something this repository can read; a box is the same in either case, and
 * `CONTENTS_ONLY` keeps it out of the layout where the control is already
 * placed by a row.
 */
export const UPDATE_ACTION_CLASS = "ce-decky-update";
/** Marks a row that is currently showing its revealed block. */
export const OPEN_ROW_CLASS = "ce-decky-open";

/**
 * Whether a row draws a separator under itself.
 *
 * Steam draws one edge to edge, which is the same shape as the rule beside a
 * section heading and turns a column of rows into a stack of equally weighted
 * lines, so CE Decky pulled it in to 15% on either side. What that leaves on a
 * dense panel is a short stripe under most rows, floating clear of both edges
 * and of the text it belongs to, which reads as a stray mark rather than as a
 * division. Row spacing and the section rules already separate the panel, so
 * this is off while that is judged without them.
 *
 * Set it back to `true` for the inset stripe. The section rule beside a heading
 * is a different rule and is unaffected either way.
 */
const ROW_SEPARATORS = false;

/**
 * Repeat a class so a rule outranks Steam's own multi-class selectors.
 *
 * Steam styles a field's padding through `.Field.<context>.<padding>`, which is
 * three classes; a plain `.ce-decky-dense .Field` is two and loses. Repeating
 * the class raises specificity deterministically instead of relying on
 * `!important` or on this stylesheet happening to come last.
 */
function outrank(className: string, times = 3): string {
  return Array.from({ length: times }, () => `.${className}`).join("");
}

/**
 * Scoped CSS that tightens Steam's own quick-access metrics.
 *
 * Steam sizes a quick-access row for a handheld read at arm's length: a 16px
 * label, a 12px description, 10px of padding above and below, a 6px row margin
 * and 24px between sections. Nine such rows put the cheats CE Decky exists to
 * control below the fold. The exact class names come from Decky's class finders
 * at runtime rather than being guessed, and every rule is scoped to this
 * plugin's own wrapper so no other panel changes.
 *
 * Padding is deliberately not touched horizontally: Steam's `compact` field
 * padding in the quick-access column is a full-bleed variant that removes the
 * inline padding and pulls the row 16px outside the section, which hangs labels
 * off the left and pushes controls past the right edge of the panel.
 */
function densityCss(): string | null {
  const field = gamepadDialogClasses?.Field;
  const label = gamepadDialogClasses?.FieldLabel;
  const description = gamepadDialogClasses?.FieldDescription;
  const leftColumn = gamepadDialogClasses?.FieldLeftColumn;
  const separators = [gamepadDialogClasses?.WithBottomSeparatorStandard, gamepadDialogClasses?.WithBottomSeparatorThick]
    .filter((name): name is string => Boolean(name));
  if (!field || !label || !description) return null;
  const scope = `.${DENSE_PANEL_CLASS}`;
  // Every value the section heading and the accent beside a header row are made
  // of, declared once on the panel itself. They were literals spread through
  // the rules below, so changing how a heading looks meant finding each of them
  // and keeping them in step; now one line here changes every screen, and a
  // single screen can override any of them on its own wrapper.
  const tokens = [
    "--ce-heading-font-size: 12px",
    "--ce-heading-font-weight: 700",
    "--ce-heading-line-height: 16px",
    "--ce-heading-letter-spacing: 0.5px",
    "--ce-heading-text-transform: uppercase",
    "--ce-heading-color: hsla(0, 0%, 100%, 0.7)",
    "--ce-heading-gap: 8px",
    // The gap under a heading is part of the heading: without it the first row
    // of the list is drawn against the rule, and the two read as one block
    // instead of as a caption over what it names. Steam's own is 8px, which is
    // more than this panel can spend per heading; this is what separates them.
    "--ce-heading-padding: 0 0 6px",
    "--ce-heading-rule-height: 3px",
    "--ce-heading-rule-radius: 2px",
    "--ce-heading-rule-color: hsla(0, 0%, 100%, 0.38)",
    // The accent CE Decky marks its own emphasis with, shared by the row that
    // heads a list and available to anything else that needs it.
    "--ce-accent: hsla(203, 89%, 66%, 0.85)",
    // The one press on this panel that is not about the game in front of the
    // user, and the only thing here drawn in the mascot's own orange. Sampled
    // from `docs/assets/hexpaw.png` rather than chosen beside it, so the panel
    // carries one orange instead of two that nearly match.
    "--ce-update-accent: #fd5605",
    "--ce-update-accent-text: #ffffff",
    // The row that heads a list is the list's first row of data, not the
    // caption above it: it is set a step darker than the panel and holds the
    // rows that follow off itself.
    "--ce-header-row-background: hsla(0, 0%, 0%, 0.28)",
    "--ce-header-row-radius: 4px",
    "--ce-header-row-gap: 6px",
    // The ground under a row from another group. Between the panel and the row
    // that heads a list, so a run of them reads as a block set back rather than
    // as a stack of headings.
    "--ce-aside-row-background: hsla(0, 0%, 0%, 0.16)",
    // And the ground under a row that is this game's own: grey-blue, desaturated
    // far enough to sit beside grey and hued far enough to be told from it.
    // `OWN_ROW_CLASS` carries why it is a hue rather than another step of grey.
    "--ce-own-row-background: hsla(205, 38%, 58%, 0.20)",
  ].join("; ");
  // Every direct child of this wrapper is one of CE Decky's own panel sections.
  // Steam's section class carries `margin: 0 0 24px` and its name is a bare
  // content hash on the shipped client - `quickAccessControlsClasses` resolves
  // it through a heuristic over Steam's own modules that can simply not match -
  // so the spacing is reached structurally instead of by name.
  const sectionSelector = `${outrank(DENSE_PANEL_CLASS)} > div`;
  return [
    `${scope} { ${tokens}; }`,
    `${scope} ${outrank(field)} { margin-top: 1px; padding-top: 4px; padding-bottom: 4px; }`,
    // One screen's rows, tightened. The quick access panel is not this screen:
    // its rows carry the same controls in a much narrower column, and pulling
    // them toward the edge there moved every button on the panel. So the
    // tightening is a wrapper a screen opts into rather than a rule every dense
    // row obeys, and it is expressed as the two lengths the rows read, so the
    // inline styles that use them need no override.
    `${scope}.${TIGHT_ROWS_CLASS} { --ce-row-trailing-gap: 6px; --ce-row-action-gap: 4px; --ce-row-mark-width: 16px; }`,
    `${scope}.${TIGHT_ROWS_CLASS} ${outrank(field)} { padding-right: 8px; }`,
    // Air under a stacked control, which the four pixels above do not give it.
    `${scope} .${BELOW_FIELD_CLASS} ${outrank(field)} { padding-bottom: 10px; }`,
    `${scope} .${label} { font-size: 14px; line-height: 17px; }`,
    `${scope} .${description} { font-size: 11px; line-height: 14px; margin-top: 1px; }`,
    // A row's own controls cannot shrink, so unless the label column may, a long
    // label - a Cheat Engine version, a game name, a table filename - widens the
    // whole row and pushes its buttons past the panel instead of being cut short
    // by the ellipsis the label already asks for.
    leftColumn ? `${scope} .${leftColumn} { min-width: 0; }` : null,
    // A table filename or a deep cheat path must cost one line, never three.
    `${scope} .${ELLIPSIS_CLASS} { display: block; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }`,
    // An installation path has no spaces to break at, so it needs an explicit
    // rule or it runs straight past the right edge once the row expands.
    `${scope} .${WRAP_CLASS} { display: block; min-width: 0; overflow-wrap: anywhere; word-break: break-word; }`,
    // A pinned cheat costs exactly two lines. A Cheat Engine record is named by
    // whoever wrote the table - "Reduction % (100 = immune, 0 = no reduction)"
    // is one real example - and letting that wrap took four lines out of a
    // single-column panel that has to hold every other cheat too.
    //
    // A pinned cheat's name is often longer than the row, so the focused row
    // scrolls its own text far enough to show the end of it. A name that fits
    // does not move at all.
    //
    // Which of the two it is used to be arranged rather than measured: the
    // shift was `left: 100%` (the container's width) plus `translateX(-100%)`
    // (the track's own), a difference that is the overflow when there is one
    // and zero when there is not, so no width had to be read. What that costs
    // is that the animation's endpoint is a live function of two box widths,
    // re-resolved as they change, and one of the things that changes them is
    // the clamp this rule takes off at the moment the animation starts. An
    // endpoint that moves under an `alternate infinite` animation is a jitter,
    // which is what the device showed: both lines of a focused pinned cheat
    // twitching, including on rows whose text fits and has nothing to reveal.
    //
    // `FocusScrollText` measures the overflow once and publishes it as a
    // length. The travel is that one number now, and a line with nothing to
    // reveal carries no animation at all rather than one that resolves to zero.
    `${scope} .${MARQUEE_CLASS} { display: block; min-width: 0; overflow: hidden; white-space: nowrap; font: inherit; line-height: inherit; }`,
    `${scope} .${MARQUEE_CLASS} > span { display: inline-block; position: relative; left: 0; vertical-align: top; min-width: 100%; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }`,
    // Steam moves real DOM focus and adds its own `gpfocuswithin`; either is
    // enough, and neither costs anything when the other is what fires. The
    // delay is what keeps a row the stick merely passes through still.
    `${scope} .${FOCUS_SCROLL_CLASS}:focus-within .${MARQUEE_CLASS}[data-clipped] > span,`
      + ` ${scope} .${FOCUS_SCROLL_CLASS}.gpfocuswithin .${MARQUEE_CLASS}[data-clipped] > span`
      + ` { max-width: none; overflow: visible;`
      + ` animation: ${MARQUEE_ANIMATION} var(${MARQUEE_SECONDS_VAR}, ${REVEAL_SECONDS}s) ease-in-out 0.7s infinite alternate; }`,
    // The same rule for the row whose focus Steam marks on a control inside it
    // rather than on the row. A row here is a plain element and Steam adds
    // `gpfocuswithin` to its own `Focusable`, which on these lists is the group
    // holding the row's buttons: that group is inside the row and beside the
    // text, so neither selector above reaches it. Its own rule, because a
    // parser that does not know `:has()` drops the rule it is written in, and
    // dropping this one costs nothing while dropping the pair above would cost
    // the panel's pinned cheats their reveal as well.
    `${scope} .${FOCUS_SCROLL_CLASS}:has(.gpfocuswithin) .${MARQUEE_CLASS}[data-clipped] > span`
      + ` { max-width: none; overflow: visible;`
      + ` animation: ${MARQUEE_ANIMATION} var(${MARQUEE_SECONDS_VAR}, ${REVEAL_SECONDS}s) ease-in-out 0.7s infinite alternate; }`,
    `@keyframes ${MARQUEE_ANIMATION} { from { transform: translateX(0); } to { transform: translateX(var(${MARQUEE_SHIFT_VAR}, 0px)); } }`,
    `@media (prefers-reduced-motion: reduce) { ${scope} .${FOCUS_SCROLL_CLASS}:focus-within .${MARQUEE_CLASS} > span,`
      + ` ${scope} .${FOCUS_SCROLL_CLASS}.gpfocuswithin .${MARQUEE_CLASS} > span { animation: none; } }`,
    `@media (prefers-reduced-motion: reduce) { ${scope} .${FOCUS_SCROLL_CLASS}:has(.gpfocuswithin) .${MARQUEE_CLASS} > span`
      + ` { animation: none; } }`,
    // Reduced motion asks for no movement, not for less to read. Taking the
    // animation away on its own left the line clipped and one line tall, so the
    // half of a filename past the cut, and the recorded reason a table did not
    // work, could only ever show their beginning. These rows carry controls, so
    // `PanelRow` gives them no press-to-wrap focusable of their own either:
    // there was no second route to the rest of the text at all.
    //
    // So the focused line that is cut wraps instead. It is the same reveal, in
    // the one form that is not motion: the row grows while it holds focus and
    // shrinks back when it loses it, and every word of the value is readable in
    // between.
    `@media (prefers-reduced-motion: reduce) { ${scope} .${FOCUS_SCROLL_CLASS}:focus-within .${MARQUEE_CLASS}[data-clipped],`
      + ` ${scope} .${FOCUS_SCROLL_CLASS}.gpfocuswithin .${MARQUEE_CLASS}[data-clipped]`
      + ` { overflow: visible; white-space: normal; } }`,
    `@media (prefers-reduced-motion: reduce) { ${scope} .${FOCUS_SCROLL_CLASS}:focus-within .${MARQUEE_CLASS}[data-clipped] > span,`
      + ` ${scope} .${FOCUS_SCROLL_CLASS}.gpfocuswithin .${MARQUEE_CLASS}[data-clipped] > span`
      + ` { max-width: none; overflow: visible; white-space: normal; text-overflow: clip; transform: none; } }`,
    // The row whose focus Steam marks on a control inside it, in its own rule
    // for the same reason the animated pair above splits: a parser that does
    // not know `:has()` drops the rule it is written in, and this is the one
    // that can be lost without costing the others.
    `@media (prefers-reduced-motion: reduce) { ${scope} .${FOCUS_SCROLL_CLASS}:has(.gpfocuswithin) .${MARQUEE_CLASS}[data-clipped]`
      + ` { overflow: visible; white-space: normal; } }`,
    `@media (prefers-reduced-motion: reduce) { ${scope} .${FOCUS_SCROLL_CLASS}:has(.gpfocuswithin) .${MARQUEE_CLASS}[data-clipped] > span`
      + ` { max-width: none; overflow: visible; white-space: normal; text-overflow: clip; transform: none; } }`,
    // One closed cheat is the label's line, the description's line and the
    // field's own padding, and it stays that whether it has a description or
    // not. The number is the sum of everything above it: the 17 and 14 pixel
    // line heights, the 1 pixel between them, and the field's own 4 above and
    // below - so a change to any of those is a change here. A record at the top
    // level of a table has no group to name and no value shown, which leaves it
    // with no second line at all, and it is the reason this exists.
    // An open row is exempt: `More` is a press that says show me the whole of
    // this, and its block is as tall as what it holds.
    `${scope} .${CHEAT_ROW_CLASS}:not(.${OPEN_ROW_CLASS}) ${outrank(field)} { min-height: 40px; box-sizing: border-box; }`,
    // The live-state line introduces the cheats under it rather than being one
    // more row among them, so it carries the section's own tint and accent.
    // Without that the panel is an undifferentiated column and the first pinned
    // cheat reads as the status row's continuation.
    `${scope} ${outrank(HEADER_ROW_CLASS)} .${field} { background: var(--ce-header-row-background);`
      + ` border-radius: var(--ce-header-row-radius); margin-bottom: var(--ce-header-row-gap);`
      + ` box-shadow: inset 2px 0 0 var(--ce-accent); }`,
    `${scope} ${outrank(ASIDE_ROW_CLASS)} .${field} { background: var(--ce-aside-row-background);`
      + ` border-radius: var(--ce-header-row-radius); }`,
    // Ground only. The accent edge is what the row heading a list is marked
    // with, and an ordinary row wearing it reads as a second heading.
    `${scope} ${outrank(OWN_ROW_CLASS)} .${field} { background: var(--ce-own-row-background);`
      + ` border-radius: var(--ce-header-row-radius); }`,
    // What a row reveals under itself belongs to that row, so it is indented
    // behind a rule and set one step down in the type scale. Steam's own text
    // input is the reason this needs rules at all: every `Field` carries
    // `padding: 0 20px`, and `DialogInputLabelGroup` carries none, so the value
    // editor ran the full width of the block while the labels around it stayed
    // inset - the editor read as a separate full-bleed panel rather than as
    // part of the cheat above it.
    // What made an open row read as one card was never a border, it was the
    // step in tone: Steam paints the focused field lighter, so a focused row
    // had a lit heading over a darker body while an unfocused one was flat -
    // its heading is opaque and its revealed rows show the block's tint, and
    // the two land close enough to cancel each other out. Both surfaces are put
    // on the block's own tint and the heading alone is lifted, so the row reads
    // the same whether or not the controller is on it. Focus still wins, and a
    // border would not survive here anyway: the opaque field rectangles do not
    // line up with the block, so one showed as stray strips on three sides.
    `${scope} ${outrank(OPEN_ROW_CLASS)} > ${outrank(field, 4)}:not(.gpfocus):not(.gpfocuswithin)`
      + ` { background: hsla(0, 0%, 100%, 0.06); }`,
    `${scope} .${REVEAL_CLASS} { padding: 0 0 3px 12px; }`,
    // Steam paints a field opaque, and those rectangles do not line up with the
    // row's own block, so anything drawn around them showed as a stray strip of
    // block tint down the left, right and bottom - and a border of this block's
    // own added a second line beside the block's inset outline. The revealed
    // rows are made one continuous surface with the row instead, and only the
    // indent says they belong to it. The focused field keeps Steam's own
    // highlight: that rectangle is the controller's position, not decoration.
    `${scope} .${REVEAL_CLASS} ${outrank(field, 4)} { padding-top: 3px; padding-bottom: 3px; margin-top: 0; }`,
    `${scope} .${REVEAL_CLASS} ${outrank(field, 4)}:not(.gpfocus):not(.gpfocuswithin) { background: transparent; }`,
    `${scope} .${REVEAL_CLASS} .${label} { font-size: 13px; line-height: 16px; }`,
    `${scope} .${REVEAL_CLASS} .${description} { font-size: 11px; line-height: 14px; }`,
    // Steam's text input is `DialogInputLabelGroup > label > (DialogLabel +
    // DialogInput_Wrapper > DialogInput)`. Those names are Steam's own stable
    // ones, not content hashes, and every rule here is scoped to this block, so
    // a renamed class costs the alignment and nothing else.
    // Steam's input group also carries a 22px bottom margin, which is right
    // for a dialog full of them and is a hole when one sits between two rows.
    `${scope} .${REVEAL_CLASS} .DialogInputLabelGroup { padding: 2px 20px 5px; margin-bottom: 0; }`,
    `${scope} .${REVEAL_CLASS} .DialogLabel { font-size: 11px; line-height: 14px; opacity: 0.75; }`,
    `${scope} .${REVEAL_CLASS} .DialogInput { height: 32px; min-height: 32px; font-size: 13px; padding: 0 10px; }`,
    // An aside about what Apply will do is not a setting, so it is not given a
    // label and a control's worth of height. It is inset like the fields around
    // it and set at description weight.
    `${scope} .${NOTE_CLASS} { padding: 2px 20px 5px; font-size: 11px; line-height: 15px; color: hsla(0, 0%, 100%, 0.62); }`,
    `${scope} .${UPDATE_ACTION_CLASS}:not(:focus-within) button`
      + ` { background: var(--ce-update-accent); color: var(--ce-update-accent-text); }`,
    `${sectionSelector} { margin-bottom: 6px; }`,
    // Steam renders a section heading at 16px/22px with 8px beneath it; five
    // headings on one diagnostics screen cost more than the rows they label.
    `${scope} .${HEADING_CLASS} { display: flex; align-items: center; gap: var(--ce-heading-gap); padding: var(--ce-heading-padding);`
      + ` font-size: var(--ce-heading-font-size); font-weight: var(--ce-heading-font-weight); line-height: var(--ce-heading-line-height);`
      + ` letter-spacing: var(--ce-heading-letter-spacing); text-transform: var(--ce-heading-text-transform); color: var(--ce-heading-color); }`,
    // What follows the text sits between it and the rule, so a heading that
    // carries a spinner keeps the rule spanning whatever is left.
    `${scope} .${HEADING_CLASS} > .${HEADING_TRAILING_CLASS} { flex: 0 0 auto; display: inline-flex; align-items: center; }`,
    // The rule that separates one section from the next runs beside the heading
    // rather than under it, so the split costs no height at all - which matters
    // once a few pinned cheats are competing for the same column. It spans the
    // full width and is deliberately heavier than a row separator.
    `${scope} .${HEADING_CLASS}::after { content: ""; flex: 1 1 auto; order: 1; height: var(--ce-heading-rule-height); border-radius: var(--ce-heading-rule-radius); background: var(--ce-heading-rule-color); }`,
    // Steam draws a row's own separator edge to edge, which is the same shape as
    // the section rule above and turns a column of rows into a stack of equally
    // weighted lines. Inside a section a separator only has to say "next row",
    // so it stops short of both edges, and the division between
    // sections is the only line that reaches the edges. Steam's own rule is four
    // classes deep, so this one is deliberately deeper.
    ...separators.map((name) => ROW_SEPARATORS
      ? `${outrank(DENSE_PANEL_CLASS, 4)} .${name}::after { left: 15%; right: 15%; margin-inline-start: 0; }`
      // Steam paints the line on the row's own pseudo-element, so hiding that
      // is the whole of it: the row keeps its height and its separator class,
      // and nothing else on the panel has to know the line is gone.
      : `${outrank(DENSE_PANEL_CLASS, 4)} .${name}::after { display: none; }`,
    ),
    // What a short screen does differently, in a media query rather than in a
    // branch: the rules belong to the page this plugin's DOM is rendered in,
    // and that page is the one whose height decides. The code runs in Steam's
    // shared context, whose window is one pixel tall, so a rule that asked the
    // window would ask the wrong one; a media query asks the right one by
    // construction, on every screen, without a re-render.
    //
    // Steam draws a text field's label above its input, which is two lines for
    // one control. On a 534 pixel page a search screen spent 371 of it on
    // chrome, and this is part of what that was. The label goes beside the
    // input there, where it costs the field's own height and nothing more.
    `@media (max-height: ${SHORT_SCREEN_MAX_HEIGHT}px) {`
      + ` ${scope} .DialogInputLabelGroup > label { display: flex; align-items: center; gap: 10px; }`
      + ` ${scope} .DialogInputLabelGroup .DialogLabel { flex: 0 0 auto; margin: 0; white-space: nowrap; }`
      + ` ${scope} .DialogInputLabelGroup .DialogInput_Wrapper { flex: 1 1 auto; min-width: 0; }`
      + ` ${scope} .DialogInputLabelGroup { padding-top: 2px; padding-bottom: 4px; margin-bottom: 0; }`
      // And a row gives up two pixels at each end. Four rows of results is what
      // a Steam Deck fits, and the fifth is inside ten pixels of arriving; the
      // page that divides the room by a row measures the row rather than
      // reading a constant, so this cannot put the arithmetic out.
      + ` ${scope} ${outrank(field, 4)} { padding-top: 2px; padding-bottom: 2px; }`
      // Except under a control that is stacked below its own label, which is
      // the one shape that needs the air: the block such a control opens is as
      // wide as the row and lands directly under it, so with the row's padding
      // gone it sits flat on whatever follows. The rule above outranks the
      // ordinary one for this class by two levels of specificity, so it took
      // that air away on exactly the screens with the least of it, and the
      // game picker and the Approve screen both lost it without either file
      // changing. This puts it back, outranking the rule that removed it.
      + ` ${scope} .${BELOW_FIELD_CLASS} ${outrank(field, 5)} { padding-bottom: 10px; }`
      // And then the rest of what a screen is made of, because giving a page a
      // row fewer is not fitting it. Every length below was read off a Steam
      // Deck with `scripts/target_panel_read.py --metrics` and is the same
      // thing said in a smaller hand, not a different screen: the heading, the
      // two lines a row is, the gap between one cheat and the next, and the
      // height Steam's own controls take when nobody tells them otherwise.
      + ` ${outrank(DENSE_PANEL_CLASS)} {`
        + " --ce-heading-line-height: 14px;"
        + " --ce-heading-padding: 0 0 4px;"
        + " --ce-header-row-gap: 4px;"
        + " --ce-cheat-row-gap: 3px;"
      + " }"
      + ` ${scope} ${outrank(label, 2)} { font-size: 13px; line-height: 16px; }`
      + ` ${scope} ${outrank(description, 2)} { font-size: 10px; line-height: 13px; }`
      + ` ${scope} .${CHEAT_ROW_CLASS}:not(.${OPEN_ROW_CLASS}) ${outrank(field, 4)} { min-height: 34px; }`
      // Steam's own controls size themselves for a television. A button or an
      // input this plugin drew carries its own padding inline, and an inline
      // length outranks a stylesheet, so this reaches exactly the ones nobody
      // here has sized: the dropdown a section is chosen from, the box a filter
      // is typed into. Padding only, and no floor under it: a floor would
      // reach this plugin's own small buttons as well, which have no inline
      // height to outrank it with, and would make the rows holding them taller
      // on the one screen size this whole block exists to shorten.
      + ` ${scope} ${outrank(field, 4)} button { padding-top: 4px; padding-bottom: 4px; }`
      + ` ${scope} ${outrank(field, 4)} input { padding-top: 4px; padding-bottom: 4px; }`
      // And the row a paged screen ends with, which is drawn outside the dense
      // wrapper and so is the one rule here that is not scoped to it. Ten
      // pixels of padding around a row of small buttons is a cheat on a
      // handheld and nothing at all on a television.
      + ` .${PAGER_FOOTER_CLASS} { --ce-footer-padding: 0 16px 2px; --ce-footer-row-padding: 2px 0 0; }`
      + " }",
  ].filter(Boolean).join("\n");
}

/** Injects the density rules once for the surface that wraps its children. */
export function DensePanel({ children, tightRows }: { children: ReactNode; tightRows?: boolean }) {
  const css = densityCss();
  return (
    <div className={tightRows ? `${DENSE_PANEL_CLASS} ${TIGHT_ROWS_CLASS}` : DENSE_PANEL_CLASS}>
      {css ? <style>{css}</style> : null}
      {children}
    </div>
  );
}

/**
 * A section heading with its separating rule beside the text.
 *
 * Steam's own `PanelSection title` renders the text inside a shrink-to-fit
 * element whose class is a bare content hash on the shipped client, so a rule
 * that has to span the remaining width cannot be attached to it. CE Decky
 * therefore renders the heading itself as the section's first child, where the
 * section's own 16px inline padding already aligns it with every row below.
 */
export function SectionHeading({ children, trailing }: { children: ReactNode; trailing?: ReactNode }) {
  return (
    <div className={HEADING_CLASS}>
      {children}
      {/* Ordered after the rule so a spinner sits at the end of the row rather
          than interrupting the rule, which is what Steam's own section title
          does with its spinner and the reason that title was worth replacing
          rather than dropping. */}
      {trailing ? <span className={HEADING_TRAILING_CLASS} style={{ order: 2 }}>{trailing}</span> : null}
    </div>
  );
}

/** A label that costs exactly one line however long its text is. */
export function OneLine({ children }: { children: ReactNode }) {
  return <span className={ELLIPSIS_CLASS}>{children}</span>;
}

/**
 * One line of text that scrolls itself while its row has controller focus.
 *
 * Off focus it is an ordinary ellipsised line, and a line the reader can
 * already finish never moves at all. Only a row carrying `FOCUS_SCROLL_CLASS`
 * animates, so the panel is never moving in more than one place.
 *
 * The overflow is read here rather than expressed as a pair of percentages in
 * the stylesheet, for the reason the stylesheet records: percentages resolve
 * against boxes, and an animation whose endpoint is a live function of a box
 * width has an endpoint that moves when the width does. This measures it while
 * the line is clamped, which is a state the animation cannot disturb, and
 * publishes one length.
 */
export function FocusScrollText({ children, paced }: { children: ReactNode; paced?: boolean }) {
  const trackRef = useRef<HTMLSpanElement | null>(null);
  const [overflow, setOverflow] = useState(0);
  useLayoutEffect(() => {
    const track = trackRef.current;
    if (!track) return;
    const row = typeof track.closest === "function" ? track.closest(`.${FOCUS_SCROLL_CLASS}`) : null;
    const reduced = typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : null;
    // Whether this row is showing the whole of its value right now, which under
    // reduced motion is a wrapped line rather than a moving one.
    const revealing = () => {
      if (!reduced?.matches || row === null) return false;
      if (row.classList.contains("gpfocuswithin")) return true;
      if (typeof row.querySelector === "function" && row.querySelector(".gpfocuswithin")) return true;
      const active = row.ownerDocument?.activeElement ?? null;
      return active !== null && row.contains(active);
    };
    const measure = () => {
      // A wrapped line measures as having nothing to reveal, and withdrawing
      // the mark on that answer unwraps it, which makes it overflow again: the
      // same flip-flop the window below exists to avoid, driven this time by
      // the reveal itself. The answer from before the reveal is the true one
      // and it is kept until the row gives focus back.
      if (revealing()) return;
      // Against the window this line is shown through, never against the line's
      // own box. The rule that reveals a focused line takes the clamp off that
      // box - `max-width: none; overflow: visible` - so while the row has
      // focus the line's own width *is* its content width and it measures as
      // having nothing to reveal. Answering that with zero withdraws the mark
      // the reveal rule matches on, which stops the reveal, re-clamps the line
      // and leaves the next measurement to do the same thing again: the line
      // never moves. That window is the parent, which no rule here touches, so
      // it gives the same answer focused or not.
      //
      // Rounded, because a sub-pixel difference is not a cut line: it is the
      // same line, and treating it as cut would start a scroll over a distance
      // nobody can see.
      const shownThrough = track.parentElement ?? track;
      const cut = Math.max(0, Math.round(track.scrollWidth - shownThrough.clientWidth));
      setOverflow((current) => (current === cut ? current : cut));
    };
    measure();
    // One measurement on mount is a measurement of whatever the layout was at
    // that instant, and a line measured before its box has a width is a line
    // with nothing to reveal: it publishes no overflow, so it never animates,
    // and nothing asks again until the row is rebuilt. A row on a list that
    // opens already populated is exactly that case, which is why the first row
    // of Manage would sit still while a row reached later moved.
    //
    // Three things ask again, and none of them costs anything while the answer
    // does not change: the frame after this one, the row taking focus, which
    // is the only moment the answer is about to matter, and a box that resizes.
    const frame = typeof requestAnimationFrame === "function" ? requestAnimationFrame(measure) : null;
    row?.addEventListener("focusin", measure);
    // And once it is given back, because the answer held above is only true
    // while the reveal is on the screen.
    row?.addEventListener("focusout", measure);
    // The track is clamped to its parent, so its own box stops changing once
    // the parent has one: the parent is what actually resizes. Both are watched
    // for that reason. Where there is no observer, the asks above are the whole
    // of it.
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    observer?.observe(track);
    if (track.parentElement) observer?.observe(track.parentElement);
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      row?.removeEventListener("focusin", measure);
      row?.removeEventListener("focusout", measure);
      observer?.disconnect();
    };
  }, [children]);
  const travel = overflow > 0
    ? ({
      [MARQUEE_SHIFT_VAR]: `${-overflow}px`,
      ...(paced
        ? {
          [MARQUEE_SECONDS_VAR]: `${Math.min(
            MARQUEE_PACE_MAX_SECONDS,
            Math.max(REVEAL_SECONDS, overflow / MARQUEE_PACE_PX_PER_SECOND),
          ).toFixed(1)}s`,
        }
        : {}),
    } as CSSProperties)
    : undefined;
  return (
    <span className={MARQUEE_CLASS} data-clipped={overflow > 0 ? "" : undefined}>
      <span ref={trackRef} style={travel}>
        {children}
      </span>
    </span>
  );
}

/**
 * A refusal, drawn so it is not read as one more row of the list it sits under.
 *
 * Every screen here is rows, so a failure rendered as a row is a failure that
 * looks like data: the Configure cheats screen reported "Cannot apply cheats"
 * in the same weight and colour as the cheats above it. This is its own block,
 * on the colour the rest of the product refuses things in, and it is shared
 * rather than styled per screen so a second one cannot drift from the first.
 */
export function RefusalBlock({ children, testId }: { children: ReactNode; testId?: string }) {
  return <div style={refusalBlockStyle} data-testid={testId}>{children}</div>;
}

/**
 * Styled inline rather than through this module's scoped stylesheet.
 *
 * A refusal is shown where the press that earned it was made, and on this
 * plugin's screens that is often outside the dense wrapper the stylesheet is
 * scoped to - the cheat picker's is below its own, beside Apply. Reaching it
 * with a class meant wrapping that block in a second `DensePanel`, which mounts
 * a second copy of the whole stylesheet for one border. These are five
 * declarations and they belong to the component.
 */
const refusalBlockStyle: CSSProperties = {
  margin: "4px 0 2px",
  borderRadius: 4,
  // The colour the rest of the product refuses things in, the same one search
  // marks a refused row with, so no screen invents a second red.
  borderLeft: "3px solid hsla(9, 74%, 62%, 0.82)",
  background: "hsla(9, 74%, 40%, 0.22)",
  padding: "2px 10px",
};

/**
 * The height a whole page of uniform rows takes, held for the pages that are
 * shorter than one.
 *
 * A paged list whose last page is short made its window change height, so
 * paging to the end moved every control below the list and the way out of the
 * screen arrived somewhere new. This holds the height a whole page takes.
 *
 * An earlier version of it answered only once a full page had actually been on
 * screen, and two things were wrong with that. A device holding three tables
 * has one short page and never a full one, so there was nothing to hold and the
 * window changed size on every filter. And the height it did hold outlived the
 * page size it was measured for: these screens settle on their page a commit
 * after they open, so a window kept the height of the page it started with and
 * stood four pixels past the bottom of a Steam Deck's display.
 *
 * So a full page is measured where there is one, and estimated from a row where
 * there is not. The measurement is the whole box, because a row times a page is
 * not quite a page: what separates two rows belongs to neither of them, and
 * counting rows alone left Manage's last page two pixels short of its first and
 * moved the window by exactly that. The estimate is the shortest row times a
 * page, which is as close as anything can come to a page that has never been
 * drawn.
 *
 * For a list whose rows are one fixed height: one line of name over one line of
 * state, clipped rather than wrapped. That is what makes a page of them worth
 * measuring once, and it is what a caller has to be sure of, because a list
 * whose rows can be opened in place would have this hold the height of the page
 * with the opened row on it.
 *
 * Measuring the box the height is put on is safe. `min-height` never changes
 * how tall the children are, and on a full page the box is at least as tall as
 * whatever is already held, so it reports what those rows actually take.
 */
export function usePageHeight(node: Element | null, rows: number, pageSize: number): number | null {
  const [held, setHeld] = useState<{ height: number; pageSize: number } | null>(null);
  // A page that was measured for six rows is not the height of a page of three.
  // The screens that measure their own page size settle on it a commit after
  // they open, so the first answer here can belong to the page they started
  // with, and holding that one padded the window to a page nobody is on.
  const height = held !== null && held.pageSize === pageSize ? held.height : null;
  const setHeight = (next: (current: number | null) => number | null) => setHeld((current) => {
    const value = next(current !== null && current.pageSize === pageSize ? current.height : null);
    return value === null ? current : { height: value, pageSize };
  });
  useLayoutEffect(() => {
    if (!node || rows <= 0 || pageSize <= 0 || typeof node.getBoundingClientRect !== "function") return;
    let page = 0;
    if (rows >= pageSize) {
      page = Math.round(node.getBoundingClientRect().height);
    } else {
      const measured: number[] = [];
      for (const child of Array.from(node.children)) {
        if (typeof child.getBoundingClientRect !== "function") continue;
        const box = Math.round(child.getBoundingClientRect().height);
        if (box > 0) measured.push(box);
      }
      if (measured.length === 0) return;
      page = Math.min(...measured) * pageSize;
    }
    if (page <= 0) return;
    // A page is never shortened by a later measurement: rows carry marks and
    // second lines that come and go, and a window that tracked the shortest of
    // them would move for exactly the reason this exists to stop.
    setHeight((current) => (current !== null && current >= page ? current : page));
  }, [node, rows, pageSize]);
  return height;
}

/**
 * How tall one row of a list actually is, rather than what a constant says.
 *
 * A page is fitted to a screen by dividing the room by a row, and the row was a
 * number written beside the stylesheet that draws it. Those two drift, and the
 * drift is silent: trim a row's padding for a short screen and the arithmetic
 * goes on dividing by the old height, so the page is a row short or a row long
 * and nothing says which.
 *
 * The shortest row on screen, for the reason `usePageHeight` takes the same
 * one: a row the reader has opened is several lines tall and is not the row a
 * page is made of. Never larger than what it has already held, so a page cannot
 * grow under a reader who opened something.
 *
 * The fallback is the caller's own constant, for the first render and for any
 * host with no layout.
 *
 * Measured on every render rather than on a dependency list, because what
 * changes a row's height is the content in it and there is no value to watch
 * for that. It settles rather than looping: an unchanged measurement sets the
 * same number, and React stops there.
 */
export function useRowHeight(node: Element | null, fallback: number): number {
  const [height, setHeight] = useState<number | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    if (!node) return;
    const measured: number[] = [];
    for (const child of Array.from(node.children)) {
      if (typeof child.getBoundingClientRect !== "function") continue;
      const box = Math.round(child.getBoundingClientRect().height);
      if (box > 0) measured.push(box);
    }
    if (measured.length === 0) return;
    const row = Math.min(...measured);
    setHeight((held) => (held !== null && held <= row ? held : row));
  });
  return height ?? fallback;
}

/** A short aside inside a revealed block: no label, no control, no row height. */
export function PanelNote({ children }: { children: ReactNode }) {
  return <div className={NOTE_CLASS}>{children}</div>;
}

/** Text that wraps inside the row even when it is one unbroken path. */
export function WrapText({ children }: { children: ReactNode }) {
  return <span className={WRAP_CLASS}>{children}</span>;
}

/**
 * A rare or secondary action, sized so it never owns a whole row.
 *
 * Decky's `ButtonItem` is a full-width row roughly as tall as a two-line field,
 * which is right for the one action a screen is about and wrong for everything
 * else on it.
 */
export const smallActionStyle: CSSProperties = {
  // Steam's DialogButton grows to fill its flex line, which is right for a
  // dialog's primary action and wrong for a secondary control that must stay
  // beside a label, so this one is sized by its own text.
  flex: "0 0 auto",
  minWidth: 0,
  width: "auto",
  padding: "4px 10px",
  fontSize: 12,
  lineHeight: "16px",
  whiteSpace: "nowrap",
  // Steam's button leaves its label top-aligned at this height, so centre the
  // text in the box and the box against the row it belongs to.
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
};

/**
 * A horizontal group of small actions with stable controller navigation.
 *
 * A `Focusable` is itself a focus target, so wrapping a lone button in one
 * gives Steam a container to land on whose activation does nothing - the button
 * looks focused and pressing A is silently ignored. Only group two or more.
 *
 * Held across a press for the reason `PanelRow` holds which element wraps a
 * row: an action that disables itself for the length of the press it started
 * takes the count of reachable controls below two, and swapping the wrapper for
 * a plain `div` unmounts every button inside it and mounts a new one when the
 * press finishes. The user loses the focus ring off the button under their
 * thumb, twice per press, and a caller holding the button it just pressed is
 * holding a detached element that stays disabled forever.
 *
 * Held only while the controls are still there, though. A group whose second
 * control is disabled is a group mid-press; a group whose second control is
 * gone is a different row, and keeping the wrapper there would put back exactly
 * the dead focus target this rule exists to avoid.
 */
export function ActionGroup({ children, style, navEntryPreferPosition }: {
  children: ReactNode;
  style?: CSSProperties;
  navEntryPreferPosition?: NavEntryPositionPreferences;
}) {
  const baseStyle: CSSProperties = { display: "flex", alignItems: "center", alignSelf: "center", ...style };
  const everGrouped = useRef(false);
  if (availableActionCount(children) > 1) everGrouped.current = true;
  else if (renderedActionCount(children) <= 1) everGrouped.current = false;
  if (!everGrouped.current) {
    return <div style={baseStyle}>{children}</div>;
  }
  return (
    <Focusable flow-children="row" navEntryPreferPosition={navEntryPreferPosition} style={{ justifyContent: "flex-end", gap: 8, ...baseStyle }}>
      {children}
    </Focusable>
  );
}

/**
 * A box a ref can be hung on without changing the layout around it.
 *
 * Steam works out which control is beside which from the geometry the layout
 * produces, so an ordinary wrapper around one control in a row is a box in the
 * middle of that row. `contents` leaves the control exactly where its group put
 * it and still gives the caller somewhere to hang a ref that finds it.
 */
export const CONTENTS_ONLY: CSSProperties = { display: "contents" };

/**
 * Put the focus ring on the first of these boxes that still holds a control.
 *
 * For a row that rebuilds itself under the user's thumb. `ActionGroup` is a
 * plain box while one control can be pressed and a navigation container once
 * two can, which is the right rule and also means the press that enables the
 * second control replaces both of them: a pager sitting on page one has
 * Previous disabled, so the first press of Next unmounts the button that press
 * was made on and the ring goes with it.
 *
 * The caller says which control the reader was working and what to fall back to
 * when that one has just disabled itself, which is the other half of the same
 * problem: the first page turns Previous off exactly as the last turns Next
 * off, and either way the ring has to land somewhere the reader can see it.
 *
 * Says whether the ring landed. A caller acting on a request that outlives the
 * press it came from has to be able to tell "not yet" from "done": every box
 * here can be empty or hold nothing that can be pressed, and a request retired
 * on that is a request spent on nothing.
 */
export function focusFirstEnabled(...boxes: Array<{ current: HTMLElement | null }>): boolean {
  for (const box of boxes) {
    const control = box.current?.querySelector<HTMLElement>("button:not([disabled])");
    if (control) {
      control.focus();
      return true;
    }
  }
  return false;
}

/**
 * Read-only facts about one thing, two to a line where the screen is short.
 *
 * A screen that opens with four lines of identity - the file, where it came
 * from, what is in it, what it can execute - spends four rows on text nobody
 * presses, and on a handheld those four rows are most of what the display has.
 * Each of them is a short label over a short value, so two of them fit a line
 * with room to spare: the panel is the same width on every display measured,
 * and it is the height that is scarce.
 *
 * Only where the screen is short. A display with the room keeps the rows it
 * had, one fact to a line, which is the shape those screens were designed and
 * looked at in.
 */
function InfoValue({ children }: { children: ReactNode }) {
  return <span style={{ display: "block", minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", lineHeight: "14px", height: 14 }}>{children}</span>;
}

export function InfoFields({ items }: { items: readonly { label: ReactNode; description: ReactNode }[] }) {
  const present = items.filter((item) => item.description !== null && item.description !== undefined);
  // A node of this screen, because this plugin's code runs in Steam's shared
  // context, whose window is one pixel tall, while its DOM is rendered in the
  // page the user is looking at. Asking without one answered that every screen
  // is short, which was right on a handheld by accident and wrong on a
  // television. The callback ref lands in the commit that mounts it, so the
  // layout settles before the frame is painted rather than after it.
  const [pageNode, setPageNode] = useState<HTMLDivElement | null>(null);
  const probe = <div ref={setPageNode} style={CONTENTS_ONLY} />;
  if (!isShortScreen(pageNode)) {
    return (
      <>
        {probe}
        {present.map((item, index) => (
          <PanelSectionRow key={index}><Field label={item.label} description={<InfoValue>{item.description}</InfoValue>} /></PanelSectionRow>
        ))}
      </>
    );
  }
  const pairs: (typeof present)[] = [];
  for (let index = 0; index < present.length; index += 2) pairs.push(present.slice(index, index + 2));
  return (
    <>
      {probe}
      {pairs.map((pair, index) => (
        <PanelSectionRow key={index}>
          {/* The last pair carries the group's own bottom edge. Paired, these
              rows read as one block, and the row after them is a different
              thing being said - so it needs separating from them by more than
              the hairline that divides one row of the block from the next. The
              measure is the channel already visible between the two columns:
              the air that separates a fact from the fact beside it is the air
              that separates the group from what follows it. */}
          <div style={index === pairs.length - 1 ? infoLastPairStyle : infoPairStyle}>
            {pair.map((item, column) => (
              // Each half is its own minimum-width-zero column, so a long value
              // clips inside its half instead of widening the row and pushing
              // the other one off the panel.
              <div key={column} style={infoHalfStyle}>
                <Field label={item.label} description={<InfoValue>{item.description}</InfoValue>} bottomSeparator="none" />
              </div>
            ))}
            {/* An odd count leaves the last fact on the left rather than
                centred, which is where the eye is already reading. */}
            {pair.length === 1 ? <div style={infoHalfStyle} /> : null}
          </div>
        </PanelSectionRow>
      ))}
    </>
  );
}

/** The air between one fact and the fact beside it, and under the group. */
const INFO_PAIR_GAP = 12;
const infoPairStyle: CSSProperties = { display: "flex", alignItems: "flex-start", gap: INFO_PAIR_GAP };
const infoLastPairStyle: CSSProperties = { ...infoPairStyle, marginBottom: INFO_PAIR_GAP };
const infoHalfStyle: CSSProperties = { flex: "1 1 0", minWidth: 0 };

/**
 * Two of Steam's own controls in the height of one row, where height is scarce.
 *
 * Steam gives a dropdown and a text field a row each, which is right on a
 * television and expensive on a handheld: the cheats screen spent 80 of its 534
 * pixels on a section picker and a filter that between them hold one word. Side
 * by side they cost the taller of the two and nothing more.
 *
 * Only where that trade is worth making. A row halved is a control halved, and
 * Steam sets a dropdown's label beside its value, so on a screen with the
 * height for two rows the pair bought nothing and cost the picker its text -
 * `All supported controls` came out as `All su...` on a 4K television. There
 * each control takes the row it was drawn for.
 *
 * Side by side it is a navigation container rather than a plain box, so the
 * controller moves between them the way it moves along any row here, and each
 * column is its own zero-minimum-width box so a long section name clips inside
 * its half instead of pushing the filter off the screen.
 */
export function SideBySide({ children, testId }: { children: ReactNode; testId?: string }) {
  // A node of this screen, because this plugin's code runs in Steam's shared
  // context, whose window is one pixel tall, while its DOM is rendered in the
  // page the user is looking at. The callback ref lands in the commit that
  // mounts it, so the layout settles before the frame is painted.
  const [pageNode, setPageNode] = useState<HTMLDivElement | null>(null);
  const probe = <div ref={setPageNode} style={CONTENTS_ONLY} />;
  const columns = Children.toArray(children);
  if (!isShortScreen(pageNode)) {
    return (
      <div style={sideBySideStyle} data-testid={testId}>
        {probe}
        {columns.map((child, row) => <div key={row}>{child}</div>)}
      </div>
    );
  }
  return (
    <Focusable flow-children="row" style={sideBySideRowStyle} data-testid={testId}>
      {probe}
      {columns.map((child, column) => (
        <div key={column} style={sideBySideHalfStyle}>{child}</div>
      ))}
    </Focusable>
  );
}

// Air under the pair, because what follows it is the list these two narrow and
// not another control: without it the row reads as the first thing in the list
// rather than as the thing that decides what the list holds. The same ten
// pixels a stacked control gets under it, so the panel has one measure for
// "this block is finished" rather than two.
const sideBySideStyle: CSSProperties = { marginBottom: 10 };
const sideBySideRowStyle: CSSProperties = { ...sideBySideStyle, display: "flex", alignItems: "center", gap: 12 };
const sideBySideHalfStyle: CSSProperties = { flex: "1 1 0", minWidth: 0 };

/**
 * A filter, labelled inside itself.
 *
 * Steam draws a text field's label above its input on a full-size screen and,
 * under this panel's own rules, beside it on a short one. Both spend width or
 * height on a word that stops being worth anything the moment the reader is
 * typing. The word goes in the box instead, as the placeholder, where it is
 * gone while there is anything to read and back as soon as the box is empty.
 *
 * Still named for anything that is not looking at it: the accessible name is
 * the same word, so a controller's own reading of the control and this
 * repository's tests both still find it by name.
 */
export function FilterField({ value, onChange, disabled, placeholder = "Filter" }: {
  value: string;
  onChange: ChangeEventHandler<HTMLInputElement>;
  disabled?: boolean;
  placeholder?: string;
}) {
  // Steam's own control, resolved out of its bundle at runtime, and its props
  // are declared as `HTMLAttributes` rather than `InputHTMLAttributes` - so
  // `placeholder` is a perfectly ordinary input attribute the declaration does
  // not list. Spread rather than cast, because a spread is the one form that
  // says "this goes to the input underneath" without claiming the declaration
  // is wrong about anything else.
  const inInput = { placeholder };
  return (
    <TextField
      {...inInput}
      aria-label={placeholder}
      value={value}
      disabled={disabled}
      onChange={onChange}
    />
  );
}

// How many times a page may give rows up before it stops trying.
const MAX_SHRINKS = 2;

/**
 * The rows this screen owes its window, or the ones it can still take back.
 *
 * `rowsOffTheBar` is the measurement; this is the rule around it. `footer` is
 * the screen's own footer box and `ready` is the caller's own "this layout is
 * the one", because until that is true there is nothing worth measuring and an
 * early answer is the transient this exists to correct.
 *
 * `more` is whether growing the page would actually show anything: a list
 * shorter than its own page has room under it that no page size can fill, and
 * asking for rows there would grow the held page height and walk the window
 * into the bar chasing rows that do not exist.
 *
 * Bounded, and that is the whole of what keeps it from oscillating. Each
 * correction re-reads the layout its own change produced, because one
 * correction is an estimate and the measurement after it is what says whether
 * the estimate was right. Past the bound the cause is not the rows - a screen
 * whose own chrome is taller than the display cannot be paged out of that - and
 * a rule that kept going would walk the page down to one and take the list away
 * as well. It stops as soon as a measurement asks for nothing, which is the
 * ordinary ending.
 */
export function useFittedRows(
  footer: Element | null,
  rowHeight: number,
  ready: boolean,
  more: boolean,
  layout = 0,
): number {
  const [adjustment, setAdjustment] = useState(0);
  const spent = useRef({ grew: false, shrinks: 0 });
  const drawn = useRef(layout);
  useLayoutEffect(() => {
    // A block that appears above or below the list after this screen has
    // settled moves the footer without changing its identity or its size, so
    // nothing here noticed and a page that fitted went behind Steam's bar.
    // `layout` is every such block on this screen's vertical path as one
    // number - a failure row, a confirmation, whatever is added next - and the
    // caller composes it rather than this taking a flag per block, because a
    // second parameter per block is a parameter somebody forgets to pass. A
    // change to it is a new layout, and a new layout gets the budget again
    // rather than the remains of the one spent on the layout before it.
    if (drawn.current !== layout) {
      drawn.current = layout;
      spent.current = { grew: false, shrinks: 0 };
    }
    if (!ready) return;
    const off = rowsOffTheBar(footer, rowHeight);
    if (off === 0) return;
    // Shrinking has the last word, and growing does not get one after it.
    // A screen drawn behind Steam's bar is a failure; a screen with a row of
    // room it did not take is a cost. Ordered the other way round - one of
    // each, in whatever order the layout produced them - a correction that
    // shrank and then grew ended behind the bar with nothing left to fix it,
    // which is exactly what a Steam Deck did.
    if (off > 0 && spent.current.shrinks >= MAX_SHRINKS) return;
    if (off < 0 && (spent.current.grew || spent.current.shrinks > 0 || !more)) return;
    if (off > 0) spent.current.shrinks += 1;
    else spent.current.grew = true;
    setAdjustment((held) => held - off);
  }, [footer, rowHeight, ready, more, adjustment, layout]);
  return adjustment;
}

/** A row that carries only actions, right-aligned like a row's own controls. */
export function ActionRow({ children, testId, navEntryPreferPosition }: {
  children: ReactNode;
  testId?: string;
  navEntryPreferPosition?: NavEntryPositionPreferences;
}) {
  return (
    <div style={{ padding: "4px 0" }} data-testid={testId}>
      <ActionGroup navEntryPreferPosition={navEntryPreferPosition}>{children}</ActionGroup>
    </div>
  );
}

/**
 * Count the controls that are here at all, disabled ones included.
 *
 * What separates a group mid-press from a group that has lost a control: the
 * first still renders both and one of them is disabled, the second renders one.
 */
function renderedActionCount(children: ReactNode): number {
  return Children.toArray(children).reduce<number>((count, child) => {
    if (!isValidElement(child)) return count;
    const props = child.props as {
      children?: ReactNode;
      onActivate?: unknown;
      onChange?: unknown;
      onClick?: unknown;
    };
    const actionable = typeof props.onActivate === "function"
      || typeof props.onChange === "function"
      || typeof props.onClick === "function";
    return actionable ? count + 1 : count + renderedActionCount(props.children);
  }, 0);
}

/** Count controls Steam can actually focus, including ones inside layout wrappers. */
function availableActionCount(children: ReactNode): number {
  return Children.toArray(children).reduce<number>((count, child) => {
    if (!isValidElement(child)) return count;
    const props = child.props as {
      children?: ReactNode;
      disabled?: boolean;
      onActivate?: unknown;
      onChange?: unknown;
      onClick?: unknown;
    };
    const actionable = typeof props.onActivate === "function"
      || typeof props.onChange === "function"
      || typeof props.onClick === "function";
    if (actionable) return count + (props.disabled === true ? 0 : 1);
    return count + availableActionCount(props.children);
  }, 0);
}

interface RowProps {
  label: ReactNode;
  description?: ReactNode;
  /**
   * Static text on the right of the row, left of any controls.
   *
   * It is deliberately not part of `actions`: a `Focusable` groups controller
   * navigation, and giving it a child that cannot take focus makes the group's
   * behaviour depend on how many real controls happen to be beside it.
   */
  trailing?: ReactNode;
  /**
   * One passive glyph about this row, held against the controls on its right.
   *
   * Separate from `trailing` because it is a mark rather than a word: it keeps
   * its own colour instead of the dimmed weight a trailing value is set in, and
   * it is what a reader scanning a list is looking for rather than something
   * they read.
   *
   * It belongs here rather than after the label because a row's label is
   * clipped to one line: on the long names real tables carry, a glyph drawn at
   * the end of the text was inside the part that got cut, so the one state the
   * list exists to show disappeared exactly on the rows whose names were
   * longest. On the right it is in the same column on every row, which is also
   * what makes a column of them readable at a glance.
   */
  mark?: ReactNode;
  /** Small controls placed on the row itself instead of below it. */
  actions?: ReactNode;
  /**
   * What this row is for, in plain language, behind a `?` button.
   *
   * A diagnostics screen is full of controls whose purpose is obvious only to
   * whoever wrote them. Keeping the explanation one press away costs no height
   * until it is asked for.
   */
  help?: string;
  /**
   * A mark shown before the label rather than beside the row's controls.
   *
   * For a row whose controls already fill what it has. The quick access panel
   * is one narrow column, and its table row carries a filename and two presses:
   * putting a third thing in the trailing group laid `Manage` off the edge of
   * the panel, which is the same arithmetic that kept that row to two controls
   * in the first place. Here the mark costs the name's width, which the name
   * can give, instead of the buttons' place, which they cannot.
   */
  leadingMark?: ReactNode;
  truncate?: boolean;
  /**
   * Reveal this row's clipped text by scrolling it while the row has focus.
   *
   * For a list whose rows are named by something the reader has to finish
   * reading: a table's file name, the sentence a not-working record was written
   * with. Both are routinely longer than a 460 pixel modal, and cutting them to
   * one line is what keeps the list a list - so the end of the name is exactly
   * what the row cannot show and exactly what tells one row from another.
   *
   * The same reveal the pinned cheats on the panel use, and it costs the row
   * nothing: off focus it is an ordinary ellipsised line, a line that already
   * fits never moves, and only the row the ring is actually on animates.
   *
   * Requires `truncate`; a row that wraps has nothing to reveal. An opened row
   * wraps instead, because then the whole text is on screen already.
   */
  scroll?: boolean;
  /**
   * `header` makes this the row that heads the list under it.
   *
   * For a list whose first row is its own summary: the totals for every table
   * source, the count of tables that did not work, Home's live runtime state
   * above the pinned cheats it describes. It is a row of data, not a caption,
   * so it is set a step darker than the panel rather than styled as text, and
   * it holds the rows that follow off itself.
   */
  tone?: "header" | "aside" | "own";
  /**
   * Let this row's controls take the width its text is not using.
   *
   * Steam sizes a row's control column to what the controls need, which is
   * right for buttons and wrong for a box somebody types into: the filter on
   * Manage stopped well short of the row it sits in while the rows under it ran
   * to the edge. A row that asks for this gives its controls the free width and
   * clips its own label, which is the trade that row wants and no other row on
   * this panel does.
   */
  fillWithActions?: boolean;
  /**
   * This row is state the user reads where it is, with nothing to reach on it.
   *
   * A read-only row is otherwise made a focus stop of its own, which is what
   * lets a screen made almost entirely of them be scrolled through and lets a
   * clipped one be opened; the note on the wrapper below records why. On a
   * screen that is mostly controls, the same rule turns a line of status into a
   * stop the ring rests on with nothing to show for it and nothing to press,
   * and paging past it is the whole cost. Set this only where the row says
   * something short and complete, and only where the controls around it can
   * still carry the screen's own scrolling.
   *
   * A row set this way does not clip its text either, whatever `truncate` says,
   * because the two are the same claim from opposite ends and only one of them
   * can be true. Clipping a line is what makes a row worth reaching: the reader
   * cannot finish it where it stands, so a press has to be able to open it. A
   * row that says it is read in place and then hides the end of its own
   * sentence has both, a stop the ring rests on and text no press can reveal,
   * which is exactly what Home's runtime line was: "Cheat Engine is not running
   * for this table. Details …", cut in a 300 pixel panel on the television as
   * well as on the handheld. So this wraps instead, and costs the line it
   * needed rather than a press to see it.
   */
  status?: boolean;
  testId?: string;
}

/**
 * One piece of a row's text, in the shape this row shows it in.
 *
 * A status row wraps rather than merely being left alone, because leaving it
 * alone only wraps at spaces: a process name or an installation path is one
 * token with none, and it would run straight past the right edge of a 300 pixel
 * panel instead of being cut, which is worse than either. `WrapText` carries
 * the rule that breaks such a token, and it is the same one an opened row uses.
 */
function readable(text: ReactNode, clip: boolean, open: boolean, status?: boolean, scroll?: boolean): ReactNode {
  if (clip) {
    if (open) return <WrapText>{text}</WrapText>;
    // Paced: these rows carry whole sentences rather than the two short lines a
    // pinned cheat does, and one flat duration over a much longer distance is
    // what made them unreadable.
    return scroll === true ? <FocusScrollText paced>{text}</FocusScrollText> : <OneLine>{text}</OneLine>;
  }
  return status === true ? <WrapText>{text}</WrapText> : text;
}

/** One status line with its own secondary actions on the same row. */
export function PanelRow({ label, description, trailing, mark, leadingMark, actions, help, truncate, scroll, tone, status, testId, fillWithActions }: RowProps) {
  const [helpOpen, setHelpOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const header = tone === "header";
  const aside = tone === "aside";
  const own = tone === "own";
  // Read in place wins over cut to one line: see `status`. Nothing that is
  // reached and opened is affected, because such a row is not a status row.
  const clip = truncate === true && status !== true;
  // Whether this row already has something Steam can put focus on. `trailing`
  // is static text and never counts, and a row whose only action is disabled
  // has nothing reachable on it either.
  //
  // Latched, because the answer decides which element wraps the row and a row
  // that changes its mind about that unmounts everything inside it. Almost
  // every action here disables itself while the press it started is running:
  // pressing Use in the imported-table list, or Read in the code view, took
  // the row from "has a control" to "has none", so the button the user had
  // just pressed was unmounted under the focus that was on it and the ring
  // went somewhere else, twice per press. A row that has ever had a control is
  // a row with a control; one whose only action is disabled from the start,
  // which is what a Debug row is, keeps the wrapper that makes it reachable.
  const everReachable = useRef(false);
  if (Boolean(help) || availableActionCount(actions) > 0) everReachable.current = true;
  const reachable = everReachable.current;
  // Either way of opening a row wraps its text. Only the help block takes the
  // separator with it, because it renders its own underneath; wrapping renders
  // nothing, so suppressing it there merged the row with the one below.
  const open = helpOpen || expanded;
  const body = (
    <>
      <Field
        label={leadingMark
          ? <span style={leadingMarkRowStyle}>{leadingMark}{readable(label, clip, open, status, scroll)}</span>
          : readable(label, clip, open, status, scroll)}
        description={description ? readable(description, clip, open, status, scroll) : undefined}
        bottomSeparator={helpOpen || header ? "none" : "standard"}
        childrenLayout="inline"
        childrenContainerWidth={fillWithActions ? "max" : "min"}
        verticalAlignment="center"
      >
        {trailing || mark || actions || help ? (
          <div style={fillWithActions ? trailingRowFillStyle : trailingRowStyle}>
            {trailing ? <div style={trailingTextStyle}>{trailing}</div> : null}
            {mark ? <div style={markStyle}>{mark}</div> : null}
            {actions || help ? (
              // The smallest gap that still reads as two controls rather than
              // one wide one. Every pixel here is a pixel the row's own text
              // does not get, and these rows are named by something the reader
              // has to finish reading.
              <ActionGroup style={fillWithActions ? rowActionGroupFillStyle : rowActionGroupStyle}>
                {actions}
                {help ? (
                  <SmallButton onClick={traceUiAction("panel_row.help", () => setHelpOpen((isOpen) => !isOpen), { row: typeof label === "string" ? label.slice(0, 80) : testId, open: !helpOpen })}>{helpOpen ? "\u00d7" : "?"}</SmallButton>
                ) : null}
              </ActionGroup>
            ) : null}
          </div>
        ) : undefined}
      </Field>
      {helpOpen && help ? (
        <Field description={help} bottomSeparator="standard" indentLevel={1} />
      ) : null}
    </>
  );
  return (
    <div
      data-testid={testId}
      className={[
        header ? HEADER_ROW_CLASS : aside ? ASIDE_ROW_CLASS : own ? OWN_ROW_CLASS : null,
        // Only the row the ring is on animates, and only while it is there, so
        // the class goes on the element that holds both this row's text and the
        // controls focus actually lands on.
        scroll && clip ? FOCUS_SCROLL_CLASS : null,
      ].filter(Boolean).join(" ") || undefined}
    >
      {/* A row with nothing to press becomes a focus target of its own.
          Steam scrolls a panel by moving focus into it, so a run of read-only
          rows at the end of a screen is not merely unfocusable: it cannot be
          scrolled to at all, and Debug, which is almost entirely such rows,
          ended at whatever fitted on one screen. `tone="header"` is not an
          exception: it is a list's own first row of data, not the caption above
          it, so a summary with nothing to press was the one row on the panel
          that could neither be reached nor opened. Home's runtime line is
          exactly that row, and it truncates.

          Pressing it is not a dead press either: a truncated row is exactly the
          one whose text the user cannot finish reading, so activating it wraps
          the row, the same thing the `?` button does for a row that has one. */}
      {/* The activation is always handed over, even where there is nothing for
          it to do. A `Focusable` holding a control is a focus target because of
          that control; one holding only text has no reason to be a stop unless
          it can be activated, and on the target that was the whole difference:
          every read-only row here could be reached except the two carrying no
          `truncate`, so the last section of Debug stayed unreachable. Where the
          row does truncate, this wraps it, which is what the `?` button does
          for a row that has one. */}
      {reachable || status ? body : (
        <Focusable onActivate={traceUiAction("panel_row.expand", () => setExpanded((isOpen) => !isOpen), { row: typeof label === "string" ? label.slice(0, 80) : testId, open: !expanded })} style={{ padding: 0 }}>
          {body}
        </Focusable>
      )}
    </div>
  );
}

/**
 * The gap between a row's own controls, and between them and its mark.
 *
 * Read from the row rather than fixed here, so a screen that wants its rows
 * tighter can say so once on its wrapper: see `TIGHT_ROWS_CLASS`.
 *
 * The fallback is the eight pixels `ActionGroup` sets on the group it builds,
 * and it has to be, because this is spread over that: a fallback of zero read
 * as a deliberate `gap: 0px` and overrode it, which stuck every pair of buttons
 * on every screen together.
 */
const rowActionGroupStyle: CSSProperties = { gap: "var(--ce-row-action-gap, 8px)" };
// And the same group for a row that asked for the width: it grows, and what is
// in it is what decides how, which on the one row using this is a filter.
const rowActionGroupFillStyle: CSSProperties = { ...rowActionGroupStyle, flex: "1 1 auto", minWidth: 0 };

/** A leading mark and the label it belongs to, on one line that can still clip. */
const leadingMarkRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  minWidth: 0,
};

const trailingRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "var(--ce-row-trailing-gap, 8px)",
};

// The same row, given the width the label is not using. Only for a row that
// asked: everywhere else a control column that grew would be taking the room
// from the name the reader is there to read.
const trailingRowFillStyle: CSSProperties = { ...trailingRowStyle, width: "100%" };

// Held to its own size against the controls beside it, and never shrunk: a
// 16 pixel glyph squeezed by a flex row is the mark being unreadable rather
// than the row being narrower.
//
// On a screen that opts into `TIGHT_ROWS_CLASS` the width is fixed whether or
// not there is a glyph to put in it, so the controls beside it start at the
// same place on every row: a list where some rows carry a mark and some do not
// otherwise has its buttons in two columns, which is what Manage looked like,
// with Use and Delete stepped left on exactly the rows that had something to
// say.
//
// Everywhere else it is the glyph's own width and nothing when there is no
// glyph, which is what every screen did before there was a choice. Reserving it
// everywhere took 22 pixels out of the quick access panel's one narrow row for
// a mark that is often not there, and moved Search and Manage for nothing.
// A minimum rather than a width: one glyph still reserves the same column on
// every row, which is what keeps a list of them lined up, and a row carrying a
// second statement about its bytes - that they are signed, that CE Decky made
// them - grows to hold it instead of drawing it over the row's own text.
const markStyle: CSSProperties = {
  flex: "0 0 auto",
  minWidth: "var(--ce-row-mark-width, auto)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 4,
  lineHeight: 0,
};

// A block rather than an inline span: a row may put more than a word here, and
// this download's countdown stacks the spinner under it.
const trailingTextStyle: CSSProperties = {
  flex: "0 0 auto",
  fontSize: 12,
  lineHeight: "16px",
  opacity: 0.75,
  whiteSpace: "nowrap",
};

/**
 * A step up for a control that is a screen's own action rather than an aside.
 *
 * Search is not a rare secondary press: it is the reason its screen exists, and
 * at the diagnostics size it reads as one. It stays a row-level control rather
 * than becoming a full-width button, which is what cost that screen its results.
 */
export const mediumActionStyle: CSSProperties = {
  ...smallActionStyle,
  padding: "6px 14px",
  fontSize: 13,
  lineHeight: "18px",
};

export function SmallButton({ children, onClick, disabled, preferredFocus, grow, size = "small" }: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  preferredFocus?: boolean;
  /**
   * Take the free width of the row, which is also where a step into the row
   * lands.
   *
   * Steam enters a row by rectangle: every control on one row is the same
   * distance from the control above it, so the tie is settled by how much of
   * that control's width each one covers, and the widest wins. A row's own
   * primary action is therefore marked rather than ordered, because the order
   * of the controls only settles a tie between two of exactly equal width,
   * which no two labels reliably are.
   */
  grow?: boolean;
  size?: "small" | "medium";
}) {
  const style = size === "medium" ? mediumActionStyle : smallActionStyle;
  // Never hand Steam's controller click event to a workflow callback: several
  // of them forward their argument, and an event reaching Steam's game list is
  // exactly the leak the panel tests guard against.
  return (
    <DialogButton style={grow ? { ...style, flex: "1 1 auto" } : style} disabled={disabled} preferredFocus={preferredFocus} onClick={() => onClick()}>{children}</DialogButton>
  );
}
