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

from dataclasses import dataclass
from pathlib import Path
import re
import time

from .atomic import read_regular_range
from .pe_version import read_pe_section_names

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
