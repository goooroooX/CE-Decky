"""Reading a table's byte patterns, and looking for them in a game's program.

The shapes here are the ones 142 real tables actually carry: the three Auto
Assembler directives, every spelling of a wildcard those tables use, patterns
written with arbitrary spacing, and the Lua scanner calls that look like
directives and are not.
"""

from __future__ import annotations

from pathlib import Path
import struct

import pytest

from ce_decky.ct_scans import (
    assert_sections_kept,
    DEFAULT_BUDGET_SECONDS,
    ScanParseError,
    ScanRepairError,
    TableScan,
    assert_only_scans_dropped,
    assert_repair_closed,
    drop_unmatched_scans,
    repair_script,
    check_executable,
    compile_pattern,
    executable_is_packed,
    extract_scans,
    normalized_pattern,
)
import ce_decky.ct_scans as ct_scans


def test_reads_each_of_the_three_directives():
    script = """
[ENABLE]
aobscanmodule(aobHealth,Game-Win64-Shipping.exe,48 8B 01 48 89 54 24 20)
aobscan(HEALTH_A,89 87 E4 00 00 00 85 C0 7F 10)
aobScanRegion(ResourceAOB, Game.exe+7000000, Game.exe+7FFFFFF, 8B 58 20 33 58 18)
"""
    scans = extract_scans(script)
    assert [scan.name for scan in scans] == ["aobHealth", "HEALTH_A", "ResourceAOB"]
    # The module is only claimed where the directive names one, and the pattern
    # is the last argument whatever sits in front of it.
    assert [scan.module for scan in scans] == ["Game-Win64-Shipping.exe", None, None]
    assert scans[2].pattern == "8B 58 20 33 58 18"


def test_reads_every_spelling_of_a_wildcard_the_corpus_carries():
    """`?`, `??`, `*` and `**` are one unknown byte each, and so is `?*`."""
    assert normalized_pattern("48 ?? 8B ? 90 * 41 **") == "48 ?? 8B ?? 90 ?? 41 ??"
    # Half a byte is half a byte: CE reads `4?` as a known high nibble.
    assert normalized_pattern("4? ?8") == "4? ?8"


def test_reads_a_pattern_the_way_it_is_written_rather_than_by_its_spaces():
    """A token is one or more bytes, and the same pattern is written every way.

    `48 B9 ? ? ? ?` is six bytes with four unknown; the same six written with
    no spaces are one token. A reader that took a token for a byte made the
    first four bytes long and refused the second outright.
    """
    spaced = normalized_pattern("48 B9 ? ? ? ?")
    assert spaced == "48 B9 ?? ?? ?? ??"
    assert normalized_pattern("48B9????????") == spaced
    # And a real corpus line, which groups its bytes however the author felt.
    assert normalized_pattern("4D 8? B? ????0000 4? 85 FF") == "4D 8? B? ?? ?? 00 00 4? 85 FF"


def test_refuses_a_directive_rather_than_reading_half_of_it():
    """Guessing puts a pattern in front of the reader the table never looks for."""
    for line in (
        "aobscan(.,3E D9 EE D9 9B D4 02 00 00 8D)",
        "aobscanmodule(aobHealth,Game.exe)",
        "aobscanmodule(aobHealth,Game.exe,48 8B 01,extra)",
        "aobscan(aobHealth,thePatternVariable)",
        # 22 corpus scans spell a wildcard `xx`. Cheat Engine does not document
        # it, and reading a pattern more loosely than Cheat Engine reads it
        # would report a game as holding code it does not.
        "aobscanmodule(master,Game.exe,488Bxx33xx4Cxx)",
        "aobscan(aobHealth,)",
    ):
        assert extract_scans(line) == [], line


def test_a_commented_out_scan_is_not_a_scan():
    """The corpus carries 198 of these against 812 live ones."""
    assert extract_scans("//aobscanmodule(aobHealth,Game.exe,48 8B 01)") == []
    assert extract_scans("  // aobscan(aobHealth,48 8B 01)") == []
    assert extract_scans("--aobscanregion(aobHealth,a,b,48 8B 01)") == []
    # And a live one keeps its trailing note.
    scans = extract_scans("aobscan(aobHealth,48 8B 01) // should be unique")
    assert [scan.pattern for scan in scans] == ["48 8B 01"]


def test_a_lua_call_that_looks_like_a_directive_is_not_one():
    """`AOBScan` and its friends are the table's own Lua, not a declaration.

    They are commonly handed a variable rather than a literal, and what they do
    with the result is the script's business. Reading one as a declaration
    invents a scan the table does not perform.
    """
    assert extract_scans('local instr = AOBScan(pat, "+X")') == []
    assert extract_scans('addressRadar = AOBScanModuleUnique(process,"F3 0F 59 80 ?? ?? 00 00")') == []
    assert extract_scans("local Scan_AoB = aobScanEx(AoB_GameEngine)") == []


def test_one_name_is_read_once_however_often_the_script_repeats_it():
    script = "aobscan(aobHealth,48 8B 01)\naobscan(aobHealth,48 8B 02)"
    assert [scan.pattern for scan in extract_scans(script)] == ["48 8B 01"]


def test_a_pattern_longer_than_a_real_one_is_refused():
    with pytest.raises(ScanParseError):
        normalized_pattern(" ".join(["90"] * (ct_scans.MAX_PATTERN_TOKENS + 1)))


def _binary(tmp_path: Path, payload: bytes, name: str = "game.exe") -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_names_exactly_the_pattern_the_program_does_not_hold(tmp_path: Path):
    """The finding is which one is missing, not that something is."""
    program = _binary(tmp_path, b"\x00" * 64 + bytes.fromhex("488B0148895424") + b"\x11" * 64
                      + bytes.fromhex("8987E4000000") + b"\x22" * 64)
    scans = extract_scans(
        "aobscan(present_one,48 8B 01 48 89 54 24)\n"
        "aobscan(present_two,89 87 E4 00 00 00)\n"
        "aobscan(absent_one,F3 0F 59 F0 48 8B C3)\n"
    )
    result = check_executable(program, scans)
    assert result.missing == ("absent_one",)
    assert set(result.present) == {"present_one", "present_two"}
    assert result.not_checked == ()
    assert result.reason is None


def test_a_wildcard_matches_any_byte_in_that_position(tmp_path: Path):
    program = _binary(tmp_path, b"\x90" * 32 + bytes.fromhex("488B7717") + b"\x90" * 32)
    scans = extract_scans("aobscan(loose,48 8B ?? 17)\naobscan(nibble,48 8B 7? 17)")
    result = check_executable(program, scans)
    assert set(result.present) == {"loose", "nibble"} and result.missing == ()


def test_a_pattern_with_no_literal_first_byte_is_not_checked(tmp_path: Path):
    """Not checked is not present, and it is not missing either.

    The whole cost of this check is finding a known first byte, and a pattern
    that opens with a wildcard has none to find. Saying nothing about it is the
    honest answer; searching it is what makes the check unbounded.
    """
    program = _binary(tmp_path, b"\x90" * 128)
    result = check_executable(program, extract_scans("aobscan(headless,?? 8B 01 48)"))
    assert result.missing == () and result.present == ()
    assert [name for name, _ in result.not_checked] == ["headless"]
    assert "wildcard" in result.not_checked[0][1]


def test_a_scan_that_looks_in_another_module_is_not_checked(tmp_path: Path):
    """A game ships more than one file, and this was handed one of them."""
    program = _binary(tmp_path, b"\x90" * 128)
    scans = extract_scans(
        "aobscanmodule(elsewhere,GameAssembly.dll,48 8B 01 48)\n"
        "aobscanmodule(here,game.exe,48 8B 01 48)\n"
        "aobscanmodule(attached,$process,48 8B 01 48)\n"
    )
    result = check_executable(program, scans)
    assert [name for name, _ in result.not_checked] == ["elsewhere"]
    assert "GameAssembly.dll" in result.not_checked[0][1]
    # The module named after this very file, and CE's own name for it, are both
    # this file: they are searched and reported.
    assert set(result.missing) == {"here", "attached"}


def test_a_match_across_a_chunk_boundary_is_still_found(tmp_path: Path, monkeypatch):
    """The file is read in spans, and a pattern does not respect them."""
    monkeypatch.setattr(ct_scans, "_CHUNK_BYTES", 64)
    payload = bytearray(b"\x90" * 200)
    payload[62:70] = bytes.fromhex("F30F59F0488BC3EE")
    result = check_executable(_binary(tmp_path, bytes(payload)),
                              extract_scans("aobscan(straddles,F3 0F 59 F0 48 8B C3 EE)"))
    assert result.present == ("straddles",) and result.missing == ()


def test_the_check_stops_at_its_bound_and_says_what_it_did_not_answer(tmp_path: Path, monkeypatch):
    """A bound that is spent is reported, never mistaken for an answer."""
    monkeypatch.setattr(ct_scans, "_CHUNK_BYTES", 16)
    program = _binary(tmp_path, b"\x90" * 4096)
    result = check_executable(program, extract_scans("aobscan(slow_one,F3 0F 59 F0)"), budget_seconds=0.0)
    assert result.missing == () and result.present == ()
    assert [name for name, _ in result.not_checked] == ["slow_one"]
    assert "time" in result.not_checked[0][1]


def test_a_table_that_scans_for_nothing_is_said_to_scan_for_nothing(tmp_path: Path):
    result = check_executable(_binary(tmp_path, b"\x90" * 32), [])
    assert result.reason is not None and result.missing == () and result.present == ()


def test_a_program_that_cannot_be_read_says_so_rather_than_reporting_misses(tmp_path: Path):
    result = check_executable(tmp_path / "absent.exe", extract_scans("aobscan(one,48 8B 01 48)"))
    assert result.reason is not None and result.missing == ()


def _pe_with_section(tmp_path: Path, section: str, name: str = "wrapped.exe") -> Path:
    """A minimal PE64 whose one section carries the name being tested."""
    headers = bytearray(0x400)
    headers[0:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x80)
    pe = 0x80
    headers[pe:pe + 4] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", headers, pe + 4, 0x8664, 1, 0, 0, 0, 240, 0x22)
    optional = pe + 24
    struct.pack_into("<H", headers, optional, 0x20B)
    struct.pack_into("<I", headers, optional + 108, 16)
    at = optional + 240
    headers[at:at + 8] = section.encode("ascii").ljust(8, b"\x00")
    struct.pack_into("<IIII", headers, at + 8, 0x100, 0x1000, 0x100, 0x400)
    path = tmp_path / name
    path.write_bytes(bytes(headers) + b"\x90" * 0x100)
    return path


def test_a_wrapped_program_is_reported_as_one_this_cannot_check(tmp_path: Path):
    """Steam's own DRM is the wrapper a user here meets most.

    Cheat Engine scans a running process. Where the bytes on disk are not the
    code that runs, every pattern would read as missing, and a screen saying so
    would be telling the user the table is broken when the check is.
    """
    wrapped = _pe_with_section(tmp_path, ".bind")
    assert executable_is_packed(wrapped) == ".bind"
    result = check_executable(wrapped, extract_scans("aobscan(one,48 8B 01 48)"))
    assert result.missing == () and result.present == ()
    assert result.reason is not None and ".bind" in result.reason


def test_an_ordinary_program_declares_no_wrapper(tmp_path: Path):
    assert executable_is_packed(_pe_with_section(tmp_path, ".text")) is None
    # And a file that is not a PE at all is not a wrapped one either.
    assert executable_is_packed(_binary(tmp_path, b"\x90" * 64)) is None


def test_a_compiled_pattern_matches_what_cheat_engine_would():
    compiled = compile_pattern("48 8B ?? 4? ?8")
    assert compiled is not None
    assert compiled.search(bytes.fromhex("488B994048")) is not None
    # The high nibble is asserted where one is given: a byte beginning `4`
    # matches `4?` and not `5?`.
    assert compile_pattern("90 5?").search(bytes.fromhex("9048")) is None
    assert compile_pattern("90 4?").search(bytes.fromhex("9048")) is not None
    # A pattern whose first byte is not fully known has no fast path at all,
    # and half of one is not one: the cost of this check is finding that byte.
    assert compile_pattern("?? 8B 01") is None
    assert compile_pattern("4? 8B 01") is None


def test_a_pattern_seen_twice_is_reported_and_one_seen_once_claims_nothing(tmp_path: Path):
    """Cheat Engine takes any one of the places a pattern matches.

    Its own documentation says the scanner "will return any random match", so
    making a pattern unique is the table author's job, and one that is not
    unique in this build is a hook that may land in unrelated code. What this
    reports is a second place it actually saw: proving uniqueness means reading
    the whole program for every pattern instead of stopping at the first match,
    which was six times the cost on the program this was built against.
    """
    program = tmp_path / "game.exe"
    twice = bytes.fromhex("F30F59F0488BC3")
    once = bytes.fromhex("488B0148895424")
    program.write_bytes(b"\x00" * 16 + twice + b"\x11" * 32 + twice + b"\x22" * 16 + once + b"\x33" * 16)

    answer = check_executable(program, [
        TableScan(name="aobTwice", module=None, pattern="F3 0F 59 F0 48 8B C3"),
        TableScan(name="aobOnce", module=None, pattern="48 8B 01 48 89 54 24"),
    ])

    assert answer.present == ("aobTwice", "aobOnce")
    assert answer.ambiguous == ("aobTwice",)


def test_a_pattern_that_overlaps_itself_is_still_two_places():
    """A second place is a second address, not a second run of bytes."""
    program_bytes = bytes.fromhex("AAAAAA")
    pattern = compile_pattern("AA AA")
    assert pattern is not None
    first = pattern.search(program_bytes)
    assert first is not None
    assert pattern.search(program_bytes, first.start() + 1) is not None


def test_the_default_budget_is_a_bound_rather_than_an_expectation():
    """Measured with `scripts/ct_scan_survey.py`: 28 patterns over a 407 MiB
    program cost under a second, so this is room rather than a target."""
    assert DEFAULT_BUDGET_SECONDS >= 5.0


# -- repairing a table whose scan no longer matches --------------------------

# The shape a real table carries, which is what the repair is written against:
# one run of scan lines with no blanks in it, a block declaring the symbols a
# hook derives from its scan, a block declaring the labels only that hook uses,
# the injected code, the entry point, and the restore in `[DISABLE]`.
REPAIRABLE_SCRIPT = """[ENABLE]
aobscanmodule(aobKeep,game.exe,48 8B 01 48 89 54 24)
aobscanmodule(aobGone,game.exe,F3 0F 59 F0 48 8B C3)
alloc(newmem,$1000)

label(bEnabled)
registersymbol(bEnabled)

label(aobGone_r)
label(aobGone_i)
registersymbol(aobGone_r)
registersymbol(aobGone_i)

label(lblGone)
label(lblGoneRet)

label(aobKeep_r)
registersymbol(aobKeep_r)

newmem:

lblGone:
cmp dword ptr [bEnabled],1
jne short lblGoneRet
mulss xmm0,xmm5
aobGone_i:
readmem(aobGone,7)

lblKeep:
readmem(aobKeep,8)
jmp lblKeepRet

aobGone:
aobGone_r:
jmp lblGone
nop 2
lblGoneRet:

aobKeep:
aobKeep_r:
jmp lblKeep
lblKeepRet:

[DISABLE]

aobGone_r:
readmem(aobGone_i,7)

aobKeep_r:
readmem(aobKeep,8)
"""


def test_a_repair_removes_the_blocks_the_failing_scan_owns_and_no_others():
    """What a scan owns is found rather than assumed.

    Not one of the 142 corpus tables names the generator that wrote it, so a
    transform gated on one would run for almost nothing. What it starts from is
    the scan's own name and the two symbols a script derives from it, and what
    pulls a block in is *defining* one of those - never merely mentioning it,
    or removing one hook would take out everything that reads the same flag.
    """
    repaired, owned, blocks = repair_script(REPAIRABLE_SCRIPT, ["aobGone"])

    # The declarations, the labels, the injection, the entry point, and the
    # restore in `[DISABLE]`: the same five a real table carries.
    assert blocks == 5
    assert owned == {"aobGone", "aobGone_r", "aobGone_i", "lblGone", "lblGoneRet"}
    # Everything the other hook needs is still there, including the flag both of
    # them read: a block that only reads what the removed code read is somebody
    # else's hook.
    assert "aobKeep" in repaired and "lblKeep" in repaired
    assert "bEnabled" in repaired and "registersymbol(bEnabled)" in repaired
    # And nothing the removal took away is named anywhere that is left.
    assert "aobGone" not in repaired and "lblGone" not in repaired
    # The scan line goes on its own: the block it sits in is the run of every
    # scan the script makes, and its neighbours stay.
    assert "aobscanmodule(aobKeep,game.exe,48 8B 01 48 89 54 24)" in repaired
    assert "alloc(newmem,$1000)" in repaired


# A script whose `[DISABLE]` gives every hook's symbols back in one run, which
# is how the corpus writes one. That run declares nothing - `unregistersymbol`
# and `dealloc` hand symbols back rather than announcing them - so no rule about
# what a block defines can ever reach it, and the line undoing a hook that is
# gone stayed behind naming a symbol that is not there.
SHARED_DISABLE_SCRIPT = """[ENABLE]
aobscanmodule(aobGone,game.exe,F3 0F 59 F0 48 8B C3)
aobscanmodule(aobKeep,game.exe,48 8B 01 48 89 54 24)
alloc(newmem,$1000)

label(lblGone)
label(lblGoneRet)
registersymbol(aobGone_r)

label(lblKeep)
label(lblKeepRet)
registersymbol(aobKeep_r)

aobGone:
aobGone_r:
jmp lblGone
lblGoneRet:

aobKeep:
aobKeep_r:
jmp lblKeep
lblKeepRet:

[DISABLE]

aobGone_r:
db F3 0F 59 F0

aobKeep_r:
db 48 8B 01 48

unregistersymbol(aobGone_r)
unregistersymbol(aobKeep_r)
dealloc(newmem)
"""


def test_a_repair_takes_the_lines_that_undo_a_hook_that_is_gone():
    """The residue a block rule cannot reach, and nothing beyond it.

    A `[DISABLE]` run declares nothing, so the pass that takes a block for what
    it defines never sees one: the hook's whole `[ENABLE]` side went and the
    line handing its symbol back stayed, naming something that is no longer
    there. The closure proof refused that, correctly, for a repair that was
    otherwise complete.
    """
    repaired, owned, _ = repair_script(SHARED_DISABLE_SCRIPT, ["aobGone"])
    assert_repair_closed(repaired, owned)

    # The line that gave the removed hook's symbol back is gone with it.
    assert "unregistersymbol(aobGone_r)" not in repaired
    # And the other hook keeps every one of its own, including the line in the
    # same run: this takes single statements about symbols that are gone, never
    # a block somebody else is still in.
    assert "unregistersymbol(aobKeep_r)" in repaired
    assert "registersymbol(aobKeep_r)" in repaired
    assert "aobKeep_r:" in repaired
    # The allocation both hooks share is nobody's residue: it is a statement
    # about a symbol the repair did not take, so it stays.
    assert "dealloc(newmem)" in repaired and "alloc(newmem,$1000)" in repaired
    # And the section itself is still a section.
    assert "[ENABLE]" in repaired and "[DISABLE]" in repaired


def test_a_statement_naming_anything_that_survived_is_left_alone():
    """The rule is one statement about symbols that are gone, and nothing else.

    A line that also names a symbol the repair left in place is the author's
    code rather than the removal's leftover, and taking it would be editing a
    table instead of repairing one. What follows is a refusal, which is the
    correct outcome: the script still names something that is not there.
    """
    script = SHARED_DISABLE_SCRIPT.replace(
        "unregistersymbol(aobGone_r)\n", "unregistersymbol(aobGone_r,aobKeep_r)\n",
    )
    repaired, owned, _ = repair_script(script, ["aobGone"])

    assert "unregistersymbol(aobGone_r,aobKeep_r)" in repaired
    with pytest.raises(ScanRepairError, match="still names aobGone_r"):
        assert_repair_closed(repaired, owned)


# A table's own header, written the way Cheat Engine writes one: a `{ ... }`
# comment listing what each hook does, symbol by symbol, with a blank line in
# the middle of it. Read as code it hands the removal every symbol it names, and
# its blank line runs the comment's tail into the `[ENABLE]` below.
COMMENTED_SCRIPT = """{Game: a game
Date 2026-01-01

  Initiate aobGone
  Initiate aobKeep
}
[ENABLE]
aobscanmodule(aobGone,game.exe,F3 0F 59 F0 48 8B C3)
aobscanmodule(aobKeep,game.exe,48 8B 01 48 89 54 24)

label(lblGone)
registersymbol(aobGone_r)

aobGone:
aobGone_r:
jmp lblGone

aobKeep:
db 90 90

[DISABLE]
aobGone_r:
db F3 0F 59 F0
unregistersymbol(aobGone_r)
"""


def test_a_table_header_is_read_as_the_comment_it_is():
    """A comment is not code, in either direction.

    Read as code, a header naming the hooks hands the removal every symbol it
    mentions and its blank line runs the comment into the section below, so
    removing one hook took `[ENABLE]` with it. Read as a reference, it refuses
    the repair for a line Cheat Engine never executes. Both are the same
    mistake.
    """
    repaired, owned, _ = repair_script(COMMENTED_SCRIPT, ["aobGone"])
    assert_repair_closed(repaired, owned)
    assert_sections_kept(COMMENTED_SCRIPT, repaired)

    # The header is the author's and stays, mentions and all.
    assert "Initiate aobGone" in repaired
    # The sections are what Cheat Engine runs the record from.
    assert "[ENABLE]" in repaired and "[DISABLE]" in repaired
    # And the other hook is untouched.
    assert "aobscanmodule(aobKeep,game.exe,48 8B 01 48 89 54 24)" in repaired


def test_a_repair_may_not_take_the_sections_the_script_runs_from():
    """A cheat that switches on and cannot be switched off is not a repair.

    A script writes `[DISABLE]` against its restore with no blank line between
    them often enough that removing the restore took the section with it, and
    nothing noticed: every symbol still resolved and every record was still the
    record it was.
    """
    script = """[ENABLE]
aobscanmodule(aobGone,game.exe,F3 0F 59 F0 48 8B C3)
label(lblGone)

aobGone:
jmp lblGone
[DISABLE]
aobGone:
db F3 0F 59 F0
"""
    repaired, owned, _ = repair_script(script, ["aobGone"])

    assert "[ENABLE]" in repaired and "[DISABLE]" in repaired
    assert_sections_kept(script, repaired)
    # And the proof is a proof rather than a description of the code above it.
    with pytest.raises(ScanRepairError, match=r"no longer has its \[DISABLE\] section"):
        assert_sections_kept(script, repaired.replace("[DISABLE]\n", ""))


def test_a_script_whose_only_hook_went_keeps_an_empty_pair_of_sections():
    """A script with one scan is dead when that scan is, and still a script.

    The markers cost nothing to keep, so what is left is an empty `[ENABLE]`
    and `[DISABLE]` rather than a record with no script at all. What it costs
    the user is the cheats that hook created, which the caller reports.
    """
    script = """[ENABLE]
aobscan(aobGone,F3 0F 59 F0)
aobGone:
db 90 90

[DISABLE]
aobGone:
db F3 0F 59 F0
"""
    repaired, _owned, _ = repair_script(script, ["aobGone"])

    assert_sections_kept(script, repaired)
    assert [line.strip() for line in repaired.splitlines() if line.strip()] == ["[ENABLE]", "[DISABLE]"]


def test_a_repair_that_leaves_a_reference_behind_is_refused():
    """A script Cheat Engine will not compile is the outcome this exists to fix.

    So the proof is about what survived: a line still naming a removed symbol
    produces the same dead table as the missing pattern did, and a repair that
    cannot be shown to resolve is not produced at all.
    """
    repaired, owned, _ = repair_script(REPAIRABLE_SCRIPT, ["aobGone"])
    assert_repair_closed(repaired, owned)

    # A line the removal should have taken and did not.
    with pytest.raises(ScanRepairError, match="still names"):
        assert_repair_closed(repaired + "\nmov [aobGone_r],1\n", owned)
    # A `readmem`, a `define` anchored on the scan, and any other reference are
    # one thing: a line naming something that is no longer there.
    with pytest.raises(ScanRepairError, match="aobGone_i"):
        assert_repair_closed(repaired + "\nreadmem(aobGone_i,7)\n", owned)
    with pytest.raises(ScanRepairError, match="aobGone"):
        assert_repair_closed(repaired + "\nalloc(mem,$100,aobGone)\n", owned)
    # And a comment is not code: a script that says in words what a hook used to
    # do would otherwise refuse its own repair, which is a refusal about nothing.
    assert_repair_closed(repaired + "\n// aobGone used to hook here\n", owned)
    # A jump to a label the repair took away. A target the script never defined
    # - an imported function, a symbol Cheat Engine resolves - was undefined
    # before the repair too, and refusing over one refuses a table for something
    # the repair did not do.
    with pytest.raises(ScanRepairError, match="which the repair removed"):
        assert_repair_closed(repaired + "\njmp lblVanished\n", owned, {"lblVanished"})
    assert_repair_closed(repaired + "\ncall GetCurrentProcessId\n", owned, {"lblKeep"})


def test_a_script_that_does_not_scan_for_it_is_refused_rather_than_edited():
    with pytest.raises(ScanRepairError):
        repair_script(REPAIRABLE_SCRIPT, ["aobSomethingElse"])


def _table_with(script: str, *, extra_records: str = "") -> bytes:
    return (
        '<?xml version="1.0"?>\r\n<CheatTable CheatEngineTableVersion="45">\r\n'
        '  <CheatEntries><CheatEntry><ID>1</ID><Description>"Hook"</Description>'
        '<VariableType>Auto Assembler Script</VariableType>'
        f'<AssemblerScript Async="1">{script}</AssemblerScript></CheatEntry>'
        f'{extra_records}</CheatEntries>\r\n</CheatTable>\r\n'
    ).encode("utf-8")


def test_the_repair_edits_the_script_where_it_lies_and_leaves_the_table_alone():
    """A byte-level edit, for the reason the signature transform gives.

    Rewriting the XML would produce a file differing from the author's
    everywhere the two writers disagree, and the user would be agreeing to bytes
    nobody has seen.
    """
    blob = _table_with(REPAIRABLE_SCRIPT)
    derived, repair = drop_unmatched_scans(blob, ["aobGone"])

    assert repair.scans == ("aobGone",) and repair.blocks == 5
    assert repair.bytes_removed == len(blob) - len(derived) > 0
    assert repair.orphaned == ()
    assert_only_scans_dropped(blob, derived, ["aobGone"])
    # Everything outside the script is the bytes it was.
    assert derived.startswith(b'<?xml version="1.0"?>\r\n<CheatTable')
    assert derived.endswith(b"</CheatEntries>\r\n</CheatTable>\r\n")


def test_the_repair_names_the_cheats_it_costs():
    """A record whose address was a removed symbol is a cheat that is gone.

    Handing somebody a repaired table quietly missing what they came for is the
    one outcome a repair may not produce silently.
    """
    extra = (
        '<CheatEntry><ID>2</ID><Description>"Acceleration"</Description>'
        '<VariableType>Float</VariableType><Address>aobGone_r</Address></CheatEntry>'
    )
    blob = _table_with(REPAIRABLE_SCRIPT, extra_records=extra)
    _, repair = drop_unmatched_scans(blob, ["aobGone"])
    assert repair.orphaned == ("Acceleration",)


def test_the_cost_names_a_cheat_whose_whole_script_went():
    """Most repairs empty a script, and saying nothing was lost is untrue.

    A script carrying one scan is dead when that scan is, so what is left is a
    record that switches and does nothing. It is reached through no address, so
    the other half of this report never sees it, and the copy would have gone to
    the reader as one that cost them nothing.
    """
    table = (
        '<?xml version="1.0"?>\n<CheatTable CheatEngineTableVersion="45">\n'
        "  <CheatEntries>"
        '<CheatEntry><ID>1</ID><Description>"Infinite health"</Description>'
        "<VariableType>Auto Assembler Script</VariableType>"
        "<AssemblerScript>[ENABLE]\n"
        "aobscan(aobGone,F3 0F 59 F0)\n"
        "aobGone:\n"
        "db 90 90\n"
        "\n"
        "[DISABLE]\n"
        "aobGone:\n"
        "db F3 0F 59 F0\n"
        "</AssemblerScript></CheatEntry>"
        "</CheatEntries>\n</CheatTable>\n"
    ).encode("utf-8")

    derived, repair = drop_unmatched_scans(table, ["aobGone"])

    assert repair.orphaned == ("Infinite health",)
    # The record is still there and still named: what changed is that its script
    # is now an empty pair of sections.
    assert b"Infinite health" in derived
    assert b"aobscan(aobGone" not in derived


def test_a_repair_that_changed_the_table_is_refused():
    """The closure proof says the script resolves; this says it is the same table."""
    blob = _table_with(REPAIRABLE_SCRIPT)
    derived, _ = drop_unmatched_scans(blob, ["aobGone"])
    tampered = derived.replace(b"<Description>\"Hook\"</Description>", b"<Description>\"Other\"</Description>")
    with pytest.raises(ScanRepairError, match="changed the table's records"):
        assert_only_scans_dropped(blob, tampered, ["aobGone"])
    # And one that took a scan nobody asked about.
    both, _ = drop_unmatched_scans(blob, ["aobGone"])
    with pytest.raises(ScanRepairError, match="nobody asked"):
        assert_only_scans_dropped(blob, both, [])
