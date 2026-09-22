/**
 * What may not start in a game until the run of it that is going now is over.
 *
 * Two holds, both about one run of one game and both lifted the same way: by
 * the game seen not running, or running as other processes than the run the
 * hold was taken in. Those processes are recorded the first time they are
 * seen, so a restart made while another game was selected lifts a hold as
 * surely as one that was watched.
 *
 * Held in this module rather than in the panel, because the panel is remounted
 * and Auto-load would find a hold gone. A plugin reload forgets both, which is
 * the one boundary they do not cross.
 */
import type { CELaunchCapability } from "./types";

type Hold = { appId: number; pids: readonly number[] | null };

/**
 * A stop ended Cheat Engine without proving the game clean.
 *
 * What that Cheat Engine left changed can no longer be put back: its restore
 * resolves symbols belonging to the process that was ended. A table started on
 * top of it meets patches it did not write, so its scans miss, its startup
 * fails and the failure is recorded against a table that works. Every start is
 * refused, with this reason.
 */
let unclean: (Hold & { reason: string }) | null = null;

/**
 * The user stopped Cheat Engine in this game.
 *
 * Auto-load starts the table whenever the game runs without it, and a Stop
 * leaves exactly that state, so it brought Cheat Engine straight back. Only
 * Auto-load is held: a start the user makes themselves is theirs to make.
 */
let stopped: Hold | null = null;

/** Hold this game, for the reason the reader is given when a start is refused. */
export function holdUncleanGame(appId: number, reason: string): void {
  unclean = { appId, reason, pids: null };
}

/** Why no table may be started in this game now, or nothing where one may. */
export function uncleanGameReason(appId: number): string | null {
  return unclean?.appId === appId ? unclean.reason : null;
}

/** Keep Auto-load from starting this game's table again in this run of it. */
export function holdAutoloadAfterStop(appId: number): void {
  stopped = { appId, pids: null };
}

/** Whether the user stopped Cheat Engine in this run of this game. */
export function autoloadHeldAfterStop(appId: number): boolean {
  return stopped?.appId === appId;
}

/** The user started a table or switched Auto-load on: the Stop is answered. */
export function releaseAutoloadHold(appId: number): void {
  if (stopped?.appId === appId) stopped = null;
}

/** Whether this sight of the game ends the run a hold was taken in, recording that run where it is new. */
function observed<T extends Hold>(hold: T | null, appId: number | null, capability: CELaunchCapability): { hold: T | null; lifted: boolean } {
  const game = capability.game;
  if (!hold || appId !== hold.appId || !game || game.app_id !== appId) return { hold, lifted: false };
  const pids = game.pids ?? [];
  if (!game.running || (hold.pids !== null && !pids.some((pid) => hold.pids!.includes(pid)))) {
    return { hold: null, lifted: true };
  }
  if (hold.pids === null && pids.length > 0) return { hold: { ...hold, pids }, lifted: false };
  return { hold, lifted: false };
}

/**
 * Read what the launcher sees of a game, and name the holds that sight lifted.
 */
export function observeGameRun(appId: number | null, capability: CELaunchCapability): Array<"unclean" | "stopped"> {
  const lifted: Array<"unclean" | "stopped"> = [];
  const nextUnclean = observed(unclean, appId, capability);
  unclean = nextUnclean.hold;
  if (nextUnclean.lifted) lifted.push("unclean");
  const nextStopped = observed(stopped, appId, capability);
  stopped = nextStopped.hold;
  if (nextStopped.lifted) lifted.push("stopped");
  return lifted;
}

/** Forget every hold. For tests, which share this module across cases. */
export function forgetGameRunHolds(): void {
  unclean = null;
  stopped = null;
}
