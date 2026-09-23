from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from hashlib import sha256 as _sha256
import io
import re
import xml.etree.ElementTree as ET
import unicodedata

from .atomic import read_regular_bytes
from .ct_hooks import flags_safe_to_switch_off
from .ct_scans import TableScan, read_scans
from .table_markers import is_auto_assembler_marker, is_embedded_file, is_form_marker, is_lua_marker, local_tag
from .table_store import MAX_CT_BYTES, MAX_XML_ELEMENTS, TableContentError, _FORBIDDEN_XML_MARKERS
from .text import fit_utf8, is_unsafe_display_character, utf8_len

MAX_INSPECTION_ENTRIES = 100_000
MAX_INSPECTION_DEPTH = 128
# How many distinct byte patterns one table may declare before this stops
# collecting them. The largest in the corpus declares 26 in one script and a
# few dozen across a table, so this is a bound on a table built to be one
# rather than a judgement about what an author may write.
MAX_TABLE_SCANS = 512
MAX_DESCRIPTION_BYTES = 4096
MAX_VARIABLE_TYPE_BYTES = 1024
# What one record's own value list may be, measured against real tables rather
# than picked. The largest list observed on this device is 268.6 KiB of 6508
# values, and one table carries three of them: at 256 KiB the first of the three
# refused the whole table, and at 4096 values each of the three silently lost
# 2412 of the items it exists to let the user choose between. Both are headroom
# over that, and neither is the real bound: the whole `.CT` is already limited
# to `MAX_CT_BYTES`, and the size of the list is what the byte limit guards
# before it is split at all.
# What one dropped whole list is marked with, so the count of values lost and
# the count of lists lost stay two different numbers.
DROPPED_VALUE_LIST = "CT dropdown list beyond the size limit"
MAX_DROPDOWN_BYTES = 1024 * 1024
MAX_DROPDOWN_VALUES = 16_384
MAX_DROPDOWN_VALUE_BYTES = 4096
MAX_DROPDOWN_LABEL_BYTES = 4096
# What a colliding label is told apart by. The value it is made from is
# table semantics and is never trimmed, but this hint is display text, and a
# long value must not push the label it disambiguates off the screen.
MAX_DROPDOWN_HINT_BYTES = 64
MAX_TABLE_VERSION_BYTES = 256
MAX_ADDRESS_HINT_BYTES = 256 * 1024
MAX_PROCESS_CANDIDATES = 256
# `aobscanmodule(name,Game.exe,AOB)` is the standard Cheat Engine idiom for
# naming the target module, so a comma and the surrounding parentheses have to
# delimit a hint just like a path separator or a quote does.
_PROCESS_RE = re.compile(r"(?i)(?:^|[\\/\s\"',(\[=])((?:[^\\/\s\"',()\[\]=]+\.exe))(?:$|[+\-\s\"',)\]:])")


# A table author's own "attach to the game" button. Its script opens the game's
# process and does nothing else: no patch, no allocation, nothing to disable.
# CE Decky attaches by exact PID before any of this runs, so the record is
# machinery rather than a cheat, and switching it on would re-open the process
# by name and can move Cheat Engine off the exact process CE Decky attached to.
# Recognised by what the script does, never by what it is called: a body with no
# assembly in it cannot change the game.
#
# Every line has to be one attach call and nothing else, which is a parse rather
# than a search. Asking only whether a line contains an attach call anywhere
# classified a line that opens the process and then does something else as if
# the something else were not there, and a `process(...)` of the author's own
# writing is not Cheat Engine's anything: neither is identifiable, so neither
# may hide a record. Anything this cannot read as canonical stays an ordinary
# script, which is the visible outcome.
ATTACH_CALL_NAMES = frozenset({"openprocess", "attachtoprocess", "getprocessidfromprocessname"})
_ATTACH_STATEMENT_RE = re.compile(r"(?i)^\s*(?:local\s+[A-Za-z_]\w*\s*=\s*)?([A-Za-z_]\w*)\s*\(")
_CALL_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(")


def _script_sections(text: str) -> tuple[str, str]:
    """Split an Auto Assembler script into its ENABLE and DISABLE bodies."""
    enable, _, rest = text.partition("[ENABLE]")
    if not rest:
        return "", ""
    body, _, disable = rest.partition("[DISABLE]")
    return body, disable


def _is_attach_statement(line: str) -> bool:
    """Whether this whole line is one recognized attach call and nothing else."""
    match = _ATTACH_STATEMENT_RE.match(line)
    if match is None or match.group(1).casefold() not in ATTACH_CALL_NAMES:
        return False
    depth = 0
    for index in range(line.index("(", match.end(1) - 1), len(line)):
        character = line[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth:
                continue
            # Anything after the call closes is another statement on this line,
            # and the call is then not all this line does.
            if line[index + 1:].strip() not in {"", ";"}:
                return False
            # `openProcess(getProcessIDFromProcessName("game.exe"))` is the same
            # idiom nested; anything else inside it is not identifiable.
            return all(
                name.casefold() in ATTACH_CALL_NAMES
                for name in _CALL_NAME_RE.findall(line[match.end():index])
            )
    return False


def _is_attach_only(text: str | None) -> bool:
    if not text or "[ENABLE]" not in text:
        return False
    enable, disable = _script_sections(text)
    if _meaningful_script_lines(disable):
        return False
    lines = _meaningful_script_lines(enable)
    if not lines:
        return False
    return all(_is_attach_statement(line) for line in lines)


def _meaningful_script_lines(body: str) -> list[str]:
    """Script lines that do something: no comments, no mode markers, no labels."""
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line or line.startswith("{$") or line in {"{", "}"} or line.endswith(":"):
            continue
        if line.startswith("{") and line.endswith("}"):
            continue
        lines.append(line)
    return lines


@dataclass(frozen=True)
class TableControl:
    id: int | None
    description: str
    path: tuple[str, ...]
    variable_type: str | None
    kind: str
    group_header: bool
    has_assembler_script: bool
    dropdown_values: tuple[tuple[str, str], ...] = ()
    dropdown_read_only: bool = False
    # The key this record's list uses for on, when its two entries are an
    # on/off pair rather than a choice between two named things. Set, the panel
    # draws a switch and writes this key for on and the other one for off; the
    # values themselves stay exactly as the author wrote them, so nothing
    # downstream loses the list.
    switch_on_value: str | None = None
    # What the enclosing Auto Assembler script declares this record's address to
    # hold, where that address is a symbol the script itself allocates and the
    # declaration can be read: `bEnableGodMode:\n  dd 1` gives `1`.
    #
    # It says what the table does on its own, before anybody chooses anything.
    # Used for two things and nothing else: holding a flag off that the script
    # would otherwise switch on, and saying so once at Review. A declaration
    # this cannot read costs both of those rather than deciding anything wrong,
    # and the symbol is the script's own, so a record named here is one that
    # exists once that script has run.
    declared_default: str | None = None
    # This record only attaches Cheat Engine to the game; it is not a cheat.
    attach_only: bool = False
    # Whether writing this record's off value is something the table's own code
    # was read and found to survive.
    #
    # A script gates each of its patches behind a flag of its own, and switching
    # a cheat off is writing that flag. One real table's hook turns a pointer
    # into an offset, tests the flag, and on the branch taken when the flag is
    # off returns to the game without turning it back: the game then reads an
    # offset as an address and dies. `ct_hooks` reads the script's own code and
    # answers in one direction only, so `false` here means anything from "this
    # cheat cannot be switched off safely" to "this was not something the reader
    # could follow" - and either way nothing writes it on the user's behalf.
    switch_off_is_safe: bool = False
    # Whether this record's address is memory one of the table's own scripts
    # allocates, rather than the game's. Decided by the address and never by
    # where the record sits: a table groups a plain `game.exe+10` switch under
    # a script as readily as it groups the script's own flags there, and only
    # the flags go away with the script's `[DISABLE]`. Read across the whole
    # table, because a symbol a script allocates is global once it runs and a
    # record reading it may sit anywhere. Backend only, so not in `as_dict`.
    script_owned: bool = False
    # Where this record sits in the table: one token per level, its place among
    # its siblings there, so an equal prefix is the same record and nothing
    # else. `path` is what the table calls those records and is for showing;
    # two siblings may both be called `Enable`, so every decision about which
    # record encloses which - the scripts a cheat needs switched on, the flags a
    # script brings with it - is taken from this one.
    structure: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value.pop("script_owned", None)
        value["path"] = list(self.path)
        value["structure"] = list(self.structure)
        value["dropdown_values"] = [list(item) for item in self.dropdown_values]
        return value


@dataclass(frozen=True)
class TableInspection:
    sha256: str
    table_version: str | None
    total_entries: int
    has_lua: bool
    has_auto_assembler: bool
    embedded_files: int
    process_candidates: tuple[str, ...]
    controls: tuple[TableControl, ...]
    ambiguous_record_ids: tuple[int, ...] = ()
    unsupported_record_id_count: int = 0
    # A designed window the table carries. Cheat Engine instantiates it when the
    # table is opened and its controls carry Lua handlers, so a table with one
    # runs code and puts a window on screen while carrying no `<LuaScript>` at
    # all - which Review used to describe as no executable content.
    has_forms: bool = False
    # Two-entry lists whose labels this could not place as an on/off pair, one
    # entry per distinct pair. Nothing behaves differently for one: it keeps its
    # dropdown, which is the right outcome for every such pair in the corpus.
    # They are reported so the next version's vocabulary is chosen from what
    # users actually met rather than from another guess.
    unrecognised_pairs: tuple[tuple[str, str], ...] = ()
    # A `<Signature>` element on the table's root. Cheat Engine refuses a signed
    # table by returning false from its own load, with no dialog and nothing in
    # its log, and every one of the 16 signed tables this project has put in
    # front of this Cheat Engine was refused. It is a structural fact about the
    # bytes, read here and stated at Review; it refuses nothing by itself.
    has_signature: bool = False
    # What that element carried, for the diagnostics log only. Which of Cheat
    # Engine's three refusals runs is not established, and one of them is a
    # Windows CNG call under Wine rather than anything about the table, so a
    # later report needs the parts rather than a verdict.
    has_signed_hash: bool = False
    has_public_key: bool = False
    public_key_bytes: int = 0
    # Labels this had to remove an invisible or bidirectional character from,
    # and values it could not carry. Both used to refuse the whole table.
    sanitized_labels: int = 0
    dropped_values: int = 0
    # Whole value lists too large to carry, counted apart from the values.
    # One of them is one picker gone from a record, not one value: the list this
    # was added for holds 6508 of them, and counting it among the values told
    # the user reading Review that a table which lost a picker had lost a value.
    dropped_value_lists: int = 0
    # Every byte pattern this table's scripts scan for, in the order the table
    # declares them and with a name read once. A script finds the game's code
    # with these, and one of them being absent from the build in front of the
    # user is what kills every cheat that script owns at the same moment.
    #
    # The patterns themselves stay on this side: what a screen needs is how
    # many there are and which of them a check could not find, and an absolute
    # sixty-byte pattern per record is not something the panel decides with.
    scans: tuple[TableScan, ...] = ()
    # Symbols this table declares more than once, meaning different code each
    # time. Two scripts of one table routinely scan under the same symbol - a
    # build for one graphics backend and a build for another - and only the
    # first of them is in `scans`, so an answer about that one is not an answer
    # about the cheats the other owns. Named here so that what is said about
    # them is that they could not be reduced to one answer.
    repeated_scans: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "sha256": self.sha256,
            "table_version": self.table_version,
            "total_entries": self.total_entries,
            "has_lua": self.has_lua,
            "has_auto_assembler": self.has_auto_assembler,
            "has_forms": self.has_forms,
            # The parts the signature carried stay out of this: they are for the
            # diagnostics log, and the panel decides nothing with them.
            "has_signature": self.has_signature,
            "sanitized_labels": self.sanitized_labels,
            "dropped_values": self.dropped_values,
            "dropped_value_lists": self.dropped_value_lists,
            # The count, not the patterns: a sentence about a missing one says
            # how many the table has, and the check that finds it runs here.
            "scan_count": len(self.scans),
            "embedded_files": self.embedded_files,
            "process_candidates": list(self.process_candidates),
            "controls": [control.as_dict() for control in self.controls],
            "ambiguous_record_ids": list(self.ambiguous_record_ids),
            "unsupported_record_id_count": self.unsupported_record_id_count,
        }


class TableBlobUnavailable(ValueError):
    """The stored file could not be read, or is not the bytes it is filed under.

    Separate from every refusal below it because it says nothing about a table:
    the file is missing, unreadable or not what this digest names, which is a
    state of the store on this device rather than a property of any content. A
    caller that records what it could not use has to be able to tell the two
    apart, or a table nobody has a copy of any more is written down as one that
    does not work.
    """


# The one element this may remove, and the whole of the transform. Matched with
# its own line where it has one, so the result is the file it was minus that
# element rather than the file with a hole punched in it.
_SIGNATURE_BYTES_RE = re.compile(
    rb"[ \t]*<Signature\b[^>]*>.*?</Signature>[ \t]*\r?\n?"
    rb"|[ \t]*<Signature\b[^>]*/>[ \t]*\r?\n?",
    re.DOTALL,
)
# The subtrees a derived table has to carry unchanged. Everything Cheat Engine
# executes or resolves is in one of them, so comparing these is what says the
# transform removed a signature rather than editing a table.
SIGNIFICANT_SUBTREES = ("CheatEntries", "LuaScript", "UserdefinedSymbols", "Forms", "Structures", "Comments")


class TableTransformError(ValueError):
    """A derivation this refused to produce, with the proof that failed."""


def strip_signature(blob: bytes) -> bytes:
    """The same table without its `<Signature>` element, and nothing else.

    A byte-level removal rather than a reserialisation: rewriting the XML would
    produce a file that differs from the author's everywhere the two writers
    disagree, and the user would be agreeing to bytes nobody has seen. One
    element goes, the rest of the file is the bytes it already was.

    Matching bytes rather than elements is what makes that possible, and it is
    only safe because of what follows it: a table whose Lua happens to contain
    the text of a signature element would be cut in the wrong place here, and
    `assert_only_signature_removed` is what refuses that result rather than
    storing it. Nothing produced by this is stored without that proof.
    """
    matches = list(_SIGNATURE_BYTES_RE.finditer(blob))
    if not matches:
        raise TableTransformError("this table carries no signature to remove")
    if len(matches) > 1:
        raise TableTransformError("this table carries more than one signature element")
    match = matches[0]
    return blob[:match.start()] + blob[match.end():]


def assert_only_signature_removed(original: bytes, derived: bytes) -> None:
    """Prove the derived bytes are the original table minus its signature.

    CE Decky is producing executable content here rather than only carrying it,
    so the result is checked against the source rather than trusted from the
    edit that made it: every subtree Cheat Engine executes or resolves has to be
    byte-identical after parsing, the root has to keep every other child it had,
    and the only element that may have gone is the signature. Anything else and
    nothing is produced at all.
    """
    for data, side in ((original, "source"), (derived, "result")):
        _reject_dtd_and_entities_bytes(data)
        _preflight_xml_structure(data)
        if len(data) > MAX_CT_BYTES:
            raise TableTransformError(f"the {side} table is larger than this store accepts")
    try:
        before = ET.fromstring(original)
        after = ET.fromstring(derived)
    except ET.ParseError as exc:
        raise TableTransformError(f"the result is not valid XML ({exc})") from exc
    if local_tag(before.tag) != "CheatTable" or local_tag(after.tag) != "CheatTable":
        raise TableTransformError("the result is not a Cheat Engine table")
    if before.attrib != after.attrib:
        raise TableTransformError("the result changed the table's own attributes")
    if _first_child(after, "Signature") is not None:
        raise TableTransformError("the result still carries a signature")
    removed = _child_tags(before) - _child_tags(after)
    if removed != {"Signature"} or _child_tags(after) - _child_tags(before):
        raise TableTransformError("the result removed or added something other than the signature")
    for name in SIGNIFICANT_SUBTREES:
        source_subtree = _first_child(before, name)
        result_subtree = _first_child(after, name)
        if (source_subtree is None) != (result_subtree is None):
            raise TableTransformError(f"the result does not carry the same {name}")
        if source_subtree is None:
            continue
        if _subtree_bytes(source_subtree) != _subtree_bytes(result_subtree):
            raise TableTransformError(f"the result changed {name}")


def _child_tags(root: ET.Element) -> set[str]:
    return {local_tag(child.tag) for child in root}


def _subtree_bytes(element: ET.Element) -> bytes:
    """One subtree, without the whitespace that follows it.

    An element's serialisation carries its tail, which is the text between it
    and whatever comes next, and removing the signature necessarily changes the
    tail of the element in front of it. Comparing that would refuse every
    derivation this exists to allow, which is what it did on the first real
    table it was run against.
    """
    clone = deepcopy(element)
    clone.tail = None
    return ET.tostring(clone)


def read_table_root(path: Path, sha256: str) -> tuple[ET.Element, bytes]:
    """One exact stored table as a DOM, or the reason it is not one.

    Every guard that stands between a file on disk and a parsed table lives
    here: the bounded read, the exact digest, the DTD refusal, the structural
    preflight and the root-element check. It is a function of its own because
    the source view reads the same bytes for a different purpose, and a second
    reader that parsed what this one refuses would be a weaker reader of the
    same untrusted file rather than another view of it.
    """
    digest = _normalize_sha(sha256)
    try:
        data = read_regular_bytes(path, max_bytes=MAX_CT_BYTES)
    except (OSError, ValueError) as exc:
        raise TableBlobUnavailable("table blob is missing or is not a regular file") from exc
    if not data:
        raise TableBlobUnavailable("table blob is empty")
    if _sha256(data).hexdigest() != digest:
        raise TableBlobUnavailable("table blob failed exact SHA-256 verification before inspection")
    _reject_dtd_and_entities_bytes(data)
    _preflight_xml_structure(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise TableContentError(f"the file is not a valid Cheat Engine table: its XML is malformed ({exc})") from exc
    if local_tag(root.tag) != "CheatTable":
        raise TableContentError("the file is not a Cheat Engine table: its XML root is not CheatTable")
    return root, data


def inspect_table(path: Path, sha256: str) -> TableInspection:
    digest = _normalize_sha(sha256)
    root, _ = read_table_root(path, digest)

    controls: list[TableControl] = []
    processes: set[str] = set()
    # What this table could not be taken exactly as written. Counted rather than
    # swallowed: a label that had a spoofing character removed, and a value this
    # cannot carry, are both things the user is entitled to be told about.
    sanitized: list[str] = []
    dropped: list[str] = []
    unrecognised: list[tuple[str, str]] = []
    scans: dict[str, TableScan] = {}
    repeated: set[str] = set()
    allocated: set[str] = set()
    owners: list[frozenset[str]] = []
    has_lua = False
    has_auto_assembler = False
    has_forms = False
    embedded_files = 0
    total_entries = 0

    # The markers themselves are classified in one place, so what Review says a
    # table carries and what the store recorded about the same bytes cannot
    # disagree.
    for parent in root.iter():
        parent_tag = local_tag(parent.tag)
        for element in parent:
            tag = local_tag(element.tag)
            if is_lua_marker(tag, element.text):
                has_lua = True
            if is_auto_assembler_marker(tag, element.text):
                has_auto_assembler = True
            # A window is recognised from the element and the container it sits
            # in, so the root itself is not a candidate: `<CheatTable>` holds no
            # window of its own and is the only element with no parent here.
            if is_form_marker(tag, parent_tag):
                has_forms = True
            if is_embedded_file(tag):
                embedded_files += 1

    signature = _first_child(root, "Signature")
    signed_hash = None if signature is None else _first_child(signature, "SignedHash")
    public_key = None if signature is None else _first_child(signature, "PublicKey")

    top_entries = _first_child(root, "CheatEntries")
    if top_entries is not None:
        position = 0
        for child in top_entries:
            if local_tag(child.tag) == "CheatEntry":
                total_entries += _walk_entry(
                    child, (), controls, processes, sanitized, dropped, unrecognised, None, scans, repeated,
                    allocated=allocated, owners=owners, structure=(str(position),),
                )
                position += 1
                if total_entries > MAX_INSPECTION_ENTRIES:
                    raise ValueError(".CT inspection exceeds entry limit")
    # Settled after the walk, because the script that allocates a symbol may
    # come after the record that reads it.
    controls = [
        replace(control, script_owned=True) if not owner.isdisjoint(allocated) else control
        for control, owner in zip(controls, owners)
    ]

    id_counts: dict[int, int] = {}
    unsupported_ids = 0
    for control in controls:
        if control.id is None:
            unsupported_ids += 1
        else:
            id_counts[control.id] = id_counts.get(control.id, 0) + 1

    return TableInspection(
        sha256=digest,
        table_version=_display_text(root.attrib.get("CheatEngineTableVersion"), "CT table version", MAX_TABLE_VERSION_BYTES, sanitized) if root.attrib.get("CheatEngineTableVersion") is not None else None,
        total_entries=total_entries,
        has_lua=has_lua,
        has_forms=has_forms,
        unrecognised_pairs=tuple(unrecognised),
        has_signature=signature is not None,
        has_signed_hash=signed_hash is not None and bool((signed_hash.text or "").strip()),
        has_public_key=public_key is not None and bool((public_key.text or "").strip()),
        public_key_bytes=_public_key_bytes(public_key),
        sanitized_labels=len(sanitized),
        dropped_values=sum(1 for item in dropped if item != DROPPED_VALUE_LIST),
        dropped_value_lists=dropped.count(DROPPED_VALUE_LIST),
        scans=tuple(scans.values()),
        repeated_scans=tuple(sorted(repeated)),
        # An Auto Assembler record with no script body is a control kind, not
        # something the table can execute, so the kind and the marker are
        # answered separately.
        has_auto_assembler=has_auto_assembler,
        embedded_files=embedded_files,
        # Two hints can differ only in case, and casefold alone then leaves the
        # order to set iteration, so the same table would not always inspect to
        # the same result. The exact spelling breaks the tie.
        process_candidates=tuple(sorted(processes, key=lambda name: (name.casefold(), name))),
        controls=tuple(controls),
        ambiguous_record_ids=tuple(sorted(record_id for record_id, count in id_counts.items() if count > 1)),
        unsupported_record_id_count=unsupported_ids,
    )


def _preflight_xml_structure(data: bytes) -> None:
    """Bound total XML structure before building the inspector DOM."""
    element_count = 0
    entry_depth = 0
    try:
        for event, element in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            tag = local_tag(element.tag)
            if event == "start":
                element_count += 1
                if element_count > MAX_XML_ELEMENTS:
                    raise ValueError(f".CT XML exceeds element limit ({MAX_XML_ELEMENTS})")
                if tag == "CheatEntry":
                    entry_depth += 1
                    if entry_depth > MAX_INSPECTION_DEPTH:
                        raise ValueError(f".CT inspection exceeds nesting depth limit ({MAX_INSPECTION_DEPTH})")
            else:
                if tag == "CheatEntry":
                    entry_depth -= 1
                element.clear()
    except ET.ParseError as exc:
        raise TableContentError(f"the file is not a valid Cheat Engine table: its XML is malformed ({exc})") from exc


def _walk_entry(
    element: ET.Element,
    parents: tuple[str, ...],
    controls: list[TableControl],
    processes: set[str],
    sanitized: list[str] | None = None,
    dropped: list[str] | None = None,
    unrecognised: list[tuple[str, str]] | None = None,
    declared: dict[str, str] | None = None,
    scans: dict[str, TableScan] | None = None,
    repeated: set[str] | None = None,
    safe: frozenset[str] | None = None,
    *,
    allocated: set[str] | None = None,
    owners: list[frozenset[str]] | None = None,
    structure: tuple[str, ...] = (),
) -> int:
    if len(parents) >= MAX_INSPECTION_DEPTH:
        raise ValueError(f".CT inspection exceeds nesting depth limit ({MAX_INSPECTION_DEPTH})")
    description = _display_text(_child_text(element, "Description") or "", "CT control description", MAX_DESCRIPTION_BYTES, sanitized).strip('"').strip() or "<unnamed>"
    path = parents + (description,)
    raw_id = (_child_text(element, "ID") or "").strip()
    try:
        record_id = int(raw_id)
        if record_id < 0 or record_id > 0x7FFFFFFF:
            record_id = None
    except ValueError:
        record_id = None
    variable_type_raw = _optional_text(_child_text(element, "VariableType"))
    variable_type = _display_text(variable_type_raw, "CT VariableType", MAX_VARIABLE_TYPE_BYTES, sanitized) if variable_type_raw is not None else None
    group_header = (_child_text(element, "GroupHeader") or "").strip() == "1"
    assembler_text = _child_text(element, "AssemblerScript")
    has_assembler = assembler_text is not None or (variable_type or "").casefold() == "auto assembler script"
    if scans is not None and assembler_text and len(scans) < MAX_TABLE_SCANS:
        # One list for the whole table, and a name read once: two scripts of one
        # table routinely scan under the same symbol, and a check that looked
        # for it twice would report it twice on the screen that names it.
        #
        # Keyed rather than searched, because this runs per record: rebuilding
        # the set of names already held, for every script of a table that may
        # carry a hundred thousand records, is quadratic in a file this parser
        # is handed by whoever wrote it.
        for scan in read_scans(assembler_text)[0]:
            if len(scans) >= MAX_TABLE_SCANS:
                break
            key = scan.name.casefold()
            first = scans.get(key)
            if first is None:
                scans[key] = scan
            elif repeated is not None and (
                (first.directive, first.module, first.pattern) != (scan.directive, scan.module, scan.pattern)
            ):
                # The same symbol standing for different code. The one kept is
                # the first the file happens to hold, so nothing said about it
                # is an answer about the other, and the check is told to leave
                # that symbol alone rather than answer for it by document order.
                repeated.add(scan.name)

    dropdown = _parse_dropdown(_child_text(element, "DropDownList") or "", sanitized, dropped)
    dropdown_read_only = (_child_text(element, "DropDownReadOnly") or "").strip() == "1"
    address = _child_text(element, "Address") or ""
    if allocated is not None and assembler_text:
        # Unbounded on purpose. A bound would have to read the names past it
        # as the game's memory, which is a stop writing a flag nobody proved
        # safe, and what bounds this is already the file: every name is a
        # line of a script the size limit has admitted.
        allocated.update(_allocated_symbols(assembler_text))
    for candidate in _process_candidates(address, "CT Address"):
        processes.add(candidate)
    # Cheat Engine writes `{ Game : <exe> }` into every Auto Assembler script it
    # generates, and disassembly lines carry the module too. A table that names
    # no process in any Address usually still names it here, which is the only
    # place the author's intent is recorded at all.
    if assembler_text:
        for candidate in _process_candidates(assembler_text, "CT AssemblerScript"):
            processes.add(candidate)

    # A record that declares itself read-only is a dropdown whether or not the
    # table author left the list empty. Classifying it as a value made the
    # picker offer a text field for a record that says it accepts nothing, and
    # the backend's `dropdown_read_only and dropdown_values` guards then let any
    # value through because the second half was false.
    kind = (
        "group" if group_header
        else "script" if has_assembler
        else "dropdown" if dropdown or dropdown_read_only
        else "value"
    )
    switch_on_value, unrecognised_pair = _switch_on_value(dropdown) if kind == "dropdown" else (None, None)
    # What the script above this record says its address holds. The address is a
    # symbol that script allocates, so this is read from the enclosing script
    # rather than from this record's own.
    # Casefolded on both sides, because Cheat Engine resolves `bEnableFlag` and
    # `benableflag` to the same symbol; so are the script's answers below.
    symbol = address.strip().casefold()
    declared_default = (declared or {}).get(symbol) if symbol else None
    # Whether this record's own flag was read and found safe to write off, asked
    # of the enclosing script for the same reason `declared_default` is: the
    # address is a symbol that script allocates and the code that reads it is
    # that script's.
    switch_off_is_safe = bool(symbol) and symbol in (safe or frozenset())
    if unrecognised_pair is not None and unrecognised is not None and unrecognised_pair not in unrecognised:
        if len(unrecognised) < MAX_UNRECOGNISED_PAIRS:
            unrecognised.append(unrecognised_pair)
    controls.append(
        TableControl(
            id=record_id,
            description=description,
            path=path,
            variable_type=variable_type,
            kind=kind,
            group_header=group_header,
            has_assembler_script=has_assembler,
            attach_only=_is_attach_only(assembler_text),
            dropdown_values=dropdown,
            dropdown_read_only=dropdown_read_only,
            switch_on_value=switch_on_value,
            declared_default=declared_default,
            switch_off_is_safe=switch_off_is_safe,
            structure=structure,
        )
    )
    if owners is not None:
        owners.append(_address_symbols(address, element))

    count = 1
    # A record inside this one is read against this script's declarations as
    # well as those of the scripts above it; the nearest one wins, because that
    # is the one that allocated the symbol last.
    nested_declared = declared
    nested_safe = safe
    if assembler_text:
        own = _declared_defaults(assembler_text)
        if own:
            nested_declared = {**(declared or {}), **own}
        # Read once per script, beside its declarations, and handed down to the
        # records it holds: the answer is about this script's own code.
        #
        # The nearest script wins here exactly as it does for declarations, and
        # for the same reason: a symbol this script allocates is read by this
        # script's hooks, so an answer another script gave for the same name is
        # not about this one. Union alone would let a parent's `safe` stand for
        # a child's unsafe hook, which is the one direction this may not err in.
        own_safe = flags_safe_to_switch_off(assembler_text)
        nested_safe = ((safe or frozenset()) - set(own)) | own_safe
    nested = _first_child(element, "CheatEntries")
    if nested is not None:
        position = 0
        for child in nested:
            if local_tag(child.tag) == "CheatEntry":
                count += _walk_entry(
                    child, path, controls, processes, sanitized, dropped, unrecognised, nested_declared, scans, repeated, nested_safe,
                    allocated=allocated, owners=owners, structure=structure + (str(position),),
                )
                position += 1
                if count > MAX_INSPECTION_ENTRIES:
                    raise ValueError(".CT inspection exceeds entry limit")
    return count


def _unique_dropdown_label(label: str, value: str, seen: set[str]) -> str:
    """A display label no other value in this dropdown already renders as.

    Cleaning and cutting labels is what keeps a table with one decorated line
    usable, but two lines whose labels differ only in what was removed then
    render as the same controller choice, and nothing on screen says which of
    them writes which value. The value is what is actually written, so the value
    is what tells them apart; it is added to the display text only, and the
    semantic value carried alongside it is untouched.
    """
    if label not in seen:
        return label
    hint = _display_text(value, "CT dropdown label", MAX_DROPDOWN_HINT_BYTES)
    ordinal = 0
    while True:
        ordinal += 1
        suffix = f" ({hint})" if ordinal == 1 else f" ({hint}) #{ordinal}"
        # The ceiling bounds what the panel renders, so the label gives up its
        # tail to the part that distinguishes it rather than the other way
        # round.
        head = _fit(label, "CT dropdown label", max(0, MAX_DROPDOWN_LABEL_BYTES - utf8_len(suffix, "CT dropdown label")))
        candidate = f"{head}{suffix}".strip()
        if candidate and candidate not in seen:
            return candidate


# What a two-entry list says when it is an on/off switch rather than a choice
# between two named things. The labels are the whole of the rule: neither the
# variable type nor the key values decide it.
#
# `scripts/table_ui_probe.py --summary` over the 142 real tables in the sampled
# corpus: 541 records declare exactly two entries and this places 468 of them,
# 86.5%. The 73 it leaves are two named alternatives, `Male / Female`,
# `CMYK / RGB`, `Set Own Ammo / Unlimited Ammo`, and they keep their list
# because there the labels are the information. `No Gravity / Original Value`
# is why a negative word alone is not enough to decide a side.
_SWITCH_OFF_WORDS = frozenset({
    "no", "off", "disabled", "disable", "inactive", "locked", "false", "unavailable", "none",
    "\u043d\u0435\u0442", "\u5426",
})
_SWITCH_ON_WORDS = frozenset({
    "yes", "on", "enabled", "enable", "active", "unlocked", "true", "available",
    "\u0434\u0430", "\u662f",
})
# Words, and nothing else: a label is `\U0001f44d ON`, `Enabled (default)` or
# `\u5426` as often as it is one bare word, so the emoji, the punctuation and the
# brackets are separators rather than something to strip by name.
_SWITCH_WORD = re.compile(r"[^\W_]+", re.UNICODE)
# How many distinct unrecognised pairs one table reports. The log line exists to
# choose the next version's vocabulary from what users actually met, and the
# corpus's 1294 dropdown records reduce to 42 distinct unplaced pairs across all
# 142 tables, so this is headroom rather than a limit anything real meets.
MAX_UNRECOGNISED_PAIRS = 64


# `bEnableGodMode:` on one line and `dd 1` under it, which is how a Cheat Engine
# script allocates and initialises a flag. The value is taken exactly as written,
# so a `(float)0.1` stays that and never compares equal to a switch's key.
_DECLARATION_RE = re.compile(
    r"(?mi)^[\t ]*([A-Za-z_]\w*)[\t ]*:[\t ]*(?:\r?\n[\t ]*)?(?:dd|dw|db|dq)[\t ]+([^\s/;]+)"
)
_ALLOCATION_RE = re.compile(r"(?mi)^[\t ]*(?:alloc|globalalloc|label)[\t ]*\([\t ]*([A-Za-z_]\w*)")
_PLACEMENT_RE = re.compile(r"(?m)^[\t ]*([A-Za-z_]\w*)[\t ]*:")
_BRACKETED_RE = re.compile(r"\[[^\[\]]*\]")
_ADDRESS_WORD_RE = re.compile(r"\b[A-Za-z_]\w*")
# What one script's declarations may cost to read. The largest real script here
# declares 48; a file that declares more than this is not what this sentence is
# about, and the records simply keep their own defaults.
MAX_DECLARATIONS = 4096


def _public_key_bytes(element) -> int:
    """How long the signature's key is, for the log and for nothing else.

    A count that refuses the table would be a diagnostic deciding the outcome,
    which is the one thing this may not do, so text that will not encode is
    measured rather than rejected.
    """
    if element is None:
        return 0
    return len((element.text or "").strip().encode("utf-8", "replace"))


def _allocated_symbols(script: str) -> set[str]:
    """Every name this script defines, which is where its own memory is named.

    `alloc`, `globalalloc` and `label` declare one and a `name:` line places
    it. A scan result is not one: `aobscanmodule(INJECT, ...)` names the game's
    own code, and a record at `INJECT` is the game's memory however the script
    registers the name.

    A `name:` line also places a patch in the game's code, so this reads a
    little more as the script's than is. That is the side it may err on: a
    record read as the script's keeps the value it has at a stop, while one
    read as the game's gets its off key written, and a flag nobody proved safe
    is exactly the record that write must not reach. A table's own flags are
    often placed with no `label` at all, which is why the placement counts.
    """
    names = {match.group(1).casefold() for match in _ALLOCATION_RE.finditer(script)}
    names.update(match.group(1).casefold() for match in _PLACEMENT_RE.finditer(script))
    return names


def _address_symbols(address: str, element: ET.Element) -> frozenset[str]:
    """The names this record's address is computed from, outside any bracket.

    Casefolded, because Cheat Engine resolves `bEnableFlag` and `benableflag`
    to the same symbol. What a bracket holds is a pointer read out of memory,
    and what it points at is the game's; a record carrying `<Offsets>` is the
    same, its address being only where the pointer chain starts. Anything else
    that names a script's symbol, quoted or with a constant beside it, is that
    script's memory, which is the side this may err on.
    """
    offsets = _first_child(element, "Offsets")
    if offsets is not None and any(local_tag(child.tag) == "Offset" for child in offsets):
        return frozenset()
    outside = address
    # Innermost first, so a nested dereference comes out whole; bounded by the
    # address itself, which the record's own size limit has already admitted.
    while True:
        stripped = _BRACKETED_RE.sub(" ", outside)
        if stripped == outside:
            break
        outside = stripped
    return frozenset(word.casefold() for word in _ADDRESS_WORD_RE.findall(outside))


def _declared_defaults(script: str | None) -> dict[str, str]:
    """Each symbol this script allocates, casefolded, and the value it gives it."""
    if not script:
        return {}
    declared: dict[str, str] = {}
    for symbol, value in _DECLARATION_RE.findall(script):
        if len(declared) >= MAX_DECLARATIONS:
            break
        # First declaration wins, which is the one that runs, whatever the
        # case of the one after it.
        declared.setdefault(symbol.casefold(), value)
    return declared


def _switch_side(label: str) -> int | None:
    """`1` for a label that says on, `-1` for one that says off, else `None`."""
    words = {word.casefold() for word in _SWITCH_WORD.findall(unicodedata.normalize("NFKC", label))}
    says_off = bool(words & _SWITCH_OFF_WORDS)
    says_on = bool(words & _SWITCH_ON_WORDS)
    # A label that says both says neither: `Off/On` as one entry is a list of
    # choices the author wrote out, not a side of a switch.
    if says_off == says_on:
        return None
    return 1 if says_on else -1


def _switch_on_value(dropdown: tuple[tuple[str, str], ...]) -> tuple[str | None, tuple[str, str] | None]:
    """The key meaning on, or the pair that was not recognised as a switch.

    The key is whatever the author wrote, not `1`. The corpus carries four
    distinct key pairs behind these labels: `0` off and `1` on in 28 tables,
    `1` off and `0` on in 3, and one each of `1040`/`2400` and `2`/`1`. Writing
    `1` for on would switch the records in five of those 33 tables the wrong
    way, which is a cheat that reports itself on and is not.
    """
    if len(dropdown) != 2:
        return None, None
    (first_value, first_label), (second_value, second_label) = dropdown
    # Two entries that write the same value cannot switch anything, whatever
    # they are called. A table may declare that; a switch drawn for it would
    # write the same key for on and for off.
    if first_value == second_value:
        return None, None
    sides = (_switch_side(first_label), _switch_side(second_label))
    if sides == (-1, 1):
        return second_value, None
    if sides == (1, -1):
        return first_value, None
    return None, (first_label, second_label)


def _parse_dropdown(raw: str, sanitized: list[str] | None = None, dropped: list[str] | None = None) -> tuple[tuple[str, str], ...]:
    if utf8_len(raw, "CT dropdown") > MAX_DROPDOWN_BYTES:
        # This record's list is dropped, not the table. It was the last limit in
        # here that still refused a whole file for one record: a good table of
        # 323 entries was unreadable, and therefore unusable and unloadable,
        # because one of its item lists was 12 KiB over. Every other limit in
        # this parser already drops what it cannot carry and counts it, which is
        # what let the four tables refused for one label or one over-long value
        # be imported.
        if dropped is not None:
            dropped.append(DROPPED_VALUE_LIST)
        return ()
    values: list[tuple[str, str]] = []
    labels: set[str] = set()
    for line in raw.splitlines():
        # Values are executable table semantics; do not normalize their
        # whitespace.  Only labels are presentation text.
        if not line.strip():
            continue
        value, sep, label = line.partition(":")
        if not value.strip():
            continue
        if len(values) >= MAX_DROPDOWN_VALUES:
            # Counted once per value actually left out. A single marker raised
            # as soon as the limit was reached said that a list ending exactly
            # on the limit had lost something, and that a list which lost a
            # thousand values had lost one.
            if dropped is not None:
                dropped.append("CT dropdown value beyond the limit")
            continue
        if utf8_len(value, "CT dropdown value") > MAX_DROPDOWN_VALUE_BYTES:
            # The value is table semantics and is never trimmed, so a line this
            # cannot carry is dropped. One of 82 sampled tables was refused
            # outright for a single such line.
            if dropped is not None:
                dropped.append("CT dropdown value")
            continue
        display_label = label.strip() if sep else value
        display_label = _display_text(display_label, "CT dropdown label", MAX_DROPDOWN_LABEL_BYTES, sanitized)
        # A label that cleaned down to nothing renders as its own value, which
        # is what the picker already showed for a blank one.
        display_label = display_label or _display_text(value, "CT dropdown label", MAX_DROPDOWN_LABEL_BYTES)
        display_label = _unique_dropdown_label(display_label, value, labels)
        labels.add(display_label)
        values.append((value, display_label))
    return tuple(values)


def _process_candidates(text: str, field: str = "CT Address") -> tuple[str, ...]:
    if utf8_len(text, field) > MAX_ADDRESS_HINT_BYTES:
        raise ValueError(f".{field.removeprefix('CT ')} exceeds process-hint scan limit")
    out: list[str] = []
    seen: set[str] = set()
    for match in _PROCESS_RE.finditer(text.replace("\t", " ")):
        raw_name = Path(match.group(1)).name
        name = _display_text(raw_name, "CT process hint", 1024)
        # Unlike a label, this is offered as the executable to attach to. A name
        # that had a spoofing character removed must not be presented under the
        # cleaned spelling it was made to imitate, so it is dropped instead.
        if name != raw_name:
            continue
        key = name.casefold()
        if key.endswith(".exe") and key not in seen:
            seen.add(key)
            out.append(name)
            if len(out) > MAX_PROCESS_CANDIDATES:
                raise ValueError(".CT contains too many process hints")
    return tuple(out)


def _display_text(value: str, field: str, max_bytes: int, sanitized: list[str] | None = None) -> str:
    """Normalize untrusted CT text that is rendered in the Decky UI.

    Semantic values such as dropdown values are kept byte-for-byte separately;
    only display text is normalized. Invisible and bidirectional formatting
    characters are removed, so a table cannot visually spoof a filename, a
    process or a control label.

    Removing them rather than refusing the table is the whole point. Measured
    across a sample of 82 tables served by FearLess, three carried a control
    description with such a character and one carried an over-long dropdown
    value: refusing cost the user the entire table, which was otherwise
    perfectly good, for a decoration in one label. The project already holds
    that a validation failure drops the value, the row or the page it belongs
    to and never a table that is otherwise obtainable. What is dropped is
    counted rather than swallowed, because a label that had a spoofing
    character removed is exactly the thing worth being told about.
    """
    text = unicodedata.normalize("NFC", value)
    text = re.sub(r"[\t\r\n]+", " ", text).strip()
    cleaned = "".join(ch for ch in text if not is_unsafe_display_character(ch))
    if cleaned != text and sanitized is not None:
        sanitized.append(field)
    # The byte ceiling bounds what the panel has to render. Cutting the label to
    # it costs a long name its tail; raising cost the table.
    return _fit(cleaned, field, max_bytes)


def _fit(text: str, field: str, max_bytes: int) -> str:
    """The longest leading part of `text` that fits the field's byte ceiling."""
    return fit_utf8(text, field, max_bytes)


def _first_child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if local_tag(child.tag) == name), None)


def _child_text(element: ET.Element, name: str) -> str | None:
    child = _first_child(element, name)
    return child.text if child is not None else None


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _normalize_sha(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("inspection SHA-256 must be a string")
    digest = value.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("inspection SHA-256 must be a 64-character hexadecimal digest")
    return digest



def _reject_dtd_and_entities_bytes(data: bytes) -> None:
    probe = data.upper()
    if any(marker in probe for marker in _FORBIDDEN_XML_MARKERS):
        raise TableContentError(".CT XML DTD/entity declarations are not accepted")
