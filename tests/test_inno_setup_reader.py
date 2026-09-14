"""Regressions for the standalone Inno Setup metadata reader.

The reader parses an untrusted third-party installer, so every case here is
either a layout the reader must accept or damage it must refuse. Fixtures are
synthetic: the real artifacts rotate on every download and must never enter Git.
"""
from __future__ import annotations

import importlib.util
import lzma
import struct
import sys
import zlib
from pathlib import Path

import pytest


def _load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "inno_setup_reader.py"
    spec = importlib.util.spec_from_file_location("inno_setup_reader", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves its own module through `sys.modules`, so register
    # the standalone tool before executing it.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reader = _load_tool()


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
    """One TCompressedBlockReader block: header CRC, header, CRC'd chunks."""
    body = payload
    framed = b""
    for start in range(0, len(body), reader.CHUNK_SIZE):
        chunk = body[start:start + reader.CHUNK_SIZE]
        framed += struct.pack("<I", _crc(chunk)) + chunk
    header = struct.pack("<qB" if wide else "<IB", len(framed), int(compressed))
    return struct.pack("<I", _crc(header)) + header + framed


def _encryption_header(use: int = 0) -> bytes:
    body = bytes([use]) + b"\x00" * (reader.ENCRYPTION_HEADER_SIZE - 1)
    return struct.pack("<I", _crc(body)) + body


def _setup_id(version: str = "6.7.0") -> bytes:
    text = f"Inno Setup Setup Data ({version})".encode("ascii")
    return text.ljust(reader.SETUP_ID_SIZE, b"\x00")


def _installer(
    *,
    revision: int = 2,
    blocks: tuple[bytes, ...] | None = None,
    encryption: bytes | None = None,
    setup_id: bytes | None = None,
    trailing: bytes = b"",
) -> bytearray:
    """A minimal file with a valid loader table pointing at valid setup-0."""
    prefix = b"MZ" + b"\x00" * 62
    setup0 = (setup_id if setup_id is not None else _setup_id())
    setup0 += encryption if encryption is not None else b""
    if blocks is None:
        blocks = (_block(b"header"), _block(b"locations"))
    setup0 += b"".join(blocks) + trailing

    table_size = 44 if revision == 1 else 64
    header_offset = len(prefix) + table_size
    data_offset = header_offset + len(setup0)

    if revision == 1:
        fields = struct.pack(
            "<IIIIiII", revision, 0, 0, 0, 0, header_offset, data_offset,
        )
    else:
        fields = struct.pack(
            "<IqqIiqqI", revision, 0, 0, 0, 0, header_offset, data_offset, 0,
        )
    table = reader.LOADER_TABLE_ID + fields
    table += struct.pack("<i", struct.unpack("<i", struct.pack("<I", _crc(table)))[0])

    out = bytearray(prefix + table + setup0)
    out += reader.DATA_MAGIC
    return out


def test_revision_two_installer_with_encryption_header_is_read():
    exe = bytes(_installer(encryption=_encryption_header()))
    table = reader.parse_offset_table(exe)
    assert table.revision == 2

    data = reader.read_setup_data(exe, table)
    assert data.setup_id == "Inno Setup Setup Data (6.7.0)"
    assert data.encrypted is False
    assert data.blocks == (b"header", b"locations")


def test_revision_one_installer_with_narrow_block_header_is_read():
    # Older releases predate both the encryption header and the 64-bit
    # StoredSize, and each variant has to be recognized from the file itself.
    exe = bytes(_installer(
        revision=1,
        setup_id=_setup_id("6.4.0.1"),
        blocks=(_block(b"header", wide=False), _block(b"locations", wide=False)),
    ))
    table = reader.parse_offset_table(exe)
    assert table.revision == 1

    data = reader.read_setup_data(exe, table)
    assert data.setup_id == "Inno Setup Setup Data (6.4.0.1)"
    assert data.blocks == (b"header", b"locations")


def test_a_compressed_block_is_decompressed():
    payload = b"compressible " * 500
    exe = bytes(_installer(blocks=(_block(_lzma1(payload), compressed=True),)))
    data = reader.read_setup_data(exe, reader.parse_offset_table(exe))
    assert data.blocks == (payload,)


def test_only_the_two_setup0_blocks_are_read():
    # The compressed Setup EXE that follows setup-0 uses the same framing, so a
    # reader that kept going would report it as a third metadata block.
    exe = bytes(_installer(
        trailing=_block(b"this is the setup exe"),
    ))
    data = reader.read_setup_data(exe, reader.parse_offset_table(exe))
    assert data.blocks == (b"header", b"locations")


def test_loader_table_must_be_present_exactly_once():
    exe = bytearray(_installer())
    exe[:12] = reader.LOADER_TABLE_ID
    with pytest.raises(reader.InnoFormatError, match="found 2"):
        reader.parse_offset_table(bytes(exe))

    with pytest.raises(reader.InnoFormatError, match="found 0"):
        reader.parse_offset_table(b"no loader table here")


def test_loader_table_crc_and_revision_fail_closed():
    exe = bytearray(_installer())
    position = exe.index(reader.LOADER_TABLE_ID)
    exe[position + 16] ^= 1
    with pytest.raises(reader.InnoFormatError, match="table CRC mismatch"):
        reader.parse_offset_table(bytes(exe))

    exe = bytearray(_installer())
    struct.pack_into("<I", exe, position + 12, 3)
    with pytest.raises(reader.InnoFormatError, match="revision 3"):
        reader.parse_offset_table(bytes(exe))


def test_setup_zero_offset_must_carry_an_inno_setup_id():
    exe = bytes(_installer(setup_id=b"Some Other Installer".ljust(64, b"\x00")))
    with pytest.raises(reader.InnoFormatError, match="setup ID"):
        reader.read_setup_data(exe, reader.parse_offset_table(exe))


def test_damaged_block_framing_is_refused_rather_than_partially_read():
    exe = bytearray(_installer(blocks=(_block(b"header"),)))
    # Corrupt the chunk without touching its declared size or header CRC.
    exe[-len(reader.DATA_MAGIC) - 1] ^= 1
    with pytest.raises(reader.InnoFormatError, match="chunk CRC mismatch"):
        reader.read_setup_data(bytes(exe), reader.parse_offset_table(bytes(exe)))


def test_encrypted_setup_data_is_refused_instead_of_guessed():
    for use in (1, 2):
        exe = bytes(_installer(encryption=_encryption_header(use)))
        with pytest.raises(reader.InnoFormatError, match="encrypted"):
            reader.read_setup_data(exe, reader.parse_offset_table(exe))


def test_a_block_claiming_more_than_the_bound_is_refused():
    oversized = struct.pack("<qB", reader.MAX_BLOCK_STORED_BYTES + 1, 0)
    block = struct.pack("<I", _crc(oversized)) + oversized
    exe = bytes(_installer(blocks=(block,)))
    with pytest.raises(reader.InnoFormatError, match="stored bytes"):
        reader.read_setup_data(exe, reader.parse_offset_table(exe))


def test_utf16_metadata_yields_urls_and_resolves_the_installer_url():
    # Inno stores its own header fields as UTF-16LE, which is where the clean
    # installer URL and its silent arguments actually live.
    clean = "https://d1dj9aohuk02ls.cloudfront.net/f/CheatEngine/2129/CheatEngine77.exe"
    payload = (
        f"{clean}\x00/VERYSILENT /ZBDIST\x00https://example.invalid/offer"
    ).encode("utf-16le")
    blocks = (payload, b"https://ansi.example/plain-text-url")

    assert reader.resolve_cheat_engine_installer(blocks) == [clean]
    urls = reader.extract_urls(blocks)
    assert clean in urls
    assert "https://example.invalid/offer" in urls
    assert "https://ansi.example/plain-text-url" in urls
    assert "/VERYSILENT /ZBDIST" in reader.extract_strings(blocks)


def test_an_installer_without_a_cheat_engine_url_resolves_to_nothing():
    blocks = ("https://example.invalid/other.exe".encode("utf-16le"),)
    assert reader.resolve_cheat_engine_installer(blocks) == []
