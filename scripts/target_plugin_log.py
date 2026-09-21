#!/usr/bin/env python3
"""Return the tail of the live CE Decky backend log, newest file first.

Decky writes one log file per plugin load, named for the moment of that load,
and keeps only the last few. Every install rewrites the set, and development
here installs constantly, so the file to read has a different name every time:
reaching it by hand is `ls -t <dir> | head -1` piped into `grep`, which is the
`grep`-over-Decky-paths shape the tracked-helper rule exists to remove, and it
silently reads the wrong file the moment an install lands between two calls.

The directory is derived from the plugin root rather than guessed. Decky's own
layout puts a plugin's log directory beside its plugin directory under the same
`DECKY_HOME`, which is the template contract `docs/FIELD_NOTES.md` pins; pass
`--log-dir` to override it for a device that does not follow it.

Read-only. It never writes, rotates or removes a log file, and it prints what
the backend already wrote into a file that is collected into the support bundle
anyway.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __package__:
    from . import host_platform
else:
    import host_platform


def _plugin_modules():
    """The two plugin modules the alternative sources read through.

    Imported on use rather than at module load, and deliberately. This helper's
    primary job is reading Decky's own log files, which depends on nothing in
    this repository; importing the plugin package up here would make a checkout
    without `py_modules/` break that job too, for the sake of two options it was
    not asked for. `--journal` and `--frontend` are the only callers.
    """
    root = Path(__file__).resolve().parent.parent / "py_modules"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from ce_decky import frontend_journal, journal_records

    return frontend_journal, journal_records

# A tail is for reading, not for exporting: a whole load's log can be megabytes
# after a long session, and nothing here is served by returning all of it.
MAX_LINES = 5000
DEFAULT_LINES = 80
# Per line, because one activity record carries an address list and a status
# payload and can be several kilobytes on its own.
MAX_LINE_CHARS = 4096
# The detached updater's own record, written beside Decky's per-load logs by a
# process that outlives the plugin. Its name is fixed by
# `ce_decky.update_manager.RUNNER_LOG_FILENAME`.
UPDATER_LOG_NAME = "plugin-update-runner.jsonl"
# How far back the newest-first search will look. Decky keeps five.
MAX_FILES = 16


def _directory(raw: Path, label: str) -> Path:
    raw = raw.expanduser()
    if raw.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    path = raw.resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"{label} must be a directory")
    return path


def _recorded_plugin_root() -> Path | None:
    """Where the installer says this plugin is, without being told again.

    The same authority `target_plugin_install.py authority` prints, read here
    rather than typed on the command line: every reader of a log on this device
    has to name the root first, and the one fact nobody should have to supply is
    the one another helper already recorded. A device that has never held an
    install of ours has no answer, and then this asks for one.
    """
    try:
        from target_plugin_install import latest_install_authority
    except ImportError:
        return None
    try:
        recorded = latest_install_authority()
    except Exception:  # noqa: BLE001 - no authority is an answer, not a failure
        return None
    root = recorded.get("plugin_root") if isinstance(recorded, dict) else None
    return Path(str(root)) if isinstance(root, str) and root else None


def log_directory(plugin_root: Path | None, log_dir: Path | None) -> Path:
    if log_dir is not None:
        return _directory(log_dir, "log directory")
    if plugin_root is None:
        plugin_root = _recorded_plugin_root()
    if plugin_root is None:
        raise ValueError("pass --plugin-root, which target_plugin_install.py authority reports, or --log-dir")
    root = _directory(plugin_root, "plugin root")
    # `<DECKY_HOME>/plugins/<name>` and `<DECKY_HOME>/logs/<name>` are siblings.
    return _directory(root.parent.parent / "logs" / root.name, "log directory")


def _log_files(directory: Path) -> list[tuple[float, str, Path]]:
    """Every readable log file, with the write time to order it by.

    The stat is taken here rather than from a sort key, because this helper is
    reached around installs and an install rewrites the whole set: a file that
    the glob listed and the stat no longer finds is a file that has just been
    rotated away, not a reason to fail the read.
    """
    found: list[tuple[float, str, Path]] = []
    for path in directory.glob("*.log"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            found.append((path.stat().st_mtime, path.name, path))
        except OSError:
            continue
    return found


def newest_files(directory: Path, count: int) -> list[Path]:
    """The most recently written log files, newest first.

    By modification time rather than by the timestamp in the name: the name is
    the moment the load began and the file goes on being written afterwards, so
    on a reload that failed early the two disagree about which one is current.
    """
    files = sorted(_log_files(directory), reverse=True)
    return [path for _mtime, _name, path in files[:max(1, min(count, MAX_FILES))]]


def tail(path: Path, lines: int, pattern: re.Pattern[str] | None) -> tuple[list[str], int]:
    """The last `lines` matching lines of one file, and how many it skipped.

    Read whole and then cut, because a plugin log is written in UTF-8 records
    that a byte-wise seek would land in the middle of, and the files Decky keeps
    are small enough that the simple answer is the correct one.
    """
    # A bounded queue rather than a list that is trimmed from the front: the
    # trim is linear in what is kept, so on a long session's log the cost is the
    # length of the file times the size of the tail.
    kept: deque[str] = deque(maxlen=max(1, lines))
    scanned = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            scanned += 1
            text = line.rstrip("\n")
            if pattern is not None and not pattern.search(text):
                continue
            kept.append(text[:MAX_LINE_CHARS])
    return list(kept), scanned


def report(
    directory: Path, files: int, lines: int, pattern: re.Pattern[str] | None,
) -> dict[str, object]:
    available = _log_files(directory)
    found = newest_files(directory, files)
    entries = []
    # Oldest of the requested set first, so a tail of several files reads in the
    # order the backend wrote them.
    for path in reversed(found):
        try:
            kept, scanned = tail(path, lines, pattern)
        except OSError as exc:
            # Rotated away between the listing and the read, which is what an
            # install landing underneath this looks like.
            entries.append({"file": str(path), "lines_scanned": 0, "lines": [], "error": str(exc)[:256]})
            continue
        entries.append({
            "file": str(path),
            "lines_scanned": scanned,
            "lines": kept,
        })
    # One shape either way, so a caller reading this never has to ask which of
    # the two it got. `files_present` counts what the same listing counted, so a
    # symlink this refuses to read is not reported as a file it could have.
    return {
        "schema": 1,
        "log_dir": str(directory),
        # Every log file the directory holds, so a report can say what was
        # available as well as what was read: an install rotates the set, and a
        # question about a session before one is a question about a file that no
        # longer exists.
        "files_present": len(available),
        "files": entries,
        "reason": None if entries else "no plugin log files were found",
    }


def journal_report(since: str, lines: int, pattern: re.Pattern[str] | None) -> dict[str, object]:
    """The same backend records out of the system journal.

    Decky writes one log file per plugin load and keeps only the last few, so
    the file holding a failure is routinely deleted by the next install: that is
    why one reported refusal in `docs/FIELD_NOTES.md` could not be diagnosed,
    and why the 2026-09-12 panel-close bundle carried no backend log at all. The
    journal keeps the same records across the reload, the webhelper restart and
    the reboot, so this is where a question about a session before an install is
    actually answerable.

    Only this plugin's own lines are returned. Every Decky plugin logs under the
    same identifier, and the filter is the one the support bundle uses, so what
    is read here is what a bug report would carry.
    """
    _, journal_records = _plugin_modules()
    collected = journal_records.collect(since=since)
    kept = [
        line[:MAX_LINE_CHARS] for line in collected.get("lines", [])
        if pattern is None or pattern.search(line)
    ]
    return {
        "schema": 1,
        "source": "journal",
        "since": since,
        "ok": collected.get("ok", False),
        "reason": None if collected.get("ok") else collected.get("reason"),
        "dropped_not_ours": collected.get("dropped_not_ours", 0),
        "lines": kept[-max(1, lines):],
    }


def frontend_report(state_root: Path, lines: int, pattern: re.Pattern[str] | None) -> dict[str, object]:
    """What the Quick Access panel recorded, from the copy that outlives it.

    The panel keeps its own record in the renderer, and recovering a wedged
    panel restarts Steam's webhelper, which destroys it. This is the copy the
    panel flushes as it goes, so it is the one that still exists afterwards.

    `last_event` is the field to read first: a panel that closed normally ends
    on `panel.dismounted`, and one that was killed while wedged ends on whatever
    it was doing at the time.
    """
    frontend_journal, _ = _plugin_modules()
    path = frontend_journal.journal_path(state_root.expanduser())
    summary = frontend_journal.summarize(path)
    data, truncated = frontend_journal.read_tail(path, max_bytes=frontend_journal.MAX_JOURNAL_BYTES)
    kept = [
        text[:MAX_LINE_CHARS]
        for text in data.decode("utf-8", "replace").splitlines()
        if text.strip() and (pattern is None or pattern.search(text))
    ]
    return {
        "schema": 1,
        "source": "frontend",
        "path": str(path),
        "front_truncated": truncated,
        "reason": None if summary.get("present") else "the panel has not flushed anything to disk yet",
        **{key: value for key, value in summary.items() if key != "present"},
        "lines": kept[-max(1, lines):],
    }


def updater_report(directory: Path, lines: int, pattern: re.Pattern[str] | None) -> dict[str, object]:
    """What the detached updater did, from the record only it writes.

    An update replaces this plugin, so Decky stops the backend whose log the
    files above are: everything that happens from the install onwards happens in
    a process that outlives it and writes here instead. Without this the
    interesting half of a failed self-update is in no log anybody reads.

    The file is appended to and never rotated, and it is small by construction:
    the runner bounds what it writes.
    """
    path = directory / UPDATER_LOG_NAME
    if path.is_symlink() or not path.is_file():
        return {
            "schema": 1, "source": "updater", "path": str(path),
            "reason": "no update has been installed from this device yet",
            "lines": [],
        }
    kept = [
        text[:MAX_LINE_CHARS]
        for text in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if text.strip() and (pattern is None or pattern.search(text))
    ]
    return {
        "schema": 1, "source": "updater", "path": str(path), "reason": None,
        "lines": kept[-max(1, lines):],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plugin-root", type=Path, help="exact Decky plugin directory; `target_plugin_install.py authority` reports it as plugin_root")
    parser.add_argument("--log-dir", type=Path, help="exact log directory, for a layout the plugin root does not imply")
    parser.add_argument("--lines", type=int, default=DEFAULT_LINES, help=f"lines kept per file, 1-{MAX_LINES}")
    parser.add_argument("--files", type=int, default=1, help="how many of the newest log files to read, newest last")
    parser.add_argument("--grep", help="keep only lines matching this regular expression")
    parser.add_argument(
        "--journal", action="store_true",
        help="read the system journal instead of the log files, which keeps this plugin's records"
             " across a reload, a webhelper restart and a reboot",
    )
    parser.add_argument(
        "--since", default="-6h",
        help="how far back --journal reads, in journalctl's own syntax; pass it as"
             " --since=-2h, because a value starting with - is otherwise read as an"
             " option rather than as this one's value (default %(default)s)",
    )
    parser.add_argument(
        "--updater", action="store_true",
        help="read what the detached plugin updater did, which no backend log holds",
    )
    parser.add_argument(
        "--frontend", action="store_true",
        help="read what the Quick Access panel recorded, from the copy that outlives its renderer",
    )
    parser.add_argument(
        "--state-root", type=Path, default=Path("~/.cheat-engine-decky/state"),
        help="where --frontend looks for the panel's journal (default %(default)s)",
    )
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON instead of the lines")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        # Reads files and nothing else: no process table, so no procfs.
        host_platform.require("The plugin log reader", needs_procfs=False)
    except host_platform.UnsupportedHost as exc:
        return host_platform.refuse(exc)
    if not 1 <= args.lines <= MAX_LINES:
        print(f"target plugin log: --lines must be between 1 and {MAX_LINES}", file=sys.stderr)
        return 2
    try:
        pattern = re.compile(args.grep) if args.grep else None
    except re.error as exc:
        print(f"target plugin log: --grep is not a valid regular expression: {exc}", file=sys.stderr)
        return 2
    if sum((bool(args.journal), bool(args.frontend), bool(args.updater))) > 1:
        print("target plugin log: pass one of --journal, --frontend and --updater", file=sys.stderr)
        return 2
    # Both alternative sources answer for themselves and need no plugin root:
    # the journal is the system's, and the panel's journal is under the managed
    # state root rather than beside Decky's own logs.
    if args.journal or args.frontend:
        result = (
            journal_report(args.since, args.lines, pattern) if args.journal
            else frontend_report(args.state_root, args.lines, pattern)
        )
        if args.json:
            json.dump(result, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
            sys.stdout.write("\n")
            return 0
        if result["reason"]:
            print(f"target plugin log: {result['reason']}", file=sys.stderr)
            return 2
        if args.frontend:
            print(f"# {result['path']}   last: {result.get('last_event')} at {result.get('last_at')}")
        for line in result["lines"]:  # type: ignore[union-attr]
            print(line)
        return 0
    try:
        directory = log_directory(args.plugin_root, args.log_dir)
        result = updater_report(directory, args.lines, pattern) if args.updater else report(
            directory, args.files, args.lines, pattern,
        )
    except (OSError, ValueError) as exc:
        print(f"target plugin log: {exc}", file=sys.stderr)
        return 2
    if args.json:
        json.dump(result, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0
    if result["reason"]:
        # Text mode has nothing to print, so it refuses. `--json` above returned
        # the report and zero on purpose: an empty directory is an observation a
        # capture should keep, which is the same rule the state probe follows
        # for state it cannot read.
        where = result.get("log_dir") or result.get("path")
        print(f"target plugin log: {result['reason']} in {where}", file=sys.stderr)
        return 2
    if args.updater:
        print(f"# {result['path']}")
        for line in result["lines"]:  # type: ignore[union-attr]
            print(line)
        return 0
    for entry in result["files"]:  # type: ignore[union-attr]
        print(f"# {entry['file']}")
        for line in entry["lines"]:
            print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
