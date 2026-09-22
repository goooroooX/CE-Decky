/**
 * The game a stop ended Cheat Engine in without proving it clean, and why no
 * table may be started in it until that run of the game is over.
 *
 * What that Cheat Engine left changed can no longer be put back: its restore
 * resolves symbols belonging to the process that was ended. A table started on
 * top of it meets patches it did not write, so its scans miss, its startup
 * fails and the failure is recorded against a table that works.
 *
 * Held in this module rather than in the panel, because the panel is remounted
 * and Auto-load would find the hold gone. The processes of the run it was taken
 * in are recorded the first time they are seen, so a restart made while another
 * game was selected lifts it as surely as one that was watched. A plugin reload
 * forgets it, which is the one boundary it does not cross.
 */
import type { CELaunchCapability } from "./types";

let held: { appId: number; reason: string; pids: readonly number[] | null } | null = null;

/** Hold this game, for the reason the reader is given when a start is refused. */
export function holdUncleanGame(appId: number, reason: string): void {
  held = { appId, reason, pids: null };
}

/** Why no table may be started in this game now, or nothing where one may. */
export function uncleanGameReason(appId: number): string | null {
  return held?.appId === appId ? held.reason : null;
}

/**
 * Read what the launcher sees of the held game, and say whether that lifted it.
 *
 * A game seen not running, or running as other processes than the run the hold
 * was taken in, has taken whatever was left in it with it.
 */
export function observeUncleanGame(appId: number | null, capability: CELaunchCapability): boolean {
  const game = capability.game;
  if (!held || appId !== held.appId || !game || game.app_id !== appId) return false;
  const pids = game.pids ?? [];
  if (!game.running || (held.pids !== null && !pids.some((pid) => held!.pids!.includes(pid)))) {
    held = null;
    return true;
  }
  if (held.pids === null && pids.length > 0) held = { ...held, pids };
  return false;
}

/** Forget every hold. For tests, which share this module across cases. */
export function forgetUncleanGames(): void {
  held = null;
}
