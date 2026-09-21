"""The byte patterns a table's scripts look for, and whether this game has them.

An Auto Assembler script finds the game's code by scanning for a byte pattern:
`aobscanmodule(name, module, pattern)` and its two siblings. If one pattern is
absent the whole `[ENABLE]` block fails to compile, every `registersymbol` in it
never runs, and every cheat that script owns goes dead at once - which is what a
table written for an older build of a game looks like from the outside: nothing
happens, and Cheat Engine says why only in its own log.

Reading the patterns out of the script and looking for them in the game's own
executable answers that before anybody starts a game. It is cheap, and it is
cheap only one way: measured against a 407 MiB game program, 28 patterns cost
under a second searched one at a time and over two minutes as a single
alternation. So this searches one pattern at a time and never builds an
alternation of them. `scripts/ct_scan_survey.py` is what re-measures it.

Two things it is careful not to claim. Cheat Engine scans a running process,
not a file, so a packed or DRM-wrapped executable has patterns in memory that
are simply not in the file on disk: the section table names those wrappers, and
a file that carries one is reported as one this cannot check rather than as one
whose patterns are missing. And a pattern that begins with a wildcard has no
literal first byte to find, which is the whole of the fast path, so it is
reported as not checked rather than searched at whatever it costs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import time
import xml.etree.ElementTree as ET

from .atomic import read_regular_range
from .pe_version import read_pe_section_names
from .table_markers import local_tag

# The three Auto Assembler directives that scan for a pattern, longest first so
# `aobscanmodule` is never read as `aobscan` with a stray suffix.
#
# Every other spelling the corpus carries is Lua rather than Auto Assembler:
# `AOBScan(pat)`, `AOBScanModuleUnique(process, "...")` and `aobScanEx(...)` are
# calls in a script's `{$lua}` block or in the table's own `<LuaScript>`, they
# are commonly handed a variable rather than a literal, and what they do with
# the result is the script's business. Reading one as a declaration would be
# inventing a scan the table does not have.
SCAN_DIRECTIVES = ("aobscanmodule", "aobscanregion", "aobscan")

# How many arguments each of them takes, and where the pattern is. The pattern
# is always last: `aobscanregion` puts a start and a stop address in front of it.
_DIRECTIVE_ARGUMENTS = {"aobscanmodule": 3, "aobscanregion": 4, "aobscan": 2}

_DIRECTIVE_RE = re.compile(
    r"^\s*(" + "|".join(SCAN_DIRECTIVES) + r")\s*\(",
    re.IGNORECASE,
)

# An Auto Assembler symbol, which is what a scan's result is registered under.
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# One nibble of a pattern: half a byte this asserts something about, or half a
# byte it does not. `?` and `*` are the two spellings Cheat Engine reads as a
# wildcard, and the corpus carries both, including `**` for a whole byte.
_NIBBLE_RE = re.compile(r"[0-9A-Fa-f?*]")
# A whole byte nobody is asserting anything about, written on its own. This is
# what makes `48 B9 ? ? ? ?` six bytes rather than four: whitespace separates
# tokens, and a token is one or more bytes rather than exactly one.
_WILDCARD_TOKENS = {"?", "*"}

# What a scan may declare before this stops believing the line is one. The
# median pattern in the corpus is 10 bytes and the longest real one is 454, so
# this is a bound on a parse that has gone wrong rather than a judgement about
# what an author may write: a tighter one refused two real scans.
MAX_PATTERN_TOKENS = 1024
# What one call may span. A directive is one line in every table measured, and
# a line longer than this is not one this reads half of.
MAX_DIRECTIVE_CHARS = 4096
# How much of a script to read at all, so a table carrying a megabyte of Lua
# cannot make this the expensive part of an inspection.
MAX_SCRIPT_CHARS = 512 * 1024

# What the executable is read in. Large enough that the per-chunk cost of the
# search dominates the per-chunk cost of the read, small enough that a 407 MiB
# game executable is never held in memory at once.
_CHUNK_BYTES = 8 * 1024 * 1024
# The whole file, bounded: a game executable of this size does not exist, and
# something that claims to be one is not what this was asked about.
MAX_EXECUTABLE_BYTES = 2 * 1024 * 1024 * 1024
# What the whole check may cost before it stops and says so. The measured case
# is 0.6 seconds for 28 patterns over 407 MiB; this is room for a slower disk
# and a bigger table, and it is a bound rather than an expectation.
DEFAULT_BUDGET_SECONDS = 20.0

# What a script writes for "the module the attached process is", which is the
# game's own program and therefore the file this is given. It is Cheat Engine's
# own name for it, and a real table uses it beside the executable's real name in
# the same script, so both have to resolve to the file being searched.
PROCESS_MODULES = {"$process", "$module"}

# Section names that say the bytes on disk are not the code that runs. A file
# carrying one of these is reported as one this cannot check: its real patterns
# are produced in memory, so every one of them would read as missing.
#
# Measured rather than assumed, over 23 game programs on one machine: three of
# them carry a wrapper, two Steam's own `.bind` and one VMProtect's sections.
# The list is deliberately short for the same reason. Those same programs carry
# section names that look like a protector and are not - `.arch`, `.xcode`,
# `.xtls`, `.code`, `.trace` among them - on files whose patterns this finds
# perfectly well, including the one this whole check was built against. A name
# added here on suspicion would refuse a check that demonstrably works, which
# is worse than the miss it was meant to prevent.
PACKER_SECTIONS = {
    ".bind",       # Steam DRM
    ".themida", ".winlice", ".vmp0", ".vmp1", ".vmp2",
    "upx0", "upx1", "upx2", ".upx0", ".upx1",
    ".enigma1", ".enigma2", ".aspack", ".adata", ".petite", ".mpress1", ".mpress2",
    ".nsp0", ".nsp1", ".nsp2", "pebundle", ".boom", ".ccg", ".charmve",
}


class ScanParseError(ValueError):
    """One scan directive this refuses to read half of."""


@dataclass(frozen=True)
class TableScan:
    """One pattern a script scans for, and the symbol it registers it under.

    `directive` is which of Cheat Engine's three scanners the script asked for,
    and it is what says where the pattern is looked for: `aobscanmodule` names
    one file, `aobscan` searches the whole of the running process, and
    `aobscanregion` searches between two addresses that only exist once the game
    is running. Two of those three are wider than any file on disk, which is why
    the difference is carried rather than dropped: a pattern absent from one
    file is only proof about a scan that was looking in that file.
    """

    name: str
    module: str | None
    pattern: str
    directive: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "module": self.module, "pattern": self.pattern, "directive": self.directive}


@dataclass(frozen=True)
class ScanCheck:
    """What looking for a table's patterns in one executable found.

    `not_checked` is not `present`: a pattern this could not search says
    nothing about the game, and the difference is the whole point. `reason` is
    set where the check did not run at all, and then everything else is empty.

    `source` names what was searched. Today that is `file`, the game's own
    program on disk, which is what can be read before anything is started.
    Cheat Engine searches the running process, so a later source reading the
    module's mapped text is the stronger answer and this field is what would
    tell the two apart on screen and in the log.
    """

    source: str
    present: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    not_checked: tuple[tuple[str, str], ...] = ()
    elapsed_ms: int = 0
    reason: str | None = None
    # Whether a copy without the hooks of what is missing is one this can prove,
    # which is not something this function knows: searching a file says what is
    # absent, and whether the table survives having it taken out is a question
    # about the table. Filled in by the caller that holds both. `None` is the
    # third state and means nobody asked - nothing is missing, or nothing was
    # searched - and a screen may not read it as a repair that was refused.
    repairable: bool | None = None
    # Patterns this saw match in more than one place. Cheat Engine's own words
    # about its scanner are that it "will return any random match", so the
    # author of a table is the one who has to make a pattern unique, and one
    # that is not unique in the build in front of the reader is a hook that may
    # land in unrelated code.
    #
    # A floor and never a ceiling. Proving a pattern unique means reading the
    # whole program for every pattern rather than stopping at the first match,
    # which was measured at six times the cost on the program this was built
    # against - so what is reported is a second match that was actually seen,
    # and a pattern absent from this list is one nothing is claimed about.
    ambiguous: tuple[str, ...] = ()
    # The patterns in `missing` whose absence from what was searched is proof
    # that the scan finds nothing, which is not every one of them. A script can
    # ask Cheat Engine to search the whole running process, and a pattern this
    # did not find in the game's program can still be in one of the libraries
    # that program loads. Taking the code that needs it out of a table is
    # something only these may be done for, and the screen tells the two apart
    # in what it says: a pattern proved absent from the whole of where its
    # script looks is a cheat that will not work, and one absent from the file
    # this could read is a pattern that was not found here.
    proven_missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "present": list(self.present),
            "missing": list(self.missing),
            "proven_missing": list(self.proven_missing),
            "ambiguous": list(self.ambiguous),
            "not_checked": [{"name": name, "reason": reason} for name, reason in self.not_checked],
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
            "repairable": self.repairable,
        }


def extract_scans(script: str | None) -> list[TableScan]:
    """Every pattern one Auto Assembler script declares, in the order it does.

    Only a line whose first word is one of the directives counts. The corpus
    carries 198 commented-out scans against 812 live ones, and a table's own
    `{$lua}` block calls Cheat Engine's Lua scanner with names that begin the
    same way, so a rule looser than this reads scans a table does not perform.

    A directive this cannot read completely is skipped rather than half-read: a
    name that is not a symbol, an argument count that is not the directive's,
    a pattern holding something that is not a byte. Guessing at one would put a
    pattern in front of the user that the table never looks for.
    """
    return read_scans(script)[0]


def read_scans(script: str | None) -> tuple[list[TableScan], list[str]]:
    """The same scans, with the reason for every directive that was refused.

    What was refused is how the next version's parser is chosen: it is measured
    over real tables by `scripts/ct_scan_survey.py` rather than argued about,
    and a shape that turns out to be common is a change to make here.
    """
    if not script:
        return [], []
    scans: list[TableScan] = []
    refused: list[str] = []
    seen: set[str] = set()
    for line in script[:MAX_SCRIPT_CHARS].splitlines():
        try:
            scan = _scan_from_line(line)
        except ScanParseError as exc:
            refused.append(str(exc))
            continue
        if scan is None or scan.name.casefold() in seen:
            continue
        seen.add(scan.name.casefold())
        scans.append(scan)
    return scans, refused


def _scan_from_line(line: str) -> TableScan | None:
    """One directive out of one line, or `None` where the line is not one."""
    if len(line) > MAX_DIRECTIVE_CHARS:
        return None
    body = _without_comment(line)
    match = _DIRECTIVE_RE.match(body)
    if match is None:
        return None
    directive = match.group(1).casefold()
    arguments = _arguments(body[match.end() - 1:])
    if arguments is None:
        raise ScanParseError("the directive's argument list does not close")
    if len(arguments) != _DIRECTIVE_ARGUMENTS[directive]:
        raise ScanParseError("that is not the number of arguments this directive takes")
    name = arguments[0].strip()
    if _SYMBOL_RE.fullmatch(name) is None:
        raise ScanParseError("a scan's name has to be a symbol")
    pattern = normalized_pattern(arguments[-1])
    module = arguments[1].strip() if directive == "aobscanmodule" else None
    return TableScan(name=name, module=module or None, pattern=pattern, directive=directive)


def _without_comment(line: str) -> str:
    """The line with whatever a reader of it would ignore taken off the end.

    Auto Assembler comments with `//` and Lua with `--`, and a byte pattern
    carries neither character, so cutting at the first of them cannot take part
    of a pattern with it.
    """
    for marker in ("//", "--"):
        at = line.find(marker)
        if at != -1:
            line = line[:at]
    return line


def _arguments(text: str) -> list[str] | None:
    """The comma separated arguments of a call, given its opening bracket.

    Depth aware, because an address argument is routinely written as
    `module.exe+7000000` and a Lua-flavoured one can carry a nested call.
    """
    if not text.startswith("("):
        return None
    depth = 0
    current: list[str] = []
    arguments: list[str] = []
    for index, character in enumerate(text):
        if character == "(":
            depth += 1
            if depth == 1:
                continue
        elif character == ")":
            depth -= 1
            if depth == 0:
                arguments.append("".join(current))
                return arguments
        elif character == "," and depth == 1:
            arguments.append("".join(current))
            current = []
            continue
        if depth >= 1:
            current.append(character)
    return None


def normalized_pattern(text: str) -> str:
    """One pattern, as one upper case byte per token separated by single spaces.

    Written the way Cheat Engine reads it rather than the way it is spaced: a
    whitespace-separated token is one or more bytes, two nibbles each, and a
    token that is a lone `?` or `*` is one unknown byte. The corpus writes the
    same pattern every way that allows - `48 B9 ? ? ? ?`, `4D 8? B? ????0000`,
    and 16 scans with no spaces in them at all - and a reader that took one
    token for one byte got two of those three wrong.

    Refuses anything that is not a byte, which is what stops a directive whose
    last argument is a variable, a quoted Lua string or an address from being
    read as a pattern and then reported missing from the game. `x` is refused
    with them: 22 corpus scans spell a wildcard that way, Cheat Engine's own
    scanner does not document it, and a pattern read more loosely than Cheat
    Engine reads it would report a game as holding code it does not.
    """
    raw = text.strip().strip("\'\"").strip()
    if not raw:
        raise ScanParseError("that scan declares no pattern")
    found: list[str] = []
    for token in raw.split():
        if token in _WILDCARD_TOKENS:
            found.append("??")
        elif len(token) % 2 or any(_NIBBLE_RE.fullmatch(nibble) is None for nibble in token):
            raise ScanParseError("that pattern holds something that is not a byte")
        else:
            for at in range(0, len(token), 2):
                pair = token[at:at + 2].upper().replace("*", "?")
                found.append(pair)
        if len(found) > MAX_PATTERN_TOKENS:
            raise ScanParseError("that pattern is longer than a real one")
    if not found:
        raise ScanParseError("that scan declares no pattern")
    return " ".join(found)


def compile_pattern(pattern: str) -> re.Pattern[bytes] | None:
    """The pattern as something to search bytes with, or `None` for no fast path.

    `None` where the first token is not a whole known byte. The cost of this
    check is the cost of finding that first byte, and a pattern that begins
    with a wildcard has nothing to find: it is reported as not checked instead
    of being searched at whatever it turns out to cost.
    """
    tokens = pattern.split()
    if not tokens or "?" in tokens[0]:
        return None
    parts: list[bytes] = []
    for token in tokens:
        if "?" not in token:
            parts.append(re.escape(bytes([int(token, 16)])))
        elif token == "??":
            parts.append(b".")
        elif token[1] == "?":
            high = int(token[0], 16) << 4
            parts.append(b"[" + re.escape(bytes([high])) + b"-" + re.escape(bytes([high + 15])) + b"]")
        else:
            low = int(token[1], 16)
            parts.append(b"[" + b"".join(re.escape(bytes([(value << 4) | low])) for value in range(16)) + b"]")
    return re.compile(b"".join(parts), re.DOTALL)


def executable_is_packed(path: Path) -> str | None:
    """The wrapper this executable declares, or `None` where it declares none.

    What is on disk is only evidence about what Cheat Engine will find while
    the file holds the code that runs. A packer and a DRM wrapper both leave
    their own section behind, which is the cheapest honest way to know that
    every answer this could give would be wrong.
    """
    names = read_pe_section_names(path)
    if names is None:
        return None
    for name in names:
        if name.casefold() in PACKER_SECTIONS:
            return name
    return None


def other_module(scan: TableScan, path: Path) -> str | None:
    """The module this scan looks in, where it is not the file being searched.

    Public because the caller is the one that can do anything about it: a game
    ships its code in more than one file, and the file next to the program is
    something this device can look for and search in turn. Reporting the pattern
    as absent from a program that was never supposed to hold it would be
    inventing a finding; leaving it unchecked when the file is right there is
    leaving a table half read.
    """
    return _other_module(scan, path)


def _other_module(scan: TableScan, path: Path) -> str | None:
    """The module this scan looks in, where that is not the file being searched.

    A game routinely ships its code in more than one file, and a script that
    scans `GameAssembly.dll` is not making a claim about the program this was
    handed: searching this file for that pattern and reporting it absent would
    be inventing a finding. `aobscan` names no module and is searched here,
    because a pattern found is found; a pattern it does not find could be in
    another module, which is why the sentence this ends up in says what was
    searched rather than what the game holds, and why nothing is removed from a
    table on the strength of it.
    """
    if scan.module is None:
        return None
    module = scan.module.strip().strip("'\"").strip()
    if not module or module.casefold() in PROCESS_MODULES:
        return None
    return None if module.casefold() == path.name.casefold() else module


def check_executable(
    path: Path,
    scans: list[TableScan],
    *,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    source: str = "file",
) -> ScanCheck:
    """Look for each of a table's patterns in one executable, once through it.

    One pass over the file with every pattern still being looked for, rather
    than one pass per pattern: the file is the expensive part and the searches
    are not. Chunks overlap by the longest pattern less one byte, so a match
    that straddles a boundary is still found, and every span is read through
    the reader that refuses a file which changes underneath it.

    The budget is a bound rather than an expectation. What it has not answered
    when the budget is spent is reported as not checked, which is a different
    statement from a pattern that was looked for and was not there.
    """
    started = time.monotonic()
    if not scans:
        return ScanCheck(source=source, reason="this table scans for nothing", elapsed_ms=0)
    packer = executable_is_packed(path)
    if packer is not None:
        return ScanCheck(
            source=source,
            reason=f"this game's program is wrapped ({packer}), so its code is only in memory",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    not_checked: list[tuple[str, str]] = []
    wanted: list[tuple[str, re.Pattern[bytes]]] = []
    # The scans this file is the whole of the place they are looked for. A
    # directive that names the module says where it searches, and this is that
    # file; the other two are wider than any file, so what they do not find here
    # is not something this can call absent from the game.
    scoped: set[str] = set()
    longest = 1
    for scan in scans:
        if scan.directive == "aobscanregion":
            # Two addresses in a process that is not running. Nothing on disk
            # says which bytes they stand for, so the same pattern found
            # somewhere else in this file would say nothing about the range the
            # script actually searches, and neither would not finding it.
            not_checked.append((scan.name, "this pattern is looked for between two addresses in the running game"))
            continue
        elsewhere = _other_module(scan, path)
        if elsewhere is not None:
            not_checked.append((scan.name, f"this pattern is looked for in {elsewhere}, which is not this program"))
            continue
        if scan.directive == "aobscanmodule" and scan.module:
            scoped.add(scan.name)
        compiled = compile_pattern(scan.pattern)
        if compiled is None:
            not_checked.append((scan.name, "this pattern begins with a wildcard, so there is nothing to look for"))
            continue
        wanted.append((scan.name, compiled))
        longest = max(longest, len(scan.pattern.split()))

    found: set[str] = set()
    twice: set[str] = set()
    ran_out = False
    identity: tuple[int, ...] | None = None
    try:
        offset = 0
        # Never more than the span itself, or a pattern longer than one chunk
        # would make each step forward a single byte and the read unbounded.
        overlap = min(longest - 1, _CHUNK_BYTES // 2)
        while wanted:
            if time.monotonic() - started > budget_seconds:
                ran_out = True
                break
            data, info = read_regular_range(path, offset=offset, length=_CHUNK_BYTES)
            # One file, all the way through. Each span is read on its own
            # descriptor, so the reader's own guarantee covers that span and
            # nothing wider: a game patched while this runs would otherwise be
            # reported as one program by a result assembled from two.
            span = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity is None:
                identity = span
            elif span != identity:
                raise ValueError("this game's program changed while it was being read")
            if info.st_size > MAX_EXECUTABLE_BYTES:
                return ScanCheck(
                    source=source,
                    reason="this game's program is larger than anything this reads",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            if not data:
                break
            still: list[tuple[str, re.Pattern[bytes]]] = []
            for name, compiled in wanted:
                hit = compiled.search(data)
                if hit is None:
                    still.append((name, compiled))
                    continue
                found.add(name)
                # One more look, in the span already read and from one byte on,
                # so an occurrence overlapping the first is still a second
                # place. It costs the rest of one chunk rather than the rest of
                # the program, which is what makes it affordable at all: a
                # pattern is dropped once it has matched, so this span is the
                # only one it is ever looked at twice in, and nothing here says
                # a pattern is unique.
                if compiled.search(data, hit.start() + 1) is not None:
                    twice.add(name)
            wanted = still
            if offset + len(data) >= info.st_size:
                break
            offset += max(1, len(data) - overlap)
    except (OSError, ValueError) as exc:
        return ScanCheck(
            source=source,
            reason=f"this game's program could not be read: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    if ran_out:
        not_checked.extend((name, "the check ran out of time") for name, _ in wanted)
        wanted = []
    ordered = [scan.name for scan in scans]
    unreachable = {name for name, _ in not_checked}
    missing = tuple(name for name in ordered if name not in found and name not in unreachable)
    return ScanCheck(
        source=source,
        present=tuple(name for name in ordered if name in found),
        missing=missing,
        not_checked=tuple(not_checked),
        ambiguous=tuple(name for name in ordered if name in twice),
        proven_missing=tuple(name for name in missing if name in scoped),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


# What a line of an Auto Assembler script does with a symbol. Only definitions
# matter here: a block is removed because it *defines* something the failing
# scan owns, never because it mentions it, or removing one block would take out
# everything that reads the same flag.
_DEFINES_RE = re.compile(r"^\s*(?:label|registersymbol|define|alloc)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
# A whole line that says one thing about symbols and nothing else: it declares
# them, registers them, labels them or gives their memory back. Where every name
# on such a line is one the repair took away, the line is the removal's own
# leftover rather than anybody's code - it is what a script with several hooks in
# one block leaves behind, because the block cannot go without taking the other
# hooks with it. Anchored at both ends and with no second argument allowed
# through unowned, so `define(speed, 10)` and anything carrying an expression
# stay exactly where the author put them.
_OWNED_LINE_RE = re.compile(
    r"^\s*(?:unregistersymbol|dealloc|registersymbol|label|define)\s*\(([^()]*)\)\s*$", re.IGNORECASE)
_LABEL_AT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:")
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# One `<AssemblerScript>` element's bytes, so a script can be edited where it
# lies rather than by rewriting the file around it.
# The attribute is not optional in practice: a real table writes
# `<AssemblerScript Async="1">`, and a pattern that assumed a bare tag matched
# nothing at all in the file this was built against.
_SCRIPT_BYTES_RE = re.compile(rb"(<AssemblerScript\b[^>]*>)(.*?)(</AssemblerScript>)", re.DOTALL)
# What one script may be before this stops reading it as one.
MAX_SCRIPT_LINES = 20000


class ScanRepairError(ValueError):
    """A repair this refused to produce, with the proof that failed."""


@dataclass(frozen=True)
class ScanRepair:
    """What dropping a scan from a table took out, and what it cost.

    `orphaned` is the cost the user is owed: a cheat that is gone from the
    repaired copy. Two things put a record here - its address was a symbol the
    removal took away, or its whole script was the failing hook and is now an
    empty pair of sections - and the second is the common one, because most
    scripts in the corpus carry exactly one scan. It is empty in the case this
    was built against, where the block that went only refined a hook another
    block already made.
    """

    scans: tuple[str, ...]
    blocks: int
    lines: int
    bytes_removed: int
    orphaned: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "scans": list(self.scans), "blocks": self.blocks, "lines": self.lines,
            "bytes_removed": self.bytes_removed, "orphaned": list(self.orphaned),
        }


# A script's own sections. Cheat Engine runs what is under `[ENABLE]` when a
# record goes on and what is under `[DISABLE]` when it comes off, so these lines
# are the script's structure rather than any hook's code.
_SECTION_RE = re.compile(r"^\s*\[(ENABLE|DISABLE)\]\s*$", re.IGNORECASE)


def _without_block_comments(text: str) -> str:
    """The same text with Cheat Engine's `{ ... }` comments blanked out.

    Line for line and character for character, so every offset and line number
    still means what it meant: what a comment held becomes spaces, and the
    newlines stay exactly where they were.

    It matters in two places and for the same reason - a comment is not code.
    The repair must not read a definition out of one, or a table's own header
    listing what its hooks do would hand the removal every symbol it names; and
    the closure proof must not refuse a repair because a comment mentions a
    symbol that has gone, which is a refusal about nothing.

    Braces that do not balance leave the text alone. A script that opens one and
    never closes it would otherwise have everything after it read as a comment,
    and a proof that cannot see the code is worse than one that reads a comment
    as code.
    """
    # Most scripts carry none at all, and this runs over every line of every
    # script a repair touches.
    if "{" not in text:
        return text
    depth = 0
    for character in text:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return text
    if depth != 0:
        return text
    out: list[str] = []
    depth = 0
    for character in text:
        if character == "{":
            depth += 1
        out.append(character if (not depth or character == "\n") else " ")
        if character == "}" and depth:
            depth -= 1
    return "".join(out)


def _lines_with_endings(text: str) -> list[str]:
    """Every line of a script, each carrying the ending it had.

    Kept rather than normalised because this rebuilds the file from them: a real
    table is written with CRLF, and a repair that quietly rewrote every ending
    would be a diff across the whole script instead of the blocks it removed.
    """
    return text.splitlines(keepends=True)


def _blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Runs of non-blank lines, which is what a script's own blocks are.

    An author writes one hook per block with a blank line around it, and that is
    the unit a scan owns: its scan line, the labels it declares, the code it
    injects and the restore that puts the bytes back.
    """
    found: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip():
            if start is None:
                start = index
        elif start is not None:
            found.append((start, index))
            start = None
    if start is not None:
        found.append((start, len(lines)))
    return found


def _defined_in(lines: list[str], block: tuple[int, int]) -> set[str]:
    """Every symbol one block declares, by any of the four ways a script can."""
    found: set[str] = set()
    for index in range(*block):
        declared = _DEFINES_RE.match(lines[index])
        if declared:
            found.add(declared.group(1))
        labelled = _LABEL_AT_RE.match(lines[index])
        if labelled:
            found.add(labelled.group(1))
    return found


def repair_script(text: str, missing: Sequence[str]) -> tuple[str, set[str], int]:
    """One script with what a failing scan owns removed, and nothing else.

    What a scan owns is found rather than assumed. Start from the scan's own
    name and the two symbols a script conventionally derives from it, take every
    block that *defines* one of them, add whatever those blocks define in turn,
    and repeat until nothing new is owned. That fixed point is what makes this
    work on a script nobody generated: the corpus names no generator at all, so
    a transform gated on one would run for nothing.

    Only definitions pull a block in. A block that merely reads a flag the
    removed code also read is somebody else's hook and stays, which is the
    difference between repairing a table and gutting it.

    Then the residue, because a block rule cannot reach all of it: a script's
    `[DISABLE]` run declares nothing, so the pass above never sees one, and the
    lines undoing a hook that has gone stay behind naming symbols that are not
    there. A whole line that is one statement about symbols the removal took and
    nothing else goes with them. A line naming a value, an expression or a
    symbol that survived is the author's code and stays, which is what keeps
    this the same transform rather than a licence to edit.

    Returns the repaired text, the symbols that went with it, and how many
    blocks were taken out. It proves nothing: `assert_repair_closed` does that,
    and nothing produced here is stored without it.
    """
    lines = _lines_with_endings(text)
    if len(lines) > MAX_SCRIPT_LINES:
        raise ScanRepairError("this script is longer than one this repairs")
    # Structure is read from the code alone and the edit is made to the author's
    # own lines. A table's header comment lists what its hooks do, symbol by
    # symbol, and reading that as declarations handed the removal every one of
    # them; a blank line inside such a comment also ran the comment's tail into
    # the `[ENABLE]` below it, so removing one hook took the section marker with
    # it. Both are the same mistake - a comment is not code - and both go away
    # by looking at the script without them.
    code = _lines_with_endings(_without_block_comments(text))
    blocks = _blocks(code)
    scan_lines: dict[str, int] = {}
    for name in missing:
        pattern = re.compile(r"^\s*(?:" + "|".join(SCAN_DIRECTIVES) + r")\s*\(\s*" + re.escape(name) + r"\s*,", re.IGNORECASE)
        for index, line in enumerate(code):
            if pattern.match(line):
                scan_lines[name] = index
                break
    if not scan_lines:
        raise ScanRepairError("this script does not scan for any of those patterns")

    owned = set(scan_lines)
    for name in list(scan_lines):
        owned.update({f"{name}_r", f"{name}_i"})
    # A script writes its scans as one run with no blank line in it, so the
    # failing scan's line goes on its own and its neighbours stay. What is
    # protected is a block holding a scan the table still makes, never the
    # failing scan's own block: that one can also declare the symbols the hook
    # derives, and leaving those behind would refuse a repair that works.
    surviving = re.compile(
        r"^\s*(?:" + "|".join(SCAN_DIRECTIVES) + r")\s*\(\s*(?!(?:" +
        "|".join(re.escape(name) for name in scan_lines) + r")\s*,)", re.IGNORECASE)
    scan_blocks = {index for index, block in enumerate(blocks)
                   if any(surviving.match(code[at]) for at in range(*block))}
    removed: set[int] = set()
    changed = True
    while changed:
        changed = False
        for index, block in enumerate(blocks):
            if index in removed or index in scan_blocks:
                continue
            declared = _defined_in(code, block)
            if declared & owned:
                removed.add(index)
                owned |= declared
                changed = True

    drop = set(scan_lines.values())
    for index in sorted(removed):
        block = blocks[index]
        drop.update(range(block[0], block[1]))
        # The blank line under a block goes with it, or a repair leaves a run of
        # empty lines where its hooks were.
        if block[1] < len(code) and not code[block[1]].strip():
            drop.add(block[1])
    # And the leftovers no block rule can reach. The pass above takes a block
    # because of what it *defines*, and a script's `[DISABLE]` run defines
    # nothing at all: it is `unregistersymbol` and `dealloc` lines, which give
    # symbols back rather than declaring them. So the hook's whole `[ENABLE]`
    # side went and the lines that undo it stayed, naming symbols that are no
    # longer there - which is what the closure proof refuses, correctly, for a
    # repair that was otherwise complete.
    #
    # Only a line that is one statement about owned symbols and nothing else,
    # which is why this cannot reach code: it is the removal's own residue.
    # Measured over the corpus it repairs 106 more scans, taking out one to
    # eleven such lines each, and takes the tables with at least one repairable
    # scan from 34 of 88 to 57. It refuses nothing it used to allow, and in not
    # one of the 106 does the repair reach another scan of the same script or
    # cost a record its address. It does not reach the monoliths: a script with
    # six scans or more still refuses, because what survives there is a scan
    # line in a block held for the scans the table still makes.
    for index, line in enumerate(code):
        if index in drop:
            continue
        leftover = _OWNED_LINE_RE.match(_without_comment(line))
        if not leftover:
            continue
        named = [name.strip() for name in leftover.group(1).split(",") if name.strip()]
        if named and all(name in owned for name in named):
            drop.add(index)
    # A section marker is structure rather than anybody's code, and it is never
    # what a hook owns: a script writes `[DISABLE]` against its restore with no
    # blank line between them often enough that removing the restore took the
    # section with it, leaving a cheat that switches on and cannot be switched
    # off. Nothing is gained by taking one, so it stays, and the proof beside
    # this says so out loud.
    drop -= {index for index in drop if _SECTION_RE.match(code[index])}
    return "".join(line for index, line in enumerate(lines) if index not in drop), owned, len(removed)


def assert_repair_closed(repaired: str, owned: set[str], before: set[str] | None = None) -> None:
    """Prove the repaired script still resolves, or refuse it and say why.

    A repair that leaves one reference behind produces a script Cheat Engine
    refuses to compile, which is the same outcome as the missing pattern this
    was meant to fix. So both checks here are about what survived rather than
    about what went: no surviving line may name a symbol that is gone, and every
    label a surviving line jumps to must still be defined.

    The first of those is the whole of what the plan asks for in three parts. A
    `readmem` naming a removed symbol, a `define` or `alloc` anchored on the
    failing scan, and any other reference are all one thing - a line naming
    something that is not there any more - and one check that refuses all of
    them cannot be the one somebody forgets to extend.

    Comments are not code. A script that says in words what a hook used to do
    would otherwise refuse its own repair, which is a refusal about nothing.
    That covers both forms a table uses: the line a `//` or `--` starts, and
    Cheat Engine's own `{ ... }`, which a table's header is written in.
    """
    lines = _lines_with_endings(_without_block_comments(repaired))
    defined: set[str] = set()
    for block in _blocks(lines):
        defined |= _defined_in(lines, block)
    for index, line in enumerate(lines):
        code = _without_comment(line)
        still_named = set(_WORD_RE.findall(code)) & owned
        if still_named:
            raise ScanRepairError(
                f"line {index + 1} still names {sorted(still_named)[0]}, which the repair removed")
        jump = re.match(r"^\s*(?:jmp|je|jne|jg|jl|jge|jle|ja|jb|call)\s+(?:short\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*$", code.strip(), re.IGNORECASE)
        # Only a target the repair took away. A script calls things it never
        # defines - an imported function, a module export, a symbol Cheat Engine
        # resolves - and those were undefined before the repair too: refusing
        # over one would be refusing a table for something the repair did not do.
        if jump and jump.group(1) not in defined and (before is None or jump.group(1) in before):
            raise ScanRepairError(f"line {index + 1} jumps to {jump.group(1)}, which the repair removed")


def assert_sections_kept(original: str, repaired: str) -> None:
    """Prove the repaired script still has the sections it had.

    `[ENABLE]` and `[DISABLE]` are what Cheat Engine runs a record's code from,
    so a repair that takes one away leaves a script that cannot be switched on -
    the same dead table the missing pattern produced, reached by the repair for
    it. Nothing else here would notice: every symbol still resolves, and every
    record is still the record it was.

    Exactly what it had, with no case for a script the repair emptied: the
    markers cost nothing to keep, so a script whose only hook has gone keeps an
    empty pair rather than becoming a record with no script at all. What that
    costs the user is the cheats that hook created, which the caller reports.
    """
    before = [match.group(1).upper() for match in
              (_SECTION_RE.match(line) for line in _without_block_comments(original).splitlines()) if match]
    after = [match.group(1).upper() for match in
             (_SECTION_RE.match(line) for line in _without_block_comments(repaired).splitlines()) if match]
    if before == after:
        return
    missing_section = next((name for name in before if name not in after), "ENABLE")
    raise ScanRepairError(
        f"the repaired script no longer has its [{missing_section}] section, which is what Cheat Engine runs")


def _declarations_of(blob: bytes, name: str) -> int:
    """How many scan directives in this table declare that symbol.

    Counted over the script text as the file stores it rather than through the
    parser, because the parser reads a name once per script by design and the
    question here is exactly how many times it was written.
    """
    declaration = re.compile(
        r"^[^\S\n]*(?:" + "|".join(SCAN_DIRECTIVES) + r")[^\S\n]*\([^\S\n]*" + re.escape(name) + r"[^\S\n]*,",
        re.IGNORECASE | re.MULTILINE,
    )
    seen = 0
    for match in _SCRIPT_BYTES_RE.finditer(blob):
        try:
            text = match.group(2).decode("utf-8")
        except UnicodeDecodeError:
            continue
        seen += sum(1 for line in text.splitlines() if declaration.match(_without_comment(line)))
        if seen > 1:
            return seen
    return seen


def drop_unmatched_scans(blob: bytes, missing: Sequence[str]) -> tuple[bytes, ScanRepair]:
    """The same table with the hooks of a failing scan removed, and nothing else.

    A byte-level edit rather than a reserialisation, for the reason the
    signature transform gives: rewriting the XML would produce a file differing
    from the author's everywhere the two writers disagree, and the user would be
    agreeing to bytes nobody has seen. Each script is edited where it lies, in
    the encoding and the line endings it already had.

    The script text is read exactly as the file stores it, escapes and all. A
    line is kept or dropped whole, so a line carrying an escaped character is
    carried through untouched; decoding first would mean writing it back, and
    two XML writers do not agree about that either.

    This proves nothing about the result. `assert_repair_closed` proves the
    script still resolves and `assert_only_scans_dropped` proves the table is
    otherwise the table it was; nothing produced here is stored without both.
    """
    wanted = [name for name in dict.fromkeys(missing) if name]
    if not wanted:
        raise ScanRepairError("no scan was named to drop")
    # One symbol, one scan, or nothing is removed for it.
    #
    # A table can declare the same scan symbol twice - a build for one graphics
    # backend and a build for another, side by side, each with its own pattern
    # and sometimes its own module. What was proved absent was one of them, and
    # a removal that goes by name would take the other with it, silently, while
    # every proof after the edit still passed: the second occurrence's pattern
    # is not what the check looked for and not what the proof compares. Which
    # of them is the one that is missing is not something this can tell apart,
    # so it removes nothing at all.
    for name in wanted:
        if _declarations_of(blob, name) > 1:
            raise ScanRepairError(
                f"this table looks for {name} in more than one place, and which of them is the one "
                "this game's program does not hold is not something this can tell apart"
            )
    edited = bytearray()
    at = 0
    blocks = lines_removed = 0
    owned: set[str] = set()
    touched = False
    for match in _SCRIPT_BYTES_RE.finditer(blob):
        body = match.group(2)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not any(re.search(r"\b" + re.escape(name) + r"\b", text) for name in wanted):
            continue
        present = [name for name in wanted if re.search(
            r"^\s*(?:" + "|".join(SCAN_DIRECTIVES) + r")\s*\(\s*" + re.escape(name) + r"\s*,", text, re.IGNORECASE | re.MULTILINE)]
        if not present:
            continue
        repaired, script_owned, script_blocks = repair_script(text, present)
        assert_repair_closed(repaired, script_owned, _defined_anywhere(text))
        assert_sections_kept(text, repaired)
        edited += blob[at:match.start(2)]
        edited += repaired.encode("utf-8")
        at = match.end(2)
        blocks += script_blocks
        lines_removed += len(text.splitlines()) - len(repaired.splitlines())
        owned |= script_owned
        touched = True
    if not touched:
        raise ScanRepairError("no script in this table scans for what was named")
    edited += blob[at:]
    derived = bytes(edited)
    # Both ways a cheat goes. One is a record reached through an address the
    # removal took away; the other is the record that *was* the script, left
    # holding nothing but its two section markers because the failing scan was
    # the only thing in it. The second is the common one by a long way, and
    # reporting it as "no cheat was lost" would hand somebody a copy quietly
    # missing what they came for - which is the whole reason this is counted.
    orphaned = _orphaned_records(blob, owned)
    orphaned += tuple(name for name in _emptied_script_records(blob, derived) if name not in orphaned)
    return derived, ScanRepair(
        scans=tuple(wanted), blocks=blocks, lines=lines_removed,
        bytes_removed=len(blob) - len(derived), orphaned=orphaned,
    )


def _emptied_script_records(original: bytes, derived: bytes) -> tuple[str, ...]:
    """Records whose whole script the repair took, leaving an empty pair of sections.

    A script with one scan is dead when that scan is, so what is left is a cheat
    that switches and does nothing. It is not reached through an address, so the
    other half of this report never sees it, and it is the outcome most repairs
    in the corpus actually produce.
    """
    def scripted(blob: bytes) -> list[tuple[str, str]] | None:
        """Every record carrying a script, in document order, with its name."""
        try:
            root = ET.fromstring(blob)
        except ET.ParseError:
            return None
        found: list[tuple[str, str]] = []
        for entry in root.iter():
            if local_tag(entry.tag) != "CheatEntry":
                continue
            for child in entry:
                if local_tag(child.tag) != "AssemblerScript":
                    continue
                described = (entry.findtext("Description") or "").strip().strip('"').strip()
                found.append((described or f"record {(entry.findtext('ID') or '?').strip()}", child.text or ""))
                break
        return found

    before, after = scripted(original), scripted(derived)
    # Read in document order rather than by id, because a table is free to use
    # one id twice and this must not decide anything from the wrong record. The
    # edit adds and removes no element, so the two orders describe the same
    # records; where they somehow do not, this reports nothing rather than
    # guessing, and the proof that follows is what refuses the result.
    if before is None or after is None or len(before) != len(after):
        return ()
    names: list[str] = []
    for (described, was), (_still_named, now) in zip(before, after):
        if _code_in(was) and not _code_in(now):
            names.append(described)
            if len(names) >= 64:
                break
    return tuple(names)


def _code_in(script: str) -> str:
    """Whatever a script holds that Cheat Engine would run, as one string.

    Its section markers are structure and its comments are words, so a script
    left with nothing but those is a cheat that switches and does nothing.
    """
    return "".join(
        line for line in _without_block_comments(script).splitlines()
        if line.strip() and not _SECTION_RE.match(line) and _without_comment(line).strip()
    ).strip()


def scans_in_table(blob: bytes) -> list[TableScan]:
    """Every pattern a whole table scans for, read from the bytes themselves.

    The same scripts `drop_unmatched_scans` edits, read the same way, so what a
    repaired table is checked against is what a repaired table holds. Reading
    the bytes rather than a stored file is what lets a result be proven before
    anybody has agreed to it: a derived table is executable content the user has
    not consented to yet, and it has no business on disk until they have.

    A name is read once. One table declares the same scan in several scripts -
    a build for one graphics backend and a build for another, side by side - and
    a caller asking what this table looks for wants the patterns, not the
    repetitions.
    """
    found: list[TableScan] = []
    seen: set[str] = set()
    for match in _SCRIPT_BYTES_RE.finditer(blob):
        try:
            text = match.group(2).decode("utf-8")
        except UnicodeDecodeError:
            continue
        for scan in extract_scans(text):
            if scan.name.casefold() in seen:
                continue
            seen.add(scan.name.casefold())
            found.append(scan)
    return found


def _orphaned_records(blob: bytes, owned: set[str]) -> tuple[str, ...]:
    """Records whose address was a symbol the repair took away.

    This is the cost the user is owed in the words they read the table in: those
    cheats are gone from the repaired copy, and a repair that did not say so
    would be handing somebody a table quietly missing what they came for.
    """
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        return ()
    found: list[str] = []
    for entry in root.iter():
        if local_tag(entry.tag) != "CheatEntry":
            continue
        address = (entry.findtext("Address") or "").strip()
        if not address or not (set(_WORD_RE.findall(address)) & owned):
            continue
        description = (entry.findtext("Description") or "").strip().strip('"').strip()
        found.append(description or f"record {(entry.findtext('ID') or '?').strip()}")
        if len(found) >= 64:
            break
    return tuple(found)


def _defined_anywhere(text: str) -> set[str]:
    """Every symbol a script defines, before anything is removed from it.

    Read the same way the repair and the proof read one, so the three agree: a
    name a comment mentions is not a definition, and a jump to it was never
    going to resolve whatever this did.
    """
    lines = _lines_with_endings(_without_block_comments(text))
    found: set[str] = set()
    for block in _blocks(lines):
        found |= _defined_in(lines, block)
    return found


def assert_only_scans_dropped(original: bytes, derived: bytes, dropped: Sequence[str]) -> None:
    """Prove the derived table is the table it was, minus those hooks.

    The closure proof says the script still resolves. This says the file is
    still the same table: every record is still there, still named the same
    thing, still of the same type and still at the same address, and every scan
    the table still makes is still the scan it was. Between them they are what
    makes a byte-level edit safe to store, and a failure refuses the repair
    rather than producing a table nobody can account for.
    """
    try:
        before = ET.fromstring(original)
        after = ET.fromstring(derived)
    except ET.ParseError as exc:
        raise ScanRepairError(f"the repaired table is not readable XML ({exc})") from exc

    def records(root: ET.Element) -> list[tuple[str, str, str, str]]:
        found = []
        for entry in root.iter():
            if local_tag(entry.tag) != "CheatEntry":
                continue
            found.append((
                (entry.findtext("ID") or "").strip(),
                (entry.findtext("Description") or "").strip(),
                (entry.findtext("VariableType") or "").strip(),
                (entry.findtext("Address") or "").strip(),
            ))
        return found

    if records(before) != records(after):
        raise ScanRepairError("the repair changed the table's records, not only its scripts")

    def scans_of(root: ET.Element) -> list[tuple[str, str, str, str]]:
        """Every scan the table makes, in the order it makes them.

        Each one whole: the symbol, which scanner is asked for it, the module
        named if one is, and the pattern. A dictionary of one pattern per name
        was what this used to compare, and it could not see a script whose scan
        was rewritten to search somewhere else - same name, same bytes, another
        place - which is a different table wearing the same list of symbols.
        """
        found: list[tuple[str, str, str, str]] = []
        for element in root.iter():
            if local_tag(element.tag) != "AssemblerScript" or not element.text:
                continue
            for scan in read_scans(element.text)[0]:
                found.append((scan.name, scan.directive, scan.module or "", scan.pattern))
        return found

    was, now = scans_of(before), scans_of(after)
    named_before = {item[0] for item in was}
    gone = named_before - {item[0] for item in now}
    asked = {name for name in dropped if name in named_before}
    # Said apart, because they are different failures and the reader of a
    # refusal is deciding what to do about it: one took away code nobody named,
    # the other left behind what it was asked to take.
    if gone - asked:
        raise ScanRepairError(f"the repair removed scans nobody asked it to: {sorted(gone - asked)}")
    if asked - gone:
        raise ScanRepairError(f"the repair left the scans it was asked to remove: {sorted(asked - gone)}")
    kept = [item for item in was if item[0] not in gone]
    if kept != now:
        changed = next(
            (before_item[0] for before_item, after_item in zip(kept, now) if before_item != after_item),
            None,
        )
        raise ScanRepairError(
            f"the repair changed the scan {changed} the table still makes"
            if changed else "the repair changed how many scans the table makes"
        )
