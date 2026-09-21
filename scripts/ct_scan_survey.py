#!/usr/bin/env python3
"""What a table scans for, and whether a game's program holds it.

Two questions, one reader. Over a directory of `.CT` files it reports what the
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

It reads and nothing else: no table is stored, no game is started, and the
executable is opened through the same bounded reader the product uses. The
number it prints for a check is this machine's, so report it with the machine
and the file's size beside it.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import platform
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tables", type=Path, help="a directory of .CT files to read patterns out of")
    parser.add_argument("--table", type=Path, help="one table to check")
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
    parser.add_argument("--json", action="store_true", help="the same answer, for a report")
    args = parser.parse_args()

    if args.tables is None and args.table is None:
        parser.error("name --tables <dir>, or --table <file.CT> with --executable <game.exe>")
    answer: dict[str, object] = {}
    try:
        if args.tables is not None and args.repair_coverage:
            answer["repair_coverage"] = repair_coverage(args.tables)
        elif args.tables is not None:
            answer["survey"] = survey_tables(args.tables)
        if args.table is not None and args.repair:
            answer["repair"] = repair_one(args.table, list(args.repair))
        if args.table is not None and not args.repair:
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
