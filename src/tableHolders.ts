import type { GameProfile } from "./types";

export interface TableHolders {
  readonly names: readonly string[];
  /** Number of games, including games with identical display names. */
  readonly count: number;
}

const DISPLAY_NAMES = 3;
const NAME_CHARACTERS = 48;
type Holder = Pick<GameProfile, "app_id" | "name" | "table_sha256">;

/** One pass over the schema's unique AppIDs; retain only a constant-size sample.
 * Lowest AppIDs make the display independent of profile enumeration order.
 */
export function aggregateTableHolders(profiles: readonly Holder[]): Record<string, TableHolders> {
  const grouped = new Map<string, { count: number; sample: Holder[] }>();
  for (const profile of profiles) {
    if (!profile.table_sha256) continue;
    let group = grouped.get(profile.table_sha256);
    if (!group) {
      group = { count: 0, sample: [] };
      grouped.set(profile.table_sha256, group);
    }
    group.count++;
    if (group.sample.length < DISPLAY_NAMES || profile.app_id < group.sample[DISPLAY_NAMES - 1].app_id) {
      group.sample.push(profile);
      // At most four entries, never a sort of the full holder population.
      group.sample.sort((left, right) => left.app_id - right.app_id);
      group.sample.length = Math.min(group.sample.length, DISPLAY_NAMES);
    }
  }
  return Object.fromEntries([...grouped].map(([sha, group]) => [sha, {
    count: group.count,
    names: group.sample.map(({ name }) => {
      const characters = Array.from(name);
      return characters.length > NAME_CHARACTERS
        ? `${characters.slice(0, NAME_CHARACTERS).join("")}…` : name;
    }),
  }]));
}

export function tableHolderLabel(holders: TableHolders | undefined): string | null {
  if (!holders || holders.count <= 0) return null;
  if (!holders.names.length) return `in use by ${holders.count} games`;
  const remaining = holders.count - holders.names.length;
  return `in use by ${holders.names.join(", ")}${remaining > 0 ? ` (+${remaining})` : ""}`;
}

export function tableHolderIds(profiles: readonly Holder[]): Record<string, readonly number[]> {
  const ids: Record<string, number[]> = {};
  for (const profile of profiles) {
    if (profile.table_sha256) (ids[profile.table_sha256] ??= []).push(profile.app_id);
  }
  return ids;
}

type Owner = Pick<GameProfile, "app_id" | "name" | "table_library">;

/**
 * Which game each stored table belongs to, by exact SHA.
 *
 * A file name is not an answer to "what is this table for". Real tables are
 * called `CD_Inventory_2145.CT` or `winmm-x64.zip`, and Manage lists every
 * table on the device, so a reader scanning it had no way to tell one game's
 * tables from another's. The list of tables a game has imported is the fact
 * that answers it, and the profile store already holds one per game.
 *
 * `preferredAppId` is the game the screen is on. Where that game holds the
 * table, its own name is the one shown: a table several games hold is being
 * read here in the context of one of them, and naming another game's copy of
 * it is answering a question nobody asked.
 *
 * The same bounded shape as the holder sample above: one pass, a constant-size
 * sample, lowest AppIDs first so the display does not depend on enumeration
 * order.
 */
export function tableOwnerNames(
  profiles: readonly Owner[], preferredAppId?: number | null,
): Record<string, string> {
  const grouped = new Map<string, { count: number; preferred: string | null; sample: Owner[] }>();
  for (const profile of profiles) {
    for (const sha of profile.table_library ?? []) {
      let group = grouped.get(sha);
      if (!group) {
        group = { count: 0, preferred: null, sample: [] };
        grouped.set(sha, group);
      }
      group.count++;
      if (preferredAppId !== null && preferredAppId !== undefined && profile.app_id === preferredAppId) {
        group.preferred = profile.name;
      }
      if (group.sample.length < DISPLAY_NAMES || profile.app_id < group.sample[group.sample.length - 1].app_id) {
        group.sample.push(profile);
        group.sample.sort((left, right) => left.app_id - right.app_id);
        group.sample.length = Math.min(group.sample.length, DISPLAY_NAMES);
      }
    }
  }
  const names: Record<string, string> = {};
  for (const [sha, group] of grouped) {
    const lead = group.preferred ?? group.sample[0]?.name;
    if (!lead) continue;
    const remaining = group.count - 1;
    names[sha] = remaining > 0 ? `${boundName(lead)} +${remaining}` : boundName(lead);
  }
  return names;
}

function boundName(name: string): string {
  const characters = Array.from(name);
  return characters.length > NAME_CHARACTERS
    ? `${characters.slice(0, NAME_CHARACTERS).join("")}…` : name;
}
