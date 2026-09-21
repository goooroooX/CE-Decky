"""Which of a table's own flags can be switched off without breaking the game.

A table's Auto Assembler script patches the game's code and gates each patch
behind a flag of its own, so switching a cheat off is writing that flag. That is
what CE Decky does when it holds a script's own defaults off, and what the user
does from the picker, and one real table showed it is not always safe: a hook
turns a pointer into an offset, tests a flag, and the branch taken when the flag
is off returns to the game without turning it back. The game's own instruction
then reads an offset as an address and the process dies, minutes later, with no
sign that a cheat table was involved.

What this reads is the script's own code: labels, direct jumps, and `sub`/`add`
pairs on the same two operands. Everything else is an opaque instruction that
changes nothing it tracks.

**It answers in one direction only.** A flag comes back as safe when every
mention of it is a test this could follow and every path through those tests
leaves the hook the way it found it. Anything else - a mention this does not
recognise, a jump this cannot follow, a walk past its own bound - is not safe,
because the whole point is to write a value into a running game and a claim of
safety this cannot support is the one thing that must not be made. The absence
of a finding is never evidence that a table is well written.
"""
from __future__ import annotations

import re

# A script longer than this is not read at all. The same bound the scan reader
# works under, for the same reason: this is a file somebody downloaded.
MAX_SCRIPT_CHARS = 2_000_000
# What one question may cost before the answer becomes "this cannot be read".
# A hook is tens of lines; a walk that needs more than this is a script shaped
# in a way this does not follow.
MAX_STATES = 4000
# How many unmatched modifications a path may carry. Deeper than this is a block
# doing something this model has no opinion about.
MAX_PENDING = 4
# How many labels a script may hold before this stops reading it.
MAX_LABELS = 4096

_JUMP = re.compile(
    r"^\s*(jmp|je|jne|jz|jnz|jl|jg|jle|jge|ja|jb|jae|jbe|jnc|jc|jns|js|jp|jnp)"
    r"\s+(?:short\s+|near\s+)?([A-Za-z_@][\w@]*)\s*$", re.IGNORECASE)
_LABEL = re.compile(r"^\s*([A-Za-z_][\w]*):\s*$")
_SUBADD = re.compile(r"^\s*(sub|add)\s+([a-z0-9]+)\s*,\s*([a-z0-9]+)\s*$", re.IGNORECASE)
# A test of one of the table's own flags against a number. The right side has
# to be a literal: `cmp qword ptr [pPlayerStats],rsi` is the script comparing a
# pointer it captured, which is not a switch and not this question.
_TEST = re.compile(
    r"^\s*cmp\s+(?:dword|qword|word|byte)\s+ptr\s*\[\s*([A-Za-z_][\w]*)\s*\]\s*,\s*"
    r"((?:0x)?[0-9][0-9A-Fa-f]*)\s*$",
    re.IGNORECASE)
_RETURN = re.compile(r"^\s*(ret|retn)\s*$", re.IGNORECASE)
# The lines a symbol is allowed to appear in without being a use of its value:
# its own declaration, and the directives that give it a name.
_DECLARES = re.compile(r"^\s*(label|registersymbol|unregistersymbol|alloc|globalalloc)\s*\(", re.IGNORECASE)
_DATA = re.compile(r"^\s*(dd|dq|dw|db|align|readmem)\b", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z_][\w]*")

_UNREADABLE = "unreadable"


def _enable_section(script: str) -> list[str]:
    """The lines Cheat Engine runs when the script is switched on.

    `[DISABLE]` is the restore and runs when the whole script comes down, which
    is not what switching one of its flags does, so it is not this question.
    """
    lines = script.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip().upper() == "[ENABLE]"), None)
    stop = next((index for index, line in enumerate(lines) if line.strip().upper() == "[DISABLE]"), len(lines))
    return lines[(start + 1) if start is not None else 0:stop]


def _without_comment(line: str) -> str:
    for marker in ("//", "--"):
        at = line.find(marker)
        if at != -1:
            line = line[:at]
    return line


class _Script:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.at: dict[str, int] = {}
        for index, line in enumerate(lines):
            found = _LABEL.match(line)
            if found and len(self.at) < MAX_LABELS:
                self.at.setdefault(found.group(1), index)

    def unbalanced_exits(self, start: int) -> set[tuple[tuple, frozenset[str]]]:
        """Exits reachable from one label that undo less than the block did.

        Walked from the block's own entry rather than from a branch, because the
        modification is made before the flag is tested: a hook subtracts a base
        from a pointer, tests, and adds it back on its way out. Each state
        carries the flags whose branches it took, which is what names the cheats
        whose being off leads to an exit that never added it back.
        """
        seen: set[tuple[int, tuple, frozenset[str]]] = set()
        found: set[tuple[tuple, frozenset[str]]] = set()
        work: list[tuple[int, tuple, frozenset[str]]] = [(start, (), frozenset())]
        while work:
            index, balance, because = work.pop()
            key = (index, balance, because)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_STATES or len(balance) > MAX_PENDING:
                return {((_UNREADABLE,), because)}
            if index >= len(self.lines):
                if balance:
                    found.add((balance, because))
                continue
            line = self.lines[index]
            state = list(balance)
            edit = _SUBADD.match(line)
            if edit:
                kind, left, right = edit.group(1).lower(), edit.group(2).lower(), edit.group(3).lower()
                if kind == "sub":
                    state.append((left, right))
                elif (left, right) in state:
                    state.remove((left, right))
            now = tuple(state)
            hop = _JUMP.match(line)
            if hop:
                target, conditional = hop.group(2), hop.group(1).lower() != "jmp"
                test = _TEST.match(self.lines[index - 1]) if index >= 1 else None
                flag = test.group(1) if test and test.group(1) in self.at else None
                taken = because | {flag} if (conditional and flag) else because
                if target.endswith("Ret"):
                    # Returning to the game is this hook's exit: the injection
                    # template names that label for the point it came from, and
                    # what the game does next is not this question.
                    if now:
                        found.add((now, taken))
                elif target not in self.at:
                    found.add(((_UNREADABLE,), taken))
                else:
                    work.append((self.at[target], now, taken))
                if conditional:
                    work.append((index + 1, now, because))
                continue
            if _RETURN.match(line):
                if now:
                    found.add((now, because))
                continue
            work.append((index + 1, now, because))
        return found


def flags_safe_to_switch_off(script: str | None) -> frozenset[str]:
    """Every symbol of this script whose flag can be written off safely.

    A positive list, and deliberately so: a caller writing into a running game
    may act on what this found, never on what it did not find.
    """
    if not script or len(script) > MAX_SCRIPT_CHARS:
        return frozenset()
    # Blank lines carry nothing and are dropped, so that "the line before this
    # jump" and "the line after this test" mean the same thing in both halves of
    # this file. They did not while blanks were kept: a blank between a flag's
    # test and its branch left the branch attributed to no flag at all, which is
    # a flag reported safe because the path that breaks it was not tied to it.
    lines = [line for line in (_without_comment(line) for line in _enable_section(script)) if line.strip()]
    reader = _Script(lines)
    if not reader.at:
        return frozenset()

    # Where each symbol is mentioned, and whether that mention is a test this
    # can follow. A symbol read some other way - moved into a register, added
    # to, used as an address - is one whose value this does not know the reach
    # of, so it is not offered as safe.
    tested: set[str] = set()
    used_otherwise: set[str] = set()
    for index, line in enumerate(lines):
        if _LABEL.match(line) or _DECLARES.match(line) or _DATA.match(line):
            continue
        test = _TEST.match(line)
        if test and test.group(1) in reader.at:
            following = _JUMP.match(lines[index + 1]) if index + 1 < len(lines) else None
            if following and following.group(1).lower() != "jmp":
                tested.add(test.group(1))
                continue
        for word in _WORD.findall(line):
            if word in reader.at:
                used_otherwise.add(word)

    refused: set[str] = set()
    for start in reader.at.values():
        for _balance, because in reader.unbalanced_exits(start):
            refused |= because
    return frozenset(tested - used_otherwise - refused)
