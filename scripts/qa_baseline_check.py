#!/usr/bin/env python3
"""Prove a regression fails against an earlier revision of the production source.

A regression that passes on the commit it was written against says nothing
about the defect it was written for. The check is therefore part of every
review round here: run the new tests with the production source rolled back to
the reviewed head, and expect them to fail.

Done by hand that meant writing baseline source over the working tree, running
the suite, and copying the files back, in a workflow whose every round ends in
a commit, a push and an install onto a device. This does the same thing in a
temporary `git worktree`, so the checkout being worked in is never touched, and
reports which of the selected tests actually failed there.

It answers for one half only. That the tests pass on the current tree is what
the ordinary routed `scripts/qa.py` run already said, and this never repeats
work that run has done.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The tests are taken from the working tree and the production source from the
# revision, which is the whole point: a test that does not exist at the baseline
# cannot be run there, and one that does would be the baseline's own version of
# it rather than the one being proved.
OVERLAID = ("tests",)
MAX_REPORTED_FAILURES = 64
# vitest and pytest name a failed test differently and both are read out of the
# stage log, because the runner's own summary is a count.
VITEST_FAILURE = re.compile(r"^\s*(?:×|✗)\s+(.+?)(?:\s+\d+ms)?$")
PYTEST_FAILURE = re.compile(r"^FAILED\s+(\S+)")


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ("git", *args), cwd=str(cwd or ROOT), text=True, capture_output=True, check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"git {' '.join(args)} failed: {(completed.stderr or completed.stdout).strip()[:512]}"
        )
    return completed.stdout


def _resolve(rev: str) -> str:
    return _git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()


def _overlay_working_tests(worktree: Path) -> None:
    """Put this checkout's tests on the baseline's production source."""
    for name in OVERLAID:
        source = ROOT / name
        if not source.is_dir():
            continue
        target = worktree / name
        if target.exists():
            shutil.rmtree(target)
        # `node_modules` never appears under these, and a build directory is
        # ignored rather than tracked, so this copies test inputs only.
        shutil.copytree(source, target, symlinks=True)


def _link_dependencies(worktree: Path) -> None:
    """Share the restored dependency trees rather than restoring them again.

    Read-only use of a directory this run does not write, which is what keeps a
    baseline check to the cost of the tests themselves instead of a full
    dependency install per revision.
    """
    for name in ("node_modules", ".venv"):
        source = ROOT / name
        if source.is_dir() and not (worktree / name).exists():
            (worktree / name).symlink_to(source, target_is_directory=True)


def _failures(log: Path) -> list[str]:
    if not log.is_file():
        return []
    names: list[str] = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        matched = VITEST_FAILURE.match(line) or PYTEST_FAILURE.match(line)
        if matched:
            name = matched.group(1).strip()
            if name and name not in names:
                names.append(name)
        if len(names) >= MAX_REPORTED_FAILURES:
            break
    return names


def _stage_logs(summary_path: Path) -> list[Path]:
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    logs = []
    for stage in summary.get("stages", []):
        if stage.get("status") != "failed":
            continue
        log = stage.get("stdout_log")
        if isinstance(log, str):
            logs.append(Path(log))
    return logs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rev", required=True, help="the revision to run the tests against")
    parser.add_argument("--vitest", action="append", default=[], metavar="PATH_OR_PATTERN")
    parser.add_argument("--pytest", action="append", default=[], metavar="NODE_OR_PATH")
    parser.add_argument(
        "--expect-pass", action="store_true",
        help="succeed when the selection passes at the revision, for a control case",
    )
    args = parser.parse_args()
    if not args.vitest and not args.pytest:
        parser.error("name at least one --vitest or --pytest selection")

    commit = _resolve(args.rev)
    holder = Path(tempfile.mkdtemp(prefix="ce-decky-baseline-"))
    worktree = holder / "tree"
    try:
        _git("worktree", "add", "--detach", "--quiet", str(worktree), commit)
        _overlay_working_tests(worktree)
        _link_dependencies(worktree)
        command = [sys.executable, str(worktree / "scripts" / "qa.py")]
        for selection in args.vitest:
            command += ["--vitest", selection]
        for selection in args.pytest:
            command += ["--pytest", selection]
        # A fresh worktree has no reuse record, so nothing is skipped here on the
        # strength of a pass the working tree earned.
        completed = subprocess.run(
            command, cwd=str(worktree), text=True, capture_output=True, check=False,
            env={**os.environ, "CE_DECKY_QA_RUN_ID": ""},
        )
        output = completed.stdout + completed.stderr
        summary = next(
            (line.split(":", 1)[1].strip() for line in output.splitlines() if line.strip().startswith("summary:")),
            None,
        )
        failures: list[str] = []
        if summary:
            for log in _stage_logs(worktree / summary):
                failures += [name for name in _failures(worktree / log) if name not in failures]
        selection_text = " ".join(args.vitest + args.pytest)
        if completed.returncode == 0:
            if args.expect_pass:
                print(f"baseline {commit[:12]}: selection passes, as expected ({selection_text})")
                return 0
            print(f"baseline {commit[:12]}: selection PASSES, so it proves nothing ({selection_text})")
            print(output.strip()[-2000:])
            return 1
        if args.expect_pass:
            print(f"baseline {commit[:12]}: selection FAILS but was expected to pass ({selection_text})")
            print(output.strip()[-2000:])
            return 1
        print(f"baseline {commit[:12]}: selection fails, as a regression should")
        for name in failures:
            print(f"  fails: {name}")
        if not failures:
            # The run failed without naming a test, which is a broken run rather
            # than a proved regression and must never read as one.
            print("  no failing test was named; the run itself did not complete")
            print(output.strip()[-2000:])
            return 1
        return 0
    finally:
        subprocess.run(
            ("git", "worktree", "remove", "--force", str(worktree)),
            cwd=str(ROOT), capture_output=True, check=False,
        )
        shutil.rmtree(holder, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
