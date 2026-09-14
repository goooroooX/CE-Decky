import { getProviderSources } from "./api";
import { commitDesiredState } from "./durableWrite";
import { forgetSearchOutcomes } from "./providerCatalog";
import type { ProviderSourcesSnapshot } from "./types";

/**
 * Save a choice of table sources and reconcile a lost reply against the record.
 *
 * These are durable writes like every other one in the panel, and sending them
 * straight through the ordinary action path gave them the semantics this
 * project already knows is wrong: a rejected Decky callable, or a failure
 * raised after the file had already been replaced, was reported as "the switch
 * failed" for a choice that is stored. `commitDesiredState` re-reads the
 * authority instead and settles it.
 *
 * The predicate takes the whole snapshot rather than one source. Applying a
 * single-provider test to every source in the roster - which is what this did -
 * is false for every provider whose ID is not the one being switched, so the
 * one path that exists to recognise a committed write reported every one of
 * them as uncommitted.
 *
 * Cache invalidation runs on every outcome, including the failures: a commit
 * whose reply was lost changed the same file a successful one does, so leaving
 * the cached search outcomes alone there is exactly how one of them outlives
 * the choice that invalidated it.
 */
export async function commitSourceSelection(
  subject: string,
  write: () => Promise<ProviderSourcesSnapshot>,
  satisfied: (snapshot: ProviderSourcesSnapshot) => boolean,
): Promise<ProviderSourcesSnapshot | null> {
  let written: ProviderSourcesSnapshot | null = null;
  let reread: ProviderSourcesSnapshot | null = null;
  try {
    await commitDesiredState({
      subject: `${subject} was saved`,
      write: async () => { written = await write(); },
      verify: async () => {
        reread = await getProviderSources();
        return satisfied(reread);
      },
    });
  } finally {
    forgetSearchOutcomes();
  }
  // Whichever read already established this, and never a third one. A commit
  // returns here having either written a snapshot or read one back to settle
  // itself, so asking the authority again could only add a read whose failure
  // would report an established commit as a failed action. Null on the paths
  // that leave neither, and the caller re-reads for display, which cannot turn
  // a saved choice into a failure because the write has already returned.
  return written ?? reread;
}

/** Whether the snapshot holds exactly this source's desired switch position. */
export function sourceSwitched(providerId: string, enabled: boolean) {
  return (snapshot: ProviderSourcesSnapshot): boolean => snapshot.sources.some(
    (source) => source.provider === providerId && source.enabled === enabled,
  );
}

/** Whether the snapshot holds no refusals at all, which is what a reset makes. */
export function everySourceOn(snapshot: ProviderSourcesSnapshot): boolean {
  return snapshot.sources.every((source) => source.enabled);
}

/**
 * Whether the record of what each source has done can be read again.
 *
 * The desired state of a counter reset, and deliberately not "every counter is
 * empty": the reset exists to replace a record nothing else can repair, it is
 * only ever offered while that record is unreadable, and an emptiness test
 * would be settled by whichever search ran next rather than by the repair.
 */
export function countsReadable(snapshot: ProviderSourcesSnapshot): boolean {
  return snapshot.diagnostics_reason === null;
}
