"""Regressions for holding a rediscovered artifact to the reviewed publisher key.

OpenSSL owns the cryptography, so these cover what this module authors: finding
the signature inside the PE, hashing the file the way Authenticode does, reading
the digest out of the signed content, and refusing everything that is not the
pinned key over these exact bytes. Fixtures are synthetic - no signed
third-party binary belongs in Git.

The OpenSSL seam was exercised against the reviewed `CheatEngine77.exe` (sha1
digest, `Cheat Engine EZ` key) and the download-page helper (sha384 digest,
`Plooto Inc` key); both reproduced their embedded digests exactly.
"""
from __future__ import annotations

import asyncio
import hashlib
import struct
from pathlib import Path

import pytest

from ce_decky import authenticode
from ce_decky.authenticode import SignatureFacts, verify_publisher

DOS_SIZE = 0x40
OPTIONAL_SIZE = 96 + 16 * 8
SECTION_TABLE = DOS_SIZE + 4 + 20 + OPTIONAL_SIZE

SHA1_OID = bytes.fromhex("2b0e03021a")
SHA256_OID = bytes.fromhex("608648016503040201")
SHA384_OID = bytes.fromhex("608648016503040202")
PIN = "a" * 64


def _der(tag: int, body: bytes) -> bytes:
    if len(body) < 0x80:
        return bytes([tag, len(body)]) + body
    length = len(body).to_bytes((len(body).bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(length)]) + length + body


def _signed_content(oid: bytes, digest: bytes) -> bytes:
    algorithm = _der(0x30, _der(0x06, oid) + _der(0x05, b""))
    return _der(0x30, _der(0x30, b"") + _der(0x30, algorithm + _der(0x04, digest)))


def _win_certificate(payload: bytes) -> bytes:
    entry = struct.pack("<IHH", 8 + len(payload), 0x0200, 0x0002) + payload
    return entry + b"\x00" * (-len(entry) % 8)


def _pe(
    *,
    sections: tuple[bytes, ...] = (b"section-one", b"section-two"),
    appended: bytes = b"",
    certificate: bytes | None = None,
) -> bytearray:
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
    struct.pack_into("<I", data, optional + 64, 0xDEADBEEF)
    struct.pack_into("<I", data, optional + 92, 16)
    for index, (pointer, size) in enumerate(placements):
        entry = SECTION_TABLE + index * 40
        data[entry:entry + 8] = f"s{index}".encode("ascii").ljust(8, b"\x00")
        struct.pack_into("<II", data, entry + 16, size, pointer)

    data += body + appended
    if certificate is not None:
        blob = _win_certificate(certificate)
        struct.pack_into("<II", data, optional + 96 + 4 * 8, len(data), len(blob))
        data += blob
    return data


def _digest(data: bytes, algorithm: str = "sha256") -> str:
    return authenticode._authenticode_digest(
        bytes(data), authenticode._parse_pe(bytes(data)), algorithm
    )


def test_the_digest_covers_sections_and_everything_appended_after_them():
    # An Inno installer keeps its whole payload after the last section, so
    # appended bytes are exactly what a header-only hash would miss.
    base = _pe(appended=b"installer payload", certificate=b"signature")
    assert _digest(base) != _digest(_pe(appended=b"installer paylaod", certificate=b"signature"))
    assert _digest(base) != _digest(_pe(
        sections=(b"section-one", b"section-TWO"),
        appended=b"installer payload", certificate=b"signature",
    ))


@pytest.mark.parametrize("field", ["checksum", "security_directory", "certificate"])
def test_the_fields_signing_rewrites_are_excluded_from_the_digest(field: str):
    data = _pe(appended=b"payload", certificate=b"signature-bytes")
    layout = authenticode._parse_pe(bytes(data))
    before = _digest(data)
    if field == "checksum":
        struct.pack_into("<I", data, layout.checksum_offset, 0x12345678)
    elif field == "security_directory":
        struct.pack_into("<I", data, layout.security_directory_offset + 4, layout.certificate_size)
    else:
        data[layout.certificate_offset + 8] ^= 0xFF
    assert _digest(data) == before


def test_the_digest_reproduces_the_documented_region_order():
    data = bytes(_pe(appended=b"tail", certificate=b"signature"))
    layout = authenticode._parse_pe(data)
    expected = hashlib.sha256()
    expected.update(data[:layout.checksum_offset])
    expected.update(data[layout.checksum_offset + 4:layout.security_directory_offset])
    expected.update(data[layout.security_directory_offset + 8:layout.size_of_headers])
    expected.update(data[layout.size_of_headers:layout.certificate_offset])
    assert _digest(data) == expected.hexdigest()


def test_an_unsigned_artifact_is_refused():
    data = bytes(_pe())
    with pytest.raises(ValueError, match="no Authenticode signature"):
        authenticode._extract_signature(data, authenticode._parse_pe(data))


@pytest.mark.parametrize("damage, message", [
    ("revision", "unsupported Authenticode certificate format"),
    ("type", "unsupported Authenticode certificate format"),
    ("length", "certificate table is malformed"),
    ("truncated", "certificate table is truncated"),
])
def test_a_damaged_certificate_entry_is_refused(damage: str, message: str):
    data = bytearray(_pe(certificate=b"signature-payload"))
    layout = authenticode._parse_pe(bytes(data))
    start = layout.certificate_offset
    if damage == "revision":
        struct.pack_into("<H", data, start + 4, 0x0100)
    elif damage == "type":
        struct.pack_into("<H", data, start + 6, 0x0001)
    elif damage == "length":
        struct.pack_into("<I", data, start, 4)
    else:
        del data[start + 4:]
    with pytest.raises(ValueError, match=message):
        authenticode._extract_signature(bytes(data), layout)


@pytest.mark.parametrize("damage, message", [
    (b"not an executable", "not a Windows executable"),
    (b"MZ" + b"\x00" * 100, "not a Windows executable"),
])
def test_a_non_executable_input_is_refused(damage: bytes, message: str):
    with pytest.raises(ValueError, match=message):
        authenticode._parse_pe(damage)


def test_an_unsupported_header_format_is_refused():
    data = bytearray(_pe())
    struct.pack_into("<H", data, DOS_SIZE + 4 + 20, 0x0107)
    with pytest.raises(ValueError, match="unsupported Windows executable header"):
        authenticode._parse_pe(bytes(data))


def test_a_section_reaching_past_the_file_is_refused():
    data = bytearray(_pe())
    struct.pack_into("<I", data, SECTION_TABLE + 16, len(data) * 2)
    with pytest.raises(ValueError, match="extends past its end"):
        authenticode._parse_pe(bytes(data))


@pytest.mark.parametrize("oid, algorithm", [
    (SHA1_OID, "sha1"), (SHA256_OID, "sha256"), (SHA384_OID, "sha384"),
])
def test_the_signed_digest_is_read_out_of_the_content(oid: bytes, algorithm: str):
    digest = hashlib.new(algorithm, b"file").digest()
    assert authenticode._parse_indirect_data(_signed_content(oid, digest)) == (
        algorithm, digest.hex(),
    )


def test_an_unsupported_digest_algorithm_is_refused():
    # md5, which Authenticode allowed historically and this must not accept.
    content = _signed_content(bytes.fromhex("2a864886f70d0205"), b"\x00" * 16)
    with pytest.raises(ValueError, match="unsupported digest algorithm"):
        authenticode._parse_indirect_data(content)


def test_content_declaring_no_digest_is_refused():
    with pytest.raises(ValueError, match="declares no digest"):
        authenticode._parse_indirect_data(_der(0x30, _der(0x02, b"\x01")))


def _install_fake_openssl(monkeypatch, *, content: bytes, subject: bytes, spki: bytes):
    async def fake(*args: str, stdin: bytes | None = None) -> bytes:
        if args[0] == "smime":
            Path(args[args.index("-out") + 1]).write_bytes(content)
            Path(args[args.index("-signer") + 1]).write_bytes(b"-----BEGIN CERTIFICATE-----")
            return b""
        if args[0] == "x509" and "-subject" in args:
            return b"subject=" + subject
        if args[0] == "x509":
            return b"-----BEGIN PUBLIC KEY-----"
        return spki
    monkeypatch.setattr(authenticode, "_openssl", fake)


def _signed_artifact(tmp_path: Path) -> tuple[Path, bytes]:
    data = _pe(appended=b"installer payload", certificate=b"pkcs7")
    path = tmp_path / "CheatEngine.exe"
    path.write_bytes(data)
    digest = bytes.fromhex(_digest(data, "sha1"))
    return path, _signed_content(SHA1_OID, digest)


def test_a_signature_by_the_pinned_key_over_these_bytes_is_accepted(monkeypatch, tmp_path: Path):
    path, content = _signed_artifact(tmp_path)
    spki = b"public-key-bytes"
    _install_fake_openssl(
        monkeypatch, content=content,
        subject=b"CN=Cheat Engine EZ,O=Cheat Engine EZ,C=NL", spki=spki,
    )
    facts = asyncio.run(verify_publisher(path, hashlib.sha256(spki).hexdigest()))
    assert facts == SignatureFacts(
        subject="CN=Cheat Engine EZ,O=Cheat Engine EZ,C=NL",
        common_name="Cheat Engine EZ",
        key_sha256=hashlib.sha256(spki).hexdigest(),
        digest_algorithm="sha1",
    )


def test_a_signature_that_does_not_cover_these_bytes_is_refused(monkeypatch, tmp_path: Path):
    path, _content = _signed_artifact(tmp_path)
    spki = b"public-key-bytes"
    _install_fake_openssl(
        monkeypatch, content=_signed_content(SHA1_OID, b"\x00" * 20),
        subject=b"CN=Cheat Engine EZ", spki=spki,
    )
    with pytest.raises(ValueError, match="does not cover these bytes"):
        asyncio.run(verify_publisher(path, hashlib.sha256(spki).hexdigest()))


def test_a_signature_by_another_publisher_is_refused(monkeypatch, tmp_path: Path):
    path, content = _signed_artifact(tmp_path)
    _install_fake_openssl(
        monkeypatch, content=content, subject=b"CN=Plooto Inc", spki=b"someone-else",
    )
    with pytest.raises(ValueError, match="different publisher"):
        asyncio.run(verify_publisher(path, PIN))


def test_an_invalid_pin_is_refused_before_anything_is_read(tmp_path: Path):
    with pytest.raises(ValueError, match="pinned publisher key is invalid"):
        asyncio.run(verify_publisher(tmp_path / "absent.exe", "not-a-sha256"))


def test_a_missing_openssl_is_a_refusal_rather_than_a_pass(monkeypatch, tmp_path: Path):
    path, _content = _signed_artifact(tmp_path)
    monkeypatch.setattr(authenticode.shutil, "which", lambda _name: None)
    with pytest.raises(ValueError, match="openssl is unavailable"):
        asyncio.run(verify_publisher(path, PIN))


def test_a_missing_artifact_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="artifact could not be read"):
        asyncio.run(verify_publisher(tmp_path / "absent.exe", PIN))


def test_bytes_the_caller_already_holds_are_used_instead_of_a_second_read(
    monkeypatch, tmp_path: Path,
):
    # An installer is tens of megabytes; reading it twice doubles the peak.
    path, content = _signed_artifact(tmp_path)
    data = path.read_bytes()
    path.unlink()
    spki = b"public-key-bytes"
    _install_fake_openssl(monkeypatch, content=content, subject=b"CN=Cheat Engine EZ", spki=spki)

    facts = asyncio.run(verify_publisher(path, hashlib.sha256(spki).hexdigest(), data=data))
    assert facts.key_sha256 == hashlib.sha256(spki).hexdigest()
