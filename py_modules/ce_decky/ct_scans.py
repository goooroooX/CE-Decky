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
    """One pattern a script scans for, and the symbol it registers it under."""

    name: str
    module: str | None
    pattern: str

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "module": self.module, "pattern": self.pattern}


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

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "present": list(self.present),
            "missing": list(self.missing),
            "not_checked": [{"name": name, "reason": reason} for name, reason in self.not_checked],
            "elapsed_ms": self.elapsed_ms,
            "reason": self.reason,
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
    return TableScan(name=name, module=module or None, pattern=pattern)


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


def _other_module(scan: TableScan, path: Path) -> str | None:
    """The module this scan looks in, where that is not the file being searched.

    A game routinely ships its code in more than one file, and a script that
    scans `GameAssembly.dll` is not making a claim about the program this was
    handed: searching this file for that pattern and reporting it absent would
    be inventing a finding. `aobscan` and `aobscanregion` name no module and
    are searched here, because a pattern found is found; a pattern they do not
    find could in principle be in another module, which is why the sentence
    this ends up in says what was searched rather than what the game holds.
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
    longest = 1
    for scan in scans:
        elsewhere = _other_module(scan, path)
        if elsewhere is not None:
            not_checked.append((scan.name, f"this pattern is looked for in {elsewhere}, which is not this program"))
            continue
        compiled = compile_pattern(scan.pattern)
        if compiled is None:
            not_checked.append((scan.name, "this pattern begins with a wildcard, so there is nothing to look for"))
            continue
        wanted.append((scan.name, compiled))
        longest = max(longest, len(scan.pattern.split()))

    found: set[str] = set()
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
                if compiled.search(data) is not None:
                    found.add(name)
                else:
                    still.append((name, compiled))
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
    return ScanCheck(
        source=source,
        present=tuple(name for name in ordered if name in found),
        missing=tuple(name for name in ordered if name not in found and name not in unreachable),
        not_checked=tuple(not_checked),
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


# What a line of an Auto Assembler script does with a symbol. Only definitions
# matter here: a block is removed because it *defines* something the failing
# scan owns, never because it mentions it, or removing one block would take out
# everything that reads the same flag.
_DEFINES_RE = re.compile(r"^\s*(?:label|registersymbol|define|alloc)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
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

    `orphaned` is the cost the user is owed: a record whose address was a symbol
    the removal took away is a cheat that is gone from the repaired copy. It is
    empty in the case this was built against, because the block that went only
    refined a hook another block already made.
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
    """One script with the blocks a failing scan owns removed, and nothing else.

    What a scan owns is found rather than assumed. Start from the scan's own
    name and the two symbols a script conventionally derives from it, take every
    block that *defines* one of them, add whatever those blocks define in turn,
    and repeat until nothing new is owned. That fixed point is what makes this
    work on a script nobody generated: the corpus names no generator at all, so
    a transform gated on one would run for nothing.

    Only definitions pull a block in. A block that merely reads a flag the
    removed code also read is somebody else's hook and stays, which is the
    difference between repairing a table and gutting it.

    Returns the repaired text, the symbols that went with it, and how many
    blocks were taken out. It proves nothing: `assert_repair_closed` does that,
    and nothing produced here is stored without it.
    """
    lines = _lines_with_endings(text)
    if len(lines) > MAX_SCRIPT_LINES:
        raise ScanRepairError("this script is longer than one this repairs")
    blocks = _blocks(lines)
    scan_lines: dict[str, int] = {}
    for name in missing:
        pattern = re.compile(r"^\s*(?:" + "|".join(SCAN_DIRECTIVES) + r")\s*\(\s*" + re.escape(name) + r"\s*,", re.IGNORECASE)
        for index, line in enumerate(lines):
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
                   if any(surviving.match(lines[at]) for at in range(*block))}
    removed: set[int] = set()
    changed = True
    while changed:
        changed = False
        for index, block in enumerate(blocks):
            if index in removed or index in scan_blocks:
                continue
            declared = _defined_in(lines, block)
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
        if block[1] < len(lines) and not lines[block[1]].strip():
            drop.add(block[1])
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
    """
    lines = _lines_with_endings(repaired)
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
    orphaned = _orphaned_records(blob, owned)
    return derived, ScanRepair(
        scans=tuple(wanted), blocks=blocks, lines=lines_removed,
        bytes_removed=len(blob) - len(derived), orphaned=orphaned,
    )


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
    """Every symbol a script defines, before anything is removed from it."""
    lines = _lines_with_endings(text)
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

    def scans_of(root: ET.Element) -> dict[str, str]:
        found: dict[str, str] = {}
        for element in root.iter():
            if local_tag(element.tag) != "AssemblerScript" or not element.text:
                continue
            for scan in read_scans(element.text)[0]:
                found.setdefault(scan.name, scan.pattern)
        return found

    was, now = scans_of(before), scans_of(after)
    gone = set(was) - set(now)
    if gone != {name for name in dropped if name in was}:
        raise ScanRepairError(f"the repair removed scans nobody asked it to: {sorted(gone)}")
    for name, pattern in now.items():
        if was.get(name) != pattern:
            raise ScanRepairError(f"the repair changed the pattern {name} scans for")
