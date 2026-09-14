"""A table's own code, read out for a user who has to decide whether to run it.

`.CT` import is not execution consent, and the one decision this project asks a
user to make is whether an exact SHA may execute what it carries. Until now the
answer they had to make it on was a count: "Lua AutoAssembler 1 embedded
file(s)". Reading the thing itself meant Desktop Mode, a file manager and a text
editor, which is not a controller workflow and is not something a user in Game
Mode has at all.

This is a read, never a run, and it is deliberately narrow. What it returns is
the executable content `table_markers` already declares to be executable, split
into the sections a user can reason about one at a time: the table's own Lua
script, each record's Auto Assembler script under the name of the cheat it
belongs to, a window the table brought with it, and the fact of an embedded
payload. The rest of the XML is addresses and offsets, which the cheat picker
already presents in a form worth reading.

Everything here is untrusted text from a stranger's file:

- bounded on every axis, because one record's script is measured in tens of
  kilobytes and one table's whole set in hundreds;
- stripped of the invisible and bidirectional characters that would let a
  script render as something it is not, and told about when that happens. Only
  those, though: a control label is put into NFC so two labels that look the
  same cannot differ only in how they were composed, and this screen does the
  opposite, because here the file is the authority and a decomposed sequence in
  a Lua string is what Cheat Engine will read;
- an embedded file's bytes are never returned. Its name and size are the whole
  of what can be said about it without becoming a route for shipping a payload
  into the panel.

The sizes below are measured against the tables on the maintainer's device
rather than picked. Across eleven of them the largest single Auto Assembler
script is 42200 bytes, the largest Lua script 19957, the most scripts in one
table 99, and the largest total 529 KiB. That last number is why the index and
the bodies are two separate reads: an answer that carried every script would be
half a megabyte of text sent to a quick-access panel to show a list.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET

from .ct_inspector import read_table_root
from .table_markers import (
    ASSEMBLER_TAG,
    EMBEDDED_FILE_TAGS,
    LUA_TAGS,
    is_form_marker,
    local_tag,
)
from .text import fit_utf8, is_unsafe_display_character, utf8_len

# 99 scripts in one table is the most observed; this is headroom over it rather
# than a guess, and a table past it is reported as having more than it shows.
MAX_SECTIONS = 512
# The largest single script observed is 42200 bytes. A section past this keeps
# its head and says it was cut, because the beginning of an Auto Assembler
# script is where its target and its allocation are.
MAX_SECTION_BYTES = 96 * 1024
MAX_SECTION_LINES = 8192
# One line of assembly or Lua. A minified or generated line far past this is
# cut rather than allowed to become a single unwrappable row.
MAX_LINE_BYTES = 2048
MAX_TITLE_BYTES = 192
MAX_PATH_SEGMENTS = 8

# What names one section, and the only thing a caller may ask for. A kind and an
# ordinal in document order: a record's own ID would have been the obvious key
# and cannot be used, because a table may carry the same ID twice or none at
# all, which is exactly what `ambiguous_record_ids` and the unsupported count
# already say about real tables.
SECTION_ID_RE = re.compile(r"^(lua|aa|form|file):(0|[1-9][0-9]{0,4})$")

SECTION_KINDS = {
    "lua": "lua",
    "aa": "auto_assembler",
    "form": "form",
    "file": "embedded_file",
}


class TableCodeError(ValueError):
    """The table could not be read out, or the section asked for is not in it."""


@dataclass(frozen=True)
class CodeSection:
    """One readable piece of a table, and where in the table it came from."""

    section_id: str
    kind: str
    title: str
    path: tuple[str, ...]
    text: str
    # An embedded payload is described and never returned, so its declared size
    # is not the size of anything this hands over.
    readable: bool

    def index_entry(self) -> dict[str, object]:
        # Counted rather than built: the index names every section a table has,
        # and normalizing all of their text to produce a list of sizes would do
        # the whole half-megabyte of work this read exists to avoid.
        size = utf8_len(self.text, "table section")
        lines = self.text.count("\n") + 1 if self.text else 0
        return {
            "id": self.section_id,
            "kind": self.kind,
            "title": self.title,
            "path": list(self.path),
            "bytes": size,
            "lines": lines,
            "truncated": size > MAX_SECTION_BYTES or lines > MAX_SECTION_LINES,
            "readable": self.readable,
        }


def list_table_code(path: Path, sha256: str) -> dict[str, object]:
    """The sections of one exact table, with no body text at all.

    Cheap on purpose: this is what the screen opens with, and it must not carry
    the hundreds of kilobytes the bodies add up to. Every section it names can
    then be asked for by `read_table_code`, one at a time, which is also the
    only shape a controller can actually read.
    """
    root, data = _root(path, sha256)
    sections: list[CodeSection] = []
    tally: dict[str, int] = {}
    omitted = 0
    for section in _sections(root, tally):
        if len(sections) >= MAX_SECTIONS:
            omitted += 1
            continue
        sections.append(section)
    totals = {kind: 0 for kind in SECTION_KINDS.values()}
    for section in sections:
        totals[section.kind] += 1
    return {
        "schema": 1,
        "sha256": sha256,
        "size": len(data),
        "sections": [section.index_entry() for section in sections],
        "totals": totals,
        # How many cheats the table declares, beside how few of them carry code.
        # A real table here holds eighteen records behind two scripts, which the
        # screen reported as "2 Auto Assembler" and nothing else: a reader who
        # had just seen eighteen cheats in the picker had no way to tell whether
        # the other sixteen were being hidden or simply have no code of their
        # own. It is every `CheatEntry` the file declares, which is what Cheat
        # Engine itself lists, groups included.
        "records": tally.get("records", 0),
        "omitted_sections": omitted,
    }


def read_table_code(path: Path, sha256: str, section_id: str) -> dict[str, object]:
    """One section's own text, normalized and bounded, as a list of lines.

    Lines rather than one string because the screen pages them: a script of a
    thousand lines is read a screenful at a time on a handheld, and joining them
    only to split them again in the panel would put the whole section into one
    unbreakable row.
    """
    if not isinstance(section_id, str) or not SECTION_ID_RE.fullmatch(section_id):
        raise TableCodeError("table section id is invalid")
    root, _ = _root(path, sha256)
    for section in _sections(root):
        if section.section_id != section_id:
            continue
        if not section.readable:
            # An embedded payload. Its bytes are the one thing in a table that
            # exists to be executed elsewhere, and handing them to the panel
            # would make this a transport for them rather than a view of them.
            return {
                "schema": 1,
                "id": section.section_id,
                "kind": section.kind,
                "title": section.title,
                "path": list(section.path),
                "lines": [],
                "total_lines": 0,
                "truncated": False,
                "sanitized": False,
                "readable": False,
            }
        lines, total_lines, truncated, sanitized = _bounded_body(section.text)
        return {
            "schema": 1,
            "id": section.section_id,
            "kind": section.kind,
            "title": section.title,
            "path": list(section.path),
            "lines": lines,
            "total_lines": total_lines,
            "truncated": truncated,
            "sanitized": sanitized,
            "readable": True,
        }
    raise TableCodeError("this table has no such section")


def _root(path: Path, sha256: str) -> tuple[ET.Element, bytes]:
    """The verified table as a DOM, refusing exactly what inspection refuses.

    Read through the inspector's own entry points so that a file this project
    will not inspect is not readable here either: a view that parsed what the
    rest of the plugin refuses would be a second, weaker reader of the same
    untrusted bytes.
    """
    return read_table_root(path, sha256)


def _sections(root: ET.Element, tally: dict[str, int] | None = None):
    """Every readable piece of the table, in the order the file declares it.

    `tally` is filled in as the walk goes, because the one number this screen is
    read against - how many cheats the table declares - is otherwise a second
    pass over the same document for the sake of counting one tag.
    """
    counters = {"lua": 0, "aa": 0, "form": 0, "file": 0}
    yield from _walk(root, (), counters, tally)


def _walk(root: ET.Element, path: tuple[str, ...], counters: dict[str, int], tally: dict[str, int] | None = None):
    """Walk every element without making XML nesting Python call depth.

    General XML nesting is not bounded by the structural preflight. Stopping at
    an arbitrary depth kept the old recursive walk alive, but it also made a
    script below that depth disappear from the exact-content review while the
    inspector still classified the table as executable. Iterator frames keep
    document order and use memory proportional to depth without hiding any of
    the elements already covered by the parser's element-count ceiling.
    """
    stack = [(iter((root,)), path, None, False)]
    while stack:
        elements, inherited_path, parent_tag, inside_form = stack[-1]
        try:
            element = next(elements)
        except StopIteration:
            stack.pop()
            continue

        tag = local_tag(element.tag)
        text = element.text or ""
        element_path = inherited_path

        if inside_form:
            # A window is one section, whatever it is built out of, so nothing
            # below it is listed separately. A payload written inside one is the
            # exception: it is not part of the window's text, it is a file the
            # table carries, and it has to be named here or it is named nowhere
            # while its bytes sit in the window's own body.
            if tag in EMBEDDED_FILE_TAGS:
                yield _section(
                    "file", counters, _embedded_name(element) or "Embedded file",
                    element_path, _inner_text(element), readable=False,
                )
                continue
            stack.append((iter(element), element_path, tag, True))
            continue

        if tag in LUA_TAGS and text.strip():
            yield _section("lua", counters, "Table Lua script", element_path, text, readable=True)
        elif tag == ASSEMBLER_TAG and text.strip():
            # Named by the record it belongs to rather than by its position:
            # what a user wants to read is the script behind the cheat they are
            # looking at.
            yield _section(
                "aa", counters,
                element_path[-1] if element_path else "Auto Assembler script",
                element_path[:-1], text, readable=True,
            )
        elif is_form_marker(tag, parent_tag):
            # A window the table brought with it, which is executable content
            # of its own: its controls carry Lua handlers, so a table with no
            # `<LuaScript>` at all can still run code from one. Nothing inside a
            # window is a separate section. The `<Forms>` container is not one
            # either, and it is the same predicate the inspector and the stored
            # metadata answer this with, so a table cannot be called executable
            # by Review and have nothing to show on the screen that says why.
            yield _section(
                "form", counters, f"Window the table carries ({tag})",
                element_path, _inner_text(element, without=EMBEDDED_FILE_TAGS), readable=True,
            )
            # Descended into all the same, for payloads only: the window is one
            # section and its parts are not, but a `<TableFile>` below it is a
            # file this table carries and is listed as one.
            stack.append((iter(element), element_path, tag, True))
            continue
        elif tag in EMBEDDED_FILE_TAGS:
            yield _section(
                "file", counters, _embedded_name(element) or "Embedded file",
                element_path, _inner_text(element), readable=False,
            )
            # An embedded payload's own children are that payload; nothing
            # below it is a section, and descending could report its stream as
            # a window.
            continue

        if tag == "CheatEntry":
            if tally is not None:
                tally["records"] = tally.get("records", 0) + 1
            element_path = _entry_path(element, element_path)
        stack.append((iter(element), element_path, tag, False))


def _section(
    prefix: str, counters: dict[str, int], title: str, path: tuple[str, ...], text: str, *, readable: bool,
) -> CodeSection:
    ordinal = counters[prefix]
    counters[prefix] = ordinal + 1
    return CodeSection(
        section_id=f"{prefix}:{ordinal}",
        kind=SECTION_KINDS[prefix],
        title=_label(title),
        path=path[-MAX_PATH_SEGMENTS:],
        text=text,
        readable=readable,
    )


def _entry_path(element: ET.Element, path: tuple[str, ...]) -> tuple[str, ...]:
    for child in element:
        if local_tag(child.tag) == "Description":
            name = _label((child.text or "").strip().strip('"').strip() or "<unnamed>")
            return (path + (name,))[-MAX_PATH_SEGMENTS:]
    return (path + ("<unnamed>",))[-MAX_PATH_SEGMENTS:]


def _embedded_name(element: ET.Element) -> str:
    for key in ("Filename", "FileName", "Name"):
        value = element.attrib.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for child in element:
        if local_tag(child.tag) in {"Filename", "FileName", "Name"}:
            return (child.text or "").strip()
    return ""


def _inner_text(element: ET.Element, *, without: frozenset[str] = frozenset()) -> str:
    """Everything this element carries as text, its descendants included.

    A window's definition and an embedded payload's stream are both written into
    the elements below the one that names them, so the element's own text is
    empty: using it reported a real window as nothing at all and a real payload
    as zero bytes.

    `without` leaves whole subtrees out, which is how a payload written inside a
    window stays out of the window's body. `itertext` cannot do that, and it is
    also recursive, which general nesting in a stranger's file is not bounded
    by; this keeps document order, tails included, with the frames on a list.
    """
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    stack: list[tuple[Iterator[ET.Element], str]] = [(iter(element), "")]
    while stack:
        children, tail = stack[-1]
        try:
            child = next(children)
        except StopIteration:
            stack.pop()
            if tail:
                parts.append(tail)
            continue
        if local_tag(child.tag) in without:
            # What follows the omitted subtree still belongs to this element.
            if child.tail:
                parts.append(child.tail)
            continue
        if child.text:
            parts.append(child.text)
        # The tail comes after the whole subtree, so it is held on the frame.
        stack.append((iter(child), child.tail or ""))
    return "".join(parts)


def _label(value: str) -> str:
    """A section's own name, normalized exactly as a control label is."""
    text = unicodedata.normalize("NFC", value)
    text = re.sub(r"[\t\r\n]+", " ", text).strip()
    text = "".join(ch for ch in text if not is_unsafe_display_character(ch))
    return fit_utf8(text, "table section title", MAX_TITLE_BYTES) or "<unnamed>"


def _bounded_body(text: str) -> tuple[list[str], int, bool, bool]:
    """One section as lines the panel can render, and what had to be left out.

    Returns the lines, how many the section has in total, whether anything was
    cut, and whether anything was normalized away. The last two are two
    different statements and are reported separately: a line that lost an
    invisible character is not a section that stops early, and counting them
    together made the screen say both things whenever either was true. Both are
    reported rather than swallowed, because a user reading a script to decide
    whether to run it is entitled to know that what is on screen is not
    exactly what the file holds.
    """
    # Deliberately not normalized. A control label is put into NFC so that two
    # labels which look identical cannot be told apart only by how they were
    # composed; this is the screen that says what an exact SHA will execute, and
    # there the file is the authority. A decomposed sequence inside a Lua string
    # is what Cheat Engine will read, so it is what is shown, and the only
    # changes made to a line are the two that are counted and said on screen:
    # the cut at the byte ceiling and the removal of characters that would let a
    # script render as something it is not.
    #
    # Line endings are not a change to the text: the panel renders lines, and a
    # file written on Windows has to become lines somehow.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Counted without being built, and then built only as far as the ceiling.
    # Splitting the whole section first and slicing afterwards is the same
    # answer and a different cost: a bounded `.CT` can still hold a script of
    # millions of one-character lines, and every one of them became a Python
    # string object before all but 8192 of them were dropped again. The panel
    # asks for this while the user is reading the table and before any consent,
    # so the cost of a stranger's file is paid before they have agreed to
    # anything. `split` with a limit stops at the ceiling and leaves the rest as
    # the one string it already was.
    total = normalized.count("\n") + 1
    raw_lines = normalized.split("\n", MAX_SECTION_LINES)
    truncated = False
    if len(raw_lines) > MAX_SECTION_LINES:
        del raw_lines[MAX_SECTION_LINES:]
        truncated = True

    lines: list[str] = []
    sanitized = False
    budget = MAX_SECTION_BYTES
    for raw in raw_lines:
        # Cut to the line ceiling first, and by slicing the encoded bytes once
        # rather than by dropping one character at a time and re-encoding to ask
        # how long it is now. That loop was quadratic on a line whose length is
        # chosen by the file: measured here, one line of 2 MB took 66 seconds
        # and the file ceiling is 32 MB, so a table with a single long line -
        # anything minified or generated - could hold the backend worker for
        # hours, on the screen whose whole purpose is reading a stranger's table
        # before consenting to run it. Slicing bytes can land inside a character
        # and `ignore` drops that partial one, which is the same head of the
        # line either way.
        clipped = fit_utf8(raw, "table section line", MAX_LINE_BYTES)
        if clipped != raw:
            # A line that lost its tail is a cut, not a normalization.
            truncated = True
        # Tabs are indentation in a script and are kept; everything else the
        # label rule removes is removed here too, and said. After the cut, so
        # that this walks at most one line's ceiling rather than the whole of
        # however long the file made it.
        line = "".join(ch for ch in clipped if ch == "\t" or not is_unsafe_display_character(ch))
        if line != clipped:
            sanitized = True
        cost = utf8_len(line, "table section line") + 1
        if cost > budget:
            truncated = True
            break
        budget -= cost
        lines.append(line)
    if len(lines) < len(raw_lines):
        truncated = True
    return lines, total, truncated, sanitized
