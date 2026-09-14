#!/usr/bin/env python3
"""
authenticode_verify.py — check who signed a Windows executable, against a
pinned publisher key.

Purpose
-------
`inno_setup_reader.py` can resolve the clean Cheat Engine installer's URL after
it rotates, but a URL is not provenance. This answers the next question: was the
artifact at that URL signed by the same key that signed the release CE Decky
already reviewed?

That matters because the reviewed manifest pins one SHA-256. When the pinned
artifact is served unchanged the hash settles it and this tool adds nothing.
When the hash differs - a rotated URL, or a new upstream version - the publisher
key is the one identity that carries across versions, so it is the strongest
single input a maintainer has before reviewing a new artifact.

**Maintainer-side.** It informs a manifest-update review; it is not an automatic
trust decision and nothing in the plugin runtime calls it.

Division of labour
------------------
The signature itself is verified by `openssl smime`, which parses the PKCS#7
SignedData, checks the RSA signature over the signed attributes, and hands back
the signing certificate. `openssl cms` refuses these blobs - Authenticode's
`SpcIndirectDataContent` is not a content type CMS accepts - but the older SMIME
path handles them.

What OpenSSL cannot do is compute the Authenticode digest, because that is a
Microsoft-specific hash over the PE with three regions excluded. That, and the
small comparison around it, is what this file implements:

* skip the optional header's `CheckSum`, which the signing process itself
  changes;
* skip the certificate table's data directory entry, for the same reason;
* skip the certificate table at the end, which holds the signature being
  checked.

Chain building and revocation are deliberately *not* performed. They answer
"does some CA vouch for this?", while the question here is "is this the exact
key we reviewed?", which a pinned SubjectPublicKeyInfo answers directly and
without depending on a trust store.

Usage
-----
    python3 authenticode_verify.py show   <file.exe>
    python3 authenticode_verify.py verify <file.exe> [--expect-spki SHA256]

`verify` exits non-zero unless the signature is valid, the Authenticode digest
matches the file, and the signer's public key matches the pin.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import argparse
import hashlib
import hmac
import re
import shutil
import struct
import subprocess
import sys
import tempfile


# The publisher of the reviewed CheatEngine77.exe, observed 2026-08-30 on the
# artifact whose SHA-256 `managed_ce_manifest.json` pins. A re-key changes this
# and must fail, because a new key is exactly what needs a human look.
CHEAT_ENGINE_EZ_SPKI_SHA256 = (
    "c6b1fd6c80b1687e348bf4d08bfc5234ea793f4e832c112b7774a4b959ba98c1"
)
CHEAT_ENGINE_EZ_COMMON_NAME = "Cheat Engine EZ"

# WIN_CERTIFICATE, from winnt.h.
WIN_CERT_REVISION_2_0 = 0x0200
WIN_CERT_TYPE_PKCS_SIGNED_DATA = 0x0002

MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_CERTIFICATE_BYTES = 8 * 1024 * 1024
OPENSSL_TIMEOUT_SECONDS = 60

_DIGEST_OIDS = {
    "1.3.14.3.2.26": "sha1",
    "2.16.840.1.101.3.4.2.1": "sha256",
    "2.16.840.1.101.3.4.2.2": "sha384",
    "2.16.840.1.101.3.4.2.3": "sha512",
}


class SignatureError(ValueError):
    """The file is not signed in a way this checker will accept."""


@dataclass(frozen=True)
class SignatureFacts:
    digest_algorithm: str
    embedded_digest: str
    computed_digest: str
    signer_subject: str
    signer_common_name: str
    spki_sha256: str

    @property
    def digest_matches(self) -> bool:
        return hmac.compare_digest(self.embedded_digest, self.computed_digest)


# --- PE structure ------------------------------------------------------------

@dataclass(frozen=True)
class PELayout:
    checksum_offset: int
    security_directory_offset: int
    size_of_headers: int
    sections: tuple[tuple[int, int], ...]
    certificate_offset: int
    certificate_size: int


def parse_pe(data: bytes) -> PELayout:
    if len(data) < 0x40 or data[:2] != b"MZ":
        raise SignatureError("not a PE file: missing MZ signature")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if pe + 24 > len(data) or data[pe:pe + 4] != b"PE\x00\x00":
        raise SignatureError("not a PE file: missing PE signature")

    section_count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x10B:
        directories = optional + 96
    elif magic == 0x20B:
        directories = optional + 112
    else:
        raise SignatureError(f"unsupported PE optional header magic 0x{magic:04x}")

    directory_count = struct.unpack_from("<I", data, directories - 4)[0]
    if directory_count < 5:
        raise SignatureError("PE has no certificate table data directory")
    security = directories + 4 * 8
    certificate_offset, certificate_size = struct.unpack_from("<II", data, security)

    size_of_headers = struct.unpack_from("<I", data, optional + 60)[0]
    if not 0 < size_of_headers <= len(data):
        raise SignatureError(f"invalid SizeOfHeaders: {size_of_headers}")

    sections: list[tuple[int, int]] = []
    table = optional + optional_size
    for index in range(section_count):
        entry = table + index * 40
        if entry + 40 > len(data):
            raise SignatureError("truncated PE section table")
        raw_size, raw_pointer = struct.unpack_from("<II", data, entry + 16)
        if raw_size:
            if raw_pointer + raw_size > len(data):
                raise SignatureError("PE section extends past end of file")
            sections.append((raw_pointer, raw_size))
    sections.sort()

    return PELayout(
        checksum_offset=optional + 64,
        security_directory_offset=security,
        size_of_headers=size_of_headers,
        sections=tuple(sections),
        certificate_offset=certificate_offset,
        certificate_size=certificate_size,
    )


def authenticode_digest(data: bytes, layout: PELayout, algorithm: str) -> str:
    """Hash the PE the way Authenticode does, skipping the three signing fields."""
    digest = hashlib.new(algorithm)
    digest.update(data[:layout.checksum_offset])
    digest.update(data[layout.checksum_offset + 4:layout.security_directory_offset])
    digest.update(data[layout.security_directory_offset + 8:layout.size_of_headers])

    cursor = layout.size_of_headers
    for pointer, size in layout.sections:
        digest.update(data[pointer:pointer + size])
        cursor = pointer + size

    # Anything appended after the last section is part of the file and is
    # hashed too - for an Inno installer that is the entire payload. Only the
    # certificate table at the very end stays out.
    end = layout.certificate_offset if layout.certificate_size else len(data)
    if end > cursor:
        digest.update(data[cursor:end])
    return digest.hexdigest()


def extract_signature(data: bytes, layout: PELayout) -> bytes:
    """Return the PKCS#7 blob from the PE's WIN_CERTIFICATE entry."""
    if not layout.certificate_size:
        raise SignatureError("the file carries no Authenticode signature")
    start, size = layout.certificate_offset, layout.certificate_size
    if start + size > len(data) or size < 8:
        raise SignatureError("certificate table extends past end of file")
    if size > MAX_CERTIFICATE_BYTES:
        raise SignatureError(f"certificate table is {size} bytes, above the bound")

    length, revision, kind = struct.unpack_from("<IHH", data, start)
    if revision != WIN_CERT_REVISION_2_0:
        raise SignatureError(f"unsupported WIN_CERTIFICATE revision 0x{revision:04x}")
    if kind != WIN_CERT_TYPE_PKCS_SIGNED_DATA:
        raise SignatureError(f"unsupported certificate type 0x{kind:04x}")
    if not 8 < length <= size:
        raise SignatureError(f"invalid WIN_CERTIFICATE length {length}")
    return data[start + 8:start + length]


# --- Minimal DER, only enough to read SpcIndirectDataContent ------------------

def _der_read(buf: bytes, pos: int) -> tuple[int, bytes, int]:
    """Return `(tag, value, next_position)` for one DER element."""
    if pos + 2 > len(buf):
        raise SignatureError("truncated DER element")
    tag = buf[pos]
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or pos + count > len(buf):
            raise SignatureError("unsupported DER length encoding")
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    if pos + length > len(buf):
        raise SignatureError("DER element extends past end of buffer")
    return tag, buf[pos:pos + length], pos + length


def _der_oid(value: bytes) -> str:
    if not value:
        raise SignatureError("empty OID")
    parts = [str(value[0] // 40), str(value[0] % 40)]
    current = 0
    for byte in value[1:]:
        current = (current << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(current))
            current = 0
    return ".".join(parts)


def parse_indirect_data(content: bytes) -> tuple[str, str]:
    """Read `(algorithm, digest)` out of SpcIndirectDataContent's DigestInfo.

    The structure is `SEQUENCE { attribute, DigestInfo }`, and DigestInfo is
    `SEQUENCE { AlgorithmIdentifier, OCTET STRING }`. OpenSSL hands back the
    content with the outer attribute already consumed on some paths, so locate
    the DigestInfo rather than assuming a fixed position.
    """
    for candidate in _der_candidates(content):
        tag, body, _ = _der_read(candidate, 0)
        if tag != 0x30:
            continue
        try:
            algorithm_tag, algorithm, rest = _der_read(body, 0)
            digest_tag, digest, _ = _der_read(body, rest)
        except SignatureError:
            continue
        if algorithm_tag != 0x30 or digest_tag != 0x04:
            continue
        oid_tag, oid_value, _ = _der_read(algorithm, 0)
        if oid_tag != 0x06:
            continue
        name = _DIGEST_OIDS.get(_der_oid(oid_value))
        if name is None:
            raise SignatureError(
                f"unsupported Authenticode digest algorithm {_der_oid(oid_value)}"
            )
        return name, digest.hex()
    raise SignatureError("no DigestInfo found in the signed content")


def _der_candidates(content: bytes) -> list[bytes]:
    """Every top-level DER element in `content`, outermost first."""
    candidates: list[bytes] = []
    pos = 0
    while pos < len(content):
        try:
            _tag, _value, next_pos = _der_read(content, pos)
        except SignatureError:
            break
        candidates.append(content[pos:next_pos])
        pos = next_pos
    # An outer SEQUENCE wraps the DigestInfo; look inside it as well.
    for element in list(candidates):
        tag, body, _ = _der_read(element, 0)
        if tag == 0x30:
            pos = 0
            while pos < len(body):
                try:
                    _tag, _value, next_pos = _der_read(body, pos)
                except SignatureError:
                    break
                candidates.append(body[pos:next_pos])
                pos = next_pos
    return candidates


# --- OpenSSL ------------------------------------------------------------------

def _openssl(*args: str, stdin: bytes | None = None) -> bytes:
    binary = shutil.which("openssl")
    if binary is None:
        raise SignatureError(
            "openssl is required for signature verification and was not found "
            "on PATH"
        )
    try:
        done = subprocess.run(
            [binary, *args],
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=OPENSSL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SignatureError(f"could not run openssl: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise SignatureError(
            f"openssl {args[0]} failed: {detail[0] if detail else 'no output'}"
        )
    return done.stdout


_CN = re.compile(r"(?:^|,\s*)CN\s*=\s*([^,]+)")


def read_signature_facts(path: Path) -> SignatureFacts:
    size = path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise SignatureError(f"{path.name} is {size} bytes, above the reader bound")
    data = path.read_bytes()
    layout = parse_pe(data)
    blob = extract_signature(data, layout)

    with tempfile.TemporaryDirectory(prefix="authenticode-") as work:
        root = Path(work)
        signature = root / "signature.der"
        content = root / "content.der"
        signer = root / "signer.pem"
        signature.write_bytes(blob)
        # `-noverify` skips chain building only; the signature over the signed
        # attributes is still checked, and a pinned key replaces the chain.
        _openssl(
            "smime", "-verify", "-noverify", "-inform", "DER",
            "-in", str(signature), "-out", str(content), "-signer", str(signer),
        )
        algorithm, embedded = parse_indirect_data(content.read_bytes())
        subject = _openssl(
            "x509", "-in", str(signer), "-noout", "-subject", "-nameopt", "RFC2253",
        ).decode("utf-8", "replace").strip()
        public_key = _openssl("x509", "-in", str(signer), "-pubkey", "-noout")
        spki = _openssl("pkey", "-pubin", "-outform", "DER", stdin=public_key)

    subject = subject.removeprefix("subject=").strip()
    match = _CN.search(subject)
    return SignatureFacts(
        digest_algorithm=algorithm,
        embedded_digest=embedded,
        computed_digest=authenticode_digest(data, layout, algorithm),
        signer_subject=subject,
        signer_common_name=match.group(1).strip() if match else "",
        spki_sha256=hashlib.sha256(spki).hexdigest(),
    )


# --- Commands ----------------------------------------------------------------

def _report(facts: SignatureFacts) -> None:
    print(f"signer subject   : {facts.signer_subject}")
    print(f"signer key       : sha256:{facts.spki_sha256}")
    print(f"digest algorithm : {facts.digest_algorithm}")
    print(f"embedded digest  : {facts.embedded_digest}")
    print(f"computed digest  : {facts.computed_digest}")
    print(f"digest matches   : {'yes' if facts.digest_matches else 'NO'}")


def _cmd_show(args: argparse.Namespace) -> int:
    facts = read_signature_facts(args.executable)
    _report(facts)
    return 0 if facts.digest_matches else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    facts = read_signature_facts(args.executable)
    _report(facts)
    expected = args.expect_spki.lower()
    if not facts.digest_matches:
        print("\nrefused: the signature does not cover these bytes",
              file=sys.stderr)
        return 1
    if not hmac.compare_digest(facts.spki_sha256, expected):
        print(f"\nrefused: signed by a different key than the pinned "
              f"sha256:{expected}", file=sys.stderr)
        return 1
    print(f"\nsigned by the pinned publisher key "
          f"({facts.signer_common_name or 'unnamed subject'})")
    if (args.expect_spki == CHEAT_ENGINE_EZ_SPKI_SHA256
            and facts.signer_common_name != CHEAT_ENGINE_EZ_COMMON_NAME):
        print(f"note: the pinned key now names "
              f"{facts.signer_common_name!r}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Windows executable's Authenticode signature "
                    "against a pinned publisher key.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", help="print the signer and both digests")
    show.add_argument("executable", type=Path)
    show.set_defaults(handler=_cmd_show)

    verify = sub.add_parser("verify", help="require the pinned publisher key")
    verify.add_argument("executable", type=Path)
    verify.add_argument(
        "--expect-spki", default=CHEAT_ENGINE_EZ_SPKI_SHA256,
        help="SHA-256 of the signer's SubjectPublicKeyInfo "
             "(default: the reviewed Cheat Engine publisher)",
    )
    verify.set_defaults(handler=_cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (SignatureError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
