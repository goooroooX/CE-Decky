import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// A Steam whose scroll panel the library could not find: the help still has
// to be reachable, so each line is a stop of its own in a box the browser
// scrolls.
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ScrollPanelGroup: undefined,
  scrollPanelClasses: undefined,
}));

import { ScrollableHelp } from "../src/components/ScrollableHelp";

describe("long help where Steam's scroll panel is missing", () => {
  it("is lines a controller can still reach and scroll through", () => {
    render(<ScrollableHelp testId="help" lines={["One.", "Two.", "Three."]} />);
    const box = screen.getByTestId("help");
    expect(box.querySelector("[data-scroll-panel]")).toBeNull();
    expect(box.style.overflowY).toBe("auto");
    expect([...box.querySelectorAll(":scope > div")].map((line) => line.textContent)).toEqual(["One.", "Two.", "Three."]);
  });
});
