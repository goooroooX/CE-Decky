from pathlib import Path

import ce_decky.network_tls as tls


def test_candidate_ca_bundles_prefers_explicit_env_file(tmp_path: Path, monkeypatch):
    cafile = tmp_path / "ca.pem"
    cafile.write_text("dummy")
    monkeypatch.setenv("SSL_CERT_FILE", str(cafile))
    candidates = tls.candidate_ca_bundles()
    assert candidates[0] == cafile
    assert len({str(p) for p in candidates}) == len(candidates)


def test_verified_context_never_disables_certificate_verification():
    context, _source = tls.build_verified_ssl_context()
    assert context.verify_mode.name == "CERT_REQUIRED"
    assert context.check_hostname is True
