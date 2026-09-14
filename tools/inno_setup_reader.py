#!/usr/bin/env python3
"""
inno_setup_reader.py — read an Inno Setup installer's own metadata without
executing it.

Purpose
-------
`ce77_extract.py` extracts the payload of the exact reviewed Cheat Engine 7.7
offline installer. This tool answers a different question: given *any* Inno
Setup executable, what does its own setup metadata say?

Its motivating case is the download button on cheatengine.org. That button
serves a third-party download manager, not the clean Cheat Engine installer:
the file name is randomized on every page load, the bytes differ on every
download, and it is signed by a different publisher. The clean installer's URL
is a literal inside that stub's compiled Inno script, which is how the upstream
Chocolatey packager rediscovers the URL when it rotates. Doing the same here
lets a maintainer re-resolve a dead manifest URL without Wine or innounp.

**This is a maintainer-side investigation tool.** Nothing here runs on a user's
device: CE Decky's production first-run path installs only the artifact its
packaged release manifest pins by exact host, size and SHA-256, and
`docs/DESIGN.md` lists runtime installer scraping as a non-goal. Use this to
propose a manifest update that a human reviews, never as an automatic source of
truth.

Dependency-free: Python 3.10+ standard library only. It never executes the
input, writes outside the requested output directory, or follows a redirect.

Usage
-----
    python3 inno_setup_reader.py info    <setup.exe>
    python3 inno_setup_reader.py strings <setup.exe> [--min N] [--grep RE]
    python3 inno_setup_reader.py urls    <setup.exe>
    python3 inno_setup_reader.py dump    <setup.exe> <output-dir>

`urls` is the one that resolves a rotated Cheat Engine installer URL.

Format notes
------------
Everything below was read from the Inno Setup sources (`jrsoftware/issrc`,
tag `is-6_7_0`, `Projects/Src/Shared.Struct.pas` and
`Projects/Src/Compression.Base.pas`) and then confirmed against real artifacts.
Three things changed between the Inno 6.4 layout `ce77_extract.py` handles and
the 6.7 layout the current stub uses, and each is detected from the file rather
than assumed:

* `TSetupLdrOffsetTable.Version` is 1 or 2. Revision 2 widened `TotalSize`,
  `OffsetEXE`, `Offset0` and `Offset1` to `Int64` and added a padding field, so
  the table is 64 bytes instead of 44.
* `TCompressedBlockHeader.StoredSize` is `Int64` in current releases and
  `Int32` in older ones, making the block header 13 or 9 bytes.
* Releases from 6.5 onward store a `TSetupEncryptionHeader` between the setup
  ID and the first compressed block.

Each variant carries its own CRC-32, so the reader tries the candidates and
keeps the one whose checksum proves it. Misdetection is not silently possible.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import lzma
import re
import struct
import sys
import zlib


# --- Structural constants, from Shared.Struct.pas / Compression.Base.pas ------

LOADER_TABLE_ID = b"rDlPtS\xcd\xe6\xd7{\x0b*"
SETUP_ID_SIZE = 64
SETUP_ID_PREFIX = b"Inno Setup Setup Data ("
DATA_MAGIC = b"zlb\x1a"
CHUNK_SIZE = 4096

# EncryptionUse(1) + KDFSalt(16) + KDFIterations(4) + BaseNonce(24) +
# PasswordTest(4). BaseNonce is an unpacked nested record whose Int64 comes
# first, so it needs no interior padding.
ENCRYPTION_HEADER_SIZE = 49

# --- Bounds. The input is untrusted, so every read is capped. ----------------

MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_BLOCK_STORED_BYTES = 64 * 1024 * 1024
MAX_BLOCK_DECODED_BYTES = 256 * 1024 * 1024
MAX_STRING_CHARS = 4096


class InnoFormatError(ValueError):
    """The input is not an Inno Setup installer this reader can parse."""


@dataclass(frozen=True)
class OffsetTable:
    table_offset: int
    revision: int
    total_size: int
    exe_offset: int
    exe_uncompressed_size: int
    exe_crc32: int
    header_offset: int
    data_offset: int


@dataclass(frozen=True)
class SetupData:
    setup_id: str
    encrypted: bool
    encryption_use: int
    blocks: tuple[bytes, ...]


def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def _crc32(buf: bytes) -> int:
    return zlib.crc32(buf) & 0xFFFFFFFF


def _find_all(buf: bytes, needle: bytes) -> list[int]:
    positions: list[int] = []
    start = 0
    while True:
        found = buf.find(needle, start)
        if found < 0:
            return positions
        positions.append(found)
        start = found + 1


# --- Loader offset table -----------------------------------------------------

# (revision, size, struct format, field order after the 12-byte ID)
_TABLE_LAYOUTS = (
    (1, 44, "<IIIIiIIi", ("revision", "total_size", "exe_offset",
                          "exe_uncompressed_size", "exe_crc32",
                          "header_offset", "data_offset", "table_crc")),
    (2, 64, "<IqqIiqqIi", ("revision", "total_size", "exe_offset",
                           "exe_uncompressed_size", "exe_crc32",
                           "header_offset", "data_offset", "padding",
                           "table_crc")),
)


def parse_offset_table(exe: bytes) -> OffsetTable:
    """Locate and CRC-validate `TSetupLdrOffsetTable`."""
    positions = _find_all(exe, LOADER_TABLE_ID)
    if len(positions) != 1:
        raise InnoFormatError(
            f"expected exactly one Inno loader table, found {len(positions)}"
        )
    pos = positions[0]
    if pos + 16 > len(exe):
        raise InnoFormatError("truncated Inno loader table")
    revision = _u32(exe, pos + 12)

    for known_revision, size, layout, names in _TABLE_LAYOUTS:
        if known_revision != revision:
            continue
        if pos + size > len(exe):
            raise InnoFormatError(
                f"truncated revision-{revision} loader table"
            )
        values = dict(zip(names, struct.unpack_from(layout, exe, pos + 12)))
        # Inno checksums every field before TableCRC itself.
        stored = values["table_crc"] & 0xFFFFFFFF
        actual = _crc32(exe[pos:pos + size - 4])
        if stored != actual:
            raise InnoFormatError(
                f"loader table CRC mismatch: stored=0x{stored:08x} "
                f"actual=0x{actual:08x}"
            )
        table = OffsetTable(
            table_offset=pos,
            revision=revision,
            total_size=values["total_size"],
            exe_offset=values["exe_offset"],
            exe_uncompressed_size=values["exe_uncompressed_size"],
            exe_crc32=values["exe_crc32"] & 0xFFFFFFFF,
            header_offset=values["header_offset"],
            data_offset=values["data_offset"],
        )
        if not 0 < table.header_offset < len(exe):
            raise InnoFormatError(
                f"invalid setup-0 offset: {table.header_offset}"
            )
        return table

    raise InnoFormatError(
        f"unsupported Inno loader table revision {revision}"
    )


# --- Compressed blocks -------------------------------------------------------

def _read_block_header(exe: bytes, pos: int) -> tuple[int, bool, int]:
    """Return `(stored_size, compressed, body_offset)` for one block header.

    `TCompressedBlockHeader.StoredSize` is `Int64` in current Inno releases and
    `Int32` in older ones. Both layouts prefix a CRC-32 of the header body, so
    the correct one identifies itself.
    """
    for size_format, header_size in (("<qB", 13), ("<IB", 9)):
        if pos + header_size > len(exe):
            continue
        stored_crc = _u32(exe, pos)
        body = exe[pos + 4:pos + header_size]
        if _crc32(body) != stored_crc:
            continue
        stored_size, compressed = struct.unpack_from(size_format, body, 0)
        if compressed not in (0, 1):
            continue
        if not 0 <= stored_size <= MAX_BLOCK_STORED_BYTES:
            raise InnoFormatError(
                f"compressed block claims {stored_size} stored bytes"
            )
        return stored_size, bool(compressed), pos + header_size
    raise InnoFormatError(f"no valid compressed-block header at offset {pos}")


def _read_block(exe: bytes, pos: int) -> tuple[bytes, int]:
    """Read one `TCompressedBlockReader` block; return its bytes and the end."""
    stored_size, compressed, body = _read_block_header(exe, pos)
    end = body + stored_size
    if end > len(exe):
        raise InnoFormatError("compressed block extends past end of file")

    framed = bytearray()
    cursor = body
    while cursor < end:
        if cursor + 5 > end:
            raise InnoFormatError("truncated compressed-block chunk header")
        chunk_crc = _u32(exe, cursor)
        cursor += 4
        length = min(CHUNK_SIZE, end - cursor)
        chunk = exe[cursor:cursor + length]
        if _crc32(chunk) != chunk_crc:
            raise InnoFormatError(
                f"compressed-block chunk CRC mismatch at offset {cursor}"
            )
        framed += chunk
        cursor += length

    if not compressed:
        return bytes(framed), end
    return _decode_lzma1(bytes(framed)), end


def _decode_lzma1(raw: bytes) -> bytes:
    """Decode Inno's raw LZMA1 stream (5-byte properties, no end marker)."""
    if len(raw) < 5:
        raise InnoFormatError("truncated LZMA1 stream")
    prop = raw[0]
    if prop > 9 * 5 * 5:
        raise InnoFormatError("invalid LZMA1 property byte")
    pb, rem = divmod(prop, 9 * 5)
    lp, lc = divmod(rem, 9)
    dict_size = _u32(raw, 1) or 1
    decompressor = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=[{
            "id": lzma.FILTER_LZMA1,
            "dict_size": dict_size,
            "lc": lc,
            "lp": lp,
            "pb": pb,
        }],
    )
    out = decompressor.decompress(raw[5:], MAX_BLOCK_DECODED_BYTES)
    if not decompressor.eof and not decompressor.needs_input:
        raise InnoFormatError(
            f"LZMA1 stream exceeds the {MAX_BLOCK_DECODED_BYTES}-byte bound"
        )
    return out


def read_setup_data(exe: bytes, table: OffsetTable) -> SetupData:
    """Read every `setup-0` metadata block the installer stores inline."""
    pos = table.header_offset
    raw_id = exe[pos:pos + SETUP_ID_SIZE]
    if not raw_id.startswith(SETUP_ID_PREFIX):
        raise InnoFormatError(
            f"setup-0 offset does not carry an Inno setup ID: "
            f"{raw_id[:32]!r}"
        )
    setup_id = raw_id.split(b"\x00", 1)[0].decode("ascii", "replace")
    pos += SETUP_ID_SIZE

    # Inno 6.5+ stores a CRC-checked encryption header here. Older releases go
    # straight to the first block, so accept the header only if its CRC proves
    # it is present.
    encryption_use = 0
    if pos + 4 + ENCRYPTION_HEADER_SIZE <= len(exe):
        stored_crc = _u32(exe, pos)
        body = exe[pos + 4:pos + 4 + ENCRYPTION_HEADER_SIZE]
        if _crc32(body) == stored_crc:
            encryption_use = body[0]
            pos += 4 + ENCRYPTION_HEADER_SIZE

    if encryption_use != 0:
        # euFiles(1)/euFull(2) need the user's password and XChaCha20; refuse
        # rather than return half-read metadata.
        raise InnoFormatError(
            f"setup data is encrypted (EncryptionUse={encryption_use}); "
            "a password would be required"
        )

    # setup-0 holds exactly two blocks: the setup header with its entry tables,
    # then the file-location entries (Setup.MainFunc.pas creates one reader for
    # each). Stopping at two matters, because the compressed Setup EXE that
    # follows uses the same block framing and would otherwise be read as if it
    # were metadata.
    try:
        first, pos = _read_block(exe, pos)
    except InnoFormatError as exc:
        raise InnoFormatError(
            f"setup-0 metadata is unreadable ({exc}); the block framing matches "
            "no known Inno layout, which is what a repacked or hand-modified "
            "installer looks like"
        ) from exc
    blocks = [first]
    try:
        second, pos = _read_block(exe, pos)
    except InnoFormatError:
        pass  # A location-free installer is unusual but not a parse failure.
    else:
        blocks.append(second)
    return SetupData(
        setup_id=setup_id,
        encrypted=encryption_use != 0,
        encryption_use=encryption_use,
        blocks=tuple(blocks),
    )


def read_bounded(path: Path) -> bytes:
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise InnoFormatError(
            f"{path.name} is {size} bytes, above the "
            f"{MAX_INPUT_BYTES}-byte reader bound"
        )
    return path.read_bytes()


def read_installer(path: Path) -> tuple[OffsetTable, SetupData]:
    exe = read_bounded(path)
    table = parse_offset_table(exe)
    return table, read_setup_data(exe, table)


# --- Text extraction ---------------------------------------------------------

_ASCII_RUN = re.compile(rb"[\x20-\x7e]{4,%d}" % MAX_STRING_CHARS)
_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){4,%d}" % MAX_STRING_CHARS)


def extract_strings(blocks: tuple[bytes, ...], *, minimum: int = 4) -> list[str]:
    """Every printable run in the metadata, both ANSI and Inno's UTF-16LE.

    Inno stores its own header fields as UTF-16LE, while the compiled Pascal
    script keeps some constants as single-byte text, so both encodings matter.
    """
    found: set[str] = set()
    for block in blocks:
        for match in _ASCII_RUN.finditer(block):
            text = match.group().decode("ascii")
            if len(text) >= minimum:
                found.add(text)
        for match in _UTF16_RUN.finditer(block):
            text = match.group().decode("utf-16le")
            if len(text) >= minimum:
                found.add(text)
    return sorted(found)


_URL = re.compile(r"https?://[^\s\"'<>\\]{4,}")


def extract_urls(blocks: tuple[bytes, ...]) -> list[str]:
    urls: set[str] = set()
    for text in extract_strings(blocks, minimum=8):
        urls.update(_URL.findall(text))
    return sorted(urls)


CE_INSTALLER_URL = re.compile(
    r"https://[\w.-]+/f/CheatEngine/\d+/CheatEngine\d+\.exe",
    re.IGNORECASE,
)


def resolve_cheat_engine_installer(blocks: tuple[bytes, ...]) -> list[str]:
    """Clean Cheat Engine installer URLs literal in the stub's script."""
    return sorted({
        url for url in extract_urls(blocks) if CE_INSTALLER_URL.fullmatch(url)
    })


# --- Commands ----------------------------------------------------------------

def _cmd_info(args: argparse.Namespace) -> int:
    exe = read_bounded(args.installer)
    table = parse_offset_table(exe)
    print(f"loader table offset : {table.table_offset}")
    print(f"loader revision     : {table.revision}")
    print(f"declared total size : {table.total_size}")
    print(f"setup-0 offset      : {table.header_offset}")
    print(f"setup-1 offset      : {table.data_offset}")
    # The loader table is worth reporting even when the metadata is unreadable:
    # that combination is exactly what a repacked installer looks like.
    try:
        data = read_setup_data(exe, table)
    except InnoFormatError as exc:
        print(f"setup-0 metadata    : unreadable ({exc})")
        return 1
    print(f"setup id            : {data.setup_id}")
    print(f"encryption use      : {data.encryption_use} "
          f"({'none' if not data.encrypted else 'password required'})")
    for index, block in enumerate(data.blocks):
        print(f"metadata block {index}    : {len(block)} bytes")
    return 0


def _cmd_strings(args: argparse.Namespace) -> int:
    _table, data = read_installer(args.installer)
    pattern = re.compile(args.grep) if args.grep else None
    for text in extract_strings(data.blocks, minimum=args.min):
        if pattern is None or pattern.search(text):
            print(text)
    return 0


def _cmd_urls(args: argparse.Namespace) -> int:
    _table, data = read_installer(args.installer)
    resolved = resolve_cheat_engine_installer(data.blocks)
    for url in extract_urls(data.blocks):
        marker = "  <-- Cheat Engine installer" if url in resolved else ""
        print(f"{url}{marker}")
    if not resolved:
        print("\nNo Cheat Engine installer URL found in this installer.",
              file=sys.stderr)
        return 1
    return 0


def _cmd_dump(args: argparse.Namespace) -> int:
    _table, data = read_installer(args.installer)
    out: Path = args.output
    if out.exists():
        print(f"refusing to write into existing {out}", file=sys.stderr)
        return 1
    out.mkdir(parents=True)
    for index, block in enumerate(data.blocks):
        target = out / f"setup0-block{index}.bin"
        target.write_bytes(block)
        print(f"{target}: {len(block)} bytes")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read Inno Setup installer metadata without executing it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        node = sub.add_parser(name, help=help_text)
        node.add_argument("installer", type=Path)
        node.set_defaults(handler=handler)
        return node

    add("info", _cmd_info, "print structural offsets and metadata sizes")
    strings = add("strings", _cmd_strings, "print printable metadata strings")
    strings.add_argument("--min", type=int, default=6,
                         help="minimum string length (default: 6)")
    strings.add_argument("--grep", help="only print strings matching this regex")
    add("urls", _cmd_urls, "print URLs found in the metadata")
    dump = add("dump", _cmd_dump, "write decompressed metadata blocks to disk")
    dump.add_argument("output", type=Path)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (InnoFormatError, OSError, lzma.LZMAError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
