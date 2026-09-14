"""What the Decky Store reads out of this plugin, checked where it is written.

The Store uploader takes the listing straight from the packaged manifest:
`.name`, `.author`, `.publish.description`, `.publish.image` and
`.publish.tags`. A field that is absent is not an error anywhere in that
pipeline. The CLI builds the ZIP regardless, the uploader posts `null`, and the
first time anyone finds out is when the listing is live and wrong. So the shape
is asserted here, and so are the two values that are easy to get wrong in a way
nothing downstream would notice.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))


def test_the_uploader_finds_every_field_it_reads():
    for field in ("name", "author", "flags", "api_version"):
        assert MANIFEST.get(field) is not None, field
    publish = MANIFEST["publish"]
    for field in ("tags", "description", "image"):
        assert publish.get(field), field


def test_the_plugin_asks_for_no_privilege_it_does_not_need():
    """`flags` carries `_root` for plugins that need it. This one never does."""
    assert MANIFEST["flags"] == []


def test_the_listing_reads_as_a_sentence():
    description = MANIFEST["publish"]["description"]
    assert description.endswith(".")
    assert "\n" not in description
    assert len(description) <= 200


def test_the_tags_are_the_established_ones():
    """An invented tag groups this plugin with nothing, which helps nobody."""
    assert MANIFEST["publish"]["tags"] == ["cheat", "trainer"]
    # `dnu` tells the Store workflow not to upload at all. It is a real tag and
    # it would silently do nothing except stop publication.
    assert "dnu" not in MANIFEST["publish"]["tags"]


def test_the_listing_image_is_a_committed_asset_served_over_https():
    """A broken image URL is found by a reviewer, not by any check we run."""
    image = MANIFEST["publish"]["image"]
    assert image.startswith("https://raw.githubusercontent.com/")
    relative = image.split("/main/", 1)[1]
    assert (ROOT / relative).is_file(), relative
