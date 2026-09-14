"""Regressions for the Authenticode publisher check.

OpenSSL owns the cryptography here, so these cover what the tool itself
authors: locating the signature in the PE, hashing the file the way
Authenticode does, reading the digest out of `SpcIndirectDataContent`, and the
decision the pin drives. Fixtures are synthetic - no signed third-party binary
belongs in Git.

The end-to-end seam through OpenSSL was exercised against the reviewed
`CheatEngine77.exe` (sha1 digest, `Cheat Engine EZ` key) and the download-page
stub (sha384 digest, `Plooto Inc` key); both reproduced their embedded digests
exactly.
"""
from __future__ import annotations

import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

import pytest


def _load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "authenticode_verify.py"
    spec = importlib.util.spec_from_file_location("authenticode_verify", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()

DOS_SIZE = 0x40
OPTIONAL_SIZE = 96 + 16 * 8  # through all sixteen data directories
SECTION_TABLE = DOS_SIZE + 4 + 20 + OPTIONAL_SIZE


def _der(tag: int, body: bytes) -> bytes:
    if len(body) < 0x80:
        return bytes([tag, len(body)]) + body
    length = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(length)]) + length + body


def _digest_info(oid: bytes, digest: bytes) -> bytes:
    algorithm = _der(0x30, _der(0x06, oid) + _der(0x05, b""))
    return _der(0x30, algorithm + _der(0x04, digest))


SHA1_OID = bytes.fromhex("2b0e03021a")
SHA256_OID = bytes.fromhex("608648016503040201")
SHA384_OID = bytes.fromhex("608648016503040202")


def _win_certificate(payload: bytes) -> bytes:
    entry = struct.pack("<IHH", 8 + len(payload), 0x0200, 0x0002) + payload
    return entry + b"\x00" * (-len(entry) % 8)


def _pe(
    *,
    sections: tuple[bytes, ...] = (b"section-one", b"section-two"),
    appended: bytes = b"",
    certificate: bytes | None = None,
) -> bytearray:
    """A minimal but structurally honest PE32 with optional appended data."""
    section_count = len(sections)
    size_of_headers = SECTION_TABLE + section_count * 40

    body = bytearray()
    placements: list[tuple[int, int]] = []
    for payload in sections:
        placements.append((size_of_headers + len(body), len(payload)))
        body += payload

    data = bytearray(size_of_headers)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, DOS_SIZE)
    data[DOS_SIZE:DOS_SIZE + 4] = b"PE\x00\x00"
    coff = DOS_SIZE + 4
    struct.pack_into("<HHIIIHH", data, coff,
                     0x014C, section_count, 0, 0, 0, OPTIONAL_SIZE, 0x0102)
    optional = coff + 20
    struct.pack_into("<H", data, optional, 0x10B)
    struct.pack_into("<I", data, optional + 60, size_of_headers)
    struct.pack_into("<I", data, optional + 64, 0xDEADBEEF)  # CheckSum
    struct.pack_into("<I", data, optional + 92, 16)          # NumberOfRvaAndSizes
    for index, (pointer, size) in enumerate(placements):
        entry = SECTION_TABLE + index * 40
        data[entry:entry + 8] = f"s{index}".encode("ascii").ljust(8, b"\x00")
        struct.pack_into("<II", data, entry + 16, size, pointer)

    data += body + appended
    if certificate is not None:
        blob = _win_certificate(certificate)
        directories = optional + 96
        struct.pack_into("<II", data, directories + 4 * 8, len(data), len(blob))
        data += blob
    return data


def _digest(data: bytes, algorithm: str = "sha256") -> str:
    return tool.authenticode_digest(bytes(data), tool.parse_pe(bytes(data)), algorithm)


def test_the_digest_covers_sections_and_everything_appended_after_them():
    # An Inno installer keeps its whole payload after the last section, so
    # appended bytes are exactly what a naive header-only hash would miss.
    base = _pe(appended=b"installer payload", certificate=b"signature")
    changed = _pe(appended=b"installer paylaod", certificate=b"signature")
    assert _digest(base) != _digest(changed)

    other = _pe(sections=(b"section-one", b"section-TWO"),
                appended=b"installer payload", certificate=b"signature")
    assert _digest(base) != _digest(other)


@pytest.mark.parametrize("field", ["checksum", "security_directory", "certificate"])
def test_the_three_signing_fields_are_excluded_from_the_digest(field: str):
    data = _pe(appended=b"payload", certificate=b"signature-bytes")
    layout = tool.parse_pe(bytes(data))
    before = _digest(data)

    if field == "checksum":
        struct.pack_into("<I", data, layout.checksum_offset, 0x12345678)
    elif field == "security_directory":
        struct.pack_into("<I", data, layout.security_directory_offset + 4,
                         layout.certificate_size)
    else:
        data[layout.certificate_offset + 8] ^= 0xFF

    assert _digest(data) == before


def test_the_digest_reproduces_the_documented_region_order():
    data = bytes(_pe(appended=b"tail", certificate=b"signature"))
    layout = tool.parse_pe(data)
    expected = hashlib.sha256()
    expected.update(data[:layout.checksum_offset])
    expected.update(data[layout.checksum_offset + 4:layout.security_directory_offset])
    expected.update(data[layout.security_directory_offset + 8:layout.size_of_headers])
    expected.update(data[layout.size_of_headers:layout.certificate_offset])
    assert _digest(data) == expected.hexdigest()


@pytest.mark.parametrize("algorithm", ["sha1", "sha256", "sha384"])
def test_every_supported_digest_algorithm_is_computed(algorithm: str):
    data = _pe(certificate=b"signature")
    assert len(_digest(data, algorithm)) == hashlib.new(algorithm).digest_size * 2


def test_a_file_without_a_certificate_table_is_refused():
    data = bytes(_pe())
    with pytest.raises(tool.SignatureError, match="no Authenticode signature"):
        tool.extract_signature(data, tool.parse_pe(data))


@pytest.mark.parametrize("damage, message", [
    ("revision", "WIN_CERTIFICATE revision"),
    ("type", "certificate type"),
    ("length", "invalid WIN_CERTIFICATE length"),
    ("truncated", "past end of file"),
])
def test_a_damaged_certificate_entry_is_refused(damage: str, message: str):
    data = bytearray(_pe(certificate=b"signature-payload"))
    layout = tool.parse_pe(bytes(data))
    start = layout.certificate_offset
    if damage == "revision":
        struct.pack_into("<H", data, start + 4, 0x0100)
    elif damage == "type":
        struct.pack_into("<H", data, start + 6, 0x0001)
    elif damage == "length":
        struct.pack_into("<I", data, start, 4)
    else:
        del data[start + 4:]

    with pytest.raises(tool.SignatureError, match=message):
        tool.extract_signature(bytes(data), layout)


def test_the_pkcs7_blob_is_returned_without_its_win_certificate_header():
    data = bytes(_pe(certificate=b"signature-payload"))
    blob = tool.extract_signature(data, tool.parse_pe(data))
    assert blob == b"signature-payload"


@pytest.mark.parametrize("damage, message", [
    (b"not a pe file at all", "missing MZ"),
    (b"MZ" + b"\x00" * 100, "missing PE"),
])
def test_a_non_pe_input_is_refused(damage: bytes, message: str):
    with pytest.raises(tool.SignatureError, match=message):
        tool.parse_pe(damage)


def test_an_unsupported_optional_header_magic_is_refused():
    data = bytearray(_pe())
    struct.pack_into("<H", data, DOS_SIZE + 4 + 20, 0x0107)
    with pytest.raises(tool.SignatureError, match="optional header magic"):
        tool.parse_pe(bytes(data))


def test_a_section_reaching_past_the_file_is_refused():
    data = bytearray(_pe())
    struct.pack_into("<I", data, SECTION_TABLE + 16, len(data) * 2)
    with pytest.raises(tool.SignatureError, match="past end of file"):
        tool.parse_pe(bytes(data))


@pytest.mark.parametrize("oid, algorithm", [
    (SHA1_OID, "sha1"),
    (SHA256_OID, "sha256"),
    (SHA384_OID, "sha384"),
])
def test_the_signed_digest_is_read_out_of_the_indirect_data(oid, algorithm):
    digest = hashlib.new(algorithm, b"file").digest()
    content = _der(0x30, _der(0x30, b"") + _digest_info(oid, digest))
    assert tool.parse_indirect_data(content) == (algorithm, digest.hex())


def test_an_unsupported_digest_algorithm_is_named_rather_than_guessed():
    # md5, which Authenticode allowed historically and this tool must not.
    content = _der(0x30, _der(0x30, b"")
                   + _digest_info(bytes.fromhex("2a864886f70d0205"), b"\x00" * 16))
    with pytest.raises(tool.SignatureError, match="1.2.840.113549.2.5"):
        tool.parse_indirect_data(content)


def test_content_without_a_digest_info_is_refused():
    with pytest.raises(tool.SignatureError, match="no DigestInfo"):
        tool.parse_indirect_data(_der(0x30, _der(0x02, b"\x01")))


def test_an_unsupported_der_length_is_refused():
    with pytest.raises(tool.SignatureError, match="length encoding"):
        tool._der_read(bytes([0x30, 0x85, 1, 2, 3, 4, 5]), 0)


def _facts(**overrides) -> object:
    defaults = dict(
        digest_algorithm="sha1",
        embedded_digest="ab" * 20,
        computed_digest="ab" * 20,
        signer_subject="CN=Cheat Engine EZ,O=Cheat Engine EZ,C=NL",
        signer_common_name="Cheat Engine EZ",
        spki_sha256=tool.CHEAT_ENGINE_EZ_SPKI_SHA256,
    )
    defaults.update(overrides)
    return tool.SignatureFacts(**defaults)


@pytest.mark.parametrize("overrides, expected", [
    ({}, 0),
    ({"computed_digest": "cd" * 20}, 1),
    ({"spki_sha256": "00" * 32}, 1),
])
def test_verify_accepts_only_the_pinned_key_over_matching_bytes(
    monkeypatch, capsys, overrides, expected,
):
    monkeypatch.setattr(tool, "read_signature_facts", lambda _path: _facts(**overrides))
    assert tool.main(["verify", "installer.exe"]) == expected
    if expected:
        assert "refused" in capsys.readouterr().err


def test_a_different_pin_can_be_required_explicitly(monkeypatch):
    monkeypatch.setattr(
        tool, "read_signature_facts",
        lambda _path: _facts(spki_sha256="11" * 32, signer_common_name="Other"),
    )
    assert tool.main(["verify", "installer.exe", "--expect-spki", "11" * 32]) == 0
    assert tool.main(["verify", "installer.exe"]) == 1
