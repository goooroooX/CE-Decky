"""The Store check has to refuse for the reason it claims.

Every failure this guard exists for produces an archive that unpacks cleanly
and looks installable: a missing vendor tree, notices that never survived the
`defaults/` strip, a blank `publish` block. A check that passed one of those
would be worse than none, because the next time anyone looks is after the
listing is live. These build each shape and read back which failure was named.
"""
from __future__ import annotations

from pathlib import Path
import json
import zipfile

import pytest

from scripts import check_store_artifact as store

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
VERSION = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]


def _archive(tmp_path: Path, *, drop: tuple[str, ...] = (), add: dict[str, str] | None = None,
             manifest: dict | None = None, version: str | None = None) -> Path:
    path = tmp_path / "ce-decky.zip"
    with zipfile.ZipFile(path, "w") as packed:
        for name in store.REQUIRED_MEMBERS:
            if name in drop:
                continue
            packed.writestr(f"ce-decky/{name}", "x\n")
        packed.writestr("ce-decky/plugin.json", json.dumps(manifest if manifest is not None else MANIFEST))
        packed.writestr("ce-decky/package.json", json.dumps({"version": version or VERSION}))
        for name, body in (add or {}).items():
            packed.writestr(f"ce-decky/{name}", body)
    return path


@pytest.fixture(autouse=True)
def _do_not_import_a_fixture(monkeypatch: pytest.MonkeyPatch):
    """These archives are shapes, not runtimes; the import is checked elsewhere."""
    monkeypatch.setattr(store, "verify_installed_backend", lambda archive, root: None)


def test_a_well_formed_archive_passes(tmp_path: Path):
    assert store.check(_archive(tmp_path)) == []


def test_a_backend_without_its_dependencies_is_refused(tmp_path: Path):
    found = store.check(_archive(tmp_path, drop=("py_modules/vendor/httpx/__init__.py",)))
    assert any("missing from the Store artifact" in failure for failure in found), found


def test_notices_that_did_not_survive_the_defaults_strip_are_refused(tmp_path: Path):
    found = store.check(_archive(tmp_path, drop=("THIRD_PARTY_NOTICES.md", "licenses/LGPL-2.1.txt")))
    assert any("THIRD_PARTY_NOTICES.md" in failure for failure in found), found


def test_an_empty_publish_block_is_refused(tmp_path: Path):
    manifest = {**MANIFEST, "publish": {**MANIFEST["publish"], "image": ""}}
    found = store.check(_archive(tmp_path, manifest=manifest))
    assert any("publish.image is empty" in failure for failure in found), found


def test_a_manifest_that_is_not_the_repository_is_refused(tmp_path: Path):
    found = store.check(_archive(tmp_path, manifest={**MANIFEST, "author": "Somebody Else"}))
    assert any("not the repository's" in failure for failure in found), found


def test_a_payload_is_refused(tmp_path: Path):
    found = store.check(_archive(tmp_path, add={"py_modules/ce_decky/CheatEngine.exe": "MZ"}))
    assert any("forbidden payload" in failure for failure in found), found


def test_development_material_is_refused(tmp_path: Path):
    found = store.check(_archive(tmp_path, add={"tests/test_secret.py": ""}))
    assert any("development material" in failure for failure in found), found


def test_a_version_from_somewhere_else_is_refused(tmp_path: Path):
    found = store.check(_archive(tmp_path, version="0.0.1"))
    assert any("does not come from" in failure for failure in found), found


def test_the_hash_the_database_appends_on_a_pull_request_is_accepted(tmp_path: Path):
    """Its workflow rewrites package.json before building, and that is normal."""
    assert store.check(_archive(tmp_path, version=f"{VERSION}-abc1234")) == []
