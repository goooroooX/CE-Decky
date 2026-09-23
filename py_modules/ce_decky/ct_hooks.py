"""Which of a table's own flags can be switched off without breaking the game.

A table's Auto Assembler script patches the game's code and gates each patch
behind a flag of its own, so switching a cheat off is writing that flag. That is
what CE Decky does when it holds a script's own defaults off, and what the user
does from the picker, and one real table showed it is not always safe: a hook
turns a pointer into an offset, tests a flag, and the branch taken when the flag
is off returns to the game without turning it back. The game's own instruction
then reads an offset as an address and the process dies, minutes later, with no
sign that a cheat table was involved.

What this reads is the script's own code: labels, direct jumps, `sub`/`add`
pairs on the same two operands, and the `lea` that does the same arithmetic.
An instruction that writes a register out of that register's own value is a
transformation this cannot follow, and a path carrying one is not a path this
can call balanced. An `add` with no `sub` to pair it with is one of those. So is
a call made once something is waiting to be undone or once a flag has been
tested, because the callee is code this does not walk and nothing obliges it to
leave a register alone; and a branch whose target this cannot name is an exit
it cannot read. Once a flag has been tested, a load into a register and a push
or a pop are refused too: they are one side of the branch handing the game a
register or a stack the other side does not, and which side is off is the
switch's own key, which this does not read.

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
# The same arithmetic written as an address calculation, which is how a hook
# that must not touch the flags moves a pointer: `lea rcx,[rcx-rsi]` and the
# `lea rcx,[rcx+rsi]` that puts it back.
_LEA = re.compile(
    r"^\s*lea\s+([a-z0-9]+)\s*,\s*\[\s*([a-z0-9]+)\s*([+-])\s*([a-z0-9]+)\s*\]\s*$", re.IGNORECASE)
# A general-purpose register, which is what a hook hands the game back. A
# floating-point register carries a cheat's own multiplier rather than the
# address the game is about to read, so it is not what this is looking for.
_REGISTER = (
    r"(?:r[abcd]x|r[sd]i|r[sb]p|r(?:8|9|1[0-5])[dwb]?|e[abcd]x|e[sd]i|e[sb]p"
    r"|[abcd]x|[abcd][lh]|[sd]il|spl|bpl)")
# An instruction that writes one of those registers, and what it reads to do it.
_WRITES = re.compile(rf"^\s*([a-z][a-z0-9]*)\s+({_REGISTER})\s*(?:,\s*(.*))?$", re.IGNORECASE)
# Instructions that read a register without writing it, so a hook is free to
# use them between a modification and the branch that undoes it.
_READ_ONLY = frozenset({"cmp", "test", "push", "ret", "retn", "nop", "int", "int3"})
# Instructions that replace a register's whole value out of somewhere else. What
# they overwrite is gone whichever way the flag goes, so they are not something
# one branch has to put back - unless the register already carries a
# modification this is waiting to see undone, which is that modification being
# lost rather than restored.
_REPLACES = frozenset({
    "mov", "movsx", "movsxd", "movzx", "movaps", "movss", "movsd", "movups", "pop",
    "cvtss2si", "cvtsi2ss", "cvttss2si", "cvtsd2si", "cvtsi2sd",
})
# A test of one of the table's own flags against a number. The right side has
# to be a literal: `cmp qword ptr [pPlayerStats],rsi` is the script comparing a
# pointer it captured, which is not a switch and not this question.
_TEST = re.compile(
    r"^\s*cmp\s+(?:dword|qword|word|byte)\s+ptr\s*\[\s*([A-Za-z_][\w]*)\s*\]\s*,\s*"
    r"((?:0x)?[0-9][0-9A-Fa-f]*)\s*$",
    re.IGNORECASE)
_RETURN = re.compile(r"^\s*(ret|retn)\s*$", re.IGNORECASE)
# Any branch at all, including the ones `_JUMP` cannot name a target for: an
# address such as `game.exe+1234`, a pointer in memory, a loop instruction.
_ANY_JUMP = re.compile(r"^\s*(j[a-z]+|loop[a-z]*)\b", re.IGNORECASE)
# A call runs code this does not walk, and nothing obliges it to leave a
# register the way it found it.
_CALL = re.compile(r"^\s*call\b", re.IGNORECASE)
# What moves the stack pointer by itself. Paired on one path it is the hook
# saving what it uses; on one side of a flag only, it is an exit that hands the
# game a stack the other side does not.
_STACK = re.compile(r"^\s*(push|pop)[a-z]*\b", re.IGNORECASE)
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


def _changes_a_register(line: str, pending: list[tuple[str, str]], dependent: bool = False) -> bool:
    """Whether this line changes a register in a way the walk cannot follow.

    The question is not whether the line writes a register. A hook loads what it
    needs into scratch registers before it tests anything, and that load happens
    whichever way the flag goes, so it is never the difference between a cheat
    being on and the game being handed something it cannot use. After a flag
    has been tested (`dependent`) the same load is exactly that difference, so
    it counts there.

    What is that difference is a register whose own value an instruction
    transforms, because such a value has to be put back and this can only
    recognise `sub`/`add` and the `lea` spelling of them putting it back. A
    shift, a negation, an address calculation into some other register and an
    instruction this has never heard of are all read as one of those, and the
    unknown mnemonic is read that way deliberately: the answer this file may not
    give is `safe`.
    """
    found = _WRITES.match(line)
    if not found:
        return False
    mnemonic, into, reads = found.group(1).lower(), found.group(2).lower(), found.group(3)
    if mnemonic in _READ_ONLY:
        return False
    sources = {word.lower() for word in _WORD.findall(reads or "")}
    if into in sources or mnemonic not in _REPLACES:
        return True
    # Once a flag has been tested, a replacement is no longer something that
    # happens whichever way the flag goes: it is one side of the branch handing
    # the game a register the other side does not, and which side is off is the
    # switch's own key, which this does not read.
    if dependent:
        return True
    # A replacement is only a loss where it overwrites a modification this is
    # still waiting to see undone.
    return any(into == left for left, _right in pending)


class _Script:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        # Keyed casefolded, as every name this file compares is: Cheat Engine
        # resolves a label and a symbol whatever their case, so `bEnableFlag`
        # and `BENABLEFLAG` are one flag, and a use spelled differently from
        # its test is still a use.
        self.at: dict[str, int] = {}
        for index, line in enumerate(lines):
            found = _LABEL.match(line)
            if found and len(self.at) < MAX_LABELS:
                self.at.setdefault(found.group(1).casefold(), index)

    def unbalanced_exits(self, start: int) -> set[tuple[tuple, frozenset[str]]]:
        """Exits reachable from one label that undo less than the block did.

        Walked from the block's own entry rather than from a branch, because the
        modification is made before the flag is tested: a hook subtracts a base
        from a pointer, tests, and adds it back on its way out. Each state
        carries the flags whose branches it took, which is what names the cheats
        whose being off leads to an exit that never added it back.

        A flag is carried into both sides of its own branch. Which side runs
        when the flag is off is the switch's own key, which this does not read,
        and the dangerous shape is written either way round: a hook that tests
        for zero and jumps to its restore leaves the unbalanced exit on the side
        the branch falls through to. Attributing one side only reported such a
        flag as safe, which is the one answer this may not give.

        A path also carries whether something happened on it that this cannot
        read, and that never comes off again. An instruction that writes a
        register out of that register's own value is a modification like the
        `sub` above, without the `add` this would recognise as putting it back;
        a load into a register that is already carrying one loses the value the
        `add` was going to restore. Either way the exits after it are exits this
        cannot call balanced.
        """
        seen: set[tuple[int, tuple, bool, frozenset[str]]] = set()
        found: set[tuple[tuple, frozenset[str]]] = set()
        work: list[tuple[int, tuple, bool, frozenset[str]]] = [(start, (), False, frozenset())]
        while work:
            index, balance, opaque, because = work.pop()
            key = (index, balance, opaque, because)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_STATES or len(balance) > MAX_PENDING:
                return {((_UNREADABLE,), because)}
            if index >= len(self.lines):
                if balance or opaque:
                    found.add((balance, because))
                continue
            line = self.lines[index]
            state = list(balance)
            lea = _LEA.match(line)
            edit = _SUBADD.match(line)
            if lea:
                into, base, sign, other = (group.lower() for group in lea.groups())
                if into == base and sign == "-":
                    state.append((into, other))
                elif into == base and (into, other) in state:
                    state.remove((into, other))
                else:
                    opaque = True
            elif edit:
                kind, left, right = edit.group(1).lower(), edit.group(2).lower(), edit.group(3).lower()
                if kind == "sub":
                    state.append((left, right))
                elif (left, right) in state:
                    state.remove((left, right))
                else:
                    # An `add` with no `sub` to pair it with is a register
                    # changed out of its own value, which is what `inc` is and
                    # what an unpaired `lea` already counts as. Left alone, it
                    # is the restore a call made invisible.
                    opaque = True
            elif _CALL.match(line):
                # Whatever the callee does is on this path. Before any flag is
                # tested and with nothing waiting to be undone, it happens
                # whichever way the flag goes and a restore of it would be an
                # unpaired `add`; after either, the `add` this pairs may be
                # restoring a value the callee already replaced.
                opaque = opaque or bool(state) or bool(because)
            elif _STACK.match(line) and because:
                # After a flag is tested, a push or a pop is one side of the
                # branch moving the stack and, for a pop, replacing a register,
                # and this does not know which side is off.
                opaque = True
            elif _ANY_JUMP.match(line) and not (_JUMP.match(line) or _LABEL.match(line)):
                # A branch whose target this cannot name is not a line to walk
                # past. Where it goes is unknown, and so is what it undoes.
                found.add(((_UNREADABLE,), because))
                if not line.strip().lower().startswith("jmp"):
                    work.append((index + 1, tuple(state), True, because))
                continue
            elif not (_JUMP.match(line) or _RETURN.match(line) or _LABEL.match(line) or _DATA.match(line)):
                opaque = opaque or _changes_a_register(line, state, bool(because))
            now = tuple(state)
            hop = _JUMP.match(line)
            if hop:
                target, conditional = hop.group(2), hop.group(1).lower() != "jmp"
                test = _TEST.match(self.lines[index - 1]) if index >= 1 else None
                flag = test.group(1).casefold() if test and test.group(1).casefold() in self.at else None
                taken = because | {flag} if (conditional and flag) else because
                if target.endswith("Ret"):
                    # Returning to the game is this hook's exit: the injection
                    # template names that label for the point it came from, and
                    # what the game does next is not this question.
                    if now or opaque:
                        found.add((now, taken))
                elif target.casefold() not in self.at:
                    found.add(((_UNREADABLE,), taken))
                else:
                    work.append((self.at[target.casefold()], now, opaque, taken))
                if conditional:
                    work.append((index + 1, now, opaque, taken))
                continue
            if _RETURN.match(line):
                if now or opaque:
                    found.add((now, because))
                continue
            work.append((index + 1, now, opaque, because))
        return found


def flags_safe_to_switch_off(script: str | None) -> frozenset[str]:
    """Every symbol of this script whose flag can be written off safely, casefolded.

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
        if test and test.group(1).casefold() in reader.at:
            following = _JUMP.match(lines[index + 1]) if index + 1 < len(lines) else None
            if following and following.group(1).lower() != "jmp":
                tested.add(test.group(1).casefold())
                continue
        for word in _WORD.findall(line):
            if word.casefold() in reader.at:
                used_otherwise.add(word.casefold())

    refused: set[str] = set()
    for start in reader.at.values():
        for _balance, because in reader.unbalanced_exits(start):
            refused |= because
    return frozenset(tested - used_otherwise - refused)
