from __future__ import annotations

from pathlib import Path
import struct

import ce_decky.pe_version as pe_version
from ce_decky.pe_version import MAX_RESOURCE_TREE_BYTES, read_pe_version


def _version_block(product: str = "7.7") -> bytes:
    """A VS_VERSIONINFO block carrying VS_FIXEDFILEINFO and one string value."""
    fixed = struct.pack(
        "<13I",
        0xFEEF04BD, 0x00010000,
        (7 << 16) | 7, (0 << 16) | 10621,
        (7 << 16) | 7, (0 << 16) | 10621,
        0x3F, 0, 0x4, 0x1, 0, 0, 0,
    )
    value = "ProductVersion".encode("utf-16-le") + b"\x00\x00"
    while len(value) % 4:
        value += b"\x00"
    value += product.encode("utf-16-le") + b"\x00\x00"
    return "VS_VERSION_INFO".encode("utf-16-le") + b"\x00\x00" + fixed + value


def _pe_with_resource(
    tmp_path: Path,
    block: bytes,
    name: str = "sample.exe",
    resource_at: int = 0x400,
    leaf_at: int = 72,
) -> Path:
    """A minimal PE32+ whose resource directory points at one RT_VERSION leaf.

    `leaf_at` is where in the section the data entry sits, which real files do
    not keep next to the directories that name them.
    """
    section_rva = 0x1000
    # type directory -> name directory -> language directory -> data entry
    type_dir = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 16, 0x80000000 | 24)
    name_dir = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 1, 0x80000000 | 48)
    lang_dir = struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack("<II", 1033, leaf_at)
    data_offset = leaf_at + 16
    data_entry = struct.pack("<IIII", section_rva + data_offset, len(block), 0, 0)
    directories = type_dir + name_dir + lang_dir
    resource = directories + b"\x00" * (leaf_at - len(directories)) + data_entry + block

    headers = bytearray(0x400)
    headers[0:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x80)
    pe = 0x80
    headers[pe:pe + 4] = b"PE\x00\x00"
    struct.pack_into("<HHIIIHH", headers, pe + 4, 0x8664, 1, 0, 0, 0, 240, 0x22)
    optional = pe + 24
    struct.pack_into("<H", headers, optional, 0x20B)
    struct.pack_into("<I", headers, optional + 108, 16)
    struct.pack_into("<II", headers, optional + 112 + 2 * 8, section_rva, len(resource))
    section = optional + 240
    headers[section:section + 8] = b".rsrc\x00\x00\x00"
    struct.pack_into("<IIII", headers, section + 8, len(resource), section_rva, len(resource), resource_at)

    path = tmp_path / name
    # Written by seek rather than by padding, so a resource placed far into the
    # file costs a hole rather than the bytes in front of it.
    with path.open("wb") as handle:
        handle.write(bytes(headers))
        handle.seek(resource_at)
        handle.write(resource)
    return path


def test_reads_the_product_version_a_resource_declares(tmp_path: Path):
    assert read_pe_version(_pe_with_resource(tmp_path, _version_block("7.7"))) == "7.7"


def test_rejects_bidirectional_controls_in_display_version_text(tmp_path: Path):
    # The unsafe string is ignored; the fixed binary version remains a safe
    # fallback and cannot visually reorder the UI around it.
    assert read_pe_version(_pe_with_resource(tmp_path, _version_block("7.7\u202eexe"))) == "7.7.0.10621"


def test_falls_back_to_the_fixed_binary_version_with_no_string_table(tmp_path: Path):
    fixed_only = "VS_VERSION_INFO".encode("utf-16-le") + b"\x00\x00" + struct.pack(
        "<IIII", 0xFEEF04BD, 0x00010000, (7 << 16) | 7, (0 << 16) | 10621,
    )
    assert read_pe_version(_pe_with_resource(tmp_path, fixed_only)) == "7.7.0.10621"


def test_a_file_that_is_not_a_pe_declares_no_version(tmp_path: Path):
    path = tmp_path / "not-a-pe.exe"
    path.write_bytes(b"not an executable at all")
    assert read_pe_version(path) is None


def test_a_pe_without_a_resource_directory_declares_no_version(tmp_path: Path):
    source = _pe_with_resource(tmp_path, _version_block())
    data = bytearray(source.read_bytes())
    # Clear the resource data directory entry.
    struct.pack_into("<II", data, 0x80 + 24 + 112 + 2 * 8, 0, 0)
    stripped = tmp_path / "no-resources.exe"
    stripped.write_bytes(bytes(data))
    assert read_pe_version(stripped) is None


def test_a_truncated_file_declares_no_version_rather_than_raising(tmp_path: Path):
    source = _pe_with_resource(tmp_path, _version_block())
    truncated = tmp_path / "truncated.exe"
    truncated.write_bytes(source.read_bytes()[:0x200])
    assert read_pe_version(truncated) is None


def test_a_missing_file_declares_no_version(tmp_path: Path):
    assert read_pe_version(tmp_path / "absent.exe") is None


def test_a_huge_file_that_is_not_a_pe_declares_no_version(tmp_path: Path):
    path = tmp_path / "oversized.exe"
    with path.open("wb") as handle:
        handle.truncate(256 * 1024 * 1024 + 1)
    assert read_pe_version(path) is None


def test_a_resource_beyond_the_old_whole_file_bound_is_still_read(tmp_path: Path):
    # The executable this was found on is 426 MiB and declares its version 117 MiB
    # into the file. Reading the file to reach that made the file's size the
    # bound, and the version came back as None.
    path = _pe_with_resource(tmp_path, _version_block("7.7"), name="huge.exe", resource_at=300 * 1024 * 1024)
    assert path.stat().st_size > 256 * 1024 * 1024
    assert read_pe_version(path) == "7.7"


def test_a_resource_directory_outside_the_file_declares_no_version(tmp_path: Path):
    source = _pe_with_resource(tmp_path, _version_block())
    data = bytearray(source.read_bytes())
    # An RVA no section maps, and then a section whose bytes are past the end.
    struct.pack_into("<I", data, 0x80 + 24 + 112 + 2 * 8, 0x900000)
    unmapped = tmp_path / "unmapped-resources.exe"
    unmapped.write_bytes(bytes(data))
    assert read_pe_version(unmapped) is None

    data = bytearray(source.read_bytes())
    struct.pack_into("<I", data, 0x80 + 24 + 240 + 20, 0x4000000)
    detached = tmp_path / "detached-resources.exe"
    detached.write_bytes(bytes(data))
    assert read_pe_version(detached) is None


def test_a_data_entry_past_the_first_resource_span_is_still_read(tmp_path: Path):
    # A real installer in the survey corpus parks its data entries 796 KiB past
    # the directories that name them, so the span fetched for the tree is a first
    # read rather than a limit on what the walk may reach.
    path = _pe_with_resource(
        tmp_path,
        _version_block("7.7"),
        name="far-entry.exe",
        leaf_at=MAX_RESOURCE_TREE_BYTES + 4096,
    )
    assert read_pe_version(path) == "7.7"


def test_an_executable_asking_for_too_many_spans_declares_no_version(tmp_path: Path, monkeypatch):
    path = _pe_with_resource(
        tmp_path,
        _version_block("7.7"),
        name="budgeted.exe",
        leaf_at=MAX_RESOURCE_TREE_BYTES + 4096,
    )
    assert read_pe_version(path) == "7.7"
    monkeypatch.setattr(pe_version, "_MAX_SPANS", 2)
    assert read_pe_version(path) is None
