/**
 * Where the ring belongs on the panel that comes back after an answer.
 *
 * Some of this plugin's questions are answered with no panel on screen. The
 * question about a table Cheat Engine refused is the clearest case: it closes
 * Configure cheats, which on the device takes the quick access panel with it,
 * so the answer lands while nothing is mounted and the panel Steam builds when
 * the user looks again opens wherever it always opens. That is Advanced, which
 * is not what somebody who has just retired a table is going to press. They are
 * going to look for another one, and the control for that is Search.
 *
 * A request outlives the panel for exactly that reason, and it is taken by
 * whichever panel is alive when the control it names can actually be pressed:
 * Search is disabled while the panel is busy and while it has no game, and the
 * writes the answer makes are still running when the request is made. Taking it
 * is what retires it, so one answer moves the ring once.
 *
 * Deliberately tiny and deliberately not a store. It carries no panel state:
 * the only thing a panel needs to be told is which of its own controls the last
 * answer left the user in front of.
 */

/** The controls an answer can hand the panel back at. */
export type PanelFocusTarget = "search";

let pending: PanelFocusTarget | null = null;

type Listener = () => void;

const listeners = new Set<Listener>();

/** Put the ring on this control, on whichever panel is or becomes alive. */
export function requestPanelFocus(target: PanelFocusTarget): void {
  pending = target;
  // Copied first: a listener that unsubscribes while this runs must not change
  // the set being walked, and a panel unmounting inside its own handler is an
  // ordinary way for that to happen.
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch {
      // One panel failing to hear this is not a reason to keep another from
      // hearing it. The request stays pending either way.
    }
  }
}

/**
 * Take the outstanding request, when it is for this control.
 *
 * The caller has to be able to act on it: a request taken by a panel whose
 * Search button is still disabled is a request spent on nothing.
 */
export function takePanelFocus(target: PanelFocusTarget): boolean {
  if (pending !== target) return false;
  pending = null;
  return true;
}

/** Hear about a request arriving, until the returned function is called. */
export function subscribePanelFocus(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
