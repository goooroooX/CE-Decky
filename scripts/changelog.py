#!/usr/bin/env python3
"""Add one entry to the top of the current version of `CHANGELOG.md`.

Every bug-fix round ends by writing entries here, and the mechanical half of
that is the same every time: find the newest version heading, and put the new
line directly above the one already under it, with no blank line between them,
because a blank line makes Markdown render the whole section as a loose list.
Done by hand, that is an anchored edit against text that changes every round,
and the failure mode is an anchor that no longer matches.

The rules it enforces are the ones `AGENTS.md` states and
`tests/test_changelog_shape.py` checks, applied here so they are met at the
moment the entry is written rather than at the next QA run:

- one of the five agreed labels;
- the version at the top matches `package.json`, so an entry cannot land under
  a heading the rest of the repository has moved past;
- no em dash, which this project does not use in its own prose;
- the entry goes above the current top entry of that version, on its own line.

Standard library only, like every other script here.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
PACKAGE = ROOT / "package.json"

HEADING = re.compile(r"^## (?P<version>\S+) — (?P<date>\d{4}-\d{2}-\d{2})$")
ALLOWED_LABELS = ("New", "Changed", "Fixed", "Security", "Limitation")
LABELLED = re.compile(r"^\[(?P<label>[A-Za-z]+)\] \S")
EM_DASH = "—"


def _current_version() -> str:
    return str(json.loads(PACKAGE.read_text(encoding="utf-8"))["version"])


def add_entry(text: str, *, lines: list[str], version: str) -> list[str]:
    """The changelog with `text` as the newest entry of the newest version."""
    # A pasted line often still carries its bullet, and the label after it is
    # right there: refusing that with "start with a label" is a lie about what
    # is wrong. Runs of whitespace collapse because this is prose on one line.
    if "\n" in text.strip():
        raise SystemExit("an entry is one line; a paragraph belongs in the documents it describes")
    entry = re.sub(r"\s+", " ", text.strip())
    if entry.startswith("- "):
        entry = entry[2:].strip()
    match = LABELLED.match(entry)
    if not match:
        raise SystemExit(
            "an entry starts with one of "
            + ", ".join(f"[{label}]" for label in ALLOWED_LABELS)
            + " and then says something"
        )
    if match.group("label") not in ALLOWED_LABELS:
        raise SystemExit(f"[{match.group('label')}] is not one of the agreed labels")
    if EM_DASH in entry:
        raise SystemExit("no em dash in prose; use a comma, a colon, a full stop or parentheses")

    headings = [index for index, line in enumerate(lines) if line.startswith("## ")]
    if not headings:
        raise SystemExit("CHANGELOG.md has no version sections")
    top = headings[0]
    heading = HEADING.match(lines[top])
    if not heading:
        raise SystemExit(f"malformed newest version heading: {lines[top]!r}")
    if heading.group("version") != version:
        raise SystemExit(
            f"the newest changelog heading is {heading.group('version')} and package.json says "
            f"{version}; synchronize the version before writing an entry for it"
        )

    end = headings[1] if len(headings) > 1 else len(lines)
    # Directly above the entry already at the top of this version, so the list
    # stays one unbroken run. A version whose entries have not started yet takes
    # the line straight after its heading's blank line.
    at = next(
        (index for index in range(top + 1, end) if lines[index].startswith("- ")),
        None,
    )
    if at is None:
        # No entries yet, which is a version heading written a moment ago. After
        # the blank line the heading carries, however many there are, rather
        # than at a position assumed from the shape it usually has.
        at = top + 1
        while at < end and not lines[at].strip():
            at += 1
    addition = [f"- {entry}"]
    # A heading written a moment ago carries its blank line above the heading
    # under it rather than under its own entries, so the first entry written
    # into it lands directly on that next heading. Markdown needs the blank
    # line: without it the two sections run together, which is the same defect
    # as a blank line between two entries and just as invisible until the
    # rendered page is read.
    if at < len(lines) and lines[at].startswith("## "):
        addition.append("")
    return lines[:at] + addition + lines[at:]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    adder = subcommands.add_parser("add", help="add one entry to the current version")
    adder.add_argument("entry", help='the entry, starting with its label: "[Fixed] ..."')
    arguments = parser.parse_args()

    version = _current_version()
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    updated = add_entry(arguments.entry, lines=lines, version=version)
    CHANGELOG.write_text("\n".join(updated) + "\n", encoding="utf-8")
    print(f"changelog: one entry added to {version}")


if __name__ == "__main__":
    main()
