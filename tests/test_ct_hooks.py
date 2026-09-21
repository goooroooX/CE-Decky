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
