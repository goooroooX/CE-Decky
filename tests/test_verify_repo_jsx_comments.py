"""Catch the comment that makes the pinned build write trailing whitespace.

A comment between two props is ordinary TypeScript and reads well where it is
written. The pinned Rollup build turns it into a line of `dist/index.js` that
ends in a space, and that bundle is tracked, so `git diff --check` refuses the
commit - at the end of a release run, after the backend suite, the component
suite, the build and the package have all been paid for. This rule is the same
finding in the first second, and `scripts/qa.py` gates the run on it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


BETWEEN_PROPS = """export function Row() {
  return (
    <PanelRow
      truncate
      // the comment that costs the bundle a trailing space
      scroll
    />
  );
}
"""

CLEAN = """export function Row() {
  // an ordinary comment in a function body
  const ordered = first < second ? "a" : "b";
  return (
    <div>
      {/* a comment among children, which the build is happy with */}
      <PanelRow
        onClick={() => {
          // inside an attribute's own expression, which is plain JavaScript
          act();
        }}
        description={`a ${ordered} template`}
        help="prose with a < and a > in it"
      />
    </div>
  );
}
"""


def test_this_repository_writes_no_comment_between_props():
    verify_repo._verify_no_comments_between_props()


def test_a_comment_between_two_props_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src" / "modals" / "Row.tsx"
    source.parent.mkdir(parents=True)
    source.write_text(BETWEEN_PROPS, encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_no_comments_between_props()
    assert "src/modals/Row.tsx:5" in str(refused.value)
    assert "above the element" in str(refused.value)


def test_every_other_place_a_comment_belongs_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src" / "modals" / "Row.tsx"
    source.parent.mkdir(parents=True)
    source.write_text(CLEAN, encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    verify_repo._verify_no_comments_between_props()


def test_a_block_comment_between_props_is_refused_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "src" / "Row.tsx"
    source.parent.mkdir(parents=True)
    source.write_text("<PanelRow truncate /* here */ scroll />\n", encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    with pytest.raises(SystemExit):
        verify_repo._verify_no_comments_between_props()


def test_an_unterminated_template_cannot_run_the_scan_past_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The scan walks quoted spans to keep a brace inside one from moving its
    # depth. A file whose last string never closes must end the scan rather
    # than read past it.
    source = tmp_path / "src" / "Row.tsx"
    source.parent.mkdir(parents=True)
    source.write_text("const broken = `never closed\n", encoding="utf-8")
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    verify_repo._verify_no_comments_between_props()


def test_a_plain_typescript_file_is_not_read_as_markup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The scan decides it is inside a tag from a `<` followed by a name, which
    # in a `.ts` file is also what an unspaced comparison looks like. Reading
    # one would leave every line after it looking like a prop, and the next
    # ordinary comment like a violation - and this rule stops the whole run.
    source = tmp_path / "src" / "uiModel.ts"
    source.parent.mkdir(parents=True)
    source.write_text(
        "export function pick(count: number, limit: number) {\n"
        "  if (count<limit) {\n"
        "    // an ordinary comment, in a file that cannot hold markup\n"
        "    return count;\n"
        "  }\n"
        "  return limit;\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)

    verify_repo._verify_no_comments_between_props()
