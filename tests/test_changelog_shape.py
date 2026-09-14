"""Keep `CHANGELOG.md` in the exact shape `AGENTS.md` requires of it.

The changelog is edited by hand at the top of every bug-fix round, and the
mistakes are always the same shape: a stray blank line that turns one version's
list into a loose one that renders with gaps, a prefix nobody agreed on, or an
`Unreleased` section the contract does not have. None of it breaks a build, so
it survives until someone reads the rendered page - which is after publication.

These are the rules stated in `AGENTS.md` under the version-session contract,
checked here rather than by eye.
"""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = ROOT / "CHANGELOG.md"

HEADING = re.compile(r"^## (?P<version>\S+) — (?P<date>\d{4}-\d{2}-\d{2})$")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
BULLET = re.compile(r"^- \[(?P<label>[A-Za-z]+)\] \S")
ALLOWED_LABELS = ("New", "Changed", "Fixed", "Security", "Limitation")


def _lines() -> list[str]:
    return CHANGELOG.read_text(encoding="utf-8").splitlines()


def test_every_version_heading_is_exactly_version_and_local_date() -> None:
    headings = [line for line in _lines() if line.startswith("## ")]
    assert headings, "CHANGELOG.md has no version sections"
    for heading in headings:
        match = HEADING.match(heading)
        assert match, f"malformed version heading: {heading!r}"
        assert SEMVER.match(match.group("version")), f"heading is not a release version: {heading!r}"


def test_no_unreleased_section() -> None:
    """The contract has no staging area; the newest version is first and real."""
    for line in _lines():
        assert "unreleased" not in line.casefold(), f"changelog carries an Unreleased section: {line!r}"


def test_every_entry_uses_one_of_the_agreed_prefixes() -> None:
    offenders = []
    for number, line in enumerate(_lines(), start=1):
        if not line.startswith("- "):
            continue
        match = BULLET.match(line)
        if not match or match.group("label") not in ALLOWED_LABELS:
            offenders.append(f"line {number}: {line[:80]!r}")
    assert not offenders, "entries must start with one of " + ", ".join(f"[{label}]" for label in ALLOWED_LABELS) + f": {offenders}"


def test_no_blank_line_between_entries_of_one_version() -> None:
    """A blank line between two entries makes Markdown render a loose list.

    Every item then gets its own paragraph spacing, so one accidental newline
    silently restyles a whole release section. It costs nothing to catch here
    and is invisible until the rendered page is read.
    """
    lines = _lines()
    offenders = []
    for index, line in enumerate(lines):
        if line.strip():
            continue
        previous = next((text for text in reversed(lines[:index]) if text.strip()), "")
        following = next((text for text in lines[index + 1:] if text.strip()), "")
        if previous.startswith("- ") and following.startswith("- "):
            offenders.append(f"line {index + 1}, between {previous[:60]!r} and {following[:60]!r}")
    assert not offenders, f"blank line inside a version's entry list: {offenders}"


def test_a_blank_line_separates_a_version_from_the_heading_under_it() -> None:
    """An entry with a heading directly under it runs the two sections together.

    The same defect as a blank line inside one version's list, from the other
    end: Markdown reads a heading that follows a list item without a blank line
    between them as part of that list on some renderers and as a heading on the
    rest, so the boundary between two releases depends on where it is read.
    """
    lines = _lines()
    offenders = [
        f"line {index + 1}: {line!r} directly under {lines[index - 1][:60]!r}"
        for index, line in enumerate(lines)
        if index > 0 and line.startswith("## ") and lines[index - 1].strip()
    ]
    assert not offenders, f"version heading with no blank line above it: {offenders}"


def test_each_version_section_actually_carries_entries() -> None:
    """A heading with no entries under it is an edit that was never finished."""
    lines = _lines()
    starts = [index for index, line in enumerate(lines) if line.startswith("## ")]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        body = lines[start + 1:end]
        assert any(line.startswith("- ") for line in body), f"no entries under {lines[start]!r}"
