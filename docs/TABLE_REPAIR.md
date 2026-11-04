# Agent procedure for table address repair

The task is to distinguish a potentially repairable address dependency from a
table that merely activates. This procedure is development tooling, outside the
plugin's production runtime. It does not promise a general repair for arbitrary
Lua, infer a game's semantics, or grant execution consent.

## Evidence and stop conditions

| Evidence | What it establishes | What remains unknown |
|---|---|---|
| Exact blob and executable digests | Which bytes a result concerns | Whether either is compatible |
| Loaded table, fresh bridge and attached target | Transport and attachment succeeded | Whether a script reached its target |
| Active readback | CE accepted the script's activation | Whether a timer found anything or a hook ran |
| Readable pointer or unique byte pattern | A candidate exists in this process | Whether it represents the intended object or instruction |
| Stable chain, expected class, owner and typed fields | Independent structural assertions hold in this observation | Whether they survive another level, weapon, process or build |
| Write readback | The named field changed | Whether the game uses that field or later overwrites it |
| Controlled gameplay comparison | The tested effect occurred in that scenario | Other cheats and untested transitions |

An unavailable player during menus, loading or respawn, unreadable memory, a
spent budget, multiple matches and a missing script-owned symbol are
inconclusive. A readable wrong object can be more dangerous than a null pointer.
Do not choose the first match, apply one delta to unrelated globals, freeze a
candidate to see what happens, or label a table incompatible because `??` was
read while its prerequisite script was off.

Stop on process/file identity drift, ambiguous instances, an anti-cheat refusal,
failed CE cleanup or inability to preserve the original state. Do not bypass
procfs permissions or anti-cheat. Use the remote-target procedure when the game
is on another machine. A live read spans a window; the tool does not pause the
game or make a coherent snapshot of all its memory.

## Capture and inventory

Follow `AGENTS.md` preflight and incident capture first. Keep the support bundle,
state probe, original runtime queries and logs together under
`build/incidents/<symptom>-<UTC>/` before any recovery. Record every mutation in
order, including session changes, cheat toggles and gameplay requested of the
maintainer. Reports contain private process paths and values and stay local.

Use `target_state_probe.py --appid <id> --startup-plan` for the exact selected
SHA, verified source path, consent, pins, typed values and startup state. Use
`target_runtime_query.py` for exact active/value readback. Inspect statically:

```bash
python3 scripts/table_repair.py inspect '<verified-table>' --sha256 '<table-sha>'
```

Inventory every record, not just the first broken cheat. Group dependencies by
what the code actually shares: module globals, pointer chains, object-name
tables, script allocations, instruction signatures, function calls and UI-only
records. Literal candidates include constants and comments and omit dynamic
Lua expressions and addresses written without `0x`. Review each script to its
end, including timer callbacks and disable/restore paths. A callback that
catches an error or returns an empty list can leave an active record doing no
work. A spawner's activation may only open its window; invoking a spawn is a
separate effect and authorization decision.

Export decoded script bodies with exact source/record identities for that review:

```bash
python3 scripts/table_repair.py extract '<verified-table>' --sha256 '<table-sha>' \
  --record 10 20 --output-dir '<new-incident-subdirectory>'
```

The directory must be new; all requested IDs are checked before it is created.
The report fingerprints each exported text file. Decoding XML entities is not
execution, and an exported script must still only be loaded for syntax checks.
Match section headers as standalone lines: a comment can itself contain
`[DISABLE]`. Keep instrumentation inside the existing `{$lua}` region, before
any `{$asm}` switch. Lua compilation alone cannot catch code appended in an
assembler region.

## Read-only target recipes

Establish the executable path independently from the installed library or the
production observation, never from a guessed directory. The target probe
requires the live profile's exact SHA and target, production blob inspection,
one corroborated AppID/target/start-time process, and that file's exact mapping.

```bash
python3 scripts/target_table_memory.py --appid '<id>' --table-sha '<table-sha>' \
  --executable '<exact-game.exe>' --out '<incident>/identity.json'
```

Without a recipe this records identities and loaded x86-64 PE sections. Create a
local JSON recipe using its table and executable digests. Keep the recipe with
its report: `recipe_sha256` fingerprints its JSON normalized with sorted keys,
compact separators and ASCII escaping, and the report names its UTC reading
window. Synthetic example:

```json
{
  "schema": 1,
  "table_sha256": "<exact table digest>",
  "executable_sha256": "<exact executable digest>",
  "operations": [
    {"name": "player", "kind": "walk", "base": "module", "offsets": ["0x123450", "0x20"]},
    {"name": "owner", "kind": "read", "base": "player", "offset": "0x18", "equals": "player"},
    {"name": "count", "kind": "read", "base": "player", "offset": "0x40", "type": "u32", "expect": 3}
  ]
}
```

These offsets and assertions are examples, not an engine layout. Run with
`--recipe '<incident>/recipe.json' --out '<incident>/new-reading.json'`. Output
files must be new. The recipe permits only these data operations:

| Kind | Required fields and interpretation |
|---|---|
| `address` | `base`, optional `offset`: compute an address without claiming it was read |
| `read` | `base`, optional `offset` and `type`: `u8`, `u16`, `u32`, `i32`, `u64`, `f32`, `f64`, or `bytes` with `size` at most 256; byte output is hex |
| `walk` | `base`, `offsets`: dereference a 64-bit pointer at each added offset, at most 24 hops; reread the chain to reject movement |
| `scan` | `pattern`, optional `sections` (`executable`, `writable`, `all`), `section_rvas` and `limit`: scan the loaded module; only a complete single match supplies a scalar address |
| `rip_relative` | `base`, `displacement_offset`, `instruction_size`: read a signed 32-bit displacement from a separately reviewed instruction; instruction decoding is the agent's job |
| `find_chain` | `offsets`, `checks`, optional `section_rvas` and `limit`: search aligned slots in writable module sections. A check has `hop`, `offset` and either `equals_hop`, literal hex `bytes`, or scalar `type` plus numeric `expect`. For example `{"hop": 6, "offset": "0x20", "equals_hop": 5}` checks an owner link. Results remain candidates, even when there is only one |
| `ue_fname_16_16` | `object`, `pool`: only the explicitly selected layout with name index at +0x18, block pointers at pool+0x10, 16/16 index split, two-byte entries and header length shifted by 6. Wide, non-ASCII, control-filled or unreadable names are inconclusive. Other layouts need another reviewed reader |
| `xor` | `left`, `right`: combine named scalar results or unsigned literals; this computes a value without treating it as a pointer |
| `array` | `base`, `count` (literal or prior numeric result), optional `type`, `stride`, `start`, `limit`: read `limit` scalar elements, 128 by default and at most 512. Count is bounded at one million. Paging/truncation is explicit in `complete`, `total_count` and `start_index` |
| `ue_objects_16_16` | `array`, `pool`, optional `depth` and `limit`: survey `limit` object pointers (128 by default, at most 512) and `depth` class ancestors per object (16 by default, at most 40) using the named FName layout. Null, out-of-range or unreadable candidates, including readable data with no readable class, retain inconclusive rows. A partial array or lineage that does not reach `Object` keeps the survey incomplete. Observed names are data, not a validity verdict |

Every operation has a unique `name`. Addresses can reference `module` or a prior
scalar result. `{"array": "items", "index": 0}` addresses an explicitly read
array element by its original array index, including when `start` pages past
zero; an absent element is inconclusive. Optional `expect` compares a
literal, or `equals` compares a prior numeric result. A mismatch is an
observation, not a compatibility verdict.
Unresolved dependencies stay inconclusive. Unknown operation fields are refused
so a misspelled assertion cannot silently disappear.

The memory phase is bounded at 30 seconds and 512 MiB of actual reads, with at most
128 operations, 256 MiB of selected scan sections, and 32 MiB of selected
chain-discovery sections. Use exact `section_rvas` to narrow a large module.
Identity RPCs have separate 15-second call timeouts and executable fingerprinting
is bounded at 2 GiB; the memory deadline is not the command's total duration.
Scans preserve overlap across chunks; a reached match/candidate limit is
incomplete, never a unique answer. Discovery may use cached reads, but a matched
chain and its assertions are reread without the cache. Hop numbers are zero-based;
the first discovery hop starts at the candidate module slot. The process identity
and executable metadata are checked again after probing. Cached pages and
repeated reads still do not prove an immutable game object. Arrays are not
snapshots: combine pages only after corroborating their owning object, data
pointer and count; changing or missing pages cannot support an absence claim.
The same PID and process start time can contain replaced game objects after
gameplay. A fixed heap address from an earlier recipe can remain readable while
its expected class no longer matches. Treat dependent field readings as
inconclusive for the intended object, rediscover live targets and repeat the
class/owner checks before drawing a write/effect conclusion.

Prefer code references with a complete unique signature and reviewed signed
RIP displacement to an exhaustive heap search. Where a static module slot holds
a pointer, structural search can find aliases of the same object: two slots
with the same target are not proof which is the durable global. Check their
code references and repeat in another game state. Names, self links, owning
objects, class ancestry, sane typed ranges and prediction/authoritative mirrors
provide independent assertions. A single readable field provides none of that.

## Derive, review and test

Only propose substitutions justified by recorded observations. A full address
repair needs an inventory row for every function: dependency verified, address
unresolved, execution untested or gameplay tested. Preserve unsupported
functions explicitly; do not silently call a partial repair a fixed table.

`table_repair.py derive` takes JSON with `schema: 1`, `source_sha256` and
`substitutions`, a list of `{"old": "0x123450", "new": "0x123890", "count": 2}`.
Counts refer to the current bytes at that step; substitutions run in order.
It refuses signed tables: use the existing reviewed signature-removal path and
inventory the resulting source separately. It also refuses ambiguous IDs,
metadata/structure changes, an existing output path and a source SHA mismatch.

```bash
python3 scripts/table_repair.py derive '<verified-table>' \
  --recipe '<incident>/edits.json' --output '<new-table-path>'
```

The source stays untouched. Review the report's exact hashes, substitutions and
diff; a truncated diff needs a complete local diff before proceeding. Syntax
check Lua by loading it without calling it. Never run an extracted script on
the host merely to check its syntax. Preserve attribution and the original
table. Complex logic changes, new invariants or changed disable behavior need
their own review and regressions, beyond this literal-substitution tool.

After separating the actual Lua section from AA headers/region markers, a
syntax-only check can use this form. It loads every argument and calls none:

```bash
lua - '<extracted-Lua-section>' <<'LUA'
for _, path in ipairs(arg) do assert(loadfile(path)) end
LUA
```

Passing a script as Lua's main filename executes it; `lua -e '<check>' <file>`
also executes that file after the check. Host compilation does not validate
dynamic AA strings or prove compatibility with the installed CE Lua runtime.

Import it with the table it was made from named, so the library lists it as a
**Fixed** copy of that table rather than as **Local**:

```bash
python3 scripts/target_plugin_rpc.py import_repaired_table \
  '["<new-table-path>", "<source-table-sha>", <appid>]'
```

The source has to be a table this device holds, and the copy has to be
different bytes. Review says the change was made outside CE Decky and names
the source's cheats the copy no longer offers. Importing the same bytes again
this way, after an ordinary import, adds that record without changing the
digest. A new digest gets fresh exact-SHA review/consent; it inherits no
execution permission or cross-SHA record choices.
Stop owned CE, confirm cleanup, retire/replace the session through production,
then launch the new exact-SHA session in the validated target environment;
`target_table_switch.py --consent` does exactly that and reports each step.
Do not swap tables in a live CE process. Test one function at a time and verify
the field before activation, after activation and after deactivation. Compare
actual gameplay, exercise a weapon/level transition and a fresh game process,
and state which of those could not be tested. A passing readback cannot replace
the maintainer firing, taking damage, moving or using the ability concerned.

For an authorized mutation, use the exact-SHA control helper rather than
submitting a batch and immediately racing it with a refresh:

```bash
python3 scripts/target_runtime_control.py --appid '<id>' --table-sha '<table-sha>' \
  --commands '[{"kind":"set_active","record_id":10,"value":"1"}]' \
  --out '<incident>/activation.json'
```

The helper is explicitly mutating. It waits for all ACKs, then issues the
read-only query batch in the same session and attached PID. Use `value: "0"`
to deactivate and `set_value` for an exact reviewed parameter. A negative ACK remains a failure;
neither an ACK nor a value readback is gameplay proof. No preferences or consent
are written by this helper. Run one device RPC operation at a time, including
read-only queries that advance the command generation. A panel action can also
win that generation; a stale-generation refusal is not a table defect.

A timeout or transport failure can follow a mutation that ran. The helper's
failure report preserves its phase, exact submitted commands and any completed
mutation ACKs, including a partial batch, and distinguishes a preflight failure
from a possible mutation. Missing ACKs do not prove that their actions did not run.
Inspect current runtime status before issuing another mutation. A failed query
after a successful activation must not be retold as a failed activation or
automatically retried. Compare typed values independently: differently formatted
text values are not enough to establish either equality or failure.
An expensive activation can temporarily age the heartbeat while CE's main
thread is busy. The ACK wait tolerates that within its bound, retaining exact
session and attached-PID checks, and accepts success only from a fresh connected
answer. A readback after a parent disables its allocation can still return an
old readable value; readability does not establish the allocation's lifetime.

## Instrumentation and restoration

For a reviewed timer script, a separate diagnostic allocation and ordinary
readable records can expose ticks, validated targets, successful write calls and
errors through the existing record protocol. Keep each API wrapper local to that
script's lexical scope. Count caught callback errors as well as outer timer
errors; an outer wrapper cannot see an exception an inner `pcall` discarded.
Preserve API return semantics. For example, the reviewed CE API returns a byte
count from `writeBytes`, while integer/float writers return booleans; testing
all writers as `result == true` would create a false failure. The source used as
API evidence is the already recorded CE pin's
[LuaHandler.pas](https://github.com/cheat-engine/cheat-engine/blob/ec45d5f4/Cheat%20Engine/LuaHandler.pas)
and [celua.txt](https://github.com/cheat-engine/cheat-engine/blob/ec45d5f4/Cheat%20Engine/bin/celua.txt),
read as evidence only, with no code copied. Verify the behavior on the exact
installed CE executable; a source pin alone does not prove that binary.

A tick proves the callback ran. A successful write call proves the API accepted
the write, not its readback or intended effect. Player-pointer refreshes and
diagnostic writes are not gameplay writes. When a hook needs hit counters, keep
them inside the existing allocation and flag/register preservation; distinguish
entry, local-player, other-actor and auxiliary-path hits. Re-resolve the owned
allocation after its final user disables it; a reused absolute address has no
authority in the next activation.

Save original field values once per validated object identity and restore those
values on disable, guarded by the same class, owner, self and array links the
enable path relied on. Do not substitute an assumed default. Destructive object
replacement makes restoration inconclusive; do not write a recycled address.
When a timer encounters an address with a different class, discard the old
saved entry before considering a new write; otherwise the disable guard can
skip a newly frozen object whose write reused an older saved identity. Class
checks alone still cannot identify reuse by a new object of the same class.
A saved original is whatever the field held when the script first saw the
object. If an earlier session ended without running the disable path, that is
the cheat's own value, and restoring it leaves the effect in place. The
production stop quiesces active records before Cheat Engine exits; after a
session that ended any other way, such as Cheat Engine dying, a restore check
is inconclusive until the field is compared with a value the game itself set.
Consumable resources deliberately replenished by a cheat can remain replenished
after its timer stops, and that intended behavior differs from failing to undo
a temporary flag or code patch. Verify every overwritten instruction byte,
with expected data of exactly the overwritten length. An extra byte in the
agent's expected string is a bad assertion, not a broken restore operation.

Do not interpret no targets as a permanent mismatch. Conditional gear and mounted
weapons require the relevant equipment. Enumerate the full bounded class lineage
and required owning objects before deciding whether the layout is contradicted
or the target is simply absent. A partial object survey cannot prove absence.
For an enemy selector, independently check local player and allies as exclusions;
a class-name heuristic observed in one encounter is not a universal team test.

ESP has separate data, projection and presentation stages. Correct camera field
widths, plausible FOV, actor discovery and projected boxes do not prove visible
rendering. The attached production bridge hides enumerated CE forms; a table
that continually re-shows a form conflicts with that background-window policy.
Keep such a renderer off after a bounded diagnostic attempt. Do not disable the
policy or claim an address-only repair provides a Game Mode overlay. A new
compatible renderer is separate integration work.

## Deliver without diagnostics

Diagnostic rows, counters, wrappers and their allocation are experiment
apparatus, and the copy handed back to the maintainer carries none of them.
Build it from the same builder as the instrumented copy with diagnostics
switched off, rather than deleting them from the file by hand, and before
importing it show that:

- the builder still reproduces the instrumented copy byte for byte, so nothing
  else drifted between the two builds;
- every record other than the diagnostic rows keeps its non-script fields, and
  each script differs from the instrumented one only by removed lines;
- the removed lines are exactly the instrumentation, with no hook body, guard,
  saved-value logic or address among them.

A wrapper that changed control flow, such as one that raises on a failed write
or catches a timer's error, goes too, which returns that script to its original
failure behaviour; the report says so. Syntax check the result as above. It is
a new SHA, so it takes the ordinary import, review, exact-SHA consent and
session replacement, and its enable and disable paths are checked again under
that SHA. Gameplay evidence from the instrumented copy speaks for the logic the
diff shows unchanged, not for the new session.

## Production automation boundary

The reusable part is identity, dependency inventory, bounded observation,
candidate discovery, assertion checking, reviewable derivation and reporting.
Automatic repair can use a reviewed recipe tied to exact source/executable
digests and independent object/instruction assertions. Arbitrary Lua can encode
any algorithm, silently ignore failures and write a plausible wrong object, so
generic activation/readability cannot prove its intended effect.

For plugin diagnostics, distinguish script activation from validated targets
and user-confirmed effect. Optional per-table health assertions can report
waiting, mismatch or validated for a fresh exact session. Missing player state
must not become a permanent not-working mark. A proven mismatch or the user's
observed failed effect can supply an explicit, clearable reason for an exact
table/build; no advisory mark may block an already-held table after it is
cleared. New telemetry must use the existing log/support-bundle budgets and
regressions. This document and its CLI tools add no production health protocol,
automatic marks or address writes.
