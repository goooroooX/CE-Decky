import { beforeEach, describe, expect, it } from "vitest";

import { forgetUncleanGames, holdUncleanGame, observeUncleanGame, uncleanGameReason } from "../src/uncleanGame";

const seen = (appId: number, running: boolean, pids: number[]) => ({ game: { app_id: appId, running, pids } } as any);

describe("a game a stop could not prove clean", () => {
  beforeEach(() => forgetUncleanGames());

  it("stays held for the run it was taken in, and only that game", () => {
    holdUncleanGame(10, "Restart the game");
    expect(uncleanGameReason(10)).toBe("Restart the game");
    expect(uncleanGameReason(20)).toBeNull();
    // The first sight of it records which run that is; the same run keeps it.
    expect(observeUncleanGame(10, seen(10, true, [42]))).toBe(false);
    expect(observeUncleanGame(10, seen(10, true, [42, 43]))).toBe(false);
    // Another game's answer says nothing about this one.
    expect(observeUncleanGame(20, seen(20, false, []))).toBe(false);
    expect(uncleanGameReason(10)).toBe("Restart the game");
  });

  it("is lifted by the game being seen not running", () => {
    holdUncleanGame(10, "Restart the game");
    expect(observeUncleanGame(10, seen(10, false, []))).toBe(true);
    expect(uncleanGameReason(10)).toBeNull();
  });

  it("is lifted by a run of other processes, seen after a restart nobody watched", () => {
    holdUncleanGame(10, "Restart the game");
    observeUncleanGame(10, seen(10, true, [42]));
    expect(observeUncleanGame(10, seen(10, true, [77]))).toBe(true);
    expect(uncleanGameReason(10)).toBeNull();
  });
});
