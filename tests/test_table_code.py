"""Reading a table's own code, which is what its consent screen asks about."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import time

import pytest

from ce_decky.table_code import (
    MAX_LINE_BYTES,
    MAX_SECTION_LINES,
    TableCodeError,
    list_table_code,
    read_table_code,
)


def _table(tmp_path: Path, body: str, name: str = "table.CT") -> tuple[Path, str]:
    data = (
        '<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45">' + body + "</CheatTable>"
    ).encode("utf-8")
    path = tmp_path / name
    path.write_bytes(data)
    return path, sha256(data).hexdigest()


LUA = "function onOpen()\n  print('hello')\nend"
AA = "[ENABLE]\naobscanmodule(inj,game.exe,89 04 8A)\nalloc(newmem,$1000)\n[DISABLE]"


def test_a_script_is_listed_under_the_name_of_the_cheat_it_belongs_to(tmp_path: Path):
    """What a user wants to read is the script behind the cheat they are on.

    A record's own ID cannot name the section: real tables carry the same ID
    twice or none at all, which is exactly what the inspector's ambiguous and
    unsupported counts already say about them.
    """
    path, digest = _table(tmp_path, (
        f"<LuaScript>{LUA}</LuaScript>"
        "<CheatEntries>"
        '<CheatEntry><ID>1</ID><Description>"Player"</Description><GroupHeader>1</GroupHeader>'
        "<CheatEntries>"
        f'<CheatEntry><ID>2</ID><Description>"Infinite Health"</Description>'
        f"<AssemblerScript>{AA}</AssemblerScript></CheatEntry>"
        "</CheatEntries></CheatEntry>"
        "</CheatEntries>"
    ))

    index = list_table_code(path, digest)
    assert [(entry["id"], entry["title"]) for entry in index["sections"]] == [
        ("lua:0", "Table Lua script"),
        ("aa:0", "Infinite Health"),
    ]
    assert index["totals"] == {"lua": 1, "auto_assembler": 1, "form": 0, "embedded_file": 0}
    # The groups the cheat sits under, so one of thirty scripts called "Enable"
    # can be told from the others.
    assert index["sections"][1]["path"] == ["Player"]
    # The index carries no body text at all: one table on the maintainer's
    # device holds 529 KiB of scripts, which is not an answer to "what is in it".
    assert all("lines" in entry and "text" not in entry for entry in index["sections"])

    body = read_table_code(path, digest, "aa:0")
    assert body["lines"][0] == "[ENABLE]"
    assert body["total_lines"] == 4
    assert body["truncated"] is False


def test_the_index_counts_every_cheat_and_not_only_the_ones_carrying_code(tmp_path: Path):
    """Two sections next to eighteen cheats is a real table, not a short read.

    One script commonly creates dozens of records, and none of those records
    carries code of its own. The count of what a table declares is what tells a
    reader that apart from a table whose scripts this screen failed to list.
    """
    entries = "".join(
        f'<CheatEntry><ID>{index}</ID><Description>"Item {index}"</Description></CheatEntry>'
        for index in range(3, 18)
    )
    path, digest = _table(tmp_path, (
        "<CheatEntries>"
        '<CheatEntry><ID>1</ID><Description>"Enable"</Description><GroupHeader>1</GroupHeader>'
        "<CheatEntries>"
        f'<CheatEntry><ID>2</ID><Description>"Install hook"</Description>'
        f"<AssemblerScript>{AA}</AssemblerScript></CheatEntry>"
        f"{entries}"
        "</CheatEntries></CheatEntry>"
        "</CheatEntries>"
    ))

    index = list_table_code(path, digest)
    assert len(index["sections"]) == 1
    assert index["totals"]["auto_assembler"] == 1
    # Every `CheatEntry` the file declares, groups included, which is the same
    # list Cheat Engine itself shows.
    assert index["records"] == 17


def test_an_embedded_payload_is_named_and_its_bytes_are_never_returned(tmp_path: Path):
    """The one thing in a table that exists to be executed somewhere else.

    Describing it is the whole of what can be said without this becoming a
    transport for it rather than a view of it.
    """
    path, digest = _table(tmp_path, (
        '<Files><TableFile Filename="celua_teleport.lua">BASE64PAYLOAD==</TableFile></Files>'
    ))

    index = list_table_code(path, digest)
    assert [(entry["id"], entry["title"], entry["readable"]) for entry in index["sections"]] == [
        ("file:0", "celua_teleport.lua", False),
    ]

    body = read_table_code(path, digest, "file:0")
    assert body["readable"] is False
    assert body["lines"] == []


def test_code_is_normalized_the_way_a_control_label_is(tmp_path: Path):
    """A script that renders as something other than what it is would be a worse
    lie here than anywhere else on the panel, because reading it is the whole
    reason to decide whether to run it."""
    hidden = "print('safe')‮)'suoregnad'(tnirp"
    path, digest = _table(tmp_path, f"<LuaScript>{hidden}</LuaScript>")

    body = read_table_code(path, digest, "lua:0")
    assert "‮" not in body["lines"][0]
    # Counted rather than swallowed: what Cheat Engine would run is the file,
    # not this rendering of it, and the user is entitled to know they differ.
    assert body["sanitized"] is True


def test_a_window_the_table_carries_is_read_once_and_is_not_empty(tmp_path: Path):
    """`Forms` holds the windows; the windows are the sections.

    Yielding the container as well reported one window as two, the first of
    them empty, and reading only the container's own text made the real one
    empty too: a form's definition is written into the elements below it.
    """
    path, digest = _table(tmp_path, (
        "<Forms><CustomForm><Name>Trainer</Name><Handler>showMessage('hi')</Handler></CustomForm></Forms>"
    ))

    index = list_table_code(path, digest)
    assert [(entry["id"], entry["kind"]) for entry in index["sections"]] == [("form:0", "form")]
    assert index["totals"]["form"] == 1

    body = read_table_code(path, digest, "form:0")
    assert "showMessage('hi')" in "".join(body["lines"])


def test_an_empty_window_container_is_no_section_and_no_marker(tmp_path: Path):
    """The container and the windows in it are one question with one answer.

    `<Forms/>` on its own was executable window content according to Review and
    the stored metadata, and zero sections according to this screen, which is
    the screen that exists to show what that classification is about.
    """
    from ce_decky.ct_inspector import inspect_table

    path, digest = _table(tmp_path, "<Forms/>")

    assert list_table_code(path, digest)["totals"]["form"] == 0
    assert inspect_table(path, digest).has_forms is False


def test_an_unrecognised_window_is_listed_as_one(tmp_path: Path):
    from ce_decky.ct_inspector import inspect_table

    path, digest = _table(tmp_path, "<Forms><UDF2><Handler>showMessage('hi')</Handler></UDF2></Forms>")

    index = list_table_code(path, digest)
    assert [entry["id"] for entry in index["sections"]] == ["form:0"]
    assert inspect_table(path, digest).has_forms is True
    assert "showMessage('hi')" in "".join(read_table_code(path, digest, "form:0")["lines"])


def test_a_payload_written_inside_the_window_container_is_still_a_payload(tmp_path: Path):
    """Where a file chooses to write a payload cannot make it readable.

    The container's children are windows, and a window is read out as text. A
    `<TableFile>` put inside `<Forms>` was therefore listed as a window and its
    bytes were handed to the panel as the text of one, which is the single
    thing a table's own content may never do, and the table then reported no
    embedded file at all.
    """
    from ce_decky.ct_inspector import inspect_table

    path, digest = _table(tmp_path, '<Forms><TableFile Filename="payload.exe">TVqQAAMAAAAEAAAA</TableFile></Forms>')

    index = list_table_code(path, digest)
    assert [(entry["id"], entry["readable"]) for entry in index["sections"]] == [("file:0", False)]
    assert index["totals"] == {"lua": 0, "auto_assembler": 0, "form": 0, "embedded_file": 1}

    body = read_table_code(path, digest, "file:0")
    assert body["lines"] == []
    assert body["readable"] is False

    inspection = inspect_table(path, digest)
    assert inspection.embedded_files == 1
    assert inspection.has_forms is False


PAYLOAD_SENTINEL = "TVqQAAMAAAAEAAAA_SENTINEL"


@pytest.mark.parametrize("body", [
    f'<Forms><CustomForm><Caption>Trainer</Caption><TableFile Filename="p.bin">{PAYLOAD_SENTINEL}</TableFile><Handler>showMessage(1)</Handler></CustomForm></Forms>',
    f'<Forms><CustomForm><Wrapper><TableFile Filename="p.bin">{PAYLOAD_SENTINEL}</TableFile></Wrapper><Handler>showMessage(1)</Handler></CustomForm></Forms>',
    f'<Forms><Files><TableFile Filename="p.bin">{PAYLOAD_SENTINEL}</TableFile></Files></Forms>',
])
def test_a_payload_written_anywhere_under_a_window_is_named_and_never_rendered(tmp_path: Path, body: str):
    """A window is read out as text, so nothing that is a payload may be in it.

    Excluding a `<TableFile>` written directly inside `<Forms>` was not enough:
    a window's body is the whole of its subtree flattened into text, and the
    walk stopped at the window, so a payload one element deeper was rendered as
    part of the window and never listed as the file it is. Review counted an
    embedded file the same screen then had nothing to show for, with its bytes
    on that screen inside something else.
    """
    from ce_decky.ct_inspector import inspect_table

    path, digest = _table(tmp_path, body)

    index = list_table_code(path, digest)
    bodies = {
        entry["id"]: "".join(read_table_code(path, digest, entry["id"])["lines"])
        for entry in index["sections"]
    }
    # Nowhere in anything this screen will show.
    assert all(PAYLOAD_SENTINEL not in text for text in bodies.values()), bodies
    # Exactly one payload, named as one and readable by nobody.
    files = [entry for entry in index["sections"] if entry["kind"] == "embedded_file"]
    assert len(files) == 1
    assert files[0]["readable"] is False
    assert files[0]["title"] == "p.bin"
    # And it is the size of what it carries, which is all that can be said.
    assert files[0]["bytes"] == len(PAYLOAD_SENTINEL)

    # The three readers agree about the same bytes.
    inspection = inspect_table(path, digest)
    assert inspection.embedded_files == 1
    assert index["totals"]["embedded_file"] == 1
    assert inspection.has_forms == (index["totals"]["form"] == 1)


def test_a_window_keeps_the_text_around_a_payload_it_carries(tmp_path: Path):
    # Only the payload leaves the window's body. What is written after it still
    # belongs to the window and is still what that window would run.
    path, digest = _table(tmp_path, (
        f'<Forms><CustomForm>before<TableFile Filename="p.bin">{PAYLOAD_SENTINEL}</TableFile>after</CustomForm></Forms>'
    ))

    body = "".join(read_table_code(path, digest, "form:0")["lines"])
    assert body == "beforeafter"


def test_deep_general_nesting_cannot_hide_executable_content(tmp_path: Path):
    """Only `CheatEntry` nesting and the element count are bounded upstream.

    Three thousand nested general elements parse. Recursing into them crashed,
    and the first fix stopped at a depth ceiling instead, which kept the read
    alive by silently omitting a script the inspector still called executable.
    The exact-content review must traverse everything the accepted DOM holds.
    """
    depth = 3000
    script = "print('still here')"
    path, digest = _table(
        tmp_path,
        "<a>" * depth + f"<LuaScript>{script}</LuaScript>" + "</a>" * depth,
    )

    index = list_table_code(path, digest)
    assert [section["id"] for section in index["sections"]] == ["lua:0"]
    assert read_table_code(path, digest, "lua:0")["lines"] == [script]


def test_a_cut_line_and_a_normalized_line_are_two_different_statements(tmp_path: Path):
    """Counting them together made the screen say both whenever either was true.

    A line that lost an invisible character is not a section that stops early,
    and a reader deciding whether to run this is entitled to the difference.
    """
    path, digest = _table(tmp_path, "<LuaScript>print('a\u202eb')</LuaScript>")
    body = read_table_code(path, digest, "lua:0")
    assert body["sanitized"] is True
    assert body["truncated"] is False

    path, digest = _table(tmp_path, f"<LuaScript>{'z' * (MAX_LINE_BYTES + 10)}</LuaScript>", "cut.CT")
    body = read_table_code(path, digest, "lua:0")
    assert body["truncated"] is True
    assert body["sanitized"] is False


def test_a_section_longer_than_the_panel_keeps_its_head_and_says_so(tmp_path: Path):
    """The beginning of an Auto Assembler script is where its target and its
    allocation are, so a cut keeps the head rather than the tail."""
    long_script = "\n".join(f"mov eax,{index}" for index in range(MAX_SECTION_LINES + 50))
    path, digest = _table(tmp_path, f"<LuaScript>{long_script}</LuaScript>")

    body = read_table_code(path, digest, "lua:0")
    assert body["lines"][0] == "mov eax,0"
    assert len(body["lines"]) <= MAX_SECTION_LINES
    assert body["total_lines"] == MAX_SECTION_LINES + 50
    assert body["truncated"] is True


def test_a_newline_dense_section_is_not_materialized_whole(tmp_path: Path):
    """The ceiling on lines has to be applied while they are made, not after.

    A `.CT` inside every accepted bound can still hold a script that is
    millions of tiny lines, and splitting the whole section before dropping all
    but the first 8192 of them made a Python string object for every one. The
    panel asks for this while the user is reading a stranger's table and before
    any consent has been given, so the cost of that file is paid first and the
    decision it is being paid for comes afterwards.

    Measured on the section rather than on the whole read, because the read
    around it legitimately holds the file, its digest and its DOM, and those
    are what a section of this size costs however it is cut.
    """
    import tracemalloc

    from ce_decky.table_code import _bounded_body

    dense = "\n".join(str(index) for index in range(1000 * 1000))

    tracemalloc.start()
    try:
        lines, total, truncated, _ = _bounded_body(dense)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert total == 1000 * 1000
    assert len(lines) == MAX_SECTION_LINES
    assert truncated is True
    # Normalizing the section is one copy of it, and that is what this should
    # cost. A list of a million of its lines is around eight times more, so the
    # bound separates the two shapes rather than pinning an exact figure.
    assert peak < 4 * len(dense), peak

    # The whole read still answers for the same section, bounded the same way.
    path, digest = _table(tmp_path, f"<LuaScript>{dense}</LuaScript>")
    body = read_table_code(path, digest, "lua:0")
    assert body["total_lines"] == 1000 * 1000
    assert len(body["lines"]) == MAX_SECTION_LINES
    assert body["truncated"] is True


def test_one_generated_line_cannot_become_one_unwrappable_row(tmp_path: Path):
    path, digest = _table(tmp_path, f"<LuaScript>{'a' * (MAX_LINE_BYTES * 3)}</LuaScript>")

    body = read_table_code(path, digest, "lua:0")
    assert len(body["lines"][0].encode("utf-8")) == MAX_LINE_BYTES
    assert body["truncated"] is True


def test_a_line_as_long_as_the_file_allows_is_cut_in_one_pass(tmp_path: Path):
    """How long a line is, is chosen by a stranger's file.

    The cut used to drop one character at a time and re-encode the line to ask
    how long it was now, which is quadratic in a length the file picks: 2 MB
    measured at 66 seconds here, against a 32 MB file ceiling, on the screen
    whose whole purpose is reading a stranger's table before consenting to run
    it. This size is the point of the test rather than incidental to it, and it
    is why the assertion is a clock: the old code produced the same lines, it
    just took minutes to do it, and a table with one minified or generated line
    is an ordinary table.
    """
    line = "a" * (4 * 1024 * 1024)
    path, digest = _table(tmp_path, f"<LuaScript>{line}</LuaScript>")

    started = time.monotonic()
    body = read_table_code(path, digest, "lua:0")
    assert time.monotonic() - started < 10.0
    assert len(body["lines"][0].encode("utf-8")) == MAX_LINE_BYTES
    assert body["truncated"] is True


def test_a_script_is_shown_as_the_file_composed_it(tmp_path: Path):
    """The file is the authority on the screen that authorizes the file.

    A control label is put into NFC so two labels that look the same cannot
    differ only in how they were composed. Doing that here changed the text of
    a script while reporting nothing: a decomposed sequence inside a Lua string
    is what Cheat Engine reads, so a screen that silently recomposed it was
    showing something other than what it was asking consent for.
    """
    decomposed = "e\u0301"  # e plus a combining acute, not the precomposed form
    path, digest = _table(tmp_path, f'<LuaScript>print("caf{decomposed}")</LuaScript>')

    body = read_table_code(path, digest, "lua:0")

    assert body["lines"] == [f'print("caf{decomposed}")']
    assert "\u00e9" not in body["lines"][0]
    # Nothing was changed, so nothing is claimed to have been.
    assert body["sanitized"] is False
    assert body["truncated"] is False


def test_a_cut_never_lands_inside_a_character(tmp_path: Path):
    # The line ceiling is bytes and the text is not: slicing the encoded line
    # can land inside a multi-byte character, and half of one is not text.
    #
    # The repeated character is bound first because an escape inside an
    # f-string expression is Python 3.12 syntax, and the interpreter this
    # plugin runs on is the loader's bundled CPython 3.11.7.
    wide = "\u4e2d" * MAX_LINE_BYTES
    path, digest = _table(tmp_path, f"<LuaScript>{wide}</LuaScript>")

    kept = read_table_code(path, digest, "lua:0")["lines"][0]
    assert len(kept.encode("utf-8")) <= MAX_LINE_BYTES
    assert kept == "\u4e2d" * len(kept)


def test_only_a_section_this_table_declares_can_be_asked_for(tmp_path: Path):
    path, digest = _table(tmp_path, f"<LuaScript>{LUA}</LuaScript>")

    for rejected in ("", "lua", "lua:", "aa:x", "../etc/passwd", "lua:0 ", "file:99999999"):
        with pytest.raises(TableCodeError):
            read_table_code(path, digest, rejected)

    with pytest.raises(TableCodeError, match="no such section"):
        read_table_code(path, digest, "aa:0")


def test_a_table_that_declares_nothing_executable_lists_nothing(tmp_path: Path):
    path, digest = _table(tmp_path, (
        "<CheatEntries>"
        '<CheatEntry><ID>1</ID><Description>"Money"</Description>'
        "<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>"
        "</CheatEntries>"
    ))

    index = list_table_code(path, digest)
    assert index["sections"] == []
    assert index["totals"] == {"lua": 0, "auto_assembler": 0, "form": 0, "embedded_file": 0}


def test_the_code_view_refuses_exactly_what_inspection_refuses(tmp_path: Path):
    """A second, weaker reader of the same untrusted bytes is not another view
    of them. Both go through the one verified read."""
    path, digest = _table(tmp_path, f"<LuaScript>{LUA}</LuaScript>")

    with pytest.raises(ValueError, match="SHA-256"):
        list_table_code(path, "a" * 64)

    entity = tmp_path / "entity.CT"
    entity.write_bytes(b'<!DOCTYPE CheatTable [<!ENTITY x "boom">]><CheatTable><CheatEntries/></CheatTable>')
    with pytest.raises(ValueError):
        list_table_code(entity, sha256(entity.read_bytes()).hexdigest())

    del digest


def test_the_service_reads_a_stored_table_by_its_digest_alone(tmp_path: Path):
    """The panel has a SHA-256 and nothing else, which is the point of the store.

    It goes through `verified_blob` like every other read of stored bytes, so a
    copy that is not what the digest names is refused here too.
    """
    import logging

    from ce_decky.paths import PluginPaths
    from ce_decky.service import PluginService

    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("test"))
    service.initialize()
    source, _ = _table(tmp_path, f"<LuaScript>{LUA}</LuaScript>", "stored.CT")
    digest = service.import_table(str(source))["sha256"]

    index = service.list_table_code(digest)
    assert [entry["id"] for entry in index["sections"]] == ["lua:0"]
    assert service.read_table_code(digest, "lua:0")["lines"][0] == "function onOpen()"
