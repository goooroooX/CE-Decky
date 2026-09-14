import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ModalRoot: ({ children }: any) => <div role="dialog">{children}</div>,
  Focusable: ({ children }: any) => <div>{children}</div>,
}));
vi.mock("../src/components/PanelDensity", () => ({
  DensePanel: ({ children }: any) => <div>{children}</div>,
  SmallButton: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  // The class a window carries so a short screen can take back half of the
  // padding Steam draws around it. Inert here, and named because the screen
  // under test is a window.
  WINDOW_CLASS: "ce-decky-window",
}));
// Both real catalog handoff entrypoints call the same Search-owned boundary.
// Acquisition closes its own modal before calling onImported.
vi.mock("../src/providerCatalog", () => ({
  ProviderCatalog: ({ onLocalSelected, onImported, footerActions }: any) => <>
    <button onClick={() => void onLocalSelected("a".repeat(64))}>Use local row</button>
    <button onClick={() => void onImported("a".repeat(64))}>Imported acquisition</button>
    {footerActions}
  </>,
}));
import { TableSearchModal } from "../src/modals/TableSearchModal";

afterEach(cleanup);
it.each(["Use local row", "Imported acquisition"])("keeps a failed %s handoff visible inside Search", async (route) => {
  const onSelected = vi.fn().mockRejectedValue(new Error("Review preparation failed"));
  const onCancel = vi.fn();
  render(<TableSearchModal gameIdentity="10:steam" gameName="Game" localArtifacts={{}}
    localTables={[]} importedArtifacts={new Set()} blockedTables={{}} blockedArtifacts={{}}
    onClearMarks={vi.fn()} onRefreshBlocked={vi.fn()} onSelected={onSelected} onCancel={onCancel} />);
  fireEvent.click(screen.getByRole("button", { name: route }));
  expect((await screen.findByRole("alert")).textContent).toBe("Review preparation failed");
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(onCancel).not.toHaveBeenCalled();
  expect((screen.getByRole("button", { name: "Close" }) as HTMLButtonElement).disabled).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: route }));
  expect(onSelected).toHaveBeenCalledTimes(2);
});
