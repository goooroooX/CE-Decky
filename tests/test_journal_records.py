"""Which journal lines are this plugin's, and which are not ours to publish.

The system journal is where this plugin's backend records actually survive: the
per-load log file Decky writes is deleted by the next install, which is why one
reported refusal in `docs/FIELD_NOTES.md` could not be diagnosed and why the
2026-09-12 panel-close bundle carried no backend log at all.

Every Decky plugin logs under the same identifier, though, and a support bundle
is attached to public issues. So the filter is the safety property here, and
these cases are about it: our own records are kept, the loader lifecycle lines
that say the renderer was replaced are kept because that is the whole question
for a wedge, and another plugin's output is dropped and counted rather than
published on its behalf.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from ce_decky import __version__, journal_records
from ce_decky.paths import PluginPaths
from ce_decky.support_bundle import MAX_SYSTEM_JOURNAL_BYTES, create_support_bundle


def _line(message: str, *, pid: int = 37554, identifier: str = "PluginLoader") -> str:
    return f"2026-09-12T01:24:31+02:00 steam-machine {identifier}[{pid}]: {message}"


def _ours(message: str) -> str:
    """One of this plugin's own records, exactly as `log_activity` writes it."""
    return f"{journal_records.ACTIVITY_PREFIX} {message}"


def test_our_own_activity_records_are_kept():
    assert journal_records._OURS.search(_ours("activity event=ce_launch.started app_id=10"))
    assert journal_records._message(
        _line(_ours("activity event=backend.initialized"))
    ) == _ours("activity event=backend.initialized")


def test_a_record_shape_another_plugin_could_write_does_not_prove_it_is_ours():
    """The prefix is the ownership statement, and it is the whole of it.

    Every plugin's output reaches this journal under the loader's identifier, so
    `activity event=` on its own says nothing about who wrote it. A plugin that
    happens to log in that shape used to be collected here and published, with
    whatever it carries, inside an archive attached to a public issue.
    """
    foreign = "activity event=sync.started path=/home/deck/private/secret-notes"
    assert not journal_records._OURS.search(foreign)
    assert not journal_records._NAMED.search(foreign)
    assert not journal_records._LIFECYCLE.match(foreign)


def test_naming_this_plugin_in_a_foreign_line_does_not_make_it_ours():
    """A line is kept for having the loader's shape, never for our name in it.

    The name occurs in another plugin's failure text as readily as in Decky's
    own, and that text carries that plugin's paths and that user's data.
    """
    for foreign in (
        "[Other] failure talking to CE Decky: /home/deck/private/secret-notes",
        "[Other] CE Decky (v0.9.24) was mentioned by somebody else entirely",
        "note: CE Decky is installed",
    ):
        assert not journal_records._NAMED.search(foreign), foreign
        assert not journal_records._OURS.search(foreign), foreign
        assert not journal_records._LIFECYCLE.match(foreign), foreign


def test_another_plugins_output_is_not_published_on_its_behalf():
    for foreign in (
        "[Wine Cask] 2026-09-12 01:28:27 INFO 127.0.0.1:43006 disconnected",
        "Fetching RSS feed from: https://example.invalid/feed.xml",
        "Found installed extensions: ['Epic']",
        "running cmd: python3 /home/deck/homebrew/plugins/Other/scripts/fetch.py",
    ):
        message = journal_records._message(_line(foreign))
        assert message is not None
        assert not journal_records._OURS.search(message)
        assert not journal_records._NAMED.search(message)
        assert not journal_records._LIFECYCLE.match(message), foreign


# Every shape this device's own journal carries for Decky's own halves, read
# over a week of development. The filter is written out line by line, so this is
# the list it has to keep matching, and a Decky that words one of them
# differently is a line dropped rather than a foreign line published.
LIFECYCLE_LINES = (
    "[localplatformlinux][INFO]: Restarting steamwebhelper",
    "[main][INFO]: CEF has disconnected...",
    "[injector][WARNING]: The Tab SharedJSContext socket has been disconnected while listening for messages.",
    "[main][INFO]: Loading Decky frontend!",
    "[main][INFO]: Starting Decky version v3.2.8",
    # The line the whole crash-window diagnosis turns on: three of these
    # inside a minute and Decky stops its own service.
    "[main][WARNING]: webhelper crashed within a minute from last crash! crash count: 3",
)
# The same for the lines Decky writes about this plugin by name.
NAMED_LINES = (
    "[loader][INFO]: Loaded CE Decky (v0.9.24)",
    "[loader][INFO]: Plugin CE Decky (v0.9.24) is already loaded and has requested to not be re-loaded",
    "[plugin][INFO]: Stopping response listener for CE Decky (v0.9.24)",
    "[plugin][INFO]: Shutting down CE Decky (v0.9.24)",
    "[plugin][INFO]: Plugin CE Decky (v0.9.24) has been stopped in 5.1s",
    "[plugin][WARNING]: Plugin CE Decky (v0.9.24) still alive 5 seconds after stop request! Sending SIGKILL!",
    "[loader][ERROR]: Method get_ce_launch_capability of plugin CE Decky (v0.9.24) failed with the following exception:",
)


def test_the_lines_that_say_the_renderer_was_replaced_are_kept():
    # These are what separate "the panel stopped" from "the renderer was
    # replaced under it", which is the whole question for a wedge. They carry no
    # plugin state and no user data.
    for lifecycle in LIFECYCLE_LINES:
        message = journal_records._message(_line(lifecycle))
        assert message is not None
        assert journal_records._LIFECYCLE.match(message), lifecycle
        assert journal_records.is_from_decky(_line(lifecycle)), lifecycle


def test_a_foreign_line_that_opens_with_a_phrase_decky_writes_is_not_published():
    """The phrase is not provenance either, and neither is the tag before it.

    A component tag and a level are plain message text: another plugin logging
    under this identifier prints its own `[main][INFO]: ` whenever it likes, and
    it may begin a message with any words it likes too. A rule that matched a
    phrase Decky writes and then accepted whatever followed therefore published
    that plugin's paths, game names and anything else it had put there, inside
    an archive attached to a public issue. Every kept shape is matched to the
    end of the message for exactly this reason.
    """
    for foreign in (
        "[main][INFO]: Restarting steamwebhelper path=/home/deck/private/foreign",
        "[main][INFO]: Loading Decky frontend! game=Private Game",
        "[main][INFO]: note CE Decky (v0.9.24) path=/home/deck/private/foreign",
        "[loader][INFO]: Loaded CE Decky (v0.9.24) from /home/deck/private/foreign",
        "[main][WARNING]: webhelper crashed within a minute from last crash! crash count: 3 in /home/deck/private",
        "[main][INFO]: CEF has disconnected... while syncing /home/deck/private/foreign",
    ):
        message = journal_records._message(_line(foreign))
        assert message is not None
        assert not journal_records._LIFECYCLE.match(message), foreign
        assert not journal_records._NAMED.match(message), foreign
        assert not journal_records._OURS.search(message), foreign
        assert not journal_records.is_from_decky(_line(foreign)), foreign


def test_the_exact_lines_this_device_carries_are_still_kept():
    """The other half of the rule above, and the reason it is written out.

    Bounding every shape to the end of the message is what keeps a foreign
    suffix out; it also means a phrase that was not written down is dropped. So
    the lines this device's journal actually holds are the regression, and the
    ones a bug report depends on are all of them.
    """
    for named in NAMED_LINES:
        message = journal_records._message(_line(named))
        assert message is not None
        assert journal_records._NAMED.match(message), named
        assert journal_records.is_from_decky(_line(named)), named


def test_a_foreign_line_is_not_kept_for_saying_webhelper(monkeypatch):
    """The component tag is text, and this rule keeps a line on it alone.

    Every other rule here requires a shape a foreign plugin cannot produce: our
    own record prefix, or the loader's tag with this plugin's name and the
    version it read. This one requires the tag and a phrase, so the phrase has
    to be one Decky writes rather than a word that can appear in anything. It
    used to accept any message from those components that mentioned a
    webhelper, which published another plugin's paths and game names in an
    archive attached to a public issue.
    """
    for foreign in (
        "[main][INFO]: killing webhelper for /home/deck/Games/Some Game/user.cfg",
        "[loader][INFO]: MyPlugin restarted the webhelper for user deck",
        "[main][INFO]: webhelper pid 1234 for /home/deck/.local/share/OtherPlugin",
    ):
        message = journal_records._message(_line(foreign))
        assert message is not None
        assert not journal_records._LIFECYCLE.match(message), foreign
        assert not journal_records._OURS.search(message), foreign
        assert not journal_records._NAMED.search(message), foreign


def test_lines_naming_this_plugin_are_kept():
    for named in (
        "[loader][INFO]: Plugin CE Decky (v0.9.24) is already loaded and has requested to not be re-loaded",
        "[plugin][INFO]: Shutting down CE Decky (v0.9.24)",
    ):
        message = journal_records._message(_line(named))
        assert message is not None
        assert journal_records._NAMED.search(message), named


def test_a_hostname_or_pid_never_decides_whether_a_line_is_ours():
    # The message is matched on its own, so a host called "CE Decky" or a
    # process id that happens to spell something does not admit a foreign line.
    message = journal_records._message(
        _line("[Wine Cask] nothing of ours", identifier="PluginLoader", pid=1709)
    )
    assert message == "[Wine Cask] nothing of ours"
    assert not journal_records._NAMED.search(message)


def test_a_line_that_is_not_a_journal_line_is_dropped_rather_than_guessed_at():
    assert journal_records._message("not a journal line at all") is None
    assert journal_records._message("") is None


def test_a_host_without_journalctl_reports_that_instead_of_failing(monkeypatch):
    monkeypatch.setattr(journal_records.shutil, "which", lambda _name: None)
    assert journal_records.available() is False
    collected = journal_records.collect()
    assert collected["ok"] is False
    assert collected["lines"] == []
    assert "journalctl" in str(collected["reason"])


def test_a_journalctl_that_fails_is_a_reason_rather_than_an_exception(monkeypatch):
    class _Completed:
        returncode = 1
        stdout = b""
        stderr = b"Failed to add match: Invalid argument"

    monkeypatch.setattr(journal_records.shutil, "which", lambda _name: "/usr/bin/journalctl")
    monkeypatch.setattr(journal_records.subprocess, "run", lambda *a, **k: _Completed())

    collected = journal_records.collect()

    assert collected["ok"] is False
    assert collected["lines"] == []
    assert "exited 1" in str(collected["reason"])


def test_collect_keeps_ours_and_counts_what_it_dropped(monkeypatch):
    stdout = "\n".join([
        _line(_ours("activity event=ce_launch.started app_id=10")),
        _line("[Wine Cask] 2026-09-12 01:28:27 INFO 127.0.0.1:43006 disconnected"),
        _line("[localplatformlinux][INFO]: Restarting steamwebhelper"),
        _line("Fetching RSS feed from: https://example.invalid/feed.xml"),
        # A foreign plugin whose own records happen to be shaped like ours, and
        # a foreign line that names this plugin. Neither is ours to publish.
        _line("activity event=sync.started path=/home/deck/private/secret-notes"),
        _line("[Other] failure talking to CE Decky: /home/deck/private/secret-notes"),
        _line(_ours("activity event=backend.unload_started")),
    ]).encode("utf-8")

    class _Completed:
        returncode = 0
        stderr = b""

    completed = _Completed()
    completed.stdout = stdout
    monkeypatch.setattr(journal_records.shutil, "which", lambda _name: "/usr/bin/journalctl")
    monkeypatch.setattr(journal_records.subprocess, "run", lambda *a, **k: completed)

    collected = journal_records.collect()

    assert collected["ok"] is True
    assert collected["kept"] == 3
    assert collected["dropped_not_ours"] == 4
    body = "\n".join(collected["lines"])
    assert "ce_launch.started" in body
    assert "Restarting steamwebhelper" in body
    assert "Wine Cask" not in body
    assert "example.invalid" not in body
    assert "secret-notes" not in body


def test_from_decky_counts_the_records_that_survived_the_line_cap(monkeypatch):
    """The count describes what is handed back, or it describes nothing.

    The window is scanned whole and then cut to its newest `max_lines`, so a
    lifecycle line early in a busy window is counted and then dropped. Reported
    from the scan, `from_decky` then said the bundle held a record of Decky's
    while the cut had just removed the only one, which suppressed the note that
    exists to say the file has no restart evidence in it.
    """
    class _Completed:
        returncode = 0
        stderr = b""

        def __init__(self, stdout: bytes) -> None:
            self.stdout = stdout

    stdout = "\n".join(
        [_line("[localplatformlinux][INFO]: Restarting steamwebhelper")]
        + [_line(_ours(f"activity event=ce_launch.started app_id={index}")) for index in range(8)]
    ).encode("utf-8")
    monkeypatch.setattr(journal_records.shutil, "which", lambda _name: "/usr/bin/journalctl")
    monkeypatch.setattr(journal_records.subprocess, "run", lambda *_a, **_k: _Completed(stdout))

    collected = journal_records.collect(max_lines=5)

    body = "\n".join(collected["lines"])
    assert "Restarting steamwebhelper" not in body
    assert collected["kept"] == 5
    assert collected["from_decky"] == 0
    # And what the cut cost is reported rather than left to be inferred from a
    # kept count that is exactly the limit.
    assert collected["dropped_over_limit"] == 4


def test_the_journal_reaches_the_archive_and_is_marked_as_read(tmp_path):
    # `AGENTS.md`: a new record is not evidence until the bundle collects it and
    # a regression proves it does.
    paths = PluginPaths.for_tests(Path(tmp_path))
    paths.ensure()
    collected = {
        "ok": True,
        "identifier": "PluginLoader",
        "since": "-6h",
        "lines": [_line(_ours("activity event=ce_launch.started app_id=10"))],
        "kept": 1,
        "dropped_not_ours": 7,
    }

    result = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None,
        provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
        journal=collected,
    )

    with zipfile.ZipFile(Path(str(result["path"]))) as archive:
        body = archive.read("logs/journal.txt").decode("utf-8")
        summary = json.loads(archive.read("diagnostics/journal.json"))
    assert "ce_launch.started" in body
    # The count of what was dropped is kept, so a reader knows the window was
    # not empty and that other plugins' output was deliberately left out.
    assert summary["dropped_not_ours"] == 7
    # The lines are in the archive rather than duplicated into the manifest.
    assert "lines" not in summary


def test_a_bundle_collected_without_a_journal_says_so_rather_than_saying_empty(tmp_path):
    paths = PluginPaths.for_tests(Path(tmp_path))
    paths.ensure()

    result = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None,
        provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
    )

    notes = [note for note in result["notes"] if note["member"] == "logs/journal.txt"]
    assert notes and "not read" in notes[0]["reason"]
    with zipfile.ZipFile(Path(str(result["path"]))) as archive:
        assert "diagnostics/journal.json" not in archive.namelist()


def test_journalctl_runs_against_the_system_libraries_not_the_bundle(monkeypatch):
    """Decky's loader is a PyInstaller bundle, and a system binary must not inherit it.

    The loader points the dynamic loader at its own unpacked libraries. Anything
    this plugin runs inherits that unless it is undone, and on the device
    `journalctl` failed every single time with `libcrypto.so.3: version
    OPENSSL_3.4.0 not found`. The support bundle then carried no journal at all,
    which is the one record that survives the install that deletes the log file
    holding a failure. Every other child process here already goes through
    `child_environment`; this was the one that did not.
    """
    seen: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(journal_records.shutil, "which", lambda _name: "/usr/bin/journalctl")
    monkeypatch.setattr(
        journal_records.subprocess, "run",
        lambda *args, **kwargs: (seen.update(kwargs), _Completed())[1],
    )
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIbundle")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/usr/lib")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/_MEIbundle/libthing.so")
    monkeypatch.delenv("LD_PRELOAD_ORIG", raising=False)

    assert journal_records.collect()["ok"] is True

    env = seen.get("env")
    assert isinstance(env, dict), "journalctl inherited this process's environment"
    # The bundle's paths are undone the way PyInstaller documents: the saved
    # original comes back, and a variable with no original goes away.
    assert env["LD_LIBRARY_PATH"] == "/usr/lib"
    assert "LD_PRELOAD" not in env
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_a_window_that_never_held_deckys_records_is_not_a_note_about_this_bundle(tmp_path: Path):
    """This is the ordinary case on every device, so it is not a member problem.

    The loader writes its lifecycle records as root and a plugin is spawned with
    `setuid` to the host user and no supplementary groups, so a bundle collected
    from the panel never sees one however long the window is. Noting it made the
    panel report a missing item on every healthy archive, which is a default
    that cannot succeed. The archive still says it, in the README beside the
    member it is about and in the counts under `diagnostics/journal.json`.
    """
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()

    def bundle(journal: dict[str, object]) -> dict[str, object]:
        return create_support_bundle(
            paths,
            status=None, diagnostics=None, self_test=None, session_inventory=None,
            runtime=[], launch_capabilities=[], provider_diagnostics=None,
            provider_sources=None, blocked_tables=None,
            frontend_log=[], frontend_dropped=0, version=__version__,
            journal=journal,
        )

    result = bundle({
        "ok": True, "kept": 1, "from_decky": 0, "dropped_not_ours": 0,
        "lines": [_line(_ours("activity event=backend.initialized"))],
    })
    assert not [note for note in result["notes"] if note["member"] == "logs/journal.txt"]
    with zipfile.ZipFile(Path(str(result["path"]))) as archive:
        readme = archive.read("README.txt").decode("utf-8")
        summary = json.loads(archive.read("diagnostics/journal.json"))
    assert "cannot read those" in readme
    assert summary["from_decky"] == 0
    assert summary["from_decky_written"] == 0

    # And a window that did hold them says nothing either.
    quiet = bundle({
        "ok": True, "kept": 2, "from_decky": 1, "dropped_not_ours": 0,
        "lines": [_line(_ours("activity event=backend.initialized")),
                  _line("[localplatformlinux][INFO]: Restarting steamwebhelper")],
    })
    assert not [note for note in quiet["notes"] if note["member"] == "logs/journal.txt"]


def test_the_lines_the_window_held_past_the_line_budget_are_still_said(tmp_path: Path):
    """The other way this file loses records, and the one the byte cap no longer causes.

    `journal_records` counts what it hands over after applying its own line cap,
    so when that cap drops the head of a window `from_decky` is already zero and
    the note about Decky's records cannot fire. What is missing is still a fact
    about our own lines, and a reader has to be told the file is the newest part
    of a longer window rather than the whole of it.
    """
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()

    result = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None,
        provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
        journal={
            "ok": True, "kept": 3, "from_decky": 0, "dropped_not_ours": 0,
            "dropped_over_limit": 600,
            "lines": [_line(_ours(f"activity event=ce_launch.started app_id={index}")) for index in range(3)],
        },
    )
    notes = [note for note in result["notes"] if note["member"] == "logs/journal.txt"]
    assert notes, result["notes"]
    assert notes[0]["kind"] == "partial"
    assert "600 older one(s) were past the line budget" in notes[0]["reason"]

    # And a window that fit says nothing, so this cannot become another note
    # that fires on every bundle.
    quiet = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None,
        provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
        journal={
            "ok": True, "kept": 1, "from_decky": 0, "dropped_not_ours": 0,
            "dropped_over_limit": 0,
            "lines": [_line(_ours("activity event=backend.initialized"))],
        },
    )
    assert not [note for note in quiet["notes"] if note["member"] == "logs/journal.txt"]


def test_a_readable_window_holding_nothing_of_ours_is_not_a_failure(tmp_path: Path):
    # The panel counts the notes that say an item could not be collected, and
    # this one was among them: a device that had simply not run the plugin
    # inside the window reported a healthy bundle as missing something. The
    # note stays, because a reader looking for those lines should find out why
    # they are not there.
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()

    result = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None,
        provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
        journal={"ok": True, "kept": 0, "from_decky": 0, "dropped_not_ours": 0,
                 "dropped_over_limit": 0, "lines": []},
    )
    notes = [note for note in result["notes"] if note["member"] == "logs/journal.txt"]
    assert notes, result["notes"]
    assert notes[0]["kind"] == "absent"
    assert "held none of this plugin's records" in notes[0]["reason"]


def test_a_panel_that_has_not_flushed_yet_is_not_a_failed_collection(tmp_path: Path):
    # The panel flushes on its own schedule, so a bundle taken shortly after it
    # loads legitimately finds no file at all. That is nothing to collect rather
    # than something that could not be collected, and the panel counts only the
    # second.
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()

    result = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None,
        provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
        journal=None,
    )
    notes = [note for note in result["notes"] if note["member"] == "logs/frontend-journal.jsonl"]
    assert notes, result["notes"]
    assert notes[0]["kind"] == "absent"
    assert "has not flushed anything to disk yet" in notes[0]["reason"]


def test_the_partial_note_follows_the_bytes_the_bundle_actually_wrote(tmp_path: Path):
    """There is a second truncation after the collector's, and it drops records too.

    `logs/journal.txt` is the tail of what was collected, cut to the bundle's
    own byte budget. A window whose only record of Decky's is older than that
    tail therefore reaches the archive with none of them in it, and a note
    decided from the collector's counts would have stayed quiet about exactly
    the file it is there to describe.
    """
    paths = PluginPaths.for_tests(tmp_path)
    paths.ensure()

    lifecycle = _line("[localplatformlinux][INFO]: Restarting steamwebhelper")
    lines = [lifecycle]
    # Counted as the lines are added rather than re-summed for each of them:
    # this window is four megabytes of records, so the running total is the
    # difference between a fixture that takes twenty seconds and one that takes
    # under a second.
    written = len(lifecycle) + 1
    while written <= MAX_SYSTEM_JOURNAL_BYTES:
        line = _line(_ours(f"activity event=ce_launch.started app_id={len(lines)}"))
        lines.append(line)
        written += len(line) + 1

    result = create_support_bundle(
        paths,
        status=None, diagnostics=None, self_test=None, session_inventory=None,
        runtime=[], launch_capabilities=[], provider_diagnostics=None,
        provider_sources=None, blocked_tables=None,
        frontend_log=[], frontend_dropped=0, version=__version__,
        journal={
            "ok": True, "kept": len(lines), "from_decky": 1, "dropped_not_ours": 0,
            "dropped_over_limit": 0, "lines": lines,
        },
    )

    with zipfile.ZipFile(Path(str(result["path"]))) as archive:
        body = archive.read("logs/journal.txt").decode("utf-8")
        summary = json.loads(archive.read("diagnostics/journal.json"))
    assert "Restarting steamwebhelper" not in body
    # The file's own counts are beside the collector's, which still describe the
    # lines it handed over rather than the ones that fit.
    assert summary["from_decky"] == 1
    assert summary["from_decky_written"] == 0
    assert summary["lines_written"] == len(body.splitlines())
    notes = [note for note in result["notes"] if note["member"] == "logs/journal.txt"]
    partial = [note for note in notes if note.get("kind") == "partial"]
    assert partial, notes
    # And it says which of the causes this was, because a reader acts on the two
    # differently: a shorter window would have kept these.
    assert "does not reach back to the 1 record(s)" in partial[0]["reason"]
    assert "target_plugin_log.py --journal" in partial[0]["reason"]


def test_the_collector_says_how_much_of_the_window_was_deckys_own(monkeypatch):
    """A window with none of Decky's records in it is the ordinary answer here.

    The loader runs as root and writes its lifecycle records as root; this
    backend is spawned with `setuid` to the host user and no supplementary
    groups, so journald shows it only what that user produced. Four thousand of
    this plugin's own lines then read as a complete journal that happens to have
    no restarts in it, which is the opposite of what those lines are collected
    for.
    """
    ours = "activity event=backend.initialized"
    theirs = "[localplatformlinux][INFO]: Restarting steamwebhelper"
    class _Completed:
        returncode = 0
        stderr = b""

        def __init__(self, stdout: bytes) -> None:
            self.stdout = stdout

    monkeypatch.setattr(journal_records.shutil, "which", lambda _name: "/usr/bin/journalctl")
    monkeypatch.setattr(
        journal_records.subprocess, "run",
        lambda *_args, **_kwargs: _Completed(f"{_line(_ours(ours))}\n{_line(theirs)}\n".encode("utf-8")),
    )
    both = journal_records.collect()
    assert both["kept"] == 2 and both["from_decky"] == 1

    monkeypatch.setattr(
        journal_records.subprocess, "run",
        lambda *_args, **_kwargs: _Completed(f"{_line(_ours(ours))}\n".encode("utf-8")),
    )
    alone = journal_records.collect()
    assert alone["kept"] == 1 and alone["from_decky"] == 0
