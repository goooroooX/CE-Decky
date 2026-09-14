"""Keep the exact head CI validated readable by a review that cannot clone it.

The snapshot exists so that an exact-head review is possible at all, and every
part of how it is built is load-bearing while looking like something a tidy-up
could simplify. Each case here is one of those simplifications.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo

WORKFLOW = """
permissions:
  contents: read
jobs:
  validate:
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 2
      - name: Pack exact source snapshot
        env:
          SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}
        run: git archive --format=zip --output=out.zip "$SOURCE_SHA"
      - uses: actions/upload-artifact@v4
        with:
          retention-days: 3
          if-no-files-found: error
      - uses: actions/setup-python@v7
        with:
          cache-dependency-path: requirements-dev.txt
      - run: python -m pip install -r requirements-dev.txt
      - run: python scripts/qa.py --profile release
"""


def _workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    path = tmp_path / ".github" / "workflows"
    path.mkdir(parents=True)
    (path / "ci.yml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)


def test_the_repository_publishes_the_head_it_validated():
    verify_repo._verify_ci_source_snapshot()


def test_a_workflow_that_keeps_every_part_of_it_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _workflow(tmp_path, monkeypatch, WORKFLOW)

    verify_repo._verify_ci_source_snapshot()


@pytest.mark.parametrize("removed, expected", [
    # The merge commit a pull request checks out is not the head anybody
    # reviewed, and it exists nowhere else.
    ("${{ github.event.pull_request.head.sha || github.sha }}", "pull request head"),
    # Shallower leaves the head out of the object store entirely.
    ("fetch-depth: 2", "too shallow"),
    # An empty artifact reads as a snapshot until somebody opens it.
    ("if-no-files-found: error", "upload empty"),
    # Expiry is the whole of the cleanup.
    ("retention-days: 3", "no expiry"),
])
def test_losing_one_part_of_it_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: str, expected: str):
    _workflow(tmp_path, monkeypatch, WORKFLOW.replace(removed, ""))

    with pytest.raises(SystemExit, match=expected):
        verify_repo._verify_ci_source_snapshot()


def test_packing_after_the_dependency_restore_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The run somebody wants the source of is the one that failed, and by then
    # a snapshot packed after the restore was never built.
    reordered = WORKFLOW.replace(
        '        run: git archive --format=zip --output=out.zip "$SOURCE_SHA"',
        "        run: echo later",
    ).replace(
        "      - run: python -m pip install -r requirements-dev.txt",
        "      - run: python -m pip install -r requirements-dev.txt\n"
        '      - run: git archive --format=zip --output=out.zip "$SOURCE_SHA"',
    )
    _workflow(tmp_path, monkeypatch, reordered)

    with pytest.raises(SystemExit, match="after the dependency restore"):
        verify_repo._verify_ci_source_snapshot()


def test_permission_to_delete_artifacts_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _workflow(tmp_path, monkeypatch, WORKFLOW.replace("  contents: read", "  contents: read\n  actions: write"))

    with pytest.raises(SystemExit, match="deletion permission"):
        verify_repo._verify_ci_source_snapshot()
