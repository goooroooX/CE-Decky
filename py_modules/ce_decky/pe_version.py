"""Read a Windows executable's declared version out of its PE resources.

The managed installation knows its version because the release manifest pins it.
An imported one - a `.exe` a user pointed at, or an installation directory they
packed into a `.zip` - carries no such record, so the only honest source is the
version resource Cheat Engine's own build stamped into the executable.

The whole file is untrusted input. Every offset is bounds-checked against the
data actually read, the resource tree is walked to a bounded depth, and a
malformed or absent resource yields `None` rather than an exception: not knowing
the version is a normal outcome, not an import failure.
"""

from __future__ import annotations

from pathlib import Path
import struct
import unicodedata

from .atomic import read_regular_bytes

MAX_PE_BYTES = 256 * 1024 * 1024
_RT_VERSION = 16
_MAX_RESOURCE_ENTRIES = 512
_MAX_VERSION_BLOCK = 64 * 1024
_VS_FIXEDFILEINFO_SIGNATURE = 0xFEEF04BD


def read_pe_version(path: Path) -> str | None:
    """The executable's display version, or `None` when it declares none."""
    try:
        data = read_regular_bytes(path, max_bytes=MAX_PE_BYTES)
    except (OSError, ValueError):
        return None
    if data is None:
        return None
    try:
        return _version_from_pe(data)
    except (struct.error, ValueError, IndexError, UnicodeDecodeError):
        return None


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _version_from_pe(data: bytes) -> str | None:
    if len(data) < 64 or data[:2] != b"MZ":
        return None
    pe_offset = _u32(data, 0x3C)
    if pe_offset < 64 or pe_offset + 24 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\x00\x00":
        return None
    sections = _u16(data, pe_offset + 6)
    optional_size = _u16(data, pe_offset + 20)
    optional = pe_offset + 24
    if sections == 0 or sections > 96 or optional + optional_size > len(data):
        return None
    magic = _u16(data, optional)
    if magic == 0x10B:
        directories_at = optional + 96
    elif magic == 0x20B:
        directories_at = optional + 112
    else:
        return None
    directory_count = _u32(data, directories_at - 4)
    if directory_count <= 2 or directories_at + directory_count * 8 > len(data):
        return None
    resource_rva = _u32(data, directories_at + 2 * 8)
    resource_size = _u32(data, directories_at + 2 * 8 + 4)
    if resource_rva == 0 or resource_size == 0:
        return None

    table = _section_table(data, optional + optional_size, sections)
    base = _offset_for_rva(table, resource_rva)
    if base is None or base + 16 > len(data):
        return None

    entry = _find_version_entry(data, base, base, depth=0)
    if entry is None:
        return None
    data_rva, data_size = entry
    if data_size == 0 or data_size > _MAX_VERSION_BLOCK:
        return None
    start = _offset_for_rva(table, data_rva)
    if start is None or start + data_size > len(data):
        return None
    return _version_from_block(data[start:start + data_size])


def _section_table(data: bytes, offset: int, count: int) -> list[tuple[int, int, int, int]]:
    table: list[tuple[int, int, int, int]] = []
    for index in range(count):
        header = offset + index * 40
        if header + 40 > len(data):
            break
        virtual_size = _u32(data, header + 8)
        virtual_address = _u32(data, header + 12)
        raw_size = _u32(data, header + 16)
        raw_pointer = _u32(data, header + 20)
        table.append((virtual_address, max(virtual_size, raw_size), raw_pointer, raw_size))
    return table


def _offset_for_rva(table: list[tuple[int, int, int, int]], rva: int) -> int | None:
    for virtual_address, span, raw_pointer, raw_size in table:
        if virtual_address <= rva < virtual_address + span:
            delta = rva - virtual_address
            if delta >= raw_size:
                return None
            return raw_pointer + delta
    return None


def _find_version_entry(data: bytes, root: int, node: int, depth: int) -> tuple[int, int] | None:
    """Walk the resource tree to the first RT_VERSION data entry."""
    if depth > 3 or node + 16 > len(data):
        return None
    named = _u16(data, node + 12)
    ids = _u16(data, node + 14)
    total = named + ids
    if total > _MAX_RESOURCE_ENTRIES:
        return None
    for index in range(total):
        entry = node + 16 + index * 8
        if entry + 8 > len(data):
            return None
        name = _u32(data, entry)
        offset = _u32(data, entry + 4)
        # At the top level only the RT_VERSION type is of interest; below it
        # take the first entry, which is this resource's first name/language.
        if depth == 0 and (name & 0x80000000 or name != _RT_VERSION):
            continue
        if offset & 0x80000000:
            found = _find_version_entry(data, root, root + (offset & 0x7FFFFFFF), depth + 1)
            if found is not None:
                return found
            continue
        leaf = root + offset
        if leaf + 8 > len(data):
            return None
        return _u32(data, leaf), _u32(data, leaf + 4)
    return None


def _version_from_block(block: bytes) -> str | None:
    """`ProductVersion` from the string table, else the fixed binary version."""
    product = _string_value(block, "ProductVersion")
    if product:
        return product
    file_version = _string_value(block, "FileVersion")
    if file_version:
        return file_version
    signature = block.find(struct.pack("<I", _VS_FIXEDFILEINFO_SIGNATURE))
    # Only the two file-version words are read, at +8 and +12; requiring the
    # whole 52-byte VS_FIXEDFILEINFO would reject a block that carries the one
    # fact wanted here.
    if signature < 0 or signature + 16 > len(block):
        return None
    most, least = struct.unpack_from("<II", block, signature + 8)
    parts = (most >> 16, most & 0xFFFF, least >> 16, least & 0xFFFF)
    while len(parts) > 2 and parts[-1] == 0:
        parts = parts[:-1]
    return ".".join(str(part) for part in parts)


def _string_value(block: bytes, key: str) -> str | None:
    marker = key.encode("utf-16-le") + b"\x00\x00"
    at = block.find(marker)
    if at < 0:
        return None
    cursor = at + len(marker)
    # The value is 32-bit aligned relative to the block, after any padding.
    while cursor + 2 <= len(block) and block[cursor:cursor + 2] == b"\x00\x00" and cursor % 4:
        cursor += 2
    end = cursor
    while end + 2 <= len(block) and block[end:end + 2] != b"\x00\x00":
        end += 2
    if end == cursor or end - cursor > 256:
        return None
    text = block[cursor:end].decode("utf-16-le", "replace").strip()
    if (
        not text
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text)
        or any(unicodedata.bidirectional(ch) in {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"} for ch in text)
    ):
        return None
    return text[:64]
