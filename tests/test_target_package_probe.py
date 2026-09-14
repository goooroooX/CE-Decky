from __future__ import annotations

import json
from pathlib import Path
import stat
import zipfile

import pytest

from scripts import target_package_probe


def _repo(tmp_path: Path, *, version: str = "0.6.0", backend_version: str | None = None) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    backend = tmp_path / "py_modules" / "ce_decky"
    backend.mkdir(parents=True)
    backend.joinpath("__init__.py").write_text(
        f'__version__ = "{backend_version or version}"\n',
        encoding="utf-8",
    )


def _package(tmp_path: Path, *, version: str = "0.6.0", backend_version: str | None = None) -> Path:
    package = tmp_path / "plugin.zip"
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in target_package_probe.REQUIRED_FILES:
            if name == f"{target_package_probe.PACKAGE_ROOT}/package.json":
                payload = json.dumps({"version": version}).encode("utf-8")
            elif name == target_package_probe.BACKEND_INIT:
                payload = f'__version__ = "{backend_version or version}"\n'.encode("utf-8")
            else:
                payload = b"fixture\n"
            archive.writestr(name, payload)
        archive.writestr(f"{target_package_probe.PACKAGE_ROOT}/licenses/LGPL-2.1.txt", b"fixture\n")
    return package


def test_valid_package_reports_digest_root_and_all_version_identities(monkeypatch, tmp_path: Path):
    _repo(tmp_path)
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    package = _package(tmp_path)

    report = target_package_probe.inspect_package(package)
    assert report["ok"] is True
    assert report["schema"] == 2
    assert len(report["sha256"]) == 64
    assert report["package_root"] == "CE-Decky"
    assert report["version"] == "0.6.0"
    assert report["repository_backend_version"] == "0.6.0"
    assert report["packaged_version"] == "0.6.0"
    assert report["packaged_backend_version"] == "0.6.0"
    assert report["crc_checked"] is True
    assert report["metadata_limit_bytes"] == target_package_probe.MAX_METADATA_BYTES


def test_package_or_backend_version_mismatch_is_failed_contract(monkeypatch, tmp_path: Path):
    _repo(tmp_path)
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    report = target_package_probe.inspect_package(_package(tmp_path, version="9.9.9", backend_version="8.8.8"))
    assert report["ok"] is False
    assert any("packaged version mismatch" in error for error in report["errors"])
    assert any("packaged backend version mismatch" in error for error in report["errors"])


def test_repository_backend_version_mismatch_is_failed_contract(monkeypatch, tmp_path: Path):
    _repo(tmp_path, backend_version="0.5.0")
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    report = target_package_probe.inspect_package(_package(tmp_path))
    assert report["ok"] is False
    assert any("repository backend version mismatch" in error for error in report["errors"])


def test_unsafe_outside_root_duplicate_and_symlink_members_are_rejected(monkeypatch, tmp_path: Path):
    _repo(tmp_path)
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    package = _package(tmp_path)
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("../escape", b"bad")
        archive.writestr("C:/absolute-ish", b"bad")
        archive.writestr("outside.txt", b"bad")
        archive.writestr("CE-Decky/README.md", b"duplicate")
        info = zipfile.ZipInfo("CE-Decky/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, b"target")

    report = target_package_probe.inspect_package(package)
    assert report["ok"] is False
    unsafe = next(error for error in report["errors"] if "unsafe/outside-root" in error)
    assert "outside.txt" in unsafe
    assert any("duplicate member" in error for error in report["errors"])
    assert any("symlink" in error for error in report["errors"])


def test_symlinked_package_input_is_rejected(monkeypatch, tmp_path: Path):
    _repo(tmp_path)
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    package = _package(tmp_path)
    link = tmp_path / "package-link.zip"
    try:
        link.symlink_to(package)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="must not be a symlink"):
        target_package_probe.inspect_package(link)


def test_oversized_version_metadata_is_rejected_before_parsing(monkeypatch, tmp_path: Path):
    _repo(tmp_path)
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    monkeypatch.setattr(target_package_probe, "MAX_METADATA_BYTES", 1)
    report = target_package_probe.inspect_package(_package(tmp_path))

    assert report["ok"] is False
    assert report["packaged_version"] is None
    assert report["packaged_backend_version"] is None
    assert any("metadata members exceed" in error for error in report["errors"])


def test_missing_required_payload_is_rejected(monkeypatch, tmp_path: Path):
    _repo(tmp_path)
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    package = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("CE-Decky/package.json", json.dumps({"version": "0.6.0"}))

    report = target_package_probe.inspect_package(package)
    assert report["ok"] is False
    assert any("required files missing" in error for error in report["errors"])
    assert any("required directory payloads missing" in error for error in report["errors"])


def test_ce_and_table_payloads_are_never_vendored(monkeypatch, tmp_path: Path):
    _repo(tmp_path)
    monkeypatch.setattr(target_package_probe, "ROOT", tmp_path)
    package = _package(tmp_path)
    with zipfile.ZipFile(package, "a") as archive:
        archive.writestr("CE-Decky/py_modules/CheatEngine.exe", b"MZ")
        archive.writestr("CE-Decky/tables/bundled.CT", b"<CheatTable/>")

    report = target_package_probe.inspect_package(package)
    assert report["ok"] is False
    assert any("forbidden CE/table payloads" in error for error in report["errors"])
