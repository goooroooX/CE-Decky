from pathlib import Path
import struct

import pytest

from ce_decky.ce_import import inspect_ce_selection


def _fake_pe(path: Path, size: int = 300_000) -> None:
    data = bytearray(size)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\x00\x00"
    path.write_bytes(data)


def test_import_explicit_ce_exe(tmp_path: Path):
    exe = tmp_path / "Cheat Engine.exe"
    _fake_pe(exe)
    result = inspect_ce_selection(str(exe))
    assert result.executable == str(exe.resolve())
    assert len(result.sha256) == 64


def test_import_directory_finds_known_ce_executable(tmp_path: Path):
    exe = tmp_path / "cheatengine-x86_64-SSE4-AVX2.exe"
    _fake_pe(exe)
    result = inspect_ce_selection(str(tmp_path))
    assert result.executable == str(exe.resolve())


def test_import_directory_prefers_the_generic_x64_binary_over_cpu_launcher_variants(tmp_path: Path):
    generic = tmp_path / "cheatengine-x86_64.exe"
    _fake_pe(generic)
    _fake_pe(tmp_path / "cheatengine-x86_64-SSE4-AVX2.exe")
    _fake_pe(tmp_path / "Cheat Engine.exe")

    result = inspect_ce_selection(str(tmp_path))

    assert result.executable == str(generic.resolve())


def test_arbitrary_renamed_exe_is_not_accepted(tmp_path: Path):
    exe = tmp_path / "random.exe"
    _fake_pe(exe)
    try:
        inspect_ce_selection(str(exe))
    except ValueError as exc:
        assert "recognizable as Cheat Engine" in str(exc)
    else:
        raise AssertionError("arbitrary executable was accepted")


def test_non_pe_file_named_like_ce_is_rejected(tmp_path: Path):
    exe = tmp_path / "Cheat Engine.exe"
    exe.write_bytes(b"not-pe" * 50_000)
    try:
        inspect_ce_selection(str(exe))
    except ValueError as exc:
        assert "PE/MZ" in str(exc)
    else:
        raise AssertionError("non-PE file was accepted")


def test_ce_import_rejects_path_identity_change_during_single_fd_inspection(tmp_path: Path, monkeypatch):
    import ce_decky.ce_import as module

    exe = tmp_path / "Cheat Engine.exe"
    _fake_pe(exe)
    real_stat = module.os.stat
    calls = 0

    def changed_stat(path, *args, **kwargs):
        nonlocal calls
        info = real_stat(path, *args, **kwargs)
        if Path(path) == exe and kwargs.get("follow_symlinks") is False:
            calls += 1
            values = list(info)
            # os.stat_result's tuple indices: st_mode, ino, dev, nlink, uid, gid, size, atime, mtime, ctime.
            values[1] = info.st_ino + 1
            return module.os.stat_result(values)
        return info

    monkeypatch.setattr(module.os, "stat", changed_stat)
    with pytest.raises(ValueError, match="path changed"):
        inspect_ce_selection(str(exe))
    assert calls == 1


def test_directory_import_rejects_windows_ambiguous_ce_names(tmp_path: Path):
    first = tmp_path / "Cheat Engine.exe"
    second = tmp_path / "CHEAT ENGINE.EXE"
    _fake_pe(first)
    _fake_pe(second)
    with pytest.raises(ValueError, match="Windows-ambiguous"):
        inspect_ce_selection(str(tmp_path))
