/**
 * A durable write announcing itself to whatever panel is on screen.
 *
 * Some of this plugin's answers outlive the panel that asked for them. The
 * question about a table Cheat Engine refused is the clearest case: it closes
 * Configure cheats, which on the device takes the quick access panel with it,
 * and the several writes the answer then makes land while no panel exists.
 * Steam builds a new one when the user looks again, and that panel reads the
 * authority once, on its way up - which is a read racing a chain of writes it
 * knows nothing about. It won the race, so the table it had just been told to
 * stop using was still sitting on the panel with its Load and Configure rows.
 *
 * `PANEL_CATCH_UP_DELAY_MS` is the timed answer to the same problem and stays
 * for what it was measured against: a single withdrawal landing a few hundred
 * milliseconds late. It cannot cover this one, because the write that matters
 * here is the last of five round trips rather than the first, and the honest
 * fix for that is not a longer timer. This is the exact one: the answer says
 * when it has finished, and the panel that happens to be alive then re-reads.
 *
 * Deliberately not a store. It carries no state and no payload, because the
 * authority is the backend and the only thing a panel needs to be told is that
 * asking again is now worth it.
 */

type Listener = () => void;

const listeners = new Set<Listener>();

/** Re-read the authority: something durable changed behind this panel's back. */
export function notifyAuthorityChanged(): void {
  // Copied first: a listener that unsubscribes while this runs must not change
  // the set being walked, and a panel unmounting inside its own re-read is an
  // ordinary way for that to happen.
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // One panel failing to re-read is not a reason to keep another from
      // being told. The write has already happened either way.
    }
  }
}

/** Hear about those, until the returned function is called. */
export function subscribeAuthorityChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
