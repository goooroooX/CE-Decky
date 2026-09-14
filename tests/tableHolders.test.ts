import { describe, expect, it } from "vitest";
import { aggregateTableHolders, tableHolderLabel, tableOwnerNames } from "../src/tableHolders";

const SHA = "a".repeat(64);
describe("bounded table holders", () => {
  it("counts all games and keeps the same three names independent of input order", () => {
    const profiles = Array.from({ length: 10_000 }, (_, i) => ({
      app_id: i + 1, name: `${i}`.padEnd(1024, "x"), table_sha256: SHA,
    }));
    const holders = aggregateTableHolders(profiles);
    expect(aggregateTableHolders([...profiles].reverse())).toEqual(holders);
    expect(holders[SHA].count).toBe(10_000);
    expect(holders[SHA].names).toHaveLength(3);
    expect(holders[SHA].names.every((name) => Array.from(name).length <= 49)).toBe(true);
    expect(tableHolderLabel(holders[SHA])!.length).toBeLessThan(180);
    expect(tableHolderLabel(holders[SHA])).toContain("(+9997)");
  });

  it("counts distinct AppIDs with identical names and ignores games without a table", () => {
    const profiles = Array.from({ length: 4 }, (_, i) => ({ app_id: i + 1, name: "Same", table_sha256: SHA }));
    const holder = aggregateTableHolders([...profiles, { app_id: 5, name: "Idle", table_sha256: null }])[SHA];
    expect(holder.count).toBe(4);
    expect(tableHolderLabel(holder)).toBe("in use by Same, Same, Same (+1)");
    expect(tableHolderLabel(undefined)).toBeNull();
  });
});

describe("which game a stored table belongs to", () => {
  const OTHER = "b".repeat(64);

  it("names the game whose library holds the table", () => {
    const names = tableOwnerNames([
      { app_id: 10, name: "Neon Bazaar", table_library: [SHA] },
      { app_id: 20, name: "Hidden Blade", table_library: [OTHER] },
    ]);
    expect(names[SHA]).toBe("Neon Bazaar");
    expect(names[OTHER]).toBe("Hidden Blade");
  });

  it("leads with the game the screen is on where that game holds it", () => {
    // A table two games imported is being read in the context of one of them,
    // and naming the other game's copy answers a question nobody asked.
    const profiles = [
      { app_id: 10, name: "First", table_library: [SHA] },
      { app_id: 20, name: "Second", table_library: [SHA] },
    ];
    expect(tableOwnerNames(profiles, 20)[SHA]).toBe("Second +1");
    expect(tableOwnerNames(profiles, 10)[SHA]).toBe("First +1");
    // With no game on the screen, the lowest AppID leads, whatever order the
    // profiles arrive in.
    expect(tableOwnerNames([...profiles].reverse())[SHA]).toBe("First +1");
  });

  it("leaves a table no profile holds without a name rather than guessing one", () => {
    const names = tableOwnerNames([{ app_id: 10, name: "Neon Bazaar", table_library: [] }], 10);
    expect(names[SHA]).toBeUndefined();
  });

  it("bounds a very long game name and a very large library", () => {
    const profiles = Array.from({ length: 10_000 }, (_, i) => ({
      app_id: i + 1, name: `${i}`.padEnd(1024, "x"), table_library: [SHA],
    }));
    const label = tableOwnerNames(profiles)[SHA];
    expect(Array.from(label).length).toBeLessThan(64);
    expect(label).toContain("+9999");
  });
});
