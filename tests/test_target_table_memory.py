from dataclasses import asdict
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from ce_decky.ce_launch import TargetIdentity
from scripts import target_table_memory as tool


def memory(size=0x10000):
    data = bytearray(size)
    base = 0x10000
    def read(fd, count, address):
        return bytes(data[address - base:address - base + count])
    mapping = tool.Mapping(base, base + size, "rw-p", 0, "00:00", 0, "")
    return data, tool.Memory(1, [mapping], pread=read), base


def recipe(*operations):
    return {"schema": 1, "operations": list(operations)}


def test_unresolved_walk_propagates_without_reading_a_guessed_dependency():
    data, mem, base = memory()
    result = tool.run_recipe(mem, base, [], recipe(
        {"name": "player", "kind": "walk", "base": "module", "offsets": [0]},
        {"name": "health", "kind": "read", "base": "player", "offset": 8},
    ))
    assert [r["state"] for r in result] == ["inconclusive", "inconclusive"]
    assert mem.bytes_read == 8


def test_mismatch_is_retained_and_misspelled_assertions_are_refused():
    data, mem, base = memory()
    struct.pack_into("<I", data, 0, 7)
    operation = {"name": "count", "kind": "read", "base": "module", "type": "u32", "expect": 9}
    result = tool.run_recipe(mem, base, [], recipe(operation))
    assert result[0]["state"] == "mismatch" and result[0]["value"] == 7
    with pytest.raises(tool.Refused, match="unknown recipe fields"):
        tool.run_recipe(mem, base, [], recipe({**operation, "expected": 7}))


def test_numeric_assertions_support_full_u64_values_and_unresolved_xor_stays_inconclusive():
    data, mem, base = memory()
    struct.pack_into("<QQ", data, 0, (1 << 63) + 7, (1 << 63) + 7)
    result = tool.run_recipe(mem, base, [], recipe(
        {"name": "encoded", "kind": "read", "base": "module"},
        {"name": "mirror", "kind": "read", "base": "module", "offset": 8, "equals": "encoded"},
        {"name": "missing", "kind": "walk", "base": "module", "offsets": [16]},
        {"name": "decoded", "kind": "xor", "left": "missing", "right": "encoded"},
    ))
    assert result[1]["state"] == "observed"
    assert result[3]["state"] == "inconclusive"


def test_array_paging_and_double_precision_reads_remain_explicit():
    data, mem, base = memory()
    struct.pack_into("<QQQ", data, 0x100, base + 0x200, base + 0x208, base + 0x210)
    struct.pack_into("<d", data, 0x208, 1234.56789)
    answer = tool.run_recipe(mem, base, [], recipe(
        {"name": "items", "kind": "array", "base": hex(base + 0x100), "count": 3, "limit": 2},
        {"name": "camera", "kind": "read", "base": {"array": "items", "index": 1}, "type": "f64"},
        {"name": "not_read", "kind": "read", "base": {"array": "items", "index": 2}},
    ))
    assert answer[0]["complete"] is False and answer[0]["total_count"] == 3
    assert answer[1]["value"] == pytest.approx(1234.56789)
    assert answer[2]["state"] == "inconclusive"


def test_paged_array_references_keep_original_indices_instead_of_rebasing_them():
    data, mem, base = memory()
    struct.pack_into("<QQQ", data, 0x100, base + 0x200, base + 0x208, base + 0x210)
    struct.pack_into("<d", data, 0x208, 1234.5)
    answer = tool.run_recipe(mem, base, [], recipe(
        {"name": "page", "kind": "array", "base": base + 0x100, "count": 3, "start": 1, "limit": 1},
        {"name": "sampled", "kind": "read", "base": {"array": "page", "index": 1}, "type": "f64"},
        {"name": "omitted", "kind": "read", "base": {"array": "page", "index": 0}, "type": "f64"},
    ))
    assert answer[1]["value"] == 1234.5
    assert answer[2]["state"] == "inconclusive"


def test_scalar_read_refuses_a_size_it_would_otherwise_ignore():
    _, mem, base = memory()
    with pytest.raises(tool.Refused, match="size applies only"):
        tool.run_recipe(mem, base, [], recipe(
            {"name": "field", "kind": "read", "base": "module", "type": "u64", "size": 2},
        ))
    assert mem.bytes_read == 0


def test_object_survey_retains_invalid_pointer_candidates_as_inconclusive():
    data, mem, base = memory()
    struct.pack_into("<QQQ", data, 0x100, 0, (1 << 64) - 1, base + 0x200)
    answer = tool.run_recipe(mem, base, [], recipe(
        {"name": "items", "kind": "array", "base": hex(base + 0x100), "count": 3},
        {"name": "objects", "kind": "ue_objects_16_16", "array": "items", "pool": "module"},
    ))
    assert answer[0]["complete"] is True
    assert answer[1]["complete"] is False
    assert [row["state"] for row in answer[1]["value"]] == ["inconclusive"] * 3
    assert answer[1]["value"][1]["address"] == (1 << 64) - 1


def test_class_survey_requires_a_readable_class_and_retains_truncated_ancestry():
    data, mem, base = memory()
    obj, cls, ancestor, pool, block = [base + i for i in (0x1000, 0x1100, 0x1200, 0x2000, 0x3000)]
    struct.pack_into("<Q", data, 0x1010, cls)
    struct.pack_into("<Q", data, 0x1140, ancestor)
    struct.pack_into("<Q", data, 0x2010, block)
    for offset, index, name in [(0x1000, 2, b"Item"), (0x1100, 8, b"Weapon"), (0x1200, 16, b"Object")]:
        struct.pack_into("<I", data, offset + 0x18, index)
        struct.pack_into("<H", data, 0x3000 + index * 2, len(name) << 6)
        data[0x3002 + index * 2:0x3002 + index * 2 + len(name)] = name
    struct.pack_into("<I", data, 0x1318, 2)
    struct.pack_into("<QQ", data, 0x4000, obj, base + 0x1300)
    def survey(depth, count):
        return tool.run_recipe(mem, base, [], recipe(
            {"name": "items", "kind": "array", "base": base + 0x4000, "count": count},
            {"name": "objects", "kind": "ue_objects_16_16", "array": "items", "pool": pool, "depth": depth},
        ))[1]
    full = survey(16, 1)
    assert full["complete"] and full["value"][0]["classes"] == ["Weapon", "Object"]
    partial = survey(1, 1)
    assert not partial["complete"] and not partial["value"][0]["lineage_complete"]
    garbage = survey(16, 2)
    assert not garbage["complete"] and garbage["value"][1]["state"] == "inconclusive"
    assert "no readable object class" in garbage["value"][1]["reason"]


def test_scan_across_a_chunk_boundary_does_not_claim_a_limited_match_is_unique():
    data, mem, base = memory(1024 * 1024 + 128)
    data[1024 * 1024 - 2:1024 * 1024 + 2] = b"\x12\x34\x56\x78"
    sections = [{"rva": 0, "size": len(data), "executable": True, "writable": False}]
    full = tool.scan(mem, base, sections, "12 34 56 78", "executable", 8)
    assert full == {"matches": [base + 1024 * 1024 - 2], "complete": True}
    limited = tool.run_recipe(mem, base, sections, recipe(
        {"name": "signature", "kind": "scan", "pattern": "12 34 56 78", "limit": 1},
        {"name": "field", "kind": "read", "base": "signature"},
    ))
    assert limited[0]["complete"] is False
    assert limited[1]["state"] == "inconclusive"


def test_overlapping_pattern_matches_are_not_mistaken_for_a_unique_signature():
    data, mem, base = memory()
    data[:3] = b"\xaa\xaa\xaa"
    sections = [{"rva": 0, "size": 16, "executable": True, "writable": False}]
    answer = tool.scan(mem, base, sections, "AA AA", "executable", 8)
    assert answer == {"matches": [base, base + 1], "complete": True}


def test_discovery_keeps_aliases_and_requires_an_independent_owner_check():
    data, mem, base = memory()
    world, obj = base + 0x3000, base + 0x4000
    for offset in (0x1000, 0x1008):
        struct.pack_into("<Q", data, offset, world)
    struct.pack_into("<Q", data, 0x3020, obj)
    struct.pack_into("<Q", data, 0x4018, world)
    sections = [{"rva": 0x1000, "size": 16, "executable": False, "writable": True}]
    checks = [{"hop": 1, "offset": 0x18, "equals_hop": 0}]
    found = tool.find_chain(mem, base, sections, [0, 0x20], checks, 8)
    assert found["complete"] and found["candidate_only"]
    assert [c["rva"] for c in found["candidates"]] == [0x1000, 0x1008]
    limited = tool.find_chain(mem, base, sections, [0, 0x20], checks, 1)
    assert limited["complete"] is False
    with pytest.raises(tool.Refused, match="independent checks"):
        tool.find_chain(mem, base, sections, [0, 0x20], [], 8)


def test_memory_denial_nonfinite_values_and_budgets_are_inconclusive():
    data, mem, base = memory()
    with pytest.raises(tool.Unavailable, match="mapping"):
        mem.read(base - 1, 1)
    struct.pack_into("<f", data, 0, float("nan"))
    with pytest.raises(tool.Unavailable, match="non-finite"):
        mem.scalar(base, "f32")
    mem.bytes_read = tool.MAX_READ_BYTES
    with pytest.raises(tool.Unavailable, match="budget"):
        mem.read(base, 8)
    mem.bytes_read = 0
    mem.deadline = -1
    with pytest.raises(tool.Unavailable, match="budget"):
        mem.read(base, 8)


def test_discovery_does_not_turn_an_io_failure_into_a_complete_negative():
    data, mem, base = memory()
    struct.pack_into("<Q", data, 0x1000, base + 0x3000)
    ordinary_read = mem.pread
    def denied(fd, count, address):
        if address == base + 0x3000:
            raise PermissionError("denied")
        return ordinary_read(fd, count, address)
    mem.pread = denied
    sections = [{"rva": 0x1000, "size": 8, "executable": False, "writable": True}]
    with pytest.raises(tool.Unavailable, match="unreadable"):
        tool.find_chain(mem, base, sections, [0], [{"hop": 0, "offset": 0, "bytes": "4e6f6e65"}], 8)


def test_changed_walk_is_inconclusive_and_malformed_patterns_are_refused():
    data, mem, base = memory()
    struct.pack_into("<Q", data, 0, base + 0x3000)
    ordinary_read = mem.pread
    reads = []
    def moving(fd, count, address):
        if address == base:
            reads.append(address)
            return struct.pack("<Q", base + (0x3000 if len(reads) == 1 else 0x4000))
        return ordinary_read(fd, count, address)
    mem.pread = moving
    answer = tool.run_recipe(mem, base, [], recipe({"name": "player", "kind": "walk", "base": "module", "offsets": [0]}))
    assert answer[0]["state"] == "inconclusive"
    for pattern in ("12 ?", "12 GG", "?? 12"):
        with pytest.raises(tool.Refused):
            tool.scan(mem, base, [], pattern, "all", 8)


def test_signed_rip_displacement_and_explicit_name_layout():
    data, mem, base = memory()
    struct.pack_into("<i", data, 0x103, -0x47)
    # A seven-byte instruction at +100 reaches module+C0.
    result = tool.run_recipe(mem, base, [], recipe({"name": "root", "kind": "rip_relative",
        "base": hex(base + 0x100), "displacement_offset": 3, "instruction_size": 7}))
    assert result[0]["value"] == base + 0xC0
    obj, pool, block = base + 0x1000, base + 0x2000, base + 0x3000
    struct.pack_into("<I", data, 0x1018, 2)
    struct.pack_into("<Q", data, 0x2010, block)
    struct.pack_into("<H", data, 0x3004, (5 << 6) | 0x12)
    data[0x3006:0x300B] = b"World"
    assert tool.ue_name(mem, obj, pool) == "World"
    data[0x3006] = 0
    with pytest.raises(tool.Unavailable, match="control"):
        tool.ue_name(mem, obj, pool)


def fake_target(tmp_path, monkeypatch, *, pids=(10,), confirmations=(True, True)):
    if not all(hasattr(os, name) for name in ("pread", "major", "minor")):
        pytest.skip("exact file-mapping probes require POSIX file identity APIs")
    path = tmp_path / "Game.exe"
    path.write_bytes(b"exact executable bytes")
    info = path.stat()
    proc = tmp_path / "proc"
    process = proc / "10"
    process.mkdir(parents=True)
    device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
    (process / "maps").write_text(f"10000-12000 rw-p 00000000 {device} {info.st_ino} {path}\n")
    image = bytearray(0x12000)
    image[0x10000:0x10002] = b"MZ"
    struct.pack_into("<I", image, 0x1003C, 0x80)
    image[0x10080:0x10084] = b"PE\0\0"
    struct.pack_into("<HH", image, 0x10084, 0x8664, 1)
    struct.pack_into("<H", image, 0x10094, 0xF0)
    struct.pack_into("<H", image, 0x10098, 0x20B)
    struct.pack_into("<I", image, 0x100D0, 0x2000)
    struct.pack_into("<8sII", image, 0x10188, b".data", 0x100, 0x1000)
    struct.pack_into("<I", image, 0x101AC, 0xC0000040)
    (process / "mem").write_bytes(image)
    wanted = TargetIdentity(pid=10, start_time=5, windows_executable="Game.exe", app_id=42)
    monkeypatch.setattr(tool, "resolve_target_identity", lambda app, target, cohort, **kw:
                        TargetIdentity(**{**asdict(wanted), "pid": cohort[0]}))
    answers = iter(confirmations)
    monkeypatch.setattr(tool, "confirm_target_identity", lambda *a, **kw: next(answers))
    calls = []
    def rpc(method, args):
        calls.append(method)
        return {
            "list_profiles": [{"app_id": 42, "table_sha256": "a" * 64, "target_process": "Game.exe"}],
            "inspect_table_sha": {"sha256": "a" * 64},
            "get_ce_launch_capability": {"game": {"app_id": 42, "running": True, "pids": list(pids)}},
        }[method]
    return path, proc, rpc, calls


def test_probe_binds_file_mapping_and_process_identity_and_only_calls_reads(tmp_path, monkeypatch):
    path, proc, rpc, calls = fake_target(tmp_path, monkeypatch)
    answer = tool.probe(42, "a" * 64, path, rpc=rpc, proc_root=proc)
    assert answer["module_base"] == 0x10000 and answer["process"]["start_time"] == 5
    assert calls == ["list_profiles", "inspect_table_sha", "get_ce_launch_capability"]
    assert answer["mode"] == "read_only_table_repair"


def test_help_remains_available_when_the_host_has_no_posix_pread():
    result = subprocess.run([sys.executable, "-c",
        "import os; os.__dict__.pop('pread', None); from scripts import target_table_memory; target_table_memory.main(['--help'])"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "--recipe" in result.stdout


def test_report_fingerprints_the_assertions_not_just_the_table(tmp_path, monkeypatch):
    path, proc, rpc, _ = fake_target(tmp_path, monkeypatch, confirmations=(True, True, True, True))
    rec = {"schema": 1, "table_sha256": "a" * 64,
           "executable_sha256": tool.file_fingerprint(path)[0],
           "operations": [{"name": "header", "kind": "read", "base": "module", "type": "u16", "expect": 0x5A4D}]}
    first = tool.probe(42, "a" * 64, path, rec, rpc=rpc, proc_root=proc)
    rec["operations"][0]["expect"] = 0
    second = tool.probe(42, "a" * 64, path, rec, rpc=rpc, proc_root=proc)
    assert first["recipe_sha256"] != second["recipe_sha256"]
    assert first["results"][0]["state"] == "observed" and second["results"][0]["state"] == "mismatch"


@pytest.mark.parametrize("condition", ["ambiguous", "pid-reused", "wrong-file", "wrong-recipe"])
def test_probe_refuses_unproven_target_or_recipe_identity(tmp_path, monkeypatch, condition):
    path, proc, rpc, _ = fake_target(tmp_path, monkeypatch,
        pids=(10, 11) if condition == "ambiguous" else (10,),
        confirmations=(True, False) if condition == "pid-reused" else (True, True))
    mapped = None
    if condition == "wrong-file":
        # The running game keeps its mapped file alive, so a file put at the
        # same path afterwards can never reuse its inode. Holding it open here is
        # that mapping: without it a filesystem that hands a freed inode straight
        # back gave the replacement the mapped file's number, and CI saw no
        # refusal for a case the device cannot produce.
        mapped = path.open("rb")
        path.unlink()
        path.write_bytes(b"a replacement file")
        assert path.stat().st_ino != os.fstat(mapped.fileno()).st_ino
    rec = {"table_sha256": "b" * 64, "executable_sha256": "c" * 64} if condition == "wrong-recipe" else None
    try:
        with pytest.raises(tool.Refused):
            tool.probe(42, "a" * 64, path, rec, rpc=rpc, proc_root=proc)
    finally:
        if mapped is not None:
            mapped.close()
