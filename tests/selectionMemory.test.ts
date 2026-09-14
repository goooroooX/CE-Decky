import { beforeEach, describe, expect, it, vi } from "vitest";

import { forgetSelectedGame, readSelectedGame, rememberSelectedGame } from "../src/selectionMemory";

const KEY = "ce-decky.selected-game.v1";

describe("selected-game memory", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
  });

  it.each([
    "not json",
    "null",
    "[]",
    JSON.stringify({ appId: 0, isShortcut: false }),
    JSON.stringify({ appId: -1, isShortcut: false }),
    JSON.stringify({ appId: 1.5, isShortcut: false }),
    JSON.stringify({ appId: 0x1_0000_0000, isShortcut: false }),
    JSON.stringify({ appId: "10", isShortcut: false }),
    JSON.stringify({ appId: 10, isShortcut: "false" }),
  ])("rejects malformed or invalid stored selection %#", (raw) => {
    window.localStorage.setItem(KEY, raw);
    expect(readSelectedGame()).toBeNull();
  });

  it("round-trips an explicit Steam library identity", () => {
    rememberSelectedGame({ appId: 620, isShortcut: false });
    expect(readSelectedGame()).toEqual({ appId: 620, isShortcut: false });

    forgetSelectedGame();
    expect(readSelectedGame()).toBeNull();
  });

  it("degrades to no remembered selection when storage is unavailable", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(readSelectedGame()).toBeNull();

    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => rememberSelectedGame({ appId: 620, isShortcut: false })).not.toThrow();

    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("blocked"); });
    expect(() => forgetSelectedGame()).not.toThrow();
  });
});
