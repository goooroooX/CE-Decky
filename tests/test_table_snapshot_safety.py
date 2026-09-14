from __future__ import annotations

import os
from pathlib import Path
import zipfile

import pytest

import ce_decky.table_store as table_store


CT = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>'


def _zip(path: Path, payload: bytes) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("main.CT", payload)


def test_archive_snapshot_rejects_opened_inode_that_does_not_match_selected_path(monkeypatch, tmp_path: Path):
    source = tmp_path / "tables.zip"
    replacement = tmp_path / "replacement.zip"
    _zip(source, CT)
    _zip(replacement, CT.replace(b"45", b"46"))
    store = table_store.TableStore(tmp_path / "store")
    real_open = os.open

    def swapped_open(path, flags, mode=0o777):
        if Path(path) == source:
            return real_open(replacement, flags, mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(table_store.os, "open", swapped_open)
    with pytest.raises(ValueError, match="changed while being snapshotted"):
        store.import_selection(str(source), member_path="main.CT")
    assert store.list_tables() == []


def test_archive_import_uses_managed_snapshot_before_inspection(monkeypatch, tmp_path: Path):
    source = tmp_path / "tables.zip"
    _zip(source, CT)
    store = table_store.TableStore(tmp_path / "store")
    real_inspect = table_store.inspect_archive
    observed: list[Path] = []

    def inspect_staged(path: Path, sevenzip=None):
        observed.append(path)
        assert path.parent == store.staging_root
        assert path != source
        return real_inspect(path, sevenzip)

    monkeypatch.setattr(table_store, "inspect_archive", inspect_staged)
    artifact = store.import_selection(str(source), member_path="main.CT")
    assert artifact.filename == "main.CT"
    assert observed and not observed[0].exists()
