import { afterEach, describe, expect, it } from "vitest";

import { listRunningGames, readAppDetails } from "../src/steam/client";

const originalSteamClient = (globalThis as any).SteamClient;
const originalAppStore = (globalThis as any).window?.appStore;

afterEach(() => {
  (globalThis as any).SteamClient = originalSteamClient;
  if ((globalThis as any).window) (globalThis as any).window.appStore = originalAppStore;
});

describe("Steam AppDetails and running-game observation", () => {
  it("settles a successful read even when Steam unregister cleanup throws", async () => {
    (globalThis as any).SteamClient = {
      Apps: {
        RegisterForAppDetails: (_appId: number, callback: (details: any) => void) => {
          const registration = { unregister: () => { throw new Error("cleanup failed"); } };
          queueMicrotask(() => callback({
            unAppID: 42,
            strDisplayName: "Example",
            strLaunchOptions: "",
            strShortcutLaunchOptions: "",
            strShortcutExe: "",
            strCompatToolName: "",
            strCompatToolDisplayName: "",
            nCompatToolPriority: 0,
            vecPlatforms: ["windows"],
          }));
          return registration;
        },
      },
    };

    const result = await readAppDetails(42, 500);
    expect(result.appId).toBe(42);
    expect(result.displayName).toBe("Example");
  });

  it("maps GameSessions running AppIDs only through exact installed library identities", async () => {
    (globalThis as any).window.appStore = { allApps: [] };
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
      GameSessions: { GetRunningApps: () => [{ appid: 42 }, { appid: "43" }, { appid: 999 }] },
      InstallFolder: { GetInstallFolders: async () => [{ vecApps: [
        { nAppID: 42, strAppName: "Running Game" },
        { nAppID: 43, strAppName: "String ID Must Not Match" },
      ] }] },
    };
    const snapshot = await listRunningGames();
    expect(snapshot.available).toBe(true);
    expect(snapshot.games).toEqual([{ appId: 42, name: "Running Game", sortAs: "Running Game", isShortcut: false }]);
  });


  it("maps an exact running non-Steam shortcut through the library identity", async () => {
    const shortcutId = 0xF1234567;
    (globalThis as any).window.appStore = { allApps: [{
      appid: shortcutId, app_type: 1 << 30, display_name: "Shortcut Game", sort_as: "Shortcut Game",
    }] };
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
      GameSessions: { GetRunningApps: () => [{ appid: shortcutId }] },
      InstallFolder: { GetInstallFolders: async () => [] },
    };

    await expect(listRunningGames()).resolves.toEqual({
      available: true,
      games: [{ appId: shortcutId, name: "Shortcut Game", sortAs: "Shortcut Game", isShortcut: true }],
      unresolvedAppIds: [],
    });
  });

  it("reuses already resolved library identities instead of re-enumerating on every poll", async () => {
    let enumerations = 0;
    (globalThis as any).window.appStore = { allApps: [] };
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
      GameSessions: { GetRunningApps: () => [{ appid: 42 }] },
      InstallFolder: { GetInstallFolders: async () => {
        enumerations += 1;
        return [{ vecApps: [{ nAppID: 42, strAppName: "Running Game" }] }];
      } },
    };

    const first = await listRunningGames();
    expect(enumerations).toBe(1);
    expect(first.games).toHaveLength(1);

    // Same running AppID already covered by the cache: no second Steam IPC.
    const second = await listRunningGames(first.games);
    expect(enumerations).toBe(1);
    expect(second).toEqual(first);

    // An unknown running AppID must still force a fresh exact enumeration.
    (globalThis as any).SteamClient.GameSessions.GetRunningApps = () => [{ appid: 42 }, { appid: 77 }];
    await listRunningGames(first.games);
    expect(enumerations).toBe(2);
  });

  it("falls back to the backend observation when this Steam build exposes no running-app query", async () => {
    // Steam build 1785799196 has no SteamClient.GameSessions.GetRunningApps at
    // all, so without this fallback the user must always pick the game by hand.
    const shortcutId = 3086660526;
    (globalThis as any).window.appStore = { allApps: [{
      appid: shortcutId, app_type: 1 << 30, display_name: "Shortcut Game", sort_as: "Shortcut Game",
    }] };
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
      GameSessions: { RegisterForAppLifetimeNotifications: () => ({ unregister() {} }) },
      InstallFolder: { GetInstallFolders: async () => [] },
    };

    // The invalid `0` is rejected by AppID validation itself, so it is not an
    // unresolved observation: it was never a running AppID at all.
    await expect(listRunningGames([], async () => [shortcutId, 0])).resolves.toEqual({
      available: true,
      games: [{ appId: shortcutId, name: "Shortcut Game", sortAs: "Shortcut Game", isShortcut: true }],
      unresolvedAppIds: [],
    });
  });

  it("keeps observation unavailable when the backend cannot observe the process table either", async () => {
    (globalThis as any).window.appStore = { allApps: [] };
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
      InstallFolder: { GetInstallFolders: async () => [] },
    };
    await expect(listRunningGames([], async () => null)).resolves.toEqual({ available: false, games: [] });
  });

  it("reports running-game observation unavailable when GameSessions is absent", async () => {
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
    };
    await expect(listRunningGames()).resolves.toEqual({ available: false, games: [] });
  });

});


describe("ambiguous running-game observation", () => {
  it("keeps an unresolvable running AppID visible instead of dropping it", async () => {
    // Two games running and one resolvable is not one game running: dropping
    // the other turned an explicitly ambiguous observation into an automatic
    // selection the project's no-guess rule forbids.
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
      GameSessions: { GetRunningApps: async () => [620, 999] },
      InstallFolder: { GetInstallFolders: async () => [{ vecApps: [{ nAppID: 620, strAppName: "Known", strSortAs: "Known" }] }] },
    };
    const snapshot = await listRunningGames();
    expect(snapshot.available).toBe(true);
    expect(snapshot.games.map((game) => game.appId)).toEqual([620]);
    expect(snapshot.unresolvedAppIds).toEqual([999]);
  });

  it("does not claim nothing is running when a non-empty answer cannot be parsed", async () => {
    (globalThis as any).SteamClient = {
      Apps: { RegisterForAppDetails: () => ({ unregister() {} }) },
      GameSessions: { GetRunningApps: async () => [{ unexpected: "shape" }] },
      InstallFolder: { GetInstallFolders: async () => [] },
    };
    await expect(listRunningGames()).resolves.toEqual({ available: false, games: [] });
  });
});
