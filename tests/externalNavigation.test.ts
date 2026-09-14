import { beforeEach, describe, expect, it, vi } from "vitest";

const deckyUi = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  Navigation: { NavigateToExternalWeb: deckyUi.navigate },
}));

import { Navigation } from "@decky/ui";
import { openExternalWeb } from "../src/externalNavigation";

describe("external web navigation", () => {
  beforeEach(() => vi.clearAllMocks());

  it.each([
    "http://example.com",
    "https://user:secret@example.com",
    "not a URL",
    `https://example.com/${"é".repeat(4100)}`,
    `https://example.com/${" ".repeat(3000)}x`,
  ])("rejects an unsafe or oversized URL %#", (url) => {
    expect(() => openExternalWeb(url)).toThrow();
    expect(deckyUi.navigate).not.toHaveBeenCalled();
  });

  it("uses Decky's external navigation with a normalized HTTPS URL", () => {
    openExternalWeb("https://example.com/path");
    expect(deckyUi.navigate).toHaveBeenCalledWith("https://example.com/path");
  });

  it("falls back to a noopener browser window when Decky navigation is unavailable", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const original = Navigation.NavigateToExternalWeb;
    (Navigation as any).NavigateToExternalWeb = undefined;
    try {
      openExternalWeb("https://example.com/help");
      expect(open).toHaveBeenCalledWith("https://example.com/help", "_blank", "noopener,noreferrer");
    } finally {
      (Navigation as any).NavigateToExternalWeb = original;
    }
  });
});
