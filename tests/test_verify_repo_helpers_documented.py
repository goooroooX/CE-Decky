"""A helper the documentation has never heard of is a helper nobody finds.

`AGENTS.md` sends an agent to the tracked helpers before it writes a command of
its own, and the whole value of that is that what it needs is in the table. The
rule was stated in the contract and trusted; this is what checks it, and the
run that added it is the one that showed why trusting it is not enough.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


def _tree(root: Path, *, helpers: dict[str, str], document: str) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    for name, body in helpers.items():
        (root / "scripts" / name).write_text(body, encoding="utf-8")
    (root / "docs" / "DEVELOPMENT.md").write_text(document, encoding="utf-8")


def test_the_repository_as_it_stands_documents_every_helper_it_ships():
    verify_repo._verify_helpers_are_documented()


def test_a_helper_the_document_never_heard_of_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _tree(
        tmp_path,
        helpers={"target_new_probe.py": '"""A probe."""\n'},
        document="# Development\n\nNothing about it.\n",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_helpers_are_documented()

    assert "scripts/target_new_probe.py" in str(refused.value)


def test_naming_the_helper_is_what_satisfies_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # It cannot check that what is written is any good, and does not try to.
    _tree(
        tmp_path,
        helpers={"target_new_probe.py": '"""A probe."""\n'},
        document="# Development\n\n`python3 scripts/target_new_probe.py` reads one bounded thing.\n",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {})

    verify_repo._verify_helpers_are_documented()


def test_an_exemption_is_a_reason_and_expires_with_the_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Something already documented has to be what runs it, and the entry has to
    # go when the helper does: an allowlist nobody prunes is how a rule stops
    # meaning anything.
    _tree(
        tmp_path,
        helpers={"engine.py": '"""Started by the documented one."""\n'},
        document="# Development\n\nNothing about it.\n",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    monkeypatch.setattr(verify_repo, "UNDOCUMENTED_HELPERS", {"engine.py": "started by the documented helper"})
    verify_repo._verify_helpers_are_documented()

    (tmp_path / "scripts" / "engine.py").unlink()
    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_helpers_are_documented()
    assert "engine.py" in str(refused.value)


def test_every_exemption_this_repository_carries_still_names_a_helper():
    for name, reason in verify_repo.UNDOCUMENTED_HELPERS.items():
        assert (verify_repo.ROOT / "scripts" / name).is_file(), f"{name}: {reason}"
        assert reason.strip(), f"{name} is exempt for no stated reason"
