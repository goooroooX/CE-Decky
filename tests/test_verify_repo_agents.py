"""Keep the marker that tells a reader whether they saw the whole contract."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


def test_start_here_names_the_section_the_contract_actually_ends_with():
    verify_repo._verify_agents_last_section()


def test_a_section_appended_below_the_named_one_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The instruction exists so that a reader whose tool cut the file short can
    # notice it themselves. Appending below the named section makes it a lie,
    # and it lies to exactly the reader who cannot see the end of the file.
    (tmp_path / "AGENTS.md").write_text(
        "1. Read this file completely. It ends with **Release discipline**.\n"
        "\n## Release discipline\n\n- something\n"
        "\n## Something newer\n\n- appended later\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_agents_last_section()

    assert "Something newer" in str(refused.value)
