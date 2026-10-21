#!/usr/bin/env python3
"""What a table scans for, whether a game's program holds it, and what it switches on by itself.

Three questions, one reader. Over a directory of `.CT` files it reports what the
production parser read out of them and what it refused, which is how the shapes
real tables carry are measured rather than guessed: the corpus this project
keeps writes the same byte pattern several different ways, comments scans out,
and calls Cheat Engine's Lua scanner with names that look like the Auto
Assembler directives and are not. Against one table and one executable it runs
the check the panel runs and prints what it found, with what it cost.

    python3 scripts/ct_scan_survey.py --tables build/corpus/store/sha256
    python3 scripts/ct_scan_survey.py --table <file.CT> --executable <game.exe>
    python3 scripts/ct_scan_survey.py --table <file.CT> --executable <game.exe> --repeat 3
    python3 scripts/ct_scan_survey.py --tables <dir> --json
    python3 scripts/ct_scan_survey.py --tables <dir> --repair-coverage
    python3 scripts/ct_scan_survey.py --table <file.CT> --flags
    python3 scripts/ct_scan_survey.py --table-sha <first 8+ hex of its SHA-256> --scripts
    python3 scripts/ct_scan_survey.py --tables <dir> --flags

It reads and nothing else: no table is stored, no game is started, and the
executable is opened through the same bounded reader the product uses. The
number it prints for a check is this machine's, so report it with the machine
and the file's size beside it.
"""

from __future__ import annotations

import argparse
import collections
from hashlib import sha256
import json
from pathlib import Path
import platform
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

from ce_decky.ct_inspector import inspect_table  # noqa: E402
from ce_decky.ct_scans import (  # noqa: E402
    ScanParseError,
    assert_only_scans_dropped,
    check_executable,
    compile_pattern,
    drop_unmatched_scans,
    executable_is_packed,
    read_scans,
)

# The reader's own line-by-line refusal, which is what the `--tables` mode is
# about: the reason alone is what the product wants from one, and a report
# wants the line that produced it.
from ce_decky.ct_scans import _scan_from_line  # noqa: E402

# What one table may hold before this stops reading it, which is the bound the
# production store works under rather than a choice made here.
MAX_TABLE_BYTES = 32 * 1024 * 1024

# How many of the flags a table leaves switched on this names. The count is the
# answer; the names are there to be recognised in the picker, and a table with
# dozens of them would otherwise print a page of them.
MAX_NAMED_FLAGS = 40

# What the store refuses outright, for the same reason it does: a table is
# untrusted XML, and an entity declaration is how one makes a reader expand
# itself to death. This helper is pointed at whatever a developer downloaded.
_FORBIDDEN_XML = (b"<!DOCTYPE", b"<!ENTITY")


def _scripts(path: Path) -> list[str]:
    """Every Auto Assembler script one table carries, in document order."""
    if path.stat().st_size > MAX_TABLE_BYTES:
        raise ValueError("that table is larger than one this reads")
    data = path.read_bytes()
    probe = data.upper()
    if any(marker in probe for marker in _FORBIDDEN_XML):
        raise ValueError("that table declares a DTD or an entity, which this does not expand")
    root = ET.fromstring(data)
    return [
        element.text for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "AssemblerScript" and element.text
    ]


def _table_scans(path: Path) -> list:
    """The scans one whole table declares, with a name read once."""
    return _table_scans_and_repeats(path)[0]


def _table_scans_and_repeats(path: Path) -> tuple[list, list[str]]:
    """The same list, and the symbols the table uses for more than one pattern.

    The product reads a symbol once per table and answers for none that stands
    for different code in different scripts, so which tables carry that shape is
    a question a report has to be able to ask.
    """
    found = []
    first: dict[str, object] = {}
    repeated: set[str] = set()
    for script in _scripts(path):
        for scan in read_scans(script)[0]:
            key = scan.name.casefold()
            was = first.get(key)
            if was is None:
                first[key] = scan
                found.append(scan)
            elif (was.directive, was.module, was.pattern) != (scan.directive, scan.module, scan.pattern):
                repeated.add(scan.name)
    return found, sorted(repeated)


def _example(script: str, reason: str) -> str:
    """One line that produced this reason, so a refusal can be looked at.

    Found by reading the script again rather than carried out of the parser:
    what a report wants is the line, and what the product wants from a refusal
    is the reason alone.
    """
    for line in script.splitlines():
        try:
            _scan_from_line(line)
        except ScanParseError as exc:
            if str(exc) == reason:
                return line.strip()[:160]
    return ""


def survey_tables(root: Path) -> dict[str, object]:
    """What the parser read out of a directory of tables, and what it refused."""
    tables = scripts = scans = 0
    with_scans = 0
    no_fast_path = 0
    refusals: collections.Counter[str] = collections.Counter()
    examples: dict[str, str] = {}
    lengths: collections.Counter[int] = collections.Counter()
    per_script: collections.Counter[int] = collections.Counter()
    # Which of Cheat Engine's three scanners each pattern is looked for with.
    # Only one of them names a file, and that is the only one a file on disk can
    # answer for, so how much of a corpus it covers is a number worth having
    # rather than assuming.
    directives: collections.Counter[str] = collections.Counter()
    # Symbols one table uses for more than one pattern. The product answers for
    # none of them, so how common that shape is decides what that costs.
    repeated_symbols = repeated_tables = 0
    unreadable = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".ct":
            continue
        tables += 1
        try:
            found_scripts = _scripts(path)
        except (OSError, ValueError, ET.ParseError):
            unreadable += 1
            continue
        table_scans = 0
        for script in found_scripts:
            scripts += 1
            found, refused = read_scans(script)
            per_script[len(found)] += 1
            table_scans += len(found)
            for scan in found:
                scans += 1
                directives[scan.directive] += 1
                lengths[len(scan.pattern.split())] += 1
                if compile_pattern(scan.pattern) is None:
                    no_fast_path += 1
            for reason in refused:
                refusals[reason] += 1
                examples.setdefault(reason, _example(script, reason))
        if table_scans:
            with_scans += 1
        try:
            repeats = _table_scans_and_repeats(path)[1]
        except (OSError, ValueError, ET.ParseError):
            repeats = []
        if repeats:
            repeated_tables += 1
            repeated_symbols += len(repeats)
    ordered = sorted(lengths.elements())
    return {
        "tables": tables,
        "unreadable": unreadable,
        "tables_with_scans": with_scans,
        "scripts": scripts,
        "scans": scans,
        "scans_with_no_fast_path": no_fast_path,
        "scans_by_directive": dict(sorted(directives.items())),
        "repeated_symbols": repeated_symbols,
        "tables_with_a_repeated_symbol": repeated_tables,
        "scans_per_script": dict(sorted(per_script.items())),
        "pattern_bytes": {
            "min": ordered[0] if ordered else 0,
            "median": ordered[len(ordered) // 2] if ordered else 0,
            "max": ordered[-1] if ordered else 0,
        },
        "refused": [
            {"reason": reason, "count": count, "example": examples[reason]}
            for reason, count in refusals.most_common()
        ],
    }


def check_one(table: Path, executable: Path, repeat: int) -> dict[str, object]:
    """The check the panel runs, on one table against one program."""
    scans, repeated = _table_scans_and_repeats(table)
    runs = []
    result = None
    for _ in range(max(1, repeat)):
        started = time.monotonic()
        result = check_executable(executable, scans)
        runs.append(round(time.monotonic() - started, 3))
    assert result is not None
    return {
        "host": f"{platform.system()} {platform.machine()}",
        "table": table.name,
        "executable": executable.name,
        "executable_bytes": executable.stat().st_size,
        "wrapper": executable_is_packed(executable),
        "scans": len(scans),
        # Symbols this table uses for more than one pattern. The product answers
        # for none of them, because which occurrence a cheat uses is not
        # something a name says.
        "repeated_symbols": repeated,
        "seconds": runs,
        **result.as_dict(),
    }


def repair_coverage(root: Path) -> dict[str, object]:
    """How much of a corpus the repair could produce a proven table for.

    One scan at a time, over every table: the transform runs, both proofs run,
    and the bytes are thrown away. A scan counts as repairable when a table with
    that one pattern missing is one this would store.

    Counted by directive as well as in total, because only one of the three says
    where it searches. `aobscan` and `aobscanregion` look in more of the running
    game than any file on disk holds, so the product never removes one on the
    strength of a file: what they contribute here is the difference between what
    the transform can do and what a user can actually be offered.
    """
    tables = unreadable = scans = repairable = 0
    tables_with_repair = tables_fully = 0
    tables_with_offer = tables_fully_offered = 0
    by_directive: collections.Counter[str] = collections.Counter()
    repairable_by_directive: collections.Counter[str] = collections.Counter()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".ct":
            continue
        tables += 1
        try:
            found = _table_scans(path)
            blob = path.read_bytes()
        except (OSError, ValueError, ET.ParseError):
            unreadable += 1
            continue
        if not found:
            continue
        here = offered = 0
        for scan in found:
            scans += 1
            by_directive[scan.directive] += 1
            try:
                derived, _repair = drop_unmatched_scans(blob, [scan.name])
                assert_only_scans_dropped(blob, derived, [scan.name])
            except (ValueError, ET.ParseError):
                # A proof that refused, which is the ordinary answer and the
                # reason this counts rather than lists.
                continue
            repairable += 1
            repairable_by_directive[scan.directive] += 1
            here += 1
            if scan.directive == "aobscanmodule" and scan.module:
                offered += 1
        if here:
            tables_with_repair += 1
            if here == len(found):
                tables_fully += 1
        if offered:
            tables_with_offer += 1
            # Every scan this table makes, and every one of them looked for in a
            # file this could read: the only shape where the whole table is
            # something a user can be offered a working copy of.
            if offered == len(found):
                tables_fully_offered += 1
    return {
        "tables": tables,
        "unreadable": unreadable,
        "scans": scans,
        "repairable": repairable,
        "scans_by_directive": dict(sorted(by_directive.items())),
        "repairable_by_directive": dict(sorted(repairable_by_directive.items())),
        "tables_with_a_repairable_scan": tables_with_repair,
        "tables_fully_repairable": tables_fully,
        "tables_a_repair_can_be_offered_for": tables_with_offer,
        "tables_fully_offerable": tables_fully_offered,
    }


def repair_one(table: Path, scans: list[str]) -> dict[str, object]:
    """What dropping those scans from one table would take out, proved.

    Read only: the repaired bytes are produced, proved and thrown away. What
    comes back is what the repair costs - how many blocks and lines went, how
    many bytes, and which cheats lost the address they were reached by - which
    is what decides whether a repair is worth offering at all.
    """
    blob = table.read_bytes()
    derived, repair = drop_unmatched_scans(blob, scans)
    assert_only_scans_dropped(blob, derived, scans)
    return {"table": table.name, "proved": True, **repair.as_dict()}


def flags_one(table: Path) -> dict[str, object]:
    """Which of one table's own flags it switches on, and which may be written off.

    Read through the production inspector rather than out of the scripts here,
    because what the product acts on is the record's own answer: a flag is a
    record whose address its enclosing script allocates and whose declared
    default is that script's on key, and whether it may be written off is what
    `ct_hooks` established from the code that reads it.

    The list that matters is the refused one. Those are the cheats CE Decky
    leaves switched on when it starts a script for something else, and the
    reason a number in a field note is a number somebody can reproduce.
    """
    data = table.read_bytes()
    if len(data) > MAX_TABLE_BYTES:
        raise ValueError("that table is larger than one this reads")
    found = inspect_table(table, sha256(data).hexdigest())
    declared = [
        control for control in found.controls
        if control.declared_default is not None and control.declared_default == control.switch_on_value
    ]
    safe = [control for control in declared if control.switch_off_is_safe is True]
    refused = [control for control in declared if control.switch_off_is_safe is not True]
    return {
        "table": table.name,
        "sha256": found.sha256,
        "controls": len(found.controls),
        "switched_on_by_the_table": len(declared),
        "safe_to_switch_off": len(safe),
        "left_on": [" > ".join(control.path) for control in refused[:MAX_NAMED_FLAGS]],
        "left_on_count": len(refused),
    }


def flags_corpus(root: Path) -> dict[str, object]:
    """The same question over a directory, which is how a shape is measured.

    One row per table would be a report nobody reads; what a corpus answers is
    how common the shape is and how much of it this reader can follow.
    """
    tables = 0
    unreadable = 0
    with_flags = 0
    declared = 0
    safe = 0
    fully_readable = 0
    for path in sorted(root.rglob("*")):
        # The same walk the scan survey makes: a directory of tables holds the
        # store's own files beside them, and one that is not a table is not a
        # table this could not read.
        if not path.is_file() or path.suffix.lower() != ".ct":
            continue
        tables += 1
        try:
            answer = flags_one(path)
        except (OSError, ValueError, ET.ParseError):
            unreadable += 1
            continue
        declared += int(answer["switched_on_by_the_table"])
        safe += int(answer["safe_to_switch_off"])
        if int(answer["switched_on_by_the_table"]) > 0:
            with_flags += 1
            if int(answer["left_on_count"]) == 0:
                fully_readable += 1
    return {
        "tables": tables,
        "unreadable": unreadable,
        "tables_that_switch_something_on": with_flags,
        "tables_whose_flags_are_all_readable": fully_readable,
        "switched_on_by_the_table": declared,
        "safe_to_switch_off": safe,
    }


# How many examples of each shape `--scripts` names. They are there to be
# recognised, so a handful per shape is the answer and a page of them is not.
MAX_SHAPE_EXAMPLES = 6

# The four shapes an enclosing record takes, by where it sits and what it holds.
_SHAPES = ("outermost_values_only", "outermost_holds_scripts", "nested_values_only", "nested_holds_scripts")


def _listed_controls(found) -> list:
    """The controls Configure cheats lists, by most of `safeActionableControls`.

    Group headers and records whose ID the table repeats are never listed, so
    they are neither scripts nor cheats to that screen and are left out here.
    Its other exclusion, a record under a script whose ID the table repeats, is
    not repeated, so a table holding one counts a few records the screen omits.
    """
    ambiguous = set(found.ambiguous_record_ids)
    return [
        control for control in found.controls
        if control.id is not None and control.kind != "group" and not control.group_header
        and control.id not in ambiguous
    ]


def scripts_one(table: Path) -> dict[str, object]:
    """Every record that encloses others in one table, sorted into four shapes.

    Configure cheats lists a record as a script, behind **Scripts**, when other
    records sit inside it, which is `enclosingControlIds` in `src/uiModel.ts`
    and is repeated here. That puts a cheat with its settings under it beside
    the machinery that installs a table's hooks, and a table does not say which
    is which. What can be read is where a record sits - whether another
    enclosing record is above it - and what it holds - a script somewhere
    inside it, or none - and those are the four shapes counted here.
    """
    data = table.read_bytes()
    if len(data) > MAX_TABLE_BYTES:
        raise ValueError("that table is larger than one this reads")
    found = inspect_table(table, sha256(data).hexdigest())
    listed = _listed_controls(found)
    prefixes = {
        tuple(control.structure[:depth])
        for control in listed for depth in range(1, len(control.structure))
    }
    enclosing = [control for control in listed if tuple(control.structure) in prefixes]

    def inside(outer, inner) -> bool:
        return len(outer.structure) < len(inner.structure) and tuple(inner.structure[:len(outer.structure)]) == tuple(outer.structure)

    shapes: dict[str, list[dict[str, object]]] = {shape: [] for shape in _SHAPES}
    for control in enclosing:
        outermost = not any(inside(other, control) for other in enclosing if other is not control)
        # A script anywhere inside it, leaf or not, which is the test
        # `scriptListedControlIds` makes: a record holding values that hold
        # values is not a script holding scripts.
        holds_scripts = any(other.kind == "script" and inside(control, other) for other in listed if other is not control)
        held = sum(1 for other in listed if inside(control, other))
        shape = ("outermost" if outermost else "nested") + ("_holds_scripts" if holds_scripts else "_values_only")
        shapes[shape].append({"name": " > ".join(control.path), "holds": held, "kind": control.kind})
    return {
        "table": table.name,
        "sha256": found.sha256,
        "listed": len(listed),
        "enclosing": len(enclosing),
        "shapes": shapes,
    }


def scripts_corpus(root: Path) -> dict[str, object]:
    """The same shapes over a directory, and what three hiding rules would do.

    `everything` hides every enclosing record, which is what Configure cheats
    did before 0.9.31. `outermost` hides only those with no enclosing record
    above them. `scripts_holding_scripts` is what `scriptListedControlIds`
    hides now: an Auto Assembler script with another script anywhere inside
    it. For each, how many records are hidden and how many enclosing records
    are shown.
    """
    tables = 0
    unreadable = 0
    listed = 0
    counts = {shape: 0 for shape in _SHAPES}
    tables_with = {shape: 0 for shape in _SHAPES}
    kinds: dict[str, dict[str, int]] = {shape: {} for shape in _SHAPES}
    script_holders = 0
    examples: dict[str, list[str]] = {shape: [] for shape in _SHAPES}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".ct":
            continue
        tables += 1
        try:
            answer = scripts_one(path)
        except (OSError, ValueError, ET.ParseError):
            unreadable += 1
            continue
        listed += int(answer["listed"])
        for shape, rows in answer["shapes"].items():
            counts[shape] += len(rows)
            if rows:
                tables_with[shape] += 1
            for row in rows:
                kinds[shape][str(row["kind"])] = kinds[shape].get(str(row["kind"]), 0) + 1
                if shape.endswith("_holds_scripts") and row["kind"] == "script":
                    script_holders += 1
                if len(examples[shape]) < MAX_SHAPE_EXAMPLES:
                    examples[shape].append(f"{row['name']} ({row['kind']}, holds {row['holds']}) in {path.name[:12]}")
    hidden_by = {
        "everything": sum(counts.values()),
        "outermost": counts["outermost_values_only"] + counts["outermost_holds_scripts"],
        "scripts_holding_scripts": script_holders,
    }
    return {
        "tables": tables,
        "unreadable": unreadable,
        "listed": listed,
        "shapes": counts,
        "tables_with_shape": tables_with,
        "kinds": kinds,
        "examples": examples,
        "hidden_by_rule": hidden_by,
        "enclosing_shown_by_rule": {rule: sum(counts.values()) - hidden for rule, hidden in hidden_by.items()},
    }


def table_by_sha(prefix: str, store: Path | None) -> Path:
    """The stored table whose SHA-256 begins with this, verified in full.

    A table on the device is named by its digest everywhere this project talks
    about it, and finding its file meant listing the store by hand. This reads
    the same content-addressed layout the plugin writes, refuses a prefix that
    names no table or more than one, and hands back the path only after
    `TableStore.verified_blob` has checked every byte against the digest.
    """
    wanted = prefix.strip().lower()
    if len(wanted) < 8 or any(ch not in "0123456789abcdef" for ch in wanted):
        raise ValueError("--table-sha takes at least eight hexadecimal characters of a table's SHA-256")
    if store is None:
        # The store is under the Steam user's home, which only the live plugin
        # or its verified install record can say: never a guessed home.
        sys.path.insert(0, str(ROOT / "scripts"))
        from target_plugin_install import resolve_install_authority  # noqa: PLC0415
        home = resolve_install_authority().get("user_home")
        if not home:
            raise ValueError("the install authority reports no user_home; pass --store")
        store = Path(str(home)) / ".cheat-engine-decky" / "tables"
    shard = store / "sha256" / wanted[:2]
    matches = sorted(item.name for item in shard.glob(f"{wanted}*")) if shard.is_dir() else []
    if not matches:
        raise ValueError(f"no stored table's SHA-256 begins with {wanted} in {store}")
    if len(matches) > 1:
        raise ValueError(f"{wanted} begins {len(matches)} stored tables: {', '.join(item[:16] for item in matches)}")
    from ce_decky.table_store import TableStore  # noqa: PLC0415
    return TableStore(store).verified_blob(matches[0])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tables", type=Path, help="a directory of .CT files to read patterns out of")
    parser.add_argument("--table", type=Path, help="one table to check")
    parser.add_argument(
        "--table-sha", metavar="PREFIX",
        help="one table from the plugin's own store, by the start of its SHA-256, in place of --table",
    )
    parser.add_argument(
        "--store", type=Path,
        help="the table store --table-sha reads; omitted, the one under user_home from the install authority",
    )
    parser.add_argument("--executable", type=Path, help="the game program to look for its patterns in")
    parser.add_argument("--repeat", type=int, default=1, help="run the check this many times and report each")
    parser.add_argument(
        "--repair", metavar="SCAN", action="append", default=[],
        help="drop this scan from --table and report what it costs; repeat for several",
    )
    parser.add_argument(
        "--repair-coverage", action="store_true",
        help="with --tables: how many of the corpus's scans the repair can produce a proven table for",
    )
    parser.add_argument(
        "--flags", action="store_true",
        help="which flags the table switches on by itself, and which of them may be switched off",
    )
    parser.add_argument(
        "--scripts", action="store_true",
        help="the records Configure cheats lists as scripts, by where they sit and what they hold",
    )
    parser.add_argument("--json", action="store_true", help="the same answer, for a report")
    args = parser.parse_args()

    if args.table is not None and args.table_sha is not None:
        parser.error("name one table: --table <file.CT> or --table-sha <prefix>")
    if args.table_sha is not None:
        try:
            args.table = table_by_sha(args.table_sha, args.store)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"ct scan survey: {exc}", file=sys.stderr)
            return 1
    if args.tables is None and args.table is None:
        parser.error("name --tables <dir>, or --table <file.CT> with --executable <game.exe>")
    answer: dict[str, object] = {}
    try:
        if args.tables is not None and args.scripts:
            answer["scripts_corpus"] = scripts_corpus(args.tables)
        elif args.tables is not None and args.flags:
            answer["flags_corpus"] = flags_corpus(args.tables)
        elif args.tables is not None and args.repair_coverage:
            answer["repair_coverage"] = repair_coverage(args.tables)
        elif args.tables is not None:
            answer["survey"] = survey_tables(args.tables)
        if args.table is not None and args.scripts:
            answer["scripts"] = scripts_one(args.table)
        if args.table is not None and args.flags:
            answer["flags"] = flags_one(args.table)
        if args.table is not None and args.repair:
            answer["repair"] = repair_one(args.table, list(args.repair))
        if args.table is not None and not args.repair and not args.flags and not args.scripts:
            if args.executable is None:
                parser.error("--table needs --executable, which is the program to look in")
            answer["check"] = check_one(args.table, args.executable, args.repeat)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"ct scan survey: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(answer, indent=2, sort_keys=True))
        return 0
    coverage = answer.get("repair_coverage")
    if isinstance(coverage, dict):
        print(f"tables {coverage['tables']} ({coverage['unreadable']} unreadable), scans {coverage['scans']}")
        print(f"  repairable {coverage['repairable']}  by directive {coverage['repairable_by_directive']}")
        print(f"  of {coverage['scans_by_directive']}")
        print(f"  tables with one {coverage['tables_with_a_repairable_scan']}, "
              f"fully {coverage['tables_fully_repairable']}")
        print(f"  offerable: tables {coverage['tables_a_repair_can_be_offered_for']}, "
              f"fully {coverage['tables_fully_offerable']}")
    survey = answer.get("survey")
    if isinstance(survey, dict):
        print(f"tables {survey['tables']} ({survey['unreadable']} unreadable), "
              f"with scans {survey['tables_with_scans']}, scripts {survey['scripts']}, scans {survey['scans']}")
        print(f"  no fast path: {survey['scans_with_no_fast_path']}   pattern bytes: {survey['pattern_bytes']}")
        print(f"  by directive: {survey['scans_by_directive']}")
        print(f"  symbols used for more than one pattern: {survey['repeated_symbols']} "
              f"in {survey['tables_with_a_repeated_symbol']} tables")
        for row in survey["refused"]:
            print(f"  refused {row['count']:4}  {row['reason']}")
            print(f"           {row['example']}")
    repaired = answer.get("repair")
    if isinstance(repaired, dict):
        print(f"{repaired['table']}: dropping {', '.join(repaired['scans'])} removes "
              f"{repaired['blocks']} blocks, {repaired['lines']} lines, {repaired['bytes_removed']} bytes")
        print(f"  cheats that lose their address: {repaired['orphaned'] or 'none'}")
    flags = answer.get("flags")
    if isinstance(flags, dict):
        print(f"{flags['table']} ({flags['sha256'][:12]}): {flags['controls']} controls, "
              f"{flags['switched_on_by_the_table']} switched on by the table")
        print(f"  safe to switch off {flags['safe_to_switch_off']}  left on {flags['left_on_count']}")
        for name in flags["left_on"]:
            print(f"    left on: {name}")
        if int(flags["left_on_count"]) > len(flags["left_on"]):
            print(f"    and {int(flags['left_on_count']) - len(flags['left_on'])} more")
    corpus = answer.get("flags_corpus")
    if isinstance(corpus, dict):
        print(f"tables {corpus['tables']} ({corpus['unreadable']} unreadable), "
              f"switching something on {corpus['tables_that_switch_something_on']}, "
              f"all flags readable {corpus['tables_whose_flags_are_all_readable']}")
        print(f"  switched on by the table {corpus['switched_on_by_the_table']}, "
              f"safe to switch off {corpus['safe_to_switch_off']}")
    one = answer.get("scripts")
    if isinstance(one, dict):
        print(f"{one['table']} ({one['sha256'][:12]}): {one['listed']} listed, {one['enclosing']} enclose others")
        for shape, rows in one["shapes"].items():
            print(f"  {shape}: {len(rows)}")
            for row in rows[:MAX_SHAPE_EXAMPLES]:
                print(f"    {row['name']} ({row['kind']}, holds {row['holds']})")
    scripts = answer.get("scripts_corpus")
    if isinstance(scripts, dict):
        print(f"tables {scripts['tables']} ({scripts['unreadable']} unreadable), listed {scripts['listed']}")
        for shape in _SHAPES:
            kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(scripts["kinds"][shape].items()))
            print(f"  {shape}: {scripts['shapes'][shape]} in {scripts['tables_with_shape'][shape]} tables ({kinds})")
            for example in scripts["examples"][shape]:
                print(f"    {example}")
        for rule, hidden in scripts["hidden_by_rule"].items():
            print(f"  rule {rule}: hides {hidden}, shows {scripts['enclosing_shown_by_rule'][rule]} enclosing records")
    check = answer.get("check")
    if isinstance(check, dict):
        megabytes = int(check["executable_bytes"]) // (1024 * 1024)
        print(f"{check['table']} against {check['executable']} ({megabytes} MiB), "
              f"{check['scans']} scans, wrapper {check['wrapper']}, on {check['host']}")
        print(f"  seconds {check['seconds']}")
        print(f"  present {len(check['present'])}  missing {check['missing']}")
        print(f"  repeated symbols: {check['repeated_symbols'] or 'none'}")
        for row in check["not_checked"]:
            print(f"  not checked: {row['name']} - {row['reason']}")
        if check["reason"]:
            print(f"  not run: {check['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
