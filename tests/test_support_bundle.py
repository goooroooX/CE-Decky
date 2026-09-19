"""The archive a bug report is attached to.

Every case here is one thing a maintainer has to be able to rely on when the
device is not available: that the archive exists at all when part of the plugin
state is broken, that it says what it could not read, that it stays inside its
size budget, and that it does not carry a secret out to a public issue.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import os
import logging
import threading
import zipfile

import pytest

from ce_decky import __version__
from ce_decky.catalog import CatalogService
from ce_decky.paths import PluginPaths
from ce_decky.service import PluginService
from ce_decky.support_bundle import (
    BUNDLE_PREFIX,
    MAX_RETAINED_BUNDLES,
    MAX_TOTAL_BYTES,
    create_support_bundle,
    managed_child,
    normalize_frontend_log,
    read_bounded,
)


CT_BYTES = (
    b'<CheatTable CheatEngineTableVersion="45"><CheatEntries>'
    b'<CheatEntry><ID>1</ID><Description>"Health"</Description>'
    b'<VariableType>4 Bytes</VariableType><Address>game.exe+10</Address></CheatEntry>'
    b'</CheatEntries></CheatTable>'
)


def _service(tmp_path: Path) -> tuple[PluginService, PluginPaths]:
    paths = PluginPaths.for_tests(tmp_path)
    service = PluginService(paths, logging.getLogger("support-bundle-test"))
    service.initialize()
    return service, paths


def _archive(result: dict[str, object]) -> zipfile.ZipFile:
    return zipfile.ZipFile(Path(str(result["path"])))


def test_bundle_collects_exact_artifact_resolution_provenance(tmp_path: Path):
    service, _ = _service(tmp_path)
    service.artifact_resolutions.record("vgtimes", "game-a:file-1", "a" * 64, "b" * 64)
    service.table_compatibility.record(10, "b" * 64, "game.exe", {"pe_version": "1"}, failure_epoch="0" * 32)
    with _archive(service.create_support_bundle([], 0)) as archive:
        payload = json.loads(archive.read("state/artifact_resolutions.json"))
        assert payload["entries"][0]["table_sha256"] == "b" * 64
        compatibility = json.loads(archive.read("state/table_compatibility.json"))
        assert compatibility["entries"][0]["pe_version"] == "1"


def test_bundle_lands_in_the_user_home_with_the_files_a_report_needs(tmp_path: Path):
    service, paths = _service(tmp_path)
    (paths.log_dir / "2026-09-01 10.00.00.log").write_text("activity event=backend.initialized\n", encoding="utf-8")
    (paths.state_root / "ce-launch").mkdir(parents=True, exist_ok=True)
    (paths.state_root / "ce-launch" / "10.log").write_text("wine: could not load\n", encoding="utf-8")
    github_index = {"schema": 1, "queries": [{"query": "Example", "fetched": 1, "used": 1, "items": []}]}
    (paths.cache_root / "github-repository-index.json").write_text(json.dumps(github_index), encoding="utf-8")

    result = service.create_support_bundle([], 0)

    written = Path(str(result["path"]))
    assert written.parent == paths.user_home
    assert written.name.startswith(f"{BUNDLE_PREFIX}-{__version__}-")
    assert written.suffix == ".zip"
    with _archive(result) as archive:
        names = set(archive.namelist())
        # The three files a person opens first, then the evidence itself.
        assert {"README.txt", "summary.txt", "manifest.json"} <= names
        assert "diagnostics/status.json" in names
        assert "diagnostics/self_test.json" in names
        assert "diagnostics/environment.json" in names
        assert "logs/frontend.json" in names
        assert "state/config.json" in names
        assert json.loads(archive.read("state/github-repository-index.json")) == github_index
        assert "logs/plugin/2026-09-01 10.00.00.log" in names
        assert "logs/ce-launch/10.log" in names
        assert b"could not load" in archive.read("logs/ce-launch/10.log")
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == __version__
        assert manifest["archive"] == written.name


def test_bundle_carries_the_search_marker_the_background_pass_turns_on(tmp_path: Path):
    """Whether the listing refresh was armed is a question about one small file.

    The marker rides in the listing index as well, and that file is a third of a
    megabyte and is deliberately not collected, so without this one a report
    cannot say whether the background pass was owed at all - and a crawl that
    was never owed a pass reads exactly like a crawl that was broken.

    Written through the catalog itself rather than by hand, which is what holds
    the collected path and the file the plugin writes equal.
    """
    service, paths = _service(tmp_path)
    catalog = CatalogService(None, paths.cache_root / "provider-results.json")  # type: ignore[arg-type]
    # One search, and nothing else: the marker is written as it happens.
    catalog._note_search_activity()
    assert catalog._searched_persisted_at == catalog._searched_at

    with _archive(service.create_support_bundle([], 0)) as archive:
        payload = json.loads(archive.read("state/fearless-search.json"))
    assert payload["searched"] == pytest.approx(catalog._searched_persisted_at)


def test_bundle_is_still_written_when_plugin_state_cannot_be_read(tmp_path: Path):
    """The unreadable file is normally the defect, so it must not also block the report."""
    service, paths = _service(tmp_path)
    (paths.state_root / "profiles.json").write_text("{ this is not json", encoding="utf-8")

    result = service.create_support_bundle([], 0)

    with _archive(result) as archive:
        status = json.loads(archive.read("diagnostics/status.json"))
        assert status["profile_state_reason"]
        assert status["plugin_dir"] == str(paths.plugin_dir)
        # The corrupt bytes themselves travel with the report, because what is
        # in the file is the question a maintainer will actually have.
        assert archive.read("state/profiles.json").startswith(b"{ this is not json")
        assert "PASS" in archive.read("summary.txt").decode("utf-8") or "FAIL" in archive.read("summary.txt").decode("utf-8")


def test_bundle_records_why_a_member_is_missing_rather_than_dropping_it_silently(tmp_path: Path):
    service, paths = _service(tmp_path)
    # A symlink where a state file belongs is refused by every bounded read in
    # this plugin, and the refusal is the interesting part.
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    (paths.state_root / "blocked_tables.json").symlink_to(target)

    result = service.create_support_bundle([], 0)

    notes = {str(note["member"]): str(note["reason"]) for note in result["notes"]}
    assert "state/blocked_tables.json" in notes
    assert "symlink" in notes["state/blocked_tables.json"]
    with _archive(result) as archive:
        assert "state/blocked_tables.json" not in archive.namelist()
        assert json.loads(archive.read("manifest.json"))["notes"]


def test_panel_log_is_carried_through_with_its_shape_enforced(tmp_path: Path):
    service, _ = _service(tmp_path)
    entries = [
        {"at": "2026-09-01T10:00:00Z", "level": "error", "event": "panel.action_failed",
         "fields": {"action": "activateTable", "message": "no such table"}},
        "not an entry",
        {"at": "2026-09-01T10:00:01Z", "level": "nonsense", "event": "panel.mounted", "fields": None},
    ]

    result = service.create_support_bundle(entries, 3)

    with _archive(result) as archive:
        panel = json.loads(archive.read("logs/frontend.json"))
    assert panel["dropped"] == 3
    assert [item["event"] for item in panel["entries"]] == ["panel.action_failed", "panel.mounted"]
    assert panel["entries"][0]["fields"]["message"] == "no such table"
    # An unknown level is recorded as ordinary rather than passed through.
    assert panel["entries"][1]["level"] == "info"


def test_panel_log_arguments_that_are_not_a_log_produce_an_empty_one(tmp_path: Path):
    service, _ = _service(tmp_path)

    result = service.create_support_bundle("not a list", "not a number")

    with _archive(result) as archive:
        panel = json.loads(archive.read("logs/frontend.json"))
    assert panel == {"dropped": 0, "entries": []}


def test_user_interaction_and_operation_outcome_survive_bundle_collection(tmp_path: Path):
    service, _ = _service(tmp_path)
    fields = {"action": "imported_tables_modal.use", "interaction": "17", "table_sha": "a" * 64}
    entries = [
        {"at": "2026-09-09T10:00:00Z", "level": "info", "event": "ui.action", "fields": fields},
        {"at": "2026-09-09T10:00:01Z", "level": "error", "event": "ui.operation_failed",
         "fields": {**fields, "operation": "manage.select", "message": "write refused"}},
    ]
    result = service.create_support_bundle(entries, 7)
    with _archive(result) as archive:
        panel = json.loads(archive.read("logs/frontend.json"))
    assert panel == {"entries": entries, "dropped": 7}


def test_normalize_frontend_log_strips_control_characters_and_bounds_fields():
    entries = normalize_frontend_log([
        {"at": "x", "level": "info", "event": "e‮en", "fields": {"k": "a\nb" + "c" * 1000}},
    ])
    assert "‮" not in entries[0]["event"]
    assert "\n" not in entries[0]["fields"]["k"]
    assert len(entries[0]["fields"]["k"]) <= 320


def test_archive_privacy_statement_is_in_the_archive_itself(tmp_path: Path):
    """A user is told what they are about to publish inside the thing they publish."""
    service, _ = _service(tmp_path)

    result = service.create_support_bundle([], 0)

    with _archive(result) as archive:
        readme = archive.read("README.txt").decode("utf-8")
    assert "No password" in readme
    assert "GitHub issue" in readme
    assert "tables/" in readme


def test_older_bundles_are_pruned_so_the_home_directory_does_not_fill_up(tmp_path: Path):
    """Reproducing a problem takes several presses, and each one writes a file."""
    service, paths = _service(tmp_path)
    for index in range(MAX_RETAINED_BUNDLES + 3):
        stale = paths.user_home / f"{BUNDLE_PREFIX}-{__version__}-2026010{index}-000000.zip"
        stale.write_bytes(b"PK\x05\x06" + bytes(18))
        os.utime(stale, (1_700_000_000 + index, 1_700_000_000 + index))
    # Never removed: it is not one of ours, whatever its extension.
    keeper = paths.user_home / "my-save-backup.zip"
    keeper.write_bytes(b"PK\x05\x06" + bytes(18))

    result = service.create_support_bundle([], 0)

    remaining = sorted(path.name for path in paths.user_home.glob(f"{BUNDLE_PREFIX}-*.zip"))
    assert len(remaining) == MAX_RETAINED_BUNDLES
    # The one just written is newest, so it is always among the survivors.
    assert Path(str(result["path"])).name in remaining
    assert result["removed_older_bundles"]
    assert keeper.is_file()


def test_read_bounded_takes_the_tail_of_a_log_and_the_head_of_a_state_file(tmp_path: Path):
    path = tmp_path / "big.log"
    path.write_bytes(b"A" * 100 + b"B" * 100)

    tail, truncated_tail = read_bounded(path, max_bytes=100, tail=True)
    head, truncated_head = read_bounded(path, max_bytes=100, tail=False)

    assert tail == b"B" * 100 and truncated_tail is True
    assert head == b"A" * 100 and truncated_head is True
    assert read_bounded(tmp_path / "absent", max_bytes=10, tail=True) == (None, False)


def test_a_single_oversized_file_cannot_spend_the_whole_bundle_budget(tmp_path: Path):
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    # Well past the whole-bundle budget, in one file the collector would
    # otherwise take at its own per-file limit.
    (paths.log_dir / "huge.log").write_bytes(b"L" * (2 * 1024 * 1024))

    result = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None, provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
    )

    assert int(result["uncompressed_bytes"]) <= MAX_TOTAL_BYTES
    with _archive(result) as archive:
        # Truncated to the per-file tail limit rather than refused outright.
        assert len(archive.read("logs/plugin/huge.log")) == 1024 * 1024
    # In the archive and readable, so it is typed as trimmed rather than as a
    # member the reader has to be told is missing.
    truncated = [note for note in result["notes"] if note["kind"] == "truncated"]
    assert [note["member"] for note in truncated] == ["logs/plugin/huge.log"]


def test_the_bundle_path_is_never_written_through_a_symlink(tmp_path: Path):
    """A claimed name is created exclusively, so an existing link is never followed."""
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()

    def attempt() -> dict:
        return create_support_bundle(
            paths,
            status=None, diagnostics=None, self_test=None, session_inventory=None,
            runtime=[], launch_capabilities=[], provider_diagnostics=None, provider_sources=None, blocked_tables=None,
            frontend_log=[], frontend_dropped=0, version=__version__, now=1_800_000_000.0,
        )

    # Take the name the fixed clock produces, then plant a link at it.
    planted = Path(str(attempt()["path"]))
    planted.unlink()
    planted.symlink_to(elsewhere / "captured.zip")

    result = attempt()

    # The link is left exactly as it was found and the archive took a name of
    # its own, rather than being written through it into another directory.
    assert not (elsewhere / "captured.zip").exists()
    assert planted.is_symlink()
    assert Path(str(result["path"])) != planted
    with zipfile.ZipFile(Path(str(result["path"]))) as archive:
        assert archive.testzip() is None


def test_a_secret_shaped_field_never_reaches_the_archive_from_the_rpc(tmp_path: Path):
    """The panel redacts these before recording; this is the boundary saying so too.

    An RPC argument is a trust boundary whatever is expected on the other side
    of it, and the cost of being wrong is a credential inside a file the user
    attaches to a public issue.
    """
    service, _ = _service(tmp_path)
    entries = [{
        "at": "2026-09-01T10:00:00Z", "level": "info", "event": "panel.archive_opened",
        "fields": {
            "password": "hunter2", "api_key": "k-123", "Authorization": "Bearer abc",
            "session_cookie": "sid=9", "lock_token": "t-1", "member": "table.CT",
        },
    }]

    result = service.create_support_bundle(entries, 0)

    with _archive(result) as archive:
        raw = archive.read("logs/frontend.json").decode("utf-8")
        fields = json.loads(raw)["entries"][0]["fields"]
    for name in ("password", "api_key", "Authorization", "session_cookie", "lock_token"):
        assert fields[name] == "<redacted>", name
    assert fields["member"] == "table.CT"
    for secret in ("hunter2", "k-123", "Bearer abc", "sid=9", "t-1"):
        assert secret not in raw


def test_two_bundles_racing_for_the_same_name_both_survive(tmp_path: Path):
    """Asking whether a name is free and using it are two steps; a race runs them interleaved.

    Both writers saw the name free, both wrote, and the second replaced the
    first: two RPCs returned the same path and one archive was gone. A barrier
    is what makes this a race rather than a sequence, so the test would pass
    against the old code without one.
    """
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()
    # Deliberately under the retention limit: every finished bundle prunes the
    # oldest beyond it, and a racer whose archive is legitimately pruned would
    # look exactly like one that lost a name race.
    racers = MAX_RETAINED_BUNDLES - 1
    barrier = threading.Barrier(racers)
    produced: list[dict[str, object]] = []
    failures: list[BaseException] = []
    lock = threading.Lock()

    def collect() -> None:
        try:
            barrier.wait(timeout=30)
            result = create_support_bundle(
                paths,
                status=None, diagnostics=None, self_test=None, session_inventory=None,
                runtime=[], launch_capabilities=[], provider_diagnostics=None, provider_sources=None, blocked_tables=None,
                frontend_log=[], frontend_dropped=0, version=__version__,
                # One fixed instant, so every racer derives the same name and
                # they have to be separated by the claim rather than by a clock.
                now=1_800_000_000.0,
            )
        except BaseException as exc:  # noqa: BLE001 - reported on the main thread
            with lock:
                failures.append(exc)
            return
        with lock:
            produced.append(result)

    threads = [threading.Thread(target=collect) for _ in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not failures, failures
    written = [str(item["path"]) for item in produced]
    assert len(written) == racers
    # Every racer got a path of its own, and every path still holds a complete
    # archive naming itself rather than one another writer replaced.
    assert len(set(written)) == racers
    for path in written:
        with zipfile.ZipFile(Path(path)) as archive:
            assert archive.testzip() is None
            assert json.loads(archive.read("manifest.json"))["archive"] == Path(path).name
    # No staging file and no empty claim is left behind.
    assert not list(paths.user_home.glob(".*partial"))
    assert all(Path(path).stat().st_size > 0 for path in written)


def test_current_state_and_tables_survive_a_budget_spent_on_logs(tmp_path: Path):
    """A reload-heavy day of log tails must not crowd out the irreplaceable half.

    Logs can ask for far more of the one whole-bundle budget than everything
    else put together, and they are the one part that can be reconstructed from
    nothing else only in the sense that nobody needs the oldest of them.
    """
    service, paths = _service(tmp_path)
    digest = sha256(CT_BYTES).hexdigest()
    source = tmp_path / "table.CT"
    source.write_bytes(CT_BYTES)
    service.import_table(str(source))
    service.save_profile(10, "Example", False, digest, "game.exe")
    for index in range(25):
        (paths.log_dir / f"2026-09-01 {index:02d}.00.00.log").write_bytes(b"L" * (1024 * 1024))

    result = service.create_support_bundle([], 0)

    with _archive(result) as archive:
        names = set(archive.namelist())
    assert "state/config.json" in names
    assert "state/profiles.json" in names
    assert f"tables/{digest[:16]}.ct" in names
    assert int(result["uncompressed_bytes"]) <= MAX_TOTAL_BYTES


def test_a_symlinked_directory_inside_the_table_tree_is_never_followed(tmp_path: Path):
    """Only the final component is protected by the bounded reader's own O_NOFOLLOW."""
    service, paths = _service(tmp_path)
    digest = sha256(CT_BYTES).hexdigest()
    source = tmp_path / "table.CT"
    source.write_bytes(CT_BYTES)
    service.import_table(str(source))
    service.save_profile(10, "Example", False, digest, "game.exe")

    # Replace the digest directory with a link to somewhere else entirely.
    blob_dir = paths.tables_root / "sha256" / digest[:2] / digest
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "table.CT").write_bytes(b"not the managed table")
    for item in blob_dir.iterdir():
        item.unlink()
    blob_dir.rmdir()
    blob_dir.symlink_to(elsewhere)

    result = service.create_support_bundle([], 0)

    with _archive(result) as archive:
        assert f"tables/{digest[:16]}.ct" not in archive.namelist()
    assert any("symlink" in str(note["reason"]) for note in result["notes"])


def test_managed_child_refuses_a_symlinked_parent_and_allows_an_ordinary_one(tmp_path: Path):
    root = tmp_path / "root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "file").write_bytes(b"x")
    assert managed_child(root, "a", "b", "file").read_bytes() == b"x"
    # A missing final component is the caller's business, not a walk failure.
    assert not managed_child(root, "a", "b", "absent").exists()

    (root / "link").symlink_to(root / "a")
    with pytest.raises(ValueError, match="symlink"):
        managed_child(root, "link", "b", "file")
    with pytest.raises(ValueError, match="missing"):
        managed_child(root, "nope", "b", "file")


def test_the_bundle_says_which_table_sources_were_switched_off(tmp_path: Path):
    """A report that a game finds no tables is unanswerable without it.

    A source that was never asked and a source that was asked and found nothing
    produce the same empty answer in every other file in the archive, so the
    choice itself, the merged per-source view and a plain-text line all have to
    be there.
    """
    service, paths = _service(tmp_path)
    service.set_provider_enabled("github", False)

    result = service.create_support_bundle([], 0)

    with _archive(result) as archive:
        names = set(archive.namelist())
        assert {"diagnostics/provider_sources.json", "state/provider_sources.json"} <= names
        merged = json.loads(archive.read("diagnostics/provider_sources.json"))
        off = {row["provider"] for row in merged["sources"] if not row["enabled"]}
        assert off == {"github"}
        assert merged["enabled_count"] == merged["total"] - 1
        # The durable record itself, so the merged view can be checked against
        # what is actually on disk.
        assert json.loads(archive.read("state/provider_sources.json"))["disabled"] == ["github"]
        summary = archive.read("summary.txt").decode("utf-8")
        assert "Table sources" in summary
        assert "OFF github" in summary
        assert "on  fearless" in summary


def test_the_bundle_says_when_the_source_choice_could_not_be_read(tmp_path: Path):
    service, paths = _service(tmp_path)
    (paths.state_root / "provider_sources.json").write_text(
        json.dumps({"schema": 99, "disabled": []}), encoding="utf-8",
    )

    result = service.create_support_bundle([], 0)

    with _archive(result) as archive:
        merged = json.loads(archive.read("diagnostics/provider_sources.json"))
        assert merged["selection_reason"]
        # Fail-open, so every source still reports as on while it cannot be read.
        assert merged["enabled_count"] == merged["total"]
        assert "UNREADABLE source selection" in archive.read("summary.txt").decode("utf-8")


def test_the_bundle_says_which_of_the_backends_loops_was_awake(tmp_path: Path):
    """A report about a hot device or a flat battery is answered here or nowhere.

    CPU for the whole backend process is one figure over several loops, so it
    cannot say which of them ran. The counted paths reach the archive as their
    own field and as a plain-text block, and the archive is what a released bug
    report is made of.
    """
    from ce_decky import poll_counters

    service, _paths = _service(tmp_path)
    poll_counters.reset()
    try:
        for _ in range(3):
            with poll_counters.timed(poll_counters.SUPERVISOR_TARGET_SCAN):
                pass

        result = service.create_support_bundle([], 0)

        with _archive(result) as archive:
            counters = json.loads(archive.read("diagnostics/diagnostics.json"))["poll_counters"]
            assert set(counters["paths"]) == set(poll_counters.PATHS)
            assert counters["paths"][poll_counters.SUPERVISOR_TARGET_SCAN]["calls"] == 3
            summary = archive.read("summary.txt").decode("utf-8")
            assert "Backend polling" in summary
            assert f"{poll_counters.SUPERVISOR_TARGET_SCAN} calls=3" in summary
    finally:
        poll_counters.reset()


def test_startup_orphan_recovery_reaches_catalogue_and_support_log(tmp_path):
    service, paths = _service(tmp_path)
    source = tmp_path / "original.CT"
    source.write_bytes(CT_BYTES)
    artifact = service.table_store.import_ct(str(source))
    (service.table_store.meta_root / f"{artifact.sha256}.json").unlink()
    source.unlink()
    logger = logging.getLogger("orphan-recovery-support-test")
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(paths.log_dir / "recovery.log")
    logger.addHandler(handler)
    try:
        restarted = PluginService(paths, logger)
        restarted.initialize()
        handler.flush()
        result = restarted.create_support_bundle([], 0)
        with _archive(result) as bundle:
            status = json.loads(bundle.read("diagnostics/status.json"))
            recovered = next(row for row in status["tables"] if row["sha256"] == artifact.sha256)
            assert recovered["available"] is True
            assert recovered["origins"] == []
            assert recovered["imported_at"] is None
            assert any(b"table.orphan_recovered" in bundle.read(name) for name in bundle.namelist() if name.endswith(".log"))
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_bundle_carries_what_an_update_did_while_the_plugin_was_being_replaced(tmp_path: Path):
    """A self-update finishes in a process the plugin logs know nothing about.

    Decky stops this backend to replace it, so the installer runs detached and
    the only account of what it did is its own record plus the durable update
    file. A report about an update that failed halfway is written from these
    two or from nothing.
    """
    service, paths = _service(tmp_path)
    service.preferences.set(mascot_visible=False)
    service.plugin_updates.state.update(
        checked_at=1.0, latest_version="0.9.28", last_error=None,
        install={"version": "0.9.28", "started_at": 2.0},
    )
    (paths.log_dir / "plugin-update-runner.jsonl").write_text(
        '{"at":3.0,"event":"update_runner.started","version":"0.9.28"}\n', encoding="utf-8",
    )
    with _archive(service.create_support_bundle([], 0)) as archive:
        state = json.loads(archive.read("state/plugin-update.json"))
        preferences = json.loads(archive.read("state/preferences.json"))
        runner = archive.read("logs/plugin-update-runner.jsonl").decode("utf-8")
    assert state["latest_version"] == "0.9.28"
    assert state["install"]["version"] == "0.9.28"
    assert "update_runner.started" in runner
    assert preferences["mascot_visible"] is False
