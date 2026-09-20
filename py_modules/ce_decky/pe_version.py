"""Read a Windows executable's declared version out of its PE resources.

The managed installation knows its version because the release manifest pins it.
An imported one - a `.exe` a user pointed at, or an installation directory they
packed into a `.zip` - carries no such record, so the only honest source is the
version resource Cheat Engine's own build stamped into the executable. A game's
own executable is read the same way, because the version it declares is one of
the two facts a table's compatibility is judged against.

The file is untrusted input and is never read whole. Each structure is fetched as
its own bounded span - the headers, the section table, the resource directory,
the version block - so what bounds this reader is the size of the structure being
read rather than the size of the file it sits in. Every offset is bounds-checked
against the span actually read, the resource tree is walked to a bounded depth,
and a malformed or absent resource yields `None` rather than an exception: not
knowing the version is a normal outcome, not an import failure.
"""

from __future__ import annotations

from pathlib import Path
import struct
import unicodedata

from .atomic import read_regular_range

# The first span of the resource section, which is where its directory tree
# sits. Anything the walk reaches outside it - a deeper directory, a data entry
# parked at the far end of the section, the version block itself - is fetched as
# its own span, so this is a first read rather than a limit on what can be found.
# `scripts/pe_version_survey.py` over the 114 readable Windows executables on the
# development device: the directories reached 4920 bytes in at most, while the
# sections holding them run to 121.6 MiB. A bound on the whole section would have
# refused a real installer at that size, and this does not.
MAX_RESOURCE_TREE_BYTES = 256 * 1024
_HEADER_BYTES = 4096
# What one executable may cost. No file in that survey needed more than 4 spans;
# a resource tree that asks for more than this is malformed or hostile, and the
# walk over it stops rather than turning a bounded read into an unbounded one.
_MAX_SPANS = 64
_RT_VERSION = 16
_MAX_RESOURCE_ENTRIES = 512
_MAX_VERSION_BLOCK = 64 * 1024
_VS_FIXEDFILEINFO_SIGNATURE = 0xFEEF04BD


def read_pe_version(path: Path) -> str | None:
    """The executable's display version, or `None` when it declares none."""
    try:
        return _version_from_pe(_Ranges(path))
    except (OSError, ValueError, struct.error, IndexError, UnicodeDecodeError):
        return None


class _Window:
    """A span of one file, addressed by absolute file offsets."""

    __slots__ = ("_data", "_start")

    def __init__(self, data: bytes, start: int) -> None:
        self._data = data
        self._start = start

    def has(self, offset: int, length: int) -> bool:
        return length >= 0 and offset >= self._start and offset + length <= self._start + len(self._data)

    def read(self, offset: int, length: int) -> bytes:
        if not self.has(offset, length):
            raise ValueError("read outside the span that was fetched")
        at = offset - self._start
        return self._data[at:at + length]

    def u16(self, offset: int) -> int:
        return struct.unpack("<H", self.read(offset, 2))[0]

    def u32(self, offset: int) -> int:
        return struct.unpack("<I", self.read(offset, 4))[0]


class _Ranges:
    """Spans of one file, proven to come from one unchanged inode."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._identity: tuple[int, ...] | None = None
        self._spans = 0

    def window(self, offset: int, length: int) -> _Window:
        self._spans += 1
        if self._spans > _MAX_SPANS:
            raise ValueError("this executable asks for too many spans to be read")
        data, info = read_regular_range(self._path, offset=offset, length=length)
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            raise ValueError("file changed between reads")
        return _Window(data, offset)

    def covering(self, window: _Window, offset: int, length: int) -> _Window | None:
        """`window` if it already holds this span, else a span fetched for it.

        A refetch reads a whole header page rather than the exact bytes asked
        for, because the structures here sit next to each other and the next
        question is almost always about the bytes after this one.
        """
        if window.has(offset, length):
            return window
        fetched = self.window(offset, max(length, _HEADER_BYTES))
        return fetched if fetched.has(offset, length) else None


def _version_from_pe(ranges: _Ranges) -> str | None:
    head = ranges.window(0, _HEADER_BYTES)
    if not head.has(0, 64) or head.read(0, 2) != b"MZ":
        return None
    pe_offset = head.u32(0x3C)
    if pe_offset < 64:
        return None
    headers = ranges.covering(head, pe_offset, 24)
    if headers is None or headers.read(pe_offset, 4) != b"PE\x00\x00":
        return None
    sections = headers.u16(pe_offset + 6)
    optional_size = headers.u16(pe_offset + 20)
    optional = pe_offset + 24
    if sections == 0 or sections > 96 or optional_size <= 0:
        return None
    headers = ranges.covering(headers, optional, optional_size)
    if headers is None:
        return None
    magic = headers.u16(optional)
    if magic == 0x10B:
        directories_at = optional + 96
    elif magic == 0x20B:
        directories_at = optional + 112
    else:
        return None
    if not headers.has(directories_at - 4, 4):
        return None
    directory_count = headers.u32(directories_at - 4)
    if directory_count <= 2 or not headers.has(directories_at, directory_count * 8):
        return None
    resource_rva = headers.u32(directories_at + 2 * 8)
    resource_size = headers.u32(directories_at + 2 * 8 + 4)
    if resource_rva == 0 or resource_size == 0:
        return None

    section_table_at = optional + optional_size
    section_window = ranges.covering(headers, section_table_at, sections * 40)
    if section_window is None:
        return None
    table = _section_table(section_window, section_table_at, sections)
    base = _offset_for_rva(table, resource_rva)
    if base is None:
        return None
    resources = ranges.window(base, min(resource_size, MAX_RESOURCE_TREE_BYTES))
    if not resources.has(base, 16):
        return None

    entry = _find_version_entry(ranges, resources, base, base, depth=0)
    if entry is None:
        return None
    data_rva, data_size = entry
    if data_size == 0 or data_size > _MAX_VERSION_BLOCK:
        return None
    start = _offset_for_rva(table, data_rva)
    if start is None:
        return None
    block = ranges.covering(resources, start, data_size)
    if block is None:
        return None
    return _version_from_block(block.read(start, data_size))


def _section_table(window: _Window, offset: int, count: int) -> list[tuple[int, int, int, int]]:
    table: list[tuple[int, int, int, int]] = []
    for index in range(count):
        header = offset + index * 40
        if not window.has(header, 40):
            break
        virtual_size = window.u32(header + 8)
        virtual_address = window.u32(header + 12)
        raw_size = window.u32(header + 16)
        raw_pointer = window.u32(header + 20)
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


def _find_version_entry(ranges: _Ranges, window: _Window, root: int, node: int, depth: int) -> tuple[int, int] | None:
    """Walk the resource tree to the first RT_VERSION data entry.

    The tree is addressed by offsets from the section's own start, and nothing
    says they point into the span that was fetched for it: the data entries in a
    real installer here sit 796 KiB past the directories that name them. Each
    step asks for a span that holds what it is about to read.
    """
    if depth > 3:
        return None
    window = ranges.covering(window, node, 16)
    if window is None:
        return None
    total = window.u16(node + 12) + window.u16(node + 14)
    if total > _MAX_RESOURCE_ENTRIES:
        return None
    window = ranges.covering(window, node, 16 + total * 8)
    if window is None:
        return None
    for index in range(total):
        entry = node + 16 + index * 8
        name = window.u32(entry)
        offset = window.u32(entry + 4)
        # At the top level only the RT_VERSION type is of interest; below it
        # take the first entry, which is this resource's first name/language.
        if depth == 0 and (name & 0x80000000 or name != _RT_VERSION):
            continue
        if offset & 0x80000000:
            found = _find_version_entry(ranges, window, root, root + (offset & 0x7FFFFFFF), depth + 1)
            if found is not None:
                return found
            continue
        leaf = root + offset
        leaf_window = ranges.covering(window, leaf, 8)
        if leaf_window is None:
            return None
        return leaf_window.u32(leaf), leaf_window.u32(leaf + 4)
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
