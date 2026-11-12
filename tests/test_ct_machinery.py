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
alloc(newmem,$1000,INJECT)
alloc(pDiff,8)
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
    assert not proven(two)
    assert proven(two.replace("objStats:\n  jmp newmem", "spare:\n  dq 0\nobjStats:\n  jmp newmem", 1))


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
    # Both are read as far as their replay, which nothing pins to this game's bytes.
    assert machinery.verdict(mono) == "replay_unproved"
    # A method hooked by its own name and offset.
    named = CAPTURE.replace("objStats:\n  jmp newmem", "Game.Stats:Read+1c:\n  jmp newmem").replace(
        "readmem(objStats,6)\n  jmp", "readmem(Game.Stats:Read+1c,6)\n  jmp")
    assert machinery.verdict(named) == "replay_unproved"
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


def test_only_instructions_whose_every_effect_is_named_write_only_the_scripts_own_data():
    def hook(lines: str) -> str | None:
        return machinery.verdict(CAPTURE.replace("  mov [pStats],rcx\n", lines))

    # What the corpus's captures write: the whole symbol, or a part of it it declares.
    assert hook("  mov dword ptr [pStats+4],ecx\n") is None
    assert hook("  mov [pStats+4],ecx\n") is None
    # Past what the symbol holds, or anywhere a register moves it.
    assert hook("  mov [pStats+4],rcx\n") == "writes_game_memory"
    # Memory that keeps what the last start left is still the script's own.
    assert machinery.verdict(CAPTURE.replace("alloc(pStats,8)", "globalalloc(pStats,8)")) is None
    assert machinery.verdict(CAPTURE.replace("alloc(pStats,8)", "globalalloc(pStats,4)")) == "writes_game_memory"
    assert hook("  mov [pStats+rax],rcx\n") == "writes_game_memory"
    assert hook("  mov [rax+pStats],rcx\n") == "writes_game_memory"
    # The label after the patch is the game's code, whatever the script calls it.
    assert hook("  mov [return],rcx\n") == "writes_game_memory"
    # Its own code is not its data.
    assert hook("  mov [newmem],rcx\n") == "writes_game_memory"
    # Effects no operand names: rdx:rax, rax, the x87 stack.
    assert hook("  div [pStats]\n") == "unread_instruction"
    assert hook("  imul [pStats]\n") == "unread_instruction"
    assert hook("  push rcx\n  cmpxchg [pStats],rcx\n  pop rcx\n") == "unread_instruction"
    assert hook("  fld [pStats]\n") == "unread_instruction"
    # A bit index in a register reaches past the operand, and a shift's count is no size.
    assert hook("  push rax\n  bts [pStats],rax\n  pop rax\n") == "unread_instruction"
    assert machinery.verdict(CAPTURE.replace("alloc(pStats,8)", "alloc(pStats,1)").replace(
        "  mov [pStats],rcx\n", "  pushfq\n  shl [pStats],cl\n  popfq\n")) == "writes_game_memory"
    assert hook("  pushfq\n  shl qword ptr [pStats],cl\n  popfq\n") is None
    assert hook("  pushfq\n  push rax\n  imul rax,[pStats]\n  pop rax\n  popfq\n") is None


def test_a_readmem_replay_runs_the_same_only_where_its_bytes_do_not_depend_on_where_they_run():
    # `mov eax,[rip+0A38]` copied into the hook reads somewhere else.
    assert machinery.verdict(CAPTURE.replace("8B 81 38 0A 00 00", "8B 05 38 0A 00 00")) == "replay_unproved"
    # A relative call, and a pattern with a wildcard where the bytes are.
    assert machinery.verdict(CAPTURE.replace("8B 81 38 0A 00 00", "E8 81 38 0A 00 00")) == "replay_unproved"
    assert machinery.verdict(CAPTURE.replace("8B 81 38 0A 00 00", "8B 81 ?? 0A 00 00")) == "replay_unproved"
    # A listing replay of an address no pattern pins.
    unpinned = LISTED.replace("aobscanmodule(INJECT,game.exe,8B 48 08 83 39 00)", "define(INJECT,game.exe+F99FA)")
    assert machinery.verdict(unpinned) == "replay_unproved"
    assert machinery._position_independent("66 44 89 43 0A".split())
    assert machinery._position_independent("48 8B 44 24 20 C7 40 08 01 00 00 00".split())
    assert machinery._position_independent("0F B6 05 10 00 00 00".split()) is False
    assert machinery._position_independent("8B 81 38".split()) is False
    assert machinery._position_independent("EB 05".split()) is False
    assert machinery._position_independent([]) is False


def test_a_line_no_branch_runs_is_still_read_as_what_cheat_engine_does_with_it():
    after_the_way_back = "  jmp return\nobjStats:"
    assert after_the_way_back in CAPTURE

    def placed(lines: str) -> str:
        return CAPTURE.replace(after_the_way_back, f"  jmp return\n{lines}\nobjStats:")

    # A command runs wherever it is written, past the jump back included.
    assert machinery.verdict(placed("createthread(newmem)")) == "unread_line"
    assert machinery.verdict(placed("loadlibrary(other.dll)")) == "unread_line"
    # A label this does not recognise puts what is under it in the game.
    for label in ("game.exe+1234+10:", "[game.exe+1234]+10:", "game.exe+0x1234:"):
        assert machinery.verdict(placed(f"{label}\n  db 90 90 90")) == "unread_line"
    # In a block of the script's own that nothing jumps to, too.
    unreached = placed("other:\ncreatethread(newmem)").replace("label(return)", "label(return other)")
    assert machinery.verdict(unreached) == "unread_line"
    # Bytes the script keeps for itself there are still only its own memory.
    saved = placed("saved:\n  readmem(objStats,6)\n  db 00 00\n  align 10").replace("label(return)", "label(return saved)")
    assert proven(saved)


def test_whatever_assembles_bytes_inside_the_patch_is_a_change_to_the_game():
    patch = "  jmp newmem\n  nop\n"
    assert patch in CAPTURE
    for line in ("dt 1.0", "df 1.0", "align 10"):
        assert machinery.verdict(CAPTURE.replace(patch, f"{patch}  {line}\n")) == "patch_is_not_a_jump"
        assert machinery.verdict(CAPTURE.replace("return:\n[DISABLE]", f"return:\n  {line}\n[DISABLE]")) == "patch_is_not_a_jump"
    # Entered by a call and left by a jump, the hook leaves the call's return
    # address on the game's stack.
    assert machinery.verdict(CAPTURE.replace("objStats:\n  jmp newmem", "objStats:\n  call newmem", 1)) == "patch_is_not_a_jump"


def test_text_this_does_not_take_out_as_cheat_engine_would_is_never_read_as_the_script():
    wildcard = LISTED.replace("8B 48 08 83 39 00)", "8B 48 ?? 83 39 00)", 1)
    assert machinery.verdict(wildcard) == "replay_unproved"
    # A commented-out scan must not pin the bytes the real one leaves open.
    exact = "aobscanmodule(INJECT,game.exe,8B 48 08 83 39 00)"
    for comment in (f"/*\n{exact}\n*/", f"  nop /*\n{exact}\n  nop */"):
        assert machinery.verdict(wildcard.replace("  jmp return\nINJECT:", f"  jmp return\n{comment}\nINJECT:")) == "block_comment"
    # Braces are a comment to this reader and a C block to Cheat Engine.
    compiled = CAPTURE.replace("  mov [pStats],rcx\n", "  mov [pStats],rcx\n{$ccode base=rcx}\n{ *(int*)(base+0x10) = 999; }\n{$asm}\n")
    assert machinery.verdict(compiled) == "compiler_directive"


def test_a_hook_whose_jump_may_be_the_long_one_is_not_proved():
    # In a 64-bit game, memory nothing asked for near the patch may be more
    # than 2 GiB from it, and the jump there is fourteen bytes over a patch
    # padded and replayed for five.
    far = CAPTURE.replace("alloc(newmem,$500,objStats)", "alloc(newmem,$500)")
    assert far != CAPTURE and machinery.verdict(far) == "jump_may_be_long"
    # Any allocation asked for near an address takes the others with it.
    assert proven(far.replace("alloc(pStats,8)", "alloc(pStats,8,objStats)"))
    # Near an address is near the patch only where that address is the patch:
    # beside another module, or at a distance from the patch, proves nothing.
    for elsewhere in ("kernel32.dll", '"other.dll"+1000', "objStats+7FFFFFFF", "pStats"):
        assert machinery.verdict(CAPTURE.replace("alloc(newmem,$500,objStats)", f"alloc(newmem,$500,{elsewhere})")) \
            == "jump_may_be_long"
        assert machinery.verdict(far.replace("alloc(pStats,8)", f"alloc(pStats,8,{elsewhere})")) == "jump_may_be_long"
        # The hook sent elsewhere is not brought back by data kept near the patch.
        assert machinery.verdict(CAPTURE.replace("alloc(newmem,$500,objStats)", f"alloc(newmem,$500,{elsewhere})")
                                 .replace("alloc(pStats,8)", "alloc(pStats,8,objStats)")) == "jump_may_be_long"
    # Every allocation near the patch, or the hook near it and the data left to follow, is.
    assert proven(CAPTURE.replace("alloc(pStats,8)", "alloc(pStats,8,objStats)"))
    # The patched address spelled as its label is, quotes and case aside.
    assert proven(CAPTURE.replace("alloc(newmem,$500,objStats)", 'alloc(newmem,$500, "OBJSTATS" )'))
    # Named only in the listing, a 64-bit register still says which game this is.
    listed = LISTED.replace("alloc(newmem,$1000,INJECT)", "alloc(newmem,$1000)")
    assert machinery.verdict(listed) == "jump_may_be_long"
    # A script naming none says nothing about the game: a hook of a 64-bit
    # one may be written with 32-bit registers alone, and it used to be taken
    # for a 32-bit game's and proved.
    narrow = listed.replace("rbx", "ebx")
    assert "rbx" in listed and "rbx" not in narrow and machinery.verdict(narrow) == "jump_may_be_long"
    # Asking for its memory near the patch proves the same script in either.
    assert proven(LISTED.replace("rbx", "ebx"))
    # An instruction Cheat Engine cannot assemble for a 64-bit game is what
    # says the game is 32-bit, where the jump is always five.
    x86 = narrow.replace("  push ebx\n", "  pushfd\n  push ebx\n").replace("  pop ebx\n", "  pop ebx\n  popfd\n")
    assert x86 != narrow and proven(x86)
    # Where its memory is asked for does not matter to it.
    assert proven(x86.replace("alloc(newmem,$1000)", "alloc(newmem,$1000,kernel32.dll)"))
    # Written only in a comment, or in the listing, it is not assembled.
    assert machinery.verdict(narrow.replace("  push ebx\n", "  push ebx // pushfd\n")) == "jump_may_be_long"
    assert machinery.verdict(narrow.replace("{ header }", "{ pushfd }")) == "jump_may_be_long"
    assert machinery.verdict(narrow + "pushad\n") == "jump_may_be_long"
    # Nor is one the script defined to mean something else.
    assert machinery.verdict(x86.replace("alloc(pDiff,8)", "alloc(pDiff,8)\ndefine(pushfd,pushfq)\ndefine(popfd,popfq)")) \
        == "jump_may_be_long"
    # Asked for as the long form, it is fourteen bytes wherever the hook is.
    assert machinery.verdict(CAPTURE.replace("objStats:\n  jmp newmem", "objStats:\n  jmp far newmem", 1)) == "patch_is_not_a_jump"


def test_a_label_is_placed_where_cheat_engine_places_it():
    # Every label from the patch to the next allocation is the game's address
    # after the jump, not only the first: what is under any of them is
    # assembled into the game's own code.
    later = CAPTURE.replace("label(return)", "label(return later)").replace(
        "return:\n[DISABLE]", "return:\nlater:\n  db 90\n[DISABLE]")
    assert later != CAPTURE and machinery.verdict(later) == "patch_is_not_a_jump"
    # Empty, it is only another name for the way back.
    assert proven(CAPTURE.replace("label(return)", "label(return back)").replace(
        "return:\n[DISABLE]", "return:\nback:\n[DISABLE]"))
    # An allocation's name moves the script back into its own memory.
    spare = CAPTURE.replace("alloc(pStats,8)", "alloc(pStats,8)\nalloc(spare,8)").replace(
        "return:\n[DISABLE]", "return:\nspare:\n  dq 0\n[DISABLE]")
    assert spare != CAPTURE and proven(spare)


def test_a_hook_that_runs_past_its_own_allocation_is_not_followed_into_the_next():
    # Two allocations are two places: what runs past the end of the first runs
    # the zeroes it holds there, never the code the text lists next.
    split = CAPTURE.replace("alloc(pStats,8)", "alloc(pStats,8)\nalloc(tail,$100,objStats)").replace(
        "  mov [pStats],rcx\n", "  mov [pStats],rcx\ntail:\n")
    assert split != CAPTURE and machinery.verdict(split) == "runs_off_its_memory"
    # A label there is the same memory, run on into.
    assert proven(CAPTURE.replace("label(return)", "label(return tail)").replace(
        "  mov [pStats],rcx\n", "  mov [pStats],rcx\ntail:\n"))


def test_a_store_or_a_saved_register_is_read_exactly_as_written():
    def hook(lines: str) -> str | None:
        return machinery.verdict(CAPTURE.replace("  mov [pStats],rcx\n", lines))

    # A segment moves the address off the script's own memory. Written first,
    # the line reads as a label; as the operand an exchange writes second, it
    # is the store it is.
    assert hook("  mov fs:[pStats],rcx\n") is not None
    assert hook("  push rax\n  xchg rax,fs:[pStats]\n  pop rax\n") == "writes_game_memory"
    assert hook("  push rax\n  xchg rax,[pStats]\n  pop rax\n") is None
    # A register is restored by the pop of the width it was pushed at.
    assert hook("  push rcx\n  mov rcx,1\n  mov [pStats],rcx\n  pop rcx\n") is None
    assert hook("  push cx\n  mov rcx,1\n  pop rcx\n") == "unbalanced_stack"
