"""The panel's record, kept where the renderer holding it cannot take it away.

The panel's own ring buffer lives in the browser view Steam gives the Quick
Access panel, and one class of defect destroys it as a condition of being
recovered: a wedged panel is fixed by restarting Steam's webhelper, which
replaces that renderer. The 2026-09-12 panel-close incident was recovered that
way and the support bundle collected afterwards reported `frontend_entries=0`.

So these cases are about the second copy: that the panel's entries are written
down as they happen, that the file cannot grow without bound, that it keeps the
end rather than the beginning when it is trimmed, that a flush never fails a
press, and that what it holds actually reaches the support bundle. The last is
what makes it evidence at all.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import zipfile
from pathlib import Path

from ce_decky import frontend_journal
from ce_decky.atomic import atomic_write_bytes
from ce_decky.paths import PluginPaths
from ce_decky.service import PluginService


def _service(tmp_path: Path) -> tuple[PluginService, PluginPaths]:
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("frontend-journal-test"))
    service.initialize()
    return service, paths


def _entries(count: int, *, event: str = "panel.action") -> list[dict[str, object]]:
    return [
        {
            "at": f"2026-09-12T01:2{index % 10}:00Z",
            "level": "info",
            "event": event,
            "fields": {"index": str(index)},
        }
        for index in range(count)
    ]


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_panel_flush_is_written_where_it_outlives_the_renderer(tmp_path: Path):
    service, paths = _service(tmp_path)

    answer = service.record_panel_log(_entries(3), 0, "sess0001")

    assert answer == {"ok": True, "accepted": 3}
    records = _read(frontend_journal.journal_path(paths.state_root))
    assert [record["event"] for record in records] == ["panel.action"] * 3
    # The session groups one frontend lifetime. A different value in the same
    # file is what says the renderer was replaced, which is the difference
    # between a panel that closed and one that was restarted out of a wedge.
    assert {record["session"] for record in records} == {"sess0001"}


def test_flushes_accumulate_rather_than_replacing_each_other(tmp_path: Path):
    service, paths = _service(tmp_path)

    service.record_panel_log(_entries(2, event="panel.mounted"), 0, "sess0001")
    service.record_panel_log(_entries(2, event="panel.dismounted"), 0, "sess0001")

    records = _read(frontend_journal.journal_path(paths.state_root))
    assert [record["event"] for record in records] == [
        "panel.mounted", "panel.mounted", "panel.dismounted", "panel.dismounted",
    ]


def test_a_flush_that_is_not_a_log_is_refused_without_writing_anything(tmp_path: Path):
    service, paths = _service(tmp_path)

    assert service.record_panel_log("not a list", 0, "sess0001") == {"ok": True, "accepted": 0}
    assert service.record_panel_log(None, 0, "") == {"ok": True, "accepted": 0}
    assert not frontend_journal.journal_path(paths.state_root).exists()


def test_a_secret_shaped_field_never_reaches_the_durable_record(tmp_path: Path):
    service, paths = _service(tmp_path)

    service.record_panel_log([{
        "at": "2026-09-12T01:27:00Z",
        "level": "error",
        "event": "panel.import_failed",
        "fields": {"password": "hunter2", "archive_token": "abcd", "member": "table.CT"},
    }], 0, "sess0001")

    body = frontend_journal.journal_path(paths.state_root).read_text(encoding="utf-8")
    assert "hunter2" not in body
    assert "abcd" not in body
    # The presence of the field is the evidence, and is kept.
    assert "password" in body
    assert "table.CT" in body


def test_the_record_is_trimmed_from_the_front_so_the_newest_survives(tmp_path: Path):
    path = tmp_path / "state" / "frontend-journal.jsonl"
    path.parent.mkdir(parents=True)
    # One append deliberately larger than the trim trigger, so exactly one
    # rewrite happens and the test does not depend on how many it takes. Each
    # record is numbered, so which end survived is a fact rather than a guess.
    bulk = [
        {"at": "2026-09-12T01:00:00Z", "level": "info", "event": f"panel.filler_{index}",
         "fields": {"pad": "p" * 4000}}
        for index in range(200)
    ]
    assert len(bulk) <= frontend_journal.MAX_FLUSH_ENTRIES
    frontend_journal.append_entries(path, bulk)
    assert path.stat().st_size > 0
    frontend_journal.append_entries(path, [{
        "at": "2026-09-12T01:27:59Z", "level": "error", "event": "panel.last", "fields": {},
    }])

    assert path.stat().st_size <= frontend_journal.TRIM_TRIGGER_BYTES
    records = _read(path)
    # Whole records only: a trim that cut one in half would fail the parse above.
    assert records[-1]["event"] == "panel.last"
    # The end is what a wedge is diagnosed from, so the front is what goes.
    events = [record["event"] for record in records]
    assert "panel.filler_0" not in events
    assert "panel.filler_199" in events


def test_the_record_stays_private_to_its_user_across_a_trim(tmp_path: Path):
    """A trim rewrites the file, and the rewrite decides the mode it keeps.

    The file is created `0600` on purpose: it holds what the panel did, which is
    game names, table identities and the reasons for refusals, and it sits in
    the plugin state directory for as long as the device keeps it. The trim used
    to write its replacement with the process umask, so on an ordinary `022`
    device the first one published the journal as `0644` and nothing afterwards
    put it back: the appends that follow reopen the file rather than create it.
    """
    path = tmp_path / "state" / "frontend-journal.jsonl"
    path.parent.mkdir(parents=True)
    umask = os.umask(0o022)
    try:
        bulk = [
            {"at": "2026-09-12T01:00:00Z", "level": "info", "event": f"panel.filler_{index}",
             "fields": {"pad": "p" * 4000}}
            for index in range(200)
        ]
        frontend_journal.append_entries(path, bulk)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        frontend_journal.append_entries(path, [{
            "at": "2026-09-12T01:27:59Z", "level": "error", "event": "panel.last", "fields": {},
        }])
    finally:
        os.umask(umask)

    assert path.stat().st_size <= frontend_journal.TRIM_TRIGGER_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # And no readable leftover of the replacement beside it.
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


def test_a_short_write_finishes_rather_than_reporting_bytes_that_never_landed(tmp_path: Path, monkeypatch):
    """`os.write` may write part of what it was given and return that count.

    It does not raise when it does, so one call taken for the whole write
    reported a flush as kept while its tail was never on disk. The panel retires
    an entry from its hand-over queue on that answer, so the record would be
    gone from the live ring and from the file at the same moment.
    """
    path = tmp_path / "frontend-journal.jsonl"
    real_write = os.write
    calls: list[int] = []

    def short_once(handle: int, data, /):
        offered = len(data)
        calls.append(offered)
        # The first call takes a few bytes and stops; the rest complete.
        return real_write(handle, bytes(data)[:7] if len(calls) == 1 else data)

    monkeypatch.setattr(frontend_journal.os, "write", short_once)
    written = frontend_journal.append_entries(path, _entries(4), session="sess0001")

    assert written == 4
    assert len(calls) > 1, "the short write was not exercised"
    records = _read(path)
    assert len(records) == 4
    assert [record["fields"]["index"] for record in records] == ["0", "1", "2", "3"]


def test_a_write_that_makes_no_progress_is_refused_rather_than_claimed(tmp_path: Path, monkeypatch):
    """The other end of the same rule, answered where the panel reads it.

    A target that takes none of the bytes offered must not be looped on and must
    not be reported as a flush that was kept: the panel keeps its entries and
    sends them again. `tests/supportFlush.test.ts` holds the frontend half, that
    a hand-over answered `ok: false` leaves the cursor where it was.
    """
    service, _paths = _service(tmp_path)
    monkeypatch.setattr(frontend_journal.os, "write", lambda handle, data, /: 0)

    answer = service.record_panel_log(_entries(2), 0, "sess0001")

    assert answer["ok"] is False
    assert answer["accepted"] == 0


def test_a_partial_record_never_swallows_the_records_appended_after_it(tmp_path: Path, monkeypatch):
    """What a failed write leaves behind, and what it must not take with it.

    The bytes that did land are half a JSON line. Without a newline after them
    the next append joins onto that half, which turns one lost record into two.
    """
    path = tmp_path / "frontend-journal.jsonl"
    real_write = os.write
    stage = 0

    def stop_part_way(handle: int, data, /):
        # A short write, and then a device that has run out. This is the order a
        # real one fails in: `write` reports what it took, and only the call
        # after it raises, because a raise is a syscall that wrote nothing.
        nonlocal stage
        if stage == 0:
            stage = 1
            return real_write(handle, bytes(data)[:40])
        if stage == 1:
            stage = 2
            raise OSError(28, "No space left on device")
        return real_write(handle, data)

    monkeypatch.setattr(frontend_journal.os, "write", stop_part_way)
    try:
        frontend_journal.append_entries(path, _entries(3), session="sess0001")
    except OSError:
        pass
    monkeypatch.setattr(frontend_journal.os, "write", real_write)
    frontend_journal.append_entries(path, _entries(1, event="panel.after"), session="sess0001")

    lines = path.read_text(encoding="utf-8").splitlines()
    # One unreadable line, and the record appended afterwards is whole.
    assert sum(1 for line in lines if not _parsable(line)) == 1
    records = _read_parsable(path)
    assert records[-1]["event"] == "panel.after"


def _parsable(line: str) -> bool:
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def _read_parsable(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and _parsable(line)
    ]


def test_the_summary_names_the_last_thing_the_panel_did(tmp_path: Path):
    path = tmp_path / "frontend-journal.jsonl"
    frontend_journal.append_entries(path, [
        {"at": "2026-09-12T01:26:00Z", "level": "info", "event": "panel.mounted", "fields": {}},
        {"at": "2026-09-12T01:27:58Z", "level": "info", "event": "panel.visibility_changed", "fields": {}},
    ])

    summary = frontend_journal.summarize(path)

    assert summary["present"] is True
    assert summary["entries"] == 2
    # This field is the whole point: a panel that closed cleanly ends on
    # `panel.dismounted`, and one killed while wedged ends on anything else.
    assert summary["last_event"] == "panel.visibility_changed"
    assert summary["last_at"] == "2026-09-12T01:27:58Z"


def test_the_summary_of_a_record_that_does_not_exist_is_not_an_error(tmp_path: Path):
    summary = frontend_journal.summarize(tmp_path / "absent.jsonl")
    assert summary["present"] is False
    assert summary["last_event"] is None


def test_the_flushed_record_reaches_the_support_bundle(tmp_path: Path):
    service, _ = _service(tmp_path)
    service.record_panel_log([
        {"at": "2026-09-12T01:26:00Z", "level": "info", "event": "panel.mounted", "fields": {}},
        {"at": "2026-09-12T01:27:58Z", "level": "warning", "event": "panel.search_stalled", "fields": {}},
    ], 0, "sess0001")

    # Deliberately collected with an empty live ring, which is exactly the state
    # a bundle is in after the renderer holding it was replaced.
    result = service.create_support_bundle([], 0)

    with zipfile.ZipFile(Path(str(result["path"]))) as archive:
        body = archive.read("logs/frontend-journal.jsonl").decode("utf-8")
        summary = json.loads(archive.read("diagnostics/frontend_journal.json"))
        manifest = json.loads(archive.read("manifest.json"))
    assert "panel.search_stalled" in body
    assert summary["last_event"] == "panel.search_stalled"
    # The manifest is what tells a reader the two records disagree, and that the
    # bundle was therefore taken after the panel's own copy was destroyed: an
    # empty live ring beside a journal that is not empty.
    assert manifest["frontend_entries"] == 0
    assert manifest["frontend_journal_entries"] == 2
    assert manifest["frontend_journal_last_event"] == "panel.search_stalled"


def test_the_bundle_says_so_when_the_panel_has_flushed_nothing(tmp_path: Path):
    service, _ = _service(tmp_path)

    result = service.create_support_bundle([], 0)

    notes = [note for note in result["notes"] if note["member"] == "logs/frontend-journal.jsonl"]
    assert notes and "not flushed" in notes[0]["reason"]


def _entry(event: str) -> dict[str, object]:
    return {"at": "2026-09-13T15:00:00.000Z", "level": "info", "event": event, "fields": {}}


def test_a_boundary_reads_back_only_what_was_appended_after_it(tmp_path: Path):
    """Where the file ended, for a reader that cannot trust the clock in it.

    This record outlives the renderer that wrote it, and every moment in it is
    that renderer's own wall clock. A reader asking what one reload produced
    cannot filter on those: a clock corrected backwards leaves the previous
    frontend's entries stamped later than the clock the reader holds. Where the
    file ended is not a claim about time.
    """
    path = frontend_journal.journal_path(tmp_path)
    frontend_journal.append_entries(path, [_entry("panel.mounted")], session="first")
    boundary = frontend_journal.journal_position(path)
    frontend_journal.append_entries(path, [_entry("panel.dismounted")], session="first")

    appended = frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)

    assert appended["valid"] is True and appended["complete"] is True
    events = [json.loads(line)["event"] for line in bytes(appended["data"]).splitlines()]
    assert events == ["panel.dismounted"]


def test_a_boundary_taken_before_the_record_exists_reads_everything_after_it(tmp_path: Path):
    path = frontend_journal.journal_path(tmp_path)
    boundary = frontend_journal.journal_position(path)
    assert boundary["present"] is False

    # Nothing yet: valid, and empty, rather than a boundary that was lost.
    appended = frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)
    assert appended == {"data": b"", "valid": True, "complete": True}

    frontend_journal.append_entries(path, [_entry("panel.mounted")], session="first")
    appended = frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)
    assert appended["valid"] is True
    assert [json.loads(line)["event"] for line in bytes(appended["data"]).splitlines()] == ["panel.mounted"]


def test_a_boundary_into_a_record_that_was_replaced_is_refused(tmp_path: Path):
    """The trim rewrites this file, and an offset into the old one means nothing."""
    path = frontend_journal.journal_path(tmp_path)
    frontend_journal.append_entries(path, [_entry("panel.mounted")], session="first")
    boundary = frontend_journal.journal_position(path)

    # What the trim does: keep the tail, write it somewhere else, rename it over.
    atomic_write_bytes(path, b'{"event":"panel.mounted"}\n', mode=frontend_journal.JOURNAL_MODE)

    assert frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)["valid"] is False

    # And a record that has gone entirely, which is the other way to lose one.
    path.unlink()
    assert frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)["valid"] is False


def test_more_appended_than_one_read_holds_is_said_to_be_incomplete(tmp_path: Path):
    """A reader pairing records needs both halves, so a short read is not a smaller answer."""
    path = frontend_journal.journal_path(tmp_path)
    boundary = frontend_journal.journal_position(path)
    frontend_journal.append_entries(path, [_entry("panel.mounted") for _ in range(20)], session="first")

    assert frontend_journal.read_appended(path, boundary, max_bytes=64)["complete"] is False
    assert frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)["complete"] is True


def test_a_replacement_between_naming_the_record_and_reading_it_is_refused(tmp_path: Path, monkeypatch):
    """The writer is another process, so nothing here excludes its trim.

    A name checked and then opened is a check of one file and a read of
    another. The replacement a trim leaves keeps the tail, so it can be long
    enough to reach the old boundary, and its retained records would then be
    read as newly appended ones - which is the opposite of what a boundary that
    no longer describes the file is supposed to produce.
    """
    path = frontend_journal.journal_path(tmp_path)
    frontend_journal.append_entries(path, [_entry("panel.mounted") for _ in range(8)], session="first")
    boundary = frontend_journal.journal_position(path)
    frontend_journal.append_entries(path, [_entry("panel.dismounted")], session="first")

    real_open = Path.open

    def replace_then_open(self, *args, **kwargs):
        # Exactly the window: the caller has decided which path to read, and the
        # trim lands before the bytes come from anywhere.
        if self == path:
            monkeypatch.undo()
            atomic_write_bytes(
                path,
                b"".join(json.dumps(_entry("panel.mounted")).encode("utf-8") + b"\n" for _ in range(20)),
                mode=frontend_journal.JOURNAL_MODE,
            )
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replace_then_open)

    appended = frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)

    assert appended["valid"] is False
    assert appended["data"] == b""


def test_a_replacement_after_the_record_is_open_is_read_as_the_file_it_opened(tmp_path: Path):
    """A descriptor already open is still the file the boundary was taken into.

    Reading it is safe, because those bytes are the ones the boundary describes.
    The replacement is seen by the next read rather than by this one.
    """
    path = frontend_journal.journal_path(tmp_path)
    frontend_journal.append_entries(path, [_entry("panel.mounted")], session="first")
    boundary = frontend_journal.journal_position(path)
    frontend_journal.append_entries(path, [_entry("panel.dismounted")], session="first")

    with path.open("rb"):
        atomic_write_bytes(path, b'{"event":"panel.mounted"}\n', mode=frontend_journal.JOURNAL_MODE)
        # The open handle above is the old inode; this call opens the path again
        # and gets the replacement, which the boundary no longer describes.
        assert frontend_journal.read_appended(path, boundary, max_bytes=64 * 1024)["valid"] is False
