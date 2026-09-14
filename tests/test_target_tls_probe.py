import ssl

import pytest

from scripts import target_tls_probe


class FakeSocket:
    def version(self):
        return "TLSv1.3"

    def cipher(self):
        return ("TLS_AES_256_GCM_SHA384", "TLSv1.3", 256)


class FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeConnection:
    last = None

    def __init__(self, host, port, *, timeout, context):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.sock = FakeSocket()
        self.request_args = None
        FakeConnection.last = self

    def request(self, method, path, headers=None):
        self.request_args = (method, path, headers)

    def getresponse(self):
        return FakeResponse()

    def close(self):
        pass


class FakeContext:
    verify_mode = ssl.CERT_REQUIRED
    check_hostname = True

    def cert_store_stats(self):
        return {"x509_ca": 123}


def test_target_rejects_non_https_credentials_and_query():
    for url in (
        "http://github.com/",
        "https://user:pass@github.com/",
        "https://github.com/?token=x",
        "https://github.com/#frag",
    ):
        with pytest.raises(ValueError):
            target_tls_probe._target(url)


def test_probe_uses_production_verification_context_and_accepts_http_error_status(monkeypatch):
    monkeypatch.setattr(
        target_tls_probe,
        "build_verified_ssl_context",
        lambda: (FakeContext(), "/etc/ssl/certs/ca-certificates.crt"),
    )
    result = target_tls_probe.probe(
        "https://github.com/path",
        timeout=7.0,
        connection_factory=FakeConnection,
    )
    assert result["http_status"] == 403
    assert result["verification_enabled"] is True
    assert result["x509_ca_roots"] == 123
    assert result["tls_version"] == "TLSv1.3"
    assert result["cipher"] == "TLS_AES_256_GCM_SHA384"
    assert FakeConnection.last.host == "github.com"
    assert FakeConnection.last.request_args[0:2] == ("HEAD", "/path")


def test_probe_fails_closed_when_tls_context_is_not_verification_ready(monkeypatch):
    context = FakeContext()
    context.check_hostname = False
    monkeypatch.setattr(target_tls_probe, "build_verified_ssl_context", lambda: (context, None))
    with pytest.raises(RuntimeError, match="not verification-ready"):
        target_tls_probe.probe("https://github.com/", connection_factory=FakeConnection)
