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
