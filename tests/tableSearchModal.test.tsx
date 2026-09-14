import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  DialogButton: ({ children, onClick, disabled }: any) => <button disabled={disabled} onClick={onClick}>{children}</button>,
  Field: ({ label, description, children }: any) => <div><span>{label}</span><span>{description}</span>{children}</div>,
  Focusable: ({ children }: any) => <div>{children}</div>,
  gamepadDialogClasses: { Field: "Field", FieldLabel: "FieldLabel", FieldDescription: "FieldDescription", CompactPadding: "CompactPadding" },
  ModalRoot: ({ children, onCancel }: any) => <div><button aria-label="Controller Back" onClick={onCancel}>Controller Back</button>{children}</div>,
}));

vi.mock("../src/providerCatalog", () => ({
  // The catalog owns the footer row the modal's own Close now sits on, so the
  // stand-in has to render what the modal hands it or Close is not on screen at
  // all and this test passes by asserting on nothing.
  ProviderCatalog: ({ onLocalSelected, footerActions }: any) => (
    <div>
      <button onClick={() => void onLocalSelected("1".repeat(64))}>Pick local table</button>
      {footerActions}
    </div>
  ),
}));

import { TableSearchModal } from "../src/modals/TableSearchModal";

describe("TableSearchModal transactional handoff", () => {
  it("does not let Back cancel a selected-table commit before Review handoff completes", async () => {
    let finish!: () => void;
    const pending = new Promise<void>((resolve) => { finish = resolve; });
    const onSelected = vi.fn(() => pending);
    const onCancel = vi.fn();

    render(<TableSearchModal
      gameIdentity="10:steam"
      gameName="Game"
      userHome="/home/deck"
      localArtifacts={{}}
      localTables={[]}
      importedArtifacts={new Set()}
      onSelected={onSelected}
      onCancel={onCancel}
    />);

    fireEvent.click(screen.getByRole("button", { name: "Pick local table" }));
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onSelected).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Close" }) as HTMLButtonElement).disabled).toBe(true);

    finish();
    await waitFor(() => expect((screen.getByRole("button", { name: "Close" }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: "Controller Back" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
