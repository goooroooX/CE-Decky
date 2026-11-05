# Address mismatch detection for Unreal Engine tables

**What this is for.** A cheat can be switched on, read back as active and
still do nothing, because the address it writes belongs to the wrong object.
This plan describes a detector that would tell the user which of a table's
functions reach their intended object, by checking each one's address
dependencies through the Unreal Engine object model (the 4.23+ FName pool and
object array) rather than trusting activation.

**Status: deferred, not built.** The maintainer set a gate before any work:
build it only if one reviewed layout reader could check at least one table in
ten. Measured on 2026-11-05 with `provider_corpus_survey.py --engines` over
259 distinct fresh and recent FearLess tables, at most 10 (3.9%) name the
Unreal object system at all, and 2 its 4.23+ name pool. 46 are for Unreal
games, and the other 36 of those never name that object system, so a layout
reader has nothing of theirs to check without a recipe written for the table.
That is an upper bound below the gate, so the detector is not built. Section
10 has the method.

The plan is kept because the evidence model, the trust boundary and the
false-positive cases it collected would apply to any later address checker,
including one for another engine's layout. The agent procedure that proved
those cases is `docs/TABLE_REPAIR.md`, which now carries them, and the
improvement this would serve is the first row of `docs/IMPROVEMENTS.md`.

Design draft, 2026-11-04. This is not a production feature or a claim that an
arbitrary table's effects can be proved automatically. The eleven-function
repair experiment is the concrete case used to test the design.

## 1. Problem and evidence boundary

A MemoryRecord can remain active while its Lua timer finds no targets. It can
also write successfully to readable memory belonging to the wrong object.
Activation, successful memory access, an intended game-state change and a visible
gameplay effect are different facts and must not share one success flag.

The initial case had an exact connected/attached session and two active ammo
scripts, but the shared world address led to unrelated readable data. Correcting
world and name-pool dependencies restored object discovery and the maintainer
confirmed the effects across a weapon switch. The two correction deltas differed.
One blanket relocation delta would have produced another wrong address.

The detector's job is to identify contradicted address assertions, report missing
evidence honestly and provide actionable reasons. It must not execute a table
before consent or guess the game's intended semantics from a cheat's label.

## 2. Unit of diagnosis

Diagnose a dependency and the records that use it, not just the whole table.
The dependency graph can include module identities, instruction signatures,
module-relative globals, pointer chains, object-name pools, class ancestry,
ownership/self links, field types/ranges and script-owned allocations.

A broken shared root can affect many functions. A broken recoil sub-array must
not retire working ammunition cheats. A setting whose parent script is off has
no allocated address yet and is not a failed address check.

Bind every observation to:

- exact source table SHA and assertion-recipe SHA;
- AppID, explicit target and unambiguous process/start-time identity;
- exact executable/module SHA, module mapping and observed Proton identity;
- CE executable/tree/bridge and session/descriptor identities when CE is involved;
- record IDs, dependency IDs, timestamp/window, operation and game-state context.

Different versions and sessions never share pending proof. Two module slots that
point to the same object remain distinct candidates until their roles are proved.

## 3. Evidence model

Keep activation, addressing, writes and gameplay evidence separately:

| Axis | Examples of states | What it can prove |
|---|---|---|
| Session | unavailable, attached-current, stale, changed | Which runtime and process were observed |
| Activation | off, pending, acknowledged-on, rejected | CE accepted or refused a record change |
| Addressing | untested, waiting, validated-now, contradicted, inconclusive | Explicit structural/instruction assertions held or failed |
| Writes | untested, attempted, accepted, readback-matched, readback-different | What happened to a specific validated field |
| Gameplay | untested, user-confirmed, user-reported-failed | A named effect was observed under a named scenario |

These states are not a linear scale. A timer can have validated targets but no
write because the field already equals the desired value. An event hook can be
installed correctly but have no hits until the relevant event occurs. An active
ESP can collect entities but produce no visible overlay because of projection or
window composition, which is not the same defect as invalid actor addresses.

## 4. Assertion recipes

The production candidate is a bounded, data-only recipe understood by trusted
code, initially reviewed and shipped for exact table/build identities. It may
contain only explicitly supported reads, pointer steps, instruction matches,
structural equalities, class/name checks and numeric ranges. No Lua source,
callback, shell command, arbitrary evaluation or network-provided executable code.

Examples of useful assertions:

- A world pointer's class resolves through the validated name pool to World.
- The chain passes through a local-player controller, not an arbitrary actor.
- Inventory outer and weapon owner equal the independently established pawn.
- A magazine's self links, class ancestry and size support the fields accessed.
- A fire mode owns the recoil/bloom settings and its curve count/stride fit.
- An original hook signature is unique before activation; after activation its
  owned patch and return destination match the reviewed injection.
- Camera position/rotation use their actual floating-point widths and the FOV
  field lies in the reviewed POV layout. A failed read at an agent's guessed
  offset is not evidence against the table.

Report which independent assertions held. A readable pointer, plausible health
number or familiar object name alone is not sufficient. Array truncation, unknown
name encoding, a class-chain depth limit and partial scans remain explicit.

The current development tools already support exact identity binding, typed
reads, FName/class surveys, pointer discovery, masked scans and signed
RIP-relative resolution. They are agent tools, not a production arbitrary-memory
RPC. Production must derive allowed checks from the exact reviewed recipe and
repeat validation at the host/bridge boundary.

Where the trusted reader runs is still open, and it decides the trust boundary.

- **procfs from the backend.** The development probe already opens a game's
  `/proc/<pid>/mem` from the agent's shell, a same-user process that is not
  the game's ancestor, under `kernel.yama.ptrace_scope=1`, and
  `docs/FIELD_NOTES.md` records that `ptrace` between unrelated same-user
  processes is not refused there. The refusal the same notes record for the
  backend's `environ` is about the backend as a *target*: it is non-dumpable,
  so others cannot read it. It says nothing about the backend as a reader.
  What is unproved is narrower: whether the backend, a child of the root
  `plugin_loader` service running as the Steam user because `plugin.json`
  asks for no `root` flag, inherits anything from that service that stops
  it opening the game's `mem`. `process_vm_readv` passes the same ptrace
  access check, so it is not a separate route with a separate answer.
- **The bridge.** It is already attached and reads memory through Cheat
  Engine, so the reader would be bridge Lua, versioned by the bridge SHA in
  the runtime compatibility key. But it runs in the same Lua state as the
  table, after the table has executed, and table Lua can replace the global
  read functions Cheat Engine exposes. By section 6's own rule, an assertion
  evaluated there is executable-content testimony (`table_reported`), not an
  independent address check.

Only the procfs reader gives independent addressing evidence. Settle whether
the backend can open the game's `mem` with one device measurement, and record
the choice in `docs/SECURITY.md` before any code depends on it. If the backend
cannot, the detector's addressing axis is limited to `table_reported`, and the
whole design has to be re-weighed against that.

## 5. Game-state readiness and negative verdicts

Readiness is itself evidence. Distinguish missing local player, loading, respawn,
mounted pawn, unequipped weapon, unavailable gear, no enemies and a script not
started. A fixed timeout cannot turn any of these into incompatibility by itself.

A contradicted assertion may justify an address-mismatch result when:

1. Exact process/module identities still agree.
2. The required object/game state is independently present.
3. The violated invariant is meaningful in that state and belongs to the exact
   recipe/version being tested.
4. The reading is complete enough to support that negative result, with moving
   objects/arrays and transport errors excluded or named as inconclusive.

Repeated observations can reject a transient sample, but repetition does not
supply missing semantics. Ten empty scans for a gear class do not prove a bad
table if that gear is not equipped.

## 6. Timer and hook observations

For a reviewed Lua timer, useful optional telemetry is tick progress, candidate
count, validated-target count, write attempts, explicit failures, matched
readbacks and a bounded reason code. Zero writes is normal when values already
match; zero targets is often a readiness state. Catching callback errors without
reporting them reproduces the original silent failure.

Telemetry produced by a table is an assertion from executable content, not
independent proof. Compare it with trusted reads where possible. Do not rewrite
arbitrary Lua or intercept every global CE function and assume the resulting
instrumentation is transparent. Recognized patterns and agent-reviewed repairs
can add diagnostics under a new table SHA with fresh consent.

For a hook, installation, an actual hit and its effect need separate evidence.
Counters can establish hits but not that the right actor was modified. Validate
the local-player/target relationship independently. Preserve original instructions
and register/flag behavior; use a reviewed disable path and verify restoration.

## 7. Runtime and UI integration

Run expensive inspection on demand and during review/preparation. Routine checks
should use bounded already-resolved identities and assertions. Do not add an
unbounded process, heap or object-array scan to the existing heartbeat.

An initial UI could show per-record status such as:

- Script on; target checks not available.
- Waiting for a supported weapon or active gear.
- Target addresses validated in this session.
- Address mismatch: inventory owner differs from the local player.
- Write accepted; gameplay effect not confirmed.

Expose the exact failed dependency and affected functions in details. A manual
Works/Does not work answer records human evidence separately and names its scope.
Do not make an active flag or one successful function imply every function works.

The product already makes that claim once: the record that a table worked is
written from one cheat's verified activation and readback, and the source table
in this experiment earned it while its ammo scripts did nothing. A detector
result must not be overruled by that record, and the record's wording in the
panel has to say what it proves (activation) once the detector can say more.

Use the existing generation/ACK protocol for commands and any supported record
diagnostics. Only the exact current session can acknowledge an operation. A
negative ACK is failure; a timeout may follow a mutation that happened and must
not be retried blindly. No automatic write, freeze or arbitrary code probe is a
diagnostic prerequisite.

## 8. Advisory marks, storage and support bundles

Keep address mismatch, observed failed effect and transport/readiness failures as
different typed causes. A table-level summary can say partially working while
preserving usable functions. Exact-build observations must not become a universal
claim about every version of a table or game.

All marks remain advisory and clearable offline. Clearing a mark restores every
affected path, and local tables remain usable under the standing contract.
Neither health checks nor positive evidence authorize execution or replace blob
verification. New durable state requires a migration preserving existing failure
epochs and compatibility records before device testing.

At each decision, log the identity, dependency/record, state change, reason code,
readiness and completeness. Do not collect raw game memory, executable images,
source code, arbitrary exception locals or secret-bearing values into a public
bundle. Optional details should be allowlisted or recorded by presence alone.
Any new durable fields, diagnostics or log need support-bundle collection and a
regression, inside the existing whole-bundle and ring budgets. Repeated healthy
samples should not flood the journal.

## 9. Relationship to repair

Detection proposes a violated dependency, not a new address. Candidate discovery
then searches a bounded scope and proves each replacement independently. A
repair must list affected records and unresolved dependencies, preserve the
original, produce a separate exact-SHA file and a complete reviewable diff, and
follow the ordinary review/consent/session-replacement workflow.

Record the derivation in the existing `derived_from` provenance a stored table
already carries, rather than in a new field. Diagnostics added for the
experiment are not part of the delivered copy: it is rebuilt with them switched
off, the builder is shown to reproduce the instrumented copy byte for byte, the
diff against it is removed lines only, and the result takes its own SHA,
consent and enable/disable retest (`docs/TABLE_REPAIR.md`, Deliver without
diagnostics).

Never choose the first match or replace every global by one delta. Do not carry
runtime preferences or consent across SHAs. Verify field behavior after enabling
and restoration after disabling, then test gameplay and transitions. A repaired
address graph still does not prove hook semantics or overlay presentation.

## 10. Validation and staged delivery

Required cases include readable wrong objects, nil chains, menus/respawn, absent
gear, untriggered hooks, prerequisite scripts off, already-matching values,
moving arrays, duplicated signatures, overlapping matches, truncated scans,
failed writes, process reuse, changed module bytes and changed session identity.

Stage delivery from agent evidence and reports, to reviewed per-table health
recipes, to explicit per-function UI diagnostics, and only then to offered
repair recipes. Broad compatibility claims require multiple independent tables,
builds and device sessions. No percentage estimated from one table is a general
repair success rate.

Coverage is the cost question this design has to answer first. Without a
reviewed recipe the detector can say nothing beyond today's activation
readback, and recipes exist only for tables an agent has repaired, so the
first stages help a handful of tables while every other table shows
"target checks not available". Each stage is worth building only if that
number grows: measure, over the tables real users hold, how many share an
engine layout (the 16/16 FName pool and object array used here) that one
reviewed layout reader could check without a per-table recipe.

**Go/no-go gate (maintainer decision, 2026-11-05).** That measurement comes
before any other work on this design. It runs over a corpus of recent,
popular tables from different games, not over the tables already repaired.
If one reviewed layout reader could check fewer than one table in ten, the
detector is not built, and this draft stays a record of the experiment.

**Result, 2026-11-05: below the gate.** The corpus was grown with
`provider_corpus_survey.py --strata fresh,recent`, which samples topics by
forum activity (the listing is ordered by last post, so a topic on the first
pages is one people still use) and takes one game once. The listing carries no
reply or view counts, so recent activity is the popularity measure. Over the
259 distinct usable fresh and recent tables, `--engines` counted:

| Mark in the table's text or topic title | Tables | Share |
|---|---|---|
| Names the Unreal object system (`GUObjectArray`, `GNames`, `GWorld`, `UObject` and the like) | 10 | 3.9% |
| Of which the 4.23+ name pool this design's reader decodes | 2 | 0.8% |
| An Unreal game, by its `-Win64-Shipping` module or a title tag | 46 | 17.8% |
| Unity, Mono | 69 | 26.6% |
| Unity, IL2CPP | 47 | 18.1% |

A mark is an upper bound: naming the object system does not mean a record's
address is reached through it. The Unreal tables that do not name it give a
layout reader nothing to check without a recipe written for that table; how
they do reach their targets was not measured. Reopening this needs a new
measurement above the gate, for this layout or another engine's.

## 11. Experiment record

One table for one Unreal Engine game, repaired by the procedure in
`docs/TABLE_REPAIR.md` on the Steam Machine on 2026-11-04. Its capture stays on
that device and is not part of this repository. Source table SHA `cea59188ac37`;
the eleven-function experiment copy `0d3ade541646`, then the final instrumented
copy `7f96e67d1271`, which differs from it only in the saved-value class
invalidation of two functions, retested on that SHA with its own consent and
session. The delivered copy `ce52758053bd`, 14 records, is the final copy built
again with every diagnostic switched off, and is recorded as an
`external-repair` of the source (0.9.34).

The experiment exercised all eleven functions. Seven received a general human
gameplay confirmation, with independent target fields or actual hook hits. One
changed and restored validated target fields while sampled allies stayed
unchanged. Two had no validated targets in the observed equipment state. An ESP
produced entities and projected boxes, while its form renderer conflicted with
the bridge's window suppression; visible rendering was not confirmed. No
table-level claim that all eleven effects work follows.

Every false-positive case this design has to survive came from that session,
and each is now part of the agent procedure in `docs/TABLE_REPAIR.md`: an
assertion one byte past a hook's overwritten range, a `[DISABLE]` inside a
comment, a transport error after a mutation that ran, a `None` name decoded
from readable zeros, zero writes or hits that were correct, a replaced object
behind an unchanged process identity and world pointer, and a saved-value cache
that has to drop an entry when its address changes class.

This draft adds no production behavior, schema, automatic mark or renderer.

## 12. Proposed implementation contract

Keep discovery and repair outside the live detector initially. The detector
consumes a reviewed assertion manifest, pinned to exact table and executable
digests; it does not fetch code or invent offsets. Its versioned dependency IDs
and operations are drawn from a trusted allowlist. The prototype JSON recipes
are evidence for that manifest's content, not an exposed arbitrary-memory RPC.

Separate the following backend concerns:

1. Preparation verifies blob/module/runtime identities and resolves the allowed
   manifest. Unknown identities yield `unsupported`, not `bad`.
2. A trusted bounded reader evaluates readiness and addressing assertions. It
   returns the assertion version, sample window, dependency/record IDs, typed
   outcomes, reasons and completeness. It cannot write memory.
3. A reducer rejects stale session/process samples and maintains the independent
   axes in section 3. Its persistence contains only allowlisted evidence, never
   absolute heap addresses carried into another process.
4. UI/support logging records meaningful state transitions and an actionable
   reason. A manual not-working mark remains distinct from technical mismatch.
5. A separately authorized repair action builds a new artifact and follows the
   ordinary exact-SHA consent/session replacement path.

Invalidate cached descendants whenever a root, pawn, owner, array identity or
readiness prerequisite changes, even inside one process. Revalidate dynamic
objects in each sample; module-relative discoveries have a different lifetime
from heap pointers. Same-class address reuse cannot be excluded by a class
check alone. Use a reviewed object serial/weak-handle layout where available;
otherwise report the narrower current-observation guarantee. A successful sample
must not become permanent object authority.

Readiness cannot be proved solely from the chain under test. A failed world/name
pair cannot identify which member is wrong without an independent anchor.
Similarly, enumeration derived from a broken actor array cannot prove that no
enemy is present. Record `dependency_unresolved` rather than a narrower cause
the observations do not support. Two agreeing aliases or repeated reads of one
bad chain are not independent assertions.

Keep logical manifest errors distinct from a runtime contradiction. Validate
assertion types, pattern canonicalization, expected byte lengths, array strides,
dependency order, field widths, count bounds and supported engine layouts before
running a sample. Invalid manifests are disabled and logged as tool defects.

Suggested result fields are `session_identity`, `process_start_identity`,
`table_sha256`, `module_sha256`, `manifest_sha256`, `sample_started_at`,
`sample_finished_at`, `record_id`, `dependency_id`, `readiness`, `assertion_state`,
`reason_code` and `complete`. Optional write/event telemetry carries a source
label (`table_reported` or independently observed) and never changes address
validation on its own. Suggested reasons include `player_unavailable`,
`equipment_unavailable`, `class_mismatch`, `owner_mismatch`, `signature_ambiguous`,
`observation_incomplete`, `write_failed`, `event_not_observed`,
`presentation_unsupported` and `transport_interrupted`.

Limit routine checks to already resolved dependencies and explicit budgets;
cancel on identity change. Exact budgets and cadence need a measured prototype
on both target machines before product adoption. Expensive complete scans run
only on demand. A budget overrun is incomplete evidence, never absence.

The reducer may expose a persistent recommendation to mark the exact table/build
as not working after a complete, independently supported contradiction. It must
not silently turn waiting, unsupported rendering, untriggered events, activation
errors of unknown cause or transport failures into an address-mismatch mark.
Per-function partial results remain visible. Clearing any advisory mark clears
all dependent suppression, following the existing offline/local-table contract.

Regress the full path from sample to support bundle and back to a cleared mark:
same SHA on a different build, same PID with a new start time, new CE session,
readiness loss/recovery, bad manifest, incomplete paging, concurrent control
generation, mutation ACK followed by failed readback, and mixed working/waiting/
contradicted records. No automatic memory patch is part of that first feature.
