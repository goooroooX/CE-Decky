from pathlib import Path
import os

import pytest

from ce_decky.atomic import (
    DurabilityUnknownError,
    atomic_write_bytes,
    atomic_write_json,
    load_json,
    read_proc_bytes,
    read_regular_bytes,
)
import ce_decky.atomic as atomic_module


def test_load_json_roundtrip_and_missing_default(tmp_path: Path):
    path = tmp_path / "state.json"
    assert load_json(path, {"missing": True}) == {"missing": True}
    atomic_write_json(path, {"ok": 1})
    assert load_json(path, None) == {"ok": 1}


def test_load_json_rejects_symlink_dangling_and_non_regular(tmp_path: Path):
    real = tmp_path / "real.json"
    real.write_text('{"ok":true}')
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="regular file"):
        load_json(link, None)

    link.unlink()
    link.symlink_to(tmp_path / "missing.json")
    with pytest.raises(ValueError, match="regular file"):
        load_json(link, None)

    directory = tmp_path / "dir.json"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        load_json(directory, None)


def test_load_json_rejects_empty_oversize_invalid_utf8_and_bad_bound(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_bytes(b"")
    with pytest.raises(ValueError, match="size"):
        load_json(path, None)
    path.write_bytes(b'{"x":"' + b"a" * 200 + b'"}')
    with pytest.raises(ValueError, match="size"):
        load_json(path, None, max_bytes=32)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8 JSON"):
        load_json(path, None)
    with pytest.raises(ValueError, match="max_bytes"):
        load_json(path, None, max_bytes=0)


def test_read_regular_bytes_is_bounded_and_no_follow(tmp_path: Path):
    from ce_decky.atomic import read_regular_bytes
    path = tmp_path / "blob"
    assert read_regular_bytes(path, max_bytes=10, allow_missing=True) is None
    path.write_bytes(b"abc")
    assert read_regular_bytes(path, max_bytes=3) == b"abc"
    with pytest.raises(ValueError, match="size"):
        read_regular_bytes(path, max_bytes=2)
    real = tmp_path / "real"
    real.write_bytes(b"x")
    path.unlink()
    path.symlink_to(real)
    with pytest.raises(ValueError, match="regular file"):
        read_regular_bytes(path, max_bytes=10)


def test_read_regular_bytes_does_not_treat_control_z_as_eof_on_windows(tmp_path: Path):
    from ce_decky.atomic import read_regular_bytes

    path = tmp_path / "binary"
    payload = b"before\x1aafter"
    path.write_bytes(payload)
    assert read_regular_bytes(path, max_bytes=len(payload)) == payload


def test_read_regular_bytes_with_stat_returns_same_descriptor_metadata(tmp_path: Path):
    from ce_decky.atomic import read_regular_bytes_with_stat
    path = tmp_path / "state.bin"
    path.write_bytes(b"hello")
    data, info = read_regular_bytes_with_stat(path, max_bytes=10)
    assert data == b"hello"
    assert info is not None and info.st_size == 5


def test_load_json_rejects_duplicate_keys_and_non_finite_numbers(tmp_path: Path):
    from ce_decky.atomic import load_json

    path = tmp_path / "state.json"
    path.write_text('{"schema": 1, "schema": 2}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate object key"):
        load_json(path, None)

    path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        load_json(path, None)


def test_atomic_write_json_refuses_payload_larger_than_reader_limit(tmp_path: Path):
    from ce_decky.atomic import atomic_write_json

    path = tmp_path / "state.json"
    with pytest.raises(ValueError, match="payload exceeds size limit"):
        atomic_write_json(path, {"value": "x" * 1024}, max_bytes=128)
    assert not path.exists()


def test_json_state_rejects_non_finite_constants_and_invalid_unicode(tmp_path: Path):
    path = tmp_path / "state.json"
    for token in ("NaN", "Infinity", "-Infinity"):
        path.write_text('{"value":' + token + '}')
        with pytest.raises(ValueError, match="non-finite"):
            load_json(path, None)
    with pytest.raises(ValueError, match="valid Unicode"):
        atomic_write_json(path, {"value": "bad\ud800"})


def test_load_json_rejects_same_inode_mutation_while_reading(tmp_path: Path, monkeypatch):
    import ce_decky.atomic as atomic

    path = tmp_path / "state.json"
    path.write_bytes(b'{"ok":1}')
    real_read = atomic.os.read
    changed = False

    def racing_read(fd: int, size: int) -> bytes:
        nonlocal changed
        if not changed:
            changed = True
            path.write_bytes(b'{"ok":2}')  # same length, same inode, changed metadata/content
        return real_read(fd, size)

    monkeypatch.setattr(atomic.os, "read", racing_read)
    with pytest.raises(ValueError, match="changed while reading"):
        atomic.load_json(path, None)


def test_posix_directory_fsync_open_failure_is_not_silently_accepted(tmp_path: Path, monkeypatch):
    if os.name != "posix":
        pytest.skip("strict directory fsync is authoritative on the POSIX/SteamOS target")

    import ce_decky.atomic as atomic

    def fail_open(_path: Path, _flags: int) -> int:
        raise OSError("directory fd unavailable")

    monkeypatch.setattr(atomic.os, "open", fail_open)
    with pytest.raises(OSError, match="directory fd unavailable"):
        atomic._fsync_dir(tmp_path)


@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="requires a real Linux procfs")
def test_proc_reader_accepts_the_synthetic_size_the_regular_reader_must_reject() -> None:
    """procfs and managed files need different descriptor-stability contracts.

    A managed file must read exactly ``st_size`` bytes, which is what makes an
    in-place overwrite detectable. procfs reports ``st_size == 0`` for records
    that still yield data, so the same check rejects every one of them.
    """
    record = Path("/proc/self/environ")
    assert read_proc_bytes(record, max_bytes=1 << 16)
    with pytest.raises(ValueError, match="changed while reading"):
        read_regular_bytes(record, max_bytes=1 << 16, allow_missing=True, allow_empty=True)


def test_a_failure_after_the_replacement_is_reported_as_durability_not_refusal(tmp_path: Path, monkeypatch):
    """The two halves of an atomic write are different facts for the caller.

    `os.replace` publishes the new content before the directory entry is synced,
    and a POSIX sync failure is deliberately re-raised. Everything downstream
    read one Python exception as proof the backend refused, so a control file
    Cheat Engine was already free to execute, or a profile already stored, was
    reported as definitely rejected - and the retry offered for it acted on
    state that had changed.
    """
    path = tmp_path / "state.json"
    path.write_bytes(b"old\n")

    def refuse(_directory):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(atomic_module, "fsync_directory", refuse)
    with pytest.raises(DurabilityUnknownError) as failure:
        atomic_write_bytes(path, b"new\n")
    # The content the caller wrote is what every reader now sees.
    assert path.read_bytes() == b"new\n"
    assert "may not survive a power loss" in str(failure.value)
    assert isinstance(failure.value.__cause__, OSError)


def test_a_failure_before_the_replacement_stays_an_ordinary_refusal(tmp_path: Path, monkeypatch):
    """Nothing was published, so nothing may describe the outcome as unknown."""
    path = tmp_path / "state.json"
    path.write_bytes(b"old\n")

    def refuse(_temp, _target):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(atomic_module.os, "replace", refuse)
    with pytest.raises(OSError) as failure:
        atomic_write_bytes(path, b"new\n")
    assert not isinstance(failure.value, DurabilityUnknownError)
    assert path.read_bytes() == b"old\n"


@pytest.mark.parametrize("operation", ["rename", "unlink"])
def test_directory_mutations_distinguish_refusal_from_post_commit_failure(tmp_path, monkeypatch, operation):
    from ce_decky import atomic
    source = tmp_path / "authority.json"
    destination = tmp_path / "quarantine.json"
    source.write_text("original")
    def refuse(_path):
        raise OSError("directory sync failed")
    monkeypatch.setattr(atomic, "fsync_directory", refuse)
    with pytest.raises(atomic.DurabilityUnknownError):
        if operation == "rename":
            atomic.durable_rename(source, destination)
        else:
            atomic.durable_unlink(source)
    assert not source.exists()
    if operation == "rename":
        assert destination.read_text() == "original"
    with pytest.raises(FileNotFoundError):
        atomic.durable_unlink(source)
