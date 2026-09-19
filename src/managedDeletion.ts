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
  const message = cause instanceof Error ? cause.message : typeof cause === "string" ? cause : "";
  return message.includes(DELETION_REFUSED_PREFIX);
}
