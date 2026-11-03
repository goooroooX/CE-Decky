import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock());

import { ScrollableHelp } from "../src/components/ScrollableHelp";

// jsdom lays nothing out, and an unmeasured box counts as overflowing by
// design, so each case gives the panel the heights a device would. Both are
// Element's own, which is where they are put back.
function boxHeights(client: number, scroll: number) {
  vi.spyOn(Element.prototype, "clientHeight", "get").mockReturnValue(client);
  vi.spyOn(Element.prototype, "scrollHeight", "get").mockReturnValue(scroll);
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("help that can run past a handheld's screen", () => {
  it("is one ringless stop in Steam's scroll panel while it overflows, so the D-pad scrolls it", () => {
    // Measured as fitting first, then as overflowing once it grows: the stop
    // follows the measurement rather than the default an unmeasured box gets.
    boxHeights(300, 200);
    const view = render(<ScrollableHelp testId="help" lines={["One."]} />);
    expect(screen.getByTestId("help").querySelector("[data-focusable]")?.getAttribute("data-focusable")).toBe("false");
    boxHeights(300, 900);
    view.rerender(<ScrollableHelp testId="help" lines={["One.", "Two."]} />);
    const box = screen.getByTestId("help");
    const stops = box.querySelectorAll("[data-focusable]");
    expect(box.querySelector("[data-scroll-panel]")).not.toBeNull();
    expect(stops).toHaveLength(1);
    expect(stops[0].getAttribute("data-focusable")).toBe("true");
    expect(stops[0].getAttribute("data-no-focus-ring")).toBe("true");
  });

  it("is no stop at all once it fits, so short help adds nothing to walk past", () => {
    boxHeights(200, 120);
    render(<ScrollableHelp testId="help" lines={["Timed out."]} />);
    expect(screen.getByTestId("help").querySelector("[data-focusable]")?.getAttribute("data-focusable")).toBe("false");
  });

  it("stays reachable where it cannot be measured", () => {
    boxHeights(0, 0);
    render(<ScrollableHelp testId="help" lines={["One."]} />);
    expect(screen.getByTestId("help").querySelector("[data-focusable]")?.getAttribute("data-focusable")).toBe("true");
  });
});
