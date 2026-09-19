import { beforeEach, describe, expect, it, vi } from "vitest";

import { forgetSelectedGame, readMascotVisible, readSelectedGame, rememberMascotVisible, rememberSelectedGame } from "../src/selectionMemory";

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

describe("the mascot's last known state", () => {
  beforeEach(() => {
    // The suite above leaves spies that make this store throw; each block owns
    // the state it reads.
    vi.restoreAllMocks();
    window.localStorage.clear();
  });

  it("answers the frame before the backend does, and defaults to on", () => {
    // Nothing remembered yet: the panel draws what it always drew.
    expect(readMascotVisible()).toBe(true);
    rememberMascotVisible(false);
    expect(readMascotVisible()).toBe(false);
    rememberMascotVisible(true);
    expect(readMascotVisible()).toBe(true);
  });

  it("treats a blocked or damaged store as no answer rather than as off", () => {
    window.localStorage.setItem("ce-decky.mascot-visible.v1", "perhaps");
    // Only an explicit off is off; anything else is the default, because the
    // backend's answer is a moment away and guessing hidden would flash the
    // panel for everybody whose store cannot be read.
    expect(readMascotVisible()).toBe(true);
  });
});
