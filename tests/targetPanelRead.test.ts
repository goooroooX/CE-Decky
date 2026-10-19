import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

const source = readFileSync("scripts/target_panel_read.py", "utf8");
// These expressions are shipped by the Python helper to the device's page.
const read = source.match(/_READ = """([\s\S]*?)"""/)![1]
  .replaceAll("\\\\", "\\").replace("%(depth)d", "12");
const press = source.match(/_PRESS = r"""([\s\S]*?)"""/)![1];

afterEach(() => {
  document.body.innerHTML = "";
  Reflect.deleteProperty(document, "elementFromPoint");
  vi.restoreAllMocks();
});

describe("target panel reader's modal navigation", () => {
  it.each(["modal-actions", "table-code-footer"])("reads and presses the external %s", (id) => {
    document.body.innerHTML = `<main><div class="ce-decky-dense"><div data-testid="table-code-summary">Table</div></div>
      <div data-testid="${id}"><button>Back</button></div></main>`;
    Object.defineProperty(document, "elementFromPoint", { configurable: true, value: () => document.body });
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockImplementation(function (this: HTMLElement) {
      return this.hidden ? [] as unknown as DOMRectList : [new DOMRect()] as unknown as DOMRectList;
    });
    const button = document.querySelector("button")!;
    const click = vi.fn();
    button.addEventListener("click", click);
    const report = JSON.parse(window.eval(read));
    expect(report.surfaces[0].rows.find((row: { testid: string }) => row.testid === id).controls[0].label).toBe("Back");
    const expression = press.replace("%(label)s", JSON.stringify("Back")).replace("%(scope)s", JSON.stringify(id));
    expect(JSON.parse(window.eval(expression))).toMatchObject({ pressed: true, where: id });
    expect(click).toHaveBeenCalledOnce();
    button.hidden = true;
    expect(JSON.parse(window.eval(expression)).pressed).toBe(false);
    expect(click).toHaveBeenCalledOnce();
  });

  it("does not let a footer inside the block hide the one outside it", () => {
    document.body.innerHTML = `<main><div class="ce-decky-dense"><div data-testid="catalog-footer"><button>Next</button></div></div>
      <div data-testid="modal-actions"><button>Use</button></div></main>`;
    Object.defineProperty(document, "elementFromPoint", { configurable: true, value: () => document.body });
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockImplementation(() => [new DOMRect()] as unknown as DOMRectList);
    const use = vi.fn();
    document.querySelector('[data-testid="modal-actions"] button')!.addEventListener("click", use);
    const rows = JSON.parse(window.eval(read)).surfaces[0].rows.map((row: { testid: string }) => row.testid);
    expect(rows.filter((id: string) => id === "catalog-footer")).toHaveLength(1);
    expect(rows).toContain("modal-actions");
    const expression = press.replace("%(label)s", JSON.stringify("Use")).replace("%(scope)s", JSON.stringify("modal-actions"));
    expect(JSON.parse(window.eval(expression))).toMatchObject({ pressed: true, where: "modal-actions" });
    expect(use).toHaveBeenCalledOnce();
  });
});
