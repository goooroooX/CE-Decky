import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  DialogButton: ({ children, onClick, disabled, style }: any) => (
    <button disabled={disabled} data-flex={style?.flex} data-font-size={style?.fontSize} onClick={onClick}>{children}</button>
  ),
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: ({ children, navEntryPreferPosition, ...props }: any) => (
    <div data-testid="focusable" data-flow-children={props["flow-children"]} data-nav-entry={navEntryPreferPosition}>{children}</div>
  ),
  PanelSectionRow: ({ children }: any) => <div>{children}</div>,
  gamepadDialogClasses: {},
}));

import { ModalActions, modalActionStyle } from "../src/components/ModalActions";

afterEach(cleanup);

describe("a modal's bottom actions", () => {
  it("puts two of them in one group the stick moves across", () => {
    // Steam derives which control is beside which from the geometry, and two
    // siblings in a plain box are two separate steps: on the review screen
    // "Use this table" and "Cancel" sit side by side and the stick moved down
    // from one to the other rather than across.
    render(<ModalActions>
      <button style={modalActionStyle} onClick={() => undefined}>Use this table</button>
      <button style={modalActionStyle} onClick={() => undefined}>Cancel</button>
    </ModalActions>);

    const group = screen.getByTestId("focusable");
    expect(group.getAttribute("data-flow-children")).toBe("row");
    for (const name of ["Use this table", "Cancel"]) {
      expect(group.contains(screen.getByRole("button", { name }))).toBe(true);
    }
  });

  it("leaves a lone action ungrouped", () => {
    // A `Focusable` around one button is itself a focus target whose activation
    // does nothing: the button looks focused and pressing A is ignored.
    render(<ModalActions><button style={modalActionStyle} onClick={() => undefined}>Back to the list</button></ModalActions>);

    expect(screen.queryByTestId("focusable")).toBeNull();
    expect(screen.getByRole("button", { name: "Back to the list" })).toBeTruthy();
  });

  it("sizes them by their own text rather than by the width of the dialog", () => {
    // They were `flex: 1 1 0` each, so two took half the dialog apiece and one
    // took the whole of it. That makes the way out as loud as the decision
    // above it, and costs a handheld a band of height it does not have.
    expect(modalActionStyle.flex).toBe("0 0 auto");
    expect(modalActionStyle.width).toBe("auto");
    expect(modalActionStyle.fontSize).toBe(13);
  });
});
