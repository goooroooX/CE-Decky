import { beforeEach, describe, expect, it } from "vitest";

import {
  autoloadHeldAfterStop, forgetGameRunHolds, holdAutoloadAfterStop, holdUncleanGame, observeGameRun, releaseAutoloadHold, uncleanGameReason,
} from "../src/gameRunHolds";

const seen = (appId: number, running: boolean, pids: number[]) => ({ game: { app_id: appId, running, pids } } as any);

describe("a game a stop could not prove clean", () => {
  beforeEach(() => forgetGameRunHolds());

  it("stays held for the run it was taken in, and only that game", () => {
    holdUncleanGame(10, "Restart the game");
    expect(uncleanGameReason(10)).toBe("Restart the game");
    expect(uncleanGameReason(20)).toBeNull();
    // The first sight of it records which run that is; the same run keeps it.
    expect(observeGameRun(10, seen(10, true, [42]))).toEqual([]);
    expect(observeGameRun(10, seen(10, true, [42, 43]))).toEqual([]);
    // Another game's answer says nothing about this one.
    expect(observeGameRun(20, seen(20, false, []))).toEqual([]);
    expect(uncleanGameReason(10)).toBe("Restart the game");
  });

  it("is lifted by the game being seen not running", () => {
    holdUncleanGame(10, "Restart the game");
    expect(observeGameRun(10, seen(10, false, []))).toEqual(["unclean"]);
    expect(uncleanGameReason(10)).toBeNull();
  });

  it("is lifted by a run of other processes, seen after a restart nobody watched", () => {
    holdUncleanGame(10, "Restart the game");
    observeGameRun(10, seen(10, true, [42]));
    expect(observeGameRun(10, seen(10, true, [77]))).toEqual(["unclean"]);
    expect(uncleanGameReason(10)).toBeNull();
  });

  it("keeps Auto-load from starting the table again after a Stop, for that run only", () => {
    holdAutoloadAfterStop(10);
    expect(autoloadHeldAfterStop(10)).toBe(true);
    expect(autoloadHeldAfterStop(20)).toBe(false);
    // The same run keeps it; a restart lifts it by the other hold's rule.
    expect(observeGameRun(10, seen(10, true, [42]))).toEqual([]);
    expect(observeGameRun(10, seen(10, true, [99]))).toEqual(["stopped"]);
    expect(autoloadHeldAfterStop(10)).toBe(false);
    // And a start the user makes, or Auto-load switched on, answers the Stop.
    holdAutoloadAfterStop(10);
    releaseAutoloadHold(20);
    expect(autoloadHeldAfterStop(10)).toBe(true);
    releaseAutoloadHold(10);
    expect(autoloadHeldAfterStop(10)).toBe(false);
  });
});
