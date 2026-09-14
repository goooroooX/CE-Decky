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
