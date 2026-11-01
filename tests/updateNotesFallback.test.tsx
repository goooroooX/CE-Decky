import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// A Steam whose scroll panel the library could not find: the notes still have
// to be reachable, so each line is a stop of its own in a box the browser
// scrolls.
vi.mock("@decky/ui", async () => (await import("./deckyUiMock")).deckyUiMock({
  ScrollPanelGroup: undefined,
  scrollPanelClasses: undefined,
}));

import { UpdateModal } from "../src/modals/UpdateModal";

describe("the release notes where Steam's scroll panel is missing", () => {
  it("are lines a controller can still reach and scroll through", () => {
    render(<UpdateModal
      currentVersion="0.9.27" targetVersion="0.9.28"
      notes={"Why to update.\n\n## Fixes\n\n- One.\n- Two."}
      onStart={vi.fn()} onPoll={vi.fn()} onCancelUpdate={vi.fn()} onClose={vi.fn()}
    />);
    const box = screen.getByTestId("update-notes");
    expect(box.querySelector("[data-scroll-panel]")).toBeNull();
    expect(box.style.overflowY).toBe("auto");
    expect(screen.getByTestId("update-note-2").textContent).toBe("• Two.");
  });
});
