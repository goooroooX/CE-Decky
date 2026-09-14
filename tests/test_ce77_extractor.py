from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import pytest

from ce_decky import ce77_extractor as extractor


def _minimal_pe() -> bytes:
    payload = bytearray(68)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 64)
    payload[64:68] = b"PE\x00\x00"
    return bytes(payload)


def _loader_fixture() -> bytearray:
    payload = bytearray(360)
    table_offset = 16
    header_offset = 160
    data_offset = 320
    payload[table_offset:table_offset + len(extractor.LOADER_MAGIC)] = extractor.LOADER_MAGIC
    struct.pack_into(
        "<IIIIIII",
        payload,
        table_offset + 12,
        1,
        len(payload),
        280,
        123,
        456,
        header_offset,
        data_offset,
    )
    payload[header_offset:header_offset + len(extractor.SETUP_ID)] = extractor.SETUP_ID
    payload[data_offset:data_offset + len(extractor.DATA_MAGIC)] = extractor.DATA_MAGIC
    struct.pack_into("<I", payload, table_offset + 40, extractor.crc32(payload[table_offset:table_offset + 40]))
    return payload


def _refresh_loader_crc(payload: bytearray) -> None:
    table_offset = payload.index(extractor.LOADER_MAGIC)
    struct.pack_into("<I", payload, table_offset + 40, extractor.crc32(payload[table_offset:table_offset + 40]))


def _framed_block(payload: bytes, *, compressed: int = 0) -> bytes:
    stored_size = 4 + len(payload)
    header_tail = struct.pack("<IB", stored_size, compressed)
    return struct.pack("<I", extractor.crc32(header_tail)) + header_tail + struct.pack("<I", extractor.crc32(payload)) + payload


def test_reviewed_extractor_rejects_manifest_drift_and_existing_staging(tmp_path):
    installer = tmp_path / "CheatEngine77.exe"
    installer.write_bytes(b"installer")

    with pytest.raises(extractor.CE77ExtractionError, match="does not match"):
        extractor.extract_reviewed_installer(installer, tmp_path / "out", expected_size=9, expected_sha256="0" * 64)

    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(extractor.CE77ExtractionError, match="already exists"):
        extractor.extract_reviewed_installer(
            installer,
            output,
            expected_size=extractor.EXPECTED_SIZE,
            expected_sha256=extractor.EXPECTED_SHA256,
        )


def test_reviewed_extractor_rejects_non_regular_input_and_identity_changes(tmp_path, monkeypatch):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"original")
    link = tmp_path / "link.exe"
    link.symlink_to(installer)
    with pytest.raises(extractor.CE77ExtractionError, match="regular non-symlink"):
        extractor._installer_identity(link, len(b"original"))

    monkeypatch.setattr(extractor, "EXPECTED_SIZE", len(b"original"))
    monkeypatch.setattr(extractor, "EXPECTED_SHA256", hashlib.sha256(b"original").hexdigest())

    def mutate_during_run(source: Path, output: Path, allow_other_hash: bool, *, raise_errors: bool = False) -> int:
        assert not allow_other_hash and raise_errors
        output.mkdir()
        source.write_bytes(b"modified")
        return 0

    monkeypatch.setattr(extractor, "run", mutate_during_run)
    with pytest.raises(extractor.CE77ExtractionError, match="changed during"):
        extractor.extract_reviewed_installer(
            installer,
            tmp_path / "out",
            expected_size=len(b"original"),
            expected_sha256=hashlib.sha256(b"original").hexdigest(),
        )


def test_loader_revision_one_parses_exact_offsets_and_crc():
    payload = _loader_fixture()
    offsets = extractor.parse_loader_rev1(bytes(payload))
    assert offsets.table_offset == 16
    assert offsets.header_offset == 160
    assert offsets.data_offset == 320
    assert offsets.exe_offset == 280


@pytest.mark.parametrize("damage, message", [
    ("missing", "found 0"),
    ("duplicate", "found 2"),
    ("truncated", "Truncated"),
    ("revision", "revision 1"),
    ("header-offset", "Invalid setup header offset"),
    ("data-magic", "does not point"),
    ("crc", "CRC mismatch"),
])
def test_loader_parser_fails_closed_on_structural_damage(damage, message):
    payload = _loader_fixture()
    table_offset = 16
    if damage == "missing":
        payload[table_offset:table_offset + 12] = b"x" * 12
    elif damage == "duplicate":
        payload[80:92] = extractor.LOADER_MAGIC
    elif damage == "truncated":
        payload = bytearray(extractor.LOADER_MAGIC + b"\x00" * 10)
    elif damage == "revision":
        struct.pack_into("<I", payload, table_offset + 12, 2)
        _refresh_loader_crc(payload)
    elif damage == "header-offset":
        struct.pack_into("<I", payload, table_offset + 32, len(payload) + 1)
        _refresh_loader_crc(payload)
    elif damage == "data-magic":
        payload[320:324] = b"nope"
    elif damage == "crc":
        payload[table_offset + 40] ^= 1

    with pytest.raises(RuntimeError, match=message):
        extractor.parse_loader_rev1(bytes(payload))


def test_physical_framing_checks_header_chunk_crc_and_truncation():
    block = _framed_block(b"metadata")
    decoded, chunks, stored_size = extractor._read_physical_framed_payload(block, 0, len(block))
    assert decoded == b"metadata"
    assert chunks == 1
    assert stored_size == 12

    bad_header = bytearray(block)
    bad_header[0] ^= 1
    with pytest.raises(RuntimeError, match="Header CRC mismatch"):
        extractor._read_physical_framed_payload(bytes(bad_header), 0, len(bad_header))

    bad_chunk = bytearray(block)
    bad_chunk[-1] ^= 1
    with pytest.raises(RuntimeError, match="Chunk CRC mismatch"):
        extractor._read_physical_framed_payload(bytes(bad_chunk), 0, len(bad_chunk))

    with pytest.raises(RuntimeError, match="too short"):
        extractor._read_physical_framed_payload(block[:13], 0, 13)


def test_chunk_decoder_accepts_zlib_and_rejects_invalid_or_short_payloads():
    raw = zlib.compress(b"decoded payload")
    assert extractor.decode_chunk_payload(raw, 4) == (b"decoded payload", "zlib")
    with pytest.raises(RuntimeError, match="Could not decompress"):
        extractor.decode_chunk_payload(b"not compressed", 1)
    with pytest.raises(RuntimeError, match="decoded 15 < required 100"):
        extractor.decode_chunk_payload(raw, 100)


def test_app_paths_are_confined_to_the_install_root():
    assert extractor.safe_app_path(r"{app}\bin\CheatEngine.exe") == Path("bin/CheatEngine.exe")
    assert extractor.safe_app_path("{app}") is None
    assert extractor.safe_app_path(r"{tmp}\file") is None
    with pytest.raises(RuntimeError, match="Unsafe '..'"):
        extractor.safe_app_path(r"{app}\..\outside")
    with pytest.raises(RuntimeError, match="Unsafe ':'"):
        extractor.safe_app_path(r"{app}\C:\outside")


def test_pe_validation_checks_dos_offset_and_nt_signature(tmp_path):
    executable = tmp_path / "CheatEngine.exe"
    executable.write_bytes(_minimal_pe())
    extractor.validate_pe_executable(executable)

    for name, payload, message in (
        ("small.exe", b"MZ", "too small"),
        ("dos.exe", b"x" * 68, "Missing MZ"),
        ("offset.exe", b"MZ" + b"\x00" * 66, "Invalid PE header offset"),
        ("signature.exe", _minimal_pe()[:64] + b"NOPE", "Missing PE signature"),
    ):
        path = tmp_path / name
        path.write_bytes(payload)
        with pytest.raises(RuntimeError, match=message):
            extractor.validate_pe_executable(path)


def test_output_target_rejects_symlinks_and_failed_commit_restores_previous_tree(tmp_path, monkeypatch):
    installer = tmp_path / "installer.exe"
    installer.write_bytes(b"installer")
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink output"):
        extractor.validate_output_target(installer, output_link)

    output = tmp_path / "output"
    output.mkdir()
    (output / "old.txt").write_text("old", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    real_replace = Path.replace

    def fail_staging_commit(self: Path, target: Path):
        if self == staging:
            raise OSError("simulated commit failure")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_staging_commit)
    with pytest.raises(OSError, match="simulated"):
        extractor.commit_output(staging, output)
    assert (output / "old.txt").read_text(encoding="utf-8") == "old"
    assert not (output / "new.txt").exists()


def test_synthetic_stored_payload_extracts_and_validates(tmp_path):
    payload = _minimal_pe()
    installer = extractor.DATA_MAGIC + payload
    files = [extractor.FileEntry("", r"{app}\CheatEngine.exe", 0, 0, 0, 0, 0)]
    data_entries = [extractor.DataEntry(
        first_slice=0,
        last_slice=0,
        chunk_offset=0,
        file_offset=0,
        file_size=len(payload),
        chunk_size=len(payload),
        sha256=hashlib.sha256(payload).digest(),
        flags=0,
        sign=0,
    )]

    assert extractor.extract_files(installer, 0, files, data_entries, tmp_path) == (1, 0)
    extractor.validate_output(tmp_path)
    assert (tmp_path / "CheatEngine.exe").read_bytes() == payload
