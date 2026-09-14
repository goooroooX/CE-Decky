"""Keep a published tree free of the device it was developed on.

The classes with a shape are checked by writing one of each. The class with no
shape - a game's title - is checked through the mechanism rather than through a
name: a digest of an invented phrase is put in the list, and what this asserts
is that the phrase is refused and that the refusal does not repeat it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_repo


def _tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "src").mkdir()
    monkeypatch.setattr(verify_repo, "ROOT", tmp_path)
    return tmp_path


def test_the_repository_is_sanitary():
    verify_repo._verify_public_sanitation()


# Built from halves rather than written out, because this file is scanned by the
# rules it is testing. A sample spelled out here would have to be allowed by
# name on the rule that catches it, and a sample that is allowed proves nothing;
# split, it is not the thing until the test writes it into a file of its own.
ADDRESS = "192.168." + "0.2"
ACCOUNT_AT_ADDRESS = "someone@" + ADDRESS
ACCOUNT_AT_HOST = "someone@" + "a-device.local"
REMOTE_ACCOUNT = "someone@" + "a-machine"
USER_DIRECTORY = "userdata/" + "12345678"
CAPTURE = "build/incidents/" + "panel-closed-20260912T2130"
TRAILER = "Co-Authored" + "-By: somebody <nobody@example.test>"


@pytest.mark.parametrize("carried", [
    f'const host = "{ADDRESS}";',
    f"// synced from {ACCOUNT_AT_ADDRESS}",
    f"// synced from {ACCOUNT_AT_HOST}",
    f'const store = "{USER_DIRECTORY}/config/shortcuts.vdf";',
    f"// see {CAPTURE}/",
    f"const message = `a commit message\n{TRAILER}`;",
])
def test_what_belongs_to_one_device_is_refused(carried: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _tree(tmp_path, monkeypatch)
    source = root / "src" / "example.ts"
    source.write_text(f"{carried}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    assert "src/example.ts:" in str(refused.value)

    source.write_text("const host = \"127.0.0.1\";\n", encoding="utf-8")
    verify_repo._verify_public_sanitation()


@pytest.mark.parametrize("name", ["plugin.json", "py_modules/ce_decky/data.json", ".github/workflows/ci.yml"])
def test_every_file_this_project_owns_is_read_not_just_its_source(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # A list of four source suffixes left a manifest, a workflow and a shipped
    # JSON unread, and a string moved into one of those is published exactly as
    # far as a string in a source file is.
    root = _tree(tmp_path, monkeypatch)
    carrier = root / name
    carrier.parent.mkdir(parents=True, exist_ok=True)
    carrier.write_text('{"note": "%s/config"}\n' % USER_DIRECTORY, encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    assert name in str(refused.value)


def test_an_account_handed_to_a_remote_command_is_refused_without_its_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # The shape this project's own helpers take. On its own `user@host` is far
    # too ordinary to look for - a pinned GitHub action and a password in a test
    # URL both read as one - so what is looked for is one handed to something
    # that connects.
    root = _tree(tmp_path, monkeypatch)
    source = root / "src" / "example.ts"
    source.write_text(f"// ssh {REMOTE_ACCOUNT} uptime\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    assert REMOTE_ACCOUNT in str(refused.value)

    # A pinned action and a credential in a URL are the same shape and are not
    # this, because nothing is connecting anywhere with them.
    source.write_text('// uses: actions/checkout@v7 and https://user@example.test/x\n', encoding="utf-8")
    verify_repo._verify_public_sanitation()


def test_a_file_that_is_not_text_is_skipped_rather_than_ending_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # Reading every file by exclusion is what catches a string moved into a
    # manifest or a workflow. The cost is meeting the occasional file that is
    # not text, and one of those is nothing to scan rather than a reason for the
    # whole check to end in a traceback.
    root = _tree(tmp_path, monkeypatch)
    (root / "src" / "blob.dat").write_bytes(b"\x00\x01\x02\xff not text")
    (root / "src" / "example.ts").write_text("const ok = 1;\n", encoding="utf-8")

    verify_repo._verify_public_sanitation()


def test_the_sanitizer_and_its_own_fixture_are_read_like_every_other_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # They were left out of their own scan, which put the one blind spot in the
    # two files least able to afford one: a real address or a removed title
    # pasted into either would have walked through the gate.
    phrase = "Kettle Moraine Overdrive"
    mark, digest = verify_repo.removed_name_entry(phrase)
    monkeypatch.setattr(verify_repo, "REMOVED_NAMES", ((mark, digest, "a game somebody removed"),))
    root = _tree(tmp_path, monkeypatch)
    for relative in ("scripts/verify_repo.py", "tests/test_verify_repo_sanitation.py"):
        carrier = root / relative
        carrier.parent.mkdir(parents=True, exist_ok=True)
        carrier.write_text(f"# measured against {phrase}\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    message = str(refused.value)
    assert "scripts/verify_repo.py names" in message
    assert "tests/test_verify_repo_sanitation.py names" in message


def test_a_payload_in_the_tree_is_refused_as_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The plugin ZIP already refuses these as members. This is the tree they are
    # packaged from, which is where one arrives first: a table copied in to
    # reproduce something, a Cheat Engine binary dropped beside the code.
    root = _tree(tmp_path, monkeypatch)
    payload = root / "src" / "Reproduction.CT"
    payload.write_bytes(b"<CheatTable/>")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    assert "src/Reproduction.CT is a payload" in str(refused.value)

    payload.unlink()
    verify_repo._verify_public_sanitation()


def test_a_table_filename_is_a_download_in_a_document_and_a_fixture_in_a_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # A document naming a `.CT` file is naming somebody's download; a test needs
    # one to have something to import. The plugin's own storage name passes
    # either way.
    root = _tree(tmp_path, monkeypatch)
    document = root / "README.md"
    document.write_text("Open `SomeGame_v3.CT` from the post.\n", encoding="utf-8")
    (root / "src" / "example.ts").write_text("const fixture = \"SomeGame_v3.CT\";\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    assert "README.md:1" in str(refused.value)
    assert "src/example.ts" not in str(refused.value)

    document.write_text("Stored as `table.CT` under its digest.\n", encoding="utf-8")
    verify_repo._verify_public_sanitation()


def test_a_name_taken_out_on_purpose_is_refused_without_being_restated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    phrase = "Kettle Moraine Overdrive"
    # Generated the way a maintainer generates it, rather than reproduced here:
    # the two values and the matcher have to normalize identically, and a test
    # that redoes the arithmetic cannot notice when they stop doing so.
    mark, digest = verify_repo.removed_name_entry(phrase)
    monkeypatch.setattr(verify_repo, "REMOVED_NAMES", ((mark, digest, "a game somebody removed"),))
    root = _tree(tmp_path, monkeypatch)
    source = root / "src" / "example.ts"
    source.write_text(f"// measured against {phrase} on the device\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    message = str(refused.value)
    assert "src/example.ts names a game somebody removed" in message
    assert digest in message
    # The whole point of holding a digest: the guard never spells out what it
    # was told to keep out.
    assert phrase not in message and phrase.lower() not in message.lower()


@pytest.mark.parametrize("spelling", [
    "Kettle Moraine Overdrive",
    "kettle moraine overdrive",
    "KETTLE MORAINE OVERDRIVE",
    "Kettle moraine OVERDRIVE",
    "kettle-moraine-overdrive",
    "kettle moraine\n    // overdrive",
])
def test_no_spelling_of_a_removed_name_walks_past_it(
    spelling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # Capitalisation used to be how a phrase became a candidate at all, which
    # made the lowercase spelling a way past the whole rule rather than a case
    # it happened to miss - and the name this guard was written for had existed
    # in lower case in this tree before. What separates words does not matter
    # either: a name wrapped across two comment lines is one name.
    phrase = "Kettle Moraine Overdrive"
    # Generated the way a maintainer generates it, rather than reproduced here:
    # the two values and the matcher have to normalize identically, and a test
    # that redoes the arithmetic cannot notice when they stop doing so.
    mark, digest = verify_repo.removed_name_entry(phrase)
    monkeypatch.setattr(verify_repo, "REMOVED_NAMES", ((mark, digest, "a game somebody removed"),))
    root = _tree(tmp_path, monkeypatch)
    source = root / "src" / "example.ts"
    source.write_text(f"// {spelling} here\n", encoding="utf-8")

    with pytest.raises(SystemExit) as refused:
        verify_repo._verify_public_sanitation()
    assert digest in str(refused.value)

    source.write_text("// a stand-in here\n", encoding="utf-8")
    verify_repo._verify_public_sanitation()
