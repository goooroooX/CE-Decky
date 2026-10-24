#!/usr/bin/env python3
"""Add one entry to the top of the current version of `CHANGELOG.md`, move its date, or start the next version.

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

`date` moves the current version's date, which `AGENTS.md` and the newest
changelog heading both carry and the release check holds equal. Changing one by
hand and forgetting the other is the failure it removes; it refuses a version
the two files and `package.json` do not already agree on, because a date move
is not how a version is changed.

`version` is how a version is changed. It is written in four places, which
`scripts/check_release.py` holds equal: `package.json`, the backend's
`__version__`, `AGENTS.md` and a new changelog heading, which gets no entries
until the first change on it is written there. The next version is the current
one with its last component advanced, because before 1.0 nothing else moves; a
version is named only for the step the maintainer takes on purpose, such as
1.0.0. It refuses four places that do not already agree, a version that is
not after the current one, a second component moved before 1.0, and a current
version with no entries, which is the one a change belongs under.

Standard library only, like every other script here.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"
PACKAGE = ROOT / "package.json"
AGENTS = ROOT / "AGENTS.md"
BACKEND_INIT = ROOT / "py_modules" / "ce_decky" / "__init__.py"

HEADING = re.compile(r"^## (?P<version>\S+) — (?P<date>\d{4}-\d{2}-\d{2})$")
ALLOWED_LABELS = ("New", "Changed", "Fixed", "Security", "Limitation")
LABELLED = re.compile(r"^\[(?P<label>[A-Za-z]+)\] \S")
EM_DASH = "—"
AGENTS_VERSION = re.compile(r"^Current development version: \*\*(?P<version>\S+) — (?P<date>\d{4}-\d{2}-\d{2})\*\*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
PACKAGE_VERSION = re.compile(r'^(?P<lead>\s*"version":\s*")(?P<version>[^"]+)(?P<tail>",?)$', re.MULTILINE)
INIT_VERSION = re.compile(r'^(?P<lead>__version__\s*=\s*")(?P<version>[^"]+)(?P<tail>")$', re.MULTILINE)


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


def set_date(
    date: str, *, changelog: list[str], agents: list[str], version: str,
) -> tuple[list[str], list[str]]:
    """Both files with the current version's date set to `date`."""
    _checked_date(date)
    top = next((index for index, line in enumerate(changelog) if line.startswith("## ")), None)
    heading = HEADING.match(changelog[top]) if top is not None else None
    if heading is None:
        raise SystemExit("CHANGELOG.md has no well-formed newest version heading")
    line = next((index for index, text in enumerate(agents) if AGENTS_VERSION.match(text)), None)
    if line is None:
        raise SystemExit("AGENTS.md has no `Current development version` line")
    stated = AGENTS_VERSION.match(agents[line])
    versions = {"package.json": version, "CHANGELOG.md": heading.group("version"), "AGENTS.md": stated.group("version")}
    if len(set(versions.values())) != 1:
        raise SystemExit(
            "the version is not synchronized ("
            + ", ".join(f"{name} {value}" for name, value in versions.items())
            + "); synchronize it before moving its date"
        )
    changelog = list(changelog)
    agents = list(agents)
    changelog[top] = f"## {version} {EM_DASH} {date}"
    agents[line] = f"Current development version: **{version} {EM_DASH} {date}**"
    return changelog, agents


def _checked_date(date: str) -> str:
    if not DATE.match(date):
        raise SystemExit(f"a date is YYYY-MM-DD, not {date!r}")
    try:
        datetime.date.fromisoformat(date)
    except ValueError as error:
        raise SystemExit(f"{date} is not a calendar date: {error}") from None
    return date


def _version_parts(version: str) -> tuple[int, int, int]:
    match = VERSION.match(version)
    if not match:
        raise SystemExit(f"a version is MAJOR.MINOR.PATCH, not {version!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def start_version(
    new: str | None, date: str, *, changelog: list[str], agents: list[str], package: str, backend_init: str,
) -> tuple[str, list[str], list[str], str, str]:
    """The next version, and all four places with it written in.

    Returns the version and the changelog, `AGENTS.md`, `package.json` and
    backend `__init__.py` as they should now read.
    """
    _checked_date(date)
    top = next((index for index, line in enumerate(changelog) if line.startswith("## ")), None)
    heading = HEADING.match(changelog[top]) if top is not None else None
    if heading is None:
        raise SystemExit("CHANGELOG.md has no well-formed newest version heading")
    line = next((index for index, text in enumerate(agents) if AGENTS_VERSION.match(text)), None)
    if line is None:
        raise SystemExit("AGENTS.md has no `Current development version` line")
    package_matches = list(PACKAGE_VERSION.finditer(package))
    init_matches = list(INIT_VERSION.finditer(backend_init))
    if len(package_matches) != 1 or len(init_matches) != 1:
        raise SystemExit("package.json and the backend __init__.py each need exactly one version line")
    versions = {
        "package.json": package_matches[0].group("version"),
        "__init__.py": init_matches[0].group("version"),
        "CHANGELOG.md": heading.group("version"),
        "AGENTS.md": AGENTS_VERSION.match(agents[line]).group("version"),
    }
    if len(set(versions.values())) != 1:
        raise SystemExit(
            "the version is not synchronized ("
            + ", ".join(f"{name} {value}" for name, value in versions.items())
            + "); synchronize it before starting another"
        )
    current = versions["package.json"]
    following = next((index for index in range(top + 1, len(changelog)) if changelog[index].startswith("## ")), len(changelog))
    if not any(changelog[index].startswith("- ") for index in range(top + 1, following)):
        raise SystemExit(f"{current} has no entries yet; a change belongs under it rather than under another version")
    before = _version_parts(current)
    if new is None:
        new = f"{before[0]}.{before[1]}.{before[2] + 1}"
    after = _version_parts(new)
    if after <= before:
        raise SystemExit(f"{new} is not after {current}")
    if before[0] == 0 and after[0] == 0 and after[1] != before[1]:
        raise SystemExit(f"before 1.0 only the last component advances, so {new} cannot follow {current}")

    changelog = changelog[:top] + [f"## {new} {EM_DASH} {date}", ""] + changelog[top:]
    agents = list(agents)
    agents[line] = f"Current development version: **{new} {EM_DASH} {date}**"
    package = PACKAGE_VERSION.sub(lambda match: f"{match.group('lead')}{new}{match.group('tail')}", package)
    backend_init = INIT_VERSION.sub(lambda match: f"{match.group('lead')}{new}{match.group('tail')}", backend_init)
    return new, changelog, agents, package, backend_init


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    adder = subcommands.add_parser("add", help="add one entry to the current version")
    adder.add_argument("entry", help='the entry, starting with its label: "[Fixed] ..."')
    dater = subcommands.add_parser("date", help="move the current version's date in AGENTS.md and CHANGELOG.md")
    dater.add_argument("date", nargs="?", help="YYYY-MM-DD; today's local date when omitted")
    starter = subcommands.add_parser(
        "version", help="start the next version in package.json, __init__.py, AGENTS.md and CHANGELOG.md",
    )
    starter.add_argument(
        "version", nargs="?", help="the version to start; the current one with its last component advanced when omitted",
    )
    starter.add_argument("--date", help="YYYY-MM-DD; today's local date when omitted")
    arguments = parser.parse_args()

    if arguments.command == "version":
        new, changelog, agents, package, backend_init = start_version(
            arguments.version,
            arguments.date or datetime.date.today().isoformat(),
            changelog=CHANGELOG.read_text(encoding="utf-8").splitlines(),
            agents=AGENTS.read_text(encoding="utf-8").splitlines(),
            package=PACKAGE.read_text(encoding="utf-8"),
            backend_init=BACKEND_INIT.read_text(encoding="utf-8"),
        )
        CHANGELOG.write_text("\n".join(changelog) + "\n", encoding="utf-8")
        AGENTS.write_text("\n".join(agents) + "\n", encoding="utf-8")
        PACKAGE.write_text(package, encoding="utf-8")
        BACKEND_INIT.write_text(backend_init, encoding="utf-8")
        print(f"changelog: started {new} in package.json, __init__.py, AGENTS.md and CHANGELOG.md")
        return

    version = _current_version()
    if arguments.command == "date":
        date = arguments.date or datetime.date.today().isoformat()
        changelog, agents = set_date(
            date,
            changelog=CHANGELOG.read_text(encoding="utf-8").splitlines(),
            agents=AGENTS.read_text(encoding="utf-8").splitlines(),
            version=version,
        )
        CHANGELOG.write_text("\n".join(changelog) + "\n", encoding="utf-8")
        AGENTS.write_text("\n".join(agents) + "\n", encoding="utf-8")
        print(f"changelog: {version} is dated {date} in AGENTS.md and CHANGELOG.md")
        return
    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    updated = add_entry(arguments.entry, lines=lines, version=version)
    CHANGELOG.write_text("\n".join(updated) + "\n", encoding="utf-8")
    print(f"changelog: one entry added to {version}")


if __name__ == "__main__":
    main()
