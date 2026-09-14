"""A regression has to fail at the revision it was written against."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import qa_baseline_check as baseline


FAKE_QA = '''\
import argparse, json, sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--vitest", action="append", default=[])
parser.add_argument("--pytest", action="append", default=[])
parser.parse_args()

source = (root / "src" / "thing.txt").read_text().strip()
selected = (root / "tests" / "probe.txt").read_text().strip()
run = root / "build" / "qa" / "run"
(run / "logs").mkdir(parents=True, exist_ok=True)
log = run / "logs" / "stage.out.txt"
failed = source == "old" and selected == "new-test"
log.write_text("   \\u00d7 the suite > the case 5ms\\n" if failed else "ok\\n")
(run / "qa-summary.json").write_text(json.dumps({
    "stages": [{
        "key": "stage",
        "status": "failed" if failed else "passed",
        "stdout_log": "build/qa/run/logs/stage.out.txt",
    }],
}))
print("summary: build/qa/run/qa-summary.json")
sys.exit(1 if failed else 0)
'''


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "scripts" / "qa.py").write_text(FAKE_QA, encoding="utf-8")
    (root / "src" / "thing.txt").write_text("old\n", encoding="utf-8")
    (root / "tests" / "probe.txt").write_text("old-test\n", encoding="utf-8")
    run = ["git", "-c", "user.email=a@b", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run([*run, "add", "-A"], cwd=root, check=True)
    subprocess.run([*run, "commit", "-qm", "baseline"], cwd=root, check=True)
    return root


def test_the_working_tree_tests_run_against_the_revision_source(tmp_path, monkeypatch, capsys):
    # The whole point of the check. The tests are this checkout's, because a
    # test that does not exist at the baseline cannot be run there; the
    # production source is the baseline's, because that is what is being proved
    # to have the defect.
    root = _repo(tmp_path)
    (root / "src" / "thing.txt").write_text("new\n", encoding="utf-8")
    (root / "tests" / "probe.txt").write_text("new-test\n", encoding="utf-8")
    monkeypatch.setattr(baseline, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["qa_baseline_check.py", "--rev", "HEAD", "--vitest", "tests/probe.txt"])

    assert baseline.main() == 0
    printed = capsys.readouterr().out
    assert "fails: the suite > the case" in printed

    # The checkout this ran from is untouched, which is the reason it happens in
    # a worktree at all: every round here ends in a commit, a push and an
    # install onto a device.
    assert (root / "src" / "thing.txt").read_text().strip() == "new"
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=root, text=True, capture_output=True, check=True,
    ).stdout
    assert listed.strip().count("\n") == 0


def test_a_selection_that_passes_at_the_revision_proves_nothing(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    (root / "src" / "thing.txt").write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(baseline, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["qa_baseline_check.py", "--rev", "HEAD", "--vitest", "tests/probe.txt"])

    assert baseline.main() == 1
    assert "proves nothing" in capsys.readouterr().out


def test_a_run_that_names_no_failing_test_is_not_a_proved_regression(tmp_path, monkeypatch, capsys):
    # A run that could not start fails too, and reading that as the regression
    # doing its job is how a check comes to report a defect nobody proved.
    root = _repo(tmp_path)
    (root / "scripts" / "qa.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-aqm", "broken runner"],
        cwd=root, check=True,
    )
    monkeypatch.setattr(baseline, "ROOT", root)
    monkeypatch.setattr("sys.argv", ["qa_baseline_check.py", "--rev", "HEAD", "--vitest", "tests/probe.txt"])

    assert baseline.main() == 1
    assert "no failing test was named" in capsys.readouterr().out


@pytest.mark.parametrize(
    "line, expected",
    [
        ("   × a suite > a case 12ms", "a suite > a case"),
        ("FAILED tests/test_thing.py::test_case", "tests/test_thing.py::test_case"),
    ],
)
def test_both_runners_name_a_failed_test_in_their_own_way(tmp_path, line, expected):
    log = tmp_path / "stage.out.txt"
    log.write_text(f"noise\n{line}\n{line}\nmore noise\n", encoding="utf-8")
    # Deduplicated, because a failure is printed once in the dot summary and
    # again in the block underneath it.
    assert baseline._failures(log) == [expected]
