from pathlib import Path
import subprocess
import zipfile

import pytest

from ce_decky.archive_import import ArchiveImportError, _parse_7z_slt, extract_ct_member, inspect_archive, sevenzip_opens_rar
from ce_decky.table_store import TableStore

CT = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries><CheatEntry><ID>7</ID><Description>"HP"</Description><VariableType>4 Bytes</VariableType></CheatEntry></CheatEntries></CheatTable>'


def _zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def test_zip_inspection_and_single_ct_import(tmp_path: Path):
    source = tmp_path / "tables.zip"
    _zip(source, {"docs/readme.txt": b"x", "game/main.CT": CT})
    inspection = inspect_archive(source)
    assert inspection.format == "zip"
    assert [item.path for item in inspection.members] == ["game/main.CT"]
    artifact = TableStore(tmp_path / "store").import_selection(str(source))
    assert artifact.filename == "main.CT"
    assert artifact.entry_count == 1
    assert Path(artifact.blob_path).read_bytes() == CT


def test_multiple_ct_members_require_explicit_selection(tmp_path: Path):
    source = tmp_path / "tables.zip"
    _zip(source, {"a.CT": CT, "nested/b.CT": CT.replace(b"HP", b"MP")})
    store = TableStore(tmp_path / "store")
    with pytest.raises(ValueError, match="multiple"):
        store.import_selection(str(source))
    artifact = store.import_selection(str(source), member_path="nested/b.CT")
    assert artifact.filename == "b.CT"


def test_zip_traversal_and_symlink_are_rejected(tmp_path: Path):
    traversal = tmp_path / "bad.zip"
    _zip(traversal, {"../evil.CT": CT})
    with pytest.raises(ArchiveImportError, match="unsafe"):
        inspect_archive(traversal)

    symlink = tmp_path / "link.zip"
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("link.CT")
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, "target")
    with pytest.raises(ArchiveImportError, match="symlink"):
        inspect_archive(symlink)


def test_duplicate_normalized_paths_are_rejected(tmp_path: Path):
    source = tmp_path / "dup.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("dir\\game.CT", CT)
        archive.writestr("dir/game.CT", CT)
    with pytest.raises(ArchiveImportError, match="duplicate"):
        inspect_archive(source)


def test_archive_member_size_is_bounded_before_extraction(tmp_path: Path):
    source = tmp_path / "huge.zip"
    with zipfile.ZipFile(source, "w") as archive:
        info = zipfile.ZipInfo("huge.CT")
        # Creating a sparse metadata-only impossible with zipfile, so patch central directory
        # behavior is not worth coupling a test to. Instead assert the common zero-byte gate.
        archive.writestr(info, b"")
    with pytest.raises(ArchiveImportError, match="between 1"):
        inspect_archive(source)


def test_7z_slt_parser_is_locale_independent_key_value_parser():
    text = """Path = tables/main.CT\nSize = 123\nPacked Size = 80\nFolder = -\nEncrypted = -\n\nPath = folder\nSize = 0\nFolder = +\n\n"""
    records = _parse_7z_slt(text)
    assert records[0]["Path"] == "tables/main.CT"
    assert records[0]["Packed Size"] == "80"
    assert records[1]["Folder"] == "+"


def _fake_7z(
    path: Path, *, extract: bytes = CT, declared_size: int | None = None, rar: bool = True,
) -> Path:
    declared = len(extract) if declared_size is None else declared_size
    script = path / "fake7z"
    payload = repr(extract)
    # `i` is how the plugin asks whether this build has a RAR handler, and a
    # build without one prints the same table with no row for it, exiting 0 all
    # the same, which is why the answer is read out of the listing.
    formats = (
        " 0  ...F..................  Rar      rar r00       R a r ! 1A 07 00\n" if rar else ""
    )
    script.write_text(
        f"""#!/usr/bin/env python3
import sys
if len(sys.argv) > 1 and sys.argv[1] == 'i':
    sys.stdout.write('Formats:\\n 0 C...F..  7z       7z\\n{formats.rstrip()}\\n')
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == 'l':
    sys.stdout.write('Path = nested/main.CT\\nSize = {declared}\\nPacked Size = 80\\nFolder = -\\nEncrypted = -\\n\\n')
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == 'x':
    sys.stdout.buffer.write({payload})
    raise SystemExit(0)
sys.stderr.write('unexpected argv: ' + repr(sys.argv))
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_7z_adapter_lists_and_extracts_only_selected_ct_via_stdout(tmp_path: Path):
    source = tmp_path / "tables.7z"
    source.write_bytes(b"fake archive bytes")
    sevenzip = _fake_7z(tmp_path)
    inspection = inspect_archive(source, str(sevenzip))
    assert inspection.format == "7z"
    assert [(item.path, item.size, item.encrypted) for item in inspection.members] == [
        ("nested/main.CT", len(CT), False),
    ]
    artifact = TableStore(tmp_path / "store").import_selection(
        str(source), member_path="nested/main.CT", sevenzip=str(sevenzip)
    )
    assert Path(artifact.blob_path).read_bytes() == CT


def test_a_rar_is_read_through_the_same_7zip_contract_and_labelled_as_itself(tmp_path: Path):
    """The sources publish tables as `.rar`, and it opens the same way `.7z` does.

    One game's only table at one source was served as `.rar` and the row was
    dropped before the user saw it; on another search five of that source's six
    rows went the same way. What differs from `.7z` is the handler, not the
    contract: the same `l -slt` listing and the same `x -so` transfer.
    """
    source = tmp_path / "tables.rar"
    source.write_bytes(b"fake archive bytes")
    sevenzip = _fake_7z(tmp_path)
    inspection = inspect_archive(source, str(sevenzip))
    # Labelled as what it is, because the password rule is keyed off the format
    # and an encrypted rar is as unopenable as an encrypted 7z.
    assert inspection.format == "rar"
    assert [(item.path, item.format) for item in inspection.members] == [("nested/main.CT", "rar")]
    artifact = TableStore(tmp_path / "store").import_selection(
        str(source), member_path="nested/main.CT", sevenzip=str(sevenzip)
    )
    assert Path(artifact.blob_path).read_bytes() == CT


def test_the_rar_handler_is_asked_once_for_one_binary(tmp_path: Path):
    # One import asks from the inspection and again from the extraction, and the
    # extraction lists the archive a second time on top. Asking the binary every
    # time ran 7-Zip four times for one file.
    from ce_decky.archive_import import _RAR_SUPPORT

    _RAR_SUPPORT.clear()
    sevenzip = _fake_7z(tmp_path)
    assert sevenzip_opens_rar(str(sevenzip)) is True
    assert len(_RAR_SUPPORT) == 1
    # The same observed binary reuses its answer.
    assert sevenzip_opens_rar(str(sevenzip)) is True
    assert len(_RAR_SUPPORT) == 1
    # A path whose binary disappeared is no longer the binary that answered.
    sevenzip.unlink()
    assert sevenzip_opens_rar(str(sevenzip)) is False
    _RAR_SUPPORT.clear()


def test_replacing_7zip_in_place_invalidates_its_rar_answer(tmp_path: Path):
    """Package updates keep the path while replacing the executable.

    The first cache used the path as the identity, so installing a full 7-Zip
    over a reduced build, or the reverse, left search using the old capability
    until the whole plugin backend was reloaded.
    """
    from ce_decky.archive_import import _RAR_SUPPORT

    _RAR_SUPPORT.clear()
    sevenzip = _fake_7z(tmp_path, rar=True)
    assert sevenzip_opens_rar(str(sevenzip)) is True

    _fake_7z(tmp_path, rar=False)
    assert sevenzip_opens_rar(str(sevenzip)) is False
    assert len(_RAR_SUPPORT) == 1
    _RAR_SUPPORT.clear()


def test_a_failed_rar_probe_is_not_misreported_as_a_missing_handler(tmp_path: Path, monkeypatch):
    """Only a successful format listing can say that RAR is absent.

    Search deliberately keeps RAR rows after an advisory probe failure, but the
    adapter turned every execution failure and nonzero exit into `False`, which
    made that fail-open boundary unreachable and hid the rows for that search.
    """
    import ce_decky.archive_import as archive_import

    sevenzip = _fake_7z(tmp_path)
    archive_import._RAR_SUPPORT.clear()
    monkeypatch.setattr(
        archive_import,
        "_run_7z",
        lambda _args: subprocess.CompletedProcess(_args, 2, b"", b"loader failed"),
    )

    with pytest.raises(ArchiveImportError, match="capability probe failed"):
        sevenzip_opens_rar(str(sevenzip))
    assert archive_import._RAR_SUPPORT == {}


def test_a_7zip_with_no_rar_handler_refuses_the_archive_by_name(tmp_path: Path):
    # `7z` carries the handler and the reduced `7za`/`7zr` builds do not, and
    # they answer `i` with a successful exit and a listing that simply lacks the
    # row, so nothing but the listing can tell the two apart.
    source = tmp_path / "tables.rar"
    source.write_bytes(b"fake archive bytes")
    from ce_decky.archive_import import _RAR_SUPPORT

    _RAR_SUPPORT.clear()
    reduced = _fake_7z(tmp_path, rar=False)
    assert sevenzip_opens_rar(str(reduced)) is False
    with pytest.raises(ArchiveImportError, match="no RAR handler"):
        inspect_archive(source, str(reduced))
    # And the same refusal at the extraction, which is reachable on its own.
    with pytest.raises(ArchiveImportError, match="no RAR handler"):
        extract_ct_member(source, "nested/main.CT", tmp_path / "out.CT", sevenzip=str(reduced))
    _RAR_SUPPORT.clear()
    assert sevenzip_opens_rar(str(_fake_7z(tmp_path))) is True
    assert sevenzip_opens_rar(None) is False
    _RAR_SUPPORT.clear()


def test_7z_extraction_size_mismatch_fails_closed(tmp_path: Path):
    source = tmp_path / "tables.7z"
    source.write_bytes(b"fake archive bytes")
    sevenzip = _fake_7z(tmp_path, declared_size=len(CT) + 1)
    with pytest.raises(ValueError, match="size does not match"):
        TableStore(tmp_path / "store").import_selection(
            str(source), member_path="nested/main.CT", sevenzip=str(sevenzip)
        )


def test_7z_stream_stops_when_output_exceeds_declared_size(tmp_path: Path):
    source = tmp_path / "tables.7z"
    source.write_bytes(b"fake archive bytes")
    sevenzip = _fake_7z(tmp_path, declared_size=len(CT) - 1)
    with pytest.raises(ValueError, match="exceeds preflight size"):
        TableStore(tmp_path / "store").import_selection(
            str(source), member_path="nested/main.CT", sevenzip=str(sevenzip)
        )


def test_7z_symlink_member_is_rejected_from_slt_metadata(tmp_path: Path):
    source = tmp_path / "tables.7z"
    source.write_bytes(b"fake archive bytes")
    script = tmp_path / "fake7z-link"
    script.write_text(
        """#!/usr/bin/env python3
import sys
if sys.argv[1] == 'l':
    sys.stdout.write('Path = nested/main.CT\\nSize = 10\\nFolder = -\\nAttributes = lrwxrwxrwx\\nSymbolic Link = ../real.CT\\n\\n')
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    with pytest.raises(ArchiveImportError, match="symlink"):
        inspect_archive(source, str(script))


def test_archive_rejects_case_unicode_and_control_character_path_ambiguity(tmp_path: Path):
    case = tmp_path / "case.zip"
    _zip(case, {"Game/Main.CT": CT, "game/main.ct": CT})
    with pytest.raises(ArchiveImportError, match="ambiguous"):
        inspect_archive(case)

    unicode_dup = tmp_path / "unicode.zip"
    composed = "caf\u00e9/main.CT"
    decomposed = "cafe\u0301/main.CT"
    _zip(unicode_dup, {composed: CT, decomposed: CT})
    with pytest.raises(ArchiveImportError, match="ambiguous"):
        inspect_archive(unicode_dup)

    controls = tmp_path / "control.zip"
    _zip(controls, {"bad\nname.CT": CT})
    with pytest.raises(ArchiveImportError, match="control characters"):
        inspect_archive(controls)

    bidi = tmp_path / "bidi.zip"
    _zip(bidi, {"safe\u202Eevil.CT": CT})
    with pytest.raises(ArchiveImportError, match="bidirectional"):
        inspect_archive(bidi)


def test_archive_rejects_windows_trailing_dot_space_collisions(tmp_path: Path):
    source = tmp_path / "windows-collision.zip"
    _zip(source, {"Game/Main.CT": CT, "game/main.CT. ": CT})
    with pytest.raises(ArchiveImportError, match="ambiguous"):
        inspect_archive(source)


def test_zip_password_validation_rejects_invalid_unicode_and_excessive_size(tmp_path: Path):
    source = tmp_path / "tables.zip"
    _zip(source, {"main.CT": CT})
    with pytest.raises(ValueError, match="valid Unicode"):
        extract_ct_member(source, "main.CT", tmp_path / "out.CT", password="\ud800")
    with pytest.raises(ArchiveImportError, match="password exceeds"):
        extract_ct_member(source, "main.CT", tmp_path / "out.CT", password="x" * 1025)



def test_archive_rejects_raw_dot_and_empty_path_segments_before_normalization(tmp_path: Path):
    for index, name in enumerate(("dir/./main.CT", "dir//main.CT")):
        source = tmp_path / f"ambiguous-{index}.zip"
        _zip(source, {name: CT})
        with pytest.raises(ArchiveImportError, match="unsafe archive member path"):
            inspect_archive(source)


def test_7z_metadata_must_be_strict_utf8_and_have_nonnegative_packed_size(tmp_path: Path):
    source = tmp_path / "tables.7z"
    source.write_bytes(b"fake archive bytes")

    invalid_utf8 = tmp_path / "fake7z-invalid-utf8"
    invalid_utf8.write_bytes(
        b"#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(b'Path = bad\\xff.CT\\n')\n"
    )
    invalid_utf8.chmod(0o755)
    with pytest.raises(ArchiveImportError, match="valid UTF-8"):
        inspect_archive(source, str(invalid_utf8))

    negative_packed = tmp_path / "fake7z-negative-packed"
    negative_packed.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "sys.stdout.write('Path = main.CT\\nSize = 10\\nPacked Size = -1\\nFolder = -\\nEncrypted = -\\n\\n')\n",
        encoding="utf-8",
    )
    negative_packed.chmod(0o755)
    with pytest.raises(ArchiveImportError, match="invalid 7z member size"):
        inspect_archive(source, str(negative_packed))
