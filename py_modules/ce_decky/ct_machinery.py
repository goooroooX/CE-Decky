"""Which of a table's scripts change nothing the game runs until a record inside them is set.

Configure cheats lists a table's scripts as cheats, and many of them are not:
the script hooks one instruction, records a pointer the records inside it read,
and returns to the game having changed nothing, or it carries switches of its
own that all start off. A user has nothing to decide about such a script. It is
switched on whenever a record inside it is, which is the dependency rule the
panel already keeps, so the screen can put it behind **Scripts** with the
other machinery.

A table does not say which scripts those are, and neither a name nor the
default a table declares for a record is evidence: a script holding only
switches can still change the game with all of them off. So this reads the
script's own Auto Assembler code and proves one thing about it. The shape is a
hook. Every block is under a label the script allocated or declared, or is its
one patch of the game, which holds only the jump into the hook and what pads
it, with nothing written under the label after it. Along every branch of the
hook, each instruction writes memory the script allocated itself, changes a
register between its own push and pop of it, compares, or branches to a label
of its own; calls are not followed, so a call is a change, and the stack is
where the game left it on the way back. There is one way back, an unconditional
jump, and what runs last before it is exactly the bytes the patch covered, once
and in order: Cheat Engine's own `readmem` of the patched address and size, or
what its template lists between `INJECTING HERE` and `DONE INJECTING` when that
listing is as long as the patch and, where the script's own `aobscan` or
`assert` pins those bytes, is those bytes. A wildcard there is a byte the author
saw change, so the author's listing is not this game's. Flags the hook changes
are kept between `pushf` and `popf`, set again by that code before it reads
them, or set again by the game, per the listing, before it reads them.

Read at its start, a script's own memory holds what the script puts there: its
own `db` lines, or the zeroed pages every `alloc` is taken from again on each
start, where only a `globalalloc` keeps what the last start left
(`autoassembler.pas` at Cheat Engine `ec45d5f4`). A branch on one whole symbol
compared with a constant then goes only the way that value sends it, unless
the script writes that memory itself.

**It answers in one direction only.** Anything this does not recognise, Lua, a
jump it cannot place, a script past its bounds, is a change or may be one, and
the script stays a cheat the user can see. `verdict` names the first reason,
which is what `scripts/ct_scan_survey.py --scripts` counts over a corpus.
"""
from __future__ import annotations

import re
import struct

# A script longer than this is not read. A hook is tens of lines; this is a
# file somebody downloaded.
MAX_SCRIPT_CHARS = 256 * 1024
# A line longer than this is not read either, which keeps every pattern below
# linear in what it is handed.
MAX_LINE_CHARS = 512
# What a symbol's start value may hold. A compare reads at most eight bytes.
MAX_START_BYTES = 64
# The largest `alloc` read as zeroed memory, which is only ever compared.
MAX_ALLOC_BYTES = 1 << 16
# How deep a hook may push, and how many (block, stack) states one script may
# cost before it is not read. A hook pushes a few registers; one pushing in a
# loop is not a hook this follows.
MAX_STACK = 16
MAX_STATES = 4096

# `"game.exe"+F99FA: 8B 48 08  - mov ecx,[eax+08]`, and `Class.Method: …` at a method's first byte.
_LISTING = re.compile(
    r'^\s*(?://\s*)?"?[^"+\s][^"+]*"?(?:\s*\+\s*[0-9A-Fa-f]+)?\s*:\s*((?:[0-9A-Fa-f]{2}\s+)+)-\s+(.+?)\s*$')
# `@name:` is a label nothing declares, and `@@:` one that `@f` and `@b` reach.
# `Class:Method+c0:` is a Mono method in the game, which Cheat Engine's own
# templates hook by name.
_SCRIPT_LABEL = re.compile(
    r'^\s*(@@|@[A-Za-z_]\w*|[A-Za-z_][\w.]*(?:::?[A-Za-z_][\w.]*)+(?:\s*\+\s*[0-9A-Fa-f]+)?'
    r'|"?[\w.\- ]+"?(?:\s*\+\s*[0-9A-Fa-f]+)?|[A-Za-z_][\w.]*)\s*:\s*(.*)$')
_DIRECTIVE = re.compile(
    r"^\s*(alloc|globalalloc|dealloc|label|registersymbol|unregistersymbol|define|assert|aobscan\w*|"
    r"readmem|fullaccess|db|dw|dd|dq|dt|df|align)\b", re.IGNORECASE)
_ALLOC = re.compile(r"^\s*(?:alloc|globalalloc)\s*\(\s*([A-Za-z_]\w*)", re.IGNORECASE)
_SIZED_ALLOC = re.compile(r"^\s*alloc\s*\(\s*([A-Za-z_]\w*)\s*,\s*([^,)\s]+)", re.IGNORECASE)
# `label(code return)` declares two names, and `label(code,return)` does too.
_LABELS = re.compile(r"^\s*label\s*\(([^)]*)\)", re.IGNORECASE)
_DEFINE = re.compile(r"^\s*define\s*\(\s*([A-Za-z_]\w*)", re.IGNORECASE)
_GAME_NAMES = re.compile(r"^\s*(?:aobscan\w*|define)\s*\(\s*([A-Za-z_]\w*)", re.IGNORECASE)
_READMEM = re.compile(r"readmem\s*\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)$", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z_]\w*")
_LUA = re.compile(r"\{\$lua\}|\bluacall\b|\bsyntaxcheck\b", re.IGNORECASE)
_FAMILY = re.compile(r"^(?:[re]?([abcd])[xlh]|[re]?(si|di|bp|sp)l?|(r(?:8|9|1[0-5]))[dwb]?)$", re.IGNORECASE)
# What changes the flags register, which the game's own next branch reads.
_SETS_FLAGS = frozenset({
    "cmp", "test", "add", "sub", "and", "or", "xor", "inc", "dec", "neg", "adc", "sbb", "shl", "shr", "sar",
    "sal", "rol", "ror", "imul", "mul", "bt", "bts", "btr", "btc", "comiss", "ucomiss", "comisd", "ucomisd",
    "vcomiss", "vucomiss", "vcomisd", "vucomisd",
})
# The ones that set every flag a branch reads: `inc` keeps the carry, and a
# shift, a rotate or a multiply leaves some as they were.
_SETS_BRANCH_FLAGS = frozenset({
    "cmp", "test", "add", "sub", "and", "or", "xor", "neg", "adc", "sbb", "comiss", "ucomiss", "comisd",
    "ucomisd", "vcomiss", "vucomiss", "vcomisd", "vucomisd",
})
# Code that reads the flags before setting them reads the hook's; `cmov…` and
# `set…` do too, and a displaced jump is never a replay at all.
_READS_FLAGS = frozenset({"adc", "sbb", "rcl", "rcr", "pushf", "pushfd", "pushfq", "lahf"})
_SAVED_FLAGS = (frozenset({"pushf", "popf"}), frozenset({"pushfd", "popfd"}), frozenset({"pushfq", "popfq"}))
# Instructions that write their second operand as well as their first.
_WRITES_BOTH = frozenset({"xchg", "xadd", "cmpxchg"})
_DATA = re.compile(r"^(db|dw|dd|dq)\s+(.+)$", re.IGNORECASE)
_WIDTH = {"db": 1, "dw": 2, "dd": 4, "dq": 8, "byte": 1, "word": 2, "dword": 4, "qword": 8}
# `cmp byte ptr [flag],01`: a compare of one whole symbol with a constant.
_COMPARE = re.compile(r"^(cmp|test)\s+(?:(byte|word|dword|qword)\s+ptr\s*)?\[\s*([A-Za-z_]\w*)\s*\]\s*,\s*(\S+)$",
                      re.IGNORECASE)
# What each conditional jump asks of `a cmp b`, signed where the mnemonic is.
_CONDITIONS = {
    "je": "==", "jz": "==", "jne": "!=", "jnz": "!=",
    "jl": "<s", "jnge": "<s", "jle": "<=s", "jng": "<=s", "jg": ">s", "jnle": ">s", "jge": ">=s", "jnl": ">=s",
    "jb": "<", "jc": "<", "jnae": "<", "jbe": "<=", "jna": "<=", "ja": ">", "jnbe": ">", "jae": ">=", "jnb": ">=",
    "jnc": ">=",
}


def inert_alone(script: str | None) -> bool:
    """Whether this script, switched on by itself, provably changes nothing the game runs."""
    return bool(script) and verdict(script, at_start=True) is None


def verdict(script: str, at_start: bool = False) -> str | None:
    """None when the script provably changes nothing the game runs, else the first reason it may.

    Without `at_start` this holds whatever the script's own memory holds,
    which is a hook that only records a pointer. With it, that memory holds
    what the script puts there when it starts.
    """
    if len(script) > MAX_SCRIPT_CHARS:
        return "too_large"
    if _LUA.search(script):
        return "lua"
    # The template's listing: what the jump displaced, and what the game runs
    # after it.
    displaced: list[str] = []
    displaced_raw: list[str] = []
    after: list[str] = []
    marked = done = False
    for line in script.splitlines():
        if len(line) > MAX_LINE_CHARS:
            continue
        upper = line.upper()
        if "INJECTING HERE" in upper:
            marked = True
            continue
        if "DONE INJECTING" in upper:
            marked, done = False, True
            continue
        if (marked or done) and (match := _LISTING.match(line)):
            (displaced if marked else after).append(_normal(match.group(2)))
            if marked:
                displaced_raw += [byte.upper() for byte in match.group(1).split()]
    lines = [_without_comment(line).strip() for line in _enable_lines(re.sub(r"\{[^{}]*\}", "", script))]
    lines = [line for line in lines if line]
    if any(len(line) > MAX_LINE_CHARS for line in lines):
        return "too_large"
    own = {match.group(1).casefold() for line in lines if (match := _ALLOC.match(line))}
    own |= {name.casefold() for line in lines if (match := _LABELS.match(line))
            for name in re.split(r"[\s,]+", match.group(1)) if name}
    game = {match.group(1).casefold() for line in lines if (match := _GAME_NAMES.match(line))}
    allocated = {match.group(1).casefold(): match.group(2) for line in lines if (match := _SIZED_ALLOC.match(line))}

    # The script as blocks, each a label and the lines under it, in order.
    blocks: list[tuple[str, bool, list[str]]] = []
    for line in lines:
        if _DIRECTIVE.match(line) and not line.lower().startswith(("readmem", "db", "dw", "dd", "dq")):
            continue
        label = _SCRIPT_LABEL.match(line)
        if label:
            name = label.group(1).strip()
            if name.startswith("@"):
                name = f"@@{len(blocks)}" if name == "@@" else name
                own.add(name.casefold())
            at_game = '"' in name or "+" in name or ":" in name or name.casefold() in game
            blocks.append((name.strip('"').casefold(), at_game, []))
            line = label.group(2).strip()
            if not line:
                continue
        if not blocks:
            return "code_outside_a_label"
        blocks[-1][2].append(line)

    known: dict[str, bytes] = {}
    names = frozenset(own | game | {match.group(1).casefold() for line in lines if (match := _DEFINE.match(line))})
    if at_start:
        known = _start_values(blocks, own, allocated, names)

    # The one place in the game the script writes, and how many bytes of the
    # game's code that is: the replay has to be exactly those bytes, or an
    # instruction the jump covered is skipped, which is the commonest cheat
    # there is. A `jmp` or `call` to the hook is five bytes.
    patched = [name for name, at_game, _ in blocks if at_game]
    if len(patched) != 1:
        return "not_one_patch"
    # A label the script neither allocated nor declared is a symbol someone
    # else registered, another script's switch or an address in the game, and
    # whatever is written under it is assembled there.
    if any(not at_game and name not in own for name, at_game, _ in blocks):
        return "writes_another_symbol"
    entries: list[str] = []
    returns: set[str] = set()
    patch_size = 0
    for index, (name, at_game, body) in enumerate(blocks):
        if not at_game:
            if index and blocks[index - 1][1]:
                # What is written under it is assembled at the game's address
                # after the jump, into the game's own code.
                if body:
                    return "patch_is_not_a_jump"
                returns.add(name)
            continue
        for line in body:
            mnemonic = line.split(None, 1)[0].lower()
            if mnemonic in {"jmp", "call"} and _target(line) in own:
                entries.append(_target(line))
                patch_size += 5
            elif mnemonic == "nop":
                count = _count(line.split(None, 1)[1]) if " " in line else 1
                if count is None:
                    return "patch_is_not_a_jump"
                patch_size += count
            elif re.fullmatch(r"db(\s+90)+", line.lower()):
                patch_size += len(line.split()) - 1
            else:
                return "patch_is_not_a_jump"
    if not entries:
        return "patch_is_not_a_jump"
    starts = {name: index for index, (name, _, _) in enumerate(blocks)}
    # Every block a branch of the hook can reach, conditional or not, and
    # every way back to the game; a jump to a name this cannot place is a
    # branch it cannot follow. With `at_start`, a branch the script's own
    # memory decides is followed only the way it goes, and what a block runs
    # ends at the first jump that is always taken. Each block is entered once.
    reached: dict[int, int] = {}
    # Where each block goes: the jumps it takes, by position, to a block or,
    # as None, back to the game; and whether it runs on into the next block.
    jumps: dict[int, dict[int, int | None]] = {}
    falls: set[int] = set()
    exits: list[tuple[int, int, str]] = []
    queue = list(entries)
    while queue:
        index = starts.get(queue.pop())
        while index is not None and index not in reached:
            if index >= len(blocks) or blocks[index][1]:
                return "runs_into_the_game"
            body = blocks[index][2]
            following: int | None = index + 1
            end = len(body)
            for position, line in enumerate(body):
                mnemonic = line.split(None, 1)[0].lower()
                if not mnemonic.startswith("j"):
                    continue
                taken = True if mnemonic == "jmp" else (
                    _decided(body[position - 1], mnemonic, known, names) if known and position else None)
                if taken is False:
                    continue
                where = _target(line)
                if where in {"@f", "@b"}:
                    # The next anonymous label after the jump, or the last one
                    # at or before it.
                    order = range(index + 1, len(blocks)) if where == "@f" else range(index, -1, -1)
                    where = next((blocks[other][0] for other in order if blocks[other][0].startswith("@@")), "")
                if where in returns:
                    exits.append((index, position, "jmp" if taken else mnemonic))
                    jumps.setdefault(index, {})[position] = None
                elif where in starts:
                    queue.append(where)
                    jumps.setdefault(index, {})[position] = starts[where]
                else:
                    return "unfollowable_jump"
                if taken:
                    following, end = None, position + 1
                    break
            reached[index] = end
            if following is not None:
                falls.add(index)
            index = following
    # One way back, unconditional, with the displaced code run in the same
    # block before it: a branch can only land on a label, so nothing reaches
    # that jump without passing through the replay.
    if len(exits) != 1 or exits[0][2] != "jmp":
        return "not_one_way_back"
    exit_block, exit_at, _ = exits[0]
    before = blocks[exit_block][2][:exit_at]

    def replays(line: str) -> bool:
        return line.lower().startswith("readmem") or _normal(line) in displaced

    # The replay is the run of lines that ends at the jump back, and it is all
    # of the displaced code, once and in order: `readmem` of the patched
    # address and size, or exactly what the listing shows. An instruction after
    # it is the hook's, run on the game's own result.
    replay_at = len(before)
    while replay_at and replays(before[replay_at - 1]):
        replay_at -= 1
    replay = before[replay_at:]
    if not replay:
        return "skips_the_game_code"
    if all(line.lower().startswith("readmem") for line in replay):
        argument = _READMEM.match(replay[0])
        if len(replay) != 1 or argument is None or argument.group(1).strip('"').casefold() != patched[0] or \
                _size(argument.group(2)) != patch_size:
            return "skips_the_game_code"
    elif [_normal(line) for line in replay] != displaced or len(displaced_raw) != patch_size:
        return "skips_the_game_code"
    else:
        # The listing is the author's game. Where the script's own pattern
        # pins those bytes, they are this game's too; a wildcard there is a
        # byte the author saw change, and replaying the author's is a change.
        pinned = _pinned_bytes(lines, patched[0], patch_size)
        if pinned is not None and pinned != displaced_raw:
            return "skips_the_game_code"
    listed = len(displaced_raw) == patch_size
    # Everything else the hook runs, a displaced instruction run a second time
    # included, walked along every path with what that path has pushed: a
    # register is the hook's to change only between its own push and pop, the
    # flags are kept only between `pushf` and `popf`, and the stack is back
    # where the game left it on the way back and where the replay starts.
    unsaved_flags = False
    states = [(starts[entry], ()) for entry in entries]
    seen: set[tuple[int, tuple[str, ...]]] = set()
    while states:
        index, carried = states.pop()
        if (index, carried) in seen:
            continue
        seen.add((index, carried))
        if len(seen) > MAX_STATES:
            return "too_large"
        stack = list(carried)
        taken = jumps.get(index, {})
        for at, line in enumerate(blocks[index][2][:reached[index]]):
            if index == exit_block and at >= replay_at:
                if at == replay_at and stack:
                    return "unbalanced_stack"
                if at < exit_at:
                    continue
            mnemonic, _, operands = line.partition(" ")
            mnemonic = mnemonic.lower()
            operands = operands.strip()
            if mnemonic.startswith("j"):
                if at in taken:
                    if taken[at] is None:
                        if stack:
                            return "unbalanced_stack"
                    else:
                        states.append((taken[at], tuple(stack)))
                continue
            if any(mnemonic in pair for pair in _SAVED_FLAGS):
                if mnemonic.startswith("push"):
                    stack.append("flags")
                elif not stack or stack.pop() != "flags":
                    return "unbalanced_stack"
                continue
            if mnemonic == "push":
                stack.append(_family(operands) or operands.casefold())
                if len(stack) > MAX_STACK:
                    return "unbalanced_stack"
                continue
            if mnemonic == "pop":
                if not stack or stack.pop() != (_family(operands) or operands.casefold()):
                    return "unbalanced_stack"
                continue
            if mnemonic in _SETS_FLAGS and "flags" not in stack:
                unsaved_flags = True
            if mnemonic in {"cmp", "test", "nop"}:
                continue
            if mnemonic == "call":
                return "calls"
            if not operands or mnemonic.startswith("readmem"):
                return "unread_instruction"
            targets = [operands.split(",", 1)[0].strip()]
            if mnemonic in _WRITES_BOTH and "," in operands:
                targets.append(operands.split(",", 1)[1].strip())
            for operand in targets:
                if "[" in operand:
                    base = _WORD.search(operand.split("[", 1)[1])
                    if base is None or base.group(0).casefold() not in own:
                        return "writes_game_memory"
                elif _family(operand) is None or _family(operand) not in stack:
                    return "changes_a_register"
        if index in falls:
            states.append((index + 1, tuple(stack)))
    if unsaved_flags:
        # Flags the hook changes reach the game's next branch unless the
        # displaced code, run last, sets every one a branch reads again, or
        # the game does before it reads them; only the listing shows either,
        # and only where it is the patch, byte for byte.
        resets = False
        for instruction in displaced if listed else []:
            opcode = instruction.split(None, 1)[0]
            if opcode.startswith(("cmov", "set")) or opcode in _READS_FLAGS:
                break
            if opcode in _SETS_BRANCH_FLAGS:
                resets = True
                break
        if not (resets or (listed and _flags_unread(after))):
            return "flags_reach_the_game"
    return None


def _pinned_bytes(lines: list[str], patched: str, size: int) -> list[str] | None:
    """The bytes the script's own `aobscan` or `assert` pins at the patch, or None where nothing does.

    A wildcard among them comes back as itself, which matches no listing.
    """
    patterns: dict[str, list[str]] = {}
    texts: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^\s*(aobscan\w*|assert|define)\s*\((.*)\)\s*$", line, re.IGNORECASE)
        if match is None:
            continue
        arguments = [argument.strip() for argument in match.group(2).split(",")]
        if len(arguments) < 2 or not _WORD.fullmatch(arguments[0]):
            continue
        name = arguments[0].casefold()
        if match.group(1).lower() == "define":
            value = arguments[1]
            texts[name] = value
            offset = re.fullmatch(r"([A-Za-z_]\w*)\s*\+\s*([0-9A-Fa-f]+)", value)
            if offset and offset.group(1).casefold() in patterns:
                patterns[name] = patterns[offset.group(1).casefold()][int(offset.group(2), 16):]
            continue
        pattern = texts.get(arguments[-1].casefold(), arguments[-1])
        tokens = pattern.split() if " " in pattern.strip() else [pattern[at:at + 2] for at in range(0, len(pattern), 2)]
        patterns[name] = [token.upper() for token in tokens]
    base, _, offset = patched.rpartition("+") if "+" in patched else (patched, "", "0")
    if base.strip('"').casefold() not in patterns:
        return None
    try:
        start = int(offset.strip(), 16)
    except ValueError:
        return ["??"]
    covered = patterns[base.strip('"').casefold()][start:start + size]
    return covered if len(covered) == size else ["??"]


def _start_values(blocks: list, own: set[str], allocated: dict[str, str], names: frozenset[str]) -> dict[str, bytes]:
    """What the script's own memory holds when it has just been switched on.

    A label's leading `db`/`dw`/`dd`/`dq` lines, or else zeros for an `alloc`.
    A symbol the script writes itself, a `globalalloc`, and anything this
    cannot assemble are left out, which leaves every branch on them followed
    both ways; so is everything, once the script writes its own memory at an
    offset, because `[newmem+200]` may be a label inside `newmem`.
    """
    written: set[str] = set()
    for _, _, body in blocks:
        for line in body:
            mnemonic, _, operands = line.partition(" ")
            if _DATA.match(line) or not operands or mnemonic.lower() in {"cmp", "test", "push"} or \
                    mnemonic.lower().startswith("j"):
                continue
            parts = [operands.split(",", 1)[0]]
            if mnemonic.lower() in _WRITES_BOTH and "," in operands:
                parts.append(operands.split(",", 1)[1])
            for part in parts:
                if "[" not in part:
                    continue
                inside = part.split("[", 1)[1].split("]", 1)[0].strip().casefold()
                base = _WORD.search(inside)
                if base is None:
                    continue
                if base.group(0) in own and inside != base.group(0):
                    return {}
                written.add(base.group(0))
    known: dict[str, bytes] = {}
    for name, at_game, body in blocks:
        if at_game or name in written:
            continue
        data: bytearray | None = bytearray()
        for line in body:
            match = _DATA.match(line)
            if not match or len(data) >= MAX_START_BYTES:
                break
            width = _WIDTH[match.group(1).lower()]
            for item in re.split(r"[\s,]+", match.group(2).strip()):
                value = None if item.casefold() in names else _constant(item, width)
                if value is None or len(data) >= MAX_START_BYTES:
                    data = None if value is None else data
                    break
                data += value.to_bytes(width, "little")
            if data is None:
                break
        if data:
            known.setdefault(name, bytes(data))
    for name, size in allocated.items():
        zeroes = _constant(size, 4)
        if name not in written and name not in known and zeroes and zeroes <= MAX_ALLOC_BYTES:
            known[name] = bytes(min(zeroes, MAX_START_BYTES))
    return known


def _decided(compare: str, jump: str, known: dict[str, bytes], names: frozenset[str] = frozenset()) -> bool | None:
    """Whether `jump` after `compare` is taken, when the compare reads a known symbol.

    `names` are the script's own: `bad` is a number to `_constant` and may be
    a `define` to Cheat Engine, so a name is never read as one.
    """
    match = _COMPARE.match(compare.strip())
    if match is None:
        return None
    operation, size, symbol, operand = match.groups()
    width = _WIDTH[(size or "dword").lower()]
    held = known.get(symbol.casefold())
    other = None if operand.casefold() in names else _constant(operand, width)
    if held is None or len(held) < width or other is None:
        return None
    value = int.from_bytes(held[:width], "little")
    if operation.lower() == "test":
        result = value & other
        return {"je": result == 0, "jz": result == 0, "jne": result != 0, "jnz": result != 0}.get(jump)
    condition = _CONDITIONS.get(jump)
    if condition is None:
        return None
    if condition.endswith("s"):
        sign = 1 << (8 * width - 1)
        value, other, condition = (value ^ sign) - sign, (other ^ sign) - sign, condition[:-1]
    return {"==": value == other, "!=": value != other, "<": value < other, "<=": value <= other,
            ">": value > other, ">=": value >= other}[condition]


def _constant(text: str, width: int) -> int | None:
    """One Auto Assembler constant as the `width` bytes it assembles to, or None."""
    text = text.strip()
    try:
        if text.lower().startswith("(float)"):
            return int.from_bytes(struct.pack("<f", float(text[7:])), "little") if width == 4 else None
        if text.lower().startswith("(double)"):
            return int.from_bytes(struct.pack("<d", float(text[8:])), "little") if width == 8 else None
        if text.lower().startswith("(int)"):
            value = int(text[5:], 10)
        elif text.startswith("#"):
            value = int(text[1:], 10)
        else:
            value = int(text.removeprefix("$").removeprefix("0x"), 16)
    except (ValueError, OverflowError, struct.error):
        return None
    return value % (1 << (8 * width)) if -(1 << (8 * width - 1)) <= value < (1 << (8 * width)) else None


def _size(text: str) -> int | None:
    """A `readmem` size, which Cheat Engine reads with Pascal's `StrToInt`: decimal, or `$` hex."""
    text = text.strip()
    try:
        return int(text[1:], 16) if text.startswith("$") else int(text, 16) if text.lower().startswith("0x") \
            else int(text, 10)
    except ValueError:
        return None


def _count(text: str) -> int | None:
    """How many bytes `nop N` pads, where one digit reads the same in either base and more does not."""
    text = text.strip()
    if text.startswith("#") and text[1:].isdigit():
        return int(text[1:])
    if text.startswith("$"):
        return _size(text)
    return int(text) if len(text) == 1 and text.isdigit() else None


def _flags_unread(after: list[str]) -> bool:
    """Whether the game, from where the hook returns, sets the flags before it reads them.

    A `ret` or a `call` counts as setting them: no caller or callee relies on
    the flags across one. A jump goes where the listing does not show.
    """
    for instruction in after:
        opcode = instruction.split(None, 1)[0]
        if opcode.startswith(("j", "cmov", "set", "loop")) or opcode in _READS_FLAGS:
            return False
        if opcode in _SETS_BRANCH_FLAGS or opcode in {"ret", "call"}:
            return True
    return False


def _target(line: str) -> str:
    return line.split(None, 1)[1].split()[-1].casefold() if len(line.split(None, 1)) == 2 else ""


def _family(register: str) -> str | None:
    """`rbx`, `ebx`, `bx` and `bl` are one register to a push; None for anything else."""
    match = _FAMILY.match(register.strip())
    return next((group.lower() for group in match.groups() if group), None) if match else None


def _normal(instruction: str) -> str:
    """One instruction as text a listing and a script both write the same way."""
    text = re.sub(r"\s+", " ", instruction.strip().lower())
    text = re.sub(r"\s*,\s*", ",", text)
    text = re.sub(r"\b(?:byte|word|dword|qword|xmmword) ptr\s*", "", text)
    # `[rbp+000000B0]` in a listing is `[rbp+B0]` in the script that copied it.
    return re.sub(r"\b0+([0-9a-f]+)\b", r"\1", text)


def _enable_lines(script: str) -> list[str]:
    """What runs on start: `[ENABLE]`, and what comes before it, which Cheat Engine runs for both sections."""
    lines = script.splitlines()
    stop = next((index for index, line in enumerate(lines) if line.strip().upper() == "[DISABLE]"), len(lines))
    return [line for line in lines[:stop] if line.strip().upper() != "[ENABLE]"]


def _without_comment(line: str) -> str:
    return line.split("//", 1)[0]
