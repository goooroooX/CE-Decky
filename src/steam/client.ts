import type { AppCompatibilitySnapshot } from "./proton";

export interface AppDetailsSnapshot extends AppCompatibilitySnapshot {
  displayName: string;
  shortcutExe: string;
}

export interface GameSummary {
  appId: number;
  name: string;
  sortAs: string;
  isShortcut: boolean;
}

export interface RunningGamesSnapshot {
  available: boolean;
  games: GameSummary[];
  /**
   * Running AppIDs a fresh library enumeration could not resolve.
   *
   * The observation is ambiguous while this is non-empty, even when `games`
   * happens to hold exactly one entry, so automatic selection must not act on
   * it. Absent from the "channel unavailable" snapshot, which is already
   * ambiguous by construction.
   */
  unresolvedAppIds?: number[];
}

interface Unregisterable { unregister(): void }
interface RawAppDetails {
  unAppID?: number;
  strDisplayName?: string;
  strShortcutExe?: string;
  strCompatToolName?: string;
  strCompatToolDisplayName?: string;
  nCompatToolPriority?: number;
  vecPlatforms?: string[];
}

interface SteamClientShape {
  Apps: {
    RegisterForAppDetails(appId: number, callback: (details: RawAppDetails) => void): Unregisterable;
  };
  GameSessions?: {
    GetRunningApps?: () => unknown | Promise<unknown>;
  };
  InstallFolder?: {
    GetInstallFolders(): Promise<Array<{ vecApps?: Array<{ nAppID: number; strAppName: string; strSortAs?: string }> }>>;
  };
}

interface RawLibraryApp {
  appid?: number;
  nAppID?: number;
  app_type?: number;
  display_name?: string;
  strAppName?: string;
  sort_as?: string;
}

const NON_STEAM_APP_TYPE = 1 << 30;
export async function readAppDetails(appId: number, timeoutMs = 3000): Promise<AppDetailsSnapshot> {
  requireAppId(appId);
  requireTimeout(timeoutMs);
  const steamClient = requireSteamClient();
  return await new Promise<AppDetailsSnapshot>((resolve, reject) => {
    let finished = false;
    let registration: Unregisterable | undefined;
    let deferredUnregister = false;
    const unregister = () => {
      if (!registration) {
        deferredUnregister = true;
        return;
      }
      try {
        registration.unregister();
      } catch {
        // Steam cleanup failures must not strand an otherwise completed read.
      }
    };
    const cleanup = () => {
      clearTimeout(timer);
      unregister();
    };
    const timer = setTimeout(() => {
      if (finished) return;
      finished = true;
      cleanup();
      reject(new Error(`Steam AppDetails timed out for AppID ${appId}`));
    }, timeoutMs);
    try {
      registration = steamClient.Apps.RegisterForAppDetails(appId, details => {
        if (finished) return;
        try {
          const snapshot = snapshotDetails(appId, details);
          finished = true;
          cleanup();
          resolve(snapshot);
        } catch (error) {
          finished = true;
          cleanup();
          reject(error);
        }
      });
      if (deferredUnregister) unregister();
    } catch (error) {
      if (!finished) {
        finished = true;
        cleanup();
        reject(error);
      }
    }
  });
}

/**
 * Resolve the running AppIDs against the exact installed library identity.
 *
 * `known` is an optional cache of already resolved library entries. Enumerating
 * install folders plus the shortcut store is a Steam IPC round trip, and this
 * runs on a short poll while a game is in the foreground, so skip it whenever
 * every running AppID is already covered. Identity is still never inferred:
 * an AppID missing from `known` forces a fresh enumeration, and callers
 * re-read AppDetails before binding any profile.
 */
/** Resolve running AppIDs, or null when no source can observe them at all. */
export type RunningAppIdObserver = () => Promise<readonly number[] | null>;

async function observeRunningAppIds_(
  steamClient: SteamClientShape,
  fallback: RunningAppIdObserver | undefined,
): Promise<Set<number> | null> {
  const getter = steamClient.GameSessions?.GetRunningApps;
  if (typeof getter === "function") {
    try {
      const raw = await getter.call(steamClient.GameSessions);
      if (Array.isArray(raw)) {
        const ids = new Set<number>();
        for (const item of raw) {
          const appId = runningAppId(item);
          if (appId !== null) ids.add(appId);
        }
        // A non-empty answer none of whose entries parse is schema drift, not
        // an empty machine. Returning "no games are running" from it would be a
        // claim this source cannot support, so fall through to the backend.
        if (raw.length > 0 && ids.size === 0) return null;
        return ids;
      }
    } catch {
      // Fall through to the backend observation below.
    }
  }
  // Steam build 1785799196 exposes no running-app query to the frontend at all,
  // so without a fallback every user would have to pick the game by hand.
  if (!fallback) return null;
  const observed = await fallback();
  if (observed === null) return null;
  const ids = new Set<number>();
  for (const appId of observed) {
    if (isValidAppId(appId)) ids.add(appId);
  }
  return ids;
}

export async function listRunningGames(
  known: readonly GameSummary[] = [],
  observeRunningAppIds?: RunningAppIdObserver,
): Promise<RunningGamesSnapshot> {
  const steamClient = requireSteamClient();
  const runningIds = await observeRunningAppIds_(steamClient, observeRunningAppIds);
  if (runningIds === null) return { available: false, games: [] };
  if (runningIds.size === 0) return { available: true, games: [] };
  let byId = new Map(known.map((game) => [game.appId, game]));
  if (![...runningIds].every((appId) => byId.has(appId))) {
    byId = new Map((await listInstalledGames()).map((game) => [game.appId, game]));
  }
  const games: GameSummary[] = [];
  const unresolved: number[] = [];
  for (const appId of runningIds) {
    const game = byId.get(appId);
    if (game) games.push(game);
    else unresolved.push(appId);
  }
  games.sort((left, right) => (left.sortAs || left.name).localeCompare(right.sortAs || right.name));
  // Dropping an AppID that a fresh library enumeration still could not resolve
  // turned an explicitly ambiguous observation into an unambiguous one: two
  // games running, one resolvable, and the caller auto-selected it. Identity is
  // never guessed here, so the ambiguity is reported instead.
  return { available: true, games, unresolvedAppIds: unresolved };
}

export async function listInstalledGames(): Promise<GameSummary[]> {
  const steamClient = requireSteamClient();
  const byId = new Map<number, GameSummary>();
  const folders = await steamClient.InstallFolder?.GetInstallFolders?.();
  for (const folder of folders ?? []) {
    for (const app of folder.vecApps ?? []) {
      if (!isValidAppId(app.nAppID)) continue;
      insertGame(byId, {
        appId: app.nAppID,
        name: app.strAppName || `App ${app.nAppID}`,
        sortAs: app.strSortAs || app.strAppName || "",
        isShortcut: false,
      });
    }
  }

  for (const app of libraryApps()) {
    if (app.app_type !== NON_STEAM_APP_TYPE) continue;
    const appId = Number(app.appid ?? app.nAppID);
    if (!isValidAppId(appId)) continue;
    const name = app.display_name || app.strAppName || `Shortcut ${appId}`;
    insertGame(byId, { appId, name, sortAs: app.sort_as || name, isShortcut: true });
  }
  return [...byId.values()].sort((left, right) => (left.sortAs || left.name).localeCompare(right.sortAs || right.name));
}

function runningAppId(value: unknown): number | null {
  if (typeof value === "number") return isValidAppId(value) ? value : null;
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  for (const key of ["appid", "appId", "nAppID", "unAppID", "unAppId"]) {
    const candidate = item[key];
    if (typeof candidate === "number" && isValidAppId(candidate)) return candidate;
  }
  return null;
}

function requireSteamClient(): SteamClientShape {
  const candidate = (globalThis as unknown as { SteamClient?: SteamClientShape }).SteamClient;
  if (!candidate?.Apps || typeof candidate.Apps.RegisterForAppDetails !== "function") {
    throw new Error("SteamClient Apps API is unavailable");
  }
  return candidate;
}

function libraryApps(): RawLibraryApp[] {
  const candidate = (globalThis as unknown as {
    window?: { appStore?: { allApps?: unknown } };
  }).window?.appStore?.allApps;
  return Array.isArray(candidate) ? candidate as RawLibraryApp[] : [];
}

function insertGame(byId: Map<number, GameSummary>, candidate: GameSummary): void {
  const existing = byId.get(candidate.appId);
  if (!existing) {
    byId.set(candidate.appId, candidate);
    return;
  }
  if (existing.isShortcut !== candidate.isShortcut) {
    throw new Error(`Steam library identity collision for AppID ${candidate.appId}`);
  }
  // Duplicate install-folder/app-store observations are normal. Keep the first
  // stable identity rather than letting enumeration order rename the profile.
}

function snapshotDetails(appId: number, details: RawAppDetails): AppDetailsSnapshot {
  if (details.unAppID !== undefined && details.unAppID !== appId) {
    throw new Error(`Steam AppDetails identity mismatch: requested ${appId}, received ${details.unAppID}`);
  }
  const shortcutExe = details.strShortcutExe ?? "";
  return {
    appId,
    displayName: details.strDisplayName ?? `App ${appId}`,
    shortcutExe,
    isShortcut: shortcutExe.trim().length > 0,
    compatToolName: details.strCompatToolName ?? "",
    compatToolDisplayName: details.strCompatToolDisplayName ?? "",
    compatToolPriority: Number.isFinite(details.nCompatToolPriority) ? Number(details.nCompatToolPriority) : 0,
    platforms: Array.isArray(details.vecPlatforms) ? details.vecPlatforms.filter(value => typeof value === "string") : [],
  };
}

function isValidAppId(appId: number): boolean {
  return Number.isSafeInteger(appId) && appId >= 1 && appId <= 0xFFFFFFFF;
}

function requireAppId(appId: number): void {
  if (!isValidAppId(appId)) throw new Error("AppID must be an integer between 1 and 4294967295");
}

function requireTimeout(timeoutMs: number): void {
  if (!Number.isFinite(timeoutMs) || timeoutMs < 50 || timeoutMs > 60_000) {
    throw new Error("timeoutMs must be a finite value between 50 and 60000 milliseconds");
  }
}
