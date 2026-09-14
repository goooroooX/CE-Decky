"""Reaching the live backend log without `ls -t | head -1` and a guess.

Decky writes one log file per plugin load and keeps only the last few, so the
file to read has a different name after every install - and development here
installs constantly. Selecting it by hand is the `grep`-over-Decky-paths shape
`AGENTS.md` names, and it reads the wrong file the moment an install lands
between two calls.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

from scripts import target_plugin_log


def _log(directory: Path, name: str, body: str, age_s: float = 0.0) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_the_log_directory_is_derived_from_the_plugin_root(tmp_path: Path):
    home = tmp_path / "homebrew"
    (home / "plugins" / "CE-Decky").mkdir(parents=True)
    logs = home / "logs" / "CE-Decky"
    logs.mkdir(parents=True)

    assert target_plugin_log.log_directory(home / "plugins" / "CE-Decky", None) == logs.resolve()


def test_neither_a_plugin_root_nor_a_log_directory_is_refused_by_name(tmp_path: Path):
    with pytest.raises(ValueError, match="authority reports"):
        target_plugin_log.log_directory(None, None)


def test_the_newest_file_is_the_one_written_last_not_the_one_named_last(tmp_path: Path):
    # The name is the moment the load began and the file goes on being written
    # afterwards, so on a reload that failed early the two disagree.
    logs = tmp_path / "logs"
    _log(logs, "2026-09-13 00.10.00.log", "late name, old writes\n", age_s=600)
    current = _log(logs, "2026-09-13 00.01.00.log", "early name, current writes\n")

    assert target_plugin_log.newest_files(logs, 1) == [current]


def test_the_tail_keeps_the_last_lines_and_counts_what_it_passed(tmp_path: Path):
    path = _log(tmp_path, "one.log", "".join(f"line {index}\n" for index in range(10)))
    kept, scanned = target_plugin_log.tail(path, 3, None)
    assert kept == ["line 7", "line 8", "line 9"] and scanned == 10


def test_a_filter_applies_before_the_tail_is_cut(tmp_path: Path):
    # Cutting first and filtering after answers "the last three lines, of which
    # none matched", which is the opposite of the question.
    path = _log(tmp_path, "one.log", "keep a\ndrop\nkeep b\ndrop\ndrop\n")
    kept, scanned = target_plugin_log.tail(path, 2, re.compile("keep"))
    assert kept == ["keep a", "keep b"] and scanned == 5


def test_several_files_read_in_the_order_the_backend_wrote_them(tmp_path: Path):
    logs = tmp_path / "logs"
    _log(logs, "older.log", "older\n", age_s=600)
    _log(logs, "newer.log", "newer\n")

    report = target_plugin_log.report(logs, 2, 10, None)
    assert [Path(entry["file"]).name for entry in report["files"]] == ["older.log", "newer.log"]
    assert report["files_present"] == 2


def test_an_empty_log_directory_says_so_in_the_same_shape_as_a_full_one(tmp_path: Path):
    # One shape either way, so a caller reading this never has to ask which of
    # the two it got.
    logs = tmp_path / "logs"
    logs.mkdir()
    empty = target_plugin_log.report(logs, 1, 10, None)
    _log(logs, "one.log", "a line\n")
    full = target_plugin_log.report(logs, 1, 10, None)

    assert set(empty) == set(full)
    assert empty["files"] == [] and "no plugin log files" in empty["reason"]
    assert empty["files_present"] == 0 and full["files_present"] == 1 and full["reason"] is None


def test_a_file_rotated_away_under_the_read_is_not_a_failed_read(tmp_path: Path, monkeypatch):
    # This helper is reached around installs, and an install rewrites the whole
    # set: a file the listing saw and the read no longer finds has just been
    # rotated, which is an observation rather than an error.
    logs = tmp_path / "logs"
    _log(logs, "one.log", "a line\n")

    def vanish(path: Path, lines: int, pattern) -> tuple[list[str], int]:
        raise OSError("No such file or directory")

    monkeypatch.setattr(target_plugin_log, "tail", vanish)
    report = target_plugin_log.report(logs, 1, 10, None)
    assert report["files"][0]["lines"] == [] and "No such file" in report["files"][0]["error"]


def test_a_symlink_is_neither_read_nor_counted(tmp_path: Path):
    logs = tmp_path / "logs"
    real = _log(logs, "one.log", "a line\n")
    (logs / "link.log").symlink_to(real)

    report = target_plugin_log.report(logs, 4, 10, None)
    assert report["files_present"] == 1
    assert [Path(entry["file"]).name for entry in report["files"]] == ["one.log"]


def test_the_tail_does_not_grow_with_the_length_of_the_file(tmp_path: Path):
    # Trimming a list from the front is linear in what is kept, so on a long
    # session's log the cost was the file's length times the size of the tail.
    path = _log(tmp_path, "one.log", "".join(f"line {index}\n" for index in range(50_000)))
    kept, scanned = target_plugin_log.tail(path, 1000, None)
    assert scanned == 50_000 and len(kept) == 1000 and kept[-1] == "line 49999"


def test_a_very_long_record_is_cut_rather_than_returned_whole(tmp_path: Path):
    # One activity record carries an address list and a status payload.
    path = _log(tmp_path, "one.log", "x" * (target_plugin_log.MAX_LINE_CHARS * 3) + "\n")
    kept, _ = target_plugin_log.tail(path, 1, None)
    assert len(kept[0]) == target_plugin_log.MAX_LINE_CHARS


def test_an_invalid_line_count_and_an_invalid_filter_are_refused(tmp_path: Path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    assert target_plugin_log.main(["--log-dir", str(logs), "--lines", "0"]) == 2
    assert "must be between" in capsys.readouterr().err
    assert target_plugin_log.main(["--log-dir", str(logs), "--grep", "("]) == 2
    assert "not a valid regular expression" in capsys.readouterr().err
