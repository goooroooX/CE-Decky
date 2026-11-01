"""Keep the release workflow naming artifacts the build actually produces.

The workflow checksummed a source ZIP for months after its generator was
deliberately removed. Nothing caught it because the step only runs on a `v*`
tag: the first stable tag would have failed at `sha256sum`, on a job that had
already spent a full release validation getting there.

These checks read the workflow as text on purpose. The failure mode is a name
that no longer exists, and a name is exactly what a text check can compare.
"""
from __future__ import annotations

from pathlib import Path
import json
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
ARTIFACT_TOKEN = re.compile(r"CE-Decky-v\$\{version\}[^\"\s]*\.zip")


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _released_artifact_names() -> set[str]:
    """Every archive `scripts/package_plugin.py` writes into `artifacts/`."""
    source = (ROOT / "scripts" / "package_plugin.py").read_text(encoding="utf-8")
    names = set(re.findall(r'"artifacts"\s*/\s*f"([^"]+)"', source))
    assert names, "packaging script no longer declares an artifacts/ output"
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    return {name.replace("{VERSION}", version) for name in names}


def test_every_archive_the_release_names_is_one_the_build_produces() -> None:
    produced = _released_artifact_names()
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    named = {token.replace("${version}", version) for token in ARTIFACT_TOKEN.findall(_workflow())}
    assert named, "the release workflow no longer names any archive"
    missing = sorted(named - produced)
    assert not missing, f"release workflow names archives the build does not produce: {missing}"


def test_the_release_publishes_the_package_and_its_checksums() -> None:
    """A release that ships neither the ZIP nor SHA256SUMS is not a release."""
    workflow = _workflow()
    assert "artifacts/*.zip" in workflow
    assert "artifacts/SHA256SUMS" in workflow
    assert "--verify-tag" in workflow, "a release must be cut from the exact signed tag"


@pytest.mark.parametrize("step", ["scripts/check_release.py", "scripts/qa.py --profile release"])
def test_the_release_validates_before_it_publishes(step: str) -> None:
    """Tag/version/changelog agreement and the release gate both run first."""
    workflow = _workflow()
    assert step in workflow
    assert workflow.index(step) < workflow.index("gh release create")


def test_the_release_is_titled_by_its_tag() -> None:
    # Without a title GitHub shows one it makes up; v0.9.30 went out headed by
    # the subject of the CI commit its tag pointed at.
    create = _workflow().split("gh release create", 1)[1].split("\n\n", 1)[0]
    assert '--title "$GITHUB_REF_NAME"' in create


# The separator the update window reads a release body to. What follows is the
# commit list GitHub generates, which is not written for a user deciding to
# update, so the marker has to be where the notes end and nowhere else.
def test_the_release_body_puts_the_marker_between_the_notes_and_the_commit_list() -> None:
    from ce_decky.plugin_update import NOTES_END_MARKER

    workflow = _workflow()
    notes = workflow.index('cp "$notes" release-body.md')
    marker = workflow.index(NOTES_END_MARKER)
    generated = workflow.index("releases/generate-notes")
    assert notes < marker < generated
    assert workflow.count(NOTES_END_MARKER) == 1


NOTES = sorted((ROOT / "docs" / "release-notes").glob("v*.md"))


@pytest.mark.parametrize("path", NOTES, ids=[path.name for path in NOTES])
def test_every_release_notes_file_reads_in_the_update_window(path: Path) -> None:
    from ce_decky.plugin_update import NOTES_END_MARKER, release_notes

    text = path.read_text(encoding="utf-8")
    # Either would end the notes early: the marker itself, or a line the
    # window takes for the start of the generated list.
    assert NOTES_END_MARKER not in text
    assert not any(line.strip().startswith(("## What's Changed", "**Full Changelog**")) for line in text.splitlines())
    # Assembled as the release workflow assembles it.
    body = f"{text}\n{NOTES_END_MARKER}\n\n## What's Changed\n* a commit by @someone\n"
    notes, cut = release_notes(body)
    assert notes and not cut
    assert "## Install" in text and "## Install" not in notes
    # The opening line is the reason to update, which the window shows alone
    # under the two versions. The rule and the window both arrived with 0.9.33,
    # and releases published before it keep the bodies they were published with.
    from ce_decky.plugin_update import parse_version

    if parse_version("0.9.33") <= parse_version(path.stem[1:]):
        assert not notes.splitlines()[0].startswith(("#", "-", "*", "+"))
    assert "commit by @someone" not in notes
