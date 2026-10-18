"""Absence decisions must survive Python 3.14's OSError-suppressing predicates."""
from pathlib import Path
import logging

import pytest

from ce_decky.ce_launch import CELaunchSupervisor
from ce_decky.session_protocol import SessionStore
from ce_decky.table_store import TableStore


def _readers(root):
    sessions = SessionStore(root, root)
    tables = TableStore(root / "tables")
    launches = CELaunchSupervisor(root, root / "ce", root, logging.getLogger("state-access-test"))
    return {
        "pointer": lambda: sessions.load_current(42),
        "inventory": sessions.inventory,
        "tables": tables.list_tables_with_errors,
        "launch": lambda: launches.inspect_owned_launch_record(42),
    }


@pytest.mark.parametrize("reader", ["pointer", "inventory", "tables", "launch"])
def test_denied_parent_is_never_absent(tmp_path: Path, reader):
    parent = tmp_path / "private"
    root = parent / "state"
    root.mkdir(parents=True)
    readers = _readers(root)
    witness = root / "unchanged"
    witness.write_bytes(b"original")
    parent.chmod(0)
    try:
        # A chmod fixture run as root can falsely prove this regression fixed.
        with pytest.raises(PermissionError):
            root.stat()
        with pytest.raises(ValueError):
            readers[reader]()
    finally:
        parent.chmod(0o700)
    assert witness.read_bytes() == b"original"
    assert sorted(p.name for p in root.iterdir()) == ["unchanged"]


def test_genuinely_missing_state_stays_missing_without_creating_directories(tmp_path: Path):
    root = tmp_path / "absent"
    readers = _readers(root)
    assert readers["pointer"]() is None
    assert readers["inventory"]() == {"apps": [], "total_sessions": 0, "errors": []}
    assert readers["tables"]() == ([], [])
    assert readers["launch"]() == {"state": "absent", "record": None}
    assert not root.exists()


@pytest.mark.parametrize("data", [b"null", b"{", b"{}"])
def test_present_invalid_pointer_is_not_absence(tmp_path: Path, data):
    path = tmp_path / "sessions" / "42" / "current.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with pytest.raises(ValueError):
        _readers(tmp_path)["pointer"]()
    assert path.read_bytes() == data


@pytest.mark.parametrize("kind", ["pointer", "inventory", "tables", "launch"])
def test_dangling_link_does_not_become_absent(tmp_path: Path, kind):
    paths = {
        "pointer": tmp_path / "sessions" / "42" / "current.json",
        "inventory": tmp_path / "sessions",
        "tables": tmp_path / "tables" / "metadata",
        "launch": tmp_path / "ce-launch" / "42.json",
    }
    path = paths[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(tmp_path / "missing")
    if kind == "launch":
        assert _readers(tmp_path)[kind]()["state"] == "invalid"
    else:
        with pytest.raises(ValueError):
            _readers(tmp_path)[kind]()
    assert path.is_symlink()
