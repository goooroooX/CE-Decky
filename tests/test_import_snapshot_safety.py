from __future__ import annotations

import os
from pathlib import Path

import pytest

from ce_decky.acquisition import stable_snapshot


def test_browser_snapshot_copies_one_stable_open_file(tmp_path: Path):
    source = tmp_path / "selected.CT"
    source.write_bytes(b"<CheatTable />")
    staging = tmp_path / "staging"

    snapshot = stable_snapshot(source, staging)

    assert snapshot.read_bytes() == source.read_bytes()
    assert snapshot.parent == staging


def test_browser_snapshot_rejects_path_swap_after_open(monkeypatch, tmp_path: Path):
    source = tmp_path / "selected.CT"
    source.write_bytes(b"expected")
    replacement = tmp_path / "replacement.CT"
    replacement.write_bytes(b"replacement")
    staging = tmp_path / "staging"
    real_open = os.open

    def swapped_open(path, flags, mode=0o777):
        candidate = Path(path)
        if candidate == source:
            return real_open(replacement, flags, mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr("ce_decky.acquisition.os.open", swapped_open)

    with pytest.raises(ValueError, match="changed while being staged"):
        stable_snapshot(source, staging)
    assert list(staging.glob(".browser-*")) == []


def test_browser_snapshot_rejects_managed_staging_symlink(tmp_path: Path):
    source = tmp_path / "selected.CT"
    source.write_bytes(b"expected")
    outside = tmp_path / "outside"
    outside.mkdir()
    staging = tmp_path / "managed" / "provider-downloads"
    staging.parent.mkdir()
    try:
        staging.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="staging directory must not be a symlink"):
        stable_snapshot(source, staging)
    assert list(outside.iterdir()) == []


def test_table_import_rejects_final_symlink_source(tmp_path: Path):
    from ce_decky.table_store import TableStore

    real = tmp_path / "real.CT"
    real.write_bytes(b'<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>')
    selected = tmp_path / "selected.CT"
    try:
        selected.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="regular file|does not exist"):
        TableStore(tmp_path / "store").import_ct(str(selected))
