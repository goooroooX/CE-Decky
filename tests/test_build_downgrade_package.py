import sys

import pytest

from scripts import build_downgrade_package as builder


@pytest.mark.parametrize(("current", "published", "expected"), [
    # A tree one version ahead of the release: one below the release, which is
    # the version it is offered to. One below the tree was the release itself.
    ("0.9.33", "0.9.32", "0.9.31"),
    ("0.9.33", "0.9.33", "0.9.32"),
    # No tag to go on, or one this tree is behind: below the tree, as before.
    ("0.9.33", None, "0.9.32"),
    ("0.9.33", "0.9.40", "0.9.32"),
])
def test_the_default_is_offered_the_published_release(current, published, expected):
    assert builder._default_target(current, published) == expected


def test_a_stale_bundle_is_refused_before_anything_is_rewritten(monkeypatch):
    # The stamp is re-taken for the rewritten tree, so packaging a bundle built
    # before the last frontend change would vouch for it as this tree's.
    monkeypatch.setattr(builder, "_current_version", lambda: "0.9.33")
    monkeypatch.setattr(builder, "_published_version", lambda: "0.9.32")
    monkeypatch.setattr(builder, "_tree_is_clean", lambda: True)
    monkeypatch.setattr(builder, "frontend_is_current", lambda: False)

    def rewrite(*_args):
        raise AssertionError("rewrote the version files for a stale bundle")

    monkeypatch.setattr(builder, "_rewrite", rewrite)
    monkeypatch.setattr(sys, "argv", ["build_downgrade_package.py"])
    with pytest.raises(SystemExit, match="not built from this tree's frontend sources"):
        builder.main()
