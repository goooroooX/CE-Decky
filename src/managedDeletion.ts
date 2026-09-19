import { describeError } from "./errors";

/**
 * Whether a deletion was refused before it touched anything.
 *
 * The panel reconciles itself after a deletion even when the reply never
 * arrives, because a deletion commits before the call returns: a lost answer is
 * not a deletion that did not happen, and the panel describing files that are
 * gone is the failure that rule exists for. A refusal is the opposite case.
 * Nothing was touched - a Cheat Engine this plugin owns is still running, a
 * setup or a download or an update is still in flight - and reconciling anyway
 * made the panel forget the game the user had chosen, and the rest of their own
 * state, for a deletion that explicitly did not happen.
 *
 * The backend says so in the first words of the refusal, and this matches them:
 * `DELETION_REFUSED_PREFIX` in `py_modules/ce_decky/service.py` is the other
 * half, and `tests/test_service.py` holds it to that.
 */
export const DELETION_REFUSED_PREFIX = "nothing was deleted; ";

export function refusedBeforeDeleting(cause: unknown): boolean {
  // Read through `describeError`, because on Decky Loader 3.2.6 a backend
  // exception arrives with an empty `PyError.message` and the Python text only
  // inside `pythonTraceback`. Matching the message alone made every refusal on
  // that loader look like an outcome nobody could be sure of, which is the
  // reconciliation this exists to prevent - and 3.2.6 is a loader this project
  // has met, not a hypothesis: `src/errors.ts` was written for it.
  //
  // The prefix stays the discriminator rather than "a Python error arrived":
  // a deletion can also fail after it has begun, and that is an outcome this
  // side does have to reconcile.
  return describeError(cause, "").includes(DELETION_REFUSED_PREFIX);
}
