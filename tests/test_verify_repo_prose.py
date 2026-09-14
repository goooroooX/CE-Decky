"""The em dash rule for documents that survive to publication.

An em dash is format in exactly two places, both parsed by `verify_repo.py`,
`check_release.py`: the AGENTS development version line
and a CHANGELOG version heading. Everywhere else it is ordinary punctuation
somebody typed, and this project does not use it in its own voice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo

EM = "—"


def test_the_repository_itself_has_no_prose_em_dashes():
    # The check is only worth having if the tree it guards already passes it.
    verify_repo._verify_no_prose_em_dashes()


@pytest.mark.parametrize("line", [
    f"## 0.9.5 {EM} 2026-08-31",
    f"Current development version: **0.9.5 {EM} 2026-08-31**",
])
def test_version_headings_keep_their_em_dash(line: str):
    # These are a format three scripts parse, not a stylistic choice.
    assert verify_repo.EM_DASH_STRUCTURAL.match(line)


@pytest.mark.parametrize("line", [
    f"Some prose {EM} with an aside.",
    f"- **Runtime** {EM} the current session.",
    f"## Not a version heading {EM} really",
])
def test_prose_and_near_misses_are_not_structural(line: str):
    assert not verify_repo.EM_DASH_STRUCTURAL.match(line)


def test_a_quoted_format_is_not_prose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Documentation that describes the changelog heading has to be able to
    # write it out.
    root = _repo(tmp_path, monkeypatch, {
        "README.md": f"Confirm the heading is `## x.y.z {EM} YYYY-MM-DD` before tagging.\n",
    })
    assert root
    verify_repo._verify_no_prose_em_dashes()


def test_prose_em_dash_is_reported_with_its_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _repo(tmp_path, monkeypatch, {
        "README.md": f"First line.\nAn aside {EM} like this one.\n",
    })
    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_no_prose_em_dashes()
    assert "README.md:2" in str(refused.value)


def test_every_changelog_entry_is_held_to_the_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Not only the newest: the maintainer took the whole file through the rule
    # once, so an older entry drifting back is a regression like any other.
    _repo(tmp_path, monkeypatch, {
        "CHANGELOG.md": (
            "# Changelog\n\n"
            f"## 0.9.5 {EM} 2026-08-31\n\n"
            "- [Fixed] Something, stated plainly.\n\n"
            f"## 0.9.4 {EM} 2026-08-30\n\n"
            f"- [Fixed] Something older {EM} with an aside.\n"
        ),
    })
    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_no_prose_em_dashes()
    assert "CHANGELOG.md:9" in str(refused.value)


def test_version_headings_survive_the_whole_file_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _repo(tmp_path, monkeypatch, {
        "CHANGELOG.md": (
            "# Changelog\n\n"
            f"## 0.9.5 {EM} 2026-08-31\n\n"
            "- [Fixed] Something, stated plainly.\n\n"
            f"## 0.9.4 {EM} 2026-08-30\n\n"
            "- [Fixed] Something older, stated plainly.\n"
        ),
    })
    verify_repo._verify_no_prose_em_dashes()


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str]) -> Path:
    """A throwaway tree containing only the documents under test."""
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    return tmp_path
