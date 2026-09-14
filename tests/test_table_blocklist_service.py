"""The blocklist where it actually acts: refusing an import, recording a bad one."""
from __future__ import annotations

from hashlib import sha256
import logging
from pathlib import Path

import pytest

from ce_decky.paths import PluginPaths
from ce_decky.service import PluginService
from ce_decky.table_blocklist import BlockedTableError

CT = (
    b'<CheatTable CheatEngineTableVersion="45"><CheatEntries>'
    b'<CheatEntry><ID>1</ID><Description>"Health"</Description>'
    b'<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>'
    b'</CheatEntries></CheatTable>'
)


def _service(tmp_path: Path) -> PluginService:
    service = PluginService(PluginPaths.for_tests(tmp_path), logging.getLogger("test"))
    service.initialize()
    return service


def _table(tmp_path: Path, name: str = "table.CT", data: bytes = CT) -> Path:
    source = tmp_path / name
    source.write_bytes(data)
    return source


def test_a_table_the_inspector_cannot_read_is_recorded_and_clearable(tmp_path: Path):
    """A table on disk that will never parse is unusable, and was unrecorded.

    Observed on this device: a real table imported, joined the game's library
    and then refused inspection every time. Nothing wrote that down, so search
    went on offering it as a local table, Review could not be opened, and there
    was nothing in **Tables that did not work** to clear. An inspection refusal
    is a property of these exact bytes, which is exactly what that record is
    keyed by.
    """
    service = _service(tmp_path)
    # Readable enough to import, and past the inspector's own depth ceiling.
    from ce_decky.ct_inspector import MAX_INSPECTION_DEPTH

    depth = MAX_INSPECTION_DEPTH + 2
    body = ""
    for index in range(depth):
        body = (
            f'<CheatEntry><ID>{index + 1}</ID><Description>"Level {index}"</Description>'
            f'<VariableType>4 Bytes</VariableType><Address>game.exe+{index}</Address>'
            f'<CheatEntries>{body}</CheatEntries></CheatEntry>'
        )
    data = (
        '<CheatTable CheatEngineTableVersion="45"><CheatEntries>' + body + "</CheatEntries></CheatTable>"
    ).encode("utf-8")

    source = _table(tmp_path, "deep.CT", data)
    digest = service.import_table(str(source))["sha256"]

    with pytest.raises(ValueError):
        service.inspect_table_sha(digest)

    recorded = {entry["sha256"]: entry for entry in service.list_blocked_tables()["tables"]}
    assert digest in recorded
    assert "could not be read" in str(recorded[digest]["reason"])
    assert recorded[digest]["filename"] == "deep.CT"

    # And the same bytes are refused rather than imported again, until the one
    # press in that list clears them.
    with pytest.raises(BlockedTableError):
        service.import_table(str(_table(tmp_path, "deep-again.CT", data)))
    assert service.unblock_table(digest) is True
    assert service.import_table(str(_table(tmp_path, "deep-again.CT", data)))["sha256"] == digest


def test_a_stored_file_that_cannot_be_read_is_not_recorded_as_a_bad_table(tmp_path: Path):
    """A missing copy is a state of this device, not a fact about any content.

    The digest names bytes nobody here has any more, so writing it down as a
    table that does not work would refuse the very re-download that fixes it.
    """
    service = _service(tmp_path)
    digest = service.import_table(str(_table(tmp_path)))["sha256"]
    Path(service.table_store.get_table(digest)["blob_path"]).unlink()

    with pytest.raises(ValueError):
        service.inspect_table_sha(digest)
    assert service.list_blocked_tables()["tables"] == []


def test_a_blocked_table_cannot_be_imported_again_from_any_route(tmp_path: Path):
    # The record is keyed by content, so the same bytes under another name are
    # the same table - which is the whole reason a provider cannot serve it back.
    service = _service(tmp_path)
    source = _table(tmp_path)
    digest = sha256(CT).hexdigest()
    assert service.import_table(str(source))["sha256"] == digest

    service.block_table(digest, "Cheat Engine ran it and it went straight back off.")
    renamed = _table(tmp_path, "renamed.CT")
    with pytest.raises(BlockedTableError) as refused:
        service.import_table(str(renamed))
    assert "went straight back off" in str(refused.value)

    assert service.unblock_table(digest) is True
    assert service.import_table(str(renamed))["sha256"] == digest


def test_bytes_that_are_not_a_table_are_recorded_by_content(tmp_path: Path):
    # CE Decky already tells a damaged download from a transport failure. Without
    # a durable record the same file is found, paid for with another provider
    # countdown and downloaded again, every search.
    service = _service(tmp_path)
    damaged = _table(tmp_path, "damaged.CT", b"<CheatTable><CheatEntries></CheatTable>")
    with pytest.raises(ValueError):
        service.import_table(str(damaged))

    listed = service.list_blocked_tables()
    assert listed["reason"] is None
    assert [entry["sha256"] for entry in listed["tables"]] == [
        sha256(b"<CheatTable><CheatEntries></CheatTable>").hexdigest()
    ]
    assert listed["tables"][0]["filename"] == "damaged.CT"

    # And it stays refused, rather than being re-inspected on every attempt.
    with pytest.raises(BlockedTableError):
        service.import_table(str(damaged))


def test_a_download_that_is_not_a_table_is_recorded_where_it_is_discarded(tmp_path: Path):
    # A provider download is inspected where it was staged and deleted there when
    # it turns out not to be a table, so it never reaches the import that would
    # otherwise record it. That is the most ordinary bad download there is.
    service = _service(tmp_path)
    damaged = tmp_path / "staged.CT"
    damaged.write_bytes(b"<CheatTable><CheatEntries></CheatTable>")
    digest = sha256(damaged.read_bytes()).hexdigest()

    service.table_store.note_unusable_bytes(digest, "the file is not a valid table", "Damaged.CT")

    listed = service.list_blocked_tables()["tables"]
    assert [entry["sha256"] for entry in listed] == [digest]
    assert listed[0]["filename"] == "Damaged.CT"
    with pytest.raises(BlockedTableError):
        service.import_table(str(damaged))


def test_a_damaged_download_records_the_provider_row_it_came_from(tmp_path: Path):
    """These bytes never enter the catalog, so nothing can look the row up later.

    Without it the record exists and search cannot recognise the row that
    produced it, so that row is offered, paid for with another provider
    countdown, downloaded and refused at import all over again.
    """
    service = _service(tmp_path)
    damaged = tmp_path / "staged.CT"
    damaged.write_bytes(b"<CheatTable><CheatEntries></CheatTable>")
    digest = sha256(damaged.read_bytes()).hexdigest()

    service.table_store.note_unusable_bytes(
        digest, "the file is not a valid table", "Damaged.CT",
        ("playground:page-1:file-2",),
    )

    listed = service.list_blocked_tables()["tables"]
    assert listed[0]["origins"] == ["playground:page-1:file-2"]


def test_a_malformed_table_inside_a_provider_archive_keeps_the_post_it_came_from(tmp_path: Path):
    """The extracted member is validated and deleted inside the import.

    Nothing downstream can discover where it came from afterwards, and a
    provider almost never advertises the digest of a file inside an archive, so
    without the origin the record existed and search could not recognise the
    post that produced it: the same archive was offered, paid for with another
    provider countdown, downloaded and refused all over again.
    """
    import zipfile
    from hashlib import sha256 as _sha256

    service = _service(tmp_path)
    broken = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries></CheatTable>'
    archive = tmp_path / "tables.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Game.CT", broken)

    with pytest.raises(ValueError):
        service.table_store.import_selection(
            str(archive),
            member_path="Game.CT",
            origins=("fearless:topic-7:attachment-3",),
        )

    listed = service.list_blocked_tables()["tables"]
    assert [entry["sha256"] for entry in listed] == [_sha256(broken).hexdigest()]
    assert listed[0]["origins"] == ["fearless:topic-7:attachment-3"]


def test_a_malformed_table_from_a_local_archive_records_no_post(tmp_path: Path):
    """Nothing offered it, so there is no row to recognise and none is invented."""
    import zipfile

    service = _service(tmp_path)
    archive = tmp_path / "local.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Game.CT", b'<CheatTable CheatEngineTableVersion="45"><CheatEntries></CheatTable>')

    with pytest.raises(ValueError):
        service.table_store.import_selection(str(archive), member_path="Game.CT")

    assert service.list_blocked_tables()["tables"][0]["origins"] == []


def test_refusing_a_known_bad_download_learns_the_post_it_came_from(tmp_path: Path):
    """A record can exist before anything knows where those bytes came from.

    A member extracted from an archive is validated and deleted inside the
    import, so an entry written the first time can carry no provider row at all.
    It then stays unrecognisable in search, and the same archive is offered,
    downloaded and refused all over again. The refusal is exactly the moment the
    row is known.
    """
    import zipfile
    from hashlib import sha256 as _sha256

    service = _service(tmp_path)
    broken = b'<CheatTable CheatEngineTableVersion="45"><CheatEntries></CheatTable>'
    digest = _sha256(broken).hexdigest()
    # Recorded by content only, the way an older build left it.
    service.table_blocklist.block(sha256=digest, reason="the file is not a valid table")
    assert service.list_blocked_tables()["tables"][0]["origins"] == []

    archive = tmp_path / "tables.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("Game.CT", broken)
    from ce_decky.table_store import TableContentError
    with pytest.raises(TableContentError):
        service.table_store.import_selection(
            str(archive), member_path="Game.CT", origins=("fearless:topic-7:attachment-3",),
        )

    entry = service.list_blocked_tables()["tables"][0]
    assert entry["origins"] == ["fearless:topic-7:attachment-3"]
    # Fresh structural proof replaces the older unclassified advisory. It must
    # never establish a usable final CT resolution for an archive.
    assert entry["cause"] == "unusable"
    assert "not a valid Cheat Engine table" in entry["reason"]


def test_reading_the_list_writes_back_the_rows_it_had_to_recover(tmp_path: Path):
    """Recovery that is only ever shown is recovery that can be lost.

    An entry written before provider rows were tracked knows only the digest,
    and the list filled that in from the table's own copy on the way out while
    leaving the record itself empty. Recognition then depended on that copy
    still existing: delete the plugin's data, or lose it any other way, and a
    table the user had already proved broken was offered and downloadable again
    with nothing left to recognise it by.
    """
    service = _service(tmp_path)
    table = service.table_store.import_ct(str(_table(tmp_path)))
    service.table_store.add_origin(table.sha256, {
        "provider": "fearless", "artifact_id": "topic-7:attachment-3", "topic_id": "7",
        "source_page": "https://fearlessrevolution.com/viewtopic.php?t=7",
        "original_filename": "table.CT", "retrieved_at": "2026-08-31T19:28:35Z",
        "advertised_sha256": None,
    })
    service.table_blocklist.block(sha256=table.sha256, reason="did not switch on")
    assert service.table_blocklist.find(table.sha256).origins == ()

    listed = service.list_blocked_tables()["tables"][0]
    assert listed["origins"] == ["fearless:topic-7:attachment-3"]
    # The record itself now holds it, so the table's own copy is no longer what
    # the mark depends on.
    assert service.table_blocklist.find(table.sha256).origins == ("fearless:topic-7:attachment-3",)


def test_a_file_the_source_no_longer_has_is_recorded_by_its_row(tmp_path: Path):
    """Nothing was downloaded, so there are no bytes to key the record on.

    The provider row is the whole of the identity, and without a durable record
    the row is offered, waited for and paid for again on the next search. It
    joins the same list the user reads and clears, because to them it is the
    same statement about the same row.
    """
    service = _service(tmp_path)
    service.block_missing_artifact("vgtimes", "game-x:file-1", "VGTimes no longer has this file.")

    entry = service.list_blocked_tables()["tables"][0]
    assert entry["sha256"] is None
    assert entry["key"] == "row:vgtimes:game-x:file-1"
    assert entry["origins"] == ["vgtimes:game-x:file-1"]

    # Cleared by the same action as any other record, by the key it is listed
    # under, and it never answers a question about content.
    assert service.table_blocklist.find(sha256("x".encode()).hexdigest()) is None
    assert service.unblock_table(entry["key"]) is True
    assert service.list_blocked_tables()["tables"] == []


def test_merging_a_provider_row_keeps_the_rows_already_recorded(tmp_path: Path):
    service = _service(tmp_path)
    store = service.table_blocklist
    store.block(sha256="d" * 64, reason="not a table", origins=("fearless:topic-1:attachment-1",))

    assert store.add_origins("d" * 64, ("playground:page-2:file-2",)) is True
    assert store.find("d" * 64).origins == ("fearless:topic-1:attachment-1", "playground:page-2:file-2")
    # Nothing new to add, and an entry that does not exist, both change nothing.
    assert store.add_origins("d" * 64, ("fearless:topic-1:attachment-1",)) is False
    assert store.add_origins("e" * 64, ("fearless:topic-9:attachment-9",)) is False


def test_a_repaired_record_reports_exactly_the_rows_it_kept(tmp_path: Path):
    """The answer and the record say one thing, on the first read and on the next.

    A table carries more provider rows than a record retains, so publishing the
    recovery itself made the first read after a repair describe a wider set than
    the one written down: the same list read again, or after a restart, said
    something else, and a row the retention rule had deliberately dropped looked
    for a moment like part of the mark.
    """
    from ce_decky.table_blocklist import MAX_BLOCKED_ORIGINS

    service = _service(tmp_path)
    table = service.table_store.import_ct(str(_table(tmp_path)))
    rows = [f"fearless:topic-{index}:attachment-{index}" for index in range(MAX_BLOCKED_ORIGINS * 3)]
    for index, row in enumerate(rows):
        topic, attachment = row.split(":")[1:]
        service.table_store.add_origin(table.sha256, {
            "provider": "fearless", "artifact_id": f"{topic}:{attachment}", "topic_id": topic.split("-")[1],
            "source_page": f"https://fearlessrevolution.com/viewtopic.php?t={index}",
            "original_filename": "table.CT", "retrieved_at": f"2026-08-{10 + index % 20:02d}T19:28:35Z",
            "advertised_sha256": None,
        })
    service.table_blocklist.block(sha256=table.sha256, reason="did not switch on")
    assert service.table_blocklist.find(table.sha256).origins == ()

    kept = rows[-MAX_BLOCKED_ORIGINS:]
    first = service.list_blocked_tables()["tables"][0]["origins"]
    assert first == kept
    assert list(service.table_blocklist.find(table.sha256).origins) == kept
    assert service.list_blocked_tables()["tables"][0]["origins"] == kept
    assert _service(tmp_path).list_blocked_tables()["tables"][0]["origins"] == kept


def test_a_repair_reports_one_whole_record_when_the_same_bytes_are_recorded_again(tmp_path: Path, monkeypatch):
    """Never a row assembled from two generations of the same record.

    The list is read first and the repair writes later, so the same bytes can
    be recorded again in between. Copying the current rows into the older
    snapshot described a record nobody ever wrote: yesterday's reason and cause
    beside today's provider rows.
    """
    service = _service(tmp_path)
    table = service.table_store.import_ct(str(_table(tmp_path)))
    service.table_store.add_origin(table.sha256, {
        "provider": "fearless", "artifact_id": "topic-7:attachment-3", "topic_id": "7",
        "source_page": "https://fearlessrevolution.com/viewtopic.php?t=7",
        "original_filename": "table.CT", "retrieved_at": "2026-08-31T19:28:35Z",
        "advertised_sha256": None,
    })
    service.table_blocklist.block(sha256=table.sha256, reason="the download was not a table",
                                  cause="unusable", now=1700000000)

    # Recorded again, with everything about it different, between the read the
    # list started from and the write the repair makes.
    original = service.table_blocklist.repair
    def reblock_then_repair(**kwargs):
        service.table_blocklist.block(sha256=table.sha256, reason="a cheat went straight back off",
                                      cause="refused", app_id=77, game_name="Another game", now=1700009999)
        return original(**kwargs)
    monkeypatch.setattr(service.table_blocklist, "repair", reblock_then_repair)

    listed = service.list_blocked_tables()["tables"][0]
    monkeypatch.undo()
    current = service.table_blocklist.find(table.sha256)
    assert listed == current.as_dict()
    assert listed["cause"] == "refused" and listed["game_name"] == "Another game"
    assert listed["origins"] == ["fearless:topic-7:attachment-3"]


def test_a_repair_whose_durability_is_unknown_reports_what_is_on_disk(tmp_path: Path, monkeypatch):
    """The replacement is visible and the sync could not be proven.

    Reporting that as a repair that did not happen published the whole recovery
    again, so the answer described a wider set than the file the next read was
    about to be served from.
    """
    from ce_decky.table_blocklist import MAX_BLOCKED_ORIGINS

    service = _service(tmp_path)
    table = service.table_store.import_ct(str(_table(tmp_path)))
    rows = [f"fearless:topic-{index}:attachment-{index}" for index in range(MAX_BLOCKED_ORIGINS * 3)]
    for index, row in enumerate(rows):
        topic, attachment = row.split(":")[1:]
        service.table_store.add_origin(table.sha256, {
            "provider": "fearless", "artifact_id": f"{topic}:{attachment}", "topic_id": topic.split("-")[1],
            "source_page": f"https://fearlessrevolution.com/viewtopic.php?t={index}",
            "original_filename": "table.CT", "retrieved_at": f"2026-08-{10 + index % 20:02d}T19:28:35Z",
            "advertised_sha256": None,
        })
    service.table_blocklist.block(sha256=table.sha256, reason="did not switch on")

    def fail_sync(path):
        raise OSError("directory sync failed")
    monkeypatch.setattr("ce_decky.atomic.fsync_directory", fail_sync)
    kept = rows[-MAX_BLOCKED_ORIGINS:]
    assert service.list_blocked_tables()["tables"][0]["origins"] == kept
    monkeypatch.undo()
    # What was already visible is what the next read and a restart are served.
    assert list(service.table_blocklist.find(table.sha256).origins) == kept
    assert service.list_blocked_tables()["tables"][0]["origins"] == kept
    assert _service(tmp_path).list_blocked_tables()["tables"][0]["origins"] == kept


def test_a_full_origin_list_still_learns_the_post_just_encountered(tmp_path: Path):
    """Keeping the first eight meant a full record could never learn a ninth.

    The post that just offered these bytes is the only one known to be offering
    them right now, and it was the one dropped, so it stayed unrecognisable in
    search across a reload.
    """
    from ce_decky.table_blocklist import MAX_BLOCKED_ORIGINS

    service = _service(tmp_path)
    store = service.table_blocklist
    existing = tuple(f"fearless:topic-{index}:attachment-{index}" for index in range(MAX_BLOCKED_ORIGINS))
    store.block(sha256="f" * 64, reason="not a table", origins=existing)
    assert store.find("f" * 64).origins == existing

    assert store.add_origins("f" * 64, ("playground:page-9:file-9",)) is True

    origins = store.find("f" * 64).origins
    assert len(origins) == MAX_BLOCKED_ORIGINS
    assert origins[-1] == "playground:page-9:file-9"
    # The oldest is what made room, and everything between it and the new row
    # is kept in the order it was recorded.
    assert existing[0] not in origins
    assert origins[:-1] == existing[1:]
    # Meeting a row that is already recorded is not news and evicts nothing.
    assert store.add_origins("f" * 64, (existing[1],)) is False
    assert store.find("f" * 64).origins == origins


def test_the_record_names_the_game_it_was_tried_against(tmp_path: Path):
    service = _service(tmp_path)
    digest = sha256(CT).hexdigest()
    service.import_table(str(_table(tmp_path)))
    service.save_profile(app_id=42, name="Some Game", is_shortcut=False, table_sha256=digest, target_process="game.exe")

    entry = service.block_table(digest, "Cheat Engine refused it", 42)
    assert entry["game_name"] == "Some Game"
    assert entry["app_id"] == 42
    assert entry["filename"] == "table.CT"
    # Nothing is running, so there is no build to observe. An absent version is
    # honest; a guessed one would be worse than none.
    assert entry["game_version"] is None


def test_a_record_written_before_the_game_had_a_profile_is_named_when_it_is_read(tmp_path: Path):
    """The row that named a file and no game, in a list that spans every game.

    Observed on this device: a VGTimes download failed because the source no
    longer had the file, and that happened before any table had been chosen for
    the game, so there was no profile to read a name from. The AppID travelled
    with the press and was recorded; the name had nowhere to come from and the
    row read `tablica-dlja-cheat-engine_1785142527_386385.rar` beside two rows
    that did name their game. The name exists by the time anyone opens the list.
    """
    service = _service(tmp_path)
    service.block_missing_artifact(
        "vgtimes", "game-x:file-1", "The source no longer has this file.",
        app_id=42, filename="tablica.rar",
    )
    recorded = service.list_blocked_tables()["tables"][0]
    assert recorded["app_id"] == 42 and recorded["game_name"] is None

    digest = sha256(CT).hexdigest()
    service.import_table(str(_table(tmp_path)))
    service.save_profile(app_id=42, name="Some Game", is_shortcut=False, table_sha256=digest, target_process="game.exe")

    named = [item for item in service.list_blocked_tables()["tables"] if item["app_id"] == 42 and item["sha256"] is None]
    assert named and named[0]["game_name"] == "Some Game"
    # Written back, not merely displayed: a name recovered on every read is a
    # name nothing else can see, and the next read would do the work again.
    stored = service.table_blocklist.list_blocked()
    assert [entry.game_name for entry in stored if entry.sha256 is None] == ["Some Game"]
    # It fills a blank and never replaces a name, and it touches nothing else.
    assert named[0]["reason"] == "The source no longer has this file."
    assert service.table_blocklist.name_game(named[0]["key"], "Another Game") is False


def test_a_very_long_reason_is_trimmed_rather_than_losing_the_record(tmp_path: Path):
    # The reason names the cheat that was refused, and a table may give a cheat a
    # very long description. The record is what stops the same file being found
    # and downloaded again, so it must not be thrown away over its own length.
    service = _service(tmp_path)
    entry = service.block_table("a" * 64, "Refused: " + "n" * 4000)
    assert len(entry["reason"].encode("utf-8")) <= 1024
    assert entry["reason"].startswith("Refused: ")
    assert entry["reason"].endswith("\u2026")
    assert service.list_blocked_tables()["tables"][0]["sha256"] == "a" * 64


def test_clearing_every_record_is_one_action(tmp_path: Path):
    service = _service(tmp_path)
    service.block_table("a" * 64, "one")
    service.block_table("b" * 64, "two")
    assert service.clear_blocked_tables() == 2
    assert service.list_blocked_tables()["tables"] == []
    assert service.clear_blocked_tables() == 0


def test_an_unreadable_record_never_stops_an_import(tmp_path: Path):
    # The list is advice. Failing closed on a corrupt advisory file would turn it
    # into "no table can be imported", which is far worse than offering a table
    # the user has to reject once more.
    service = _service(tmp_path)
    service.table_blocklist.path.parent.mkdir(parents=True, exist_ok=True)
    service.table_blocklist.path.write_text("{ not json", encoding="utf-8")

    assert service.import_table(str(_table(tmp_path)))["sha256"] == sha256(CT).hexdigest()
    listed = service.list_blocked_tables()
    assert listed["tables"] == [] and listed["reason"]

    # Unreadable at the descriptor rather than at the parser is the same rule.
    # A directory where the record should be is the reachable shape of it.
    service.table_blocklist.path.unlink()
    service.table_blocklist.path.mkdir()
    assert service.import_table(str(_table(tmp_path, "again.CT")))["sha256"] == sha256(CT).hexdigest()


def test_the_record_remembers_the_provider_row_the_bytes_came_from(tmp_path: Path):
    """A search result advertises a digest only sometimes and a provider row always.

    Recognising a bad table only by content digest meant the mark was invisible
    on exactly the rows it had been recorded from, so the same table was
    offered, downloaded and proved broken again on every search.
    """
    service = _service(tmp_path)
    digest = sha256(CT).hexdigest()
    service.import_table(str(_table(tmp_path)))
    service.table_store.add_origin(digest, {
        "provider": "fearless",
        "artifact_id": "topic-1:attachment-2",
        "topic_id": "1",
        "source_page": "https://fearlessrevolution.com/viewtopic.php?t=1",
        "original_filename": "hl2.CT",
        "retrieved_at": "2026-09-01T00:00:00Z",
        "advertised_sha256": None,
    })

    entry = service.block_table(digest, "It went straight back off.")

    assert entry["origins"] == ["fearless:topic-1:attachment-2"]
    listed = service.list_blocked_tables()["tables"]
    assert listed[0]["origins"] == ["fearless:topic-1:attachment-2"]


def test_a_locally_imported_table_is_recorded_with_no_provider_row(tmp_path: Path):
    """Nothing offered it, so there is no row to recognise and none is invented."""
    service = _service(tmp_path)
    digest = sha256(CT).hexdigest()
    service.import_table(str(_table(tmp_path)))

    assert service.block_table(digest, "It went straight back off.")["origins"] == []


def test_a_malformed_origin_never_costs_the_record_that_marks_a_bad_table(tmp_path: Path):
    """Provider-described data, so it is dropped on its own like every other field."""
    from ce_decky.table_blocklist import MAX_BLOCKED_ORIGINS, TableBlocklist

    store = TableBlocklist(tmp_path / "blocked.json")
    entry = store.block(
        sha256="b" * 64,
        reason="It went straight back off.",
        origins=[
            "fearless:topic-1:attachment-2",
            "fearless:topic-1:attachment-2",
            123,
            "",
            "bad‮origin",
            *[f"fearless:topic-{index}" for index in range(MAX_BLOCKED_ORIGINS + 4)],
        ],
    )

    # The rows kept are the newest of what was submitted, which is the set a
    # search is most likely to put in front of the user again.
    assert entry.origins == tuple(
        f"fearless:topic-{index}" for index in range(MAX_BLOCKED_ORIGINS + 4 - MAX_BLOCKED_ORIGINS, MAX_BLOCKED_ORIGINS + 4)
    )
    assert len(entry.origins) <= MAX_BLOCKED_ORIGINS
    assert all(isinstance(item, str) and item for item in entry.origins)
    # Bidirectional overrides can reorder what is on screen, exactly as for a
    # filename or a reason rendered beside it.
    assert not any("‮" in item for item in entry.origins)


def test_a_record_written_before_provider_rows_were_tracked_is_still_recognised(tmp_path: Path):
    """Otherwise the fix only helps tables the user proves broken a second time.

    The record knows the digest; the catalog still knows where those bytes came
    from. Filling that in on the way out is what makes an existing mark visible
    in search straight away.
    """
    import json

    service = _service(tmp_path)
    digest = sha256(CT).hexdigest()
    service.import_table(str(_table(tmp_path)))
    service.table_store.add_origin(digest, {
        "provider": "fearless",
        "artifact_id": "topic-39756:attachment-79400",
        "topic_id": "39756",
        "source_page": "https://fearlessrevolution.com/viewtopic.php?t=39756",
        "original_filename": "hl2.CT",
        "retrieved_at": "2026-09-01T00:00:00Z",
        "advertised_sha256": None,
    })
    service.block_table(digest, "It went straight back off.")

    # Rewrite the stored record the way an older build left it: no origins.
    path = service.paths.state_root / "blocked_tables.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    for row in stored["tables"]:
        row.pop("origins", None)
    path.write_text(json.dumps(stored), encoding="utf-8")

    listed = service.list_blocked_tables()["tables"]
    assert listed[0]["origins"] == ["fearless:topic-39756:attachment-79400"]


def test_a_record_whose_table_is_gone_still_lists_without_provider_rows(tmp_path: Path):
    """A table can be blocked long after its own file was removed."""
    service = _service(tmp_path)
    service.table_blocklist.block(sha256="c" * 64, reason="It went straight back off.")

    listed = service.list_blocked_tables()["tables"]
    assert listed[0]["origins"] == []


def test_each_route_into_the_list_records_what_put_it_there(tmp_path: Path):
    """Three routes, three statements, and the panel must be able to tell them apart.

    A user reading a retired row acts differently on each: a table that ran and
    did not work may work again after the game updates, bytes that were never a
    table never will, and a file the source no longer has is not about this
    device at all. Only the route that writes the record knows which it is, so
    if any of these three stopped saying so the screen would fall back to a
    generic mark with nothing able to notice.
    """
    service = _service(tmp_path)
    imported = service.import_table(str(_table(tmp_path)))
    service.block_table(imported["sha256"], "it went straight back off", None)
    service.table_store.note_unusable_bytes(
        sha256(b"junk").hexdigest(), "the file is not a valid table", "Damaged.CT",
    )
    service.block_missing_artifact("vgtimes", "game-x:file-1", "VGTimes no longer has this file.")

    causes = {entry["key"]: entry["cause"] for entry in service.list_blocked_tables()["tables"]}
    assert causes == {
        imported["sha256"]: "refused",
        sha256(b"junk").hexdigest(): "unusable",
        "row:vgtimes:game-x:file-1": "gone",
    }


def test_a_record_names_the_game_it_was_tried_on_whatever_put_it_there(tmp_path: Path):
    """The list spans every game and is read months later.

    Only the refusal route recorded a game, so the two rows a Neon Bazaar
    search had just retired read as `winmm-x64.zip` and a raw provider key, and
    nothing on the screen said either belonged to that game or to any other:
    the user could not tell whether the rows marked in the search were even in
    this list. The AppID travels with the press; the name is looked up from the
    profile store, which already holds it.
    """
    service = _service(tmp_path)
    service.save_profile(
        app_id=42, name="Neon Bazaar", is_shortcut=False,
        table_sha256=None, target_process=None,
    )
    damaged = _table(tmp_path, "damaged.CT", b"<CheatTable><CheatEntries></CheatTable>")
    with pytest.raises(ValueError):
        service.import_table(str(damaged), app_id=42)
    service.block_missing_artifact(
        "vgtimes", "game-neon-bazaar:file-96527", "VGTimes no longer has this file.",
        42, "tablica-dlja-cheat-engine.rar",
    )

    listed = {entry["key"]: entry for entry in service.list_blocked_tables()["tables"]}
    unusable = listed[sha256(damaged.read_bytes()).hexdigest()]
    assert (unusable["app_id"], unusable["game_name"]) == (42, "Neon Bazaar")
    gone = listed["row:vgtimes:game-neon-bazaar:file-96527"]
    assert (gone["app_id"], gone["game_name"]) == (42, "Neon Bazaar")
    # And the row's own file name, so the list stops naming it by the key it is
    # stored under, which is neither a game nor a table.
    assert gone["filename"] == "tablica-dlja-cheat-engine.rar"


def test_a_game_that_cannot_be_read_costs_the_record_nothing(tmp_path: Path):
    # The game is display context for a list, and the record it decorates is an
    # import refusal. An AppID with no profile behind it, and no AppID at all,
    # both leave an entry that still refuses those bytes.
    service = _service(tmp_path)
    damaged = _table(tmp_path, "damaged.CT", b"<CheatTable><CheatEntries></CheatTable>")
    with pytest.raises(ValueError):
        service.import_table(str(damaged), app_id=999999)

    entry = service.list_blocked_tables()["tables"][0]
    assert (entry["app_id"], entry["game_name"]) == (999999, None)
    with pytest.raises(BlockedTableError):
        service.import_table(str(damaged))


def test_every_route_that_can_record_a_table_names_the_game(tmp_path: Path):
    """A refusal is recorded from wherever the table is read, not only from Review.

    The AppID is a parameter of every one of those methods, so the game is
    known exactly at each of them; passing it at some and not others leaves the
    same list with the same hole in it, on the routes where the game is least
    in doubt. Reached here through the pinned-control write, which reads the
    table to validate the record it is asked to pin.
    """
    from ce_decky.ct_inspector import MAX_INSPECTION_DEPTH

    service = _service(tmp_path)
    body = ""
    for index in range(MAX_INSPECTION_DEPTH + 2):
        body = (
            f'<CheatEntry><ID>{index + 1}</ID><Description>"Level {index}"</Description>'
            f'<VariableType>4 Bytes</VariableType><Address>game.exe+{index}</Address>'
            f'<CheatEntries>{body}</CheatEntries></CheatEntry>'
        )
    data = (
        '<CheatTable CheatEngineTableVersion="45"><CheatEntries>' + body + "</CheatEntries></CheatTable>"
    ).encode("utf-8")
    digest = service.import_table(str(_table(tmp_path, "deep.CT", data)))["sha256"]
    service.save_profile(
        app_id=42, name="Neon Bazaar", is_shortcut=False,
        table_sha256=digest, target_process="game.exe",
    )

    with pytest.raises(ValueError):
        service.set_pinned_control(42, digest, 1, True)

    entry = service.list_blocked_tables()["tables"][0]
    assert entry["cause"] == "unusable"
    assert (entry["app_id"], entry["game_name"]) == (42, "Neon Bazaar")


def test_reading_the_list_repairs_every_row_with_one_write(tmp_path: Path, monkeypatch):
    """A read that repairs must not rewrite the store once per row.

    Filling in a missing game name used to load the whole store, save the whole
    store, and parse it a second time on the way to carrying the failure
    epochs. That is right for a repair arriving on its own from the press that
    learned the fact, and wrong for reading the list, which repairs every row
    that needs it: at the entries this store allows it turned one nominal read
    into a four figure number of parses and one durable replacement per row,
    worst on exactly the large part-named store that needs the most repair.
    """
    service = _service(tmp_path)
    rows = 40
    for index in range(rows):
        digest = sha256(f"table-{index}".encode()).hexdigest()
        service.table_blocklist.block(
            sha256=digest, reason="a cheat went straight back off", cause="refused",
            app_id=100 + index, now=1700000000 + index,
        )
        service.save_profile(100 + index, f"Game {index}", False)
    assert all(entry.game_name is None for entry in service.table_blocklist.list_blocked())

    reads = {"load": 0, "save": 0}
    store = service.table_blocklist
    original_read, original_save = store._read_state, store._save
    monkeypatch.setattr(store, "_read_state", lambda: (reads.__setitem__("load", reads["load"] + 1), original_read())[1])
    monkeypatch.setattr(store, "_save", lambda *a, **k: (reads.__setitem__("save", reads["save"] + 1), original_save(*a, **k))[1])

    listed = service.list_blocked_tables()["tables"]

    assert len(listed) == rows
    assert all(item["game_name"] == f"Game {item['app_id'] - 100}" for item in listed)
    # One durable replacement for the whole repair, not one per row.
    assert reads["save"] == 1
    # And a bounded number of parses: the list itself, the repair's own load,
    # and the read that carries the failure epochs into the save.
    assert reads["load"] <= 4, reads

    # Read again and nothing is owed, so nothing is written.
    reads["load"] = reads["save"] = 0
    again = service.list_blocked_tables()["tables"]
    assert [item["game_name"] for item in again] == [item["game_name"] for item in listed]
    assert reads["save"] == 0
