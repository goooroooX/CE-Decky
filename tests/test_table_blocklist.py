"""Durable record of exact tables that were proven not to work."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ce_decky.table_blocklist import (
    CAUSE_GONE,
    CAUSE_REFUSED,
    CAUSE_UNKNOWN,
    CAUSE_UNUSABLE,
    MAX_BLOCKED_ORIGINS,
    MAX_BLOCKED_TABLES,
    BlockedTableError,
    TableBlocklist,
)

SHA_A = "a" * 64
SHA_B = "b" * 64


def _blocklist(tmp_path: Path) -> TableBlocklist:
    return TableBlocklist(tmp_path / "blocked_tables.json")


def test_a_blocked_table_refuses_re_import_and_names_the_recorded_reason(tmp_path: Path):
    # The whole point of recording by content: the same bytes, found again under
    # another name in another post, are refused with what happened last time.
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="Cheat Engine ran it and it went straight back off.")
    store.assert_importable(SHA_B)
    with pytest.raises(BlockedTableError) as refused:
        store.assert_importable(SHA_A.upper())
    assert "went straight back off" in str(refused.value)
    assert refused.value.entry.sha256 == SHA_A


def test_clearing_one_entry_makes_exactly_that_table_importable_again(tmp_path: Path):
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="not a table")
    store.block(sha256=SHA_B, reason="not a table")
    assert store.unblock(SHA_A) is True
    assert store.unblock(SHA_A) is False
    store.assert_importable(SHA_A)
    with pytest.raises(BlockedTableError):
        store.assert_importable(SHA_B)
    assert store.clear() == 1
    store.assert_importable(SHA_B)


def test_the_shape_before_this_one_is_read_and_written_back_in_this_one(tmp_path: Path):
    # The exact file 0.9.24 wrote: the same entries, the same floor, the same
    # epochs, and no `table_version` anywhere. Refusing it did not merely hide
    # the not-working list - the epochs live here too, so every table the device
    # had proven read back as needing a retest, for a record that differs from
    # this one by a field that is allowed to be absent.
    path = tmp_path / "blocked_tables.json"
    epochs = {SHA_A: "c" * 32}
    path.write_text(json.dumps({"schema": 3, "failure_floor": "d" * 32, "failure_epochs": epochs, "tables": [{
        "sha256": SHA_A, "reason": "did not switch on", "filename": "Table.CT",
        "app_id": 220, "game_name": "Half-Life 2", "game_version": "build 9", "recorded_at": 1700000000,
        "origins": ["fearless:41"], "cause": CAUSE_REFUSED,
    }]}), encoding="utf-8")
    store = TableBlocklist(path)

    [entry] = store.list_blocked()
    assert (entry.sha256, entry.reason, entry.app_id, entry.game_name) == (SHA_A, "did not switch on", 220, "Half-Life 2")
    assert (entry.game_version, entry.origins, entry.cause) == ("build 9", ("fearless:41",), CAUSE_REFUSED)
    # Absent, which is what a record whose source advertised no release carries
    # anyway, rather than invented from the game version beside it.
    assert entry.table_version is None
    assert store.failure_epochs() == ("d" * 32, epochs)
    with pytest.raises(BlockedTableError):
        store.assert_importable(SHA_A)

    # The next write puts the file in this version's shape, with everything that
    # was in it still in it.
    store.block(sha256=SHA_B, reason="froze the game", cause=CAUSE_REFUSED)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["schema"] == TableBlocklist.SCHEMA == 4
    assert written["failure_floor"] == "d" * 32
    # The epoch the old file carried is the one a positive record is still
    # checked against, and the new failure adds its own beside it.
    assert written["failure_epochs"][SHA_A] == "c" * 32
    assert re.fullmatch("[0-9a-f]{32}", written["failure_epochs"][SHA_B])
    assert sorted(entry.key for entry in store.list_blocked()) == [SHA_A, SHA_B]
    assert [row["table_version"] for row in written["tables"] if row["sha256"] == SHA_A] == [None]


def test_a_file_older_than_that_refuses_nothing_rather_than_half_reading(tmp_path: Path):
    # The reader goes one shape back and stops. Older than that is a record the
    # user clears, and an unreadable record blocks no import, which is the same
    # fail-open every other unreadable state here has.
    path = tmp_path / "blocked.json"
    path.write_text(json.dumps({"schema": 2, "tables": [{
        "sha256": "a" * 64, "reason": "did not switch on", "filename": "Table.CT",
        "app_id": None, "game_name": None, "game_version": None, "recorded_at": 1700000000,
        "origins": [],
    }]}), encoding="utf-8")
    store = TableBlocklist(path)
    with pytest.raises(ValueError):
        store.list_blocked()

    # It is an unreadable record like any other, so it refuses nothing and the
    # same Clear repairs it, rather than being a state of its own to explain.
    assert store.clear() == 0
    store.assert_importable("a" * 64)
    store.block(sha256="b" * 64, reason="did not switch on", cause=CAUSE_REFUSED)
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == TableBlocklist.SCHEMA
    assert [entry.key for entry in store.list_blocked()] == ["b" * 64]


def test_the_record_survives_and_keeps_the_context_a_user_reads_later(tmp_path: Path):
    # A table stops working because the game was updated, so the build it was
    # tried against is what dates the record.
    store = _blocklist(tmp_path)
    store.block(
        sha256=SHA_A,
        reason="Cheat Engine refused it",
        filename="Table.CT",
        app_id=42,
        game_name="Some Game",
        game_version="1.2.3.4-build-7",
        table_version="1.05.01",
        cause=CAUSE_REFUSED,
        now=1_700_000_000,
    )
    reopened = _blocklist(tmp_path).list_blocked()
    assert [entry.as_dict() for entry in reopened] == [{
        "key": "a" * 64,
        "sha256": SHA_A,
        "reason": "Cheat Engine refused it",
        "filename": "Table.CT",
        "app_id": 42,
        "game_name": "Some Game",
        "game_version": "1.2.3.4-build-7",
        "table_version": "1.05.01",
        "recorded_at": 1_700_000_000,
        "origins": [],
        "cause": CAUSE_REFUSED,
    }]


def test_blocking_the_same_table_again_keeps_the_newer_reason(tmp_path: Path):
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="first", now=1)
    store.block(sha256=SHA_A, reason="second", game_name="Other Game", now=2)
    entries = store.list_blocked()
    assert len(entries) == 1
    assert entries[0].reason == "second" and entries[0].game_name == "Other Game"


def test_newest_records_are_listed_first(tmp_path: Path):
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="older", now=10)
    store.block(sha256=SHA_B, reason="newer", now=20)
    assert [entry.reason for entry in store.list_blocked()] == ["newer", "older"]


def test_the_record_is_bounded_and_evicts_the_oldest_download_it_holds(tmp_path: Path):
    # Bounded, oldest first, and never at the cost of a decision the user made:
    # enough ordinary download failures would otherwise have lifted a mark on a
    # table nobody had cleared.
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="a cheat went straight back off", cause=CAUSE_REFUSED, now=1)
    for index in range(MAX_BLOCKED_TABLES - 1):
        store.block(sha256=f"{index:064x}", reason="not a table", cause=CAUSE_UNUSABLE, now=1000 + index)
    store.block(sha256="f" * 64, reason="newest", cause=CAUSE_UNUSABLE, now=9999)
    entries = store.list_blocked()
    assert len(entries) == MAX_BLOCKED_TABLES
    assert store.find("f" * 64) is not None
    # The oldest record of all is the user's, and it is the one that stays.
    assert store.find(SHA_A) is not None
    assert store.find(f"{0:064x}") is None


def test_a_new_record_keeps_the_rows_the_bytes_were_most_recently_found_through(tmp_path: Path):
    # One retention policy on every path that writes origins. A table met on
    # many posts over the years was keeping the ones it was found through first,
    # so the row a user had just downloaded from, which is the row a search is
    # about to offer again, carried no mark at all.
    store = _blocklist(tmp_path)
    rows = [f"fearless:topic-{index}" for index in range(MAX_BLOCKED_ORIGINS * 3)]
    entry = store.block(sha256=SHA_A, reason="did not switch on", origins=[*rows, rows[0]])
    assert entry.origins == tuple(rows[-MAX_BLOCKED_ORIGINS:])
    # A row met again keeps the place it already had rather than counting as news.
    assert store.add_origins(SHA_A, [rows[-1]]) is False
    # A bulk repair is read whole too: a record with no rows at all is repaired
    # from the newest of what the table carries, not the first eight of it.
    bare = _blocklist(tmp_path / "bare")
    bare.block(sha256=SHA_B, reason="did not switch on")
    assert bare.find(SHA_B).origins == ()
    assert bare.add_origins(SHA_B, rows) is True
    assert bare.find(SHA_B).origins == tuple(rows[-MAX_BLOCKED_ORIGINS:])
    assert TableBlocklist(bare.path).find(SHA_B).origins == tuple(rows[-MAX_BLOCKED_ORIGINS:])
    assert store.add_origins(SHA_A, ["vgtimes:game-1:file-2"]) is True
    merged = store.find(SHA_A)
    assert merged is not None
    assert merged.origins == (*rows[-(MAX_BLOCKED_ORIGINS - 1):], "vgtimes:game-1:file-2")


def test_download_churn_cannot_lift_a_mark_the_user_made(tmp_path: Path):
    # The shape the bound is actually met in: one table the user marked, and a
    # long run of ordinary download failures afterwards. Every one of them makes
    # room from its own kind, and the mark is still there at the end of it.
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="a cheat went straight back off", cause=CAUSE_REFUSED, now=1)
    for index in range(MAX_BLOCKED_TABLES * 2):
        store.block(sha256=f"{index:064x}", reason="not a table", cause=CAUSE_UNUSABLE, now=1000 + index)
    held = store.find(SHA_A)
    assert held is not None and held.cause == CAUSE_REFUSED
    assert len(store.list_blocked()) == MAX_BLOCKED_TABLES
    assert store.unblock(SHA_A) is True
    assert store.find(SHA_A) is None


def test_a_full_list_of_the_user_s_own_decisions_refuses_rather_than_drops_one(tmp_path: Path):
    # The boundary: nothing here came from a download, so there is nothing to
    # make room with that was not somebody's explicit answer. The refusal names
    # the press that makes room, and every existing record stands.
    store = _blocklist(tmp_path)
    for index in range(MAX_BLOCKED_TABLES):
        store.block(sha256=f"{index:064x}", reason="did not switch on", cause=CAUSE_REFUSED, now=1000 + index)
    with pytest.raises(ValueError, match="clear one under"):
        store.block(sha256="f" * 64, reason="not a table", cause=CAUSE_UNUSABLE, now=9999)
    with pytest.raises(ValueError, match="clear one under"):
        store.block(sha256="f" * 64, reason="did not switch on", cause=CAUSE_REFUSED, now=9999)
    assert len(store.list_blocked()) == MAX_BLOCKED_TABLES
    assert store.find(f"{0:064x}") is not None
    # Clearing one is what makes room, which is the only thing that ever did.
    store.unblock(f"{0:064x}")
    store.block(sha256="f" * 64, reason="did not switch on", cause=CAUSE_REFUSED, now=9999)
    assert store.find("f" * 64) is not None


def test_untrusted_reason_text_is_refused_rather_than_rendered(tmp_path: Path):
    # The reason is assembled from what Cheat Engine and the table said, and it
    # is later rendered beside a filename, so it may not carry anything that can
    # reorder what is on screen: an empty reason, a bidirectional override, a
    # control character, or more text than the row can hold.
    store = _blocklist(tmp_path)
    for bad in ("", "   ", "a‮b", "a\x07b", "x" * 2000):
        with pytest.raises(ValueError):
            store.block(sha256=SHA_A, reason=bad)


def test_clearing_repairs_a_record_that_cannot_be_read(tmp_path: Path):
    # Clearing is the one action offered for an unreadable record, and reading
    # it first meant the only recovery on offer was the thing the corrupt state
    # prevented.
    path = tmp_path / "blocked_tables.json"
    path.write_text(json.dumps({"schema": 99, "tables": []}), encoding="utf-8")
    store = TableBlocklist(path)
    with pytest.raises(ValueError):
        store.list_blocked()

    # Nothing removed, because nothing about the old contents is known.
    assert store.clear() == 0
    assert store.list_blocked() == []
    store.assert_importable(SHA_A)
    # And the repaired record takes new entries normally.
    store.block(sha256=SHA_A, reason="after repair")
    assert [entry.sha256 for entry in store.list_blocked()] == [SHA_A]


def test_a_corrupt_record_is_reported_rather_than_silently_empty(tmp_path: Path):
    path = tmp_path / "blocked_tables.json"
    path.write_text(json.dumps({"schema": 99, "tables": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        TableBlocklist(path).list_blocked()


def test_a_duplicate_entry_is_refused_rather_than_resolved_by_order(tmp_path: Path):
    path = tmp_path / "blocked_tables.json"
    entry = {
        "sha256": SHA_A, "reason": "x", "filename": None, "app_id": None,
        "game_name": None, "game_version": None, "recorded_at": 1,
    }
    path.write_text(json.dumps({"schema": 1, "tables": [entry, entry]}), encoding="utf-8")
    with pytest.raises(ValueError):
        TableBlocklist(path).list_blocked()


def test_each_entry_carries_why_it_is_here(tmp_path: Path):
    # Three different statements about a row, and a user deciding what to do
    # about it acts on each differently. The reason beside them is a sentence
    # assembled from what Cheat Engine or the archive layer said, so it is the
    # wrong thing for a screen to classify by; the writer of the record is the
    # one place that knows.
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="it went straight back off", cause=CAUSE_REFUSED)
    store.block(sha256=SHA_B, reason="there is no table in the archive", cause=CAUSE_UNUSABLE)
    store.block_row(origin="fearless:topic-1:attachment-2", reason="the source no longer has it")

    causes = {entry.key: entry.cause for entry in store.list_blocked()}
    assert causes[SHA_A] == CAUSE_REFUSED
    assert causes[SHA_B] == CAUSE_UNUSABLE
    assert causes["row:fearless:topic-1:attachment-2"] == CAUSE_GONE

    # And it survives the round trip through the file rather than living only in
    # the object the write returned.
    reread = TableBlocklist(store.path)
    assert {entry.key: entry.cause for entry in reread.list_blocked()} == causes


def test_the_release_a_source_stated_is_kept_with_the_record(tmp_path: Path):
    """The mark outlives the file, and one line has to tell two revisions apart.

    A post carries every revision of one table under one filename and one title,
    so months later `Neon Bazaar - NeonBazaar113.CT` names two records
    with nothing between them. The release search offered the bytes under is
    what does, and it cannot be recovered from the device once the table itself
    has been deleted.
    """
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="a cheat went straight back off",
                table_version="1.05.01", cause=CAUSE_REFUSED)
    held = TableBlocklist(store.path).find(SHA_A)
    assert held is not None and held.table_version == "1.05.01"
    assert held.as_dict()["table_version"] == "1.05.01"

    # And nothing is invented for a record no source stated one for. The `.CT`
    # file's own `CheatEngineTableVersion` is the version of Cheat Engine's
    # table format, not of the table, so it is never a fallback.
    store.block(sha256=SHA_B, reason="a cheat went straight back off", cause=CAUSE_REFUSED)
    quiet = TableBlocklist(store.path).find(SHA_B)
    assert quiet is not None and quiet.table_version is None


def test_a_cause_this_build_does_not_know_leaves_the_record_usable(tmp_path: Path):
    # Fail-soft, unlike every other field here: the cause decides which two
    # words a chip says, and refusing the entry over it would drop the
    # protection the record exists for to keep a label honest.
    path = tmp_path / "blocked.json"
    path.write_text(json.dumps({"schema": 4, "failure_floor": "0" * 32, "failure_epochs": {}, "tables": [{
        "sha256": "a" * 64, "reason": "did not switch on", "filename": None,
        "app_id": None, "game_name": None, "game_version": None,
        "recorded_at": 1700000000, "origins": [], "cause": "something-later",
    }]}), encoding="utf-8")
    store = TableBlocklist(path)
    entry = store.find("a" * 64)
    assert entry is not None and entry.cause == CAUSE_UNKNOWN
    with pytest.raises(BlockedTableError):
        store.assert_importable("a" * 64)


@pytest.mark.parametrize("later", ["unusable", "encrypted", "gone"])
def test_a_later_condition_never_lifts_a_proven_failure(tmp_path, later):
    # The same bytes can be described twice: a cheat from them ran and did not
    # work, and a stricter parser afterwards decides the file is not a table.
    # The second is a statement about the file and does not undo the first, and
    # letting it replace the record lifted the refusal the user never cleared.
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="a cheat went straight back off", cause=CAUSE_REFUSED,
                origins=["fearless:topic-1:attachment-2"])
    store.block(sha256=SHA_A, reason="there is no table in it", cause=later,
                origins=["vgtimes:game-1:file-3"])
    held = store.find(SHA_A)
    assert held is not None and held.cause == CAUSE_REFUSED
    assert held.reason == "a cheat went straight back off"
    # The row the later attempt came through is how the mark is recognised on a
    # source that advertises no digest, so it is kept.
    assert held.origins == ("fearless:topic-1:attachment-2", "vgtimes:game-1:file-3")

    # The definite failure may still arrive after a condition, and it wins.
    store.unblock(SHA_A)
    store.block(sha256=SHA_A, reason="there is no table in it", cause=later)
    store.block(sha256=SHA_A, reason="a cheat went straight back off", cause=CAUSE_REFUSED)
    refined = store.find(SHA_A)
    assert refined is not None and refined.cause == CAUSE_REFUSED

    # Cleared explicitly, a later condition stands on its own.
    store.unblock(SHA_A)
    store.block(sha256=SHA_A, reason="there is no table in it", cause=later)
    standing = store.find(SHA_A)
    assert standing is not None and standing.cause == later


@pytest.mark.parametrize("bulk", [False, True])
def test_clearing_a_legacy_failure_leaves_its_old_success_needing_a_retest(tmp_path, bulk):
    # A record from a build that did not write its cause down is a compatibility
    # failure, and one can arrive with no epoch of its own. Clearing it used to
    # leave the success it had invalidated reading as valid again, which is the
    # one thing clearing must never do.
    store = _blocklist(tmp_path)
    store.path.write_text(json.dumps({"schema": 4, "failure_floor": "0" * 32, "failure_epochs": {}, "tables": [{
        "sha256": SHA_A, "reason": "did not switch on", "filename": None,
        "app_id": None, "game_name": None, "game_version": None,
        "recorded_at": 1700000000, "origins": [], "cause": "something-later",
    }]}), encoding="utf-8")
    floor, epochs = store.failure_epochs()
    # What positive evidence written before the failure would be carrying.
    carried = epochs.get(SHA_A, floor)
    if bulk:
        store.clear()
    else:
        store.unblock(SHA_A)
    after_floor, after_epochs = store.failure_epochs()
    assert store.find(SHA_A) is None
    assert after_epochs.get(SHA_A, after_floor) != carried


@pytest.mark.parametrize("cause", ["unusable", "encrypted", "gone"])
def test_clearing_a_condition_leaves_the_evidence_it_never_touched(tmp_path, cause):
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="there is no table in it", cause=cause)
    floor, epochs = store.failure_epochs()
    store.unblock(SHA_A)
    after_floor, after_epochs = store.failure_epochs()
    assert (after_floor, after_epochs) == (floor, epochs)


def test_failure_epoch_eviction_and_clear_cannot_restore_old_success_token(tmp_path):
    store = _blocklist(tmp_path)
    store.path.write_text(json.dumps({"schema": 4, "tables": [], "failure_floor": "0" * 32,
        "failure_epochs": {f"{number:064x}": "1" * 32 for number in range(4096)}}))
    store.block(sha256=SHA_A, reason="failed", cause=CAUSE_REFUSED)
    floor, epochs = store.failure_epochs()
    assert floor != "0" * 32 and len(epochs) == 4096
    token = epochs[SHA_A]
    store.clear()
    assert store.find(SHA_A) is None
    # Clearing the mark restores every path it stood in and not the success it
    # stood over: the evidence that was invalidated by the failure stays
    # invalidated until a cheat proves the table again.
    cleared_floor, cleared_epochs = store.failure_epochs()
    assert cleared_floor == floor and len(cleared_epochs) == len(epochs)
    assert cleared_epochs[SHA_A] != token
    assert {key: value for key, value in cleared_epochs.items() if key != SHA_A} \
        == {key: value for key, value in epochs.items() if key != SHA_A}
    store.block(sha256=SHA_A, reason="failed again", cause=CAUSE_REFUSED)
    assert store.failure_epochs()[1][SHA_A] not in (token, cleared_epochs[SHA_A])


def test_clear_has_one_directory_durability_boundary(tmp_path, monkeypatch):
    import os
    import stat
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="failed")
    original = os.fsync
    directories = []
    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directories.append(fd)
            if len(directories) > 1:
                raise OSError("redundant directory sync")
        original(fd)
    monkeypatch.setattr(os, "fsync", sync)
    assert store.clear() == 1
    assert len(directories) == 1
    assert store.find(SHA_A) is None


def test_clear_preserves_post_replace_uncertainty(tmp_path, monkeypatch):
    from ce_decky.atomic import DurabilityUnknownError
    store = _blocklist(tmp_path)
    store.block(sha256=SHA_A, reason="failed")
    def fail_sync(path):
        raise OSError("directory sync failed")
    monkeypatch.setattr("ce_decky.atomic.fsync_directory", fail_sync)
    with pytest.raises(DurabilityUnknownError):
        store.clear()
    assert store.find(SHA_A) is None
