"""Check which publisher key signed a Windows executable.

The release manifest pins one artifact by SHA-256, and that hash is what
authorizes an install - the native extractor is pinned to the same artifact's
format, so nothing else could be extracted anyway. This answers a different
question, and only on the rediscovery path: when a rediscovered URL serves
something that is *not* the reviewed release, was it Cheat Engine's own
publisher that signed it? "Upstream released a version this build cannot read
yet" and "we were handed a different file" are both refusals, but they are not
the same thing to tell a user, and the publisher key is what separates them.

Only the Authenticode digest is computed here. The signature itself is verified
by `openssl smime`, which parses the PKCS#7 structure and checks the RSA
signature over the signed attributes. (`openssl cms` refuses these blobs;
Authenticode's `SpcIndirectDataContent` is not a content type CMS accepts.)
OpenSSL cannot compute the digest, because that is a Microsoft-specific hash
over the PE with the two fields signing rewrites - the optional header checksum
and the certificate table's data directory entry - plus the certificate table
itself excluded.

No certificate chain is built and no revocation is consulted. Those answer
whether some CA vouches for the file; the question here is whether this is the
exact key CE Decky already reviewed, which a pinned `SubjectPublicKeyInfo`
answers directly and without depending on a system trust store. Every failure,
including a missing `openssl`, is a refusal: the caller falls back to asking the
user for their own Cheat Engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import asyncio
import hashlib
import hmac
import re
import shutil
import struct
import tempfile

from .atomic import read_regular_bytes
from .child_env import child_environment

# WIN_CERTIFICATE, from winnt.h.
WIN_CERT_REVISION_2_0 = 0x0200
WIN_CERT_TYPE_PKCS_SIGNED_DATA = 0x0002

MAX_PE_BYTES = 256 * 1024 * 1024
MAX_CERTIFICATE_BYTES = 8 * 1024 * 1024
OPENSSL_TIMEOUT_SECONDS = 60
MAX_OPENSSL_ERROR_BYTES = 4096
MAX_SUBJECT_BYTES = 512

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMON_NAME = re.compile(r"(?:^|,\s*)CN\s*=\s*([^,]+)")

_DIGEST_OIDS = {
    "1.3.14.3.2.26": "sha1",
    "2.16.840.1.101.3.4.2.1": "sha256",
    "2.16.840.1.101.3.4.2.2": "sha384",
    "2.16.840.1.101.3.4.2.3": "sha512",
}


@dataclass(frozen=True)
class SignatureFacts:
    """What a verified signature says, in the form the UI and metadata keep."""

    subject: str
    common_name: str
    key_sha256: str
    digest_algorithm: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _PELayout:
    checksum_offset: int
    security_directory_offset: int
    size_of_headers: int
    sections: tuple[tuple[int, int], ...]
    certificate_offset: int
    certificate_size: int


def _parse_pe(data: bytes) -> _PELayout:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise ValueError("artifact is not a Windows executable")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\x00\x00":
        raise ValueError("artifact is not a Windows executable")

    section_count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    if optional + optional_size > len(data):
        raise ValueError("Windows executable header is truncated")
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x10B:
        directories = optional + 96
    elif magic == 0x20B:
        directories = optional + 112
    else:
        raise ValueError("unsupported Windows executable header format")
    if directories + 5 * 8 > len(data):
        raise ValueError("Windows executable has no certificate table entry")
    if struct.unpack_from("<I", data, directories - 4)[0] < 5:
        raise ValueError("Windows executable has no certificate table entry")

    security = directories + 4 * 8
    certificate_offset, certificate_size = struct.unpack_from("<II", data, security)
    size_of_headers = struct.unpack_from("<I", data, optional + 60)[0]
    if not 0 < size_of_headers <= len(data):
        raise ValueError("Windows executable header size is invalid")

    sections: list[tuple[int, int]] = []
    table = optional + optional_size
    for index in range(section_count):
        entry = table + index * 40
        if entry + 40 > len(data):
            raise ValueError("Windows executable section table is truncated")
        raw_size, raw_pointer = struct.unpack_from("<II", data, entry + 16)
        if raw_size:
            if raw_pointer + raw_size > len(data):
                raise ValueError("Windows executable section extends past its end")
            sections.append((raw_pointer, raw_size))
    sections.sort()

    return _PELayout(
        checksum_offset=optional + 64,
        security_directory_offset=security,
        size_of_headers=size_of_headers,
        sections=tuple(sections),
        certificate_offset=certificate_offset,
        certificate_size=certificate_size,
    )


def _authenticode_digest(data: bytes, layout: _PELayout, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    digest.update(data[:layout.checksum_offset])
    digest.update(data[layout.checksum_offset + 4:layout.security_directory_offset])
    digest.update(data[layout.security_directory_offset + 8:layout.size_of_headers])

    cursor = layout.size_of_headers
    for pointer, size in layout.sections:
        digest.update(data[pointer:pointer + size])
        cursor = pointer + size
    # An Inno installer keeps its whole payload after the last section, so the
    # appended region is hashed too; only the trailing certificate table, which
    # holds the signature being checked, stays out.
    end = layout.certificate_offset if layout.certificate_size else len(data)
    if end > cursor:
        digest.update(data[cursor:end])
    return digest.hexdigest()


def _extract_signature(data: bytes, layout: _PELayout) -> bytes:
    if not layout.certificate_size:
        raise ValueError("artifact carries no Authenticode signature")
    start, size = layout.certificate_offset, layout.certificate_size
    if start + size > len(data) or size < 8:
        raise ValueError("Authenticode certificate table is truncated")
    if size > MAX_CERTIFICATE_BYTES:
        raise ValueError("Authenticode certificate table is unreasonably large")
    length, revision, kind = struct.unpack_from("<IHH", data, start)
    if revision != WIN_CERT_REVISION_2_0 or kind != WIN_CERT_TYPE_PKCS_SIGNED_DATA:
        raise ValueError("unsupported Authenticode certificate format")
    if not 8 < length <= size:
        raise ValueError("Authenticode certificate table is malformed")
    return data[start + 8:start + length]


def _der_read(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    if pos + 2 > len(buf):
        raise ValueError("signed content is truncated")
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or pos + count > len(buf):
            raise ValueError("signed content uses an unsupported encoding")
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    if pos + length > len(buf):
        raise ValueError("signed content is truncated")
    return tag, buf[pos:pos + length], pos + length


def _der_oid(value: bytes) -> str:
    if not value:
        raise ValueError("signed content declares an empty algorithm")
    parts = [str(value[0] // 40), str(value[0] % 40)]
    current = 0
    for byte in value[1:]:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(current))
            current = 0
    return ".".join(parts)


def _elements(buf: bytes) -> list[bytes]:
    found: list[bytes] = []
    pos = 0
    while pos < len(buf):
        try:
            _tag, _value, next_pos = _der_read(buf, pos)
        except ValueError:
            break
        found.append(buf[pos:next_pos])
        pos = next_pos
    return found


def _parse_indirect_data(content: bytes) -> tuple[str, str]:
    """Read `(algorithm, digest)` from SpcIndirectDataContent's DigestInfo.

    The structure is `SEQUENCE { attribute, DigestInfo }`. OpenSSL hands the
    content back with the outer wrapper already consumed on some paths, so the
    DigestInfo is located rather than assumed to sit at a fixed position.
    """
    candidates = list(_elements(content))
    for element in list(candidates):
        tag, body, _ = _der_read(element, 0)
        if tag == 0x30:
            candidates.extend(_elements(body))
    for candidate in candidates:
        tag, body, _ = _der_read(candidate, 0)
        if tag != 0x30:
            continue
        try:
            algorithm_tag, algorithm, rest = _der_read(body, 0)
            digest_tag, digest, _ = _der_read(body, rest)
        except ValueError:
            continue
        if algorithm_tag != 0x30 or digest_tag != 0x04:
            continue
        oid_tag, oid_value, _ = _der_read(algorithm, 0)
        if oid_tag != 0x06:
            continue
        name = _DIGEST_OIDS.get(_der_oid(oid_value))
        if name is None:
            raise ValueError("artifact is signed with an unsupported digest algorithm")
        return name, digest.hex()
    raise ValueError("artifact's signed content declares no digest")


async def _openssl(*args: str, stdin: bytes | None = None) -> bytes:
    binary = shutil.which("openssl")
    if binary is None:
        raise ValueError("openssl is unavailable, so the signature cannot be checked")
    process = await asyncio.create_subprocess_exec(
        binary, *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=child_environment(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin), timeout=OPENSSL_TIMEOUT_SECONDS
        )
    except (TimeoutError, asyncio.CancelledError):
        # A signature check is read-only, so an abandoned child has nothing to
        # finish; stop the group rather than leaving it behind.
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        detail = stderr[:MAX_OPENSSL_ERROR_BYTES].decode("utf-8", "replace").strip()
        first = detail.splitlines()[0] if detail else "no output"
        raise ValueError(f"signature check failed: {first}")
    return stdout


async def verify_publisher(
    path: Path,
    expected_key_sha256: str,
    *,
    data: bytes | None = None,
) -> SignatureFacts:
    """Prove `path` is signed by the pinned publisher key, or raise.

    Returns the facts worth recording: the signer's distinguished name, the
    SHA-256 of its `SubjectPublicKeyInfo`, and which digest algorithm the
    signature covers the file with. A caller that already holds the artifact's
    bytes passes them in; an installer is tens of megabytes and reading it a
    second time doubles the peak for no gain.
    """
    if not isinstance(expected_key_sha256, str) or not _SHA256_RE.fullmatch(expected_key_sha256):
        raise ValueError("pinned publisher key is invalid")
    if data is None:
        try:
            data = read_regular_bytes(path, max_bytes=MAX_PE_BYTES)
        except (OSError, ValueError) as exc:
            raise ValueError(f"artifact could not be read: {exc}") from exc
    if data is None:
        raise ValueError("artifact could not be read")
    layout = _parse_pe(data)
    blob = _extract_signature(data, layout)

    with tempfile.TemporaryDirectory(prefix="ce-decky-authenticode-") as work:
        root = Path(work)
        signature = root / "signature.der"
        content = root / "content.der"
        signer = root / "signer.pem"
        signature.write_bytes(blob)
        # `-noverify` skips chain building only. The signature over the signed
        # attributes is still checked, and the pinned key replaces the chain.
        await _openssl(
            "smime", "-verify", "-noverify", "-inform", "DER",
            "-in", str(signature), "-out", str(content), "-signer", str(signer),
        )
        algorithm, embedded = _parse_indirect_data(content.read_bytes())
        raw_subject = await _openssl(
            "x509", "-in", str(signer), "-noout", "-subject", "-nameopt", "RFC2253",
        )
        public_key = await _openssl("x509", "-in", str(signer), "-pubkey", "-noout")
        spki = await _openssl("pkey", "-pubin", "-outform", "DER", stdin=public_key)

    if not hmac.compare_digest(embedded, _authenticode_digest(data, layout, algorithm)):
        raise ValueError("artifact's signature does not cover these bytes")
    key_sha256 = hashlib.sha256(spki).hexdigest()
    if not hmac.compare_digest(key_sha256, expected_key_sha256):
        raise ValueError("artifact is signed by a different publisher than the reviewed one")

    subject = raw_subject.decode("utf-8", "replace").strip()
    subject = subject.removeprefix("subject=").strip()[:MAX_SUBJECT_BYTES]
    match = _COMMON_NAME.search(subject)
    return SignatureFacts(
        subject=subject,
        common_name=(match.group(1).strip() if match else ""),
        key_sha256=key_sha256,
        digest_algorithm=algorithm,
    )
