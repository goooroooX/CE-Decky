#!/usr/bin/env python3
"""
ce77_extract.py — standalone Cheat Engine 7.7 installer extractor

Purpose
-------
Extract the Windows Cheat Engine payload from the known full/offline
CheatEngine77.exe without ever executing the installer.

The tool is intentionally dependency-free: Python 3.10+ standard library
only. It does not require Wine, Proton, 7z, innoextract, pip packages or
system modifications. The same code path is intended for Windows and Linux /
SteamOS and filesystem handling uses pathlib.

CE Decky production setup does not shell out to this file. Its backend contains
an adapted importable CE 7.7 parser with the same format-validation invariants;
this script remains standalone so a future reviewed installer change can be
investigated without the plugin runtime.

Default usage
-------------
Put this script next to CheatEngine77.exe and run it without positional
arguments:

    Windows:        python ce77_extract.py
    Linux/SteamOS:  python3 ce77_extract.py

The installer is auto-detected next to the script and output defaults to a
sibling directory named CheatEngine77. Explicit paths are also supported:

    python ce77_extract.py /path/to/CheatEngine77.exe /path/to/output

Diagnostic options:

    --verbose
        Show structural offsets, section ranges, decompression details and
        extraction progress. Default output is compact.

    --allow-other-hash
        Research mode for a newer/different installer. This bypasses only the
        outer pinned artifact size/SHA-256 check. Structural CRC validation,
        path safety and per-file SHA-256 verification remain mandatory.

Known pinned artifact
---------------------
    filename: CheatEngine77.exe
    size:     34,690,856 bytes
    SHA256:   cf0f4b6002555677984233c95856683959be83a5def3dc12175a8296eb22676b
    SetupID:  Inno Setup Setup Data (6.4.0.1)

Why this is CE-specific
-----------------------
The CE 7.7 full installer is Inno-based but modifies normal setup metadata.
Generic Inno unpackers therefore cannot be assumed to parse it correctly.
The notes below document the discoveries needed to maintain this tool.

1. Loader table
   The installer contains the canonical 12-byte Inno loader ID beginning with
   ``rDlPtS``. The known artifact uses loader revision 1. The table contains
   TotalSize, OffsetEXE, UncompressedSizeEXE, CRCEXE, Offset0, Offset1 and a
   table CRC32. The table CRC is checked before offsets are trusted.

2. Physical metadata layout
   Offset0 points at the 64-byte SetupID followed by TWO compressed metadata
   blocks. OffsetEXE is the physical end of block #2 and start of the separately
   compressed embedded Setup executable. Offset1 points at installer file data
   whose chunks begin with ``b"zlb\\x1a"``.

3. CE-modified metadata StoredSize
   Standard Inno compressed-block framing is still present: CRC32 (4),
   StoredSize (4), Compressed (1). CE 7.7 modifies StoredSize for the setup
   metadata blocks; in the pinned artifact it is exactly 32 times the physical
   CRC-framed byte count. This tool therefore derives physical boundaries from
   CRC-valid framing, requiring block #2 to end exactly at OffsetEXE.

4. Compression framing
   Apart from the StoredSize transformation, normal Inno CRC framing remains
   intact. Every metadata block header and every 4096-byte/final partial framed
   sub-block is CRC-checked before LZMA decompression.

5. CE post-LZMA metadata XOR
   LZMA output is still obfuscated. CE restarts this XOR stream at EACH original
   Inno Writer.Write call:

       key[i]   = (0xCE + 2*i) & 0xFF
       plain[i] = encoded[i] XOR key[i]

   Primary metadata was serialized via SECompressedBlockWrite, so the stream
   restarts independently for every 4-byte string length, every non-empty
   string body, and every packed fixed record tail. Secondary metadata contains
   packed 87-byte TSetupFileLocationEntry records; the stream restarts for each
   87-byte record.

6. Primary metadata
   The parser reads the serialized Inno 6.4.0.1 TSetupHeader and walks sections
   deterministically through TSetupFileEntry. It does NOT search for ``{app}``
   strings or filenames. One compiler-generated ftUninstExe record may have
   empty SourceFilename/DestName; ordinary ftUserFile records may not.

7. Secondary metadata
   The secondary stream is parsed directly as 87-byte
   TSetupFileLocationEntry records. Sign must be a known enum value and Flags
   must not contain unknown bits. StartOffset values are cross-checked against
   real ``b"zlb\\x1a"`` markers. There is no decoy/recovery fallback in the
   production path: a changed table fails closed.

8. Payload integrity
   Data can be stored, LZMA1, LZMA2, zlib or bzip2 as supported by Python's
   standard library. When foCallInstructionOptimized is set, the Inno CALL/JMP
   transform is reversed before verification. Every non-empty extracted file
   must match the SHA-256 stored in its location record.

9. Output safety / transactionality
   Only ``{app}`` paths are extracted. Parent traversal, absolute/drive syntax
   and resolved paths escaping staging are rejected. Extraction occurs in a
   temporary sibling directory. Existing output is renamed aside only during
   commit; if final commit fails, the previous tree is restored.

10. Final executable validation
    At least one Cheat Engine executable must exist. Every detected Cheat Engine
    executable is checked for DOS MZ and NT PE\\0\\0 signatures before commit.

Adapting to a future installer
------------------------------
Use ``--allow-other-hash --verbose`` and investigate the FIRST failing stage.
Check in order: loader layout/CRC; SetupID and offsets; metadata boundaries;
StoredSize transformation; CRC framing/compression; XOR formula/reset points;
serialized Inno record layouts; location record size/enums/flags; data chunk
format/compression; CALL/JMP transform.

Do not weaken CRC checks, path confinement, transactional output, PE validation
or per-file SHA-256 while investigating a new format.
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import lzma
import shutil
import secrets
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


VERBOSE = False

def emit(*args, **kwargs) -> None:
    """Print, hiding [i] diagnostics unless --verbose is enabled."""
    if (not VERBOSE and args and isinstance(args[0], str) and args[0].startswith("[i]")):
        return
    print(*args, **kwargs)



EXPECTED_NAME = "CheatEngine77.exe"
EXPECTED_SIZE = 34_690_856
EXPECTED_SHA256 = (
    "cf0f4b6002555677984233c95856683959be83a5def3dc12175a8296eb22676b"
)

LOADER_MAGIC = b"rDlPtS\xcd\xe6\xd7{\x0b*"
SETUP_ID = b"Inno Setup Setup Data (6.4.0.1)"
SETUP_ID_SIZE = 64
DATA_MAGIC = b"zlb\x1a"

SUBBLOCK_SIZE = 4096
DATA_ENTRY_SIZE_6401 = 87
FILE_ENTRY_STRING_COUNT = 10
FILE_ENTRY_FIXED_TAIL = 43

# Exact serialized fixed tails for Inno 6.4.0.1 packed records.
#
# SECompressedBlockWrite serializes all String / AnsiString fields first as:
#     Int32 byte_length + raw bytes
# and then writes the non-string portion of the packed record verbatim.
#
# TSetupHeader:
#   16 counters                      64
#   2 x TSetupVersionData            20
#   WizardStyle                       1
#   WizardSizePercentX/Y              8
#   WizardImageAlphaFormat            1
#   PasswordTest                      4
#   EncryptionKDFSalt                16
#   EncryptionKDFIterations           4
#   EncryptionBaseNonce              24
#   ExtraDiskSpaceRequired            8
#   SlicesPerDisk                     4
#   8 small enum/set fields           8
#   UninstallDisplaySize              8
#   TSetupHeader.Options (44 bits)    6
#                                      ---
#                                      177
SETUP_HEADER_UNICODE_STRINGS = 32
SETUP_HEADER_ANSI_STRINGS = 4
SETUP_HEADER_FIXED_TAIL = 177
SETUP_HEADER_COUNTERS = 16

# Entry layouts before seFile in TEntryType order:
#   strings, ansi_strings, fixed_tail
PRE_FILE_ENTRY_LAYOUTS = (
    ("language",       6, 4, 21),
    ("custom-message", 2, 0, 4),
    ("permission",     0, 1, 0),
    ("type",           4, 0, 30),
    ("component",      5, 0, 42),
    ("task",           6, 0, 26),
    ("dir",            7, 0, 27),
)

SETUP_COUNTER_NAMES = (
    "language",
    "custom_message",
    "permission",
    "type",
    "component",
    "task",
    "dir",
    "file",
    "file_location",
    "icon",
    "ini",
    "registry",
    "install_delete",
    "uninstall_delete",
    "run",
    "uninstall_run",
)

# TSetupFileEntry.Options bit index.
FILE_OPTION_DONT_COPY = 19

# TSetupFileLocationEntry.Flags bit indices.
DATA_FLAG_CALL_OPTIMIZED = 4
DATA_FLAG_ENCRYPTED = 6
DATA_FLAG_COMPRESSED = 7
DATA_FLAGS_KNOWN_MASK = (1 << 9) - 1  # nine Inno 6.4 location flags


@dataclass(frozen=True)
class LoaderOffsets:
    table_offset: int
    total_size: int
    exe_offset: int
    exe_uncompressed_size: int
    exe_crc32: int
    header_offset: int
    data_offset: int
    table_crc32: int


@dataclass(frozen=True)
class FileEntry:
    source: str
    destination: str
    location: int
    options: int
    file_type: int
    start: int
    end: int


@dataclass(frozen=True)
class DataEntry:
    first_slice: int
    last_slice: int
    chunk_offset: int
    file_offset: int
    file_size: int
    chunk_size: int
    sha256: bytes
    flags: int
    sign: int

    @property
    def compressed(self) -> bool:
        return bool(self.flags & (1 << DATA_FLAG_COMPRESSED))

    @property
    def encrypted(self) -> bool:
        return bool(self.flags & (1 << DATA_FLAG_ENCRYPTED))

    @property
    def call_optimized(self) -> bool:
        return bool(self.flags & (1 << DATA_FLAG_CALL_OPTIMIZED))


def u16(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<H", buf, off)[0]


def u32(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def i32(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<i", buf, off)[0]


def u64(buf: bytes | bytearray, off: int) -> int:
    return struct.unpack_from("<Q", buf, off)[0]


def crc32(buf: bytes) -> int:
    return zlib.crc32(buf) & 0xFFFFFFFF


def find_all(buf: bytes, needle: bytes) -> list[int]:
    positions: list[int] = []
    pos = 0
    while True:
        pos = buf.find(needle, pos)
        if pos < 0:
            return positions
        positions.append(pos)
        pos += 1


def find_default_installer(script_dir: Path) -> Path:
    exact = script_dir / EXPECTED_NAME
    if exact.is_file():
        return exact

    matches = [
        p for p in script_dir.iterdir()
        if p.is_file() and p.name.lower() == EXPECTED_NAME.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError(
            "Multiple case-insensitive CheatEngine77.exe matches found: "
            + ", ".join(sorted(p.name for p in matches))
        )
    raise FileNotFoundError(
        f"{EXPECTED_NAME} was not found next to the script: {script_dir}"
    )


def parse_loader_rev1(exe: bytes) -> LoaderOffsets:
    """
    Parse the exact loader-table layout used by this CE 7.7 artifact.

    From the observed installer and Inno Setup 6.4 loader layout:
      +00 ID[12]
      +12 revision u32 == 1
      +16 TotalSize u32
      +20 OffsetEXE u32
      +24 UncompressedSizeEXE u32
      +28 CRCEXE u32
      +32 Offset0 u32  -> setup headers
      +36 Offset1 u32  -> embedded file data
      +40 table CRC32 u32
    """
    positions = find_all(exe, LOADER_MAGIC)
    if len(positions) != 1:
        raise RuntimeError(
            f"Expected exactly one Inno loader table, found {len(positions)}"
        )

    pos = positions[0]
    if pos + 44 > len(exe):
        raise RuntimeError("Truncated Inno loader table")

    revision = u32(exe, pos + 12)
    if revision != 1:
        raise RuntimeError(
            f"Expected Inno loader revision 1 for this artifact, got {revision}"
        )

    offsets = LoaderOffsets(
        table_offset=pos,
        total_size=u32(exe, pos + 16),
        exe_offset=u32(exe, pos + 20),
        exe_uncompressed_size=u32(exe, pos + 24),
        exe_crc32=u32(exe, pos + 28),
        header_offset=u32(exe, pos + 32),
        data_offset=u32(exe, pos + 36),
        table_crc32=u32(exe, pos + 40),
    )

    if not (0 < offsets.header_offset < len(exe)):
        raise RuntimeError(f"Invalid setup header offset: {offsets.header_offset}")
    if not (0 < offsets.data_offset < len(exe)):
        raise RuntimeError(f"Invalid setup data offset: {offsets.data_offset}")
    if exe[offsets.header_offset:offsets.header_offset + len(SETUP_ID)] != SETUP_ID:
        got = exe[
            offsets.header_offset:
            offsets.header_offset + len(SETUP_ID)
        ].split(b"\x00", 1)[0]
        raise RuntimeError(
            "Loader Offset0 does not point to expected Inno 6.4.0.1 setup data; "
            f"found {got!r}"
        )
    if exe[offsets.data_offset:offsets.data_offset + 4] != DATA_MAGIC:
        raise RuntimeError(
            "Loader Offset1 does not point to embedded zlb data: "
            f"{offsets.data_offset}"
        )

    # Revision-1 Inno validates CRC32 over the complete table except the
    # trailing TableCRC field itself (first 40 bytes).
    actual_table_crc = crc32(exe[pos:pos + 40])

    emit(f"[i] Loader table:      {offsets.table_offset}")
    emit(f"[i] Loader revision:   {revision}")
    emit(f"[i] Header Offset0:    {offsets.header_offset}")
    emit(f"[i] Setup EXE offset:  {offsets.exe_offset}")
    emit(f"[i] Data Offset1:      {offsets.data_offset}")
    if actual_table_crc != offsets.table_crc32:
        raise RuntimeError(
            "Loader table CRC mismatch: "
            f"stored=0x{offsets.table_crc32:08x} "
            f"actual=0x{actual_table_crc:08x}"
        )
    emit("[i] Loader table CRC:  OK")

    return offsets


def decode_lzma1(raw: bytes) -> bytes:
    if len(raw) < 5:
        raise RuntimeError("Truncated LZMA1 stream")
    prop = raw[0]
    if prop > 9 * 5 * 5:
        raise RuntimeError("Invalid LZMA1 property byte")
    pb = prop // (9 * 5)
    rem = prop % (9 * 5)
    lp = rem // 9
    lc = rem % 9
    dict_size = u32(raw, 1)
    if dict_size == 0:
        dict_size = 1
    return lzma.decompress(
        raw[5:],
        format=lzma.FORMAT_RAW,
        filters=[{
            "id": lzma.FILTER_LZMA1,
            "dict_size": dict_size,
            "lc": lc,
            "lp": lp,
            "pb": pb,
        }],
    )


def decode_lzma2(raw: bytes) -> bytes:
    if not raw:
        raise RuntimeError("Truncated LZMA2 stream")
    prop = raw[0]
    if prop > 40:
        raise RuntimeError("Invalid LZMA2 property byte")
    if prop == 40:
        dict_size = 0xFFFFFFFF
    else:
        dict_size = (2 | (prop & 1)) << (prop // 2 + 11)
    return lzma.decompress(
        raw[1:],
        format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA2, "dict_size": dict_size}],
    )


def _block_header_fields(exe: bytes, offset: int) -> tuple[int, int, int]:
    if offset < 0 or offset + 9 > len(exe):
        raise ValueError("compressed-block header outside file")
    expected_crc = u32(exe, offset)
    stored_size = u32(exe, offset + 4)
    compressed = exe[offset + 8]
    return expected_crc, stored_size, compressed


def _valid_block_header(exe: bytes, offset: int) -> bool:
    """Check only the self-contained 9-byte Inno compressed-block header."""
    if offset < 0 or offset + 9 > len(exe):
        return False
    expected_crc, _stored_size, compressed = _block_header_fields(exe, offset)
    if compressed not in (0, 1):
        return False
    return crc32(exe[offset + 4:offset + 9]) == expected_crc


def _read_physical_framed_payload(
    exe: bytes,
    block_start: int,
    block_end: int,
) -> tuple[bytes, int, int]:
    """
    Read one CE-modified Inno block using its PHYSICAL end boundary.

    CE 7.7 keeps the standard 9-byte block header and standard framed chunks,
    but the StoredSize field is not a trustworthy physical boundary for the
    metadata blocks.  The framing itself remains standard:

        u32 chunk_crc
        up to 4096 bytes of chunk payload

    All non-final chunks are exactly 4096 payload bytes.  The final chunk is
    whatever remains before block_end after its 4-byte CRC.

    Returns: (concatenated compressed payload, chunk_count, stored_size_field)
    """
    if block_end <= block_start + 9:
        raise RuntimeError(
            f"Invalid physical Inno block range: {block_start}..{block_end}"
        )
    if block_end > len(exe):
        raise RuntimeError(f"Physical block end beyond EOF: {block_end}")

    expected_hdr_crc, stored_size_field, compressed = _block_header_fields(
        exe, block_start
    )
    actual_hdr_crc = crc32(exe[block_start + 4:block_start + 9])
    if expected_hdr_crc != actual_hdr_crc:
        raise RuntimeError(
            f"Header CRC mismatch at {block_start}: "
            f"stored=0x{expected_hdr_crc:08x} actual=0x{actual_hdr_crc:08x}"
        )
    if compressed not in (0, 1):
        raise RuntimeError(
            f"Invalid compressed flag {compressed} at {block_start}"
        )

    pos = block_start + 9
    framed_bytes = block_end - pos
    if framed_bytes < 5:
        raise RuntimeError(
            f"Physical block too short after header: {framed_bytes} bytes"
        )

    payload = bytearray()
    chunk_index = 0

    while pos < block_end:
        remaining = block_end - pos
        if remaining < 5:
            raise RuntimeError(
                f"Trailing {remaining} bytes cannot form CRC + payload "
                f"in block {block_start}..{block_end}"
            )

        expected = u32(exe, pos)
        pos += 4
        remaining_after_crc = block_end - pos

        # Every chunk except the last carries 4096 payload bytes.  If fewer
        # than 4096 remain, that is the final partial chunk.
        take = min(SUBBLOCK_SIZE, remaining_after_crc)
        piece = exe[pos:pos + take]
        actual = crc32(piece)

        if actual != expected:
            raise RuntimeError(
                f"Chunk CRC mismatch in physical block {block_start}..{block_end}: "
                f"chunk={chunk_index} crc_offset={pos - 4} "
                f"payload_len={take} stored=0x{expected:08x} "
                f"actual=0x{actual:08x}"
            )

        payload += piece
        pos += take
        chunk_index += 1

    return bytes(payload), chunk_index, stored_size_field


def _physical_block_is_valid(
    exe: bytes,
    block_start: int,
    block_end: int,
) -> bool:
    try:
        _read_physical_framed_payload(exe, block_start, block_end)
        return True
    except Exception:
        return False


def find_metadata_block_boundary(
    exe: bytes,
    first_block_start: int,
    metadata_end: int,
) -> int:
    """
    Find the boundary between the two WriteSetup0 metadata blocks.

    Facts used:
      * first block starts immediately after the 64-byte SetupID;
      * OffsetEXE is the physical end of WriteSetup0 metadata;
      * WriteSetup0 contains two consecutive compressed blocks;
      * both block headers and every framed chunk retain standard CRC32s.

    A split is accepted only if BOTH complete physical blocks validate.
    """
    if not (0 <= first_block_start < metadata_end <= len(exe)):
        raise RuntimeError(
            f"Invalid metadata range {first_block_start}..{metadata_end}"
        )
    if not _valid_block_header(exe, first_block_start):
        raise RuntimeError(
            f"Primary metadata block header is invalid at {first_block_start}"
        )

    candidates: list[int] = []

    # A second block must leave room for its 9-byte header plus at least one
    # CRC byte-frame (4-byte CRC + >=1 byte payload).
    scan_start = first_block_start + 9 + 5
    scan_end = metadata_end - 9 - 5

    for candidate in range(scan_start, scan_end + 1):
        if not _valid_block_header(exe, candidate):
            continue

        # Cheap structural check first: candidate -> OffsetEXE must form a
        # complete framed block. Only then validate the first block too.
        if not _physical_block_is_valid(exe, candidate, metadata_end):
            continue
        if not _physical_block_is_valid(exe, first_block_start, candidate):
            continue

        candidates.append(candidate)

    if not candidates:
        raise RuntimeError(
            "Could not find a split where both CE metadata blocks pass "
            "header CRC and every framed-chunk CRC"
        )
    if len(candidates) != 1:
        raise RuntimeError(
            "Metadata block split is ambiguous; CRC-valid candidates: "
            + ", ".join(str(x) for x in candidates[:32])
        )

    return candidates[0]


def read_inno_block_physical(
    exe: bytes,
    block_start: int,
    block_end: int,
    label: str,
) -> bytes:
    """Read and decompress a CE metadata block with a known physical end."""
    framed_payload, chunks, stored_size_field = _read_physical_framed_payload(
        exe, block_start, block_end
    )
    _expected_crc, _stored_size, compressed = _block_header_fields(exe, block_start)

    physical_stored = block_end - (block_start + 9)
    emit(
        f"[i] {label}: start={block_start}, end={block_end}, "
        f"physical_framed={physical_stored:,}, chunks={chunks}, "
        f"StoredSize_field={stored_size_field:,}"
    )

    if stored_size_field != physical_stored:
        emit(
            f"[i] {label}: CE modified StoredSize differs by "
            f"{stored_size_field - physical_stored:+,} bytes; "
            "using CRC-proven physical boundary"
        )

    if compressed:
        try:
            decoded = decode_lzma1(framed_payload)
        except Exception as exc:
            raise RuntimeError(
                f"{label} LZMA1 decode failed after all physical chunk CRCs "
                f"passed: {exc}"
            ) from exc
    else:
        decoded = framed_payload

    emit(f"[i] {label}: decoded={len(decoded):,} bytes")
    return decoded


def ce_xor_unit(data: bytes) -> bytes:
    """
    Reverse one CE-obfuscated Inno Writer.Write unit.

    The CE 7.7 fork restarts the XOR key for every Write call:
        key[i] = (0xCE + 2*i) & 0xFF
    """
    return bytes(
        value ^ ((0xCE + 2 * i) & 0xFF)
        for i, value in enumerate(data)
    )



def _decode_write_into(out: bytearray, source: bytes, pos: int, size: int) -> int:
    if size < 0 or pos < 0 or pos + size > len(source):
        raise ValueError(
            f"encoded Write outside metadata: pos={pos} size={size}"
        )
    out[pos:pos + size] = ce_xor_unit(source[pos:pos + size])
    return pos + size


def _decode_serialized_string_into(
    source: bytes,
    out: bytearray,
    pos: int,
    *,
    unicode_string: bool,
    label: str,
) -> int:
    """
    Decode one SECompressedBlockWrite string field.

    Inno performs two Writer.Write calls:
      1) Int32 byte length
      2) string bytes, if non-empty

    CE restarts its XOR key for each of those calls.
    """
    if pos + 4 > len(source):
        raise RuntimeError(f"{label}: truncated encoded string length")

    decoded_len = ce_xor_unit(source[pos:pos + 4])
    size = struct.unpack_from("<I", decoded_len, 0)[0]
    out[pos:pos + 4] = decoded_len
    pos += 4

    limit = 16_000_000
    if size > limit:
        raise RuntimeError(
            f"{label}: implausible decoded string byte length {size}"
        )
    if unicode_string and (size % 2):
        raise RuntimeError(
            f"{label}: odd UTF-16 byte length {size}"
        )
    if pos + size > len(source):
        raise RuntimeError(
            f"{label}: decoded string extends outside metadata "
            f"(pos={pos}, size={size}, total={len(source)})"
        )

    if size:
        out[pos:pos + size] = ce_xor_unit(source[pos:pos + size])
    pos += size
    return pos


def decode_primary_metadata_writes(encoded: bytes) -> tuple[bytes, dict[str, int]]:
    """
    Decode the CE XOR layer according to exact Inno 6.4 serialization Write
    boundaries, through the entire FileEntry section.

    The returned buffer has identical byte offsets to `encoded`; bytes after
    the FileEntry section are left untouched because current extraction only
    needs SetupHeader + sections through seFile.
    """
    out = bytearray(encoded)
    pos = 0

    # TSetupHeader strings: each length and each non-empty body are separate
    # Writer.Write calls.
    for i in range(SETUP_HEADER_UNICODE_STRINGS):
        pos = _decode_serialized_string_into(
            encoded, out, pos,
            unicode_string=True,
            label=f"SetupHeader Unicode[{i}]",
        )
    for i in range(SETUP_HEADER_ANSI_STRINGS):
        pos = _decode_serialized_string_into(
            encoded, out, pos,
            unicode_string=False,
            label=f"SetupHeader ANSI[{i}]",
        )

    tail_offset = pos
    pos = _decode_write_into(
        out, encoded, pos, SETUP_HEADER_FIXED_TAIL
    )

    counts_raw = [
        struct.unpack_from("<i", out, tail_offset + i * 4)[0]
        for i in range(SETUP_HEADER_COUNTERS)
    ]
    counts = dict(zip(SETUP_COUNTER_NAMES, counts_raw))

    for name, count in counts.items():
        if count < 0 or count > 1_000_000:
            raise RuntimeError(
                f"CE per-Write XOR produced implausible "
                f"TSetupHeader count {name}={count}"
            )

    emit(
        "[i] CE per-Write XOR SetupHeader counts: "
        f"files={counts['file']}, "
        f"locations={counts['file_location']}, "
        f"languages={counts['language']}, "
        f"dirs={counts['dir']}"
    )

    # Decode every section preceding seFile using the same Write boundaries.
    section_keys = (
        "language",
        "custom_message",
        "permission",
        "type",
        "component",
        "task",
        "dir",
    )

    for (entry_label, ucount, acount, fixed_tail), count_key in zip(
        PRE_FILE_ENTRY_LAYOUTS, section_keys
    ):
        count = counts[count_key]
        section_start = pos

        for entry_index in range(count):
            for string_index in range(ucount):
                pos = _decode_serialized_string_into(
                    encoded, out, pos,
                    unicode_string=True,
                    label=(
                        f"{entry_label}[{entry_index}] "
                        f"Unicode[{string_index}]"
                    ),
                )
            for string_index in range(acount):
                pos = _decode_serialized_string_into(
                    encoded, out, pos,
                    unicode_string=False,
                    label=(
                        f"{entry_label}[{entry_index}] "
                        f"ANSI[{string_index}]"
                    ),
                )
            if fixed_tail:
                pos = _decode_write_into(
                    out, encoded, pos, fixed_tail
                )

        emit(
            f"[i] CE XOR decoded section {entry_label}: "
            f"count={count}, range={section_start}..{pos}"
        )

    # Decode TSetupFileEntry records.
    file_start = pos
    for entry_index in range(counts["file"]):
        for string_index in range(10):
            pos = _decode_serialized_string_into(
                encoded, out, pos,
                unicode_string=True,
                label=f"file[{entry_index}] Unicode[{string_index}]",
            )
        pos = _decode_write_into(
            out, encoded, pos, FILE_ENTRY_FIXED_TAIL
        )

    emit(
        f"[i] CE XOR decoded file section: "
        f"count={counts['file']}, range={file_start}..{pos}"
    )

    return bytes(out), counts


def decode_secondary_location_writes(encoded: bytes) -> bytes:
    """
    Decode the secondary metadata block.

    Compiler.WriteSetup0 writes each TSetupFileLocationEntry using one
    Writer.Write(record, 87), therefore CE's XOR key restarts for each record.
    """
    if len(encoded) % DATA_ENTRY_SIZE_6401:
        raise RuntimeError(
            "Secondary metadata length is not divisible by the "
            f"{DATA_ENTRY_SIZE_6401}-byte location record: {len(encoded)}"
        )

    out = bytearray(len(encoded))
    count = len(encoded) // DATA_ENTRY_SIZE_6401

    for index in range(count):
        start = index * DATA_ENTRY_SIZE_6401
        end = start + DATA_ENTRY_SIZE_6401
        out[start:end] = ce_xor_unit(encoded[start:end])

    if count:
        first_slice = u32(out, 0)
        last_slice = u32(out, 4)
        sign = out[86]
        emit(
            f"[i] CE per-record XOR secondary: records={count}, "
            f"first_slice={first_slice}, last_slice={last_slice}, "
            f"first_sign={sign}"
        )

    return bytes(out)


def validate_decoded_primary_metadata(buf: bytes) -> None:
    if len(buf) < 4:
        raise RuntimeError("Primary metadata too short after CE XOR decode")

    first_len = u32(buf, 0)
    if first_len == 0 or first_len > 4096 or first_len % 2:
        raise RuntimeError(
            "CE per-Write XOR did not produce a plausible first UTF-16 "
            f"string length: {first_len}"
        )

    if 4 + first_len > len(buf):
        raise RuntimeError(
            "First UTF-16 string exceeds decoded primary metadata"
        )

    try:
        buf[4:4 + first_len].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise RuntimeError(
            "CE per-Write XOR produced an invalid first UTF-16 string"
        ) from exc


def validate_decoded_secondary_metadata(
    buf: bytes,
    expected_records: int | None = None,
) -> None:
    if len(buf) % DATA_ENTRY_SIZE_6401 != 0:
        raise RuntimeError(
            "CE XOR-decoded secondary metadata length is not divisible by "
            f"{DATA_ENTRY_SIZE_6401}: {len(buf)}"
        )

    count = len(buf) // DATA_ENTRY_SIZE_6401
    if expected_records is not None and expected_records > 0 and count != expected_records:
        emit(
            "[i] Secondary record-count cross-check differs: "
            f"decoded={count}, expected={expected_records}"
        )

    if count:
        first_slice = u32(buf, 0)
        last_slice = u32(buf, 4)
        emit(
            f"[i] Secondary table after CE XOR: "
            f"records={count}, first_slice={first_slice}, "
            f"last_slice={last_slice}"
        )


def read_string(buf: bytes, pos: int) -> tuple[str, int]:
    if pos + 4 > len(buf):
        raise ValueError("string length outside header")

    size = u32(buf, pos)
    pos += 4

    # 6.4 uses Unicode String fields. Length is stored in bytes.
    if size > 2_000_000 or size % 2:
        raise ValueError(f"implausible Unicode string length {size}")
    if pos + size > len(buf):
        raise ValueError("string outside header")

    raw = buf[pos:pos + size]
    pos += size
    try:
        value = raw.decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid UTF-16LE string") from exc

    # Inno strings are not supposed to carry an embedded NUL here.
    if "\x00" in value:
        raise ValueError("embedded NUL in string")
    return value, pos


def read_ansi_string(buf: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(buf):
        raise ValueError("ANSI string length outside header")

    size = u32(buf, pos)
    pos += 4

    if size > 16_000_000:
        raise ValueError(f"implausible ANSI string length {size}")
    if pos + size > len(buf):
        raise ValueError("ANSI string outside header")

    value = buf[pos:pos + size]
    pos += size
    return value, pos


def skip_serialized_entry(
    buf: bytes,
    pos: int,
    unicode_strings: int,
    ansi_strings: int,
    fixed_tail: int,
    label: str,
) -> int:
    start = pos

    for _ in range(unicode_strings):
        _value, pos = read_string(buf, pos)

    for _ in range(ansi_strings):
        _value, pos = read_ansi_string(buf, pos)

    if pos + fixed_tail > len(buf):
        raise ValueError(
            f"truncated fixed tail while parsing {label} at {start}"
        )

    return pos + fixed_tail


@dataclass(frozen=True)
class SetupHeader6401:
    counts: dict[str, int]
    entries_offset: int
    raw_tail_offset: int


def parse_setup_header_6401(buf: bytes) -> SetupHeader6401:
    """
    Parse enough of the serialized Inno 6.4.0.1 TSetupHeader to recover
    all section counts and the exact offset of the first entry section.

    This follows SECompressedBlockWrite exactly and does not scan for text.
    """
    pos = 0

    unicode_values: list[str] = []
    for _ in range(SETUP_HEADER_UNICODE_STRINGS):
        value, pos = read_string(buf, pos)
        unicode_values.append(value)

    for _ in range(SETUP_HEADER_ANSI_STRINGS):
        _value, pos = read_ansi_string(buf, pos)

    tail = pos
    if tail + SETUP_HEADER_FIXED_TAIL > len(buf):
        raise RuntimeError(
            "Serialized TSetupHeader fixed tail is truncated: "
            f"tail={tail}, need={SETUP_HEADER_FIXED_TAIL}, "
            f"decoded={len(buf)}"
        )

    counts_raw = [
        i32(buf, tail + i * 4)
        for i in range(SETUP_HEADER_COUNTERS)
    ]

    counts = dict(zip(SETUP_COUNTER_NAMES, counts_raw))

    # These are compile-time list counts and must never be negative.
    for name, count in counts.items():
        if count < 0 or count > 1_000_000:
            raise RuntimeError(
                f"Implausible TSetupHeader count {name}={count}"
            )

    entries_offset = tail + SETUP_HEADER_FIXED_TAIL

    # Cheap semantic sanity checks. They are intentionally broad.
    if counts["file"] == 0:
        raise RuntimeError("TSetupHeader says NumFileEntries=0")
    if counts["file_location"] == 0:
        raise RuntimeError("TSetupHeader says NumFileLocationEntries=0")

    app_name = unicode_values[0] if unicode_values else ""
    app_version_name = unicode_values[1] if len(unicode_values) > 1 else ""

    emit(
        f"[i] SetupHeader: AppName={app_name!r}, "
        f"AppVerName={app_version_name!r}"
    )
    emit(
        "[i] SetupHeader counts: "
        f"files={counts['file']}, "
        f"locations={counts['file_location']}, "
        f"languages={counts['language']}, "
        f"dirs={counts['dir']}, "
        f"icons={counts['icon']}, "
        f"run={counts['run']}"
    )
    emit(
        f"[i] SetupHeader serialized tail: start={tail}, "
        f"entries_start={entries_offset}"
    )

    return SetupHeader6401(
        counts=counts,
        entries_offset=entries_offset,
        raw_tail_offset=tail,
    )


def locate_file_section_6401(
    buf: bytes,
    setup: SetupHeader6401,
) -> int:
    """
    Walk the exact entry sections preceding seFile and return the first
    TSetupFileEntry offset.
    """
    pos = setup.entries_offset

    section_count_keys = (
        "language",
        "custom_message",
        "permission",
        "type",
        "component",
        "task",
        "dir",
    )

    for (label, unicode_count, ansi_count, fixed_tail), count_key in zip(
        PRE_FILE_ENTRY_LAYOUTS,
        section_count_keys,
    ):
        count = setup.counts[count_key]
        section_start = pos

        for index in range(count):
            try:
                pos = skip_serialized_entry(
                    buf,
                    pos,
                    unicode_count,
                    ansi_count,
                    fixed_tail,
                    f"{label}[{index}]",
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not walk {label} section "
                    f"entry {index + 1}/{count} at offset {pos}: {exc}"
                ) from exc

        emit(
            f"[i] Section {label}: count={count}, "
            f"range={section_start}..{pos}"
        )

    return pos


def parse_file_table_exact_6401(
    buf: bytes,
    setup: SetupHeader6401,
) -> list[FileEntry]:
    """
    Parse exactly NumFileEntries records from the deterministic section offset.
    """
    pos = locate_file_section_6401(buf, setup)
    file_start = pos
    total = setup.counts["file"]
    location_count = setup.counts["file_location"]
    entries: list[FileEntry] = []

    for index in range(total):
        try:
            entry = parse_file_entry_at(buf, pos)
        except Exception as exc:
            raise RuntimeError(
                f"Could not parse TSetupFileEntry {index + 1}/{total} "
                f"at primary offset {pos}: {exc}"
            ) from exc

        if entry.location < -1 or entry.location >= location_count:
            raise RuntimeError(
                f"TSetupFileEntry {index} has invalid LocationEntry "
                f"{entry.location}; expected -1..{location_count - 1}"
            )

        entries.append(entry)
        pos = entry.end

    app_entries = [
        e for e in entries
        if e.destination.lower().startswith("{app}")
        and e.location >= 0
        and not (e.options & (1 << FILE_OPTION_DONT_COPY))
    ]

    if not app_entries:
        # Do not silently accept a wrong structural walk.
        samples = ", ".join(
            repr(e.destination) for e in entries[:8]
        )
        raise RuntimeError(
            "Exact file-table walk parsed records but found no extractable "
            f"{{app}} destinations. First destinations: {samples}"
        )

    internal_uninstaller_entries = sum(
        1 for e in entries if e.file_type == 1
    )
    unnamed_internal_entries = sum(
        1 for e in entries
        if e.file_type == 1 and not e.source and not e.destination
    )

    emit(
        f"[i] Exact file table: start={file_start}, end={pos}, "
        f"entries={len(entries)}"
    )
    emit(
        f"[i] Internal ftUninstExe entries: "
        f"{internal_uninstaller_entries} "
        f"(unnamed={unnamed_internal_entries})"
    )
    emit(f"[i] Extractable {{app}}: {len(app_entries)} entries")

    return entries


def parse_file_entry_at(buf: bytes, start: int) -> FileEntry:
    pos = start
    strings: list[str] = []

    for _ in range(FILE_ENTRY_STRING_COUNT):
        value, pos = read_string(buf, pos)
        strings.append(value)

    if pos + FILE_ENTRY_FIXED_TAIL > len(buf):
        raise ValueError("truncated file-entry tail")

    tail = pos

    # Two TSetupVersionData records = 10 + 10 bytes.
    location = i32(buf, tail + 20)
    options = u32(buf, tail + 38)
    file_type = buf[tail + 42]
    end = tail + FILE_ENTRY_FIXED_TAIL

    source, destination = strings[0], strings[1]

    if file_type not in (0, 1):
        raise ValueError(f"invalid file type {file_type}")

    # ftUninstExe is an internal compiler-generated entry.  It may carry no
    # SourceFilename/DestName at all; rejecting it would incorrectly fail on the
    # compiler-generated uninstall record in CE 7.7.
    #
    # Normal ftUserFile entries must still have at least one meaningful name.
    if file_type == 0 and not source and not destination:
        raise ValueError(
            "ordinary ftUserFile has empty source and destination"
        )

    if len(destination) > 32_768:
        raise ValueError("implausible destination length")

    return FileEntry(
        source=source,
        destination=destination,
        location=location,
        options=options,
        file_type=file_type,
        start=start,
        end=end,
    )




def discover_file_table(header: bytes, data_count: int) -> list[FileEntry]:
    """
    Deterministic Inno 6.4.0.1 file-table parser.

    data_count is retained as an external cross-check only; the authoritative
    location count comes from TSetupHeader.NumFileLocationEntries.
    """
    setup = parse_setup_header_6401(header)

    header_locations = setup.counts["file_location"]
    if data_count > 0 and header_locations != data_count:
        raise RuntimeError(
            "TSetupHeader.NumFileLocationEntries does not match the decoded "
            f"secondary table: header={header_locations}, secondary={data_count}"
        )

    return parse_file_table_exact_6401(header, setup)


def parse_data_entries(header2: bytes) -> list[DataEntry]:
    """Parse and strictly validate 87-byte Inno 6.4 location records."""
    if len(header2) % DATA_ENTRY_SIZE_6401 != 0:
        raise RuntimeError(
            "Secondary header size is not divisible by the CE/Inno "
            f"data-entry size ({DATA_ENTRY_SIZE_6401}): {len(header2)}"
        )

    entries: list[DataEntry] = []
    for off in range(0, len(header2), DATA_ENTRY_SIZE_6401):
        flags = u16(header2, off + 84)
        sign = header2[off + 86]
        index = len(entries)

        if flags & ~DATA_FLAGS_KNOWN_MASK:
            raise RuntimeError(
                f"Unknown FileLocationEntry flags in data entry {index}: "
                f"0x{flags:04x}"
            )
        if sign not in (0, 1, 2, 3):
            raise RuntimeError(
                f"Invalid TSetupFileLocationSign in data entry {index}: {sign}"
            )

        entry = DataEntry(
            first_slice=u32(header2, off + 0),
            last_slice=u32(header2, off + 4),
            chunk_offset=u32(header2, off + 8),
            file_offset=u64(header2, off + 12),
            file_size=u64(header2, off + 20),
            chunk_size=u64(header2, off + 28),
            sha256=header2[off + 36:off + 68],
            flags=flags,
            sign=sign,
        )

        if entry.first_slice > 1_000_000 or entry.last_slice > 1_000_000:
            raise RuntimeError(
                f"Implausible slice index in data entry {index}: "
                f"{entry.first_slice}..{entry.last_slice}"
            )
        if entry.last_slice < entry.first_slice:
            raise RuntimeError(f"LastSlice < FirstSlice in data entry {index}")
        if entry.file_size > (1 << 40):
            raise RuntimeError(
                f"Implausible file size in data entry {index}: {entry.file_size}"
            )
        if entry.chunk_size > (1 << 40):
            raise RuntimeError(
                f"Implausible chunk size in data entry {index}: {entry.chunk_size}"
            )

        entries.append(entry)

    if not entries:
        raise RuntimeError("No data entries found in secondary header")

    emit(f"[i] Parsed data table: {len(entries)} entries")
    return entries





def collect_data_markers(
    exe: bytes,
    data_base: int,
    data_end: int,
) -> list[int]:
    """Return relative offsets of every zlb marker in the embedded data region."""
    if not (0 <= data_base < data_end <= len(exe)):
        raise RuntimeError(
            f"Invalid embedded data range: {data_base}..{data_end}"
        )

    result: list[int] = []
    pos = data_base

    while True:
        pos = exe.find(DATA_MAGIC, pos, data_end)
        if pos < 0:
            break
        result.append(pos - data_base)
        pos += 4

    if not result or result[0] != 0:
        raise RuntimeError(
            "Embedded data region does not begin with the expected zlb marker"
        )

    return result





def validate_data_entries_against_markers(
    entries: list[DataEntry],
    marker_offsets: list[int],
) -> None:
    marker_set = set(marker_offsets)
    misses = [
        (idx, entry.chunk_offset)
        for idx, entry in enumerate(entries)
        if entry.chunk_offset not in marker_set
    ]
    if misses:
        sample = ", ".join(
            f"#{idx}:{off}" for idx, off in misses[:8]
        )
        raise RuntimeError(
            f"{len(misses)}/{len(entries)} recovered location entries do not "
            f"point to real zlb markers; examples: {sample}"
        )


def decode_chunk_payload(raw: bytes, required_size: int) -> tuple[bytes, str]:
    """
    Auto-detect the installer's configured compression method.

    Inno's chunk stream itself carries only compressor properties, while the
    chosen method normally lives in TSetupHeader. For this narrow extractor,
    trying the stdlib-supported Inno methods is simpler and safer than parsing
    the entire main header structure just to obtain CompressMethod.
    """
    attempts: list[tuple[str, callable]] = [
        ("lzma2", lambda: decode_lzma2(raw)),
        ("lzma1", lambda: decode_lzma1(raw)),
        ("zlib", lambda: zlib.decompress(raw)),
        ("bzip2", lambda: bz2.decompress(raw)),
    ]

    errors: list[str] = []

    for name, fn in attempts:
        try:
            decoded = fn()
            if len(decoded) < required_size:
                errors.append(
                    f"{name}: decoded {len(decoded)} < required {required_size}"
                )
                continue
            return decoded, name
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")

    raise RuntimeError(
        "Could not decompress Inno chunk with lzma2/lzma1/zlib/bzip2; "
        + "; ".join(errors)
    )


def undo_instruction_filter_5309(data: bytes) -> bytes:
    """
    Python port of innoextract's inno_exe_decoder_5200(true), used for
    Inno Setup >= 5.3.9 when foCallInstructionOptimized is set.
    """
    out = bytearray(data)
    size = len(out)
    i = 0
    offset = 0
    block_size = 0x10000

    while i < size:
        opcode = out[i]
        i += 1
        offset = (offset + 1) & 0xFFFFFFFF

        if opcode not in (0xE8, 0xE9):
            continue

        block_size_left = block_size - ((offset - 1) % block_size)
        if block_size_left < 5:
            continue
        if i + 4 > size:
            break

        b0, b1, b2, b3 = out[i:i + 4]
        offset = (offset + 4) & 0xFFFFFFFF

        if b3 in (0x00, 0xFF):
            addr = offset & 0xFFFFFF
            rel = (b0 | (b1 << 8) | (b2 << 16))
            rel = (rel - addr) & 0xFFFFFFFF

            out[i + 0] = rel & 0xFF
            out[i + 1] = (rel >> 8) & 0xFF
            out[i + 2] = (rel >> 16) & 0xFF

            if rel & 0x800000:
                out[i + 3] = (~b3) & 0xFF

        i += 4

    return bytes(out)


def safe_app_path(destination: str) -> Path | None:
    normalized = destination.replace("\\", "/")
    low = normalized.lower()

    if low == "{app}":
        return None
    if not low.startswith("{app}/"):
        return None

    relative = PurePosixPath(normalized[6:])
    if relative.is_absolute():
        raise RuntimeError(f"Unsafe absolute path inside {{app}}: {destination}")
    parts: list[str] = []

    for part in relative.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise RuntimeError(f"Unsafe '..' path in installer: {destination}")
        if ":" in part:
            raise RuntimeError(f"Unsafe ':' path in installer: {destination}")
        parts.append(part)

    return Path(*parts) if parts else None


def extract_files(
    exe: bytes,
    data_base: int,
    files: list[FileEntry],
    data_entries: list[DataEntry],
    out_dir: Path,
) -> tuple[int, int]:
    selected = [
        f for f in files
        if f.destination.lower().startswith("{app}")
        and f.location >= 0
        and not (f.options & (1 << FILE_OPTION_DONT_COPY))
    ]

    emit(f"[+] Extracting {len(selected)} {{app}} metadata entries.")

    by_chunk: dict[int, list[DataEntry]] = {}
    for f in selected:
        de = data_entries[f.location]
        by_chunk.setdefault(de.chunk_offset, []).append(de)

    chunk_cache: dict[int, bytes] = {}
    compression_seen: set[str] = set()

    out_root = out_dir.resolve()
    extracted = 0
    skipped_empty = 0

    for index, f in enumerate(selected, 1):
        rel = safe_app_path(f.destination)
        if rel is None:
            continue

        de = data_entries[f.location]

        if de.first_slice != de.last_slice:
            raise RuntimeError(
                f"Multi-slice entry is unsupported for embedded installer: "
                f"{f.destination}"
            )
        if de.encrypted:
            raise RuntimeError(
                f"Encrypted Inno chunk encountered: {f.destination}"
            )

        if de.file_size == 0:
            target = (out_root / rel).resolve()
            if target != out_root and out_root not in target.parents:
                raise RuntimeError(f"Unsafe output path: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"")
            skipped_empty += 1
            continue

        chunk = chunk_cache.get(de.chunk_offset)
        if chunk is None:
            chunk_pos = data_base + de.chunk_offset
            if chunk_pos + 4 > len(exe):
                raise RuntimeError(
                    f"Chunk offset outside installer: {de.chunk_offset}"
                )
            if exe[chunk_pos:chunk_pos + 4] != DATA_MAGIC:
                raise RuntimeError(
                    f"Missing zlb marker at chunk offset {de.chunk_offset}"
                )

            raw_start = chunk_pos + 4
            raw_end = raw_start + de.chunk_size
            if raw_end > len(exe):
                raise RuntimeError(
                    f"Compressed chunk extends beyond installer: {de.chunk_offset}"
                )
            raw = exe[raw_start:raw_end]

            chunk_group = by_chunk[de.chunk_offset]
            required = max(
                x.file_offset + x.file_size
                for x in chunk_group
            )

            if de.compressed:
                chunk, method = decode_chunk_payload(raw, required)
                compression_seen.add(method)
            else:
                chunk = raw
                if len(chunk) < required:
                    raise RuntimeError(
                        f"Stored chunk is {len(chunk)} bytes but requires {required}"
                    )
                compression_seen.add("stored")

            chunk_cache[de.chunk_offset] = chunk

        start = de.file_offset
        end = start + de.file_size
        if end > len(chunk):
            raise RuntimeError(
                f"File slice outside decompressed chunk for {f.destination}: "
                f"{start}:{end} > {len(chunk)}"
            )

        payload = bytes(chunk[start:end])
        if de.call_optimized:
            payload = undo_instruction_filter_5309(payload)

        actual_sha = hashlib.sha256(payload).digest()
        if actual_sha != de.sha256:
            raise RuntimeError(
                f"SHA256 mismatch after extraction: {f.destination} "
                f"(location {f.location}, call-filter={de.call_optimized})"
            )

        target = (out_root / rel).resolve()
        if target != out_root and out_root not in target.parents:
            raise RuntimeError(f"Unsafe output path: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        extracted += 1

        if index == 1 or index % 50 == 0 or index == len(selected):
            emit(f"[i] Extracted {index}/{len(selected)} metadata entries")

    emit(
        "[i] Compression used:  "
        + ", ".join(sorted(compression_seen))
    )
    return extracted, skipped_empty



def validate_pe_executable(path: Path) -> None:
    """Validate DOS MZ and NT PE signatures without executing the file."""
    size = path.stat().st_size
    if size < 64:
        raise RuntimeError(f"PE file is too small: {path.name}")
    with path.open("rb") as f:
        dos = f.read(64)
        if dos[:2] != b"MZ":
            raise RuntimeError(f"Missing MZ header: {path.name}")
        pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
        if pe_offset < 64 or pe_offset + 4 > size:
            raise RuntimeError(f"Invalid PE header offset in {path.name}: {pe_offset}")
        f.seek(pe_offset)
        if f.read(4) != b"PE\x00\x00":
            raise RuntimeError(f"Missing PE signature: {path.name}")


def validate_output_target(installer: Path, output: Path) -> Path:
    """Reject final paths that could destroy the installer or its directory."""
    installer = installer.resolve()
    output = output.expanduser().absolute()

    if output.is_symlink():
        raise RuntimeError(f"Refusing symlink output path: {output}")

    canonical = output.resolve(strict=False)
    if canonical == installer:
        raise RuntimeError("Output path must not be the installer file")
    if canonical == installer.parent:
        raise RuntimeError(
            "Refusing to replace the directory containing the installer"
        )
    if canonical.anchor and canonical == Path(canonical.anchor):
        raise RuntimeError(f"Refusing filesystem-root output path: {output}")
    if output.name in ("", ".", ".."):
        raise RuntimeError(f"Unsafe output directory: {output}")

    return output


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def commit_output(staging: Path, output: Path) -> None:
    """Atomically expose staging, restoring existing output if commit fails."""
    backup: Path | None = None
    if output.exists() or output.is_symlink():
        suffix = secrets.token_hex(6)
        backup = output.with_name(f".{output.name}.previous-{suffix}")
        output.replace(backup)
    try:
        staging.replace(output)
    except Exception:
        if backup is not None and not output.exists():
            backup.replace(output)
        raise
    if backup is not None:
        try:
            remove_path(backup)
        except Exception as exc:
            emit(f"[!] New output committed, but old backup could not be removed: {backup} ({exc})", file=sys.stderr)


def validate_output(out_dir: Path) -> None:
    candidates = sorted(
        p for p in out_dir.rglob("*.exe")
        if "cheatengine" in p.name.lower()
    )

    if not candidates:
        raise RuntimeError(
            "Extraction finished but no Cheat Engine executable was found"
        )

    for p in candidates:
        validate_pe_executable(p)

    emit("[+] Cheat Engine executable candidates:")
    for p in candidates[:20]:
        try:
            rel = p.relative_to(out_dir)
        except ValueError:
            rel = p
        emit(f"    {rel}")



def run(installer: Path, output: Path, allow_other_hash: bool) -> int:
    stage = "read"
    try:
        exe = installer.read_bytes()
        actual_size = len(exe)
        actual_hash = hashlib.sha256(exe).hexdigest()

        emit(f"[i] Input:   {installer}")
        emit(f"[i] Size:    {actual_size:,} bytes")
        emit(f"[i] SHA256:  {actual_hash}")

        if actual_size != EXPECTED_SIZE or actual_hash != EXPECTED_SHA256:
            message = (
                "This script is intentionally pinned to the known full CE 7.7 "
                "installer. Artifact size/SHA256 do not match."
            )
            if not allow_other_hash:
                raise RuntimeError(message)
            emit(f"[!] WARNING: {message}")
            emit("[!] Continuing only because --allow-other-hash was supplied.")
        else:
            emit("[+] Pinned Cheat Engine 7.7 installer verified.")

        stage = "loader"
        offsets = parse_loader_rev1(exe)

        stage = "metadata-layout"
        block1_at = offsets.header_offset + SETUP_ID_SIZE
        metadata_end = offsets.exe_offset

        if exe[offsets.header_offset:offsets.header_offset + SETUP_ID_SIZE].split(b"\x00", 1)[0] != SETUP_ID:
            raise RuntimeError("Unexpected SetupID contents")

        block2_at = find_metadata_block_boundary(
            exe,
            first_block_start=block1_at,
            metadata_end=metadata_end,
        )

        emit(f"[i] Metadata block #1: {block1_at}..{block2_at}")
        emit(f"[i] Metadata block #2: {block2_at}..{metadata_end}")
        emit(f"[i] Embedded Setup EXE block begins exactly at {metadata_end}")

        stage = "headers"
        header1_encoded = read_inno_block_physical(
            exe, block1_at, block2_at, "Primary metadata block"
        )
        header2_encoded = read_inno_block_physical(
            exe, block2_at, metadata_end, "Secondary metadata block"
        )

        emit(
            f"[i] Primary header:    {len(header1_encoded):,} "
            "LZMA-decoded bytes"
        )
        emit(
            f"[i] Secondary header:  {len(header2_encoded):,} "
            "LZMA-decoded bytes"
        )

        stage = "ce-metadata-xor"
        header1, xor_counts = decode_primary_metadata_writes(
            header1_encoded
        )
        header2 = decode_secondary_location_writes(
            header2_encoded
        )

        validate_decoded_primary_metadata(header1)
        validate_decoded_secondary_metadata(
            header2,
            expected_records=xor_counts.get("file_location"),
        )

        emit("[i] CE per-Write metadata XOR decoded and validated")

        stage = "data-layout"
        marker_offsets = collect_data_markers(
            exe,
            offsets.data_offset,
            offsets.header_offset,
        )
        emit(f"[i] zlb markers in data region: {len(marker_offsets)}")

        inferred_count = len(header2) // DATA_ENTRY_SIZE_6401

        stage = "file-table"
        files = discover_file_table(header1, inferred_count)

        used_locations = sorted({
            f.location for f in files if f.location >= 0
        })
        if not used_locations:
            raise RuntimeError("Primary file table uses no embedded locations")

        expected_count = max(used_locations) + 1
        emit(
            f"[i] Primary file table references location indices "
            f"0..{expected_count - 1} "
            f"({len(used_locations)} distinct used)"
        )

        stage = "location-table"
        data_entries = parse_data_entries(header2)
        validate_data_entries_against_markers(data_entries, marker_offsets)

        if expected_count > len(data_entries):
            raise RuntimeError(
                f"File table references location {expected_count - 1}, "
                f"but only {len(data_entries)} location records exist"
            )

        emit(
            "[i] CE XOR-decoded secondary FileLocationEntry table "
            "validated against real zlb markers."
        )
        emit(
            f"[+] Metadata validated: {len(files)} file entries, "
            f"{len(data_entries)} payload locations."
        )

        stage = "prepare-output"
        output = validate_output_target(installer, output)

        # Transactional extraction: do not leave a half-populated final tree.
        parent = output.parent
        parent.mkdir(parents=True, exist_ok=True)
        temp = Path(
            tempfile.mkdtemp(
                prefix=f".{output.name}.extracting-",
                dir=str(parent),
            )
        )

        try:
            stage = "extract"
            extracted, empty = extract_files(
                exe,
                offsets.data_offset,
                files,
                data_entries,
                temp,
            )

            stage = "validate-output"
            validate_output(temp)

            stage = "commit"
            commit_output(temp, output)

        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise

        emit()
        emit(f"[+] Extracted files: {extracted}")
        emit(f"[+] Output:          {output}")
        emit("[+] Every non-empty extracted file passed the installer SHA256.")
        emit("[+] Installer EXE was never executed.")
        return 0

    except Exception as exc:
        emit(f"[!] Extraction failed at stage '{stage}': {exc}", file=sys.stderr)
        return 1


def main() -> int:
    script_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description=(
            "Extract the known full Cheat Engine 7.7 Inno 6.4.0.1 installer "
            "without executing it and without third-party dependencies."
        )
    )
    parser.add_argument(
        "installer",
        nargs="?",
        type=Path,
        help=f"default: {EXPECTED_NAME} next to this script",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        help="default: directory named after the installer EXE",
    )
    parser.add_argument(
        "--allow-other-hash",
        action="store_true",
        help=(
            "diagnostic only: allow an artifact whose size/SHA256 differs "
            "from the pinned CE 7.7 installer"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show structural offsets, metadata layout and extraction progress",
    )
    args = parser.parse_args()

    global VERBOSE
    VERBOSE = args.verbose

    if args.installer is None:
        installer = find_default_installer(script_dir)
    else:
        installer = args.installer.expanduser()
        if not installer.is_absolute():
            installer = (Path.cwd() / installer).resolve()

    if not installer.is_file():
        parser.error(f"installer not found: {installer}")

    if args.output is None:
        output = installer.parent / installer.stem
    else:
        output = args.output.expanduser()
        if not output.is_absolute():
            output = (Path.cwd() / output).absolute()

    emit(f"[i] Script directory: {script_dir}")
    emit(f"[i] Installer:        {installer}")
    emit(f"[i] Output directory: {output}")

    return run(installer, output, args.allow_other_hash)


if __name__ == "__main__":
    raise SystemExit(main())
