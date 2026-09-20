"""The one place a table's executable-content markers are decided.

Two readers used to answer this question separately. The import path read
bounded metadata with `iterparse` and treated any `<LuaScript>` element, any
`<AssemblerScript>` element and the `<Files>` container itself as executable
content; Review's inspector built a DOM and required a Lua script to have text
in it, derived Auto Assembler from a record's declared type, and counted only
the embedded files themselves. The two therefore disagreed about the same
bytes: Review could say **No static executable-content markers found** directly
above **Confirmation required**, which is the one decision this project asks
the user to make, and the session descriptor's `table_has_lua` was taken from
the inspector while the stored metadata came from the importer.

A marker here means content Cheat Engine can actually execute, not a container
that could have held some. An empty `<LuaScript/>` runs nothing and Cheat Engine
raises no question about it, so it is not a marker; a record declared as an Auto
Assembler script with no script body executes nothing either, and stays a
control kind rather than a marker. Everything that classifies a table reads
these predicates so that one table has one answer.
"""

from __future__ import annotations

LUA_TAGS = frozenset({"LuaScript", "LuaScriptEntry"})
ASSEMBLER_TAG = "AssemblerScript"
# A window the table brought with it. Cheat Engine instantiates these when the
# table is opened and their controls carry Lua event handlers, so a table can
# put a window on the running game and run code from it while carrying no
# `<LuaScript>` at all.
FORM_CONTAINER_TAGS = frozenset({"Forms"})
FORM_ELEMENT_TAGS = frozenset({"CustomForm", "UDF1"})
# Every tag a window involves, container included, for a reader that has to
# recognise one rather than decide whether it executes.
FORM_TAGS = FORM_CONTAINER_TAGS | FORM_ELEMENT_TAGS
# The embedded files themselves, never the `<Files>` container that holds them:
# counting the container reported one embedded file for a table that carries
# none, and counted the same payload twice for a table that carries one.
EMBEDDED_FILE_TAGS = frozenset({"TableFile", "File"})
EMBEDDED_FILE_CONTAINER_TAGS = frozenset({"Files"})
# What a `<Forms>` container can hold that is not a window. The rule below reads
# an unrecognised child of that container as a window, which is the safe way
# round for something that might execute; a payload and the container that holds
# payloads are the exception, because reading one as a window is what puts its
# bytes on the screen as the text of one.
NON_FORM_TAGS = EMBEDDED_FILE_TAGS | EMBEDDED_FILE_CONTAINER_TAGS


def local_tag(tag: object) -> str:
    """The element name without its XML namespace."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def is_lua_marker(tag: str, text: str | None) -> bool:
    """A Lua script Cheat Engine would run when it opens the table."""
    return tag in LUA_TAGS and bool((text or "").strip())


def is_auto_assembler_marker(tag: str, text: str | None) -> bool:
    """An Auto Assembler script with a body, which is what can patch the game."""
    return tag == ASSEMBLER_TAG and bool((text or "").strip())


def is_embedded_file(tag: str) -> bool:
    """One embedded payload the table carries."""
    return tag in EMBEDDED_FILE_TAGS


def is_form_marker(tag: str, parent_tag: str | None = None) -> bool:
    """A designed window the table carries, never the container holding them.

    `<Forms>` is to a window what `<Files>` is to an embedded payload: it can be
    present and empty, and counting the container itself said an empty
    `<Forms/>` was executable window content while the screen that lists what a
    table can execute, which reads the windows themselves, found nothing to
    show. That is the one screen where the two must agree, because it exists to
    justify the consent asked for directly beside it.

    A window is one of the tags Cheat Engine writes for one, or whatever else a
    `<Forms>` container holds: an unrecognised child of that container is still
    a window rather than something to classify as harmless. A payload, and the
    container payloads are written into, are the exception, because they are
    that wherever a file chooses to write them: read as a window, their bytes
    would be handed to the panel as the text of one, which is the one thing a
    table's own content may never be.
    """
    if tag in FORM_ELEMENT_TAGS:
        return True
    return parent_tag in FORM_CONTAINER_TAGS and tag not in NON_FORM_TAGS


def is_table_signature(tag: str, parent_tag: str | None) -> bool:
    """The table's own `<Signature>`, which Cheat Engine refuses to open.

    A direct child of the table root and nothing else. `Signature` is an
    ordinary word and a table is free to carry a record, a form control or a
    script variable called that; only the one Cheat Engine reads is the fact a
    row reports, and that one is where Cheat Engine writes it.

    `ct_inspector` asks the same question of the parsed root, because it has the
    element itself to read the signature's parts out of. This is the streaming
    reader's spelling of the same rule, for the metadata a listed row carries.
    """
    return tag == "Signature" and parent_tag == "CheatTable"
