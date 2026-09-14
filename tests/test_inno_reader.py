"""Regressions for reading a third-party installer's own Inno metadata.

The download helper this parses is hostile input: it is fetched from a rotating
URL, its bytes differ on every download, and it is signed by another company.
Every case here is either a layout that must be accepted or damage that must be
refused. Fixtures are synthetic - the real helper must never enter Git.
"""
from __future__ import annotations

import lzma
import struct
import zlib

import pytest

from ce_decky import inno_reader
from ce_decky.inno_reader import (
    metadata_strings,
    metadata_urls,
    read_setup_metadata,
)


def _crc(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def _lzma1(payload: bytes) -> bytes:
    """Inno's raw LZMA1 stream: a hand-built 5-byte property header, no EOS."""
    dict_size = 1 << 20
    lc, lp, pb = 3, 0, 2
    raw = lzma.compress(
        payload,
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                  "lc": lc, "lp": lp, "pb": pb}],
    )
    return bytes([(pb * 5 + lp) * 9 + lc]) + struct.pack("<I", dict_size) + raw


def _block(payload: bytes, *, compressed: bool = False, wide: bool = True) -> bytes:
    framed = b""
    for start in range(0, len(payload), inno_reader.CHUNK_SIZE):
        chunk = payload[start:start + inno_reader.CHUNK_SIZE]
        framed += struct.pack("<I", _crc(chunk)) + chunk
    header = struct.pack("<qB" if wide else "<IB", len(framed), int(compressed))
    return struct.pack("<I", _crc(header)) + header + framed


def _encryption_header(use: int = 0) -> bytes:
    body = bytes([use]) + b"\x00" * (inno_reader.ENCRYPTION_HEADER_SIZE - 1)
    return struct.pack("<I", _crc(body)) + body


def _setup_id(version: str = "6.7.0") -> bytes:
    text = f"Inno Setup Setup Data ({version})".encode("ascii")
    return text.ljust(inno_reader.SETUP_ID_SIZE, b"\x00")


def _installer(
    *,
    revision: int = 2,
    blocks: tuple[bytes, ...] | None = None,
    encryption: bytes | None = None,
    setup_id: bytes | None = None,
    trailing: bytes = b"",
) -> bytearray:
    prefix = b"MZ" + b"\x00" * 62
    setup0 = setup_id if setup_id is not None else _setup_id()
    setup0 += encryption if encryption is not None else b""
    if blocks is None:
        blocks = (_block(b"header"), _block(b"locations"))
    setup0 += b"".join(blocks) + trailing

    table_size = 44 if revision == 1 else 64
    header_offset = len(prefix) + table_size
    data_offset = header_offset + len(setup0)
    if revision == 1:
        fields = struct.pack("<IIIIiII", revision, 0, 0, 0, 0, header_offset, data_offset)
    else:
        fields = struct.pack("<IqqIiqqI", revision, 0, 0, 0, 0, header_offset, data_offset, 0)
    table = inno_reader.LOADER_TABLE_ID + fields
    table += struct.pack("<I", _crc(table))
    return bytearray(prefix + table + setup0)


def test_a_current_installer_with_an_encryption_header_is_read():
    metadata = read_setup_metadata(bytes(_installer(encryption=_encryption_header())))
    assert metadata.setup_id == "Inno Setup Setup Data (6.7.0)"
    assert metadata.revision == 2
    assert metadata.blocks == (b"header", b"locations")


def test_an_older_installer_with_a_narrow_block_header_is_read():
    # Older releases predate both the encryption header and the 64-bit
    # StoredSize, and each variant has to be recognized from the file itself.
    metadata = read_setup_metadata(bytes(_installer(
        revision=1,
        setup_id=_setup_id("6.4.0.1"),
        blocks=(_block(b"header", wide=False), _block(b"locations", wide=False)),
    )))
    assert metadata.revision == 1
    assert metadata.blocks == (b"header", b"locations")


def test_a_compressed_block_is_decompressed():
    payload = b"compressible " * 500
    exe = bytes(_installer(blocks=(_block(_lzma1(payload), compressed=True),)))
    assert read_setup_metadata(exe).blocks == (payload,)


def test_only_the_two_setup0_blocks_are_read():
    # The compressed Setup EXE that follows setup-0 reuses the same framing, so
    # a reader that kept going would report it as a third metadata block.
    exe = bytes(_installer(trailing=_block(b"this is the setup exe")))
    assert read_setup_metadata(exe).blocks == (b"header", b"locations")


def test_the_loader_table_must_be_present_exactly_once():
    exe = bytearray(_installer())
    exe[:12] = inno_reader.LOADER_TABLE_ID
    with pytest.raises(ValueError, match="exactly one Inno loader table"):
        read_setup_metadata(bytes(exe))
    with pytest.raises(ValueError, match="exactly one Inno loader table"):
        read_setup_metadata(b"no loader table here")


@pytest.mark.parametrize("damage, message", [
    ("crc", "checksum does not match"),
    ("revision", "revision 3"),
    ("offset", "points outside the installer"),
])
def test_a_damaged_loader_table_fails_closed(damage: str, message: str):
    exe = bytearray(_installer())
    position = exe.index(inno_reader.LOADER_TABLE_ID)
    if damage == "crc":
        exe[position + 16] ^= 1
    elif damage == "revision":
        struct.pack_into("<I", exe, position + 12, 3)
    else:
        struct.pack_into("<q", exe, position + 40, len(exe) * 4)
        struct.pack_into("<I", exe, position + 60, _crc(bytes(exe[position:position + 60])))
    with pytest.raises(ValueError, match=message):
        read_setup_metadata(bytes(exe))


def test_the_loader_must_point_at_setup_metadata():
    exe = bytes(_installer(setup_id=b"Some Other Installer".ljust(64, b"\x00")))
    with pytest.raises(ValueError, match="does not point at setup metadata"):
        read_setup_metadata(exe)


def test_damaged_block_framing_is_refused_rather_than_partially_read():
    exe = bytearray(_installer(blocks=(_block(b"header"),)))
    exe[-1] ^= 1
    with pytest.raises(ValueError, match="chunk checksum does not match"):
        read_setup_metadata(bytes(exe))


@pytest.mark.parametrize("use", [1, 2])
def test_encrypted_metadata_is_refused_instead_of_guessed(use: int):
    exe = bytes(_installer(encryption=_encryption_header(use)))
    with pytest.raises(ValueError, match="encrypted"):
        read_setup_metadata(exe)


def test_a_block_claiming_more_than_the_bound_is_refused():
    oversized = struct.pack("<qB", inno_reader.MAX_BLOCK_STORED_BYTES + 1, 0)
    block = struct.pack("<I", _crc(oversized)) + oversized
    exe = bytes(_installer(blocks=(block,)))
    with pytest.raises(ValueError, match="unreasonable size"):
        read_setup_metadata(exe)


def test_a_block_reaching_past_the_installer_is_refused():
    header = struct.pack("<qB", 4096, 0)
    block = struct.pack("<I", _crc(header)) + header
    exe = bytes(_installer(blocks=(block,)))
    with pytest.raises(ValueError, match="extends past the installer"):
        read_setup_metadata(exe)


def test_metadata_text_is_read_in_both_encodings():
    # Inno stores its own header fields as UTF-16LE while the compiled script
    # keeps some constants as single-byte text, so both have to be read.
    blocks = (
        "https://cdn.example/f/CheatEngine/2129/CheatEngine77.exe".encode("utf-16le"),
        b"https://ansi.example/plain-text-url",
    )
    strings = metadata_strings(blocks)
    assert "https://cdn.example/f/CheatEngine/2129/CheatEngine77.exe" in strings
    assert "https://ansi.example/plain-text-url" in strings
    assert metadata_urls(blocks) == [
        "https://ansi.example/plain-text-url",
        "https://cdn.example/f/CheatEngine/2129/CheatEngine77.exe",
    ]


def test_plain_http_is_not_reported_as_a_url():
    # Nothing downstream may follow a cleartext link, so it is never offered.
    assert metadata_urls((b"http://cdn.example/installer.exe",)) == []
