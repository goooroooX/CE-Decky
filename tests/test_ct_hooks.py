"""Which of a table's own flags can be written off without killing the game."""
from __future__ import annotations

from ce_decky.ct_hooks import flags_safe_to_switch_off


SAFE_AND_UNSAFE = """
[ENABLE]
label(bEnablePlain)
label(bEnableVitals)
registersymbol(bEnablePlain)
registersymbol(bEnableVitals)

lblPlain:
cmp dword ptr [bEnablePlain],1
jne short lblPlainSkip
mulss xmm0,[fPlainMod]
lblPlainSkip:
readmem(aobPlain,7)
jmp lblPlainRet

lblStatUpdater:
cmp qword ptr [pPlayerStats],rsi
jne lblStatUpdaterSkip
sub rcx,rsi
cmp rcx,0x48
je lblStatUpdaterVitals
lblStatUpdaterDone:
add rcx,rsi
lblStatUpdaterSkip:
readmem(aobStatUpdater,3)
jmp lblStatUpdaterRet
lblStatUpdaterVitals:
cmp dword ptr [bEnableVitals],1
jne lblStatUpdaterSkip
mulss xmm4,[fVitalsMod]
jmp lblStatUpdaterDone

bEnablePlain:
dd 1
bEnableVitals:
dd 1
[DISABLE]
"""


def test_a_flag_whose_off_branch_leaves_the_hook_changed_is_not_safe():
    """The shape that killed a real game, minutes after one cheat was switched on.

    The hook turns a pointer into an offset, tests the flag, and on the branch
    taken when the flag is off returns to the game without turning it back. The
    game's own instruction then reads an offset as an address.
    """
    safe = flags_safe_to_switch_off(SAFE_AND_UNSAFE)
    assert "bEnableVitals" not in safe
    # And the hook beside it, which changes nothing before it tests its flag,
    # is the ordinary shape and stays usable.
    assert "bEnablePlain" in safe


def test_a_flag_read_in_a_way_this_does_not_follow_is_not_offered_as_safe():
    """The answer is only ever `safe`, never `no finding, so fine`.

    A flag moved into a register is one whose reach this does not know, and a
    caller about to write it into a running game may not act on that silence.
    """
    script = SAFE_AND_UNSAFE.replace(
        "mulss xmm0,[fPlainMod]",
        "mov eax,[bEnablePlain]\ntest eax,eax",
    )
    assert "bEnablePlain" not in flags_safe_to_switch_off(script)


def test_a_flag_nothing_tests_is_not_safe_either():
    """Nothing read it here, which is not the same as nothing reading it."""
    script = SAFE_AND_UNSAFE.replace("cmp dword ptr [bEnablePlain],1\njne short lblPlainSkip\n", "")
    assert "bEnablePlain" not in flags_safe_to_switch_off(script)


def test_the_disable_section_is_not_this_question():
    """`[DISABLE]` runs when the whole script comes down, not when a flag moves."""
    script = SAFE_AND_UNSAFE.replace("[DISABLE]", "[DISABLE]\ncmp dword ptr [bEnablePlain],1\njne lblNowhere")
    assert "bEnablePlain" in flags_safe_to_switch_off(script)


def test_a_script_this_cannot_read_offers_nothing():
    assert flags_safe_to_switch_off(None) == frozenset()
    assert flags_safe_to_switch_off("") == frozenset()
    assert flags_safe_to_switch_off("[ENABLE]\nnothing here\n[DISABLE]") == frozenset()
    # A jump to a label this section does not define is a path this cannot
    # follow, so the flag that led there is not offered.
    script = SAFE_AND_UNSAFE.replace("jne short lblPlainSkip", "jne short lblSomewhereElse")
    assert "bEnablePlain" not in flags_safe_to_switch_off(script)


def test_the_reader_is_bounded_rather_than_clever():
    """A script that loops on itself costs a bound, not a hang."""
    script = """
[ENABLE]
label(bEnableLoop)
registersymbol(bEnableLoop)
lblLoop:
sub rcx,rsi
cmp dword ptr [bEnableLoop],1
jne lblLoop
add rcx,rsi
jmp lblLoopRet
bEnableLoop:
dd 1
[DISABLE]
"""
    assert flags_safe_to_switch_off(script) == frozenset()


FALLTHROUGH = """
[ENABLE]
label(bEnableThing)
registersymbol(bEnableThing)
lblHook:
sub rcx,rsi
cmp dword ptr [bEnableThing],0
jne lblHookRestore
jmp lblHookRet
lblHookRestore:
add rcx,rsi
jmp lblHookRet
bEnableThing:
dd 1
[DISABLE]
"""


def test_the_unsafe_exit_counts_whichever_side_of_the_branch_it_is_on():
    """The same hook written the other way round is the same hook.

    Which branch runs when the flag is off is the switch's own key, and this
    reader does not know it. A hook that tests for zero and jumps to its restore
    leaves the exit that never restores on the side the branch falls through to,
    and attributing the flag to the jump alone reported it as safe.
    """
    assert "bEnableThing" not in flags_safe_to_switch_off(FALLTHROUGH)


def test_the_same_shape_written_as_an_address_calculation_is_the_same_shape():
    """A hook that must not touch the flags moves its pointer with `lea`."""
    script = FALLTHROUGH.replace("sub rcx,rsi", "lea rcx,[rcx-rsi]").replace("add rcx,rsi", "lea rcx,[rcx+rsi]")
    assert "bEnableThing" not in flags_safe_to_switch_off(script)
    # And the balanced spelling of it is still a hook this can follow, because
    # refusing every `lea` would leave the reader with nothing to prove.
    balanced = script.replace("jmp lblHookRet\nlblHookRestore:", "lblHookRestore:")
    assert "bEnableThing" in flags_safe_to_switch_off(balanced)


def test_a_transformation_this_does_not_model_is_not_a_balanced_one():
    """A shift is a modification with no `add` to recognise as putting it back."""
    script = FALLTHROUGH.replace("sub rcx,rsi", "shl rcx,04").replace("add rcx,rsi", "shr rcx,04")
    assert "bEnableThing" not in flags_safe_to_switch_off(script)


def test_a_modification_overwritten_before_its_restore_is_not_restored():
    """The `add` at the end put back a value that was no longer there.

    The register was loaded with something else in between, so the arithmetic
    this reader pairs up balances while the hook does not.
    """
    script = """
[ENABLE]
label(bEnableThing)
registersymbol(bEnableThing)
lblHook:
sub rcx,rsi
mov rcx,[rdx+08]
cmp dword ptr [bEnableThing],1
jne lblHookSkip
mulss xmm0,[fThingMod]
lblHookSkip:
add rcx,rsi
jmp lblHookRet
bEnableThing:
dd 1
[DISABLE]
"""
    assert "bEnableThing" not in flags_safe_to_switch_off(script)


def test_what_a_hook_loads_before_it_tests_anything_is_not_a_finding():
    """It happens whichever way the flag goes, so it is never the difference.

    A hook reads what it needs into scratch registers first. Treating that as a
    modification somebody has to undo would refuse the ordinary shape and leave
    this reader with no positive answer to give.
    """
    script = """
[ENABLE]
label(bEnableThing)
registersymbol(bEnableThing)
lblHook:
mov r10,[rbx+20]
movsxd r11,[iOwnerOffset]
cmp dword ptr [bEnableThing],1
jne lblHookSkip
mulss xmm1,[fThingMod]
lblHookSkip:
jmp lblHookRet
bEnableThing:
dd 1
[DISABLE]
"""
    assert "bEnableThing" in flags_safe_to_switch_off(script)


BALANCED = """
[ENABLE]
label(bEnableThing)
registersymbol(bEnableThing)
lblHook:
sub rcx,rsi
cmp dword ptr [bEnableThing],0
jne lblHookRestore
mulss xmm0,[fThingMod]
lblHookRestore:
add rcx,rsi
jmp lblHookRet
lblMutate:
mov rcx,[rdx+08]
ret
bEnableThing:
dd 1
[DISABLE]
"""


def test_a_hook_that_puts_back_what_it_changed_is_still_safe():
    """The control for the refusals below: without it they prove nothing."""
    assert "bEnableThing" in flags_safe_to_switch_off(BALANCED)


def test_a_call_between_a_modification_and_its_restore_breaks_the_proof():
    """The `add` pairs the `sub` on paper while the callee replaced the value.

    A direct call into the script, an indirect one, and one into the game are
    all code this does not walk, so none of them leaves a proof standing.
    """
    for call in ("call lblMutate", "call qword ptr [rax+10]", "call game.exe+1234"):
        script = BALANCED.replace("mulss xmm0,[fThingMod]", call)
        assert "bEnableThing" not in flags_safe_to_switch_off(script), call
        script = BALANCED.replace("sub rcx,rsi\n", f"sub rcx,rsi\n{call}\n")
        assert "bEnableThing" not in flags_safe_to_switch_off(script), call


def test_a_call_made_whichever_way_the_flag_goes_is_not_the_difference():
    """Before any test and with nothing to undo, a call happens either way.

    Refusing it would refuse every hook that calls a helper on its way in,
    and a restore of what the callee changed would be an unpaired `add`,
    which is refused below.
    """
    script = BALANCED.replace("lblHook:\nsub rcx,rsi\n", "lblHook:\ncall lblMutate\nsub rcx,rsi\n")
    assert "bEnableThing" in flags_safe_to_switch_off(script)


def test_an_add_with_no_sub_to_pair_is_a_change_on_that_path():
    """`add rcx,rsi` on one side of a flag changes what the game is handed.

    It is the same kind of change as the `inc` and the unpaired `lea` this
    already refuses, and it is how a restore of a callee's change looks.
    """
    script = BALANCED.replace("sub rcx,rsi\n", "").replace(
        "mulss xmm0,[fThingMod]", "add rcx,rsi").replace("lblHookRestore:\nadd rcx,rsi\n", "lblHookRestore:\n")
    assert "bEnableThing" not in flags_safe_to_switch_off(script)


def test_a_branch_whose_target_this_cannot_name_is_not_walked_past():
    """`jmp game.exe+1234` leaves the hook; reading on would be reading code that never runs."""
    for jump in ("jmp game.exe+1234", "jmp qword ptr [rax]", "je game.exe+1234"):
        script = BALANCED.replace("mulss xmm0,[fThingMod]", jump)
        assert "bEnableThing" not in flags_safe_to_switch_off(script), jump


def test_a_label_named_like_a_branch_is_still_a_label():
    """`jumpTable:` and `loopTop:` start the way a branch does and are not one."""
    for label in ("jumpTable", "loopTop"):
        script = BALANCED.replace("lblHookRestore", label)
        assert "bEnableThing" in flags_safe_to_switch_off(script), label


def test_a_register_one_side_of_the_flag_replaces_is_not_proven_safe():
    """Both sides return to the game, and only one of them changed what it reads.

    Which side runs when the flag is off is the switch's own key, which this
    reader does not read, so a load on one side is a register the off state may
    hand the game. Measured on the table the crash came from, this refuses the
    three hooks that load `r10` or convert into `r8d` behind their flag.
    """
    one_side = BALANCED.replace("sub rcx,rsi\n", "").replace("add rcx,rsi\n", "")
    assert "bEnableThing" in flags_safe_to_switch_off(one_side)
    for change in ("mov rcx,[rdx+08]", "movsxd r10,[rbx+04]", "cvtss2si r8d,xmm5", "pop rcx", "push rax"):
        script = one_side.replace("mulss xmm0,[fThingMod]", change)
        assert "bEnableThing" not in flags_safe_to_switch_off(script), change
    # The same load made before the flag is tested happens either way.
    before = one_side.replace("lblHook:\n", "lblHook:\nmov r10,[rbx+20]\n")
    assert "bEnableThing" in flags_safe_to_switch_off(before)
