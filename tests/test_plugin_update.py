from pathlib import Path
import json

import pytest

from ce_decky.plugin_update import (
    ARCHIVE_PREFIX,
    MAX_NOTES_BYTES,
    NOTES_END_MARKER,
    UpdateError,
    UpdateStateStore,
    digest_from_sums,
    parse_release,
    parse_version,
    release_notes,
    version_from_tag,
)


def _release(version: str = "0.9.28", **overrides):
    archive = f"{ARCHIVE_PREFIX}{version}.zip"
    payload = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": archive, "browser_download_url": f"https://github.com/goooroooX/CE-Decky/releases/download/v{version}/{archive}"},
            {"name": "SHA256SUMS", "browser_download_url": f"https://github.com/goooroooX/CE-Decky/releases/download/v{version}/SHA256SUMS"},
        ],
    }
    payload.update(overrides)
    return payload


def test_versions_order_numerically_and_put_a_prerelease_below_its_release():
    order = ["0.9.9", "0.9.10", "0.9.27", "0.9.28", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0"]
    parsed = [parse_version(value) for value in order]
    for lower, higher in zip(parsed, parsed[1:]):
        assert lower < higher
        assert not higher < lower
    same = parse_version("0.9.27")
    assert same <= parse_version("0.9.27")
    assert not same < parse_version("0.9.27")


def test_versions_and_tags_refuse_what_this_project_does_not_publish():
    for value in ["", "0.9", "0.9.27.1", "v0.9.27", "0.9.x", "1.0.0-", 27, None, "0.9.27 "]:
        with pytest.raises(UpdateError):
            parse_version(value)
    assert str(version_from_tag("v1.0.0")) == "1.0.0"
    for tag in ["1.0.0", "release-1.0.0", None]:
        with pytest.raises(UpdateError):
            version_from_tag(tag)


def test_a_newer_release_offers_both_of_its_assets():
    offer = parse_release(_release(), current_version="0.9.27")
    assert offer is not None
    assert offer.version == "0.9.28"
    assert offer.archive_name == "CE-Decky-v0.9.28.zip"
    assert offer.archive_url.endswith("/CE-Decky-v0.9.28.zip")
    assert offer.sums_url.endswith("/SHA256SUMS")
    assert offer.page_url.endswith("/releases/tag/v0.9.28")


def test_the_same_or_an_older_release_is_not_an_update():
    assert parse_release(_release("0.9.27"), current_version="0.9.27") is None
    assert parse_release(_release("0.9.26"), current_version="0.9.27") is None
    assert parse_release(_release("0.9.9"), current_version="0.9.10") is None


def test_a_release_missing_either_asset_or_serving_one_elsewhere_is_refused():
    only_archive = _release()
    only_archive["assets"] = only_archive["assets"][:1]
    with pytest.raises(UpdateError, match="SHA256SUMS"):
        parse_release(only_archive, current_version="0.9.27")

    only_sums = _release()
    only_sums["assets"] = only_sums["assets"][1:]
    with pytest.raises(UpdateError, match="CE-Decky-v0.9.28.zip"):
        parse_release(only_sums, current_version="0.9.27")

    elsewhere = _release()
    elsewhere["assets"][0]["browser_download_url"] = "https://cdn.example.net/CE-Decky-v0.9.28.zip"
    with pytest.raises(UpdateError, match="unexpected host"):
        parse_release(elsewhere, current_version="0.9.27")

    insecure = _release()
    insecure["assets"][1]["browser_download_url"] = "http://github.com/goooroooX/CE-Decky/releases/download/v0.9.28/SHA256SUMS"
    with pytest.raises(UpdateError, match="unexpected host"):
        parse_release(insecure, current_version="0.9.27")


def test_a_prerelease_a_draft_and_a_malformed_payload_are_refused():
    with pytest.raises(UpdateError, match="stable"):
        parse_release(_release(prerelease=True), current_version="0.9.27")
    with pytest.raises(UpdateError, match="stable"):
        parse_release(_release(draft=True), current_version="0.9.27")
    with pytest.raises(UpdateError, match="stable"):
        parse_release(_release("1.0.0-rc.1"), current_version="0.9.27")
    with pytest.raises(UpdateError):
        parse_release(["not", "an", "object"], current_version="0.9.27")
    with pytest.raises(UpdateError, match="assets"):
        parse_release(_release(assets={}), current_version="0.9.27")


def test_the_digest_comes_from_the_line_for_this_exact_archive():
    digest = "a" * 64
    body = f"{digest}  CE-Decky-v0.9.28.zip\n{'b' * 64}  other.zip\n".encode()
    assert digest_from_sums(body, "CE-Decky-v0.9.28.zip") == digest
    assert digest_from_sums(f"{digest} *CE-Decky-v0.9.28.zip\n".encode(), "CE-Decky-v0.9.28.zip") == digest
    with pytest.raises(UpdateError):
        digest_from_sums(b"", "CE-Decky-v0.9.28.zip")
    with pytest.raises(UpdateError):
        digest_from_sums(f"{digest}  CE-Decky-v0.9.29.zip\n".encode(), "CE-Decky-v0.9.28.zip")
    with pytest.raises(UpdateError):
        digest_from_sums(f"{digest}  x.zip\n{'c' * 64}  x.zip\n".encode(), "x.zip")
    with pytest.raises(UpdateError):
        digest_from_sums(b"\xff\xfe not text", "x.zip")


def test_update_state_reads_an_unreadable_record_as_never_checked(tmp_path: Path):
    path = tmp_path / "plugin-update.json"
    store = UpdateStateStore(path)
    assert store.load() == {}
    store.update(checked_at=123.0, latest_version="0.9.28")
    assert store.load()["latest_version"] == "0.9.28"
    path.write_text("{ not json")
    assert store.load() == {}
    path.write_text(json.dumps({"schema": 99, "latest_version": "9.9.9"}))
    assert store.load() == {}


_BODY = """# CE Decky 0.9.28

Lets more games start Cheat Engine.

## Fixes

- A cheat no longer switches itself on.‮
- **Load table & start CE** works with `proton-cachyos`.

## Install

Close any running game, then update from the CE Decky panel.

Requires Decky Loader 3.2.8+.
<!-- ce-decky:notes-end -->

## What's Changed
* Fix things by @someone in #12
"""


def test_the_notes_are_the_part_written_for_a_user():
    notes, truncated = release_notes(_BODY)
    assert truncated is False
    # The title and the manual route go, and so does the commit list.
    assert notes == (
        "Lets more games start Cheat Engine.\n\n## Fixes\n\n"
        "- A cheat no longer switches itself on.\n"
        "- **Load table & start CE** works with `proton-cachyos`."
    )
    # A bidirectional override is not carried into what is shown.
    assert "‮" not in notes


def test_a_body_without_the_marker_stops_at_the_list_github_generates():
    body = "Fixes a crash.\r\n\r\n## What's Changed\r\n* a commit\r\n**Full Changelog**: x"
    assert release_notes(body) == ("Fixes a crash.", False)
    assert release_notes("Fixes a crash.\n**Full Changelog**: x") == ("Fixes a crash.", False)


def test_notes_are_bounded_on_a_line_and_absent_ones_are_nothing():
    notes, truncated = release_notes("\n".join(f"- line {index:04}" for index in range(2000)))
    assert truncated is True
    assert len(notes.encode("utf-8")) <= MAX_NOTES_BYTES
    assert notes.splitlines()[-1].startswith("- line ")
    assert release_notes(None) == (None, False)
    assert release_notes(["not", "text"]) == (None, False)
    assert release_notes("# CE Decky 0.9.28\n\n## Install\n\nBy hand.") == (None, False)
    # The title goes after leading blank lines too.
    assert release_notes("\n\n# CE Decky 0.9.28\nFixes.") == ("Fixes.", False)
    # The bound is what the record is written with, so escapes count.
    quoted, cut = release_notes("\n".join('"' * 60 for _ in range(200)))
    assert cut is True and len(json.dumps(quoted, ensure_ascii=False).encode("utf-8")) <= MAX_NOTES_BYTES + 2


def test_an_offer_carries_its_notes():
    offer = parse_release(_release(body=_BODY), current_version="0.9.27")
    assert offer.notes.startswith("Lets more games start Cheat Engine.")
    assert parse_release(_release(), current_version="0.9.27").notes is None


def test_the_release_workflow_writes_the_marker_the_notes_are_read_to():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml").read_text()
    assert NOTES_END_MARKER in workflow
