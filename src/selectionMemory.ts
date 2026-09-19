/**
 * Remember which library entry the user chose, across panel remounts.
 *
 * Opening any Decky modal closes the quick-access panel, so the plugin's React
 * tree unmounts and every piece of frontend-only state is lost. The selected
 * table survives that because it lives in the backend profile; the selected
 * *game* did not, so picking a game that was not running put the user straight
 * back on "No game selected" the moment the picker closed.
 *
 * This is an explicit user choice, not an inferred identity: it is stored as
 * the exact AppID plus whether it is a non-Steam shortcut, and the caller must
 * still match it against the live Steam library and re-resolve everything
 * downstream before acting on it.
 */

const KEY = "ce-decky.selected-game.v1";

export interface RememberedSelection {
  appId: number;
  isShortcut: boolean;
}

export function rememberSelectedGame(selection: RememberedSelection): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(selection));
  } catch {
    // Private windows and blocked site data are normal; the panel simply loses
    // the convenience and behaves as it did before.
  }
}

export function forgetSelectedGame(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    // Ignore: nothing downstream depends on the removal succeeding.
  }
}

export function readSelectedGame(): RememberedSelection | null {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(KEY);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return null;
    const { appId, isShortcut } = parsed as Record<string, unknown>;
    if (typeof appId !== "number" || !Number.isInteger(appId) || appId <= 0 || appId > 0xFFFFFFFF) return null;
    if (typeof isShortcut !== "boolean") return null;
    return { appId, isShortcut };
  } catch {
    return null;
  }
}

/**
 * Remember whether the mascot was on, for the moment before the status arrives.
 *
 * Every modal this plugin opens closes the quick-access panel, so the panel is
 * rebuilt constantly and each rebuild starts with no backend status at all. The
 * preference lives in the backend, which is right, and the render before the
 * first read has to guess: guessing "on" drew the image for a second in the
 * face of every user who had switched it off, on every remount.
 *
 * This is the last answer the backend gave in this browser session, so the
 * guess is the user's own most recent state rather than the default. It decides
 * nothing else: the status that arrives a moment later is the authority, and a
 * blocked or empty store just means the first frame guesses "on" as before.
 */
const MASCOT_KEY = "ce-decky.mascot-visible.v1";

export function rememberMascotVisible(visible: boolean): void {
  try {
    window.localStorage.setItem(MASCOT_KEY, visible ? "1" : "0");
  } catch {
    // Private windows and blocked site data are normal; the panel loses the
    // convenience and behaves as it did before.
  }
}

export function readMascotVisible(): boolean {
  try {
    return window.localStorage.getItem(MASCOT_KEY) !== "0";
  } catch {
    return true;
  }
}
