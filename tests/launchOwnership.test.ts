import { describe, expect, it } from "vitest";
import { launchOwnership } from "../src/uiModel";
import type { CELaunchCapability } from "../src/types";

function capability(overrides: Partial<CELaunchCapability> = {}): CELaunchCapability {
  return {
    schema: 2,
    modes: ["attached"],
    self_test_prefix: "/prefix",
    operations: [],
    game: null,
    compat_data: null,
    proton_tools: [],
    reason: null,
    observed_proton_tool: null,
    observed_proton_reason: null,
    ce_executable_sha256: "a".repeat(64),
    ce_ready: true,
    recovered: null,
    recovery_error: null,
    owned_launch_owners: [],
    ownership_state_error: null,
    ...overrides,
  };
}

describe("launchOwnership", () => {
  it("blocks every action while the capability itself could not be read", () => {
    const view = launchOwnership({ capability: null, scopeAppId: null, selectedAppId: 10, readError: "socket closed" });
    expect(view.ambiguous).toBe(true);
    expect(view.blockedReason).toContain("socket closed");
    expect(view.ownedBySelected).toBe(false);
  });

  it("treats an incomplete ownership inventory as owned, matching the backend guard", () => {
    const view = launchOwnership({
      capability: capability({ ownership_state_error: "records could not be listed: EACCES" }),
      scopeAppId: null,
      selectedAppId: 10,
    });
    expect(view.ambiguous).toBe(true);
    expect(view.blockedReason).toContain("EACCES");
    expect(view.repairHint).not.toBeNull();
  });

  it("blocks and names recovery for a malformed record belonging to the selected game", () => {
    const view = launchOwnership({
      capability: capability({
        owned_launch_owners: [{ app_id: 10, state: "invalid", recovered: true }],
        recovered: null,
        recovery_error: "record is malformed",
      }),
      scopeAppId: 10,
      selectedAppId: 10,
    });
    expect(view.blockedReason).toContain("malformed");
    expect(view.ambiguous).toBe(true);
    expect(view.ownedBySelected).toBe(false);
  });

  it("names the other game that holds Cheat Engine", () => {
    const view = launchOwnership({
      capability: capability({ owned_launch_owners: [{ app_id: 77, state: "matched", recovered: true }] }),
      scopeAppId: 10,
      selectedAppId: 10,
      nameOf: (appId) => (appId === 77 ? "Other Game" : null),
    });
    expect(view.blockedReason).toContain("Other Game");
    expect(view.ownedBySelected).toBe(false);
  });

  it("keeps the launcher-global owner visible when no game is selected", () => {
    const view = launchOwnership({
      capability: capability({ owned_launch_owners: [{ app_id: 77, state: "matched", recovered: true }] }),
      scopeAppId: null,
      selectedAppId: null,
    });
    expect(view.blockedReason).toContain("AppID 77");
  });

  it("reports the selected game as the owner from a live operation", () => {
    const view = launchOwnership({
      capability: capability({
        operations: [{
          operation_id: "op", app_id: 10, state: "connected", mode: "attached",
          session_id: "s", message: null, error: null,
        } as CELaunchCapability["operations"][number]],
      }),
      scopeAppId: 10,
      selectedAppId: 10,
    });
    expect(view.ownedBySelected).toBe(true);
    expect(view.blockedReason).toBeNull();
  });

  it("does not attribute another game's snapshot to the selected game", () => {
    const view = launchOwnership({
      capability: capability({ recovered: { app_id: 55 } as CELaunchCapability["recovered"] }),
      scopeAppId: 55,
      selectedAppId: 10,
    });
    expect(view.ownedBySelected).toBe(false);
  });

  it("is clear when nothing owns Cheat Engine", () => {
    const view = launchOwnership({ capability: capability(), scopeAppId: 10, selectedAppId: 10 });
    expect(view).toEqual({
      blockedReason: null, identityBlockedReason: null, ownedBySelected: false, ambiguous: false, repairHint: null,
    });
  });

  it("blocks changing the registered Cheat Engine while this game itself owns one", () => {
    // The backend refuses an identity change while any game owns a live Cheat
    // Engine, so Import, Forget and the self-test were offered against a
    // guaranteed rejection whenever the owner was the selected game.
    const view = launchOwnership({
      capability: capability({ owned_launch_owners: [{ app_id: 10, state: "matched", recovered: true }] }),
      scopeAppId: 10,
      selectedAppId: 10,
    });
    expect(view.ownedBySelected).toBe(true);
    expect(view.blockedReason).toBeNull();
    expect(view.identityBlockedReason).toContain("Stop it before changing the registered Cheat Engine");
  });
});
