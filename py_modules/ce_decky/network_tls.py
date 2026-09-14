from __future__ import annotations

from pathlib import Path
import os
import ssl

# Decky can run plugins inside an embedded Python. On target systems the interpreter's
# compiled OpenSSL CA path can be unusable even though SteamOS has a valid system bundle.
# Keep verification enabled and explicitly load a real bundle when defaults contain no roots.
SYSTEM_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # SteamOS/Arch, Debian/Ubuntu
    "/etc/ssl/cert.pem",                  # Arch/OpenSSL compatibility path
    "/etc/pki/tls/certs/ca-bundle.crt",   # Fedora/Bazzite
    "/etc/ssl/ca-bundle.pem",             # openSUSE
)


def candidate_ca_bundles() -> list[Path]:
    candidates: list[str | None] = [
        os.environ.get("SSL_CERT_FILE"),
        ssl.get_default_verify_paths().cafile,
        *SYSTEM_CA_BUNDLES,
    ]
    try:
        import certifi  # type: ignore
        candidates.append(certifi.where())
    except ImportError:
        pass

    result: list[Path] = []
    seen: set[str] = set()
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            result.append(path)
    return result


def build_verified_ssl_context() -> tuple[ssl.SSLContext, str | None]:
    """Return a TLS-verifying context and the explicit CA file if one was needed.

    Never falls back to CERT_NONE.  If no trust roots can be loaded, the returned context
    remains verifying and HTTPS will fail closed on certificate validation.
    """
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) > 0:
        return context, None

    for path in candidate_ca_bundles():
        try:
            context.load_verify_locations(cafile=str(path))
        except (OSError, ssl.SSLError):
            continue
        if context.cert_store_stats().get("x509_ca", 0) > 0:
            return context, str(path)
    return context, None
