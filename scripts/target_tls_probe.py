#!/usr/bin/env python3
"""Perform one verified HTTPS target probe using CE Decky's production TLS context."""
from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "py_modules"))

from ce_decky.network_tls import build_verified_ssl_context  # noqa: E402


def _target(url: str) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("probe URL must be absolute HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("probe URL must not contain credentials, query, or fragment")
    port = parsed.port or 443
    path = parsed.path or "/"
    return parsed.hostname, port, path


def probe(
    url: str,
    *,
    timeout: float = 15.0,
    connection_factory=http.client.HTTPSConnection,
) -> dict[str, object]:
    host, port, path = _target(url)
    context, explicit_ca = build_verified_ssl_context()
    roots = int(context.cert_store_stats().get("x509_ca", 0))
    if context.verify_mode == ssl.CERT_NONE or not context.check_hostname or roots <= 0:
        raise RuntimeError("production TLS context is not verification-ready")
    connection = connection_factory(host, port, timeout=timeout, context=context)
    try:
        connection.request("HEAD", path, headers={"User-Agent": "CE-Decky-target-tls-probe"})
        response = connection.getresponse()
        sock = connection.sock
        return {
            "schema": 1,
            "url": url,
            "host": host,
            "port": port,
            "http_status": int(response.status),
            "http_reason": str(response.reason),
            "verification_enabled": True,
            "x509_ca_roots": roots,
            "explicit_ca_file": explicit_ca,
            "tls_version": sock.version() if sock is not None else None,
            "cipher": sock.cipher()[0] if sock is not None and sock.cipher() else None,
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://github.com/")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    try:
        result = probe(args.url, timeout=args.timeout)
    except Exception as exc:
        print(json.dumps({"schema": 1, "url": args.url, "ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({**result, "ok": True}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
