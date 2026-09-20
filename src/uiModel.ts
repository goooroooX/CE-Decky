import { isArchiveFilename } from "./tableImport";
import type { GameSummary } from "./steam/client";
import type { BlockedTable, BlockedTableCause, CELaunchCapability, ConfiguredValue, GameContainerObservation, GameExecutable, GameExecutableListing, LocalLibrary, PluginUpdateState, RuntimeEnvelope, RuntimeResult, SelfTestCheck, SelfTestResult, StartupPreference, TableControl, TableInspection, TableStatus } from "./types";

// One 1280x800 Game Mode viewport fits roughly six compact record rows beside
// the modal header, section/filter, pager and Apply/Cancel. Eight overflowed the
// screen and clipped the modal title on the target.
export const CONTROL_PAGE_SIZE = 6;
export const PROVIDER_PAGE_SIZE = 6;
/**
 * The most tables Manage draws on one page, before the screen is measured.
 *
 * It has a filter, a section heading, an explanatory row and a footer beside
 * its tables, which is different chrome from the search results, and a value
 * that happens to match today is not a reason to share a name.
 *
 * Six was written as the answer for a handheld and it is not: the screen was
 * the one paged list here that never measured anything, and on a Steam Deck's
 * 534 pixel page a page of six ran off the bottom. It is the starting point
 * now, and what the screen actually fits is read from the screen, the way the
 * results list and the cheat picker already read theirs.
 *
 * One budget for the whole screen, not one per list. Two sections paging six
 * each is twelve rows plus two pagers, two headings, the file row, the filter
 * and the way out, which is the unreachable screen the paging was added to
 * prevent, and is why Manage shows one ordered sequence rather than two lists.
 */
export const MANAGE_PAGE_SIZE = 6;

/**
 * How tall one table row is, for fitting a page of them to the screen.
 *
 * A constant because the row is a constant: one line of name over one line of
 * state, both clipped to one line and revealed under the controller ring, with
 * the row's own controls beside them.
 */
export const MANAGE_ROW_HEIGHT = 58;

/**
 * The fewest tables a page may hold before the measurement stops taking any.
 *
 * A list of two is still a list to page through. One is a screen that shows a
 * table at a time, which is worse than a page that runs a little long.
 */
export const MIN_MANAGE_ROWS = 2;

/**
 * A release, labelled the way the source wrote it.
 *
 * Three screens each put a `v` in front of whatever the provider advertised,
 * and GitHub advertises a tag verbatim: a repository tagged `v2` was shown as
 * `vv2` on every one of them. Prefixing is only right for a bare number, and it
 * is wrong for a tag that already carries a letter, a word or its own prefix.
 *
 * It formats what is stored rather than what is ingested, because origins
 * written before any of this are on devices now and will be read for as long as
 * those tables are kept.
 */
export function releaseLabel(version: string | null | undefined): string | null {
  const text = (version ?? "").trim();
  if (!text) return null;
  // Already a version label: `v2`, `V2.1`. Left as the source wrote it, apart
  // from the case of the marker itself, so two rows do not disagree on it.
  if (/^[vV]\d+(\.\d+)*$/.test(text)) return `v${text.slice(1)}`;
  // A bare release number, which is the one shape the prefix was written for.
  // Deliberately the whole string and not merely its first character: a tag
  // that starts with a digit is not necessarily a version, and `2026-08` is a
  // date the source chose to release under. Decorating it made this function
  // do the same kind of thing to a source's own label that it exists to stop.
  if (/^\d+(\.\d+)*$/.test(text)) return `v${text}`;
  // Anything else is a name rather than a number: `release-42`, `latest`,
  // `2026-08`, `1.2-rc1`.
  return text;
}
export const MAX_REMEMBERED_CONTROLS = 1024;
export const MAX_EFFECTIVE_STARTUP_ACTIONS = 2048;

// How long after a panel first has state to show it asks the authority once
// more, for the mutation the panel it replaced had already started.
//
// A panel Steam recreates while the quick-access panel is open never sees a
// visibility change, so reading again the moment its first status arrives is
// the only catch-up it gets, and that read is issued one round trip after the
// first one rather than at a moment related to the write. On the target the
// withdrawal landed 340 ms after the replacement panel had read the profile, so
// two reads that close together both miss it. This is one further read, once
// per panel, past the far side of the window that was actually observed. It is
// not polling: nothing repeats it, and a panel hidden or dismounted before it
// fires cancels it, because the next time this panel is shown it re-reads
// anyway.
export const PANEL_CATCH_UP_DELAY_MS = 1200;

const PROCESS_BASENAME_RE = /^[^\\/:*?"<>|\x00-\x1f]{1,255}\.exe$/iu;
const UNSAFE_PROCESS_DISPLAY_CONTROL_RE = /[\p{Cc}\p{Cf}]/u;
const MAX_PROCESS_NAME_BYTES = 1024;

export interface RuntimeAttachCandidate {
  name: string;
  pids: number[];
  /** True for a Wine/Proton/launcher helper that never owns a game's memory. */
  runtimeNoise?: boolean;
}

/** One launcher-scope answer to "who owns Cheat Engine, and is that known". */
export interface LaunchOwnershipView {
  /** Blocks a new launch for the selected game, and every global identity mutation. */
  blockedReason: string | null;
  /**
   * Blocks changing the registered Cheat Engine.
   *
   * Strictly wider than `blockedReason`: the backend refuses an identity change
   * while *any* game owns a live Cheat Engine, the selected one included, so
   * Import, Forget and the self-test were offered against a guaranteed
   * rejection whenever the selected game was the owner.
   */
  identityBlockedReason: string | null;
  /** The selected game holds a live owned Cheat Engine that Stop can act on. */
  ownedBySelected: boolean;
  /** Ownership is known to be incomplete or malformed; every action must fail closed. */
  ambiguous: boolean;
  /** Where the user has to go to clear an ambiguous ownership state. */
  repairHint: string | null;
}

/**
 * Ownership of the one Cheat Engine CE Decky may run is launcher-global, but the
 * panel used to read it three different ways: a same-AppID filter that dropped
 * malformed owners, a per-game live-operation test that only recognized
 * recovered/live records, and a snapshot that could belong to a different game
 * entirely. Each of them could report "clear" while the backend guard treated
 * the same state as owned, so Home offered install, import, forget and Start
 * actions that were guaranteed to be rejected. This is the single authority.
 *
 * `scopeAppId` is the AppID the capability was actually fetched for: launcher
 * scope is `null`, and a snapshot fetched for another game contributes only its
 * launcher-global facts.
 */
export function launchOwnership(input: {
  capability: CELaunchCapability | null;
  scopeAppId: number | null;
  selectedAppId: number | null;
  /** Why the last capability read failed, when it did. */
  readError?: string | null;
  nameOf?: (appId: number) => string | null;
}): LaunchOwnershipView {
  const { capability, scopeAppId, selectedAppId } = input;
  if (!capability) {
    const reason = input.readError
      ? `Cheat Engine launch state could not be read: ${input.readError}`
      : "Cheat Engine launch state could not be read. Refresh and try again.";
    return {
      blockedReason: reason,
      identityBlockedReason: reason,
      ownedBySelected: false,
      ambiguous: true,
      repairHint: "Advanced → Refresh",
    };
  }
  const name = (appId: number) => input.nameOf?.(appId) ?? `AppID ${appId}`;
  const owners = capability.owned_launch_owners ?? [];
  const unreadable = owners.find((owner) => owner.state === "invalid" || owner.state === "unreadable");
  if (capability.ownership_state_error) {
    const reason = `Cheat Engine ownership cannot be read: ${capability.ownership_state_error}`;
    return {
      blockedReason: reason,
      identityBlockedReason: reason,
      ownedBySelected: false,
      ambiguous: true,
      repairHint: "Advanced → Launch ownership",
    };
  }
  if (unreadable) {
    // A malformed record for the selected game used to fall through both
    // guards: the live-operation test did not recognize it and the owner
    // filter dropped it for having the selected AppID.
    const reason = `The Cheat Engine ownership record for ${name(unreadable.app_id)} is malformed and cannot be trusted.`;
    return {
      blockedReason: reason,
      identityBlockedReason: reason,
      ownedBySelected: false,
      ambiguous: true,
      repairHint: "Advanced → Launch ownership cannot be read",
    };
  }
  const elsewhere = owners.find((owner) => owner.app_id !== selectedAppId);
  if (elsewhere) {
    const reason = `Cheat Engine is still running for ${name(elsewhere.app_id)}. Select that game and stop it first.`;
    return {
      blockedReason: reason,
      identityBlockedReason: reason,
      ownedBySelected: false,
      ambiguous: false,
      repairHint: null,
    };
  }
  const sameScope = scopeAppId !== null && scopeAppId === selectedAppId;
  const ownedBySelected = Boolean(
    selectedAppId !== null
    && (
      owners.some((owner) => owner.app_id === selectedAppId)
      || (sameScope && capability.recovered?.app_id === selectedAppId)
      || (sameScope && capability.operations.some((operation) =>
        operation.app_id === selectedAppId && ["starting", "running", "connected"].includes(operation.state)))
    ),
  );
  return {
    blockedReason: null,
    // Only one Cheat Engine may run at a time, and replacing the registered one
    // means replacing what a running process is executing from.
    identityBlockedReason: ownedBySelected
      ? "Cheat Engine is running for this game. Stop it before changing the registered Cheat Engine."
      : null,
    ownedBySelected,
    ambiguous: false,
    repairHint: null,
  };
}

export function isExactRuntimeSession(
  envelope: RuntimeEnvelope | null,
  appId: number,
  tableSha256: string,
): boolean {
  if (!envelope?.connected || !envelope.session_current || !envelope.prepared || !envelope.status) return false;
  if (envelope.prepared.app_id !== appId || envelope.status.app_id !== appId) return false;
  if (envelope.prepared.table_sha256 !== tableSha256 || envelope.status.table_sha256 !== tableSha256) return false;
  if (envelope.prepared.session_id !== envelope.status.session_id) return false;
  if (envelope.prepared.ce_sha256 !== envelope.status.ce_sha256) return false;
  if (envelope.prepared.descriptor_sha256 !== envelope.status.descriptor_sha256) return false;
  return true;
}

/**
 * The live bridge holding this exact table, attached, with the table in it.
 *
 * Attached is not loaded. A Cheat Engine that could not open the table attaches
 * to the game anyway and reports itself perfectly healthy, with an address list
 * that answers "missing" for every record: nothing to switch on, nothing to
 * pin, and no live value to read. Every caller here means "this session can be
 * used now", so a stated load failure is not one of them - Home says so in its
 * own words, and an activation that finds this false starts Cheat Engine again
 * rather than quietly doing nothing. A bridge that states nothing is an older
 * one that never reported this at all, and is unchanged.
 */
export function isExactAttachedRuntime(
  envelope: RuntimeEnvelope | null,
  appId: number,
  tableSha256: string,
): boolean {
  return isExactRuntimeSession(envelope, appId, tableSha256)
    && Boolean(envelope?.status?.attached)
    && (envelope?.status?.opened_process_id ?? 0) > 0
    && envelope?.status?.table_load_state !== "failed";
}

/**
 * The live bridge target when it is not the one the profile will use next time.
 *
 * Advanced -> Retry attach updates the bridge's live target and PID only. The
 * prepared descriptor and the durable profile keep the old basename, so Home
 * could return to a healthy connected state while displaying one target and
 * actually controlling another - and the next session started from the old name
 * and repeated the attachment failure. Returns `null` when they agree.
 */
export function divergentLiveTarget(
  envelope: RuntimeEnvelope | null,
  profileTargetProcess: string | null | undefined,
): string | null {
  const live = envelope?.status?.target_process;
  if (!live || !profileTargetProcess) return null;
  if (!envelope?.status?.attached) return null;
  return live.toLowerCase() === profileTargetProcess.toLowerCase() ? null : live;
}

/**
 * The cap the backend puts on the executables it reports for one game.
 *
 * A list that reached it is a list that may have been cut, and a cut list
 * cannot prove anything is absent from it. The backend's own target-state
 * answer refuses to say `absent` on one for the same reason, and this is that
 * rule again on the panel side rather than a second opinion about it.
 */
const MAX_OBSERVED_WINDOWS_EXECUTABLES = 32;

/**
 * Whether this game is running its own programs and the saved target is not one.
 *
 * The check that catches a target chosen before the game had ever run. A table
 * can be authorized against an executable read out of the game's own folder,
 * which is the only evidence there is when nothing is running, and this is what
 * turns the first real start into the answer: the game is up, these are the
 * Windows programs it started, and the one saved for it is not among them.
 *
 * `divergentLiveTarget` is the other half and a different question: it is about
 * a Cheat Engine that has already attached to something else. This one fires
 * before anything attaches, which is where the wrong name actually costs a
 * press that does nothing.
 *
 * It takes the observation rather than the capability that holds it, because
 * the caller is the only thing that knows whether the snapshot it has was
 * fetched for the game it is describing. Handing it the capability invited the
 * panel to answer about whichever AppID was read last, which is the mistake
 * `index.tsx` records beside its own scoped observation.
 *
 * Everything that could make the absence unprovable answers `null`, because a
 * warning a user cannot act on is worse than none and the press it offers
 * overwrites a working target:
 *
 * - the observation saw nothing, which is a prefix that has not got going;
 * - the raw list reached the backend's own collection cap, so it may have been
 *   cut and cannot prove anything is missing from it. Counted before the
 *   validity filter, because the cap is about what was collected: filtering
 *   first let a full list of 32 lose one malformed name and pass as 31;
 * - nothing is left once the Wine and Proton machinery is dropped. That is the
 *   important one. It is what a prefix mid-startup looks like, and it is also
 *   what a game that has *exited* looks like, because `running` stays true
 *   while `wineserver`, the Proton chain and Steam's reaper still report the
 *   AppID. Returning the unfiltered list there offered `explorer.exe` as a
 *   one-press repair for a game that was not running at all.
 */
export function absentLiveTarget(
  game: GameContainerObservation | null | undefined,
  profileTargetProcess: string | null | undefined,
): readonly string[] | null {
  const target = (profileTargetProcess ?? "").trim();
  if (!game?.running || !target) return null;
  const collected = game.windows_executables ?? [];
  if (collected.length === 0 || collected.length >= MAX_OBSERVED_WINDOWS_EXECUTABLES) return null;
  const observed = collected.filter(isValidProcessBasename);
  if (observed.some((name) => name.toLowerCase() === target.toLowerCase())) return null;
  // What it did start, which is the repair. The Wine and Proton machinery is
  // dropped for the same reason the Review screen drops it: none of it ever
  // owns a game's memory, and a dozen of them turns a fix into a search.
  const candidates = withoutWineRuntimeProcesses(observed);
  return candidates.length > 0 ? candidates : null;
}

export function isValidProcessBasename(value: string): boolean {
  return value.normalize("NFC") === value
    && new TextEncoder().encode(value).length <= MAX_PROCESS_NAME_BYTES
    && !UNSAFE_PROCESS_DISPLAY_CONTROL_RE.test(value)
    && PROCESS_BASENAME_RE.test(value);
}

function stableProcessDisplayName(current: string, candidate: string, key: string): string {
  if (current === key) return current;
  if (candidate === key) return candidate;
  return candidate < current ? candidate : current;
}

export function runtimeAttachCandidates(processes: readonly [number, string][]): RuntimeAttachCandidate[] {
  const byName = new Map<string, RuntimeAttachCandidate>();
  for (const [pid, name] of processes) {
    if (!Number.isSafeInteger(pid) || pid < 1 || !isValidProcessBasename(name)) continue;
    const key = name.toLowerCase();
    const existing = byName.get(key);
    if (existing) {
      existing.name = stableProcessDisplayName(existing.name, name, key);
      existing.pids.push(pid);
      continue;
    }
    byName.set(key, { name, pids: [pid], runtimeNoise: isWineRuntimeExecutable(name) });
  }
  return [...byName.entries()]
    .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
    // The bridge's process snapshot is deliberately broad, so Wine services,
    // Proton helpers, crash reporters and store launchers all appear here. This
    // is the recovery route for an attachment that chose wrongly, so offering
    // them as equally valid targets is what made that recovery unstable. They
    // stay selectable - one of them can be the right answer for an unusual
    // game - but they are marked and ranked last so the exact-PID list opens on
    // something that can actually own game memory.
    .map(([, candidate]) => ({ ...candidate, pids: [...candidate.pids].sort((a, b) => a - b) }))
    .sort((left, right) => Number(left.runtimeNoise ?? false) - Number(right.runtimeNoise ?? false));
}

export interface ControlSection {
  key: string;
  label: string;
  path: readonly string[] | null;
  pinnedOnly: boolean;
}

function pathStartsWith(path: readonly string[], prefix: readonly string[]): boolean {
  return prefix.length < path.length && prefix.every((segment, index) => path[index] === segment);
}

/**
 * How long one section may name itself in the picker.
 *
 * The picker is a full-width list and a name past this wraps, so nine of them
 * fill the screen. Measured against that list rather than picked: on the
 * development device an option of 95 characters fills the line exactly, so this
 * is where the line ends rather than a budget with room to spare in it.
 */
export const MAX_SECTION_LABEL = 96;

/**
 * How many times two options that read alike may be given back a group.
 *
 * A bound rather than a loop to exhaustion: every pass is another walk of the
 * whole list, the paths that reach it are pathological already, and a picker
 * that renders is worth more than one that is provably unambiguous. Two rows
 * that still read alike select what their own key says, never the wrong one.
 */
const MAX_SECTION_LABEL_PASSES = 8;

/**
 * One space where the author left several, and nothing at either end.
 *
 * A table author writes a heading, not a label: two spaces before a bracketed
 * note is the ordinary way one of these reads, and on a proportional list that
 * is a gap in the middle of a name rather than emphasis. It also costs
 * characters against the width of the line, which is the thing that decides
 * whether a name has to be cut at all.
 */
export function tidySegment(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/**
 * How many leading groups every section here has in common.
 *
 * A table author's own structure routinely begins with instructions rather
 * than with a category: one real table nests every one of its groups under
 * "[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world" and
 * "[STEP 2] Enable / Initialize Table (Enable this SECOND)", so every option in
 * the picker opened with the same hundred and ten characters and the part that
 * told them apart was at the end of the third line.
 *
 * Never the whole of any path, because a section still has to name itself: with
 * one group the shared prefix is that group, and dropping it would leave the
 * option blank.
 */
export function sharedSectionDepth(paths: readonly (readonly string[])[]): number {
  // Nothing is repeated when there is only one of them. Read as "what this
  // section has in common with itself" it is the whole path, and the single
  // group in one real table lost the group it sits under for no gain at all.
  if (paths.length < 2) return 0;
  const limit = Math.min(...paths.map((path) => path.length)) - 1;
  let depth = 0;
  while (depth < limit && paths.every((path) => path[depth] === paths[0][depth])) depth += 1;
  return depth;
}

/**
 * How much of a path is a section that the picker already lists above this one.
 *
 * A group whose own parent is an option two rows up was spelling that parent
 * out again on every child, which is the same repetition `sharedSectionDepth`
 * removes, one branch down: it is what pushed three of this device's eight
 * Neon Bazaar options past the width of the list and made them read
 * `... > <leaf>`. A CT holds a group's descendants directly under it, so the
 * ancestor is always visible immediately above, and the row below reads as
 * what it is - the next level of the same outline.
 *
 * Deepest first: an option nested three groups down drops all of them, not the
 * outermost one.
 */
export function listedAncestorDepth(path: readonly string[], listed: ReadonlySet<string>): number {
  for (let depth = path.length - 1; depth > 0; depth -= 1) {
    if (listed.has(JSON.stringify(path.slice(0, depth)))) return depth;
  }
  return 0;
}

/**
 * What one section is called, once the picker has stopped repeating itself.
 *
 * What is left after the groups this section shares with the ones above it, and
 * then whole groups off the front while it does not fit the line. Nothing marks
 * that: what is dropped is either on the screen already, as the option this one
 * sits under, or is a heading with no cheats of its own that the picker never
 * offers, so a leading `... >` pointed at nothing a reader could go and look
 * at, and it cost two characters of the name to say so.
 *
 * The leaf is the part that tells one section from another, so it is the part
 * that survives. Length is marked once, at the end, and only when the leaf
 * alone is longer than the line: a name past that width wraps and pushes every
 * option below it down the screen, so it is cut where a reader has already
 * stopped rather than allowed to reflow the window.
 */
export function sectionLabel(path: readonly string[], shared: number, max = MAX_SECTION_LABEL): string {
  const rest = path.slice(shared);
  const segments = (rest.length > 0 ? rest : path.slice(-1)).map(tidySegment).filter(Boolean);
  // The separator between two levels of one path is the one this product
  // already uses for that, in the cheat rows on the panel behind this list.
  // What went is the *leading* mark, which was not a path at all.
  for (let start = 0; start < segments.length; start += 1) {
    const candidate = segments.slice(start).join(" \u203a ");
    if (candidate.length <= max) return candidate;
  }
  // Every group in front of it is gone and the section's own name still does
  // not fit the line. Only now is anything actually lost, and it is lost off
  // the end, where a reader has already stopped.
  const leaf = segments[segments.length - 1] ?? "";
  return `${leaf.slice(0, max - 1)}\u2026`;
}

/**
 * How many leading groups every one of these paths repeats, the whole of the
 * shortest included.
 *
 * The picker's own rule stops one short, because a section still has to name
 * itself there. A path shown as context beside a name it does not provide has
 * no such floor: what every row repeats tells the reader nothing, and a row
 * left with no context at all is exactly as informative as the row above it
 * that never had any. Paths with nothing in them are ignored rather than
 * making the answer zero, since a row showing no context repeats nothing.
 */
export function sharedContextDepth(paths: readonly (readonly string[])[]): number {
  const present = paths.filter((path) => path.length > 0);
  if (present.length < 2) return 0;
  const limit = Math.min(...present.map((path) => path.length));
  let depth = 0;
  while (depth < limit && present.every((path) => path[depth] === present[0][depth])) depth += 1;
  return depth;
}

export function controlSections(inspection: TableInspection | null, safeControls: readonly TableControl[]): ControlSection[] {
  const sections: ControlSection[] = [
    { key: "all", label: "All supported controls", path: null, pinnedOnly: false },
    { key: "pinned", label: "Pinned controls", path: null, pinnedOnly: true },
  ];
  if (!inspection) return sections;

  const descendantGroupPaths = new Set<string>();
  for (const candidate of safeControls) {
    for (let depth = 1; depth < candidate.path.length; depth += 1) {
      descendantGroupPaths.add(JSON.stringify(candidate.path.slice(0, depth)));
    }
  }
  const seenGroupPaths = new Set<string>();
  const groups: Array<{ key: string; path: readonly string[] }> = [];
  inspection.controls.forEach((control, index) => {
    if (control.kind !== "group" && !control.group_header) return;
    const pathKey = JSON.stringify(control.path);
    if (!descendantGroupPaths.has(pathKey)) return;
    if (seenGroupPaths.has(pathKey)) return;
    seenGroupPaths.add(pathKey);
    groups.push({ key: `group:${index}`, path: control.path });
  });
  // Named after every group is known, because what a section can leave out
  // depends on what the others say. Dropping the groups they all share and
  // keeping the leaf is what turns nine options that each fill three lines
  // into nine that each name themselves.
  const shared = sharedSectionDepth(groups.map((group) => group.path));
  // And what one section has in common with the section it sits inside, when
  // that one is an option of its own. Measured over the ten tables on the
  // development device: with this and the tidying above, none of their eighty
  // options has to be cut at all, where eight did.
  const listed = new Set(groups.map((group) => JSON.stringify(group.path)));
  const from = groups.map((group) => Math.max(shared, listedAncestorDepth(group.path, listed)));
  // Two options that read the same select different things, and a picker is
  // the one place that cannot be lived with: the row says nothing about which
  // of them it is. Each is given back one more group of its own path until they
  // differ, which is the same rule the table inspector uses for two cheat
  // values that clean down to the same text.
  for (let attempt = 0; attempt < MAX_SECTION_LABEL_PASSES; attempt += 1) {
    const seen = new Map<string, number>();
    const labels = groups.map((group, index) => sectionLabel(group.path, from[index]));
    for (const label of labels) seen.set(label, (seen.get(label) ?? 0) + 1);
    let grew = false;
    labels.forEach((label, index) => {
      if ((seen.get(label) ?? 0) > 1 && from[index] > 0) {
        from[index] -= 1;
        grew = true;
      }
    });
    if (!grew) break;
  }
  groups.forEach((group, index) => {
    sections.push({
      key: group.key,
      label: sectionLabel(group.path, from[index]),
      path: group.path,
      pinnedOnly: false,
    });
  });
  return sections;
}

export function controlsForSection(
  controls: readonly TableControl[],
  section: ControlSection,
  pinned: readonly number[],
): TableControl[] {
  if (section.pinnedOnly) {
    const pinnedIds = new Set(pinned);
    return controls.filter((control) => control.id !== null && pinnedIds.has(control.id));
  }
  if (!section.path) return [...controls];
  return controls.filter((control) => pathStartsWith(control.path, section.path!));
}


export interface RuntimeStateLike {
  record_id: number | null;
  active: boolean | null;
  value: string | null;
}

/**
 * The selection to persist for this exact table after a verified Apply.
 *
 * `pluginManaged` names the records CE Decky decided about itself, which carry
 * no user intent: a script switched on only because a cheat inside it was
 * selected is machinery, not a choice. Remembering one meant a later session
 * restored a running script with every cheat under it off - and it was never
 * needed, because auto-load already derives the scripts a remembered record
 * requires from the table's own structure.
 */
export function rememberedSelection(
  controls: readonly TableControl[],
  states: readonly RuntimeStateLike[],
  previous: readonly StartupPreference[],
  touchedActive: ReadonlySet<number>,
  touchedValues: ReadonlySet<number>,
  pluginManaged: ReadonlySet<number> = new Set(),
): StartupPreference[] {
  const controlById = new Map(controls.flatMap((control) => control.id === null ? [] : [[control.id, control] as const]));
  const stateById = new Map(states.flatMap((state) => state.record_id === null ? [] : [[state.record_id, state] as const]));
  const previousById = new Map(previous.map((item) => [item.record_id, item]));
  const ids = new Set<number>([...previousById.keys(), ...touchedActive, ...touchedValues]);
  const remembered: StartupPreference[] = [];
  for (const recordId of [...ids].sort((a, b) => a - b)) {
    if (pluginManaged.has(recordId)) continue;
    const control = controlById.get(recordId);
    const state = stateById.get(recordId);
    if (!control) continue;
    const prior = previousById.get(recordId);
    if (!state) {
      // The record has no state row because it no longer exists - its enclosing
      // script was switched off, which is what destroys the records it created.
      // Dropping the entry here would silently keep a stale "active" preference
      // for an ID the user just switched off, and the next session would put it
      // back. An explicitly touched record therefore keeps its deliberate
      // inactive choice; an untouched one keeps whatever it had.
      if (touchedActive.has(recordId)) {
        // A switch's value follows its toggle here too. The record is gone, so
        // what is stored now is what the next session replays, and storing the
        // on key beside "off" writes the cheat's own on value into the game and
        // then releases it, which is the cheat on rather than off.
        const value = controlIsSwitch(control) ? switchValueFor(control, false) : prior?.value ?? null;
        remembered.push({ record_id: recordId, active: false, value });
      } else if (prior) {
        remembered.push(prior);
      }
      continue;
    }
    const active = touchedActive.has(recordId) ? state.active : prior?.active ?? null;
    // A switch's toggle is its value, so touching the one touches the other.
    // Remembering `active` alone would replay the activation next session with
    // nothing written, and a flag frozen at whatever the game happens to hold
    // is a cheat that reports itself on and is off.
    const valueTouched = touchedValues.has(recordId)
      || (controlIsSwitch(control) && touchedActive.has(recordId));
    // Cheat Engine reports `??` for a record it cannot read yet. Remembering
    // that would replay a meaningless write on the next session and would erase
    // the value the user actually chose, so keep the previous one instead.
    const observed = valueTouched
      ? (control.kind === "value" || control.kind === "dropdown" ? displayableControlValue(state.value) : null)
      : null;
    const value = valueTouched ? observed ?? prior?.value ?? null : prior?.value ?? null;
    if (active !== null || value !== null) remembered.push({ record_id: recordId, active, value });
  }
  return remembered;
}

/**
 * The startup action Cheat Engine refused to switch **on**, if any.
 *
 * Startup runs without anyone watching, so its refusals never reached the one
 * place that records a table as not working - and a table written for a
 * different build of the game fails here first, every launch, before the user
 * ever opens the picker.
 *
 * Only a refused enable qualifies, for the same reason it does in the picker: a
 * cheat that would not switch off is very likely still running in the game, and
 * calling the table unusable there misdescribes it.
 *
 * The direction comes from the result, not from the saved selection. The bridge
 * emits `activation_rejected` only after reading the record back and finding it
 * in the state that was *not* asked for, so the reported state is the exact
 * negation of the attempted one: `active: false` is a refused enable. Reading
 * it from the profile instead was wrong twice over - session preparation adds
 * the enclosing scripts a remembered cheat needs, and it also overrides a
 * parent saved as off to on when an active child requires it, so a saved
 * `false` is not evidence that off is what startup attempted.
 */
export function refusedStartupEnable(results: readonly RuntimeResult[]): RuntimeResult | null {
  return results.find((result) =>
    result.generation === 0
    && !result.ok
    && result.error_code === "activation_rejected"
    && result.record_id !== null
    // Never `!== true`: a record that reported no state at all says nothing
    // about which direction was attempted.
    && result.active === false,
  ) ?? null;
}

export function effectiveStartupPreferences(
  startup: readonly StartupPreference[],
  remembered: readonly StartupPreference[],
): StartupPreference[] {
  const byId = new Map(startup.map((item) => [item.record_id, { ...item }]));
  for (const item of remembered) {
    const base = byId.get(item.record_id);
    byId.set(item.record_id, {
      record_id: item.record_id,
      active: item.active !== null ? item.active : base?.active ?? null,
      value: item.value !== null ? item.value : base?.value ?? null,
    });
  }
  return [...byId.values()].sort((a, b) => a.record_id - b.record_id);
}

export function rememberedSelectionBudgetError(
  startup: readonly StartupPreference[],
  remembered: readonly StartupPreference[],
): string | null {
  if (remembered.length > MAX_REMEMBERED_CONTROLS) {
    return `This selection would remember ${remembered.length} controls; the safe per-table limit is ${MAX_REMEMBERED_CONTROLS}. Narrow the remembered selection before applying.`;
  }
  const actionCount = effectiveStartupPreferences(startup, remembered)
    .reduce((count, item) => count + Number(item.active !== null) + Number(item.value !== null), 0);
  if (actionCount > MAX_EFFECTIVE_STARTUP_ACTIONS) {
    return `This selection would create ${actionCount} autoload actions; the safe per-session limit is ${MAX_EFFECTIVE_STARTUP_ACTIONS}. Reduce startup/remembered fields before applying.`;
  }
  return null;
}

export interface StartupParentWarning {
  childId: number;
  parentId: number;
}

export function startupParentWarnings(
  controls: readonly TableControl[],
  startup: readonly StartupPreference[],
): StartupParentWarning[] {
  const configured = new Map(startup.map((preference) => [preference.record_id, preference]));
  const controlsByPath = new Map<string, TableControl[]>();
  for (const control of controls) {
    if (control.id === null) continue;
    const key = JSON.stringify(control.path);
    controlsByPath.set(key, [...(controlsByPath.get(key) ?? []), control]);
  }
  const warnings: StartupParentWarning[] = [];
  for (const child of controls) {
    if (child.id === null || !configured.has(child.id)) continue;
    for (let depth = 1; depth < child.path.length; depth += 1) {
      const parents = controlsByPath.get(JSON.stringify(child.path.slice(0, depth))) ?? [];
      for (const parent of parents) {
        if (parent.id === child.id || configured.get(parent.id!)?.active === true) continue;
        warnings.push({ childId: child.id, parentId: parent.id! });
      }
    }
  }
  return warnings;
}

export function compactSha(value: string | null): string {
  return value ? `${value.slice(0, 12)}…` : "n/a";
}

export function pageCount(total: number, pageSize: number): number {
  if (!Number.isSafeInteger(pageSize) || pageSize < 1) throw new Error("page size must be a positive integer");
  return Math.ceil(Math.max(0, total) / pageSize);
}

export function clampPage(page: number, total: number, pageSize: number): number {
  const pages = pageCount(total, pageSize);
  if (!Number.isFinite(page)) return 0;
  return Math.max(0, Math.min(Math.trunc(page), pages - 1));
}

export function pageItems<T>(items: readonly T[], page: number, pageSize: number): T[] {
  const safePage = clampPage(page, items.length, pageSize);
  const start = safePage * pageSize;
  return items.slice(start, start + pageSize);
}

export function stepPage(page: number, total: number, pageSize: number, direction: -1 | 1): number {
  const current = clampPage(page, total, pageSize);
  return clampPage(current + direction, total, pageSize);
}

/**
 * Whether a script this control cannot exist without cannot be addressed.
 *
 * A record inside a script does not exist until that script has run, so
 * switching the record on means switching its enclosing scripts on first. When
 * two MemoryRecords in the exact table share an enclosing script's ID, no
 * command names one of them - and the child was still offered, because
 * ambiguity was only ever checked on the record itself. Switching it on then
 * waits for something Cheat Engine never creates and reports the child as the
 * failure. A dependency that cannot be addressed makes the descendant
 * unactionable, exactly as the backend's startup plan now refuses it.
 */
function hasUnaddressableEnclosingScript(
  control: TableControl,
  controls: readonly TableControl[],
  ambiguous: ReadonlySet<number>,
): boolean {
  for (let depth = 1; depth < control.path.length; depth += 1) {
    const prefix = control.path.slice(0, depth);
    const ancestor = controls.find((candidate) =>
      candidate.id !== null
      && candidate.id !== control.id
      && candidate.kind !== "group"
      && !candidate.group_header
      && candidate.path.length === prefix.length
      && candidate.path.every((segment, index) => segment === prefix[index]),
    );
    if (ancestor && ancestor.id !== null && ambiguous.has(ancestor.id)) return true;
  }
  return false;
}

export function safeActionableControls(inspection: TableInspection | null): TableControl[] {
  if (!inspection) return [];
  const ambiguous = new Set(inspection.ambiguous_record_ids);
  return inspection.controls.filter((control) =>
    control.id !== null && control.kind !== "group" && !control.group_header && !ambiguous.has(control.id)
    && !hasUnaddressableEnclosingScript(control, inspection.controls, ambiguous),
  );
}

export function filterControls(controls: readonly TableControl[], query: string): TableControl[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return [...controls];
  return controls.filter((control) =>
    `${control.description} ${control.path.join(" ")} ${control.id ?? ""}`
      .toLowerCase()
      .includes(normalized),
  );
}

/**
 * Above this many declared values, a dropdown gets a search box of its own.
 *
 * The parser's ceiling on a value list was raised to 16384 for a real table
 * whose pickers declare 6508 items each, and that made those tables parse
 * without giving anyone a way to use them: the values went straight into one
 * Decky dropdown, which a controller walks one item at a time and offers no
 * search, no paging and no jump. A list this size is still walkable; past it
 * the list is narrowed by typing first.
 */
export const DROPDOWN_SEARCH_THRESHOLD = 24;
/** How many values are ever handed to one dropdown at once. */
export const DROPDOWN_VALUE_LIMIT = 24;

export interface DropdownValueMatches {
  /** Exactly the values the table declares, never more than the limit. */
  options: [string, string][];
  /**
   * How many of `options` are answers to the query.
   *
   * Separate from `options.length` because the record's own value is kept on
   * offer whether or not it matches, and it takes the place of a match when it
   * does not: counting the two together said "showing all 24 matching" on a
   * list that was showing 23 of them, and "nothing matches" beside a value
   * that was still on screen.
   */
  shown: number;
  /** How many of the table's values the query matches. */
  matched: number;
  /** How many the table declares in total. */
  total: number;
  /** Whether the record's own value is on offer only because it is that. */
  selectionKept: boolean;
}

/**
 * The values of one large dropdown that are worth offering right now.
 *
 * Matching is on the label and on the value itself, because a table's own item
 * list is written as `1:Fire` and a user looking for item 4211 has the number
 * rather than the name. The chosen value is always among the options even when
 * it does not match, so the dropdown can show what this record is set to; the
 * selection therefore stays one of the table's own declared values, which is
 * the only thing that may be written back to a read-only picker.
 */
export function matchingDropdownValues(
  values: readonly (readonly [string, string])[],
  query: string,
  selected: string | null,
  limit = DROPDOWN_VALUE_LIMIT,
): DropdownValueMatches {
  const normalized = query.trim().toLowerCase();
  const matches: [string, string][] = [];
  let matched = 0;
  let chosen: [string, string] | null = null;
  for (const [value, label] of values) {
    if (selected !== null && value === selected) chosen = [value, label];
    if (normalized && !`${label} ${value}`.toLowerCase().includes(normalized)) continue;
    matched += 1;
    if (matches.length < limit) matches.push([value, label]);
  }
  if (!chosen || matches.some(([value]) => value === chosen![0])) {
    return { options: matches, shown: matches.length, matched, total: values.length, selectionKept: false };
  }
  // The record's own value goes at the top and takes a slot rather than an
  // extra one, so the number of options stays bounded. What it costs is one
  // matching value, and that is counted separately from the total number
  // rendered, because a screen that says it is showing all the matches while
  // one of them was dropped for the selection is telling the user something
  // that is not true about a list they cannot see the end of.
  if (matches.length >= limit) matches.pop();
  return {
    options: [chosen, ...matches],
    shown: matches.length,
    matched,
    total: values.length,
    selectionKept: true,
  };
}

/** What a narrowed value list is showing, and out of how much. */
export function describeValueChoices(choices: DropdownValueMatches): string {
  const kept = choices.selectionKept
    ? " The value this record is set to is kept at the top."
    : "";
  if (choices.matched === 0) {
    return `Nothing matches. This record declares ${choices.total} values.${kept}`;
  }
  const shown = choices.shown < choices.matched
    ? `Showing ${choices.shown} of ${choices.matched} matching`
    : `Showing all ${choices.matched} matching`;
  return `${shown} \u00b7 ${choices.total} declared. Type above to narrow the list.${kept}`;
}

export function sortPinnedControls(controls: readonly TableControl[], pinned: readonly number[]): TableControl[] {
  const pinnedIds = new Set(pinned);
  return controls
    .map((control, index) => ({ control, index }))
    .sort((left, right) => {
      const pinDelta = Number(pinnedIds.has(right.control.id ?? -1)) - Number(pinnedIds.has(left.control.id ?? -1));
      return pinDelta || left.index - right.index;
    })
    .map(({ control }) => control);
}

export function latestRuntimeResult(results: readonly RuntimeResult[], recordId: number): RuntimeResult | null {
  for (let index = results.length - 1; index >= 0; index -= 1) {
    if (results[index].record_id === recordId) return results[index];
  }
  return null;
}

export function runtimeControlDescription(
  control: TableControl,
  results: readonly RuntimeResult[],
  pendingLabel?: string | null,
): string {
  if (control.id === null) return control.kind;
  if (pendingLabel) return `${control.kind} · ID ${control.id} · ${pendingLabel}`;
  const latest = latestRuntimeResult(results, control.id);
  if (!latest) return `${control.kind} · ID ${control.id} · not queried`;
  if (!latest.ok) return `${control.kind} · ID ${control.id} · ERROR ${presentableControlValue(latest.error) ?? "unknown"}`;
  const active = latest.active === null ? "" : latest.active ? " · active" : " · inactive";
  const presentable = latest.value === null ? null : presentableControlValue(latest.value) ?? "";
  const value = presentable === null ? "" : ` · value ${presentable}`;
  return `${control.kind} · ID ${control.id}${active}${value}`;
}

// Third-party stores are often present in the Steam library as their own entry,
// so a game started through one leaves two library apps reporting as running and
// the automatic selection has to give up. These are the launcher executables
// themselves - never a game - so dropping them can leave exactly one real
// candidate. Matching is only on the executable basename: a game can live
// below a store-owned directory such as `GOG Galaxy/Games`, so a parent path is
// not identity evidence and must never make CE Decky drop that game.
const KNOWN_LAUNCHER_EXECUTABLES: readonly string[] = [
  "epicgameslauncher.exe",
  "galaxyclient.exe",
  "gog galaxy.exe",
  "origin.exe",
  "eadesktop.exe",
  "ealauncher.exe",
  "battle.net.exe",
  "battle.net launcher.exe",
  "upc.exe",
  "ubisoftconnect.exe",
  "ubisoftgamelauncher.exe",
  "uplay.exe",
  "rockstarlauncher.exe",
  "socialclubhelper.exe",
  "playgameslauncher.exe",
  "amazon games.exe",
  "itch.exe",
  "playnite.desktopapp.exe",
  "playnite.fullscreenapp.exe",
  "steam.exe",
  "bethesdanetlauncher.exe",
  "glyphclient.exe",
  "riotclientservices.exe",
];

function normalizedPath(value: string): string {
  // Steam stores a shortcut target with its surrounding quotes, so an unquoted
  // basename comparison would silently never match.
  return value.trim().replace(/^"+|"+$/g, "").replace(/\\/g, "/").toLowerCase();
}

/** True when this executable is a store/launcher client rather than a game. */
export function isKnownLauncherExecutable(executable: string | null | undefined): boolean {
  if (!executable) return false;
  const path = normalizedPath(executable);
  const basename = path.slice(path.lastIndexOf("/") + 1);
  return KNOWN_LAUNCHER_EXECUTABLES.includes(basename);
}

// A Proton prefix runs a full Windows environment, so the live process table for
// one game also contains Wine's own services, Proton's helpers and the game's
// crash reporter. None of them ever owns the game's memory, and offering a dozen
// of them turns a controller choice into a guessing game.
//
// The backend already drops everything running out of the prefix's own Windows
// directory, which covers Wine's built-in programs whatever a future Proton
// names them. This list covers only what that rule cannot see: Proton helpers
// that live in the tool directory rather than in the prefix, the crash and
// telemetry helpers a game ships beside its own binary, and the same built-in
// names again as a second net. Names taken from the executables in the
// installed GE-Proton11-2 tree, not from recollection.
//
// Matching is only on the exact executable basename, never a path: a real game
// binary can live anywhere. Generic built-in names such as `find.exe` or
// `net.exe` are deliberately absent, because a game could plausibly ship one and
// the directory rule already covers them.
const WINE_RUNTIME_EXECUTABLES: readonly string[] = [
  // Wine services and identity-bearing programs.
  "explorer.exe",
  "services.exe",
  "winedevice.exe",
  "plugplay.exe",
  "rpcss.exe",
  "svchost.exe",
  "spoolsv.exe",
  "dllhost.exe",
  "conhost.exe",
  "ntoskrnl.exe",
  "winmgmt.exe",
  "wuauserv.exe",
  "wineboot.exe",
  "winebrowser.exe",
  "winecfg.exe",
  "wineconsole.exe",
  "winedbg.exe",
  "winefile.exe",
  "winemenubuilder.exe",
  "winemine.exe",
  "winemsibuilder.exe",
  "winepath.exe",
  "winevdm.exe",
  "winver.exe",
  "winhelp.exe",
  "winhlp32.exe",
  // Windows utilities Wine provides; none is ever a game binary.
  "rundll32.exe",
  "regsvr32.exe",
  "regedit.exe",
  "taskmgr.exe",
  "control.exe",
  "uninstaller.exe",
  "msiexec.exe",
  "tabtip.exe",
  "presentationfontcache.exe",
  "iexplore.exe",
  "wscript.exe",
  "cscript.exe",
  "mshta.exe",
  "powershell.exe",
  "notepad.exe",
  "wordpad.exe",
  "wmplayer.exe",
  "progman.exe",
  "oleview.exe",
  "dxdiag.exe",
  "msinfo32.exe",
  // Proton helpers that run from the tool directory, outside the prefix.
  "xalia.exe",
  "bridge.exe",
  "umu.exe",
  "belauncher.exe",
  "steamerrorreporter.exe",
  "steamwebhelper.exe",
  "steamservice.exe",
  "gameoverlayui.exe",
  // Crash and telemetry helpers games ship beside their own binary.
  "crashpad_handler.exe",
  "crashreportclient.exe",
  "unrealcefsubprocess.exe",
  "epicwebhelper.exe",
];

/**
 * Executables that positively identify a known anti-cheat as part of this game.
 *
 * Exact basenames only, taken from the launcher this project has actually
 * observed. Nothing here is inferred from a game title, and nothing acts on the
 * anti-cheat itself: this list exists so a security-relevant observation the UI
 * already makes reaches the policy layer instead of being consumed as
 * target-selection noise and discarded.
 */
const KNOWN_ANTI_CHEAT_EXECUTABLES: readonly string[] = [
  "belauncher.exe",
];

/** The known anti-cheat basename observed in this process set, if any. */
export function observedAntiCheat(processNames: readonly string[]): string | null {
  for (const name of processNames) {
    const path = normalizedPath(name);
    const basename = path.slice(path.lastIndexOf("/") + 1);
    if (KNOWN_ANTI_CHEAT_EXECUTABLES.includes(basename)) return basename;
  }
  return null;
}

/**
 * The refusal text for a game observed running a known anti-cheat.
 *
 * The documented boundary is offline/single-player use with warning or refusal
 * rather than bypass automation, and this is that refusal. It never disables,
 * hides from, or interferes with the anti-cheat in any way.
 */
export function antiCheatBlockedReason(processNames: readonly string[]): string | null {
  const observed = observedAntiCheat(processNames);
  if (!observed) return null;
  return `This game is running ${observed}, a known anti-cheat. CE Decky is for offline and single-player use and will not attach Cheat Engine to it.`;
}

/** True when this executable belongs to the Wine/Proton runtime rather than the game. */
export function isWineRuntimeExecutable(executable: string | null | undefined): boolean {
  if (!executable) return false;
  const path = normalizedPath(executable);
  const basename = path.slice(path.lastIndexOf("/") + 1);
  return WINE_RUNTIME_EXECUTABLES.includes(basename) || isKnownLauncherExecutable(basename);
}

/**
 * Drop Wine/Proton runtime processes from an observed target-process list.
 *
 * Filtering to nothing is a real answer, not a failure to hand back. Both lists
 * this classifies against are exact basenames, so an empty result means every
 * process observed so far is one that never owns a game's memory - normally
 * because Review was opened during startup, before the game's own binary
 * appeared. Returning the unfiltered list there let an ordinary race default
 * the target to a helper like `xalia.exe`, persist it to the profile on
 * `Use this table`, and then attach the table to that helper in later sessions
 * until the user repaired it by hand. The caller shows "not observed yet"
 * instead, and manual entry stays available as an explicit override.
 */
export function withoutWineRuntimeProcesses(candidates: readonly string[]): readonly string[] {
  return candidates.filter((candidate) => !isWineRuntimeExecutable(candidate));
}

/**
 * Programs that ship inside a game's folder and never own the game's memory.
 *
 * Only for executables read off the disk. The live-process lists above answer
 * the same question for what is actually running, and none of these ever
 * appears there in a healthy session, which is why they were never needed: a
 * crash handler runs after the game has stopped and a redistributable runs once
 * at install. Walking the folder is what puts them in front of a reader, and
 * `UnityCrashHandler64.exe` sits in the root of a Unity title beside the game's
 * own binary, which is the position that otherwise decides a default.
 *
 * Exact basenames, like every other list here, and deliberately short. A name
 * this does not know costs one more row in a picker the user is reading; a name
 * wrongly on it would hide a real game binary, which is the expensive mistake.
 */
const INSTALLED_HELPER_EXECUTABLES: readonly string[] = [
  "unitycrashhandler32.exe",
  "unitycrashhandler64.exe",
  "ueprereqsetup_x64.exe",
  "ueprereqsetup_x86.exe",
  "dxsetup.exe",
  "vcredist_x64.exe",
  "vcredist_x86.exe",
  "vc_redist.x64.exe",
  "vc_redist.x86.exe",
  "oalinst.exe",
  "dotnetfx.exe",
  "directx_setup.exe",
  "touchup.exe",
];

/**
 * The library entries this device can actually do anything with.
 *
 * Steam's library belongs to an account, so a second device lists every title
 * the first one has: a Steam Deck beside a Steam Machine offered 39 non-Steam
 * shortcuts while its own store held 6, and each of the other 33 is a game
 * whose files are on the other machine. Nine of its 22 installed Steam apps
 * were Proton builds and Steam Linux Runtimes, which are installed and are not
 * games. All of that is a thumbstick's worth of scrolling to reach the title
 * somebody actually came for.
 *
 * What is kept is what the device's own files say it has: an `appmanifest` that
 * says fully installed, or an entry in this device's own `shortcuts.vdf`. What
 * Steam calls a tool rather than a game is dropped even though it is installed.
 *
 * A library the backend could not read keeps everything. Hiding a game the user
 * has is the worse mistake of the two, and an unreadable answer is not evidence
 * that a game is missing.
 */
export function gamesOnThisDevice(
  games: readonly GameSummary[],
  library: LocalLibrary | null | undefined,
): readonly GameSummary[] {
  if (!library) return games;
  const installed = new Set(library.steam_app_ids ?? []);
  const unstartable = new Set(library.unstartable_app_ids ?? []);
  const shortcuts = new Set(library.shortcut_app_ids ?? []);
  return games.filter((game) => {
    if (game.isShortcut) {
      // The shortcut store is the only thing that knows a shortcut is this
      // device's, so an unreadable one means every shortcut stays.
      return library.shortcuts_reason !== null || shortcuts.has(game.appId);
    }
    if (library.reason !== null) return true;
    return installed.has(game.appId) && !unstartable.has(game.appId);
  });
}

/**
 * Whether an executable found in a game's folder is one worth offering at all.
 *
 * The Wine and launcher rule first, because a game folder can hold a store
 * client, and then the installers and crash handlers above.
 */
export function isInstalledGameExecutable(name: string): boolean {
  return isValidProcessBasename(name)
    && !isWineRuntimeExecutable(name)
    && !INSTALLED_HELPER_EXECUTABLES.includes(name.toLowerCase());
}

/**
 * The game's own executables, worth offering, best evidence first.
 *
 * Where Steam's own record answered, the order is Steam's: it lists what it
 * starts first and its options after, and `game/bin/win64/cs2.exe` being three
 * directories down says nothing against it.
 *
 * Where a walk of the folder is all there is, depth is the whole of the ranking
 * and it is not a guess about names: the executable that owns a game's memory
 * is at the root far more often than not, and Half-Life 2 is the case that
 * shows what the alternative costs. Its root holds exactly `hl2.exe` while
 * `bin/` holds thirty-odd SDK compilers, all of which read like plausible
 * programs and none of which is the game.
 */
export function installedGameExecutables(
  listing: GameExecutableListing | null | undefined,
): readonly GameExecutable[] {
  // What Steam declares is kept whatever it is called. The list below exists to
  // clean up a walk of a folder, where a crash handler and a redistributable sit
  // beside the game and read exactly like it, and none of those is ever a launch
  // entry. Steam does start some games through a store client of its own, and
  // dropping that left a screen with Steam's answer taken and no walk behind it:
  // no candidates at all, for a game whose program is perfectly well known.
  if (listing?.source === "steam") {
    return (listing.executables ?? []).filter((item) => isValidProcessBasename(item.name));
  }
  const found = (listing?.executables ?? []).filter((item) => isInstalledGameExecutable(item.name));
  return [...found].sort((left, right) =>
    left.depth - right.depth
    || left.directory.localeCompare(right.directory)
    || left.name.localeCompare(right.name));
}

/**
 * The executable Steam itself starts for this game, where Steam says so.
 *
 * This is the strongest thing that can be known about a game that has never
 * run, and it is not read off a disk: the client cannot start a game without
 * knowing what to start, and this is that same record, for the branch this
 * device actually has installed. Half-Life 2 is why it matters. Its folder
 * holds twenty-eight Windows executables and Steam's record holds one line,
 * `hl2.exe`.
 *
 * It is still not a claim that this executable owns the game's memory: a game
 * that starts through a launcher of its own declares the launcher, which is
 * exactly what Steam starts. So it is a default the reader confirms with the
 * press that uses the table, and the first real launch checks it - the panel
 * says so when the game is running and the saved process is not among what it
 * started.
 */
export function declaredLaunchExecutable(
  listing: GameExecutableListing | null | undefined,
): string | null {
  if (listing?.source !== "steam") return null;
  return installedGameExecutables(listing).find((item) => item.declared)?.name ?? null;
}

/**
 * The one executable in a game's own root, where the root holds exactly one.
 *
 * This is the only thing read off a disk that may preselect anything, and the
 * condition is deliberately the strictest one available: one candidate at the
 * top level of the game's own folder, with nothing else there to be confused
 * with. Two of them is not a weaker version of this case, it is a question, and
 * the reader answers it.
 *
 * It is still not a claim that this executable owns the game's memory. Nothing
 * off a disk can be: the game has never run. What it is, is a choice the reader
 * confirms with the press that uses the table, on a screen that says where the
 * name came from, and which the first real launch checks - the panel says so
 * when the game is running and the saved process is not among what it started.
 */
export function soleInstalledExecutable(
  listing: GameExecutableListing | null | undefined,
): string | null {
  const root = installedGameExecutables(listing).filter((item) => item.depth === 0);
  return root.length === 1 ? root[0].name : null;
}

/**
 * Choose the target process the Review modal starts on.
 *
 * The user should not have to know which of a game's Windows executables owns
 * its memory, so a default is always offered when anything at all is known;
 * only a table that names nothing for a game that is not running leaves the
 * manual entry selected.
 *
 * Ranked by how exact the evidence is, never by how a name reads. Matching a
 * game's display name against its executables was considered and rejected on
 * target evidence: for `Lumen Hollow: Voyage 12` it scores the launcher
 * `Voyage12_Steam.exe` highest while the executable that actually owns the
 * game's memory, `LumenHollow-Win64-Shipping.exe`, shares nothing with the title.
 * The launch executable is used as the inverse signal instead, which is exact.
 */
export function defaultTargetProcess(input: {
  confirmed?: string | null;
  tableHints?: readonly string[];
  observed?: readonly string[];
  launchExecutable?: string | null;
  /**
   * What this game holds, ranked below everything observed.
   *
   * Reached only when nothing else says anything: no process confirmed for this
   * game, nothing running, and a table that names none. That is the case this
   * was added for, and before it the screen had no candidate at all and its one
   * press could not be made until the game had been started once. Steam's own
   * launch record answers it exactly where it is available, and a walk of the
   * game's folder is what is left when it is not.
   */
  installed?: GameExecutableListing | null;
}): string {
  const tableHints = (input.tableHints ?? []).filter(isValidProcessBasename);
  const observed = (input.observed ?? []).filter(isValidProcessBasename);
  const launcher = input.launchExecutable ? processBasename(input.launchExecutable) : "";
  const notTheLauncher = (candidates: readonly string[]) =>
    candidates.find((candidate) => candidate.toLowerCase() !== launcher) ?? candidates[0] ?? "";

  // A process the user already confirmed for this game normally stays
  // confirmed. The one exception is stronger fresh evidence that the saved
  // choice is the exact executable Steam asked Proton to launch and another
  // game process is running beside it. This repairs an older automatically
  // accepted launcher default without guessing from names. A table that
  // explicitly names that launcher can still select it in the hinted branch.
  const confirmed = input.confirmed && isValidProcessBasename(input.confirmed) ? input.confirmed : "";
  const confirmedIsLauncher = Boolean(confirmed && launcher && confirmed.toLowerCase() === launcher);
  const runningAlternative = observed.some((candidate) => candidate.toLowerCase() !== launcher);
  if (confirmed && (!confirmedIsLauncher || !runningAlternative)) return confirmed;

  if (observed.length > 0) {
    // A name the table carries that is also running right now. Cheat Engine
    // matches process names case-insensitively, so use the observed spelling.
    const hinted = new Set(tableHints.map((hint) => hint.toLowerCase()));
    const running = observed.filter((candidate) => hinted.has(candidate.toLowerCase()));
    if (running.length > 0) return notTheLauncher(running);
    // The table names something, but nothing running answers to it. Two tables
    // for the same reviewed game name `LumenHollowEos-Win64-Shipping.exe`, which is
    // the Epic build; what the user is running is the truth, not the hint.
    return notTheLauncher(observed);
  }

  // Nothing is running, so the table's own hint is the best evidence there is.
  if (tableHints.length > 0) return notTheLauncher(tableHints);

  // And where the table names nothing either, what Steam starts for this game,
  // which is the exact program the client would run rather than something read
  // off a disk. Failing that, the game's own files under the one condition that
  // leaves no question to answer: exactly one executable in the root of the
  // game's folder.
  return declaredLaunchExecutable(input.installed) ?? soleInstalledExecutable(input.installed) ?? "";
}

function processBasename(value: string): string {
  const path = normalizedPath(value);
  return path.slice(path.lastIndexOf("/") + 1);
}

/**
 * The executable name inside a launch target, with the spelling it was written
 * with.
 *
 * `processBasename()` exists for evidence matching and lower-cases for it.
 * Steam records a shortcut's target and Proton reports a launch target as the
 * user or the packager spelled it, and that spelling is what a person
 * recognises in a list and what gets stored as the target process.
 */
export function launchExecutableBasename(value: string | null | undefined): string {
  if (!value) return "";
  const text = value.trim().replace(/^"+|"+$/g, "").replace(/\\/g, "/");
  return text.slice(text.lastIndexOf("/") + 1);
}

/**
 * Drop launcher entries from an ambiguous running-game observation.
 *
 * Returns the original list whenever filtering would leave nothing, so a
 * misclassified entry can never hide the only running game.
 */
/**
 * The two ways a search result can be recognised as a table already proved bad.
 *
 * A result carries a provider row always and an advertised content digest only
 * sometimes, so the digest alone left the mark invisible on most rows. Both
 * lookups are built from one record, and built in one place because the panel
 * needs them at open and the search screen needs them again after it clears
 * some: a detached modal never sees its props change.
 */
export interface BlockedMark {
  /** What clears this record: the digest, or the row a digest-less one holds. */
  sha256: string;
  /** The recorded sentence, for a screen with room to read one. */
  reason: string;
  /** The same statement in one token, for a screen that has room for a chip. */
  cause: BlockedTableCause;
  /** When it was recorded, seconds since the epoch, so newest wins by itself. */
  recordedAt: number;
}

export interface BlockedLookups {
  /** Exact table SHA-256 to the mark on it. One digest is one record. */
  byDigest: Record<string, BlockedMark>;
  /**
   * `provider:artifact_id` to every record reached through that row, newest first.
   *
   * A list rather than one mark, because one provider row serves several
   * revisions over the years and each of them can earn its own record: a table
   * that came straight back off in March, and the file being gone in August,
   * are two true statements about the same row and neither replaces the other.
   * Collapsing them let whichever the loop happened to write last stand for all
   * of them, so a source failure could be hidden behind an old exact-byte
   * refusal, and clearing the row's marks took one press per record with the
   * next one appearing only after the first was gone.
   */
  byArtifact: Record<string, BlockedMark[]>;
}

/**
 * Short display names for the providers CE Decky knows by id.
 *
 * A provider id is a lowercase token and reads like one, and a screen that
 * names a provider must never name the wrong one: the download screen was
 * titled after Playground whatever provider the artifact came from, which on
 * this device was FearLess for the whole session.
 */
const SHORT_PROVIDER_NAMES: Record<string, string> = {
  fearless: "FearLess",
  playground: "Playground",
  github: "GitHub",
  thecheatscript: "The Cheat Script",
  vgtimes: "VGTimes",
  // Searched by no version of this plugin any more. The name stays because a
  // table imported while it was still a source keeps its origin, and a row that
  // names where it came from should keep saying it in words.
  opencheattables: "OpenCT",
};

/** The provider's short name, or the best fallback the caller has. */
export function providerShortName(provider: string, fallback?: string | null): string {
  return SHORT_PROVIDER_NAMES[provider] ?? (fallback || provider);
}

/**
 * What one blocked record is identified and cleared by.
 *
 * The backend sends it, and this derives it anyway: the panel and the backend
 * are briefly out of step across a plugin reload, and a record from before this
 * field existed still has to be clearable rather than throwing on a row.
 */
export function blockedKey(entry: BlockedTable): string {
  return entry.key ?? entry.sha256 ?? entry.origins?.[0] ?? "";
}

const BLOCKED_CAUSES: readonly BlockedTableCause[] = ["refused", "unusable", "encrypted", "gone", "unknown"];

/**
 * What one entry in the not-working list is called, game first.
 *
 * A file name is not an answer to "what is this table for". The list spans
 * every game and is read months later, and it was showing rows like
 * `winmm-x64.zip` and a raw `provider:artifact_id` key, neither of which named
 * a game or a table: the user could not tell whether the rows they had just
 * marked in one game's search were even in it. The game leads because that is
 * what the list is scanned by.
 *
 * The fallbacks are what identity the entry actually has: the downloaded file
 * name, else the digest that is its identity, else the provider row that is.
 */
export function blockedRowLabel(entry: BlockedTable): string {
  const name = entry.filename
    ?? (entry.sha256 ? entry.sha256.slice(0, 12) : entry.origins?.[0] ?? blockedKey(entry));
  return entry.game_name ? `${entry.game_name} · ${name}` : name;
}

/**
 * The longest a shortened recorded reason may be before it is cut.
 *
 * Long enough for the whole of the sentence the runtime writes for the failure
 * this record is almost always about, and short enough that a status line puts
 * the reason and the release in front of the reader together.
 */
const SHORT_REASON_MAX = 120;

/**
 * The head of a recorded reason, for a row that has to lead with something else.
 *
 * Two screens show these records and both of them are lists: a row is scanned
 * rather than read, and the durable sentence is written to be read once, in
 * full, by whoever is answering a bug report. The one the runtime writes for a
 * cheat that came straight back off is 230 characters, of which the first
 * clause says what happened and the rest explains why it usually happens - so a
 * row led with a paragraph, and the date, the release and the game it belongs
 * to were past the end of the line.
 *
 * This takes the first sentence and nothing else. The record itself is not
 * touched: it is evidence, the archive carries it whole, and the row still
 * reveals the rest of its own line under focus.
 */
export function shortBlockedReason(reason: string | null | undefined): string | null {
  const text = (reason ?? "").trim();
  if (!text) return null;
  // A sentence end is a full stop followed by a space, never a full stop on its
  // own: a version, a file name and a digest all carry one with no space after
  // it, and cutting there would end a row in the middle of `1.05.01`.
  const end = text.search(/\.\s/);
  const first = end >= 0 ? text.slice(0, end + 1) : text;
  if (first.length <= SHORT_REASON_MAX) return first;
  // Still too long for a row, so it is cut at a word rather than mid-token, and
  // marked as cut. The whole of it is one press or one reveal away.
  const cut = first.slice(0, SHORT_REASON_MAX);
  const space = cut.lastIndexOf(" ");
  return `${(space > SHORT_REASON_MAX / 2 ? cut.slice(0, space) : cut).trimEnd()}\u2026`;
}

/**
 * The release a source stated for a stored table's newest download.
 *
 * One implementation of a rule three screens and the backend all needed, and
 * which the backend's own copy had already drifted from: the newest origin and
 * no other, because a version belonging to an earlier download is a different
 * table's release. A table opened from a local file has no origin and therefore
 * no release, which is the honest answer rather than a gap to fill from the
 * file itself - the `.CT` carries only `CheatEngineTableVersion`, which is the
 * version of Cheat Engine's table format.
 */
export function advertisedRelease(table: Pick<TableStatus, "origins"> | null | undefined): string | null {
  const origins = table?.origins ?? [];
  const newest = origins.length ? origins[origins.length - 1] : null;
  return releaseLabel(newest?.version);
}

/**
 * The release a not-working record was offered under, from wherever it is known.
 *
 * The record's own field first, because it is what was true when the mark was
 * written and it is the only one that survives the table being deleted. A copy
 * still on this device is the fallback, for a record written before the field
 * existed, and it is looked up by digest so it can only ever answer about these
 * exact bytes.
 */
export function blockedRelease(
  entry: BlockedTable,
  tables: readonly TableStatus[] = [],
): string | null {
  const recorded = releaseLabel(entry.table_version);
  if (recorded) return recorded;
  if (!entry.sha256) return null;
  return advertisedRelease(tables.find((table) => table.sha256 === entry.sha256));
}

/**
 * What one not-working row says under its name, in the order it is scanned in.
 *
 * When it happened, which release it was, which build of the game it was tried
 * against, and only then what happened. It used to open with the reason, which
 * for the failure this list is almost always about is a 230 character sentence
 * whose second half explains why that failure is usual: the row therefore led
 * with a paragraph and the three facts that place the record were past the end
 * of the line. The reason is still here and still reveals itself under focus,
 * shortened to its first sentence, and the whole of it is in the support
 * archive where a bug report reads it.
 */
export function blockedRowDetail(
  entry: BlockedTable,
  tables: readonly TableStatus[] = [],
): string {
  return [
    blockedRecordedOn(entry.recorded_at),
    blockedRelease(entry, tables),
    // The build it was tried against is what dates the evidence: a game update
    // is the ordinary reason a table stops working, and the same one is the
    // ordinary reason it starts working again.
    entry.game_version ? `game ${entry.game_version}` : null,
    shortBlockedReason(entry.reason),
  ].filter(Boolean).join(" \u00b7 ");
}

/** The day a record was made, which is all a user needs to place it. */
export function blockedRecordedOn(seconds: number): string | null {
  if (!Number.isFinite(seconds) || seconds <= 0) return null;
  const when = new Date(seconds * 1000);
  return Number.isNaN(when.getTime()) ? null : when.toISOString().slice(0, 10);
}

export function blockedTableLookups(tables: readonly BlockedTable[]): BlockedLookups {
  const byDigest: Record<string, BlockedMark> = {};
  const byArtifact: Record<string, BlockedMark[]> = {};
  for (const entry of tables) {
    // A record written before the cause was tracked, and one naming a cause
    // this panel does not know, both say only that the table does not work,
    // which is what the panel then says about it. The backend fails soft on the
    // same question for the same reason: the mark is a label on a record that
    // is refusing an import either way.
    const cause: BlockedTableCause = entry.cause !== undefined && BLOCKED_CAUSES.includes(entry.cause)
      ? entry.cause
      : "unknown";
    // A record whose file the source no longer has never produced bytes, so it
    // has no digest to look up by: the provider row is the whole of it. What a
    // row carries here is what clears it, which for those entries is the key.
    const recordedAt = Number.isFinite(entry.recorded_at) ? entry.recorded_at : 0;
    if (entry.sha256) byDigest[entry.sha256] = { sha256: entry.sha256, reason: entry.reason, cause, recordedAt };
    for (const origin of entry.origins ?? []) {
      (byArtifact[origin] ??= []).push({
        sha256: entry.sha256 ?? blockedKey(entry), reason: entry.reason, cause, recordedAt,
      });
    }
  }
  // Newest first, decided here rather than inherited from the order the record
  // arrived in: which of a row's marks drives its badge, its reason and its
  // download is a question about when they were written, and a reader that
  // depends on the caller having sorted them answers it by accident.
  for (const marks of Object.values(byArtifact)) marks.sort((left, right) => right.recordedAt - left.recordedAt);
  return { byDigest, byArtifact };
}

/**
 * The records that stop a download of a row that has not said what it serves.
 *
 * Not every not-working record does. `refused` is a statement about exact
 * bytes: Cheat Engine ran them and the record came straight back off, which is
 * what a table written for an older build of the game does, and which stops
 * being true when the game updates or the author republishes. The provider row
 * those bytes arrived through is how search recognises the post again; it is
 * not a claim that the post still holds them. `unknown` is the same statement
 * from a record that could not say more, and is read the same way.
 *
 * The other three describe what came through the row. The file is gone from it,
 * or what it served was not a table at all, or was an archive nothing here can
 * open. On a source that publishes no digest there is nothing to tell those
 * apart from what the row holds today, so they stop the download and
 * **Retry** is what says to try anyway.
 */
const SOURCE_LEVEL_BLOCKS: readonly BlockedTableCause[] = ["gone", "unusable", "encrypted"];

/**
 * The records that survive the source naming the bytes it will serve.
 *
 * `unusable` and `encrypted` are written against the exact digest of what was
 * downloaded, so a fresh result advertising a different digest is direct
 * evidence that the record is not about these bytes: the row was replaced, and
 * refusing the new file over the old one is refusing bytes nothing here has
 * ever seen. `gone` is the one cause with no digest of its own, because nothing
 * was ever downloaded for it, so it stays a fact about the row.
 */
const ROW_LEVEL_BLOCKS: readonly BlockedTableCause[] = ["gone"];

/**
 * Which half of a row a durable refusal actually refuses.
 *
 * One record is projected into both lookups: a table proven not to work is
 * keyed by its exact SHA and again by every provider row it was downloaded
 * from, so that the mark is visible on a source that advertises no digest.
 * Read as one answer, that projection said two different things at once. The
 * saved copy was refused, correctly, and the download was refused with it,
 * which is wrong: the bytes on this device are not the bytes the row would
 * serve next, and re-downloading is the only way the user finds out that the
 * source has published a fix.
 *
 * So the two are decided apart, and by what each record is about rather than
 * by which revision of the row happens to be on this device now. A row that
 * has served several revisions carries a record per revision, and an exact-byte
 * refusal of any of them, the one currently saved or an older one, refuses only
 * those bytes.
 *
 * What refuses the download depends on whether the source has said what it will
 * serve. Where it advertises a digest, that digest is the answer: bytes already
 * recorded as not working are refused, and every record naming other bytes is
 * about a revision this row no longer offers, so it refuses nothing. Only a
 * record with no digest of its own, which is the file being gone, is still
 * about the row. Where the source advertises nothing, there is nothing to tell
 * an old failure from the current file, so the newest failure of what came
 * through the row stands and **Retry** is what says to try it anyway.
 *
 * `history` is the third answer, and it refuses nothing. It is the newest
 * exact-byte refusal this row has earned that is neither of the other two: a
 * revision that came straight back off, on a row whose source has not been
 * accused of anything. A row that has only that and nothing on this device to
 * offer is worth naming and not worth pressing, because pressing it spends a
 * download to be told by the importer what its own badge already says, and
 * **Retry** is the press that says to try it anyway. A row that does hold a
 * usable copy is a different thing entirely, and this must never reach it.
 */
export function tableRowRefusals(
  lookups: BlockedLookups,
  row: {
    /** The digest the provider advertises for what this row would serve. */
    advertisedSha256?: string | null;
    /** `provider:artifact_id`, where a provider row is in play. */
    artifactKey?: string | null;
    /** The exact SHA of the copy already on this device, where there is one. */
    savedSha256?: string | null;
  },
): { download: BlockedMark | null; saved: BlockedMark | null; history: BlockedMark | null } {
  const saved = row.savedSha256 ? lookups.byDigest[row.savedSha256.toLowerCase()] ?? null : null;
  const origin = row.artifactKey ? lookups.byArtifact[row.artifactKey] ?? [] : [];
  const advertisedKey = row.advertisedSha256 ? row.advertisedSha256.toLowerCase() : null;
  // The source has named the bytes it will serve and they are already known not
  // to work. Nothing else here is that specific about a download.
  const advertised = advertisedKey ? lookups.byDigest[advertisedKey] ?? null : null;
  const blocks = advertisedKey ? ROW_LEVEL_BLOCKS : SOURCE_LEVEL_BLOCKS;
  const download = advertised ?? origin.find((mark) => blocks.includes(mark.cause)) ?? null;
  // History names a row that is not worth pressing. A row whose source has
  // named unblocked bytes is worth pressing whatever happened to the revisions
  // before them, so it has none.
  const history = download || (advertisedKey && !advertised)
    ? null
    : origin.find((mark) => mark.sha256.toLowerCase() !== (row.savedSha256 ?? "").toLowerCase()) ?? null;
  return { download, saved, history };
}

export function withoutKnownLaunchers<T>(
  candidates: readonly T[],
  executableOf: (candidate: T) => string | null | undefined,
): readonly T[] {
  const games = candidates.filter((candidate) => !isKnownLauncherExecutable(executableOf(candidate)));
  return games.length > 0 ? games : candidates;
}


/** Leaf name of a control: the part that identifies it inside its group. */
export function controlRowLabel(control: TableControl): string {
  const leaf = control.path.length ? control.path[control.path.length - 1] : "";
  return leaf.trim() || control.description.trim() || `Record ${control.id ?? "?"}`;
}

/** Parent breadcrumb of a control, empty when it has no enclosing group. */
export function controlRowContext(control: TableControl): string {
  return control.path.slice(0, -1).join(" \u203a ");
}

/**
 * True when this record is drawn as a plain on/off switch.
 *
 * Its two list entries are an on/off pair, so the list and the field would both
 * be asking the reader to say again what the toggle beside them already says.
 * The backend decides this from the author's own labels; see `ct_inspector`.
 */
export function controlIsSwitch(control: TableControl): boolean {
  // A string, not merely "not null": a backend that predates this field sends
  // no field at all, and reading that as a switch would take the list away from
  // every two-entry record in the table.
  return typeof control.switch_on_value === "string"
    && control.switch_on_value !== ""
    && control.dropdown_values.length === 2;
}

/** The key a switch record's toggle writes, or `null` when it is not one. */
export function switchValueFor(control: TableControl, active: boolean | null): string | null {
  if (active === null || !controlIsSwitch(control)) return null;
  const on = control.switch_on_value as string;
  if (active) return on;
  // The off key is the other entry, whatever number the author gave it. It has
  // to be written: a record left at its on value with the freeze released is a
  // cheat still running under a switch that says it is off.
  return control.dropdown_values.find(([value]) => value !== on)?.[0] ?? null;
}

/** Both keys of a switch record, for a desired state to carry, else undefined. */
export function switchValuesFor(control: TableControl): { on: string; off: string } | undefined {
  const on = switchValueFor(control, true);
  const off = switchValueFor(control, false);
  return on !== null && off !== null ? { on, off } : undefined;
}

/**
 * The switches a script would turn on by itself, and the key that turns each off.
 *
 * A table's Auto Assembler script declares its own defaults, and they are not
 * modest: one real table declares 22 of its 24 flags as on, with damage times
 * ten and enemies that can barely see. Switching that script on because one
 * cheat under it was asked for therefore switched on most of the table, while
 * the panel counted the one cheat and said `1 active`.
 *
 * So every switch a script CE Decky enables declares as on is written to its off
 * key, unless the user asked for that one. It is sent as an ordinary desired
 * state with no active of its own: the record does not exist until the script
 * has run, which is exactly the case the runtime client's deferred path already
 * waits out.
 */
export function switchesToHoldOff(
  scripts: readonly TableControl[],
  controls: readonly TableControl[],
  requested: ReadonlySet<number>,
): { control: TableControl; value: string; script: number }[] {
  const held = new Map<number, { control: TableControl; value: string; script: number }>();
  for (const script of scripts) {
    if (script.id === null) continue;
    for (const control of controls) {
      if (control.id === null || control.id === script.id || requested.has(control.id)) continue;
      if (control.path.length <= script.path.length) continue;
      if (!script.path.every((segment, index) => control.path[index] === segment)) continue;
      // Only a flag this script declares as on: its address is a symbol the
      // script itself allocates, so it exists once the script has run, and
      // there is nothing to hold off about one the script leaves off anyway.
      // A declaration that could not be read holds nothing off, which is the
      // table behaving as it did before any of this.
      if (control.declared_default === null || control.declared_default !== control.switch_on_value) continue;
      const off = switchValueFor(control, false);
      // The script that brought it, so a later report can say which one did.
      // The innermost wins: a flag under two nested scripts is that one's.
      if (off !== null) held.set(control.id, { control, value: off, script: script.id });
    }
  }
  return [...held.values()];
}

/**
 * What this table does on its own, for the one sentence Review says about it.
 *
 * A script's declarations are the author's preset rather than the user's
 * choice, and a reader deciding whether to use a table is entitled to know it
 * switches most of itself on. Counted from what the scripts declare, so a table
 * whose declarations cannot be read says nothing rather than guessing, and a
 * table that declares nothing on says nothing either: there is no finding.
 */
export function scriptDefaultsOn(inspection: TableInspection | null): { on: number; switches: number } | null {
  // Counted over what the picker will actually draw, so the number on this
  // screen is the number of switches the reader then meets: a record with a
  // duplicate ID, or one under a script whose address cannot be resolved, is
  // offered nowhere and may not be counted here either.
  const switches = safeActionableControls(inspection).filter((control) => controlIsSwitch(control));
  const on = switches.filter((control) => control.declared_default === control.switch_on_value).length;
  return on > 0 ? { on, switches: switches.length } : null;
}

/** Every switch record's off key, for a call that only switches things off. */
export function switchOffValues(controls: readonly TableControl[]): Map<number, string> {
  const values = new Map<number, string>();
  for (const control of controls) {
    if (control.id === null) continue;
    const off = switchValueFor(control, false);
    if (off !== null) values.set(control.id, off);
  }
  return values;
}

/** True when this exact control accepts a typed value. */
export function controlAcceptsTypedValue(control: TableControl): boolean {
  if (controlIsSwitch(control)) return false;
  return control.kind === "value" || (control.kind === "dropdown" && !control.dropdown_read_only);
}

/**
 * How many names a refusal about empty values lists before it stops.
 *
 * Enough to find them on a page, short enough to stay one sentence. Past this
 * the count is the useful part: a user who switched on twelve of these is not
 * reading twelve names off a toast, they are going back to the list.
 */
const MISSING_VALUE_NAMES = 3;

/**
 * The cheats this Apply would switch on without the value they do nothing
 * without.
 *
 * A value or dropdown record is not finished by being switched on: Cheat Engine
 * freezes whatever the game happens to hold at that address, which is not what
 * the user asked for and is indistinguishable, afterwards, from a cheat that
 * did not work. Apply took it anyway, committed it to this table's startup
 * state, and reported success.
 *
 * Scoped to the records this Apply actually switches on. Two states are
 * deliberately outside it. A record that was already on and already had no
 * value is one this screen did not create, and refusing every Apply until it is
 * dealt with would block every other change the user came here to make. And
 * clearing the field of a record that is already on is a supported thing to
 * ask for: it drops the value this table has stored for the next session, and
 * it never writes a blank into a game that is holding a real number right now.
 */
export function controlsMissingRequiredValue(
  controls: readonly TableControl[],
  desired: ReadonlyMap<number, { active: boolean | null; value: string | null }>,
  switchedOn: ReadonlySet<number>,
): readonly TableControl[] {
  return controls.filter((control) => {
    if (control.id === null || !switchedOn.has(control.id)) return false;
    const state = desired.get(control.id);
    if (!state || state.active !== true) return false;
    return controlNeedsValueInput(control) && displayableControlValue(state.value) === null;
  });
}

/**
 * How many of a picker's staged records actually differ from what was confirmed.
 *
 * Counted by comparing, not by counting what the reader touched. Switching a
 * cheat on and off again leaves it in the touched set for the rest of the
 * screen's life, so a form returned to exactly the state it opened in still
 * said "1 unapplied change" and offered to discard it, which is a prompt about
 * nothing and teaches the reader to dismiss the one that matters.
 *
 * A value is compared through `displayableControlValue`, so an empty field, a
 * field of spaces and Cheat Engine's own unreadable placeholder are all the
 * same absence rather than three different edits.
 */
export function unsavedChangeCount(
  staged: Readonly<Record<number, { active: boolean | null; value: string | null }>>,
  confirmed: Readonly<Record<number, { active: boolean | null; value: string | null }>>,
  touched: Iterable<number>,
): number {
  let changed = 0;
  for (const recordId of new Set(touched)) {
    const now = staged[recordId];
    const before = confirmed[recordId];
    const activeChanged = (now?.active ?? null) !== (before?.active ?? null);
    const valueChanged = displayableControlValue(now?.value ?? null)
      !== displayableControlValue(before?.value ?? null);
    if (activeChanged || valueChanged) changed += 1;
  }
  return changed;
}

/**
 * The cheats pinned onto the panel that have no value to send when pressed.
 *
 * A pinned control is a switch on the quick access panel and nothing else:
 * there is no field on it, and there is nowhere to put one in a 300 pixel
 * column. So a value or dropdown record pinned without a value can only ever be
 * switched on empty, which writes nothing and freezes whatever the game holds
 * - the same thing Apply refuses for a cheat switched on here, reached from a
 * screen that has no field to fix it on.
 *
 * Asked of every pinned record rather than only the ones this press touched,
 * and without regard to whether it is switched on, because the press that will
 * switch it on happens somewhere else and later. Pinning itself is never
 * refused: it is committed on its own and is only metadata about which cheats
 * the panel shows, and Apply is the one place on this screen that reports a
 * missing value.
 */
export function pinnedMissingRequiredValue(
  controls: readonly TableControl[],
  desired: ReadonlyMap<number, { active: boolean | null; value: string | null }>,
  pinned: readonly number[],
): readonly TableControl[] {
  const shown = new Set(pinned);
  return controls.filter((control) => {
    if (control.id === null || !shown.has(control.id)) return false;
    return controlNeedsValueInput(control)
      && displayableControlValue(desired.get(control.id)?.value ?? null) === null;
  });
}

/** The refusal a caller reports for those, naming them where naming helps. */
export function missingRequiredValueReason(missing: readonly TableControl[]): string | null {
  if (missing.length === 0) return null;
  const names = missing.slice(0, MISSING_VALUE_NAMES).map(controlRowLabel);
  const rest = missing.length - names.length;
  const named = rest > 0 ? `${names.join(", ")} and ${rest} more` : names.join(", ");
  // Short enough to read as one line on a handheld, because that is what it is
  // shown as. Where to do it is not said: a record that is switched on and
  // needs a value keeps its editor open whatever else the list is doing, so the
  // field is already on screen on the row this names.
  return missing.length === 1
    ? `${named} needs a value. Enter one, or switch it back off.`
    : `${named} need values. Enter them, or switch them back off.`;
}

/**
 * True when this control does nothing until the user supplies a value.
 *
 * A script record is complete once it is active, but a value or dropdown record
 * only takes effect after something is written to it, so its editor must be on
 * screen the moment the record is switched on rather than behind More.
 */
export function controlNeedsValueInput(control: TableControl): boolean {
  // A switch carries a value by construction: its toggle writes one key or the
  // other, so there is never a moment where it is on with nothing written.
  if (controlIsSwitch(control)) return false;
  return controlAcceptsTypedValue(control)
    || (control.kind === "dropdown" && control.dropdown_values.length > 0);
}

/**
 * A runtime value worth showing, or `null`.
 *
 * Cheat Engine writes the literal `??` for a record it cannot read yet, which
 * is a state, not a value; repeating it on every compact row is noise.
 */
export function displayableControlValue(value: string | null | undefined): string | null {
  const text = (value ?? "").trim();
  return !text || text === "??" ? null : text;
}

/** Invisible and bidirectional formatting characters, replaced before display. */
const UNSAFE_DISPLAY_CONTROL_RE = /[\p{Cc}\p{Cf}]/gu;
const MAX_DISPLAYED_VALUE_LENGTH = 96;

/**
 * A runtime value in a form that is safe to put inside a sentence.
 *
 * Table labels, descriptions and process hints are already rejected by the
 * inspector when they carry invisible or bidirectional formatting, but values
 * are deliberately preserved byte for byte: normalizing one would change what
 * Cheat Engine receives. That exact string must therefore never be interpolated
 * into visible text directly - a value carrying RLO/RLI or zero-width
 * characters can reorder the compact line it sits in and misrepresent which
 * cheat a number belongs to. This is the display copy only; comparisons,
 * persistence and `set_value` keep the semantic string.
 */
export function presentableControlValue(value: string | null | undefined): string | null {
  const text = displayableControlValue(value);
  if (text === null) return null;
  const safe = text.replace(UNSAFE_DISPLAY_CONTROL_RE, "\uFFFD");
  return safe.length > MAX_DISPLAYED_VALUE_LENGTH
    ? `${safe.slice(0, MAX_DISPLAYED_VALUE_LENGTH)}\u2026`
    : safe;
}

/**
 * The one value a pinned control is about.
 *
 * Its row shows this, its switch may not be pressed without it where the record
 * needs one, and pressing that switch writes it. Those were three separate
 * resolutions reading three different sources, so a row could show `100` while
 * the press sent nothing and activated on whatever the game held, or show `40`
 * while the press wrote `100`. A switch has to mean what the row above it says.
 *
 * What is running comes first, then the choice this table confirmed, then the
 * one Configure stored: a value Cheat Engine can read is the truth about this
 * record, and the stored ones are what to say when it cannot read one yet.
 *
 * Displayability chooses which of the three answers; the answer itself comes
 * back exactly as it was stored. The two are not the same string: a record's
 * value is preserved byte for byte here, because it is what gets written into
 * the game, while `displayableControlValue` trims for a row. Returning the
 * trimmed one meant a switch that only re-applies what the row already shows
 * could rewrite ` 100 ` as `100`.
 */
export function pinnedControlValue(
  live: string | null | undefined,
  remembered: string | null | undefined,
  configured: string | null | undefined,
): string | null {
  for (const source of [live, remembered, configured]) {
    if (source != null && displayableControlValue(source) !== null) return source;
  }
  return null;
}

export interface PinnedCheatRow {
  recordId: number;
  label: string;
  summary: string;
  active: boolean | null;
}

/**
 * Pinned controls promoted onto the CE Decky panel, in table order.
 *
 * Only a live exact-session result can be shown here: an unqueried or failed
 * record must never render as a real toggle state, so it is dropped rather than
 * guessed. The list is bounded because the panel is one Decky QAM column.
 */
export function pinnedCheatRows(
  controls: readonly TableControl[],
  pinned: readonly number[],
  results: readonly RuntimeResult[],
  remembered: readonly StartupPreference[] = [],
  configured: readonly ConfiguredValue[] = [],
  limit = CONTROL_PAGE_SIZE,
): PinnedCheatRow[] {
  const pinnedIds = new Set(pinned);
  const rememberedById = new Map(remembered.map((item) => [item.record_id, item]));
  const configuredById = new Map(configured.map((item) => [item.record_id, item]));
  const rows: PinnedCheatRow[] = [];
  for (const control of controls) {
    if (rows.length >= limit) break;
    if (control.id === null || !pinnedIds.has(control.id)) continue;
    const latest = latestRuntimeResult(results, control.id);
    if (!latest || !latest.ok) continue;
    const context = controlRowContext(control);
    // A value CE cannot read yet still has a confirmed choice behind it, so show
    // what this table was set to rather than nothing. Resolved once, where the
    // press on this row resolves it too.
    // A switch says what it is by being on or off, so `= 1` beside it is the
    // same fact written twice in the row's own scarcest space.
    const value = controlIsSwitch(control) ? null : presentableControlValue(pinnedControlValue(
      latest.value,
      rememberedById.get(control.id)?.value,
      configuredById.get(control.id)?.value,
    ));
    rows.push({
      recordId: control.id,
      label: controlRowLabel(control),
      summary: [context, value ? `= ${value}` : null].filter(Boolean).join(" · "),
      active: latest.active,
    });
  }
  return rows;
}

/**
 * The nearest enclosing control that is switched off, if any.
 *
 * A value inside a Cheat Engine group only has a resolvable address once the
 * script that creates it is enabled. Writing to it first fails verification -
 * Cheat Engine answers `??` - which reads to a user as "my value was ignored".
 * Naming the exact control they have to enable is the difference between a
 * dead end and an obvious next step.
 */
export function inactiveAncestorControl(
  control: TableControl,
  controls: readonly TableControl[],
  activeById: ReadonlyMap<number, boolean | null>,
): TableControl | null {
  const chain = inactiveAncestorControls(control, controls, activeById);
  return chain.length ? chain[chain.length - 1] : null;
}

/**
 * IDs of the controls whose group encloses at least one other control.
 *
 * These are the scripts a table uses to build its real cheats. CE Decky
 * switches them on by itself, so listing them beside the cheats a user
 * recognises just doubles the list they have to page through.
 */
export function enclosingControlIds(controls: readonly TableControl[]): Set<number> {
  const prefixes = new Set<string>();
  for (const control of controls) {
    for (let depth = 1; depth < control.path.length; depth += 1) {
      prefixes.add(JSON.stringify(control.path.slice(0, depth)));
    }
  }
  const ids = new Set<number>();
  for (const control of controls) {
    if (control.id !== null && prefixes.has(JSON.stringify(control.path))) ids.add(control.id);
  }
  return ids;
}

/**
 * Controls listed with the scripts rather than with the cheats.
 *
 * Two different things belong here for the same reason: neither is a choice a
 * user makes. An enclosing script is the machinery a table uses to build its
 * real cheats, and CE Decky switches it on by itself. An attach-only record is
 * the table author's own "attach to the game" button, which changes nothing in
 * the game and duplicates what CE Decky already did by exact PID.
 *
 * This is the view, not the dependency rule: only enclosing scripts are ever
 * switched on to reach something else, and that stays keyed off
 * `enclosingControlIds`.
 */
export function scriptListedControlIds(controls: readonly TableControl[]): Set<number> {
  const ids = enclosingControlIds(controls);
  for (const control of controls) {
    if (control.id !== null && control.attach_only) ids.add(control.id);
  }
  return ids;
}

/**
 * Enclosing scripts that are on with nothing under them on, deepest first.
 *
 * A script exists to create the records inside it, so one left running after
 * the last cheat that needed it was switched off is pure residue: it keeps its
 * patch in the game and, once remembered, switched itself back on in the next
 * session with every cheat under it off. Deepest first is what makes one pass
 * enough - an inner script counts as a user of the one around it, so it has to
 * be released before its parent is judged.
 */
export function unusedActiveScripts(
  controls: readonly TableControl[],
  activeById: ReadonlyMap<number, boolean | null>,
): number[] {
  const enclosing = enclosingControlIds(controls);
  const scripts = controls
    .filter((control): control is TableControl & { id: number } => control.id !== null && enclosing.has(control.id))
    .sort((left, right) => right.path.length - left.path.length);
  const active = new Map(activeById);
  const released: number[] = [];
  for (const script of scripts) {
    if (active.get(script.id) !== true) continue;
    const used = controls.some((candidate) =>
      candidate.id !== null
      && candidate.id !== script.id
      && candidate.path.length > script.path.length
      && script.path.every((segment, index) => candidate.path[index] === segment)
      && active.get(candidate.id) === true);
    if (used) continue;
    active.set(script.id, false);
    released.push(script.id);
  }
  return released;
}

/**
 * Every enclosing control that is switched off, outermost first.
 *
 * Switching a cheat on has to switch on the scripts that create it, and a
 * script two levels up is as necessary as the one directly above. Outermost
 * first is the order Cheat Engine has to receive them in, because an inner
 * script does not exist until the one enclosing it has run.
 */
export function inactiveAncestorControls(
  control: TableControl,
  controls: readonly TableControl[],
  activeById: ReadonlyMap<number, boolean | null>,
): TableControl[] {
  const chain: TableControl[] = [];
  for (let depth = 1; depth < control.path.length; depth += 1) {
    const prefix = control.path.slice(0, depth);
    const ancestor = controls.find((candidate) =>
      candidate.id !== null
      && candidate.id !== control.id
      && candidate.path.length === prefix.length
      && candidate.path.every((segment, index) => segment === prefix[index]),
    );
    if (ancestor && ancestor.id !== null && activeById.get(ancestor.id) === false) chain.push(ancestor);
  }
  return chain;
}

const PROVIDER_DISPLAY_NAMES: Record<string, string> = {
  fearless: "FearLess Cheat Engine",
  playground: "Playground",
  github: "GitHub",
  thecheatscript: "The Cheat Script",
  vgtimes: "VGTimes",
  // Retired as a source; kept as a name, for the tables it already provided.
  opencheattables: "Open Cheat Tables",
};

/**
 * A provider's own name, or its stored identifier when it has no known one.
 *
 * A table's origin records the provider identifier, which is a lookup key and
 * not something to show a user: "fearless" is not what that site calls itself.
 */
export function providerDisplayName(provider: string): string {
  return PROVIDER_DISPLAY_NAMES[provider] ?? provider;
}

/**
 * Which provider digests already resolve to a table this device already holds.
 *
 * Scoped to the store and deliberately not to the game's own library, which is
 * what it used to ask. "Local" answers whether these bytes have to be fetched
 * again, and content identity settles that on its own: the same table published
 * for two games, the same game reached through a second shortcut, and a first
 * import whose profile step failed after the bytes were already stored all made
 * the user sit through another provider countdown for a file on disk. Which
 * tables a game actually uses is a different question, answered by its imported
 * list and by Home. This screen already treats the other content-addressed fact
 * this way: a table marked as not working is marked in every game's search.
 *
 * The provider's advertised digest identifies the bytes it served. For a direct
 * `.CT` that is the table itself; for an archive it is the archive, whose
 * extracted `.CT` has its own SHA - which is why an already-imported archive
 * result used to be offered for download again. Both are content identity, so
 * both may resolve to this table. A provider artifact ID is not: the same ID can
 * serve changed bytes.
 *
 * An archive holding several tables is the exception: its digest is not the
 * identity of whichever member was imported first, and collapsing it to one made
 * the other members unreachable, because the row opened that table instead of
 * reopening member selection.
 *
 * An origin recorded before cardinality was persisted declares nothing, and
 * reading that silence as "one member" reintroduces exactly that bug for every
 * archive already imported. Only bytes that cannot hold a second table - a
 * direct `.CT` download - are unambiguous without a declared count; an archive
 * without one is reacquired and reinspected.
 */
export function localTableArtifacts(tables: readonly TableStatus[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const table of tables) {
    if (!table.available) continue;
    result[`sha:${table.sha256.toLowerCase()}`] = table.sha256;
    for (const origin of table.origins) {
      const unambiguous = origin.member_count === undefined || origin.member_count === null
        ? !isArchiveFilename(origin.original_filename)
        : origin.member_count <= 1;
      // Keyed in lower case at both ends. Recorded digests are normalised, but
      // what a provider advertises is whatever its page said, and a row whose
      // digest arrives in upper case is the same bytes: reading the two in
      // different cases hid a local copy behind a download.
      if (origin.advertised_sha256 && unambiguous) result[`sha:${origin.advertised_sha256.toLowerCase()}`] = table.sha256;
    }
  }
  return result;
}

/**
 * Which provider rows a table on this device was actually imported from.
 *
 * The weaker of the two answers, and a separate one on purpose. `localTableArtifacts`
 * answers "these are those bytes" and is what lets a press skip the download;
 * this answers only "a table on this device came from this exact row", which
 * two sources of four cannot say any other way: FearLess and every other phpbb
 * attachment advertise no digest at all, so every table already imported from
 * them was offered back with no mark on it and downloaded again to reach a
 * table the device already had.
 *
 * It never becomes the stronger answer, and it is a set of rows rather than a
 * lookup to the table each one produced so that it cannot be made into one. The
 * same artifact ID can serve changed bytes, so a row known only this way is
 * still reacquired on a press and the import still turns on the SHA of what
 * actually arrives; if the bytes have not
 * changed the store recognizes them and nothing is duplicated. What this buys
 * is the one thing the user could not get otherwise: knowing, before pressing,
 * which of twenty rows they have already been through.
 */
export function importedTableArtifacts(tables: readonly TableStatus[]): Set<string> {
  const result = new Set<string>();
  for (const table of tables) {
    if (!table.available) continue;
    for (const origin of table.origins) {
      result.add(`${origin.provider}:${origin.artifact_id}`);
    }
  }
  return result;
}

export type SelfTestVerdict = "pass" | "warn" | "fail";

export interface SelfTestSummary {
  verdict: SelfTestVerdict;
  label: string;
  counts: string;
  failures: SelfTestCheck[];
  toast: string;
}

/**
 * What a self-test result actually says, as opposed to what its `ok` says.
 *
 * The backend's `ok` means "nothing blocking failed", and several checks are
 * deliberately not blocking: the ones that answer whether a later bug report
 * will have any evidence in it, whether the managed root still has room, and
 * whether the registered Cheat Engine is still the one that was registered.
 * A screen that renders `ok` alone says PASS while one of those has failed,
 * and the detail that names the failure is then displayed nowhere at all -
 * which is the diagnostic detecting exactly the condition it was added for and
 * telling the user everything is fine.
 *
 * So there are three states rather than two, and the failed checks travel with
 * them. `ok === false` stays a failure even if no check says it is blocking,
 * because the backend is the authority on its own verdict and a disagreement
 * here must resolve towards the worse answer rather than the better one.
 */
export function selfTestSummary(result: SelfTestResult): SelfTestSummary {
  const checks = Array.isArray(result.checks) ? result.checks : [];
  // Blocking unless it says otherwise, which is the rule the backend derives
  // `ok` by: a check from a newer backend that carries no flag is not a
  // warning by default.
  const failures = checks.filter((check) => !check.ok);
  const blockers = failures.filter((check) => check.blocking !== false);
  const warnings = failures.length - blockers.length;
  const counts = `${checks.filter((check) => check.ok).length}/${checks.length} checks`;
  if (blockers.length > 0 || result.ok === false) {
    return {
      verdict: "fail",
      label: "Self-test FAIL",
      counts,
      failures: [...blockers, ...failures.filter((check) => check.blocking === false)],
      toast: "Plugin self-test found a blocker.",
    };
  }
  if (warnings > 0) {
    return {
      verdict: "warn",
      label: "Self-test WARN",
      counts,
      failures,
      toast: `Plugin self-test found ${warnings} warning${warnings === 1 ? "" : "s"}.`,
    };
  }
  return { verdict: "pass", label: "Self-test PASS", counts, failures: [], toast: "Plugin self-test passed." };
}

/**
 * A self-test check's name as a person reads it.
 *
 * Derived rather than mapped, so a check a later backend adds is readable here
 * without this file knowing about it: a mapping that has to be kept in step
 * would show an unknown check as nothing at all, which is the failure mode this
 * whole summary exists to remove.
 */
export function selfTestCheckLabel(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  return words === "" ? "unnamed check" : words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * The update section's own first line, said in the state the device is in.
 *
 * Five states, and each one leads with the thing a reader acts on. A failed
 * check is the one that must not read as "up to date": a device that could not
 * ask is not a device that has nothing to install, and the two used to look the
 * same on every updater anybody has used.
 */
export function updateSummary(
  state: PluginUpdateState | null,
  currentVersion: string,
): { label: string; description: string } {
  if (!state) {
    return {
      label: `CE Decky v${currentVersion}`,
      description: "This build does not report its update state.",
    };
  }
  const checked = updateCheckedOn(state.checked_at);
  const when = checked ? `Checked ${checked}.` : "Not checked yet.";
  // What was found and whether anything has answered since are two facts, and
  // this line carries the first. The second is a row of its own rather than a
  // clause appended here: these rows are cut to one line until they are opened,
  // so a sentence added to the end of this one is a sentence nobody reads.
  // What this line owes the reader is the date, which is the last check that
  // answered rather than the last one attempted.
  // The newest thing this device knows wins the line, and the newest thing is
  // the attempt that failed. A finding from before it is still true and still
  // worth naming, but as something found then rather than as the state of the
  // project now: with the failure said only in the row below, a device that had
  // not reached GitHub for a week read exactly like one that had just confirmed
  // the offer.
  if (state.last_error && state.update_available && state.latest_version) {
    return {
      label: `v${state.latest_version} was found${checked ? ` on ${checked}` : ""}`,
      description: `This device is on v${state.current_version}. The latest check did not finish, so this may no longer be the newest release.`,
    };
  }
  if (state.update_available && state.latest_version) {
    return {
      label: `v${state.latest_version} is available`,
      description: `This device is on v${state.current_version}. ${when}`,
    };
  }
  if (state.last_error) {
    return {
      label: `CE Decky v${state.current_version}`,
      description: when,
    };
  }
  if (!state.auto_check && !state.checked_at) {
    return {
      label: `CE Decky v${state.current_version}`,
      description: "Automatic checking is off. Check now asks once, without switching it back on.",
    };
  }
  if (!state.checked_at) {
    return {
      label: `CE Decky v${state.current_version}`,
      description: "No check has run yet. One happens after a table search, or press Check now.",
    };
  }
  return { label: `CE Decky v${state.current_version} is up to date`, description: when };
}

/**
 * One sentence, ended, so the next one can follow it.
 *
 * What goes in front of it here is somebody else's message - Decky's, GitHub's,
 * an exception's - and those end where they end. Joining one to a sentence of
 * ours gave the screen "Decky did not report this plugin at the new version The
 * checked release is saved at /home/...".
 */
export function sentence(text: string): string {
  const trimmed = text.trim();
  if (trimmed === "") return trimmed;
  // Started as well as ended. These messages are written as clauses, because
  // most of them are read inside one: `this is already the newest release`
  // under a label is a fragment, and on screen it looked like a line that had
  // lost its beginning. Only an ASCII lower-case letter is raised, so a path,
  // a version or a quoted name is left exactly as it was written.
  const opened = /^[a-z]/.test(trimmed) ? `${trimmed[0].toUpperCase()}${trimmed.slice(1)}` : trimmed;
  return /[.!?:;]$/.test(opened) ? opened : `${opened}.`;
}

/** The day a check ran, which is all a user needs to place it. */
export function updateCheckedOn(seconds: number | null): string | null {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds <= 0) return null;
  const when = new Date(seconds * 1000);
  return Number.isNaN(when.getTime()) ? null : when.toISOString().slice(0, 10);
}

/**
 * The version the home panel offers, which is not the same as the one it knows.
 *
 * Three things have to hold before a press belongs on a 300 pixel panel: there
 * is a newer release, the user has not switched automatic checking off, and
 * this device can actually carry the install out. Advanced shows the finding
 * whatever the last two say, because that is the screen the switch is on and
 * the screen that explains an install this device cannot do.
 */
export function panelUpdateOffer(update: PluginUpdateState | null): string | null {
  if (!update || !update.update_available || !update.latest_version) return null;
  if (!update.auto_check || !update.install_supported) return null;
  // And not while the newest thing this device tried was a check that failed.
  // The finding behind the press would then be older than what is known about
  // it, and a press on a 300 pixel panel carries none of that: it says a
  // version and offers to install it. Advanced still shows the finding, says
  // the latest check did not finish, and can still start the update from there.
  if (update.last_error) return null;
  return update.latest_version;
}
