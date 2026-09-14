"""Read an Inno Setup installer's own metadata without executing it.

The download page's Windows button does not serve the Cheat Engine installer. It
serves a third-party download manager whose file name is randomized on every
page load, whose bytes differ on every download, and which is signed by another
company entirely. The clean installer's URL is a literal inside that stub's
compiled Inno script, and reading it back is how CE Decky rediscovers a rotated
artifact URL.

The stub is hostile input and is never executed: it is parsed here, in a reader
with a hard bound on every length it reads. `ce77_extractor` is the sibling of
this module and goes much further, decoding an exact reviewed artifact's payload
tables; this one stops at the metadata strings, because a URL is all that is
wanted from a file nothing trusts.

Format notes
------------
Read from the Inno Setup sources (`jrsoftware/issrc`, tag `is-6_7_0`,
`Projects/Src/Shared.Struct.pas` and `Projects/Src/Compression.Base.pas`). Three
things differ between the Inno 6.4 layout `ce77_extractor` handles and the 6.7
layout the current stub uses, and each is proven from the file rather than
assumed, because every variant carries its own CRC-32:

* `TSetupLdrOffsetTable.Version` is 1 or 2; revision 2 widened four fields to
  `Int64` and added padding, making the table 64 bytes instead of 44.
* `TCompressedBlockHeader.StoredSize` is `Int64` in current releases and `Int32`
  in older ones, making the block header 13 or 9 bytes.
* Releases from 6.5 onward store a `TSetupEncryptionHeader` between the setup ID
  and the first compressed block.
"""

from __future__ import annotations

from dataclasses import dataclass
import lzma
import re
import struct
import zlib

LOADER_TABLE_ID = b"rDlPtS\xcd\xe6\xd7{\x0b*"
SETUP_ID_SIZE = 64
SETUP_ID_PREFIX = b"Inno Setup Setup Data ("
CHUNK_SIZE = 4096

# EncryptionUse(1) + KDFSalt(16) + KDFIterations(4) + BaseNonce(24) +
# PasswordTest(4). BaseNonce is an unpacked nested record whose Int64 comes
# first, so it needs no interior padding.
ENCRYPTION_HEADER_SIZE = 49

MAX_BLOCK_STORED_BYTES = 64 * 1024 * 1024
MAX_BLOCK_DECODED_BYTES = 128 * 1024 * 1024
MAX_STRING_CHARS = 4096

# (revision, table size, layout after the 12-byte ID, field names)
_TABLE_LAYOUTS = (
    (1, 44, "<IIIIiIIi", ("revision", "total_size", "exe_offset",
                          "exe_uncompressed_size", "exe_crc32",
                          "header_offset", "data_offset", "table_crc")),
    (2, 64, "<IqqIiqqIi", ("revision", "total_size", "exe_offset",
                           "exe_uncompressed_size", "exe_crc32",
                           "header_offset", "data_offset", "padding",
                           "table_crc")),
)


@dataclass(frozen=True)
class SetupMetadata:
    setup_id: str
    revision: int
    blocks: tuple[bytes, ...]


def _crc32(buf: bytes) -> int:
    return zlib.crc32(buf) & 0xFFFFFFFF


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _parse_offset_table(exe: bytes) -> tuple[int, int]:
    """Return `(revision, setup-0 offset)` from a CRC-validated loader table."""
    positions: list[int] = []
    start = 0
    while True:
        found = exe.find(LOADER_TABLE_ID, start)
        if found < 0:
            break
        positions.append(found)
        start = found + 1
    if len(positions) != 1:
        raise ValueError("installer does not carry exactly one Inno loader table")

    pos = positions[0]
    if pos + 16 > len(exe):
        raise ValueError("Inno loader table is truncated")
    revision = _u32(exe, pos + 12)
    for known_revision, size, layout, names in _TABLE_LAYOUTS:
        if known_revision != revision:
            continue
        if pos + size > len(exe):
            raise ValueError("Inno loader table is truncated")
        values = dict(zip(names, struct.unpack_from(layout, exe, pos + 12)))
        if (values["table_crc"] & 0xFFFFFFFF) != _crc32(exe[pos:pos + size - 4]):
            raise ValueError("Inno loader table checksum does not match")
        header_offset = values["header_offset"]
        if not 0 < header_offset < len(exe):
            raise ValueError("Inno loader table points outside the installer")
        return revision, header_offset
    raise ValueError(f"unsupported Inno loader table revision {revision}")


def _read_block_header(exe: bytes, pos: int) -> tuple[int, bool, int]:
    """Return `(stored_size, compressed, body_offset)`, choosing the layout by CRC."""
    for size_format, header_size in (("<qB", 13), ("<IB", 9)):
        if pos < 0 or pos + header_size > len(exe):
            continue
        body = exe[pos + 4:pos + header_size]
        if _crc32(body) != _u32(exe, pos):
            continue
        stored_size, compressed = struct.unpack_from(size_format, body, 0)
        if compressed not in (0, 1):
            continue
        if not 0 <= stored_size <= MAX_BLOCK_STORED_BYTES:
            raise ValueError("Inno metadata block declares an unreasonable size")
        return stored_size, bool(compressed), pos + header_size
    raise ValueError("Inno metadata block header is not readable")


def _decode_lzma1(raw: bytes) -> bytes:
    """Decode Inno's raw LZMA1 stream: 5-byte properties, no end marker."""
    if len(raw) < 5:
        raise ValueError("Inno metadata stream is truncated")
    prop = raw[0]
    if prop > 9 * 5 * 5:
        raise ValueError("Inno metadata stream declares invalid LZMA properties")
    pb, rem = divmod(prop, 9 * 5)
    lp, lc = divmod(rem, 9)
    decompressor = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=[{
            "id": lzma.FILTER_LZMA1,
            "dict_size": _u32(raw, 1) or 1,
            "lc": lc, "lp": lp, "pb": pb,
        }],
    )
    try:
        out = decompressor.decompress(raw[5:], MAX_BLOCK_DECODED_BYTES)
    except lzma.LZMAError as exc:
        raise ValueError("Inno metadata stream could not be decompressed") from exc
    if not decompressor.eof and not decompressor.needs_input:
        raise ValueError("Inno metadata expands past its decompression bound")
    return out


def _read_block(exe: bytes, pos: int) -> tuple[bytes, int]:
    stored_size, compressed, body = _read_block_header(exe, pos)
    end = body + stored_size
    if end > len(exe):
        raise ValueError("Inno metadata block extends past the installer")

    framed = bytearray()
    cursor = body
    while cursor < end:
        if cursor + 5 > end:
            raise ValueError("Inno metadata chunk header is truncated")
        chunk_crc = _u32(exe, cursor)
        cursor += 4
        length = min(CHUNK_SIZE, end - cursor)
        chunk = exe[cursor:cursor + length]
        if _crc32(chunk) != chunk_crc:
            raise ValueError("Inno metadata chunk checksum does not match")
        framed += chunk
        cursor += length

    if not compressed:
        return bytes(framed), end
    return _decode_lzma1(bytes(framed)), end


def read_setup_metadata(exe: bytes) -> SetupMetadata:
    """Read the installer's own `setup-0` metadata blocks."""
    revision, pos = _parse_offset_table(exe)
    raw_id = exe[pos:pos + SETUP_ID_SIZE]
    if not raw_id.startswith(SETUP_ID_PREFIX):
        raise ValueError("Inno loader does not point at setup metadata")
    setup_id = raw_id.split(b"\x00", 1)[0].decode("ascii", "replace")
    pos += SETUP_ID_SIZE

    # Inno 6.5+ stores a CRC-checked encryption header here; older releases go
    # straight to the first block. Accept it only when its checksum proves it.
    if pos + 4 + ENCRYPTION_HEADER_SIZE <= len(exe):
        body = exe[pos + 4:pos + 4 + ENCRYPTION_HEADER_SIZE]
        if _crc32(body) == _u32(exe, pos):
            if body[0] != 0:
                raise ValueError(
                    "installer metadata is encrypted and would need a password"
                )
            pos += 4 + ENCRYPTION_HEADER_SIZE

    # setup-0 holds exactly two blocks: the setup header with its entry tables,
    # then the file locations. Stopping at two matters, because the compressed
    # Setup EXE that follows reuses the same framing.
    first, pos = _read_block(exe, pos)
    blocks = [first]
    try:
        second, pos = _read_block(exe, pos)
    except ValueError:
        pass  # A location-free installer is unusual, not a parse failure.
    else:
        blocks.append(second)
    return SetupMetadata(setup_id=setup_id, revision=revision, blocks=tuple(blocks))


_ASCII_RUN = re.compile(rb"[\x20-\x7e]{8,%d}" % MAX_STRING_CHARS)
_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){8,%d}" % MAX_STRING_CHARS)


def metadata_strings(blocks: tuple[bytes, ...]) -> list[str]:
    """Printable runs in the metadata, both ANSI and Inno's own UTF-16LE.

    Inno stores its header fields as UTF-16LE while the compiled script keeps
    some constants as single-byte text, so both encodings have to be read.
    """
    found: set[str] = set()
    for block in blocks:
        for match in _ASCII_RUN.finditer(block):
            found.add(match.group().decode("ascii"))
        for match in _UTF16_RUN.finditer(block):
            found.add(match.group().decode("utf-16le"))
    return sorted(found)


_URL = re.compile(r"https://[^\s\"'<>\\]{4,}")


def metadata_urls(blocks: tuple[bytes, ...]) -> list[str]:
    urls: set[str] = set()
    for text in metadata_strings(blocks):
        urls.update(_URL.findall(text))
    return sorted(urls)
