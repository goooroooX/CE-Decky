import { describeError, pythonExceptionClass, pythonTracebackSummary } from "./errors";
import type { ConfiguredValue, StartupPreference } from "./types";

/**
 * The backend's name for a write that failed after its content was in place.
 *
 * `atomic_write_bytes` publishes the replacement with `os.replace` and only
 * then syncs the directory entry, and a POSIX failure there is re-raised. The
 * new authority is visible to every reader at that point - including a resident
 * Cheat Engine reading its control file - so this exact exception is the one
 * Python traceback that is not proof the backend refused.
 */
const DURABILITY_UNKNOWN_EXCEPTION = "DurabilityUnknownError";

/** Whether this rejection is a backend refusal that happened before any commit. */
export function isPreCommitRefusal(cause: unknown): boolean {
  const traceback = (cause as { pythonTraceback?: unknown })?.pythonTraceback;
  if (pythonTracebackSummary(traceback) === null) return false;
  return pythonExceptionClass(traceback) !== DURABILITY_UNKNOWN_EXCEPTION;
}

/**
 * A durable write whose outcome could not be established.
 *
 * Raised only when the write may have been committed and the authority that
 * would settle it could not be re-read either. The caller must not offer
 * ordinary "nothing was saved" semantics for this.
 */
export class DurableOutcomeUnknownError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DurableOutcomeUnknownError";
  }
}

/**
 * A rejection that a *previous* durable write in the same action survived.
 *
 * One user action can commit several independent backend mutations, and each
 * one is durable the moment it returns. Rethrowing the later failure on its own
 * described the action as if nothing had happened, so the caller never learned
 * that the earlier write is already stored. Wrap the later failure in this and
 * the surfaces that name partial commits can say so.
 */
export class PriorDurableCommitError extends Error {
  readonly cause: unknown;
  /** Whether the failing write itself also has an unestablished outcome. */
  readonly outcomeUnknown: boolean;

  constructor(cause: unknown, committedSubject: string) {
    super(`${describeError(cause)} ${committedSubject} was already saved and stays saved.`);
    this.name = "PriorDurableCommitError";
    this.cause = cause;
    this.outcomeUnknown = cause instanceof DurableOutcomeUnknownError;
  }
}

/** What an aborted multi-write action left durably behind. */
export function durableResidue(cause: unknown): { committed: boolean; unknown: boolean } {
  if (cause instanceof PriorDurableCommitError) {
    return { committed: true, unknown: cause.outcomeUnknown };
  }
  return { committed: false, unknown: cause instanceof DurableOutcomeUnknownError };
}

/**
 * Commit an exact desired state and reconcile a lost reply.
 *
 * Backend profile mutations are atomic and fully inspectable afterwards, but a
 * rejected Decky callable was being treated as proof the write did not happen -
 * so a saved configuration could be reported as a failed Apply, and the user
 * offered a Discard for state that was already durable.
 *
 * A Python traceback is proof the backend itself refused, with one exception it
 * names: a write that failed after `os.replace` had already published its
 * content is not a refusal, because the new state is already the one every
 * reader sees. Everything else is transport loss. Both of the non-refusal cases
 * ask the authority what the exact desired state actually is.
 *
 * `write` must perform exactly one backend mutation. That inference only holds
 * per mutation: grouping two of them behind one call made a traceback from the
 * second stand for the first as well, so a committed write was reported as
 * nothing having happened. Reconcile each one separately and wrap the later
 * failure in `PriorDurableCommitError`.
 */
export async function commitDesiredState(input: {
  write: () => Promise<unknown>;
  /** Re-read the exact authority; true when the desired state is present. */
  verify: () => Promise<boolean>;
  /** What was being saved, for the unknown-outcome message. */
  subject: string;
}): Promise<void> {
  try {
    await input.write();
    return;
  } catch (cause) {
    if (isPreCommitRefusal(cause)) {
      throw cause;
    }
    let present: boolean;
    try {
      present = await input.verify();
    } catch {
      throw new DurableOutcomeUnknownError(
        `${describeError(cause)} CE Decky could not confirm whether ${input.subject} was saved; refresh before deciding what to do.`,
      );
    }
    if (present) return;
    // The content was already published when the backend raised, so an
    // authority that does not hold it is a contradiction, not a refusal.
    if (!isPreCommitRefusal(cause) && pythonTracebackSummary((cause as { pythonTraceback?: unknown })?.pythonTraceback) !== null) {
      throw new DurableOutcomeUnknownError(
        `${describeError(cause)} CE Decky could not establish whether ${input.subject} was saved; refresh before deciding what to do.`,
      );
    }
    throw cause;
  }
}


/**
 * Re-label a failed commit with the context only the caller has, without
 * turning an unestablished outcome into a definite one.
 *
 * A caller that has already changed something else - the running game, most of
 * all - needs to say so alongside the failure. Prefixing the message flattened
 * the one case the helper exists to distinguish: an unknown outcome was
 * announced as "it was not remembered", immediately followed by the helper's own
 * "could not confirm whether it was saved", which contradicts it. The unknown
 * branch keeps its type, so anything downstream that treats an unestablished
 * outcome differently still can.
 */
export function describeCommitFailure(cause: unknown, wording: {
  /** Stated before the error when the backend definitely refused. */
  definite: string;
  /** Stated before the helper's own account when the outcome is unknown. */
  unknown: string;
}): Error {
  if (cause instanceof DurableOutcomeUnknownError) {
    return new DurableOutcomeUnknownError(`${wording.unknown} ${cause.message}`);
  }
  return new Error(`${wording.definite} ${describeError(cause)}`);
}

/** Whether the profile already holds exactly this configured-value set. */
export function configuredValuesMatch(
  stored: readonly ConfiguredValue[] | undefined,
  desired: readonly ConfiguredValue[],
): boolean {
  const present = new Map((stored ?? []).map((item) => [item.record_id, item.value]));
  if (present.size !== desired.length) return false;
  return desired.every((item) => present.get(item.record_id) === item.value);
}

/** Whether the profile already holds exactly this remembered selection. */
export function rememberedMatches(
  stored: readonly StartupPreference[] | undefined,
  desired: readonly StartupPreference[],
): boolean {
  const present = new Map((stored ?? []).map((item) => [item.record_id, item]));
  if (present.size !== desired.length) return false;
  return desired.every((item) => {
    const found = present.get(item.record_id);
    return Boolean(found) && found!.active === item.active && found!.value === item.value;
  });
}
