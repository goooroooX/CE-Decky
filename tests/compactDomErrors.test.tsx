import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { pageOutline } from "./setup/compactDomErrors";

describe("a failed query", () => {
  it("prints the rows it could have found, without the stylesheet", () => {
    render(
      <div>
        <style>{".ce-decky-dense { --ce-heading-font-size: 12px; }"}</style>
        <div data-testid="pinned-cheat-7">Infinite Health · pinned</div>
        <div data-testid="runtime-row">1 active (1 script)</div>
      </div>,
    );

    let message = "";
    try {
      screen.getByTestId("pinned-cheat-32");
    } catch (error) {
      message = String((error as Error).message);
      expect((error as Error).name).toBe("TestingLibraryElementError");
    }
    expect(message).toContain('Unable to find an element by: [data-testid="pinned-cheat-32"]');
    expect(message).toContain("[pinned-cheat-7] Infinite Health · pinned");
    expect(message).toContain("[runtime-row] 1 active (1 script)");
    expect(message).not.toContain("--ce-heading-font-size");
  });

  it("stays bounded on a page with thousands of rows", () => {
    const container = document.createElement("div");
    for (let index = 0; index < 2000; index += 1) {
      const row = document.createElement("div");
      row.setAttribute("data-testid", `row-${index}`);
      row.textContent = "x".repeat(300);
      container.appendChild(row);
    }
    expect(pageOutline(container).length).toBeLessThanOrEqual(6001);
  });
});
