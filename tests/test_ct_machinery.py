"""Which scripts change nothing the game runs until a record inside them is set."""
from __future__ import annotations

from ce_decky import ct_machinery as machinery


def proven(script: str, at_start: bool = False) -> bool:
    return machinery.verdict(script, at_start=at_start) is None


CAPTURE = """[ENABLE]
aobscanmodule(objStats,$process,8B 81 38 0A 00 00 41 89 00)
alloc(newmem,$500,objStats)
alloc(pStats,8)
label(return)
registersymbol(objStats pStats)
newmem:
  mov [pStats],rcx
  readmem(objStats,6)
  jmp return
objStats:
  jmp newmem
  nop
return:
[DISABLE]
objStats:
  readmem(origStats,6)
"""

LISTED = """{ header }
[ENABLE]
aobscanmodule(INJECT,game.exe,8B 48 08 83 39 00)
alloc(newmem,$1000)
alloc(pDiff,4)
label(code)
label(return)
newmem:
code:
  push rbx
  mov rbx,[eax+10]
  mov [pDiff],rbx
  pop rbx
  mov ecx,[eax+08]
  cmp dword ptr [ecx],00
  jmp return
INJECT:
  jmp newmem
  nop
return:
[DISABLE]
INJECT:
  db 8B 48 08 83 39 00
{
// ---------- INJECTING HERE ----------
"game.exe"+F99FA: 8B 48 08              -  mov ecx,[eax+08]
"game.exe"+F99FD: 83 39 00              -  cmp dword ptr [ecx],00
// ---------- DONE INJECTING  ----------
"game.exe"+F9A00: 7E 05                 -  jle game.exe+F9A07
}
"""


def test_a_hook_that_only_records_a_pointer_changes_nothing():
    assert proven(CAPTURE)
    # The template's own record of what the jump displaced, when it has no readmem.
    assert proven(LISTED)


def test_a_hook_that_changes_anything_is_a_cheat():
    # Back to the game without running what the jump displaced: the game's own
    # write is skipped, which is the whole cheat.
    assert not proven(CAPTURE.replace("  readmem(objStats,6)\n  jmp return", "  jmp return"))
    # A branch that writes the game's memory, reached only conditionally.
    branch = CAPTURE.replace("  mov [pStats],rcx\n", "  cmp rcx,0\n  je other\n  mov [pStats],rcx\n").replace(
        "objStats:\n  jmp newmem", "other:\n  mov [rcx+10],0\n  readmem(objStats,6)\n  jmp return\nobjStats:\n  jmp newmem")
    assert not proven(branch.replace("label(return)", "label(return other)"))
    # A register the hook did not save.
    assert not proven(LISTED.replace("  push rbx\n", "").replace("  pop rbx\n", ""))
    # A branch to a place this cannot name, Lua, and an instruction the listing does not hold.
    assert not proven(CAPTURE.replace("  mov [pStats],rcx\n", "  je elsewhere\n  mov [pStats],rcx\n"))
    assert not proven("{$lua}\n" + CAPTURE)
    # Only the hook's copy: the listing writes the same line, and is what it is compared with.
    assert not proven(LISTED.replace("  cmp dword ptr [ecx],00\n", "  cmp dword ptr [ecx],01\n", 1))
    # A compare the game's next branch would read in place of its own.
    assert not proven(CAPTURE.replace("  mov [pStats],rcx\n", "  cmp rcx,0\n  mov [pStats],rcx\n"))
    saved = CAPTURE.replace("  mov [pStats],rcx\n", "  pushfq\n  cmp rcx,0\n  popfq\n  mov [pStats],rcx\n")
    assert proven(saved)
    # Something of the hook's own after the replay, on the game's result.
    assert not proven(CAPTURE.replace("  readmem(objStats,6)\n", "  readmem(objStats,6)\n  mov [pStats],rcx\n"))
    # Two ways back to the game, so one of them can skip the replay.
    assert not proven(CAPTURE.replace("  mov [pStats],rcx\n", "  cmp rcx,0\n  je return\n  mov [pStats],rcx\n"))
    # Code under the label after the jump is assembled into the game's own.
    assert not proven(CAPTURE.replace("return:\n[DISABLE]", "return:\n  mov eax,1\n[DISABLE]"))
    # A register taken off the stack that the hook never put there.
    assert not proven(LISTED.replace("  pop rbx\n", "  pop rax\n"))
    # A call runs whatever it reaches, even through the script's own memory.
    assert not proven(CAPTURE.replace("  mov [pStats],rcx\n", "  call [pStats]\n"))
    # An exchange writes the game's memory through its second operand.
    assert not proven(LISTED.replace("  mov [pDiff],rbx\n", "  xchg rbx,[eax+10]\n"))
    # A displaced instruction run twice, and a replay out of order.
    assert not proven(LISTED.replace("  push rbx\n", "  mov ecx,[eax+08]\n  push rbx\n"))
    reordered = LISTED.replace("  mov ecx,[eax+08]\n  cmp dword ptr [ecx],00\n", "  cmp dword ptr [ecx],00\n  mov ecx,[eax+08]\n")
    assert not proven(reordered)
    # A readmem of no address this script scanned for, or one written without its parentheses.
    assert not proven(CAPTURE.replace("  readmem(objStats,6)\n  jmp", "  readmem(pStats,6)\n  jmp"))
    assert not proven(CAPTURE.replace("  readmem(objStats,6)\n  jmp", "  readmem objStats,6\n  jmp"))
    # A displaced `inc` leaves the carry the hook's compare set.
    inc = LISTED.replace("  pop rbx\n", "  cmp rbx,0\n  pop rbx\n")
    assert proven(inc)
    assert not proven(inc.replace("cmp dword ptr [ecx],00", "inc dword ptr [ecx]"))
    # Displaced code that reads the flags before its own compare reads the hook's.
    assert not proven(inc.replace("mov ecx,[eax+08]", "setne al"))
    # Every name a `label` declares is the script's own.
    two = CAPTURE.replace("label(return)", "label(return spare)").replace("  mov [pStats],rcx\n", "  mov [spare],rcx\n")
    assert proven(two)


GATED = """[ENABLE]
aobscanmodule(objItems,$process,66 44 89 43 0A 45 84 E4)
alloc(newmem,$500,objItems)
alloc(bMaxItems,1)
label(return)
registersymbol(objItems bMaxItems)
newmem:
  cmp byte ptr [bMaxItems],1
  je @f
  readmem(objItems,5)
  jmp return
@@:
  push rax
  mov rax,[rbx+0C]
  mov [rbx+0A],ax
  pop rax
  jmp return
objItems:
  jmp newmem
return:
[DISABLE]
objItems:
  db 66 44 89 43 0A
dealloc(*)
{
// ---------- INJECTING HERE ----------
"game.exe"+276287: 66 44 89 43 0A           - mov [rbx+0A],r8w
// ---------- DONE INJECTING  ----------
"game.exe"+27628C: 45 84 E4                 - test r12b,r12b
"game.exe"+27628F: 74 20                    - je game.exe+2762B1
}
"""


def test_a_hook_its_own_switches_hold_off_is_idle_until_one_is_set():
    # Switched on alone, its switch is the zeroed page `alloc` hands back, so
    # it only replays; the other branch is the cheat a record inside sets.
    assert not proven(GATED)
    assert proven(GATED, at_start=True)
    # A start value the script writes itself, either way.
    assert proven(GATED.replace("newmem:\n", "bMaxItems:\n  db 0\nnewmem:\n", 1), at_start=True)
    assert not proven(GATED.replace("newmem:\n", "bMaxItems:\n  db 1\nnewmem:\n", 1), at_start=True)
    # Memory that keeps what the last start left, or that the hook writes.
    assert not proven(GATED.replace("alloc(bMaxItems,1)", "globalalloc(bMaxItems,1)"), at_start=True)
    written = GATED.replace("  cmp byte ptr [bMaxItems],1\n", "  mov byte ptr [bMaxItems],1\n  cmp byte ptr [bMaxItems],1\n")
    assert not proven(written, at_start=True)
    # The hook's compare reaches a game that reads the flags next.
    assert not proven(GATED.replace("test r12b,r12b", "cmovne eax,ecx"), at_start=True)
    # A replay shorter than what the jump covered skips an instruction of the game's.
    assert not proven(GATED.replace("readmem(objItems,5)\n  jmp", "readmem(objItems,6)\n  jmp"), at_start=True)
    assert not proven(CAPTURE.replace("  jmp newmem\n  nop\n", "  jmp newmem\n"))
    assert not proven(LISTED.replace("  jmp newmem\n  nop\n", "  jmp newmem\n  nop 2\n"))
    # A `define` is a name, however much it looks like hex: `c` here is 0 and sends the hook into its cheat.
    named = GATED.replace("cmp byte ptr [bMaxItems],1", "cmp byte ptr [bMaxItems],c").replace("label(return)", "label(return)\ndefine(c,0)")
    assert not proven(named, at_start=True)
    # A write into its own memory at an offset may be the switch itself.
    offset = GATED.replace("  cmp byte ptr [bMaxItems],1\n", "  mov byte ptr [newmem+100],1\n  cmp byte ptr [bMaxItems],1\n")
    assert not proven(offset, at_start=True)
    # A listing longer than the patch says nothing about what the game runs next.
    assert not proven(GATED.replace("66 44 89 43 0A           -", "66 44 89 43 0A 00        -"), at_start=True)
    # Bytes under a symbol the script never declared land in someone else's memory.
    assert not proven(CAPTURE.replace("newmem:\n", "otherFlag:\n  db 0\nnewmem:\n", 1))


def test_a_branch_on_a_known_symbol_is_decided_the_way_cheat_engine_would():
    known = {"flag": b"\xff", "wide": (1).to_bytes(4, "little")}
    assert machinery._decided("cmp byte ptr [flag],FF", "je", known) is True
    assert machinery._decided("cmp byte ptr [flag],01", "jl", known) is True
    assert machinery._decided("cmp byte ptr [flag],01", "jb", known) is False
    assert machinery._decided("test byte ptr [flag],01", "jz", known) is False
    assert machinery._decided("cmp [wide],#1", "jne", known) is False
    # A wider read than the symbol holds, a flag a compare does not decide, or a register.
    assert machinery._decided("cmp dword ptr [flag],0", "je", known) is None
    assert machinery._decided("test byte ptr [flag],01", "jl", known) is None
    assert machinery._decided("cmp eax,1", "je", known) is None
    # `readmem` sizes are Pascal's StrToInt; `nop 10` says no base.
    assert (machinery._size("10"), machinery._size("$10"), machinery._count("3"), machinery._count("10")) == (10, 16, 3, None)
    assert machinery._flags_unread(["mov eax,ecx", "ret"]) and not machinery._flags_unread(["mov eax,ecx", "jne x"])


def test_what_the_corpus_showed_a_hook_looks_like_is_read_and_what_changes_the_game_is_not():
    # Cheat Engine runs what comes before `[ENABLE]` for both sections, and a
    # Mono template names its patch there; its listing has no offset.
    mono = LISTED.replace("aobscanmodule(INJECT,game.exe,8B 48 08 83 39 00)\n", "").replace(
        "{ header }\n", "define(INJECT,Game.Stats.Read)\n").replace('"game.exe"+F99FA: ', "Game.Stats.Read: ")
    assert proven(mono)
    # A method hooked by its own name and offset.
    named = CAPTURE.replace("objStats:\n  jmp newmem", "Game.Stats:Read+1c:\n  jmp newmem").replace(
        "readmem(objStats,6)\n  jmp", "readmem(Game.Stats:Read+1c,6)\n  jmp")
    assert proven(named)
    # A wildcard over the patched bytes is a byte the author saw change: their listing is not this game's.
    assert not proven(LISTED.replace("8B 48 08 83 39 00)", "8B 48 ?? 83 39 00)"))
    # One past them pins nothing the hook replays.
    assert proven(LISTED.replace("8B 48 08 83 39 00)", "8B 48 08 83 39 00 ??)"))
    # A register changed before it is pushed is handed back changed.
    early = LISTED.replace("  push rbx\n  mov rbx,[eax+10]\n", "  mov rbx,[eax+10]\n  push rbx\n")
    assert machinery.verdict(early) == "changes_a_register"
    # Saved flags may span the hook's own jumps, but not its way back.
    spanning = CAPTURE.replace("  mov [pStats],rcx\n  readmem(objStats,6)\n  jmp return\n",
                               "  pushfq\n  cmp rcx,0\n  je store\n  jmp store\nstore:\n  mov [pStats],rcx\n  popfq\n"
                               "  readmem(objStats,6)\n  jmp return\n").replace("label(return)", "label(return store)")
    assert proven(spanning)
    assert machinery.verdict(spanning.replace("  popfq\n", "")) == "unbalanced_stack"
