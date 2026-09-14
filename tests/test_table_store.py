from pathlib import Path

from contextlib import contextmanager
import json
import shutil

import pytest

from ce_decky.table_store import TABLE_METADATA_SCHEMA, TableContentError, TableStore


CT = b'''<?xml version="1.0" encoding="utf-8"?>\n<CheatTable CheatEngineTableVersion="45"><CheatEntries><CheatEntry><ID>1</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType></CheatEntry></CheatEntries></CheatTable>'''
CT_WITH_SCRIPTS = b'''<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45"><CheatEntries><CheatEntry><ID>1</ID><AssemblerScript>[ENABLE]\nnop\n[DISABLE]</AssemblerScript></CheatEntry></CheatEntries><LuaScript>return true</LuaScript></CheatTable>'''


def test_import_table_is_content_addressed_and_idempotent(tmp_path: Path):
    source = tmp_path / "a.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "store")
    first = store.import_ct(str(source))
    second = store.import_ct(str(source))
    assert first.sha256 == second.sha256
    assert Path(first.blob_path).read_bytes() == CT
    assert first.table_version == "45"
    assert first.entry_count == 1
    assert not first.has_lua
    assert not first.has_auto_assembler
    assert len(store.list_tables()) == 1


def test_lua_and_auto_assembler_are_classified_separately(tmp_path: Path):
    source = tmp_path / "scripts.CT"
    source.write_bytes(CT_WITH_SCRIPTS)
    artifact = TableStore(tmp_path / "store").import_ct(str(source))
    assert artifact.has_lua
    assert artifact.has_auto_assembler


def test_invalid_xml_is_rejected(tmp_path: Path):
    source = tmp_path / "bad.CT"
    source.write_text("not xml")
    store = TableStore(tmp_path / "store")
    try:
        store.import_ct(str(source))
    except ValueError as exc:
        assert "its XML is malformed" in str(exc)
    else:
        raise AssertionError("invalid table was accepted")


def test_dtd_entity_declarations_are_rejected(tmp_path: Path):
    source = tmp_path / "entity.CT"
    source.write_text('''<?xml version="1.0"?><!DOCTYPE CheatTable [<!ENTITY x "boom">]><CheatTable><Comments>&x;</Comments></CheatTable>''')
    try:
        TableStore(tmp_path / "store").import_ct(str(source))
    except ValueError as exc:
        assert "DTD/entity" in str(exc)
    else:
        raise AssertionError("DTD-bearing CT was accepted")


def test_corrupt_existing_blob_is_repaired_from_same_digest_source(tmp_path: Path):
    source = tmp_path / "a.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "store")
    first = store.import_ct(str(source))
    Path(first.blob_path).write_bytes(b"corrupt")
    second = store.import_ct(str(source))
    assert second.sha256 == first.sha256
    assert Path(second.blob_path).read_bytes() == CT
    assert store.list_tables()[0]["available"] is True


def test_import_validates_and_stores_single_staged_snapshot_when_source_changes(tmp_path: Path, monkeypatch):
    import ce_decky.table_store as module

    root = tmp_path / "store"
    source = tmp_path / "race.CT"
    original = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>'
    source.write_bytes(original)
    expected_sha = __import__("hashlib").sha256(original).hexdigest()
    real_inspect = module._inspect_xml_bytes

    def inspect_and_mutate(snapshot: bytes):
        source.write_bytes(b'<CheatTable><LuaScript>changed</LuaScript></CheatTable>')
        return real_inspect(snapshot)

    monkeypatch.setattr(module, "_inspect_xml_bytes", inspect_and_mutate)
    artifact = module.TableStore(root).import_ct(str(source))
    assert artifact.sha256 == expected_sha
    assert Path(artifact.blob_path).read_bytes() == original
    assert not artifact.has_lua


def test_lua_script_entry_and_embedded_files_are_executable_content(tmp_path: Path):
    source = tmp_path / "rich.CT"
    source.write_text(
        '''<?xml version="1.0"?><CheatTable CheatEngineTableVersion="45">'''
        '''<Files><File Encoding="Ascii85">payload</File></Files>'''
        '''<LuaScriptEntry Name="startup">return true</LuaScriptEntry>'''
        '''</CheatTable>'''
    )
    artifact = TableStore(tmp_path / "store").import_ct(str(source))
    assert artifact.has_lua
    assert artifact.has_embedded_files
    assert artifact.executable_content


def test_deleting_a_table_fails_towards_the_state_that_can_be_recovered(tmp_path: Path):
    """Which of the two halves goes first is decided by which failure survives.

    A record with no blob lists as a table whose file is missing, which the
    panel describes and the user can act on. A blob with no record is invisible
    to every route here: nothing can name it, reach it or remove it. So the
    bytes go first and the record last, and a delete that fails in between is
    one a second press can finish.
    """
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "Game.CT"
    source.write_bytes(CT)
    stored = store.import_ct(str(source), display_filename="Game.CT")
    blob = store.verified_blob(stored.sha256)

    # The record cannot be removed, after the bytes already have been.
    with pytest.raises(OSError):
        with _failing_unlink(store.meta_root / f"{stored.sha256}.json"):
            store.delete_table(stored.sha256)
    assert not blob.exists()
    # Still listed, and listed as what it now is, rather than vanished with its
    # bytes left behind it.
    listed = store.get_table(stored.sha256)
    assert listed["available"] is False
    # And the press that failed finishes it.
    assert store.delete_table(stored.sha256)["sha256"] == stored.sha256
    assert store.list_tables() == []


def test_deleting_a_table_never_follows_a_link_out_of_the_store(tmp_path: Path):
    """Every component of the path, not only the last one.

    Checking the leaf alone leaves each directory above it unchecked, and a
    digest directory that is itself a link contains a perfectly ordinary file:
    the leaf is not a link, the file test follows the directory and says yes,
    and the unlink lands outside this store on something the user owns.
    """
    def stored_table(name: str) -> tuple[str, Path]:
        source = tmp_path / name
        source.write_bytes(CT + f"<!--{name}-->".encode())
        artifact = store.import_ct(str(source), display_filename=name)
        return artifact.sha256, (
            tmp_path / "tables" / "sha256" / artifact.sha256[:2] / artifact.sha256 / "table.CT"
        )

    store = TableStore(tmp_path / "tables")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "table.CT").write_bytes(CT)

    # The final entry is a link: the link goes, its target stays.
    linked_sha, linked_blob = stored_table("Linked.CT")
    linked_blob.unlink()
    linked_blob.symlink_to(outside / "table.CT")
    store.delete_table(linked_sha)
    assert not linked_blob.is_symlink()
    assert (outside / "table.CT").is_file()

    # The digest directory is a link, and the file inside it is an ordinary
    # file the user owns. Nothing here may be unlinked, and refusing leaves the
    # record so the table can still be named.
    (outside / "table.CT").write_bytes(CT)
    dir_sha, dir_blob = stored_table("Dir.CT")
    digest_dir = dir_blob.parent
    dir_blob.unlink()
    digest_dir.rmdir()
    digest_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="resolves through a symlink"):
        store.delete_table(dir_sha)
    assert (outside / "table.CT").is_file()
    assert store.get_table(dir_sha)["sha256"] == dir_sha

    # And the shard above it, the same answer.
    digest_dir.unlink()
    shard_sha, shard_blob = stored_table("Shard.CT")
    shard = shard_blob.parent.parent
    shutil.rmtree(shard)
    shard.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="resolves through a symlink"):
        store.delete_table(shard_sha)
    assert (outside / "table.CT").is_file()


def test_deleting_a_table_whose_bytes_are_already_gone_finishes_the_record(tmp_path: Path):
    """A removal that got part way is finished, not refused."""
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "Game.CT"
    source.write_bytes(CT)
    stored = store.import_ct(str(source), display_filename="Game.CT")
    shutil.rmtree(tmp_path / "tables" / "sha256")

    assert store.delete_table(stored.sha256)["sha256"] == stored.sha256
    assert store.list_tables() == []


@contextmanager
def _failing_unlink(path: Path):
    """Make exactly one path refuse to be unlinked, for the window of a call."""
    original = Path.unlink

    def guarded(self: Path, *args, **kwargs):
        if self == path:
            raise OSError("read-only file system")
        return original(self, *args, **kwargs)

    Path.unlink = guarded  # type: ignore[method-assign]
    try:
        yield
    finally:
        Path.unlink = original  # type: ignore[method-assign]


def test_a_table_records_when_it_first_arrived_on_this_device(tmp_path: Path):
    """A local import had no date of any kind, so half a stored list was dated.

    A downloaded table carries `retrieved_at` in the origin its acquisition
    wrote. A table opened from a file has no origin at all, and the screen that
    lists what is stored is where a user decides which copy to keep.

    Arrival, not import: the same file opened a second time is the same bytes
    that have been here since the first time, so the stamp is carried forward
    rather than moved to today.
    """
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "Game.CT"
    source.write_bytes(CT)
    first = store.import_ct(str(source), display_filename="Game.CT")
    stamp = store.get_table(first.sha256)["imported_at"]
    assert isinstance(stamp, str) and stamp.endswith("Z")

    again = store.import_ct(str(source), display_filename="Game.CT")
    assert again.sha256 == first.sha256
    assert store.get_table(first.sha256)["imported_at"] == stamp

    # And the marker on disk says what the record actually is, through every
    # rewrite. `add_origin` stamped it back to the schema that has no arrival
    # field while writing one, which the reader tolerates and a migration would
    # not: read the raw file, because the normalizer repairs it in memory.
    store.add_origin(first.sha256, {
        "provider": "fearless", "artifact_id": "a", "topic_id": "t",
        "source_page": "https://example.invalid/p", "original_filename": "Game.CT",
        "retrieved_at": "2026-09-01T10:30:00Z", "advertised_sha256": None,
    })
    raw = json.loads((store.meta_root / f"{first.sha256}.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == TABLE_METADATA_SCHEMA
    assert raw["imported_at"] == stamp


def test_legacy_metadata_is_normalized_without_execution_assumptions(tmp_path: Path):
    import json
    import hashlib

    store = TableStore(tmp_path / "store")
    digest = hashlib.sha256(CT).hexdigest()
    blob = store.blob_root / digest[:2] / digest / "table.CT"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(CT)
    store.meta_root.mkdir(parents=True)
    (store.meta_root / f"{digest}.json").write_text(json.dumps({
        "sha256": digest,
        "filename": "legacy.CT",
        "size": len(CT),
        "table_version": "45",
        "entry_count": 1,
    }))
    item = store.list_tables()[0]
    assert item["schema_version"] == 3
    assert item["origins"] == []
    # A record written before arrival was tracked has no date and is not given
    # one: when the bytes were written is not when the user chose them.
    assert item["imported_at"] is None
    assert item["has_lua"] is False
    assert item["has_auto_assembler"] is False
    assert item["has_embedded_files"] is False
    assert item["executable_content"] is False
    assert item["available"] is True


def test_verified_blob_detects_same_size_tamper(tmp_path: Path):
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "game.CT"
    source.write_text('<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>', encoding="utf-8")
    artifact = store.import_ct(str(source))
    verified = store.verified_blob(artifact.sha256)
    assert verified == Path(artifact.blob_path)

    original = verified.read_bytes()
    replacement = (b"X" if original[:1] != b"X" else b"Y") + original[1:]
    assert len(replacement) == len(original)
    verified.write_bytes(replacement)
    with pytest.raises(ValueError, match="SHA-256"):
        store.verified_blob(artifact.sha256)


def test_verified_blob_rejects_invalid_digest(tmp_path: Path):
    store = TableStore(tmp_path / "tables")
    with pytest.raises(ValueError, match="SHA-256"):
        store.verified_blob("../not-a-digest")


def test_import_replaces_managed_blob_symlink_with_regular_file(tmp_path: Path):
    import hashlib

    source = tmp_path / "a.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "store")
    digest = hashlib.sha256(CT).hexdigest()
    blob = store.blob_root / digest[:2] / digest / "table.CT"
    blob.parent.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "external.CT"
    external.write_bytes(CT)
    blob.symlink_to(external)

    artifact = store.import_ct(str(source))
    assert artifact.sha256 == digest
    assert not blob.is_symlink()
    assert blob.is_file()
    assert blob.read_bytes() == CT
    assert external.read_bytes() == CT


def test_archive_import_rejects_source_replacement_race(tmp_path: Path, monkeypatch):
    import zipfile
    import ce_decky.table_store as module

    source = tmp_path / "tables.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("main.CT", CT)
    store = module.TableStore(tmp_path / "store")
    real_extract = module.extract_ct_member

    def extract_then_replace(*args, **kwargs):
        real_extract(*args, **kwargs)
        replacement = tmp_path / "replacement.zip"
        with zipfile.ZipFile(replacement, "w") as archive:
            archive.writestr("main.CT", CT_WITH_SCRIPTS)
        replacement.replace(source)

    monkeypatch.setattr(module, "extract_ct_member", extract_then_replace)
    with pytest.raises(ValueError, match="changed during import"):
        store.import_selection(str(source), member_path="main.CT")
    assert store.list_tables() == []


def test_table_metadata_fails_closed_on_type_confusion_and_unknown_fields(tmp_path: Path):
    import hashlib, json
    store = TableStore(tmp_path / "store")
    digest = hashlib.sha256(CT).hexdigest()
    blob = store.blob_root / digest[:2] / digest / "table.CT"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(CT)
    store.meta_root.mkdir(parents=True)
    meta = store.meta_root / f"{digest}.json"
    base = {
        "schema_version": 1,
        "sha256": digest,
        "filename": "game.CT",
        "size": len(CT),
        "table_version": "45",
        "has_lua": False,
        "has_auto_assembler": False,
        "has_embedded_files": False,
        "executable_content": False,
        "entry_count": 1,
        "blob_path": str(blob),
    }
    for patch in (
        {"size": True},
        {"has_lua": 1},
        {"entry_count": True},
        {"filename": "bad\nname.CT"},
        {"unknown": "x"},
    ):
        raw = dict(base)
        raw.update(patch)
        meta.write_text(json.dumps(raw))
        assert store.list_tables() == []
        with pytest.raises(ValueError):
            store.get_table(digest)


def test_table_metadata_never_trusts_false_executable_content_over_script_flags(tmp_path: Path):
    import hashlib, json
    store = TableStore(tmp_path / "store")
    digest = hashlib.sha256(CT).hexdigest()
    blob = store.blob_root / digest[:2] / digest / "table.CT"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(CT)
    store.meta_root.mkdir(parents=True)
    (store.meta_root / f"{digest}.json").write_text(json.dumps({
        "sha256": digest,
        "filename": "game.CT",
        "size": len(CT),
        "table_version": "45",
        "has_lua": True,
        "has_auto_assembler": False,
        "has_embedded_files": False,
        "executable_content": False,
        "entry_count": 1,
    }))
    assert store.get_table(digest)["executable_content"] is True


def test_direct_ct_import_rejects_control_character_filename(tmp_path: Path):
    source = tmp_path / "bad\nname.CT"
    source.write_bytes(CT)
    with pytest.raises(ValueError, match="filename"):
        TableStore(tmp_path / "store").import_ct(str(source))


def test_table_store_rejects_internal_directory_symlink_redirection(tmp_path: Path):
    root = tmp_path / "tables"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "metadata").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "safe.CT"
    source.write_text('<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>')
    store = TableStore(root)
    with pytest.raises(ValueError, match="symlink"):
        store.import_ct(str(source))
    assert list(outside.iterdir()) == []


def test_table_store_rejects_staging_symlink_redirection(tmp_path: Path):
    root = tmp_path / "tables"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".staging").symlink_to(outside, target_is_directory=True)
    source = tmp_path / "safe.CT"
    source.write_text('<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>')
    with pytest.raises(ValueError, match="symlink"):
        TableStore(root).import_ct(str(source))
    assert list(outside.iterdir()) == []


def test_verified_blob_rejects_intermediate_symlink_redirection(tmp_path: Path):
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "safe.CT"
    source.write_text('<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>')
    artifact = store.import_ct(str(source))
    digest_dir = store.blob_root / artifact.sha256[:2] / artifact.sha256
    outside = tmp_path / "outside"
    outside.mkdir()
    replacement = outside / "table.CT"
    replacement.write_bytes(Path(artifact.blob_path).read_bytes())
    for child in digest_dir.iterdir():
        child.unlink()
    digest_dir.rmdir()
    digest_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        store.verified_blob(artifact.sha256)


def test_promotion_metadata_and_digest_share_one_snapshot(tmp_path: Path, monkeypatch):
    import ce_decky.table_store as table_store
    store = table_store.TableStore(tmp_path / "store")
    store._ensure_roots(store.staging_root)
    staged = store.staging_root / "race.CT"
    original = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries/></CheatTable>'
    staged.write_bytes(original)
    real_inspect = table_store._inspect_xml_bytes

    def mutate_after_snapshot(data: bytes):
        result = real_inspect(data)
        staged.write_bytes(b'<CheatTable><LuaScript>changed()</LuaScript></CheatTable>')
        return result

    monkeypatch.setattr(table_store, "_inspect_xml_bytes", mutate_after_snapshot)
    with pytest.raises(table_store.DurabilityUnknownError, match="metadata was published") as failure:
        store._validate_and_promote(staged, "race.CT")
    assert "promotion failed integrity" in str(failure.value.__cause__)
    rows = store.list_tables()
    assert len(rows) == 1 and rows[0]["available"] is False


def test_verified_blob_hashing_rejects_oversize_drift_and_non_string_digest(monkeypatch, tmp_path: Path):
    import ce_decky.table_store as table_store

    source = tmp_path / "source.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "store")
    artifact = store.import_ct(str(source))
    blob = Path(artifact.blob_path)

    with pytest.raises(ValueError, match="must be a string"):
        store.verified_blob(123)  # type: ignore[arg-type]

    original_limit = table_store.MAX_CT_BYTES
    monkeypatch.setattr(table_store, "MAX_CT_BYTES", max(1, len(CT) - 1))
    with pytest.raises(ValueError, match="size"):
        store.verified_blob(artifact.sha256)
    monkeypatch.setattr(table_store, "MAX_CT_BYTES", original_limit)


def test_table_catalog_enumeration_is_bounded_before_materializing_entries(monkeypatch, tmp_path: Path):
    import ce_decky.table_store as table_store
    store = TableStore(tmp_path / "tables")
    store.meta_root.mkdir(parents=True)
    (store.meta_root / "junk-a").write_text("x", encoding="utf-8")
    (store.meta_root / "junk-b").write_text("x", encoding="utf-8")
    monkeypatch.setattr(table_store, "MAX_TABLE_METADATA_ENTRIES", 1)
    with pytest.raises(ValueError, match="entry limit"):
        store.list_tables()


def test_inspect_source_rejects_a_damaged_ct_before_it_looks_importable(tmp_path: Path):
    # A real Playground table closed one CheatEntry it never opened. The download
    # matched its advertised SHA-256, so only structural inspection can tell the
    # user the table itself is unusable rather than the transfer.
    source = tmp_path / "damaged.CT"
    source.write_bytes(
        b'<?xml version="1.0" encoding="utf-8"?>\r\n'
        b'<CheatTable CheatEngineTableVersion="45">\r\n  <CheatEntries>\r\n'
        b'    <CheatEntry>\r\n      <ID>1</ID>\r\n    </CheatEntry>// comment\r\n'
        b'      <ID>2</ID>\r\n    </CheatEntry>\r\n  </CheatEntries>\r\n</CheatTable>\r\n'
    )
    store = TableStore(tmp_path / "store")
    with pytest.raises(TableContentError, match="its XML is malformed"):
        store.inspect_source(str(source))
    with pytest.raises(TableContentError, match="its XML is malformed"):
        store.import_ct(str(source))


def test_inspect_source_reports_a_valid_ct_as_one_importable_member(tmp_path: Path):
    source = tmp_path / "ok.CT"
    source.write_bytes(CT)
    inspection = TableStore(tmp_path / "store").inspect_source(str(source))
    assert inspection["format"] == "ct"
    assert [member["path"] for member in inspection["members"]] == ["ok.CT"]


def test_origin_version_is_optional_and_bounded(tmp_path: Path):
    """The advertised release is display text from a provider, so it is bounded."""
    source = tmp_path / "Example.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "tables")
    artifact = store.import_ct(str(source))
    origin = {
        "provider": "fearless", "artifact_id": "topic-1:attachment-2", "topic_id": "1",
        "source_page": "https://fearlessrevolution.com/viewtopic.php?t=1",
        "original_filename": "Example.CT", "retrieved_at": "2026-08-16T00:00:00Z",
        "advertised_sha256": None,
    }
    store.add_origin(artifact.sha256, {**origin, "version": "1.0.6"})
    assert store.get_table(artifact.sha256)["origins"][-1]["version"] == "1.0.6"
    # An origin recorded before the release was tracked simply has none.
    store.add_origin(artifact.sha256, origin)
    assert "version" not in store.get_table(artifact.sha256)["origins"][-1]
    for rejected in ("", "   ", "1.0\n6", "9" * 257):
        with pytest.raises(ValueError, match="version"):
            store.add_origin(artifact.sha256, {**origin, "version": rejected})


def test_an_unreadable_origin_is_dropped_and_the_table_stays_listed(tmp_path: Path):
    """Provenance is description; the verified bytes are the artifact."""
    import json

    source = tmp_path / "Example.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "tables")
    artifact = store.import_ct(str(source))
    good = {
        "provider": "fearless", "artifact_id": "topic-1:attachment-2", "topic_id": "1",
        "source_page": "https://fearlessrevolution.com/viewtopic.php?t=1",
        "original_filename": "Example.CT", "retrieved_at": "2026-08-16T00:00:00Z",
        "advertised_sha256": None,
    }
    meta = store.meta_root / f"{artifact.sha256}.json"
    raw = json.loads(meta.read_text())
    raw["origins"] = [{**good, "retrieved_at": "the day before yesterday"}, good]
    meta.write_text(json.dumps(raw))

    tables, errors = store.list_tables_with_errors()
    # The loss is reported rather than hidden, and it costs the origin only.
    assert [entry["error"] for entry in errors] == ["1 unreadable origin entry(s) dropped; the table is unaffected"]
    assert [item["sha256"] for item in tables] == [artifact.sha256]
    assert [origin["retrieved_at"] for origin in tables[0]["origins"]] == ["2026-08-16T00:00:00Z"]


def test_a_table_that_carries_its_own_window_is_imported_as_executable(tmp_path):
    # `<Forms>` is a section Cheat Engine acts on: it instantiates the designed
    # window when the table is opened, and that window's controls carry Lua
    # handlers. A table whose only executable content is one was imported with
    # `executable_content` false and reviewed as carrying none.
    from ce_decky.table_store import TableStore

    source = tmp_path / "trainer.CT"
    source.write_bytes(b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>
  </CheatEntries>
  <Forms><CustomForm Name="Trainer">TFVLTQ==</CustomForm></Forms>
</CheatTable>""")
    artifact = TableStore(tmp_path / "tables").import_ct(str(source))
    assert artifact.has_forms is True
    assert artifact.has_lua is False
    assert artifact.executable_content is True
    assert artifact.as_dict()["has_forms"] is True


def test_a_payload_inside_the_window_container_is_recorded_as_a_payload(tmp_path):
    # Stored metadata answers this with the same predicate the code view does,
    # so a payload cannot be recorded as a window on one screen and a payload
    # on another.
    from ce_decky.table_store import TableStore

    source = tmp_path / "payload.CT"
    source.write_bytes(b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <Forms><TableFile Filename="payload.exe">TVqQAAMAAAAEAAAA</TableFile></Forms>
</CheatTable>""")
    artifact = TableStore(tmp_path / "tables").import_ct(str(source))
    assert artifact.has_embedded_files is True
    assert artifact.has_forms is False
    assert artifact.executable_content is True


def test_a_payload_nested_under_a_window_is_recorded_as_a_payload(tmp_path):
    # The store walks with the same predicate the code view does, so a payload
    # written inside a window is an embedded file on every screen or on none.
    from ce_decky.table_store import TableStore

    source = tmp_path / "nested.CT"
    source.write_bytes(b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <Forms><CustomForm><Wrapper><TableFile Filename="p.bin">TVqQAAMAAAAEAAAA</TableFile></Wrapper></CustomForm></Forms>
</CheatTable>""")
    artifact = TableStore(tmp_path / "tables").import_ct(str(source))
    assert artifact.has_embedded_files is True
    assert artifact.has_forms is True
    assert artifact.executable_content is True


def test_the_payload_container_under_a_window_is_not_a_window(tmp_path):
    from ce_decky.table_store import TableStore

    source = tmp_path / "container.CT"
    source.write_bytes(b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <Forms><Files><TableFile Filename="p.bin">TVqQAAMAAAAEAAAA</TableFile></Files></Forms>
</CheatTable>""")
    artifact = TableStore(tmp_path / "tables").import_ct(str(source))
    assert artifact.has_embedded_files is True
    assert artifact.has_forms is False


def test_an_empty_window_container_is_not_imported_as_executable(tmp_path):
    # The container can be present and hold nothing. Stored metadata answers
    # this with the same predicate Review and the code view use, so a table
    # cannot be stored as executable and then have nothing to show for it.
    from ce_decky.table_store import TableStore

    source = tmp_path / "empty.CT"
    source.write_bytes(b"""<?xml version="1.0" encoding="utf-8"?>
<CheatTable CheatEngineTableVersion="45">
  <CheatEntries>
    <CheatEntry><ID>1</ID><Description>"Health"</Description><VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>
  </CheatEntries>
  <Forms/>
</CheatTable>""")
    artifact = TableStore(tmp_path / "tables").import_ct(str(source))
    assert artifact.has_forms is False
    assert artifact.executable_content is False


@pytest.mark.parametrize("existing", [False, True])
def test_metadata_failure_never_publishes_or_deletes_shared_blob(tmp_path, monkeypatch, existing):
    import ce_decky.table_store as module
    source = tmp_path / "a.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "tables")
    digest = __import__("hashlib").sha256(CT).hexdigest()
    if existing:
        store.import_ct(str(source))
    def refuse(*args, **kwargs):
        raise OSError("metadata publication refused")
    with monkeypatch.context() as patch:
        patch.setattr(module, "atomic_write_json", refuse)
        with pytest.raises(OSError, match="metadata publication"):
            store.import_ct(str(source))
    blob = store.blob_root / digest[:2] / digest / "table.CT"
    assert blob.exists() is existing
    if existing:
        assert blob.read_bytes() == CT
    assert source.read_bytes() == CT
    assert not list(store.staging_root.iterdir())
    assert store.import_ct(str(source)).sha256 == digest


def test_interrupted_import_leaves_a_visible_removable_record(tmp_path, monkeypatch):
    import ce_decky.table_store as module
    source = tmp_path / "a.CT"
    source.write_bytes(CT)
    store = TableStore(tmp_path / "tables")
    real_replace = module.os.replace
    def crash(source, destination):
        if Path(destination).name == "table.CT":
            raise KeyboardInterrupt("simulated process exit before blob publication")
        return real_replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", crash)
        with pytest.raises(KeyboardInterrupt):
            store.import_ct(str(source))
    restarted = TableStore(store.root)
    rows = restarted.list_tables()
    assert len(rows) == 1 and rows[0]["available"] is False
    restarted.delete_table(rows[0]["sha256"])
    assert restarted.list_tables() == []


def test_legacy_orphan_is_recovered_without_inventing_provenance(tmp_path):
    import logging
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "a.CT"
    source.write_bytes(CT)
    original = store.import_ct(str(source))
    metadata = store.meta_root / f"{original.sha256}.json"
    metadata.unlink()
    source.unlink()
    restarted = TableStore(store.root)
    restarted.reconcile_orphan_blobs(logging.getLogger("test"))
    recovered = restarted.get_table(original.sha256)
    assert recovered["available"] is True
    assert recovered["origins"] == () or recovered["origins"] == []
    assert recovered["imported_at"] is None
    assert recovered["filename"].startswith("Recovered-")
    first = metadata.read_bytes()
    restarted.reconcile_orphan_blobs(logging.getLogger("test"))
    assert metadata.read_bytes() == first
    restarted.delete_table(original.sha256)
    assert restarted.list_tables() == []


@pytest.mark.parametrize("fail_at", ["blob", "metadata"])
def test_delete_commits_blob_absence_before_metadata_and_reports_unknown(tmp_path, monkeypatch, fail_at):
    from ce_decky import atomic
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "a.CT"
    source.write_bytes(CT)
    artifact = store.import_ct(str(source))
    blob = Path(artifact.blob_path)
    metadata = store.meta_root / f"{artifact.sha256}.json"
    calls = []
    real_sync = atomic.fsync_directory
    def sync(path):
        calls.append(path)
        if path == (blob.parent if fail_at == "blob" else metadata.parent):
            raise OSError("sync failed")
        real_sync(path)
    with monkeypatch.context() as patch:
        patch.setattr(atomic, "fsync_directory", sync)
        with pytest.raises(atomic.DurabilityUnknownError):
            store.delete_table(artifact.sha256)
    assert calls[0] == blob.parent
    assert not blob.exists()
    assert metadata.exists() is (fail_at == "blob")
    if metadata.exists():
        store.delete_table(artifact.sha256)


def test_orphan_reconciliation_preserves_metadata_and_refuses_linked_bytes(tmp_path, caplog):
    import logging
    store = TableStore(tmp_path / "tables")
    source = tmp_path / "source.CT"
    source.write_bytes(CT)
    stored = store.import_ct(str(source))
    metadata = store.meta_root / f"{stored.sha256}.json"
    metadata.write_text("unreadable but pre-existing authority")
    logger = logging.getLogger("reconcile-test")
    store.reconcile_orphan_blobs(logger)
    assert metadata.read_text() == "unreadable but pre-existing authority"
    metadata.unlink()
    blob = Path(stored.blob_path)
    blob.unlink()
    blob.symlink_to(source)
    store.reconcile_orphan_blobs(logger)
    assert not metadata.exists()
    assert source.read_bytes() == CT
    assert blob.is_symlink()
    assert "table.orphan_recovery_failed" in caplog.text
