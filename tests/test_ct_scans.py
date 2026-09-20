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
    DEFAULT_BUDGET_SECONDS,
    ScanParseError,
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


def test_the_default_budget_is_a_bound_rather_than_an_expectation():
    """Measured with `scripts/ct_scan_survey.py`: 28 patterns over a 407 MiB
    program cost under a second, so this is room rather than a target."""
    assert DEFAULT_BUDGET_SECONDS >= 5.0
