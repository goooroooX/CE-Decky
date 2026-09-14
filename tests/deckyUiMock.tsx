/**
 * The shared half of every test's `@decky/ui` double.
 *
 * Eleven test files each carried their own factory, and most of what was in
 * them was the same plain thing written eleven times. That was tolerable until
 * a component shared by several screens began importing one more export from
 * Decky: three files whose factory did not list it broke at once, with
 * `No "NavEntryPositionPreferences" export is defined on the "@decky/ui" mock`
 * from whichever test happened to render it first, and the fix was the same
 * line typed into each of them.
 *
 * So the parts that are the same live here and a file spreads its own over
 * them. What a file renders differently it keeps: these doubles are not
 * decoration, and the tests assert against exactly what they emit - one file's
 * `ModalRoot` has to offer the controller's Back and Escape as buttons, another
 * needs `Focusable` to carry its navigation attributes, a third needs the
 * dropdown to hold an empty option. Making those uniform would quietly rewrite
 * what a dozen assertions are about, which is a different change from this one.
 *
 * What it costs, said plainly: an export a file never listed now resolves to a
 * plain double instead of throwing `No "X" export is defined`. That throw is a
 * signal - it says this test has started rendering something it was not written
 * for - and the base trades some of it for not having to type the same eleven
 * lines. So what belongs here is what is inert when nothing uses it, and a
 * control whose behaviour a test would need to assert belongs at the call site.
 *
 * Use it as the factory itself, so a new export is added once:
 *
 *     vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
 *       ModalRoot: ({ children }: any) => <div role="dialog">{children}</div>,
 *     }));
 */
import React from "react";
import { vi } from "vitest";

/** Steam's class names, as the panel's own stylesheet outranks them by name. */
export const gamepadDialogClasses = {
  Field: "Field",
  FieldLabel: "FieldLabel",
  FieldDescription: "FieldDescription",
  FieldLeftColumn: "FieldLeftColumn",
  FieldChildren: "FieldChildren",
  CompactPadding: "CompactPadding",
  WithBottomSeparatorStandard: "WithBottomSeparatorStandard",
  WithBottomSeparatorThick: "WithBottomSeparatorThick",
};

/**
 * The plainest rendering of each control that any file needed.
 *
 * Deliberately the plainest: a file that needs more than this says so at its
 * own call site, where the assertion that needs it can be read beside it.
 */
export function deckyUiBase(): Record<string, unknown> {
  return {
    ButtonItem: ({ children, onClick, disabled }: any) => (
      <button disabled={disabled} onClick={onClick}>{children}</button>
    ),
    DialogButton: ({ children, onClick, disabled, preferredFocus }: any) => (
      <button disabled={disabled} data-preferred-focus={preferredFocus ? "true" : undefined} onClick={onClick}>{children}</button>
    ),
    // The bare control, without the row around it: the Approve screen puts one
    // in a field of its own so that field can hold the press beside it too.
    //
    // Named by `menuLabel`, which is the real control's own prop for what the
    // menu it opens is called, and is the only name a bare dropdown carries:
    // the words beside it belong to the field around it, and a field does not
    // name its children to anything reading the page.
    Dropdown: ({ rgOptions, selectedOption, onChange, disabled, menuLabel }: any) => (
      <select
        aria-label={menuLabel}
        disabled={disabled}
        value={selectedOption ?? ""}
        onChange={(event: any) => onChange?.({ data: event.target.value })}
      >
        {(rgOptions ?? []).map((option: any, index: number) => (
          <option key={`${String(option.data)}:${index}`} value={String(option.data)}>{option.label}</option>
        ))}
      </select>
    ),
    DropdownItem: ({ label, rgOptions, selectedOption, onChange, disabled }: any) => (
      <label>{label}
        <select
          aria-label={label}
          disabled={disabled}
          value={selectedOption ?? ""}
          onChange={(event: any) => onChange?.({ data: event.target.value })}
        >
          {(rgOptions ?? []).map((option: any, index: number) => (
            <option key={`${String(option.data)}:${index}`} value={String(option.data)}>{option.label}</option>
          ))}
        </select>
      </label>
    ),
    Field: ({ label, description, children }: any) => (
      <div><span>{label}</span><span>{description}</span>{children}</div>
    ),
    // Forwards its ref, because Steam's does and a screen that measures its own
    // window asks for the element through one. A double that dropped it handed
    // the component `null`, which is the shape it falls back to on a host with
    // no layout at all, so the measurement under test was never the one that
    // runs on a device.
    Focusable: React.forwardRef(({ children, ...props }: any, ref: any) => (
      <div ref={ref} data-testid={props["data-testid"]} data-flow-children={props["flow-children"]}>{children}</div>
    )),
    ModalRoot: ({ children }: any) => <div>{children}</div>,
    // The value Steam's own enum carries, which the footer row passes through
    // to a focus container. A file that renders `Focusable` with its navigation
    // attributes asserts against this number.
    NavEntryPositionPreferences: { PREFERRED_CHILD: 4 },
    PanelSection: ({ children }: any) => <section>{children}</section>,
    PanelSectionRow: ({ children }: any) => <div>{children}</div>,
    Spinner: () => <span>Loading</span>,
    // A control with no label carries its own accessible name and its
    // placeholder, because Steam's input does: a filter labelled inside itself
    // is still named for a controller's own reading of it and for a test.
    TextField: (props: any) => (
      <label>{props.label}<input
        aria-label={props["aria-label"] ?? props.label}
        placeholder={props.placeholder}
        value={props.value}
        disabled={props.disabled}
        onChange={props.onChange}
      /></label>
    ),
    Toggle: ({ value, onChange, disabled }: any) => (
      <input type="checkbox" checked={Boolean(value)} disabled={disabled} onChange={(event: any) => onChange?.(event.target.checked)} />
    ),
    gamepadDialogClasses,
    showModal: vi.fn(() => ({ Close: vi.fn() })),
  };
}

/** The base with this file's own doubles on top of it. */
export function deckyUiMock(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return { ...deckyUiBase(), ...overrides };
}
