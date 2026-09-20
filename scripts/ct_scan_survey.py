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
    check_executable,
    compile_pattern,
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
    found = []
    seen: set[str] = set()
    for script in _scripts(path):
        for scan in read_scans(script)[0]:
            if scan.name.casefold() in seen:
                continue
            seen.add(scan.name.casefold())
            found.append(scan)
    return found


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
                lengths[len(scan.pattern.split())] += 1
                if compile_pattern(scan.pattern) is None:
                    no_fast_path += 1
            for reason in refused:
                refusals[reason] += 1
                examples.setdefault(reason, _example(script, reason))
        if table_scans:
            with_scans += 1
    ordered = sorted(lengths.elements())
    return {
        "tables": tables,
        "unreadable": unreadable,
        "tables_with_scans": with_scans,
        "scripts": scripts,
        "scans": scans,
        "scans_with_no_fast_path": no_fast_path,
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
    scans = _table_scans(table)
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
        "seconds": runs,
        **result.as_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tables", type=Path, help="a directory of .CT files to read patterns out of")
    parser.add_argument("--table", type=Path, help="one table to check")
    parser.add_argument("--executable", type=Path, help="the game program to look for its patterns in")
    parser.add_argument("--repeat", type=int, default=1, help="run the check this many times and report each")
    parser.add_argument("--json", action="store_true", help="the same answer, for a report")
    args = parser.parse_args()

    if args.tables is None and args.table is None:
        parser.error("name --tables <dir>, or --table <file.CT> with --executable <game.exe>")
    answer: dict[str, object] = {}
    try:
        if args.tables is not None:
            answer["survey"] = survey_tables(args.tables)
        if args.table is not None:
            if args.executable is None:
                parser.error("--table needs --executable, which is the program to look in")
            answer["check"] = check_one(args.table, args.executable, args.repeat)
    except (OSError, ValueError, ET.ParseError) as exc:
        print(f"ct scan survey: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(answer, indent=2, sort_keys=True))
        return 0
    survey = answer.get("survey")
    if isinstance(survey, dict):
        print(f"tables {survey['tables']} ({survey['unreadable']} unreadable), "
              f"with scans {survey['tables_with_scans']}, scripts {survey['scripts']}, scans {survey['scans']}")
        print(f"  no fast path: {survey['scans_with_no_fast_path']}   pattern bytes: {survey['pattern_bytes']}")
        for row in survey["refused"]:
            print(f"  refused {row['count']:4}  {row['reason']}")
            print(f"           {row['example']}")
    check = answer.get("check")
    if isinstance(check, dict):
        megabytes = int(check["executable_bytes"]) // (1024 * 1024)
        print(f"{check['table']} against {check['executable']} ({megabytes} MiB), "
              f"{check['scans']} scans, wrapper {check['wrapper']}, on {check['host']}")
        print(f"  seconds {check['seconds']}")
        print(f"  present {len(check['present'])}  missing {check['missing']}")
        for row in check["not_checked"]:
            print(f"  not checked: {row['name']} - {row['reason']}")
        if check["reason"]:
            print(f"  not run: {check['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
