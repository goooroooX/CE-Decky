import { useEffect, useRef } from "react";
import type { CompatibilityEvidence } from "./types";
import { logUiFailure, logUiWarning } from "./supportLog";

/**
 * How often, and for how long, a screen asks again for a compatibility record
 * whose comparison is still being made.
 *
 * A game whose executable declares no version is compared by the executable's
 * own bytes, and the backend reads them in the background rather than inside
 * the call that answers the screen. The answer is `pending` until that read is
 * done, which was measured well under a second for an executable of half a
 * gigabyte. A screen that is open when the read finishes should show it without
 * being closed and opened again, and a screen with nothing pending should not
 * be polling at all. The bound is the backend's own read deadline with room to
 * spare: past it, the answer is not coming from this read.
 */
export const PENDING_COMPATIBILITY_DELAY_MS = 3000;
export const PENDING_COMPATIBILITY_ATTEMPTS = 20;

export function hasPendingCompatibility(entries: readonly CompatibilityEvidence[] | undefined): boolean {
  return Boolean(entries?.some((entry) => entry.pending));
}

/**
 * Ask again, a bounded number of times, while any of `entries` is pending.
 *
 * Each answer is a new list, so each one schedules at most the next ask; a
 * list with nothing pending schedules nothing and resets the count. The timer
 * belongs to the screen and goes with it.
 */
export function usePendingCompatibilityRefresh(
  entries: readonly CompatibilityEvidence[] | undefined,
  refresh: () => Promise<unknown>,
  surface: string,
): void {
  const attempts = useRef(0);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const pending = hasPendingCompatibility(entries);
  useEffect(() => {
    if (!pending) {
      attempts.current = 0;
      return;
    }
    if (attempts.current >= PENDING_COMPATIBILITY_ATTEMPTS) {
      if (attempts.current === PENDING_COMPATIBILITY_ATTEMPTS) {
        attempts.current += 1;
        logUiWarning("compatibility.pending_gave_up", { surface, attempts: PENDING_COMPATIBILITY_ATTEMPTS });
      }
      return;
    }
    const timer = setTimeout(() => {
      attempts.current += 1;
      refreshRef.current().catch((cause) => logUiFailure("compatibility.pending_refresh_failed", cause, { surface }));
    }, PENDING_COMPATIBILITY_DELAY_MS);
    return () => clearTimeout(timer);
  }, [entries, pending, surface]);
}
