import { describe, expect, it } from "vitest";
import {
  selfTestCheckLabel,
  selfTestSummary,
  blockedRowLabel,
  blockedRowDetail,
  absentLiveTarget,
  installedGameExecutables,
  declaredLaunchExecutable,
  gamesOnThisDevice,
  soleInstalledExecutable,
  blockedRelease,
  shortBlockedReason,
  releaseLabel,
  sharedContextDepth,
  blockedTableLookups,
  refusedStartupEnable,
  CONTROL_PAGE_SIZE,
  controlAcceptsTypedValue,
  controlIsSwitch,
  DROPDOWN_SEARCH_THRESHOLD,
  DROPDOWN_VALUE_LIMIT,
  describeValueChoices,
  matchingDropdownValues,
  controlNeedsValueInput,
  switchOffValues,
  switchValueFor,
  switchesToHoldOff,
  switchValuesFor,
  controlsMissingRequiredValue,
  missingRequiredValueReason,
  pinnedMissingRequiredValue,
  unsavedChangeCount,
  controlRowContext,
  controlRowLabel,
  displayableControlValue,
  presentableControlValue,
  antiCheatBlockedReason,
  observedAntiCheat,
  importedTableArtifacts,
  localTableArtifacts,
  pinnedCheatRows,
  pinnedControlValue,
  providerDisplayName,
  leftOnSentence,
  switchesLeftOn,
  tableSourceLabel,
  derivedSummaryLine,
  fearlessIndexSentence,
  blockedRecordShape,
  providerShortName,
  controlSections,
  sharedSectionDepth,
  sectionLabel,
  MAX_SECTION_LABEL,
  controlsForSection,
  effectiveStartupPreferences,
  filterControls,
  defaultTargetProcess,
  isExactAttachedRuntime,
  isWineRuntimeExecutable,
  isExactRuntimeSession,
  isKnownLauncherExecutable,
  isValidProcessBasename,
  latestRuntimeResult,
  pageCount,
  pageItems,
  rememberedSelection,
  enclosingControlIds,
  scriptListedControlIds,
  unusedActiveScripts,
  rememberedSelectionBudgetError,
  runtimeAttachCandidates,
  safeActionableControls,
  scriptDefaultsOn,
  scriptDefaultsSentence,
  switchAfterStopRefusal,
  sortPinnedControls,
  startupParentWarnings,
  withoutKnownLaunchers,
  divergentLiveTarget,
  withoutWineRuntimeProcesses,
} from "../src/uiModel";
import type { TableControl, TableInspection } from "../src/types";

function control(id: number | null, kind: TableControl["kind"], description = `Control ${id}`): TableControl {
  return {
    id,
    description,
    path: ["Root", description],
    variable_type: kind === "script" ? "Auto Assembler Script" : "4 Bytes",
    kind,
    group_header: kind === "group",
    has_assembler_script: kind === "script",
    dropdown_values: kind === "dropdown" ? [["0", "Off"], ["1", "On"]] : [],
    dropdown_read_only: kind === "dropdown",
  };
}

function inspection(controls: TableControl[], ambiguous: number[] = []): TableInspection {
  return {
    sha256: "1".repeat(64),
    table_version: "45",
    total_entries: controls.length,
    has_lua: false,
    has_auto_assembler: controls.some((item) => item.kind === "script"),
    embedded_files: 0,
    process_candidates: ["game.exe"],
    controls,
    ambiguous_record_ids: ambiguous,
    unsupported_record_id_count: controls.filter((item) => item.id === null).length,
  };
}

describe("the label a release is shown under", () => {
  it("prefixes a bare number and never a tag that already carries one", () => {
    // GitHub advertises its tag verbatim, so a repository tagged v2 was shown
    // as vv2 on the search rows, in Manage and under Advanced alike.
    expect(releaseLabel("2")).toBe("v2");
    expect(releaseLabel("2.1")).toBe("v2.1");
    expect(releaseLabel("v2")).toBe("v2");
    expect(releaseLabel("V2.1")).toBe("v2.1");
  });

  it("leaves a release that is a name rather than a number alone", () => {
    expect(releaseLabel("release-42")).toBe("release-42");
    expect(releaseLabel("latest")).toBe("latest");
    // Starting with a digit does not make it a version. A source that releases
    // by date wrote that date, and decorating it is the same kind of mutation
    // of a source's own label this function exists to stop.
    expect(releaseLabel("2026-08")).toBe("2026-08");
    expect(releaseLabel("1.2-rc1")).toBe("1.2-rc1");
    expect(releaseLabel("v2026-08")).toBe("v2026-08");
  });

  it("claims nothing where the source published nothing", () => {
    expect(releaseLabel(null)).toBeNull();
    expect(releaseLabel(undefined)).toBeNull();
    expect(releaseLabel("   ")).toBeNull();
  });
});

describe("controller UI model", () => {

  it("names The Cheat Script consistently in acquisition and provenance UI", () => {
    expect(providerShortName("thecheatscript")).toBe("The Cheat Script");
    expect(providerDisplayName("thecheatscript")).toBe("The Cheat Script");
  });

  it("names VGTimes consistently in acquisition and provenance UI", () => {
    expect(providerShortName("vgtimes")).toBe("VGTimes");
    expect(providerDisplayName("vgtimes")).toBe("VGTimes");
  });

  it("names GitHub for both of its routes rather than for releases alone", () => {
    // The same provider now answers a search of the repository index and
    // resolves the exact repository another catalog links, and a table it
    // serves may be committed in a tree rather than published as a release.
    expect(providerShortName("github")).toBe("GitHub");
    expect(providerDisplayName("github")).toBe("GitHub");
  });

  it("calls a copy CE Decky made Fixed rather than Local", () => {
    // `Local` says where the file is, which is true of every table the panel
    // can show. What the reader needs to know about this one is that it is not
    // the table they downloaded: it is that table with what stopped it working
    // taken out, and it came from no site at all.
    const derived = { origins: [], derived_from: { sha256: "a".repeat(64), transforms: ["remove-signature"] } };
    expect(tableSourceLabel(derived as any)).toBe("Fixed");
    // A table the device holds and nobody changed keeps the answer it had.
    expect(tableSourceLabel({ origins: [], derived_from: null } as any)).toBe("Local");
    // And one that came from a source is still named by that source, whether
    // or not anything was done to it afterwards.
    expect(tableSourceLabel({ origins: [{ provider: "vgtimes" }], derived_from: null } as any)).toBe("vgtimes");
    expect(tableSourceLabel(null)).toBe("Local");
  });

  it("puts what a fixed copy was made from, and what it cost, on one row", () => {
    // The screen that names where a table came from has nothing to say about a
    // copy CE Decky made: no provider served those bytes. This is that row, and
    // it is a row rather than the whole account, which the press holds.
    const derivation = {
      sha256: "c".repeat(64),
      transforms: ["remove-signature", "drop-unmatched-scans"],
      scans: ["aobHealth", "aobAmmo"],
      orphaned: ["Health"],
    };
    expect(derivedSummaryLine(derivation, "Downloaded.CT"))
      .toBe("from Downloaded.CT · signature removed · code for 2 missing patterns removed · 1 cheat gone");
    // The source table is named by its digest where this device no longer
    // holds it, and no cheat lost is a finding of its own rather than silence.
    expect(derivedSummaryLine({ ...derivation, scans: [], orphaned: [] }, null))
      .toBe(`from ${"c".repeat(12)} · signature removed · code for a missing pattern removed · no cheat lost`);
    // A transform this build cannot name is said to be one, and a table nobody
    // derived has no row at all.
    expect(derivedSummaryLine({ ...derivation, transforms: ["something-later"], orphaned: [] }, "T.CT"))
      .toBe("from T.CT · a change this version cannot name · no cheat lost");
    expect(derivedSummaryLine(null)).toBeNull();
    // A record naming no transform still leaves a row, because a label with
    // nothing under it is the state this row was added to end.
    expect(derivedSummaryLine({ ...derivation, transforms: [], orphaned: [] }, "T.CT"))
      .toBe("from T.CT · what was changed is not recorded · no cheat lost");
  });

  it("says what the indexed source's own copy of the listing holds", () => {
    // One source is not searched the way the others are: that forum has no
    // search route, so this device reads its listing pages and keeps them. A
    // table on a page it has not read yet is absent from the copy rather than
    // from the source, and a result count cannot say that.
    const now = Date.UTC(2026, 8, 22, 12, 0, 0);
    const row = (overrides: Record<string, unknown>) => ([{
      provider: "fearless", provider_display_name: "FearLess Cheat Engine",
      results: 3, status: "indexing", error: null, ...overrides,
    }] as any);

    const indexing = fearlessIndexSentence(row({
      indexed_pages: 12, total_pages: 42, indexed_topics: 900, stale_pages: 30, missing_pages: 30,
      last_refresh_at: now / 1000 - 600, last_refresh_pages: 5,
    }), now);
    expect(indexing).toContain("12 of its 42 listing pages are indexed here, holding 900 tables.");
    expect(indexing).toContain("30 pages have still to be read");
    expect(indexing).toContain("searching again once the index has caught up is what finds it.");
    expect(indexing).toContain("The last pass read 5 pages 10 minutes ago.");
    expect(indexing).not.toContain("due to be read again");

    // Two of the twelve held pages have aged out. They are still in the copy
    // and still searched, so they are not among the pages a table can hide on.
    const aged = fearlessIndexSentence(row({
      indexed_pages: 12, total_pages: 42, indexed_topics: 900, stale_pages: 32, missing_pages: 30,
    }), now);
    expect(aged).toContain("30 pages have still to be read");
    expect(aged).not.toContain("32 pages");
    expect(aged).toContain("2 indexed pages are due to be read again, and are still searched until then.");
    // An answer from before never-read and due-again were told apart names
    // neither, rather than calling every owed page missing.
    const older = fearlessIndexSentence(row({ indexed_pages: 12, total_pages: 42, indexed_topics: 900, stale_pages: 32 }), now);
    expect(older).not.toContain("still to be read");
    expect(older).not.toContain("due to be read again");
    // A listing that shrank leaves more pages held than it has, and a page of
    // it as it is now that was never read still hides whatever it lists.
    const shrunk = fearlessIndexSentence(row({ indexed_pages: 42, total_pages: 40, indexed_topics: 900, stale_pages: 1, missing_pages: 1 }), now);
    expect(shrunk).not.toContain("the whole listing is indexed");
    expect(shrunk).toContain("39 of its 40 listing pages are indexed here");
    expect(shrunk).toContain("1 page has still to be read");

    // A complete index answers the other question: how fresh the whole of it
    // is, which is the age of its least recently read page.
    const whole = fearlessIndexSentence(row({
      status: "ok", indexed_pages: 42, total_pages: 42, indexed_topics: 1200,
      stale_pages: 2, fully_refreshed_at: now / 1000 - 3 * 3600,
    }), now);
    expect(whole).toContain("the whole listing is indexed, 42 pages, holding 1200 tables.");
    expect(whole).toContain("Every page has been read, the oldest of them 3 hours ago.");
    expect(whole).toContain("2 pages are due to be read again.");

    // Two states the counts do not explain on their own.
    expect(fearlessIndexSentence(row({ indexed_pages: 4, stale_pages: 1, retry_after_seconds: 90 }), now))
      .toContain("asked CE Decky to wait, so nothing is read from it for another 90s.");
    // A kept answer counts that wait down from when the source said it, and a
    // wait that is over is not one to tell anybody about.
    const cooled = row({ indexed_pages: 4, stale_pages: 1, retry_after_seconds: 90 });
    expect(fearlessIndexSentence(cooled, now, now - 30_000)).toContain("for another 60s.");
    expect(fearlessIndexSentence(cooled, now, now - 30_000)).not.toContain("These counts are from that search");
    const over = fearlessIndexSentence(cooled, now, now - 120_000);
    expect(over).not.toContain("asked CE Decky to wait");
    // And a snapshot old enough for the index to have moved says whose counts these are.
    expect(over).toContain("These counts are from that search; searching again reads them as they are now.");
    expect(fearlessIndexSentence(row({ indexed_pages: 4, stale_pages: 1, error: "HTTP 503" }), now))
      .toContain("The last read of the listing did not finish: HTTP 503");

    // An empty index is a state rather than a count of nothing, and it is what
    // makes a search of that source find nothing whatever the source holds.
    const empty = fearlessIndexSentence(row({
      status: "unavailable", indexed_pages: 0, total_pages: 42, indexed_topics: 0,
      stale_pages: 42, error: "HTTP 403",
    }), now);
    expect(empty).toContain("none of it has been read yet, so a search finds nothing there until it has.");
    expect(empty).not.toContain("holding 0 tables");
    expect(empty).toContain("The last read of the listing did not finish: HTTP 403");

    // Silence where there is nothing to say: no such source in this search, and
    // an answer from a build that carried no index state.
    expect(fearlessIndexSentence([], now)).toBeNull();
    expect(fearlessIndexSentence(null, now)).toBeNull();
    expect(fearlessIndexSentence(row({}), now)).toBeNull();
  });

  it("describes the record of refused tables by what its entries cannot say", () => {
    // The one row on that screen whose list is somewhere else. How many and how
    // recent are already on it; what it was missing is how far the record
    // spreads and how far back it goes.
    const entry = (overrides: Record<string, unknown>) => ({
      key: "k", sha256: "a".repeat(64), reason: "it did not work", filename: "T.CT",
      app_id: 10, game_name: "Example", game_version: null, table_version: null,
      recorded_at: 1_700_000_000, ...overrides,
    }) as any;
    expect(blockedRecordShape([])).toBeNull();
    // One game is named; a single entry says nothing about a span.
    expect(blockedRecordShape([entry({})])).toBe("Example");
    expect(blockedRecordShape([entry({}), entry({ recorded_at: 1_600_000_000 })]))
      .toBe("Example · since 2020-09-13");
    // A record written in one day says nothing about a span: the row beside
    // this already carries the newest, and printing that date twice is not a
    // second fact.
    expect(blockedRecordShape([entry({}), entry({})])).toBe("Example");
    // More games are counted, because a name here is longer than the row.
    expect(blockedRecordShape([entry({}), entry({ app_id: 20, game_name: "Other" })]))
      .toBe("2 games");
    // A row whose file the source no longer has never produced bytes, and an
    // entry belonging to no game of this device's is counted rather than lost.
    expect(blockedRecordShape([entry({}), entry({ app_id: null, game_name: null, sha256: null })]))
      .toBe("Example · 1 for no game of this device's · 1 without a file of its own");
  });

  it("requires prepared/status exact identity before Home treats runtime as attached", () => {
    const exact: any = {
      prepared: { session_id: "s", app_id: 10, ce_sha256: "a".repeat(64), table_sha256: "b".repeat(64), descriptor_sha256: "c".repeat(64) },
      status: { session_id: "s", app_id: 10, ce_sha256: "a".repeat(64), table_sha256: "b".repeat(64), descriptor_sha256: "c".repeat(64), attached: true, opened_process_id: 42 },
      connected: true, session_current: true,
    };
    expect(isExactRuntimeSession(exact, 10, "b".repeat(64))).toBe(true);
    expect(isExactAttachedRuntime(exact, 10, "b".repeat(64))).toBe(true);
    expect(isExactRuntimeSession({ ...exact, status: { ...exact.status, attached: false, opened_process_id: 0 } }, 10, "b".repeat(64))).toBe(true);
    expect(isExactAttachedRuntime({ ...exact, status: { ...exact.status, attached: false, opened_process_id: 0 } }, 10, "b".repeat(64))).toBe(false);
    expect(isExactAttachedRuntime({ ...exact, session_current: false }, 10, "b".repeat(64))).toBe(false);
    expect(isExactAttachedRuntime({ ...exact, status: { ...exact.status, session_id: "other" } }, 10, "b".repeat(64))).toBe(false);
    expect(isExactAttachedRuntime({ ...exact, status: { ...exact.status, descriptor_sha256: "d".repeat(64) } }, 10, "b".repeat(64))).toBe(false);
    expect(isExactAttachedRuntime(exact, 11, "b".repeat(64))).toBe(false);
    expect(isExactAttachedRuntime(exact, 10, "e".repeat(64))).toBe(false);
    // Attached is not loaded. A Cheat Engine that could not open the table
    // attaches anyway and reports itself healthy, with an address list that
    // answers "missing" for every record, so it is not a session anything here
    // can be run from. The session itself is still exactly this session.
    const emptyTable = { ...exact, status: { ...exact.status, table_load_state: "failed", table_load_error: "Cheat Engine refused to open the table" } };
    expect(isExactRuntimeSession(emptyTable, 10, "b".repeat(64))).toBe(true);
    expect(isExactAttachedRuntime(emptyTable, 10, "b".repeat(64))).toBe(false);
    // A bridge that states nothing about the load is an older one, unchanged.
    expect(isExactAttachedRuntime({ ...exact, status: { ...exact.status, table_load_state: "loaded" } }, 10, "b".repeat(64))).toBe(true);
    expect(isExactAttachedRuntime({ ...exact, status: { ...exact.status, table_load_state: "pending" } }, 10, "b".repeat(64))).toBe(true);
  });
  it("makes every safe actionable control reachable across pages without slice truncation", () => {
    const controls = [control(1, "group"), ...Array.from({ length: 37 }, (_, index) => control(index + 2, index % 5 === 0 ? "script" : "value"))];
    const safe = safeActionableControls(inspection(controls));
    const reached = Array.from({ length: pageCount(safe.length, CONTROL_PAGE_SIZE) }, (_, page) => pageItems(safe, page, CONTROL_PAGE_SIZE)).flat();
    expect(reached.map((item) => item.id)).toEqual(safe.map((item) => item.id));
    expect(reached).toHaveLength(37);
  });

  it("reports zero pages for an empty controller list while keeping page helpers stable", () => {
    expect(pageCount(0, CONTROL_PAGE_SIZE)).toBe(0);
    expect(pageItems([], 4, CONTROL_PAGE_SIZE)).toEqual([]);
  });

  it("excludes group headers, unsupported IDs, and ambiguous IDs from mutation controls", () => {
    const safe = safeActionableControls(inspection([
      control(1, "group"),
      control(null, "value"),
      control(7, "value"),
      control(7, "script", "Duplicate"),
      { ...control(8, "value", "Inconsistent legacy group"), group_header: true },
      control(9, "dropdown"),
    ], [7]));
    expect(safe.map((item) => item.id)).toEqual([9]);
  });

  it("excludes a control whose enclosing script is ambiguous in the exact table", () => {
    // A record inside a script does not exist until that script has run, and a
    // script two MemoryRecords share the ID of cannot be named by any command.
    // Offering the unique child means offering something that can only wait for
    // a parent Cheat Engine will never create.
    const parent = control(4, "script", "Master script");
    parent.path = ["Master script"];
    const twin = control(4, "script", "Master script twin");
    twin.path = ["Elsewhere"];
    const child = control(5, "value", "Inside");
    child.path = ["Master script", "Inside"];
    const sibling = control(6, "value", "Outside");
    sibling.path = ["Outside"];

    const safe = safeActionableControls(inspection([parent, twin, child, sibling], [4]));
    expect(safe.map((item) => item.id)).toEqual([6]);
  });

  it("keeps search optional and stable while pinned controls sort first", () => {
    const controls = [control(1, "value", "Health"), control(2, "script", "Infinite ammo"), control(3, "dropdown", "Difficulty")];
    expect(filterControls(controls, "").map((item) => item.id)).toEqual([1, 2, 3]);
    expect(filterControls(controls, "AMMO").map((item) => item.id)).toEqual([2]);
    expect(sortPinnedControls(controls, [3, 1]).map((item) => item.id)).toEqual([1, 3, 2]);
  });

  it("exposes nested table groups as controller sections without hiding descendants", () => {
    const root = control(1, "group", "Root");
    root.path = ["Root"];
    const nested = control(2, "group", "Nested");
    nested.path = ["Root", "Nested"];
    const rootValue = control(3, "value", "Root value");
    rootValue.path = ["Root", "Root value"];
    const nestedValue = control(4, "value", "Nested value");
    nestedValue.path = ["Root", "Nested", "Nested value"];
    const outside = control(5, "value", "Outside");
    outside.path = ["Outside"];
    const table = inspection([root, nested, rootValue, nestedValue, outside]);
    const safe = safeActionableControls(table);
    const sections = controlSections(table, safe);

    expect(sections.map((section) => section.label)).toEqual([
      "All supported controls",
      "Pinned controls",
      "Root",
      // Root is the option directly above, so this one says the level it adds
      // rather than spelling its parent out again.
      "Nested",
    ]);
    expect(controlsForSection(safe, sections[2], []).map((item) => item.id)).toEqual([3, 4]);
    expect(controlsForSection(safe, sections[3], []).map((item) => item.id)).toEqual([4]);
    expect(controlsForSection(safe, sections[1], [4, 5]).map((item) => item.id)).toEqual([4, 5]);
  });


  it("stops the section picker repeating what every section has in common", () => {
    // The real paths of one table on this device. Its author nests every group
    // under two instruction steps, so every option in the picker opened with
    // the same 128 characters and the part that told them apart was at the end
    // of the third line; nine options filled the screen.
    const paths: string[][] = [
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Current Player Pointers - Values may take a few seconds to populate  [Credits: Tuuuup!]", "pa::ServerChildOnlyInGameActor  [READ ONLY - do not modify]"],
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Current Player Pointers - Values may take a few seconds to populate  [Credits: Tuuuup!]", "pa::ServerChildOnlyInGameActor  [READ ONLY - do not modify]", "Player Stat Block  [Base: cplayer+68 -> chain below]"],
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Items Don't Decrease & Inventory Scripts  [ONLY 1 ACTIVE AT A TIME]"],
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Items Don't Decrease & Inventory Scripts  [ONLY 1 ACTIVE AT A TIME]", "Items Don't Decrease  (2 Versions) Updated with AOB from Cielos's table and updated the opcodes"],
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Items Don't Decrease & Inventory Scripts  [ONLY 1 ACTIVE AT A TIME]", "Advanced Inventory Scripts  [Credits: Austin / MPElite]"],
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Max Trust  (People / Pets / Horses)"],
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Durability  [ONLY 1 ACTIVE AT A TIME]"],
    ["[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world", "[STEP 2] Enable / Initialize Table (Enable this SECOND)", "Character Body/Head Scale (For Fun!!) [Credits: bobdandy / MPElite]"]
    ];

    expect(sharedSectionDepth(paths)).toBe(2);
    const labels = paths.map((path) => sectionLabel(path, sharedSectionDepth(paths)));

    expect(labels).toContain("Max Trust (People / Pets / Horses)");
    expect(labels).toContain("Durability [ONLY 1 ACTIVE AT A TIME]");
    // The leaf is what tells one section from another, so it is what survives.
    // Nothing marks what was dropped in front of it: those are groups the
    // picker does not offer, so there is nowhere for that mark to point.
    expect(labels[0]).toBe("pa::ServerChildOnlyInGameActor [READ ONLY - do not modify]");
    expect(labels[1]).toBe("Player Stat Block [Base: cplayer+68 -> chain below]");
    expect(labels.every((label) => label.length <= MAX_SECTION_LABEL)).toBe(true);
    // And they still name different things.
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("stops a section spelling out a parent the picker lists above it", () => {
    // The same repetition one branch down, and what was left of the cropping
    // after the shared prefix went: on this device three of the eight options
    // of one table were cut because each carried a group that was an option two
    // rows up. A CT holds a group's descendants directly under it, so that
    // parent is always immediately above.
    const step1 = "[STEP 1] Auto Attach Process - Wait until FULLY loaded into game world";
    const step2 = "[STEP 2] Enable / Initialize Table (Enable this SECOND)";
    const inventory = "Items Don't Decrease & Inventory Scripts  [ONLY 1 ACTIVE AT A TIME]";
    const paths = [
      [step1, step2, inventory],
      [step1, step2, inventory, "Advanced Inventory Scripts  [Credits: Austin / MPElite]"],
      [step1, step2, "Durability  [ONLY 1 ACTIVE AT A TIME]"],
    ];
    const controls = paths.flatMap((path, index) => {
      const group = control(index * 2 + 1, "group", path[path.length - 1]);
      group.path = path;
      const leaf = control(index * 2 + 2, "value", `Value ${index}`);
      leaf.path = [...path, `Value ${index}`];
      return [group, leaf];
    });
    const table = inspection(controls);
    const labels = controlSections(table, safeActionableControls(table)).map((section) => section.label);

    expect(labels.slice(2)).toEqual([
      // The author's own double spaces go with it: on a proportional list they
      // are a gap in the middle of a name, and they cost width that decides
      // whether the name has to be cut at all.
      "Items Don't Decrease & Inventory Scripts [ONLY 1 ACTIVE AT A TIME]",
      "Advanced Inventory Scripts [Credits: Austin / MPElite]",
      "Durability [ONLY 1 ACTIVE AT A TIME]",
    ]);
    expect(labels.some((label) => label.includes("\u2026"))).toBe(false);
  });

  it("keeps two options that would read the same apart", () => {
    // A picker is the one place identical rows cannot be lived with: the row
    // says nothing about which of the two it is. Each is given back one more
    // group of its own path until they differ.
    const paths = [
      ["Table", "Client", "Filter"],
      ["Table", "Server", "Filter"],
    ];
    const controls = paths.flatMap((path, index) => {
      const group = control(index * 2 + 1, "group", "Filter");
      group.path = path;
      const leaf = control(index * 2 + 2, "value", `Value ${index}`);
      leaf.path = [...path, `Value ${index}`];
      return [group, leaf];
    });
    const table = inspection(controls);
    const labels = controlSections(table, safeActionableControls(table)).map((section) => section.label);

    expect(labels.slice(2)).toEqual(["Client \u203a Filter", "Server \u203a Filter"]);
  });

  it("cuts a name too long for the list at its end, so the window keeps its shape", () => {
    // A name past the width of the list wraps and pushes every option below it
    // down. It is cut where a reader stops caring instead.
    const huge = "x".repeat(MAX_SECTION_LABEL * 2);
    const label = sectionLabel(["Group", huge], 1);
    expect(label.length).toBe(MAX_SECTION_LABEL);
    expect(label.endsWith("\u2026")).toBe(true);
    // And a path that does not fit only because of the groups in front of it
    // loses those, in whole groups and in silence, never its own name.
    expect(sectionLabel([`Group ${"y".repeat(MAX_SECTION_LABEL)}`, "Leaf"], 0)).toBe("Leaf");
  });

  it("drops the whole of a context path every row repeats", () => {
    // The picker's rule stops one short, because a section still has to name
    // itself there. A path shown beside a name it does not provide has no such
    // floor: on one real table every code row spent its single truncated line
    // on the same 128 characters and cut off the size and the line count.
    const step1 = "[STEP 1] Auto Attach Process";
    const step2 = "[STEP 2] Enable / Initialize Table";
    expect(sharedContextDepth([[step1, step2, "Health"], [step1, step2, "Stamina"]])).toBe(2);
    // A row with no context of its own repeats nothing, so it does not make
    // the answer zero for every row that does.
    expect(sharedContextDepth([[], [step1], [step1, step2]])).toBe(1);
    // And nothing shared is nothing dropped.
    expect(sharedContextDepth([["Player"], ["World"]])).toBe(0);
    expect(sharedContextDepth([["Player"]])).toBe(0);
  });

  it("never leaves a section with nothing to call itself", () => {
    // Nothing is repeated when there is only one of them, so nothing is
    // dropped: one real table has a single group and was losing the group it
    // sits under for no gain at all.
    expect(sharedSectionDepth([["Only group"]])).toBe(0);
    expect(sharedSectionDepth([["Setup", "Cheats"]])).toBe(0);
    expect(sectionLabel(["Setup", "Cheats"], 0)).toBe("Setup \u203a Cheats");
    // And a single group keeps the whole of its own name.
    expect(sectionLabel(["Only group"], 0)).toBe("Only group");
  });

  it("remembers a switch's key with its toggle, because the toggle is the value", () => {
    // Remembering `active` alone replays the activation next session with
    // nothing written, and a flag frozen at whatever the game holds is a cheat
    // that reports itself on and is off. The user never typed a value here:
    // they pressed the switch, which is the only control such a record has.
    const flag = {
      id: 4, description: "bEnableGodMode", path: ["bEnableGodMode"], variable_type: "4 Bytes",
      kind: "dropdown" as const, group_header: false, has_assembler_script: false,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]] as [string, string][],
      dropdown_read_only: false, switch_on_value: "1",
    };
    expect(rememberedSelection([flag], [{ record_id: 4, active: true, value: "1" }], [], new Set([4]), new Set()))
      .toEqual([{ record_id: 4, active: true, value: "1" }]);
    // And switched off it remembers the off key, not the on one it had.
    expect(rememberedSelection(
      [flag],
      [{ record_id: 4, active: false, value: "0" }],
      [{ record_id: 4, active: true, value: "1" }],
      new Set([4]),
      new Set(),
    )).toEqual([{ record_id: 4, active: false, value: "0" }]);
    // Switched off along with the script that created it, so it has no state
    // row at all. Keeping the on value it used to hold would write that value
    // into the game next session and then release it, which is the cheat on.
    expect(rememberedSelection(
      [flag],
      [],
      [{ record_id: 4, active: true, value: "1" }],
      new Set([4]),
      new Set(),
    )).toEqual([{ record_id: 4, active: false, value: "0" }]);
  });

  it("remembers only explicit CE Decky selections and preflights persistence before runtime mutation", () => {
    const controls = [control(1, "script"), control(2, "value"), control(3, "value")];
    const states = [
      { record_id: 1, active: true, value: null },
      { record_id: 2, active: true, value: "200" },
      { record_id: 3, active: true, value: "300" },
    ];
    const remembered = rememberedSelection(controls, states, [
      { record_id: 2, active: true, value: null },
    ], new Set([1]), new Set([3]));
    expect(remembered).toEqual([
      { record_id: 1, active: true, value: null },
      { record_id: 2, active: true, value: null },
      { record_id: 3, active: null, value: "300" },
    ]);
    expect(rememberedSelectionBudgetError([], remembered)).toBeNull();
    expect(rememberedSelection(
      controls,
      [{ record_id: 1, active: false, value: null }, { record_id: 2, active: false, value: "999" }, { record_id: 3, active: true, value: "300" }],
      [{ record_id: 2, active: true, value: "200" }],
      new Set(),
      new Set(),
    )).toEqual([{ record_id: 2, active: true, value: "200" }]);

    const tooMany = Array.from({ length: 1025 }, (_, index) => ({ record_id: index, active: true, value: null }));
    expect(rememberedSelectionBudgetError([], tooMany)).toMatch(/1025 controls/);
  });

  it("overlays remembered fields without erasing untouched startup fields", () => {
    expect(effectiveStartupPreferences(
      [{ record_id: 5, active: true, value: "10" }],
      [{ record_id: 5, active: null, value: "20" }, { record_id: 7, active: false, value: null }],
    )).toEqual([
      { record_id: 5, active: true, value: "20" },
      { record_id: 7, active: false, value: null },
    ]);
  });

  it("warns about configured child startup whose actionable parent is not explicitly enabled", () => {
    const parent = control(10, "script", "Parent script");
    parent.path = ["Parent script"];
    const child = control(11, "value", "Child value");
    child.path = ["Parent script", "Child value"];
    const controls = [parent, child];

    expect(startupParentWarnings(controls, [{ record_id: 11, active: true, value: null }])).toEqual([
      { childId: 11, parentId: 10 },
    ]);
    expect(startupParentWarnings(controls, [
      { record_id: 10, active: true, value: null },
      { record_id: 11, active: true, value: null },
    ])).toEqual([]);
  });

  it("keeps retry-attach choices deterministic across process-enumeration order and case", () => {
    expect(isValidProcessBasename("game.exe")).toBe(true);
    expect(isValidProcessBasename("żółć.exe")).toBe(true);
    expect(isValidProcessBasename(`${"界".repeat(255)}.exe`)).toBe(true);
    expect(isValidProcessBasename("game")).toBe(false);
    expect(isValidProcessBasename("folder/game.exe")).toBe(false);
    expect(isValidProcessBasename("ga\u202eme.exe")).toBe(false);
    expect(isValidProcessBasename("ga\u200bme.exe")).toBe(false);
    expect(isValidProcessBasename("game\u0301.exe")).toBe(false);

    const expected = [
      { name: "game.exe", pids: [11, 22], runtimeNoise: false },
      { name: "other.exe", pids: [40], runtimeNoise: false },
    ];
    expect(runtimeAttachCandidates([
      [40, "other.exe"],
      [11, "GAME.EXE"],
      [30, "helper"],
      [31, "ga\u202eme.exe"],
      [22, "game.exe"],
    ])).toEqual(expected);
    expect(runtimeAttachCandidates([
      [22, "game.exe"],
      [30, "helper"],
      [40, "other.exe"],
      [11, "GAME.EXE"],
    ])).toEqual(expected);
  });

  it("uses the newest runtime result for a MemoryRecord", () => {
    const results = [
      { generation: 1, record_id: 2, ok: true, active: false, value: "1", error: null },
      { generation: 2, record_id: 2, ok: true, active: true, value: "2", error: null },
    ];
    expect(latestRuntimeResult(results, 2)?.generation).toBe(2);
    expect(latestRuntimeResult(results, 99)).toBeNull();
  });
});


describe("running-game launcher classification", () => {
  it("identifies store launchers by executable name without guessing from a parent path", () => {
    expect(isKnownLauncherExecutable("C:\\Program Files\\Epic Games\\Launcher\\Portal\\Binaries\\Win64\\EpicGamesLauncher.exe")).toBe(true);
    expect(isKnownLauncherExecutable("/home/deck/Games/GOG Galaxy/GalaxyClient.exe")).toBe(true);
    // Steam keeps the surrounding quotes on a shortcut target.
    expect(isKnownLauncherExecutable('"/opt/launchers/Battle.net.exe"')).toBe(true);
    expect(isKnownLauncherExecutable("/home/deck/Games/Ubisoft Game Launcher/anything.exe")).toBe(false);
    expect(isKnownLauncherExecutable("C:\\Program Files (x86)\\GOG Galaxy\\Games\\Cyberpunk 2077\\bin\\x64\\Cyberpunk2077.exe")).toBe(false);
    expect(isKnownLauncherExecutable("/home/deck/Games/Lumen.Hollow/Voyage12_Steam.exe")).toBe(false);
    expect(isKnownLauncherExecutable(null)).toBe(false);
    expect(isKnownLauncherExecutable("")).toBe(false);
  });

  it("drops launchers from an ambiguous observation but never empties it", () => {
    const candidates = [
      { appId: 1, exe: "/games/Epic Games/Launcher/EpicGamesLauncher.exe" },
      { appId: 2, exe: "/games/Voyage12_Steam.exe" },
    ];
    expect(withoutKnownLaunchers(candidates, (candidate) => candidate.exe).map((candidate) => candidate.appId)).toEqual([2]);

    // Every candidate looking like a launcher is not a reason to report that
    // nothing is running; the manual choice must stay available instead.
    const launchersOnly = [
      { appId: 1, exe: "/games/Epic Games/Launcher/EpicGamesLauncher.exe" },
      { appId: 3, exe: "/games/GOG Galaxy/GalaxyClient.exe" },
    ];
    expect(withoutKnownLaunchers(launchersOnly, (candidate) => candidate.exe)).toHaveLength(2);

    // A Steam library app exposes no shortcut executable and is never dropped.
    const steamApps = [{ appId: 4, exe: null }, { appId: 5, exe: null }];
    expect(withoutKnownLaunchers(steamApps, (candidate) => candidate.exe)).toHaveLength(2);
  });
});


describe("Wine runtime process classification", () => {
  it("keeps the game executables and drops the prefix's own processes", () => {
    // The exact observation for Lumen Hollow: Voyage 12 on the target.
    expect(withoutWineRuntimeProcesses([
      "crashpad_handler.exe", "Voyage12_Steam.exe", "explorer.exe", "plugplay.exe",
      "rpcss.exe", "LumenHollow-Win64-Shipping.exe", "services.exe", "steam.exe",
      "svchost.exe", "tabtip.exe", "winedevice.exe", "xalia.exe",
    ])).toEqual(["Voyage12_Steam.exe", "LumenHollow-Win64-Shipping.exe"]);
  });

  it("returns nothing when every observed process is runtime noise", () => {
    // Review opened during startup, before the game's own binary appeared. The
    // old fallback handed this list back unfiltered, so defaultTargetProcess
    // picked a helper, Use this table persisted it, and later sessions attached
    // the table to that helper.
    expect(withoutWineRuntimeProcesses(["explorer.exe", "services.exe"])).toEqual([]);
    expect(withoutWineRuntimeProcesses(["xalia.exe", "crashpad_handler.exe"])).toEqual([]);
    expect(withoutWineRuntimeProcesses([])).toEqual([]);
  });

  it("leaves the target unresolved until the real game process appears", () => {
    const startup = ["xalia.exe", "crashpad_handler.exe"];
    expect(defaultTargetProcess({ observed: withoutWineRuntimeProcesses(startup) })).toBe("");

    const settled = [...startup, "LumenHollow-Win64-Shipping.exe"];
    expect(defaultTargetProcess({ observed: withoutWineRuntimeProcesses(settled) }))
      .toBe("LumenHollow-Win64-Shipping.exe");
  });

  it("classifies only by exact basename, never by a parent path", () => {
    expect(isWineRuntimeExecutable("Z:\\games\\explorer\\MyGame.exe")).toBe(false);
    expect(isWineRuntimeExecutable("C:\\windows\\system32\\explorer.exe")).toBe(true);
  });
});


describe("default target process", () => {
  const observed = ["Voyage12_Steam.exe", "LumenHollow-Win64-Shipping.exe"];
  const launcher = "\"/home/deck/Games/Lumen.Hollow.Voyage.12/Voyage12_Steam.exe\"";

  it("keeps a confirmed process that is not the observed launcher", () => {
    expect(defaultTargetProcess({
      confirmed: "Chosen.exe", tableHints: ["Other.exe"], observed, launchExecutable: launcher,
    })).toBe("Chosen.exe");
  });

  it("repairs a saved launcher default when another game process is now observed", () => {
    expect(defaultTargetProcess({
      confirmed: "Voyage12_Steam.exe", tableHints: [], observed, launchExecutable: launcher,
    })).toBe("LumenHollow-Win64-Shipping.exe");
  });

  it("keeps a confirmed launcher when it is the only running candidate", () => {
    expect(defaultTargetProcess({
      confirmed: "Voyage12_Steam.exe",
      observed: ["Voyage12_Steam.exe"],
      launchExecutable: launcher,
    })).toBe("Voyage12_Steam.exe");
  });

  it("prefers the table's own name in the spelling the game is running", () => {
    // The exact reviewed table writes `Lumenhollow-Win64-Shipping.exe`; the process
    // is `LumenHollow-Win64-Shipping.exe`, and Cheat Engine matches either way.
    expect(defaultTargetProcess({
      tableHints: ["Lumenhollow-Win64-Shipping.exe"], observed, launchExecutable: launcher,
    })).toBe("LumenHollow-Win64-Shipping.exe");
  });

  it("trusts the running game over a hint for a different build", () => {
    // Two tables in the same topic name the Epic build's executable.
    expect(defaultTargetProcess({
      tableHints: ["LumenHollowEos-Win64-Shipping.exe"], observed, launchExecutable: launcher,
    })).toBe("LumenHollow-Win64-Shipping.exe");
  });

  it("skips the game's own launch executable when nothing names a process", () => {
    expect(defaultTargetProcess({ observed, launchExecutable: launcher }))
      .toBe("LumenHollow-Win64-Shipping.exe");
  });

  it("uses the table's hint when the game is not running", () => {
    expect(defaultTargetProcess({ tableHints: ["Lore.exe"], observed: [] })).toBe("Lore.exe");
  });

  it("selects the launcher only when it is the single observed process", () => {
    // The launch executable is an inverse signal, not a veto: for plenty of
    // games it is the game. Refusing it as the sole candidate would leave every
    // single-process game to be typed in by hand.
    expect(defaultTargetProcess({ observed: ["Voyage12_Steam.exe"], launchExecutable: launcher }))
      .toBe("Voyage12_Steam.exe");
  });

  it("leaves manual entry when nothing at all is known", () => {
    expect(defaultTargetProcess({ tableHints: [], observed: [] })).toBe("");
    expect(defaultTargetProcess({ tableHints: ["not a process"], observed: ["also/invalid"] })).toBe("");
  });
});


describe("a target process chosen before the game has ever run", () => {
  const listing = (names: Array<[string, number, string]>) => ({
    schema: 1, app_id: 220, install_dir: "/games/Half-Life 2", truncated: false, reason: null,
    executables: names.map(([name, depth, directory]) => ({ name, depth, directory, size_bytes: 1 })),
  });

  it("ranks the game's own files by depth, which is the only honest signal", () => {
    // Half-Life 2's root holds exactly `hl2.exe` and its `bin/` holds thirty
    // odd SDK compilers. Every one of those reads like a plausible program, so
    // nothing about the names separates them and the position does.
    const found = installedGameExecutables(listing([
      ["vrad.exe", 1, "bin"],
      ["hl2.exe", 0, ""],
      ["hammer.exe", 1, "bin"],
    ]));
    expect(found.map((item) => item.name)).toEqual(["hl2.exe", "hammer.exe", "vrad.exe"]);
  });

  it("drops what ships in a game folder and never owns its memory", () => {
    // These never appear in a running game's process table, which is why the
    // live lists never needed them: a crash handler runs after the game stops
    // and a redistributable runs once at install. Walking the folder is what
    // puts them in front of a reader, and a Unity title's sits in the root
    // beside the game's own binary, which is the position that decides a
    // default.
    const found = installedGameExecutables(listing([
      ["Game.exe", 0, ""],
      ["UnityCrashHandler64.exe", 0, ""],
      ["vcredist_x64.exe", 1, "_CommonRedist"],
      ["explorer.exe", 1, "tools"],
    ]));
    expect(found.map((item) => item.name)).toEqual(["Game.exe"]);
  });

  it("preselects only the one root executable, never a choice between two", () => {
    expect(soleInstalledExecutable(listing([["hl2.exe", 0, ""], ["vrad.exe", 1, "bin"]]))).toBe("hl2.exe");
    // Two at the root is a question, and the reader answers it.
    expect(soleInstalledExecutable(listing([["Game.exe", 0, ""], ["Launcher.exe", 0, ""]]))).toBeNull();
    // And nothing at the root is nothing: a binary two directories down is
    // offered in the picker and is not a default.
    expect(soleInstalledExecutable(listing([["Game-Win64-Shipping.exe", 2, "Binaries/Win64"]]))).toBeNull();
    expect(soleInstalledExecutable(null)).toBeNull();
  });

  it("offers only the library entries this device actually holds", () => {
    // Measured on a Steam Deck beside a Steam Machine: Steam offered 39
    // non-Steam shortcuts while the Deck's own store held 6, and 9 of its 22
    // installed apps were Proton builds and Steam Linux Runtimes.
    const games = [
      { appId: 220, name: "Half-Life 2", sortAs: "", isShortcut: false },
      { appId: 1151340, name: "Fallout 76", sortAs: "", isShortcut: false },
      { appId: 3658110, name: "Proton 10.0", sortAs: "", isShortcut: false },
      { appId: 3407131886, name: "Halo Campaign Evolved", sortAs: "", isShortcut: true },
      { appId: 4093505389, name: "Neon Bazaar", sortAs: "", isShortcut: true },
    ] as any;
    const library = {
      schema: 1,
      steam_app_ids: [220, 3658110],
      unstartable_app_ids: [3658110],
      shortcut_app_ids: [3407131886],
      reason: null,
      shortcuts_reason: null,
    } as any;
    expect(gamesOnThisDevice(games, library).map((game) => game.appId))
      .toEqual([220, 3407131886]);

    // An unreadable answer is not evidence that a game is missing, and hiding
    // the user's own library is the worse mistake of the two.
    expect(gamesOnThisDevice(games, { ...library, reason: "could not read" }).map((game) => game.appId))
      .toEqual([220, 1151340, 3658110, 3407131886]);
    expect(gamesOnThisDevice(games, { ...library, shortcuts_reason: "no store" }).map((game) => game.appId))
      .toEqual([220, 3407131886, 4093505389]);
    expect(gamesOnThisDevice(games, null)).toEqual(games);
  });

  it("takes what Steam starts over anything read off the disk", () => {
    // The whole of the change: Half-Life 2's folder holds twenty-eight Windows
    // executables and Steam's own record holds one line. The client cannot
    // start a game without knowing what to start, so this is not a guess about
    // names or depths, it is the same record the client uses.
    const declared = {
      schema: 2, app_id: 220, install_dir: "/games/Half-Life 2", truncated: false,
      source: "steam", declared_reason: null, reason: null,
      executables: [
        { name: "cs2.exe", directory: "game/bin/win64", depth: 3, size_bytes: 1, declared: true, description: null },
        { name: "csgo_legacy_app.exe", directory: "game/csgo/bin/legacy", depth: 4, size_bytes: 1, declared: true, description: "Legacy" },
      ],
    } as any;
    // Steam's own order, not the shallowest first: three directories down says
    // nothing against the program the client runs.
    expect(installedGameExecutables(declared).map((item) => item.name))
      .toEqual(["cs2.exe", "csgo_legacy_app.exe"]);
    expect(declaredLaunchExecutable(declared)).toBe("cs2.exe");
    expect(defaultTargetProcess({ installed: declared })).toBe("cs2.exe");
    // A game Steam starts through a store client of its own keeps it. That
    // name is dropped from a walk of a folder, where it is a client that
    // happens to ship beside the game; here it is the answer to the question,
    // and dropping it left the screen with no candidates at all.
    const throughLauncher = {
      ...declared,
      executables: [{ name: "upc.exe", directory: "", depth: 0, size_bytes: 1, declared: true, description: null }],
    } as any;
    expect(declaredLaunchExecutable(throughLauncher)).toBe("upc.exe");
    // A list the folder walk produced declares nothing, and the strict
    // one-root-executable rule is what still decides a default there.
    expect(declaredLaunchExecutable(listing([["hl2.exe", 0, ""]]))).toBeNull();
    expect(declaredLaunchExecutable(null)).toBeNull();
    // And every observation still outranks it, because a game that is running
    // has answered the question this was asked in place of.
    expect(defaultTargetProcess({ observed: ["running.exe"], installed: declared })).toBe("running.exe");
    expect(defaultTargetProcess({ tableHints: ["table.exe"], installed: declared })).toBe("table.exe");
  });

  it("uses the game's files only when nothing else says anything", () => {
    const installed = listing([["hl2.exe", 0, ""]]);
    // The case this exists for: a table that names nothing, for a game that is
    // not running. Before this the screen had no candidate and its one press
    // could not be made until the game had been started once.
    expect(defaultTargetProcess({ installed })).toBe("hl2.exe");
    // And every stronger signal outranks it.
    expect(defaultTargetProcess({ tableHints: ["table.exe"], installed })).toBe("table.exe");
    expect(defaultTargetProcess({ observed: ["running.exe"], installed })).toBe("running.exe");
    expect(defaultTargetProcess({ confirmed: "saved.exe", installed })).toBe("saved.exe");
  });

  it("says what the game did start when the saved process is not among it", () => {
    const observation = (over: Record<string, unknown>) => ({
      app_id: 10, running: true, pids: [1], windows_executables: ["Game.exe", "explorer.exe"], ...over,
    }) as any;
    expect(absentLiveTarget(observation({}), "hl2.exe")).toEqual(["Game.exe"]);
    // Nothing to say where the name is right, or where the absence cannot be
    // proved: a game that is not running started nothing, an observation with
    // no Windows process at all is a prefix that has not got going, and a list
    // that reached the backend's own cap of 32 may have been cut.
    expect(absentLiveTarget(observation({}), "game.exe")).toBeNull();
    expect(absentLiveTarget(observation({ running: false }), "hl2.exe")).toBeNull();
    expect(absentLiveTarget(observation({ windows_executables: [] }), "hl2.exe")).toBeNull();
    expect(absentLiveTarget(observation({
      windows_executables: Array.from({ length: 32 }, (_, index) => `p${index}.exe`),
    }), "hl2.exe")).toBeNull();
    expect(absentLiveTarget(null, "hl2.exe")).toBeNull();
    expect(absentLiveTarget(observation({}), null)).toBeNull();
  });

  it("says nothing when all the game has running is Wine's own machinery", () => {
    // The state a prefix mid-startup is in, and the state a game that has
    // EXITED is in, because `running` stays true while wineserver, the Proton
    // chain and Steam's reaper still report the AppID. Handing the unfiltered
    // list back offered `explorer.exe` as a one-press repair for a game that
    // was not running at all, and the press overwrites a working target.
    const leftovers = {
      app_id: 10, running: true, pids: [1],
      windows_executables: ["explorer.exe", "services.exe", "winedevice.exe"],
    } as any;
    expect(absentLiveTarget(leftovers, "Game-Win64-Shipping.exe")).toBeNull();
  });

  it("counts the collection cap before the validity filter, not after", () => {
    // The cap is about what the backend collected. Filtering first let a full
    // list of 32 lose one malformed name and pass the guard as 31, which is the
    // panel asserting an absence from a list it knows may have been cut.
    const full = Array.from({ length: 31 }, (_, index) => `p${index}.exe`);
    const cut = {
      app_id: 10, running: true, pids: [1],
      windows_executables: [...full, "not\u0301nfc.exe"],
    } as any;
    expect(absentLiveTarget(cut, "Game.exe")).toBeNull();
  });
});

describe("second observed game on the target", () => {
  // Cobalt Ascent, observed live beside the first reviewed game: the same
  // launcher-then-engine shape, a different engine binary name, and Proton's
  // accessibility helper running beside both.
  const observed = ["CobaltAscent-Win64-Shipping.exe", "CobaltAscent.exe", "xalia.exe"];

  it("keeps only the game's own executables", () => {
    expect(withoutWineRuntimeProcesses(observed))
      .toEqual(["CobaltAscent-Win64-Shipping.exe", "CobaltAscent.exe"]);
  });

  it("defaults to the engine binary rather than the observed launch executable", () => {
    expect(defaultTargetProcess({
      observed: withoutWineRuntimeProcesses(observed),
      launchExecutable: "CobaltAscent.exe",
    })).toBe("CobaltAscent-Win64-Shipping.exe");
  });
});


describe("compact cheat rows", () => {
  const base = {
    id: 7, description: "Outgoing Damage %", variable_type: "Float", kind: "value" as const,
    group_header: false, has_assembler_script: false, dropdown_values: [] as [string, string][],
    dropdown_read_only: false,
  };

  it("labels a row with its leaf name and keeps the group as context", () => {
    const control = { ...base, path: ["Enable 1.0", "Outgoing Damage Scaling?", "Outgoing Damage %"] };
    expect(controlRowLabel(control)).toBe("Outgoing Damage %");
    expect(controlRowContext(control)).toBe("Enable 1.0 \u203a Outgoing Damage Scaling?");
  });

  it("falls back to the description and never renders an empty row label", () => {
    expect(controlRowLabel({ ...base, path: ["  "] })).toBe("Outgoing Damage %");
    expect(controlRowLabel({ ...base, path: [], description: "" })).toBe("Record 7");
    expect(controlRowContext({ ...base, path: ["Only"] })).toBe("");
  });

  it("offers typed value editing only where the exact table supports it", () => {
    expect(controlAcceptsTypedValue({ ...base, path: ["v"] })).toBe(true);
    expect(controlAcceptsTypedValue({ ...base, path: ["s"], kind: "script" })).toBe(false);
    expect(controlAcceptsTypedValue({ ...base, path: ["d"], kind: "dropdown" })).toBe(true);
    expect(controlAcceptsTypedValue({ ...base, path: ["d"], kind: "dropdown", dropdown_read_only: true })).toBe(false);
  });

  it("draws a two-entry on/off list as a switch and nothing else", () => {
    const flag = {
      ...base, path: ["bEnableGodMode"], kind: "dropdown" as const,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]] as [string, string][],
      switch_on_value: "1",
    };
    expect(controlIsSwitch(flag)).toBe(true);
    // Neither control is drawn for it, and it is never held back for a missing
    // value: the toggle writes one key or the other, so it always has one.
    expect(controlAcceptsTypedValue(flag)).toBe(false);
    expect(controlNeedsValueInput(flag)).toBe(false);
    expect(switchValueFor(flag, true)).toBe("1");
    expect(switchValueFor(flag, false)).toBe("0");
    expect(switchValueFor(flag, null)).toBe(null);
    expect(switchValuesFor(flag)).toEqual({ on: "1", off: "0" });
    expect([...switchOffValues([flag])]).toEqual([[7, "0"]]);
  });

  it("writes the key the author gave each side, whatever its number", () => {
    const reversed = {
      ...base, path: ["Immortal"], kind: "dropdown" as const,
      dropdown_values: [["1040", "Yes"], ["2400", "No"]] as [string, string][],
      switch_on_value: "1040",
    };
    expect(switchValueFor(reversed, true)).toBe("1040");
    expect(switchValueFor(reversed, false)).toBe("2400");
  });

  it("keeps the list for anything the backend did not call a switch", () => {
    const choice = {
      ...base, path: ["Body"], kind: "dropdown" as const,
      dropdown_values: [["0", "Male"], ["1", "Female"]] as [string, string][],
      switch_on_value: null,
    };
    expect(controlIsSwitch(choice)).toBe(false);
    expect(controlNeedsValueInput(choice)).toBe(true);
    // A backend that predates the field sends none at all, and reading that as
    // a switch would take the list away from every two-entry record.
    const { switch_on_value: _unused, ...older } = choice;
    expect(controlIsSwitch(older as typeof choice)).toBe(false);
  });

  it("names the switches a script would turn on by itself, and nothing else", () => {
    const script = { ...base, id: 10, path: ["Enable"], kind: "script" as const, has_assembler_script: true };
    const flag = (id: number, name: string) => ({
      ...base, id, path: ["Enable", name], kind: "dropdown" as const,
      dropdown_values: [["0", "Off"], ["1", "On"]] as [string, string][], switch_on_value: "1",
      declared_default: "1", switch_off_is_safe: true,
    });
    const chosen = flag(11, "God mode");
    const unasked = flag(12, "One hit kill");
    const multiplier = { ...base, id: 13, path: ["Enable", "Damage"], kind: "value" as const };
    const elsewhere = flag(14, "Other");
    const held = switchesToHoldOff([script], [script, chosen, unasked, multiplier, { ...elsewhere, path: ["Other", "Other"] }], new Set([11]));
    // The one the user asked for is left alone, a value record keeps its own
    // number, and a switch under a different script is not this script's doing.
    expect(held.map((item) => [item.control.id, item.value])).toEqual([[12, "0"]]);
    // A flag the script leaves off, and one whose declaration could not be
    // read, are both the table behaving as it always did: nothing to hold off.
    const leftOff = { ...unasked, id: 15, declared_default: "0" };
    const unreadable = { ...unasked, id: 16, declared_default: null };
    expect(switchesToHoldOff([script], [script, leftOff, unreadable], new Set())).toEqual([]);
    // A script with nothing under it asks for nothing.
    expect(switchesToHoldOff([{ ...base, id: 20, path: ["Alone"], kind: "script" as const }], [script, chosen], new Set())).toEqual([]);
  });

  it("leaves on a cheat whose own code was not read as surviving being switched off", () => {
    // A table's hook can turn a pointer into an offset, test its own flag and,
    // on the branch taken when the flag is off, hand the game back the offset:
    // the game reads it as an address and dies, minutes later, with nothing to
    // say a cheat table was involved. So the write only happens where the
    // backend read that code and found it survives, and what stays on is named
    // rather than counted.
    const script = { ...base, id: 10, path: ["Enable"], kind: "script" as const, has_assembler_script: true };
    const flag = (id: number, name: string, safe: boolean) => ({
      ...base, id, path: ["Enable", name], kind: "dropdown" as const,
      dropdown_values: [["0", "Off"], ["1", "On"]] as [string, string][], switch_on_value: "1",
      declared_default: "1", switch_off_is_safe: safe,
    });
    const safe = flag(11, "One hit kill", true);
    const unsafe = flag(12, "Vitals drain", false);
    // A backend that does not answer the question at all holds nothing off.
    const { switch_off_is_safe: _unused, ...unanswered } = flag(13, "Ability cooldown", true);
    const controls = [script, safe, unsafe, unanswered as typeof safe];

    const held = switchesToHoldOff([script], controls, new Set());
    expect(held.map((item) => item.control.id)).toEqual([11]);

    const left = switchesLeftOn([script], controls, new Set());
    expect(left.map((control) => control.id)).toEqual([12, 13]);
    const said = leftOnSentence(left);
    expect(said).toContain("Vitals drain");
    expect(said).toContain("Ability cooldown");
    expect(said).toContain("2 more cheats from this table are on");
    // And where everything could be held off, there is nothing to say.
    expect(leftOnSentence(switchesLeftOn([script], [script, safe], new Set()))).toBeNull();
  });

  it("counts what a table switches on by itself, and says nothing when it does not", () => {
    const flag = (id: number, declared: string | null) => ({
      ...base, id, path: ["Enable", `Flag ${id}`], kind: "dropdown" as const,
      dropdown_values: [["0", "Off"], ["1", "On"]] as [string, string][],
      switch_on_value: "1", declared_default: declared,
    });
    const inspection = (controls: unknown[], ambiguous: number[] = []) =>
      ({ controls, ambiguous_record_ids: ambiguous } as any);
    expect(scriptDefaultsOn(inspection([flag(1, "1"), flag(2, "1"), flag(3, "0")])))
      .toEqual({ on: 2, switches: 3, unsafe: 2 });
    expect(scriptDefaultsOn(inspection([{ ...flag(1, "1"), switch_off_is_safe: true }, flag(2, "1")])))
      .toEqual({ on: 2, switches: 2, unsafe: 1 });
    // A table that switches nothing on by itself has no finding to report, and
    // neither has one whose declarations could not be read at all.
    expect(scriptDefaultsOn(inspection([flag(1, "0"), flag(2, null)]))).toBe(null);
    expect(scriptDefaultsOn(inspection([{ ...base, id: 9, path: ["Health"] }]))).toBe(null);
    expect(scriptDefaultsOn(null)).toBe(null);
    // A record the picker will not draw is not counted on the screen that
    // promises how many the picker holds.
    expect(scriptDefaultsOn(inspection([flag(1, "1"), flag(2, "1")], [2])))
      .toEqual({ on: 1, switches: 1, unsafe: 1 });
  });

  it("refuses another table only after a stop that could not prove the game clean", () => {
    expect(switchAfterStopRefusal({ stopped: false, cleanupConfirmed: null, unsettled: [] })).toBeNull();
    expect(switchAfterStopRefusal({ stopped: true, cleanupConfirmed: true, unsettled: [] })).toBeNull();
    expect(switchAfterStopRefusal({ stopped: true, cleanupConfirmed: false, unsettled: ["6"] }))
      .toContain("One cheat from the table that was running could not be switched off");
    expect(switchAfterStopRefusal({ stopped: true, cleanupConfirmed: false, unsettled: ["6", "7"] }))
      .toContain("2 cheats from the table that was running");
    expect(switchAfterStopRefusal({ stopped: true, cleanupConfirmed: false, unsettled: [] }))
      .toContain("could not confirm the cheats from the table that was running were switched off");
    // A stop that ended Cheat Engine with no verdict at all is not a clean one.
    expect(switchAfterStopRefusal({ stopped: true, cleanupConfirmed: null, unsettled: [] })).toContain("Restart the game");
  });

  it("promises only the chosen cheats where every default can be held off", () => {
    const said = (on: number, unsafe: number) => scriptDefaultsSentence({ on, switches: 24, unsafe });
    expect(said(22, 0)).toBe(
      "Of this table's 24 on/off cheats, 22 are switched on by the table itself. CE Decky turns on only the ones you choose.",
    );
    // A default CE Decky will not write off is left running, so the promise
    // would be false the moment its script starts.
    expect(said(22, 5)).not.toContain("only the ones you choose");
    expect(said(22, 5)).toContain("5 of them stay on whenever another cheat from the same script is on");
    expect(said(22, 5)).toContain("The rest are off unless you choose them.");
    expect(said(22, 1)).toContain("1 of them stays on");
    expect(said(22, 1)).toContain("switching it off");
    expect(said(2, 2)).toContain("They stay on");
    expect(said(2, 2)).not.toContain("The rest");
    expect(said(1, 1)).toContain("It stays on");
    expect(scriptDefaultsSentence(null)).toBeNull();
  });

  it("keeps one page inside the 800p Game Mode viewport", () => {
    expect(CONTROL_PAGE_SIZE).toBeLessThanOrEqual(6);
  });
});


describe("choosing from a value list a real table declares", () => {
  // One table on the development device declares 6508 items in each of three
  // pickers. The parser ceiling was raised to 16384 so those tables read at
  // all, and handing the whole list to one Decky dropdown then made them
  // unusable: a controller walks it one item at a time, and a read-only
  // dropdown is the one control that cannot be typed into instead.
  const items: [string, string][] = Array.from(
    { length: 6508 },
    (_, index) => [String(index + 1), `Item ${index + 1}`],
  );

  it("hands a dropdown a bounded number of options however long the list is", () => {
    const choices = matchingDropdownValues(items, "", null);
    expect(choices.options.length).toBeLessThanOrEqual(DROPDOWN_VALUE_LIMIT);
    expect(choices.shown).toBe(choices.options.length);
    expect(choices.total).toBe(6508);
    expect(choices.matched).toBe(6508);
    expect(items.length).toBeGreaterThan(DROPDOWN_SEARCH_THRESHOLD);
  });

  it("reaches a value deep in the list by typing instead of by scrolling to it", () => {
    const choices = matchingDropdownValues(items, "4211", null);
    expect(choices.options).toEqual([["4211", "Item 4211"]]);
    expect(choices.matched).toBe(1);
    // The number is what a table's own list gives a user to search on, and the
    // label carries it here; both are matched so either works.
    expect(matchingDropdownValues(items, "Item 6508", null).options).toEqual([["6508", "Item 6508"]]);
  });

  it("keeps the chosen value on offer even when the query does not match it", () => {
    // Otherwise the dropdown shows a selection it does not hold, and moving
    // through it would silently write the first option instead.
    const choices = matchingDropdownValues(items, "Item 12", "4211");
    expect(choices.options[0]).toEqual(["4211", "Item 4211"]);
    expect(choices.options.length).toBeLessThanOrEqual(DROPDOWN_VALUE_LIMIT);
    expect(choices.selectionKept).toBe(true);
  });

  it("counts the matches it is showing apart from the selection it kept", () => {
    // The kept selection takes a slot rather than an extra one, so it costs a
    // matching value. Counting the two together said "showing all 24 matching"
    // on a list that was showing 23 of them, and it is a list whose end the
    // user cannot see for themselves.
    const exactly = Array.from({ length: 24 }, (_, index) => [`m${index}`, `Match ${index}`] as [string, string]);
    const withSelection = [...exactly, ["other", "Something else"] as [string, string]];

    const atTheLimit = matchingDropdownValues(withSelection, "Match", "other", 24);
    expect(atTheLimit.options).toHaveLength(24);
    expect(atTheLimit.options[0]).toEqual(["other", "Something else"]);
    expect(atTheLimit.shown).toBe(23);
    expect(atTheLimit.matched).toBe(24);
    expect(describeValueChoices(atTheLimit)).toContain("Showing 23 of 24 matching");

    // Past the limit the same holds, and the description says so.
    const past = matchingDropdownValues(
      [...Array.from({ length: 40 }, (_, index) => [`m${index}`, `Match ${index}`] as [string, string]),
        ["other", "Something else"] as [string, string]],
      "Match", "other", 24,
    );
    expect(past.options).toHaveLength(24);
    expect(past.shown).toBe(23);
    expect(past.matched).toBe(40);
    expect(describeValueChoices(past)).toContain("Showing 23 of 40 matching");

    // And a selection that is itself a match takes nothing from the count.
    const matching = matchingDropdownValues(withSelection, "Match", "m3", 24);
    expect(matching.selectionKept).toBe(false);
    expect(matching.shown).toBe(24);
    expect(describeValueChoices(matching)).toContain("Showing all 24 matching");
  });

  it("says the kept value is kept when nothing matches at all", () => {
    // "Nothing matches" beside a value still on screen reads as a broken
    // filter. The value is there because it is what this record is set to.
    const choices = matchingDropdownValues(items, "nothing here", "4211");
    expect(choices.options).toEqual([["4211", "Item 4211"]]);
    expect(choices.shown).toBe(0);
    expect(choices.matched).toBe(0);
    expect(choices.selectionKept).toBe(true);
    expect(describeValueChoices(choices)).toBe(
      "Nothing matches. This record declares 6508 values. The value this record is set to is kept at the top.",
    );
  });

  it("offers only values the table itself declares", () => {
    const choices = matchingDropdownValues(items, "42", "not-in-the-table");
    const declared = new Set(items.map(([value]) => value));
    expect(choices.options.every(([value]) => declared.has(value))).toBe(true);
  });

  it("says nothing matches rather than offering an unrelated value", () => {
    const choices = matchingDropdownValues(items, "nothing here", null);
    expect(choices.options).toEqual([]);
    expect(choices.shown).toBe(0);
    expect(choices.matched).toBe(0);
    expect(choices.total).toBe(6508);
    expect(choices.selectionKept).toBe(false);
    expect(describeValueChoices(choices)).toBe("Nothing matches. This record declares 6508 values.");
  });
});


describe("pinned controls on the panel", () => {
  const control = (id: number, leaf: string) => ({
    id, description: leaf, path: ["Enable 1.0", leaf], variable_type: "4 Bytes", kind: "value" as const,
    group_header: false, has_assembler_script: false, dropdown_values: [] as [string, string][], dropdown_read_only: false,
  });
  const controls = [control(7, "Health"), control(8, "Ammo"), control(9, "Money")];

  it("promotes pinned controls in table order with their confirmed live state", () => {
    const rows = pinnedCheatRows(controls, [9, 7], [
      { generation: 1, record_id: 7, ok: true, active: true, value: "100", error: null },
      { generation: 1, record_id: 9, ok: true, active: false, value: null, error: null },
    ]);
    expect(rows.map((row) => row.recordId)).toEqual([7, 9]);
    expect(rows[0]).toEqual({ recordId: 7, label: "Health", summary: "Enable 1.0 \u00b7 = 100", active: true });
    expect(rows[1].summary).toBe("Enable 1.0");
  });

  it("never renders a guessed state for an unqueried or failed record", () => {
    expect(pinnedCheatRows(controls, [7], [])).toEqual([]);
    expect(pinnedCheatRows(controls, [7], [
      { generation: 1, record_id: 7, ok: false, active: null, value: null, error: "MemoryRecord missing" },
    ])).toEqual([]);
    expect(pinnedCheatRows(controls, [], [
      { generation: 1, record_id: 7, ok: true, active: true, value: null, error: null },
    ])).toEqual([]);
  });

  it("says on the row when a pinned cheat is one its own code may not switch off", () => {
    // This row writes the flag's off key the moment it is pressed, and the
    // picker's warning about it is two screens away.
    const flag = {
      ...control(11, "Vitals drain"),
      kind: "dropdown" as const,
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]] as [string, string][],
      dropdown_read_only: true,
      switch_on_value: "1",
      declared_default: "1",
      switch_off_is_safe: false,
    };
    const safe = { ...flag, id: 12, description: "Damage", path: ["Enable 1.0", "Damage"], switch_off_is_safe: true };
    const rows = pinnedCheatRows([flag, safe], [11, 12], [
      { generation: 1, record_id: 11, ok: true, active: true, value: "1", error: null },
      { generation: 1, record_id: 12, ok: true, active: true, value: "1", error: null },
    ]);

    expect(rows[0].summary).toBe("Enable 1.0 \u00b7 not safe to switch off");
    expect(rows[1].summary).toBe("Enable 1.0");
  });

  it("bounds the panel to one page of pinned rows", () => {
    const many = Array.from({ length: 20 }, (_, index) => control(100 + index, `Cheat ${index}`));
    const results = many.map((item) => ({ generation: 1, record_id: item.id, ok: true, active: false, value: null, error: null }));
    expect(pinnedCheatRows(many, many.map((item) => item.id), results)).toHaveLength(CONTROL_PAGE_SIZE);
  });
});


describe("value input a cheat cannot work without", () => {
  const base = {
    id: 7, description: "Damage", path: ["Damage"], variable_type: "Float", kind: "value" as const,
    group_header: false, has_assembler_script: false, dropdown_values: [] as [string, string][],
    dropdown_read_only: false,
  };

  it("marks every control whose effect depends on a written value", () => {
    expect(controlNeedsValueInput(base)).toBe(true);
    expect(controlNeedsValueInput({ ...base, kind: "script" })).toBe(false);
    expect(controlNeedsValueInput({ ...base, kind: "dropdown" })).toBe(true);
    // A read-only dropdown still needs a choice when it declares one.
    expect(controlNeedsValueInput({
      ...base, kind: "dropdown", dropdown_read_only: true, dropdown_values: [["1", "One"]],
    })).toBe(true);
    // A read-only dropdown declaring nothing cannot be given a value at all.
    expect(controlNeedsValueInput({ ...base, kind: "dropdown", dropdown_read_only: true })).toBe(false);
  });

  it("treats Cheat Engine's unreadable placeholder as no value", () => {
    expect(displayableControlValue("??")).toBe(null);
    expect(displayableControlValue("  ")).toBe(null);
    expect(displayableControlValue(null)).toBe(null);
    expect(displayableControlValue(undefined)).toBe(null);
    expect(displayableControlValue(" 100 ")).toBe("100");
    expect(displayableControlValue("0")).toBe("0");
  });

  it("keeps the placeholder out of a pinned row summary", () => {
    const rows = pinnedCheatRows([base], [7], [
      { generation: 1, record_id: 7, ok: true, active: true, value: "??", error: null },
    ]);
    expect(rows[0].summary).toBe("");
  });

  it("falls back through every stored choice the switch would send", () => {
    // The row, the refusal on the panel and the command that switches this on
    // read one resolution. A row showing nothing where the press would have
    // sent something, or the other way round, is a switch that does not mean
    // what the row above it says.
    const unread = [{ generation: 1, record_id: 7, ok: true, active: false, value: "??", error: null }];
    expect(pinnedCheatRows([base], [7], unread, [{ record_id: 7, active: true, value: "100" }])[0].summary)
      .toBe("= 100");
    expect(pinnedCheatRows([base], [7], unread, [], [{ record_id: 7, value: "250" }])[0].summary)
      .toBe("= 250");
    // And what is running outranks both, because it is what this record holds.
    const live = [{ generation: 1, record_id: 7, ok: true, active: false, value: "40", error: null }];
    expect(pinnedCheatRows([base], [7], live, [{ record_id: 7, active: true, value: "100" }], [{ record_id: 7, value: "250" }])[0].summary)
      .toBe("= 40");
  });

  it("resolves one value for the row, the refusal and the write", () => {
    expect(pinnedControlValue("40", "100", "250")).toBe("40");
    expect(pinnedControlValue("??", "100", "250")).toBe("100");
    expect(pinnedControlValue(null, " ", "250")).toBe("250");
    expect(pinnedControlValue(undefined, undefined, undefined)).toBe(null);

    // Which of the three, not what it says. This value is written into the game
    // and is preserved byte for byte; the trimming belongs to the row that
    // displays it, and doing both here turned a switch that re-applies what is
    // already there into one that rewrites it.
    expect(pinnedControlValue(" 40 ", "100", "250")).toBe(" 40 ");
    expect(pinnedControlValue("??", " 100 ", "250")).toBe(" 100 ");
    expect(presentableControlValue(pinnedControlValue(" 40 ", "100", "250"))).toBe("40");
  });
});


describe("a cheat switched on without the value it needs", () => {
  const control = (id: number, kind: string, over: Record<string, unknown> = {}) => ({
    id, description: `Cheat ${id}`, path: ["Enable", `Cheat ${id}`], variable_type: "4 Bytes",
    kind, group_header: false, has_assembler_script: false,
    dropdown_values: [], dropdown_read_only: false, ...over,
  }) as any;
  const staged = (entries: Array<[number, boolean | null, string | null]>) =>
    new Map(entries.map(([id, active, value]) => [id, { active, value }]));

  it("names what this Apply would switch on with nothing in it", () => {
    // Switching one of these on without a value does not do nothing: Cheat
    // Engine freezes whatever the game happens to hold at that address, and
    // Apply saved that into the table's startup state and reported success.
    const controls = [control(1, "value"), control(2, "script"), control(3, "dropdown", {
      dropdown_values: [["1", "One"]], dropdown_read_only: true,
    })];
    const missing = controlsMissingRequiredValue(
      controls,
      staged([[1, true, null], [2, true, null], [3, true, "  "]]),
      new Set([1, 2, 3]),
    );
    // The script is complete once it is on, and the two that are not are named.
    expect(missing.map((item) => item.id)).toEqual([1, 3]);
    expect(missingRequiredValueReason(missing)).toContain("need values");
    expect(missingRequiredValueReason(missing)).toContain("Cheat 1");
  });

  it("says nothing about what this Apply is not switching on", () => {
    const controls = [control(1, "value")];
    // Already on, already empty, and not switched on by this press: a state
    // this screen did not create, and refusing every Apply over it would block
    // every other change. Clearing the field of a record that is already on is
    // the same case, and is a supported ask: it drops the value stored for the
    // next session without writing a blank into a game holding a real number.
    expect(controlsMissingRequiredValue(controls, staged([[1, true, null]]), new Set())).toEqual([]);
    // Switched off, so there is nothing to supply.
    expect(controlsMissingRequiredValue(controls, staged([[1, false, null]]), new Set([1]))).toEqual([]);
    // And a value that is there is a value.
    expect(controlsMissingRequiredValue(controls, staged([[1, true, "500"]]), new Set([1]))).toEqual([]);
    expect(missingRequiredValueReason([])).toBeNull();
  });

  it("names a pinned cheat with no value, on or off", () => {
    // A pinned cheat is a switch on the quick access panel with nowhere to
    // type, so one pinned without a value can only ever be switched on empty.
    // The press that switches it on happens on another screen and later, so
    // this asks of every pinned record rather than the ones this press touched,
    // and without regard to whether it is on.
    const controls = [control(1, "value"), control(2, "script"), control(3, "value")];
    const missing = pinnedMissingRequiredValue(
      controls, staged([[1, false, null], [2, false, null], [3, false, "500"]]), [1, 2, 3],
    );
    expect(missing.map((item) => item.id)).toEqual([1]);
    // Nothing to say about a record nobody pinned.
    expect(pinnedMissingRequiredValue(controls, staged([[1, false, null]]), [])).toEqual([]);
  });

  it("never asks for a value on a cheat whose toggle is the whole control", () => {
    // A switch carries its value by construction: its toggle writes one key for
    // on and the other for off, and the row has no field because there is
    // nothing to type. Asking for one would refuse an Apply over a cheat the
    // reader can do nothing about from either screen, and a build that had not
    // yet drawn these as switches did exactly that - naming a flag on the quick
    // access panel that nobody could give a value to.
    const flag = control(1, "dropdown", {
      dropdown_values: [["0", "Disabled"], ["1", "Enabled"]], switch_on_value: "1",
    });
    const controls = [flag];

    // Switched on by this press with nothing staged beside it.
    expect(controlsMissingRequiredValue(controls, staged([[1, true, null]]), new Set([1]))).toEqual([]);
    // And pinned onto the panel with no value anywhere, which is the shape that
    // refused every Apply until it was dealt with.
    expect(pinnedMissingRequiredValue(controls, staged([[1, false, null]]), [1])).toEqual([]);
    // The rule is the switch, not the two entries: a list of two named
    // alternatives is a choice and still needs one.
    const choice = control(2, "dropdown", {
      dropdown_values: [["0", "Sword"], ["1", "Axe"]], dropdown_read_only: true,
    });
    expect(controlsMissingRequiredValue([choice], staged([[2, true, null]]), new Set([2])).map((item) => item.id)).toEqual([2]);
  });

  it("counts what differs, not what was touched on the way there", () => {
    // Switching a cheat on and off again leaves it touched for the rest of the
    // screen's life, so a form returned to exactly the state it opened in still
    // offered to discard a change it no longer held.
    const before = { 1: { active: false, value: null }, 2: { active: false, value: "10" } };
    expect(unsavedChangeCount(before, before, [1, 2])).toBe(0);
    expect(unsavedChangeCount({ ...before, 1: { active: true, value: null } }, before, [1, 2])).toBe(1);
    // A value that reads as the same absence is the same value: an empty
    // field, spaces, and Cheat Engine's own unreadable placeholder.
    expect(unsavedChangeCount({ 2: { active: false, value: "  " } }, { 2: { active: false, value: null } }, [2])).toBe(0);
    expect(unsavedChangeCount({ 2: { active: false, value: "11" } }, before, [2])).toBe(1);
    // And a record nobody touched is never counted, whatever it holds.
    expect(unsavedChangeCount({ 9: { active: true, value: "x" } }, {}, [])).toBe(0);
  });

  it("counts the rest rather than listing every one of them", () => {
    const controls = Array.from({ length: 6 }, (_, index) => control(index + 1, "value"));
    const missing = controlsMissingRequiredValue(
      controls,
      staged(controls.map((item) => [item.id as number, true, null] as [number, boolean, null])),
      new Set(controls.map((item) => item.id as number)),
    );
    expect(missingRequiredValueReason(missing)).toContain("and 3 more");
  });
});

describe("exact-PID attach recovery", () => {
  it("ranks Wine/Proton helpers last and marks them, without hiding them", () => {
    // This is the recovery route for an attachment that chose the wrong
    // process. Offering `xalia.exe` as an equally valid target - unranked and
    // unlabelled, unlike Review - is what made that recovery unstable.
    const candidates = runtimeAttachCandidates([
      [11, "xalia.exe"],
      [22, "Game-Win64-Shipping.exe"],
      [33, "crashpad_handler.exe"],
    ]);
    expect(candidates.map((candidate) => candidate.name)).toEqual([
      "Game-Win64-Shipping.exe", "crashpad_handler.exe", "xalia.exe",
    ]);
    expect(candidates[0].runtimeNoise).toBe(false);
    expect(candidates.slice(1).every((candidate) => candidate.runtimeNoise)).toBe(true);
  });

  it("names a live target that the profile will not use next time", () => {
    const attached = (target: string) => ({ status: { attached: true, target_process: target } }) as any;
    expect(divergentLiveTarget(attached("Game-Win64-Shipping.exe"), "Game-Win64-Shipping.exe")).toBeNull();
    expect(divergentLiveTarget(attached("GAME-WIN64-SHIPPING.EXE"), "Game-Win64-Shipping.exe")).toBeNull();
    expect(divergentLiveTarget(attached("Other.exe"), "Game-Win64-Shipping.exe")).toBe("Other.exe");
    expect(divergentLiveTarget({ status: { attached: false, target_process: "Other.exe" } } as any, "Game.exe")).toBeNull();
    expect(divergentLiveTarget(null, "Game.exe")).toBeNull();
  });
});

describe("Executable-content review", () => {
  // A table builds its cheats inside scripts: `Enable 1.0` creates the records,
  // an inner script creates the ones under it, and the cheats are the leaves.
  const script = (id: number, path: string[]): TableControl => ({
    ...control(id, "script", path[path.length - 1]), path,
  });
  const leaf = (id: number, path: string[]): TableControl => ({
    ...control(id, "value", path[path.length - 1]), path,
  });
  const controls: TableControl[] = [
    script(1, ["Enable 1.0"]),
    script(2, ["Enable 1.0", "Party Damage Reduction"]),
    leaf(3, ["Enable 1.0", "Party Damage Reduction", "Reduction %"]),
    leaf(4, ["Enable 1.0", "Easy Kills"]),
  ];

  it("lists an attach-only record with the scripts, without making it a dependency", () => {
    // A table author's own "attach to the game" button changes nothing in the
    // game and duplicates what CE Decky already did, so it belongs with the
    // machinery. But nothing is ever switched on through it, so it must not
    // join the enclosing scripts that Apply turns on to reach a cheat.
    const attach: TableControl = { ...control(9, "script", "game attach (f2)"), path: ["game attach (f2)"], attach_only: true };
    const withAttach = [...controls, attach];

    expect([...scriptListedControlIds(withAttach)].sort((a, b) => a - b)).toEqual([1, 2, 9]);
    expect([...enclosingControlIds(withAttach)].sort((a, b) => a - b)).toEqual([1, 2]);
  });

  it("releases a whole chain of scripts once the last cheat under it is off", () => {
    // Deepest first is what makes one pass enough: the inner script has to stop
    // counting as a user of the outer one before the outer one is judged.
    const active = new Map<number, boolean | null>([[1, true], [2, true], [3, false], [4, false]]);
    expect(unusedActiveScripts(controls, active)).toEqual([2, 1]);
  });

  it("keeps a script that still has an active cheat under it, at any depth", () => {
    expect(unusedActiveScripts(controls, new Map([[1, true], [2, true], [3, true], [4, false]]))).toEqual([]);
    expect(unusedActiveScripts(controls, new Map([[1, true], [2, false], [3, false], [4, true]]))).toEqual([]);
    // An off script is not released again, and a leaf is never a script.
    expect(unusedActiveScripts(controls, new Map([[1, false], [2, false], [3, false], [4, true]]))).toEqual([]);
  });

  it("never writes a script CE Decky switched on itself into the remembered state", () => {
    // This is what put `Enable 1.0` back on with every cheat under it off: the
    // ancestor Apply had to enable was recorded as though the user chose it.
    const states = [
      { record_id: 1, active: true, value: null },
      { record_id: 2, active: true, value: null },
      { record_id: 3, active: true, value: "95" },
    ];
    const touched = new Set([1, 2, 3]);
    const withoutOmission = rememberedSelection(controls, states, [], touched, new Set());
    expect(withoutOmission.map((item) => item.record_id)).toEqual([1, 2, 3]);

    const remembered = rememberedSelection(controls, states, [], touched, new Set(), new Set([1, 2]));
    expect(remembered).toEqual([{ record_id: 3, active: true, value: null }]);
  });

  it("drops a script an older build already stored as active", () => {
    // The stale entry is what a profile written before this fix carries, and it
    // has to go rather than be carried forward as a prior preference.
    const previous = [
      { record_id: 1, active: true, value: null },
      { record_id: 3, active: false, value: "95" },
    ];
    const states = [
      { record_id: 1, active: false, value: null },
      { record_id: 3, active: false, value: "95" },
    ];
    const remembered = rememberedSelection(controls, states, previous, new Set([3]), new Set(), new Set([1]));
    expect(remembered.map((item) => item.record_id)).toEqual([3]);
  });
});


describe("presentableControlValue", () => {
  it("keeps an ordinary value untouched", () => {
    expect(presentableControlValue("1234")).toBe("1234");
    expect(presentableControlValue("??")).toBeNull();
    expect(presentableControlValue("  ")).toBeNull();
  });

  it("replaces invisible and bidirectional formatting before it reaches a sentence", () => {
    // The semantic value is preserved byte for byte elsewhere because Cheat
    // Engine receives it; only the display copy is neutralized.
    const hostile = "100\u202e\u200b999";
    expect(presentableControlValue(hostile)).toBe("100\uFFFD\uFFFD999");
    expect(hostile).toBe("100\u202e\u200b999");
  });

  it("bounds a very long value so one row cannot take the panel", () => {
    const rendered = presentableControlValue("9".repeat(500));
    expect(rendered).not.toBeNull();
    expect((rendered as string).length).toBeLessThanOrEqual(97);
  });
});


describe("known anti-cheat observation", () => {
  it("recognizes the BattlEye launcher in the exact observed process set", () => {
    // The signal already existed as target-selection noise; it now also reaches
    // the policy layer, which is the whole point of the finding.
    expect(observedAntiCheat(["Game-Win64-Shipping.exe", "BELauncher.exe"])).toBe("belauncher.exe");
    expect(antiCheatBlockedReason(["Game-Win64-Shipping.exe", "belauncher.exe"]))
      .toContain("offline and single-player");
  });

  it("leaves an ordinary process set completely unaffected", () => {
    expect(observedAntiCheat(["Game-Win64-Shipping.exe", "crashpad_handler.exe"])).toBeNull();
    expect(antiCheatBlockedReason(["Game-Win64-Shipping.exe"])).toBeNull();
    expect(antiCheatBlockedReason([])).toBeNull();
  });

  it("matches only the exact basename, never a path segment", () => {
    expect(observedAntiCheat(["C:\\Games\\belauncher\\Game.exe"])).toBeNull();
    expect(observedAntiCheat(["C:\\Games\\Ark\\BELauncher.exe"])).toBe("belauncher.exe");
  });
});

describe("localTableArtifacts", () => {
  const SHA = "1".repeat(64);
  const ARCHIVE = "2".repeat(64);

  function table(origins: any[]) {
    return {
      sha256: SHA, filename: "Game.CT", size: 100, table_version: null, has_lua: false,
      has_auto_assembler: false, has_embedded_files: false, executable_content: false, entry_count: 1,
      blob_path: "/managed/Game.CT", available: true, schema_version: 2, origins,
    } as any;
  }
  function origin(extra: Record<string, unknown>) {
    return {
      provider: "fearless", artifact_id: "a1", topic_id: "t1", source_page: "https://example.invalid/t1",
      original_filename: "Game.zip", retrieved_at: "2026-08-30T00:00:00Z", advertised_sha256: ARCHIVE,
      ...extra,
    };
  }

  it("always resolves the table's own exact SHA", () => {
    expect(localTableArtifacts([table([])])).toEqual({ [`sha:${SHA}`]: SHA });
  });

  it("resolves a declared single-member archive digest to the imported table", () => {
    expect(localTableArtifacts([table([origin({ member_count: 1 })])])[`sha:${ARCHIVE}`]).toBe(SHA);
  });

  it("refuses a declared multi-member archive digest", () => {
    expect(localTableArtifacts([table([origin({ member_count: 3 })])])[`sha:${ARCHIVE}`]).toBeUndefined();
  });

  it("refuses an archive origin that declares no cardinality at all", () => {
    // Written before cardinality was persisted. Reading that silence as one
    // member made every other table in the archive unreachable through the
    // provider result, because the row reopened the one already imported.
    expect(localTableArtifacts([table([origin({})])])[`sha:${ARCHIVE}`]).toBeUndefined();
  });

  it("still resolves a direct .CT origin that declares no cardinality", () => {
    const direct = origin({ original_filename: "Game.CT" });
    expect(localTableArtifacts([table([direct])])[`sha:${ARCHIVE}`]).toBe(SHA);
  });

  it("answers for the store rather than for one game's library", () => {
    // "Local" says whether these bytes have to be fetched again, and content
    // identity settles that on its own. Asking the game's own library instead
    // made the same table published for a second game, and a first import whose
    // profile step failed after the bytes were stored, sit through another
    // provider countdown for a file already on disk.
    expect(localTableArtifacts([table([origin({ member_count: 1 })])])).toEqual({
      [`sha:${SHA}`]: SHA, [`sha:${ARCHIVE}`]: SHA,
    });
  });

  it("ignores a table whose bytes are gone", () => {
    const missing = { ...table([origin({ member_count: 1 })]), available: false };
    expect(localTableArtifacts([missing])).toEqual({});
  });

  it("matches an advertised digest whatever case the provider wrote it in", () => {
    const shouty = origin({ original_filename: "Game.CT", advertised_sha256: ARCHIVE.toUpperCase() });
    expect(localTableArtifacts([table([shouty])])[`sha:${ARCHIVE}`]).toBe(SHA);
  });

  it("names the provider rows a table was imported from, archive or not", () => {
    // The weaker answer, and the only one two of the four sources can give:
    // FearLess and every other phpbb attachment advertise no digest, so a table
    // already imported from one came back through search with no mark on it and
    // was downloaded again to reach bytes the device already had.
    expect(importedTableArtifacts([table([origin({ member_count: 3 })])])).toEqual(new Set(["fearless:a1"]));
  });

  it("ignores a provider row whose table is gone", () => {
    const missing = { ...table([origin({})]), available: false };
    expect(importedTableArtifacts([missing])).toEqual(new Set());
  });
});


describe("refused startup enable", () => {
  const result = (recordId: number, code: string, active: boolean | null) => ({
    generation: 0, record_id: recordId, ok: false, active, value: null,
    error: "activation did not settle", error_code: code,
  });

  it("names a record Cheat Engine was asked to switch on and left off", () => {
    // `activation_rejected` is only emitted after reading the record back and
    // finding it in the state that was not asked for, so the reported state is
    // the exact negation of the attempted one.
    expect(refusedStartupEnable([result(7, "activation_rejected", false)])?.record_id).toBe(7);
  });

  it("ignores a record Cheat Engine was asked to switch off and left on", () => {
    // A cheat that will not switch off is very likely still running in the
    // game; calling the table unusable there misdescribes it.
    expect(refusedStartupEnable([result(7, "activation_rejected", true)])).toBeNull();
  });

  it("names an enclosing script the plan turned on over a saved off", () => {
    // Session preparation adds the enclosing scripts a remembered cheat needs,
    // and overrides a parent saved as off to on when an active child requires
    // it - so the saved selection is not evidence of what startup attempted.
    // The result is.
    expect(refusedStartupEnable([result(80014, "activation_rejected", false)])?.record_id).toBe(80014);
  });

  it("ignores a startup failure that is not a refusal", () => {
    expect(refusedStartupEnable([result(7, "target_detached", false)])).toBeNull();
  });

  it("ignores a record that reported no state at all", () => {
    // Nothing about which direction was attempted can be read from that.
    expect(refusedStartupEnable([result(7, "activation_rejected", null)])).toBeNull();
  });

  it("ignores a live result that merely shares the record", () => {
    // Generation zero is startup; anything else is a command someone sent.
    const live = { ...result(7, "activation_rejected", false), generation: 4 };
    expect(refusedStartupEnable([live])).toBeNull();
  });
});

describe("what a blocked record says about itself", () => {
  const entry = (over: Record<string, unknown>) => ({
    key: "k", sha256: null, reason: "did not work", filename: null,
    app_id: null, game_name: null, game_version: null, recorded_at: 1,
    ...over,
  }) as any;

  it("names a row by its game first, then by the table", () => {
    // The list spans every game and is read months later: `winmm-x64.zip` and
    // a raw provider key named neither a game nor a table, so the rows one
    // game's search had just retired could not be found in it.
    expect(blockedRowLabel(entry({
      sha256: "a".repeat(64), filename: "winmm-x64.zip", game_name: "Neon Bazaar",
    }))).toBe("Neon Bazaar · winmm-x64.zip");
  });

  it("falls back to whatever identity the entry actually has", () => {
    // A record with no game is one written before the game travelled with the
    // press. A record with no file name is a provider row whose file the source
    // no longer has, and which never produced bytes to name.
    expect(blockedRowLabel(entry({ sha256: "a".repeat(64), filename: "hl2.CT" }))).toBe("hl2.CT");
    expect(blockedRowLabel(entry({ sha256: "a".repeat(64) }))).toBe("a".repeat(12));
    expect(blockedRowLabel(entry({
      key: "row:vgtimes:g-x:f-1", origins: ["vgtimes:g-x:f-1"], game_name: "Neon Bazaar",
    }))).toBe("Neon Bazaar · vgtimes:g-x:f-1");
  });

  it("places the record before it explains it", () => {
    // The runtime writes 230 characters for the failure this list is almost
    // always about, and the row led with them: the day, the release and the
    // build a reader scans for were all past the end of the line, and a reveal
    // had to travel the whole sentence to reach the first of them.
    const detail = blockedRowDetail(entry({
      sha256: "a".repeat(64), table_version: "1.05.01", game_version: "1.2.3",
      recorded_at: 1_700_000_000,
      reason: "MemoryRecord 12 did not switch on: Cheat Engine ran it and it went straight back off."
        + " A cheat table finds the game's code by scanning for patterns, so this normally means this"
        + " table was written for a different build of the game.",
    }));
    expect(detail.startsWith("2023-11-14 · v1.05.01 · game 1.2.3 · MemoryRecord 12 did not switch on:")).toBe(true);
    // The half that explains why the failure is usual is not on the row.
    expect(detail).not.toContain("scanning for patterns");
  });

  it("takes the release from a copy still held when the record carries none", () => {
    // The field was added after records already existed, and a record outlives
    // the file it is about: the device answers while it still has the bytes,
    // and nothing is invented once it does not.
    const held = [{
      sha256: "a".repeat(64),
      origins: [{ version: "1.6" }, { version: "2.0" }],
    }] as any;
    expect(blockedRelease(entry({ sha256: "a".repeat(64) }), held)).toBe("v2.0");
    // The record's own field is what was true when the mark was written, so it
    // outranks whatever the device holds now.
    expect(blockedRelease(entry({ sha256: "a".repeat(64), table_version: "1.6" }), held)).toBe("v1.6");
    expect(blockedRelease(entry({ sha256: "b".repeat(64) }), held)).toBeNull();
  });

  it("cuts a recorded reason at a sentence, never inside a version", () => {
    expect(shortBlockedReason("It failed. Because of a thing."))
      .toBe("It failed.");
    // A full stop with no space after it is a version, a file name or a digest,
    // and ending a row inside one of those says less than saying nothing.
    expect(shortBlockedReason("Table 1.05.01 refused"))
      .toBe("Table 1.05.01 refused");
    expect(shortBlockedReason("")).toBeNull();
    const long = `${"word ".repeat(60)}end. And more.`;
    const cut = shortBlockedReason(long) as string;
    expect(cut.endsWith("\u2026")).toBe(true);
    expect(cut).not.toContain("And more");
  });

  it("carries the cause to both the ways a row is recognised", () => {
    // The digest recognises a result only where the provider advertises one,
    // and the row recognises every result: the mark has to say the same thing
    // whichever of the two found it.
    const { byDigest, byArtifact } = blockedTableLookups([
      entry({ key: "a".repeat(64), sha256: "a".repeat(64), origins: ["fearless:t-1:a-2"], cause: "refused" }),
    ]);
    expect(byDigest["a".repeat(64)]).toEqual({
      sha256: "a".repeat(64), reason: "did not work", cause: "refused", recordedAt: 1,
    });
    // A list, because one provider row serves several revisions over the years
    // and each of them can earn a record of its own.
    expect(byArtifact["fearless:t-1:a-2"]).toEqual([byDigest["a".repeat(64)]]);
  });

  it("keeps every record one provider row has earned, newest first", () => {
    // Assigning one mark per row let whichever the loop wrote last stand for
    // all of them, so a source failure could be hidden behind an older
    // exact-byte refusal of a revision that row no longer serves.
    const { byArtifact } = blockedTableLookups([
      entry({ key: "row:fearless:t-1:a-2", reason: "the source no longer has it", origins: ["fearless:t-1:a-2"], cause: "gone", recorded_at: 900 }),
      entry({ key: "a".repeat(64), sha256: "a".repeat(64), reason: "went straight back off", origins: ["fearless:t-1:a-2"], cause: "refused", recorded_at: 100 }),
    ]);
    expect(byArtifact["fearless:t-1:a-2"].map((mark) => mark.cause)).toEqual(["gone", "refused"]);

    // And newest first is decided here rather than inherited from the order the
    // record arrived in, so a reader asking which mark is current is answered
    // by the data and not by the caller having sorted it.
    const reversed = blockedTableLookups([
      entry({ key: "a".repeat(64), sha256: "a".repeat(64), origins: ["fearless:t-1:a-2"], cause: "refused", recorded_at: 100 }),
      entry({ key: "row:fearless:t-1:a-2", origins: ["fearless:t-1:a-2"], cause: "gone", recorded_at: 900 }),
    ]);
    expect(reversed.byArtifact["fearless:t-1:a-2"].map((mark) => mark.cause)).toEqual(["gone", "refused"]);
  });

  it("keys a file the source no longer has by the row, and knows why it is here", () => {
    const { byDigest, byArtifact } = blockedTableLookups([
      entry({ key: "row:vgtimes:g-x:f-1", origins: ["vgtimes:g-x:f-1"], cause: "gone" }),
    ]);
    expect(byDigest).toEqual({});
    expect(byArtifact["vgtimes:g-x:f-1"]).toEqual([{
      sha256: "row:vgtimes:g-x:f-1", reason: "did not work", cause: "gone", recordedAt: 1,
    }]);
  });

  it("says only that the table does not work when it cannot say more", () => {
    // An entry written before the cause was recorded, and one naming a cause
    // this build does not know. Both are true records of a table that does not
    // work, and a chip it cannot make exact must never cost either of them.
    const { byDigest } = blockedTableLookups([
      entry({ key: "a".repeat(64), sha256: "a".repeat(64) }),
      entry({ key: "b".repeat(64), sha256: "b".repeat(64), cause: "something-later" }),
    ]);
    expect(byDigest["a".repeat(64)].cause).toBe("unknown");
    expect(byDigest["b".repeat(64)].cause).toBe("unknown");
  });
});

describe("selfTestSummary", () => {
  const blocking = { name: "settings", ok: true, detail: "/settings", blocking: true };

  it("says PASS only when every check passed", () => {
    const summary = selfTestSummary({ ok: true, checks: [blocking, { name: "host_7zip", ok: true, detail: "/usr/bin/7z", blocking: false }] });
    expect(summary.verdict).toBe("pass");
    expect(summary.label).toBe("Self-test PASS");
    expect(summary.counts).toBe("2/2 checks");
    expect(summary.failures).toEqual([]);
    expect(summary.toast).toBe("Plugin self-test passed.");
  });

  it("says WARN when a non-blocking check failed, and carries it", () => {
    // The backend's `ok` is about blockers alone, so this is a valid result:
    // the journal a later bug report depends on cannot be read, and nothing
    // about the overall verdict says so.
    const failed = { name: "system_journal", ok: false, detail: "OPENSSL_3.4.0 not found", blocking: false };
    const summary = selfTestSummary({ ok: true, checks: [blocking, failed] });
    expect(summary.verdict).toBe("warn");
    expect(summary.label).toBe("Self-test WARN");
    expect(summary.failures).toEqual([failed]);
    expect(summary.toast).toBe("Plugin self-test found 1 warning.");
    expect(selfTestSummary({ ok: true, checks: [failed, { ...failed, name: "managed_root_space" }] }).toast)
      .toBe("Plugin self-test found 2 warnings.");
  });

  it("says FAIL for a blocking failure, and puts blockers before warnings", () => {
    const blocker = { name: "managed_root", ok: false, detail: "read-only file system", blocking: true };
    const warning = { name: "host_7zip", ok: false, detail: "7z not found", blocking: false };
    const summary = selfTestSummary({ ok: false, checks: [warning, blocker] });
    expect(summary.verdict).toBe("fail");
    expect(summary.failures).toEqual([blocker, warning]);
    expect(summary.toast).toBe("Plugin self-test found a blocker.");
  });

  it("resolves a disagreement towards the worse answer", () => {
    // A backend that says no and names no blocking failure is still the
    // authority on its own verdict, and a check from a newer backend that
    // carries no flag at all is blocking, which is how the backend reads it.
    expect(selfTestSummary({ ok: false, checks: [blocking] }).verdict).toBe("fail");
    const unflagged = { name: "future_check", ok: false, detail: "unknown" } as unknown as typeof blocking;
    expect(selfTestSummary({ ok: true, checks: [unflagged] }).verdict).toBe("fail");
  });

  it("reads a check name as a person does, without a mapping that can go stale", () => {
    expect(selfTestCheckLabel("system_journal")).toBe("System journal");
    expect(selfTestCheckLabel("cheat_engine_identity")).toBe("Cheat engine identity");
    expect(selfTestCheckLabel("")).toBe("unnamed check");
  });
});
