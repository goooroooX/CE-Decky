"""Keep controller groups inside Steam's exact focus-flow vocabulary."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


def test_the_repository_uses_only_supported_focusable_flows():
    verify_repo._verify_focusable_flows()


def test_every_installed_steam_flow_value_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / verify_repo.FOCUSABLE_FLOW_OWNER
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(
            f'<Focusable flow-children="{value}">{{children}}</Focusable>'
            for value in sorted(verify_repo.FOCUSABLE_FLOWS)
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    verify_repo._verify_focusable_flows()


def test_direction_instead_of_layout_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / verify_repo.FOCUSABLE_FLOW_OWNER
    source.parent.mkdir(parents=True)
    source.write_text(
        '<Focusable flow-children="row">{first}</Focusable>\n'
        '<Focusable flow-children="right">{second}</Focusable>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_focusable_flows()
    assert "src/components/PanelDensity.tsx:2='right'" in str(refused.value)


def test_directional_group_cannot_bypass_enabled_control_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src" / "groups.tsx"
    source.parent.mkdir(parents=True)
    source.write_text(
        '<Focusable flow-children="row">{actions}</Focusable>\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_focusable_flows()
    assert "src/groups.tsx:1=bypass ActionGroup" in str(refused.value)


def test_the_repository_renders_one_section_heading_everywhere():
    verify_repo._verify_section_headings()


def test_steam_own_section_title_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Steam renders that title inside a shrink-to-fit element whose class is a
    # bare content hash on the shipped client, so the rule that separates one
    # section from the next cannot be attached to it. Both were in use, and the
    # same screen looked like two different products depending on which modal
    # the user had opened.
    source = tmp_path / "src" / "modals" / "Example.tsx"
    source.parent.mkdir(parents=True)
    source.write_text('<PanelSection title="Choose game">{rows}</PanelSection>', encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    with pytest.raises(SystemExit, match="SectionHeading"):
        verify_repo._verify_section_headings()

    source.write_text(
        "<PanelSection>\n  <SectionHeading>Choose game</SectionHeading>\n</PanelSection>",
        encoding="utf-8",
    )
    verify_repo._verify_section_headings()
