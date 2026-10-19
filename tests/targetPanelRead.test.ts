import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

const source = readFileSync("scripts/target_panel_read.py", "utf8");
// These expressions are shipped by the Python helper to the device's page.
const screen = source.match(/_SCREEN = r"""([\s\S]*?)"""/)![1];
const read = source.match(/_READ = """([\s\S]*?)"""/)![1]
  .replaceAll("\\\\", "\\").replace("%(depth)d", "12").replace("%(screen)s", screen);
const press = source.match(/_PRESS = r"""([\s\S]*?)"""/)![1].replace("%(screen)s", screen);
const choose = source.match(/_CHOOSE = r"""([\s\S]*?)"""/)![1].replace("%(screen)s", screen);
const pressing = (label: string, scope: string | null = null) =>
  press.replace("%(label)s", JSON.stringify(label)).replace("%(scope)s", JSON.stringify(scope));
const choosing = (option: string) =>
  choose.replace("%(option)s", JSON.stringify(option)).replace("%(scope)s", "null");

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

describe("target panel reader's screens", () => {
  // `front` is whatever the page says is on top at any point; null is a page
  // with no layout to ask, which must fall back to the document's order.
  const layout = (front: Element | null) => {
    Object.defineProperty(document, "elementFromPoint", { configurable: true, value: () => front ?? document.body });
    vi.spyOn(HTMLElement.prototype, "getClientRects").mockImplementation(function (this: HTMLElement) {
      return this.hidden ? [] as unknown as DOMRectList : [new DOMRect()] as unknown as DOMRectList;
    });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(() => new DOMRect(0, 0, 100, 40));
  };

  it("reads and presses a modal's own way out that sits outside its block", () => {
    document.body.innerHTML = `<form role="dialog"><div class="ce-decky-dense"><div data-testid="update-state">Up to date</div></div>
      <div><button>Close</button></div></form>`;
    layout(null);
    const close = vi.fn();
    document.querySelector("button")!.addEventListener("click", close);
    const rows = JSON.parse(window.eval(read)).surfaces[0].rows;
    expect(rows.find((row: { testid: string }) => row.testid === "untagged-controls").text).toBe("Close");
    expect(JSON.parse(window.eval(pressing("Close")))).toMatchObject({ pressed: true });
    expect(close).toHaveBeenCalledOnce();
  });

  it("presses the control on the screen in front, not the last one in the document", () => {
    document.body.innerHTML = `<form role="dialog" id="front"><div class="ce-decky-dense"><button>Use this game</button></div></form>
      <form role="dialog" id="behind"><div class="ce-decky-dense"><button>Use this game</button></div></form>`;
    const [front, behind] = Array.from(document.querySelectorAll("button"));
    layout(front);
    const pressedFront = vi.fn();
    const pressedBehind = vi.fn();
    front.addEventListener("click", pressedFront);
    behind.addEventListener("click", pressedBehind);
    expect(JSON.parse(window.eval(pressing("Use this game")))).toMatchObject({ pressed: true });
    expect(pressedFront).toHaveBeenCalledOnce();
    expect(pressedBehind).not.toHaveBeenCalled();
  });

  const dropdowns = () => {
    document.body.innerHTML = `<div class="contextmenu"><div class="item">Half-Life 2</div></div>
      <form role="dialog" id="front"><div class="ce-decky-dense"><div class="dropdown">Borderlands 2</div></div></form>
      <form role="dialog" id="behind"><div class="ce-decky-dense"><div class="dropdown">GE-Proton10-30</div></div></form>`;
    const [front, behind] = Array.from(document.querySelectorAll<HTMLElement>(".dropdown"));
    const stale = vi.fn();
    document.querySelector(".contextmenu .item")!.addEventListener("click", stale);
    const openedBehind = vi.fn();
    behind.addEventListener("click", openedBehind);
    const picked: string[] = [];
    front.addEventListener("click", () => {
      const menu = document.createElement("div");
      menu.className = "contextmenu";
      for (const label of ["Borderlands 2", "Half-Life 2"]) {
        const item = document.createElement("div");
        item.className = "item";
        item.textContent = label;
        item.addEventListener("click", () => { picked.push(label); front.textContent = label; menu.remove(); });
        menu.append(item);
      }
      document.body.append(menu);
    });
    layout(front);
    return { front, stale, openedBehind, picked };
  };

  it("chooses in the dropdown in front, from the menu it just opened, and checks what it shows", async () => {
    const { stale, openedBehind, picked } = dropdowns();
    const answer = JSON.parse(await window.eval(choosing("Half-Life 2")));
    expect(answer).toMatchObject({ chosen: true, option: "Half-Life 2", was: "Borderlands 2", verified: true });
    expect(picked).toEqual(["Half-Life 2"]);
    expect(stale).not.toHaveBeenCalled();
    expect(openedBehind).not.toHaveBeenCalled();
  });

  it("closes a menu it refused by picking the value the control already had", async () => {
    const { front, picked } = dropdowns();
    const answer = JSON.parse(await window.eval(choosing("Atomic Heart")));
    expect(answer).toMatchObject({ chosen: false, menu: "closed by picking the value it already had" });
    expect(picked).toEqual(["Borderlands 2"]);
    expect(front.textContent).toBe("Borderlands 2");
    expect(document.querySelectorAll(".contextmenu")).toHaveLength(1);
  });
});
